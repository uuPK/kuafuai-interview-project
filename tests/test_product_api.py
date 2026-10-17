"""Product HTTP tests; accounts/news/models are explicit fixtures."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.agent.briefs import BriefDocument, ReviewOutcome
from app.agent.runner import AgentRunner
from app.agent.stubs import ScriptedModel
from app.config import Settings
from app.providers.bocha import BochaClient
from app.runtime import open_runtime
from app.storage.models import RunRequest, StateChange, SubscriptionSnapshot, timestamp
from app.tools.executor import ToolExecutor
from app.tools.models import ToolInvocation, ToolResult
from app.tools.registry import ToolRegistry
from app.tools.search import register_search
from app.tools.submit import register_submission
from app.worker.service import Worker
from tests.test_auth import PASSWORD, client_for, login, protection, register
from tests.test_bocha import settings as bocha_settings
from tests.test_bocha import wire
from tests.test_harness import Clock

PREFERENCES = {"topics": ["开源模型"], "keywords": ["Atlas"], "exclude_keywords": ["广告"]}


@pytest.fixture
def client(tmp_path):
    config = Settings(
        _env_file=None, data_dir=tmp_path, session_secret="fixture-session-secret-" * 3
    )
    with client_for(config) as value:
        value.app.state.run_store.clock = Clock().now
        assert register(value).status_code == 201
        yield value


def put(client, path, value):
    return client.put(path, json=value, headers=protection(client))


def subscribe(client, **changes):
    return put(client, "/subscription", {**PREFERENCES, **changes})


def preview(client):
    return client.post("/runs", json={}, headers=protection(client))


def finish(client, run_id):
    runs = client.app.state.run_store
    owner = client.get("/me").json()["user"]["id"]
    runs.transition(run_id, owner, StateChange(status="running"))
    runs.transition(run_id, owner, StateChange(status="failed", stop_reason="fixture_finished"))


def seed_brief(client, *, title="[fixture] 暂无新闻", mode="preview"):
    """Direct fixture record for API reading, without claiming model acceptance."""
    state = client.app.state
    account = client.get("/me").json()["user"]
    saved = subscribe(client).json()["subscription"]
    snapshot = SubscriptionSnapshot(
        nickname=account["nickname"], email=account["email"], topics=("开源模型",)
    )
    run = state.run_store.create_run(
        RunRequest(
            user_id=account["id"],
            subscription_id=saved["id"],
            subscription=snapshot,
            mode=mode,
            scheduled_for=state.run_store.now() if mode == "scheduled" else None,
        )
    )
    run = state.run_store.transition(run.id, account["id"], StateChange(status="running"))
    brief_id = "brief-" + uuid4().hex
    document = BriefDocument(title=title, items=(), shortfall_reason="[fixture] 无新闻")
    with state.database.transaction() as connection:
        state.run_store._transition(
            connection, run, StateChange(status="no_news"), state.run_store.now()
        )
        connection.execute(
            "INSERT INTO briefs VALUES(?,?,?,?,?)",
            (
                brief_id,
                run.id,
                timestamp(state.run_store.now()),
                "fixture",
                document.model_dump_json(),
            ),
        )
    return brief_id, run.id


def test_profile_subscription_defaults_and_login_email_change(client):
    account = client.get("/me").json()["user"]
    assert account["email"] == "reader@example.com" and "password_hash" not in str(account)
    assert client.get("/subscription").json() == {"subscription": None}
    changed = put(client, "/me", {"email": "NEW@example.com", "nickname": " 新读者 "})
    assert changed.status_code == 200
    assert changed.json()["user"]["id"] == account["id"]
    assert changed.json()["user"]["email"] == "new@example.com"
    saved = subscribe(client).json()["subscription"]
    assert saved["push_time"] == "09:00" and saved["max_items"] == 5 and saved["enabled"]
    updated = subscribe(client, topics=["AI芯片"], max_items=2, push_time="23:59").json()[
        "subscription"
    ]
    assert updated["id"] == saved["id"] and updated["created_at"] == saved["created_at"]
    assert client.get("/subscription").json()["subscription"] == updated
    client.post("/auth/logout", json={}, headers=protection(client))
    assert login(client, email="reader@example.com").status_code == 401
    assert login(client, email="new@example.com").status_code == 200


def test_preview_requires_enabled_saved_subscription_and_returns_before_execution(
    client, monkeypatch
):
    assert preview(client).json()["error"]["code"] == "SUBSCRIPTION_REQUIRED"
    assert subscribe(client, enabled=False).status_code == 200
    assert preview(client).json()["error"]["code"] == "SUBSCRIPTION_DISABLED"
    subscribe(client)

    async def forbidden(*args, **kwargs):
        raise AssertionError("HTTP preview must not execute research")

    monkeypatch.setattr(AgentRunner, "run", forbidden)
    response = preview(client)
    assert response.status_code == 202 and response.json()["reused"] is False
    run_id = response.json()["run_id"]
    assert preview(client).json() == {"run_id": run_id, "reused": True}
    record = client.get("/runs/" + run_id).json()
    assert record["status"] == "queued" and record["mode"] == "preview"
    assert record["started_at"] is None and record["budget_usage"]["model_requests"] == 0
    assert record["window_end"] == record["created_at"] and record["brief_id"] is None
    assert "user_id" not in record and "execution_token" not in response.text
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_responses").fetchone()[0] == 0
    finish(client, run_id)
    assert preview(client).json()["run_id"] != run_id


def test_existing_snapshot_survives_profile_and_preference_changes_and_pause(client):
    subscribe(client)
    run_id = preview(client).json()["run_id"]
    original = client.get("/runs/" + run_id).json()
    put(client, "/me", {"email": "new@example.com", "nickname": "新名字"})
    subscribe(client, topics=["AI芯片"], max_items=1, enabled=False, push_time="10:30")
    assert client.get("/runs/" + run_id).json() == original
    assert preview(client).status_code == 409
    finish(client, run_id)
    subscribe(client, topics=["AI芯片"], max_items=1)
    new_id = preview(client).json()["run_id"]
    snapshot = client.get("/runs/" + new_id).json()["subscription"]
    assert snapshot["email"] == "new@example.com" and snapshot["nickname"] == "新名字"
    assert snapshot["topics"] == ["AI芯片"] and snapshot["max_items"] == 1


def test_stored_snapshot_lists_are_redacted_before_public_response(client):
    subscribe(client)
    run_id = preview(client).json()["run_id"]
    state = client.app.state
    secret = state.settings.session_secret.get_secret_value()
    owner = client.get("/me").json()["user"]["id"]
    # Explicit legacy/corrupt fixture: bypass API validation to audit safe reads.
    snapshot = state.run_store.get_run(run_id, owner).subscription.model_dump(mode="json")
    snapshot.update(topics=[secret], keywords=[secret], exclude_keywords=[secret])
    with state.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET subscription_json=? WHERE id=?", (json.dumps(snapshot), run_id)
        )
    response = client.get("/runs/" + run_id)
    assert response.status_code == 200 and secret not in response.text
    public = response.json()["subscription"]
    assert public["topics"] == public["keywords"] == public["exclude_keywords"] == ["[REDACTED]"]


@pytest.mark.parametrize(
    "changes",
    [
        {"topics": []},
        {"topics": [" "]},
        {"topics": ["AI", "ai"]},
        {"topics": ["x\nline"]},
        {"topics": ["x"] * 21},
        {"topics": ["x" * 121]},
        {"keywords": "AI"},
        {"keywords": ["x"] * 101},
        {"exclude_keywords": ["AD", "ad"]},
        {"max_items": 0},
        {"max_items": 6},
        {"max_items": True},
        {"max_items": "3"},
        {"enabled": "false"},
        {"push_time": "9:00"},
        {"push_time": "24:00"},
        {"push_time": "09:60"},
        {"user_id": "foreign"},
        {"recipient": "other@example.com"},
    ],
)
def test_subscription_validation_does_not_mutate_or_echo_input(client, changes):
    before = subscribe(client).json()
    response = subscribe(client, **changes)
    assert response.status_code == 422 and '"input":' not in response.text
    assert client.get("/subscription").json() == before


@pytest.mark.parametrize(
    "path,form",
    [
        ("/me", {"email": "bad\r\nBcc:a@example.com", "nickname": "reader"}),
        ("/me", {"email": "a@example.com", "nickname": "x\x7f"}),
        ("/me", {"email": "a@example.com", "nickname": "reader", "role": "admin"}),
        ("/subscription", {"topics": ["sk-fixture12345678"]}),
        ("/subscription", {"topics": ["fixture-session-secret-" * 3]}),
    ],
)
def test_profile_and_sensitive_preference_validation(client, path, form):
    response = put(client, path, form)
    assert (
        response.status_code == 422 and form.get("topics", ["not-present"])[0] not in response.text
    )
    assert PASSWORD not in response.text


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/me", "put", {"email": "new@example.com", "nickname": "reader"}),
        ("/subscription", "put", PREFERENCES),
        ("/runs", "post", {}),
    ],
)
def test_writes_require_session_bound_csrf(client, path, method, body):
    assert getattr(client, method)(path, json=body).status_code == 403
    headers = {**protection(client), "Origin": "https://evil.example"}
    assert getattr(client, method)(path, json=body, headers=headers).status_code == 403


def test_product_json_boundary_and_preview_server_owned_parameters(client):
    headers = protection(client)
    for content, content_type, status in [
        ('{"topics":[],"topics":["AI"]}', "application/json", 422),
        ('{"topics":[NaN]}', "application/json", 422),
        ("{", "application/json", 422),
        ("x" * 131073, "application/json", 413),
        ("topics=AI", "text/plain", 415),
    ]:
        response = client.put(
            "/subscription", content=content, headers={**headers, "Content-Type": content_type}
        )
        assert response.status_code == status and response.headers["cache-control"] == "no-store"
    subscribe(client)
    for forbidden in (
        "user_id",
        "subscription_id",
        "mode",
        "scheduled_for",
        "recipient",
        "budget_limits",
    ):
        response = client.post("/runs", json={forbidden: "fixture"}, headers=headers)
        assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/runs?limit=0",
        "/runs?limit=51",
        "/runs?limit=true",
        "/runs?limit=1&limit=2",
        "/runs?user_id=other",
        "/runs?cursor=bad",
        "/runs?cursor=",
        "/briefs?limit=100",
        "/me?user_id=other",
        "/subscription?user_id=other",
        "/runs/missing?user_id=other",
        "/runs/missing/events?limit=101",
        "/runs/missing/events?after_id=-1",
        "/runs/missing/events?after_id=9223372036854775808",
    ],
)
def test_query_boundaries(client, path):
    assert client.get(path).status_code == 422


def test_owner_checked_lists_details_cursors_and_duplicate_email(client):
    first_id, run_id = seed_brief(client)
    seed_brief(client, title="[fixture] 第二份")
    run_page = client.get("/runs?limit=1").json()
    brief_page = client.get("/briefs?limit=1").json()
    assert len(brief_page["items"]) == 1 and brief_page["next_cursor"]
    assert "document" not in brief_page["items"][0]
    document = client.get("/briefs/" + first_id).json()
    assert document["review_basis"] == "fixture" and document["status"] == "no_news"
    with client_for(client.app.state.settings) as other:
        assert register(other, email="second@example.com").status_code == 201
        for collection in ("runs", "briefs"):
            assert other.get("/" + collection).json() == {"items": [], "next_cursor": None}
        for resource in ("/runs/" + run_id, "/runs/" + run_id + "/events", "/briefs/" + first_id):
            assert other.get(resource).status_code == 404
            assert other.get(resource).json() == {"error": {"code": "NOT_FOUND"}}
        for collection, cursor in (
            ("runs", run_page["next_cursor"]),
            ("briefs", brief_page["next_cursor"]),
        ):
            assert other.get("/" + collection, params={"cursor": cursor}).status_code == 422
        before = other.get("/me").json()
        assert (
            put(other, "/me", {"email": "READER@example.com", "nickname": "duplicate"}).status_code
            == 409
        )
        assert other.get("/me").json() == before
    assert client.get("/briefs/missing").json() == {"error": {"code": "NOT_FOUND"}}
    cursor = run_page["next_cursor"]
    assert client.get("/briefs", params={"cursor": cursor}).status_code == 422
    assert client.get("/runs", params={"cursor": cursor[:-1] + "!"}).status_code == 422


def test_keyset_pagination_and_safe_events(client):
    subscribe(client)
    ids = []
    for _ in range(3):
        run_id = preview(client).json()["run_id"]
        ids.append(run_id)
        finish(client, run_id)
    page = client.get("/runs?limit=1").json()
    seen = [page["items"][0]["id"]]
    while page["next_cursor"]:
        page = client.get("/runs", params={"limit": 1, "cursor": page["next_cursor"]}).json()
        seen.extend(item["id"] for item in page["items"])
    assert len(seen) == 3 and set(seen) == set(ids)
    path = "/runs/" + ids[0] + "/events"
    first = client.get(path, params={"limit": 1}).json()
    assert first["has_more"] and first["events"][0]["summary"] == "任务已创建"
    rest = client.get(path, params={"after_id": first["next_after_id"]}).json()
    assert not rest["has_more"] and rest["stop_reason"] == "fixture_finished"
    assert all(event["id"] > first["next_after_id"] for event in rest["events"])
    assert "reasoning_content" not in json.dumps(rest)
    assert client.get(path).headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("PUBLIC_ADDRESS_BLOCKED", "PUBLIC_ADDRESS_BLOCKED"),
        ("PUBLIC_DNS_TIMEOUT", "PUBLIC_DNS_TIMEOUT"),
        ("UNKNOWN_SOURCE_ERROR", None),
    ],
)
def test_failed_run_projects_only_allowlisted_source_error(client, code, expected):
    subscribe(client)
    run_id = preview(client).json()["run_id"]
    state = client.app.state
    owner = client.get("/me").json()["user"]["id"]
    call = ToolInvocation(tool_call_id="blocked", name="fetch_url", arguments="{}")
    state.call_store.prepare_calls(run_id, owner, [call])
    result = ToolResult.failure(code, "sensitive private diagnostic")
    with state.database.transaction() as connection:
        connection.execute(
            "UPDATE tool_calls SET status='completed', finished_at=?, result_json=? WHERE run_id=?",
            (timestamp(state.run_store.now()), result.model_dump_json(), run_id),
        )
    # A running task's transient failures must not become a final failure diagnosis.
    assert client.get("/runs/" + run_id).json()["source_error_code"] is None
    finish(client, run_id)
    response = client.get("/runs/" + run_id)
    assert response.json()["source_error_code"] == expected
    assert "sensitive private diagnostic" not in response.text
    # Existing source evidence takes precedence over an unrelated blocked candidate.
    with state.database.transaction() as connection:
        connection.execute(
            "INSERT INTO sources VALUES(?,?,?,?,?,?)",
            ("fixture-source", run_id, "https://example.com", "fixture-hash", "{}", "{}"),
        )
    assert client.get("/runs/" + run_id).json()["source_error_code"] is None


def test_brief_pagination_empty_last_page_and_external_text_remains_json(client):
    first_id, _ = seed_brief(client, title="<script>fixture external title</script>")
    second_id, _ = seed_brief(client)
    page = client.get("/briefs?limit=1").json()
    next_page = client.get("/briefs", params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert next_page["next_cursor"] is None
    assert {page["items"][0]["brief_id"], next_page["items"][0]["brief_id"]} == {
        first_id,
        second_id,
    }
    response = client.get("/briefs/" + first_id)
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    # This API returns text; page rendering/escaping is covered in step 4.3.
    assert response.json()["document"]["title"] == "<script>fixture external title</script>"
    owner = client.get("/me").json()["user"]["id"]
    cursor = client.app.state.catalog_store.cursors.encode(
        owner, "briefs", "2000-01-01T00:00:00.000000+00:00", "brief-" + "0" * 32
    )
    assert client.get("/briefs", params={"cursor": cursor}).json() == {
        "items": [],
        "next_cursor": None,
    }


def test_anonymous_writes_cannot_use_bootstrap_csrf(client):
    with client_for(client.app.state.settings) as anonymous:
        headers = protection(anonymous)
        assert anonymous.put("/subscription", json=PREFERENCES, headers=headers).status_code == 401
        assert (
            anonymous.put(
                "/me", json={"email": "x@example.com", "nickname": "x"}, headers=headers
            ).status_code
            == 401
        )
        assert anonymous.post("/runs", json={}, headers=headers).status_code == 401


def test_anonymous_reads_are_rejected_and_restart_retains_subscription(client):
    saved = subscribe(client).json()
    for path in (
        "/me",
        "/subscription",
        "/runs",
        "/runs/missing",
        "/runs/missing/events",
        "/briefs",
        "/briefs/missing",
    ):
        with client_for(client.app.state.settings) as anonymous:
            assert anonymous.get(path).status_code == 401
    with client_for(client.app.state.settings) as reopened:
        assert login(reopened).status_code == 200
        assert reopened.get("/subscription").json() == saved


def test_corrupt_subscription_run_brief_and_events_fail_explicitly(client):
    brief_id, run_id = seed_brief(client)
    database = client.app.state.database
    with database.transaction() as connection:
        connection.execute("UPDATE subscriptions SET preferences_json='broken'")
    assert client.get("/subscription").json()["error"]["code"] == "SUBSCRIPTION_STATE_PROBLEM"
    with database.transaction() as connection:
        connection.execute("UPDATE briefs SET document_json='broken'")
    assert client.get("/briefs/" + brief_id).json()["error"]["code"] == "BRIEF_STATE_PROBLEM"
    with database.transaction() as connection:
        connection.execute("UPDATE runs SET budget_usage_json='broken'")
    assert client.get("/runs/" + run_id).json()["error"]["code"] == "RUN_STATE_PROBLEM"
    assert client.get("/runs").status_code == 409
    with database.connection() as connection:
        connection.execute("UPDATE run_events SET data_json='broken'")
    assert client.get("/runs/" + run_id + "/events").status_code == 409


def test_api_preview_cold_worker_submission_and_history(client, response_factory):
    subscribe(client)
    run_id = preview(client).json()["run_id"]
    original = client.get("/runs/" + run_id).json()
    config = client.app.state.settings
    draft = {
        "title": "[fixture] 暂无符合订阅条件的新闻",
        "items": [],
        "uncovered_topics": [{"topic": "开源模型", "reason": "[fixture] 检索为空"}],
        "shortfall_reason": "[fixture] 未发现有效新闻",
    }

    async def work():
        async with (
            open_runtime(config) as runtime,
            BochaClient(
                bocha_settings(config.data_dir),
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json=wire())),
            ) as bocha,
        ):
            runtime.run_store.clock = Clock().now
            registry = ToolRegistry()
            register_search(registry, bocha, runtime.candidate_store, config)

            async def reviewer(*args):
                return ReviewOutcome(approved=True, issues=())

            register_submission(
                registry, runtime.brief_store, reviewer=reviewer, review_basis="fixture"
            )
            model = ScriptedModel(
                [
                    response_factory(("search", "web_search", {"query": "fixture empty"})),
                    response_factory(("submit", "submit_brief", {"draft": draft})),
                ]
            )
            runner = AgentRunner(
                model,
                ToolExecutor(registry, runtime.call_store, config),
                runtime.conversation_store,
                config,
            )
            worker = Worker(runner)
            try:
                result = await worker.run_once()
                assert result.id == run_id and result.status == "no_news"
                assert await worker.run_once() is None
            finally:
                worker.leases.stop_worker(worker.worker_id)

    asyncio.run(work())
    finished = client.get("/runs/" + run_id).json()
    assert finished["status"] == "no_news" and finished["window_end"] == original["window_end"]
    document = client.get("/briefs/" + finished["brief_id"]).json()
    assert document["review_basis"] == "fixture" and document["document"]["title"] == draft["title"]
    assert len(client.get("/briefs").json()["items"]) == 1
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0
