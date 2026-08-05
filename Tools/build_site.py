#!/usr/bin/env python3
"""Build the Form NT static API from current government sources.

One command. Downloads what it needs, matches the rule library, and writes a
tree of JSON files that can be served by any static host — no server, no
database, no runtime.

    python3 build_site.py            # incremental, uses cached downloads
    python3 build_site.py --fresh    # re-download everything

Output (public/v1/):
    feed/sec/page-1.json ...     cursor-paginated feed pages
    feed/oge/page-1.json ...
    signals/<id>.json            one per record
    companies/<TICKER>.json      one per issuer
    search-index.json            every record, for on-device filtering
    meta.json                    when this was built and from what

Design notes that are not negotiable:

- Rules are matched HERE, not on the device. The client stays a reader, which
  is what keeps the compliance surface in one place.
- No base rate is computed or invented. Every record ships
  baseRateStatus "pending".
- Congressional filers are named in the source PDFs and are DISCARDED. Only the
  owner code survives, rendered as a role.
- Value bands are copied verbatim. Never averaged, summed, or converted.
- Schedule 13D subjects are resolved from each filing's own index page, because
  the quarterly index lists a 13D under both the filer and the subject and
  collapses amendments under the same form type.
"""
import argparse, csv, io, json, os, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
OUT = os.path.abspath(os.path.join(HERE, "..", "public", "v1"))

UA = os.environ.get("FORMNT_UA", "Form NT records reader nate@templebit.com")
HEADERS = {"User-Agent": UA}

# SEC fair access: 10 requests/second across the whole system. Stay well under.
SEC_DELAY = 0.25

csv.field_size_limit(10_000_000)


# ------------------------------------------------------------------ fetching

def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read()


def cached(name, url, fresh=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and not fresh:
        return open(path, "rb").read()
    data = fetch(url)
    with open(path, "wb") as f:
        f.write(data)
    return data


def latest_insider_quarter(fresh=False):
    """The insider data set publishes a quarter or so behind. Walk back."""
    now = datetime.now(timezone.utc)
    for back in range(0, 5):
        d = now - timedelta(days=95 * back)
        q = (d.month - 1) // 3 + 1
        name = f"{d.year}q{q}_form345.zip"
        url = ("https://www.sec.gov/files/structureddata/data/"
               f"insider-transactions-data-sets/{name}")
        try:
            data = cached(name, url, fresh)
            if data[:2] == b"PK":
                return name, data, f"{d.year}Q{q}"
        except Exception:
            continue
    raise SystemExit("no insider data set found in the last five quarters")


def current_quarters():
    now = datetime.now(timezone.utc)
    q = (now.month - 1) // 3 + 1
    out = [(now.year, q)]
    if q > 1:
        out.append((now.year, q - 1))
    else:
        out.append((now.year - 1, 4))
    return out


# ------------------------------------------------------------------ shaping

def iso(d):
    return d.strftime("%Y-%m-%dT00:00:00.000Z")


def parse_date(s):
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def acc_url(cik, acc):
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc.replace('-', '')}/{acc}-index.htm")


def signal(**kw):
    kw.setdefault("historicalBase", None)
    kw.setdefault("baseRateStatus", "pending")
    return kw


BAND_ORDER = [
    "$1,001 - $15,000", "$15,001 - $50,000", "$50,001 - $100,000",
    "$100,001 - $250,000", "$250,001 - $500,000", "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000", "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000", "Over $50,000,000",
]


def band_score(b):
    try:
        return min(70, 30 + 5 * BAND_ORDER.index(b))
    except ValueError:
        return 40


RULE_NAMES = {
    "cluster_insider_buy": "insider purchase cluster",
    "restatement": "restatement",
    "auditor_exit": "auditor change",
    "late_filing": "late filing",
    "activist_stake": "activist stake",
}


# ------------------------------------------------------------------ sources

def load_ticker_map(fresh):
    raw = cached("company_tickers.json",
                 "https://www.sec.gov/files/company_tickers.json", fresh)
    data = json.loads(raw)
    return {str(v["cik_str"]): (v["ticker"], v["title"]) for v in data.values()}


