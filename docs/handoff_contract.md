# PaperCrawler → TunnelBookAI Handoff Contract

- **Producer:** `paper-crawler-agent` (PaperCrawler)
- **Consumer:** TunnelBookAI Unified Ingest Engine
- **Contract schema version:** `2.0`
- **Machine copy:** every export writes `00_registry/handoff_contract.json`

PaperCrawler never writes a canonical TunnelBookAI corpus. It produces a
self-contained, integrity-checked **source package** at
`READY_FOR_HANDOFF`. TunnelBookAI owns everything downstream.

---

## 1. Responsibility split

| PaperCrawler (producer) | TunnelBookAI (consumer) |
|---|---|
| discovery | full content conversion |
| source acquisition (original PDF / trusted web snapshot) | Docling processing |
| source integrity — byte SHA256 | page snapshots |
| source metadata preservation | embedded image extraction |
| bibliographic dedup (DOI / URL / SHA256 / fuzzy identity) | OCR / vision |
| provisional relevance | table extraction |
| provisional classification (document type, source tier, section hints) | metadata enrichment |
| provisional coverage & gap discovery | global cross-source content dedup |
| provenance | **final** section classification |
| fail-closed **handoff** quality gate | evidence evaluation |
| | **corpus** quality gate |
| | canonical ingest (`corpus/staging` → `corpus/canonical`) |

PaperCrawler explicitly does **not** contain: a DOCX/PPTX/XLSX importer, generic
image OCR, office page rendering, or a manual inbox. Those belong to
TunnelBookAI's `incoming/manual/` pipeline.

---

## 2. Package layout

```text
<package>/
├── 00_registry/
│   ├── handoff_contract.json      # this contract, machine form
│   ├── manifest.jsonl             # full internal manifest
│   ├── handoff_manifest.jsonl     # AUTHORITATIVE consumer manifest (schema 2.0)
│   └── checksums.sha256           # byte checksum for every packaged file
├── 01_originals/
│   └── <ROUTE>/<document_id>/
│       ├── source.<ext>           # acquired original (hardlink or copy)
│       ├── source_raw.html        # raw web snapshot, when available
│       ├── metadata.json
│       └── classification.json
└── 99_audit/
    ├── handoff_audit.json
    ├── handoff_quality_gate.json  # GO / CONDITIONAL_GO / NO_GO
    ├── decision_summary.json
    ├── review_queue.jsonl
    ├── retry_acquisition.jsonl
    ├── metadata_references.jsonl  # reference metadata, NOT in READY_FOR_HANDOFF
    ├── reclassify_queue.jsonl
    └── rejected_manifest.jsonl
```

The legacy alias `99_audit`/`audit/corpus_quality_gate.json` is still written for
backward compatibility and carries `{"deprecated_alias": true,
"canonical_artifact": "audit/handoff_quality_gate.json"}`.

---

## 3. State machine (`paper_crawler_status`)

| State | Meaning | In `READY_FOR_HANDOFF` count? |
|---|---|---|
| `READY_FOR_HANDOFF` | Physical source + SHA256 + provenance + passing integrity + sufficient provisional classification | **yes** |
| `METADATA_REFERENCE` | High-value official/bibliographic reference with **no ingestable content** (URL/DOI/ISBN/title only) | **no** — separate queue |
| `RETRY_ACQUISITION` | Relevant, source missing or transiently failed; bounded reacquire | no |
| `MANUAL_REVIEW` | Unresolved relevance/section judgement needs a human | no |
| `AUTO_REJECT` | Deterministic reject (irrelevant, non-content, hard acquisition failure, SHA mismatch, duplicate); provenance retained | no |

`tunnelbookai_status` is always `NOT_INGESTED` at handoff.

TunnelBookAI may treat `METADATA_REFERENCE` as `REFERENCE_ONLY` or as an
acquisition-retry lead. It must not be ingested as corpus content.

---

## 4. Evidence semantics

`crawler_evidence_level` describes **only what the crawler inspected**:

| Level | Meaning |
|---|---|
| `ORIGINAL_ACQUIRED` | Original file acquired; no text signal extracted yet |
| `LIGHT_PDF_TEXT` | First-pages text read for provisional classification only |
| `WEB_SNAPSHOT_TEXT` | Text from a captured web representation |
| `ABSTRACT` | Publisher/aggregator abstract only |
| `TITLE_METADATA_ONLY` | Title + metadata only |

PaperCrawler **never** emits `FULL_TEXT` or `PDF_EXTRACT`. Full-text evidence is
established by TunnelBookAI after Docling conversion, OCR/vision and table
extraction. Legacy artifacts containing `extracted_full_text` /
`FULL_TEXT` / `PDF_EXTRACT` are accepted on read and remapped
(`legacy_extracted_full_text_metric`, `LIGHT_PDF_TEXT`, `WEB_SNAPSHOT_TEXT`),
never re-emitted.

---

## 5. Provisional vs final metadata

Every handoff manifest record carries both:

