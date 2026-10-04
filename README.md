# manzlytics-pipeline

[manzlytics](https://manzlytics.pages.dev) veri hattının işleme kodu: adsb.lol günlük
arşivlerinden (ODbL) uçak konumlarını ve GNSS girişimi için saatlik özetleri üretir.

Bu depo yalnızca geçmiş günleri toplu işlemek için kullanılır (`Geçmiş doldurma` iş akışı).
Sonuçlar bu depoda durmaz; ana projenin arşivine yüklenir. Kod ana projeden otomatik
kopyalanır, burada düzenlenmez.

Veri: [adsb.lol](https://adsb.lol), Open Database License (ODbL).
