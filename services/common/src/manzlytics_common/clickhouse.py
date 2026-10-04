"""Bağımlılıksız, küçük ClickHouse HTTP istemcisi.

Ortam değişkenleri: CLICKHOUSE_URL, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD, CLICKHOUSE_DB.
Parametreler sunucu tarafı `{name:Type}` sözdizimiyle gönderilir (SQL enjeksiyonu yok).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class ClickHouseError(RuntimeError):
    pass


DEFAULT_URL = "http://localhost:8123"
DEFAULT_USER = "default"
DEFAULT_DATABASE = "manzlytics"


@dataclass(frozen=True, slots=True)
class ClickHouse:
    url: str = DEFAULT_URL
    user: str = DEFAULT_USER
    password: str = ""
    database: str = DEFAULT_DATABASE
    timeout: float = 60

    @classmethod
    def from_env(cls) -> ClickHouse:
        # Not: slots=True dataclass'ta `cls.alan` varsayılan değeri değil bir descriptor döndürür;
        # varsayılanlar bu yüzden modül sabitlerinden okunur.
        return cls(
            url=os.environ.get("CLICKHOUSE_URL", DEFAULT_URL),
            user=os.environ.get("CLICKHOUSE_USER", DEFAULT_USER),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_DB", DEFAULT_DATABASE),
        )

    def _request(
        self, body: bytes, params: Mapping[str, Any] | None, database: str | None
    ) -> bytes:
        qs: dict[str, str] = {}
        if database:
            qs["database"] = database
        for k, v in (params or {}).items():
            qs[f"param_{k}"] = str(v)
        url = f"{self.url}/?{urllib.parse.urlencode(qs)}"
        req = urllib.request.Request(url, data=body, method="POST")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise ClickHouseError(e.read().decode(errors="replace").strip()) from e

    def command(
        self, sql: str, params: Mapping[str, Any] | None = None, use_db: bool = True
    ) -> None:
        self._request(sql.encode(), params, self.database if use_db else None)

    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        out = self._request(f"{sql}\nFORMAT JSONEachRow".encode(), params, self.database)
        return [json.loads(line) for line in out.splitlines() if line]

    def insert(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        lines = [json.dumps(r, separators=(",", ":")) for r in rows]
        if not lines:
            return 0
        sql = f"INSERT INTO {table} FORMAT JSONEachRow\n" + "\n".join(lines)
        self._request(sql.encode(), None, self.database)
        return len(lines)