```jsonc
{
  "provisional_document_type":  "TECHNICAL_REPORT",
  "provisional_source_tier":    "TIER_A",
  "provisional_primary_section":"5.5.2",
  "provisional_secondary_sections": ["5.4", "5.6"],
  "provisional_section_confidence": 0.91,
  "provisional_classification_status": "AUTO_ACCEPT",

  "final_document_type":   null,
  "final_source_tier":     null,
  "final_primary_section": null,
  "final_secondary_sections": [],
  "final_section_status":  "NOT_EVALUATED",
  "final_evidence_status": "NOT_EVALUATED"
}
```

> PaperCrawler section assignments are provisional discovery/classification
> hints. TunnelBookAI must perform final section classification using normalized
> full content before corpus ingest.

---

## 6. Source representation (web)

```jsonc
"source_representation": {
  "original_or_raw": "01_originals/.../source_raw.html",  // authoritative capture
  "crawler_normalized": null,                              // or a .md path
  "crawler_normalized_status": "PROVISIONAL"
}
```

TunnelBookAI re-normalizes from `original_or_raw` and produces the final corpus
Markdown itself.

---

## 7. Checksum & provenance rules

- Every packaged file appears in `00_registry/checksums.sha256`.
- `handoff_manifest.jsonl.sha256` is the byte SHA256 of the acquired original.
- A SHA256 mismatch is a hard `AUTO_REJECT` and a `handoff_sha256_mismatch`
  gate blocker.
- `provenance` must be non-empty: `{source_url, landing_url, pdf_url,
  discovery_source, discovery_query, doi, publisher}` (present subset).
- `producer: "paper-crawler-agent"` and `source_kind: "EXTERNAL_DISCOVERY"` are
  stamped on every record so TunnelBookAI's global dedup can merge the external
  and manual/internal channels.

---

## 8. Handoff quality gate blockers

`handoff_quality_gate.py` is fail-closed. `NO_GO` on any of:
`pipeline_incomplete`, `classification_index_unreadable`,
`reconciliation_invariant_failed`, `handoff_manifest_missing_or_invalid`,
`duplicate_or_missing_document_id`, `duplicate_or_missing_canonical_id`,
`duplicate_or_missing_sha256`, `handoff_file_missing`,
`handoff_sha256_mismatch`, `handoff_provenance_missing`,
`coverage_policy_missing`, `coverage_internal_inconsistency`,
`invalid_route_path`, `invalid_papercrawler_fulltext_claim`,
`metadata_reference_marked_ready`, `invalid_provisional_section`,
`handoff_contract_schema_invalid`, `source_representation_missing`.

---

## 9. Backward compatibility

- `corpus_quality_gate.evaluate(...)` still works as a deprecated shim.
- `evidence_level` field retained as an alias of `crawler_evidence_level`.
- `primary_section` / `book_sections` / `classification_confidence` retained
  alongside the `provisional_*` names.
- Package folder layout (`00_registry` / `01_originals` / `99_audit`) unchanged.
- `--destination`, `--resume`, checkpoint/resume behaviour unchanged.

---

## 10. Example manifest record

```json
{
  "schema_version": "2.0",
  "document_id": "PC_1234567890ABCDEF",
  "canonical_id": "CAN_1234567890ABCDEF",
  "canonical_hint_id": "CAN_1234567890ABCDEF",
  "producer": "paper-crawler-agent",
  "source_kind": "EXTERNAL_DISCOVERY",
  "title": "Life cycle cost of road tunnels",
  "authors": ["A. Author"],
  "year": 2025,
  "doi": null,
  "source_url": "https://example.org/paper",
  "landing_url": "https://example.org/paper",
  "resolved_url": "https://example.org/paper.pdf",
  "local_path": "01_originals/C_ACADEMIC/ARTICLES/PC_1234567890ABCDEF/source.pdf",
  "sha256": "…",
  "route_path": "C_ACADEMIC/ARTICLES",
  "acquisition_status": "DOWNLOADED_PDF",
  "crawler_evidence_level": "LIGHT_PDF_TEXT",
  "source_representation": {
    "original_or_raw": "01_originals/.../source.pdf",
    "crawler_normalized": null,
    "crawler_normalized_status": "PROVISIONAL"
  },
  "provisional_document_type": "JOURNAL_ARTICLE",
  "provisional_source_tier": "TIER_B",
  "provisional_primary_section": "4.3.5",
  "provisional_secondary_sections": ["4.3"],
  "provisional_section_confidence": 0.91,
  "provisional_classification_status": "AUTO_ACCEPT",
  "final_primary_section": null,
  "final_secondary_sections": [],
  "final_section_status": "NOT_EVALUATED",
  "final_evidence_status": "NOT_EVALUATED",
  "paper_crawler_status": "READY_FOR_HANDOFF",
  "tunnelbookai_status": "NOT_INGESTED",
  "provenance": {
    "source_url": "https://example.org/paper",
    "discovery_source": "crossref",
    "discovery_query": "road tunnel life cycle cost"
  }
}
```
