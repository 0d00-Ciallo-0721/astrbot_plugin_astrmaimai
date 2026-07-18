const API_PREFIX = "admin";
const SCHEDULER_POLL_INTERVAL_MS = 5000;
const DASHBOARD_CACHE_TTL_MS = 10000;

const TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "learning", label: "主动学习" },
  { id: "reviews", label: "表达审核" },
  { id: "memories", label: "记忆网络" },
  { id: "users", label: "用户画像" },
  { id: "personaSlices", label: "角色切片" },
];

function normalizeTabId(tab) {
  if (tab === "persona") return "personaSlices";
  if (tab === "settings") return "dashboard";
  return TABS.some((item) => item.id === tab) ? tab : "dashboard";
}

const SLICE_FIELDS = [
  ["memory_points", "长期记忆点"],
  ["identity_points", "身份认知点"],
  ["preference_points", "偏好点"],
  ["relationship_points", "关系点"],
  ["speech_style_points", "说话风格点"],
];

const state = {
  bridge: null,
  current: normalizeTabId(location.hash.replace("#", "") || "dashboard"),
  dashboardTab: "overview",
  reviewTab: "jargon_pending",
  memoryTab: "canonical",
  observabilityOverview: null,
  observabilityTimeline: [],
  observabilityFilters: {
    chat_id: "",
    domains: "",
    levels: "",
    kinds: "",
    q: "",
    tags: "",
  },
  schedulerStatus: null,
  schedulerDueSelection: null,
  schedulerChatLoop: null,
  schedulerChatId: "",
  schedulerPollTimer: null,
  lastApiErrorToastAt: 0,
  lastApiErrorKey: "",
  selectedReviews: new Set(),
  activeUserId: "",
  userSearch: "",
  usersScrollTop: 0,
  dashboardCache: {},
  cache: {
    reviews: {
      expressionPending: [],
      expressionAll: { items: [], total: 0, page: 1, page_size: 20 },
      jargonPending: { items: [], total: 0, limit: 200, offset: 0 },
      jargonAll: { items: [], total: 0, limit: 200, offset: 0 },
      filters: { status: "", group_id: "", keyword: "" },
    },
    memories: {
      month: new Date().toISOString().slice(0, 7),
      canonical: { items: [], total: 0, limit: 100, offset: 0 },
      canonicalKind: "",
      events: [],
      reflections: [],
      nodes: [],
      jargon: { items: [], total: 0, limit: 200, offset: 0 },
    },
    users: [],
    personaSlices: {},
    personaSlicesError: null,
    turns: [],
  },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const content = () => $("#content");

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function attr(value) {
  return escapeHtml(value);
}

function segment(value) {
  return encodeURIComponent(String(value));
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

function json(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonSafe(text, fallback) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function formatTime(value) {
  const ts = Number(value || 0);
  if (!ts) return "无";
  return new Date(ts * 1000).toLocaleString();
}

function formatPercent(value) {
  const num = Number(value || 0);
  return `${(num <= 1 ? num * 100 : num).toFixed(1)}%`;
}

function asItems(value) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.items)) return value.items;
  return [];
}

function schedulerReport() {
  return state.schedulerDueSelection?.report || {};
}

function schedulerOverview() {
  return state.schedulerStatus?.overview || {};
}

function observabilityTimelinePath() {
  const filters = state.observabilityFilters || {};
  const params = new URLSearchParams({ limit: "80" });
  if (filters.chat_id) params.set("chat_id", filters.chat_id);
  if (filters.domains) params.set("domains", filters.domains);
  if (filters.levels) params.set("levels", filters.levels);
  if (filters.kinds) params.set("kinds", filters.kinds);
  if (filters.q || filters.tags) {
    if (filters.q) params.set("q", filters.q);
    if (filters.tags) params.set("tags", filters.tags);
    return `/cognition/observability/search?${params.toString()}`;
  }
  return `/cognition/observability/timeline?${params.toString()}`;
}

function clampPercent(value) {
  const num = Number(value || 0);
  return Math.max(0, Math.min(100, num <= 1 ? num * 100 : num));
}

function setBridgeStatus(text, kind = "") {
  const node = $("#bridge-status");
  node.textContent = text;
  node.className = `status-pill ${kind}`.trim();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2800);
}

function ensureBridge() {
  if (!state.bridge) throw new Error("AstrBotPluginPage bridge is not ready");
  return state.bridge;
}

function pluginEndpoint(path) {
  const [rawPath, rawQuery = ""] = String(path || "").split("?");
  const cleanPath = rawPath.replace(/^\/+/, "");
  const cleanPrefix = API_PREFIX.replace(/^\/+|\/+$/g, "");
  const endpoint = cleanPath ? `${cleanPrefix}/${cleanPath}` : cleanPrefix;
  const params = {};
  if (rawQuery) {
    new URLSearchParams(rawQuery).forEach((value, key) => {
      params[key] = value;
    });
  }
  return { endpoint, params: Object.keys(params).length ? params : undefined };
}

function apiErrorMessage(payload) {
  if (!payload) return "请求失败";
  if (typeof payload === "string") return payload;
  if (payload.message) return payload.message;
  if (payload.detail) return typeof payload.detail === "string" ? payload.detail : json(payload.detail);
  if (payload.error) return typeof payload.error === "string" ? payload.error : json(payload.error);
  if (Array.isArray(payload.errors) && payload.errors.length) {
    return payload.errors.map((item) => item.message || item.detail || json(item)).join("; ");
  }
  return json(payload);
}

function unwrapResponse(result) {
  if (result && (result.status === "error" || result.ok === false)) {
    throw new Error(apiErrorMessage(result));
  }
  if (result && Object.prototype.hasOwnProperty.call(result, "data") && (result.status || "runtime_bound" in result)) {
    return result.data;
  }
  return result;
}

/** Safe API fetch wrapper — preserves last successful data on error */
function safeFetch(fetchFn, fallback) {
  return fetchFn().catch((err) => {
    const message = err.message || String(err);
    console.warn("[AstrMai-Admin] API fetch degraded:", message);
    const now = Date.now();
    if (state.lastApiErrorKey !== message || now - Number(state.lastApiErrorToastAt || 0) > 5000) {
      state.lastApiErrorKey = message;
      state.lastApiErrorToastAt = now;
      toast(`数据加载失败：${message}`);
    }
    return fallback;
  });
}

function getDashboardCache(key) {
  const entry = state.dashboardCache[key];
  if (!entry || Date.now() - Number(entry.updatedAt || 0) > DASHBOARD_CACHE_TTL_MS) return null;
  return entry.data;
}

function setDashboardCache(key, data) {
  state.dashboardCache[key] = { data, updatedAt: Date.now() };
  return data;
}

function clearDashboardCache(key = "") {
  if (key) {
    delete state.dashboardCache[key];
    return;
  }
  state.dashboardCache = {};
}

async function readyBridge(bridge) {
  if (typeof bridge.ready === "function") {
    return bridge.ready();
  }
  if (typeof bridge.initialize === "function") {
    return bridge.initialize();
  }
  return bridge.getContext ? bridge.getContext() : {};
}

function waitForBridge(timeoutMs = 6000) {
  if (window.AstrBotPluginPage) return Promise.resolve(window.AstrBotPluginPage);
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const timer = setInterval(() => {
      if (window.AstrBotPluginPage) {
        clearInterval(timer);
        resolve(window.AstrBotPluginPage);
        return;
      }
      if (Date.now() - startedAt >= timeoutMs) {
        clearInterval(timer);
        reject(new Error("AstrBotPluginPage bridge was not injected"));
      }
    }, 60);
  });
}

const api = {
  async get(path) {
    const { endpoint, params } = pluginEndpoint(path);
    return unwrapResponse(await ensureBridge().apiGet(endpoint, params));
  },
  async post(path, body = {}) {
    const { endpoint } = pluginEndpoint(path);
    return unwrapResponse(await ensureBridge().apiPost(endpoint, body));
  },
};

