"""Agent ID validation tests."""

import os
import tempfile

import pytest

from runtime.agent_manager import AgentManager, validate_agent_id


def test_validate_agent_id_rejects_hyphen():
    with pytest.raises(ValueError, match="cannot contain"):
        validate_agent_id("code-agent")


def test_create_rejects_hyphenated_agent_id():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = AgentManager(agents_dir=temp_dir)
        with pytest.raises(ValueError, match="cannot contain"):
            manager.create(
                agent_id="code-agent",
                model_id="model1",
                nickname="Code Agent",
            )
        assert manager.list_all() == []
        assert not os.path.exists(os.path.join(temp_dir, "agents", "code-agent.json"))


def test_update_rejects_hyphenated_agent_id_without_mutation():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = AgentManager(agents_dir=temp_dir)
        manager.create(
            agent_id="code_agent",
            model_id="model1",
            nickname="Code Agent",
        )

        with pytest.raises(ValueError, match="cannot contain"):
            manager.update("code_agent", {"agent_id": "code-agent"})

        assert manager.get("code_agent")["agent_id"] == "code_agent"
        assert manager.get("code-agent") is None
        assert os.path.isfile(os.path.join(temp_dir, "agents", "code_agent.json"))
        assert not os.path.exists(os.path.join(temp_dir, "agents", "code-agent.json"))
