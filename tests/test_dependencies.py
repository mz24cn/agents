# Feature: composable-agent-runtime, Property 17: 零第三方依赖
"""
Property 17: Zero Third-Party Dependencies

Scan all .py files in runtime/ directory and verify that every import
statement references only Python standard library modules or internal
runtime modules.

**Validates: Requirements 7.1**
"""

import ast
import os
import sys

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUNTIME_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime")

# Python 3.10+ exposes stdlib_module_names
STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)


def _collect_py_files() -> list[str]:
    """Return absolute paths of all .py files under runtime/."""
    py_files = []
    for root, _dirs, files in os.walk(RUNTIME_DIR):
        for fname in sorted(files):
            if fname.endswith(".py"):
                py_files.append(os.path.join(root, fname))
    return py_files


def _extract_top_level_module(module_name: str) -> str:
    """Extract the top-level package from a dotted module path.

    e.g. 'urllib.request' -> 'urllib', 'json' -> 'json'
    """
    return module_name.split(".")[0]


def _extract_imports(filepath: str) -> list[str]:
    """Parse a Python file and return all imported top-level module names."""
    with open(filepath, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=filepath)

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(_extract_top_level_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.append(_extract_top_level_module(node.module))
            # `from . import X` has node.module == None; that's a relative
            # import within the package – always allowed.
    return modules


def _is_allowed_module(top_level: str) -> bool:
    """Return True if the module is stdlib or an internal runtime module."""
    if top_level == "runtime":
        return True
    if top_level in STDLIB_MODULES:
        return True
    return False


# ---------------------------------------------------------------------------
# Deterministic property test – validates the zero-dependency invariant
# ---------------------------------------------------------------------------

# Collect all .py files once at module load time so Hypothesis can sample them
_ALL_PY_FILES = _collect_py_files()


@settings(max_examples=max(len(_ALL_PY_FILES), 1), suppress_health_check=[HealthCheck.filter_too_much])
@given(file_index=st.integers(min_value=0, max_value=max(len(_ALL_PY_FILES) - 1, 0)))
def test_property17_zero_third_party_dependencies(file_index: int):
    """Property 17: 零第三方依赖

    For every .py file in runtime/, all import statements must reference
    only Python standard library modules or internal runtime modules.

    **Validates: Requirements 7.1**
    """
    if not _ALL_PY_FILES:
        pytest.skip("No .py files found in runtime/")

    filepath = _ALL_PY_FILES[file_index]
    rel_path = os.path.relpath(filepath, os.path.dirname(RUNTIME_DIR))
    imports = _extract_imports(filepath)

    violations = [mod for mod in imports if not _is_allowed_module(mod)]
    assert violations == [], (
        f"File '{rel_path}' imports non-stdlib third-party modules: {violations}"
    )


# ---------------------------------------------------------------------------
# Exhaustive unit test – checks every single file (not sampled)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filepath",
    _ALL_PY_FILES,
    ids=[os.path.relpath(p, os.path.dirname(RUNTIME_DIR)) for p in _ALL_PY_FILES],
)
def test_no_third_party_imports_exhaustive(filepath: str):
    """Unit test: verify each runtime .py file has zero third-party imports.

    **Validates: Requirements 7.1**
    """
    rel_path = os.path.relpath(filepath, os.path.dirname(RUNTIME_DIR))
    imports = _extract_imports(filepath)

    violations = [mod for mod in imports if not _is_allowed_module(mod)]
    assert violations == [], (
        f"File '{rel_path}' imports non-stdlib third-party modules: {violations}"
    )
