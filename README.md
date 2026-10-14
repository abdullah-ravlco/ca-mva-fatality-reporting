# California fatal motor-vehicle crash reporting pipeline

Collects public news reports and official releases about fatal motor-vehicle crashes in
California, groups them into incidents, and produces a quote-checked evidence record per
incident. The LLM runs once per incident, after articles have been deduplicated, fetched,
filtered and grouped.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then set OPENAI_API_KEY
```

| Variable | Required | Default |
|---|---|---|
| `OPENAI_API_KEY` | for `lead_verification` | — |
| `OPENAI_MODEL` | no | `gpt-4.1-mini` |

## Running

```bash
python -m src.main                              # every stage, in order
python -m src.main --stage link_extraction      # one stage (repeatable)
.venv/bin/python -m pytest -q                   # tests (no network, no LLM)
```

The run stops at the first stage that fails, so later stages never run on stale input.
Logs go to the console and `logs/pipeline_<run_id>.log`.

## Stages

| # | Stage | Reads | Writes (`data/<stage>/`) |
|---|---|---|---|
| 1 | `source_collection` | Google News RSS queries, publisher RSS feeds, coroner and CHP pages | `raw_feed_items.json` (every feed item, before filters), `fetched_sources.json` (every record + filter decision), `sources_to_open.json` (cases) |
| 2 | `link_extraction` | `sources_to_open.json` | `articles.json` (one record per article: fetch status, text, page metadata, full-text filter decision), `extraction_errors.json` |
| 3 | `incident_grouping` | `articles.json` (kept articles) | `incidents.json` (articles grouped per crash, candidate dates/locations, conflicts, uncertainties, grouping rationale) |
| 4 | `lead_verification` | `incidents.json` | `incident_analysis.json` (per incident: scope checks, evidence status, every person, fault statements, quotes and source URLs), `fetch_cache.json` |

Every run is also kept in `data/runs/<run_id>/<stage>/`.

### What each stage decides

- **Article deduplication** (stage 1) merges only copies of the same article: the same
  canonical URL, or a near-identical headline from the same publisher. Every query and feed
  that found an article is kept in `discovered_by_queries` / `discovered_by_sources`.
- **Full-text filter** (stage 2) drops articles whose text puts the crash outside the
  lookback window or outside California, says it happened earlier, or does not describe a
  fatal road crash. Undetermined articles are kept. Articles that could not be fetched keep
  their RSS data and stay in the output.
- **Incident grouping** (stage 3) is deterministic. Records join one incident only when
  nothing conflicts (date, place, county, case number) and there is positive evidence
  (similar headlines, same place/road and date, same victim age and type, overlapping text).
  Victim type and age never split an incident: one crash can have several victims.
- **Incident analysis** (stage 4) lists every person involved, named or unnamed. Each fact
  must be supported by a quote found in the cited source.

## Policies

- **Blocked pages fall back.** A 401/403 response is retried once with a second browser
  profile. A page that is still blocked (or shows a CAPTCHA, which is never solved) is read
  through the site's WordPress REST API, and any remaining failure through the Wayback
  Machine. `extraction_method` records the route (`page`, `wordpress_api`,
  `wayback_snapshot`). If every route fails the article is `blocked`/`failed` and keeps its
  RSS data.
- **Fault is never inferred.** It is reported as the sources state it, with the statement
  type (official finding, attributed to authorities, allegation, citation/arrest, under
  investigation). Being struck, being a passenger, or another person's arrest does not make
  anyone "not at fault". A person is `explicitly_reported_not_at_fault` only when a source
  says so. None of this is a legal determination, and fault never affects scope or evidence
  status.
- **Personal data.** Only what public sources state is recorded. Residence is a city as
  written in a source; home addresses are not searched for, and people-search, property
  and social-media sites are never used as sources.
