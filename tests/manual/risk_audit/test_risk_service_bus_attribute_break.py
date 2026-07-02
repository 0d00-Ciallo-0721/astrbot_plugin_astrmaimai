"""Risk 4.1: Service bus attribute break after service replacement.

Verifies that when a service inside CoreServices/CognitionServices is replaced
at runtime (e.g. hot-reload), the host plugin's legacy attrs still point to
the OLD object — because sync_host_compat_attrs() only runs on bootstrap
and apply_hot_config(), not on individual service swaps.
"""

from __future__ import annotations

import unittest


class _FakeService:
    def __init__(self, name: str):
        self.name = name


class TestServiceBusAttributeBreak(unittest.TestCase):
    """Verify legacy attribute staleness after service replacement."""

    def test_gateway_replacement_not_reflected_in_host_plugin(self):
        """When gateway is replaced, host_plugin.gateway still points to old."""
        from dataclasses import replace

        from astrmai.app.runtime_context import (
            PluginRuntimeContext,
        )

        # 1. Build a minimal runtime with gateway A
        old_gateway = _FakeService("gateway_v1")
        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )
        runtime.core = replace(runtime.core, gateway=old_gateway)

        # 2. Create a fake host plugin and sync attrs
        class FakeHostPlugin:
            pass

        host = FakeHostPlugin()
        runtime.bind_host_plugin(host)
        runtime.sync_host_compat_attrs()

        self.assertIs(host.gateway, old_gateway, "host.gateway should be gateway_v1 after sync")

        # 3. Replace gateway in runtime
        new_gateway = _FakeService("gateway_v2")
        runtime.core = replace(runtime.core, gateway=new_gateway)

        # 4. Verify runtime sees the new gateway
        self.assertIs(runtime.gateway, new_gateway, "runtime.gateway should be gateway_v2 after replacement")

        # 5. CRITICAL: host plugin still points to OLD gateway
        self.assertIs(host.gateway, old_gateway,
                      "BUG: host.gateway still points to gateway_v1 after replacement! "
                      "Any code using host.gateway directly runs on stale service.")

        # 6. sync_host_compat_attrs() would fix it — but is it called on every swap?
        runtime.sync_host_compat_attrs()
        self.assertIs(host.gateway, new_gateway,
                      "After explicit sync_host_compat_attrs(), host.gateway is updated.")

    def test_export_legacy_attrs_skips_none_services(self):
        """Services that are None (degraded/optional) are omitted from export."""
        from astrmai.app.runtime_context import PluginRuntimeContext, export_legacy_attrs

        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )
        # sys3_router and cron_guard start as None in WorkModeServices
        attrs = export_legacy_attrs(runtime)

        self.assertNotIn("sys3_router", attrs, "None services should be omitted from legacy attrs")
        self.assertNotIn("cron_guard", attrs, "None services should be omitted from legacy attrs")

    def test_weakref_does_not_prevent_gc(self):
        """host_plugin_ref uses weakref — host can be GC'd even if runtime alive."""
        import gc
        import weakref

        from astrmai.app.runtime_context import PluginRuntimeContext

        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=None,
            runtime_coordinator=None,
            host_bridge=None,
        )

        class FakeHostPlugin:
            pass

        host = FakeHostPlugin()
        host_ref = weakref.ref(host)
        runtime.bind_host_plugin(host)

        del host
        gc.collect()

        self.assertIsNone(host_ref(),
                          "host_plugin_ref is a weakref — host can be GC'd. "
                          "This is BY DESIGN but callers must handle host_plugin_ref() returning None.")


if __name__ == "__main__":
    unittest.main()
