# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`cc-session-control`（CLI：`csctl`）是面向 **Claude Code 自身** sessions、后台 agents 和 Remote Control 服务器的机器级操作员面板。它读取 Claude Code 的磁盘状态（`~/.claude/projects/*/*.jsonl` transcripts、`~/.claude/sessions/*.json` + `~/.claude/jobs/*/state.json` 注册表、`~/.claude.json`），遍历 `/proc`，并 shell 调用 `claude` CLI 和 `tmux`——它是*面向* Claude Code 的操作员工具，不是通用 app。自 0.7.0 起它是 **tmux-first 调度中心**（`docs/adr/0001-tmux-first-session-dispatch.md`）：每个主要动词都把操作员带入——或把 session 带入——一个 per-project tmux window，因此 session 默认能在终端/SSH 断连后存活；Remote Control 被降级为次要的手机/网页入口。TUI 有三个 tab，按 launcher 优先排序：**项目（Projects——启动 tab：新建 session 的 launcher + Remote Control 入口）**、**会话（Sessions）**、**后台（Background agents）**；cleanup 是 Sessions 内的子菜单，不是一个 tab。

`CONTEXT.md` 是本代码库的 **领域词汇表 / ubiquitous language**——先读它，了解 *Live Session*、*Bridge Environment* 的精确定义（以及 `_Avoid_:` 反例），尤其是下文架构所依赖的 *Session Remote Control* 与 *Project RC Server* 的区分。

## 命令

```bash
# csctl is a uv tool. Install / refresh it FROM PyPI — the package is published
# and this machine now tracks the PyPI release (not GitHub HEAD). Requires Python 3.12+.
uv tool install cc-session-control          # first install (or: pipx install cc-session-control)
uv tool upgrade cc-session-control          # refresh to the latest published release
#   csctl is a uv tool at ~/.local/bin/csctl — it is NOT mise-managed (no mise pin;
#   `mise install`/`mise uninstall …pipx…` are no-ops here). Verify: csctl --version
#   ESCAPE HATCH — to run UNRELEASED master before a tag, install the GitHub HEAD
#   build with a FORCED rebuild (plain `uv tool upgrade` keeps the cached git ref):
#     uv tool install --reinstall git+https://github.com/dzshzx/cc-session-control.git
#   This is no longer the default — prefer the PyPI release above.

# Run the installed TUI
csctl

# Dev/test ONLY — uv manages a transient .venv here; this is NOT how csctl is installed
# for use. Do not treat the editable .venv as the csctl you run day-to-day.
uv run --extra dev pytest tests/                                                # all
uv run --extra dev pytest tests/test_views.py::test_sessions_view_filter_logic  # single test
uv run csctl                                                                    # exercise local source changes

# Guardrail enforced for contributions (must return nothing)
grep -rn --include='*.py' '/home/' src/      # no hardcoded paths in product source
```

`csctl` 不只是 TUI——不带子命令运行会启动 TUI，但 `cli.py` 还暴露一个 headless CLI（子命令输出为英文；见 Conventions 说明）：

```bash
csctl rc status                              # RC status for all projects
csctl rc add <dir> | rc rm <dir>             # add/remove a project (by directory path) from the auto-start list (and start/stop it)
csctl rc up                                  # start every project on the auto-start list
csctl rc stop <proj> | rc list               # stop one project / show the enabled list
csctl prune [--sweep-orphans] [--sweep-zombies] [--sweep-aged] [--apply]  # cleanup; dry-run unless --apply
csctl resume [keyword] [--page N] [--limit N] [--all]  # cross-directory resume commands (incl. hidden sessions; body-search fallback)
csctl resume --take-over <sid>                 # execution-time re-resolution + guarded live takeover
csctl skill install [--force] | skill uninstall        # bundled Claude Code skill -> ~/.claude/skills/claude-session-doctor
csctl agents                                 # list background agents
csctl env                                    # list bridge environments (current + orphan)
```

`CONTRIBUTING.md` 的约束：每个源文件保持 **600 行以内**，使用 type hints，不硬编码路径。

## 架构

UI 工具库是 **urwid**（唯一的运行时依赖是 `urwid>=2.0.0`）。`src/cc_session_control/` 下分三层：

- **`data/`**——所有触碰外部状态的代码，读*和*写都在这里。它内部是一个 **bottom→top DAG**（无环）：
  - bottom（纯 IO + 解析）：`proc.py`（唯一的 `/proc` seam——`proc_starttime`/`pid_alive`/`ancestor_pids`/`scan_rc_server_inventory`）、`transcripts.py`（transcript inventory/stat/read/decode 的唯一 typed seam；`sessions.py` 投影与 `cleanup.py` 执行时重验共同消费它，cleanup 绝不反向 import 顶层 `sessions`）、`registry.py`（`sessions/*.json` + `jobs/*/state.json`，约 5s TTL 缓存）、`tmux_outcomes.py`（tmux 的纯 typed outcomes）、`tmux.py`（唯一的 tmux subprocess seam——见下文；本包内只能 import `proc` 和纯 outcome 模块）、`atomic_write.py`（唯一的 tmp→write→fsync→replace 原子写机制；`project_settings`/`environment_ledger`/`rc_enabled` 共用它，各自只保留 typed stage 映射）、`project_settings.py`（`~/.claude.json` typed read + per-project atomic write）、`rc_enabled.py`（locked rc-enabled read-modify-write）、`environment_ledger.py`（typed/locked/atomic ledger store）、`removal.py`（per-path cleanup result + filesystem removal seam）、`rc_environment.py`（按 `(window_id, pane_pid)` 缓存 managed RC pane 的 `env_*` 抓取，负结果与失败都用有界指数退避）。
  - middle：`liveness.py`（唯一的 liveness 权威——`alive_map`/`invalidate_cache`/纯函数 `live_index`）、`environments.py`（bridge-environment reconciliation——**绝不能 import `rc`**）、`cleanup.py`（per-dir-key + age 的清理策略）、`rc_outcomes.py`（RC 的 typed outcomes 与纯投影）。
  - top（组装）：`sessions.py`（把 typed transcript records + liveness/residency 投影为 `Session`，不拥有 transcript 文件 IO/解析）、`rc.py`（RC 领域逻辑：trust + autostart 列表 + `/proc` 服务器发现 → `RCProject`/`RCServer`；实际 tmux 调用消费 `tmux.py`，不写 environment ledger）。
  - `snapshot.py` 位于其余之上（把它们组合成一个 `WorldSnapshot`，并调用 `environments.reconcile` 持有唯一的 ledger 管道）；`data/` 里没有任何东西 import 它（只有 `refresh` 会）。
  返回 `models.py` 中的 dataclass（`Session`、`SessionProc`、`AgentJob`、`LiveInfo`、`RCProject`、`RCServer`、`EnvRecord`、`BridgeEnv`）。
