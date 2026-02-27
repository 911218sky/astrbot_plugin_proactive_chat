# 實作計畫：程式碼品質重構

## 概述

依據設計文件，對 AstrBot 主動訊息插件進行行為不變的程式碼品質重構。任務按依賴順序排列：先建立基礎共用元件，再逐步替換各模組中的重複邏輯，最後進行整合驗證。

## Tasks

- [x] 1. 建立基礎共用元件（core/utils.py）
  - [x] 1.1 新增訊息類型常數 `MSG_TYPE_FRIEND`、`MSG_TYPE_GROUP`、`MSG_TYPE_KEYWORD_FRIEND`、`MSG_TYPE_KEYWORD_GROUP`
    - 在 `core/utils.py` 中定義模組級常數
    - 更新 `core/__init__.py` 匯出這些常數
    - _Requirements: 5.2, 5.3_

  - [x] 1.2 實作通用 JSON 解析器 `parse_llm_json`
    - 在 `core/utils.py` 中新增 `parse_llm_json(text, *, expect_type, log_tag)` 函數
    - 支援 markdown 程式碼區塊清理、fallback 正則搜尋
    - 支援 `expect_type` 參數過濾 dict / list / None
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ]* 1.3 撰寫 Property 1 屬性測試：JSON 解析 round-trip
    - **Property 1: JSON 解析 round-trip**
    - 測試檔案：`tests/test_parse_llm_json.py`
    - 使用 Hypothesis `st.dictionaries` / `st.lists` + 隨機 markdown 包裝
    - **Validates: Requirements 2.2, 2.3**

  - [x] 1.4 實作 UMO 容錯包裝器 `with_umo_fallback` 與 `async_with_umo_fallback`
    - 在 `core/utils.py` 中新增同步與非同步版本
    - 捕獲包含 "too many values" 或 "expected 3" 的 ValueError
    - 以 `parse_session_id` 重組標準三段式格式重試
    - _Requirements: 3.1, 3.2, 3.4_

  - [ ]* 1.5 撰寫 Property 3 屬性測試：UMO 容錯重試行為
    - **Property 3: UMO 容錯重試行為**
    - 測試檔案：`tests/test_umo_fallback.py`
    - 使用 `st.from_regex` 生成 UMO 格式字串
    - **Validates: Requirements 3.2**

  - [x] 1.6 實作 session_id 精確比對函數 `_is_target_match`
    - 在 `core/config.py` 中新增 `_is_target_match(target_id, config_id)` 函數
    - 支援完全匹配與帶 `:` 分隔符的尾部匹配
    - 修改 `_match_session` 改用 `_is_target_match`
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [ ]* 1.7 撰寫 Property 7 屬性測試：Session ID 精確比對
    - **Property 7: Session ID 精確比對**
    - 測試檔案：`tests/test_session_match.py`
    - 使用 `st.from_regex(r"[0-9]+")` 生成數字 ID
    - **Validates: Requirements 9.1**

- [x] 2. Checkpoint — 確認基礎元件正確
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. 重構 LLM 相關模組
  - [x] 3.1 實作共用對話歷史載入器 `load_conversation_history`
    - 在 `core/llm_helpers.py` 中新增 `load_conversation_history(context, session_id)` 函數
    - 封裝「取得 conversation → 解析 history JSON → 清洗格式」流程
    - 字串型 history 以 `json.loads` 解析，失敗回傳空列表
    - _Requirements: 4.1, 4.2, 4.4_

  - [ ]* 3.2 撰寫 Property 4 屬性測試：對話歷史 JSON 解析 round-trip
    - **Property 4: 對話歷史 JSON 解析 round-trip**
    - 測試檔案：`tests/test_history_loader.py`
    - 使用 `st.lists(st.fixed_dictionaries({...}))` 生成歷史列表
    - **Validates: Requirements 4.4**

  - [x] 3.3 重構 `context_predictor.py` 改用 `parse_llm_json`
    - 將 `_parse_json_response` 改為呼叫 `parse_llm_json(text, expect_type=dict)` 的薄包裝
    - 將 `_parse_json_array_response` 改為呼叫 `parse_llm_json(text, expect_type=list)` 的薄包裝
    - _Requirements: 2.1, 2.4_

  - [ ]* 3.4 撰寫 Property 2 屬性測試：JSON 解析器行為等價
    - **Property 2: JSON 解析器行為等價**
    - 測試檔案：`tests/test_parse_llm_json.py`
    - 使用 `st.text()` 生成任意字串，比較新舊函數輸出
    - **Validates: Requirements 2.4**

  - [x] 3.5 重構 `llm_helpers.py` 使用共用元件
    - `prepare_llm_request` 改用 `load_conversation_history` 取得對話歷史
    - `safe_prepare_llm_request` 改用 `async_with_umo_fallback`
    - _Requirements: 3.3, 4.3_

  - [x] 3.6 重構 `context_scheduling.py` 使用共用元件
    - `get_history_for_prediction` 改為呼叫 `load_conversation_history`
    - 修改 `restore_pending_context_tasks` 回傳 `bool` 表示是否需要持久化
    - _Requirements: 4.3, 8.1_

  - [ ]* 3.7 撰寫 Property 6 屬性測試：過期任務清理持久化標記
    - **Property 6: 過期任務清理持久化標記**
    - 測試檔案：`tests/test_restore_tasks.py`
    - 自訂策略生成含過期/未過期任務的 session_data
    - **Validates: Requirements 8.1**

