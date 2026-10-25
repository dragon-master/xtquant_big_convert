# coding: utf-8
"""Check configured pipe accounts without submitting or cancelling orders."""

import argparse
import importlib

from bigqmt_signal_trader.account_type_map import primary_account_type
from bigqmt_signal_trader.xtquant_compat import (
    BigQmtXtData, BigQmtXtTrader, StockAccount, load_client_config,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true",
                        help="validate local settings without contacting QMT")
    args = parser.parse_args()
    loaded = load_client_config()
    if not loaded.get("module"):
        raise RuntimeError("Client config not found; run next to bigqmt_signal_trader_client_config.py")
    config = importlib.import_module(loaded["module"])
    primary = str(config.BIGQMT_ACCOUNT_ID)
    accounts = dict(getattr(config, "BIGQMT_ACCOUNT_TYPE_MAP", {}) or {
        primary: getattr(config, "BIGQMT_ACCOUNT_TYPE", "STOCK"),
    })
    if primary not in accounts:
        raise RuntimeError("The primary account is missing from BIGQMT_ACCOUNT_TYPE_MAP")
    for account_id, account_type in accounts.items():
        label = "%s (...%s)" % (primary_account_type(account_type), str(account_id)[-4:])
        trader = BigQmtXtTrader(account_id=account_id)
        try:
            client = trader.client
            if client.transport_name != "pipe" or client._redis_explicit:
                raise RuntimeError("Expected pipe without Redis settings; check config and BIGQMT_REDIS_* environment variables")
            if client.full_tick_cache_config["enabled"]:
                raise RuntimeError("Redis full-tick cache must be disabled for this deployment")
            if client.formula_server_config["enabled"]:
                raise RuntimeError("Disable FormulaServer to verify all reads through the pipe")
            transport = client._transport()
            print("%s: local config OK, transport=%s" % (label, transport.name))
            if args.config_only:
                continue
            pong = client.call("ping")
            if str(pong.get("account_id")) != str(account_id):
                raise RuntimeError("Server replied for a different account")
            if pong.get("account_type") != primary_account_type(account_type):
                raise RuntimeError("Server account type differs from the local account map")
            account = StockAccount(str(account_id), primary_account_type(account_type))
            asset = trader.query_stock_asset(account)
            positions = trader.query_stock_positions(account)
            print("%s: ping OK, order_api=%s, cash=%s, positions=%d" % (
                label, pong.get("allow_order_methods"), getattr(asset, "cash", None), len(positions)))
            if str(account_id) == primary:
                tick = BigQmtXtData(client).get_full_tick(["000300.SH"])
                if not tick.get("000300.SH"):
                    raise RuntimeError("000300.SH quote was not returned")
                print("000300.SH quote OK")
        finally:
            trader.stop()
    print("Checked %d account(s). No orders submitted or cancelled." % len(accounts))


if __name__ == "__main__":
    main()
