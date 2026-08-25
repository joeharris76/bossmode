# ADR 0005: Defer policy-based executor selection

**Status:** Deferred
**Date:** 2026-08-25
**Related:** ADR 0001 and ADR 0004

## Context

Bossmode should eventually help an executive coordinate a varied group of
agents. Useful routing policies include assigning simple implementation to a
lower-cost model and requiring implementation and review to use meaningfully
different providers or model families. This would help use multiple available
subscriptions and reduce reviews that merely repeat the implementer's reasoning.

The reviewed proposal added versioned executor profiles and policies,
phase-aware selection, durable assignments, runtime observations, and
worker/reviewer diversity gates. The goal is valid, but the current runtime and
control-plane contracts do not yet support the proposed guarantees cleanly:

- Bossmode can record an intended model, but current native and Herdr bindings
  do not mechanically prove the provider, model family, or model that ran.
- Provider and model-family names have no canonical vocabulary, so string
  inequality cannot establish meaningful reviewer independence.
- Native runtime identity exists only after creation. Selection therefore needs
  an explicit reserve, launch, bind, and failed-launch compensation lifecycle.
- Counted executor capacity does not fit the existing exclusive resource-claim
  model and would add a separate stale-run recovery problem.
- The proposed profile and policy surface is larger than the evidence needed to
  justify it now.

ADR 0001 keeps process launch, runtime identity, liveness, panes, and native
session state outside the registry. ADR 0004 adds durable team, run, claim, and
review coordination without changing that boundary.

## Decision

Defer the entire policy-based executor-selection workstream. Do not implement a
partial catalog, provenance schema, selector, or automatic routing path now.
Specifically, do not add:

- executor profile or policy commands;
- task phases or automatic executor selection;
- executor assignment or observation tables;
- automatic low-cost routing or provider/model-family review gates;
- executor capacity or quota scheduling;
- headless execution, provider adapters, credential management, live pricing,
  learned routing, or automatic task classification.

Continue to let the executive choose native or Herdr workers explicitly. The
registry may retain caller-supplied model and reasoning metadata for audit, but
that metadata is not proof of the runtime model. Existing reviewer identity and
exact-head evaluation gates remain in force; they do not claim provider or
model-family independence.

This deferral covers all implementation slices. It must not be bypassed by
shipping a smaller provenance-only schema or a skill-local automatic selector.
Either would create another source of routing truth without delivering the
requested end-to-end guarantee.

## Preserved design direction

If the workstream resumes, start from a narrow registry-owned selector rather
than the original full proposal:

- Keep selection policy and immutable assignment intent in the registry.
- Keep launch, runtime identity, liveness, and observation with native or Herdr
  authorities.
- Reserve a run before launch; bind the returned native or Herdr identity once;
  finish the reservation as failed when launch fails.
- Separate assigned intent from observed execution and grade evidence as
  asserted, structured runtime observation, or mechanically verified.
- Define closed provider and model-family vocabularies before enforcing
  diversity.
- Reject simultaneous legacy model overrides for policy-bound runs.
- Omit counted capacity, subscription aliases, user-declared hard independence
  groups, and uncertified headless transports from the first version.

The selector may recommend a different provider or model family, but a hard
soundness gate must depend on an evidence grade that actually supports that
claim. A shared task prompt also means provider diversity is only one input to
review independence, not proof that two agents reasoned independently.

## Reconsideration triggers

Reconsider this ADR only when all of the following are true:

1. The workstream is explicitly reprioritized with acceptance criteria for
   low-cost routing and reviewer diversity.
2. Supported runtimes expose trustworthy provider and model-family evidence,
   or the product explicitly accepts asserted evidence and its limits.
3. Bossmode defines canonical provider, model-family, billing-class, quality,
   and phase semantics.
4. The reserve, bind, failed-launch, and stale-run recovery contracts are
   specified for both native and Herdr execution.
5. A new implementation plan reconciles ADR 0001's runtime boundary and is
   approved after review.

Until then, manual executor choice is the intentional product behavior, not a
temporary implementation gap to fill opportunistically.
