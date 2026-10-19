# KAP Scraper

[KAP](https://www.kap.org.tr) bildirimlerini toplayan bir veri toplayıcı. Yalnızca `companies.txt` dosyasındaki şirketlerle ilgili bildirimleri alır. Model eğitimi için her bildirimi metni, mali tabloları ve önemli ekleriyle birlikte tek bir dosyada toplar. İki şekilde çalışır: yeni bildirimleri yayımlandıkları anda yakalar (**canlı**) ya da verilen tarih aralığındaki bildirimleri çeker (**geçmiş**).

> Resmî değildir. KAP sitesinin kendi kullandığı, belgelenmemiş uç noktaları okur. İstek sıklığını düşük tutun.

## Nasıl çalışır

Her bildirim için aynı adımlar izlenir:

1. **Liste.** Bildirim listesi KAP'tan çekilir ve hemen süzülür. Yalnızca [listedeki şirketlerle](#şirket-listesi) ilgili bildirimler kalır; diğerlerinin sayfaları hiç istenmez. Fon bildirimleri hariçtir.
2. **Sayfa.** Bildirimin sayfası çekilir. Metni, alanları ve tabloları ayrıştırılır.
3. **Mali tablolar.** Finansal raporlarda bilanço, gelir tablosu, nakit akış ve özkaynak tabloları sayısal değerler olarak çıkarılır.
4. **Ekler.** Sadece değerli türlerin ekleri indirilir ve metni çıkarılır. Taranmış belgeler OCR ile okunur. Ayrıntılar: [Hangi ekler okunur](#hangi-ekler-okunur).
5. **Kayıt.** Sayfası ve ekleri tamamlanan bildirim `data/disclosures.jsonl` dosyasına bir satır olarak eklenir. Veritabanı yoktur; bu dosya tek çıktıdır.

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

## Docker ile kurulum

Gereksinim: Docker Desktop. Tesseract, Türkçe OCR dosyası ve Türkiye saati image'ın içindedir; `.env` ve `tessdata` gerekmez.

```powershell
docker compose up -d --build
```

Bu komut canlı izleyiciyi arka planda başlatır. Çıktılar yine projedeki `data/` klasörüne yazılır.

| Komut | Ne yapar |
|---|---|
| `docker compose logs -f` | İzleyicinin çıktısını gösterir |
| `docker compose stop` | İzleyiciyi durdurur. Ctrl+C gibidir, çıktılar yazılıp kapanır |
| `docker compose start` | İzleyiciyi yeniden başlatır |
| `docker compose run --rm kap python kap_history.py --start 01.01.2025 --end 31.12.2025` | Geçmiş. Önce izleyiciyi durdurun |

- **Otomatik başlama.** Siz durdurmadıkça izleyici, çökerse ve Docker Desktop her açıldığında kendiliğinden başlar. Bilgisayar açılınca çalışması için Docker Desktop ayarlarında "Start Docker Desktop when you sign in" seçili olmalı.
- **Kod ya da `companies.txt` değişince** `docker compose up -d --build` ile image'ı yeniden oluşturun.
- **Aynı anda tek süreç.** Container çalışırken Windows'ta `python kap_*.py` çalıştırmayın. İkisi aynı dosyaya yazar.

## AWS ile geçmiş

Geçmiş komutu AWS'de (Frankfurt, ECS Fargate) çalışır, bilgisayarın açık kalması gerekmez. Aralık parçalara bölünür, her parçayı ayrı bir görev çeker. KAP çok istek gönderen IP'yi engellediği için istekler `aws/` içindeki squid proxy'lerinden sırayla gider. Komutlar Git Bash'te proje klasöründen çalıştırılır; AWS CLI ve Docker Desktop gerekir.

```bash
aws login                                       # tarayıcıda AWS'ye giriş; oturum süresi dolunca tekrar
bash aws/setup.sh                               # ilk kurulum; kod ya da companies.txt değişince de çalıştırın
bash aws/run.sh 01.12.2022 18.10.2026 8 40      # aralık, görev sayısı, proxy sayısı
bash aws/status.sh                              # çalışan görevler ve S3'teki dosyalar
aws logs tail /ecs/kapscrapper --follow         # canlı log
bash aws/stop.sh                                # hepsini durdurur; `stop.sh proxy` yalnızca proxy'leri
```

- **Sonuç.** Her görev kendi aralığını `s3://kapscrapper-<hesap>-eu-central-1/disclosures/<ilk gün>_<son gün>.jsonl` dosyasına yazar. Her satır `disclosures.jsonl` dosyasındaki bir satırla aynıdır. İndirmek için: `aws s3 sync s3://kapscrapper-<hesap>-eu-central-1/disclosures data/aws`.
- **Devam etme.** Dosya 5 dakikada bir ve görev durdurulunca S3'e yüklenir. Aynı komut yeniden çalıştırılınca dosyadaki bildirimler atlanır.
- **Eksiksizlik.** Hata veren bildirimler ve listeler, görevin sonunda listeler yeniden çekilerek 5 dakika arayla 3 tura kadar yeniden denenir. Görev logunun sonunda `Complete: ...` ya da eksiklerin numaralarıyla `Incomplete: ...` yazar. Okunamayan bir ek bildirimi düşürmez; `file_type` değeri `error` olur, nedeni `error` alanındadır.
- **Proxy'ler.** Her proxy'den aynı anda en fazla 8 istek gider; KAP'ın engellediği proxy 4 dakika dinlenir. Proxy'ler 12 saat sonra kendiliğinden kapanır.
- **Maliyet.** 8 görev ve 40 proxy saatte yaklaşık 1,3 $ tutar. 01.12.2022–18.10.2026 aralığı (~48.000 bildirim) 7–8 saat sürer.
- **Kaldırma.** `bash aws/teardown.sh` her şeyi siler, S3'teki veriyi bırakır.

## Kullanım

Canlı ve geçmişi aynı anda çalıştırmayın. İkisi aynı dosyaya yazar ve aynı bildirimi iki kez ekleyebilir. İkisi de Ctrl+C ile durdurulur.

### Canlı

```powershell
python kap_watch.py
```

KAP'ı beklemeden sürekli kontrol eder ve her yeni bildirimi hemen işler. Başladığı andan 10 dakika öncesine kadar yayımlananları da alır. Hata alan bir bildirim 5 dakika sonra yeniden denenir, en fazla 3 kez. Açık kaldığı sürece çalışır. Kapalı kaldığı aralığı sonradan geçmiş komutuyla doldurabilirsiniz.

### Geçmiş

```powershell
python kap_history.py --start 01.01.2025 --end 31.12.2025
```

- **Tarihler.** Tarihler `GG.AA.YYYY` biçimindedir ve iki gün de aralığa dahildir. Bildirimler yeniden eskiye doğru işlenir.
- **Devam etme.** Durdurulursa aynı komutu yeniden çalıştırın; dosyada olan bildirimleri atlar, kaldığı yerden devam eder. Hata alan bildirimler de o zaman baştan denenir. Yarım kalan bir bildirimin (örneğin eki inmekte olan) sayfası da yeniden çekilir.
- **Bitiş.** Aralık bitince kendiliğinden kapanır.
- **Süre.** Liste ay ay, tek istekte çekilir. Asıl süreyi sayfalar ve ekler alır. Eylül 2026'da listedeki şirketlerle ilgili 1.210 bildirim vardı. 12 işçi aynı anda çalışır; bazıları yavaş inen ekleri beklerken diğerleri sayfalara devam eder. Uzun aralıkları birkaç güne bölerek çalıştırabilirsiniz.

## Çıktılar

Tek çıktı `data/disclosures.jsonl` dosyasıdır (**eğitim verisi**). Her satır bir bildirimin tam kaydıdır.

Bir bildirim, sayfası ve ekleri okunduğunda dosyaya bir kez eklenir. Satırlar yayın sırasına göre değil, işin bittiği sıraya göre dizilir. Okuma örneği: `[json.loads(line) for line in open("data/disclosures.jsonl", encoding="utf-8")]`.

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
| `attachments` | Ekler: adı, bağlantısı, `text` ile `tables`. Türkçesi olan bir belgenin İngilizcesinde `file_type` `skipped`, okunamayan dosya türlerinde `unsupported` olur; ikisinde de `text` boştur |

## Şirket listesi

Bildirimleri toplanan şirketler `companies.txt` dosyasındadır, sektörlere göre gruplanmış hâlde. Her satırın ilk kelimesi bir hisse kodudur, gerisi açıklamadır. `#` ile başlayan satırlar, sektör başlıkları dahil, okunmaz.

Bir bildirim iki durumda alınır:

- Listedeki bir şirketin kendi bildirimiyse.
- Başka bir kurumun bildirimi listedeki bir şirketi "ilgili hisse" olarak anıyorsa. Örneğin Borsa İstanbul'un devre kesici bildirimleri, Takasbank duyuruları ve derecelendirme kuruluşlarının kredi notları.

Kodu değişen şirketlerin eski kodları da listede durmalıdır. KAP, şirketin kendi bildirimlerinde eski tarihli olsalar bile güncel kodu verir. Başka kurumların bildirimlerinde ise o günkü kod yazar. Örneğin Türk Altın (TRALT), eski bildirimlerde KOZAL olarak geçer.

Listeye şirket eklerseniz geçmiş komutunu yeniden çalıştırmanız yeterlidir. Listeler her çalıştırmada KAP'tan yeniden alındığı için yeni şirketlerin bildirimleri de çekilir. Listeden çıkarılan şirketlerin toplanmış bildirimleri silinmez.

## Hangi ekler okunur

Her bildirimin bütün ekleri, türüne bakılmadan indirilip okunur. Yalnızca aynı bildirimde Türkçesi bulunan bir belgenin İngilizcesi indirilmez. Dil, dosya adından tahmin edilir; emin olunamazsa ya da bildirimin bütün ekleri İngilizceyse ek okunur. PDF'lerin metni ve tabloları, taranmış sayfalar ve resimler OCR ile çıkarılır. PDF ya da resim olmayan dosyalar (ör. Excel, Word) `unsupported` olarak kaydedilir.

## Dosyalar

```text
companies.txt         Bildirimleri toplanan şirketler
kap_watch.py          Canlı izleyici; bir bildirimi baştan sona işleyen ortak kod da burada
kap_history.py        Tarih aralığındaki geçmiş bildirimler
kap_aws.py            AWS'de geçmiş: kap_history.py'nin işini yapar, sonuçları S3'e yazar
aws/                  AWS kurulumu, çalıştırma ve durdurma betikleri, squid proxy imajı
kap_disclosures.py    Bildirim listesini çeker
kap_details.py        Bildirim sayfasını çeker ve ayrıştırır
kap_financials.py     Finansal raporlardan mali tabloları çıkarır
kap_attachments.py    Eklerden metin ve tablo çıkarır, OCR yapar
kap_output.py         Bildirimin kaydını kurar ve JSON Lines dosyasına yazar
kap_http.py           KAP'ın istek sınırına karşı bekleme ve yeniden deneme
```

## Bilinmesi gerekenler

- **İstek sınırı.** KAP çok fazla istek gönderen IP'yi geçici olarak engeller. Engeli bazen HTTP 429 ile bildirir, bazen de bağlantıları hiç yanıt vermeden keser. Geçmiş komutu 12, canlı izleyici 4 işçiyle çalışır. KAP birkaç dakikada ~100 istekten sonra IP'yi birkaç dakikalığına engeller. İstekler arasında bekleme yoktur; engel ne kadar seyrek istekte de gelir. Engel olunca (HTTP 429, 403, 503 ya da yanıtsız kesilen bağlantı) herkes 4 dakika bekler, sonra devam eder. Hata alan diğer istekler beklemeden en fazla 5 kez denenir. İşçi sayıları `kap_history.py` ve `kap_watch.py` içindeki `WORKERS` değerleridir.
- **Yavaş indirmeler.** KAP ekleri her bağlantıya saniyede yaklaşık 10-15 KB ile verir. Bu yüzden her ekin indirilmesine boyutuna göre süre tanınır: en az 180 saniye, büyük dosyalarda saniyede 4 KB'a göre gereken süre. Örneğin bir bankanın 4,7 MB'lık finansal raporuna yaklaşık 19 dakika tanınır. KAP bazen dosyanın ortasında veri göndermeyi bırakır. 30 saniye hiç veri gelmezse indirme kesilip baştan denenir. Bu sürede inmeyen ek bir sonraki çalıştırmada yeniden denenir. Eki inmeyen bildirim o zamana kadar JSON Lines dosyasına girmez.
- **Yeniden ayrıştırma yok.** Ham sayfalar saklanmaz. Ayrıştırıcı değişirse eski satırlar kendiliğinden yenilenmez; o bildirimlerin KAP'tan yeniden çekilmesi gerekir.
- **Türkçe karakterler bozuksa** PowerShell'de `$env:PYTHONIOENCODING = "utf-8"` ayarlayın.
- **KAP değişebilir.** Ayrıştırıcılar KAP'ın Ekim 2026'daki sayfa düzenine göre yazıldı.
