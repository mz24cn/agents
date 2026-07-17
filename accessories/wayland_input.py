"""
Wayland-native input injection via Mutter RemoteDesktop + libei.
================================================================

原理:
  启动一个 C 守护进程 (./wayland_input_{arch}.exe) 作为持久子进程，
  通过 stdin 发送命令，stdout 读取响应。
  C 进程通过 DBus 创建 Mutter RemoteDesktop 会话，用 ConnectToEIS
  获取 EIS fd，再用 libei 做 POINTER_ABSOLUTE 绝对坐标定位。
  这样生成的输入事件与真实硬件无异，所有 Wayland 原生应用均正确响应。

  libei 是必须的——纯 DBus 方案只能做相对移动（NotifyPointerMotionRelative），
  无法实现精确的 move_abs(x, y)。

用法:
  from accessories import wayland_input as wi
  wi.ensure()           # 首次调用时自动编译 C 二进制
  wi.move_abs(500, 300)
  wi.click(1)
  wi.type_text("Hello")
  wi.shutdown()
"""

import os
import time
import logging
import subprocess
import threading
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# --- Globals ---
_proc: Optional[subprocess.Popen] = None
_ready = False
_screen_width = 0
_screen_height = 0
_lock = threading.Lock()
_binary_path: Optional[str] = None


