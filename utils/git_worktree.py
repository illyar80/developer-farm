"""
Git Worktree Manager
--------------------
Manage isolated git worktrees for each worker.

Each worker gets:
- Its own branch: `agent/{task_id}-{artifact_id}`
- Its own worktree: `worktrees/{artifact_id}/`
- Full isolation from other workers and the main branch
"""

import os
import shutil
import subprocess
from pathlib import Path

from utils.output import console

from utils.farm_config import get_repo_path


# Base directory for worktrees (resolved relative to target repo)
WORKTREES_DIR = get_repo_path() / "worktrees"

GIT_ENV = {**os.environ, "LANG": "C", "LC_ALL": "C", "GIT_PAGER": "cat"}


def _git(
    *args: str,
    repo_path: Path | None = None,
    check: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a git command with C locale for consistent output parsing."""
    rp = repo_path or get_repo_path()
    result = subprocess.run(
        ["git", *args],
        cwd=rp,
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
        **kwargs,
    )
    return result


def ensure_git_repo():
    """Ensure the target repo is a git repository."""
    rp = get_repo_path()
    if not (rp / ".git").exists():
        console.print(f"[yellow]No git repo found at {rp}. Initializing...[/]")
        _git("init", repo_path=rp)
        _git("config", "user.email", "farm@developer.local", repo_path=rp)
        _git("config", "user.name", "Developer Farm", repo_path=rp)
        if not any(rp.glob("*")):
            (rp / "README.md").write_text("# Developer Farm\n\nAI-powered development pipeline.\n")
            _git("add", ".", repo_path=rp)
            _git("commit", "-m", "Initial commit", repo_path=rp)
        console.print("[green]Git repo initialized[/]")


def create_worktree(task_id: str, artifact_id: str) -> Path:
    """Create a new git worktree for a worker."""
    ensure_git_repo()
    branch_name = f"agent/{task_id}-{artifact_id}"
    worktree_path = WORKTREES_DIR / artifact_id
    WORKTREES_DIR.mkdir(exist_ok=True)
    if worktree_path.exists():
        console.print(f"[yellow]Worktree already exists: {worktree_path}[/]")
        return worktree_path
    try:
        _git("worktree", "add", "-b", branch_name, str(worktree_path))
        console.print(f"[green]Created worktree: {worktree_path} (branch: {branch_name})[/]")
        return worktree_path
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to create worktree: {e.stderr}[/]")
        raise


def apply_diff_to_worktree(worktree_path: Path, files: dict[str, str]):
    """Apply generated files into a worktree."""
    for rel_path, content in files.items():
        filepath = worktree_path / rel_path
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
    console.print(f"[cyan]Applied {len(files)} files to {worktree_path}[/]")


def commit_worktree(
    worktree_path: Path, message: str, files: list[str] | None = None
) -> bool:
    """Commit changes inside a worktree."""
    try:
        if files:
            subprocess.run(
                ["git", "add", "-f", "--", *files],
                cwd=str(worktree_path), check=True, capture_output=True,
                env=GIT_ENV,
            )
        else:
            subprocess.run(
                ["git", "add", "-f", "."],
                cwd=str(worktree_path), check=True, capture_output=True,
                env=GIT_ENV,
            )
            # Unstage graphify-out so live graphify cache doesn't pollute commits
            subprocess.run(
                ["git", "reset", "HEAD", "--", "graphify-out/"],
                cwd=str(worktree_path), capture_output=True, check=False,
                env=GIT_ENV,
            )
        # Check for staged changes (after potentially unstaging graphify-out)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(worktree_path), capture_output=True, check=False,
            env=GIT_ENV,
        )
        if result.returncode == 0:
            console.print(f"[yellow]No code changes to commit in {worktree_path}[/]")
            return False
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree_path), check=True, capture_output=True,
            env=GIT_ENV,
        )
        console.print(f"[green]Committed: {message[:50]}...[/]")
        return True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        console.print(f"[red]Commit failed: {err}[/]")
        return False


def merge_worktree(
    worktree_path: Path,
    branch_name: str,
    target_branch: str,
    repo_path: Path | None = None,
) -> bool:
    """
    Merge a worktree branch into a staging branch in the target repo.

    Steps:
      1. Commit any pending changes in the worktree
      2. Merge the worktree branch into target_branch

    Returns:
        ``True`` if the merge succeeds
    """
    rp = repo_path or get_repo_path()
    stashed = False
    original_branch: str | None = None
    try:
        commit_worktree(worktree_path, f"feat: {branch_name}")

        # Remember original branch and stash dirty files before switching
        orig = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=rp, capture_output=True, text=True, check=True,
            env=GIT_ENV,
        )
        original_branch = orig.stdout.strip()

        stash_result = subprocess.run(
            ["git", "stash", "push", "--include-untracked",
             "-m", f"auto-stash before merge {branch_name}"],
            cwd=rp, capture_output=True, text=True, check=False,
            env=GIT_ENV,
        )
        stashed = stash_result.returncode == 0 and "Saved working directory" in stash_result.stdout

        # Check if nested refs block the target branch
        nested_refs = subprocess.run(
            ["git", "branch", "--list", f"{target_branch}/*"],
            cwd=rp, capture_output=True, text=True, check=False,
            env=GIT_ENV,
        )
        if nested_refs.stdout.strip():
            for ref_line in nested_refs.stdout.strip().splitlines():
                ref_name = ref_line.strip().lstrip("* ")
                subprocess.run(
                    ["git", "branch", "-D", ref_name],
                    cwd=rp, capture_output=True, check=False,
                    env=GIT_ENV,
                )

        branch_exists = bool(
            subprocess.run(
                ["git", "branch", "--list", target_branch],
                cwd=rp, capture_output=True, text=True, check=False,
                env=GIT_ENV,
            ).stdout.strip()
        )
        if not branch_exists:
            _git("checkout", "-b", target_branch, repo_path=rp, check=True)
        else:
            _git("checkout", target_branch, repo_path=rp, check=True)
        _git("merge", branch_name, "--no-edit", "-X", "theirs", repo_path=rp, check=True)

        console.print(f"[green]Merged {branch_name} -> {target_branch}[/]")
        return True

    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else e.stdout.strip() if e.stdout else "unknown git error"
        console.print(f"[red]Merge failed: {err_msg}[/]")
        return False

    finally:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=rp, capture_output=True, check=False,
            env=GIT_ENV,
        )
        if original_branch and original_branch != "HEAD":
            subprocess.run(
                ["git", "checkout", original_branch],
                cwd=rp, capture_output=True, check=False,
                env=GIT_ENV,
            )
        if stashed:
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=rp, capture_output=True, check=False,
                env=GIT_ENV,
            )


def get_diff_from_main(worktree_path: Path, base_branch: str | None = None) -> str:
    """Get the diff between the worktree and its base branch."""
    try:
        if base_branch is None:
            for candidate in ("main", "master", "dev", "origin/main", "origin/master", "origin/dev"):
                try:
                    subprocess.run(
                        ["git", "rev-parse", "--verify", candidate],
                        cwd=str(worktree_path), capture_output=True, check=True,
                        env=GIT_ENV,
                    )
                    base_branch = candidate
                    break
                except subprocess.CalledProcessError:
                    continue
            if base_branch is None:
                console.print("[yellow]Could not detect base branch[/]")
                return ""
        EXCLUDE_PATTERNS = [":(exclude)graphify-out/*"]
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...HEAD", "--", *EXCLUDE_PATTERNS],
            cwd=str(worktree_path), capture_output=True, text=True, check=True,
            env=GIT_ENV,
        )
        MAX_DIFF = 80000
        diff = result.stdout[:MAX_DIFF]
        if not diff.strip():
            result = subprocess.run(
                ["git", "diff", f"{base_branch}...HEAD"],
                cwd=str(worktree_path), capture_output=True, text=True, check=True,
                env=GIT_ENV,
            )
            return result.stdout[:MAX_DIFF]
        return diff
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to get diff: {e.stderr}[/]")
        return ""


def cleanup_worktree(
    worktree_path: Path,
    delete_branch: bool = False,
    branch_name: str | None = None,
    repo_path: Path | None = None,
):
    """Remove a worktree and optionally delete its branch."""
    rp = repo_path or get_repo_path()
    try:
        if branch_name is None:
            wt_list = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True,
                cwd=rp, env=GIT_ENV,
            )
            for entry in wt_list.stdout.strip().split("\n\n"):
                if str(worktree_path) in entry:
                    for line in entry.split("\n"):
                        if line.startswith("branch refs/heads/"):
                            branch_name = line.replace("branch refs/heads/", "").strip()
                            break
                    break

        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            capture_output=True, check=True,
            cwd=rp, env=GIT_ENV,
        )
        console.print(f"[green]Removed worktree: {worktree_path}[/]")

        if delete_branch and branch_name:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=rp, capture_output=True, check=False,
                env=GIT_ENV,
            )
            console.print(f"[green]Deleted branch: {branch_name}[/]")
    except subprocess.CalledProcessError as e:
        console.print(f"[yellow]Cleanup warning: {e.stderr}[/]")
        if worktree_path.exists():
            shutil.rmtree(worktree_path)


def list_worktrees() -> list[dict]:
    """Return a list of all active worktrees."""
    try:
        rp = get_repo_path()
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True,
            cwd=rp, env=GIT_ENV,
        )
        worktrees: list[dict] = []
        current: dict = {}
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": Path(line.replace("worktree ", ""))}
            elif line.startswith("branch refs/heads/"):
                current["branch"] = line.replace("branch refs/heads/", "")
        if current:
            worktrees.append(current)
        return worktrees
    except subprocess.CalledProcessError:
        return []
