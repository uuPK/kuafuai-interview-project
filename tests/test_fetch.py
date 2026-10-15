import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

import app.storage.database as database_module
from app.agent.protocol import ModelRequest, ToolMessage
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.providers.bocha import BochaClient
from app.providers.public_http import PublicPage
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.database import Database
from app.storage.models import RunRequest
from app.storage.runs import RunNotFoundError, RunStore
from app.storage.snapshots import _safe_path, digest
from app.storage.sources import SourceStore
from app.tools.context import bind_context
from app.tools.executor import ToolExecutor
from app.tools.fetch import FetchParameters, FetchTool, register_fetch
from app.tools.models import ToolFailure, ToolInvocation
from app.tools.registry import ToolDefinition, ToolRegistry
from app.tools.search import register_search
from app.tools.stubs import register_submission_fixture
from tests.test_article import article_html
from tests.test_bocha import item, settings, wire


class Pages:
    def __init__(self, env):
        self.env, self.seen = env, []
        self.body = article_html((env.run.window_end - timedelta(minutes=1)).isoformat())
        self.error = None

    async def get(self, url):
        self.seen.append(url)
        if self.error:
            raise self.error
        return PublicPage(url, self.body, ())


def fetch_call(candidate_id, call_id="fetch", **fields):
    return ToolInvocation(
        tool_call_id=call_id,
        name="fetch_url",
        arguments=json.dumps({"candidate_id": candidate_id, **fields}),
    )


def context(env, call_id="fetch"):
    return bind_context(env.runs, env.settings.data_dir, env.run.id, "user-1", tool_call_id=call_id)


async def install(env, pages, config=None):
    config = config or env.settings
    registry = ToolRegistry()
    candidates = CandidateStore(env.calls)
    async with BochaClient(
        settings(config.data_dir),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=wire(item(), item(url="https://other.com/new"))
            )
        ),
    ) as search:
        register_search(registry, search, candidates, config)
        store = SourceStore(candidates, config)
        register_fetch(registry, pages, store, config)
        executor = ToolExecutor(registry, env.calls, config)
        discovered = await executor.execute(
            env.run.id,
            "user-1",
            ToolInvocation(tool_call_id="search", name="web_search", arguments='{"query":"AI"}'),
        )
        ids = [entry["candidate_id"] for entry in discovered.data["items"]]
    return SimpleNamespace(store=store, executor=executor, ids=ids, pages=pages)


def test_fetch_persists_utf8_snapshot_index_metadata_and_replays(tool_env):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        call = fetch_call(setup.ids[0])
        result = await setup.executor.execute(env.run.id, "user-1", call)
        assert result.ok and result.data["usable_in_window"]
        meta, index = setup.store.get_source(env.run.id, "user-1", result.data["source_id"])
        body = setup.store.files.verify(context(env), meta, index)
        assert meta.char_count == len(body) and meta.content_hash == digest(body.encode("utf-8"))
        assert "\r" not in body and result.data["preview"] == body[:800]
        assert meta.publication.status == "confirmed" and meta.fetched_at
        assert meta.index_hash == digest(index.model_dump_json().encode("utf-8"))
        assert await setup.executor.execute(env.run.id, "user-1", call) == result
        assert len(setup.pages.seen) == 1
        reopened = Database(env.database.path)
        reopened.initialize()
        store = SourceStore(
            CandidateStore(CallStore(RunStore(reopened, env.settings))), env.settings
        )
        assert store.get_source(env.run.id, "user-1", meta.source_id) == (meta, index)
        saved = env.runs.get_run(env.run.id, "user-1")
        assert saved.budget_usage.fetches == 1 and saved.budget_usage.tool_calls == 2
        assert (
            len(
                [
                    event
                    for event in env.runs.list_events(env.run.id, "user-1")
                    if event.kind == "source_saved"
                ]
            )
            == 1
        )

    asyncio.run(run())


