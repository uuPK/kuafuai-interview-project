import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.storage.database as database_module
from app.agent.protocol import ModelRequest
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.conversations import ConversationStore
from app.storage.database import Database
from app.storage.materials import MaterialStore
from app.storage.runs import RunStore
from app.storage.snapshots import digest
from app.storage.sources import SourceStore
from app.tools.context import bind_context
from app.tools.executor import ToolExecutor
from app.tools.files import (
    FileTools,
    ListParameters,
    ReadParameters,
    SearchContentParameters,
    WriteParameters,
)
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.stubs import register_submission_fixture
from tests.test_agent_runner import observation, submit
from tests.test_fetch import Pages, fetch_call, install


def context(env, call_id="write"):
    return bind_context(
        env.runs, env.settings.data_dir, env.run.id, env.run.user_id, tool_call_id=call_id
    )


def test_write_read_unicode_lf_redaction_and_replay(material_env):
    async def run():
        env = material_env
        text = "中文🙂\r\n条件：8 GB；密钥 fixture-deep-key\r结尾"
        written = await env.invoke(
            "write_file", call_id="write", path="scratch/中文.txt", content=text
        )
        assert written.ok
        raw = (env.root / "scratch/中文.txt").read_bytes()
        assert b"\r" not in raw and b"fixture-deep-key" not in raw
        assert written.data["content_hash"] == digest(raw)
        assert (
            await env.invoke("write_file", call_id="write", path="scratch/中文.txt", content=text)
            == written
        )
        read = await env.invoke("read_file", path="scratch/中文.txt", offset=2, limit=2)
        assert read.ok and read.data["content"] == "🙂\n"
        assert (read.data["start_offset"], read.data["end_offset"], read.data["next_offset"]) == (
            2,
            4,
            4,
        )
        assert read.data["offset_unit"] == "unicode_characters"
        assert read.data["truncated"] and not read.data["eof"]
        assert (
            len(
                [
                    e
                    for e in env.runs.list_events(env.run.id, env.run.user_id)
                    if e.kind == "material_written"
                ]
            )
            == 1
        )
        usage = env.runs.get_run(env.run.id, env.run.user_id).budget_usage
        assert usage.tool_calls == 2 and usage.searches == usage.fetches == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "text", ["", "中文🙂" * 1800, '"\\\n' * 1800], ids=["empty", "unicode", "escaped"]
)
def test_pagination_complete_and_exact_material_ranges(material_env, text):
    async def run():
        env = material_env
        env.seed("scratch/body.txt", text)
        offset, output = 0, ""
        while True:
            result = await env.invoke("read_file", path="scratch/body.txt", offset=offset)
            assert result.ok and len(result.model_dump_json()) <= env.settings.max_tool_result_chars
            data = result.data
            assert data["start_offset"] == offset and data["end_offset"] - offset <= 3000
            output += data["content"]
            if data["eof"]:
                assert data["next_offset"] is None and not data["truncated"]
                break
            assert data["next_offset"] > offset
            offset = data["next_offset"]
        assert output == text
        at_end = await env.invoke("read_file", path="scratch/body.txt", offset=len(text))
        assert at_end.ok and at_end.data["content"] == ""
        past = await env.invoke("read_file", path="scratch/body.txt", offset=len(text) + 1)
        assert past.error.code == "FILE_OFFSET_INVALID"

    asyncio.run(run())


def test_read_version_change_requires_restart(material_env):
    async def run():
        env = material_env
        await env.invoke("write_file", path="scratch/a", content="旧正文🙂")
        assert (await env.invoke("read_file", path="scratch/a", limit=1)).ok
        await env.invoke("write_file", path="scratch/a", content="新正文🙂条件")
        stale = await env.invoke("read_file", path="scratch/a", offset=1)
        assert stale.error.code == "FILE_VERSION_CHANGED"
        assert (await env.invoke("read_file", path="scratch/a", offset=0)).ok
        assert (await env.invoke("read_file", path="scratch/a", offset=1)).data[
            "content"
        ] == "正文🙂条件"

    asyncio.run(run())


