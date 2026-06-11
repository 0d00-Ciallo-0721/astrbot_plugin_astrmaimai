# 窗口4-Prompt05-FrequencyController并发保护修复审查报告

## 审查结论

- 本轮深度代码审查未发现新的 P1/P2 级问题。
- `FrequencyController` 的 `_records` 共享状态访问已完成统一锁保护，本窗口对应问题已收口。
- 与本窗口直接相关的历史遗留项已清理完成：
  - 模块报告与总报告关于 `FrequencyController` 的状态表述已对齐
  - `artifacts/review_new/report` 中的窗口编号漂移已纠正
- 针对本窗口的改动范围，当前可判定为 **窗口4 历史遗留问题已清零**。

## 审查范围

- `astrmai/state/energy/frequency_controller.py`
- `tests/unit/state/test_state_subservices_migrated.py`
- `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`
- `artifacts/review_new/report/`

## 关键核验结果

### 1. 并发保护已真实落地

- `_records_lock` 已从无法在同步路径中直接生效的 `asyncio.Lock()` 收口为同步可用的 `threading.RLock()`
- `should_reply()`、`on_message_received()`、`_get_record()`、`cleanup_inactive()` 对 `_records` 的增删改查已统一纳入锁保护
- `ChatReplyRecord.reply_timestamps` 的追加与裁剪不再暴露于无保护并发访问

### 2. 外部行为与接口保持稳定

- `FrequencyController` 对外仍保持同步接口：
  - `should_reply()`
  - `on_message_received()`
  - `cleanup_inactive()`
- 未引入新的配置项
- 未修改上游接线或调用方式
- 概率计算、mention 豁免、日志语义与返回值语义保持不变

### 3. 并发回归覆盖有效

- 已保留原有基础行为测试
- 已新增同一 `chat_id` 并发创建/读取回归，确认只保留稳定记录对象
- 已新增并发更新与清理交叉回归，确认不会出现字典迭代/删除冲突或非法记录结构

### 4. 文档与报告状态已收口

- `05-模块-M5-状态主动性与工作模式.md` 已明确标注 `FrequencyController` finding 为已修复
- `13-汇总-AstrMai最终审查报告.md` 已移除 `FrequencyController` 的过期未修复表述，并同步调整优先级建议
- `report` 目录中原“窗口0内容、窗口1文件名”的漂移已纠正回一致状态

## 验证记录

已通过：

- `python -m pytest tests/unit/state/test_state_subservices_migrated.py -q`
- `python -m pytest tests/unit/state tests/regression/state -q`

本轮额外复核：

- 搜索 `artifacts/review_new` 中所有 `FrequencyController` / `_records_lock` 残留表述，确认与当前实现状态一致
- 复核 `report` 目录现有窗口命名，确认本窗口写入前不存在新的未处理命名冲突

## 备注

- 测试输出中的 `pkg_resources` / `jieba` / `after_nonebot_init` warnings 为既有环境噪声，不属于本窗口改动引入的问题。
- 本报告只覆盖 Prompt05 / 窗口4 的并发保护修复与相关报告收口，不扩展评价其他尚未修复的模块问题。