def test_new_call_keeps_old_version_and_reports_duplicate_hints(tool_env):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        first = await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        old_meta, old_index = setup.store.get_source(env.run.id, "user-1", first.data["source_id"])
        old_body = setup.store.files.verify(context(env), old_meta, old_index)
        second = await setup.executor.execute(
            env.run.id, "user-1", fetch_call(setup.ids[0], "same")
        )
        assert second.data["duplicates"] == {
            "same_url": [old_meta.source_id],
            "same_body": [old_meta.source_id],
        }
        setup.pages.body = article_html(content="<p>" + "更新后的正文 " * 100 + "</p>")
        third = await setup.executor.execute(
            env.run.id, "user-1", fetch_call(setup.ids[0], "changed")
        )
        assert third.data["source_id"] not in {first.data["source_id"], second.data["source_id"]}
        assert third.data["content_hash"] != first.data["content_hash"]
        assert len(third.data["duplicates"]["same_url"]) == 2
        assert third.data["duplicates"]["same_body"] == []
        assert setup.store.files.verify(context(env), old_meta, old_index) == old_body

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["missing", "other_user", "server_argument", "cross_run"])
def test_permission_and_schema_errors_block_fetch(tool_env, mode):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        candidate = setup.ids[0] if mode != "missing" else "not-found"
        run_id = env.run.id
        if mode == "cross_run":
            run_id = env.runs.create_run(
                RunRequest(
                    user_id="user-1", subscription_id="other", subscription=env.run.subscription
                )
            ).id
            from app.storage.models import StateChange

            env.runs.transition(run_id, "user-1", StateChange(status="running"))
        call = fetch_call(
            candidate, **({"url": "https://example.com/"} if mode == "server_argument" else {})
        )
        result = await setup.executor.execute(
            run_id, "other" if mode == "other_user" else "user-1", call
        )
        assert (
            result.error.code
            == {
                "missing": "CANDIDATE_NOT_FOUND",
                "other_user": "PERMISSION_DENIED",
                "server_argument": "INVALID_ARGUMENTS",
                "cross_run": "CANDIDATE_NOT_FOUND",
            }[mode]
        )
        assert setup.pages.seen == []

    asyncio.run(run())


def test_source_reads_are_run_and_user_owned(tool_env):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        result = await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        with pytest.raises(RunNotFoundError):
            setup.store.get_source(env.run.id, "other", result.data["source_id"])
        with pytest.raises(LookupError):
            setup.store.get_source(env.run.id, "user-1", "src-" + "0" * 32)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["missing", "ambiguous", "outside_window", "conflicting"])
def test_unconfirmed_publication_saves_material_and_explicit_feedback(tool_env, status):
    async def run():
        env = tool_env
        pages = Pages(env)
        valid = (env.run.window_end - timedelta(minutes=1)).isoformat()
        dates = {
            "missing": None,
            "ambiguous": "2026-10-13",
            "outside_window": "2000-01-01T00:00:00Z",
            "conflicting": valid,
        }
        extra = (
            '<meta itemprop="datePublished" content="2000-01-01T00:00:00Z">'
            if status == "conflicting"
            else ""
        )
        pages.body = article_html(dates[status], extra=extra)
        setup = await install(env, pages)
        result = await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        assert result.ok and not result.data["usable_in_window"] and result.data["issues"]
        assert result.data["publication_status"] == status
        assert (
            setup.store.get_source(env.run.id, "user-1", result.data["source_id"])[
                0
            ].publication.status
            == status
        )

    asyncio.run(run())


