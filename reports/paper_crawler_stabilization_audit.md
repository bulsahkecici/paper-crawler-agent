# Paper Crawler Stabilization Audit

## 1. Executive Summary

**Decision for the existing interrupted pilot: NO-GO.** The observed run stopped
during stage 5/7 with `KeyboardInterrupt`; stages 6/7 and 7/7 did not complete,
and it predates the new checkpoint and corpus gate artifacts. It must not be
treated as a final TunnelBookAI handoff.

The hardened code path is ready for a controlled resumed run. All 91 offline
unit/regression tests pass, including a two-record no-network pipeline smoke test.
No production crawl or destructive corpus operation was performed.

## 2. Bugs Found

- Coverage counted only exact section IDs. A `5.5.1` assignment did not roll up
  into `5.5` and `5`.
- `handoff_candidate_section_coverage` mixed file availability with corpus
  acceptance and did not expose discovered, review, evidence, or accepted counts.
- The displayed `classic=831, discovery=362` values were raw input counts, while
  the 1160 classification count was post-dedup. The missing 33 rows were not
  reconciled in the audit.
- Gap queries could be generic and domain-unanchored.
- Navigation/legal endpoints had no deterministic acquisition prefilter.
- Circuit-breaker state was local to free discovery and reset before gap search.
- Robots policies were fetched repeatedly per host.
- Interrupted stages had no persistent, atomic checkpoint.
- Handoff had no fail-closed final corpus quality gate or CSV review queue.

## 3. Changes Made

- `coverage_policy.py`: parent aggregation and explicit discovered, accepted,
  review, fulltext, and corpus-eligible dimensions.
- `corpus_policy.py`: evidence level, source tier, normalized document type, and
  centralized acceptance policy.
- `pipeline_state.py`: atomic stage checkpointing with `PARTIAL` recovery.
- `source_health.py`: pipeline-global persistent circuit breaker.
- `bibliographic_dedup.py`: DOI, SHA, canonical URL, normalized title, and
  conservative fuzzy canonicalization.
- `corpus_quality_gate.py`: fail-closed GO/CONDITIONAL_GO/NO-GO decision.
- `free_discovery.py`: navigation/legal prefilter, deterministic plus optional
  local-embedding relevance, robots cache, provider metrics, 429 retry, and
  persistent source health.
- `gap_discovery.py`: mandatory tunnel anchors, detailed coverage input,
  metadata-first ranking, and section/download budgets.
- `classify_catalog.py`: evidence/source/document metadata, reconciliation,
  canonical IDs, detailed coverage, and stage summary.
- `handoff_export.py`: strict evidence acceptance, `handoff_manifest.jsonl`,
  `review_queue.csv`, provenance, and manual grouping metadata.
- `prepare_tunnelbookai_handoff.py`: safe resume by default and `--fresh-run`.
- `light_pdf_extract.py`: complete PDF/text enrichment metrics.

## 4. Coverage Before / After

The old `current=0` values were a mixture of real evidence gaps and an
aggregation bug. A read-only calculation over the existing 1160-row index gave:

| Section | Old exact eligible | New discovered (parent roll-up) | New corpus eligible | Review |
| --- | ---: | ---: | ---: | ---: |
| 5 | 0 | 514 | 23 | 486 |
| 5.5 | 0 | 3 | 0 | 3 |
| 5.5.1 | 0 | 2 | 0 | 2 |
| 6 | 0 | 16 | 0 | 15 |
| 6.2 | 5 | 12 | 0 | 12 |
| 6.2.2 | 0 | 4 | 0 | 4 |
| 4.3.5 | 0 | 7 | 0 | 7 |
| 1.1 | 0 | 0 | 0 | 0 |
| 1.3.1 | 0 | 0 | 0 | 0 |

Thus section 5's zero was a clear aggregation bug. `5.5.1`, `6.2.2`, and
`4.3.5` also contain discovered material, but their corpus-eligible count remains
zero under the stricter evidence/acceptance gate; those are real quality gaps.

## 5. Deduplication

The existing input reconciles as follows:

- Classic input: 831
- Discovery input: 362
- Raw total: 1193
- Existing merge output: 1160
- Existing duplicates removed: 33, all DOI matches
- Additional conservative normalized-title duplicates detected: 10
- New projected canonical total: 1150

Fuzzy merging requires both matching year and author overlap. Same-title records
with different year/author are not auto-merged.

## 6. Discovery Precision

Every generic gap query now receives a controlled English or Turkish tunnel
anchor. Login, subscription, sitemap, legal, privacy, contact, and similar pages
are rejected before model use/acquisition, while technical manuals and real
operation pages remain eligible. Strongly unrelated records are rejected by
lexical rules; adjacent engineering records remain borderline and can be
promoted by the local embedding model instead of being aggressively discarded.

No new live precision percentage is claimed because a network crawl was not run.

## 7. Resume Behaviour

`audit/pipeline_state.json` is written atomically. Completed stages are skipped,
an interrupted stage becomes `PARTIAL`, and a subsequent default run resumes it.
Existing successfully downloaded PDFs are detected and hashed instead of being
downloaded again. `--fresh-run` creates a new state without deleting documents.

## 8. Source Reliability

CORE/OpenAlex and other provider health now survives stage transitions in
`audit/source_health.json`. Two consecutive failures open the circuit; success
resets the consecutive counter. OpenAlex 429 responses use bounded exponential
backoff, `Retry-After`, jitter, then the global circuit breaker. Robots results
are cached per host with TTL and robots enforcement remains enabled.

## 9. Classification

Existing interrupted-run distribution:

- LLM_ACCEPTED: 691
- ACCEPT_WITH_AUDIT: 5
- NEEDS_REVIEW: 382
- LOCAL_LLM_REVIEW: 18
- REJECT_IRRELEVANT: 64

Every new classification records one of `FULL_TEXT`, `PDF_EXTRACT`, `ABSTRACT`,
`WEBPAGE_TEXT`, or `TITLE_METADATA_ONLY`. Title-only records cannot auto-handoff.
Source quality (`TIER_A`–`TIER_D`) and normalized document type are independent
from section relevance.

## 10. Corpus Gate

The pipeline now writes `audit/corpus_quality_gate.json` and rejects incomplete
runs, broken reconciliation, unreadable classification indexes, duplicate
canonical IDs/SHA values, missing handoff files, missing provenance, invalid
coverage policy, or missing manifests. Accepted artifacts are exposed through
`00_registry/handoff_manifest.jsonl`; review cases are preserved in JSONL and CSV.

## 11. Tests

**91 / 91 PASS.** Coverage, query anchoring, prefilter, relevance, dedup,
resume, global source health, OpenAlex 429, robots cache, reconciliation policy,
handoff, SHA validation, and corpus gate regressions pass. The controlled smoke
test classified two records covering NATM/support and maintenance/operating cost
through the real rules classifier without network access.

## 12. Remaining Risks

- The existing interrupted pilot has not been resumed through the new stages;
  therefore its final decision remains NO-GO.
- New live provider precision, download success, Qwen review rate, and final
  canonical counts require a bounded resumed run.
- Lightweight PDF extraction is not a replacement for TunnelBookAI/Docling full
  conversion; the handoff contract continues to require downstream validation.
- PIARC parent-manual grouping is metadata-based and should be reviewed against
  live page structure during the bounded run.
