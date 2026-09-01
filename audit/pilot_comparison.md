# Pilot Karşılaştırması — Baseline ve Hardening

Bu belge, sertleştirme öncesi pilot ile aynı kapsamda yapılacak doğrulama
çalışması için karşılaştırma çerçevesidir. Yeni sütundaki değerler canlı pilot
çalıştırılmadan doldurulmaz; bu nedenle aşağıda **çalıştırılmadı** olarak
işaretlenmiştir.

| Ölçüm | Önceki pilot | Hardening pilotu |
| --- | ---: | ---: |
| Discovery unique | 108 | Çalıştırılmadı |
| PDF indirilen | 44 | Çalıştırılmadı |
| HTML snapshot | 11 | Çalıştırılmadı |
| Metadata only | 53 | Çalıştırılmadı |
| Ready for handoff | 35 | Çalıştırılmadı |
| Qwen review oranı | Ölçülmemiş | Çalıştırılmadı |
| Relevance reddi | Ölçülmemiş | Çalıştırılmadı |
| DNS / SSRF ayrımı | Ölçülmemiş | Çalıştırılmadı |

## Hardening ile doğrulanacak davranışlar

- Tünel bağlamı olmayan OAI-PMH ve akademik kayıtlar acquisition öncesinde
  elenir; güçlü tünel-mühendisliği kayıtları korunur.
- Yerel veya özel IP'ye yönlenen URL'ler güvenlik nedeniyle engellenir. DNS
  çözümsüzlüğü ise ayrı bir `DNS_FAILURE` olarak raporlanır.
- Acquisition sonuçları HTTP, robots, timeout, içerik türü, boyut ve PDF
  doğrulama nedenleriyle ayrıştırılır.
- Qwen yalnızca belirsiz, çelişkili veya yüksek değerli `PROBABLE` kayıtlarda
  çağrılır. Güçlü kural+embedding uyumunda çağrılmaz.
- Handoff'a yalnız uygun relevance ve bütünlük kontrolünden geçen kaynaklar
  girer; inceleme ve ret gerekçeleri ayrı JSONL manifestlerinde kalır.

## Canlı pilot komutu

Yerel model sunucusu çalışıyorken, ağ erişimi olan bir terminalde aşağıdaki
komutu kullanın:

```bash
python3 prepare_tunnelbookai_handoff.py --output-dir tunel_makaleleri --max-queries 12 --per-source 5 --embedding-server http://127.0.0.1:1234/v1 --embedding-model text-embedding-nomic-embed-text-v1.5 --llm-server http://127.0.0.1:1234/v1 --llm-model qwen3.6-35b-a3b-mlx
```

Sonuçlar şu dosyalardan karşılaştırılmalıdır:

- `tunel_makaleleri/audit/discovery_audit.json`
- `tunel_makaleleri/audit/classification_audit.json`
- `tunel_makaleleri/audit/run_summary.md`
- `tunel_makaleleri/exports/TunnelBookAI_Source_Pack/99_audit/review_queue.jsonl`
- `tunel_makaleleri/exports/TunnelBookAI_Source_Pack/99_audit/rejected_manifest.jsonl`
