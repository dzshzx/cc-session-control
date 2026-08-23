# Codex 项目契约

## 范围与架构

- `csctl` 是仅限 Linux/WSL 的本地多 agent-CLI sessions（Claude Code / Codex / Kimi Code / opencode，ADR-0005）操作员 TUI。它读取 `~/.claude`、`~/.codex`、`~/.kimi-code`、`~/.local/share/opencode`（尊重官方 `CODEX_HOME`/`KIMI_CODE_HOME`；opencode 的数据目录随 `XDG_DATA_HOME` 迁移，无专属变量；`~/.config/csctl/providers.json` 声明 `codex_homes` 时，该清单取代继承来的 `CODEX_HOME` 成为完整 codex 身份集，ADR-0008）、检查 `/proc`，并调用本地 CLI 与 tmux。没有 `/proc` 时，显示降级状态，并拒绝无法证明当前 session 安全的破坏性操作。
- 保持 tmux-first 模型：csctl 派发的 agent sessions 统一进入 `csctl` tmux session，window 只以 CLI 裸名命名（`claude`/`codex`/`kimi`/`opencode`，声明的 codex 身份为 `codex-<label>`），不带项目名或 sid；已驻留在旧或用户自建 tmux session 的会话原地接入，不迁移。统一 session 的有效 `prefix2` 未设置时，csctl 只给该 session 设 `C-a`，手机端可从任一 provider TUI 用 `C-a s` 打开 tmux 会话树；已有 `prefix2`、主 prefix 与全局配置不改。所有 tmux subprocess 调用都属于 `data/tmux.py`。
- `data/` 以自底向上的 DAG 管理外部状态读写。`data/proc.py` 是唯一的 `/proc` 接缝，`data/liveness.py` 是 Claude liveness 权威，`data/providers/` 是 CLI 适配层（注册表、typed capabilities、argv 合成、非 Claude 磁盘发现与保守 liveness：argv 真实 resume 目标优先，kimi 可选的 hook 运行时注册表次之，csctl 自己写入且经进程身份复核的 tmux 派发元数据作为补充，存疑即不绑；无任何证据的裸启动 TUI 与 daemons 绝不是 kill 目标；Codex app-server 的精确 rollout fd 只产生 `hosted` 只读态，不产生 alive/pid/接管或删除权限——各 CLI 的证据来源、登记时机与绑定优先级见 `docs/architecture.md`「Provider 层」，版本化实测见 ADR-0005 与 `docs/claude-code-compatibility.md`），`config.py::cfg` 是唯一的路径权威。`views/` 只消费 `data/` 和 `actions/`，不得反向 import；refresh worker 只构建完整 generation，action worker 只返回 typed result，urwid widget 只能在 main loop 上变更。非 Claude provider 的来源故障降级为 issue 展示，绝不清空 Claude 视图；cleanup 只建模 Claude 状态。

## 外部失败

- 可预期、可恢复的只读探测失败返回带类型的安全值（`[]`、`{}`、`False` 或 `None`）；trust/settings、cleanup、refresh 与写操作保留 typed result、失败阶段和详情。缺失或畸形的运行时文件、`/proc` 扫描期间进程消失，以及 tmux/CLI 探测不可用、超时或返回非零，都必须把相应失败或降级状态暴露给操作员。
- 不得新增兜底式 `except Exception`。解析器、invariant 和编程错误必须带上下文地在 UI 或 CLI 错误边界保持可观察；绝不能变成看似成功的空结果。

## 开发护栏

- 使用 type hints。不得硬编码机器路径（`scripts/check.sh` 内的 `grep '/home/' src/` 守卫）。
- 提交前跑 `scripts/check.sh`——本地与 CI（`quality-gate.yml`）共用的唯一质量门：Ruff lint/format、mypy、路径守卫、带分支覆盖率的 pytest 与阈值检查。聚焦迭代时运行单个节点，例如 `uv run --extra dev pytest tests/test_views.py::test_sessions_view_filter_logic`。用 `tmp_path` 和 `monkeypatch` 造假，不要触碰实时 `~/.claude` 或 tmux 状态。
- 唯一版本源是 `src/cc_session_control/__init__.py`。用 `python scripts/bump_version.py {patch|minor|major}` 步进；候选提交必须先位于 `origin/master` 且该 SHA 的 CI 已成功，之后才创建匹配的带注解 `vX.Y.Z` tag 触发 PyPI Trusted Publishing。发布 tag 不移动、不复用；失败修复使用下一个 patch。`scripts/validate_release_tag.py` 用 `gh run list` 反查该 SHA 的 `CI` 结论（非 success 一律拒绝）并校验 `CHANGELOG.md` 顶部标题与版本一致，`release.yml` 的 `publish` 还 `needs` 一个独立跑 3.13/3.14 的矩阵 job，不再只靠 3.12 的 quality-gate 把关。