def test_navigation_output_limit_keeps_complete_index(tool_env):
    async def run():
        env = tool_env
        config = env.settings.model_copy(update={"max_tool_result_chars": 1700})
        pages = Pages(env)
        pages.body = article_html(
            content="".join(f"<h2>章节 {i}</h2><p>" + "正文证据 " * 50 + "</p>" for i in range(30))
        )
        setup = await install(env, pages, config)
        result = await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        assert result.ok and len(result.model_dump_json()) <= 1700
        meta, index = setup.store.get_source(env.run.id, "user-1", result.data["source_id"])
        assert result.data["omitted_sections"] > 0
        assert len(index.sections) == len(result.data["sections"]) + result.data["omitted_sections"]
        assert meta.char_count > 800

    asyncio.run(run())


def test_tiny_result_limit_leaves_no_partial_source(tool_env):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        config = env.settings.model_copy(update={"max_tool_result_chars": 256})
        with pytest.raises(ToolFailure) as error:
            await FetchTool(setup.pages, setup.store, config).execute(
                context(env), FetchParameters(candidate_id=setup.ids[0])
            )
        assert error.value.result.error.code == "RESULT_TOO_LARGE"
        assert setup.store.files.load_job(context(env)) is None

    asyncio.run(run())


def test_files_saved_before_completion_can_be_reused_after_reopen(tool_env, monkeypatch):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        call = fetch_call(setup.ids[0])

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(setup.executor, "finish", interrupted)
        with pytest.raises(KeyboardInterrupt):
            await setup.executor.execute(env.run.id, "user-1", call)
        job = setup.store.files.load_job(context(env))
        database = Database(env.database.path)
        database.initialize()
        calls = CallStore(RunStore(database, env.settings))
        store = SourceStore(CandidateStore(calls), env.settings)
        registry = ToolRegistry()
        register_fetch(registry, setup.pages, store, env.settings)
        result = await FetchTool(setup.pages, store, env.settings).execute(
            context(env), FetchParameters(candidate_id=setup.ids[0])
        )
        assert result == job.result and len(setup.pages.seen) == 1
        executor = ToolExecutor(registry, calls, env.settings)
        assert executor.finish(env.run.id, "user-1", call, result) == job.result
        assert store.get_source(env.run.id, "user-1", job.metadata.source_id) == (
            job.metadata,
            job.index,
        )

    asyncio.run(run())


def test_source_result_and_feedback_transaction_roll_back_together(
    agent_env, response_factory, monkeypatch
):
    async def run():
        env = agent_env
        setup = await install(env, Pages(env))
        call = fetch_call(setup.ids[0])
        env.conversations.save_response(
            env.run.id,
            "user-1",
            response_factory(("fetch", "fetch_url", {"candidate_id": setup.ids[0]})),
        )

        def broken(connection, saved):
            env.conversations.record_feedback(connection, "user-1", saved)
            raise RuntimeError("transaction interrupted")

        with pytest.raises(RuntimeError, match="interrupted"):
            await setup.executor.execute(env.run.id, "user-1", call, on_finished=broken)
        with env.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM run_messages WHERE role='tool'"
                ).fetchone()[0]
                == 0
            )
        assert env.calls.get_call(env.run.id, "user-1", "fetch").status == "executing"
        job = setup.store.files.load_job(context(env))

        def record(connection, saved):
            env.conversations.record_feedback(connection, "user-1", saved)

        setup.executor.finish(env.run.id, "user-1", call, job.result, on_finished=record)
        assert setup.store.get_source(env.run.id, "user-1", job.metadata.source_id)
        assert isinstance(env.conversations.messages(env.run.id, "user-1")[-1], ToolMessage)
        assert len(setup.pages.seen) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", ["missing_body", "body_hash", "index_hash", "manifest", "candidate", "index_range"]
)
def test_damaged_material_is_explicit_error_without_refetch(tool_env, damage):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        ctx = context(env)
        job = setup.store.files.load_job(ctx)
        body = ctx.working_dir / job.metadata.file_path
        index = ctx.working_dir / job.metadata.index_path
        manifest = setup.store.files.job_path(ctx)
        if damage == "missing_body":
            body.unlink()
        elif damage == "body_hash":
            body.write_bytes(b"tampered")
        elif damage == "index_hash":
            index.write_bytes(b"{}")
        elif damage == "manifest":
            manifest.write_bytes(b"invalid")
        elif damage == "candidate":
            job = job.model_copy(
                update={"metadata": job.metadata.model_copy(update={"candidate_id": setup.ids[1]})}
            )
            manifest.write_text(job.model_dump_json(), encoding="utf-8")
        else:
            bad = job.index.model_copy(
                update={
                    "blocks": (
                        job.index.blocks[0].model_copy(
                            update={"end_offset": job.metadata.char_count + 1}
                        ),
                    )
                }
            )
            raw = bad.model_dump_json().encode("utf-8")
            job = job.model_copy(
                update={
                    "index": bad,
                    "metadata": job.metadata.model_copy(update={"index_hash": digest(raw)}),
                }
            )
            index.write_bytes(raw)
            manifest.write_text(job.model_dump_json(), encoding="utf-8")
        with pytest.raises(ToolFailure) as error:
            await FetchTool(setup.pages, setup.store, env.settings).execute(
                ctx, FetchParameters(candidate_id=setup.ids[0])
            )
        assert error.value.result.error.code == "SOURCE_STATE_PROBLEM"
        assert len(setup.pages.seen) == 1

    asyncio.run(run())


