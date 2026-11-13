"""Aggregate per-unit results, score them, and compare:

  * human vs llm              (column `group`)
  * any two cohorts           (column `cohort`, e.g. llm-2023 vs llm-2025; --compare A B)
  * older vs newer versions   (paired: same repo measured at >= 2 commits, `version` column)

With ~10 units per group treat p-values as descriptive: lead with medians and Cliff's delta, use
non-parametric tests, and correct for the number of metrics tested (Holm).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from .score import SCORE_COLUMNS, add_scores, weight_sensitivity
from .stats_util import cliffs_delta, delta_label, holm  # noqa: F401  (re-exported)


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _age_days(commit_date: str | None, first_commit: str | None) -> float | None:
    try:
        return float((pd.Timestamp(commit_date) - pd.Timestamp(first_commit)).days)
    except (TypeError, ValueError):
        return None


def flatten(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    kloc = (_g(result, "loc", "ts_code_total") or 0) / 1000
    per_kloc = (lambda x: round(x / kloc, 3) if (x is not None and kloc) else None)
    tsc_err = _g(result, "tsc", "errors")
    env = _g(result, "tsc", "env_suspect_errors")
    lint_total = _g(result, "eslint", "total")
    smells = (_g(result, "eslint", "by_plugin", "sonarjs") or 0) if lint_total is not None else None
    prov = meta.get("provenance") or {}
    row = {
        "slug": meta.get("slug"), "repo": meta.get("repo"), "group": meta.get("group"),
        "cohort": meta.get("cohort") or meta.get("group"), "version": meta.get("version") or "",
        "basis": meta.get("basis"), "commit": meta.get("commit"), "commit_date": meta.get("commit_date"),
        "age_days": _age_days(meta.get("commit_date"), prov.get("first_commit")),
        "llm_share": prov.get("llm_share"),
        "install_ok": _g(result, "install", "status") == "ok",
        # size / testability structure
        "src_loc": _g(result, "loc", "src_code"), "test_loc": _g(result, "loc", "test_code"),
        "src_files": _g(result, "loc", "src_files"), "test_files": _g(result, "loc", "test_files"),
        "test_to_src_loc": _g(result, "loc", "test_to_src_loc"), "comment_ratio": _g(result, "loc", "comment_ratio"),
        # compilation
        "tsc_status": _g(result, "tsc", "status"), "tsc_clean": _g(result, "tsc", "clean"),
        "tsc_errors": tsc_err, "tsc_errors_per_kloc": per_kloc(tsc_err),
        "tsc_env_suspect": env,
        "tsc_errors_excl_env_per_kloc": per_kloc(tsc_err - env) if tsc_err is not None and env is not None else None,
        "tsc_strict": _g(result, "tsc", "strict"),
        # lint (disjoint: sonarjs smells vs everything else)
        "eslint_total": lint_total, "eslint_errors": _g(result, "eslint", "errors"),
        "eslint_warnings": _g(result, "eslint", "warnings"),
        "eslint_per_kloc": per_kloc(lint_total),
        "eslint_sonarjs": smells,
        "eslint_smells_per_kloc": per_kloc(smells),
        "eslint_lint_per_kloc": per_kloc(lint_total - smells) if lint_total is not None else None,
        "eslint_fatal": _g(result, "eslint", "fatal_parse_errors"),
        # maintainability
        "functions": _g(result, "eslint", "cyclomatic", "functions"),
        "cyc_mean": _g(result, "eslint", "cyclomatic", "mean"), "cyc_median": _g(result, "eslint", "cyclomatic", "median"),
        "cyc_p90": _g(result, "eslint", "cyclomatic", "p90"), "cyc_max": _g(result, "eslint", "cyclomatic", "max"),
        "cyc_share_gt10": _g(result, "eslint", "cyclomatic", "share_gt10"),
        "dup_pct_lines": _g(result, "duplication", "pct_lines"), "dup_pct_tokens": _g(result, "duplication", "pct_tokens"),
        "dup_clones": _g(result, "duplication", "clones"),
        # test effectiveness
        "tests_status": _g(result, "tests", "status"), "test_runner": _g(result, "tests", "runner"),
        "mutation_status": _g(result, "mutation", "status"),
        "mutation_score": _g(result, "mutation", "mutation_score"),
        "mutation_score_covered": _g(result, "mutation", "mutation_score_covered"),
        "mutants_total": _g(result, "mutation", "mutants_total"), "mutants_killed": _g(result, "mutation", "killed"),
        "mutants_survived": _g(result, "mutation", "survived"), "mutants_no_cov": _g(result, "mutation", "no_coverage"),
        "n_tests": _g(result, "mutation", "n_tests"),
        "mutation_files": _g(result, "mutation", "files_mutated"),
    }
    if row["mutation_score"] is not None and row["mutation_status"] in (None, "ok"):
        row["mutation_status"] = "ok"  # Stryker success has no explicit status key
    return row


def load_results(results_dir: Path) -> pd.DataFrame:
    rows = []
    for d in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        rf, mf = d / "result.json", d / "meta.json"
        if not rf.exists():
            continue
        result = json.loads(rf.read_text())
        meta = json.loads(mf.read_text()) if mf.exists() else {"slug": d.name}
        rows.append(flatten(result, meta))
    return pd.DataFrame(rows)


NUMERIC_METRICS = [
    "tsc_errors_per_kloc", "tsc_errors_excl_env_per_kloc",
    "eslint_lint_per_kloc", "eslint_smells_per_kloc",
    "cyc_mean", "cyc_p90", "cyc_share_gt10", "dup_pct_lines", "comment_ratio",
    "test_to_src_loc", "src_loc", "age_days", "mutation_score", "mutation_score_covered",
]


def compare_groups(df: pd.DataFrame, a: str = "human", b: str = "llm", *, by: str = "group",
                   metrics: Sequence[str] | None = None, binary: bool = True) -> pd.DataFrame:
    """One row per metric. Effect size is Cliff's delta of (b vs a): positive => b larger."""
    rows = []
    for m in (NUMERIC_METRICS if metrics is None else metrics):
        if m not in df:
            continue
        x = df.loc[df[by] == a, m].dropna().astype(float).to_numpy()
        y = df.loc[df[by] == b, m].dropna().astype(float).to_numpy()
        row = {"metric": m, f"n_{a}": len(x), f"n_{b}": len(y),
               f"median_{a}": float(np.median(x)) if len(x) else math.nan,
               f"median_{b}": float(np.median(y)) if len(y) else math.nan,
               "cliffs_delta": math.nan, "effect": "", "p_mwu": math.nan}
        if len(x) >= 2 and len(y) >= 2:
            row["p_mwu"] = float(stats.mannwhitneyu(y, x, alternative="two-sided").pvalue)
            row["cliffs_delta"] = round(cliffs_delta(y, x), 3)
            row["effect"] = delta_label(row["cliffs_delta"])
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["p_holm"] = holm(out["p_mwu"].tolist())
    if binary and "tsc_clean" in df:  # RQ3: share of projects that compile with zero errors (Fisher exact)
        clean = df[df.tsc_clean.notna()]
        ca, cb = clean[clean[by] == a].tsc_clean.astype(bool), clean[clean[by] == b].tsc_clean.astype(bool)
        if len(ca) and len(cb):
            p = float(stats.fisher_exact([[int(ca.sum()), int((~ca).sum())], [int(cb.sum()), int((~cb).sum())]])[1])
            out = pd.concat([out, pd.DataFrame([{
                "metric": "tsc_clean_share", f"n_{a}": len(ca), f"n_{b}": len(cb),
                f"median_{a}": ca.mean(), f"median_{b}": cb.mean(), "p_mwu": math.nan, "p_holm": math.nan,
                "cliffs_delta": math.nan, "effect": f"fisher p={p:.3f}"}])], ignore_index=True)
    return out


