#!/usr/bin/env python3
"""Deterministic tunnel-domain relevance gate used before acquisition and handoff."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "relevance_policy.yaml"

NAVIGATION_SLUGS = {"login", "log-in", "sign-in", "signin", "register", "signup", "account", "search", "newsletter", "subscribe", "subscription", "sitemap", "site-map", "author-login"}
LEGAL_SLUGS = {"privacy", "privacy-policy", "cookie-policy", "terms", "terms-of-use", "copyright", "disclaimer", "credits", "contact", "about"}


def _policy() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    return value if isinstance(value, dict) else {}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _host(record: dict[str, Any]) -> str:
    for key in ("source_url", "landing_url", "pdf_url"):
        host = (urlparse(str(record.get(key) or "")).hostname or "").casefold()
        if host:
            return host
    return ""


def _matches(terms: list[str], text: str) -> list[str]:
    return [term for term in terms if _norm(term) in text]


def evaluate(record: dict[str, Any], *, text_override: str | None = None) -> dict[str, Any]:
    """Return a reproducible relevance decision without any model call."""
    policy = _policy()
    title = _norm(record.get("title"))
    abstract = _norm(text_override if text_override is not None else record.get("abstract") or record.get("text_excerpt"))
    metadata = _norm(" ".join(str(record.get(k) or "") for k in ("publisher", "venue", "source", "source_url", "landing_url", "pdf_url", "keywords")))
    full = " ".join((title, abstract, metadata))
    anchors = _matches(list(policy.get("tunnel_anchors") or []), full)
    title_anchors = _matches(list(policy.get("tunnel_anchors") or []), title)
    method_anchors = _matches(list(policy.get("method_anchors") or []), full)
    title_method_anchors = _matches(list(policy.get("method_anchors") or []), title)
    domain = _matches(list(policy.get("engineering_signals") or []), full)
    title_domain = _matches(list(policy.get("engineering_signals") or []), title)
    negatives = _matches(list(policy.get("negative_patterns") or []), full)
    trusted = any(_host(record) == source or _host(record).endswith("." + source) for source in (policy.get("trusted_tunnel_sources") or []))

    thresholds = policy.get("thresholds") or {}
    if negatives:
        score, status = 0.0, "IRRELEVANT"
    elif not anchors and domain:
        # Adjacent engineering material is borderline until the local embedding
        # stage can compare it with the TunnelBookAI taxonomy.
        score, status = float(thresholds.get("weak", 0.45)), "WEAK"
    elif not anchors:
        score, status = 0.0, "IRRELEVANT"
    else:
        score = 0.52
        if domain:
            score += 0.20
        if method_anchors:
            score += 0.20
        if title_anchors:
            score += 0.14
        if title_domain:
            score += 0.08
        if abstract and anchors:
            score += 0.04
        if trusted and (title_anchors or domain):
            score += 0.06
        score = min(0.99, score)
        if not domain and not method_anchors and not (trusted and title_anchors):
            score = min(score, float(thresholds.get("weak", 0.45)) + 0.10)
        if score >= float(thresholds.get("strong", 0.80)):
            status = "STRONG"
        elif score >= float(thresholds.get("probable", 0.65)):
            status = "PROBABLE"
        elif score >= float(thresholds.get("weak", 0.45)):
            status = "WEAK"
        else:
            status = "IRRELEVANT"
    return {
        "tunnel_relevance_score": round(score, 4),
        "relevance_status": status,
        "relevance_signals": list(dict.fromkeys([*anchors, *method_anchors, *domain])),
        "negative_signals": list(dict.fromkeys(negatives)),
        "relevance_method": "deterministic_v1",
    }


def acquisition_allowed(decision: dict[str, Any], record: dict[str, Any]) -> bool:
    status = decision.get("relevance_status")
    if status == "STRONG":
        return True
    if status != "PROBABLE":
        return False
    # PROBABLE sources may be validated cheaply; low-value weak sources never download.
    return bool(record.get("doi") or record.get("pdf_url") or record.get("source_class") in {"TR_OFFICIAL", "INT_OFFICIAL"})


def noncontent_decision(record: dict[str, Any]) -> dict[str, Any]:
    """Reject obvious navigation/legal endpoints without substring false positives."""
    url = str(record.get("source_url") or record.get("landing_url") or "")
    parsed = urlparse(url)
    slug = (parsed.path.rstrip("/").split("/")[-1] or "").casefold()
    slug = re.sub(r"\.(?:html?|php|aspx?)$", "", slug)
    title = _norm(record.get("title"))
    content_type = _norm(record.get("content_type"))
    looks_document = bool(record.get("pdf_url")) or "application/pdf" in content_type or slug.endswith(".pdf")
    if looks_document:
        return {"noncontent_status": "CONTENT_CANDIDATE", "noncontent_reason": None}
    exact_title = re.sub(r"[^a-z0-9ğüşöçıİ\- ]+", "", title).strip().replace(" ", "-")
    if slug in NAVIGATION_SLUGS or (exact_title in NAVIGATION_SLUGS and len(title.split()) <= 3):
        return {"noncontent_status": "REJECT_NONCONTENT_PAGE", "noncontent_reason": "navigation_page"}
    if slug in LEGAL_SLUGS or (exact_title in LEGAL_SLUGS and len(title.split()) <= 4):
        return {"noncontent_status": "REJECT_NONCONTENT_PAGE", "noncontent_reason": "legal_page"}
    return {"noncontent_status": "CONTENT_CANDIDATE", "noncontent_reason": None}
