from __future__ import annotations

import json
import shutil

import pytest

from bossmode.artifacts import (
    MAX_ARTIFACTS_PER_RUN,
    MAX_TURN_RESULT_BYTES,
    ArtifactError,
    ArtifactRecord,
    CentralArtifactStore,
    canonical_result_digest,
    validate_relative_artifact_path,
)


def test_artifact_record_serializes_and_rejects_non_string_paths():
    record = ArtifactRecord("a.txt", "report", "central-copy", "abc", 3)
    assert record.to_dict() == {
        "path": "a.txt",
        "kind": "report",
        "disposition": "central-copy",
        "digest": "abc",
        "size_bytes": 3,
    }
    with pytest.raises(ArtifactError, match="must be a string"):
        validate_relative_artifact_path(123)  # type: ignore[arg-type]


def test_validate_relative_artifact_path_accepts_clean_paths():
    assert validate_relative_artifact_path("foo/bar.txt") == "foo/bar.txt"
    assert validate_relative_artifact_path("src/module/code.py") == "src/module/code.py"
    assert validate_relative_artifact_path("README.md") == "README.md"
    assert validate_relative_artifact_path("docs/spec.json") == "docs/spec.json"


@pytest.mark.parametrize(
    "invalid_path,match",
    [
        ("", "cannot be empty"),
        ("   ", "cannot be empty"),
        ("/etc/passwd", "repository-relative, not absolute"),
        ("\\windows\\path", "repository-relative, not absolute"),
        ("foo/../bar", "directory traversal"),
        ("../bar", "directory traversal"),
        (".git/config", "transient or internal directory"),
        (".claude/worktrees/sub", "transient or internal directory"),
        ("tmp/scratch.txt", "transient or internal directory"),
        ("temp/scratch.txt", "transient or internal directory"),
        ("worktrees/sub/file.txt", "transient or internal directory"),
        ("foo/worktree_a/file.txt", "transient or internal directory"),
    ],
)
def test_validate_relative_artifact_path_rejects_invalid(invalid_path, match):
    with pytest.raises(ArtifactError, match=match):
        validate_relative_artifact_path(invalid_path)


def test_canonical_result_digest_is_deterministic():
    payload1 = {
        "turn_id": "turn_123",
        "outcome": "succeeded",
        "summary": "done",
        "artifacts": [{"path": "a.txt", "kind": "file", "disposition": "accepted-commit"}],
    }
    payload2 = {
        "artifacts": [{"kind": "file", "path": "a.txt", "disposition": "accepted-commit"}],
        "summary": "done",
        "outcome": "succeeded",
        "turn_id": "turn_123",
    }
    digest1 = canonical_result_digest(payload1)
    digest2 = canonical_result_digest(payload2)
    assert digest1 == digest2
    assert len(digest1) == 64


