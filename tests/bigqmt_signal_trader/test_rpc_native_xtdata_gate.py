# coding: utf-8
"""Every strategy-created market provider must honor the native SDK gate.

The runtime and application already accepted native_xtdata_enabled=False,
but the RPC service created another provider with its default True. A real
RPC therefore still dialled MiniQMT after a successful strategy restart.
"""

import os
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_strategy as strategy
from bigqmt_signal_trader.adapters import market_bigqmt
from bigqmt_signal_trader import download_jobs


class Context(object):
    def get_instrumentdetail(self, code):
        return {"InstrumentID": code.split(".")[0], "OpenDate": 19910403}


class RpcNativeXtdataGateTest(unittest.TestCase):
    def setUp(self):
        self._quote_service = strategy._quote_subscription_service
        self.addCleanup(setattr, strategy, "_quote_subscription_service", self._quote_service)

    def _service(self, native_enabled=None):
        rpc = {"enabled": True, "transport": "redis", "redis_client": object(),
               "response_redis_client": object(), "background_threads": False}
        if native_enabled is not None:
            rpc["native_xtdata_enabled"] = native_enabled
        config = {"account_id": "test-only", "account_type": "STOCK", "rpc": rpc}
        with mock.patch.object(strategy, "_load_bridge_module", None), \
                mock.patch.object(strategy, "_build_quote_subscription_service", return_value=None), \
                mock.patch("importlib.reload", side_effect=lambda module: module):
            return strategy._build_rpc_service(Context(), types.SimpleNamespace(), config)

    def test_disabled_rpc_provider_uses_context_without_loading_sdk(self):
        service = self._service(native_enabled=False)
        with mock.patch.object(market_bigqmt, "_load_native_xtdata",
                               side_effect=AssertionError("native SDK must not load")):
            details = service.handlers.handle("get_instrument_detail", {"code": "000001.SZ"})

        self.assertFalse(service.handlers.market_data.native_xtdata_enabled)
        self.assertEqual(details["InstrumentID"], "000001")
        self.assertEqual(details["OpenDate"], 19910403)

    def test_redis_default_stays_enabled(self):
        service = self._service()
        self.assertTrue(service.handlers.market_data.native_xtdata_enabled)

    def test_explicit_enabled_stays_enabled(self):
        service = self._service(native_enabled=True)
        self.assertTrue(service.handlers.market_data.native_xtdata_enabled)

    def test_download_fallback_provider_uses_the_same_disabled_gate(self):
        config = {"account_id": "test-only", "rpc": {"transport": "redis",
                  "native_xtdata_enabled": False}, "download_jobs": {"enabled": True}}
        with mock.patch.object(strategy, "_rpc_service", None), \
                mock.patch.object(strategy, "_exec_event_redis", return_value=object()), \
                mock.patch.object(download_jobs, "pump_download_jobs") as pump:
            strategy._pump_download_jobs(Context(), config)

        pump.assert_called_once()
        provider = pump.call_args[0][1]
        self.assertFalse(provider.native_xtdata_enabled)
        with mock.patch.object(market_bigqmt, "_load_native_xtdata",
                               side_effect=AssertionError("native SDK must not load")):
            self.assertIsNone(provider._native())


if __name__ == "__main__":
    unittest.main()
