"""End-to-end DVC integration, against real archives.

The design doc's target user experience, verified rather than assumed:

    a committed .dvc import file references an accession/DOI (never a secret),
    and `dvc pull` materialises the bytes on demand using locally-resolved
    credentials.

Marked ``network`` and skipped by default — it drives the real ``dvc`` CLI against
zenodo.org and EGA. Run with::

    ARCHIVEFS_TEST_DVC=1 pytest tests/test_dvc_integration.py

EGA uses the public test account that ships inside pyega3, so no real credentials
are needed to exercise the controlled-access path.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("dvc")
yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.network

ZENODO_FILE = "zenodo://1486184/github-mark.zip"
ZENODO_MD5 = "49f6a914ea33912471512ac9228c6923"
EGA_FILE = "ega://EGAD00001009826/EGAF00007243773"
EGA_MD5 = "ed365c71461eac21a64d2c29e7216e50"


def requires_dvc_test() -> None:
    if os.environ.get("ARCHIVEFS_TEST_DVC", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_DVC=1 to drive the real dvc CLI")


@pytest.fixture
def repo(tmp_path: Path):
    """A throwaway git+dvc repo with archivefs registered for its subprocesses."""
    requires_dvc_test()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
    )
    dvc_bin = Path(sys.executable).with_name("dvc")
    subprocess.run([str(dvc_bin), "init", "-q"], cwd=tmp_path, check=True)

    def run(*args: str, **environment: str) -> subprocess.CompletedProcess:
        # PYTHONSTARTUP is not enough: dvc is a console script, so registration
        # has to come from the start-up hook. Force it explicitly here so the test
        # does not depend on `archivefs dvc enable` having been run.
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).parent.parent / "src"), os.environ.get("PYTHONPATH", "")]
            ),
        }
        # Clear any inherited opt-out, unless this call is deliberately setting it.
        if "ARCHIVEFS_NO_DVC_HOOK" not in environment:
            env.pop("ARCHIVEFS_NO_DVC_HOOK", None)
        env.update(environment)
        return subprocess.run(
            [str(dvc_bin), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=900,
            check=False,
        )

    return tmp_path, run


def test_hook_must_be_enabled_for_dvc_to_resolve_us(repo):
    """Documents the requirement rather than pretending it does not exist."""
    _, run = repo
    result = run("list-url", "zenodo://1486184", ARCHIVEFS_NO_DVC_HOOK="1")
    assert result.returncode != 0
    assert "Unsupported URL type" in result.stderr


def test_list_url_zenodo(repo):
    _, run = repo
    result = run("list-url", "zenodo://1486184")
    assert result.returncode == 0, result.stderr
    # relative to the record, not "../../1486184/…"
    assert result.stdout.split() == ["github-mark.zip"]


def test_list_url_geo(repo):
    _, run = repo
    result = run("list-url", "geo://GSE176078")
    assert result.returncode == 0, result.stderr
    assert "GSE176078_Wu_etal_2021_bulkRNAseq_raw_counts.txt.gz" in result.stdout


def test_import_url_no_download_records_a_real_checksum(repo):
    """The --no-download contract: a pinned md5 without moving any payload."""
    root, run = repo
    result = run("import-url", "--no-download", ZENODO_FILE, "logo.zip")
    assert result.returncode == 0, result.stderr
    assert not (root / "logo.zip").exists()  # nothing downloaded

    spec = yaml.safe_load((root / "logo.zip.dvc").read_text())
    dep = spec["deps"][0]
    assert dep["path"] == ZENODO_FILE
    assert dep["md5"] == ZENODO_MD5
    assert dep["size"] == 853
    assert dep["hash"] == "md5"


def test_pull_materialises_the_recorded_bytes(repo):
    root, run = repo
    assert run("import-url", "--no-download", ZENODO_FILE, "logo.zip").returncode == 0
    result = run("pull", "logo.zip.dvc")
    assert result.returncode == 0, result.stderr
    payload = (root / "logo.zip").read_bytes()
    assert hashlib.md5(payload).hexdigest() == ZENODO_MD5  # noqa: S324


def test_directory_import_builds_a_dir_manifest(repo):
    root, run = repo
    result = run("import-url", "--no-download", "zenodo://1486184", "record")
    assert result.returncode == 0, result.stderr
    spec = yaml.safe_load((root / "record.dvc").read_text())
    dep = spec["deps"][0]
    assert dep["md5"].endswith(".dir")
    assert dep["nfiles"] == 1

    assert run("pull", "record.dvc").returncode == 0
    assert (root / "record" / "github-mark.zip").exists()


def test_ega_import_keeps_credentials_out_of_the_dvc_file(repo):
    """The whole point of the credential model, checked on the committed artifact."""
    root, run = repo
    result = run(
        "import-url",
        "--no-download",
        EGA_FILE,
        "sample.bam",
        EGA_USERNAME="ega-test-data@ebi.ac.uk",
        EGA_PASSWORD="egarocks",
    )
    assert result.returncode == 0, result.stderr
    text = (root / "sample.bam.dvc").read_text()
    assert "egarocks" not in text
    assert "ega-test-data" not in text
    spec = yaml.safe_load(text)
    assert spec["deps"][0]["md5"] == EGA_MD5
    assert spec["deps"][0]["path"] == EGA_FILE


def test_ega_pull_uses_the_environment(repo):
    root, run = repo
    assert (
        run(
            "import-url",
            "--no-download",
            EGA_FILE,
            "sample.bam",
            EGA_USERNAME="ega-test-data@ebi.ac.uk",
            EGA_PASSWORD="egarocks",
        ).returncode
        == 0
    )
    result = run(
        "pull",
        "sample.bam.dvc",
        EGA_USERNAME="ega-test-data@ebi.ac.uk",
        EGA_PASSWORD="egarocks",
    )
    assert result.returncode == 0, result.stderr
    payload = (root / "sample.bam").read_bytes()
    assert hashlib.md5(payload).hexdigest() == EGA_MD5  # noqa: S324


def test_ega_without_credentials_fails_with_our_message(repo):
    _, run = repo
    result = run(
        "import-url", "--no-download", EGA_FILE, "sample.bam", EGA_PASSWORD=""
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    # our remediation must survive DVC's error wrapping
    assert "EGA_PASSWORD" in combined or "no username and password found" in combined


def test_update_refreshes_and_materialises(repo):
    """`dvc update` is half of the design's target UX; it must work both ways."""
    root, run = repo
    assert run("import-url", "--no-download", ZENODO_FILE, "logo.zip").returncode == 0
    assert run("update", "logo.zip.dvc", "--no-download").returncode == 0
    assert not (root / "logo.zip").exists()
    assert run("update", "logo.zip.dvc").returncode == 0
    assert hashlib.md5((root / "logo.zip").read_bytes()).hexdigest() == ZENODO_MD5  # noqa: S324


