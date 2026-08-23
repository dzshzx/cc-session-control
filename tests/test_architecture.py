"""Mechanical guards for the architecture invariants documented in
`CLAUDE.md` (`## 架构`) and `docs/architecture.md`: import direction between
`data`/`actions`/`views`, the `data/` internal bottom→top DAG, and single
path authority in `config.py`. Before this file, these were "docs + author
self-discipline" only — nothing failed a test if a change violated them.

Everything here is derived from `ast.parse` of `src/cc_session_control/**/*.py`
— no import of the package under test is required for the DAG/direction
checks, so a violation cannot accidentally "not run" because the violating
edge itself breaks collection.

TYPE_CHECKING handling: imports lexically inside an `if TYPE_CHECKING:`
guard never execute (doubly so here — every module using it also has
`from __future__ import annotations`, so the referenced names are never
even looked up at runtime). They are collected separately and EXEMPTED from
the direction/layer checks below. The one place this exemption is load-
bearing is `views/*` type-hinting `..app.App` for the `TabView` Protocol
methods: `app.py` imports `views` at runtime (to build `self.views`), so an
unguarded reverse import would be a real cycle; the TYPE_CHECKING guard is
what keeps it from being one. `test_views_app_backreference_is_type_checking_only`
asserts the guard is actually present, not just permitted.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from cc_session_control.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "cc_session_control"
PACKAGE = "cc_session_control"


def _iter_py_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_ROOT)
    parts = list(rel.parts)
    parts[-1] = parts[-1].removesuffix(".py")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(dotted: str, is_package: bool) -> str:
    if is_package:
        return dotted
    if "." not in dotted:
        return ""
    return dotted.rsplit(".", 1)[0]


def _module_file_exists(dotted: str) -> bool:
    """Whether `dotted` names an actual module/package file on disk — used
    to tell `from ..data import cleanup` (alias IS a submodule) apart from
    `from .liveness import LivenessSnapshot` (alias is a symbol, the
    dependency is on the module itself)."""
    rel = dotted.split(".")
    if rel[0] != PACKAGE:
        return False
    base = SRC_ROOT.joinpath(*rel)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _type_checking_node_ids(tree: ast.Module) -> set[int]:
    """id() of every Import/ImportFrom node lexically inside `if
    TYPE_CHECKING:` (matches both `TYPE_CHECKING` and `typing.TYPE_CHECKING`
    spellings), at any nesting depth."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_type_checking = (
            isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
        ) or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        if not is_type_checking:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    ids.add(id(inner))
    return ids


def _resolve_import_from(node: ast.ImportFrom, current_package: str) -> list[str]:
    if node.level == 0:
        base = node.module or ""
    else:
        parts = current_package.split(".") if current_package else []
        strip = node.level - 1
        parts = parts[: len(parts) - strip] if strip <= len(parts) else []
        base = ".".join(parts)
        if node.module:
            base = f"{base}.{node.module}" if base else node.module
    if not base:
        return []
    if not base.startswith(PACKAGE):
        return [base]
    targets = []
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        targets.append(candidate if _module_file_exists(candidate) else base)
    return targets


@dataclass
class ModuleImports:
    module: str
    runtime: set[str]
    type_checking: set[str]


def _collect(path: Path) -> ModuleImports:
    dotted = _module_name(path)
    is_package = path.name == "__init__.py"
    current_package = _package_of(dotted, is_package)
    tree = ast.parse(path.read_text(), filename=str(path))
    tc_ids = _type_checking_node_ids(tree)
    runtime: set[str] = set()
    type_checking: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = _resolve_import_from(node, current_package)
        else:
            continue
        bucket = type_checking if id(node) in tc_ids else runtime
        for target in targets:
            if target == PACKAGE or target.startswith(f"{PACKAGE}."):
                bucket.add(target)
    return ModuleImports(dotted, runtime, type_checking)


@pytest.fixture(scope="module")
def module_imports() -> dict[str, ModuleImports]:
    return {mi.module: mi for mi in (_collect(p) for p in _iter_py_files())}


# --- (a) import direction: data/actions never -> views/app; views only <- data/actions/models/config/views ---


def test_data_and_actions_never_import_views_or_app(
    module_imports: dict[str, ModuleImports],
) -> None:
    violations = []
    for mod, mi in module_imports.items():
        if not (
            mod.startswith(f"{PACKAGE}.data") or mod.startswith(f"{PACKAGE}.actions")
        ):
            continue
        for target in mi.runtime:
            if target == f"{PACKAGE}.app" or target.startswith(f"{PACKAGE}.views"):
                violations.append(f"{mod} -> {target}")
    assert not violations, (
        "data/ and actions/ must never import views/app at runtime "
        "(CLAUDE.md ## 架构):\n" + "\n".join(sorted(violations))
    )


