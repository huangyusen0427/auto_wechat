# auto_wechat 自动回复

微信 PC 私聊 LLM 自动回复（基于 pyweixin UI 自动化）。

## 文件

| 文件 | 说明 |
|------|------|
| `auto_reply_11.py` | 主程序：多用户轮询 + LLM 随机 1~3 句回复 |
| `stop_auto_reply.py` | 停止监听 |
| `uia_keeper.py` | UI 守护，保持微信 UI 树可见（建议常开） |
| `weixin_pace.py` | 操作节奏与补丁（被主程序引用） |
| `llm_config.example.env` | 配置模板，复制为 `llm_config.local.env` |

## 快速开始

```powershell
# 1. 复制配置并填写 API Key
copy llm_config.example.env llm_config.local.env

# 2. 需已安装 pyweixin，且微信 4.1.6+ 已登录、UI 可见

# 3. 先开 UI 守护（可后台）
python uia_keeper.py --bg

# 4. 启动自动回复
python -u auto_reply_11.py

# 5. 停止（同时停守护）
python stop_auto_reply.py
```

## 注意

- `llm_config.local.env` 含密钥，勿提交 git
- `friend_wxid_cache.json` 为本地微信号缓存，自动生成
- 运行时会居中微信窗口并使用鼠标，建议专用机部署
