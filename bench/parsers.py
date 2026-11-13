"""Pure parsing / aggregation helpers (no I/O, no subprocesses).

Shared by the in-container collector, the host CLI and the unit tests, so every
number in the study goes through exactly one implementation.
"""
from __future__ import annotations

import math
import os
import re
import statistics
from collections import Counter
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# File classification
# --------------------------------------------------------------------------- #

_TEST_PATH_RE = re.compile(
    r"(^|/)(__tests__|__mocks__|__fixtures__|tests?|specs?|e2e|cypress)(/|$)"
    r"|\.(test|spec|e2e)\.[cm]?[jt]sx?$",
    re.IGNORECASE,
)
_TS_EXTS = (".ts", ".tsx", ".mts", ".cts")
_DTS_EXTS = (".d.ts", ".d.mts", ".d.cts")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.replace("\\", "/")))


def is_ts_source_file(path: str) -> bool:
    """TypeScript file that is not a declaration file."""
    p = path.lower()
    return p.endswith(_TS_EXTS) and not p.endswith(_DTS_EXTS)


# --------------------------------------------------------------------------- #
# tsc
# --------------------------------------------------------------------------- #

_TSC_FILE_LINE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<sev>error|warning)\s+TS(?P<code>\d+):\s*(?P<msg>.*)$"
)
_TSC_GLOBAL_LINE = re.compile(r"^(?P<sev>error|warning)\s+TS(?P<code>\d+):\s*(?P<msg>.*)$")

# "Cannot find module / type definitions / name 'require'|'describe'" -- these
# mostly indicate a missing dependency or @types package, i.e. an environment
# problem rather than a genuine type error.  Reported separately for validity.
ENV_SUSPECT_CODES = {2307, 2688, 2792, 7016, 2580, 2581, 2582, 2591, 2592, 2593}


def tsc_category(code: int) -> str:
    if 1000 <= code < 2000:
        return "syntax"
    if 2000 <= code < 3000:
        return "semantic"
    if 7000 <= code < 8000:
        return "strictness"  # noImplicitAny & friends
    if 5000 <= code < 7000 or 18000 <= code < 19000:
        return "config"
    return "other"


def parse_tsc_output(text: str) -> dict[str, Any]:
    """Parse `tsc --pretty false` output into counts."""
    by_code: Counter[int] = Counter()
    by_cat: Counter[str] = Counter()
    files: set[str] = set()
    warnings = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _TSC_FILE_LINE.match(line)
        file = None
        if m:
            file = m.group("file")
        else:
            m = _TSC_GLOBAL_LINE.match(line)
        if not m:
            continue  # continuation line of a multi-line message
        if m.group("sev") == "warning":
            warnings += 1
            continue
        code = int(m.group("code"))
        by_code[code] += 1
        by_cat[tsc_category(code)] += 1
        if file:
            files.add(file)
    total = sum(by_code.values())
    return {
        "errors": total,
        "warnings": warnings,
        "files_with_errors": len(files),
        "by_category": dict(by_cat),
        "env_suspect_errors": sum(c for code, c in by_code.items() if code in ENV_SUSPECT_CODES),
        "top_codes": [[f"TS{c}", n] for c, n in by_code.most_common(15)],
    }


# --------------------------------------------------------------------------- #
# ESLint
# --------------------------------------------------------------------------- #

_CYCLO_RE = re.compile(r"has a complexity of (\d+)")


def summarize_complexity(values: Iterable[int]) -> dict[str, Any]:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return {"functions": 0, "mean": None, "median": None, "p90": None, "max": None,
                "share_gt10": None, "share_gt20": None}
    p90 = vals[min(n - 1, math.ceil(0.9 * n) - 1)]
    return {
        "functions": n,
        "mean": round(statistics.fmean(vals), 3),
        "median": statistics.median(vals),
        "p90": p90,
        "max": vals[-1],
        "share_gt10": round(sum(v > 10 for v in vals) / n, 4),
        "share_gt20": round(sum(v > 20 for v in vals) / n, 4),
    }


def _rule_plugin(rule_id: str | None) -> str:
    if not rule_id:
        return "parser"  # parse errors have no rule id
    if rule_id.startswith("@typescript-eslint/"):
        return "typescript-eslint"
    if rule_id.startswith("sonarjs/"):
        return "sonarjs"
    return "core"