- **`actions/`**——不属于 `data/` 的操作：`session_ops.py`（`take_over_result`、`resume_cmd`/`do_resume_result`、`do_tmux_resume_result`/`attach_target`、`to_clipboard`）、`agent_ops.py`（后台 agent 生命周期：`respawn_result`/`remove_job`/`watch`/`prepare_takeover`/`stop_job_result`，其中 `job_host` 把 sid→`sessions/<pid>.json` join 起来）、`resume_list.py`（headless `csctl resume` 的选择/分页/格式化——命令合成留在 `session_ops.resume_cmd`，绝不在这里重新推导）、`runner.py`（stay-in-TUI mutation 的 single-flight worker + `ActionResult`）、`tui_actions.py`（view model → frozen request → action adapter）、`feedback.py`（typed cleanup/delete result → notice），以及 `skill_ops.py`（bundled skill 的安装/卸载；skill 源作为 package data `skill/SKILL.md` 一并发布）。
- **`views/`**——每个 tab 一组 urwid widget（`sessions.py` + `_session_row.py` + `_sessions_cleanup.py`（cleanup 子菜单 `CleanupMixin`，为 600 行预算拆分）、`agents.py`、`rc.py`），全部继承 `_base.py::ListTabView`（共享的 walker/overlay/footer 管道——见下文 view contract）。`_colspec.py` 是**表格列的单一来源**：每个 tab 声明一个 `(sizing, align, header)` spec，并据此同时生成表头行和数据行（`header_columns`/`row_columns`，统一 `dividechars=2` 间隔）——绝不手写第二份 `urwid.Columns` 宽度列表。`_keytable.py` 对 key 做同样的处理：每个 tab 声明一个 `KEY_TABLE`（+ `HELP_LAYOUT`），其 footer 提示、help overlay 和 list-mode dispatch 全部由它生成（`footer_hints`/`help_lines`/`ListTabView._dispatch_key`）——绝不在提示字符串或 `elif` 阶梯里手写第二份 key 列表。sessions 的 状态 列三重编码状态（形状 + 词 + 颜色：`▸● 忙` busy / `● 闲` idle / `○ 停` dead），live session 的 tmux residency 再用三态呈现：`⧉` 是确认驻留，无徽标是确认 bare，ASCII `?` 是驻留证据不完整；展示读取 `Session.tmux_target` / `Session.tmux_inventory_complete` / `Session.tmux_inventory_detail`，动作也只把具体 `tmux_target` 当作 resident。调色板是一套语义集（`alive`/`status_busy`/`status_err`/`dead`/…）——没有 per-tab 的 attr 别名；其 dark/light 变体都来自顶层 `theme.py` 里唯一的 `_SPEC` 表（见下文），所以加颜色去*那里*，绝不内联一个 attr 元组。`app.py` 编排它们；`cli.py` 只持有 argparse 构建/dispatch，`cli_commands.py`/`cli_rc.py` 持有 handler 与 renderer，`cli_streams.py` 提供可注入输出边界；`config.py` 持有全局 `cfg` 单例，**并且是唯一的路径权威**（`cfg.sessions_dir`/`jobs_dir`/`environments_ledger`/各 cleanup 目录/`cleanup_age_days`）——绝不在别处内联 `claude_home / "..."`。

三个顶层 helper 位于这些包之外：`clipboard.py` 是**跨平台剪贴板 seam**（自动探测后端：WSL `clip.exe` → `pbcopy` → `wl-copy` → `xclip`，都不可用时返回 `False`）；`session_ops.to_clipboard` 是对它的一行委托，所以剪贴板后端改动放这里，而不是 `actions/`。`theme.py` 是**终端主题 seam**：一个 `_SPEC` 表同时生成 dark 和 light 调色板（名字绝不会分叉——body attr 通过 `default` 继承终端背景，只有结构性色带保留显式背景），`detect_mode()` 在启动时选定用哪一侧（`cfg.theme`/`CSCTL_THEME`/`--theme` override → OSC 11 tty 查询 → `$COLORFGBG` → dark）；`app._make_screen` 是它唯一的消费者，且 OSC 查询必须在 `loop.run()` 把 tty 交给 urwid *之前*完成。该查询带一个 **DA1 sentinel**（追加 `ESC[c`；读到 DA1 回复为止，绝不只读 OSC 终止符）——它让不应答的终端（tmux！实测 0.3ms 对比 250ms 上限）一个来回就返回，*并且*防止回复字节作为幽灵按键泄漏进 urwid。测试用一对 `os.openpty()` 驱动 fd 层的 `_query_bg_rgb_on`——pytest 的 capture 占用了 `sys.stdin/stdout`，所以 monkeypatch 它们会挂死。`__main__.py` 让 `python -m cc_session_control` 等价于 `csctl` 入口。

不变量是 **import 方向，而非纯度**：`views` 从 `data`/`actions` import；`data`/`actions` 绝不向上 import；`data` 内部上述 DAG 是单向的（尤其 `environments` 绝不 import `rc`）。没有单独的「纯读 vs 副作用」划分——`data/` 两者都装。

### view contract（`app.py` 如何通用地驱动 tab）