function renderTabs() {
  $("#tabs").innerHTML = TABS.map((tab) => `
    <button class="tab-button ${state.current === tab.id ? "active" : ""}" data-tab="${tab.id}" type="button">${tab.label}</button>
  `).join("");
  $$("[data-tab]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.tab)));
}

function navigate(tab) {
  state.current = normalizeTabId(tab);
  location.hash = state.current;
  renderTabs();
  loadCurrent();
}

function pageHeader(title, subtitle, actions = "") {
  return `
    <div class="page-header">
      <div>
        <h2>${title}</h2>
        <p>${subtitle}</p>
      </div>
      <div class="row-actions">${actions}</div>
    </div>
  `;
}

function section(title, subtitle, body, actions = "") {
  return `
    <section class="panel">
      <div class="section-head">
        <div>
          <h3>${title}</h3>
          ${subtitle ? `<p>${subtitle}</p>` : ""}
        </div>
        <div class="row-actions">${actions}</div>
      </div>
      ${body}
    </section>
  `;
}

function subTabs(active, tabs, attrName = "subtab") {
  return `
    <div class="subtabs">
      ${tabs.map((tab) => `
        <button class="subtab-button ${active === tab.id ? "active" : ""}" data-${attrName}="${tab.id}" type="button">${tab.label}</button>
      `).join("")}
    </div>
  `;
}

function metric(label, value, hint = "", kind = "") {
  return `
    <div class="metric ${kind}">
      <span class="metric-label">${label}</span>
      <span class="metric-value">${escapeHtml(value ?? "-")}</span>
      ${hint ? `<span class="muted">${hint}</span>` : ""}
    </div>
  `;
}

function statusChip(text, kind = "") {
  return `<span class="chip ${kind}">${escapeHtml(text)}</span>`;
}

function progressBar(value, label = "") {
  const width = clampPercent(value);
  return `
    <div class="progress-wrap">
      <div class="progress-bar"><span style="width:${width}%"></span></div>
      ${label ? `<span class="progress-label">${escapeHtml(label)}</span>` : ""}
    </div>
  `;
}

function formField(name, label, value, type = "text") {
  return `<label>${escapeHtml(label)}<input data-user-field="${attr(name)}" type="${attr(type)}" value="${attr(value)}"></label>`;
}

function table(headers, rows, empty = "暂无数据") {
  if (!rows || rows.length === 0) {
    return `<div class="empty-state"><p>${empty}</p></div>`;
  }
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((item) => `<th>${item}</th>`).join("")}</tr></thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>
  `;
}

function showLoading(title = "正在读取数据...") {
  content().innerHTML = `<section class="empty-state"><h2>${escapeHtml(title)}</h2><p>请稍候，正在通过 AstrBot Plugin Page bridge 读取插件数据。</p></section>`;
}

function openModal(title, body, footer = "") {
  const root = $("#modal-root");
  root.hidden = false;
  root.innerHTML = `
    <div class="modal-backdrop" data-modal-close></div>
    <section class="modal-card">
      <header class="modal-head">
        <h3>${title}</h3>
        <button class="ghost-button" data-modal-close type="button">关闭</button>
      </header>
      <div class="modal-body">${body}</div>
      ${footer ? `<footer class="modal-foot">${footer}</footer>` : ""}
    </section>
  `;
  $$("[data-modal-close]", root).forEach((node) => node.addEventListener("click", closeModal));
}

function closeModal() {
  const root = $("#modal-root");
  root.hidden = true;
  root.innerHTML = "";
}

function confirmAction(message, title = "确认操作") {
  const modalTitle = title === "danger" ? "危险操作" : title;
  return new Promise((resolve) => {
    openModal(
      modalTitle,
      `<p class="confirm-message">${escapeHtml(message)}</p>`,
      `<button class="ghost-button" data-confirm-no type="button">取消</button><button class="danger-button" data-confirm-yes type="button">确认</button>`,
    );
    $("[data-confirm-no]").addEventListener("click", () => {
      closeModal();
      resolve(false);
    });
    $("[data-confirm-yes]").addEventListener("click", () => {
      closeModal();
      resolve(true);
    });
  });
}

function openFormModal(title, fields, initial, onSubmit, submitText = "保存") {
  const body = `
    <form id="modal-form" class="form-grid single">
      ${fields.map((field) => {
        const value = initial?.[field.name] ?? field.default ?? "";
        if (field.type === "textarea") {
          return `<label>${field.label}<textarea name="${field.name}" rows="${field.rows || 5}">${escapeHtml(value)}</textarea></label>`;
        }
        if (field.type === "select") {
          return `<label>${field.label}<select name="${field.name}">${(field.options || []).map((option) => `<option value="${attr(option.value)}" ${String(value) === String(option.value) ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select></label>`;
        }
        return `<label>${field.label}<input name="${field.name}" type="${field.type || "text"}" value="${attr(value)}"></label>`;
      }).join("")}
    </form>
  `;
  openModal(title, body, `<button class="ghost-button" data-modal-close type="button">取消</button><button class="primary-button" data-modal-submit type="button">${escapeHtml(submitText)}</button>`);
  $("[data-modal-submit]").addEventListener("click", async () => {
    const form = $("#modal-form");
    const data = {};
    fields.forEach((field) => {
      const node = form.elements[field.name];
      if (field.cast === "float") {
        const v = Number.parseFloat(node.value || "0");
        data[field.name] = Number.isNaN(v) ? 0 : v;
      } else if (field.cast === "int") {
        const v = Number.parseInt(node.value || "0", 10);
        data[field.name] = Number.isNaN(v) ? 0 : v;
      }
      else data[field.name] = node.value;
    });
    await onSubmit(data);
    closeModal();
  });
}

function openJsonModal(title, value, onSubmit) {
  openModal(
    title,
    `<textarea id="json-modal-editor" rows="18">${escapeHtml(json(value))}</textarea>`,
    `<button class="ghost-button" data-modal-close type="button">取消</button><button class="primary-button" data-json-save type="button">保存</button>`,
  );
  $("[data-json-save]").addEventListener("click", async () => {
    try {
      await onSubmit(JSON.parse($("#json-modal-editor").value));
      closeModal();
    } catch (error) {
      toast(`JSON 保存失败：${error.message || error}`);
    }
  });
}

function splitLines(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinLines(value) {
  if (Array.isArray(value)) return value.map((item) => String(item || "").trim()).filter(Boolean).join("\n");
  return String(value || "");
}

function findReviewItem(kind, id) {
  const groups = kind === "jargon"
    ? [state.cache.reviews.jargonPending.items, state.cache.reviews.jargonAll.items, state.cache.memories.jargon.items]
    : [state.cache.reviews.expressionPending, state.cache.reviews.expressionAll.items];
  return groups.flat().find((item) => String(item.id || item.review_id || "") === String(id)) || null;
}

function findMemoryItem(tab, id) {
  const source = state.cache.memories[tab];
  const items = Array.isArray(source) ? source : (source?.items || []);
  return items.find((item) => String(item.id || item.date || item.term || "") === String(id)) || null;
}

function jargonPayload(data) {
  return {
    content: data.content,
    raw_content: data.raw_content || data.content,
    meaning: data.meaning,
    scene: data.scene,
    examples: splitLines(data.examples),
    group_id: data.group_id,
    confidence: data.confidence,
    importance: data.importance,
    review_reason: data.review_reason,
    review_suggestion: data.review_suggestion,
  };
}

function expressionPayload(data) {
  return {
    situation: data.situation,
    replacement: data.expression,
    expression: data.expression,
    style: data.style,
    shared_scope: data.shared_scope,
    weight: data.weight,
    reason: data.review_reason,
    review_reason: data.review_reason,
    review_suggestion: data.review_suggestion,
  };
}

function openJargonCalibration(item, action = "save") {
  const itemId = item.id || item.review_id || item.canonical_id || "";
  const title = action === "approve" ? "修正黑话并通过" : action === "reject" ? "修正黑话并驳回" : "编辑黑话";
  const submitText = action === "approve" ? "保存并通过" : action === "reject" ? "保存并驳回" : "保存修改";
  openFormModal(
    title,
    [
      { name: "content", label: "黑话词", default: item.content || "", type: "text" },
      { name: "raw_content", label: "原始提取", default: item.raw_content || item.content || "", type: "text" },
      { name: "meaning", label: "含义解释", default: item.meaning || "", type: "textarea", rows: 4 },
      { name: "scene", label: "适用场景", default: item.scene || "", type: "textarea", rows: 3 },
      { name: "examples", label: "例句/证据（每行一条）", default: joinLines(item.examples), type: "textarea", rows: 4 },
      { name: "group_id", label: "适用会话", default: item.group_id || "GLOBAL", type: "text" },
      { name: "confidence", label: "置信度", default: item.confidence ?? 0.8, type: "number", cast: "float" },
      { name: "importance", label: "权重", default: item.importance ?? 0.7, type: "number", cast: "float" },
      { name: "review_reason", label: "审核备注", default: item.review_reason || "", type: "textarea", rows: 3 },
      { name: "review_suggestion", label: "修正建议", default: item.review_suggestion || "", type: "textarea", rows: 3 },
    ],
    item,
    async (data) => {
      const payload = jargonPayload(data);
      if (action === "approve") await api.post(`/memories/jargon/${segment(itemId)}/approve`, payload);
      else if (action === "reject") await api.post(`/memories/jargon/${segment(itemId)}/reject`, payload);
      else await api.post(`/memories/jargon/${segment(itemId)}`, payload);
      toast(action === "approve" ? "黑话已修正并通过" : action === "reject" ? "黑话已修正并驳回" : "黑话已保存");
      if (state.current === "memories") await loadMemories();
      else await loadReviews();
    },
    submitText,
  );
}

function openExpressionCalibration(item, action = "save") {
  const itemId = item.id || item.review_id || item.canonical_id || "";
  const title = action === "approve" ? "修正表达并通过" : action === "reject" ? "修正表达并驳回" : "编辑表达";
  const submitText = action === "approve" ? "保存并通过" : action === "reject" ? "保存并驳回" : "保存修改";
  openFormModal(
    title,
    [
      { name: "situation", label: "使用场景", default: item.situation || "", type: "textarea", rows: 3 },
      { name: "expression", label: "表达文本", default: item.expression || item.content || "", type: "textarea", rows: 4 },
      { name: "style", label: "风格标签", default: item.style || "", type: "text" },
      { name: "shared_scope", label: "共享范围", default: item.shared_scope || item.group_id || "GLOBAL", type: "text" },
      { name: "weight", label: "权重", default: item.weight ?? 1.0, type: "number", cast: "float" },
      { name: "review_reason", label: "审核备注", default: item.review_reason || "", type: "textarea", rows: 3 },
      { name: "review_suggestion", label: "修正建议", default: item.review_suggestion || "", type: "textarea", rows: 3 },
    ],
    item,
    async (data) => {
      const payload = expressionPayload(data);
      if (action === "approve") await api.post(`/reviews/${segment(itemId)}/submit`, { ...payload, action: "approve" });
      else if (action === "reject") await api.post(`/reviews/${segment(itemId)}/submit`, { ...payload, action: "reject" });
      else await api.post(`/reviews/${segment(itemId)}`, payload);
      toast(action === "approve" ? "表达已修正并通过" : action === "reject" ? "表达已修正并驳回" : "表达已保存");
      await loadReviews();
    },
    submitText,
  );
}

async function loadDashboard() {
  if (!getDashboardCache(state.dashboardTab)) {
    const hasDashboardShell = Boolean($("[data-dashboard-tab]"));
    if (hasDashboardShell) {
      dashboardShell(`
        <section class="empty-state">
          <h2>正在读取运行状态大盘...</h2>
          <p>请稍候，正在通过 AstrBot Plugin Page bridge 读取插件数据。</p>
        </section>
      `);
    } else {
      showLoading("正在读取运行状态大盘...");
    }
  }
  if (state.dashboardTab === "overview") {
    stopSchedulerPolling();
    return renderDashboardOverview();
  }
  if (state.dashboardTab === "heartflow") {
    stopSchedulerPolling();
    return renderDashboardHeartflow();
  }
  if (state.dashboardTab === "cognition") {
    await renderDashboardCognition();
    startSchedulerPolling();
    return;
  }
  stopSchedulerPolling();
  return renderDashboardTools();
}

function dashboardShell(body) {
  content().innerHTML = `
    ${pageHeader("运行状态大盘", "实时监控 AstrMai 核心系统的生命周期、心流、认知和工具链。")}
    ${subTabs(state.dashboardTab, [
      { id: "overview", label: "运行概览" },
      { id: "heartflow", label: "心智流 Heartflow" },
      { id: "cognition", label: "主动决策池 Cognition" },
      { id: "tools", label: "工具链观测 Tools" },
    ], "dashboard-tab")}
    ${body}
  `;
  $$('[data-dashboard-tab]').forEach((button) => button.addEventListener("click", () => {
    if (state.dashboardTab === button.dataset.dashboardTab) return;
    state.dashboardTab = button.dataset.dashboardTab;
    loadDashboard();
  }));
}

async function renderDashboardOverview() {
  const cached = getDashboardCache("overview");
  const [snapshot, health, capabilities, models, observabilityOverview] = cached || setDashboardCache("overview", await Promise.all([
    safeFetch(() => api.get("/dashboard"), {}),
    safeFetch(() => api.get("/runtime/health"), {}),
    safeFetch(() => api.get("/runtime/capabilities"), {}),
    safeFetch(() => api.get("/runtime/models"), {}),
    safeFetch(() => api.get("/cognition/observability/overview"), {}),
  ]));
  state.observabilityOverview = observabilityOverview;
  const healthData = health || {};
  const running = Boolean(healthData.running);
  const obs = observabilityOverview || {};
  const obsSnapshot = obs.snapshot || {};
  dashboardShell(`
    <div class="health-strip ${running ? "ok" : "warn"}">
      <span class="status-dot ${running ? "ok" : "warn"}"></span>
      <div>
        <strong>${running ? (healthData.degraded_count > 0 ? "部分降级运行" : "全系统健康运行") : "系统状态未确认"}</strong>
        <p>阶段：${escapeHtml(healthData.boot_phase || "unknown")}，降级组件：${healthData.degraded_count ?? 0}</p>
      </div>
    </div>
    <div class="grid">
      ${metric("总用户数", snapshot.total_users ?? "—")}
      ${metric("待审核项", snapshot.pending_reviews ?? "—")}
      ${metric("长期记忆(v2)", snapshot.total_canonical_memories ?? "—")}
      ${metric("旧记忆事件", snapshot.total_memory_events ?? "—")}
      ${metric("数据库大小", `${snapshot.db_size_kb ?? 0} KB`)}
    </div>
    <div class="grid two">
      ${section("能力矩阵", "Capabilities", `<pre>${json(capabilities)}</pre>`)}
      ${section("模型与健康诊断", "Models / Health", `<pre>${json({ models, health: healthData })}</pre>`)}
    </div>
    ${section("Observability Overview", "统一观测摘要与最近异常。", `
      <div class="grid">
        ${metric("Retained Events", obsSnapshot.retained_events ?? 0)}
        ${metric("Tracked Chats", obsSnapshot.retained_chats ?? 0)}
        ${metric("Warnings", obsSnapshot.recent_warning_count ?? 0)}
        ${metric("Errors", obsSnapshot.recent_error_count ?? 0)}
      </div>
      <pre>${json(obsSnapshot)}</pre>
      <h4>Recent Observability Errors</h4>
      <pre>${json(asItems(obs.recent_errors))}</pre>
    `)}
  `);
}

async function renderDashboardHeartflow() {
  const cached = getDashboardCache("heartflow");
  const [status, chats, impulses, timeline, digests, intents] = cached || setDashboardCache("heartflow", await Promise.all([
    safeFetch(() => api.get("/heartflow/status"), {}),
    safeFetch(() => api.get("/heartflow/chats"), { items: [] }),
    safeFetch(() => api.get("/heartflow/impulses?limit=50"), { items: [] }),
    safeFetch(() => api.get("/heartflow/timeline?limit=80"), { items: [] }),
    safeFetch(() => api.get("/heartflow/topic-digests?limit=50"), { items: [] }),
    safeFetch(() => api.get("/proactive/intents?limit=50"), { items: [] }),
  ]));
  const impulseRows = asItems(impulses).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.pulse_type || "-")}</td>
      <td>${item.visible_candidate_allowed ? statusChip("allowed", "ok") : statusChip(item.blocked_reason || "hidden", "")}</td>
      <td>${item.requires_synthetic_event ? statusChip("synthetic", "warn") : statusChip("hidden", "")}</td>
      <td>${String(Boolean(item.dispatch_enabled))}</td>
      <td>${String(Boolean(item.synthetic_event_queued))}</td>
      <td><pre>${json(item.safety_checks || {})}</pre></td>
    </tr>
  `);
  const timelineRows = asItems(timeline).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.kind || "-")}</td>
      <td>${escapeHtml(item.label || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td><pre>${json(item.payload || {})}</pre></td>
    </tr>
  `);
  const digestRows = asItems(digests).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${item.status === "written" ? statusChip("written", "ok") : statusChip(item.skip_reason || "skipped", "warn")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td>${escapeHtml((item.tags || []).join(", ") || "-")}</td>
      <td>${escapeHtml(item.importance ?? "-")}</td>
    </tr>
  `);
  const intentRows = asItems(intents).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp || item.created_at)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${item.reply_sent ? statusChip("sent", "ok") : statusChip(item.blocked_reason || item.status || "queued", item.blocked_reason ? "warn" : "")}</td>
      <td>${escapeHtml(item.blocked_reason || "-")}</td>
      <td>${escapeHtml(item.reason || item.guidance || "-")}</td>
    </tr>
  `);
  const rows = asItems(chats).map((item) => {
    const session = item.session || {};
    const action = item.latest_action_decision || {};
    return `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${item.talk_willingness ?? "-"}</td>
      <td>${item.interest ?? "-"}</td>
      <td>${item.silence_pressure ?? "-"}</td>
      <td>${escapeHtml(action.action_type || session.last_impulse || "-")}</td>
      <td>${escapeHtml(session.tick_count ?? "-")}</td>
      <td>${escapeHtml(session.talk_frequency_adjust ?? "-")}</td>
      <td>${escapeHtml(session.insert_pressure ?? "-")}</td>
      <td>${escapeHtml(session.reply_pressure ?? "-")}</td>
      <td>${escapeHtml(session.visible_candidate_score ?? "-")}</td>
      <td>${escapeHtml(session.topic_heat ?? "-")}</td>
      <td>${escapeHtml([
        `observe:${session.consecutive_observe_count ?? 0}`,
        `no_reply:${session.consecutive_no_reply_count ?? 0}`,
        `prepare:${session.consecutive_prepare_count ?? 0}`,
      ].join(" / "))}</td>
      <td>${(item.cooldown_tags || []).map((tag) => statusChip(tag, "warn")).join(" ")}</td>
      <td class="row-actions">
        <button class="ghost-button" data-heartflow-impulses="${attr(item.chat_id)}" type="button">Impulse Safety</button>
        <button class="ghost-button" data-heartflow-timeline="${attr(item.chat_id)}" type="button">Timeline</button>
        <button class="ghost-button" data-hidden-context="${attr(item.chat_id)}" type="button">隐藏上下文</button>
        <button class="danger-button" data-clear-heartflow="${attr(item.chat_id)}" type="button">清理 cooldown</button>
      </td>
    </tr>
  `;
  });
  dashboardShell(`
    ${section("心智流状态", "Heartflow manager describe_status()", `<pre>${json(status)}</pre>`)}
    ${section("Heartflow Sessions", "Session / Rhythm / Hidden Action", table(["Chat", "Talk", "Interest", "Silence", "Hidden Action", "Session", "Talk Freq", "Insert", "Reply", "Score", "Topic", "Rhythm", "Cooldowns", "操作"], rows))}
  `);
  content().insertAdjacentHTML("beforeend", section("Impulse Safety", "Heartflow impulse safety decisions: v1 only records candidate state; dispatch_enabled=false means no visible message is sent.", table(["Time", "Chat", "Pulse", "Candidate", "Synthetic", "Dispatch", "Queued", "Safety"], impulseRows)));
  content().insertAdjacentHTML("beforeend", section("Heartflow Timeline", "observe / wait / no_reply / prepare_reply / proactive_candidate / dispatched / blocked 的安全轨迹。", table(["Time", "Chat", "Kind", "Label", "Summary", "Payload"], timelineRows)));
  content().insertAdjacentHTML("beforeend", section("Proactive Intents", "Heartflow / Wakeup 主动候选经过 ProactiveDispatcher 后的结果。", table(["Time", "Chat", "Source", "Status", "Blocked", "Preview"], intentRows)));
  content().insertAdjacentHTML("beforeend", section("topic-digests", "HeartflowTopicDigest 写入 cognitive feedback 的记录与跳过原因。", table(["Time", "Chat", "Status", "Summary", "Tags", "Importance"], digestRows)));
  $$('[data-hidden-context]').forEach((button) => button.addEventListener("click", async () => {
    const result = await api.get(`/heartflow/chats/${segment(button.dataset.hiddenContext)}/hidden-context`);
    openModal("Heartflow Hidden Context", `<pre>${json(result)}</pre>`);
  }));
  $$('[data-heartflow-impulses]').forEach((button) => button.addEventListener("click", async () => {
    const chatId = button.dataset.heartflowImpulses;
    const result = await api.get(`/heartflow/chats/${segment(chatId)}/impulses?limit=20`);
    openModal(`Impulse Safety: ${escapeHtml(chatId)}`, `<pre>${json(result.items || [])}</pre>`);
  }));
  $$('[data-heartflow-timeline]').forEach((button) => button.addEventListener("click", async () => {
    const chatId = button.dataset.heartflowTimeline;
    const result = await api.get(`/heartflow/chats/${segment(chatId)}/timeline?limit=50`);
    openModal(`Heartflow Timeline: ${escapeHtml(chatId)}`, `<pre>${json(result.items || [])}</pre>`);
  }));
  $$('[data-clear-heartflow]').forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("清理这个 chat 的 Heartflow cooldown？")) return;
    await api.post(`/heartflow/chats/${segment(button.dataset.clearHeartflow)}/cooldowns/clear`);
    toast("Heartflow cooldown 已清理");
    clearDashboardCache("heartflow");
    renderDashboardHeartflow();
  }));
}

