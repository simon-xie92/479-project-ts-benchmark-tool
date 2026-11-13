# ts-bench — Human-written vs LLM-generated TypeScript projects

An automated pipeline that measures hand-picked GitHub TypeScript projects (at pinned commits) with the same
toolchain inside Docker, scores them, and compares human vs LLM projects, LLM cohorts, and older vs newer versions.

```
your repos.csv ─▶ check ─▶ run (Docker: install → measure) ─▶ analyze
 (group, cohort,   pre-flight  tsc · ESLint · jscpd · cloc ·    benchmark score, medians, Cliff's δ,
  version, commit)  report     tests · StrykerJS                 Mann-Whitney / Wilcoxon, Holm, figures
        optional discovery helpers: collect ─▶ clone ─▶ provenance ─▶ select
```

## Quick start (you supply the projects)

```bash
pip install -r requirements.txt
(cd tools && npm install)                # once: creates tools/package-lock.json -> commit it

# you may need to open docker desktop for this step.
python -m bench build-image              # builds ts-bench:latest (node 20 + pinned tools)

# 1. your links -> repos.csv   (format below; see data/links.example.txt)
python -m bench import links.txt         # or:  --group llm --cohort llm-2025  for a one-group file
python -m bench check                    # fetches each project, prints a pre-flight report (advisory)
python -m bench pin                      # freezes every row to the exact commit; commit repos.csv to git

# 2. measure
python -m bench run --only OWNER/NAME --skip-mutation     # try ONE project first
python -m bench run                      # everything; resumable, finished units are skipped (--force to redo)

# 3. analyse
python -m bench analyze --compare llm-2023 llm-2025       # extra cohort comparison(s) are optional
```

### `links.txt`

One link per line; sections set defaults for the lines below; anything after the URL is optional.

```
[human]
https://github.com/OWNER/old-app                       snapshot_before=2020-12-31
https://github.com/OWNER/mono/tree/main/packages/core  notes="package core only"

[llm cohort=llm-2025]
https://github.com/OWNER/claude-app
git@github.com:OWNER/cursor-app.git
https://github.com/OWNER/evolving/commit/a1b2c3d  cohort=llm-2023 version=early   # same project twice
https://github.com/OWNER/evolving                 cohort=llm-2025 version=late
https://github.com/OWNER/odd-one  human          # a bare human/llm token overrides the section
```

* Accepted links: `https://github.com/o/r[.git]`, `github.com/o/r`, `o/r`, `git@github.com:o/r.git`, and the
  `/tree/<ref>[/<subdir>]`, `/blob/<ref>/<path>`, `/commit/<sha>`, `/releases/tag/<tag>` forms (the ref becomes the
  pinned `commit`, the folder the `subdir`). Other hosts (GitLab, ...) keep their URL.
* Each line needs a group, from a `[human]`/`[llm]` header, a bare token, or `--group`. Keys after the URL are the
  `repos.csv` columns below (`cohort=`, `version=`, `commit=`, `snapshot_before=`, `subdir=`, `notes=`).
* `import` is all-or-nothing: on any error it prints every problem with its line number and writes nothing. It refuses to
  overwrite an existing file unless you pass `--append` (add to it) or `--force`. The same repo twice without distinct
  `version`s is an error.
* `/tree/<branch>/<folder>` is read as branch = first segment; if your branch name contains `/`, fix the row by hand
  (`check` will warn that no package.json was found).
* `pin` records the commit each unpinned row resolves to *today*. Branch links move, so pin before the first full run.

### `data/repos.csv`

| column | required | meaning |
|---|---|---|
| `repo` | yes | `owner/name` on GitHub |
| `group` | yes | `human` or `llm` (your decision; the tool never changes it) |
| `cohort` | no | finer label for comparisons, e.g. `llm-2023`, `llm-2025`. Defaults to `group` |
| `version` | no | label that lets the **same repo appear several times** (e.g. `early`, `late`) |
| `commit` | no | exact SHA / tag to measure. Overrides `snapshot_before` |
| `snapshot_before` | no | measure the last commit before this date (e.g. a pre-LLM snapshot of an old project) |
| `subdir` | no | folder with the package to measure, for monorepos |
| `url` | no | clone URL for non-GitHub repos |
| `notes` | no | free text. Write down *why* you believe a project is human/LLM-written |

Each row is a **unit** = one repo at one pinned commit, kept in `work/trees/<owner>__<name>[@version]` as its
own clone. `check` is advisory: it prints each unit's commit, date, #TS files, #test files, detected test runner and the
share of commits carrying LLM trailers, and warns about things that will bite later (no tests, unsupported runner,
a project labelled `llm` with no LLM evidence in its history, labelled `human` but with LLM trailers, ...).

### Optional: let the tool find candidates (instead of your own links)

