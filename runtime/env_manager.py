"""EnvManager — 环境变量管理模块。

负责读写 ~/.agents_runtime/env.json 文件，并将键值对同步到当前进程的 os.environ。
支持递归扫描 .py 文件以检测项目中已使用的环境变量。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import datetime
import base64
import json
import logging
import os
import re
import tarfile
import tempfile
import textwrap
from io import BytesIO

logger = logging.getLogger("runtime.env_manager")


class EnvManager:
    """管理 env.json 文件的读写及 os.environ 同步。

    Args:
        env_path: env.json 文件的完整路径。
    """

    def __init__(self, env_path: str) -> None:
        self._env_path = env_path

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def read(self) -> dict[str, str]:
        """从 env.json 读取所有键值对。

        Returns:
            包含所有环境变量键值对的字典。文件不存在时返回空字典。

        Raises:
            ValueError: 文件内容不是合法的 JSON 对象时抛出。
        """
        if not os.path.isfile(self._env_path):
            result: dict[str, str] = {}
            workspace = os.environ.get("AGENTS_WORKSPACE", "")
            if workspace:
                result["AGENTS_WORKSPACE"] = workspace
            return result
        try:
            with open(self._env_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"env.json 格式异常: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"无法读取 env.json: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"env.json 内容必须是 JSON 对象，实际类型: {type(data).__name__}"
            )
        result = {str(k): str(v) for k, v in data.items()}
        if "AGENTS_WORKSPACE" not in result:
            workspace = os.environ.get("AGENTS_WORKSPACE", "")
            if workspace:
                result["AGENTS_WORKSPACE"] = workspace
        return result

    def set(self, key: str, value: str) -> dict[str, str]:
        """新增或更新一个键值对，原子写入 env.json，并同步到 os.environ。"""
        try:
            env_map = self.read()
        except ValueError:
            env_map = {}
        env_map[key] = value
        content = json.dumps(env_map, ensure_ascii=False, indent=2)
        self._atomic_write(self._env_path, content)
        self._sync_to_environ(env_map)
        return env_map

    def delete(self, key: str) -> dict[str, str]:
        """删除指定 key，原子写入 env.json。key 不存在时静默忽略。"""
        try:
            env_map = self.read()
        except ValueError:
            env_map = {}
        env_map.pop(key, None)
        content = json.dumps(env_map, ensure_ascii=False, indent=2)
        self._atomic_write(self._env_path, content)
        return env_map

    def detect_used_keys(self, scan_dir: str) -> list[str]:
        """递归扫描 scan_dir 下所有 .py 文件，提取引用环境变量的 KEY。

        识别三种源码形式：

        * ``os.environ.get("KEY")``
        * ``env_int("KEY", default)``（runtime.common.env_int）
        * ``env_float("KEY", default)``（runtime.common.env_float）

        后两种是防御式数值解析 helper：调用点不再出现
        ``os.environ.get("...")`` 字面量，若正则不覆盖它们，
        这些 key 会从侦测结果中静默消失。
        """
        pattern = re.compile(r'(?:os\.environ\.get|env_int|env_float)\("(\w+)"')
        found: set[str] = set()
        for dirpath, _dirnames, filenames in os.walk(scan_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                filepath = os.path.join(dirpath, filename)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    matches = pattern.findall(content)
                    found.update(matches)
                except OSError as exc:
                    logger.warning("跳过不可读文件 %s: %s", filepath, exc)
        return list(found)

    def build_setup_script(
        self,
        *,
        project_root: str,
        data_dir: str,
        runtime: object | None = None,
        prompt_template_manager: object | None = None,
        agent_manager: object | None = None,
        include_project: bool = True,
        include_env: bool = False,
        script_format: str = "sh",
    ) -> bytes:
        """生成可通过 ``curl ... | sh`` 或 ``irm ... | iex`` 执行的自解压安装脚本。"""
        payload = self._build_setup_payload(
            project_root=project_root,
            data_dir=data_dir,
            runtime=runtime,
            prompt_template_manager=prompt_template_manager,
            agent_manager=agent_manager,
            include_project=include_project,
            include_env=include_env,
        )
        encoded = "\n".join(textwrap.wrap(base64.b64encode(payload).decode("ascii"), 76))
        fmt = script_format.lower().strip()
        if fmt in {"sh", "shell", "posix", "unix"}:
            return self._render_setup_script_sh(encoded).encode("utf-8")
        if fmt in {"ps1", "powershell", "pwsh"}:
            return self._render_setup_script_ps1(encoded).encode("utf-8")
        raise ValueError(f"Unsupported setup script format: {script_format}")

    def get_backend_build_mtime(self, project_root: str) -> float:
        """Return the newest deployable non-web project file timestamp.

        This intentionally includes accessories and non-Python extension assets
        such as SKILL.md or C sources, so ``op=hello`` advertises their updates.
        """
        project_root = os.path.realpath(project_root)
        web_root_real = os.path.realpath(os.path.join(project_root, "web"))
        exclude_dirs = {".git", "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
                        ".ruff_cache", "node_modules", "dist", "build", ".venv", "venv",
                        "workspace"}
        latest_mtime = 0.0
        for dirpath, dirnames, filenames in os.walk(project_root):
            dir_real = os.path.realpath(dirpath)
            dirnames[:] = [
                name for name in dirnames
                if (os.path.realpath(os.path.join(dir_real, name)) != web_root_real
                    and not (name.startswith(".") and os.path.isdir(os.path.join(dir_real, name)))
                    and name not in exclude_dirs
                    and not os.path.islink(os.path.join(dir_real, name)))
            ]
            for filename in filenames:
                fpath = os.path.join(dir_real, filename)
                try:
                    if not os.path.islink(fpath):
                        latest_mtime = max(latest_mtime, os.path.getmtime(fpath))
                except OSError:
                    continue
        return latest_mtime

    def build_delta_tar(
        self,
        *,
        project_root: str,
        data_dir: str,
        frontend_since: float,
        backend_since: float,
        config_since: float,
    ) -> bytes | None:
        """Build a minimal tar.gz delta using independent version thresholds."""
        project_root = os.path.realpath(project_root)
        data_dir = os.path.realpath(data_dir)
        web_dist_real = os.path.realpath(os.path.join(project_root, "web", "dist"))
        exclude_dirs = {".git", "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
                        ".ruff_cache", "node_modules", "dist", "build", ".venv", "venv",
                        "workspace"}

        def should_exclude(path_real: str, name: str) -> bool:
            if path_real == web_dist_real:
                return False
            if name.startswith(".") and os.path.isdir(path_real):
                return True
            return name in exclude_dirs

        collected: dict[str, str] = {}

        web_root_real = os.path.realpath(os.path.join(project_root, "web"))

        # Every deployable file under web/ (compiled output, Svelte sources,
        # public assets and build metadata) uses the frontend threshold.  Keeping
        # the sources in online updates is intentional: an installed instance
        # must remain self-contained enough to diagnose and patch frontend bugs.
        # All other included project files, including accessories/, use the
        # backend threshold. Every file is selected independently by mtime.
        for dirpath, dirnames, filenames in os.walk(project_root):
            dir_real = os.path.realpath(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not should_exclude(os.path.realpath(os.path.join(dir_real, d)), d)
            ]
            rel_dir = os.path.relpath(dir_real, project_root)
            if rel_dir != ".":
                first = rel_dir.replace("\\", "/").split("/")[0]
                first_path = os.path.realpath(os.path.join(project_root, first))
                if should_exclude(first_path, first):
                    dirnames.clear()
                    continue
            in_web = os.path.commonpath([web_root_real, dir_real]) == web_root_real
            threshold = frontend_since if in_web else backend_since
            for filename in filenames:
                fpath = os.path.join(dir_real, filename)
                try:
                    if os.path.islink(fpath):
                        continue
                    if int(os.path.getmtime(fpath)) <= int(threshold):
                        continue
                except OSError:
                    continue
                arcname = os.path.relpath(fpath, project_root).replace("\\", "/")
                collected[arcname] = fpath

        # Runtime configuration files use their own independent threshold.
        for filename in ("models.json", "tools.json", "mcp_servers.json", "prompt_templates.json"):
            fpath = os.path.join(data_dir, filename)
            try:
                if (os.path.isfile(fpath) and not os.path.islink(fpath)
                        and int(os.path.getmtime(fpath)) > int(config_since)):
                    collected[f"agents_runtime/{filename}"] = fpath
            except OSError:
                continue

        agents_dir = os.path.join(data_dir, "agents")
        if os.path.isdir(agents_dir):
            for dirpath, dirnames, filenames in os.walk(agents_dir):
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for filename in filenames:
                    if not filename.endswith(".json"):
                        continue
                    fpath = os.path.join(dirpath, filename)
                    try:
                        if os.path.islink(fpath) or int(os.path.getmtime(fpath)) <= int(config_since):
                            continue
                    except OSError:
                        continue
                    relative = os.path.relpath(fpath, agents_dir).replace("\\", "/")
                    collected[f"agents_runtime/agents/{relative}"] = fpath

        if not collected:
            return None

        bio = BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            for arcname, fpath in sorted(collected.items()):
                try:
                    tar.add(fpath, arcname=arcname, filter=self._setup_tar_filter)
                except OSError as exc:
                    logger.warning("增量打包跳过 %s: %s", fpath, exc)
        return bio.getvalue()

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _atomic_write(self, path: str, content: str) -> None:
        from runtime.common import atomic_write_text
        atomic_write_text(path, content)

    def _sync_to_environ(self, env_map: dict[str, str]) -> None:
        for k, v in env_map.items():
            os.environ[str(k)] = str(v)

    # ------------------------------------------------------------------
    # Setup script helpers
    # ------------------------------------------------------------------

    def _build_setup_payload(self, *, project_root: str, data_dir: str, runtime: object | None,
                             prompt_template_manager: object | None, agent_manager: object | None,
                             include_project: bool, include_env: bool) -> bytes:
        project_root = os.path.realpath(project_root)
        data_dir = os.path.realpath(data_dir)
        with tempfile.TemporaryDirectory(prefix="agent_setup_") as tmpdir:
            payload_root = os.path.join(tmpdir, "payload")
            app_dir = os.path.join(payload_root, "app")
            cfg_dir = os.path.join(payload_root, "agents_runtime")
            os.makedirs(app_dir, exist_ok=True)
            os.makedirs(cfg_dir, exist_ok=True)
            if include_project:
                self._copy_project(project_root, app_dir)
            self._write_runtime_configs(
                cfg_dir, data_dir=data_dir, project_root=project_root,
                runtime=runtime, prompt_template_manager=prompt_template_manager,
                agent_manager=agent_manager, include_env=include_env,
            )
            self._dump_json(os.path.join(payload_root, "manifest.json"), {
                "name": "agent-service-setup", "version": 1,
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            bio = BytesIO()
            with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                tar.add(payload_root, arcname=".", filter=self._setup_tar_filter)
            return bio.getvalue()

    def _setup_tar_filter(self, tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if tarinfo.issym() or tarinfo.islnk():
            target = getattr(tarinfo, "linkname", "")
            logger.warning("打包 setup payload 时跳过链接 %s -> %s", tarinfo.name, target)
            return None
        return tarinfo

    def _copy_project(self, src: str, dst: str) -> None:
        import shutil
        exclude_dirs = {".git", "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
                        ".ruff_cache", "node_modules", "dist", "build", ".venv", "venv",
                        "workspace", "docs"}
        exclude_files = {".DS_Store"}
        src_real = os.path.realpath(src)
        web_dist_real = os.path.join(src_real, "web", "dist")

        def should_exclude_dir(parent_dir: str, name: str) -> bool:
            path = os.path.join(parent_dir, name)
            path_real = os.path.realpath(path)
            if path_real == web_dist_real:
                return False
            if name.startswith(".") and os.path.isdir(path):
                return True
            return name in exclude_dirs

        def ignore(dirpath: str, names: list[str]) -> set[str]:
            ignored: set[str] = set()
            for name in names:
                path = os.path.join(dirpath, name)
                if name in exclude_files or should_exclude_dir(dirpath, name):
                    ignored.add(name)
                    continue
                if os.path.islink(path):
                    logger.warning("打包时跳过符号链接 %s -> %s", path, os.readlink(path))
                    ignored.add(name)
            return ignored

        for name in os.listdir(src):
            if name in exclude_files or should_exclude_dir(src, name):
                continue
            s = os.path.join(src, name); d = os.path.join(dst, name)
            try:
                if os.path.islink(s):
                    logger.warning("打包时跳过符号链接 %s -> %s", s, os.readlink(s))
                    continue
                if os.path.isdir(s):
                    shutil.copytree(s, d, symlinks=False, ignore=ignore)
                else:
                    shutil.copy2(s, d, follow_symlinks=True)
            except OSError as exc:
                logger.warning("打包时跳过 %s: %s", s, exc)

    def _write_runtime_configs(self, cfg_dir: str, *, data_dir: str, project_root: str, runtime: object | None,
                               prompt_template_manager: object | None,
                               agent_manager: object | None,
                               include_env: bool = False) -> None:
        if include_env:
            self._dump_json(os.path.join(cfg_dir, "env.json"), self.read())
        auth_path = os.path.join(data_dir, "auth_token.json")
        if os.path.isfile(auth_path):
            import shutil
            dst_auth_path = os.path.join(cfg_dir, "auth_token.json")
            shutil.copy2(auth_path, dst_auth_path)
            try:
                os.chmod(dst_auth_path, 0o600)
            except OSError:
                pass
        model_registry = getattr(runtime, "_model_registry", None)
        tool_registry = getattr(runtime, "_tool_registry", None)
        mcp_manager = getattr(runtime, "_mcp_manager", None)
        models = ([m.to_dict() for m in model_registry.list_all()]
                  if model_registry else self._read_json_file(os.path.join(data_dir, "models.json"), []))
        tools = ([t.to_dict() for t in tool_registry.list_all() if not getattr(t, "builtin", False)]
                 if tool_registry else self._read_json_file(os.path.join(data_dir, "tools.json"), []))
        self._dump_json(os.path.join(cfg_dir, "models.json"), models)
        mcp_cfg = self._read_json_file(os.path.join(data_dir, "mcp_servers.json"), {})
        if mcp_manager is not None:
            for attr in ("to_config", "get_config", "config"):
                obj = getattr(mcp_manager, attr, None)
                try:
                    if callable(obj):
                        mcp_cfg = obj(); break
                    if obj is not None:
                        mcp_cfg = obj; break
                except Exception:
                    pass
        self._dump_json(os.path.join(cfg_dir, "mcp_servers.json"), mcp_cfg)
        templates = ([t.to_dict() for t in prompt_template_manager.list_all()]
                     if prompt_template_manager else self._read_json_file(os.path.join(data_dir, "prompt_templates.json"), []))
        self._dump_json(os.path.join(cfg_dir, "prompt_templates.json"), templates)
        agents_dir = os.path.join(cfg_dir, "agents")
        os.makedirs(agents_dir, exist_ok=True)
        agents = agent_manager.list_all() if agent_manager else []
        if agents:
            for agent in agents:
                safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(agent.get("agent_id", "agent")))
                self._dump_json(os.path.join(agents_dir, f"{safe_id}.json"), agent)
        else:
            src_agents = os.path.join(data_dir, "agents")
            if os.path.isdir(src_agents):
                import shutil
                def ignore_links(dirpath: str, names: list[str]) -> set[str]:
                    ignored: set[str] = set()
                    for name in names:
                        path = os.path.join(dirpath, name)
                        if os.path.islink(path):
                            logger.warning("打包 agents 配置时跳过符号链接 %s -> %s", path, os.readlink(path))
                            ignored.add(name)
                    return ignored
                shutil.copytree(src_agents, agents_dir, dirs_exist_ok=True, symlinks=False, ignore=ignore_links)
        package_root_real = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                continue
            for key in ("function_file_path", "skill_dir"):
                pth = tool.get(key)
                if not pth or not os.path.exists(os.path.expanduser(pth)):
                    continue
                real = os.path.realpath(os.path.expanduser(pth))
                try:
                    common = os.path.commonpath([package_root_real, real])
                except ValueError:
                    common = ""
                if common != package_root_real:
                    logger.info(
                        "工具资源不在 env_manager.py 所在目录的父目录下，跳过打包但保留工具注册信息不变: "
                        "tool_id=%s, %s=%s。安装后相关工具或AI代理可能无法正常工作。",
                        tool.get("tool_id", ""), key, pth,
                    )
        self._dump_json(os.path.join(cfg_dir, "tools.json"), tools)

    def _read_json_file(self, path: str, default):
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        except Exception as exc:
            logger.warning("读取配置文件失败 %s: %s", path, exc)
        return default

    def _dump_json(self, path: str, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def _render_setup_script_sh(self, encoded_payload: str) -> str:
        return r"""#!/bin/sh
