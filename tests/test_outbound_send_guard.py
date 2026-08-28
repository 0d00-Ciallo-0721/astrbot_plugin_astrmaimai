import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.outbound_send_guard import (
    OUTBOUND_SEND_GATE,
    bind_event_generation,
    outbound_send_allowed,
    provider_request_allowed,
)
from astrmai.infrastructure.gateway.gateway_call import GatewayCallMixin
from astrmai.infrastructure.gateway.gateway_exceptions import GatewayShutdownRejected


class OutboundSendGuardTests(unittest.TestCase):
    def setUp(self):
        # Move to a known closed generation; lifecycle transitions remain
        # monotonic so stale event generations can be tested deterministically.
        OUTBOUND_SEND_GATE.close(enforce_provider=True)

    def tearDown(self):
        # Do not leak the strict provider fence into unrelated test modules.
        OUTBOUND_SEND_GATE.close(enforce_provider=True)

    def test_bound_event_is_rejected_after_shutdown(self):
        OUTBOUND_SEND_GATE.open()
        event = SimpleNamespace(_extra={}, get_extra=lambda k, d=None: event._extra.get(k, d), set_extra=lambda k, v: event._extra.__setitem__(k, v))
        generation = bind_event_generation(event)
        self.assertTrue(outbound_send_allowed(event))
        OUTBOUND_SEND_GATE.close()
        self.assertFalse(outbound_send_allowed(event))
        self.assertEqual(event._extra["astrmai_outbound_generation"], generation)

    def test_stale_generation_cannot_send_after_reopen(self):
        OUTBOUND_SEND_GATE.open()
        event = SimpleNamespace(_extra={}, get_extra=lambda k, d=None: event._extra.get(k, d), set_extra=lambda k, v: event._extra.__setitem__(k, v))
        bind_event_generation(event)
        OUTBOUND_SEND_GATE.close()
        OUTBOUND_SEND_GATE.open()
        self.assertFalse(outbound_send_allowed(event))

    def test_provider_request_is_rejected_before_network_after_shutdown(self):
        OUTBOUND_SEND_GATE.open()
        event = SimpleNamespace(_extra={}, get_extra=lambda k, d=None: event._extra.get(k, d), set_extra=lambda k, v: event._extra.__setitem__(k, v))
        bind_event_generation(event)
        OUTBOUND_SEND_GATE.close()
        self.assertFalse(provider_request_allowed(event))
        with self.assertRaises(GatewayShutdownRejected):
            GatewayCallMixin._assert_provider_request_allowed(event)
        self.assertTrue(event._extra["astrmai_provider_request_blocked"])

    def test_explicit_provider_generation_is_rejected_by_lifecycle_shutdown(self):
        OUTBOUND_SEND_GATE.open()
        OUTBOUND_SEND_GATE.close(enforce_provider=True)
        self.assertFalse(provider_request_allowed(generation=1))

    def test_unbound_provider_request_is_rejected_by_strict_shutdown(self):
        OUTBOUND_SEND_GATE.open()
        OUTBOUND_SEND_GATE.close(enforce_provider=True)
        self.assertFalse(provider_request_allowed())
        with self.assertRaises(GatewayShutdownRejected):
            GatewayCallMixin._assert_provider_request_allowed(None)


if __name__ == "__main__":
    unittest.main()
