"""Root-level conftest for the Agent Service test suite."""

import os

import pytest


# ═══════════════════════════════════════════════════════════════════════
# pytest configuration
# ═══════════════════════════════════════════════════════════════════════

def pytest_configure(config):
    """Register custom markers for test classification."""
    config.addinivalue_line(
        "markers",
        "smoke: Fast tests that verify core functionality is not broken (CI fast lane).",
    )
    config.addinivalue_line(
        "markers",
        "regression: Full test suite for deeper validation (CI full + CD gate).",
    )
    _load_hypothesis_profile()


# Hypothesis profiles
# Property tests no longer hard-code ``max_examples``; the active profile does:
#   default: 100 examples  (spec minimum: "at least 100 iterations" per property)
#   full:    200 examples  (deeper gate, e.g. nightly / release)
#   fast:     25 examples  (local iteration)
# Select with:  HYPOTHESIS_PROFILE=fast pytest ...
def _load_hypothesis_profile() -> None:
    try:
        from hypothesis import HealthCheck, settings
    except ImportError:  # pragma: no cover - hypothesis is a dev dependency
        return
    # deadline=None everywhere: several properties exercise real subprocess /
    # file / HTTP paths, so per-example wall-clock varies with machine load
    # and the default 200 ms deadline produces load-dependent DeadlineExceeded
    # flakes. Timing guarantees are asserted explicitly where they matter.
    settings.register_profile("default", max_examples=100, deadline=None)
    settings.register_profile("full", max_examples=200, deadline=None)
    settings.register_profile(
        "fast", max_examples=25, deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    name = os.environ.get("HYPOTHESIS_PROFILE", "default").strip().lower()
    if name not in {"default", "full", "fast"}:
        name = "default"
    settings.load_profile(name)


# ── Automatic marker assignment by file name ──────────────────────────

# Smoke tests: fast critical-path tests (mock-based, no server, no heavy property tests)
_SMOKE_FILES = frozenset({
    "test_protocols_smoke.py",
    "test_models.py",
    "test_registry.py",
    "test_labels.py",
    "test_common_search_files.py",
    "test_setup_export_auth.py",
    "test_agent_labels_integration.py",
    "test_full_lifecycle.py",
    "test_env_session_properties.py",
    "test_skill_manager.py",
    "test_read_file_output_limits.py",
})


def pytest_collection_modifyitems(config, items):
    """Auto-apply smoke / regression markers based on file name.

    This avoids the need for per-file ``pytestmark`` boilerplate and
    keeps classification centralized.
    """
    for item in items:
        fname = item.location[0]  # e.g. "tests/test_models.py"
        base = fname.split("/")[-1] if "/" in fname else fname
        if base in _SMOKE_FILES:
            item.add_marker(pytest.mark.smoke)
        else:
            item.add_marker(pytest.mark.regression)


# ── Shared fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def project_root():
    """Absolute path to the repository root."""
    import os
    return os.path.dirname(os.path.abspath(__file__))
