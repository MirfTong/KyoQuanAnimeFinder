# KyoQuan Catalogue

<https://kyoquan.onrender.com/>

KyoQuan is a React single-page application backed by a Flask REST API and
PostgreSQL. It supports one searchable catalogue for Anime, Manga, and Manhwa,
with metadata populated incrementally from Tenrai and Jikan-compatible APIs.

Genres are normalized across all three content types:

- `genre` stores each genre name once.
- `anime_genre` connects Anime to genres.
- `manga_genre` connects Manga and Manhwa to the same genre rows.
- `genres_detailed` arrays retain searchable themes, demographics, explicit
  categories, and other detailed tags.

Anime studios and streaming providers use the same normalized relational
approach:

- `studio` stores each studio once, with a normalized name and optional MAL ID.
- `anime_studio` connects an Anime to any number of studios.
- `streaming_service` stores each provider once.
- `anime_streaming_service` connects Anime to providers and stores the
  title-specific external URL on that relationship.
- `author` stores each Manga or Manhwa author once using a normalized name and
  optional MAL person ID.
- `manga_author` connects readable titles to authors and stores the credited
  role when the provider supplies one.

Frequently filtered score, year, type, season, status, episode, chapter,
volume, genre, tag, studio, streaming-service, and author fields and linking tables
have PostgreSQL indexes. The schema enables PostgreSQL's trusted `pg_trgm`
extension for indexed partial-title searches. The ETL maintains an indexed
`is_adult` flag so public queries do not repeatedly scan genre arrays. It also
rebuilds the indexed `catalogue_facet` table for genre, tag, studio,
streaming-service, and author options. Five-minute process caches reuse facet results,
numeric slider bounds, and exact pagination totals. Hentai and Erotica records
are rejected during discovery and detail refresh, removed during cleanup, and
excluded from public API queries.

Schema upgrades are additive and versioned. A process lock plus PostgreSQL
transaction advisory lock serializes the one-time migration across Render
workers and GitHub Actions; later imports use the recorded schema version and
skip repeated DDL.

## Local setup

1. Create `.env` from `.env.example` and set `DATABASE_URL` to PostgreSQL.
2. Install Python dependencies with `uv sync`, or use the included virtual
   environment.
3. Install and build the frontend:

   ```powershell
   npm --prefix frontend ci
   npm --prefix frontend run build
   ```

4. Run the API and React host:

   ```powershell
   .\.venv\Scripts\python.exe -m backend.app
   ```

For production, build the frontend and start `gunicorn backend.app:app`.

## Production database (Neon)

KyoQuan uses a managed Neon PostgreSQL database in production. The Flask web
service and the scheduled GitHub Actions ETL must use the **same pooled Neon
connection string**; otherwise the website and ETL will read and write different
catalogues.

Create a Neon project without enabling Neon Auth, then use its pooled
PostgreSQL connection string. Neon includes the required SSL settings in that
string. Keep the real value secret and set it only in these two locations:

```text
# Render web service environment variable
DATABASE_URL=<pooled-neon-postgresql-url>

# GitHub Actions repository secret
DATABASE_URL=<same-pooled-neon-postgresql-url>
```

On an empty database, importing the Flask app runs the additive schema setup
and creates the tables, indexes, normalized relationship tables, ETL cursors,
cached facets, and analytics tables. The first Jikan ETL run then rebuilds the
catalogue incrementally. It is normal for Anime to appear before Manga and
Manhwa during an initial run.

Neon's free plan can suspend compute after inactivity, so the first database
request after a quiet period can be briefly slower. Monitor the Neon dashboard
for storage use throughout the rebuild. The provider's billed project or branch
storage can differ from PostgreSQL's logical database size because retained
history and branches are outside the live relation-size total.

Run the storage and duplicate audit before attempting cleanup. It is read-only
by default and prints the logical database size, table/index sizes, duplicate
natural keys, expired analytics rows, and unreferenced lookup rows:

```powershell
.\.venv\Scripts\python.exe -m backend.jobs.database_maintenance
```

The catalogue tables and join tables have unique keys, so repeated imports
update existing records rather than appending duplicate titles or links. The
maintenance command never proposes deleting Anime, Manga, or Manhwa. After a
backup and review of the dry-run counts, the bounded analytics/orphan cleanup
can be applied explicitly:

