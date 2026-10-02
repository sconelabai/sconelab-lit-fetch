#!/usr/bin/env python3
"""SCoNeLab weekly literature fetcher (runs on GitHub Actions).

Queries Europe PMC (PubMed + bioRxiv/medRxiv/PsyArXiv/Research Square) for each
domain in config/scope.json plus the author watch list, scores candidates by
keyword/author overlap, attaches abstracts and legitimate open-access PDF links,
and checks watched preprints for published journal versions.

No judgment happens here: Claude screens and summarizes the output.

Outputs (committed back to the repo by the workflow):
  data/latest.json                 full candidate records + preprint updates
  data/latest.tsv                  compact view for screening (one line per paper)
  data/runs/<date>.json            archived copy of latest.json
  data/preprints_watch.tsv         preprints to check for publication (grows)

Usage: python fetch.py [--days N] [--end YYYY-MM-DD]
Standard library only.
"""
import relevance
import argparse, datetime as dt, json, os, re, sys, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UA = "SCoNeLab-literature-tracker/1.0 (mailto:dstanley@adelphi.edu)"
MAX_PER_QUERY = 300
MAX_OUT = 150

# ---------------------------------------------------------------- helpers
def get_json(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # 429/5xx/network: back off and retry
            wait = 5 * 2 ** i
            print(f"  retry {i+1} after {e} (sleep {wait}s)", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed: {url[:120]}")

def epmc(query, limit=MAX_PER_QUERY):
    """Yield core records for a Europe PMC query (cursor paging)."""
    cursor, n = "*", 0
    while n < limit:
        params = {"query": query, "format": "json", "resultType": "core",
                  "pageSize": min(100, limit - n), "cursorMark": cursor,
                  "sort": "P_PDATE_D desc"}
        d = get_json(EPMC + "?" + urllib.parse.urlencode(params))
        res = d.get("resultList", {}).get("result", [])
        for r in res:
            yield r
        n += len(res)
        nxt = d.get("nextCursorMark")
        if not res or not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.3)

def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()

def make_id(r):
    doi = (r.get("doi") or "").lower()
    if doi:
        return "doi:" + re.sub(r"[^a-z0-9_.~:@+-]", "_", doi.replace("/", "~"))
    if r.get("source") == "MED" and r.get("pmid"):
        return "pmid:" + r["pmid"]
    return f"{(r.get('source') or 'x').lower()}:{r.get('id')}"

STOP = {"of", "the", "and", "in", "for", "on", "to", "a", "an", "vs", "vs.", "versus", "with"}

def term(k):
    """Keyword -> Europe PMC clause: phrases of 1-2 words quoted, longer ones ANDed."""
    w = [x for x in k.split() if x.lower() not in STOP]
    if len(w) <= 2:
        k = " ".join(w)
        return f'TITLE_ABS:"{k}"'
    return "(" + " AND ".join(f"TITLE_ABS:{x}" for x in w) + ")"

NAME_FIX = {"Read Montague": "Montague P"}  # names whose first given name isn't the publishing initial

def initials_name(full):
    if full in NAME_FIX:
        return NAME_FIX[full]
    p = full.replace(".", "").split()
    return f"{p[-1]} {p[0][0]}" if len(p) > 1 else full

ANCHOR = ('(TITLE_ABS:social OR TITLE_ABS:trust OR TITLE_ABS:learning OR '
          'TITLE_ABS:"theory of mind" OR TITLE_ABS:mentalizing OR TITLE_ABS:decision OR '
          'TITLE_ABS:cooperation OR TITLE_ABS:bias)')

def pdf_link(r):
    for u in (r.get("fullTextUrlList") or {}).get("fullTextUrl", []):
        if u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F"):
            return u.get("url")
    if r.get("pmcid"):
        return f"https://europepmc.org/articles/{r['pmcid']}?pdf=render"
    m = re.match(r"10\.31234/osf\.io/([a-z0-9]+(_v\d+)?)", (r.get("doi") or "").lower())
    if m:
        return f"https://osf.io/{m.group(1)}/download"
    return ""

def record(r):
    authors = [a.get("fullName", "") for a in (r.get("authorList") or {}).get("author", [])]
    venue = ((r.get("journalInfo") or {}).get("journal") or {}).get("medlineAbbreviation") \
        or ((r.get("journalInfo") or {}).get("journal") or {}).get("title") \
        or (r.get("bookOrReportDetails") or {}).get("publisher") or r.get("source")
    types = [t for t in (r.get("pubTypeList") or {}).get("pubType", [])]
    abstract = re.sub(r"<[^>]+>", "", r.get("abstractText") or "")
    preprint = r.get("source") == "PPR" or "Preprint" in types or "preprint" in types
    pdf = pdf_link(r)
    return {
        "id": make_id(r), "title": re.sub(r"<[^>]+>", "", r.get("title") or "").rstrip("."),
        "authors": authors[:6] + (["et al."] if len(authors) > 6 else []),
        "year": int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
        "venue": venue, "doi": r.get("doi", ""), "pmid": r.get("pmid", ""),
        "pmcid": r.get("pmcid", ""), "epmc": f"{r.get('source')}:{r.get('id')}",
        "first_pub": r.get("firstPublicationDate", ""), "pub_types": types,
        "preprint": preprint, "pdf_url": pdf,
        "access": "oa" if (pdf or r.get("isOpenAccess") == "Y" or preprint) else "abstract",
        "abstract": abstract,
    }

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="look-back window (overlap is fine; Claude dedupes)")
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today()
    start = end - dt.timedelta(days=a.days)
    win = f"FIRST_PDATE:[{start} TO {end}]"
    scope = json.load(open(os.path.join(ROOT, "config", "scope.json")))
    domains = scope["domains"] + scope.get("peripheral", [])
    watch = [initials_name(n) for n in scope.get("watchlist", [])]
    watch_sur = {w.split()[0].lower() for w in watch}

    cands, hits = {}, {}
    T0 = time.time()
    def add(r, tag):
        rec = record(r)
        if not rec["title"]:
            return
        k = rec["id"]
        cands.setdefault(k, rec)
        hits.setdefault(k, set()).add(tag)

    for d in domains:
        q = "(" + " OR ".join(term(k) for k in d.get("keywords", [])) + f") AND {win}"
        if not d.get("core", d["id"].startswith("D")):
            q += f" AND {ANCHOR}"
        n0 = len(cands)
        for r in epmc(q):
            add(r, d["id"])
        print(f"{d['id']}: +{len(cands)-n0} (total {len(cands)})")
        time.sleep(1)

    # watch-list authors, in chunks (surname collisions handled by the anchor)
    for i in range(0, len(watch), 12):
        chunk = watch[i:i + 12]
        q = "(" + " OR ".join(f'AUTH:"{w}"' for w in chunk) + f") AND {ANCHOR} AND {win}"
        for r in epmc(q, 200):
            add(r, "WATCH")
        time.sleep(1)
    print(f"after watchlist: {len(cands)}")

    # ---- extra sources (merged by id, then normalized title) ----
    import sources
    tix = {norm(c["title"]): k for k, c in cands.items()}
    def merge(rec, tag):
        if not rec["title"]:
            return
        k = rec["id"] if rec["id"] in cands else tix.get(norm(rec["title"]), rec["id"])
        if k in cands:
            c = cands[k]
            for f in ("abstract", "pdf_url", "doi", "venue"):
                if not c.get(f) and rec.get(f):
                    c[f] = rec[f]
            if c["pdf_url"]:
                c["access"] = "oa"
        else:
            cands[k] = rec; tix[norm(rec["title"])] = k
        hits.setdefault(k, set()).add(tag)
    KW = [k.lower() for d in domains for k in d.get("keywords", [])]
    jcfg = json.load(open(os.path.join(ROOT, "config", "journals.json")))["journals"]
    resolved, unresolved = sources.resolve_issns(jcfg, os.path.join(ROOT, "data", "issn_cache.json"))
    for name, issn in resolved:
        for r in sources.crossref_toc(make_id, issn, name, start, end):
            t = (r["title"] + " " + r["abstract"]).lower()
            h = sum(1 for x in KW if x in t)
            if h >= 2 or (h >= 1 and not r["abstract"]):  # cheap topical prefilter (journals publish thousands/week)
                merge(r, "JOURNAL")
        if time.time() - T0 > 1200:
            print("time budget reached; skipping remaining journals", flush=True); break
        time.sleep(1)
    print(f"after journal ToCs: {len(cands)} ({len(unresolved)} journals unresolved)")
    for d in domains:
        for r in sources.openalex(make_id, d.get("keywords", []), start, end):
            merge(r, d["id"])
    print(f"after OpenAlex: {len(cands)}")
    llm = [d for d in domains if any("language model" in k.lower() or "llm" in k.lower() for k in d.get("keywords", []))]
    phr = ["theory of mind", "social reasoning", "mentalizing", "false belief"] + [k for d in llm for k in d["keywords"]][:6]
    for r in sources.arxiv(make_id, phr, start, end):
        merge(r, "ARXIV")
    print(f"after arXiv: {len(cands)}")

    sources.s2_fill(list(cands.values()))  # back-fill missing abstracts / OA PDFs
    print(f"abstracts: {sum(1 for c in cands.values() if c['abstract'])}/{len(cands)}")

    # score: domain hits, keyword density, watch-list author, review/core-domain bonus
    kw = [k.lower() for d in domains for k in d.get("keywords", [])]
    out = []
    for k, c in cands.items():
        text = (c["title"] + " " + c["abstract"]).lower()
        h = hits[k]
        surnames = {x.split()[0].lower() for x in c["authors"] if x and x != "et al."}
        score = 2 * len([t for t in h if t.startswith("D")]) + len([t for t in h if t.startswith("P")])
        score += sum(1 for x in kw if x in text)
        if "WATCH" in h and surnames & watch_sur:
            score += 3
        if any("review" in t.lower() for t in c["pub_types"]):
            score += 1
        cc = relevance.concepts(text)
        if not cc:
            continue  # no core concept of the field: drop (surname collisions, generic keyword hits)
        score += 3 * len(cc)
        c["concepts"] = cc
        kwhits = sum(1 for x in kw if x in text)
        dom = any(t[0] in "DP" for t in h)
        watch_ok = "WATCH" in h and bool(surnames & watch_sur)
        if not (len(cc) >= 2 or (len(cc) == 1 and ((dom and kwhits >= 2) or watch_ok))):
            continue  # weak match: one generic concept without a strong keyword or watch-list hit
        if "JOURNAL" in h:
            score += 1
        c["matched"] = sorted(h)
        c["score"] = score
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    out = [c for c in out if c["score"] >= 2][:MAX_OUT]

    # preprint watch: check for published versions
    wpath = os.path.join(ROOT, "data", "preprints_watch.tsv")
    rows = [l.rstrip("\n").split("\t") for l in open(wpath)][1:] if os.path.exists(wpath) else []
    updates = []
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        q = "(" + " OR ".join('TITLE:"' + " ".join(norm(r[3]).split()[:8]) + '"' for r in batch) + ") AND SRC:MED"
        try:
            res = list(epmc(q, 50))
        except RuntimeError:
            continue
        for r in batch:
            for p in res:
                same = norm(p.get("title")) == norm(r[3]) or norm(p.get("title")).startswith(norm(r[3])[:60])
                fa = (r[2].split() or [""])[0].lower()
                if same and fa and fa in (p.get("authorString") or "").lower():
                    rec = record(p)
                    updates.append({"id": r[0], "preprint_doi": r[1], "doi": rec["doi"], "venue": rec["venue"],
                                    "year": rec["year"], "title": rec["title"], "pdf_url": rec["pdf_url"]})
                    break
        time.sleep(1)
    done = {u["id"] for u in updates}
    rows = [r for r in rows if r[0] not in done]
    # watch new high-scoring preprints too (kept two years)
    have = {r[0] for r in rows}
    cutoff = str(end - dt.timedelta(days=730))
    for c in out:
        if c["preprint"] and c["score"] >= 4 and c["id"] not in have:
            rows.append([c["id"], c["doi"], (c["authors"] or [""])[0], c["title"], str(end)])
    rows = [r for r in rows if len(r) < 5 or r[4] >= cutoff]
    with open(wpath, "w") as f:
        f.write("id\tdoi\tfirst_author\ttitle\tadded\n")
        for r in rows:
            f.write("\t".join(r) + "\n")

    result = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "window_start": str(start),
              "window_end": str(end), "n_candidates": len(out), "candidates": out,
              "preprint_updates": updates, "unresolved_journals": unresolved}
    os.makedirs(os.path.join(ROOT, "data", "runs"), exist_ok=True)
    for p in ("data/latest.json", f"data/runs/{end}.json"):
        json.dump(result, open(os.path.join(ROOT, p), "w"), indent=1, ensure_ascii=False)
    with open(os.path.join(ROOT, "data", "latest.tsv"), "w") as f:
        f.write(f"# window {start}..{end}; {len(out)} candidates; {len(updates)} preprint updates\n")
        f.write("id\tscore\tmatched\tyear\tvenue\tfirst_author\tpreprint\tpdf\ttitle\tabstract(<=700)\n")
        for c in out:
            f.write("\t".join([c["id"], str(c["score"]), ",".join(c["matched"] + c.get("concepts", [])), str(c["year"]),
                               str(c["venue"]), (c["authors"] or [""])[0], "Y" if c["preprint"] else "",
                               "Y" if c["pdf_url"] else "", c["title"],
                               re.sub(r"\s+", " ", c["abstract"])[:700]]) + "\n")
    print(f"wrote {len(out)} candidates, {len(updates)} preprint updates")

if __name__ == "__main__":
    main()
