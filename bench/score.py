"""Benchmark score: one 0-100 number per project, built so that it is hard to misuse.

Design
------
1. Every metric is turned into a *percentile score* against the benchmark pool:
   "what % of the pool is at least as bad as this project" (ties share the best score, so every
   project that compiles with 0 errors gets 100 on that metric, not a mid-rank).  Percentiles are
   used because the raw metrics have wildly different scales and heavy tails (errors/KLOC vs
   mutation %), and they are robust to outliers.
2. Metrics are averaged into four dimension scores:
       compilation      tsc errors/KLOC (environment errors such as "cannot find module" removed)
       quality          ESLint lint violations/KLOC  +  SonarJS smells/KLOC
       maintainability  mean cyclomatic complexity, share of functions with CC>10, duplicated lines %
       tests            StrykerJS mutation score
   Comment ratio is deliberately NOT scored: more comments are not better code.
3. Two composites, because the mutation score is missing for many repos and must not be imputed:
       score_static = weighted mean of compilation + quality + maintainability   (available for all)
       score_full   = weighted mean of all four                                  (only where Stryker ran)
4. Weights default to equal. They are arbitrary, so `weight_sensitivity` re-scores under
   thousands of random weightings and reports whether the human-vs-LLM conclusion survives.

Caveats: scores are *relative to the pool* (adding projects changes them) and are only meaningful
for ranking/comparing projects inside one study. Projects whose dependencies failed to install are
not scored. Report the dimension scores alongside the composite, never the composite alone.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd
from scipy import stats

from .stats_util import cliffs_delta

# dimension -> [(summary.csv column, "low" if lower is better else "high")]
DIMENSIONS: dict[str, list[tuple[str, str]]] = {
    "compilation": [("tsc_errors_excl_env_per_kloc", "low")],
    "quality": [("eslint_lint_per_kloc", "low"), ("eslint_smells_per_kloc", "low")],
    "maintainability": [("cyc_mean", "low"), ("cyc_share_gt10", "low"), ("dup_pct_lines", "low")],
    "tests": [("mutation_score", "high")],
}
STATIC = ("compilation", "quality", "maintainability")
ALL_DIMS = tuple(DIMENSIONS)
DEFAULT_WEIGHTS = {d: 1.0 for d in DIMENSIONS}
SCORE_COLUMNS = ["score_static", "score_full", "s_compilation", "s_quality", "s_maintainability", "s_tests"]


def percentile_score(values: pd.Series, direction: str) -> pd.Series:
    """% of the (non-missing) pool that is at least as bad as each value. Range (0, 100]."""
    v = values.astype(float)
    arr = v.dropna().to_numpy()
    if arr.size == 0:
        return v
    if direction == "low":
        return v.map(lambda x: np.nan if np.isnan(x) else 100.0 * np.sum(arr >= x) / arr.size)
    return v.map(lambda x: np.nan if np.isnan(x) else 100.0 * np.sum(arr <= x) / arr.size)


def _weighted(df: pd.DataFrame, dims: tuple[str, ...], w: Mapping[str, float]) -> pd.Series:
    vals = df[[f"s_{d}" for d in dims]].to_numpy(float)
    wt = np.array([w[d] for d in dims], float)
    out = np.full(len(df), np.nan)
    if wt.sum() == 0:  # e.g. all weight on `tests`: this composite is simply undefined
        return pd.Series(out, index=df.index)
    ok = ~np.isnan(vals).any(axis=1)  # no imputation: a missing dimension => no composite
    out[ok] = (vals[ok] * wt).sum(axis=1) / wt.sum()
    return pd.Series(out, index=df.index)


def add_scores(df: pd.DataFrame, weights: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Return a copy of `df` with s_<dimension>, score_static and score_full columns."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    unknown = set(w) - set(DIMENSIONS)
    if unknown:
        raise ValueError(f"unknown dimension(s) {sorted(unknown)}; valid: {list(DIMENSIONS)}")
    if any(v < 0 for v in w.values()) or sum(w.values()) == 0:
        raise ValueError(f"weights must be non-negative and not all zero: {w}")
    out = df.copy()
    eligible = out["install_ok"].fillna(False).astype(bool) if "install_ok" in out else pd.Series(True, index=out.index)
    for dim, metrics in DIMENSIONS.items():
        cols = [percentile_score(out[c].where(eligible), d) for c, d in metrics if c in out]
        out[f"s_{dim}"] = pd.concat(cols, axis=1).mean(axis=1, skipna=True) if cols else np.nan
    out["score_static"] = _weighted(out, STATIC, w)
    out["score_full"] = _weighted(out, ALL_DIMS, w)
    return out


def weight_sensitivity(df: pd.DataFrame, a: str, b: str, by: str = "group", n: int = 1000, seed: int = 0) -> pd.DataFrame:
    """Does 'b scores higher than a' (or the reverse) depend on the weights?

    Draws `n` weight vectors uniformly from the simplex (Dirichlet(1,..,1)), recomputes the composite from
    the dimension scores, and records Cliff's delta (b vs a) each time.  `df` must already have s_* columns.
    `rank_corr` is the mean Spearman correlation between the random-weight ranking and the equal-weight one.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for name, dims in (("score_static", STATIC), ("score_full", ALL_DIMS)):
        sub = df[df[by].isin([a, b])]
        S = sub[[f"s_{d}" for d in dims]].to_numpy(float)
        ok = ~np.isnan(S).any(axis=1)
        S, lab = S[ok], sub[by].to_numpy()[ok]
        row = {"score": name, f"n_{a}": int((lab == a).sum()), f"n_{b}": int((lab == b).sum())}
        if row[f"n_{a}"] < 2 or row[f"n_{b}"] < 2:
            rows.append(row)
            continue
        equal = S.mean(axis=1)
        deltas, rhos = [], []
        for _ in range(n):
            c = S @ rng.dirichlet(np.ones(len(dims)))
            deltas.append(cliffs_delta(c[lab == b], c[lab == a]))
            rhos.append(stats.spearmanr(c, equal)[0])
        d = np.array(deltas)
        row.update({
            "delta_equal_weights": round(cliffs_delta(equal[lab == b], equal[lab == a]), 3),
            "delta_min": round(d.min(), 3), "delta_median": round(float(np.median(d)), 3), "delta_max": round(d.max(), 3),
            f"share_{b}_higher": round(float((d > 0).mean()), 3), f"share_{a}_higher": round(float((d < 0).mean()), 3),
            "rank_corr": round(float(np.nanmean(rhos)), 3),
        })
        rows.append(row)
    return pd.DataFrame(rows)
