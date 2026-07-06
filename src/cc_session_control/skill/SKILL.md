---
name: claude-session-doctor
description: Diagnose and rescue local Claude Code sessions via csctl — list live sessions, fix "/resume can't find it or rejects it", and print the exact resume command for any past session across directories. Use when a Claude session can't be resumed or found, /resume is rejected or shows nothing useful, the user asks which sessions are running, wants to reattach/接回/接管 a session, or is confused by bg agent / daemon / bridge / remote-control behavior. Triggers: 会话找不到, 接回会话, 串会话, 会话好乱, /resume 不行, resume 被拒, 哪些会话在跑, can't resume, session not showing.
---

# Claude Session Doctor (csctl)

诊断与接回本机（Linux/WSL）的 Claude Code 会话。会话形态多样、活/死判定互不相通，原生 `/resume` 还会隐藏会话——本 skill 给确定的命令，别让模型现编。

所有能力由 `csctl`（PyPI 包 `cc-session-control`）提供，任意目录可跑。没有 `csctl` 时先装：`uv tool install cc-session-control`；升级：`uv tool upgrade cc-session-control`。

## 交互式 TUI

```bash
csctl          # 三个 tab（tmux-first，启动落在项目 tab）：项目（Enter 新建 tmux 会话 / o 启动远控）/ 会话 / 后台 agent；清理在会话 tab 的子菜单
```

会话 tab 键位：方向键移动 · `/` 过滤 · `Enter` tmux 接回（主操作：恢复进 per-project tmux 窗口并进入，断线不死；已驻留 ⧉ 会话就地进入；接活会话先确认）· `t` 终端接回（裸终端兜底，真接续自动 cd+kill）· `f` 分叉进 tmux · `s` 停止活会话(确认) · `R` 转后台（进 tmux 不进入、不开远控，留在 csctl）· `d` 删除已结束会话 · `y` 复制命令到剪贴板 · `h` 桥接/SDK 显隐 · `c` 清理子菜单 · `r` 刷新 · `q` 退出。接回 = 离开 csctl（attach 进 tmux 或 exec 成 claude）。

## 心智模型：会话形态

| 形态 | 怎么产生 | 判活 |
|------|---------|------|
| **local** 前台 | 裸 `claude` 起的交互会话 | `claude agents` (kind=interactive) |
| **remote-control / bridge** | `claude --remote-control` 或 claude.ai 网页控制本机会话（transcript 带 `bridge-session` + `cse_` 云中继，**计算仍在本机**） | `claude agents` + 网页 |
| **bg agent** | detach 残留 / fleet 视图派生 / SDK 子 agent | `claude agents` (kind=background) |

**唯一权威判活 = `claude agents --json`**（`ps` 会骗你）。已结束 / blocked 的后台会话仍会被 `claude agents` 列出但 pid 为空——判活要看 pid 是否非空。bg agent 把 daemon 钉住是正常的，跑完自动放。

## 看现在有哪些会话/后台 agent 在跑

```bash
csctl agents   # 后台 agent 一览（live/settled、tempo、目录）
csctl env      # bridge 环境（当前绑定 + 孤儿）
```

status=busy/working 的**别杀**。

## 接回会话 / 修「/resume 找不到或被拒」

**先理解为什么找不到**（原生 `/resume` 两条硬规则）：

1. 会话按 cwd 存放在 `~/.claude/projects/<编码后的cwd>/<id>.jsonl`，`/resume` 和 `--resume` **都只在当前目录里找**。在错的目录里，连显式 `claude --resume <完整id>` 都会报 `No conversation found with session ID`——它**不会**自动切到会话原目录。所以接回前**必须先 `cd` 回会话原目录**（这就是 `csctl resume` 给的命令带 `cd` 前缀的原因）。
2. 同目录下，选择器还会隐藏带 `sdk-ts` / `bridge-session` 标记的会话（远程控制/网页/SDK 子 agent 建的）。但**在正确目录里显式 `claude --resume <完整id>` 不受这个过滤**，照样进得去。

