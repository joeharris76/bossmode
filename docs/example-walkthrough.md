# Example Walkthrough: Prompt-by-Prompt Execution

This document illustrates how **Bossmode** operates from the user's perspective. It presents a realistic interactive dialogue showing:
1. **The User Prompt** (the natural language command or request).
2. **Behind the Scenes** (the control plane CLI commands, SQLite state transitions, Herdr worker interactions, and evaluation gates).
3. **The User-Facing Response** (the concise status, evidence, or approval request shown to the user).

---

## Scenario Overview

* **Goal**: Implement a token bucket rate limiter in auth middleware.
* **Supervisor**: Any coordinator agent (e.g. Antigravity/AGY, Codex, Claude, Pi, Grok, Muse).
* **Worker Run 1**: Interactive external agent (`pi` via Herdr).
* **Reviewer Run 1**: Independent evaluator (`grok`).
* **Worker Run 2 (Remediation)**: Interactive external agent (`claude` via Herdr).
* **Reviewer Run 2**: Independent evaluator (`muse`).

```text
User Request
  -> Supervisor Coordinator (AGY)
       -> bossmode CLI / SQLite registry (.bossmode/control.db)
       -> Worker Agent (pi) --------------> Run 1 (Defect: Fixed window, no burst)
       -> Independent Reviewer (grok) ----> Evaluation 1: FAILED
       -> User Feedback ------------------> Recurrence Key: ratelimit.token-bucket
       -> Worker Agent (claude) ----------> Run 2 (Remediation & Token Bucket Fix)
       -> Independent Reviewer (muse) ----> Evaluation 2: PASSED
       -> Supervisor Tick ----------------> Propose 'skill' promotion
       -> User Approval ------------------> Promotion accepted & applied
```

---

## Turn 1: Initial Request & Task Dispatch

### 💬 User Prompt
> "Please implement rate limiting for our auth endpoints in `src/middleware/ratelimit.py`. It needs to enforce 100 req/min and handle burst traffic of up to 20 requests."

---

### ⚙️ Behind the Scenes (Bossmode Engine)

1. **Initialize Registry** (idempotent SQLite setup in WAL mode):
   ```bash
   uv run bossmode init
   ```

2. **Add Task** with explicit success criteria and permission limits:
   ```bash
   uv run bossmode task add \
     --title "Implement Rate Limiting" \
     --goal "Implement token bucket algorithm in auth middleware" \
     --success-criteria "Token bucket handles burst of 20 reqs and enforces 100 req/min" \
     --priority 10 \
     --permissions-json '{"filesystem":["src/middleware/*","tests/*"],"network":false}'
   ```
   *Registry records `task_a1b2c3d4e5f6` in state `ready`.*

3. **Supervisor Reconciliation**:
   ```bash
   uv run bossmode supervisor tick
   ```
   *`dispatch` returns `task_a1b2c3d4e5f6` (single-flight scheduling).*

4. **Reserve Run & Bind Herdr Worker**:
   ```bash
   # Reserve run ID in SQLite
   uv run bossmode run start task_a1b2c3d4e5f6 \
     --role worker_pi \
     --model pi-base

   # Create Herdr pane & start agent
   herdr pane split p_root --direction right --cwd "$PWD" --no-focus
   herdr agent start worker_ratelimit_pi --kind pi --pane p_worker1

   # Bind live worker and native session reference
   uv run bossmode herdr bind run_111122223333 \
     --herdr-session bossmode \
     --worker worker_ratelimit_pi \
     --kind pi \
     --pane-id p_worker1 \
     --session-source "herdr:pi" \
     --session-agent "pi" \
     --session-ref-kind "id" \
     --session-value "sess_pi_101"
   ```

5. **Open Turn & Prompt with Envelope**:
   ```bash
   uv run bossmode turn start run_111122223333 \
     --purpose task \
     --prompt "Implement rate limiting for auth endpoints supporting 100 req/min and 20 burst"
   ```

   *The supervisor wraps the prompt into an envelope and sends it to Herdr:*
   ```bash
   herdr agent prompt worker_ratelimit_pi \
     "Complete the bounded task below. Write exactly one JSON result to .bossmode/turns/turn_9876543210ab.json.
   The JSON turn_id must equal turn_9876543210ab. Do not claim success unless declared artifacts exist.

   Logical task: Implement rate limiting for auth endpoints supporting 100 req/min and 20 burst" \
     --wait
   ```

6. **Worker Writes Result File**:
   Worker writes `.bossmode/turns/turn_9876543210ab.json`:
   ```json
   {
     "turn_id": "turn_9876543210ab",
     "status": "succeeded",
     "summary": "Implemented fixed-window rate limiter in src/middleware/ratelimit.py",
     "artifacts": [{"path": "src/middleware/ratelimit.py", "kind": "code"}]
   }
   ```

