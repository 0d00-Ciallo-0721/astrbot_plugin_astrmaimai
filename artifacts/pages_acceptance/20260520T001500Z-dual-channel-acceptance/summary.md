# AstrMai 双通道页面验收说明

- 时间：2026-05-20 00:15 (Asia/Shanghai)
- 范围：AstrBot 宿主页挂载链路 + `admin_full` 直开页细粒度验收
- 目标：把“是否已正常挂进 AstrBot”和“插件页自身细节是否正确”拆开验收，再合并成完整结论

## 验收结论

这轮双通道验收整体通过。

- **宿主页链路** 证明 AstrMai 插件页已经能被 AstrBot 正常挂载，bridge 已成功注入，宿主路由可用。
- **直开页链路** 证明在可控 seed 数据下，scheduler、reviews、memories、users、persona 这些管理页内容已经能稳定渲染，并完成了结构、轮询与空状态验证。

合并后的结论是：

> 当前 AstrMai 管理台页面没有新增已确认的页面逻辑 bug。  
> 宿主页自动化剩余阻塞来自 `@chrome` 对 AstrBot 跨进程 iframe 的能力边界，而不是插件页本身的已确认缺陷。

## 通道 A：AstrBot 宿主页挂载验收

### 证据目录

- [宿主页验收目录](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T221500Z-astrbot-plugin-host-check)
- [宿主截图](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T221500Z-astrbot-plugin-host-check/01-plugin-page-host.png)
- [宿主摘要](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T221500Z-astrbot-plugin-host-check/summary.md)
- [宿主结果 JSON](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T221500Z-astrbot-plugin-host-check/result.json)

### 已确认通过

- AstrBot 宿主可访问
- `#/plugin-page/astrmai/admin` 可打开
- 插件页已在宿主 iframe 中渲染
- `Bridge 已连接` 可见
- Dashboard 内容可见
- `主动决策池 Cognition` 子页签可见

### 宿主页链路负责证明什么

- AstrBot 路由是否正常
- Plugin Page bridge 是否注入成功
- AstrMai 插件页是否真的被宿主挂载
- 顶层布局与入口是否工作

### 宿主页链路的剩余边界

当前 `@chrome` 不能自动穿透 AstrBot 的跨进程 `iframe.plugin-page-frame`，所以当时没法在宿主页里继续自动完成：

- cognition 内部点击
- `Scheduler Diagnostics` 第一块结构确认
- `5s` 轮询观察
- 空 `chat_id` drill-down 验证

这条边界已经记录为**验收工具能力边界**，不定义为页面 bug。

## 通道 B：admin_full 直开页细粒度验收

### 证据目录

- [直开页验收目录](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full)
- [直开页摘要](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/summary.md)
- [直开页结果 JSON](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/result.json)

### 核心截图

- [Scheduler / Cognition](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/01-cognition-scheduler.png)
- [空 state drill-down](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/02-cognition-empty-state.png)
- [轮询后稳定状态](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/03-cognition-after-polling.png)
- [Reviews](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/04-reviews.png)
- [Memories](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/05-memories.png)
- [Users](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/06-users.png)
- [Persona](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/artifacts/pages_acceptance/20260519T230500Z-direct-open-admin-full/07-persona.png)

### 已确认通过

- 直开页 bridge stub 可正常连到 fixture backend
- `Scheduler Diagnostics` 是 cognition 正文第一块
- cognition 在至少两个 `5s` polling 周期后仍稳定
- 不存在的 `chat_id` 会显示 `暂无 loop state。该 chat 尚未进入 scheduler 跟踪。`
- `表达审核 / 记忆网络 / 用户画像 / 角色切片` 四个管理页都已渲染 `admin_full` seed 数据
- Persona 面板已显示：
  - `Persona ID = fixture-persona`
  - `Cache Key = fixture-persona`
  - `状态 = ready`
  - summary / first-person rewrite / style / shards 全部可见

### 直开页链路负责证明什么

- 插件页自身 HTML/CSS/JS 渲染
- 页面内 tabs、panels、empty state、polling 行为
- scheduler、reviews、memories、users、persona 的真实 seed 数据呈现
- 不依赖 AstrBot 宿主 iframe 时，页面本体是否工作

## 双通道怎么互补

### 宿主页链路解决

- “有没有真的挂进 AstrBot”
- “bridge 有没有接上”
- “用户在 AstrBot 里能不能打开这个插件页”

### 直开页链路解决

- “挂进去之后页面细节对不对”
- “scheduler panel 是不是在对的位置”
- “空状态、轮询和各管理页内容是否真的可见”

## 当前最终判断

基于两条链路的合并结果：

- 当前 AstrMai 管理台主链路可用
- Scheduler Diagnostics、Reviews、Memories、Users、Persona 这几块已经有真实页面证据支撑
- 没有新增已确认的页面逻辑 bug
- 剩余宿主页自动化盲区来自 `@chrome` 对 AstrBot iframe 的能力边界

## 后续建议

如果后面还要继续补页面验收，建议保持这个双通道策略：

1. **宿主页链路** 用来证明 AstrBot 集成挂载还活着
2. **直开页链路** 用来证明插件页细节没有退化

这样即使 iframe 自动化仍受限，我们也不会失去对页面质量的可验证把握。
