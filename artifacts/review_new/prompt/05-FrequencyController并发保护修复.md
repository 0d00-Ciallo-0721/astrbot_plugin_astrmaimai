# Prompt 05：`FrequencyController` 并发保护修复

## 任务目标
修复 `FrequencyController` 中“声明了锁但未用于共享状态保护”的并发问题。

本轮只做：
- `_records` 访问保护
- 相关回归测试

不要顺手扩大到其他 state 模块。

## 必读报告
- `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/state/energy/frequency_controller.py`
- 直接调用它的状态更新路径
- 已有相关测试

## 必须完成的修复
1. 核实 `_records_lock` 当前未保护哪些读写路径。
2. 统一 `_records` 的增删改查策略。
3. 补最小回归，证明并发路径下不会再无保护访问共享状态。

## 实施要求
- 不要重写整个类。
- 优先最小接入锁保护。
- 保持现有职责和外部接口不变。

## 验证要求
至少执行：
- 直接相关测试
- 如无现成测试，新增最小并发回归

## 完成标准
- `_records_lock` 不再是摆设
- 关键共享状态访问已有统一保护
- 更新：
  - `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`

