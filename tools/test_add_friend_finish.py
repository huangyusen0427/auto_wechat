"""
仅完成最后一步：「申请添加朋友」弹窗点确定。

用法:
    python tools/test_add_friend_finish.py
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

import pyautogui

try:
    import psutil
except ImportError:
    psutil = None

pyautogui.FAILSAFE = False

GREETINGS = "你好，想加个好友"


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


def kill_other_test_scripts() -> None:
    if psutil is None:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "test_add_friend" not in cmd or proc.info["pid"] == me:
            continue
        _safe_print(f"[finish] 结束进程 PID={proc.info['pid']}")
        proc.kill()


def _find_verify_hwnd() -> int | None:
    import win32gui

    matched: list[int] = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if cls == "mmui::VerifyFriendWindow" or title == "申请添加朋友":
            matched.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    return matched[0] if matched else None


def _paste_text(text: str) -> None:
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
    pyautogui.hotkey("ctrl", "a")
    pyautogui.hotkey("ctrl", "v")


def finish_add_friend(greetings: str) -> None:
    import win32gui

    hwnd = _find_verify_hwnd()
    if not hwnd:
        raise RuntimeError("未找到「申请添加朋友」窗口")

    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    _safe_print(f"[finish] 窗口位置 ({left},{top}) {w}x{h}")

    if greetings:
        edit_x = left + w // 2
        edit_y = top + int(h * 0.22)
        pyautogui.click(edit_x, edit_y)
        time.sleep(0.1)
        _paste_text(greetings)
        _safe_print(f"[finish] 招呼语: {greetings}")

    confirm_x = left + w // 4
    confirm_y = bottom - 28
    _safe_print(f"[finish] 点击确定 ({confirm_x},{confirm_y})")
    pyautogui.click(confirm_x, confirm_y)
    time.sleep(0.2)


def main() -> None:
    kill_other_test_scripts()
    _safe_print("[finish] 已清理其他进程，执行最后一步")
    try:
        finish_add_friend(GREETINGS)
    except Exception as e:
        _safe_print(f"[finish] 失败: {e}")
        sys.exit(1)
    _safe_print("[finish] 已发送，退出")
    sys.exit(0)


if __name__ == "__main__":
    main()