- [x] 4. Checkpoint — 確認 LLM 模組重構正確
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. 重構其餘模組
  - [x] 5.1 修正 `send.py` 模組級匯入與 UMO 容錯
    - 將 `send_proactive_message` 內的 `import time` 移至模組頂層
    - `get_tts_provider` 改用 `with_umo_fallback`
    - _Requirements: 3.3, 5.1_

  - [x] 5.2 實作 `messaging.py` 正則表達式快取
    - 新增 `_compile_split_regex` 函數，使用 `functools.lru_cache(maxsize=32)`
    - `split_text` 改用 `_compile_split_regex` 編譯使用者自訂正則
    - 無效模式回傳 `None`，回退到預設正則
    - _Requirements: 6.1, 6.2, 6.3_

  - [ ]* 5.3 撰寫 Property 5 屬性測試：正則快取功能等價
    - **Property 5: 正則快取功能等價**
    - 測試檔案：`tests/test_regex_cache.py`
    - 使用 `st.text()` + `st.from_regex` 驗證快取版與直接編譯結果一致
    - **Validates: Requirements 6.1**

  - [x] 5.3b 重構 `main.py` 改用訊息類型常數
    - 將所有 `"FriendMessage"`、`"GroupMessage"`、`"Friend"`、`"Group"` 字面量替換為 `core/utils.py` 中的常數
    - 同步替換 `core/messaging.py`、`core/config.py` 中的字面量
    - _Requirements: 5.2, 5.3_

- [x] 6. 全域型別標註與清理
  - [x] 6.1 補齊所有公開函數的型別標註
    - 為 `core/` 各模組與 `main.py` 的公開函數添加參數與回傳值型別標註
    - 使用 `TYPE_CHECKING` 守衛避免循環匯入
    - 隱式 `Any` 參數替換為具體型別（`AstrBotConfig`、`Context`、`ZoneInfo | None` 等）
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 6.2 清理未使用的變數與參數
    - 移除 `chat_executor.py` 中 `check_and_chat` 的未使用 `session_config` 變數賦值
    - 修正 `_deliver_and_finalize` 呼叫鏈中未使用的 `ctx_task` 回傳值
    - 掃描所有 `.py` 檔案移除其他未使用的變數或匯入
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 6.3 統一錯誤處理與日誌格式
    - 確保所有 `except Exception as e` 區塊記錄足夠上下文（函數名稱、session_id）
    - 將裸露的 `except Exception: pass` 替換為至少 `logger.debug` 的版本
    - 關鍵路徑錯誤使用 `logger.error` 並包含 traceback
    - 所有日誌訊息使用 `_LOG_TAG` 前綴
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [x] 7. 整合與持久化修正
  - [x] 7.1 修改 `main.py` 的 `initialize()` 處理 `restore_pending_context_tasks` 回傳值
    - 根據 `restore_pending_context_tasks` 回傳的 `bool` 決定是否呼叫 `await _save_data()`
    - 確保不改變函數的同步特性（持久化在非同步階段執行）
    - _Requirements: 8.2, 8.3_

  - [x] 7.2 更新 `core/__init__.py` 匯出清單
    - 確保新增的 `parse_llm_json`、`with_umo_fallback`、`async_with_umo_fallback`、`load_conversation_history`、訊息類型常數都正確匯出
    - 確保匯入順序符合 `ruff` 的 isort 規則
    - _Requirements: 5.4_

  - [x] 7.3 執行 `ruff format .` 與 `ruff check .` 確認零錯誤
    - 格式化所有修改過的檔案
    - 確認無 lint 錯誤、無未使用匯入、匯入順序正確
    - _Requirements: 1.4, 5.4_

- [x] 8. Final checkpoint — 確認所有變更正確
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 標記 `*` 的任務為可選，可跳過以加速 MVP
- 每個任務引用具體需求以確保可追溯性
- Checkpoint 確保增量驗證
- 屬性測試使用 Hypothesis 驗證通用正確性屬性
- 所有變更嚴格遵守「行為不變」原則
