# 开发窗口 10：WebUI — 认证安全 + 服务层重构 + 接口契约修复

## 必须先读取的审查报告
1. `artifacts/review_new/r09-webui.md` — 3🔴 12🟡 14🟢

## 审查范围
`astrmai/webui/`（52+ 源文件）

---

## 🔴 严重（3 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `backend/auth.py:94-102` | **密码验证路径歧义**。`secrets.compare_digest(plaintext, stored)` 兜底比较永不为真（stored 是 scrypt hash）。**修复**：移除兜底，统一走 `PluginApiAdapter.check_webui_password`。 |
| 2 | `backend/services/chatruntimeservice.py:1-83` | **服务层循环委托反模式**。ChatRuntimeService → AdminUiService → ChatRuntimeService 循环依赖。**修复**：移除中间层，路由直接调用 AdminUiService。 |
| 3 | `backend/memory_feedback_routes.py:35` | **REST 语义违反**。DELETE 实际执行 disable。**修复**：改名路由为 `/disable`。 |

---

## 🟡 中等（重点 6 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `frontend/js/pages/persona.js:16-30` | **人设编辑功能不可用**。前端期望 `{summary, first_person_rewrite}` 但后端返回 error。修复前后端契约。 |
| 5 | `backend/services/learningservice.py:48-50` | `_expression_pattern_stats()` 永远返回全零（未实现） |
| 6 | `backend/adapters/plugin_api.py:177-196` | `get_webui_password()` 读取时隐式修改配置文件，迁移应在写入时完成 |
| 7 | `backend/server.py:15` | CORS `allow_origins` 写死单一来源，改为环境变量配置 |
| 8 | `frontend/js/api.js:26` | api.request 401 时 `throw res`（抛 Response 对象），统一错误格式 |
| 9 | `backend/repositories.py:82-92` | `UserProfileRepository.update()` 接受原始 SQL 片段，改为 kwargs 模式 |

---

## 🟢 建议（选做）

- `frontend/js/template_loader.js` 模板并行加载
- `backend/services/admin_ui_service.py` 800+ 行 God Object — 标记技术债
- `backend/db.py:25-32` 路径穿越 Windows 兼容性

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/unit/webui/ -q
```

## 成功标准
- 🔴 3 项全部修复
- 🟡 #4 #6 #7 修复（人设编辑是关键用户可见功能）
- 2 个残留测试失败中 #1（canonical_memories）属于本窗口范围
