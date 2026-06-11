import importlib
import sys
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class MessageScopeContractRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_contract_message_scope_reexports_presentation_authority(self):
        from astrmai.conversation.contracts.message_scope import IngressDecision as ContractIngressDecision
        from astrmai.conversation.contracts.message_scope import MessageScope as ContractMessageScope
        from astrmai.presentation.dto.message_scope import IngressDecision as PresentationIngressDecision
        from astrmai.presentation.dto.message_scope import MessageScope as PresentationMessageScope

        self.assertIs(ContractIngressDecision, PresentationIngressDecision)
        self.assertIs(ContractMessageScope, PresentationMessageScope)

    def test_text_segmenter_no_longer_exposes_semantic_chunk(self):
        sys.modules.pop("astrmai.conversation.execution.text_segmenter", None)
        segmenter_mod = importlib.import_module("astrmai.conversation.execution.text_segmenter")

        self.assertFalse(hasattr(segmenter_mod.TextSegmenter, "semantic_chunk"))
