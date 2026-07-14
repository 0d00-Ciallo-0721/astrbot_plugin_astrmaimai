"""Risk 6.6: _conf_schema.json garbled-text descriptions.

Verifies that Chinese text fields in the JSON config schema are properly
encoded and not corrupted (mojibake from double-encoding).
"""

from __future__ import annotations

import json
import os
import unittest


class TestConfSchemaGarbledText(unittest.TestCase):
    """Verify _conf_schema.json has no garbled Chinese descriptions."""

    @classmethod
    def setUpClass(cls):
        base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )))
        schema_path = os.path.join(base, "_conf_schema.json")
        with open(schema_path, encoding="utf-8") as f:
            cls.schema = json.load(f)

    def test_memory_section_has_no_garbled_text(self):
        """Memory descriptions remain valid UTF-8 Chinese after schema edits."""
        memory = self.schema.get("memory", {}).get("items", {})

        garbled_markers = ["鏃堕棿", "琛板噺", "绯绘暟", "绐楀彛", "璁板繂"]  # common mojibake

        found_garbled = []
        for field_name, field_info in memory.items():
            desc = field_info.get("description", "")
            for marker in garbled_markers:
                if marker in desc:
                    found_garbled.append((field_name, desc[:80]))
                    break

        self.assertEqual(
            found_garbled,
            [],
            f"Memory config contains mojibake: {[field[0] for field in found_garbled]}",
        )

    def test_count_garbled_fields(self):
        """Count how many fields across the entire schema have garbled text."""
        garbled_markers = ["鏃堕棿", "琛板噺", "绯绘暟", "绐楀彛", "璁板繂", "绫诲瀷", "鎻忚堪"]

        garbled_count = 0
        garbled_fields = []

        def _scan(obj, path=""):
            nonlocal garbled_count
            if isinstance(obj, dict):
                desc = obj.get("description", "")
                for marker in garbled_markers:
                    if marker in str(desc):
                        garbled_count += 1
                        garbled_fields.append(f"{path}: {desc[:60]}")
                        break
                for key, value in obj.items():
                    if key == "description":
                        continue
                    _scan(value, f"{path}.{key}" if path else key)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(self.schema)

        print(f"\n    [GARBLED] {garbled_count} fields with mojibake in _conf_schema.json")
        if garbled_fields:
            for field in garbled_fields:
                print(f"      {field}")

        self.assertEqual(
            garbled_count,
            0,
            f"Found mojibake fields: {garbled_fields}",
        )


if __name__ == "__main__":
    unittest.main()