# --------------------------------------------------------------------------- #
# older vs newer versions of the same project (paired)
# --------------------------------------------------------------------------- #

def version_pairs(df: pd.DataFrame) -> list[tuple[pd.Series, pd.Series]]:
    """(oldest, newest) measured unit per repo that has >= 2 units, ordered by commit date."""
    if "commit_date" not in df or df.empty:
        return []
    d = df.copy()
    d["_t"] = pd.to_datetime(d["commit_date"], utc=True, errors="coerce")
    d = d.dropna(subset=["_t"])
    pairs = []
    for _, g in d.groupby("repo"):
        if len(g) >= 2:
            g = g.sort_values("_t")
            pairs.append((g.iloc[0], g.iloc[-1]))
    return pairs


def compare_versions(df: pd.DataFrame, metrics: Sequence[str]) -> pd.DataFrame:
    """Paired comparison newer-minus-older per project (Wilcoxon signed-rank)."""
    pairs = version_pairs(df)
    if not pairs:
        return pd.DataFrame()
    rows = []
    for m in metrics:
        if m not in df:
            continue
        old = np.array([float(o[m]) if pd.notna(o[m]) else np.nan for o, _ in pairs])
        new = np.array([float(n[m]) if pd.notna(n[m]) else np.nan for _, n in pairs])
        ok = ~np.isnan(old) & ~np.isnan(new)
        diff = new[ok] - old[ok]
        row = {"metric": m, "n_pairs": int(ok.sum()),
               "median_old": float(np.median(old[ok])) if ok.any() else math.nan,
               "median_new": float(np.median(new[ok])) if ok.any() else math.nan,
               "median_diff": float(np.median(diff)) if ok.any() else math.nan,
               "share_newer_higher": float((diff > 0).mean()) if ok.any() else math.nan,
               "p_wilcoxon": math.nan}
        if len(diff) >= 2 and np.any(diff != 0):
            try:
                row["p_wilcoxon"] = float(stats.wilcoxon(diff).pvalue)
            except ValueError:
                pass
        rows.append(row)
    out = pd.DataFrame(rows)
    out["p_holm"] = holm(out["p_wilcoxon"].tolist())
    return out