def parse_eslint_json(results: list[dict], root: str = "") -> dict[str, Any]:
    """Aggregate ESLint's JSON formatter output.

    The `complexity` rule (threshold 0) is used purely as a cyclomatic
    complexity probe: its messages are removed from the violation counts and
    summarised separately, for production (non-test) files only.
    """
    errors = warnings = fatal = 0
    src_errors = src_warnings = 0
    by_rule: Counter[str] = Counter()
    by_plugin: Counter[str] = Counter()
    files_with_issues = 0
    cyclo: list[int] = []

    for res in results:
        path = res.get("filePath", "")
        rel = os.path.relpath(path, root) if root and os.path.isabs(path) else path
        in_test = is_test_path(rel)
        had_issue = False
        for msg in res.get("messages", []):
            rule = msg.get("ruleId")
            if rule == "complexity":
                m = _CYCLO_RE.search(msg.get("message", ""))
                if m and not in_test:
                    cyclo.append(int(m.group(1)))
                continue
            had_issue = True
            sev = msg.get("severity", 1)
            if msg.get("fatal"):
                fatal += 1
            if sev == 2:
                errors += 1
                src_errors += 0 if in_test else 1
            else:
                warnings += 1
                src_warnings += 0 if in_test else 1
            by_rule[rule or "parse-error"] += 1
            by_plugin[_rule_plugin(rule)] += 1
        files_with_issues += had_issue

    return {
        "files_linted": len(results),
        "files_with_issues": files_with_issues,
        "errors": errors,
        "warnings": warnings,
        "total": errors + warnings,
        "fatal_parse_errors": fatal,
        "src_total": src_errors + src_warnings,
        "by_plugin": dict(by_plugin),
        "top_rules": [[r, n] for r, n in by_rule.most_common(15)],
        "cyclomatic": summarize_complexity(cyclo),
    }


# --------------------------------------------------------------------------- #
# cloc / jscpd
# --------------------------------------------------------------------------- #

def parse_cloc(by_file: dict[str, Any]) -> dict[str, Any]:
    """Aggregate `cloc --json --by-file` output for TypeScript files only."""
    agg = {k: 0 for k in ("src_code", "src_comment", "src_blank", "test_code", "test_comment", "test_blank")}
    n_src = n_test = 0
    for path, info in by_file.items():
        if path in ("header", "SUM") or not isinstance(info, dict):
            continue
        norm = os.path.normpath(path).replace("\\", "/")
        if not is_ts_source_file(norm):
            continue
        kind = "test" if is_test_path(norm) else "src"
        agg[f"{kind}_code"] += int(info.get("code", 0))
        agg[f"{kind}_comment"] += int(info.get("comment", 0))
        agg[f"{kind}_blank"] += int(info.get("blank", 0))
        if kind == "test":
            n_test += 1
        else:
            n_src += 1
    agg["src_files"] = n_src
    agg["test_files"] = n_test
    agg["ts_code_total"] = agg["src_code"] + agg["test_code"]
    agg["comment_ratio"] = round(agg["src_comment"] / agg["src_code"], 4) if agg["src_code"] else None
    agg["test_to_src_loc"] = round(agg["test_code"] / agg["src_code"], 4) if agg["src_code"] else None
    return agg


def parse_jscpd(report: dict[str, Any]) -> dict[str, Any]:
    total = (report.get("statistics") or {}).get("total") or {}
    return {
        "lines": total.get("lines"),
        "duplicated_lines": total.get("duplicatedLines"),
        "pct_lines": total.get("percentage"),
        "tokens": total.get("tokens"),
        "duplicated_tokens": total.get("duplicatedTokens"),
        "pct_tokens": total.get("percentageTokens"),
        "clones": total.get("clones"),
    }


# --------------------------------------------------------------------------- #
# Stryker
# --------------------------------------------------------------------------- #

def parse_stryker(report: dict[str, Any]) -> dict[str, Any]:
    """Mutation score using Stryker's own definition.

    detected   = Killed + Timeout
    undetected = Survived + NoCoverage
    invalid    = CompileError + RuntimeError  (excluded)
    score      = detected / (detected + undetected)
    covered    = detected / (detected + Survived)   (ignores un-covered code)
    """
    counts: Counter[str] = Counter()
    for f in (report.get("files") or {}).values():
        for m in f.get("mutants", []):
            counts[m.get("status", "Unknown")] += 1
    detected = counts["Killed"] + counts["Timeout"]
    undetected = counts["Survived"] + counts["NoCoverage"]
    covered = detected + counts["Survived"]
    n_tests = sum(len(tf.get("tests", [])) for tf in (report.get("testFiles") or {}).values())
    return {
        "files_mutated": len(report.get("files") or {}),
        "mutants_total": sum(counts.values()),
        "killed": counts["Killed"],
        "survived": counts["Survived"],
        "no_coverage": counts["NoCoverage"],
        "timeout": counts["Timeout"],
        "invalid": counts["CompileError"] + counts["RuntimeError"],
        "ignored": counts["Ignored"],
        "mutation_score": round(100 * detected / (detected + undetected), 2) if detected + undetected else None,
        "mutation_score_covered": round(100 * detected / covered, 2) if covered else None,
        "n_tests": n_tests or None,
    }


# --------------------------------------------------------------------------- #
# package.json helpers
# --------------------------------------------------------------------------- #

STRYKER_RUNNERS = ("jest", "vitest", "mocha", "jasmine")


def detect_test_runner(pkg: dict[str, Any]) -> str | None:
    """Pick the test runner StrykerJS should drive (None = unsupported/unknown)."""
    script = (pkg.get("scripts") or {}).get("test", "") or ""
    for name in STRYKER_RUNNERS:
        if re.search(rf"\b{name}\b", script):
            return name
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps or "ts-jest" in deps or "@jest/core" in deps or "jest" in pkg:
        return "jest"
    if "mocha" in deps:
        return "mocha"
    if "jasmine" in deps or "jasmine-core" in deps:
        return "jasmine"
    return None