def test_zip_transparency_through_fs_config(repo):
    """The workspace must hold the extracted tree, not the archive.

    This caught a real bug: info() called the ZIP a directory while isdir() —
    which is what DVC's download path asks — called it a file, so the workspace
    got the .zip. Every derived predicate now goes through info().

    Driven through `--fs-config` rather than a named remote: the archive-as-remote
    pattern is unsupported (decisions.md D15), so the coverage belongs on the path
    we actually ship.
    """
    root, run = repo
    result = run(
        "import-url",
        "--fs-config",
        "extract_zips=true",
        "--",
        "zenodo://1486184/github-mark.zip",
        "tree",
    )
    assert result.returncode == 0, result.stderr
    spec = yaml.safe_load((root / "tree.dvc").read_text())
    assert spec["deps"][0]["md5"].endswith(".dir")
    assert spec["deps"][0]["nfiles"] == 2

    extracted = sorted(p.name for p in (root / "tree").iterdir())
    assert extracted == ["github-mark.svg", "md5sum"]
    assert not (root / "tree").is_file()


BIA_THUMB = (
    "01776a3a-16a5-4c48-a104-fefcc83cc2ee/"
    "01776a3a-16a5-4c48-a104-fefcc83cc2ee-thumbnail-128-128.png"
)
BIA_THUMB_MD5 = "a84906a7545b1d47386e7d72ee807c41"


def test_list_url_bia(repo):
    _, run = repo
    result = run("list-url", "bia://S-BIAD599")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["20200220-ftp"]


def test_bia_derived_import_is_genuinely_download_free(repo):
    """BIA's converted data publishes single-part S3 ETags, which *are* MD5s.

    So unlike GEO — and unlike BIA's own submitted files — this records a real
    content hash without moving the bytes.
    """
    root, run = repo
    result = run(
        "import-url",
        "--no-download",
        "--fs-config",
        "view=derived",
        "--",
        f"bia://S-BIAD599/{BIA_THUMB}",
        "thumb.png",
    )
    assert result.returncode == 0, result.stderr
    assert not (root / "thumb.png").exists()
    spec = yaml.safe_load((root / "thumb.png.dvc").read_text())
    assert spec["deps"][0]["md5"] == BIA_THUMB_MD5
    assert spec["deps"][0]["size"] == 12234

    assert run("pull", "thumb.png.dvc").returncode == 0
    payload = (root / "thumb.png").read_bytes()
    assert hashlib.md5(payload).hexdigest() == BIA_THUMB_MD5  # noqa: S324
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"


BIA_CSV = "bia://S-BIAD605/ccrcc/clinical_data.csv"
BIA_CSV_MD5 = "6ae3d055bae6270cf1c02f66f8edb6dc"


