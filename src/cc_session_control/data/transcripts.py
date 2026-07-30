"""Bottom-layer transcript inventory with typed source completeness."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..models import InventoryIssue

_NOISE = (
    "<command-message>",
    "<command-name>",
    "<command-args>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "caveat:",
)


#: Same (source, path, detail) record everywhere — one canonical issue type.
TranscriptIssue = InventoryIssue


@dataclass(frozen=True)
class TranscriptRecord:
    """Successfully parsed, cwd-bearing transcript metadata."""

    sid: str
    cwd: str
    path: str
    mtime: float
    title: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    prompts: int = 0
    hidden: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hidden", frozenset(self.hidden))


@dataclass(frozen=True)
class TranscriptInventory:
    """Transcript records, pathname sids, and source evidence for one fresh scan."""

    records: tuple[TranscriptRecord, ...] = ()
    issues: tuple[TranscriptIssue, ...] = ()
    path_sids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "path_sids", frozenset(self.path_sids))

    @property
    def complete(self) -> bool:
        return not self.issues

    @property
    def sids(self) -> frozenset[str]:
        return self.path_sids | frozenset(record.sid for record in self.records)


class _TranscriptJSONError(ValueError):
    """A relevant transcript line could not be decoded as JSON."""


def _directory_entries(
    path: str,
    *,
    missing_ok: bool,
) -> tuple[list[os.DirEntry[str]], TranscriptIssue | None]:
    try:
        entries = os.scandir(path)
    except FileNotFoundError:
        if missing_ok:
            return [], None
        return (
            [],
            TranscriptIssue(
                "session transcript inventory",
                path,
                "directory disappeared during transcript discovery",
            ),
        )
    except OSError as exc:
        return (
            [],
            TranscriptIssue("session transcript inventory", path, str(exc)),
        )
    try:
        with entries:
            return list(entries), None
    except OSError as exc:
        return (
            [],
            TranscriptIssue("session transcript inventory", path, str(exc)),
        )


def _transcript_paths(root: str) -> tuple[list[str], list[TranscriptIssue]]:
    """Enumerate ``projects/*/*.jsonl`` without hiding source-tree failures."""
    project_entries, root_issue = _directory_entries(root, missing_ok=True)
    issues = [root_issue] if root_issue is not None else []
    project_paths: list[str] = []
    for entry in project_entries:
        try:
            if entry.is_dir():
                project_paths.append(entry.path)
        except OSError as exc:
            issues.append(
                TranscriptIssue(
                    "session transcript inventory",
                    entry.path,
                    str(exc),
                )
            )

    paths: list[str] = []
    for project_path in project_paths:
        entries, issue = _directory_entries(project_path, missing_ok=False)
        if issue is not None:
            issues.append(issue)
            continue
        paths.extend(entry.path for entry in entries if entry.name.endswith(".jsonl"))
    return paths, issues


def _is_noise(text: str) -> bool:
    normalized = text.strip().lower()
    return (not normalized) or any(normalized.startswith(noise) for noise in _NOISE)


def _clean_text(text: str) -> str:
    text = " ".join(text.split())
    for marker in (
        "<system-reminder",
        "<command-message",
        "<command-name",
        "<command-args",
        "<local-command-",
    ):
        index = text.find(marker)
        if index != -1:
            text = text[:index]
    return text.strip()


def _json_object(line: str, line_number: int) -> dict[str, object] | None:
    try:
        document = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _TranscriptJSONError(
            f"line {line_number}: invalid JSON: {exc.msg}"
        ) from exc
    return document if isinstance(document, dict) else None


def _parse_transcript(path: str) -> TranscriptRecord | None:
    """Parse one transcript into cwd-bearing metadata."""
    sid = os.path.basename(path)[:-6]
    metadata = os.stat(path)

    cwd = title = last_prompt = first_prompt = ""
    hidden: set[str] = set()
    prompts = 0

    with open(path, encoding="utf-8") as file_handle:
        for line_number, line in enumerate(file_handle, start=1):
            if '"sdk-ts"' in line:
                hidden.add("sdk")
            if '"bridge-session"' in line:
                hidden.add("bridge")
            if not cwd and '"cwd"' in line:
                document = _json_object(line, line_number)
                candidate = document.get("cwd") if document is not None else None
                if isinstance(candidate, str) and candidate:
                    cwd = candidate
            if '"aiTitle"' in line:
                document = _json_object(line, line_number)
                candidate = document.get("aiTitle") if document is not None else None
                if isinstance(candidate, str) and candidate:
                    title = candidate
            if '"lastPrompt"' in line:
                document = _json_object(line, line_number)
                candidate = document.get("lastPrompt") if document is not None else None
                if isinstance(candidate, str) and candidate:
                    last_prompt = candidate
            if '"type":"user"' in line:
                document = _json_object(line, line_number)
                if document is None or document.get("type") != "user":
                    continue
                message = document.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    texts = [content]
                elif isinstance(content, list):
                    texts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        text = block.get("text")
                        if block.get("type") == "text" and isinstance(text, str):
                            texts.append(text)
                else:
                    texts = []
                texts = [text for text in texts if text.strip()]
                if texts:
                    prompts += 1
                    if not first_prompt:
                        for text in texts:
                            if _is_noise(text):
                                continue
                            cleaned = _clean_text(text)
                            if cleaned:
                                first_prompt = cleaned
                                break

    if not cwd:
        return None

    return TranscriptRecord(
        sid=sid,
        cwd=cwd,
        path=path,
        mtime=metadata.st_mtime,
        title=title,
        first_prompt=first_prompt,
        last_prompt="" if _is_noise(last_prompt) else last_prompt,
        prompts=prompts,
        hidden=frozenset(hidden),
    )


def load_inventory(root: str) -> TranscriptInventory:
    """Load all transcript records while retaining every source issue."""
    paths, issues = _transcript_paths(root)
    path_sids = frozenset(os.path.basename(path)[:-6] for path in paths)
    records: list[TranscriptRecord] = []
    for path in paths:
        try:
            record = _parse_transcript(path)
        except (OSError, UnicodeError, _TranscriptJSONError) as exc:
            issues.append(TranscriptIssue("session transcript", path, str(exc)))
            continue
        if record is not None:
            records.append(record)
    return TranscriptInventory(tuple(records), tuple(issues), path_sids)
