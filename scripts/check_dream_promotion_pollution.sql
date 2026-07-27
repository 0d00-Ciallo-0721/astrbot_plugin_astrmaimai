-- ML-08 取证 SQL：核查 Dream 晋升是否写入过「同 turn 重复证据」的高权威事实
--
-- 背景（.agent/claude-full-audit-20260727/findings.json / ML-08，OPT-15）：
--   修复前 promotion_engine._iter_detected_facts 不按 turn 去重，单次 LLM 响应把同一条
--   (subject, entity, attribute, value) 重复 3 遍就能凑满 PROMOTION_THRESHOLD，
--   随后以 confidence=1.0 + source='dream_audit_pipeline' 写入。该 source 在
--   v2_store._looks_like_authority_eav 的白名单里，可以 supersede 用户亲述的事实。
--   代码侧已在 OPT-15 修复（按 (key, turn_id) 去重 + confidence 降为 0.95），
--   但**修复前已经落库的污染事实不会自动消失**，需要在真实库上采样定级。
--
-- 用法（只读，不写不删）：
--   sqlite3 <你的 astrmai 数据库.db> ".read scripts/check_dream_promotion_pollution.sql"
--   需要 SQLite 3.38+（用到 JSON1 的 json_each / ->>）。
--   本脚本**不由 CI 或插件自动执行**，只在有真实库的环境手工采样。
--
-- 判读：
--   Q2 命中行 = 疑似污染（证据条数 >= 3 但去重后 turn 数 < 3）。
--   命中后再看 Q4 的 supersede 影响面，决定是否需要一次性清理脚本（单独评审）。

.headers on
.mode column
.width 24 10 8 8 10 40

-- ── Q1 总量与置信度分布：确认这个库里到底有没有 Dream 晋升产物 ──────────────
SELECT
    '[Q1] dream 晋升总览' AS section,
    COUNT(*)                                        AS total_rows,
    SUM(status = 'active')                          AS active_rows,
    SUM(confidence >= 1.0)                          AS confidence_1_0,   -- 修复前的硬编码值
    SUM(confidence >= 0.95 AND confidence < 1.0)    AS confidence_0_95,  -- 修复后的取值
    MIN(datetime(create_time, 'unixepoch', 'localtime')) AS earliest,
    MAX(datetime(create_time, 'unixepoch', 'localtime')) AS latest
FROM canonical_memories
WHERE source = 'dream_audit_pipeline';

-- ── Q2 核心判据：证据够 3 条，但去重后的 turn 不够 3 个 ──────────────────────
-- 这正是 ML-08 描述的伪造阈值形态：一次响应里同一事实重复计数。
SELECT
    '[Q2] 疑似同 turn 重复凑阈值' AS section,
    m.id,
    m.confidence,
    m.importance,
    m.status,
    datetime(m.create_time, 'unixepoch', 'localtime') AS created_at,
    ev.evidence_count,
    ev.distinct_turns,
    substr(m.content, 1, 60) AS content_head
FROM canonical_memories AS m
JOIN (
    SELECT
        c.id                                        AS mid,
        COUNT(*)                                    AS evidence_count,
        COUNT(DISTINCT NULLIF(t.value ->> 'turn_id', '')) AS distinct_turns
    FROM canonical_memories AS c,
         json_each(json_extract(c.metadata, '$.evidence_turns')) AS t
    WHERE c.source = 'dream_audit_pipeline'
      AND json_valid(c.metadata)
      AND json_type(c.metadata, '$.evidence_turns') = 'array'
    GROUP BY c.id
) AS ev ON ev.mid = m.id
WHERE ev.evidence_count >= 3
  AND ev.distinct_turns < 3          -- 空 turn_id 也会落到这里，属于同样不可信的证据
ORDER BY m.create_time DESC
LIMIT 50;

-- ── Q3 空证据链：连 evidence_turns 都没有的晋升事实（无法自证来源）────────────
SELECT
    '[Q3] 缺证据链' AS section,
    id,
    confidence,
    status,
    datetime(create_time, 'unixepoch', 'localtime') AS created_at,
    substr(content, 1, 60) AS content_head
FROM canonical_memories
WHERE source = 'dream_audit_pipeline'
  AND (
        NOT json_valid(metadata)
     OR json_type(metadata, '$.evidence_turns') IS NULL
     OR json_array_length(json_extract(metadata, '$.evidence_turns')) = 0
  )
ORDER BY create_time DESC
LIMIT 50;

-- ── Q4 影响面：被 Dream 事实 supersede 掉的、来自用户亲述的记忆 ───────────────
-- 这是 ML-08 真正的用户可感后果——机器推断覆盖了本人说过的话。
SELECT
    '[Q4] 被 dream 覆盖的用户亲述' AS section,
    victim.id                AS superseded_id,
    victim.source            AS victim_source,
    victim.confidence        AS victim_confidence,
    datetime(victim.update_time, 'unixepoch', 'localtime') AS superseded_at,
    substr(victim.content, 1, 50) AS victim_content,
    substr(winner.content, 1, 50) AS dream_content
FROM canonical_memories AS victim
JOIN canonical_memories AS winner ON winner.id = victim.superseded_by
WHERE winner.source = 'dream_audit_pipeline'
  AND victim.source <> 'dream_audit_pipeline'
ORDER BY victim.update_time DESC
LIMIT 50;

-- ── Q5 结论口径 ────────────────────────────────────────────────────────────
-- Q2/Q3 均为 0 行  → ML-08 在本库无实证污染，保持「代码已修 + 无需清理」。
-- Q2 有行、Q4 为 0 → 有伪造阈值的产物但未覆盖用户亲述，可降级为观察项。
-- Q2 且 Q4 有行    → 需要一次性清理脚本（恢复 victim.status、清空 superseded_by），
--                    该脚本必须单独评审后执行，不在本文件内提供。