def test_atomic_publication_reuses_identical_and_rejects_overwrite(tool_env, monkeypatch):
    files = SourceStore(CandidateStore(tool_env.calls), tool_env.settings).files
    target = tool_env.settings.data_dir / "fixed.txt"
    files.publish(target, b"original")
    files.publish(target, b"original")
    with pytest.raises(ToolFailure):
        files.publish(target, b"changed")
    assert target.read_bytes() == b"original"
    assert list(target.parent.glob(".pending-*")) == []
    with pytest.raises(ToolFailure) as error:
        files.publish(target.parent / "large", b"a" * (files.max_bytes * 8 + 100_001))
    assert error.value.result.error.code == "FETCH_EXTRACTED_TOO_LARGE"


def test_partial_publication_is_not_silently_refetched(tool_env, monkeypatch):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        publish = setup.store.files.publish

        def interrupt(path, content):
            if path.name == "index.json":
                raise KeyboardInterrupt
            return publish(path, content)

        monkeypatch.setattr(setup.store.files, "publish", interrupt)
        with pytest.raises(KeyboardInterrupt):
            await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        with pytest.raises(ToolFailure) as error:
            setup.store.files.load_job(context(env))
        assert error.value.result.error.code == "SOURCE_STATE_PROBLEM"
        assert len(setup.pages.seen) == 1

    asyncio.run(run())


def test_manifest_ownership_and_path_boundaries(tool_env):
    env = tool_env
    files = SourceStore(CandidateStore(env.calls), env.settings).files
    with pytest.raises(PermissionError):
        files.job_path(replace(context(env), tool_call_id=None))
    with pytest.raises(PermissionError):
        files.job_path(replace(context(env), run_id="../../escape"))
    with pytest.raises(PermissionError):
        _safe_path(env.settings.data_dir, "../escape")


