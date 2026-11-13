import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import cli, importer, runner  # noqa: E402
from bench.util import read_csv  # noqa: E402
from test_pipeline import commit, make_repo  # noqa: E402

P = importer.parse_repo_url


class UrlForms(unittest.TestCase):
    def test_plain_forms_all_give_the_same_repo(self):
        for u in ("https://github.com/o/r", "https://github.com/o/r/", "https://github.com/o/r.git", "http://www.github.com/o/r",
                  "github.com/o/r", "o/r", "git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                  "https://github.com/o/r?tab=readme#install", "https://user:tok@github.com/o/r.git"):
            got = P(u)
            self.assertEqual((got["repo"], got["url"], got["commit"], got["subdir"]), ("o/r", "", "", ""), u)

    def test_refs_and_subdirs(self):
        g = P("https://github.com/o/r/tree/main/packages/core")
        self.assertEqual((g["commit"], g["subdir"]), ("main", "packages/core"))
        self.assertIn("branch", g["warn"])
        self.assertEqual(P("https://github.com/o/r/tree/v1.2.0")["commit"], "v1.2.0")
        self.assertEqual(P("https://github.com/o/r/commit/a1b2c3d4")["commit"], "a1b2c3d4")
        self.assertEqual(P("https://github.com/o/r/releases/tag/v2")["commit"], "v2")
        b = P("https://github.com/o/r/blob/abc1234/apps/web/src/index.ts")
        self.assertEqual((b["commit"], b["subdir"]), ("abc1234", "apps/web/src"))
        self.assertIn("ignored", P("https://github.com/o/r/pull/12")["warn"])

    def test_other_hosts_and_rejects(self):
        g = P("https://gitlab.com/grp/proj.git")
        self.assertEqual((g["repo"], g["url"]), ("grp/proj", "https://gitlab.com/grp/proj.git"))
        for bad in ("https://github.com/onlyowner", "notalink", "https://github.com/o/r with space", "",
                    "/tmp/x/h", "./a/b", "~/proj/x", "a/b/c"):
            self.assertIsNone(P(bad), bad)


class ListFormat(unittest.TestCase):
    TEXT = """
# my projects
[human]
https://github.com/a/old            snapshot_before=2020-12-31   # trailing comment
https://github.com/a/mono/tree/main/pkg/core notes="core only, see paper"

[llm cohort=llm-2025]
git@github.com:b/claude-app.git
https://github.com/b/evo/commit/a1b2c3d  cohort=llm-2023 version=early
https://github.com/b/evo                 version=late
https://github.com/c/odd  human          # bare token overrides the section
"""

    def test_sections_tokens_and_overrides(self):
        rows, problems = importer.parse_links(self.TEXT)
        self.assertEqual([p for p in problems if p[0] == "error"], [])
        self.assertEqual(importer.check_duplicates(rows), [])
        by = {(r["repo"], r["version"]): r for r in rows}
        self.assertEqual(len(rows), 6)
        self.assertEqual(by[("a/old", "")]["snapshot_before"], "2020-12-31")
        self.assertEqual(by[("a/mono", "")]["notes"], "core only, see paper")
        self.assertEqual(by[("a/mono", "")]["subdir"], "pkg/core")
        self.assertEqual((by[("b/claude-app", "")]["group"], by[("b/claude-app", "")]["cohort"]), ("llm", "llm-2025"))
        self.assertEqual((by[("b/evo", "early")]["cohort"], by[("b/evo", "early")]["commit"]), ("llm-2023", "a1b2c3d"))
        self.assertEqual(by[("b/evo", "late")]["cohort"], "llm-2025")
        self.assertEqual(by[("c/odd", "")]["group"], "human")
        self.assertEqual(by[("c/odd", "")]["cohort"], "llm-2025")  # inherited; user can override explicitly
        self.assertTrue(any(p[0] == "warning" and "a/mono" in p[1] for p in problems))

    def test_errors_are_reported_with_line_numbers(self):
        text = "https://github.com/a/x\nnot a link\n[llm]\nhttps://github.com/a/y bogus=1\n[llm\nhttps://github.com/a/z\n"
        rows, problems = importer.parse_links(text)
        msgs = [m for lvl, m in problems if lvl == "error"]
        self.assertTrue(any(m.startswith("line 1") and "no group" in m for m in msgs))
        self.assertTrue(any(m.startswith("line 2") and "not a repository link" in m for m in msgs))
        self.assertTrue(any(m.startswith("line 4") and "bogus=1" in m for m in msgs))
        self.assertTrue(any(m.startswith("line 5") and "unterminated" in m for m in msgs))

    def test_default_group_and_duplicates(self):
        rows, problems = importer.parse_links("https://github.com/a/x\nA/X\n", {"group": "llm"})
        self.assertEqual(problems, [])
        dup = importer.check_duplicates(rows)
        self.assertEqual(len(dup), 1)
        self.assertIn("line 2", dup[0][1])


def run_cli(*argv):
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            cli.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                buf.write(e.code)
    return code, buf.getvalue()


class ImportPinCheck(unittest.TestCase):
    def test_import_append_pin_and_branch_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            claude = "feat\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
            h = make_repo(tmp, "h", "2019-06-01T12:00:00+00:00", ["init"])
            l = make_repo(tmp, "l", "2025-01-01T12:00:00+00:00", [claude])
            first = runner._git(l, "rev-parse", "HEAD")
            (l / "src" / "a.ts").write_text("later")
            commit(l, claude, "2025-09-01T12:00:00+00:00")
            branch = runner._git(l, "rev-parse", "--abbrev-ref", "HEAD")
            links = tmp / "links.txt"
            # non-GitHub style link lets us point at local repos via the `url` column
            links.write_text(f"[human]\n{h}\n")
            out = tmp / "repos.csv"
            code, text = run_cli("import", str(links), "--out", str(out))
            self.assertEqual(code, 1)                       # a filesystem path is not a repo link
            self.assertIn("ERROR", text)
            self.assertFalse(out.exists())                  # nothing written on errors

            links.write_text("[human]\nhttps://github.com/me/h\n[llm]\nhttps://github.com/me/l/tree/%s\n" % branch)
            code, text = run_cli("import", str(links), "--out", str(out))
            self.assertEqual(code, 0, text)
            rows = read_csv(out)
            self.assertEqual([(r["repo"], r["group"], r["commit"]) for r in rows], [("me/h", "human", ""), ("me/l", "llm", branch)])
            code, text = run_cli("import", str(links), "--out", str(out))
            self.assertEqual(code, 1)                       # refuses to clobber an existing file
            code, text = run_cli("import", str(links), "--out", str(out), "--append")
            self.assertEqual(code, 1)                       # appending the same repos again => duplicates
            self.assertIn("listed twice", text)

            # point the rows at local repos, then pin
            for r in rows:
                r["url"] = str(h if r["repo"] == "me/h" else l)
            from bench.util import write_csv
            rows[1]["commit"] = branch                      # branch name must resolve through origin/<branch>
            write_csv(out, rows)
            work = tmp / "work"
            code, text = run_cli("pin", "--repos", str(out), "--workdir", str(work))
            self.assertEqual(code, 0, text)
            pinned = read_csv(out)
            self.assertTrue(all(len(r["commit"]) == 40 for r in pinned if r["repo"] == "me/h"))
            self.assertEqual(pinned[1]["commit"], branch)   # already had a commit value: left alone
            tree, sha = runner.prepare_tree(pinned[1], work)
            self.assertEqual(sha, runner._git(l, "rev-parse", "HEAD"))
            self.assertNotEqual(sha, first)
            code, text = run_cli("check", "--repos", str(out), "--workdir", str(work))
            self.assertEqual(code, 0, text)
            self.assertIn("2 units checked", text)


if __name__ == "__main__":
    unittest.main()
