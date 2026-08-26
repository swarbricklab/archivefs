"""Test-wide isolation.

The offline suite must not read the developer's home directory. It used to,
silently, and the bill arrived the day a real token file appeared: three tests
asserting "no credential configured" behaviour started exercising the *rejected
credential* path instead, because ``~/.config/archivefs/zenodo.json`` existed.

The fixtures already cleared the credential *environment* variables. That was half
the lookup chain — :class:`archivefs.credentials.CredentialResolver` searches
options, then environment, then files under ``~`` — and the missing half meant the
suite passed or failed according to whether whoever ran it had configured the
archive it was testing.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(request, monkeypatch, tmp_path_factory):
    """Point ``~`` at an empty directory for every non-network test.

    Every credential file source resolves through ``Path(...).expanduser()``, so
    one ``HOME`` redirect covers all of them — present and future, which a
    per-backend list of paths would not.

    Network tests are exempt, deliberately. They authenticate through the
    *environment* (EGA's live fixture sets ``EGA_USERNAME``/``EGA_PASSWORD`` from
    pyega3's published test account) or not at all (Zenodo, GEO and BIA read public
    records anonymously), so none of them needs the real home — but they do spawn
    ``git`` and ``dvc`` subprocesses that write into it, and breaking those to
    tighten an invariant they do not rely on would be a bad trade.
    """
    if request.node.get_closest_marker("network"):
        return
    empty = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(empty))
    # Same argument, one directory over: a developer who has turned the block
    # cache on would otherwise have every test resuming from *their* cache, and
    # "did this read go to the network" is the question half of them ask.
    monkeypatch.delenv("ARCHIVEFS_CACHE_DIR", raising=False)
    monkeypatch.delenv("ARCHIVEFS_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("ARCHIVEFS_CACHE_MAX_AGE_DAYS", raising=False)
    # And the config layers, for the same reason one directory further out: a
    # developer with `extract_zips` or a `cache_dir` in their own config would
    # otherwise change the answer to "did this read go to the network", which is the
    # question half these tests ask. `XDG_CONFIG_DIRS` is pointed at an empty
    # directory rather than unset, because unset means `/etc/xdg`.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / ".config"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(empty / "etc"))
    monkeypatch.delenv("ARCHIVEFS_CONFIG", raising=False)
