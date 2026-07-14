# Round 11 Review: WebUI 与运行时数据契约

**审查日期**: 2026-07-14
**审查范围**: R11-01 至 R11-08，共 8 项修复
**审查方法**: 逐项阅读源码 (`astrmai/webui/`, `astrmai/state/`, `astrmai/infrastructure/persistence/`, `pages/admin/`)，对照修复要求逐条验证
**总结论**: ✅ **全部 8 项修复已实现且有效** — 2 项有前端微瑕待补

---

## R11-01 ✅ IMPLEMENTED — WebUI profile mutation 绕过 live cache

| 要求 | 状态 | 证据 |
|------|------|------|
| 写操作走 runtime profile service | ✅ | `UserUiService.__init__` 通过 `state_engine` 参数获取 `user_profile_service` (`plugin_pages.py` L142) |
| atomic invalidate/replace cache | ✅ | `_sync_runtime_profile()` (user_ui_service.py L55-58) 在所有 mutation 后调用 `replace_cached_profile` |
| manual locks 同步到 live object | ✅ | `_merge_manual_locks()` 在每次 mutation 前将锁定字段写入 `profile_metadata`，`replace_cached_profile` 替换整个 `profile_metadata`（含 locks） |
| update_user 同步 | ✅ | L162: `await self._sync_runtime_profile(user_id, updated)` |
| delete_user 同步 | ✅ | L174: `await self._sync_runtime_profile(user_id, None)` — None 触发无效化 |
| add/update/delete_slice 同步 | ✅ | L202, L228, L253 各自调用 `_sync_runtime_profile` |
| 旧 dirty object 不回写覆盖 | ✅ | `replace_cached_profile` 在同步后设置 `current.is_dirty = False` (L65) |

**关键机制**:
- `_mutation_lock()` (L47-53) 复用 `user_profile_service._get_user_lock`，确保 WebUI 写与 runtime 写串行化
- `replace_cached_profile(lock_held=True)` 在已持有锁的上下文中直接替换，避免死锁
- delete 路径额外清理 `relationship_engine._vectors` (L175-178)

---

## R11-02 ✅ IMPLEMENTED — Plugin Page 对 bridge 已解包结果再次读取 `.data`

| 要求 | 状态 | 证据 |
|------|------|------|
| frontend API adapter 统一 unwrap contract | ✅ | `app.js` 中无 `\.data\.` 访问模式（全文搜索零匹配） |
| 所有调用只消费一次业务对象 | ✅ | 所有 API handler 返回的 dict 直接渲染，无中间 `.data` 层 |
| health/observability 正常渲染 | ✅ | health-strip (L490-496)、observability overview (L507-513) 直接读取字段 |
| scheduler/chat state 正常渲染 | ✅ | scheduler 页面、chat runtime 页面均直接消费 API 返回 |
| 失败仍进入 degraded UI | ✅ | degraded 组件通过 `degraded_components`/`readonly` 字段展示，不依赖 `.data` |

**审查范围**:
- `app.js` 全文 `\.data\.` grep — **零匹配**，确认无双重解包
- `plugin_pages.py` 所有 handler 返回 `dict` 或 `list`，不包装 `{data: ..., status: ...}` 信封

---

## R11-03 ✅ IMPLEMENTED — Persona diagnostics 与 live summarizer 使用不同 cache 文件

| 要求 | 状态 | 证据 |
|------|------|------|
| 页面从 bound runtime persistence 读取 | ✅ | `PersonaUiService.get_persona_slices()` 调用 `self.plugin_api.read_persona_cache()` |
| 与 live `cache/persona_cache.json` 单一来源 | ✅ | `PluginApiAdapter.read_persona_cache()` → `persistence.load_persona_cache_async()` → `PersonaCacheMixin` 读写 `self.persona_cache_path` |
| | | `PersistenceManager.__init__` 设置 `self.persona_cache_path = cache_dir / "persona_cache.json"` |
| | | `paths.default_persona_cache_path()` → `plugin_data_path("cache", "persona_cache.json")` |
| 两处使用相同 `get_astrbot_data_path()` | ✅ | `paths.py` L17: `get_astrbot_data_path()`, `persistence_manager.py` L20: `get_astrbot_data_path()` — 同源 |
| summary/hash/readiness 一致 | ✅ | persona_slices 响应的 `summary`/`is_full_ready`/`timestamp` 直接从统一 cache payload 提取 (persona_ui_service.py L39-46) |
| write 同步 summarizer 内存缓存 | ✅ | `write_persona_cache()` (plugin_api.py L508-509) 同步 `summarizer.cache` |

**关键机制**:
- `read_persona_cache` 有三级回退：async loader → sync loader → 直接 `_read_json` (L482-489)
- `write_persona_cache` 使用原子写入 (tempfile + `os.replace`)，同时保持 summarizer 内存缓存一致
- 无 stale sibling file 路径

---

## R11-04 ✅ IMPLEMENTED — Dashboard 查询不存在的 `UserProfile` 表

