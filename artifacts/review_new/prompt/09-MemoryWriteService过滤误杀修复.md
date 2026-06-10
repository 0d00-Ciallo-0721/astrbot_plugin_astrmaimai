# Prompt 09：`MemoryWriteService` 过滤误杀修复

## 任务目标
修复 `MemoryWriteService.should_skip_content()` 对以 `{` 开头内容的误杀问题。

本轮目标：
- 保留对真正噪音/异常文本的拦截
- 不再因为粗暴的前缀规则误丢合法记忆内容

## 必读报告
- `artifacts/review_new/04-模块-M4-记忆与学习.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/memory/services/memory_write_service.py`
- 相关写入调用点
- 相关测试

## 必须完成的修复
1. 重新设计 `{` / ````json` / 异常 token 的过滤策略。
2. 至少覆盖一类“以 `{` 开头但应被正常写入”的文本样例。
3. 如果跳过仍有合理场景，最好让原因可观测，而不是静默丢弃。

## 实施要求
- 不要一刀切取消全部过滤。
- 目标是更精细，而不是彻底放开。

## 验证要求
至少执行：
- memory 写入相关测试
- 新增最小回归：合法 `{...` 文本不再被误杀

## 完成标准
- `should_skip_content()` 不再粗暴误杀正常文本
- 噪音过滤仍然存在合理边界
- 更新：
  - `artifacts/review_new/04-模块-M4-记忆与学习.md`

