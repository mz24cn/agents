"""RemoteEnvManager — 远程环境管理模块。

从当前（母）环境的视角管理其派生出的子环境，读写 DATA_DIR/remote_envs.json。
每条记录包含环境地址（URL）以及最近一次状态检查得到的版本 / 推理状态快照
（frontend_build / backend_build / last_config / inference_active 等）。

更新采用**推送**模型：母环境把增量 tar 直接 POST 到子环境的
``/v1/setup?op=push``，子环境无需知道（更无需能访问）母环境地址——
母环境可以匿名、或只能通过 localhost 访问。

remote_envs.json 只是本环境的本地管理元数据：与 env.json 一样，它不参与
在线更新（build_delta_tar 只打包指定的配置文件）和自解压导出
（build_setup_script 的 _write_runtime_configs 从不写入该文件），也不会被
同步到其他环境。

文件内容是纯 JSON 数组；早期版本写出的 ``{"envs": [...], "source_url": ...}``
对象格式仍然可读（source_url 字段被忽略）。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import os
import threading

import urllib.parse

from runtime.common import now_iso

logger = logging.getLogger("runtime.remote_env_manager")

# /v1/setup 各请求自行决定 / 传递的查询参数。归一化环境 URL 时剔除它们，
# 其余查询参数（如 token=...）原样保留，供后续 hello / update 请求鉴权。
MANAGED_SETUP_QUERY_KEYS = frozenset({
    "op", "source", "frontend_build", "backend_build", "last_config",
})

# 快照字段：从 /v1/setup?op=hello 响应中取值并持久化到 remote_envs.json。
SNAPSHOT_VERSION_KEYS = ("frontend_build", "backend_build", "last_config", "server_instance_id")
SNAPSHOT_FLAG_KEYS = ("inference_active", "api_inference_active", "session_inference_active")
# 应用 / 平台信息字段：同样取自 op=hello 响应（app_title / app_logo / arch / os），
# 在远程环境列表中展示环境的应用标识与运行平台。
SNAPSHOT_TEXT_KEYS = ("app_title", "app_logo", "arch", "os")


def normalize_setup_url(url: str) -> dict:
    """归一化一个 SETUP_SOURCE 风格的环境 URL。

    接受三种形态（与构建版本页的 SETUP_SOURCE 输入框相同）：

    * ``http://172.28.70.13:7988/``
    * ``http://172.28.70.13:7988/v1/setup``
    * ``http://172.28.70.13:7988/v1/setup?token=...``

    Returns:
        dict，包含：

        * ``id`` — 稳定标识 ``scheme://netloc``，作为环境记录的去重键；
        * ``scheme`` / ``netloc`` / ``path`` — 归一化后的地址各部分，
          path 固定为指向 ``/v1/setup`` 的路径；
        * ``query`` — 需要保留的查询参数 (key, value) 列表（剔除 op 等
          由具体请求决定的参数）。

    Raises:
        ValueError: 不是合法的 http(s) URL 时。
    """
    raw = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须是 http(s) 地址，例如 http://host:7988/v1/setup")
    try:
        parsed.port  # 非数字端口在这里抛出 ValueError
    except ValueError:
        raise ValueError(f"URL 端口无效: {raw}") from None
    if "/v1/setup" in parsed.path:
        path = parsed.path[: parsed.path.index("/v1/setup") + len("/v1/setup")]
    else:
        path = "/v1/setup"
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in MANAGED_SETUP_QUERY_KEYS
    ]
    return {
        "id": f"{parsed.scheme}://{parsed.netloc}",
        "scheme": parsed.scheme,
        "netloc": parsed.netloc,
        "path": path,
        "query": query,
    }


def canonical_setup_url(url: str) -> str:
    """返回归一化后的环境地址（base + 保留的查询参数，不含 op 等受管理参数）。"""
    normalized = normalize_setup_url(url)
    url_str = f"{normalized['scheme']}://{normalized['netloc']}{normalized['path']}"
    if normalized["query"]:
        url_str += "?" + urllib.parse.urlencode(normalized["query"])
    return url_str


def build_setup_request_url(url: str, params: dict) -> str:
    """基于环境 URL 构造带指定查询参数的 /v1/setup 请求 URL。

    保留原 URL 中的鉴权类查询参数（如 token），剔除由 *params* 重新决定的
    受管理参数（op / source / 三个版本基线），与后端 op=update 组装远端
    delta 地址的规则一致。
    """
    normalized = normalize_setup_url(url)
    query = list(normalized["query"])
    query.extend((key, str(value)) for key, value in params.items())
    return urllib.parse.urlunsplit((
        normalized["scheme"],
        normalized["netloc"],
        normalized["path"],
        urllib.parse.urlencode(query),
        "",
    ))


def snapshot_from_hello(data: dict | None) -> dict:
    """从 ``/v1/setup?op=hello`` 响应中提取可展示的快照字段。"""
    data = data if isinstance(data, dict) else {}
    snapshot: dict = {}
    for key in SNAPSHOT_VERSION_KEYS:
        snapshot[key] = str(data.get(key, "") or "")
    for key in SNAPSHOT_FLAG_KEYS:
        snapshot[key] = bool(data.get(key))
    for key in SNAPSHOT_TEXT_KEYS:
        snapshot[key] = str(data.get(key, "") or "")
    return snapshot


class RemoteEnvManager:
    """管理 remote_envs.json 的读写。

    文件内容是一个 JSON 数组::

        [
          {
            "id": "http://172.28.70.13:7988",
            "url": "http://172.28.70.13:7988/v1/setup?token=...",
            "created_at": "2025-09-08T15:30:00",
            "frontend_build": "250908_143000",
            "backend_build": "250908_143000",
            "last_config": "250908_143000",
            "inference_active": false,
            "api_inference_active": false,
            "session_inference_active": false,
            "server_instance_id": "...",
            "app_title": "...",
            "app_logo": "...",
            "arch": "x86_64",
            "os": "linux",
            "checked_at": "2025-09-08T15:30:00"
          }
        ]

    早期版本写出的 ``{"envs": [...], "source_url": ...}`` 对象格式仍然可读
    （source_url 字段被忽略）。
    所有写操作都在同一把锁内完成读-改-写，跨线程（HTTP 请求线程）安全。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def read(self) -> list[dict]:
        """读取全部环境记录；文件不存在时返回空列表。"""
        with self._lock:
            return self._read_locked()

    def get(self, env_id: str) -> dict:
        """按 id 返回单条环境记录（副本）；不存在时抛 KeyError。"""
        with self._lock:
            envs = self._read_locked()
        record = next((e for e in envs if e.get("id") == env_id), None)
        if record is None:
            raise KeyError(env_id)
        return dict(record)

    def upsert(self, url: str, snapshot: dict | None = None) -> list[dict]:
        """新增或更新一条环境记录（按 ``id`` 去重），返回完整列表。

        ``snapshot`` 为前端查询目标环境 ``op=hello`` 的结果；提供时立即
        落盘为版本快照。
        """
        normalized = normalize_setup_url(url)  # 非法 URL 在这里抛 ValueError
        canonical = canonical_setup_url(url)
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == normalized["id"]), None)
            if record is None:
                record = {
                    "id": normalized["id"],
                    "url": canonical,
                    "created_at": now_iso(),
                }
                envs.append(record)
            else:
                record["url"] = canonical
            self._apply_snapshot(record, snapshot)
            self._write_locked(envs)
            return envs

    def update_snapshot(self, env_id: str, snapshot: dict) -> list[dict]:
        """更新指定环境的版本快照（刷新 / 更新完成后由前端回写），返回完整列表。"""
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == env_id), None)
            if record is None:
                raise KeyError(env_id)
            self._apply_snapshot(record, snapshot)
            self._write_locked(envs)
            return envs

    def remove(self, env_id: str) -> list[dict]:
        """删除一条环境记录，返回剩余列表。记录不存在时抛 KeyError。"""
        with self._lock:
            envs = self._read_locked()
            kept = [e for e in envs if e.get("id") != env_id]
            if len(kept) == len(envs):
                raise KeyError(env_id)
            self._write_locked(kept)
            return kept

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_snapshot(record: dict, snapshot: dict | None) -> None:
        if not isinstance(snapshot, dict) or not snapshot:
            return
        for key in SNAPSHOT_VERSION_KEYS:
            record[key] = str(snapshot.get(key, "") or "")
        for key in SNAPSHOT_FLAG_KEYS:
            record[key] = bool(snapshot.get(key))
        for key in SNAPSHOT_TEXT_KEYS:
            record[key] = str(snapshot.get(key, "") or "")
        record["checked_at"] = now_iso()

    def _read_locked(self) -> list[dict]:
        """读取文件；兼容早期版本写出的 ``{"envs": [...]}`` 对象格式。"""
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # 该文件只是本地管理元数据；损坏时告警并按空列表处理，
            # 不阻塞服务启动或页面加载。
            logger.warning("读取 remote_envs.json 失败，按空列表处理: %s", exc)
            return []
        if isinstance(data, dict):
            raw_envs = data.get("envs", [])
        elif isinstance(data, list):
            raw_envs = data
        else:
            logger.warning("remote_envs.json 内容不是 JSON 对象或数组，按空列表处理")
            return []
        if not isinstance(raw_envs, list):
            logger.warning("remote_envs.json 的 envs 字段不是数组，按空列表处理")
            return []
        return [e for e in raw_envs if isinstance(e, dict) and e.get("id")]

    def _write_locked(self, envs: list[dict]) -> None:
        from runtime.common import atomic_write_text
        content = json.dumps(envs, ensure_ascii=False, indent=2)
        atomic_write_text(self._path, content)
