"""
轮询白名单好友新消息，LLM 自动简短回复（随机 1~3 条，节奏随机但偏快）。

经典模式：微信窗口居中 + 鼠标操作，多用户轮询。

启动: python tools/auto_reply_11.py
停止: python tools/stop_auto_reply.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"), flush=True)


TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from weixin_pace import apply_pace, patch_wxid_folder_lookup

from pyweixin import Messages
from pyweixin.Config import GlobalConfig
from pyweixin.Uielements import Edits, Lists
from pyweixin.WeChatTools import Navigator, Tools
from pyweixin.utils import scan_for_new_messages

SYSTEM_PROMPT = """你是 Claude Code 助手，在微信里帮用户写代码、答疑、聊天。

输出规则（非常重要）：
1. 像真人发微信，口语化、自然，每次语气略有变化
2. 按要求的条数输出极短回复，用换行分隔
3. 每条 15~40 字，绝不超过 50 字
4. 禁止 markdown、加粗、列表、长段落、emoji 堆砌
5. 不要自我介绍，直接回答问题
"""


def rand_float(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def rand_reply_count() -> int:
    """1/2/3 条各约 33%。"""
    return random.randint(1, 3)


class RandomPace:
    """随机节奏：打破固定间隔，但整体保持较快。"""

    def __init__(self, cfg: dict[str, str]) -> None:
        self.poll_min = float(cfg.get("POLL_INTERVAL_MIN", "3"))
        self.poll_max = float(cfg.get("POLL_INTERVAL_MAX", "6"))
        self.gap_min = float(cfg.get("REPLY_GAP_MIN", "0.08"))
        self.gap_max = float(cfg.get("REPLY_GAP_MAX", "0.35"))
        self.send_min = float(cfg.get("SEND_DELAY_MIN", "0.1"))
        self.send_max = float(cfg.get("SEND_DELAY_MAX", "0.28"))
        self.llm_min = float(cfg.get("LLM_GAP_MIN", "0"))
        self.llm_max = float(cfg.get("LLM_GAP_MAX", "0.2"))
        self.action_min = float(cfg.get("ACTION_DELAY_MIN", "0.1"))
        self.action_max = float(cfg.get("ACTION_DELAY_MAX", "0.25"))

    def poll_sleep(self) -> None:
        time.sleep(rand_float(self.poll_min, self.poll_max))

    def reply_gap(self) -> None:
        time.sleep(rand_float(self.gap_min, self.gap_max))

    def send_delay(self) -> float:
        return rand_float(self.send_min, self.send_max)

    def action_delay(self) -> float:
        return rand_float(self.action_min, self.action_max)

    def wait_llm_gap(self, last_call_at: float) -> None:
        gap = rand_float(self.llm_min, self.llm_max)
        elapsed = time.time() - last_call_at
        if elapsed < gap:
            time.sleep(gap - elapsed)


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {path}，请复制 llm_config.example.env 为 llm_config.local.env"
        )
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def parse_friend_list(cfg: dict[str, str]) -> list[str]:
    raw = cfg.get("TARGET_FRIENDS") or cfg.get("TARGET_FRIEND") or "11"
    return [x.strip() for x in raw.split(",") if x.strip()]


def ensure_single_instance() -> None:
    if psutil is None:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "auto_reply_11" not in cmd:
            continue
        if proc.info["pid"] == me:
            continue
        print(
            f"[auto_reply] 已有实例 PID={proc.info['pid']}，请先: python tools/stop_auto_reply.py"
        )
        sys.exit(1)


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 200,
    temperature: float = 0.85,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


class ShortReplyEngine:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        pace: RandomPace,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.pace = pace
        self.histories: dict[str, list[dict[str, str]]] = {}
        self.last_call_at = 0.0

    def _history_for(self, friend: str) -> list[dict[str, str]]:
        if friend not in self.histories:
            self.histories[friend] = [{"role": "system", "content": SYSTEM_PROMPT}]
        return self.histories[friend]

    def generate(self, friend: str, text: str) -> list[str]:
        self.pace.wait_llm_gap(self.last_call_at)

        want = rand_reply_count()
        history = self._history_for(friend)
        history.append(
            {"role": "user", "content": f"请用{want}条极短微信回复（每条独立成句）：\n{text}"}
        )
        trimmed = [history[0]] + history[-20:]

        try:
            raw = chat_completion(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                messages=trimmed,
                temperature=rand_float(0.75, 0.95),
            )
        except Exception as e:
            _safe_print(f"[auto_reply] LLM 失败: {e}")
            return ["稍等，我这边卡了一下，你再发一次？"]

        replies = self._split_short_replies(raw, want)
        history.append({"role": "assistant", "content": "\n".join(replies)})
        self.last_call_at = time.time()
        _safe_print(f"[auto_reply] 本次回复 {len(replies)} 条")
        return replies

    @staticmethod
    def _split_short_replies(raw: str, want: int) -> list[str]:
        lines = []
        for part in raw.replace("；", "\n").split("\n"):
            part = part.strip().strip("-•* ")
            if not part:
                continue
            if len(part) > 50:
                part = part[:50]
            lines.append(part)
        if not lines:
            return [raw[:40] if raw else "嗯，收到。"]
        return lines[:want]


def parse_scan_unread(cfg: dict[str, str]) -> bool:
    raw = (cfg.get("SCAN_UNREAD") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def seed_friend_baselines(friends: set[str], last_runtime: dict[str, int]) -> None:
    """启动时给每个白名单好友建立基线，避免第一次轮询把未读当旧消息跳过。"""
    for friend in sorted(friends):
        try:
            main_window = open_friend_chat(friend)
            chat_list = main_window.child_window(**Lists.FriendChatList)
            if not chat_list.exists(timeout=0.5):
                _safe_print(f"[auto_reply] {friend} 基线跳过: 无消息列表")
                continue
            Tools.activate_chatList(chat_list)
            text_items = [
                item
                for item in chat_list.children(control_type="ListItem")
                if item.class_name() == "mmui::ChatTextItemView"
            ]
            if not text_items:
                continue
            last_runtime[friend] = text_items[-1].element_info.runtime_id
            _safe_print(f"[auto_reply] {friend} 基线已建立")
        except Exception as e:
            _safe_print(f"[auto_reply] {friend} 基线失败: {e}")


def scan_unread_friends() -> dict[str, int]:
    """扫描会话列表里有红点的联系人。"""
    main_window = ensure_wechat_centered()
    return scan_for_new_messages(main_window=main_window, close_weixin=False, is_maximize=False)


def build_poll_targets(
    whitelist: set[str],
    *,
    scan_unread: bool,
    last_runtime: dict[str, int],
) -> set[str]:
    targets = set(whitelist)
    if not scan_unread:
        return targets
    try:
        scanned = scan_unread_friends()
    except Exception as e:
        _safe_print(f"[auto_reply] 扫描未读失败: {e}")
        return targets

    if scanned:
        names = ", ".join(f"{k}({v})" for k, v in scanned.items())
        _safe_print(f"[auto_reply] 扫描到未读: {names}")
    for friend in scanned:
        targets.add(friend)
        if friend not in last_runtime:
            _safe_print(f"[auto_reply] {friend} 新发现(有未读)，将纳入轮询")
    return targets


def ensure_wechat_centered():
    """打开微信并固定到屏幕中央（pyweixin 默认行为）。"""
    main_window = Navigator.open_weixin(is_maximize=False)
    Tools.move_window_to_center(Window_handle=main_window.handle)
    Tools.cancel_pin(main_window)
    return main_window


def open_friend_chat(friend: str):
    """切换到好友聊天窗口，窗口居中。"""
    main_window = Navigator.open_dialog_window(
        friend=friend,
        is_maximize=False,
        search_pages=0,
    )
    Tools.move_window_to_center(Window_handle=main_window.handle)
    return main_window


def poll_friend_new_text(friend: str, last_runtime: dict[str, int]) -> str | None:
    """轮询单个好友最新文本（鼠标激活聊天列表 + 拍一拍判断收发）。"""
    main_window = open_friend_chat(friend)
    chat_list = main_window.child_window(**Lists.FriendChatList)
    edit_area = main_window.child_window(**Edits.CurrentChatEdit)
    if not chat_list.exists(timeout=0.5) or not edit_area.exists(timeout=0.5):
        _safe_print(f"[auto_reply] {friend} 聊天界面未就绪")
        return None

    Tools.activate_chatList(chat_list)
    text_items = [
        item
        for item in chat_list.children(control_type="ListItem")
        if item.class_name() == "mmui::ChatTextItemView"
    ]
    if not text_items:
        return None

    latest = text_items[-1]
    runtime_id = latest.element_info.runtime_id
    prev = last_runtime.get(friend)
    if prev is None:
        last_runtime[friend] = runtime_id
        if Tools.is_my_bubble(main_window, latest, edit_area):
            return None
        content = (latest.window_text() or "").strip()
        if content:
            _safe_print(f"[auto_reply] {friend} 首次发现对方消息")
        return content or None
    if runtime_id == prev:
        return None

    last_runtime[friend] = runtime_id
    if Tools.is_my_bubble(main_window, latest, edit_area):
        return None

    content = (latest.window_text() or "").strip()
    return content or None


def poll_and_reply_loop(
    *,
    whitelist: set[str],
    engine: ShortReplyEngine,
    pace: RandomPace,
    scan_unread: bool,
) -> None:
    processed: set[str] = set()
    last_runtime: dict[str, int] = {}

    ensure_wechat_centered()
    _safe_print("[auto_reply] 微信已居中，建立白名单基线")
    seed_friend_baselines(whitelist, last_runtime)

    while True:
        targets = build_poll_targets(
            whitelist,
            scan_unread=scan_unread,
            last_runtime=last_runtime,
        )
        for friend in sorted(targets):
            try:
                content = poll_friend_new_text(friend, last_runtime)
            except Exception as e:
                _safe_print(f"[auto_reply] 轮询 {friend} 失败: {e}")
                continue

            if not content:
                continue

            key = f"{friend}|{content}"
            if key in processed:
                continue
            processed.add(key)

            _safe_print(f"[auto_reply] {friend} -> {content[:60]}")
            try:
                replies = engine.generate(friend, content)
                for i, reply in enumerate(replies):
                    apply_pace(pace.action_delay())
                    Messages.send_messages_to_friend(
                        friend=friend,
                        messages=[reply],
                        close_weixin=False,
                        search_pages=0,
                        send_delay=pace.send_delay(),
                    )
                    _safe_print(f"[auto_reply] 已回 {friend}: {reply}")
                    if i < len(replies) - 1:
                        pace.reply_gap()
                    poll_friend_new_text(friend, last_runtime)
            except Exception as e:
                _safe_print(f"[auto_reply] 回复 {friend} 失败: {e}")

        pace.poll_sleep()


def main() -> None:
    cfg = load_env_file(TOOLS_DIR / "llm_config.local.env")
    api_key = cfg.get("MIMO_API_KEY") or os.environ.get("MIMO_API_KEY", "")
    base_url = cfg.get("MIMO_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
    model = cfg.get("MIMO_MODEL", "mimo-v2.5-pro")
    friends = parse_friend_list(cfg)
    scan_unread = parse_scan_unread(cfg)
    pace = RandomPace(cfg)

    if not api_key:
        raise ValueError("请在 llm_config.local.env 中设置 MIMO_API_KEY")

    ensure_single_instance()
    apply_pace(pace.action_min)
    patch_wxid_folder_lookup()

    GlobalConfig.close_weixin = False
    GlobalConfig.is_maximize = False
    GlobalConfig.search_pages = 0

    engine = ShortReplyEngine(
        base_url=base_url,
        api_key=api_key,
        model=model,
        pace=pace,
    )

    _safe_print(f"[auto_reply] 白名单: {', '.join(friends)}")
    _safe_print(f"[auto_reply] 模型: {model}")
    _safe_print("[auto_reply] 模式: 窗口居中 + 鼠标 + 多用户轮询")
    _safe_print(f"[auto_reply] 扫描未读: {'开' if scan_unread else '关'}")
    _safe_print("[auto_reply] 回复: 随机1~3条(各约33%)，节奏随机偏快")
    _safe_print(
        f"[auto_reply] 轮询: {pace.poll_min}~{pace.poll_max}s | "
        f"发送: {pace.send_min}~{pace.send_max}s"
    )
    _safe_print("[auto_reply] 停止: python tools/stop_auto_reply.py")

    try:
        poll_and_reply_loop(
            whitelist=set(friends),
            engine=engine,
            pace=pace,
            scan_unread=scan_unread,
        )
    except KeyboardInterrupt:
        _safe_print("\n[auto_reply] 已停止")
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
