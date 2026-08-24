# ADR 0002: Agent Organizational Topologies and Conway's Law

**Status:** Under consideration (deferred post-MVP)

---

## 1. Context and Problem Statement

Conway's Law states: *"Organizations which design systems are constrained to produce designs which are copies of the communication structures of these organizations."*

In multi-agent systems like Bossmode, agent communication structures and permission boundaries dictate the resulting software architecture. As Bossmode evolves beyond the single-flight supervisor MVP, we evaluated how to structure agent teams to intentionally shape software architecture (the *Inverse Conway Maneuver*) without creating unproductive agent bureaucracy.

---

## 2. Analyzed Organizational Topologies

### Option A: Product-Aligned Teams (Vertical Bounded Contexts)
Agents are organized around business domains (e.g., Auth, Billing, Core Engine). Each team has a Domain Manager, Workers, and a Reviewer owning a dedicated directory subtree and interface spec.

* **Architecture Outcome:** Highly modular, loosely coupled architecture (microservices or modular monolith with clean bounded contexts).
* **Pros:**
  * Enables safe parallel execution across disjoint filesystem paths (e.g., `src/auth/**` vs `src/billing/**`).
  * Scopes context windows and memories strictly to domain logic, reducing hallucinations.
  * Localizes failure blast radius to the affected subsystem.
  * Scopes continual learning promotions (`feedback -> skill`) to the relevant domain.
* **Cons & Risks:**
  * Risk of code duplication across teams (e.g., reinventing helper utilities).
  * High latency and token overhead if hierarchical managers relay messages interactively ("Telephone Game").
  * Inter-team dependency deadlocks if cross-boundary contract changes stall.

### Option B: Functional Teams (Horizontal Technical Layers)
Agents are organized by technical layer or discipline (e.g., Architecture/Spec, Database/Backend, Frontend/UI, QA/Security). Tasks flow sequentially across layers like an assembly line.

* **Architecture Outcome:** N-tier layered architecture with centralized schemas, shared design systems, and standardized protocols.
* **Pros:**
  * Maximizes model specialization (e.g., deep reasoning for DB/Backend, multimodal for UI, deterministic for QA).
  * Enforces global consistency and eliminates duplicate utility code.
  * Promotes technical rules and skills (e.g., SQL safety, React performance) globally across all tasks.
  * Native alignment with Bossmode's independent evaluation gate (QA/Reviewers are structurally independent).
* **Cons & Risks:**
  * High handoff latency (Waterfall assembly line requiring 4–5 sequential agent steps per feature).
  * Severe horizontal file write collisions (all backend tasks touch shared `routes.py`, `models.py`, or migrations), forcing serialized execution.
  * Loss of product context across handoffs and potential ping-pong blame loops.

### Option C: Hybrid Matrix (Vertical Execution with Horizontal Quality Gates)
Single workers implement vertical feature slices within bounded contexts, while specialized horizontal gates handle independent verification, contract validation, and cross-cutting standards.

* **Architecture Outcome:** Clean modular components with enforced global security, data integrity, and interface consistency.
* **Pros:**
  * Avoids assembly line handoff latency while maintaining strict quality gates.
  * Reconciles global technical skills with local product context.
  * Balances parallel feature velocity with architectural governance.

---

## 3. Comparison Matrix

| Dimension | Option A: Product-Aligned (Vertical) | Option B: Functional-Aligned (Horizontal) | Option C: Hybrid Matrix |
|---|---|---|---|
| **Conway's Law Result** | Modular / Bounded Contexts | Layered / N-Tier | Modular with Global Standards |
| **Feature Velocity** | Fast (Single domain owns the slice) | Slow (Waterfall handoffs) | Fast (Single worker executes slice) |
| **Concurrency Potential** | High (Disjoint directory locking) | Low (Horizontal file collisions) | High (Disjoint directory locking) |
| **Code Consistency** | Lower (Potential silo duplication) | High (Centralized standards) | High (Enforced via review gates) |
| **Model Specialization** | Medium | High | High (Specialized review/eval models) |
| **Coordination Tax** | Medium (Inter-domain APIs) | High (Sequential layer relay) | Low (Contract-first async handoffs) |
| **Continual Learning** | Domain-scoped skills | Global discipline skills | Global controls + local skills |

---

## 4. Decision & Future Guidance

1. **Retain Current MVP Architecture:** For the current spike, keep Bossmode's lightweight, single-flight coordinator model (`.bossmode/control.db`). Do not add multi-tiered agent hierarchies or background management loops at this stage.
2. **Post-MVP Scaling Model:** When multi-agent concurrency is implemented, adopt **Option C (Hybrid Matrix)**:
   * **Declarative Bounded Contexts:** Define team boundaries via path permissions in task definitions rather than through interactive manager chat trees.
   * **Contract-First Inter-Team Dependencies:** Require cross-team dependencies to be expressed as mockable interface contracts (e.g., OpenAPI, Protobuf, TypeScript types).
   * **Independent Functional Evaluators:** Use specialized evaluators (`evaluator != worker`) for security, data migration safety, and automated test gates.

---

## 5. Implementation Status

**No modifications have been applied to the MVP implementation.** The code, SQLite schema, CLI interface, and supervisor harness remain unchanged in their minimal single-flight state.