`App` 持有 `self.views: list[TabView]`，通过 `TabView` `Protocol`（定义在 `app.py`，`@runtime_checkable`）驱动每一个。Protocol 背后共享的*实现*是 `views/_base.py::ListTabView`——walker/listbox/status frame、保持焦点的 `_rebuild`（子类提供 `_build_rows`/`_status_text`）、居中的 `_show_overlay`、`_update_footer`、默认的 `handle_key`（overlay 模式经 `_overlay_active` hook，否则从 `KEY_TABLE` 走 `_dispatch_key`），以及 overlay 模式的按键 dispatch（`_handle_overlay_key`/`_exit_overlay`/`_close_overlay_mode`）都只在那里存在一次；新 tab 继承它，只添加行 + 按键语义（有额外模式的 view，如 Sessions filter/cleanup/preview，覆盖 `handle_key` 并回落到 `super()`）。反方向上，view 只通过 App 面向 view 的门面与其通信——`notify`/`confirm`/`submit_action`/`submit_completion`/`set_hints`/`trigger_async_refresh`/`refresh_with_notice`/`exit_with(intent)` 加 `is_active(view)`——绝不直接碰 `app.frame`/`app._active`/`app.views`。要添加/修改一个 tab，在结构上满足 Protocol——这些成员：

- `.widget`——tab body 的 urwid widget
- `._loaded`——bool；是否已在 main loop 应用过一个完整 generation
- `apply_refresh(batch)`——**只在 main loop 上运行**；从同一个完整 `RefreshBatch` 投影并重建 live walker。view 不做刷新 IO，也没有 worker 写入的 `_pending`
- `keyhints() -> str`——当前模式的 footer 提示字符串
- `handle_key(key)`——处理除 `Tab` 和 `q` 外的每个按键
- `captures_text()`——当 view 正在捕获原始文本输入（Sessions filter Edit）时为 True；此时 App 会把*每个*按键都转发给 `handle_key`，包括 `tab`/`q`，且 `set_hints` 去掉 Tab/q/r 的 footer 前缀（打字期间这些承诺是假的）。filter Edit 存在于 **view 自己的 frame footer**（status-bar 槽位），因此只要其模式保持活跃，`notify`/tab 切换都无法把它挤掉。

`App._input` 只处理 `tab`（切换）和 `q`（退出）——而且只在询问 `captures_text()` 之后；其余一律转发给活跃 view 的 `handle_key`。添加一个 tab 意味着同时更新 `self.views`、`TAB_NAMES` 和 `_switch_tab` 循环（它们按索引同步对齐）。

### Async refresh + 共享 world snapshot（线程模型）

扫描会触及文件系统、`/proc` 和子进程，所以它不能阻塞 urwid loop，且三个 tab 不能各自重复扫描（R11/D8）。模式（`App.trigger_async_refresh`）：

1. 一个 daemon 线程为整个 generation 计算**一个** `data/snapshot.py::build_world_snapshot()`，再由 `data/refresh.py::build_refresh_result()` 生成一个原子 `RefreshBatch`。`liveness.liveness_inputs()` 返回 generation-local、不可变的 `LivenessSnapshot`；`sessions.scan_result(inputs)`、agent 投影和 cleanup plan 共用其中同一组 `session_procs` / `cur` / `agent_jobs` / `agents_map`，不再重复逐 PID 的 `/proc` 判活。这里要区分两类成本：session liveness 是 targeted proc reads；RC server discovery 的 `proc.scan_rc_server_inventory()` 才是每 generation 一次完整 `/proc` 遍历。transcript、tmux、RC 与 `/proc` 的 issues 都跟着同一 typed generation 保持可见；新 generation 强制重新采集 liveness，不把前一代 PID verdict 当作新鲜状态。
2. 该线程向经 `loop.watch_pipe(self._on_pipe)` 注册的管道写入一个字节。
3. `_on_pipe` 在 main loop 上运行，对每个 view 调用 `apply_refresh(batch)`；三个 tab 同时切换到同一 generation。

`RefreshCoordinator` 让同一时刻至多有一个 generation 在构建/待消费，并把期间任意次数的刷新请求合并为至多一个 follow-up generation。`RefreshFailure` 只显示来源与详情，不应用半成品，画面保留 last-good generation。自动刷新每 10s 经 `set_alarm_in` 重新武装。**绝不从 worker 线程改动 urwid widget。** TUI action 同样经 `actions/runner.py::ActionRunner` 单飞：view 先冻结请求；写操作的 worker 返回 `ActionResult`，key 触发的外部读取/准备返回 `ActionCompletion[T]`，`App` 只在 Accepted 后关联 main-loop completion（Busy/Closed/异常/关闭都不能偷换或残留它）；main loop 再通知/刷新或应用 confirm/overlay/`ExitIntent`。需要 `exec`/tmux client 切换的动作本身仍走下文 `ExitIntent` 边界。完整决策见 ADR-0002。

### Resume 发生在 UI loop *之外*（进程替换）

TUI 无法在自身内部运行 `claude`。resume 家族的动作退出 MainLoop 时携带一个 **`ExitIntent`**（`actions/session_ops.py`：`ResumeIntent`/`AttachIntent`/`TmuxResumeIntent`/`TmuxNewIntent`）：view 构造 intent 并调用唯一通用的 `app.exit_with(intent)`；回到 `cli._cmd_tui`，`intent.run()` 在 loop *之外*收尾——例如 `ResumeIntent`（`t` 终端接回 fallback）→ typed `do_resume_result`，它最终 `os.chdir` 到 session 的 cwd 并 `os.execvp("claude", ...)`，**替换 csctl 进程**，失败则保留 detail。每个 intent 拥有自己的收尾器 + 失败消息；新增一个 resume 变体只触及 intent 类和 view 的按键处理器，绝不碰 `app.py`/`cli.py`。

