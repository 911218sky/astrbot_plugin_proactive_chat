from __future__ import annotations

import asyncio
import json
import random
import time
import zoneinfo
from datetime import datetime

import aiofiles
import aiofiles.os as aio_os
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core import chat_executor
from .core.auto_check import (
    clamp_auto_check_interval,
    clamp_future_trigger_time,
    compute_session_interval,
    resolve_auto_check_settings,
)
from .core.config import backup_configurations, get_session_config, validate_config
from .core.context_scheduling import (
    handle_context_aware_scheduling,
    restore_pending_context_tasks,
)
from .core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    AcceptedTurn,
    DeliveryCoordinatorRegistry,
    DispatchGate,
    GateVerdict,
    make_accepted_turn,
)
from .core.human_like import (
    apply_heat,
    compute_follow_up_delay_seconds,
    normalize_heat_score,
    resolve_human_like_settings,
)
from .core.immediate_follow_up import resolve_immediate_follow_up_settings
from .core.scheduler import (
    compute_habit_next_run,
    get_current_time_slot_id,
    get_time_slot_reset_count,
    is_unanswered_limit_reached,
)
from .core.state_store import ProactiveStateStore, StateStoreCorruptionError

# ── 核心模組匯入（使用相對匯入，避免與 AstrBot 自身的 core 衝突） ──
from .core.utils import (
    MSG_TYPE_FRIEND,
    MSG_TYPE_GROUP,
    MSG_TYPE_KEYWORD_FRIEND,
    get_session_log_str,
    is_group_session_id,
    is_private_session,
    is_quiet_time,
    parse_session_id,
    resolve_full_umo,
)

# 統一日誌前綴，方便在 AstrBot 日誌中篩選本插件的輸出
_LOG_TAG = "[主動訊息]"
_RESTORE_MISSED_GRACE_SECONDS = 30 * 60
_AUTO_TRIGGER_DEADLINE_KEY = "auto_trigger_deadline"
_GROUP_IDLE_DEADLINE_KEY = "group_idle_deadline"
_HABIT_TASK_PREFIX = "habit_"
_HABIT_LEARNING_KEY = "habit_learning"
_AUTO_HABIT_RULES_KEY = "auto_habit_rules"
_AUTO_HABIT_RULE_NAME = "自動學習：常聊天時段"
_AUTO_HABIT_MAX_OBSERVATIONS = 160


