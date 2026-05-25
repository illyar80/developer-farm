"""
Git Worktree Manager
---------------------
Управление изолированными git worktrees для каждого воркера.

Каждый воркер получает:
- Свой branch: agent/{task_id}-{artifact_id}
- Свой worktree: worktrees/{artifact_id}/
- Полную изоляцию от других воркеров и main branch
"""
import subprocess
import shutil
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

# Базовая директория для worktrees
WORKTREES_DIR = Path("worktrees")


def ensure_git_repo():
    """Убеждается что в текущей директории есть git репозиторий."""
    if not Path(".git").exists():
        console.print("[yellow]⚠ No git repo found. Initializing...[/]")
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "farm@developer.local"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Developer Farm"], check=True, capture_output=True)
        
        # Создаём初始 commit если репозиторий пустой
        if not any(Path(".").glob("*")):
            Path("README.md").write_text("# Developer Farm\n\nAI-powered development pipeline.\n")
            subprocess.run(["git", "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], check=True, capture_output=True)
        
        console.print("[green]✅ Git repo initialized[/]")


def create_worktree(task_id: str, artifact_id: str) -> Path:
    """
    Создаёт новый git worktree для воркера.
    
    Args:
        task_id: ID задачи (например "task-001")
        artifact_id: Уникальный ID артефакта (например "abc123")
    
    Returns:
        Path к созданному worktree
    """
    ensure_git_repo()
    
    branch_name = f"agent/{task_id}-{artifact_id}"
    worktree_path = WORKTREES_DIR / artifact_id
    
    # Создаём директорию для worktrees если её нет
    WORKTREES_DIR.mkdir(exist_ok=True)
    
    # Проверяем не существует ли уже такой worktree
    if worktree_path.exists():
        console.print(f"[yellow]⚠ Worktree already exists: {worktree_path}[/]")
        return worktree_path
    
    try:
        # Создаём новый branch от HEAD и worktree
        # git worktree add -b <branch> <path>
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
            capture_output=True,
            text=True,
            check=True
        )
        
        console.print(f"[green]✅ Created worktree: {worktree_path} (branch: {branch_name})[/]")
        return worktree_path
        
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to create worktree: {e.stderr}[/]")
        raise


def apply_diff_to_worktree(worktree_path: Path, files: dict[str, str]):
    """
    Применяет сгенерированные файлы в worktree.
    
    Args:
        worktree_path: Путь к worktree
        files: dict {relative_path: content}
    """
    for rel_path, content in files.items():
        filepath = worktree_path / rel_path
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
    
    console.print(f"[cyan]📝 Applied {len(files)} files to {worktree_path}[/]")


def commit_worktree(worktree_path: Path, message: str) -> bool:
    """
    Делает commit изменений в worktree.
    
    Args:
        worktree_path: Путь к worktree
        message: Commit message
    
    Returns:
        True если commit успешен, False если нет изменений
    """
    try:
        # git add .
        subprocess.run(
            ["git", "add", "."],
            cwd=str(worktree_path),
            check=True,
            capture_output=True
        )
        
        # Проверяем есть ли изменения
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True
        )
        
        if not result.stdout.strip():
            console.print(f"[yellow]⚠ No changes to commit in {worktree_path}[/]")
            return False
        
        # git commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree_path),
            check=True,
            capture_output=True
        )
        
        console.print(f"[green]✅ Committed: {message[:50]}...[/]")
        return True
        
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Commit failed: {e.stderr}[/]")
        return False


def get_diff_from_main(worktree_path: Path) -> str:
    """
    Получает diff между worktree и main/master branch.
    
    Returns:
        Unified diff string
    """
    try:
        # Определяем основной branch (main или master)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True
        )
        current_branch = result.stdout.strip()
        
        # Пробуем main, потом master
        base_branch = "main"
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "main"],
                capture_output=True,
                check=True
            )
        except subprocess.CalledProcessError:
            base_branch = "master"
        
        # git diff main...HEAD (три точки = от общего предка)
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True
        )
        
        return result.stdout
        
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to get diff: {e.stderr}[/]")
        return ""


def cleanup_worktree(worktree_path: Path, delete_branch: bool = False):
    """
    Удаляет worktree и опционально branch.
    
    Args:
        worktree_path: Путь к worktree
        delete_branch: Удалять ли branch тоже
    """
    try:
        # Получаем имя branch из worktree
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        
        branch_name = None
        for line in result.stdout.split("\n"):
            if str(worktree_path) in line:
                # Следующая строка после worktree path содержит branch
                continue
            if line.startswith("branch refs/heads/"):
                branch_name = line.replace("branch refs/heads/", "").strip()
                break
        
        # git worktree remove
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            capture_output=True,
            check=True
        )
        
        console.print(f"[green]✅ Removed worktree: {worktree_path}[/]")
        
        # Удаляем branch если нужно
        if delete_branch and branch_name:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                capture_output=True,
                check=True
            )
            console.print(f"[green]✅ Deleted branch: {branch_name}[/]")
            
    except subprocess.CalledProcessError as e:
        console.print(f"[yellow]⚠ Cleanup warning: {e.stderr}[/]")
        # Fallback: просто удаляем директорию
        if worktree_path.exists():
            shutil.rmtree(worktree_path)


def list_worktrees() -> list[dict]:
    """
    Возвращает список всех активных worktrees.
    
    Returns:
        List of {"path": Path, "branch": str}
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        
        worktrees = []
        current = {}
        
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


# ─── CLI для тестирования ───────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python -m utils.git_worktree [create|list|cleanup] [args]")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "create":
        task_id = sys.argv[2] if len(sys.argv) > 2 else "task-test"
        artifact_id = sys.argv[3] if len(sys.argv) > 3 else "test123"
        path = create_worktree(task_id, artifact_id)
        print(f"Created: {path}")
        
    elif command == "list":
        worktrees = list_worktrees()
        for wt in worktrees:
            print(f"{wt['path']} → {wt['branch']}")
            
    elif command == "cleanup":
        path = Path(sys.argv[2])
        cleanup_worktree(path, delete_branch=True)
        print(f"Cleaned: {path}")
