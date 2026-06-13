"""
UIA 守护进程：模拟无障碍客户端常驻，防止微信登录后封 UI 树。

原理（见 GitHub pywechat#110）：
  微信 4.x 会定时检测是否有 UIA 客户端（讲述人/NVDA/自建客户端）在运行；
  无客户端时收缩 UI 树，有客户端时保持 mmui:: 结构暴露。

用法：
  python tools/uia_keeper.py          # 前台运行（看日志）
  python tools/uia_keeper.py --bg     # 后台运行

配合 pyweixin 使用前，先启动本脚本，再开/登录微信。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import winreg

import comtypes.client
import psutil
import win32gui

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen.UIAutomationClient import CUIAutomation  # noqa: E402


def find_wechat_hwnd() -> int:
    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
    if hwnd == 0:
        hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "Weixin")
    return hwnd


def get_class_name_pywinauto(hwnd: int) -> str:
    import pythoncom
    from pywinauto import Desktop

    pythoncom.CoInitialize()
    return Desktop(backend="uia").window(handle=hwnd).class_name()


def poll_uia_tree(automation) -> tuple[str, int]:
    hwnd = find_wechat_hwnd()
    if not hwnd:
        return "not_running", 0
    elem = automation.ElementFromHandle(hwnd)
    cls = elem.CurrentClassName or ""
    walker = automation.ControlViewWalker
    child = walker.GetFirstChildElement(elem)
    count = 0
    while child:
        count += 1
        # 深入遍历，让微信感知到有 UIA 客户端在活跃读取
        sub = walker.GetFirstChildElement(child)
        while sub:
            _ = sub.CurrentClassName
            sub = walker.GetNextSiblingElement(sub)
        child = walker.GetNextSiblingElement(child)
    return cls, count


def ensure_narrator_registry() -> None:
    """保持讲述人注册表 RunningState，部分环境可辅助持久解锁。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Narrator\NoRoam",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "RunningState", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except OSError:
        pass


def ensure_narrator_process() -> None:
    for proc in psutil.process_iter(["name"]):
        if "narrator" in (proc.info["name"] or "").lower():
            return
    subprocess.Popen(
        [r"C:\Windows\System32\Narrator.exe"],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def run_keeper(interval: float = 0.8, use_narrator: bool = True) -> None:
    automation = comtypes.client.CreateObject(CUIAutomation)
    print("[uia_keeper] 已启动，按 Ctrl+C 停止")
    print("[uia_keeper] 请在此脚本运行期间启动/登录微信")
    last_cls = ""
    while True:
        if use_narrator:
            ensure_narrator_registry()
            ensure_narrator_process()
        cls, count = poll_uia_tree(automation)
        pwa_cls = ""
        hwnd = find_wechat_hwnd()
        if hwnd:
            try:
                pwa_cls = get_class_name_pywinauto(hwnd)
            except Exception:
                pwa_cls = "error"
        if cls != last_cls:
            print(f"[uia_keeper] UIA={cls!r} children={count} | pywinauto={pwa_cls!r}")
            last_cls = cls
        if cls == "mmui::MainWindow":
            print("[uia_keeper] [OK] 主界面已解锁，可运行 pyweixin")
        elif cls == "mmui::LoginWindow":
            print("[uia_keeper] [WAIT] 登录界面已解锁，请完成登录")
        elif cls == "not_running":
            pass
        elif "mmui" not in cls:
            print("[uia_keeper] [WARN] UI 被封，持续轮询中...")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="微信 UIA 守护进程")
    parser.add_argument("--bg", action="store_true", help="后台运行（仅 Windows）")
    parser.add_argument("--no-narrator", action="store_true", help="不启动讲述人，仅 UIA 轮询")
    parser.add_argument("--interval", type=float, default=0.8, help="轮询间隔秒")
    args = parser.parse_args()
    if args.bg:
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--interval",
                str(args.interval),
            ]
            + (["--no-narrator"] if args.no_narrator else []),
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=str(__file__).rsplit("\\", 2)[0] if "\\" in __file__ else ".",
        )
        print("[uia_keeper] 已在后台启动")
        return
    run_keeper(interval=args.interval, use_narrator=not args.no_narrator)


if __name__ == "__main__":
    main()