**统一的接管语义：** `_resume_plan` 是运行时 `should_kill = alive and not current and not fork` 决策的单一来源；普通 **resume 会接管**一个 live session（先终止旧 pid），**fork 是副本**，让原进程继续运行。`resume_cmd`（`y` 键和 headless 列表的剪贴板字符串）绝不把 snapshot pid、`procStart` 或 cwd 序列化为延迟执行的破坏性 shell：live 非 fork 只生成 `csctl resume --take-over <sid>`。执行时的 shared typed exact-SID resolver `resolve_execution_session` 被 CLI take-over、`ResumeIntent`、`TmuxResumeIntent` 和转后台共同复用：只要是 live 非 fork 的 required takeover，就重新采集完整 liveness/transcript 证据，按完整 sid 要求唯一目标，拒绝 current、缺失、歧义和不完整 identity，并把新解析出的整个 `Session`（尤其 pid / proc_start / cwd）交给执行器；dead resume 与 fork 都是非破坏性的，即使 liveness 降级也不强制进入这条 destructive resolver。接管执行的单一来源仍是 `session_ops.take_over_result(pid, proc_start)`，它在真正发信号前做最后一次 current ancestor 检查和 PID-generation（`procStart`）检查，再 SIGTERM → settle → liveness 缓存失效；`tui_actions.stop_session`、`do_resume_result`、tmux 生成（经共享的 `_spawn_in_tmux` 骨架）和 `agent_ops.stop_job_result` 全都消费它，而非各自手搓序列。任何当前 operator contract 都不得教人保存或执行针对快照 PID 的裸终止命令。

**tmux 接回（`Enter`，主动作——ADR-0001）：** 在 session 自己的 per-project tmux session 内 resume 它（`tmux.session_name_for(cwd)`——每个项目一个 tmux session，每个 claude 一个 window；普通 `claude --resume`，有意**不加 `--remote-control`**——每个 RC 进程都会铸造一个新的 cloud env 条目），并把用户的终端带*进*那个 window，这样 SSH/手机连接断掉也不再杀死 session。`sessions.scan_result()` 每代只调用一次 typed `tmux.residency_inventory`，把 pane-pid ∈ session pid 的 `/proc` 祖先链批量映射到任意 tmux session，并写入 `tmux_target` + evidence completeness/detail；界面三态是 `⧉` 确认 resident、无徽标确认 bare、ASCII `?` 表示证据不完整。只有具体 `tmux_target` 才会让 `attach_target` 判定 resident 并**就地**进入（不杀、不确认、无 R10 gate）；unknown 绝不被当成 bare 后贸然接管，动作会保留 typed detail 并 fail closed。否则走 `would_take_over`/confirm/R10 路径，然后 `cli.py` 生成 window（`do_tmux_resume_result`）并进入它。整个 驻留→attach / 否则 confirm→spawn 序列只在 `views/_confirm.py::confirm_tmux_takeover` 中存在一次（Sessions `Enter`、Sessions `f` 且 `fork=True`——fork 绝不就地 attach，它会生成自己的 `<sid8>-fork` window——以及 后台 `Enter`）。进入 = `enter_window`：在 tmux 外用 `exec tmux attach-session`（替换 csctl，和 `do_resume_result` 一样）；在 tmux 内（`$TMUX` 已设置）用 `switch-client`，然后 csctl 正常退出——两条路径都会结束 csctl（「接回 = 离开 csctl」）。**终端接回（`t`，fallback）：** 经 `ResumeIntent`/`do_resume_result` 的裸终端 resume——session 随终端一起死掉；对 resident session，`t` 会通过同样的标准接管确认把它拉*出* tmux。

**转后台（`R` 键——无 Remote Control）：** `do_tmux_resume_result` 在 per-project tmux session 里生成普通 resume window（经 typed `tmux.run_in_tmux_result`，它通过 `-P` 返回精确的 "session:window_index" 目标并保留失败 stage/detail）**但不进入它**——操作员留在 csctl；notify 携带该目标。tmux-resident 的 session 会被拒绝并提示 已在 tmux（无可移动）。复用同样的 `should_kill` 交接与 execution-time resolver（live 且非 current 的 session → 用 fresh whole `Session` 先杀旧 pid；current session 被拒）。0.7 之前的 `R`（带 `--remote-control` 重启）已**移除**——Sessions tab 不再能铸造 cloud environment（ADR-0001）；手机/网页接管在 项目 tab（`o`/`c`）或 in-session `/remote-control`。Gotcha（对 RC servers 仍然成立）：杀掉一个 tmux window **不能**可靠地杀死一个 `--remote-control` 进程，所以要用 `s` terminate-by-pid 动作来停这类进程，而不是关闭它的 window。

