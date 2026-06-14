"""
一次性测试：用手机号添加好友，发送完立即退出。

走主界面搜索 ->「网络查找手机/QQ号」路径，不经过快捷操作菜单。

用法:
    python tools/test_add_friend_once.py
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

from weixin_pace import apply_pace, patch_wxid_folder_lookup

from pywinauto import Desktop

from pyweixin.Config import GlobalConfig
from pyweixin.Uielements import Buttons, Lists, Main_window, SideBar
from pyweixin.WeChatTools import Navigator, Tools

desktop = Desktop(backend="uia")

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

PHONE = "13242851449"
GREETINGS = "你好，想加个好友"
WAIT_STEP = 3.0

ADD_FRIEND_TITLE = "添加朋友"
VERIFY_TITLE = "申请添加朋友"
WECHAT_CLASS = "Qt51514QWindowIcon"


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


def _step(msg: str) -> None:
    _safe_print(f"[test_add_friend] {msg}")


def _find_hwnd(title: str) -> int:
    import win32gui

    hwnd = win32gui.FindWindow(WECHAT_CLASS, title)
    if hwnd and win32gui.IsWindowVisible(hwnd):
        return hwnd
    return 0


def _wait_hwnd(title: str, timeout: float = WAIT_STEP) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_hwnd(title)
        if hwnd:
            return hwnd
        time.sleep(0.12)
    raise RuntimeError(f"超时未找到窗口: {title!r}")


def _focus_hwnd(hwnd: int) -> None:
    import win32con
    import win32gui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    import win32gui

    return win32gui.GetWindowRect(hwnd)


def ensure_single_instance() -> None:
    if psutil is None:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "test_add_friend" not in cmd or proc.info["pid"] == me:
            continue
        _step(f"结束旧实例 PID={proc.info['pid']}")
        proc.kill()


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


def _click_xy(x: int, y: int) -> None:
    pyautogui.click(x, y)


def _click_ctrl(ctrl) -> None:
    rect = ctrl.rectangle()
    _click_xy(rect.mid_point().x, rect.mid_point().y)


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


def _input_text(ctrl, text: str) -> None:
    try:
        ctrl.set_text("")
        ctrl.set_text(text)
        return
    except Exception:
        pass
    try:
        _paste_text(text)
    except Exception:
        if text.isdigit():
            pyautogui.typewrite(text, interval=0.03)
        else:
            ctrl.type_keys(text, with_spaces=True)


def _open_add_friend_by_main_search(main_window, phone: str) -> int:
    existing = _find_hwnd(ADD_FRIEND_TITLE)
    if existing:
        _step(f"复用已打开的添加朋友窗口 hwnd={existing}")
        return existing

    _step("主界面搜索手机号")
    tab = main_window.child_window(**SideBar.Weixin)
    if tab.exists(timeout=0.5):
        _click_ctrl(tab)
        time.sleep(0.08)

    search_edits = main_window.descendants(**Main_window.Search)
    if not search_edits:
        raise RuntimeError("未找到主界面搜索框")
    search_edit = search_edits[1] if len(search_edits) == 2 else search_edits[0]
    _click_ctrl(search_edit)
    time.sleep(0.08)
    _input_text(search_edit, phone)
    time.sleep(0.5)

    search_results = main_window.child_window(**Lists.SearchResult)
    if not search_results.exists(timeout=1):
        raise RuntimeError("搜索结果列表未出现")

    mobile_item = None
    for item in search_results.children(control_type="ListItem"):
        text = (item.window_text() or "").strip()
        if "网络查找" in text or "手机" in text or "QQ" in text:
            mobile_item = item
            break
    if mobile_item is None:
        raise RuntimeError("未出现「网络查找手机/QQ号」选项")

    _step("点击网络查找")
    _click_ctrl(mobile_item)
    hwnd = _wait_hwnd(ADD_FRIEND_TITLE, timeout=WAIT_STEP)
    _step(f"添加朋友窗口 hwnd={hwnd}")
    return hwnd


def _click_add_to_contacts(add_hwnd: int) -> None:
    """用 hwnd 绑定按钮坐标点击，避免慢速 exists/descendants。"""
    _focus_hwnd(add_hwnd)
    win = desktop.window(handle=add_hwnd)
    btn = win.child_window(**Buttons.AddToContactsButton)
    if btn.exists(timeout=0.8):
        rect = btn.rectangle()
        x, y = rect.mid_point().x, rect.mid_point().y
        _step(f"2/4 点击添加到通讯录 ({x},{y})")
        _click_xy(x, y)
    else:
        left, top, right, bottom = _rect(add_hwnd)
        w, h = right - left, bottom - top
        x, y = left + w // 2, top + int(h * 0.54)
        _step(f"2/4 坐标回退点击 ({x},{y})")
        _click_xy(x, y)

    deadline = time.time() + WAIT_STEP
    while time.time() < deadline:
        if _find_hwnd(VERIFY_TITLE):
            return
        time.sleep(0.12)
    raise RuntimeError("点击后未弹出「申请添加朋友」")


def _finish_verify(greetings: str | None) -> None:
    hwnd = _wait_hwnd(VERIFY_TITLE, timeout=WAIT_STEP)
    _focus_hwnd(hwnd)
    win = desktop.window(handle=hwnd)
    left, top, right, bottom = _rect(hwnd)
    w, h = right - left, bottom - top
    _step(f"3/4 申请窗口 ({left},{top}) {w}x{h}")

    if greetings:
        req = win.child_window(control_type="Edit", found_index=0)
        if req.exists(timeout=0.5):
            rect = req.rectangle()
            _click_xy(rect.mid_point().x, rect.mid_point().y)
        else:
            _click_xy(left + w // 2, top + int(h * 0.22))
        time.sleep(0.08)
        _paste_text(greetings)
        _step(f"招呼语: {greetings}")

    confirm = win.child_window(**Buttons.ConfirmButton)
    if confirm.exists(timeout=0.8):
        rect = confirm.rectangle()
        x, y = rect.mid_point().x, rect.mid_point().y
    else:
        x = left + int(w * 0.25)
        y = bottom - 36

    _focus_hwnd(hwnd)
    time.sleep(0.1)
    _step(f"4/4 点击确定 ({x},{y})")
    _click_xy(x, y)
    time.sleep(0.15)
    _click_xy(x, y)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _find_hwnd(VERIFY_TITLE):
            return
        time.sleep(0.15)

    _focus_hwnd(hwnd)
    pyautogui.press("enter")
    time.sleep(0.5)
    if _find_hwnd(VERIFY_TITLE):
        raise RuntimeError("确定未生效，申请窗口仍在")


def add_friend_by_phone(phone: str, greetings: str | None) -> None:
    _step("1/4 居中微信")
    main_window = Navigator.open_weixin(is_maximize=False)
    _center_main(main_window)

    add_hwnd = _open_add_friend_by_main_search(main_window, phone)

    if not _find_hwnd(VERIFY_TITLE):
        _click_add_to_contacts(add_hwnd)
        import win32gui

        try:
            win32gui.PostMessage(add_hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass
    else:
        _step("申请窗口已打开，跳过点击添加到通讯录")

    _finish_verify(greetings)


def main() -> None:
    ensure_single_instance()
    apply_pace(0.05)
    patch_wxid_folder_lookup()
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False

    _step(f"手机号: {PHONE}")
    t0 = time.time()
    try:
        add_friend_by_phone(PHONE, GREETINGS)
    except Exception as e:
        _step(f"失败({time.time() - t0:.1f}s): {e}")
        sys.exit(1)
    _step("已发送，退出")
    os._exit(0)


if __name__ == "__main__":
    main()
