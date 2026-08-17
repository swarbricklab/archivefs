"""``dbgap://`` — **DEFERRED**. Registered, documented, not implemented.

``docs/plan.md`` step 4: dbGaP is in scope but not yet adopted, and the design doc
is explicit that it should be built *last*, against the by-then-proven base and
against **real portal access rather than documentation**. So this module exists to
do three things and nothing more:

1. make ``dbgap://`` a *known* protocol, so the failure is "this is deferred, here
   is why and here is what to use instead" rather than "unknown protocol", which
   reads like a typo;
2. record, in code, the three properties that make dbGaP awkward, so whoever picks
   it up starts from the constraints rather than rediscovering them;
3. hold the credential declaration, because it is the one part that can be
   designed without portal access — and it is the interesting one: dbGaP's
   credential is a **path to a file**, not a secret string, which is precisely the
   case :mod:`archivefs.credentials` was shaped to handle.

The three properties:

**The credential is an NGC key file.** ``prefetch --ngc /path/to/prj.ngc``. The
committed ``.dvc`` may legitimately carry the *path* (it is not itself a secret),
while the file it points at must never be committed. The key is project-scoped and
expires; when it does, a human must re-download it from the portal, so that failure
mode needs detecting and saying out loud rather than surfacing as a generic auth
error.

**The workspace is per-project and stateful.** SRA Toolkit tools expect to be run
inside a configured workspace directory (``vdb-config``), which is a poor fit for
a stateless filesystem whose methods can be called from anywhere. Any
implementation has to own a workspace per project and be explicit about where it
lives — an fsspec filesystem that quietly changes the process's working directory
would be a bug.

**Byte fidelity is lost.** Data arrives as SRA archives and is converted to FASTQ
by ``fasterq-dump``. The conversion is not byte-preserving, so the checksum of
what you get is not the checksum of what was deposited: the manifest contract the
rest of archivefs relies on — "the token the source gives me corresponds to the
bytes the user ends up with" — does not hold. Whatever is built here must be
honest about that in ``info()`` rather than advertising an SRA checksum as if it
described the FASTQ.

Whether dbGaP can conform to :class:`~archivefs.base.ArchiveFileSystem` without
violating it is, per the design doc, *the test of whether the abstraction held*.
Deliberately not answered yet.
"""

from __future__ import annotations

from typing import Any, ClassVar

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import MissingToolError, NotSupportedError

#: The SRA Toolkit binaries an implementation would need on ``PATH``. Checked at
#: runtime so the failure is "install the SRA Toolkit", not an import error.
REQUIRED_TOOLS = ("prefetch", "fasterq-dump", "vdb-config")

DEFERRED_MESSAGE = """\
the dbgap:// backend is deferred, not missing (docs/plan.md step 4).

dbGaP is in scope but not yet adopted here, and the design deliberately builds it
last: against the proven base, and against real portal access rather than
documentation. Three things make it different from the archives archivefs already
speaks, and each needs deciding with a real project in hand:

  * the credential is a path to an NGC key file, project-scoped and expiring;
  * SRA Toolkit tools want a stateful per-project workspace;
  * SRA -> FASTQ conversion is not byte-preserving, so a source checksum does not
    describe the bytes you end up with.

If you have dbGaP data now: run the SRA Toolkit directly (prefetch --ngc, then
fasterq-dump), and record what you got with a Chirograph 'observed-download'
manifest so the analysis is still reproducible. For human raw reads that we
control, ega:// is the supported path.\
"""


class DbGaPFileSystem(ArchiveFileSystem):
    """Placeholder that refuses clearly. See the module docstring."""

    protocol = "dbgap"
    archive_name = "dbGaP"
    credentials: ClassVar[list[Credential]] = [
        Credential(
            name="ngc",
            env=("DBGAP_NGC",),
            # A path, not a secret: it may appear in a committed .dvc. The file it
            # points at must not be committed.
            secret=False,
            is_path=True,
            required=True,
            help=(
                "path to your project's dbGaP NGC key file, downloaded from the "
                "portal. It is project-scoped and expires; re-download it when it "
                "does."
            ),
        )
    ]

    def __init__(self, **kwargs: Any) -> None:
        # Deliberately raise before touching the network or the credential chain:
        # there is nothing here to configure yet.
        raise NotSupportedError(DEFERRED_MESSAGE)

    @staticmethod
    def check_tools() -> list[str]:
        """Which required SRA Toolkit binaries are missing from ``PATH``.

        Useful now — it lets ``archivefs info`` tell someone considering dbGaP
        whether their environment is even close — and it is the check the eventual
        implementation must run before shelling out.
        """
        import shutil

        return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]

    @staticmethod
    def require_tools() -> None:
        missing = DbGaPFileSystem.check_tools()
        if missing:
            raise MissingToolError(
                ", ".join(missing),
                "dbGaP data is retrieved with the SRA Toolkit",
                "Install the SRA Toolkit from "
                "https://github.com/ncbi/sra-tools and put its bin/ on PATH. "
                "pip will not fix this — the tools are compiled binaries.",
            )

    def _load_members(self, container: str) -> dict[str, dict]:  # pragma: no cover
        raise NotSupportedError(DEFERRED_MESSAGE)

    def _data_url(self, info: dict) -> str:  # pragma: no cover
        raise NotSupportedError(DEFERRED_MESSAGE)
