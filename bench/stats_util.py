"""Small statistics helpers shared by analyze.py and score.py."""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float:
    """P(x>y) - P(x<y).  Positive => x tends to be larger than y.  Range [-1, 1]."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    return float(np.sign(x[:, None] - y[None, :]).mean())


def delta_label(d: float) -> str:
    a = abs(d)
    return "negligible" if a < 0.147 else "small" if a < 0.33 else "medium" if a < 0.474 else "large"


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (NaNs are skipped and stay NaN)."""
    idx = sorted((p, i) for i, p in enumerate(pvals) if not math.isnan(p))
    m, adj, running = len(idx), [math.nan] * len(pvals), 0.0
    for rank, (p, i) in enumerate(idx):
        running = max(running, (m - rank) * p)
        adj[i] = min(1.0, running)
    return adj
