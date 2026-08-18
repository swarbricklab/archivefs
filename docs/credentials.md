# Configuring credentials for DVC + archivefs

**A credential comes from your environment or from a file only you can read. There
is no third option, and that is enforced rather than recommended.**

An accession is not a secret. A `.dvc` file that says
`path: ega://EGAD00001006673/EGAF00004908765` reveals nothing an authorised reader
does not already have; a `.dvc` file containing your EGA password would be a breach in
version control forever. So the accession is committed, the credential is not, and the
two are recombined at `dvc pull` time on whichever machine is pulling.

The model is ssh's. `ssh` has no `-o Password=` — it has `IdentityFile`, which is a
*path*. archivefs works the same way: paths are configurable, secrets are not.

## Which archives need anything

| protocol | credential | required? |
|---|---|---|
| `zenodo://` | personal access token | only for restricted or embargoed records |
| `geo://` | — | never; public HTTPS |
| `bia://` | — | never; open BioStudies + anonymous S3 |
| `ega://` | EGA account **username + password** | always, plus DAC authorisation for the dataset |
| `dbgap://` | path to a project `.ngc` key file | deferred — the backend is not implemented |

If you are only importing from BIA, GEO and public Zenodo, there is nothing to
configure. Skip to [`using-with-dvc.md`](using-with-dvc.md).

## The chain

Two places, identical for every archive:

1. **environment variables**
2. **an on-disk JSON file** that is not readable by anyone else

First hit wins. When nothing is found, the error names every place it looked, in
order — the message doubles as this documentation:

```console
$ archivefs ls ega://EGAD00001006673
CredentialsNotFoundError: no username and password found for EGA. Looked, in order:
    username:
      1. $EGA_USERNAME
      2. ~/.pyega3/credentials.json
      3. ~/.config/archivefs/ega.json
      (your EGA account email address)
    password:
      1. $EGA_PASSWORD
      2. ~/.pyega3/credentials.json
      3. ~/.config/archivefs/ega.json
      (EGA account password. On shared HPC use a per-user environment variable from
       your own profile, never a shared file — that keeps access traceable and
       revocable per person.)
  Nothing was found, so the request was not attempted.
  Note: those are the only places archivefs looks. Credentials cannot be passed as
  options or written into DVC config — see docs/credentials.md.
```

Per archive:

| archive | environment | files |
|---|---|---|
| EGA | `$EGA_USERNAME`, `$EGA_PASSWORD` | `~/.pyega3/credentials.json`, `~/.config/archivefs/ega.json` |
| Zenodo | `$ZENODO_TOKEN` | `~/.config/archivefs/zenodo.json` |
| dbGaP | `$DBGAP_NGC` (a *path*) | — |

`~/.pyega3/credentials.json` is read for compatibility, so an existing pyega3 setup
works unchanged — archivefs does not depend on pyega3 itself
([`decisions.md`](decisions.md) D2).

## What is refused, and by whom

There is no supported way to put a credential in a DVC config file or on a command
line. Every route is closed, and mostly not by archivefs:

```console
$ dvc remote modify ega password hunter2
ERROR: configuration error - config file error:
       extra keys not allowed @ data['remote']['ega']['password']

$ dvc remote modify --local ega password hunter2      # even the gitignored one
ERROR: configuration error - config file error:
       extra keys not allowed @ data['remote']['ega']['password']

$ dvc import-url --fs-config password=hunter2 -- ega://…  out.bam
ERROR: failed to import … - config file error: extra keys not allowed @ data['password']

$ archivefs ls -o password=hunter2 ega://EGAD00001006673
CredentialSourceError: password cannot be passed as an option to EGA. archivefs reads
credentials only from the environment or a file only you can read:
    password: $EGA_PASSWORD, ~/.pyega3/credentials.json, ~/.config/archivefs/ega.json
```

The first three are **DVC's own config validator**, not ours:
[`dvcfs.py`](../src/archivefs/dvcfs.py) omits the credential keys from the schema it
registers, so the failure happens during config parsing, before any archivefs code
runs. There is nothing to remember and nothing to get wrong.

`--fs-config` is the one that most deserved closing. It looks like a command-line
flag, but DVC records it in the `.dvc` file's `deps[].fs_config` — so
`--fs-config password=…` used to write a plaintext password straight into the file
you commit. Use it for `view`, `layout`, `api_url`, `chiro`; those are all it will
now accept.

`.dvc/config.local` is refused too, even though git ignores it. It is a real file on
a shared filesystem with default permissions, which is the thing the file-mode check
below exists to prevent.

