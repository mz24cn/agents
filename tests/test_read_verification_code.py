#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
read_verification_code 工具测试（桩测试，无需真机）

重点覆盖 UI 兜底的"读码后还原界面"行为：
  1. _get_focused_component: 从 dumpsys window 提取当前焦点组件（跳过 null 行）
  2. _restore_screen_to:
     - 无记录 / 之前是桌面(启动器) → 回桌面
     - 之前就在短信 App 内 → 保持不动（不发任何 adb 命令）
     - 其他 App → am start -n 精确恢复原 Activity
     - am start 失败 → 退化为默认入口 monkey，再失败回桌面
  3. _read_verification_code_via_ui: 读码成功/失败都会还原到原界面（不回桌面）
  4. 端到端: content:// 全空 → UI 兜底读到验证码 → 返回成功且界面已还原
"""

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "accessories"))

import android_use_mcp as m  # noqa: E402


PREV_APP = "com.example.app/.MainActivity"
SMS_PKG = "com.android.mms"
LAUNCHER = "com.miui.home/com.miui.home.launcher.Launcher"
AM_START_CMD = f"shell am start -n {PREV_APP} 2>&1"
MOVE_TASK_CMD = "shell cmd activity stack move-task 42 42 true"

# 模拟 dumpsys activity recents 输出（节选真实格式）：
# PREV_APP 在任务 42（独立任务，类似微信小程序），启动器在任务 43
RECENTS_OUT = """ACTIVITY MANAGER RECENT TASKS (dumpsys activity recents)
mRecentsUid=10138
  Recent tasks:
  * Recent #0: Task{aaa1111 #42 type=standard A=10086:com.example.app}
    userId=0 effectiveUid=u0a86 mCallingUid=u0a86
    intent={flg=0x10000000 cmp=com.example.app/.MainActivity}
    mActivityComponent=com.example.app/.MainActivity
    Activities=[ActivityRecord{111 u0 com.example.app/.MainActivity t42}]
    taskId=42 rootTaskId=42
  * Recent #1: Task{bbb2222 #43 type=home I=com.miui.home/.launcher.Launcher}
    userId=0 effectiveUid=u0a138
    intent={act=android.intent.action.MAIN cmp=com.miui.home/.launcher.Launcher}
    mActivityComponent=com.miui.home/.launcher.Launcher
    Activities=[ActivityRecord{222 u0 com.miui.home/.launcher.Launcher t43}]
    taskId=43 rootTaskId=43
  Visible recent tasks (most recent first):
  * RecentTaskInfo #0: 
    id=42 userId=0 hasTask=true
    baseActivity={com.example.app/com.example.app.MainActivity}
