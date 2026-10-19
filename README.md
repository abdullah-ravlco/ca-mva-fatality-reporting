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
| 4 | `severity_screening` | `incidents.json` | `screened_incidents.json` (per incident: status, tags, reasons, facts JEV extracted with `*_quote_verified`, JEV's own status), `passed_leads.json` (only the passed leads, with screening result and full article content), `screening_errors.json` (every failed JEV call) |
| 5 | `lead_verification` | `passed_leads.json` (only the leads that passed screening, with their full article content) | `verified_leads.json` (per incident: qualification verdict and reasons, incident facts, every person with name/age/residence/fault/impairment, quotes with `quote_verified`, agent log), `fetch_cache.json` |
| 6 | `contact_enrichment` | `verified_leads.json` (verdict `qualifies`) | `contacts.json` and `contacts.csv` (the team's mailer layout, one row per deceased person: name, death date, aliases, address, phones, age, locations, accident notes; then match status and up to 2 next of kin, closest first: spouse, children, parents, siblings, others) (per deceased person: match status, date of birth, last address, phones, relatives with relationship, address, phones), `contacts_details.json` (candidates, match reasons, lookups), `searchbug_errors.json` |

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
- **Severity screening** (stage 4) sends the full text of every article of an incident to JEV
  (`JEV_API_URL`, `JEV_API_KEY`, `JEV_MODEL` in `.env`; OpenAI chat-completions format). JEV
  extracts the facts and proposes a status; `decision.py` recomputes it from the facts:
  `qualified`, `victim_identity_unidentified` (name unknown), `fault_unidentified` (fault unknown),
  or `dropped` with a `drop_reason` (codes + text). Drop codes: ruled out by the evidence
  (`injury_not_catastrophic`, `no_other_party`, `victim_at_fault`, `own_driver_at_fault`,
  `victim_impaired_driver`) or not enough to qualify (`injury_not_reported`, `other_party_unclear`,
  `conflicting_reports`, `crash_details_missing`, `age_missing`, `name_and_fault_unknown`,
  `no_victim_reported`). Each victim is judged separately; the incident takes the best result.
  A death is always catastrophic; "critical condition" / "life-threatening" count as catastrophic.
  Only non-dropped incidents go to lead verification. Every JEV failure is logged with its HTTP
  status, response excerpt, request id, attempts and a hint; 429/5xx/timeouts are retried. Failed
  incidents are `screening_failed` and retried next run.
- **Lead verification** (stage 5) is an agent (`agent.py`). The model gets the full text of an
  incident's articles and decides whether to search the web (`web_search`), read more pages
  (`fetch_page`) or return the structured result (`schema.py`). If it answers without searching
  while key facts are missing, it is pushed once to search. Each quote is then checked against
  the page it cites (`quote_verified`). `qualification.py` decides the verdict from the facts:
  it does **not** qualify with no death, no other party involved (single-vehicle), every deceased
  person impaired or at fault, not a road crash, outside California or outside the window;
  it **qualifies** with a death, another party involved and at fault, and a deceased person who
  was not impaired or at fault; **fault_unidentified** when the other party's fault is not
  established (under investigation, a driver's medical emergency); otherwise **needs_review**.
  Before deciding, `checks.py` removes claims the quotes do not support and records each
  correction: "not a road crash" for a vehicle crash (a car striking a person, a building or a
  home is a road crash), "cited or arrested" without a quote saying so, "the other party was at
  fault" without a quote showing what they did, and a residence taken from the crash location.
  `python -m src.modules.lead_verification.pipeline --reassess` re-decides saved leads with the
  current rules without calling the model (`--redecide` does the same for severity screening).
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
- **Personal data.** Stages 1-5 record only what public news and official sources state.
  `contact_enrichment` then looks up the deceased person and their relatives in SearchBug (a paid
  people-search service; the account's permissible purpose must cover this use). A SearchBug record
  is used only when the name and the age or a death record agree; several candidates are left as
  `ambiguous` for a person to choose, never picked automatically; a record that agrees on name and age
  only is filled in but marked `possible_match` until a person confirms it. Relationships come from SearchBug
  or from the articles' own words, otherwise "relationship not stated". Results are leads, not
  verified identities: confirm identity and relationship before any contact, and follow the rules
  that apply to contacting families.
