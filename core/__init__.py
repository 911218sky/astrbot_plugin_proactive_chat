# core — 主動訊息插件模組化核心

from .chat_executor import (
    check_and_chat,
    finalize_and_reschedule,
)
from .config import (
    backup_configurations,
    get_session_config,
    validate_config,
)
from .context_predictor import (
    check_should_cancel_tasks_batch,
    predict_proactive_timing,
)
from .context_scheduling import (
    create_context_predicted_task,
    get_history_for_prediction,
    handle_context_aware_scheduling,
    maybe_cancel_pending_context_task,
    remove_context_predicted_task,
    restore_pending_context_tasks,
)
from .llm_helpers import (
    call_llm,
    get_livingmemory_engine,
    prepare_llm_request,
    recall_memories_for_proactive,
    resolve_system_prompt,
    safe_prepare_llm_request,
)
from .messaging import (
    calc_segment_interval,
    sanitize_history_content,
    send_chain_with_hooks,
    split_text,
    trigger_decorating_hooks,
)
from .scheduler import compute_weighted_interval, should_trigger_by_unanswered
from .send import (
    get_tts_provider,
    send_proactive_message,
    try_send_tts,
)
from .utils import (
    get_session_log_str,
    is_group_session_id,
    is_private_session,
    is_quiet_time,
    parse_session_id,
    resolve_full_umo,
)

__all__ = [
    # utils
    "is_quiet_time",
    "parse_session_id",
    "get_session_log_str",
    "resolve_full_umo",
    "is_private_session",
    "is_group_session_id",
    # config
    "validate_config",
    "get_session_config",
    "backup_configurations",
    # scheduler
    "compute_weighted_interval",
    "should_trigger_by_unanswered",
    # context_predictor
    "predict_proactive_timing",
    "check_should_cancel_tasks_batch",
    # context_scheduling
    "handle_context_aware_scheduling",
    "maybe_cancel_pending_context_task",
    "remove_context_predicted_task",
    "create_context_predicted_task",
    "get_history_for_prediction",
    "restore_pending_context_tasks",
    # chat_executor
    "check_and_chat",
    "finalize_and_reschedule",
    # messaging
    "trigger_decorating_hooks",
    "send_chain_with_hooks",
    "split_text",
    "calc_segment_interval",
    "sanitize_history_content",
    # llm_helpers
    "get_livingmemory_engine",
    "recall_memories_for_proactive",
    "prepare_llm_request",
    "resolve_system_prompt",
    "safe_prepare_llm_request",
    "call_llm",
    # send
    "send_proactive_message",
    "try_send_tts",
    "get_tts_provider",
]
