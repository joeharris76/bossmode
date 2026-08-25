from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from bossmode import registry as registry_module
from bossmode.cli import main
from bossmode.registry import Registry, RegistryError


@pytest.fixture
def registry(tmp_path):
    repository = _create_repository(tmp_path)
    return Registry(tmp_path / "control.db", repository_path=repository)


def _create_repository(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repository"
    remote = tmp_path / "origin.git"
    repository.mkdir()
    for arguments in (
        ["git", "init", "-b", "main", str(repository)],
        ["git", "-C", str(repository), "config", "user.name", "Test User"],
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
    ):
        subprocess.run(arguments, check=True, capture_output=True, text=True)
    (repository / "README").write_text("test\n")
    subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repository


def _writer(registry: Registry, branch: str, directory: str, worktree_id: str) -> dict:
    path = registry.repository_path / directory
    subprocess.run(
        ["git", "-C", str(registry.repository_path), "worktree", "add", "-b", branch, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "push", "-u", "origin", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "branch_name": branch,
        "base_sha": base_sha,
        "worktree_path": str(path),
        "worktree_id": worktree_id,
    }


def _head(writer: dict) -> str:
    return subprocess.run(
        ["git", "-C", writer["worktree_path"], "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_head(registry: Registry) -> str:
    return subprocess.run(
        ["git", "-C", str(registry.repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _setup(registry: Registry, tmp_path: Path, count: int = 3, *, start_managers: bool = True):
    approved_base_sha = _repo_head(registry)
    root = registry.create_task(
        title="Root",
        goal="Coordinate teams",
        success_criteria="All children pass",
        approved_base_sha=approved_base_sha,
    )
    teams = []
    children = []
    for team_number in range(2):
        team = registry.create_team(
            root["id"],
            name=f"team-{team_number}",
            manager_identity={"source": "native", "value": f"manager-{team_number}"},
            scope={"domain": team_number},
        )
        teams.append(team)
    for child_number in range(count):
        team = teams[child_number % 2]
        child = registry.create_child_task(
            root["id"],
            title=f"Child {child_number}",
            goal="Implement one disjoint slice",
            success_criteria="Slice is independently verified",
            team_id=team["id"],
            scope={"paths": [f"src/slice{child_number}"]},
        )
        child["_registry"] = registry
        children.append(child)
    managers = (
        [
            registry.start_manager_run(
                team["id"], identity={"source": "native", "value": f"manager-{index}"}
            )
            for index, team in enumerate(teams)
        ]
        if start_managers
        else []
    )
    return root, teams, children, managers


def _worker_spec(tmp_path: Path, child: dict, manager: dict, number: int, resource: object):
    del tmp_path
    registry = child["_registry"]
    return {
        "task_id": child["id"],
        "manager_run_id": manager["id"],
        "identity": {"source": "native", "value": f"worker-{number}"},
        "writer": _writer(registry, f"team/{number}", f"worktree-{number}", f"worktree-{number}"),
        "resources": [resource],
    }


def _complete_worker(
    registry: Registry, child: dict, manager: dict, number: int
) -> tuple[dict, dict]:
    worker = registry.start_worker_run(
        child["id"],
        manager_run_id=manager["id"],
        identity={"source": "native", "value": f"complete-worker-{number}"},
        writer=_writer(registry, f"complete/{number}", f"complete-{number}", f"complete-{number}"),
        resources=[f"complete-{number}.py"],
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        child["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": f"complete-reviewer-{number}"},
    )
    registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
    registry.add_evaluation(
        child["id"],
        run_id=worker["id"],
        evaluator=f"complete-reviewer-{number}",
        evaluator_run_id=reviewer["id"],
        passed=True,
        evidence="exact head reviewed",
        reviewed_head_sha=accepted_head,
    )
    return worker, reviewer


def _legacy_worker(registry: Registry, tmp_path: Path) -> tuple[dict, dict, dict, dict]:
    root, teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "legacy-worker"},
        writer=_writer(registry, "legacy/worker", "legacy-worker", "legacy-worker"),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="legacy worker complete",
        accepted_head_sha=accepted_head,
    )
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET repository_path = NULL, accepted_head_sha = NULL "
            "WHERE run_id = ?",
            (worker["id"],),
        )
    return root, teams[0], children[0], registry.get_run(worker["id"])


def test_reconcile_accepted_head_repairs_a_legacy_team_worker(registry, tmp_path):
    _root, _team, child, worker = _legacy_worker(registry, tmp_path)
    accepted_head = _head(worker["writer_identity"])

    reconciled = registry.reconcile_accepted_head(
        worker["id"],
        repository_path=registry.repository_path,
        accepted_head_sha=accepted_head,
        evidence="Repository, worktree, branch, and live current head verified",
    )

    assert reconciled["writer_identity"]["accepted_head_sha"] == accepted_head
    assert reconciled["writer_identity"]["repository_path"] == str(registry.repository_path)
    event = registry.get_task(child["id"])["events"][-1]
    assert event["event_type"] == "accepted_head_reconciled"
    assert "live current head verified" in event["evidence"]


def test_reconcile_accepted_head_requires_explicit_repository_path(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    with pytest.raises(RegistryError, match="requires a repository path"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=None,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_unrelated_supplied_repository(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    unrelated = _create_repository(tmp_path / "unrelated-supplied")
    with pytest.raises(RegistryError, match="Registry common repository"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=unrelated,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_non_root_supplied_repository(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    nested = registry.repository_path / "nested"
    nested.mkdir()
    with pytest.raises(RegistryError, match="live Git root"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=nested,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_preserves_matching_non_null_repository(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    recorded = str(registry.repository_path)
    supplied = registry.repository_path / ".." / registry.repository_path.name
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET repository_path = ? WHERE run_id = ?",
            (recorded, worker["id"]),
        )
    reconciled = registry.reconcile_accepted_head(
        worker["id"],
        repository_path=supplied,
        accepted_head_sha=_head(worker["writer_identity"]),
        evidence="matching recorded repository verified",
    )
    assert reconciled["writer_identity"]["repository_path"] == recorded


def test_reconcile_accepted_head_rejects_dirty_worktree(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    worktree = Path(worker["writer_identity"]["worktree_path"])
    (worktree / "dirty").write_text("uncommitted\n")
    with pytest.raises(RegistryError, match="worktree is dirty"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_race_has_one_winner(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    barrier = Barrier(2)

    def attempt(index: int):
        barrier.wait()
        try:
            return registry.reconcile_accepted_head(
                worker["id"],
                repository_path=registry.repository_path,
                accepted_head_sha=_head(worker["writer_identity"]),
                evidence=f"race attempt {index}",
            )
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (0, 1)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RegistryError) for result in results) == 1
    persisted = registry.get_run(worker["id"])["writer_identity"]
    assert persisted["repository_path"] == str(registry.repository_path)
    assert persisted["accepted_head_sha"] == _head(worker["writer_identity"])


def test_cli_run_finish_records_exact_accepted_head_for_team_worker(
    registry, tmp_path, capsys, monkeypatch
):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "cli-finish-worker"},
        writer=_writer(registry, "cli/finish", "cli-finish", "cli-finish"),
    )
    accepted_head = _head(worker["writer_identity"])
    monkeypatch.chdir(registry.repository_path)

    assert (
        main(
            [
                "--db",
                str(registry.path),
                "run",
                "finish",
                worker["id"],
                "--outcome",
                "succeeded",
                "--summary",
                "CLI finished team worker",
                "--accepted-head-sha",
                accepted_head,
            ]
        )
        == 0
    )
    finished = json.loads(capsys.readouterr().out)
    assert finished["writer_identity"]["accepted_head_sha"] == accepted_head
    assert registry.get_run(worker["id"])["writer_identity"]["accepted_head_sha"] == accepted_head


def test_reconcile_accepted_head_rejects_active_and_wrong_identity(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "active-worker"},
        writer=_writer(registry, "reconcile/active", "reconcile-active", "reconcile-active"),
    )
    with pytest.raises(RegistryError, match="finished successful worker"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )
    with pytest.raises(RegistryError, match="team worker run"):
        registry.reconcile_accepted_head(
            managers[0]["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_repo_head(registry),
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_unrelated_repository(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    unrelated = _create_repository(tmp_path / "unrelated")
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET repository_path = ? WHERE run_id = ?",
            (str(unrelated), worker["id"]),
        )
    with pytest.raises(RegistryError, match="recorded writer repository does not match"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_a_clone_of_the_common_repository(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(registry.repository_path), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="Registry common repository"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=clone,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_team_root_does_not_derive_an_approved_base_from_live_head(registry, tmp_path):
    root = registry.create_task(
        title="Root",
        goal="g",
        success_criteria="s",
        scope={"approved_base_sha": _repo_head(registry)},
    )
    assert root["approved_base_sha"] is None
    team = registry.create_team(
        root["id"], name="explicit-base", manager_identity={"source": "n", "value": "m"}
    )
    child = registry.create_child_task(
        root["id"],
        title="Child",
        goal="g",
        success_criteria="s",
        team_id=team["id"],
        scope={},
    )
    manager = registry.start_manager_run(team["id"], identity={"source": "n", "value": "m"})
    writer = _writer(registry, "explicit-base/worker", "explicit-base-worker", "explicit-base")
    with pytest.raises(RegistryError, match="not approved"):
        registry.start_worker_run(
            child["id"],
            manager_run_id=manager["id"],
            identity={"source": "n", "value": "w"},
            writer=writer,
        )


def test_reconcile_accepted_head_rejects_an_unpushed_writer_head(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    worktree = Path(worker["writer_identity"]["worktree_path"])
    (worktree / "unpushed-recovery.txt").write_text("not on origin\n")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "unpushed-recovery.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "unpushed recovery head"],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="pushed remote branch head"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_missing_or_inconsistent_identity(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    accepted_head = _head(worker["writer_identity"])
    with registry._transaction() as connection:
        connection.execute("UPDATE runs SET identity_source = NULL WHERE id = ?", (worker["id"],))
    with pytest.raises(RegistryError, match="identity is missing"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=accepted_head,
            evidence="checked",
        )
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE runs SET identity_source = 'native', agent_role = 'different-worker' "
            "WHERE id = ?",
            (worker["id"],),
        )
    with pytest.raises(RegistryError, match="identity is inconsistent"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=accepted_head,
            evidence="checked",
        )


@pytest.mark.parametrize("change", ["branch", "head"])
def test_reconcile_accepted_head_rejects_moved_branch_or_head(registry, tmp_path, change):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    accepted_head = _head(worker["writer_identity"])
    if change == "branch":
        with registry._transaction() as connection:
            connection.execute(
                "UPDATE writer_identities SET branch_name = ? WHERE run_id = ?",
                ("legacy/moved", worker["id"]),
            )
    else:
        worktree = Path(worker["writer_identity"]["worktree_path"])
        (worktree / "moved").write_text("head moved\n")
        subprocess.run(["git", "-C", str(worktree), "add", "moved"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", "move head"],
            check=True,
            capture_output=True,
            text=True,
        )
    with pytest.raises(RegistryError, match="branch|current head"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=accepted_head,
            evidence="checked",
        )


def test_reconcile_accepted_head_rejects_ambiguous_live_worktree(registry, tmp_path, monkeypatch):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)

    def ambiguous(_repository):
        raise RegistryError("live Git worktree inventory is ambiguous")

    monkeypatch.setattr(registry_module, "_live_worktrees", ambiguous)
    with pytest.raises(RegistryError, match="ambiguous"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=_head(worker["writer_identity"]),
            evidence="checked",
        )


def test_reconcile_accepted_head_is_one_time_and_requires_evidence(registry, tmp_path):
    _root, _team, _child, worker = _legacy_worker(registry, tmp_path)
    accepted_head = _head(worker["writer_identity"])
    with pytest.raises(RegistryError, match="evidence"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha=accepted_head,
            evidence=" ",
        )
    registry.reconcile_accepted_head(
        worker["id"],
        repository_path=registry.repository_path,
        accepted_head_sha=accepted_head,
        evidence="first reconciliation",
    )
    with pytest.raises(RegistryError, match="cannot be overwritten"):
        registry.reconcile_accepted_head(
            worker["id"],
            repository_path=registry.repository_path,
            accepted_head_sha="0" * 40,
            evidence="second reconciliation",
        )


def test_batch_dispatch_supports_three_workers_and_two_managers(registry, tmp_path):
    root, teams, children, _ = _setup(registry, tmp_path, start_managers=False)
    result = registry.dispatch_batch(
        root["id"],
        managers=[
            {"team_id": teams[0]["id"], "identity": {"source": "native", "value": "manager-0"}},
            {"team_id": teams[1]["id"], "identity": {"source": "native", "value": "manager-1"}},
        ],
        workers=[
            {
                **_worker_spec(tmp_path, children[0], {"id": "unused"}, 0, "src/slice0/a.py"),
                "manager_index": 0,
            },
            {
                **_worker_spec(
                    tmp_path, children[1], {"id": "unused"}, 1, {"kind": "queue", "value": "slice1"}
                ),
                "manager_index": 1,
            },
            {
                **_worker_spec(tmp_path, children[2], {"id": "unused"}, 2, "src/slice2/a.py"),
                "manager_index": 0,
            },
        ],
    )

    assert len(result["manager_runs"]) == 2
    assert len(result["worker_runs"]) == 3
    assert {item["manager_run_id"] for item in result["worker_runs"]} == {
        result["manager_runs"][0]["id"],
        result["manager_runs"][1]["id"],
    }
    assert all(
        registry.get_run(item["id"])["run_type"] == "worker" for item in result["worker_runs"]
    )
    assert registry.get_task(root["id"])["parent_task_id"] is None
    assert all(registry.get_task(child["id"])["parent_task_id"] == root["id"] for child in children)


def test_writer_and_resource_conflicts_reject_before_worker_creation(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    first = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "writer-a"},
        writer=_writer(registry, "team/shared", "a", "a"),
        resources=["shared.py"],
    )
    with pytest.raises(RegistryError, match="writer identity"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "writer-b"},
            writer={
                **first["writer_identity"],
                "worktree_path": str(registry.repository_path / "b"),
                "worktree_id": "b",
            },
            resources=["other.py"],
        )
    assert registry.get_task(children[2]["id"])["state"] == "ready"
    with pytest.raises(RegistryError, match="already claimed"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "writer-c"},
            writer=_writer(registry, "team/c", "c", "c"),
            resources=["shared.py"],
        )
    assert registry.get_task(children[2]["id"])["state"] == "ready"


def test_expired_claim_requires_reconciliation_and_fence_is_required(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    first = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "lease-a"},
        writer=_writer(registry, "lease/a", "lease-a", "lease-a"),
        resources=["lease.py"],
        lease_seconds=1,
    )
    claim = first["resource_claims"][0]
    report = registry.reconcile_resource_claims(now="2999-01-01T00:00:00+00:00")
    assert report["expired"] == 1
    with pytest.raises(RegistryError, match="requires reconciliation"):
        registry.renew_resource_claim(
            claim["id"], run_id=first["id"], fence_token=claim["fence_token"]
        )
    with pytest.raises(RegistryError, match="requires reconciliation"):
        registry.claim_resources(first["id"], ["lease.py"])
    with pytest.raises(RegistryError, match="owner"):
        registry.reconcile_resource_claim(
            claim["id"],
            run_id="foreign-run",
            fence_token=claim["fence_token"],
            evidence="live worker is gone",
        )
    with pytest.raises(RegistryError, match="evidence"):
        registry.release_resource_claim(
            claim["id"], run_id=first["id"], fence_token=claim["fence_token"], evidence=" "
        )
    released = registry.reconcile_resource_claim(
        claim["id"],
        run_id=first["id"],
        fence_token=claim["fence_token"],
        evidence="live worker confirmed stopped; worktree inspected clean",
    )
    assert released["status"] == "released"
    assert "worktree inspected clean" in released["reconciliation_evidence"]
    assert registry.get_task(children[0]["id"])["events"][-1]["event_type"] == (
        "resource_reconciled"
    )
    with pytest.raises(RegistryError, match="only applies"):
        registry.reconcile_resource_claim(
            claim["id"],
            run_id=first["id"],
            fence_token=claim["fence_token"],
            evidence="second release",
        )


def test_claim_race_has_one_winner(tmp_path):
    registry = Registry(tmp_path / "control.db", repository_path=_create_repository(tmp_path))
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    barrier = Barrier(2)

    def attempt(index: int):
        barrier.wait()
        try:
            return registry.start_worker_run(
                children[index]["id"],
                manager_run_id=managers[0]["id"],
                identity={"source": "native", "value": f"race-{index}"},
                writer=_writer(registry, f"race/{index}", f"race-{index}", f"race-{index}"),
                resources=["race.py"],
            )
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (0, 2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RegistryError) for result in results) == 1


def test_reviewer_identity_and_redacted_executive_status(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "worker"},
        writer=_writer(registry, "review/a", "review-a", "review-a"),
        resources=["review.py"],
    )
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=_head(worker["writer_identity"]),
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "reviewer"},
    )
    registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
    evaluation = registry.add_evaluation(
        children[0]["id"],
        run_id=worker["id"],
        evaluator="reviewer",
        evaluator_run_id=reviewer["id"],
        passed=True,
        evidence="independent check",
        reviewed_head_sha=_head(worker["writer_identity"]),
    )
    registry.record_decision(root["id"], "Use the bounded team architecture")
    registry.record_blocker(root["id"], "private credential omitted", redacted=True)
    registry.record_approval(root["id"], "review gate passed", source_run_id=reviewer["id"])
    status = registry.executive_status(root["id"])
    assert evaluation["evaluator_run_id"] == reviewer["id"]
    assert status["decisions"][0]["content"] == "Use the bounded team architecture"
    assert status["blockers"][0]["content"] == "[redacted]"
    assert status["approvals"][0]["content"] == "review gate passed"
    assert "prompt" not in str(status).lower()
    assert "transcript" not in str(status).lower()
    assert "worker complete" not in str(status)


def test_team_evaluation_requires_a_successful_linked_reviewer(registry, tmp_path):
    root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "worker-linked"},
        writer=_writer(registry, "eval/worker", "eval-worker", "eval-worker"),
    )
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=_head(worker["writer_identity"]),
    )
    with pytest.raises(RegistryError, match="requires an evaluator_run_id"):
        registry.add_evaluation(
            children[0]["id"],
            run_id=worker["id"],
            evaluator="reviewer-linked",
            passed=True,
            evidence="string-only team evaluation",
        )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "reviewer-linked"},
    )
    registry.finish_run(reviewer["id"], outcome="failed", summary="reviewer failed")
    with pytest.raises(RegistryError, match="succeeded outcome"):
        registry.add_evaluation(
            children[0]["id"],
            run_id=worker["id"],
            evaluator="reviewer-linked",
            evaluator_run_id=reviewer["id"],
            passed=True,
            evidence="failed reviewer must not pass",
        )
    assert registry.get_task(root["id"])["state"] == "running"


