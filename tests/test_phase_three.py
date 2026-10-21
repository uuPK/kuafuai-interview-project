import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest

from app.agent.protocol import ModelFailure
from app.agent.stubs import ScriptedModel
from tests.brief_support import draft, draft_item, review_world
from tests.phase_three_support import (
    Interrupted,
    environment,
    paired,
    record,
    reopen,
    seed,
    state,
    worker,
)
from tests.test_briefs import invocation
from tests.test_review import ReviewModel, reject


@pytest.mark.parametrize("correction", ["number", "conditions", "range"])
def test_revision_saved_response_restart_then_unique_scheduled_delivery(
    tmp_path, response_factory, correction
):
    async def run():
        env = environment(tmp_path, "revision-" + correction)
        reviews = ReviewModel(
            *(
                [{"approved": True, "issues": []}]
                if correction == "range"
                else [
                    reject(issue="原文为23%，比较条件是H100、batch size 1"),
                    {"approved": True, "issues": []},
                ]
            )
        )
        async with review_world(env, reviews) as world:
            good = draft(await draft_item(world))
            bad = deepcopy(good)
            if correction == "number":
                bad["items"][0]["summary"] = bad["items"][0]["summary"].replace("23%", "99%")
                bad["items"][0]["key_facts"][-1] = bad["items"][0]["key_facts"][-1].replace(
                    "23%", "99%"
                )
            elif correction == "conditions":
                bad["items"][0]["summary"] = "Atlas v2 已发布，相比Atlas v1吞吐量提高23%。"
                bad["items"][0]["key_facts"][-1] = "相比Atlas v1吞吐量提高23%"
            else:
                bad["items"][0]["citations"][0]["start_offset"] += 1
                bad["items"][0]["citations"][0]["end_offset"] += 1
                # A changed quote still needs model repair; unique unchanged anchors
                # are now relocated automatically and have their own regression.
                citation = bad["items"][0]["citations"][0]
                citation["quote"] = "错" + citation["quote"][1:]
            seed(env, response_factory(("bad", "submit_brief", {"draft": bad})))
            ref = good["items"][0]["citations"][0]
            path = f"sources/{ref['source_id']}/body.txt"

            def reread(request):
                feedback = json.loads(request.messages[-1].content)
                assert feedback["error"]["code"] == (
                    "BRIEF_RULES_REJECTED" if correction == "range" else "BRIEF_REVIEW_REJECTED"
                )
                return response_factory(
                    (
                        "reread",
                        "read_file",
                        {
                            "path": path,
                            "offset": ref["start_offset"],
                            "limit": 600,
                        },
                    )
                )

            def revise(request):
                assert (
                    "H100" in request.messages[-1].content and "23%" in request.messages[-1].content
                )
                return response_factory(
                    ("revised", "submit_brief", {"draft": good}),
                    (
                        "late",
                        "update_progress",
                        {"collected": ["开源模型"], "missing": [], "issues": []},
                    ),
                )

            model = ScriptedModel([reread, revise])
            active = worker(env, world, model)
            save = env.conversations.save_response

            def crash(run_id, user_id, response, **kwargs):
                result = save(run_id, user_id, response, **kwargs)
                if any(call.id == "revised" for call in response.message.tool_calls):
                    raise Interrupted
                return result

            env.conversations.save_response = crash
            original = state(env)
            with pytest.raises(Interrupted):
                await active.run_once()
            before = state(env)
            assert before["counts"] == {"briefs": 0, "delivered_history": 0, "mail_outbox": 0}
            assert env.calls.get_call(env.run.id, env.run.user_id, "revised").status == "pending"
            assert len(model.requests) == 2
        cold = reopen(env)
        async with review_world(cold, reviews) as world:
            no_research = ScriptedModel([])
            resumed = worker(cold, world, no_research)
            done = await resumed.run_once()
            after = state(cold)
            assert done.status == "completed" and not no_research.requests
            assert after["sources"] == before["sources"] == original["sources"]
            for key in ("run_id", "window_start", "window_end", "deadline_at"):
                assert after[key] == before[key] == original[key]
            for dimension in ("searches", "fetches"):
                assert after["budget_usage"][dimension] == before["budget_usage"][dimension]
            assert (
                after["budget_usage"]["model_requests"]
                == before["budget_usage"]["model_requests"] + 1
            )
            assert after["budget_usage"]["tool_calls"] == before["budget_usage"]["tool_calls"] + 1
            assert after["counts"] == {"briefs": 1, "delivered_history": 1, "mail_outbox": 1}
            assert (
                cold.calls.get_call(done.id, done.user_id, "late").result.error.code
                == "RUN_FINISHED"
            )
            paired(cold, world)
            assert await resumed.run_once() is None
            record(
                cold,
                world,
                "revision-" + correction,
                before_restart=before,
                after_restart=after,
                seeded_first_draft=True,
                research_requests=2,
                independent_review_requests=len(reviews.requests),
            )

    asyncio.run(run())


