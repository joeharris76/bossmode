from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from bossmode.cli import main
from bossmode.registry import Registry, RegistryError


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "control.db")


def _setup(registry: Registry, tmp_path: Path, count: int = 3, *, start_managers: bool = True):
    root = registry.create_task(
        title="Root", goal="Coordinate teams", success_criteria="All children pass"
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
        children.append(
            registry.create_child_task(
                root["id"],
                title=f"Child {child_number}",
                goal="Implement one disjoint slice",
                success_criteria="Slice is independently verified",
                team_id=team["id"],
                scope={"paths": [f"src/slice{child_number}"]},
            )
        )
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
    return {
        "task_id": child["id"],
        "manager_run_id": manager["id"],
        "identity": {"source": "native", "value": f"worker-{number}"},
        "writer": {
            "branch_name": f"team/{number}",
            "base_sha": f"abcdef{number}",
            "worktree_path": str(tmp_path / f"worktree-{number}"),
            "worktree_id": f"worktree-{number}",
        },
        "resources": [resource],
    }


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
    registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "writer-a"},
        writer={
            "branch_name": "team/shared",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "a"),
            "worktree_id": "a",
        },
        resources=["shared.py"],
    )
    with pytest.raises(RegistryError, match="writer identity"):
        registry.start_worker_run(
            children[2]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "native", "value": "writer-b"},
            writer={
                "branch_name": "team/shared",
                "base_sha": "abcdef2",
                "worktree_path": str(tmp_path / "b"),
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
            writer={
                "branch_name": "team/c",
                "base_sha": "abcdef3",
                "worktree_path": str(tmp_path / "c"),
                "worktree_id": "c",
            },
            resources=["shared.py"],
        )
    assert registry.get_task(children[2]["id"])["state"] == "ready"


def test_expired_claim_requires_reconciliation_and_fence_is_required(registry, tmp_path):
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    first = registry.start_worker_run(
        children[0]["id"],
        manager_run_id=managers[0]["id"],
        identity={"source": "native", "value": "lease-a"},
        writer={
            "branch_name": "lease/a",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "lease-a"),
            "worktree_id": "lease-a",
        },
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


def test_claim_race_has_one_winner(tmp_path):
    registry = Registry(tmp_path / "control.db")
    root, teams, children, managers = _setup(registry, tmp_path, count=3)
    barrier = Barrier(2)

    def attempt(index: int):
        barrier.wait()
        try:
            return registry.start_worker_run(
                children[index]["id"],
                manager_run_id=managers[0]["id"],
                identity={"source": "native", "value": f"race-{index}"},
                writer={
                    "branch_name": f"race/{index}",
                    "base_sha": f"abcdef{index}",
                    "worktree_path": str(tmp_path / f"race-{index}"),
                    "worktree_id": f"race-{index}",
                },
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
        writer={
            "branch_name": "review/a",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "review-a"),
            "worktree_id": "review-a",
        },
        resources=["review.py"],
    )
    registry.finish_run(worker["id"], outcome="succeeded", summary="worker complete")
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
    root = registry.create_task(title="Admission", goal="Same tab", success_criteria="Same tab")
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
        writer={
            "branch_name": "admission/worker",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "worker"),
            "worktree_id": "admission-worker",
        },
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
    registry.finish_run(worker["id"], outcome="succeeded", summary="worker complete")
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
        writer={
            "branch_name": "claims/a",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "claims"),
            "worktree_id": "claims",
        },
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
    root = registry.create_task(title="batch", goal="batch", success_criteria="batch")
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
                "writer": {
                    "branch_name": "created/w",
                    "base_sha": "abcdef1",
                    "worktree_path": str(tmp_path / "created"),
                    "worktree_id": "created",
                },
                "resources": [],
            }
        ],
    )
    assert len(result["manager_runs"]) == 1
    assert registry.list_teams(root["id"])[0]["name"] == "created"


def test_cli_exposes_team_dispatch_status_and_signal(tmp_path, capsys):
    database = tmp_path / "control.db"

    def call(*args):
        assert main(["--db", str(database), *args]) == 0
        return __import__("json").loads(capsys.readouterr().out)

    root = call("task", "create", "--title", "CLI root", "--goal", "g", "--success-criteria", "s")
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
        '{"branch_name":"cli/w","base_sha":"abcdef1","worktree_path":"/tmp/cli-w","worktree_id":"cli-w"}',
        "--resources-json",
        '[{"kind":"service","value":"cli"}]',
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
    root = registry.create_task(title="root", goal="g", success_criteria="s")
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
            writer={
                "branch_name": "reject/w",
                "base_sha": "abcdef1",
                "worktree_path": str(tmp_path / "reject"),
                "worktree_id": "reject",
            },
            resources=[],
        )
    worker = registry.start_worker_run(
        child["id"],
        manager_run_id=manager["id"],
        identity={"source": "n", "value": "w"},
        writer={
            "branch_name": "reject/w",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "reject"),
            "worktree_id": "reject",
        },
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
        writer={
            "branch_name": "matrix/w",
            "base_sha": "abcdef1",
            "worktree_path": str(tmp_path / "matrix"),
            "worktree_id": "matrix",
        },
        resources=["matrix.py"],
    )
    with pytest.raises(RegistryError, match="found running"):
        registry.start_worker_run(
            children[0]["id"],
            manager_run_id=managers[0]["id"],
            identity={"source": "n", "value": "matrix-2"},
            writer={
                "branch_name": "matrix/w2",
                "base_sha": "abcdef2",
                "worktree_path": str(tmp_path / "matrix-2"),
                "worktree_id": "matrix-2",
            },
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
    registry.finish_run(worker["id"], outcome="succeeded", summary="matrix complete")
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