def _admit_overlapping_reviewer(registry: Registry, worker: dict) -> dict:
    overlapping_id = "run_overlapping_reviewer"
    with registry._transaction() as connection:
        first_reviewer = connection.execute(
            "SELECT started_at FROM runs WHERE parent_run_id = ? AND run_type = 'reviewer'",
            (worker["id"],),
        ).fetchone()
        overlapping_started_at = (
            datetime.fromisoformat(first_reviewer["started_at"]) + timedelta(microseconds=1)
        ).isoformat()
        connection.execute(
            """
            INSERT INTO runs(
                id, task_id, agent_role, run_type, parent_run_id, team_id,
                identity_source, identity_value, status, started_at
            ) VALUES (?, ?, ?, 'reviewer', ?, ?, ?, ?, 'running', ?)
            """,
            (
                overlapping_id,
                worker["task_id"],
                "overlapping-reviewer",
                worker["id"],
                worker["team_id"],
                "native",
                "overlapping-reviewer",
                overlapping_started_at,
            ),
        )
    return registry.get_run(overlapping_id)


def test_failed_reviewer_allows_a_sequential_successful_retry(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "sequential-worker"},
        writer=_writer(
            registry, "reviewer-sequential/worker", "reviewer-sequential-worker", "sequential"
        ),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )

    failed_reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "sequential-reviewer-failed"},
    )
    registry.finish_run(failed_reviewer["id"], outcome="failed", summary="review failed")

    retry = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "sequential-reviewer-retry"},
    )
    finished = registry.finish_run(retry["id"], outcome="succeeded", summary="retry passed")

    assert finished["outcome"] == "succeeded"
    assert registry.get_task(children[0]["id"])["state"] == "evaluating"


