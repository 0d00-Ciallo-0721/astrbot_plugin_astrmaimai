# P0 — 发布前修复（15 min）

> 基于终审报告未修复项 | 2 项 | 🟡 中等

---

## #1 `lifecycle.py` — timeout 硬编码

**文件：** `astrmai/app/lifecycle.py:195`
**问题：** `_terminate_impl` 中 `await asyncio.wait(unique_tasks, timeout=3.0)` 的 `3.0` 是字面量。
**修复：**

```python
# 类顶部添加
SHUTDOWN_TASK_TIMEOUT: float = 3.0

# 替换 195 行
await asyncio.wait(unique_tasks, timeout=self.SHUTDOWN_TASK_TIMEOUT)
```

**验证：** `python -m pytest tests/ -q --tb=line -k "not (admin_full_fixture)"`

---

## #2 `api.js` — 401 错误格式统一

**文件：** `astrmai/webui/frontend/js/api.js:30`
**问题：** 401 分支 `throw res`（抛 Response 对象），其他非 200 路径 `throw { status, data }`，调用方无法统一捕获。
**修复：**

```javascript
// 将第 30 行 throw res 改为
const data = await res.text();
try { data = JSON.parse(data); } catch (_) {}
throw { status: 401, data };
```

**验证：** 前端登出后调用 API，确认 catch 块收到 `{status: 401, data}` 格式。
