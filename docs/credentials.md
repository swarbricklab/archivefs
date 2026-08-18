# Configuring credentials for DVC + archivefs

**The rule the whole design serves: credentials are resolved from the local
environment at pull time, and never written into a committed artifact.**

An accession is not a secret. A `.dvc` file that says
`path: ega://EGAD00001006673/EGAF00004908765` reveals nothing an authorised reader
does not already have; a `.dvc` file containing your EGA password would be a breach in
version control forever. So the accession is committed, the credential is not, and the
two are recombined at `dvc pull` time on whichever machine is pulling.

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

## The precedence chain

Identical for every archive, so learning it once is enough:

1. **`storage_options`** — what DVC passes from `.dvc/config`, `.dvc/config.local`, or
   a `--fs-config` on the command line
2. **environment variables**
3. **an on-disk JSON file**

First hit wins. When nothing is found, the error names every place it looked, in
order — the message doubles as this documentation:

```console
$ archivefs ls ega://EGAD00001006673
CredentialsNotFoundError: no username and password found for EGA. Looked, in order:
    username:
      1. storage_options['username'] (from .dvc/config or .dvc/config.local)
      2. $EGA_USERNAME
      3. ~/.pyega3/credentials.json
      4. ~/.config/archivefs/ega.json
      (your EGA account email address)
    password:
      1. storage_options['password'] (put it in .dvc/config.local — never the committed .dvc)
      2. $EGA_PASSWORD
      3. ~/.pyega3/credentials.json
      4. ~/.config/archivefs/ega.json
      (EGA account password. On shared HPC use a per-user environment variable from
       your own profile, never a shared file — that keeps access traceable and
       revocable per person.)
  Nothing was found, so the request was not attempted.
  Note: secrets must never be written into a committed .dvc file — put them in
  .dvc/config.local (git-ignored) or the environment.
```

Per archive:

| archive | option keys | environment | files |
|---|---|---|---|
| EGA | `username`, `password` | `$EGA_USERNAME`, `$EGA_PASSWORD` | `~/.pyega3/credentials.json`, `~/.config/archivefs/ega.json` |
| Zenodo | `token` | `$ZENODO_TOKEN` | `~/.config/archivefs/zenodo.json` |
| dbGaP | `ngc` (a *path*) | `$DBGAP_NGC` | — |

`~/.pyega3/credentials.json` is read for compatibility, so an existing pyega3 setup
works unchanged — archivefs does not depend on pyega3 itself
([`decisions.md`](decisions.md) D2).

## Where to put things when using DVC

Four places a setting can come from, and only two of them are safe for secrets:

| location | committed to git? | secrets? |
|---|---|---|
| `.dvc/config` | **yes** | ❌ never |
| `.dvc/config.local` | no — DVC gitignores it | ✅ yes |
| `--fs-config` on the command line | **yes** — written into the `.dvc` file | ❌ never |
| environment variables | no | ✅ yes, and the best option on shared systems |

`--fs-config` is the trap, because it looks like a command-line flag rather than a
file. It is not: DVC records it in the `.dvc` file's `deps[].fs_config`, which you then
commit. Use it for `view`, `layout`, `api_url`, `chiro` — never for a credential.

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

## Recipe: EGA credentials scoped to one repo

When environment variables are awkward, DVC's gitignored local config works:

```console
$ dvc remote add ega ega://EGAD00001006673
$ dvc remote modify --local ega username you@example.org
$ dvc remote modify --local ega password '…'
$ git check-ignore -v .dvc/config.local     # confirm it is ignored
.dvc/.gitignore:1:/config.local	.dvc/config.local
```

Note `--local` on both. Without it they go into `.dvc/config`, which is committed.
Then import through the remote — `dvc import-url remote://ega/EGAF00004908765 …` — and
be aware of the trade-off in [`using-with-dvc.md`](using-with-dvc.md): the accession
moves out of the `.dvc` file and into `.dvc/config`.

Other EGA options that are *not* secrets and may live in the committed `.dvc/config`:
`layout` (`accession` / `display` / `nested`), `chiro`, `api_url`, `credentials_file`.

## Recipe: a restricted Zenodo record

```bash
export ZENODO_TOKEN='…'
```

or `~/.config/archivefs/zenodo.json`:

```json
{ "token": "…" }
```

Public records need no token, and most do not have one configured — which is why the
Zenodo credential is declared optional rather than required.

## Checking you have not committed a secret

The invariant is enforced in the design (each credential declares whether it is a
secret) but there is **not yet** a CLI command that audits a repository — see
[`ideas.md`](ideas.md) #17. Until there is, before pushing:

```console
$ git grep -nE '(password|token)\s*[:=]' -- '*.dvc' .dvc/config
$ git log -p -- .dvc/config | grep -iE 'password|token'    # and the history
```

If something did get committed: rotate the credential first — rewriting history does
not un-leak it — then clean up the repository.

## Failure modes worth recognising

| symptom | meaning |
|---|---|
| `CredentialsNotFoundError` listing four locations | nothing was found; the request was never attempted |
| HTTP 401 on the first call | wrong username or password |
| 401 *mid-transfer* | the access token expired; archivefs refreshes and retries once, so you should not see this |
| authenticated but "not authorised for this dataset" | your account is fine; the DAC has not granted you *this* dataset |
| `connection refused` / hangs on EGA | EGA's endpoints are on **port 8443**, which many institutional firewalls block. It is reachable from NCI. |

## See also

[`using-with-dvc.md`](using-with-dvc.md) · [`cli-dvc.md`](cli-dvc.md) ·
[`decisions.md`](decisions.md) D1–D3 (EGA specifics) · `src/archivefs/credentials.py`
if you want the enforcement rather than the description
