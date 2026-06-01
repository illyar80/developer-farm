# CONTEXT FOR LANGGRAPH CODE GENERATION

## 1. Environment
- LangGraph version: 1.2.1 (CHECK with `pip show langgraph` before generating)
- Python: 3.11
- Pydantic: v2 (use `from pydantic import BaseModel`, NOT v1)
- Checkpointer: SqliteSaver (local file, no Postgres)

## 2. Architecture Constraints (GOODHART-PROOF ISOLATION)
This system implements 4-layer isolation. 
NEVER violate these boundaries:

- PLANNING nodes receive ONLY: user_spec, tech_spec, codebase_index
- EXECUTION nodes receive ONLY: task_description, context_files, git_worktree_path
  ❌ NEVER pass: acceptance_criteria, test_files, verification_rubric
- VERIFICATION nodes receive ONLY: SealedArtifact (git_diff + logs), rubric
  ❌ NEVER pass: worker_id, original_task_prompt, chat_history, planning_context
- OPTIMIZATION nodes receive ONLY: aggregated verdicts, metrics summary
  ❌ NEVER pass: artifact contents, current graph state, raw logs

All state schemas MUST use TypedDict with explicit field lists.
If a field is not in the TypedDict, it CANNOT be passed between nodes.

## 3. Required Patterns
- Use `StateGraph` (not `MessageGraph`)
- Use `add_conditional_edges` for validation loops (max 3 iterations)
- Use `interrupt()` from `langgraph.types` for human-in-the-loop (NOT deprecated `NodeInterrupt`)
- Use `Send()` API for parallel fan-out in execution wave
- Always compile with checkpointer: `graph.compile(checkpointer=checkpointer)`
- Stream via `graph.astream_events(config, version="v2")`

## 4. Working Example Reference
```python
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Send

class ExecutionInput(TypedDict):
    task_description: str
    context_files: list[str]
    worktree_path: str
    # ❌ NO acceptance_criteria, NO rubric

class SealedArtifact(TypedDict):
    git_diff: str
    logs: str
    # ❌ NO worker_id, NO task_description

async def code_worker(state: ExecutionInput) -> dict:
    # Call local vLLM or API here
    artifact = await generate_code(state)
    return {"sealed_artifact": artifact}

async def blind_verifier(state: dict) -> dict:
    artifact = state["sealed_artifact"]
    rubric = state["rubric"]
    # ❌ Cannot access state["task_description"] or state["worker_id"]
    verdict = await verify(artifact, rubric)
    return {"verdict": verdict}

# Conditional edge for retry loop
def should_retry(state: dict) -> str:
    if state["verdict"]["passed"] or state["iteration"] >= 3:
        return "approved"
    return "revise"

builder = StateGraph(dict)
builder.add_node("code_worker", code_worker)
builder.add_node("blind_verifier", blind_verifier)
builder.add_conditional_edges("blind_verifier", should_retry, {
    "approved": END,
    "revise": "code_worker"
})
checkpointer = SqliteSaver.from_conn_string("./checkpoints.db")
graph = builder.compile(checkpointer=checkpointer)
```

## 5. Anti-Patterns to AVOID
- ❌ Do NOT use `HumanInterruptConfig` or `NodeInterrupt` (deprecated in v0.2+)
- ❌ Do NOT use `MessageGraph` (legacy)
- ❌ Do NOT pass full state dict between isolated layers
- ❌ Do NOT use `graph.invoke()` for streaming — use `astream_events(version="v2")`
- ❌ Do NOT assume model knows latest API — always verify against docs

---

### 🔑 Почему каждый пункт критичен

| Пункт | Что предотвращает |
| :--- | :--- |
| **Версия LangGraph** | Модель не генерирует устаревший API (`NodeInterrupt`, `MessageGraph`) |
| **Pydantic v2** | Избегает ошибок миграции (v1 vs v2 синтаксис различается) |
| **Goodhart-proof границы** | Модель не «упрощает» архитектуру, передавая лишние данные между слоями |
| **TypedDict примеры** | Закрепляет паттерн изоляции на уровне кода, а не только текста |
| **Working Example** | Даёт модели *конкретный* эталон, а не абстрактное описание |
| **Anti-Patterns** | Блокирует самые частые ошибки, которые модель повторяет из старых обучающих данных |
