# core/chat_executor.py — 主動訊息核心執行邏輯
"""
由 APScheduler 定時觸發的核心流程，拆分為獨立的子步驟函數：

1. ``_check_preconditions``  — 免打擾 / 衰減 / 硬性上限
2. ``_resolve_session_umo``  — 動態修正 UMO（平台重啟容錯）
3. ``_prepare_and_call_llm`` — 準備請求、構造 Prompt、呼叫 LLM
4. ``_deliver_and_finalize`` — 發送訊息、存檔歷史、重新排程

``check_and_chat`` 為唯一公開入口，串接上述步驟並統一處理錯誤恢復。
"""

from __future__ import annotations

import asyncio
import time
import traceback
import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.platform.platform import PlatformStatus

from .config import get_session_config
from .llm_helpers import (
    NonRetryableLLMError,
    call_llm,
    recall_memories_for_proactive,
    safe_prepare_llm_request,
    truncate_history_for_proactive_llm,
)
from .messaging import sanitize_history_content
from .scheduler import (
    compute_weighted_interval,
    is_unanswered_limit_reached,
    should_trigger_by_unanswered,
)
from .send import send_proactive_message
from .utils import (
    get_session_log_str,
    is_group_session_id,
    parse_session_id,
    resolve_full_umo,
)

if TYPE_CHECKING:
    from ..main import ProactiveChatPlugin

_LOG_TAG = "[主動訊息]"

# 已知的無效 LLM 回應（直接丟棄）
_INVALID_RESPONSES = frozenset({"[object Object]"})

# 認證相關錯誤關鍵字（命中時不重試，避免無限循環）
_AUTH_ERROR_KEYWORDS = frozenset(
    {"authentication", "auth", "unauthorized", "forbidden"}
)

_SQLITE_LOCK_KEYWORDS = frozenset({"database is locked", "database table is locked"})
_RELATIONSHIP_CONTEXT = (
    "\n\n[關係時間感知]\n"
    "- 這個會話第一次被你記錄到互動的時間：{first_interaction_time}\n"
    "- 從第一次互動到現在大約經過：{relationship_duration}\n"
    "請把這個資訊當成背景感受來調整熟悉程度、關心方式和話題深度；"
    "不要生硬地報出精確時間，除非使用者正在詢問。"
)


# ═══════════════════════════════════════════════════════════
#  公開入口
# ═══════════════════════════════════════════════════════════


async def check_and_chat(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str = ""
) -> None:
    """由定時任務觸發的核心函數，完成一次完整的主動訊息流程。

    流程：前置檢查 → UMO 修正 → LLM 生成 → 發送與收尾。
    任何步驟回傳 ``None`` 即表示本次應中止（已在內部處理重新排程）。
    """
    context_finished = False
    habit_finished = False
    try:
        habit_task = plugin._find_habit_task(session_id, ctx_job_id)
        skip_unanswered = bool(
            habit_task and not habit_task.get("count_unanswered", False)
        )
        # ── 步驟 1：前置條件檢查（免打擾 / 衰減 / 硬性上限） ──
        result = await _check_preconditions(
            plugin, session_id, skip_unanswered=skip_unanswered
        )
        if result is None:
            return
        session_config, unanswered_count = result

        # ── 步驟 2：動態修正 UMO（平台重啟容錯） ──
        resolved_id = await _resolve_session_umo(plugin, session_id)
        if resolved_id is None:
            return
        if resolved_id != session_id:
            await plugin._merge_session_state(session_id, resolved_id)
        session_id = resolved_id

        # ── 步驟 3：準備請求、構造 Prompt、呼叫 LLM ──
        llm_result = await _prepare_and_call_llm(
            plugin, session_id, session_config, unanswered_count, ctx_job_id
        )
        if llm_result is None:
            return
        response_text, conv_id, final_prompt, _ctx_task = llm_result

        # ── 步驟 4：發送訊息、存檔歷史、重新排程 ──
        delivered = await _deliver_and_finalize(
            plugin,
            session_id,
            session_config,
            response_text,
            conv_id,
            final_prompt,
            unanswered_count,
            ctx_job_id,
        )
        if delivered:
            context_finished = True
            habit_finished = True

    except Exception as e:
        await _handle_fatal_error(
            plugin, session_id, e, skip_reschedule=_is_habit_job(ctx_job_id)
        )
    finally:
        if ctx_job_id and not context_finished and not _is_habit_job(ctx_job_id):
            await _cleanup_context_task(plugin, session_id, ctx_job_id)
        if ctx_job_id and _is_habit_job(ctx_job_id):
            if not habit_finished:
                await plugin._cleanup_habit_task(session_id, ctx_job_id)
            await plugin._schedule_next_habit_task(session_id)