def test_concurrent_reviewer_admission_allows_only_one_active_reviewer(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "race-worker"},
        writer=_writer(registry, "reviewer-race/worker", "reviewer-race-worker", "race-worker"),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    barrier = Barrier(2)

    def attempt(identity: str):
        barrier.wait()
        try:
            return Registry(
                registry.path, repository_path=registry.repository_path
            ).start_reviewer_run(
                children[0]["id"],
                worker_run_id=worker["id"],
                identity={"source": "native", "value": identity},
            )
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("race-reviewer-a", "race-reviewer-b")))

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, RegistryError)]
    assert len(errors) == 1
    assert "active reviewer" in str(errors[0])
    reviewers = [
        run for run in registry.get_task(children[0]["id"])["runs"] if run["run_type"] == "reviewer"
    ]
    assert len(reviewers) == 1


def test_overlapping_reviewer_cannot_settle_after_success_without_exact_head_acceptance(
    registry, tmp_path
):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "redundant-worker"},
        writer=_writer(
            registry,
            "reviewer-redundant/worker",
            "reviewer-redundant-worker",
            "redundant-worker",
        ),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "primary-reviewer"},
    )
    overlapping = _admit_overlapping_reviewer(registry, worker)
    registry.finish_run(reviewer["id"], outcome="failed", summary="review failed")
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (children[0]["id"],)
        )

    with pytest.raises(RegistryError, match="passing exact-head evaluation"):
        registry.finish_run(overlapping["id"], outcome="succeeded", summary="overlapping review")
    assert registry.get_task(children[0]["id"])["state"] == "succeeded"


def test_overlapping_reviewer_rejects_mismatched_head_evidence(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "mismatch-worker"},
        writer=_writer(
            registry,
            "reviewer-mismatch/worker",
            "reviewer-mismatch-worker",
            "mismatch-worker",
        ),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "mismatch-reviewer"},
    )
    overlapping = _admit_overlapping_reviewer(registry, worker)
    registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")

    mismatch_file = Path(worker["writer_identity"]["worktree_path"]) / "mismatch.txt"
    mismatch_file.write_text("mismatch\n")
    subprocess.run(
        ["git", "-C", str(mismatch_file.parent), "add", mismatch_file.name],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(mismatch_file.parent), "commit", "-m", "mismatch head"],
        check=True,
        capture_output=True,
        text=True,
    )
    mismatched_head = _head(worker["writer_identity"])

    with pytest.raises(RegistryError, match="does not match the accepted worker head"):
        registry.add_evaluation(
            children[0]["id"],
            run_id=worker["id"],
            evaluator="mismatch-reviewer",
            evaluator_run_id=reviewer["id"],
            passed=True,
            evidence="wrong head",
            reviewed_head_sha=mismatched_head,
        )
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (children[0]["id"],)
        )
    with pytest.raises(RegistryError, match="passing exact-head evaluation"):
        registry.finish_run(overlapping["id"], outcome="succeeded", summary="overlapping review")
    assert registry.get_task(children[0]["id"])["state"] == "succeeded"


def test_overlapping_reviewer_wins_evaluation_then_earlier_reviewer_settles(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "accepted-worker"},
        writer=_writer(
            registry, "reviewer-accepted/worker", "reviewer-accepted-worker", "accepted-worker"
        ),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    earlier = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "earlier-reviewer"},
    )
    overlapping = _admit_overlapping_reviewer(registry, worker)
    assert registry.get_run(earlier["id"])["status"] == "running"

    registry.finish_run(overlapping["id"], outcome="succeeded", summary="later review complete")

    registry.add_evaluation(
        children[0]["id"],
        run_id=worker["id"],
        evaluator="overlapping-reviewer",
        evaluator_run_id=overlapping["id"],
        passed=True,
        evidence="later reviewer accepted exact head",
        reviewed_head_sha=accepted_head,
    )
    task_after_evaluation = registry.get_task(children[0]["id"])
    assert task_after_evaluation["state"] == "succeeded"
    assert len(task_after_evaluation["evaluations"]) == 1

    finished = registry.finish_run(
        earlier["id"], outcome="succeeded", summary="earlier review settled"
    )
    assert finished["outcome"] == "succeeded"
    task_after_settlement = registry.get_task(children[0]["id"])
    assert task_after_settlement["state"] == "succeeded"
    assert task_after_settlement["evaluations"] == task_after_evaluation["evaluations"]


def test_reviewer_admission_after_success_and_finish_on_other_task_state_are_rejected(
    registry, tmp_path
):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "state-worker"},
        writer=_writer(registry, "reviewer-state/worker", "reviewer-state-worker", "state-worker"),
    )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "state-reviewer"},
    )
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (children[0]["id"],)
        )
    with pytest.raises(RegistryError, match="requires an evaluating task"):
        registry.start_reviewer_run(
            children[0]["id"],
            worker_run_id=worker["id"],
            identity={"source": "native", "value": "after-success-reviewer"},
        )
    with registry._transaction() as connection:
        connection.execute("UPDATE tasks SET state = 'running' WHERE id = ?", (children[0]["id"],))
    with pytest.raises(RegistryError, match="evaluating or succeeded"):
        registry.finish_run(reviewer["id"], outcome="succeeded", summary="wrong task state")
    assert registry.get_task(children[0]["id"])["state"] == "running"


@pytest.mark.parametrize(
    ("branch_name", "message"),
    [
        ("main", "dedicated"),
        ("refs/heads/main", "dedicated"),
        ("refs/remotes/origin/main", "local branch"),
    ],
)
def test_writer_git_admission_rejects_protected_branches(registry, tmp_path, branch_name, message):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    with pytest.raises(RegistryError, match=message):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": f"protected-{branch_name}"},
            writer={
                "branch_name": branch_name,
                "base_sha": "abcdef1",
                "worktree_path": str(registry.repository_path),
                "worktree_id": f"protected-{branch_name}",
            },
        )


