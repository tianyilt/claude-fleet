[English](README.md) | 中文

# Claude Fleet

同时开 5-7 个 Claude Code 窗口 vibe coding 的时候，你需要一个地方看到所有窗口在干嘛、谁卡了、谁做完了。

![](docs/screenshot-hero.png)

## 30 秒跑起来

```bash
git clone https://github.com/tianyilt/claude-fleet
cd claude-fleet && bash run.sh
# 浏览器打开 http://127.0.0.1:7878
```

首次运行自动建 venv 装依赖，不用管。

## 安装方式

**打包成 macOS app（Mac 上推荐）。** 一个签名的、可双击的 `.app`:

```bash
./scripts/build-app.sh --install      # 构建并拷到 /Applications
```

Resume/Fork 通过 `open -a`(LaunchServices)开新终端 —— **不需要自动化授权**,重启后
也照常工作。**Focus**(把拥有该 session 的 tab 切到前台)用的是 AppleScript,第一次用时
批准 **“Claude Fleet”想要控制“iTerm.app”** 即可,签名 app 让这个授权长期生效。需要热
重载开发时用 `./run.sh`。

**任意平台（Windows / Linux）。** dashboard、历史、搜索、监控都是跨平台的：

```bash
pip install -e .
./run.sh        # macOS/Linux
run.bat         # Windows
```

Resume/Fork 在 macOS(`open -a`)和 Linux(gnome-terminal / konsole / kitty /
wezterm / xterm…,可用 `CLAUDE_FLEET_TERMINAL_CMD` 指定)都能开真实终端,**Claude 和
Codex** 会话都支持。Windows(或任何没有已知启动器的环境)上则把 `claude --resume …`
命令复制到剪贴板,你粘到自己终端里跑。**Focus 仅 macOS。**

**Releases。** 打 tag 的发布会产出三种产物：`claude-fleet-macos-app.zip`（可双击的
app）、`claude-fleet-src.tar.gz`（任意平台）、`claude-fleet-windows.zip`。

## 解决什么问题

多窗口 vibe coding 的日常痛点：

- **Permission 通知一闪而过** → 红条常驻顶部，点一下跳回对应终端
- **不知道哪个窗口在干嘛** → 每张卡片显示当前任务、triage 状态、后台任务
- **做完的窗口忘记关** → patrol 引擎自动标 closeable，一键关闭
- **想找上周某个 session** → 全文搜索 50ms 返回，带 VS Code 风格匹配上下文
- **Skill 用了多少次不知道** → 三维统计（invoke + file read/write + bash 引用）
- **Memory 被谁改过** → 入度（↓被几个 session 参考）+ 出度（↑被几个 session 修改）

## 核心功能

### Triage 分类

不是简单的 busy/idle。Patrol 引擎读 transcript 的 `stop_reason`、`queue-operation` 事件和后台任务状态：

| 状态 | 含义 | 怎么判的 |
|------|------|---------|
| 🟢 working | 在干活 | busy 或有活跃 Monitor/Bash bg |
| 🔴 waiting | 等你批准 | permission prompt / dialog open |
| 🟡 stalled | 卡住了 | stop_reason=tool_use + 空闲>5min |
| 🔵 completed | 做完了 | stop_reason=end_turn + 空闲>5min |
| ⚪ closeable | 可以关了 | completed + 空闲>1h |

后台任务（Bash `run_in_background`、Monitor `persistent`）会追踪 tool_use/tool_result 配对，完成的自动清掉，不会误判成 working。

### 搜索

ripgrep 跨 Claude + Codex 全部 transcript，50ms 返回。不只搜 session 标题——搜 "hailuo" 能找到对话里提过 Hailuo 的 session，即使标题是 "你需要看下 klingai.com"。

每条结果带匹配上下文片段（最多 3 条），一眼看出为什么命中。

![](docs/screenshot-search.png)

### Skill / Memory 追踪

Skill 面板统计三个维度：

```
paper2video        333   1 invoke · ↓122 reads · ↑53 writes · 157 bash
feishu-notify       45  24 invokes · ↓7 reads · ↑7 writes · 7 bash
qzcli-topdowneval   12   3 invokes · ↓1 reads · ↑2 writes · 6 bash
```

只统计 `/skill-name` 正式调用的话是 44 次；加上 Read/Write/Edit skill 文件 + Bash 里引用 skills/ 的操作，实际是 431 次。

Memory 面板按 type 分组（user/feedback/project/reference），每条显示 `↓3 ↑2`（3 个 session 读过，2 个 session 改过）。

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### 时间线 + Plan 历史

点开任意 session 看完整对话流。Skill 调用紫色、Memory 读蓝色虚线、Memory 写粉红色。

Plan 版本历史：一个 session 通常迭代 5-14 次 plan，每次 Write 是完整快照，Edit 是红绿 diff。

![](docs/screenshot-timeline.png)

### 操作

| 按钮 | 做什么 |
|------|--------|
| Focus | 跳到那个终端 tab |
| Fork | `claude --resume <sid> --fork-session`，新 session 继承对话历史 |
| Resume | `claude --resume <sid>`，继续原 session |
| Review | 后台跑 `claude -p` 审查，结论（PASS/FAIL/PARTIAL）显示在卡片上 |
| Close | SIGTERM |

Fork/Resume 在 macOS 和 Linux 上都能开真实终端(Claude 和 Codex 会话都支持);Windows
或没有已知启动器的环境则把命令复制到剪贴板。Focus 仅 macOS。

> **Focus 设置（macOS）。** Focus 开箱即用,支持 Terminal.app 和 iTerm2——包括 session 跑在
> **tmux** 里(自带的 [`scripts/focus-tty.sh`](scripts/focus-tty.sh) 把进程 tty → 所属终端 tab →
> 切过去)。想换别的终端/窗口管理器,放一个可执行的 `~/.claude/focus-tty.sh`(接收 `<tty>` 参数)
> 即可,优先于自带默认。*(自带 shim 由 [@wanshuiyin](https://github.com/wanshuiyin) 贡献。)*

## 架构

单文件前端（Alpine.js + Tailwind CDN，不需要 npm），Python 后端只读 `~/.claude/` 和 `~/.codex/`，不改任何 agent 状态。

```
app.py                FastAPI + SSE (2s 轮询)
core/
  sessions.py         读 sessions/*.json，关联 TTY
  transcripts.py      解析 JSONL，提取 skill/memory/plan/后台任务
  patrol.py           triage 分类引擎
  codex.py            Codex session 解析
  search.py           ripgrep 跨平台搜索
  terminal.py         按平台分派的终端控制（macOS iTerm2；其它平台降级）
  actions.py          fork / review / close（在 terminal.py 外层做 session 查找）
  history.py          统一索引 + 全文 rg 搜索
  skills.py           skill 目录扫描
  memory.py           memory 文件解析
  plans.py            plan 关联（从 transcript 提取）
  perms.py            permission 事件
static/index.html     单文件 SPA
```

## 致谢

- [HarnessKit](https://github.com/RealZST/HarnessKit) — 跨平台 skill 管理的 UI 参考
- [Synergy](https://github.com/SII-Holos/synergy) — Memory engram 分类展示的灵感

## License

[MIT](LICENSE)