_VIEWS_ALLOWED_PREFIXES = (f"{PACKAGE}.data", f"{PACKAGE}.actions", f"{PACKAGE}.views")
_VIEWS_ALLOWED_EXACT = {f"{PACKAGE}.models", f"{PACKAGE}.config"}


def test_views_only_import_data_actions_models_config_or_views(
    module_imports: dict[str, ModuleImports],
) -> None:
    """rg of the current tree confirms views/ already only imports
    data/actions/models/config/views(self) + urwid/stdlib at runtime — no
    discrepancy from the CLAUDE.md wording was found, so this is the
    invariant as stated, not a widened set."""
    violations = []
    for mod, mi in module_imports.items():
        if not mod.startswith(f"{PACKAGE}.views"):
            continue
        for target in mi.runtime:
            if target in _VIEWS_ALLOWED_EXACT:
                continue
            if any(
                target == p or target.startswith(f"{p}.")
                for p in _VIEWS_ALLOWED_PREFIXES
            ):
                continue
            violations.append(f"{mod} -> {target}")
    assert not violations, (
        "views/ may only import data/actions/models/config/views (runtime):\n"
        + "\n".join(sorted(violations))
    )


def test_views_app_backreference_is_type_checking_only(
    module_imports: dict[str, ModuleImports],
) -> None:
    referencing = [
        mod
        for mod, mi in module_imports.items()
        if mod.startswith(f"{PACKAGE}.views") and f"{PACKAGE}.app" in mi.type_checking
    ]
    assert referencing, (
        "expected at least one views module to type-hint App under "
        "TYPE_CHECKING (views/_base.py etc.) — if this list is empty the "
        "exemption above is untested, not just unused"
    )
    for mod in referencing:
        assert f"{PACKAGE}.app" not in module_imports[mod].runtime, (
            f"{mod} imports app at runtime as well as under TYPE_CHECKING "
            "— that IS a real cycle, not an annotation-only reference"
        )


# --- (b) data/ internal bottom -> top DAG (docs/architecture.md "## 架构" data/ 分层) ---

_BOTTOM, _MIDDLE, _TOP, _SNAPSHOT, _REFRESH = 1, 2, 3, 4, 5

DATA_MODULE_LAYERS: dict[str, int] = {
    f"{PACKAGE}.data": _BOTTOM,  # empty __init__, no edges either way
    # bottom: pure IO + parsing, no data/ dependents besides each other
    f"{PACKAGE}.data.proc": _BOTTOM,
    f"{PACKAGE}.data.transcripts": _BOTTOM,
    f"{PACKAGE}.data.registry": _BOTTOM,
    f"{PACKAGE}.data.tmux_outcomes": _BOTTOM,
    f"{PACKAGE}.data.tmux": _BOTTOM,
    f"{PACKAGE}.data.atomic_write": _BOTTOM,
    f"{PACKAGE}.data.project_settings": _BOTTOM,
    f"{PACKAGE}.data.curation": _BOTTOM,
    f"{PACKAGE}.data.provider_config": _BOTTOM,
    f"{PACKAGE}.data.removal": _BOTTOM,
    # middle: liveness authority + cleanup policy/execution
    f"{PACKAGE}.data.liveness": _MIDDLE,
    f"{PACKAGE}.data.cleanup": _MIDDLE,
    f"{PACKAGE}.data.age_cleanup": _MIDDLE,
    f"{PACKAGE}.data.cleanup_anchors": _MIDDLE,
    # top: assembly (Session/Project projection, membership, provider layer)
    f"{PACKAGE}.data.sessions": _TOP,
    f"{PACKAGE}.data.membership": _TOP,
    f"{PACKAGE}.data.providers": _TOP,
    f"{PACKAGE}.data.providers.base": _TOP,
    f"{PACKAGE}.data.providers.claude": _TOP,
    f"{PACKAGE}.data.providers.codex": _TOP,
    f"{PACKAGE}.data.providers.codex_hosted": _TOP,
    f"{PACKAGE}.data.providers.codex_rollout": _TOP,
    f"{PACKAGE}.data.providers.codex_source": _TOP,
    f"{PACKAGE}.data.providers.codex_trust": _TOP,
    f"{PACKAGE}.data.providers.kimi": _TOP,
    f"{PACKAGE}.data.providers.opencode": _TOP,
    f"{PACKAGE}.data.providers.argv_live": _TOP,
    # topmost: combines liveness/membership/providers/sessions/tmux into one WorldSnapshot
    f"{PACKAGE}.data.snapshot": _SNAPSHOT,
    # above even snapshot: the only module in data/ that imports snapshot
    f"{PACKAGE}.data.refresh": _REFRESH,
}


