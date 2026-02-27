# 需求文件：程式碼品質重構

## 簡介

對 AstrBot 主動訊息插件的所有 `.py` 檔案進行全面程式碼品質審查與重構。目標是消除程式碼異味（code smells）、反模式（anti-patterns），提升可讀性、可維護性與專業度，同時不改變任何現有功能行為。

## 審查發現摘要

經過逐檔審查，發現以下主要問題類別：

1. **型別標註不完整**：多處函數參數與回傳值缺少型別標註（如 `config`、`context`、`timezone` 等參數使用隱式 `Any`）
2. **重複的 JSON 解析邏輯**：`context_predictor.py` 中 `_parse_json_response` 與 `_parse_json_array_response` 有大量重複的 markdown 清理與 fallback 邏輯
3. **重複的 UMO ValueError 容錯模式**：`llm_helpers.py` 的 `safe_prepare_llm_request` 和 `send.py` 的 `get_tts_provider` 使用完全相同的 try/except ValueError 回退模式，應抽取為共用工具
4. **重複的對話歷史取得邏輯**：`llm_helpers.py` 的 `prepare_llm_request` 和 `context_scheduling.py` 的 `get_history_for_prediction` 都各自實作了「取得 conversation → 解析 history JSON」的流程
5. **`send.py` 中延遲匯入 `time` 模組**：`send_proactive_message` 函數內部使用 `import time`，應移至模組頂層
6. **`messaging.py` 中 `split_text` 每次呼叫都重新編譯正則**：使用者自訂的 regex 模式未快取，每次分段都重新 `re.compile`
7. **`chat_executor.py` 中未使用的變數**：`_deliver_and_finalize` 中的 `ctx_task` 參數從 `_build_final_prompt` 取得後未被使用（IDE 已報告此問題）
8. **`main.py` 過度使用字串字面量**：`"FriendMessage"`、`"GroupMessage"` 等訊息類型字串散落各處，應定義為常數
9. **`context_scheduling.py` 中 `restore_pending_context_tasks` 為同步函數但操作持久化數據**：修改了 `session_data` 但未呼叫 `_save_data()` 持久化清理結果
10. **`config.py` 中 `_match_session` 的 target_id 比對邏輯脆弱**：使用 `endswith(f":{cid}")` 可能誤匹配（如 `123` 匹配到 `4123`）

## 術語表

- **Plugin（插件）**：繼承 `star.Star` 的 AstrBot 主動訊息插件主類
- **Refactoring_Engine（重構引擎）**：執行本次程式碼品質改善的整體流程
- **Session_Config（會話配置）**：透過 `get_session_config()` 取得的會話級配置字典
- **UMO（Unified Message Origin）**：AstrBot 的統一訊息來源格式，格式為 `平台ID:訊息類型:目標ID`
- **LLM_Response_Parser（LLM 回應解析器）**：從 LLM 回應文字中解析 JSON 的工具函數群
- **History_Loader（歷史載入器）**：取得並解析對話歷史記錄的共用邏輯
- **UMO_Fallback_Handler（UMO 回退處理器）**：處理 AstrBot 版本間 UMO 格式不相容的共用容錯邏輯

## 需求

### 需求 1：補齊型別標註

**使用者故事：** 身為開發者，我希望所有公開函數都有完整的型別標註，以便 IDE 能提供準確的自動補全與靜態分析。

#### 驗收條件

1. THE Refactoring_Engine SHALL 為所有公開函數的參數與回傳值添加明確的型別標註
2. WHEN 函數參數目前標註為隱式 `Any`（如 `config`、`context`、`timezone`），THE Refactoring_Engine SHALL 將其替換為具體型別（如 `AstrBotConfig`、`Context`、`zoneinfo.ZoneInfo | None`）
3. THE Refactoring_Engine SHALL 使用 `TYPE_CHECKING` 守衛來避免循環匯入，僅在型別檢查時匯入重型別
4. THE Refactoring_Engine SHALL 確保所有新增的型別標註通過 `ruff check` 且不引入執行期匯入開銷

### 需求 2：消除重複的 JSON 解析邏輯

**使用者故事：** 身為開發者，我希望 LLM 回應的 JSON 解析邏輯只存在一處，以便未來修改解析策略時只需改一個地方。

#### 驗收條件

1. THE Refactoring_Engine SHALL 將 `context_predictor.py` 中 `_parse_json_response` 與 `_parse_json_array_response` 的共用邏輯（markdown 清理、fallback 搜尋）抽取為單一的通用解析函數
2. WHEN 通用解析函數接收到包含 markdown 程式碼區塊的文字，THE LLM_Response_Parser SHALL 正確移除區塊標記後解析 JSON
3. WHEN 通用解析函數無法直接解析 JSON，THE LLM_Response_Parser SHALL 嘗試以正則搜尋文字中的 JSON 物件或陣列作為 fallback
4. THE Refactoring_Engine SHALL 確保重構後的解析行為與原始實作完全一致

### 需求 3：抽取共用的 UMO ValueError 容錯模式

**使用者故事：** 身為開發者，我希望 UMO 格式不相容的容錯處理只寫一次，避免在多處維護相同的 try/except 邏輯。

#### 驗收條件

1. THE Refactoring_Engine SHALL 在 `core/utils.py` 中建立一個通用的 UMO 容錯裝飾器或包裝函數
2. WHEN 被包裝的函數因 UMO 格式問題拋出 `ValueError`（包含 "too many values" 或 "expected 3"），THE UMO_Fallback_Handler SHALL 自動以標準三段式格式重試
3. THE Refactoring_Engine SHALL 將 `llm_helpers.py` 的 `safe_prepare_llm_request` 和 `send.py` 的 `get_tts_provider` 改為使用此共用容錯機制
4. IF 重試後仍然失敗，THEN THE UMO_Fallback_Handler SHALL 將原始例外向上傳播

