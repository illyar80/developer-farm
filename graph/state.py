"""
LangGraph State Definitions
----------------------------
Typed state definitions for each graph layer.
This is the foundation of Goodhart-proof isolation in LangGraph.
"""

import operator
from typing import Annotated, Literal, TypedDict

from typing_extensions import NotRequired

from contracts import CodeArtifact, TaskInput, Verdict


class GraphState(TypedDict):
    """
    Global graph state.
    Passed between nodes and persisted in checkpoints.
    """

    # Input
    user_spec_path: str
    feature_name: str

    # Planning output
    task: NotRequired[TaskInput]

    # Execution/Verification loop
    iteration: Annotated[int, operator.add]  # Accumulates on each update
    artifacts: Annotated[list[CodeArtifact], operator.add]  # Append-only
    verdicts: Annotated[list[Verdict], operator.add]  # Append-only
    feedback: NotRequired[str]  # Abstract feedback for retries

    # Final result
    final_passed: NotRequired[bool]
    total_cost: NotRequired[float]

    # Metadata
    thread_id: str
