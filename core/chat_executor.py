# core/chat_executor.py — 主動訊息核心執行邏輯
"""
由 APScheduler 定時觸發的核心流程：
- check_and_chat：檢查條件 → 動態修正 UMO → 準備 LLM 請求 →
  呼叫 LLM → 狀態一致性檢查 → 發送訊息 → 收尾與重新排程
- finalize_and_reschedule：發送成功後的收尾工作

所有函數接收插件實例 ``plugin`` 作為第一個參數。
"""

from __future__ import annotations

import time
import traceback
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
    call_llm,
    recall_memories_for_proactive,
    safe_prepare_llm_request,
)
from .messaging import sanitize_history_content
from .scheduler import compute_weighted_interval, should_trigger_by_unanswered
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


async def check_and_chat(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str = ""
) -> None:
    """由定時任務觸發的核心函數，完成一次完整的主動訊息流程。"""
    session_config = None
    try:
        # ── 步驟 1：檢查是否允許發送 ──
        session_config = get_session_config(plugin.config, session_id)
        if not await plugin._is_chat_allowed(session_id, session_config):
            # 不允許但仍需排定下一次（例如免打擾時段結束後繼續）
            await plugin._schedule_next_chat_and_save(session_id)
            return

        schedule_conf = session_config.get("schedule_settings", {})

        # ── 步驟 2：檢查未回覆次數（概率衰減 / 硬性上限） ──
        async with plugin.data_lock:
            unanswered_count = plugin.session_data.get(session_id, {}).get(
                "unanswered_count", 0
            )
            should_trigger, reason = should_trigger_by_unanswered(
                unanswered_count, schedule_conf, plugin.timezone
            )
            if not should_trigger:
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(session_id, session_config, plugin.session_data)} "
                    f"{reason}"
                )
                # 衰減跳過時仍需排定下一次（給下次機會擲骰）
                if "衰減" in reason:
                    await plugin._schedule_next_chat_and_save(session_id)
                return
            if reason:
                logger.info(
                    f"{_LOG_TAG} {get_session_log_str(session_id, session_config, plugin.session_data)} "
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
                plugin.context.platform_manager,
                plugin.session_data,
                original_platform,
            )

            # 驗證目標平台是否正在運行
            new_parsed = parse_session_id(new_session_id)
            if new_parsed:
                insts = {
                    p.meta().id: p
                    for p in plugin.context.platform_manager.get_insts()
                    if p.meta().id
                }
                platform_inst = insts.get(new_parsed[0])
                if not platform_inst or platform_inst.status != PlatformStatus.RUNNING:
                    # 平台未運行，延後重試
                    await plugin._schedule_next_chat_and_save(session_id)
                    return

            if new_session_id != session_id:
                session_id = new_session_id

        # ── 步驟 4：準備 LLM 請求 ──
        request_package = await safe_prepare_llm_request(plugin.context, session_id)
        if not request_package:
            await plugin._schedule_next_chat_and_save(session_id)
            return

        conv_id = request_package["conv_id"]
        history = request_package["history"]
        system_prompt = request_package["system_prompt"]

        # 記錄任務開始時的狀態快照（用於後續一致性檢查）
        snapshot_last_msg = plugin.last_message_times.get(session_id, 0)
        snapshot_unanswered = unanswered_count

        # ── 步驟 5：構造 Prompt 並呼叫 LLM ──
        final_prompt, ctx_task = _build_final_prompt(
            plugin,
            session_id,
            session_config,
            unanswered_count,
            snapshot_last_msg,
            ctx_job_id,
        )

        # 嘗試從 livingmemory 檢索相關記憶並注入 system_prompt（可選依賴）
        system_prompt = await _inject_memory(
            plugin, session_id, session_config, ctx_task, system_prompt
        )

        # 清洗歷史記錄格式（確保 content 欄位一致）
        history = sanitize_history_content(history)

        # 呼叫 LLM（主要路徑 + 備用路徑）
        llm_response = await call_llm(
            plugin.context, session_id, final_prompt, history, system_prompt
        )
        if not llm_response or not llm_response.completion_text:
            await plugin._schedule_next_chat_and_save(session_id)
            return

        response_text = llm_response.completion_text.strip()
        # 過濾無效回應
        if response_text == "[object Object]":
            await plugin._schedule_next_chat_and_save(session_id)
            return

        # ── 步驟 6：狀態一致性檢查 ──
        # 若在 LLM 生成期間使用者發送了新訊息，則丟棄本次回應
        current_last_msg = plugin.last_message_times.get(session_id, 0)
        current_unanswered = plugin.session_data.get(session_id, {}).get(
            "unanswered_count", 0
        )
        if (
            current_last_msg > snapshot_last_msg
            or current_unanswered < snapshot_unanswered
        ):
            logger.info(f"{_LOG_TAG} 使用者在 LLM 生成期間發送了新訊息，丟棄本次回應。")
            return

        # ── 步驟 7：發送訊息並收尾 ──
        def _set_bot_time(t: float) -> None:
            plugin.last_bot_message_time = t

        await send_proactive_message(
            session_id=session_id,
            text=response_text,
            config=plugin.config,
            context=plugin.context,
            session_data=plugin.session_data,
            reset_group_silence_cb=plugin._reset_group_silence_timer,
            last_bot_message_time_setter=_set_bot_time,
        )
        await finalize_and_reschedule(
            plugin,
            session_id,
            conv_id,
            final_prompt,
            response_text,
            unanswered_count,
        )

        # 清理語境預測任務的追蹤（僅移除本次觸發的任務）
        if ctx_job_id and session_id in plugin._pending_context_tasks:
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

        # 群聊：清除 next_trigger_time（由沉默計時器接管後續排程）
        if is_group_session_id(session_id):
            async with plugin.data_lock:
                sd = plugin.session_data.get(session_id)
                if sd and "next_trigger_time" in sd:
                    del sd["next_trigger_time"]
                    await plugin._save_data()

    except Exception as e:
        logger.error(f"{_LOG_TAG} check_and_chat 致命錯誤: {type(e).__name__}: {e}")
        logger.debug(traceback.format_exc())

        # 認證錯誤不重試（避免無限循環）
        if "Authentication" in type(e).__name__ or "auth" in str(e).lower():
            return

        # 清理失敗的排程數據
        try:
            async with plugin.data_lock:
                sd = plugin.session_data.get(session_id)
                if sd and "next_trigger_time" in sd:
                    del sd["next_trigger_time"]
                    await plugin._save_data()
        except Exception:
            pass

        # 嘗試重新排程（錯誤恢復）
        try:
            await plugin._schedule_next_chat_and_save(session_id)
        except Exception as se:
            logger.error(f"{_LOG_TAG} 錯誤恢復中重新調度失敗: {se}")


