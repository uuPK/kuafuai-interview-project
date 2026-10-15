"""Recover durable artifacts first; retry only uncertain, explicitly safe requests."""

from app.agent.protocol import ModelRequest
from app.harness.failures import FailureInfo
from app.storage.models import StateChange
from app.tools.bash import DockerRunner
from app.tools.context import bind_context
from app.tools.models import ToolFailure, ToolInvocation, ToolResult

TERMINAL = {"completed", "no_news", "failed"}
LOCAL_TOOLS = {
    "list_dir",
    "read_file",
    "search_content",
    "query_history",
    "update_progress",
    "bash",
    "write_file",
}


class Recovery:
    def __init__(self, runner, *, sources=None, materials=None, docker=None):
        self.runner, self.executor, self.runs = runner, runner.executor, runner.runs
        self.database, self.requests = self.runs.database, runner.requests
        self.sources, self.materials = sources, materials
        self.docker = docker or DockerRunner(runner.settings)

    def event(self, run_id, user_id, kind, data):
        with self.database.transaction() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            self.runs._append_event(connection, run_id, kind, self.runs.now(), data)

    def _feedback(self, run_id, user_id, call_id):
        with self.database.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM model_tool_calls WHERE run_id=? AND tool_call_id=?",
                (run_id, call_id),
            ).fetchone()
        return self.runner._feedback(user_id) if exists else None

    def _calls(self, run_id, user_id):
        with self.database.connection() as connection:
            self.runs._owned_row(connection, run_id, user_id)
            return [
                ToolInvocation(
                    tool_call_id=row["tool_call_id"], name=row["name"], arguments=row["arguments"]
                )
                for row in connection.execute(
                    "SELECT * FROM tool_calls WHERE run_id=? "
                    "AND status!='completed' ORDER BY rowid",
                    (run_id,),
                )
            ]

    def _temporary_files(self, context):
        roots = [
            context.working_dir,
            self.runner.settings.data_dir / "source_jobs" / context.run_id,
            self.runner.settings.data_dir / "file_jobs" / context.run_id,
        ]
        removed, scanned = 0, 0
        for root in roots:
            if not root.exists():
                continue
            if root.resolve() != root.absolute() or root.is_symlink() or root.is_junction():
                raise ToolFailure("RECOVERY_PATH_PROBLEM", "恢复目录包含链接，已停止清理。")
            stack = [root]
            while stack:
                for path in stack.pop().iterdir():
                    scanned += 1
                    if scanned > self.runner.settings.max_material_files * 4 + 100:
                        raise ToolFailure("RECOVERY_PATH_PROBLEM", "恢复目录超过扫描边界。")
                    if (
                        path.is_symlink()
                        or path.is_junction()
                        or not path.resolve().is_relative_to(root)
                    ):
                        raise ToolFailure("RECOVERY_PATH_PROBLEM", "恢复路径越界或包含链接。")
                    if path.is_dir():
                        stack.append(path)
                    elif path.name.startswith(".pending-") and path.is_file():
                        # Revalidate the absolute target and ownership under the takeover lock.
                        with self.database.transaction():
                            if path.resolve().is_relative_to(root) and not path.is_symlink():
                                path.unlink()
                                removed += 1
        return removed

    async def recover(self, run_id, user_id):
        context = bind_context(self.runs, self.runner.settings.data_dir, run_id, user_id)
        with self.database.connection() as connection:
            has_bash = connection.execute(
                "SELECT 1 FROM run_events WHERE run_id=? AND kind='bash_started'", (run_id,)
            ).fetchone()
        if has_bash:
            removed = await self.docker.cleanup_run(context)
            self.event(run_id, user_id, "bash_recovery_cleaned", {"container_count": removed})
        removed = self._temporary_files(context)
        if removed:
            self.event(run_id, user_id, "temporary_files_cleaned", {"file_count": removed})
        run = self.runs.get_run(run_id, user_id)
        if (
            run.status not in TERMINAL
            and run.deadline_at is not None
            and self.runs.now() >= run.deadline_at
        ):
            self.runs.transition(
                run_id, user_id, StateChange(status="failed", stop_reason="budget_deadline")
            )
            run = self.runs.get_run(run_id, user_id)
        if run.status not in TERMINAL and self.sources is not None:
            with self.database.connection() as connection:
                ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM sources WHERE run_id=?", (run_id,)
                    )
                ]
            for source_id in ids:
                self.sources.get_source(run_id, user_id, source_id, repair_index=True)
        # Unknown model responses cannot be invented. Retry keeps the charged old attempt.
        saved = self.requests.active_model(run_id, user_id)
        if saved is not None:
            try:
                ModelRequest.model_validate_json(saved.payload_json)
            except ValueError:
                raise ToolFailure(
                    "RECOVERY_STATE_PROBLEM", "已保存的模型请求损坏，未重复请求。"
                ) from None
            if run.status in TERMINAL:
                self.requests.abandon(saved.id, user_id)
            elif saved.status == "executing":
                self.requests.fail_attempt(
                    saved.id,
                    user_id,
                    FailureInfo(code="MODEL_INTERRUPTED", retryable=True, category="transient"),
                    self.runner.settings,
                    failure_stop_reason="model_recovery_exhausted",
                    reserve_model_requests=self.executor.registry.submission_model_reserve,
                )
                self.event(
                    run_id,
                    user_id,
                    "model_request_recovered",
                    {"request_id": saved.id, "attempts": saved.attempts},
                )
        for invocation in self._calls(run_id, user_id):
            call = self.executor.calls.get_call(run_id, user_id, invocation.tool_call_id)
            if call.status != "executing":
                continue
            feedback = self._feedback(run_id, user_id, invocation.tool_call_id)
            run = self.runs.get_run(run_id, user_id)
            if run.status in TERMINAL:
                self.executor.finish(
                    run_id,
                    user_id,
                    invocation,
                    ToolResult.failure(
                        "BUDGET_EXCEEDED"
                        if run.stop_reason == "budget_deadline"
                        else "RUN_FINISHED",
                        "任务已结束，未继续执行中断调用。",
                    ),
                    on_finished=feedback,
                    stop_reason="retry_deadline" if run.stop_reason == "budget_deadline" else None,
                )
                continue
            request = self.requests.for_tool(run_id, user_id, invocation.tool_call_id)
            if request is not None and request.status != "executing":
                continue  # Durable waiting/feedback is already handled by ToolExecutor.
            definition = self.executor.registry.get(invocation.name)
            local = invocation.name in LOCAL_TOOLS
            try:
                bound = getattr(definition.handler, "__self__", None) if definition else None
                context = bind_context(
                    self.runs,
                    self.runner.settings.data_dir,
                    run_id,
                    user_id,
                    tool_call_id=invocation.tool_call_id,
                )
                if invocation.name == "write_file" and bound is not None:
                    parameters = self.executor._arguments(definition, invocation)
                    if (
                        not bound.store.job_path(context).exists()
                        and context.resolve_path(parameters.path, write=True).exists()
                    ):
                        raise ToolFailure(
                            "FILE_WRITE_STATE_PROBLEM",
                            "中断写入缺少意图且目标已存在，无法确认原内容；请重新读取再决定。",
                        )
                if invocation.name == "fetch_url" and bound is not None:
                    local = bound.store.files.load_job(context, repair_index=True) is not None
                if invocation.name in {"web_search", "read_feed"} and bound is not None:
                    local = (
                        bound.store.discovery(run_id, user_id, invocation.tool_call_id) is not None
                    )
                if invocation.name == "submit_brief" and bound is not None:
                    with self.database.connection() as connection:
                        local = (
                            connection.execute(
                                "SELECT 1 FROM submission_jobs WHERE run_id=? AND tool_call_id=?",
                                (run_id, invocation.tool_call_id),
                            ).fetchone()
                            is not None
                            or connection.execute(
                                "SELECT 1 FROM brief_reviews WHERE run_id=? AND tool_call_id=? "
                                "AND response_json IS NOT NULL",
                                (run_id, invocation.tool_call_id),
                            ).fetchone()
                            is not None
                        )
                if local and definition is not None:
                    await self.executor.replay_local(
                        run_id, user_id, invocation, on_finished=feedback
                    )
                    mode = "local_replay"
                elif definition is not None and definition.retry_safe and request is not None:
                    self.requests.fail_attempt(
                        request.id,
                        user_id,
                        FailureInfo(code="TOOL_INTERRUPTED", retryable=True, category="transient"),
                        self.runner.settings,
                        feedback=ToolResult.failure(
                            "TOOL_INTERRUPTED",
                            "调用在中断时结果不确定，按原预算有限重试。",
                            retryable=True,
                        ),
                    )
                    mode = "bounded_retry"
                else:
                    self.executor.finish(
                        run_id,
                        user_id,
                        invocation,
                        ToolResult.failure(
                            "TOOL_INTERRUPTED_UNSAFE",
                            "中断调用没有可验证结果，未假定成功或重复执行。",
                        ),
                        on_finished=feedback,
                    )
                    mode = "explicit_failure"
            except ToolFailure as error:
                self.executor.finish(
                    run_id, user_id, invocation, error.result, on_finished=feedback
                )
                mode = "material_problem"
            self.event(
                run_id,
                user_id,
                "tool_call_recovered",
                {"tool_call_id": invocation.tool_call_id, "name": invocation.name, "mode": mode},
            )
        if self.runs.get_run(run_id, user_id).status in TERMINAL:
            self.close_requests(run_id, user_id)

    def close_requests(self, run_id, user_id):
        with self.database.connection() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM external_requests WHERE run_id=? AND status!='completed'",
                    (run_id,),
                )
            ]
        for request_id in ids:
            self.requests.abandon(request_id, user_id)

    async def fail_and_pair(self, run_id, user_id, reason, code):
        run = self.runs.get_run(run_id, user_id)
        self.event(run_id, user_id, "recovery_failed", {"error_code": code})
        if run.status not in TERMINAL:
            self.runs.transition(run_id, user_id, StateChange(status="failed", stop_reason=reason))
        saved = self.requests.active_model(run_id, user_id)
        if saved is not None:
            self.requests.abandon(saved.id, user_id)
        for invocation in self._calls(run_id, user_id):
            self.executor.finish(
                run_id,
                user_id,
                invocation,
                ToolResult.failure("RUN_FINISHED", "恢复失败，任务已结束。"),
                on_finished=self._feedback(run_id, user_id, invocation.tool_call_id),
            )
        self.close_requests(run_id, user_id)
        return await self.runner.run(run_id, user_id)