class ProactiveChatPlugin(star.Star):
    """
    主動訊息插件主類。

    繼承 AstrBot 的 ``star.Star``，透過裝飾器註冊事件處理器，
    並使用 APScheduler 管理定時主動聊天任務。

    核心流程：
    1. 使用者發送訊息 → 記錄時間、重設計時器
    2. 私聊：立即排定下一次主動訊息
    3. 群聊：等待群組沉默一段時間後才排定
    4. 定時觸發 ``check_and_chat`` → 檢查條件 → 呼叫 LLM → 發送訊息
    """

    # 使用 __slots__ 減少記憶體開銷（每個實例不再需要 __dict__）
    __slots__ = (
        "config",
        "scheduler",
        "timezone",
        "data_dir",
        "session_data_file",
        "state_store",
        "data_lock",
        "session_data",
        "group_timers",
        "last_bot_message_time",
        "session_temp_state",
        "last_message_times",
        "auto_trigger_timers",
        "plugin_start_time",
        "first_message_logged",
        "_cleanup_counter",
        "_pending_context_tasks",
        "_pending_habit_tasks",
        "_context_analysis_tasks",
        "_context_analysis_semaphore",
        "_ctx_task_counter",
        "_delivery_coordinators",
        "_reply_follow_up_tasks",
        "_inbound_debounce_tokens",
        "_chat_run_semaphore",
        "_history_save_lock",
        "page_api",
    )

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        # APScheduler 實例，在 initialize() 中啟動
        self.scheduler: AsyncIOScheduler | None = None
        # 使用者在 AstrBot 全域設定中指定的時區
        self.timezone: zoneinfo.ZoneInfo | None = None

        # ── 持久化路徑 ──
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"
        self.state_store = ProactiveStateStore(self.data_dir / "proactive_state.db")

        # 非同步鎖，保護 session_data 的讀寫
        self.data_lock: asyncio.Lock | None = None
        # 會話持久化數據：{ session_id: { unanswered_count, next_trigger_time, self_id, ... } }
        self.session_data: dict[str, dict] = {}

        # ── 計時器 ──
        # 群聊沉默倒計時：群組靜默 N 分鐘後觸發主動訊息
        self.group_timers: dict[str, asyncio.TimerHandle] = {}
        # 機器人最後一次發送訊息的時間戳（用於群聊節流）
        self.last_bot_message_time: float = 0.0
        # 群聊臨時狀態（用於追蹤最後使用者活動時間，定期清理過期條目）
        self.session_temp_state: dict[str, dict] = {}
        # 各會話最後收到訊息的時間戳
        self.last_message_times: dict[str, float] = {}
        # 自動觸發計時器：插件啟動後若會話無訊息，延遲 N 分鐘自動建立排程
        self.auto_trigger_timers: dict[str, asyncio.TimerHandle] = {}

        # 插件啟動時間，用於判斷「啟動後」的訊息
        self.plugin_start_time: float = time.time()
        # 已記錄首次訊息的會話集合（避免重複日誌）
        self.first_message_logged: set[str] = set()
        # 清理計數器：每處理 10 次 after_message_sent 就清理過期的 session_temp_state
        self._cleanup_counter: int = 0
        # 語境預測的待執行任務追蹤: { session_id: [ { job_id, reason, hint, ... }, ... ] }
        # 每個會話可同時存在多個語境任務（如短期跟進 + 長期早安問候）
        self._pending_context_tasks: dict[str, list[dict]] = {}
        # 習慣時段任務追蹤: { session_id: [ { job_id, reason, prompt, ... }, ... ] }
        self._pending_habit_tasks: dict[str, list[dict]] = {}
        # 語境分析背景任務：同一會話只保留最新一次，避免連續訊息時並發讀取 Core history。
        self._context_analysis_tasks: dict[str, asyncio.Task] = {}
        # 全域限流：避免多會話語境分析同時讀 AstrBot Core history，降低 SQLite 壓力尖峰。
        self._context_analysis_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)
        # 語境任務計數器，用於生成唯一 job_id
        self._ctx_task_counter: int = 0
        self._delivery_coordinators = DeliveryCoordinatorRegistry()
        self._reply_follow_up_tasks: dict[str, asyncio.Task] = {}
        self._inbound_debounce_tokens: dict[str, asyncio.Task] = {}
        # 全域限流：避免多個會話同時觸發主動訊息，一起打進 AstrBot agent/history 流程。
        self._chat_run_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)
        # 主動訊息寫回 AstrBot 主對話歷史時使用，避免本插件自己並發搶同一個 SQLite。
        self._history_save_lock: asyncio.Lock = asyncio.Lock()
        # AstrBot 官方插件 Pages API（新版 AstrBot 可用）
        self.page_api = None

        self._register_official_page_api_if_available()
        logger.info(f"{_LOG_TAG} 插件實例已創建。")

    def _register_official_page_api_if_available(self) -> None:
        """註冊 AstrBot 官方插件頁 API；舊版 AstrBot 不支援時自動跳過。"""
        if not hasattr(self.context, "register_web_api"):
            return

        try:
            from .core.page_api import PluginPageApi
        except Exception as e:
            logger.warning(f"{_LOG_TAG} 官方插件頁 API 不可用，已跳過註冊: {e}")
            return

        try:
            self.page_api = PluginPageApi(self)
            self.page_api.register_routes()
        except Exception as e:
            self.page_api = None
            logger.warning(
                f"{_LOG_TAG} 官方插件頁 API 註冊失敗，已跳過: {e}",
                exc_info=True,
            )

    # ═══════════════════════════════════════════════════════════
    #  數據持久化
    # ═══════════════════════════════════════════════════════════

    async def _load_data(self) -> None:
        """從插件自己的 SQLite DB 載入最新會話狀態。"""
        try:
            stored_data = await self.state_store.load_session_data()
            if stored_data is not None:
                self.session_data = stored_data
                return

            # 新 DB 尚未有資料時，讀一次舊 JSON 作為目前最新狀態，避免升級後任務清空。
            if not await aio_os.path.exists(str(self.session_data_file)):
                self.session_data = {}
                await self.state_store.save_session_data(self.session_data)
                return
            async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                content = await f.read()
            legacy_data = json.loads(content) if content.strip() else {}
            self.session_data = legacy_data if isinstance(legacy_data, dict) else {}
            await self.state_store.save_session_data(self.session_data)
            if self.session_data:
                logger.info(f"{_LOG_TAG} 已讀取既有 JSON 狀態並寫入插件 SQLite。")
        except StateStoreCorruptionError:
            self.session_data = {}
            raise
        except Exception as e:
            logger.error(f"{_LOG_TAG} 加載會話數據失敗: {e}")
            raise

    async def _save_data(self) -> None:
        """將最新會話狀態寫入插件自己的 SQLite DB。呼叫前須持有 data_lock。"""
        try:
            await self.state_store.save_session_data(self.session_data)
        except Exception as e:
            logger.error(f"{_LOG_TAG} 保存會話數據失敗: {e}")
            raise

    async def _persist_regular_job(
        self,
        session_id: str,
        run_at_ts: float,
        *,
        unanswered_count: int | None = None,
        clear_timer_keys: tuple[str, ...] = (),
    ) -> None:
        """保存一般主動訊息任務的下一次觸發時間。"""
        async with self.data_lock:
            sd = self.session_data.setdefault(session_id, {})
            if unanswered_count is not None:
                sd["unanswered_count"] = unanswered_count
            sd["next_trigger_time"] = run_at_ts
            for key in clear_timer_keys:
                sd.pop(key, None)
            await self._save_data()

    async def _set_timer_deadline(
        self, session_id: str, key: str, deadline_ts: float
    ) -> None:
        """保存等待型計時器的到期時間，讓重啟後可以恢復。"""
        async with self.data_lock:
            self.session_data.setdefault(session_id, {})[key] = deadline_ts
            await self._save_data()

    async def _clear_timer_state(self, session_id: str, *keys: str) -> None:
        """清除等待型計時器狀態。"""
        if not keys:
            return
        async with self.data_lock:
            sd = self.session_data.get(session_id)
            if not isinstance(sd, dict):
                return
            changed = False
            for key in keys:
                if key in sd:
                    sd.pop(key, None)
                    changed = True
            if changed:
                await self._save_data()

    async def _clear_timer_state_many(self, session_ids: set[str], *keys: str) -> None:
        """批次清除等待型計時器狀態。"""
        if not session_ids or not keys:
            return
        async with self.data_lock:
            changed = False
            for session_id in session_ids:
                sd = self.session_data.get(session_id)
                if not isinstance(sd, dict):
                    continue
                for key in keys:
                    if key in sd:
                        sd.pop(key, None)
                        changed = True
            if changed:
                await self._save_data()

    async def _clear_regular_job_state(
        self, session_id: str, *, clear_description: bool = False
    ) -> None:
        """清除一般排程的持久化狀態，再由呼叫端移除 scheduler job。"""
        async with self.data_lock:
            sd = self.session_data.get(session_id)
            if not isinstance(sd, dict):
                return
            changed = False
            if "next_trigger_time" in sd:
                sd.pop("next_trigger_time", None)
                changed = True
            if clear_description and "task_description" in sd:
                sd.pop("task_description", None)
                changed = True
            if changed:
                await self._save_data()

    async def _merge_session_state(
        self, old_session_id: str, new_session_id: str
    ) -> None:
        """合併平台前綴變更造成的同一會話狀態，避免重複排程。"""
        if old_session_id == new_session_id:
            return
        self._delivery_coordinators.merge_aliases(old_session_id, new_session_id)

        try:
            if self.scheduler and self.scheduler.get_job(old_session_id):
                self.scheduler.remove_job(old_session_id)
        except Exception as e:
            logger.debug(
                f"{_LOG_TAG} 移除舊平台排程失敗 | session={old_session_id}: {e}"
            )

        async with self.data_lock:
            old_sd = self.session_data.get(old_session_id)
            if not isinstance(old_sd, dict):
                return
            new_sd = self.session_data.setdefault(new_session_id, {})
            if not isinstance(new_sd, dict):
                new_sd = {}
                self.session_data[new_session_id] = new_sd

            for key, value in old_sd.items():
                if key in {
                    "unanswered_count",
                    "first_interaction_time",
                    "last_message_time",
                    "next_trigger_time",
                    _AUTO_TRIGGER_DEADLINE_KEY,
                    _GROUP_IDLE_DEADLINE_KEY,
                }:
                    old_value = self._coerce_timestamp(value)
                    new_value = self._coerce_timestamp(new_sd.get(key))
                    if key == "unanswered_count":
                        old_value = int(value or 0)
                        new_value = int(new_sd.get(key, 0) or 0)
                    if key == "first_interaction_time":
                        if new_value is None or (
                            old_value is not None and old_value < new_value
                        ):
                            new_sd[key] = value
                        continue
                    if new_value is None or (
                        old_value is not None and old_value > new_value
                    ):
                        new_sd[key] = value
                    continue
                if key == "pending_context_tasks":
                    merged = list(new_sd.get(key) or [])
                    seen = {
                        task.get("job_id") for task in merged if isinstance(task, dict)
                    }
                    for task in value or []:
                        if not isinstance(task, dict):
                            continue
                        job_id = task.get("job_id")
                        if job_id in seen:
                            continue
                        merged.append(task)
                        seen.add(job_id)
                    if merged:
                        new_sd[key] = merged
                    continue
                if key == "pending_habit_tasks":
                    merged = list(new_sd.get(key) or [])
                    seen = {
                        task.get("job_id") for task in merged if isinstance(task, dict)
                    }
                    for task in value or []:
                        if not isinstance(task, dict):
                            continue
                        job_id = task.get("job_id")
                        if job_id in seen:
                            continue
                        merged.append(task)
                        seen.add(job_id)
                    if merged:
                        new_sd[key] = merged
                    continue
                new_sd.setdefault(key, value)

            self.session_data.pop(old_session_id, None)
            await self._save_data()

        old_last = self.last_message_times.pop(old_session_id, 0)
        if old_last:
            self.last_message_times[new_session_id] = max(
                self.last_message_times.get(new_session_id, 0), old_last
            )

        old_tasks = self._pending_context_tasks.pop(old_session_id, [])
        if old_tasks:
            merged_tasks = self._pending_context_tasks.setdefault(new_session_id, [])
            seen_jobs = {
                task.get("job_id") for task in merged_tasks if isinstance(task, dict)
            }
            for task in old_tasks:
                if not isinstance(task, dict):
                    continue
                job_id = task.get("job_id")
                if job_id in seen_jobs:
                    continue
                merged_tasks.append(task)
                seen_jobs.add(job_id)

        old_habit_tasks = self._pending_habit_tasks.pop(old_session_id, [])
        if old_habit_tasks:
            merged_habit_tasks = self._pending_habit_tasks.setdefault(
                new_session_id, []
            )
            seen_jobs = {
                task.get("job_id")
                for task in merged_habit_tasks
                if isinstance(task, dict)
            }
            for task in old_habit_tasks:
                if not isinstance(task, dict):
                    continue
                job_id = task.get("job_id")
                if job_id in seen_jobs:
                    continue
                merged_habit_tasks.append(task)
                seen_jobs.add(job_id)

    # ═══════════════════════════════════════════════════════════
    #  生命週期
    # ═══════════════════════════════════════════════════════════

    async def initialize(self) -> None:
        """
        插件初始化入口（由 AstrBot 框架呼叫）。

        流程：備份配置 → 驗證配置 → 載入持久化數據 → 恢復訊息時間 →
              啟動調度器 → 恢復定時任務 → 設置自動觸發器。
        """
        self.data_lock = asyncio.Lock()

        # 備份使用者配置快照（方便除錯）
        await backup_configurations(self.config, self.data_dir)
        try:
            await validate_config(self.config)
        except Exception as e:
            logger.warning(f"{_LOG_TAG} 配置驗證發現問題: {e}，將繼續使用默認設置。")

        # 載入持久化的會話數據
        await self.state_store.initialize()
        async with self.data_lock:
            await self._load_data()
        logger.info(f"{_LOG_TAG} 已成功從插件 SQLite 加載會話數據。")

        # 從持久化數據恢復「最後訊息時間」到記憶體快取
        restored = 0
        now = time.time()
        for sid, info in self.session_data.items():
            if not isinstance(info, dict):
                continue
            ts = info.get("last_message_time")
            # 恢復合法時間戳；只過濾未來過多或無效數據。
            if isinstance(ts, (int, float)) and 0 < ts <= now + 60:
                self.last_message_times[sid] = ts
                restored += 1
        if restored:
            logger.info(f"{_LOG_TAG} 已從持久化數據恢復 {restored} 個會話的訊息時間。")

        # 解析 AstrBot 全域時區設定
        try:
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError):
            self.timezone = None

        # 啟動 APScheduler
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        # 恢復上次未完成的定時任務 & 設置自動觸發器
        await self._init_jobs_from_data()
        if restore_pending_context_tasks(self):
            async with self.data_lock:
                await self._save_data()
        await self._restore_pending_habit_tasks()
        await self._restore_waiting_timers_from_data()
        await self._setup_auto_triggers_for_enabled_sessions()
        await self._setup_habit_tasks_for_enabled_sessions()
        logger.info(f"{_LOG_TAG} 初始化完成。")

    async def terminate(self) -> None:
        """
        插件終止入口（由 AstrBot 框架呼叫）。

        取消所有計時器 → 關閉調度器 → 持久化數據。
        """
        for timer in self.group_timers.values():
            timer.cancel()
        self.group_timers.clear()

        for timer in self.auto_trigger_timers.values():
            timer.cancel()
        self.auto_trigger_timers.clear()

        if self._context_analysis_tasks:
            for task in self._context_analysis_tasks.values():
                task.cancel()
            await asyncio.gather(
                *self._context_analysis_tasks.values(), return_exceptions=True
            )
            self._context_analysis_tasks.clear()

        if self._reply_follow_up_tasks:
            for task in self._reply_follow_up_tasks.values():
                task.cancel()
            await asyncio.gather(
                *self._reply_follow_up_tasks.values(), return_exceptions=True
            )
            self._reply_follow_up_tasks.clear()
        self._inbound_debounce_tokens.clear()

        if self.scheduler and self.scheduler.running:
            try:
                for job in self.scheduler.get_jobs():
                    self.scheduler.remove_job(job.id)
                self.scheduler.shutdown()
            except Exception as e:
                logger.error(f"{_LOG_TAG} 關閉調度器時出錯: {e}")

        if self.data_lock:
            try:
                async with self.data_lock:
                    await self._save_data()
            except Exception as e:
                logger.error(f"{_LOG_TAG} 保存數據時出錯: {e}")

        try:
            await self.state_store.close()
        except Exception as e:
            logger.error(f"{_LOG_TAG} 關閉狀態資料庫時出錯: {e}")

        logger.info(f"{_LOG_TAG} 插件已終止。")

    # ═══════════════════════════════════════════════════════════
    #  調度核心
    # ═══════════════════════════════════════════════════════════

    def _add_scheduled_job(self, session_id: str, delay_seconds: int) -> datetime:
        """
        建立一次性 APScheduler 定時任務。

        Args:
            session_id: 會話的 unified_msg_origin
            delay_seconds: 延遲秒數

        Returns:
            排定的執行時間（含時區）
        """
        run_date = datetime.fromtimestamp(time.time() + delay_seconds, tz=self.timezone)
        self._add_scheduled_job_at(session_id, run_date)
        return run_date

    def _add_scheduled_job_at(self, session_id: str, run_date: datetime) -> datetime:
        """依指定時間建立一次性 APScheduler 定時任務。"""
        self.scheduler.add_job(
            self.check_and_chat,
            "date",
            run_date=run_date,
            args=[session_id],
            id=session_id,
            replace_existing=True,
            misfire_grace_time=60,
        )
        return run_date

    def _add_habit_job_at(
        self, session_id: str, job_id: str, run_date: datetime
    ) -> datetime:
        """依指定時間建立習慣時段一次性任務。"""
        self.scheduler.add_job(
            self.check_and_chat,
            "date",
            run_date=run_date,
            args=[session_id],
            kwargs={"ctx_job_id": job_id},
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120,
        )
        return run_date

    def _habit_settings(self, session_config: dict) -> dict:
        """取得會話的習慣時段配置。"""
        settings = session_config.get("habit_settings", {})
        return settings if isinstance(settings, dict) else {}

    def _effective_habit_settings(self, session_id: str, session_config: dict) -> dict:
        """合併手動設定與自動學習出的習慣時段規則。"""
        habit_conf = dict(self._habit_settings(session_config))
        allow_manual_rules = bool(habit_conf.get("allow_manual_habit_rules", True))
        if is_group_session_id(session_id):
            allow_manual_rules = True
        manual_rules = (
            [
                rule
                for rule in habit_conf.get("habit_rules", [])
                if isinstance(rule, dict)
            ]
            if allow_manual_rules
            else []
        )
        info = self.session_data.get(session_id)
        learned_rules: list[dict] = []
        if self._habit_learning_settings(habit_conf)["enable"] and isinstance(
            info, dict
        ):
            learning = info.get(_HABIT_LEARNING_KEY)
            if isinstance(learning, dict):
                learned_rules = [
                    rule
                    for rule in learning.get(_AUTO_HABIT_RULES_KEY, [])
                    if isinstance(rule, dict)
                ]

        habit_conf["habit_rules"] = [*manual_rules, *learned_rules]
        habit_conf["adaptive_timing"] = not is_group_session_id(session_id)
        if learned_rules:
            habit_conf["enable"] = True
        return habit_conf

    def _habit_learning_settings(self, habit_conf: dict) -> dict:
        """取得自動學習設定，並限制在合理範圍。"""
        return {
            "enable": bool(habit_conf.get("enable_auto_learning", True)),
            "min_samples": self._coerce_int(
                habit_conf.get("auto_learning_min_samples"), 5, 2, 80
            ),
            "history_days": self._coerce_int(
                habit_conf.get("auto_learning_history_days"), 30, 1, 365
            ),
            "cluster_window_minutes": self._coerce_int(
                habit_conf.get("auto_learning_cluster_window_minutes"), 120, 30, 360
            ),
            "appear_chance": self._coerce_float(
                habit_conf.get("auto_learning_appear_chance"), 0.75, 0.0, 1.0
            ),
        }

    def _build_auto_habit_rule(
        self,
        observations: list[dict],
        settings: dict,
        now_ts: float,
    ) -> dict | None:
        """依近期私聊時間樣本產生一條內部習慣規則。"""
        min_samples = int(settings["min_samples"])
        window = int(settings["cluster_window_minutes"])
        points: list[dict] = []
        for item in observations:
            if not isinstance(item, dict):
                continue
            ts = self._coerce_timestamp(item.get("ts"))
            minute = self._coerce_int(item.get("minute_of_day"), -1, -1, 1439)
            weekday = self._coerce_int(item.get("weekday"), 0, 0, 7)
            if ts is None or minute < 0 or weekday <= 0:
                continue
            points.append({"ts": ts, "minute": minute, "weekday": weekday})
        if len(points) < min_samples:
            return None

        best_cluster: list[dict] = []
        for anchor in points:
            cluster = []
            for item in points:
                diff = abs(int(item["minute"]) - int(anchor["minute"]))
                diff = min(diff, 1440 - diff)
                if diff <= window // 2:
                    cluster.append(item)
            if len(cluster) > len(best_cluster):
                best_cluster = cluster
        if len(best_cluster) < min_samples:
            return None

        minutes = sorted(int(item["minute"]) for item in best_cluster)
        median = minutes[len(minutes) // 2]
        half_width = max(45, min(120, window // 2))
        start_minute_total = (median - half_width) % 1440
        end_minute_total = (median + half_width) % 1440
        weekdays = sorted({int(item["weekday"]) for item in best_cluster})
        if start_minute_total > end_minute_total and median < end_minute_total:
            weekdays = [7 if day <= 1 else day - 1 for day in weekdays]
        days = ",".join(str(day) for day in weekdays)
        if len(weekdays) >= 5:
            days = ""

        updated_at = datetime.fromtimestamp(now_ts, tz=self.timezone).isoformat()
        return {
            "name": _AUTO_HABIT_RULE_NAME,
            "enable": True,
            "days": days,
            "start_hour": start_minute_total // 60,
            "start_minute": start_minute_total % 60,
            "end_hour": end_minute_total // 60,
            "end_minute": end_minute_total % 60,
            "appear_chance": float(settings["appear_chance"]),
            "jitter_min_minutes": 0,
            "jitter_max_minutes": 25,
            "oversleep_chance": 0.05,
            "oversleep_min_minutes": 10,
            "oversleep_max_minutes": 45,
            "message_hint": "根據最近私聊習慣，這個時段對方通常比較有空；像自然剛有空一樣打招呼，不要提到排程或學習規則。",
            "description": (
                f"由最近 {len(best_cluster)} 次私聊時間自動學習，更新於 {updated_at}。"
            ),
            "count_unanswered": False,
            "auto_learned": True,
            "sample_count": len(best_cluster),
            "updated_at": updated_at,
        }

    async def _record_private_habit_observation(
        self, session_id: str, session_config: dict, now_ts: float
    ) -> None:
        """記錄私聊發生時段，樣本足夠時自動寫入內部習慣規則。"""
        habit_conf = self._habit_settings(session_config)
        settings = self._habit_learning_settings(habit_conf)
        if not settings["enable"]:
            return

        current = datetime.fromtimestamp(now_ts, tz=self.timezone)
        cutoff = now_ts - int(settings["history_days"]) * 86400
        observation = {
            "ts": now_ts,
            "weekday": current.weekday() + 1,
            "minute_of_day": current.hour * 60 + current.minute,
        }
        learned_rule: dict | None = None
        learned_changed = False
        async with self.data_lock:
            sd = self.session_data.setdefault(session_id, {})
            learning = sd.setdefault(_HABIT_LEARNING_KEY, {})
            if not isinstance(learning, dict):
                learning = {}
                sd[_HABIT_LEARNING_KEY] = learning
            observations = [
                item
                for item in learning.get("observations", [])
                if isinstance(item, dict)
                and (self._coerce_timestamp(item.get("ts")) or 0) >= cutoff
            ]
            observations.append(observation)
            observations = observations[-_AUTO_HABIT_MAX_OBSERVATIONS:]
            learning["observations"] = observations

            learned_rule = self._build_auto_habit_rule(observations, settings, now_ts)
            if learned_rule is not None:
                old_rules = [
                    rule
                    for rule in learning.get(_AUTO_HABIT_RULES_KEY, [])
                    if isinstance(rule, dict)
                ]
                old_rule = old_rules[0] if old_rules else None
                comparable_keys = (
                    "days",
                    "start_hour",
                    "start_minute",
                    "end_hour",
                    "end_minute",
                    "appear_chance",
                )
                learned_changed = old_rule is None or any(
                    old_rule.get(key) != learned_rule.get(key)
                    for key in comparable_keys
                )
                learning[_AUTO_HABIT_RULES_KEY] = [learned_rule]
            await self._save_data()

        if learned_rule is not None and learned_changed:
            await self._reschedule_auto_habit_rule(session_id, session_config)

    async def _reschedule_auto_habit_rule(
        self, session_id: str, session_config: dict
    ) -> None:
        """自動學習規則變更後，重排同名習慣任務。"""
        removed = False
        for task in list(self._pending_habit_tasks.get(session_id, [])):
            if task.get("rule_name") != _AUTO_HABIT_RULE_NAME:
                continue
            job_id = str(task.get("job_id", ""))
            if self.scheduler and job_id:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
            await self._cleanup_habit_task(session_id, job_id)
            removed = True
        if removed or not self._pending_habit_tasks.get(session_id):
            logger.info(
                f"{_LOG_TAG} 已更新 {get_session_log_str(session_id, session_config, self.session_data)} "
                "的自動學習私聊習慣時段。"
            )
            await self._schedule_next_habit_task(session_id)

    async def _cleanup_auto_habit_rule_task(self, session_id: str) -> None:
        """自動學習關閉時移除已排定的自動習慣任務。"""
        for task in list(self._pending_habit_tasks.get(session_id, [])):
            if task.get("rule_name") != _AUTO_HABIT_RULE_NAME:
                continue
            job_id = str(task.get("job_id", ""))
            if self.scheduler and job_id:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
            await self._cleanup_habit_task(session_id, job_id)

    def _find_habit_task(self, session_id: str, job_id: str) -> dict | None:
        """根據 job_id 查找習慣時段任務。"""
        if not job_id:
            return None
        tasks = self._pending_habit_tasks.get(session_id, [])
        return next((task for task in tasks if task.get("job_id") == job_id), None)

    async def _schedule_next_habit_task(self, session_id: str) -> bool:
        """依習慣配置安排下一次習慣時段主動訊息。"""
        if not self.scheduler:
            return False
        session_config = get_session_config(self.config, session_id)
        if not session_config or not session_config.get("enable", False):
            return False
        habit_conf = self._effective_habit_settings(session_id, session_config)
        run_date, rule, reason = compute_habit_next_run(habit_conf, self.timezone)
        if run_date is None or rule is None:
            logger.debug(f"{_LOG_TAG} 習慣時段未安排 | session={session_id}: {reason}")
            return False

        if not is_group_session_id(session_id):
            try:
                if self.scheduler and self.scheduler.get_job(session_id):
                    self.scheduler.remove_job(session_id)
            except Exception as e:
                logger.debug(
                    f"{_LOG_TAG} 移除私聊一般排程失敗 | session={session_id}: {e}"
                )
            await self._clear_regular_job_state(session_id)

        rule_name = str(rule.get("name", "") or "習慣時段")
        job_id = f"{_HABIT_TASK_PREFIX}{session_id}_{int(run_date.timestamp())}"
        task_info = {
            "job_id": job_id,
            "type": "habit",
            "rule_name": rule_name,
            "reason": reason,
            "hint": str(rule.get("message_hint", "") or "").strip(),
            "description": str(rule.get("description", "") or "").strip(),
            "count_unanswered": bool(rule.get("count_unanswered", False)),
            "created_at": time.time(),
            "run_at": run_date.isoformat(),
        }

        async with self.data_lock:
            tasks = [
                task
                for task in self._pending_habit_tasks.get(session_id, [])
                if isinstance(task, dict)
                and str(task.get("rule_name", "")) != rule_name
            ]
            tasks.append(task_info)
            self._pending_habit_tasks[session_id] = tasks
            self.session_data.setdefault(session_id, {})["pending_habit_tasks"] = tasks
            await self._save_data()

        self._add_habit_job_at(session_id, job_id, run_date)
        logger.info(
            f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
            f"安排習慣時段「{rule_name}」，時間：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
        )
        return True

    async def _cleanup_habit_task(self, session_id: str, job_id: str) -> None:
        """清理已完成或失效的習慣時段任務。"""
        tasks = self._pending_habit_tasks.get(session_id)
        if not tasks:
            return
        remaining = [task for task in tasks if task.get("job_id") != job_id]
        if remaining:
            self._pending_habit_tasks[session_id] = remaining
        else:
            self._pending_habit_tasks.pop(session_id, None)

        async with self.data_lock:
            sd = self.session_data.get(session_id)
            if isinstance(sd, dict):
                if remaining:
                    sd["pending_habit_tasks"] = remaining
                else:
                    sd.pop("pending_habit_tasks", None)
                await self._save_data()

    async def _restore_pending_habit_tasks(self) -> None:
        """從持久化狀態恢復習慣時段任務。"""
        if not self.scheduler:
            return
        now = time.time()
        restored = 0
        stale: set[str] = set()
        for session_id, info in list(self.session_data.items()):
            if not isinstance(info, dict):
                continue
            tasks = [
                t for t in info.get("pending_habit_tasks", []) if isinstance(t, dict)
            ]
            if tasks:
                self._pending_habit_tasks[session_id] = tasks
            for task in tasks:
                job_id = str(task.get("job_id", ""))
                run_at = str(task.get("run_at", ""))
                if not job_id or not run_at:
                    continue
                try:
                    run_date = datetime.fromisoformat(run_at)
                except ValueError:
                    continue
                if run_date.timestamp() < now - _RESTORE_MISSED_GRACE_SECONDS:
                    stale.add(job_id)
                    continue
                if run_date.timestamp() < now:
                    run_date = datetime.fromtimestamp(now + 1, tz=self.timezone)
                    task["run_at"] = run_date.isoformat()
                self._add_habit_job_at(session_id, job_id, run_date)
                restored += 1

        if stale:
            for session_id, tasks in list(self._pending_habit_tasks.items()):
                self._pending_habit_tasks[session_id] = [
                    task for task in tasks if task.get("job_id") not in stale
                ]
                if not self._pending_habit_tasks[session_id]:
                    self._pending_habit_tasks.pop(session_id, None)
            async with self.data_lock:
                for session_id, info in self.session_data.items():
                    if not isinstance(info, dict):
                        continue
                    tasks = [
                        task
                        for task in info.get("pending_habit_tasks", [])
                        if isinstance(task, dict) and task.get("job_id") not in stale
                    ]
                    if tasks:
                        info["pending_habit_tasks"] = tasks
                    else:
                        info.pop("pending_habit_tasks", None)
                await self._save_data()

        if restored:
            logger.info(f"{_LOG_TAG} 已恢復 {restored} 個習慣時段任務。")

    async def _setup_habit_tasks_for_known_sessions(self) -> None:
        """為已知且啟用的會話補上習慣時段任務。"""
        count = 0
        for session_id, info in list(self.session_data.items()):
            if not isinstance(info, dict):
                continue
            session_config = get_session_config(self.config, session_id)
            if not session_config or not session_config.get("enable", False):
                continue
            habit_conf = self._effective_habit_settings(session_id, session_config)
            if not habit_conf.get("enable", False):
                continue
            if self._pending_habit_tasks.get(session_id):
                continue
            if await self._schedule_next_habit_task(session_id):
                count += 1
        if count:
            logger.info(f"{_LOG_TAG} 已為 {count} 個既有會話安排習慣時段任務。")

    async def _setup_habit_tasks_for_enabled_sessions(self) -> None:
        await self._setup_habit_tasks_for_known_sessions()
        processed: set[str] = set(self._pending_habit_tasks)
        count = 0
        for sessions_key, message_type in (
            ("private_sessions", MSG_TYPE_FRIEND),
            ("group_sessions", MSG_TYPE_GROUP),
        ):
            for settings in self.config.get(sessions_key, []):
                if not isinstance(settings, dict) or not settings.get("enable", False):
                    continue
                target_id = settings.get("session_id")
                if not target_id or str(target_id) in processed:
                    continue
                count += await self._setup_habit_task_for_config(
                    settings, message_type, str(target_id)
                )
                processed.add(str(target_id))

        for settings_key, message_type in (
            ("private_settings", MSG_TYPE_FRIEND),
            ("group_settings", MSG_TYPE_GROUP),
        ):
            settings = self.config.get(settings_key, {})
            if not isinstance(settings, dict) or not settings.get("enable", False):
                continue
            for target_id in settings.get("session_list", []):
                target = str(target_id)
                if target in processed:
                    continue
                count += await self._setup_habit_task_for_config(
                    settings, message_type, target
                )
                processed.add(target)
        if count:
            logger.info(f"{_LOG_TAG} 已為 {count} 個配置會話安排習慣時段任務。")

    async def _setup_habit_task_for_config(
        self, settings: dict, message_type: str, target_id: str
    ) -> int:
        habit_conf = settings.get("habit_settings", {})
        if not isinstance(habit_conf, dict) or not habit_conf.get("enable", False):
            return 0
        parsed = parse_session_id(target_id)
        preferred_platform = parsed[0] if parsed else None
        actual_type = parsed[1] if parsed else message_type
        actual_target = parsed[2] if parsed else target_id
        session_id = resolve_full_umo(
            actual_target,
            actual_type,
            self.context.platform_manager,
            self.session_data,
            preferred_platform,
        )
        if session_id in self._pending_habit_tasks:
            return 0
        return int(await self._schedule_next_habit_task(session_id))

    async def _create_auto_trigger_job(
        self,
        session_id: str,
        auto_minutes: int,
        *,
        ignore_start_grace: bool = False,
    ) -> None:
        """自動觸發等待到期後建立正式排程，並立即持久化。"""
        if session_id not in self.auto_trigger_timers:
            return
        try:
            cfg = get_session_config(self.config, session_id)
            if not cfg or not cfg.get("enable", False):
                await self._clear_timer_state(session_id, _AUTO_TRIGGER_DEADLINE_KEY)
                return
            if self.last_message_times.get(session_id, 0) != 0:
                await self._clear_timer_state(session_id, _AUTO_TRIGGER_DEADLINE_KEY)
                return
            if (
                not ignore_start_grace
                and time.time() - self.plugin_start_time < auto_minutes * 60
            ):
                return

            schedule_conf = cfg.get("schedule_settings", {})
            interval = compute_session_interval(schedule_conf, cfg, self.timezone, 0)
            run_date = datetime.fromtimestamp(time.time() + interval, tz=self.timezone)
            await self._persist_regular_job(
                session_id,
                run_date.timestamp(),
                unanswered_count=0,
                clear_timer_keys=(_AUTO_TRIGGER_DEADLINE_KEY,),
            )
            self._add_scheduled_job_at(session_id, run_date)
            logger.info(
                f"{_LOG_TAG} {get_session_log_str(session_id, cfg, self.session_data)} "
                f"自動觸發任務已創建，執行時間: {run_date.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        finally:
            self.auto_trigger_timers.pop(session_id, None)

    async def _retry_chat_job(
        self,
        session_id: str,
        ctx_job_id: str = "",
        delay_seconds: int = 15,
    ) -> None:
        """同一會話忙碌時延後一次性任務，避免 date job 被直接吃掉。"""
        if not self.scheduler:
            return
        run_date = datetime.fromtimestamp(time.time() + delay_seconds, tz=self.timezone)
        job_id = ctx_job_id or session_id
        async with self.data_lock:
            if ctx_job_id:
                pending_tasks = (
                    self._pending_habit_tasks.get(session_id, [])
                    if ctx_job_id.startswith(_HABIT_TASK_PREFIX)
                    else self._pending_context_tasks.get(session_id, [])
                )
                found = False
                for task in pending_tasks:
                    if (
                        isinstance(task, dict)
                        and str(task.get("job_id", "")) == ctx_job_id
                    ):
                        task["run_at"] = run_date.isoformat()
                        key = (
                            "pending_habit_tasks"
                            if ctx_job_id.startswith(_HABIT_TASK_PREFIX)
                            else "pending_context_tasks"
                        )
                        self.session_data.setdefault(session_id, {})[key] = (
                            pending_tasks
                        )
                        found = True
                        break
                if not found:
                    logger.warning(
                        f"{_LOG_TAG} 找不到特殊任務 {ctx_job_id}，略過重試排程。"
                    )
                    return
            else:
                session_info = self.session_data.setdefault(session_id, {})
                session_info["next_trigger_time"] = run_date.timestamp()
            await self._save_data()
        self.scheduler.add_job(
            self.check_and_chat,
            "date",
            run_date=run_date,
            args=[session_id],
            kwargs={"ctx_job_id": ctx_job_id} if ctx_job_id else {},
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120,
        )

    async def _schedule_next_chat_and_save(
        self,
        session_id: str,
        reset_counter: bool = False,
        clear_timer_keys: tuple[str, ...] = (),
        delay_minutes: int | None = None,
    ) -> None:
        """
        安排下一次主動聊天並持久化狀態。

        私聊自動查看使用受邊界限制的自適應間隔；群聊與舊設定仍可使用 schedule_rules。
        若 ``reset_counter=True``，會將未回覆計數歸零（通常在使用者回覆後呼叫）。
        """
        session_config = get_session_config(self.config, session_id)
        if not session_config:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with self.data_lock:
            sd = self.session_data.setdefault(session_id, {})
            current_slot_id = get_current_time_slot_id(schedule_conf, self.timezone)

            if reset_counter:
                sd["unanswered_count"] = 0
                sd["last_schedule_slot_id"] = current_slot_id
            else:
                # 檢查當前時段是否需要重置未回覆計數
                previous_slot_id = sd.get("last_schedule_slot_id")
                reset_count = get_time_slot_reset_count(schedule_conf, self.timezone)
                if reset_count is not None and previous_slot_id != current_slot_id:
                    old_count = sd.get("unanswered_count", 0)
                    sd["unanswered_count"] = reset_count
                    if old_count != reset_count:
                        logger.info(
                            f"{_LOG_TAG} 時段切換：未回覆計數從 {old_count} "
                            f"{'重置為' if reset_count == 0 else '調整為'} {reset_count}"
                        )
                sd["last_schedule_slot_id"] = current_slot_id

                limit_reached, reason = is_unanswered_limit_reached(
                    int(sd.get("unanswered_count", 0) or 0),
                    schedule_conf,
                    self.timezone,
                )
                if limit_reached:
                    sd.pop("next_trigger_time", None)
                    await self._save_data()
                    logger.info(
                        f"{_LOG_TAG} {get_session_log_str(session_id, session_config, self.session_data)} "
                        f"{reason}，不再安排下一次主動訊息。"
                    )
                    return

            # 計算加權隨機間隔
            unanswered_count = sd.get("unanswered_count", 0)
            if delay_minutes is not None:
                auto_settings = resolve_auto_check_settings(session_config)
                interval = clamp_auto_check_interval(
                    int(delay_minutes) * 60, auto_settings
                )
            else:
                interval = compute_session_interval(
                    schedule_conf,
                    session_config,
                    self.timezone,
                    int(unanswered_count or 0),
                )
            run_date = datetime.fromtimestamp(time.time() + interval, tz=self.timezone)
            # 持久化下次觸發時間（供重啟後恢復）
            sd["next_trigger_time"] = run_date.timestamp()
            for key in clear_timer_keys:
                sd.pop(key, None)
            await self._save_data()

            self._add_scheduled_job_at(session_id, run_date)

            logger.info(
                f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
                f"安排下一次主動訊息，時間：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
            )

    async def _is_chat_allowed(
        self,
        session_id: str,
        session_config: dict | None = None,
    ) -> bool:
        """
        檢查是否允許主動聊天。

        條件：會話配置存在且啟用 + 當前不在免打擾時段。
        可傳入已查詢的 ``session_config`` 避免重複查詢。
        """
        if session_config is None:
            session_config = get_session_config(self.config, session_id)
        if not session_config or not session_config.get("enable", False):
            return False
        quiet = session_config.get("schedule_settings", {}).get("quiet_hours", "1-7")
        if is_quiet_time(quiet, self.timezone):
            logger.info(f"{_LOG_TAG} 當前為免打擾時段。")
            return False
        return True

    def _canonical_delivery_session(self, session_id: str) -> str:
        parsed = parse_session_id(session_id)
        if not parsed:
            return session_id
        platform_id, message_type, target_id = parsed
        return resolve_full_umo(
            target_id,
            message_type,
            self.context.platform_manager,
            self.session_data,
            platform_id,
        )

    def _gate_verdict(self, gate: DispatchGate) -> GateVerdict:
        session_config = get_session_config(self.config, gate.canonical_session_id)
        enabled = bool(session_config and session_config.get("enable", False))
        quiet_hours = False
        if session_config:
            quiet = session_config.get("schedule_settings", {}).get(
                "quiet_hours", "1-7"
            )
            quiet_hours = is_quiet_time(quiet, self.timezone)
        return self._delivery_coordinators.verdict(
            gate,
            enabled=enabled,
            quiet_hours=quiet_hours,
        )

    def _cancel_reply_follow_up_task(self, session_id: str) -> None:
        task = self._reply_follow_up_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    @staticmethod
    def _accepted_turn_from_result(result: object) -> AcceptedTurn | None:
        chain = getattr(result, "chain", None)
        if not isinstance(chain, list):
            return None
        components = tuple(
            AcceptedComponent(AcceptedComponentKind.TEXT, text)
            for component in chain
            if isinstance(text := getattr(component, "text", None), str) and text
        )
        if not components:
            return None
        return make_accepted_turn(
            "".join(component.content for component in components),
            components,
            intended_components=len(components),
        )

    async def _run_reply_follow_ups(
        self,
        session_id: str,
        session_config: dict,
        gate: DispatchGate,
        accepted_turn: AcceptedTurn,
    ) -> None:
        task = asyncio.current_task()
        coordinator = self._delivery_coordinators.coordinator_for(session_id)
        try:
            async with coordinator.lease():
                await chat_executor.collect_follow_ups(
                    self,
                    session_id,
                    session_config,
                    gate,
                    (accepted_turn,),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                f"{_LOG_TAG} 一般回覆即時跟進失敗 | session={session_id}: {error}"
            )
        finally:
            if self._reply_follow_up_tasks.get(session_id) is task:
                self._reply_follow_up_tasks.pop(session_id, None)
            self._delivery_coordinators.retire(gate)

    async def _init_jobs_from_data(self) -> None:
        """
        從持久化數據恢復定時任務。

        遍歷 session_data，對每個仍在有效期內的 next_trigger_time
        重新建立 APScheduler 任務。錯過觸發時間但仍在寬限內的任務會立即補跑；
        太舊的任務會重新計算下一輪排程，避免重啟後在任務頁靜默消失。
        """
        restored = 0
        missed = 0
        rescheduled = 0
        now = time.time()
        needs_save = False

        # 清理非 dict 的無效條目
        invalid = [k for k, v in self.session_data.items() if not isinstance(v, dict)]
        if invalid:
            for k in invalid:
                del self.session_data[k]
            needs_save = True

        sessions_to_reschedule: list[str] = []
        for sid, info in list(self.session_data.items()):
            if not isinstance(info, dict):
                continue
            cfg = get_session_config(self.config, sid)
            if not cfg or not cfg.get("enable", False):
                continue
            schedule_conf = cfg.get("schedule_settings", {})
            unanswered_count = int(info.get("unanswered_count", 0) or 0)
            limit_reached, reason = is_unanswered_limit_reached(
                unanswered_count, schedule_conf, self.timezone
            )
            if limit_reached:
                if info.pop("next_trigger_time", None) is not None:
                    needs_save = True
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(sid, cfg, self.session_data)} "
                    f"{reason}，跳過恢復排程。"
                )
                continue
            raw_next_t = info.get("next_trigger_time")
            if not raw_next_t:
                continue
            try:
                next_t = float(raw_next_t)
            except (TypeError, ValueError):
                info.pop("next_trigger_time", None)
                needs_save = True
                continue

            auto_settings = resolve_auto_check_settings(cfg)
            parsed_session = parse_session_id(sid)
            is_private = bool(parsed_session and is_private_session(parsed_session[1]))
            bounded_next_t = (
                clamp_future_trigger_time(next_t, now, auto_settings)
                if is_private and auto_settings.enable
                else next_t
            )
            if bounded_next_t != next_t:
                next_t = bounded_next_t
                info["next_trigger_time"] = next_t
                needs_save = True

            # 避免重複建立
            if self.scheduler.get_job(sid):
                continue

            if next_t < now:
                if now - next_t <= _RESTORE_MISSED_GRACE_SECONDS:
                    next_t = now + 1
                    info["next_trigger_time"] = next_t
                    missed += 1
                    needs_save = True
                else:
                    info.pop("next_trigger_time", None)
                    sessions_to_reschedule.append(sid)
                    needs_save = True
                    continue

            try:
                run_date = datetime.fromtimestamp(next_t, tz=self.timezone)
                self.scheduler.add_job(
                    self.check_and_chat,
                    "date",
                    run_date=run_date,
                    args=[sid],
                    id=sid,
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                restored += 1
            except Exception as e:
                logger.error(f"{_LOG_TAG} 恢復任務失敗: {e}")

        if needs_save:
            async with self.data_lock:
                await self._save_data()

        for sid in sessions_to_reschedule:
            try:
                await self._schedule_next_chat_and_save(sid)
                rescheduled += 1
            except Exception as e:
                logger.error(f"{_LOG_TAG} 重排過期任務失敗 | session={sid}: {e}")

        logger.info(
            f"{_LOG_TAG} 任務恢復完成，共恢復 {restored} 個定時任務，"
            f"補跑 {missed} 個，重排 {rescheduled} 個。"
        )

    async def _restore_waiting_timers_from_data(self) -> None:
        """從持久化狀態恢復自動觸發與群聊沉默等待計時器。"""
        now = time.time()
        auto_restored = 0
        auto_missed = 0
        group_restored = 0
        group_missed = 0
        stale: dict[str, set[str]] = {}

        for sid, info in list(self.session_data.items()):
            if not isinstance(info, dict):
                continue
            cfg = get_session_config(self.config, sid)
            if not cfg or not cfg.get("enable", False):
                for key in (_AUTO_TRIGGER_DEADLINE_KEY, _GROUP_IDLE_DEADLINE_KEY):
                    if key in info:
                        stale.setdefault(sid, set()).add(key)
                continue

            auto_deadline = self._coerce_timestamp(info.get(_AUTO_TRIGGER_DEADLINE_KEY))
            if auto_deadline is not None:
                auto_settings = cfg.get("auto_trigger_settings", {})
                if (
                    auto_settings.get("enable_auto_trigger", False)
                    and self.last_message_times.get(sid, 0) == 0
                    and not info.get("next_trigger_time")
                ):
                    delay = max(0.0, auto_deadline - now)
                    if delay <= 0:
                        if now - auto_deadline <= _RESTORE_MISSED_GRACE_SECONDS:
                            await self._setup_auto_trigger(
                                sid,
                                silent=True,
                                delay_seconds=1.0,
                                ignore_start_grace=True,
                            )
                            auto_missed += 1
                        else:
                            stale.setdefault(sid, set()).add(_AUTO_TRIGGER_DEADLINE_KEY)
                    else:
                        await self._setup_auto_trigger(
                            sid, silent=True, delay_seconds=delay
                        )
                        auto_restored += 1
                else:
                    stale.setdefault(sid, set()).add(_AUTO_TRIGGER_DEADLINE_KEY)

            group_deadline = self._coerce_timestamp(info.get(_GROUP_IDLE_DEADLINE_KEY))
            if group_deadline is not None:
                if is_group_session_id(sid) and not info.get("next_trigger_time"):
                    delay = max(0.0, group_deadline - now)
                    if delay <= 0:
                        if now - group_deadline <= _RESTORE_MISSED_GRACE_SECONDS:
                            await self._setup_group_silence_timer(
                                sid, delay_seconds=1.0
                            )
                            group_missed += 1
                        else:
                            stale.setdefault(sid, set()).add(_GROUP_IDLE_DEADLINE_KEY)
                    else:
                        await self._setup_group_silence_timer(sid, delay_seconds=delay)
                        group_restored += 1
                else:
                    stale.setdefault(sid, set()).add(_GROUP_IDLE_DEADLINE_KEY)

        if stale:
            async with self.data_lock:
                changed = False
                for sid, keys in stale.items():
                    sd = self.session_data.get(sid)
                    if not isinstance(sd, dict):
                        continue
                    for key in keys:
                        if key in sd:
                            sd.pop(key, None)
                            changed = True
                if changed:
                    await self._save_data()

        if auto_restored or auto_missed or group_restored or group_missed:
            logger.info(
                f"{_LOG_TAG} 等待計時器恢復完成：自動觸發 {auto_restored} 個、"
                f"補跑 {auto_missed} 個；群聊沉默 {group_restored} 個、"
                f"補跑 {group_missed} 個。"
            )

    def _coerce_timestamp(self, value: object) -> float | None:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    def _coerce_int(
        self, value: object, default: int, min_value: int, max_value: int
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(min_value, min(max_value, parsed))

    def _coerce_float(
        self, value: object, default: float, min_value: float, max_value: float
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if parsed > 1 and max_value <= 1:
            parsed = parsed / 100
        return max(min_value, min(max_value, parsed))

    # ═══════════════════════════════════════════════════════════
    #  自動觸發
    #
    #  「自動觸發」是指：插件啟動後，若某個已啟用的會話在指定分鐘內
    #  沒有收到任何訊息，就自動為它建立一個主動訊息排程。
    #  這確保即使使用者從未主動發訊息，機器人也能開始主動聊天。
    # ═══════════════════════════════════════════════════════════

    def _cancel_timer(self, store: dict[str, asyncio.TimerHandle], key: str) -> None:
        """安全取消並移除指定計時器。若 key 不存在則靜默跳過。"""
        timer = store.pop(key, None)
        if timer is not None:
            timer.cancel()

    async def _cancel_all_related_auto_triggers(self, session_id: str) -> None:
        """
        取消與指定會話相關的所有自動觸發計時器。

        因為同一個 target_id 可能在不同平台上有不同的 session_id，
        所以需要比對 suffix 來找出所有相關的計時器。
        """
        parsed = parse_session_id(session_id)
        if not parsed:
            await self._clear_timer_state(session_id, _AUTO_TRIGGER_DEADLINE_KEY)
            self._cancel_timer(self.auto_trigger_timers, session_id)
            return

        _, _, target_id = parsed
        suffix = f":{target_id}"
        to_cancel = {
            sid
            for sid in set(self.auto_trigger_timers) | set(self.session_data)
            if sid.endswith(suffix) or sid == session_id
        }
        to_cancel.add(session_id)
        await self._clear_timer_state_many(to_cancel, _AUTO_TRIGGER_DEADLINE_KEY)
        for sid in to_cancel:
            self._cancel_timer(self.auto_trigger_timers, sid)

    async def _setup_auto_trigger(
        self,
        session_id: str,
        silent: bool = False,
        delay_seconds: float | None = None,
        ignore_start_grace: bool = False,
    ) -> None:
        """
        為單一會話設置自動觸發計時器。

        計時器到期時，若該會話仍未收到任何訊息，就建立主動訊息排程。
        ``silent=True`` 時不輸出設置日誌（批量設置時使用）。
        """
        session_config = get_session_config(self.config, session_id)
        if not session_config:
            return
        auto_settings = session_config.get("auto_trigger_settings", {})
        if not auto_settings.get("enable_auto_trigger", False):
            return

        auto_minutes = auto_settings.get("auto_trigger_after_minutes", 5)
        if auto_minutes <= 0:
            return
        delay = float(delay_seconds) if delay_seconds is not None else auto_minutes * 60
        if delay <= 0:
            delay = 1.0

        deadline_ts = time.time() + delay

        def _auto_trigger_callback(captured_sid: str = session_id) -> None:
            """計時器到期回調（在事件迴圈中同步執行）。"""
            try:
                # 若計時器已被外部取消（pop 掉了），則不執行
                if captured_sid not in self.auto_trigger_timers:
                    return
                asyncio.create_task(
                    self._create_auto_trigger_job(
                        captured_sid,
                        auto_minutes,
                        ignore_start_grace=ignore_start_grace,
                    )
                )
            except Exception as e:
                logger.error(f"{_LOG_TAG} 自動觸發回調失敗: {e}")

        try:
            await self._set_timer_deadline(
                session_id,
                _AUTO_TRIGGER_DEADLINE_KEY,
                deadline_ts,
            )
            # DB 已保存新 deadline 後再替換記憶體 timer。
            self._cancel_timer(self.auto_trigger_timers, session_id)
            loop = asyncio.get_running_loop()
            self.auto_trigger_timers[session_id] = loop.call_later(
                delay,
                _auto_trigger_callback,
            )
            if not silent:
                logger.info(
                    f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
                    f"設置自動觸發器，{delay / 60:.1f} 分鐘後檢查。"
                )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 設置自動觸發計時器失敗: {e}")
            raise

    async def _setup_auto_trigger_for_session_config(
        self,
        settings: dict,
        message_type: str,
        target_id: str,
        session_name: str = "",
    ) -> int:
        """
        根據會話配置為指定目標設置自動觸發器。

        Returns:
            1 表示成功設置，0 表示跳過。
        """
        type_desc = "私聊" if MSG_TYPE_KEYWORD_FRIEND in message_type else "群聊"
        log_str = f"{type_desc} {target_id}" + (
            f" ({session_name})" if session_name else ""
        )

        auto_settings = settings.get("auto_trigger_settings", {})
        if not auto_settings.get("enable_auto_trigger", False):
            return 0

        # 解析 target_id（可能本身就是完整 UMO 格式）
        parsed = parse_session_id(target_id)
        preferred_platform = parsed[0] if parsed else None
        real_message_type = parsed[1] if parsed else message_type
        real_target_id = parsed[2] if parsed else target_id
        suffix = f":{real_message_type}:{real_target_id}"

        # 若該會話已有尚未過期的持久化任務，則跳過（避免重複排程）
        now = time.time()
        for sid, info in self.session_data.items():
            if not isinstance(info, dict):
                continue
            if sid.endswith(suffix):
                if self.last_message_times.get(sid, 0) or info.get("last_message_time"):
                    logger.info(f"{_LOG_TAG} {log_str} 已有歷史訊息，跳過自動觸發。")
                    return 0
                limit_reached, reason = is_unanswered_limit_reached(
                    int(info.get("unanswered_count", 0) or 0),
                    settings.get("schedule_settings", {}),
                    self.timezone,
                )
                if limit_reached:
                    logger.info(f"{_LOG_TAG} {log_str} {reason}，跳過自動觸發。")
                    return 0
            if sid.endswith(suffix) and info.get("next_trigger_time"):
                try:
                    next_trigger_time = float(info["next_trigger_time"])
                except (TypeError, ValueError):
                    continue
                if now < next_trigger_time + 60:
                    logger.info(
                        f"{_LOG_TAG} {log_str} 已存在持久化任務，跳過自動觸發。"
                    )
                    return 0
            if sid.endswith(suffix) and sid in self.auto_trigger_timers:
                logger.info(
                    f"{_LOG_TAG} {log_str} 已存在自動觸發等待計時器，跳過重複設置。"
                )
                return 0
            if sid.endswith(suffix):
                deadline = self._coerce_timestamp(info.get(_AUTO_TRIGGER_DEADLINE_KEY))
                if deadline and now < deadline + 60:
                    logger.info(
                        f"{_LOG_TAG} {log_str} 已存在自動觸發等待狀態，跳過重複設置。"
                    )
                    return 0

        # 動態解析完整的 UMO（找到存活的平台）
        session_id = resolve_full_umo(
            real_target_id,
            real_message_type,
            self.context.platform_manager,
            self.session_data,
            preferred_platform,
        )
        auto_minutes = auto_settings.get("auto_trigger_after_minutes", 5)
        logger.info(
            f"{_LOG_TAG} 已為 {log_str} 設置自動觸發器，{auto_minutes} 分鐘後檢查。"
        )
        await self._setup_auto_trigger(session_id, silent=True)
        return 1

    async def _setup_auto_triggers_for_enabled_sessions(self) -> None:
        """
        遍歷所有已啟用的會話配置，為符合條件的會話設置自動觸發器。

        優先處理 private_sessions / group_sessions 中的個性化配置，
        再處理 private_settings / group_settings 中 session_list 的全域配置。
        使用 ``processed`` 集合避免重複設置。
        """
        logger.info(f"{_LOG_TAG} 開始檢查並設置自動觸發器...")
        count = 0
        processed: set[str] = set()

        # 1) 個性化會話配置（private_sessions / group_sessions）
        for sessions_key, msg_type in (
            ("private_sessions", MSG_TYPE_FRIEND),
            ("group_sessions", MSG_TYPE_GROUP),
        ):
            for sc in self.config.get(sessions_key, []):
                tid = sc.get("session_id")
                if tid and tid not in processed and sc.get("enable", False):
                    processed.add(tid)
                    count += await self._setup_auto_trigger_for_session_config(
                        sc,
                        msg_type,
                        tid,
                        sc.get("session_name", ""),
                    )

        # 2) 全域設定中的 session_list
        for settings_key, msg_type, sessions_key in (
            ("private_settings", MSG_TYPE_FRIEND, "private_sessions"),
            ("group_settings", MSG_TYPE_GROUP, "group_sessions"),
        ):
            settings = self.config.get(settings_key, {})
            sl = settings.get("session_list", [])
            if not settings.get("enable", False) or not sl:
                continue
            # 建立名稱查找表，用於日誌顯示
            sessions = self.config.get(sessions_key, [])
            name_map = {
                sc.get("session_id"): sc.get("session_name", "") for sc in sessions
            }
            for tid in sl:
                if tid not in processed:
                    processed.add(tid)
                    count += await self._setup_auto_trigger_for_session_config(
                        settings,
                        msg_type,
                        tid,
                        name_map.get(tid, ""),
                    )

        if count:
            logger.info(f"{_LOG_TAG} 已為 {count} 個會話設置自動觸發器。")
        else:
            logger.info(f"{_LOG_TAG} 沒有會話啟用自動觸發功能。")

    # ═══════════════════════════════════════════════════════════
    #  事件處理
    #
    #  私聊與群聊的訊息處理流程約 80% 相同，因此合併為
    #  ``_handle_message()``，透過 ``is_group`` 參數區分差異。
    # ═══════════════════════════════════════════════════════════

    def _context_analysis_delay(self, ctx_settings: dict) -> float:
        """取得語境分析防抖延遲秒數，預設讓 Core 先完成本輪 history 寫入。"""
        raw_value = ctx_settings.get("analysis_delay_seconds", 10)
        try:
            delay = float(raw_value)
        except (TypeError, ValueError):
            delay = 10.0
        return min(max(delay, 0.0), 120.0)

    async def _wait_for_inbound_quiet(
        self,
        session_id: str,
        session_config: dict,
        *,
        message_text: str = "",
        local_hour: int | None = None,
        random_value: float | None = None,
        sleep=asyncio.sleep,
    ) -> bool:
        settings = resolve_human_like_settings(session_config)
        if not settings.enable:
            return True
        if local_hour is None:
            local_hour = datetime.now(getattr(self, "timezone", None)).hour
        if random_value is None:
            random_value = random.random()
        human_delay_seconds = compute_follow_up_delay_seconds(
            message_text,
            local_hour,
            settings,
            random_value,
        )
        delay_seconds = max(settings.inbound_debounce_seconds, human_delay_seconds)
        if delay_seconds <= 0:
            return True

        if settings.inbound_debounce_seconds <= 0:
            await sleep(human_delay_seconds)
            return True

        token = asyncio.current_task()
        if token is None:
            return True
        self._inbound_debounce_tokens[session_id] = token
        try:
            await sleep(delay_seconds)
            return self._inbound_debounce_tokens.get(session_id) is token
        finally:
            if self._inbound_debounce_tokens.get(session_id) is token:
                self._inbound_debounce_tokens.pop(session_id, None)

    def _schedule_context_analysis(
        self,
        session_id: str,
        message_text: str,
        ctx_settings: dict,
        message_time: float,
    ) -> None:
        """延遲執行語境分析；同一會話的新訊息會取代尚未開始的舊任務。"""
        old_task = self._context_analysis_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(
            self._run_context_analysis_after_quiet(
                session_id,
                message_text,
                dict(ctx_settings),
                message_time,
            )
        )
        self._context_analysis_tasks[session_id] = task

    async def _run_context_analysis_after_quiet(
        self,
        session_id: str,
        message_text: str,
        ctx_settings: dict,
        message_time: float,
    ) -> None:
        """在會話短暫安靜後執行語境感知排程，降低 Core SQLite 競爭。"""
        try:
            delay = self._context_analysis_delay(ctx_settings)
            if delay > 0:
                await asyncio.sleep(delay)

            if self.last_message_times.get(session_id, 0.0) > message_time + 0.001:
                logger.debug(
                    f"{_LOG_TAG} 語境分析已有更新訊息，略過舊任務 | session={session_id}"
                )
                return

            async with self._context_analysis_semaphore:
                if self.last_message_times.get(session_id, 0.0) > message_time + 0.001:
                    logger.debug(
                        f"{_LOG_TAG} 語境分析等待期間已有更新訊息，略過舊任務"
                        f" | session={session_id}"
                    )
                    return
                await handle_context_aware_scheduling(
                    self, session_id, message_text, ctx_settings
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"{_LOG_TAG} 語境分析背景任務失敗 | session={session_id}: {e}")
        finally:
            current = asyncio.current_task()
            if self._context_analysis_tasks.get(session_id) is current:
                self._context_analysis_tasks.pop(session_id, None)

    async def _handle_message(self, event: AstrMessageEvent, *, is_group: bool) -> None:
        """
        私聊與群聊的共用訊息處理流程。

        步驟：
        1. 記錄 self_id（機器人自身 ID，供後續發送使用）
        2. 更新最後訊息時間戳
        3. 取消相關的自動觸發計時器（使用者已活躍，不需要自動觸發）
        4. 記錄首次訊息日誌
        5. 私聊：移除舊排程 → 立即安排下一次主動訊息
           群聊：移除舊排程 → 重設沉默倒計時 → 歸零未回覆計數
        6. 語境感知排程（背景執行，不阻塞訊息回覆流程）
        """
        if not event.get_messages():
            return

        alias_session_id = event.unified_msg_origin
        session_id = self._canonical_delivery_session(alias_session_id)
        cancel_follow_up = getattr(self, "_cancel_reply_follow_up_task", None)
        if callable(cancel_follow_up):
            cancel_follow_up(session_id)
        session_config = get_session_config(self.config, session_id)
        enabled = bool(session_config and session_config.get("enable", False))
        if enabled and not is_group:
            if not await self._wait_for_inbound_quiet(
                session_id,
                session_config,
                message_text=getattr(event, "message_str", "") or "",
            ):
                event.stop_event()
                return
        gate = self._delivery_coordinators.record_activity(alias_session_id, session_id)
        coordinator = self._delivery_coordinators.coordinator_for(session_id)
        try:
            async with coordinator.lease():
                now = time.time()

                # 記錄機器人自身 ID（用於構建模擬事件時的 self_id 欄位）
                self_id = event.get_self_id()
                self.last_message_times[session_id] = now
                if is_group:
                    # 群聊額外記錄臨時狀態（用於 after_message_sent 的過期清理）
                    self.session_temp_state[session_id] = {"last_user_time": now}

                # 先把使用者已回覆這件事落盤：舊任務不應在重啟後又被恢復。
                async with self.data_lock:
                    sd = self.session_data.setdefault(session_id, {})
                    if self_id:
                        sd["self_id"] = self_id
                    sd.setdefault("first_interaction_time", now)
                    if now >= self.plugin_start_time:
                        sd["last_message_time"] = now
                    if enabled:
                        sd["unanswered_count"] = 0
                        human_settings = resolve_human_like_settings(session_config)
                        follow_up_enabled = resolve_immediate_follow_up_settings(
                            session_config
                        ).enable
                        parsed_session = parse_session_id(session_id)
                        if (
                            (human_settings.enable or follow_up_enabled)
                            and parsed_session
                            and is_private_session(parsed_session[1])
                        ):
                            sd["interaction_heat"] = apply_heat(
                                normalize_heat_score(
                                    sd.get("interaction_heat"),
                                    human_settings.initial_heat_score,
                                ),
                                "user_activity",
                                human_settings,
                            )
                    await self._save_data()

                # 首次訊息日誌（每個會話只記錄一次）
                if enabled and session_id not in self.first_message_logged:
                    self.first_message_logged.add(session_id)
                    logger.info(
                        f"{_LOG_TAG} 已記錄 "
                        f"{get_session_log_str(session_id, session_config, self.session_data)} 的訊息時間。"
                    )

                if not enabled:
                    return

                # 使用者已活躍，取消自動觸發計時器並保存，避免重啟後又恢復等待任務。
                await self._cancel_all_related_auto_triggers(session_id)

                # 先提取語境感知所需的資訊，再進行排程操作
                ctx_settings = session_config.get("context_aware_settings", {})
                ctx_enabled = ctx_settings.get("enable", False)
                message_text = event.message_str or "" if ctx_enabled else ""

                if is_group:
                    # 群聊：重設沉默倒計時，等群組再次安靜後才排定主動訊息。
                    await self._reset_group_silence_timer(session_id)
                    await self._clear_regular_job_state(session_id)
                    try:
                        if self.scheduler and self.scheduler.get_job(session_id):
                            self.scheduler.remove_job(session_id)
                    except Exception as e:
                        logger.debug(
                            f"{_LOG_TAG} _handle_message 移除舊排程任務失敗"
                            f" | session={session_id}: {e}"
                        )
                else:
                    # 私聊：排程本身很輕量，直接保存可避免重啟時短暫丟失下一次任務。
                    await self._schedule_next_chat_and_save(
                        session_id, reset_counter=True
                    )
                    habit_conf = self._habit_settings(session_config)
                    if self._habit_learning_settings(habit_conf)["enable"]:
                        await self._record_private_habit_observation(
                            session_id, session_config, now
                        )
                    else:
                        await self._cleanup_auto_habit_rule_task(session_id)

                habit_settings = self._effective_habit_settings(
                    session_id, session_config
                )
                if habit_settings.get(
                    "enable", False
                ) and not self._pending_habit_tasks.get(session_id):
                    await self._schedule_next_habit_task(session_id)

                # 語境感知排程：在背景執行，避免阻塞訊息回覆流程
                if ctx_enabled:
                    self._schedule_context_analysis(
                        session_id, message_text, ctx_settings, now
                    )
        finally:
            self._delivery_coordinators.retire(gate)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=998)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        """私聊訊息事件處理器。priority=998 確保在大多數插件之前執行。"""
        try:
            await self._handle_message(event, is_group=False)
        except Exception as e:
            logger.error(f"{_LOG_TAG} 私聊訊息處理失敗: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊訊息事件處理器。"""
        try:
            await self._handle_message(event, is_group=True)
        except Exception as e:
            logger.error(f"{_LOG_TAG} 群聊訊息處理失敗: {e}")

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        session_id = event.unified_msg_origin
        result = event.get_result()

        if is_group_session_id(session_id):
            self._cleanup_counter += 1
            if self._cleanup_counter % 10 == 0:
                self._cleanup_expired_session_states(time.time())

        try:
            if is_group_session_id(session_id):
                await self._reset_group_silence_timer(session_id)
                self.session_temp_state.pop(session_id, None)
        except Exception as e:
            logger.error(f"{_LOG_TAG} after_message_sent 處理異常: {e}")

        if result is None:
            return
        is_llm_result = getattr(result, "is_llm_result", None)
        if not callable(is_llm_result) or not is_llm_result():
            return
        accepted_turn = self._accepted_turn_from_result(result)
        if accepted_turn is None:
            return

        canonical_session_id = self._canonical_delivery_session(session_id)
        self._cancel_reply_follow_up_task(canonical_session_id)
        gate = self._delivery_coordinators.record_activity(
            session_id, canonical_session_id
        )
        session_config = get_session_config(self.config, canonical_session_id)
        if not session_config or not session_config.get("enable", False):
            self._delivery_coordinators.retire(gate)
            return

        task = asyncio.create_task(
            self._run_reply_follow_ups(
                canonical_session_id,
                session_config,
                gate,
                accepted_turn,
            )
        )
        self._reply_follow_up_tasks[canonical_session_id] = task

    # ═══════════════════════════════════════════════════════════
    #  指令處理（指令組 /proactive）
    # ═══════════════════════════════════════════════════════════

    @filter.command_group("proactive")
    def proactive(self):
        """主動訊息管理指令組 /proactive"""

    @proactive.command("help")
    async def cmd_help(self, event: AstrMessageEvent) -> None:
        """顯示主動訊息插件的可用指令列表。"""
        yield event.plain_result(
            "📖 主動訊息插件指令一覽\n\n"
            "/proactive help — 顯示此幫助訊息\n"
            "/proactive tasks — 列出所有待執行的排程任務"
        )

    @proactive.command("tasks")
    async def cmd_list_pending_tasks(self, event: AstrMessageEvent) -> None:
        """列出當前所有待執行的主動訊息排程任務。"""
        now = datetime.now(self.timezone)
        lines: list[str] = [f"📋 待執行任務一覽（{now.strftime('%H:%M:%S')}）\n"]

        # ── 1. APScheduler 一般排程任務 ──
        scheduled_jobs = self.scheduler.get_jobs() if self.scheduler else []
        regular_jobs = [
            j
            for j in scheduled_jobs
            if not str(j.id).startswith(("ctx_", _HABIT_TASK_PREFIX))
        ]
        ctx_jobs = [j for j in scheduled_jobs if j.id.startswith("ctx_")]
        habit_jobs = [
            j for j in scheduled_jobs if str(j.id).startswith(_HABIT_TASK_PREFIX)
        ]

        lines.append(f"【一般排程】共 {len(regular_jobs)} 個")
        if regular_jobs:
            for job in regular_jobs:
                run_time = job.next_run_time
                time_str = run_time.strftime("%m/%d %H:%M:%S") if run_time else "未知"
                session_config = get_session_config(self.config, job.id)
                log_str = get_session_log_str(job.id, session_config, self.session_data)
                lines.append(f"  • {log_str} → {time_str}")
        else:
            lines.append("  （無）")

        # ── 2. 習慣時段任務 ──
        total_habit = sum(len(tasks) for tasks in self._pending_habit_tasks.values())
        lines.append(f"\n【習慣時段】共 {total_habit} 個")
        if self._pending_habit_tasks:
            for sid, tasks in self._pending_habit_tasks.items():
                session_config = get_session_config(self.config, sid)
                log_str = get_session_log_str(sid, session_config, self.session_data)
                for t in tasks:
                    run_at = t.get("run_at", "")
                    rule_name = t.get("rule_name", "習慣時段")
                    hint = t.get("hint", "")
                    try:
                        dt = datetime.fromisoformat(run_at)
                        time_str = dt.strftime("%m/%d %H:%M:%S")
                    except (ValueError, TypeError):
                        time_str = run_at or "未知"
                    lines.append(f"  • {log_str} → {time_str}")
                    lines.append(f"    規則: {rule_name} / 提示: {hint or '無'}")
        else:
            lines.append("  （無）")

        # ── 3. 語境預測任務 ──
        total_ctx = sum(len(tasks) for tasks in self._pending_context_tasks.values())
        lines.append(f"\n【語境預測】共 {total_ctx} 個")
        if self._pending_context_tasks:
            for sid, tasks in self._pending_context_tasks.items():
                session_config = get_session_config(self.config, sid)
                log_str = get_session_log_str(sid, session_config, self.session_data)
                for t in tasks:
                    run_at = t.get("run_at", "")
                    reason = t.get("reason", "")
                    hint = t.get("hint", "")
                    # 嘗試格式化時間
                    try:
                        dt = datetime.fromisoformat(run_at)
                        time_str = dt.strftime("%m/%d %H:%M:%S")
                    except (ValueError, TypeError):
                        time_str = run_at or "未知"
                    desc = reason or hint or "無描述"
                    lines.append(f"  • {log_str} → {time_str}")
                    lines.append(f"    原因: {desc}")
        else:
            lines.append("  （無）")

        # ── 4. APScheduler 中的特殊 job（補充顯示未被追蹤的） ──
        tracked_habit_ids = {
            t.get("job_id")
            for tasks in self._pending_habit_tasks.values()
            for t in tasks
        }
        orphan_habit = [j for j in habit_jobs if j.id not in tracked_habit_ids]
        if orphan_habit:
            lines.append(f"\n【未追蹤的習慣時段排程】共 {len(orphan_habit)} 個")
            for job in orphan_habit:
                run_time = job.next_run_time
                time_str = run_time.strftime("%m/%d %H:%M:%S") if run_time else "未知"
                lines.append(f"  • {job.id} → {time_str}")

        tracked_ids = {
            t.get("job_id")
            for tasks in self._pending_context_tasks.values()
            for t in tasks
        }
        orphan_ctx = [j for j in ctx_jobs if j.id not in tracked_ids]
        if orphan_ctx:
            lines.append(f"\n【未追蹤的語境排程】共 {len(orphan_ctx)} 個")
            for job in orphan_ctx:
                run_time = job.next_run_time
                time_str = run_time.strftime("%m/%d %H:%M:%S") if run_time else "未知"
                lines.append(f"  • {job.id} → {time_str}")

        yield event.plain_result("\n".join(lines))

    def _cleanup_expired_session_states(self, now: float) -> None:
        """清理超過 1 小時未活動的群聊臨時狀態。"""
        expired = [
            sid
            for sid, st in self.session_temp_state.items()
            if now - st.get("last_user_time", 0) > 3600
        ]
        for sid in expired:
            del self.session_temp_state[sid]

    async def _reset_group_silence_timer(self, session_id: str) -> None:
        """
        重設群聊沉默倒計時。

        當群組中有新訊息（使用者或機器人）時呼叫。
        取消舊計時器，建立新的 ``idle_minutes`` 分鐘倒計時。
        倒計時到期後，會建立主動訊息排程。
        """
        session_config = get_session_config(self.config, session_id)
        if not session_config or not session_config.get("enable", False):
            return

        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)
        await self._setup_group_silence_timer(
            session_id,
            delay_seconds=max(1.0, float(idle_minutes) * 60),
            idle_minutes=idle_minutes,
        )

    async def _setup_group_silence_timer(
        self,
        session_id: str,
        *,
        delay_seconds: float,
        idle_minutes: float | None = None,
    ) -> None:
        """設置群聊沉默等待計時器，並保存 deadline。"""
        session_config = get_session_config(self.config, session_id)
        if not session_config or not session_config.get("enable", False):
            return

        idle_minutes = (
            session_config.get("group_idle_trigger_minutes", 10)
            if idle_minutes is None
            else idle_minutes
        )
        deadline_ts = time.time() + delay_seconds

        def _schedule_callback(captured_sid: str = session_id) -> None:
            """沉默倒計時到期回調。"""
            try:
                # 若計時器已被外部取消，則不執行
                if captured_sid not in self.group_timers:
                    return
                # 確保 session_data 中有該會話的條目
                if captured_sid not in self.session_data:
                    self.session_data[captured_sid] = {"unanswered_count": 0}
                cfg = get_session_config(self.config, captured_sid)
                if not cfg or not cfg.get("enable", False):
                    return
                # 建立非同步任務來安排主動訊息
                asyncio.create_task(self._handle_group_silence_elapsed(captured_sid))
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(captured_sid, cfg, self.session_data)} "
                    f"已沉默 {idle_minutes} 分鐘，開始計劃主動訊息。"
                )
            except Exception as e:
                logger.error(f"{_LOG_TAG} 沉默倒計時回調失敗: {e}")

        try:
            await self._set_timer_deadline(
                session_id,
                _GROUP_IDLE_DEADLINE_KEY,
                deadline_ts,
            )
            self._cancel_timer(self.group_timers, session_id)
            loop = asyncio.get_running_loop()
            self.group_timers[session_id] = loop.call_later(
                delay_seconds, _schedule_callback
            )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 設置沉默倒計時失敗: {e}")
            raise

    async def _handle_group_silence_elapsed(self, session_id: str) -> None:
        """群聊沉默等待到期後轉成正式主動訊息排程。"""
        try:
            await self._schedule_next_chat_and_save(
                session_id,
                reset_counter=False,
                clear_timer_keys=(_GROUP_IDLE_DEADLINE_KEY,),
            )
        finally:
            self.group_timers.pop(session_id, None)

    # ═══════════════════════════════════════════════════════════
    #  核心執行：check_and_chat
    #
    #  委派至 core.chat_executor 模組，保持 main.py 精簡。
    # ═══════════════════════════════════════════════════════════

    async def check_and_chat(
        self,
        session_id: str,
        ctx_job_id: str = "",
        manual: bool = False,
    ) -> None:
        """由定時任務觸發的核心函數，委派至 core.chat_executor。"""
        alias_session_id = session_id
        resolved_session_id = chat_executor.resolve_session_umo(self, alias_session_id)
        if resolved_session_id is None:
            await self._schedule_next_chat_and_save(alias_session_id)
            return
        session_id = resolved_session_id
        coordinator = self._delivery_coordinators.merge_aliases(
            alias_session_id, session_id
        )
        gate = self._delivery_coordinators.snapshot(alias_session_id, session_id)
        if alias_session_id != session_id:
            await self._merge_session_state(alias_session_id, session_id)
        if coordinator.locked:
            if manual:
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(session_id, None, self.session_data)} "
                    "已有主動訊息流程執行中，略過本次手動立即執行。"
                )
                return
            logger.info(
                f"{_LOG_TAG} {get_session_log_str(session_id, None, self.session_data)} "
                "已有主動訊息流程執行中，延後本次任務。"
            )
            await self._retry_chat_job(session_id, ctx_job_id)
            return
        try:
            async with coordinator.lease():
                if self._chat_run_semaphore.locked():
                    if manual:
                        logger.info(
                            f"{_LOG_TAG} {get_session_log_str(session_id, None, self.session_data)} "
                            "目前已有其他主動訊息流程執行中，略過本次手動立即執行。"
                        )
                        return
                    logger.info(
                        f"{_LOG_TAG} {get_session_log_str(session_id, None, self.session_data)} "
                        "目前已有其他主動訊息流程執行中，延後本次任務。"
                    )
                    await self._retry_chat_job(session_id, ctx_job_id)
                    return
                async with self._chat_run_semaphore:
                    await chat_executor.check_and_chat(
                        self, session_id, ctx_job_id, gate=gate
                    )
        finally:
            self._delivery_coordinators.retire(gate)