def test_writer_git_admission_rejects_primary_dirty_and_mismatched_worktrees(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    base = _writer(registry, "git/clean", "git-clean", "git-clean")
    with pytest.raises(RegistryError, match="primary checkout"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "primary"},
            writer={
                **base,
                "worktree_path": str(registry.repository_path),
                "worktree_id": "primary",
            },
        )

    dirty = _writer(registry, "git/dirty", "git-dirty", "git-dirty")
    (registry.repository_path / "git-dirty" / "dirty.txt").write_text("dirty\n")
    with pytest.raises(RegistryError, match="dirty"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "dirty"},
            writer=dirty,
        )

    mismatched = _writer(registry, "git/live", "git-live", "git-live")
    with pytest.raises(RegistryError, match="does not match the live worktree branch"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "mismatched"},
            writer={**mismatched, "branch_name": "git/other"},
        )

    non_ancestor = _writer(registry, "git/non-ancestor", "git-non-ancestor", "git-non-ancestor")
    subprocess.run(
        ["git", "-C", str(registry.repository_path), "checkout", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    (registry.repository_path / "README").write_text("new base\n")
    subprocess.run(["git", "-C", str(registry.repository_path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(registry.repository_path), "commit", "-m", "new base"],
        check=True,
        capture_output=True,
        text=True,
    )
    new_base = subprocess.run(
        ["git", "-C", str(registry.repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(RegistryError, match="not an ancestor"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "non-ancestor"},
            writer={**non_ancestor, "base_sha": new_base},
        )


def test_git_inventory_rejects_ambiguous_and_unusable_records(monkeypatch):
    def command(output):
        monkeypatch.setattr(
            registry_module,
            "_git_command",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                ["git"], 0, stdout=output, stderr=""
            ),
        )

    for output, message in (
        ("", "inventory is empty"),
        ("worktree /tmp/one\n", "inventory is ambiguous"),
        ("worktree /tmp/one\nHEAD abc\nlocked\n", "inventory is ambiguous"),
        (
            "worktree /tmp/one\nHEAD abc\nbranch refs/heads/a\n\n"
            "worktree /tmp/one\nHEAD def\nbranch refs/heads/b\n",
            "duplicate paths",
        ),
    ):
        command(output)
        with pytest.raises(RegistryError, match=message):
            registry_module._live_worktrees("/tmp/repository")


def test_git_command_reports_unavailable_and_failed_git(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("git missing")

    monkeypatch.setattr(registry_module.subprocess, "run", unavailable)
    with pytest.raises(RegistryError, match="validation unavailable"):
        registry_module._git_command(["status"], cwd=".")

    monkeypatch.setattr(
        registry_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["git"], 1, stdout="", stderr=""),
    )
    with pytest.raises(RegistryError, match="unknown Git error"):
        registry_module._git_command(["status"], cwd=".")


def test_writer_git_admission_rejects_missing_detached_and_wrong_roots(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    valid = _writer(registry, "git/valid", "git-valid", "git-valid")
    with pytest.raises(RegistryError, match="missing or ambiguous"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "missing-worktree"},
            writer={
                **valid,
                "worktree_path": str(registry.repository_path / "missing"),
                "worktree_id": "missing",
            },
        )

    detached = _writer(registry, "git/detached", "git-detached", "git-detached")
    subprocess.run(
        ["git", "-C", detached["worktree_path"], "checkout", "--detach"],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="live branch"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "detached-worktree"},
            writer=detached,
        )

    other_repository = _create_repository(tmp_path / "other")
    with pytest.raises(RegistryError, match="registry repository"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "wrong-root"},
            writer=valid,
            repository_path=other_repository,
        )


def test_writer_git_admission_rejects_configured_default_branch(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    writer = _writer(registry, "configured-default", "configured-default", "configured-default")
    subprocess.run(
        [
            "git",
            "-C",
            str(registry.repository_path),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/configured-default",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(registry.repository_path),
            "config",
            "--local",
            "branch.unprotected.protected",
            "false",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="protected or the repository default"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "configured-default"},
            writer=writer,
        )


def test_writer_git_admission_rejects_repository_configured_protected_branch(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    writer = _writer(registry, "release/protected", "release-protected", "release-protected")
    subprocess.run(
        [
            "git",
            "-C",
            str(registry.repository_path),
            "config",
            "--local",
            "bossmode.protected-branches",
            "release/protected",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(registry.repository_path),
            "config",
            "--local",
            "branch.release/protected.protected",
            "true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="protected"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "configured-protected"},
            writer=writer,
        )


def test_writer_git_admission_rejects_missing_task_approval(registry, tmp_path):
    _root, _teams, _children, _managers = _setup(registry, tmp_path, count=1)
    writer = _writer(registry, "approval/missing", "approval-missing", "approval-missing")
    with pytest.raises(RegistryError, match="not approved"):
        registry_module._validate_writer_git(
            writer, registry.repository_path, approved_base_sha=None
        )


def test_writer_git_admission_requires_the_task_approved_base(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    (registry.repository_path / "approved.txt").write_text("new base\n")
    subprocess.run(
        ["git", "-C", str(registry.repository_path), "add", "approved.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(registry.repository_path), "commit", "-m", "new approved base"],
        check=True,
        capture_output=True,
        text=True,
    )
    writer = _writer(registry, "approved/mismatch", "approved-mismatch", "approved-mismatch")
    with pytest.raises(RegistryError, match="approved base"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "approved-mismatch"},
            writer=writer,
        )


def test_team_finish_and_review_require_matching_exact_heads(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "exact-worker"},
        writer=_writer(registry, "exact/worker", "exact-worker", "exact-worker"),
    )
    accepted_head = _head(worker["writer_identity"])
    stale_head = accepted_head
    with pytest.raises(RegistryError, match="accepted head"):
        registry.finish_run(worker["id"], outcome="succeeded", summary="missing head")
    (Path(worker["writer_identity"]["worktree_path"]) / "change.txt").write_text("change\n")
    subprocess.run(
        ["git", "-C", worker["writer_identity"]["worktree_path"], "add", "change.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            worker["writer_identity"]["worktree_path"],
            "commit",
            "-m",
            "accepted change",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            worker["writer_identity"]["worktree_path"],
            "push",
            "origin",
            worker["writer_identity"]["branch_name"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="does not match the live current head"):
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="stale head",
            accepted_head_sha=accepted_head,
        )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="accepted head",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "exact-reviewer"},
    )
    registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
    with pytest.raises(RegistryError, match="does not match the accepted worker head"):
        registry.add_evaluation(
            children[0]["id"],
            run_id=worker["id"],
            evaluator="exact-reviewer",
            evaluator_run_id=reviewer["id"],
            passed=True,
            evidence="wrong exact head",
            reviewed_head_sha=stale_head,
        )


def test_team_finish_rejects_a_dirty_writer_worktree(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "dirty-finish-worker"},
        writer=_writer(registry, "strict/dirty", "strict-dirty", "strict-dirty"),
    )
    worktree = Path(worker["writer_identity"]["worktree_path"])
    (worktree / "untracked.txt").write_text("must be rejected\n")
    with pytest.raises(RegistryError, match="worktree is dirty"):
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="dirty writer",
            accepted_head_sha=_head(worker["writer_identity"]),
        )


def test_team_finish_rejects_an_unpushed_writer_head(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "unpushed-finish-worker"},
        writer=_writer(registry, "strict/unpushed", "strict-unpushed", "strict-unpushed"),
    )
    worktree = Path(worker["writer_identity"]["worktree_path"])
    (worktree / "unpushed.txt").write_text("not on origin\n")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "unpushed.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "unpushed head"],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RegistryError, match="pushed remote branch head"):
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="unpushed writer",
            accepted_head_sha=_head(worker["writer_identity"]),
        )


def test_team_finish_rejects_a_primary_checkout_recorded_as_writer(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "primary-finish-worker"},
        writer=_writer(registry, "strict/primary", "strict-primary", "strict-primary"),
    )
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET worktree_path = ?, branch_name = ? WHERE run_id = ?",
            (str(registry.repository_path), "main", worker["id"]),
        )
    with pytest.raises(RegistryError, match="primary checkout"):
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="primary writer",
            accepted_head_sha=_repo_head(registry),
        )


