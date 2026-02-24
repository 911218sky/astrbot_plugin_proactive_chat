# core/context_predictor.py — 基於 LLM 的語境感知主動訊息時機預測
"""
利用 LLM 分析對話語境，動態預測下一次主動訊息的最佳延遲時間。
同時判斷已排定的語境任務是否應該取消（例如用戶說「看完了」）。
"""

from __future__ import annotations

import json
import re

from astrbot.api import logger

_LOG_TAG = "[主動訊息][語境預測]"

# 預測下一次主動訊息時機的 Prompt 模板
PREDICT_TIMING_PROMPT = """\
你正在分析一段聊天對話，以判斷最佳的主動跟進訊息發送時機。

最近的對話記錄（最後幾條訊息）：
{recent_messages}

當前時間：{current_time}

用戶最新的訊息：「{last_message}」
{cancelled_context}
請根據對話語境判斷：
1. 是否適合安排一條主動跟進訊息
2. 如果是，應該等待多少分鐘後發送
3. 跟進訊息應該聊什麼內容

請參考以下模式：
- 「我在看電影」→ 約 90-120 分鐘（問電影好不好看）
- 「晚安」/「我去睡了」→ 約 420-540 分鐘（早晨問候）
- 「我去開會了」→ 約 30-90 分鐘（關心會議情況）
- 「在通勤」/「在路上」→ 約 20-60 分鐘（問是否到了）
- 「吃飯」/「吃午餐」→ 約 30-60 分鐘（輕鬆跟進）
- 「在工作」/「忙」→ 約 60-180 分鐘（稍後關心）
- 普通閒聊、沒有明確活動 → 使用預設排程（回傳 should_schedule: false）

【重要】以下情況必須回傳 should_schedule: false：
- 用戶表示某個活動已經結束（如「吃飽了」「看完了」「到了」「開完會了」「忙完了」）
- 用戶的訊息是對之前活動的收尾或總結，而非開始新活動
- 剛剛才因為用戶的新訊息取消了一個排程任務（表示語境已轉移，不需要再排）

你必須只回傳一個 JSON 物件，不要有其他文字：
{{
  "should_schedule": true/false,
  "delay_minutes": <數字>,
  "reason": "<簡短原因>",
  "message_hint": "<跟進訊息應該說什麼>"
}}

如果語境沒有暗示特定的時機，請回傳 should_schedule: false。
"""

# 檢查已排定任務是否應該取消的 Prompt 模板
CHECK_CANCEL_PROMPT = """\
你正在檢查一個之前排定的主動訊息是否應該被取消。

排定此任務的原因：「{task_reason}」
排定的跟進提示：「{task_hint}」

用戶剛剛說了：「{last_message}」

請判斷用戶的新訊息是否表示活動已結束，或排定的跟進已不再需要。

取消的範例：
- 任務是「問電影好不好看」，用戶說「電影看完了」→ 取消
- 任務是「早晨問候」，用戶在早上主動發了訊息 → 取消
- 任務是「問是否到了」，用戶說「到了」→ 取消
- 用戶開始了新的話題 → 取消（語境已轉移）

你必須只回傳一個 JSON 物件：
{{
  "should_cancel": true/false,
  "reason": "<簡短原因>"
}}
"""


def build_recent_messages_str(history: list, max_messages: int = 10) -> str:
    """從對話歷史中提取最近的訊息，用於語境分析。"""
    if not history:
        return "（無最近訊息）"

    recent = (
        history[-max_messages:]
        if max_messages > 0 and len(history) > max_messages
        else history
    )
    lines: list[str] = []
    for msg in recent:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # 從結構化內容中提取文字部分
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = " ".join(text_parts)
        if isinstance(content, str) and content.strip():
            label = "用戶" if role == "user" else "助手"
            # 截斷過長的訊息
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"{label}: {content}")

    return "\n".join(lines) if lines else "（無最近訊息）"


