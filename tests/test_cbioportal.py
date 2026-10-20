"""cbioportal:// is deferred — these pin that it refuses clearly, like dbGaP.

A deferred backend's whole job is to make the failure read "deferred, here is why
and what to use instead" rather than "unknown scheme", and to advertise nothing to
DVC. The parallel with ``test_dbgap.py`` is deliberate.
"""

from __future__ import annotations

import pytest

from archivefs.backends.cbioportal import CBioPortalFileSystem
from archivefs.errors import NotSupportedError
from archivefs.registry import PROTOCOLS


def test_instantiating_refuses_with_an_explanation():
    with pytest.raises(NotSupportedError) as exc:
        CBioPortalFileSystem()
    message = str(exc.value)
    assert "deferred" in message
    # names the blockers and the licit interim, not just "not implemented"
    assert "tar.gz" in message
    assert "zenodo://" in message


def test_the_profile_advertises_nothing():
    """A deferred backend must look deferred in every field, not just `status`."""
    backend = PROTOCOLS["cbioportal"]
    assert backend.status == "deferred"
    assert backend.dvc_wrapper is None  # DVC is never told it exists
    assert backend.config == {}
    assert backend.concept_scheme is None
    assert backend.immutability_basis is None
    assert backend.published_checksum is None


def test_it_is_not_registered_with_dvc():
    from archivefs.dvcfs import DVC_PROTOCOLS

    assert "cbioportal" not in DVC_PROTOCOLS


def test_fsspec_can_still_see_it_so_the_error_is_ours():
    import fsspec

    from archivefs.registry import register_implementations

    register_implementations()
    with pytest.raises(NotSupportedError):
        fsspec.filesystem("cbioportal")
