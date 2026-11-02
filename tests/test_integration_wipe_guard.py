"""The SQL Server suite wipes its target, so it needs a typed confirmation and refuses the shared demo database. Offline."""
import pytest

from app.config import Settings
from app.db.safety import UnsafeTarget, assert_safe_integration_target, assert_wipe_confirmed


@pytest.mark.parametrize("name,confirm", [("dataops_demo_test", "dataops_demo_test"), ("DATAOPS_DEMO_TEST", "DATAOPS_DEMO_TEST"),
                                           ("dataops_scratch_test", None), ("dataops_scratch_test", ""), ("dataops_scratch_test", "dataops_sim_test"),
                                           ("", "")])
def test_refused(name, confirm):
    with pytest.raises(UnsafeTarget):
        assert_wipe_confirmed(name, confirm)


def test_the_exact_name_is_accepted():
    assert_wipe_confirmed("dataops_scratch_test", "dataops_scratch_test")
    assert_wipe_confirmed("dataops_scratch_test", " dataops_scratch_test ")


@pytest.mark.parametrize("name", ["dataops_sim", "master", "tempdb", "msdb", "model", "dataops_ecommerce_demo", "dataops_scratch"])
def test_the_target_guard_still_refuses_protected_and_unsuffixed_names(name):
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(Settings(db_name=name, db_auth_mode="windows", db_server="localhost"))


def test_a_mismatched_live_database_is_refused():
    s = Settings(db_name="dataops_scratch_test", db_auth_mode="windows", db_server="localhost")
    assert_safe_integration_target(s, "dataops_scratch_test")
    for observed in ("dataops_sim", "", "master", "other_test"):
        with pytest.raises(UnsafeTarget):
            assert_safe_integration_target(s, observed)
