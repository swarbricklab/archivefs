"""Credential bundles: what may travel, what must not, and what lands on disk.

Two properties carry most of the weight here and are worth naming, because the rest
of the file is mechanics around them:

* **the exporter cannot be talked into packaging an identity.** Tested from the
  backends' own declarations rather than from a list kept here, so a new backend that
  forgets to think about it fails a test instead of shipping.
* **no value ever reaches a report.** Every human-readable path — the export report,
  ``credentials list``, the conflict error, the plan — is asserted against the literal
  secret, because "we do not print it" is a property that decays one helpful error
  message at a time.
"""

from __future__ import annotations

import json
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from archivefs import bundle as bundles
from archivefs.cli import main
from archivefs.credentials import CredentialResolver
from archivefs.errors import ArchiveFSError
from archivefs.registry import PROTOCOLS

KEY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "99999999-8888-7777-6666-555555555555"
ACC = "S-BIAD3080"
LINK = f"https://www.ebi.ac.uk/biostudies/bioimages/studies/{ACC}?key={KEY}"


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """A developer's own $BIA_KEY must not decide what these tests see."""
    for protocol in PROTOCOLS:
        try:
            declared = bundles.declared(protocol)
        except ArchiveFSError:  # pragma: no cover - an uninstalled extra
            continue
        for credential in declared:
            for variable in credential.env:
                monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def paper(tmp_path):
    """A repository that depends on one private BIA study and three other things."""
    root = tmp_path / "paper"
    (root / ".dvc").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "matrix.csv.dvc").write_text(
        "deps:\n"
        f"- path: bia://{ACC}/images/he/alignment_files/xenium/P17_Xe5k_matrix.csv\n"
        "outs:\n- path: matrix.csv\n"
    )
    (root / "logo.zip.dvc").write_text(
        "deps:\n- path: zenodo://1486184/logo.zip\nouts:\n- path: logo.zip\n"
    )
    (root / "reads.dvc").write_text(
        "deps:\n- path: ega://EGAD00001006673/EGAF00004908765\n"
        "outs:\n- path: reads.bam\n"
    )
    (root / "counts.dvc").write_text(
        "deps:\n- path: geo://GSE12345/counts.txt.gz\nouts:\n- path: counts.txt.gz\n"
    )
    return root


@pytest.fixture
def depositor(monkeypatch):
    """Someone holding a sharing key for ``ACC`` and nothing else."""
    monkeypatch.setenv("BIA_KEY", KEY)


def bia_file() -> Path:
    return Path("~/.config/archivefs/bia.json").expanduser()


# ------------------------------------------------- what is allowed to travel


def test_exactly_one_credential_is_shareable_and_it_is_a_per_study_key():
    """A tripwire, not a tautology.

    The whole feature rests on ``shareable`` being set by someone who thought about
    it. Enumerating the answer here means adding a backend, or flipping a flag on an
    existing one, has to come past this line — where the reviewer of that change is
    looking at the security question rather than at plumbing.
    """
    marked = {
        (protocol, credential.name)
        for protocol in PROTOCOLS
        for credential in bundles.declared(protocol)
        if credential.shareable
    }
    assert marked == {("bia", "key")}


def test_a_shareable_credential_is_scoped_to_named_data():
    """Unscoped-and-shareable is a contradiction: it would open everything."""
    for protocol in PROTOCOLS:
        for credential in bundles.declared(protocol):
            if credential.shareable:
                assert credential.scoped, (
                    f"{protocol}.{credential.name} is shareable but not scoped, so "
                    f"handing it over grants access to every deposit it reaches"
                )


def test_an_account_credential_is_never_collected(paper, monkeypatch):
    monkeypatch.setenv("EGA_USERNAME", "someone@example.org")
    monkeypatch.setenv("EGA_PASSWORD", "hunter2")
    monkeypatch.setenv("ZENODO_TOKEN", "zen-tok")
    pack, rows, _ = bundles.collect(paper)
    assert pack.grants == []
    statuses = {(r.protocol, r.status) for r in rows}
    assert ("ega", "personal") in statuses
    assert ("zenodo", "personal") in statuses
    assert "hunter2" not in json.dumps(bundles._manifest(pack))


def test_a_forged_bundle_carrying_a_password_is_refused():
    forged = bundles.Bundle(
        grants=[bundles.Grant("ega", "password", None, "hunter2")]
    )
    with pytest.raises(ArchiveFSError, match="does not treat as shareable"):
        bundles.plan(forged)


# ------------------------------------------------------ derived from the repo