# ═══════════════════════════════════════════════════════════
#  步驟 1：前置條件檢查
# ═══════════════════════════════════════════════════════════


async def _check_preconditions(
    plugin: ProactiveChatPlugin, session_id: str, *, skip_unanswered: bool = False
) -> tuple[dict, int] | None:
    """檢查免打擾時段與未回覆衰減，決定是否繼續執行。

    Returns:
        ``(session_config, unanswered_count)``；不應繼續時回傳 ``None``。
    """
    session_config = get_session_config(plugin.config, session_id)
    if not await plugin._is_chat_allowed(session_id, session_config):
        await plugin._schedule_next_chat_and_save(session_id)
        return None

    schedule_conf = session_config.get("schedule_settings", {})
    log_str = get_session_log_str(session_id, session_config, plugin.session_data)

    # 在鎖內僅讀取數據與做純計算，不呼叫任何取鎖的函數
    async with plugin.data_lock:
        unanswered_count = plugin.session_data.get(session_id, {}).get(
            "unanswered_count", 0
        )
        if skip_unanswered:
            should_trigger, reason = True, ""
        else:
            should_trigger, reason = should_trigger_by_unanswered(
                unanswered_count, schedule_conf, plugin.timezone
            )

    # 鎖外處理判定結果（_schedule_next_chat_and_save 內部會取鎖，不可嵌套）
    if not should_trigger:
        logger.info(f"{_LOG_TAG} {log_str} {reason}")
        if "衰減" in reason:
            await plugin._schedule_next_chat_and_save(session_id)
        elif "硬性上限" in reason:
            await plugin._clear_regular_job_state(session_id)
        return None
    if reason:
        logger.info(f"{_LOG_TAG} {log_str} {reason}")

    return session_config, unanswered_count


# ═══════════════════════════════════════════════════════════
#  步驟 2：動態修正 UMO
# ═══════════════════════════════════════════════════════════


async def _resolve_session_umo(
    plugin: ProactiveChatPlugin, session_id: str
) -> str | None:
    """解析並驗證目標平台是否存活，回傳修正後的 session_id。

    平台可能因重啟導致 ID 變更，需要重新解析到存活的平台實例。

    Returns:
        修正後的 ``session_id``；平台未運行時回傳 ``None``（已排定重試）。
    """
    parsed = parse_session_id(session_id)
    if not parsed:
        return session_id

    original_platform, msg_type, target_id = parsed
    new_session_id = resolve_full_umo(
        target_id,
        msg_type,
        plugin.context.platform_manager,
        plugin.session_data,
        original_platform,
    )

    # 驗證目標平台是否正在運行
    new_parsed = parse_session_id(new_session_id)
    if new_parsed:
        running_platforms = {
            p.meta().id: p
            for p in plugin.context.platform_manager.get_insts()
            if p.meta().id
        }
        platform_inst = running_platforms.get(new_parsed[0])
        if not platform_inst or platform_inst.status != PlatformStatus.RUNNING:
            await plugin._schedule_next_chat_and_save(session_id)
            return None

    return new_session_id


# ═══════════════════════════════════════════════════════════
#  步驟 3：準備請求、構造 Prompt、呼叫 LLM
# ═══════════════════════════════════════════════════════════


