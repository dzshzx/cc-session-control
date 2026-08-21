# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`cc-session-control`（CLI：`csctl`）是面向本机 **agent CLI**（Claude Code / Codex CLI / Kimi Code / opencode——ADR-0005 多 CLI provider 层）sessions 的机器级操作员工作台。它读取各 CLI 自己的磁盘状态（Claude：`~/.claude/projects/*/*.jsonl` transcripts、`~/.claude/sessions/*.json` 注册表、`~/.claude.json`；Codex：`~/.codex/sessions/**/rollout-*.jsonl` 首行 `session_meta` + `session_index.jsonl`，外加平铺的 `~/.codex/archived_sessions/`（行打 `archived` 标记）；Kimi：`~/.kimi-code/session_index.jsonl` + per-session `state.json`；opencode：`~/.local/share/opencode/opencode.db`（SQLite，随 `XDG_DATA_HOME` 迁移）的 `session` 表根行；尊重官方 `CODEX_HOME`/`KIMI_CODE_HOME`——但一旦 `~/.config/csctl/providers.json` 声明 `codex_homes`，那份清单就是完整的 codex 身份集，继承来的 `CODEX_HOME` 不再参与，见 ADR-0008），遍历 `/proc`，并 shell 调用各 CLI 与 `tmux`——它是*面向*这些 agent CLI 的操作员工具，不是通用 app。自 0.7.0 起它是 **tmux-first 调度中心**（`docs/adr/0001-tmux-first-session-dispatch.md`）；ADR-0006 进一步把 csctl 派发的所有 agent sessions 统一放入 `csctl` tmux session 的项目标记 window，因此 session 默认能在终端/SSH 断连后存活且跨项目切换不再跨 tmux session。已驻留在旧或用户自建 tmux session 的会话原地接入，不迁移。TUI 有两个 tab，按 launcher 优先排序：**项目（Projects——启动 tab：多 CLI 新建 session 的 launcher（Enter=CLI 选择器，仅列已启用 provider、默认焦点 claude——Enter-Enter 即新建 claude；x=codex / k=kimi / O=opencode 直达）+ 成员取舍（p/h/H））**、**会话（Sessions——各 CLI 身份的统一列表，CLI 列 cc/cx/km/oc，多 codex 身份各用自己的 label 如 cx2）**；cleanup 是 Sessions 内的子菜单，不是一个 tab，且只建模 Claude 状态。

ADR-0010/0011 再补两条现行事实：Codex app-server 只凭同身份进程 fd 表里**精确的 active rollout 路径**标记 `Session.hosted`，该态不冒充 `alive`、没有 session pid、所有接回/分叉/停止/删除/复制命令路径都拒绝；统一 `csctl` tmux session 的有效 `prefix2` 为 `None` 时，csctl 只给该 session 设 `C-a`，手机端可从任一 provider TUI 用 `C-a s` 打开 `choose-tree -Zs`，已有第二前缀、主前缀和全局 tmux 配置不改。

`CONTEXT.md` 是本代码库的 **领域词汇表 / ubiquitous language**——先读它，了解 *Live Session*、*Bridge Environment* 的精确定义（以及 `_Avoid_:` 反例），尤其是下文架构所依赖的 *Session Remote Control* 的定义（*Project RC Server* 与后台 agents 已随 ADR-0009 从模型中移除）。

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

`csctl` 不只是 TUI——不带子命令运行会启动 TUI，`cli.py` 还暴露一个**面向 agent 的最小 headless CLI**（0.8 起只剩 `resume`；`agents` 随 0.8.8 移除——ADR-0004/0009。RC 管理已随 0.8.8 整体移除；cleanup 是 TUI 专属表面，`prune`/`env`/`skill`/`rc`/`agents` 子命令已移除。子命令输出为英文；见 Conventions 说明）：

```bash
csctl resume [keyword] [--page N] [--limit N] [--all]  # cross-directory resume commands for ALL providers (incl. hidden sessions; body-search fallback; non-Claude rows tagged [codex]/[kimi], unbound-live rows flagged [live?], archived rows flagged (archived))
csctl resume --take-over <sid>                 # execution-time re-resolution + guarded live takeover (Claude sids only)
```

会话接回行为以 `csctl resume` / TUI 为权威。

`CONTRIBUTING.md` 的约束：文件体量当作**设计信号**判断（按职责内聚与可导航性拆分，不设行数门槛——见其 Code Style 一节），使用 type hints，不硬编码路径。

## 架构

完整架构参考（模块 DAG、view contract、并发/刷新模型、resume/tmux 语义、liveness 权威、cleanup 策略）见 `docs/architecture.md`；此处只做导航，跨改动必须遵守的不变量摘要见下方「约定」。

- **`src/cc_session_control/data/`**——唯一触碰外部状态（文件系统/`/proc`/tmux/CLI 子进程）的层，内部是 bottom→top 单向 DAG（`proc`/`transcripts`/`registry`/`tmux` → `liveness`/`cleanup` → `sessions`/`membership`/`providers` → `snapshot`）。
- **`actions/`**——不属于 `data/` 的操作（resume/take-over/cleanup 执行/剪贴板），只消费 `data/` 产出的 typed 结果。
- **`views/`**——每个 tab 一组 urwid widget，共享 `_base.py::ListTabView` 的 walker/overlay/footer 管道（详见架构文档「view contract」）。
- **异步刷新**——单个 daemon 线程每代计算一份 `WorldSnapshot`，经 pipe 通知 main loop；TUI action 经 `ActionRunner` 单飞（详见架构文档「Async refresh」）。
- **resume/tmux**——脱离 UI loop 的 `ExitIntent` 边界（进程替换/`exec`），tmux-first 调度（ADR-0001/0006，详见架构文档「Resume」）。
- **liveness**——`data/liveness.py` 是唯一权威（详见架构文档「Liveness 与身份」）。
- **membership/cleanup**——ADR-0007 证据分层成员资格；cleanup 两种 key 语义（session-keyed / age-keyed），plan 冻结 + preview 优先（详见架构文档相应小节）。

