const API_PREFIX = "admin";
const ADMIN_BUILD_VERSION = "2026.07.28-r7";
const SCHEDULER_POLL_INTERVAL_MS = 45000;
const DASHBOARD_CACHE_TTL_MS = 180000;
const DATA_CACHE_TTL_MS = 180000;
const REVIEW_PAGE_SIZE = 25;
const MEMORY_PAGE_SIZE = 25;

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
  learningTab: "overview",
  reviewTab: "jargon_pending",
  memoryTab: "canonical",
  toolsTab: "executions",
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
  personaRegenerationPollTimer: null,
  lastApiErrorToastAt: 0,
  lastApiErrorKey: "",
  selectedReviews: new Set(),
  activeUserId: "",
  userSearch: "",
  usersScrollTop: 0,
  dashboardCache: {},
  dataCache: {},
  requestGeneration: 0,
  cache: {
    reviews: {
      expressionPending: [],
      expressionAll: { items: [], total: 0, page: 1, page_size: 20 },
      jargonPending: { items: [], total: 0, limit: REVIEW_PAGE_SIZE, offset: 0 },
      jargonAll: { items: [], total: 0, limit: REVIEW_PAGE_SIZE, offset: 0 },
      filters: { status: "", group_id: "", keyword: "" },
    },
    memories: {
      month: new Date().toISOString().slice(0, 7),
      canonical: { items: [], total: 0, limit: MEMORY_PAGE_SIZE, offset: 0 },
      canonicalKind: "",
      canonicalStatus: "active",
      qualityOverview: { counts: {}, index: {} },
      qualityAudit: null,
      events: [],
      reflections: [],
      nodes: [],
      jargon: { items: [], total: 0, limit: MEMORY_PAGE_SIZE, offset: 0 },
    },
    users: [],
    personaSlices: {},
    personaSlicesError: null,
    turns: [],
    learningFeedback: { items: [], total: 0, limit: REVIEW_PAGE_SIZE, offset: 0 },
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

function previewText(value, maxLength = 220) {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!normalized) return "—";
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}…` : normalized;
}

function formatScore(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function detailsJson(title, value, open = false) {
  return `<details class="diagnostic-details" ${open ? "open" : ""}><summary>${escapeHtml(title)}</summary><pre>${escapeHtml(json(value))}</pre></details>`;
}

function currentViewKey() {
  const suffix = state.current === "dashboard"
    ? state.dashboardTab
    : state.current === "learning"
      ? state.learningTab
      : state.current === "reviews"
        ? state.reviewTab
        : state.current === "memories"
          ? state.memoryTab
          : "";
  return `${state.current}:${suffix}`;
}

function beginViewRequest() {
  state.requestGeneration += 1;
  return { generation: state.requestGeneration, key: currentViewKey() };
}

function isCurrentView(request) {
  return request && request.generation === state.requestGeneration && request.key === currentViewKey();
}

async function cachedFetch(key, fetchFn, fallback, ttlMs = DATA_CACHE_TTL_MS) {
  const cached = state.dataCache[key];
  if (cached && Date.now() - Number(cached.updatedAt || 0) <= ttlMs) return cached.data;
  // OPT-16/WU-09: 错误回退不得当作新鲜数据写缓存——否则一次瞬时 bridge/后端故障
  // 会让该 tab 稳定空白 180 秒（切 tab 也不重试）；失败时沿用旧缓存值但不刷新时间戳
  try {
    const data = await fetchFn();
    state.dataCache[key] = { data, updatedAt: Date.now() };
    return data;
  } catch (error) {
    return await safeFetch(() => { throw error; }, cached?.data ?? fallback);
  }
}

function clearDataCache(prefix = "") {
  if (!prefix) {
    state.dataCache = {};
    return;
  }
  Object.keys(state.dataCache).forEach((key) => {
    if (key.startsWith(prefix)) delete state.dataCache[key];
  });
}

function hasDataCachePrefix(prefix) {
  return Object.keys(state.dataCache).some((key) => key.startsWith(prefix));
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

function renderOffsetPager(dataName, page) {
  const limit = Math.max(1, Number(page?.limit || 20));
  const offset = Math.max(0, Number(page?.offset || 0));
  const total = Math.max(0, Number(page?.total || 0));
  const current = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return `
    <div class="pager">
      <button class="ghost-button" data-${dataName}-page="${Math.max(0, offset - limit)}" type="button" ${offset <= 0 ? "disabled" : ""}>上一页</button>
      <span>第 ${current} / ${pages} 页，共 ${total} 条</span>
      <button class="ghost-button" data-${dataName}-page="${offset + limit}" type="button" ${offset + limit >= total ? "disabled" : ""}>下一页</button>
    </div>
  `;
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

function reviewSelectionKey(kind, id) {
  return `${kind}:${String(id || "")}`;
}

function clearReviewSelection() {
  state.selectedReviews.clear();
}

function selectedReviewIds(kind) {
  const prefix = `${kind}:`;
  return Array.from(state.selectedReviews)
    .filter((key) => key.startsWith(prefix))
    .map((key) => key.slice(prefix.length))
    .filter(Boolean);
}

function syncReviewSelectionControls(kind, items) {
  const pageIds = items
    .map((item) => String(item.id || item.review_id || ""))
    .filter(Boolean);
  const selected = selectedReviewIds(kind).filter((id) => pageIds.includes(id));
  const count = $('[data-review-selection-count]');
  if (count) count.textContent = `已选 ${selected.length} 条`;
  $$('[data-review-batch-action]').forEach((button) => {
    button.disabled = selected.length === 0;
  });
  const selectAll = $('[data-review-select-all]');
  if (selectAll) {
    selectAll.checked = pageIds.length > 0 && selected.length === pageIds.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < pageIds.length;
  }
}

async function runBatchReviewAction(kind, action) {
  const ids = selectedReviewIds(kind);
  if (!ids.length) return toast("请先选择要处理的候选");
  const actionLabel = action === "approve" ? "通过" : "拒绝";
  if (!await confirmAction(`确认批量${actionLabel} ${ids.length} 条${kind === "jargon" ? "黑话" : "表达"}候选？`)) return;
  const result = await api.post("/reviews/batch", { kind, action, ids });
  clearDataCache("reviews:");
  clearDataCache("learning:status");
  clearReviewSelection();
  if (result?.status === "degraded") {
    toast(`批量${actionLabel}未完成：后端审核数据源不可用`);
  } else {
    toast(`已${actionLabel} ${result?.updated ?? ids.length} 条候选`);
  }
  loadReviews();
}

function findMemoryItem(tab, id) {
  const source = state.cache.memories[tab];
  const items = Array.isArray(source) ? source : (source?.items || []);
  return items.find((item) => String(item.id || item.date || item.term || "") === String(id)) || null;
}

function openCanonicalMemoryEditor(item) {
  openFormModal(
    "修正长期记忆",
    [
      { name: "content", label: "完整内容", type: "textarea", rows: 8 },
      { name: "summary", label: "摘要", type: "textarea", rows: 4 },
      {
        name: "status",
        label: "状态",
        type: "select",
        options: [
          { value: "active", label: "启用" },
          { value: "review_pending", label: "待审核隔离" },
          { value: "rejected", label: "已驳回" },
          { value: "stale", label: "已过期" },
        ],
      },
      {
        name: "visibility",
        label: "召回范围",
        type: "select",
        options: [
          { value: "auto_and_tool", label: "自动召回与工具均可用" },
          { value: "tool_only", label: "仅工具查询" },
          { value: "maintenance_only", label: "仅维护与审核可见" },
        ],
      },
      { name: "confidence", label: "置信度（0-1）", type: "number", cast: "float" },
      { name: "importance", label: "重要度（0-1）", type: "number", cast: "float" },
      { name: "tags", label: "标签（逗号分隔）" },
    ],
    {
      content: item.content || "",
      summary: item.summary || "",
      status: item.status || "active",
      visibility: item.visibility || "auto_and_tool",
      confidence: item.confidence ?? 0.5,
      importance: item.importance ?? 0.5,
      tags: Array.isArray(item.tags) ? item.tags.join(",") : "",
    },
    async (data) => {
      const result = await api.post(`/memories/canonical/${segment(item.id)}`, data);
      if (result?.changed && result?.projected) {
        toast("长期记忆已修正并同步索引");
      } else if (result?.changed) {
        toast("长期记忆已修正；索引将在维护任务中补齐");
      } else {
        toast("长期记忆没有变化");
      }
      clearDataCache("memories:");
      await loadMemories();
    },
  );
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
      clearDataCache("reviews:");
      clearDataCache("memories:");
      clearDataCache("learning:status");
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
      clearDataCache("reviews:");
      clearDataCache("learning:status");
      await loadReviews();
    },
    submitText,
  );
}

async function loadDashboard() {
  const hasCachedDashboard = getDashboardCache(state.dashboardTab)
    || (state.dashboardTab === "tools" && hasDataCachePrefix("tools:"));
  if (!hasCachedDashboard) {
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
  const request = beginViewRequest();
  const cached = getDashboardCache("overview");
  const [snapshot, health, capabilities, models, observabilityOverview, runtimeStatus] = cached || setDashboardCache("overview", await Promise.all([
    safeFetch(() => api.get("/dashboard"), {}),
    safeFetch(() => api.get("/runtime/health"), {}),
    safeFetch(() => api.get("/runtime/capabilities"), {}),
    safeFetch(() => api.get("/runtime/models"), {}),
    safeFetch(() => api.get("/cognition/observability/overview"), {}),
    safeFetch(() => api.get("/runtime/status"), {}),
  ]));
  if (!isCurrentView(request)) return;
  state.observabilityOverview = observabilityOverview;
  const healthData = health || {};
  const running = Boolean(healthData.running);
  const obs = observabilityOverview || {};
  const obsSnapshot = obs.snapshot || {};
  const components = runtimeStatus?.components || {};
  const featureFlags = runtimeStatus?.infrastructure?.features || {};
  const capabilityState = (value) => value === true ? "ok" : value === false ? "danger" : "muted";
  const capabilityLabel = (label, value) => `${label} ${value === true ? "ready" : value === false ? "offline" : "unknown"}`;
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
      ${section("能力状态", "只显示关键能力；完整信息按需展开。", `
        <div class="chip-row">
          ${statusChip(capabilityLabel("Gateway", components.gateway), capabilityState(components.gateway))}
          ${statusChip(capabilityLabel("Memory", components.memory_engine), capabilityState(components.memory_engine))}
          ${statusChip(capabilityLabel("Heartflow", components.proactive_task ?? featureFlags.proactive_enabled), capabilityState(components.proactive_task ?? featureFlags.proactive_enabled))}
        </div>
        ${detailsJson("查看完整能力矩阵", { components, feature_flags: featureFlags, capabilities })}
      `)}
      ${section("模型健康", "当前模型池与降级组件摘要。", `
        <div class="grid compact-grid">
          ${metric("降级组件", healthData.degraded_count ?? 0)}
          ${metric("运行阶段", healthData.boot_phase || "unknown")}
        </div>
        ${detailsJson("查看模型与健康诊断", { models, health: healthData })}
      `)}
    </div>
    ${section("统一观测", "默认只显示统计摘要，异常详情按需展开。", `
      <div class="grid">
        ${metric("Retained Events", obsSnapshot.retained_events ?? 0)}
        ${metric("Tracked Chats", obsSnapshot.retained_chats ?? 0)}
        ${metric("Warnings", obsSnapshot.recent_warning_count ?? 0)}
        ${metric("Errors", obsSnapshot.recent_error_count ?? 0)}
      </div>
      ${detailsJson("查看观测快照", obsSnapshot)}
      ${detailsJson(`查看最近异常（${asItems(obs.recent_errors).length}）`, asItems(obs.recent_errors))}
    `)}
  `);
}

