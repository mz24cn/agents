#coding=utf-8
"""
OCR MCP Server

基于 PaddleOCR 的 OCR 服务，提供文字识别和文字定位功能。

前置条件：
pip install fastmcp paddleocr paddlepaddle opencv-python-headless pillow numpy

功能：
1. ocr - 对图片进行 OCR 识别，返回排序合并后的文本字符串
2. detect_text_blocks - 定位图片中的文字，返回所有文字块的位置范围
3. find_image - 在大图中查找小图的位置，返回小图在大图中的坐标范围
4. find_location - 综合定位工具，支持文字模式匹配和/或图片匹配

使用方法：
    # HTTP 方式运行（默认）
    python OCR_mcp.py
    
    # 指定端口
    python OCR_mcp.py --port 8002
    
    # SSE 方式运行
    python OCR_mcp.py --transport sse
    
    # stdio 方式运行（本地调试）
    python OCR_mcp.py --transport stdio
    
    # 指定 GPU
    python OCR_mcp.py --gpu 0

MCP Server 配置（添加到 Agent Service）：
{
  "mcpServers": {
    "ocr": {
      "url": "http://<remote-host>:8002/mcp",
      "headers": {}
    }
  }
}
"""

import base64
import os
import sys
import json
import io
import re
import logging
import argparse
from typing import Optional

import numpy as np
import cv2
from PIL import Image, ImageOps

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "true"
from paddleocr import PaddleOCR

from mcp.server.fastmcp import FastMCP

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局模型实例
_model = None


def load_model():
    """加载 PaddleOCR 模型（兼容 2.x 和 3.x）"""
    try:
        return PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False)
    except (TypeError, ValueError):
        return PaddleOCR(use_angle_cls=False)


def get_model():
    """获取或懒加载模型"""
    global _model
    if _model is None:
        logger.info("首次调用，正在加载 PaddleOCR 模型...")
        _model = load_model()
        logger.info("模型加载完成")
    return _model


def decode_image(binary_data: bytes) -> np.ndarray:
    """将二进制图片数据解码为 BGR numpy 数组（兼容各种格式和 EXIF 旋转）"""
    with Image.open(io.BytesIO(binary_data)) as img:
        img = ImageOps.exif_transpose(img)
        img_rgb = img.convert('RGB')
        frame = np.array(img_rgb)
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return frame_bgr


def predict_ocr(imgs):
    """执行 OCR 识别，兼容 PaddleOCR 2.x 和 3.x"""
    model = get_model()

    if hasattr(model, 'predict'):  # PaddleOCR 3.x
        results = model.predict(imgs)
        return [{
            'boxes': result.json['res']['rec_boxes'],
            'texts': result.json['res']['rec_texts'],
            'scores': result.json['res']['rec_scores'],
            'width': imgs[i].shape[1],
            'height': imgs[i].shape[0]
        } for i, result in enumerate(results)]

    results = []
    for img in imgs:
        ocr_result = model.ocr(img, cls=False)  # PaddleOCR 2.x
        if ocr_result and len(ocr_result) == 1 and (not ocr_result[0] or isinstance(ocr_result[0][0], list)):
            ocr_result = ocr_result[0] or []

        boxes = []
        texts = []
        scores = []
        for line in ocr_result or []:
            points = np.array(line[0]).reshape(-1, 2)
            x1, y1 = points.min(axis=0)
            x2, y2 = points.max(axis=0)
            boxes.append([int(x1), int(y1), int(x2), int(y2)])
            texts.append(line[1][0])
            scores.append(line[1][1])

        results.append({
            'boxes': boxes,
            'texts': texts,
            'scores': scores,
            'width': img.shape[1],
            'height': img.shape[0]
        })
    return results


