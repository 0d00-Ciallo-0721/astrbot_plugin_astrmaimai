# AstrMai 页面验收基线（P8）

## 已纳入的真实证据

- 宿主截图：`01-plugin-page-host.png`
- 验收摘要：`summary.md`
- 结构化结果：`result.json`

## 本轮确认通过

- AstrBot 宿主可访问
- `#/plugin-page/astrmai/admin` 可打开
- 插件页已在 AstrBot 宿主内渲染
- `Bridge 已连接` 可见
- `主动决策池 Cognition` 子页签可见

## 当前未完成项

- `Scheduler Diagnostics` 是否为 cognition 正文第一块
- cognition 下 `5s` 自动刷新是否稳定
- 不存在 `chat_id` 的空 state 提示是否正常

## 未完成原因

当前 Codex in-app `@chrome` 运行时无法穿透 AstrBot 宿主页里的跨进程 `iframe.plugin-page-frame`，报错为：

`Cross-origin or out-of-process iframes are not supported by this runtime selector path`

这属于 **验收工具边界**，不是当前已确认的 AstrMai 页面逻辑 bug。

## 后续双通道验收策略

### A. AstrBot 宿主页验收

用于确认：

- 宿主路由正常
- bridge 已连接
- 插件页被 AstrBot 正常挂载
- 顶层布局正确

### B. 直开内容页验收

用于确认：

- 插件页自身 HTML/CSS/JS 渲染
- seed data 是否能把 cognition/reviews/memories/users/persona 全部撑起来
- 空 state / drill-down / panel 结构是否正确

P8 之后，直开页验收通过开发态 bridge stub + fixture server 完成，不再强依赖 AstrBot 宿主页 iframe 自动化能力。
