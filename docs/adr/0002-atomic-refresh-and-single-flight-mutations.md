# Atomic refresh generations and single-flight TUI mutations

Status: accepted (2026-07-29)

csctl reads several independently changing local sources: transcripts,
registries, `/proc`, tmux, project settings, and its environment ledger. A
refresh that published each view as soon as its own scan finished could render
sessions, projects, cleanup counts, and agents from different points in time.
Running filesystem or subprocess mutations on urwid's thread would instead
freeze navigation and could let worker threads mutate widgets.

## Decision

### Refresh generations

- `data/refresh.py::RefreshCoordinator` owns refresh scheduling and worker
  state. One daemon worker builds a complete `RefreshBatch` from one
  `WorldSnapshot`; views do no refresh I/O and have no worker-written pending
  fields.
- A batch carries one monotonically increasing generation plus the snapshot,
  generation-local cleanup plan/counts, session stats, and activity-ordered projects.
  `App._on_pipe` is the only consumer. It runs on the urwid main loop and
  applies the same complete batch to every view.
- Refresh requests are coalesced. While a generation is running or waiting to
  be consumed, any number of requests reserve at most one follow-up
  generation. Refreshes therefore do not overlap or build an unbounded queue.
- An expected source `OSError` or incomplete required evidence becomes a typed
  `RefreshFailure`. The worker captures the session-agnostic age cleanup plan
  before deciding whether liveness or transcript evidence is complete. The
  main loop displays the failure, keeps every tab's complete world/session rows
  on the last good generation, and replaces cleanup state with that safe age
  projection. Session-keyed cleanup targets and anchors are cleared and its
  actions explicitly refuse as unavailable. Parser, invariant, and programming
  errors are not converted into an apparently successful empty world.
- Closing the coordinator rejects new requests and discards unpublished or
  late results. Coordinator state shared by requester, worker, and consumer is
  lock-protected; widgets and the last-applied generation counter are
  main-loop-owned.

### Stay-in-TUI mutations

- `actions/runner.py::ActionRunner` accepts at most one mutation until its
  result is consumed. A concurrent request receives `Busy(active_key)` and the
  operator sees that another action is in progress.
- A view passes its selected frozen domain model (`Session`/`AgentJob`)
  directly to the action adapter before submission — the models are already
  immutable, so no snapshot copy is needed. The worker receives no App,
  walker, selection, or urwid widget. It publishes an `ActionResult`
  (`success`, `partial`, `refused`, or `failure`); only the main-loop pipe
  callback updates notices or requests one follow-up refresh.
- Key-triggered external reads and preparations use that same runner and publish
  an `ActionCompletion[T]`. `App.submit_completion` associates its typed
  callback only after `Accepted`; Busy/Closed submissions cannot replace the
  active callback. The main-loop pipe callback clears the association before it
  applies confirm, overlay, notification, or exit-intent effects. Worker
  exceptions and App close discard the association.
- Expected operation failures are mapped to typed, operator-visible results.
  Unexpected exceptions remain visible through `threading.excepthook`; the
  runner still releases its single-flight state after the completion signal.
- Actions that must leave the TUI are not submitted to `ActionRunner`.
  A resume preparation may run there as a read, but its main-loop completion
  constructs the `ExitIntent`. Resume/attach/new-session variants remain
  `ExitIntent` values: the view asks `App` to exit, then the CLI runs the intent
  after urwid has stopped. This is the boundary for `exec` replacement and tmux
  client switching.

## Consequences

- A rendered screen is one coherent generation, or the previous known-good
  tab data plus a worker-built age-only cleanup projection whose session-keyed
  actions refuse, together with an explicit failure notice.
- Refresh I/O and stay-in-TUI mutations keep keyboard navigation responsive,
  while destructive mutations cannot race each other through the UI.
- A slow mutation and a refresh may overlap because they have different
  responsibilities. Mutation completion requests a fresh generation when its
  result says the world changed.
- New views implement `apply_refresh(batch)` and new stay-in-TUI mutations
  return `ActionResult`; neither may mutate widgets from a worker thread.
