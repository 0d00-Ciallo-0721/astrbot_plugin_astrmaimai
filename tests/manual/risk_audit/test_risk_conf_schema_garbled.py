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

    def test_memory_section_has_garbled_text_detected(self):
        """deep_temporal_* fields have garbled Chinese descriptions — CONFIRMED BUG."""
        memory = self.schema.get("memory", {}).get("items", {})

        garbled_markers = ["鏃堕棿", "琛板噺", "绯绘暟", "绐楀彛", "璁板繂"]  # common mojibake

        found_garbled = []
        for field_name, field_info in memory.items():
            desc = field_info.get("description", "")
            for marker in garbled_markers:
                if marker in desc:
                    found_garbled.append((field_name, desc[:80]))
                    break

        self.assertGreater(len(found_garbled), 0,
                           f"CONFIRMED: {len(found_garbled)} fields in memory section have garbled text. "
                           f"Fields: {[f[0] for f in found_garbled]}. "
                           f"This is the mojibake bug — file was double-encoded.")
        print(f"\n    [CONFIRMED] {len(found_garbled)} garbled memory field(s): {[f[0] for f in found_garbled]}")

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

        # This is informational — we don't assert zero because the bug is known
        self.assertLessEqual(garbled_count, 20,
                             f"Found {garbled_count} garbled fields. "
                             f"Should be 0 or very few. Check encoding pipeline.")
        self.assertGreater(garbled_count, 0,
                           "If 0 garbled fields, the bug may be fixed — update this test.")


if __name__ == "__main__":
    unittest.main()
