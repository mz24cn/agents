"""Regression tests for the source files exported by /v1/setup and op=delta."""

from __future__ import annotations

import datetime
import io
import json
import os
import tarfile
from pathlib import Path

from runtime.env_manager import EnvManager, compute_setup_versions, frontend_build_version


def _tar_names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return {member.name.lstrip("./") for member in archive.getmembers() if member.isfile()}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _stamp(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).strftime("%y%m%d_%H%M%S")


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


def test_full_setup_payload_excludes_dev_only_trees(tmp_path: Path) -> None:
    """docs/, tests/ and examples/ must never enter the full setup payload.

    An installed instance only needs what runs the service; shipping the
    test suite and example scripts added ~1.3 MB (uncompressed) to every
    full setup download for no runtime benefit.
    """
    project, data, manager = _sample_project(tmp_path)
    _write(project / "tests" / "test_backend.py", "def test_x():\n    pass\n")
    _write(project / "examples" / "demo.py", "print('demo')\n")
    _write(project / "docs" / "guide.md", "# guide\n")

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

    assert not any(name.startswith("app/tests/") for name in names)
    assert not any(name.startswith("app/examples/") for name in names)
    assert not any(name.startswith("app/docs/") for name in names)
    # Deployable trees are still shipped.
    assert "app/runtime/server.py" in names
    assert "app/web/src/App.svelte" in names
    assert "app/accessories/extension.py" in names


def test_delta_excludes_dev_only_trees_even_when_newest(tmp_path: Path) -> None:
    """A delta carries only deployable files, whatever their mtime.

    docs/ used to be missing from the delta's exclude list, so an edited
    document could drag ~17 MB of docs into an update delta.
    """
    project, data, manager = _sample_project(tmp_path)
    old = 1_000_000_000
    new = 2_000_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))

    # Dev-only files crossing the threshold must not be collected ...
    _write(project / "tests" / "test_new.py", "def test_y():\n    pass\n")
    _write(project / "examples" / "new_demo.py", "print(1)\n")
    _write(project / "docs" / "new.md", "# new\n")
    # ... but a real change next to them must still be delivered.
    _write(project / "runtime" / "runtime.py", "# backend v2\n")
    for path in (project / "tests" / "test_new.py",
                 project / "examples" / "new_demo.py",
                 project / "docs" / "new.md",
                 project / "runtime" / "runtime.py"):
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
    assert "runtime/runtime.py" in names
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.startswith("examples/") for name in names)
    assert not any(name.startswith("docs/") for name in names)


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


def test_backend_build_mtime_ignores_dev_only_trees(tmp_path: Path) -> None:
    """Editing docs/tests/examples must not bump the advertised build.

    Those trees are not shipped, so a version bump they cause would
    advertise an update that carries no deployable file.
    """
    project, _data, manager = _sample_project(tmp_path)
    old = 1_500_000_000
    dev_new = 1_700_000_000
    code_new = 1_900_000_000

    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    _write(project / "docs" / "new.md", "# new\n")
    _write(project / "tests" / "test_new.py", "def test_z():\n    pass\n")
    os.utime(project / "docs" / "new.md", (dev_new, dev_new))
    os.utime(project / "tests" / "test_new.py", (dev_new, dev_new))

    assert manager._scan_backend_build_mtime(str(project)) == old
    os.utime(project / "runtime" / "server.py", (code_new, code_new))
    assert manager._scan_backend_build_mtime(str(project)) == code_new


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

    # web/ sources and accessories/ are per-file deltas.  web/dist is the
    # exception: the receiver replaces that directory wholesale, so a selected
    # dist file drags the complete dist along.  Here index.html is the only
    # file in the fixture's dist, so per-file and whole-dist agree.
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


