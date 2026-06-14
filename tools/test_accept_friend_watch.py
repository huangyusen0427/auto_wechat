"""
监测「新的朋友」，检测到一条待验证好友申请后自动通过一条，然后退出。

复用项目内微信居中 + weixin_pace 配置；验证弹窗用窗口标题 + 坐标点击确定。

官方频率说明（被动通过验证）：
    - 单次最多通过 8 人（本脚本固定 limit=1，只通过 1 条）
    - 每日建议不超过 4 次
    - 两次操作间隔建议 ≥ 2 小时，频繁操作有封号风险

主动添加好友（FriendSettings.add_new_friend）另有限制：不建议频繁使用。

用法:
    python tools/uia_keeper.py --bg
    python tools/test_accept_friend_watch.py
    python tools/test_accept_friend_watch.py --peek          # 只查看，不通过
    python tools/test_accept_friend_watch.py --timeout 600   # 最多等 10 分钟
"""
from __future__ import annotations

import argparse
import os
import random
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

from pyweixin.Config import GlobalConfig
from pyweixin.Uielements import Buttons, Customs, ListItems, SideBar
from pyweixin.WeChatTools import Navigator, Tools

pyautogui.FAILSAFE = False

POLL_MIN = 5.0
POLL_MAX = 8.0
ACCEPT_LIMIT = 1
WECHAT_DIALOG_CLASS = "Qt51514QWindowIcon"
VERIFY_DIALOG_TITLES = {
    "通过朋友验证",
    "Confirm Friend Request",
    "通過朋友驗證",
}
PENDING_MARKERS = ("等待验证", "Waiting for verification", "等待驗證")
SECTION_HEADERS = (
    "新的朋友",
    "公众号",
    "服务号",
    "企业微信",
    "联系人",
    "群聊",
    "通讯录管理",
    "New Friends",
    "Official Accounts",
    "Service Accounts",
    "Contacts",
)


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


def _step(msg: str) -> None:
    _safe_print(f"[accept_friend] {msg}")


def ensure_single_instance() -> None:
    if psutil is None:
        return
    me = os.getpid()
    script = Path(__file__).name
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if script not in cmd or proc.info["pid"] == me:
            continue
        _step(f"结束旧实例 PID={proc.info['pid']}")
        proc.kill()


def ensure_wechat_centered():
    """打开微信并居中（与 auto_reply / sync 一致）。"""
    main_window = Navigator.open_weixin(is_maximize=False)
    Tools.move_window_to_center(Window_handle=main_window.handle)
    Tools.cancel_pin(main_window)
    return main_window


def _back_to_chat(main_window) -> None:
    try:
        chat_button = main_window.child_window(**SideBar.Weixin)
        if chat_button.exists(timeout=0.2):
            chat_button.click_input()
    except Exception:
        pass


def _open_contacts_centered():
    ensure_wechat_centered()
    contact_list, main_window = Navigator.open_contacts(is_maximize=False)
    Tools.move_window_to_center(Window_handle=main_window.handle)
    Tools.cancel_pin(main_window)
    contact_list.type_keys("{HOME}")
    time.sleep(0.2)
    return contact_list, main_window


def _row_text(item) -> str:
    try:
        return (item.window_text() or "").strip()
    except Exception:
        return ""


def _robust_click(ctrl) -> None:
    try:
        ctrl.set_focus()
    except Exception:
        pass
    try:
        ctrl.click_input()
        return
    except Exception:
        pass
    rect = ctrl.rectangle()
    pyautogui.click(rect.mid_point().x, rect.mid_point().y)


def _find_pending_rows(contact_list) -> list:
    """扫描通讯录列表中带「等待验证」的待处理行（含展开后的子项）。"""
    pending: list = []
    for item in contact_list.children(control_type="ListItem"):
        text = _row_text(item)
        if not text:
            continue
        if any(m in text for m in PENDING_MARKERS):
            pending.append(item)
            continue
        if item.class_name() == "mmui::ContactsCellGroupView":
            continue
        if item.class_name() == "mmui::XTableCell":
            first_line = text.split("\n", 1)[0]
            if first_line and not any(h in text for h in SECTION_HEADERS):
                pending.append(item)
    return pending


def _ensure_new_friends_expanded(main_window, contact_list) -> bool:
    newfriend_item = main_window.child_window(**ListItems.NewFriendListItem)
    if not newfriend_item.exists(timeout=0.5):
        return False
    if _find_pending_rows(contact_list):
        return True
    _step("展开「新的朋友」")
    _robust_click(newfriend_item)
    time.sleep(0.35)
    return True


def peek_pending_friend_request() -> bool:
    """检查通讯录里是否有待验证好友（扫描「等待验证」行）。"""
    contact_list, main_window = _open_contacts_centered()
    try:
        if not _ensure_new_friends_expanded(main_window, contact_list):
            return False
        pending = _find_pending_rows(contact_list)
        if pending:
            _step(f"发现待验证: {_row_text(pending[0]).splitlines()[0]}")
            return True
        return False
    finally:
        _back_to_chat(main_window)


def _focus_hwnd(hwnd: int) -> None:
    import win32con
    import win32gui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _find_verify_accept_hwnd() -> int | None:
    """弹窗 UI 树常被藏，用窗口标题 + Qt 类名定位（非 mmui::VerifyFriendWindow）。"""
    import win32gui

    matched: list[int] = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if title in VERIFY_DIALOG_TITLES and cls in {
            WECHAT_DIALOG_CLASS,
            "mmui::VerifyFriendWindow",
        }:
            matched.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    return matched[0] if matched else None


