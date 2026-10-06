"""Yayın öncesi sözleşme kapısı (yalnız sıfır satır): boş saatlik özet arşive yüklenmez.

`manzlytics` deposundaki `contract.ensure_nonempty` ile aynı kural ve mesaj (P1-4). O depodaki
tam kontrol (sütun/tip/sürüm bulguları) burada yok; bu kapı yalnızca `jamming_ac_hourly_r5.parquet`
0 satırken günün yayınlanmasını engeller. 2026-10-06: 7 gerçek gün böyle yayınlanmıştı.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq


def ensure_nonempty(path: Path) -> pq.ParquetFile:
    """Saatlik parquet'i footer'dan açar; 0 satırsa `ValueError` (gün kullanılamaz)."""
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows == 0:
        raise ValueError(f"{path.name}: saatlik özet boş (0 satır); gün kullanılamaz")
    return parquet
