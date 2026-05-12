from __future__ import annotations

import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
LOG_PATH = Path(__file__).resolve().parent / "mock_frontend_server.log"
PORT_PATH = Path(__file__).resolve().parent / "mock_frontend_server.port"
DEFAULT_PORT = int(os.environ.get("ASTRMAI_MOCK_FRONTEND_PORT", "8787"))


NOW = int(time.time())
CHATS = ["group:10001", "group:10086", "private:alice"]


def _read_log_tail(max_lines: int = 160) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception as exc:
        return [f"Failed to read log: {type(exc).__name__}: {exc}"]


def _log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
    try:
        print(line, end="")
    except Exception:
        pass


def _mock_debug_overlay() -> str:
    return r'''
<style id="astrmai-mock-debug-style">
  #astrmai-mock-debug {
    position: fixed;
    right: 16px;
    bottom: 16px;
    width: min(520px, calc(100vw - 32px));
    max-height: min(520px, calc(100vh - 32px));
    z-index: 99999;
    color: #dbeafe;
    background: rgba(15, 23, 42, 0.94);
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 8px;
    box-shadow: 0 22px 60px rgba(0, 0, 0, 0.45);
    overflow: hidden;
    font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  }
  #astrmai-mock-debug header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.18);
    background: rgba(30, 41, 59, 0.82);
  }
  #astrmai-mock-debug strong { color: #f8fafc; font-size: 12px; }
  #astrmai-mock-debug .mock-pill {
    padding: 2px 6px;
    border-radius: 999px;
    background: rgba(34, 197, 94, 0.16);
    color: #bbf7d0;
    border: 1px solid rgba(34, 197, 94, 0.24);
  }
  #astrmai-mock-debug button {
    border: 1px solid rgba(148, 163, 184, 0.3);
    border-radius: 6px;
    padding: 3px 7px;
    background: rgba(15, 23, 42, 0.75);
    color: #cbd5e1;
    cursor: pointer;
  }
  #astrmai-mock-debug button:hover { color: #fff; border-color: rgba(226, 232, 240, 0.55); }
  #astrmai-mock-debug pre {
    margin: 0;
    padding: 10px;
    max-height: 410px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }
  #astrmai-mock-debug[data-collapsed="true"] pre { display: none; }
  #astrmai-mock-debug[data-collapsed="true"] { width: auto; }
</style>
<script id="astrmai-mock-debug-script">
(function () {
  if (window.__astrmaiMockDebugInstalled) return;
  window.__astrmaiMockDebugInstalled = true;

  const originalFetch = window.fetch.bind(window);
  const state = { collapsed: false };

  function safeText(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }

  function clientLog(level, message) {
    const text = safeText(message).slice(0, 900);
    if (!text) return;
    originalFetch("/api/mock/client-log", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        level,
        message: text,
        hash: location.hash || "",
        pathname: location.pathname || "",
        ts: Date.now()
      })
    }).catch(function () {});
  }

  function ensurePanel() {
    let panel = document.getElementById("astrmai-mock-debug");
    if (panel) return panel;
    panel = document.createElement("aside");
    panel.id = "astrmai-mock-debug";
    panel.dataset.collapsed = "false";
    panel.innerHTML = [
      "<header>",
      "<strong>AstrMai Mock Logs</strong>",
      "<span class=\"mock-pill\">password: astrmai_admin</span>",
      "<span style=\"flex:1\"></span>",
      "<button type=\"button\" data-action=\"refresh\">刷新</button>",
      "<button type=\"button\" data-action=\"clear\">清空</button>",
      "<button type=\"button\" data-action=\"toggle\">收起</button>",
      "</header>",
      "<pre>loading...</pre>"
    ].join("");
    document.body.appendChild(panel);
    panel.addEventListener("click", function (event) {
      const action = event.target && event.target.getAttribute("data-action");
      if (action === "refresh") pollLogs();
      if (action === "clear") {
        originalFetch("/api/mock/logs/clear", { method: "POST" })
          .then(function () { pollLogs(); })
          .catch(function () {});
      }
      if (action === "toggle") {
        state.collapsed = !state.collapsed;
        panel.dataset.collapsed = state.collapsed ? "true" : "false";
        event.target.textContent = state.collapsed ? "展开" : "收起";
      }
    });
    return panel;
  }

  async function pollLogs() {
    try {
      const panel = ensurePanel();
      const pre = panel.querySelector("pre");
      const response = await originalFetch("/api/mock/logs?tail=140", { cache: "no-store" });
      const data = await response.json();
      pre.textContent = (data.lines || []).join("\n") || "No logs yet.";
      pre.scrollTop = pre.scrollHeight;
    } catch (err) {
      const panel = ensurePanel();
      const pre = panel.querySelector("pre");
      pre.textContent = "Failed to load mock logs: " + safeText(err);
    }
  }

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || "GET";
    const started = performance.now();
    try {
      const response = await originalFetch(input, init);
      if (!url.includes("/api/mock/")) {
        clientLog("api", method + " " + url + " -> " + response.status + " (" + Math.round(performance.now() - started) + "ms)");
        setTimeout(pollLogs, 120);
      }
      return response;
    } catch (err) {
      if (!url.includes("/api/mock/")) {
        clientLog("api-error", method + " " + url + " -> " + safeText(err));
        setTimeout(pollLogs, 120);
      }
      throw err;
    }
  };

  document.addEventListener("click", function (event) {
    const el = event.target && event.target.closest && event.target.closest("button,a,input,textarea,select,[role='button']");
    if (!el || el.closest("#astrmai-mock-debug")) return;
    const label = (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || el.tagName || "").trim();
    clientLog("click", (label || el.tagName).slice(0, 160));
    setTimeout(pollLogs, 120);
  }, true);

  window.addEventListener("error", function (event) {
    clientLog("window-error", event.message + " @ " + event.filename + ":" + event.lineno);
    setTimeout(pollLogs, 120);
  });

  window.addEventListener("unhandledrejection", function (event) {
    clientLog("unhandled-rejection", safeText(event.reason));
    setTimeout(pollLogs, 120);
  });

  const oldError = console.error;
  console.error = function () {
    clientLog("console.error", Array.prototype.map.call(arguments, safeText).join(" "));
    oldError.apply(console, arguments);
    setTimeout(pollLogs, 120);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      ensurePanel();
      pollLogs();
      setInterval(pollLogs, 2000);
    });
  } else {
    ensurePanel();
    pollLogs();
    setInterval(pollLogs, 2000);
  }
})();
</script>
'''