def _wait_verify_dialog_hwnd(timeout: float = 12.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_verify_accept_hwnd()
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return None


def _dialog_screen_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区转屏幕坐标，避免边框干扰。"""
    import win32gui

    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    x1, y1 = win32gui.ClientToScreen(hwnd, (cl, ct))
    x2, y2 = win32gui.ClientToScreen(hwnd, (cr, cb))
    return x1, y1, x2, y2


def _click_confirm_by_coords(hwnd: int) -> bool:
    """精准坐标点绿色「确定」（弹窗 UI 树不可见）。"""
    _focus_hwnd(hwnd)
    time.sleep(0.3)
    left, top, right, bottom = _dialog_screen_rect(hwnd)
    w, h = right - left, bottom - top
    _step(f"验证弹窗客户区 ({left},{top}) {w}x{h}")

    # 绿色「确定」在弹窗左下方，用比例坐标适配不同 DPI
    points = (
        (left + int(w * 0.26), top + int(h * 0.935)),
        (left + int(w * 0.30), top + int(h * 0.945)),
        (left + w // 4, bottom - 28),
        (left + int(w * 0.24), bottom - 22),
    )
    for confirm_x, confirm_y in points:
        _step(f"坐标点击确定 ({confirm_x},{confirm_y})")
        _focus_hwnd(hwnd)
        pyautogui.click(confirm_x, confirm_y)
        time.sleep(0.55)
        if not _find_verify_accept_hwnd():
            return True

    _focus_hwnd(hwnd)
    pyautogui.press("enter")
    time.sleep(0.45)
    return not _find_verify_accept_hwnd()


def _confirm_verify_dialog(main_window=None) -> bool:
    hwnd = _wait_verify_dialog_hwnd(timeout=12.0)
    if not hwnd:
        _step("未等到「通过朋友验证」弹窗")
        return False
    if _click_confirm_by_coords(hwnd):
        _step("已点确定，通过完成")
        return True
    _step("坐标点击后弹窗仍在")
    return False


def accept_one_friend_request() -> list[str]:
    """点击待验证行 → 前往验证 → 确定，只通过 1 条。"""
    contact_list, main_window = _open_contacts_centered()
    try:
        if not _ensure_new_friends_expanded(main_window, contact_list):
            return []

        pending = _find_pending_rows(contact_list)
        if not pending:
            return []

        target = pending[0]
        label = _row_text(target).splitlines()[0] or "新朋友"
        _step(f"点击待验证行: {label}")
        _robust_click(target)
        time.sleep(0.5)

        contact_custom = main_window.child_window(**Customs.ContactDetailCustom)
        verify_button = contact_custom.child_window(**Buttons.VerifyNowButton)
        if not verify_button.exists(timeout=1.5):
            _step("右侧未出现「前往验证」")
            return []

        _step("点击「前往验证」")
        _robust_click(verify_button)
        time.sleep(0.8)

        if _confirm_verify_dialog(main_window):
            return [label]

        _step("确定未生效，请检查弹窗")
        return []
    finally:
        _back_to_chat(main_window)


def watch_and_accept(*, peek_only: bool, timeout: float | None) -> int:
    _step("官方限制: 单次≤8人, 每日≤4次, 间隔≥2h（本脚本只通过 1 条）")
    _step(f"轮询间隔: {POLL_MIN}~{POLL_MAX}s | 通过上限: {ACCEPT_LIMIT}")
    if peek_only:
        _step("模式: 仅查看（--peek）")
    if timeout:
        _step(f"超时: {timeout:.0f}s")

    deadline = time.time() + timeout if timeout else None
    round_no = 0

    while True:
        round_no += 1
        if deadline and time.time() >= deadline:
            _step("超时，未检测到待验证好友申请")
            return 1

        _step(f"第 {round_no} 轮监测…")
        try:
            pending = peek_pending_friend_request()
        except Exception as e:
            _step(f"监测异常: {e}")
            time.sleep(POLL_MIN)
            continue

        if not pending:
            wait = random.uniform(POLL_MIN, POLL_MAX)
            _step(f"暂无待验证，{wait:.1f}s 后再查")
            time.sleep(wait)
            continue

        _step("检测到待验证好友申请")
        if peek_only:
            _step("--peek 模式，不执行通过，退出")
            return 0

        try:
            accepted = accept_one_friend_request()
        except Exception as e:
            _step(f"通过失败: {e}")
            return 1

        if accepted:
            _step(f"已通过 1 条: {accepted[0]}")
            if len(accepted) > 1:
                _step(f"（共返回 {len(accepted)} 条信息，仅处理了 1 条）")
        else:
            _step("未返回通过记录（可能已被处理或 UI 未就绪）")
            return 1

        _step("完成，退出")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="监测并通过一条好友验证")
    parser.add_argument(
        "--peek",
        action="store_true",
        help="只监测是否有待验证，不点击通过",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0,
        metavar="SEC",
        help="最长等待秒数，0 表示一直等（默认 0）",
    )
    args = parser.parse_args()

    ensure_single_instance()
    apply_pace(0.15)
    patch_wxid_folder_lookup()
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 0

    timeout = args.timeout if args.timeout > 0 else None
    code = watch_and_accept(peek_only=args.peek, timeout=timeout)
    sys.exit(code)


if __name__ == "__main__":
    main()
