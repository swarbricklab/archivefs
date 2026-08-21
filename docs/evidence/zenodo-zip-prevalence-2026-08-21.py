"""What fraction of a Zenodo record's files are actually ZIPs?"""
import json, urllib.request, collections, posixpath, time, sys

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "archivefs-survey"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def survey(label, extra, want=300):
    records = []
    for page in range(1, want // 25 + 1):
        url = f"https://zenodo.org/api/records?size=25&page={page}&{extra}"
        try:
            hits = get(url)["hits"]["hits"]
        except Exception as exc:
            print(f"  (page {page}: {exc})", file=sys.stderr); break
        if not hits: break
        records.extend(hits)
        time.sleep(0.3)

    tally = collections.Counter()
    shape = collections.Counter()
    for rec in records:
        files = rec.get("files") or []
        if not files:
            shape["no file list (restricted/embargoed)"] += 1
            continue
        exts = [posixpath.splitext(f.get("key", ""))[1].lower() or "(none)" for f in files]
        for e in exts:
            tally[e] += 1
        zips = sum(1 for e in exts if e == ".zip")
        if zips == len(exts):
            shape["every file a .zip" + (" (single)" if len(exts) == 1 else "")] += 1
        elif zips:
            shape["mixed: some .zip"] += 1
        else:
            shape["no .zip at all"] += 1

    print(f"\n=== {label}: {len(records)} records, {sum(tally.values())} files")
    for k, n in shape.most_common():
        print(f"  {k:38} {n:5}  {100*n/max(len(records),1):5.1f}%")
    tot = sum(tally.values()) or 1
    print(f"  -- top extensions --")
    for ext, n in tally.most_common(12):
        print(f"  {ext:38} {n:5}  {100*n/tot:5.1f}%")
    return tally, shape

survey("all recent datasets", "resource_type=dataset&sort=newest")
survey("bio/omics datasets", "resource_type=dataset&q=RNA%20OR%20genomics%20OR%20sequencing%20OR%20microscopy&sort=bestmatch")
