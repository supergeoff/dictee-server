import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from dictee.db import Database, StoredSettings


def test_created_key_is_stored_hashed(db, db_path):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    assert key.startswith("dk_") and len(key) >= 40
    stored_hash, prefix = sqlite3.connect(db_path).execute("select hash, prefix from api_keys").fetchone()
    assert stored_hash == hashlib.sha256(key.encode()).hexdigest()
    assert key.startswith(prefix) and len(prefix) == 10


def test_two_keys_are_never_the_same(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    assert db.create_key(uid, "Android") != db.create_key(uid, "Android")


def test_key_owner_records_the_last_use(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    assert db.list_keys(uid)[0].last_used_at is None
    assert db.key_owner(key) == uid
    assert db.list_keys(uid)[0].last_used_at is not None


def test_unknown_key_has_no_owner(db):
    db.upsert_user("sub-1", "a@example.org", "A")
    assert db.key_owner("dk_unknown") is None


def test_revoked_key_has_no_owner(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    key_id = db.list_keys(uid)[0].id
    assert db.revoke_key(uid, key_id) is True
    assert db.key_owner(key) is None
    assert db.list_keys(uid)[0].revoked_at is not None


def test_a_revoked_key_keeps_its_first_revocation_date(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.create_key(uid, "Android")
    key_id = db.list_keys(uid)[0].id
    db.revoke_key(uid, key_id)
    first = db.list_keys(uid)[0].revoked_at
    assert db.revoke_key(uid, key_id) is False
    assert db.list_keys(uid)[0].revoked_at == first


def test_a_user_cannot_revoke_another_users_key(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    other = db.upsert_user("sub-2", "b@example.org", "B")
    key = db.create_key(uid, "Android")
    key_id = db.list_keys(uid)[0].id
    assert db.revoke_key(other, key_id) is False
    assert db.key_owner(key) == uid


def test_list_keys_returns_only_the_keys_of_that_user(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    other = db.upsert_user("sub-2", "b@example.org", "B")
    key = db.create_key(uid, "Android")
    db.create_key(other, "Dictate")
    (info,) = db.list_keys(uid)
    assert info.label == "Android"
    assert info.prefix == key[:10]
    assert info.last_used_at is None and info.revoked_at is None


def test_dates_are_iso_8601_in_utc(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    db.key_owner(key)
    info = db.list_keys(uid)[0]
    for value in (info.created_at, info.last_used_at):
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - datetime.fromisoformat(info.created_at)) < timedelta(minutes=1)


def test_same_sub_updates_the_email(db):
    uid = db.upsert_user("sub-1", "old@example.org", "A")
    assert db.upsert_user("sub-1", "new@example.org", "A") == uid
    assert db.user_by_email("new@example.org") == uid
    assert db.user_by_email("old@example.org") is None


def test_email_lookup_ignores_case(db):
    uid = db.upsert_user("s", "Ada@Example.org", "Ada")
    assert db.user_by_email("ada@example.ORG") == uid


def test_user_sub_gives_the_sub_of_an_account_and_none_for_an_unknown_one(db, db_path):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    assert db.user_sub(uid) == "sub-1"
    assert db.user_sub(uid + 1) is None
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("delete from users where id = ?", (uid,))
    finally:
        conn.close()
    assert db.user_sub(uid) is None


def test_unknown_email_has_no_user(db):
    db.upsert_user("s", "a@example.org", "A")
    assert db.user_by_email("nobody@example.org") is None


def test_settings_are_empty_by_default(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    assert db.get_settings(uid) == StoredSettings("", "", "")


def test_saved_settings_are_read_back(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(uid, glossary="Starlette", instructions="Be brief.", voice="Sofia")
    assert db.get_settings(uid) == StoredSettings("Starlette", "Be brief.", "Sofia")


def test_saving_settings_keeps_the_fields_that_are_not_given(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(uid, glossary="Starlette", voice="Sofia")
    db.save_settings(uid, instructions="Be brief.")
    db.save_settings(uid, voice="")
    assert db.get_settings(uid) == StoredSettings("Starlette", "Be brief.", "")


def test_settings_belong_to_one_user(db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    other = db.upsert_user("sub-2", "b@example.org", "B")
    db.save_settings(uid, glossary="Starlette")
    assert db.get_settings(other) == StoredSettings("", "", "")


def test_reopening_the_database_keeps_its_data(db, db_path):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    assert Database(db_path).user_by_email("a@example.org") == uid


def user_version(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("pragma user_version").fetchone()[0]
    finally:
        conn.close()


def set_user_version(db_path, version: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"pragma user_version = {version}")
    finally:
        conn.close()


def table_names(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    finally:
        conn.close()


# Le schéma tel que la version 1 le créait, recopié ici pour que le test de migration ne suive pas celui du code.
VERSION_1_SCHEMA = """
create table users (
    id integer primary key,
    sub text not null unique,
    email text not null unique,
    name text not null,
    created_at text not null,
    last_login_at text not null
);
create table api_keys (
    id integer primary key,
    user_id integer not null references users (id),
    label text not null,
    prefix text not null,
    hash text not null unique,
    created_at text not null,
    last_used_at text,
    revoked_at text
);
create index api_keys_user_id on api_keys (user_id);
create table settings (
    user_id integer primary key references users (id),
    glossary text not null default '',
    instructions text not null default '',
    voice text not null default ''
);
"""


def write_version_1_database(directory, *, email: str):
    """Écrit une base comme la version 1 la laissait : ses trois tables, un compte et `user_version` à 1."""
    path = directory / "dictee.db"
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.executescript(VERSION_1_SCHEMA)
            conn.execute(
                "insert into users (sub, email, name, created_at, last_login_at) values (?, ?, ?, ?, ?)",
                ("sub-1", email, "Alice", "2026-10-31T10:00:00+00:00", "2026-10-31T10:00:00+00:00"),
            )
            conn.execute("pragma user_version = 1")
    finally:
        conn.close()
    return path


def test_the_schema_is_created_at_version_2(db, db_path):
    assert user_version(db_path) == 2
    assert {"tasks", "task_chunks"} <= table_names(db_path)


def test_a_version_1_database_gains_the_task_tables_and_keeps_its_accounts(tmp_path):
    path = write_version_1_database(tmp_path, email="alice@example.test")
    assert not {"tasks", "task_chunks"} & table_names(path)
    database = Database(str(path))
    assert user_version(path) == 2
    assert {"tasks", "task_chunks"} <= table_names(path)
    assert database.user_by_email("alice@example.test") is not None


def test_a_database_created_before_the_versioning_gets_its_version_when_opened(db, db_path):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    set_user_version(db_path, 0)
    reopened = Database(db_path)
    assert user_version(db_path) == 2
    assert reopened.user_by_email("a@example.org") == uid


def test_a_newer_version_is_left_as_it_is(db, db_path):
    set_user_version(db_path, 7)
    Database(db_path)
    assert user_version(db_path) == 7


def test_database_creates_its_parent_directory(tmp_path):
    path = tmp_path / "missing" / "dir" / "dictee.db"
    Database(str(path))
    assert path.exists()
