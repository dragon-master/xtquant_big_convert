"""Inspect original Python modules, not generated multi-module containers.

The flat builders embed both server and client modules in separate loader
functions. Their output is not one server module. Inspect the original modules
here; tests/test_single_file_build.py covers the generated builds separately.
"""

import ast
import os
import tokenize


GENERATED_TOP_LEVEL_FILES = frozenset({
    "BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py",
    "BIGQMT_DRYRUN_NO_REDIS_FLAT_ALL_IN_ONE.py",
    "BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py",
})


def iter_original_python_sources(source_root):
    """Include new/untracked modules; omit only known root-level build output."""
    for directory, subdirs, filenames in os.walk(source_root):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            if os.path.relpath(path, source_root) in GENERATED_TOP_LEVEL_FILES:
                continue
            yield path


def read_python_source(path):
    """Honor Python's own encoding declaration, including UTF-8 BOM and GBK."""
    with tokenize.open(path) as handle:
        return handle.read()


def parse_python_source(path):
    return ast.parse(read_python_source(path), filename=path)
