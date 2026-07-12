# Agent Rules

完整开发指南（架构、命令、不变量）在 `CLAUDE.md`；建议会话开始时读一次 @CLAUDE.md，同一会话内不必重读。领域词汇表在 `CONTEXT.md`（Live Session、Bridge Environment 等）。
通用行为契约与机器事实由全局指令层承载；本文件只记非 Claude agent 需额外知道的项目事实。

## 项目边界

- csctl 是面向 Claude Code 自身 sessions/agents/Remote-Control 的操作员工具：它读取 `~/.claude` 的磁盘状态，遍历 `/proc`，并 shell 调用 `claude` + `tmux`（tmux-first，ADR-0001）。仅限 Linux/WSL。
- 贡献约束（CONTRIBUTING.md）：处处加 type hints；禁止硬编码机器路径——护栏 `grep -rn --include='*.py' '/home/' src/` 必须无输出。
- 版本号单一来源于 `src/cc_session_control/__init__.py`；只能通过 `python scripts/bump_version.py {patch|minor|major}` 步进；带注解的 `vX.Y.Z` tag 会经 Trusted Publishing 发布到 PyPI。
- 对通用「不吞错」规则的有意例外：data 函数吞掉错误并返回安全空值——TUI 绝不能崩溃。不要「修复」这一点。
- 测试：`uv run --extra dev pytest tests/`；优先用 `tmp_path`/`monkeypatch` 造假，而非触碰实时的 `~/.claude` 或 tmux 状态。
