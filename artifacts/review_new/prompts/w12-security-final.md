# 开发窗口 12：安全专项 + 最终收口

## 必须先读取的审查报告
1. `artifacts/review_new/r12-security.md` — 4🔴 9🟡 5🟢
2. `artifacts/review_new/r00-master-index.md` — 总报告

## 当前测试基线
713 passed / 2 failed / 1 skipped

---

## 🔴 安全严重（4 项）— SSRF 最高优先级

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `executor.py:405` | **SSRF：用户提供的图片 URL 直接作为 aiohttp GET 目标**。无域名/IP 白名单。攻击者可访问 `169.254.169.254` 云元数据。**修复**：添加域名白名单，禁用私有 IP 段。 |
| 2 | `vision_binding.py:34` | **SSRF：`extract_image_base64_from_url` 对任意 URL 发起请求**。同 #1，添加 URL 校验。 |
| 3 | `mock_frontend_server.py:631` | **硬编码认证绕过**。任何非 "wrong" 密码返回有效 token。**修复**：`if __debug__` 保护或移除。 |
| 4 | `plugin_api.py:83-86` | **路径穿越符号链接绕过**。`_validate_path` 未处理 symlink 攻击。**修复**：`os.path.realpath` 前检查符号链接。 |

---

## 🟡 安全中等（重点 5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `server.py:19-25` | CORS `allow_headers=["*"]` 过于宽松 + token localStorage |
| 6 | `api.js:3` | JWT Token 存储在 localStorage 无 HttpOnly 保护 |
| 7 | `context_economy/center.py:455` | SHA-1 缓存亲和性哈希 → SHA-256 |
| 8 | `prompt_refiner.py:709` | LLM 输出 JSON 直接嵌入 prompt，存在注入风险放大 |
| 9 | `auth_routes.py:25-35` | 登录速率限制基于 `request.client.host`，反向代理下失效 |

---

## 🟢 批量替换（弱哈希）

全局替换 SHA-1/MD5 → SHA-256（6 处）：
- `context_economy/center.py:455` (SHA-1)
- `persona_summarizer.py:32` (MD5)
- `context_engine.py:207,494,595` (MD5 × 3)
- `thread_builder.py:130` (MD5)

---

## 残留测试失败修复

| # | 测试 | 根因 |
|---|------|------|
| T1 | `test_admin_full_fixture_supports_backend_service_views` | `no such table: canonical_memories` — v2_store 独立数据库后某处仍引用旧路径 |
| T2 | `test_project_files_do_not_embed_local_absolute_paths` | `.agent/compact-report.md` 包含本地绝对路径 — 删除或加入忽略列表 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'

# SSRF 扫描
python -c "import os; [print(os.path.join(r,f)) for r,d,fs in os.walk('astrmai') for f in fs if f.endswith('.py') and 'aiohttp' in open(os.path.join(r,f),errors='ignore').read() and 'url' in open(os.path.join(r,f),errors='ignore').read().lower()]"

# 硬编码密钥扫描
python -c "import os; [print(os.path.join(r,f)) for r,d,fs in os.walk('astrmai') for f in fs if f.endswith('.py') and any(k in open(os.path.join(r,f),errors='ignore').read().lower() for k in ['password=', 'secret=', 'token=', 'api_key=', 'private_key='])]"

# 全量测试
python -m pytest tests/ -q --tb=line
```

## 成功标准
- 🔴 4 项安全严重全部修复
- 🟡 #7 弱哈希替换为 SHA-256
- 批量替换 6 处 SHA-1/MD5
- 2 → 0 失败
- 713+ passed
