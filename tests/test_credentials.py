"""The credential resolver: precedence, reporting, and the no-secrets invariant."""

from __future__ import annotations

import json

import pytest

from archivefs.credentials import Credential, CredentialResolver
from archivefs.errors import (
    ArchiveFSError,
    CredentialsNotFoundError,
    CredentialSourceError,
)

TOKEN = Credential(name="token", env=("A_TOKEN", "LEGACY_TOKEN"), secret=True)
USER = Credential(name="username", env=("A_USER",), secret=False)


def resolver(credentials=None, options=None, environ=None):
    return CredentialResolver(
        "TestArchive",
        credentials or [TOKEN, USER],
        options or {},
        environ=environ or {},
    )


def write_creds(path, payload):
    """Write a credentials file the resolver will agree to read.

    The 0600 is not incidental: a group-readable file is refused, so every test
    that wants the *file* path exercised has to produce a private one.
    """
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


# ----------------------------------------------------------------- precedence


def test_a_credential_cannot_be_passed_as_an_option():
    """The policy: env or a private file, and nothing else.

    This is the route that ends up in a committed .dvc file via
    deps[].fs_config, so it is refused rather than deprioritised.
    """
    with pytest.raises(CredentialSourceError) as exc:
        resolver(options={"token": "from-options"}, environ={"A_TOKEN": "from-env"})
    message = str(exc.value)
    assert "token cannot be passed as an option" in message
    assert "$A_TOKEN" in message  # names where it *should* come from
    assert "from-options" not in message  # and never echoes the value


def test_refusal_happens_before_the_value_is_ever_read():
    """Construction fails, so no code path can quietly consume the option."""
    with pytest.raises(CredentialSourceError):
        resolver(options={"username": "alice"})


def test_a_non_secret_option_is_unaffected():
    r = resolver(options={"layout": "nested"}, environ={"A_TOKEN": "t"})
    assert r.resolve("token").value == "t"


def test_environment_beats_file(tmp_path):
    path = write_creds(tmp_path / "creds.json", {"token": "from-file"})
    r = resolver(
        credentials=[Credential(name="token", env=("A_TOKEN",), files=(str(path),))],
        environ={"A_TOKEN": "from-env"},
    )
    assert r.resolve("token").value == "from-env"


def test_file_is_the_last_resort(tmp_path):
    path = write_creds(tmp_path / "creds.json", {"token": "from-file"})
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
    path = write_creds(tmp_path / "creds.json", {"access_token": "xyz"})
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
    # the chain no longer offers an option-shaped route, and says so
    assert "storage_options" not in message
    assert "cannot be passed as options" in message


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
    path.chmod(0o600)
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
    r = resolver(environ={"A_TOKEN": "sekrit", "A_USER": "alice"})
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


def test_several_missing_credentials_are_grouped_not_run_together():
    """One numbered chain per credential.

    A single run of ten numbered lines covering two different secrets reads as one
    chain, which is exactly what the message is trying to document.
    """
    r = resolver(
        credentials=[
            Credential(name="username", env=("A_USER",), secret=False, help="your email"),
            Credential(name="password", env=("A_PASS",), secret=True, help="your password"),
        ]
    )
    with pytest.raises(CredentialsNotFoundError) as exc:
        r.require("username", "password")
    message = str(exc.value)
    assert "username:" in message and "password:" in message
    assert "(your email)" in message and "(your password)" in message
    # each chain numbered from 1, so neither looks like a continuation of the other
    assert message.count("1. $A_") == 2
    # and the structured form is available to a caller that wants to render it
    assert set(exc.value.sections) == {"username", "password"}


def test_a_single_missing_credential_is_still_a_flat_list():
    r = resolver()
    with pytest.raises(CredentialsNotFoundError) as exc:
        r.resolve("token")
    assert exc.value.sections is None
    assert "1. $A_TOKEN" in str(exc.value)


# ------------------------------------------------------------- file permissions


def test_a_group_readable_credentials_file_is_refused(tmp_path):
    """ssh refuses a world-readable private key; so do we.

    A warning would be the softer option and the wrong one: on a shared HPC
    filesystem this is the difference between an auditable credential and a
    shared one, and nobody reads warnings in a PBS job log.
    """
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"token": "sekrit"}))
    path.chmod(0o640)
    r = resolver(credentials=[Credential(name="token", files=(str(path),))])
    with pytest.raises(CredentialSourceError) as exc:
        r.resolve("token")
    message = str(exc.value)
    assert f"chmod 600 {path}" in message  # the fix, not just the complaint
    assert "sekrit" not in message


def test_a_world_readable_credentials_file_is_refused(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"token": "sekrit"}))
    path.chmod(0o604)
    r = resolver(credentials=[Credential(name="token", files=(str(path),))])
    with pytest.raises(CredentialSourceError):
        r.resolve("token")


def test_a_private_file_is_read(tmp_path):
    path = write_creds(tmp_path / "creds.json", {"token": "sekrit"})
    r = resolver(credentials=[Credential(name="token", files=(str(path),))])
    assert r.resolve("token").value == "sekrit"


def test_mode_is_not_checked_on_a_file_that_is_never_reached(tmp_path):
    """The environment wins, so an exposed file further down is irrelevant."""
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"token": "from-file"}))
    path.chmod(0o644)
    r = resolver(
        credentials=[Credential(name="token", env=("A_TOKEN",), files=(str(path),))],
        environ={"A_TOKEN": "from-env"},
    )
    assert r.resolve("token").value == "from-env"
