"""Generated bundles must not change original-module boundary checks."""

import ast
import io
import os
import tempfile
import unittest
from unittest import mock

from qmt_source_inspection import (
    GENERATED_TOP_LEVEL_FILES,
    iter_original_python_sources,
    parse_python_source,
    read_python_source,
)


class OriginalSourceInspectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name

    def write_source(self, relative, source, encoding="utf-8"):
        path = os.path.join(self.root, relative)
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        with io.open(path, "w", encoding=encoding) as handle:
            handle.write(source)
        return path

    def test_known_generated_root_files_are_not_original_modules(self):
        for name in GENERATED_TOP_LEVEL_FILES:
            self.write_source(name, "# coding: gbk\n# 中文生成文件\n", "gbk")
        original = self.write_source("new_server.py", "value = 1\n")
        self.assertEqual(list(iter_original_python_sources(self.root)), [original])

    def test_same_filename_inside_a_package_is_still_checked(self):
        name = "BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py"
        self.write_source(name, "# generated at source root\n")
        nested = self.write_source(os.path.join("package", name), "value = 1\n")
        self.assertEqual(list(iter_original_python_sources(self.root)), [nested])

    def test_new_untracked_gbk_server_imports_are_not_hidden(self):
        path = self.write_source(
            "new_server.py",
            "# coding: gbk\n# 中文源码\n"
            "from bigqmt_signal_trader.option_analytics import option_greeks\n"
            "from xtquant.xtconstant import ACCOUNT_TYPE_DICT\n"
            "value = xtconstant.ACCOUNT_TYPE_DICT\n",
            "gbk",
        )
        self.assertEqual(list(iter_original_python_sources(self.root)), [path])
        tree = parse_python_source(path)
        imports = {node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        attributes = {node.attr for node in ast.walk(tree)
                      if isinstance(node, ast.Attribute)}
        self.assertIn("bigqmt_signal_trader.option_analytics", imports)
        self.assertIn("xtquant.xtconstant", imports)
        self.assertIn("ACCOUNT_TYPE_DICT", attributes)

    def test_parser_honors_utf8_bom_and_gbk_without_rewriting_files(self):
        for encoding in ("utf-8", "utf-8-sig", "gbk"):
            with self.subTest(encoding=encoding):
                declared = "utf-8" if encoding.startswith("utf-8") else encoding
                source = "# coding: %s\n# 中文源码\nvalue = 1\n" % declared
                path = self.write_source("server.py", source, encoding)
                with open(path, "rb") as handle:
                    original = handle.read()
                self.assertIn("中文源码", read_python_source(path))
                self.assertIsInstance(parse_python_source(path), ast.Module)
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), original)

    def test_invalid_original_source_still_fails(self):
        path = self.write_source("server.py", "def broken(:\n")
        with self.assertRaises(SyntaxError):
            parse_python_source(path)

    def test_option_guard_still_rejects_an_original_gbk_server_import(self):
        import test_option_analytics as option_tests

        self.write_source(
            os.path.join("src", "server.py"),
            "# coding: gbk\n# 中文源码\n"
            "from bigqmt_signal_trader.option_analytics import option_greeks\n",
            "gbk",
        )
        case = option_tests.ClientOnlyImportBoundaryTest()
        with mock.patch.object(option_tests, "ROOT", self.root):
            with self.assertRaisesRegex(AssertionError, "server.py"):
                case.test_qmt_server_modules_do_not_import_option_analytics()

    def test_constant_guards_still_reject_unsupported_names_in_gbk_source(self):
        import test_qmt_bundled_xtconstant as constant_tests

        self.write_source(
            os.path.join("src", "server.py"),
            "# coding: gbk\n# 中文源码\n"
            "from xtquant.xtconstant import ACCOUNT_TYPE_DICT\n"
            "value = xtconstant.ACCOUNT_TYPE_DICT\n",
            "gbk",
        )
        case = constant_tests.ServerSideUsageTest()
        methods = (case.test_no_server_module_uses_a_name_the_terminal_lacks,
                   case.test_no_server_module_imports_one_by_name_either)
        with mock.patch.object(constant_tests, "ROOT", self.root), \
                mock.patch.object(constant_tests, "SRC", os.path.join(self.root, "src")):
            for method in methods:
                with self.subTest(method=method.__name__):
                    with self.assertRaisesRegex(AssertionError, "ACCOUNT_TYPE_DICT"):
                        method()


if __name__ == "__main__":
    unittest.main()
