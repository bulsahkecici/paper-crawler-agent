#!/usr/bin/env python3
"""Free source discovery for PaperCrawler.

No paid search API is used. Discovery combines:
- existing free scholarly sources (OpenAlex, Crossref, Europe PMC, DOAJ, arXiv)
- OpenAIRE Graph API and optional/best-effort CORE API
- curated institutional seed pages and internal site search
- sitemap.xml / robots.txt respecting same-domain crawling
- OAI-PMH for repositories/journals (including DergiPark journal endpoints)
- Common Crawl URL index for bounded domain expansion only
- dynamic promotion of newly discovered official/university/repository domains

Acquisition is deliberately separate from final TunnelBookAI ingest. Public PDF
or HTML sources are snapshotted under ``discovery_sources/`` and registered in
``discovery_catalog.jsonl``. TunnelBookAI still performs full-text conversion,
quality/evidence audit and canonical corpus ingest later.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import random
import re
import signal
import socket
import threading
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

import classification_engine as classifier
import hybrid_classifier as hybrid
import relevance_engine as relevance
import source_health
import tunnel_harvest as harvest

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
USER_AGENT = harvest.USER_AGENT
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5"}
_ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
SOURCE_DEADLINE_SECONDS = 30


def _log(message: str) -> None:
    print(f"[discovery] {message}", flush=True)


class SourceDeadlineExceeded(TimeoutError):
    """Raised when one discovery provider exceeds the whole-call deadline."""


def _run_with_source_deadline(callback, *args, seconds: int = SOURCE_DEADLINE_SECONDS, **kwargs):
    """Run a provider call with a hard wall-clock limit on Unix main threads.

    ``requests``' read timeout is reset whenever a server sends a little data,
    so it cannot bound a slow/stalled streaming response.  This guard stops the
    complete provider call after 30 seconds, allowing the next provider to run.
    """
    if (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        return callback(*args, **kwargs)

    def _expired(_signum, _frame):
        raise SourceDeadlineExceeded(f"source exceeded {seconds}s deadline")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, max(1, seconds))
    try:
        return callback(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@dataclass
class DiscoveryRecord:
    title: str
    source: str
    discovery_source: str
    discovery_query: str = ""
    source_url: str | None = None
    landing_url: str | None = None
    pdf_url: str | None = None
    publisher: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    doi: str | None = None
    document_type: str | None = None
    source_class: str | None = None
    metadata_only: bool = False
    access_kind: str = "discovered"
    extra: dict[str, Any] = field(default_factory=dict)
    tunnel_relevance_score: float = 0.0
    relevance_status: str = "UNASSESSED"
    relevance_signals: list[str] = field(default_factory=list)
    negative_signals: list[str] = field(default_factory=list)
    relevance_method: str = "deterministic_v1"

    def key(self) -> str:
        doi = harvest.normalize_doi(self.doi)
        if doi:
            return "doi:" + doi.lower()
        for value in (self.pdf_url, self.source_url, self.landing_url):
            if value:
                return "url:" + normalize_url(value)
        title = re.sub(r"\W+", " ", (self.title or "").lower()).strip()
        return "title:" + title

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["doi"] = harvest.normalize_doi(self.doi)
        payload["discovery_key"] = self.key()
        return payload


@dataclass(frozen=True)
class URLSafetyResult:
    safe: bool
    reason: str


class DNSResolutionError(ConnectionError):
    pass


class SecurityBlockedError(ValueError):
    pass


def _config() -> dict[str, Any]:
    with (CONFIG_DIR / "discovery_sources.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("discovery_sources.yaml must contain a mapping")
    return payload

def institutional_sources() -> dict[str, Any]:
    """Merge legacy adapter details with the global identity registry."""
    sources = dict(_config().get("institutional_sources") or {})
    registry = yaml.safe_load((CONFIG_DIR / "institutional_sources.yaml").read_text(encoding="utf-8")) or {}
    tier_scores = {"A1":100,"A2":95,"A3":90,"A4":88,"D1":75,"C1":70}
    for item in registry.get("institutions") or []:
        key = str(item.get("id") or "").strip()
        if not key: continue
        current = dict(sources.get(key) or {})
        sources[key] = {**item, **current, "publisher":item.get("name"), "authority_score":tier_scores.get(str(item.get("authority_tier")),60), "sitemap_probe":"sitemap" in (item.get("discovery_methods") or []), "common_crawl_expand":False}
    return sources


def _topic_queries() -> dict[str, Any]:
    with (CONFIG_DIR / "topic_queries.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload if isinstance(payload, dict) else {}


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    # Strip common tracking parameters but preserve functional site/search params.
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(query, doseq=True), ""))


def _host(url: str | None) -> str:
    try:
        return (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def url_safety(url: str) -> URLSafetyResult:
    """Classify URL safety without conflating DNS failures with SSRF blocks."""
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return URLSafetyResult(False, "INVALID_HOST")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return URLSafetyResult(False, "INVALID_SCHEME")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        return URLSafetyResult(False, "LOOPBACK")
    try:
        direct = ipaddress.ip_address(host)
        if direct.is_loopback:
            return URLSafetyResult(False, "LOOPBACK")
        if direct.is_link_local:
            return URLSafetyResult(False, "LINK_LOCAL")
        if direct.is_private:
            return URLSafetyResult(False, "PRIVATE_IP")
        if direct.is_multicast or direct.is_reserved or direct.is_unspecified:
            return URLSafetyResult(False, "RESERVED")
        return URLSafetyResult(True, "PUBLIC")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return URLSafetyResult(False, "DNS_RESOLUTION_FAILED")
    if not infos:
        return URLSafetyResult(False, "DNS_RESOLUTION_FAILED")
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return URLSafetyResult(False, "INVALID_HOST")
        if addr.is_loopback:
            return URLSafetyResult(False, "LOOPBACK")
        if addr.is_link_local:
            return URLSafetyResult(False, "LINK_LOCAL")
        if addr.is_private:
            return URLSafetyResult(False, "PRIVATE_IP")
        if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
            return URLSafetyResult(False, "RESERVED")
    return URLSafetyResult(True, "PUBLIC")


def is_public_web_url(url: str) -> bool:
    return url_safety(url).safe


def _blocked_domain(url: str) -> bool:
    host = _host(url)
    for domain in _config().get("blocked_acquisition_domains") or []:
        domain = str(domain).lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _robots_allowed(url: str, session: requests.Session) -> bool:
    policy = _config().get("policy") or {}
    if not policy.get("respect_robots_txt", True):
        return True
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    cache_key = f"{parsed.scheme}://{parsed.netloc}".lower()
    ttl = float(policy.get("robots_cache_ttl_seconds") or 3600)
    cached = _ROBOTS_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < ttl:
        parser = cached[1]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)
    safety = url_safety(robots_url)
    if safety.reason == "DNS_RESOLUTION_FAILED":
        return True
    if not safety.safe:
        return False
    try:
        response = session.get(robots_url, headers=HEADERS, timeout=8, allow_redirects=False)
        if response.status_code >= 400:
            _ROBOTS_CACHE[cache_key] = (time.monotonic(), None)
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        _ROBOTS_CACHE[cache_key] = (time.monotonic(), parser)
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        # Fail open for unavailable robots.txt, not for explicit disallow rules.
        _ROBOTS_CACHE[cache_key] = (time.monotonic(), None)
        return True


def safe_get(
    url: str,
    *,
    session: requests.Session | None = None,
    stream: bool = False,
    timeout: float | None = None,
    respect_robots: bool = True,
) -> requests.Response:
    """GET with public-network validation on every redirect."""
    cfg = _config().get("policy") or {}
    max_redirects = int(cfg.get("max_redirects") or 5)
    timeout = float(timeout or cfg.get("request_timeout_seconds") or 25)
    sess = session or requests.Session()
    current = normalize_url(url)
    safety = url_safety(current)
    if safety.reason == "DNS_RESOLUTION_FAILED":
        raise DNSResolutionError(f"DNS resolution failed: {_host(current)}")
    if not safety.safe:
        raise SecurityBlockedError(f"Security blocked URL ({safety.reason}): {url}")
    if _blocked_domain(current):
        raise ValueError(f"Acquisition blocked by policy: {_host(current)}")
    if respect_robots and not _robots_allowed(current, sess):
        raise PermissionError(f"robots.txt disallows: {current}")

    for _ in range(max_redirects + 1):
        response = sess.get(current, headers=HEADERS, timeout=timeout, stream=stream, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            response.url = current
            return response
        location = response.headers.get("Location")
        if not location:
            response.raise_for_status()
        nxt = normalize_url(urllib.parse.urljoin(current, location))
        safety = url_safety(nxt)
        if safety.reason == "DNS_RESOLUTION_FAILED":
            response.close()
            raise DNSResolutionError(f"DNS resolution failed: {_host(nxt)}")
        if not safety.safe:
            response.close()
            raise SecurityBlockedError(f"Redirect security blocked ({safety.reason}): {nxt}")
        if _blocked_domain(nxt):
            response.close()
            raise ValueError(f"Redirect to blocked acquisition domain: {_host(nxt)}")
        current = nxt
    raise requests.TooManyRedirects(f"Too many redirects for {url}")


class _HTMLCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._ignore_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignore_depth += 1
            return
        if self._ignore_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._anchor_href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._ignore_depth:
            self._ignore_depth -= 1
            return
        if self._ignore_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._anchor_href:
            label = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
            self.links.append((self._anchor_href, label))
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._ignore_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)
        if self._anchor_href is not None:
            self._anchor_text.append(text)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()


def parse_html(text: str) -> _HTMLCollector:
    parser = _HTMLCollector()
    try:
        parser.feed(text)
    except Exception:  # HTMLParser is best-effort on malformed web pages.
        pass
    return parser


def html_to_markdown(title: str, text: str, source_url: str) -> str:
    body = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    return "\n".join([
        "---",
        f"source_url: {source_url}",
        "snapshot_kind: web_text",
        "evidence_eligible_provisional: true",
        "---",
        "",
        f"# {title or 'Web source'}",
        "",
        body,
        "",
    ])


def topic_queries(max_queries: int | None = None, request_path: str | Path | None = None) -> list[str]:
    """Build multilingual, book-agnostic tunnel-engineering queries."""
    queries: list[str] = []
    seen: set[str] = set()
    for q in [*harvest.SEARCH_QUERIES, *harvest.FILL_QUERIES]:
        key = q.lower().strip()
        if key not in seen:
            seen.add(key)
            queries.append(q)
    for terms in (_topic_queries().get("queries") or {}).values():
        for term in terms or []:
            q = qualify_tunnel_query(str(term))
            if len(q) >= 4 and q.casefold() not in seen:
                seen.add(q.casefold()); queries.append(q)
    if request_path:
        payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
        if not str(payload.get("schema_version") or "").startswith("1.") or not payload.get("request_id"):
            raise ValueError("invalid TunnelBookAI discovery request")
        for term in [*(payload.get("queries") or []), *(payload.get("topics") or [])]:
            q = qualify_tunnel_query(str(term).replace("_", " "))
            if q.casefold() not in seen: seen.add(q.casefold()); queries.append(q)
    return queries[:max_queries] if max_queries else queries

def taxonomy_queries(max_queries: int | None = None) -> list[str]:
    """Deprecated compatibility alias; no taxonomy is loaded."""
    return topic_queries(max_queries=max_queries)


def qualify_tunnel_query(query: str) -> str:
    """Ensure broad taxonomy terms never become unqualified web/API queries."""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    lowered = text.casefold()
    policy = relevance._policy()
    anchors = [str(x).casefold() for x in (policy.get("tunnel_anchors") or [])]
    if any(anchor in lowered for anchor in anchors):
        return text
    generic = [str(x).casefold() for x in (policy.get("generic_terms_requiring_tunnel_context") or [])]
    if any(term == lowered or term in lowered for term in generic):
        return f"tunnel {text}"
    return f"tunnel {text}"


def _with_relevance(record: DiscoveryRecord) -> DiscoveryRecord:
    decision = relevance.evaluate(record.as_dict())
    return replace(record, **decision)


def filter_relevant_records(
    records: Iterable[DiscoveryRecord], *, embedding_client: hybrid.LocalOpenAIClient | None = None,
    embedding_model: str | None = None, profile_vectors: dict[str, list[float]] | None = None,
) -> tuple[list[DiscoveryRecord], int]:
    kept: list[DiscoveryRecord] = []
    rejected = 0
    for record in records:
        noncontent = relevance.noncontent_decision(record.as_dict())
        if noncontent["noncontent_status"] == "REJECT_NONCONTENT_PAGE":
            rejected += 1
            continue
        evaluated = _with_relevance(record)
        if evaluated.relevance_status == "WEAK" and embedding_client and embedding_model:
            try:
                scores = hybrid.embedding_scores(evaluated.as_dict(), embedding_client, embedding_model, profile_vectors=profile_vectors)
                best = float((scores or [{}])[0].get("score") or 0.0)
                if best >= 0.62:
                    evaluated = replace(
                        evaluated, relevance_status="PROBABLE",
                        tunnel_relevance_score=max(float(evaluated.tunnel_relevance_score), best),
                        relevance_method="deterministic_plus_local_embedding",
                    )
            except (requests.RequestException, ValueError, OSError):
                pass
        if evaluated.relevance_status in {"STRONG", "PROBABLE", "WEAK"}:
            kept.append(evaluated)
        else:
            rejected += 1
    return kept, rejected


def relevance_terms() -> tuple[str, ...]:
    cfg_terms = [str(x).lower() for x in (_config().get("relevance_url_terms") or [])]
    return tuple(dict.fromkeys(cfg_terms))


def relevant_text(value: str, terms: Iterable[str] | None = None) -> bool:
    blob = html.unescape(str(value or "")).lower()
    candidates = tuple(terms or relevance_terms())
    return any(term.lower() in blob for term in candidates)


def _paper_record(paper: harvest.Paper, query: str) -> DiscoveryRecord:
    return DiscoveryRecord(
        title=paper.title,
        source=paper.source,
        discovery_source=paper.source,
        discovery_query=query,
        source_url=paper.landing_url or paper.pdf_url,
        landing_url=paper.landing_url,
        pdf_url=paper.pdf_url,
        publisher=paper.venue,
        abstract=paper.abstract,
        authors=list(paper.authors),
        year=paper.year,
        doi=paper.doi,
        document_type="PREPRINT" if paper.source == "arxiv" else "JOURNAL_ARTICLE",
        source_class="ACADEMIC",
        access_kind="academic_api",
    )


def _provider_failure_bucket(exc: Exception) -> str:
    """Keep provider health diagnostics actionable without exposing stack traces."""
    if isinstance(exc, DNSResolutionError) or isinstance(exc, socket.gaierror):
        return "dns_failures"
    if isinstance(exc, (requests.ConnectTimeout, requests.ReadTimeout, TimeoutError)):
        return "timeout_failures"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"http_{status}_failures"
    return "other_failures"


def _provider_call(name: str, fn: Any, query: str, limit: int) -> Any:
    """Bounded OpenAlex 429 retry; the outer circuit breaker handles final failure."""
    attempts = 3 if name == "openalex" else 1
    for attempt in range(attempts):
        try:
            return _run_with_source_deadline(fn, query, limit)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status != 429 or attempt + 1 >= attempts:
                raise
            retry_header = getattr(exc.response, "headers", {}).get("Retry-After")
            try:
                delay = float(retry_header)
            except (TypeError, ValueError):
                delay = float(2 ** attempt)
            delay = min(8.0, delay + random.uniform(0, 0.25))
            _log(f"source=openalex rate_limited retry={attempt + 2}/{attempts} delay={delay:.2f}s")
            time.sleep(delay)


def discover_existing_academic(
    query: str, per_source: int = 12, disabled_sources: set[str] | None = None,
    provider_health: dict[str, Counter[str]] | None = None,
    health_registry: source_health.SourceHealthRegistry | None = None,
) -> tuple[list[DiscoveryRecord], list[str]]:
    cfg = _config().get("academic_sources") or {}
    functions = {
        "openalex": harvest.search_openalex,
        "crossref": harvest.search_crossref,
        "europe_pmc": harvest.search_europe_pmc,
        "doaj": harvest.search_doaj,
        "arxiv": harvest.search_arxiv,
    }
    records: list[DiscoveryRecord] = []
    errors: list[str] = []
    disabled_sources = disabled_sources or set()
    for name, fn in functions.items():
        if name in disabled_sources or (health_registry is not None and not health_registry.available(name)):
            _log(f"source={name} skipped for this run after repeated failures")
            continue
        if not (cfg.get(name) or {}).get("enabled", False):
            continue
        try:
            if provider_health is not None:
                provider_health[name]["requests"] += 1
            _log(f"source={name} query={query!r} started")
            batch = _provider_call(name, fn, query, per_source)
            if health_registry is not None:
                health_registry.success(name)
            if provider_health is not None:
                provider_health[name]["successful_requests"] += 1
                provider_health[name]["records_seen"] += len(batch)
            _log(f"source={name} query={query!r} records={len(batch)}")
        except Exception as exc:  # noqa: BLE001
            if health_registry is not None:
                retry_after = None
                if getattr(getattr(exc, "response", None), "status_code", None) == 429:
                    try:
                        retry_after = float(exc.response.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                health_registry.failure(name, _provider_failure_bucket(exc), retry_after=retry_after)
            if provider_health is not None:
                provider_health[name][_provider_failure_bucket(exc)] += 1
            errors.append(f"{name}: {exc}")
            _log(f"source={name} query={query!r} skipped: {exc}")
            continue
        records.extend(_paper_record(p, query) for p in batch)
    return records, errors


def _nested_first(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _urls_recursive(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        urls.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            urls.extend(_urls_recursive(child))
    elif isinstance(value, list):
        for child in value:
            urls.extend(_urls_recursive(child))
    return urls


def _choose_urls(item: dict[str, Any]) -> tuple[str | None, str | None]:
    urls = list(dict.fromkeys(_urls_recursive(item)))
    pdf = next((u for u in urls if re.search(r"\.pdf(?:$|[?#])", u, re.I)), None)
    landing = next((u for u in urls if "doi.org/" in u.lower()), None)
    if not landing:
        landing = next((u for u in urls if u != pdf), None)
    return pdf, landing


def search_openaire(query: str, limit: int = 20) -> list[DiscoveryRecord]:
    cfg = (_config().get("academic_sources") or {}).get("openaire") or {}
    if not cfg.get("enabled", False):
        return []
    endpoint = str(cfg.get("endpoint") or "https://api.openaire.eu/graph/v3/research-products")
    response = safe_get(
        endpoint + "?" + urllib.parse.urlencode({"search": query, "type": "publication", "pageSize": min(limit, 100)}),
        respect_robots=False,
    )
    payload = response.json()
    rows = payload.get("results") or []
    records: list[DiscoveryRecord] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        title = _nested_first(item, ("mainTitle", "title", "name")) or "untitled"
        if isinstance(title, dict):
            title = _nested_first(title, ("value", "name", "title")) or "untitled"
        authors_raw = item.get("authors") or item.get("creators") or []
        authors: list[str] = []
        for author in authors_raw if isinstance(authors_raw, list) else []:
            if isinstance(author, dict):
                name = _nested_first(author, ("fullName", "name", "displayName"))
                if name:
                    authors.append(str(name))
            elif author:
                authors.append(str(author))
        pids = item.get("pids") or item.get("identifiers") or []
        doi = None
        for pid in pids if isinstance(pids, list) else []:
            if isinstance(pid, dict):
                scheme = str(pid.get("scheme") or pid.get("type") or "").lower()
                value = pid.get("value") or pid.get("id")
                if "doi" in scheme and value:
                    doi = harvest.normalize_doi(str(value))
                    break
        pdf, landing = _choose_urls(item)
        date = str(_nested_first(item, ("publicationDate", "dateOfPublication", "date")) or "")
        year = date[:4] if len(date) >= 4 and date[:4].isdigit() else None
        abstract = _nested_first(item, ("description", "abstract", "summary")) or ""
        if isinstance(abstract, list):
            abstract = " ".join(str(x) for x in abstract)
        publisher = _nested_first(item, ("publisher", "journal", "source")) or ""
        if isinstance(publisher, dict):
            publisher = _nested_first(publisher, ("name", "title", "value")) or ""
        records.append(DiscoveryRecord(
            title=str(title), source="openaire", discovery_source="openaire", discovery_query=query,
            source_url=landing or pdf, landing_url=landing, pdf_url=pdf, publisher=str(publisher),
            abstract=str(abstract), authors=authors, year=year, doi=doi,
            document_type="JOURNAL_ARTICLE", source_class="ACADEMIC", access_kind="academic_api",
        ))
    return records


def search_core(query: str, limit: int = 20) -> list[DiscoveryRecord]:
    cfg = (_config().get("academic_sources") or {}).get("core") or {}
    if not cfg.get("enabled", False):
        return []
    endpoint = str(cfg.get("endpoint") or "https://api.core.ac.uk/v3/search/works")
    key = os.getenv(str(cfg.get("api_key_env") or "CORE_API_KEY"), "").strip()
    headers = dict(HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if not is_public_web_url(endpoint):
        return []
    response = requests.get(endpoint, params={"q": query, "limit": min(limit, 100)}, headers=headers, timeout=25)
    if response.status_code in {401, 403, 429}:
        return []
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") or payload.get("data") or []
    records: list[DiscoveryRecord] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "untitled")
        authors: list[str] = []
        for author in item.get("authors") or []:
            if isinstance(author, dict) and author.get("name"):
                authors.append(str(author["name"]))
            elif isinstance(author, str):
                authors.append(author)
        pdf = item.get("downloadUrl") or item.get("fullTextLink")
        if not pdf:
            full_urls = item.get("sourceFulltextUrls") or []
            pdf = full_urls[0] if isinstance(full_urls, list) and full_urls else None
        landing = item.get("doi") or item.get("oai")
        doi = harvest.normalize_doi(item.get("doi"))
        if doi:
            landing = "https://doi.org/" + doi
        records.append(DiscoveryRecord(
            title=title, source="core", discovery_source="core", discovery_query=query,
            source_url=landing or pdf, landing_url=landing, pdf_url=pdf,
            publisher=str(item.get("publisher") or item.get("journals") or ""),
            abstract=str(item.get("abstract") or ""), authors=authors,
            year=str(item.get("yearPublished") or item.get("year") or "") or None, doi=doi,
            document_type="JOURNAL_ARTICLE", source_class="ACADEMIC", access_kind="academic_api",
        ))
    return records


def discover_academic(
    query: str, per_source: int = 12, disabled_sources: set[str] | None = None,
    provider_health: dict[str, Counter[str]] | None = None,
    health_registry: source_health.SourceHealthRegistry | None = None,
) -> tuple[list[DiscoveryRecord], list[str]]:
    disabled_sources = disabled_sources or set()
    records, errors = discover_existing_academic(
        query, per_source=per_source, disabled_sources=disabled_sources, provider_health=provider_health,
        health_registry=health_registry,
    )
    for name, fn in (("openaire", search_openaire), ("core", search_core)):
        if name in disabled_sources or (health_registry is not None and not health_registry.available(name)):
            _log(f"source={name} skipped for this run after repeated failures")
            continue
        try:
            if provider_health is not None:
                provider_health[name]["requests"] += 1
            _log(f"source={name} query={query!r} started")
            batch = _run_with_source_deadline(fn, query, per_source)
            if health_registry is not None:
                health_registry.success(name)
            records.extend(batch)
            if provider_health is not None:
                provider_health[name]["successful_requests"] += 1
                provider_health[name]["records_seen"] += len(batch)
            _log(f"source={name} query={query!r} completed")
        except Exception as exc:  # noqa: BLE001
            if health_registry is not None:
                health_registry.failure(name, _provider_failure_bucket(exc))
            if provider_health is not None:
                provider_health[name][_provider_failure_bucket(exc)] += 1
            errors.append(f"{name}: {exc}")
            _log(f"source={name} query={query!r} skipped: {exc}")
    cfg = (_config().get("academic_sources") or {}).get("semantic_scholar") or {}
    if cfg.get("enabled", False):
        try:
            if health_registry is not None and not health_registry.available("semantic_scholar"):
                return records, errors
            if provider_health is not None:
                provider_health["semantic_scholar"]["requests"] += 1
            batch = harvest.search_semantic_scholar(query, per_source)
            if health_registry is not None:
                health_registry.success("semantic_scholar")
            records.extend(_paper_record(p, query) for p in batch)
            if provider_health is not None:
                provider_health["semantic_scholar"]["successful_requests"] += 1
                provider_health["semantic_scholar"]["records_seen"] += len(batch)
        except Exception as exc:  # noqa: BLE001
            if health_registry is not None:
                health_registry.failure("semantic_scholar", _provider_failure_bucket(exc))
            if provider_health is not None:
                provider_health["semantic_scholar"][_provider_failure_bucket(exc)] += 1
            errors.append(f"semantic_scholar: {exc}")
    return records, errors


def _guess_web_document_type(title: str, url: str, text: str = "") -> str:
    blob = f"{title} {url} {text[:2500]}".lower()
    if any(x in blob for x in ("technical specification", "teknik şartname", "şartname")):
        return "TECHNICAL_STANDARD"
    if any(x in blob for x in ("guideline", "manual", "yönerge", "rehber")):
        return "TECHNICAL_GUIDELINE"
    if any(x in blob for x in ("inventory", "envanter")):
        return "INVENTORY"
    if any(x in blob for x in ("unit price", "birim fiyat", "maliyet kitab", "cost report")):
        return "COST_REPORT"
    if any(x in blob for x in ("accident investigation", "kaza raporu", "investigation report")):
        return "ACCIDENT_REPORT"
    if any(x in blob for x in ("/haber", "/news", "press release", "basın açıklaması")):
        return "NEWS"
    return "WEB_PAGE"


def _allowed_domain(url: str, domains: Iterable[str]) -> bool:
    host = _host(url)
    for domain in domains:
        d = str(domain).lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _extract_page_record(
    url: str,
    html_text: str,
    *,
    source_name: str,
    source_cfg: dict[str, Any],
    query: str,
) -> tuple[DiscoveryRecord | None, _HTMLCollector]:
    parsed = parse_html(html_text)
    title = parsed.title or Path(urllib.parse.urlparse(url).path).name or source_name
    combined = f"{title} {url} {parsed.text[:5000]}"
    if not relevant_text(combined):
        return None, parsed
    doc_type = _guess_web_document_type(title, url, parsed.text)
    record = DiscoveryRecord(
        title=title,
        source=source_name.lower(),
        discovery_source=f"institutional:{source_name}",
        discovery_query=query,
        source_url=url,
        landing_url=url,
        publisher=str(source_cfg.get("publisher") or source_name),
        abstract=parsed.text[:4000],
        document_type=doc_type,
        source_class=str(source_cfg.get("source_class") or "UNKNOWN"),
        metadata_only=bool(source_cfg.get("metadata_only", False)),
        access_kind="institutional_web",
    )
    return record, parsed


def crawl_seed_source(source_name: str, source_cfg: dict[str, Any], terms: list[str] | None = None) -> tuple[list[DiscoveryRecord], list[str]]:
    if not source_cfg.get("enabled", False):
        return [], []
    policy = _config().get("policy") or {}
    max_pages = int(policy.get("max_pages_per_seed") or 30)
    max_depth = int(policy.get("max_depth") or 2)
    delay = float(policy.get("crawl_delay_seconds") or 0.35)
    domains = list(source_cfg.get("domains") or [])
    session = requests.Session()
    queue: deque[tuple[str, int, str]] = deque()
    for url in source_cfg.get("seed_urls") or []:
        queue.append((str(url), 0, "seed"))
    search_terms = list(terms or relevance_terms())[:12]
    for template in source_cfg.get("internal_search_urls") or []:
        for term in search_terms:
            queue.append((str(template).format(query=urllib.parse.quote_plus(term)), 0, term))

    seen: set[str] = set()
    records: list[DiscoveryRecord] = []
    errors: list[str] = []
    pages = 0
    while queue and pages < max_pages:
        raw_url, depth, query = queue.popleft()
        url = normalize_url(raw_url)
        if url in seen or not _allowed_domain(url, domains):
            continue
        seen.add(url)
        try:
            response = safe_get(url, session=session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_name}:{url}: {exc}")
            continue
        pages += 1
        ctype = response.headers.get("Content-Type", "").lower()
        final_url = response.url
        if "application/pdf" in ctype or final_url.lower().split("?", 1)[0].endswith(".pdf"):
            title = Path(urllib.parse.urlparse(final_url).path).stem or source_name
            records.append(DiscoveryRecord(
                title=title, source=source_name.lower(), discovery_source=f"institutional:{source_name}",
                discovery_query=query, source_url=final_url, pdf_url=final_url,
                publisher=str(source_cfg.get("publisher") or source_name),
                document_type=_guess_web_document_type(title, final_url),
                source_class=str(source_cfg.get("source_class") or "UNKNOWN"),
                metadata_only=bool(source_cfg.get("metadata_only", False)), access_kind="institutional_pdf",
            ))
            response.close()
            continue
        text = response.text
        record, parsed = _extract_page_record(final_url, text, source_name=source_name, source_cfg=source_cfg, query=query)
        if record:
            records.append(record)
        if depth >= max_depth:
            continue
        for href, anchor in parsed.links:
            if not href or href.startswith(("mailto:", "javascript:", "#")):
                continue
            child = normalize_url(urllib.parse.urljoin(final_url, href))
            if not _allowed_domain(child, domains):
                continue
            clue = f"{child} {anchor}"
            if relevant_text(clue):
                if child.lower().split("?", 1)[0].endswith(".pdf"):
                    records.append(DiscoveryRecord(
                        title=anchor or Path(urllib.parse.urlparse(child).path).stem,
                        source=source_name.lower(), discovery_source=f"institutional:{source_name}",
                        discovery_query=query, source_url=child, pdf_url=child,
                        publisher=str(source_cfg.get("publisher") or source_name),
                        document_type=_guess_web_document_type(anchor, child),
                        source_class=str(source_cfg.get("source_class") or "UNKNOWN"),
                        metadata_only=bool(source_cfg.get("metadata_only", False)), access_kind="institutional_link",
                    ))
                else:
                    queue.append((child, depth + 1, query))
        time.sleep(delay)
    return records, errors


def sitemap_urls(base_url: str, *, limit: int = 500) -> list[str]:
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    probes = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    found: list[str] = []
    seen_maps: set[str] = set()
    queue = deque(probes)
    session = requests.Session()
    while queue and len(found) < limit:
        sm = queue.popleft()
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            response = safe_get(sm, session=session, respect_robots=False, timeout=12)
            root = ET.fromstring(response.content)
        except Exception:
            continue
        locs = [el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
        if root.tag.lower().endswith("sitemapindex"):
            for loc in locs[:50]:
                if is_public_web_url(loc) and _host(loc) == _host(base_url):
                    queue.append(loc)
        else:
            for loc in locs:
                if _host(loc) == _host(base_url) and relevant_text(loc):
                    found.append(normalize_url(loc))
                    if len(found) >= limit:
                        break
    return list(dict.fromkeys(found))


def dergipark_oai_endpoint(url: str) -> str | None:
    match = re.search(r"dergipark\.org\.tr/(?:tr|en)/pub/([^/?#]+)", url, re.I)
    if not match:
        return None
    return f"https://dergipark.org.tr/api/public/oai/{match.group(1)}/"


def harvest_oai(endpoint: str, *, terms: Iterable[str] | None = None, limit: int = 100) -> list[DiscoveryRecord]:
    if not is_public_web_url(endpoint):
        return []
    response = safe_get(endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode({"verb": "ListRecords", "metadataPrefix": "oai_dc"}), respect_robots=False)
    root = ET.fromstring(response.content)
    records: list[DiscoveryRecord] = []
    for record in root.iter():
        if not record.tag.lower().endswith("record"):
            continue
        fields: defaultdict[str, list[str]] = defaultdict(list)
        for el in record.iter():
            name = el.tag.split("}")[-1].lower()
            if el.text and name in {"title", "creator", "subject", "description", "date", "identifier", "publisher", "type", "language"}:
                fields[name].append(re.sub(r"\s+", " ", el.text).strip())
        title = " ".join(fields["title"]).strip()
        blob = " ".join([title, *fields["subject"], *fields["description"]])
        if not title or not relevant_text(blob, terms):
            continue
        urls = [x for x in fields["identifier"] if x.startswith(("http://", "https://"))]
        pdf = next((u for u in urls if re.search(r"\.pdf(?:$|[?#])", u, re.I)), None)
        landing = next((u for u in urls if u != pdf), None)
        doi = next((harvest.normalize_doi(x) for x in fields["identifier"] if harvest.normalize_doi(x)), None)
        year = next((re.search(r"\b(?:19|20)\d{2}\b", x).group(0) for x in fields["date"] if re.search(r"\b(?:19|20)\d{2}\b", x)), None)
        records.append(DiscoveryRecord(
            title=title, source="oai_pmh", discovery_source="oai_pmh", source_url=landing or pdf,
            landing_url=landing, pdf_url=pdf, publisher="; ".join(fields["publisher"]),
            abstract=" ".join(fields["description"]), authors=list(fields["creator"]), year=year, doi=doi,
            document_type="JOURNAL_ARTICLE", source_class="ACADEMIC", access_kind="oai_pmh",
            extra={"oai_endpoint": endpoint, "subjects": fields["subject"]},
        ))
        if len(records) >= limit:
            break
    return records


def latest_common_crawl_index() -> str | None:
    cfg = _config().get("common_crawl") or {}
    if not cfg.get("enabled", False):
        return None
    url = str(cfg.get("collinfo_url") or "https://index.commoncrawl.org/collinfo.json")
    try:
        response = safe_get(url, respect_robots=False, timeout=15)
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, list) and payload:
        return str(payload[0].get("id") or "") or None
    return None


def common_crawl_expand_domain(domain: str, *, terms: Iterable[str] | None = None, limit: int | None = None) -> list[DiscoveryRecord]:
    """Use Common Crawl only to find URLs within an already trusted domain."""
    cfg = _config().get("common_crawl") or {}
    if not cfg.get("enabled", False):
        return []
    index_id = latest_common_crawl_index()
    if not index_id:
        return []
    limit = int(limit or cfg.get("max_records_per_domain") or 250)
    base = str(cfg.get("index_base") or "https://index.commoncrawl.org").rstrip("/")
    endpoint = f"{base}/{index_id}-index"
    params = {
        "url": f"{domain}/*",
        "output": "json",
        "page": 0,
        "pageSize": int(cfg.get("max_index_pages") or 1),
    }
    try:
        response = safe_get(endpoint + "?" + urllib.parse.urlencode(params), respect_robots=False, timeout=25)
    except Exception:
        return []
    rows: list[DiscoveryRecord] = []
    for line in response.text.splitlines():
        if len(rows) >= limit:
            break
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = item.get("url")
        if not url or not relevant_text(str(url), terms):
            continue
        rows.append(DiscoveryRecord(
            title=Path(urllib.parse.urlparse(url).path).name or str(url),
            source="common_crawl", discovery_source="common_crawl", source_url=str(url), landing_url=str(url),
            document_type="DISCOVERY_RECORD", source_class="DISCOVERY_ONLY", metadata_only=True,
            access_kind="common_crawl_url_index", extra={"crawl": index_id, "mime": item.get("mime")},
        ))
    return rows


def _known_domain_policy() -> dict[str, tuple[int, str, str]]:
    mapping: dict[str, tuple[int, str, str]] = {}
    for name, cfg in institutional_sources().items():
        for domain in cfg.get("domains") or []:
            mapping[str(domain).lower()] = (
                int(cfg.get("authority_score") or 0),
                str(cfg.get("source_class") or "UNKNOWN"),
                str(cfg.get("publisher") or name),
            )
    return mapping


def domain_authority(url: str) -> tuple[int, str, str]:
    host = _host(url)
    known = _known_domain_policy()
    for domain, info in known.items():
        if host == domain or host.endswith("." + domain):
            return info
    if host.endswith(".gov.tr"):
        return 95, "TR_OFFICIAL", host
    if host.endswith((".gov", ".gov.uk", ".admin.ch")) or host.endswith(".europa.eu"):
        return 93, "INT_OFFICIAL", host
    if host.endswith((".edu.tr", ".edu", ".ac.uk", ".edu.au")):
        return 84, "UNIVERSITY_REPOSITORY", host
    if any(x in host for x in ("repository", "dspace", "openaccess", "eprints")):
        return 82, "UNIVERSITY_REPOSITORY", host
    if host.endswith(".int"):
        return 90, "INT_OFFICIAL", host
    return 50, "UNKNOWN", host


def promote_dynamic_seeds(records: Iterable[DiscoveryRecord]) -> list[dict[str, Any]]:
    policy = _config().get("policy") or {}
    min_docs = int(policy.get("dynamic_seed_min_relevant_urls") or 2)
    min_score = int(policy.get("dynamic_seed_min_authority_score") or 75)
    grouped: defaultdict[str, list[DiscoveryRecord]] = defaultdict(list)
    for record in records:
        url = record.landing_url or record.source_url or record.pdf_url
        if not url or _blocked_domain(url):
            continue
        host = _host(url)
        if host:
            grouped[host].append(record)
    promoted: list[dict[str, Any]] = []
    for host, rows in grouped.items():
        score, source_class, publisher = domain_authority("https://" + host)
        if len(rows) < min_docs or score < min_score:
            continue
        promoted.append({
            "domain": host,
            "authority_score": score,
            "source_class": source_class,
            "publisher": publisher,
            "relevant_records": len(rows),
            "seed_url": "https://" + host + "/",
        })
    promoted.sort(key=lambda x: (x["authority_score"], x["relevant_records"]), reverse=True)
    return promoted


def deduplicate(records: Iterable[DiscoveryRecord]) -> list[DiscoveryRecord]:
    best: dict[str, DiscoveryRecord] = {}
    for record in records:
        key = record.key()
        prev = best.get(key)
        if prev is None:
            best[key] = record
            continue
        # Prefer a direct PDF, richer abstract, and non-metadata-only source.
        rank = (bool(record.pdf_url), not record.metadata_only, len(record.abstract or ""))
        prev_rank = (bool(prev.pdf_url), not prev.metadata_only, len(prev.abstract or ""))
        if rank > prev_rank:
            best[key] = record
    return list(best.values())


def secure_download_pdf(url: str, destination: Path) -> tuple[str, int, str]:
    """Stream a public PDF to disk, validating every redirect and hashing inline."""
    cfg = _config().get("policy") or {}
    max_bytes = int(cfg.get("max_pdf_bytes") or harvest.MAX_PDF_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    tmp.unlink(missing_ok=True)
    response = safe_get(url, stream=True)
    digest = hashlib.sha256()
    total = 0
    first = b""
    try:
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:5]
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("PDF exceeds configured size limit")
                digest.update(chunk)
                handle.write(chunk)
        if not first.startswith(b"%PDF"):
            raise ValueError("response is not a PDF")
        tmp.replace(destination)
        return digest.hexdigest(), total, response.url
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _snapshot_web(record: DiscoveryRecord, root: Path) -> dict[str, Any]:
    url = record.landing_url or record.source_url
    if not url:
        raise ValueError("web record has no URL")
    response = safe_get(url)
    max_bytes = int((_config().get("policy") or {}).get("max_web_bytes") or 10 * 1024 * 1024)
    raw = response.content
    if len(raw) > max_bytes:
        raise ValueError("web page exceeds configured size limit")
    text = raw.decode(response.encoding or "utf-8", errors="replace")
    parsed = parse_html(text)
    title = record.title or parsed.title or "web_source"
    stem = harvest.sanitize_filename(title, max_length=90)
    fingerprint = hashlib.sha256(normalize_url(response.url).encode("utf-8")).hexdigest()[:10]
    doc_dir = root / "web" / f"{stem}_{fingerprint}"
    doc_dir.mkdir(parents=True, exist_ok=True)
    html_path = doc_dir / "source.html"
    md_path = doc_dir / "source.md"
    html_path.write_bytes(raw)
    md_path.write_text(html_to_markdown(title, parsed.text, response.url), encoding="utf-8")
    sha, size = _hash_file(md_path)
    return {
        "source_path": str(md_path),
        "source_sha256": sha,
        "source_size_bytes": size,
        "raw_html_path": str(html_path),
        "raw_html_sha256": _hash_file(html_path)[0],
        "resolved_url": response.url,
        "snapshot_title": parsed.title,
        "text_excerpt": parsed.text[:6000],
    }


def _oa_fallback_url(record: DiscoveryRecord, attempted_url: str) -> str | None:
    """Find a legal OA PDF alternative after a publisher URL is blocked or fails."""
    if not record.doi:
        return None
    candidate = harvest.resolve_pdf_url(harvest.Paper(
        title=record.title,
        source=record.source,
        authors=record.authors,
        year=record.year,
        abstract=record.abstract,
        venue=record.publisher,
        doi=record.doi,
        pdf_url=None,
        landing_url=record.landing_url,
        query=record.discovery_query,
    ))
    if not candidate or normalize_url(candidate) == normalize_url(attempted_url):
        return None
    if _blocked_domain(candidate):
        return None
    return candidate


def _acquisition_failure_status(exc: Exception, record: DiscoveryRecord) -> str:
    if isinstance(exc, DNSResolutionError):
        return "DNS_FAILURE"
    if isinstance(exc, SecurityBlockedError):
        return "SECURITY_BLOCKED"
    if isinstance(exc, PermissionError):
        return "ROBOTS_BLOCKED"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "READ_TIMEOUT"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 401:
            return "LOGIN_REQUIRED" if "kgm.gov.tr" in _host(record.source_url or record.landing_url) else "HTTP_401"
        if status == 403:
            return "HTTP_403"
        if status == 404:
            return "HTTP_404"
        if status == 429:
            return "HTTP_429"
        if status and 500 <= status <= 599:
            return "HTTP_5XX"
    message = str(exc).casefold()
    if "blocked by policy" in message:
        return "BLOCKED_DOMAIN"
    if "not a pdf" in message or "content-type" in message:
        return "INVALID_CONTENT_TYPE"
    if "too large" in message or "exceeds configured size" in message:
        return "SOURCE_TOO_LARGE"
    if "pdf" in message and ("validation" in message or "header" in message):
        return "PDF_VALIDATION_FAILED"
    return "UNKNOWN_ACQUISITION_ERROR"


def acquire_record(record: DiscoveryRecord, output_root: Path) -> dict[str, Any]:
    payload = record.as_dict()
    payload["paper_crawler_status"] = "DISCOVERED"
    noncontent = relevance.noncontent_decision(payload)
    payload.update(noncontent)
    if noncontent["noncontent_status"] == "REJECT_NONCONTENT_PAGE":
        payload["acquisition_status"] = "REJECT_NONCONTENT_PAGE"
        payload["paper_crawler_status"] = "FILTERED"
        return payload
    decision = relevance.evaluate(payload)
    payload.update(decision)
    if not relevance.acquisition_allowed(decision, payload):
        payload["acquisition_status"] = "REJECT_IRRELEVANT"
        payload["paper_crawler_status"] = "REJECTED"
        return payload
    if record.metadata_only:
        payload["acquisition_status"] = "METADATA_ONLY"
        return payload
    url = record.pdf_url or record.source_url or record.landing_url
    if not url:
        payload["acquisition_status"] = "NO_URL"
        return payload
    try:
        looks_pdf = bool(record.pdf_url) or re.search(r"\.pdf(?:$|[?#])", url, re.I)
        if looks_pdf:
            stem = harvest.sanitize_filename(record.title, max_length=90)
            year = f"{record.year}_" if record.year and str(record.year).isdigit() else ""
            dest = output_root / "pdfs" / f"{year}{stem}.pdf"
            if dest.exists():
                sha, size = _hash_file(dest)
                resolved = url
            else:
                try:
                    sha, size, resolved = secure_download_pdf(url, dest)
                except Exception as first_exc:
                    fallback = _oa_fallback_url(record, url)
                    if not fallback:
                        raise
                    _log(f"OA fallback: {record.title[:70]!r} → {fallback}")
                    sha, size, resolved = secure_download_pdf(fallback, dest)
                    payload["initial_acquisition_error"] = str(first_exc)
                    payload["oa_fallback_url"] = fallback
            payload.update({
                "source_path": str(dest),
                "local_pdf_path": str(dest),
                "pdf_path": str(dest),
                "path": str(dest),
                "source_sha256": sha,
                "source_size_bytes": size,
                "resolved_url": resolved,
                "acquisition_status": "DOWNLOADED_PDF",
            })
        else:
            payload.update(_snapshot_web(record, output_root))
            payload["acquisition_status"] = "SNAPSHOTTED_WEB"
        payload["paper_crawler_status"] = "STAGING"
    except Exception as exc:  # noqa: BLE001
        payload["acquisition_status"] = _acquisition_failure_status(exc, record)
        payload["acquisition_error"] = str(exc)
        if record.source_class in {"TR_OFFICIAL", "INT_OFFICIAL"}:
            payload["metadata_only"] = True
    return payload


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def discover_all(
    *,
    output_dir: str | Path | None = None,
    max_queries: int = 60,
    per_source: int = 10,
    acquire: bool = True,
    expand_dynamic: bool = True,
    embedding_server: str | None = None,
    embedding_model: str | None = None,
    use_local_embedding: bool = True,
) -> dict[str, Any]:
    """Run bounded free discovery and optionally acquire public sources."""
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    discovery_root = root / "discovery_sources"
    discovery_root.mkdir(parents=True, exist_ok=True)
    queries = topic_queries(max_queries=max_queries)
    records: list[DiscoveryRecord] = []
    errors: list[str] = []
    source_counts: Counter[str] = Counter()
    provider_health: dict[str, Counter[str]] = defaultdict(Counter)
    health_registry = source_health.SourceHealthRegistry(root / "audit" / "source_health.json")
    rejected_irrelevant = 0
    oai_seen = oai_relevant = oai_rejected_irrelevant = 0
    provider_failures: Counter[str] = Counter()
    disabled_sources: set[str] = set()
    source_cfg = _config().get("academic_sources") or {}
    embedding_client, selected_embedding_model = hybrid.detect_local_embedding(embedding_server, embedding_model) if use_local_embedding else (None, None)
    relevance_profile_vectors: dict[str, list[float]] = {}

    # Academic discovery is query-driven.
    _log(f"starting discovery: queries={len(queries)} per_source={per_source} acquire={acquire}")
    for index, query in enumerate(queries, 1):
        _log(f"query {index}/{len(queries)}: {query!r}")
        batch, batch_errors = discover_academic(
            query, per_source=per_source, disabled_sources=disabled_sources, provider_health=provider_health,
            health_registry=health_registry,
        )
        batch, rejected = filter_relevant_records(batch, embedding_client=embedding_client, embedding_model=selected_embedding_model, profile_vectors=relevance_profile_vectors)
        rejected_irrelevant += rejected
        records.extend(batch)
        errors.extend(batch_errors)
        for error in batch_errors:
            name = error.partition(":")[0]
            max_failures = int((source_cfg.get(name) or {}).get("max_consecutive_failures") or 2)
            provider_failures[name] += 1
            if provider_failures[name] >= max_failures and name not in disabled_sources:
                disabled_sources.add(name)
                provider_health[name]["disabled"] = 1
                _log(f"source={name} disabled for remaining queries after {provider_failures[name]} failures")
        for item in batch:
            source_counts[item.discovery_source] += 1
            provider_health[item.discovery_source]["relevant_records"] += 1
        if len(records) >= max_queries * per_source * 4:
            _log(f"academic record cap reached: records={len(records)}")
            break
        time.sleep(0.08)

    # Curated institutions use their own seeds/search/sitemaps.
    institutional = institutional_sources()
    for name, cfg in institutional.items():
        if not cfg.get("enabled", False) or (cfg.get("metadata_only") and not cfg.get("seed_urls")):
            continue
        batch, batch_errors = crawl_seed_source(name, cfg)
        batch, rejected = filter_relevant_records(batch, embedding_client=embedding_client, embedding_model=selected_embedding_model, profile_vectors=relevance_profile_vectors)
        rejected_irrelevant += rejected
        records.extend(batch)
        errors.extend(batch_errors)
        source_counts[f"institutional:{name}"] += len(batch)
        if cfg.get("sitemap_probe"):
            for seed in cfg.get("seed_urls") or []:
                for url in sitemap_urls(str(seed), limit=150):
                    records.append(DiscoveryRecord(
                        title=Path(urllib.parse.urlparse(url).path).name or url,
                        source=name.lower(), discovery_source=f"sitemap:{name}", source_url=url, landing_url=url,
                        publisher=str(cfg.get("publisher") or name), document_type="DISCOVERY_RECORD",
                        source_class=str(cfg.get("source_class") or "UNKNOWN"), metadata_only=True,
                        access_kind="sitemap_url",
                    ))

    records = deduplicate(records)
    _log(f"discovery complete: unique_records={len(records)}; starting acquisition={acquire}")

    # DergiPark OAI-PMH is derived from journal URLs found by academic discovery.
    oai_endpoints = {ep for rec in records for ep in [dergipark_oai_endpoint(rec.landing_url or rec.source_url or "")] if ep}
    for endpoint in sorted(oai_endpoints)[:25]:
        try:
            raw_batch = harvest_oai(endpoint, limit=100)
            oai_seen += len(raw_batch)
            batch, rejected = filter_relevant_records(raw_batch, embedding_client=embedding_client, embedding_model=selected_embedding_model, profile_vectors=relevance_profile_vectors)
            oai_relevant += len(batch)
            oai_rejected_irrelevant += rejected
            rejected_irrelevant += rejected
            records.extend(batch)
            source_counts["oai_pmh"] += len(batch)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"oai:{endpoint}: {exc}")
    records = deduplicate(records)

    dynamic = promote_dynamic_seeds(records)
    (root / "dynamic_seeds.json").write_text(json.dumps(dynamic, ensure_ascii=False, indent=2), encoding="utf-8")
    candidates_path = root / "audit" / "institution_candidates.jsonl"
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(candidates_path, ({**seed, "status": "CANDIDATE_UNVERIFIED", "auto_promoted_to_high_authority": False} for seed in dynamic))

    # Expand newly found trusted domains conservatively. Common Crawl supplies URLs only.
    if expand_dynamic:
        known_domains = {d for cfg in institutional.values() for d in (cfg.get("domains") or [])}
        for seed in dynamic[:20]:
            domain = str(seed["domain"])
            if domain in known_domains:
                continue
            try:
                for url in sitemap_urls(str(seed["seed_url"]), limit=100):
                    records.append(DiscoveryRecord(
                        title=Path(urllib.parse.urlparse(url).path).name or url,
                        source="dynamic_seed", discovery_source="dynamic_sitemap", source_url=url, landing_url=url,
                        publisher=str(seed.get("publisher") or domain), document_type="DISCOVERY_RECORD",
                        source_class=str(seed.get("source_class") or "UNKNOWN"), metadata_only=True,
                        access_kind="dynamic_sitemap",
                    ))
                records.extend(common_crawl_expand_domain(domain, limit=100))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dynamic:{domain}: {exc}")
    records, rejected = filter_relevant_records(records, embedding_client=embedding_client, embedding_model=selected_embedding_model, profile_vectors=relevance_profile_vectors)
    rejected_irrelevant += rejected
    records = deduplicate(records)

    discovered_path = root / "discovered_records.jsonl"
    _write_jsonl(discovered_path, (r.as_dict() for r in records))

    acquired: list[dict[str, Any]] = []
    if acquire:
        for index, record in enumerate(records, 1):
            _log(f"acquire {index}/{len(records)}: {record.title[:80]!r}")
            # Do not acquire discovery-only sitemap/Common Crawl rows until a current page is validated.
            if record.document_type == "DISCOVERY_RECORD" and record.metadata_only:
                acquired.append(record.as_dict() | {"acquisition_status": "METADATA_ONLY"})
                continue
            acquired.append(acquire_record(record, discovery_root))
    else:
        acquired = [r.as_dict() | {"acquisition_status": "NOT_REQUESTED"} for r in records]

    catalog_path = root / "discovery_catalog.jsonl"
    _write_jsonl(catalog_path, acquired)
    acquisition_counts = Counter(str(row.get("acquisition_status") or "UNKNOWN") for row in acquired)
    report = {
        "schema_version": "1.0",
        "paid_search_apis_used": False,
        "relevance_embedding_model": selected_embedding_model,
        "queries": len(queries),
        "discovered_unique": len(records),
        "relevant_candidates": len(records),
        "rejected_irrelevant": rejected_irrelevant,
        "oai_stats": {
            "oai_seen": oai_seen,
            "oai_relevant": oai_relevant,
            "oai_rejected_irrelevant": oai_rejected_irrelevant,
        },
        "dynamic_seeds": len(dynamic),
        "source_counts": dict(source_counts),
        "disabled_sources": sorted(disabled_sources),
        "provider_failures": dict(provider_failures),
        "provider_health": {name: dict(values) for name, values in sorted(provider_health.items())},
        "global_source_health": health_registry.data,
        "acquisition_counts": dict(acquisition_counts),
        "errors": errors,
        "discovered_records": str(discovered_path),
        "discovery_catalog": str(catalog_path),
        "dynamic_seed_registry": str(root / "dynamic_seeds.json"),
    }
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "discovery_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"finished: catalog={catalog_path}")
    return report
