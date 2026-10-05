# manzlytics-pipeline

[manzlytics](https://manzlytics.pages.dev) veri hattının işleme kodu: adsb.lol günlük
arşivlerinden (ODbL) uçak konumlarını ve GNSS girişimi için saatlik özetleri üretir.

Bu depo **herkese açık** olduğu için GitHub Actions dakikası ücretsizdir; ana proje (private)
ağır işlemeyi burada yaptırır. İki iş akışı var:

- `Günlük işleme` (`daily.yml`, günde 4 kez): dün ve önceki 2 günü işler; kaynak günü henüz
  yayınlamadıysa sonraki koşu yeniden dener. Elle: `days_back` ile bir hafta gibi daha geriye bakılabilir.
- `Geçmiş doldurma` (`backfill.yml`, elle): geçmiş günleri toplu işler.

Sonuçlar bu depoda durmaz; ana projenin `archive-YYYY-MM` Releases'ine yüklenir. Ana projedeki
`Arşiv` iş akışı (`EXPORT_ONLY=true`) yalnız o arşivi alıp statik veriye dönüştürür. Kod ana
projeden otomatik kopyalanır, burada düzenlenmez.

Veri: [adsb.lol](https://adsb.lol), Open Database License (ODbL).