def _load_schema() -> dict:
    schema_path = ROOT / "_conf_schema.json"
    if not schema_path.exists():
        return {}
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _default_from_field(field: dict):
    if "default" in field:
        return field["default"]
    field_type = field.get("type")
    if field_type in {"list", "template_list"}:
        return []
    if field_type in {"object", "dict"}:
        return {}
    if field_type == "bool":
        return False
    if field_type == "int":
        return 0
    if field_type == "float":
        return 0.0
    return ""


def _default_config(schema: dict) -> dict:
    config: dict[str, dict] = {}
    for section, section_def in schema.items():
        fields = section_def.get("items") or section_def.get("keys") or {}
        config[section] = {key: _default_from_field(field) for key, field in fields.items()}
    config.setdefault("global_settings", {}).setdefault("debug_mode", True)
    config.setdefault("provider", {}).setdefault("fallback_models", ["gemini-mock", "deepseek-mock"])
    config.setdefault("persona", {}).setdefault("bot_name", "亚托莉")
    return config


SCHEMA = _load_schema()
CONFIG = _default_config(SCHEMA)


def _decision_items(chat_id: str | None = None) -> list[dict]:
    rows = [
        {
            "id": "dec-001",
            "chat_id": "group:10001",
            "created_at": NOW - 70,
            "action": "reply",
            "reply_need": "reply",
            "social_intent": "join",
            "action_tier": "chat",
            "stance": "warm",
            "memory_policy": "light",
            "attack_confidence": 0.02,
            "risk_flags": [],
            "reason": "群聊正在围绕 bot 近期表现闲聊，适合轻松接话。",
        },
        {
            "id": "dec-002",
            "chat_id": "group:10086",
            "created_at": NOW - 260,
            "action": "reply",
            "reply_need": "reply",
            "social_intent": "inquire",
            "action_tier": "full",
            "stance": "neutral",
            "memory_policy": "deep",
            "attack_confidence": 0.0,
            "risk_flags": [],
            "reason": "用户显式询问“你还记得吗”，允许记忆查询。",
        },
        {
            "id": "dec-003",
            "chat_id": "private:alice",
            "created_at": NOW - 520,
            "action": "wait",
            "reply_need": "wait",
            "social_intent": "observe",
            "action_tier": "none",
            "stance": "guarded",
            "memory_policy": "none",
            "attack_confidence": 0.0,
            "risk_flags": ["low_context"],
            "reason": "信息不足，先观察下一条消息。",
        },
    ]
    if chat_id:
        rows = [row for row in rows if row["chat_id"] == chat_id]
    return rows


