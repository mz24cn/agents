"""Prompt template management with CRUD operations and JSON persistence.

Provides PromptTemplate dataclass and PromptTemplateManager for managing
prompt templates. Follows the same patterns as ModelRegistry in runtime/registry.py.

Zero third-party dependencies — only Python standard library.
"""

import datetime
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from runtime.common import parse_labels


@dataclass
class PromptTemplate:
    """A prompt template with an ID and content.

    Content may contain {variable_name} placeholders that are replaced
    with user-provided values at application time.
    """

    template_id: str
    content: str
    labels: list = field(default_factory=list)
    created_at: str = ""
    last_modified: str = ""

    def __post_init__(self):
        self.labels = parse_labels(self.labels)

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        d = {
            "template_id": self.template_id,
            "content": self.content,
            "created_at": self.created_at,
            "last_modified": self.last_modified,
        }
        if self.labels:
            d["labels"] = list(self.labels)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PromptTemplate":
        """Deserialize from a plain dict."""
        return cls(
            template_id=data["template_id"],
            content=data["content"],
            labels=parse_labels(data.get("labels", [])),
            created_at=data.get("created_at", ""),
            last_modified=data.get("last_modified", ""),
        )


class PromptTemplateManager:
    """Manages prompt templates with CRUD operations and JSON persistence.

    Stores PromptTemplate instances keyed by template_id. Supports saving
    to and loading from JSON files for persistent storage.

    Also maintains a labels index for flexible lookup: when get(template_id)
    fails to find by ID, it falls back to searching the labels index.
    """

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}
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
        for item_id, template in self._templates.items():
            self._index_labels(item_id, template.labels)

    def list_all(self) -> list[PromptTemplate]:
        """Return a list of all prompt templates.

        Returns:
            A list of all PromptTemplate instances, in insertion order.
        """
        return list(self._templates.values())

    def get(self, template_id: str) -> Optional[PromptTemplate]:
        """Retrieve a prompt template by its ID, with labels fallback.

        First attempts a direct lookup by template_id.  If that fails, treats
        the argument as a label and returns the first matching template.

        Args:
            template_id: The unique identifier of the template, or a label.

        Returns:
            The PromptTemplate if found, or None if not registered.
        """
        template = self._templates.get(template_id)
        if template is not None:
            return template
        # Fallback: search by label
        ids = self._labels_index.get(template_id)
        if ids:
            return self._templates.get(next(iter(ids)))
        return None

    def create(self, template_id: str, content: str, labels: list = None) -> PromptTemplate:
        """Create a new prompt template.

        Args:
            template_id: The unique identifier for the template.
            content: The template content (may contain {placeholder} variables).
            labels: Optional list of labels for categorization.

        Returns:
            The newly created PromptTemplate.
        """
        now = datetime.datetime.now().isoformat()
        template = PromptTemplate(
            template_id=template_id,
            content=content,
            labels=labels or [],
            created_at=now,
            last_modified=now,
        )
        self._templates[template_id] = template
        self._index_labels(template_id, template.labels)
        return template

    def update(self, template_id: str, new_template_id: str, content: str, labels: list = None) -> Optional[PromptTemplate]:
        """Update an existing prompt template.

        Args:
            template_id: The current unique identifier of the template to update.
            new_template_id: The new unique identifier (may be the same as template_id).
            content: The new template content.
            labels: Optional list of labels for categorization.

        Returns:
            The updated PromptTemplate, or None if template_id not found.
        """
        if template_id not in self._templates:
            return None
        # Get existing labels if not provided
        existing = self._templates[template_id]
        old_labels = existing.labels
        if labels is None:
            labels = old_labels
        # Preserve created_at from existing template
        created_at = existing.created_at
        now = datetime.datetime.now().isoformat()
        template = PromptTemplate(
            template_id=new_template_id,
            content=content,
            labels=labels,
            created_at=created_at,
            last_modified=now,
        )
        # Remove old index entry
        self._unindex_labels(template_id, old_labels)
        del self._templates[template_id]
        # Insert new
        self._templates[new_template_id] = template
        self._index_labels(new_template_id, template.labels)
        return template

    def delete(self, template_id: str) -> bool:
        """Delete a prompt template by its ID.

        Args:
            template_id: The unique identifier of the template to delete.

        Returns:
            True if the template was found and deleted, False otherwise.
        """
        if template_id in self._templates:
            self._unindex_labels(template_id, self._templates[template_id].labels)
            del self._templates[template_id]
            return True
        return False

    def save(self, path: str) -> None:
        """Serialize all templates to a JSON file.

        Args:
            path: File path to write the JSON data to.
        """
        data = [t.to_dict() for t in self._templates.values()]
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, path: str) -> None:
        """Load prompt templates from a JSON file.

        Replaces all current templates with those from the file.

        Args:
            path: File path to read the JSON data from.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the JSON data is not a valid list.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
        self._templates.clear()
        self._labels_index.clear()
        for item in data:
            template = PromptTemplate.from_dict(item)
            self._templates[template.template_id] = template
        self._rebuild_labels_index()
