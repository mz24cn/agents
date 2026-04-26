"""Skill Manager: SKILL.md parsing, loading, and progressive disclosure.

Provides SkillManager which loads Skill definitions from SKILL.md files,
parses YAML front-matter for name/description, and stores the full markdown body.

The progressive disclosure pattern:
  1. First inference round: only Skill name + description are exposed to the LLM
  2. When LLM selects a Skill: full SKILL.md body is injected into context,
     and built-in tools (bash, fetch) become available
  3. LLM reads the full body and decides how to use built-in tools to execute

SkillManager does NOT generate any command execution tools. The specific
implementation of a skill is entirely determined by the SKILL.md content
and the LLM's judgment after reading it.
"""

import os
import re
from typing import Optional

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry


def _parse_front_matter(content: str) -> tuple[dict, str]:
    """Parse YAML front-matter from a SKILL.md file.

    Expects the file to start with '---' delimited YAML front-matter.
    Returns (metadata_dict, body_text).

    Args:
        content: Full file content.

    Returns:
        Tuple of (front-matter dict, remaining body text).

    Raises:
        ValueError: If front-matter is missing or malformed.
    """
    content = content.strip()
    if not content.startswith("---"):
        raise ValueError(
            "SKILL.md must start with YAML front-matter (---). "
            f"Got: {content[:50]!r}..."
        )

    end_idx = content.find("---", 3)
    if end_idx == -1:
        raise ValueError("SKILL.md front-matter is not closed (missing second ---)")

    yaml_block = content[3:end_idx].strip()
    body = content[end_idx + 3:].strip()

    # Simple YAML key: value parser (no third-party yaml lib)
    metadata: dict = {}
    current_key: Optional[str] = None
    current_lines: list[str] = []

    for line in yaml_block.splitlines():
        m = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)", line)
        if m:
            if current_key is not None:
                metadata[current_key] = " ".join(current_lines).strip()
            current_key = m.group(1)
            current_lines = [m.group(2).strip()] if m.group(2).strip() else []
        elif current_key is not None and line.strip():
            current_lines.append(line.strip())

    if current_key is not None:
        metadata[current_key] = " ".join(current_lines).strip()

    if "name" not in metadata:
        raise ValueError("SKILL.md front-matter must contain a 'name' field")
    if "description" not in metadata:
        raise ValueError("SKILL.md front-matter must contain a 'description' field")

    return metadata, body


class SkillManager:
    """Skill loading and progressive disclosure manager.

    Loads Skill definitions from SKILL.md files, parses front-matter,
    stores full body content, and manages progressive disclosure state.

    Does NOT generate any command execution tools. After the LLM reads
    the full SKILL.md body, it decides how to use built-in tools
    (bash, fetch, etc.) to carry out the skill's operations.
    """

    def __init__(self, tool_registry: ToolRegistry):
        self._tool_registry = tool_registry
        # skill_name -> {metadata, body, skill_dir, tool_config}
        self._skills: dict[str, dict] = {}

    def load_skill(self, skill_dir: str) -> ToolConfig:
        """Load a single Skill from a directory containing SKILL.md.

        1. Reads skill_dir/SKILL.md
        2. Parses YAML front-matter for name, description
        3. Stores markdown body and skill_dir path
        4. Generates Skill ToolConfig (tool_type="skill") and registers it
        5. Returns the Skill's ToolConfig

        Args:
            skill_dir: Path to the skill directory.

        Returns:
            The Skill's ToolConfig.

        Raises:
            ValueError: If SKILL.md is missing or malformed.
        """
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            raise ValueError(f"SKILL.md not found in {skill_dir}")

        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        metadata, body = _parse_front_matter(content)
        skill_name = metadata["name"]
        description = metadata["description"]

        # Generate Skill ToolConfig (lightweight — only name + description)
        skill_tool_config = ToolConfig(
            tool_id=f"skill-{skill_name}",
            tool_type="skill",
            name=skill_name,
            description=description,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            skill_dir=os.path.abspath(skill_dir),
        )
        self._tool_registry.register(skill_tool_config)

        # Store skill data
        self._skills[skill_name] = {
            "metadata": metadata,
            "body": body,
            "skill_dir": os.path.abspath(skill_dir),
            "tool_config": skill_tool_config,
        }

        return skill_tool_config

    def load_skills_dir(self, base_dir: str) -> list[ToolConfig]:
        """Scan base_dir for subdirectories containing SKILL.md and load them.

        Args:
            base_dir: Base directory to scan.

        Returns:
            List of loaded Skill ToolConfigs.
        """
        results = []
        if not os.path.isdir(base_dir):
            return results

        for entry in sorted(os.listdir(base_dir)):
            sub_dir = os.path.join(base_dir, entry)
            if os.path.isdir(sub_dir) and os.path.isfile(
                os.path.join(sub_dir, "SKILL.md")
            ):
                try:
                    tc = self.load_skill(sub_dir)
                    results.append(tc)
                except ValueError:
                    pass

        return results

    def get_skill_body(self, skill_name: str) -> Optional[str]:
        """Get the full SKILL.md body content for a skill.

        Args:
            skill_name: The skill name (from front-matter).

        Returns:
            The markdown body, or None if skill not found.
        """
        skill = self._skills.get(skill_name)
        return skill["body"] if skill else None

    def get_skill_dir(self, skill_name: str) -> Optional[str]:
        """Get the directory path where the skill is located.

        Args:
            skill_name: The skill name.

        Returns:
            Absolute path to the skill directory, or None if not found.
        """
        skill = self._skills.get(skill_name)
        return skill["skill_dir"] if skill else None

    def is_skill(self, tool_name: str) -> bool:
        """Check if a tool_name corresponds to a loaded Skill.

        Args:
            tool_name: The tool name to check.

        Returns:
            True if it's a loaded Skill.
        """
        return tool_name in self._skills