## File permissions are checked

A credentials file that anyone else can read is refused outright, as ssh does with a
private key:

```console
$ archivefs ls ega://EGAD00001006673
CredentialSourceError: ~/.config/archivefs/ega.json is readable by others (mode 0644);
archivefs will not read a credential out of it.
  Fix it with:  chmod 600 ~/.config/archivefs/ega.json
  On a shared filesystem this is the difference between a credential that is
  traceable to you and one that is not.
```

This refuses rather than warns because a warning in a PBS job log is invisible at
exactly the moment it matters. Note that **pyega3 may have left
`~/.pyega3/credentials.json` at 0644** — if you have an existing setup, one `chmod
600` is the whole migration.

## Recipe: EGA on a shared HPC (recommended)

Environment variables from **your own** profile, never a shared file:

```bash
# ~/.bashrc, or better a file only you can read and source
export EGA_USERNAME='you@example.org'
export EGA_PASSWORD='…'
```

```console
$ archivefs ls ega://EGAD00001006673        # confirm before involving DVC
$ dvc import-url --no-download ega://EGAD00001006673/EGAF00004908765 sample.bam
$ git diff --cached sample.bam.dvc          # no username, no password — by design
```

Why per-user rather than a shared credentials file in `/g/data`: access stays
traceable to a person and revocable per person, which is what a Data Access Agreement
generally requires. A shared file makes "who pulled this dataset" unanswerable.

For a *pipeline* that runs unattended, put the same variables in the job's environment
(a PBS job script, a CI secret) rather than in the repo.

## Recipe: a credentials file

```console
$ mkdir -p ~/.config/archivefs
$ install -m 600 /dev/null ~/.config/archivefs/ega.json
$ cat > ~/.config/archivefs/ega.json <<'JSON'
{ "username": "you@example.org", "password": "…" }
JSON
```

The `install -m 600` first is deliberate: create the file private, then write to it,
so the secret is never briefly world-readable between `cat` and `chmod`.

Zenodo is the same shape at `~/.config/archivefs/zenodo.json`:

```json
{ "token": "…" }
```

Public Zenodo records need no token, which is why that credential is declared
optional rather than required.

## Recipe: two projects, two EGA accounts

This is what `credentials_file` is for. It is a **path**, not a secret, so it is the
one credential-related setting that may live in a committed config:

```console
$ dvc import-url --no-download \
    --fs-config credentials_file=~/.config/archivefs/ega-projectB.json -- \
    ega://EGAD00001006673/EGAF00004908765 sample.bam
```

or per command, `archivefs ls -o credentials_file=~/.config/archivefs/ega-projectB.json …`.
A path recorded in the committed `.dvc` tells a reader *which* account is expected
without telling them anything secret — the same trade ssh makes with `IdentityFile`.

## Checking a repository's history

New commits cannot contain one of these keys — DVC rejects them. That leaves
repositories configured before this policy, and git history, where the answer is to
**rotate the credential first** (rewriting history does not un-leak it) and clean up
afterwards:

```console
$ git grep -nE '(password|token)\s*[:=]' -- '*.dvc' .dvc/config
$ git log -p -- .dvc/config | grep -iE 'password|token'    # and the history
```

An `archivefs check-config` that automates this is [`ideas.md`](ideas.md) #17.

## Failure modes worth recognising

| symptom | meaning |
|---|---|
| `CredentialsNotFoundError` listing three locations | nothing was found; the request was never attempted |
| `CredentialSourceError: … cannot be passed as an option` | you used `-o`, `--fs-config` or an fsspec keyword; use the environment or a file |
| `CredentialSourceError: … is readable by others` | `chmod 600` the file it names |
| `extra keys not allowed @ data['remote'][…]['password']` | DVC refusing the key; the credential does not belong in config at all |
| HTTP 401 on the first call | wrong username or password |
| 401 *mid-transfer* | the access token expired; archivefs refreshes and retries once, so you should not see this |
| authenticated but "not authorised for this dataset" | your account is fine; the DAC has not granted you *this* dataset |
| `connection refused` / hangs on EGA | EGA's endpoints are on **port 8443**, which many institutional firewalls block. It is reachable from NCI. |

## See also

[`using-with-dvc.md`](using-with-dvc.md) · [`cli-dvc.md`](cli-dvc.md) ·
[`decisions.md`](decisions.md) D16 for why this is enforced rather than documented,
D1–D3 for EGA specifics · `src/archivefs/credentials.py` if you want the enforcement
rather than the description
