(() => {
  "use strict";

  const state = {
    tasks: [],
    sessions: [],
    filter: "all",
    sessionFilter: "all",
    enabledFilter: "all",
    query: "",
    timer: null,
  };

  const TYPE_LABELS = {
    regular: "一般排程",
    context: "語境預測",
    auto_trigger: "自動觸發",
    group_idle: "群聊沉默",
    context_orphan: "未追蹤語境",
  };

  function endpoint(path) {
    return "page/" + String(path).replace(/^\/+/, "").replace(/\/+/g, "/");
  }

  async function apiGet(path, params) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("目前頁面必須從 AstrBot 官方插件 Pages 開啟");
    }
    const response = await bridge.apiGet(endpoint(path), params || {});
    if (response && response.status === "error") {
      throw new Error(response.message || "API 請求失敗");
    }
    return response && response.status === "ok" ? response.data || {} : response || {};
  }

  async function apiPost(path, body) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("目前頁面必須從 AstrBot 官方插件 Pages 開啟");
    }
    const response = await bridge.apiPost(endpoint(path), body || {});
    if (response && response.status === "error") {
      throw new Error(response.message || "API 請求失敗");
    }
    return response && response.status === "ok" ? response.data || {} : response || {};
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function showToast(message) {
    const el = byId("toast");
    el.textContent = message;
    el.classList.remove("visible");
    void el.offsetWidth;
    el.classList.add("visible");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => el.classList.remove("visible"), 2600);
  }

  function readTheme() {
    try {
      const bridge = window.AstrBotPluginPage;
      if (bridge && typeof bridge.getContext === "function") {
        const ctx = bridge.getContext();
        if (ctx && typeof ctx.isDark === "boolean") return ctx.isDark ? "dark" : "light";
      }
    } catch (_) {}
    try {
      const stored = localStorage.getItem("proactive_chat_theme");
      if (stored) return stored;
    } catch (_) {}
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const darkIcon = byId("theme-icon-dark");
    const lightIcon = byId("theme-icon-light");
    if (darkIcon && lightIcon) {
      darkIcon.classList.toggle("hidden", theme === "light");
      lightIcon.classList.toggle("hidden", theme === "dark");
    }
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || "light";
    const next = current === "light" ? "dark" : "light";
    applyTheme(next);
    try {
      localStorage.setItem("proactive_chat_theme", next);
    } catch (_) {}
  }

  function listenBridgeTheme() {
    try {
      const bridge = window.AstrBotPluginPage;
      if (!bridge || typeof bridge.onContext !== "function") return;
      bridge.onContext((ctx) => {
        if (!ctx || typeof ctx.isDark !== "boolean") return;
        applyTheme(ctx.isDark ? "dark" : "light");
      });
    } catch (_) {}
  }

  function askConfirm(message, title) {
    const dialog = byId("confirm-dialog");
    const titleEl = byId("confirm-title");
    const messageEl = byId("confirm-message");
    const okButton = byId("confirm-ok");
    const cancelButton = byId("confirm-cancel");
    const previousFocus = document.activeElement;
    if (!dialog || !titleEl || !messageEl || !okButton || !cancelButton) {
      return Promise.resolve(false);
    }

    titleEl.textContent = title || "確認操作";
    messageEl.textContent = message;
    dialog.hidden = false;
    okButton.focus();

    return new Promise((resolve) => {
      let done = false;

      const close = (result) => {
        if (done) return;
        done = true;
        dialog.hidden = true;
        okButton.removeEventListener("click", onOk);
        cancelButton.removeEventListener("click", onCancel);
        dialog.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKeydown);
        if (previousFocus && typeof previousFocus.focus === "function") {
          previousFocus.focus();
        }
        resolve(result);
      };

      const onOk = () => close(true);
      const onCancel = () => close(false);
      const onBackdrop = (event) => {
        if (event.target === dialog) close(false);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") close(false);
      };

      okButton.addEventListener("click", onOk);
      cancelButton.addEventListener("click", onCancel);
      dialog.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKeydown);
    });
  }

  function openRescheduleDialog(task, currentDescription) {
    const dialog = byId("reschedule-dialog");
    const subtitle = byId("reschedule-subtitle");
    const delayInput = byId("reschedule-delay-input");
    const runAtInput = byId("reschedule-run-at-input");
    const descriptionInput = byId("reschedule-description-input");
    const okButton = byId("reschedule-ok");
    const cancelButton = byId("reschedule-cancel");
    const closeButton = byId("reschedule-close");
    const previousFocus = document.activeElement;
    if (
      !dialog ||
      !subtitle ||
      !delayInput ||
      !runAtInput ||
      !descriptionInput ||
      !okButton ||
      !cancelButton ||
      !closeButton
    ) {
      return Promise.resolve(null);
    }

    subtitle.textContent = `${task.session_label || task.session_id} · ${TYPE_LABELS[task.type] || task.type}`;
    delayInput.value = "10";
    runAtInput.value = "";
    descriptionInput.value = currentDescription || "";
    dialog.hidden = false;
    delayInput.focus();

    return new Promise((resolve) => {
      let done = false;

      const close = (result) => {
        if (done) return;
        done = true;
        dialog.hidden = true;
        okButton.removeEventListener("click", onOk);
        cancelButton.removeEventListener("click", onCancel);
        closeButton.removeEventListener("click", onCancel);
        dialog.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKeydown);
        if (previousFocus && typeof previousFocus.focus === "function") {
          previousFocus.focus();
        }
        resolve(result);
      };

      const onOk = () => {
        const runAt = runAtInput.value;
        const delay = delayInput.value;
        if (!runAt && (!delay || Number(delay) <= 0)) {
          showToast("請填寫延遲分鐘或指定時間");
          delayInput.focus();
          return;
        }
        close({
          run_at: runAt || "",
          delay_minutes: runAt ? "" : delay,
          description: descriptionInput.value.trim(),
        });
      };
      const onCancel = () => close(null);
      const onBackdrop = (event) => {
        if (event.target === dialog) close(null);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") close(null);
      };

      okButton.addEventListener("click", onOk);
      cancelButton.addEventListener("click", onCancel);
      closeButton.addEventListener("click", onCancel);
      dialog.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKeydown);
    });
  }

  async function withButtonBusy(button, callback) {
    const oldText = button.textContent;
    button.disabled = true;
    button.textContent = "處理中";
    try {
      return await callback();
    } finally {
      button.disabled = false;
      button.textContent = oldText;
    }
  }

  function formatRemaining(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "";
    let value = Math.max(0, Number(seconds));
    const days = Math.floor(value / 86400);
    value %= 86400;
    const hours = Math.floor(value / 3600);
    value %= 3600;
    const minutes = Math.floor(value / 60);
    if (days > 0) return `${days} 天 ${hours} 小時`;
    if (hours > 0) return `${hours} 小時 ${minutes} 分`;
    return `${minutes} 分`;
  }

  function taskTone(type) {
    if (type === "context" || type === "context_orphan") return "tone-context";
    if (type === "auto_trigger" || type === "group_idle") return "tone-waiting";
    return "tone-regular";
  }

  function isEditingDescription() {
    const active = document.activeElement;
    return Boolean(active && active.matches && active.matches(".description-edit"));
  }

  function searchableText(task) {
    return [
      task.title,
      task.session_label,
      task.session_id,
      task.target_id,
      task.detail,
      task.extra && task.extra.reason,
      task.extra && task.extra.hint,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function filteredTasks() {
    const query = state.query.trim().toLowerCase();
    return state.tasks.filter((task) => {
      if (state.filter !== "all" && task.type !== state.filter) return false;
      if (state.enabledFilter === "enabled" && !task.enabled) return false;
      if (state.enabledFilter === "disabled" && task.enabled) return false;
      if (state.sessionFilter === "private" && String(task.message_type).toLowerCase().includes("group")) return false;
      if (state.sessionFilter === "group" && !String(task.message_type).toLowerCase().includes("group")) return false;
      if (!query) return true;
      return searchableText(task).includes(query);
    });
  }

  function renderSessionSelect() {
    const select = byId("session-select");
    if (!state.sessions.length) {
      select.innerHTML = '<option value="">沒有可用會話</option>';
      return;
    }
    const enabledSessions = state.sessions.filter((session) => session.enabled);
    if (!enabledSessions.length) {
      select.innerHTML = '<option value="">沒有已啟用會話</option>';
      return;
    }
    select.innerHTML = enabledSessions
      .map((session) => {
        const label = `${session.label || session.session_id} · ${session.session_id}`;
        return `<option value="${escapeHtml(session.session_id)}">${escapeHtml(label)}</option>`;
      })
      .join("");
  }

  function renderSummary(summary) {
    byId("metric-total").textContent = summary.total_count || 0;
    byId("metric-regular").textContent = summary.regular_count || 0;
    byId("metric-context").textContent = summary.context_count || 0;
    byId("metric-timers").textContent =
      (summary.auto_trigger_count || 0) + (summary.group_idle_count || 0);
    const pill = byId("scheduler-pill");
    pill.textContent = summary.scheduler_running ? "運行中" : "未運行";
    pill.classList.toggle("is-off", !summary.scheduler_running);
    byId("subtitle").textContent = [
      `時區：${summary.timezone || "local"}`,
      `更新：${summary.generated_at || ""}`,
    ].join(" · ");
  }

  function renderTasks() {
    const body = byId("task-body");
    const tasks = filteredTasks();
    byId("result-count").textContent = `${tasks.length} / ${state.tasks.length} 個任務`;
    if (!tasks.length) {
      body.innerHTML = '<tr><td colspan="6" class="table-empty">沒有符合條件的任務</td></tr>';
      return;
    }

    body.innerHTML = tasks
      .map((task) => {
        const typeLabel = TYPE_LABELS[task.type] || task.type || "未知";
        const enabled = task.enabled ? "已啟用" : "未啟用";
        const messageType = task.message_type ? escapeHtml(task.message_type) : "未知類型";
        const target = task.target_id ? escapeHtml(task.target_id) : "";
        const detail = task.detail || (task.extra && task.extra.hint) || "";
        const description = task.description || "";
        const canDelete = ["regular", "context", "context_orphan", "auto_trigger", "group_idle"].includes(task.type);
        const canReschedule = ["regular", "context", "auto_trigger", "group_idle"].includes(task.type);
        const canEditDescription = ["regular", "context", "auto_trigger", "group_idle"].includes(task.type);
        const canRunNow = Boolean(task.enabled);
        const rescheduleTitle = ["auto_trigger", "group_idle"].includes(task.type)
          ? "開啟彈窗選擇時間，並轉為手動排程"
          : "開啟彈窗選擇新的執行時間";
        const lastMessage = task.last_message_time
          ? `<span class="meta">最後訊息：${escapeHtml(task.last_message_time)}</span>`
          : "";
        const remaining = formatRemaining(task.remaining_seconds) || "未知";
        return `
          <tr data-task-id="${escapeHtml(task.id)}" data-task-type="${escapeHtml(task.type)}" data-session-id="${escapeHtml(task.session_id)}">
            <td>
              <div class="session">
                <strong>${escapeHtml(task.session_label || task.session_id)}</strong>
                <span class="meta">${escapeHtml(enabled)} · ${messageType}${target ? ` · ${target}` : ""}</span>
                ${lastMessage}
              </div>
            </td>
            <td>
              <div class="task-kind ${taskTone(task.type)}">
                <span class="badge ${escapeHtml(task.type)}">${escapeHtml(typeLabel)}</span>
                <strong>${escapeHtml(task.title || typeLabel)}</strong>
              </div>
            </td>
            <td>
              <div class="time-cell">
                <strong>${escapeHtml(remaining)}</strong>
                <span class="meta">${escapeHtml(task.next_run_time || "未知")}</span>
              </div>
            </td>
            <td><span class="count-pill">${escapeHtml(task.unanswered_count || 0)}</span></td>
            <td class="detail-cell">
              <textarea class="description-edit" data-role="description" rows="3" maxlength="800" ${canEditDescription ? "" : "disabled"} placeholder="${escapeHtml(detail || "填寫這個任務要提醒或接續的內容")}">${escapeHtml(description)}</textarea>
            </td>
            <td>
              <div class="row-actions">
                <button class="btn btn-secondary btn-sm" type="button" data-action="run-now" ${canRunNow ? "" : "disabled"} title="${canRunNow ? "立即檢查條件，符合時會發送訊息" : "會話未啟用"}">立即執行</button>
                <button class="btn btn-secondary btn-sm" type="button" data-action="reschedule" ${canReschedule ? "" : "disabled"} title="${escapeHtml(rescheduleTitle)}">改期</button>
                <button class="btn btn-secondary btn-sm" type="button" data-action="save-description" ${canEditDescription ? "" : "disabled"} title="保存此任務描述">保存描述</button>
                <button class="btn btn-danger btn-sm" type="button" data-action="delete" ${canDelete ? "" : "disabled"} title="刪除任務">刪除</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function readSchedulePayload(descriptionOverride) {
    const runAt = byId("run-at-input").value;
    const delay = byId("delay-input").value;
    const description =
      descriptionOverride == null
        ? byId("description-input").value.trim()
        : String(descriptionOverride).trim();
    return {
      run_at: runAt || "",
      delay_minutes: runAt ? "" : delay,
      description,
    };
  }

  async function createTask() {
    const sessionId = byId("session-select").value;
    if (!sessionId) {
      showToast("請先選擇會話");
      return;
    }
    await apiPost("tasks/action", {
      action: "create",
      session_id: sessionId,
      ...readSchedulePayload(),
    });
    showToast("任務已新增");
    await refresh(false);
  }

  async function handleTaskAction(button) {
    const row = button.closest("tr");
    if (!row) return;
    const action = button.dataset.action;
    const taskId = row.dataset.taskId;
    const taskType = row.dataset.taskType;
    const sessionId = row.dataset.sessionId;

    const payload = {
      task_id: taskId,
      task_type: taskType,
      session_id: sessionId,
    };
    const task = state.tasks.find(
      (item) =>
        String(item.id) === taskId &&
        String(item.type) === taskType &&
        String(item.session_id) === sessionId
    ) || {
      id: taskId,
      type: taskType,
      session_id: sessionId,
      session_label: sessionId,
    };

    if (action === "delete" && !(await askConfirm("確定刪除此任務？", "刪除任務"))) return;
    if (action === "run-now") {
      if (!(await askConfirm("這會立即檢查發送條件，符合條件時可能真的送出主動訊息。確定執行？", "立即執行任務"))) return;
    }

    await withButtonBusy(button, async () => {
      if (action === "run-now") {
        await apiPost("tasks/action", { action: "run_now", ...payload });
        showToast("已送出立即執行");
      } else if (action === "reschedule") {
        const textarea = row.querySelector('[data-role="description"]');
        const schedulePayload = await openRescheduleDialog(
          task,
          textarea ? textarea.value : ""
        );
        if (!schedulePayload) return;
        await apiPost("tasks/action", {
          action: "reschedule",
          ...payload,
          ...schedulePayload,
        });
        showToast("任務時間已更新");
      } else if (action === "save-description") {
        const textarea = row.querySelector('[data-role="description"]');
        await apiPost("tasks/action", {
          action: "update_description",
          ...payload,
          description: textarea ? textarea.value.trim() : "",
        });
        showToast("任務描述已保存");
      } else if (action === "delete") {
        await apiPost("tasks/action", { action: "delete", ...payload });
        showToast("任務已刪除");
      }
      await refresh(false);
    });
  }

  async function refresh(showDone) {
    try {
      const data = await apiGet("tasks", {});
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
      renderSummary(data.summary || {});
      renderSessionSelect();
      renderTasks();
      if (showDone) showToast("已刷新");
    } catch (error) {
      byId("subtitle").textContent = error.message || "載入失敗";
      byId("task-body").innerHTML =
        `<tr><td colspan="6" class="table-empty">${escapeHtml(error.message || "載入失敗")}</td></tr>`;
      showToast(error.message || "載入失敗");
    }
  }

  function syncAutoRefresh() {
    clearInterval(state.timer);
    state.timer = null;
    if (byId("auto-refresh").checked) {
      state.timer = setInterval(() => {
        if (!isEditingDescription()) refresh(false);
      }, 10000);
    }
  }

  function bindEvents() {
    byId("theme-toggle").addEventListener("click", toggleTheme);
    document.querySelectorAll(".nav-item[data-scroll-target]").forEach((item) => {
      item.addEventListener("click", () => {
        document.querySelectorAll(".nav-item[data-scroll-target]").forEach((nav) => {
          nav.classList.toggle("active", nav === item);
        });
        const target = byId(item.dataset.scrollTarget);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    byId("refresh-button").addEventListener("click", () => refresh(true));
    byId("auto-refresh").addEventListener("change", syncAutoRefresh);
    byId("reset-filter-button").addEventListener("click", () => {
      state.filter = "all";
      state.sessionFilter = "all";
      state.enabledFilter = "all";
      state.query = "";
      byId("type-filter").value = "all";
      byId("session-filter").value = "all";
      byId("enabled-filter").value = "all";
      byId("search-input").value = "";
      renderTasks();
    });
    byId("type-filter").addEventListener("change", (event) => {
      state.filter = event.target.value;
      renderTasks();
    });
    byId("session-filter").addEventListener("change", (event) => {
      state.sessionFilter = event.target.value;
      renderTasks();
    });
    byId("enabled-filter").addEventListener("change", (event) => {
      state.enabledFilter = event.target.value;
      renderTasks();
    });
    byId("search-input").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderTasks();
    });
    byId("create-task-button").addEventListener("click", async () => {
      const button = byId("create-task-button");
      try {
        await withButtonBusy(button, createTask);
      } catch (error) {
        showToast(error.message || "新增失敗");
      }
    });
    byId("task-body").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button || button.disabled) return;
      try {
        await handleTaskAction(button);
      } catch (error) {
        showToast(error.message || "操作失敗");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(readTheme());
    listenBridgeTheme();
    bindEvents();
    syncAutoRefresh();
    refresh(false);
  });
})();
