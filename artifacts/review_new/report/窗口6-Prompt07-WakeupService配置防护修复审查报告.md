# 窗口6-Prompt07-WakeupService配置防护修复审查报告

## 审查范围
- `astrmai/proactive/wakeup_service.py`
- `tests/test_proactive_scheduler_refactor.py`
- `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 审查结论
- 本轮深度代码审查未发现新的 P1/P2 级问题。
- `WakeupService` 的配置防护修复已经形成闭环：
  - 运行时不再直接脆弱访问 `self.config.life.*`
  - `config=None`、缺失 `config.life`、以及 `life` 仅有部分字段时都能统一回退到 `LifeConfig` 默认值
  - `build_signal()` / `run_for_chat()` 的既有返回结构和调度行为保持兼容
- 本窗口相关历史遗留已清理完成：
  - 模块报告已追加 Prompt07 修复更新
  - 总报告已移除 `WakeupService` 与 `GroupReplyWaitManager` 的过期未修复表述，并对齐当前优先级

## 已确认修复事实

### 1. `WakeupService` 已收口到统一 `life` 配置入口
- `WakeupService` 新增局部 `life` 配置解析 helper，默认值唯一来源复用 `config.py` 中的 `LifeConfig`
- `build_signal()` 中的 `silence_threshold`、`wakeup_min_energy`、`wakeup_cost`、`wakeup_cooldown` 已改为统一安全读取
- `run_for_chat()` 中从 `signal` 回退到配置的 `wakeup_cost` / `wakeup_cooldown` 也已走同一入口

### 2. 缺配置与缺字段路径行为可预期
- `config=None` 时，`WakeupService` 不再因属性访问抛异常
- `config` 存在但无 `life` 属性时，可继续构造 wakeup signal / intent
- `life` 仅提供部分字段时，已提供字段生效，缺失字段自动回退到 `LifeConfig` 默认值

### 3. 测试覆盖已补齐关键回退路径
- 已保留原有 wakeup 正常路径、quiet hours 阻断路径、guidance 文案路径
- 已新增最小回归测试覆盖：
  - `config=None`
  - `config.life` 缺失
  - `life` 部分字段缺失
  - `signal` 缺少 `wakeup_cost` / `wakeup_cooldown`

### 4. 审查文档状态已对齐
- `05-模块-M5-状态主动性与工作模式.md` 已追加 Prompt07 修复更新
- `13-汇总-AstrMai最终审查报告.md` 已移除 `WakeupService` 和 `GroupReplyWaitManager` 的过期未修复项
- 总报告第二优先级已收敛为仍未修复的 `evolution_manager.py` 运行时问题

## 审查验证

### 已执行测试
- `python -m pytest tests/test_proactive_scheduler_refactor.py -k wakeup`
- `python -m pytest tests/test_infrastructure_settings_refactor.py -k astrmai_config`

### 附加事实核验
- 已复核 `wakeup_service.py` 中新引入的 `_resolve_life_config()` 路径，未发现新的外部接口破坏
- 已搜索 `artifacts/review_new/13-汇总-AstrMai最终审查报告.md` 中与 Prompt07 相关的旧表述并完成对齐
- 已确认窗口 6 报告命名与 `artifacts/review_new/report/` 现有编号规则一致

## 备注
- 测试运行中仍可见既有环境 warning：
  - `after_nonebot_init was never awaited`
- 该 warning 在本轮修改前已存在，不属于 Prompt07 改动引入的问题。

## 最终结论
- 本窗口无历史遗留问题。
- `Prompt07 / WakeupService 配置防护修复` 已通过深度代码审查。
