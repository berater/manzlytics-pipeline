"""Gece işinin iş planı: hangi günler eksik, hangisi önce işlenir (uzlaştırma / reconcile).

Elle tetiklenen "geriye dönük doldurma" yerine her zamanlanmış `archive.yml` koşusu aynı soruyu
sorar: "pencere (`--from` … dün) içinde eksik gün var mı?" Cevap deterministiktir, bu yüzden iş
kendini onarır: bir koşu düşerse ya da GitHub zamanlanmış koşuyu atlarsa sonraki koşu kaldığı
yerden sürer; kimsenin tetiklemesi gerekmez.

Bir gün için durum:
  - `process`: kalıcı arşivde (release) manifest yok → adsb.lol'dan işlenmeli (≈ 45 dk).
  - `export`: arşivde var ama statik veri eksik (`REQUIRED_STATIC`) → yalnız dışa aktar (≈ 9 dk).
  - tamam: ikisi de var → atlanır.

Sıra en yeniden eskiye: dün her zaman önce gelir, geçmiş onun arkasından dolar. Sürekli hata
veren gün (bozuk kaynak vb.) `pipeline-state` release'indeki `failures.json`'a yazılır;
`max_attempts` denemeden sonra `cooldown_days` boyunca atlanır (Actions dakikası boşa gitmesin),
sonra yeniden denenir.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from manzlytics_ingest.release import (
    RELEASE_NOT_FOUND,
    Runner,
    _check,
    _gh,
    _run,
    gh_error_text,
    release_assets,
    release_tag,
)

STATE_TAG = "pipeline-state"
FAILURES_FILE = "failures.json"
REQUIRED_STATIC = ("timeline.json", "route-hours-r4.json", "events.json")
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_COOLDOWN_DAYS = 3


@dataclass(frozen=True)
class PlanItem:
    day: date
    mode: str  # "process" | "export"


def select_days(
    start: date,
    end: date,
    archived: set[date],
    exported: set[date],
    failures: dict[str, dict] | None = None,
    now: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
) -> list[PlanItem]:
    """Eksik günleri en yeniden eskiye sıralar; karantinadaki günleri dışarıda bırakır."""
    failures = failures or {}
    now = now or datetime.now(UTC)
    items: list[PlanItem] = []
    day = end
    while day >= start:
        if not (day in archived and day in exported) and not _quarantined(
            failures.get(day.isoformat()), now, max_attempts, cooldown_days
        ):
            items.append(PlanItem(day, "export" if day in archived else "process"))
        day -= timedelta(days=1)
    return items


def _quarantined(entry: dict | None, now: datetime, max_attempts: int, cooldown_days: int) -> bool:
    if not entry or entry.get("count", 0) < max_attempts:
        return False
    last = datetime.fromisoformat(entry["last"])
    return now - last < timedelta(days=cooldown_days)


def record_failure(
    failures: dict[str, dict], day: date, error: str, now: datetime | None = None
) -> dict[str, dict]:
    now = now or datetime.now(UTC)
    count = failures.get(day.isoformat(), {}).get("count", 0)
    # Karantina süresi dolduktan sonraki ilk hata sayacı baştan başlatır: bir deneme daha hakkı.
    if count >= DEFAULT_MAX_ATTEMPTS and _expired(failures[day.isoformat()], now):
        count = 0
    return {
        **failures,
        day.isoformat(): {"count": count + 1, "last": now.isoformat(), "error": error[:300]},
    }


def _expired(entry: dict, now: datetime) -> bool:
    return now - datetime.fromisoformat(entry["last"]) >= timedelta(days=DEFAULT_COOLDOWN_DAYS)


def record_success(failures: dict[str, dict], day: date) -> dict[str, dict]:
    return {k: v for k, v in failures.items() if k != day.isoformat()}


def exported_days(static_dir: Path) -> set[date]:
    """Güncel statik veri dosyalarının hepsi (`REQUIRED_STATIC`) olan gün klasörleri.

    Dışa aktarmaya yeni bir dosya eklenince eski günler otomatik "eksik" olur ve `export`
    modunda yeniden üretilir (N12: `route-hours-r4.json`, N16: `events.json`).
    """
    days = set()
    for p in static_dir.glob("????-??-??/timeline.json"):
        try:
            day = date.fromisoformat(p.parent.name)
        except ValueError:
            continue
        if all((p.parent / name).is_file() for name in REQUIRED_STATIC):
            days.add(day)
    return days


def archived_days(start: date, end: date, repo: str | None = None, run: Runner = _run) -> set[date]:
    """Release'lerde manifest'i olan günler (manifest en son yüklenir: var = gün tam).

    Yalnızca release'i hiç olmayan ay boş sayılır. Başka her `gh` hatası (auth, rate-limit, ağ…)
    `RuntimeError` olarak yükselir; böylece "veri yok" ile "bilinmiyor" karışmaz ve kısmi sonuç
    dönmez.
    """
    days: set[date] = set()
    months = sorted({(d.year, d.month) for d in _span(start, end)})
    for year, month in months:
        assets = release_assets(release_tag(date(year, month, 1)), repo, run)
        if assets is None:  # o ayın release'i henüz yok
            continue
        for asset in assets:
            name = asset["name"]
            if name.startswith("adsblol-") and name.endswith("-manifest.json"):
                try:
                    days.add(date.fromisoformat(name.removeprefix("adsblol-")[:10]))
                except ValueError:
                    continue
    return days


def _span(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# `gh release download --pattern` hiçbir varlıkla eşleşmezse verdiği mesaj (release var, dosya yok).
_NO_ASSET = "no assets match"


def load_failures(repo: str | None = None, run: Runner = _run) -> dict[str, dict]:
    """Karantina sayaçları. Release ya da dosya henüz yoksa `{}` (hiç hata kaydı yok).

    Başka her hata `RuntimeError`: indirme hatasını "kayıt yok" sayıp sayaçları sıfırlamayız.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [*_gh(repo), "release", "download", STATE_TAG, "--pattern", FAILURES_FILE]
        p = run([*cmd, "--dir", tmp])
        path = Path(tmp) / FAILURES_FILE
        if p.returncode != 0:
            err = gh_error_text(p)
            if RELEASE_NOT_FOUND in err.lower() or _NO_ASSET in err.lower():
                return {}
            raise RuntimeError(f"{STATE_TAG} {FAILURES_FILE} indirme başarısız: {err}")
        if not path.is_file():
            return {}
        try:
            failures = json.loads(path.read_text())
        except ValueError as e:
            raise RuntimeError(
                f"{STATE_TAG} {FAILURES_FILE} bozuk (geçerli JSON değil): {e}"
            ) from e
        if not isinstance(failures, dict):
            raise RuntimeError(f"{STATE_TAG} {FAILURES_FILE} bozuk: nesne bekleniyordu")
        return failures


def save_failures(failures: dict[str, dict], repo: str | None = None, run: Runner = _run) -> None:
    if run([*_gh(repo), "release", "view", STATE_TAG]).returncode != 0:
        notes = "Boru hattı durumu (otomatik): başarısız günlerin deneme sayacı. Elle düzenlemeyin."
        cmd = [*_gh(repo), "release", "create", STATE_TAG, "--title", STATE_TAG, "--notes", notes]
        _check(run([*cmd, "--latest=false"]), f"release {STATE_TAG} oluşturma")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / FAILURES_FILE
        path.write_text(json.dumps(failures, ensure_ascii=False, indent=1, sort_keys=True))
        cmd = [*_gh(repo), "release", "upload", STATE_TAG, str(path), "--clobber"]
        _check(run(cmd), "failures.json yükleme")


def format_plan(items: Sequence[PlanItem]) -> str:
    return "\n".join(f"{i.day.isoformat()} {i.mode}" for i in items)
