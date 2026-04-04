"""Registry classes for managing model and tool configurations.

Provides ModelRegistry for model endpoint management with JSON persistence.
ToolRegistry will be added in a subsequent task.
"""

import json
import os
from typing import Optional

from runtime.models import ModelConfig


class ModelRegistry:
    """Manages model JSON configurations with CRUD operations and persistence.

    Stores ModelConfig instances keyed by model_id. Supports saving to and
    loading from JSON files for persistent storage.
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelConfig] = {}

    def register(self, config: ModelConfig) -> None:
        """Register a model configuration.

        Args:
            config: The ModelConfig to register. If a config with the same
                model_id already exists, it will be overwritten.
        """
        self._models[config.model_id] = config

    def get(self, model_id: str) -> Optional[ModelConfig]:
        """Retrieve a model configuration by its ID.

        Args:
            model_id: The unique identifier of the model.

        Returns:
            The ModelConfig if found, or None if not registered.
        """
        return self._models.get(model_id)

    def remove(self, model_id: str) -> bool:
        """Remove a model configuration by its ID.

        Args:
            model_id: The unique identifier of the model to remove.

        Returns:
            True if the model was found and removed, False otherwise.
        """
        if model_id in self._models:
            del self._models[model_id]
            return True
        return False

    def list_all(self) -> list[ModelConfig]:
        """Return a list of all registered model configurations.

        Returns:
            A list of all ModelConfig instances, in insertion order.
        """
        return list(self._models.values())

    def save(self, path: str) -> None:
        """Serialize all registered models to a JSON file.

        Args:
            path: File path to write the JSON data to.
        """
        data = [config.to_dict() for config in self._models.values()]
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, path: str) -> None:
        """Load model configurations from a JSON file.

        Replaces all currently registered models with those from the file.

        Args:
            path: File path to read the JSON data from.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the JSON data is not a valid list of model configs.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
        self._models.clear()
        for item in data:
            config = ModelConfig.from_dict(item)
            self._models[config.model_id] = config


from typing import Callable

from runtime.models import ToolConfig


class ToolRegistry:
    """Manages tool configurations and callable instances.

    Stores ToolConfig instances keyed by tool_id, with optional associated
    callable functions. Supports filtering by tool_type and JSON persistence.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolConfig] = {}
        self._callables: dict[str, Callable] = {}

    def register(self, config: ToolConfig, callable_fn: Callable | None = None) -> None:
        """Register a tool configuration with an optional callable.

        Args:
            config: The ToolConfig to register. If a config with the same
                tool_id already exists, it will be overwritten.
            callable_fn: Optional callable associated with this tool
                (used for function-type tools).
        """
        self._tools[config.tool_id] = config
        if callable_fn is not None:
            self._callables[config.tool_id] = callable_fn
        elif config.tool_id in self._callables:
            del self._callables[config.tool_id]

    def get(self, tool_id: str) -> Optional[ToolConfig]:
        """Retrieve a tool configuration by its ID.

        Args:
            tool_id: The unique identifier of the tool.

        Returns:
            The ToolConfig if found, or None if not registered.
        """
        return self._tools.get(tool_id)

    def get_callable(self, tool_id: str) -> Optional[Callable]:
        """Retrieve the callable associated with a tool.

        Args:
            tool_id: The unique identifier of the tool.

        Returns:
            The callable if found, or None if not registered or no callable.
        """
        return self._callables.get(tool_id)

    def remove(self, tool_id: str) -> bool:
        """Remove a tool configuration and its callable by ID.

        Args:
            tool_id: The unique identifier of the tool to remove.

        Returns:
            True if the tool was found and removed, False otherwise.
        """
        if tool_id in self._tools:
            del self._tools[tool_id]
            self._callables.pop(tool_id, None)
            return True
        return False

    def list_all(self) -> list[ToolConfig]:
        """Return a list of all registered tool configurations.

        Returns:
            A list of all ToolConfig instances, in insertion order.
        """
        return list(self._tools.values())

    def list_by_type(self, tool_type: str) -> list[ToolConfig]:
        """Return a list of tool configurations filtered by tool_type.

        Args:
            tool_type: The tool type to filter by (e.g. "function", "mcp", "skill").

        Returns:
            A list of ToolConfig instances matching the given tool_type.
        """
        return [cfg for cfg in self._tools.values() if cfg.tool_type == tool_type]

    def save(self, path: str) -> None:
        """Serialize all registered tools to a JSON file.

        Only tool configurations are persisted; callable references are not
        serializable and will be lost.

        Args:
            path: File path to write the JSON data to.
        """
        data = [config.to_dict() for config in self._tools.values() if not config.builtin]
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, path: str) -> None:
        """Load tool configurations from a JSON file.

        Replaces all currently registered tools with those from the file.
        Callable references are cleared since they cannot be persisted.

        Args:
            path: File path to read the JSON data from.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the JSON data is not a valid list of tool configs.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
        self._tools.clear()
        self._callables.clear()
        for item in data:
            config = ToolConfig.from_dict(item)
            self._tools[config.tool_id] = config
            # Auto-reload callable for function tools that have file path info
            if (
                config.tool_type == "function"
                and config.function_file_path
                and config.function_name
            ):
                try:
                    import importlib.util as _ilu
                    import sys as _sys
                    _mod_name = f"_dynamic_tool_{hash(config.function_file_path)}"
                    _sys.modules.pop(_mod_name, None)
                    _spec = _ilu.spec_from_file_location(_mod_name, config.function_file_path)
                    if _spec and _spec.loader:
                        _mod = _ilu.module_from_spec(_spec)
                        _sys.modules[_mod_name] = _mod
                        _spec.loader.exec_module(_mod)
                        _fn = getattr(_mod, config.function_name, None)
                        if callable(_fn):
                            self._callables[config.tool_id] = _fn
                except Exception:
                    pass  # callable unavailable; tool config still loaded