def test_team_finish_rejects_a_foreign_registry_repository(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "foreign-finish-worker"},
        writer=_writer(registry, "strict/foreign", "strict-foreign", "strict-foreign"),
    )
    clone = tmp_path / "finish-clone"
    subprocess.run(
        ["git", "clone", str(registry.repository_path), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    foreign_registry = Registry(registry.path, repository_path=clone)
    with pytest.raises(RegistryError, match="Registry common repository"):
        foreign_registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="foreign repository",
            accepted_head_sha=_head(worker["writer_identity"]),
        )


def test_parallel_manager_finish_is_fail_closed_until_all_gates_pass(registry, tmp_path):
    root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    for number, child in enumerate(children):
        _complete_worker(registry, child, managers[number % 2], number)
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    with pytest.raises(RegistryError, match="three overlapping workers"):
        registry.finish_run(managers[1]["id"], outcome="succeeded", summary="manager complete")
    assert registry.get_task(root["id"])["state"] == "running"


def test_archived_failed_worker_is_ignored_but_non_archived_failure_blocks(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    archived_failed = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "archived-failed-worker"},
        writer=_writer(registry, "archived/failed", "archived-failed", "archived-failed"),
    )
    registry.finish_run(archived_failed["id"], outcome="failed", summary="superseded attempt")
    registry.transition_task(
        children[0]["id"],
        "archived",
        actor="audit",
        reason="superseded attempt retired",
    )
    _complete_worker(registry, children[2], managers[0], 301)
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")

    live_failed = registry.start_worker_run(
        children[1]["id"],
        manager_run_id=managers[1]["id"],
        identity={"source": "native", "value": "live-failed-worker"},
        writer=_writer(registry, "active/failed", "active-failed", "active-failed"),
    )
    registry.finish_run(live_failed["id"], outcome="failed", summary="current failure")
    with pytest.raises(RegistryError, match="every worker succeeds"):
        registry.finish_run(managers[1]["id"], outcome="succeeded", summary="must reject")


def test_archived_worker_does_not_count_toward_parallel_minimum(registry, tmp_path):
    root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    archived = registry.start_worker_run(
        children[2]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "archived-worker"},
        writer=_writer(registry, "archived/worker", "archived-worker", "archived-worker"),
    )
    registry.finish_run(
        archived["id"],
        outcome="succeeded",
        summary="superseded success",
        accepted_head_sha=_head(archived["writer_identity"]),
    )
    registry.transition_task(
        children[2]["id"],
        "archived",
        actor="audit",
        reason="superseded success retired",
    )
    _complete_worker(registry, children[0], managers[0], 302)
    _complete_worker(registry, children[1], managers[1], 303)
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    with pytest.raises(RegistryError, match="at least three workers"):
        registry.finish_run(managers[1]["id"], outcome="succeeded", summary="must reject")
    assert registry.get_task(root["id"])["state"] == "running"


def test_archived_worker_does_not_count_toward_parallel_overlap(registry, tmp_path):
    root, _teams, children, managers = _setup(registry, tmp_path, count=4)
    workers = [
        registry.start_worker_run(
            child["id"],
            manager_run_id=managers[number % 2]["id"],
            identity={"source": "native", "value": f"overlap-worker-{number}"},
            writer=_writer(
                registry,
                f"archived-overlap/{number}",
                f"archived-overlap-{number}",
                f"archived-overlap-{number}",
            ),
        )
        for number, child in enumerate(children)
    ]
    for worker in workers:
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="worker complete",
            accepted_head_sha=_head(worker["writer_identity"]),
        )
    registry.transition_task(
        children[0]["id"],
        "archived",
        actor="audit",
        reason="historical overlap attempt retired",
    )
    for number in (1, 2, 3):
        reviewer = registry.start_reviewer_run(
            children[number]["id"],
            worker_run_id=workers[number]["id"],
            identity={"source": "native", "value": f"overlap-reviewer-{number}"},
        )
        accepted_head = _head(workers[number]["writer_identity"])
        registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
        registry.add_evaluation(
            children[number]["id"],
            run_id=workers[number]["id"],
            evaluator=f"overlap-reviewer-{number}",
            evaluator_run_id=reviewer["id"],
            passed=True,
            evidence="exact head reviewed",
            reviewed_head_sha=accepted_head,
        )
    with registry._transaction() as connection:
        intervals = (
            (workers[0]["id"], "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"),
            (workers[1]["id"], "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"),
            (workers[2]["id"], "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00"),
            (workers[3]["id"], "2026-01-01T00:04:00+00:00", "2026-01-01T00:05:00+00:00"),
        )
        for run_id, started_at, finished_at in intervals:
            connection.execute(
                "UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (started_at, finished_at, run_id),
            )
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    with pytest.raises(RegistryError, match="three overlapping workers"):
        registry.finish_run(managers[1]["id"], outcome="succeeded", summary="must reject")
    assert registry.get_task(root["id"])["state"] == "running"


def test_archived_task_does_not_retire_an_active_worker(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "archived-active-worker"},
        writer=_writer(registry, "archived/active", "archived-active", "archived-active"),
    )
    with registry._transaction() as connection:
        connection.execute("UPDATE tasks SET state = 'archived' WHERE id = ?", (children[0]["id"],))
    with pytest.raises(RegistryError, match="active"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="must reject")
    assert registry.get_run(worker["id"])["status"] == "running"


def test_manager_finish_rejects_active_and_unaccepted_children(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "active-worker"},
        writer=_writer(registry, "active/worker", "active-worker", "active-worker"),
    )
    with pytest.raises(RegistryError, match="active"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="too soon")
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=_head(worker["writer_identity"]),
    )
    with pytest.raises(RegistryError, match="child task is accepted"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="not reviewed")
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (children[0]["id"],)
        )
    with pytest.raises(RegistryError, match="every worker evaluation passes"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="still not reviewed")


def test_manager_finish_rejects_unreleased_claim(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "claim-worker"},
        writer=_writer(registry, "claim/worker", "claim-worker", "claim-worker"),
        resources=["claim.py"],
    )
    claim = worker["resource_claims"][0]
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE resource_claims SET status = 'reconcile_required' WHERE id = ?",
            (claim["id"],),
        )
    accepted_head = _head(worker["writer_identity"])
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=accepted_head,
    )
    reviewer = registry.start_reviewer_run(
        children[0]["id"],
        worker_run_id=worker["id"],
        identity={"source": "native", "value": "claim-reviewer"},
    )
    registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
    registry.add_evaluation(
        children[0]["id"],
        run_id=worker["id"],
        evaluator="claim-reviewer",
        evaluator_run_id=reviewer["id"],
        passed=True,
        evidence="reviewed",
        reviewed_head_sha=accepted_head,
    )
    with pytest.raises(RegistryError, match="claims are unreleased"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="claim remains")


def test_manager_finish_requires_a_worker(registry, tmp_path):
    _root, _teams, _children, managers = _setup(registry, tmp_path, count=0)
    with pytest.raises(RegistryError, match="without worker runs"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="empty team")


def test_manager_finish_rejects_failed_worker(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "failed-worker"},
        writer=_writer(registry, "failed/worker", "failed-worker", "failed-worker"),
    )
    registry.finish_run(worker["id"], outcome="failed", summary="worker failed")
    with pytest.raises(RegistryError, match="every worker succeeds"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="must reject")


def _reject_worker_attempt(
    registry: Registry, child: dict, manager: dict, number: int, *, worker_failed: bool
) -> dict:
    worker = registry.start_worker_run(
        child["id"],
        manager_run_id=manager["id"],
        identity={"source": "native", "value": f"retry-worker-{number}"},
        writer=_writer(registry, f"retry/{number}", f"retry-{number}", f"retry-{number}"),
    )
    if worker_failed:
        registry.finish_run(worker["id"], outcome="failed", summary="worker failed")
    else:
        accepted_head = _head(worker["writer_identity"])
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="worker complete",
            accepted_head_sha=accepted_head,
        )
        reviewer = registry.start_reviewer_run(
            child["id"],
            worker_run_id=worker["id"],
            identity={"source": "native", "value": f"retry-reviewer-{number}"},
        )
        registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
        registry.add_evaluation(
            child["id"],
            run_id=worker["id"],
            evaluator=f"retry-reviewer-{number}",
            evaluator_run_id=reviewer["id"],
            passed=False,
            evidence="rejected worker attempt",
            reviewed_head_sha=accepted_head,
        )
    registry.transition_task(
        child["id"],
        "ready",
        actor="retry",
        reason="retry rejected worker attempt",
    )
    return worker


@pytest.mark.parametrize(
    ("worker_failed", "negative_message"),
    [(False, "child task is accepted"), (True, "every worker succeeds")],
    ids=["rejected", "failed"],
)
def test_manager_finish_selects_corrected_worker_attempt(
    registry, tmp_path, worker_failed, negative_message
):
    root, _teams, children, managers = _setup(registry, tmp_path, count=3)
    rejected = _reject_worker_attempt(
        registry, children[0], managers[0], 500, worker_failed=worker_failed
    )
    with pytest.raises(RegistryError, match=negative_message):
        registry.finish_run(
            managers[0]["id"], outcome="succeeded", summary="negative control before retry"
        )

    corrected, _reviewer = _complete_worker(registry, children[0], managers[0], 501)
    other_workers = [
        _complete_worker(registry, children[number], managers[number % 2], number + 502)[0]
        for number in (1, 2)
    ]

    for worker in [corrected, *other_workers]:
        assert registry.get_run(worker["id"])["outcome"] == "succeeded"
    intervals = (
        (rejected["id"], "2025-12-31T23:00:00+00:00", "2025-12-31T23:10:00+00:00"),
        (corrected["id"], "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"),
        (other_workers[0]["id"], "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"),
        (other_workers[1]["id"], "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00"),
    )
    with registry._transaction() as connection:
        for run_id, started_at, finished_at in intervals:
            connection.execute(
                "UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (started_at, finished_at, run_id),
            )
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    registry.finish_run(managers[1]["id"], outcome="succeeded", summary="manager complete")
    assert registry.get_task(root["id"])["state"] == "succeeded"


