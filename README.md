# KAP Scraper

[KAP](https://www.kap.org.tr) bildirimlerini toplayan bir veri toplayıcı. Model eğitimi için her bildirimi metni, mali tabloları ve önemli ekleriyle birlikte tek bir dosyada toplar. İki şekilde çalışır: yeni bildirimleri yayımlandıkları anda yakalar (**canlı**) ya da verilen tarih aralığındaki bildirimleri çeker (**geçmiş**).

> Resmî değildir. KAP sitesinin kendi kullandığı, belgelenmemiş uç noktaları okur. İstek sıklığını düşük tutun.

## Nasıl çalışır

Her bildirim için aynı adımlar izlenir:

1. **Liste.** Günün bildirim listesi KAP'tan çekilir. Fon bildirimleri hariçtir.
2. **Sayfa.** Bildirimin sayfası çekilir. Metni, alanları ve tabloları ayrıştırılır.
3. **Mali tablolar.** Finansal raporlarda bilanço, gelir tablosu, nakit akış ve özkaynak tabloları sayısal değerler olarak çıkarılır.
4. **Ekler.** Sadece değerli türlerin ekleri indirilir ve metni çıkarılır. Taranmış belgeler OCR ile okunur. Ayrıntılar: [Hangi ekler okunur](#hangi-ekler-okunur).
5. **Kayıt.** Her şey `data/kap.duckdb` veritabanına yazılır. Sayfası ve ekleri tamamlanan bildirim `data/disclosures.jsonl` dosyasına bir satır olarak eklenir.

## Kurulum (Windows)

Gereksinimler: Python 3.14 ve Tesseract OCR. Bilgisayar saati Türkiye saatinde olmalı, çünkü KAP saatleri bilgisayarın saatiyle karşılaştırılır.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

winget install UB-Mannheim.TesseractOCR
mkdir tessdata
copy "C:\Program Files\Tesseract-OCR\tessdata\eng.traineddata" tessdata\
copy "C:\Program Files\Tesseract-OCR\tessdata\osd.traineddata" tessdata\
curl.exe -L -o tessdata\tur.traineddata https://github.com/tesseract-ocr/tessdata_best/raw/main/tur.traineddata

copy .env.example .env
```

Ardından `.env` dosyasında iki yolu doldurun:

- `TESSERACT_CMD`: `tesseract.exe` dosyasının tam yolu, genelde `C:\Program Files\Tesseract-OCR\tesseract.exe`.
- `TESSDATA_PREFIX`: projedeki `tessdata` klasörünün tam yolu.

## Kullanım

Canlı ve geçmiş aynı anda çalıştırılamaz. Veritabanını aynı anda yalnızca bir süreç kullanabilir. İkisi de Ctrl+C ile durdurulur.

### Canlı

```powershell
python kap_watch.py
```

KAP'ı 15 saniyede bir kontrol eder ve her yeni bildirimi hemen işler. Başladığı andan 10 dakika öncesine kadar yayımlananları da alır. Açık kaldığı sürece çalışır. Kapalı kaldığı aralığı sonradan geçmiş komutuyla doldurabilirsiniz.

### Geçmiş

```powershell
python kap_history.py --start 01.01.2025 --end 31.12.2025
```

- **Tarihler.** Tarihler `GG.AA.YYYY` biçimindedir ve iki gün de aralığa dahildir. Günler bitişten başlangıca doğru işlenir.
- **Devam etme.** Durdurulursa aynı komutu yeniden çalıştırın; tamamlananları atlar, kaldığı yerden devam eder. Hata alan sayfa ve ekler de o zaman yeniden denenir.
- **Bitiş.** Aralık bitince kendiliğinden kapanır.
- **Süre.** KAP'ın istek sınırı yüzünden yavaştır. Bir sayfa 5 ile 9 saniye sürer. İş günü başına yaklaşık 330 bildirim olduğu için bir gün yarım saat ile bir saat arası sürer. Uzun aralıkları birkaç güne bölerek çalıştırabilirsiniz.

### Bakım komutları

| Komut | Ne yapar |
|---|---|
| `python kap_output.py` | JSON Lines dosyasını, detay dosyalarını ve Excel özetini veritabanından baştan yazar. KAP'a istek atmaz |
| `python kap_details.py --reparse` | Saklı sayfaları KAP'a gitmeden yeniden ayrıştırır. Ayrıştırıcı değiştiğinde kullanılır |
| `python kap_financials.py --all` | Saklı finansal raporlardan mali tabloları yeniden çıkarır |
| `python kap_attachments.py` | Yarım kalan ekleri tamamlar |

Yeniden ayrıştırmadan sonra JSON Lines dosyasını güncellemek için `python kap_output.py` komutunu çalıştırın.

## Çıktılar

Hepsi `data/` klasöründedir:

| Dosya | İçerik |
|---|---|
| `disclosures.jsonl` | **Eğitim verisi.** Her satır bir bildirimin tam kaydıdır |
| `kap.duckdb` | Her şeyin tutulduğu veritabanı, ham sayfa HTML'leri dahil |
| `json/YYYY-AA-GG/` | Her bildirim için ayrı bir JSON dosyası. Canlıda saniyeler içinde hazır olur |
| `details/YYYY-AA-GG/` | Her bildirim için göz atmaya uygun bir Markdown dosyası |
| `disclosures.xlsx` | En yeni 5.000 bildirimin Excel özeti. Detay dosyalarına bağlantı içerir |

Bir bildirim, sayfası okunup ekleri de okunduğunda ya da atlandığında JSON Lines dosyasına bir kez eklenir. Okuma örneği: `pl.read_ndjson("data/disclosures.jsonl")`.

Bir kaydın alanları:

| Alan | İçerik |
|---|---|
| `disclosure_index` | KAP'ın bildirim numarası |
| `publish_date`, `stock_code`, `company_title` | Yayın zamanı, hisse kodu, şirket |
| `title`, `summary` | Bildirim türü, örneğin "Özel Durum Açıklaması (Genel)", ve özeti |
| `disclosure_class` | `ODA` özel durum açıklaması, `FR` finansal rapor, `DUY` duyuru, `DG` diğer |
| `text` | Sayfanın tam metni |
| `fields`, `tables` | Sayfadaki etiket-değer alanları ve tablolar |
| `financial_statements` | Finansal raporların mali tabloları. Her değerin XBRL kavramı, dönemi ve para birimi vardır |
| `attachments` | Ekler: adı, bağlantısı ve okunduysa `text` ile `tables`. Atlanan eklerde `file_type` `skipped` olur, `text` boştur |

## Hangi ekler okunur

Eklerin çoğu ya sayfada zaten yazılanın resmî belgesidir ya da faaliyet raporu gibi uzun, rutin raporlardır. Bu yüzden ekler sadece şu bildirim türlerinde okunur:

- **Özel Durum Açıklaması (Genel):** yatırımcı sunumları, finansal sonuç notları, beklenti güncellemeleri
- **Kredi Derecelendirmesi**
- **Değerleme Raporu**
- **Halka arz fiyatının varsayımlarına ilişkin rapor**
- **Fon kullanım raporu**

Bu türlerde de, aynı bildirimde Türkçesi bulunan bir belgenin İngilizcesi indirilmez. Dil, dosya adından tahmin edilir; emin olunamazsa ek okunur. Listeyi değiştirmek için `kap_attachments.py` içindeki `EXTRACT_TYPES` sabitini düzenleyin.

## Dosyalar

```text
kap_watch.py          Canlı izleyici
kap_history.py        Tarih aralığındaki geçmiş bildirimler
kap_disclosures.py    Bildirim listesini çeker
kap_details.py        Bildirim sayfasını çeker ve ayrıştırır
kap_financials.py     Finansal raporlardan mali tabloları çıkarır
kap_attachments.py    Hangi eklerin okunacağına karar verir, ekleri indirir, metin çıkarır, OCR yapar
kap_output.py         Veritabanı yolu ve tüm çıktı dosyaları (JSON Lines, JSON, Markdown, Excel)
kap_http.py           KAP'ın istek sınırına karşı bekleme ve yeniden deneme
```

## Bilinmesi gerekenler

- **İstek sınırı.** KAP birkaç dakikada yaklaşık 100 istekten sonra geçici olarak engeller. Betikler bunu bekleyip kendiliğinden devam eder. Ekranda sık sık `KAP rate limit hit` görürseniz `kap_history.py` içindeki `REQUEST_DELAY` değerini artırın.
- **Yavaş indirmeler.** KAP ekleri bazen çok yavaş verir. 180 saniyede inmeyen ek bir sonraki çalıştırmada yeniden denenir. Eki inmeyen bildirim o zamana kadar JSON Lines dosyasına girmez.
- **Kilit hatası.** `Could not set lock on file ... kap.duckdb` hatası, veritabanının başka bir süreçte açık olduğunu gösterir. Önce o süreci durdurun.
- **Türkçe karakterler bozuksa** PowerShell'de `$env:PYTHONIOENCODING = "utf-8"` ayarlayın.
- **KAP değişebilir.** Ayrıştırıcılar KAP'ın Ekim 2026'daki sayfa düzenine göre yazıldı.
