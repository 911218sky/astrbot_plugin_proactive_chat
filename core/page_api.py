"""AstrBot 官方插件頁 API。

提供只讀任務看板資料，讓官方 WebUI 的 plugin Pages 可以查看目前排程狀態。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .config import get_session_config
from .utils import get_session_log_str, parse_session_id

PLUGIN_NAME = "astrbot_plugin_proactive_chat_plus"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"
_LOG_TAG = "[主動訊息]"


class PluginPageApi:
    """Proactive Chat 官方插件頁 API。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(
            f"{PAGE_API_PREFIX}/status",
            self.get_status,
            ["GET"],
            "Proactive Chat Page status",
        )
        register(
            f"{PAGE_API_PREFIX}/tasks",
            self.list_tasks,
            ["GET"],
            "Proactive Chat Page tasks",
        )

    async def get_status(self):
        """回傳看板摘要。"""
        try:
            data = self._build_snapshot(include_tasks=False)
            return self._ok(data)
        except Exception as e:
            logger.error(f"{_LOG_TAG} Page API 取得狀態失敗: {e}", exc_info=True)
            return self._error(str(e))

    async def list_tasks(self):
        """回傳所有可觀察的待執行任務。"""
        try:
            data = self._build_snapshot(include_tasks=True)
            return self._ok(data)
        except Exception as e:
            logger.error(f"{_LOG_TAG} Page API 取得任務失敗: {e}", exc_info=True)
            return self._error(str(e))

    def _build_snapshot(self, *, include_tasks: bool) -> dict[str, Any]:
        now_ts = time.time()
        jobs = self.plugin.scheduler.get_jobs() if self.plugin.scheduler else []
        regular_jobs = [j for j in jobs if not str(j.id).startswith("ctx_")]
        scheduler_ctx_jobs = [j for j in jobs if str(j.id).startswith("ctx_")]
        context_tasks = self._collect_context_tasks(scheduler_ctx_jobs)
        auto_timers = self._collect_timers(
            self.plugin.auto_trigger_timers,
            "auto_trigger",
            "自動觸發等待",
            now_ts,
        )
        group_timers = self._collect_timers(
            self.plugin.group_timers,
            "group_idle",
            "群聊沉默倒計時",
            now_ts,
        )

        session_count = sum(
            1 for info in self.plugin.session_data.values() if isinstance(info, dict)
        )
        summary = {
            "scheduler_running": bool(
                self.plugin.scheduler and self.plugin.scheduler.running
            ),
            "timezone": str(self.plugin.timezone) if self.plugin.timezone else "local",
            "session_count": session_count,
            "regular_count": len(regular_jobs),
            "context_count": len(context_tasks),
            "auto_trigger_count": len(auto_timers),
            "group_idle_count": len(group_timers),
            "total_count": (
                len(regular_jobs)
                + len(context_tasks)
                + len(auto_timers)
                + len(group_timers)
            ),
            "generated_at": self._format_datetime(datetime.now(self.plugin.timezone)),
        }

        data: dict[str, Any] = {"summary": summary}
        if include_tasks:
            tasks = []
            tasks.extend(self._collect_scheduler_jobs(regular_jobs, "regular"))
            tasks.extend(context_tasks)
            tasks.extend(auto_timers)
            tasks.extend(group_timers)
            tasks.sort(key=lambda item: item.get("sort_time") or float("inf"))
            for task in tasks:
                task.pop("sort_time", None)
            data["tasks"] = tasks
        return data

    def _collect_scheduler_jobs(self, jobs: list[Any], task_type: str) -> list[dict]:
        result = []
        for job in jobs:
            session_id = str(job.id)
            next_run_time = getattr(job, "next_run_time", None)
            result.append(
                self._task_base(
                    session_id=session_id,
                    task_id=session_id,
                    task_type=task_type,
                    title="一般主動訊息",
                    next_run_time=next_run_time,
                    detail="依照排程設定觸發",
                )
            )
        return result

    def _collect_context_tasks(self, scheduler_ctx_jobs: list[Any]) -> list[dict]:
        result = []
        tracked_job_ids = set()
        scheduler_index = {str(job.id): job for job in scheduler_ctx_jobs}

        for session_id, tasks in self.plugin._pending_context_tasks.items():
            session_config = get_session_config(self.plugin.config, session_id)
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                job_id = str(task.get("job_id", ""))
                tracked_job_ids.add(job_id)
                job = scheduler_index.get(job_id)
                run_at = self._parse_datetime(task.get("run_at"))
                next_run_time = getattr(job, "next_run_time", None) or run_at
                result.append(
                    self._task_base(
                        session_id=session_id,
                        task_id=job_id or f"ctx:{session_id}",
                        task_type="context",
                        title="語境預測主動訊息",
                        next_run_time=next_run_time,
                        session_config=session_config,
                        detail=task.get("reason") or task.get("hint") or "無描述",
                        extra={
                            "reason": task.get("reason", ""),
                            "hint": task.get("hint", ""),
                            "delay_minutes": task.get("delay_minutes"),
                            "created_at": self._format_timestamp(
                                task.get("created_at")
                            ),
                            "tracked": True,
                        },
                    )
                )

        for job in scheduler_ctx_jobs:
            job_id = str(job.id)
            if job_id in tracked_job_ids:
                continue
            result.append(
                self._task_base(
                    session_id=self._session_from_context_job(job),
                    task_id=job_id,
                    task_type="context_orphan",
                    title="未追蹤的語境排程",
                    next_run_time=getattr(job, "next_run_time", None),
                    detail="APScheduler 中存在，但 pending_context_tasks 未追蹤",
                    extra={"tracked": False},
                )
            )
        return result

    def _collect_timers(
        self,
        timers: dict[str, Any],
        task_type: str,
        title: str,
        now_ts: float,
    ) -> list[dict]:
        result = []
        loop_time = self._current_loop_time(timers)

        for session_id, handle in timers.items():
            when = getattr(handle, "when", None)
            remaining = None
            if callable(when) and loop_time is not None:
                remaining = max(0.0, when() - loop_time)
            next_ts = now_ts + remaining if remaining is not None else None
            result.append(
                self._task_base(
                    session_id=session_id,
                    task_id=f"{task_type}:{session_id}",
                    task_type=task_type,
                    title=title,
                    next_run_time=next_ts,
                    detail="等待條件成立後建立正式排程",
                    extra={"remaining_seconds": int(remaining) if remaining else 0},
                )
            )
        return result

    def _current_loop_time(self, timers: dict[str, Any]) -> float | None:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            pass
        except Exception:
            return None

        try:
            first_handle = next(iter(timers.values())) if timers else None
            loop = getattr(first_handle, "_loop", None)
            return loop.time() if loop else None
        except Exception:
            return None

    def _task_base(
        self,
        *,
        session_id: str,
        task_id: str,
        task_type: str,
        title: str,
        next_run_time: Any,
        session_config: dict | None = None,
        detail: str = "",
        extra: dict | None = None,
    ) -> dict:
        session_config = session_config or get_session_config(
            self.plugin.config, session_id
        )
        session_info = self.plugin.session_data.get(session_id, {})
        if not isinstance(session_info, dict):
            session_info = {}
        next_dt = self._coerce_datetime(next_run_time)
        parsed = parse_session_id(session_id)
        message_type = parsed[1] if parsed else ""
        target_id = parsed[2] if parsed else session_id
        return {
            "id": task_id,
            "type": task_type,
            "title": title,
            "session_id": session_id,
            "session_label": get_session_log_str(
                session_id, session_config, self.plugin.session_data
            ),
            "message_type": message_type,
            "target_id": target_id,
            "enabled": bool(session_config and session_config.get("enable", False)),
            "unanswered_count": int(session_info.get("unanswered_count", 0) or 0),
            "last_message_time": self._format_timestamp(
                session_info.get("last_message_time")
            ),
            "next_run_time": self._format_datetime(next_dt),
            "remaining_seconds": self._remaining_seconds(next_dt),
            "detail": detail,
            "extra": extra or {},
            "sort_time": next_dt.timestamp() if next_dt else None,
        }

    def _session_from_context_job(self, job: Any) -> str:
        args = getattr(job, "args", None) or ()
        if args:
            return str(args[0])
        job_id = str(getattr(job, "id", ""))
        if job_id.startswith("ctx_"):
            parts = job_id.rsplit("_", 1)
            return parts[0][4:] if parts else job_id
        return job_id

    def _remaining_seconds(self, value: datetime | None) -> int | None:
        if value is None:
            return None
        return max(0, int(value.timestamp() - time.time()))

    def _format_timestamp(self, value: Any) -> str:
        if not isinstance(value, (int, float)) or value <= 0:
            return ""
        return self._format_datetime(datetime.fromtimestamp(value, tz=self.plugin.timezone))

    def _format_datetime(self, value: datetime | None) -> str:
        if value is None:
            return ""
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _coerce_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=self.plugin.timezone)
        if isinstance(value, str):
            return self._parse_datetime(value)
        return None

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _ok(self, data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "data": data or {}}

    def _error(self, message: str) -> dict[str, str]:
        return {"status": "error", "message": message}
