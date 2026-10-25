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