"""


def _adb_json_result(stdout, rc=0, stderr=""):
    return json.dumps({
        "success": rc == 0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": rc,
    })


class FakeAdb:
    """run_adb_command 的桩：按命令关键字分发响应，并记录所有调用。"""

    def __init__(self, focus=PREV_APP, am_ok=True, monkey_ok=True, sms_holder=True,
                 move_ok=True, recents_empty=False, force_stop_reveals=True):
        self.calls = []
        self._focus = focus  # 当前焦点组件（am start / home 会改变它）
        self._am_ok = am_ok
        self._monkey_ok = monkey_ok
        self._sms_holder = sms_holder
        # move_ok: True=还原成功；False=报错；"noop"=返回码 0 但界面纹丝不动
        # （实测 HyperOS 上 `move-task X X true` 就是这个静默空操作）
        self._move_ok = move_ok
        self._recents_empty = recents_empty
        # 关掉前台 App 后系统是否露出它下面的原任务（真机实测会露出）
        self._force_stop_reveals = force_stop_reveals

    @property
    def focus(self):
        return self._focus

    # ---------- responder ----------
    def __call__(self, command, device_id=None):
        self.calls.append(command)
        cmd = command

        if "dumpsys activity recents" in cmd:
            return _adb_json_result("" if self._recents_empty else RECENTS_OUT)
        if cmd.startswith("shell cmd activity stack move-task"):
            if self._move_ok == "noop":
                # 真机踩到的坑：返回码 0、无任何报错，但界面根本没变
                return _adb_json_result("")
            if self._move_ok:
                self._focus = PREV_APP  # 模拟原任务被拉到前台
                return _adb_json_result("")
            return _adb_json_result(
                "", rc=255,
                stderr="java.lang.IllegalStateException: No task for taskId=42")
        if "mCurrentFocus" in cmd:
            if self._focus:
                out = (f"  mCurrentFocus=null\n"
                       f"  mCurrentFocus=Window{{abc123 u0 {self._focus}}}\n")
            else:
                out = "  mCurrentFocus=null\n"
            return _adb_json_result(out)
        if cmd.startswith("shell wm size"):
            return _adb_json_result("Physical size: 1080x2400")
        if "dumpsys display" in cmd:
            return _adb_json_result("mState=ON")
        if ("keyevent 224" in cmd or "svc power" in cmd
                or "screen_brightness" in cmd or "keyevent 26" in cmd
                or "CLOSE_SYSTEM_DIALOGS" in cmd or "keyevent 82" in cmd
                or "input swipe" in cmd):
            return _adb_json_result("")
        if "cmd role get-holder" in cmd:
            return _adb_json_result(SMS_PKG if self._sms_holder else "")
        if "pm list packages" in cmd:
            pkg = cmd.rsplit(" ", 1)[1]
            return _adb_json_result(f"package:{pkg}" if self._sms_holder else "")
        if "force-stop" in cmd:
            pkg = cmd.rsplit(" ", 1)[-1]
            if self._focus and self._focus.split("/", 1)[0] == pkg:
                # 关掉我们自己拉起的 App 后，系统露出它下面的原任务（实测行为）
                if self._force_stop_reveals:
                    self._focus = PREV_APP
            return _adb_json_result("")
        if cmd.startswith("shell monkey -p com.android.mms"):
            if self._monkey_ok:
                self._focus = f"{SMS_PKG}/.ui.MmsTabActivity"  # 短信 App 到前台
                return _adb_json_result("Events injected: 1")
            return _adb_json_result("No activities found")
        if cmd.startswith("shell monkey -p com.example.app"):
            if self._monkey_ok:
                self._focus = PREV_APP
                return _adb_json_result("Events injected: 1")
            return _adb_json_result("No activities found")
        if cmd.startswith("shell am start -n"):
            comp = cmd.split("shell am start -n ", 1)[1].split(" ", 1)[0]
            if self._am_ok:
                self._focus = comp
                return _adb_json_result(f"Starting: Intent {{ cmp={comp} }}")
            # 真实 am start 失败时 rc 非 0（SecurityException 等输出在 stderr）
            return _adb_json_result(
                "", rc=255,
                stderr=f"Exception occurred while executing 'start':\n"
                       f"java.lang.SecurityException: Permission Denial: "
                       f"starting Intent ... {comp} ... not exported from uid")
        if "input tap" in cmd:
            return _adb_json_result("")
        if cmd.startswith("shell input keyevent 3"):
            self._focus = LAUNCHER
            return _adb_json_result("")
        if ("content query" in cmd or cmd.startswith("shell dumpsys content")
                or cmd.startswith("shell dumpsys package")):
            return _adb_json_result("No result found.")
        return _adb_json_result("")

    # ---------- 断言辅助 ----------
    @property
    def move_task_calls(self):
        return [c for c in self.calls if c.startswith("shell cmd activity stack move-task")]

    @property
    def am_start_calls(self):
        return [c for c in self.calls if c.startswith("shell am start -n")]

    @property
    def home_calls(self):
        return [c for c in self.calls if c.startswith("shell input keyevent 3")]

    def monkey_for(self, pkg):
        return [c for c in self.calls if c.startswith(f"shell monkey -p {pkg}")]


def _ocr_lines(text, h_frac):
    """构造 OCR 响应：单行文本，位于屏幕 h_frac 高度处。"""
    return {
        "success": True,
        "full_text": text,
        "lines": [{
            "texts": [text],
            "x_ranges": [[100, 900]],
            "y_ranges": [[int(2400 * h_frac), int(2400 * h_frac) + 40]],
        }],
    }


def _env_patches(fake, ocr_responder=None):
    return [
        mock.patch.object(m, "run_adb_command", side_effect=fake),
        mock.patch.object(m, "call_mcp_ocr_tool",
                          return_value=ocr_responder or {"success": False}),
        mock.patch.object(m, "adb_screenshot_to_base64", return_value="QUJD"),
        mock.patch("time.sleep", side_effect=lambda *a, **k: None),
        mock.patch.object(m, "_discover_message_authorities", return_value=[]),
    ]


def _with_env(fake, ocr_responder=None):
    class Ctx:
        def __enter__(self):
            self._ms = [p.start() for p in _env_patches(fake, ocr_responder)]
            m._discover_cache.clear()
            return self

        def __exit__(self, *exc):
            for p in reversed(self._ms):
                p.stop()
            m._discover_cache.clear()
            return False
    return Ctx()


# ==================== 1. 焦点组件提取 ====================

def test_get_focused_component_skips_null_lines():
    """多屏输出里第一行可能是 null，要取到真正带组件名的行。"""
    with _with_env(FakeAdb(focus=PREV_APP)):
        assert m._get_focused_component(None) == PREV_APP


def test_get_focused_component_all_null_returns_empty():
    with _with_env(FakeAdb(focus="")):
        assert m._get_focused_component(None) == ""


# ==================== 2. 任务 ID 提取 ====================

def test_short_component():
    assert (m._short_component(
        "com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI01")
        == "com.tencent.mm/.plugin.appbrand.ui.AppBrandUI01")
    assert m._short_component("com.example.app/.MainActivity") == \
        "com.example.app/.MainActivity"


def test_get_focused_task_info_finds_task():
    """从 recents 任务列表里找到包含焦点组件的任务，返回 taskId/rootTaskId。"""
    with _with_env(FakeAdb()):
        assert m._get_focused_task_info(None, PREV_APP) == ("42", "42")


def test_get_focused_task_info_not_found():
    with _with_env(FakeAdb(recents_empty=True)):
        assert m._get_focused_task_info(None, PREV_APP) is None
    with _with_env(FakeAdb()):
        # recents 里没有的任务（如被系统清掉）→ None，还原时走降级链
        assert m._get_focused_task_info(None, "com.other.app/.X") is None
    assert m._get_focused_task_info(None, "") is None


# ==================== 3. 界面还原 ====================

def test_restore_no_prev_goes_home():
    fake = FakeAdb()
    with _with_env(fake):
        m._restore_screen_to(None, "", SMS_PKG)
    assert fake.home_calls, "无记录时应回桌面"
    assert not fake.am_start_calls


def test_restore_prev_is_sms_app_is_noop():
    """之前就在短信 App 内：保持当前界面，不发任何 adb 命令。"""
    fake = FakeAdb(focus=f"{SMS_PKG}/{SMS_PKG}.ui.MmsTabActivity")
    with _with_env(fake):
        m._restore_screen_to(None, f"{SMS_PKG}/{SMS_PKG}.ui.MmsTabActivity", SMS_PKG)
    assert fake.calls == []


def test_restore_prev_is_launcher_goes_home():
    fake = FakeAdb()
    with _with_env(fake):
        m._restore_screen_to(None, LAUNCHER, SMS_PKG)
    assert fake.home_calls
    assert not fake.am_start_calls


def test_restore_closes_sms_app_to_reveal_prev_task():
    """首选：关掉我们刚拉起的短信 App，系统露出它下面的原任务（实测最可靠）。

    比 am start -n 更强：不受 Activity 是否导出(exported)限制，小程序页也能回去。
    """
    fake = FakeAdb(focus=f"{SMS_PKG}/.ui.MmsTabActivity")
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert f"shell am force-stop {SMS_PKG}" in fake.calls
    assert fake.focus == PREV_APP, "关闭短信 App 后焦点应回到原 App"
    assert not fake.am_start_calls, "已经还原成功就不该再 am start"
    assert not fake.move_task_calls
    assert not fake.home_calls


def test_restore_prev_app_uses_am_start():
    """force-stop 没能露出原任务时，用 am start -n 精确恢复原 Activity。"""
    fake = FakeAdb(focus=LAUNCHER, force_stop_reveals=False)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG)
    assert fake.am_start_calls == [AM_START_CMD]
    assert fake.focus == PREV_APP
    assert not fake.home_calls


def test_restore_am_fail_falls_back_to_monkey():
    """am start 失败（未导出的 Activity）→ 退化到该应用默认入口。"""
    fake = FakeAdb(focus=LAUNCHER, force_stop_reveals=False,
                   am_ok=False, monkey_ok=True)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG)
    assert fake.monkey_for("com.example.app"), "am start 失败应尝试默认入口"
    assert fake.focus == PREV_APP
    assert not fake.home_calls


def test_restore_all_fail_goes_home():
    fake = FakeAdb(focus=LAUNCHER, force_stop_reveals=False, move_ok=False,
                   am_ok=False, monkey_ok=False)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert fake.home_calls, "所有途径失败时兜底回桌面"


def test_restore_with_task_uses_move_task():
    """有任务 ID 时用 move-task 还原原任务：
    未导出的 Activity（如微信小程序页）am start -n 会被系统拒绝，
    按任务 ID 还原才是唯一能回到原页面的途径。"""
    fake = FakeAdb(focus=LAUNCHER, force_stop_reveals=False)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert fake.move_task_calls == [MOVE_TASK_CMD]
    assert not fake.am_start_calls, "move-task 成功就不需要 am start"
    assert not fake.home_calls


def test_restore_move_task_fail_falls_back_to_am_start():
    fake = FakeAdb(focus=LAUNCHER, force_stop_reveals=False,
                   move_ok=False, am_ok=True)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert fake.move_task_calls == [MOVE_TASK_CMD]
    assert fake.am_start_calls == [AM_START_CMD], "move-task 失败应降级 am start"
    assert not fake.home_calls


def test_restore_move_task_silent_noop_must_not_stop_there():
    """回归测试（真机踩到的坑）：`move-task X X true` 返回码 0 却没有效果时，
    绝不能当成还原成功就 return —— 否则用户会卡在短信界面出不来。
    必须继续走后面的兜底，最终离开短信 App。
    """
    fake = FakeAdb(focus=f"{SMS_PKG}/.ui.MmsTabActivity",
                   force_stop_reveals=False, move_ok="noop", am_ok=True)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert fake.move_task_calls == [MOVE_TASK_CMD]
    assert fake.am_start_calls == [AM_START_CMD], "空操作后必须继续兜底"
    assert fake.focus == PREV_APP
    assert not fake.home_calls


def test_restore_move_task_and_am_fail_falls_back_to_monkey():
    fake = FakeAdb(focus=f"{SMS_PKG}/.ui.MmsTabActivity", force_stop_reveals=False,
                   move_ok=False, am_ok=False, monkey_ok=True)
    with _with_env(fake):
        m._restore_screen_to(None, PREV_APP, SMS_PKG, prev_task=("42", "42"))
    assert fake.monkey_for("com.example.app"), "前两级都失败应走默认入口"
    assert fake.focus == PREV_APP
    assert not fake.home_calls


# ==================== 4. UI 兜底读码 + 还原 ====================

UI_LIST_TEXT = "中山医院 5G消息小助手\n您的验证码为134595，5分钟内有效"


def test_ui_fallback_success_restores_prev_app():
    fake = FakeAdb(focus=PREV_APP)
    with _with_env(fake, _ocr_lines(UI_LIST_TEXT, 0.15)):
        result = m._read_verification_code_via_ui(None, prev_component=PREV_APP)
    assert result is not None
    code, text = result
    assert code == "134595"
    # 关键断言：还原到原 App（关掉临时拉起的短信 App），而不是回桌面
    assert f"shell am force-stop {SMS_PKG}" in fake.calls
    assert fake.focus == PREV_APP
    assert not fake.home_calls
    # 顺序：先打开短信 App 读码，最后才关掉它还原
    # （注意 _launch_default_sms_app 开头也会 force-stop 一次，所以取最后一次）
    launch = "shell monkey -p com.android.mms -c android.intent.category.LAUNCHER 1"
    last_close = max(i for i, c in enumerate(fake.calls)
                     if c == f"shell am force-stop {SMS_PKG}")
    assert fake.calls.index(launch) < last_close


def test_ui_fallback_failure_still_restores():
    fake = FakeAdb(focus=PREV_APP)
    with _with_env(fake, _ocr_lines("暂无新消息", 0.15)):
        result = m._read_verification_code_via_ui(None, prev_component=PREV_APP)
    assert result is None
    assert f"shell am force-stop {SMS_PKG}" in fake.calls, \
        "读码失败也必须还原界面，不能把用户丢在短信页"
    assert fake.focus == PREV_APP
    assert not fake.home_calls
    assert m._ui_last_note, "失败原因应回传，便于定位（是没收到还是 OCR 问题）"


def test_ui_fallback_no_blind_tap_when_ocr_returns_nothing():
    """OCR 一张文本都没识别出来时，绝不能按固定坐标盲点会话：
    那只会把用户丢进一个同样读不出码的会话里，还连带还原失败卡住。"""
    empty_ocr = {"success": True, "full_text": "", "lines": []}
    fake = FakeAdb(focus=PREV_APP)
    with _with_env(fake, empty_ocr):
        result = m._read_verification_code_via_ui(None, prev_component=PREV_APP)
    assert result is None
    assert not [c for c in fake.calls if "input tap" in c], "不应盲点会话"
    assert f"shell am force-stop {SMS_PKG}" in fake.calls, "仍要还原界面"
    assert "OCR" in m._ui_last_note


def test_ui_fallback_no_prev_component_goes_home():
    fake = FakeAdb(focus="")
    with _with_env(fake, _ocr_lines(UI_LIST_TEXT, 0.15)):
        result = m._read_verification_code_via_ui(None, prev_component="")
    assert result is not None
    assert result[0] == "134595"
    assert fake.home_calls, "无界面记录时回桌面"
    assert not fake.am_start_calls


def test_ui_fallback_no_sms_app_still_restores():
    fake = FakeAdb(focus=PREV_APP, sms_holder=False)
    with _with_env(fake, _ocr_lines(UI_LIST_TEXT, 0.15)):
        result = m._read_verification_code_via_ui(None, prev_component=PREV_APP)
    assert result is None
    assert fake.am_start_calls == [AM_START_CMD], \
        "找不到短信 App 时也要还原用户界面"


def test_ui_fallback_with_task_restores_via_move_task():
    """之前界面有任务 ID（如微信小程序这类独立任务）：
    读码后按任务 ID 还原原任务，而不是 am start/回桌面。"""
    fake = FakeAdb(focus=PREV_APP, force_stop_reveals=False)
    with _with_env(fake, _ocr_lines(UI_LIST_TEXT, 0.15)):
        result = m._read_verification_code_via_ui(
            None, prev_component=PREV_APP, prev_task=("42", "42"))
    assert result is not None
    assert result[0] == "134595"
    assert fake.move_task_calls == [MOVE_TASK_CMD]
    assert fake.focus == PREV_APP
    assert not fake.am_start_calls
    assert not fake.home_calls


# ==================== 4. 端到端 ====================

def test_end_to_end_content_empty_ui_fallback_restores():
    """content:// 全读不到 → UI 兜底读到码 → 返回成功，且界面还原到原 App。"""
    fake = FakeAdb(focus=PREV_APP)
    with _with_env(fake, _ocr_lines(UI_LIST_TEXT, 0.15)):
        out = m.read_verification_code(None)
    data = json.loads(out)
    assert data["success"] is True
    assert data["verification_code"] == "134595"
    assert data["source"] == "UI-OCR"
    # 还原：关掉临时拉起的短信 App，焦点回到原 App
    assert f"shell am force-stop {SMS_PKG}" in fake.calls
    assert fake.focus == PREV_APP
    assert not fake.home_calls, "不能把用户丢回桌面"


