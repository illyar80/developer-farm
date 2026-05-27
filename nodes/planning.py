"""
PLANNING LAYER — Spec-to-Task Converter with Neo4j Graph Analysis
-----------------------------------------------------------------

This is the entry point of the Developer Farm pipeline. It transforms
human-written specifications into structured TaskInput objects that
feed the Execution layer.

GOODHART-PROOF GUARANTEES:
- TaskInput NEVER contains acceptance_criteria, test_cases, or rubric
- Only technical descriptions and context files are passed downstream
- The seal_task_for_execution() function enforces this at runtime

NEO4J ENHANCEMENT:
- Builds a dependency graph of the codebase (AST-based)
- Finds related files via Cypher queries (imports, inheritance, calls)
- Provides precise context_files instead of guessing
- Falls back to empty context if Neo4j is unavailable

ARCHITECTURAL ISOLATION:
- This layer sees: user-spec.md, codebase
- This layer NEVER sees: execution results, verification verdicts
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from rich.console import Console

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from contracts import TaskInput, seal_task_for_execution

# Optional Neo4j import with graceful degradation
try:
    from utils.code_graph import CodeGraph
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False

console = Console()


# ═══════════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Instructions for the Planning LLM
# ═══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a technical planning agent. Your job is to analyze
user specifications and generate precise, atomic tasks for code generation.

Your output MUST be a JSON object with these exact fields:
- task_id: string (format: "task-XXX" where XXX is a 3-digit number)
- description: string (TECHNICAL description of what to implement)
- context_files: list of strings (paths to existing files for context)
- language: string (programming language: "python", "javascript", "typescript", etc.)
- target_path: string (where the new code should be written)

CRITICAL RULES:
1. Description must be TECHNICAL (how to implement), not BEHAVIORAL (what tests check)
2. Do NOT include acceptance criteria, test cases, or rubric in description
3. Context files should be paths to existing files in the project
4. Target path should follow project conventions
5. Output ONLY the JSON object, no markdown fences, no explanations

Example output:
{
  "task_id": "task-001",
  "description": "Create a Python module with function `is_palindrome(s: str) -> bool` that normalizes input and checks symmetry.",
  "context_files": ["src/utils/__init__.py"],
  "language": "python",
  "target_path": "src/utils/palindrome.py"
}"""


# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def _extract_target_path_from_spec(spec: str) -> Optional[str]:
    """
    Extract a target file path hint from the specification text.
    
    Uses heuristic pattern matching to find references like:
    - "modify src/auth/jwt.py"
    - "update the file in src/utils/helpers.py"
    - "add to app/models/user.py"
    
    Args:
        spec: Raw user specification text
    
    Returns:
        Extracted path or None if not found
    """
    # Common patterns that reference file paths
    patterns = [
        r"(?:modify|update|change|edit|refactor)\s+([\w/./-]+\.\w{1,5})",
        r"in\s+(?:the\s+)?(?:file\s+)?([\w/./-]+\.\w{1,5})",
        r"file\s+([\w/./-]+\.\w{1,5})",
        r"path\s*[:=]\s*([\w/./-]+\.\w{1,5})",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, spec, re.IGNORECASE)
        if match:
            path = match.group(1).strip()
            # Basic validation: should contain at least one / or be a valid filename
            if "/" in path or "." in path:
                return path
    
    return None


def _clean_json_response(raw_output: str) -> str:
    """
    Strip markdown code fences and whitespace from LLM JSON response.
    
    Handles common formatting variations:
    - ```json { ... } ```
    - ``` { ... } ```
    - Bare JSON object
    
    Args:
        raw_output: Raw LLM response text
    
    Returns:
        Clean JSON string ready for parsing
    """
    cleaned = raw_output.strip()
    
    # Remove leading ```json or ```
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    
    # Remove trailing ```
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    
    return cleaned.strip()