def test_bia_submitted_import_hashes_what_it_downloads(repo):
    """The submitted tier publishes no checksum, so DVC hashes the bytes it fetched.

    That is the honest GEO-shaped behaviour, and the .dvc still pins the content —
    it just cannot pin it *before* the download. (The derived tier can; see above.)
    """
    root, run = repo
    result = run("import-url", BIA_CSV, "clinical.csv")
    assert result.returncode == 0, result.stderr
    spec = yaml.safe_load((root / "clinical.csv.dvc").read_text())
    assert spec["deps"][0]["path"] == BIA_CSV
    assert spec["deps"][0]["md5"] == BIA_CSV_MD5
    payload = (root / "clinical.csv").read_bytes()
    assert hashlib.md5(payload).hexdigest() == BIA_CSV_MD5  # noqa: S324
    assert payload.startswith(b"ICAIRD number,Gender,Response")


# ------------------------------------------------------- the reviewer's route (D17)


def _clone_and_pull(root: Path, name: str, dvc_bin: Path):
    """Clone *root* into a sibling and `dvc pull` there, with no DVC remote."""
    clone = root.parent / name
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent.parent / "src"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    env.pop("ARCHIVEFS_NO_DVC_HOOK", None)
    result = subprocess.run(
        [str(dvc_bin), "pull"],
        cwd=clone,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
        check=False,
    )
    return clone, result


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "import"],
        cwd=root,
        check=True,
    )


def test_partial_import_is_pullable_from_the_archive_by_a_reviewer(repo):
    """The project's core promise, pinned: clone, no remote, `dvc pull` works.

    `--no-download` then `dvc pull` leaves the committed .dvc without an `outs.md5`,
    which is what makes DVC keep the source URL as a fallback storage. See
    decisions.md D17 — this is the *only* shape a reviewer can pull without the
    authors hosting a second copy of already-published data.
    """
    root, run = repo
    dvc_bin = Path(sys.executable).with_name("dvc")
    assert run("import-url", "--no-download", ZENODO_FILE, "logo.zip").returncode == 0
    before = (root / "logo.zip.dvc").read_text()
    assert run("pull").returncode == 0
    assert hashlib.md5((root / "logo.zip").read_bytes()).hexdigest() == ZENODO_MD5  # noqa: S324
    # pulling locally must not change the stage, or the reviewer route is lost
    assert (root / "logo.zip.dvc").read_text() == before
    assert "md5" not in yaml.safe_load(before)["outs"][0]

    _commit_all(root)
    clone, result = _clone_and_pull(root, "reviewer", dvc_bin)
    assert result.returncode == 0, result.stderr
    assert hashlib.md5((clone / "logo.zip").read_bytes()).hexdigest() == ZENODO_MD5  # noqa: S324


def test_completed_import_is_not_pullable_without_a_remote(repo):
    """The trap, pinned so we notice if DVC ever changes it.

    A full `dvc import-url` records `outs.md5`, and DVC then stops offering the
    source as a fallback — so a reviewer with no remote cannot get the bytes even
    though the checksum is in the committed file. If this test ever *fails*, the
    upstream gap in ideas.md #0 has been closed and the docs should say so.
    """
    root, run = repo
    dvc_bin = Path(sys.executable).with_name("dvc")
    assert run("import-url", ZENODO_FILE, "logo.zip").returncode == 0
    assert yaml.safe_load((root / "logo.zip.dvc").read_text())["outs"][0]["md5"]

    _commit_all(root)
    clone, result = _clone_and_pull(root, "reviewer-full", dvc_bin)
    assert result.returncode != 0
    assert not (clone / "logo.zip").exists()


def test_an_expanded_zip_import_must_not_use_no_download(repo):
    """`--no-download` + extract_zips writes a stage that cannot be pulled.

    Not even locally: fetch transfers nothing and checkout fails with
    `CheckoutError`. A zip member publishes no md5 (D9 — member integrity is
    CRC32 plus the workspace hash at extraction), so the recorded .dir has no
    per-member checksum for DVC to fetch against, and the source-fallback that
    makes a plain partial import pullable (D17) has nothing to work with.

    Pinned so we notice if DVC changes it. A full import (no --no-download) works
    and is what decisions.md D20 tells people to use.
    """
    root, run = repo
    assert run(
        "import-url", "--no-download", "--fs-config", "extract_zips=true", "--",
        ZENODO_FILE, "tree",
    ).returncode == 0
    spec = yaml.safe_load((root / "tree.dvc").read_text())
    assert spec["deps"][0]["md5"].endswith(".dir")  # the stage looks fine...
    assert run("pull").returncode != 0             # ...and cannot be materialised
    assert not (root / "tree").exists()


def test_an_expanded_zip_full_import_does_work(repo):
    """The supported form: no --no-download, bytes land at import time."""
    root, run = repo
    result = run(
        "import-url", "--fs-config", "extract_zips=true", "--", ZENODO_FILE, "tree"
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (root / "tree").iterdir()) == [
        "github-mark.svg",
        "md5sum",
    ]
