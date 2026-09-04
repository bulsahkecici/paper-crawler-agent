# PaperCrawler

PaperCrawler is not a book-writing system. It is a zero-paid-API technical source discovery and acquisition system specializing in tunnel engineering.

> PaperCrawler classifies sources.
>
> TunnelBookAI classifies chunks and knowledge.

PaperCrawler discovers academic, governmental, road-authority, standards, professional, repository, web, and presentation sources. It decides whether a source is genuinely relevant to civil/transport/underground tunnel engineering; identifies its document type, producer, source class and authority tier; assigns stable broad topics; safely acquires public originals; preserves provenance and SHA-256; deduplicates; audits; and builds a TunnelBookAI source pack.

It does not load the current book table of contents, assign chapter numbers, perform Docling conversion, chunk documents, approve evidence, run book RAG, or write the book. A future table-of-contents replacement therefore does not require PaperCrawler reclassification.

## Core flow

```text
Discovery → tunnel relevance → acquisition → document type → producer identity
→ authority tier → broad topics → dedup → SHA/provenance/audit → handoff
```

TunnelBookAI owns full extraction, canonical Markdown, document structure, chunking, chunk-level topics and evidence, current-book taxonomy matching, RAG, citation, and writing.

## Classification and discovery

- Relevance: `STRONG`, `PROBABLE`, `WEAK`, `IRRELEVANT`.
- Decisions: `AUTO_HANDOFF`, `RETRY_ACQUISITION`, `RECLASSIFY`, `AUTO_REJECT`, `MANUAL_REVIEW`, `METADATA_REFERENCE`.
- Topics are controlled, broad tunnel-engineering labels without chapter numbers.
- Authority is separate from document type. Upload platforms never confer authority.
- Old `primary_section` and `book_sections` values may survive only in `legacy`; no current decision reads them.

Free scholarly discovery uses OpenAlex, Crossref, Europe PMC, DOAJ, arXiv, OpenAIRE, best-effort CORE, OAI-PMH, institutional public search, sitemaps, robots-compliant web discovery, and bounded Common Crawl discovery. Official sources are configured in `config/institutional_sources.yaml`; multilingual queries are in `config/topic_queries.yaml`.

Presentation discovery is metadata-first and public-only. The named author or institution determines authority. When an original institutional copy is resolved, it is preferred and the platform URL remains discovery provenance. No login, paywall, CAPTCHA, anti-bot, or download-control bypass is permitted.

PDF, HTML, PPT/PPTX, DOC/DOCX, XLS/XLSX, CSV, TXT, Markdown, and public ZIP originals are preserved. Optional HTML Markdown is only a lightweight snapshot; heavy canonical conversion belongs to TunnelBookAI.

## Run

```bash
python3 prepare_tunnelbookai_handoff.py \
  --output-dir tunel_makaleleri \
  --embedding-server http://127.0.0.1:1234/v1 \
  --embedding-model text-embedding-baai-bge-m3-568m \
  --llm-server http://127.0.0.1:1234/v1 \
  --llm-model qwen3.6-35b-a3b-mlx
```

Rules-only reclassification without redownloading:

```bash
python3 classify_catalog.py --output-dir tunel_makaleleri --rules-only
```

Safe legacy migration (dry run, then write):

```bash
python3 migrate_book_agnostic.py --output-dir tunel_makaleleri
python3 migrate_book_agnostic.py --output-dir tunel_makaleleri --write
```

Focused discovery supports autonomous broad topics or a TunnelBookAI request:

```bash
python3 gap_discovery.py --output-dir tunel_makaleleri
python3 gap_discovery.py --output-dir tunel_makaleleri --request TunnelBookAI_Discovery_Request.json
```

## Handoff v2.0

```text
tunel_makaleleri/exports/TunnelBookAI_Source_Pack/
├── 00_registry/
├── 01_originals/
└── 99_audit/
```

Each accepted source directory contains its original plus `metadata.json` and `classification.json`. The consumer manifest requires no book chapter and exposes relevance, topics, producer identity, acquisition, provenance, PaperCrawler state, and `tunnelbookai_status: NOT_INGESTED`.

Topic coverage is informational and never an arbitrary GO target. TunnelBookAI determines missing book evidence and may send a discovery request without revealing a chapter number.
