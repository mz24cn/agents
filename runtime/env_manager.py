"""EnvManager — 环境变量管理模块。

负责读写 ~/.agents_runtime/env.json 文件，并将键值对同步到当前进程的 os.environ。
支持递归扫描 .py 文件以检测项目中已使用的环境变量。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile

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
        return {str(k): str(v) for k, v in data.items()}

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
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _sync_to_environ(self, env_map: dict[str, str]) -> None:
        """将 env_map 中所有键值对写入 os.environ。

        Args:
            env_map: 要同步的键值对字典。
        """
        for k, v in env_map.items():
            os.environ[str(k)] = str(v)
