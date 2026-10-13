"""Bounded UTF-8 file tools; source references always point into verified snapshots."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from app.harness.redaction import Redactor
from app.storage.materials import MaterialStore
from app.storage.snapshots import digest
from app.tools.models import ToolFailure, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition


class ListParameters(ToolParameters):
    path: str = Field(default=".", min_length=1, max_length=1024)


class ReadParameters(ToolParameters):
    path: str = Field(min_length=1, max_length=1024)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=3000, ge=1, le=6000)


class SearchContentParameters(ToolParameters):
    keyword: str = Field(min_length=1, max_length=200)
    dir: str = Field(default=".", min_length=1, max_length=1024)
    cursor: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=10, ge=1, le=20)


class WriteParameters(ToolParameters):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=100_000)


@dataclass(frozen=True)
class TextFile:
    path: str
    text: str
    content_hash: str
    byte_count: int
    source_id: str | None = None


class FileAccess:
    def __init__(self, store: MaterialStore, settings):
        self.store, self.settings = store, settings
        self.redactor = Redactor(settings)

    def resolve(self, context, path, *, write=False):
        target = context.resolve_path(path, write=write)
        relative = target.relative_to(context.working_dir).as_posix()
        parts = Path(relative).parts
        if parts and parts[0] not in {"sources", "scratch"}:
            raise PermissionError("only sources and scratch are material directories")
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in path.replace("\\", "/").split("/"):
            if part == "." and path == ".":
                continue
            if part and (
                part in {".", ".."}
                or part.startswith(".")
                or part.endswith((".", " "))
                or any(char in '<>:"|?*' or ord(char) < 32 for char in part)
                or part.split(".")[0].upper() in reserved
            ):
                raise PermissionError("unsafe material filename")
        if self.redactor.contains_credential(relative):
            raise ToolFailure("FILE_SENSITIVE_PATH", "材料路径含敏感凭据，已阻止访问。")
        return target, relative

    def load(self, context, path):
        target, relative = self.resolve(context, path)
        source_id = None
        parts = Path(relative).parts
        expected_hash = None
        if parts and parts[0] == "sources":
            if len(parts) != 3 or parts[2] not in {"body.txt", "index.json"}:
                raise PermissionError("only registered source files may be read")
            try:
                metadata, _ = self.store.sources.get_source(
                    context.run_id, context.user_id, parts[1]
                )
            except LookupError:
                raise ToolFailure("SOURCE_STATE_PROBLEM", "来源材料未登记在当前任务中。") from None
            expected_hash = metadata.content_hash if parts[2] == "body.txt" else metadata.index_hash
            source_id = metadata.source_id if parts[2] == "body.txt" else None
        try:
            if target.exists() and not target.is_file():
                raise ToolFailure("FILE_TYPE_INVALID", "读取目标必须是普通文本文件。")
            with target.open("rb") as file:
                raw = file.read(self.settings.max_material_file_bytes + 1)
        except FileNotFoundError:
            raise ToolFailure("FILE_NOT_FOUND", "当前任务中未找到该文件。") from None
        except IsADirectoryError:
            raise ToolFailure("FILE_TYPE_INVALID", "读取目标必须是普通文本文件。") from None
        except PermissionError:
            raise
        except OSError:
            raise ToolFailure("FILE_IO_ERROR", "材料文件暂时无法读取。") from None
        if len(raw) > self.settings.max_material_file_bytes:
            raise ToolFailure("FILE_TOO_LARGE", "材料文件超过读取大小限制，请缩小材料。")
        content_hash = digest(raw)
        if expected_hash and content_hash != expected_hash:
            raise ToolFailure("SOURCE_STATE_PROBLEM", "正文或索引在核对后发生变化。")
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise ToolFailure(
                "FILE_ENCODING_INVALID", "材料文件必须使用有效 UTF-8 编码。"
            ) from None
        if self.redactor.clean(text) != text:
            raise ToolFailure("FILE_SENSITIVE_CONTENT", "材料含敏感凭据或认证内容，已阻止读取。")
        return TextFile(relative, text, content_hash, len(raw), source_id)

    def walk(self, context, directory, *, strict=False):
        target, _ = self.resolve(context, directory)
        if not target.exists() and target == context.working_dir:
            return []
        if not target.is_dir():
            raise ToolFailure("DIRECTORY_NOT_FOUND", "当前任务中未找到该材料目录。")
        found, count = [], 0

        def visit(folder):
            nonlocal count
            for entry in folder.iterdir():
                count += 1
                if count > self.settings.max_material_files * 4 + 20:
                    raise ToolFailure("FILE_TREE_TOO_LARGE", "材料目录条目过多，请缩小目录。")
                relative = entry.relative_to(context.working_dir).as_posix()
                if entry.name.startswith(".") and not strict:
                    continue
                self.resolve(context, relative)
                if entry.is_dir():
                    visit(entry)
                elif entry.is_file():
                    found.append(relative)
                    if len(found) > self.settings.max_material_files:
                        raise ToolFailure(
                            "FILE_TREE_TOO_LARGE", "材料文件数量超过限制，请缩小目录。"
                        )
                else:
                    raise ToolFailure("FILE_TYPE_INVALID", "材料目录包含非普通文件。")

        visit(target)
        return sorted(found)


def bounded_prefix(count, build, maximum):
    result = build(count)
    if len(result.model_dump_json()) <= maximum:
        return count, result
    low, high = 0, count
    while low < high:
        middle = (low + high + 1) // 2
        if len(build(middle).model_dump_json()) <= maximum:
            low = middle
        else:
            high = middle - 1
    result = build(low)
    if len(result.model_dump_json()) > maximum or (count and not low):
        raise ToolFailure("RESULT_TOO_LARGE", "工具结果无法容纳，请缩小范围或缩短路径。")
    return low, result


class FileTools:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.access = FileAccess(store, settings)

    async def list_dir(self, context, parameters):
        target, relative = self.access.resolve(context, parameters.path)
        entries = []
        if target.exists():
            if not target.is_dir():
                raise ToolFailure("FILE_TYPE_INVALID", "目录工具的目标必须是目录。")
            for entry in target.iterdir():
                if entry.name.startswith("."):
                    continue
                path = entry.relative_to(context.working_dir).as_posix()
                self.access.resolve(context, path)
                if len(entries) >= self.settings.max_material_files:
                    raise ToolFailure("FILE_TREE_TOO_LARGE", "目录条目超过限制，请缩小目录。")
                if not entry.is_dir():
                    material = self.access.load(context, path)
                    entries.append(
                        {
                            "path": path,
                            "type": "file",
                            "char_count": len(material.text),
                            "content_hash": material.content_hash,
                            "source_id": material.source_id,
                        }
                    )
                else:
                    entries.append({"path": path, "type": "directory"})
        elif target != context.working_dir:
            raise ToolFailure("DIRECTORY_NOT_FOUND", "当前任务中未找到该目录。")
        entries.sort(key=lambda entry: entry["path"])
        _, result = bounded_prefix(
            len(entries),
            lambda count: ToolResult.success(
                {
                    "path": relative,
                    "entries": entries[:count],
                    "omitted_count": len(entries) - count,
                }
            ),
            self.settings.max_tool_result_chars,
        )
        return result

    async def read_file(self, context, parameters):
        material = self.access.load(context, parameters.path)
        saved_hash = self.store.read_hash(context, material.path)
        if parameters.offset and saved_hash is not None and saved_hash != material.content_hash:
            raise ToolFailure(
                "FILE_VERSION_CHANGED", "文件已变化，请从 offset=0 重新读取并取得新位置。"
            )
        if parameters.offset > len(material.text):
            raise ToolFailure("FILE_OFFSET_INVALID", "字符起点超出文件结尾。")
        start = parameters.offset
        length = min(parameters.limit, len(material.text) - start)

        def build(count):
            end = start + count
            eof = end == len(material.text)
            return ToolResult.success(
                {
                    "path": material.path,
                    "content": material.text[start:end],
                    "start_offset": start,
                    "end_offset": end,
                    "next_offset": None if eof else end,
                    "eof": eof,
                    "truncated": not eof,
                    "char_count": len(material.text),
                    "content_hash": material.content_hash,
                    "source_id": material.source_id,
                    "offset_unit": "unicode_characters",
                }
            )

        return bounded_prefix(length, build, self.settings.max_tool_result_chars)[1]

    async def search_content(self, context, parameters):
        if not parameters.keyword.strip() or self.access.redactor.contains_credential(
            parameters.keyword
        ):
            raise ToolFailure("FILE_KEYWORD_INVALID", "请输入不含凭据的非空字面关键词。")
        _, directory = self.access.resolve(context, parameters.dir)
        paths = self.access.walk(context, directory)
        paths = [
            path
            for path in paths
            if not (path.startswith("sources/") and path.endswith("/index.json"))
        ]
        materials, total = [], 0
        for path in paths:
            material = self.access.load(context, path)
            total += material.byte_count
            if total > self.settings.max_material_scan_bytes:
                raise ToolFailure("FILE_SCAN_TOO_LARGE", "材料扫描总量超过限制，请指定更小目录。")
            materials.append(material)
        manifest = [{"path": file.path, "hash": file.content_hash} for file in materials]
        keyword_hash = digest(parameters.keyword.encode("utf-8"))
        file_pos, char_pos = (
            self.store.cursor(context, parameters.cursor, directory, keyword_hash, manifest)
            if parameters.cursor
            else (0, 0)
        )
        hits = []
        for position in range(file_pos, len(materials)):
            material = materials[position]
            offset = char_pos if position == file_pos else 0
            while len(hits) <= parameters.limit:
                start = material.text.find(parameters.keyword, offset)
                if start < 0:
                    break
                end = start + len(parameters.keyword)
                hits.append(
                    (
                        {
                            "path": material.path,
                            "source_id": material.source_id,
                            "content_hash": material.content_hash,
                            "start_offset": start,
                            "end_offset": end,
                            "excerpt": material.text[
                                max(0, start - 80) : min(len(material.text), end + 80)
                            ],
                            "excerpt_start_offset": max(0, start - 80),
                            "excerpt_end_offset": min(len(material.text), end + 80),
                        },
                        position,
                        start + 1,
                    )
                )
                offset = start + 1
            if len(hits) > parameters.limit:
                break
        token = self.store.new_cursor()

        def build(count):
            return ToolResult.success(
                {
                    "dir": directory,
                    "keyword": parameters.keyword,
                    "items": [hit[0] for hit in hits[:count]],
                    "next_cursor": token if count < len(hits) else None,
                    "offset_unit": "unicode_characters",
                    "literal_match": True,
                    "guidance": "命中片段用于定位；核实数字和条件请用 read_file 读取相邻正文。"
                    if hits
                    else "该字面关键词未命中，请结合语言换词、查看章节或扩大读取范围。",
                }
            )

        count, result = bounded_prefix(
            min(len(hits), parameters.limit), build, self.settings.max_tool_result_chars
        )
        if count < len(hits):
            _, position, offset = hits[count - 1]
            self.store.save_cursor(
                context, token, directory, keyword_hash, manifest, position, offset
            )
        return result

    async def write_file(self, context, parameters):
        target, relative = self.access.resolve(context, parameters.path, write=True)
        content = (
            self.access.redactor.clean(parameters.content).replace("\r\n", "\n").replace("\r", "\n")
        )
        try:
            raw = content.encode("utf-8")
        except UnicodeError:
            raise ToolFailure("FILE_ENCODING_INVALID", "笔记内容必须能编码为 UTF-8。") from None
        if (
            len(content) > self.settings.max_note_chars
            or len(raw) > self.settings.max_material_file_bytes
        ):
            raise ToolFailure("FILE_TOO_LARGE", "笔记内容超过配置大小限制。")
        current_hash = self.access.load(context, relative).content_hash if target.exists() else None
        return self.store.write(context, relative, target, content, current_hash)


def register_file_tools(registry, store, settings):
    tools = FileTools(store, settings)
    definitions = [
        (
            "list_dir",
            "查看当前任务的 sources/scratch 目录，返回材料路径和版本；省略条目时注明数量。",
            ListParameters,
            tools.list_dir,
            None,
        ),
        (
            "read_file",
            "按零起点 Unicode 字符位置读取 UTF-8 材料，默认3000、最多6000字符；"
            "返回实际范围、next_offset/eof/版本。笔记变化时从offset=0重读；"
            "来源正文的 source_id 与位置可用于引用。",
            ReadParameters,
            tools.read_file,
            store.on_read,
        ),
        (
            "search_content",
            "按非空字面关键词定位当前任务正文与笔记，默认10、最多20条；"
            "服务端游标绑定任务/目录/关键词/材料版本。零命中请换词或读章节，命中后读取相邻正文确认条件。",
            SearchContentParameters,
            tools.search_content,
            None,
        ),
        (
            "write_file",
            "在当前任务 scratch 中原子写入 UTF-8/LF 笔记和草稿；返回内容哈希。"
            "正文不可修改，写文件不代表提交。中断复用相同写入意图，冲突时需重新读取。",
            WriteParameters,
            tools.write_file,
            store.on_written,
        ),
    ]
    for name, description, parameters, handler, hook in definitions:
        registry.register(
            ToolDefinition(
                name,
                description,
                parameters,
                handler,
                retry_safe=name != "write_file",
                on_completed=hook,
            )
        )
