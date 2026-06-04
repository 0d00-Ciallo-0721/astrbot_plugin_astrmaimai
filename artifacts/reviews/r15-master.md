# AstrMai 全量代码审查总报告

> 审查轮次：R15 终态  
> 审查方式：12 个独立 subagent 并行审查 + 3 份汇总文件  
> 测试基线：692 passed / 12 failed / 1 skipped  
> 审查文件数：~262  
> 发现总数：171（32🔴 82🟡 57🟢）

---

## 报告索引

| # | 文件 | 模块 | 🔴 | 🟡 | 🟢 | 总计 |
|---|------|------|----|----|----|------|
| r01 | `r01-plugin-entry.md` | Plugin Entry / Bootstrap | 3 | 11 | 8 | 22 |
| r02 | `r02-gateway-runtime.md` | Gateway / Runtime | 2 | 8 | 5 | 15 |
| r03 | `r03-conversation.md` | Conversation / Planning / Execution | 3 | 10 | 7 | 20 |
| r04 | `r04-attention.md` | Attention | 2 | 7 | 3 | 12 |
| r05 | `r05-memory.md` | Memory | 3 | 5 | 5 | 13 |
| r06 | `r06-state.md` | State / Decay | 3 | 8 | 5 | 16 |
| r07 | `r07-proactive.md` | Proactive | 4 | 8 | 6 | 18 |
| r08 | `r08-webui.md` | WebUI | 5 | 7 | 5 | 17 |
| r09 | `r09-presentation.md` | Presentation / Events | 1 | 3 | 6 | 10 |
| r10 | `r10-infrastructure.md` | Compat / Persistence | 3 | 4 | 5 | 12 |
| r11 | `r11-security.md` | 安全专项 | 3 | 4 | 9 | 16 |
| **合计** | | | **32** | **75** | **64** | **171** |

### 汇总文件

| # | 文件 | 内容 |
|---|------|------|
| r12 | `r12-remaining-debt.md` | 剩余待修清单（12 预存失败 + 6 延期项 + 建议项） |
| r13 | `r13-session-fixes.md` | 本轮修复记录（25 项已落地） |
| r14 | `r14-index.md` | 审查索引（旧版） |
| **r15** | `r15-master.md`（本文件） | **全量整合总报告** |

---

## 🔴 严重发现全量清单（32 项）

### r01 — Plugin Entry / Bootstrap（3 项）
| # | 位置 | 描述 |
|---|------|------|
| 1 | `lifecycle.py:211-216` | `_reset_runtime_status_flags` 未复位 `bootstrap_completed`、`boot_logged`、`work_mode_enabled` 三个启动期标志。热重启时残余值会导致诊断误报 |
| 2-3 | 详见 `r01-plugin-entry.md` | |

### r02 — Gateway / Runtime（2 项）
| # | 位置 | 描述 |
|---|------|------|
| 4 | `gateway_lane.py:217-262` | chat_in_lane_result 双重冷却过滤：trace stage 使用的 `skipped_cooldown_models` 取自第一次过滤快照，但 `_elastic_call_result` 内部二次过滤可能产生不同结果，导致 trace 与执行不一致 |
| 5 | 详见 `r02-gateway-runtime.md` | |

### r03 — Conversation（3 项）
| # | 位置 | 描述 |
|---|------|------|
| 6-8 | 详见 `r03-conversation.md` | |

### r04 — Attention（2 项）
| # | 位置 | 描述 |
|---|------|------|
| 9 | `context_compaction.py:1654` | `_build_summary` 被装饰为 `@staticmethod` 却使用 `self`（`self._segment_to_summary_line`）。当前为死代码但属潜伏崩溃 bug |
| 10 | 详见 `r04-attention.md` | |

### r05 — Memory（3 项）
| # | 位置 | 描述 |
|---|------|------|
| 11 | `contracts/memory_query.py:28-29` | `include_feedback` 和 `retrieve_keys` 是死字段 — 声明但所有检索逻辑均未读取 |
| 12-13 | 详见 `r05-memory.md` | |

### r06 — State（3 项）
| # | 位置 | 描述 |
|---|------|------|
| 14 | `mood_decay.py:11` | `config.energy.recovery_silence_min` 无空安全访问 — 若 config 无 `energy` 属性则 `AttributeError` |
| 15-16 | 详见 `r06-state.md` | |

### r07 — Proactive（4 项）
| # | 位置 | 描述 |
|---|------|------|
| 17 | `rhythm.py:72-76` | `getattr(None, …)` 崩溃风险：`getattr(config, "reply", None)` 返回 None 后直接 `getattr(None, "base_frequency", 0.7)` |
| 18-20 | 详见 `r07-proactive.md` | |