**统一的跨 tab 按键表（tmux-first，ADR-0001）。** 三个 tab 共享一套动词词汇，让肌肉记忆可迁移：`r` = 刷新（每个 tab——在 App 级 `FOOTER_PREFIX` 中经 `App.set_hints` 只渲染一次，不是每个 view 各渲染），`Enter` = **进 tmux**，tab 的主动作——Sessions/后台 的 tmux 接回（resident → 就地进入；否则 resume 进 per-project tmux window 并进入）和 项目 的 tmux 新建（`do_tmux_new_result`：在项目自己的 per-project tmux session 里开新 `claude`，无 `--remote-control`，不杀任何东西 → 无 confirm/R10/trust gate，然后 `enter_window`），`t` = **终端接回**（经 `ResumeIntent` 的裸终端 fallback；仅 Sessions + 后台——项目 上没有 `t`），`s` = 停止（停止/杀死一个 live 进程——Sessions / Agents / RC 单个停止），`R` = 转后台（Sessions：tmux，无 RC，留在 csctl）和 Agents 上的 重启（respawn），`f` = 分叉进 tmux（仅 Sessions），`o` = 启动远控（仅 项目——被降级的 RC 启动；后台 tab 旧的 `o` 别名已废弃），`d` = 删除一条已 settled 的记录。**「杀前确认」是统一的（不止两个键）：** 每个终止 live 进程的操作都经 App 级 `App.confirm(message, on_yes)` modal 确认（在 `_input` 中路由；`_confirm_yes` 把关）——Sessions `s` / `Enter`-live-非 resident / `t`-live / `R`-live，Agents `s` / `Enter`-live-非 resident / `t`-live，RC `s`（单个，仅在 running 时）/ `S`（全部，无 running 时跳过）。一个 resume 动词是否为接管（先杀旧 pid）读取 `session_ops.would_take_over`——唯一的 `should_kill`，所以确认关卡绝不重新推导它；resume 一个 DEAD session 什么都不杀，且*不*确认，就地进入一个 RESIDENT session 同样什么都不杀（无 confirm，无 gate）。整个 降级 gate → confirm → 执行 序列只在 `views/_confirm.py` 中存在一次——接管用 `confirm_takeover`，tmux-first 的 Enter/f 用 `confirm_tmux_takeover`（驻留→`AttachIntent`；否则 confirm→`TmuxResumeIntent`），以及普通停止的孪生 `confirm_stop`（降级 gate → alive → current → confirm，文案由一个名词派生；RC 传 `gated=False`，因为它的停止杀的是一个 tmux window，不是 pid）——外加 `_DEGRADED` 拒绝字符串和 `stop_message` 文案。view 调用这些，而不是把步骤重新内联。非 kill 操作保持单键：Sessions/Agents `d`（删除 settled）、Agents `R`（respawn）、`f`（fork——副本，从不确认）。`f` 分叉 有一个 current-session 守卫（和 `Enter`/`s` 一样）。相同键 → 跨 tab 相同汉字（`s`=停止，`Enter`=接回/新建=进 tmux，`t`=终端接回）；`R`（转后台 vs 重启）是有意的双含义并如此注册。R10 降级 gate 在 confirm *之前*触发，且感知接管：它拒绝一个脱离 `/proc` 的 live 接管，但仍允许 resume 一个 dead session。confirm 文案遵循一个模板——`{动词}{对象}「name」？{后果}`（接管类「将先终止原进程。」/ 停止类「将终止其进程。」）。`c` 保持 tab 专属（Sessions cleanup / RC 切换 自动远控）——不属于通用动词集。RC `ServerRow`/`EnvRow` 是 `selectable() == False`（仅显示；焦点跳过它们）。

### Liveness 与身份——sessionId 是主键

**主键是 `sessionId`**，绝非 pid。Liveness 是一次 **多源合并**，以 `data/liveness.py` 为**唯一权威**：

- `data/liveness.py::alive_map()` 运行 `claude agents --json`（缓存 5s）→ `{sessionId: pid}`（仅 agent sessions——它*不*列出 RC servers，也不反映 RC exposure）。
- `data/registry.py::read_session_procs()` 读取 `sessions/<pid>.json` 以获取更丰富的 per-runtime 状态（`status`/`procStart`/`kind`/`entrypoint`/`bridgeSessionId`）。**文件存在 ≠ alive**（多数是僵尸）。
- `data/proc.py::pid_alive(pid, procStart)` 确认一个 pid 是真的：`/proc/<pid>` 存在**且**其 stat starttime（第 22 个字段——在最后一个 `)` *之后*解析，因为 `comm` 可能含空格/括号）等于记录的 `procStart`（这挫败 pid 复用）。
- `data/liveness.py::live_session_procs()` 是把两者 join 起来的唯一装配点：registry 的 `SessionProc` 行，经 `pid_alive` 注入 `proc_alive`。`proc_alive` 是一个**三态哨兵**：registry 解析时留它为 `None`（= 尚未注入），只有这个 seam 会设 `True`/`False`——且破坏性消费者拒绝 `None`（`select_zombie_pids` 绝不把未注入的行判为僵尸；`host_pid_for_sid` 绝不据此回答 `alive=True`），所以把原始 registry 行喂给僵尸清扫会安全失败（什么都不删），而不是把每个 session 文件都判为 dead。
- `data/liveness.py::live_index(session_procs, agents_map)` 是一个**纯的、依赖注入的**函数，按 sid 合并两个源，处理 **resume 的多 pid**（一个 sid → 多个 `sessions/<pid>.json`；挑选 proc-alive 的 pid，把*所有* alive pid 保留在 `pids` 里），返回 `{sid: LiveInfo}`。`sessions.scan_result()` 取一代数据后调用它（所以合并可无 IO 单元测试）。

一个 sid **alive** 当且仅当其某个 pid 满足 `pid_alive`，或它以**非空 pid**出现在 `alive_map()` 中——`claude agents --json` 会持续列出 settled/blocked 的 bg session 而*不带* pid，那些不算 alive（没有进程可发信号；无 pid 的条目绝不覆盖基于 `/proc` 的判定）。`alive_map()` 还**在缓存刷新时擦除过期 pid**（在 `/proc` 可用时，一个 `/proc/<pid>` 已消失的上报 pid 会经 `proc.probe_pid` 被清空，这样一个 `claude agents` 尚未回收的死 worker 无法把它的 sid 翻回 alive；没有 `/proc` 时该 map 原样透传——它是降级模式下唯一的 liveness 源）。终止是唯一改变 liveness 的 session 操作，且 `take_over_result` / `stop_job_result` **自己使缓存失效**——调用者不用手动调 `invalidate_cache()`。（删除/清理只作用于已经 dead 的 session。）

**「current」= 自我保护。** `data/proc.py::ancestor_pids()` 沿父链向上遍历 `/proc/<pid>/stat` 直到 csctl 自己的祖先。一个 session **current** 当且仅当其任一 alive pid 在该集合中（感知多 pid）——即启动了 csctl 的那个 session，受保护：你不能 resume、terminate 或 prune 它。

**跨平台安全（R10）。** `/proc` **仅限 Linux/WSL**。没有 `/proc` 时，`pid_alive`/`ancestor_pids` 返回空，liveness 降级为 `alive_map()`——而由于**此时无法确定「current」**，破坏性操作（terminate/delete/clean/stop/remove）会**拒绝**（`proc.probe_current_ancestors().complete` 把关），而不是冒着打到 csctl 自己 session 的风险；UI 会标示降级。

### Session 模型与 transcript 解析

