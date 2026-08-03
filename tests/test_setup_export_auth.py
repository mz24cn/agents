from __future__ import annotations

import json
import tarfile
from io import BytesIO

from runtime.env_manager import EnvManager


def _read_payload_file(payload: bytes, suffix: str) -> bytes | None:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith(suffix):
                extracted = tar.extractfile(member)
                assert extracted is not None
                return extracted.read()
    return None


def _payload_contains(payload: bytes, suffix: str) -> bool:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        return any(member.name.endswith(suffix) for member in tar.getmembers())


def _build_payload(tmp_path, *, include_env: bool = False) -> tuple[bytes, dict, dict]:
    project_root = tmp_path / "project"
    data_dir = tmp_path / "agents_runtime"
    project_root.mkdir()
    data_dir.mkdir()

    auth_data = {
        "version": 1,
        "password_hash": "pbkdf2_sha256$260000$salt$digest",
        "api_key_hash": "sha256$salt$digest",
        "session_secret": "session-secret",
        "setup_secret": "setup-secret",
    }
    env_data = {"EXPORT_ME": "only_when_include_env_true"}
    (data_dir / "auth_token.json").write_text(json.dumps(auth_data), encoding="utf-8")
    (data_dir / "env.json").write_text(json.dumps(env_data), encoding="utf-8")

    manager = EnvManager(env_path=str(data_dir / "env.json"))
    payload = manager._build_setup_payload(
        project_root=str(project_root),
        data_dir=str(data_dir),
        runtime=None,
        prompt_template_manager=None,
        agent_manager=None,
        include_project=False,
        include_env=include_env,
    )
    return payload, auth_data, env_data


def test_setup_payload_exports_auth_token_json(tmp_path) -> None:
    payload, auth_data, _env_data = _build_payload(tmp_path, include_env=False)

    exported_auth = _read_payload_file(payload, "agents_runtime/auth_token.json")
    assert exported_auth is not None
    assert json.loads(exported_auth.decode("utf-8")) == auth_data
    assert not _payload_contains(payload, "agents_runtime/env.json")


def test_setup_payload_exports_env_json_when_requested(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTS_WORKSPACE", raising=False)
    payload, _auth_data, env_data = _build_payload(tmp_path, include_env=True)

    exported_env = _read_payload_file(payload, "agents_runtime/env.json")
    assert exported_env is not None
    assert json.loads(exported_env.decode("utf-8")) == env_data
