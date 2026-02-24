# core/context_predictor.py — 基於 LLM 的語境感知主動訊息時機預測
"""
利用 LLM 分析對話語境，動態預測下一次主動訊息的最佳延遲時間。
同時判斷已排定的語境任務是否應該取消（例如用戶說「看完了」）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

_LOG_TAG = "[主動訊息][語境預測]"

# ── Prompt 模板從檔案載入 ──────────────────────────────────
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(filename: str) -> str:
    """從 core/prompts/ 載入 prompt 模板檔案。"""
    path = _PROMPTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"{_LOG_TAG} 找不到 prompt 檔案: {path}")
    return path.read_text(encoding="utf-8").strip()


PREDICT_TIMING_PROMPT = _load_prompt("predict_timing.txt")
PREDICT_TIMING_SYSTEM = _load_prompt("predict_timing_system.txt")
CHECK_CANCEL_PROMPT = _load_prompt("check_cancel.txt")
CHECK_CANCEL_SYSTEM = _load_prompt("check_cancel_system.txt")


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
    context: Context,
    session_id: str,
    last_message: str,
    history: list,
    current_time_str: str,
    config: dict,
    just_cancelled_reason: str = "",
    llm_provider_id: str = "",
    extra_prompt: str = "",
) -> dict | None:
    """
    呼叫 LLM 預測下一次主動訊息的最佳時機。

    Args:
        just_cancelled_reason: 若剛才因為這條訊息取消了一個語境任務，
            傳入被取消任務的原因，讓 LLM 知道語境已轉移。
        llm_provider_id: 指定 LLM 平台 ID，留空則使用會話預設。
        extra_prompt: 使用者自訂的補充提示，會附加到 prompt 末尾。

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

    # 附加使用者自訂的補充提示
    if extra_prompt and extra_prompt.strip():
        prompt += f"\n\n[補充指示]\n{extra_prompt.strip()}"

    try:
        # 若指定了 LLM 平台 ID 則使用，否則使用會話預設
        provider_id = (
            llm_provider_id.strip()
            if llm_provider_id and llm_provider_id.strip()
            else await context.get_current_chat_provider_id(session_id)
        )
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=PREDICT_TIMING_SYSTEM,
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

        should = result.get("should_schedule")
        delay = result.get("delay_minutes")
        reason = result.get("reason", "無")

        if should and delay:
            trigger_at = datetime.now() + timedelta(minutes=delay)
            logger.info(
                f"{_LOG_TAG} {session_id} 的預測結果: "
                f"排程={should}, "
                f"延遲={delay}分鐘, "
                f"預計觸發時間={trigger_at.strftime('%Y-%m-%d %H:%M:%S')}, "
                f"原因={reason}"
            )
        else:
            logger.info(
                f"{_LOG_TAG} {session_id} 的預測結果: 排程={should}, 原因={reason}"
            )
        return result

    except Exception as e:
        logger.error(f"{_LOG_TAG} 預測 LLM 呼叫失敗: {e}")
        return None


async def check_should_cancel_task(
    *,
    context: Context,
    session_id: str,
    last_message: str,
    task_reason: str,
    task_hint: str,
    llm_provider_id: str = "",
) -> bool:
    """
    呼叫 LLM 檢查已排定的語境預測任務是否應該取消。

    Args:
        llm_provider_id: 指定 LLM 平台 ID，留空則使用會話預設。

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
        # 若指定了 LLM 平台 ID 則使用，否則使用會話預設
        provider_id = (
            llm_provider_id.strip()
            if llm_provider_id and llm_provider_id.strip()
            else await context.get_current_chat_provider_id(session_id)
        )
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=CHECK_CANCEL_SYSTEM,
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