async function renderDashboardHeartflow() {
  const request = beginViewRequest();
  const cached = getDashboardCache("heartflow");
  const [status, chats, impulses, timeline, digests, intents] = cached || setDashboardCache("heartflow", await Promise.all([
    safeFetch(() => api.get("/heartflow/status"), {}),
    safeFetch(() => api.get("/heartflow/chats"), { items: [] }),
    safeFetch(() => api.get("/heartflow/impulses?limit=50"), { items: [] }),
    safeFetch(() => api.get("/heartflow/timeline?limit=80"), { items: [] }),
    safeFetch(() => api.get("/heartflow/topic-digests?limit=50"), { items: [] }),
    safeFetch(() => api.get("/proactive/intents?limit=50"), { items: [] }),
  ]));
  if (!isCurrentView(request)) return;
  const impulseRows = asItems(impulses).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.pulse_type || "-")}</td>
      <td>${item.visible_candidate_allowed ? statusChip("allowed", "ok") : statusChip(item.blocked_reason || "hidden", "")}</td>
      <td>${item.requires_synthetic_event ? statusChip("synthetic", "warn") : statusChip("hidden", "")}</td>
      <td>${String(Boolean(item.dispatch_enabled))}</td>
      <td>${String(Boolean(item.synthetic_event_queued))}</td>
      <td><button class="ghost-button" data-json-payload="${attr(json(item.safety_checks || {}))}" type="button">查看</button></td>
    </tr>
  `);
  const timelineRows = asItems(timeline).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.kind || "-")}</td>
      <td>${escapeHtml(item.label || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td><button class="ghost-button" data-json-payload="${attr(json(item.payload || {}))}" type="button">查看</button></td>
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
    ${section("心智流状态", "区分调度内核运行与会话管理器空闲，空闲不等于故障。", `
      <div class="grid">
        ${metric("运行状态", status.operational_state || status.state || "unknown")}
        ${metric("状态原因", status.operational_reason || status.reason || "—")}
        ${metric("活跃会话", status.manager?.active_chats ?? status.active_chats ?? 0)}
        ${metric("Kernel 跟踪", status.kernel?.tracked_chats ?? 0, "调度跟踪不等于已产生 Heartflow state")}
      </div>
      ${detailsJson("查看完整 Heartflow 状态", status)}
    `)}
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
  $$('[data-json-payload]').forEach((button) => button.addEventListener("click", () => {
    openModal("诊断详情", `<pre>${escapeHtml(json(parseJsonSafe(button.dataset.jsonPayload, {})))}</pre>`);
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
    clearDashboardCache("cognition");
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
          ${detailsJson("批次计划", state.schedulerStatus?.proactive?.scheduler_batch_plan || {})}
          ${detailsJson("配额跳过统计", state.schedulerStatus?.proactive?.quota_skip_counts || {})}
          ${detailsJson("轮询模式切换", state.schedulerStatus?.proactive?.poll_mode_transition || {})}
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
          ${detailsJson("待处理信号", chatData.scheduler_pending_signals || {})}
          ${detailsJson("Due selection 报告", report)}
        `)}
      </div>
    `,
  );
}

async function renderDashboardCognition() {
  const request = beginViewRequest();
  const cached = getDashboardCache("cognition");
  const [decisions, turns, schedulerStatus, schedulerDueSelection, observabilityOverview, unifiedTimeline] = cached || setDashboardCache("cognition", await Promise.all([
    safeFetch(() => api.get("/cognition/recent-decisions?limit=50"), { items: [] }),
    safeFetch(() => api.get("/cognition/recent-turns?limit=50"), { items: [] }),
    safeFetch(() => api.get("/cognition/scheduler/status"), null),
    safeFetch(() => api.get("/cognition/scheduler/due-selection"), null),
    safeFetch(() => api.get("/cognition/observability/overview"), {}),
    safeFetch(() => api.get(observabilityTimelinePath()), { items: [] }),
  ]));
  if (!isCurrentView(request)) return;
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
      <td class="row-actions"><button class="ghost-button" data-unified-detail="${index}" type="button">详情</button></td>
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
    ${section("运行账本 Run Ledger", "OPT-11/WU-11：调用账本、阶段账本、回复统计与预算此前已随接口返回但页面零呈现，排查延迟要下载 trace 文件。", `
      <div class="grid two">
        ${section("预算 Budget", "", `<pre>${json(item.budget || {})}</pre>`)}
        ${section("回复统计 Reply Stats", "", `<pre>${json(item.reply_stats || {})}</pre>`)}
      </div>
      <pre>${json({ llm_call_ledger: item.llm_call_ledger || [], stage_ledger: item.stage_ledger || [], memory_funnel: item.memory_funnel || {} })}</pre>`)}
  `);
}