def _find_binary() -> Optional[str]:
    """查找或自动编译 wayland_input 二进制。优先 arch 后缀名。"""
    import platform
    my_dir = os.path.dirname(os.path.abspath(__file__))
    arch = platform.machine()
    preferred = os.path.join(my_dir, f"wayland_input_{arch}.exe")
    generic = os.path.join(my_dir, "wayland_input")

    for p in [preferred, generic]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    # 二进制不存在，尝试自动编译
    source = os.path.join(my_dir, "wayland_input.c")
    if not os.path.isfile(source):
        return None

    logger.info("Compiling wayland_input for %s...", arch)
    try:
        subprocess.check_output(
            ["gcc", "-O2", "-o", preferred, source]
            + subprocess.check_output(
                ["pkg-config", "--cflags", "--libs", "glib-2.0", "gio-2.0", "libei-1.0"]
            ).decode().strip().split(),
            stderr=subprocess.STDOUT, text=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        logger.error("wayland_input compile failed:\n%s", e.output)
        return None
    except FileNotFoundError:
        logger.error("gcc not found. Install: sudo apt-get install gcc libei-dev libglib2.0-dev")
        return None

    if os.path.isfile(preferred):
        os.chmod(preferred, 0o755)
        return preferred
    return None


def _readline(timeout: float = 5.0) -> str:
    """从子进程 stdout 同步读取一行（带超时）。"""
    if _proc is None or _proc.stdout is None:
        raise RuntimeError("subprocess not running")

    import select
    r, _, _ = select.select([_proc.stdout], [], [], timeout)
    if not r:
        raise TimeoutError(f"timeout reading from wayland_input ({timeout}s)")

    line = _proc.stdout.readline()
    if not line:
        raise EOFError("wayland_input stdout closed")
    return line.decode("utf-8", errors="replace").strip()


def _send_cmd(cmd: str) -> str:
    """发送命令到 C 守护进程，读取响应（线程安全）。"""
    global _proc
    if _proc is None or _proc.stdin is None:
        raise RuntimeError(
            "wayland_input not ready. Call ensure() first. "
            "(If ensure() was called but failed, check stderr above.)"
        )

    with _lock:
        _proc.stdin.write((cmd + "\n").encode("utf-8"))
        _proc.stdin.flush()

        # 读取响应——跳过 READY 等中间行，直到 OK 或 ERROR
        response = _readline()
        while response and not response.startswith("OK") and not response.startswith("ERROR"):
            if response.startswith("READY "):
                parts = response.split()
                if len(parts) == 3:
                    global _screen_width, _screen_height
                    _screen_width = int(parts[1])
                    _screen_height = int(parts[2])
            response = _readline()
        return response


# --- Public API ---

def ensure():
    """启动 C 守护进程，等待 READY。幂等。"""
    global _proc, _ready, _screen_width, _screen_height, _binary_path

    if _ready:
        return

    # 查找二进制
    binary = _find_binary()
    if not binary:
        raise RuntimeError(
            "wayland_input binary not found. "
            "Compile: cd accessories && gcc -O2 -o wayland_input_x86_64.exe "
            "wayland_input.c $(pkg-config --cflags --libs glib-2.0 gio-2.0 libei-1.0)"
        )

    _binary_path = binary

    # 启动守护进程（-q = quiet，日志走 stderr）
    _proc = subprocess.Popen(
        [binary, "-q"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )

    # 同步等待 READY（最多约 10 秒）
    for _ in range(100):
        line = _readline(timeout=5.0)
        if line.startswith("READY "):
            parts = line.split()
            if len(parts) == 3:
                _screen_width = int(parts[1])
                _screen_height = int(parts[2])
            _ready = True
            logger.info("Wayland input ready (%dx%d)", _screen_width, _screen_height)
            return
        elif line.startswith("ERROR "):
            shutdown()
            raise RuntimeError(f"wayland_input init error: {line}")
    else:
        # 超时——收集 stderr 用于诊断
        stderr_output = ""
        if _proc.stderr:
            try:
                import select
                r, _, _ = select.select([_proc.stderr], [], [], 0.5)
                if r:
                    stderr_output = _proc.stderr.read(4096).decode(errors="replace")
            except Exception:
                pass
        shutdown()
        raise RuntimeError(
            f"wayland_input did not send READY. Is GNOME/Mutter running?\n"
            f"  WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')}\n"
            f"  XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')}\n"
            f"  stderr: {stderr_output}"
        )


def shutdown():
    """关闭守护进程。"""
    global _proc, _ready
    if _proc is not None:
        try:
            if _proc.stdin:
                _proc.stdin.write(b"quit\n")
                _proc.stdin.flush()
        except Exception:
            pass
        try:
            _proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait()
        _proc = None
    _ready = False


def is_available() -> bool:
    """Wayland 输入是否可用。"""
    return _ready


def screen_size() -> Tuple[int, int]:
    """返回屏幕尺寸 (width, height)。"""
    return _screen_width, _screen_height


def move_abs(x: int, y: int):
    """绝对坐标移动鼠标。使用 libei POINTER_ABSOLUTE。"""
    resp = _send_cmd(f"move_abs {x} {y}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def move_rel(dx: int, dy: int):
    """相对坐标移动鼠标。"""
    resp = _send_cmd(f"move_rel {dx} {dy}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def click(button: int = 1):
    """点击。1=左键, 2=中键, 3=右键。"""
    resp = _send_cmd(f"click {button}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def press(button: int = 1):
    """按下鼠标按键（不释放）。"""
    resp = _send_cmd(f"press {button}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def release(button: int = 1):
    """释放鼠标按键。"""
    resp = _send_cmd(f"release {button}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def key(keycode: int, state: int):
    """发送键盘事件。keycode 为 Linux evdev 键码，state: 1=按下, 0=释放。"""
    resp = _send_cmd(f"key {keycode} {state}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


def type_text(text: str):
    """输入字符串。"""
    resp = _send_cmd(f"type {text}")
    if resp.startswith("ERROR"):
        raise RuntimeError(resp)


# --- 键盘辅助 ---

_KEY_MAP = {
    "enter": 28, "return": 28,
    "space": 57, "spacebar": 57,
    "tab": 15,
    "backspace": 14, "delete": 111,
    "escape": 1, "esc": 1,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "shift": 42, "shiftleft": 42, "shiftright": 54,
    "ctrl": 29, "ctrlleft": 29, "ctrlright": 97,
    "alt": 56, "altleft": 56, "altright": 100,
    "win": 125, "winleft": 125, "super": 125,
    "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62,
    "f5": 63, "f6": 64, "f7": 65, "f8": 66,
    "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "printscreen": 99,
    "insert": 110,
}

_MOD_KEYS = {
    "shift", "shiftleft", "shiftright",
    "ctrl", "ctrlleft", "ctrlright",
    "alt", "altleft", "altright",
    "win", "winleft", "super",
}


def press_key(key_name: str):
    """按下并释放单个按键（如 'enter', 'space', 'a'）。"""
    key_lower = key_name.lower()
    kc = _KEY_MAP.get(key_lower)
    if kc is not None:
        key(kc, 1)
        time.sleep(0.02)
        key(kc, 0)
        return
    # 不在映射表中则当作字符输入
    if len(key_name) == 1:
        type_text(key_name)
        return
    type_text(key_name)


def hotkey(keys: List[str]):
    """同时按下多个按键（组合键）。如 hotkey(['ctrl', 'c'])。"""
    modifiers = []
    regular = []
    for k in keys:
        if k.lower() in _MOD_KEYS:
            modifiers.append(k.lower())
        else:
            regular.append(k)
    for m in modifiers:
        kc = _KEY_MAP.get(m)
        if kc:
            key(kc, 1)
            time.sleep(0.02)
    for k in regular:
        press_key(k)
    for m in reversed(modifiers):
        kc = _KEY_MAP.get(m)
        if kc:
            time.sleep(0.02)
            key(kc, 0)


def scroll(amount: int):
    """滚动鼠标滚轮。正数向上，负数向下。
    
    C 二进制不支持 scroll 命令，通过 PgUp/PgDn 模拟。
    """
    if not _ready:
        raise RuntimeError("Wayland input not available")
    if amount > 0:
        press_key("pageup")
    else:
        press_key("pagedown")