def insider_clusters(fresh, limit):
    """>=3 distinct insiders, code-P open-market purchases, inside 14 days."""
    import zipfile
    name, data, label = latest_insider_quarter(fresh)
    z = zipfile.ZipFile(io.BytesIO(data))

    def rows(member):
        with z.open(member) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)

    subs = {}
    for r in rows("SUBMISSION.tsv"):
        if r["DOCUMENT_TYPE"] != "4":
            continue
        t = (r["ISSUERTRADINGSYMBOL"] or "").strip()
        if not t or t in ("NONE", "N/A"):
            continue
        subs[r["ACCESSION_NUMBER"]] = (r["ISSUERCIK"], r["ISSUERNAME"], t)

    owners = defaultdict(list)
    for r in rows("REPORTINGOWNER.tsv"):
        owners[r["ACCESSION_NUMBER"]].append(
            (r["RPTOWNERCIK"], (r["RPTOWNER_TITLE"] or r["RPTOWNER_RELATIONSHIP"] or "").strip()))

    buys = []
    for r in rows("NONDERIV_TRANS.tsv"):
        if r["TRANS_CODE"] != "P" or r["TRANS_ACQUIRED_DISP_CD"] != "A":
            continue
        s = subs.get(r["ACCESSION_NUMBER"])
        d = parse_date(r["TRANS_DATE"] or "")
        if not s or not d:
            continue
        try:
            sh, px = float(r["TRANS_SHARES"] or 0), float(r["TRANS_PRICEPERSHARE"] or 0)
        except ValueError:
            continue
        if sh <= 0 or px <= 0:
            continue
        for ocik, title in owners.get(r["ACCESSION_NUMBER"], []):
            buys.append({"acc": r["ACCESSION_NUMBER"], "cik": s[0], "company": s[1],
                         "ticker": s[2], "date": d, "value": sh * px,
                         "owner": ocik, "title": title})

    by_ticker = defaultdict(list)
    for b in buys:
        by_ticker[b["ticker"]].append(b)

    out = []
    for ticker, items in by_ticker.items():
        items.sort(key=lambda x: x["date"])
        for i, anchor in enumerate(items):
            window = [x for x in items[i:] if (x["date"] - anchor["date"]).days <= 14]
            insiders = {x["owner"] for x in window}
            if len(insiders) < 3:
                continue
            total = sum(x["value"] for x in window)
            accs = sorted({x["acc"] for x in window})
            roles = sorted({x["title"] for x in window if x["title"]})[:4]
            end = max(x["date"] for x in window)
            out.append(signal(
                id=f"sig_cluster_{ticker.lower()}_{anchor['date']:%Y%m%d}",
                source="sec", filingId=accs[0], ticker=ticker,
                companyName=window[0]["company"], ruleId="cluster_insider_buy",
                notabilityScore=min(95, 50 + 10 * (len(insiders) - 3) + (10 if total >= 1_000_000 else 0)),
                triggeredCriteria=[
                    f"{len(insiders)} separate insiders reported open-market purchases (Form 4 transaction code P)",
                    f"All reported transactions fall within a 14-day window ({anchor['date']:%Y-%m-%d} to {end:%Y-%m-%d})",
                    f"{len(window)} reported transactions across {len(accs)} filings",
                ],
                contextNotes=[
                    f"Combined disclosed transaction value across the window: ${total:,.0f}.",
                    (f"Reporting roles: {', '.join(roles)}." if roles
                     else "Reporting roles were not itemised in the source filings."),
                    "Values are taken directly from the Form 4 filings as reported.",
                ],
                rawUrl=acc_url(window[0]["cik"], accs[0]), createdAt=iso(end)))
            break
    out.sort(key=lambda s: s["createdAt"], reverse=True)
    print(f"  insider clusters ({label}): {len(out)}", file=sys.stderr)
    return out[:limit]