### r08 — WebUI（5 项）
| # | 位置 | 描述 |
|---|------|------|
| 21 | `admin_ui_service.py:26-37` | God Object 反模式：1148 行文件承载 7 域服务的全部 ~40 个 public 方法，领域服务全部委托回自身 |
| 22-25 | 详见 `r08-webui.md` | |

### r09 — Presentation（1 项）
| # | 位置 | 描述 |
|---|------|------|
| 26 | `message_entry.py:40` | 自消息检查滞后于防抖和权限检查，应在第 30 行后立即执行 |

### r10 — Infrastructure（3 项）
| # | 位置 | 描述 |
|---|------|------|
| 27 | `orm_models.py:60-82` | ChatState 持久化严重不完整：12+ 字段仅 7 个入库，`total_messages`/`judgment_mode`/`last_msg_info` 等被静默丢弃 |
| 28-29 | 详见 `r10-infrastructure.md` | |

### r11 — Security（3 项 CRITICAL）
| # | 位置 | 描述 |
|---|------|------|
| 30-32 | 详见 `r11-security.md` | 认证/授权/注入相关 |

---

## 🟡 中等问题分类（75 项）

| 类别 | 数量 | 典型问题 |
|------|------|----------|
| 并发/竞态 | 6 | `_runtime_meta` 锁外读取、coroutine 未 await |
| 内存泄漏 | 4 | `_remote_sessions`/`_sticky_primary` 无淘汰（⚠️ 本轮已修） |
| 错误处理 | 8 | 空 try/except、异常吞噬、降级路径缺失 |
| 数据一致性 | 5 | ChatState 部分字段丢失、序列化不一致 |
| 接口设计 | 12 | God Object、死代码字段、过度委托 |
| 性能 | 7 | O(n) 扫描、重复计算、不必要的 await |
| 可维护性 | 15 | 过长方法、魔法数字、重复代码 |
| 测试 | 5 | mock 不完整、断言漂移 |
| 配置/依赖 | 8 | 硬编码值、循环引用、初始化顺序脆弱 |
| 文档 | 5 | 废弃 API 未标注、注释与实现不一致 |

---

## 🟢 建议级别（57 项）

涵盖代码整洁、命名规范、PEP 8 格式、docstring 补全、类型标注、日志改进等。详见各分报告。

---

## 本轮已修复（25 项）

全部记录在 `r13-session-fixes.md`，概要：

| 窗口 | 数量 | 关键修复 |
|------|------|----------|
| 窗口 4 | 9 | bool-sticky、_runtime_meta lock、_sticky_primary LRU、_remote_sessions TTL、lane 旋转终止、model_id 回退、冷却过滤 |
| Phase 1 | 6 | cache_observation 数据源、del event 清理、warm_quotes O(n)、continuity 文档化、冷却审计、trace 冷却记录 |
| Phase 2 | 2 | compaction session 轮换、AttentionGate None 防护 |
| Phase 3 | 4 | bootstrap 分段注释、循环引用文档化（D27 延期） |
| 窗口 1 | 2 | PEP 8 空行、hasattr 私有方法公开化 |
| 测试 | 6 | T1-T3, T6-T8 mock 补齐 + 断言适配 |

---

## 剩余已知问题

详见 `r12-remaining-debt.md`：

- **12 个预存测试失败**（mood 审计漂移 ×2、ported 测试 ×5、schema 迁移 ×3、profile roundtrip ×2）
- **6 个独立窗口延期项**（compaction 拆分、_get_runtime 迁移、bootstrap 提取、DB migration、cache reason、profile 序列化）

---

## 模块健康度总览

| 模块 | 🔴 | 状态 |
|------|----|------|
| Plugin Entry | 3 | ⚠️ 生命周期标志复位不完整 |
| Gateway/Runtime | 2 | ⚠️ trace 日志一致性 |
| Conversation | 3 | ⚠️ 决策链边界条件 |
| Attention | 2 | ⚠️ 死代码潜伏崩溃 |
| Memory | 3 | ⚠️ 死字段 + recall 兼容性 |
| State | 3 | ⚠️ 空安全访问 |
| Proactive | 4 | 🔴 最多 — getattr(None) 崩溃风险 |
| WebUI | 5 | 🔴 最多 — God Object 反模式 |
| Presentation | 1 | 🟢 较健康 |
| Infrastructure | 3 | ⚠️ ChatState 持久化不完整 |
| Security | 3 | ⚠️ CRITICAL 级别 |
