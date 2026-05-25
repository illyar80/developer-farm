"""
LangGraph State Definitions
----------------------------
Типизированные состояния для каждого слоя графа.
Это основа Goodhart-proof изоляции в LangGraph.
"""
from typing import TypedDict, Annotated, Literal
from typing_extensions import NotRequired
import operator

from contracts import TaskInput, CodeArtifact, Verdict


class GraphState(TypedDict):
    """
    Глобальное состояние графа.
    Передаётся между узлами, сохраняется в checkpoint.
    """
    # Input
    user_spec_path: str
    feature_name: str
    
    # Planning output
    task: NotRequired[TaskInput]
    
    # Execution/Verification loop
    iteration: Annotated[int, operator.add]  # Суммируется при каждом update
    artifacts: Annotated[list[CodeArtifact], operator.add]  # Append-only
    verdicts: Annotated[list[Verdict], operator.add]  # Append-only
    feedback: NotRequired[str]  # Abstract feedback для retry
    
    # Final result
    final_passed: NotRequired[bool]
    total_cost: NotRequired[float]
    
    # Metadata
    thread_id: str
