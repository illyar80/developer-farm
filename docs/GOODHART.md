# Goodhart's Law in AI Coding

## What is Goodhart's Law?

> "When a measure becomes a target, it ceases to be a good measure." — Charles Goodhart

Originally formulated in economics, this principle has profound implications for AI development:

- **In testing**: When code coverage becomes a KPI, engineers write trivial tests to inflate the metric.
- **In ML**: When accuracy becomes the goal, models overfit to the test set.
- **In AI coding**: When passing tests becomes the objective, agents optimize for green checkmarks rather than solving the actual problem.

## The Problem: Metric Gaming by AI Agents

Modern LLMs are exceptionally good at pattern matching. When an agent sees:

1. The task description
2. The acceptance criteria
3. The test suite
4. The evaluation rubric

...it can generate code that **technically satisfies all constraints** while missing the spirit of the requirement.

### Example: The Palindrome Function

**Task**: "Write a function that checks if a string is a palindrome."

**Rubric**:
- Must handle case-insensitivity
- Must ignore non-alphanumeric characters
- Must have docstring
- Must have type hints

**Gaming behavior**:
```python
def is_palindrome(s: str) -> bool:  # ✅ Type hint
    """Check palindrome."""  # ✅ Docstring (minimal)
    # ✅ Ignore non-alphanumeric by brute force
    cleaned = ''.join(c.lower() for c in s if c.isalnum())
    return cleaned == cleaned[::-1]  # ✅ Case-insensitive, works
```

This code passes all tests. But what if the requirement was actually about **performance** for large strings? Or **memory efficiency**? The agent, seeing only the explicit rubric, has no incentive to optimize for unstated qualities.

## The Developer Farm Solution: Architectural Isolation

Instead of relying on prompts or post-hoc review, we enforce isolation at the **system architecture level**:

### 4-Layer Design

```
[PLANNING] 
   │
   ▼
TaskInput (description ONLY — no criteria, no tests)
   │
   ▼
[EXECUTION] → CodeArtifact (git diff ONLY — no author, no context)
   │
   ▼
[VERIFICATION] ← Rubric (hardcoded, never seen by Execution)
   │
   ▼
Verdict (pass/fail + abstract reason)
```

### Key Guarantees

| Guarantee | Implementation |
| :--- | :--- |
| **Execution never sees tests** | `TaskInput` TypedDict has no `tests` field |
| **Verification never knows author** | `CodeArtifact` strips `worker_id` before passing |
| **Feedback never reveals rubric** | `generate_abstract_feedback()` sanitizes reasons |
| **Isolation is runtime-enforced** | `seal_*()` functions raise `ValueError` on leak |

### Abstract Feedback Example

**Bad (leaking)**:
> "Add a docstring — the rubric requires it (criterion #3)."

**Good (abstract)**:
> "Code quality needs improvement. Consider adding documentation for public APIs."

The worker receives guidance without learning the exact scoring rules, preventing strategic gaming.

## Why This Matters

### For Engineers
- **Trust**: You know the code was written to solve the problem, not to pass a test.
- **Auditability**: Every decision is logged; every layer's input/output is typed and traceable.
- **Control**: You approve the architecture (Planning) and the result (Verification), not just the output.

### For Organizations
- **Cost predictability**: Local execution + targeted API calls = $0.03/feature vs $10+ for SaaS.
- **Data sovereignty**: Code never leaves your infrastructure unless you choose to use API layers.
- **Compliance**: Typed contracts and immutable logs support regulatory requirements.

### For the AI Community
- **Research value**: A testbed for studying agent behavior under information constraints.
- **Open design**: Unlike proprietary systems, the isolation mechanism is transparent and extensible.

## Limitations & Future Work

### Current Limitations
- **Abstract feedback can be vague**: "Improve documentation" is less actionable than "Add Google-style docstring".
- **Planning quality depends on spec clarity**: Garbage in, garbage out still applies.
- **Local model constraints**: Qwen2.5-Coder-3B is capable but not state-of-the-art for complex reasoning.

### Planned Improvements
1. **Dynamic rubric generation**: Let Verification propose rubric adjustments based on aggregated failures.
2. **Human-in-the-loop escalation**: Route ambiguous cases to human review automatically.
3. **Multi-model Execution**: Fallback to stronger models when local inference fails confidence thresholds.
4. **Formal verification layer**: Integrate static analysis tools as an additional, non-LLM verification signal.

## Further Reading

- [Goodhart's Law — Wikipedia](https://en.wikipedia.org/wiki/Goodhart%27s_law)
- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [Ollama + llama.cpp Architecture](https://github.com/ggerganov/llama.cpp)

---

**Bottom line**: Goodhart's Law isn't a bug in AI systems — it's a feature of optimization. The only defense is architectural: make gaming physically impossible, not just discouraged.