def test_the_scan_finds_every_deposit_the_repository_depends_on(paper):
    found = {(u.protocol, u.scope) for u in bundles.scan(paper)}
    assert found == {
        ("bia", ACC),
        ("zenodo", "1486184"),
        ("ega", "EGAD00001006673"),
        ("geo", "GSE12345"),
    }


def test_a_key_for_an_unrelated_deposit_is_not_exported(paper, tmp_path):
    """The argument for scanning the repo at all, in one test.

    A depositor reviewing three papers has three keys in one file. Reviewers of the
    first must not receive the other two, and they will not notice if they do.
    """
    bia_file().parent.mkdir(parents=True, exist_ok=True)
    bia_file().write_text(json.dumps({"keys": {ACC: KEY, "S-BIAD9999": OTHER}}))
    bia_file().chmod(0o600)
    pack, _, _ = bundles.collect(paper)
    assert [g.scope for g in pack.grants] == [ACC]
    assert OTHER not in json.dumps(bundles._manifest(pack))


def test_include_adds_a_deposit_the_repository_does_not_mention(paper, depositor):
    pack, rows, _ = bundles.collect(paper, ["bia:S-BIAD7777"])
    assert {g.scope for g in pack.grants} == {ACC, "S-BIAD7777"}
    assert any(r.scope == "S-BIAD7777" for r in rows)


def test_include_wants_a_protocol(paper):
    with pytest.raises(ArchiveFSError, match="PROTOCOL:ACCESSION"):
        bundles.collect(paper, ["S-BIAD3080"])


def test_a_flat_key_is_reported_as_not_per_study(paper, depositor):
    """$BIA_KEY answers for any accession asked of it, which the report must say.

    Otherwise exporting for a second study silently pins the first study's key to it,
    and the failure surfaces as a 403 on the reviewer's machine with nothing to
    connect it to the depositor's environment.
    """
    _, rows, _ = bundles.collect(paper, ["bia:S-BIAD7777"])
    included = [r for r in rows if r.status == "included"]
    assert len(included) == 2
    assert all("not per-study" in r.detail for r in included)


def test_a_per_study_key_is_reported_without_the_caveat(paper):
    bia_file().parent.mkdir(parents=True, exist_ok=True)
    bia_file().write_text(json.dumps({"keys": {ACC: KEY}}))
    bia_file().chmod(0o600)
    _, rows, _ = bundles.collect(paper)
    included = [r for r in rows if r.status == "included"]
    assert len(included) == 1
    assert "not per-study" not in included[0].detail


def test_a_missing_key_is_reported_rather_than_omitted(paper):
    _, rows, _ = bundles.collect(paper)
    missing = [r for r in rows if r.status == "missing"]
    assert [r.scope for r in missing] == [ACC]
    assert "unreleased" in missing[0].detail


# --------------------------------------------------------------- the artefact


def test_a_bundle_round_trips(paper, depositor):
    pack, _, _ = bundles.collect(paper)
    pack.description = "reviewer access"
    restored = bundles.read(bundles.write(pack), "test")
    assert restored.grants == pack.grants
    assert restored.description == "reviewer access"


def test_the_same_grants_produce_the_same_bytes(paper, depositor):
    """A bundle you cannot checksum is a bundle you cannot confirm receipt of."""
    pack, _, _ = bundles.collect(paper)
    assert bundles.write(pack) == bundles.write(pack)


def test_the_zip_carries_a_readme_naming_the_file_it_is(paper, depositor):
    pack, _, _ = bundles.collect(paper)
    raw = bundles.write(pack, filename="smith-2026-access.zip")
    with zipfile.ZipFile(__import__("io").BytesIO(raw)) as archive:
        assert sorted(archive.namelist()) == sorted(
            [bundles.MANIFEST_NAME, bundles.README_NAME]
        )
        readme = archive.read(bundles.README_NAME).decode()
    assert "smith-2026-access.zip" in readme
    assert f"bia://{ACC}" in readme
    assert KEY not in readme


def test_json_output_is_the_manifest_alone(paper, depositor):
    pack, _, _ = bundles.collect(paper)
    parsed = json.loads(bundles.write(pack, as_zip=False))
    assert parsed["archivefs_bundle"] == bundles.BUNDLE_VERSION


def test_a_zip_without_a_manifest_says_what_it_holds(tmp_path):
    path = tmp_path / "wrong.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("photo.png", b"not a bundle")
    with pytest.raises(ArchiveFSError, match=r"photo\.png"):
        bundles.read(path.read_bytes(), str(path))


def test_a_bundle_from_the_future_is_refused():
    payload = json.dumps({"archivefs_bundle": 99, "grants": []}).encode()
    with pytest.raises(ArchiveFSError, match="Upgrade archivefs"):
        bundles.read(payload, "test")