### 需求 4：統一對話歷史取得邏輯

**使用者故事：** 身為開發者，我希望「取得並解析對話歷史」的邏輯只存在一處，避免兩處實作不同步。

#### 驗收條件

1. THE Refactoring_Engine SHALL 在 `core/llm_helpers.py` 中建立一個共用的對話歷史取得函數
2. THE History_Loader SHALL 封裝「取得 conversation → 解析 history JSON → 清洗格式」的完整流程
3. THE Refactoring_Engine SHALL 將 `prepare_llm_request` 和 `context_scheduling.py` 的 `get_history_for_prediction` 改為使用此共用函數
4. WHEN conversation 的 history 欄位為字串，THE History_Loader SHALL 以 `json.loads` 解析；WHEN 解析失敗，THE History_Loader SHALL 回傳空列表

### 需求 5：修正模組級匯入與常數定義

**使用者故事：** 身為開發者，我希望所有匯入都在模組頂層完成，所有重複使用的字串字面量都定義為常數，以符合 Python 最佳實踐。

#### 驗收條件

1. THE Refactoring_Engine SHALL 將 `send.py` 中 `send_proactive_message` 函數內的 `import time` 移至模組頂層
2. THE Refactoring_Engine SHALL 將 `main.py` 和 `core/` 模組中重複出現的訊息類型字串（`"FriendMessage"`、`"GroupMessage"`、`"Friend"`、`"Group"`）定義為 `core/utils.py` 中的模組級常數
3. THE Refactoring_Engine SHALL 確保所有引用這些字串的位置都改為使用常數
4. THE Refactoring_Engine SHALL 確保變更後所有模組的匯入順序符合 `ruff` 的 isort 規則

### 需求 6：改善正則表達式快取策略

**使用者故事：** 身為開發者，我希望使用者自訂的正則表達式不會在每次呼叫時重新編譯，以減少不必要的效能開銷。

#### 驗收條件

1. THE Refactoring_Engine SHALL 為 `messaging.py` 中 `split_text` 的使用者自訂正則模式引入快取機制（如 `functools.lru_cache` 或模組級字典快取）
2. WHEN 使用者提供的正則模式字串與上次相同，THE Refactoring_Engine SHALL 重用已編譯的正則物件而非重新編譯
3. IF 使用者提供的正則模式無效，THEN THE Refactoring_Engine SHALL 記錄警告並回退到預設分段正則（與現有行為一致）

### 需求 7：清理未使用的變數與參數

**使用者故事：** 身為開發者，我希望程式碼中沒有未使用的變數或參數，以消除 IDE 警告並提升可讀性。

#### 驗收條件

1. THE Refactoring_Engine SHALL 移除 `chat_executor.py` 中 `check_and_chat` 函數裡未使用的 `session_config` 變數賦值
2. THE Refactoring_Engine SHALL 修正 `_deliver_and_finalize` 的呼叫鏈，確保 `ctx_task` 不再作為未使用的回傳值傳遞
3. THE Refactoring_Engine SHALL 掃描所有 `.py` 檔案，移除其他未使用的變數或匯入
4. THE Refactoring_Engine SHALL 確保清理後不影響任何現有功能邏輯

### 需求 8：強化 `restore_pending_context_tasks` 的持久化一致性

**使用者故事：** 身為開發者，我希望 `restore_pending_context_tasks` 在清理過期任務後能正確持久化結果，避免下次重啟時重複處理已清理的條目。

#### 驗收條件

1. THE Refactoring_Engine SHALL 修改 `restore_pending_context_tasks` 使其在清理過期任務後標記數據為「需要持久化」
2. WHEN `restore_pending_context_tasks` 清理了過期的語境任務，THE Plugin SHALL 在初始化流程中呼叫 `_save_data()` 將清理結果寫入磁碟
3. THE Refactoring_Engine SHALL 確保此修改不改變函數的同步特性（持久化可延遲到 `initialize()` 中的非同步階段執行）

### 需求 9：改善 session_id 比對的精確度

**使用者故事：** 身為開發者，我希望會話 ID 的比對邏輯不會因為字串尾部巧合而誤匹配，以避免配置錯誤地套用到不相關的會話。

#### 驗收條件

1. THE Refactoring_Engine SHALL 修改 `config.py` 中 `_match_session` 的 target_id 比對邏輯，使用精確匹配或帶分隔符的尾部匹配
2. WHEN target_id 為 `"123"` 且 session_id 中的 cid 為 `"4123"`，THE Session_Config SHALL 不匹配該配置
3. WHEN target_id 為 `"123"` 且 session_id 中的 cid 為 `"123"`，THE Session_Config SHALL 正確匹配該配置
4. THE Refactoring_Engine SHALL 確保修改後的比對邏輯向下相容現有的配置格式

### 需求 10：統一錯誤處理與日誌格式

**使用者故事：** 身為開發者，我希望所有模組的錯誤處理和日誌輸出遵循一致的模式，以便在生產環境中快速定位問題。

#### 驗收條件

1. THE Refactoring_Engine SHALL 確保所有 `except Exception as e` 區塊都記錄了足夠的上下文資訊（函數名稱、session_id 等）
2. THE Refactoring_Engine SHALL 將裸露的 `except Exception: pass` 替換為至少記錄 `logger.debug` 的版本
3. WHEN 錯誤發生在關鍵路徑（如 LLM 呼叫、訊息發送），THE Plugin SHALL 記錄 `logger.error` 並包含 traceback 資訊
4. THE Refactoring_Engine SHALL 確保所有日誌訊息都使用 `_LOG_TAG` 前綴，格式為 `f"{_LOG_TAG} 描述: {細節}"`