async function loadSchedulerChatLoop(chatId = null) {
  const targetChat = String(chatId ?? state.schedulerChatId ?? "").trim();
  if (!targetChat) {
    state.schedulerChatLoop = null;
    return;
  }
  state.schedulerChatId = targetChat;
  state.schedulerChatLoop = await safeFetch(() => api.get(`/cognition/scheduler/chats/${segment(targetChat)}`), null);
}

function shouldPollScheduler() {
  return state.current === "dashboard" && state.dashboardTab === "cognition";
}

function stopSchedulerPolling() {
  if (state.schedulerPollTimer) {
    clearInterval(state.schedulerPollTimer);
    state.schedulerPollTimer = null;
  }
}

function startSchedulerPolling() {
  stopSchedulerPolling();
  if (!shouldPollScheduler()) return;
  state.schedulerPollTimer = setInterval(() => {
    if (!shouldPollScheduler()) {
      stopSchedulerPolling();
      return;
    }
    if (state._pollingInFlight) return;  // guard: skip if previous poll still running
    state._pollingInFlight = true;
    renderDashboardCognition()
      .catch(() => {})
      .finally(() => { state._pollingInFlight = false; });
  }, SCHEDULER_POLL_INTERVAL_MS);
}

function renderSchedulerDiagnosticsSection() {
  const overview = schedulerOverview();
  const report = schedulerReport();
  const selected = asItems(report.selected).slice(0, 6);
  const chatData = state.schedulerChatLoop || {};
  const emptyState = chatData.state_present === false
    ? `<div class="empty-state compact"><p>暂无 loop state。该 chat 尚未进入 scheduler 跟踪。</p></div>`
    : "";
  const quickButtons = selected.length
    ? `<div class="chip-row">${selected.map((chatId) => `<button class="ghost-button" data-scheduler-chat="${attr(chatId)}" type="button">${escapeHtml(chatId)}</button>`).join("")}</div>`
    : `<div class="empty-state compact"><p>当前没有 due selection 选中的 chat。</p></div>`;
  return section(
    "Scheduler Diagnostics",
    "Chat Loop Kernel 在 AstrBot 插件页内的调度摘要、批次配额与单 chat 诊断。",
    `
      <div class="grid">
        ${metric("Profile", state.schedulerStatus?.scheduler_policy?.active_profile || "balanced")}
        ${metric("Poll Mode", overview.scheduler_poll_mode || "-")}
        ${metric("Poll Interval", overview.scheduler_poll_interval ?? 0)}
        ${metric("Due Chats", overview.due_chat_count ?? 0)}
        ${metric("Forced Promotions", overview.forced_promotion_count ?? 0)}
        ${metric("Batch Fill", formatPercent(overview.batch_fill_rate ?? 0))}
      </div>
      <div class="grid two">
        ${section("Batch / Backpressure", "", `
          <div class="chip-row">
            ${statusChip(`busy_backpressure: ${state.schedulerStatus?.proactive?.busy_backpressure_active ? "on" : "off"}`, state.schedulerStatus?.proactive?.busy_backpressure_active ? "warn" : "ok")}
            ${statusChip(`maintenance_backpressure: ${state.schedulerStatus?.proactive?.maintenance_backpressure_active ? "on" : "off"}`, state.schedulerStatus?.proactive?.maintenance_backpressure_active ? "warn" : "ok")}
          </div>
          <pre>${json(state.schedulerStatus?.proactive?.scheduler_batch_plan || {})}</pre>
          <pre>${json(state.schedulerStatus?.proactive?.quota_skip_counts || {})}</pre>
          <pre>${json(state.schedulerStatus?.proactive?.poll_mode_transition || {})}</pre>
        `)}
        ${section("Chat Loop Drill-down", "", `
          <div class="form-grid single">
            <label>chat_id<input id="scheduler-chat-id" value="${attr(state.schedulerChatId || "")}" placeholder="chat_id"></label>
          </div>
          <div class="row-actions">
            <button class="primary-button" data-scheduler-load type="button">加载 scheduler chat</button>
          </div>
          ${quickButtons}
          ${emptyState}
          <div class="grid">
            ${metric("phase", chatData.phase || "-")}
            ${metric("next_tick_at", chatData.next_tick_at ?? 0)}
            ${metric("missed_due_passes", chatData.missed_due_passes ?? 0)}
            ${metric("forced_promotion_count", chatData.forced_promotion_count ?? 0)}
          </div>
          <pre>${json(chatData.scheduler_pending_signals || {})}</pre>
          <pre>${json(report)}</pre>
        `)}
      </div>
    `,
  );
}

