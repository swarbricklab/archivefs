# `archivefs ping`

**Is a pin pullable right now — and if not, whose fault? Reaches the archive and fetches
one sample byte, no download.**

```
archivefs ping [--retries N] TARGET
```

`TARGET` is a [`chiro://md5:<digest>`](chiro-protocol.md) reference, an archive URL
(`zenodo://…`, `bia://…`), or a path to a `.dvc` file (its dependency URL is pinged).

A reviewer who runs `dvc pull` and hits a gateway timeout cannot tell *the archive is down,
retry later* from *my setup is broken* from *I lack access* — they learn it only minutes
into a download, as a traceback. `ping` answers in seconds, before any transfer: it walks
the exact chain a pull uses and returns a verdict that names the culprit.

## What it does

1. **Resolves** the target — no network. A `.dvc` file yields its dependency URL; a
   `chiro://md5:` reference is looked up in `.chirograph/` and routed to its backend via
   the manifest's `concept`; an archive URL is probed directly.
2. **Reaches the record's metadata endpoint** (`fs.info`) — the same call a pull makes to
   learn what to fetch.
3. **Fetches a single sample byte** from the content endpoint (a ranged `cat_file`) — this
   proves the data is actually being served, without moving the file.

Each attempt uses a **fail-fast backend** (one retry, 30 s timeout), so a single ping is
quick; `--retries` controls how long you wait for a flaky archive, not per-request
backoff. Progress is printed to stderr as each step starts and finishes, so a slow request
does not look like a hang.

## What it prints

```console
$ archivefs ping data/pathology_annotations.dvc
pinging data/pathology_annotations.dvc
  ✓ resolved: zenodo · doi:10.5281/zenodo.18917099
  … reaching the archive (record metadata)…
  ✓ metadata ok
  … fetching a sample byte from annotations.parquet…
  ✓ content ok
verdict: PULLABLE — the data is reachable, go ahead
```

On a transient outage it says so and suggests waiting:

```console
$ archivefs ping chiro://md5:1766bd…
pinging chiro://md5:1766bd…
  ✓ resolved: zenodo · doi:10.5281/zenodo.18917099
  … reaching the archive (record metadata)…
  ✗ metadata: the archive returned HTTP 504
verdict: ARCHIVE-UNAVAILABLE (transient) — the archive returned HTTP 504
  → re-run, or `archivefs ping chiro://md5:1766bd… --retries 5` to keep trying
    while the archive recovers.
```

## Verdicts and exit status

| verdict | meaning | exit |
|---|---|---|
| **PULLABLE** | the archive served metadata and content | 0 |
| **ARCHIVE-UNAVAILABLE** | *transient* — a 5xx/gateway/timeout; the archive is down, not your setup. Retry. | 1 |
| **GONE** | the record or file is missing or withdrawn — the pin is broken | 2 |
| **AUTH** | you need credentials for this record (or they were rejected) | 2 |
| **NETWORK** | cannot reach the archive from here — no route out (a compute node has none; use a login/data-mover node) | 2 |
| **SETUP** | a local problem — an unresolvable target, a missing manifest, a missing extra | 2 |

Exit `1` is reserved for the one verdict retrying can fix, so a wrapper can loop on it.

## Ping vs confirm

* **`ping`** — *can I reach this at all, right now?* One metadata call + one byte. For a
  reviewer or a depositor sanity-checking that an archive is up before a big pull.
* [**`confirm`**](cli-confirm.md) — *does the live deposit still match the manifest,
  byte-for-byte (cheaply)?* A full published-checksum / `crc32` cross-check. `ping` is the
  reachability probe; `confirm` is the integrity check.

## See also

[`using-with-dvc.md`](using-with-dvc.md) (the reviewer pull loop) ·
[`cli-confirm.md`](cli-confirm.md) · [`chiro-protocol.md`](chiro-protocol.md) ·
[`credentials.md`](credentials.md)
