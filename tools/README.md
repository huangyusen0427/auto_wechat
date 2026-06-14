# auto_wechat 工具集

微信 PC 自动化（基于 pyweixin UI）。核心是两个**完全独立**的脚本，互不引用，可单独运行：

| 脚本 | 功能 | 依赖 |
|------|------|------|
| `auto_reply_11.py` | 白名单轮询 + LLM 自动回复 | 仅 `weixin_pace.py` |
| `sync_poll_service.py` | 微信号 + 聊天内容同步，Webhook 推送 | 仅 `weixin_pace.py` |

## 其他文件

| 文件 | 说明 |
|------|------|
| `stop_auto_reply.py` | 停止自动回复 |
| `uia_keeper.py` | UI 守护，保持微信 UI 树可见（建议常开） |
| `test_post_moments_once.py` | 发朋友圈纯文字测试 |
| `weixin_pace.py` | 操作节奏与补丁（两脚本共用） |
| `llm_config.example.env` | 配置模板，复制为 `llm_config.local.env` |

## 独立自动回复 — `auto_reply_11.py`

```powershell
copy llm_config.example.env llm_config.local.env
python uia_keeper.py --bg
python -u auto_reply_11.py
python stop_auto_reply.py
```

## 独立同步微信号与聊天内容 — `sync_poll_service.py`

```powershell
python sync_poll_service.py --once
python sync_poll_service.py --daemon
python sync_poll_service.py --report
```

- 本地数据：`tools/data/sync_contacts.json`、`sync_messages.jsonl`、`sync_export_latest.json`、`sync_report.txt`
- 消息 `role`：`friend` 对方 / `ai` 自动回复 / `self` 人工发出
- 配置 `SYNC_WEBHOOK_URL` 自动推送；或 `GET http://127.0.0.1:8765/api/v1/export/latest` 拉取

本机 API：`/api/v1/health`、`/contacts`、`/messages`、`POST /api/v1/sync/run`

## 注意

- `llm_config.local.env` 含密钥，勿提交 git
- `friend_wxid_cache.json`、`tools/data/` 为本地数据，勿提交 git
- 运行时会居中微信窗口并使用鼠标，建议专用机部署
