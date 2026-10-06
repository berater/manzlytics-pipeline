"""Kalıcı arşiv: günlük dosyaları bu reponun GitHub Releases'ına yükler ve geri indirir (#16).

Karar ve gerekçe: docs/veri-stratejisi.md §8 (ücretsiz, kartsız; dosya başına < 2 GiB,
release başına ≤ 1000 dosya). Düzen: ayda bir release (`archive-2026-09`), her gün için
`<kaynak>-<gün>-<dosya>` adlı varlıklar (release içi düz liste olduğu için gün adın içinde).

Manifest en son yüklenir: manifest'i olan gün tamdır. Aynı gün yeniden yüklenirse dosyalar
değiştirilir (`--clobber`), yani iş idempotenttir. Geri yüklemede her dosyanın boyutu ve
SHA-256'sı manifest'le karşılaştırılır.

GitHub ile konuşmayı `gh` komutu yapar (Actions'ta kurulu; yetki `GH_TOKEN` ile).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from manzlytics_ingest.archive import day_dir, file_sha256
from manzlytics_ingest.sources import SOURCES

MANIFEST = "manifest.json"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def release_tag(day: date) -> str:
    return f"archive-{day:%Y-%m}"


def asset_prefix(source: str, day: date) -> str:
    return f"{source}-{day.isoformat()}-"


def day_files(manifest: dict) -> list[str]:
    """Manifest'in listelediği dosyalar (konumlar + yan dosyalar); manifest hariç."""
    names = ["positions.parquet"]
    names += [v["file"] for v in manifest.values() if isinstance(v, dict) and "file" in v]
    return names


def _check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode != 0:
        raise RuntimeError(f"{what} başarısız: {p.stderr.strip() or p.stdout.strip()}")


def _gh(repo: str | None) -> list[str]:
    return ["gh"] if repo is None else ["gh", "-R", repo]


# gh'nin "release yok" mesajı. Yalın "HTTP 404" bunun yerine geçmez: yetkisiz token ya da yanlış
# depo da 404 görür ve "veri yok" diye yutulmamalı.
RELEASE_NOT_FOUND = "release not found"


def gh_error_text(p: subprocess.CompletedProcess) -> str:
    """`gh` hatasının okunur metni; stderr boşsa çıkış kodu."""
    return p.stderr.strip() or p.stdout.strip() or f"gh çıkış kodu {p.returncode}"


def release_assets(tag: str, repo: str | None = None, run: Runner = _run) -> list[dict] | None:
    """Release'in varlık listesi; release hiç yoksa `None`.

    Yalnızca gh'nin açık "release not found" cevabı `None` döner. Auth, rate-limit, ağ, API ve
    diğer her hata `RuntimeError`: "release yok" ile "bilinmiyor" karıştırılmamalı.
    """
    p = run([*_gh(repo), "release", "view", tag, "--json", "assets"])
    if p.returncode != 0:
        err = gh_error_text(p)
        if RELEASE_NOT_FOUND in err.lower():
            return None
        raise RuntimeError(f"release {tag} sorgusu başarısız: {err}")
    try:
        return json.loads(p.stdout).get("assets", [])
    except ValueError as e:
        raise RuntimeError(f"release {tag} cevabı çözülemedi: {e}") from e


def ensure_release(tag: str, source: str, repo: str | None = None, run: Runner = _run) -> None:
    if run([*_gh(repo), "release", "view", tag]).returncode == 0:
        return
    src = SOURCES[source]
    notes = (
        f"Günlük arşiv ({tag.removeprefix('archive-')}). Kaynak: {src.name}. "
        f"Lisans: {src.license}; atıf: {src.attribution}. "
        "Her gün için manifest.json dosyaları, kapsamı ve özetleri (SHA-256) listeler. "
        "Geri yükleme: `mz-ingest archive-restore --date YYYY-MM-DD` (docs/veri-stratejisi.md §9)."
    )
    # Veri deposu; sürüm değil. `--latest=false` sitenin "son sürüm" bağlantısını değiştirmez.
    cmd = [*_gh(repo), "release", "create", tag, "--title", tag, "--notes", notes]
    _check(run([*cmd, "--latest=false"]), f"release {tag} oluşturma")


def publish_day(
    out: Path, source: str, day: date, repo: str | None = None, run: Runner = _run
) -> list[str]:
    """Günün dosyalarını yükler; yüklenen varlık adlarını döner (manifest en sonda)."""
    d = day_dir(out, source, day)
    manifest = json.loads((d / MANIFEST).read_text())
    names = [*day_files(manifest), MANIFEST]
    missing = [n for n in names if not (d / n).is_file()]
    if missing:
        raise FileNotFoundError(f"{d}: eksik dosya {missing}")

    tag, prefix = release_tag(day), asset_prefix(source, day)
    ensure_release(tag, source, repo, run)
    uploaded = []
    with tempfile.TemporaryDirectory() as tmp:
        # gh varlığı dosya adıyla yükler: gün önekli adlarla bağlantı oluştur
        for name in names:
            link = Path(tmp) / f"{prefix}{name}"
            link.symlink_to((d / name).resolve())
            cmd = [*_gh(repo), "release", "upload", tag, str(link), "--clobber"]
            _check(run(cmd), f"{link.name} yükleme")
            uploaded.append(link.name)
    return uploaded


def verify_day(d: Path) -> dict:
    """Günün dosyalarını manifest'teki boyut ve SHA-256 ile karşılaştırır; manifest'i döner."""
    manifest = json.loads((d / MANIFEST).read_text())
    entries = {"positions.parquet": manifest}
    entries |= {v["file"]: v for v in manifest.values() if isinstance(v, dict) and "file" in v}
    for name, entry in entries.items():
        path = d / name
        if not path.is_file():
            raise FileNotFoundError(f"{path} yok")
        if path.stat().st_size != entry["bytes"]:
            raise ValueError(f"{name}: boyut {path.stat().st_size} ≠ {entry['bytes']}")
        if "sha256" in entry and file_sha256(path) != entry["sha256"]:
            raise ValueError(f"{name}: SHA-256 manifest'le uyuşmuyor")
    return manifest


def restore_day(
    out: Path, source: str, day: date, repo: str | None = None, run: Runner = _run
) -> Path:
    """Günü arşivden `out/<kaynak>/YYYY/MM/DD/` altına indirir ve doğrular."""
    tag, prefix = release_tag(day), asset_prefix(source, day)
    d = day_dir(out, source, day)
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        cmd = [*_gh(repo), "release", "download", tag, "--pattern", f"{prefix}*", "--dir", tmp]
        _check(run(cmd), f"{tag} indirme")
        got = sorted(Path(tmp).iterdir())
        if not any(p.name == prefix + MANIFEST for p in got):
            raise FileNotFoundError(f"{tag} içinde {prefix}{MANIFEST} yok (gün yüklenmemiş)")
        for p in got:
            p.replace(d / p.name.removeprefix(prefix))
    verify_day(d)
    return d