async def _prepare_and_call_llm(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    ctx_job_id: str,
) -> tuple[str, str, str, dict | None] | None:
    """準備 LLM 請求並取得回應，包含狀態一致性檢查。

    Returns:
        ``(response_text, conv_id, final_prompt, ctx_task)``；
        LLM 失敗或狀態不一致時回傳 ``None``（已排定重試）。
    """
    # 先記錄狀態，再做任何 await；避免 history 載入期間使用者回覆後仍用舊上下文發送。
    snapshot_last_msg = plugin.last_message_times.get(session_id, 0)
    snapshot_unanswered = plugin.session_data.get(session_id, {}).get(
        "unanswered_count", unanswered_count
    )

    request_package = await safe_prepare_llm_request(plugin.context, session_id)
    if not request_package:
        if not _is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None

    conv_id = request_package["conv_id"]
    history = request_package["history"]
    system_prompt = request_package["system_prompt"]

    if _state_changed_during_generation(
        plugin, session_id, snapshot_last_msg, snapshot_unanswered
    ):
        logger.info(f"{_LOG_TAG} 使用者在準備 LLM 請求期間發送了新訊息，丟棄本次任務。")
        return None

    # 構造 Prompt
    final_prompt, ctx_task = _build_final_prompt(
        plugin,
        session_id,
        session_config,
        unanswered_count,
        snapshot_last_msg,
        ctx_job_id,
    )

    # 注入 livingmemory 記憶（可選依賴）
    system_prompt = await _inject_memory(
        plugin, session_id, session_config, ctx_task, system_prompt, final_prompt
    )

    # 清洗歷史記錄格式（確保 content 欄位一致）
    history = sanitize_history_content(history)

    # 主動回覆需要自行截斷 history，減少上下文長度，避免 LLM 記憶體不足
    history = await truncate_history_for_proactive_llm(
        plugin.context, session_id, history
    )

    # 呼叫 LLM
    try:
        llm_response = await call_llm(
            plugin.context, session_id, final_prompt, history, system_prompt
        )
    except NonRetryableLLMError as e:
        logger.error(
            f"{_LOG_TAG} LLM 發生不可重試錯誤，停止本輪排程 | session={session_id}: {e}"
        )
        if not _is_habit_job(ctx_job_id):
            await plugin._clear_regular_job_state(session_id)
        return None
    if not llm_response or not llm_response.completion_text:
        if not _is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None

    response_text = llm_response.completion_text.strip()
    if not response_text:
        if not _is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None
    if response_text in _INVALID_RESPONSES:
        if not _is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None

    # 狀態一致性檢查：若 LLM 生成期間使用者發送了新訊息，丟棄本次回應
    if _state_changed_during_generation(
        plugin, session_id, snapshot_last_msg, snapshot_unanswered
    ):
        logger.info(f"{_LOG_TAG} 使用者在 LLM 生成期間發送了新訊息，丟棄本次回應。")
        return None

    return response_text, conv_id, final_prompt, ctx_task


def _state_changed_during_generation(
    plugin: ProactiveChatPlugin,
    session_id: str,
    snapshot_last_msg: float,
    snapshot_unanswered: int,
) -> bool:
    """檢查 LLM 生成期間使用者是否發送了新訊息。"""
    current_last_msg = plugin.last_message_times.get(session_id, 0)
    current_unanswered = plugin.session_data.get(session_id, {}).get(
        "unanswered_count", 0
    )
    return (
        current_last_msg > snapshot_last_msg or current_unanswered < snapshot_unanswered
    )


# ═══════════════════════════════════════════════════════════
#  步驟 4：發送訊息、存檔歷史、重新排程
# ═══════════════════════════════════════════════════════════


async def _deliver_and_finalize(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    response_text: str,
    conv_id: str,
    final_prompt: str,
    unanswered_count: int,
    ctx_job_id: str,
) -> bool:
    """發送訊息、存檔對話歷史、清理語境任務、重新排程。"""

    def _set_bot_time(t: float) -> None:
        plugin.last_bot_message_time = t

    sent = await send_proactive_message(
        session_id=session_id,
        text=response_text,
        config=plugin.config,
        context=plugin.context,
        session_data=plugin.session_data,
        reset_group_silence_cb=plugin._reset_group_silence_timer,
        last_bot_message_time_setter=_set_bot_time,
    )
    if not sent:
        logger.warning(
            f"{_LOG_TAG} 主動訊息發送失敗，清除本輪排程並等待使用者新訊息"
            f" | session={session_id}"
        )
        if not _is_habit_job(ctx_job_id):
            await plugin._clear_regular_job_state(session_id)
        return False

    await _save_conversation_history(
        plugin, session_config, conv_id, final_prompt, response_text
    )
    await _update_unanswered_and_reschedule(
        plugin,
        session_id,
        session_config,
        unanswered_count,
        ctx_job_id=ctx_job_id,
        clear_task_description=not bool(ctx_job_id),
    )

    if ctx_job_id:
        if _is_habit_job(ctx_job_id):
            await plugin._cleanup_habit_task(session_id, ctx_job_id)
        else:
            await _cleanup_context_task(plugin, session_id, ctx_job_id)
    return True


