"""Turn a plain list of repository links into repos.csv rows.

Input file format (everything after the URL is optional):

    # comments and blank lines are ignored
    [human]                                   <- section header: defaults for the lines below
    https://github.com/owner/old-app
    github.com/owner/other-app snapshot_before=2020-12-31

    [llm cohort=llm-2025]                     <- header may carry any key=value defaults
    https://github.com/owner/claude-app
    https://github.com/owner/evolving/commit/a1b2c3d  version=early cohort=llm-2023
    https://github.com/owner/evolving                 version=late
    https://github.com/owner/mono/tree/main/packages/core  notes="package core only"
    git@github.com:owner/app.git llm          <- a bare `human` / `llm` token sets the group on one line

Recognised URL forms: https://github.com/o/r[.git], github.com/o/r, o/r, git@github.com:o/r.git,
.../tree/<ref>[/<subdir>], .../blob/<ref>/<path>, .../commit/<sha>, .../releases/tag/<tag>.
Links from other hosts keep their host in the `url` column.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

from .util import unit_id

COLUMNS = ["repo", "group", "cohort", "version", "commit", "snapshot_before", "subdir", "url", "notes"]
_KEYS = set(COLUMNS) - {"repo"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
Problem = tuple[str, str]  # (level 'error'|'warning', message)


def parse_repo_url(text: str) -> dict[str, str] | None:
    """Extract repo / pinned ref / subdir from a link. Returns None if it is not a repo link."""
    t = re.split(r"[?#]", text.strip(), maxsplit=1)[0].rstrip("/")
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", t)
    if m:
        host, path = m.group(1).lower(), m.group(2)
    else:
        t = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", t)
        t = re.sub(r"^[^/@]+@", "", t)  # strip user[:pass]@
        if "/" not in t:
            return None
        first, _, tail = t.partition("/")
        if "." in first or ":" in first:
            host, path = first.lower(), tail
        elif t.startswith(("/", ".", "~")) or t.count("/") != 1:
            return None  # filesystem path or junk; the bare shorthand is exactly `owner/name`
        else:
            host, path = "github.com", t
    if not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$", host):
        return None  # not a real hostname (e.g. './a/b')
    if host == "www.github.com":
        host = "github.com"
    seg = [s for s in path.split("/") if s]
    if len(seg) < 2:
        return None
    owner, name, rest = seg[0], re.sub(r"\.git$", "", seg[1]), seg[2:]
    if not (_NAME.match(owner) and _NAME.match(name)):
        return None

    out = {"repo": f"{owner}/{name}", "url": "" if host == "github.com" else f"https://{host}/{owner}/{name}.git",
           "commit": "", "subdir": "", "warn": ""}
    if rest[:1] == ["tree"] and len(rest) > 1:
        out["commit"], out["subdir"] = rest[1], "/".join(rest[2:])
        if len(rest) > 2:
            out["warn"] = f"read '{rest[1]}' as the branch/tag and '{out['subdir']}' as subdir (wrong if the branch name contains '/')"
    elif rest[:1] == ["blob"] and len(rest) > 2:
        out["commit"], out["subdir"] = rest[1], "/".join(rest[2:-1])
        out["warn"] = "blob link: used its folder as subdir"
    elif rest[:1] == ["commit"] and len(rest) > 1:
        out["commit"] = rest[1]
    elif rest[:2] == ["releases", "tag"] and len(rest) > 2:
        out["commit"] = rest[2]
    elif rest:
        out["warn"] = f"ignored extra path '/{'/'.join(rest)}'"
    return out


def _tokens(text: str) -> tuple[dict[str, str], list[str]]:
    """'llm cohort=x notes="a b"' -> ({'group': 'llm', 'cohort': 'x', 'notes': 'a b'}, [bad tokens])."""
    vals: dict[str, str] = {}
    bad: list[str] = []
    for tok in shlex.split(text):
        if tok in ("human", "llm"):
            vals["group"] = tok
        elif "=" in tok and tok.split("=", 1)[0] in _KEYS:
            k, v = tok.split("=", 1)
            vals[k] = v
        else:
            bad.append(tok)
    return vals, bad


def parse_links(text: str, defaults: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[Problem]]:
    """Parse a link list. Rows carry a private `_line` key (stripped before writing)."""
    rows: list[dict[str, Any]] = []
    problems: list[Problem] = []
    base = {k: v for k, v in (defaults or {}).items() if v}
    section = dict(base)
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        try:
            if line.startswith("["):
                if not line.endswith("]"):
                    problems.append(("error", f"line {n}: unterminated section header {line!r}"))
                    continue
                vals, bad = _tokens(line[1:-1])
                section = {**base, **vals}
                problems += [("error", f"line {n}: unrecognised header token {b!r}") for b in bad]
                continue
            parts = shlex.split(line)
        except ValueError as exc:
            problems.append(("error", f"line {n}: {exc}"))
            continue

        parsed = parse_repo_url(parts[0])
        if parsed is None:
            problems.append(("error", f"line {n}: not a repository link: {parts[0]!r}"))
            continue
        vals, bad = _tokens(" ".join(shlex.quote(p) for p in parts[1:]))
        problems += [("error", f"line {n}: unrecognised token {b!r} (use human|llm or key=value; "
                               f"keys: {', '.join(sorted(_KEYS))})") for b in bad]
        if parsed["warn"]:
            problems.append(("warning", f"line {n}: {parsed['repo']}: {parsed['warn']}"))

        row: dict[str, Any] = {c: "" for c in COLUMNS}
        row.update({"repo": parsed["repo"], "url": parsed["url"], "commit": parsed["commit"], "subdir": parsed["subdir"]})
        row.update({k: v for k, v in section.items() if v})
        row.update(vals)
        row["_line"] = n
        if row["group"] not in ("human", "llm"):
            problems.append(("error", f"line {n}: {row['repo']}: no group. Add a [human]/[llm] header, a bare "
                                      f"`human`/`llm` token, or --group"))
        rows.append(row)
    return rows, problems


def check_duplicates(rows: list[dict[str, Any]]) -> list[Problem]:
    seen: dict[str, Any] = {}
    out: list[Problem] = []
    for r in rows:
        key = unit_id(r).lower()
        if key in seen:
            out.append(("error", f"line {r.get('_line', '?')}: {r['repo']} is listed twice without a distinct version "
                                 f"(first at line {seen[key].get('_line', 'existing file')}); add version=early / version=late"))
        else:
            seen[key] = r
    return out