`transcripts.load_inventory()` 是 transcript discovery/stat/read/decode 的唯一入口：它枚举 `~/.claude/projects/*/*.jsonl`，逐行扫描（一个廉价的子串预检为提速守卫每次 `json.loads`——保持这个模式），并返回 records + issues；`sessions.scan_result()` 只把这些 records 经 `live_index()` + registry 富化为 `Session`，再以一次 `tmux.residency_inventory` 注入 residency typed evidence。cleanup 执行时也直接重新读取同一个 transcript inventory 保护 orphan removal，绝不依赖 top-layer session projection。显示 `label` 优先级：`aiTitle` → 第一条非噪声用户 prompt → `lastPrompt` → `(untitled)`。`transcripts.py` 中的 `_NOISE` / `_clean_text` 剥除 command/system-reminder 的包裹标签，使 prompt 读起来干净。桥接/SDK 隐藏过滤以 `Session.bridge_or_sdk` 为键（D9：transcript `hidden` 标签与 registry `source == "sdk"` 的并集），因此徽标与 `h` 开关绝不打架。

**RC exposure 是一个纯谓词。** `liveness.is_rc_exposed(bridge, pid_alive) = bool(bridge) and pid_alive`——一个 session 的 *session remote control* 只有在其 `bridgeSessionId` 为 truthy *且*其 proc alive 时才算「exposed」（僵尸的过期 bridge *不*算）。在完整的 missing/null/string × alive/dead 矩阵上做过单元测试；`sessions.scan_result` 和 `environments.observe_live` 都调用这唯一的实现。同样，**带命名空间的 id 解析是单一来源**：`models.split_env_id` 是 `session_*`/`cse_*`/`env_*` id 的唯一切分器（registry/environments/rc 全都经它路由），`EnvRecord.env_id`/`BridgeEnv.env_id` 作为其格式化逆运算。

### 后台 agents（后台 tab）

一个后台 agent 的持久真相是 `jobs/<short>/state.json`（`registry.read_agent_jobs` → `AgentJob`），*不是* `sessions/`。`state.json` **不带 pid**，所以 host pid 从 `job.sid → sessions/<pid>.json` join 出来：**批量富化循环是 `liveness.enrich_jobs(jobs, session_procs=None)`**——snapshot、agents view 的自取和 `csctl agents` 背后唯一的实现（传共享 snapshot 的 `session_procs` 以零额外 IO；`None` 则自取一次）——而 `actions/agent_ops.py::job_host` 保留为生命周期操作使用的单 job 便捷函数（一个没有 sessions 文件的 live worker 无法停止——一个有记录的孤儿风险）。生命周期操作：`respawn_result`（经 `shlex.join` 的 `claude --resume <resume_sid> <flags> --bg`，在 tmux 中生成——绝不替换 csctl）、`prepare_takeover`（把 job 适配成一个 `Session` 以走现有 resume 路径）、`watch`（只读 `timeline.jsonl`）、`remove_job`（仅 settled）、`stop_job_result`（仅 live，向 join 出的 host pid 发信号；杀一个 `--remote-control`/bg worker 可能无法完全回收它——孤儿风险在 UI 中暴露）。

### Remote Control：tmux servers、/proc 发现、三个命名空间

**Managed RC servers 是 tmux windows**，位于名为 `rc` 的 session（env `CSCTL_RC_SESSION`）中。`rc.start_one(path)` 启动一个 `claude remote-control --name <basename> --spawn same-dir` 进程，并在 window 上写入 `@csctl_path` user option——**window 与项目的 join 键是这个路径元数据（外加 `pane_current_path` 对旧 window 的领养 fallback），绝不是 window 名**（名字只是装饰，可重名；tmux 按名寻址还会回退前缀匹配，可能误中）；kill/capture 一律用 server 级唯一的 `#{window_id}`。它有意**不**自动重启：每个新的 Remote Control 进程都会注册一个新的 cloud environment，自动重启会堆出一堆同名的重复 mobile/web environment 条目。状态来自 tmux `#{pane_dead}`：`running` / `dead` / `stopped`；重启是显式的用户动作。

所有 tmux 访问都经单一 seam **`data/tmux.py`**：`_tmux_run_result` 独占普通的有界 `subprocess.run` 调用；`capture_pane_result` 是独立的有界 streaming `Popen` + selector seam，最多请求/保留 2000 行并设 1 MiB byte limit，避免先让 subprocess 无界收集输出。纯 `KillResult` / inventory / capture / write outcomes 位于 `tmux_outcomes.py`；production 决策一律消费 typed results 并保留 stage/detail/issues——旧的 bool/text primitive wrappers 已随 typed 迁移移除，不再新增。新的 tmux 操作作为封装加在 `tmux.py`，而不是在别处裸调 `subprocess`。`rc.py` 只保留 RC 领域逻辑和绑定到 `cfg.rc_session` 的 typed RC 范围委托：`_tmux_window_inventory`、`_tmux_capture_pane_result` 与按路径 join 的 `_window_for_inventory`。`actions/session_ops.py` 和 `actions/agent_ops.py` 为 resume/relaunch 的 tmux 调用直接 import `tmux`（不是 `rc`），因此 session-resume 路径绝不依赖 RC 模块。

**超出 tmux 的发现（`rc.scan_servers_result()`）。** RC servers 也通过 typed `proc.scan_rc_server_inventory()` 遍历 `/proc` 找到（纯函数 `proc._match_rc_cmdline` 匹配 argv0 basename `claude` *且*一个 `remote-control` 子命令 token *且* `--name`——用 `--remote-control` *flag* 的 codex 被排除）。一个属于 csctl 管理的 tmux pane 的被发现 pid 是 **managed**；否则它是 **external** 且**只读**（csctl 绝不接管/重启它）。Managed servers 的 `env_*` cloud id 通过 `_tmux_capture_pane_result` → bounded `capture_pane_result` → `rc_environment.EnvironmentIdCache` 抓取；同一 `(window_id, pane_pid)` 的成功抓取跨 generation 复用，普通 miss 与 typed capture failure 都按 monotonic clock 有界退避并最终重试，失败 detail 在 RC inventory 中持续可见，窗口消失/PID 改变会 prune，start/stop/remove 会显式失效。`rc.py` 只返回这些 observations，不调用 ledger persistence。

