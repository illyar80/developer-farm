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
from utils.output import console

sys.path.insert(0, str(Path(__file__).parent.parent))
from contracts import CodeArtifact, TaskInput, seal_artifact_for_verification
from utils.git_worktree import (
    apply_diff_to_worktree,
    cleanup_worktree,
    commit_worktree,
    create_worktree,
    get_diff_from_main,
)



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

    if task.get("language") == "javascript":
        parts.append(
            "\n# Code Structure\n"
            "Use `const router = express.Router();` and export as `module.exports = { router };`. "
            "The handler function receives (req, res). "
            "Use `res.status(CODE).json({...})` for responses.\n"
            "\n"
            "# Route Path\n"
            "Define your route at the path specified in the task description "
            "(e.g., /api/users, /api/register, /api/checkout). "
            "Do NOT use a different route path.\n"
            "\n"
            "# Response Format\n"
            "Return JSON responses as flat objects. "
            "Place all expected fields (id, email, name, token, etc.) at the top level "
            "of the response object — do NOT nest them under 'user', 'data', or other wrapper keys.\n"
            "\n"
            "# Required Fields\n"
            "Only validate fields explicitly mentioned in the task description. "
            "Do NOT require additional fields that are not specified in the description.\n"
            "\n"
            "# Input Validation\n"
            "If you need input validation, import `check` and `validationResult` "
            "directly from `'express-validator'` at the top of your route file. "
            "Do NOT reference `check` or `validationResult` without importing them.\n"
        )

    if task.get("language") == "php":
        caps = task.get("required_capabilities", [])
        desc = task.get("description", "").lower()
        is_wordpress = (
            any(kw in caps for kw in ("wordpress", "wp", "wp_theme", "wp_plugin", "wp_rest_api"))
            or "wordpress" in desc
        )
        is_yii2 = any(kw in caps for kw in ("yii2", "yii")) or "yii2" in desc

        if is_wordpress:
            parts.append(
                "\n# WordPress Conventions\n"
                "Follow WordPress Coding Standards (WPCS). "
                "Use snake_case for function names and lowercase with underscores for hooks.\n"
                "\n"
                "# Hooks (Actions & Filters)\n"
                "Use `add_action('hook_name', 'callback_func')` and "
                "`add_filter('filter_name', 'callback_func')`. "
                "Prefix custom hooks with the plugin/theme slug. "
                "Define callback functions as `prefix_feature_action()`.\n"
                "\n"
                "# Enqueuing Assets\n"
                "Use `wp_enqueue_script()` and `wp_enqueue_style()` with proper handles, "
                "dependencies, version, and footer placement. "
                "Hook into `wp_enqueue_scripts` action.\n"
                "\n"
                "# WP_Query & The Loop\n"
                "Use `WP_Query` with `array` arguments for custom database queries. "
                "Use `have_posts()` / `the_post()` loop pattern. "
                "Use `wp_reset_postdata()` after custom queries.\n"
                "\n"
                "# WordPress REST API\n"
                "Register routes via `register_rest_route()`. "
                "Use `WP_REST_Request` and `WP_REST_Response`. "
                "Set `permission_callback` for auth checks.\n"
                "\n"
                "# Database\n"
                "Use `$wpdb->get_results()`, `$wpdb->get_row()`, `$wpdb->insert()`, "
                "`$wpdb->update()` for direct queries. "
                "Use `$wpdb->prepare()` with `%s`/`%d` placeholders for SQL injection prevention. "
                "Prefix custom table names with `$wpdb->prefix`.\n"
                "\n"
                "# Options & Transients\n"
                "Use `get_option()` / `update_option()` for persistent settings. "
                "Use `set_transient()` / `get_transient()` for cached data.\n"
                "\n"
                "# Shortcodes\n"
                "Register via `add_shortcode('tag', 'callback')`. "
                "The callback receives `$atts` and `$content`, returns string output.\n"
                "\n"
                "# Response Format\n"
                "For REST endpoints return `WP_REST_Response` or `WP_Error`. "
                "Use `rest_ensure_response()` for data wrapping. "
                "Place all expected fields at the top level of the response.\n"
                "\n"
                "# Required Fields\n"
                "Only validate fields explicitly mentioned in the task description. "
                "Do NOT require additional fields that are not specified.\n"
            )
        elif is_yii2:
            parts.append(
                "\n# Yii2 Conventions\n"
                "Follow PSR-12 coding standard. Use `<?php` with `declare(strict_types=1);`.\n"
                "\n"
                "# MVC Structure\n"
                "Controllers extend `yii\\web\\Controller`. "
                "Actions are named `actionIndex()`, `actionCreate()`, etc. "
                "Models extend `yii\\db\\ActiveRecord` for database tables. "
                "Views are PHP files in `views/controller/action.php`.\n"
                "\n"
                "# Database (ActiveRecord)\n"
                "Define table via `public static function tableName()` in the model. "
                "Use `find()`, `findOne()`, `findAll()`, `find()->where(['col' => $val])->all()`. "
                "Define validation rules in `public function rules()`. "
                "Define attribute labels in `public function attributeLabels()`.\n"
                "\n"
                "# Database (Query Builder)\n"
                "Use `(new \\yii\\db\\Query())->from('table')->where(['col' => $val])->all()`. "
                "Always use parameter binding — never concatenate raw values into queries.\n"
                "\n"
                "# Request & Response\n"
                "Access request data via `\\Yii::$app->request->get()`, "
                "`\\Yii::$app->request->post()`. "
                "Return JSON via `\\Yii::$app->response->data = [...]` "
                "or `return $this->asJson([...])`.\n"
                "\n"
                "# REST API\n"
                "For REST controllers extend `yii\\rest\\ActiveController`. "
                "Configure `$modelClass` and `$serializer`. "
                "Use `yii\\rest\\UrlRule` in URL rules for automatic route generation.\n"
                "\n"
                "# Data Providers\n"
                "Use `yii\\data\\ActiveDataProvider` for paginated listings: "
                "`new ActiveDataProvider(['query' => Model::find(), 'pagination' => ['pageSize' => 20]])`.\n"
                "\n"
                "# Behaviors\n"
                "Attach behaviors via `public function behaviors()`. "
                "Common: `TimestampBehavior`, `BlameableBehavior`, `yii\\filters\\VerbFilter` for controllers.\n"
                "\n"
                "# Required Fields\n"
                "Only validate fields explicitly mentioned in the task description. "
                "Do NOT require additional fields that are not specified.\n"
            )
        else:
            parts.append(
                "\n# Code Structure\n"
                "Use `<?php` opening tag. Use `declare(strict_types=1);` on the first line "
                "after the opening tag. Follow PSR-12 coding standard.\n"
                "\n"
                "# Namespace & Autoloading\n"
                "Place the file in the correct namespace following PSR-4. "
                "Use Composer autoloading conventions. "
                "The namespace should match the directory structure from `src/`.\n"
                "\n"
                "# Routing (if applicable)\n"
                "For Laravel: define routes in route files using `Route::` facade. "
                "For Symfony: use attribute-based routing (`#[Route('/path')]`).\n"
                "\n"
                "# Response Format\n"
                "For API controllers, return JSON responses using "
                "`return response()->json([...])` (Laravel) or "
                "`return $this->json([...])` (Symfony). "
                "Place all expected fields at the top level — "
                "do NOT nest under 'data' unless the spec explicitly says so.\n"
                "\n"
                "# Required Fields\n"
                "Only validate fields explicitly mentioned in the task description. "
                "Do NOT require additional fields that are not specified.\n"
                "\n"
                "# Input Validation\n"
                "Use Form Request validation (Laravel) or the Validator component (Symfony). "
                "Import validation classes at the top of the file. "
                "Do NOT use raw `$_POST` or `$_GET` — use injected Request objects.\n"
            )

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


