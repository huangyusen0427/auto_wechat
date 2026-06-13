"""停止自动回复与 uia_keeper 守护进程。"""
from __future__ import annotations

import sys

try:
    import psutil
except ImportError:
    print("需要 psutil: pip install psutil")
    sys.exit(1)

TARGETS = ("auto_reply_11", "uia_keeper")
killed = 0

for proc in psutil.process_iter(["pid", "name", "cmdline"]):
    name = (proc.info.get("name") or "").lower()
    if name != "python.exe":
        continue
    cmdline = " ".join(proc.info.get("cmdline") or [])
    if not any(t in cmdline for t in TARGETS):
        continue
    if proc.pid == psutil.Process().pid:
        continue
    print(f"结束 PID {proc.pid}: {cmdline[:100]}")
    try:
        proc.kill()
        killed += 1
    except psutil.Error as e:
        print(f"  失败: {e}")

print(f"已结束 {killed} 个进程")
