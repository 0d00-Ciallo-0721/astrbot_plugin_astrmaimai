"""Risk 4.6: Legacy compat layer disconnect after service degradation.

Verifies that when a service becomes None (degraded/disabled), the legacy
compat layer silently skips it — host plugin attributes are NOT cleaned up.
This leaves stale references that can cause confusing behavior.
"""

from __future__ import annotations

import unittest


class TestLegacyCompatDisconnect(unittest.TestCase):
    """Verify legacy compat attribute lifecycle."""

    def test_all_legacy_attrs_have_corresponding_property(self):
        """Every name in LEGACY_RUNTIME_ATTRS must have a matching @property."""
        from astrmai.app.runtime_context import (
            LEGACY_RUNTIME_ATTRS,
            PluginRuntimeContext,
        )

        missing = []
        for name in LEGACY_RUNTIME_ATTRS:
            if not hasattr(PluginRuntimeContext, name):
                missing.append(name)

        self.assertEqual(missing, [],
                         f"These LEGACY_RUNTIME_ATTRS have no @property on PluginRuntimeContext: {missing}. "
                         f"They will always be None and never exported.")

    def test_none_service_not_exported(self):
        """Services that are None are omitted from export_legacy_attrs."""
        from astrmai.app.runtime_context import PluginRuntimeContext, export_legacy_attrs

        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )

        attrs = export_legacy_attrs(runtime)

        none_services = ["sys3_router", "cron_guard", "visual_cortex", "reflector"]
        for name in none_services:
            self.assertNotIn(name, attrs,
                             f"None service '{name}' should NOT be in exported attrs")

    def test_stale_attribute_not_cleaned_on_none(self):
        """When a previously-set service becomes None, host plugin keeps old value."""
        from dataclasses import replace

        from astrmai.app.runtime_context import (
            CoreServices,
            PluginRuntimeContext,
        )

        class _FakeGateway:
            pass

        old_gw = _FakeGateway()
        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )
        runtime.core = replace(runtime.core, gateway=old_gw)

        class FakeHost:
            pass

        host = FakeHost()
        runtime.bind_host_plugin(host)
        runtime.sync_host_compat_attrs()

        self.assertTrue(hasattr(host, "gateway"), "host.gateway should be set")
        self.assertIs(host.gateway, old_gw)

        # Now "degrade" gateway to None
        runtime.core = replace(runtime.core, gateway=None)

        # sync_host_compat_attrs SKIPS None values
        runtime.sync_host_compat_attrs()

        # host.gateway STILL points to old_gw — not cleaned up!
        self.assertIs(host.gateway, old_gw,
                      "CRITICAL: host.gateway still points to old gateway after degradation! "
                      "Code using host.gateway will call a stale/dead service.")

    def test_export_legacy_attrs_coverage(self):
        """Check how many of the 32 LEGACY_RUNTIME_ATTRS would be exported."""
        from astrmai.app.runtime_context import (
            LEGACY_RUNTIME_ATTRS,
            PluginRuntimeContext,
            export_legacy_attrs,
        )

        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )

        attrs = export_legacy_attrs(runtime)

        always_present = {"raw_config", "config", "_background_tasks", "runtime_coordinator", "host_bridge"}
        for key in always_present:
            self.assertIn(key, attrs, f"'{key}' should always be in exported attrs")

        exported_legacy = [name for name in LEGACY_RUNTIME_ATTRS if name in attrs]
        missing_legacy = [name for name in LEGACY_RUNTIME_ATTRS if name not in attrs]

        print(f"\n    [COVERAGE] Fresh runtime exports {len(exported_legacy)}/32 legacy attrs")
        if missing_legacy:
            print(f"    [COVERAGE] Missing (None): {missing_legacy}")

        # Runtime is bare — all services are None. This is expected.
        self.assertLessEqual(len(exported_legacy), 32,
                             f"Fresh runtime exports {len(exported_legacy)}/32 attrs. "
                             f"Missing: {len(missing_legacy)}")


if __name__ == "__main__":
    unittest.main()