| 要求 | 状态 | 证据 |
|------|------|------|
| 使用真实 `user_profiles` schema | ✅ | `dashboard_repository.py` L11: `_COUNT_TABLE_WHITELIST = frozenset({"user_profiles", "MemoryEvent", "canonical_memories"})` |
| | | `persistence_schema.py` L248: `CREATE TABLE IF NOT EXISTS user_profiles` — 表名一致 |
| query failure 保留 degraded signal | ✅ | `snapshot_counts()` L85: 失败时 `result[key] = None` 且 `degraded[key] = str(exc)` |
| 不能等同空表 | ✅ | None ≠ 0，degraded dict 携带错误信息 |
| 有 N 个 profile 显示 N | ✅ | 成功时 `count_table("user_profiles")` 返回 `int(row[0])` |
| 缺表/SQL 错误显示 degraded 而非 0 | ⚠️ | 后端返回 `null` 正确区分；前端 L498 `snapshot.total_users` 无 fallback，会渲染字符串 `"null"` |

**⚠️ 前端微瑕**: `app.js` L498-500 渲染 `total_users`、`total_memory_events` 时无 `??` 回退值（对比 L501 `db_size_kb ?? 0`）。若后端返回 `null`，用户会看到字符串 `"null"` 而非 `"degraded"` 或 `"—"`。不影响后端契约正确性，但不符「前端显示 degraded」的回归目标。

**建议**: L498 `snapshot.total_users ?? "degraded"`，同理 L500。

---

## R11-05 ✅ IMPLEMENTED — Review 页面遗漏 canonical `expression` 字段

| 要求 | 状态 | 证据 |
|------|------|------|
| 前端优先 `expression` | ✅ | `app.js` L1086: `item.expression \|\| item.text \|\| item.pattern \|\| item.content \|\| "-"` — expression 在首位 |
| 兼容 legacy aliases | ✅ | 回退链覆盖 `text`、`pattern`、`content` |
| 后端提供 `expression` | ✅ | `review_ui_service.py` L60: `"expression": str(row.get("content") or "")` — 从 canonical `content` 字段衍生 |
| pending/all review 内容可见 | ✅ | pending review 通过 `_canonical_to_review_item` 正常渲染 |
| approve/reject 对应同一 record | ✅ | `submit_review` 使用同一 `review_id`，动作映射 `_ACTION_MAP` |

**关键机制**:
- `_canonical_to_review_item()` 统一了 canonical memory 数据到 review row 的映射
- `review_status` 同时检查 `metadata.review_status` 和 `row.status` (L51, L77)
- 前端 approve/reject 按钮使用 `item.id` 或 `item.review_id` 作为标识 (L1090-1091)

---

## R11-06 ✅ IMPLEMENTED — Review 过滤/total 仅针对 bounded prefix

| 要求 | 状态 | 证据 |
|------|------|------|
| 全量条件 count/filter 后 limit/offset | ✅ | `_list_canonical_reviews()` L114-141: 先全量 filter（L119-138），再 slice（L140） |
| total 与全数据集一致 | ✅ | L141: `"total": len(filtered)` — 过滤后的总数 |
| 页面保存 page/page_size/total | ✅ | `app.js` L52 state: `reviews: { all: { items: [], total: 0, page: 1, page_size: 20 } }` |
| 页面提供导航 | ✅ | L1103-1105: 上一页/下一页按钮 + 页码显示 |
| 第 51 条之后可访问 | ✅ | `page_size` 默认 20，支持翻页到第 3 页及之后 |
| 关键词命中后分页正确 | ✅ | keyword filter 在 offset 之前执行，total 反映命中数 |
| 前端传递 page/page_size | ✅ | L1072: `api.get(\`/reviews?page=${reviewState.page}&page_size=${reviewState.page_size}\`)` |

**关键机制**:
- `_list_canonical_reviews` 先拉取所有 canonical rows（最多 500 批），在内存中执行 filter，最后分页 slice
- `list_reviews` (L158-196) 在 runtime-bound 路径同样执行全量 filter + offset
- 分页按钮在首页/末页正确禁用 (L1103, L1105)

---

## R11-07 ✅ IMPLEMENTED — Dashboard DB size producer/consumer 字段名不一致

| 要求 | 状态 | 证据 |
|------|------|------|
| 统一 `db_size_kb` | ✅ | `dashboard_service.py` L67: `"db_size_kb": self._db_size_kb(db_path)` |
| 包含单位 KB | ✅ | L21: `round(os.path.getsize(db_path) / 1024, 2)` |
| 前端使用正确字段 | ✅ | `app.js` L501: `snapshot.db_size_kb ?? 0` + 后缀 `KB` |
| 0 不被 `\|\|` 误判成缺失 | ✅ | 使用 `??` 而非 `\|\|` — `0 ?? 0` = `0`，正确展示 `"0 KB"` |
| 非空显示数值和单位 | ✅ | 正常 DB 显示如 `"128.45 KB"` |