# --------------------------------------------------------------------------- #
# figures
# --------------------------------------------------------------------------- #

PLOT_METRICS = [
    ("tsc_errors_per_kloc", "tsc errors / KLOC"), ("eslint_per_kloc", "ESLint violations / KLOC"),
    ("cyc_mean", "mean cyclomatic complexity"), ("cyc_p90", "p90 cyclomatic complexity"),
    ("dup_pct_lines", "duplicated lines (%)"), ("comment_ratio", "comment / code ratio"),
    ("test_to_src_loc", "test LOC / source LOC"), ("mutation_score", "mutation score (%)"),
]
SCORE_PLOT = [("score_static", "score (static)"), ("score_full", "score (full, incl. mutation)"),
              ("s_compilation", "compilation"), ("s_quality", "quality"),
              ("s_maintainability", "maintainability"), ("s_tests", "test effectiveness")]


def _order(cohorts: Sequence[str]) -> list[str]:
    return sorted(cohorts, key=lambda c: (c != "human", c))


def _boxgrid(df: pd.DataFrame, cols: list[tuple[str, str]], by: str, groups: Sequence[str], shape: tuple[int, int], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(*shape, figsize=(3.8 * shape[1], 3.5 * shape[0]))
    rng = np.random.default_rng(0)
    for ax, (col, title) in zip(np.ravel(axes), cols):
        if col in df:
            data = [df.loc[df[by] == g, col].dropna().astype(float).to_numpy() for g in groups]
            if any(len(d) for d in data):
                ax.boxplot([d if len(d) else [np.nan] for d in data], showfliers=False)
                ax.set_xticks(range(1, len(groups) + 1))
                ax.set_xticklabels(groups, rotation=20 if len(groups) > 3 else 0, fontsize=8)
                for i, d in enumerate(data, start=1):
                    ax.scatter(i + rng.uniform(-0.08, 0.08, len(d)), d, alpha=0.7, s=18, zorder=3)
        ax.set_title(title, fontsize=10)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot(df: pd.DataFrame, path: Path, by: str = "group", groups: Sequence[str] = ("human", "llm")) -> None:
    _boxgrid(df, PLOT_METRICS, by, groups, (2, 4), path)


def plot_scores(df: pd.DataFrame, path: Path, by: str = "cohort") -> None:
    _boxgrid(df, SCORE_PLOT, by, _order(df[by].dropna().unique()), (2, 3), path)


def plot_versions(df: pd.DataFrame, path: Path, col: str = "score_static") -> bool:
    """Slope chart: each line is one project, oldest -> newest measured version."""
    pairs = [(o, n) for o, n in version_pairs(df) if pd.notna(o[col]) and pd.notna(n[col])]
    if not pairs:
        return False
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 4.5))
    for o, n in pairs:
        ax.plot([0, 1], [o[col], n[col]], marker="o", alpha=0.8, label=str(o["repo"]))
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["older version", "newer version"])
    ax.set_ylabel(col)
    ax.set_title(f"{col}: same project, two versions", fontsize=10)
    if len(pairs) <= 10:
        ax.legend(fontsize=6, loc="best")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #

