"""
EXECUTION LAYER — Goodhart-Proof Code Generator
-----------------------------------------------
Receives ONLY the task description and context files.
It does NOT receive `acceptance_criteria`, tests, rubric, or `worker_id`.

Returns a sealed artifact (`git diff` + logs) without `worker_id`.
"""

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
from contracts import CodeArtifact, TaskInput, seal_artifact_for_verification
from utils.git_worktree import (
    apply_diff_to_worktree,
    cleanup_worktree,
    commit_worktree,
    create_worktree,
    get_diff_from_main,
)

console = Console()

# ─── Prompts (with no mention of tests or evaluation criteria) ─────────────
SYSTEM_PROMPT = """You are a precise code generator. You receive:
1. A task description (what needs to be implemented)
2. Context files from the codebase (for style/structure reference)
3. A target path where the new code should be placed

Your job:
- Write clean, idiomatic code for the specified language
- Follow patterns from context files
- Include type hints where appropriate
- Handle errors gracefully
- Output ONLY the code, wrapped in markdown code blocks with language tag

Output format:
```language:path/to/file.py
<code here>
```

If multiple files are needed, output multiple code blocks.
Do NOT write tests. Do NOT write explanations outside code blocks."""


def _build_user_prompt(task: TaskInput, context_contents: dict[str, str]) -> str:
    """
    Build the worker prompt.
    ⛔ It must not mention tests, evaluation criteria, or rubric.
    """
    parts = [f"# Task\n{task['description']}\n"]
    parts.append(f"# Language\n{task['language']}\n")
    parts.append(f"# Target Path\n{task['target_path']}\n")

    if context_contents:
        parts.append("# Context Files\n")
        for path, content in context_contents.items():
            parts.append(f"## {path}\n```{task['language']}\n{content}\n```\n")

    return "\n".join(parts)


def _read_context_files(files: list[str]) -> dict[str, str]:
    """Read context files and ignore missing ones."""
    contents: dict[str, str] = {}
    for path in files:
        p = Path(path)
        if p.exists() and p.is_file():
            try:
                contents[str(p)] = p.read_text()
            except Exception as e:
                console.print(f"[yellow]⚠ Cannot read {p}: {e}[/]")
        else:
            console.print(f"[yellow]⚠ Context file not found: {p}[/]")
    return contents


def _sanitize_generated_path(candidate_path: str, fallback_path: str) -> str:
    """
    Normalize a path returned by the LLM.

    If the model returns a comment, an absolute path, a traversal path,
    or an excessively long file name, use `fallback_path` instead.
    """
    candidate = candidate_path.strip().strip("`'\"")
    candidate = candidate.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]

    if not candidate or candidate.startswith("/") or len(candidate) > 180:
        return fallback_path

    if re.fullmatch(r"[A-Za-z0-9_./-]+", candidate) is None:
        return fallback_path

    parts = PurePosixPath(candidate).parts
    if any(part in ("", ".", "..") for part in parts):
        return fallback_path

    return candidate


