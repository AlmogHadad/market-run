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
    """[[code, name, price, perUnit, perUnitOf, size, weighted], ...] by code.
    Products on sale get an 8th element later — see deals_of."""
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


# ------------------------------------------------------------------ deals
#
# Each branch file lists its deals once, one per line after the products:
#   [description, minQty, pricePerUnit, maxQty, counts, "YYYY-MM-DD"]
# and a product on sale carries an 8th element: the indexes of its deals.
# pricePerUnit is per item, or per kg for produce. counts=1 means the app may
# use it in totals whenever the list's quantity reaches minQty; 0 means show
# it as a tag only, because what you'd pay can't be told from the list
# (spend-over-₪75 deals, 1+1 gifts, prices that fail a sanity check).
# Coupons, club-only deals and packaging adjustments are left out.

NOT_SALES = re.compile(r"קיזוז|שקיות|פיקדון|פקדון")
# "…מעל 75" — only with a minimum spend, even when the field is left empty.
MIN_SPEND = re.compile(r"מעל\s*\d")


def latest_promo(files, store):
    pat = re.compile(r"^promofull\d+-\d+-%s-(\d{8,12})" % store, re.I)
    hits = [(m.group(1), f) for f in files for m in [pat.match(f)] if m]
    return max(hits)[1] if hits else None


def israel_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=2)


def when(s):
    try:
        return datetime.datetime.fromisoformat(s.strip()[:19])
    except ValueError:
        return None


def deals_of(root, regular):
    """code -> deals for products we price, best (cheapest) first."""
    now = israel_now()
    out = {}
    for pr in root.iter("promotion"):
        desc = re.sub(r"\s+", " ", text(pr, "promotiondescription")).strip(" -")
        if not desc or NOT_SALES.search(desc):
            continue
        if text(pr, "additionaliscoupon") == "1":
            continue
        clubs = [c.text or "" for c in pr.iter("clubid")]
        if any(not c.strip().startswith("0") for c in clubs):
            continue
        start, end = when(text(pr, "promotionstartdatetime")), when(text(pr, "promotionenddatetime"))
        if (start and start > now) or (end and end < now):
            continue
        min_buy = num(text(pr, "minpurchaseamount")) or 0

        for it in pr.iter("promotionitem"):
            code = text(it, "itemcode")
            reg = regular.get(code)
            if not reg:
                continue
            # The chains put these on the item, the promotion, or both.
            pick = lambda tag: text(it, tag) or text(pr, tag)
            reward = pick("rewardtype")
            price = num(pick("discountedprice"))
            mn = num(pick("minqty")) or 1
            mx = num(pick("maxqty")) or 0
            if price is None:
                continue
            per = round(price / mn, 2) if mn >= 1 else price
            counts = 1 if (reward in ("1", "3", "10") and not min_buy
                           and not MIN_SPEND.search(desc)) else 0
            # A "deal" that isn't cheaper, or is implausibly cheap, is
            # something we've misread — show it, but don't count it.
            if not (reg * 0.15 <= per < reg * 0.995):
                counts = 0
                if per >= reg:
                    continue
            d = [desc, mn, per, mx, counts, end.strftime("%Y-%m-%d") if end else ""]
            lst = out.setdefault(code, [])
            if all(x[0] != desc for x in lst):
                lst.append(d)
    for code in out:
        out[code] = sorted(out[code], key=lambda d: (-d[4], d[2]))[:3]
    return out


def write_branch(path, head, items, deals=()):
    dump = lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":"))
    body = dump(head)[:-1]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body + ',"items":[\n' + ",\n".join(dump(i) for i in items) +
                '\n],"deals":[\n' + ",\n".join(dump(d) for d in deals) + "\n]}\n")


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
                # Deals are a bonus: a missing or broken file leaves them out.
                ndeals, table = 0, []
                pname = latest_promo(files, store)
                if pname:
                    try:
                        deals = deals_of(parse_xml(download(s, pname)),
                                         {i[0]: i[2] for i in items})
                        # Sorted, so the indexes (and the diff) stay stable.
                        table = sorted({tuple(d) for ds in deals.values() for d in ds},
                                       key=lambda d: (d[0], d[1], d[2], d[5]))
                        at = {d: n for n, d in enumerate(table)}
                        for i in items:
                            if i[0] in deals:
                                i.append([at[tuple(d)] for d in deals[i[0]]])
                                ndeals += 1
                    except Exception as e:
                        print("  deals skipped for %s %s: %s" % (cid, store, e), file=sys.stderr)
                stamp = re.search(r"-(\d{8})-?(\d{4})", fname)
                when = "%s-%s-%sT%s:%s" % (stamp.group(1)[:4], stamp.group(1)[4:6],
                                           stamp.group(1)[6:], stamp.group(2)[:2],
                                           stamp.group(2)[2:]) if stamp else ""
                path = "%s-%s.json" % (cid, store)
                head = {"chain": cid, "store": store, "name": names.get(store, store),
                        "priced": when}
                write_branch(os.path.join(OUT, path), head, items, [list(d) for d in table])
                branches.append({"store": store, "name": names.get(store, store),
                                 "file": path, "items": len(items), "priced": when})
                print("%-10s %s %-24s %6d items %5d on sale  %s" % (
                    cid, store, names.get(store, ""), len(items), ndeals, fname))
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
