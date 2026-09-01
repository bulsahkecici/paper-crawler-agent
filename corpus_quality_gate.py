#!/usr/bin/env python3
"""DEPRECATED compatibility shim.

PaperCrawler evaluates only its *handoff package*, never a canonical corpus.
The name ``corpus_quality_gate`` is reserved for TunnelBookAI's ingest layer.
Use :mod:`handoff_quality_gate` instead.

This module is kept so existing imports/scripts keep working. It performs a
single evaluation via :func:`handoff_quality_gate.evaluate_handoff`; the
authoritative artifact is ``audit/handoff_quality_gate.json`` and a deprecated
alias ``audit/corpus_quality_gate.json`` is also written.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from handoff_quality_gate import evaluate_handoff

__all__ = ["evaluate", "evaluate_handoff"]


def evaluate(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    warnings.warn(
        "corpus_quality_gate.evaluate is deprecated; use handoff_quality_gate.evaluate_handoff. "
        "'corpus_quality_gate' now belongs to TunnelBookAI.",
        DeprecationWarning,
        stacklevel=2,
    )
    return evaluate_handoff(output_dir, package_root=package_root)
