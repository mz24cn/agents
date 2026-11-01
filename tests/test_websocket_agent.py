#!/usr/bin/env python3
"""
测试 agent service 的 WebSocket 支持
"""
import socket
import json
import time
import hashlib
import base64
import struct
import sys


def test_websocket(host='localhost', port=7988):
    """测试 WebSocket 连接"""
    print(f"测试 WebSocket 连接到 {host}:{port}...")
    
    try:
        # 创建 TCP 连接
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, port))
        
        # WebSocket 握手
        key = base64.b64encode(b'test-key-123456789012345').decode()
        handshake = (
            f"GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode())
        
        # 读取响应
        response = sock.recv(4096).decode()
        print("握手响应:")
        print(response[:200])
        
        if '101' in response:
            print("\n✓ WebSocket 握手成功!")
            
            # 读取欢迎消息
            try:
                data = sock.recv(4096)
                if data:
                    # 解码 WebSocket 帧
                    header = data[:2]
                    b1, b2 = header[0], header[1]
                    payload_len = b2 & 0x7F
                    payload = data[2:2+payload_len].decode('utf-8', errors='replace')
                    print(f"收到消息: {payload[:100]}...")
            except Exception as e:
                print(f"读取消息失败: {e}")
            
            # 发送测试命令
            test_cmd = "echo 'Hello from WebSocket test'\n"
            masked = mask_data(test_cmd.encode())
            sock.sendall(masked)
            print(f"\n发送命令: {test_cmd.strip()}")
            
            # 读取响应
            time.sleep(0.5)
            try:
                data = sock.recv(4096)
                if data:
                    header = data[:2]
                    b1, b2 = header[0], header[1]
                    payload_len = b2 & 0x7F
                    payload = data[2:2+payload_len].decode('utf-8', errors='replace')
                    print(f"响应: {payload[:200]}...")
            except Exception as e:
                print(f"读取响应失败: {e}")
        else:
            print("✗ WebSocket 握手失败")
        
        sock.close()
        
    except ConnectionRefusedError:
        print(f"✗ 连接被拒绝，请确保服务运行在 {host}:{port}")
    except socket.timeout:
        print("✗ 连接超时")
    except Exception as e:
        print(f"✗ 错误: {e}")


def mask_data(data):
    """创建带掩码的 WebSocket 帧"""
    mask = b'\x01\x02\x03\x04'
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    
    frame = bytearray([0x81])  # FIN + TEXT
    length = len(data)
    frame.append(0x80 | length)  # 设置掩码位
    frame.extend(mask)
    frame.extend(masked)
    return frame


if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else 'localhost'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 7988
    test_websocket(host, port)