def _get_neo4j_context(
    target_path: Optional[str],
    project_root: str = "."
) -> list[str]:
    """
    Query Neo4j graph to find files related to target_path.
    
    This is the key innovation: instead of blindly passing all files
    or guessing which ones matter, we use AST-based dependency analysis
    to find precisely the files that the target module depends on.
    
    Args:
        target_path: Path to the file being modified/created
        project_root: Root directory of the codebase
    
    Returns:
        List of related file paths (max 10), empty list if Neo4j unavailable
    """
    # Check if Neo4j integration is enabled
    use_neo4j = os.getenv("USE_NEO4J", "true").lower() == "true"
    if not use_neo4j or not NEO4J_AVAILABLE:
        if not NEO4J_AVAILABLE:
            console.print("[yellow]⚠ Neo4j not available (install neo4j package)[/]")
        return []
    
    try:
        # Connect to Neo4j using environment variables
        graph = CodeGraph(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "developer-farm-2026"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )
        
        # Index the codebase (idempotent operation)
        console.print("[cyan]📂 Indexing codebase in Neo4j...[/]")
        graph.index_codebase(project_root)
        
        # If we have a target path, find related files via graph traversal
        related_files = []
        if target_path:
            console.print(f"[cyan]🔍 Finding files related to {target_path}...[/]")
            related_files = graph.find_related_files(target_path, depth=2)
            console.print(f"[green]📊 Found {len(related_files)} related files[/]")
        else:
            console.print("[yellow]⚠ No target path in spec, skipping graph query[/]")
        
        graph.close()
        
        # Limit to 10 files to avoid context window explosion
        return related_files[:10]
        
    except Exception as e:
        console.print(f"[yellow]⚠ Neo4j query failed, falling back: {e}[/]")
        return []


# ═══════════════════════════════════════════════════════════════════
# MAIN PLANNING FUNCTION
# ═══════════════════════════════════════════════════════════════════

async def plan(
    user_spec_path: Path,
    feature_name: Optional[str] = None,
) -> TaskInput:
    """
    Read a user specification and generate a TaskInput via Qwen-Max API.
    
    This function is the bridge between human intent (user-spec.md)
    and machine execution (TaskInput). It uses Neo4j to intelligently
    select context files based on code dependency analysis.
    
    GOODHART-PROOF GUARANTEE:
    The returned TaskInput will NEVER contain acceptance_criteria,
    test_cases, rubric, or any evaluation metrics. The seal function
    enforces this at runtime.
    
    Args:
        user_spec_path: Path to the user specification markdown file
        feature_name: Optional name for the feature (defaults to parent dir)
    
    Returns:
        TaskInput TypedDict (sealed, no forbidden fields)
    
    Raises:
        FileNotFoundError: If user_spec_path doesn't exist
        ValueError: If LLM response is not valid JSON
        RuntimeError: If API call fails
    """
    # ─── Validate input ──────────────────────────────────────────────
    if not user_spec_path.exists():
        raise FileNotFoundError(f"User spec not found: {user_spec_path}")
    
    user_spec = user_spec_path.read_text(encoding="utf-8")
    feature_name = feature_name or user_spec_path.parent.name
    
    console.print(f"\n[bold cyan]═══ PLANNING: {feature_name} ═══[/]\n")
    console.print(f"📄 Reading: {user_spec_path}")
    console.print(f"📝 Spec length: {len(user_spec)} chars\n")
    
    # ─── Extract target path hint from spec (for Neo4j query) ───────
    target_path_hint = _extract_target_path_from_spec(user_spec)
    if target_path_hint:
        console.print(f"🎯 Target path hint: {target_path_hint}")
    
    # ─── Neo4j: Analyze code dependencies ────────────────────────────
    neo4j_context = _get_neo4j_context(target_path_hint, project_root=".")
    
    # ─── Build the prompt for Qwen-Max ───────────────────────────────
    context_info = ""
    if neo4j_context:
        context_info = (
            "\n\nAvailable context files (discovered via Neo4j graph analysis):\n"
            + "\n".join(f"- {f}" for f in neo4j_context)
            + "\n\nPrefer these files when selecting context_files for the task."
        )
    
    full_system_prompt = SYSTEM_PROMPT + context_info
    
    # ─── Initialize the LLM client ───────────────────────────────────
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set in environment. "
            "Get one at: https://openrouter.ai/keys"
        )
    
    llm = ChatOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        model=os.getenv("OPENROUTER_MODEL", "qwen/qwen3.7-max"),
        temperature=0.3,
        max_tokens=1024,
        request_timeout=60,
    )
    
    # ─── Call the LLM ────────────────────────────────────────────────
    console.print(f"🧠 Calling {llm.model_name} (OpenRouter)...")
    
    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [
                SystemMessage(content=full_system_prompt),
                HumanMessage(content=f"User Specification:\n\n{user_spec}"),
            ],
        )
        raw_output = response.content.strip()
        console.print(f"✅ Response received: {len(raw_output)} chars\n")
    except Exception as e:
        console.print(f"[red]❌ LLM API call failed: {e}[/]")
        raise
    
    # ─── Parse JSON response ─────────────────────────────────────────
    cleaned_output = _clean_json_response(raw_output)
    
    try:
        task_dict = json.loads(cleaned_output)
    except json.JSONDecodeError as e:
        console.print(f"[red]❌ JSON parse error: {e}[/]")
        console.print(f"[dim]Raw output:\n{raw_output[:500]}[/]")
        raise ValueError(f"LLM response is not valid JSON: {e}")
    
    # ─── Enhance with Neo4j context if LLM didn't provide any ────────
    if neo4j_context and not task_dict.get("context_files"):
        task_dict["context_files"] = neo4j_context
        console.print(
            f"[cyan]ℹ Added Neo4j context: {len(neo4j_context)} files[/]"
        )
    
    # ─── Ensure all required fields exist ────────────────────────────
    required_fields = ["task_id", "description", "context_files", "language", "target_path"]
    for field in required_fields:
        if field not in task_dict:
            raise ValueError(f"Missing required field in LLM response: {field}")
    
    # ─── Seal the task (Goodhart-proof barrier) ─────────────────────
    # This function strips any forbidden fields and validates the structure.
    # If someone tried to add acceptance_criteria, it will raise ValueError.
    console.print("🔒 Sealing task (removing forbidden fields)...")
    sealed_task = seal_task_for_execution(task_dict)
    
    # ─── Display the result ─────────────────────────────────────────
    console.print(f"\n[bold green]═══ TASK GENERATED ═══[/]")
    console.print(f"ID: {sealed_task['task_id']}")
    console.print(f"Language: {sealed_task['language']}")
    console.print(f"Target: {sealed_task['target_path']}")
    console.print(f"Context files: {len(sealed_task['context_files'])}")
    
    if sealed_task["context_files"]:
        for cf in sealed_task["context_files"][:5]:
            console.print(f"  [dim]• {cf}[/]")
        if len(sealed_task["context_files"]) > 5:
            console.print(
                f"  [dim]... and {len(sealed_task['context_files']) - 5} more[/]"
            )
    
    console.print(f"\nDescription preview:")
    console.print(f"  [dim]{sealed_task['description'][:200]}...[/]")
    
    # ─── Final Goodhart-proof verification ───────────────────────────
    forbidden = {"acceptance_criteria", "test_cases", "rubric", "worker_id"}
    leaked = forbidden & set(sealed_task.keys())
    if leaked:
        console.print(f"\n[bold red]🚫 GOODHART VIOLATION: {leaked}[/]")
        raise ValueError(f"Planning leaked forbidden fields: {leaked}")
    else:
        console.print(f"\n[green]✅ Goodhart-proof: no forbidden fields[/]")
    
    return sealed_task


