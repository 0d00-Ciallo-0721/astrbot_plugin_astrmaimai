# AstrMai 直开页验收（admin_full）

- 时间: 2026-05-19 23:05 (Asia/Shanghai)
- 直开页 URL: `http://127.0.0.1:8766/artifacts/scheduler_fixture/direct_open_plugin_page.html`
- Fixture API: `http://127.0.0.1:8765`
- Fixture profile: `admin_full`
- 浏览器: Codex in-app `@chrome`

## 结论

- 通过:
  - 直开页 bridge stub 可正常登录 fixture backend 并进入插件页
  - `Scheduler Diagnostics` 出现在 cognition 正文第一块
  - cognition 页在至少两个 5s polling 周期后仍稳定渲染
  - 不存在 chat 的 drill-down 可显示 `暂无 loop state。该 chat 尚未进入 scheduler 跟踪。`
  - `表达审核 / 记忆网络 / 用户画像 / 角色切片` 四个管理页都已成功渲染真实 seed 数据

- 过程中顺手收掉的 gap:
  - 直开页最初因为 browser 安全策略拦 `file://`，已切换为 `127.0.0.1:8766` 静态托管
  - `persona` 最初为空，根因是直开页需要插件页专用 `/persona/slices` 语义；fixture server 已补 dev-only 只读桥接路由

## 关键截图

- `01-cognition-scheduler.png`
- `02-cognition-empty-state.png`
- `03-cognition-after-polling.png`
- `04-reviews.png`
- `05-memories.png`
- `06-users.png`
- `07-persona.png`

## 验收要点

### Scheduler / Cognition
- 可见 `Profile=balanced`
- 可见 `Poll Mode=FAST`
- 可见 batch/backpressure JSON
- 可见 chat drill-down 详情
- 空 state 已确认可见

### Reviews
- 待审队列中可见 `expr-1`
- 待审项状态为 `review_pending`

### Memories
- Events / Reflections / Nodes / Jargon 四个子页签框架已渲染
- 首屏 events 中可见多条 fixture memory event

### Users
- 首屏可见 3 个用户画像 seed
- 可见长期记忆点与切片编辑区域

### Persona
- Persona ID = `fixture-persona`
- Cache Key = `fixture-persona`
- 状态 = `ready`
- 核心摘要、第一人称自觉、风格指南、八维切片均可见
