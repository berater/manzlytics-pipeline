"""Şema göçleri: infra/clickhouse/init/*.sql dosyalarını sırayla uygular.

Dosyalar idempotent yazılır (CREATE ... IF NOT EXISTS). Docker compose aynı dosyaları
ilk açılışta otomatik yükler; bu komut docker dışı ortamlar ve yeni göçler içindir.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from manzlytics_common.clickhouse import ClickHouse

# Paketler workspace içinde düzenlenebilir (editable) kurulur; SQL repodan okunur.
REPO_SQL = Path(__file__).resolve().parents[4] / "infra" / "clickhouse" / "init"


def sql_dir() -> Path:
    return REPO_SQL


def split_statements(sql: str) -> list[str]:
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def migrate(ch: ClickHouse, directory: Path | None = None) -> int:
    """SQL dosyalarındaki `manzlytics` veritabanı adı, istemcinin veritabanıyla değiştirilir
    (testler ayrı bir veritabanında çalışır)."""
    n = 0
    for path in sorted((directory or sql_dir()).glob("*.sql")):
        sql = re.sub(r"\bmanzlytics\b", ch.database, path.read_text())
        for stmt in split_statements(sql):
            ch.command(stmt, use_db=False)
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mz-db")
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args(argv)
    n = migrate(ClickHouse.from_env())
    print(f"{n} ifade uygulandı")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
