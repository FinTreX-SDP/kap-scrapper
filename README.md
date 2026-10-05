# KAP Scraper

[KAP](https://www.kap.org.tr) (Kamuyu Aydınlatma Platformu) için gerçek zamanlı bir veri toplayıcı. FinTrex için geliştirildi.

KAP'ta yayımlanan her yeni bildirimi saniyeler içinde yakalar ve içindeki her şeyi kaydeder:

- bildirimin metni, alanları ve tabloları,
- ekteki PDF ve görsellerin metni ve tabloları, taranmış belgeler için OCR dahil,
- finansal raporlardaki mali tablolar, dönemleriyle birlikte sayısal değerler olarak.

Tüm veriler tek bir [DuckDB](https://duckdb.org) veritabanında tutulur. Hızlıca göz atmak için ayrıca bir Excel dosyası üretilir.

> **Resmî değildir.** Bu proje KAP veya MKK ile bağlantılı değildir. KAP web sitesinin kendisinin kullandığı iç uç noktaları okur. Bu uç noktalar belgelenmemiştir ve haber verilmeden değişebilir. İstek sıklığını düşük tutun ve KAP'ın kullanım koşullarına uyun.

## Özellikler

- **Gerçek zamanlı izleyici.** KAP'ı 15 saniyede bir kontrol eder ve her yeni bildirimi hemen işler.
- **Bildirim sayfaları.** Düz metin, etiket-değer alanları ve tablolar. KAP bir alanı XBRL kavramıyla etiketlemişse kavram adı da saklanır.
- **Ekler.** PDF ve görseller bellekte indirilir, diske hiç yazılmaz.
  - Metin, PDF'in metin katmanından ya da taranmış sayfa ve görsellerde Tesseract OCR ile çıkarılır.
  - Tablolar PyMuPDF'in tablo bulucusuyla çıkarılır. Taranmış belgelerde tablolar çizgilerinden bulunur ve her hücre ayrı ayrı OCR'dan geçirilir. Böylece hiçbir değer yanlış satıra ya da sütuna kayamaz.
- **Mali tablolar.** Bilanço, kâr veya zarar tablosu, diğer kapsamlı gelir tablosu, nakit akış tablosu ve özkaynak değişim tablosu. Her satırda tek bir sayısal değer bulunur. Değerin XBRL kavramı, dönemi ve özkaynak tablosunda ait olduğu özkaynak kalemi de saklanır.
- **KAP'ın sınırlarına uyumlu.** İstek sınırına takılınca bekler, yavaşlatılan indirmeleri bir süre sınırından sonra bırakır ve daha sonra yeniden dener.

## Nasıl çalışır

```mermaid
flowchart LR
    feed["KAP bildirim listesi<br/>(15 sn'de bir kontrol)"] --> page["Bildirim sayfası"]
    page --> details["Metin, alanlar, tablolar"]
    page --> files["Ekler<br/>(PDF, görsel)"]
    page -->|finansal raporlar| fin["Mali tablolar"]
    files -->|metin katmanı veya OCR| att["Ek metinleri ve tabloları"]
    details --> db[("DuckDB<br/>data/kap.duckdb")]
    att --> db
    fin --> db
    db --> xlsx["Excel çıktısı<br/>data/disclosures.xlsx"]
```

1. **Bildirim listesi.** KAP ana sayfasının attığı isteğin aynısı gönderilir:
   `POST https://www.kap.org.tr/tr/api/disclosure/list/main`, gövdesi
   `{"fromDate": "GG.AA.YYYY", "toDate": "GG.AA.YYYY", "memberTypes": ["IGS", "DDK"]}`.
   Gelen JSON yanıtında her bildirim için bir kayıt bulunur. Fon bildirimleri istenmez.
2. **Bildirim sayfası.** `https://www.kap.org.tr/tr/Bildirim/{disclosure_index}` sunucu tarafında üretilen bir HTML sayfasıdır. KAP iki farklı sayfa düzeni kullanır:
   - **legacy (eski düzen):** etiket-değer satırları, bölüm başlıkları, kenarlıklı tablolar ve serbest metin blokları;
   - **xbrl:** her satırda `ifrs-full_CashAndCashEquivalents` gibi bir XBRL kavramı, Türkçe ve İngilizce etiket ve değerler bulunur. Sadece Türkçe metin saklanır.

   Bildirim gövdesinin ham HTML'i de saklanır. Böylece sayfalar daha sonra KAP'a gitmeden yeniden ayrıştırılabilir.
3. **Ekler.** Dosyalar `https://www.kap.org.tr/tr/api/file/download/{file_id}` adresinden iner. KAP her dosyayı serileştirilmiş bir Java bayt dizisinin içine sarar: 23 baytlık bir başlık, 4 baytlık bir uzunluk bilgisi ve ardından dosyanın kendisi. Betik dosyayı okumadan önce bu sarmalı açar.
4. **Mali tablolar.** Finansal rapor sayfalarında her mali tablo, XBRL etiketli ayrı bir HTML tablosudur. Olağan tablolarda her değer sütunu bir dönem başlığına aittir. Özkaynak değişim tablosunda ise sütunlar özkaynak kalemleridir, dönemler satır grupları olarak gelir.

## Gereksinimler

- **Python.** Windows 11 üzerinde Python 3.14 ile test edildi.
- **Tesseract OCR 5** ve Türkçe dil verisi. Taranmış ekleri okumak için gerekir.
- **Bilgisayar saati Türkiye saatinde olmalı** (Europe/Istanbul). KAP yayın saatleri Türkiye yerel saatidir ve izleyici bunları bilgisayarın saatiyle karşılaştırır.

## Kurulum (Windows)

1. Depoyu klonlayın ve Python paketlerini kurun:

   ```powershell
   git clone https://github.com/Simittin/kap-scrapper-.git
   cd kap-scrapper-
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

   PowerShell `Activate.ps1` dosyasını çalıştırmayı reddederse, kendi kullanıcınız için yerel betiklere şu komutla izin verin:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

2. Tesseract OCR'ı kurun:

   ```powershell
   winget install UB-Mannheim.TesseractOCR
   ```

3. Dil dosyalarını hazırlayın. `Program Files` altına yazmak yönetici izni gerektirdiği için proje kendi `tessdata` klasörünü kullanır:

   ```powershell
   mkdir tessdata
   copy "C:\Program Files\Tesseract-OCR\tessdata\eng.traineddata" tessdata\
   copy "C:\Program Files\Tesseract-OCR\tessdata\osd.traineddata" tessdata\
   curl.exe -L -o tessdata\tur.traineddata https://github.com/tesseract-ocr/tessdata_best/raw/main/tur.traineddata
   ```

4. Yerel ayar dosyanızı oluşturun ve yolları doldurun:

   ```powershell
   copy .env.example .env
   ```

   | Değişken | Değer |
   |---|---|
   | `TESSERACT_CMD` | `tesseract.exe` dosyasının tam yolu. Genelde `C:\Program Files\Tesseract-OCR\tesseract.exe` |
   | `TESSDATA_PREFIX` | Projedeki `tessdata` klasörünün tam yolu |

   `kap_watch.py` ve `kap_attachments.py` bu ayarlara ihtiyaç duyar. `TESSERACT_CMD` tanımlı değilse başlarken durur.

### Linux ve macOS (test edilmedi)

Kodda Windows'a özgü bir kısım yok, ancak sadece Windows'ta çalıştırıldı. Debian veya Ubuntu'da:

```bash
sudo apt install tesseract-ocr tesseract-ocr-tur
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Ardından `.env` dosyasında `TESSERACT_CMD=/usr/bin/tesseract` ve `TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata` olarak ayarlayın. Bir sunucuda çalıştıracaksanız saat dilimini Europe/Istanbul yapın.

## Kullanım

### Gerçek zamanlı izleyici

Projenin ana kullanım şekli budur. Başlatın ve açık bırakın:

```powershell
python kap_watch.py
```

Her yeni bildirim ekranda tek bir satır olarak görünür. Örnek çıktı, değerler temsilîdir:

```text
[09:30:02] Watching KAP every 15 s for disclosures published after 06.10 09:20:02. Ctrl+C to stop.
[09:31:17] NEW 1672701 ASELS: Yeni İş İlişkisi (published 09:31:05, caught after 12 s)
[09:31:29]   attachment 1672701 'Sozlesme.pdf': 3 pages, 0 OCR, 1 tables
[09:42:48]   financial statements of 1672720: 498 values
[09:42:48] NEW 1672720 BTCIM: Finansal Rapor (published 09:42:36, caught after 12 s)
```

- **Neyi işler.** İzleyici başladıktan sonra yayımlanan bildirimleri ve başlangıçtan önceki 10 dakikayı işler. Böylece kısa bir yeniden başlatmada hiçbir şey kaçmaz. Daha eski bildirimler için toplu çalıştırma betiklerini kullanın.
- **İş sırası.** Önce bildirim sayfası çekilip kaydedilir. Bildirim finansal raporsa mali tablolar da aynı anda çıkarılır. Ekler arka plandaki bir işçiye gider. Böylece uzun bir OCR işi bir sonraki bildirimi geciktirmez.
- **Hatalar.** Alınamayan bir bildirim sayfası bir sonraki kontrolde yeniden denenir, en fazla 3 kez. Başarısız olan ekler, ek işçisi boştayken 5 dakikada bir yeniden kuyruğa alınır.
- **Excel.** Yeni veri geldiğinde Excel dosyası yeniden yazılır. Dosya Excel'de açıksa değiştirilemez. İzleyici bunu ekrana yazar ve bir sonraki değişiklikte yeniden dener.
- **Durdurma.** Ctrl+C'ye basın. Kuyrukta kalan ekler ekrana yazılır. Onları tamamlamak için `python kap_attachments.py` komutunu çalıştırın.

İzleyici çalıştığı sürece veritabanını açık tutar. DuckDB buna aynı anda yalnızca tek bir sürecin izin verir. Toplu çalıştırma betiklerini başlatmadan ya da veritabanını başka bir yerden açmadan önce izleyiciyi durdurun.

### Toplu çalıştırma

Geçmiş bildirimleri çekmek ya da yarım kalan işi tamamlamak için bu betikleri sırayla çalıştırın:

| Adım | Komut | Ne yapar |
|---|---|---|
| 1 | `python kap_disclosures.py --start 01.10.2026 --end 05.10.2026` | Aralıktaki her günün bildirim listesini çeker. Tarih verilmezse bugünü çeker. |
| 2 | `python kap_details.py --limit 50` | Detayı henüz çekilmemiş bildirimlerin sayfalarını en yeniden başlayarak çeker. `--limit` verilmezse hepsini çeker. |
| 3 | `python kap_attachments.py --limit 20` | Metni henüz çıkarılmamış ekleri indirir, metinlerini ve tablolarını çıkarır. `--limit` verilmezse hepsini işler. |
| 4 | `python kap_financials.py` | Saklı finansal rapor sayfalarından mali tablo satırlarını üretir. KAP'a istek atmaz. |

2, 3 ve 4. adımlar sadece eksik kalanları işler. Bu yüzden istediğiniz an durdurup yeniden başlatabilirsiniz. 1. adım verilen günleri yeniden çeker ve var olan satırları günceller. Her kayıt tek bir işlem (transaction) içinde yazılır, bu yüzden bir kesinti asla yarım kayıt bırakmaz.

Ayrıştırıcılar değiştiğinde, saklı sayfalar KAP'a gitmeden yeniden işlenebilir:

```powershell
python kap_details.py --reparse     # saklı tüm sayfaların alanları, tabloları ve metni
python kap_financials.py --all      # tüm mali tablolar
```

Toplu çekme, KAP'ın istek sınırları yüzünden yavaştır. Binlerce bildirimin çekilmesi saatler sürebilir. Ayrıntılar için [KAP istek sınırları](#kap-istek-sınırları) bölümüne bakın.

## Çıktılar

### Veritabanı tabloları

Veritabanı `data/kap.duckdb` dosyasıdır. Her tabloda `disclosure_index` sütunu bulunur. Bu, KAP'ın bildirime verdiği numaradır ve sayfa adresinde de geçer. Ek tabloları ayrıca `file_id` ile birbirine bağlanır.

| Tablo | Her satır | Başlıca sütunlar |
|---|---|---|
| `disclosures` | bir bildirim | `publish_date`, `stock_code`, `company_title`, `title`, `summary`, `disclosure_class`, `attachment_count`, `url` |
| `disclosure_details` | çekilmiş bir sayfa | `page_format` (`legacy`, `xbrl` veya `empty`), `text`, `html`, `fetched_at` |
| `disclosure_fields` | sayfadaki bir alan | `section`, `concept` (sadece XBRL sayfalarında), `label`, `value_col`, `value` |
| `disclosure_tables` | eski düzen bir sayfadaki tablo | `section`, `rows_json` (satır listesi, her satır bir hücre listesi) |
| `disclosure_attachments` | bir ek | `file_id`, `file_name`, `url` |
| `attachment_texts` | işlenmiş bir ek | `file_type`, `page_count`, `ocr_pages`, `skipped_pages`, `text` |
| `attachment_tables` | bir ekteki tablonun bir satırı | `page`, `table_no`, `row_no`, `method` (`text` veya `ocr`), `cells` (metin listesi) |
| `financial_items` | mali tablodaki bir değer | `statement`, `concept`, `label`, `member`, `period_label`, `period_start`, `period_end`, `value`, `currency`, `consolidation` |

Veriyle ilgili notlar:

- **Bildirim sınıfları.** `ODA` özel durum açıklaması, `FR` finansal rapor, `DUY` duyuru, `DG` diğer bildirimlerdir.
- **Hisse kodları.** Birden fazla aracı ilgilendiren bir bildirimde kodların hepsi yazılır, örneğin `TBY, TEBYT`.
- **Ek metinleri.** Metinde her sayfa `[Page N]` işaretiyle başlar.
- **Mali değerler.** Sayı olarak, KAP'ta gösterildiği haliyle ve `currency` sütunundaki para biriminde saklanır.
- **Şirketleri karşılaştırmak.** `label` yerine `concept` sütununu kullanın, örneğin `ifrs-full_Revenue`. Etiketler şirketten şirkete farklı olabilir, kavramlar ise ortak XBRL taksonomisinden gelir.
- **Dönemler.** `period_label`, KAP'taki sütun başlığıdır: `Cari Dönem`, `Önceki Dönem` ya da `Cari Dönem 3 Aylık` gibi. Bilanço değerleri belirli bir tarihteki durumu gösterdiği için `period_start` boştur.
- **Özkaynak değişim tablosu.** `member` sütunu özkaynak kalemini tutar, toplam sütunu `Özkaynaklar`'dır. Her satır kendi grubunun dönemini taşır. Dönem başı ve dönem sonu bakiyelerini `Dönem Başı Bakiyeler` ve `Dönem Sonu Bakiyeler` etiketli satırlardan okuyun.
- **Birleşik tablolar.** Bazı şirketler kâr veya zarar tablosu ile diğer kapsamlı gelir tablosunu tek tabloda verir: `Kar veya Zarar ve Diğer Kapsamlı Gelir Tablosu`.

### Excel çıktısı

`data/disclosures.xlsx` her betik çalıştığında, izleyicide ise her değişiklikten sonra yeniden yazılır. Her sayfada başlık satırı sabitlenmiştir ve filtreler açıktır.

| Sayfa | İçerik |
|---|---|
| Disclosures | Her bildirim bir satır. Detayı çekilmişse sayfa metni ve ek adları da yer alır |
| Details | Çekilen her sayfanın düzeni ve metni |
| Fields | Tüm sayfaların tüm alanları |
| Tables | Eski düzen sayfalardaki tablolar |
| Attachments | Her ek ve çıkarılan metni |
| AttachmentTables | Eklerdeki tabloların her satırı. Her hücre ayrı bir sütunda |
| Financials | Her satırda bir mali değer, hisse kodu ve şirket adıyla birlikte |

Bir Excel hücresi en fazla 32.767 karakter alabilir. Bu yüzden uzun metinler Excel'de kesik görünür. Veritabanında her zaman tam metin bulunur.

### Örnek sorgular

İzleyici kapalıyken veritabanını Python'dan salt okunur modda açın:

```python
import duckdb

con = duckdb.connect("data/kap.duckdb", read_only=True)
df = con.sql("SELECT * FROM disclosures ORDER BY publish_date DESC LIMIT 10").pl()  # Polars DataFrame
```

Bir hissenin son bildirimleri:

```sql
SELECT publish_date, title, summary, url
FROM disclosures
WHERE list_contains(string_split(stock_code, ', '), 'ASELS')
ORDER BY publish_date DESC
LIMIT 20;
```

Eklerin içinde arama:

```sql
SELECT d.publish_date, d.stock_code, d.title, a.file_name
FROM attachment_texts t
JOIN disclosure_attachments a USING (disclosure_index, file_id)
JOIN disclosures d USING (disclosure_index)
WHERE t.text ILIKE '%geri alım%'
ORDER BY d.publish_date DESC;
```

Tüm şirketlerin cari dönem hasılatı:

```sql
SELECT d.stock_code, f.period_start, f.period_end, f.value AS revenue
FROM financial_items f
JOIN disclosures d USING (disclosure_index)
WHERE f.concept = 'ifrs-full_Revenue'
  AND f.period_label = 'Cari Dönem'
ORDER BY revenue DESC;
```

Bir şirketin bilançosu, KAP'taki sırasıyla:

```sql
SELECT f.label, f.period_label, f.period_end, f.value
FROM financial_items f
JOIN disclosures d USING (disclosure_index)
WHERE d.stock_code = 'BTCIM'
  AND f.statement = 'Finansal Durum Tablosu (Bilanço)'
ORDER BY f.row_no;
```

Aktif toplamının kaynak toplamına eşit olduğunu kontrol etme:

```sql
SELECT d.stock_code, f.period_end,
       max(f.value) FILTER (WHERE f.concept = 'ifrs-full_Assets') AS assets,
       max(f.value) FILTER (WHERE f.concept = 'ifrs-full_EquityAndLiabilities') AS equity_and_liabilities
FROM financial_items f
JOIN disclosures d USING (disclosure_index)
WHERE f.statement = 'Finansal Durum Tablosu (Bilanço)'
GROUP BY d.stock_code, f.period_end;
```

## Ayarlar

Bu sabitler ilgili dosyaların başında bulunur:

| Dosya | Sabit | Varsayılan | Anlamı |
|---|---|---|---|
| `kap_watch.py` | `POLL_INTERVAL` | 15 sn | Bildirim listesinin kontrol edilme aralığı |
| `kap_watch.py` | `CATCH_UP_MINUTES` | 10 dk | İzleyicinin başlangıç anından ne kadar geriye baktığı |
| `kap_watch.py` | `RETRY_INTERVAL` | 300 sn | Başarısız eklerin yeniden denenme aralığı |
| `kap_watch.py` | `MAX_FAILURES` | 3 | İzleyicinin bir bildirim sayfasını atlamadan önceki deneme sayısı |
| `kap_disclosures.py` | `REQUEST_DELAY` | 1 sn | İstekler arasındaki bekleme |
| `kap_http.py` | `RATE_LIMIT_PAUSE` | 300 sn | KAP "çok fazla istek" yanıtı verince beklenen süre |
| `kap_http.py` | `MAX_RATE_LIMIT_PAUSES` | 3 | Vazgeçmeden önceki ardışık bekleme sayısı |
| `kap_http.py` | `DOWNLOAD_DEADLINE` | 180 sn | Tek bir indirmenin sürebileceği en uzun süre |
| `kap_attachments.py` | `OCR_LANG` | `tur` | Tesseract dili |
| `kap_attachments.py` | `OCR_DPI` | 300 | Taranmış sayfaların görüntüye çevrilme çözünürlüğü |
| `kap_attachments.py` | `MIN_TEXT_CHARS` | 50 | Bundan az metin içeren ve görsel barındıran sayfa taranmış sayılır |
| `kap_attachments.py` | `MAX_OCR_PAGES` | 50 | Dosya başına okunan en fazla taranmış sayfa. Kalanlar `skipped_pages` sütununda sayılır |

## KAP istek sınırları

KAP istek sınırlarını belgelemiyor. Gözlemlediklerimiz şunlar:

- **Engelleme.** Birkaç dakika içinde yaklaşık 100 sayfa ya da dosya isteğinden sonra KAP HTTP 429 yanıtı verir ve birkaç dakika boyunca engellemeye devam eder. Bir sayfa ya da dosya isteği bu yanıtı aldığında betikler 5 dakika bekleyip yeniden dener, en fazla 3 kez. Bundan sonra toplu çalıştırma betikleri durur, izleyici ise bir sonraki kontrolde yeniden dener.
- **Yavaşlatma.** KAP bazen isteği reddetmek yerine yanıtı saniyede yaklaşık 10 KB'a düşürür. Bu yüzden her indirmenin 180 saniyelik bir süre sınırı vardır. İzleyici yarıda bırakılan ekleri daha sonra yeniden kuyruğa alır.
- **Veri kaybolmaz.** KAP izleyiciyi engellediği sürece yeni bildirimler sadece gecikir. Engel kalkınca izleyici, başladığından beri yayımlanan her şeyi işler.

İzleyicinin kendi yükü küçüktür: 15 saniyede bir liste isteği, artı her yeni bildirim ve her ek için birer istek. Sınırlara asıl takılan iş toplu çekmedir. `POLL_INTERVAL` ya da `REQUEST_DELAY` değerlerini fazla düşürmek engellenme ihtimalini artırır. Bir engelleme ise tüm yakalamayı dakikalarca durdurur.

## Sınırlamalar

- **Fonlar** kapsanmıyor.
- **İzleyicinin çalışıyor olması gerekir.** Kapalıyken hiçbir şey yakalamaz. Yeniden başladığında sadece son 10 dakikayı telafi eder. Henüz bilgisayar açılınca kendiliğinden başlamıyor.
- **Aynı anda tek süreç.** DuckDB, veritabanının yazma amacıyla aynı anda yalnızca tek bir süreç tarafından açılmasına izin verir.
- **OCR kusursuz değil.** Çizgili tablolardaki rakamlar güvenilir şekilde okunur, ama semboller bazen yanlış okunur. Örneğin `(=)` yerine `(-)` çıkabilir. Taranmış sayfalardaki çizgisiz tablolar sadece düz metin olarak saklanır.
- **Dosya türleri.** Sadece PDF ve görseller okunur. Diğer ekler `file_type` değeri `unsupported` olarak kaydedilir.
- **Excel çıktısı büyümeye uygun değil.** Her değişiklikte tüm dosya baştan yazılır ve veritabanı büyüdükçe bu işlem yavaşlar. Ayrıca bir Excel sayfası en fazla 1.048.576 satır alabilir. Asıl analiz için veritabanını kullanın.
- **KAP değişebilir.** Ayrıştırıcılar, KAP'ın Ekim 2026 itibarıyla kullandığı sayfa düzenlerine göre yazıldı.

## Sorun giderme

| Mesaj veya belirti | Sebep | Çözüm |
|---|---|---|
| `KeyError: 'TESSERACT_CMD'` | `.env` dosyası yok | `.env.example` dosyasını `.env` adıyla kopyalayıp yolları doldurun |
| `Failed loading language 'tur'` | `tur.traineddata` eksik ya da `TESSDATA_PREFIX` yanlış | `TESSDATA_PREFIX` klasöründe `tur.traineddata` dosyası olduğunu kontrol edin |
| `Could not set lock on file ... kap.duckdb` | Veritabanını başka bir süreç açmış, çoğunlukla izleyici | Önce o süreci durdurun |
| `Excel file is open, so it was not updated` | Dosya Excel'de açık | Dosyayı kapatın. Bir sonraki değişiklikte güncellenir |
| `KAP rate limit hit, pausing 5 minutes...` | KAP bir süreliğine engelliyor | Bir şey yapmanıza gerek yok. Betik kendiliğinden devam eder |
| Türkçe karakterler bozuk görünüyor | Konsol ya da yönlendirilen çıktı UTF-8 değil | `PYTHONIOENCODING=utf-8` ayarlayın. PowerShell'de: `$env:PYTHONIOENCODING = "utf-8"` |

## Proje yapısı

```text
kap_watch.py          Gerçek zamanlı izleyici, ana giriş noktası
kap_disclosures.py    Bildirim listesi, ortak ayarlar ve Excel çıktısı
kap_details.py        Bildirim sayfaları: metin, alanlar, tablolar ve ek listesi
kap_attachments.py    Ek indirme, metin ve tablo çıkarma, OCR
kap_financials.py     XBRL etiketli rapor sayfalarından mali tablolar
kap_http.py           KAP'ın istek sınırına ve yavaşlatmasına karşı HTTP yardımcısı
requirements.txt      Python paketleri
.env.example          Yerel ayarlar için şablon
data/                 Veritabanı ve Excel çıktısı. İlk çalıştırmada oluşur, git'e eklenmez
tessdata/             Tesseract dil dosyaları. Git'e eklenmez
```

Başlıca kütüphaneler: istekler için [httpx](https://www.python-httpx.org), HTML için [selectolax](https://github.com/rushter/selectolax), PDF için [PyMuPDF](https://pymupdf.readthedocs.io), OCR ve tablo çizgisi tespiti için pytesseract üzerinden [Tesseract](https://github.com/tesseract-ocr/tesseract) ile [OpenCV](https://opencv.org), veri saklama için [DuckDB](https://duckdb.org) ve [Polars](https://pola.rs), Excel için [openpyxl](https://openpyxl.readthedocs.io), yeniden denemeler için [tenacity](https://tenacity.readthedocs.io).