def test_forbidden_feedback_changes_candidate_and_pairs_messages(agent_env, response_factory):
    async def run():
        env = agent_env
        pages = Pages(env)
        setup = await install(env, pages)
        pages.error = ToolFailure("FETCH_FORBIDDEN", "网页返回 HTTP 403，可选择其他来源。")

        def switch(request):
            feedback = json.loads(request.messages[-1].content)
            assert feedback["error"]["code"] == "FETCH_FORBIDDEN"
            pages.error = None
            return response_factory(("other", "fetch_url", {"candidate_id": setup.ids[1]}))

        def submit(request):
            assert json.loads(request.messages[-1].content)["data"]["source_id"].startswith("src-")
            return response_factory(
                ("submit", "submit_brief", {"draft": {"title": "抓取协议测试"}})
            )

        register_submission_fixture(setup.executor.registry)
        model = ScriptedModel(
            [
                response_factory(("first", "fetch_url", {"candidate_id": setup.ids[0]})),
                switch,
                submit,
            ]
        )
        completed = await AgentRunner(model, setup.executor, env.conversations, env.settings).run(
            env.run.id, "user-1"
        )
        assert completed.status == "completed" and completed.budget_usage.fetches == 2
        ModelRequest(
            messages=env.conversations.messages(env.run.id, "user-1"),
            tools=tuple(setup.executor.registry.schemas()),
        )
        assert pages.seen == ["https://news.example.com/new", "https://other.com/new"]

    asyncio.run(run())


def test_v6_migration_preserves_candidates_and_rolls_back(tool_env, tmp_path, monkeypatch):
    old = Database(tmp_path / "v5.sqlite3")
    statements = database_module.SCHEMA_V6
    monkeypatch.setattr(database_module, "SCHEMA_V7", ())
    monkeypatch.setattr(database_module, "SCHEMA_V8", ())
    monkeypatch.setattr(database_module, "SCHEMA_V11", ())
    monkeypatch.setattr(database_module, "SCHEMA_V12", ())
    monkeypatch.setattr(database_module, "SCHEMA_V13", ())
    monkeypatch.setattr(database_module, "SCHEMA_V14", ())
    monkeypatch.setattr(database_module, "SCHEMA_V15", ())
    monkeypatch.setattr(database_module, "SCHEMA_V10", ())
    monkeypatch.setattr(database_module, "SCHEMA_V9", ())
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 5)
    monkeypatch.setattr(database_module, "SCHEMA_V6", ())
    old.initialize()
    env = tool_env
    env.database = old
    env.runs = RunStore(old, env.settings)
    env.run = env.runs.create_run(
        RunRequest(user_id="user-1", subscription_id="sub", subscription=env.run.subscription)
    )
    from app.storage.models import StateChange

    env.run = env.runs.transition(env.run.id, "user-1", StateChange(status="running"))
    env.calls = CallStore(env.runs)
    setup = asyncio.run(install(env, Pages(env)))
    candidate = setup.store.candidates.get_candidate(env.run.id, "user-1", setup.ids[0])
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 6)
    monkeypatch.setattr(database_module, "SCHEMA_V6", (*statements, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        old.initialize()
    with old.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='sources'"
        ).fetchone()
    monkeypatch.setattr(database_module, "SCHEMA_V6", statements)
    old.initialize()
    old.initialize()
    assert (
        setup.store.candidates.get_candidate(env.run.id, "user-1", candidate.candidate_id)
        == candidate
    )
    with old.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_result_hook_is_server_only_and_synchronous():
    async def callback(*args):
        pass

    with pytest.raises(ValueError):
        ToolDefinition("hook", "description", FetchParameters, callback, on_completed=callback)
    definition = ToolDefinition(
        "hook", "description", FetchParameters, callback, on_completed=lambda *args: None
    )
    assert "on_completed" not in json.dumps(definition.schema())


@pytest.mark.parametrize("different", [False, True])
def test_atomic_file_publication_handles_competing_writer(tool_env, monkeypatch, different):
    files = SourceStore(CandidateStore(tool_env.calls), tool_env.settings).files
    target = tool_env.settings.data_dir / "race.txt"

    def race(temporary, destination):
        destination.write_bytes(b"different" if different else b"original")
        raise FileExistsError

    monkeypatch.setattr(os, "link", race)
    if different:
        with pytest.raises(ToolFailure, match="SOURCE_STATE_PROBLEM"):
            files.publish(target, b"original")
    else:
        files.publish(target, b"original")
    assert list(target.parent.glob(".pending-*")) == []


@pytest.mark.parametrize("damage", ["utf8", "crlf", "filepath", "run", "call", "oversize"])
def test_snapshot_verification_rejects_encoding_ownership_and_size(tool_env, damage):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        ctx = context(env)
        job = setup.store.files.load_job(ctx)
        metadata = job.metadata
        manifest = setup.store.files.job_path(ctx)
        if damage in {"utf8", "crlf"}:
            body = b"\xff" if damage == "utf8" else b"text\r\n"
            (ctx.working_dir / metadata.file_path).write_bytes(body)
            metadata = metadata.model_copy(
                update={"content_hash": digest(body), "char_count": len(body)}
            )
        elif damage == "filepath":
            metadata = metadata.model_copy(update={"file_path": "scratch/forged.txt"})
        elif damage == "run":
            metadata = metadata.model_copy(update={"run_id": "wrong"})
            with pytest.raises(PermissionError):
                setup.store.files.verify(ctx, metadata, job.index)
        elif damage == "call":
            job = job.model_copy(update={"tool_call_id": "different"})
        else:
            setup.store.files.max_bytes = 1
            (ctx.working_dir / metadata.file_path).write_bytes(b"x" * 100009)
        manifest.write_text(
            job.model_copy(update={"metadata": metadata}).model_dump_json(), encoding="utf-8"
        )
        with pytest.raises(ToolFailure, match="SOURCE_STATE_PROBLEM"):
            setup.store.files.load_job(ctx)

    asyncio.run(run())


def test_snapshot_paths_reject_symlinks_and_absolute_escape(tool_env, monkeypatch):
    from pathlib import Path

    root = tool_env.settings.data_dir
    with pytest.raises(PermissionError):
        _safe_path(root, "../../../escape")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "linked" or original(path))
    with pytest.raises(PermissionError):
        _safe_path(root, "linked/body.txt")
    monkeypatch.setattr(
        Path, "resolve", lambda path, *a, **k: root.parent if path == root else path
    )
    with pytest.raises(PermissionError):
        _safe_path(root, "body.txt")