async def _save_conversation_history(
    plugin: ProactiveChatPlugin,
    session_config: dict,
    conv_id: str,
    user_prompt: str,
    assistant_response: str,
) -> None:
    """按配置將主動訊息寫回 AstrBot 主對話歷史。

    預設關閉，避免主動排程在一般 agent request 存檔時搶同一個 SQLite。
    這只影響 AstrBot 主歷史是否記錄本次主動 prompt/response，不影響訊息發送、
    未回覆計數或 livingmemory 檢索。
    """
    history_conf = _history_settings(plugin, session_config)
    if not history_conf["save_proactive_history"]:
        return
    if not conv_id:
        return

    delay_seconds = history_conf["history_save_delay_seconds"]
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    max_attempts = history_conf["history_save_retry_attempts"] + 1
    async with plugin._history_save_lock:
        for attempt in range(1, max_attempts + 1):
            try:
                await plugin.context.conversation_manager.add_message_pair(
                    cid=conv_id,
                    user_message=UserMessageSegment(
                        content=[TextPart(text=user_prompt)]
                    ),
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=assistant_response)]
                    ),
                )
                return
            except Exception as e:
                if _is_sqlite_lock_error(e):
                    if attempt >= max_attempts:
                        logger.warning(
                            f"{_LOG_TAG} AstrBot 對話歷史忙碌，已跳過本次主動訊息寫回"
                            f" | conv_id={conv_id}: {e}"
                        )
                        return
                    await asyncio.sleep(min(0.5 * attempt, 2.0))
                    continue

                logger.warning(
                    f"{_LOG_TAG} 主動訊息寫回 AstrBot 對話歷史失敗，已跳過: {e}"
                )
                logger.debug(traceback.format_exc())
                return


def _history_settings(plugin: ProactiveChatPlugin, session_config: dict) -> dict:
    """合併全域與會話級歷史寫回設定。"""
    default_settings = {
        "save_proactive_history": False,
        "history_save_delay_seconds": 2.0,
        "history_save_retry_attempts": 3,
    }
    global_settings = plugin.config.get("history_settings", {})
    session_settings = session_config.get("history_settings", {})
    if not isinstance(global_settings, dict):
        global_settings = {}
    if not isinstance(session_settings, dict):
        session_settings = {}

    out = (
        default_settings
        | {k: v for k, v in global_settings.items() if v is not None}
        | {k: v for k, v in session_settings.items() if v is not None}
    )
    out["save_proactive_history"] = bool(out.get("save_proactive_history", False))
    out["history_save_delay_seconds"] = _coerce_float(
        out.get("history_save_delay_seconds"), 2.0, min_value=0.0, max_value=30.0
    )
    out["history_save_retry_attempts"] = int(
        _coerce_float(
            out.get("history_save_retry_attempts"), 3, min_value=0.0, max_value=10.0
        )
    )
    return out