def test_delta_ships_the_whole_dist_when_one_dist_file_changed(tmp_path: Path) -> None:
    """web/dist is replaced wholesale on the receiver, so it must arrive whole.

    Only the newest asset crosses the threshold here; the delta still has to
    carry index.html and the previous hashes, because applying it deletes the
    child's whole dist first.
    """
    project, data, manager = _sample_project(tmp_path)
    dist = project / "web" / "dist"
    _write(dist / "assets" / "index-new.js", "new\n")
    _write(dist / "assets" / "index-obsolete.js", "obsolete\n")

    old = 1_000_000_000
    changed = 1_700_000_000
    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    os.utime(dist / "assets" / "index-new.js", (changed, changed))

    delta = manager.build_delta_tar(
        project_root=str(project),
        data_dir=str(data),
        frontend_since=1_500_000_000,
        backend_since=0,
        config_since=0,
    )
    assert delta is not None
    names = _tar_names(delta)

    assert "web/dist/assets/index-new.js" in names
    # The rest of the dist has to travel too, even though it is older than the
    # baseline: the receiver never merges into an existing dist.
    assert "web/dist/index.html" in names
    assert "web/dist/assets/index-obsolete.js" in names
    # Non-dist web files stay per-file deltas.
    assert "web/src/App.svelte" not in names


def test_frontend_build_version_follows_dist_files_not_the_stamp(tmp_path: Path) -> None:
    """A build_version stamp newer than the dist must not be advertised.

    A failed frontend build still refreshes web/dist/build_version while
    leaving the previous dist in place.  Trusting that stamp made the
    environment report a frontend build that no file backed, and the pushed
    delta then carried the lone stamp — which deleted the child's frontend.
    """
    project, _data, _manager = _sample_project(tmp_path)
    dist = project / "web" / "dist"
    built = 1_600_000_000
    _write(dist / "index.html", "<script src=\"/assets/index-abc.js\"></script>\n")
    _write(dist / "assets" / "index-abc.js", "console.log(1)\n")
    _write(dist / "build_version", "251231_235959")
    for path in (dist / "index.html", dist / "assets" / "index-abc.js"):
        os.utime(path, (built, built))
    os.utime(dist / "build_version", (built + 600, built + 600))

    assert frontend_build_version(str(project)) == _stamp(built + 600)
    assert frontend_build_version(str(project)) != "251231_235959"


def test_frontend_build_version_empty_without_index_html(tmp_path: Path) -> None:
    """An environment that cannot serve "/" reports no version, so it heals."""
    project, _data, _manager = _sample_project(tmp_path)
    (project / "web" / "dist" / "index.html").unlink()

    assert frontend_build_version(str(project)) == ""


def test_frontend_build_version_empty_when_referenced_asset_is_missing(tmp_path: Path) -> None:
    project, _data, _manager = _sample_project(tmp_path)
    _write(
        project / "web" / "dist" / "index.html",
        "<script src=\"/assets/index-gone.js\"></script>\n",
    )

    assert frontend_build_version(str(project)) == ""


def test_compute_setup_versions_derives_frontend_from_dist(tmp_path: Path) -> None:
    project, data, manager = _sample_project(tmp_path)
    built = 1_600_000_000
    os.utime(project / "web" / "dist" / "index.html", (built, built))

    versions = compute_setup_versions(manager, str(data), project_root=str(project))

    assert versions["frontend_build"] == _stamp(built)


class _StubAgentManager:
    """Stand-in for AgentManager: agents come from ``list_all``, not from a copy."""

    def __init__(self, *agents: dict) -> None:
        self._agents = list(agents)

    def list_all(self) -> list[dict]:
        return [dict(agent) for agent in self._agents]


class _StubEntry:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


class _StubRegistry:
    """Stand-in for ModelRegistry/ToolRegistry holding un-persisted state."""

    def __init__(self, *entries: _StubEntry) -> None:
        self._entries = list(entries)

    def list_all(self) -> list[_StubEntry]:
        return list(self._entries)


class _StubRuntime:
    def __init__(self, model_registry=None, tool_registry=None) -> None:
        self._model_registry = model_registry
        self._tool_registry = tool_registry


