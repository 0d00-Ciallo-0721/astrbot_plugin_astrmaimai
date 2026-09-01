import unittest

from astrmai.infrastructure.gateway.json_utils import parse_json_contract, parse_json_payload


class JsonUtilsTests(unittest.TestCase):
    def test_native_object(self):
        result = parse_json_payload('{"goal":"陪伴"}')
        self.assertEqual(result.value, {"goal": "陪伴"})
        self.assertEqual(result.stage, "raw")

    def test_markdown_fence(self):
        result = parse_json_payload("```json\n{\"goal\": \"陪伴\"}\n```")
        self.assertEqual(result.value["goal"], "陪伴")
        self.assertEqual(result.stage, "code_fence_normalized")

    def test_naked_members_are_repaired_only_with_allowlist(self):
        result = parse_json_payload(
            '"morning": "散步", "night": "听歌",',
            allowed_keys=("morning", "night"),
            allow_naked_members=True,
        )
        self.assertEqual(result.value, {"morning": "散步", "night": "听歌"})
        self.assertTrue(result.repair_attempted)

    def test_plain_prose_is_not_promoted_to_object(self):
        with self.assertRaises(ValueError):
            parse_json_payload(
                "模型建议输出 morning 但没有结构化内容",
                allowed_keys=("morning",),
                allow_naked_members=True,
            )

    def test_embedded_object(self):
        result = parse_json_payload("结果如下：{\"decision\":\"approved\"}谢谢")
        self.assertEqual(result.value["decision"], "approved")
        self.assertEqual(result.stage, "embedded_object")

    def test_embedded_array(self):
        result = parse_json_payload('结果如下：["第一段", "第二段"]谢谢')
        self.assertEqual(result.value, ["第一段", "第二段"])
        self.assertEqual(result.stage, "embedded_array")

    def test_schema_invalid_reports_missing_and_unexpected_keys(self):
        result = parse_json_contract(
            '{"extra": true}',
            required_keys=("decision",),
            optional_keys=("reason",),
            field_types={"decision": str, "reason": str},
        )
        self.assertFalse(result.schema_valid)
        self.assertEqual(result.missing_keys, ("decision",))
        self.assertEqual(result.unexpected_keys, ("extra",))
        self.assertEqual(result.terminal_status, "schema_invalid")

    def test_schema_invalid_reports_wrong_field_type(self):
        result = parse_json_contract(
            '{"worth": "yes", "fact": "用户喜欢咖啡"}',
            required_keys=("worth", "fact"),
            field_types={"worth": bool, "fact": str},
        )
        self.assertFalse(result.schema_valid)
        self.assertEqual(result.invalid_type_keys, ("worth",))

    def test_parse_failure_has_structured_terminal_status(self):
        result = parse_json_contract("not json", required_keys=("decision",))
        self.assertFalse(result.schema_valid)
        self.assertEqual(result.terminal_status, "parse_failed")


if __name__ == "__main__":
    unittest.main()
