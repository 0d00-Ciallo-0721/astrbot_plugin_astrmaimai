import unittest

from astrmai.infrastructure.runtime.runtime_contracts import (
    FailureKind,
    FocusThreadContext,
    LLMCallResult,
    PromptEnvelope,
    VisionBundle,
    VisibleReplyArtifact,
)


class RuntimeContractsMigratedTests(unittest.TestCase):
    def test_focus_thread_context_all_thread_events_deduplicates(self):
        root = object()
        focus = object()
        related = object()
        ctx = FocusThreadContext(
            focus_event=focus,
            root_event=root,
            core_events=[focus, root],
            related_events=[related, focus],
        )
        merged = ctx.all_thread_events()
        self.assertEqual(merged, [root, focus, related])

    def test_prompt_envelope_preserves_structured_sections(self):
        envelope = PromptEnvelope(
            raw_user_text="Alice: why not?",
            last_assistant_reply="AstrMai: no, that is not allowed",
            focus_message_text="Alice: why not?",
            direct_context_text="AstrMai: that topic is not allowed",
            related_context_text="Bob: same question here",
            ambient_background_text="Carol: I am getting water",
        )
        self.assertEqual(envelope.focus_message_text, "Alice: why not?")
        self.assertIn("AstrMai:", envelope.direct_context_text)
        self.assertIn("Bob:", envelope.related_context_text)
        self.assertEqual(envelope.ambient_background_text, "Carol: I am getting water")

    def test_llm_call_result_and_visible_reply_artifact_are_typed(self):
        result = LLMCallResult(
            ok=False,
            error_kind=FailureKind.BAD_PAYLOAD,
            error_message="payload invalid",
            model_id="model-x",
        )
        artifact = VisibleReplyArtifact(
            visible_text="hello",
            segments=["hello"],
            persistable_text="hello",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, FailureKind.BAD_PAYLOAD)
        self.assertFalse(artifact.blocked)

    def test_vision_bundle_keeps_direct_request_metadata(self):
        bundle = VisionBundle(
            image_urls=["a.png"],
            direct_image_urls=["a.png"],
            is_direct_request=True,
            is_image_only=True,
            source="focus_thread",
        )
        self.assertTrue(bundle.is_direct_request)
        self.assertEqual(bundle.source, "focus_thread")


__all__ = ["RuntimeContractsMigratedTests"]
