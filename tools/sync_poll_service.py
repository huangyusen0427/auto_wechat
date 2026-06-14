"""
微信联系人/消息同步服务：轮询微信号与聊天消息，本地 JSON 存储，推送业务 Webhook。

- 复用居中微信配置
- 白名单 + 扫描未读发现的新好友一并同步
- 消息 role: friend(对方) / ai(AI回复) / self(人工发出)
- 本地 API 供业务系统拉取；Webhook 供业务系统接收推送

用法:
    python tools/sync_poll_service.py --once
    python tools/sync_poll_service.py --daemon
    python tools/sync_poll_service.py --api
    python tools/sync_poll_service.py --daemon --api
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import psutil
except ImportError:
    psutil = None

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from weixin_pace import apply_pace, patch_wxid_folder_lookup
from weixin_sync_lib import (
    AiOutboundLog,
    FriendWxidCache,
    SyncStore,
    WebhookClient,
    init_global_config,
    load_env_file,
    run_sync_cycle,
    safe_print,
)

DATA_DIR = TOOLS_DIR / "data"


def ensure_single_instance() -> None:
    if psutil is None:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "python.exe":
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "sync_poll_service" not in cmd or proc.info["pid"] == me:
            continue
        safe_print(f"[sync] 结束旧实例 PID={proc.info['pid']}")
        proc.kill()


def load_config() -> dict[str, str]:
    cfg = load_env_file(TOOLS_DIR / "llm_config.local.env")
    example = load_env_file(TOOLS_DIR / "llm_config.example.env")
    for k, v in example.items():
        cfg.setdefault(k, v)
    return cfg


class SyncApiHandler(BaseHTTPRequestHandler):
    cfg: dict[str, str] = {}
    store: SyncStore | None = None
    wxid_cache: FriendWxidCache | None = None
    ai_log: AiOutboundLog | None = None
    api_token: str = ""

    def _auth_ok(self) -> bool:
        token = self.api_token.strip()
        if not token:
            return True
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer "):
            got = got[7:]
        if not got:
            got = self.headers.get("X-Api-Token", "")
        return got == token

    def _json_response(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def log_message(self, format: str, *args) -> None:
        safe_print(f"[sync-api] {self.address_string()} {format % args}")

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._json_response(401, {"ok": False, "error": "unauthorized"})
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        store = self.store
        assert store is not None

        if path in {"/", "/health", "/api/v1/health"}:
            state = store.load_state()
            self._json_response(
                200,
                {
                    "ok": True,
                    "service": "wechat-sync",
                    "last_sync_at": state.get("last_sync_at"),
                },
            )
            return

        if path == "/api/v1/contacts":
            self._json_response(200, {"ok": True, "contacts": store.load_contacts()})
            return

        if path == "/api/v1/messages":
            limit = int((qs.get("limit") or ["200"])[0])
            remark = (qs.get("remark") or [""])[0].strip()
            messages = store.load_recent_messages(limit=limit)
            if remark:
                messages = [m for m in messages if m.get("remark") == remark]
            self._json_response(200, {"ok": True, "messages": messages})
            return

        if path == "/api/v1/export/latest":
            export_path = store.export_path
            if not export_path.exists():
                self._json_response(404, {"ok": False, "error": "no_export_yet"})
                return
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            self._json_response(200, {"ok": True, "data": payload})
            return

        self._json_response(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._json_response(401, {"ok": False, "error": "unauthorized"})
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        store = self.store
        assert store is not None
        wxid_cache = self.wxid_cache
        ai_log = self.ai_log
        assert wxid_cache is not None and ai_log is not None

        if path == "/api/v1/sync/run":
            body = self._read_json_body()
            push = bool(body.get("push", True))
            result = run_sync_cycle(
                cfg=self.cfg,
                store=store,
                wxid_cache=wxid_cache,
                ai_log=ai_log,
                push=push,
            )
            self._json_response(
                200,
                {
                    "ok": True,
                    "stats": {
                        "contacts": len(result["contacts"]),
                        "new_messages": len(result["messages"]),
                    },
                    "export_path": result["export_path"],
                    "push_result": result.get("push_result"),
                },
            )
            return

        if path == "/api/v1/push/latest":
            export_path = store.export_path
            if not export_path.exists():
                self._json_response(404, {"ok": False, "error": "no_export_yet"})
                return
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            payload["event"] = "manual_push"
            webhook = WebhookClient(
                self.cfg.get("SYNC_WEBHOOK_URL", ""),
                self.cfg.get("SYNC_WEBHOOK_TOKEN", ""),
            )
            result = webhook.push(payload)
            self._json_response(200, {"ok": result.get("ok", False), "result": result})
            return

        self._json_response(404, {"ok": False, "error": "not_found"})


def start_api_server(
    cfg: dict[str, str],
    store: SyncStore,
    wxid_cache: FriendWxidCache,
    ai_log: AiOutboundLog,
) -> ThreadingHTTPServer:
    host = cfg.get("SYNC_API_HOST", "127.0.0.1")
    port = int(cfg.get("SYNC_API_PORT", "8765"))
    token = cfg.get("SYNC_API_TOKEN", "")

    SyncApiHandler.cfg = cfg
    SyncApiHandler.store = store
    SyncApiHandler.wxid_cache = wxid_cache
    SyncApiHandler.ai_log = ai_log
    SyncApiHandler.api_token = token

    server = ThreadingHTTPServer((host, port), SyncApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    safe_print(f"[sync] 本地 API 已启动 http://{host}:{port}")
    safe_print("[sync] GET  /api/v1/health /contacts /messages /export/latest")
    safe_print("[sync] POST /api/v1/sync/run  /api/v1/push/latest")
    return server


def daemon_loop(cfg: dict[str, str], store: SyncStore, wxid_cache: FriendWxidCache, ai_log: AiOutboundLog) -> None:
    poll_min = float(cfg.get("SYNC_POLL_INTERVAL_MIN", "20"))
    poll_max = float(cfg.get("SYNC_POLL_INTERVAL_MAX", "40"))
    while True:
        try:
            run_sync_cycle(
                cfg=cfg,
                store=store,
                wxid_cache=wxid_cache,
                ai_log=ai_log,
                push=True,
            )
        except Exception as e:
            safe_print(f"[sync] 本轮失败: {e}")
            traceback.print_exc()
        sleep_sec = __import__("random").uniform(poll_min, poll_max)
        safe_print(f"[sync] 下轮 {sleep_sec:.1f}s 后")
        time.sleep(sleep_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="微信联系人/消息同步服务")
    parser.add_argument("--once", action="store_true", help="执行一轮同步后退出")
    parser.add_argument("--daemon", action="store_true", help="持续轮询同步")
    parser.add_argument("--api", action="store_true", help="启动本地 HTTP API")
    parser.add_argument("--no-push", action="store_true", help="本轮不推送 Webhook")
    args = parser.parse_args()

    if not any([args.once, args.daemon, args.api]):
        args.once = True

    ensure_single_instance()
    cfg = load_config()
    apply_pace(float(cfg.get("ACTION_DELAY_MIN", "0.1")))
    patch_wxid_folder_lookup()
    init_global_config()

    store = SyncStore(DATA_DIR)
    wxid_cache = FriendWxidCache(TOOLS_DIR / "friend_wxid_cache.json")
    ai_log = AiOutboundLog(DATA_DIR / "ai_outbound_log.jsonl")

    safe_print("[sync] 数据目录: tools/data/")
    safe_print("[sync] 消息角色: friend=对方, ai=AI回复, self=人工发出")
    safe_print("[sync] 导出文件: sync_export_latest.json (给业务系统)")

    server = None
    if args.api:
        server = start_api_server(cfg, store, wxid_cache, ai_log)

    try:
        if args.once:
            result = run_sync_cycle(
                cfg=cfg,
                store=store,
                wxid_cache=wxid_cache,
                ai_log=ai_log,
                push=not args.no_push,
            )
            safe_print(
                f"[sync] 完成 contacts={len(result['contacts'])} "
                f"messages={len(result['messages'])} "
                f"export={result['export_path']}"
            )
            safe_print(f"[sync] 报告已写入: {result['report_path']}")
            safe_print("")
            safe_print(result.get("report_text") or "")
            if not args.daemon and not args.api:
                return

        if args.daemon:
            if not args.once:
                safe_print("[sync] 先居中微信，开始持续轮询")
            daemon_loop(cfg, store, wxid_cache, ai_log)
    except KeyboardInterrupt:
        safe_print("\n[sync] 已停止")
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
