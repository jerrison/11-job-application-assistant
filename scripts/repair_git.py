"""Isolated git helpers for repair supervisor worktrees and promotions."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_ORIGIN_MAIN_REF = "origin/main"


def _run_git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )


def _sanitize_branch_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().casefold()).strip("-")
    return token[:48] or "repair"


@dataclass(frozen=True)
class RepairWorktree:
    path: Path
    branch: str
    base_sha: str


def fetch_origin_main(project_root: Path) -> None:
    _run_git(project_root, ["fetch", "origin", "main"])


def read_ref_sha(project_root: Path, ref: str = "HEAD") -> str:
    return _run_git(project_root, ["rev-parse", ref]).stdout.strip()


def verified_origin_main_sha(project_root: Path) -> str:
    fetch_origin_main(project_root)
    return read_ref_sha(project_root, _ORIGIN_MAIN_REF)


def create_repair_worktree(*, project_root: Path, cluster_fingerprint: str) -> RepairWorktree:
    token = _sanitize_branch_token(cluster_fingerprint)
    branch = f"autofix/{token[:12]}"
    base_sha = verified_origin_main_sha(project_root)
    path = project_root.parent / f"{project_root.name}-repair-{token[:12]}"
    if path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    _run_git(project_root, ["worktree", "add", "-B", branch, str(path), _ORIGIN_MAIN_REF])
    return RepairWorktree(path=path, branch=branch, base_sha=base_sha)


def create_detached_verification_worktree(
    *,
    project_root: Path,
    ref: str,
    cluster_fingerprint: str,
    label: str,
) -> RepairWorktree:
    token = _sanitize_branch_token(f"{cluster_fingerprint}-{label}")
    path = project_root.parent / f"{project_root.name}-{label}-{token[:12]}"
    if path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    _run_git(project_root, ["worktree", "add", "--detach", str(path), ref])
    return RepairWorktree(path=path, branch=f"{label}/{token[:12]}", base_sha=read_ref_sha(project_root, ref))


def commit_repair_candidate(worktree: RepairWorktree, *, message: str) -> str:
    status = _run_git(worktree.path, ["status", "--porcelain"]).stdout.strip()
    if not status:
        return read_ref_sha(worktree.path, "HEAD")
    _run_git(worktree.path, ["add", "-A"])
    _run_git(worktree.path, ["commit", "-m", message])
    return read_ref_sha(worktree.path, "HEAD")


def push_main(project_root: Path, promoted_sha: str) -> None:
    _run_git(project_root, ["push", "origin", f"{promoted_sha}:main"])


def revert_main(project_root: Path, promoted_sha: str) -> str:
    verified_origin_main_sha(project_root)
    worktree = create_detached_verification_worktree(
        project_root=project_root,
        ref=_ORIGIN_MAIN_REF,
        cluster_fingerprint=promoted_sha,
        label="repair-revert",
    )
    try:
        _run_git(worktree.path, ["revert", "--no-edit", promoted_sha])
        revert_sha = read_ref_sha(worktree.path, "HEAD")
        _run_git(project_root, ["push", "origin", f"{revert_sha}:main"])
        return revert_sha
    finally:
        cleanup_repair_worktree(worktree)


def verify_origin_main_base(project_root: Path, *, expected_base_sha: str) -> str:
    current_origin_main = verified_origin_main_sha(project_root)
    if current_origin_main != expected_base_sha:
        raise RuntimeError(
            f"origin/main moved from {expected_base_sha} to {current_origin_main}; refusing to promote stale repair"
        )
    return current_origin_main


def verify_candidate_commit(project_root: Path, *, candidate_sha: str, expected_base_sha: str) -> str:
    current_origin_main = verify_origin_main_base(project_root, expected_base_sha=expected_base_sha)
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected_base_sha, candidate_sha],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"repair candidate {candidate_sha} does not descend from verified base {expected_base_sha}")
    return current_origin_main


def sync_runtime_repo_to_promoted_main(project_root: Path, *, promoted_sha: str) -> str:
    current_origin_main = verified_origin_main_sha(project_root)
    if current_origin_main != promoted_sha:
        raise RuntimeError(
            f"origin/main is {current_origin_main}, expected promoted repair {promoted_sha}; refusing runtime sync"
        )
    if _run_git(project_root, ["status", "--porcelain"]).stdout.strip():
        raise RuntimeError("runtime repo has local changes; refusing to sync promoted repair into a dirty checkout")
    current_head = read_ref_sha(project_root, "HEAD")
    if current_head != promoted_sha:
        _run_git(project_root, ["checkout", "--detach", promoted_sha])
    synced_head = read_ref_sha(project_root, "HEAD")
    if synced_head != promoted_sha:
        raise RuntimeError(f"runtime repo synced to {synced_head}, expected promoted repair {promoted_sha}")
    return synced_head


def cleanup_repair_worktree(worktree: RepairWorktree) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree.path)],
        cwd=worktree.path.parent,
        check=True,
        capture_output=True,
        text=True,
    )
