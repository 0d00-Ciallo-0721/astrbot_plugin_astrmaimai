# AstrBot 插件页真页面检查

- 时间: 2026-05-19 22:15 (Asia/Shanghai)
- 宿主 URL: `http://127.0.0.1:6185/#/plugin-page/astrmai/admin`
- 浏览器: Codex in-app `@chrome`
- 截图: `01-plugin-page-host.png`

## 结论

- 通过:
  - AstrBot 宿主已启动并可访问
  - `#/plugin-page/astrmai/admin` 可正常打开
  - 宿主页内 iframe 已成功渲染 AstrMai 插件页
  - 截图中可见 `Bridge 已连接`
  - 截图中可见 Dashboard 子页签行, 包含 `主动决策池 Cognition`

- 阻塞:
  - 当前 `@chrome` 运行时无法穿透 AstrBot 的跨进程 iframe 去点击或读取内部节点
  - 直接打开 iframe 内容 URL 时, 页面会停在 `正在连接 AstrBot 页面桥接`, 因为缺少宿主注入的 `AstrBotPluginPage` bridge

## 证据

从宿主截图可直接确认:

- 左侧 AstrBot 导航已进入插件页区域
- 右侧插件页内容已渲染
- 顶部状态显示 `Bridge 已连接`
- 页面主体显示 AstrMai Dashboard 内容
- 可见子页签: `Heartflow`, `主动决策池 Cognition`, `工具链观测 Tools`

## 自动化阻塞细节

宿主页里插件内容挂载在:

- `iframe.plugin-page-frame`

当前 `@chrome` 运行时对该 iframe 的限制表现为:

- 无法用 selector path 进入 iframe 内部
- 无法在宿主页上下文中直接读取 iframe 内部文本

已观察到的运行时错误为:

`Cross-origin or out-of-process iframes are not supported by this runtime selector path`

## 未完成项

以下项目本轮未能在 `@chrome` 中继续自动化完成:

- 切换到 `主动决策池 Cognition`
- 验证 `Scheduler Diagnostics` 是否为 cognition 正文第一块
- 验证 cognition 下 `5s` 自动刷新
- 输入不存在的 `chat_id` 验证 `暂无 loop state`
- 留存 cognition/scheduler 面板截图

## 建议的下一步

若继续坚持使用 AstrBot 宿主页做真页面验收, 建议改用能穿透该 iframe 的自动化方式, 例如:

- 常规 Playwright / Selenium 直接驱动本机浏览器
- 或者人工点入 `主动决策池 Cognition`, 再由 Codex 基于当前可见页面继续截图留档
