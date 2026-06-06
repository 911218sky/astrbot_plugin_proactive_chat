"""AstrBot 官方插件頁 API。

提供任務看板與操作 API，讓官方 WebUI 的 plugin Pages 可以查看、建立、修改、
立即執行與刪除目前排程狀態。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from quart import request

from .config import get_session_config
from .utils import (
    MSG_TYPE_FRIEND,
    MSG_TYPE_GROUP,
    get_session_log_str,
    parse_session_id,
    resolve_full_umo,
)

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
        register(
            f"{PAGE_API_PREFIX}/tasks/action",
            self.handle_task_action,
            ["POST"],
            "Proactive Chat Page task actions",
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

    async def handle_task_action(self):
        """建立、修改、刪除或立即執行任務。"""
        try:
            payload = await request.get_json(silent=True) or {}
            action = str(payload.get("action", "")).strip()
            if action == "create":
                result = await self._create_or_reschedule(payload, require_task=False)
            elif action == "reschedule":
                result = await self._create_or_reschedule(payload, require_task=True)
            elif action == "delete":
                result = await self._delete_task(payload)
            elif action == "run_now":
                result = await self._run_now(payload)
            else:
                return self._error("未知操作")
            return self._ok(result)
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            logger.error(f"{_LOG_TAG} Page API 操作任務失敗: {e}", exc_info=True)
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
            data["sessions"] = self._collect_sessions()
        return data

    async def _create_or_reschedule(
        self, payload: dict[str, Any], *, require_task: bool
    ) -> dict[str, Any]:
        session_id = self._resolve_session_from_payload(payload)
        run_date = self._parse_run_date(payload)

        if require_task:
            task_id = str(payload.get("task_id") or "").strip()
            task_type = str(payload.get("task_type") or "").strip()
            if task_type == "context":
                return await self._reschedule_context_task(task_id, session_id, run_date)
            if task_type in {"auto_trigger", "group_idle"}:
                self._cancel_timer_task(task_type, session_id)
            elif task_type == "regular":
                if not self.plugin.scheduler or not self.plugin.scheduler.get_job(task_id):
                    raise ValueError("找不到指定一般排程任務")
            else:
                raise ValueError("不支援修改此任務類型")

        if not self.plugin.scheduler:
            raise ValueError("排程器尚未啟動")
        delay_seconds = max(1, int(run_date.timestamp() - time.time()))
        self.plugin._add_scheduled_job(session_id, delay_seconds)
        async with self.plugin.data_lock:
            sd = self.plugin.session_data.setdefault(session_id, {})
            sd["next_trigger_time"] = time.time() + delay_seconds
            await self.plugin._save_data()

        logger.info(
            f"{_LOG_TAG} Web 任務頁已為 {session_id} 設定手動排程："
            f"{run_date.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return {"session_id": session_id, "next_run_time": self._format_datetime(run_date)}

    async def _reschedule_context_task(
        self, task_id: str, session_id: str, run_date: datetime
    ) -> dict[str, Any]:
        if not task_id:
            raise ValueError("缺少任務 ID")
        if not self.plugin.scheduler:
            raise ValueError("排程器尚未啟動")

        found = False
        for task in self.plugin._pending_context_tasks.get(session_id, []):
            if isinstance(task, dict) and str(task.get("job_id", "")) == task_id:
                task["run_at"] = run_date.isoformat()
                found = True
                break
        if not found:
            raise ValueError("找不到指定語境任務")

        self.plugin.scheduler.add_job(
            self.plugin.check_and_chat,
            "date",
            run_date=run_date,
            args=[session_id],
            kwargs={"ctx_job_id": task_id},
            id=task_id,
            replace_existing=True,
            misfire_grace_time=60,
        )
        async with self.plugin.data_lock:
            sd = self.plugin.session_data.setdefault(session_id, {})
            sd["pending_context_tasks"] = self.plugin._pending_context_tasks.get(
                session_id, []
            )
            await self.plugin._save_data()
        return {"session_id": session_id, "next_run_time": self._format_datetime(run_date)}

    async def _delete_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "").strip()
        task_type = str(payload.get("task_type") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not task_id:
            raise ValueError("缺少任務 ID")

        removed = False
        if self.plugin.scheduler and self.plugin.scheduler.get_job(task_id):
            self.plugin.scheduler.remove_job(task_id)
            removed = True

        if task_type == "regular" and session_id:
            async with self.plugin.data_lock:
                sd = self.plugin.session_data.get(session_id)
                if isinstance(sd, dict):
                    sd.pop("next_trigger_time", None)
                await self.plugin._save_data()
        elif task_type in {"context", "context_orphan"}:
            removed = self._remove_context_task(session_id, task_id) or removed
            async with self.plugin.data_lock:
                sd = self.plugin.session_data.get(session_id)
                if isinstance(sd, dict):
                    pending = self.plugin._pending_context_tasks.get(session_id)
                    if pending:
                        sd["pending_context_tasks"] = pending
                    else:
                        sd.pop("pending_context_tasks", None)
                await self.plugin._save_data()
        elif task_type in {"auto_trigger", "group_idle"} and session_id:
            removed = self._cancel_timer_task(task_type, session_id) or removed

        if not removed:
            raise ValueError("找不到可刪除的任務")
        logger.info(f"{_LOG_TAG} Web 任務頁已刪除任務 {task_id}")
        return {"task_id": task_id}

    async def _run_now(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = self._resolve_session_from_payload(payload)
        asyncio.create_task(self.plugin.check_and_chat(session_id))
        logger.info(f"{_LOG_TAG} Web 任務頁已要求立即執行 {session_id}")
        return {"session_id": session_id}

    def _remove_context_task(self, session_id: str, task_id: str) -> bool:
        if not session_id or session_id not in self.plugin._pending_context_tasks:
            return False
        before = len(self.plugin._pending_context_tasks[session_id])
        self.plugin._pending_context_tasks[session_id] = [
            task
            for task in self.plugin._pending_context_tasks[session_id]
            if not isinstance(task, dict) or str(task.get("job_id", "")) != task_id
        ]
        if not self.plugin._pending_context_tasks[session_id]:
            self.plugin._pending_context_tasks.pop(session_id, None)
        return len(self.plugin._pending_context_tasks.get(session_id, [])) != before

    def _cancel_timer_task(self, task_type: str, session_id: str) -> bool:
        timers = {
            "auto_trigger": self.plugin.auto_trigger_timers,
            "group_idle": self.plugin.group_timers,
        }.get(task_type)
        if timers is None:
            return False
        handle = timers.pop(session_id, None)
        if handle is None:
            return False
        handle.cancel()
        return True

    def _resolve_session_from_payload(self, payload: dict[str, Any]) -> str:
        session_id = str(payload.get("session_id") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        message_type = str(payload.get("message_type") or MSG_TYPE_FRIEND).strip()

        if session_id:
            config = get_session_config(self.plugin.config, session_id)
            if config:
                return session_id
            parsed = parse_session_id(session_id)
            if parsed:
                target_id = parsed[2]
                message_type = parsed[1]
                preferred = parsed[0]
            else:
                target_id = session_id
                preferred = None
        else:
            preferred = None

        if not target_id:
            raise ValueError("缺少會話 ID")
        if "group" in message_type.lower():
            message_type = MSG_TYPE_GROUP
        elif "friend" in message_type.lower() or "private" in message_type.lower():
            message_type = MSG_TYPE_FRIEND

        resolved = resolve_full_umo(
            target_id,
            message_type,
            self.plugin.context.platform_manager,
            self.plugin.session_data,
            preferred,
        )
        if not get_session_config(self.plugin.config, resolved):
            raise ValueError("此會話未在插件配置中啟用")
        return resolved

    def _parse_run_date(self, payload: dict[str, Any]) -> datetime:
        delay = payload.get("delay_minutes")
        if delay not in (None, ""):
            try:
                minutes = float(delay)
            except (TypeError, ValueError) as exc:
                raise ValueError("延遲分鐘格式錯誤") from exc
            if minutes <= 0:
                raise ValueError("延遲分鐘必須大於 0")
            return datetime.fromtimestamp(
                time.time() + minutes * 60, tz=self.plugin.timezone
            )

        run_at = str(payload.get("run_at") or "").strip()
        if not run_at:
            raise ValueError("請填寫延遲分鐘或執行時間")
        try:
            value = datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise ValueError("執行時間格式錯誤") from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.plugin.timezone)
        else:
            value = value.astimezone(self.plugin.timezone)
        if value.timestamp() <= time.time():
            raise ValueError("執行時間必須晚於現在")
        return value

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
                    detail="等待條件成立後建立正式排程；改期後會轉為一般排程",
                    extra={"remaining_seconds": int(remaining) if remaining else 0},
                )
            )
        return result

    def _collect_sessions(self) -> list[dict[str, Any]]:
        sessions: dict[str, dict[str, Any]] = {}

        def add(session_id: str, session_config: dict | None, source: str) -> None:
            if not session_id:
                return
            parsed = parse_session_id(session_id)
            message_type = parsed[1] if parsed else ""
            target_id = parsed[2] if parsed else session_id
            sessions[session_id] = {
                "session_id": session_id,
                "label": get_session_log_str(
                    session_id, session_config, self.plugin.session_data
                ),
                "message_type": message_type,
                "target_id": target_id,
                "enabled": bool(session_config and session_config.get("enable", False)),
                "source": source,
            }

        for session_id in self.plugin.session_data:
            add(
                session_id,
                get_session_config(self.plugin.config, session_id),
                "session_data",
            )

        for key, message_type in (
            ("private_sessions", MSG_TYPE_FRIEND),
            ("group_sessions", MSG_TYPE_GROUP),
        ):
            for session_config in self.plugin.config.get(key, []):
                target_id = str(session_config.get("session_id") or "").strip()
                if not target_id:
                    continue
                parsed = parse_session_id(target_id)
                if parsed:
                    session_id = target_id
                else:
                    session_id = resolve_full_umo(
                        target_id,
                        message_type,
                        self.plugin.context.platform_manager,
                        self.plugin.session_data,
                    )
                add(session_id, session_config, key)

        for settings_key, message_type in (
            ("private_settings", MSG_TYPE_FRIEND),
            ("group_settings", MSG_TYPE_GROUP),
        ):
            settings = self.plugin.config.get(settings_key, {})
            for target_id in settings.get("session_list", []):
                target_id = str(target_id).strip()
                if not target_id:
                    continue
                parsed = parse_session_id(target_id)
                if parsed:
                    session_id = target_id
                else:
                    session_id = resolve_full_umo(
                        target_id,
                        message_type,
                        self.plugin.context.platform_manager,
                        self.plugin.session_data,
                    )
                add(session_id, get_session_config(self.plugin.config, session_id), settings_key)

        return sorted(sessions.values(), key=lambda item: item["label"])

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