**三个 Remote Control 命名空间——相互独立，绝不通过后缀关联：**
- **session remote control** → `sessions/<pid>.json` 中的 `bridgeSessionId: session_*`（一个前台 session 暴露自己）。三态：key 缺失（从未开启）/ `null`（瞬态——重新开启会覆盖它）/ string（已暴露）。「现在已暴露」= 上文的 `is_rc_exposed` 谓词。
- **background agent env** → `jobs/<short>/state.json` 中的 `bridgeSessionId: cse_*`。一对 `cse_*` resume 共享一个后缀（同一 env）；`session_*` 和 `cse_*` 后缀绝不重合 → 只会在命名空间*内部*去重。
- **project rc server env** → `env_*`，只打印到服务器的 stdout/QR，**没有**任何 state 文件——唯一的本地信号是运行中的进程 + 其 pane 输出。

**Bridge-environment reconciliation（`data/environments.py` + `data/environment_ledger.py`，R6）。** Claude Code 只保留每个 session/agent 的*当前*绑定（一个被覆盖的字段），所以被切走或历史铸造的 cloud environment 会从磁盘消失。csctl 保留自己的 append-only ledger（`$XDG_CONFIG_HOME/csctl/environments.jsonl`），让它们保持可追溯。`environments.py` 是被动 reconciliation 层：它**绝不 import `rc`**；`snapshot.py` 与 `csctl env` 把 RC observations 传给 `environments.reconcile(evidence, rc_servers, inventory_issues=...)`，`environment_ledger.py` 独占 typed/locked/atomic persistence。两个观测层级：
- `observe()`——bridge 为 truthy 的 **FILE-REFERENCED** 集合（此刻任何磁盘文件所引用的每个 env，alive 或 zombie，+ 从 rc servers 传入的 `env_*`）。这定义了 ledger 的**成员资格**。
- `observe_live()`——**alive 门控的** CURRENT/已绑定集合（用于显示；僵尸的过期 bridge *不*显示为已绑定）。

整个管道存在于唯一入口 `environments.reconcile(...)` → `environments.Reconciliation`（current / orphans / 两个观测层级 + typed `LedgerUpdate`）：它拥有承重的顺序——observe（file-referenced）→ upsert → observe_live → 分类，其中 orphan 是相对 FILE-REFERENCED 层计算的——所以 `build_world_snapshot`（每个周期）和 `csctl env` 绝不重新接线这些部件。`Reconciliation` 不携带本地化字符串；CLI 把 typed failure/warning 渲染成英文诊断与非零状态，TUI 只渲染紧凑的中文计数/状态。一个之后被切走的 env 留在 ledger 里，但退出 file-referenced 集合。三个自洽的层级：`active(alive) ⊆ file-referenced(现在在 ledger 里) ⊆ ledger(历史)`，且 **`orphan = ledger − file-referenced`**——那些 orphan 就是手动删除候选。ledger 写入是 **write-on-change + `tmp+rename` 原子 + `fcntl` 咨询锁 + 保留/压实**；missing/partial/read/write/fsync/replace/unlock 等状态不会再伪装成完整历史。**能力红线：** csctl **没有 deregister**——它只能在本地遗忘并打印一份清单；orphan 列表天然不完整（csctl 未运行时铸造的 env 无法回填）。`csctl env` 列出 cloud environment rows；项目 tab 有意不列这些 rows（csctl 无法作用于 cloud env），但 TUI snapshot 每个周期仍运行 reconciliation、更新 ledger 并显示异常计数。

**两个独立的「start」概念——不要混为一谈**（RC tab 中的列）：
- `auto_start`（「开机自启」）——项目在 csctl 自己位于 `$XDG_CONFIG_HOME/csctl/rc-enabled` 的列表中；控制 `csctl rc up` / `A` 键启动什么。
- `rc_at_startup`（「自动远控」）——`<proj>/.claude/settings.local.json` 中的 per-project `remoteControlAtStartup` flag；控制 **`claude` 自身**是否在启动时开启 Remote Control。三态（`True`/`False`/`None`=未设）。`remoteControlSpawnMode`（也类似三态，`None`=未设）一并搭载在 `RCProject.spawn_mode` 上。

**项目成员资格是路径主键、无 workspace root 概念**（0.7.3 起）：项目 tab 列出 `~/.claude.json` `projects` map 中**有效受信任**且目录存在的条目，主键是绝对路径，`RCProject.name` 只是派生 basename 显示名。有效信任 = `models.effective_trust_decision`——**唯一的三态信任谓词**（成员扫描与 `start_one` gate 共用，绝不重推）：`TRUSTED` / `UNTRUSTED` / `UNAVAILABLE`；证据不可读、畸形或 schema 无效时 fail closed，且诊断保持可观察。自身或任一祖先条目带 `hasTrustDialogAccepted: true`，按路径分段边界匹配（`workspace` 不覆盖 `workspace-external`）、只 normpath 不 realpath。**显式 `False` 不是否决**——上次语义实测（Claude Code 2.1.218，2026-07-23）它是「被祖先信任抑制、从未被询问」的落盘痕迹，拒答对话框根本不写条目；这是上游依赖，每次发布按 `docs/claude-code-compatibility.md` 重验。**平台临时目录不是项目**（0.7.4 起）：路径等于或位于 `rc._TEMP_ROOTS`（`tempfile.gettempdir()` + `/tmp` + `/var/tmp`）子树下的条目不经信任发现上榜——这是成员/显示规则，不碰信任状态（刻意受信任的 `/tmp` 继续为 scratch 会话抑制对话框）；在自启列表或已有 rc window 的显式可操作条目照常显示（与「目录缺失但可操作则保留」同款例外）。`rc-enabled` 自启列表存绝对路径；旧短名行在 typed `list_result` 首读时按冻结的旧探测逻辑（`_legacy_workspace_root`，迁移专用死代码）解析并原子重写一次（tmp+rename，保留注释行）。`rc_enabled.py` 的 `EnabledListResult` 对每次事务保留精确 `operation` / `stage` / `detail` / `changed` / `committed`：lock/read/write/fsync/replace/cleanup/unlock 等失败不会变成空清单，尤其 atomic replace 已提交后的 unlock failure 仍是 failed + `committed=True`，调用者不得继续启动/停止；CLI 明示变更已在失败前提交，TUI 另标记需刷新确认。production scan、CLI 和 TUI 都传播这些 outcomes（bool/list primitive 兼容层已移除，`*_result` 是唯一表面）。启动 RC 仍要求项目有效受信任。完整决策见 ADR-0003。