`collect` (GitHub search) → `clone` → `provenance` (labels from git-history evidence) → `select -n 10` (size-matched
pairs) produces a `data/repos.csv` for you. You need `GITHUB_TOKEN` only for `collect` (higher rate limit).

## Comparing older vs newer versions of LLM projects

"Older vs newer" can mean two different studies; the pipeline supports both and they can be combined.

**A. The same project at two points in time** (does LLM-generated code degrade or improve as the project grows?)
Add the repo twice with different `version`s and pin them with `commit` (or leave `late` blank for the latest).
`analyze` pairs them by repo and runs a **paired** comparison (newer minus older, Wilcoxon signed-rank) →
`stats_versions.csv`, `figures/versions.png`. Needs ≥ 6 projects with two versions for any chance of p < 0.05
(with 5 pairs the smallest possible exact p is 0.0625), so aim for 8–10.

**B. Older-generation vs newer-generation LLM projects** (e.g. ChatGPT-3.5-era vs agentic-Claude-era)
Give projects a `cohort` (`llm-2023`, `llm-2025`) and run `analyze --compare llm-2023 llm-2025`
(independent groups, Mann-Whitney) → `stats_llm-2023_vs_llm-2025.csv`. Decide cohort boundaries by evidence (commit
date range, README statements, trailers naming a tool), write the reasoning in `notes`, and fix the rule *before*
looking at results.

Confounds to discuss: later versions are bigger (the per-KLOC metrics help; `src_loc` and `age_days` are in the
comparison tables so you can see imbalance), later versions of an "LLM project" contain more human edits
(`llm_share` = share of commits with LLM trailers is recorded per unit in `summary.csv`), and "model generation"
is confounded with tooling (chat copy-paste vs agentic IDEs).

## Benchmark score (one number per project)

Yes, but treat it as a *summary device*, not a finding. `bench/score.py`:

1. Each metric → **percentile score** vs. the pool of all measured units: "% of the pool that is at least as bad as
   this project" (ties share the best score, so everyone with 0 compile errors gets 100). Used instead of raw
   min-max because the metrics have different scales and heavy tails.
2. Metrics → four **dimension scores** (0–100, higher is better):
   `compilation` (tsc errors/KLOC without "cannot find module"-style environment errors),
   `quality` (ESLint lint + SonarJS smells per KLOC),
   `maintainability` (mean cyclomatic complexity, share of functions with CC > 10, duplicated-lines %),
   `tests` (mutation score). Comment ratio is deliberately not scored (more comments ≠ better code).
3. Two composites: `score_static` (first three dimensions; every installed project) and `score_full` (all four;
   only where StrykerJS worked). No imputation: a missing dimension means no composite, because Stryker failures
   are not random. Compare projects only within the same composite.
4. Weights default to 1/1/1/1 (`analyze --weights compilation=1,quality=1,maintainability=1,tests=2`). They are
   arbitrary, so `analyze` also draws 1000 random weightings and reports (`weight_sensitivity_*.csv`) the
   share of weightings under which each group scores higher. **If that share is ~1.0 the conclusion does not
   depend on the weights; if it is ~0.5 it does, and you should say so.**

Caveats to state in the report: scores are relative to the pool (adding or removing projects changes them, so freeze
the pool before reporting); projects whose dependencies failed to install are not scored; a composite hides trade-offs
(a project can score 70 by being average everywhere or by being perfect on one dimension and poor on another), so always
show the four dimension scores (`figures/scores.png`, `scores_*.csv`) next to it; choose the weights *before* looking
at results.

## What is measured (your proposal table → implementation)

