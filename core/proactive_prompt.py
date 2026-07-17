from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .delivery import AcceptedTurn, DispatchGate, GateVerdict, accepted_turn_text
from .config import get_session_config
from .llm_helpers import (
    NonRetryableLLMError,
    call_llm,
    recall_memories_for_proactive,
    safe_prepare_llm_request,
    truncate_history_for_proactive_llm,
)
from .messaging import sanitize_history_content
from .proactive_state import (
    active_task_description,
    find_context_task,
    format_elapsed_duration,
    format_first_interaction_time,
    format_last_reply_time,
    is_habit_job,
)
from .utils import get_session_log_str

if TYPE_CHECKING:
    from ..main import ProactiveChatPlugin

_LOG_TAG = "[主動訊息]"
_INVALID_RESPONSES = frozenset({"[object Object]"})
_CONTROLLER_PROMPT = (
    "判斷是否應立即補充一則自然且不重複的訊息。只輸出完整 JSON："
    '{"send_follow_up":true|false,"message":"..."}。'
    "不需要補充時 message 必須是空字串。已接受的助理訊息："
)
_MESSAGE_PROMPT = (
    "請根據目前對話自然地補充一則不重複的訊息。你已經被隨機策略選中，"
    "不要判斷是否追加，必須產生一則訊息。只輸出完整 JSON："
    '{"send_follow_up":true,"message":"..."}。已接受的助理訊息：'
)
_RELATIONSHIP_CONTEXT = (
    "\n\n[關係時間感知]\n"
    "- 這個會話第一次被你記錄到互動的時間：{first_interaction_time}\n"
    "- 從第一次互動到現在大約經過：{relationship_duration}\n"
    "請把這個資訊當成背景感受來調整熟悉程度、關心方式和話題深度；"
    "不要生硬地報出精確時間，除非使用者正在詢問。"
)


async def prepare_and_call_llm(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    ctx_job_id: str,
) -> tuple[str, str, str, dict | None] | None:
    snapshot_last_msg = plugin.last_message_times.get(session_id, 0)
    request = await safe_prepare_llm_request(plugin.context, session_id)
    if not request:
        if not is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None
    final_prompt, ctx_task = build_final_prompt(
        plugin,
        session_id,
        session_config,
        unanswered_count,
        snapshot_last_msg,
        ctx_job_id,
    )
    system_prompt = await inject_memory(
        plugin,
        session_id,
        session_config,
        ctx_task,
        request["system_prompt"],
        final_prompt,
    )
    history = sanitize_history_content(request["history"])
    history = await truncate_history_for_proactive_llm(
        plugin.context, session_id, history
    )
    try:
        response = await call_llm(
            plugin.context, session_id, final_prompt, history, system_prompt
        )
    except NonRetryableLLMError as error:
        logger.error(f"{_LOG_TAG} LLM 不可重試錯誤 | session={session_id}: {error}")
        if not is_habit_job(ctx_job_id):
            await plugin._clear_regular_job_state(session_id)
        return None
    completion = getattr(response, "completion_text", None)
    response_text = completion.strip() if isinstance(completion, str) else ""
    if not response_text or response_text in _INVALID_RESPONSES:
        if not is_habit_job(ctx_job_id):
            await plugin._schedule_next_chat_and_save(session_id)
        return None
    return response_text, request["conv_id"], final_prompt, ctx_task


async def _request_follow_up_completion(
    plugin: ProactiveChatPlugin,
    session_id: str,
    accepted_turns: tuple[AcceptedTurn, ...],
    gate: DispatchGate,
    controller_prompt: str,
) -> str | None:
    if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
        return None
    request = await safe_prepare_llm_request(plugin.context, session_id)
    if plugin._gate_verdict(gate) is not GateVerdict.CURRENT or not request:
        return None
    history = sanitize_history_content(request["history"])
    history.extend(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": accepted_turn_text(turn)}],
        }
        for turn in accepted_turns
    )
    history = await truncate_history_for_proactive_llm(
        plugin.context, session_id, history
    )
    if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
        return None
    prompt = controller_prompt + json.dumps(
        [accepted_turn_text(turn) for turn in accepted_turns], ensure_ascii=False
    )
    session_config = get_session_config(plugin.config, session_id) or {}
    context_settings = session_config.get("context_aware_settings", {})
    provider_id = (
        context_settings.get("llm_provider_id")
        if isinstance(context_settings, dict)
        else None
    )
    try:
        response = await call_llm(
            plugin.context,
            session_id,
            prompt,
            history,
            request["system_prompt"],
            provider_id=provider_id if isinstance(provider_id, str) else None,
        )
    except NonRetryableLLMError:
        return None
    if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
        return None
    completion = getattr(response, "completion_text", None)
    return completion.strip() if isinstance(completion, str) else None