set -eu

AGENTS_HOME="$PWD/agents"
: "${AGENTS_RUNTIME_DIR:=$HOME/.agents_runtime}"
: "${START_AGENTS:=background}"

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/agent-service-setup.XXXXXX")
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

echo "Extracting agent service package..." >&2
base64 -d > "$TMPDIR/payload.tar.gz" <<'__AGENTS_TAR_GZ_BASE64__'
""" + encoded_payload + """
__AGENTS_TAR_GZ_BASE64__
mkdir -p "$TMPDIR/payload"
tar -xzf "$TMPDIR/payload.tar.gz" -C "$TMPDIR/payload"
find "$TMPDIR/payload" -type l -exec rm -f {} \\;

mkdir -p "$AGENTS_HOME" "$AGENTS_RUNTIME_DIR"
rm -rf "$AGENTS_HOME/web/dist"
[ ! -d "$TMPDIR/payload/app" ] || tar -C "$TMPDIR/payload/app" -cf - . | tar -C "$AGENTS_HOME" -xf -
[ ! -d "$TMPDIR/payload/agents_runtime" ] || tar -C "$TMPDIR/payload/agents_runtime" -cf - . | tar -C "$AGENTS_RUNTIME_DIR" -xf -

cat > "$PWD/start-agent-service.sh" <<'SH'
#!/bin/sh
set -eu
# 访问地址由 app.py 启动时解析：AGENTS_URL（如 https://domain:7988/，
# 可在 web 界面的环境变量设置中修改，重启后生效），默认 http://0.0.0.0:7988。
: "${AGENTS_URL:=}"
: "${AGENTS_RUNTIME_DIR:=$HOME/.agents_runtime}"
: "${AGENTS_LOG:=$AGENTS_RUNTIME_DIR/server.log}"
: "${START_AGENTS:=background}"
export AGENTS_URL AGENTS_RUNTIME_DIR AGENTS_LOG
mkdir -p "$AGENTS_RUNTIME_DIR"
AGENTS_DIR="$(cd "$(dirname "$0")/agents" >/dev/null 2>&1 && pwd)"

