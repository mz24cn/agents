"""Prompt template management with CRUD operations and JSON persistence.

Provides PromptTemplate dataclass and PromptTemplateManager for managing
prompt templates. Follows the same patterns as ModelRegistry in runtime/registry.py.

Zero third-party dependencies — only Python standard library.
"""

import json
import os
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class PromptTemplate:
    """A prompt template with an ID, name, and content.

    Content may contain {variable_name} placeholders that are replaced
    with user-provided values at application time.
    """

    template_id: str
    name: str
    content: str

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {
            "template_id": self.template_id,
            "name": self.name,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PromptTemplate":
        """Deserialize from a plain dict."""
        return cls(
            template_id=data["template_id"],
            name=data["name"],
            content=data["content"],
        )


class PromptTemplateManager:
    """Manages prompt templates with CRUD operations and JSON persistence.

    Stores PromptTemplate instances keyed by template_id. Supports saving
    to and loading from JSON files for persistent storage.
    """

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}

    def list_all(self) -> list[PromptTemplate]:
        """Return a list of all prompt templates.

        Returns:
            A list of all PromptTemplate instances, in insertion order.
        """
        return list(self._templates.values())

    def get(self, template_id: str) -> Optional[PromptTemplate]:
        """Retrieve a prompt template by its ID.

        Args:
            template_id: The unique identifier of the template.

        Returns:
            The PromptTemplate if found, or None if not registered.
        """
        return self._templates.get(template_id)

    def create(self, name: str, content: str) -> PromptTemplate:
        """Create a new prompt template with an auto-generated ID.

        Args:
            name: The template name.
            content: The template content (may contain {placeholder} variables).

        Returns:
            The newly created PromptTemplate.
        """
        template_id = str(uuid.uuid4())
        template = PromptTemplate(template_id=template_id, name=name, content=content)
        self._templates[template_id] = template
        return template

    def update(self, template_id: str, name: str, content: str) -> Optional[PromptTemplate]:
        """Update an existing prompt template.

        Args:
            template_id: The unique identifier of the template to update.
            name: The new template name.
            content: The new template content.

        Returns:
            The updated PromptTemplate, or None if template_id not found.
        """
        if template_id not in self._templates:
            return None
        template = PromptTemplate(template_id=template_id, name=name, content=content)
        self._templates[template_id] = template
        return template

    def delete(self, template_id: str) -> bool:
        """Delete a prompt template by its ID.

        Args:
            template_id: The unique identifier of the template to delete.

        Returns:
            True if the template was found and deleted, False otherwise.
        """
        if template_id in self._templates:
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
        for item in data:
            template = PromptTemplate.from_dict(item)
            self._templates[template.template_id] = template
