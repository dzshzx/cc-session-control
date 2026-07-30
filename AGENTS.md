# Codex 项目契约

## 范围与架构

- `csctl` 是仅限 Linux/WSL 的本地 sessions、后台 agents 和 Remote Control 操作员 TUI。它读取 `~/.claude`、检查 `/proc`，并调用本地 CLI 与 tmux。没有 `/proc` 时，显示降级状态，并拒绝无法证明当前 session 安全的破坏性操作。
- 保持 tmux-first 模型：每个项目的 tmux window 是主要的 session 生命周期载体；Remote Control 是次要入口。所有 tmux subprocess 调用都属于 `data/tmux.py`。
- `data/` 以自底向上的 DAG 管理外部状态读写。`data/proc.py` 是唯一的 `/proc` 接缝，`data/liveness.py` 是 liveness 权威，`config.py::cfg` 是唯一的路径权威。`views/` 只消费 `data/` 和 `actions/`，不得反向 import；refresh worker 只构建完整 generation，action worker 只返回 typed result，urwid widget 只能在 main loop 上变更。

## 外部失败

- 可预期、可恢复的只读探测失败返回带类型的安全值（`[]`、`{}`、`False` 或 `None`）；trust/settings、cleanup、refresh 与写操作保留 typed result、失败阶段和详情。缺失或畸形的运行时文件、`/proc` 扫描期间进程消失，以及 tmux/CLI 探测不可用、超时或返回非零，都必须把相应失败或降级状态暴露给操作员。
- 不得新增兜底式 `except Exception`。解析器、invariant 和编程错误必须带上下文地在 UI 或 CLI 错误边界保持可观察；绝不能变成看似成功的空结果。

## 开发护栏

- 使用 type hints。不得硬编码机器路径；`grep -rn --include='*.py' '/home/' src/` 必须无输出。
- 全量运行 `uv run --extra dev pytest tests/`，或运行聚焦节点，例如 `uv run --extra dev pytest tests/test_views.py::test_sessions_view_filter_logic`。用 `tmp_path` 和 `monkeypatch` 造假，不要触碰实时 `~/.claude` 或 tmux 状态。
- 唯一版本源是 `src/cc_session_control/__init__.py`。用 `python scripts/bump_version.py {patch|minor|major}` 步进；匹配的带注解 `vX.Y.Z` tag 是 PyPI Trusted Publishing 的发布触发器。
