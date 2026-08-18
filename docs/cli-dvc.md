# `archivefs dvc`

**Register archivefs' protocols with DVC — the one setup step you cannot skip.**

```
archivefs dvc {status|enable|disable} [--json]
```

DVC has no plugin mechanism for storage backends: it looks up URL schemes in a table
it owns, and it does **not** read fsspec's entry points. So a perfectly installed
archivefs is invisible to DVC until something patches that table. `enable` installs a
`.pth` file that does it at interpreter start-up. The reasoning, and the four
alternatives that were tried first, are in [`decisions.md`](decisions.md) D4.

## The actions

| action | what it does |
|---|---|
| `status` | report whether the hook is installed and whether registration actually works. Exits 1 if the hook is missing, so it works in scripts. |
| `enable` | write `archivefs_dvc.pth` into the active environment's `site-packages`. Idempotent. |
| `disable` | remove it. Idempotent. |

`--json` applies to `status`.

## `enable`

```console
$ archivefs dvc enable
installed /…/envs/archivefs/lib/python3.12/site-packages/archivefs_dvc.pth
dvc will now resolve bia://, ega://, geo://, zenodo:// URLs.
```

**Per environment.** The `.pth` lives in one env's `site-packages`; activating a
different conda env means doing it again there. It needs write access to that
`site-packages`, which for a shared env means whoever administers it.

It survives `conda env update` (neither pip nor conda owns the file), but **not** a
from-scratch rebuild of the environment — and that is the failure mode to plan for,
because the error then appears to whoever *uses* the env, not whoever rebuilt it.

## `status`

```console
$ archivefs dvc status
hook: /…/site-packages/archivefs_dvc.pth
installed: True
registration works for: bia, ega, geo, zenodo
deferred: dbgap
```

Two independent facts, worth separating:

* **`installed`** — is the `.pth` on disk? This is what makes *other* processes
  (i.e. `dvc`) see our protocols.
* **`registration works for`** — did registration succeed *in this process*, right
  now? A protocol missing here means an import problem, not a hook problem.

When the hook is absent, `status` says what to do and exits 1:

```console
$ archivefs dvc status
hook: /…/site-packages/archivefs_dvc.pth
installed: False
registration works for: bia, ega, geo, zenodo
deferred: dbgap

DVC has no plugin hook for storage backends, so archivefs has to register itself at
interpreter start-up. Run `archivefs dvc enable`.
$ echo $?
1
```

Note `registration works for` is still fully populated: everything is importable, DVC
just cannot see it. That combination is the signature of a missing hook.

## The error this prevents

Without the hook, every archive URL fails inside DVC's config parser:

```console
$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
ERROR: failed to import zenodo://1486184/github-mark.zip. You could also try
downloading it manually, and adding it with `dvc add`. - config file error:
Unsupported URL type zenodo://
```

"Unsupported URL type" from DVC almost always means the hook, not a typo.

## `disable`

```console
$ archivefs dvc disable
removed /…/site-packages/archivefs_dvc.pth
$ archivefs dvc disable
nothing to remove
```

For one command rather than permanently, set `ARCHIVEFS_NO_DVC_HOOK=1` — the `.pth`
stays in place but does nothing:

```console
$ ARCHIVEFS_NO_DVC_HOOK=1 dvc import-url zenodo://1486184/github-mark.zip logo.zip
```

Useful for proving that a bug is or is not ours.

## Scenario: `--json` in a setup script

```console
$ archivefs dvc status --json
{
  "dvc_installed": true,
  "hook_path": "/…/site-packages/archivefs_dvc.pth",
  "hook_installed": true,
  "registered": ["bia", "ega", "geo", "zenodo"],
  "protocols": ["bia", "ega", "geo", "zenodo"],
  "deferred": ["dbgap"]
}
```

```bash
archivefs dvc status >/dev/null 2>&1 || archivefs dvc enable
```

Idempotent, so this is safe in a login script or an environment bootstrap.

## Exit status

| code | when |
|---|---|
| 0 | `enable`/`disable` succeeded; `status` found the hook installed |
| 1 | `status` found the hook missing, or DVC is not installed at all |
| 2 | filesystem error — usually no write permission on `site-packages` |

## See also

[`using-with-dvc.md`](using-with-dvc.md) for what to do once it is registered ·
[`cli-info.md`](cli-info.md) · [`decisions.md`](decisions.md) D4 ·
[`ideas.md`](ideas.md) #3 (proposing a real plugin entry point upstream)
