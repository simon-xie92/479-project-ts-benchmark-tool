"""Host-side orchestration: clone repos, then run the two container phases per repo.

Phase 1 (install)  runs with network access so dependencies can be fetched.
Phase 2 (measure)  runs with --network none, so test suites cannot depend on the
internet (reproducibility) and untrusted repo code cannot phone home.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import provenance
from .util import docker_name, unit_id


@dataclass
class RunOptions:
    image: str = "ts-bench:latest"
    memory: str = "8g"
    cpus: float = 16
    install_timeout: int = 1800          # per install attempt, seconds
    measure_timeout: int = 4 * 3600      # whole measure container, seconds
    mutation_timeout: int = 900         # StrykerJS only
    max_mutate_files: int = 40
    stryker_concurrency: int = 2
    skip_mutation: bool = False
    force: bool = False


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _default_ref(tree: Path) -> str:
    r = subprocess.run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", "origin/HEAD"],
                       capture_output=True, text=True)
    return "origin/HEAD" if r.returncode == 0 else "HEAD"


def resolve_commit(tree: Path, row: dict[str, str]) -> str:
    """Which commit to measure.  Priority: `commit` column > `snapshot_before` date > default branch tip."""
    ref = (row.get("commit") or "").strip()
    if ref:
        for cand in (ref, f"origin/{ref}"):  # SHA / tag, or a branch name (clones only keep origin/<branch>)
            r = subprocess.run(["git", "-C", str(tree), "rev-parse", "--verify", "-q", f"{cand}^{{commit}}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout.strip()
        raise RuntimeError(f"cannot resolve ref {ref!r} (is it a commit, tag or branch of this repo?)")
    snap = (row.get("snapshot_before") or "").strip()
    if snap:
        sha = _git(tree, "rev-list", "-1", f"--before={snap}", _default_ref(tree))
        if not sha:
            raise RuntimeError(f"no commit before {snap}")
        return sha
    return _git(tree, "rev-parse", _default_ref(tree))


def prepare_tree(row: dict[str, str], work_dir: Path) -> tuple[Path, str]:
    """Make work/trees/<unit> a real git clone checked out at the unit's pinned commit; return (path, sha).

    Every unit gets its OWN partial clone (full history, file contents fetched only for the checked-out
    commit), so two versions of one project can be measured side by side.  A real `.git` directory matters:
    install scripts such as `husky install` call git inside the container, and a git *worktree* would
    point at a host path that does not exist there.
    """
    tree = work_dir / "trees" / unit_id(row)
    fresh = not tree.exists()
    if fresh:
        url = row.get("url") or f"https://github.com/{row['repo']}.git"
        tree.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(tree)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr.strip()[:300]}")
    sha = resolve_commit(tree, row)
    if fresh or not (tree / ".git" / "index").exists():
        _git(tree, "checkout", "--detach", "--force", sha)
    elif _git(tree, "rev-parse", "HEAD") != sha:
        _git(tree, "checkout", "--detach", "--force", sha)
        _git(tree, "clean", "-fdxq")
    return tree, sha


def docker_cmd(image: str, repo_dir: Path, results_dir: Path, phase: str, *, subdir: str, name: str,
               memory: str, cpus: float, network: bool, extra: list[str]) -> list[str]:
    cmd = ["docker", "run", "--rm", "--name", name, "--memory", memory, "--cpus", str(cpus),
           "--pids-limit", "4096", "--security-opt", "no-new-privileges",
           "-v", f"{repo_dir.resolve()}:/work/repo", "-v", f"{results_dir.resolve()}:/results",
           "-w", "/work/repo", "-e", "HOME=/tmp", "-e", "CI=true",
           "-e", "HUSKY=0",  # skip git-hook installers; harmless if absent
           "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*"]
    if hasattr(os, "getuid"):  # keep bind-mounted files owned by the host user
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if not network:
        cmd += ["--network", "none"]
    cmd += [image, "python3", "-m", "bench.container_measure", phase, "--subdir", subdir, *extra]
    return cmd


def _run_container(cmd: list[str], name: str, timeout: int) -> tuple[int | None, bool]:
    try:
        p = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True, errors="replace")
        return p.returncode, False
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return None, True


def run_repo(row: dict[str, str], trees_dir: Path, results_dir: Path, opts: RunOptions) -> str:
    uid = unit_id(row)
    tree, res_dir = trees_dir / uid, results_dir / uid
    if not tree.exists():
        return f"{uid}: no checked-out tree (run `check` or `clone` first)"
    if (res_dir / "result.json").exists() and not opts.force:
        return f"{uid}: already done (use --force to redo)"
    res_dir.mkdir(parents=True, exist_ok=True)
    subdir = (row.get("subdir") or "").strip().strip("/")

    (res_dir / "meta.json").write_text(json.dumps({
        "repo": row["repo"], "slug": uid, "unit": uid, "group": row.get("group"),
        "cohort": row.get("cohort") or row.get("group"), "version": row.get("version", ""),
        "basis": row.get("basis"), "subdir": subdir, "notes": row.get("notes", ""),
        "commit": _git(tree, "rev-parse", "HEAD"), "commit_date": _git(tree, "log", "-1", "--format=%cI"),
        "provenance": provenance.scan_repo(tree, subdir),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "options": opts.__dict__,
    }, indent=2))

    tag = uuid.uuid4().hex[:8]
    # ---- phase 1: install (network on) ------------------------------------
    name = docker_name(f"tsbench-{uid}-install-{tag}")[:120]
    cmd = docker_cmd(opts.image, tree, res_dir, "install", subdir=subdir, name=name, memory=opts.memory,
                     cpus=opts.cpus, network=True, extra=["--install-timeout", str(opts.install_timeout)])
    _, timed_out = _run_container(cmd, name, opts.install_timeout * 3 + 300)
    if timed_out and not (res_dir / "install.json").exists():
        (res_dir / "install.json").write_text(json.dumps({"status": "timeout"}))

    # ---- phase 2: measure (network off) -----------------------------------
    name = docker_name(f"tsbench-{uid}-measure-{tag}")[:120]
    extra = ["--max-mutate-files", str(opts.max_mutate_files), "--mutation-timeout", str(opts.mutation_timeout),
             "--stryker-concurrency", str(opts.stryker_concurrency)]
    if opts.skip_mutation:
        extra.append("--skip-mutation")
    cmd = docker_cmd(opts.image, tree, res_dir, "measure", subdir=subdir, name=name, memory=opts.memory,
                     cpus=opts.cpus, network=False, extra=extra)
    _, timed_out = _run_container(cmd, name, opts.measure_timeout)
    if not (res_dir / "result.json").exists():
        (res_dir / "result.json").write_text(json.dumps(
            {"schema": 1, "status": "container_timeout" if timed_out else "container_failed"}))
        return f"{uid}: FAILED ({'timeout' if timed_out else 'container error'})"
    return f"{uid}: done"


def build_image(root: Path, image: str) -> None:
    subprocess.run(["docker", "build", "-f", "docker/Dockerfile", "-t", image, "."], cwd=root, check=True)
