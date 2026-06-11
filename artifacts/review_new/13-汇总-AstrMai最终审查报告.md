# AstrMai 最终审查报告

## 说明
- 本报告已按当前仓库代码重新校正。
- 本轮已落实 Prompt 12 的结构收口与主链补测；本版同步更新到最新状态。
- 旧报告中部分问题仍然真实存在，部分已因后续重构或本轮收口而过时；本版只保留当前仍有参考价值的结论。

## 总体结论
- Prompt 13 范围内的三项收尾目标已完成当前仓库内的实质收口。
- 插件主入口 `main.py -> PluginFacade -> Plugin Page` 主链路仍然连通，且宿主级证据比 Prompt 12 时更扎实。
- `EvolutionManager` 现在已经形成明确边界：
  - 正式异步入口继续是 `get_active_patterns_canonical_async()`
  - 生产主链不再依赖同步包装
  - 同步包装仅保留为 sync-only 兼容层
- `config.py` 与 `_conf_schema.json` 已按 `performance / evolution / conversation / memory / vision / private_chat` 六个重点段完成覆盖性复查；**本轮未再发现新增配置链断裂**。
- 宿主级最小验收已补强到以下证据组合：
  - 插件注册/加载测试通过
  - `main.py -> PluginFacade.on_global_message() -> yield reply` smoke 通过
  - Plugin Page 注册与关键 handler 调用通过
  - backend HTTP smoke 路由通过
- 当前仓库仍不能直接等同于“真实外部 AstrBot 安装环境 + live provider 的完整发布级验收已完成”，但 Prompt 13 要求的仓内高价值收尾项已完成。

## Prompt 13 收口结论

### 1. EvolutionManager
- 复查仓内生产调用点后，当前主链真实读取位仍是 `planner_side_inputs.py` 中的 `await get_active_patterns_canonical_async(...)`。
- `get_active_patterns_canonical()` / `get_active_patterns()` 继续保留，但定位已经明确为 sync-only 兼容接口。
- 相关回归已证明：
  - 活动事件循环中走 async API 正常
  - 活动事件循环中误调同步包装会收到明确边界错误
  - 纯同步上下文仍可兼容调用同步包装

### 2. 配置链
- 本轮对 `AstrMaiConfig` 与 `_conf_schema.json` 做了覆盖性复查，并对下游真实消费位做了交叉核验。
- 已确认以下六段不存在新的“模型里有但 schema 未暴露”或“schema 暴露但运行时未消费”的新增断裂：
  - `performance`
  - `evolution`
  - `conversation`
  - `memory`
  - `vision`
  - `private_chat`
- 结论应从“建议继续复查”更新为：**本轮未发现新增配置链断裂**。

### 3. 宿主级最小验收
- 当前已形成一组比 Prompt 12 更扎实的宿主级证据：
  - host mock / host registration 测试通过
  - Plugin Page 注册测试通过
  - backend WebUI smoke 测试通过
  - 组合回归中 `main.py -> PluginFacade.on_global_message() -> yield reply` 仍可正常走通
- 本轮额外修复了 WebUI adapter 的弱宿主降级问题：
  - `PluginApiAdapter` 现在区分“未显式传 facade”与“显式传 `None`”
  - 对弱 facade / 半装配 runtime 的查询改为保守降级，不再把宿主级 smoke 直接炸穿

## 已校正的旧结论

以下内容不应再继续算作“当前仓库仍存在的问题”：

- `message_scope` 双副本
  - Prompt 12 已将 `astrmai.presentation.dto.message_scope` 收口为唯一权威实现
  - `astrmai.conversation.contracts.message_scope` 当前仅保留兼容 re-export
- `TextSegmenter.semantic_chunk()` 死方法
  - Prompt 12 已直接删除这条未接线且实现错误的历史残留
- standalone FastAPI backend route/service 显性断裂
  - Prompt03/04 已修复构造参数断裂、缺失方法和 route/service 签名不匹配
  - 已通过 backend route 相关测试与最小路由调用复核
- `astrmai/state/group_wait/group_reply_wait_manager.py` 并发保护
  - Prompt06 已修复 `_states` / timeout task 读写收口与 timeout 误删问题
  - 已通过并发回归测试与窗口级深度代码审查复核
- `astrmai/proactive/wakeup_service.py` 配置防护
  - Prompt07 已修复 `self.config.life.*` 的脆弱直接访问
  - 已通过缺配置 / 缺字段回退测试与窗口级深度代码审查复核
- `astrmai/memory/services/memory_write_service.py` 过滤误杀
  - Prompt09 已收窄 `should_skip_content()` 的 `{` 过滤边界，不再把合法 `{...` 文本一刀切跳过
  - 仍保留对 fenced JSON、异常 token、异常 JSON 载荷的过滤，并新增跳过原因日志
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
- Prompt 12 已补上一条入口级最小 smoke，能够真实经过 `main.py -> PluginFacade.on_global_message() -> yield reply`
- 但这不自动等于完整宿主环境、planner/executor 深链或发布级全量验收已经完成

### Plugin Page / 页面入口
- 当前主支持入口是 AstrBot Plugin Page
- 相关主链路没有被这次复核直接判定为整体断裂
- 此前确认的 standalone FastAPI backend route/service 断裂已收口
- 但这不自动等于整个 standalone backend 已完成全量发布级验证

### 配置系统
- 配置系统此前已知的 4 个缺口仍保持收口状态。
- Prompt 13 的覆盖性复查未再发现新的 schema/runtime 映射断裂。
- 因此当前更准确的判断应为：配置定义层与重点消费链在本轮复查范围内已对齐，而不是继续保守地写成“建议后续复查”。

## 建议的真实优先级

### 仓内第一优先级
- 若继续留在当前插件仓库范围内，优先级已从“继续修 Prompt 13 范围内残留 bug”转为“保持回归稳定，避免重新引入旧链断裂”。

### 仓外第一优先级
- 如果后续要再提高把握度，下一步应是外部真实 AstrBot 安装环境或 live provider 级别的发布前验收，而不是重新打开本轮已收口的问题。

## 最终结论
- 旧版总报告中的“有问题”方向并不是错的。
- 但旧版把“当前真实问题”和“已删除旧链路的历史问题”混在了一起。
- 当前应以本版为准：
  - **Prompt 13 范围内未再保留已确认的仓内阻塞缺陷**
  - **主插件入口可加载，主消息链与 Plugin Page / backend 最小宿主级证据已补强**
  - **`EvolutionManager` 已完成“生产链 async、兼容层 sync-only”的边界收口**
  - **配置链在本轮覆盖性复查范围内未发现新增断裂**
  - **仍未宣称外部真实宿主 + live provider 的完整发布级验收已经完成**
