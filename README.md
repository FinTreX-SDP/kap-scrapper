# KAP Scraper

[KAP](https://www.kap.org.tr) bildirimlerini toplayan bir veri toplayıcı. Yalnızca `companies.txt` dosyasındaki şirketlerle ilgili bildirimleri alır. Model eğitimi için her bildirimi metni, mali tabloları ve önemli ekleriyle birlikte tek bir dosyada toplar. İki şekilde çalışır: yeni bildirimleri yayımlandıkları anda yakalar (**canlı**) ya da verilen tarih aralığındaki bildirimleri çeker (**geçmiş**).

> Resmî değildir. KAP sitesinin kendi kullandığı, belgelenmemiş uç noktaları okur. İstek sıklığını düşük tutun.

## Nasıl çalışır

Her bildirim için aynı adımlar izlenir:

1. **Liste.** Bildirim listesi KAP'tan çekilir ve hemen süzülür. Yalnızca [listedeki şirketlerle](#şirket-listesi) ilgili bildirimler kalır; diğerleri veritabanına yazılmaz, sayfaları da hiç istenmez. Fon bildirimleri hariçtir.
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
- **Süre.** Liste ay ay, tek istekte çekilir. Asıl süreyi sayfalar alır: KAP'ın istek sınırı yüzünden bir sayfa, bekleme dahil ortalama 6 saniye sürer. Eylül 2026'da listedeki şirketlerle ilgili 1.210 bildirim vardı, yani bir ay yaklaşık 2 saat sürer. Ekler arka planda bu sırada okunur. Uzun aralıkları birkaç güne bölerek çalıştırabilirsiniz.

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
| `related_disclosure_oid` | Bu bildirimin devamı, güncellemesi ya da düzeltmesi olduğu önceki bildirimin `disclosure_id` değeri |
| `is_changed` | Güncellenen bildirimde `DUZENLENEN`, düzeltilen bildirimde `DUZELTILEN`, ilk bildirimde boş |
| `mkk_member_oid` | KAP'ın şirket kimliği. Hisse kodu değişse de aynı kalır |
| `term`, `has_multi_language_support` | Finansal raporun çeyreği; bildirimin İngilizcesi de varsa `Y` |
| `text` | Sayfanın tam metni |
| `fields`, `tables` | Sayfadaki etiket-değer alanları ve tablolar. Finansal raporlarda `fields` boştur, çünkü sayfadaki alanların hepsi `financial_statements` içinde vardır |
| `financial_statements` | Finansal raporların mali tabloları. Her değerin XBRL kavramı, dönemi ve para birimi vardır |
| `attachments` | Ekler: adı, bağlantısı ve okunduysa `text` ile `tables`. Atlanan eklerde `file_type` `skipped` olur, `text` boştur |

## Şirket listesi

Bildirimleri toplanan şirketler `companies.txt` dosyasındadır, sektörlere göre gruplanmış hâlde. Her satırın ilk kelimesi bir hisse kodudur, gerisi açıklamadır. `#` ile başlayan satırlar, sektör başlıkları dahil, okunmaz.

Bir bildirim iki durumda alınır:

- Listedeki bir şirketin kendi bildirimiyse.
- Başka bir kurumun bildirimi listedeki bir şirketi "ilgili hisse" olarak anıyorsa. Örneğin Borsa İstanbul'un devre kesici bildirimleri, Takasbank duyuruları ve derecelendirme kuruluşlarının kredi notları.

Kodu değişen şirketlerin eski kodları da listede durmalıdır. KAP, şirketin kendi bildirimlerinde eski tarihli olsalar bile güncel kodu verir. Başka kurumların bildirimlerinde ise o günkü kod yazar. Örneğin Türk Altın (TRALT), eski bildirimlerde KOZAL olarak geçer.

Listeye şirket eklerseniz geçmiş komutunu yeniden çalıştırmanız yeterlidir. Listeler her çalıştırmada KAP'tan yeniden alındığı için yeni şirketlerin bildirimleri de çekilir. Listeden çıkarılan şirketlerin toplanmış bildirimleri silinmez.

## Hangi ekler okunur

Eklerin çoğu ya sayfada zaten yazılanın resmî belgesidir ya da faaliyet raporu gibi uzun, rutin raporlardır. Bu yüzden ekler sadece şu bildirim türlerinde okunur:

- **Finansal Rapor** ve **Finansal Rapor Bildirimi:** dipnotlar ve bağımsız denetçi raporu. Sayfada yalnızca mali tablolar vardır, gerisi PDF'tedir. PDF'teki mali tablo sayfaları sayfadakilerle aynıdır; tüm dosya okunur
- **Özel Durum Açıklaması (Genel):** yatırımcı sunumları, finansal sonuç notları, beklenti güncellemeleri
- **Kredi Derecelendirmesi**
- **Değerleme Raporu**
- **Halka arz fiyatının varsayımlarına ilişkin rapor**
- **Fon kullanım raporu**

Bu türlerde de, aynı bildirimde Türkçesi bulunan bir belgenin İngilizcesi indirilmez. Dil, dosya adından tahmin edilir; emin olunamazsa ek okunur. Listeyi değiştirmek için `kap_attachments.py` içindeki `EXTRACT_TYPES` sabitini düzenleyin.

## Dosyalar

```text
companies.txt         Bildirimleri toplanan şirketler
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

- **İstek sınırı.** KAP çok fazla istek gönderen IP'yi geçici olarak engeller. Engeli bazen HTTP 429 ile bildirir, bazen de bağlantıları hiç yanıt vermeden keser. Geçmiş komutu sayfaları 6, ekleri 12 işçiyle aynı anda çeker. İstekler tüm işçiler için ortak bir sırayla, 1,5 saniye arayla başlar. Bu aralık uzun süre sorunsuz çalıştı; 1,22 saniyeye inildiğinde ise KAP IP'yi engelledi. Engel olunca (HTTP 429, 403, 503 ya da yanıtsız kesilen bağlantı) herkes 5 dakika bekler, sonra istekler 1,5 kat daha seyrek gönderilir. Aralık `kap_http.py` içindeki `REQUEST_INTERVAL` değeridir, işçi sayıları `kap_history.py` içindedir. Aralığı kısaltmayın.
- **Yavaş indirmeler.** KAP ekleri her bağlantıya saniyede yaklaşık 10-15 KB ile verir. Bu yüzden her ekin indirilmesine boyutuna göre süre tanınır: en az 180 saniye, büyük dosyalarda saniyede 4 KB'a göre gereken süre. Örneğin bir bankanın 4,7 MB'lık finansal raporuna yaklaşık 19 dakika tanınır. KAP bazen dosyanın ortasında veri göndermeyi bırakır. 30 saniye hiç veri gelmezse indirme kesilip baştan denenir. Bu sürede inmeyen ek bir sonraki çalıştırmada yeniden denenir. Eki inmeyen bildirim o zamana kadar JSON Lines dosyasına girmez.
- **Kilit hatası.** `Could not set lock on file ... kap.duckdb` hatası, veritabanının başka bir süreçte açık olduğunu gösterir. Önce o süreci durdurun.
- **Türkçe karakterler bozuksa** PowerShell'de `$env:PYTHONIOENCODING = "utf-8"` ayarlayın.
- **KAP değişebilir.** Ayrıştırıcılar KAP'ın Ekim 2026'daki sayfa düzenine göre yazıldı.