def test_unrecognisable_input_names_both_things_it_could_have_been():
    with pytest.raises(ArchiveFSError) as caught:
        bundles.read(b"hello there", "the input")
    assert "credentials export" in str(caught.value)
    assert "?key=" in str(caught.value)


# ------------------------------------------------------------ sharing links


def test_a_sharing_link_is_a_bundle():
    pack = bundles.read(LINK.encode(), "the input")
    assert pack.grants == [bundles.Grant("bia", "key", ACC, KEY)]


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.ebi.ac.uk/biostudies/bioimages/studies/{ACC}?key={KEY}",
        f"https://www.ebi.ac.uk/biostudies/studies/{ACC}?key={KEY}",
        f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{ACC}?key={KEY}",
        f"https://www.ebi.ac.uk/biostudies/api/v1/files/{ACC}?key={KEY}",
        f"bia://{ACC}?key={KEY}",
    ],
)
def test_every_shape_of_biostudies_link_scopes_the_key_to_its_study(url):
    pack = bundles.from_links(url)
    assert pack.grants == [bundles.Grant("bia", "key", ACC, KEY)]


def test_a_link_with_a_key_but_no_recognisable_study_still_yields_it():
    """Better an unscoped key than a refusal: the resolver treats it as a catch-all."""
    pack = bundles.from_links(f"https://example.org/whatever?key={KEY}")
    assert pack.grants == [bundles.Grant("bia", "key", None, KEY)]


def test_a_link_without_a_key_carries_nothing():
    assert bundles.from_links(f"https://www.ebi.ac.uk/biostudies/studies/{ACC}") == (
        bundles.Bundle(description="from a sharing link")
    )


def test_several_links_install_together():
    text = f"{LINK}\nhttps://www.ebi.ac.uk/biostudies/studies/S-BSST1?key={OTHER}\n"
    pack = bundles.from_links(text)
    assert {(g.scope, g.value) for g in pack.grants} == {
        (ACC, KEY),
        ("S-BSST1", OTHER),
    }


# ---------------------------------------------------------------- installing


def test_import_writes_a_file_only_you_can_read():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    path = bia_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not stat.S_IMODE(path.parent.stat().st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    )
    assert json.loads(path.read_text()) == {"keys": {ACC: KEY}}


def test_what_import_writes_is_what_the_resolver_reads():
    """The end-to-end invariant: the two halves must agree on shape and location.

    They are written in different modules from different declarations, and nothing
    else would notice them drifting apart until a reviewer's pull failed.
    """
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    resolver = CredentialResolver("BioImage Archive", bundles.declared("bia"))
    assert resolver.get("key", scope=ACC) == KEY


def test_import_is_idempotent():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    second = bundles.plan(bundles.from_links(LINK))
    assert [c.action for c in second] == ["unchanged"]
    assert bundles.install(second) == []


def test_a_second_deposit_merges_beside_the_first():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    other = f"https://www.ebi.ac.uk/biostudies/studies/S-BSST1?key={OTHER}"
    bundles.install(bundles.plan(bundles.from_links(other)))
    assert json.loads(bia_file().read_text()) == {
        "keys": {ACC: KEY, "S-BSST1": OTHER}
    }


def test_a_differing_key_is_a_conflict_and_nothing_is_written():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    replacement = f"https://www.ebi.ac.uk/biostudies/studies/{ACC}?key={OTHER}"
    changes = bundles.plan(bundles.from_links(replacement))
    assert [c.action for c in changes] == ["conflict"]
    with pytest.raises(ArchiveFSError) as caught:
        bundles.install(changes)
    assert OTHER not in str(caught.value)
    assert KEY not in str(caught.value)
    assert json.loads(bia_file().read_text()) == {"keys": {ACC: KEY}}


def test_force_takes_the_bundles_value():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    replacement = f"https://www.ebi.ac.uk/biostudies/studies/{ACC}?key={OTHER}"
    bundles.install(bundles.plan(bundles.from_links(replacement)), force=True)
    assert json.loads(bia_file().read_text()) == {"keys": {ACC: OTHER}}


def test_an_existing_group_readable_file_is_tightened_not_left():
    path = bia_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keys": {"S-BSST1": OTHER}}))
    path.chmod(0o644)
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["keys"]["S-BSST1"] == OTHER


def test_a_destination_that_is_not_json_is_left_alone(tmp_path):
    path = bia_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json")
    with pytest.raises(ArchiveFSError, match="will not overwrite"):
        bundles.plan(bundles.from_links(LINK))
    assert path.read_text() == "this is not json"


