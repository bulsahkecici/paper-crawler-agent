# PaperCrawler v1.0.0

TunnelBookAI için **kaynak keşfi → indirme/snapshot → sınıflandırma → coverage audit → gap search → handoff** hazırlayan ayrı bir staging projesidir.

## Temel sınır

PaperCrawler internetten ve açık kaynak sistemlerinden tünelle ilgili kaynakları
keşfeder, güvenli acquisition yapar, provenance ve byte SHA256 üretir,
bibliographic dedup uygular, **provisional** relevance/section classification ve
coverage-gap analizi yapar ve doğrulanabilir bir `READY_FOR_HANDOFF` paketi
üretir.

PaperCrawler **yapmaz**: full content conversion, final Docling pipeline,
Word/PPTX/XLSX dönüştürme, final OCR/vision, final image extraction, final corpus
section classification, final evidence evaluation, canonical corpus ingest. Bu
görevlerin tamamı **TunnelBookAI Unified Ingest Engine**'e aittir.

Üç kural handoff boundary'de kesindir:

```text
READY_FOR_HANDOFF   ≠  CANONICAL_CORPUS
LIGHT_PDF_TEXT      ≠  FULL_TEXT
PAPERCRAWLER SECTION =  PROVISIONAL  (final section = TunnelBookAI)
```

`METADATA_REFERENCE` kayıtları (fiziksel içeriği olmayan resmî/bibliyografik
referanslar) `READY_FOR_HANDOFF` sayısına **dahil edilmez**; ayrı raporlanır.

## Nihai mimari

```text
External sources
      ↓
PaperCrawler
      ↓
READY_FOR_HANDOFF
      ↓
──────────────  HANDOFF CONTRACT (docs/handoff_contract.md)  ──────────────
      ↓
TunnelBookAI  incoming/crawler
      ↓
Unified Ingest Engine
      ↓
Full conversion / Docling
      ↓
Page snapshots · image extraction · OCR/vision · table extraction
      ↓
Global content dedup
      ↓
Final section classification
      ↓
Evidence evaluation
      ↓
Corpus Quality Gate      (TunnelBookAI'a ait; PaperCrawler bu ismi sahiplenmez)
      ↓
corpus/staging → corpus/canonical
```

