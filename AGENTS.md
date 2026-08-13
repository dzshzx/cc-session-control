# Codex 项目契约

## 范围与架构

- `csctl` 是仅限 Linux/WSL 的本地多 agent-CLI sessions（Claude Code / Codex / Kimi Code，ADR-0005）操作员 TUI。它读取 `~/.claude`、`~/.codex`、`~/.kimi-code`（尊重官方 `CODEX_HOME`/`KIMI_CODE_HOME`；`~/.config/csctl/providers.json` 声明 `codex_homes` 时，该清单取代继承来的 `CODEX_HOME` 成为完整 codex 身份集，ADR-0008）、检查 `/proc`，并调用本地 CLI 与 tmux。没有 `/proc` 时，显示降级状态，并拒绝无法证明当前 session 安全的破坏性操作。
- 保持 tmux-first 模型：csctl 派发的 agent sessions 统一进入 `csctl` tmux session，以 `项目/会话` window 名保留项目线索；已驻留在旧或用户自建 tmux session 的会话原地接入，不迁移。所有 tmux subprocess 调用都属于 `data/tmux.py`。
- `data/` 以自底向上的 DAG 管理外部状态读写。`data/proc.py` 是唯一的 `/proc` 接缝，`data/liveness.py` 是 Claude liveness 权威，`data/providers/` 是 CLI 适配层（注册表、typed capabilities、argv 合成、非 Claude 磁盘发现与保守 liveness：真实 resume argv 优先；kimi 可选的运行时注册表（官方 SessionStart/SessionEnd hook 写入 `run/<pid>.json`，读时复核 pid 身份与 starttime）覆盖裸起会话；csctl 自己写入且经进程身份复核的 tmux 派发元数据作为补充；无任何证据的裸启动 TUI 绝不是 kill 目标），`config.py::cfg` 是唯一的路径权威。`views/` 只消费 `data/` 和 `actions/`，不得反向 import；refresh worker 只构建完整 generation，action worker 只返回 typed result，urwid widget 只能在 main loop 上变更。非 Claude provider 的来源故障降级为 issue 展示，绝不清空 Claude 视图；cleanup 只建模 Claude 状态。

## 外部失败

- 可预期、可恢复的只读探测失败返回带类型的安全值（`[]`、`{}`、`False` 或 `None`）；trust/settings、cleanup、refresh 与写操作保留 typed result、失败阶段和详情。缺失或畸形的运行时文件、`/proc` 扫描期间进程消失，以及 tmux/CLI 探测不可用、超时或返回非零，都必须把相应失败或降级状态暴露给操作员。
- 不得新增兜底式 `except Exception`。解析器、invariant 和编程错误必须带上下文地在 UI 或 CLI 错误边界保持可观察；绝不能变成看似成功的空结果。

## 开发护栏

- 使用 type hints。不得硬编码机器路径；`grep -rn --include='*.py' '/home/' src/` 必须无输出。
- 全量运行 `uv run --extra dev pytest tests/`，或运行聚焦节点，例如 `uv run --extra dev pytest tests/test_views.py::test_sessions_view_filter_logic`。用 `tmp_path` 和 `monkeypatch` 造假，不要触碰实时 `~/.claude` 或 tmux 状态。
- 唯一版本源是 `src/cc_session_control/__init__.py`。用 `python scripts/bump_version.py {patch|minor|major}` 步进；匹配的带注解 `vX.Y.Z` tag 是 PyPI Trusted Publishing 的发布触发器。
