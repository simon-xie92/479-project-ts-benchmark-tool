"""Run with:  python -m unittest discover -s tests -v   (or pytest)"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import analyze, container_measure as cm, parsers, provenance, runner  # noqa: E402


class TscParser(unittest.TestCase):
    OUT = """\
src/a.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.
  Type 'x' is not assignable to type 'y'.
src/a.ts(12,1): error TS2307: Cannot find module 'lodash' or its corresponding type declarations.
src/b.tsx(3,9): error TS7006: Parameter 'x' implicitly has an 'any' type.
src/c.ts(1,1): error TS1005: ';' expected.
error TS18003: No inputs were found in config file 'tsconfig.json'.
src/d.ts(2,2): warning TS6385: 'foo' is deprecated.
Found 5 errors in 3 files.
"""

    def test_counts(self):
        r = parsers.parse_tsc_output(self.OUT)
        self.assertEqual(r["errors"], 5)
        self.assertEqual(r["warnings"], 1)
        self.assertEqual(r["files_with_errors"], 3)
        self.assertEqual(r["env_suspect_errors"], 1)  # only TS2307
        self.assertEqual(r["by_category"], {"semantic": 2, "strictness": 1, "syntax": 1, "config": 1})

    def test_clean(self):
        self.assertEqual(parsers.parse_tsc_output("")["errors"], 0)


class EslintParser(unittest.TestCase):
    DATA = [
        {"filePath": "/work/repo/src/a.ts", "messages": [
            {"ruleId": "complexity", "severity": 1, "message": "Function 'f' has a complexity of 12. Maximum allowed is 0."},
            {"ruleId": "complexity", "severity": 1, "message": "Arrow function has a complexity of 3. Maximum allowed is 0."},
            {"ruleId": "@typescript-eslint/no-explicit-any", "severity": 2, "message": "any"},
            {"ruleId": "sonarjs/no-duplicate-string", "severity": 2, "message": "dup"},
            {"ruleId": "no-unused-vars", "severity": 1, "message": "unused"},
        ]},
        {"filePath": "/work/repo/src/a.test.ts", "messages": [
            {"ruleId": "complexity", "severity": 1, "message": "Function 't' has a complexity of 40. Maximum allowed is 0."},
            {"ruleId": "@typescript-eslint/no-explicit-any", "severity": 2, "message": "any"},
        ]},
        {"filePath": "/work/repo/src/broken.ts", "messages": [
            {"ruleId": None, "severity": 2, "fatal": True, "message": "Parsing error"}]},
        {"filePath": "/work/repo/src/clean.ts", "messages": []},
    ]

    def test_aggregation(self):
        r = parsers.parse_eslint_json(self.DATA, root="/work/repo")
        self.assertEqual(r["errors"], 4)       # any, sonar, any(test), parse error
        self.assertEqual(r["warnings"], 1)
        self.assertEqual(r["total"], 5)        # complexity messages excluded
        self.assertEqual(r["fatal_parse_errors"], 1)
        self.assertEqual(r["src_total"], 4)    # test-file violation excluded
        self.assertEqual(r["files_with_issues"], 3)
        self.assertEqual(r["by_plugin"]["sonarjs"], 1)
        c = r["cyclomatic"]                    # test-file complexity (40) must NOT be counted
        self.assertEqual((c["functions"], c["max"], c["mean"]), (2, 12, 7.5))
        self.assertEqual(c["share_gt10"], 0.5)

    def test_empty_complexity(self):
        self.assertIsNone(parsers.summarize_complexity([])["mean"])


class OtherParsers(unittest.TestCase):
    def test_cloc(self):
        data = {"header": {}, "SUM": {"code": 999},
                "./src/a.ts": {"code": 100, "comment": 20, "blank": 5, "language": "TypeScript"},
                "./src/a.test.ts": {"code": 50, "comment": 0, "blank": 2, "language": "TypeScript"},
                "./src/types.d.ts": {"code": 500, "comment": 0, "blank": 0, "language": "TypeScript"},
                "./src/b.tsx": {"code": 100, "comment": 10, "blank": 1, "language": "TypeScript"},
                "./tests/x.ts": {"code": 25, "comment": 0, "blank": 0, "language": "TypeScript"},
                "./README.md": {"code": 10, "comment": 0, "blank": 0, "language": "Markdown"}}
        r = parsers.parse_cloc(data)
        self.assertEqual((r["src_code"], r["test_code"]), (200, 75))
        self.assertEqual((r["src_files"], r["test_files"]), (2, 2))
        self.assertEqual(r["comment_ratio"], 0.15)
        self.assertEqual(r["test_to_src_loc"], 0.375)

    def test_jscpd(self):
        rep = {"statistics": {"total": {"lines": 1000, "duplicatedLines": 50, "percentage": 5, "clones": 4,
                                        "tokens": 9000, "duplicatedTokens": 600, "percentageTokens": 6.67}}}
        r = parsers.parse_jscpd(rep)
        self.assertEqual((r["pct_lines"], r["clones"], r["pct_tokens"]), (5, 4, 6.67))
        self.assertIsNone(parsers.parse_jscpd({})["pct_lines"])

    def test_stryker(self):
        statuses = ["Killed"] * 6 + ["Timeout"] + ["Survived"] * 2 + ["NoCoverage"] + ["CompileError"] * 4 + ["Ignored"]
        rep = {"files": {"src/a.ts": {"mutants": [{"status": s} for s in statuses]}},
               "testFiles": {"a.test.ts": {"tests": [{}, {}, {}]}}}
        r = parsers.parse_stryker(rep)
        self.assertEqual(r["mutation_score"], 70.0)           # 7 / (7 + 3)
        self.assertEqual(r["mutation_score_covered"], 77.78)  # 7 / (7 + 2)
        self.assertEqual((r["invalid"], r["ignored"], r["n_tests"]), (4, 1, 3))
        self.assertIsNone(parsers.parse_stryker({})["mutation_score"])

    def test_runner_detection(self):
        d = parsers.detect_test_runner
        self.assertEqual(d({"scripts": {"test": "vitest run"}, "devDependencies": {"jest": "1"}}), "vitest")
        self.assertEqual(d({"scripts": {"test": "npm run build && jest --ci"}}), "jest")
        self.assertEqual(d({"devDependencies": {"ts-jest": "29"}}), "jest")
        self.assertEqual(d({"devDependencies": {"mocha": "10"}}), "mocha")
        self.assertIsNone(d({"scripts": {"test": "node --test"}}))
        self.assertIsNone(d({}))

    def test_paths(self):
        for p in ("src/a.test.ts", "src/__tests__/a.ts", "test/a.ts", "pkg/e2e/x.ts", "a.spec.tsx"):
            self.assertTrue(parsers.is_test_path(p), p)
        for p in ("src/latest/a.ts", "src/testing-utils.ts", "src/contest.ts"):
            self.assertFalse(parsers.is_test_path(p), p)
        self.assertFalse(parsers.is_ts_source_file("a.d.ts"))
        self.assertTrue(parsers.is_ts_source_file("A.TSX"))


def git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def commit(repo, msg, date):
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "d@x.io",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "d@x.io"}
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg, env=env)


def make_repo(root, name, first_date, messages, extra_files=()):
    repo = Path(root) / name
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q") if False else subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}}))
    (repo / "src" / "a.ts").write_text("export const a = 1;\n")
    (repo / "src" / "a.test.ts").write_text("test('a', () => {});\n")
    for f in extra_files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("x")
    commit(repo, messages[0], first_date)
    for i, m in enumerate(messages[1:], 1):
        (repo / "src" / "a.ts").write_text(f"export const a = {i + 1};\n")
        commit(repo, m, first_date)
    return repo


class Provenance(unittest.TestCase):
    def test_scan_and_classify_against_real_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = ("feat: add parser\n\n\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
                      "Co-Authored-By: Claude <noreply@anthropic.com>")
            llm = make_repo(tmp, "llm", "2025-03-01T12:00:00+00:00", [claude, claude, "manual fix", claude],
                            extra_files=["CLAUDE.md"])
            human = make_repo(tmp, "human", "2019-06-01T12:00:00+00:00", ["init", "fix bug", "refactor"])
            old_then_llm = make_repo(tmp, "old", "2019-06-01T12:00:00+00:00", ["init", claude])
            gap = make_repo(tmp, "gap", "2022-02-01T12:00:00+00:00", ["init"])
            modern_plain = make_repo(tmp, "plain", "2024-02-01T12:00:00+00:00", ["init", "more"])

            i = provenance.scan_repo(llm)
            self.assertEqual((i["n_commits"], i["llm_commits"], i["llm_share"]), (4, 3, 0.75))
            self.assertEqual(i["marker_files"], "CLAUDE.md")
            self.assertEqual((i["ts_files"], i["ts_test_files"], i["test_runner"]), (2, 1, "jest"))
            self.assertTrue(i["first_commit"].startswith("2025-03-01"))
            self.assertEqual(provenance.classify(i), ("llm", "commit-trailers"))

            self.assertEqual(provenance.classify(provenance.scan_repo(human)), ("human", "date"))
            self.assertEqual(provenance.classify(provenance.scan_repo(old_then_llm))[0], "excluded")
            self.assertEqual(provenance.classify(provenance.scan_repo(gap))[0], "excluded")
            plain = provenance.scan_repo(modern_plain)
            self.assertEqual(provenance.classify(plain)[0], "excluded")
            self.assertEqual(provenance.classify(plain, allow_date_only_llm=True), ("llm", "date-only"))
            self.assertEqual(provenance.classify({**i, "llm_commits": 1, "llm_share": 0.25})[0], "mixed")

    def test_select_matched(self):
        def r(name, g, n, ok=True):
            return {"repo": name, "group": g, "ts_files": n, "ts_test_files": 3 if ok else 0,
                    "has_package_json": "True", "test_runner": "jest"}
        rows = [r("h1", "human", 10), r("h2", "human", 100), r("h3", "human", 1000), r("h4", "human", 12, ok=False),
                r("l1", "llm", 11), r("l2", "llm", 900), r("l3", "llm", 5000), {**r("m", "mixed", 10)}]
        chosen = provenance.select_matched(rows, 2)
        self.assertEqual([c["repo"] for c in chosen], ["h1", "l1", "h3", "l2"])


class Analysis(unittest.TestCase):
    def test_stats_helpers(self):
        import numpy as np
        self.assertEqual(analyze.cliffs_delta(np.array([5, 6, 7]), np.array([1, 2, 3])), 1.0)
        self.assertEqual(analyze.cliffs_delta(np.array([1, 2]), np.array([1, 2])), 0.0)
        self.assertEqual(analyze.delta_label(0.5), "large")
        adj = analyze.holm([0.01, 0.04, 0.03])
        self.assertEqual([round(a, 3) for a in adj], [0.03, 0.06, 0.06])

    def test_flatten_and_compare(self):
        import pandas as pd
        rows = []
        for i in range(6):
            for g, err, cyc in (("human", 0 if i % 2 else 3, 2.0 + i * 0.1), ("llm", 8 + i, 4.0 + i * 0.1)):
                res = {"install": {"status": "ok"}, "loc": {"ts_code_total": 2000, "src_code": 1500, "test_code": 500},
                       "tsc": {"errors": err, "env_suspect_errors": 0, "clean": err == 0},
                       "eslint": {"total": 40, "cyclomatic": {"mean": cyc, "functions": 50}, "by_plugin": {"sonarjs": 3}},
                       "duplication": {"pct_lines": 2.5}, "tests": {"status": "pass"},
                       "mutation": {"mutation_score": 60.0 + i, "files_mutated": 5}}
                rows.append(analyze.flatten(res, {"slug": f"{g}{i}", "group": g}))
        df = pd.DataFrame(rows)
        self.assertEqual(df.loc[0, "tsc_errors_per_kloc"], 1.5)
        self.assertEqual(df.loc[0, "mutation_status"], "ok")
        cmp = analyze.compare_groups(df).set_index("metric")
        self.assertEqual(cmp.loc["cyc_mean", "effect"], "large")
        self.assertGreater(cmp.loc["cyc_mean", "cliffs_delta"], 0.9)
        self.assertLess(cmp.loc["cyc_mean", "p_mwu"], 0.05)
        self.assertIn("fisher", cmp.loc["tsc_clean_share", "effect"])
        with tempfile.TemporaryDirectory() as tmp:
            analyze.plot(df, Path(tmp) / "f.png")
            self.assertTrue((Path(tmp) / "f.png").exists())


class ContainerHelpers(unittest.TestCase):
    def test_run_kills_process_group_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.time()
            r = cm.run(["sh", "-c", "sleep 30 & sleep 30"], Path(tmp), timeout=1)
            self.assertTrue(r["timed_out"])
            self.assertLess(time.time() - t0, 10)

    def test_run_missing_binary(self):
        self.assertEqual(cm.run(["definitely-not-a-binary"], Path("."), 5)["returncode"], 127)

    def test_step_survives_exceptions(self):
        def boom():
            raise ValueError("x")
        self.assertEqual(cm.step(boom)["status"], "error")
        self.assertEqual(cm.step(lambda: {"a": 1})["status"], "ok")

    def test_mutation_file_selection_is_deterministic_and_skips_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            (w / "src" / "__tests__").mkdir(parents=True)
            for i in range(10):
                (w / "src" / f"m{i}.ts").write_text("x")
            (w / "src" / "m0.test.ts").write_text("x")
            (w / "src" / "types.d.ts").write_text("x")
            (w / "src" / "__tests__" / "t.ts").write_text("x")
            all_files, total = cm.select_mutation_files(w, 100)
            self.assertEqual((len(all_files), total), (10, 10))
            a, _ = cm.select_mutation_files(w, 4)
            b, _ = cm.select_mutation_files(w, 4)
            self.assertEqual(a, b)
            self.assertEqual(len(a), 4)

    def test_mutation_skip_reasons(self):
        class A:
            skip_mutation = False
            max_mutate_files = 10
            stryker_concurrency = 1
            mutation_timeout = 10
        w = Path(".")
        self.assertIn("baseline", cm.measure_mutation(w, {"status": "fail"}, {}, A)["reason"])
        self.assertIn("unsupported", cm.measure_mutation(w, {"status": "pass", "runner": "ava"}, {}, A)["reason"])

    def test_docker_cmd(self):
        cmd = runner.docker_cmd("img", Path("/r"), Path("/o"), "measure", subdir="pkg/a", name="n",
                                memory="8g", cpus=4, network=False, extra=["--skip-mutation"])
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[-4:], ["measure", "--subdir", "pkg/a", "--skip-mutation"])
        self.assertNotIn("--network", runner.docker_cmd("img", Path("/r"), Path("/o"), "install", subdir="", name="n",
                                                        memory="8g", cpus=4, network=True, extra=[]))


if __name__ == "__main__":
    unittest.main()
