from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import bossmode.bootstrap as bootstrap
from bossmode.bootstrap import (
    NEXT_PROMPT,
    BootstrapError,
    install_project_skill,
    packaged_skill_bytes,
)
from bossmode.cli import main


def test_packaged_skill_matches_canonical_source() -> None:
    source_root = Path(__file__).parents[1]
    source_skill = source_root / ".agents" / "skills" / "bossmode" / "SKILL.md"
    assert packaged_skill_bytes() == source_skill.read_bytes()
    assert NEXT_PROMPT in (source_root / "README.md").read_text()
    assert b"docs/agent-workflow.md" not in packaged_skill_bytes()


def test_project_skill_install_is_idempotent(tmp_path: Path) -> None:
    first = install_project_skill(tmp_path)
    installed = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"

    assert first["status"] == "installed"
    assert first["skill_version"] == "0.1.0"
    assert first["skill_path"] == str(installed)
    assert first["next_prompt"] == NEXT_PROMPT
    assert installed.read_bytes() == packaged_skill_bytes()

    second = install_project_skill(tmp_path)
    assert second["status"] == "already_installed"
    assert installed.read_bytes() == packaged_skill_bytes()


def test_cli_init_refuses_to_overwrite_conflicting_skill(tmp_path: Path, capsys) -> None:
    installed = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("user-owned instructions\n")

    assert main(["install-skill", "--project-dir", str(tmp_path)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": f"refuse to overwrite conflicting skill: {installed}"}
    assert installed.read_text() == "user-owned instructions\n"


def test_cli_init_refuses_symlinked_skill_parent(tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agents").symlink_to(outside, target_is_directory=True)

    assert main(["install-skill", "--project-dir", str(tmp_path)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error": f"refuse to install through symlinked directory: {tmp_path / '.agents'}"
    }
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("project_kind", ["missing", "file"])
def test_project_skill_install_requires_existing_directory(
    tmp_path: Path, project_kind: str
) -> None:
    project = tmp_path / "project"
    if project_kind == "file":
        project.write_text("not a directory")

    with pytest.raises(
        BootstrapError, match="project (directory does not exist|path is not a directory)"
    ):
        install_project_skill(project)


@pytest.mark.parametrize("conflict_kind", ["parent_file", "skill_directory", "skill_symlink"])
def test_project_skill_install_rejects_non_regular_conflicts(
    tmp_path: Path, conflict_kind: str
) -> None:
    agents = tmp_path / ".agents"
    destination = agents / "skills" / "bossmode" / "SKILL.md"
    if conflict_kind == "parent_file":
        agents.write_text("not a directory")
        message = "skill parent path is not a directory"
    elif conflict_kind == "skill_directory":
        destination.mkdir(parents=True)
        message = "skill path is not a regular file"
    else:
        target = tmp_path / "user-skill.md"
        target.write_text("user-owned instructions")
        destination.parent.mkdir(parents=True)
        destination.symlink_to(target)
        message = "refuse to replace symlinked skill"

    with pytest.raises(BootstrapError, match=message):
        install_project_skill(tmp_path)


def test_project_skill_install_rejects_parent_swap_without_writing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    detached = tmp_path / "detached-agents"
    original_link = os.link

    def swap_parent_before_publish(*args, **kwargs) -> None:
        (tmp_path / ".agents").rename(detached)
        (tmp_path / ".agents").symlink_to(outside, target_is_directory=True)
        original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", swap_parent_before_publish)

    with pytest.raises(BootstrapError, match="skill path changed during installation"):
        install_project_skill(tmp_path)

    assert list(outside.iterdir()) == []
    detached_skill = detached / "skills" / "bossmode" / "SKILL.md"
    assert detached_skill.read_bytes() == packaged_skill_bytes()
    assert list(detached_skill.parent.glob(".SKILL.md.tmp-*")) == []


def test_project_skill_install_rejects_final_replacement_before_post_link_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    attacker_bytes = b"attacker replacement\n"
    original_link = os.link

    def replace_after_link(*args, **kwargs) -> None:
        original_link(*args, **kwargs)
        destination.unlink()
        destination.write_bytes(attacker_bytes)

    monkeypatch.setattr(os, "link", replace_after_link)

    with pytest.raises(BootstrapError, match="skill path changed during installation"):
        install_project_skill(tmp_path)

    assert destination.read_bytes() == attacker_bytes


def test_project_skill_install_rejects_final_swap_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_project_skill(tmp_path)
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    saved = tmp_path / "saved-skill.md"
    outside = tmp_path / "outside-skill.md"
    outside.write_bytes(packaged_skill_bytes())
    original_read = os.read
    swapped = False

    def swap_final_before_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            destination.rename(saved)
            destination.symlink_to(outside)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", swap_final_before_read)

    with pytest.raises(BootstrapError, match="skill path changed during installation"):
        install_project_skill(tmp_path)

    assert destination.is_symlink()
    assert saved.read_bytes() == packaged_skill_bytes()
    assert outside.read_bytes() == packaged_skill_bytes()


def test_project_skill_install_rejects_final_replacement_after_post_link_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    attacker_bytes = b"attacker replacement\n"
    original_verify = bootstrap._verify_path_binding

    def replace_before_binding(parent_descriptor, verified_destination, metadata) -> None:
        destination.unlink()
        destination.write_bytes(attacker_bytes)
        original_verify(parent_descriptor, verified_destination, metadata)

    monkeypatch.setattr(bootstrap, "_verify_path_binding", replace_before_binding)

    with pytest.raises(BootstrapError, match="skill path changed during installation"):
        install_project_skill(tmp_path)

    assert destination.read_bytes() == attacker_bytes


def test_concurrent_identical_installs_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_link = os.link
    winner_ready = threading.Event()
    release_winner = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []

    def pause_first_publisher(*args, **kwargs) -> None:
        if threading.current_thread().name == "first-installer":
            winner_ready.set()
            assert release_winner.wait(timeout=5)
        original_link(*args, **kwargs)

    def install() -> None:
        try:
            results.append(install_project_skill(tmp_path)["status"])
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(os, "link", pause_first_publisher)
    first = threading.Thread(target=install, name="first-installer")
    first.start()
    assert winner_ready.wait(timeout=5)
    install()
    release_winner.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert errors == []
    assert sorted(results) == ["already_installed", "installed"]
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    assert destination.read_bytes() == packaged_skill_bytes()
    assert list(destination.parent.glob(".SKILL.md.tmp-*")) == []


def test_cli_init_translates_filesystem_error_to_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    original_open = os.open

    def deny_project_open(path, *args, **kwargs):
        if path == tmp_path:
            raise PermissionError("permission denied by test")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_project_open)

    assert main(["install-skill", "--project-dir", str(tmp_path)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"].startswith(f"cannot open project directory {tmp_path}:")


def test_binding_failure_never_deletes_substituted_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    published = tmp_path / "published-skill.md"
    user_owned = tmp_path / "user-owned.md"
    user_owned.write_text("user-owned instructions\n")
    original_verify = bootstrap._verify_path_binding

    def substitute_before_verify(parent_descriptor, verified_destination, metadata) -> None:
        destination.rename(published)
        user_owned.rename(destination)
        original_verify(parent_descriptor, verified_destination, metadata)

    monkeypatch.setattr(bootstrap, "_verify_path_binding", substitute_before_verify)

    with pytest.raises(BootstrapError, match="skill path changed during installation"):
        install_project_skill(tmp_path)

    assert destination.read_text() == "user-owned instructions\n"
    assert published.read_bytes() == packaged_skill_bytes()


def test_close_failure_does_not_mask_original_bootstrap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".agents" / "skills" / "bossmode" / "SKILL.md"
    destination.mkdir(parents=True)
    parent_descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    original_close = os.close
    close_calls = 0

    def report_first_close_failure(descriptor: int) -> None:
        nonlocal close_calls
        original_close(descriptor)
        close_calls += 1
        if close_calls == 1:
            raise OSError("simulated close failure")

    monkeypatch.setattr(os, "close", report_first_close_failure)

    try:
        with pytest.raises(BootstrapError, match="skill path is not a regular file") as captured:
            bootstrap._read_at(parent_descriptor, destination, packaged_skill_bytes())
    finally:
        original_close(parent_descriptor)

    assert any("cannot close" in note for note in captured.value.__notes__)
