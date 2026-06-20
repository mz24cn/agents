"""Integration test for ModelConfig and ToolConfig labels."""

import sys
import os

# Add the parent directory to the path so we can import runtime modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.models import ModelConfig, ToolConfig


def test_model_config_labels():
    """Test ModelConfig labels."""
    # Test 1: Create ModelConfig with labels
    config1 = ModelConfig(
        model_id="test1",
        api_base="http://localhost",
        model_name="test-model",
        labels=["label1", "label2", "label1"]
    )
    print(f"ModelConfig 1 labels: {config1.labels}")
    assert config1.labels == ["label1", "label2"]
    
    # Test 2: Serialize and deserialize
    data = config1.to_dict()
    print(f"Serialized data: {data}")
    assert data["labels"] == ["label1", "label2"]
    
    config2 = ModelConfig.from_dict(data)
    print(f"Deserialized labels: {config2.labels}")
    assert config2.labels == ["label1", "label2"]
    
    # Test 3: From dict with string labels
    data_with_string = {
        "model_id": "test3",
        "api_base": "http://localhost",
        "model_name": "test-model",
        "labels": "a,b,c,a"
    }
    config3 = ModelConfig.from_dict(data_with_string)
    print(f"ModelConfig 3 labels (from string): {config3.labels}")
    assert config3.labels == ["a", "b", "c"]
    
    print("ModelConfig labels tests passed!")


def test_tool_config_labels():
    """Test ToolConfig labels."""
    # Test 1: Create ToolConfig with labels
    config1 = ToolConfig(
        tool_id="test1",
        tool_type="function",
        name="Test Tool",
        description="A test tool",
        parameters={},
        labels=["tool1", "tool2", "tool1"]
    )
    print(f"ToolConfig 1 labels: {config1.labels}")
    assert config1.labels == ["tool1", "tool2"]
    
    # Test 2: Serialize and deserialize
    data = config1.to_dict()
    print(f"Serialized data: {data}")
    assert data["labels"] == ["tool1", "tool2"]
    
    config2 = ToolConfig.from_dict(data)
    print(f"Deserialized labels: {config2.labels}")
    assert config2.labels == ["tool1", "tool2"]
    
    # Test 3: From dict with string labels
    data_with_string = {
        "tool_id": "test3",
        "tool_type": "function",
        "name": "Test Tool",
        "description": "A test tool",
        "parameters": {},
        "labels": "x,y,z,x"
    }
    config3 = ToolConfig.from_dict(data_with_string)
    print(f"ToolConfig 3 labels (from string): {config3.labels}")
    assert config3.labels == ["x", "y", "z"]
    
    print("ToolConfig labels tests passed!")


if __name__ == "__main__":
    test_model_config_labels()
    test_tool_config_labels()
