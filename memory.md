## 当前系统现状（2026-05-15 校准）

| 组件 | 当前真实状态 | 说明 |
|------|--------------|------|
| [pump_memory_reflection()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/summarizer.py#L517-L541) | 已接入即时记忆闸门 | 规则优先写 `instant_gate`，并保留 `instant_gate_llm` 低频补漏路径 |
| [_retrieve_once()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_retrieval_service.py#L135-L153) | 已 always-parallel | canonical 与 hybrid 始终并行，再统一 `_fuse_candidates()` 排序 |
| [v2_store.search()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/v2_store.py#L416-L533) | 已 FTS5 优先 | FTS5 为主路径，非 FTS 场景才回退到基础 overlap 检索 |
| [DEFAULT_MEMORY_SCORING](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_scoring.py#L6-L21) | 已统一收口 | retrieval / store / context builder 共用同一套评分配置 |
| [ReActRetriever](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/retrieval/react_retriever.py) | 已受益于统一检索融合 | 继续走 `retrieval_service.retrieve_deep()`，无需单独再造旁路 |

### 已完成项

- 即时记忆主链已落地：`_try_instant_memorize()` 先走规则，未命中时可走 LLM backfill。
- 检索主链已落地：`_retrieve_once()` 不再短路，统一并行 canonical + hybrid。
- FTS5 主路径已落地：`v2_store.search()` 已具备全文检索与索引同步。
- 评分收口已落地：`MemoryScoringConfig` 已被 retrieval / store / context builder 使用。

### 未完成收尾项

- 即时记忆 LLM backfill 仍需继续保证与真实运行时 `think_level` 信号保持一致。
- `instant_gate_llm` 需要直接单测覆盖，避免后续只测到规则直写而漏掉补漏链。
- 本文档需要持续作为“当前真实状态”维护，不再保留已失效的旧现状判断。

### 验证结果

- `python -m unittest tests.unit.memory.test_memory_v2_services tests.regression.memory.test_memory_v2_tool_injection tests.regression.memory.test_react_retriever_traces_migrated -q`
  - 当前通过
- `pytest` 下同组 targeted memory suite 在禁用自动插件加载后通过，用于补充确认 v2 retrieval / tool injection / react retriever 行为

> [!NOTE]
> 下方阶段 A / B / C 的细化设计继续保留，作为实现参考与历史记录；若与本节“当前系统现状”冲突，以本节为准。

---
# AstrMai 璁板繂绯荤粺浼樺寲 v2 鈥?涓夐樁娈靛疄鏂芥柟妗?
鍩轰簬 2026-05-14 瀵逛粨搴撶湡瀹炰唬鐮佺殑瀹屾暣瀹¤锛岃仛鐒︿笁鏉′粛鐒舵垚绔嬬殑鏍稿績涓荤嚎銆?
> [!IMPORTANT]
> 鏈柟妗堝簾寮冩棫鐗?`implementation_plan.md`锛屾寜褰撳墠浠ｇ爜瀹炲喌閲嶆柊鎺掍紭鍏堢骇銆?> 鏃ф枃妗ｄ腑"BM25 骞界伒璁板綍"闂宸茶 `_retrieve_once()` 鐨?canonical 鍥炶〃鍏滃簳閮ㄥ垎缂撹В锛岄檷绾т负闃舵 B 鐨勯檮甯︽敹鐩娿€?
---

## 褰撳墠绯荤粺鐜扮姸閫熻

| 缁勪欢 | 鐜扮姸 | 鏍稿績闂 |
|------|------|----------|
| [pump_memory_reflection()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/summarizer.py#L401-L476) | buffer 绱Н `threshold*2` 鏉℃墠瑙﹀彂 | 鏃犲嵆鏃堕噸瑕佹€ч椄闂紝鍏抽敭淇℃伅琚饭娌?|
| [_retrieve_once()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_retrieval_service.py#L106-L183) | store.search 澶熶簡鐩存帴鐭矾 | 闈?always-parallel锛屽彲鑳介敊杩囧悜閲忓眰鏇翠紭缁撴灉 |
| [v2_store.search()](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/v2_store.py#L369-L463) | `term in haystack` 瀛愪覆鍖归厤 | 涓枃鏃犲垎璇嶃€佹棤 BM25 鏉冮噸銆佹棤 FTS5 |
| 鎺掑簭鏉冮噸 | 涓夊纭紪鐮佸井鏈夊樊寮?| v2_store(0.2) vs context_builder(0.25) |
| [ReActRetriever](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/retrieval/react_retriever.py) | 宸茶蛋 `retrieval_service.retrieve_deep()` | 鉁?宸查儴鍒嗗榻愶紝铻嶅悎鍗囩骇鍚庤嚜鍔ㄥ彈鐩?|

---

## 闃舵 A锛氫富鍔ㄨ蹇嗛棴鐜紙P0锛?
### 鐩爣

鍦ㄦ瘡杞璇濈粨鏉熸椂锛岄€氳繃**瑙勫垯闂搁棬**鍗虫椂鎹曡幏楂樹环鍊间俊鎭洿鍐?canonical锛屼笉鍐嶇瓑 buffer 绱Н銆?
### 璁捐鍘熷垯

**瑙勫垯涓绘帶锛孡LM 鍋氬寮鸿ˉ婕?*鈥斺€斾笌鏁翠釜 Memory v2 "纭畾鎬х瓥鐣ヤ紭鍏?鐨勮矾绾夸竴鑷淬€?
### 瑙勫垯闂搁棬鍒嗙被

| 绫诲埆 | 鍖归厤绛栫暐 | 绀轰緥 |
|------|---------|------|
| 韬唤澹版槑 | 姝ｅ垯 `鎴?鍙玕|鏄痋|鍚嶅瓧)` | "鎴戝彨灏忔槑" |
| 鑱旂郴鏂瑰紡 | 姝ｅ垯 鎵嬫満/閭/寰俊鍙?| "鎴戞墜鏈哄彿鏄?38xxx" |
| 绋冲畾鍋忓ソ | 鍏抽敭璇?`鍠滄/璁ㄥ帉/鏈€鐖?涓嶅悆` | "鎴戜笉鍚冮鑿? |
| 鍏崇郴鍙樺寲 | 鍏抽敭璇?`鐢锋湅鍙?濂虫湅鍙?鍒嗘墜/缁撳` | "鎴戞槰澶╁垎鎵嬩簡" |
| 寮烘儏缁?閲嶅ぇ浜嬩欢 | 鎯呮劅寮鸿瘝 + 浜嬩欢璇?| "鎴戠埜浣忛櫌浜? |
| 鏄惧紡鎸囦护 | 鍏抽敭璇?`璁颁綇/鍒繕浜?璁颁笅鏉 | "甯垜璁颁綇杩欎釜鍦板潃" |

### LLM 澧炲己灞傦紙浣庨瑙﹀彂锛?
- 浠呭湪瑙勫垯鏈懡涓?**涓?* `think_level >= 2` 鎴栧璇濊疆鏁?`>= 5` 鏃跺惎鐢?- 璋冪敤 `call_data_process_task`锛宲rompt 鏋佺畝锛歚"杩欒疆瀵硅瘽鏄惁鏈夊€煎緱闀挎湡璁颁綇鐨?鏉″叧閿簨瀹烇紵杩斿洖JSON {\"worth\": bool, \"fact\": \"...\"}"`
- 涓嶅仛鍐崇瓥锛屽彧鍋氳ˉ婕忓缓璁?
---

### 娑夊強鏂囦欢

#### [MODIFY] [summarizer.py](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/summarizer.py)

**鏀瑰姩 1**锛歚pump_memory_reflection()` 鍦?buffer 绱Н閫昏緫**涔嬪墠**鎻掑叆鍗虫椂闂搁棬

```python
async def pump_memory_reflection(self, chat_id, user_msg, ai_msg):
    if not ai_msg: return
    if ai_msg.strip().startswith('{') or ai_msg.strip().startswith('```json'):
        return

    # 馃啎 闃舵 A锛氬嵆鏃堕噸瑕佹€ч椄闂紙鍦?buffer 绱Н涔嬪墠鎵ц锛?    await self._try_instant_memorize(chat_id, user_msg, ai_msg)

    # 鍘熸湁 buffer 绱Н閫昏緫淇濇寔涓嶅彉...
    lock = self._get_memory_lock(chat_id)
    async with lock:
        ...  # 鐜版湁浠ｇ爜涓嶅姩
```

**鏀瑰姩 2**锛氭柊澧?`_try_instant_memorize()` 鏂规硶

```python
async def _try_instant_memorize(self, chat_id: str, user_msg: str, ai_msg: str):
    """瑙勫垯浼樺厛鐨勫嵆鏃惰蹇嗛椄闂?""
    matched = self._rule_gate_match(user_msg)
    if not matched:
        return
    
    category, extracted = matched
    content = f"[鍗虫椂璁板繂|{category}] 鐢ㄦ埛璇达細{user_msg}"
    
    if hasattr(self.engine, "write_service"):
        from ..contracts.memory_query import MemoryWriteRequest
        await self.engine.write_service.write(MemoryWriteRequest(
            source="instant_gate",
            kind="fact",
            session_id=str(chat_id),
            content=content,
            summary=extracted[:240],
            importance=0.85,
            confidence=0.9,
            metadata={"gate_category": category, "instant_write": True},
            dedup_key=f"instant_gate:{chat_id}:{category}:{extracted[:60]}",
        ))
```

**鏀瑰姩 3**锛氭柊澧?`_rule_gate_match()` 瑙勫垯寮曟搸

```python
import re

# 缂栬瘧涓€娆★紝绫荤骇鍒父閲?_INSTANT_PATTERNS = [
    ("identity",     re.compile(r"鎴??:鍙珅鏄瘄鍚嶅瓧(?:鏄瘄鍙?)\s*(\S{1,20})")),
    ("contact",      re.compile(r"(?:鎵嬫満|鐢佃瘽|寰俊|QQ|閭)[鍙风爜]*\s*[:锛歖?\s*(\S{5,30})")),
    ("preference",   re.compile(r"鎴??:鍠滄|璁ㄥ帉|鏈€鐖眧涓嶅悆|涓嶅枩娆鍋忓ソ)\s*(.{2,40})")),
    ("relationship", re.compile(r"(?:鐢锋湅鍙媩濂虫湅鍙媩鑰佸叕|鑰佸﹩|鍒嗘墜|缁撳|绂诲|鎭嬬埍)")),
    ("major_event",  re.compile(r"(?:浣忛櫌|鍘讳笘|姣曚笟|鍏ヨ亴|杈炶亴|鎼|鎬€瀛晐鐢熶簡)")),
    ("explicit_cmd", re.compile(r"(?:璁颁綇|鍒繕浜唡璁颁笅鏉甯垜璁皘浣犺璁板緱)")),
]

def _rule_gate_match(self, user_msg: str) -> tuple[str, str] | None:
    text = str(user_msg or "").strip()
    if len(text) < 4:
        return None
    for category, pattern in self._INSTANT_PATTERNS:
        m = pattern.search(text)
        if m:
            extracted = m.group(1) if m.lastindex else m.group(0)
            return (category, extracted.strip())
    return None
```

> [!NOTE]
> 鍗虫椂闂搁棬鍙啓 canonical锛堥€氳繃 `write_service.write()`锛夛紝涓嶆柊澧炴梺璺瓨鍌ㄣ€?> `MemoryWriteService` 鍐呴儴鐨?dedup_key 鏈哄埗澶╃劧闃查噸澶嶃€?
---

## 闃舵 B锛氱粺涓€妫€绱㈣瀺鍚堬紙P0锛?
### 鐩爣

灏?`_retrieve_once()` 浠?canonical 澶熶簡灏辩煭璺?閲嶆瀯涓?**always-parallel + unified scoring**锛屾墍鏈夋秷璐规柟锛坕njection / tool / ReAct锛夎嚜鍔ㄥ叡浜敹鐩娿€?
### 鏍稿績鏀瑰姩

#### [MODIFY] [memory_retrieval_service.py](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_retrieval_service.py)

**鏀瑰姩 1**锛氶噸鏋?`_retrieve_once()` 鈥?濮嬬粓骞惰 + 缁熶竴璇勫垎

```python
import asyncio

async def _retrieve_once(self, query: MemoryQuery) -> list[MemoryCandidate]:
    visibility_mode = str(query.metadata.get("visibility_mode") or "")
    query_layers = {str(item) for item in query.layers or [] if str(item).strip()}

    # 鐗瑰寲绛栫暐璺緞淇濇寔涓嶅彉
    if query.intent == "jargon" or query_layers == {"jargon"}:
        return await self.jargon_policy.search(...)
    if query.intent == "expression_pattern" or query_layers == {"expression_pattern"}:
        return await self.expression_pattern_policy.search(...)

    # 馃啎 濮嬬粓骞惰鎵ц涓よ矾妫€绱?    canonical_task = self.store.search(
        query.query, session_id=query.session_id, persona_id=query.persona_id,
        layers=query.layers, top_k=query.top_k,
        exclude_ids=query.exclude_ids, allow_stale=query.allow_stale,
        visibility_mode=visibility_mode,
    )
    hybrid_task = self._hybrid_search(query, visibility_mode)

    canonical_results, hybrid_results = await asyncio.gather(
        canonical_task, hybrid_task, return_exceptions=True
    )
    if isinstance(canonical_results, Exception):
        canonical_results = []
    if isinstance(hybrid_results, Exception):
        hybrid_results = []

    # 馃啎 缁熶竴铻嶅悎
    return self._fuse_candidates(
        canonical_results, hybrid_results, query
    )
```

**鏀瑰姩 2**锛氭娊鍙?`_hybrid_search()` 鍖呰鏃х殑 engine 璋冪敤

```python
async def _hybrid_search(self, query: MemoryQuery, visibility_mode: str) -> list[MemoryCandidate]:
    """鍖呰 engine._search_memories锛岀粨鏋滅粡杩?canonical 鍥炶〃楠岃瘉"""
    if not self.engine or not hasattr(self.engine, "_search_memories"):
        return []
    try:
        session_id = "__self_lore__" if query.include_persona_lore or "persona_lore" in query.layers else query.session_id
        results = await self.engine._search_memories(
            query.query, top_k=max(int(query.top_k or 5), 1),
            session_id=session_id, persona_id=query.persona_id or None,
        )
    except Exception:
        return []

    candidates = []
    excluded = set(query.exclude_ids or [])
    for result in results:
        candidate = self._result_to_candidate(result, query)
        # canonical 鍥炶〃楠岃瘉锛堜繚鐣欑幇鏈夐€昏緫锛?        canonical_id = str(candidate.metadata.get("canonical_id") or candidate.id or "")
        if canonical_id and not canonical_id.startswith("idx_"):
            canonical = await self.store.get_by_id(canonical_id, allow_stale=query.allow_stale)
            if not canonical:
                continue
            canonical.relevance_score = max(candidate.relevance_score, canonical.relevance_score)
            candidate = canonical
        if candidate.id in excluded:
            continue
        if candidate.status in {"deleted", "merged", "deprecated", "review_pending", "rejected"}:
            continue
        if candidate.status == "stale" and not query.allow_stale:
            continue
        if visibility_mode == "auto" and candidate.visibility != "auto_and_tool":
            continue
        if visibility_mode == "tool" and candidate.visibility not in {"auto_and_tool", "tool_only"}:
            continue
        if query.layers and candidate.kind not in set(query.layers):
            continue
        candidates.append(candidate)
    return candidates
```

**鏀瑰姩 3**锛氭柊澧?`_fuse_candidates()` 缁熶竴璇勫垎

```python
def _fuse_candidates(
    self,
    canonical: list[MemoryCandidate],
    hybrid: list[MemoryCandidate],
    query: MemoryQuery,
) -> list[MemoryCandidate]:
    """鍙岃矾缁撴灉缁熶竴鍘婚噸 + 褰掍竴鍖栬瘎鍒?""
    merged: dict[str, MemoryCandidate] = {}
    # canonical 璺粨鏋?    for c in canonical:
        merged[c.id] = c
        c.metadata["_canon_score"] = c.relevance_score
        c.metadata["_hybrid_score"] = 0.0
    # hybrid 璺粨鏋滐細濡傛灉 id 宸插瓨鍦ㄥ垯铻嶅悎锛屽惁鍒欐柊澧?    for h in hybrid:
        if h.id in merged:
            existing = merged[h.id]
            existing.metadata["_hybrid_score"] = max(
                existing.metadata.get("_hybrid_score", 0.0),
                h.relevance_score,
            )
        else:
            h.metadata["_canon_score"] = 0.0
            h.metadata["_hybrid_score"] = h.relevance_score
            merged[h.id] = h

    # 缁熶竴鎺掑簭锛堟潈閲嶄粠 MemoryScoringConfig 璇诲彇锛岄樁娈?C 鎶藉彇鍚庢浛鎹級
    for c in merged.values():
        canon = float(c.metadata.get("_canon_score", 0.0))
        hybrid_s = float(c.metadata.get("_hybrid_score", 0.0))
        c.relevance_score = (
            canon * 0.25
            + hybrid_s * 0.45
            + c.importance * 0.15
            + c.recency_score * 0.1
            + c.confidence * 0.05
            - (0.2 if c.status == "stale" else 0.0)
        )
    ranked = sorted(merged.values(), key=lambda x: x.relevance_score, reverse=True)
    return ranked[:max(int(query.top_k or 5), 1)]
```

> [!NOTE]
> **ReActRetriever 鏃犻渶鍗曠嫭鏀瑰姩**鈥斺€斿畠璋冪敤 `retrieval_service.retrieve_deep()`锛?> `retrieve_deep()` 鍐呴儴璧?`_retrieve_queries()` 鈫?`_retrieve_once()`锛岃嚜鍔ㄧ户鎵跨粺涓€铻嶅悎銆?
---

## 闃舵 C锛欶allback 妫€绱㈠崌绾?+ 璇勫垎缁熶竴锛圥1锛?
### C-1锛歷2_store.search() FTS5 鍗囩骇

#### [MODIFY] [v2_store.py](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/v2_store.py)

**鏀瑰姩 1**锛歚initialize()` 涓垱寤?FTS5 铏氭嫙琛?
```python
async def initialize(self) -> None:
    ...
    async with aiosqlite.connect(self.db_path) as db:
        # 鐜版湁琛ㄥ垱寤轰笉鍙?..
        
        # 馃啎 FTS5 鍏ㄦ枃绱㈠紩
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS canonical_fts
            USING fts5(
                memory_id UNINDEXED,
                content,
                summary,
                tags,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        await db.commit()
    self._initialized = True
```

**鏀瑰姩 2**锛氭柊澧?`_sync_fts()` 鍦?upsert / soft_delete 鏃跺悓姝?
```python
async def _sync_fts(self, db, memory_id: str, *, delete_only: bool = False):
    """鍚屾 FTS5 绱㈠紩"""
    await db.execute("DELETE FROM canonical_fts WHERE memory_id = ?", (memory_id,))
    if delete_only:
        return
    cursor = await db.execute(
        "SELECT content, summary, tags FROM canonical_memories WHERE id = ? AND status = 'active'",
        (memory_id,),
    )
    row = await cursor.fetchone()
    if row:
        await db.execute(
            "INSERT INTO canonical_fts(memory_id, content, summary, tags) VALUES (?, ?, ?, ?)",
            (memory_id, row[0] or "", row[1] or "", row[2] or ""),
        )
```

**鏀瑰姩 3**锛歚search()` 鐢?FTS5 鏇挎崲瀛愪覆鍖归厤

```python
async def search(self, query, ...) -> list[MemoryCandidate]:
    await self.initialize()
    query_text = str(query or "").strip()
    if not query_text:
        return []

    # 馃啎 鏋勯€?FTS5 鏌ヨ锛氭寜瀛楃 bigram 鎷嗗垎涓枃
    fts_terms = self._build_fts_query(query_text)
    
    # 鐘舵€?鍙鎬ц繃婊ゆ潯浠舵瀯閫狅紙淇濇寔涓嶅彉锛?..

    async with aiosqlite.connect(self.db_path) as db:
        if fts_terms:
            # FTS5 涓昏矾寰?            cursor = await db.execute(f"""
                SELECT cm.id, cm.kind, cm.source, cm.summary, cm.content,
                       cm.session_id, cm.persona_id, cm.tags, cm.importance,
                       cm.confidence, cm.status, cm.create_time,
                       cm.update_time, cm.last_access_time, cm.metadata, cm.visibility,
                       bm25(canonical_fts) AS fts_score
                FROM canonical_fts
                JOIN canonical_memories cm ON cm.id = canonical_fts.memory_id
                WHERE canonical_fts MATCH ?
                  AND {' AND '.join(where_conditions)}
                ORDER BY fts_score
                LIMIT ?
            """, (fts_terms, *params, top_k * 4))
        else:
            # 闄嶇骇鍒板師鏈夐€昏緫锛堟瀬鐭煡璇級
            ...
```

**鏀瑰姩 4**锛欶TS 鏌ヨ鏋勯€犺緟鍔╁嚱鏁?
```python
@staticmethod
def _build_fts_query(text: str) -> str:
    """灏嗕腑鏂囨枃鏈媶鍒嗕负 unicode61 鍏煎鐨勬悳绱㈣瘝"""
    import re
    # 鎻愬彇涓枃瀛楃搴忓垪鍜岃嫳鏂囧崟璇?    segments = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+', text)
    terms = []
    for seg in segments:
        if re.match(r'[a-zA-Z0-9]+', seg):
            terms.append(f'"{seg}"')
        else:
            # 涓枃鎸?bigram 鎷嗗垎锛屼笌 unicode61 tokenizer 瀵归綈
            for i in range(len(seg)):
                terms.append(f'"{seg[i]}"')
    return " OR ".join(terms) if terms else ""
```

**鏀瑰姩 5**锛氬湪 `upsert()` 鍜?`soft_delete()` 鏈熬鍚屾 FTS

鍦?`upsert()` 鐨?`await db.commit()` 涔嬪墠锛?```python
await self._sync_fts(db, memory_id)
```

鍦?`soft_delete()` 鐨勬洿鏂板悗锛?```python
await self._sync_fts(db, memory_id, delete_only=True)
```

---

### C-2锛氱粺涓€ Scoring 閰嶇疆

#### [NEW] [memory_scoring.py](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/contracts/memory_scoring.py)

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class MemoryScoringConfig:
    """缁熶竴鐨勮蹇嗘帓搴忔潈閲嶉厤缃紝娑堥櫎澶氬纭紪鐮?""
    # 缁熶竴铻嶅悎灞傛潈閲嶏紙闃舵 B _fuse_candidates锛?    w_canonical: float = 0.25
    w_hybrid: float = 0.45
    w_importance: float = 0.15
    w_recency: float = 0.10
    w_confidence: float = 0.05
    stale_penalty: float = 0.20

    # v2_store.search 鍐呮帓搴忥紙闃舵 C FTS5 鍚庣殑浜屾鎺掑簭锛?    store_w_relevance: float = 0.45
    store_w_importance: float = 0.20
    store_w_confidence: float = 0.15
    store_w_recency: float = 0.10
    store_stale_penalty: float = 0.25

    # context_builder.select 鎺掑簭
    ctx_w_relevance: float = 0.45
    ctx_w_importance: float = 0.25
    ctx_w_confidence: float = 0.15
    ctx_w_recency: float = 0.10
    ctx_stale_penalty: float = 0.25

DEFAULT_SCORING = MemoryScoringConfig()
```

#### [MODIFY] 涓夊纭紪鐮佹浛鎹?
| 鏂囦欢 | 褰撳墠纭紪鐮?| 鏀逛负寮曠敤 |
|------|-----------|---------|
| [v2_store.py L450-458](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/v2_store.py#L450-L458) | `0.45, 0.2, 0.15, 0.1, 0.25` | `DEFAULT_SCORING.store_w_*` |
| [memory_context_builder.py L19-23](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_context_builder.py#L19-L23) | `0.45, 0.25, 0.15, 0.1, 0.25` | `DEFAULT_SCORING.ctx_w_*` |
| [memory_retrieval_service.py _fuse_candidates](file:///c:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/memory/services/memory_retrieval_service.py) | 闃舵 B 鏂板 | `DEFAULT_SCORING.w_*` |

---

## 浼樺厛绾т笌渚濊禆鍏崇郴

```mermaid
graph LR
    A["闃舵 A: 鍗虫椂璁板繂闂搁棬"] --> B["闃舵 B: 缁熶竴妫€绱㈣瀺鍚?]
    B --> C["闃舵 C: FTS5 + Scoring 缁熶竴"]
    
    style A fill:#ff6b6b,color:#fff
    style B fill:#ff6b6b,color:#fff
    style C fill:#ffa94d,color:#fff
```

| 闃舵 | 浼樺厛绾?| 褰卞搷鑼冨洿 | 闅惧害 | Token 寮€閿€ |
|------|--------|---------|------|-----------|
| **A** | P0 | 璁板繂瑕嗙洊鐜?| 鈽呪槄鈽?| 瑙勫垯灞傞浂寮€閿€ |
| **B** | P0 | 妫€绱㈠噯纭巼 | 鈽呪槄鈽?| 鏃犳柊澧?|
| **C-1** FTS5 | P1 | 绂荤嚎妫€绱㈣川閲?| 鈽呪槄鈽?| 鏃?|
| **C-2** Scoring | P1 | 鍙淮鎶ゆ€?| 鈽呪槅鈽?| 鏃?|

> [!IMPORTANT]
> **闃舵 A 鍜?B 浜掔浉鐙珛**锛屽彲浠ュ苟琛屽紑鍙戙€?> 闃舵 C 渚濊禆闃舵 B 瀹屾垚锛堢粺涓€铻嶅悎灞傜殑鏉冮噸闇€瑕佸厛钀藉湴锛屾墠鑳芥娊鍙栦负 config锛夈€?
---

## P2 澶囧繕锛堟湰杞笉瀹炴柦锛?
浠ヤ笅浜嬮」璁板綍鍦ㄦ锛岀瓑 A/B/C 绋冲畾鍚庡啀鎺ㄨ繘锛?
1. **DreamAgent 绉嶅瓙婧?canonical-first**锛氳 `_get_seed_events()` 澧炲姞 `v2_store.list_candidates()` 浣滀负琛ュ厖婧?2. **Retrieval Trace / Ranking Telemetry**锛氬湪 `_fuse_candidates()` 涓煁鐐硅褰?query 鈫?鍊欓€?鈫?鏈€缁堟帓搴?鈫?鏄惁琚敞鍏?3. **杞婚噺璋冧紭**锛氬熀浜?trace 鏁版嵁绂荤嚎姣斿鏌ヨ鏍锋湰锛屽井璋?`MemoryScoringConfig` 鏉冮噸
4. **LLM 澧炲己闂搁棬**锛氬湪闃舵 A 瑙勫垯闂搁棬绋冲畾鍚庯紝浣庨瑙﹀彂 LLM 琛ユ紡灞?
---

## 楠岃瘉璁″垝

### 闃舵 A 楠岃瘉
- 鏋勯€犲寘鍚韩浠藉０鏄庛€佽仈绯绘柟寮忋€佸亸濂界殑娴嬭瘯娑堟伅锛岄獙璇?`_rule_gate_match()` 鍛戒腑鐜?- 纭鍗虫椂鍐欏叆鐨勮蹇嗗湪 canonical_memories 琛ㄤ腑鍙煡
- 纭 dedup_key 鏈哄埗闃叉鍚屼竴浜嬪疄閲嶅鍐欏叆

### 闃舵 B 楠岃瘉
- 瀵规瘮鏀硅繘鍓嶅悗锛氭瀯閫犱竴涓?canonical 鏈?5 鏉″急鍖归厤銆佷絾 hybrid 鏈?1 鏉″己璇箟鍖归厤鐨勫満鏅?- 鏀硅繘鍓嶏細寮鸿涔夊尮閰嶈鐭矾璺宠繃 鈫?鏀硅繘鍚庯細寮鸿涔夊尮閰嶆帓鍦?top 1
- 纭 ReActRetriever 涓嶉渶瑕佷换浣曟敼鍔ㄥ嵆鑷姩鍙楃泭

### 闃舵 C 楠岃瘉
- 楠岃瘉 FTS5 瀵逛腑鏂囨煡璇?鎴戝枩娆㈠悆鑻规灉"鑳藉尮閰嶅埌"鐢ㄦ埛鏈€鐖辩殑姘存灉鏄嫻鏋?
- 楠岃瘉 FTS 绱㈠紩鍦?upsert/delete 鏃舵纭悓姝?- 楠岃瘉涓夊鎺掑簭鏉冮噸缁熶竴鍚庢帓搴忕粨鏋滀笌鏀硅繘鍓嶆棤鍥炲綊

