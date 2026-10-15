"""Exercise the deployer's config boundary, without running a deployment."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy_qmt_bridge.ps1"
WINDOWS_POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(WINDOWS_POWERSHELL, "requires Windows PowerShell")
class DeploymentConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bigqmt_signal_trader_local_config.py"

    def write_config(self, preserve=True, content="generated config\n"):
        # Extract just the helper from the real script's syntax tree. Never
        # execute deployment steps, start Redis, copy QMT files, or send RPCs.
        command = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TEST_DEPLOY_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'deploy script has syntax errors' }
$helper = $tree.Find({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Write-DeploymentConfig'
}, $true)
if ($null -eq $helper) { throw 'config helper not found' }
function Info($message) {}
function Ok($message) {}
Invoke-Expression $helper.Extent.Text
$written = Write-DeploymentConfig -Path $env:TEST_CONFIG_PATH `
    -Content $env:TEST_CONFIG_CONTENT -PreserveExisting:($env:TEST_PRESERVE -eq '1')
@{written = $written} | ConvertTo-Json -Compress
"""
        env = dict(os.environ, TEST_DEPLOY_SCRIPT=str(DEPLOY_SCRIPT),
                   TEST_CONFIG_PATH=str(self.path), TEST_CONFIG_CONTENT=content,
                   TEST_PRESERVE="1" if preserve else "0")
        result = subprocess.run(
            [WINDOWS_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", command], env=env, capture_output=True, text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_server_account_map_and_custom_permissions_are_not_rewritten(self):
        original = (
            b"\xef\xbb\xbf# private test config\r\n"
            b'BIGQMT_ACCOUNT_TYPE_MAP = {"test-stock": "STOCK", "test-credit": "CREDIT"}\r\n'
            b'BIGQMT_REDIS_CONFIG = {"rpc_allow_order_methods": True, "password": "test-only"}\r\n'
        )
        self.path.write_bytes(original)
        result = self.write_config(content='BIGQMT_REDIS_CONFIG = {"rpc_allow_order_methods": False}\n')
        self.assertIs(result["written"], False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_client_private_settings_are_not_rewritten(self):
        self.path = self.path.with_name("bigqmt_signal_trader_client_config.py")
        original = b'BIGQMT_RPC_TIMEOUT_SECONDS = 75.0\r\n# client-owned\r\n'
        self.path.write_bytes(original)
        self.assertIs(self.write_config()["written"], False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_preservation_does_not_enable_orders_in_an_existing_readonly_config(self):
        original = b'BIGQMT_REDIS_CONFIG = {"rpc_allow_order_methods": False}\n'
        self.path.write_bytes(original)
        self.write_config(content='BIGQMT_REDIS_CONFIG = {"rpc_allow_order_methods": True}\n')
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_config_is_created_even_in_preserve_mode(self):
        result = self.write_config(content="# newly generated\n")
        self.assertIs(result["written"], True)
        self.assertEqual(self.path.read_text(encoding="utf-8-sig").strip(),
                         "# newly generated")

    def test_explicit_regeneration_keeps_the_previous_behavior(self):
        self.path.write_bytes(b"old config\n")
        self.assertIs(self.write_config(preserve=False)["written"], True)
        self.assertEqual(self.path.read_text(encoding="utf-8-sig").strip(),
                         "generated config")

    def test_real_server_and_client_writes_use_the_helper(self):
        source = DEPLOY_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertEqual(source.count("-Content $localCfg -PreserveExisting:$PreserveConfig"), 1)
        self.assertEqual(source.count("-Content $clientCfg -PreserveExisting:$PreserveConfig"), 1)
        self.assertNotIn('Set-Content "$dst\\bigqmt_signal_trader_local_config.py"', source)
        self.assertNotIn('Set-Content "$cdir\\bigqmt_signal_trader_client_config.py"', source)


if __name__ == "__main__":
    unittest.main()