def cluster_text_blocks_into_lines(locations, threshold_ratio=0.3):
    """
    将文本块按行聚类
    
    Args:
        locations: 文本块列表，每个元素包含 text、x_range、y_range、score
        threshold_ratio: 阈值比例，用于判断是否属于同一行（相对于文本块高度）
    
    Returns:
        按行分组的文本块列表
    """
    if not locations:
        return []
    
    # 计算每个文本块的中心点和高度
    blocks_with_info = []
    for loc in locations:
        x1, x2 = loc['x_range']
        y1, y2 = loc['y_range']
        center_y = (y1 + y2) / 2
        height = y2 - y1
        blocks_with_info.append({
            'text': loc['text'],
            'x1': x1,
            'x2': x2,
            'y1': y1,
            'y2': y2,
            'center_y': center_y,
            'height': height,
            'score': loc['score']
        })
    
    # 先按 y1 排序
    blocks_with_info.sort(key=lambda x: x['y1'])
    
    # 聚类分组
    lines = []
    current_line = [blocks_with_info[0]]
    
    for block in blocks_with_info[1:]:
        # 计算当前块与当前行中所有块的重叠程度
        should_merge = False
        avg_height = sum(b['height'] for b in current_line) / len(current_line)
        threshold = avg_height * threshold_ratio
        
        # 检查与当前行中任意块的垂直重叠
        for line_block in current_line:
            # 计算垂直重叠区域
            overlap_y1 = max(block['y1'], line_block['y1'])
            overlap_y2 = min(block['y2'], line_block['y2'])
            overlap_height = max(0, overlap_y2 - overlap_y1)
            
            # 如果重叠高度超过阈值，则认为是同一行
            min_height = min(block['height'], line_block['height'])
            if overlap_height >= min_height * threshold_ratio:
                should_merge = True
                break
        
        if should_merge:
            current_line.append(block)
        else:
            lines.append(current_line)
            current_line = [block]
    
    lines.append(current_line)
    
    # 对每行内的文本块按 x1 排序
    for line in lines:
        line.sort(key=lambda x: x['x1'])
    
    return lines


def merge_text_lines(lines, add_spaces_for_english=True):
    """
    将分好行的文本块合并成字符串
    
    Args:
        lines: 按行分组的文本块列表
        add_spaces_for_english: 是否为英文单词添加空格
    
    Returns:
        合并后的字符串
    """
    result_lines = []
    
    for line in lines:
        line_text = []
        prev_x2 = None
        
        for block in line:
            if prev_x2 is not None and add_spaces_for_english:
                # 检查是否需要添加空格
                gap = block['x1'] - prev_x2
                avg_char_width = (block['x2'] - block['x1']) / max(1, len(block['text']))
                
                # 如果间隙超过半个字符宽度，且文本包含英文字符，则添加空格
                if gap > avg_char_width * 0.5 and any(c.isascii() and c.isalpha() for c in block['text']):
                    line_text.append(' ')
            
            line_text.append(block['text'])
            prev_x2 = block['x2']
        
        result_lines.append(''.join(line_text))
    
    return '\n'.join(result_lines)


# ---------------------------------------------------------------------------
# find_location implementation
# ---------------------------------------------------------------------------

