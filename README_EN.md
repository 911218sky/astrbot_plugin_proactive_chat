<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD041 -->

<div align="center">

# 🤖 AstrBot Proactive Chat Plugin (Plus Fork)

[繁體中文](README.md) | English | [日本語](README_JP.md)

</div>

<p align="center">
  <img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg" alt="License: AGPL-3.0">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/AstrBot-v4.8.0+-orange.svg" alt="AstrBot v4.8.0+">
</p>

<p align="center">
  <img src="logo.png" width="200" alt="logo" />
</p>

---

A proactive messaging plugin for [AstrBot](https://github.com/AstrBotDevs/AstrBot) that enables your Bot to initiate context-aware, persona-consistent conversations with dynamic emotions at random intervals after session silence.

Current version: `v2.24.0`

Recent updates:

- Added optional private-session auto-check/revisit mode with six interaction profiles and context-analysis LLM decisions based on recent chat history.
- Auto-check intervals follow the selected profile and can be replaced with custom minimum/maximum values.
- Added optional AI-decided immediate follow-ups. After each proactive message, the AI can stop or append another message, up to 0-10 follow-ups per burst.
- A new user activity, disabled session, quiet hours, incomplete send, invalid controller output, or an AI stop decision ends the burst before any future message is sent.
- Added a plugin-owned SQLite state store, `proactive_state.db`, for the latest task/session state.
- Hardened create, reschedule, delete, and context-cancel flows so task state is saved before scheduler/timer changes.
- Added exact session filtering in the dashboard. The reschedule dialog now defaults to the current task time instead of a hidden delay.
- `requirements.txt` now pins compatible major versions for APScheduler, aiofiles, and aiosqlite.
- Restored restart-safe auto-trigger and group-idle waiting timers, including missed-task catch-up within a 30-minute grace window.
- The dashboard reschedule action now opens its own dialog for delay time, exact run time, and task description.
- DB-backed waiting tasks can now be edited or deleted from the dashboard even after AstrBot restarts.
- `requirements.txt` now declares `aiosqlite>=0.20.0,<1`.
- Proactive history write-back to AstrBot's main conversation history is disabled by default to reduce `database is locked` risk. Enable `save_proactive_history` only if you need it.
- Upgraded the AstrBot Pages task dashboard into a task management UI with filters, create, reschedule, run-check, and delete actions.
- Added editable task descriptions. Manual schedule descriptions are injected into proactive generation.
- Restyled the dashboard after the livingmemory AstrBot Pages UI, including sidebar navigation, theme switching, and denser task tables.
- Auto-trigger and group-idle task descriptions are now also injected into proactive generation.
- Refined the dashboard UI for clearer summaries, filters, schedule creation, and task scanning.
- Improved livingmemory integration with session/persona filtering support.
- Changed the default `decay_rate` to empty, meaning no decay by default.
- Clarified QQ/Telegram session hints and empty states in the task dashboard.

## 🙏 Credits

This project is based on [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat). Huge thanks to the original author **DBJD-CR** and collaborators for building the complete proactive messaging framework including multi-session support, persistence, DND periods, TTS integration, segmented replies, and more.

> If you appreciate the core concept, please give the original repo a ⭐ Star.

## ✨ New Features in This Fork

### 1. Modular Refactor + Performance Optimization

The original 2500+ line `main.py` has been split into clean modules with comprehensive performance improvements:

| Module | Responsibility |
| :--- | :--- |
| `core/utils.py` | Utility functions (DND check, UMO parsing, log formatting) |
| `core/config.py` | Config management (validation, session config lookup, backup) |
| `core/scheduler.py` | Scheduling logic (weighted random interval, time-range matching) |
| `core/messaging.py` | Message sending (decorator hooks, segmented reply, history sanitization) |
| `main.py` | Plugin entry point (lifecycle, events, core orchestration) |

Performance highlights:
- Plugin class uses `__slots__` to reduce memory overhead
- Merged private/group message handlers into shared `_handle_message()`, eliminating ~80% duplicate code
- Extracted `_add_scheduled_job`, `_cancel_timer`, `_call_llm` and other helpers to reduce redundancy
- `_is_chat_allowed` accepts pre-fetched config to avoid duplicate lookups in `check_and_chat`
- Pre-compiled regex patterns, `frozenset` constants, sync functions where async is unnecessary
- Thorough code comments throughout for developer onboarding

### 2. Dynamic Session Management with template_list

Converted `private_sessions` and `group_sessions` from 5 hardcoded slots (`session_1`..`session_5`) to AstrBot's `template_list` type:

- No session count limit — add/remove freely
- Config JSON reduced from ~2500 lines to ~660 lines (74% reduction)
- Faster WebUI loading and smoother operation

### 3. schedule_rules — Group Time-Based Weighted Random Scheduling

`group_settings.schedule_settings` and group session templates retain `schedule_rules` (`template_list` type), enabling weighted random interval distribution by time of day. Private chats use the adaptive schedule and LLM check flow described below by default. Selecting `weighted_random` in the private schedule mode reveals the legacy time-rule and decay controls in WebUI for compatibility:

- Each rule has `start_hour`, `end_hour`, `interval_weights`
- `interval_weights` format: `"20-30:0.2,30-50:0.5,50-90:0.3"` (minutes:weight)
- Matches current hour to rules for weighted random interval selection; falls back to global min/max if no rule matches
- Supports overnight ranges (e.g., 22-6)

## 🌟 Features Inherited from Original

- Multi-session support (private + group, fully isolated)
- Global + per-session configuration system
- Silence-based timed triggers
- Auto proactive messaging (no user input needed to start)
- Context awareness + full persona support
- Multiple concurrent context tasks per session (short-term follow-ups don't overwrite long-term scheduled greetings)
- Parallel context task cancellation checks
- Dynamic emotions (unanswered counter)
- Persistent sessions backed by the plugin's own SQLite DB (task recovery after restart)
- Do Not Disturb periods
- TTS voice integration
- Segmented replies (simulated typing intervals)
- Optional AI-decided multi-message bursts
- Decorator hooks (compatible with meme/emotion plugins)
- Highly configurable (WebUI-based, no code changes needed)

### Immediate AI-Decided Follow-Ups

`immediate_follow_up_settings` is disabled by default. After a normal AI reply or a proactive message is delivered, the AI can decide whether another message is useful. `max_follow_ups` defaults to 1 and is bounded to 0-10 additional messages; `delay_seconds` defaults to 2 and is bounded to 0-10 seconds. Its semantics are a quiet-period debounce: new user activity cancels the pending follow-up, so only the final message in a burst can trigger it. The implementation also reads the temporary `debounce_seconds` key. A stop decision, malformed controller output, incomplete send, disabled session, or quiet-hours gate ends the burst immediately.

### Human-Like Private Timing

`human_like_settings` is disabled by default and applies only to private sessions. When enabled, `timing_min_seconds` to `timing_max_seconds` (1-3 seconds by default, with 0.1-second precision) become the human-like wait range for both normal AI replies and immediate follow-ups; long messages and late-night replies add their configured bonus. `inbound_debounce_seconds` (3 seconds by default) waits while the user is sending a burst, stops stale events before they reach the LLM, and lets only the final segment trigger a normal reply; `0` disables segment debounce while keeping human reply timing for every event. Private `immediate_follow_up_settings` owns the interaction heat controls: `initial_heat_score` defaults to 50, `user_activity_delta` to 15, and `proactive_delivery_delta` to -5. When immediate follow-ups are enabled, the LLM receives the current 0-100 heat score and is more willing to continue a naturally active conversation at higher heat, while staying restrained at lower heat. Legacy heat keys under `human_like_settings` remain a compatibility fallback.

### Private Auto-Check / Revisit

`auto_check_settings` is disabled by default and applies only to private sessions. It uses a stable adaptive rhythm and asks the top-level `context_analysis_llm_provider_id` (legacy nested provider is a fallback) to review recent chat history. The model may return `{"send_message": true|false, "message": "...", "next_check_minutes": 180}`; the requested timing is clamped to the configured minimum/maximum. Legacy two-field responses remain valid. Add natural-language `guidance` for usual chat hours or quiet preferences. A false decision only schedules the next check without increasing the unanswered counter.

### 4. livingmemory Integration

Optional integration with [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) — retrieves relevant long-term memories during proactive message generation and adds them to the current user prompt, keeping the stable system prompt prefix cache-friendly while making conversations more personalized and contextually rich.

- Toggle on/off via `context_aware_settings.enable_memory`
- Control retrieval count via `memory_top_k` (1-20, visible when memory is enabled)
- Fully optional: works without livingmemory installed, no errors or side effects
- Query priority: context task hint/reason → current proactive prompt as fallback
- Respects livingmemory `use_session_filtering` and `use_persona_filtering` settings

### 5. AstrBot Pages Task Management Dashboard

The plugin provides a Pages dashboard in AstrBot WebUI for checking and managing pending proactive tasks:

- Regular proactive schedules
- Context-aware follow-up tasks
- Auto-trigger timers
- Group silence timers
- Filters by keyword, task type, session type, and enabled status
- Create regular one-shot schedules, reschedule tasks through a dialog, run a session check immediately, or delete waiting tasks
- Add, edit, or clear task descriptions directly from the dashboard

Task state is stored in the plugin's own `proactive_state.db`. On first upgrade, the plugin reads the old `session_data.json` once if the new DB is empty, then keeps only the latest state snapshot in SQLite.

### 6. Dedicated LLM Provider for Context Analysis

Context-aware scheduling can now use a separate LLM provider, saving tokens on your primary model:

- `llm_provider_id` — select from a dropdown of available LLM providers in WebUI; leave empty to use session default
- `extra_prompt` — append custom instructions to the context analysis prompt (e.g., "If user mentions exercise, set delay to 60-90 minutes")

### 7. Externalized Prompt Templates

Context prediction prompts have been extracted to `core/prompts/` as `.txt` files, making them easy to customize without modifying Python code.

## 🚀 Installation

1. Download `.zip` from this repo, install via AstrBot WebUI "Install from file"
2. Core dependencies `APScheduler`, `aiofiles`, and `aiosqlite` are declared in `requirements.txt`
3. Go to WebUI → Plugin Configuration, set target sessions and proactive message motivation
4. Save and enjoy

## 💬 Chat Commands

| Command | Description |
| :--- | :--- |
| `/proactive help` | Show available commands |
| `/proactive tasks` | List all pending proactive message scheduled tasks (regular + habit windows + context-predicted) |

## 📂 Project Structure

```
astrbot_plugin_proactive_chat_plus/
├── core/                      # Core modules
│   ├── __init__.py            # Module exports
│   ├── config.py              # Config management
│   ├── context_predictor.py   # Context-aware scheduling (LLM prediction)
│   ├── llm_helpers.py         # LLM helpers (request prep, memory retrieval, LLM calls)
│   ├── messaging.py           # Message sending
│   ├── scheduler.py           # Scheduling logic
│   ├── send.py                # Proactive message dispatch (TTS / text / segmented)
│   ├── context_scheduling.py  # Context-aware scheduling (task creation/cancellation/restore)
│   ├── chat_executor.py       # Core execution (check_and_chat flow, prompt building, finalization)
│   ├── page_api.py            # AstrBot Pages API (task status, list, and actions)
│   ├── state_store.py         # Plugin SQLite state store
│   ├── prompts/               # LLM prompt templates (context prediction, task cancellation)
│   └── utils.py               # Utilities
├── pages/
│   └── dashboard/             # AstrBot Pages task management dashboard
├── main.py                    # Plugin entry point
├── _conf_schema.json          # Config schema definition
├── metadata.yaml              # Plugin metadata
├── requirements.txt           # Dependencies
├── CHANGELOG.md               # Changelog
└── LICENSE                    # AGPL-3.0
```

## 🌐 Platform Support

| Platform | Status |
| :--- | :--- |
| QQ Personal (aiocqhttp) | ✅ Fully supported |
| Telegram | ✅ Fully supported |
| Feishu | ❓ Theoretically supported |

## 📄 License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

## 💖 Links

- Original project: [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)
- AstrBot: [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