```powershell
.\.venv\Scripts\python.exe -m backend.jobs.database_maintenance --retention-days 365 --apply --confirm CLEANUP
```

If Neon rejects writes or connections, disable the scheduled workflow first,
then compare the web-service `DATABASE_URL` and GitHub Actions `DATABASE_URL`
without printing either secret. In the Neon dashboard, inspect all branches and
retained history as well as the primary database. Take a backup before removing
any branch or changing history retention. Run the read-only audit against the
intended production URL, restore capacity or move the verified backup to a new
database, and test `GET /api/v1/catalogue` before re-enabling the workflow.

When the database is temporarily unavailable, the Flask process still serves
the React application. API database failures return a retryable JSON `503`, and
the interface shows a service-unavailable message with a **Try again** button
instead of an empty result page.

### Backups and rollback

Create regular off-site PostgreSQL backups from Neon. Do not commit dump files
or database URLs:

```powershell
$env:PGDATABASE_URL = '<pooled-neon-postgresql-url>'
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' `
  --format=custom --no-owner --file kyoquan-backup.dump $env:PGDATABASE_URL
```

To restore to a replacement database, use `pg_restore` with a new, empty Neon
database and then point both `DATABASE_URL` settings to that replacement.
Before retiring a previous provider, keep its database and at least one verified
off-site dump until row counts, API responses, and a small ETL batch have been
validated against Neon.

## REST API

The canonical endpoints are:

- `GET /api/v1/catalogue` searches and filters the catalogue.
- `GET /api/v1/catalogue/random` returns a random selection.
- `GET /api/v1/catalogue/<content_type>/<mal_id>` returns full details.
- `GET /api/v1/genres` lists normalized genres.
- `GET /api/v1/tags` searches detailed tags.
- `GET /api/v1/studios` searches normalized Anime studios.
- `GET /api/v1/streaming-services` searches normalized streaming providers.
- `GET /api/v1/authors` searches normalized Manga and Manhwa authors.
- `GET /api/v1/filter-ranges` returns cached numeric bounds for the current
  content scope.

Use `content_type=ANIME`, `MANGA`, `MANHWA`, or `ALL`. Common list filters are
`q`, `genre`, `tag`, `min_score`, `min_year`, `max_year`, `page`, and
`per_page`. Anime also supports `type`, `season`, `status`, `min_episodes`,
`max_episodes`, repeatable `studio`, and repeatable `streaming_service`
parameters. Manga and Manhwa support `status`, `min_chapters`, `max_chapters`,
`min_volumes`, `max_volumes`, and repeatable `author`.

The React range controls keep using these explicit minimum and maximum API
parameters, so filtered URLs remain bookmarkable and Browser Back/Forward can
restore the complete state. When no range is selected, records with unknown
values remain eligible. Applying a range excludes unknown values because they
cannot be confirmed to satisfy it. Adaptive slider scales keep common episode,
chapter, and volume values precise while retaining the catalogue's full
extrema. The `ALL` view intentionally shows only filters that apply across the
whole catalogue: search, genres/tags, score, and year. Media-specific filters
remain available in the Anime, Manga, and Manhwa views.

Multiple Studio or Streaming Service selections use match-any semantics.
Multiple Author selections also use match-any semantics.
Streaming links are provider-supplied hints rather than guaranteed regional
availability, and availability may change.

The existing `/api/v1/anime`, `/api/v1/anime/random`,
`/api/v1/anime/seasonal`, and `/api/v1/anime/<mal_id>` routes remain available.
Convenience `/api/v1/manga` and `/api/v1/manhwa` list, random, and detail routes
are also provided.

## Private visit analytics

KyoQuan records lightweight, privacy-conscious frontend visit aggregates in
PostgreSQL. A browser receives an HttpOnly, SameSite `Lax` random cookie that
expires after 30 days. The database stores only a SHA-256 digest of that random
value, the UTC date, a fixed `frontend` category, and an aggregate visit count.
It does **not** store IP addresses, user-agent strings, query strings, search
terms, authentication data, or raw cookie values. A unique database constraint
on cookie digest, date, and category prevents repeat refreshes from increasing
the same day's anonymous-visitor count, while `visit_count` still preserves the
total number of page visits.

Only successful browser HTML page loads are eligible. API requests, static
assets, health/probe paths, and recognizable bots are excluded. Tracking is
best-effort: a database problem is rolled back and never prevents KyoQuan from
serving a page.

Analytics are intentionally backend-only. Set these Render environment
variables (never commit their real values):

```text
ADMIN_ANALYTICS_TOKEN=<a-long-random-secret>
ANALYTICS_COOKIE_SECURE=true
```

`ADMIN_ANALYTICS_TOKEN` protects the private reporting route. Call it from a
terminal, Postman, or another trusted server-side tool; do not place this token
in the React application or a public URL:

```bash
curl -H "Authorization: Bearer <ADMIN_ANALYTICS_TOKEN>" \
  https://kyoquan.onrender.com/api/v1/admin/analytics/visits