### Cleanup——两种策略，preview 优先（`data/cleanup.py`，R7）

Cleanup 逻辑完全位于 `data/cleanup.py`；Sessions 子菜单和 `csctl prune` CLI 都驱动它。key **按目录定类型，绝不假设 uuid==sid**：
- **策略 A——per-dir key 语义。** `session-env`/`file-history`/`tasks`/`uploads` 是 **sid-keyed** → orphan = 不在已知 session 集合中的 sid。`sessions/` 是 **pid-keyed** → 清扫僵尸（`pid_alive` 为 false），但**保留 current-bound 的 pid 和任何 current session 的 pid 文件**，且当一个 sid 有多个 pid 时保留 alive 的那些。`debug/` 是 **debug-run-id-keyed**（*不是* sid）→ 有自己的语义。
- **策略 B——age 清扫。** `shell-snapshots`/`telemetry`/`plans`/`backups`/`paste-cache` 是 time/global-keyed → 按 mtime 超过 `cfg.cleanup_age_days` 丢弃。

表面映射：Sessions 子菜单暴露 empty/short session prune + sid-keyed orphan 目录；`csctl prune` 暴露相同的（`--sweep-orphans`）**外加** pid-keyed 僵尸清扫（`--sweep-zombies`）和 age 清扫（`--sweep-aged`）。所有 cleanup 都是 **plan 冻结且 preview 优先**：`build_plan` 每个周期把候选冻结一次为一个 `CleanupPlan`（分类计数、TUI preview 和 CLI dry-run 的唯一来源——因此这些计数意味着「现在可操作」：empty/short 计的是*可 prune 的* session，而非原始 prompt 数），`execute_*` 函数**至多删除那份冻结列表**，在执行时对每一项用新鲜的保护数据重新校验（删除 ⊆ 预览——preview 之后变 alive 或变已知的 session/pid/sid 会被跳过，宁可少删）。在 TUI 中每个子菜单动作是一条 `views/_sessions_cleanup.py::_CleanupAction` 记录（counts key、R10 gate、plan targets、preview 行/文案、executor）——像 `_keytable`/`_colspec` 一样表驱动，绝不再来一条 elif 阶梯。所有这些都**排除 live + current**，并**在无法确定 current 时拒绝**（无 `/proc`，R10）——age 清扫除外，它只看 mtime 且与 session 无关。`jobs/` 绝不自动清扫（只有对一个 settled agent 显式调 `agent_ops.remove_job` 才会移除一个 job 目录）。

## 约定

- **UI 字符串是简体中文**（通知、状态、按键提示、帮助屏）。**CLI 子命令输出是英文。** 添加字符串时遵循这一点。
- 可预期的外部失败由所属边界显式建模：允许降级的只读探测返回有类型的安全值；trust/settings、ledger、cleanup、refresh 和写操作保留 typed result、失败阶段与详情，并让 CLI/TUI 可见。不得用 broad `except Exception` 把 parser/invariant/编程错误伪装成空结果或成功。
- 破坏性 cleanup 总是先 preview：`_enter_preview` 在一个 `Overlay` 中显示目标，`_confirm_cleanup` 在第二次 `Enter` 时执行。
- Config 是 `config.py` 中单一的全局 `cfg = Config()`；测试通过 monkeypatch `cfg` 属性来覆盖路径（例如 `cfg.claude_home`、`cfg.claude_json`、`cfg.config_dir`）。

## 发布与 CI

完整维护者指南：`docs/releasing.md`。不那么显然的点：

- **版本号单一来源**于 `src/cc_session_control/__init__.py`（`pyproject.toml` 经 setuptools dynamic 派生它）。只能通过 `python scripts/bump_version.py {patch|minor|major}` 或 `--set X.Y.Z` 步进——它只编辑那一个文件。然后加一条 `CHANGELOG.md` 条目。
- **打 tag 即发布。** 推送一个带注解的 `vX.Y.Z` tag（与 `__version__` 匹配）会触发 `.github/workflows/release.yml`，它重新运行检查、构建、对 wheel + sdist 做 smoke 测试，并**经 Trusted Publishing 发布到 PyPI**（GitHub environment `pypi`，OIDC——不存储 API token）。PyPI trusted publisher 已经配置好（owner `dzshzx`，repo `cc-session-control`，workflow `release.yml`，env `pypi`）。
- **CI**（`.github/workflows/ci.yml`）在每次推送到 `master` 和 PR 时运行相同的 测试 + `/home/` grep + 构建 + smoke 关卡。
- **TestPyPI dry run**（`.github/workflows/release-testpypi.yml`）是一个手动 `workflow_dispatch`，发布到 TestPyPI（env `testpypi`）而不触碰真实索引——真正打 tag 前的可选彩排。
- **Gotchas：** 已发布的版本是不可变的——绝不覆盖它，而是 bump 到下一个 patch。`dist/` 被 gitignore；本地的预发布序列镜像该 workflow（`uv run --extra dev pytest tests/`、`/home/` grep、`uv build --no-sources`、wheel/sdist 的 `csctl --version`、`uvx twine check dist/*`）。发布后立刻，`uv` 可能看不到新版本，直到你用 `uv ... --refresh` 打爆它的索引缓存。
