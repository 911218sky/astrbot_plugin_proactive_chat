# 文件名: main.py
# 版本: v2.0.0 — 模塊化重構 + schedule_rules 加權隨機調度
#
# 本檔案為 AstrBot 主動訊息插件的入口點。
# 負責：插件生命週期管理、事件監聽、定時任務調度、LLM 呼叫、訊息發送。
# 業務邏輯已拆分至 core/ 子模組（utils / config / scheduler / messaging）。

from __future__ import annotations

import asyncio
import json
import time
import traceback
import zoneinfo
from datetime import datetime

import aiofiles
import aiofiles.os as aio_os
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.platform import PlatformStatus

from .core.config import backup_configurations, get_session_config, validate_config
from .core.context_predictor import check_should_cancel_task, predict_proactive_timing
from .core.llm_helpers import (
    call_llm,
    recall_memories_for_proactive,
    safe_prepare_llm_request,
)
from .core.messaging import sanitize_history_content
from .core.scheduler import compute_weighted_interval, should_trigger_by_unanswered
from .core.send import send_proactive_message

# ── 核心模組匯入（使用相對匯入，避免與 AstrBot 自身的 core 衝突） ──
from .core.utils import (
    get_session_log_str,
    is_group_session_id,
    is_quiet_time,
    parse_session_id,
    resolve_full_umo,
)