```

The response includes aggregate total, daily (last 7 days), weekly (last 12
weeks), monthly (last 12 months), and category counts. Missing credentials
receive `401`, invalid credentials receive `403`, and the endpoint is not part
of any public catalogue payload.

## Provider client

`backend.services.jikan_client` uses Tenrai's Jikan-compatible v1 API as its
primary provider and public Jikan v4 as a fallback. Override the endpoints with
`ANIME_API_BASE_URL` and `ANIME_API_FALLBACK_BASE_URL`. Streaming enrichment
uses Jikan directly by default because Tenrai's otherwise valid full responses
rarely include service links; override that endpoint separately with
`ANIME_STREAMING_API_BASE_URL`.

Anime and manga calls share one process-wide limiter capped at three requests
per second and 55 requests per minute. Temporary network failures and 5xx
responses have bounded retries; HTTP 429 responses honor a cooldown. Paginated
catalogue scans stay pinned to the primary provider so page boundaries cannot
change during a cursor-based run.

## ETL behavior

The scheduled worker plans due work, closes its database session and connections,
fetches provider data into temporary files on the runner, and then applies the
results in short transactions. No database transaction spans the rate limiter
or external API requests. Small runs may finish fetching before Neon can
suspend; the worker does not add sleeps to force suspension.

The same bounded pipeline runs locally or through GitHub Actions:

```powershell
.\.venv\Scripts\python.exe -m backend.jobs.jikan_etl --scheduled-sync --page-limit 10 --limit 200 --streaming-limit 100 --request-budget 800
```

Scheduled defaults (standalone focused commands keep their existing behavior):

| Setting | Default | Meaning |
| --- | --- | --- |
| `--request-budget` | 800 | Maximum actual HTTP attempts, including retries and fallbacks; minimum 40 |
| `--page-limit` | 10 | Maximum pages per TV, Movie, OVA, ONA, Special, TV Special, Manga and Manhwa cursor |
| `--limit` | 200 | Maximum Anime detail selections, plus 200 readable-title detail selections split between Manga and Manhwa; minimum 2 |
| `--streaming-limit` | 100 | Maximum due Anime without saved streaming links |
| `--batch-size` | 25 | Maximum detail attempts applied per transaction; pages commit independently |

Current season discovery has a separate ten-page cap; upcoming season discovery
has a one-page cap. HTTP shares are reserved: 25% discovery, 30% Anime details,
15% Manga details, 15% Manhwa details, and 15% streaming. Requests are interleaved
across queues and discovery cursors. Unused shares are not borrowed, so an outage
or large queue cannot consume another category's allocation. Caps can mean fewer
records are handled than selected.

Existing `jikan_sync_state` keys and progress are retained. Pages commit their
catalogue changes and next cursor atomically. A failed page is retried on a later
run; completed bulk scans wrap to page 1 but wait 14 days before beginning a new
pass. Completed seasonal scans wait three days. Partial scans remain eligible,
and the oldest-attempted discovery cursors are considered first. Existing
provider limits and shrinking-catalogue recovery still apply.
Reaching Manga's provider page cap finishes the accessible pass atomically,
waits before restarting, and is reported separately as limited coverage.

Schema version 7 adds `jikan_refresh_state`; it does not reset existing cursors or
rewrite the catalogue. This records successful detail refreshes independently
of listing timestamps. Listing updates cannot postpone missing detail data.
On the first optimized run, titles without this new state are eligible and are
gradually processed within the budget.

Detail slots rotate between active and archive titles, and between never
attempted and previously attempted titles within each group. Overdue attempts
are oldest first. This reserves progress for older titles while new titles
continue to arrive. Missing seasons are filled by normal detail responses and
listings; the scheduled worker no longer makes an extra per-title season request.
The standalone `--backfill-seasons` repair command remains available.

Refresh intervals are configurable through environment variables:

| Variable | Days | Applies to |
| --- | --- | --- |
| `ETL_AIRING_DAYS` | 3 | Airing Anime and publishing Manga/Manhwa; completed seasonal scan restart |
| `ETL_RECENT_DAYS` | 7 | Upcoming, recently finished, unknown status or unknown finish date |
| `ETL_STABLE_DAYS` | 60 | Finished titles whose provider end date is at least 90 days ago |
| `ETL_RETRY_DAYS` | 1 | Temporary failures, malformed results, incomplete detail/streaming responses |
| `ETL_DISCOVERY_DAYS` | 14 | Restarting a completed bulk catalogue pass |

All intervals must be positive. They describe eligibility, not a guarantee that
the next run will reach a title: the 72-hour guard and queue budgets still apply.
A missing detail endpoint (404) is eligible again after 30 days. A basic-endpoint
fallback can update available metadata but does not mark full details successful.

Valid empty streaming responses back off for 7, 30, then 90 days. Failures retain
the empty-result streak and retry sooner. Successful detail responses supply
streaming data when that field is usable; an equivalent full-URL HTTP request
is also cached within the run. Different providers remain distinct. Both the
response cache and fetched payload spool are temporary runner files, deleted
on exit and never uploaded to Actions caches or artifacts.

Missing/malformed relationship fields preserve known links. Valid empty
studio, streaming and author arrays clear stale links; partially malformed
arrays remain additive. Malformed genre arrays preserve existing genres. Only
HTTP(S) streaming URLs are saved. Scheduled writes compare business data and
preserve an unchanged catalogue row's `last_jikan_sync`; detail freshness is
tracked separately. Streaming-only batches do not read title descriptions or
unrelated relationships. Author/studio/service lookups are limited to the
incoming batch. Existing adult-only cleanup remains; no catalogue pruning is
performed to meet a storage quota. Facets are published only after changes.

A fully applied budget-limited run is successful even when work remains, and
counts toward the 72-hour cadence. Deferred/unfetched titles are not marked
attempted. When at least one verified page or complete record is committed,
routine item failures (404s, exhausted temporary retries, sparse responses) and
failed pages produce `success_with_warnings`, not a failed workflow. This counts
toward the same 72-hour interval, preventing one unavailable title from causing
another full ETL every day. Failed items retain retry state; failed pages do not
advance. Partial detail payloads may safely improve metadata, but do not mark
detail freshness successful: title/status, genres and studios (Anime) or authors
(Manga/Manhwa) must be usable. Streaming completeness is tracked separately.

An outage with no verified progress remains fatal, including when unsuccessful
HTTP attempts exhaust the budget. Unexpected exceptions and database/schema
errors also fail the run. Database failures roll back the current transaction;
earlier committed pages/batches remain resumable. Fatal runs do not reset the
cadence, so the next daily check may retry. Interruptions during fetching leave
planned work pending, and the next run may fetch it again.

Useful focused commands are:

```powershell
# Discover Manga and Manhwa catalogue pages
.\.venv\Scripts\python.exe -m backend.jobs.jikan_etl --manga-catalogue --page-limit 40

