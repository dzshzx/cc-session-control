# ADR Decision Index

先读本表再开单个 ADR。取代/扩展细节以各文件 `Status:` 段为准（`tests/test_docs.py` 守住
「正文声明 supersedes/extends/amends X ⇒ X 的 Status 必须回指」）；本表只做导航。

| ADR | Status | 决定了什么 |
| --- | --- | --- |
| [ADR-0001](./0001-tmux-first-session-dispatch.md) | accepted 2026-07-06; placement → 0006, project-RC/后台 → 0009, Projects-Enter → 0005, hosted rows → 0010 | tmux 接回是主动词；Enter/t/R/f 语义 |
| [ADR-0002](./0002-atomic-refresh-and-single-flight-mutations.md) | accepted 2026-07-29; ledger → 0004, AgentJob → 0009 | 刷新世代原子、ActionRunner 单飞、widget 只在 main loop |
| [ADR-0003](./0003-upstream-settings-and-typed-diagnostics.md) | accepted 2026-07-29; ledger 面 → 0004, RC/settings 写 → 0009, 成员清单半边 → 0007 | effective trust 谓词（保留）、typed 诊断、不 broad except |
| [ADR-0004](./0004-surface-reduction-to-the-operator-core.md) | accepted 2026-07-30; RC/后台管理 → 0009 | headless 只剩 resume；prune/env/skill/rc/agents 移除 |
| [ADR-0005](./0005-multi-cli-provider-layer.md) | accepted 2026-08-04 (+amendment 2026-08-18 opencode); RC/后台 → 0009 | provider 注册表、typed caps、拒绝不模拟、三级 liveness 绑定 |
| [ADR-0006](./0006-unified-interactive-tmux-session.md) | accepted 2026-08-08; RC 放置 → 0009; prefix2 由 0011 扩展 | 单一 csctl tmux session，项目/会话 window，原地接入 |
| [ADR-0007](./0007-evidence-tier-project-membership.md) | accepted 2026-08-12; RC 相关条款 → 0009 | Pinned/Trusted/Observed 三层证据 + 卫生 + 取舍存储 |
| [ADR-0008](./0008-declared-cli-instances.md) | accepted 2026-08-13; 证据列条款 → 0009; 扩展 0005 | providers.json codex_homes 取代 CODEX_HOME |
| [ADR-0009](./0009-remove-rc-and-background-agent-management.md) | accepted 2026-08-13 | 移除 RC 管理与后台 agent 管理；只留证据读 |
| [ADR-0010](./0010-codex-app-server-hosted-sessions.md) | accepted 2026-08-17 | 托管态独立于 alive，所有变更动词拒绝 |
| [ADR-0011](./0011-mobile-tmux-switch-prefix.md) | accepted 2026-08-17; 扩展 0006 | managed session 的 C-a 第二前缀，C-a s 开 choose-tree |
| [ADR-0012](./0012-per-instance-launch-env-file.md) | accepted 2026-08-21; 扩展 0008 | 声明身份的 launch-only env_file |

## 决策链

- tmux 调度：0001 → 0006（单一 session）→ 0011（手机前缀）；0010 限定托管行不参与动词。
- 表面削减：0004（headless 只剩 resume、ledger 移除）→ 0009（RC/后台管理整体移除）。
- Provider：0005（多 CLI 层）→ 0008（多 codex 身份）→ 0012（身份级 env_file）；0005 的 2026-08-18 修正加入 opencode。
- 成员资格：0003（trust 谓词）→ 0007（证据分层成员）。
