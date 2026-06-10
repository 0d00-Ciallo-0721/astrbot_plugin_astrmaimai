# Prompt 07：`WakeupService` 配置防护修复

## 任务目标
修复 `WakeupService` 直接读取 `self.config.life.*` 的条件性运行时风险。

本轮只处理：
- `config` / `config.life` 缺失时的防护
- 默认值入口统一

不要顺手修改其他 proactive 子系统。

## 必读报告
- `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/proactive/wakeup_service.py`
- 该类的构造与调用点
- 相关测试

## 必须完成的修复
1. 明确 `WakeupService` 对 `config.life` 的契约。
2. 收口“配置缺失时直接属性访问”的风险。
3. 如果已有统一默认值入口，优先复用，不要自创一套配置回退逻辑。

## 验证要求
至少执行：
- 与 `WakeupService` 相关的测试
- 最小空配置 / 缺字段路径验证

## 完成标准
- 不再存在 `self.config.life.*` 的脆弱直接访问
- 缺配置时行为可预期
- 更新：
  - `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`

