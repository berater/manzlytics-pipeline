"""ARCHIVE_TOKEN yalnız `archive-publish` komutunda görünür; işleme adımları token'sız çalışır."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_process_days_gives_token_only_to_archive_publish(tmp_path):
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\necho "$3 token=${GH_TOKEN:-none} ${GITHUB_TOKEN:-}" >>"$CALLS_LOG"\n'
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS_LOG": str(log),
        "DAYS": "2026-09-25",
        "TARGET_REPO": "o/r",
        "ARCHIVE_TOKEN": "dummy-not-a-secret",
        "GH_TOKEN": "inherited-should-be-dropped",
        "GITHUB_TOKEN": "inherited-should-be-dropped",
    }
    p = subprocess.run(
        ["bash", str(ROOT / "scripts/process_days.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    calls = dict(line.split(" ", 1) for line in log.read_text().splitlines())
    assert calls["daily"].startswith("token=none")
    assert calls["check-contract"].startswith("token=none")
    assert calls["archive-publish"].startswith("token=dummy-not-a-secret")


def test_workflows_do_not_export_gh_token_to_process_step():
    for name in ("daily", "backfill"):
        text = (ROOT / f".github/workflows/{name}.yml").read_text()
        process = text.split("  process:")[1]
        code = "\n".join(
            l for l in process.splitlines() if not l.lstrip().startswith("#")
        )
        assert "GH_TOKEN" not in code, name
        assert "ARCHIVE_TOKEN: ${{ secrets.ARCHIVE_TOKEN }}" in process