def test_literal_search_cursor_readback_and_database_reopen(material_env):
    async def run():
        env = material_env
        text = "中文🙂C++ [AI] 在8 GB显存条件下运行；C++ [AI]后续 C++ [AI]"
        env.seed("scratch/a", text)
        env.seed("scratch/b", "C++ [AI] English")
        cursor, all_hits = None, []
        while True:
            result = await env.invoke(
                "search_content", keyword="C++ [AI]", dir="scratch", cursor=cursor, limit=2
            )
            assert result.ok
            all_hits.extend(result.data["items"])
            cursor = result.data["next_cursor"]
            if not cursor:
                break
            reopened = Database(env.database.path)
            reopened.initialize()
            store = MaterialStore(
                SourceStore(
                    CandidateStore(CallStore(RunStore(reopened, env.settings))), env.settings
                ),
                env.settings,
            )
            assert store.cursor(
                context(env),
                cursor,
                "scratch",
                digest(b"C++ [AI]"),
                [
                    {"path": "scratch/a", "hash": digest(text.encode())},
                    {"path": "scratch/b", "hash": digest(b"C++ [AI] English")},
                ],
            ) == (0, text.index("C++ [AI]", text.index("C++ [AI]") + 1) + 1)
        assert len(all_hits) == 4
        assert len({(hit["path"], hit["start_offset"]) for hit in all_hits}) == 4
        for hit in all_hits:
            read = await env.invoke(
                "read_file",
                path=hit["path"],
                offset=hit["start_offset"],
                limit=hit["end_offset"] - hit["start_offset"],
            )
            assert (
                read.data["content"] == "C++ [AI]"
                and read.data["content_hash"] == hit["content_hash"]
            )
        assert "8 GB" in all_hits[0]["excerpt"]
        assert (await env.invoke("search_content", keyword="c++ [ai]")).data["items"] == []

    asyncio.run(run())


@pytest.mark.parametrize("change", ["keyword", "directory", "forged", "content", "add", "delete"])
def test_cursor_binding_and_material_changes(material_env, change):
    async def run():
        env = material_env
        target = env.seed("scratch/a", "AI AI AI")
        result = await env.invoke("search_content", keyword="AI", dir="scratch", limit=1)
        args = {"keyword": "AI", "dir": "scratch", "cursor": result.data["next_cursor"]}
        if change == "keyword":
            args["keyword"] = "A"
        if change == "directory":
            args["dir"] = "."
        if change == "forged":
            args["cursor"] = "forged"
        if change == "content":
            target.write_text("AI changed", encoding="utf-8")
        if change == "add":
            env.seed("scratch/b", "AI")
        if change == "delete":
            target.unlink()
        invalid = await env.invoke("search_content", **args)
        assert invalid.error.code == (
            "FILE_VERSION_CHANGED"
            if change in {"content", "add", "delete"}
            else "FILE_CURSOR_INVALID"
        )

    asyncio.run(run())


def test_overlap_and_zero_hit_guidance(material_env):
    async def run():
        env = material_env
        env.seed("scratch/a", "aaaa中文")
        result = await env.invoke("search_content", keyword="aa")
        assert [hit["start_offset"] for hit in result.data["items"]] == [0, 1, 2]
        miss = await env.invoke("search_content", keyword="English")
        assert miss.ok and not miss.data["items"] and "换词" in miss.data["guidance"]

    asyncio.run(run())


@pytest.mark.parametrize("keyword", [" ", "\n\t", "fixture-deep-key", "sk-" + "x" * 32])
def test_search_rejects_empty_or_sensitive_keywords(material_env, keyword):
    result = asyncio.run(material_env.invoke("search_content", keyword=keyword))
    assert result.error.code == "FILE_KEYWORD_INVALID"


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/etc/passwd",
        "C:/secret",
        "scratch/a:stream",
        "scratch/CON.txt",
        "scratch/.env",
        "scratch/a.",
        "scratch/a ",
        "scratch/../sources/a",
        "source_jobs/a",
        "scratch/sk-" + "x" * 32,
    ],
)
def test_material_paths_reject_escape_and_unsafe_names(material_env, path):
    result = asyncio.run(material_env.invoke("write_file", path=path, content="note"))
    assert result.error.code in {"PERMISSION_DENIED", "FILE_SENSITIVE_PATH"}
    assert not (material_env.settings.data_dir / "file_jobs").exists()


@pytest.mark.parametrize(
    "model,fields",
    [
        (ReadParameters, {"path": "scratch/a", "offset": -1}),
        (ReadParameters, {"path": "scratch/a", "offset": True}),
        (ReadParameters, {"path": "scratch/a", "limit": 6001}),
        (ReadParameters, {"path": "scratch/a", "limit": 0}),
        (SearchContentParameters, {"keyword": ""}),
        (SearchContentParameters, {"keyword": "AI", "limit": 21}),
        (WriteParameters, {"path": "scratch/a", "content": "a", "run_id": "x"}),
    ],
)
def test_strict_material_parameters(model, fields):
    with pytest.raises(ValidationError):
        model.model_validate(fields)


