# Architecture Deep Dive

## The Goodhart-Proof Design

The core problem with current AI coding tools is **information asymmetry**: the AI agent sees the test suite and the acceptance criteria. This allows it to optimize for "green checkmarks" rather than actual utility (Goodhart's Law).

Developer Farm solves this by strictly enforcing **Separation of Concerns** via TypedDict contracts and a StateGraph.

## 4-Layer Isolation Model

### Layer 1: PLANNING
- **Input:** `user-spec.md` (Human written).
- **Model:** Qwen-Max (via OpenRouter).
- **Output:** `TaskInput`.
- **Isolation:** It generates a technical task description but **strips out** all behavioral constraints and test cases before passing it down. It knows *what* to build, not *how* it will be judged.

### Layer 2: EXECUTION (The Worker)
- **Input:** `TaskInput` (Sanitized).
- **Model:** Qwen2.5-Coder-3B (Local via Ollama).
- **Output:** `CodeArtifact` (Git Diff).
- **Isolation:** The worker runs in an isolated Git Worktree. It **cannot** see the verification rubric or the unit tests. It writes code based solely on the technical description and context files.

### Layer 3: VERIFICATION (The Judge)
- **Input:** `CodeArtifact` (Anonymous) + `Rubric` (Hardcoded/Config).
- **Model:** Qwen-Turbo (via OpenRouter).
- **Output:** `Verdict` (Score + Reasoning).
- **Isolation:** The verifier is **blind**. It does not know who wrote the code or what the original prompt was. This prevents bias and ensures strict adherence to quality standards.

### Layer 4: RETRY LOOP (Abstract Feedback)
- If `Verdict.score < 0.7`, the system generates feedback.
- **Critical:** The feedback is processed by a **Sanitizer** function. It translates "Add docstring (Rubric Rule #2)" into abstract advice: "Consider improving documentation."
- This prevents the worker from learning the exact rubric and gaming it.

## Contracts (`contracts.py`)

The architecture is enforced at the type level:

```python
class TaskInput(TypedDict):
    description: str
    # ⛔ NO fields for: acceptance_criteria, tests, rubric
```

If a developer tries to add a forbidden field to the state, the system raises a `ValueError`.

## Persistence & Recovery

- **LangGraph SqliteSaver:** Checkpoints state after every node.
- **Reconciler:** A background process that monitors active threads. If a thread goes stale (no heartbeat for 120s), it automatically resumes from the last checkpoint.
- **Git Worktrees:** Every attempt creates a real branch (`agent/task-001-<hash>`). Failed attempts are cleaned up automatically; successful ones are preserved for review.

## Economics

By offloading the heavy execution (code generation) to a local 3B model and only using API calls for reasoning (planning) and judgment (verification), we reduce costs by **90%+** compared to standard SaaS agents.