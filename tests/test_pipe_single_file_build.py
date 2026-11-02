"""The actual pipe build must preserve the account map and enable its events pipe."""

import ast
import os
import subprocess
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pipe_build_keeps_multi_account_config_and_push(tmp_path):
    target = tmp_path / "pipe_bridge.py"
    env = dict(os.environ, BIGQMT_BUILD_OUT=str(target))
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_pipe_single_file_flat.py")],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    source = target.read_text(encoding="gbk")
    compile(source, str(target), "exec")
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_mod_local_config")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<config>", "exec"), namespace)
    expected = {"stock": "STOCK", "credit": "CREDIT"}
    config = {"_SHELL_CONFIG": {
        "BIGQMT_ACCOUNT_ID": "stock", "BIGQMT_ACCOUNT_TYPE": "STOCK",
        "BIGQMT_ACCOUNT_TYPE_MAP": expected,
        "BIGQMT_REDIS_CONFIG": {"transport": "pipe"},
    }}
    types.FunctionType(namespace["_mod_local_config"].__code__, config)()
    assert config["BIGQMT_ACCOUNT_TYPE_MAP"] == expected
    assert config["BIGQMT_ACCOUNT_ID"] == "stock"
    assert 'setdefault("quote_push", {"enabled": True})' in source
    assert 'BIGQMT_REDIS_CONFIG["redis_enabled"] = False' in source

    # Execute only the generated entry's API-binding block with synthetic QMT
    # globals. Never execute the strategy or connect to a running terminal.
    from bigqmt_signal_trader_strategy import capture_qmt_injected_funcs

    binding = next(node for node in tree.body if isinstance(node, ast.Try) and any(
        isinstance(statement, ast.Assign) and any(
            isinstance(name, ast.Name) and name.id == "qmt_extra"
            for name in statement.targets)
        for statement in node.body))
    names = ("passorder", "cancel", "get_trade_detail_data", "download_history_data",
             "download_history_data2", "down_history_data", "query_credit_account",
             "call_formula", "subscribe_formula", "unsubscribe_formula",
             "get_formula_result", "gen_factor_index")
    injected = {name: (lambda *args, _name=name, **kwargs: _name) for name in names}
    captured = []
    namespace = dict(injected, _strategy=types.SimpleNamespace(
        capture_qmt_injected_funcs=capture_qmt_injected_funcs),
        _runtime=types.SimpleNamespace(bind_runtime_api=lambda **kwargs: captured.append(kwargs)))
    exec(compile(ast.Module(body=[binding], type_ignores=[]), "<entry-binding>", "exec"), namespace)
    assert len(captured) == 1
    assert set(captured[0]["extra_funcs"]) == set(names)
    assert all(captured[0]["extra_funcs"][name] is injected[name] for name in names)