def test_real_process_exit_after_committed_queue_preserves_one_delivery(tmp_path, response_factory):
    async def prepare():
        env = environment(tmp_path, "committed-queue-process-exit")
        reviews = ReviewModel()
        async with review_world(env, reviews) as world:
            value = draft(await draft_item(world))
            seed(env, response_factory(("submit", "submit_brief", {"draft": value})))

            def crash(*args):
                raise Interrupted

            with pytest.raises(Interrupted):
                await world.executor.execute(
                    env.run.id,
                    env.run.user_id,
                    invocation(value),
                    on_finished=crash,
                )
            assert state(env)["counts"]["mail_outbox"] == 0
            return env, state(env)

    env, original = asyncio.run(prepare())
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP"}
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.phase_three_crash",
            str(env.settings.data_dir),
            env.clock.now().isoformat(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )
    assert result.returncode == 37, result.stderr
    committed = state(env)
    assert committed["status"] == "completed"
    assert committed["counts"] == {"briefs": 1, "delivered_history": 1, "mail_outbox": 1}
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_leases").fetchone()[0] == 1
        brief_id = connection.execute("SELECT id FROM briefs").fetchone()[0]
    env.clock.advance(4)

    async def resume():
        cold = reopen(env)
        async with review_world(cold, ReviewModel()) as world:
            model = ScriptedModel([])
            resumed = worker(cold, world, model)
            mail = world.briefs.outbox.get(brief_id, cold.run.user_id)
            done = await resumed.run_once()
            after = state(cold)
            assert done.status == "completed" and not model.requests
            assert after == committed
            assert after["budget_usage"] == original["budget_usage"]
            assert after["sources"] == original["sources"]
            assert world.briefs.outbox.get(brief_id, cold.run.user_id) == mail
            assert mail.status == "pending" and mail.attempts == 0
            paired(cold, world)
            with cold.database.connection() as connection:
                assert connection.execute("SELECT COUNT(*) FROM run_leases").fetchone()[0] == 0
            assert await resumed.run_once() is None
            record(
                cold,
                world,
                "committed-queue-process-exit",
                before_restart=committed,
                after_restart=after,
                child_exit_code=37,
                independent_review_requests=1,
            )

    asyncio.run(resume())


@pytest.mark.parametrize(
    "case,expected",
    [
        ("revision_limit", "brief_revision_exhausted"),
        ("authentication", "tool_dependency_authentication"),
        ("service_exhausted", "brief_review_service_failed"),
        ("deadline", "budget_deadline"),
        ("model_budget", "budget_model_requests"),
        ("material_changed", "recovery_material_state_problem"),
    ],
)
def test_failed_research_stops_with_paired_feedback_and_no_delivery(
    tmp_path, response_factory, case, expected
):
    async def run():
        overrides = {"external_max_attempts": 1}
        env = environment(tmp_path, "stop-" + case, **overrides)
        if case == "authentication":
            reviews = ReviewModel(ModelFailure("UNAUTHORIZED", category="authentication"))
        elif case == "service_exhausted":
            reviews = ReviewModel(ModelFailure("UNAVAILABLE", category="transient", retryable=True))
        else:
            reviews = ReviewModel(reject(), reject())
        async with review_world(env, reviews) as world:
            value = draft(await draft_item(world))
            if case == "revision_limit":
                # Invalid citations retain the two-attempt stop; factual review rejection
                # now has its separately tested unverified-only delivery path.
                value["items"][0]["citations"][0]["quote"] = "[fixture] invalid quotation"
            if case == "model_budget":
                # Prepare fixed evidence before tightening the budget for the recovery scenario.
                with env.database.transaction() as connection:
                    connection.execute(
                        "UPDATE runs SET budget_limits_json=json_set("
                        "budget_limits_json, '$.model_requests', 2) WHERE id=?",
                        (env.run.id,),
                    )
            seed(env, response_factory(("first", "submit_brief", {"draft": value})))
            original = state(env)
            if case == "deadline":
                env.clock.advance(601)
            if case == "material_changed":
                path = (
                    env.settings.data_dir
                    / "runs"
                    / env.run.id
                    / original["sources"][0]["file_path"]
                )
                path.write_bytes(path.read_bytes() + b"changed")
            model = ScriptedModel([response_factory(("second", "submit_brief", {"draft": value}))])
            active = worker(env, world, model)
            done = await active.run_once()
            assert done.status == "failed" and done.stop_reason == expected
            after = state(env)
            assert after["counts"] == {"briefs": 0, "delivered_history": 0, "mail_outbox": 0}
            for key in ("run_id", "window_start", "window_end", "deadline_at"):
                assert after[key] == original[key]
            paired(env, world)
            assert await active.run_once() is None
            record(
                env,
                world,
                "stop-" + case,
                before_execution=original,
                research_requests=len(model.requests),
                independent_review_requests=len(reviews.requests),
            )

    asyncio.run(run())
