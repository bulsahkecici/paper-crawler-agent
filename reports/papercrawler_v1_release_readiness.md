# PaperCrawler v1.0.0 — Release Readiness

**Scope:** Boundary hardening & TunnelBookAI handoff contract finalization.
**Date:** 2026-09-01
**Branch:** `hardening` → merged to `master`
**VERSION:** `1.0.0-rc1` → `1.0.0`

---

## Decision: **GO**

All boundary checks pass, the full offline test suite is green (116/116), and a
controlled two-record offline smoke test through stages 3–7 plus the quality gate
returns `GO`. No production crawl and no destructive corpus operation were
performed.

### Post-review fixes applied

1. **light_pdf_text ↔ abstract separation.** Light PDF text is no longer written
   into the `abstract` field at any stage. `light_pdf_extract.py` stores it only
   in `light_pdf_text`; `classify_catalog.py` feeds the classifier a separate
   `classification_text` (real abstract → light PDF text → web excerpt) and
   persists `abstract` (real only), `light_pdf_text`, and `classification_text`
   as distinct fields. `handoff_export.py` relevance recheck uses the same
   fallback chain.
2. **Acquisition-first ordering for official sources.** `decision_router.route`
   now attempts `RETRY_ACQUISITION` for any relevant record with a real
   acquisition target (incl. official pages) *before* the `METADATA_REFERENCE`
   fallback. `METADATA_REFERENCE` applies to records with nothing to acquire
   (`metadata_only`, ISBN-only) or after a hard acquisition failure — in which
   case a high-value reference is retained rather than `AUTO_REJECT`ed.

---

## Boundary checks

| Check | Result | Evidence |
|---|---|---|
| Canonical corpus not written | **PASS** | PaperCrawler has no corpus write path; `handoff_export.py` only links/copies into a package. `corpus_quality_gate` name released to TunnelBookAI. |
| Full Docling / conversion not performed | **PASS** | No conversion code; `light_pdf_extract` reads first pages only and is labelled provisional. |
| Manual file ingest excluded | **PASS** | No DOCX/PPTX/XLSX importer, generic OCR, office rendering, or manual inbox in the tree. |
| Light PDF is not full-text | **PASS** | metric renamed `extracted_full_text` → `light_pdf_text_extracted`; `crawler_evidence_level` can never be `FULL_TEXT`/`PDF_EXTRACT`; gate blocker `invalid_papercrawler_fulltext_claim`. |
| Section classification is provisional | **PASS** | handoff manifest exposes `provisional_primary_section` / `provisional_secondary_sections`; `final_primary_section: null`, `final_section_status: "NOT_EVALUATED"`; gate blocker `invalid_provisional_section`. |
| Metadata reference separated | **PASS** | new `METADATA_REFERENCE` router state + `99_audit/metadata_references.jsonl`; excluded from `ready_for_handoff`; gate blocker `metadata_reference_marked_ready`. |
| Handoff quality gate correctly named | **PASS** | authoritative `handoff_quality_gate.py` → `audit/handoff_quality_gate.json`; `corpus_quality_gate.py` is a deprecated shim writing an alias artifact with `deprecated_alias: true`. Single evaluation. |
| Handoff contract versioned | **PASS** | `handoff_contract.json` schema `1.1` → `2.0` with `producer_responsibilities`, `consumer_responsibilities`, `semantic_rules`, `state_machine`, `package_layout`. |
| TunnelBookAI consumer responsibilities explicit | **PASS** | contract + `docs/handoff_contract.md` list full conversion, Docling, page snapshots, image extraction, OCR/vision, table extraction, final section classification, evidence evaluation, corpus quality gate, canonical ingest. |
| Web raw vs provisional-normalized semantics | **PASS** | `source_representation.{original_or_raw, crawler_normalized, crawler_normalized_status: "PROVISIONAL"}` on every record; gate blocker `source_representation_missing`. |
| Delivery mode configurable | **PASS** | `--destination <path>` writes a self-contained package; PaperCrawler does not know TunnelBookAI internals. |
| Dedup boundary | **PASS** | bibliographic dedup retained; `producer` + `source_kind: EXTERNAL_DISCOVERY` stamped for TunnelBookAI global re-dedup. |

## Stop-condition audit (§26)

| Stop condition | Status |
|---|---|
| PaperCrawler produces FULL_TEXT from light extraction | not present |
| metadata-only source marked READY_FOR_HANDOFF | not present (routed to `METADATA_REFERENCE`) |
| final section fields populated by PaperCrawler | not present (always `null` / `NOT_EVALUATED`) |
| handoff SHA validation broken | not present (SHA mismatch → `AUTO_REJECT` + gate blocker) |
| provenance missing | blocked (`handoff_provenance_missing`) |
| canonical corpus modified | not present |
| TunnelBookAI-specific full conversion introduced | not present |
| existing regression tests broken | none (113/113 pass) |

---

## Semantics

| Item | Result |
|---|---|
| Light PDF | `LIGHT_PDF_TEXT` |
| PaperCrawler `FULL_TEXT` claims | `0` |
| Section classification | `PROVISIONAL` |
| Metadata-only records | `METADATA_REFERENCE` (separate queue) |
| Handoff quality gate | `handoff_quality_gate.json` authoritative |

---

## Tests

```
python -m unittest discover -s tests -p 'test_*.py' -v
Ran 116 tests — OK
```

New coverage: `tests/test_boundary_hardening.py` (evidence semantics, legacy
alias remap, metadata-reference routing/export, contract versioning,
provisional/final section exposure, gate boundary blockers) plus expanded
`tests/test_stabilization.py::QualityGateTests` (rename, deprecated shim,
fulltext-claim and metadata-reference blockers).

Controlled offline smoke test (2 records, 0 downloads, no network): stages
`pdf_enrichment → initial_classification → source_audit → handoff → quality gate`
→ decision **GO**, `crawler_evidence_level ∈ {ABSTRACT}`, no `extracted_full_text`
metric, `final_primary_section == null`.

---

## Version

- Current: `1.0.0-rc1`
- Recommended: **`1.0.0`** — no failing tests, handoff contract is unambiguous and
  versioned, no metadata-only `READY_FOR_HANDOFF`, light PDF is not counted as
  full-text, provisional/final classification semantics are separated, and the
  quality gate no longer carries corpus semantics.

`VERSION` has been set to `1.0.0`. Tagging/release is left to the maintainer.

---

## Remaining blockers

None.

## Follow-ups (non-blocking)

- `coverage_policy.corpus_eligible_count` keeps its historical key name for
  migration safety; consider renaming to `handoff_eligible_count` in a future
  minor with a compatibility alias.
- `source_representation.crawler_normalized` is currently always `null` (no
  Markdown normalization step exists); the field is contractually reserved.
- Deprecated `corpus_quality_gate.py` shim and `audit/corpus_quality_gate.json`
  alias can be removed once no external consumer reads them.
- `decision_router` has no persistent retry counter; `RETRY_ACQUISITION` relies
  on the bounded `--retry-acquisition-only` mode and hard-failure statuses to
  terminate. A per-record attempt count would let officials fall back to
  `METADATA_REFERENCE` automatically after N soft failures.

---

## Freeze

On `master` CI PASS, PaperCrawler **v1.0.0 is frozen**. Subsequent changes go
through a new branch and a version bump; the handoff contract (`schema_version
2.0`) is stable for TunnelBookAI to build against.