async function renderDashboardCognition() {
  const cached = getDashboardCache("cognition");
  const [decisions, turns, schedulerStatus, schedulerDueSelection, observabilityOverview, unifiedTimeline] = cached || setDashboardCache("cognition", await Promise.all([
    safeFetch(() => api.get("/cognition/recent-decisions?limit=50"), { items: [] }),
    safeFetch(() => api.get("/cognition/recent-turns?limit=50"), { items: [] }),
    safeFetch(() => api.get("/cognition/scheduler/status"), null),
    safeFetch(() => api.get("/cognition/scheduler/due-selection"), null),
    safeFetch(() => api.get("/cognition/observability/overview"), {}),
    safeFetch(() => api.get(observabilityTimelinePath()), { items: [] }),
  ]));
  state.observabilityOverview = observabilityOverview;
  state.schedulerStatus = schedulerStatus;
  state.schedulerDueSelection = schedulerDueSelection;
  state.cache.turns = asItems(turns);
  const selectedSchedulerChats = asItems(schedulerReport().selected);
  if (!state.schedulerChatId && selectedSchedulerChats.length > 0) {
    state.schedulerChatId = selectedSchedulerChats[0];
  }
  if (!state.observabilityFilters.chat_id && state.schedulerChatId) {
    state.observabilityFilters.chat_id = state.schedulerChatId;
  }
  if (state.schedulerChatId) {
    await loadSchedulerChatLoop(state.schedulerChatId);
  } else {
    state.schedulerChatLoop = null;
  }
  const unifiedRows = asItems(unifiedTimeline).map((item, index) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.domain || "-")}</td>
      <td>${escapeHtml(item.kind || "-")}</td>
      <td>${escapeHtml(item.level || "-")}</td>
      <td>${escapeHtml(item.title || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td class="row-actions"><button class="ghost-button" data-unified-detail="${index}" type="button">璇︽儏</button></td>
    </tr>
  `);
  const decisionRows = asItems(decisions).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.social_intent || item.action || "-")}</td>
      <td>${escapeHtml(item.action_tier || item.memory_policy || "-")}</td>
      <td>${escapeHtml(item.stance || item.style_policy || "-")}</td>
      <td class="row-actions"><button class="ghost-button" data-chat-trace="${attr(item.chat_id || "")}" type="button">chat 详情</button></td>
    </tr>
  `);
  const turnRows = state.cache.turns.map((item, index) => {
    const perception = item.perception || {};
    const attention = item.attention || {};
    const cognitive = item.cognitive || {};
    const continuity = item.continuity || {};
    const tools = item.tools || {};
    const toolCount = Array.isArray(tools.filtered_tools) ? tools.filtered_tools.length : 0;
    return `
      <tr>
        <td>${formatTime(item.created_at)}</td>
        <td>${escapeHtml(item.chat_id || perception.chat_id || "-")}</td>
        <td>${escapeHtml(perception.sender_name || perception.sender_id || "-")}</td>
        <td>${escapeHtml(attention.judge_action || "-")}</td>
        <td>${escapeHtml(cognitive.social_intent || cognitive.action || "-")}</td>
        <td>${escapeHtml(cognitive.think_level ?? "-")}</td>
        <td>${escapeHtml(tools.final_tier || cognitive.action_tier || "-")}</td>
        <td>${toolCount}</td>
        <td>${continuity.has_heartflow_context ? statusChip("Heartflow", "ok") : statusChip("none")}</td>
        <td class="row-actions"><button class="ghost-button" data-turn-detail="${index}" type="button">详情</button></td>
      </tr>
    `;
  });
  dashboardShell(`
    ${renderSchedulerDiagnosticsSection()}
    ${section("Global Observability Timeline", "统一 scheduler / heartflow / cognition / memory 的全局观测流。", table(["Time", "Domain", "Kind", "Level", "Title", "Summary", "Action"], unifiedRows))}
    ${section("主动决策池 Cognition", "最近 CognitiveLoop 决策，支持按 chat 查看决策/工具轨迹。", table(["Chat", "意图", "动作层级", "姿态", "操作"], decisionRows))}
    ${section("Turn Context", "每轮心智状态摘要：感知、注意力、主观决策、工具决策和连续性来源。", table(["时间", "Chat", "Sender", "Judge", "Intent", "Budget", "Tier", "工具数", "Heartflow", "操作"], turnRows))}
  `);
  $('[data-scheduler-load]')?.addEventListener("click", async () => {
    const input = $("#scheduler-chat-id");
    await loadSchedulerChatLoop(input ? input.value : state.schedulerChatId);
    renderDashboardCognition();
  });
  $$('[data-scheduler-chat]').forEach((button) => button.addEventListener("click", async () => {
    await loadSchedulerChatLoop(button.dataset.schedulerChat);
    renderDashboardCognition();
  }));
  $$('[data-chat-trace]').forEach((button) => button.addEventListener("click", () => openChatTrace(button.dataset.chatTrace)));
  $$('[data-turn-detail]').forEach((button) => button.addEventListener("click", () => openTurnTrace(Number(button.dataset.turnDetail))));
  $$('[data-unified-detail]').forEach((button) => button.addEventListener("click", () => {
    const item = asItems(unifiedTimeline)[Number(button.dataset.unifiedDetail)] || {};
    openModal("Unified Timeline Detail", `<pre>${json(item.raw || item.detail || {})}</pre>`);
  }));
}

async function loadUnifiedTimeline(chatId) {
  const buildUnifiedTimelinePath = (chatId) => `/cognition/chats/${segment(chatId)}/unified-timeline?limit=80&include=decision,tool,trace,memory`;
  return safeFetch(() => api.get(buildUnifiedTimelinePath(chatId)), { items: [] });
}

function renderThinkLevelSummary(cognitive) {
  const signals = Array.isArray(cognitive.think_signals) ? cognitive.think_signals.join(", ") : "";
  const skipSignals = Array.isArray(cognitive.cognitive_loop_skip_signals) ? cognitive.cognitive_loop_skip_signals.join(", ") : "";
  return `
    <div class="chip-row">
      <span class="chip">think_level: ${escapeHtml(cognitive.think_level ?? "-")}</span>
      <span class="chip">reason: ${escapeHtml(cognitive.think_reason || "-")}</span>
      <span class="chip">signals: ${escapeHtml(signals || "none")}</span>
      <span class="chip">loop_ran: ${escapeHtml(cognitive.cognitive_loop_ran ? "yes" : "no")}</span>
      <span class="chip">loop_skip: ${escapeHtml(cognitive.cognitive_loop_skipped_reason || "none")}</span>
      <span class="chip">loop_skip_signals: ${escapeHtml(skipSignals || "none")}</span>
      <span class="chip">readonly_allowed: ${escapeHtml(cognitive.readonly_tools_allowed ? "yes" : "no")}</span>
      <span class="chip">readonly_skip: ${escapeHtml(cognitive.readonly_tools_skip_reason || "none")}</span>
    </div>
    <pre>${json({
      think_level: cognitive.think_level,
      think_reason: cognitive.think_reason,
      think_signals: cognitive.think_signals || [],
      cognitive_loop_ran: Boolean(cognitive.cognitive_loop_ran),
      cognitive_loop_skipped_reason: cognitive.cognitive_loop_skipped_reason || "",
      cognitive_loop_skip_signals: cognitive.cognitive_loop_skip_signals || [],
      readonly_tools_allowed: Boolean(cognitive.readonly_tools_allowed),
      readonly_tools_skip_reason: cognitive.readonly_tools_skip_reason || "",
    })}</pre>
  `;
}

function renderToolRemovalSummary(tools) {
  const groups = [
    ["removed_by_energy", "energy"],
    ["removed_by_mood", "mood"],
    ["removed_by_hostility", "hostility"],
    ["removed_by_cooldown", "cooldown"],
    ["removed_by_caution", "caution"],
    ["removed_by_social_intent", "social_intent"],
  ];
  const chips = groups.map(([key, label]) => {
    const values = Array.isArray(tools[key]) ? tools[key] : [];
    const text = values.length ? values.join(", ") : "none";
    return `<span class="chip">${escapeHtml(label)}: ${escapeHtml(text)}</span>`;
  }).join(" ");
  return `<div class="chip-row">${chips}</div>`;
}

function renderFollowUpSummary(followUp) {
  const signals = Array.isArray(followUp.signals) ? followUp.signals.join(", ") : "";
  return `
    <div class="chip-row">
      <span class="chip">eligible: ${escapeHtml(followUp.eligible ? "yes" : "no")}</span>
      <span class="chip">skip: ${escapeHtml(followUp.skipped_reason || "none")}</span>
      <span class="chip">probability: ${escapeHtml(followUp.probability ?? 0)}</span>
      <span class="chip">llm_checked: ${escapeHtml(followUp.llm_checked ? "yes" : "no")}</span>
      <span class="chip">followed: ${escapeHtml(followUp.followed ? "yes" : "no")}</span>
      <span class="chip">reason: ${escapeHtml(followUp.reason || "none")}</span>
      <span class="chip">signals: ${escapeHtml(signals || "none")}</span>
    </div>
    <pre>${json(followUp || {})}</pre>
  `;
}

function renderSideInputTimings(sideInputs) {
  const timings = Array.isArray(sideInputs?.timings) ? sideInputs.timings : [];
  if (!timings.length) {
    return `<div class="empty-state compact"><p>No side input timing records for this turn.</p></div>`;
  }
  const totalMs = timings.reduce((sum, item) => sum + Number(item.elapsed_ms || 0), 0);
  const failed = timings.filter((item) => item.ok === false).length;
  const skipped = timings.filter((item) => item.skipped_reason).length;
  const chips = `
    <div class="chip-row">
      <span class="chip">items: ${escapeHtml(timings.length)}</span>
      <span class="chip">total: ${escapeHtml(totalMs.toFixed(2))}ms</span>
      <span class="chip">failed: ${escapeHtml(failed)}</span>
      <span class="chip">skipped: ${escapeHtml(skipped)}</span>
    </div>
  `;
  const rows = timings.map((item) => {
    const ok = item.ok === false ? statusChip("error", "danger") : statusChip(item.skipped_reason ? "skipped" : "ok", "ok");
    return `
      <tr>
        <td>${escapeHtml(item.name || "-")}</td>
        <td>${escapeHtml(item.elapsed_ms ?? 0)}ms</td>
        <td>${ok}</td>
        <td>${escapeHtml(item.skipped_reason || "-")}</td>
        <td>${escapeHtml(item.error || "-")}</td>
      </tr>
    `;
  });
  return `${chips}${table(["Input", "Elapsed", "Status", "Skipped", "Error"], rows)}<pre>${json(sideInputs || {})}</pre>`;
}

