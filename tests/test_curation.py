"""Tests for the operator curation store (ADR-0007).

The store is the one csctl-OWNED membership source: typed reads that never
conflate absence with failure, and locked atomic writes that preserve keys
csctl does not own and never clobber a broken file.
"""

from __future__ import annotations

import json

from cc_session_control.data.curation import (
    CurationState,
    CurationWriteState,
    read_curation,
    set_hidden,
    set_pinned,
)


def test_read_missing_is_an_empty_non_failure(tmp_path):
    result = read_curation(tmp_path / "projects.json")

    assert result.state is CurationState.MISSING
    assert result.available
    assert result.pinned == frozenset() and result.hidden == frozenset()


def test_read_valid_exposes_both_lists_normalized(tmp_path):
    store = tmp_path / "projects.json"
    store.write_text(json.dumps({"pinned": ["/a/proj/"], "hidden": ["/b"], "other": 1}))

    result = read_curation(store)

    assert result.state is CurationState.AVAILABLE
    assert result.pinned == frozenset({"/a/proj"})  # trailing slash normalized
    assert result.hidden == frozenset({"/b"})


def test_read_malformed_and_invalid_are_distinct_failures(tmp_path):
    store = tmp_path / "projects.json"
    store.write_text("{broken")
    assert read_curation(store).state is CurationState.MALFORMED

    store.write_text(json.dumps({"pinned": "not-a-list"}))
    result = read_curation(store)
    assert result.state is CurationState.INVALID
    assert not result.available

    store.write_text(json.dumps({"hidden": ["relative/path"]}))
    assert read_curation(store).state is CurationState.INVALID


def test_set_pinned_creates_store_and_round_trips(tmp_path):
    store = tmp_path / "nested" / "projects.json"

    result = set_pinned(store, "/a/proj/", True)

    assert result.state is CurationWriteState.UPDATED
    read = read_curation(store)
    assert read.pinned == frozenset({"/a/proj"})  # normpath'd on write
    assert read.hidden == frozenset()


def test_pin_unhides_and_hide_unpins(tmp_path):
    store = tmp_path / "projects.json"
    set_hidden(store, "/a", True)
    set_pinned(store, "/a", True)
    read = read_curation(store)
    assert read.pinned == frozenset({"/a"})
    assert read.hidden == frozenset()

    set_hidden(store, "/a", True)
    read = read_curation(store)
    assert read.pinned == frozenset()
    assert read.hidden == frozenset({"/a"})


def test_noop_write_reports_unchanged(tmp_path):
    store = tmp_path / "projects.json"
    set_pinned(store, "/a", True)

    result = set_pinned(store, "/a", True)

    assert result.state is CurationWriteState.UNCHANGED
    assert result.success


def test_writes_preserve_foreign_keys(tmp_path):
    store = tmp_path / "projects.json"
    store.write_text(json.dumps({"operator-note": "keep me", "pinned": ["/b"]}))

    set_pinned(store, "/a", True)

    document = json.loads(store.read_text())
    assert document["operator-note"] == "keep me"
    assert document["pinned"] == ["/a", "/b"]  # sorted


def test_write_refuses_a_broken_store_without_clobbering(tmp_path):
    store = tmp_path / "projects.json"
    store.write_text("{broken")

    result = set_pinned(store, "/a", True)

    assert result.state is CurationWriteState.FAILED
    assert result.failure is not None and result.failure.value == "malformed"
    assert store.read_text() == "{broken"


def test_write_reports_directory_creation_failure(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    result = set_hidden(blocker / "projects.json", "/a", True)

    assert result.state is CurationWriteState.FAILED
    assert result.failure is not None
    assert result.failure.value == "create-directory"