def test_manager_finish_rejects_missing_and_mismatched_review_heads(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=1)
    worker, reviewer = _complete_worker(registry, children[0], managers[0], 99)
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET accepted_head_sha = NULL WHERE run_id = ?",
            (worker["id"],),
        )
    with pytest.raises(RegistryError, match="without an accepted worker head"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="missing head")
    accepted_head = _head(worker["writer_identity"])
    with registry._transaction() as connection:
        connection.execute(
            "UPDATE writer_identities SET accepted_head_sha = ? WHERE run_id = ?",
            (accepted_head, worker["id"]),
        )
        connection.execute(
            "UPDATE evaluations SET reviewed_head_sha = ? WHERE run_id = ?",
            ("0" * 40, worker["id"]),
        )
    del reviewer
    with pytest.raises(RegistryError, match="exact-head review"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="mismatched head")


def test_parallel_manager_finish_requires_three_workers(registry, tmp_path):
    _root, _teams, children, managers = _setup(registry, tmp_path, count=2)
    for number, child in enumerate(children):
        _complete_worker(registry, child, managers[number], number + 100)
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    with pytest.raises(RegistryError, match="at least three workers"):
        registry.finish_run(managers[1]["id"], outcome="succeeded", summary="too few workers")


def test_manager_finish_requires_two_manager_runs(registry, tmp_path):
    _root, teams, children, managers = _setup(registry, tmp_path, count=1)
    _complete_worker(registry, children[0], managers[0], 200)
    with registry._transaction() as connection:
        connection.execute("UPDATE teams SET manager_run_id = NULL WHERE id = ?", (teams[1]["id"],))
        connection.execute("DELETE FROM runs WHERE id = ?", (managers[1]["id"],))
    with pytest.raises(RegistryError, match="at least two managers"):
        registry.finish_run(managers[0]["id"], outcome="succeeded", summary="one manager")


def test_writer_git_admission_rejects_internal_root_branch_and_head_mismatches(
    monkeypatch, tmp_path
):
    writer = {
        "branch_name": "git/worker",
        "base_sha": "a" * 40,
        "worktree_path": str(tmp_path / "worker"),
        "worktree_id": "worker",
    }
    monkeypatch.setattr(
        registry_module,
        "_git_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, stdout=str(tmp_path), stderr=""
        ),
    )
    with pytest.raises(RegistryError, match="live Git root"):
        registry_module._validate_writer_git(writer, tmp_path / "other")

    def branch_mismatch(arguments, **_kwargs):
        if arguments[:2] == ["rev-parse", "--show-toplevel"]:
            output = str(tmp_path)
        elif arguments[:3] == ["worktree", "list", "--porcelain"]:
            output = (
                f"worktree {writer['worktree_path']}\nHEAD {'b' * 40}\n"
                "branch refs/heads/git/worker\n"
            )
        else:
            output = "other/branch"
        return subprocess.CompletedProcess(["git"], 0, stdout=output, stderr="")

    monkeypatch.setattr(registry_module, "_git_command", branch_mismatch)
    with pytest.raises(RegistryError, match="mismatched with the live worktree head"):
        registry_module._validate_writer_git(writer, tmp_path)

    def head_mismatch(arguments, **_kwargs):
        if arguments[:2] == ["rev-parse", "--show-toplevel"]:
            output = str(tmp_path)
        elif arguments[:3] == ["worktree", "list", "--porcelain"]:
            output = (
                f"worktree {writer['worktree_path']}\nHEAD {'b' * 40}\n"
                "branch refs/heads/git/worker\n"
            )
        elif arguments[:3] == ["symbolic-ref", "--short", "HEAD"]:
            output = "git/worker"
        elif arguments[0] == "status":
            output = ""
        elif arguments[:2] == ["rev-parse", "--verify"]:
            output = "a" * 40
        elif arguments[:2] == ["rev-parse", "HEAD"]:
            output = "c" * 40
        else:
            output = ""
        return subprocess.CompletedProcess(["git"], 0, stdout=output, stderr="")

    monkeypatch.setattr(registry_module, "_git_command", head_mismatch)
    with pytest.raises(RegistryError, match="head is mismatched"):
        registry_module._validate_writer_git(writer, tmp_path)


def test_executive_status_derives_finished_team_state(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    workers = [
        registry.start_worker_run(
            child["id"],
            manager_run_id=managers[number % 2]["id"],
            identity={"source": "native", "value": f"status-worker-{number}"},
            writer=_writer(registry, f"status/{number}", f"status-{number}", f"status-{number}"),
            resources=[f"status-{number}.py"],
        )
        for number, child in enumerate(children)
    ]
    accepted_heads = [_head(worker["writer_identity"]) for worker in workers]
    for worker, accepted_head in zip(workers, accepted_heads, strict=True):
        registry.finish_run(
            worker["id"],
            outcome="succeeded",
            summary="worker complete",
            accepted_head_sha=accepted_head,
        )
    for number, (child, worker, accepted_head) in enumerate(
        zip(children, workers, accepted_heads, strict=True)
    ):
        reviewer = registry.start_reviewer_run(
            child["id"],
            worker_run_id=worker["id"],
            identity={"source": "native", "value": f"status-reviewer-{number}"},
        )
        registry.finish_run(reviewer["id"], outcome="succeeded", summary="review complete")
        registry.add_evaluation(
            child["id"],
            run_id=worker["id"],
            evaluator=f"status-reviewer-{number}",
            evaluator_run_id=reviewer["id"],
            passed=True,
            evidence="status verified",
            reviewed_head_sha=accepted_head,
        )
    registry.finish_run(managers[0]["id"], outcome="succeeded", summary="manager complete")
    assert registry.get_task(root["id"])["state"] == "running"
    registry.finish_run(managers[1]["id"], outcome="succeeded", summary="manager complete")
    status = registry.executive_status(root["id"])
    team_status = next(item for item in status["teams"] if item["team_id"] == teams[0]["id"])
    assert team_status["status"] == "finished"
    assert registry.get_task(root["id"])["state"] == "succeeded"


def test_singleton_start_run_remains_compatible(registry):
    task = registry.create_task(
        title="Legacy", goal="Keep old callers working", success_criteria="Run finishes"
    )
    run = registry.start_run(task["id"], agent_role="worker")
    finished = registry.finish_run(run["id"], outcome="succeeded", summary="legacy complete")
    assert finished["run_type"] == "worker"
    assert registry.get_task(task["id"])["state"] == "evaluating"


def test_team_tab_layout_has_unique_label_and_idempotent_live_reconciliation(registry):
    root = registry.create_task(title="Tabs", goal="Keep teams separated", success_criteria="Tabs")
    team = registry.create_team(
        root["id"],
        name="layout",
        tab_label="PR6 · Team Layout",
        manager_identity={"source": "native", "value": "manager"},
    )
    with pytest.raises(RegistryError, match="tab label is already reserved"):
        registry.create_team(
            root["id"],
            name="duplicate-label",
            tab_label="PR6 · Team Layout",
            manager_identity={"source": "native", "value": "manager-2"},
        )

    first = registry.bind_team_herdr_tab(
        team["id"],
        herdr_session="bossmode",
        workspace_id="w8",
        tab_id="w8:t5",
        observed_tab_label="PR6 · Team Layout",
    )
    second = registry.bind_team_herdr_tab(
        team["id"],
        herdr_session="bossmode",
        workspace_id="w8",
        tab_id="w8:t5",
        observed_tab_label="PR6 · Team Layout",
    )
    assert second["expected_tab_label"] == first["expected_tab_label"]
    assert second["tab_id"] == "w8:t5"
    with pytest.raises(RegistryError, match="reconciled Herdr tab"):
        registry.bind_team_herdr_tab(
            team["id"],
            herdr_session="bossmode",
            workspace_id="w8",
            tab_id="w8:t6",
            observed_tab_label="PR6 · Team Layout",
        )

    other = registry.create_team(
        root["id"],
        name="other",
        tab_label="Other Team",
        manager_identity={"source": "native", "value": "manager-3"},
    )
    with pytest.raises(RegistryError, match="already bound to team"):
        registry.bind_team_herdr_tab(
            other["id"],
            herdr_session="bossmode",
            workspace_id="w8",
            tab_id="w8:t5",
            observed_tab_label="Other Team",
        )


def test_team_manager_worker_and_reviewer_bindings_reject_wrong_tabs(registry, tmp_path):
    root = registry.create_task(
        title="Admission",
        goal="Same tab",
        success_criteria="Same tab",
        approved_base_sha=_repo_head(registry),
    )
    team = registry.create_team(
        root["id"],
        name="admission",
        tab_label="Admission Team",
        manager_identity={"source": "native", "value": "manager"},
    )
    registry.bind_team_herdr_tab(
        team["id"],
        herdr_session="bossmode",
        workspace_id="w8",
        tab_id="w8:t5",
        observed_tab_label="Admission Team",
    )
    manager = registry.start_manager_run(
        team["id"], identity={"source": "native", "value": "manager"}
    )
    with pytest.raises(RegistryError, match="does not match team tab"):
        registry.bind_herdr_run(
            manager["id"],
            herdr_session="bossmode",
            worker_name="manager_wrong_tab",
            agent_kind="codex",
            workspace_id="w8",
            tab_id="w8:t2",
        )

    child = registry.create_child_task(
        root["id"],
        title="worker",
        goal="worker",
        success_criteria="worker",
        team_id=team["id"],
        scope={},
    )
    worker = registry.start_worker_run(
        child["id"],
        manager_run_id=manager["id"],
        identity={"source": "native", "value": "worker"},
        writer=_writer(registry, "admission/worker", "worker", "admission-worker"),
    )
    with pytest.raises(RegistryError, match="does not match team tab"):
        registry.bind_herdr_run(
            worker["id"],
            herdr_session="bossmode",
            worker_name="worker_wrong_tab",
            agent_kind="codex",
            workspace_id="w8",
            tab_id="w8:t2",
        )
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="worker complete",
        accepted_head_sha=_head(worker["writer_identity"]),
    )
    reviewer = registry.start_reviewer_run(
        child["id"], worker_run_id=worker["id"], identity={"source": "native", "value": "reviewer"}
    )
    with pytest.raises(RegistryError, match="does not match team tab"):
        registry.bind_herdr_run(
            reviewer["id"],
            herdr_session="bossmode",
            worker_name="reviewer_wrong_tab",
            agent_kind="codex",
            workspace_id="w8",
            tab_id="w8:t2",
        )