function openTurnTrace(index, source = state.cache.turns) {
  const item = source[index];
  if (!item) return toast("缺少 Turn Context 详情");
  openModal(`Turn Context: ${escapeHtml(item.chat_id || "-")}`, `
    <div class="grid two">
      ${section("感知 Perception", "", `<pre>${json(item.perception || {})}</pre>`)}
      ${section("注意力 Attention", "", `<pre>${json(item.attention || {})}</pre>`)}
      ${section("主观决策 Cognitive", "", `<pre>${json(item.cognitive || {})}</pre>`)}
      ${section("Think Level Budget", "0 quick / 1 normal / 2 deep / 3 tools or ReAct.", renderThinkLevelSummary(item.cognitive || {}))}
      ${section("记忆裁决 Memory", "", `<pre>${json(item.memory || {})}</pre>`)}
      ${section("Follow-up", "Follow-up budget, cooldown, and skipped reason.", renderFollowUpSummary(item.follow_up || {}))}
      ${section("Side Inputs Timings", "Budgeted prompt inputs, degradation status, and per-input latency.", renderSideInputTimings(item.side_inputs || {}))}
      ${section("工具决策 Tools", "", `${renderToolRemovalSummary(item.tools || {})}<pre>${json(item.tools || {})}</pre>`)}
    </div>
    ${section("连续性来源 Continuity", "", `<pre>${json(item.continuity || {})}</pre>`)}
  `);
}

