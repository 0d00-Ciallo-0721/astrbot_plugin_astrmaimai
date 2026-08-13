import asyncio
from types import SimpleNamespace

from astrmai.learning.mining.expression_candidate_extractor import ExpressionCandidateExtractor
from astrmai.learning.mining.expression_pattern_enricher import ExpressionPatternEnricher
from astrmai.learning.mining.jargon_candidate_extractor import JargonCandidateExtractor
from astrmai.learning.mining.jargon_enricher import JargonEnricher
from astrmai.learning.mining.jargon_senses import merge_jargon_senses, select_jargon_senses


class _Gateway:
    def __init__(self, result):
        self.result = result

    async def call_data_process_task(self, **_kwargs):
        return self.result


def test_jargon_evidence_bundle_contains_real_context_and_no_model_evidence():
    extractor = JargonCandidateExtractor(min_count=2)
    messages = [
        SimpleNamespace(id=10, sender_id="u1", sender_name="甲", content="火钳刘明"),
        SimpleNamespace(id=11, sender_id="u2", sender_name="乙", content="火钳刘明"),
        SimpleNamespace(id=12, sender_id="u3", sender_name="丙", content="懂了"),
    ]

    candidates = asyncio.run(extractor.extract("group-1", messages))
    candidate = next(item for item in candidates if item["content"] == "火钳刘明")

    assert candidate["evidence_version"] == 2
    assert candidate["source_message_ids"] == ["10", "11"]
    assert candidate["support_count"] == 2
    assert candidate["contributor_count"] == 2
    assert candidate["model_examples"] == []
    assert candidate["context_windows"]


def test_jargon_enricher_separates_model_examples_and_validates_citations():
    candidate = {
        "content": "火钳刘明",
        "raw_content": "火钳刘明就是火前留名",
        "examples": ["这个词叫火钳刘明", "火钳刘明就是火前留名"],
        "source_examples": ["这个词叫火钳刘明", "火钳刘明就是火前留名"],
        "source_message_ids": ["10", "11"],
        "context_count": 2,
        "count": 2,
        "activation_score": 0.8,
    }
    gateway = _Gateway(
        {
            "items": [
                {
                    "index": 1,
                    "meaning": "在热门内容早期留言",
                    "scene": "网络评论",
                    "confidence": 0.9,
                    "is_jargon": True,
                    "term_type": "jargon",
                    "semantic_novelty": True,
                    "evidence_sufficient": True,
                    "supported_by": ["10", "11", "invented"],
                    "contradicted_by": [],
                    "review_status": "review_pending",
                    "examples": ["模型生成的例句"],
                }
            ]
        }
    )

    result = asyncio.run(JargonEnricher(gateway).enrich("group-1", [candidate]))
    payload = result.items[0]

    assert payload["examples"] == candidate["source_examples"]
    assert payload["model_examples"] == ["模型生成的例句"]
    assert payload["supported_by"] == ["10", "11"]
    assert payload["support_count"] == 2
    assert payload["evidence_sufficient"] is True


def test_expression_enricher_never_promotes_model_samples_to_source_evidence():
    candidate = {
        "candidate_id": "expr-1",
        "candidate_type": "exact",
        "expression": "唉嘿嘿",
        "situation": "轻松回应",
        "style": "轻松",
        "habit_type": "catchphrase",
        "content_kind": "expression",
        "content_samples": ["唉嘿嘿", "唉嘿嘿～"],
        "source_examples": ["唉嘿嘿", "唉嘿嘿～"],
        "source_message_ids": ["1", "2"],
        "support_count": 2,
        "contributor_count": 2,
        "count": 2,
        "distinct_turn_count": 2,
    }
    gateway = _Gateway(
        {
            "items": [
                {
                    "candidate_id": "expr-1",
                    "decision": "keep",
                    "content_kind": "expression",
                    "habit_type": "catchphrase",
                    "summary": "轻松笑声口癖",
                    "situation": "轻松回应",
                    "style": "俏皮",
                    "confidence": 0.8,
                    "review_status": "pending_human",
                    "content_samples": ["模型生成的唉嘿嘿"],
                }
            ]
        }
    )

    result = asyncio.run(ExpressionPatternEnricher(gateway).enrich("group-1", [candidate]))
    payload = result.items[0]

    assert payload["content_samples"] == candidate["source_examples"]
    assert payload["model_examples"] == ["模型生成的唉嘿嘿"]


def test_jargon_polysemy_keeps_approved_sense_and_adds_reviewable_sense():
    existing = {
        "meaning": "网络评论里提前占位",
        "scene": "论坛",
        "review_status": "approved",
        "confidence": 0.9,
        "source_message_ids": ["1", "2"],
        "source_examples": ["火钳刘明"],
        "source_groups": ["group-1"],
        "evidence_digest": "old",
    }
    incoming = {
        "meaning": "群里约定的某个成员昵称",
        "scene": "熟人群聊",
        "review_status": "review_pending",
        "confidence": 0.8,
        "source_message_ids": ["3", "4"],
        "source_examples": ["火钳刘明来了"],
        "source_group_ids": ["group-2"],
        "support_count": 2,
        "contributor_count": 2,
        "evidence_digest": "new",
    }

    senses, _sense_id, is_new, reopened = merge_jargon_senses(
        existing,
        incoming,
        group_id="group-2",
        record_status="active",
    )

    assert is_new is True
    assert reopened is False
    assert len(senses) == 2
    assert {sense["review_status"] for sense in senses} == {"approved", "review_pending"}
    selected = select_jargon_senses({"senses": senses}, "论坛里火钳刘明", limit=2)
    assert selected[0]["meaning"] == "网络评论里提前占位"


def test_rejected_jargon_sense_reopens_only_with_stronger_new_evidence():
    existing = {
        "senses": [
            {
                "sense_id": "sense:fixed",
                "meaning": "旧含义",
                "scene": "群聊",
                "review_status": "rejected",
                "source_message_ids": ["1"],
                "support_count": 1,
                "evidence_digest": "old",
            }
        ]
    }
    incoming = {
        "meaning": "旧含义",
        "scene": "群聊",
        "review_status": "review_pending",
        "source_message_ids": ["2", "3"],
        "support_count": 2,
        "evidence_digest": "new",
    }
    # Use the generated identity so this exercises revision of the same sense.
    from astrmai.learning.mining.jargon_senses import jargon_sense_id

    existing["senses"][0]["sense_id"] = jargon_sense_id("旧含义", "群聊")
    senses, _sense_id, is_new, reopened = merge_jargon_senses(
        existing,
        incoming,
        group_id="group-1",
        record_status="rejected",
    )

    assert is_new is False
    assert reopened is True
    assert senses[0]["review_status"] == "revision_needed"