# ═══════════════════════════════════════════════════════════════════
# STANDALONE TEST ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run this module directly to test planning in isolation:
    
        python -m nodes.planning
    
    Expected output:
    - Reads work/mvp/user-spec.md
    - Calls Qwen-Max via OpenRouter
    - Generates TaskInput
    - Saves to work/mvp/results/task-input.json
    """
    from dotenv import load_dotenv
    load_dotenv()
    
    # Validate API key
    if not os.getenv("OPENROUTER_API_KEY"):
        console.print("[red]❌ OPENROUTER_API_KEY not set in .env[/]")
        console.print("[dim]Get your key at: https://openrouter.ai/keys[/]")
        exit(1)
    
    # Locate test spec
    test_spec = Path("work/mvp/user-spec.md")
    if not test_spec.exists():
        console.print(f"[red]❌ Test spec not found: {test_spec}[/]")
        console.print("\n[yellow]Create one with:[/]")
        console.print("  mkdir -p work/mvp")
        console.print("  cat > work/mvp/user-spec.md << 'EOF'")
        console.print("# Feature: Calculator Module\n")
        console.print("Create a simple Python module with basic arithmetic.\n")
        console.print("## Goals")
        console.print("- add, subtract, multiply, divide functions")
        console.print("- Handle division by zero")
        console.print("- Include type hints\n")
        console.print("## Constraints")
        console.print("- Python 3.11+, pure functions only")
        console.print("EOF")
        exit(1)
    
    console.print("\n[bold magenta]═══ PLANNING NODE TEST ═══[/]\n")
    
    try:
        task = asyncio.run(plan(test_spec))
        
        # Save result for inspection
        output_path = Path("work/mvp/results/task-input.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(task, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"\n💾 Saved to: {output_path}")
        
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Interrupted by user[/]")
        exit(130)
    except Exception as e:
        console.print(f"[red]❌ Planning failed: {e}[/]")
        import traceback
        traceback.print_exc()
        exit(1)