async function renderDashboardTools() {
  const cached = getDashboardCache("tools");
  const [status, policy, calls] = cached || setDashboardCache("tools", await Promise.all([
    safeFetch(() => api.get("/tools/status"), {}),
    safeFetch(() => api.get("/tools/policy"), {}),
    safeFetch(() => api.get("/tools/recent-calls?limit=50"), { items: [] }),
  ]));
  const rows = asItems(calls).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.tool_tier || item.final_tier || "-")}</td>
      <td>${item.tool_count ?? (Array.isArray(item.filtered_tools) ? item.filtered_tools.length : "-")}</td>
      <td><pre>${json(item)}</pre></td>
    </tr>
  `);
  dashboardShell(`
    <div class="grid two">
      ${section("工具层级", "chat / guarded / full tool tier", `<pre>${json(status)}</pre>`)}
      ${section("工具策略", "Tool policy rules", `<pre>${json(policy)}</pre>`)}
    </div>
    ${section("工具链观测 Tools", "最近工具调用轨迹。", table(["Chat", "Tier", "工具数", "详情"], rows))}
  `);
}

async function openChatTrace(chatId) {
  if (!chatId) return toast("缺少 chat_id");
  const [decisions, tools, turns] = await Promise.all([
    safeFetch(() => api.get(`/cognition/chats/${segment(chatId)}/recent-decisions?limit=20`), { items: [] }),
    safeFetch(() => api.get(`/tools/chats/${segment(chatId)}/recent-calls?limit=20`), { items: [] }),
    safeFetch(() => api.get(`/cognition/chats/${segment(chatId)}/turns?limit=20`), { items: [] }),
  ]);
  openModal(`chat 轨迹：${escapeHtml(chatId)}`, `
    <h4>主动决策池</h4><pre>${json(decisions.items || [])}</pre>
    <h4>工具链观测</h4><pre>${json(tools.items || [])}</pre>
    <h4>Turn Context</h4><pre>${json(turns.items || [])}</pre>
  `);
}

async function loadLearning() {
  showLoading("正在读取主动学习与任务...");
  const [proactive, intents, dream, diary, wakeup, learning, feedback, sources, chats, cooldowns] = await Promise.all([
    safeFetch(() => api.get("/proactive/status"), {}),
    safeFetch(() => api.get("/proactive/intents?limit=50"), { items: [] }),
    safeFetch(() => api.get("/proactive/dream/status"), {}),
    safeFetch(() => api.get("/proactive/diary/status"), {}),
    safeFetch(() => api.get("/proactive/wakeup/status"), {}),
    safeFetch(() => api.get("/learning/status"), {}),
    safeFetch(() => api.get("/memory-feedback?limit=50"), { items: [] }),
    safeFetch(() => api.get("/memory-feedback/sources"), { items: [] }),
    safeFetch(() => api.get("/chats/active?max_age_seconds=1800"), { items: [] }),
    safeFetch(() => api.get("/learning/cooldowns"), {}),
  ]);
  const cards = [
    ["造梦空间", "Dream Agent", dream, "执行造梦序列", "run-dream"],
    ["日记写作", "Diary Agent", diary, "撰写今日日记", "run-diary"],
    ["沉淀审核", "Reflect / Learning", learning, "基于会话触发", ""],
  ].map(([title, subtitle, data, action, key]) => `
    <article class="feature-card">
      <div><h3>${title}</h3><p>${subtitle}</p><pre>${json(data)}</pre></div>
      ${key ? `<button class="primary-button" data-${key} type="button">${action}</button>` : `<span class="chip ok">${action}</span>`}
    </article>
  `).join("");
  const sourceChips = asItems(sources).map((item) => statusChip(`${item.source}: ${item.count}`, "ok")).join(" ");
  const feedbackRows = asItems(feedback).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td>${escapeHtml(item.guidance || "-")}</td>
      <td><button class="danger-button" data-disable-feedback="${attr(item.id || "")}" type="button">禁用反馈</button></td>
    </tr>
  `);
  const intentRows = asItems(intents).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp || item.created_at)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${item.reply_sent ? statusChip("sent", "ok") : statusChip(item.blocked_reason || item.status || "queued", item.blocked_reason ? "warn" : "")}</td>
      <td>${escapeHtml(item.blocked_reason || "-")}</td>
      <td>${escapeHtml(item.reason || item.guidance || "-")}</td>
    </tr>
  `);
  const chatRows = asItems(chats).map((chatId) => `
    <tr>
      <td>${escapeHtml(chatId)}</td>
      <td class="row-actions">
        <button class="ghost-button" data-chat-runtime="${attr(chatId)}" type="button">运行态</button>
        <button class="primary-button" data-run-reflect="${attr(chatId)}" type="button">立即反思</button>
        <button class="danger-button" data-clear-runtime="${attr(chatId)}" type="button">清理状态</button>
      </td>
    </tr>
  `);
  const expressionStats = learning.expression_patterns || {};
  const jargonStats = learning.jargons || {};
  const backlog = learning.backlog || {};
  const diagnostics = learning.diagnostics || {};
  const topBacklogRows = asItems(backlog.top_unprocessed_groups).map((item) => `
    <tr>
      <td>${escapeHtml(item.group_id || "-")}</td>
      <td>${item.count ?? 0}</td>
      <td>${formatTime(item.oldest_timestamp)}</td>
      <td>${formatTime(item.latest_timestamp)}</td>
    </tr>
  `);
  content().innerHTML = `
    ${pageHeader("主动学习与任务", "监控 AI 的独立思考周期、夜间造梦及反思过滤池。")}
    <div class="feature-grid">${cards}</div>
    ${section("学习产出", "人物画像、表达习惯和黑话是不同学习通道；表达/黑话默认先进入审核。", `
      <div class="grid">
        ${metric("表达习惯", expressionStats.total ?? 0)}
        ${metric("表达待审核", expressionStats.pending ?? 0)}
        ${metric("黑话词库", jargonStats.total ?? 0)}
        ${metric("黑话待审核", jargonStats.pending ?? 0)}
      </div>
    `)}
    ${section("积压学习诊断", "后台会低频扫描未处理消息，满足阈值后自动挖掘表达和黑话。", `
      <div class="grid">
        ${metric("积压学习", backlog.enabled ? "开启" : "关闭")}
        ${metric("触发阈值", backlog.threshold ?? diagnostics.backlog?.threshold ?? "—")}
        ${metric("每轮会话数", backlog.group_limit ?? diagnostics.backlog?.group_limit ?? "—")}
        ${metric("Worker", backlog.worker_running ? "运行中" : "未运行")}
      </div>
      ${table(["Chat", "未处理消息", "最早", "最新"], topBacklogRows)}
      <h4>最近一次后台学习</h4>
      <pre>${json(backlog.last_report || diagnostics.backlog?.last_report || {})}</pre>
    `)}
    <div class="grid two">
      ${section("主动组件调度 Proactive", "Proactive / Wakeup 状态。", `<pre>${json({ proactive, wakeup })}</pre>`)}
      ${section("表达冷却", "Expression selector cooldowns", `<pre>${json(cooldowns)}</pre>`)}
    </div>
    ${section("主动意图轨迹", "Wakeup / Heartflow 候选经安全裁决后进入主链路的结果。", table(["时间", "Chat", "来源", "状态", "阻断", "预览"], intentRows))}
    ${section("记忆反馈", "Memory feedback sources 与反馈列表。", `<div class="chip-row">${sourceChips || "<span class='muted'>暂无来源</span>"}</div>${table(["Chat", "来源", "摘要", "指引", "操作"], feedbackRows)}`)}
    ${section("活跃会话", "可触发独立反思或清理 runtime。", table(["Chat", "操作"], chatRows))}
  `;
  bindLearningActions();
}

function bindLearningActions() {
  $('[data-run-dream]')?.addEventListener("click", async () => {
    await api.post("/proactive/dream/run-once");
    toast("Dream 已调度");
  });
  $('[data-run-diary]')?.addEventListener("click", async () => {
    await api.post("/proactive/diary/run-once");
    toast("Diary 已调度");
  });
  $$('[data-disable-feedback]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.disableFeedback) return;
    if (!await confirmAction("禁用这条记忆反馈？")) return;
    await api.post(`/memory-feedback/${segment(button.dataset.disableFeedback)}/disable`);
    toast("反馈已禁用");
    loadLearning();
  }));
  $$('[data-chat-runtime]').forEach((button) => button.addEventListener("click", async () => {
    const result = await api.get(`/chats/${segment(button.dataset.chatRuntime)}/runtime`);
    openModal("Chat Runtime", `<pre>${json(result)}</pre>`);
  }));
  $$('[data-run-reflect]').forEach((button) => button.addEventListener("click", async () => {
    await api.post("/learning/reflect/run-once", { chat_id: button.dataset.runReflect });
    toast("反思任务已触发");
  }));
  $$('[data-clear-runtime]').forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("清理该 chat 的运行态？")) return;
    await api.post(`/chats/${segment(button.dataset.clearRuntime)}/runtime/clear`);
    toast("运行态已清理");
    loadLearning();
  }));
}

async function loadReviews() {
  showLoading("正在读取表达审核...");
  const expressionState = state.cache.reviews.expressionAll;
  const [expressionPending, expressionAll, jargonPending, jargonAll] = await Promise.all([
    safeFetch(() => api.get("/reviews/pending"), { items: [] }),
    safeFetch(() => api.get(`/reviews?page=${expressionState.page}&page_size=${expressionState.page_size}`), expressionState),
    safeFetch(() => api.get("/memories/jargon?status=review_pending&limit=200"), { items: [], total: 0 }),
    safeFetch(() => api.get("/memories/jargon?limit=200"), { items: [], total: 0 }),
  ]);
  const expressionPendingItems = asItems(expressionPending);
  const expressionAllItems = asItems(expressionAll);
  const jargonPendingItems = asItems(jargonPending);
  const jargonAllItems = asItems(jargonAll);
  state.cache.reviews.expressionPending = expressionPendingItems;
  state.cache.reviews.expressionAll = {
    items: expressionAllItems,
    total: Number(expressionAll.total ?? expressionAllItems.length),
    page: Number(expressionAll.page ?? expressionState.page),
    page_size: Number(expressionAll.page_size ?? expressionState.page_size),
  };
  state.cache.reviews.jargonPending = {
    items: jargonPendingItems,
    total: Number(jargonPending.total ?? jargonPendingItems.length),
    limit: Number(jargonPending.limit ?? 200),
    offset: Number(jargonPending.offset ?? 0),
  };
  state.cache.reviews.jargonAll = {
    items: jargonAllItems,
    total: Number(jargonAll.total ?? jargonAllItems.length),
    limit: Number(jargonAll.limit ?? 200),
    offset: Number(jargonAll.offset ?? 0),
  };
  const reviewMode = state.reviewTab.startsWith("jargon") ? "jargon" : "expression";
  const activeItems = {
    expression_pending: expressionPendingItems,
    expression_all: expressionAllItems,
    jargon_pending: jargonPendingItems,
    jargon_all: jargonAllItems,
  }[state.reviewTab] || [];
  const rows = activeItems.map((item) => {
    const id = item.id || item.review_id || "";
    const contentText = reviewMode === "jargon"
      ? `${item.content || "-"}${item.meaning ? `\n含义：${item.meaning}` : ""}${item.scene ? `\n场景：${item.scene}` : ""}`
      : (item.expression || item.text || item.pattern || item.content || "-");
    return `
      <tr>
        <td>${escapeHtml(id || "-")}</td>
        <td><pre>${escapeHtml(contentText)}</pre></td>
        <td>${statusChip(item.review_status || item.status || "pending", item.status === "rejected" ? "danger" : "")}</td>
        <td>${escapeHtml(item.weight ?? item.confidence ?? "-")}</td>
        <td class="row-actions">
          <button class="ghost-button" data-edit-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">编辑</button>
          <button class="primary-button" data-edit-approve-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">编辑通过</button>
          <button class="primary-button" data-approve-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">批准</button>
          <button class="ghost-button" data-edit-reject-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">备注驳回</button>
          <button class="danger-button" data-reject-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">驳回</button>
        </td>
      </tr>
    `;
  });
  const totals = `
    <div class="grid">
      ${metric("表达待审核", expressionPendingItems.length)}
      ${metric("表达语料", state.cache.reviews.expressionAll.total)}
      ${metric("黑话待审核", state.cache.reviews.jargonPending.total)}
      ${metric("黑话词库", state.cache.reviews.jargonAll.total)}
    </div>
  `;
  const title = state.reviewTab.startsWith("jargon") ? "黑话审核" : "表达习惯审核";
  const emptyText = state.reviewTab.startsWith("jargon")
    ? "当前黑话分类暂无数据。若 Dashboard 显示黑话待审核，请确认正在查看“黑话待审”。"
    : "当前表达习惯暂无数据。黑话审核在同页的“黑话待审/黑话全量”中查看。";
  content().innerHTML = `
    ${pageHeader("表达与黑话审核 Reviews", "表达习惯和黑话是两条学习通道；待审队列用于人工校准后通过，全量库查阅用于回看历史表达/黑话。")}
    ${totals}
    ${subTabs(state.reviewTab, [
      { id: "jargon_pending", label: "黑话待审" },
      { id: "jargon_all", label: "黑话全量" },
      { id: "expression_pending", label: "表达待审" },
      { id: "expression_all", label: "表达全量" },
    ], "review-tab")}
    ${section(title, "批准、驳回或查看 AI 提取的表达/黑话候选。", `${table(["ID", "内容", "状态", "权重/置信度", "操作"], rows, emptyText)}${state.reviewTab === "expression_all" ? `
      <div class="row-actions">
        <button class="ghost-button" data-review-page="${state.cache.reviews.expressionAll.page - 1}" type="button" ${state.cache.reviews.expressionAll.page <= 1 ? "disabled" : ""}>上一页</button>
        <span>第 ${state.cache.reviews.expressionAll.page} / ${Math.max(1, Math.ceil(state.cache.reviews.expressionAll.total / state.cache.reviews.expressionAll.page_size))} 页，共 ${state.cache.reviews.expressionAll.total} 条</span>
        <button class="ghost-button" data-review-page="${state.cache.reviews.expressionAll.page + 1}" type="button" ${state.cache.reviews.expressionAll.page * state.cache.reviews.expressionAll.page_size >= state.cache.reviews.expressionAll.total ? "disabled" : ""}>下一页</button>
      </div>` : ""}`)}
  `;
  $$('[data-review-tab]').forEach((button) => button.addEventListener("click", () => {
    state.reviewTab = button.dataset.reviewTab;
    loadReviews();
  }));
  $$('[data-review-page]').forEach((button) => button.addEventListener("click", () => {
    state.cache.reviews.expressionAll.page = Math.max(1, Number(button.dataset.reviewPage || 1));
    loadReviews();
  }));
  bindReviewActions();
}

function bindReviewActions() {
  $$('[data-edit-review]').forEach((button) => button.addEventListener("click", () => {
    const item = findReviewItem(button.dataset.reviewKind, button.dataset.editReview);
    if (!item) return toast("未找到待编辑项");
    if (button.dataset.reviewKind === "jargon") openJargonCalibration(item, "save");
    else openExpressionCalibration(item, "save");
  }));
  $$('[data-edit-approve-review]').forEach((button) => button.addEventListener("click", () => {
    const item = findReviewItem(button.dataset.reviewKind, button.dataset.editApproveReview);
    if (!item) return toast("未找到待编辑项");
    if (button.dataset.reviewKind === "jargon") openJargonCalibration(item, "approve");
    else openExpressionCalibration(item, "approve");
  }));
  $$('[data-edit-reject-review]').forEach((button) => button.addEventListener("click", () => {
    const item = findReviewItem(button.dataset.reviewKind, button.dataset.editRejectReview);
    if (!item) return toast("未找到待编辑项");
    if (button.dataset.reviewKind === "jargon") openJargonCalibration(item, "reject");
    else openExpressionCalibration(item, "reject");
  }));
  $$('[data-approve-review]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.approveReview) return;
    if (button.dataset.reviewKind === "jargon") {
      await api.post(`/memories/jargon/${segment(button.dataset.approveReview)}/approve`);
    } else {
      await api.post(`/reviews/${segment(button.dataset.approveReview)}/submit`, { action: "approve" });
    }
    toast("已批准");
    loadReviews();
  }));
  $$('[data-reject-review]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.rejectReview) return;
    if (button.dataset.reviewKind === "jargon") {
      await api.post(`/memories/jargon/${segment(button.dataset.rejectReview)}/reject`);
    } else {
      await api.post(`/reviews/${segment(button.dataset.rejectReview)}/submit`, { action: "reject" });
    }
    toast("已驳回");
    loadReviews();
  }));
}

async function loadMemories() {
  showLoading("正在读取记忆网络...");
  const memoryState = state.cache.memories.canonical;
  const kindParam = state.cache.memories.canonicalKind ? `&kind=${segment(state.cache.memories.canonicalKind)}` : "";
  const [canonical, events, reflections, nodes, jargon] = await Promise.all([
    safeFetch(() => api.get(`/memories/canonical?limit=${memoryState.limit}&offset=${memoryState.offset}${kindParam}`), memoryState),
    safeFetch(() => api.get("/memories/events"), { items: [] }),
    safeFetch(() => api.get(`/memories/reflections?month=${segment(state.cache.memories.month)}`), { items: [] }),
    safeFetch(() => api.get("/memories/nodes"), { items: [] }),
    safeFetch(() => api.get("/memories/jargon?limit=200"), { items: [], total: 0 }),
  ]);
  state.cache.memories.canonical = {
    items: asItems(canonical),
    total: Number(canonical.total ?? asItems(canonical).length),
    limit: Number(canonical.limit ?? memoryState.limit),
    offset: Number(canonical.offset ?? memoryState.offset),
  };
  state.cache.memories.events = asItems(events);
  state.cache.memories.reflections = asItems(reflections);
  state.cache.memories.nodes = asItems(nodes);
  state.cache.memories.jargon = {
    items: asItems(jargon),
    total: Number(jargon.total ?? asItems(jargon).length),
    limit: Number(jargon.limit ?? 200),
    offset: Number(jargon.offset ?? 0),
  };
  const tabItems = {
    canonical: state.cache.memories.canonical.items,
    events: state.cache.memories.events,
    reflections: state.cache.memories.reflections,
    nodes: state.cache.memories.nodes,
    jargon: state.cache.memories.jargon.items,
  }[state.memoryTab] || [];
  const rows = tabItems.map((item) => {
    const id = item.id || item.date || item.term || "-";
    const contentText = item.content || item.summary || item.narrative || item.reflection || item.meaning || "-";
    const actions = state.memoryTab === "jargon"
      ? `<button class="ghost-button" data-memory-jargon-edit="${attr(id)}" type="button">编辑</button><button class="danger-button" data-memory-delete="${attr(id)}" type="button">删除</button>`
      : `<button class="danger-button" data-memory-delete="${attr(id)}" type="button">删除</button>`;
    return `
      <tr>
        <td>${escapeHtml(id)}</td>
        <td>${escapeHtml(item.kind || item.status || item.date || "-")}</td>
        <td><pre>${escapeHtml(contentText)}</pre></td>
        <td>${escapeHtml(item.session_id || item.group_id || "-")}</td>
        <td>${escapeHtml(item.confidence ?? item.importance ?? "-")}</td>
        <td class="row-actions">${actions}</td>
      </tr>
    `;
  });
  const kindOptions = [
    ["", "全部类型"],
    ["fact", "事实"],
    ["topic", "话题"],
    ["feedback", "反馈"],
    ["memory", "普通记忆"],
    ["jargon", "黑话"],
    ["persona_lore", "角色原典"],
  ].map(([value, label]) => `<option value="${attr(value)}" ${state.cache.memories.canonicalKind === value ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
  const canonicalPager = state.memoryTab === "canonical" ? `
    <div class="row-actions">
      <label class="inline-label">类型 <select data-memory-kind>${kindOptions}</select></label>
      <button class="ghost-button" data-memory-page="${state.cache.memories.canonical.offset - state.cache.memories.canonical.limit}" type="button" ${state.cache.memories.canonical.offset <= 0 ? "disabled" : ""}>上一页</button>
      <span>第 ${Math.floor(state.cache.memories.canonical.offset / state.cache.memories.canonical.limit) + 1} / ${Math.max(1, Math.ceil(state.cache.memories.canonical.total / state.cache.memories.canonical.limit))} 页，共 ${state.cache.memories.canonical.total} 条</span>
      <button class="ghost-button" data-memory-page="${state.cache.memories.canonical.offset + state.cache.memories.canonical.limit}" type="button" ${state.cache.memories.canonical.offset + state.cache.memories.canonical.limit >= state.cache.memories.canonical.total ? "disabled" : ""}>下一页</button>
    </div>
  ` : "";
  const totals = `
    <div class="grid">
      ${metric("Canonical v2", state.cache.memories.canonical.total)}
      ${metric("黑话", state.cache.memories.jargon.total)}
      ${metric("旧事件", state.cache.memories.events.length)}
      ${metric("旧实体", state.cache.memories.nodes.length)}
    </div>
  `;
  const emptyText = state.memoryTab === "canonical"
    ? "Canonical v2 当前筛选无数据。可切换类型为“全部类型”查看完整长期记忆。"
    : "当前旧分类暂无数据；长期记忆主体请查看“Canonical 总览”。";
  content().innerHTML = `
    ${pageHeader("记忆网络 Memories", "以 Canonical v2 长期记忆为主视图；旧事件、反思和实体图谱保留为辅助诊断。")}
    ${totals}
    ${subTabs(state.memoryTab, [
      { id: "canonical", label: "Canonical 总览" },
      { id: "jargon", label: "黑话字典 Jargon" },
      { id: "events", label: "记忆碎片 Events" },
      { id: "reflections", label: "每日反思 Reflections" },
      { id: "nodes", label: "实体图谱 Nodes（旧实体图谱）" },
    ], "memory-tab")}
    ${section("记忆数据", state.memoryTab === "canonical" ? "当前展示 canonical_memories v2 数据。" : "当前展示旧版或专题数据。", `${canonicalPager}${table(["ID", "类型/状态", "内容", "会话", "权重", "操作"], rows, emptyText)}`)}
  `;
  $$('[data-memory-tab]').forEach((button) => button.addEventListener("click", () => {
    state.memoryTab = button.dataset.memoryTab;
    if (state.memoryTab === "canonical") state.cache.memories.canonical.offset = 0;
    loadMemories();
  }));
  $('[data-memory-kind]')?.addEventListener("change", (event) => {
    state.cache.memories.canonicalKind = event.target.value;
    state.cache.memories.canonical.offset = 0;
    loadMemories();
  });
  $$('[data-memory-page]').forEach((button) => button.addEventListener("click", () => {
    state.cache.memories.canonical.offset = Math.max(0, Number(button.dataset.memoryPage || 0));
    loadMemories();
  }));
  $$('[data-memory-jargon-edit]').forEach((button) => button.addEventListener("click", () => {
    const item = findMemoryItem("jargon", button.dataset.memoryJargonEdit);
    if (!item) return toast("未找到黑话记录");
    openJargonCalibration(item, "save");
  }));
  $$('[data-memory-delete]').forEach((button) => button.addEventListener("click", async () => {
    const id = button.dataset.memoryDelete;
    if (!id || !await confirmAction("删除这条记忆记录？", "danger")) return;
    if (state.memoryTab === "canonical") await api.post(`/memories/canonical/${segment(id)}/delete`);
    if (state.memoryTab === "events") await api.post(`/memories/events/${segment(id)}/delete`);
    if (state.memoryTab === "reflections") await api.post(`/memories/reflections/${segment(id)}/delete`);
    if (state.memoryTab === "nodes") await api.post(`/memories/nodes/${segment(id)}/delete`);
    if (state.memoryTab === "jargon") await api.post(`/memories/jargon/${segment(id)}/delete`);
    toast("记忆记录已删除");
    loadMemories();
  }));
}