def read_image_as_base64(image_input: str) -> str:
    """Read an image from local file path or return as-is if already base64.

    Args:
        image_input: Local file path or base64 encoded string

    Returns:
        Base64 encoded image content

    Raises:
        FileNotFoundError: If image_input is a file path that doesn't exist
        ValueError: If image_input is neither a valid file path nor valid base64
    """
    # Read threshold from environment variable, default to 1024
    base64_check_threshold = int(os.environ.get("BASE64_CHECK_THRESHOLD", "1024"))

    # First check if the string is short (likely a file path)
    if len(image_input) < base64_check_threshold:
        # Check if it's a local file path
        if os.path.isfile(image_input):
            with open(image_input, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        # Not a valid file, raise FileNotFoundError
        raise FileNotFoundError(f"Image file not found: {image_input}")

    # String is long, assume it's base64 and validate
    try:
        # Attempt to decode to verify it's valid base64
        base64.b64decode(image_input, validate=True)
        # Return original string as-is
        return image_input
    except Exception as e:
        raise ValueError(f"Invalid base64 string: {image_input}. Error: {str(e)}")


def _find_location_by_pattern(base64_image: str, pattern: str) -> list[dict]:
    """Find locations in image matching the pattern using OCR detect_text_blocks.
    
    Args:
        base64_image: Base64 encoded image
        pattern: Regex pattern to match
        
    Returns:
        List of location dicts with x_range, y_range, score, text
    """
    # Direct call to local detect_text_blocks function (no HTTP overhead)
    result_json = detect_text_blocks(base64_image)
    result = json.loads(result_json)
    
    if not result.get("success"):
        logger.warning(f"detect_text_blocks failed: {result.get('message', 'unknown error')}")
        return []
    
    locations = result.get("locations", [])
    matched = []
    
    for loc in locations:
        text = loc.get("text", "")
        if re.search(pattern, text):
            matched.append({
                "x_range": loc.get("x_range", []),
                "y_range": loc.get("y_range", []),
                "score": loc.get("score", 0),
                "text": text
            })
    
    return matched


def _find_location_by_image(base64_big: str, base64_small: str) -> list[dict]:
    """Find small image location in big image using template matching.
    
    Args:
        base64_big: Base64 encoded big image
        base64_small: Base64 encoded small image
        
    Returns:
        List containing location dict with x_range, y_range, score
    """
    # Direct call to local find_image function (no HTTP overhead)
    result_json = find_image(base64_big, base64_small)
    result = json.loads(result_json)
    
    if not result.get("success"):
        logger.warning(f"find_image failed: {result.get('message', 'unknown error')}")
        return []
    
    x_range = result.get("x_range", [])
    y_range = result.get("y_range", [])
    
    if not x_range or not y_range:
        return []
    
    return [{
        "x_range": x_range,
        "y_range": y_range,
        "score": result.get("score", 0)
    }]



def create_mcp_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:
    """创建 MCP server 实例并注册工具"""
    server = FastMCP(
        "ocr",
        instructions="OCR 服务：基于 PaddleOCR 的文字识别和定位，支持 base64 图片输入",
        host=host,
        port=port,
    )

    @server.tool()
    def detect_text_blocks(base64_content: str) -> str:
        """
        定位图片中的文字，返回所有文字块的位置范围。

        Args:
            base64_content: base64 编码的图片内容。可提供本地文件路径，底层会自动读取并编码。

        Returns:
            JSON 格式的定位结果，每个元素包含 text、x_range、y_range、score
        """
        try:
            binary_data = base64.b64decode(base64_content)
            img = decode_image(binary_data)
            ocr_results = predict_ocr([img])

            if not ocr_results:
                return json.dumps({"success": True, "locations": []}, ensure_ascii=False)

            result = ocr_results[0]
            locations = []
            for i in range(len(result['texts'])):
                box = result['boxes'][i]
                x1, y1, x2, y2 = box
                locations.append({
                    "text": result['texts'][i],
                    "x_range": [x1, x2],
                    "y_range": [y1, y2],
                    "score": result['scores'][i]
                })

            return json.dumps({
                "success": True,
                "locations": locations
            }, ensure_ascii=False)

        except Exception as e:
            error_msg = f"文字定位失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)
    
    @server.tool()
    def ocr(base64_content: str, threshold_ratio: float = 0.3, add_spaces: bool = True) -> str:
        """
        对图片进行 OCR 识别，并将文本块按阅读顺序排序合并成字符串。

        Args:
            base64_content: base64 编码的图片内容。可提供本地文件路径，底层会自动读取并编码。
            threshold_ratio: 行聚类阈值比例（0-1），越大越容易合并到同一行。建议值0.3
            add_spaces: 是否为英文单词自动添加空格

        Returns:
            JSON 格式的结果，包含排序合并后的文本、行分组信息和原始定位数据
        """
        try:
            binary_data = base64.b64decode(base64_content)
            img = decode_image(binary_data)
            ocr_results = predict_ocr([img])

            if not ocr_results:
                return json.dumps({
                    "success": True,
                    "full_text": "",
                    "lines": [],
                    "locations": []
                }, ensure_ascii=False)

            result = ocr_results[0]
            locations = []
            for i in range(len(result['texts'])):
                box = result['boxes'][i]
                x1, y1, x2, y2 = box
                locations.append({
                    "text": result['texts'][i],
                    "x_range": [x1, x2],
                    "y_range": [y1, y2],
                    "score": result['scores'][i]
                })

            lines = cluster_text_blocks_into_lines(locations, threshold_ratio=threshold_ratio)

            full_text = merge_text_lines(lines, add_spaces_for_english=add_spaces)

            line_info = []
            for line in lines:
                line_texts = [block['text'] for block in line]
                line_x_ranges = [[block['x1'], block['x2']] for block in line]
                line_y_ranges = [[block['y1'], block['y2']] for block in line]
                line_info.append({
                    "texts": line_texts,
                    "x_ranges": line_x_ranges,
                    "y_ranges": line_y_ranges
                })

            return json.dumps({
                "success": True,
                "full_text": full_text,
                "lines": line_info,
                "locations": locations,
                "width": result['width'],
                "height": result['height']
            }, ensure_ascii=False)

        except Exception as e:
            error_msg = f"OCR 识别失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)

    @server.tool()
    def find_image(base64_big_img: str, base64_small_img: str) -> str:
        """
        在大图中查找小图的位置。

        Args:
            base64_big_img: base64 编码的大图内容。可提供本地文件路径，底层会自动读取并编码。
            base64_small_img: base64 编码的要查找的小图内容。可提供本地文件路径，底层会自动读取并编码。

        Returns:
            JSON 格式的结果，包含小图坐标区间 x_range, y_range 和匹配置信度 score
        """
        try:
            big_binary = base64.b64decode(base64_big_img)
            small_binary = base64.b64decode(base64_small_img)

            big_img = decode_image(big_binary)
            small_img = decode_image(small_binary)

            big_gray = cv2.cvtColor(big_img, cv2.COLOR_BGR2GRAY)
            small_gray = cv2.cvtColor(small_img, cv2.COLOR_BGR2GRAY)

            small_gray = cv2.equalizeHist(small_gray)
            big_gray = cv2.equalizeHist(big_gray)

            result = cv2.matchTemplate(big_gray, small_gray, cv2.TM_CCOEFF_NORMED)

            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            top_left = max_loc
            h, w = small_gray.shape[:2]

            return json.dumps({
                "success": True,
                "x_range": [top_left[0], top_left[0] + w],
                "y_range": [top_left[1], top_left[1] + h],
                "score": float(max_val)
            }, ensure_ascii=False)

        except Exception as e:
            error_msg = f"模板匹配失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)

    @server.tool()
    def find_location(
        base64_image_big: str,
        pattern: Optional[str] = None,
        base64_image_small: Optional[str] = None,
    ) -> str:
        """
        在大图中查找目标位置（支持文字模式匹配和/或小图匹配）。

        至少提供 pattern 或 base64_image_small 之一。当两者都提供时，
        优先返回文字匹配的位置，但会选择距离小图匹配最近的那个文字匹配。

        Args:
            base64_image_big: 大图（base64 编码或本地文件路径）
            pattern: 可选，正则表达式模式（非特殊字符 = 关键词搜索）
            base64_image_small: 可选，要查找的小图（base64 编码或本地文件路径）

        Returns:
            JSON 格式的结果，包含：
            - success: 是否成功
            - x_range: [min_x, max_x] 坐标范围
            - y_range: [min_y, max_y] 坐标范围
            - center_x, center_y: 中心点坐标
            - score: 匹配置信度
            - text: 匹配的文字（如果有）
            - source: 匹配来源 (pattern/image/pattern+image)
        """
        # Validate inputs
        if not pattern and not base64_image_small:
            return json.dumps({
                "success": False,
                "message": "At least one of pattern or base64_image_small must be provided"
            }, ensure_ascii=False)

        # Read images (support both file paths and base64)
        try:
            big_image = read_image_as_base64(base64_image_big)
            small_image = read_image_as_base64(base64_image_small) if base64_image_small else None
        except FileNotFoundError as e:
            return json.dumps({
                "success": False,
                "message": str(e)
            }, ensure_ascii=False)
        except ValueError as e:
            return json.dumps({
                "success": False,
                "message": str(e)
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "success": False,
                "message": f"Failed to read image: {str(e)}"
            }, ensure_ascii=False)
        
        # Search by pattern (text)
        pattern_matches = []
        if pattern:
            pattern_matches = _find_location_by_pattern(big_image, pattern)
        
        # Search by small image
        image_matches = []
        if small_image:
            image_matches = _find_location_by_image(big_image, small_image)
        
        # Determine final result based on the algorithm:
        # 1. If one returns empty, use the other (if non-empty)
        # 2. If both return non-empty, use pattern position but select based on proximity to image match
        
        if not pattern_matches and not image_matches:
            return json.dumps({
                "success": False,
                "message": "No match found"
            }, ensure_ascii=False)
        
        if not pattern_matches:
            # Only image matches found
            best = image_matches[0]
            x_range = best["x_range"]
            y_range = best["y_range"]
            return json.dumps({
                "success": True,
                "x_range": x_range,
                "y_range": y_range,
                "center_x": (x_range[0] + x_range[1]) // 2,
                "center_y": (y_range[0] + y_range[1]) // 2,
                "score": best.get("score", 0),
                "text": None,
                "source": "image"
            }, ensure_ascii=False)
        
        if not image_matches:
            # Only pattern matches found
            best = pattern_matches[0]
            x_range = best["x_range"]
            y_range = best["y_range"]
            return json.dumps({
                "success": True,
                "x_range": x_range,
                "y_range": y_range,
                "center_x": (x_range[0] + x_range[1]) // 2,
                "center_y": (y_range[0] + y_range[1]) // 2,
                "score": best.get("score", 0),
                "text": best.get("text"),
                "source": "pattern"
            }, ensure_ascii=False)
        
        # Both have matches: find pattern match closest to any image match
        def get_center(loc):
            x_range = loc["x_range"]
            y_range = loc["y_range"]
            return ((x_range[0] + x_range[1]) / 2, (y_range[0] + y_range[1]) / 2)
        
        def distance(p1, p2):
            return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5
        
        # Get centers of all image matches
        image_centers = [get_center(m) for m in image_matches]
        
        # Find pattern match closest to any image match
        best_pattern = None
        min_dist = float('inf')
        
        for p_match in pattern_matches:
            p_center = get_center(p_match)
            for i_center in image_centers:
                dist = distance(p_center, i_center)
                if dist < min_dist:
                    min_dist = dist
                    best_pattern = p_match
        
        if best_pattern:
            x_range = best_pattern["x_range"]
            y_range = best_pattern["y_range"]
            return json.dumps({
                "success": True,
                "x_range": x_range,
                "y_range": y_range,
                "center_x": (x_range[0] + x_range[1]) // 2,
                "center_y": (y_range[0] + y_range[1]) // 2,
                "score": best_pattern.get("score", 0),
                "text": best_pattern.get("text"),
                "source": "pattern+image",
                "distance_to_image": min_dist
            }, ensure_ascii=False)
        
        # Fallback (should not reach here)
        return json.dumps({
            "success": False,
            "message": "Failed to determine best match"
        }, ensure_ascii=False)

    return server


def main():
    """主入口函数，供 uvx 和命令行调用"""
    parser = argparse.ArgumentParser(description="OCR MCP Server (PaddleOCR)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
        help="传输协议 (默认: streamable-http)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="监听端口 (默认: 8000)"
    )
    parser.add_argument(
        "--mount-path",
        default="/mcp",
        help="SSE/HTTP 挂载路径 (默认: /mcp)"
    )
    parser.add_argument(
        "--gpu",
        default="0",
        help="GPU 设备编号 (默认: 0)"
    )

    args = parser.parse_args()

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    logger.info("启动 OCR MCP Server...")
    logger.info(f"传输协议: {args.transport}")
    logger.info(f"GPU 设备: {args.gpu}")

    if args.transport in ("sse", "streamable-http"):
        logger.info(f"监听地址: {args.host}:{args.port}")
        logger.info(f"挂载路径: {args.mount_path}")
        logger.info(f"访问地址: http://{args.host}:{args.port}{args.mount_path}")

    # 创建 MCP server（传递 host 和 port）
    mcp = create_mcp_server(host=args.host, port=args.port)

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
