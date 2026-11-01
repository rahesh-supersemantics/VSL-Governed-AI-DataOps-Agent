"""Profiler hardening: fail-closed read-only check, local-server check, honest and atomic reports. Fakes only."""

from __future__ import annotations

import json

import pytest

from scripts import profile_olist_database as prof
from tests.dq_support import FakeConnection, FakeInspector, FakeSettings, install_fake_run, read_only_row


class ProfileInspector(FakeInspector):
    columns = {**FakeInspector.columns, "ecom_order_payments": {"order_id", "payment_sequential"}}

    def get_columns(self, table, schema=None):
        return [{"name": name, "type": "NVARCHAR(32)", "nullable": True} for name in sorted(self.columns[table])]


@pytest.fixture
def fakes(monkeypatch):
    def install(connection, settings=None):
        engine = install_fake_run(monkeypatch, prof, connection, settings)
        monkeypatch.setattr(prof, "inspect", lambda _c: ProfileInspector())
        return engine
    return install


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_verified_profile_completes_and_reports_computed_read_only(tmp_path, fakes, capsys):
    connection = FakeConnection()
    engine = fakes(connection)
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 0 and report["status"] == "completed"
    assert report["read_only_verified"] is True and "read_only" not in report
    assert report["target"]["connected_database"] == "dataops_ecommerce_demo" and report["target"]["server_is_local"] is True
    assert set(report["relationships"]) == {n for n, *_ in prof.RELATIONSHIPS}
    assert set(report["business_checks"]) == set(prof.BUSINESS_SQL)
    assert report["tables"]["ecom_orders"]["candidate_key"] == ["order_id"]
    assert connection.only_selects() and engine.disposed
    assert read(tmp_path / "p.json")["run_id"] == report["run_id"]
    assert "No database write statements were issued" in capsys.readouterr().out


def test_wrong_database_stops_before_any_profiling_query(tmp_path, fakes):
    connection = FakeConnection(database="dataops_sim")
    fakes(connection)
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 1 and report["status"] == "failed"
    assert [s for s, _ in connection.executed] == ["SELECT DB_NAME()"]


def test_write_capable_principal_fails_closed_before_profiling(tmp_path, fakes):
    connection = FakeConnection(permission_row=read_only_row(is_db_datawriter=1))
    fakes(connection)
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 1 and report["status"] == "failed" and report["read_only_verified"] is False
    assert "could not be verified" in report["errors"][0]
    assert connection.rule_statements() == []
    assert len(connection.executed) == 2  # DB_NAME() and the permission query only


def test_inconclusive_permission_check_fails_closed(tmp_path, fakes):
    fakes(FakeConnection(permission_row=read_only_row(can_insert=None)))
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 1 and report["read_only_verification"]["status"] == "inconclusive"


def test_override_profiles_but_never_claims_verification(tmp_path, fakes, capsys):
    connection = FakeConnection(permission_row=read_only_row(is_sysadmin=1))
    fakes(connection)
    code, report = prof.run_profile(tmp_path / "p.json", allow_unverified_read_only=True)
    assert code == 0 and report["read_only_verified"] is False
    assert any("NOT verified" in w for w in report["warnings"])
    assert "WARNING" in capsys.readouterr().out and connection.only_selects()


def test_non_local_server_is_refused_before_connecting(tmp_path, monkeypatch):
    monkeypatch.setattr(prof, "get_settings", lambda: type("S", (), {"db_server": "prod-sql.example.com"})())
    monkeypatch.setattr(prof, "make_engine", lambda *a, **k: pytest.fail("must not connect to a non-local server"))
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 1 and report["status"] == "failed" and report["target"]["server_is_local"] is None


def test_failed_run_replaces_a_stale_completed_report_and_preserves_it(tmp_path, fakes):
    path = tmp_path / "p.json"
    fakes(FakeConnection())
    _, good = prof.run_profile(path)
    fakes(FakeConnection(permission_row=read_only_row(is_sysadmin=1)))
    code, bad = prof.run_profile(path)
    assert code == 1
    assert read(path)["status"] == "failed" and read(path)["run_id"] == bad["run_id"]
    archived = list(tmp_path.glob("p.*.json"))
    assert len(archived) == 1 and read(archived[0])["run_id"] == good["run_id"]


def test_unexpected_errors_are_redacted_in_the_report_and_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prof, "get_settings", lambda: FakeSettings())

    def boom(*_a, **_k):
        raise RuntimeError("Login failed. password=hunter2 token=abc123")

    monkeypatch.setattr(prof, "make_engine", boom)
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 1
    text = (tmp_path / "p.json").read_text(encoding="utf-8") + capsys.readouterr().out
    assert "hunter2" not in text and "abc123" not in text


def test_missing_tables_are_reported_as_skipped_checks_not_silently_dropped(tmp_path, monkeypatch):
    class Sparse(ProfileInspector):
        columns = {"ecom_customers": {"customer_id"}}

    install_fake_run(monkeypatch, prof, FakeConnection())
    monkeypatch.setattr(prof, "inspect", lambda _c: Sparse())
    code, report = prof.run_profile(tmp_path / "p.json")
    assert code == 0
    skipped = {s["check"] for s in report["skipped_checks"]}
    assert {n for n, *_ in prof.RELATIONSHIPS} <= skipped and set(prof.BUSINESS_SQL) <= skipped


def test_main_parses_flags_and_returns_the_exit_code(tmp_path, fakes):
    fakes(FakeConnection(permission_row=read_only_row(is_sysadmin=1)))
    assert prof.main(["--report-path", str(tmp_path / "m.json")]) == 1
    assert prof.main(["--report-path", str(tmp_path / "m2.json"), "--allow-unverified-read-only"]) == 0


def test_default_report_path_is_anchored_to_the_repository():
    assert prof.DEFAULT_REPORT_PATH.is_absolute() and prof.DEFAULT_REPORT_PATH.parent.name == "reports"
