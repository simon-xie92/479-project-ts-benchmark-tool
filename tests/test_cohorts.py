"""Tests for manual selection, versions/cohorts and the benchmark score."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import analyze, provenance, runner, score  # noqa: E402
from bench.util import docker_name, unit_id  # noqa: E402
from test_pipeline import commit, make_repo  # noqa: E402


class Units(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(unit_id({"repo": "o/r"}), "o__r")
        self.assertEqual(unit_id({"repo": "o/r", "version": "early"}), "o__r@early")
        self.assertEqual(unit_id({"repo": "o/r", "version": "v 1/2"}), "o__r@v-1-2")
        self.assertNotIn("@", docker_name("tsbench-o__r@early-install"))

    def test_prepare_tree_pins_each_version_in_its_own_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_repo(tmp, "src", "2019-06-01T12:00:00+00:00", ["init"])
            sha1 = runner._git(src, "rev-parse", "HEAD")
            (src / "src" / "a.ts").write_text("v2")
            commit(src, "v2", "2022-01-01T00:00:00+00:00")
            (src / "src" / "a.ts").write_text("v3")
            commit(src, "v3", "2025-01-01T00:00:00+00:00")
            sha3 = runner._git(src, "rev-parse", "HEAD")
            work = Path(tmp) / "work"
            base = {"repo": "o/r", "url": str(src)}
            early, sha_e = runner.prepare_tree({**base, "version": "early", "commit": sha1}, work)
            mid, sha_m = runner.prepare_tree({**base, "version": "mid", "snapshot_before": "2023-01-01"}, work)
            late, sha_l = runner.prepare_tree({**base, "version": "late"}, work)
            self.assertEqual((sha_e, sha_l), (sha1, sha3))
            for t in (early, mid, late):   # real .git directories (not worktree pointer files)
                self.assertTrue((t / ".git").is_dir())
            self.assertNotIn(sha_m, (sha1, sha3))
            self.assertEqual([(t / "src" / "a.ts").read_text().strip() for t in (early, mid, late)],
                             ["export const a = 1;", "v2", "v3"])
            # idempotent, and a changed pin re-checks the existing tree out
            _, again = runner.prepare_tree({**base, "version": "late"}, work)
            self.assertEqual(again, sha3)
            _, moved = runner.prepare_tree({**base, "version": "late", "commit": sha1}, work)
            self.assertEqual(moved, sha1)
            self.assertEqual((late / "src" / "a.ts").read_text().strip(), "export const a = 1;")
            # provenance at each pinned version sees only history up to that commit
            self.assertEqual(provenance.scan_repo(early)["n_commits"], 1)
            self.assertEqual(provenance.scan_repo(mid)["n_commits"], 2)


class CheckRow(unittest.TestCase):
    INFO = {"has_package_json": True, "ts_test_files": 3, "test_runner": "jest", "llm_commits": 0,
            "marker_files": "", "first_commit": "2025-01-01T00:00:00+00:00"}

    def test_warnings(self):
        w = provenance.check_row({"group": "human"}, self.INFO)
        self.assertTrue(any("first commit is on/after" in x for x in w))
        self.assertTrue(any("no LLM" in x for x in provenance.check_row({"group": "llm"}, self.INFO)))
        self.assertEqual(provenance.check_row({"group": "llm"}, {**self.INFO, "llm_commits": 2}), [])
        self.assertTrue(any("LLM evidence exists" in x for x in
                            provenance.check_row({"group": "human"}, {**self.INFO, "marker_files": "CLAUDE.md",
                                                                      "first_commit": "2019-01-01T00:00:00+00:00"})))
        self.assertTrue(any("must be" in x for x in provenance.check_row({"group": "foo"}, self.INFO)))
        bad = {**self.INFO, "has_package_json": False, "ts_test_files": 0, "test_runner": ""}
        self.assertEqual(len(provenance.check_row({"group": "llm"}, {**bad, "llm_commits": 1})), 3)


def synthetic(n_per=8, llm_worse=True, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for g in ("human", "llm"):
        bad = 1.0 if (g == "llm") == llm_worse else 0.0
        for i in range(n_per):
            rows.append({"slug": f"{g}{i}", "repo": f"{g}/{i}", "group": g, "cohort": g, "install_ok": True,
                         "tsc_errors_excl_env_per_kloc": bad * (1 + rng.random() * 3) + i * 0.01,
                         "eslint_lint_per_kloc": 5 + 10 * bad + rng.random() * 3,
                         "eslint_smells_per_kloc": 1 + 2 * bad + rng.random(),
                         "cyc_mean": 2 + bad + rng.random() * 0.5, "cyc_share_gt10": 0.02 + 0.05 * bad + rng.random() * 0.01,
                         "dup_pct_lines": 2 + 3 * bad + rng.random(), "mutation_score": 70 - 20 * bad + rng.random() * 5})
    return pd.DataFrame(rows)


class Score(unittest.TestCase):
    def test_percentile_ties_share_best_score(self):
        s = pd.Series([0, 0, 5, 10, np.nan])
        low = score.percentile_score(s, "low").tolist()
        self.assertEqual(low[:4], [100, 100, 50, 25])
        self.assertTrue(np.isnan(low[4]))
        self.assertEqual(score.percentile_score(s, "high").tolist()[:4], [50, 50, 75, 100])

    def test_scores_rank_good_above_bad_and_handle_missing(self):
        df = synthetic()
        df.loc[0, "install_ok"] = False                  # not scored at all
        df.loc[1, "mutation_score"] = np.nan             # static score only
        out = score.add_scores(df)
        self.assertTrue(np.isnan(out.loc[0, "score_static"]))
        self.assertFalse(np.isnan(out.loc[1, "score_static"]))
        self.assertTrue(np.isnan(out.loc[1, "score_full"]))
        h, l = out[out.group == "human"], out[out.group == "llm"]
        self.assertGreater(h.score_static.mean(), l.score_static.mean() + 30)
        self.assertTrue(out.score_full.dropna().between(0, 100).all())

    def test_weights(self):
        df = synthetic()
        only_tests = score.add_scores(df, {"compilation": 0, "quality": 0, "maintainability": 0, "tests": 1})
        self.assertTrue(np.allclose(only_tests.score_full, only_tests.s_tests))
        with self.assertRaises(ValueError):
            score.add_scores(df, {"nonsense": 1})
        self.assertTrue(only_tests.score_static.isna().all())   # static composite undefined, not an error
        with self.assertRaises(ValueError):
            score.add_scores(df, {"compilation": 0, "quality": 0, "maintainability": 0, "tests": 0})
        with self.assertRaises(ValueError):
            score.add_scores(df, {"tests": -1})

    def test_weight_sensitivity(self):
        out = score.add_scores(synthetic())
        sens = score.weight_sensitivity(out, "human", "llm", n=200).set_index("score")
        self.assertEqual(sens.loc["score_full", "share_human_higher"], 1.0)
        self.assertEqual(sens.loc["score_full", "share_llm_higher"], 0.0)
        self.assertLess(sens.loc["score_full", "delta_max"], 0)
        # no real difference -> the sign of the effect should flip with the weights
        mixed = synthetic()
        mixed["eslint_lint_per_kloc"] = mixed["eslint_lint_per_kloc"].iloc[::-1].to_numpy()
        mixed["mutation_score"] = mixed["mutation_score"].iloc[::-1].to_numpy()
        s2 = score.weight_sensitivity(score.add_scores(mixed), "human", "llm", n=200).set_index("score")
        self.assertLess(s2.loc["score_full", "share_human_higher"], 1.0)


def write_unit(root, uid, *, group, cohort, repo, version, commit_date, cyc, errors, mutation=None):
    d = Path(root) / uid
    d.mkdir(parents=True)
    result = {"install": {"status": "ok"},
              "loc": {"ts_code_total": 2000, "src_code": 1500, "test_code": 500, "test_to_src_loc": 0.33, "comment_ratio": 0.1},
              "tsc": {"errors": errors, "env_suspect_errors": 0, "clean": errors == 0},
              "eslint": {"total": 40, "errors": 30, "warnings": 10, "by_plugin": {"sonarjs": 8},
                         "cyclomatic": {"mean": cyc, "functions": 50, "share_gt10": 0.05, "p90": 8, "median": 2, "max": 20}},
              "duplication": {"pct_lines": 3.0}, "tests": {"status": "pass", "runner": "jest"},
              "mutation": {"mutation_score": mutation, "files_mutated": 4} if mutation is not None else {"status": "skipped"}}
    meta = {"slug": uid, "repo": repo, "group": group, "cohort": cohort, "version": version, "commit_date": commit_date,
            "provenance": {"first_commit": "2023-01-01T00:00:00+00:00", "llm_share": 0.8}}
    (d / "result.json").write_text(json.dumps(result))
    (d / "meta.json").write_text(json.dumps(meta))


class Versions(unittest.TestCase):
    def test_paired_comparison(self):
        rows = []
        for i in range(6):
            for ver, date, bump in (("early", "2024-01-01T00:00:00+00:00", 0.0), ("late", "2025-06-01T00:00:00+00:00", 1.0 + i * 0.1)):
                rows.append({"repo": f"o/r{i}", "commit_date": date, "cyc_mean": 2.0 + i * 0.1 + bump, "age_days": 10 + bump})
        df = pd.DataFrame(rows)
        pairs = analyze.version_pairs(df)
        self.assertEqual(len(pairs), 6)
        self.assertLess(pairs[0][0]["commit_date"], pairs[0][1]["commit_date"])
        v = analyze.compare_versions(df, ["cyc_mean"]).iloc[0]
        self.assertEqual((v.n_pairs, v.share_newer_higher), (6, 1.0))
        self.assertAlmostEqual(v.p_wilcoxon, 2 / 64)    # exact two-sided p for 6 same-sign pairs
        self.assertGreater(v.median_diff, 1.0)
        self.assertTrue(analyze.compare_versions(pd.DataFrame({"repo": ["a"], "commit_date": ["2024-01-01"]}), ["x"]).empty)

    def test_full_analyze_with_cohorts_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp) / "results"
            rd.mkdir()
            rng = np.random.default_rng(1)
            for i in range(5):                               # human projects
                write_unit(rd, f"h__{i}", group="human", cohort="human", repo=f"h/{i}", version="",
                           commit_date="2020-01-01T00:00:00+00:00", cyc=2 + rng.random(), errors=0, mutation=70 + i)
            for i in range(5):                               # LLM projects, two versions each
                write_unit(rd, f"l__{i}@early", group="llm", cohort="llm-2023", repo=f"l/{i}", version="early",
                           commit_date="2023-06-01T00:00:00+00:00", cyc=3 + rng.random(), errors=4 + i, mutation=50 + i)
                write_unit(rd, f"l__{i}@late", group="llm", cohort="llm-2025", repo=f"l/{i}", version="late",
                           commit_date="2025-06-01T00:00:00+00:00", cyc=4 + rng.random(), errors=1 + i % 2, mutation=60 + i)
            analyze.main(str(rd), compares=[("llm-2023", "llm-2025")], weights={"tests": 2})
            for f in ("summary.csv", "stats_human_vs_llm.csv", "scores_human_vs_llm.csv", "weight_sensitivity_human_vs_llm.csv",
                      "stats_llm-2023_vs_llm-2025.csv", "stats_versions.csv", "figures/metrics.png", "figures/scores.png",
                      "figures/versions.png"):
                self.assertTrue((rd / f).exists(), f)
            summ = pd.read_csv(rd / "summary.csv")
            self.assertEqual(len(summ), 15)
            self.assertEqual(set(summ.cohort), {"human", "llm-2023", "llm-2025"})
            self.assertTrue(summ.score_full.notna().all())
            self.assertEqual(int(pd.read_csv(rd / "stats_versions.csv").n_pairs.max()), 5)


if __name__ == "__main__":
    unittest.main()
