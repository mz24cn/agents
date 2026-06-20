"""Full lifecycle test for agent labels."""

import tempfile
import shutil
import os
import sys

# Add the parent directory to the path so we can import runtime modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.agent_manager import AgentManager
from runtime.models import ModelConfig, ToolConfig
from runtime.prompt_template_manager import PromptTemplateManager


def test_full_lifecycle():
    """Test full lifecycle of labels across all components."""
    temp_dir = tempfile.mkdtemp()
    try:
        print("=== Testing Agent Labels ===")
        agent_manager = AgentManager(agents_dir=temp_dir)
        
        # Create agents with various label formats
        agent1 = agent_manager.create(
            agent_id="agent1",
            model_id="model1",
            nickname="Agent 1",
            labels=["research", "science", "research"]  # duplicate
        )
        
        agent2 = agent_manager.create(
            agent_id="agent2",
            model_id="model1",
            nickname="Agent 2",
            labels="engineering, development, engineering"  # string input, duplicate
        )
        
        agent3 = agent_manager.create(
            agent_id="agent3",
            model_id="model1",
            nickname="Agent 3",
            labels=["marketing", "sales"]  # no duplicates
        )
        
        print(f"Agent 1: labels={agent1['labels']}")
        print(f"Agent 2: labels={agent2['labels']}")
        print(f"Agent 3: labels={agent3['labels']}")
        assert agent1["labels"] == ["research", "science"]
        assert agent2["labels"] == ["engineering", "development"]
        assert agent3["labels"] == ["marketing", "sales"]
        
        # Update agent1 labels
        updated_agent1 = agent_manager.update("agent1", {"labels": ["science", "physics", "science"]})
        print(f"Updated Agent 1: labels={updated_agent1['labels']}")
        assert updated_agent1["labels"] == ["science", "physics"]
        
        # List all agents
        all_agents = agent_manager.list_all()
        print(f"\nAll agents ({len(all_agents)}):")
        for agent in all_agents:
            print(f"  {agent['agent_id']}: {agent['labels']}")
        
        print("\n=== Testing ModelConfig Labels ===")
        model_config = ModelConfig(
            model_id="test-model",
            api_base="http://localhost:11434",
            model_name="qwen3:9b",
            labels=["local", "fast", "local"]  # duplicate
        )
        print(f"ModelConfig labels: {model_config.labels}")
        assert model_config.labels == ["local", "fast"]
        
        # Serialize and deserialize
        model_data = model_config.to_dict()
        model_config2 = ModelConfig.from_dict(model_data)
        print(f"Deserialized ModelConfig labels: {model_config2.labels}")
        assert model_config2.labels == ["local", "fast"]
        
        print("\n=== Testing ToolConfig Labels ===")
        tool_config = ToolConfig(
            tool_id="test-tool",
            tool_type="function",
            name="test_function",
            description="A test function",
            parameters={},
            labels=["utility", "helper", "utility"]  # duplicate
        )
        print(f"ToolConfig labels: {tool_config.labels}")
        assert tool_config.labels == ["utility", "helper"]
        
        # Serialize and deserialize
        tool_data = tool_config.to_dict()
        tool_config2 = ToolConfig.from_dict(tool_data)
        print(f"Deserialized ToolConfig labels: {tool_config2.labels}")
        assert tool_config2.labels == ["utility", "helper"]
        
        print("\n=== Testing PromptTemplate Labels ===")
        template_manager = PromptTemplateManager()
        
        template1 = template_manager.create(
            template_id="template1",
            content="Hello {name}",
            labels=["greeting", "basic", "greeting"]  # duplicate
        )
        print(f"Template 1 labels: {template1.labels}")
        assert template1.labels == ["greeting", "basic"]
        
        # Update template
        updated_template = template_manager.update(
            "template1", "template1_updated",
            "Hi {name}",
            labels=["welcome", "greeting", "welcome"]
        )
        print(f"Updated template labels: {updated_template.labels}")
        assert updated_template.labels == ["welcome", "greeting"]
        
        # Save and load
        temp_file = os.path.join(temp_dir, "templates.json")
        template_manager.save(temp_file)
        template_manager2 = PromptTemplateManager()
        template_manager2.load(temp_file)
        loaded_template = template_manager2.get("template1_updated")
        print(f"Loaded template labels: {loaded_template.labels}")
        assert loaded_template.labels == ["welcome", "greeting"]
        
        print("\n=== Testing Edge Cases ===")
        # Test empty labels
        agent_empty = agent_manager.create(
            agent_id="agent_empty",
            model_id="model1",
            nickname="Empty Labels Agent",
            labels=[]
        )
        print(f"Agent with empty labels: labels={agent_empty['labels']}")
        assert agent_empty["labels"] == []
        
        # Test None labels
        agent_none = agent_manager.create(
            agent_id="agent_none",
            model_id="model1",
            nickname="None Labels Agent",
            labels=None
        )
        print(f"Agent with None labels: labels={agent_none['labels']}")
        assert agent_none["labels"] == []
        
        # Test empty string labels
        agent_empty_str = agent_manager.create(
            agent_id="agent_empty_str",
            model_id="model1",
            nickname="Empty String Labels Agent",
            labels=""
        )
        print(f"Agent with empty string labels: labels={agent_empty_str['labels']}")
        assert agent_empty_str["labels"] == []
        
        # Test migration: old agent JSON with only group field
        import json
        old_agent_file = os.path.join(temp_dir, "agents", "old_agent.json")
        with open(old_agent_file, "w") as f:
            json.dump({
                "agent_id": "old_agent",
                "model_id": "model1",
                "nickname": "Old Agent",
                "group": "legacy, migrated",
                "last_modified": "2024-01-01"
            }, f)
        
        manager3 = AgentManager(agents_dir=temp_dir)
        manager3.load()
        old_agent = manager3.get("old_agent")
        print(f"Old agent after migration: labels={old_agent['labels']}")
        assert old_agent["labels"] == ["legacy", "migrated"]
        assert "group" not in old_agent
        
        print("\n=== All tests passed! ===")
        
    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    test_full_lifecycle()
