"""
把本地已同步的数据打印到控制台，并写入 tools/data/sync_report.txt。

无需重新轮询微信，直接读本地 JSON/JSONL。

用法:
    python tools/print_sync_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from weixin_sync_lib import SyncStore, safe_print

DATA_DIR = TOOLS_DIR / "data"


def main() -> None:
    store = SyncStore(DATA_DIR)
    contacts = store.load_contacts()
    messages = store.load_all_messages()
    state = store.load_state()
    text = store.save_report(contacts, messages, state)
    safe_print(text)
    safe_print(f"[report] 已写入: {store.report_path}")


if __name__ == "__main__":
    main()