def _tool_items(chat_id: str | None = None) -> list[dict]:
    rows = [
        {
            "id": "tool-001",
            "chat_id": "group:10001",
            "created_at": NOW - 60,
            "tool_name": "message_reaction_action",
            "tool_tier": "chat",
            "status": "success",
            "arguments": {"reaction": "like", "target": "msg-8848"},
            "result_preview": "已轻量互动：点赞",
            "duration_ms": 132,
        },
        {
            "id": "tool-002",
            "chat_id": "group:10086",
            "created_at": NOW - 240,
            "tool_name": "self_lore_query",
            "tool_tier": "full",
            "status": "success",
            "arguments": {"query": "用户问到的自我设定"},
            "result_preview": "命中 persona_cache.first_person_rewrite",
            "duration_ms": 418,
        },
        {
            "id": "tool-003",
            "chat_id": "private:alice",
            "created_at": NOW - 510,
            "tool_name": "wait_and_listen",
            "tool_tier": "full",
            "status": "skipped",
            "arguments": {"reason": "low_context"},
            "result_preview": "等待下一条消息",
            "duration_ms": 18,
        },
    ]
    if chat_id:
        rows = [row for row in rows if row["chat_id"] == chat_id]
    return rows


def _user_items() -> list[dict]:
    return [
        {
            "user_id": "10001",
            "nickname": "小明",
            "nickname_reason": "常在群里主动测试功能",
            "social_score": 0.82,
            "identity": "活跃测试用户",
            "tags": ["测试", "群聊核心", "喜欢吐槽"],
            "persona_analysis": "偏直接，喜欢快速看到结果。",
            "memory_points": ["上次关注过 WebUI 美化", "希望 bot 回复更自然"],
            "identity_points": ["群管理员"],
            "preference_points": ["喜欢简洁 dashboard", "不喜欢花哨动画"],
            "relationship_points": ["与 bot 熟悉，能接受轻微玩笑"],
            "speech_style_points": ["短句多，常用“看看/试试”"],
        },
        {
            "user_id": "alice",
            "nickname": "Alice",
            "nickname_reason": "私聊中自报英文名",
            "social_score": 0.64,
            "identity": "私聊用户",
            "tags": ["私聊", "记忆观察"],
            "persona_analysis": "表达比较细腻，常给较长上下文。",
            "memory_points": ["提过喜欢夜间写计划"],
            "identity_points": ["开发协作者"],
            "preference_points": ["喜欢解释清楚取舍"],
            "relationship_points": ["对 bot 信任度中等偏高"],
            "speech_style_points": ["完整句较多"],
        },
    ]


def _memory_events() -> list[dict]:
    return [
        {
            "id": "mem-001",
            "timestamp": NOW - 3600,
            "narrative": "用户要求 WebUI 支持配置面板同步修改到底层 config。",
            "memory_kind": "Project",
            "importance": 0.86,
            "tags": "webui,config,admin",
        },
        {
            "id": "mem-002",
            "timestamp": NOW - 7600,
            "narrative": "连续 poke 会触发旧历史污染，已通过 lightweight event 和 transcript 时间窗治理。",
            "memory_kind": "Bugfix",
            "importance": 0.74,
            "tags": "attention,poke,memory",
        },
    ]


def _reflections(month: str) -> list[dict]:
    today = time.strftime("%Y-%m-%d")
    return [
        {
            "date": today,
            "summary": "今天重点围绕 WebUI 前后端接口闭环、前端美化边界和 Mock 预览环境做收口。",
            "raw_log": "dashboard/settings/reviews/memory pages checked",
            "meta": "{}",
        }
    ]


def _canonical_memories() -> list[dict]:
    return [
        {
            "id": "mem-can-001",
            "session_id": "chat:demo",
            "persona_id": "atri",
            "source": "summary",
            "kind": "memory",
            "content": "User prefers deterministic Memory v2 behavior with SQL canonical storage.",
            "summary": "SQL canonical memory is the only authority; vector/BM25 are projections.",
            "tags": ["memory-v2", "canonical"],
            "importance": 0.92,
            "confidence": 0.9,
            "status": "active",
            "visibility": "auto_and_tool",
            "updated_at": NOW - 1800,
            "last_access_time": NOW - 1200,
            "metadata": {"source_ref": "mock:summary"},
        },
        {
            "id": "mem-can-002",
            "session_id": "__self_lore__",
            "persona_id": "atri",
            "source": "persona_cache",
            "kind": "persona_lore",
            "content": "I speak in a concise, warm, practical style.",
            "summary": "Concise warm practical style.",
            "tags": ["persona"],
            "importance": 1.0,
            "confidence": 0.8,
            "status": "stale",
            "visibility": "auto_and_tool",
            "updated_at": NOW - 86400,
            "last_access_time": NOW - 86400,
            "metadata": {"source_ref": "persona_cache:atri"},
        },
    ]