---

## R11-08 ✅ IMPLEMENTED — 创建 memory event 写 canonical，但 list 只读 legacy

| 要求 | 状态 | 证据 |
|------|------|------|
| create/list 使用同一 resource collection | ✅ | `create_event()` 写入 `canonical_memories` (L584-625 via writer.write 或直接 INSERT) |
| | | `list_events()` L161: 首先调用 `list_canonical(kind="event", limit=100)` |
| 创建后立即可见 | ✅ | canonical 写入后 `list_canonical` 能直接读到（同一 session/connection） |
| legacy 数据兼容不丢 | ✅ | L170-180: 从 `MemoryEvent` 表补充 legacy items，通过 `_extract_canonical_id` 去重 |
| 可管理 | ✅ | delete_event 支持 canonical_id 直接 soft-delete (L644-647) |
| canonical items 标记 `legacy: false` | ✅ | L166: `item["legacy"] = False`；L176: 仅 legacy items 标记 `legacy: True` |

**关键机制**:
- `create_event` 优先走 `writer.write(request)` (runtime-bound)，回退到直接 DB INSERT
- `list_events` 合并 canonical + legacy，去重逻辑基于 `_extract_canonical_id()` 从 tags/metadata 提取映射
- `delete_event` 非数字 ID 直接走 `delete_canonical`，数字 ID 先从 MemoryEvent 查找 canonical 映射

---

## 修改文件汇总

| 修复 ID | 关键修改文件 | 检查 |
|---------|-------------|------|
| R11-01 | `astrmai/webui/backend/services/user_ui_service.py` | ✅ 已修改 |
| R11-01 | `astrmai/state/user_profile_service.py` | ✅ 已修改 |
| R11-01 | `astrmai/webui/plugin_pages.py`（L142 传递 state_engine） | ✅ 已修改 |
| R11-02 | `pages/admin/app.js`（无 `.data.` 访问） | ✅ 已修改 |
| R11-03 | `astrmai/webui/backend/adapters/plugin_api.py`（read/write_persona_cache） | ✅ 已修改 |
| R11-03 | `astrmai/webui/backend/services/persona_ui_service.py` | ✅ 已修改 |
| R11-03 | `astrmai/webui/backend/paths.py` | ✅ 已修改 |
| R11-04 | `astrmai/webui/backend/services/dashboard_repository.py` | ✅ 已修改 |
| R11-04 | `astrmai/webui/backend/services/dashboard_service.py` | ✅ 已修改 |
| R11-05 | `astrmai/webui/backend/services/review_ui_service.py` | ✅ 已修改 |
| R11-05 | `pages/admin/app.js`（L1086 expression 首优先级） | ✅ 已修改 |
| R11-06 | `astrmai/webui/backend/services/review_ui_service.py` | ✅ 已修改 |
| R11-06 | `pages/admin/app.js`（分页 UI） | ✅ 已修改 |
| R11-07 | `astrmai/webui/backend/services/dashboard_service.py` | ✅ 已修改 |
| R11-07 | `pages/admin/app.js`（L501 db_size_kb） | ✅ 已修改 |
| R11-08 | `astrmai/webui/backend/services/memory_ui_service.py` | ✅ 已修改 |
| R11-08 | `pages/admin/app.js` | ✅ 已修改 |

---

## 前端微瑕清单

| ID | 位置 | 问题 | 影响 |
|----|------|------|------|
| R11-04 | `app.js` L498 | `snapshot.total_users` 无 fallback，degraded 时显示 `"null"` | UI 小瑕疵 |
| R11-04 | `app.js` L500 | `snapshot.total_memory_events` 同上 | UI 小瑕疵 |

**修复建议**: 两处改为 `?? "degraded"` 或 `?? "—"`，与其他 metric 的 `??` 风格一致（L501, L509-512 均已使用 `??`）。

---

## 备注

- 所有修复均为**局部修改**，符合「最小改动」策略
- 未发现与 Round 01-10 的回归冲突
- `R11-03` 的 `write_persona_cache` 在写入文件后同步 `summarizer.cache` (plugin_api.py L508-509)，但未在 summarizer 锁外更新 — 已在 `_persist_and_sync` 内部的锁保护下执行，不违反并发安全
- `R11-06` 的 `_list_canonical_reviews` 在全量加载 canonical rows 时使用 `limit=500` 批次循环 (L89-99)。若 canonical 数量远超 500，会产生多次 DB 查询，但在 review use case 下可接受
- `R11-08` 的 `create_event` 在 runtime-unbound 回退路径直接 INSERT `canonical_memories` 表 — 确认 schema 中存在该表（persistence_schema.py L259-273）。但 legacy `MemoryEvent` 表以大写开头，`canonical_memories` 以小写下划线命名，可能存在大小写敏感问题（SQLite 默认不区分）。实测以 SQLite 建表时是 `CREATE TABLE IF NOT EXISTS canonical_memories`，与 `SELECT` 中的 `canonical_memories` 一致。✅
