"""Test read_file output limits (EXEC_OUTPUT_LINE_LIMIT and EXEC_OUTPUT_COLUMN_LIMIT)."""

import json
import os
import sys
import tempfile
import pytest

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.builtin_tools import _read_file


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace directory."""
    os.environ["AGENT_WORKSPACE"] = str(tmp_path)
    yield tmp_path
    os.environ.pop("AGENT_WORKSPACE", None)


class TestReadFileOutputLimits:
    """Test read_file output limits."""
    
    def test_read_file_respects_EXEC_OUTPUT_LINE_LIMIT(self, workspace):
        """Test that read_file respects EXEC_OUTPUT_LINE_LIMIT."""
        # Create a file with 10 lines
        test_file = workspace / "test_output_limits.txt"
        with open(test_file, "w") as f:
            for i in range(1, 11):
                f.write(f"Line {i}\n")
        
        # Set EXEC_OUTPUT_LINE_LIMIT to 5
        os.environ["EXEC_OUTPUT_LINE_LIMIT"] = "5"
        
        try:
            result = json.loads(_read_file("test_output_limits.txt"))
            
            # Should have truncated output
            assert result["truncated"] == True
            assert result["total_lines"] == 10
            
            # Content should contain only first 5 lines with line numbers
            content_lines = result["content"].split("\n")
            # Filter out the truncation notice line
            actual_lines = [line for line in content_lines if not line.startswith("[...output truncated")]
            
            assert len(actual_lines) == 5
            assert actual_lines[0] == "1: Line 1"
            assert actual_lines[4] == "5: Line 5"
            
            # Should have omitted_lines in result
            assert "omitted_lines" in result
            assert result["omitted_lines"] == 5
        finally:
            os.environ.pop("EXEC_OUTPUT_LINE_LIMIT", None)
    
    def test_read_file_respects_EXEC_OUTPUT_COLUMN_LIMIT(self, workspace):
        """Test that read_file respects EXEC_OUTPUT_COLUMN_LIMIT."""
        # Create a file with a long line
        test_file = workspace / "test_column_limits.txt"
        long_line = "A" * 2000  # 2000 characters
        with open(test_file, "w") as f:
            f.write(long_line + "\n")
            f.write("Short line\n")
        
        # Set EXEC_OUTPUT_COLUMN_LIMIT to 100
        os.environ["EXEC_OUTPUT_COLUMN_LIMIT"] = "100"
        
        try:
            result = json.loads(_read_file("test_column_limits.txt"))
            
            # Should have truncated output
            assert result["truncated"] == True
            
            # Content should have truncated long line
            content_lines = result["content"].split("\n")
            assert len(content_lines) == 2
            
            # First line should be truncated
            first_line = content_lines[0]
            assert first_line.startswith("1: ")
            assert first_line.endswith("...")
            assert len(first_line) <= 100 + 50  # Line number prefix + some margin
            
            # Second line should be unchanged
            assert content_lines[1] == "2: Short line"
            
            # Should have omitted_lines (0 since no lines were omitted, only columns)
            assert "omitted_lines" in result
            assert result["omitted_lines"] == 0
        finally:
            os.environ.pop("EXEC_OUTPUT_COLUMN_LIMIT", None)
    
    def test_read_file_no_truncation_when_within_limits(self, workspace):
        """Test that read_file doesn't truncate when within limits."""
        # Create a file with 5 lines
        test_file = workspace / "test_no_truncation.txt"
        with open(test_file, "w") as f:
            for i in range(1, 6):
                f.write(f"Line {i}\n")
        
        # Set limits higher than file size
        os.environ["EXEC_OUTPUT_LINE_LIMIT"] = "100"
        os.environ["EXEC_OUTPUT_COLUMN_LIMIT"] = "100"
        
        try:
            result = json.loads(_read_file("test_no_truncation.txt"))
            
            # Should not be truncated
            assert result["truncated"] == False
            assert result["total_lines"] == 5
            
            # Content should contain all lines
            content_lines = result["content"].split("\n")
            assert len(content_lines) == 5
            assert content_lines[0] == "1: Line 1"
            assert content_lines[4] == "5: Line 5"
            
            # Should not have omitted_lines
            assert "omitted_lines" not in result
        finally:
            os.environ.pop("EXEC_OUTPUT_LINE_LIMIT", None)
            os.environ.pop("EXEC_OUTPUT_COLUMN_LIMIT", None)
    
    def test_read_file_with_range_and_limits(self, workspace):
        """Test read_file with explicit range and output limits."""
        # Create a file with 10 lines
        test_file = workspace / "test_range_limits.txt"
        with open(test_file, "w") as f:
            for i in range(1, 11):
                f.write(f"Line {i}\n")
        
        # Set EXEC_OUTPUT_LINE_LIMIT to 3
        os.environ["EXEC_OUTPUT_LINE_LIMIT"] = "3"
        
        try:
            # Request lines 3-7 (5 lines)
            result = json.loads(_read_file("test_range_limits.txt", start_line=3, end_line=7))
            
            # Should be truncated by output limit (3 lines)
            assert result["truncated"] == True
            assert result["total_lines"] == 10
            
            # Content should contain only first 3 of the requested 5 lines
            content_lines = result["content"].split("\n")
            actual_lines = [line for line in content_lines if not line.startswith("[...output truncated")]
            
            assert len(actual_lines) == 3
            assert actual_lines[0] == "3: Line 3"
            assert actual_lines[2] == "5: Line 5"
            
            # Should have omitted_lines
            assert "omitted_lines" in result
            assert result["omitted_lines"] == 7  # 10 total - 3 shown
        finally:
            os.environ.pop("EXEC_OUTPUT_LINE_LIMIT", None)
    
    def test_read_file_both_limits_apply(self, workspace):
        """Test that both line and column limits apply together."""
        # Create a file with 10 lines, some long
        test_file = workspace / "test_both_limits.txt"
        with open(test_file, "w") as f:
            for i in range(1, 11):
                if i % 2 == 0:  # Even lines are long
                    f.write(f"Line {i}: " + "X" * 500 + "\n")
                else:  # Odd lines are short
                    f.write(f"Line {i}\n")
        
        # Set limits
        os.environ["EXEC_OUTPUT_LINE_LIMIT"] = "5"
        os.environ["EXEC_OUTPUT_COLUMN_LIMIT"] = "50"
        
        try:
            result = json.loads(_read_file("test_both_limits.txt"))
            
            # Should be truncated
            assert result["truncated"] == True
            assert result["total_lines"] == 10
            
            # Content should respect both limits
            content_lines = result["content"].split("\n")
            actual_lines = [line for line in content_lines if not line.startswith("[...output truncated")]
            
            # Should have only 5 lines due to line limit
            assert len(actual_lines) == 5
            
            # Check that long lines are truncated
            for line in actual_lines:
                if line.startswith("2: ") or line.startswith("4: "):
                    # These lines should be truncated
                    assert line.endswith("...")
                    assert len(line) <= 50 + 50  # Column limit + line number prefix + margin
            
            # Should have omitted_lines
            assert "omitted_lines" in result
            assert result["omitted_lines"] == 5  # 10 total - 5 shown
        finally:
            os.environ.pop("EXEC_OUTPUT_LINE_LIMIT", None)
            os.environ.pop("EXEC_OUTPUT_COLUMN_LIMIT", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])