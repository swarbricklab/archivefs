"""Can a Zenodo file key contain a '/'? i.e. can a record have real directories?"""
import json, urllib.request, time, collections

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "archivefs-survey"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

keys = 0
with_slash = []
recs = 0
for extra in ("resource_type=dataset&sort=newest",
              "resource_type=dataset&q=spaceranger%20OR%20cellranger%20OR%20visium&sort=bestmatch",
              "sort=mostviewed"):
    for page in range(1, 13):
        try:
            hits = get(f"https://zenodo.org/api/records?size=25&page={page}&{extra}")["hits"]["hits"]
        except Exception as e:
            print("  page err", e); break
        if not hits: break
        for r in hits:
            recs += 1
            for f in (r.get("files") or []):
                k = f.get("key", "")
                keys += 1
                if "/" in k:
                    with_slash.append((r["id"], k))
        time.sleep(0.25)

print(f"records: {recs}   file keys inspected: {keys}")
print(f"keys containing '/': {len(with_slash)}")
for rid, k in with_slash[:10]:
    print(f"   {rid}  {k}")