# ==================== 5. content:// 查询命令拼装（"短信读不出来"的根因） ====================

def test_shell_arg_quotes_space_and_redirect():
    """设备端 sh 会重新解析命令：含空格/重定向符的参数必须整体加引号。

    真机实测：`--sort date DESC` 会被 sh 拆成两个参数，content 工具只打 usage +
    "Unsupported argument: DESC"（返回码仍是 0）；`--where date > 123` 里的 >
    会被当成写文件重定向（"can't create 123: Read-only file system"）。
    """
    assert m._shell_arg("date DESC") == '"date DESC"'
    assert m._shell_arg("date > 123") == '"date > 123"'
    assert m._shell_arg("_id:date:body") == "_id:date:body"
    assert m._shell_arg("") == ""


def test_content_query_builds_quoted_sort_and_where():
    calls = []

    def fake(command, device_id=None):
        calls.append(command)
        return json.dumps({"success": True, "stdout": "Row: 0 date=1 body=hi"})

    with mock.patch.object(m, "run_adb_command", side_effect=fake):
        out = m._content_query_raw(
            None, "content://sms", projection="address:date:body",
            sort="date DESC", where="date > 123")

    assert out == "Row: 0 date=1 body=hi"
    assert '--sort "date DESC"' in calls[0]
    assert '--where "date > 123"' in calls[0]