7. **Validate Turn & Complete Run**:
   ```bash
   uv run bossmode turn finish turn_9876543210ab \
     --status succeeded \
     --lifecycle-evidence done

   uv run bossmode run finish run_111122223333 \
     --outcome succeeded \
     --summary "Worker completed initial rate limiter" \
     --artifacts-json '[{"path":"src/middleware/ratelimit.py","kind":"code"}]'
   ```
   *Task moves from `running` to `evaluating`.*

---

### 🖥️ User-Facing Response
> **Supervisor:** "Worker (`pi`) completed an initial implementation in `src/middleware/ratelimit.py`. Submitting the implementation to an independent reviewer for verification..."

---

## Turn 2: Independent Evaluation & Rejection

### 💬 User Prompt
*(The user waits as the supervisor automatically executes the evaluation gate)*

---

### ⚙️ Behind the Scenes (Bossmode Engine)

1. **Independent Reviewer Evaluates Run 1**:
   The supervisor invokes `reviewer_grok` (`evaluator != worker_pi`) to run test assertions against the task's criteria:
   ```bash
   uv run bossmode evaluate task_a1b2c3d4e5f6 \
     --run-id run_111122223333 \
     --evaluator reviewer_grok \
     --failed \
     --evidence "Burst test failed: requests rejected immediately without handling burst bucket capacity" \
     --notes "Implementation used a fixed-window counter instead of a token bucket"
   ```
   *Task transitions from `evaluating` to `failed`.*

2. **Supervisor Tick Identifies Blocker**:
   ```bash
   uv run bossmode supervisor tick
   ```
   *Tick indicates task `task_a1b2c3d4e5f6` is in `failed` state and needs remediation.*

---

### 🖥️ User-Facing Response
> **Supervisor:** "❌ **Evaluation Failed:** The independent reviewer (`grok`) rejected Run 1.
> * **Evidence:** Burst traffic test failed. Requests were rejected immediately without accommodating the 20-request burst window.
> * **Root Cause:** A fixed-window counter was implemented instead of a token bucket algorithm.
> 
> Would you like me to retry the task with explicit token bucket requirements?"

---

## Turn 3: User Feedback & Remediation Cycle

### 💬 User Prompt
> "Yes, retry it. Make sure it specifically implements a token bucket algorithm that accommodates the burst capacity."

---

### ⚙️ Behind the Scenes (Bossmode Engine)

1. **Record Structured Feedback with Recurrence Key**:
   ```bash
   uv run bossmode feedback task_a1b2c3d4e5f6 \
     --run-id run_111122223333 \
     --kind correction \
     --key ratelimit.token-bucket \
     --content "Must implement token bucket with burst capacity, not fixed window"
   ```

2. **Transition Task to Ready**:
   ```bash
   uv run bossmode task transition task_a1b2c3d4e5f6 ready \
     --actor supervisor \
     --reason "User requested retry with token bucket guidance" \
     --next-action "Re-dispatch worker with token bucket guidance"
   ```

3. **Start Run 2 & Bind Remediation Worker**:
   ```bash
   uv run bossmode run start task_a1b2c3d4e5f6 --role worker_claude
   # Returns run_444455556666

   herdr pane split p_root --direction right --cwd "$PWD" --no-focus
   herdr agent start worker_ratelimit_claude --kind claude --pane p_worker2

   uv run bossmode herdr bind run_444455556666 \
     --herdr-session bossmode \
     --worker worker_ratelimit_claude \
     --kind claude \
     --pane-id p_worker2 \
     --session-source "herdr:claude" \
     --session-agent "claude" \
     --session-ref-kind "id" \
     --session-value "sess_claude_999"
   ```

4. **Start Remediation Turn**:
   ```bash
   uv run bossmode turn start run_444455556666 \
     --purpose correction \
     --prompt "Refactor rate limiter to token bucket algorithm supporting burst capacity of 20 and 100 req/min"
   ```

   *Worker updates `src/middleware/ratelimit.py`, creates `tests/test_ratelimit.py`, and writes `.bossmode/turns/turn_fedcba987654.json`:*
   ```json
   {
     "turn_id": "turn_fedcba987654",
     "status": "succeeded",
     "summary": "Implemented token bucket with burst capacity parameter and unit test suite",
     "artifacts": [
       {"path": "src/middleware/ratelimit.py", "kind": "code"},
       {"path": "tests/test_ratelimit.py", "kind": "test"}
     ]
   }
   ```

5. **Finish Turn & Run 2**:
   ```bash
   uv run bossmode turn finish turn_fedcba987654 --status succeeded --lifecycle-evidence done
   uv run bossmode run finish run_444455556666 \
     --outcome succeeded \
     --summary "Token bucket algorithm implemented with unit tests" \
     --artifacts-json '[{"path":"src/middleware/ratelimit.py","kind":"code"},{"path":"tests/test_ratelimit.py","kind":"test"}]'
   ```

