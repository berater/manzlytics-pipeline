"""adsb.lol `globe_history` günlük arşivi (ODbL).

Arşiv yıllara göre ayrı depolarda (`adsblol/globe_history_YYYY`) GitHub release'i olarak durur;
her gün için tercih edilen kopyanın parçaları `adsblol/globe_history` deposundaki
`PREFERRED_RELEASES.txt`'te listelenir. Parçalar art arda eklenince tek bir tar olur.

Tar içinde uçak başına bir iz dosyası vardır (`traces/xx/trace_full_<hex>.json`, gzip'li).
Biçim: https://github.com/wiedehopf/readsb/blob/dev/README-json.md#trace-jsons
NIC/NACp/SIL her noktada değil, yalnızca ayrıntı kaydı olan noktalarda (yaklaşık her 4 noktadan
birinde) gelir; bu değerler en fazla MAX_DETAIL_AGE_S saniye sonraki noktalara taşınır.
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from manzlytics_ingest.adsblol import PRIVACY_FLAGS, USER_AGENT, BBox
from manzlytics_ingest.models import PositionReport

PREFERRED_RELEASES = (
    "https://raw.githubusercontent.com/adsblol/globe_history/main/PREFERRED_RELEASES.txt"
)
YEAR_REPO = "https://github.com/adsblol/globe_history_{year}"
# `PREFERRED_RELEASES.txt` yalnız son ≈ 10 ayı listeler; daha eski günler yıllık depoda aynı gün
# için birden çok kopya olarak durur. Tercih sırası: prod, staging, test (mlatonly yalnız MLAT);
# `…tmp` son ek geçici yükleme adıdır (2025-05-28 … 2025-06-10 yalnız böyle var), en son denenir.
COPY_PREFERENCE = ("prod-0", "staging-0", "test-0", "test-1", "prod-0tmp", "staging-0tmp")
MAX_PARTS = 26 * 26  # .tar.aa … .tar.zz
FIRST_DAY = date(2023, 2, 16)
MAX_DETAIL_AGE_S = 60.0
# Saklanan odak bölge: Avrupa, Orta Doğu, Kafkasya, Kuzey Afrika kıyısı (veri-stratejisi.md §8)
ARCHIVE_BBOX = BBox(south=25.0, west=-12.0, north=72.0, east=65.0)

# İz noktası alanları (readsb README-json)
_DT, _LAT, _LON, _ALT, _GS, _TRACK, _FLAGS, _VRATE, _DETAILS, _TYPE, _ALT_GEOM = range(11)


class ArchiveNotPublished(LookupError):
    """Günün arşivi kaynakta (henüz) yok. adsb.lol dünü genellikle gün içinde yayınlar; gece
    işi bunu hata saymaz, sonraki çalıştırmada yeniden dener."""


def preferred_release(day: date, listing: str) -> list[str]:
    """`PREFERRED_RELEASES.txt` içinden o günün parça adreslerini bul."""
    tag = f"/v{day:%Y.%m.%d}-"
    for line in listing.splitlines():
        urls = [u.strip() for u in line.split(",") if u.strip()]
        if urls and tag in urls[0]:
            return urls
    raise ArchiveNotPublished(f"{day.isoformat()} için arşiv bulunamadı")


def year_tags(year: int) -> list[str]:
    """Yıllık arşiv deposunun etiketleri (API kotası harcamamak için `git ls-remote`)."""
    p = subprocess.run(
        ["git", "ls-remote", "--tags", f"{YEAR_REPO.format(year=year)}.git"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    refs = (line.split("refs/tags/", 1)[-1] for line in p.stdout.splitlines())
    return [r for r in refs if r and not r.endswith("^{}")]


def pick_tag(day: date, tags: list[str]) -> str | None:
    """Günün yıllık depodaki tercih edilen kopyası."""
    available = set(tags)
    for copy in COPY_PREFERENCE:
        tag = f"v{day:%Y.%m.%d}-planes-readsb-{copy}"
        if tag in available:
            return tag
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def url_exists(url: str) -> bool:
    """Release varlığı var mı? GitHub varlık adresi indirme sunucusuna yönlendirir (302)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=60):
            return True
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return True
        if e.code == 404:
            return False
        raise