async function renderDashboardTools() {
  const request = beginViewRequest();
  const common = await cachedFetch("tools:common", () => Promise.all([
    safeFetch(() => api.get("/tools/status"), {}),
    safeFetch(() => api.get("/tools/policy"), {}),
  ]), [{}, {}]);
  const [status, policy] = Array.isArray(common) ? common : [{}, {}];
  const isCatalog = state.toolsTab === "catalog";
  const path = isCatalog
    ? "/tools/catalog"
    : (state.toolsTab === "executions" ? "/tools/executions?limit=50" : "/tools/recent-calls?limit=50");
  const calls = await cachedFetch(`tools:${state.toolsTab}`, () => api.get(path), { items: [] }, 60000);
  if (!isCurrentView(request)) return;
  const catalogRows = asItems(calls).map((item, index) => `
    <tr>
      <td>${escapeHtml(item.name || "-")}</td>
      <td>${statusChip(item.tier || "unknown", item.tier === "full_only" ? "warn" : "ok")}</td>
      <td>${escapeHtml(Array.isArray(item.families) ? item.families.join(", ") : "-")}</td>
      <td><button class="ghost-button" data-tool-detail="${index}" type="button">详情</button></td>
    </tr>
  `);
  const rows = asItems(calls).map((item, index) => `
    <tr>
      <td>${formatTime(item.created_at || item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.tool_name || item.name || (Array.isArray(item.tool_names) ? item.tool_names.join(", ") : "") || "-")}</td>
      <td>${escapeHtml(item.tool_tier || item.final_tier || "-")}</td>
      <td>${statusChip(item.status || item.lifecycle || "observed", item.status === "failed" ? "danger" : "ok")}</td>
      <td><button class="ghost-button" data-tool-detail="${index}" type="button">详情</button></td>
    </tr>
  `);
  const renderedRows = isCatalog ? catalogRows : rows;
  const renderedHeaders = isCatalog ? ["工具", "层级", "能力族", "操作"] : ["时间", "Chat", "工具", "Tier", "状态", "操作"];
  const viewLabel = state.toolsTab === "executions" ? "真实执行" : (isCatalog ? "能力目录" : "策略披露");
  const viewDescription = state.toolsTab === "executions"
    ? "工具实际被调用后的生命周期轨迹。"
    : (isCatalog ? "当前运行时注册的工具、所属层级和能力族。" : "模型可见工具、过滤结果和层级披露，不等同于实际执行。");
  const emptyText = state.toolsTab === "executions"
    ? "暂无真实工具执行记录。仅有策略披露不代表工具已调用。"
    : (isCatalog ? "当前没有可展示的工具目录。" : "暂无工具策略披露记录。");
  dashboardShell(`
    ${subTabs(state.toolsTab, [
      { id: "executions", label: "真实执行" },
      { id: "disclosure", label: "策略披露" },
      { id: "catalog", label: "能力目录" },
    ], "tools-tab")}
    <div class="grid two">
      ${section("工具运行摘要", "真实执行与模型可见性分开统计。", `
        <div class="grid compact-grid">
          ${metric("记录数", calls.total ?? asItems(calls).length)}
          ${metric("当前视图", viewLabel)}
        </div>
        ${detailsJson("查看工具层级", status)}
      `)}
      ${section("工具策略", "按需展开完整策略，不在首页铺开大 JSON。", detailsJson("查看策略详情", policy))}
    </div>
    ${section("工具链观测 Tools", viewDescription, table(renderedHeaders, renderedRows, emptyText))}
  `);
  $$('[data-tools-tab]').forEach((button) => button.addEventListener("click", () => {
    if (state.toolsTab === button.dataset.toolsTab) return;
    state.toolsTab = button.dataset.toolsTab;
    renderDashboardTools();
  }));
  $$('[data-tool-detail]').forEach((button) => button.addEventListener("click", () => {
    openModal("工具轨迹详情", `<pre>${escapeHtml(json(asItems(calls)[Number(button.dataset.toolDetail)] || {}))}</pre>`);
  }));
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
  const request = beginViewRequest();
  const cacheKey = `learning:${state.learningTab}`;
  if (!hasDataCachePrefix(cacheKey)) showLoading("正在读取主动学习与任务...");
  const learning = await cachedFetch("learning:status", () => api.get("/learning/status"), {});
  let body = "";
  const expressionStats = learning.expression_patterns || {};
  const jargonStats = learning.jargons || {};
  const backlog = learning.backlog || {};
  const diagnostics = learning.diagnostics || {};
  if (state.learningTab === "overview") {
    const [proactive, dream, diary, wakeup, cooldowns] = await cachedFetch(cacheKey, () => Promise.all([
      safeFetch(() => api.get("/proactive/status"), {}),
      safeFetch(() => api.get("/proactive/dream/status"), {}),
      safeFetch(() => api.get("/proactive/diary/status"), {}),
      safeFetch(() => api.get("/proactive/wakeup/status"), {}),
      safeFetch(() => api.get("/learning/cooldowns"), {}),
    ]), [{}, {}, {}, {}, {}]);
    const topBacklogRows = asItems(backlog.top_unprocessed_groups).map((item) => `
      <tr><td>${escapeHtml(item.group_id || "-")}</td><td>${item.count ?? 0}</td><td>${formatTime(item.oldest_timestamp)}</td><td>${formatTime(item.latest_timestamp)}</td></tr>
    `);
    const expressionReport = diagnostics.mining?.expression || diagnostics.expression || {};
    const jargonReport = diagnostics.mining?.jargon || diagnostics.jargon || {};
    body = `
      <div class="grid">
        ${metric("表达习惯", expressionStats.total ?? 0, `${expressionStats.pending ?? 0} 待审核`)}
        ${metric("黑话词库（已通过）", jargonStats.approved ?? 0, `${jargonStats.pending ?? 0} 待审核`)}
        ${metric("积压学习", backlog.enabled ? "开启" : "关闭", `阈值 ${backlog.threshold ?? diagnostics.backlog?.threshold ?? "—"}`)}
        ${metric("Worker", backlog.worker_running ? "运行中" : "未运行", `每轮 ${backlog.group_limit ?? diagnostics.backlog?.group_limit ?? "—"} 会话`)}
      </div>
      ${section("学习漏斗与最近诊断", "明确区分没有输入、噪声过滤、候选不足和成功写入。", `
        <div class="grid two">
          <div>${statusChip("表达提取", "ok")}${detailsJson("表达提取诊断", expressionReport, true)}</div>
          <div>${statusChip("黑话提取", "ok")}${detailsJson("黑话提取诊断", jargonReport, true)}</div>
        </div>
        ${table(["Chat", "未处理消息", "最早", "最新"], topBacklogRows, "当前没有达到学习阈值的会话。")}
        ${detailsJson("后台扫描完整报告", backlog.last_report || diagnostics.backlog?.last_report || {})}
      `)}
      ${section("表达历史回填", "只重新分析指定会话的历史消息，不重跑黑话，也不会改变消息的已处理状态。建议先预检，再确认写入。", `
        <div class="form-grid">
          <label>会话 ID<input id="expression-backfill-chat" type="text" placeholder="例如 ff:GroupMessage:123456"></label>
          <label>最多读取消息数<input id="expression-backfill-limit" type="number" min="10" max="500" value="120"></label>
          <label>回看天数<input id="expression-backfill-days" type="number" min="1" max="30" value="7"></label>
        </div>
        <div class="row-actions">
          <button class="ghost-button" data-expression-backfill-dry type="button">预检候选</button>
          <button class="primary-button" data-expression-backfill-run type="button">确认写入待审库</button>
        </div>
        ${detailsJson("最近回填结果", diagnostics.expression_backfill || {})}
      `)}
      <div class="feature-grid">
        <article class="feature-card"><div><h3>造梦空间</h3><p>${escapeHtml(dream.state || dream.status || "按计划运行")}</p>${detailsJson("诊断", dream)}</div><button class="primary-button" data-run-dream type="button">执行造梦序列</button></article>
        <article class="feature-card"><div><h3>日记写作</h3><p>${escapeHtml(diary.state || diary.status || "按计划运行")}</p>${detailsJson("诊断", diary)}</div><button class="primary-button" data-run-diary type="button">撰写今日日记</button></article>
        <article class="feature-card"><div><h3>主动组件调度 Proactive</h3><p>Wakeup 与 Proactive 运行摘要</p>${detailsJson("查看状态", { proactive, wakeup })}</div></article>
      </div>
      ${section("表达冷却", "仅在排查表达选择时展开。", detailsJson("查看 cooldown", cooldowns))}
    `;
  } else if (state.learningTab === "intents") {
    const intents = await cachedFetch(cacheKey, () => api.get("/proactive/intents?limit=50"), { items: [] }, 60000);
    const rows = asItems(intents).map((item) => `<tr><td>${formatTime(item.timestamp || item.created_at)}</td><td>${escapeHtml(item.chat_id || "-")}</td><td>${escapeHtml(item.source || "-")}</td><td>${item.reply_sent ? statusChip("已发送", "ok") : statusChip(item.blocked_reason || item.status || "候选", item.blocked_reason ? "warn" : "")}</td><td>${escapeHtml(item.blocked_reason || "-")}</td><td>${escapeHtml(previewText(item.reason || item.guidance, 160))}</td></tr>`);
    body = section("主动意图轨迹", "候选经过正常回复判决后的发送或阻断结果。", table(["时间", "Chat", "来源", "状态", "阻断", "摘要"], rows, "暂无主动意图记录；这通常表示当前没有满足条件的主动候选。"));
  } else if (state.learningTab === "feedback") {
    const page = state.cache.learningFeedback;
    const [feedback, sources] = await cachedFetch(`${cacheKey}:${page.offset}`, () => Promise.all([
      safeFetch(() => api.get(`/memory-feedback?limit=${page.limit}&offset=${page.offset}`), { items: [] }),
      safeFetch(() => api.get("/memory-feedback/sources"), { items: [] }),
    ]), [{ items: [] }, { items: [] }]);
    state.cache.learningFeedback = { items: asItems(feedback), total: Number(feedback.total ?? asItems(feedback).length), limit: page.limit, offset: page.offset };
    const rows = state.cache.learningFeedback.items.map((item) => `<tr><td>${escapeHtml(item.chat_id || item.session_id || "-")}</td><td>${escapeHtml(item.source_label || item.source || "-")}</td><td>${escapeHtml(previewText(item.summary, 180))}</td><td>${escapeHtml(previewText(item.guidance, 180))}</td><td>${statusChip(item.persisted === false ? "仅当前运行有效" : (item.expiry_state || "短期持久反馈"), item.expiry_state === "即将过期" ? "warn" : "ok")}</td><td>${detailsJson("技术详情", { source: item.source, tags: item.tags || [], payload: item.payload || {}, feedback_schema_version: item.feedback_schema_version, valid_until: item.valid_until })}</td><td><button class="danger-button" data-disable-feedback="${attr(item.id || "")}" type="button">禁用</button></td></tr>`);
    const sourceChips = asItems(sources).map((item) => statusChip(`${item.source_label || item.source}: ${item.count}`, "ok")).join(" ");
    body = section("记忆反馈", "展示系统从近期交流中提炼的短期行为提醒。默认只显示仍然有效的记录，旧格式会自动转换为中文。", `<div class="chip-row">${sourceChips || "<span class='muted'>暂无反馈来源</span>"}</div>${table(["会话", "来源", "摘要", "下一轮指引", "有效状态", "详情", "操作"], rows, "暂无记忆反馈。")}${renderOffsetPager("learning-feedback", state.cache.learningFeedback)}`);
  } else {
    const chats = await cachedFetch(cacheKey, () => api.get("/chats/active?max_age_seconds=1800"), { items: [] }, 60000);
    const rows = asItems(chats).map((chatId) => `<tr><td>${escapeHtml(chatId)}</td><td class="row-actions"><button class="ghost-button" data-chat-runtime="${attr(chatId)}" type="button">运行态</button><button class="primary-button" data-run-reflect="${attr(chatId)}" type="button">立即反思</button><button class="danger-button" data-clear-runtime="${attr(chatId)}" type="button">清理状态</button></td></tr>`);
    body = section("活跃会话", "仅显示最近 30 分钟活跃会话，可触发独立反思。", table(["Chat", "操作"], rows, "最近 30 分钟没有活跃会话。"));
  }
  if (!isCurrentView(request)) return;
  content().innerHTML = `
    ${pageHeader("主动学习与任务", "监控 AI 的独立思考周期、夜间造梦及反思过滤池。")}
    ${subTabs(state.learningTab, [
      { id: "overview", label: "学习概览" },
      { id: "intents", label: "主动意图" },
      { id: "feedback", label: "记忆反馈" },
      { id: "sessions", label: "活跃会话" },
    ], "learning-tab")}
    ${body}
  `;
  $$('[data-learning-tab]').forEach((button) => button.addEventListener("click", () => {
    if (state.learningTab === button.dataset.learningTab) return;
    state.learningTab = button.dataset.learningTab;
    loadLearning();
  }));
  $$('[data-learning-feedback-page]').forEach((button) => button.addEventListener("click", () => {
    state.cache.learningFeedback.offset = Math.max(0, Number(button.dataset.learningFeedbackPage || 0));
    loadLearning();
  }));
  bindLearningActions();
}