**别跟选择器较劲，直接：**

```bash
csctl resume [关键词]            # 第1页(每页20条)，跨目录、含隐藏会话
csctl resume [关键词] --page 2   # 翻页
csctl resume [关键词] --limit 50 # 改每页条数
csctl resume [关键词] --all      # 不分页全列
```

跨目录扫全量 transcript（含隐藏会话），标 live/dead，打印可直接复制的接续命令。**关键词默认搜 transcript 正文**（sid/目录/标题先匹配，没命中再扫正文）——"标题里没有、正文里聊过"的会话也能找到（代价：常见词命中会偏多，用更具体的词缩小）。cwd 只从 transcript 读取，**从不靠目录名反推**，`cd` 路径可信；无正文的空壳会话（纯 bridge 占位）自动过滤。

**接回规则**（`csctl resume` 已按死活自动给对应命令，手动时记住）：

- 会话**已死**（不在 `claude agents`）→ `cd <dir> && claude --resume <id>`，全历史真接续。
- 会话**还活着** → 直接 `--resume` 会被拒。**真接续**（单时间线、无 fork）要先把它的后台进程停掉再接：`kill <pid> && sleep 1 && cd <dir> && claude --resume <id>`（代价是中断它正在跑的活）。**不想中断**就用 fleet 页 TUI 接管：空输入框按 `←` → 选中那行 → 按 `→`/Enter。
- 只有**想要一份分叉副本**（保留原会话另起一条）时才用 `claude --resume <id> --fork-session`。
- `csctl resume` 不会对**你当前所在的会话**给 kill 命令（防自杀），只标注你在这。

## 接管远程控制 / 网页会话到本地

`claude --remote-control` 或 claude.ai 网页控制的会话，在本机就是一个普通的**活会话**（会进 `claude agents`）。想搬到本地终端接管，照「接回规则」当"还活着"处理即可；`csctl resume` 已自动给出对应命令。TUI 里选中后 `Enter` tmux 接回（或 `t` 终端接回）。注意 0.7 起会话 tab **不再提供带 `--remote-control` 的 relaunch**（每个远控进程都会新铸一个云端环境）；要恢复远控暴露，用项目 tab 的 `o`/`c` 或会话内 `/remote-control`。

## 清理残留 bg agent

- 关窗口 / Ctrl+C 只是 **detach**，会话留成常驻 bg agent；`/exit` 才真正结束。
- 清掉**所有** bg agent（让 daemon idle-exit）：`claude daemon stop --any`（谨慎，会停所有）。

## 清理空/极短会话 transcript

按**人类提问数**清掉空壳和极短会话。**默认 dry-run 只统计**，`--apply` 才**硬删除、不可恢复**。

```bash
csctl prune                          # 按提问数统计
csctl prune --max-prompts 1          # 预览将删 ≤1 提问的
csctl prune --max-prompts 1 --apply  # 真删
csctl prune --sweep-orphans [--apply]   # 清残留孤儿目录
csctl prune --sweep-zombies [--apply]   # 清死进程的 sessions/<pid>.json
csctl prune --sweep-aged [--apply]      # 清超龄的时间键目录
```

- 安全：永不删**存活会话**、**当前会话**、**10 分钟内动过的**。
- 删会话时**连带清残留**：companion 目录、`session-env/<id>`、`file-history/<id>`、`jobs/<id前8位>`。
- **与原生清理的关系**：Claude Code 原生 `cleanupPeriodDays`(默认 30 天)只按**时间**在启动时硬删旧 transcript；`csctl prune` 按**内容**即时删，互补不冲突。别用"移到别处归档"——那会让会话逃出原生到期清理、永久占盘。
- Claude Code **没有**"归档会话"功能；要控量就是 prune + 调 `cleanupPeriodDays`。

## 本 skill 的安装/升级

skill 随包分发：`csctl skill install`（已存在时 `--force` 替换）、`csctl skill uninstall`。升级 csctl 后重跑 `csctl skill install --force` 刷新 skill 内容。