def test_content_query_unsupported_arg_is_treated_as_empty():
    """参数不被支持时 content 只打 usage + [ERROR]（返回码仍为 0），
    不能当成查询结果 —— 否则会解析出"没有消息"的假象。"""
    usage = ("usage: adb shell content [subcommand] [options]\n\n"
             "[ERROR] Unsupported argument: DESC")

    with mock.patch.object(m, "run_adb_command",
                           side_effect=lambda c, d=None: json.dumps(
                               {"success": True, "stdout": usage})):
        assert m._content_query_raw(None, "content://sms", sort="date DESC") == ""


def test_content_query_retries_without_limit():
    """部分 ROM（HyperOS）不支持 --limit：带上失败后自动去掉重试。"""
    calls = []

    def fake(command, device_id=None):
        calls.append(command)
        if "--limit" in command:
            return json.dumps({
                "success": True,
                "stdout": ("usage: adb shell content\n"
                           "[ERROR] Unsupported argument: --limit"),
            })
        return json.dumps({"success": True, "stdout": "Row: 0 date=1 snippet=hi"})

    with mock.patch.object(m, "run_adb_command", side_effect=fake):
        out = m._content_query_raw(None, "content://mms", limit=10)

    assert out == "Row: 0 date=1 snippet=hi"
    assert len(calls) == 2 and "--limit" not in calls[1]


# ==================== 6. 会话行定位 ====================

def test_top_conversation_center_none_when_no_candidate():
    """定位不到会话行时返回 None（调用方据此跳过点击），而不是给固定坐标盲点。"""
    assert m._top_conversation_center([], 1080, 2400) is None
    lines = [{
        "texts": ["底部文字"],
        "x_ranges": [[10, 100]],
        "y_ranges": [[2100, 2150]],
    }]
    assert m._top_conversation_center(lines, 1080, 2400) is None


def test_top_conversation_center_picks_topmost_row():
    lines = [
        {"texts": ["第二条"], "x_ranges": [[100, 900]],
         "y_ranges": [[900, 960]]},
        {"texts": ["第一条"], "x_ranges": [[100, 900]],
         "y_ranges": [[400, 460]]},
    ]
    assert m._top_conversation_center(lines, 1080, 2400) == (500, 430)