def _payload_mtime(payload: bytes, arcname: str) -> float:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.name.lstrip("./") == arcname:
                return member.mtime
    raise AssertionError(f"{arcname} is not in the payload")


def _extract_config_tree(payload: bytes, target: Path) -> Path:
    """Unpack the payload's ``agents_runtime/`` tree the way the script does."""
    target.mkdir(parents=True, exist_ok=True)
    # 3.12 added the extract filter; the default warns from 3.12 on and changes
    # in 3.14.  Both filters keep file mtimes, which is what is under test.
    kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(
            target,
            members=[m for m in archive.getmembers()
                     if m.name.lstrip("./").startswith("agents_runtime/")],
            **kwargs,
        )
    return target / "agents_runtime"


def test_full_setup_payload_keeps_config_file_mtimes(tmp_path: Path) -> None:
    """Installing must not restamp ~/.agents_runtime — there, mtime *is* the version.

    Config files are re-serialized from the live registries rather than copied,
    so they used to arrive stamped with the packaging instant.  That instant
    became the child's advertised ``last_config``, which doubles as the baseline
    of every later config delta: no config file on the parent ever cleared the
    ``mtime > config_since`` filter again, and a pushed delta was refused
    outright by the per-axis downgrade guard -- so the install had cut the child
    off from its own update source.  Project files were never affected, because
    ``_copy_project`` copies metadata along.
    """
    project, data, manager = _sample_project(tmp_path)
    built = 1_000_000_000
    sources = {
        "env.json": '{"APP_TITLE": "source"}',
        "models.json": "[]",
        "tools.json": "[]",
        "mcp_servers.json": "{}",
        "prompt_templates.json": "[]",
        "agents/a1.json": '{"agent_id": "a1", "name": "one"}',
    }
    stamps = {name: built + 10 * index for index, name in enumerate(sorted(sources))}
    for name, content in sources.items():
        _write(data / name, content + "\n")
        os.utime(data / name, (stamps[name], stamps[name]))

    payload = manager._build_setup_payload(
        project_root=str(project),
        data_dir=str(data),
        runtime=None,
        prompt_template_manager=None,
        # The branch that re-serializes agents instead of copying their files.
        agent_manager=_StubAgentManager({"agent_id": "a1", "name": "one"}),
        include_project=True,
        include_env=True,
    )

    for name in sources:
        assert _payload_mtime(payload, f"agents_runtime/{name}") == stamps[name], name


def test_installed_environment_advertises_its_sources_config_version(tmp_path: Path) -> None:
    """What ``curl … | sh`` writes into ~/.agents_runtime must not outdate its source.

    Here only ``prompt_templates.json`` exists on disk; the rest is shipped as
    an empty structure, or (for models) as state the source never persisted.
    Stamping those with "now" was enough to leave the child permanently ahead of
    the parent on the config axis, so it falls back to the instant the parent
    already advertises.
    """
    project, data, manager = _sample_project(tmp_path)
    built = 1_000_000_000
    _write(data / "prompt_templates.json", "[]\n")
    os.utime(data / "prompt_templates.json", (built, built))
    parent = compute_setup_versions(manager, str(data), project_root=str(project))
    assert parent["last_config"] == _stamp(built)

    payload = manager._build_setup_payload(
        project_root=str(project),
        data_dir=str(data),
        runtime=_StubRuntime(model_registry=_StubRegistry(_StubEntry({"model_id": "m"}))),
        prompt_template_manager=None,
        agent_manager=None,
        include_project=True,
        include_env=False,
    )

    child_data = _extract_config_tree(payload, tmp_path / "install")
    child = compute_setup_versions(
        EnvManager(str(child_data / "env.json")), str(child_data),
        project_root=str(project),
    )

    assert child["last_config"] == parent["last_config"]
    # Sanity: the shipped files really are there, so the equality is not two
    # empty directories agreeing by accident.
    assert (child_data / "models.json").is_file()
    assert json.loads((child_data / "models.json").read_text(encoding="utf-8"))
