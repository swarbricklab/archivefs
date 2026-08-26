# Giving reviewers access to unpublished data

You have submitted a paper. Its data is deposited but not released, so the `.dvc`
files in your repository point at deposits nobody outside the project can read yet.
Reviewers need to be able to `dvc pull` — or at least `archivefs ls` — without you
mailing anyone a password, and without the repository itself changing between now
and publication.

That is what `archivefs credentials export` and `archivefs credentials import` are
for. Two commands:

```console
# you, once, in the project
$ archivefs credentials export --out ~/smith-2026-access.zip \
      --description "reviewer access, Smith et al. 2026"

# each reviewer, once
$ archivefs credentials import smith-2026-access.zip
```

Nothing about the repository changes. The keys never touch a `.dvc` file, `.dvc/config`
or git; after publication the same `.dvc` files work for everyone with no key at all.
See [`decisions.md`](decisions.md) D31 for why that invariance holds.

---

## The shortest route: send the link you already have

If everything reviewers need is one BioStudies study, there is no artefact at all.
BioStudies' sharing link *contains* the key, so a reviewer can paste it straight back:

```console
$ echo 'https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3080?key=<uuid>' \
    | archivefs credentials import -
  + bia     key      S-BIAD3080             ~/.config/archivefs/bia.json

wrote ~/.config/archivefs/bia.json (mode 0600)

verify with:
  archivefs ls bia://S-BIAD3080
```

Read from stdin rather than as an argument, deliberately: an argument lands in
`~/.bash_history` and in the process table, where a key visible to anyone running
`ps` is not much of a key.

Prefer this when it applies. There is no file to mislay, nothing to go stale, and
the link is already in your cover letter.

---

## What can and cannot be handed over

Two different things get called "credentials", and only one of them can go in a
bundle.

| | capability | identity |
|---|---|---|
| example | BioStudies per-study sharing key | EGA password, Zenodo API token, dbGaP `.ngc` |
| what it opens | one named deposit | everything your account can reach |
| meant to be shared? | yes — that is what it is for | no |
| revoking it | affects that deposit | means rotating your account |
| in a bundle? | **yes** | **refused** |

`archivefs credentials export` refuses to package an identity. Not "warns" —
refuses, and there is no flag. The failure it prevents is a researcher running an
obvious-sounding command and mailing four strangers a file containing their own EGA
password, which is a mistake that cannot be taken back.

So a bundle can only help with the archives that issue per-deposit capabilities.
Today that is BioStudies; the same shape will fit Zenodo's per-record preview links
when they are supported. For everything else the report tells reviewers what they
must arrange themselves:

```console
$ archivefs credentials export --out ~/access.zip
/scratch/.../paper depends on 4 deposit(s):
  bia://S-BIAD3080          included     key from ~/.config/archivefs/bia.json (S-BIAD3080)
  ega://EGAD00001006673     personal     password, username authenticate an account, not a
                                         deposit — a reviewer needs their own, and no bundle
                                         can supply them
  geo://GSE12345            public       needs no credential; nothing to hand over
  zenodo://1486184          personal     public unless this deposit is restricted; token
                                         authenticates an account and cannot be handed on

wrote /home/you/access.zip (1 grant(s), mode 0600)
```

That report is worth reading even when you are not exporting anything. It is an
access audit of the paper: every deposit the repository depends on, and what a
stranger would need for each.

For an EGA dataset the honest answer is that reviewers need their own EGA account
authorised by the Data Access Committee, and journals generally expect that to be
arranged before submission.

### Which deposits get included

The bundle is built from **the repository**, not from your credential store. We read
the `*.dvc` files, `dvc.lock`, `dvc.yaml` and `.dvc/config` for archivefs URLs, work
out which deposits this project depends on, and export keys for exactly those.

That matters if you have submitted more than one paper. Your `bia.json` may hold
three keys; reviewers of the first paper get one.

Add something the repository does not mention with `--include`:

```console
$ archivefs credentials export --include bia:S-BIAD7777 --out ~/access.zip
```

### `$BIA_KEY` is not per-study

If your key comes from the environment rather than from a per-study entry in
`~/.config/archivefs/bia.json`, it answers for *any* accession asked of it — so
exporting for two studies would pin the same key to both, and only one would work.
The report says so:

```
  bia://S-BIAD7777          included     key from $BIA_KEY, which is not per-study —
                                         check it is this deposit's
```

Move it into the file to get per-study accuracy:

```json
{"keys": {"S-BIAD3080": "…", "S-BIAD7777": "…"}}
```

---

## Sending it safely

The bundle holds live keys. It is written `0600` and it is exactly as sensitive as
the keys inside it, so send it the way you would send a password.