def _part_suffixes() -> Iterator[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    for i in range(MAX_PARTS):
        yield letters[i // 26] + letters[i % 26]


def release_parts(
    day: date, tag: str, exists: Callable[[str], bool] = url_exists, year: int | None = None
) -> list[str]:
    """Etiketin tar adresleri: tek parça `.tar` ya da `.tar.aa`, `.tar.ab`, …"""
    base = f"{YEAR_REPO.format(year=year or day.year)}/releases/download/{tag}/{tag}.tar"
    if exists(base):
        return [base]
    urls = []
    for suffix in _part_suffixes():
        if not exists(f"{base}.{suffix}"):
            break
        urls.append(f"{base}.{suffix}")
    if not urls:
        raise ArchiveNotPublished(f"{day.isoformat()}: {tag} etiketinde tar bulunamadı")
    return urls


def resolve_release(
    day: date,
    listing: str,
    tags_for_year: Callable[[int], list[str]] = year_tags,
    exists: Callable[[str], bool] = url_exists,
) -> list[str]:
    """Günün parça adresleri: önce `PREFERRED_RELEASES.txt`, yoksa yıllık depodaki kopya."""
    try:
        return preferred_release(day, listing)
    except ArchiveNotPublished:
        pass
    # Yıl sonu/başı günleri komşu yılın deposunda durabilir (31 Aralık → sonraki yıl).
    errors = []
    for year in (day.year, day.year + 1, day.year - 1):
        try:
            tags = tags_for_year(year)
        except (subprocess.SubprocessError, OSError) as e:
            # Örneğin yılın deposu henüz açılmamış (1 Ocak): o depoda gün yok demektir.
            errors.append(f"{year} deposu okunamadı ({e})")
            continue
        if tag := pick_tag(day, tags):
            return release_parts(day, tag, exists, year)
    detail = f" ({'; '.join(errors)})" if errors else ""
    raise ArchiveNotPublished(f"{day.isoformat()} için arşiv bulunamadı{detail}")


def _open(url: str, timeout: float = 60):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout
    )


def fetch_listing() -> str:
    with _open(PREFERRED_RELEASES) as resp:
        return resp.read().decode()


DOWNLOAD_ATTEMPTS = 3


def _download_one(url: str, path: Path, chunk: int) -> None:
    """Tek parçayı indir; boyut `Content-Length` ile uyuşmazsa (yarım indirme) yeniden dene."""
    tmp = path.with_suffix(path.suffix + ".part")
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with _open(url, timeout=300) as resp, open(tmp, "wb") as f:
                expected = resp.headers.get("Content-Length")
                while block := resp.read(chunk):
                    f.write(block)
            if expected is not None and tmp.stat().st_size != int(expected):
                raise OSError(f"eksik indirme: {tmp.stat().st_size} / {expected} bayt")
            tmp.rename(path)
            return
        except OSError:
            tmp.unlink(missing_ok=True)
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(5 * attempt)


def download(urls: list[str], dest: Path, chunk: int = 8 << 20) -> list[Path]:
    """Parçaları indir; tamamlanmış parça tekrar indirilmez (`.part` → yeniden adlandırma)."""
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for url in urls:
        path = dest / url.rsplit("/", 1)[-1]
        if not path.exists():
            _download_one(url, path, chunk)
        paths.append(path)
    return paths


class _Concat(io.RawIOBase):
    """Parça dosyalarını tek bir akış gibi okur (diske birleşik tar yazmadan)."""

    def __init__(self, paths: list[Path]):
        self._paths = list(paths)
        self._f = None

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while True:
            if self._f is None:
                if not self._paths:
                    return 0
                self._f = open(self._paths.pop(0), "rb")
            n = self._f.readinto(b)
            if n:
                return n
            self._f.close()
            self._f = None


def fetch_day(day: date, cache: Path) -> tuple[list[str], list[Path]]:
    """Günün tercih edilen arşivini `cache/<gün>/` altına indir; (adresler, parçalar)."""
    if day < FIRST_DAY:
        raise SystemExit(f"arşiv {FIRST_DAY.isoformat()} tarihinden başlıyor")
    urls = resolve_release(day, fetch_listing())
    return urls, download(urls, cache / day.isoformat())


def iter_trace_files(parts: list[Path]) -> Iterator[bytes]:
    """Tar akışındaki iz dosyalarının ham içeriği (heatmap, acas vb. atlanır)."""
    stream = io.BufferedReader(_Concat(parts), buffer_size=8 << 20)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            name = member.name.removeprefix("./")
            if member.isfile() and name.startswith("traces/"):
                f = tar.extractfile(member)
                if f is not None:
                    yield f.read()


def load_trace(raw: bytes) -> dict:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _num(value) -> float | None:
    # "ground" gibi sayı olmayan değerler boş sayılır
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_trace(trace: dict, bbox: BBox | None = None) -> Iterator[PositionReport]:
    """Tek uçağın günlük izini PositionReport'lara çevirir.

    NIC/NACp/SIL ve çağrı kodu son ayrıntı kaydından gelir; kayıt MAX_DETAIL_AGE_S'den
    eskiyse NIC boş kalır. bbox verilirse yalnızca içindeki noktalar döner (ayrıntı kaydı
    bbox dışında olsa bile sonraki noktalara taşınır).
    """
    base = float(trace["timestamp"])
    icao24 = trace["icao"].lower().removeprefix("~")
    anonymous = bool(int(trace.get("dbFlags") or 0) & PRIVACY_FLAGS)
    detail_ts: float | None = None
    nic = nac_p = sil = None
    callsign = None
    for p in trace.get("trace", []):
        ts = base + p[_DT]
        details = p[_DETAILS] if len(p) > _DETAILS else None
        if isinstance(details, dict):
            detail_ts = ts
            nic, nac_p, sil = details.get("nic"), details.get("nac_p"), details.get("sil")
            callsign = (details.get("flight") or "").strip() or callsign
        lat, lon = p[_LAT], p[_LON]
        if lat is None or lon is None:
            continue
        if bbox is not None and not bbox.contains(lat, lon):
            continue
        fresh = detail_ts is not None and ts - detail_ts <= MAX_DETAIL_AGE_S
        alt = p[_ALT]
        on_ground = alt == "ground"
        src = p[_TYPE] if len(p) > _TYPE and p[_TYPE] else "unknown"
        yield PositionReport(
            ts=datetime.fromtimestamp(ts, tz=UTC),
            icao24=icao24,
            lat=float(lat),
            lon=float(lon),
            alt_baro_ft=None if on_ground else _num(alt),
            alt_geom_ft=_num(p[_ALT_GEOM]) if len(p) > _ALT_GEOM else None,
            callsign=callsign,
            nic=nic if fresh else None,
            nac_p=nac_p if fresh else None,
            sil=sil if fresh else None,
            on_ground=on_ground,
            source=f"adsblol:{src}",
            anonymous=anonymous,
            gs_kt=_num(p[_GS]),
            track=_num(p[_TRACK]),
            nic_age_s=round(ts - detail_ts, 2) if fresh else None,
        )