case "$START_AGENTS" in
background)
 nohup python3 "$AGENTS_DIR/app.py" >> "$AGENTS_LOG" 2>&1 &
  pid=$!
  # server.pid 由 app.py 在启动时写入（os.getpid()），这里等待其出现后读取，
  # 确保返回的是 python 进程的真实 PID，且与 PID 文件内容一致。
  _i=0
  while [ ! -f "$AGENTS_RUNTIME_DIR/server.pid" ] && [ "$_i" -lt 60 ]; do
    _i=$((_i + 1))
    sleep 0.5 2>/dev/null || sleep 1
  done
  if [ -f "$AGENTS_RUNTIME_DIR/server.pid" ]; then
    pid=$(cat "$AGENTS_RUNTIME_DIR/server.pid" 2>/dev/null || echo "$pid")
  fi
  echo "$pid"
  ;;
foreground)
 exec python3 "$AGENTS_DIR/app.py" 2>&1 | tee -a "$AGENTS_LOG"
  ;;
none)
  exit 0
  ;;
*)
  echo "Invalid START_AGENTS=$START_AGENTS. Expected: background, foreground, none" >&2
  exit 2
  ;;
esac
SH
chmod +x "$PWD/start-agent-service.sh"

cat > "$PWD/stop-agent-service.sh" <<'SH'
#!/bin/sh
set -eu
: "${AGENTS_RUNTIME_DIR:=$HOME/.agents_runtime}"
pid_file="$AGENTS_RUNTIME_DIR/server.pid"
if [ ! -f "$pid_file" ]; then
  echo "No pid file found: $pid_file" >&2
  exit 0
