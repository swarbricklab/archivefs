# Configuration

**Four layers, XDG-shaped, and one rule about which of them may change your data.**

```
-o / --fs-config / kwarg   >   $ARCHIVEFS_*   >   local   >   project
                           >   user           >   system  >   built-in default
```

Same precedence [`credentials.md`](credentials.md) uses for secrets — option, then
environment, then file — so learning it once is enough.

| layer | file | travels with the data? |
|---|---|---|
| `system` | `$XDG_CONFIG_DIRS/archivefs/config.json` (default `/etc/xdg/…`) | no |
| `user` | `$XDG_CONFIG_HOME/archivefs/config.json` (default `~/.config/…`) | no |
| `project` | `.archivefs.json` at the repository root | **yes — committed** |
| `local` | `.archivefs.local.json` beside it | no — gitignored |

`$ARCHIVEFS_CONFIG` names one file to use *as* the user layer, like git's
`GIT_CONFIG_GLOBAL`. `archivefs config` shows all of it.

## The one rule

**Only a layer that travels with the data may change what the data is.**

`--fs-config extract_zips=true` is written into the `.dvc` file, so it travels: a
colleague's `dvc pull` reproduces the same tree. The same setting arriving from
`~/.config/archivefs/config.json` would leave the `.dvc` file with no record of it, and
that identical file would then resolve to a **different tree** on a different machine —
silently. That is the reproducibility hole this project is arranged to avoid, reached
sideways through a convenience.

So every option is one of two kinds, and a layer that cannot honour a kind refuses it.

**Machine settings** — change what a read *costs*, never what it returns. Settable
from `system`, `user` and `local`; refused in `project`, because a committed
`/scratch/...` path is wrong for everyone who did not write it.

`cache_dir` · `scratch_dir` · `cache_max_age_days` · `max_concurrent_reads` ·
`block_size` · `max_retries` · `request_timeout` · `transfer_timeout` · `api_url` ·
`base_url` · `s3_url` · `credentials_file`

**Content settings** — change which bytes, or which names, come back. Settable **only**
from `project`, because only `project` is committed next to the data it describes.

`extract_zips` · `strip_components` · `view` · `layout` · `chiro` · `selection` ·
`include_index_files` · `zip_limits`

**Credentials** are refused from every layer, committed or not. They have their own
file, with its own permission check and its own policy
([`credentials.md`](credentials.md)). A *path* to one — `credentials_file`, `ngc` — is
not a secret and is settable, the same distinction ssh draws with `IdentityFile`.

**Unknown keys are refused too.** A typo that silently does nothing is the failure mode
config models are famous for.

## Shape

A JSON object. Keys at the top level apply to every protocol; a protocol name
introduces a section that overrides them for that protocol.

```json
{
  "cache_dir": "/scratch/$PROJECT/$USER/archivefs-cache",
  "scratch_dir": "/scratch/$PROJECT/$USER/tmp",
  "cache_max_age_days": 14,
  "max_concurrent_reads": 8,

  "ega":    { "credentials_file": "~/.config/archivefs/ega-projectB.json" },
  "zenodo": { "api_url": "https://zenodo.example.org/api" }
}
```

JSON rather than TOML: `requires-python` is 3.9, so there is no `tomllib`; the CLI is
deliberately stdlib-only; and credential files are already JSON, so it is one syntax.

Note that `$PROJECT` above is **not** expanded — JSON has no variables. `~` is. Write
the path out, or set `$ARCHIVEFS_CACHE_DIR` in your shell profile instead, which is
expanded by the shell.

## Worked setups

### Turn on resumable imports, once

Naming the directory **is** the opt-in — from then on every import resumes instead of
starting again ([`cli-cache.md`](cli-cache.md)).

```json
// ~/.config/archivefs/config.json
{
  "cache_dir": "/scratch/a56/jr9959/archivefs-cache",
  "scratch_dir": "/scratch/a56/jr9959/tmp"
}
```

Neither ever defaults to `$HOME`, and pointing either there warns: one entry is as
large as the archive it caches, and a home quota is not (10 GB on Gadi).

### A site default for everyone on the cluster

`$XDG_CONFIG_DIRS` is already set to a shared directory on many HPC setups, which makes
the `system` layer the right place for "the scratch filesystem everyone here should
use". No per-user setup at all:

```json
// /g/data/a56/config/xdg/archivefs/config.json
{ "scratch_dir": "/scratch/a56/tmp", "max_concurrent_reads": 8 }
```

### A dataset that has to be expanded the same way for everyone

Committed, so it travels with the clone:

```json
// .archivefs.json  — commit this
{ "zenodo": { "extract_zips": true, "strip_components": 0 } }
```

### One machine that is different

```json
// .archivefs.local.json  — gitignored
{ "cache_dir": "/local/nvme/blocks" }
```

## Inspecting it

```console
$ archivefs config
layers, highest precedence first:
    local    /home/me/work/study/.archivefs.local.json
              machine settings
  * project  /home/me/work/study/.archivefs.json
              content settings, committed
  * user     /home/me/.config/archivefs/config.json
              machine settings
    system   /etc/xdg/archivefs/config.json
              machine settings
  (* = present;  above all of these: $ARCHIVEFS_* then -o/--fs-config)

resolved:
  zenodo://
      cache_dir = '/scratch/a56/jr9959/archivefs-cache'
      extract_zips = True
```

`archivefs config path` prints the user layer's path and nothing else, so
`$EDITOR $(archivefs config path)` works. `--json` gives the machine-readable form.

## Turning things off

Every step of the chain can say *off*, not just *a value* — because the direction that
has to work is disabling something for one command without editing a file the rest of
your work depends on:

```console
$ archivefs ls -o cache_dir=false 'zenodo://17411292'
$ dvc import-url --fs-config cache_dir=false 'zenodo://…' out
```

`false`, `no`, `off`, `none` and the empty string all mean off.

## Errors

A bad config file fails at the point a filesystem is built, naming the file, the layer,
and where the setting does belong:

```console
$ archivefs ls 'zenodo://1486184'
ConfigError: /home/me/.config/archivefs/config.json (user layer) may not set
  'extract_zips': it changes which bytes an archive returns, and this layer does not
  travel with the data — the same .dvc file would resolve differently on another
  machine with nothing to say why. Pass it per import (`--fs-config extract_zips=…`,
  which DVC records in the .dvc file), or put it in .archivefs.json, which is
  committed.
```

Refused rather than warned, throughout. A warning in a pipeline log is invisible at
exactly the moment it matters — which is the argument `credentials.py` already makes
for refusing a password on the command line rather than discouraging one.

Full reasoning: [`decisions.md`](decisions.md) D30.
