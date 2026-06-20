"""Integration test for agent labels via AgentManager."""

import tempfile
import shutil
import os
import sys

# Add the parent directory to the path so we can import runtime modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.agent_manager import AgentManager


def test_agent_labels_integration():
    """Test agent labels integration."""
    temp_dir = tempfile.mkdtemp()
    try:
        manager = AgentManager(agents_dir=temp_dir)
        
        # Test 1: Create agent with labels list
        agent1 = manager.create(
            agent_id="test1",
            model_id="model1",
            nickname="Test Agent 1",
            labels=["label1", "label2", "label1"]
        )
        print(f"Agent 1 labels: {agent1['labels']}")
        assert agent1["labels"] == ["label1", "label2"]
        assert "group" not in agent1
        
        # Test 2: Create agent with string labels
        agent2 = manager.create(
            agent_id="test2",
            model_id="model1",
            nickname="Test Agent 2",
            labels="group1, group2, group1"
        )
        print(f"Agent 2 labels: {agent2['labels']}")
        assert agent2["labels"] == ["group1", "group2"]
        assert "group" not in agent2
        
        # Test 3: Update agent with labels
        updated = manager.update("test1", {"labels": ["new1", "new2", "new1"]})
        print(f"Updated agent 1 labels: {updated['labels']}")
        assert updated["labels"] == ["new1", "new2"]
        assert "group" not in updated
        
        # Test 4: List all agents
        agents = manager.list_all()
        print(f"Number of agents: {len(agents)}")
        for agent in agents:
            print(f"  Agent {agent['agent_id']}: labels={agent['labels']}")
        
        # Test 5: Get agent
        agent = manager.get("test1")
        print(f"Agent 1 labels: {agent['labels']}")
        assert agent["labels"] == ["new1", "new2"]
        
        # Test 6: Load from disk
        manager2 = AgentManager(agents_dir=temp_dir)
        manager2.load()
        agents2 = manager2.list_all()
        print(f"After load, number of agents: {len(agents2)}")
        for agent in agents2:
            print(f"  Agent {agent['agent_id']}: labels={agent['labels']}")
        
        print("\nAll tests passed!")
        
    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    test_agent_labels_integration()
