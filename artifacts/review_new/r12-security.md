# 安全审查报告：astrmai/（全量 .py 文件）
> task_id: r12-security | 审查时间: 2025-07-16

## 📋 执行摘要

对 astrmai/ 模块共 **293 个 Python 文件** 进行了全量安全审计，覆盖认证授权、注入风险、密钥管理、反序列化、路径穿越、加密实践和 SSRF 七个维度。

**整体安全态势：中等偏上。** 核心业务代码（对话、记忆、学习）安全设计较好：无 `pickle` 反序列化、无 `eval`/`exec` 调用、SQL 全部使用参数化查询或 ORM。主要风险集中在 **WebUI 边界**（SSRF、硬编码认证绕过、CORS 配置）和 **Mock 开发服务器**（无认证）。另外多处使用 **SHA-1/MD5**（虽用于非安全场景，但仍应替换）。

**发现总数：18 | 🔴 严重 4 | 🟡 中等 9 | 🟢 建议 5**

---

## 🔴 严重发现

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `astrmai/conversation/execution/executor.py:405` | **SSRF：用户提供的图片 URL 直接作为 aiohttp GET 请求目标，未做域名/IP 白名单校验。** 攻击者可构造 `http://169.254.169.254/latest/meta-data/` 或内网地址，导致云元数据泄露或内网探测。`url_or_path` 源自 `vision_bundle.direct_image_urls`，由用户消息触发。 |
| 2 | `astrmai/conversation/attention/vision_binding.py:34` | **SSRF：`extract_image_base64_from_url` 对任意 URL 发起请求无校验。** 调用方 `gate` 传入的 `url` 直接传给 `session.get(url, timeout=5)`，可被用于 SSRF 攻击。 |
| 3 | `astrmai/webui/mock_frontend_server.py:631` | **硬编码认证绕过：Mock 服务器接受任何非 "wrong" 的密码返回有效 token。** 第 630-632 行逻辑 `if password and password != "wrong"`，意味着 `password="anything"` 即返回 `{"token": "mock-admin-token"}`。这是开发残件，若部署到生产将完全绕过认证。 |
| 4 | `astrmai/webui/backend/adapters/plugin_api.py:83-86` | **路径穿越保护存在绕过风险：`_validate_path` 使用 `os.path.realpath` 但未处理符号链接攻击。** 如果攻击者能在允许目录内创建指向外部的符号链接，`os.path.realpath` 会解析出外部路径，绕过 `allowed_roots` 检查。 |

---

## 🟡 中等发现

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `astrmai/webui/backend/server.py:19-25` | **CORS 过于宽松：`allow_origins=["http://localhost:8765"]` 但 `allow_headers=["*"]` 且无凭证限制。** 同时服务器监听 `0.0.0.0:8765`，局域网内其他设备可访问。结合 token 存储在 `localStorage`，存在 XSS+CSRF 链式攻击风险。 |
| 6 | `astrmai/webui/frontend/js/api.js:3` | **JWT Token 存储在 localStorage，无 HttpOnly/Cookie 机制。** 遭受 XSS 攻击时 token 可被窃取。建议使用 HttpOnly Cookie + CSRF Token 方案。 |
| 7 | `astrmai/webui/backend/auth.py:40-49` | **JWT 密钥通过环境变量 `ASTRMAI_WEBUI_SECRET` 配置，若未设置则启动失败。** 这是合理的设计，但缺少密钥轮换机制。另外 `HS256` 是对称算法，前端无法验证签名。建议记录此项为运维要求。 |
| 8 | `astrmai/webui/backend/auth.py:86` | **回退密码比较路径使用 `secrets.compare_digest(plaintext, stored)`，但保留了明文比较的 fallback。** 当 adapter 不支持 `check_webui_password` 方法时，从配置读取原始密码并与明文比较。若配置中的密码是 scrypt hash 而非明文，这段代码不会正确工作。 |
| 9 | `astrmai/infrastructure/context_economy/center.py:455` | **使用 SHA-1 作为缓存亲和性哈希键。** `hashlib.sha1(payload.encode("utf-8")).hexdigest()` — SHA-1 已被 NIST 弃用，虽用于非安全场景（缓存键），但应迁移至 SHA-256 或更安全的算法。 |
| 10 | `astrmai/infrastructure/persistence/database_jargon.py:72-81` | **原始 SQL 中参数占位符通过 f-string 构造但状态值仍参数化，存在 SQL 注入风险（虽当前安全）。** 第 72-81 行 `f"""...status IN ({','.join('?' for _ in statuses)})..."""` — `statuses` 的值通过 `?` 参数化，但如果未来不小心将用户输入拼入 f-string，将直接产生注入漏洞。建议使用 ORM 或 `executemany`。 |
| 11 | `astrmai/webui/backend/adapters/plugin_api.py:416-428` | **WebUI 密码迁移逻辑中如果旧密码是明文，`compare_digest(plaintext, stored)` 会做明文比较。** 第 447 行：`return secrets.compare_digest(plaintext, stored)` — 若 `stored` 是明文，则"安全比较"的只是两个明文字符串，攻击者可通过时序攻击（虽然极难）之外的途径获取密码。 |
| 12 | `astrmai/conversation/planning/prompt_refiner.py:709` | **LLM 输出的 JSON 直接 `json.loads` 后用于构造 prompt，存在 prompt 注入风险放大。** 虽然单独的 `json.loads` 本身安全，但 LLM 输出的恶意内容被直接嵌入后续系统 prompt（如第 715 行 `f"[发了一个表情包，画面是：{mem.description}]"`），可能被用于 prompt 注入链。 |
| 13 | `astrmai/webui/backend/routes/auth_routes.py:25-35` | **登录速率限制基于客户端 IP，但在反向代理场景下 `request.client.host` 可能全是代理 IP。** 未检查 `X-Forwarded-For` 头，攻击者可通过代理绕过速率限制。 |

