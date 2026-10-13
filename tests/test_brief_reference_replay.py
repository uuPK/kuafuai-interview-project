"""Opt-in regression against untouched original DeepSeek citations, zero model calls."""

import json
import os
from pathlib import Path

import pytest

from app.agent.brief_rules import BriefRules
from app.agent.briefs import BriefDraft, BriefItem, UncoveredTopic
from app.config import Settings
from app.storage.calls import CallStore
from app.storage.candidates import CandidateStore
from app.storage.database import Database
from app.storage.runs import RunStore
from app.storage.sources import SourceStore
from app.tools.context import bind_context

pytestmark = pytest.mark.skipif(
    os.environ.get("NEWS_SUBMISSION_REPLAY") != "1",
    reason="requires explicit NEWS_SUBMISSION_REPLAY=1 and recorded step-2.6 runs",
)


@pytest.mark.parametrize("profile", ["model", "chip", "gap"])
def test_original_live_quote_ranges_are_rejected_and_corrected_ranges_pass_rules(profile):
    root = Path("data/acceptance/research-live-2026-10-13") / profile
    files = list(root.glob("*.json"))
    assert files, "missing original recorded run"
    original = json.loads(
        max(files, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
    )
    config = Settings(_env_file=None, data_dir=root)
    database = Database(config.database_path)
    database.initialize()
    runs = RunStore(database, config)
    run = runs.get_run(original["run_id"], "live-fixture")
    sources = SourceStore(CandidateStore(CallStore(runs)), config)
    context = bind_context(runs, config.data_dir, run.id, run.user_id)
    # Project only formal-schema fields and supply a fixture title; never rewrite the model file.
    projected = {
        "title": "历史草稿引用回归（fixture）",
        "items": [
            {key: value for key, value in item.items() if key in BriefItem.model_fields}
            for item in original["draft"]["items"]
        ],
        "uncovered_topics": [
            {key: value for key, value in note.items() if key in UncoveredTopic.model_fields}
            for note in original["draft"].get("uncovered_topics", [])
        ],
    }
    for item in projected["items"]:
        item["citations"] = [
            {key: ref[key] for key in ("source_id", "start_offset", "end_offset", "quote")}
            for ref in item["citations"]
        ]
    draft = BriefDraft.model_validate_json(json.dumps(projected, ensure_ascii=False))
    rules = BriefRules(sources, config)
    _, rejected = rules.check(context, draft)
    assert rejected.error.code == "BRIEF_RULES_REJECTED"
    checks = []
    for item in projected["items"]:
        for ref in item["citations"]:
            meta, index = sources.get_source(run.id, run.user_id, ref["source_id"])
            body = sources.files.verify(context, meta, index)
            before = [ref["start_offset"], ref["end_offset"]]
            assert body[before[0] : before[1]] != ref["quote"]
            start = body.index(ref["quote"])
            ref["start_offset"], ref["end_offset"] = start, start + len(ref["quote"])
            checks.append(
                {
                    "source_id": meta.source_id,
                    "content_hash": meta.content_hash,
                    "original_range": before,
                    "corrected_range": [start, ref["end_offset"]],
                }
            )
    corrected = BriefDraft.model_validate_json(json.dumps(projected, ensure_ascii=False))
    checked, rejected_after = rules.check(context, corrected)
    assert rejected_after is None and checked is not None
    assert runs.get_run(run.id, run.user_id) == run
    report = {
        "profile": profile,
        "run_id": run.id,
        "scope": "stored_quote_rule_regression",
        "fixture_title_and_schema_projection": True,
        "original_citations_rejected": len(checks),
        "corrected_citations_pass_rules": len(checks),
        "new_model_requests": 0,
        "formal_quality_review_executed": False,
        "checks": checks,
        "issues": [issue.model_dump() for issue in rejected.error.details],
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
    }
    target = Path("data/acceptance/brief-reference-replay-2026-10-13")
    target.mkdir(parents=True, exist_ok=True)
    (target / (profile + ".json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_recorded_real_nvidia_sources_pass_rules_without_new_http_requests():
    report_path = Path("data/acceptance/rss-live-2026-10-13.json")
    assert report_path.exists(), "missing original real-source record"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    config = Settings(_env_file=None, data_dir=Path("data/acceptance/rss-live-2026-10-13"))
    database = Database(config.database_path)
    database.initialize()
    runs = RunStore(database, config)
    with database.connection() as connection:
        owner = connection.execute(
            "SELECT user_id FROM runs WHERE id=?", (report["run_id"],)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id FROM sources WHERE run_id=?", (report["run_id"],)
        ).fetchall()
    run = runs.get_run(report["run_id"], owner)
    sources = SourceStore(CandidateStore(CallStore(runs)), config)
    context = bind_context(runs, config.data_dir, run.id, owner)
    checks = []
    assert len(rows) == 2
    for row in rows:
        meta, index = sources.get_source(run.id, owner, row["id"])
        body = sources.files.verify(context, meta, index)
        draft = BriefDraft.model_validate_json(
            json.dumps(
                {
                    "title": "公开正文规则回归（fixture）",
                    "items": [
                        {
                            "title": "NVIDIA公开文章",
                            "summary": "本测试仅检查已保存公开正文的来源与引用规则。",
                            "topics": list(run.subscription.topics),
                            "event_id": meta.source_id,
                            "key_facts": ["官方文章已发布"],
                            "selection_reason": "来源和时间可从固定快照核对",
                            "citations": [
                                {
                                    "source_id": meta.source_id,
                                    "start_offset": 0,
                                    "end_offset": 100,
                                    "quote": body[:100],
                                }
                            ],
                        }
                    ],
                    "shortfall_reason": "逐篇验证引用，独立摘要核对尚未执行",
                },
                ensure_ascii=False,
            )
        )
        checked, rejected = BriefRules(sources, config).check(context, draft)
        assert checked is not None and rejected is None
        checks.append(
            {
                "source_id": meta.source_id,
                "content_hash": meta.content_hash,
                "url": meta.url,
                "char_count": len(body),
                "rules_passed": True,
            }
        )
    assert runs.get_run(run.id, owner) == run
    target = Path("data/acceptance/brief-reference-replay-2026-10-13")
    target.mkdir(parents=True, exist_ok=True)
    (target / "public-sources.json").write_text(
        json.dumps(
            {
                "run_id": run.id,
                "scope": "recorded_public_body_rule_regression",
                "new_model_requests": 0,
                "new_http_requests": 0,
                "formal_quality_review_executed": False,
                "fixture_summaries": True,
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
