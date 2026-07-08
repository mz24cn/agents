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

    Also maintains a labels index for flexible lookup: when get(model_id)
    fails to find by ID, it falls back to searching the labels index.
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelConfig] = {}
        self._labels_index: dict[str, dict[str, None]] = {}

    # -- labels index helpers --------------------------------------------------

    def _index_labels(self, item_id: str, labels: list) -> None:
        """Add an item's labels to the index."""
        for label in labels:
            if label not in self._labels_index:
                self._labels_index[label] = {}
            self._labels_index[label][item_id] = None

    def _unindex_labels(self, item_id: str, labels: list) -> None:
        """Remove an item's labels from the index."""
        for label in labels:
            ids = self._labels_index.get(label)
            if ids is not None:
                ids.pop(item_id, None)
                if not ids:
                    del self._labels_index[label]

    def _rebuild_labels_index(self) -> None:
        """Rebuild the entire labels index from current items."""
        self._labels_index.clear()
        for item_id, config in self._models.items():
            self._index_labels(item_id, config.labels)

    # -- CRUD ------------------------------------------------------------------

    def register(self, config: ModelConfig) -> None:
        """Register a model configuration.

        Args:
            config: The ModelConfig to register. If a config with the same
                model_id already exists, it will be overwritten.
        """
        if config.model_id in self._models:
            self._unindex_labels(config.model_id, self._models[config.model_id].labels)
        self._models[config.model_id] = config
        self._index_labels(config.model_id, config.labels)

    def get(self, model_id: str) -> Optional[ModelConfig]:
        """Retrieve a model configuration by its ID, with labels fallback.

        First attempts a direct lookup by model_id.  If that fails, treats
        the argument as a label and returns the first matching config.

        Args:
            model_id: The unique identifier of the model, or a label.

        Returns:
            The ModelConfig if found, or None if not registered.
        """
        config = self._models.get(model_id)
        if config is not None:
            return config
        # Fallback: search by label
        ids = self._labels_index.get(model_id)
        if ids:
            return self._models.get(next(iter(ids)))
        return None

    def remove(self, model_id: str) -> bool:
        """Remove a model configuration by its ID.

        Args:
            model_id: The unique identifier of the model to remove.

        Returns:
            True if the model was found and removed, False otherwise.
        """
        if model_id in self._models:
            self._unindex_labels(model_id, self._models[model_id].labels)
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
        self._labels_index.clear()
        for item in data:
            config = ModelConfig.from_dict(item)
            self._models[config.model_id] = config
        self._rebuild_labels_index()


from typing import Callable

from runtime.models import ToolConfig


class ToolRegistry:
    """Manages tool configurations and callable instances.

    Stores ToolConfig instances keyed by tool_id, with optional associated
    callable functions. Supports filtering by tool_type and JSON persistence.

    Also maintains a labels index for flexible lookup: when get(tool_id)
    fails to find by ID, it falls back to searching the labels index.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolConfig] = {}
        self._callables: dict[str, Callable] = {}
        self._labels_index: dict[str, dict[str, None]] = {}

    # -- labels index helpers --------------------------------------------------

    def _index_labels(self, item_id: str, labels: list) -> None:
        """Add an item's labels to the index."""
        for label in labels:
            if label not in self._labels_index:
                self._labels_index[label] = {}
            self._labels_index[label][item_id] = None

    def _unindex_labels(self, item_id: str, labels: list) -> None:
        """Remove an item's labels from the index."""
        for label in labels:
            ids = self._labels_index.get(label)
            if ids is not None:
                ids.pop(item_id, None)
                if not ids:
                    del self._labels_index[label]

    def _rebuild_labels_index(self) -> None:
        """Rebuild the entire labels index from current items."""
        self._labels_index.clear()
        for item_id, config in self._tools.items():
            self._index_labels(item_id, config.labels)

    # -- CRUD ------------------------------------------------------------------

    def register(self, config: ToolConfig, callable_fn: Callable | None = None) -> None:
        """Register a tool configuration with an optional callable.

        Args:
            config: The ToolConfig to register. If a config with the same
                tool_id already exists, it will be overwritten.
            callable_fn: Optional callable associated with this tool
                (used for function-type tools).
        """
        if config.tool_id in self._tools:
            self._unindex_labels(config.tool_id, self._tools[config.tool_id].labels)
        self._tools[config.tool_id] = config
        self._index_labels(config.tool_id, config.labels)
        if callable_fn is not None:
            self._callables[config.tool_id] = callable_fn
        elif config.tool_id in self._callables:
            del self._callables[config.tool_id]

    def get(self, tool_id: str) -> Optional[ToolConfig]:
        """Retrieve a tool configuration by its ID, with labels fallback.

        First attempts a direct lookup by tool_id.  If that fails, treats
        the argument as a label and returns the first matching config.

        Args:
            tool_id: The unique identifier of the tool, or a label.

        Returns:
            The ToolConfig if found, or None if not registered.
        """
        config = self._tools.get(tool_id)
        if config is not None:
            return config
        # Fallback: search by label
        ids = self._labels_index.get(tool_id)
        if ids:
            return self._tools.get(next(iter(ids)))
        return None

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
            self._unindex_labels(tool_id, self._tools[tool_id].labels)
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
        self._labels_index.clear()
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
        self._rebuild_labels_index()
