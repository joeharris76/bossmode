# Release controls

The `.github/workflows/publish.yml` workflow is triggered only when a GitHub
release is published. Before the PyPI job can start, its `verify` job:

1. Checks out the release tag and verifies that the tag commit, checkout, and
   `GITHUB_SHA` are the same revision.
2. Verifies the `v<project.version>` tag and that the revision is reachable from
   `main`.
3. Runs `uv lock --check`, locked dependency synchronization, Ruff lint,
   format-check, the full pytest suite, and the UAT harness.
4. Builds exactly one wheel and source distribution, runs Twine validation, and
   runs the installed wheel in an isolated environment.

The `publish` job consumes only the distributions produced by that successful
verification job. It references the protected GitHub environment `pypi` and
uses GitHub OIDC through `pypa/gh-action-pypi-publish`; it does not use a stored
PyPI token.

## Live boundary configuration

On 2026-08-24, the `joeharris76/bossmode` repository was configured with a
`pypi` environment through the GitHub API. Joe Harris (`joeharris76`) is the
required reviewer, self-review is disabled, administrator bypass is disabled,
and the environment accepts only tags matching `v*`. No deployment was
approved.

The release workflow remains the enforcement point for the exact-tag test gate.
The environment's custom deployment policy is an additional `v*` tag-only
boundary; it does not replace repository branch protection or rulesets.

The user confirmed that PyPI has a pending Trusted Publisher for `bossmode` with
the following claims:

- owner: `joeharris76`
- repository: `bossmode`
- workflow: `publish.yml`
- environment: `pypi`

PyPI's public project endpoint currently returns HTTP 404 for `bossmode`. That
is expected for a pending or not-yet-public project, so the pending publisher is
not publicly verifiable; the user-confirmed state is the authoritative evidence
available for this task.

Because `prevent_self_review` is enabled, Joe cannot approve a deployment for a
workflow run that Joe initiated. A non-Joe release initiator must publish the
GitHub release before Joe can approve the protected `pypi` deployment. The
first release must also wait for the exact tag verification run to pass.

Authoritative references:

- GitHub environments and required reviewers: <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
- GitHub environment API: <https://docs.github.com/en/rest/deployments/environments>
- PyPI Trusted Publishers: <https://docs.pypi.org/trusted-publishers/adding-a-publisher/>
- PyPI Trusted Publishing with GitHub Actions: <https://docs.pypi.org/trusted-publishers/using-a-publisher/>
