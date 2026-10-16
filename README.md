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
| `TAVILY_API_KEY` | no; Tavily search in `source_collection` is skipped without it | — |

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
| 1 | `source_collection` | publisher RSS feeds, Google News RSS searches (statewide + ~50 California areas) and Tavily news search | `all_raw.json` (every item, unfiltered), `finalized_raw.json` (same-link duplicates, off-topic and old stories removed), `run_history.json` |
| 2 | `link_extraction` | `finalized_raw.json` (every item is opened) | `articles.json` (one record per article: fetch status, text, page metadata, full-text filter decision), `extraction_errors.json` |
| 3 | `incident_grouping` | `articles.json` (kept articles) | `incidents.json` (articles grouped per crash, candidate dates/locations, conflicts, uncertainties, grouping rationale) |
| 4 | `lead_verification` | `incidents.json` | `verified_leads.json` (per incident: qualification verdict and reasons, incident facts, every person with name/age/residence/fault/impairment, quotes with `quote_verified`, agent log), `fetch_cache.json` |

Every run is also kept in `data/runs/<run_id>/<stage>/`.

### source_collection: raw items and finalizing

Every item has five fields: `published_date`, `source_name`, `title`, `url`, `description`.
`all_raw.json` keeps everything fetched, exactly as collected. `finalized_raw.json` drops, in order:

1. items with the same canonical link;
2. low-value sources (`source_quality.py`): law-firm marketing, social media, NewsBreak.
   Yahoo/AOL syndication and local aggregators are kept;
3. stories not about a deadly road crash (`topic.py`): other topics, plane/train/amusement-park
   deaths, statistics and road-safety pieces;
4. stories clearly outside California (`location.py`): another state, city, county or country is
   named and nothing points to California, or the outlet is out of state and the text has no
   California indicator. A story with no location is kept, and "Utah man ... in Kern River
   Valley" is kept;
5. stories published before the lookback window (undated ones are kept).

Google News links are then resolved to the publisher URL (`resolve_links.py`; about 3 s per
link, cached in `data/state/google_links.json`, so only the first run is slow). A link that
cannot be resolved stays as collected, and links that become equal are merged.

`data/history/run_history.json` lists every run with statistics only (raw items, how many were
dropped for each reason, finalized, Google links resolved). The same entry is saved in
`data/runs/<run_id>/source_collection/`. Incremental loading is not implemented yet.

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
- **Lead verification** (stage 4) is an agent (`agent.py`). The model gets the full text of an
  incident's articles and decides whether to search the web (`web_search`), read more pages
  (`fetch_page`) or return the structured result (`schema.py`). If it answers without searching
  while key facts are missing, it is pushed once to search. Each quote is then checked against
  the page it cites (`quote_verified`). `qualification.py` decides the verdict from the facts:
  it does **not** qualify with no death, no other party involved (single-vehicle), every deceased
  person impaired or at fault, not a road crash, outside California or outside the window;
  it **qualifies** with a death, another party, and a deceased person who was not impaired or at
  fault (open items such as "fault not yet reported" are listed); otherwise **needs_review**.
  Unchanged incidents are not analysed again.

## Policies

- **Blocked pages fall back.** A 401/403 response is retried once with a second browser
  profile. A page that is still blocked (or shows a CAPTCHA, which is never solved) is read
  through the site's WordPress REST API, and any remaining failure through the Wayback
  Machine. `extraction_method` records the route (`page`, `wordpress_api`,
  `wayback_snapshot`). If every route fails the article is `blocked`/`failed` and keeps its
  RSS data.
- **Fault is what the sources report.** `at_fault` is yes/no/unknown per person with a basis
  (stated by authorities, the other party cited or arrested, circumstances described, or none)
  and a quote. It decides the verdict only when a deceased person is reported at fault or
  impaired; unknown fault stays an open item. It is not a legal determination.
- **Personal data.** Only what public sources state is recorded. Residence is a city as
  written in a source; home addresses are not searched for, and people-search, property
  and social-media sites are never used as sources.
