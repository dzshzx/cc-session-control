"""Version-plan identity and authorization checks for release tooling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
REPOSITORY_PATTERN = re.compile(r"(?:^|[:/])([^/:]+)/([^/]+?)(?:\.git)?$")
TAG_NAMESPACE = "v"


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = SEMVER_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(f"version {value!r} is not MAJOR.MINOR.PATCH")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class VersionPlan:
    repository: str
    namespace: str
    baseline: str
    target: str

    def payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "repository": self.repository,
            "versions": [
                {
                    "namespace": self.namespace,
                    "baseline": self.baseline,
                    "target": self.target,
                }
            ],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def summary(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


def repository_identity(repo_root: Path | None = None) -> str:
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    if github_repository.count("/") == 1:
        owner, repository = github_repository.split("/", 1)
        if owner and repository:
            return f"{owner}/{repository.removesuffix('.git')}".lower()

    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("origin remote URL cannot be read")
    match = REPOSITORY_PATTERN.search(result.stdout.strip())
    if match is None:
        raise ValueError("origin remote URL cannot be normalized to owner/repository")
    return f"{match.group(1)}/{match.group(2)}".lower()


def remote_versions(
    namespace: str = TAG_NAMESPACE, repo_root: Path | None = None
) -> list[Version]:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{namespace}*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"remote release versions cannot be read: {result.stderr.strip()}"
        )

    versions: set[Version] = set()
    prefix = f"refs/tags/{namespace}"
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1].endswith("^{}"):
            continue
        ref = fields[1]
        if not ref.startswith(prefix):
            continue
        value = ref.removeprefix(prefix)
        if SEMVER_PATTERN.fullmatch(value):
            versions.add(Version.parse(value))
    return sorted(versions)


def build_version_plan(
    target: str, namespace: str = TAG_NAMESPACE, *, repo_root: Path | None = None
) -> VersionPlan | None:
    target_version = Version.parse(target)
    candidates = [
        version
        for version in remote_versions(namespace, repo_root)
        if version != target_version
    ]
    if not candidates:
        return None
    return VersionPlan(
        repository=repository_identity(repo_root),
        namespace=namespace,
        baseline=str(max(candidates)),
        target=target,
    )


def authorization_kind(plan: VersionPlan) -> str:
    baseline = Version.parse(plan.baseline)
    target = Version.parse(plan.target)
    if target < baseline:
        return "downgrade"
    if (
        target.major == baseline.major
        and target.minor == baseline.minor
        and target.patch == baseline.patch + 1
    ):
        return "patch"
    return "confirmation"


def validate_execution(
    plan: VersionPlan | None,
    confirmed_summary: str | None,
    *,
    no_change: bool = False,
) -> None:
    if plan is None:
        raise ValueError(
            "remote release baseline is unknown; version execution is paused"
        )
    kind = authorization_kind(plan)
    if kind == "downgrade":
        raise ValueError(
            f"version downgrade is not allowed: {plan.baseline} -> {plan.target}"
        )
    expected = plan.summary()
    if confirmed_summary is not None and confirmed_summary != expected:
        raise ValueError(
            "confirmed version plan does not match the current repository, baseline, and target"
        )
    if no_change:
        return
    if kind == "confirmation" and confirmed_summary is None:
        raise ValueError(
            "version change requires explicit approval; rerun with "
            f"--confirmed-version-plan {expected} after approval"
        )


def print_version_plan(plan: VersionPlan | None, target: str) -> None:
    print("== version_approval_plan ==")
    if plan is None:
        print("baseline=unknown")
        print(f"target={target}")
        print("authorization=blocked-unknown-baseline")
        return
    print(f"version_plan={plan.canonical_json()}")
    print(f"version_plan_summary={plan.summary()}")
    print(f"authorization={authorization_kind(plan)}")


def tag_approval_summary(tag: str) -> str | None:
    result = subprocess.run(
        ["git", "cat-file", "tag", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"release tag cannot be read: {result.stderr.strip()}")
    trailers = re.findall(r"^Version-Approval:.*$", result.stdout, re.MULTILINE)
    if len(trailers) > 1:
        raise ValueError("release tag has more than one Version-Approval trailer")
    if not trailers:
        return None
    match = re.fullmatch(r"Version-Approval:\s*(sha256:[0-9a-f]{64})\s*", trailers[0])
    if match is None:
        raise ValueError("release tag has a malformed Version-Approval trailer")
    return match.group(1)
