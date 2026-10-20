# KAP Scraper

[KAP](https://www.kap.org.tr) bildirimlerini model eğitimi için toplar. Yalnızca `companies.txt` içindeki şirketlerle ilgili bildirimleri alır ve her birini sayfa metni, alanları, mali tabloları ve ekleriyle birlikte JSON Lines dosyasına tek satır olarak yazar.

Üç şekilde çalışır:

| | Komut | Ne yapar |
|---|---|---|
| Canlı | `python kap_watch.py` | Yeni bildirimleri yayımlandıkları anda yakalar |
| Geçmiş | `python kap_history.py --start 01.01.2025 --end 31.12.2025` | Tarih aralığındaki bildirimleri çeker |
| AWS | `bash aws/run.sh 01.12.2022 19.10.2026 8 40` | Geçmişi AWS'de paralel görevlerle çeker |

> Resmî değildir; KAP sitesinin kendi kullandığı, belgelenmemiş uç noktaları okur.

## Nasıl çalışır

1. **Liste.** KAP'ın bildirim listesi çekilir; yalnızca listedeki şirketleri (kendi bildirimi ya da "ilgili hisse" olarak) ilgilendirenler kalır.
2. **Sayfa.** Bildirim sayfasının metni, alanları ve tabloları ayrıştırılır. Finansal raporlarda mali tablolar sayısal değerler olarak çıkarılır.
3. **Ekler.** Bütün ekler indirilir; yalnızca Türkçesi bulunan belgenin İngilizcesi atlanır. PDF'lerin metni ve tabloları çıkarılır, taranmış sayfalar ve resimler OCR ile okunur.
4. **Kayıt.** Sayfası ve ekleri biten bildirim dosyaya eklenir. Dosyadaki bildirimler sonraki çalıştırmalarda atlanır, böylece yarım kalan iş aynı komutla devam eder.

## Kayıt alanları

| Alan | İçerik |
|---|---|
| `disclosure_index`, `publish_date`, `stock_code`, `title`, ... | Listeden gelen bilgiler |
| `text`, `fields`, `tables` | Bildirim sayfasının metni, etiket-değer alanları ve tabloları |
| `financial_statements` | Finansal raporların mali tabloları |
| `attachments` | Her ek: `file_name`, `url`, `file_type`, `page_count`, `ocr_pages`, `text` (`[Page N]` bölümleriyle), `tables` |

Ekin `file_type` değeri `pdf` ya da resim türüdür. `skipped` Türkçesi olduğu için atlanan İngilizce kopyadır, `unsupported` PDF ya da resim olmayan dosyadır, `error` hiç okunamayan dosyadır (nedeni `error` alanında). Bazı PDF sayfaları PyMuPDF'in tablo bulucusunu çökertir; o dosya sayfa sayfa yeniden okunur ve tablosu alınamayan sayfalar `table_errors` alanında listelenir. `skipped_pages` eski kayıtlarla aynı düzen için duran, hep 0 olan bir alandır.

## Yerel kurulum (Windows)

Python 3.14 ve Tesseract gerekir. Bilgisayar saati Türkiye saatinde olmalıdır.

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR
mkdir tessdata
copy "C:\Program Files\Tesseract-OCR\tessdata\eng.traineddata" tessdata\
copy "C:\Program Files\Tesseract-OCR\tessdata\osd.traineddata" tessdata\
curl.exe -L -o tessdata\tur.traineddata https://github.com/tesseract-ocr/tessdata_best/raw/main/tur.traineddata
copy .env.example .env   # TESSERACT_CMD ve TESSDATA_PREFIX yollarını doldurun
```

Çıktı `data/disclosures.jsonl` dosyasıdır. Canlı izleyici ile geçmişi aynı anda çalıştırmayın; ikisi aynı dosyaya yazar. İkisi de Ctrl+C ile durur.

## Docker

Tesseract, Türkçe OCR dosyası ve Türkiye saati imajın içindedir. `docker compose up -d --build` canlı izleyiciyi arka planda başlatır; çıktı yine `data/` klasörüne yazılır. `docker compose logs -f` çıktıyı gösterir, `docker compose stop` durdurur. Geçmiş için: `docker compose run --rm kap python kap_history.py --start ... --end ...`.

## AWS

Geçmişi Frankfurt'ta (eu-central-1) ECS Fargate görevleriyle çeker. KAP çok istek gönderen IP'yi engellediği için istekler squid proxy görevleri üzerinden sırayla gider. Her proxy'den aynı anda en fazla 8 istek geçer ve engellenen proxy 4 dakika dinlenir. Git Bash, AWS CLI (`aws login`) ve Docker Desktop gerekir.

| Komut | Ne yapar |
|---|---|
| `bash aws/setup.sh` | İlk kurulum: S3 bucket, imajlar, roller, cluster. Kod değişince de çalıştırın |
| `bash aws/run.sh BAŞLANGIÇ BİTİŞ [GÖREV=8] [PROXY=16]` | Proxy'leri ve aralığın her parçası için bir görev başlatır |
| `bash aws/resume.sh BAŞLANGIÇ BİTİŞ [VCPU]` | Çöken ya da durdurulan bir parçayı çalışan proxy'lerle yeniden başlatır |
| `bash aws/status.sh` | Çalışan görevler ve S3'teki dosyalar |
| `bash aws/stop.sh [proxy]` | Görevleri durdurur; dosyalar önce S3'e yazılır |
| `bash aws/teardown.sh` | S3 dışındaki her şeyi siler |

Her görev kendi parçasını `s3://kapscrapper-<hesap>-eu-central-1/disclosures/<ilk gün>_<son gün>.jsonl` dosyasına yazar ve 5 dakikada bir yükler. Sonunda KAP listelerini yeniden çekip eksik kalanları 5 dakika arayla 3 tura kadar yeniden dener; logun sonunda `Complete: ...` ya da eksiklerle `Incomplete: ...` yazar. Loglar: `aws logs tail /ecs/kapscrapper --follow`.

## Bilinmesi gerekenler

- **İstek sınırı.** KAP birkaç dakikada ~100 istekten sonra IP'yi birkaç dakika engeller (HTTP 429/403/503 ya da yanıtsız kesilen bağlantı). Proxy'siz çalışırken herkes 4 dakika bekler.
- **Yavaş indirmeler.** KAP ekleri bağlantı başına ~10 KB/s ile gönderir; süreyi büyük raporlar belirler.
- **Çöken dosyalar.** Her ek ayrı bir süreçte okunur; okuyucu çökerse yalnızca o süreç ölür.
- **Ham sayfalar saklanmaz.** Ayrıştırıcı değişirse eski kayıtlar kendiliğinden yenilenmez.
- **KAP değişebilir.** Ayrıştırıcılar KAP'ın Ekim 2026'daki sayfa düzenine göre yazıldı.

## Dosyalar

```text
companies.txt        Bildirimleri toplanan şirketler (her satırın ilk kelimesi hisse kodu)
kap_watch.py         Canlı izleyici
kap_history.py       Tarih aralığındaki geçmiş bildirimler
kap_aws.py           AWS'de geçmiş: kap_history'nin işini yapar, sonucu S3'e yazar
kap_pipeline.py      Bir bildirimi baştan sona işleyen ortak kod
kap_disclosures.py   Bildirim listesi
kap_details.py       Bildirim sayfası
kap_financials.py    Mali tablolar
kap_attachments.py   Eklerden metin ve tablo, OCR, okunacak eklerin seçimi
kap_output.py        Kaydın düzeni ve JSON Lines dosyası
kap_http.py          Yeniden deneme, engel beklemesi, proxy sırası
aws/                 AWS betikleri ve squid proxy imajı
```