fi
pid=$(cat "$pid_file")
if [ -z "$pid" ]; then
  echo "Empty pid file: $pid_file" >&2
  exit 0
fi
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid" 2>/dev/null || true
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5 2>/dev/null || sleep 1
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  echo "Stopped pid: $pid" >&2
else
  echo "Process $pid not running" >&2
fi
rm -f "$pid_file"
SH
chmod +x "$PWD/stop-agent-service.sh"

case "$START_AGENTS" in
  background|foreground|none) ;;
  true) START_AGENTS=background ;;
  false) START_AGENTS=none ;;
  *)
    echo "Invalid START_AGENTS=$START_AGENTS. Expected: background, foreground, none" >&2
    exit 2
    ;;
esac

AGENTS_LOG="$AGENTS_RUNTIME_DIR/server.log"
export AGENTS_HOME AGENTS_LOG

echo "Agent service installed:" >&2
echo "  app:    $AGENTS_HOME" >&2
echo "  config: $AGENTS_RUNTIME_DIR" >&2
echo "  log:    $AGENTS_LOG" >&2

if [ -d "$AGENTS_RUNTIME_DIR/patch" ]; then
  echo "Applying patch from $AGENTS_RUNTIME_DIR/patch..." >&2
  cp -r "$AGENTS_RUNTIME_DIR/patch"/. "$AGENTS_HOME"/ 2>/dev/null || true
