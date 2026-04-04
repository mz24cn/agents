"""Tests for SkillManager: SKILL.md parsing, loading, and progressive disclosure."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry
from runtime.skill_manager import SkillManager, _parse_front_matter


# ── SKILL.md parsing tests ──


class TestParseFrontMatter:

    def test_valid_front_matter(self):
        content = """---
name: my_skill
description: A test skill for testing.
---

# My Skill

Some body content here.
"""
        metadata, body = _parse_front_matter(content)
        assert metadata["name"] == "my_skill"
        assert metadata["description"] == "A test skill for testing."
        assert "# My Skill" in body
        assert "Some body content here." in body

    def test_multiline_description(self):
        content = """---
name: my_skill
description: A test skill that does
  many things across multiple lines.
---

Body.
"""
        metadata, body = _parse_front_matter(content)
        assert metadata["name"] == "my_skill"
        assert "many things" in metadata["description"]

    def test_missing_front_matter_raises(self):
        try:
            _parse_front_matter("# No front matter here")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "front-matter" in str(e).lower()

    def test_missing_name_raises(self):
        content = "---\ndescription: No name field\n---\nBody."
        try:
            _parse_front_matter(content)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "name" in str(e).lower()

    def test_missing_description_raises(self):
        content = "---\nname: my_skill\n---\nBody."
        try:
            _parse_front_matter(content)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "description" in str(e).lower()

    def test_unclosed_front_matter_raises(self):
        content = "---\nname: my_skill\ndescription: test\n"
        try:
            _parse_front_matter(content)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not closed" in str(e).lower()


# ── SkillManager loading tests ──


def _create_skill_dir(base_dir, name="test_skill", description="A test skill.",
                      body="# Test\n\nBody content."):
    """Helper to create a temporary skill directory with SKILL.md."""
    skill_dir = os.path.join(base_dir, name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return skill_dir


class TestSkillManagerLoad:

    def test_load_skill_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(tmpdir, "my_skill", "Does things.")
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            tc = mgr.load_skill(skill_dir)
            assert tc.tool_id == "my_skill"
            assert tc.tool_type == "skill"
            assert tc.name == "my_skill"
            assert tc.description == "Does things."
            assert tc.parameters == {"type": "object", "properties": {}, "required": []}

    def test_load_skill_registers_in_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(tmpdir, "my_skill", "Does things.")
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            tc = registry.get("my_skill")
            assert tc is not None
            assert tc.tool_type == "skill"

    def test_load_skill_no_command_tools_generated(self):
        """SkillManager should NOT generate any command execution tools."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(tmpdir, "my_skill", "Does things.")
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            # Only the skill itself should be registered, no _exec tool
            assert registry.get("my_skill_exec") is None
            all_tools = registry.list_all()
            assert len(all_tools) == 1
            assert all_tools[0].tool_id == "my_skill"

    def test_load_skill_body_stored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(
                tmpdir, "my_skill", "Does things.", body="# Detailed\n\nFull docs here."
            )
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            body = mgr.get_skill_body("my_skill")
            assert body is not None
            assert "Full docs here." in body

    def test_get_skill_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(tmpdir, "my_skill", "Does things.")
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            result = mgr.get_skill_dir("my_skill")
            assert result is not None
            assert os.path.isdir(result)

    def test_is_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(tmpdir, "my_skill", "Does things.")
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            assert mgr.is_skill("my_skill") is True
            assert mgr.is_skill("nonexistent") is False

    def test_load_skill_missing_skill_md_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            try:
                mgr.load_skill(tmpdir)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "SKILL.md not found" in str(e)

    def test_load_skills_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_skill_dir(tmpdir, "skill_a", "Skill A")
            _create_skill_dir(tmpdir, "skill_b", "Skill B")
            os.makedirs(os.path.join(tmpdir, "not_a_skill"), exist_ok=True)
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            results = mgr.load_skills_dir(tmpdir)
            assert len(results) == 2
            names = {tc.name for tc in results}
            assert names == {"skill_a", "skill_b"}


# ── Progressive disclosure tests ──


class TestProgressiveDisclosure:

    def test_skill_only_exposes_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = _create_skill_dir(
                tmpdir, "my_skill", "Short description.",
                body="# Full detailed documentation\n\nLots of content here.",
            )
            registry = ToolRegistry()
            mgr = SkillManager(registry)
            mgr.load_skill(skill_dir)
            tc = registry.get("my_skill")
            assert tc.description == "Short description."
            assert tc.parameters == {"type": "object", "properties": {}, "required": []}

    def test_builtin_tools_not_registered_at_init(self):
        """Runtime should NOT register bash/fetch at init — only on Skill disclosure."""
        from runtime import Runtime, ModelRegistry
        model_registry = ModelRegistry()
        registry = ToolRegistry()
        Runtime(model_registry=model_registry, tool_registry=registry)
        assert registry.get("bash") is None
        assert registry.get("fetch") is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
