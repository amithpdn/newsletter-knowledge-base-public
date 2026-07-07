# agents/git_backup.py — Newsletter AI Pipeline v4.0
# Agent 9: Git Backup
#
# Responsibilities:
#   - Stage all changes in the project folder
#   - Commit with a timestamped message
#   - Push to the GitHub remote (backup destination)
#   - Log the outcome to git_backup_log in registry.db
#
# This agent runs as a SEPARATE Windows Task Scheduler task, independent
# of the main pipeline. Typical schedule: 30 minutes after orchestrator.py.
# A failure here is a WARNING — primary OneDrive storage is unaffected.
#
# Design decisions:
#   - registry.db, .env, and secrets/ are gitignored — never pushed
#   - "nothing to commit" is treated as success (not an error)
#   - Push failures are logged but do not raise — backup is best-effort
#   - Git is invoked via subprocess to use the system Git install;
#     no Python Git library dependency needed
#
# Prerequisites:
#   - Git for Windows installed (git.exe on PATH)
#   - GitHub remote configured: git remote add origin https://github.com/...
#   - Personal Access Token stored in Windows Credential Manager:
#       cmdkey /add:git:https://github.com /user:<username> /pass:<PAT>
#
# Usage:
#   Run directly as a scheduled task:
#     python pipeline\agents\git_backup.py
#
#   Or call run_git_backup() from orchestrator with --no-backup flag check.
#
# Standalone test:
#   cd pipeline && python agents/git_backup.py
# =============================================================================

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import PROJECT_ROOT
from db import initialise_db, insert_git_backup_entry


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_git_backup(dry_run: bool = False) -> dict:
    """
    Run the full git add → commit → push sequence.

    Args:
        dry_run: If True, runs 'git status' instead of modifying the repo.
                 Useful for testing that Git is configured correctly.

    Returns:
        Result dict with keys:
          push_status    str  — "success" | "failed" | "nothing_to_commit"
          commit_hash    str  — short hash if a commit was made, else None
          files_staged   int  — approximate count of staged files
          error_message  str  — error text if push_status is "failed", else None
          run_at         str  — ISO 8601 UTC timestamp

    Side effect:
        Writes one row to git_backup_log in registry.db.
        Prints progress to stdout (visible in Task Scheduler log).
    """
    repo      = Path(PROJECT_ROOT)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_iso   = datetime.now(timezone.utc).isoformat()

    print(f"[git_backup] Starting at {timestamp}")
    print(f"[git_backup] Repo: {repo}")

    if dry_run:
        return _dry_run(repo, now_iso)

    # -----------------------------------------------------------------------
    # Step 1: git add .
    # -----------------------------------------------------------------------
    add_result = _run_git(["git", "-C", str(repo), "add", "."])
    if add_result["returncode"] != 0:
        return _record_failure(
            now_iso, 0, None,
            f"git add failed: {add_result['stderr']}"
        )

    # -----------------------------------------------------------------------
    # Step 2: Count staged files (informational)
    # -----------------------------------------------------------------------
    files_staged = _count_staged_files(repo)

    # -----------------------------------------------------------------------
    # Step 3: git commit
    # -----------------------------------------------------------------------
    commit_message = f"Backup: newsletter notes — {timestamp}"
    commit_result  = _run_git([
        "git", "-C", str(repo), "commit", "-m", commit_message
    ])

    # "nothing to commit" is a successful no-op
    combined_output = commit_result["stdout"] + commit_result["stderr"]
    if "nothing to commit" in combined_output or "nothing added to commit" in combined_output:
        print(f"[git_backup] Nothing to commit — backup up to date.")
        result = {
            "run_at":        now_iso,
            "files_staged":  0,
            "commit_hash":   None,
            "push_status":   "nothing_to_commit",
            "error_message": None,
        }
        insert_git_backup_entry(result)
        return result

    if commit_result["returncode"] != 0:
        return _record_failure(
            now_iso, files_staged, None,
            f"git commit failed: {commit_result['stderr']}"
        )

    # Extract commit hash from output e.g. "[main abc1234] Backup: ..."
    commit_hash = _extract_commit_hash(commit_result["stdout"])
    print(f"[git_backup] Committed: {commit_hash or '(hash unavailable)'}")

    # -----------------------------------------------------------------------
    # Step 4: git push
    # -----------------------------------------------------------------------
    push_result = _run_git(["git", "-C", str(repo), "push", "origin", "main"])

    if push_result["returncode"] != 0:
        # Commit succeeded but push failed — not a disaster, will retry next run
        error_msg = push_result["stderr"][:500]
        print(f"[git_backup] WARNING: push failed: {error_msg}")
        return _record_failure(now_iso, files_staged, commit_hash, f"git push failed: {error_msg}")

    print(
        f"[git_backup] Push successful. "
        f"Files staged: {files_staged}. "
        f"Commit: {commit_hash or 'n/a'}"
    )

    result = {
        "run_at":        now_iso,
        "files_staged":  files_staged,
        "commit_hash":   commit_hash,
        "push_status":   "success",
        "error_message": None,
    }
    insert_git_backup_entry(result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_git(cmd: list[str]) -> dict:
    """
    Run a git command via subprocess and return stdout, stderr, returncode.

    Uses shell=False for security. Captures output for logging.
    A non-zero returncode does not raise — callers decide how to handle it.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,    # 2-minute timeout; push to GitHub should be fast
        )
        return {
            "returncode": proc.returncode,
            "stdout":     proc.stdout,
            "stderr":     proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out after 120s"}
    except FileNotFoundError:
        return {
            "returncode": -1,
            "stdout":     "",
            "stderr":     (
                "git executable not found. "
                "Install Git for Windows and ensure git.exe is on the system PATH."
            ),
        }


def _count_staged_files(repo: Path) -> int:
    """
    Count the number of files staged for commit.
    Uses 'git diff --cached --name-only' — returns 0 on any error.
    """
    result = _run_git(["git", "-C", str(repo), "diff", "--cached", "--name-only"])
    if result["returncode"] == 0 and result["stdout"].strip():
        return len(result["stdout"].strip().splitlines())
    return 0


def _extract_commit_hash(commit_stdout: str) -> str | None:
    """
    Parse the short commit hash from 'git commit' output.
    Output format: "[main abc1234] Commit message"
    Returns None if parsing fails.
    """
    import re
    match = re.search(r"\[(?:main|master)\s+([a-f0-9]+)\]", commit_stdout)
    return match.group(1) if match else None


def _record_failure(
    now_iso:       str,
    files_staged:  int,
    commit_hash:   str | None,
    error_message: str,
) -> dict:
    """Log a failed backup result and return the result dict."""
    print(f"[git_backup] FAILED: {error_message}")
    result = {
        "run_at":        now_iso,
        "files_staged":  files_staged,
        "commit_hash":   commit_hash,
        "push_status":   "failed",
        "error_message": error_message,
    }
    insert_git_backup_entry(result)
    return result


def _dry_run(repo: Path, now_iso: str) -> dict:
    """
    Run git status only — no modifications. Used for connectivity testing.
    """
    print("[git_backup] DRY RUN — no changes will be made\n")

    status = _run_git(["git", "-C", str(repo), "status"])
    print(status["stdout"] or status["stderr"])

    # Check remote is configured
    remote = _run_git(["git", "-C", str(repo), "remote", "-v"])
    if remote["stdout"].strip():
        print(f"Configured remotes:\n{remote['stdout']}")
    else:
        print("WARNING: No git remote configured.")
        print("Run: git remote add origin https://github.com/<username>/newsletter-knowledge-base.git")

    return {
        "run_at":        now_iso,
        "files_staged":  0,
        "commit_hash":   None,
        "push_status":   "skipped",
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# Standalone entry point (Task Scheduler target)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Entry point when run directly by Windows Task Scheduler or from the CLI.

    Supports one optional flag:
        --dry-run   Run git status only, no commits or pushes.

    Exit codes:
        0  — success or nothing_to_commit
        1  — failure (git error, push failed, etc.)

    Task Scheduler uses the exit code to determine if the task succeeded.
    """
    initialise_db()

    dry_run = "--dry-run" in sys.argv

    result = run_git_backup(dry_run=dry_run)

    status = result.get("push_status", "unknown")
    print(f"\n[git_backup] Exit status: {status}")

    # Exit with code 1 on failure so Task Scheduler marks the task as failed
    if status == "failed":
        sys.exit(1)
    else:
        sys.exit(0)
