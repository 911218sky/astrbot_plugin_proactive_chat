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

### 3. schedule_rules — Time-Based Weighted Random Scheduling

Added `schedule_rules` (`template_list` type) to all `schedule_settings`, enabling weighted random interval distribution by time of day:

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
- Persistent sessions (task recovery after restart)
- Do Not Disturb periods
- TTS voice integration
- Segmented replies (simulated typing intervals)
- Decorator hooks (compatible with meme/emotion plugins)
- Highly configurable (WebUI-based, no code changes needed)

### 4. livingmemory Integration

Optional integration with [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) — retrieves relevant long-term memories during proactive message generation and injects them into the system prompt, making conversations more personalized and contextually rich.

- Toggle on/off via `context_aware_settings.enable_memory`
- Control retrieval count via `memory_top_k` (1-20, visible when memory is enabled)
- Fully optional: works without livingmemory installed, no errors or side effects
- Query priority: context task hint/reason → current time as fallback

### 5. Dedicated LLM Provider for Context Analysis

Context-aware scheduling can now use a separate LLM provider, saving tokens on your primary model:

- `llm_provider_id` — select from a dropdown of available LLM providers in WebUI; leave empty to use session default
- `extra_prompt` — append custom instructions to the context analysis prompt (e.g., "If user mentions exercise, set delay to 60-90 minutes")

### 6. Externalized Prompt Templates

Context prediction prompts have been extracted to `core/prompts/` as `.txt` files, making them easy to customize without modifying Python code.

## 🚀 Installation

1. Download `.zip` from this repo, install via AstrBot WebUI "Install from file"
2. Core dependencies `APScheduler` and `aiofiles` are typically bundled with AstrBot
3. Go to WebUI → Plugin Configuration, set target sessions and proactive message motivation
4. Save and enjoy

## 💬 Chat Commands

| Command | Description |
| :--- | :--- |
| `/proactive help` | Show available commands |
| `/proactive tasks` | List all pending proactive message scheduled tasks (regular + context-predicted) |

## 📂 Project Structure

```
astrbot_plugin_proactive_chat/
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
│   ├── prompts/               # LLM prompt templates (context prediction, task cancellation)
│   └── utils.py               # Utilities
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
