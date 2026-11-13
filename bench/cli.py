"""python -m bench <command>

Manual workflow (you supply the projects):
  import       turn a text file of repo links into data/repos.csv
  check        fetch every row at its pinned commit and print a pre-flight report (advisory only)
  pin          freeze every row to the exact commit it resolves to (reproducible selection)
  run          install + measure every row inside Docker
  analyze      scores, group / cohort / version comparisons, figures

Optional discovery helpers (only if you want help finding candidates):
  collect      GitHub search -> candidates CSV
  clone        fetch every row of a CSV (one pinned clone per unit)
  provenance   label candidates human / llm / mixed / excluded from git-history evidence
  select       size-matched human/LLM pairs from a labelled CSV

  build-image  docker build
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# from . import collect as collect_mod
from . import importer
from . import provenance
from . import runner
from .util import read_csv, unit_id, write_csv

ROOT = Path(__file__).resolve().parent.parent


def _duplicates(rows: list[dict[str, str]]) -> list[str]:
    return [u for u, n in Counter(unit_id(r) for r in rows).items() if n > 1]


# ----------------------------------------------------------------- discovery helpers
# def cmd_collect(a: argparse.Namespace) -> None:
#     rows = collect_mod.collect(
#         a.group, limit=a.limit, keywords=a.keyword or [], sort=a.sort, snapshot_before=a.snapshot_before or "",
#         human_before=a.human_before, llm_after=a.llm_after, min_stars=a.min_stars,
#         min_size_kb=a.min_size_kb, max_size_kb=a.max_size_kb)
#     write_csv(a.out, rows)
#     print(f"wrote {len(rows)} candidates to {a.out}")


def cmd_clone(a: argparse.Namespace) -> None:
    rows = read_csv(a.repos)
    ok = 0
    for row in rows:
        try:
            _, sha = runner.prepare_tree(row, Path(a.workdir))
            ok += 1
            print(f"{unit_id(row):55s} {sha[:10]}")
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {unit_id(row)}: {exc}")
    print(f"{ok}/{len(rows)} units ready in {Path(a.workdir) / 'trees'}")


def cmd_provenance(a: argparse.Namespace) -> None:
    out = []
    for row in read_csv(a.repos):
        try:
            tree, _ = runner.prepare_tree(row, Path(a.workdir))
            info = provenance.scan_repo(tree, row.get("subdir", ""))
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {row['repo']}: {exc}")
            continue
        group, basis = provenance.classify(
            info, human_before=a.human_before, llm_after=a.llm_after,
            share_threshold=a.llm_share_threshold, allow_date_only_llm=a.allow_date_only_llm)
        out.append({**row, **info, "group": group, "basis": basis})
        print(f"{row['repo']:50s} {group:9s} {basis}")
    write_csv(a.out, out)
    counts = {g: sum(r["group"] == g for r in out) for g in ("human", "llm", "mixed", "excluded")}
    print(f"\nwrote {a.out}: {counts}")


def cmd_select(a: argparse.Namespace) -> None:
    chosen = provenance.select_matched(read_csv(a.repos), a.n, require_tests=not a.allow_no_tests,
                                       require_runner=not a.allow_unsupported_runner)
    write_csv(a.out, chosen)
    print(f"selected {len(chosen)} repos ({len(chosen)//2} pairs) -> {a.out}")


# ----------------------------------------------------------------- manual workflow
def cmd_check(a: argparse.Namespace) -> None:
    rows = read_csv(a.repos)
    dups = _duplicates(rows)
    if dups:
        raise SystemExit(f"duplicate units {dups}: give repeated repos a distinct `version` (e.g. early / late)")
    hdr = f"{'unit':44s} {'group':6s} {'cohort':10s} {'commit':10s} {'date':10s} {'ts':>4s} {'tests':>5s} {'runner':8s} {'llm%':>5s}"
    print(hdr + "\n" + "-" * len(hdr))
    n_warn = 0
    for row in rows:
        uid = unit_id(row)
        try:
            tree, sha = runner.prepare_tree(row, Path(a.workdir))
            info = provenance.scan_repo(tree, row.get("subdir", ""))
            date = runner._git(tree, "log", "-1", "--format=%cs")
        except Exception as exc:  # noqa: BLE001
            print(f"{uid:44s} FAILED: {exc}")
            n_warn += 1
            continue
        print(f"{uid:44.44s} {row.get('group',''):6s} {(row.get('cohort') or row.get('group','')):10.10s} {sha[:10]} {date} "
              f"{info['ts_files']:>4d} {info['ts_test_files']:>5d} {info['test_runner'] or '-':8s} {100*info['llm_share']:>4.0f}%")
        for w in provenance.check_row(row, info, human_before=a.human_before):
            print(f"    ! {w}")
            n_warn += 1
    print(f"\n{len(rows)} units checked, {n_warn} warning(s). Warnings are advisory: your labels are never changed.")


def cmd_import(a: argparse.Namespace) -> None:
    import sys
    text = sys.stdin.read() if a.links == "-" else Path(a.links).read_text(encoding="utf-8-sig")
    rows, problems = importer.parse_links(text, {"group": a.group or "", "cohort": a.cohort or ""})
    out = Path(a.out)
    existing: list[dict[str, str]] = []
    if out.exists():
        if not (a.append or a.force):
            raise SystemExit(f"{out} exists: use --append to add to it or --force to overwrite")
        if a.append:
            existing = read_csv(out)
    problems += importer.check_duplicates([*existing, *rows])
    for level, msg in problems:
        print(f"{level.upper():7s} {msg}")
    if any(level == "error" for level, _ in problems):
        raise SystemExit("\nnothing written: fix the errors above and re-run")
    merged = [*existing, *({k: v for k, v in r.items() if k != "_line"} for r in rows)]
    cols = importer.COLUMNS + [c for r in merged for c in r if c not in importer.COLUMNS]
    write_csv(out, merged, list(dict.fromkeys(cols)))
    by = Counter(r.get("cohort") or r.get("group") for r in merged)
    print(f"wrote {len(merged)} rows to {out}  {dict(by)}\nnext: python -m bench check --repos {out}")


def cmd_pin(a: argparse.Namespace) -> None:
    rows = read_csv(a.repos)
    changed = 0
    for row in rows:
        if row.get("commit"):
            continue
        try:
            _, sha = runner.prepare_tree(row, Path(a.workdir))
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {unit_id(row)}: {exc}")
            continue
        row["commit"] = sha
        changed += 1
        print(f"{unit_id(row):55s} {sha}")
    cols = list(dict.fromkeys([*importer.COLUMNS, *(c for r in rows for c in r)]))
    write_csv(a.out or a.repos, rows, cols)
    print(f"pinned {changed} row(s) in {a.out or a.repos}. Commit this file to git before the full run.")


def cmd_build(a: argparse.Namespace) -> None:
    runner.build_image(ROOT, a.image)


def cmd_run(a: argparse.Namespace) -> None:
    opts = runner.RunOptions(
        image=a.image, memory=a.memory, cpus=a.cpus, install_timeout=a.install_timeout,
        measure_timeout=a.measure_timeout, mutation_timeout=a.mutation_timeout,
        max_mutate_files=a.max_mutate_files, stryker_concurrency=a.stryker_concurrency,
        skip_mutation=a.skip_mutation, force=a.force)
    rows = [r for r in read_csv(a.repos) if r.get("group") in ("human", "llm")]
    dups = _duplicates(rows)
    if dups:
        raise SystemExit(f"duplicate units {dups}: give repeated repos a distinct `version`")
    if a.only:
        rows = [r for r in rows if r["repo"] in a.only or unit_id(r) in a.only]
    work = Path(a.workdir)
    ready = []
    for r in rows:  # sequential on purpose: `git worktree add` locks the shared clone
        try:
            runner.prepare_tree(r, work)
            ready.append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {unit_id(r)}: {exc}")
    print(f"running {len(ready)} unit(s) with {a.jobs} parallel job(s)")
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for msg in ex.map(lambda r: runner.run_repo(r, work / "trees", Path(a.results), opts), ready):
            print(msg, flush=True)


def _parse_weights(text: str | None) -> dict[str, float] | None:
    if not text:
        return None
    try:
        return {k.strip(): float(v) for k, v in (kv.split("=") for kv in text.split(","))}
    except ValueError:
        raise SystemExit("--weights format: compilation=1,quality=1,maintainability=1,tests=1")


def cmd_analyze(a: argparse.Namespace) -> None:
    from . import analyze  # imported lazily: pulls in pandas/scipy/matplotlib
    analyze.main(a.results, compares=[tuple(c) for c in (a.compare or [])], weights=_parse_weights(a.weights))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    # s = sub.add_parser("collect")
    # s.add_argument("--group", choices=["human", "llm"], required=True)
    # s.add_argument("--out", required=True)
    # s.add_argument("--limit", type=int, default=100)
    # s.add_argument("--keyword", action="append", help='README/description search term, e.g. \'"Claude Code"\' (repeatable)')
    # s.add_argument("--sort", choices=["stars", "updated"], default="stars")
    # s.add_argument("--human-before", default="2021-01-01")
    # s.add_argument("--llm-after", default="2023-01-01")
    # s.add_argument("--min-stars", type=int, default=20)
    # s.add_argument("--min-size-kb", type=int, default=200)
    # s.add_argument("--max-size-kb", type=int, default=30000)
    # s.add_argument("--snapshot-before", help="for the human group: pin to the last commit before this date")
    # s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("clone")
    s.add_argument("--repos", default="data/candidates.csv")
    s.add_argument("--workdir", default="work")
    s.set_defaults(fn=cmd_clone)

    s = sub.add_parser("import", help="repo links (text file, or - for stdin) -> repos.csv")
    s.add_argument("links")
    s.add_argument("--out", default="data/repos.csv")
    s.add_argument("--group", choices=["human", "llm"], help="default group for lines/sections that do not set one")
    s.add_argument("--cohort", help="default cohort, e.g. llm-2025")
    s.add_argument("--append", action="store_true", help="add to an existing --out file")
    s.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("pin", help="write the resolved commit SHA of every unpinned row back to the CSV")
    s.add_argument("--repos", default="data/repos.csv")
    s.add_argument("--workdir", default="work")
    s.add_argument("--out", help="write here instead of editing --repos in place")
    s.set_defaults(fn=cmd_pin)

    s = sub.add_parser("check", help="pre-flight report for a hand-written repos.csv")
    s.add_argument("--repos", default="data/repos.csv")
    s.add_argument("--workdir", default="work")
    s.add_argument("--human-before", default="2021-01-01")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("provenance")
    s.add_argument("--repos", default="data/candidates.csv")
    s.add_argument("--workdir", default="work")
    s.add_argument("--out", default="data/labeled.csv")
    s.add_argument("--human-before", default="2021-01-01")
    s.add_argument("--llm-after", default="2023-01-01")
    s.add_argument("--llm-share-threshold", type=float, default=0.5)
    s.add_argument("--allow-date-only-llm", action="store_true",
                   help="label post-cutoff repos with NO LLM evidence as 'llm' (the proposal's fallback rule; weak!)")
    s.set_defaults(fn=cmd_provenance)

    s = sub.add_parser("select")
    s.add_argument("--repos", default="data/labeled.csv")
    s.add_argument("--out", default="data/repos.csv")
    s.add_argument("-n", type=int, default=10, help="pairs (=> 2n repos)")
    s.add_argument("--allow-no-tests", action="store_true")
    s.add_argument("--allow-unsupported-runner", action="store_true")
    s.set_defaults(fn=cmd_select)

    s = sub.add_parser("build-image")
    s.add_argument("--image", default="ts-bench:latest")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("run")
    s.add_argument("--repos", default="data/repos.csv")
    s.add_argument("--workdir", default="work")
    s.add_argument("--results", default="results")
    s.add_argument("--image", default="ts-bench:latest")
    s.add_argument("--only", nargs="*", help="owner/name or unit id filter (unit = owner__name[@version])")
    s.add_argument("--jobs", type=int, default=1)
    s.add_argument("--memory", default="8g")
    s.add_argument("--cpus", type=float, default=4.0)
    s.add_argument("--install-timeout", type=int, default=1800)
    s.add_argument("--measure-timeout", type=int, default=4 * 3600)
    s.add_argument("--mutation-timeout", type=int, default=3600)
    s.add_argument("--max-mutate-files", type=int, default=40)
    s.add_argument("--stryker-concurrency", type=int, default=2)
    s.add_argument("--skip-mutation", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("analyze")
    s.add_argument("--results", default="results")
    s.add_argument("--compare", nargs=2, action="append", metavar=("COHORT_A", "COHORT_B"),
                   help="extra cohort comparison, repeatable, e.g. --compare llm-2023 llm-2025")
    s.add_argument("--weights", help="dimension weights for the benchmark score, "
                                     "e.g. compilation=1,quality=1,maintainability=1,tests=2")
    s.set_defaults(fn=cmd_analyze)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
