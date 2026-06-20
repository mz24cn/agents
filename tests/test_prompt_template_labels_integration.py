"""Integration test for PromptTemplate labels."""

import tempfile
import shutil
import os
import sys

# Add the parent directory to the path so we can import runtime modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.prompt_template_manager import PromptTemplateManager


def test_prompt_template_labels_integration():
    """Test PromptTemplate labels integration."""
    temp_dir = tempfile.mkdtemp()
    temp_file = os.path.join(temp_dir, "templates.json")
    try:
        manager = PromptTemplateManager()
        
        # Test 1: Create template with labels
        template1 = manager.create(
            template_id="test1",
            content="Hello {name}",
            labels=["label1", "label2", "label1"]
        )
        print(f"Template 1 labels: {template1.labels}")
        assert template1.labels == ["label1", "label2"]
        
        # Test 2: Create template with string labels (via from_dict)
        from runtime.prompt_template_manager import PromptTemplate
        template2 = PromptTemplate.from_dict({
            "template_id": "test2",
            "content": "Hello {name}",
            "labels": "tag1, tag2, tag1"
        })
        print(f"Template 2 labels (from string): {template2.labels}")
        assert template2.labels == ["tag1", "tag2"]
        
        # Test 3: Update template with labels
        updated = manager.update("test1", "test1_updated", "Updated {name}", labels=["new1", "new2", "new1"])
        print(f"Updated template labels: {updated.labels}")
        assert updated.labels == ["new1", "new2"]
        
        # Test 4: Save and load
        manager.save(temp_file)
        manager2 = PromptTemplateManager()
        manager2.load(temp_file)
        loaded_template = manager2.get("test1_updated")
        print(f"Loaded template labels: {loaded_template.labels}")
        assert loaded_template.labels == ["new1", "new2"]
        
        # Test 5: List all templates
        templates = manager.list_all()
        print(f"Number of templates: {len(templates)}")
        for template in templates:
            print(f"  Template {template.template_id}: labels={template.labels}")
        
        print("\nAll PromptTemplate labels tests passed!")
        
    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    test_prompt_template_labels_integration()
