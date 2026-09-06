# 项目契约

- csctl 是 Linux/WSL 本地多 CLI session 操作员 TUI。路径由 `config.py::cfg` 统一解析；架构接缝与 provider 身份证据见 `docs/architecture.md`，上游兼容变化见 `docs/claude-code-compatibility.md`。
- 保持 tmux-first：新派发会话进入 `csctl` tmux session，window 用 CLI 裸名或声明的 Codex 身份；既有会话原地接入。tmux 调用归 `data/tmux.py`。
- session-id、进程身份和当前 session 保护必须有证据；缺少 /proc 或绑定不确定时拒绝破坏性操作。Codex app-server 的 rollout fd 只产生 `hosted` 只读态，不授予 kill、接管或删除权限。
- cleanup 仅处理 Claude 状态：先预览精确候选，执行时重查保护条件，只处理批准子集。测试用 `tmp_path`、`monkeypatch`，不触碰实时 agent 目录或 tmux。
- 探测失败向操作员暴露降级原因；写操作保留 typed result、失败阶段与详情。不得新增兜底式 `except Exception` 把错误转为空成功。
- `views/` 消费 `data/` 与 `actions/`；urwid widget 只在 main loop 修改。使用 type hints，禁止硬编码机器路径。UI 中文、CLI 英文。
- 提交前运行 `scripts/check.sh`（与 CI 共用）。开发与架构资料按改动读取 `CONTRIBUTING.md`、`docs/architecture.md`。
- 唯一版本源是 `src/cc_session_control/__init__.py`。发布时按 `docs/releasing.md`：候选在 origin/master 且同一 SHA 的 CI 成功后推匹配 annotated tag；远端 tag 不移动、不复用。