def _memory_nodes() -> list[dict]:
    return [
        {"id": "node-webui", "name": "WebUI 管理台", "type": "project", "description": "AstrMai 后台管理入口。"},
        {"id": "node-agency", "name": "主观能动性", "type": "concept", "description": "CognitiveLoop + AgencyReflection + Heartflow。"},
    ]


def _jargons() -> list[dict]:
    return [
        {"id": "jargon-001", "content": "轻量链路", "meaning": "CORE_ONLY/chat tier 等低延迟路径", "is_jargon": 1, "is_complete": 1, "group_id": "GLOBAL"},
        {"id": "jargon-002", "content": "止血", "meaning": "先修复最影响输出质量的问题", "is_jargon": 1, "is_complete": 1, "group_id": "GLOBAL"},
    ]


MOCK_MEMORY_EVENTS = _memory_events()
MOCK_CANONICAL_MEMORIES = _canonical_memories()
MOCK_REVIEW_ITEMS = [
    {"id": "rv-001", "situation": "用户夸 bot 可爱", "expression": "嘿嘿，那我今天也稍微得意一下。", "style": "warm", "weight": 1.1, "group_id": "group:10001", "status": "pending"},
    {"id": "rv-002", "situation": "强攻击边界", "expression": "这话有点过了，我不接这个。", "style": "boundary", "weight": 0.8, "group_id": "GLOBAL", "status": "pending"},
    {"id": "rv-101", "situation": "普通接话", "expression": "我懂你的意思，先接眼前这句。", "style": "natural", "weight": 1.0, "group_id": "GLOBAL", "status": "approved"},
    {"id": "rv-102", "situation": "重复口头禅", "expression": "咻——", "style": "catchphrase", "weight": 0.2, "group_id": "group:10001", "status": "rejected"},
]


def _review_status_for_action(action: str) -> str:
    if action == "approve":
        return "approved"
    if action == "reject":
        return "rejected"
    return action or "pending"


def _copy_rows(rows: list[dict]) -> list[dict]:
    return [dict(row) for row in rows]