def _show(title: str, frame: pd.DataFrame, cols: Sequence[str] | None = None) -> None:
    if frame.empty:
        return
    shown = frame[[c for c in (cols or frame.columns) if c in frame]]
    with pd.option_context("display.width", 220, "display.max_columns", 30, "display.float_format", "{:.3g}".format):
        print(f"\n{title}\n{shown.to_string(index=False)}")


def main(results_dir: str = "results", compares: Sequence[tuple[str, str]] | None = None,
         weights: Mapping[str, float] | None = None) -> None:
    rd = Path(results_dir)
    df = load_results(rd)
    if df.empty:
        raise SystemExit(f"no result.json files under {rd}/")
    df = add_scores(df, weights)
    df.to_csv(rd / "summary.csv", index=False)

    print(f"\n{len(df)} units | groups: {df.group.value_counts().to_dict()} | cohorts: {df.cohort.value_counts().to_dict()}")
    cov = df.groupby("cohort").agg(units=("slug", "count"), installed=("install_ok", "sum"), tsc=("tsc_errors", "count"),
                                   eslint=("eslint_total", "count"), dup=("dup_pct_lines", "count"),
                                   mutation=("mutation_score", "count"), scored=("score_static", "count"),
                                   scored_full=("score_full", "count"))
    print("\npipeline coverage per cohort (units with a value):\n" + cov.to_string())

    comparisons: list[tuple[str, str, str]] = []
    if {"human", "llm"} <= set(df.group.dropna()):
        comparisons.append(("group", "human", "llm"))
    comparisons += [("cohort", a, b) for a, b in (compares or [])]
    seen: set[str] = set()
    for by, a, b in comparisons:
        tag = f"{a}_vs_{b}"
        if tag in seen:
            continue
        seen.add(tag)
        sub = df[df[by].isin([a, b])]
        raw = compare_groups(sub, a, b, by=by)
        sc = compare_groups(sub, a, b, by=by, metrics=SCORE_COLUMNS, binary=False)
        sens = weight_sensitivity(df, a, b, by=by)
        raw.to_csv(rd / f"stats_{tag}.csv", index=False)
        sc.to_csv(rd / f"scores_{tag}.csv", index=False)
        sens.to_csv(rd / f"weight_sensitivity_{tag}.csv", index=False)
        keep = ["metric", f"n_{a}", f"n_{b}", f"median_{a}", f"median_{b}", "cliffs_delta", "effect", "p_holm"]
        _show(f"=== {a} vs {b}: raw metrics (delta>0 means {b} is higher) ===", raw, keep)
        _show(f"=== {a} vs {b}: benchmark scores (higher = better) ===", sc, keep)
        _show(f"=== {a} vs {b}: does the score comparison depend on the weights? ===", sens)

    versions = compare_versions(df, NUMERIC_METRICS + SCORE_COLUMNS)
    if not versions.empty:
        versions.to_csv(rd / "stats_versions.csv", index=False)
        n = int(versions["n_pairs"].max())
        _show(f"=== older vs newer version of the same project ({n} projects, paired) ===", versions,
              ["metric", "n_pairs", "median_old", "median_new", "median_diff", "share_newer_higher", "p_wilcoxon", "p_holm"])
        if plot_versions(df, rd / "figures" / "versions.png"):
            print(f"wrote {rd / 'figures' / 'versions.png'}")

    plot(df, rd / "figures" / "metrics.png")
    plot_scores(df, rd / "figures" / "scores.png")
    print(f"\nwrote {rd / 'summary.csv'} + stats_*.csv / scores_*.csv / weight_sensitivity_*.csv + figures/")
