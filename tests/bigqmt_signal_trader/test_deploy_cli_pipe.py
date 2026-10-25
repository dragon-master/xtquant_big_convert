"""The deployed CLI uses its configured accounts without connecting to Redis."""

import importlib.util
import types
from pathlib import Path
from unittest import mock


def _cli():
    path = Path(__file__).resolve().parents[2] / "deploy" / "qmt_cli.py"
    spec = importlib.util.spec_from_file_location("deploy_cli_pipe_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.cfg = types.SimpleNamespace(
        BIGQMT_ACCOUNT_ID="stock", BIGQMT_ACCOUNT_TYPE="STOCK",
        BIGQMT_ACCOUNT_TYPE_MAP={"stock": "STOCK", "credit": "CREDIT"},
        BIGQMT_REDIS_CONFIG={"transport": "pipe", "pipe": {"pipe_name": "bigqmt_rpc"}})
    return module


def test_pipe_discovery_does_not_touch_redis(capsys):
    cli = _cli()
    with mock.patch.object(cli, "raw_redis", side_effect=AssertionError("no Redis")):
        cli.cmd_discover(types.SimpleNamespace(account=""))
    output = capsys.readouterr().out
    assert "stock" in output and "credit" in output
    assert "connection not checked" in output


def test_credit_cli_requests_keep_the_credit_type():
    cli = _cli()
    assert cli.as_stock_account("stock").account_type == 2
    assert cli.as_stock_account("credit").account_type == 3
