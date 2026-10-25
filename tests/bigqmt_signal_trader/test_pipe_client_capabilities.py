"""Pipe subscriptions must stay Redis-free and async orders must use event push."""

import os
from unittest import mock

import pytest

from bigqmt_signal_trader import xtquant_compat as compat


def _client(config, redis_client=None):
    with mock.patch.object(compat, "load_client_config", return_value={}), \
            mock.patch.dict(os.environ, {
                "BIGQMT_REDIS_HOST": "", "BIGQMT_REDIS_USERNAME": "",
                "BIGQMT_REDIS_PASSWORD": "",
            }):
        return compat.BigQmtRpcClient(
            account_id="acct", redis_config=config, redis_client=redis_client)


def test_pipe_subscription_events_do_not_create_a_redis_client():
    client = _client({"transport": "pipe"})
    client._redis = mock.Mock(side_effect=AssertionError("Unexpected Redis access"))
    event = client.publish_event("subscribe_quote", {"seq": 1})
    client.save_quote_subscription(1, {"seq": 1})
    client.save_quote_subscription(1, {}, active=False)
    assert event["event_type"] == "subscribe_quote"
    assert event["account_id"] == "acct"
    assert event["payload"] == {"seq": 1}
    client._redis.assert_not_called()


@pytest.mark.parametrize("config,injected", [
    ({"transport": "redis"}, False),
    ({"transport": "pipe", "host": "configured-redis"}, False),
    ({"transport": "pipe"}, True),
    ({"transport": "zmq", "zmq": {"redis_discovery_enabled": True}}, False),
    ({"transport": "mysql"}, False),
    ({"transport": "shm"}, False),
])
def test_existing_redis_metadata_paths_keep_writes_and_removals(config, injected):
    redis = mock.Mock()
    client = _client(config, redis_client=redis if injected else None)
    client._redis = mock.Mock(return_value=redis)
    client.publish_event("subscribe_quote", {"seq": 2})
    client.save_quote_subscription(2, {"seq": 2})
    client.save_quote_subscription(2, {}, active=False)
    redis.xadd.assert_called_once()
    redis.publish.assert_called_once()
    redis.hset.assert_called_once()
    redis.hdel.assert_called_once_with("bigqmt:quote_subscriptions:acct", "2")


def test_pure_zmq_keeps_skipping_redis_metadata():
    client = _client({"transport": "zmq", "host": "unused-redis"})
    client._redis = mock.Mock(side_effect=AssertionError("Unexpected Redis access"))
    client.publish_event("subscribe_quote", {})
    client.save_quote_subscription(1, {})
    client.save_quote_subscription(1, {}, active=False)
    client._redis.assert_not_called()


def test_pipe_tick_subscribe_callback_and_unsubscribe_never_touch_redis():
    client = _client({"transport": "pipe", "formula_server": {"enabled": False}})
    client._redis = mock.Mock(side_effect=AssertionError("Unexpected Redis access"))
    initial = {"600000.SH": {"lastPrice": 10.0}}

    class SnapshotTransport:
        def send_request(self, request, timeout):
            assert request["method"] == "get_full_tick"
            return {"ok": True, "data": initial}

    class Subscription:
        def start(self):
            pass

        def subscribe_whole_quote(self, codes, callback=None):
            assert codes == ["600000.SH"]
            self.callback = callback
            self.active = True
            return 1

        def has_subscription(self, seq):
            return seq == 1 and self.active

        def unsubscribe_quote(self, seq):
            assert seq == 1
            self.active = False

    client._transport_instance = SnapshotTransport()
    data = compat.BigQmtXtData(client)
    subscription = data._quote_session = Subscription()
    received = []
    seq = data.subscribe_quote("600000.SH", period="tick", callback=received.append)
    update = {"600000.SH": {"lastPrice": 10.1}}
    subscription.callback(update)
    assert data.unsubscribe_quote(seq) == 0
    assert received == [initial, update]
    assert not subscription.active
    # _record_subscription catches errors, so completion alone cannot prove no I/O.
    client._redis.assert_not_called()


def test_pipe_async_submit_uses_push_and_keeps_response_before_order_and_trade():
    trader = compat.BigQmtXtTrader(account_id="acct")
    trader.client = _client({"transport": "pipe"})
    received = []

    class Callback(compat.XtQuantTraderCallback):
        def on_order_stock_async_response(self, response):
            received.append("response")

        def on_stock_order(self, order):
            received.append("order")

        def on_stock_trade(self, trade):
            received.append("trade")

        def on_order_error(self, error):
            received.append("error")

    trader.register_callback(Callback())
    waits = []

    def submit(*args, **kwargs):
        waits.append(kwargs["wait_settlement"])
        for kind in ("order", "trade"):
            trader._dispatch_event({
                "event_type": kind, "account_id": "acct", "stock_code": "600000.SH",
                "order_sys_id": "sys-1", "remark": "pipe-tag", "user_order_id": "pipe-tag",
                "trade_id": "trade-1", "action": "BUY", "volume": 100, "price": 10.0,
            })
        return {"status": "SUBMITTED", "order_sys_id": "sys-1", "user_order_id": "pipe-tag"}

    trader.order_stock_result = submit
    try:
        seq = trader.order_stock_async("acct", "600000.SH", 23, 100, 11, 10.0, "s", "pipe-tag")
        assert seq > 0
        assert trader.wait_async_orders(timeout=5)
        assert waits == [False]
        assert received == ["response", "order", "trade"]
    finally:
        trader.stop()