def test_team_binding_requires_a_reconciled_tab(registry):
    root = registry.create_task(title="Unbound", goal="Unbound", success_criteria="Unbound")
    team = registry.create_team(
        root["id"],
        name="unbound",
        manager_identity={"source": "native", "value": "manager"},
    )
    manager = registry.start_manager_run(
        team["id"], identity={"source": "native", "value": "manager"}
    )
    with pytest.raises(RegistryError, match="requires a reconciled team tab"):
        registry.bind_herdr_run(
            manager["id"],
            herdr_session="bossmode",
            worker_name="unbound_manager",
            agent_kind="codex",
            workspace_id="w8",
            tab_id="w8:t5",
        )


def test_team_tab_label_must_match_observed_tab(registry):
    root = registry.create_task(title="Observed", goal="Observed", success_criteria="Observed")
    team = registry.create_team(
        root["id"],
        name="observed",
        tab_label="Expected Team",
        manager_identity={"source": "native", "value": "manager"},
    )
    with pytest.raises(RegistryError, match="expected tab label"):
        registry.bind_team_herdr_tab(
            team["id"],
            herdr_session="bossmode",
            workspace_id="w8",
            tab_id="w8:t5",
            observed_tab_label="Focused Tab",
        )


def test_team_workflow_requires_downward_panes_and_forbids_right_splits():
    repository = Path(__file__).parents[1]
    command_surfaces = (
        "README.md",
        "docs/agent-workflow.md",
        "docs/cli-reference.md",
        ".agents/skills/bossmode/SKILL.md",
        "src/bossmode/skills/bossmode/SKILL.md",
    )
    for relative_path in command_surfaces:
        surface = (repository / relative_path).read_text()
        command_lines = [line for line in surface.splitlines() if "herdr " in line]
        assert not any("--current" in line for line in command_lines)
        assert not any("--direction right" in line for line in command_lines)
        assert "herdr pane split TEAM_ANCHOR_PANE_ID --direction down" in surface

    adr = (repository / "docs/adr/0004-parallel-manager-teams.md").read_text()
    assert "right split" not in adr
    assert "down split" in adr
    assert "manager/control pane stays at the top" in adr
    assert "every worker and reviewer is stacked" in adr
    assert "below it with horizontal dividers" in adr


def test_singleton_herdr_binding_does_not_require_team_tab(registry):
    task = registry.create_task(title="Singleton", goal="Legacy", success_criteria="Legacy")
    run = registry.start_run(task["id"], agent_role="codex")
    binding = registry.bind_herdr_run(
        run["id"], herdr_session="bossmode", worker_name="singleton", agent_kind="codex"
    )
    assert binding["tab_id"] is None


@pytest.mark.parametrize(
    ("identity", "writer", "message"),
    [
        (None, None, "identity is required"),
        ({"source": "", "value": "worker"}, None, "requires non-empty"),
        ({"source": "native", "value": "worker"}, None, "writer identity is required"),
        (
            {"source": "native", "value": "worker"},
            {"branch_name": "main"},
            "writer identity requires",
        ),
    ],
)
def test_identity_and_writer_contracts_fail_closed(registry, tmp_path, identity, writer, message):
    root, teams, children, managers = _setup(registry, tmp_path)
    with pytest.raises(RegistryError, match=message):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity=identity,
            writer=writer,
            resources=[],
        )

    if writer is None:
        return
    with pytest.raises(RegistryError, match="dedicated"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "worker"},
            writer={
                "branch_name": "main",
                "base_sha": "abcdef1",
                "worktree_path": str(tmp_path / "bad"),
                "worktree_id": "bad",
            },
            resources=[],
        )


def test_claim_renew_release_and_non_file_canonicalization(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path)
    run = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "claims"},
        writer=_writer(registry, "claims/a", "claims", "claims"),
        resources=[{"kind": "service", "value": "  queue   alpha  "}],
    )
    claim = run["resource_claims"][0]
    assert claim["canonical_key"] == "queue alpha"
    renewed = registry.renew_resource_claim(
        claim["id"], run_id=run["id"], fence_token=claim["fence_token"]
    )
    assert renewed["status"] == "active"
    assert (
        registry.release_resource_claim(
            claim["id"], run_id=run["id"], fence_token=claim["fence_token"]
        )["status"]
        == "released"
    )
    with pytest.raises(RegistryError, match="owner"):
        registry.release_resource_claim(claim["id"], run_id=run["id"], fence_token="wrong")


def test_batch_can_create_teams_and_rolls_back_conflicts(registry, tmp_path):
    root = registry.create_task(
        title="batch",
        goal="batch",
        success_criteria="batch",
        approved_base_sha=_repo_head(registry),
    )
    child = registry.create_task(
        title="child",
        goal="child",
        success_criteria="child",
        parent_task_id=root["id"],
        task_kind="child",
        scope={"paths": ["child"]},
    )
    with pytest.raises(RegistryError, match="manager name"):
        registry.dispatch_batch(
            root["id"], managers=[{"identity": {"source": "n", "value": "m"}}], workers=[{}]
        )
    result = registry.dispatch_batch(
        root["id"],
        managers=[{"name": "created", "identity": {"source": "n", "value": "m"}}],
        workers=[
            {
                "task_id": child["id"],
                "manager_index": 0,
                "identity": {"source": "n", "value": "w"},
                "writer": _writer(registry, "created/w", "created", "created"),
                "resources": [],
            }
        ],
    )
    assert len(result["manager_runs"]) == 1
    assert registry.list_teams(root["id"])[0]["name"] == "created"