def _build_final_prompt(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    snapshot_last_msg: float,
    ctx_job_id: str,
) -> tuple[str, dict | None]:
    """構造最終的 LLM Prompt，回傳 (final_prompt, ctx_task)。"""
    motivation_template = session_config.get("proactive_prompt", "")
    now_str = datetime.now(plugin.timezone).strftime("%Y年%m月%d日 %H:%M")

    # 計算使用者最後回覆時間的可讀字串
    if snapshot_last_msg > 0:
        last_reply_dt = datetime.fromtimestamp(snapshot_last_msg, tz=plugin.timezone)
        elapsed_sec = int(time.time() - snapshot_last_msg)
        elapsed_min = elapsed_sec // 60
        if elapsed_min < 60:
            elapsed_str = f"{elapsed_min}分鐘"
        else:
            elapsed_h, elapsed_m = divmod(elapsed_min, 60)
            elapsed_str = (
                f"{elapsed_h}小時{elapsed_m}分鐘" if elapsed_m else f"{elapsed_h}小時"
            )
        last_reply_str = (
            f"{last_reply_dt.strftime('%Y年%m月%d日 %H:%M')}（{elapsed_str}前）"
        )
    else:
        last_reply_str = "未知"

    final_prompt = (
        motivation_template.replace("{{unanswered_count}}", str(unanswered_count))
        .replace("{{current_time}}", now_str)
        .replace("{{last_reply_time}}", last_reply_str)
    )

    # 若本次觸發來自語境預測，將預測的原因和跟進提示注入 Prompt
    ctx_task = None
    if ctx_job_id:
        task_list = plugin._pending_context_tasks.get(session_id, [])
        ctx_task = next((t for t in task_list if t.get("job_id") == ctx_job_id), None)
    if ctx_task:
        ctx_reason = ctx_task.get("reason", "")
        ctx_hint = ctx_task.get("hint", "")
        final_prompt += (
            f"\n\n[語境感知觸發]\n"
            f"這條主動訊息的排程原因：{ctx_reason}\n"
            f"建議的跟進話題：{ctx_hint}\n"
            f"請將這個語境自然地融入你的訊息中。"
        )

    return final_prompt, ctx_task


async def _inject_memory(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    ctx_task: dict | None,
    system_prompt: str,
) -> str:
    """嘗試從 livingmemory 檢索相關記憶並注入 system_prompt，回傳更新後的 prompt。"""
    ctx_settings = session_config.get("context_aware_settings", {})
    enable_memory = ctx_settings.get("enable_memory", True)
    log_str = get_session_log_str(session_id, session_config, plugin.session_data)

    if not enable_memory:
        logger.info(
            f"{_LOG_TAG} {log_str} "
            f"本次主動訊息未帶記憶（無相關記憶或 livingmemory 不可用）。"
        )
        return system_prompt

    memory_top_k = ctx_settings.get("memory_top_k", 5)
    memory_query = ""
    if ctx_task:
        memory_query = ctx_task.get("hint", "") or ctx_task.get("reason", "")
    if not memory_query:
        memory_query = datetime.now(plugin.timezone).strftime("%Y年%m月%d日 %H:%M")

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


async def finalize_and_reschedule(
    plugin: ProactiveChatPlugin,
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
        await plugin.context.conversation_manager.add_message_pair(
            cid=conv_id,
            user_message=UserMessageSegment(content=[TextPart(text=user_prompt)]),
            assistant_message=AssistantMessageSegment(
                content=[TextPart(text=assistant_response)]
            ),
        )
    except Exception as e:
        logger.error(f"{_LOG_TAG} 存檔對話歷史失敗: {e}")

    async with plugin.data_lock:
        sd = plugin.session_data.setdefault(session_id, {})
        sd["unanswered_count"] = unanswered_count + 1

        # 私聊：安排下一次；群聊由沉默計時器自行處理
        if not is_group_session_id(session_id):
            session_config = get_session_config(plugin.config, session_id)
            if session_config:
                schedule_conf = session_config.get("schedule_settings", {})
                interval = compute_weighted_interval(schedule_conf, plugin.timezone)
                run_date = plugin._add_scheduled_job(session_id, interval)
                sd["next_trigger_time"] = time.time() + interval
                logger.info(
                    f"{_LOG_TAG} 已為 "
                    f"{get_session_log_str(session_id, session_config, plugin.session_data)} "
                    f"安排下一次主動訊息: {run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
                )
        await plugin._save_data()
