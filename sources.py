"""Extra sources for fetch.py: Crossref journal tables of contents, OpenAlex keyword
search, arXiv (AI / theory-of-mind), and Semantic Scholar abstract back-fill.
Every function returns records in fetch.record()'s format. Standard library only."""
import json, os, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET

UA = "SCoNeLab-literature-tracker/1.0 (mailto:dstanley@adelphi.edu)"

def _get(url, data=None, tries=4, raw=False):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                return b if raw else json.loads(b)
        except Exception as e:
            time.sleep(5 * 2 ** i)
            last = e
    print(f"  give up {url[:100]}: {last}")
    return None

def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()

def _rec(make_id, doi, title, authors, year, venue, abstract, preprint=False, pdf="", src=""):
    r = {"doi": doi or "", "source": src, "id": doi or title, "pmid": ""}
    return {"id": make_id(r) if doi else f"{src.lower()}:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:80]}",
            "title": _clean(title).rstrip("."), "authors": authors[:6] + (["et al."] if len(authors) > 6 else []),
            "year": year, "venue": venue, "doi": doi or "", "pmid": "", "pmcid": "", "epmc": "",
            "first_pub": "", "pub_types": [], "preprint": preprint, "pdf_url": pdf,
            "access": "oa" if (pdf or preprint) else "abstract", "abstract": _clean(abstract)}

# ---------------------------------------------------------------- Crossref ToC
def resolve_issns(journals, cache_path):
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&amp;", "&"))
    out, unresolved = [], []
    for j in journals:
        n = j["name"]
        issn = j.get("issn") or cache.get(n)
        if not issn:
            d = _get("https://api.crossref.org/journals?rows=20&query=" + urllib.parse.quote(n))
            items = (d or {}).get("message", {}).get("items", [])
            m = next((x for x in items if norm(x.get("title")) == norm(n) and x.get("ISSN")), None)
            if m:
                issn = m["ISSN"][0]; cache[n] = issn
            time.sleep(1)
        (out if issn else unresolved).append((n, issn))
    json.dump(cache, open(cache_path, "w"), indent=1)
    return [x for x in out], [n for n, _ in unresolved]

def crossref_toc(make_id, issn, name, start, end):
    recs, cursor = [], "*"
    while True:
        q = {"filter": f"from-created-date:{start},until-created-date:{end},type:journal-article",
             "rows": 500, "cursor": cursor,
             "select": "DOI,title,author,container-title,published,abstract,type,subtype"}
        d = _get(f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(q))
        if not d:
            break
        items = d["message"].get("items", [])
        for it in items:
            au = [f"{a.get('family','')} {''.join(x[0] for x in (a.get('given') or '').replace('-', ' ').split())}".strip()
                  for a in it.get("author", []) if a.get("family")]
            y = ((it.get("published") or {}).get("date-parts") or [[None]])[0][0]
            recs.append(_rec(make_id, it.get("DOI", "").lower(), (it.get("title") or [""])[0], au, y,
                             (it.get("container-title") or [name])[0], it.get("abstract", ""), src="CR"))
        cursor = d["message"].get("next-cursor")
        if len(items) < 500 or not cursor:
            break
        time.sleep(1)
    return recs

# ---------------------------------------------------------------- OpenAlex
def _inv(ix):
    if not ix:
        return ""
    pos = sorted((p, w) for w, ps in ix.items() for p in ps)
    return " ".join(w for _, w in pos)

def openalex(make_id, keywords, start, end, limit=200):
    terms = " OR ".join(f'"{k}"' for k in keywords)
    recs, cursor = [], "*"
    while cursor and len(recs) < limit:
        q = {"filter": f"title_and_abstract.search:{terms},from_publication_date:{start},to_publication_date:{end}",
             "per-page": 100, "cursor": cursor, "mailto": "dstanley@adelphi.edu",
             "select": "doi,title,authorships,publication_year,primary_location,abstract_inverted_index,type,open_access"}
        d = _get("https://api.openalex.org/works?" + urllib.parse.urlencode(q))
        if not d:
            break
        for w in d.get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {})
            au = [a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")]
            au = [f"{n.split()[-1]} {''.join(p[0] for p in n.split()[:-1])}" for n in au]
            oa = (w.get("open_access") or {}).get("oa_url") or ""
            recs.append(_rec(make_id, doi, w.get("title") or "", au, w.get("publication_year"),
                             src.get("display_name") or "", _inv(w.get("abstract_inverted_index")),
                             preprint=w.get("type") == "preprint", pdf=oa if oa.lower().endswith(".pdf") else "", src="OA"))
        cursor = d.get("meta", {}).get("next_cursor")
        time.sleep(0.5)
    return recs

# ---------------------------------------------------------------- arXiv
def arxiv(make_id, phrases, start, end, limit=100):
    q = "(cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:q-bio.NC) AND (" + " OR ".join(f'abs:"{p}"' for p in phrases) + ")"
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": q, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit})
    b = _get(url, raw=True)
    if not b:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    recs = []
    for e in ET.fromstring(b).findall("a:entry", ns):
        pub = (e.findtext("a:published", "", ns) or "")[:10]
        if not (str(start) <= pub <= str(end)):
            continue
        aid = e.findtext("a:id", "", ns).rsplit("/", 1)[-1]
        doi = e.findtext("x:doi", "", ns) or f"10.48550/arxiv.{re.sub(r'v[0-9]+$', '', aid)}"
        au = [n.findtext("a:name", "", ns) for n in e.findall("a:author", ns)]
        au = [f"{n.split()[-1]} {''.join(p[0] for p in n.split()[:-1])}" for n in au if n]
        recs.append(_rec(make_id, doi.lower(), e.findtext("a:title", "", ns), au, int(pub[:4]), "arXiv",
                         e.findtext("a:summary", "", ns), preprint=True, pdf=f"https://arxiv.org/pdf/{aid}", src="ARXIV"))
    return recs

# ---------------------------------------------------------------- Semantic Scholar abstracts
def s2_fill(cands):
    need = [c for c in cands if not c["abstract"] and c["doi"]]
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        d = _get("https://api.semanticscholar.org/graph/v1/paper/batch?fields=abstract,openAccessPdf",
                 data=json.dumps({"ids": ["DOI:" + c["doi"] for c in chunk]}).encode())
        for c, r in zip(chunk, d or []):
            if r and r.get("abstract"):
                c["abstract"] = r["abstract"]
            if r and not c["pdf_url"] and (r.get("openAccessPdf") or {}).get("url"):
                c["pdf_url"] = r["openAccessPdf"]["url"]; c["access"] = "oa"
        time.sleep(3)