def test_cli_exposes_team_dispatch_status_and_signal(tmp_path, capsys, monkeypatch):
    database = tmp_path / "control.db"
    repository = _create_repository(tmp_path)
    git_registry = Registry(database, repository_path=repository)
    monkeypatch.chdir(repository)

    def call(*args):
        assert main(["--db", str(database), *args]) == 0
        return __import__("json").loads(capsys.readouterr().out)

    root = call(
        "task",
        "create",
        "--title",
        "CLI root",
        "--goal",
        "g",
        "--success-criteria",
        "s",
        "--approved-base-sha",
        _repo_head(git_registry),
    )
    team = call(
        "team",
        "create",
        root["id"],
        "--name",
        "cli",
        "--manager-identity-json",
        '{"source":"cli","value":"m"}',
        "--tab-label",
        "CLI Team",
    )
    assert (
        call(
            "team",
            "bind-tab",
            team["id"],
            "--herdr-session",
            "bossmode",
            "--workspace-id",
            "w8",
            "--tab-id",
            "w8:t5",
            "--observed-tab-label",
            "CLI Team",
        )["tab_id"]
        == "w8:t5"
    )
    child = call(
        "task",
        "create",
        "--title",
        "CLI child",
        "--goal",
        "g",
        "--success-criteria",
        "s",
        "--parent-task-id",
        root["id"],
        "--team-id",
        team["id"],
        "--task-kind",
        "child",
        "--scope-json",
        '{"paths":["cli"]}',
    )
    manager = call(
        "run", "manager-start", team["id"], "--identity-json", '{"source":"cli","value":"m"}'
    )
    worker = call(
        "run",
        "worker-start",
        child["id"],
        "--manager-run-id",
        manager["id"],
        "--identity-json",
        '{"source":"cli","value":"w"}',
        "--writer-json",
        json.dumps(_writer(git_registry, "cli/w", "cli-w", "cli-w")),
        "--repository-path",
        str(repository),
        "--resources-json",
        '[{"kind":"service","value":"cli"}]',
    )
    claim = worker["resource_claims"][0]
    assert call("resource", "reconcile", "--now", "2999-01-01T00:00:00+00:00")["expired"] == 1
    assert (
        call(
            "resource",
            "release",
            claim["id"],
            "--run-id",
            worker["id"],
            "--fence-token",
            claim["fence_token"],
            "--evidence",
            "live worker stopped and worktree inspected",
        )["status"]
        == "released"
    )
    assert call("team", "list", "--root-task-id", root["id"])[0]["id"] == team["id"]
    assert call("team", "show", team["id"])["manager_run_id"] == manager["id"]
    assert call("status", "executive", root["id"])["task_id"] == root["id"]
    assert call("signal", root["id"], "decision", "--content", "CLI decision")["kind"] == "decision"
    assert call("resource", "reconcile")["expired"] == 0
    assert worker["run_type"] == "worker"


def test_parallel_contract_rejections_are_explicit(registry, tmp_path):
    with pytest.raises(RegistryError, match="task kind"):
        registry.create_task(title="bad", goal="bad", success_criteria="bad", task_kind=" ")
    with pytest.raises(RegistryError, match="parent task"):
        registry.create_task(
            title="bad", goal="bad", success_criteria="bad", parent_task_id="missing"
        )
    with pytest.raises(RegistryError, match="root task"):
        registry.create_team("missing", name="team", manager_identity={"source": "n", "value": "m"})
    root = registry.create_task(
        title="root", goal="g", success_criteria="s", approved_base_sha=_repo_head(registry)
    )
    other_root = registry.create_task(title="other root", goal="g", success_criteria="s")
    with pytest.raises(RegistryError, match="team name"):
        registry.create_team(root["id"], name=" ", manager_identity={"source": "n", "value": "m"})
    with pytest.raises(RegistryError, match="team tab label"):
        registry.create_team(
            root["id"],
            name="valid",
            tab_label=" ",
            manager_identity={"source": "n", "value": "label"},
        )
    with pytest.raises(RegistryError, match="parent team"):
        registry.create_team(
            root["id"],
            name="team",
            parent_team_id="missing",
            manager_identity={"source": "n", "value": "m"},
        )
    team = registry.create_team(
        root["id"], name="team", manager_identity={"source": "n", "value": "m"}
    )
    child_root = registry.create_child_task(
        root["id"], title="child root", goal="g", success_criteria="s", team_id=team["id"], scope={}
    )
    with pytest.raises(RegistryError, match="team root"):
        registry.create_team(
            child_root["id"],
            name="child-root-team",
            manager_identity={"source": "n", "value": "child-root"},
        )
    other_team = registry.create_team(
        root["id"], name="other-team", manager_identity={"source": "n", "value": "other-team"}
    )
    with pytest.raises(RegistryError, match="crosses a team hierarchy"):
        registry.create_child_task(
            child_root["id"],
            title="cross-team child",
            goal="g",
            success_criteria="s",
            team_id=other_team["id"],
            scope={},
        )
    with pytest.raises(RegistryError, match="team assignment"):
        registry.create_task(title="unscoped", goal="g", success_criteria="s", team_id=team["id"])
    with pytest.raises(RegistryError, match="does not match the parent task root"):
        registry.create_child_task(
            other_root["id"],
            title="cross-root",
            goal="g",
            success_criteria="s",
            team_id=team["id"],
            scope={},
        )
    with pytest.raises(RegistryError, match="does not match the root task"):
        registry.create_team(
            other_root["id"],
            name="cross-root-team",
            parent_team_id=team["id"],
            manager_identity={"source": "n", "value": "cross"},
        )
    with pytest.raises(RegistryError, match="does not match"):
        registry.start_manager_run(team["id"], identity={"source": "n", "value": "other"})
    child = registry.create_child_task(
        root["id"], title="child", goal="g", success_criteria="s", team_id=team["id"], scope={}
    )
    manager = registry.start_manager_run(team["id"], identity={"source": "n", "value": "m"})
    with pytest.raises(RegistryError, match="running manager"):
        registry.start_worker_run(
            child["id"],
            manager_run_id="missing",
            identity={"source": "n", "value": "w"},
            writer=_writer(registry, "reject/preflight", "reject-preflight", "reject-preflight"),
            resources=[],
        )
    worker = registry.start_worker_run(
        child["id"],
        manager_run_id=manager["id"],
        identity={"source": "n", "value": "w"},
        writer=_writer(registry, "reject/w", "reject-2", "reject-2"),
        resources=[],
    )
    with pytest.raises(RegistryError, match="positive"):
        registry.claim_resources(worker["id"], [], lease_seconds=0)
    with pytest.raises(RegistryError, match="not found"):
        registry.release_resource_claim("missing", run_id=worker["id"], fence_token="missing")
    with pytest.raises(RegistryError, match="succeeded worker"):
        registry.start_reviewer_run(
            child["id"], worker_run_id=worker["id"], identity={"source": "n", "value": "w"}
        )


def test_parallel_recovery_and_conflict_error_matrix(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path)
    assert registry.get_team(teams[0]["id"])["manager_run"] == managers[0]["id"]
    assert len(registry.list_teams()) == 2
    assert registry.list_teams("missing") == []
    with pytest.raises(RegistryError, match="team not found"):
        registry.get_team("missing")
    with pytest.raises(RegistryError, match="resource kind"):
        registry.claim_resources(managers[0]["id"], [{"kind": "", "value": "x"}])
    with pytest.raises(RegistryError, match="base SHA"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "n", "value": "bad-sha"},
            writer={
                "branch_name": "bad/sha",
                "base_sha": "bad",
                "worktree_path": str(tmp_path / "bad-sha"),
                "worktree_id": "bad-sha",
            },
            resources=[],
        )
    other = registry.create_team(
        root["id"], name="other", manager_identity={"source": "n", "value": "other"}
    )
    wrong_child = registry.create_child_task(
        root["id"], title="wrong", goal="g", success_criteria="s", team_id=other["id"], scope={}
    )
    with pytest.raises(RegistryError, match="outside"):
        registry.start_worker_run(
            wrong_child["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "n", "value": "outside"},
            writer={
                "branch_name": "outside/w",
                "base_sha": "abcdef1",
                "worktree_path": str(tmp_path / "outside"),
                "worktree_id": "outside",
            },
            resources=[],
        )
    worker = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "n", "value": "matrix"},
        writer=_writer(registry, "matrix/w", "matrix", "matrix"),
        resources=["matrix.py"],
    )
    with pytest.raises(RegistryError, match="found running"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "n", "value": "matrix-2"},
            writer=_writer(registry, "matrix/w2", "matrix-2", "matrix-2"),
            resources=[],
        )
    claim = worker["resource_claims"][0]
    assert (
        registry.claim_resources(worker["id"], [("service", "tuple-resource")])[0]["resource_kind"]
        == "service"
    )
    with pytest.raises(RegistryError, match="resource must"):
        registry.claim_resources(worker["id"], [object()])
    with pytest.raises(RegistryError, match="duplicate"):
        registry.claim_resources(worker["id"], ["duplicate.py", "duplicate.py"])
    with pytest.raises(RegistryError, match="fence"):
        registry.renew_resource_claim(claim["id"], run_id=worker["id"], fence_token="wrong")
    with pytest.raises(RegistryError, match="fence"):
        registry.release_resource_claim(claim["id"], run_id=worker["id"], fence_token="wrong")
    with pytest.raises(RegistryError, match="not found"):
        registry.claim_resources("missing", [])
    with pytest.raises(RegistryError, match="not found"):
        registry.renew_resource_claim("missing", run_id=worker["id"], fence_token="missing")
    registry.finish_run(
        worker["id"],
        outcome="succeeded",
        summary="matrix complete",
        accepted_head_sha=_head(worker["writer_identity"]),
    )
    with pytest.raises(RegistryError, match="released"):
        registry.renew_resource_claim(
            claim["id"], run_id=worker["id"], fence_token=claim["fence_token"]
        )
    with pytest.raises(RegistryError, match="released"):
        registry.release_resource_claim(
            claim["id"], run_id=worker["id"], fence_token=claim["fence_token"]
        )
    with pytest.raises(RegistryError, match="source run"):
        registry.record_approval(root["id"], "bad source", source_run_id="missing")