def test_central_artifact_store_adopt_and_read(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()

    source_file = worktree_dir / "docs" / "output.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("Hello from worktree artifact", encoding="utf-8")

    store = CentralArtifactStore(store_dir)
    record = store.adopt_file_to_central(
        "docs/output.txt",
        source_base_dir=worktree_dir,
    )

    assert isinstance(record, ArtifactRecord)
    assert record.path == "docs/output.txt"
    assert record.disposition == "central-copy"
    assert record.size_bytes == len("Hello from worktree artifact")

    # Read back from store
    data = store.read_bounded_bytes("docs/output.txt")
    assert data == b"Hello from worktree artifact"

    # Delete worktree and prove central storage survives
    shutil.rmtree(worktree_dir)
    data_after = store.read_bounded_bytes("docs/output.txt")
    assert data_after == b"Hello from worktree artifact"


def test_central_artifact_store_rejects_symlinks(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()

    target_outside = tmp_path / "secret.txt"
    target_outside.write_text("secret content")

    symlink_file = worktree_dir / "bad_link.txt"
    symlink_file.symlink_to(target_outside)

    store = CentralArtifactStore(store_dir)
    with pytest.raises(ArtifactError, match="cannot be a symlink"):
        store.adopt_file_to_central("bad_link.txt", source_base_dir=worktree_dir)


def test_central_artifact_store_rejects_parent_symlinks(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "file.txt").write_text("content")

    (worktree_dir / "link_dir").symlink_to(outside_dir)

    store = CentralArtifactStore(store_dir)
    with pytest.raises(ArtifactError, match="cannot be a symlink"):
        store.adopt_file_to_central("link_dir/file.txt", source_base_dir=worktree_dir)


def test_central_artifact_store_rejects_oversized_files(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()

    oversized_file = worktree_dir / "big.bin"
    oversized_file.write_bytes(b"x" * 1024)

    store = CentralArtifactStore(store_dir)
    with pytest.raises(ArtifactError, match="exceeds maximum size"):
        store.adopt_file_to_central("big.bin", source_base_dir=worktree_dir, max_bytes=512)


def test_validate_result_envelope_normalizes_source_paths_and_metadata(tmp_path):
    store = CentralArtifactStore(tmp_path / "central")
    payload = {
        "turn_id": "t1",
        "outcome": "succeeded",
        "summary": "done",
        "artifacts": [
            {
                "path": str(tmp_path / "report.json"),
                "kind": " report ",
                "digest": " abc ",
                "size_bytes": 4,
            }
        ],
    }
    result = store.validate_result_envelope(
        json.dumps(payload), expected_turn_id="t1", source_base_dir=tmp_path
    )
    assert result["artifacts"] == [
        {
            "path": "report.json",
            "kind": "report",
            "disposition": "accepted-commit",
            "digest": "abc",
            "size_bytes": 4,
        }
    ]


def test_validate_result_envelope_success():
    store = CentralArtifactStore("/tmp/unused")
    payload = {
        "turn_id": "turn_abc123",
        "run_id": "run_xyz456",
        "task_id": "task_111222",
        "prompt_digest": "abcdef123456",
        "registry_id": "registry_999",
        "accepted_head": "0123456789abcdef",
        "outcome": "succeeded",
        "summary": "Implemented feature successfully",
        "artifacts": [
            {"path": "src/app.py", "kind": "source_code", "disposition": "accepted-commit"},
            {"path": "reports/summary.json", "kind": "telemetry", "disposition": "central-copy"},
        ],
    }
    raw = json.dumps(payload).encode("utf-8")
    validated = store.validate_result_envelope(
        raw,
        expected_turn_id="turn_abc123",
        expected_run_id="run_xyz456",
        expected_task_id="task_111222",
        expected_prompt_digest="abcdef123456",
        expected_registry_id="registry_999",
        expected_accepted_head="0123456789abcdef",
        expected_summary="Implemented feature successfully",
    )
    assert validated["turn_id"] == "turn_abc123"
    assert len(validated["artifacts"]) == 2
    assert "result_digest" in validated
    assert len(validated["result_digest"]) == 64


@pytest.mark.parametrize(
    "tamper_field,tamper_value,expected_error",
    [
        ("turn_id", "wrong_turn", "turn result ID mismatch"),
        ("run_id", "wrong_run", "turn result run_id mismatch"),
        ("task_id", "wrong_task", "turn result task_id mismatch"),
        ("prompt_digest", "wrong_digest", "turn result prompt_digest mismatch"),
        ("registry_id", "wrong_reg", "turn result registry_id mismatch"),
        ("accepted_head", "wrong_head", "turn result accepted_head mismatch"),
        ("outcome", "failed", "must have outcome succeeded"),
        ("summary", "different summary", "does not match --summary"),
    ],
)
def test_validate_result_envelope_tampering_rejected(tamper_field, tamper_value, expected_error):
    store = CentralArtifactStore("/tmp/unused")
    payload = {
        "turn_id": "turn_abc123",
        "run_id": "run_xyz456",
        "task_id": "task_111222",
        "prompt_digest": "abcdef123456",
        "registry_id": "registry_999",
        "accepted_head": "0123456789abcdef",
        "outcome": "succeeded",
        "summary": "Summary text",
        "artifacts": [{"path": "file.txt", "kind": "file"}],
    }
    payload[tamper_field] = tamper_value
    raw = json.dumps(payload).encode("utf-8")

    with pytest.raises(ArtifactError, match=expected_error):
        store.validate_result_envelope(
            raw,
            expected_turn_id="turn_abc123",
            expected_run_id="run_xyz456",
            expected_task_id="task_111222",
            expected_prompt_digest="abcdef123456",
            expected_registry_id="registry_999",
            expected_accepted_head="0123456789abcdef",
            expected_summary="Summary text",
        )


def test_validate_result_envelope_rejects_non_object_and_missing_fields():
    store = CentralArtifactStore("/tmp/unused")
    with pytest.raises(ArtifactError, match="must be a JSON object"):
        store.validate_result_envelope("[]", expected_turn_id="t1")
    with pytest.raises(ArtifactError, match="missing fields"):
        store.validate_result_envelope('{"turn_id": "t1"}', expected_turn_id="t1")
    with pytest.raises(ArtifactError, match="artifacts must be a list"):
        store.validate_result_envelope(
            json.dumps(
                {"turn_id": "t1", "outcome": "succeeded", "summary": "done", "artifacts": {}}
            ),
            expected_turn_id="t1",
        )


def test_validate_result_envelope_rejects_markdown_code_fence():
    store = CentralArtifactStore("/tmp/unused")
    raw = (
        b'```json\n{"turn_id": "t1", "outcome": "succeeded", "summary": "s", "artifacts": []}\n```'
    )
    with pytest.raises(ArtifactError, match="contains markdown code fence"):
        store.validate_result_envelope(raw, expected_turn_id="t1")


def test_validate_result_envelope_rejects_oversized_payload():
    store = CentralArtifactStore("/tmp/unused")
    raw = b"x" * (MAX_TURN_RESULT_BYTES + 1)
    with pytest.raises(ArtifactError, match="exceeds maximum allowed size"):
        store.validate_result_envelope(raw, expected_turn_id="t1")


def test_validate_result_envelope_rejects_digest_tampering_and_limits_artifacts():
    store = CentralArtifactStore("/tmp/unused")
    payload = {
        "turn_id": "t1",
        "outcome": "succeeded",
        "summary": "done",
        "artifacts": [],
        "result_digest": "wrong",
    }
    with pytest.raises(ArtifactError, match="digest does not match"):
        store.validate_result_envelope(json.dumps(payload), expected_turn_id="t1")

    payload["result_digest"] = None
    payload["artifacts"] = [{"path": "a.txt", "kind": "file"}] * (MAX_ARTIFACTS_PER_RUN + 1)
    with pytest.raises(ArtifactError, match="exceeding maximum"):
        store.validate_result_envelope(json.dumps(payload), expected_turn_id="t1")


def test_secure_and_adopt_run_artifacts_rejects_special_files_and_custom_destination(tmp_path):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    (worktree_dir / "socket").mkdir()
    store = CentralArtifactStore(tmp_path / "central")
    with pytest.raises(ArtifactError, match="regular file"):
        store.secure_and_adopt_run_artifacts(
            [{"path": "socket", "kind": "file"}], source_base_dir=worktree_dir
        )

    source = worktree_dir / "source.txt"
    source.write_text("content")
    result = store.adopt_file_to_central(
        "source.txt", source_base_dir=worktree_dir, destination_relative_path="nested/out.txt"
    )
    assert result.path == "nested/out.txt"
    assert store.read_bounded_bytes("nested/out.txt") == b"content"


def test_secure_and_adopt_run_artifacts(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()

    commit_file = worktree_dir / "src" / "code.py"
    commit_file.parent.mkdir(parents=True)
    commit_file.write_text("print('code')")

    copy_file = worktree_dir / "data" / "result.json"
    copy_file.parent.mkdir(parents=True)
    copy_file.write_text('{"key": "val"}')

    store = CentralArtifactStore(store_dir)
    artifacts_input = [
        {"path": "src/code.py", "kind": "source_code", "disposition": "accepted-commit"},
        {"path": "data/result.json", "kind": "output_data", "disposition": "central-copy"},
    ]

    secured = store.secure_and_adopt_run_artifacts(
        artifacts_input,
        source_base_dir=worktree_dir,
    )

    assert len(secured) == 2
    assert secured[0]["path"] == "src/code.py"
    assert secured[0]["disposition"] == "accepted-commit"
    assert "digest" in secured[0]

    assert secured[1]["path"] == "data/result.json"
    assert secured[1]["disposition"] == "central-copy"
    assert "digest" in secured[1]

    # Verify central copy is present in central store
    assert (store_dir / "data" / "result.json").exists()
    assert (store_dir / "data" / "result.json").read_text() == '{"key": "val"}'


def test_artifact_store_rejects_prefix_collision(tmp_path):
    """Adoption must not confuse sibling names that share a string prefix."""
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    (worktree_dir / "reports").mkdir()
    (worktree_dir / "reports-evil").mkdir()
    (worktree_dir / "reports" / "ok.txt").write_text("ok")
    (worktree_dir / "reports-evil" / "bad.txt").write_text("bad")
    store = CentralArtifactStore(store_dir)
    # reports/ok.txt must resolve under reports, not bleed into reports-evil.
    data = store.read_bounded_bytes("reports/ok.txt", base_dir=worktree_dir)
    assert data == b"ok"
    # Central store adopts under the exact relative path; sibling prefix is distinct.
    store.adopt_file_to_central("reports/ok.txt", source_base_dir=worktree_dir)
    assert (store_dir / "reports" / "ok.txt").exists()
    assert not (store_dir / "reports-evil").exists()
    # Descriptor walk must not resolve reports-evil when asked for reports/ok.txt.
    # If containment used startswith, "reports-evil/bad.txt".startswith("reports")
    # would incorrectly pass; descriptor walk prevents that by construction.
    data2 = store.read_bounded_bytes("reports-evil/bad.txt", base_dir=worktree_dir)
    assert data2 == b"bad"


def test_artifact_store_rejects_fifo_and_device_special_files(tmp_path):
    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    fifo = worktree_dir / "pipe"
    try:
        import os

        os.mkfifo(fifo)
    except (AttributeError, OSError, NotImplementedError):
        pytest.skip("mkfifo not available")
    store = CentralArtifactStore(store_dir)
    with pytest.raises(ArtifactError, match="regular file"):
        store.read_bounded_bytes("pipe", base_dir=worktree_dir)
    with pytest.raises(ArtifactError, match="regular file"):
        store.adopt_file_to_central("pipe", source_base_dir=worktree_dir)


def test_artifact_store_swap_race_mitigated_by_held_descriptor(tmp_path):
    """Held parent descriptor keeps the original directory even after path swap."""
    import os

    store_dir = tmp_path / "central_store"
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    (worktree_dir / "a").mkdir()
    (worktree_dir / "a" / "file.txt").write_text("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("evil")
    store = CentralArtifactStore(store_dir)
    # First hold the parent descriptor before any swap.
    base = worktree_dir.resolve()
    parent_fd = store._open_directory_descriptor(base, ("a",), create=False)
    try:
        # Swap the path on disk while descriptor is held.
        (worktree_dir / "a").rename(tmp_path / "a_orig")
        (worktree_dir / "a").symlink_to(outside)
        # Reading via the held descriptor still sees the original file, not the symlink target.
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open("file.txt", flags, dir_fd=parent_fd)
        try:
            data = os.read(fd, 64)
        finally:
            os.close(fd)
        assert data == b"original"
        # But a fresh walk that re-resolves the path must reject the symlink parent.
        with pytest.raises(ArtifactError, match="cannot be a symlink"):
            store.read_bounded_bytes("a/file.txt", base_dir=worktree_dir)
    finally:
        os.close(parent_fd)
        if (worktree_dir / "a").is_symlink():
            (worktree_dir / "a").unlink()
            (tmp_path / "a_orig").rename(worktree_dir / "a")
        elif not (worktree_dir / "a").exists():
            (tmp_path / "a_orig").rename(worktree_dir / "a")


def test_validate_result_envelope_rejects_replayed_turn_id():
    store = CentralArtifactStore("/tmp/unused")
    payload = {
        "turn_id": "turn_right",
        "outcome": "succeeded",
        "summary": "done",
        "artifacts": [{"path": "a.txt", "kind": "file"}],
    }
    raw = json.dumps(payload).encode()
    # Replaying the same payload under a different expected turn_id must fail.
    with pytest.raises(ArtifactError, match="turn result ID mismatch"):
        store.validate_result_envelope(raw, expected_turn_id="turn_other")


def test_validate_result_envelope_rejects_absolute_path_outside_source(tmp_path):
    store = CentralArtifactStore(tmp_path / "central")
    # Absolute path outside source_base_dir must be rejected as repository-relative.
    payload = {
        "turn_id": "t1",
        "outcome": "succeeded",
        "summary": "done",
        "artifacts": [{"path": "/tmp/evil.txt", "kind": "file"}],
    }
    with pytest.raises(ArtifactError, match="repository-relative"):
        store.validate_result_envelope(
            json.dumps(payload), expected_turn_id="t1", source_base_dir=tmp_path
        )
