# core/llm_helpers.py — LLM 請求準備、記憶整合、呼叫封裝
"""
將 LLM 相關的輔助邏輯從 main.py 中抽離，包括：
- LLM 請求上下文準備（對話歷史、system prompt）
- livingmemory 記憶檢索與注入
- LLM 呼叫（主要路徑 + 備用路徑）
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from astrbot.api import logger, sp
from astrbot.core.agent.context.truncator import ContextTruncator
from astrbot.core.agent.message import Message

from .utils import async_with_umo_fallback

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

    from ...astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

_LOG_TAG = "[主動訊息]"
_LIVINGMEMORY_ROOT = "astrbot_plugin_livingmemory"
_LIVINGMEMORY_NAMES = {
    "livingmemory",
    "astrbot_plugin_livingmemory",
}
_SQLITE_LOCK_KEYWORDS = frozenset({"database is locked", "database table is locked"})
_AUTH_ERROR_KEYWORDS = frozenset(
    {"authentication", "auth", "unauthorized", "forbidden"}
)


class CoreHistoryBusy(RuntimeError):
    """AstrBot Core conversation DB is temporarily busy."""


class NonRetryableLLMError(RuntimeError):
    """LLM 發生不可重試錯誤，避免主動訊息排程無限重試。"""


def _is_sqlite_lock_error(exc: Exception) -> bool:
    return any(keyword in str(exc).lower() for keyword in _SQLITE_LOCK_KEYWORDS)


def _is_auth_error(exc: Exception) -> bool:
    error_text = f"{type(exc).__name__} {exc}".lower()
    return any(keyword in error_text for keyword in _AUTH_ERROR_KEYWORDS)


async def _retry_core_conversation_call(
    label: str,
    session_id: str,
    call,
    *,
    attempts: int = 3,
) -> Any:
    """Retry short Core conversation-manager operations when SQLite is busy."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as e:
            if not _is_sqlite_lock_error(e):
                raise
            if attempt >= attempts:
                raise CoreHistoryBusy(str(e)) from e
            await asyncio.sleep(min(0.4 * attempt, 1.5))


def _is_livingmemory_star(star_meta: Any) -> bool:
    """Return True when the metadata looks like the LivingMemory plugin."""
    root_dir_name = str(getattr(star_meta, "root_dir_name", "") or "").lower()
    star_name = str(getattr(star_meta, "name", "") or "").lower()
    return root_dir_name == _LIVINGMEMORY_ROOT or star_name in _LIVINGMEMORY_NAMES


def _engine_from_initializer(initializer: Any) -> MemoryEngine | None:
    if not initializer or not getattr(initializer, "is_initialized", False):
        return None

    engine: MemoryEngine | None = getattr(initializer, "memory_engine", None)
    return engine if engine is not None else None


def _find_livingmemory_initializer(context: Context) -> Any | None:
    for star_meta in context.get_all_stars():
        if (
            _is_livingmemory_star(star_meta)
            and getattr(star_meta, "activated", False)
            and getattr(star_meta, "star_cls", None) is not None
        ):
            return getattr(star_meta.star_cls, "initializer", None)
    return None


def get_livingmemory_engine(context: Context) -> MemoryEngine | None:
    """嘗試取得 livingmemory 插件的 MemoryEngine 實例。

    Returns:
        MemoryEngine 實例，找不到或未初始化時回傳 None。
    """
    try:
        initializer = _find_livingmemory_initializer(context)
        engine = _engine_from_initializer(initializer)
        if engine:
            return engine
        if initializer:
            logger.info(
                f"{_LOG_TAG} livingmemory 插件尚未完成初始化，暫時跳過記憶檢索。"
            )
    except Exception as e:
        logger.info(f"{_LOG_TAG} 取得 livingmemory 引擎失敗: {e}")
    return None


async def get_livingmemory_engine_async(context: Context) -> MemoryEngine | None:
    """取得 livingmemory 的 MemoryEngine，必要時短暫等待其初始化完成。"""
    engine = get_livingmemory_engine(context)
    if engine:
        return engine

    try:
        initializer = _find_livingmemory_initializer(context)
        ensure_initialized = getattr(initializer, "ensure_initialized", None)
        if callable(ensure_initialized):
            await ensure_initialized(timeout=10.0)
            engine = _engine_from_initializer(initializer)
            if engine:
                return engine

        if initializer:
            logger.info(
                f"{_LOG_TAG} livingmemory 初始化未就緒，本次主動訊息不注入記憶。"
            )
    except Exception as e:
        logger.info(f"{_LOG_TAG} 取得 livingmemory 引擎失敗: {e}")
    return None


def _get_livingmemory_filtering_settings(context: Context) -> dict[str, Any]:
    initializer = _find_livingmemory_initializer(context)
    config_manager = getattr(initializer, "config_manager", None)
    filtering_settings = getattr(config_manager, "filtering_settings", None)
    return filtering_settings if isinstance(filtering_settings, dict) else {}


def _uses_persona_filtering(context: Context) -> bool:
    filtering_settings = _get_livingmemory_filtering_settings(context)
    return filtering_settings.get("use_persona_filtering", True)


