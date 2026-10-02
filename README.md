# SCoNeLab literature fetcher

Deterministic half of the SCoNeLab weekly literature tracker.

- `fetch.py` (standard library only) queries Europe PMC, which covers PubMed plus bioRxiv, medRxiv, PsyArXiv and Research Square. It runs one query per domain in `config/scope.json` plus the author watch list, scores and deduplicates the results, and attaches abstracts and legitimate open-access PDF links. It also checks watched preprints for published versions.
- `sources.py` adds more sources. **Crossref tables of contents** pull every new article from the journals in `config/journals.json` (Nature, Science, Current Biology, Sci Rep, PNAS, ...) and keep only those matching the domain keywords. **OpenAlex** adds a keyword search covering nearly all DOI-registered journals and preprints. **arXiv** adds a search for LLM and theory-of-mind papers. **Semantic Scholar** back-fills missing abstracts and open-access PDFs. Google Scholar is deliberately not used: it has no API and forbids scraping.
- `.github/workflows/fetch.yml` runs it every Monday at 08:17 UTC and commits `data/`. You can also run it by hand from the Actions tab ("Run workflow").
- Claude's Monday task reads `data/latest.tsv` and `data/latest.json`, screens and summarizes the candidates, and writes the digest to the tracker page.

There is no judgment and no LLM in this repo. Edit `config/scope.json` to change keywords, domains or the watch list. Domain edits made through topics.md / the tracker page should also be mirrored here.

Run locally: `python fetch.py --days 10` (add `--end YYYY-MM-DD` for a past window).
