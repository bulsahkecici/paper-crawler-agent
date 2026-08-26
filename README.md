# Paper Crawler Agent

TunnelBookAI için **kaynak keşfi → indirme/snapshot → sınıflandırma → audit → handoff** hazırlayan ayrı bir staging projesidir.

## Temel sınır

PaperCrawler canonical TunnelBookAI corpus'unu oluşturmaz. Çıktıları `READY_FOR_HANDOFF` seviyesine getirir. TunnelBookAI daha sonra SHA256 doğrulaması, full-text/Docling dönüşümü, kalite audit'i, final bölüm sınıflandırması ve evidence gate uyguladıktan sonra corpus'a ingest eder.

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
Ambiguous? → local Qwen reviewer
   ↓
classification_index.jsonl
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

Sınıflandırma eksenleri:

- `document_type`
- `source_class`
- `authority_tier`
- `book_sections`
- `topics`
- `classification_confidence`
- `route_path`
- `classification_status`

## 5. TunnelBookAI handoff

```bash
python handoff_export.py
```

Çıktı:

```text
tunel_makaleleri/exports/TunnelBookAI_Source_Pack/
├── 00_registry/
│   ├── manifest.jsonl
│   ├── checksums.sha256
│   └── handoff_contract.json
├── 01_originals/
│   ├── A_OFFICIAL/
│   ├── B_STANDARDS_GUIDELINES/
│   ├── C_ACADEMIC/
│   ├── D_REPORTS_BOOKS/
│   └── E_NEWS_CASES/
└── 99_audit/
    └── handoff_audit.json
```

Web kaynaklarında normalize edilmiş `source.md` ana handoff kaynağıdır; mevcutsa ham `source_raw.html` da ek asset olarak pakete alınır ve SHA256 listesine yazılır.

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
- `READY_FOR_HANDOFF`, **kitapta kanıt olarak onaylandı** anlamına gelmez.

## Test

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub Actions Python 3.11 ve 3.12 için aynı test paketini çalıştıracak şekilde tanımlanmıştır.
