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
required reviewer, and self-review is disabled. No deployment was approved.

The repository currently has no branch protection or ruleset. The release
workflow is therefore the enforcement point for the exact-tag test gate; this
task did not broaden scope to change branch policy.

PyPI currently returns HTTP 404 for the `bossmode` project, so there is no
existing project publisher to verify or edit. Before the first publication, an
authorized PyPI maintainer must configure a GitHub Actions Trusted Publisher (or
the pending publisher flow for a new project) with:

- owner: `joeharris76`
- repository: `bossmode`
- workflow: `.github/workflows/publish.yml`
- environment: `pypi`

That remains a protected manual gate. The first release must not be approved
until the publisher is visible in PyPI's authoritative project settings and the
tag-specific verification run has passed.

Authoritative references:

- GitHub environments and required reviewers: <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
- GitHub environment API: <https://docs.github.com/en/rest/deployments/environments>
- PyPI Trusted Publishers: <https://docs.pypi.org/trusted-publishers/adding-a-publisher/>
- PyPI Trusted Publishing with GitHub Actions: <https://docs.pypi.org/trusted-publishers/using-a-publisher/>
