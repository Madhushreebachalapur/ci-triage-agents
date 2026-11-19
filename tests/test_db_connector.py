from src.db_connector import connect, get_user_name


def test_connect():
    assert connect()["connected"] is True


def test_get_user_name():
    assert get_user_name(1) == "alice"
