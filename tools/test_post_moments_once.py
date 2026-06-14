"""
一次性测试：发纯文字朋友圈，发完立即退出（不关闭微信）。

流程：先居中微信 → 打开朋友圈并居中 → 相机右键选「发表文字」→ 填文案 → 发表。

用法:
    python tools/test_post_moments_once.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import pyautogui

try:
    import psutil
except ImportError:
    psutil = None

pyautogui.FAILSAFE = False

from weixin_pace import apply_pace, patch_wxid_folder_lookup

from pyweixin.Config import GlobalConfig
from pyweixin.Uielements import Buttons, Edits, Groups, SideBar
from pyweixin.WeChatTools import Navigator, Tools, desktop

MOMENTS_TEXT = "你好六月"
FIND_TIMEOUT = 4.0
WECHAT_CLASS = "Qt51514QWindowIcon"


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


def _step(msg: str) -> None:
    _safe_print(f"[test_moments] {msg}")


def ensure_single_instance() -> None:
    if psutil is None:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "test_post_moments_once" not in cmd or proc.info["pid"] == me:
            continue
        _step(f"结束旧实例 PID={proc.info['pid']}")
        proc.kill()


def _focus_hwnd(hwnd: int) -> None:
    import win32con
    import win32gui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _center_main(main_window) -> None:
    import win32api
    import win32con
    import win32gui

    hwnd = main_window.handle
    _focus_hwnd(hwnd)
    sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    rect = main_window.rectangle()
    w, h = rect.width(), rect.height()
    if w > 0 and h > 0:
        win32gui.MoveWindow(hwnd, (sw - w) // 2, (sh - h) // 2, w, h, True)
    _focus_hwnd(hwnd)
    Tools.cancel_pin(main_window)


def _center_hwnd(hwnd: int) -> tuple[int, int, int, int]:
    import win32api
    import win32con
    import win32gui

    _focus_hwnd(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    if w > 0 and h > 0:
        win32gui.MoveWindow(hwnd, (sw - w) // 2, (sh - h) // 2, w, h, True)
        _focus_hwnd(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right, bottom


def _find_sns_hwnd(timeout: float = FIND_TIMEOUT) -> int:
    import win32gui

    deadline = time.time() + timeout
    while time.time() < deadline:
        matched: list[int] = []

        def enum_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            if title in ("朋友圈", "Moments") or cls == "mmui::SNSWindow":
                matched.append(hwnd)

        win32gui.EnumWindows(enum_cb, None)
        if matched:
            return matched[0]
        time.sleep(0.1)
    raise RuntimeError("未找到朋友圈窗口")


def _file_picker_open() -> bool:
    import win32gui

    matched: list[int] = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if cls != "#32770":
            return
        if any(k in title for k in ("打开", "Open", "选择", "文件", "Browse")):
            matched.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    return bool(matched)


def _close_file_picker_only() -> None:
    if _file_picker_open():
        _step("关闭误开的选图窗口")
        pyautogui.press("esc")
        time.sleep(0.15)


def _select_post_text_menu() -> None:
    """官方纯文字：up×2 → down×1 → enter。"""
    pyautogui.press("up", presses=2)
    time.sleep(0.08)
    pyautogui.press("down", presses=1)
    time.sleep(0.08)
    pyautogui.press("enter")
    time.sleep(0.3)
    if _file_picker_open():
        _close_file_picker_only()
        raise RuntimeError("误选了图片，已关闭选图窗口")


def _wait_publish_panel(moments, timeout: float = 2.0):
    publish_panel = moments.child_window(**Groups.SnsPublishGroup)
    if publish_panel.exists(timeout=timeout):
        return publish_panel
    return None


def _open_text_editor(moments) -> None:
    _close_file_picker_only()
    post_btn = moments.child_window(**Buttons.PostButton)
    if not post_btn.exists(timeout=1.0):
        raise RuntimeError("未找到朋友圈相机按钮")

    rect = post_btn.rectangle()
    _step(f"3/5 右键相机 ({rect.mid_point().x},{rect.mid_point().y})")
    post_btn.right_click_input()
    time.sleep(0.25)
    _step("3/5 键盘选「发表文字」")
    _select_post_text_menu()

    if _wait_publish_panel(moments, timeout=1.5):
        return

    _step("3/5 重试右键相机")
    post_btn.right_click_input()
    time.sleep(0.25)
    _select_post_text_menu()
    if not _wait_publish_panel(moments, timeout=2.0):
        raise RuntimeError("未打开纯文字编辑面板")


def _fill_and_post(moments, text: str) -> None:
    _step("4/5 填写文案")
    publish_panel = _wait_publish_panel(moments, timeout=2.0)
    if publish_panel is None:
        raise RuntimeError("编辑面板不存在")

    text_input = publish_panel.child_window(**Edits.SnsEdit)
    text_input.click_input()
    text_input.set_text(text)

    _step("5/5 点击发表")
    publish_panel.child_window(**Buttons.PostButton).click_input()
    time.sleep(0.4)


def post_text_moments(text: str) -> None:
    _step("1/5 居中微信")
    main_window = Navigator.open_weixin(is_maximize=False)
    _center_main(main_window)
    time.sleep(0.15)

    sns_hwnd = None
    try:
        sns_hwnd = _find_sns_hwnd(timeout=0.5)
    except RuntimeError:
        pass

    if not sns_hwnd:
        _step("2/5 打开朋友圈")
        main_window.child_window(**SideBar.Moments).click_input()
        sns_hwnd = _find_sns_hwnd()
    else:
        _step("2/5 朋友圈已打开")

    _step("2/5 居中朋友圈")
    rect = _center_hwnd(sns_hwnd)
    moments = desktop.window(handle=sns_hwnd)
    Tools.cancel_pin(moments)
    time.sleep(0.15)

    _open_text_editor(moments)
    _fill_and_post(moments, text)

    Tools.cancel_pin(main_window)
    Tools.cancel_pin(moments)


def main() -> None:
    ensure_single_instance()
    apply_pace(0.1)
    patch_wxid_folder_lookup()
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False

    _step("纯文字发帖，先居中微信再操作")
    _step("运行期间请勿操作鼠标键盘")
    t0 = time.time()
    try:
        post_text_moments(MOMENTS_TEXT)
    except Exception as e:
        _step(f"失败({time.time() - t0:.1f}s): {e}")
        sys.exit(1)
    _step(f"已发布「{MOMENTS_TEXT}」，退出({time.time() - t0:.1f}s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
