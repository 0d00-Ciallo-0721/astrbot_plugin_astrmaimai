# AstrMai 最终审查报告

## 说明
- 本报告已按当前仓库代码重新校正。
- 本次仅修正报告内容，不修改任何业务代码。
- 旧报告中部分问题仍然真实存在，部分已因后续重构而过时；本版只保留当前仍有参考价值的结论。

## 总体结论
- 当前仓库**不是“完全无问题”状态**。
- 插件主入口 `main.py -> PluginFacade -> Plugin Page` 主链路整体仍可用。
- 但仓库中仍存在一组**真实运行级问题**，主要集中在：
  - `astrmai/webui/backend/routes/*` 与对应 service 的断裂
  - `config.py` 与 `_conf_schema.json` 的配置映射断裂
  - 若干并发与运行时防护不足
  - 记忆写入内容过滤过严

## 已确认仍然真实存在的问题

### P1

#### 1. WebUI 后端 route/service 断裂
以下问题当前仍然成立：

- `astrmai/webui/backend/routes/cognition_routes.py`
  - `_service()` 调用 `CognitionService(PluginApiAdapter(), get_db)`
  - 但 `astrmai/webui/backend/services/cognitionservice.py` 构造函数只接收 `plugin_api`
- `astrmai/webui/backend/routes/runtime_routes.py`
  - `_service()` 调用 `ObservabilityService(PluginApiAdapter(), get_db)`
  - 但 `astrmai/webui/backend/services/observabilityservice.py` 构造函数只接收 `plugin_api`
- `astrmai/webui/backend/routes/heartflow_routes.py`
- `astrmai/webui/backend/routes/learning_routes.py`
- `astrmai/webui/backend/routes/tools_routes.py`
  - 这三处也仍然把 `get_db` 多传给了只接收 `plugin_api` 的 service

额外补充：
- `tools_routes.py` 还调用了 `recent_tool_traces()`，但 `toolsservice.py` 当前没有这个方法
- `learning_routes.py` 还调用了 `run_reflect_once()`，但 `learningservice.py` 当前没有这个方法
- `cognition_routes.py` 里 `recent_turn_traces(chat_id=..., limit=...)` 与 `cognition_unified_timeline(chat_id, limit, level, include)` 也和当前 `CognitionService` 签名不匹配

影响判断：
- 这组问题对 `astrmai/webui/backend/server.py` 这条 standalone FastAPI 聚合链是实质性断裂
- 但**不能直接等同于**当前 `main.py` 注册的 AstrBot Plugin Page 主入口一定不可用

#### 2. 配置链断裂
以下问题当前仍然成立：

- `_conf_schema.json` 仍存在 `conversation` 配置段
- `config.py` 中 `AstrMaiConfig` 当前没有对应的 `conversation` 字段
- `_conf_schema.json` 中 `deep_temporal_*` 与 `maintenance_*` 字段当前仍挂在 `global_settings`
- `config.py` 中这些字段定义在 `MemoryConfig`

影响判断：
- 用户通过配置界面或配置文件写入的部分参数，存在静默失效风险

#### 3. 并发与运行时防护不足
以下问题当前仍然成立或基本成立：

- `astrmai/learning/evolution_manager.py`
  - `get_active_patterns_canonical()` 在 loop 活跃时会直接抛 `RuntimeError`
- `astrmai/state/energy/frequency_controller.py`
  - 声明了 `_records_lock`，但当前未实际用于 `_records` 的保护
- `astrmai/state/group_wait/group_reply_wait_manager.py`
  - 正常 `_states` 读写路径未完整加锁
- `astrmai/proactive/wakeup_service.py`
  - 仍直接访问 `self.config.life.*`

影响判断：
- 这些问题更偏向“特定条件触发”的运行时风险
- 不一定启动即炸，但不能视为安全

#### 4. 记忆写入过滤过严
- `astrmai/memory/services/memory_write_service.py`
  - `should_skip_content()` 仍然直接跳过以 `{` 开头的内容

影响判断：
- 可能误杀本应写入记忆的正常文本内容

## 已校正的旧结论

以下内容不应再继续算作“当前仓库仍存在的问题”：

- standalone 前端残留
  - `astrmai/webui/frontend/` 当前已删除
- mock server 链路
  - `astrmai/webui/mock_frontend_server.py` 当前已删除
- 远程图片 URL 安全链路
  - `astrmai/infrastructure/security/url_validator.py` 当前已删除
  - `REMOTE_IMAGE_ALLOWLIST.md` 当前已删除

## 对当前运行状态的更准确判断

### 插件加载
- 当前 `main.py` 插件入口仍然完整
- `@register`、`PluginFacade`、`register_astrmai_admin_pages(...)` 仍然连通
- 从入口代码看，插件**大概率可以正常加载**

### 主消息链
- `main.py -> facade -> ingress/attention/planning/execution` 主链路未发现“已删除模块仍被直接引用”的明显断点
- 但并发与运行时防护问题仍可能在特定条件下触发异常

### Plugin Page / 页面入口
- 当前主支持入口是 AstrBot Plugin Page
- 相关主链路没有被这次复核直接判定为整体断裂
- 但 standalone FastAPI backend 路由层当前存在明显断裂，不应视为可用

### 配置系统
- 配置系统当前存在真实断裂，尤其是 `conversation` 段缺失与 `deep_temporal_*` / `maintenance_*` 错位

## 建议的真实优先级

### 第一优先级
- 修 `astrmai/webui/backend/routes/*` 与 service 的签名/方法断裂
- 修 `config.py` 与 `_conf_schema.json` 的映射断裂

### 第二优先级
- 修 `frequency_controller.py` / `group_reply_wait_manager.py` 的并发保护
- 修 `evolution_manager.py` 的运行时 worker/loop 调用路径

### 第三优先级
- 重新评估 `memory_write_service.py` 的 `{` 过滤规则

## 最终结论
- 旧版总报告中的“有问题”方向并不是错的。
- 但旧版把“当前真实问题”和“已删除旧链路的历史问题”混在了一起。
- 当前应以本版为准：
  - **仍有真实 bug**
  - **主插件入口大概率可加载**
  - **standalone FastAPI backend 不能视为已修好**
  - **配置链与部分运行时链路仍需继续修复**
