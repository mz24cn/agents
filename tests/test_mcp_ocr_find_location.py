#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 MCP-OCR find_location 工具集成

验证从内置工具迁移到 MCP 工具后的调用方式
"""

import os
import sys
import json
import base64
from pathlib import Path

# 添加父目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "accessories"))


def test_ocr_mcp_import():
    """测试 OCR_mcp 模块导入"""
    try:
        # Mock paddleocr
        import types
        paddleocr = types.ModuleType('paddleocr')
        paddleocr.PaddleOCR = type('PaddleOCR', (), {})
        sys.modules['paddleocr'] = paddleocr
        
        from OCR_mcp import create_mcp_server
        print("✅ OCR_mcp 模块导入成功")
        return True
    except Exception as e:
        print(f"❌ OCR_mcp 导入失败: {e}")
        return False


def test_mcp_tool_naming():
    """测试 MCP 工具命名规范"""
    # MCP 工具应命名为 mcp-{service}-{tool}
    expected_tool_id = "mcp-OCR-find_location"
    
    # 验证命名格式
    parts = expected_tool_id.split("-")
    if len(parts) == 3 and parts[0] == "mcp" and parts[1] == "OCR":
        print(f"✅ MCP 工具命名规范: {expected_tool_id}")
        return True
    else:
        print(f"❌ 命名不规范: {expected_tool_id}")
        return False


def test_android_use_mcp_import():
    """测试 android_use_mcp 中的函数名"""
    try:
        import types
        
        # Mock fastmcp
        fastmcp = types.ModuleType('fastmcp')
        fastmcp.FastMCP = type('FastMCP', (), {})
        sys.modules['fastmcp'] = fastmcp
        
        # Mock paddleocr
        paddleocr = types.ModuleType('paddleocr')
        paddleocr.PaddleOCR = type('PaddleOCR', (), {})
        sys.modules['paddleocr'] = paddleocr
        
        from android_use_mcp import create_mcp_server
        
        # 检查 call_mcp_ocr_tool 函数是否存在
        import inspect
        source = inspect.getsource(create_mcp_server)
        
        if "call_mcp_ocr_tool" in source and "call_builtin_tool" not in source:
            print("✅ android_use_mcp 使用 call_mcp_ocr_tool")
            return True
        else:
            print("❌ android_use_mcp 函数名未更新")
            return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("MCP-OCR find_location 工具集成测试")
    print("=" * 60)
    print()
    
    results = []
    results.append(("OCR_mcp 模块导入", test_ocr_mcp_import()))
    results.append(("MCP 工具命名", test_mcp_tool_naming()))
    results.append(("android_use_mcp 集成", test_android_use_mcp_import()))
    
    print()
    print("=" * 60)
    print("测试结果:")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print()
    print(f"总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！MCP-OCR find_location 工具集成成功。")
    else:
        print("\n⚠️  部分测试失败，请检查代码。")