**There is no `--password` flag, and that is deliberate.** Python's `zipfile` cannot
write encrypted archives, and the legacy ZipCrypto scheme it can *read* was broken
decades ago. A password prompt would be a promise archivefs cannot keep. Instead the
bundle can be written to stdout, so real encryption composes:

```console
$ archivefs credentials export --out - | age -r age1ql3z…  > access.age
# and on the other side
$ age -d access.age | archivefs credentials import -
```

`gpg --encrypt` works the same way. Writing to a terminal is refused — binary output
in your scrollback is no use, and the keys would be in it.

### It will not write into a git repository

```console
$ archivefs credentials export --out ./access.zip
ERROR: ./access.zip is inside the git repository at /scratch/.../paper and is not
  ignored by it.
  A bundle holds live access keys; one `git add -A` publishes them to everyone who
  can read the repo, and rewriting history does not un-publish a key.
```

An already-ignored path is fine and needs no flag — git is asked, via
`git check-ignore`, rather than us re-implementing `.gitignore`. `--force` overrides.

---

## What import does

```console
$ archivefs credentials import access.zip
  + bia     key      S-BIAD3080             /home/reviewer/.config/archivefs/bia.json
  (reviewer access, Smith et al. 2026)

wrote /home/reviewer/.config/archivefs/bia.json (mode 0600)

verify with:
  archivefs ls bia://S-BIAD3080/images/he/alignment_files/xenium/P17_Xe5k_matrix.csv
```

* Writes to the same file the resolver reads, `0600`, under a `0700` directory.
* **Merges.** A second bundle for a second study sits beside the first.
* **Never overwrites silently.** A key that differs from one already held is a
  conflict: nothing is written, and the error names the deposit.

  ```console
  $ archivefs credentials import newer.zip
  ERROR: 1 credential(s) already set to a different value:
      bia://S-BIAD3080 key in /home/reviewer/.config/archivefs/bia.json
    Nothing was written. A key replaced without you noticing fails later, as a 403
    with no obvious cause.
    Keep what you have, or re-run with --force to take the bundle's.
  ```

* **Idempotent.** Importing the same bundle twice says `already installed`.
* Refuses a bundle carrying anything not marked shareable, however it was produced,
  and says the sender should rotate it.
* `--dry-run` prints the plan and writes nothing.

Neither command talks to the network. Both are pure local file operations.

---

## What you hold: `archivefs credentials list`

```console
$ archivefs credentials list
credentials archivefs can see. Values are never shown.

  bia://     key        shareable  ~/.config/archivefs/bia.json  [S-BIAD3080, S-BSST1234]
  dbgap://   ngc        personal   not found
  ega://     username   personal   $EGA_USERNAME
             password   personal   $EGA_PASSWORD
  geo://     public; needs no credential
  zenodo://  token      personal   not found (optional)

  shareable = issued to be handed on, scoped to named data
  personal  = authenticates you; a bundle will refuse to carry it
```

Values are never printed, by any of these commands, in any mode — not in the export
report, not in an error message, not in `--json`. A file whose permissions the
resolver would refuse is flagged here rather than silently skipped at pull time.

---

## After publication

Nothing to undo. A BioStudies key keeps working on a released study, and a released
study needs no key, so a reviewer's installed key simply stops mattering. If you want
it gone, delete the entry from `~/.config/archivefs/bia.json`.

Revoking early is the depositor's business, in BioStudies: regenerate the sharing
link and every bundle carrying the old key stops working at once. That is the
property that makes a capability safer to hand out than an account.

---

## The bundle format

A zip holding two files:

* `archivefs-bundle.json` — the manifest
* `README.txt` — plain-English instructions for a reviewer who opens the zip instead
  of reading your email

```json
{
  "archivefs_bundle": 1,
  "description": "reviewer access, Smith et al. 2026",
  "grants": [
    {"protocol": "bia", "credential": "key", "scope": "S-BIAD3080", "value": "…"}
  ],
  "verify": ["bia://S-BIAD3080/images/he/alignment_files/xenium/P17_Xe5k_matrix.csv"]
}
```

`--out something.json` writes the manifest alone, with no zip and no README — useful
for scripting. Import sniffs the bytes, so a zip, a bare manifest and a sharing link
are all accepted from a file or from stdin.

Writing is deterministic: the same grants produce byte-identical output, so a bundle
can be checksummed and a recipient can confirm they got the one you sent. The
manifest is versioned; a bundle from a newer archivefs is refused with an upgrade
instruction rather than half-read.

---

## See also

* [`credentials.md`](credentials.md) — where credentials come from, and what is refused
* [`importing-from-bia.md`](importing-from-bia.md) — the private-study import itself
* [`decisions.md`](decisions.md) — D31 (`.dvc` invariance across publication),
  D32 (why bundles carry capabilities only)