def _uses_session_filtering(context: Context) -> bool:
    filtering_settings = _get_livingmemory_filtering_settings(context)
    return filtering_settings.get("use_session_filtering", True)


def _recall_session_id(context: Context, session_id: str) -> str | None:
    if _uses_session_filtering(context):
        return session_id
    return None


async def resolve_persona_id_for_session(
    context: Context, session_id: str
) -> str | None:
    """依 AstrBot 主流程優先順序取得目前 session 的 persona_id。"""
    try:
        session_service_config = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_service_config",
            default={},
        )
        persona_id = (
            session_service_config.get("persona_id")
            if isinstance(session_service_config, dict)
            else None
        )
        if persona_id:
            return persona_id
    except Exception as e:
        logger.debug(
            f"{_LOG_TAG} 讀取 session_service_config 失敗 | session={session_id}: {e}"
        )

    try:
        conv_id = await _retry_core_conversation_call(
            "get_curr_conversation_id",
            session_id,
            lambda: context.conversation_manager.get_curr_conversation_id(session_id),
        )
        if conv_id:
            conversation = await _retry_core_conversation_call(
                "get_conversation",
                session_id,
                lambda: context.conversation_manager.get_conversation(
                    session_id, conv_id
                ),
            )
            persona_id = getattr(conversation, "persona_id", None)
            if persona_id == "[%None]":
                return None
            if persona_id:
                return persona_id
    except CoreHistoryBusy as e:
        logger.info(
            f"{_LOG_TAG} AstrBot 對話資料庫忙碌，改用預設人格做記憶檢索"
            f" | session={session_id}: {e}"
        )
    except Exception as e:
        logger.debug(
            f"{_LOG_TAG} 讀取對話人格失敗，改用預設人格 | session={session_id}: {e}"
        )

    try:
        default_persona = await context.persona_manager.get_default_persona_v3(
            umo=session_id
        )
        return default_persona["name"] if default_persona else None
    except Exception as e:
        logger.debug(f"{_LOG_TAG} 取得預設 persona_id 失敗 | session={session_id}: {e}")
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
    try:
        memory_top_k = int(memory_top_k)
    except (TypeError, ValueError):
        logger.info(f"{_LOG_TAG} memory_top_k 配置無效，改用預設值 5。")
        memory_top_k = 5

    if memory_top_k <= 0:
        logger.info(f"{_LOG_TAG} memory_top_k={memory_top_k}，已停用記憶檢索。")
        return ""

    engine = await get_livingmemory_engine_async(context)
    if not engine:
        logger.info(f"{_LOG_TAG} livingmemory 不可用，跳過記憶檢索。")
        return ""

    try:
        results = await engine.search_memories(
            query=query,
            k=memory_top_k,
            session_id=_recall_session_id(context, session_id),
            persona_id=(
                await resolve_persona_id_for_session(context, session_id)
                if _uses_persona_filtering(context)
                else None
            ),
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


async def load_conversation_history(
    context: Context,
    session_id: str,
    *,
    raise_on_busy: bool = False,
) -> tuple[str, list]:
    """取得當前對話的 conv_id 與已解析的歷史記錄。

    僅載入既有對話，不會建立新對話。

    Returns:
        (conv_id, history)；無法取得時回傳 ("", [])。
    """
    try:
        conv_id = await _retry_core_conversation_call(
            "get_curr_conversation_id",
            session_id,
            lambda: context.conversation_manager.get_curr_conversation_id(session_id),
        )
        if not conv_id:
            return ("", [])

        conversation = await _retry_core_conversation_call(
            "get_conversation",
            session_id,
            lambda: context.conversation_manager.get_conversation(session_id, conv_id),
        )
        if not conversation or not conversation.history:
            return (conv_id, [])

        history: list = []
        try:
            history = (
                json.loads(conversation.history)
                if isinstance(conversation.history, str)
                else conversation.history
            )
        except (json.JSONDecodeError, TypeError):
            pass

        return (conv_id, history if history else [])
    except CoreHistoryBusy as e:
        if raise_on_busy:
            raise
        logger.info(
            f"{_LOG_TAG} AstrBot 對話歷史忙碌，略過本次讀取 | session={session_id}: {e}"
        )
        return ("", [])
    except Exception as e:
        logger.debug(f"{_LOG_TAG} 載入對話歷史失敗 | session={session_id}: {e}")
        return ("", [])


async def prepare_llm_request(context: Context, session_id: str) -> dict | None:
    """準備 LLM 請求所需的上下文。

    Returns:
        包含 conv_id、history、system_prompt 的 dict，
        若無法取得則回傳 None。
    """
    try:
        conv_id, history = await load_conversation_history(
            context, session_id, raise_on_busy=True
        )
        if not conv_id:
            # 嘗試建立新對話
            try:
                conv_id = await _retry_core_conversation_call(
                    "new_conversation",
                    session_id,
                    lambda: context.conversation_manager.new_conversation(session_id),
                )
            except ValueError:
                raise
            except CoreHistoryBusy as e:
                logger.warning(
                    f"{_LOG_TAG} AstrBot 對話資料庫忙碌，略過本次主動訊息上下文準備"
                    f" | session={session_id}: {e}"
                )
                return None
            except Exception as e:
                logger.error(
                    f"{_LOG_TAG} prepare_llm_request 創建新對話失敗"
                    f" | session={session_id}: {e}"
                )
                return None
        if not conv_id:
            return None

        # 若 load_conversation_history 回傳了 conv_id 但 history 為空，
        # 仍需取得 conversation 以解析 system_prompt
        conversation = await _retry_core_conversation_call(
            "get_conversation",
            session_id,
            lambda: context.conversation_manager.get_conversation(session_id, conv_id),
        )

        system_prompt = await resolve_system_prompt(context, conversation, session_id)
        if not system_prompt:
            logger.error(f"{_LOG_TAG} 無法加載任何人格設定，放棄。")
            return None

        return {
            "conv_id": conv_id,
            "history": history,
            "system_prompt": system_prompt,
        }
    except CoreHistoryBusy as e:
        logger.warning(
            f"{_LOG_TAG} AstrBot 對話資料庫忙碌，略過本次主動訊息上下文準備"
            f" | session={session_id}: {e}"
        )
        return None
    except Exception as e:
        logger.warning(
            f"{_LOG_TAG} prepare_llm_request 獲取上下文或人格失敗"
            f" | session={session_id}: {e}"
        )
        return None


async def resolve_system_prompt(
    context: Context, conversation: Any, session_id: str
) -> str:
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
    return await async_with_umo_fallback(
        lambda sid: prepare_llm_request(context, sid),
        session_id,
    )


async def truncate_history_for_proactive_llm(
    context: Context, session_id: str, history: list
) -> list:
    """在傳送歷史紀錄至 LLM 前裁剪主動訊息對話內容。

    讓主動訊息插件遵循 AstrBot 的上下文壓縮設定（尤其是對話「最大輪數」的規則）。
    """
    if not history:
        return history

    try:
        astrbot_cfg = context.get_config(umo=session_id)
        provider_settings = astrbot_cfg.get("provider_settings", {}) or {}

        max_context_length = provider_settings.get("max_context_length", -1)
        if max_context_length == -1 or max_context_length is None:
            return history

        try:
            max_context_length = int(max_context_length)
        except (TypeError, ValueError):
            return history

        if max_context_length <= 0:
            return history

        dequeue_context_length = provider_settings.get("dequeue_context_length", 1)
        try:
            dequeue_context_length = int(dequeue_context_length)
        except (TypeError, ValueError):
            dequeue_context_length = 1

        dequeue_context_length = min(
            max(1, dequeue_context_length), max_context_length - 1
        )
        if dequeue_context_length <= 0:
            dequeue_context_length = 1

        truncator = ContextTruncator()

        messages: list[Message] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            try:
                messages.append(Message.model_validate(item))
            except Exception as e:
                logger.debug(
                    f"{_LOG_TAG} skip invalid history item for truncation: {e}"
                )

        if not messages:
            return history

        truncated_messages = truncator.truncate_by_turns(
            messages,
            keep_most_recent_turns=max_context_length,
            drop_turns=dequeue_context_length,
        )

        if len(truncated_messages) != len(messages):
            logger.debug(
                f"{_LOG_TAG} proactive history truncated: "
                f"{len(messages)} -> {len(truncated_messages)} "
                f"(keep_turns={max_context_length}, drop_turns={dequeue_context_length})"
            )

        return [m.model_dump() for m in truncated_messages]
    except Exception as e:
        logger.debug(f"{_LOG_TAG} truncate_history_for_proactive_llm failed: {e}")
        return history


async def call_llm(
    context: Context,
    session_id: str,
    prompt: str,
    contexts: list,
    system_prompt: str,
    provider_id: str | None = None,
) -> Any:
    """呼叫 LLM 生成回應。

    主要路徑：透過 ``llm_generate`` API。
    備用路徑：若主要路徑失敗，回退到 ``get_using_provider().text_chat()``。
    """
    try:
        selected_provider_id = (
            provider_id.strip()
            if isinstance(provider_id, str) and provider_id.strip()
            else await context.get_current_chat_provider_id(session_id)
        )
        return await context.llm_generate(
            chat_provider_id=selected_provider_id,
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
        )
    except Exception as llm_err:
        logger.error(
            f"{_LOG_TAG} call_llm LLM 調用失敗 | session={session_id}: {llm_err}"
        )
        if _is_auth_error(llm_err):
            raise NonRetryableLLMError(str(llm_err)) from llm_err
        try:
            provider = (
                context.get_provider_by_id(provider_id)
                if isinstance(provider_id, str) and provider_id.strip()
                else context.get_using_provider(umo=session_id)
            )
            if provider:
                return await provider.text_chat(
                    prompt=prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                )
        except Exception as fallback_err:
            if _is_auth_error(fallback_err):
                raise NonRetryableLLMError(str(fallback_err)) from fallback_err
            logger.debug(
                f"{_LOG_TAG} call_llm 備用路徑也失敗"
                f" | session={session_id}: {fallback_err}"
            )
        return None