# Refresh the next 1,000 readable-title details
.\.venv\Scripts\python.exe -m backend.jobs.jikan_etl --refresh-manga --limit 1000

# Import the current Anime season
.\.venv\Scripts\python.exe -m backend.jobs.jikan_etl --season current

# Refresh the existing Anime catalogue
.\.venv\Scripts\python.exe -m backend.jobs.jikan_etl --limit 1000
```

## Scheduled GitHub Actions sync

`.github/workflows/jikan-sync.yml` wakes once per day. A read-only GitHub Actions
API guard starts the ETL only when at least 72 hours have passed since the last
scheduled run whose **Sync Anime, Manga, and Manhwa** step completed successfully.
Successful daily runs where that step was skipped do not reset the interval. The
guard inspects workflow jobs and steps, and fails closed without starting a
database-writing sync if the GitHub API history cannot be verified. Manual
`workflow_dispatch` runs bypass the cadence guard, while the existing
concurrency group ensures manual and scheduled jobs never write simultaneously.

Run a manual sync from the repository's **Actions** page by selecting
**Scheduled catalogue metadata sync** and choosing **Run workflow**. All phases
run inside one Python process, which preserves the shared rate limiter. The
workflow caches only pip dependencies, keyed by requirements.txt. The step
summary and `ETL efficiency` JSON log report HTTP attempts, successes, failures,
requests avoided, selected/changed/unchanged/failed/deferred records, adult-only
removals, applied/failed pages, cursor positions, fetch/apply seconds, and exhausted
budget categories. `records_succeeded` counts complete record refreshes, and
`failure_reasons` separates missing, temporary, invalid and incomplete responses.
Record counters are phase operations, not unique title counts;
deferred counts cover due detail/streaming queues, while page progress is shown
by cursor. HTTP success means a parsed response, not necessarily valid title data.

Compare several runs by saving these summaries and the corresponding Neon
dashboard compute/transfer measurements over matching time windows. Compare
similar backlog sizes and account for manual runs and site visits. Fetch/apply
timings are not measured Neon CU-hours or transfer, and do not establish a
specific saving. Smaller defaults reduce throughput: older titles and initial
catalogue discovery take more runs. Increase budgets only after observing usage.

Add the external PostgreSQL URL as a repository Actions secret named
`DATABASE_URL` before running the workflow.

The facet publication step is incremental: it inserts new filter values and
deletes only stale values. It no longer deletes and reinserts the entire indexed
facet table on every ETL run, reducing persistent write amplification and
retained database history.

For Render, use:

```bash
npm --prefix frontend ci && npm --prefix frontend run build && pip install -r requirements.txt
```

## Tests

Run the complete backend suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

The application endpoint tests require a populated local PostgreSQL fixture and
a frontend build. For isolated ETL validation, set `DATABASE_URL=sqlite://` in
the test process and run:

