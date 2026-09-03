"""One owner for execution grants, runtimes and whole-workflow window leases."""
from __future__ import annotations

from contextlib import ExitStack

from .browser_scheduler import BrowserWaitCancelled


class ExecutionLifecycle:
    def __init__(self, *, scheduler, policy_channel, plan, execution_id,
                 conversation_id, cancelled, finish_runtime, report_error):
        self.scheduler = scheduler
        self.policy_channel = policy_channel
        self.plan = plan
        self.execution_id = execution_id
        self.conversation_id = conversation_id
        self.cancelled = cancelled
        self.finish_runtime = finish_runtime
        self.report_error = report_error
        self._leases = ExitStack()
        self._runtime_finished = False
        self._grant_attempted = False

    def __enter__(self):
        return self

    def acquire(self, resolve_resources, *, on_wait):
        resources = resolve_resources()
        self._leases.enter_context(self.scheduler.reserve(resources,
            conversation_id=self.conversation_id, execution_id=self.execution_id,
            cancelled=self.cancelled, on_wait=on_wait))
        if self.cancelled.is_set():
            raise BrowserWaitCancelled("已取消等待浏览器窗口。")
        if resolve_resources() != resources:
            raise ValueError("等待期间浏览器窗口注册信息已变化，请重新执行任务。")
        self._grant_attempted = True
        self.policy_channel.grant(self.execution_id,
            max_download_posts=self.plan.max_download_posts,
            allowed_session_refs=[item.session_ref for item in self.plan.authorized_browser_sessions()],
            task_id=self.plan.task_id, steps=self.plan.execution_steps())

    def stop_runtime(self, *, failed=False):
        """Revoke once; join cancelled/isolated runtime before cleanup or handoff."""
        if self._runtime_finished:
            return
        self._runtime_finished = True
        errors = []
        actions = [lambda: self.finish_runtime(failed)]
        if self._grant_attempted:
            actions.insert(0, lambda: self.policy_channel.revoke(self.execution_id))
        for action in actions:
            try:
                action()
            except BaseException as error:
                errors.append(error)
                self.report_error(error)
        if errors:
            raise errors[0]

    def __exit__(self, exc_type, exc, traceback):
        try:
            try:
                self.stop_runtime(failed=exc is not None)
            except BaseException:
                if exc is None:
                    raise
        finally:
            try:
                self._leases.close()
            finally:
                self.cancelled.clear()
        return False