function bindLearningActions() {
  const runExpressionBackfill = async (dryRun) => {
    const chatId = String($("#expression-backfill-chat")?.value || "").trim();
    if (!chatId) return toast("请先填写会话 ID");
    const limit = Math.max(10, Math.min(500, Number($("#expression-backfill-limit")?.value || 120)));
    const days = Math.max(1, Math.min(30, Number($("#expression-backfill-days")?.value || 7)));
    if (!dryRun && !await confirmAction("把本次表达候选写入待审库？历史消息状态不会改变。")) return;
    const result = await api.post("/learning/expression-backfill", {
      chat_id: chatId,
      limit,
      max_age_seconds: days * 86400,
      dry_run: dryRun,
    });
    openModal(dryRun ? "表达回填预检" : "表达回填结果", `<pre>${escapeHtml(json(result))}</pre>`);
    clearDataCache("learning:");
  };
  $('[data-expression-backfill-dry]')?.addEventListener("click", () => runExpressionBackfill(true));
  $('[data-expression-backfill-run]')?.addEventListener("click", () => runExpressionBackfill(false));
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
    clearDataCache("learning:feedback");
    loadLearning();
  }));
  $$('[data-chat-runtime]').forEach((button) => button.addEventListener("click", async () => {
    const result = await api.get(`/chats/${segment(button.dataset.chatRuntime)}/runtime`);
    openModal("Chat Runtime", `<pre>${json(result)}</pre>`);
  }));
  $$('[data-run-reflect]').forEach((button) => button.addEventListener("click", async () => {
    await api.post("/learning/reflect/run-once", { chat_id: button.dataset.runReflect });
    toast("反思任务已触发");
    clearDataCache("learning:");
  }));
  $$('[data-clear-runtime]').forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("清理该 chat 的运行态？")) return;
    await api.post(`/chats/${segment(button.dataset.clearRuntime)}/runtime/clear`);
    toast("运行态已清理");
    clearDataCache("learning:sessions");
    loadLearning();
  }));
}

