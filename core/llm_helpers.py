# core/llm_helpers.py — LLM 請求準備、記憶整合、呼叫封裝
"""
將 LLM 相關的輔助邏輯從 main.py 中抽離，包括：
- LLM 請求上下文準備（對話歷史、system prompt）
- livingmemory 記憶檢索與注入
- LLM 呼叫（主要路徑 + 備用路徑）
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from astrbot.api import logger

from .utils import parse_session_id

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

    from ...astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

_LOG_TAG = "[主動訊息]"


def get_livingmemory_engine(context: Context) -> MemoryEngine | None:
    """嘗試取得 livingmemory 插件的 MemoryEngine 實例。

    Returns:
        MemoryEngine 實例，找不到或未初始化時回傳 None。
    """
    try:
        for star_meta in context.get_all_stars():
            if (
                star_meta.root_dir_name == "astrbot_plugin_livingmemory"
                and star_meta.activated
                and star_meta.star_cls is not None
            ):
                initializer = getattr(star_meta.star_cls, "initializer", None)
                if initializer and getattr(initializer, "is_initialized", False):
                    engine: MemoryEngine | None = getattr(
                        initializer, "memory_engine", None
                    )
                    if engine:
                        return engine
                else:
                    logger.info(
                        f"{_LOG_TAG} livingmemory 插件尚未完成初始化，跳過記憶檢索。"
                    )
    except Exception as e:
        logger.info(f"{_LOG_TAG} 取得 livingmemory 引擎失敗: {e}")
    return None


async def recall_memories_for_proactive(
    context: Context,
    session_id: str,
    query: str,
    memory_top_k: int = 5,
) -> str:
    """檢索 livingmemory 中與主動訊息相關的記憶，格式化為可注入的文字。

    Args:
        context: 插件 context（用於取得 livingmemory 引擎）
        session_id: 會話 ID（unified_msg_origin）
        query: 檢索用的查詢字串（通常是語境提示或 proactive_prompt 摘要）
        memory_top_k: 最多回傳幾條記憶。0 表示停用。

    Returns:
        格式化的記憶文字，無記憶或 livingmemory 不可用時回傳空字串。
    """
    if memory_top_k <= 0:
        logger.info(f"{_LOG_TAG} memory_top_k={memory_top_k}，已停用記憶檢索。")
        return ""

    engine = get_livingmemory_engine(context)
    if not engine:
        logger.info(f"{_LOG_TAG} livingmemory 不可用，跳過記憶檢索。")
        return ""

    try:
        results = await engine.search_memories(
            query=query,
            k=memory_top_k,
            session_id=session_id,
        )
        if not results:
            logger.info(f"{_LOG_TAG} livingmemory 中未找到 {session_id} 的相關記憶。")
            return ""

        lines = ["[相關記憶（來自長期記憶）]"]
        for i, mem in enumerate(results, 1):
            content = mem.content if hasattr(mem, "content") else str(mem)
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"- 記憶 {i}: {content}")

        memory_str = "\n".join(lines)
        logger.info(f"{_LOG_TAG} 已為 {session_id} 檢索到 {len(results)} 條相關記憶。")
        return memory_str

    except Exception as e:
        logger.info(f"{_LOG_TAG} 記憶檢索失敗（不影響主動訊息）: {e}")
        return ""


async def prepare_llm_request(context: Context, session_id: str) -> dict | None:
    """準備 LLM 請求所需的上下文。

    Returns:
        包含 conv_id、history、system_prompt 的 dict，
        若無法取得則回傳 None。
    """
    try:
        conv_id = await context.conversation_manager.get_curr_conversation_id(
            session_id
        )
        if not conv_id:
            try:
                conv_id = await context.conversation_manager.new_conversation(
                    session_id
                )
            except ValueError:
                raise
            except Exception as e:
                logger.error(f"{_LOG_TAG} 創建新對話失敗: {e}")
                return None
        if not conv_id:
            return None

        conversation = await context.conversation_manager.get_conversation(
            session_id, conv_id
        )

        history: list = []
        if conversation and conversation.history:
            try:
                history = (
                    json.loads(conversation.history)
                    if isinstance(conversation.history, str)
                    else conversation.history
                )
            except (json.JSONDecodeError, TypeError):
                pass

        system_prompt = await resolve_system_prompt(context, conversation, session_id)
        if not system_prompt:
            logger.error(f"{_LOG_TAG} 無法加載任何人格設定，放棄。")
            return None

        return {
            "conv_id": conv_id,
            "history": history,
            "system_prompt": system_prompt,
        }
    except Exception as e:
        logger.warning(f"{_LOG_TAG} 獲取上下文或人格失敗: {e}")
        return None


async def resolve_system_prompt(context: Context, conversation, session_id: str) -> str:
    """依序嘗試取得 system prompt。

    優先順序：對話綁定的人格 → AstrBot 預設人格。
    """
    if conversation and conversation.persona_id:
        persona = await context.persona_manager.get_persona(conversation.persona_id)
        if persona and persona.system_prompt:
            return persona.system_prompt

    default_persona = await context.persona_manager.get_default_persona_v3(
        umo=session_id
    )
    return default_persona["prompt"] if default_persona else ""


async def safe_prepare_llm_request(context: Context, session_id: str) -> dict | None:
    """準備 LLM 請求，自動處理 UMO 格式相容問題。

    某些 AstrBot 版本的 conversation_manager 對 UMO 格式有嚴格要求，
    若首次呼叫失敗且為 ValueError，會嘗試用標準三段式格式重試。
    """
    try:
        return await prepare_llm_request(context, session_id)
    except ValueError as e:
        if "too many values" not in str(e) and "expected 3" not in str(e):
            raise
        parsed = parse_session_id(session_id)
        if parsed:
            return await prepare_llm_request(
                context, f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
            )
        raise


async def call_llm(
    context: Context,
    session_id: str,
    prompt: str,
    contexts: list,
    system_prompt: str,
):
    """呼叫 LLM 生成回應。

    主要路徑：透過 ``llm_generate`` API。
    備用路徑：若主要路徑失敗，回退到 ``get_using_provider().text_chat()``。
    """
    try:
        provider_id = await context.get_current_chat_provider_id(session_id)
        return await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
        )
    except Exception as llm_err:
        logger.error(f"{_LOG_TAG} LLM 調用失敗: {llm_err}")
        try:
            provider = context.get_using_provider(umo=session_id)
            if provider:
                return await provider.text_chat(
                    prompt=prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                )
        except Exception:
            pass
        return None
