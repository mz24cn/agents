# TUI 对话客户端（`tui.py`）

> 玲珑 Agent Service 是一个完全基于 Python 标准库构建、零第三方运行时依赖的 Agent 服务。本文聚焦它的 TUI 对话客户端 `tui.py`：纯 Python 标准库实现的本地终端对话客户端，**零第三方依赖**，只连本机 `127.0.0.1` 服务——生产环境 SSH 登录服务器后 `python tui.py` 即开即聊；母端、子端/独立环境均可用，随增量包/自守装包自动分发到仓库根目录，无需手动安装。本文是 TUI 的唯一参考文档。

---

## 一、快速开始

```bash
python tui.py                   # 直接进入（免鉴权服务零提示）
python tui.py --port 8000       # 显式指定端口
python tui.py --session <id>    # 挂载历史会话
python tui.py --new             # 强制新会话
```

端口解析优先级：`--port` > `$AGENTS_RUNTIME_DIR/env.json`（默认 `~/.agents_runtime/env.json`）的 `AGENTS_URL`（https 自动启用不校验证书的 SSL）> 默认 `7988`；主机恒为 `127.0.0.1`。

---

## 界面布局

与 web ChatPage 顶栏对齐，界面由两条常驻栏组成；原启动 banner 三行已废除，内容并入这两栏：

- 顶栏（第一行）：模型名在最左、agent 名在最右（右对齐），中间依次为选中工具数与工作区路径（宽屏下还有会话标题），隧道启用时追加隧道徽章（`已注册->母端(在线/离线)`），未启用则不显示；窄终端按序收缩（先丢标题、再截/丢路径、再丢工具数）。
- 底部状态栏：左段为键位提示（回车发送、`/` 命令、上下回看、`/help` 帮助、Ctrl+C 中断/退出、Ctrl+L 重绘），右段为推理状态。

---

## 二、鉴权

- 服务未启用鉴权 → 直连；启用且未带 token → 默认 `getpass` 隐藏输入提示密码登录（`--login` 强制）。
- `--token <T>` 支持 `st_`（setup token，约 1 小时过期；反复使用建议 `as_` 或 `--login`）与 `as_`（API key）；GET 走 `?token=`，写方法走 `Bearer`。
- 非交互场景（脚本/管道/CI）必须显式 `--token`，否则报错退出。

---

## 三、键位

| 键 | 作用 |
| --- | --- |
| Enter | 发送；命令模式下执行命令 |
| Ctrl+C | 推理中 = 中断；空闲 = 退出 |
| Ctrl+L | 全量重绘 |
| Ctrl+O | 展开/收起最近 tool 块 |
| Ctrl+U | 清空输入 |
| ↑/↓ | 输入为空 → 转写区滚动；非空 → 历史输入 |
| Tab | 命令补全 |
| Esc | 关闭模态/补全 |
| / | 起始命令模式 |

底部状态栏左侧提示可用操作，右侧显示推理状态：

```text
回车发送 [/]命令 [上下]回看 [/help]帮助 [Ctrl+C]中断/退出 [Ctrl+L]重绘    推理:空闲
回车发送 [/]命令 [上下]回看 [/help]帮助 [Ctrl+C]中断/退出 [Ctrl+L]重绘    推理:进行中...
```

---

## 四、斜杠命令

| 命令 | 作用 |
| --- | --- |
| /new | 新会话（清空转写区上下文） |
| /sessions | 会话选择器，加载历史对话 |
| /continue | 对当前会话续推 |
| /agent、/model | 选择 agent / 模型 |
| /tools | 列出当前 agent 可见工具 |
| /status | 版本 / 鉴权 / 隧道摘要 |
| /tunnel | 隧道状态全字段（含状态徽标） |
| /tunnel register <url> | 注册到母端 |
| /tunnel unregister | 从母端注销 |
| /plain | 运行时切换 plain 模式（再次切回 TUI） |
| /help | 帮助页（键位 + 命令全表） |
| /exit | 退出 |

`/title <新标题>` 人工设置当前会话标题（与 Web 侧边栏同一接口）。

---

## 五、子端隧道注册

TUI 的主要用途之一：把本机 agent 注册到母端，供远程访问。拿到母端 setup 链接后，子端无头注册：

```bash
python tui.py --register <母端setup-url>   # 失败退出码 1，成功打印一行后继续
```

或进入 TUI 后执行 `/tunnel register <url>`；`/tunnel` 查看状态徽标。

---

## 六、plain 模式（远程排障留痕）

```bash
python tui.py --plain | tee chat.log
```

无 ANSI / 无备用屏幕的日志式输出，可管道/重定向；`/plain` 也可在运行中切换。

退出码：`0` 正常 / `1` 注册或登录失败 / `2` 服务不可达。平台：Linux 已实机验证；Windows 走 `msvcrt` + VT 序列路径，代码上支持但尚未实机验证。

---

*本文档为 TUI 的唯一参考文档，由 README「TUI 对话客户端」章节迁入，并按代码最终状态修订键位与斜杠命令。2026 年 9 月 17 日*
