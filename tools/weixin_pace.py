"""
统一放慢 pyweixin / pyautogui 操作节奏，避免点击过快。

用法（任意脚本最前面）:
    from weixin_pace import apply_pace
    apply_pace(0.5)
"""
from __future__ import annotations

import time

import pyautogui

try:
    from pyweixin.Config import GlobalConfig
except ImportError:
    GlobalConfig = None  # type: ignore


def apply_pace(delay: float = 0.5) -> float:
    """设置全局操作间隔（秒）。"""
    if delay < 0:
        raise ValueError("delay 不能为负数")

    pyautogui.PAUSE = delay

    if GlobalConfig is not None:
        GlobalConfig.send_delay = float(delay)

    return delay


def patch_preserve_wechat_window() -> None:
    """禁止 pyweixin 把微信主窗口强行移到屏幕中央，尽量不打扰用户操作。"""
    try:
        import win32con
        import win32gui
        from pywinauto import Desktop

        from pyweixin.Errors import NetWorkError, NotFoundError, NotLoginError, NotStartError
        from pyweixin.Uielements import Buttons
        from pyweixin.WeChatTools import Navigator, Tools
    except ImportError:
        return

    desktop = Desktop(backend="uia")

    def _preserve_open_weixin(is_maximize=None, window_size=None):
        if is_maximize is None and GlobalConfig is not None:
            is_maximize = GlobalConfig.is_maximize
        if not Tools.is_weixin_running():
            raise NotStartError
        hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
        if hwnd == 0:
            hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "Weixin")
        main_window = desktop.window(handle=hwnd)
        if main_window.class_name() == "mmui::LoginWindow":
            raise NotLoginError
        if main_window.class_name() != "mmui::MainWindow":
            raise NotFoundError
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        offline_button = main_window.child_window(**Buttons.OffLineButton)
        Tools.cancel_pin(main_window)
        if offline_button.exists(timeout=0.1):
            main_window.close()
            raise NetWorkError("当前网络不可用,无法进行UI自动化!")
        return main_window

    def _noop_move_window_to_center(Window=None, Window_handle: int = 0):
        if Window_handle:
            return desktop.window(handle=Window_handle)
        return desktop.window(**Window)

    Navigator.open_weixin = staticmethod(_preserve_open_weixin)  # type: ignore[method-assign]
    Tools.move_window_to_center = staticmethod(_noop_move_window_to_center)  # type: ignore[method-assign]


def patch_activate_chatlist_no_mouse() -> None:
    """激活聊天列表时不移动鼠标，避免窗口偏移后点错位置。"""
    try:
        from pyweixin.WeChatTools import Tools
    except ImportError:
        return

    def _activate_chatList(chatList):
        try:
            chatList.set_focus()
        except Exception:
            pass
        chatList.type_keys("{END}")

    Tools.activate_chatList = staticmethod(_activate_chatList)  # type: ignore[method-assign]


def focus_wechat_window(main_window) -> None:
    """把微信带到前台，但不移动窗口位置。"""
    try:
        import win32con
        import win32gui
    except ImportError:
        return
    try:
        hwnd = main_window.handle
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def patch_wxid_folder_lookup() -> None:
    """绕过 psutil.memory_maps 权限问题，从 Documents/xwechat_files 定位 wxid。"""
    if GlobalConfig is None:
        return
    try:
        from pyweixin.WeChatTools import Tools
    except ImportError:
        return

    def _where_wxid_folder(open_folder: bool = False) -> str:
        import os

        base = os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "xwechat_files")
        if not os.path.isdir(base):
            return ""
        candidates = sorted(
            (
                os.path.join(base, name)
                for name in os.listdir(base)
                if name.startswith("wxid_") and os.path.isdir(os.path.join(base, name))
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        wxid_folder = candidates[0] if candidates else ""
        if wxid_folder and open_folder:
            os.startfile(wxid_folder)
        return wxid_folder

    Tools.where_wxid_folder = staticmethod(_where_wxid_folder)  # type: ignore[method-assign]


def paced_sleep(delay: float = 0.5) -> None:
    time.sleep(delay)


def wrap_callback_with_pace(callback, delay: float = 0.5):
    """给 AutoReply 回调前后各加一次等待。"""

    def wrapped(new_message: str, contexts: list[str]):
        time.sleep(delay)
        reply = callback(new_message, contexts)
        if reply:
            time.sleep(delay)
        return reply

    return wrapped