async def execute(
    task: TaskInput,
    workdir: Optional[Path] = None,
    timeout_sec: int = 300,
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    execution_config: dict | None = None,
) -> CodeArtifact:
    """
    Main function of the EXECUTION layer using git worktrees.

    Args:
        task: The task to execute
        workdir: Optional working directory override
        timeout_sec: LLM call timeout in seconds
        model_name: Override model name (default: MODEL_NAME env or qwen3-coder-next:cloud)
        base_url: Override OpenAI-compatible base URL (default: OPENAI_API_BASE env or localhost:11434)
        api_key: Override API key (default: OPENAI_API_KEY env or "ollama" for local)
        execution_config: Optional dict from Model Router with keys: model, base_url, api_key, max_tokens.
                          Overrides individual model_name/base_url/api_key params when provided.
    """
    artifact_id = str(uuid.uuid4())[:8]
    logs = []

    # Determine model config: execution_config takes precedence
    if execution_config:
        actual_model = execution_config.get("model", os.getenv("MODEL_NAME", "qwen3-coder-next:cloud"))
        actual_base = execution_config.get("base_url") or os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1")
        actual_api_key = execution_config.get("api_key") or os.getenv("CLOUD_API_KEY") or os.getenv("OPENAI_API_KEY", "ollama")
        actual_timeout = execution_config.get("timeout", timeout_sec)
    else:
        actual_model = model_name or os.getenv("MODEL_NAME", "qwen3-coder-next:cloud")
        actual_base = base_url or os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1")
        actual_api_key = api_key or os.getenv("CLOUD_API_KEY") or os.getenv("OPENAI_API_KEY", "ollama")
        actual_timeout = timeout_sec

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

    # 4. Call LLM
    log(f"calling {actual_model} @ {actual_base}")
    max_tokens = execution_config.get("max_tokens", 2048) if execution_config else 2048
    llm = ChatOpenAI(
        base_url=actual_base,
        api_key=SecretStr(actual_api_key),
        model=actual_model,
        temperature=0.2,
        max_completion_tokens=max_tokens,
        timeout=actual_timeout,
    )

    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_prompt)],
        )
        generated_text = _extract_text_content(response.content)
        log(f"generated {len(generated_text)} chars")
    except Exception as e:
        log(f"❌ LLM call failed ({actual_model}): {e}")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise

    # 5. Parse files
    files = _parse_code_blocks(generated_text, fallback_path=task["target_path"])
    log(f"parsed {len(files)} files: {list(files.keys())}")

    if not files:
        log("❌ no code blocks found in response")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise ValueError("LLM response contained no valid code blocks")

    # 6. Apply files in the worktree
    apply_diff_to_worktree(worktree_path, files)

    # 7. Commit changes in the worktree
    commit_message = f"feat({task['task_id']}): {task['description'][:50]}"
    committed = commit_worktree(worktree_path, commit_message)

    if not committed:
        log("⚠ No changes to commit")

    # 8. Get the diff from the main branch (after commit, so git diff main...HEAD works)
    git_diff = get_diff_from_main(worktree_path)
    log(f"diff size: {len(git_diff)} chars")

    # 8. Build the artifact
    raw_artifact = {
        "artifact_id": artifact_id,
        "task_id": task["task_id"],
        "files_changed": list(files.keys()),
        "files": files,
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