def test_every_data_module_is_classified(
    module_imports: dict[str, ModuleImports],
) -> None:
    actual = {m for m in module_imports if m.startswith(f"{PACKAGE}.data")}
    expected = set(DATA_MODULE_LAYERS)
    missing = actual - expected
    stale = expected - actual
    assert not missing, (
        f"new data/ module(s) not classified in DATA_MODULE_LAYERS: {sorted(missing)}"
    )
    assert not stale, (
        f"DATA_MODULE_LAYERS references module(s) that no longer exist: {sorted(stale)}"
    )


def test_data_layers_only_import_same_or_lower(
    module_imports: dict[str, ModuleImports],
) -> None:
    violations = []
    for mod, layer in DATA_MODULE_LAYERS.items():
        for target in module_imports[mod].runtime:
            if target == mod or not target.startswith(f"{PACKAGE}.data"):
                continue  # self, or outside the data/ DAG (config/models/etc — always fine)
            target_layer = DATA_MODULE_LAYERS.get(target)
            if target_layer is None:
                violations.append(
                    f"{mod} -> {target} (target not in DATA_MODULE_LAYERS)"
                )
            elif target_layer > layer:
                violations.append(
                    f"{mod} (layer {layer}) -> {target} (layer {target_layer})"
                )
    assert not violations, (
        "data/ module imports a HIGHER layer (bottom->top DAG must be "
        "one-directional):\n" + "\n".join(sorted(violations))
    )


def test_data_import_graph_has_no_cycles(
    module_imports: dict[str, ModuleImports],
) -> None:
    graph = {
        mod: {
            t
            for t in module_imports[mod].runtime
            if t.startswith(f"{PACKAGE}.data") and t != mod
        }
        for mod in DATA_MODULE_LAYERS
    }
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = stack[stack.index(node) :] + [node]
            pytest.fail("cycle in data/ import graph: " + " -> ".join(cycle))
        visiting.add(node)
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            dfs(nxt)
        stack.pop()
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        dfs(node)


# --- (c) single path authority: cfg root-path + literal joins only in config.py ---


def _cfg_path_attribute_names() -> set[str]:
    """Every attribute of `Config` whose VALUE is a `Path` — derived from a
    live instance rather than hardcoded, so a newly added `cfg.foo_dir`
    property is covered automatically instead of silently exempt."""
    instance = Config()
    return {
        name
        for name in dir(instance)
        if not name.startswith("_") and isinstance(getattr(instance, name), Path)
    }


def _cfg_join_violations(path: Path, banned_attrs: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and isinstance(left.value, ast.Name)
            and left.value.id == "cfg"
            and left.attr in banned_attrs
        ):
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{node.lineno}: cfg.{left.attr} / ...")
    return violations


def test_cfg_root_path_joins_only_in_config_py() -> None:
    """CLAUDE.md ## 约定: `config.py` 的全局 `cfg` 是唯一的路径权威——绝不在
    别处内联拼接 `claude_home / "..."` 之类路径. Covers every Path-valued
    `cfg.*` attribute (claude_home/kimi_home/codex_home/opencode_home/
    sessions_dir/projects_root and beyond — see `_cfg_path_attribute_names`),
    not just the 6 named in the brief.

    Does NOT flag `self.home / "sessions"` in `data/providers/codex.py`
    (~line 548): that is a per-INSTANCE codex path (`CodexProvider.home`,
    ADR-0008 multi-instance), never routed through the `cfg` singleton, so
    it is outside this rule's scope by construction (the AST match requires
    the base name to be `cfg`). Each such literal ("sessions",
    "archived_sessions") appears exactly once in that file, so there is no
    second-event duplication to collapse into a module constant either.
    """
    banned_attrs = _cfg_path_attribute_names()
    config_py = PACKAGE_ROOT / "config.py"
    violations: list[str] = []
    for path in _iter_py_files():
        if path == config_py:
            continue
        violations.extend(_cfg_join_violations(path, banned_attrs))
    assert not violations, (
        "cfg root-path + literal join found outside config.py — add a "
        "named Config property/method instead:\n" + "\n".join(violations)
    )