def test_listing_and_non_text_files(material_env):
    async def run():
        env = material_env
        assert (await env.invoke("list_dir")).data["entries"] == []
        env.seed("scratch/z", "末尾🙂")
        env.seed("scratch/a", "开头")
        env.seed("scratch/.hidden", "hidden")
        listing = await env.invoke("list_dir", path="scratch")
        assert [entry["path"] for entry in listing.data["entries"]] == ["scratch/a", "scratch/z"]
        assert listing.data["entries"][1]["char_count"] == 3
        assert (await env.invoke("list_dir", path="scratch/a")).error.code == "FILE_TYPE_INVALID"
        assert (await env.invoke("read_file", path="scratch")).error.code == "FILE_TYPE_INVALID"
        assert (
            await env.invoke("read_file", path="scratch/missing")
        ).error.code == "FILE_NOT_FOUND"
        assert (
            await env.invoke("list_dir", path="scratch/missing")
        ).error.code == "DIRECTORY_NOT_FOUND"
        (env.root / "scratch/a").write_bytes(b"\xff")
        assert (
            await env.invoke("read_file", path="scratch/a")
        ).error.code == "FILE_ENCODING_INVALID"
        env.seed("scratch/a", "fixture-deep-key")
        assert (
            await env.invoke("read_file", path="scratch/a")
        ).error.code == "FILE_SENSITIVE_CONTENT"

    asyncio.run(run())


@pytest.mark.parametrize("link_type", ["is_symlink", "is_junction"])
def test_links_inside_task_are_blocked(material_env, monkeypatch, link_type):
    env = material_env
    env.seed("scratch/link/a", "same task")
    original = getattr(Path, link_type)
    monkeypatch.setattr(Path, link_type, lambda path: path.name == "link" or original(path))
    result = asyncio.run(env.invoke("read_file", path="scratch/link/a"))
    assert result.error.code == "PERMISSION_DENIED"


def test_source_snapshot_reads_and_tamper_detection(material_env):
    async def run():
        env = material_env
        setup = await install(env, Pages(env))
        result = await setup.executor.execute(env.run.id, env.run.user_id, fetch_call(setup.ids[0]))
        path = result.data["file_path"]
        read = await env.invoke("read_file", path=path)
        assert read.ok and read.data["source_id"] == result.data["source_id"]
        index = await env.invoke("read_file", path=path.replace("body.txt", "index.json"))
        assert index.ok and index.data["source_id"] is None
        hits = await env.invoke("search_content", keyword="start_offset", dir="sources")
        assert hits.ok and hits.data["items"] == []
        assert (
            await env.invoke("write_file", path=path, content="changed")
        ).error.code == "PERMISSION_DENIED"
        (env.root / path).write_text("tampered", encoding="utf-8")
        assert (await env.invoke("read_file", path=path)).error.code == "SOURCE_STATE_PROBLEM"

    asyncio.run(run())


def test_result_and_storage_limits(material_env):
    async def run():
        env = material_env
        config = env.settings.model_copy(update={"max_tool_result_chars": 750})
        env.executor = ToolExecutor(env.registry, env.calls, config)
        tools = FileTools(env.materials, config)
        env.seed("scratch/a", '"\\\n' * 1000)
        data = await tools.read_file(context(env), ReadParameters(path="scratch/a"))
        assert len(data.model_dump_json()) <= 750 and data.data["end_offset"] < 3000
        for i in range(4):
            env.seed(f"scratch/n{i}", "AI " * 60)
        listing = await tools.list_dir(context(env), ListParameters(path="scratch"))
        assert listing.data["omitted_count"] > 0
        searched = await tools.search_content(
            context(env), SearchContentParameters(keyword="AI", limit=20)
        )
        assert len(searched.model_dump_json()) <= 750 and searched.data["next_cursor"]
        small = env.settings.model_copy(update={"max_material_file_bytes": 5})
        with pytest.raises(ToolFailure, match="FILE_TOO_LARGE"):
            await FileTools(env.materials, small).read_file(
                context(env), ReadParameters(path="scratch/a")
            )
        with pytest.raises(ToolFailure, match="FILE_TREE_TOO_LARGE"):
            FileTools(
                env.materials, env.settings.model_copy(update={"max_material_files": 2})
            ).access.walk(context(env), "scratch")
        with pytest.raises(ToolFailure, match="FILE_SCAN_TOO_LARGE"):
            await FileTools(
                env.materials, env.settings.model_copy(update={"max_material_scan_bytes": 1})
            ).search_content(context(env), SearchContentParameters(keyword="AI"))
        with pytest.raises(ToolFailure, match="FILE_TOO_LARGE"):
            await FileTools(
                env.materials, env.settings.model_copy(update={"max_note_chars": 1})
            ).write_file(context(env), WriteParameters(path="scratch/new", content="两个"))

    asyncio.run(run())