def _coerce_float(
    value: object, default: float, *, min_value: float, max_value: float
) -> float:
    """將配置值轉為 float，並限制在合理範圍。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _is_sqlite_lock_error(error: Exception) -> bool:
    """判斷例外是否為 SQLite lock 類型錯誤。"""
    message = str(error).lower()
    return any(keyword in message for keyword in _SQLITE_LOCK_KEYWORDS)


async def _update_unanswered_and_reschedule(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    *,
    ctx_job_id: str = "",
    clear_task_description: bool = False,
) -> None:
    """遞增未回覆計數，私聊時安排下一次主動訊息。"""
    habit_task = plugin._find_habit_task(session_id, ctx_job_id)
    count_unanswered = not habit_task or bool(habit_task.get("count_unanswered", False))
    async with plugin.data_lock:
        sd = plugin.session_data.setdefault(session_id, {})
        next_unanswered_count = unanswered_count + (1 if count_unanswered else 0)
        sd["unanswered_count"] = next_unanswered_count
        if clear_task_description:
            sd.pop("task_description", None)

        if habit_task and not count_unanswered:
            await plugin._save_data()
            logger.info(
                f"{_LOG_TAG} {get_session_log_str(session_id, session_config, plugin.session_data)} "
                "本次習慣時段訊息不累加未回覆次數。"
            )
            return

        # 私聊：安排下一次；群聊由沉默計時器自行處理
        if is_group_session_id(session_id):
            sd.pop("next_trigger_time", None)
            await plugin._save_data()
            return

        schedule_conf = session_config.get("schedule_settings", {})
        limit_reached, reason = is_unanswered_limit_reached(
            next_unanswered_count, schedule_conf, plugin.timezone
        )
        if limit_reached:
            sd.pop("next_trigger_time", None)
            await plugin._save_data()
            logger.info(
                f"{_LOG_TAG} {get_session_log_str(session_id, session_config, plugin.session_data)} "
                f"{reason}，不再安排下一次主動訊息。"
            )
            return

        interval = compute_weighted_interval(
            schedule_conf, plugin.timezone, next_unanswered_count
        )
        run_date = datetime.fromtimestamp(time.time() + interval, tz=plugin.timezone)
        sd["next_trigger_time"] = run_date.timestamp()
        await plugin._save_data()

        plugin._add_scheduled_job_at(session_id, run_date)
        logger.info(
            f"{_LOG_TAG} 已為 "
            f"{get_session_log_str(session_id, session_config, plugin.session_data)} "
            f"安排下一次主動訊息: {run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
        )


async def _cleanup_context_task(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str
) -> None:
    """清理已完成的語境預測任務追蹤。"""
    if session_id not in plugin._pending_context_tasks:
        return

    task_list = plugin._pending_context_tasks[session_id]
    plugin._pending_context_tasks[session_id] = [
        t for t in task_list if t.get("job_id") != ctx_job_id
    ]
    if not plugin._pending_context_tasks[session_id]:
        plugin._pending_context_tasks.pop(session_id, None)

    async with plugin.data_lock:
        sd = plugin.session_data.get(session_id)
        if sd:
            remaining = plugin._pending_context_tasks.get(session_id)
            if remaining:
                sd["pending_context_tasks"] = remaining
            else:
                sd.pop("pending_context_tasks", None)
                sd.pop("pending_context_task", None)
            await plugin._save_data()


# ═══════════════════════════════════════════════════════════
#  錯誤恢復
# ═══════════════════════════════════════════════════════════


async def _handle_fatal_error(
    plugin: ProactiveChatPlugin,
    session_id: str,
    error: Exception,
    *,
    skip_reschedule: bool = False,
) -> None:
    """統一處理 check_and_chat 中的未預期例外。

    認證類錯誤不重試（避免無限循環），其餘錯誤清理排程數據後嘗試重新排程。
    """
    logger.error(
        f"{_LOG_TAG} check_and_chat 致命錯誤"
        f" | session={session_id}: {type(error).__name__}: {error}"
    )
    logger.debug(traceback.format_exc())

    # 認證錯誤不重試
    error_str = f"{type(error).__name__} {error}".lower()
    if any(kw in error_str for kw in _AUTH_ERROR_KEYWORDS):
        return
    if skip_reschedule:
        return

    # 清理失敗的排程數據
    try:
        async with plugin.data_lock:
            sd = plugin.session_data.get(session_id)
            if sd and "next_trigger_time" in sd:
                del sd["next_trigger_time"]
                await plugin._save_data()
    except Exception as cleanup_err:
        logger.debug(
            f"{_LOG_TAG} _handle_fatal_error 清理排程數據失敗"
            f" | session={session_id}: {cleanup_err}"
        )

    # 嘗試重新排程
    try:
        await plugin._schedule_next_chat_and_save(session_id)
    except Exception as se:
        logger.error(f"{_LOG_TAG} 錯誤恢復中重新調度失敗: {se}")


# ═══════════════════════════════════════════════════════════
#  Prompt 構造與記憶注入
# ═══════════════════════════════════════════════════════════


def _build_final_prompt(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    snapshot_last_msg: float,
    ctx_job_id: str,
) -> tuple[str, dict | None]:
    """構造最終的 LLM Prompt。

    替換佔位符 ``{{current_time}}``、``{{unanswered_count}}``、``{{last_reply_time}}``、
    ``{{first_interaction_time}}``、``{{relationship_duration}}``，
    並在語境預測觸發時注入原因與跟進提示。

    Returns:
        ``(final_prompt, ctx_task)``，``ctx_task`` 為語境任務 dict 或 ``None``。
    """
    motivation_template = session_config.get("proactive_prompt", "")
    now_str = datetime.now(plugin.timezone).strftime("%Y年%m月%d日 %H:%M")
    last_reply_str = _format_last_reply_time(snapshot_last_msg, plugin.timezone)
    session_info = plugin.session_data.get(session_id, {})
    first_interaction_ts = (
        session_info.get("first_interaction_time")
        if isinstance(session_info, dict)
        else None
    )
    first_interaction_str = _format_first_interaction_time(
        first_interaction_ts, plugin.timezone
    )
    relationship_duration_str = _format_elapsed_duration(first_interaction_ts)

    final_prompt = (
        motivation_template.replace("{{unanswered_count}}", str(unanswered_count))
        .replace("{{current_time}}", now_str)
        .replace("{{last_reply_time}}", last_reply_str)
        .replace("{{first_interaction_time}}", first_interaction_str)
        .replace("{{relationship_duration}}", relationship_duration_str)
    )
    final_prompt += _RELATIONSHIP_CONTEXT.format(
        first_interaction_time=first_interaction_str,
        relationship_duration=relationship_duration_str,
    )

    # 若本次觸發來自語境預測，將預測的原因和跟進提示注入 Prompt
    ctx_task = _find_context_task(plugin, session_id, ctx_job_id)
    if ctx_task:
        ctx_reason = str(ctx_task.get("reason", "")).strip()
        ctx_hint = str(ctx_task.get("hint", "")).strip()
        ctx_description = str(ctx_task.get("description", "")).strip()
        final_prompt += (
            f"\n\n[語境感知觸發]\n"
            f"這條主動訊息的排程原因：{ctx_reason}\n"
            f"建議的跟進話題：{ctx_hint}\n"
        )
        if ctx_description:
            final_prompt += f"任務補充描述：{ctx_description}\n"
        final_prompt += "請將這個語境自然地融入你的訊息中。"
    else:
        habit_task = plugin._find_habit_task(session_id, ctx_job_id)
        if habit_task:
            rule_name = str(habit_task.get("rule_name", "") or "習慣時段")
            hint = str(habit_task.get("hint", "") or "").strip()
            description = str(habit_task.get("description", "") or "").strip()
            final_prompt += (
                f"\n\n[習慣時段觸發]\n現在符合你的日常習慣時段：{rule_name}。\n"
            )
            if hint:
                final_prompt += f"建議的情緒或話題：{hint}\n"
            if description:
                final_prompt += f"補充描述：{description}\n"
            final_prompt += "請像平常這個時間自然出現一樣回覆，不要提到排程或設定。"
        task_description = (
            "" if habit_task else _active_task_description(plugin, session_id)
        )
        if task_description:
            final_prompt += (
                f"\n\n[排程任務描述]\n"
                f"{task_description}\n"
                f"請將這個任務描述自然地融入你的主動訊息中。"
            )

    return final_prompt, ctx_task


def _format_last_reply_time(
    last_msg_ts: float, timezone: zoneinfo.ZoneInfo | None
) -> str:
    """將時間戳格式化為可讀的「最後回覆時間（N 分鐘/小時前）」字串。"""
    if last_msg_ts <= 0:
        return "未知"

    last_reply_dt = datetime.fromtimestamp(last_msg_ts, tz=timezone)
    elapsed_min = int(time.time() - last_msg_ts) // 60

    if elapsed_min < 60:
        elapsed_str = f"{elapsed_min}分鐘"
    else:
        hours, mins = divmod(elapsed_min, 60)
        elapsed_str = f"{hours}小時{mins}分鐘" if mins else f"{hours}小時"

    return f"{last_reply_dt.strftime('%Y年%m月%d日 %H:%M')}（{elapsed_str}前）"


def _format_first_interaction_time(
    first_ts: object, timezone: zoneinfo.ZoneInfo | None
) -> str:
    """將首次互動時間戳格式化為 prompt 可讀文字。"""
    ts = _coerce_timestamp(first_ts)
    if ts is None:
        return "未知"
    first_dt = datetime.fromtimestamp(ts, tz=timezone)
    return first_dt.strftime("%Y年%m月%d日 %H:%M")


def _format_elapsed_duration(since_ts: object) -> str:
    """將起始時間到現在的距離格式化為自然語言。"""
    ts = _coerce_timestamp(since_ts)
    if ts is None:
        return "未知"

    elapsed_seconds = max(0, int(time.time() - ts))
    minutes = elapsed_seconds // 60
    if minutes < 1:
        return "不到1分鐘"
    if minutes < 60:
        return f"{minutes}分鐘"

    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小時{mins}分鐘" if mins else f"{hours}小時"

    days, hours = divmod(hours, 24)
    if days < 30:
        return f"{days}天{hours}小時" if hours else f"{days}天"

    months, days = divmod(days, 30)
    if months < 12:
        return f"{months}個月{days}天" if days else f"{months}個月"

    years, months = divmod(months, 12)
    return f"{years}年{months}個月" if months else f"{years}年"


def _coerce_timestamp(value: object) -> float | None:
    """將持久化狀態中的時間值轉為合法時間戳。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or ts > time.time() + 60:
        return None
    return ts