async function loadUsers() {
  showLoading("正在读取用户画像...");
  const users = await safeFetch(() => api.get("/users"), { items: [] });
  state.cache.users = asItems(users);
  if (!state.activeUserId && state.cache.users[0]) state.activeUserId = state.cache.users[0].user_id || state.cache.users[0].id || "";
  renderUsers();
}

function renderUsers() {
  const users = state.cache.users || [];
  const active = users.find((item) => String(item.user_id || item.id || "") === String(state.activeUserId)) || users[0] || null;
  const list = users.map((user) => {
    const userId = String(user.user_id || user.id || "");
    const displayName = String(user.nickname || user.name || userId || "未命名用户");
    const identity = String(user.identity || "身份尚未定义");
    const rawScore = Number(user.social_score || 0);
    const score = Number.isFinite(rawScore) ? rawScore : 0;
    const scoreTone = score > 20 ? "positive" : (score < -10 ? "negative" : "neutral");
    const scorePosition = Math.max(3, Math.min(100, (score + 100) / 2));
    const searchText = `${displayName} ${identity} ${userId}`.toLocaleLowerCase();
    return `
      <button
        class="user-card ${userId === String(state.activeUserId) ? "active" : ""}"
        data-select-user="${attr(userId)}"
        data-user-search-text="${attr(searchText)}"
        type="button"
        aria-current="${userId === String(state.activeUserId) ? "true" : "false"}"
      >
        <span class="avatar">${escapeHtml(displayName.slice(0, 1).toUpperCase())}</span>
        <span class="user-card-body">
          <span class="user-card-heading">
            <strong title="${attr(displayName)}">${escapeHtml(displayName)}</strong>
            <span class="user-score ${scoreTone}">${escapeHtml(score)}</span>
          </span>
          <small class="user-card-identity">${escapeHtml(identity)}</small>
          <small class="user-card-id">QQ ${escapeHtml(userId || "-")}</small>
          <span class="user-relation-meter" aria-label="羁绊权重 ${attr(score)}">
            <span style="width:${scorePosition}%"></span>
          </span>
        </span>
      </button>
    `;
  }).join("");
  content().innerHTML = `
    ${pageHeader("用户画像", "管理用户身份、关系权重和长期画像切片。")}
    <div class="users-layout">
      <aside class="users-sidebar panel">
        <div class="users-sidebar-head">
          <div>
            <h3>用户列表</h3>
            <p data-user-visible-count>共 ${users.length} 位</p>
          </div>
          <span class="chip muted">${users.length}</span>
        </div>
        <label class="user-search-field">
          <span>搜索用户</span>
          <input data-user-search type="search" value="${attr(state.userSearch)}" placeholder="昵称、身份或 QQ 号" autocomplete="off">
        </label>
        <div class="user-list" role="list">${list || "<div class='empty-state compact'><p>暂无用户画像</p></div>"}</div>
      </aside>
      <section class="user-profile-detail">${active ? renderUserDetail(active) : "<div class='empty-state'><h3>请选择用户</h3></div>"}</section>
    </div>
  `;
  $$('[data-select-user]').forEach((button) => button.addEventListener("click", () => {
    state.usersScrollTop = Number($(".user-list")?.scrollTop || 0);
    state.activeUserId = button.dataset.selectUser;
    renderUsers();
  }));
  $('[data-user-search]')?.addEventListener("input", (event) => {
    state.userSearch = event.target.value;
    applyUserSearchFilter();
  });
  applyUserSearchFilter();
  const userList = $(".user-list");
  if (userList) userList.scrollTop = Number(state.usersScrollTop || 0);
  bindUserActions(active);
}

function applyUserSearchFilter() {
  const query = String(state.userSearch || "").trim().toLocaleLowerCase();
  let visible = 0;
  $$('[data-user-search-text]').forEach((card) => {
    const matches = !query || String(card.dataset.userSearchText || "").includes(query);
    card.hidden = !matches;
    if (matches) visible += 1;
  });
  const count = $('[data-user-visible-count]');
  if (count) count.textContent = query ? `显示 ${visible} / ${state.cache.users.length} 位` : `共 ${state.cache.users.length} 位`;
}

function renderUserDetail(user) {
  const userId = String(user.user_id || user.id || "");
  const displayName = String(user.nickname || user.name || userId || "未命名用户");
  const identity = String(user.identity || "身份尚未定义");
  const tags = Array.isArray(user.tags)
    ? user.tags
    : String(user.tags || "").split(",").map((item) => item.trim()).filter(Boolean);
  const score = Number.isFinite(Number(user.social_score)) ? Number(user.social_score) : 0;
  return `
    <section class="profile-summary">
      <span class="avatar large">${escapeHtml(displayName.slice(0, 1).toUpperCase())}</span>
      <div class="profile-summary-main">
        <span class="profile-kicker">当前画像</span>
        <h2>${escapeHtml(displayName)}</h2>
        <p>${escapeHtml(identity)} <span aria-hidden="true">·</span> QQ ${escapeHtml(userId || "-")}</p>
        <div class="profile-tag-row">
          ${tags.slice(0, 6).map((tag) => `<span class="chip muted">${escapeHtml(tag)}</span>`).join("") || "<span class='muted'>暂无标签</span>"}
        </div>
      </div>
      <div class="profile-score-card">
        <span>羁绊权重</span>
        <strong>${escapeHtml(score)}</strong>
      </div>
    </section>
    <section class="panel user-profile-panel">
      <div class="section-head">
        <div>
          <h3>基础画像</h3>
          <p>身份字段与画像分析。</p>
        </div>
      </div>
      <div class="user-form-grid">
        ${formField("nickname", "系统称呼", user.nickname || "")}
        ${formField("identity", "底层身份", user.identity || "")}
        ${formField("social_score", "羁绊权重", user.social_score ?? "", "number")}
        ${formField("tags", "全局标签", Array.isArray(user.tags) ? user.tags.join(", ") : (user.tags || ""))}
      </div>
      <label class="profile-analysis-field">画像分析概览<textarea data-user-field="persona_analysis" placeholder="暂无画像分析">${escapeHtml(user.persona_analysis || "")}</textarea></label>
      <div class="profile-form-actions">
        <button class="primary-button" data-save-user type="button">保存基础画像</button>
        <button class="danger-button" data-delete-user type="button">删除用户画像</button>
      </div>
    </section>
    <section class="profile-slices-section">
      <div class="profile-slices-head">
        <div>
          <h3>画像切片</h3>
          <p>按维度维护可检索的长期认知点。</p>
        </div>
        <span class="chip muted">${SLICE_FIELDS.length} 个维度</span>
      </div>
      <div class="profile-slice-grid">
        ${SLICE_FIELDS.map(([field, label]) => renderSliceSection(user, field, label)).join("")}
      </div>
    </section>
  `;
}

function renderSliceSection(user, field, label) {
  const values = Array.isArray(user[field]) ? user[field] : [];
  return `
    <article class="profile-slice-card">
      <div class="profile-slice-card-head">
        <div>
          <h3>${escapeHtml(label)}</h3>
          <code>${escapeHtml(field)}</code>
        </div>
        <span class="slice-count">${values.length}</span>
      </div>
      <div class="profile-chip-list">
        ${values.map((value, index) => `
          <span class="profile-chip">
            <span>${escapeHtml(value)}</span>
            <button class="chip-remove" data-delete-slice="${field}:${index}" type="button" aria-label="删除${attr(label)}：${attr(value)}">×</button>
          </span>
        `).join("") || "<span class='slice-empty'>暂无内容</span>"}
      </div>
      <div class="inline-form profile-slice-add">
        <input data-slice-input="${field}" placeholder="新增${attr(label)}">
        <button class="ghost-button" data-add-slice="${field}" type="button">添加</button>
      </div>
    </article>
  `;
}

function bindUserActions(active) {
  if (!active) return;
  $('[data-save-user]')?.addEventListener("click", async () => {
    const body = {};
    $$('[data-user-field]').forEach((input) => { body[input.dataset.userField] = input.value; });
    await api.post(`/users/${segment(active.user_id || active.id)}`, body);
    toast("用户画像已保存");
    loadUsers();
  });
  $('[data-delete-user]')?.addEventListener("click", async () => {
    if (!await confirmAction("删除这个用户画像？该操作不可恢复。", "danger")) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/delete`);
    state.activeUserId = "";
    toast("用户画像已删除");
    loadUsers();
  });
  $$('[data-add-slice]').forEach((button) => button.addEventListener("click", async () => {
    const field = button.dataset.addSlice;
    const input = $(`[data-slice-input="${field}"]`);
    if (!input?.value?.trim()) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/slices`, { type: field, content: input.value.trim() });
    toast("切片已添加");
    loadUsers();
  }));
  $$('[data-slice-input]').forEach((input) => input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    $(`[data-add-slice="${cssEscape(input.dataset.sliceInput)}"]`)?.click();
  }));
  $$('[data-delete-slice]').forEach((button) => button.addEventListener("click", async () => {
    const [field, index] = button.dataset.deleteSlice.split(":");
    await api.post(`/users/${segment(active.user_id || active.id)}/slices/${segment(index)}/delete`, { type: field });
    toast("切片已删除");
    loadUsers();
  }));
}

async function loadPersonaSlices() {
  showLoading("正在读取角色切片诊断...");
  state.cache.personaSlicesError = null;
  try {
    const result = await api.get("/persona/slices");
    state.cache.personaSlices = result;
  } catch (error) {
    state.cache.personaSlices = {};
    state.cache.personaSlicesError = error.message || String(error);
    toast("角色切片读取失败");
  }
  renderPersonaSlices();
}