fi

# Record SETUP_SOURCE into env.json
_SETUP_SOURCE='__SETUP_SOURCE_URL__'
if [ -n "$_SETUP_SOURCE" ]; then
  _ENV_JSON="$AGENTS_RUNTIME_DIR/env.json"
  _PY="${PYTHON:-python3}"
  $_PY -c "
import json, os, sys
path, url = sys.argv[1], sys.argv[2]
if os.path.isfile(path):
    with open(path, 'r') as f:
        data = json.load(f)
else:
    data = {}
data['SETUP_SOURCE'] = url
with open(path, 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
" "$_ENV_JSON" "$_SETUP_SOURCE" 2>&1 || echo "Warning: failed to write SETUP_SOURCE to env.json" >&2
fi

case "$START_AGENTS" in
  background)
    pid=$("$PWD/start-agent-service.sh")
    echo "Agent service started in background, pid: $pid" >&2
    echo "Log: $AGENTS_LOG" >&2
    ;;
  foreground)
    "$PWD/start-agent-service.sh"
    ;;
  none)
    echo "Agent service not started because START_AGENTS=none" >&2
    ;;
esac
exit 0
"""

    def _render_setup_script_ps1(self, encoded_payload: str) -> str:
        """生成 PowerShell 自解压安装脚本（用于 `irm ... | iex`）。

        解压后只生成 .bat 文件，不生成 .ps1。
        - base64 用单引号 here-string (@'...'@) 承载，避免变量展开
        - bat 内容用 here-string 变量承载，支持多行
        - 文件拷贝用 robocopy 替代 tar 管道，Windows 兼容性更好
        """
        # ── start-agent-service.bat ──────────────────────────────
        # 用 PowerShell Start-Process -PassThru 启动 cmd /c python（cmd 负责日志重定向），
        # server.pid 由 app.py 在启动时写入 python 进程真实 PID，脚本等待其出现后读取。
        start_bat = (
            "@echo off\r\n"
            "setlocal enabledelayedexpansion\r\n"
            "rem 访问地址由 app.py 启动时解析: AGENTS_URL（如 https://domain:7988/），默认 http://0.0.0.0:7988\r\n"
            "if not defined AGENTS_RUNTIME_DIR set \"AGENTS_RUNTIME_DIR=%USERPROFILE%\\.agents_runtime\"\r\n"
            "if not defined AGENTS_LOG set \"AGENTS_LOG=%AGENTS_RUNTIME_DIR%\\server.log\"\r\n"
            "if not defined START_AGENTS set START_AGENTS=background\r\n"
            "rem Always use the agents directory next to this script; do not inherit a stale AGENTS_HOME.\r\n"
            "set \"AGENTS_HOME=%~dp0agents\"\r\n"
            "\r\n"
            "mkdir \"%AGENTS_RUNTIME_DIR%\" 2>nul\r\n"
            "\r\n"
            "if /i \"%START_AGENTS%\"==\"background\" goto :background\r\n"
            "if /i \"%START_AGENTS%\"==\"foreground\" goto :foreground\r\n"
            "if /i \"%START_AGENTS%\"==\"none\" goto :none\r\n"
            "echo Invalid START_AGENTS=%START_AGENTS%. Expected: background, foreground, none 1>&2\r\n"
            "exit /b 2\r\n"
            "\r\n"
            ":foreground\r\n"
            "python \"%AGENTS_HOME%\\app.py\"\r\n"
            "exit /b %ERRORLEVEL%\r\n"
            "\r\n"
            ":none\r\n"
            "echo Agent service not started because START_AGENTS=none\r\n"
            "exit /b 0\r\n"
            "\r\n"
            ":background\r\n"
            "rem Remove a stale PID so startup success cannot be reported from an old process.\r\n"
            "del \"%AGENTS_RUNTIME_DIR%\\server.pid\" 2>nul\r\n"
            "set \"PS_START=%TEMP%\\agent-start-%RANDOM%-%RANDOM%.ps1\"\r\n"
            "> \"%PS_START%\" echo $ErrorActionPreference = 'Stop'\r\n"
            ">> \"%PS_START%\" echo $p = Start-Process -FilePath 'python' -ArgumentList '\"%AGENTS_HOME%\\app.py\"' -RedirectStandardOutput '%AGENTS_LOG%' -RedirectStandardError '%AGENTS_LOG%.err' -PassThru -WindowStyle Hidden\r\n"
            ">> \"%PS_START%\" echo $p.Id\r\n"
            "powershell -NoProfile -ExecutionPolicy Bypass -File \"%PS_START%\"\r\n"
            "set \"START_RC=%ERRORLEVEL%\"\r\n"
            "del \"%PS_START%\" 2>nul\r\n"
            "if not \"%START_RC%\"==\"0\" (\r\n"
            "    echo Failed to create background process, PowerShell exit code: %START_RC% 1>&2\r\n"
            "    exit /b %START_RC%\r\n"
            ")\r\n"
            "for /l %%i in (1,1,60) do (\r\n"
            "    if exist \"%AGENTS_RUNTIME_DIR%\\server.pid\" goto :check_process\r\n"
            "    ping -n 1 -w 500 127.0.0.1 >nul\r\n"
            ")\r\n"
            "echo Agent service did not create %AGENTS_RUNTIME_DIR%\\server.pid 1>&2\r\n"
            "echo Logs: %AGENTS_LOG% and %AGENTS_LOG%.err 1>&2\r\n"
            "exit /b 1\r\n"
            "\r\n"
            ":check_process\r\n"
            "set \"PID=\"\r\n"
            "set /p PID=<\"%AGENTS_RUNTIME_DIR%\\server.pid\"\r\n"
            "ping -n 2 -w 500 127.0.0.1 >nul\r\n"
            "tasklist /FI \"PID eq %PID%\" /NH 2>nul | findstr /R /C:\"[ ]%PID%[ ]\" >nul\r\n"
            "if errorlevel 1 (\r\n"
            "    echo Agent service process %PID% exited during startup. 1>&2\r\n"
            "    echo Logs: %AGENTS_LOG% and %AGENTS_LOG%.err 1>&2\r\n"
            "    exit /b 1\r\n"
            ")\r\n"
            "echo Agent service started in background, pid: %PID%\r\n"
            "echo Log: %AGENTS_LOG%\r\n"
            "exit /b 0\r\n"
        )
        # ── stop-agent-service.bat ───────────────────────────────
        # 读取 server.pid，用 taskkill /T (tree) 终止整个进程树（cmd.exe + python）。
        stop_bat = (
            "@echo off\r\n"
            "setlocal\r\n"
            "if not defined AGENTS_RUNTIME_DIR set \"AGENTS_RUNTIME_DIR=%USERPROFILE%\\.agents_runtime\"\r\n"
            "set \"PID_FILE=%AGENTS_RUNTIME_DIR%\\server.pid\"\r\n"
            "if not exist \"%PID_FILE%\" (\r\n"
            "    echo No pid file found: %PID_FILE%\r\n"
            "    exit /b 0\r\n"
            ")\r\n"
            "set /p PID=<\"%PID_FILE%\"\r\n"
            "if \"%PID%\"==\"\" (\r\n"
            "    echo Empty pid file: %PID_FILE%\r\n"
            "    exit /b 0\r\n"
            ")\r\n"
            "taskkill /T /F /PID %PID% 2>nul\r\n"
            "if %errorlevel%==0 (\r\n"
            "    echo Stopped process tree: %PID%\r\n"
            ") else (\r\n"
            "    echo Process %PID% not running\r\n"
            ")\r\n"
            "del \"%PID_FILE%\" 2>nul\r\n"
            "endlocal\r\n"
        )
        return (
            "$ErrorActionPreference = 'Stop'\n"
            "\n"
            "$AgentServiceHome = Join-Path $PWD 'agents'\n"
           "if (-not $env:AGENTS_RUNTIME_DIR) { $env:AGENTS_RUNTIME_DIR = Join-Path $HOME '.agents_runtime' }\n"
            "if (-not $env:START_AGENTS) { $env:START_AGENTS = 'background' }\n"
            "\n"
            "$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ('agent-setup-' + [guid]::NewGuid().ToString('N').Substring(0,8))\n"
            "New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null\n"
            "try {\n"
            "    Write-Host 'Extracting agent service package...' -ForegroundColor Cyan\n"
            "    $b64 = @'\n"
            + encoded_payload + "\n"
            "'@\n"
            "    $b64Clean = ($b64 -replace '\\s+', '')\n"
            "    $bytes = [Convert]::FromBase64String($b64Clean)\n"
            "    $tarGz = Join-Path $tmpDir 'payload.tar.gz'\n"
            "    [IO.File]::WriteAllBytes($tarGz, $bytes)\n"
            "\n"
            "    $payloadDir = Join-Path $tmpDir 'payload'\n"
            "    New-Item -ItemType Directory -Path $payloadDir -Force | Out-Null\n"
            "    tar -xzf $tarGz -C $payloadDir 2>&1 | Out-Null\n"
            "    if ($LASTEXITCODE -ne 0) { throw \"tar extraction failed (exit code $LASTEXITCODE)\" }\n"
            "\n"
            "    Get-ChildItem -Path $payloadDir -Recurse -Force |\n"
            "        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |\n"
            "        ForEach-Object { Remove-Item $_.FullName -Force -EA SilentlyContinue }\n"
            "\n"
            "    New-Item -ItemType Directory -Path $AgentServiceHome -Force | Out-Null\n"
            "    New-Item -ItemType Directory -Path $env:AGENTS_RUNTIME_DIR -Force | Out-Null\n"
            "    Remove-Item -Path (Join-Path $AgentServiceHome 'web\\dist') -Recurse -Force -ErrorAction SilentlyContinue\n"
            "    $appSrc = Join-Path $payloadDir 'app'\n"
            "    if (Test-Path $appSrc) {\n"
            "        robocopy $appSrc $AgentServiceHome /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null\n"
            "    }\n"
            "    $cfgSrc = Join-Path $payloadDir 'agents_runtime'\n"
            "    if (Test-Path $cfgSrc) {\n"
            "        robocopy $cfgSrc $env:AGENTS_RUNTIME_DIR /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null\n"
            "    }\n"
            "\n"
            "    $startBat = @'\n"
            + start_bat +
            "'@\n"
            "    $stopBat = @'\n"
            + stop_bat +
            "'@\n"
            "    Set-Content -Path (Join-Path $PWD 'start-agent-service.bat') -Value $startBat -Encoding ASCII\n"
            "    Set-Content -Path (Join-Path $PWD 'stop-agent-service.bat')  -Value $stopBat  -Encoding ASCII\n"
            "\n"
            "    $logFile = if ($env:AGENTS_LOG) { $env:AGENTS_LOG } else { Join-Path $env:AGENTS_RUNTIME_DIR 'server.log' }\n"
            "    Write-Host ''\n"
            "    Write-Host 'Agent service installed:' -ForegroundColor Green\n"
            "    Write-Host \"  app:    $AgentServiceHome\"\n"
            "    Write-Host \"  config: $($env:AGENTS_RUNTIME_DIR)\"\n"
            "    Write-Host \"  log:    $logFile\"\n"
            "\n"
            "    $patchDir = Join-Path $env:AGENTS_RUNTIME_DIR 'patch'\n"
            "    if (Test-Path $patchDir) {\n"
            "        Write-Host \"Applying patch from $patchDir...\"\n"
            "        Copy-Item -Path (Join-Path $patchDir '*') -Destination $AgentServiceHome -Recurse -Force -ErrorAction SilentlyContinue\n"
            "    }\n"
            "\n"
            "    # Record SETUP_SOURCE into env.json\n"
            "    $setupSource = '__SETUP_SOURCE_URL__'\n"
            "    if ($setupSource) {\n"
            "        $envJson = Join-Path $env:AGENTS_RUNTIME_DIR 'env.json'\n"
            "        if (Test-Path $envJson) {\n"
            "            $data = Get-Content $envJson -Raw | ConvertFrom-Json\n"
            "        } else {\n"
            "            $data = New-Object PSObject\n"
            "        }\n"
            "        $data | Add-Member -NotePropertyName 'SETUP_SOURCE' -NotePropertyValue $setupSource -Force\n"
            "        $data | ConvertTo-Json -Depth 10 | Set-Content $envJson -Encoding UTF8\n"
            "    }\n"
            "\n"
            "    if ($env:START_AGENTS -eq 'background') {\n"
            "        $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'start-agent-service.bat' -PassThru -WindowStyle Hidden\n"
            "        Write-Host \"Agent service started in background, pid: $($proc.Id)\" -ForegroundColor Green\n"
            "        Write-Host \"Log: $logFile\"\n"
            "    } elseif ($env:START_AGENTS -eq 'foreground') {\n"
            "        & (Join-Path $PWD 'start-agent-service.bat')\n"
            "    } elseif ($env:START_AGENTS -eq 'none') {\n"
            "        Write-Host 'Agent service not started because START_AGENTS=none'\n"
            "    }\n"
            "} finally {\n"
            "    Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue\n"
            "}\n"
        )
