"""The credential resolver: precedence, reporting, and the no-secrets invariant."""

from __future__ import annotations

import json

import pytest

from archivefs.credentials import Credential, CredentialResolver
from archivefs.errors import ArchiveFSError, CredentialsNotFoundError

TOKEN = Credential(name="token", env=("A_TOKEN", "LEGACY_TOKEN"), secret=True)
USER = Credential(name="username", env=("A_USER",), secret=False)


def resolver(credentials=None, options=None, environ=None):
    return CredentialResolver(
        "TestArchive",
        credentials or [TOKEN, USER],
        options or {},
        environ=environ or {},
    )


# ----------------------------------------------------------------- precedence


def test_storage_options_beats_environment():
    r = resolver(options={"token": "from-options"}, environ={"A_TOKEN": "from-env"})
    resolved = r.resolve("token")
    assert resolved.value == "from-options"
    assert "storage_options" in resolved.source


def test_environment_beats_file(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"token": "from-file"}))
    r = resolver(
        credentials=[Credential(name="token", env=("A_TOKEN",), files=(str(path),))],
        environ={"A_TOKEN": "from-env"},
    )
    assert r.resolve("token").value == "from-env"


def test_file_is_the_last_resort(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"token": "from-file"}))
    r = resolver(
        credentials=[Credential(name="token", env=("A_TOKEN",), files=(str(path),))]
    )
    resolved = r.resolve("token")
    assert resolved.value == "from-file"
    assert str(path) in resolved.source


def test_env_vars_are_tried_in_order():
    r = resolver(environ={"LEGACY_TOKEN": "old"})
    assert r.resolve("token").value == "old"
    r2 = resolver(environ={"A_TOKEN": "new", "LEGACY_TOKEN": "old"})
    assert r2.resolve("token").value == "new"


def test_a_different_file_key_is_honoured(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"access_token": "xyz"}))
    r = resolver(
        credentials=[
            Credential(name="token", files=(str(path),), file_key="access_token")
        ]
    )
    assert r.resolve("token").value == "xyz"


# -------------------------------------------------------------------- failure


def test_missing_required_credential_lists_the_chain():
    r = resolver()
    with pytest.raises(CredentialsNotFoundError) as exc:
        r.resolve("token")
    message = str(exc.value)
    assert "$A_TOKEN" in message
    assert "$LEGACY_TOKEN" in message
    assert "storage_options" in message
    # the message doubles as documentation of the invariant
    assert "never be written into a committed .dvc" in message


def test_require_reports_every_missing_credential_at_once():
    r = resolver()
    with pytest.raises(CredentialsNotFoundError) as exc:
        r.require("token", "username")
    message = str(exc.value)
    assert "token and username" in message
    assert "$A_TOKEN" in message
    assert "$A_USER" in message


def test_optional_credential_returns_default():
    r = resolver(credentials=[Credential(name="token", required=False)])
    assert r.get("token") is None
    assert r.get("token", "fallback") == "fallback"


def test_unknown_credential_is_a_programming_error():
    r = resolver()
    with pytest.raises(ArchiveFSError) as exc:
        r.resolve("nonesuch")
    assert "token" in str(exc.value)  # tells you what it does know


def test_invalid_json_file_is_reported_not_swallowed(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("{not json")
    r = resolver(credentials=[Credential(name="token", files=(str(path),))])
    with pytest.raises(ArchiveFSError) as exc:
        r.resolve("token")
    assert str(path) in str(exc.value)


# ------------------------------------------------- path-valued credentials


def test_path_credential_must_exist(tmp_path):
    ngc = Credential(name="ngc", env=("NGC",), is_path=True)
    r = resolver(credentials=[ngc], environ={"NGC": str(tmp_path / "absent.ngc")})
    with pytest.raises(CredentialsNotFoundError) as exc:
        r.resolve("ngc")
    assert "does not exist" in str(exc.value)


def test_path_credential_is_expanded(tmp_path):
    key = tmp_path / "project.ngc"
    key.write_text("key")
    ngc = Credential(name="ngc", env=("NGC",), is_path=True)
    r = resolver(credentials=[ngc], environ={"NGC": str(key)})
    assert r.resolve("ngc").value == str(key)


# ------------------------------------------------------------ the invariant


def test_committed_secrets_names_what_must_not_be_committed():
    r = resolver()
    problems = r.committed_secrets({"username": "alice", "token": "sekrit"})
    assert problems == ["token"]  # username is fine to commit, token is not


def test_committed_secrets_ignores_empty_values():
    r = resolver()
    assert r.committed_secrets({"token": ""}) == []


# -------------------------------------------------------------- reporting


def test_describe_never_leaks_the_value():
    r = resolver(options={"token": "sekrit"}, environ={"A_USER": "alice"})
    r.resolve("token")
    r.resolve("username")
    description = r.describe()
    assert "sekrit" not in description
    assert "token from" in description
    assert "$A_USER" in description


def test_the_documented_api_exists():
    """The module docstring named a method that did not exist."""
    assert hasattr(CredentialResolver, "committed_secrets")
    import archivefs.credentials as module

    assert "committed_secrets" in module.__doc__
    assert "warn_if_committed" not in module.__doc__
