"""Yayın güvenliği: gerçek `gh` hatası "yok" sayılmaz; boş saatlik özet arşive yüklenmez."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from manzlytics_detector import cli, plan
from manzlytics_detector.daily import HOURLY_FILE
from manzlytics_ingest.archive import day_dir

DAY = date(2026, 9, 25)
ROOT = Path(__file__).resolve().parents[3]


def runner(rc=0, out="", err=""):
    def run(cmd):
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    return run


REAL_ERRORS = [
    "HTTP 401: Bad credentials",
    "HTTP 403: API rate limit exceeded",
    "HTTP 502: Bad Gateway",
    "error connecting to api.github.com",
    "HTTP 404: Not Found",  # yalın 404: yetkisiz token / yanlış depo da bunu görür
]


@pytest.mark.parametrize("err", REAL_ERRORS)
def test_archived_days_propagates_real_gh_errors(err):
    with pytest.raises(RuntimeError):
        plan.archived_days(DAY, DAY, "o/r", runner(1, err=err))


def test_archived_days_missing_release_is_empty_and_assets_are_read():
    assert plan.archived_days(DAY, DAY, "o/r", runner(1, err="release not found")) == set()
    assets = {"assets": [{"name": f"adsblol-{DAY}-manifest.json"}, {"name": "x"}]}
    assert plan.archived_days(DAY, DAY, "o/r", runner(0, out=json.dumps(assets))) == {DAY}


@pytest.mark.parametrize("err", REAL_ERRORS)
def test_load_failures_propagates_real_gh_errors(err):
    with pytest.raises(RuntimeError):
        plan.load_failures("o/r", runner(1, err=err))


@pytest.mark.parametrize("err", ["release not found", "no assets match the file pattern"])
def test_load_failures_absent_state_is_empty(err):
    assert plan.load_failures("o/r", runner(1, err=err)) == {}


def write_hourly(archive: Path, rows: int) -> None:
    d = day_dir(archive, "adsblol", DAY)
    d.mkdir(parents=True)
    pq.write_table(pa.table({"hour": pa.array(list(range(rows)), pa.int64())}), d / HOURLY_FILE)


def gate(archive: Path) -> int:
    return cli.main(["check-contract", "--date", str(DAY), "--archive", str(archive)])


def test_gate_fails_on_zero_rows_and_missing_file(tmp_path, capsys):
    write_hourly(tmp_path, 0)
    assert gate(tmp_path) == cli.EXIT_CONTRACT_ERROR == 12
    assert "saatlik özet boş" in capsys.readouterr().out
    assert gate(tmp_path / "yok") == 12


def test_gate_passes_filled_day(tmp_path):
    write_hourly(tmp_path, 3)
    assert gate(tmp_path) == 0


def run_script(tmp_path, gate_rc, daily_rc=0):
    """`process_days.sh`i sahte `uv` ile çalıştırır; çağrı sırasını döner."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin"
    fake.mkdir()
    uv = fake / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "$3" >>"$CALLS"\n'
        f'case "$3" in daily) exit {daily_rc};; check-contract) exit {gate_rc};; esac\nexit 0\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "CALLS": str(log)}
    env |= {"DAYS": "2026-09-25", "TARGET_REPO": "o/r", "ARCHIVE_TOKEN": "dummy", "GITHUB_STEP_SUMMARY": str(tmp_path / "s")}
    p = subprocess.run(
        ["bash", str(ROOT / "scripts/process_days.sh")], cwd=tmp_path, env=env, capture_output=True
    )
    return p.returncode, log.read_text().split()


def test_script_does_not_publish_when_gate_fails(tmp_path):
    rc, calls = run_script(tmp_path, gate_rc=12)
    assert rc == 1
    assert calls == ["daily", "check-contract"]


def test_script_publishes_after_gate_on_filled_day(tmp_path):
    rc, calls = run_script(tmp_path, gate_rc=0)
    assert rc == 0
    assert calls == ["daily", "check-contract", "archive-publish"]
