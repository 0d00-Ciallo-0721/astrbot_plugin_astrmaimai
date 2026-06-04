# 开发窗口 06：Attention 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r04-attention.md` — 完整发现清单（2🔴 7🟡 3🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复（D6/D7/D20/D21）

## 目标文件
- `astrmai/conversation/attention/gate.py` — AttentionGate 核心（759 行）
- `astrmai/conversation/attention/context_compaction.py` — 上下文压缩（~1999 行单体）
- `astrmai/conversation/attention/compaction_providers.py` — 压缩 Provider
- `astrmai/conversation/attention/group_dialogue_store.py` — 群聊存储

## 依赖
窗口 02（gateway/runtime）+ 窗口 03（state）

---

## 🔴 严重（2 项）

### P6-1：context_compaction.py 潜伏崩溃 — @staticmethod 使用 self
- **文件**：`astrmai/conversation/attention/context_compaction.py:1654`
- **问题**：`_build_summary` 方法被装饰为 `@staticmethod`，但方法体内使用了 `self._segment_to_summary_line`。若被调用会抛 `TypeError`（第一个位置参数 `drained_segments` 被当作 `self`，不是实例）。
- **当前状态**：死代码（未被调用），但属**潜伏崩溃 bug**——任何人后续添加调用都会立刻 crash。
- **最小修复**：
  - 先 `search_content "_build_summary"` 确认无调用方
  - 若确实无调用方：删除此方法（或移除 `@staticmethod` 装饰器，改为普通方法）
  - 若需要保留：移除 `@staticmethod`，使用 `self`

### P6-2：详见 r04-attention.md

---

## 🟡 中等（7 项）

详见 `r04-attention.md`，重点：
- `AttentionGate.__init__` 接受 11 个独立参数（应用 AttentionGateConfig 封装——D44）
- `_handle_fast_wakeup` 魔法数字 `12`/`2`/`3`（D46）
- `trigger_phrases` 含 `"这个"`/`"那个"` 死代码（D19）
- `GroupDialogueStore._build_warm_quotes` O(n) 扫描（D21 已修至 O(1)）
- `compaction_providers.py` session 损坏轮换（D6 已修复）
- `context_compaction.py` 1999 行单体拆分（标记为独立窗口，本轮不拆）

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_attention_gate_refactor.py tests/regression/conversation/ -q
```

## 成功标准
- 🔴 P6-1：潜伏崩溃消除（死代码移除或修复）
- 🔴 2 项全部修复
- Attention 相关测试全部通过