function renderReadonlyText(value, empty = "暂无内容") {
  const text = String(value || "").trim();
  return text ? `<p class="readonly-text">${escapeHtml(text)}</p>` : `<div class="empty-state compact"><p>${escapeHtml(empty)}</p></div>`;
}

function renderShardCards(persona = {}) {
  const shards = persona.shards || {};
  const labels = persona.shard_labels || {};
  const overrides = persona.manual_overrides || {};
  const keys = persona.shard_order || Object.keys(labels).concat(Object.keys(shards)).filter((value, index, array) => array.indexOf(value) === index);
  return `
    <div class="persona-shard-grid">
      ${keys.map((key) => {
        const value = shards[key] || "";
        const ready = String(value || "").trim().length > 0;
        const overrideKey = `shards.${key}`;
        const manual = Boolean(overrides[overrideKey]);
        return `
          <article class="persona-shard-card ${ready ? "" : "missing"}">
            <div class="field-head">
              <div><strong>${escapeHtml(labels[key] || key)}</strong> <code>${escapeHtml(key)}</code></div>
              <div class="row-actions">
                ${manual ? statusChip("已人工修改", "ok") : statusChip("AI 生成", "muted")}
                <button class="ghost-button" data-edit-persona-shard="${attr(key)}" type="button">编辑</button>
                ${manual ? `<button class="ghost-button" data-restore-persona-field="${attr(overrideKey)}" type="button">恢复</button>` : ""}
              </div>
            </div>
            ${renderReadonlyText(value, "该切片尚未生成")}
          </article>
        `;
      }).join("") || `<div class="empty-state"><p>暂无切片定义</p></div>`}
    </div>
  `;
}

async function savePersonaSlices(changes) {
  const persona = state.cache.personaSlices || {};
  const updated = await api.post("/persona/slices/update", {
    cache_key: persona.cache_key || "",
    expected_timestamp: persona.timestamp || 0,
    ...changes,
  });
  state.cache.personaSlices = updated;
  renderPersonaSlices();
  toast("派生人格已保存，下一轮聊天生效");
}

async function restorePersonaFields(fields = []) {
  const persona = state.cache.personaSlices || {};
  const confirmed = await confirmAction(
    fields.length ? "确认将所选内容恢复为最初的 AI 生成版本？" : "确认恢复全部人工修改？",
  );
  if (!confirmed) return;
  const updated = await api.post("/persona/slices/restore", {
    cache_key: persona.cache_key || "",
    expected_timestamp: persona.timestamp || 0,
    fields,
  });
  state.cache.personaSlices = updated;
  renderPersonaSlices();
  toast("已恢复 AI 生成版本");
}

function openPersonaCoreEditor(persona) {
  openFormModal(
    "微调核心人格与说话方式",
    [
      { name: "summary", label: "核心摘要", type: "textarea", rows: 8 },
      { name: "first_person_rewrite", label: "第一人称自觉", type: "textarea", rows: 8 },
      { name: "style", label: "说话方式", type: "textarea", rows: 8 },
    ],
    persona,
    async (data) => {
      try {
        const changed = {};
        ["summary", "first_person_rewrite", "style"].forEach((field) => {
          if (String(data[field] || "").trim() !== String(persona[field] || "").trim()) changed[field] = data[field];
        });
        if (!Object.keys(changed).length) {
          toast("内容没有变化");
          return;
        }
        await savePersonaSlices(changed);
      } catch (error) {
        toast(`保存失败：${error.message || error}`);
        throw error;
      }
    },
  );
}

function openPersonaShardEditor(persona, key) {
  const label = persona.shard_labels?.[key] || key;
  openFormModal(
    `微调${label}`,
    [{ name: "value", label: `${label}切片内容`, type: "textarea", rows: 12 }],
    { value: persona.shards?.[key] || "" },
    async (data) => {
      try {
        if (String(data.value || "").trim() === String(persona.shards?.[key] || "").trim()) {
          toast("内容没有变化");
          return;
        }
        await savePersonaSlices({ shards: { [key]: data.value } });
      } catch (error) {
        toast(`保存失败：${error.message || error}`);
        throw error;
      }
    },
  );
}

function renderPersonaSlices() {
  const error = state.cache.personaSlicesError;
  if (error) {
    content().innerHTML = `
      ${pageHeader("角色切片诊断 Persona Slices", "只读查看 AstrMai 从 AstrBot 人格中提炼出的角色理解结果。", `<button class="primary-button" data-retry-persona-slices type="button">重新读取</button>`)}
      ${section("读取失败", "这不是空数据，而是角色切片 API 没有成功返回。", `
        <div class="empty-state">
          <h3>无法读取 /persona/slices</h3>
          <p>${escapeHtml(error)}</p>
          <p>请确认插件已经重载、Plugin Page 后端路由已注册，或者查看 AstrBot 后台日志里的具体异常。</p>
        </div>
      `)}
      ${section("管理边界", "配置与原始人格不在本页管理，避免 AstrMai 插件页形成第二套入口。", `
        <div class="chip-row">
          ${statusChip("配置：AstrBot 插件配置页", "muted")}
          ${statusChip("原始人格：AstrBot 人格管理", "muted")}
          ${statusChip("本页：只读诊断", "ok")}
        </div>
      `)}
    `;
    $('[data-retry-persona-slices]')?.addEventListener("click", loadPersonaSlices);
    return;
  }
  const persona = state.cache.personaSlices || {};
  const summary = persona.summary || "";
  const firstPerson = persona.first_person_rewrite || "";
  const style = persona.style || "";
  const ready = Boolean(persona.is_full_ready);
  const pending = Boolean(persona.pending_task);
  content().innerHTML = `
    ${pageHeader("角色理解与微调 Persona Slices", "查看并微调 AstrMai 从 AstrBot 人格中提炼出的核心内容与 8 类角色切片。", `<button class="ghost-button" data-persona-slices-json type="button">诊断 JSON</button>${Object.keys(persona.manual_overrides || {}).length ? `<button class="ghost-button" data-restore-persona-all type="button">恢复全部 AI 版本</button>` : ""}`)}
    ${section("管理边界", "本页只修改 AstrMai 的派生人格缓存，保存后下一轮聊天生效；不会修改 AstrBot 原始人格或 self-lore。", `
      <div class="chip-row">
        ${statusChip("配置：AstrBot 插件配置页", "muted")}
        ${statusChip("原始人格：AstrBot 人格管理", "muted")}
        ${statusChip("本页：派生人格微调", "ok")}
      </div>
    `)}
    <div class="grid">
      ${metric("Persona ID", persona.persona_id || "-", "来自 _conf_schema.json 的 persona.persona_id")}
      ${metric("Cache Key", persona.cache_key || "-", "PersonaSummarizer 当前缓存键")}
      ${metric("切片状态", ready ? "ready" : (pending ? "building" : "partial"), ready ? "8 类切片已生成" : "可能仍在后台构建")}
      ${metric("缓存时间", formatTime(persona.timestamp), "persona_cache 时间戳")}
    </div>
    <div class="grid two">
      ${section("核心摘要 Summary", `用于压缩长人格，降低即时回复 token 成本。${persona.manual_overrides?.summary ? " 当前为人工版本。" : ""}`, renderReadonlyText(summary), persona.manual_overrides?.summary ? `<button class="ghost-button" data-restore-persona-field="summary" type="button">恢复</button>` : "")}
      ${section("第一人称自觉 First Person Rewrite", `ContextEngine 优先使用这段短自述来稳定扮演视角。${persona.manual_overrides?.first_person_rewrite ? " 当前为人工版本。" : ""}`, renderReadonlyText(firstPerson), persona.manual_overrides?.first_person_rewrite ? `<button class="ghost-button" data-restore-persona-field="first_person_rewrite" type="button">恢复</button>` : "")}
    </div>
    ${section("风格指南 Style", `每轮用于约束说话方式，不直接等同原始人格 prompt。${persona.manual_overrides?.style ? " 当前为人工版本。" : ""}`, renderReadonlyText(style), `<button class="primary-button" data-edit-persona-core type="button">编辑核心内容与说话方式</button>${persona.manual_overrides?.style ? `<button class="ghost-button" data-restore-persona-field="style" type="button">恢复</button>` : ""}`)}
    ${section("八维角色切片", "CognitiveLoop 可按 retrieve_keys 临时加载这些切片；缺失时不会阻断聊天。", renderShardCards(persona))}
    ${section("自我原典与缓存", "self_lore 是长期记忆中的角色原典索引，本页不展示原文，只展示安全摘要。", `
      <div class="chip-row">
        ${statusChip(`self_lore: ${persona.self_lore?.available ? "available" : "unavailable"}`, persona.self_lore?.available ? "ok" : "muted")}
        ${statusChip(`raw_length: ${persona.raw_length || 0}`, "muted")}
        ${statusChip(`cache_keys: ${(persona.cache_keys || []).length}`, "muted")}
      </div>
    `)}
  `;
  $('[data-persona-slices-json]')?.addEventListener("click", () => openModal("Persona Slices Diagnostic", `<pre>${json(state.cache.personaSlices || {})}</pre>`));
  $('[data-edit-persona-core]')?.addEventListener("click", () => openPersonaCoreEditor(persona));
  $$('[data-edit-persona-shard]').forEach((button) => button.addEventListener("click", () => openPersonaShardEditor(persona, button.dataset.editPersonaShard)));
  $$('[data-restore-persona-field]').forEach((button) => button.addEventListener("click", () => restorePersonaFields([button.dataset.restorePersonaField]).catch((error) => toast(`恢复失败：${error.message || error}`))));
  $('[data-restore-persona-all]')?.addEventListener("click", () => restorePersonaFields().catch((error) => toast(`恢复失败：${error.message || error}`)));
}

async function loadCurrent() {
  const loaders = {
    dashboard: loadDashboard,
    learning: loadLearning,
    reviews: loadReviews,
    memories: loadMemories,
    users: loadUsers,
    personaSlices: loadPersonaSlices,
  };
  if (state.current !== "dashboard") {
    stopSchedulerPolling();
  }
  try {
    await (loaders[state.current] || loadDashboard)();
  } catch (error) {
    content().innerHTML = `<section class="empty-state"><h2>加载失败</h2><p>${escapeHtml(error.message || error)}</p></section>`;
  }
}

async function init() {
  setBridgeStatus("Bridge 初始化中", "muted");
  renderTabs();
  $("#refresh-button").addEventListener("click", () => {
    if (state.current === "dashboard") clearDashboardCache(state.dashboardTab);
    loadCurrent();
  });
  window.addEventListener("hashchange", () => {
    const next = normalizeTabId(location.hash.replace("#", "") || "dashboard");
    if (next !== state.current) {
      state.current = next;
      if (location.hash.replace("#", "") !== next) location.hash = next;
      renderTabs();
      loadCurrent();
    }
  });

  try {
    state.bridge = await waitForBridge();
    await readyBridge(state.bridge);
  } catch (error) {
    setBridgeStatus("Bridge 连接失败", "danger");
    content().innerHTML = `<section class="empty-state"><h2>没有检测到 AstrBotPluginPage</h2><p>请从 AstrBot WebUI 的插件详情页打开本页面，不要直接打开 HTML 文件。</p><p>${escapeHtml(error.message || error)}</p></section>`;
    return;
  }

  setBridgeStatus("Bridge 已连接", "ok");
  await loadCurrent();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