def _extract_text_content(content: Any) -> str:
    """Normalize response content from LangChain/OpenAI-like clients into a string."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    return str(content)


def _parse_code_blocks(
    response: str, fallback_path: str = "generated.py"
) -> dict[str, str]:
    """
    Extract files from the LLM response. Supports 6 formats:
    1. ```python:path/to/file.py      (ideal)
    2. # path/to/file.py before the block (Qwen with comments)
    3. // path/to/file.py before the block (JS/TS)
    4. path/to/file.py before the block WITHOUT a comment (Qwen without #)
    5. Plain ```python (fallback to `target_path`)
    6. Multiple code blocks
    """
    files: dict[str, str] = {}

    # Format 1: ```lang:path
    pattern1 = r"```(\w+):([^\n`]+\.\w+)\n(.*?)```"
    for _, path, code in re.findall(pattern1, response, re.DOTALL):
        safe_path = _sanitize_generated_path(path, fallback_path)
        files[safe_path] = code.strip()
    if files:
        return files

    # Formats 2 and 3: # or // before the block
    pattern2 = r"(?:^|\n)(?:#|//)\s*([^\n`]+\.\w+)\s*\n```(\w+)\n(.*?)```"
    for path, _, code in re.findall(pattern2, response, re.DOTALL):
        safe_path = _sanitize_generated_path(path, fallback_path)
        files[safe_path] = code.strip()
    if files:
        return files

    # Format 4: a path WITHOUT a comment before ```
    pattern3 = r"(?:^|\n)\s*([A-Za-z0-9_./\\-]+\.\w{1,5})\s*\n```(\w+)\n(.*?)```"
    for path, _, code in re.findall(pattern3, response, re.DOTALL):
        if "/" in path or path.count(".") >= 1:
            safe_path = _sanitize_generated_path(path, fallback_path)
            files[safe_path] = code.strip()
    if files:
        return files

    # Format 5: one block without a path — use the fallback
    pattern4 = r"```(\w+)\n(.*?)```"
    matches = re.findall(pattern4, response, re.DOTALL)

    if len(matches) == 1:
        files[fallback_path] = matches[0][1].strip()
    elif len(matches) > 1:
        for i, (lang, code) in enumerate(matches, 1):
            ext = {
                "python": "py",
                "javascript": "js",
                "typescript": "ts",
                "php": "php",
                "bash": "sh",
                "ruby": "rb",
            }.get(lang, lang)
            files[f"generated_{i}.{ext}"] = code.strip()

    return files


def _strip_path_echo(code: str, filepath: str) -> str:
    """
    Safety layer: if the LLM echoes the file path as the first code line,
    remove it. This is a common Qwen bug with the `path\n```code```` format.
    """
    lines = code.split("\n")
    if not lines:
        return code

    first_line = lines[0].strip()
    # Remove the first line if it is the full path or its basename
    if first_line == filepath or first_line == filepath.split("/")[-1]:
        return "\n".join(lines[1:]).lstrip("\n")

    # If the first line ends with a file extension and contains no operators, it is likely noise
    if any(
        first_line.endswith(ext) for ext in (".py", ".js", ".ts", ".php", ".rb", ".go")
    ):
        if not any(
            op in first_line
            for op in ("=", "(", ":", " ", "import", "from", "def", "class")
        ):
            return "\n".join(lines[1:]).lstrip("\n")

    return code


def _make_git_diff(workdir: Path, files: dict[str, str]) -> str:
    """
    Create a unified diff using a git worktree.
    Replaces the old `/tmp`-based implementation.
    """
    # Apply files to the worktree
    apply_diff_to_worktree(workdir, files)

    # Get the diff from the main branch
    return get_diff_from_main(workdir)


async def execute(
    task: TaskInput, workdir: Optional[Path] = None, timeout_sec: int = 300
) -> CodeArtifact:
    """
    Main function of the EXECUTION layer using git worktrees.
    """
    artifact_id = str(uuid.uuid4())[:8]
    logs = []

    def log(msg: str):
        logs.append(msg)
        console.print(f"[cyan][{artifact_id}][/cyan] {msg}")

    # 1. Create a git worktree instead of a temp directory
    log(f"creating git worktree for {task['task_id']}")
    worktree_path = create_worktree(task["task_id"], artifact_id)
    log(f"worktree: {worktree_path}")

    # 2. Read context files from the worktree
    log(f"reading {len(task['context_files'])} context files")
    context_contents = {}
    for path in task["context_files"]:
        p = worktree_path / path
        if p.exists():
            context_contents[path] = p.read_text()

    # 3. Build the prompt
    user_prompt = _build_user_prompt(task, context_contents)
    log(f"prompt length: {len(user_prompt)} chars")

    # 4. Call Ollama
    log("calling Ollama (qwen2.5-coder:3b-instruct)")
    llm = ChatOpenAI(
        base_url=os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1"),
        api_key=SecretStr(os.getenv("OPENAI_API_KEY", "ollama")),
        model=os.getenv("MODEL_NAME", "qwen2.5-coder:3b-instruct"),
        temperature=0.2,
        max_completion_tokens=2048,
        timeout=timeout_sec,
    )

    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_prompt)],
        )
        generated_text = _extract_text_content(response.content)
        log(f"generated {len(generated_text)} chars")
    except Exception as e:
        log(f"❌ Ollama call failed: {e}")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise

    # 5. Parse files
    files = _parse_code_blocks(generated_text, fallback_path=task["target_path"])
    log(f"parsed {len(files)} files: {list(files.keys())}")

    if not files:
        log("❌ no code blocks found in response")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise ValueError("LLM response contained no valid code blocks")

    # 6. Apply files in the worktree and generate the diff
    git_diff = _make_git_diff(worktree_path, files)
    log(f"diff size: {len(git_diff)} chars")

    # 7. Commit changes in the worktree
    commit_message = f"feat({task['task_id']}): {task['description'][:50]}"
    committed = commit_worktree(worktree_path, commit_message)

    if not committed:
        log("⚠ No changes to commit")

    # 8. Build the artifact
    raw_artifact = {
        "artifact_id": artifact_id,
        "task_id": task["task_id"],
        "files_changed": list(files.keys()),
        "git_diff": git_diff,
        "logs": "\n".join(logs),
        "worktree_path": str(worktree_path),  # New: persist the worktree path
        "branch_name": f"agent/{task['task_id']}-{artifact_id}",  # New: branch name
    }

    sealed = seal_artifact_for_verification(raw_artifact)
    log(f"✅ artifact sealed in branch {sealed.get('branch_name', 'unknown')}")

    return sealed


# ─── Standalone test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Run with: `python -m nodes.execution`
    Tests `execution.py` in isolation, WITHOUT `planning.py`.
    """
    from dotenv import load_dotenv

    load_dotenv()

    # Test task — simulate input from Planning
    test_task: TaskInput = {
        "task_id": "test-001",
        "description": (
            "Create a Python function `is_palindrome(s: str) -> bool` "
            "that checks if a string reads the same forwards and backwards. "
            "Handle case insensitivity and ignore spaces."
        ),
        "context_files": [],  # No context for the standalone test
        "language": "python",
        "target_path": "src/utils/palindrome.py",
        # ❌ No acceptance_criteria, test_cases, or rubric
    }

    console.print("\n[bold magenta]═══ EXECUTION NODE TEST ═══[/]\n")

    try:
        result = asyncio.run(execute(test_task))

        console.print("\n[bold green]═══ RESULT ═══[/]")
        console.print(f"artifact_id: {result['artifact_id']}")
        console.print(f"files: {result['files_changed']}")
        console.print(f"diff preview:\n{result['git_diff'][:500]}")

        # Verify that forbidden fields are truly absent
        forbidden = {"worker_id", "task_description", "original_prompt"}
        leaked = forbidden & set(result.keys())
        if leaked:
            console.print(f"[bold red]🚫 GOODHART VIOLATION: leaked {leaked}[/]")
        else:
            console.print("[green]✅ Goodhart-proof: no forbidden fields leaked[/]")

    except Exception as e:
        console.print(f"[red]❌ Execution failed: {e}[/]")