6. **Independent Evaluation (Passing)**:
   ```bash
   uv run bossmode evaluate task_a1b2c3d4e5f6 \
     --run-id run_444455556666 \
     --evaluator reviewer_muse \
     --passed \
     --score 1.0 \
     --evidence "All 15 burst and rate-limiting test assertions passed in tests/test_ratelimit.py"
   ```
   *Task transitions from `evaluating` to `succeeded`.*

---

### 🖥️ User-Facing Response
> **Supervisor:** "✅ **Task Complete:** Rate limiting successfully implemented and verified.
> * **Artifacts:**
>   * `src/middleware/ratelimit.py` (Token bucket implementation)
>   * `tests/test_ratelimit.py` (15 passing unit tests)
> * **Verification:** Independent evaluation passed by `muse` (score: 1.0) — verified steady-state enforcement at 100 req/min and burst tolerance up to 20 requests."

---

## Turn 4: Continual Learning Promotion Proposal

### 💬 User Prompt
> "Great. For any future rate limiting work, let's always make sure we standardize on token bucket algorithms."

---

### ⚙️ Behind the Scenes (Bossmode Engine)

1. **Ingest User Preference / Second Correction**:
   ```bash
   uv run bossmode feedback task_a1b2c3d4e5f6 \
     --kind correction \
     --key ratelimit.token-bucket \
     --content "Token bucket algorithm should be standard across all API endpoints"
   ```

2. **Supervisor Tick Detects Learning Opportunity**:
   ```bash
   uv run bossmode supervisor tick
   ```
   *The registry detects 2 corrections under `ratelimit.token-bucket` with at least 1 passing evaluation. It generates a `skill` promotion proposal:*
   ```json
   {
     "new_promotion_proposals": [
       {
         "id": "promotion_778899aabbcc",
         "recurrence_key": "ratelimit.token-bucket",
         "target_layer": "skill",
         "status": "proposed",
         "rationale": "Repeated correction appeared 2 times with 1 passing evaluation(s); propose a tested reusable workflow.",
         "evidence": {
           "evaluation_ids": ["eval_001122334455", "eval_667788990011"],
           "feedback_ids": ["feedback_111", "feedback_222"],
           "task_ids": ["task_a1b2c3d4e5f6"]
         }
       }
     ]
   }
   ```

---

### 🖥️ User-Facing Response
> **Supervisor:** "I noticed recurring corrections regarding token bucket rate limiting with verified passing test evidence.
> 
> I have prepared a **Skill Promotion Proposal**:
> * **ID:** `promotion_778899aabbcc`
> * **Layer:** `skill` (reusable procedure)
> * **Key:** `ratelimit.token-bucket`
> * **Proposed Change:** Create `.agents/skills/rate-limiting/SKILL.md` defining standard token bucket implementation and testing procedures.
> 
> Would you like to accept and apply this promotion?"

---

## Turn 5: User Approval & Promotion Application

### 💬 User Prompt
> "Approved. Apply the skill."

---

### ⚙️ Behind the Scenes (Bossmode Engine)

1. **Accept Promotion**:
   ```bash
   uv run bossmode promotion set promotion_778899aabbcc accepted
   ```

2. **Write Tested Skill Document**:
   The supervisor creates `.agents/skills/rate-limiting/SKILL.md` detailing the token bucket pattern, parameter contracts, and test fixtures.

3. **Mark Promotion Applied**:
   ```bash
   uv run bossmode promotion set promotion_778899aabbcc applied
   ```

---

### 🖥️ User-Facing Response
> **Supervisor:** "✅ Promotion `promotion_778899aabbcc` accepted and applied.
> * **Durable Asset:** [`.agents/skills/rate-limiting/SKILL.md`](file:///Users/joe/Developer/continual-agent-mvp/.agents/skills/rate-limiting/SKILL.md)
> 
> Future agent sessions will automatically discover this skill when implementing API middleware."

---

## Summary of Key Features Demonstrated

| Feature | Where Demonstrated |
|---|---|
| **Durable SQLite State** | Tasks, runs, turns, evaluations, feedback, and promotions persisted transactionally. |
| **Supervisor Loop** | `bossmode supervisor tick` coordinates single-flight dispatch and surfaces blockers. |
| **Herdr Worker Binding** | Live process identity and native session `{source, agent, kind, value}` bound across turns. |
| **Exact Turn Correlation** | Enveloped prompts require exact JSON result at `.bossmode/turns/<turn_id>.json`. |
| **Independent Evaluation Gate** | `evaluator != worker` gate prevents self-approval and caught the missing burst support. |
| **Remediation Cycle** | Reused existing worker session with correction prompt without losing audit trail. |
| **Gated Promotion Ladder** | Repeated corrections + passing evaluation promoted to a durable `skill` with human approval. |
