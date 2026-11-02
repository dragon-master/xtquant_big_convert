"""Offline contracts where the Guosen adaptations meet the upstream fixes."""

import datetime as dt
import os
import sys
import types
import unittest
from unittest import mock

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat
from bigqmt_signal_trader.account_type_map import reload as reload_map
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.models import OrderRequest
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


TODAY = dt.datetime(2026, 10, 26, 13, 30)
TODAY_LABEL = "20261026"
PREVIOUS_LABEL = "20261023"
FIELDS = ["open", "high", "low", "close", "volume", "amount"]


class MultiAccountCollateralContractTest(unittest.TestCase):
    def test_one_gateway_resolves_each_accounts_order_type_without_real_orders(self):
        config = types.ModuleType("bigqmt_signal_trader_local_config")
        config.BIGQMT_ACCOUNT_TYPE_MAP = {"stock": "STOCK", "credit": "CREDIT"}
        recorded = []

        def record(op_type, order_type, account, *args, **kwargs):
            recorded.append((account, op_type))

        try:
            with mock.patch.dict("sys.modules", {config.__name__: config}):
                reload_map()
                gateway = BigQmtOrderGateway(
                    context_info=object(), account_id="stock", account_type="STOCK",
                    passorder_func=record,
                )
                for account in ("stock", "credit"):
                    for action in ("BUY", "SELL"):
                        gateway.submit(OrderRequest(
                            signal_id="offline-" + account + action,
                            account_id=account, action=action, stock_code="600000.SH",
                            volume=100, price=10.0, price_type="LIMIT",
                            strategy_name="offline-contract", order_type=None,
                        ))
        finally:
            reload_map()
        self.assertEqual(recorded, [
            ("stock", 23), ("stock", 24), ("credit", 33), ("credit", 34),
        ])


class _MemoryCache:
    def __init__(self):
        self.frame = pd.DataFrame(
            {"open": [2.55, 2.50], "high": [2.63, 2.51],
             "low": [2.45, 2.25], "close": [2.46, 2.43],
             "volume": [100, 200], "amount": [246, 486]},
            index=["20261021", PREVIOUS_LABEL],
        )
        self.write_calls = 0

    def read(self, *args, **kwargs):
        return self.frame.copy()

    def write(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("The forming daily bar must not be written to the cache")


class _CachedData(BigQmtXtData):
    def __init__(self):
        self.cache = _MemoryCache()
        self.client = types.SimpleNamespace(local_cache_config={"fallback_rpc": False})
        self.live_calls = []

    def _local_cache(self):
        return self.cache

    def _call(self, *args, **kwargs):
        raise AssertionError("No actual RPC is allowed in this test")

    def get_market_data_ex(self, **kwargs):
        self.live_calls.append(kwargs)
        return {"000001.SZ": pd.DataFrame(
            {"open": [2.41], "high": [2.42], "low": [2.29],
             "close": [2.36], "volume": [150], "amount": [354]},
            index=[TODAY_LABEL],
        )}


class CachedDailyRescueContractTest(unittest.TestCase):
    def read(self, data, **kwargs):
        with mock.patch.object(xtquant_compat, "_now", return_value=TODAY), \
                mock.patch.object(xtquant_compat, "_in_trading_session", return_value=True), \
                mock.patch.dict(xtquant_compat._DAILY_RESCUE_NOTICE, {"shown": False}):
            return data.get_local_data(
                field_list=FIELDS, stock_list=["000001.SZ"], period="1d", count=2,
                **kwargs,
            )["000001.SZ"]

    def test_forming_bar_is_read_live_without_rewriting_the_local_cache(self):
        data = _CachedData()
        frame = self.read(data)
        self.assertEqual(str(frame.index[-1]), TODAY_LABEL)
        self.assertEqual(float(frame["close"].iloc[-1]), 2.36)
        self.assertEqual(len(data.live_calls), 1)
        self.assertFalse(data.live_calls[0]["use_formula"])
        self.assertEqual(str(data.cache.frame.index[-1]), PREVIOUS_LABEL)
        self.assertEqual(len(data.cache.frame), 2)
        self.assertEqual(data.cache.write_calls, 0)

    def test_opt_out_keeps_the_raw_local_answer_and_never_reads_live(self):
        data = _CachedData()
        frame = self.read(data, resynth_ongoing_multiday=False)
        self.assertEqual(str(frame.index[-1]), PREVIOUS_LABEL)
        self.assertEqual(len(frame), 2)
        self.assertEqual(data.live_calls, [])

    def test_historical_window_does_not_append_the_forming_bar(self):
        data = _CachedData()
        frame = self.read(data, end_time=PREVIOUS_LABEL)
        self.assertEqual(str(frame.index[-1]), PREVIOUS_LABEL)
        self.assertEqual(data.live_calls, [])


if __name__ == "__main__":
    unittest.main()