def test_atomic_write_intent_recovery_and_conflict(material_env, monkeypatch):
    env = material_env
    target = env.seed("scratch/a", "original")
    tools, ctx = FileTools(env.materials, env.settings), context(env)
    params = WriteParameters(path="scratch/a", content="desired")
    real_replace = os.replace

    def interrupt(*args):
        raise OSError("interrupted before replace")

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(OSError):
        asyncio.run(tools.write_file(ctx, params))
    job_path = env.materials.job_path(ctx)
    assert json.loads(job_path.read_text(encoding="utf-8"))["original_hash"] == digest(b"original")
    assert target.read_text() == "original" and not list(target.parent.glob(".pending-*"))
    monkeypatch.setattr(os, "replace", real_replace)
    first = asyncio.run(tools.write_file(ctx, params))
    monkeypatch.setattr(os, "replace", interrupt)
    assert asyncio.run(tools.write_file(ctx, params)) == first
    target.write_text("external change", encoding="utf-8")
    with pytest.raises(ToolFailure, match="FILE_WRITE_CONFLICT"):
        asyncio.run(tools.write_file(ctx, params))
    with pytest.raises(ToolFailure, match="FILE_WRITE_STATE_PROBLEM"):
        asyncio.run(
            tools.write_file(ctx, WriteParameters(path="scratch/a", content="different intent"))
        )
    job_path.write_text("broken", encoding="utf-8")
    with pytest.raises(ToolFailure, match="FILE_WRITE_STATE_PROBLEM"):
        asyncio.run(tools.write_file(ctx, params))


def test_write_finish_and_feedback_roll_back_together(material_env):
    async def run():
        env = material_env
        call = ToolInvocation(
            tool_call_id="write",
            name="write_file",
            arguments='{"path":"scratch/a","content":"note"}',
        )

        def broken(connection, record):
            raise RuntimeError("feedback unavailable")

        with pytest.raises(RuntimeError):
            await env.executor.execute(env.run.id, env.run.user_id, call, on_finished=broken)
        assert (env.root / "scratch/a").read_text() == "note"
        assert not [
            e
            for e in env.runs.list_events(env.run.id, env.run.user_id)
            if e.kind == "material_written"
        ]
        assert env.calls.get_call(env.run.id, env.run.user_id, "write").result is None
        replay = await FileTools(env.materials, env.settings).write_file(
            context(env), WriteParameters(path="scratch/a", content="note")
        )
        env.executor.finish(env.run.id, env.run.user_id, call, replay)
        assert (
            len(
                [
                    e
                    for e in env.runs.list_events(env.run.id, env.run.user_id)
                    if e.kind == "material_written"
                ]
            )
            == 1
        )

    asyncio.run(run())