```powershell
python -m unittest tests.test_efficient_sync tests.test_etl_relationship_recovery tests.test_jikan_client tests.test_jikan_etl tests.test_manga_etl tests.test_sync_guard tests.test_workflow tests.test_models tests.test_database_maintenance
```

These unit tests use mocked providers and isolated SQLite transactions (with
JSON standing in for PostgreSQL arrays), plus model/schema assertions. Those
assertions alone do not validate PostgreSQL persistence or migrations.

Run the PostgreSQL integration suite against a disposable **local** database:

```powershell
$env:DATABASE_URL = 'sqlite://'
$env:ETL_TEST_DATABASE_URL = 'postgresql+psycopg2://etl_test@127.0.0.1:55437/kyoquan_etl_test'
python -m unittest tests.test_etl_postgres
```

The integration suite uses isolated test schemas, real PostgreSQL migrations
and transactions, and mocked HTTP responses. It covers fresh installation,
version-6 upgrade preserving catalogue/cursors, scheduled-entrypoint recovery,
and duplicate-safe relationship updates. It never needs Neon or a live ETL.
Without `ETL_TEST_DATABASE_URL`, PostgreSQL integration checks are skipped.

Run the frontend production-build validation:

```powershell
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

The regression suite covers provider URLs and fallbacks, Movie discovery,
Manga/Manhwa mapping, normalized Anime studios and streaming services,
non-destructive sparse-payload reconciliation, adult-content exclusion,
duplicate-safe page application, independent cursors, refresh-queue
progression, API filtering and numeric ranges, and workflow configuration.
Rendered React interaction tests additionally cover touch and keyboard slider
behavior, searchable multi-select navigation, mobile disclosure, clear
behavior, and mixed-content filter guidance.

## Tech stack

- Python, Flask, Flask-SQLAlchemy, SQLAlchemy, and PostgreSQL
- Tenrai v1 and Jikan v4-compatible APIs
- React, Vite, and Tailwind CSS
- GitHub Actions and Render
