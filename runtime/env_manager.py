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
            return {}
        try:
            with open(self._env_path, "r", encoding="utf-8") as fh:
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
        # 如果 env.json 中没有 AGENT_WORKSPACE，从 os.environ 中补充（app.py 启动时已保证 environ 中有值）
        if "AGENT_WORKSPACE" not in result:
            workspace = os.environ.get("AGENT_WORKSPACE", "")
            if workspace:
                result["AGENT_WORKSPACE"] = workspace
        return result

    def set(self, key: str, value: str) -> dict[str, str]:
        """新增或更新一个键值对，原子写入 env.json，并同步到 os.environ。

        Args:
            key: 环境变量名。
            value: 环境变量值。

        Returns:
            更新后的完整键值对字典。

        Raises:
            OSError: 文件写入失败时抛出。
        """
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
        """删除指定 key，原子写入 env.json。key 不存在时静默忽略。

        Args:
            key: 要删除的环境变量名。

        Returns:
            更新后的完整键值对字典。

        Raises:
            OSError: 文件写入失败时抛出。
        """
        try:
            env_map = self.read()
        except ValueError:
            env_map = {}
        env_map.pop(key, None)
        content = json.dumps(env_map, ensure_ascii=False, indent=2)
        self._atomic_write(self._env_path, content)
        return env_map

    def detect_used_keys(self, scan_dir: str) -> list[str]:
        """递归扫描 scan_dir 下所有 .py 文件，提取 os.environ.get( "KEY" ) 中的 KEY。

        使用正则 ``os\\.environ\\.get\\("(\\w+)"`` 匹配，返回去重后的列表。
        无法读取的文件会被跳过并记录日志。

        Args:
            scan_dir: 要扫描的根目录路径。

        Returns:
            去重后的环境变量 key 列表。
        """
        pattern = re.compile(r'os\.environ\.get\("(\w+)"')
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
        script_format: str = "sh",
    ) -> bytes:
        """生成可通过 ``curl ... | sh`` 或 ``irm ... | iex`` 执行的自解压安装脚本。

        脚本内嵌一个 tar.gz 载荷，包含当前 agent service 代码以及服务端
        已注册的模型、工具、MCP server、提示词模板、智能体配置和 env.json。
        """
        payload = self._build_setup_payload(
            project_root=project_root,
            data_dir=data_dir,
            runtime=runtime,
            prompt_template_manager=prompt_template_manager,
            agent_manager=agent_manager,
            include_project=include_project,
        )
        encoded = "\n".join(textwrap.wrap(base64.b64encode(payload).decode("ascii"), 76))
        fmt = script_format.lower().strip()
        if fmt in {"ps1", "powershell", "windows"}:
            return self._render_setup_script_ps1(encoded).encode("utf-8")
        if fmt not in {"sh", "shell", "posix", "unix"}:
            raise ValueError(f"Unsupported setup script format: {script_format}")
        return self._render_setup_script_sh(encoded).encode("utf-8")

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _atomic_write(self, path: str, content: str) -> None:
        """原子写入：先写临时文件，再 os.replace。

        Args:
            path: 目标文件路径。
            content: 要写入的文本内容。

        Raises:
            OSError: 写入或替换失败时抛出。
        """
        from runtime.common import atomic_write_text

        atomic_write_text(path, content)

    def _sync_to_environ(self, env_map: dict[str, str]) -> None:
        """将 env_map 中所有键值对写入 os.environ。

        Args:
            env_map: 要同步的键值对字典。
        """
        for k, v in env_map.items():
            os.environ[str(k)] = str(v)

    # ------------------------------------------------------------------
    # Setup script helpers
    # ------------------------------------------------------------------

    def _build_setup_payload(self, *, project_root: str, data_dir: str, runtime: object | None,
                             prompt_template_manager: object | None, agent_manager: object | None,
                             include_project: bool) -> bytes:
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
            self._write_runtime_configs(cfg_dir, data_dir=data_dir, project_root=project_root, runtime=runtime,
                                        prompt_template_manager=prompt_template_manager,
                                        agent_manager=agent_manager)
            self._dump_json(os.path.join(payload_root, "manifest.json"), {
                "name": "agent-service-setup", "version": 1,
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            bio = BytesIO()
            with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                tar.add(payload_root, arcname=".", filter=self._setup_tar_filter)
            return bio.getvalue()

    def _setup_tar_filter(self, tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """过滤 setup payload 中不应包含的 tar 条目。"""
        if tarinfo.issym() or tarinfo.islnk():
            target = getattr(tarinfo, "linkname", "")
            logger.warning("打包 setup payload 时跳过链接 %s -> %s", tarinfo.name, target)
            return None
        return tarinfo

    def _copy_project(self, src: str, dst: str) -> None:
        import shutil
        exclude_dirs = {".git", "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
                        ".ruff_cache", "node_modules", "dist", "build", ".venv", "venv"}
        exclude_files = {".DS_Store"}
        src_real = os.path.realpath(src)
        web_dist_real = os.path.join(src_real, "web", "dist")

        def should_exclude_dir(parent_dir: str, name: str) -> bool:
            path = os.path.join(parent_dir, name)
            path_real = os.path.realpath(path)
            # web/dist 是前端编译产物，部署后用于提供 Web UI，必须随源码一起打包。
            if path_real == web_dist_real:
                return False
            # Agent Service 部署目录下不应包含任何点号开头的目录（例如 .vscode、.git、.hypothesis）。
            # 注意：运行时配置目录 payload/agents_runtime 会安装到 AGENTS_RUNTIME_DIR，不在部署目录下，不能因此排除。
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
                # Windows 自带 tar.exe 对 Linux/Unix 符号链接，尤其是指向绝对路径的链接，
                # 经常会解压失败（例如项目根目录 chat_data -> /root/.agents_runtime/chat_data）。
                # setup payload 需要跨平台可解压，因此项目源码打包时跳过符号链接。
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
                               agent_manager: object | None) -> None:
        self._dump_json(os.path.join(cfg_dir, "env.json"), self.read())
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
                if not pth or not os.path.exists(pth):
                    continue
                real = os.path.realpath(pth)
                try:
                    common = os.path.commonpath([package_root_real, real])
                except ValueError:
                    common = ""
                if common != package_root_real:
                    logger.info(
                        "工具资源不在 env_manager.py 所在目录的父目录下，跳过打包但保留工具注册信息不变: "
                        "tool_id=%s, %s=%s。安装后相关工具或智能体可能无法正常工作。",
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
        return """#!/bin/sh
set -eu

AGENT_SERVICE_HOME="$PWD/agents"
: "${AGENTS_RUNTIME_DIR:=$HOME/.agents_runtime}"
: "${AGENT_SERVICE_PORT:=7988}"
: "${START_AGENT_SERVICE:=background}"

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/agent-service-setup.XXXXXX")
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

echo "Extracting agent service package..." >&2
base64 -d > "$TMPDIR/payload.tar.gz" <<'__AGENT_SERVICE_TAR_GZ_BASE64__'
""" + encoded_payload + """
__AGENT_SERVICE_TAR_GZ_BASE64__
mkdir -p "$TMPDIR/payload"
tar -xzf "$TMPDIR/payload.tar.gz" -C "$TMPDIR/payload"
# setup 包不应包含符号链接。即使历史版本 payload 中误带链接，也在 shell 侧二次清理，
# 避免 Linux/Windows tar 行为差异或无效链接影响安装。
find "$TMPDIR/payload" -type l -exec rm -f {} \\;

mkdir -p "$AGENT_SERVICE_HOME" "$AGENTS_RUNTIME_DIR"
[ ! -d "$TMPDIR/payload/app" ] || tar -C "$TMPDIR/payload/app" -cf - . | tar -C "$AGENT_SERVICE_HOME" -xf -
[ ! -d "$TMPDIR/payload/agents_runtime" ] || tar -C "$TMPDIR/payload/agents_runtime" -cf - . | tar -C "$AGENTS_RUNTIME_DIR" -xf -

cat > "$AGENT_SERVICE_HOME/start-agent-service.sh" <<'SH'
#!/bin/sh
set -eu
: "${AGENT_SERVICE_PORT:=7988}"
: "${AGENTS_RUNTIME_DIR:=$HOME/.agents_runtime}"
: "${AGENT_SERVICE_LOG:=$AGENTS_RUNTIME_DIR/server.log}"
: "${START_AGENT_SERVICE:=background}"
export AGENTS_RUNTIME_DIR AGENT_SERVICE_LOG
mkdir -p "$AGENTS_RUNTIME_DIR"
cd "$(dirname "$0")"

case "$START_AGENT_SERVICE" in
  background)
    nohup python3 app.py "0.0.0.0:${AGENT_SERVICE_PORT}" >> "$AGENT_SERVICE_LOG" 2>&1 &
    echo $!
    ;;
  foreground)
    exec python3 app.py "0.0.0.0:${AGENT_SERVICE_PORT}" 2>&1 | tee -a "$AGENT_SERVICE_LOG"
    ;;
  none)
    exit 0
    ;;
  *)
    echo "Invalid START_AGENT_SERVICE=$START_AGENT_SERVICE. Expected: background, foreground, none" >&2
    exit 2
    ;;
esac
SH
chmod +x "$AGENT_SERVICE_HOME/start-agent-service.sh"

case "$START_AGENT_SERVICE" in
  background|foreground|none) ;;
  true) START_AGENT_SERVICE=background ;;
  false) START_AGENT_SERVICE=none ;;
  *)
    echo "Invalid START_AGENT_SERVICE=$START_AGENT_SERVICE. Expected: background, foreground, none" >&2
    exit 2
    ;;
esac

AGENT_SERVICE_HOME="$(cd "$(dirname "$0")" && pwd)"
AGENT_SERVICE_LOG="$AGENTS_RUNTIME_DIR/server.log"
export AGENT_SERVICE_HOME AGENT_SERVICE_LOG

echo "Agent service installed:" >&2
echo "  app:    $AGENT_SERVICE_HOME" >&2
echo "  config: $AGENTS_RUNTIME_DIR" >&2
echo "  log:    $AGENT_SERVICE_LOG" >&2

case "$START_AGENT_SERVICE" in
  background)
    pid=$("$AGENT_SERVICE_HOME/start-agent-service.sh")
    echo "Agent service started in background, pid: $pid" >&2
    echo "Log: $AGENT_SERVICE_LOG" >&2
    ;;
  foreground)
    "$AGENT_SERVICE_HOME/start-agent-service.sh"
    ;;
  none)
    echo "Agent service not started because START_AGENT_SERVICE=none" >&2
    ;;
esac
exit 0
"""

    def _render_setup_script_ps1(self, encoded_payload: str) -> str:
        return """$ErrorActionPreference = 'Stop'

$env:AGENT_SERVICE_HOME = Join-Path $PWD 'agents'
if (-not $env:AGENTS_RUNTIME_DIR) { $env:AGENTS_RUNTIME_DIR = Join-Path $HOME '.agents_runtime' }
if (-not $env:AGENT_SERVICE_PORT) { $env:AGENT_SERVICE_PORT = '7988' }
if (-not $env:START_AGENT_SERVICE) { $env:START_AGENT_SERVICE = 'background' }

$tmpRoot = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$tmpDir = Join-Path $tmpRoot ("agent-service-setup." + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
try {
    Write-Host "Extracting agent service package..."
    $payloadPath = Join-Path $tmpDir 'payload.tar.gz'
    $base64Payload = @'
""" + encoded_payload + """
'@
    [System.IO.File]::WriteAllBytes($payloadPath, [System.Convert]::FromBase64String(($base64Payload -replace '\\s', '')))

    $payloadDir = Join-Path $tmpDir 'payload'
    New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
    tar -xzf $payloadPath -C $payloadDir
    if ($LASTEXITCODE -ne 0) { throw "tar extraction failed with exit code $LASTEXITCODE" }

    New-Item -ItemType Directory -Force -Path $env:AGENT_SERVICE_HOME | Out-Null
    New-Item -ItemType Directory -Force -Path $env:AGENTS_RUNTIME_DIR | Out-Null

    $appPayload = Join-Path $payloadDir 'app'
    if (Test-Path -LiteralPath $appPayload -PathType Container) {
        Get-ChildItem -LiteralPath $appPayload -Force | Copy-Item -Destination $env:AGENT_SERVICE_HOME -Recurse -Force
    }
    $runtimePayload = Join-Path $payloadDir 'agents_runtime'
    if (Test-Path -LiteralPath $runtimePayload -PathType Container) {
        Get-ChildItem -LiteralPath $runtimePayload -Force | Copy-Item -Destination $env:AGENTS_RUNTIME_DIR -Recurse -Force
    }

    $startScript = Join-Path $env:AGENT_SERVICE_HOME 'start-agent-service.ps1'
    @'
$ErrorActionPreference = 'Stop'
if (-not $env:AGENT_SERVICE_PORT) { $env:AGENT_SERVICE_PORT = '7988' }
if (-not $env:AGENTS_RUNTIME_DIR) { $env:AGENTS_RUNTIME_DIR = Join-Path $HOME '.agents_runtime' }
if (-not $env:AGENT_SERVICE_LOG) { $env:AGENT_SERVICE_LOG = Join-Path $env:AGENTS_RUNTIME_DIR 'server.log' }
if (-not $env:START_AGENT_SERVICE) { $env:START_AGENT_SERVICE = 'background' }
New-Item -ItemType Directory -Force -Path $env:AGENTS_RUNTIME_DIR | Out-Null
$currentDir = if ($PSScriptRoot) { $PSScriptRoot } else { $env:AGENT_SERVICE_HOME }
Set-Location -LiteralPath $currentDir

switch ($env:START_AGENT_SERVICE) {
    'background' {
        $errLog = Join-Path (Split-Path -Parent $env:AGENT_SERVICE_LOG) 'server.err.log'
        $pidPath = Join-Path $env:AGENTS_RUNTIME_DIR 'server.pid'
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        $appDir = $currentDir.Replace("'", "''")
        $outLog = $env:AGENT_SERVICE_LOG.Replace("'", "''")
        $errorLog = $errLog.Replace("'", "''")
        $runner = @"
`$ErrorActionPreference = 'Continue'
try {
    ('[{0}] Launching agent service from $appDir on port $($env:AGENT_SERVICE_PORT)' -f (Get-Date -Format o)) | Add-Content -LiteralPath '$outLog'
    `$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    `$pythonArgs = @('app.py', '0.0.0.0:$($env:AGENT_SERVICE_PORT)')
    if (-not `$pythonCmd) {
        `$pythonCmd = Get-Command py -ErrorAction SilentlyContinue
        `$pythonArgs = @('-3', 'app.py', '0.0.0.0:$($env:AGENT_SERVICE_PORT)')
    }
    if (-not `$pythonCmd) { throw 'Neither python nor py was found in PATH' }
    `$pythonExe = if (`$pythonCmd.Source) { `$pythonCmd.Source } else { `$pythonCmd.Name }
    ('[{0}] Python command: {1} {2}' -f (Get-Date -Format o), `$pythonExe, (`$pythonArgs -join ' ')) | Add-Content -LiteralPath '$outLog'
    Set-Location -LiteralPath '$appDir'
    & `$pythonExe @pythonArgs >> '$outLog' 2>> '$errorLog'
    `$exitCode = if (`$null -ne `$LASTEXITCODE) { `$LASTEXITCODE } else { 0 }
    ('[{0}] Agent service exited with code {1}' -f (Get-Date -Format o), `$exitCode) | Add-Content -LiteralPath '$errorLog'
    exit `$exitCode
} catch {
    ('[{0}] Failed to launch agent service:' -f (Get-Date -Format o)) | Add-Content -LiteralPath '$errorLog'
    (`$_ | Out-String) | Add-Content -LiteralPath '$errorLog'
    exit 1
}
"@
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($runner))
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) -WorkingDirectory $currentDir -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 2
        $p.Refresh()
        if ($p.HasExited) {
            $tail = if (Test-Path -LiteralPath $errLog) { (Get-Content -LiteralPath $errLog -Tail 80 -ErrorAction SilentlyContinue | Out-String) } else { '' }
            throw "Agent service exited immediately with code $($p.ExitCode). Error log: $errLog`n$tail"
        }
        Set-Content -LiteralPath $pidPath -Value $p.Id -Encoding ASCII
        Write-Output $p.Id
    }
    'foreground' {
        & python app.py "0.0.0.0:$($env:AGENT_SERVICE_PORT)" 2>&1 | Tee-Object -FilePath $env:AGENT_SERVICE_LOG -Append
    }
    'none' { return }
    default { throw "Invalid START_AGENT_SERVICE=$($env:START_AGENT_SERVICE). Expected: background, foreground, none" }
}
'@ | Set-Content -LiteralPath $startScript -Encoding UTF8

    $stopScript = Join-Path $env:AGENT_SERVICE_HOME 'stop-agent-service.ps1'
    @'
$ErrorActionPreference = 'Stop'
if (-not $env:AGENTS_RUNTIME_DIR) { $env:AGENTS_RUNTIME_DIR = Join-Path $HOME '.agents_runtime' }
$pidPath = Join-Path $env:AGENTS_RUNTIME_DIR 'server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No pid file found: $pidPath"
    exit 0
}
$servicePidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
$servicePid = 0
if (-not [int]::TryParse($servicePidText, [ref]$servicePid)) {
    throw "Invalid pid file content: $pidPath"
}
function Stop-AgentProcessTree {
    param([int]$RootPid)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$RootPid" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-AgentProcessTree -RootPid ([int]$child.ProcessId)
    }
    $proc = Get-Process -Id $RootPid -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped pid: $RootPid"
    }
}
Stop-AgentProcessTree -RootPid $servicePid
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
'@ | Set-Content -LiteralPath $stopScript -Encoding UTF8

    switch ($env:START_AGENT_SERVICE) {
        'background' { }
        'foreground' { }
        'none' { }
        'true' { $env:START_AGENT_SERVICE = 'background' }
        'false' { $env:START_AGENT_SERVICE = 'none' }
        default { throw "Invalid START_AGENT_SERVICE=$($env:START_AGENT_SERVICE). Expected: background, foreground, none" }
    }

    $env:AGENT_SERVICE_LOG = Join-Path $env:AGENTS_RUNTIME_DIR 'server.log'
    $agentServiceErrLog = Join-Path $env:AGENTS_RUNTIME_DIR 'server.err.log'

    Write-Host "Agent service installed:"
    Write-Host "  app:    $(Join-Path $PWD 'agents')"
    Write-Host "  config: $($env:AGENTS_RUNTIME_DIR)"
    Write-Host "  log:    $($env:AGENT_SERVICE_LOG)"
    Write-Host "  errlog: $agentServiceErrLog"

    switch ($env:START_AGENT_SERVICE) {
        'background' {
            try {
                $pidText = powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript
                Write-Host "Agent service started in background, pid: $pidText"
                Write-Host "Log: $($env:AGENT_SERVICE_LOG)"
                Write-Host "Error log: $agentServiceErrLog"
                Write-Host "Stop: powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$stopScript'"
            }
            catch {
                Write-Warning "Agent service was installed, but automatic start failed: $($_.Exception.Message)"
                Write-Warning "You can start it manually: cd '$(Join-Path $PWD 'agents')'; python app.py '0.0.0.0:$($env:AGENT_SERVICE_PORT)'"
                Write-Warning "Or run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$startScript'"
            }
        }
        'foreground' {
            try {
                powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript
            }
            catch {
                Write-Warning "Agent service was installed, but automatic start failed: $($_.Exception.Message)"
                Write-Warning "You can start it manually: cd '$(Join-Path $PWD 'agents')'; python app.py '0.0.0.0:$($env:AGENT_SERVICE_PORT)'"
                Write-Warning "Or run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$startScript'"
            }
        }
        'none' { Write-Host "Agent service not started because START_AGENT_SERVICE=none" }
    }
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
"""
