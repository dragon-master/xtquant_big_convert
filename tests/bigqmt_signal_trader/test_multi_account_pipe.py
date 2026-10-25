# coding: utf-8
"""Each account keeps its own RPC pipe and receives only its execution events."""

import os
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from bigqmt_signal_trader import multi_account
from bigqmt_signal_trader.account_type_map import reload as reload_map
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService
from bigqmt_signal_trader.transports.pipe_push_channel import (
    MultiAccountPipePushChannel, PipeQuotePushChannel, pipe_events_path,
)
from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport, pipe_path
from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient


def test_secondary_uses_its_own_pipe_with_the_shared_prefix():
    primary = types.SimpleNamespace(handlers=types.SimpleNamespace(), redis=None)
    with mock.patch("bigqmt_signal_trader.adapters.redis_common.build_redis_client",
                    side_effect=AssertionError("Redis must not be used")):
        secondary = multi_account._build_secondary(
            primary, "credit", {"rpc": {
                "transport": "pipe", "pipe": {"pipe_name": "custom_bridge"},
            }})
    assert secondary.account_id == "credit"
    assert secondary._transport.path == pipe_path("custom_bridge", "credit")
    assert not secondary.background_threads
    assert secondary.redis is None


def test_secondary_ping_reports_the_secondary_account_and_type():
    from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

    config = types.ModuleType("bigqmt_signal_trader_local_config")
    config.BIGQMT_ACCOUNT_TYPE_MAP = {"stock": "STOCK", "credit": "CREDIT"}
    handlers = BigQmtRpcHandlers("stock", None, None,
                                order_gateway=BigQmtOrderGateway(None, account_type="STOCK"))
    try:
        with mock.patch.dict("sys.modules", {config.__name__: config}):
            reload_map()
            result = multi_account.SecondaryHandlersProxy(handlers, "credit").handle("ping")
        assert result["pong"]
        assert result["account_id"] == "credit"
        assert result["account_type"] == "CREDIT"
    finally:
        reload_map()


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_both_accounts_answer_queries_on_the_strategy_thread():
    prefix = "bigqmt_multi_test_" + uuid.uuid4().hex
    main_thread = threading.get_ident()
    mapping = {"stock": "STOCK", "credit": "CREDIT"}
    config = types.ModuleType("bigqmt_signal_trader_local_config")
    config.BIGQMT_ACCOUNT_TYPE_MAP = mapping

    class Handlers:
        def handle(self, method, params=None):
            account = (params or {}).get("account_id", "stock")
            return {"account_id": account, "account_type": mapping[account],
                    "thread": threading.get_ident()}

    handlers = Handlers()
    primary = RedisPubSubRpcService(
        redis_client=None, handlers=handlers, account_id="stock",
        process_in_listener=True, listener_methods=("*",),
        background_threads=False, heavy_offload=False,
        transport=NamedPipeTransport(account_id="stock", pipe_name=prefix))
    clients = [BigQmtRpcClient(account_id=account, redis_config={
        "transport": "pipe", "pipe": {"pipe_name": prefix},
        "formula_server": {"enabled": False},
    }) for account in mapping]
    try:
        with mock.patch.dict("sys.modules", {config.__name__: config}):
            reload_map()
            manager = multi_account.build_multi_account_rpc_service(
                None, None, {"rpc": {"transport": "pipe", "pipe": {"pipe_name": prefix}},
                             "exec_events": {"enabled": False}},
                lambda *args: primary)
        assert len(manager._services) == 2
        manager.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.call, "query_stock_asset", {},
                                   timeout_seconds=3) for client in clients]
            deadline = time.monotonic() + 4
            while not all(f.done() for f in futures) and time.monotonic() < deadline:
                manager.drain_request_queue()
                manager.drain_pending()
                time.sleep(0.005)
            results = [f.result() for f in futures]
        assert [(r["account_id"], r["account_type"]) for r in results] == list(mapping.items())
        assert all(r["thread"] == main_thread for r in results)
    finally:
        for client in clients:
            client.close()
        if "manager" in locals():
            manager.stop()
        reload_map()


def test_multi_account_events_are_sent_only_to_the_owner():
    channel = MultiAccountPipePushChannel(["stock", "credit"], pipe_name="custom")
    channel.channels = {account: mock.Mock() for account in channel.channels}
    event = {"account_id": "credit", "event_type": "order"}
    channel.publish("exec:order", event)
    channel.channels["credit"].publish.assert_called_once_with("exec:order", event)
    channel.channels["stock"].publish.assert_not_called()
    channel.publish("exec:order", {"account_id": "unknown"})
    channel.channels["stock"].publish.assert_not_called()
    assert channel.channels["credit"].publish.call_count == 1
    channel.publish("quote:sample", {"000300.SH": {"lastPrice": 1}})
    assert channel.channels["stock"].publish.call_count == 1
    assert channel.channels["credit"].publish.call_count == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_secondary_client_receives_its_execution_event_over_a_real_pipe():
    from bigqmt_signal_trader.quote_subscription_manager import build_quote_subscription_service

    prefix = "bigqmt_events_multi_" + uuid.uuid4().hex
    manager, channel = build_quote_subscription_service(
        object(), transport_name="pipe", account_id="stock",
        account_ids=["stock", "credit"], pipe_name=prefix)
    assert isinstance(channel, MultiAccountPipePushChannel)
    received = []
    done = threading.Event()
    client = PipeQuotePushChannel(connect_address=pipe_events_path(prefix, "credit"))
    channel.start_publisher()
    client.start_subscriber(["exec:order"], lambda topic, data: (received.append(data), done.set()))
    try:
        deadline = time.monotonic() + 3
        publisher = channel.channels["credit"]
        while publisher.publisher_subscriber_count() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.publisher_subscriber_count() == 1
        manager._on_push_publisher("exec:order", {"account_id": "stock"})
        manager._on_push_publisher("exec:order", {"account_id": "credit", "order_sys_id": "test"})
        assert done.wait(2)
        assert received == [{"account_id": "credit", "order_sys_id": "test"}]
    finally:
        client.stop()
        channel.stop()