def efts(phrase, forms, days, limit):
    """EDGAR full-text search. Live, no key."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    seen = {}
    for page in range(0, 6):
        q = urllib.parse.urlencode({
            "q": f'"{phrase}"', "forms": forms, "from": page * 10,
            "startdt": start.strftime("%Y-%m-%d"), "enddt": end.strftime("%Y-%m-%d")})
        try:
            data = json.loads(fetch(f"https://efts.sec.gov/LATEST/search-index?{q}", 40))
        except Exception as e:
            print(f"  efts error: {e}", file=sys.stderr)
            break
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            s = h["_source"]
            if s.get("form") != forms or s["adsh"] in seen:
                continue
            m = re.match(r"(.*?)\s*\((.*?)\)\s*\(CIK (\d+)\)", s["display_names"][0])
            if not m:
                continue
            seen[s["adsh"]] = {"company": m.group(1).strip(), "ticker": m.group(2),
                               "cik": m.group(3).lstrip("0"), "acc": s["adsh"],
                               "date": s["file_date"]}
        time.sleep(SEC_DELAY)
        if len(seen) >= limit:
            break
    return list(seen.values())[:limit]


def form_index(fresh):
    """Current and previous quarter EDGAR form indexes."""
    rows = []
    for year, q in current_quarters():
        name = f"form_{year}Q{q}.idx"
        url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
        try:
            raw = cached(name, url, fresh).decode("utf-8", "replace")
        except Exception:
            continue
        for line in raw.splitlines():
            if not re.search(r"\s\d{4}-\d{2}-\d{2}\s", line):
                continue
            p = line.rstrip().rsplit(None, 3)
            if len(p) != 4:
                continue
            head, cik, date, fn = p
            rows.append({"form": head[:12].strip(), "company": head[12:].strip(),
                         "cik": cik.lstrip("0"), "date": date, "file": fn})
    return rows


PARTY = re.compile(r"([A-Za-z0-9][^|<>]{2,80}?)\s*\((Filed by|Subject)\)[\s\S]{0,200}?CIK[\s\S]{0,60}?(\d{10})")
FORM_TYPE = re.compile(r"Type:\s*\|?\s*(SCHEDULE 13D(?:/A)?|SC 13D(?:/A)?)")


def resolve_13d(rows, cik2t, limit):
    """Only initial 13Ds, keyed to the SUBJECT issuer. See module docstring."""
    out, seen, checked = [], set(), 0
    for r in rows:
        if len(out) >= limit or checked >= limit * 8:
            break
        acc = r["file"].split("/")[-1].replace(".txt", "")
        if acc in seen:
            continue
        seen.add(acc)
        checked += 1
        try:
            html = fetch(acc_url(r["cik"], acc), 40).decode("utf-8", "replace")
        except Exception:
            continue
        time.sleep(SEC_DELAY)
        flat = re.sub(r"\|+", "|", re.sub(r"<[^>]+>", "|", html))
        types = set(FORM_TYPE.findall(flat))
        if not types or any("/A" in t for t in types):
            continue
        subj = next(((n.strip(), c.lstrip("0")) for n, role, c in PARTY.findall(flat)
                     if role == "Subject"), None)
        if not subj:
            continue
        hit = cik2t.get(subj[1])
        if not hit:
            continue
        d = parse_date(r["date"])
        out.append(signal(
            id=f"sig_stake_{hit[0].lower()}_{acc.replace('-', '')[-6:]}",
            source="sec", filingId=acc, ticker=hit[0], companyName=hit[1],
            ruleId="activist_stake", notabilityScore=65,
            triggeredCriteria=[f"Initial Schedule 13D filed {r['date']}",
                               "Filing is an original statement, not an amendment"],
            contextNotes=[
                "A Schedule 13D is required of a beneficial owner of more than 5% of a class "
                "of registered equity securities who does not qualify for the shorter 13G form.",
                "Item 4 of the filing states the filer's stated purpose.",
            ],
            rawUrl=acc_url(subj[1], acc), createdAt=iso(d)))
    return out


TXN = re.compile(
    r"(?:(?P<owner>SP|DC|JT)\s+)?(?P<asset>[^\n(]{2,120}?)\s*\((?P<ticker>[A-Z]{1,5})\)\s*"
    r"\[(?P<klass>[A-Z]{2})\]\s*(?P<ttype>[PSE])\s*(?P<tdate>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<ndate>\d{2}/\d{2}/\d{4})\s*(?P<band>\$[\d,]+\s*-\s*\$?[\d,]+)", re.S)

OWNER_ROLE = {"SP": "spouse", "DC": "dependent child", "JT": "joint filing", "": "self"}
TTYPE = {"P": "purchase", "S": "sale", "E": "exchange"}


def house_disclosures(fresh, limit, ticker_names):
    """Real congressional PTRs. Filer names are discarded — see docstring."""
    import zipfile
    year = datetime.now(timezone.utc).year
    try:
        raw = cached(f"{year}FD.zip",
                     f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip",
                     fresh)
        z = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        print(f"  house archive unavailable: {e}", file=sys.stderr)
        return []

    txt = [n for n in z.namelist() if n.endswith(".txt")][0]
    with z.open(txt) as fh:
        filings = [r for r in csv.DictReader(
            io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t")
            if r["FilingType"] == "P"]
    filings.sort(key=lambda r: r["DocID"], reverse=True)

    try:
        import pypdf
    except ImportError:
        print("  pypdf not installed; skipping congressional disclosures", file=sys.stderr)
        return []

    out, scanned, seen = [], 0, set()
    for r in filings:
        if len(out) >= limit:
            break
        url = (f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/"
               f"{r['Year']}/{r['DocID']}.pdf")
        try:
            text = "\n".join(p.extract_text() or ""
                             for p in pypdf.PdfReader(io.BytesIO(fetch(url, 60))).pages)
        except Exception:
            continue
        time.sleep(0.2)
        if len(text) < 200:
            scanned += 1
            continue
        for m in TXN.finditer(text):
            if m.group("klass") != "ST" or len(out) >= limit:
                continue
            role = OWNER_ROLE[m.group("owner") or ""]
            band = re.sub(r"\s+", " ", m.group("band")).strip()
            band = re.sub(r"-\s*(?!\$)([\d,]+)$", r"- $\1", band)
            key = (m.group("ticker"), role, m.group("tdate"), band)
            if key in seen:
                continue
            seen.add(key)
            asset = re.sub(r"^(SP|DC|JT)\s+", "", re.sub(r"\s+", " ", m.group("asset")).strip(" -,"))
            d = parse_date(m.group("tdate"))
            out.append(signal(
                id=f"sig_oge_{r['DocID']}_{len(out):02d}", source="oge",
                filingId=r["DocID"], ticker=m.group("ticker"),
                companyName=ticker_names.get(m.group("ticker"), asset),
                ruleId="oge_disclosure", notabilityScore=band_score(band),
                actorLabel=f"U.S. Representative ({role})", valueBand=band,
                assetDescription=asset, transactionType=TTYPE[m.group("ttype")],
                triggeredCriteria=[
                    f"Periodic Transaction Report filed {r['FilingDate']}",
                    f"Reported {TTYPE[m.group('ttype')]} dated {m.group('tdate')}",
                    f"Reported value band: {band}",
                ],
                contextNotes=[
                    "Federal financial disclosures report value in bands. The band is shown "
                    "exactly as filed and is not converted to a point estimate.",
                    "The filer role is reported as disclosed. A filing covering a spouse or "
                    "dependent child is made by the member but is not attributed to the "
                    "member personally.",
                    f"Notification date {m.group('ndate')}. Reports are filed after the fact, "
                    "so the filing date trails the transaction date.",
                ],
                rawUrl=url, createdAt=iso(d)))
    if scanned:
        print(f"  congressional filings skipped (no text layer): {scanned}", file=sys.stderr)
    return out


# ------------------------------------------------------------------- output

def write(path, obj):
    full = os.path.join(OUT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def paginate(items, source, per_page=20):
    pages = [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
    for n, page in enumerate(pages, start=1):
        nxt = f"p{n + 1}" if n < len(pages) else None
        write(f"feed/{source}/page-{n}.json", {"items": page, "nextCursor": nxt})
    return len(pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="re-download sources")
    ap.add_argument("--days", type=int, default=120, help="lookback for full-text search")
    ap.add_argument("--limit", type=int, default=40, help="max records per rule")
    ap.add_argument("--min-records", type=int, default=60,
                    help="refuse to publish below this many records")
    ap.add_argument("--max-drop", type=float, default=0.40,
                    help="refuse to publish if the record count falls by more than this")
    ap.add_argument("--allow-drop", action="store_true",
                    help="publish anyway after a confirmed legitimate drop")
    args = ap.parse_args()
    if args.allow_drop:
        args.max_drop = 1.0

    print("building Form NT static API", file=sys.stderr)
    cik2t = load_ticker_map(args.fresh)
    ticker_names = {t: name for t, name in cik2t.values()}

    records = []
    records += insider_clusters(args.fresh, args.limit)

    for rule, phrase, score, crit, notes in [
        ("restatement", "Non-Reliance on Previously Issued Financial Statements", 80,
         ["Form 8-K filed reporting Item 4.02",
          "Non-reliance on previously issued financial statements"],
         ["Item 4.02 indicates the issuer has advised that previously issued financial "
          "statements or a related audit report can no longer be relied upon.",
          "The filing text is the authoritative statement of scope and periods affected."]),
        ("auditor_exit", "Changes in Registrant's Certifying Accountant", 70,
         ["Form 8-K filed reporting Item 4.01",
          "Change in the registrant's certifying accountant"],
         ["Item 4.01 covers both resignation and dismissal of the certifying accountant. "
          "The filing text states which occurred.",
          "Any reported disagreements on accounting matters are disclosed in the filing."]),
    ]:
        hits = efts(phrase, "8-K", args.days, args.limit)
        print(f"  {rule}: {len(hits)}", file=sys.stderr)
        for f in hits:
            records.append(signal(
                id=f"sig_{rule[:8]}_{f['ticker'].lower()}_{f['acc'].replace('-', '')[-6:]}",
                source="sec", filingId=f["acc"], ticker=f["ticker"],
                companyName=f["company"], ruleId=rule, notabilityScore=score,
                triggeredCriteria=crit, contextNotes=notes,
                rawUrl=acc_url(f["cik"], f["acc"]), createdAt=iso(parse_date(f["date"]))))

    idx = form_index(args.fresh)
    nt = [r for r in idx if r["form"] in ("NT 10-K", "NT 10-Q")]
    nt.sort(key=lambda r: r["date"], reverse=True)
    seen_nt = set()
    late = []
    for r in nt:
        if r["company"] in seen_nt or len(late) >= args.limit:
            continue
        seen_nt.add(r["company"])
        acc = r["file"].split("/")[-1].replace(".txt", "")
        late.append(signal(
            id=f"sig_late_{acc.replace('-', '')[-8:]}", source="sec", filingId=acc,
            ticker=None, companyName=r["company"], ruleId="late_filing",
            notabilityScore=55 if r["form"] == "NT 10-K" else 45,
            triggeredCriteria=[f"Form {r['form']} filed {r['date']}",
                               "Periodic report was not filed by its prescribed due date"],
            contextNotes=[f"A {r['form']} states the issuer could not file the report on "
                          "time and gives the reason. The reason is set out in Part III of "
                          "the notification."],
            rawUrl=acc_url(r["cik"], acc), createdAt=iso(parse_date(r["date"]))))
    print(f"  late filings: {len(late)}", file=sys.stderr)
    records += late

    sc = [r for r in idx if r["form"] == "SCHEDULE 13D"]
    sc.sort(key=lambda r: r["date"], reverse=True)
    stakes = resolve_13d(sc, cik2t, min(args.limit, 12))
    print(f"  activist stakes (subject-resolved): {len(stakes)}", file=sys.stderr)
    records += stakes

    oge = house_disclosures(args.fresh, args.limit, ticker_names)
    print(f"  congressional disclosures: {len(oge)}", file=sys.stderr)

    # overlap: same ticker, both sources, inside 90 days. Both halves are real.
    sec_by_ticker = defaultdict(list)
    for r in records:
        if r.get("ticker"):
            sec_by_ticker[r["ticker"]].append(r)
    overlaps, used = [], set()
    for o in oge:
        for s in sec_by_ticker.get(o["ticker"], []):
            gap = abs((parse_date(o["createdAt"][:10]) - parse_date(s["createdAt"][:10])).days)
            if gap > 90 or o["ticker"] in used:
                continue
            used.add(o["ticker"])
            overlaps.append(signal(
                id=f"sig_overlap_{o['ticker'].lower()}", source="sec",
                filingId=s["filingId"], ticker=o["ticker"], companyName=s["companyName"],
                ruleId="overlap", notabilityScore=min(90, 60 + max(0, (90 - gap) // 10)),
                triggeredCriteria=[
                    f"SEC signal on this ticker ({RULE_NAMES.get(s['ruleId'], s['ruleId'])}) dated {s['createdAt'][:10]}",
                    f"Congressional disclosure reporting a {o['transactionType']} dated {o['createdAt'][:10]}",
                    f"Both fall within a 90-day window ({gap} days apart)",
                ],
                contextNotes=[
                    f"The disclosure was filed by a U.S. Representative ({o['actorLabel'].split('(')[1].rstrip(')')}) "
                    f"and reports a value band of {o['valueBand']}. The band is shown as filed.",
                    "The two filings are independent public records. Their proximity in time is "
                    "the only relationship asserted here, and no connection between them is implied.",
                    "Both records appear separately in this app, each with its own source link.",
                ],
                rawUrl=s["rawUrl"], createdAt=s["createdAt"]))
            break
    print(f"  cross-source overlaps: {len(overlaps)}", file=sys.stderr)
    records += overlaps

    # Interleave rules so one rule cannot dominate the top of the feed.
    by_rule = defaultdict(list)
    for r in sorted(records, key=lambda r: r["createdAt"], reverse=True):
        by_rule[r["ruleId"]].append(r)
    mixed, queues = [], list(by_rule.values())
    while any(queues):
        for q in queues:
            if q:
                mixed.append(q.pop(0))

    oge.sort(key=lambda r: r["createdAt"], reverse=True)

    sec_pages = paginate(mixed, "sec")
    oge_pages = paginate(oge, "oge")

    everything = mixed + oge
    for r in everything:
        write(f"signals/{r['id']}.json", r)

    by_ticker = defaultdict(list)
    for r in everything:
        if r.get("ticker"):
            by_ticker[r["ticker"]].append(r)
    for ticker, rs in by_ticker.items():
        cik = "0"
        if "/data/" in rs[0]["rawUrl"]:
            cik = re.sub(r"\D", "", rs[0]["rawUrl"].split("/data/")[1].split("/")[0])
        write(f"companies/{ticker}.json", {
            "ticker": ticker, "name": rs[0]["companyName"], "cik": cik,
            "filings": [{"id": r["filingId"], "formType": form_label(r),
                         "filedAt": r["createdAt"], "rawUrl": r["rawUrl"]} for r in rs],
            "signalIds": [r["id"] for r in rs], "forecastId": None,
            "baseRateStatus": "pending"})

    # A partial build must not publish. If a source silently starts returning
    # nothing, the site would claim the world went quiet — which is a false
    # statement, not an empty one. Fail loudly and leave the last good build up.
    previous = None
    prev_path = os.path.join(OUT, "meta.json")
    if os.path.exists(prev_path):
        try:
            previous = json.load(open(prev_path))
        except Exception:
            previous = None

    if len(everything) < args.min_records:
        raise SystemExit(
            f"refusing to publish: {len(everything)} records, minimum is {args.min_records}")

    if previous and previous.get("records"):
        drop = 1 - (len(everything) / previous["records"])
        if drop > args.max_drop:
            raise SystemExit(
                f"refusing to publish: records fell {drop:.0%} "
                f"({previous['records']} -> {len(everything)}), limit is {args.max_drop:.0%}. "
                "Re-run with --allow-drop once you have confirmed the sources are correct.")

    missing = [r for r in ("restatement", "late_filing", "oge_disclosure")
               if r not in {x["ruleId"] for x in everything}]
    if missing:
        raise SystemExit(f"refusing to publish: no records for {', '.join(missing)} — "
                         "a core source is probably failing")

    write("search-index.json", {"items": everything, "nextCursor": None})
    write("meta.json", {
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "records": len(everything), "secRecords": len(mixed), "ogeRecords": len(oge),
        "rules": sorted({r["ruleId"] for r in everything}),
        "sources": ["SEC EDGAR full-text search", "SEC insider transactions data sets",
                    "SEC EDGAR quarterly form index",
                    "Clerk of the U.S. House of Representatives"]})

    print(f"\nwrote {len(everything)} records to {OUT}", file=sys.stderr)
    print(f"  sec feed pages {sec_pages}, ethics feed pages {oge_pages}, "
          f"companies {len(by_ticker)}", file=sys.stderr)


def form_label(r):
    rule = r["ruleId"]
    return {"cluster_insider_buy": "Form 4",
            "restatement": "Form 8-K (Item 4.02)",
            "auditor_exit": "Form 8-K (Item 4.01)",
            "activist_stake": "Schedule 13D",
            "oge_disclosure": "Periodic Transaction Report"}.get(
        rule, r["triggeredCriteria"][0])


if __name__ == "__main__":
    main()