async def request_follow_up_decision(
    plugin: ProactiveChatPlugin,
    session_id: str,
    accepted_turns: tuple[AcceptedTurn, ...],
    gate: DispatchGate,
) -> str | None:
    return await _request_follow_up_completion(
        plugin, session_id, accepted_turns, gate, _CONTROLLER_PROMPT
    )


async def request_follow_up_message(
    plugin: ProactiveChatPlugin,
    session_id: str,
    accepted_turns: tuple[AcceptedTurn, ...],
    gate: DispatchGate,
) -> str | None:
    return await _request_follow_up_completion(
        plugin, session_id, accepted_turns, gate, _MESSAGE_PROMPT
    )


def build_final_prompt(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    snapshot_last_msg: float,
    ctx_job_id: str,
) -> tuple[str, dict | None]:
    template = session_config.get("proactive_prompt", "")
    first_value = plugin.session_data.get(session_id, {}).get("first_interaction_time")
    first_text = format_first_interaction_time(first_value, plugin.timezone)
    duration_text = format_elapsed_duration(first_value)
    prompt = (
        template.replace("{{unanswered_count}}", str(unanswered_count))
        .replace(
            "{{current_time}}",
            datetime.now(plugin.timezone).strftime("%Y年%m月%d日 %H:%M"),
        )
        .replace(
            "{{last_reply_time}}",
            format_last_reply_time(snapshot_last_msg, plugin.timezone),
        )
        .replace("{{first_interaction_time}}", first_text)
        .replace("{{relationship_duration}}", duration_text)
    )
    prompt += _RELATIONSHIP_CONTEXT.format(
        first_interaction_time=first_text,
        relationship_duration=duration_text,
    )
    context_task = find_context_task(plugin, session_id, ctx_job_id)
    if context_task:
        prompt += (
            "\n\n[語境感知觸發]\n"
            f"這條主動訊息的排程原因：{str(context_task.get('reason', '')).strip()}\n"
            f"建議的跟進話題：{str(context_task.get('hint', '')).strip()}\n"
        )
        description = str(context_task.get("description", "")).strip()
        if description:
            prompt += f"任務補充描述：{description}\n"
        prompt += "請將這個語境自然地融入你的訊息中。"
        return prompt, context_task
    habit_task = plugin._find_habit_task(session_id, ctx_job_id)
    if habit_task:
        prompt += (
            "\n\n[習慣時段觸發]\n現在符合你的日常習慣時段："
            f"{str(habit_task.get('rule_name', '') or '習慣時段')}。\n"
        )
        for key, label in (("hint", "建議的情緒或話題"), ("description", "補充描述")):
            value = str(habit_task.get(key, "") or "").strip()
            if value:
                prompt += f"{label}：{value}\n"
        prompt += "請像平常這個時間自然出現一樣回覆，不要提到排程或設定。"
    description = "" if habit_task else active_task_description(plugin, session_id)
    if description:
        prompt += f"\n\n[排程任務描述]\n{description}\n請將這個任務描述自然地融入你的主動訊息中。"
    return prompt, None


async def inject_memory(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    context_task: dict | None,
    system_prompt: str,
    final_prompt: str,
) -> str:
    settings = session_config.get("context_aware_settings", {})
    if not settings.get("enable_memory", True):
        return system_prompt
    query = ""
    if context_task:
        query = (
            context_task.get("description", "")
            or context_task.get("hint", "")
            or context_task.get("reason", "")
        )
    memory = await recall_memories_for_proactive(
        plugin.context,
        session_id,
        query or final_prompt.strip(),
        memory_top_k=settings.get("memory_top_k", 5),
    )
    log = get_session_log_str(session_id, session_config, plugin.session_data)
    if memory:
        logger.info(f"{_LOG_TAG} 已為 {log} 注入記憶到主動訊息 system_prompt。")
        return system_prompt + "\n\n" + memory
    logger.info(f"{_LOG_TAG} {log} 本次主動訊息未帶記憶。")
    return system_prompt
