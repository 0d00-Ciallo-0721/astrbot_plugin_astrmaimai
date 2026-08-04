import unittest

from astrmai.learning.dedup import (
    CandidateRegistry,
    expression_fingerprint,
    jargon_fingerprint,
    normalize_expression_text,
    normalize_jargon_term,
)


class LearningScopeDedupTests(unittest.TestCase):
    def test_expression_identity_is_group_scoped_and_situation_independent(self):
        first = expression_fingerprint("group-1", "ending", " 好呀～ ", "简短接话")
        same = expression_fingerprint("group-1", "ending", "好呀~", "日常回应")
        other_group = expression_fingerprint("group-2", "ending", "好呀~", "日常回应")

        self.assertEqual(first, same)
        self.assertNotEqual(first, other_group)
        self.assertEqual(normalize_expression_text(" 好呀～ "), "好呀~")

    def test_jargon_identity_is_global_and_nfkc_normalized(self):
        self.assertEqual(jargon_fingerprint("ＢＩＧＢＩＲＤ"), jargon_fingerprint("bigbird"))
        self.assertEqual(
            jargon_fingerprint("bigbird", "旧解释"),
            jargon_fingerprint("bigbird", "修正后的解释"),
        )
        self.assertEqual(normalize_jargon_term(" ＢＩＧＢＩＲＤ "), "bigbird")

    def test_process_registry_blocks_concurrent_duplicate_until_release(self):
        registry = CandidateRegistry()
        accepted, rejected = registry.claim(["expression:a", "expression:b"])
        self.assertEqual(accepted, {"expression:a", "expression:b"})
        self.assertEqual(rejected, set())

        accepted_again, rejected_again = registry.claim(["expression:a", "expression:c"])
        self.assertEqual(accepted_again, {"expression:c"})
        self.assertEqual(rejected_again, {"expression:a"})

        registry.release(["expression:a"])
        accepted_after_release, _ = registry.claim(["expression:a"])
        self.assertEqual(accepted_after_release, {"expression:a"})


if __name__ == "__main__":
    unittest.main()
