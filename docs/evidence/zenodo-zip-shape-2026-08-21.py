"""Are ZIPs in Zenodo records directories-in-a-trench-coat, or archival units?

Reads only each ZIP's central directory (ranged reads) via archivefs.
"""
import json, urllib.request, time, collections, posixpath, sys, warnings
sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")
import fsspec  # noqa: E402

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "archivefs-survey"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

targets = []
for extra in ("resource_type=dataset&q=visium%20OR%20cellranger%20OR%20microscopy%20OR%20imaging&sort=bestmatch",
              "resource_type=dataset&sort=newest"):
    for page in range(1, 9):
        hits = get(f"https://zenodo.org/api/records?size=25&page={page}&{extra}")["hits"]["hits"]
        if not hits: break
        for r in hits:
            for f in (r.get("files") or []):
                k = f.get("key", "")
                if k.lower().endswith(".zip") and f.get("size", 0) < 3_000_000_000:
                    targets.append((str(r["id"]), k, f["size"]))
        time.sleep(0.25)

seen_rec, picked = set(), []
for rid, key, size in targets:          # at most 2 zips per record, for spread
    if sum(1 for p in picked if p[0] == rid) >= 2: continue
    picked.append((rid, key, size))
    if len(picked) >= 40: break

fs = fsspec.filesystem("zenodo", extract_zips=True, skip_instance_cache=True)
shape = collections.Counter()
rows = []
for rid, key, size in picked:
    path = f"{rid}/{key}"
    try:
        members = fs.find(path)
    except Exception as exc:
        shape[f"ERROR {type(exc).__name__}"] += 1
        rows.append((rid, key, size, "error", 0, str(exc)[:60]))
        continue
    rel = [m[len(path) + 1:] for m in members]
    tops = {r.split("/")[0] for r in rel}
    if len(rel) == 0:                       cls = "empty"
    elif len(tops) == 1 and any("/" in r for r in rel):  cls = "single top-level directory"
    elif len(tops) == 1 and len(rel) == 1:  cls = "one file, no directory"
    elif all("/" not in r for r in rel):    cls = "flat bag of files"
    else:                                   cls = "several top-level entries"
    shape[cls] += 1
    rows.append((rid, key, size, cls, len(rel), sorted(tops)[:3]))
    print(f"  {rid:>9} {key[:46]:46} {len(rel):>5} members  {cls}")

print(f"\n=== {sum(shape.values())} ZIPs inspected (central directory only)")
for k, n in shape.most_common():
    print(f"  {k:32} {n:4}  {100*n/max(sum(shape.values()),1):5.1f}%")
json.dump(rows, open("zipshape.json", "w"), indent=1, default=str)