# 統一日誌前綴，方便在 AstrBot 日誌中篩選本插件的輸出
_LOG_TAG = "[主動訊息]"


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
        "_ctx_task_counter",
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
        # 語境任務計數器，用於生成唯一 job_id
        self._ctx_task_counter: int = 0

        logger.info(f"{_LOG_TAG} 插件實例已創建。")

    # ═══════════════════════════════════════════════════════════
    #  數據持久化
    # ═══════════════════════════════════════════════════════════

    async def _load_data(self) -> None:
        """從 JSON 檔案載入會話持久化數據。若檔案不存在或損壞則初始化為空 dict。"""
        try:
            if await aio_os.path.exists(str(self.session_data_file)):
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    self.session_data = json.loads(content) if content.strip() else {}
            else:
                self.session_data = {}
        except Exception as e:
            logger.error(f"{_LOG_TAG} 加載會話數據失敗: {e}")
            self.session_data = {}

    async def _save_data(self) -> None:
        """將會話持久化數據寫入 JSON 檔案。呼叫前須持有 data_lock。"""
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                await f.write(
                    json.dumps(self.session_data, indent=2, ensure_ascii=False)
                )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 保存會話數據失敗: {e}")

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
        async with self.data_lock:
            await self._load_data()
        logger.info(f"{_LOG_TAG} 已成功從文件加載會話數據。")

        # 從持久化數據恢復「最後訊息時間」到記憶體快取
        restored = 0
        start = self.plugin_start_time
        for sid, info in self.session_data.items():
            if not isinstance(info, dict):
                continue
            ts = info.get("last_message_time")
            # 只恢復插件啟動後的時間戳（避免過期數據干擾自動觸發判斷）
            if isinstance(ts, (int, float)) and ts >= start:
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
        await self._restore_pending_context_tasks()
        await self._setup_auto_triggers_for_enabled_sessions()
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

    async def _schedule_next_chat_and_save(
        self,
        session_id: str,
        reset_counter: bool = False,
    ) -> None:
        """
        安排下一次主動聊天並持久化狀態。

        使用 ``compute_weighted_interval`` 根據 schedule_rules 加權隨機決定間隔。
        若 ``reset_counter=True``，會將未回覆計數歸零（通常在使用者回覆後呼叫）。
        """
        session_config = get_session_config(self.config, session_id)
        if not session_config:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with self.data_lock:
            if reset_counter:
                self.session_data.setdefault(session_id, {})["unanswered_count"] = 0

            # 計算加權隨機間隔
            interval = compute_weighted_interval(schedule_conf, self.timezone)
            run_date = self._add_scheduled_job(session_id, interval)

            # 持久化下次觸發時間（供重啟後恢復）
            self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                time.time() + interval
            )
            logger.info(
                f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
                f"安排下一次主動訊息，時間：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
            )
            await self._save_data()

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

    async def _init_jobs_from_data(self) -> None:
        """
        從持久化數據恢復定時任務。

        遍歷 session_data，對每個仍在有效期內的 next_trigger_time
        重新建立 APScheduler 任務。同時清理格式異常的條目。
        """
        restored = 0
        now = time.time()

        # 清理非 dict 的無效條目
        invalid = [k for k, v in self.session_data.items() if not isinstance(v, dict)]
        if invalid:
            for k in invalid:
                del self.session_data[k]
            async with self.data_lock:
                await self._save_data()

        for sid, info in self.session_data.items():
            cfg = get_session_config(self.config, sid)
            if not cfg or not cfg.get("enable", False):
                continue
            next_t = info.get("next_trigger_time")
            # 只恢復尚未過期（含 60 秒寬限）的任務
            if not next_t or now >= next_t + 60:
                continue
            # 避免重複建立
            if self.scheduler.get_job(sid):
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

        logger.info(f"{_LOG_TAG} 任務恢復完成，共恢復 {restored} 個定時任務。")

    async def _restore_pending_context_tasks(self) -> None:
        """從持久化的 session_data 中恢復語境預測的待執行任務。"""
        restored = 0
        now = time.time()
        for sid, info in self.session_data.items():
            if not isinstance(info, dict):
                continue
            # 相容舊格式（單一 dict）與新格式（list[dict]）
            raw = info.get("pending_context_tasks") or info.get("pending_context_task")
            if raw is None:
                continue
            task_list = raw if isinstance(raw, list) else [raw]
            valid_tasks: list[dict] = []
            for pending in task_list:
                if not isinstance(pending, dict):
                    continue
                run_at_str = pending.get("run_at", "")
                if run_at_str:
                    try:
                        run_at_dt = datetime.fromisoformat(run_at_str)
                        if run_at_dt.timestamp() < now:
                            continue  # 任務已過期，跳過
                    except (ValueError, TypeError):
                        continue
                valid_tasks.append(pending)
                restored += 1
            if valid_tasks:
                self._pending_context_tasks[sid] = valid_tasks
            # 清理舊格式的持久化 key
            info.pop("pending_context_task", None)
            if valid_tasks:
                info["pending_context_tasks"] = valid_tasks
            else:
                info.pop("pending_context_tasks", None)
        if restored:
            logger.info(f"{_LOG_TAG} 已恢復 {restored} 個語境預測的待執行任務。")

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
            self._cancel_timer(self.auto_trigger_timers, session_id)
            return

        _, _, target_id = parsed
        suffix = f":{target_id}"
        to_cancel = [
            sid
            for sid in self.auto_trigger_timers
            if sid.endswith(suffix) or sid == session_id
        ]
        for sid in to_cancel:
            self._cancel_timer(self.auto_trigger_timers, sid)

    async def _setup_auto_trigger(self, session_id: str, silent: bool = False) -> None:
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

        # 先取消舊的計時器（避免重複）
        self._cancel_timer(self.auto_trigger_timers, session_id)

        def _auto_trigger_callback(captured_sid: str = session_id) -> None:
            """計時器到期回調（在事件迴圈中同步執行）。"""
            try:
                # 若計時器已被外部取消（pop 掉了），則不執行
                if captured_sid not in self.auto_trigger_timers:
                    return
                cfg = get_session_config(self.config, captured_sid)
                if not cfg or not cfg.get("enable", False):
                    return
                # 條件：該會話從未收到訊息 且 插件已運行超過指定分鐘
                if self.last_message_times.get(captured_sid, 0) == 0 and (
                    time.time() - self.plugin_start_time >= auto_minutes * 60
                ):
                    schedule_conf = cfg.get("schedule_settings", {})
                    interval = compute_weighted_interval(schedule_conf, self.timezone)
                    run_date = self._add_scheduled_job(captured_sid, interval)
                    logger.info(
                        f"{_LOG_TAG} {get_session_log_str(captured_sid, cfg, self.session_data)} "
                        f"自動觸發任務已創建，執行時間: {run_date.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    # 任務已建立，移除計時器引用
                    self.auto_trigger_timers.pop(captured_sid, None)
            except Exception as e:
                logger.error(f"{_LOG_TAG} 自動觸發回調失敗: {e}")

        try:
            loop = asyncio.get_running_loop()
            self.auto_trigger_timers[session_id] = loop.call_later(
                auto_minutes * 60,
                _auto_trigger_callback,
            )
            if not silent:
                logger.info(
                    f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
                    f"設置自動觸發器，{auto_minutes} 分鐘後檢查。"
                )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 設置自動觸發計時器失敗: {e}")

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
        type_desc = "私聊" if "Friend" in message_type else "群聊"
        log_str = f"{type_desc} {target_id}" + (
            f" ({session_name})" if session_name else ""
        )

        auto_settings = settings.get("auto_trigger_settings", {})
        if not auto_settings.get("enable_auto_trigger", False):
            return 0

        # 若該會話已有尚未過期的持久化任務，則跳過（避免重複排程）
        now = time.time()
        suffix = f":{message_type}:{target_id}"
        for sid, info in self.session_data.items():
            if sid.endswith(suffix) and info.get("next_trigger_time"):
                if now < info["next_trigger_time"] + 60:
                    logger.info(
                        f"{_LOG_TAG} {log_str} 已存在持久化任務，跳過自動觸發。"
                    )
                    return 0

        # 解析 target_id（可能本身就是完整 UMO 格式）
        parsed = parse_session_id(target_id)
        preferred_platform = parsed[0] if parsed else None
        real_target_id = parsed[2] if parsed else target_id

        # 動態解析完整的 UMO（找到存活的平台）
        session_id = resolve_full_umo(
            real_target_id,
            message_type,
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
            ("private_sessions", "FriendMessage"),
            ("group_sessions", "GroupMessage"),
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
            ("private_settings", "FriendMessage", "private_sessions"),
            ("group_settings", "GroupMessage", "group_sessions"),
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
        """
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        now = time.time()

        # 記錄機器人自身 ID（用於構建模擬事件時的 self_id 欄位）
        self_id = event.get_self_id()
        if self_id:
            async with self.data_lock:
                self.session_data.setdefault(session_id, {})["self_id"] = self_id

        # 更新時間戳
        self.last_message_times[session_id] = now
        if is_group:
            # 群聊額外記錄臨時狀態（用於 after_message_sent 的過期清理）
            self.session_temp_state[session_id] = {"last_user_time": now}

        # 持久化最後訊息時間
        async with self.data_lock:
            if now >= self.plugin_start_time:
                self.session_data.setdefault(session_id, {})["last_message_time"] = now

        # 使用者已活躍，取消自動觸發計時器
        await self._cancel_all_related_auto_triggers(session_id)

        # 首次訊息日誌（每個會話只記錄一次）
        session_config = get_session_config(self.config, session_id)
        enabled = session_config and session_config.get("enable", False)
        if enabled and session_id not in self.first_message_logged:
            self.first_message_logged.add(session_id)
            logger.info(
                f"{_LOG_TAG} 已記錄 "
                f"{get_session_log_str(session_id, session_config, self.session_data)} 的訊息時間。"
            )

        if not enabled:
            return

        # 移除現有的定時任務（使用者回覆後需要重新計算間隔）
        try:
            self.scheduler.remove_job(session_id)
        except Exception:
            pass

        if is_group:
            # 群聊：重設沉默倒計時，等群組再次安靜後才排定主動訊息
            await self._reset_group_silence_timer(session_id)
            async with self.data_lock:
                sd = self.session_data.get(session_id)
                if sd:
                    sd["unanswered_count"] = 0
                    sd.pop("next_trigger_time", None)
        else:
            # 私聊：立即安排下一次主動訊息，並歸零未回覆計數
            await self._schedule_next_chat_and_save(session_id, reset_counter=True)

        # 語境感知排程：在背景執行，避免阻塞訊息處理流程
        ctx_settings = session_config.get("context_aware_settings", {})
        if ctx_settings.get("enable", False):
            message_text = event.message_str or ""
            asyncio.create_task(
                self._handle_context_aware_scheduling(
                    session_id, message_text, ctx_settings
                )
            )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=998)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        """私聊訊息事件處理器。priority=998 確保在大多數插件之前執行。"""
        await self._handle_message(event, is_group=False)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊訊息事件處理器。"""
        await self._handle_message(event, is_group=True)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """
        機器人發送訊息後的回調（僅處理群聊）。

        用途：機器人回覆群聊訊息後，重設沉默倒計時，
        確保從「最後一條訊息」開始計算沉默時間。
        同時定期清理過期的 session_temp_state。
        """
        session_id = event.unified_msg_origin
        if not is_group_session_id(session_id):
            return

        # 每 10 次清理一次過期的臨時狀態（避免記憶體洩漏）
        self._cleanup_counter += 1
        if self._cleanup_counter % 10 == 0:
            self._cleanup_expired_session_states(time.time())

        try:
            await self._reset_group_silence_timer(session_id)
            self.session_temp_state.pop(session_id, None)
        except Exception as e:
            logger.error(f"{_LOG_TAG} after_message_sent 處理異常: {e}")

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

        # 取消舊的沉默計時器
        self._cancel_timer(self.group_timers, session_id)
        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)

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
                asyncio.create_task(
                    self._schedule_next_chat_and_save(captured_sid, reset_counter=False)
                )
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(captured_sid, cfg, self.session_data)} "
                    f"已沉默 {idle_minutes} 分鐘，開始計劃主動訊息。"
                )
            except Exception as e:
                logger.error(f"{_LOG_TAG} 沉默倒計時回調失敗: {e}")

        try:
            loop = asyncio.get_running_loop()
            self.group_timers[session_id] = loop.call_later(
                idle_minutes * 60, _schedule_callback
            )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 設置沉默倒計時失敗: {e}")

    # ═══════════════════════════════════════════════════════════
    #  語境感知排程
    #
    #  利用 LLM 根據對話語境預測最佳的主動訊息觸發時機。
    #  使用插件自帶的 APScheduler 管理排程，觸發時走 check_and_chat
    #  流程，確保所有業務邏輯（免打擾、衰減、一致性檢查等）生效。
    # ═══════════════════════════════════════════════════════════

    async def _handle_context_aware_scheduling(
        self,
        session_id: str,
        message_text: str,
        ctx_settings: dict,
    ) -> None:
        """
        背景任務：檢查待執行的語境任務並執行 LLM 預測。

        步驟：
        1. 若存在待執行的語境任務，詢問 LLM 是否應該取消
        2. 根據最新訊息執行 LLM 時機預測
        3. 若預測結果建議排程，建立一次性任務
        """
        try:
            # 步驟 1：檢查待執行的語境任務是否應該取消
            cancelled_reason = await self._maybe_cancel_pending_context_task(
                session_id, message_text
            )

            # 步驟 2：取得對話歷史用於語境分析
            history = await self._get_history_for_prediction(session_id)

            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")

            # 步驟 3：呼叫 LLM 預測時機（若剛取消了任務，傳入原因讓 LLM 知道語境已轉移）
            prediction = await predict_proactive_timing(
                context=self.context,
                session_id=session_id,
                last_message=message_text,
                history=history,
                current_time_str=now_str,
                config=ctx_settings,
                just_cancelled_reason=cancelled_reason,
                llm_provider_id=ctx_settings.get("llm_provider_id", ""),
                extra_prompt=ctx_settings.get("extra_prompt", ""),
            )

            session_config = get_session_config(self.config, session_id)
            log_name = get_session_log_str(
                session_id, session_config, self.session_data
            )

            if not prediction or not prediction.get("should_schedule"):
                logger.info(
                    f"{_LOG_TAG} {log_name} "
                    f"語境分析完成，LLM 判定目前不需要排程主動訊息。"
                )
                return

            delay_minutes = prediction.get("delay_minutes", 60)
            reason = prediction.get("reason", "")
            hint = prediction.get("message_hint", "")

            run_at = datetime.fromtimestamp(
                time.time() + delay_minutes * 60, tz=self.timezone
            )
            logger.info(
                f"{_LOG_TAG} {log_name} "
                f"語境分析完成，LLM 判定需要排程主動訊息，"
                f"預計觸發時間 {run_at.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(+{delay_minutes}分鐘，原因: {reason})"
            )

            # 步驟 4：建立排程任務
            await self._create_context_predicted_task(
                session_id=session_id,
                delay_minutes=delay_minutes,
                reason=reason,
                hint=hint,
            )

        except Exception as e:
            logger.error(f"{_LOG_TAG} 語境感知排程失敗: {e}")

    async def _maybe_cancel_pending_context_task(
        self,
        session_id: str,
        message_text: str,
    ) -> str:
        """若用戶的新訊息使待執行的語境任務不再需要，則取消該任務。

        遍歷該會話所有待執行的語境任務，逐一詢問 LLM 是否應取消。

        Returns:
            被取消任務的原因字串（多個以分號分隔），未取消則回傳空字串。
        """
        task_list = self._pending_context_tasks.get(session_id)
        if not task_list:
            return ""

        # 從會話配置中取得語境感知的 LLM 平台 ID
        session_config = get_session_config(self.config, session_id)
        ctx_llm_id = ""
        if session_config:
            ctx_llm_id = session_config.get("context_aware_settings", {}).get(
                "llm_provider_id", ""
            )

        cancelled_reasons: list[str] = []
        to_remove: list[dict] = []

        for task in task_list:
            job_id = task.get("job_id", "")
            task_reason = task.get("reason", "")
            task_hint = task.get("hint", "")

            should_cancel = await check_should_cancel_task(
                context=self.context,
                session_id=session_id,
                last_message=message_text,
                task_reason=task_reason,
                task_hint=task_hint,
                llm_provider_id=ctx_llm_id,
            )

            if should_cancel:
                to_remove.append(task)
                cancelled_reasons.append(task_reason)
                logger.info(
                    f"{_LOG_TAG} 已取消 "
                    f"{get_session_log_str(session_id, None, self.session_data)} "
                    f"的語境預測任務 ({job_id})：用戶新訊息使其不再需要。"
                )

        # 批次移除被取消的任務
        for task in to_remove:
            job_id = task.get("job_id", "")
            try:
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
            except Exception:
                pass
            task_list.remove(task)

        # 清理空列表
        if not task_list:
            self._pending_context_tasks.pop(session_id, None)

        # 更新持久化
        if to_remove:
            async with self.data_lock:
                sd = self.session_data.get(session_id)
                if sd:
                    if task_list:
                        sd["pending_context_tasks"] = task_list
                    else:
                        sd.pop("pending_context_tasks", None)
                        sd.pop("pending_context_task", None)
                    await self._save_data()

        return "; ".join(cancelled_reasons)

    async def _remove_context_predicted_task(
        self, session_id: str, job_id: str
    ) -> None:
        """從本地排程器和追蹤中移除指定的語境預測任務。"""
        task_list = self._pending_context_tasks.get(session_id)
        if task_list:
            self._pending_context_tasks[session_id] = [
                t for t in task_list if t.get("job_id") != job_id
            ]
            if not self._pending_context_tasks[session_id]:
                self._pending_context_tasks.pop(session_id, None)

        try:
            if job_id and self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        except Exception:
            pass

    async def _create_context_predicted_task(
        self,
        *,
        session_id: str,
        delay_minutes: int,
        reason: str,
        hint: str,
    ) -> None:
        """
        根據 LLM 預測結果建立一次性排程任務。

        支援同一會話同時存在多個語境任務（如短期跟進 + 長期早安問候），
        每個任務使用唯一的 job_id。
        """
        run_at = datetime.fromtimestamp(
            time.time() + delay_minutes * 60, tz=self.timezone
        )

        # 生成唯一 job_id
        self._ctx_task_counter += 1
        ctx_job_id = f"ctx_{session_id}_{self._ctx_task_counter}"

        self.scheduler.add_job(
            self.check_and_chat,
            "date",
            run_date=run_at,
            args=[session_id],
            kwargs={"ctx_job_id": ctx_job_id},
            id=ctx_job_id,
            replace_existing=True,
            misfire_grace_time=120,
        )

        session_config = get_session_config(self.config, session_id)
        logger.info(
            f"{_LOG_TAG} 已為 "
            f"{get_session_log_str(session_id, session_config, self.session_data)} "
            f"建立語境預測排程，"
            f"觸發時間 {run_at.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(+{delay_minutes}分鐘，原因: {reason})"
        )

        # 追蹤待執行任務（追加到列表）
        task_info = {
            "job_id": ctx_job_id,
            "reason": reason,
            "hint": hint,
            "delay_minutes": delay_minutes,
            "created_at": time.time(),
            "run_at": run_at.isoformat(),
        }
        task_list = self._pending_context_tasks.setdefault(session_id, [])
        task_list.append(task_info)

        # 持久化到 session_data
        async with self.data_lock:
            sd = self.session_data.setdefault(session_id, {})
            sd["pending_context_tasks"] = task_list
            sd.pop("pending_context_task", None)  # 清理舊格式
            await self._save_data()

    async def _get_history_for_prediction(self, session_id: str) -> list:
        """取得最近的對話歷史，用於語境預測。"""
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            if not conv_id:
                return []
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, conv_id
            )
            if not conversation or not conversation.history:
                return []
            history = (
                json.loads(conversation.history)
                if isinstance(conversation.history, str)
                else conversation.history
            )
            return sanitize_history_content(history) if history else []
        except Exception as e:
            logger.debug(f"{_LOG_TAG} 取得預測用歷史記錄失敗: {e}")
            return []

    async def _finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ) -> None:
        """
        主動訊息發送成功後的收尾工作。

        1. 將本次對話（prompt + response）存入對話歷史
        2. 遞增未回覆計數
        3. 私聊：安排下一次主動訊息（群聊由沉默計時器處理）
        4. 持久化數據
        """
        # 存檔對話歷史
        try:
            await self.context.conversation_manager.add_message_pair(
                cid=conv_id,
                user_message=UserMessageSegment(content=[TextPart(text=user_prompt)]),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_response)]
                ),
            )
        except Exception as e:
            logger.error(f"{_LOG_TAG} 存檔對話歷史失敗: {e}")

        async with self.data_lock:
            sd = self.session_data.setdefault(session_id, {})
            sd["unanswered_count"] = unanswered_count + 1

            # 私聊：安排下一次；群聊由沉默計時器自行處理
            if not is_group_session_id(session_id):
                session_config = get_session_config(self.config, session_id)
                if session_config:
                    schedule_conf = session_config.get("schedule_settings", {})
                    interval = compute_weighted_interval(schedule_conf, self.timezone)
                    run_date = self._add_scheduled_job(session_id, interval)
                    sd["next_trigger_time"] = time.time() + interval
                    logger.info(
                        f"{_LOG_TAG} 已為 "
                        f"{get_session_log_str(session_id, session_config, self.session_data)} "
                        f"安排下一次主動訊息: {run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
                    )
            await self._save_data()

    # ═══════════════════════════════════════════════════════════
    #  核心執行：check_and_chat
    #
    #  由 APScheduler 定時觸發，完成一次完整的主動訊息流程：
    #  檢查條件 → 動態修正 UMO → 準備 LLM 請求 → 呼叫 LLM →
    #  狀態一致性檢查 → 發送訊息 → 收尾與重新排程。
    # ═══════════════════════════════════════════════════════════

    async def check_and_chat(self, session_id: str, ctx_job_id: str = "") -> None:
        """由定時任務觸發的核心函數，完成一次完整的主動訊息流程。"""
        session_config = None
        try:
            # ── 步驟 1：檢查是否允許發送 ──
            session_config = get_session_config(self.config, session_id)
            if not await self._is_chat_allowed(session_id, session_config):
                # 不允許但仍需排定下一次（例如免打擾時段結束後繼續）
                await self._schedule_next_chat_and_save(session_id)
                return

            schedule_conf = session_config.get("schedule_settings", {})

            # ── 步驟 2：檢查未回覆次數（概率衰減 / 硬性上限） ──
            async with self.data_lock:
                unanswered_count = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )
                should_trigger, reason = should_trigger_by_unanswered(
                    unanswered_count, schedule_conf, self.timezone
                )
                if not should_trigger:
                    logger.info(
                        f"{_LOG_TAG} {get_session_log_str(session_id, session_config, self.session_data)} "
                        f"{reason}"
                    )
                    # 衰減跳過時仍需排定下一次（給下次機會擲骰）
                    if "衰減" in reason:
                        await self._schedule_next_chat_and_save(session_id)
                    return
                if reason:
                    logger.info(
                        f"{_LOG_TAG} {get_session_log_str(session_id, session_config, self.session_data)} "
                        f"{reason}"
                    )

            # ── 步驟 3：動態修正 UMO ──
            # 平台可能重啟導致 ID 變更，需要重新解析到存活的平台
            parsed = parse_session_id(session_id)
            if parsed:
                original_platform, msg_type, target_id = parsed
                new_session_id = resolve_full_umo(
                    target_id,
                    msg_type,
                    self.context.platform_manager,
                    self.session_data,
                    original_platform,
                )

                # 驗證目標平台是否正在運行
                new_parsed = parse_session_id(new_session_id)
                if new_parsed:
                    insts = {
                        p.meta().id: p
                        for p in self.context.platform_manager.get_insts()
                        if p.meta().id
                    }
                    platform_inst = insts.get(new_parsed[0])
                    if (
                        not platform_inst
                        or platform_inst.status != PlatformStatus.RUNNING
                    ):
                        # 平台未運行，延後重試
                        await self._schedule_next_chat_and_save(session_id)
                        return

                if new_session_id != session_id:
                    session_id = new_session_id

            # ── 步驟 4：準備 LLM 請求 ──
            request_package = await safe_prepare_llm_request(self.context, session_id)
            if not request_package:
                await self._schedule_next_chat_and_save(session_id)
                return

            conv_id = request_package["conv_id"]
            history = request_package["history"]
            system_prompt = request_package["system_prompt"]

            # 記錄任務開始時的狀態快照（用於後續一致性檢查）
            snapshot_last_msg = self.last_message_times.get(session_id, 0)
            snapshot_unanswered = unanswered_count

            # ── 步驟 5：構造 Prompt 並呼叫 LLM ──
            motivation_template = session_config.get("proactive_prompt", "")
            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
            final_prompt = motivation_template.replace(
                "{{unanswered_count}}", str(unanswered_count)
            ).replace("{{current_time}}", now_str)

            # 若本次觸發來自語境預測，將預測的原因和跟進提示注入 Prompt
            ctx_task = None
            if ctx_job_id:
                task_list = self._pending_context_tasks.get(session_id, [])
                ctx_task = next(
                    (t for t in task_list if t.get("job_id") == ctx_job_id), None
                )
            if ctx_task:
                ctx_reason = ctx_task.get("reason", "")
                ctx_hint = ctx_task.get("hint", "")
                final_prompt += (
                    f"\n\n[語境感知觸發]\n"
                    f"這條主動訊息的排程原因：{ctx_reason}\n"
                    f"建議的跟進話題：{ctx_hint}\n"
                    f"請將這個語境自然地融入你的訊息中。"
                )

            # 嘗試從 livingmemory 檢索相關記憶並注入 system_prompt（可選依賴）
            ctx_settings = session_config.get("context_aware_settings", {})
            enable_memory = ctx_settings.get("enable_memory", True)
            memory_str = ""
            if enable_memory:
                memory_top_k = ctx_settings.get("memory_top_k", 5)
                memory_query = ""
                if ctx_task:
                    memory_query = ctx_task.get("hint", "") or ctx_task.get(
                        "reason", ""
                    )
                if not memory_query:
                    memory_query = now_str
                memory_str = await recall_memories_for_proactive(
                    self.context, session_id, memory_query, memory_top_k=memory_top_k
                )
            if memory_str:
                system_prompt = system_prompt + "\n\n" + memory_str
                logger.info(
                    f"{_LOG_TAG} 已為 {get_session_log_str(session_id, session_config, self.session_data)} "
                    f"注入記憶到主動訊息 system_prompt。"
                )
            else:
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(session_id, session_config, self.session_data)} "
                    f"本次主動訊息未帶記憶（無相關記憶或 livingmemory 不可用）。"
                )

            # 清洗歷史記錄格式（確保 content 欄位一致）
            history = sanitize_history_content(history)

            # 呼叫 LLM（主要路徑 + 備用路徑）
            llm_response = await call_llm(
                self.context, session_id, final_prompt, history, system_prompt
            )
            if not llm_response or not llm_response.completion_text:
                await self._schedule_next_chat_and_save(session_id)
                return

            response_text = llm_response.completion_text.strip()
            # 過濾無效回應
            if response_text == "[object Object]":
                await self._schedule_next_chat_and_save(session_id)
                return

            # ── 步驟 6：狀態一致性檢查 ──
            # 若在 LLM 生成期間使用者發送了新訊息，則丟棄本次回應
            current_last_msg = self.last_message_times.get(session_id, 0)
            current_unanswered = self.session_data.get(session_id, {}).get(
                "unanswered_count", 0
            )
            if (
                current_last_msg > snapshot_last_msg
                or current_unanswered < snapshot_unanswered
            ):
                logger.info(
                    f"{_LOG_TAG} 使用者在 LLM 生成期間發送了新訊息，丟棄本次回應。"
                )
                return

            # ── 步驟 7：發送訊息並收尾 ──
            def _set_bot_time(t: float) -> None:
                self.last_bot_message_time = t

            await send_proactive_message(
                session_id=session_id,
                text=response_text,
                config=self.config,
                context=self.context,
                session_data=self.session_data,
                reset_group_silence_cb=self._reset_group_silence_timer,
                last_bot_message_time_setter=_set_bot_time,
            )
            await self._finalize_and_reschedule(
                session_id,
                conv_id,
                final_prompt,
                response_text,
                unanswered_count,
            )

            # 清理語境預測任務的追蹤（僅移除本次觸發的任務）
            if ctx_job_id and session_id in self._pending_context_tasks:
                task_list = self._pending_context_tasks[session_id]
                self._pending_context_tasks[session_id] = [
                    t for t in task_list if t.get("job_id") != ctx_job_id
                ]
                if not self._pending_context_tasks[session_id]:
                    self._pending_context_tasks.pop(session_id, None)
                async with self.data_lock:
                    sd = self.session_data.get(session_id)
                    if sd:
                        remaining = self._pending_context_tasks.get(session_id)
                        if remaining:
                            sd["pending_context_tasks"] = remaining
                        else:
                            sd.pop("pending_context_tasks", None)
                            sd.pop("pending_context_task", None)

            # 群聊：清除 next_trigger_time（由沉默計時器接管後續排程）
            if is_group_session_id(session_id):
                async with self.data_lock:
                    sd = self.session_data.get(session_id)
                    if sd and "next_trigger_time" in sd:
                        del sd["next_trigger_time"]
                        await self._save_data()

        except Exception as e:
            logger.error(f"{_LOG_TAG} check_and_chat 致命錯誤: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())

            # 認證錯誤不重試（避免無限循環）
            if "Authentication" in type(e).__name__ or "auth" in str(e).lower():
                return

            # 清理失敗的排程數據
            try:
                async with self.data_lock:
                    sd = self.session_data.get(session_id)
                    if sd and "next_trigger_time" in sd:
                        del sd["next_trigger_time"]
                        await self._save_data()
            except Exception:
                pass

            # 嘗試重新排程（錯誤恢復）
            try:
                await self._schedule_next_chat_and_save(session_id)
            except Exception as se:
                logger.error(f"{_LOG_TAG} 錯誤恢復中重新調度失敗: {se}")
