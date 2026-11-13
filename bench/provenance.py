"""Decide whether a repository is human-written or LLM-generated.

Dates alone are weak evidence for "LLM-generated" (a 2024 repo is usually still
human-written), so we look for positive evidence in the git history first and
only fall back to the proposal's date rule when explicitly enabled:

  human  : first commit before --human-before AND no LLM evidence at all
  llm    : first commit on/after --llm-after AND >= --llm-share-threshold of commits carry an LLM trailer
           (basis = "commit-trailers"), or, with --allow-date-only-llm, no evidence but date qualifies
           (basis = "date-only"  <- filter these out in a sensitivity analysis!)
  mixed  : some LLM evidence (marker files / a few trailers) but below the threshold
  excluded: anything else (2021-2023 gap, pre-2021 repo with later LLM edits, ...)
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import parsers

LLM_MARKER_FILES = {
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules",
    ".github/copilot-instructions.md", ".aider.conf.yml", ".aiderignore",
}
LLM_MARKER_PREFIXES = (".cursor/", ".claude/", ".kiro/")

LLM_COMMIT_RE = re.compile(
    r"co-authored-by:[^\n]*(claude|copilot|cursor|aider|codex|gemini|chatgpt|openai)"
    r"|generated with \[?(claude|github copilot|cursor)"
    r"|noreply@anthropic\.com"
    r"|copilot-swe-agent"
    r"|\(aider\)"
    r"|^aider:",
    re.IGNORECASE | re.MULTILINE,
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def scan_repo(repo: Path, subdir: str = "") -> dict[str, Any]:
    """Collect provenance evidence + cheap size/testability indicators from a checked-out tree.

    Git history is scanned repo-wide (up to the checked-out commit); size/test indicators and
    package.json only look inside `subdir` (for monorepos).
    """
    fmt = "%aI%x1f%an <%ae>%x1f%B%x1e"
    log = _git(repo, "log", "--max-count=20000", f"--format={fmt}")
    dates: list[str] = []
    llm_commits = 0
    for entry in log.split("\x1e"):
        entry = entry.strip("\n")
        if not entry.strip():
            continue
        parts = entry.split("\x1f", 2)
        if len(parts) < 3:
            continue
        dates.append(parts[0].strip())
        if LLM_COMMIT_RE.search(parts[1] + "\n" + parts[2]):
            llm_commits += 1

    files = [f for f in _git(repo, "ls-files").splitlines() if f]
    markers = sorted(f for f in files if f in LLM_MARKER_FILES or f.startswith(LLM_MARKER_PREFIXES))
    prefix = subdir.strip("/") + "/" if subdir.strip("/") else ""
    ts_files = [f for f in files if f.startswith(prefix) and parsers.is_ts_source_file(f) and "node_modules/" not in f]
    ts_tests = [f for f in ts_files if parsers.is_test_path(f)]

    pkg: dict[str, Any] = {}
    pkg_path = repo / subdir.strip("/") / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except ValueError:
            pkg = {}

    parsed = sorted(datetime.fromisoformat(d) for d in dates if d)
    n = len(dates)
    return {
        "first_commit": parsed[0].isoformat() if parsed else "",
        "last_commit": parsed[-1].isoformat() if parsed else "",
        "n_commits": n,
        "llm_commits": llm_commits,
        "llm_share": round(llm_commits / n, 4) if n else 0.0,
        "marker_files": "|".join(markers),
        "ts_files": len(ts_files),
        "ts_test_files": len(ts_tests),
        "has_package_json": bool(pkg),
        "test_runner": parsers.detect_test_runner(pkg) or "",
    }


def classify(info: dict[str, Any], *, human_before: str = "2021-01-01", llm_after: str = "2023-01-01",
             share_threshold: float = 0.5, allow_date_only_llm: bool = False) -> tuple[str, str]:
    """Return (group, basis)."""
    if not info.get("first_commit"):
        return "excluded", "no commits"
    first = datetime.fromisoformat(info["first_commit"]).date()
    h_cut, l_cut = date.fromisoformat(human_before), date.fromisoformat(llm_after)
    markers = bool(info.get("marker_files"))
    llm_commits = int(info.get("llm_commits", 0))
    share = float(info.get("llm_share", 0.0))
    any_evidence = markers or llm_commits > 0

    if first < h_cut:
        if any_evidence:
            return "excluded", "pre-cutoff repo with later LLM evidence (use snapshot_before)"
        return "human", "date"
    if first < l_cut:
        return "excluded", "first commit in the gap between cutoffs"
    if share >= share_threshold:
        return "llm", "commit-trailers"
    if any_evidence:
        return "mixed", f"partial LLM evidence (share={share:.2f}, markers={markers})"
    if allow_date_only_llm:
        return "llm", "date-only"
    return "excluded", "post-cutoff repo without any LLM evidence"


def select_matched(rows: list[dict[str, Any]], n: int, *, require_tests: bool = True,
                   require_runner: bool = True) -> list[dict[str, Any]]:
    """Greedy size-matching on log(#TS files): up to n human/LLM pairs, closest pairs first."""
    def ok(r: dict[str, Any]) -> bool:
        if str(r.get("has_package_json")).lower() not in ("true", "1"):
            return False
        if require_tests and int(r.get("ts_test_files") or 0) == 0:
            return False
        if require_runner and not r.get("test_runner"):
            return False
        return int(r.get("ts_files") or 0) > 0

    humans = [r for r in rows if r.get("group") == "human" and ok(r)]
    llms = [r for r in rows if r.get("group") == "llm" and ok(r)]
    cand = sorted(
        (abs(math.log1p(int(h["ts_files"])) - math.log1p(int(l["ts_files"]))), hi, li)
        for hi, h in enumerate(humans) for li, l in enumerate(llms)
    )
    used_h: set[int] = set()
    used_l: set[int] = set()
    chosen: list[dict[str, Any]] = []
    for _, hi, li in cand:
        if hi in used_h or li in used_l:
            continue
        used_h.add(hi)
        used_l.add(li)
        chosen += [humans[hi], llms[li]]
        if len(used_h) == n:
            break
    return chosen


def check_row(row: dict[str, Any], info: dict[str, Any], *, human_before: str = "2021-01-01") -> list[str]:
    """Pre-flight warnings for a hand-written repos.csv row. Advisory only: never changes the label."""
    warns: list[str] = []
    group = row.get("group", "")
    if group not in ("human", "llm"):
        warns.append(f"group must be 'human' or 'llm' (got {group!r}); the row will be skipped by `run`")
    if not info["has_package_json"]:
        warns.append("no package.json at the measured root (monorepo? set subdir)")
    if info["ts_test_files"] == 0:
        warns.append("no TypeScript test files found")
    if not info["test_runner"]:
        warns.append("test runner not detected / unsupported by StrykerJS -> no mutation score")
    evidence = info["llm_commits"] > 0 or bool(info["marker_files"])
    if group == "llm" and not evidence:
        warns.append("labelled llm but no LLM trailers/marker files up to this commit: note your evidence in `notes`")
    if group == "human" and evidence:
        warns.append("labelled human but LLM evidence exists up to this commit (trailers or marker files)")
    if group == "human" and info.get("first_commit"):
        if datetime.fromisoformat(info["first_commit"]).date() >= date.fromisoformat(human_before):
            warns.append(f"labelled human but first commit is on/after {human_before}")
    return warns
