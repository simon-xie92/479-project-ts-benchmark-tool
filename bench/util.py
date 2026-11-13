from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Iterable

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def slug(repo: str) -> str:
    """'owner/name' -> 'owner__name' (filesystem safe)."""
    return repo.strip().replace("/", "__")


def unit_id(row: dict[str, str]) -> str:
    """Identifier of one measured unit = one repo at one pinned version.

    'owner/name' -> 'owner__name';  with version 'early' -> 'owner__name@early'.
    """
    v = (row.get("version") or "").strip()
    return f"{slug(row['repo'])}@{_UNSAFE.sub('-', v)}" if v else slug(row["repo"])


def docker_name(text: str) -> str:
    """Docker container names only allow [a-zA-Z0-9_.-]."""
    return _UNSAFE.sub("-", text)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:  # utf-8-sig: tolerate Excel's BOM
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def write_csv(path: str | Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    rows = list(rows)
    if fieldnames is None:
        fieldnames = []
        for r in rows:
            fieldnames += [k for k in r if k not in fieldnames]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