| Dimension | Tool | Fields in `result.json` / `summary.csv` |
|---|---|---|
| Compilation | `tsc --noEmit` (project's own tsc + tsconfig; bundled TS 5.6 if absent) | `tsc.errors`, `by_category` (syntax/semantic/strictness/config), `env_suspect_errors`, `clean`, `strict` |
| Code quality | ESLint 9 + typescript-eslint + eslint-plugin-sonarjs, one uniform config | `eslint.errors/warnings`, `by_plugin`, `top_rules`, `fatal_parse_errors`; `eslint_lint_per_kloc` / `eslint_smells_per_kloc` in summary |
| Maintainability | ESLint `complexity` rule (cyclomatic, per function) · jscpd (duplication) · cloc (comment ratio) | `eslint.cyclomatic.{mean,median,p90,max,share_gt10}`, `duplication.pct_lines/pct_tokens/clones`, `loc.comment_ratio` |
| Structural testability | cloc test vs source split | `loc.test_to_src_loc`, `loc.test_files` |
| Test effectiveness | StrykerJS (jest / vitest / mocha / jasmine runners) | `mutation.mutation_score`, `mutation_score_covered`, `killed/survived/no_coverage/timeout`, `n_tests` |

Mutation score follows Stryker's definition: `(killed + timeout) / (killed + timeout + survived + no-coverage)`;
compile/runtime-error mutants are excluded as invalid.

## Decisions you should know about (and may want to overrule)

1. **SonarQube is replaced by jscpd + ESLint + cloc.** SonarQube needs a running server, tokens and
   a per-project scan, and its Community edition has no CLI-only mode — painful to automate and to
   reproduce for 20 repos. jscpd gives duplication %, ESLint's `complexity` rule gives per-function
   cyclomatic complexity, `eslint-plugin-sonarjs` brings SonarQube's own smell rules, cloc gives the
   comment ratio. If your supervisor insists on SonarQube, run `sonarqube:community` in Docker, scan with
   `sonar-scanner`, and read `ncloc, complexity, cognitive_complexity, duplicated_lines_density,
   comment_lines_density, code_smells` from `/api/measures/component` — it slots in as one more step in
   `container_measure.py`.
2. **"LLM-generated" should rest on evidence, not just dates** (applies to `provenance`/`check`). A 2024 repo is usually still human-written.
   `provenance` labels `llm` only if ≥50 % of commits carry an LLM trailer (`Co-Authored-By: Claude`,
   "Generated with Claude Code", Copilot/aider/Cursor markers). Repos with weaker evidence (e.g. just a
   `CLAUDE.md`) become `mixed`. Your proposal's date-only fallback exists as `--allow-date-only-llm`;
   those repos get `basis=date-only` so you can drop them in a sensitivity analysis.
3. **Pre-2021 repos can be contaminated by later AI edits.** They are excluded unless you pin them with
   `snapshot_before` (e.g. `collect --snapshot-before 2021-01-01`), which checks out the last commit before
   that date. Trade-off: old snapshots have rotting dependencies, so more installs fail.
4. **Two-phase containers.** `install` has network; `measure` runs with `--network none` so tests behave
   deterministically and untrusted repo code can't phone home. Resource limits: 8 GB, 4 CPUs, pids cap.
5. **Stryker is sampled and time-boxed.** Mutating a whole repo takes hours. By default a deterministic
   sample of ≤40 production files is mutated (`--max-mutate-files`) with a 1 h cap (`--mutation-timeout`).
   `files_selected/files_available` are recorded. The TypeScript checker is only enabled if the project
   compiles cleanly, otherwise type-error mutants would be misclassified.
6. **ESLint ignores each repo's own config and inline `eslint-disable`s**, so counts are comparable.
   Type-aware rules are off (they need a working program per repo).

## Threats to validity (put these in your report)

* **n ≈ 10 per group**: use medians + Cliff's δ; p-values are descriptive. Holm correction is applied.
* **Environment noise in `tsc`**: a failed install produces hundreds of "cannot find module" errors.
  `tsc_env_suspect` / `tsc_errors_excl_env_per_kloc` separate these; also filter on `install_ok`.
* **tsc version**: using each project's own TypeScript measures "compiles as its authors intended"; a fixed
  version would be more comparable but fails old projects. `tsc.version` / `tsc.source` are recorded.
* **Survivorship**: repos that don't install, have failing tests or an unsupported runner yield no mutation
  score. Expect a substantial share of repos to drop out of the Stryker analysis (I can't tell you how many
  until you run it); `analyze` prints per-group coverage. Better to over-collect candidates (100+ per group) and *pre-filter* on "installs and
  tests pass" than to discover this on the final 20.
* **Confounders**: project size, domain, popularity and author experience differ between eras; size is
  matched, the rest is not. Say so.
* **Provenance is probabilistic**: trailers prove LLM involvement, their absence doesn't prove its absence.
* `cognitive complexity`, type-aware lint rules and the repos' own `build` scripts are not measured.

## Layout

```
bench/parsers.py            pure parsing of tool output (all unit-tested)
bench/container_measure.py  runs inside Docker: install + tsc/ESLint/jscpd/cloc/tests/Stryker
bench/runner.py             per-unit clones + docker orchestration     bench/provenance.py  evidence, check, matching
bench/importer.py           link list -> repos.csv
bench/score.py              benchmark score + weight sensitivity       bench/analyze.py     stats, versions, plots
bench/stats_util.py         Cliff's delta, Holm                        bench/collect.py     GitHub search (optional)
docker/Dockerfile · tools/{package.json,eslint.config.mjs}             tests/
```

Outputs in `results/`: `summary.csv` (one row per unit, incl. scores), `stats_<a>_vs_<b>.csv`, `scores_<a>_vs_<b>.csv`,
`weight_sensitivity_<a>_vs_<b>.csv`, `stats_versions.csv`, `figures/{metrics,scores,versions}.png`. Raw artefacts per unit
live in `results/<unit>/` (`tsc_output.txt`, `eslint_raw.json`, `jscpd/`, `mutation.json`, `stryker.conf.json`,
`result.json`, `meta.json` with commit SHA/date and provenance evidence).

Run the tests: `python -m unittest discover -s tests -v`
