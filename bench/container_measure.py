"""Runs INSIDE the Docker container.

    python3 -m bench.container_measure install  [--subdir X]   (network on)
    python3 -m bench.container_measure measure  [--subdir X]   (network off)

Every measurement step is isolated: a crash or timeout in one tool is recorded
in the result JSON and never prevents the other tools from running.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import parsers

TOOLS = Path(os.environ.get("BENCH_TOOLS", "/opt/bench"))
NODE_BIN = TOOLS / "node_modules" / ".bin"
REPO_ROOT = Path(os.environ.get("BENCH_REPO", "/work/repo"))
RESULTS = Path(os.environ.get("BENCH_RESULTS", "/results"))

LOG_TAIL = 3000
EXCLUDE_DIRS = "node_modules,dist,build,coverage,.git,.stryker-tmp,out,.next,reports"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def run(cmd: list, cwd: Path, timeout: int, env: dict | None = None) -> dict[str, Any]:
    """Run a command in its own process group; kill the whole group on timeout."""
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd), env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc), "timed_out": False, "seconds": 0.0}
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
    return {"returncode": proc.returncode, "stdout": out or "", "stderr": err or "",
            "timed_out": timed_out, "seconds": round(time.time() - t0, 1)}


def tail(text: str, n: int = LOG_TAIL) -> str:
    return text[-n:] if len(text) > n else text


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def step(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    t0 = time.time()
    try:
        res = fn()
    except Exception as exc:  # noqa: BLE001 - we want to survive anything
        res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    res.setdefault("status", "ok")
    res["seconds"] = round(time.time() - t0, 1)
    return res


def find_local_bin(name: str, work: Path) -> Path | None:
    """Look for node_modules/.bin/<name> in the work dir and its parents (monorepos)."""
    d = work
    while True:
        cand = d / "node_modules" / ".bin" / name
        if cand.exists():
            return cand
        if d == REPO_ROOT or d == d.parent:
            return None
        d = d.parent


# --------------------------------------------------------------------------- #
# phase 1: install
# --------------------------------------------------------------------------- #

def detect_pm(d: Path) -> str:
    if (d / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (d / "yarn.lock").exists():
        return "yarn"
    return "npm"


def do_install(subdir: str, timeout: int) -> dict[str, Any]:
    work = REPO_ROOT / subdir
    install_dir = REPO_ROOT if (REPO_ROOT / "package.json").exists() else work
    if not (install_dir / "package.json").exists():
        return {"status": "no_package_json"}

    npm_flags = ["--no-audit", "--no-fund", "--loglevel=error"]
    has_lock = any((install_dir / f).exists() for f in ("package-lock.json", "npm-shrinkwrap.json"))
    npm_first = ["npm", "ci" if has_lock else "install", *npm_flags]
    npm_legacy = ["npm", "install", "--legacy-peer-deps", *npm_flags]
    pm = detect_pm(install_dir)
    attempts = {
        "pnpm": [["pnpm", "install", "--frozen-lockfile"], ["pnpm", "install", "--no-frozen-lockfile"], npm_legacy],
        "yarn": [["yarn", "install", "--frozen-lockfile", "--non-interactive"], ["yarn", "install", "--non-interactive"], npm_legacy],
        "npm": [npm_first, npm_legacy],
    }[pm]

    log: list[dict] = []
    t0 = time.time()
    ok = False
    for cmd in attempts:
        r = run(cmd, install_dir, timeout)
        log.append({"cmd": " ".join(cmd), "returncode": r["returncode"], "timed_out": r["timed_out"],
                    "seconds": r["seconds"], "stderr_tail": tail(r["stderr"], 1500)})
        if r["returncode"] == 0:
            ok = True
            break
        if r["timed_out"]:
            break
    return {"status": "ok" if ok else "failed", "package_manager": pm, "install_dir": str(install_dir),
            "attempts": log, "total_seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- #
# phase 2: measurements
# --------------------------------------------------------------------------- #

def measure_loc(work: Path) -> dict[str, Any]:
    r = run(["cloc", "--json", "--by-file", "--quiet", f"--exclude-dir={EXCLUDE_DIRS}", "."], work, 600)
    if not r["stdout"].strip():
        return {"status": "error", "error": tail(r["stderr"], 500)}
    return parsers.parse_cloc(json.loads(r["stdout"]))


def _write_fallback_tsconfig(work: Path) -> Path:
    cfg = {
        "compilerOptions": {
            "noEmit": True, "skipLibCheck": True, "target": "ES2020", "module": "commonjs",
            "moduleResolution": "node", "esModuleInterop": True, "resolveJsonModule": True, "jsx": "react-jsx",
        },
        "include": ["**/*.ts", "**/*.tsx"],
        "exclude": ["node_modules", "dist", "build", "coverage", "out", ".stryker-tmp"],
    }
    path = work / "tsconfig.bench.generated.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


def measure_tsc(work: Path) -> dict[str, Any]:
    local = find_local_bin("tsc", work)
    tsc = local or (NODE_BIN / "tsc")
    version = run([tsc, "--version"], work, 60)["stdout"].strip()

    tsconfig = work / "tsconfig.json"
    used_fallback = not tsconfig.exists()
    cfg_file = _write_fallback_tsconfig(work) if used_fallback else tsconfig

    sc = run([tsc, "--showConfig", "-p", cfg_file.name], work, 120)
    try:
        cfg = json.loads(sc["stdout"])
    except ValueError:
        cfg = {}
    opts = cfg.get("compilerOptions", {}) or {}
    solution_style = bool(cfg.get("references")) and not cfg.get("files")

    if solution_style:
        cmd = [tsc, "-b", cfg_file.name, "--pretty", "false"]
    else:
        cmd = [tsc, "-p", cfg_file.name, "--noEmit", "--pretty", "false"]
    r = run(cmd, work, 1800, env={"NODE_OPTIONS": "--max-old-space-size=4096"})
    combined = r["stdout"] + "\n" + r["stderr"]
    (RESULTS / "tsc_output.txt").write_text(combined, encoding="utf-8", errors="replace")

    base = {
        "version": version, "source": "project" if local else "bundled",
        "used_fallback_config": used_fallback, "project_mode": "solution" if solution_style else "project",
        "strict": opts.get("strict"), "no_implicit_any": opts.get("noImplicitAny"),
    }
    if r["timed_out"]:
        return {**base, "status": "timeout"}
    if r["returncode"] == 127:
        return {**base, "status": "error", "error": tail(r["stderr"], 500)}
    parsed = parsers.parse_tsc_output(combined)
    return {**base, **parsed, "clean": parsed["errors"] == 0}


def measure_eslint(work: Path) -> dict[str, Any]:
    out = RESULTS / "eslint_raw.json"
    out.unlink(missing_ok=True)
    cmd = [NODE_BIN / "eslint", "-c", TOOLS / "eslint.config.mjs", "-f", "json", "-o", out,
           "--no-warn-ignored", "--no-inline-config", "."]
    r = run(cmd, work, 1800, env={"NODE_OPTIONS": "--max-old-space-size=4096"})
    if r["timed_out"]:
        return {"status": "timeout"}
    data = read_json(out)
    if data is None:
        return {"status": "error", "returncode": r["returncode"], "error": tail(r["stderr"] or r["stdout"], 800)}
    return parsers.parse_eslint_json(data, root=str(work))


def measure_duplication(work: Path) -> dict[str, Any]:
    out_dir = RESULTS / "jscpd"
    shutil.rmtree(out_dir, ignore_errors=True)
    ignore = ",".join([
        "**/node_modules/**", "**/dist/**", "**/build/**", "**/coverage/**", "**/.stryker-tmp/**",
        "**/*.d.ts", "**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/__mocks__/**", "**/test/**", "**/tests/**",
    ])
    cmd = [NODE_BIN / "jscpd", "--silent", "--reporters", "json", "--output", out_dir,
           "--format", "typescript,tsx", "--min-tokens", "50", "--min-lines", "5",
           "--ignore", ignore, "--gitignore", "."]
    r = run(cmd, work, 900)
    if r["timed_out"]:
        return {"status": "timeout"}
    report = read_json(out_dir / "jscpd-report.json")
    if report is None:
        return {"status": "no_data", "error": tail(r["stderr"] or r["stdout"], 500)}
    return parsers.parse_jscpd(report)


def measure_tests(work: Path) -> dict[str, Any]:
    pkg = read_json(work / "package.json", {}) or {}
    script = (pkg.get("scripts") or {}).get("test", "")
    runner = parsers.detect_test_runner(pkg)
    if not script or "no test specified" in script:
        return {"status": "no_test_script", "runner": runner}
    r = run(["npm", "test", "--silent"], work, 1800, env={"CI": "true"})
    status = "timeout" if r["timed_out"] else ("pass" if r["returncode"] == 0 else "fail")
    return {"status": status, "runner": runner, "returncode": r["returncode"],
            "log_tail": tail(r["stdout"] + "\n" + r["stderr"], 1500)}


def select_mutation_files(work: Path, cap: int, seed: str = "ts-bench") -> tuple[list[str], int]:
    """Production .ts files to mutate; deterministic sample when there are more than `cap`."""
    patterns = ["src/**/*", "lib/**/*", "packages/*/src/**/*", "apps/*/src/**/*"]
    found: set[str] = set()
    for pat in patterns:
        for p in work.glob(pat):
            rel = p.relative_to(work).as_posix()
            if p.is_file() and parsers.is_ts_source_file(rel) and not parsers.is_test_path(rel):
                found.add(rel)
    if not found:  # unusual layout: take every non-test TS file
        for p in work.rglob("*"):
            rel = p.relative_to(work).as_posix()
            if "node_modules/" in rel or rel.startswith(("dist/", "build/", ".stryker-tmp/")):
                continue
            if p.is_file() and parsers.is_ts_source_file(rel) and not parsers.is_test_path(rel):
                found.add(rel)
    files = sorted(found)
    total = len(files)
    if total > cap:
        files = sorted(sorted(files, key=lambda f: hashlib.sha1(f"{seed}:{f}".encode()).hexdigest())[:cap])
    return files, total


def measure_mutation(work: Path, tests: dict, tsc: dict, args: argparse.Namespace) -> dict[str, Any]:
    if args.skip_mutation:
        return {"status": "skipped", "reason": "disabled"}
    if tests.get("status") != "pass":
        return {"status": "skipped", "reason": f"baseline tests: {tests.get('status')}"}
    runner = tests.get("runner")
    if runner not in parsers.STRYKER_RUNNERS:
        return {"status": "skipped", "reason": f"unsupported test runner: {runner}"}
    files, total = select_mutation_files(work, args.max_mutate_files)
    if not files:
        return {"status": "skipped", "reason": "no production TypeScript files"}

    report_path = RESULTS / "mutation.json"
    report_path.unlink(missing_ok=True)
    conf: dict[str, Any] = {
        "testRunner": runner,
        "mutate": files,
        "reporters": ["json"],
        "jsonReporter": {"fileName": str(report_path)},
        "coverageAnalysis": "perTest",
        "ignoreStatic": True,
        "timeoutMS": 10000,
        "timeoutFactor": 2,
        "concurrency": args.stryker_concurrency,
        "cleanTempDir": True,
    }
    # The TypeScript checker aborts when the *unmutated* project has type errors,
    # so only enable it for projects that compile cleanly with their own tsconfig.
    if tsc.get("clean") and not tsc.get("used_fallback_config") and tsc.get("project_mode") == "project":
        conf["checkers"] = ["typescript"]
        conf["tsconfigFile"] = "tsconfig.json"
    conf_path = RESULTS / "stryker.conf.json"
    conf_path.write_text(json.dumps(conf, indent=2))

    r = run([NODE_BIN / "stryker", "run", conf_path], work, args.mutation_timeout,
            env={"NODE_OPTIONS": "--max-old-space-size=4096"})
    shutil.rmtree(work / ".stryker-tmp", ignore_errors=True)
    base = {"runner": runner, "files_selected": len(files), "files_available": total,
            "typescript_checker": "checkers" in conf}
    report = read_json(report_path)
    if report is None:
        status = "timeout" if r["timed_out"] else "failed"
        return {**base, "status": status, "log_tail": tail(r["stdout"] + "\n" + r["stderr"], 2000)}
    return {**base, **parsers.parse_stryker(report)}


def do_measure(args: argparse.Namespace) -> dict[str, Any]:
    work = REPO_ROOT / args.subdir
    result: dict[str, Any] = {
        "schema": 1,
        "subdir": args.subdir,
        "install": read_json(RESULTS / "install.json", {"status": "missing"}),
        "node": run(["node", "--version"], work, 30)["stdout"].strip(),
    }
    result["loc"] = step(lambda: measure_loc(work))
    result["tsc"] = step(lambda: measure_tsc(work))
    result["eslint"] = step(lambda: measure_eslint(work))
    result["duplication"] = step(lambda: measure_duplication(work))
    result["tests"] = step(lambda: measure_tests(work))
    result["mutation"] = step(lambda: measure_mutation(work, result["tests"], result["tsc"], args))
    return result


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["install", "measure"])
    ap.add_argument("--subdir", default="")
    ap.add_argument("--install-timeout", type=int, default=1800)
    ap.add_argument("--max-mutate-files", type=int, default=40)
    ap.add_argument("--mutation-timeout", type=int, default=3600)
    ap.add_argument("--stryker-concurrency", type=int, default=2)
    ap.add_argument("--skip-mutation", action="store_true")
    args = ap.parse_args(argv)

    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.phase == "install":
        res = step(lambda: do_install(args.subdir, args.install_timeout))
        (RESULTS / "install.json").write_text(json.dumps(res, indent=2))
    else:
        res = do_measure(args)
        (RESULTS / "result.json").write_text(json.dumps(res, indent=2))
    print(json.dumps({k: (v.get("status") if isinstance(v, dict) else v) for k, v in res.items()
                      if k in ("status", "install", "loc", "tsc", "eslint", "duplication", "tests", "mutation")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
