"""Test labels parsing and deduplication logic."""

import pytest
import sys
import os

# Add the parent directory to the path so we can import runtime modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.common import parse_labels
from runtime.agent_manager import AgentManager


class TestParseLabels:
    """Test the parse_labels function."""
    
    def test_none_input(self):
        """Test None input returns empty list."""
        assert parse_labels(None) == []
    
    def test_empty_string(self):
        """Test empty string returns empty list."""
        assert parse_labels("") == []
        assert parse_labels("   ") == []
    
    def test_single_label(self):
        """Test single label string."""
        assert parse_labels("test") == ["test"]
        assert parse_labels("  test  ") == ["test"]
    
    def test_multiple_labels_comma_separated(self):
        """Test comma-separated labels."""
        assert parse_labels("a,b,c") == ["a", "b", "c"]
        assert parse_labels("a, b, c") == ["a", "b", "c"]
        assert parse_labels("a , b , c") == ["a", "b", "c"]
    
    def test_deduplication_preserves_order(self):
        """Test that deduplication preserves order of first occurrence."""
        assert parse_labels("a,b,a,c,b") == ["a", "b", "c"]
        assert parse_labels("a, b, a, c, b") == ["a", "b", "c"]
    
    def test_list_input(self):
        """Test list input."""
        assert parse_labels(["a", "b", "c"]) == ["a", "b", "c"]
        assert parse_labels(["a", "b", "a", "c"]) == ["a", "b", "c"]
    
    def test_list_with_empty_strings(self):
        """Test list with empty strings and whitespace."""
        assert parse_labels(["a", "", "b", " ", "c"]) == ["a", "b", "c"]
    
    def test_mixed_types_in_list(self):
        """Test list with mixed types."""
        assert parse_labels(["a", 1, "b", True]) == ["a", "1", "b", "True"]
    
    def test_non_string_input(self):
        """Test non-string input converted to string."""
        assert parse_labels(123) == ["123"]
        assert parse_labels(True) == ["True"]


class TestAgentManagerLabels:
    """Test AgentManager labels handling."""
    
    def test_create_with_labels_list(self):
        """Test creating agent with labels as list."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            manager = AgentManager(agents_dir=temp_dir)
            agent = manager.create(
                agent_id="test1",
                model_id="model1",
                nickname="Test Agent",
                labels=["label1", "label2", "label1"]
            )
            assert agent["labels"] == ["label1", "label2"]
            assert "group" not in agent
        finally:
            shutil.rmtree(temp_dir)
    
    def test_create_with_string_labels(self):
        """Test creating agent with comma-separated string labels."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            manager = AgentManager(agents_dir=temp_dir)
            agent = manager.create(
                agent_id="test2",
                model_id="model1",
                nickname="Test Agent",
                labels="group1, group2, group1"
            )
            assert agent["labels"] == ["group1", "group2"]
            assert "group" not in agent
        finally:
            shutil.rmtree(temp_dir)
    
    def test_update_with_labels(self):
        """Test updating agent labels."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            manager = AgentManager(agents_dir=temp_dir)
            manager.create(
                agent_id="test3",
                model_id="model1",
                nickname="Test Agent",
                labels=["initial"]
            )
            
            updated = manager.update("test3", {"labels": ["new1", "new2", "new1"]})
            assert updated["labels"] == ["new1", "new2"]
            assert "group" not in updated
        finally:
            shutil.rmtree(temp_dir)
    
    def test_list_all(self):
        """Test that list_all returns labels field."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            manager = AgentManager(agents_dir=temp_dir)
            manager.create(
                agent_id="test5",
                model_id="model1",
                nickname="Test Agent",
                labels=["primary", "secondary"]
            )
            
            agents = manager.list_all()
            assert len(agents) == 1
            assert agents[0]["labels"] == ["primary", "secondary"]
            assert "group" not in agents[0]
        finally:
            shutil.rmtree(temp_dir)
    
    def test_get(self):
        """Test that get returns labels field."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            manager = AgentManager(agents_dir=temp_dir)
            manager.create(
                agent_id="test6",
                model_id="model1",
                nickname="Test Agent",
                labels=["main"]
            )
            
            agent = manager.get("test6")
            assert agent is not None
            assert agent["labels"] == ["main"]
            assert "group" not in agent
        finally:
            shutil.rmtree(temp_dir)


class TestModelConfigLabels:
    """Test ModelConfig labels handling."""
    
    def test_model_config_with_labels(self):
        """Test ModelConfig with labels."""
        from runtime.models import ModelConfig
        
        config = ModelConfig(
            model_id="test",
            api_base="http://localhost",
            model_name="test-model",
            labels=["label1", "label2", "label1"]
        )
        assert config.labels == ["label1", "label2"]
        
        # Test serialization
        data = config.to_dict()
        assert data["labels"] == ["label1", "label2"]
        
        # Test deserialization
        config2 = ModelConfig.from_dict(data)
        assert config2.labels == ["label1", "label2"]
    
    def test_model_config_from_dict_with_string_labels(self):
        """Test ModelConfig.from_dict with string labels."""
        from runtime.models import ModelConfig
        
        data = {
            "model_id": "test",
            "api_base": "http://localhost",
            "model_name": "test-model",
            "labels": "a,b,c,a"
        }
        config = ModelConfig.from_dict(data)
        assert config.labels == ["a", "b", "c"]


class TestToolConfigLabels:
    """Test ToolConfig labels handling."""
    
    def test_tool_config_with_labels(self):
        """Test ToolConfig with labels."""
        from runtime.models import ToolConfig
        
        config = ToolConfig(
            tool_id="test",
            tool_type="function",
            name="Test Tool",
            description="A test tool",
            parameters={},
            labels=["tool1", "tool2"]
        )
        assert config.labels == ["tool1", "tool2"]
        
        # Test serialization
        data = config.to_dict()
        assert data["labels"] == ["tool1", "tool2"]
        
        # Test deserialization
        config2 = ToolConfig.from_dict(data)
        assert config2.labels == ["tool1", "tool2"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
