import pytest

from dictee.auth import Caller, resolve_caller
from dictee.errors import ApiError


def test_personal_key_designates_its_owner(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    assert resolve_caller(db, settings, f"Bearer {key}", None) == Caller(uid)


def test_personal_key_ignores_the_openwebui_header(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.upsert_user("sub-2", "b@example.org", "B")
    key = db.create_key(uid, "Android")
    assert resolve_caller(db, settings, f"Bearer {key}", "b@example.org") == Caller(uid)


def test_service_key_with_a_known_email_designates_that_user(db, settings):
    uid = db.upsert_user("sub-1", "Ada@Example.org", "Ada")
    authorization = f"Bearer {settings.openwebui_service_key}"
    assert resolve_caller(db, settings, authorization, "ada@example.org") == Caller(uid)
    assert resolve_caller(db, settings, authorization, "ADA@EXAMPLE.ORG") == Caller(uid)


@pytest.mark.parametrize("email", [None, "", "nobody@example.org"])
def test_service_key_without_a_known_email_uses_defaults(db, settings, email):
    db.upsert_user("sub-1", "a@example.org", "A")
    authorization = f"Bearer {settings.openwebui_service_key}"
    assert resolve_caller(db, settings, authorization, email) == Caller(None)


def test_bearer_scheme_is_case_insensitive(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    assert resolve_caller(db, settings, f"bearer {key}", None) == Caller(uid)


# Les refus se vérifient avec l'email d'un compte qui existe : sans clé valide, l'en-tête
# d'Open WebUI ne doit jamais désigner ce compte (n'importe qui peut envoyer cet en-tête).
@pytest.mark.parametrize("authorization", [None, "", "Bearer", "Bearer ", "Bearer dk_unknown", "Basic abc"])
def test_refused_authorizations(db, settings, authorization):
    db.upsert_user("sub-1", "victim@example.org", "Victim")
    with pytest.raises(ApiError) as e:
        resolve_caller(db, settings, authorization, "victim@example.org")
    assert e.value.status == 401 and e.value.code == "invalid_api_key"


def test_a_key_that_only_resembles_the_service_key_is_refused(db, settings):
    db.upsert_user("sub-1", "victim@example.org", "Victim")
    with pytest.raises(ApiError) as e:
        resolve_caller(db, settings, f"Bearer {settings.openwebui_service_key}x", "victim@example.org")
    assert e.value.status == 401 and e.value.code == "invalid_api_key"


@pytest.mark.parametrize("email", [None, "victim@example.org"])
def test_revoked_key_is_refused(db, settings, email):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.upsert_user("sub-2", "victim@example.org", "Victim")
    key = db.create_key(uid, "Android")
    db.revoke_key(uid, db.list_keys(uid)[0].id)
    with pytest.raises(ApiError) as e:
        resolve_caller(db, settings, f"Bearer {key}", email)
    assert e.value.status == 401 and e.value.code == "invalid_api_key"