---

## 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 14 | `astrmai/memory/persona/persona_summarizer.py:32` | **MD5 用于缓存键生成。** 虽非安全用途，但 MD5 碰撞攻击已是现实威胁。建议迁移至 SHA-256：`hashlib.sha256(text.encode()).hexdigest()[:16]`。 |
| 15 | `astrmai/conversation/planning/context_engine.py:207,494,595` | **多处使用 MD5 生成语义哈希、风格种子等。** 第 207 行系统提示哈希、第 494 行随机种子、第 595 行风格种子。建议统一替换为 `hashlib.blake2b` 或 `secrets.token_hex`。 |
| 16 | `astrmai/conversation/attention/thread_builder.py:130` | **MD5 用于生成线程指纹。** 同上建议迁移至更安全的哈希或直接使用 UUID。 |
| 17 | `astrmai/infrastructure/gateway/gateway_lane.py:61-63` | **LLM API key/token 可能出现在日志中。** 第 61-63 行的 `usage` 字典如果意外包含 `api_key` 字段，会被记录到 `token_usage` 日志。建议在记录前做敏感字段脱敏过滤。 |
| 18 | `astrmai/webui/mock_frontend_server.py:1012` | **开发服务器日志中明文打印默认密码。** `_log("Login password: astrmai_admin")` — 虽然仅用于开发，但 PR 合并时可能被遗漏到生产代码。建议加 `if __debug__` 保护。 |

---

## ✅ 安全态势总评

| 维度 | 评价 | 说明 |
|------|------|------|
| **认证/授权** | 🟡 较好 | WebUI 路由全部使用 `Depends(get_current_user)`，JWT 签名强制配置。Mock 服务器是最大短板。 |
| **注入风险** | 🟢 安全 | 无 SQL 注入（全部参数化或 ORM），无 eval/exec，无 pickle。 |
| **密钥管理** | 🟡 合理 | 通过环境变量注入密钥，无硬编码。建议增加密钥轮换和日志脱敏。 |
| **反序列化** | 🟢 安全 | 仅使用 `json.loads`（安全的），无 `pickle`/`yaml.load`。 |
| **路径穿越** | 🟡 基本安全 | `_validate_path` 做了 realpath 校验，但符号链接攻击向量未覆盖。 |
| **加密实践** | 🟡 需改进 | SHA-1/MD5 多处用于非安全用途，建议批量替换。密码存储使用 scrypt，实践正确。 |
| **SSRF 风险** | 🔴 需修复 | 两个核心路径（executor + vision_binding）对外部 URL 无校验，这是最紧急的修复项。 |

### 紧急修复项（按优先级）

1. **executor.py:405** — 对用户提供的图片 URL 添加域名白名单校验，禁用私有 IP 段
2. **vision_binding.py:34** — 同上，对 `extract_image_base64_from_url` 添加 URL 校验
3. **mock_frontend_server.py:631** — 移除或添加 `__debug__` 保护，确保不部署到生产
4. **SHA-1/MD5 → SHA-256** — 批量替换 6 处弱哈希调用

### 整体评分：**B（良好）**

核心对话引擎安全设计扎实，所有 SQL 使用参数化查询，无危险反序列化，WebUI 路由全部受 JWT 保护。主要风险集中在 URL 请求校验缺失和开发残件，修复优先级清晰。
