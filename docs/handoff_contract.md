# PaperCrawler → TunnelBookAI Handoff Contract 2.0

PaperCrawler exports integrity-checked original sources. `READY_FOR_HANDOFF` means ready for TunnelBookAI ingest processing, not canonical evidence.

The source-level manifest contains `document_id`, title, document type, source class, authority tier, relevance status/score, broad topics, producer identity, acquisition status/path/SHA-256, provenance, optional presentation metadata/assets, `paper_crawler_status`, and `tunnelbookai_status`.

No current book chapter is required or emitted. Historical `primary_section` and `book_sections` may exist only within a `legacy` object in internal migration artifacts and are ignored by relevance, routing, coverage, acceptance, and export.

PaperCrawler owns discovery, safe public acquisition, original preservation, source identity, broad topics, deduplication, provenance and checksum audit. For presentations, it also owns best-effort provisional extraction of PPTX/PDF embedded images and public slide previews. TunnelBookAI owns Docling/full extraction, canonical slide rendering and OCR, canonical Markdown, document structure, chunking, figure interpretation, chunk-level topics/evidence, current-book taxonomy, RAG, citation and writing.

For captured web pages, `local_path`, `sha256`, `source_representation.original_or_raw`, and `source_representation.original_or_raw_sha256` identify the immutable raw HTML. The crawler Markdown is exposed only through `crawler_normalized` and `crawler_normalized_sha256`, with status `PROVISIONAL`. TunnelBookAI must derive its own canonical Markdown from the raw HTML.

Schema 2.1 releases carry `00_registry/release_metadata.json`. The release ID is derived after the consumer manifest is written, so the metadata can bind the release to `manifest_sha256` without a circular self-reference. `00_registry/checksums.sha256` covers package files, including the package-local `99_audit/handoff_quality_gate.json`, and excludes only itself and the completion marker. A completed release is an independent copy and must never be overwritten.

Presentation platforms are discovery provenance, never producers. Producer authority comes from an attributable author or institution. Public originals are preferred; inaccessible content remains `METADATA_ONLY` or `NO_PUBLIC_FULLTEXT`.

Presentation acquisition accepts public PPT, PPTX, and PDF decks. A `presentation_assets` entry must identify its package-relative path, asset SHA-256, parent deck SHA-256, source URL, slide number, media type, dimensions when available, extraction method, and provisional status. The handoff quality gate blocks missing files, checksum mismatches, unsafe paths, and incomplete asset provenance. Legacy binary PPT originals may be exported without extracted assets; canonical rendering remains a consumer responsibility.