@pytest.mark.parametrize("damage", ["metadata", "index"])
def test_invalid_database_material_reports_state_problem(tool_env, damage):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        result = await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        with env.database.transaction() as connection:
            column = "metadata_json" if damage == "metadata" else "index_json"
            connection.execute(
                f"UPDATE sources SET {column}='invalid' WHERE id=?", (result.data["source_id"],)
            )
        with pytest.raises(ToolFailure, match="SOURCE_STATE_PROBLEM"):
            setup.store.get_source(env.run.id, "user-1", result.data["source_id"])

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["manifest", "result", "candidate", "existing"])
def test_registration_guard_rolls_back_on_inconsistent_material(tool_env, damage):
    async def run():
        env = tool_env
        setup = await install(env, Pages(env))
        await setup.executor.execute(env.run.id, "user-1", fetch_call(setup.ids[0]))
        job = setup.store.files.load_job(context(env))
        call = env.calls.get_call(env.run.id, "user-1", "fetch")
        if damage == "manifest":
            setup.store.files.job_path(context(env)).unlink()
        elif damage == "result":
            call = call.model_copy(
                update={"result": call.result.model_copy(update={"data": {"different": True}})}
            )
        elif damage == "candidate":
            job = job.model_copy(
                update={
                    "metadata": job.metadata.model_copy(update={"candidate_id": "not-existing"})
                }
            )
            setup.store.files.job_path(context(env)).write_text(
                job.model_dump_json(), encoding="utf-8"
            )
        else:
            with env.database.transaction() as connection:
                connection.execute(
                    "UPDATE sources SET metadata_json='{}' WHERE id=?", (job.metadata.source_id,)
                )
        expected = PermissionError if damage == "candidate" else ToolFailure
        with pytest.raises(expected), env.database.transaction() as connection:
            setup.store.on_completed(connection, call)
        with env.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 1

    asyncio.run(run())
