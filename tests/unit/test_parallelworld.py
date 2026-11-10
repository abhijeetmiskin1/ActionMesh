from __future__ import annotations

import hashlib

import pytest

from actionmesh.agent.parallelworld import preview, reserve_apply
from actionmesh.agent.paths import Workspace
from actionmesh.providers.base import ProviderError


def test_preview_evaluates_plan_in_shadow_and_leaves_source_unchanged(tmp_path) -> None:
    (tmp_path / "release.txt").write_text("candidate build\n", encoding="utf-8")
    (tmp_path / "candidate.txt").write_text("artifact\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("review\n", encoding="utf-8")
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}

    record, summary = preview(Workspace(str(tmp_path)), [
        {"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"},
        {"op": "copy", "path": "candidate.txt", "to": "candidate-copy.txt"},
    ])

    assert summary["files_modified"] == 1
    assert summary["files_created"] == 1
    assert "-candidate build" in record.diff and "+ready build" in record.diff
    assert (tmp_path / "candidate-copy.txt").exists() is False
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()} == original


def test_stale_preview_rejected_on_hash_change(tmp_path) -> None:
    source = tmp_path / "release.txt"
    source.write_text("candidate\n", encoding="utf-8")
    record, _ = preview(Workspace(str(tmp_path)), [
        {"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"},
    ])
    source.write_text("external update\n", encoding="utf-8")

    with pytest.raises(ProviderError, match="stale preview"):
        reserve_apply(Workspace(str(tmp_path)), record.preview_id)
    assert source.read_text(encoding="utf-8") == "external update\n"


def test_preview_refuses_workspace_escape_and_symlink(tmp_path) -> None:
    (tmp_path / "inside.txt").write_text("safe", encoding="utf-8")
    with pytest.raises(ProviderError):
        preview(Workspace(str(tmp_path)), [
            {"op": "create", "path": "../escape.txt", "content": "no"},
        ])
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(outside)
        with pytest.raises(ProviderError, match="symlink"):
            preview(Workspace(str(tmp_path)), [
                {"op": "edit", "path": "inside.txt", "find": "safe", "replace": "changed"},
            ])
        assert outside.read_text(encoding="utf-8") == "outside"
    finally:
        outside.unlink(missing_ok=True)


def test_preview_refuses_second_apply_reservation(tmp_path) -> None:
    (tmp_path / "release.txt").write_text("candidate", encoding="utf-8")
    record, _ = preview(Workspace(str(tmp_path)), [
        {"op": "edit", "path": "release.txt", "find": "candidate", "replace": "ready"},
    ])
    reserved = reserve_apply(Workspace(str(tmp_path)), record.preview_id)
    assert reserved.apply_started
    with pytest.raises(ProviderError, match="already been submitted"):
        reserve_apply(Workspace(str(tmp_path)), record.preview_id)
