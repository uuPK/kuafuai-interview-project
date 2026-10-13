"""Immutable discovery snapshots and run-owned candidates, ready for source fetching."""

import json

from pydantic import AwareDatetime

from app.storage.calls import CallStatus, CallStore
from app.storage.models import StoredModel, timestamp
from app.tools.discovery import CandidateMaterial
from app.tools.models import ToolResult


class CandidateRecord(StoredModel):
    candidate_id: str
    run_id: str
    tool_call_id: str
    position: int
    material: CandidateMaterial


class DiscoveryRecord(StoredModel):
    run_id: str
    tool_call_id: str
    provider: str
    provider_request_id: str | None
    result: ToolResult
    created_at: AwareDatetime


def _discovery(row):
    values = dict(row)
    values.pop("raw_json")
    values["result"] = ToolResult.model_validate_json(values.pop("result_json"))
    return DiscoveryRecord.model_validate(values)


class CandidateStore:
    def __init__(self, calls: CallStore):
        self.calls = calls
        self.runs = calls.runs
        self.database = calls.database

    def discovery(self, run_id: str, user_id: str, call_id: str) -> DiscoveryRecord | None:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            self.calls._row(connection, run_id, call_id)
            row = connection.execute(
                "SELECT * FROM discoveries WHERE run_id = ? AND tool_call_id = ?",
                (run_id, call_id),
            ).fetchone()
            return _discovery(row) if row is not None else None

    def save(
        self,
        run_id: str,
        user_id: str,
        call_id: str,
        *,
        provider: str,
        provider_request_id: str | None,
        raw: dict,
        candidates: tuple[CandidateRecord, ...],
        result: ToolResult,
        references: tuple[str, ...] = (),
    ) -> DiscoveryRecord:
        """Persist the complete replay result with its candidate IDs in one short transaction."""
        if not result.ok:
            raise ValueError("only successful discoveries can be cached")
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            call = self.calls._row(connection, run_id, call_id)
            row = connection.execute(
                "SELECT * FROM discoveries WHERE run_id = ? AND tool_call_id = ?",
                (run_id, call_id),
            ).fetchone()
            if row is not None:
                return _discovery(row)
            if call["status"] != CallStatus.EXECUTING:
                raise ValueError("discovery must belong to an executing call")
            at = self.runs.now()
            connection.execute(
                "INSERT INTO discoveries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    call_id,
                    provider,
                    provider_request_id,
                    json.dumps(raw, ensure_ascii=False, allow_nan=False),
                    result.model_dump_json(),
                    timestamp(at),
                ),
            )
            for candidate in candidates:
                if candidate.run_id != run_id or candidate.tool_call_id != call_id:
                    raise PermissionError("candidate does not belong to this discovery")
                connection.execute(
                    "INSERT INTO candidates VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.candidate_id,
                        run_id,
                        call_id,
                        candidate.position,
                        candidate.material.model_dump_json(),
                    ),
                )
            for candidate_id in references:
                if (
                    connection.execute(
                        "SELECT id FROM candidates WHERE run_id=? AND id=?", (run_id, candidate_id)
                    ).fetchone()
                    is None
                ):
                    raise PermissionError("referenced candidate does not belong to this run")
            self.runs._append_event(
                connection,
                run_id,
                "search_discovered",
                at,
                {
                    "tool_call_id": call_id,
                    "provider": provider,
                    "provider_request_id": provider_request_id,
                    "candidate_ids": list(
                        dict.fromkeys(
                            [candidate.candidate_id for candidate in candidates] + list(references)
                        )
                    ),
                    "received_count": result.data["received_count"],
                    "filtered": result.data["filtered"],
                    "omitted_count": result.data["omitted_count"],
                },
            )
            return _discovery(
                connection.execute(
                    "SELECT * FROM discoveries WHERE run_id = ? AND tool_call_id = ?",
                    (run_id, call_id),
                ).fetchone()
            )

    def get_candidate(self, run_id: str, user_id: str, candidate_id: str) -> CandidateRecord:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            row = connection.execute(
                "SELECT * FROM candidates WHERE run_id = ? AND id = ?", (run_id, candidate_id)
            ).fetchone()
            if row is None:
                raise LookupError("candidate not found in current run")
            values = dict(row)
            values["candidate_id"] = values.pop("id")
            values["material"] = CandidateMaterial.model_validate_json(values.pop("material_json"))
            return CandidateRecord.model_validate(values)

    def known_urls(self, run_id, user_id) -> dict[str, CandidateRecord]:
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            rows = connection.execute(
                "SELECT * FROM candidates WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        found = {}
        for row in rows:
            material = CandidateMaterial.model_validate_json(row["material_json"])
            if material.url not in found:
                found[material.url] = CandidateRecord(
                    candidate_id=row["id"],
                    run_id=run_id,
                    tool_call_id=row["tool_call_id"],
                    position=row["position"],
                    material=material,
                )
        return found
