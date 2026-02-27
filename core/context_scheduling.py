# core/context_scheduling.py — 語境感知排程
"""
語境感知排程的所有邏輯，包括：
- LLM 預測主動訊息時機並建立排程
- 檢查並取消不再需要的語境任務
- 語境預測任務的建立與移除
- 從持久化數據恢復語境任務

所有函數接收插件實例 ``plugin`` 作為第一個參數，
以存取共享狀態（scheduler、session_data、data_lock 等）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .config import get_session_config
from .context_predictor import (
    check_should_cancel_tasks_batch,
    predict_proactive_timing,
)
from .llm_helpers import load_conversation_history
from .messaging import sanitize_history_content
from .utils import get_session_log_str

if TYPE_CHECKING:
    from ..main import ProactiveChatPlugin

_LOG_TAG = "[主動訊息]"


async def handle_context_aware_scheduling(
    plugin: ProactiveChatPlugin,
    session_id: str,
    message_text: str,
    ctx_settings: dict,
) -> None:
    """
    背景任務：檢查待執行的語境任務並執行 LLM 預測。

    步驟：
    1. 並行執行：取消檢查（所有待執行任務同時檢查）+ 取得對話歷史
    2. 根據最新訊息執行 LLM 時機預測
    3. 若預測結果建議排程，建立一次性任務
    """
    try:
        # 步驟 1：並行執行取消檢查與歷史取得，減少等待時間
        cancel_coro = maybe_cancel_pending_context_task(
            plugin, session_id, message_text
        )
        history_coro = get_history_for_prediction(plugin, session_id)
        cancelled_reason, history = await asyncio.gather(cancel_coro, history_coro)

        now_str = datetime.now(plugin.timezone).strftime("%Y年%m月%d日 %H:%M")

        # 步驟 2：呼叫 LLM 預測時機（若剛取消了任務，傳入原因讓 LLM 知道語境已轉移）
        prediction = await predict_proactive_timing(
            context=plugin.context,
            session_id=session_id,
            last_message=message_text,
            history=history,
            current_time_str=now_str,
            config=ctx_settings,
            just_cancelled_reason=cancelled_reason,
            llm_provider_id=ctx_settings.get("llm_provider_id", ""),
            extra_prompt=ctx_settings.get("extra_prompt", ""),
        )

        session_config = get_session_config(plugin.config, session_id)
        log_name = get_session_log_str(session_id, session_config, plugin.session_data)

        if not prediction or not prediction.get("should_schedule"):
            logger.info(
                f"{_LOG_TAG} {log_name} 語境分析完成，LLM 判定目前不需要排程主動訊息。"
            )
            return

        delay_minutes = prediction.get("delay_minutes", 60)
        reason = prediction.get("reason", "")
        hint = prediction.get("message_hint", "")

        run_at = datetime.fromtimestamp(
            time.time() + delay_minutes * 60, tz=plugin.timezone
        )
        logger.info(
            f"{_LOG_TAG} {log_name} "
            f"語境分析完成，LLM 判定需要排程主動訊息，"
            f"預計觸發時間 {run_at.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(+{delay_minutes}分鐘，原因: {reason})"
        )

        # 步驟 3：建立排程任務
        await create_context_predicted_task(
            plugin,
            session_id=session_id,
            delay_minutes=delay_minutes,
            reason=reason,
            hint=hint,
        )

    except Exception as e:
        logger.error(
            f"{_LOG_TAG} handle_context_aware_scheduling 語境感知排程失敗"
            f" | session={session_id}: {e}"
        )


async def maybe_cancel_pending_context_task(
    plugin: ProactiveChatPlugin,
    session_id: str,
    message_text: str,
) -> str:
    """若用戶的新訊息使待執行的語境任務不再需要，則取消該任務。

    使用批量 LLM 請求一次性檢查所有待執行的語境任務。

    Returns:
        被取消任務的原因字串（多個以分號分隔），未取消則回傳空字串。
    """
    task_list = plugin._pending_context_tasks.get(session_id)
    if not task_list:
        return ""

    # 從會話配置中取得語境感知的 LLM 平台 ID
    session_config = get_session_config(plugin.config, session_id)
    ctx_llm_id = ""
    if session_config:
        ctx_llm_id = session_config.get("context_aware_settings", {}).get(
            "llm_provider_id", ""
        )

    # 批量檢查所有待執行任務（一次 LLM 請求）
    cancel_map = await check_should_cancel_tasks_batch(
        context=plugin.context,
        session_id=session_id,
        last_message=message_text,
        tasks=task_list,
        llm_provider_id=ctx_llm_id,
    )

    if not cancel_map:
        return ""

    cancelled_reasons: list[str] = []
    to_remove: list[dict] = []

    # 處理取消結果
    for idx, (should_cancel, reason) in cancel_map.items():
        if should_cancel and 0 <= idx < len(task_list):
            task = task_list[idx]
            to_remove.append(task)
            cancelled_reasons.append(task.get("reason", ""))
            logger.info(
                f"{_LOG_TAG} 已取消 "
                f"{get_session_log_str(session_id, None, plugin.session_data)} "
                f"的語境預測任務 ({task.get('job_id', '')})：{reason}"
            )

    # 批次移除被取消的任務
    for task in to_remove:
        job_id = task.get("job_id", "")
        try:
            if plugin.scheduler.get_job(job_id):
                plugin.scheduler.remove_job(job_id)
        except Exception as e:
            logger.debug(
                f"{_LOG_TAG} maybe_cancel_pending_context_task 移除排程任務失敗"
                f" | session={session_id}, job_id={job_id}: {e}"
            )
        task_list.remove(task)

    # 清理空列表
    if not task_list:
        plugin._pending_context_tasks.pop(session_id, None)

    # 更新持久化
    if to_remove:
        async with plugin.data_lock:
            sd = plugin.session_data.get(session_id)
            if sd:
                if task_list:
                    sd["pending_context_tasks"] = task_list
                else:
                    sd.pop("pending_context_tasks", None)
                    sd.pop("pending_context_task", None)
                await plugin._save_data()

    return "; ".join(cancelled_reasons)


def remove_context_predicted_task(
    plugin: ProactiveChatPlugin,
    session_id: str,
    job_id: str,
) -> None:
    """從本地排程器和追蹤中移除指定的語境預測任務。"""
    task_list = plugin._pending_context_tasks.get(session_id)
    if task_list:
        plugin._pending_context_tasks[session_id] = [
            t for t in task_list if t.get("job_id") != job_id
        ]
        if not plugin._pending_context_tasks[session_id]:
            plugin._pending_context_tasks.pop(session_id, None)

    try:
        if job_id and plugin.scheduler.get_job(job_id):
            plugin.scheduler.remove_job(job_id)
    except Exception as e:
        logger.debug(
            f"{_LOG_TAG} remove_context_predicted_task 移除排程任務失敗"
            f" | session={session_id}, job_id={job_id}: {e}"
        )


async def create_context_predicted_task(
    plugin: ProactiveChatPlugin,
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
        time.time() + delay_minutes * 60, tz=plugin.timezone
    )

    # 生成唯一 job_id
    plugin._ctx_task_counter += 1
    ctx_job_id = f"ctx_{session_id}_{plugin._ctx_task_counter}"

    plugin.scheduler.add_job(
        plugin.check_and_chat,
        "date",
        run_date=run_at,
        args=[session_id],
        kwargs={"ctx_job_id": ctx_job_id},
        id=ctx_job_id,
        replace_existing=True,
        misfire_grace_time=120,
    )

    session_config = get_session_config(plugin.config, session_id)
    logger.info(
        f"{_LOG_TAG} 已為 "
        f"{get_session_log_str(session_id, session_config, plugin.session_data)} "
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
    task_list = plugin._pending_context_tasks.setdefault(session_id, [])
    task_list.append(task_info)

    # 持久化到 session_data
    async with plugin.data_lock:
        sd = plugin.session_data.setdefault(session_id, {})
        sd["pending_context_tasks"] = task_list
        sd.pop("pending_context_task", None)  # 清理舊格式
        await plugin._save_data()


async def get_history_for_prediction(
    plugin: ProactiveChatPlugin, session_id: str
) -> list:
    """取得最近的對話歷史，用於語境預測。"""
    _, history = await load_conversation_history(plugin.context, session_id)
    return sanitize_history_content(history) if history else []


def restore_pending_context_tasks(plugin: ProactiveChatPlugin) -> bool:
    """從持久化的 session_data 中恢復語境預測的待執行任務。

    注意：此函數為同步函數，在 initialize() 中呼叫。

    Returns:
        True 表示有過期任務被清理或舊格式被移除，需要持久化；False 表示無需持久化。
    """
    restored = 0
    needs_save = False
    now = time.time()
    for sid, info in plugin.session_data.items():
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
        # 檢查是否有任務被過濾掉（過期任務被清理）
        if len(valid_tasks) != len(task_list):
            needs_save = True
        if valid_tasks:
            plugin._pending_context_tasks[sid] = valid_tasks
        # 清理舊格式的持久化 key
        if "pending_context_task" in info:
            info.pop("pending_context_task", None)
            needs_save = True
        if valid_tasks:
            info["pending_context_tasks"] = valid_tasks
        else:
            if "pending_context_tasks" in info:
                info.pop("pending_context_tasks", None)
                needs_save = True
    if restored:
        logger.info(f"{_LOG_TAG} 已恢復 {restored} 個語境預測的待執行任務。")
    return needs_save