def _parse_json_response(text: str) -> dict | None:
    """穩健地從 LLM 回應中解析 JSON，處理 markdown 程式碼區塊。"""
    if not text:
        return None
    # 移除 markdown 程式碼區塊標記
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        # 嘗試在文字中尋找 JSON 物件
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                pass
    logger.warning(f"{_LOG_TAG} 無法解析 LLM 的 JSON 回應: {text[:200]}")
    return None


async def predict_proactive_timing(
    *,
    context,
    session_id: str,
    last_message: str,
    history: list,
    current_time_str: str,
    config: dict,
    just_cancelled_reason: str = "",
) -> dict | None:
    """
    呼叫 LLM 預測下一次主動訊息的最佳時機。

    Args:
        just_cancelled_reason: 若剛才因為這條訊息取消了一個語境任務，
            傳入被取消任務的原因，讓 LLM 知道語境已轉移。

    Returns:
        包含 should_schedule、delay_minutes、reason、message_hint 的 dict，
        預測失敗或未啟用時回傳 None。
    """
    if not last_message or not last_message.strip():
        return None

    max_context_messages = config.get("max_context_messages", 10)
    recent_str = build_recent_messages_str(history, max_context_messages)

    cancelled_context = ""
    if just_cancelled_reason:
        cancelled_context = (
            f"（注意：剛才因為這條訊息取消了一個排程任務，"
            f"被取消的原因是「{just_cancelled_reason}」。"
            f"這表示之前的活動已結束或語境已轉移。）\n"
        )

    prompt = PREDICT_TIMING_PROMPT.format(
        recent_messages=recent_str,
        current_time=current_time_str,
        last_message=last_message.strip(),
        cancelled_context=cancelled_context,
    )

    try:
        provider_id = await context.get_current_chat_provider_id(session_id)
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt="你是一個時機預測助手。請只回傳要求的 JSON 物件。",
        )
        if not resp or not resp.completion_text:
            return None

        result = _parse_json_response(resp.completion_text)
        if not result:
            return None

        # 驗證並限制 delay_minutes 的範圍
        if result.get("should_schedule"):
            delay = result.get("delay_minutes", 0)
            min_delay = config.get("min_delay_minutes", 5)
            max_delay = config.get("max_delay_minutes", 720)
            try:
                delay = max(min_delay, min(max_delay, int(float(delay))))
            except (ValueError, TypeError):
                delay = 60
            result["delay_minutes"] = delay

        logger.info(
            f"{_LOG_TAG} {session_id} 的預測結果: "
            f"排程={result.get('should_schedule')}, "
            f"延遲={result.get('delay_minutes')}分鐘, "
            f"原因={result.get('reason', '無')}"
        )
        return result

    except Exception as e:
        logger.error(f"{_LOG_TAG} 預測 LLM 呼叫失敗: {e}")
        return None


async def check_should_cancel_task(
    *,
    context,
    session_id: str,
    last_message: str,
    task_reason: str,
    task_hint: str,
) -> bool:
    """
    呼叫 LLM 檢查已排定的語境預測任務是否應該取消。

    Returns:
        True 表示應該取消，False 表示保留。
    """
    if not last_message or not last_message.strip():
        return False

    prompt = CHECK_CANCEL_PROMPT.format(
        task_reason=task_reason or "主動跟進",
        task_hint=task_hint or "關心用戶近況",
        last_message=last_message.strip(),
    )

    try:
        provider_id = await context.get_current_chat_provider_id(session_id)
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt="你是一個任務取消判斷助手。請只回傳要求的 JSON 物件。",
        )
        if not resp or not resp.completion_text:
            return False

        result = _parse_json_response(resp.completion_text)
        if not result:
            return False

        should_cancel = bool(result.get("should_cancel", False))
        if should_cancel:
            logger.info(
                f"{_LOG_TAG} 建議取消 {session_id} 的語境任務: "
                f"{result.get('reason', '無')}"
            )
        return should_cancel

    except Exception as e:
        logger.error(f"{_LOG_TAG} 取消檢查 LLM 呼叫失敗: {e}")
        return False