def _find_context_task(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str
) -> dict | None:
    """根據 job_id 查找對應的語境預測任務。"""
    if not ctx_job_id:
        return None
    task_list = plugin._pending_context_tasks.get(session_id, [])
    return next((t for t in task_list if t.get("job_id") == ctx_job_id), None)


def _is_habit_job(job_id: str) -> bool:
    """判斷 job_id 是否為習慣時段任務。"""
    return str(job_id).startswith("habit_")


def _active_task_description(plugin: ProactiveChatPlugin, session_id: str) -> str:
    """回傳目前會話待執行任務的補充描述。"""
    session_info = plugin.session_data.get(session_id, {})
    if not isinstance(session_info, dict):
        return ""
    for key in (
        "task_description",
        "auto_trigger_description",
        "group_idle_description",
    ):
        description = str(session_info.get(key) or "").strip()
        if description:
            return description
    return ""


async def _inject_memory(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    ctx_task: dict | None,
    system_prompt: str,
    final_prompt: str,
) -> str:
    """嘗試從 livingmemory 檢索相關記憶並注入 system_prompt。

    Returns:
        注入記憶後的 system_prompt（若無可用記憶則原樣回傳）。
    """
    ctx_settings = session_config.get("context_aware_settings", {})
    if not ctx_settings.get("enable_memory", True):
        return system_prompt

    log_str = get_session_log_str(session_id, session_config, plugin.session_data)
    memory_top_k = ctx_settings.get("memory_top_k", 5)

    # 優先使用語境任務的使用者描述 / hint / reason 作為檢索查詢。
    memory_query = ""
    if ctx_task:
        memory_query = (
            ctx_task.get("description", "")
            or ctx_task.get("hint", "")
            or ctx_task.get("reason", "")
        )
    if not memory_query:
        memory_query = final_prompt.strip()

    memory_str = await recall_memories_for_proactive(
        plugin.context, session_id, memory_query, memory_top_k=memory_top_k
    )
    if memory_str:
        logger.info(f"{_LOG_TAG} 已為 {log_str} 注入記憶到主動訊息 system_prompt。")
        return system_prompt + "\n\n" + memory_str

    logger.info(
        f"{_LOG_TAG} {log_str} "
        f"本次主動訊息未帶記憶（無相關記憶或 livingmemory 不可用）。"
    )
    return system_prompt
