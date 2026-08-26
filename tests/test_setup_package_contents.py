"""Regression tests for the source files exported by /v1/setup and op=delta."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from runtime.env_manager import EnvManager


def _tar_names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return {member.name.lstrip("./") for member in archive.getmembers() if member.isfile()}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sample_project(tmp_path: Path) -> tuple[Path, Path, EnvManager]:
    project = tmp_path / "project"
    data = tmp_path / "data"
    data.mkdir()
    _write(project / "app.py", "print('app')\n")
    _write(project / "runtime" / "server.py", "# backend\n")
    _write(project / "accessories" / "extension.py", "# extension\n")
    _write(project / "accessories" / "agent-service" / "SKILL.md", "# skill\n")
    _write(project / "web" / "src" / "App.svelte", "<h1>source</h1>\n")
    _write(project / "web" / "public" / "logo.svg", "<svg/>\n")
    _write(project / "web" / "index.html", "<div id='app'></div>\n")
    _write(project / "web" / "package.json", "{}\n")
    _write(project / "web" / "vite.config.js", "export default {}\n")
    _write(project / "web" / "dist" / "index.html", "<h1>built</h1>\n")
    _write(project / "web" / "node_modules" / "ignored.js", "ignored\n")
    return project, data, EnvManager(str(data / "env.json"))


def test_full_setup_payload_contains_frontend_sources_and_accessories(tmp_path: Path) -> None:
    project, data, manager = _sample_project(tmp_path)

    payload = manager._build_setup_payload(
        project_root=str(project),
        data_dir=str(data),
        runtime=None,
        prompt_template_manager=None,
        agent_manager=None,
        include_project=True,
        include_env=False,
    )
    names = _tar_names(payload)

    assert "app/web/src/App.svelte" in names
    assert "app/web/public/logo.svg" in names
    assert "app/web/index.html" in names
    assert "app/web/package.json" in names
    assert "app/web/vite.config.js" in names
    assert "app/web/dist/index.html" in names
    assert "app/accessories/extension.py" in names
    assert "app/accessories/agent-service/SKILL.md" in names
    assert "app/web/node_modules/ignored.js" not in names


def test_delta_contains_changed_frontend_sources_and_accessories(tmp_path: Path) -> None:
    project, data, manager = _sample_project(tmp_path)
    old = 1_000_000_000
    new = 2_000_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (new, new))

    delta = manager.build_delta_tar(
        project_root=str(project),
        data_dir=str(data),
        frontend_since=old,
        backend_since=old,
        config_since=old,
    )
    assert delta is not None
    names = _tar_names(delta)

    assert "web/src/App.svelte" in names
    assert "web/public/logo.svg" in names
    assert "web/index.html" in names
    assert "web/package.json" in names
    assert "web/vite.config.js" in names
    assert "web/dist/index.html" in names
    assert "accessories/extension.py" in names
    assert "accessories/agent-service/SKILL.md" in names
    assert "web/node_modules/ignored.js" not in names


def test_backend_build_mtime_includes_accessories_assets_but_not_web(tmp_path: Path) -> None:
    project, _data, manager = _sample_project(tmp_path)
    old = 1_500_000_000
    accessory_new = 1_700_000_000
    web_newest = 2_000_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    accessory_skill = project / "accessories" / "agent-service" / "SKILL.md"
    os.utime(accessory_skill, (accessory_new, accessory_new))
    os.utime(project / "web" / "src" / "App.svelte", (web_newest, web_newest))

    assert manager.get_backend_build_mtime(str(project)) == accessory_new


def test_delta_sends_only_changed_web_and_accessories_files(tmp_path: Path) -> None:
    project, data, manager = _sample_project(tmp_path)
    old = 1_000_000_000
    changed = 2_000_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    os.utime(project / "web" / "dist" / "index.html", (changed, changed))
    os.utime(project / "accessories" / "extension.py", (changed, changed))

    delta = manager.build_delta_tar(
        project_root=str(project),
        data_dir=str(data),
        frontend_since=1_500_000_000,
        backend_since=1_500_000_000,
        config_since=1_500_000_000,
    )
    assert delta is not None
    names = _tar_names(delta)

    # Both web/ and accessories/ are normal per-file deltas. The receiver clears
    # web/dist before extraction, so unchanged or obsolete build files need not
    # be included as a compatibility snapshot.
    assert "web/src/App.svelte" not in names
    assert "web/public/logo.svg" not in names
    assert "web/dist/index.html" in names
    assert "accessories/extension.py" in names
    assert "accessories/agent-service/SKILL.md" not in names


def test_delta_uses_frontend_threshold_for_all_web_files(tmp_path: Path) -> None:
    project, data, manager = _sample_project(tmp_path)
    web_mtime = 1_500_000_000
    backend_mtime = 2_000_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (web_mtime, web_mtime))
    # Keep accessories unchanged here; this test isolates threshold selection
    # for web files versus ordinary backend files.
    os.utime(project / "runtime" / "server.py", (backend_mtime, backend_mtime))

    delta = manager.build_delta_tar(
        project_root=str(project),
        data_dir=str(data),
        frontend_since=1_600_000_000,
        backend_since=1_600_000_000,
        config_since=1_000_000_000,
    )
    assert delta is not None
    names = _tar_names(delta)

    assert "runtime/server.py" in names
    assert "accessories/extension.py" not in names
    assert not any(name.startswith("web/") for name in names)