## 约定

- **UI 字符串是简体中文**（通知、状态、按键提示、帮助屏）。**CLI 子命令输出是英文。** 添加字符串时遵循这一点。
- 可预期的外部失败由所属边界显式建模：允许降级的只读探测返回有类型的安全值；trust/settings、cleanup、refresh 和写操作保留 typed result、失败阶段与详情，并让 CLI/TUI 可见。不得用 broad `except Exception` 把 parser/invariant/编程错误伪装成空结果或成功。
- 破坏性 cleanup 总是先 preview：`_enter_preview` 在一个 `Overlay` 中显示目标，`_confirm_cleanup` 在第二次 `Enter` 时执行。
- Config 是 `config.py` 中单一的全局 `cfg = Config()`；测试通过 monkeypatch `cfg` 属性来覆盖路径（例如 `cfg.claude_home`、`cfg.claude_json`）。
- **不硬编码机器专属路径**：产品源码（`src/`）不得内联 `/home/...` 之类的绝对路径；CI 与本地预发布序列都跑 `grep -rn --include='*.py' '/home/' src/`（见 `## 命令`），必须返回空。
- **架构不变量**（详见 `docs/architecture.md`）：
  - Import 方向：`views` 只从 `data`/`actions` import；`data`/`actions` 绝不向上 import；`data/` 内部的 bottom→top DAG 单向、无环。
  - `config.py` 的全局 `cfg` 是唯一的路径权威——绝不在别处内联拼接 `claude_home / "..."` 之类路径。
  - 绝不从 worker 线程改动 urwid widget；widget 变更只在 main loop（`apply_refresh`/`_on_pipe`）上进行。
  - 主键是 `sessionId`，绝非 pid；current session（启动了 csctl 的那个 session）受保护，不能被 resume/terminate/prune。
  - 无法确定 current 时（无 `/proc`，R10 降级），破坏性操作（terminate/delete/clean/stop/remove）一律拒绝，不得冒险执行。
  - Cleanup 删除 ⊆ preview：`execute_*` 只能删除 `build_plan` 冻结的候选，且执行时必须对每一项用新鲜保护数据重新校验。
  - 任何 operator contract 都不得教人保存或执行针对快照 PID 的裸终止命令；接管统一走 `session_ops.take_over_result`。

## 发布与 CI

完整维护者指南：`docs/releasing.md`。不那么显然的点：

- **版本号单一来源**于 `src/cc_session_control/__init__.py`（`pyproject.toml` 经 setuptools dynamic 派生它）。只能通过 `python scripts/bump_version.py {patch|minor|major}` 或 `--set X.Y.Z` 步进——它只编辑那一个文件。然后加一条 `CHANGELOG.md` 条目。
- **候选先过 CI，tag 后发布。** 版本提交先进入 `origin/master`，等该同一 SHA 的 `CI` 成功后，才创建并单独推送匹配 `__version__` 的带注解 `vX.Y.Z` tag。tag 会触发 `.github/workflows/release.yml` 再跑检查、构建及 wheel/sdist smoke，并**经 Trusted Publishing 发布到 PyPI**（GitHub environment `pypi`，OIDC——不存储 API token）。发布 tag 不移动、不复用；失败修复使用下一个 patch。PyPI trusted publisher 已配置（owner `dzshzx`，repo `cc-session-control`，workflow `release.yml`，env `pypi`）。
- **CI**（`.github/workflows/ci.yml`）在每次推送到 `master` 和 PR 时运行相同的 测试 + 路径硬编码检查（见 `## 约定`）+ 构建 + smoke 关卡。
- **TestPyPI dry run**（`.github/workflows/release-testpypi.yml`）是一个手动 `workflow_dispatch`，发布到 TestPyPI（env `testpypi`）而不触碰真实索引——真正打 tag 前的可选彩排。
- **Gotchas：** 已发布的版本是不可变的——绝不覆盖它，而是 bump 到下一个 patch。`dist/` 被 gitignore；本地的预发布序列镜像该 workflow（`uv run --extra dev pytest tests/`、路径硬编码检查（`## 约定`）、`uv build --no-sources`、wheel/sdist 的 `csctl --version`、`uvx twine check dist/*`）。发布后立刻，`uv` 可能看不到新版本：PyPI 的 simple index 有 CDN 延迟，`uv` 自己还另有一层索引缓存。等 simple index 出现该版本后，`uv tool upgrade cc-session-control` 仍可能报 `Nothing to upgrade`——`uv tool upgrade` **没有** `--refresh` 这个 flag，且 `--reinstall`（号称 implies `--refresh`）实测也不够；打爆缓存要用 `uv tool upgrade cc-session-control --reinstall --no-cache`（v0.8.8 实测，2026-08-13）。绝不用 `==X.Y.Z` 固定版本绕过它。