def test_v6_migration_preserves_run_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    database = Database(path)
    original = database_module.SCHEMA_V7
    monkeypatch.setattr(database_module, "SCHEMA_V7", ())
    latest = database_module.SCHEMA_V8
    worker_schema = database_module.SCHEMA_V11
    delivery_schema = database_module.SCHEMA_V12
    review_schema = database_module.SCHEMA_V10
    submission_schema = database_module.SCHEMA_V9
    monkeypatch.setattr(database_module, "SCHEMA_V8", ())
    monkeypatch.setattr(database_module, "SCHEMA_V11", ())
    monkeypatch.setattr(database_module, "SCHEMA_V12", ())
    monkeypatch.setattr(database_module, "SCHEMA_V10", ())
    monkeypatch.setattr(database_module, "SCHEMA_V9", ())
    database.initialize()
    with database.connection() as connection:
        connection.execute("PRAGMA user_version=6")
    monkeypatch.setattr(database_module, "SCHEMA_V7", (*original, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='material_reads'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V7", original)
    monkeypatch.setattr(database_module, "SCHEMA_V8", latest)
    monkeypatch.setattr(database_module, "SCHEMA_V9", submission_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V10", review_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V11", worker_schema)
    monkeypatch.setattr(database_module, "SCHEMA_V12", delivery_schema)
    database.initialize()
    database.initialize()
    with database.connection() as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database_module.SCHEMA_VERSION
        )
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone()


def test_feedback_changes_keyword_then_reads_conditions_and_writes_note(
    material_env, response_factory
):
    async def run():
        env = material_env
        text = (
            "标题\n" + "背景" * 1500 + "\nThe model requires 8 GB GPU memory under batch size 1.\n"
        )
        env.seed("scratch/article", text)
        conversations = ConversationStore(env.calls)
        register_submission_fixture(env.registry)

        def change_word(request):
            name, result = observation(request)
            assert name == "search_content" and result.ok and not result.data["items"]
            return response_factory(
                ("english", "search_content", {"keyword": "8 GB", "dir": "scratch"})
            )

        def read_context(request):
            name, result = observation(request)
            assert name == "search_content" and result.data["items"]
            hit = result.data["items"][0]
            return response_factory(
                (
                    "read",
                    "read_file",
                    {"path": hit["path"], "offset": hit["excerpt_start_offset"], "limit": 200},
                )
            )

        def note(request):
            name, result = observation(request)
            assert (
                name == "read_file"
                and "8 GB" in result.data["content"]
                and "batch size 1" in result.data["content"]
            )
            return response_factory(
                (
                    "note",
                    "write_file",
                    {"path": "scratch/notes", "content": "要求8 GB显存，条件为batch size 1。"},
                )
            )

        def finish(request):
            name, result = observation(request)
            assert name == "write_file" and result.ok
            return submit(response_factory)

        model = ScriptedModel(
            [
                response_factory(("chinese", "search_content", {"keyword": "显存"})),
                change_word,
                read_context,
                note,
                finish,
            ]
        )
        done = await AgentRunner(model, env.executor, conversations, env.settings).run(
            env.run.id, env.run.user_id
        )
        assert done.status == "completed" and done.budget_usage.tool_calls == 5
        assert done.budget_usage.searches == 0 and done.budget_usage.model_requests == 5
        assert "batch size 1" in (env.root / "scratch/notes").read_text(encoding="utf-8")
        ModelRequest(
            messages=conversations.messages(env.run.id, env.run.user_id),
            tools=tuple(env.registry.schemas()),
        )

    asyncio.run(run())


def test_read_completion_version_rolls_back_with_feedback(material_env):
    async def run():
        env = material_env
        env.seed("scratch/a", "中文")
        call = ToolInvocation(
            tool_call_id="read", name="read_file", arguments='{"path":"scratch/a"}'
        )

        def fail(connection, record):
            raise RuntimeError("feedback unavailable")

        with pytest.raises(RuntimeError):
            await env.executor.execute(env.run.id, env.run.user_id, call, on_finished=fail)
        assert env.materials.read_hash(context(env), "scratch/a") is None

    asyncio.run(run())


def test_clipped_search_pages_do_not_lose_hits(material_env):
    async def run():
        env = material_env
        text = ("背景" * 85 + "AI ") * 8
        env.seed("scratch/a", text)
        tools = FileTools(
            env.materials, env.settings.model_copy(update={"max_tool_result_chars": 1000})
        )
        cursor, positions = None, []
        while True:
            result = await tools.search_content(
                context(env), SearchContentParameters(keyword="AI", cursor=cursor, limit=20)
            )
            assert len(result.model_dump_json()) <= 1000
            positions.extend(hit["start_offset"] for hit in result.data["items"])
            cursor = result.data["next_cursor"]
            if not cursor:
                break
        assert positions == [index for index in range(len(text)) if text.startswith("AI", index)]

    asyncio.run(run())


def test_write_detects_change_between_intent_and_replace(material_env, monkeypatch):
    env = material_env
    target = env.seed("scratch/a", "original")
    original = env.materials.files.publish

    def changed(path, content):
        original(path, content)
        target.write_bytes(b"concurrent change")

    monkeypatch.setattr(env.materials.files, "publish", changed)
    with pytest.raises(ToolFailure, match="FILE_WRITE_CONFLICT"):
        asyncio.run(
            FileTools(env.materials, env.settings).write_file(
                context(env), WriteParameters(path="scratch/a", content="desired")
            )
        )
    assert target.read_bytes() == b"concurrent change"


@pytest.mark.parametrize("field", ["content", "target_hash", "result"])
def test_semantically_corrupt_write_intents_cannot_replay(material_env, field):
    env = material_env
    ctx, tools = context(env), FileTools(env.materials, env.settings)
    parameters = WriteParameters(path="scratch/a", content="note")
    asyncio.run(tools.write_file(ctx, parameters))
    path = env.materials.job_path(ctx)
    job = json.loads(path.read_text(encoding="utf-8"))
    if field == "content":
        job["content"] = "modified content"
    if field == "target_hash":
        job["target_hash"] = "0" * 64
    if field == "result":
        job["result"]["data"]["char_count"] = 999
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ToolFailure, match="FILE_WRITE_STATE_PROBLEM"):
        asyncio.run(tools.write_file(ctx, parameters))
    assert (env.root / "scratch/a").read_text() == "note"
