# Example walkthrough: a Bossmode conversation

This example shows the interface most users will see: prompts, evidence-backed updates, and explicit
approval gates. The registry and runtime commands are implementation details handled by the
supervisor. IDs below are illustrative.

## Scenario

- Goal: add a token-bucket rate limiter to authentication middleware.
- Supervisor: AGY.
- First worker: Pi through Herdr.
- First reviewer: Grok.
- Remediation worker: Claude through Herdr.
- Final reviewer: Muse.

## 1. Start bounded work

**User**

```text
Use Bossmode to implement rate limiting for our auth endpoints.

Success means the middleware uses a token bucket, enforces 100 requests per minute, allows bursts
of 20 requests, and passes focused tests. Limit edits to src/middleware/ratelimit.py and its tests.
Use Pi through Herdr for the implementation and an independent reviewer before completion. Ask
before expanding permissions or approving a trust dialog.
```

**Supervisor**

```text
Recorded task task_a1b2c3d4e5f6 with the requested scope and success criteria. I reserved
run_111122223333 before creating the Pi worker, verified its live Herdr identity, and sent the
correlated task turn. I will report again after the worker settles or if it needs a protected
approval.
```

Behind the scenes, the supervisor records the task, accepts only the task selected by Bossmode,
reserves the run, verifies the live Herdr worker, and registers the exact result path before sending
the prompt. The user does not need to issue those commands.

## 2. Independent evaluation rejects the first result

**Supervisor**

```text
Evaluation failed for run_111122223333.

- Reviewer: Grok (evaluation eval_001122334455)
- Evidence: the burst test rejected requests immediately instead of allowing a burst of 20.
- Cause: the worker used a fixed-window counter rather than a token bucket.
- Preserved artifact: src/middleware/ratelimit.py

The requested implementation is not complete. May I retry the same task with the failed evaluation
as a required correction and use Claude for the remediation run?
```

The worker's successful self-report was not enough. Bossmode preserved the run, and the independent
evaluation kept the task from reaching final success.

## 3. Retry with the correction

**User**

```text
Yes. Retry task_a1b2c3d4e5f6 with Claude. Require a token bucket that supports the burst capacity.
Keep the existing file scope and require a new independent evaluation.
```

**Supervisor**

```text
Recorded the correction under recurrence key ratelimit.token-bucket. I started remediation run
run_444455556666 and bound a newly verified Claude worker. The original Pi run remains unchanged in
the task history.
```

This is a new worker and run, not a continuation of the Pi session. If the same live worker had been
reused, the supervisor would still have reconciled its identity and created a new run record.

## 4. Complete only after a passing evaluation

**Supervisor**

```text
Task task_a1b2c3d4e5f6 is complete.

- Run: run_444455556666
- Artifacts: src/middleware/ratelimit.py and tests/test_ratelimit.py
- Verification: 15 focused rate-limit tests passed.
- Independent reviewer: Muse
- Evaluation: eval_667788990011, passed
- Evidence: steady-state enforcement at 100 requests per minute and burst tolerance through 20
  requests both passed.
```

The supervisor recorded the worker result, checked that declared artifacts existed, and obtained an
evaluation from a different agent before reporting success.

## 5. Turn feedback into a proposal

**User**

```text
Record this as a second correction under recurrence key ratelimit.token-bucket: future API rate
limiting work should standardize on token-bucket algorithms. Show me any learning proposal, but do
not apply one.
```

**Supervisor**

```text
Recorded the second correction. Bossmode found repeated evidence under ratelimit.token-bucket and
proposed promotion_778899aabbcc.

- Target layer: skill
- Evidence: two related corrections and a passing independent evaluation
- Status: proposed

No memory, skill, AGENTS.md, or control file was changed.
```

The proposal records a candidate improvement. It is not permission to create or edit the skill.

## 6. Approve the artifact, not only the status

**User**

```text
Accept promotion_778899aabbcc. Implement the proposed rate-limiting skill, make it reviewable, test
it, and obtain independent evidence. Mark it applied only after those checks pass.
```

**Supervisor**

```text
Promotion promotion_778899aabbcc was accepted, implemented, independently checked, and then marked
applied.

- Artifact: .agents/skills/rate-limiting/SKILL.md
- Verification: skill structure and referenced commands passed their checks
- Promotion status: applied
```

The supervisor created and verified the artifact. The Bossmode promotion commands only recorded the
approval and final applied state.

## 7. Keep host changes explicit

**User**

```text
Run Bossmode maintenance and report its findings. Do not install a scheduler.
```

**Supervisor**

```text
Maintenance completed. The registry is healthy, no turns are orphaned, and there are no new
promotion proposals. I did not change the host scheduler.
```

If the user later asks for hourly maintenance, the supervisor first reports the exact scheduler
target, repository path, and log path, then verifies registration after the authorized installation.

## Protocol trace

The conversation maps to this durable sequence:

```text
task create
  -> reconcile and single-task dispatch
  -> run start
  -> optional Herdr bind and correlated turns
  -> run finish
  -> independent evaluate
  -> feedback
  -> promotion proposal
  -> explicit accept
  -> artifact implementation and verification
  -> promotion apply
```

For the exact state, identity, turn-result, and failure rules, see the
[supervisor protocol](agent-workflow.md). For command syntax, see the
[CLI reference](cli-reference.md).
