"""Fetch the Haifa-area price lists of Rami Levy, Osher Ad and Yochananof.

Israeli law (the food price-transparency regulations) makes every large chain
publish its full price list per branch. These three publish through the same
portal, url.publishedprices.co.il, where each chain's public username logs in
with an empty password.

Writes public/prices/<chain>-<store>.json for every branch in BRANCHES, plus
public/prices/index.json describing them. Each product sits on its own line
and lines are sorted by code, so a daily commit only records what changed.

A chain that fails keeps yesterday's files; the script still writes the rest
and exits non-zero at the end so the GitHub run is marked failed (and GitHub
emails about it).

Run:  python scripts/fetch_prices.py
"""

import datetime
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import requests

PORTAL = "https://url.publishedprices.co.il"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "prices")

# chain id -> (portal username, Hebrew name, default branch, branches to keep)
# Branches are the Haifa area; the first is what the app uses until changed.
BRANCHES = {
    "ramilevi": ("RamiLevi", "רמי לוי", ["062", "057", "019", "015", "727"]),
    "osherad": ("osherad", "אושר עד", ["030", "005", "029"]),
    "yohananof": ("yohananof", "יוחננוף", ["013"]),
}

UNKNOWN_UNITS = {"", "לא ידוע", "unknown"}


def login(username):
    s = requests.Session()
    s.headers["User-Agent"] = "market-run-prices/1.0"
    page = s.get(PORTAL + "/login", timeout=60).text
    token = re.search(r'name="csrftoken" content="([^"]+)"', page).group(1)
    r = s.post(PORTAL + "/login/user", timeout=60, data={
        "r": "", "username": username, "password": "",
        "Submit": "Sign in", "csrftoken": token,
    })
    r.raise_for_status()
    token = re.search(r'name="csrftoken" content="([^"]+)"', r.text)
    if not token:
        raise RuntimeError("login failed for " + username)
    return s, token.group(1)


def list_files(s, token):
    # The listing comes back empty unless the full DataTables parameters
    # are posted, not just the paging ones.
    data = {
        "sEcho": 1, "iColumns": 5, "sColumns": ",,,,",
        "iDisplayStart": 0, "iDisplayLength": 100000,
        "sSearch": "", "bRegex": "false", "iSortingCols": 0,
        "cd": "/", "csrftoken": token,
    }
    for i, prop in enumerate(["fname", "typeLabel", "size", "ftime", ""]):
        data["mDataProp_%d" % i] = prop
    r = s.post(PORTAL + "/file/json/dir", data=data, timeout=120,
               headers={"X-Requested-With": "XMLHttpRequest"})
    r.raise_for_status()
    return [row["fname"] for row in r.json().get("aaData", [])]


def download(s, name):
    r = s.get(PORTAL + "/file/d/" + name, timeout=300)
    r.raise_for_status()
    raw = r.content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def parse_xml(raw):
    """Root element with every tag lower-cased — the chains disagree on case."""
    root = ET.fromstring(raw)
    for el in root.iter():
        el.tag = el.tag.lower()
    return root


def text(el, tag):
    v = el.findtext(tag)
    return v.strip() if v else ""


def num(s):
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def store_names(s, files):
    stores = sorted(f for f in files if f.lower().startswith("stores"))
    if not stores:
        return {}
    root = parse_xml(download(s, stores[-1]))
    return {text(st, "storeid").zfill(3): text(st, "storename") for st in root.iter("store")}


def latest_full(files, store):
    """Newest full price file for a branch: PriceFull<chain>-<sub>-<store>-<stamp>."""
    pat = re.compile(r"^pricefull\d+-\d+-%s-(\d{8,12})" % store, re.I)
    hits = [(m.group(1), f) for f in files for m in [pat.match(f)] if m]
    return max(hits)[1] if hits else None


def size_label(qty, unit_qty, uom):
    unit = unit_qty if unit_qty not in UNKNOWN_UNITS else re.sub(r"^[\d.\s]+", "", uom)
    unit = unit.strip()
    if not qty or not unit or unit in UNKNOWN_UNITS:
        return ""
    if qty == 1 and unit.startswith("יחיד"):   # "1 יחידות" says nothing
        return ""
    return "%g %s" % (qty, unit)


def items_of(root):
    """[[code, name, price, perUnit, perUnitOf, size, weighted], ...] by code."""
    seen = {}
    for it in root.iter("item"):
        code = text(it, "itemcode")
        price = num(text(it, "itemprice"))
        name = re.sub(r"\s+", " ", text(it, "itemname"))
        if not code or not name or not price:
            continue
        weighted = 1 if text(it, "bisweighted") == "1" else 0
        uom = re.sub(r"\s+", " ", text(it, "unitofmeasure"))
        size = "" if weighted else size_label(num(text(it, "quantity")), text(it, "unitqty"), uom)
        seen[code] = [code, name, price, num(text(it, "unitofmeasureprice")) or price,
                      re.sub(r"^1(?=\D)", "", uom), size, weighted]
    return [seen[k] for k in sorted(seen)]


def write_branch(path, head, items):
    lines = [json.dumps(i, ensure_ascii=False, separators=(",", ":")) for i in items]
    body = json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body + ',"items":[\n' + ",\n".join(lines) + "\n]}\n")


def main():
    os.makedirs(OUT, exist_ok=True)
    index_path = os.path.join(OUT, "index.json")
    try:
        with open(index_path, encoding="utf-8") as f:
            old = {c["id"]: c for c in json.load(f).get("chains", [])}
    except (OSError, ValueError):
        old = {}

    chains, failed = [], []
    for cid, (user, cname, wanted) in BRANCHES.items():
        try:
            s, token = login(user)
            files = list_files(s, token)
            names = store_names(s, files)
            branches = []
            for store in wanted:
                fname = latest_full(files, store)
                if not fname:
                    raise RuntimeError("no PriceFull for %s %s" % (cid, store))
                items = items_of(parse_xml(download(s, fname)))
                if len(items) < 500:
                    raise RuntimeError("only %d items in %s" % (len(items), fname))
                stamp = re.search(r"-(\d{8})-?(\d{4})", fname)
                when = "%s-%s-%sT%s:%s" % (stamp.group(1)[:4], stamp.group(1)[4:6],
                                           stamp.group(1)[6:], stamp.group(2)[:2],
                                           stamp.group(2)[2:]) if stamp else ""
                path = "%s-%s.json" % (cid, store)
                head = {"chain": cid, "store": store, "name": names.get(store, store),
                        "priced": when}
                write_branch(os.path.join(OUT, path), head, items)
                branches.append({"store": store, "name": names.get(store, store),
                                 "file": path, "items": len(items), "priced": when})
                print("%-10s %s %-24s %6d items  %s" % (cid, store, names.get(store, ""),
                                                         len(items), fname))
            chains.append({"id": cid, "name": cname, "default": wanted[0], "branches": branches})
        except Exception as e:  # keep yesterday's data for this chain
            print("FAILED %s: %s" % (cid, e), file=sys.stderr)
            failed.append(cid)
            if cid in old:
                chains.append(old[cid])

    index = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "chains": chains,
    }
    with open(index_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
        f.write("\n")

    if failed:
        sys.exit("failed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
