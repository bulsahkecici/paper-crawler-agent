# PaperCrawler → TunnelBookAI Handoff Contract 2.0

PaperCrawler exports integrity-checked original sources. `READY_FOR_HANDOFF` means ready for TunnelBookAI ingest processing, not canonical evidence.

The source-level manifest contains `document_id`, title, document type, source class, authority tier, relevance status/score, broad topics, producer identity, acquisition status/path/SHA-256, provenance, `paper_crawler_status`, and `tunnelbookai_status`.

No current book chapter is required or emitted. Historical `primary_section` and `book_sections` may exist only within a `legacy` object in internal migration artifacts and are ignored by relevance, routing, coverage, acceptance, and export.

PaperCrawler owns discovery, safe public acquisition, original preservation, source identity, broad topics, deduplication, provenance and checksum audit. TunnelBookAI owns Docling/full extraction, canonical Markdown, document structure, chunking, chunk-level topics/evidence, current-book taxonomy, RAG, citation and writing.

Presentation platforms are discovery provenance, never producers. Producer authority comes from an attributable author or institution. Public originals are preferred; inaccessible content remains `METADATA_ONLY` or `NO_PUBLIC_FULLTEXT`.
