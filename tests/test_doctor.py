from jevme import config, doctor


def test_missing_typesafe_key_is_a_failure(monkeypatch):
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    rows = doctor.check_keys(live=False)
    assert rows[0][0] == doctor.FAIL and "TYPESAFE_API_KEY" in rows[0][1]


def test_permission_rows_always_name_a_fix_when_not_ok():
    for mark, _, fix in doctor.check_permissions():
        assert mark == doctor.OK or fix
