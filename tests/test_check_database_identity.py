"""scripts/check_database_identity.py with mocked engines only: no real connection, no SQL Server, no secrets."""
import pytest
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db.connection import DatabaseConfigError, DatabaseUnavailable
from scripts import check_database_identity as cdi


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeEngine:
    def __init__(self, database="dataops_demo_test", server="HOST\\SQLEXPRESS03", fail=None):
        self.answers = {cdi.STATEMENTS[0]: database, cdi.STATEMENTS[1]: server}
        self.fail, self.executed, self.connects = fail, [], 0

    def connect(self):
        self.connects += 1
        if self.fail:
            raise self.fail
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, *a, **k):
        sql = str(statement)
        self.executed.append(sql)
        return FakeResult(self.answers[sql])  # KeyError for any other statement: the script may not run it


def settings(**kw):
    return Settings(_env_file=None, db_name=kw.pop("db_name", "dataops_demo_test"), **kw)


def run(engine, **kw):
    return cdi.run(settings(**kw), lambda s: engine)


def test_a_matching_identity_exits_zero_and_runs_only_the_two_read_statements(capsys):
    engine = FakeEngine()
    assert run(engine) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "dataops_demo_test" in out and "SQLEXPRESS03" in out
    assert engine.executed == list(cdi.STATEMENTS) and engine.connects == 1


@pytest.mark.parametrize("actual", ["dataops_sim", "other_test", "DATAOPS_DEMO_TEST", "master"])
def test_a_different_database_exits_one(actual, capsys):
    assert run(FakeEngine(database=actual)) == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("value", [None, "", "   ", 7])
def test_an_unestablishable_identity_exits_one(value, capsys):
    assert run(FakeEngine(database=value)) == 1
    assert "cannot be established" in capsys.readouterr().out


@pytest.mark.parametrize("error", [OperationalError("SELECT", {}, Exception("Login failed for user 'sa' PWD=hunter2")), OSError("down"),
                                   DatabaseUnavailable("Could not connect"), DatabaseConfigError("incomplete")])
def test_a_connection_or_config_failure_exits_nonzero_without_leaking_secrets(error, capsys):
    code = run(FakeEngine(fail=error))
    out = capsys.readouterr().out
    assert code == 2 and "FAIL" in out and "hunter2" not in out and "Traceback" not in out


def test_a_failure_while_building_the_engine_is_handled():
    def broken(_s):
        raise DatabaseUnavailable("driver missing")
    assert cdi.run(settings(), broken) == 2


def test_the_output_never_contains_credentials_or_a_connection_string(capsys):
    s = Settings(_env_file=None, db_name="dataops_demo_test", db_auth_mode="sql", db_user="svc_user", db_password="s3cretvalue")
    cdi.run(s, lambda _s: FakeEngine())
    out = capsys.readouterr().out
    assert "s3cretvalue" not in out and "svc_user" not in out and "DRIVER=" not in out and "PWD" not in out


def test_only_the_two_read_statements_exist_in_the_script():
    from pathlib import Path
    import ast
    tree = ast.parse(Path(cdi.__file__).read_text(encoding="utf-8"))
    tree.body[0].value.value = ""                                   # drop the module docstring: only code is checked
    src = ast.unparse(tree)
    assert cdi.STATEMENTS == ("SELECT DB_NAME()", "SELECT @@SERVERNAME")
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "USE ", "EXEC", "wipe", "seed("):
        assert word not in src, word
    assert "make_engine" in src and "settings.db_name" in src


def test_the_allowlist_note_is_informational_only(capsys):
    assert run(FakeEngine(), recovery_target_allowlist="dataops_demo_test") == 0
    assert "is in RECOVERY_TARGET_ALLOWLIST" in capsys.readouterr().out
    assert run(FakeEngine()) == 0
    assert "is NOT in" in capsys.readouterr().out


def test_the_shared_redactor_masks_odbc_pwd_and_uid_fragments():
    from app.core.logging import redact
    out = redact("Login failed; UID=svc_user;PWD=hunter2; Server=x")
    assert "hunter2" not in out and "svc_user" not in out