async function loadReviews() {
  const request = beginViewRequest();
  const tabKey = `reviews:${state.reviewTab}`;
  if (!hasDataCachePrefix(tabKey)) showLoading("正在读取表达审核...");
  const learning = await cachedFetch("learning:status", () => api.get("/learning/status"), {});
  let activePage;
  if (state.reviewTab === "jargon_pending" || state.reviewTab === "jargon_all") {
    const target = state.reviewTab === "jargon_pending" ? state.cache.reviews.jargonPending : state.cache.reviews.jargonAll;
    const statusParam = state.reviewTab === "jargon_pending" ? "status=review_pending&" : "status=active&";
    const query = state.cache.reviews.filters.keyword ? `&query=${segment(state.cache.reviews.filters.keyword)}` : "";
    activePage = await cachedFetch(`${tabKey}:${target.offset}:${state.cache.reviews.filters.keyword}`, () => api.get(`/memories/jargon?${statusParam}limit=${target.limit}&offset=${target.offset}${query}`), target);
    const normalized = { items: asItems(activePage), total: Number(activePage.total ?? asItems(activePage).length), limit: target.limit, offset: target.offset };
    if (state.reviewTab === "jargon_pending") state.cache.reviews.jargonPending = normalized;
    else state.cache.reviews.jargonAll = normalized;
    activePage = normalized;
  } else if (state.reviewTab === "expression_all") {
    const target = state.cache.reviews.expressionAll;
    // OPT-04/WU-10: 关键字下推给后端（后端已支持 keyword），否则搜索只作用于当前页
    const expressionKeyword = state.cache.reviews.filters.keyword ? `&keyword=${segment(state.cache.reviews.filters.keyword)}` : "";
    activePage = await cachedFetch(`${tabKey}:${target.page}:${state.cache.reviews.filters.keyword}`, () => api.get(`/reviews?page=${target.page}&page_size=${target.page_size}${expressionKeyword}`), target);
    state.cache.reviews.expressionAll = { items: asItems(activePage), total: Number(activePage.total ?? asItems(activePage).length), page: Number(activePage.page ?? target.page), page_size: Number(activePage.page_size ?? target.page_size) };
    activePage = state.cache.reviews.expressionAll;
  } else {
    activePage = await cachedFetch(tabKey, () => api.get("/reviews/pending"), { items: [] });
    state.cache.reviews.expressionPending = asItems(activePage);
    activePage = { items: state.cache.reviews.expressionPending, total: state.cache.reviews.expressionPending.length };
  }
  if (!isCurrentView(request)) return;
  const reviewMode = state.reviewTab.startsWith("jargon") ? "jargon" : "expression";
  const batchEnabled = state.reviewTab === "jargon_pending" || state.reviewTab === "expression_pending";
  const keyword = String(state.cache.reviews.filters.keyword || "").trim().toLowerCase();
  const activeItems = asItems(activePage).filter((item) => !keyword || json(item).toLowerCase().includes(keyword));
  const rows = activeItems.map((item) => {
    const id = item.id || item.review_id || "";
    const selection = batchEnabled
      ? `<td><input type="checkbox" data-review-select data-review-kind="${attr(reviewMode)}" data-review-id="${attr(id)}" ${state.selectedReviews.has(reviewSelectionKey(reviewMode, id)) ? "checked" : ""}></td>`
      : "";
    const contentText = reviewMode === "jargon"
      ? `${item.content || "-"}${item.meaning ? `\n含义：${item.meaning}` : ""}${item.scene ? `\n场景：${item.scene}` : ""}`
      : (item.expression || item.text || item.pattern || item.content || "-");
    return `
      <tr>
        ${selection}
        <td>${escapeHtml(id || "-")}</td>
        <td><div class="content-preview">${escapeHtml(previewText(contentText, 180))}</div></td>
        <td>${statusChip(item.review_status || item.status || "pending", item.status === "rejected" ? "danger" : "")}</td>
        <td>${formatScore(item.weight ?? item.confidence)}</td>
        <td class="row-actions">
          <button class="ghost-button" data-review-detail="${attr(id)}" data-review-kind="${reviewMode}" type="button">详情</button>
          <button class="ghost-button" data-edit-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">编辑</button>
          <button class="primary-button" data-edit-approve-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">编辑通过</button>
          <button class="primary-button" data-approve-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">批准</button>
          ${reviewMode === "jargon" ? "" : `<button class="ghost-button" data-edit-reject-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">备注驳回</button>`}
          <button class="danger-button" data-reject-review="${attr(id)}" data-review-kind="${reviewMode}" type="button">${reviewMode === "jargon" ? (state.reviewTab === "jargon_all" ? "下架" : "驳回") : "驳回"}</button>
        </td>
      </tr>
    `;
  });
  const expressionStats = learning.expression_patterns || {};
  const jargonStats = learning.jargons || {};
  const totals = `
    <div class="grid">
      ${metric("表达待审核", expressionStats.pending ?? state.cache.reviews.expressionPending.length)}
      ${metric("表达语料", expressionStats.total ?? state.cache.reviews.expressionAll.total)}
      ${metric("黑话待审核", jargonStats.pending ?? state.cache.reviews.jargonPending.total)}
      ${metric("黑话词库（已通过）", jargonStats.approved ?? state.cache.reviews.jargonAll.total)}
    </div>
  `;
  const title = state.reviewTab.startsWith("jargon") ? "黑话审核" : "表达习惯审核";
  const emptyText = state.reviewTab.startsWith("jargon")
    ? "当前黑话分类暂无数据。若 Dashboard 显示黑话待审核，请确认正在查看“黑话待审”。"
    : "当前表达习惯暂无数据。黑话审核在同页的“黑话待审/黑话全量”中查看。";
  const batchToolbar = batchEnabled ? `
    <div class="filter-bar review-batch-toolbar">
      <label class="inline-label"><input type="checkbox" data-review-select-all> 全选当前页</label>
      <span class="muted" data-review-selection-count>已选 0 条</span>
      <button class="primary-button" data-review-batch-action="approve" type="button" disabled>批量通过</button>
      <button class="danger-button" data-review-batch-action="reject" type="button" disabled>批量拒绝</button>
      <button class="ghost-button" data-review-clear-selection type="button">清空选择</button>
    </div>
  ` : "";
  const reviewHeaders = batchEnabled ? ["选择", "ID", "内容", "状态", "权重/置信度", "操作"] : ["ID", "内容", "状态", "权重/置信度", "操作"];
  content().innerHTML = `
    ${pageHeader("表达与黑话审核 Reviews", "待审队列和全量库查阅按当前分类加载；候选可先查看证据，再编辑、通过或驳回。")}
    ${totals}
    <div class="filter-bar"><label class="inline-label">搜索 <input data-review-search value="${attr(state.cache.reviews.filters.keyword)}" placeholder="词语、含义或场景"></label>${state.reviewTab.startsWith("jargon") ? `<button class="ghost-button" data-jargon-noise-preview type="button">噪声预检</button>` : ""}</div>
    ${subTabs(state.reviewTab, [
      { id: "jargon_pending", label: "黑话待审" },
      { id: "jargon_all", label: "黑话词库（已通过）" },
      { id: "expression_pending", label: "表达待审" },
      { id: "expression_all", label: "表达全量" },
    ], "review-tab")}
    ${section(title, "批准、驳回或查看 AI 提取的表达/黑话候选。", `${batchToolbar}${table(reviewHeaders, rows, emptyText)}${state.reviewTab === "expression_all" ? `
      <div class="row-actions">
        <button class="ghost-button" data-review-page="${state.cache.reviews.expressionAll.page - 1}" type="button" ${state.cache.reviews.expressionAll.page <= 1 ? "disabled" : ""}>上一页</button>
        <span>第 ${state.cache.reviews.expressionAll.page} / ${Math.max(1, Math.ceil(state.cache.reviews.expressionAll.total / state.cache.reviews.expressionAll.page_size))} 页，共 ${state.cache.reviews.expressionAll.total} 条</span>
        <button class="ghost-button" data-review-page="${state.cache.reviews.expressionAll.page + 1}" type="button" ${state.cache.reviews.expressionAll.page * state.cache.reviews.expressionAll.page_size >= state.cache.reviews.expressionAll.total ? "disabled" : ""}>下一页</button>
      </div>` : state.reviewTab.startsWith("jargon") ? renderOffsetPager(state.reviewTab === "jargon_pending" ? "jargon-pending" : "jargon-all", activePage) : ""}`)}
  `;
  $$('[data-review-tab]').forEach((button) => button.addEventListener("click", () => {
    clearReviewSelection();
    state.reviewTab = button.dataset.reviewTab;
    loadReviews();
  }));
  $$('[data-review-page]').forEach((button) => button.addEventListener("click", () => {
    clearReviewSelection();
    state.cache.reviews.expressionAll.page = Math.max(1, Number(button.dataset.reviewPage || 1));
    loadReviews();
  }));
  $('[data-review-search]')?.addEventListener("change", (event) => {
    clearReviewSelection();
    state.cache.reviews.filters.keyword = event.target.value.trim();
    state.cache.reviews.jargonPending.offset = 0;
    state.cache.reviews.jargonAll.offset = 0;
    clearDataCache("reviews:");
    loadReviews();
  });
  $$('[data-jargon-pending-page]').forEach((button) => button.addEventListener("click", () => {
    clearReviewSelection();
    state.cache.reviews.jargonPending.offset = Math.max(0, Number(button.dataset.jargonPendingPage || 0));
    loadReviews();
  }));
  $$('[data-jargon-all-page]').forEach((button) => button.addEventListener("click", () => {
    clearReviewSelection();
    state.cache.reviews.jargonAll.offset = Math.max(0, Number(button.dataset.jargonAllPage || 0));
    loadReviews();
  }));
  $$('[data-review-detail]').forEach((button) => button.addEventListener("click", () => {
    const item = findReviewItem(button.dataset.reviewKind, button.dataset.reviewDetail);
    openModal("候选详情与证据", `<pre>${escapeHtml(json(item || {}))}</pre>`);
  }));
  $('[data-jargon-noise-preview]')?.addEventListener("click", openJargonNoisePreview);
  if (batchEnabled) {
    $$('[data-review-select]').forEach((checkbox) => checkbox.addEventListener("change", () => {
      const key = reviewSelectionKey(checkbox.dataset.reviewKind, checkbox.dataset.reviewId);
      if (checkbox.checked) state.selectedReviews.add(key);
      else state.selectedReviews.delete(key);
      syncReviewSelectionControls(reviewMode, activeItems);
    }));
    $('[data-review-select-all]')?.addEventListener("change", (event) => {
      activeItems.forEach((item) => {
        const id = String(item.id || item.review_id || "");
        if (!id) return;
        const key = reviewSelectionKey(reviewMode, id);
        if (event.target.checked) state.selectedReviews.add(key);
        else state.selectedReviews.delete(key);
      });
      $$('[data-review-select]').forEach((checkbox) => { checkbox.checked = event.target.checked; });
      syncReviewSelectionControls(reviewMode, activeItems);
    });
    $('[data-review-clear-selection]')?.addEventListener("click", () => {
      clearReviewSelection();
      $$('[data-review-select]').forEach((checkbox) => { checkbox.checked = false; });
      syncReviewSelectionControls(reviewMode, activeItems);
    });
    $$('[data-review-batch-action]').forEach((button) => button.addEventListener("click", () => {
      runBatchReviewAction(reviewMode, button.dataset.reviewBatchAction);
    }));
    syncReviewSelectionControls(reviewMode, activeItems);
  }
  bindReviewActions();
}

async function openJargonNoisePreview() {
  const preview = await api.get("/memories/jargon/cleanup/preview?limit=300");
  const items = asItems(preview);
  const rows = items.map((item) => `
    <tr>
      <td><input type="checkbox" data-noise-id="${attr(item.id)}" ${item.severity === "obvious" ? "checked" : ""}></td>
      <td>${escapeHtml(item.content || "-")}</td>
      <td>${statusChip(item.severity === "obvious" ? "明显噪声" : "需复核", item.severity === "obvious" ? "danger" : "warn")}</td>
      <td>${escapeHtml(item.reason || "-")}</td>
      <td>${formatScore(item.confidence)}</td>
    </tr>
  `);
  openModal(
    "黑话噪声预检",
    `<p class="muted">这里只显示预检结果。确认后会物理删除所选黑话及其检索索引，删除后不可在审核页恢复。</p>${table(["选择", "内容", "判定", "原因", "置信度"], rows, "没有发现明显噪声。")}`,
    `<button class="ghost-button" data-modal-close type="button">取消</button><button class="danger-button" data-apply-noise-cleanup type="button" ${items.length ? "" : "disabled"}>物理删除所选项</button>`,
  );
  $('[data-apply-noise-cleanup]')?.addEventListener("click", async () => {
    const ids = $$('[data-noise-id]:checked').map((node) => node.dataset.noiseId).filter(Boolean);
    if (!ids.length) return toast("请先选择要驳回的候选");
    if (!await confirmAction(`确认物理删除 ${ids.length} 条黑话候选？删除后不可恢复。`)) return;
    await api.post("/memories/jargon/cleanup/apply", { action: "reject", ids });
    clearDataCache("reviews:");
    clearDataCache("memories:");
    clearDataCache("learning:status");
    closeModal();
    toast(`已物理删除 ${ids.length} 条候选`);
    loadReviews();
  });
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
    clearDataCache("reviews:");
    clearDataCache("learning:status");
    loadReviews();
  }));
  $$('[data-reject-review]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.rejectReview) return;
    if (button.dataset.reviewKind === "jargon") {
      if (!await confirmAction(state.reviewTab === "jargon_all" ? "确认下架这条已通过黑话？将进入驳回墓碑（同词不再回流待审），过期由维护任务清理。" : "确认驳回这条黑话？驳回后同词不再回流待审（进入墓碑，过期由维护任务清理）。")) return;
      await api.post(`/memories/jargon/${segment(button.dataset.rejectReview)}/reject`);
    } else {
      await api.post(`/reviews/${segment(button.dataset.rejectReview)}/submit`, { action: "reject" });
    }
    toast("已驳回");
    clearDataCache("reviews:");
    clearDataCache("learning:status");
    loadReviews();
  }));
}

async function loadMemories() {
  const request = beginViewRequest();
  const tabKey = `memories:${state.memoryTab}`;
  if (!hasDataCachePrefix(tabKey)) showLoading("正在读取记忆网络...");
  if (state.memoryTab === "canonical") {
    const target = state.cache.memories.canonical;
    const kindParam = state.cache.memories.canonicalKind ? `&kind=${segment(state.cache.memories.canonicalKind)}` : "";
    const statusParam = state.cache.memories.canonicalStatus ? `&status=${segment(state.cache.memories.canonicalStatus)}` : "";
    const [result, qualityOverview] = await Promise.all([
      cachedFetch(`${tabKey}:${target.offset}:${state.cache.memories.canonicalKind}:${state.cache.memories.canonicalStatus}`, () => api.get(`/memories/canonical?limit=${target.limit}&offset=${target.offset}${kindParam}${statusParam}`), target),
      cachedFetch("memories:quality:overview", () => api.get("/memories/quality/overview"), state.cache.memories.qualityOverview),
    ]);
    state.cache.memories.canonical = { items: asItems(result), total: Number(result.total ?? asItems(result).length), limit: target.limit, offset: target.offset };
    state.cache.memories.qualityOverview = qualityOverview || { counts: {}, index: {} };
  } else if (state.memoryTab === "jargon") {
    const target = state.cache.memories.jargon;
    const result = await cachedFetch(`${tabKey}:${target.offset}`, () => api.get(`/memories/jargon?status=active&limit=${target.limit}&offset=${target.offset}`), target);
    state.cache.memories.jargon = { items: asItems(result), total: Number(result.total ?? asItems(result).length), limit: target.limit, offset: target.offset };
  } else if (state.memoryTab === "events") {
    state.cache.memories.events = asItems(await cachedFetch(tabKey, () => api.get("/memories/events"), { items: [] }));
  } else if (state.memoryTab === "reflections") {
    state.cache.memories.reflections = asItems(await cachedFetch(`${tabKey}:${state.cache.memories.month}`, () => api.get(`/memories/reflections?month=${segment(state.cache.memories.month)}`), { items: [] }));
  } else {
    state.cache.memories.nodes = asItems(await cachedFetch(tabKey, () => api.get("/memories/nodes"), { items: [] }));
  }
  if (!isCurrentView(request)) return;
  const tabItems = {
    canonical: state.cache.memories.canonical.items,
    events: state.cache.memories.events,
    reflections: state.cache.memories.reflections,
    nodes: state.cache.memories.nodes,
    jargon: state.cache.memories.jargon.items,
  }[state.memoryTab] || [];
  const rows = tabItems.map((item, index) => {
    const id = item.id || item.date || item.term || "-";
    const contentText = item.content || item.summary || item.narrative || item.reflection || item.meaning || "-";
    const actions = state.memoryTab === "jargon"
      ? `<button class="ghost-button" data-memory-jargon-edit="${attr(id)}" type="button">编辑</button><button class="danger-button" data-memory-delete="${attr(id)}" type="button">删除</button>`
      : state.memoryTab === "canonical"
        ? `<button class="ghost-button" data-memory-canonical-edit="${attr(id)}" type="button">修正</button>${item.status !== "active" ? `<button class="ghost-button" data-memory-restore="${attr(id)}" type="button">恢复启用</button>` : `<button class="ghost-button" data-memory-stale="${attr(id)}" type="button">标记过期</button>`}<button class="danger-button" data-memory-delete="${attr(id)}" type="button">删除</button>`
        : `<button class="danger-button" data-memory-delete="${attr(id)}" type="button">删除</button>`;
    return `
      <tr>
        <td>${escapeHtml(id)}</td>
        <td>${escapeHtml(item.kind || item.status || item.date || "-")}</td>
        <td><div class="content-preview">${escapeHtml(previewText(contentText, 220))}</div></td>
        <td>${escapeHtml(item.session_id || item.group_id || "-")}</td>
        <td>${escapeHtml(item.confidence ?? item.importance ?? "-")}</td>
        <td class="row-actions"><button class="ghost-button" data-memory-detail="${index}" type="button">详情</button>${actions}</td>
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
  const statusOptions = [
    ["active", "启用中"],
    ["review_pending", "待审核隔离"],
    ["rejected", "已驳回"],
    ["stale", "已过期"],
    ["deleted", "已删除"],
    ["merged", "已合并"],
    ["", "全部状态"],
  ].map(([value, label]) => `<option value="${attr(value)}" ${state.cache.memories.canonicalStatus === value ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
  const canonicalPager = state.memoryTab === "canonical" ? `
    <div class="row-actions">
      <label class="inline-label">状态 <select data-memory-status>${statusOptions}</select></label>
      <label class="inline-label">类型 <select data-memory-kind>${kindOptions}</select></label>
      <button class="ghost-button" data-memory-page="${state.cache.memories.canonical.offset - state.cache.memories.canonical.limit}" type="button" ${state.cache.memories.canonical.offset <= 0 ? "disabled" : ""}>上一页</button>
      <span>第 ${Math.floor(state.cache.memories.canonical.offset / state.cache.memories.canonical.limit) + 1} / ${Math.max(1, Math.ceil(state.cache.memories.canonical.total / state.cache.memories.canonical.limit))} 页，共 ${state.cache.memories.canonical.total} 条</span>
      <button class="ghost-button" data-memory-page="${state.cache.memories.canonical.offset + state.cache.memories.canonical.limit}" type="button" ${state.cache.memories.canonical.offset + state.cache.memories.canonical.limit >= state.cache.memories.canonical.total ? "disabled" : ""}>下一页</button>
    </div>
  ` : state.memoryTab === "jargon" ? renderOffsetPager("memory-jargon", state.cache.memories.jargon) : "";
  const totals = `
    <div class="grid">
      ${metric("Canonical v2", state.cache.memories.canonical.total)}
      ${metric("黑话", state.cache.memories.jargon.total)}
      ${metric("旧事件", state.cache.memories.events.length || "未加载", "兼容诊断源")}
      ${metric("旧实体", state.cache.memories.nodes.length || "未加载", "旧实体图谱")}
    </div>
  `;
  const emptyText = state.memoryTab === "canonical"
    ? "Canonical v2 当前筛选无数据。可切换类型为“全部类型”查看完整长期记忆。"
    : "当前旧分类暂无数据；长期记忆主体请查看“Canonical 总览”。";
  const quality = state.cache.memories.qualityOverview || { counts: {}, index: {} };
  const qualityCounts = quality.counts || {};
  const indexInfo = quality.index || {};
  const indexIssueCount = ["missing_projection_ids", "orphan_projection_ids", "inactive_projection_ids", "duplicate_projection_ids"]
    .reduce((total, key) => total + (Array.isArray(indexInfo[key]) ? indexInfo[key].length : 0), 0);
  const audit = state.cache.memories.qualityAudit;
  const auditReasons = audit?.reasons || {};
  const qualityPanel = state.memoryTab === "canonical" ? section(
    "长期记忆质量控制",
    "先执行只读审计确认影响范围，再将疑似污染项移入待审核隔离区；任何操作都不会物理删除长期记忆。",
    `<div class="grid">
      ${metric("启用中", qualityCounts.active || 0)}
      ${metric("待审核隔离", qualityCounts.review_pending || 0)}
      ${metric("已驳回", qualityCounts.rejected || 0)}
      ${metric("索引异常", indexIssueCount, "缺失、孤立、失效或重复投影")}
    </div>
    ${audit ? `<div class="notice ${Number(audit.suspect_count || 0) ? "warning" : "ok"}">最近审计：扫描 ${Number(audit.scanned || 0)} 条，发现 ${Number(audit.suspect_count || 0)} 条疑似污染。${Object.entries(auditReasons).map(([reason, count]) => `${escapeHtml(reason)} ${Number(count)}`).join(" · ")}</div>` : ""}`,
    `<button class="ghost-button" data-memory-quality-audit type="button">只读质量审计</button><button class="danger-button" data-memory-quality-quarantine type="button">隔离审计命中项</button><button class="ghost-button" data-memory-index-rebuild type="button">重建召回索引</button><button class="ghost-button" data-memory-maintenance-run type="button">执行维护</button>`,
  ) : "";
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
    ${qualityPanel}
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
  $('[data-memory-status]')?.addEventListener("change", (event) => {
    state.cache.memories.canonicalStatus = event.target.value;
    state.cache.memories.canonical.offset = 0;
    loadMemories();
  });
  $$('[data-memory-page]').forEach((button) => button.addEventListener("click", () => {
    state.cache.memories.canonical.offset = Math.max(0, Number(button.dataset.memoryPage || 0));
    loadMemories();
  }));
  $$('[data-memory-jargon-page]').forEach((button) => button.addEventListener("click", () => {
    state.cache.memories.jargon.offset = Math.max(0, Number(button.dataset.memoryJargonPage || 0));
    loadMemories();
  }));
  $$('[data-memory-detail]').forEach((button) => button.addEventListener("click", () => {
    openModal("记忆详情", `<pre>${escapeHtml(json(tabItems[Number(button.dataset.memoryDetail)] || {}))}</pre>`);
  }));
  $$('[data-memory-jargon-edit]').forEach((button) => button.addEventListener("click", () => {
    const item = findMemoryItem("jargon", button.dataset.memoryJargonEdit);
    if (!item) return toast("未找到黑话记录");
    openJargonCalibration(item, "save");
  }));
  $$('[data-memory-canonical-edit]').forEach((button) => button.addEventListener("click", () => {
    const item = findMemoryItem("canonical", button.dataset.memoryCanonicalEdit);
    if (!item) return toast("未找到长期记忆");
    openCanonicalMemoryEditor(item);
  }));
  $$('[data-memory-restore]').forEach((button) => button.addEventListener("click", async () => {
    await api.post(`/memories/canonical/${segment(button.dataset.memoryRestore)}/restore`);
    toast("记忆已恢复启用");
    clearDataCache("memories:");
    loadMemories();
  }));
  $$('[data-memory-stale]').forEach((button) => button.addEventListener("click", async () => {
    await api.post(`/memories/canonical/${segment(button.dataset.memoryStale)}/stale`);
    toast("记忆已标记过期");
    clearDataCache("memories:");
    loadMemories();
  }));
  $('[data-memory-quality-audit]')?.addEventListener("click", async () => {
    const result = await api.post("/memories/quality/audit", { limit: 5000 });
    state.cache.memories.qualityAudit = result || {};
    toast(`质量审计完成：发现 ${Number(state.cache.memories.qualityAudit.suspect_count || 0)} 条`);
    clearDataCache("memories:canonical");
    loadMemories();
  });
  $('[data-memory-quality-quarantine]')?.addEventListener("click", async () => {
    if (!await confirmAction("将本次质量审计命中的活动记忆移入待审核隔离区？不会物理删除。", "danger")) return;
    const result = await api.post("/memories/quality/quarantine", { limit: 5000 });
    const report = result || {};
    state.cache.memories.qualityAudit = report;
    toast(`已隔离 ${Number(report.changed || 0)} 条，索引清理 ${Number(report.projection_deleted || 0)} 条`);
    clearDataCache("memories:");
    loadMemories();
  });
  $('[data-memory-index-rebuild]')?.addEventListener("click", async () => {
    if (!await confirmAction("重建全部长期记忆召回索引？期间聊天会自动降级到 Canonical 检索。")) return;
    const result = await api.post("/memories/index/rebuild", {});
    toast(`索引重建完成：${Number(result.rebuilt || 0)} 条`);
    clearDataCache("memories:");
    loadMemories();
  });
  // OPT-05/WU-04: 维护端点此前后端已注册但前端从不调用，治理自愈通道断裂
  $('[data-memory-maintenance-run]')?.addEventListener("click", async () => {
    if (!await confirmAction("立即执行一次记忆维护（索引一致性修复 + 积压体检）？物理清理受配置开关控制。")) return;
    // round11 契约：api 层已统一单层解包，禁止再 .data 二次解包
    const report = (await api.post("/memories/maintenance/run", {})) || {};
    toast(`维护完成：修复投影 ${Number(report.projection_deleted || 0)}，物理清理 ${Number(report.physically_deleted || 0)}，标记过期 ${Number(report.marked_stale || 0)}`);
    clearDataCache("memories:");
    loadMemories();
  });
  $$('[data-memory-delete]').forEach((button) => button.addEventListener("click", async () => {
    const id = button.dataset.memoryDelete;
    if (!id || !await confirmAction("删除这条记忆记录？", "danger")) return;
    let deleteResult = null;
    if (state.memoryTab === "canonical") deleteResult = await api.post(`/memories/canonical/${segment(id)}/delete`);
    if (state.memoryTab === "events") deleteResult = await api.post(`/memories/events/${segment(id)}/delete`);
    if (state.memoryTab === "reflections") deleteResult = await api.post(`/memories/reflections/${segment(id)}/delete`);
    if (state.memoryTab === "nodes") deleteResult = await api.post(`/memories/nodes/${segment(id)}/delete`);
    if (state.memoryTab === "jargon") deleteResult = await api.post(`/memories/jargon/${segment(id)}/delete`);
    // OPT-16/WU-12: legacy 只读记录删除返回 readonly/changed=false，此前一律
    // toast "已删除"而行刷新后仍在——按钮看似坏了
    if (deleteResult && (deleteResult.status === "readonly" || deleteResult.changed === false)) {
      toast("该记录为只读历史数据，无法删除（未做任何更改）");
    } else {
      toast("记忆记录已删除");
    }
    clearDataCache("memories:");
    loadMemories();
  }));
}

async function loadUsers() {
  const request = beginViewRequest();
  if (!hasDataCachePrefix("users:list")) showLoading("正在读取用户画像...");
  const users = await cachedFetch("users:list", () => api.get("/users"), { items: [] });
  if (!isCurrentView(request)) return;
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
    clearDataCache("users:");
    loadUsers();
  });
  $('[data-delete-user]')?.addEventListener("click", async () => {
    if (!await confirmAction("删除这个用户画像？该操作不可恢复。", "danger")) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/delete`);
    state.activeUserId = "";
    toast("用户画像已删除");
    clearDataCache("users:");
    loadUsers();
  });
  $$('[data-add-slice]').forEach((button) => button.addEventListener("click", async () => {
    const field = button.dataset.addSlice;
    const input = $(`[data-slice-input="${field}"]`);
    if (!input?.value?.trim()) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/slices`, { type: field, content: input.value.trim() });
    toast("切片已添加");
    clearDataCache("users:");
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
    clearDataCache("users:");
    loadUsers();
  }));
}

async function loadPersonaSlices() {
  const request = beginViewRequest();
  if (!hasDataCachePrefix("persona:slices")) showLoading("正在读取角色切片诊断...");
  state.cache.personaSlicesError = null;
  try {
    const result = await cachedFetch("persona:slices", () => api.get("/persona/slices"), {});
    if (!isCurrentView(request)) return;
    state.cache.personaSlices = result;
    schedulePersonaRegenerationPoll(result.regeneration);
  } catch (error) {
    state.cache.personaSlices = {};
    state.cache.personaSlicesError = error.message || String(error);
    toast("角色切片读取失败");
  }
  if (!isCurrentView(request)) return;
  renderPersonaSlices();
}

function stopPersonaRegenerationPolling() {
  if (state.personaRegenerationPollTimer) {
    clearTimeout(state.personaRegenerationPollTimer);
    state.personaRegenerationPollTimer = null;
  }
}

function schedulePersonaRegenerationPoll(regeneration = {}) {
  stopPersonaRegenerationPolling();
  if (!["queued", "running"].includes(String(regeneration?.state || ""))) return;
  state.personaRegenerationPollTimer = setTimeout(async () => {
    state.personaRegenerationPollTimer = null;
    if (state.current !== "personaSlices") return;
    try {
      const previousState = String(state.cache.personaSlices?.regeneration?.state || "");
      const status = await api.get("/persona/slices/regeneration-status");
      if (state.current !== "personaSlices") return;
      state.cache.personaSlices = {
        ...(state.cache.personaSlices || {}),
        regeneration: status,
      };
      const nextState = String(status?.state || "");
      if (["completed", "failed", "cancelled"].includes(nextState)) {
        clearDataCache("persona:");
        await loadPersonaSlices();
        if (nextState === "completed" && previousState !== "completed") {
          toast("人格核心与 8 维切片已重新生成");
        } else if (nextState === "failed" && previousState !== "failed") {
          toast(`人格重建失败：${status.error || "旧人格已保留"}`);
        }
        return;
      }
      renderPersonaSlices();
      schedulePersonaRegenerationPoll(status);
    } catch (error) {
      toast(`人格重建状态读取失败：${error.message || error}`);
      schedulePersonaRegenerationPoll(state.cache.personaSlices?.regeneration || {});
    }
  }, 2500);
}

async function startPersonaRegeneration() {
  const persona = state.cache.personaSlices || {};
  const regeneration = persona.regeneration || {};
  if (["queued", "running"].includes(String(regeneration.state || ""))) {
    toast("人格重建已经在进行中");
    return;
  }
  const confirmed = await confirmAction(
    "确认重新生成人格核心内容与 8 维切片？生成期间继续使用当前人格；全部成功后才会替换，并清除当前人工微调。失败时旧人格会完整保留。",
  );
  if (!confirmed) return;
  const result = await api.post("/persona/slices/regenerate", {
    cache_key: persona.cache_key || "",
    expected_timestamp: persona.timestamp || 0,
    clear_manual_overrides: true,
    idempotency_key: window.crypto?.randomUUID?.() || `persona-${Date.now()}`,
  });
  state.cache.personaSlices = {
    ...persona,
    regeneration: result,
  };
  renderPersonaSlices();
  schedulePersonaRegenerationPoll(result);
  toast("人格重建已开始，当前人格会继续正常服务");
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
  clearDataCache("persona:");
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
  clearDataCache("persona:");
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
  const regeneration = persona.regeneration || {};
  const regenerationRunning = ["queued", "running"].includes(String(regeneration.state || ""));
  const regenerationProgress = Number(regeneration.total_components || 11) > 0
    ? Math.round((Number(regeneration.completed_components || 0) / Number(regeneration.total_components || 11)) * 100)
    : 0;
  content().innerHTML = `
    ${pageHeader("角色理解与微调 Persona Slices", "查看并微调 AstrMai 从 AstrBot 人格中提炼出的核心内容与 8 类角色切片。", `<button class="primary-button" data-regenerate-persona type="button" ${regenerationRunning ? "disabled" : ""}>${regenerationRunning ? "正在重新生成..." : "重新生成人格8维度切片"}</button><button class="ghost-button" data-persona-slices-json type="button">诊断 JSON</button>${Object.keys(persona.manual_overrides || {}).length ? `<button class="ghost-button" data-restore-persona-all type="button">恢复全部 AI 版本</button>` : ""}`)}
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
    ${String(regeneration.state || "idle") !== "idle" ? section(
      "人格重建状态",
      regenerationRunning
        ? "后台正在生成；聊天仍使用当前已生效人格。"
        : (regeneration.state === "completed" ? "新版本已原子替换并生效。" : "重建未生效，旧人格保持不变。"),
      `
        <div class="grid">
          ${metric("状态", regeneration.state || "idle", regeneration.stage || "-")}
          ${metric("进度", `${regeneration.completed_components || 0}/${regeneration.total_components || 11}`, `${regenerationProgress}%`)}
          ${metric("派生版本", regeneration.derivation_version || persona.derivation_version || "-", "人格压缩算法版本")}
          ${metric("结束时间", formatTime(regeneration.finished_at), regeneration.error || "无错误")}
        </div>
      `,
    ) : ""}
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
  $('[data-regenerate-persona]')?.addEventListener("click", () => startPersonaRegeneration().catch((error) => toast(`人格重建启动失败：${error.message || error}`)));
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
  if (state.current !== "personaSlices") {
    stopPersonaRegenerationPolling();
  }
  try {
    await (loaders[state.current] || loadDashboard)();
  } catch (error) {
    content().innerHTML = `<section class="empty-state"><h2>加载失败</h2><p>${escapeHtml(error.message || error)}</p></section>`;
  }
}

async function init() {
  setBridgeStatus("Bridge 初始化中", "muted");
  const buildNode = $("#build-version");
  if (buildNode) buildNode.textContent = `Build ${ADMIN_BUILD_VERSION}`;
  renderTabs();
  $("#refresh-button").addEventListener("click", () => {
    if (state.current === "dashboard") {
      clearDashboardCache(state.dashboardTab);
      if (state.dashboardTab === "tools") clearDataCache("tools:");
    }
    clearDataCache(`${state.current}:`);
    if (state.current === "learning") clearDataCache("learning:");
    if (state.current === "reviews") clearDataCache("reviews:");
    if (state.current === "memories") clearDataCache("memories:");
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
