"""
微信联系人/消息同步库：微信号采集、消息区分、JSON 存储、Webhook 推送。

供 sync_poll_service.py 与 auto_reply_11.py 复用。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyweixin.Config import GlobalConfig
from pyweixin.Uielements import Buttons, Edits, Lists, Main_window, Windows
from pyweixin.WeChatTools import Navigator, Tools, desktop
from pyweixin.utils import scan_for_new_messages

try:
    from pyweixin import Messages as WxMessages
except ImportError:
    WxMessages = None

SKIP_SCAN_NAMES = {
    "折叠的聊天",
    "折叠的群聊",
    "Folded Chats",
    "Minimized Chats",
    "折疊的聊天",
    "公众号",
    "服务号",
    "Service Accounts",
    "Official Accounts",
    "服務賬號",
    "官方賬號",
    "微信团队",
    "Weixin Team",
    "文件传输助手",
    "File Transfer",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def parse_friend_list(cfg: dict[str, str]) -> list[str]:
    raw = cfg.get("TARGET_FRIENDS") or cfg.get("TARGET_FRIEND") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def parse_bool(cfg: dict[str, str], key: str, default: str = "1") -> bool:
    return (cfg.get(key) or default).strip().lower() in {"1", "true", "yes", "on"}


def is_pollable_friend(name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    if name in SKIP_SCAN_NAMES:
        return False
    if "折叠" in name or "Folded" in name or "Minimized" in name:
        return False
    return True


def ensure_wechat_centered():
    main_window = Navigator.open_weixin(is_maximize=False)
    Tools.move_window_to_center(Window_handle=main_window.handle)
    Tools.cancel_pin(main_window)
    return main_window


def open_friend_chat(friend: str):
    main_window = Navigator.open_dialog_window(
        friend=friend,
        is_maximize=False,
        search_pages=0,
    )
    Tools.move_window_to_center(Window_handle=main_window.handle)
    return main_window


class FriendWxidCache:
    """备注名 -> 微信号缓存。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): str(v) for k, v in raw.items()}
        except Exception as e:
            safe_print(f"[sync] 读取微信号缓存失败: {e}")

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            safe_print(f"[sync] 保存微信号缓存失败: {e}")

    def get(self, friend: str) -> str | None:
        wxid = (self._data.get(friend) or "").strip()
        if wxid and wxid not in {"无", "未知"}:
            return wxid
        return None

    def set(self, friend: str, wxid: str) -> None:
        wxid = (wxid or "").strip()
        if wxid and wxid not in {"无", "未知"}:
            self._data[friend] = wxid
            self.save()

    def fetch_and_cache(self, friend: str, main_window=None) -> str | None:
        cached = self.get(friend)
        if cached:
            return cached
        wx_number = self._fetch_wxid_via_profile(friend)
        if wx_number:
            self.set(friend, wx_number)
            safe_print(f"[sync] {friend}({wx_number}) 微信号已缓存")
            return wx_number
        if main_window is not None:
            wx_number = self._read_wxid_from_chatinfo(main_window, friend)
            if wx_number:
                self.set(friend, wx_number)
                safe_print(f"[sync] {friend}({wx_number}) 侧栏微信号已缓存")
                return wx_number
        return None

    def _bring_profile_front(self, profile_pane) -> None:
        """资料卡常被右侧聊天信息挡住，移到屏幕偏左并置顶。"""
        try:
            import win32api
            import win32con
            import win32gui

            hwnd = profile_pane.handle
            rect = profile_pane.rectangle()
            w, h = rect.width(), rect.height()
            sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            x = max(20, (sw - w) // 2 - 160)
            y = max(20, (sh - h) // 2)
            win32gui.MoveWindow(hwnd, x, y, w, h, True)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            Tools.cancel_pin(profile_pane)
        except Exception:
            pass

    def _close_chatinfo(self, main_window) -> None:
        try:
            pane = main_window.child_window(
                auto_id="single_chat_info_view", control_type="Group"
            )
            if pane.exists(timeout=0.15):
                btn = main_window.child_window(**Buttons.ChatInfoButton)
                if btn.exists(timeout=0.15):
                    btn.click_input()
                    time.sleep(0.15)
        except Exception:
            pass

    def _fetch_wxid_via_profile(self, friend: str) -> str | None:
        """官方路径：聊天信息 -> 点头像 -> 资料卡读微信号。"""
        wxnum_label = "微信号："
        main_window = None
        profile_pane = None
        try:
            chatinfo_pane, main_window = Navigator.open_chatinfo(
                friend=friend,
                is_maximize=False,
                search_pages=0,
            )
            Tools.move_window_to_center(Window_handle=main_window.handle)
            Tools.cancel_pin(main_window)
            time.sleep(0.2)

            friend_button = chatinfo_pane.child_window(
                title=friend, control_type="Button"
            )
            if not friend_button.exists(timeout=0.8):
                buttons = [
                    b
                    for b in chatinfo_pane.children(control_type="Button")
                    if b.window_text() == friend
                ]
                friend_button = buttons[0] if buttons else None
            if friend_button is None or not friend_button.exists(timeout=0.2):
                safe_print(f"[sync] {friend} 聊天信息内未找到头像按钮")
                return None

            rect = friend_button.rectangle()
            safe_print(
                f"[sync] {friend} 点击资料卡 ({rect.mid_point().x},{rect.mid_point().y})"
            )
            import pyautogui

            pyautogui.click(rect.mid_point().x, rect.mid_point().y)
            time.sleep(0.45)

            profile_pane = desktop.window(**Windows.PopUpProfileWindow)
            if not profile_pane.exists(timeout=1.5):
                safe_print(f"[sync] {friend} 资料卡未弹出")
                return None

            self._bring_profile_front(profile_pane)
            time.sleep(0.25)
            texts = [
                item.window_text()
                for item in profile_pane.descendants(control_type="Text")
            ]
            wx_number = None
            if wxnum_label in texts:
                wx_number = texts[texts.index(wxnum_label) + 1].strip()
            safe_print(
                f"[sync] {friend} 资料卡字段: 微信号={wx_number or '(无)'}"
            )

            try:
                profile_pane.close()
            except Exception:
                import pyautogui

                pyautogui.press("esc")
            time.sleep(0.15)
            self._close_chatinfo(main_window)

            if wx_number and wx_number not in {"无", "未知"}:
                return wx_number
        except Exception as e:
            safe_print(f"[sync] {friend} 资料卡读微信号失败: {e}")
        finally:
            if profile_pane is not None:
                try:
                    if profile_pane.exists(timeout=0.1):
                        profile_pane.close()
                except Exception:
                    pass
            if main_window is not None:
                self._close_chatinfo(main_window)
        return None

    def _read_wxid_from_chatinfo(self, main_window, friend: str) -> str | None:
        wxnum_label = "微信号："
        try:
            if Tools.is_group_chat(main_window):
                return None
            chatinfo_button = main_window.child_window(**Buttons.ChatInfoButton)
            if not chatinfo_button.exists(timeout=0.2):
                return None
            chatinfo_button.click_input()
            time.sleep(0.35)
            pane = main_window.child_window(
                auto_id="single_chat_info_view", control_type="Group"
            )
            if not pane.exists(timeout=0.5):
                chatinfo_button.click_input()
                return None
            texts = [item.window_text() for item in pane.descendants(control_type="Text")]
            if wxnum_label in texts:
                wx_number = texts[texts.index(wxnum_label) + 1].strip()
                if wx_number and wx_number != "无":
                    chatinfo_button.click_input()
                    return wx_number
            chatinfo_button.click_input()
        except Exception as e:
            safe_print(f"[sync] {friend} 侧栏读微信号失败: {e}")
        return None


class AiOutboundLog:
    """记录 AI 自动回复，用于区分 friend / ai / self。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[dict[str, str]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    self._entries.append(item)
            except json.JSONDecodeError:
                continue

    def record(self, remark: str, content: str) -> None:
        entry = {
            "remark": remark,
            "content": content.strip(),
            "sent_at": utc_now(),
        }
        self._entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def is_ai_reply(self, remark: str, content: str) -> bool:
        content = (content or "").strip()
        if not content:
            return False
        for item in reversed(self._entries[-200:]):
            if item.get("remark") == remark and item.get("content") == content:
                return True
        return False


def record_ai_outbound(remark: str, content: str, data_dir: Path | None = None) -> None:
    root = data_dir or Path(__file__).resolve().parent / "data"
    AiOutboundLog(root / "ai_outbound_log.jsonl").record(remark, content)


class SyncStore:
    """本地 JSON / JSONL 存储，便于业务系统对接。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.contacts_path = data_dir / "sync_contacts.json"
        self.messages_path = data_dir / "sync_messages.jsonl"
        self.state_path = data_dir / "sync_state.json"
        self.export_path = data_dir / "sync_export_latest.json"
        self.report_path = data_dir / "sync_report.txt"

    def load_all_messages(self) -> list[dict[str, Any]]:
        return self.load_recent_messages(limit=100000)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "extended_whitelist": [],
                "message_cursors": {},
                "seen_message_ids": [],
                "last_sync_at": None,
            }
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "extended_whitelist": [],
                "message_cursors": {},
                "seen_message_ids": [],
                "last_sync_at": None,
            }

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_contacts(self, contacts: list[dict[str, Any]]) -> None:
        payload = {"version": 1, "updated_at": utc_now(), "contacts": contacts}
        self.contacts_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_messages(self, messages: list[dict[str, Any]]) -> int:
        if not messages:
            return 0
        with self.messages_path.open("a", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return len(messages)

    def save_export_snapshot(
        self,
        contacts: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "version": 1,
            "device_id": socket.gethostname(),
            "exported_at": utc_now(),
            "contacts": contacts,
            "messages": messages,
        }
        self.export_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    def load_contacts(self) -> list[dict[str, Any]]:
        if not self.contacts_path.exists():
            return []
        try:
            raw = json.loads(self.contacts_path.read_text(encoding="utf-8"))
            return list(raw.get("contacts") or [])
        except Exception:
            return []

    def load_recent_messages(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.messages_path.exists():
            return []
        lines = self.messages_path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def save_report(
        self,
        contacts: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
    ) -> str:
        text = build_report_text(contacts, messages, state or self.load_state())
        self.report_path.write_text(text, encoding="utf-8")
        return text


ROLE_LABEL = {"friend": "对方", "ai": "AI", "self": "自己"}


def build_report_text(
    contacts: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    state: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("=" * 50)
    lines.append("微信同步报告")
    lines.append("=" * 50)
    lines.append(f"导出时间: {utc_now()}")
    lines.append(f"设备: {socket.gethostname()}")
    lines.append(f"上次同步: {state.get('last_sync_at') or '无'}")
    lines.append("")

    lines.append("【联系人 / 微信号】")
    if not contacts:
        lines.append("  (无)")
    for i, c in enumerate(contacts, 1):
        wxid = c.get("wxid")
        wxid_show = wxid if wxid else "(未获取)"
        lines.append(f"  {i}. 备注: {c.get('remark')}")
        lines.append(f"     微信号: {wxid_show}")
        lines.append(f"     状态: {c.get('wxid_status', 'unknown')}")
        lines.append(
            f"     白名单: {'是' if c.get('in_whitelist') else '否'}"
            f" ({c.get('whitelist_source', '-')})"
        )
    lines.append("")

    wxid_ok = sum(1 for c in contacts if c.get("wxid"))
    wxid_pending = len(contacts) - wxid_ok
    lines.append(
        f"统计: 联系人 {len(contacts)} | 微信号已获取 {wxid_ok} | 待获取 {wxid_pending}"
    )
    lines.append("")

    lines.append("【聊天消息】")
    if not messages:
        lines.append("  (无)")
    else:
        by_remark: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            by_remark.setdefault(str(msg.get("remark") or ""), []).append(msg)
        for remark in sorted(by_remark):
            wxid = next(
                (c.get("wxid") for c in contacts if c.get("remark") == remark),
                None,
            )
            wxid_show = wxid if wxid else "未获取"
            lines.append(f"--- {remark} (微信号: {wxid_show}) ---")
            for msg in by_remark[remark]:
                role = ROLE_LABEL.get(str(msg.get("role")), str(msg.get("role")))
                content = str(msg.get("content") or "")
                captured = msg.get("captured_at") or ""
                lines.append(f"  [{role}] {content}")
                if captured:
                    lines.append(f"           时间: {captured}")
            lines.append("")

    lines.append(f"消息总数: {len(messages)}")
    lines.append("=" * 50)
    return "\n".join(lines) + "\n"


def message_id(remark: str, runtime_id: Any, content: str) -> str:
    base = f"{remark}|{runtime_id}|{content}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def classify_role(
    remark: str,
    content: str,
    *,
    is_outbound: bool,
    ai_log: AiOutboundLog,
) -> tuple[str, str]:
    if not is_outbound:
        return "friend", "inbound"
    if ai_log.is_ai_reply(remark, content):
        return "ai", "outbound"
    return "self", "outbound"


def scan_discovered_friends() -> dict[str, int]:
    main_window = ensure_wechat_centered()
    return scan_for_new_messages(
        main_window=main_window,
        close_weixin=False,
        is_maximize=False,
    )


def enumerate_session_list_friends() -> list[str]:
    """遍历左侧会话列表，拿到所有可轮询好友。"""
    if WxMessages is None:
        return []
    ensure_wechat_centered()
    try:
        sessions = WxMessages.dump_sessions(
            chat_only=False,
            close_weixin=False,
            is_maximize=False,
        )
    except Exception as e:
        safe_print(f"[sync] 会话列表遍历失败: {e}")
        return []

    names: list[str] = []
    for item in sessions:
        if not item:
            continue
        name = str(item[0]).strip()
        if is_pollable_friend(name):
            names.append(name)
    deduped = sorted(set(names))
    safe_print(f"[sync] 会话列表共 {len(deduped)} 人: {', '.join(deduped)}")
    return deduped


def build_target_friends(
    whitelist: set[str],
    *,
    scan_unread: bool,
    poll_session_list: bool,
    state: dict[str, Any],
) -> set[str]:
    targets = set(whitelist)
    extended = set(state.get("extended_whitelist") or [])
    targets |= extended

    if poll_session_list:
        for name in enumerate_session_list_friends():
            targets.add(name)
            if name not in whitelist and name not in extended:
                extended.add(name)
                safe_print(f"[sync] 会话列表纳入: {name}")
        state["extended_whitelist"] = sorted(extended)

    if not scan_unread:
        return targets

    try:
        scanned = scan_discovered_friends()
    except Exception as e:
        safe_print(f"[sync] 扫描未读失败: {e}")
        return targets

    valid = {k: v for k, v in scanned.items() if is_pollable_friend(k)}
    for friend in valid:
        targets.add(friend)
        if friend not in whitelist and friend not in extended:
            extended.add(friend)
            safe_print(f"[sync] 未读新发现纳入: {friend}")
    state["extended_whitelist"] = sorted(extended)
    return targets


def collect_friend_messages(
    friend: str,
    *,
    wxid_cache: FriendWxidCache,
    ai_log: AiOutboundLog,
    state: dict[str, Any],
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    main_window = open_friend_chat(friend)
    chat_list = main_window.child_window(**Lists.FriendChatList)
    edit_area = main_window.child_window(**Edits.CurrentChatEdit)
    if not chat_list.exists(timeout=0.5) or not edit_area.exists(timeout=0.5):
        safe_print(f"[sync] {friend} 聊天界面未就绪")
        return [], wxid_cache.get(friend)

    wxid = wxid_cache.fetch_and_cache(friend, main_window=main_window)
    wxid_cache._close_chatinfo(main_window)
    Tools.activate_chatList(chat_list)
    text_items = [
        item
        for item in chat_list.children(control_type="ListItem")
        if item.class_name() == "mmui::ChatTextItemView"
    ]
    if not text_items:
        return [], wxid

    cursors: dict[str, Any] = state.setdefault("message_cursors", {})
    seen_ids: set[str] = set(state.setdefault("seen_message_ids", []))
    prev_cursor = cursors.get(friend)
    new_messages: list[dict[str, Any]] = []

    start_idx = 0
    if prev_cursor is not None:
        for idx, item in enumerate(text_items):
            if item.element_info.runtime_id == prev_cursor:
                start_idx = idx + 1
                break

    slice_items = text_items[start_idx:]
    if limit > 0:
        slice_items = slice_items[-limit:]

    for item in slice_items:
        content = (item.window_text() or "").strip()
        if not content:
            continue
        runtime_id = item.element_info.runtime_id
        is_outbound = Tools.is_my_bubble(main_window, item, edit_area)
        role, direction = classify_role(
            friend,
            content,
            is_outbound=is_outbound,
            ai_log=ai_log,
        )
        mid = message_id(friend, runtime_id, content)
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        new_messages.append(
            {
                "id": mid,
                "remark": friend,
                "wxid": wxid,
                "role": role,
                "direction": direction,
                "content": content,
                "message_type": "text",
                "runtime_id": str(runtime_id),
                "captured_at": utc_now(),
            }
        )

    if text_items:
        cursors[friend] = text_items[-1].element_info.runtime_id
    state["seen_message_ids"] = sorted(seen_ids)[-5000:]
    return new_messages, wxid


def build_contact_record(
    friend: str,
    wxid: str | None,
    *,
    in_whitelist: bool,
    source: str,
) -> dict[str, Any]:
    return {
        "remark": friend,
        "wxid": wxid,
        "wxid_status": "ok" if wxid else "pending",
        "in_whitelist": in_whitelist,
        "whitelist_source": source,
        "updated_at": utc_now(),
    }


class WebhookClient:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url.strip()
        self.token = token.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def push(self, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "webhook_disabled"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Sync-Token"] = self.token
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return {
                    "ok": 200 <= resp.status < 300,
                    "status": resp.status,
                    "body": text[:500],
                }
        except urllib.error.HTTPError as e:
            return {
                "ok": False,
                "status": e.code,
                "body": e.read().decode("utf-8", errors="replace")[:500],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


def run_sync_cycle(
    *,
    cfg: dict[str, str],
    store: SyncStore,
    wxid_cache: FriendWxidCache,
    ai_log: AiOutboundLog,
    push: bool = True,
) -> dict[str, Any]:
    whitelist = set(parse_friend_list(cfg))
    scan_unread = parse_bool(cfg, "SCAN_UNREAD", "1")
    poll_session_list = parse_bool(cfg, "SYNC_POLL_SESSION_LIST", "1")
    message_limit = int(cfg.get("SYNC_MESSAGE_LIMIT", "50"))
    state = store.load_state()

    targets = build_target_friends(
        whitelist,
        scan_unread=scan_unread,
        poll_session_list=poll_session_list,
        state=state,
    )
    contacts: list[dict[str, Any]] = []
    all_messages: list[dict[str, Any]] = []

    safe_print(f"[sync] 本轮同步目标: {len(targets)} 人")
    for friend in sorted(targets):
        source = "config" if friend in whitelist else "session_list"
        if friend in set(state.get("extended_whitelist") or []):
            source = "discovered" if friend not in whitelist else source

        if not wxid_cache.get(friend):
            wxid_cache.fetch_and_cache(friend)

        msgs, wxid = collect_friend_messages(
            friend,
            wxid_cache=wxid_cache,
            ai_log=ai_log,
            state=state,
            limit=message_limit,
        )
        contacts.append(
            build_contact_record(
                friend,
                wxid,
                in_whitelist=friend in whitelist or friend in targets,
                source=source,
            )
        )
        all_messages.extend(msgs)
        if msgs:
            safe_print(
                f"[sync] {friend}({wxid or '待查'}) 新消息 {len(msgs)} 条"
            )

    store.save_contacts(contacts)
    appended = store.append_messages(all_messages)
    state["last_sync_at"] = utc_now()
    store.save_state(state)

    export_payload = store.save_export_snapshot(contacts, all_messages)
    export_payload["event"] = "sync_batch"
    export_payload["stats"] = {
        "contacts": len(contacts),
        "new_messages": len(all_messages),
        "wxid_ok": sum(1 for c in contacts if c.get("wxid")),
        "wxid_pending": sum(1 for c in contacts if not c.get("wxid")),
    }

    all_saved_messages = store.load_all_messages()
    report_text = store.save_report(contacts, all_saved_messages, state)

    push_result: dict[str, Any] | None = None
    if push and parse_bool(cfg, "AUTO_PUSH", "1"):
        webhook = WebhookClient(
            cfg.get("SYNC_WEBHOOK_URL", ""),
            cfg.get("SYNC_WEBHOOK_TOKEN", ""),
        )
        if webhook.enabled:
            push_result = webhook.push(export_payload)
            safe_print(f"[sync] Webhook 推送: {push_result}")
        else:
            safe_print("[sync] 未配置 SYNC_WEBHOOK_URL，仅写本地 JSON")

    return {
        "contacts": contacts,
        "messages": all_messages,
        "appended": appended,
        "push_result": push_result,
        "export_path": str(store.export_path),
        "report_path": str(store.report_path),
        "report_text": report_text,
    }


def init_global_config() -> None:
    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 0