`corpus_quality_gate` adı ve canonical corpus yazımı TunnelBookAI'a aittir.
PaperCrawler tarafındaki fail-closed gate `handoff_quality_gate.py`'dir ve
`audit/handoff_quality_gate.json` yazar (`audit/corpus_quality_gate.json` yalnız
deprecated alias'tır).

## Ücretli arama API'si yok

Keşif katmanı şu ücretsiz/açık mekanizmaları kullanır:

- OpenAlex
- Crossref
- Europe PMC
- DOAJ
- arXiv
- OpenAIRE Graph API
- CORE API (best-effort; ücretsiz anahtar opsiyonel)
- KGM / FHWA / PIARC / ITA gibi kurumsal seed siteleri
- sitemap.xml + robots.txt
- OAI-PMH (DergiPark journal endpoint'leri dahil)
- Common Crawl URL index (yalnızca güvenilir domain genişletme)
- bulunan güvenilir kamu / üniversite repository domainlerini dinamik seed'e yükseltme

Google Scholar / ResearchGate otomatik scraper olarak kullanılmaz. ResearchGate ve benzeri erişim kısıtlı domainlerden otomatik PDF acquisition yapılmaz.

## Akış

```text
Book taxonomy
   ↓
Free discovery
   ↓
discovered_records.jsonl
   ↓
PDF download / web snapshot
   ↓
discovery_catalog.jsonl
   ↓
Rules + local embedding
   ↓
Only explicitly ambiguous? → local Qwen reviewer
   ↓
deterministic decision router
   ├─ AUTO_HANDOFF
   ├─ RETRY_ACQUISITION
   ├─ RECLASSIFY
   ├─ AUTO_REJECT
   └─ MANUAL_REVIEW
   ↓
Coverage target met?
   ├─ yes → handoff
   └─ no  → focused gap search → reclassify
   ↓
READY_FOR_HANDOFF export
   ↓
TunnelBookAI_Source_Pack
```

## 1. Kurulum

```bash
pip install -r requirements.txt
```

Yerel AI kullanmak istenirse LM Studio / Ollama / vLLM yalnızca loopback (`127.0.0.1`, `localhost`, `::1`) üzerinde OpenAI-compatible endpoint sağlamalıdır. Cloud fallback yoktur.

Yerel LLM sağlayıcısı Qwen'e bağlı değildir. Varsayılan modeller otomatik bulunur;
isterseniz sunucu ve modeli açıkça seçebilirsiniz:

```bash
python prepare_tunnelbookai_handoff.py \
  --llm-server http://127.0.0.1:1234/v1 \
  --llm-model qwen3.6-35b-a3b-mlx \
  --embedding-server http://127.0.0.1:1234/v1 \
  --embedding-model text-embedding-nomic-embed-text-v1.5
```

Pipeline aşamaları, sorgular, kaynak sonuçları, zaman aşımı/atlama kararları,
acquisition, sınıflandırma, coverage-gap ve handoff durumu terminale anlık yazılır.

Pipeline varsayılan olarak `audit/pipeline_state.json` üzerinden güvenli biçimde
devam eder. Tamamlanmış aşamalar yeniden çalıştırılmaz:

```bash
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --resume
```

Yeni bir checkpoint başlatmak (mevcut PDF ve kullanıcı verilerini silmeden) için
`--fresh-run` kullanılabilir. Final handoff yalnız `audit/handoff_quality_gate.json`
kararı ve `00_registry/handoff_manifest.jsonl` üzerinden tüketilmelidir.

Handoff paketi başka bir dizine (ör. TunnelBookAI `incoming/crawler/<release_id>`)
yazılabilir:

```bash
python prepare_tunnelbookai_handoff.py \
  --destination ~/Projects/tunnelbookai_v1/incoming/crawler/RELEASE_2026_09_01
```

PaperCrawler tunnelbookai_v1 iç yapısını bilmez; yalnızca destination path'e
self-contained paket yazar.

## 2. Ücretsiz kaynak keşfi

```bash
python discover_sources.py --max-queries 60 --per-source 10
```

Sadece keşif/metadata istiyorsanız:

```bash
python discover_sources.py --discover-only
```

Dinamik sitemap/Common Crawl genişlemesini kapatmak için:

```bash
python discover_sources.py --no-dynamic-expansion
```

Üretilen ana dosyalar:

```text
tunel_makaleleri/
├── discovered_records.jsonl
├── discovery_catalog.jsonl
├── dynamic_seeds.json
├── discovery_sources/
│   ├── pdfs/
│   └── web/
└── audit/
    └── discovery_audit.json
```

## 3. Klasik akademik harvest (opsiyonel / birlikte kullanılabilir)

```bash
python paper_crawler_agent.py --harvest-only --limit 800
```

`classify_catalog.py` klasik `catalog.json` ile `discovery_catalog.jsonl` kayıtlarını tek sınıflandırma indeksinde birleştirir.

## 4. Sınıflandırma

Yerel embedding + gerekiyorsa Qwen reviewer:

```bash
python classify_catalog.py
```

Sadece deterministik kurallar:

```bash
python classify_catalog.py --rules-only
```

Sınıflandırma eksenleri (hepsi PaperCrawler seviyesinde **provisional**'dır):

- `provisional_document_type` (`document_type`)
- `provisional_source_tier` / `authority_tier`
- `provisional_primary_section` + `provisional_secondary_sections` (`book_sections`)
- `topics`
- `provisional_section_confidence` (`classification_confidence`)
- `route_path`
- `provisional_classification_status` (`classification_status`)
- `handoff_candidate`
- `crawler_evidence_level` ∈ {`ABSTRACT`, `LIGHT_PDF_TEXT`, `WEB_SNAPSHOT_TEXT`, `TITLE_METADATA_ONLY`, `ORIGINAL_ACQUIRED`} — asla `FULL_TEXT`/`PDF_EXTRACT` değil

Handoff manifestindeki her kayıt `final_primary_section: null`,
`final_section_status: NOT_EVALUATED`, `final_evidence_status: NOT_EVALUATED`
taşır. Final section/evidence kararını TunnelBookAI normalize edilmiş tam içerik
üzerinden verir.

Coverage beş ayrı düzeyi raporlar: `raw_matches`, `relevant_matches`,
`acquired_matches`, `handoff_candidates` ve `authority_weighted_score`. Gap search
`handoff_candidates` değerini kullanır; TunnelBookAI final evidence kararı coverage'ı
etkilemez ve metadata-only URL'ler coverage'ı yapay olarak dolduramaz.

Qwen varsayılan sınıflandırıcı değildir. Yalnızca rule/embedding anlaşmazlığı,
yakın top-1/top-2 skorları, güven ambiguity bandı, çözülemeyen belge türü veya
yüksek değerli `PROBABLE` kaynak arbitrajında çağrılır. Her çağrı
`llm_review.trigger_reasons` ile açıklanır.

## 5. Coverage-gap ikinci turu

```bash
python gap_discovery.py
python classify_catalog.py
```

`config/coverage_targets.yaml` her kitap bölümü için discovery hedeflerini tutar. Bir bölüm hedefin altındaysa yalnızca o bölümün taxonomy terimleriyle odaklı ikinci arama yapılır.

Örneğin `4.3.5 Tünel Yaşam Döngü Maliyetleri` zayıf kalırsa LCC/cost sorguları; `5.7.2 Enerji Maliyetini Azaltma` zayıf kalırsa ventilation/lighting/energy sorguları çalışır.

## 6. TunnelBookAI handoff

```bash
python handoff_export.py
```

Çıktı:

```text
tunel_makaleleri/exports/TunnelBookAI_Source_Pack/
├── 00_registry/
│   ├── manifest.jsonl
│   ├── handoff_manifest.jsonl        # authoritative consumer manifest (schema 2.0)
│   ├── checksums.sha256
│   └── handoff_contract.json         # producer/consumer responsibilities + semantic rules
├── 01_originals/
│   ├── A_OFFICIAL/  B_STANDARDS_GUIDELINES/  C_ACADEMIC/  D_REPORTS_BOOKS/  E_NEWS_CASES/
└── 99_audit/
    ├── handoff_audit.json
    ├── handoff_quality_gate.json     # GO / CONDITIONAL_GO / NO_GO
    ├── decision_summary.json
    ├── review_queue.jsonl
    ├── retry_acquisition.jsonl
    ├── metadata_references.jsonl     # reference metadata, NOT in READY_FOR_HANDOFF
    ├── reclassify_queue.jsonl
    └── rejected_manifest.jsonl
```

Web kaynaklarında `source_representation.original_or_raw` (mümkünse ham
`source_raw.html`) authoritative yakalanmış temsildir; `crawler_normalized`
PaperCrawler'ın **provisional** normalizasyonudur ve final corpus Markdown'ı
sayılmaz — onu TunnelBookAI kendi ingest pipeline'ında üretir. Ham HTML
mevcutsa ek asset olarak pakete alınır ve SHA256 listesine yazılır. DOI,
publisher, source URL, discovery source/query gibi provenance alanları handoff
metadata'sında korunur.

İnsanlar yalnız `audit/review_queue.csv` dosyasını inceler. Eksik kaynaklar
`retry_acquisition.csv`, açık sınıflandırma çelişkileri `reclassify_queue.csv`,
deterministik retler `rejected_manifest.csv` içindedir. `source_missing` doğrudan
insan kuyruğuna gitmez. `insufficient_evidence_level` PaperCrawler ret nedeni
değildir; exported metadata `final_evidence_status: NOT_EVALUATED` taşır.

## Tek komut

Tam pipeline:

```bash
python prepare_tunnelbookai_handoff.py \
  --output-dir tunel_makaleleri \
  --resume \
  --embedding-server http://127.0.0.1:1234/v1 \
  --embedding-model text-embedding-nomic-embed-text-v1.5 \
  --llm-server http://127.0.0.1:1234/v1 \
  --llm-model qwen3.6-35b-a3b-mlx
```

Bu komut sırasıyla:

1. ücretsiz discovery,
2. acquisition/snapshot,
3. classification,
4. coverage-gap pass,
5. yeniden classification,
6. handoff export

çalıştırır.

Local AI istemiyorsanız:

```bash
python prepare_tunnelbookai_handoff.py --rules-only
```

Gap pass istemiyorsanız:

```bash
python prepare_tunnelbookai_handoff.py --skip-gap-pass
```

Sınırlı üretim modları:

```bash
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --classify-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --handoff-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --review-export-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --retry-acquisition-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --gap-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --rules-only
python prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --dry-run
```

Her full run `audit/runs/RUN_YYYYMMDD_HHMMSS/` altında config hash'leri, git
commit/branch, yerel model seçimi, provider health, coverage, dedup, decision ve
run summary kayıtlarını tutar. `--resume` kaynakları silmez; tamamlanmış checkpoint
aşamalarını atlar. Classification semantic'i değiştiğinde `--classify-only`
affected sidecar ve current index'i deterministik olarak yeniler; fiziksel sidecar
sayısı current-record sayısı olarak raporlanmaz.

## Dedup kimliği

Aynı akademik eser için öncelik DOI tabanlı work identity'dir. Aynı DOI farklı API/landing/PDF URL'lerinden gelirse tek kayıt seçilir ve daha zengin/direct-PDF kayıt tercih edilir. DOI yoksa URL kayıtları acquisition aşamasına kadar ayrı kalabilir; indirilen içeriklerde SHA256 kesin byte-level duplicate kontrolüdür. Bu yaklaşım, yalnız URL benzerliğine bakıp iki farklı sürümü yanlışlıkla aynı eser saymaktan kaçınır.

## Evidence önceliği

Genel politika:

```text
A1  Türkiye resmi/birincil kaynak
A2  Uluslararası resmi teknik kaynak
A3  Standart/şartname
B1  Review/systematic review
B2  Hakemli araştırma makalesi
C1  Doktora tezi
C2  Yüksek lisans tezi
D1  Conference/preprint
D2  Teknik rapor
E   Haber/genel web
F   Discovery-only
X   AI note (evidence değil)
```

Bu sıra konuya göre `source_policy.yaml` ile override edilebilir.

## Güvenlik

- LLM/embedding yalnızca loopback.
- Web acquisition yalnızca public IP hedeflerine izin verir.
- Redirect zincirinin her adımı tekrar public-IP kontrolünden geçer.
- `robots.txt` kurallarına uyulur.
- ResearchGate, Academia.edu ve belirlenmiş kapalı/paywall domainlerinden otomatik acquisition yapılmaz.
- PDF stream edilir, boyut sınırı uygulanır ve SHA256 indirme sırasında hesaplanır.
- Common Crawl genel arama motoru gibi kullanılmaz; yalnızca önceden güvenilir bulunan domain içinde URL genişletme yapar.
- `READY_FOR_HANDOFF`, **kitapta kanıt olarak onaylandı** anlamına gelmez.
- Ücretli API veya cloud LLM fallback yoktur; opsiyonel local model endpoint'leri
  loopback dışına çıkamaz.

## Test

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub Actions Python 3.11 ve 3.12 için aynı test paketini çalıştıracak şekilde tanımlanmıştır.