class MockFrontendHandler(BaseHTTPRequestHandler):
    server_version = "AstrMaiMockFrontend/1.0"

    def log_message(self, fmt: str, *args) -> None:
        _log(f"{self.client_address[0]} {fmt % args}")

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _serve_static_body(self, path: Path) -> tuple[bytes, str]:
        if path.suffix == ".html":
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.name == "index.html":
                marker = "</body>"
                overlay = _mock_debug_overlay()
                if marker in text:
                    text = text.replace(marker, overlay + "\n" + marker, 1)
                else:
                    text += overlay
            return text.encode("utf-8"), "text/html; charset=utf-8"
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in {".js", ".css"}:
            content_type += "; charset=utf-8"
        return path.read_bytes(), content_type

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api"):
            self._route_api("GET", parsed.path[4:] or "/", parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api"):
            self._route_api("POST", parsed.path[4:] or "/", parse_qs(parsed.query), self._read_json())
            return
        self._send_text("Not Found", 404)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api"):
            self._route_api("PATCH", parsed.path[4:] or "/", parse_qs(parsed.query), self._read_json())
            return
        self._send_text("Not Found", 404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api"):
            self._route_api("PUT", parsed.path[4:] or "/", parse_qs(parsed.query), self._read_json())
            return
        self._send_text("Not Found", 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api"):
            self._route_api("DELETE", parsed.path[4:] or "/", parse_qs(parsed.query))
            return
        self._send_text("Not Found", 404)

    def _serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/") or "index.html"
        path = (FRONTEND_DIR / relative).resolve()
        if not str(path).startswith(str(FRONTEND_DIR.resolve())) or not path.exists() or path.is_dir():
            path = FRONTEND_DIR / "index.html"
        body, content_type = self._serve_static_body(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route_api(self, method: str, path: str, query: dict, body: dict | None = None) -> None:
        if path not in {"/mock/logs", "/mock/client-log"}:
            _log(f"{method} /api{path} query={query}")
        if path == "/mock/logs" and method == "GET":
            try:
                tail = int((query.get("tail") or ["160"])[0])
            except Exception:
                tail = 160
            self._send_json({"lines": _read_log_tail(max(20, min(tail, 500)))})
            return
        if path == "/mock/logs/clear" and method == "POST":
            LOG_PATH.write_text("", encoding="utf-8")
            _log("CLIENT clear mock log panel")
            self._send_json({"ok": True})
            return
        if path == "/mock/client-log" and method == "POST":
            payload = body or {}
            level = str(payload.get("level") or "client")[:40]
            message = str(payload.get("message") or "")[:900].replace("\r", " ").replace("\n", " ")
            page = str(payload.get("hash") or payload.get("pathname") or "")[:120]
            _log(f"CLIENT {level} {page} {message}".strip())
            self._send_json({"ok": True})
            return

        if path == "/auth/login" and method == "POST":
            password = (body or {}).get("password")
            if password and password != "wrong":
                self._send_json({"token": "mock-admin-token", "expires_in": 86400})
            else:
                self._send_json({"detail": "invalid password"}, 401)
            return
        if path == "/auth/verify":
            self._send_json({"ok": True, "user": "mock-admin"})
            return

        if path == "/dashboard":
            self._send_json(
                {
                    "sys_cpu_percent": 18.5,
                    "sys_mem_percent": 46.2,
                    "webui_mem_mb": 142.7,
                    "db_size_kb": 27955.2,
                    "total_users": 128,
                    "pending_reviews": 9,
                    "total_memory_events": 642,
                    "total_canonical_memories": len(MOCK_CANONICAL_MEMORIES),
                    "active_chats": 3,
                    "uptime_seconds": 18642,
                }
            )
            return
        if path == "/runtime/health":
            self._send_json({"status": "healthy", "checks": {"frontend": "mock", "database": "ok", "scheduler": "ok"}})
            return
        if path == "/runtime/models":
            self._send_json({"models": ["gemini-mock", "deepseek-mock", "vision-mock"]})
            return
        if path == "/runtime/capabilities":
            self._send_json(
                {
                    "data": {
                        "CognitiveLoop": {"enabled": True, "timeout_ms": 2500},
                        "Heartflow": {"enabled": True, "active_chats": 3},
                        "DreamAgent": {"enabled": True, "last_run": NOW - 1800},
                        "ToolTier": {"chat": True, "full": True, "sys3": True},
                    }
                }
            )
            return
        if path == "/heartflow/status":
            self._send_json({"data": {"enabled": True, "active_chats": 3, "last_tick_time": NOW - 24, "pending_pulses": 4}})
            return
        if path == "/heartflow/chats":
            self._send_json(
                {
                    "items": [
                        {"chat_id": "group:10001", "interest": 0.78, "engagement": 0.72, "fatigue": 0.18, "talk_willingness": 0.66, "silence_pressure": 0.22, "cooldown_tags": ["meme"]},
                        {"chat_id": "group:10086", "interest": 0.53, "engagement": 0.44, "fatigue": 0.31, "talk_willingness": 0.38, "silence_pressure": 0.74, "cooldown_tags": []},
                        {"chat_id": "private:alice", "interest": 0.64, "engagement": 0.37, "fatigue": 0.49, "talk_willingness": 0.29, "silence_pressure": 0.31, "cooldown_tags": ["sharp_reply"]},
                    ]
                }
            )
            return
        if path.startswith("/heartflow/chats/") and path.endswith("/hidden-context"):
            chat_id = unquote(path.split("/")[3])
            self._send_json({"data": {"chat_id": chat_id, "hidden_context": f"Heartflow: interest=0.72, talk_willingness=0.54, recent_impulse=join, cooldown=meme for {chat_id}."}})
            return
        if path.startswith("/heartflow/chats/") and path.endswith("/cooldowns/clear"):
            self._send_json({"ok": True})
            return
        if path == "/cognition/recent-decisions":
            self._send_json({"items": _decision_items()})
            return
        if path.startswith("/cognition/chats/") and path.endswith("/recent-decisions"):
            chat_id = unquote(path.split("/")[3])
            self._send_json({"items": _decision_items(chat_id)})
            return
        if path == "/tools/status":
            self._send_json({"data": {"enabled": True, "recent_calls": 42, "chat_tier_max_steps": 2, "full_tier_max_steps": 5}})
            return
        if path == "/tools/policy":
            self._send_json(
                {
                    "data": {
                        "chat": ["proactive_meme", "message_reaction_action", "proactive_like_action"],
                        "guarded_chat": ["proactive_poke", "construct_at_event"],
                        "full_only": ["self_lore_query", "omni_perception_query", "wait_and_listen", "regret_and_withdraw_action"],
                        "rules": ["普通聊天优先 chat tier", "撤回/查询/转移话题才进入 full tier", "低能量会裁剪高扰动工具"],
                    }
                }
            )
            return
        if path == "/tools/recent-calls":
            self._send_json({"items": _tool_items()})
            return
        if path.startswith("/tools/chats/") and path.endswith("/recent-calls"):
            chat_id = unquote(path.split("/")[3])
            self._send_json({"items": _tool_items(chat_id)})
            return

        if path == "/proactive/status":
            self._send_json({"data": {"enabled": True, "loop_interval": 60, "last_run": NOW - 38}})
            return
        if path == "/proactive/dream/status":
            self._send_json({"data": {"enabled": True, "last_run": NOW - 1800, "next_eta": 5400}})
            return
        if path == "/proactive/diary/status":
            self._send_json({"data": {"enabled": True, "last_run": NOW - 7200, "entries_today": 1}})
            return
        if path == "/proactive/wakeup/status":
            self._send_json({"data": {"enabled": True, "last_wakeup": NOW - 2400, "cooldown_remaining": 0}})
            return
        if path.startswith("/proactive/") and method == "POST":
            self._send_json({"ok": True, "queued": True})
            return
        if path == "/learning/status":
            self._send_json({"data": {"expression_patterns": 126, "pending_reviews": 9, "cooldowns": 3}})
            return
        if path.startswith("/learning/reflect/run-once"):
            self._send_json({"ok": True, "queued": True})
            return
        if path == "/memory-feedback":
            self._send_json(
                {
                    "items": [
                        {"id": "fb-001", "source": "heartflow", "summary": "最近 meme 使用偏频繁", "guidance": "接下来两轮减少表情包。", "tags": ["meme_cooldown"], "importance": 0.5, "created_at": NOW - 600},
                        {"id": "fb-002", "source": "agency_reflection", "summary": "用户更喜欢短回复", "guidance": "优先短句，避免长解释。", "tags": ["short_reply"], "importance": 0.7, "created_at": NOW - 1600},
                    ]
                }
            )
            return
        if path == "/memory-feedback/sources":
            self._send_json({"items": [{"source": "heartflow", "count": 8}, {"source": "dream", "count": 4}, {"source": "agency_reflection", "count": 12}]})
            return
        if path.startswith("/memory-feedback/"):
            self._send_json({"ok": True})
            return
        if path == "/chats/active":
            self._send_json({"items": CHATS})
            return
        if path.startswith("/chats/") and path.endswith("/runtime"):
            chat_id = unquote(path.split("/")[2])
            self._send_json({"data": {"chat_id": chat_id, "latest_activity_ts": NOW - 48, "sender": "小明", "preview": "今晚看一下前端效果", "thread_signature": "webui-preview", "wait_target_name": "下一条用户反馈"}})
            return
        if path.startswith("/chats/") and path.endswith("/runtime/clear"):
            self._send_json({"ok": True})
            return

        if path == "/config/schema":
            self._send_json(SCHEMA)
            return
        if path in {"/config", "/config/effective"} and method == "GET":
            self._send_json(CONFIG)
            return
        if path == "/config/meta":
            self._send_json({"config_path": str(ROOT / "config.json"), "schema_path": str(ROOT / "_conf_schema.json"), "config_mtime": NOW - 300, "schema_mtime": NOW - 9000, "pending_apply": True, "apply_status": "mock pending reload"})
            return
        if path == "/config/apply":
            self._send_json({"ok": True, "reload_required": True})
            return
        if path.startswith("/config/reset"):
            self._send_json({"ok": True, "data": CONFIG.get(path.rsplit("/", 1)[-1], CONFIG), "reload_required": True})
            return
        if path.startswith("/config") and method in {"POST", "PATCH", "PUT"}:
            self._send_json({"ok": True, "config": CONFIG, "reload_required": True})
            return

        if path == "/reviews/batch" and method == "POST":
            payload = body or {}
            target_ids = {str(item) for item in payload.get("ids", [])}
            new_status = _review_status_for_action(str(payload.get("action") or ""))
            for item in MOCK_REVIEW_ITEMS:
                if item["id"] in target_ids:
                    item["status"] = new_status
            self._send_json({"ok": True})
            return
        if path.startswith("/reviews/") and path.endswith("/submit") and method == "POST":
            review_id = unquote(path.split("/")[2])
            payload = body or {}
            new_status = _review_status_for_action(str(payload.get("action") or ""))
            for item in MOCK_REVIEW_ITEMS:
                if item["id"] == review_id:
                    item["status"] = new_status
                    if payload.get("replacement"):
                        item["expression"] = payload["replacement"]
                    if payload.get("weight") is not None:
                        item["weight"] = payload["weight"]
                    break
            self._send_json({"ok": True})
            return
        if path.startswith("/reviews/") and method == "DELETE":
            review_id = unquote(path.rsplit("/", 1)[-1])
            MOCK_REVIEW_ITEMS[:] = [item for item in MOCK_REVIEW_ITEMS if item["id"] != review_id]
            self._send_json({"ok": True})
            return
        if path.startswith("/reviews/") and method == "PUT":
            review_id = unquote(path.rsplit("/", 1)[-1])
            payload = body or {}
            for item in MOCK_REVIEW_ITEMS:
                if item["id"] == review_id:
                    item.update({key: value for key, value in payload.items() if key in {"expression", "style", "weight", "status"}})
                    break
            self._send_json({"ok": True})
            return
        if path == "/reviews" and method == "POST":
            payload = body or {}
            item = {
                "id": f"rv-{int(time.time() * 1000)}",
                "situation": payload.get("situation", ""),
                "expression": payload.get("expression", ""),
                "style": payload.get("style", ""),
                "weight": payload.get("weight", 1.0),
                "group_id": payload.get("group_id", "GLOBAL"),
                "status": "pending",
            }
            MOCK_REVIEW_ITEMS.insert(0, item)
            self._send_json({"ok": True, "data": item})
            return
        if path == "/reviews/pending":
            self._send_json(_copy_rows([item for item in MOCK_REVIEW_ITEMS if item.get("status") == "pending"]))
            return
        if path == "/reviews":
            status_filter = (query.get("status") or [""])[0]
            rows = MOCK_REVIEW_ITEMS
            if status_filter:
                rows = [item for item in rows if item.get("status") == status_filter]
            self._send_json({"items": _copy_rows(rows), "total": len(rows)})
            return
        if path.startswith("/reviews"):
            self._send_json({"ok": True})
            return

        if path == "/memories/canonical" and method == "GET":
            items = _copy_rows(MOCK_CANONICAL_MEMORIES)
            status = (query.get("status") or [""])[0]
            kind = (query.get("kind") or [""])[0]
            session_id = (query.get("session_id") or [""])[0]
            if status:
                items = [item for item in items if item.get("status") == status]
            if kind:
                items = [item for item in items if item.get("kind") == kind]
            if session_id:
                items = [item for item in items if item.get("session_id") == session_id]
            self._send_json({"status": "ok", "items": items, "total": len(items), "runtime_bound": False})
            return
        if path.startswith("/memories/canonical/") and method == "GET":
            memory_id = unquote(path.rsplit("/", 1)[-1])
            item = next((row for row in MOCK_CANONICAL_MEMORIES if row["id"] == memory_id), None)
            self._send_json({"status": "ok" if item else "not_found", "data": item})
            return
        if path.startswith("/memories/canonical/") and path.endswith("/restore"):
            memory_id = unquote(path.split("/")[3])
            for item in MOCK_CANONICAL_MEMORIES:
                if item["id"] == memory_id:
                    item["status"] = "active"
            self._send_json({"status": "ok", "changed": True})
            return
        if path.startswith("/memories/canonical/") and path.endswith("/stale"):
            memory_id = unquote(path.split("/")[3])
            for item in MOCK_CANONICAL_MEMORIES:
                if item["id"] == memory_id:
                    item["status"] = "stale"
            self._send_json({"status": "ok", "changed": True})
            return
        if path.startswith("/memories/canonical/") and path.endswith("/merge"):
            memory_id = unquote(path.split("/")[3])
            for item in MOCK_CANONICAL_MEMORIES:
                if item["id"] == memory_id:
                    item["status"] = "merged"
                    item["superseded_by"] = (body or {}).get("target_id", "")
            self._send_json({"status": "ok", "changed": True})
            return
        if path.startswith("/memories/canonical/") and method == "DELETE":
            memory_id = unquote(path.rsplit("/", 1)[-1])
            for item in MOCK_CANONICAL_MEMORIES:
                if item["id"] == memory_id:
                    item["status"] = "deleted"
            self._send_json({"status": "ok", "changed": True})
            return
        if path == "/memories/diagnostics/migrations":
            self._send_json({"status": "ok", "data": {"schema_version": 2, "canonical_counts": {"active": 1, "stale": 1}, "migrations": []}})
            return
        if path == "/memories/diagnostics/index":
            self._send_json({"status": "ok", "data": {"projection_count": 2, "missing_projection_ids": [], "orphan_projection_ids": []}})
            return
        if path == "/memories/migration/dry-run":
            self._send_json({"status": "ok", "data": {"mode": "dry_run", "totals": {"importable": 2, "duplicates": 0, "skipped": 0}}})
            return
        if path == "/memories/migration/execute":
            self._send_json({"status": "ok", "data": {"mode": "execute", "imported": {"documents": 1, "MemoryEvent": 1}, "rebuilt_projection": 2}})
            return
        if path == "/memories/migration/verify":
            self._send_json({"status": "ok", "data": {"mode": "verify", "legacy": {"unmapped_memory_events": 0}}})
            return
        if path == "/memories/migration/repair" or path == "/memories/diagnostics/index/repair":
            self._send_json({"status": "ok", "data": {"mode": "repair", "index": {"rebuilt_missing": 0}}})
            return
        if path == "/memories/index/rebuild":
            self._send_json({"status": "ok", "rebuilt": len(MOCK_CANONICAL_MEMORIES)})
            return
        if path == "/memories/maintenance/run":
            self._send_json({"status": "ok", "data": {"decayed": 1, "marked_stale": 0, "restored": 0, "physically_deleted": 0, "projection_deleted": 0, "protected_skipped": 1, "errors": []}})
            return
        if path == "/memories/events" and method == "GET":
            self._send_json(_copy_rows(MOCK_MEMORY_EVENTS))
            return
        if path == "/memories/events" and method == "POST":
            payload = body or {}
            item = {
                "id": f"mem-{int(time.time() * 1000)}",
                "timestamp": NOW,
                "narrative": payload.get("narrative", ""),
                "memory_kind": payload.get("memory_kind", "Misc"),
                "importance": payload.get("importance", 0.5),
                "tags": payload.get("tags", ""),
            }
            MOCK_MEMORY_EVENTS.insert(0, item)
            self._send_json({"ok": True, "data": item})
            return
        if path.startswith("/memories/events/") and method == "DELETE":
            event_id = unquote(path.rsplit("/", 1)[-1])
            MOCK_MEMORY_EVENTS[:] = [item for item in MOCK_MEMORY_EVENTS if item["id"] != event_id]
            self._send_json({"ok": True})
            return
        if path.startswith("/memories/events"):
            self._send_json({"ok": True})
            return
        if path == "/memories/reflections":
            month = (query.get("month") or [""])[0]
            self._send_json(_reflections(month))
            return
        if path.startswith("/memories/reflections"):
            self._send_json({"ok": True})
            return
        if path == "/memories/nodes":
            self._send_json(_memory_nodes())
            return
        if path.startswith("/memories/nodes"):
            self._send_json({"ok": True})
            return
        if path == "/memories/jargon":
            self._send_json(_jargons())
            return
        if path.startswith("/memories/jargon"):
            self._send_json({"ok": True})
            return

        if path == "/users":
            self._send_json(_user_items())
            return
        if path.startswith("/users/"):
            self._send_json({"ok": True})
            return
        if path == "/persona":
            self._send_json(
                {
                    "summary": "亚托莉是一个敏锐、轻快、但有边界感的聊天角色。",
                    "first_person_rewrite": "我是亚托莉。我会先接住眼前这句话，保持轻快，但不会把旧历史当成正在发生的事。",
                    "style": "短句、自然、偶尔轻微调侃。",
                    "updated_at": NOW - 1200,
                }
            )
            return

        self._send_json({"detail": f"mock route not found: {method} {path}"}, 404)


def _build_server() -> ThreadingHTTPServer:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), MockFrontendHandler)
            PORT_PATH.write_text(str(port), encoding="utf-8")
            return server
        except OSError:
            continue
    raise RuntimeError(f"No free port in range {DEFAULT_PORT}-{DEFAULT_PORT + 19}")


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    server = _build_server()
    host, port = server.server_address
    _log(f"Mock frontend server started at http://{host}:{port}")
    _log(f"Serving frontend from {FRONTEND_DIR}")
    _log("Login password: astrmai_admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Mock frontend server stopped by KeyboardInterrupt")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        _log(f"FATAL {type(exc).__name__}: {exc}")
        raise
