# ADR 0005: Defer policy-based executor selection

Status: accepted

Date: 2026-08-25

Related: ADR 0001 and ADR 0002

## Context

Bossmode coordinates native subagents and external Herdr workers, but it is a control record rather
than an executor or provider router. The supervisor currently chooses the execution path and agent
explicitly, then records the run and reconciles its identity against the authoritative native
runtime or Herdr state.

The registry can store a caller-supplied agent role, model name, reasoning effort, Herdr agent kind,
and native session reference. Those fields support dispatch, recovery, and telemetry. They do not
mechanically prove the provider, model family, billing class, or exact model that performed a run.
The supported launch tools can also change independently of Bossmode.

Automatic routing could eventually assign work by capability, cost, availability, or review
diversity. Enforcing those policies now would require contracts that the current runtime boundary
does not provide:

- closed, versioned vocabularies for provider, model family, capability, quality, and billing class;
- structured observations that distinguish requested execution from what actually ran;
- a reserve, launch, bind, failed-launch, and stale-run recovery lifecycle for every runtime;
- evidence grades strong enough to support any mandatory diversity or capability gate; and
- capacity and quota semantics that can recover safely after interruption.

Provider or model-family inequality would not by itself prove independent reasoning. Both agents
may receive the same prompt, context, tools, and assumptions.

## Decision

Keep executor selection explicit and capability-based. The supervisor chooses the execution path
and agent for each run using current, observable constraints:

1. Honor an explicit user request for an agent or runtime when it is available and within the
   task's permission scope.
2. Choose the narrowest task role: researcher, worker, or independent reviewer.
3. Use a native subagent only when the host runtime exposes the required creation, messaging,
   waiting, and identity controls.
4. Use Herdr when the request calls for an external interactive agent or needs a visible pane and
   durable detach/reattach behavior. Verify the selected kind against the live, release-matched
   Herdr command surface before relying on it.
5. Treat stored model and reasoning fields as caller-supplied telemetry, not observed provenance.
6. Apply the existing independent-evaluation and evidence gates regardless of agent or model
   labels. A different provider or model family does not replace those gates.

Do not add an executor catalog, selection policy commands, automatic task classification,
provider adapters, credential management, live pricing, learned routing, capacity scheduling, or
provider/model-family review gates. Do not implement automatic selection in a skill as a substitute
for a control-plane contract.

This decision changes documentation only. It adds no runtime behavior, schema, CLI vocabulary,
test contract, or guarantee about which executor is installed or available.

## Rejected alternatives

### Route from historical performance or cost data

Historical telemetry is incomplete and caller-supplied model metadata is not runtime proof. Using
it automatically would turn observations into an unsupported control decision. A supervisor may
consider relevant evidence manually but must still verify present capability and availability.

### Require different provider or model-family labels for review

Bossmode has no canonical provider or family vocabulary and no mechanically verified observation
for either value. Label inequality would be easy to satisfy without establishing independent
reasoning or a sound review.

### Add provenance storage before selection

A provenance-only table would create a second source of routing truth without a launch-time
observation contract. Storing asserted values would not justify enforcing them.

### Add a generic executor abstraction now

ADR 0001 keeps process launch, liveness, panes, and native session state with native runtimes and
Herdr. A generic abstraction is not justified until at least two concrete runtimes require behavior
that their authoritative interfaces cannot provide.

## Compatibility

Current explicit native and Herdr dispatch remains valid. Existing run metadata keeps its telemetry
meaning. No migration or compatibility adapter is required because the registry and CLI are
unchanged.

## Reconsideration gates

Reconsider automatic selection only after all of these gates pass:

1. The work is explicitly prioritized with measurable acceptance criteria for the routing outcome.
2. Supported runtimes expose trustworthy structured observations, or the product explicitly
   accepts asserted evidence and documents its limits.
3. Bossmode defines canonical and versioned provider, model-family, capability, billing-class,
   quality, and task-phase vocabularies.
4. Reserve, launch, bind, compensation, and stale-run recovery are specified for every supported
   execution path.
5. Any mandatory independence or capability rule names the evidence grade that proves it.
6. The implementation plan reconciles ADR 0001's runtime boundary and passes live canaries for each
   behavior it proposes to enforce.

Until then, manual executor choice is intentional product behavior rather than a routing gap to
fill opportunistically.
