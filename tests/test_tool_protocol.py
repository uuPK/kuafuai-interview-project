from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.tools.context import bind_context
from app.tools.models import ToolInvocation, ToolParameters, ToolResult
from app.tools.registry import ToolDefinition, ToolRegistry
from app.tools.stubs import SearchParameters, search_fixture


def test_uniform_results_and_invalid_envelopes() -> None:
    assert ToolResult.success({"items": []}).model_dump() == {
        "ok": True,
        "data": {"items": []},
        "error": None,
    }
    failed = ToolResult.failure("NO_RESULT", "没有结果。")
    assert failed.error.code == "NO_RESULT"
    assert failed.error.retryable is False
    for values in (
        {"ok": False},
        {"ok": True, "error": failed.error},
        {"ok": False, "error": failed.error, "data": "unexpected"},
    ):
        with pytest.raises(ValidationError):
            ToolResult(**values)


def test_invocation_validation_and_fingerprint() -> None:
    first = ToolInvocation(tool_call_id="id-1", name="web_search", arguments='{"query":"AI"}')
    second = first.model_copy(update={"tool_call_id": "id-2"})
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != first.model_copy(update={"arguments": "{}"}).fingerprint
    assert "AI" not in repr(first)
    with pytest.raises(ValidationError):
        ToolInvocation(tool_call_id="", name="web_search", arguments="{}")
    with pytest.raises(ValidationError):
        ToolInvocation(tool_call_id="id", name="invalid;name", arguments="{}")


def test_schema_contains_argument_constraints_and_no_server_context(tool_env) -> None:
    schemas = tool_env.registry.schemas()
    assert [item["function"]["name"] for item in schemas] == ["web_search", "fetch_url"]
    parameters = schemas[0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["query"]
    assert parameters["properties"]["limit"]["maximum"] == 10
    assert "user_id" not in parameters["properties"]
    assert "reader@example.com" not in str(schemas)
    with pytest.raises(ValueError, match="already"):
        tool_env.registry.register(tool_env.registry.get("web_search"))


@pytest.mark.parametrize(
    "parameter_kind", ["reserved", "alias", "hidden_reserved", "extra", "lax", "base"]
)
def test_registration_rejects_unsafe_parameter_models(parameter_kind: str) -> None:
    class Reserved(ToolParameters):
        user_id: str

    class Alias(ToolParameters):
        query: str = Field(alias="recipient")

    class HiddenReserved(ToolParameters):
        user_id: str = Field(alias="query")

    class Extra(ToolParameters):
        model_config = ConfigDict(extra="allow")

    class Lax(ToolParameters):
        model_config = ConfigDict(strict=False)

    parameters = {
        "reserved": Reserved,
        "alias": Alias,
        "hidden_reserved": HiddenReserved,
        "extra": Extra,
        "lax": Lax,
        "base": BaseModel,
    }
    with pytest.raises(ValueError):
        ToolDefinition("sample", "测试工具", parameters[parameter_kind], search_fixture)


def test_registration_rejects_sync_handler_and_bad_metadata() -> None:
    with pytest.raises(ValueError, match="asynchronous"):
        ToolDefinition("sample", "测试工具", SearchParameters, lambda *args: ToolResult.success())
    with pytest.raises(ValidationError):
        ToolDefinition("bad name", "测试工具", SearchParameters, search_fixture)
    assert ToolRegistry().schemas() == []


@pytest.mark.parametrize("async_resolver", [True, False])
def test_completion_resolver_is_trusted_synchronous_metadata(async_resolver) -> None:
    async def resolver(result):
        return "completed"

    with pytest.raises(ValueError, match="synchronous"):
        ToolDefinition(
            "sample",
            "测试工具",
            SearchParameters,
            search_fixture,
            completion_status=resolver if async_resolver else "completed",
        )
    definition = ToolDefinition(
        "sample",
        "测试工具",
        SearchParameters,
        search_fixture,
        completion_status=lambda result: "no_news",
    )
    assert "completion_status" not in str(definition.schema())


def test_context_is_server_bound_and_resolves_task_paths(tool_env) -> None:
    context = bind_context(tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1")
    assert context.user_id == "user-1"
    assert context.recipient == "reader@example.com"
    assert "reader@example.com" not in repr(context)
    assert context.window_end - context.window_start == timedelta(hours=24)
    assert context.resolve_path(".") == context.working_dir
    assert (
        context.resolve_path("sources/article.txt") == context.working_dir / "sources/article.txt"
    )
    assert (
        context.resolve_path("scratch/note.txt", write=True)
        == context.working_dir / "scratch/note.txt"
    )
    assert (
        context.resolve_path("scratch\\note.txt", write=True)
        == context.working_dir / "scratch/note.txt"
    )
    assert not context.working_dir.exists()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../other-run/article.txt",
        "..\\other-run\\article.txt",
        "/etc/passwd",
        "C:\\Windows\\file.txt",
        "C:relative.txt",
        "\\\\host\\share",
        "note\x00.txt",
    ],
)
def test_context_rejects_path_escape(tool_env, path: str) -> None:
    context = bind_context(tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1")
    with pytest.raises(PermissionError):
        context.resolve_path(path)


@pytest.mark.parametrize(
    "path", ["note.txt", "sources/article.txt", "scratch", "scratch/../sources/file"]
)
def test_writes_are_limited_to_scratch_files(tool_env, path: str) -> None:
    context = bind_context(tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1")
    with pytest.raises(PermissionError):
        context.resolve_path(path, write=True)


def test_symlink_escape_is_rejected(tool_env, tmp_path: Path) -> None:
    context = bind_context(tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1")
    context.working_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = context.working_dir / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks in this test environment")
    with pytest.raises(PermissionError):
        context.resolve_path("escape/file.txt")


def test_binding_rejects_resolved_directory_escape(tool_env, monkeypatch) -> None:
    original = Path.resolve
    target = tool_env.settings.data_dir / "runs" / tool_env.run.id

    def resolve(path, *args, **kwargs):
        if path == target:
            return tool_env.settings.data_dir.parent / "outside"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(PermissionError):
        bind_context(tool_env.runs, tool_env.settings.data_dir, tool_env.run.id, "user-1")
