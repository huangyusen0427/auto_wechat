# auto_wechat 自动回复

微信 PC 私聊 LLM 自动回复（基于 pyweixin UI 自动化）。

## 文件

| 文件 | 说明 |
|------|------|
| `auto_reply_11.py` | 主程序：多用户轮询 + LLM 随机 1~3 句回复 |
| `stop_auto_reply.py` | 停止监听 |
| `weixin_pace.py` | 操作节奏与补丁（被主程序引用） |
| `llm_config.example.env` | 配置模板，复制为 `llm_config.local.env` |

## 快速开始

```powershell
# 1. 复制配置并填写 API Key
copy llm_config.example.env llm_config.local.env

# 2. 需已安装 pyweixin，且微信 4.1.6+ 已登录、UI 可见

# 3. 启动
python -u auto_reply_11.py

# 4. 停止
python stop_auto_reply.py
```

## 注意

- `llm_config.local.env` 含密钥，勿提交 git
- 运行时会居中微信窗口并使用鼠标，建议专用机部署