# -------------------------------------------------------------- write safety


def test_writing_into_a_git_repository_is_refused(paper):
    objection = bundles.refuse_in_repo(paper / "reviewers.zip")
    assert objection is not None
    assert "git add -A" in objection


def test_an_ignored_path_is_allowed(paper):
    (paper / ".gitignore").write_text("*.zip\n")
    assert bundles.refuse_in_repo(paper / "reviewers.zip") is None


def test_outside_a_repository_there_is_nothing_to_object_to(tmp_path):
    assert bundles.refuse_in_repo(tmp_path / "reviewers.zip") is None


# --------------------------------------------------------- nothing leaks out


def test_no_value_reaches_the_export_report(
    paper, depositor, tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(paper)
    out = tmp_path / "reviewers.zip"
    assert main(["credentials", "export", "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert KEY not in captured.err
    assert KEY not in captured.out
    # ...and it *is* in the bundle itself, which is the point of the bundle.
    assert bundles.read(out.read_bytes(), "x").grants[0].value == KEY


def test_no_value_reaches_credentials_list(capsys):
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    assert main(["credentials", "list"]) == 0
    captured = capsys.readouterr()
    assert KEY not in captured.out
    assert ACC in captured.out
    assert "shareable" in captured.out


def test_credentials_list_marks_personal_credentials_as_such(monkeypatch, capsys):
    monkeypatch.setenv("EGA_PASSWORD", "hunter2")
    assert main(["credentials", "list"]) == 0
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out
    assert "personal" in captured.out


def test_holdings_report_scopes_but_never_values():
    bundles.install(bundles.plan(bundles.from_links(LINK)))
    held = {h.credential: h for h in bundles.holdings("bia")}
    assert held["key"].scopes == [ACC]
    assert held["key"].shareable is True
    assert KEY not in json.dumps(held["key"].__dict__)


def test_a_group_readable_credentials_file_is_flagged_not_read():
    path = bia_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keys": {ACC: KEY}}))
    path.chmod(0o644)
    held = {h.credential: h for h in bundles.holdings("bia")}
    assert held["key"].scopes == []
    assert "chmod 600" in held["key"].warning


# ---------------------------------------------------------------------- CLI


def test_cli_export_then_import(paper, depositor, tmp_path, capsys, monkeypatch):
    out = tmp_path / "reviewers.zip"
    monkeypatch.chdir(paper)
    assert main(["credentials", "export", "--out", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    capsys.readouterr()

    bia_file().unlink(missing_ok=True)
    monkeypatch.delenv("BIA_KEY")
    assert main(["credentials", "import", str(out)]) == 0
    assert json.loads(bia_file().read_text()) == {"keys": {ACC: KEY}}
    assert "verify with" in capsys.readouterr().out


def test_cli_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    link = tmp_path / "link.txt"
    link.write_text(LINK)
    monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": link.open("rb")})())
    assert main(["credentials", "import", "-", "--dry-run"]) == 0
    assert "nothing written" in capsys.readouterr().out
    assert not bia_file().exists()


def test_cli_export_outside_a_repository_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="not inside a repository"):
        main(["credentials", "export", "--out", str(tmp_path / "x.zip")])


def test_cli_export_with_nothing_to_share_exits_nonzero(tmp_path, monkeypatch):
    root = tmp_path / "public"
    (root / ".dvc").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "x.dvc").write_text("deps:\n- path: geo://GSE1/x.txt\n")
    monkeypatch.chdir(root)
    assert main(["credentials", "export", "--out", str(tmp_path / "x.zip")]) == 1
    assert not (tmp_path / "x.zip").exists()


def test_cli_export_json_reports_every_deposit(
    paper, depositor, tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(paper)
    main(["credentials", "export", "--out", str(tmp_path / "x.zip"), "--json"])
    report = json.loads(capsys.readouterr().err)
    assert {d["protocol"] for d in report["deposits"]} == {
        "bia",
        "zenodo",
        "ega",
        "geo",
    }
    assert report["grants"] == 1


def test_cli_refuses_to_put_a_bundle_on_a_terminal(paper, depositor, monkeypatch):
    """Binary on a tty is unreadable; worse, the keys land in scrollback."""
    monkeypatch.chdir(paper)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    with pytest.raises(SystemExit, match="terminal"):
        main(["credentials", "export", "--out", "-"])


def test_cli_json_report_names_what_was_written(
    paper, depositor, tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(paper)
    out = tmp_path / "reviewers.zip"
    assert main(["credentials", "export", "--out", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().err)
    assert report["written"] == str(out)
