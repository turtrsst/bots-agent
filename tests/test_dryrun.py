from __future__ import annotations

import pytest

from studio.config import Settings
from studio.dryrun import doctor_snapshot, run_dry_run


def test_doctor_snapshot_reports_checks(settings: Settings) -> None:
    snapshot = doctor_snapshot(settings)
    names = {check["name"] for check in snapshot["checks"]}
    assert {"python", "sqlite memori", "mesin TTS"} <= names
    assert isinstance(snapshot["ok"], bool)
    assert all(check["status"] in {"✅", "⚠️", "❌"} for check in snapshot["checks"])


def test_dry_run_passes_without_fastapi(settings: Settings, monkeypatch, capsys) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in {"fastapi", "httpx"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    code = run_dry_run(settings)
    output = capsys.readouterr().out
    assert code == 0
    assert "FASE 1" in output and "RINGKASAN" in output
    assert "fastapi/httpx belum terpasang" in output


def test_dry_run_hits_endpoints_in_process(settings: Settings, capsys) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    code = run_dry_run(settings)
    output = capsys.readouterr().out
    assert code == 0
    assert "dashboard /" in output
    assert "publish shorts" in output
    assert "rekap mingguan" in output
