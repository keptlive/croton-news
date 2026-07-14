# croton.news

Hyperlocal AI-powered news site for Croton-on-Hudson, NY. Aggregates community news, generates articles from meeting transcripts, and provides RAG-powered search across years of municipal records.

## Architecture

**Flask app** (`app.py`, ~2100 lines) serving:
- AI-generated articles from meeting video transcripts
- Community news aggregation (RSS scrapers for local sources)
- RAG search across transcripts, documents, and entities
- Community calendar (ICS feeds from village, schools, library, parks)
- Photo gallery with comments
- Knowledge graph of local people, places, and organizations

**RAG pipeline** (`rag/`, 30+ scripts) handling:
- Meeting discovery from ChampDS API + YouTube + BoardDocs
- Audio download, Deepgram Nova 3 transcription with diarization
- Transcript enrichment (proper nouns, speaker correction)
- Chunk embedding (Gemini 3072-dim vectors)
- Article generation (Claude Opus / GLM 5.0 Turbo)
- Entity extraction and topic threading

## Databases

All in `rag/` directory:

| DB | Purpose |
|----|---------|
| `rag.db` | Primary: meetings, chunks, embeddings, entities, topic_threads |
| `comments.db` | Photo gallery comments |
| `tips.db` | Anonymous news tips |
| `search.db` | Cached search results |

### Key Tables (rag.db)

**meetings** — One row per meeting session. Core fields: `date`, `committee`, `event_id` (ChampDS or `yt-VIDEO_ID`), `headline`, `article`, `article_model`, `has_transcript`, `has_minutes`, `agenda_json`, `boarddocs_id`.

**chunks** — Transcript/document segments for RAG search. Fields: `doc_id`, `doc_type`, `content`, `speaker`, `start_time`, `end_time`. FTS5 index at `chunks_fts`.

**entities** — People, places, organizations. Fields: `name`, `type`, `slug`, `metadata_json`, `mention_count`.

**embeddings** — Gemini 3072-dim vectors for semantic search. Linked to chunks by `chunk_id`.

## Pipeline Flow

```
ChampDS API → discover event IDs → download HLS video → ffmpeg extract audio
    → Deepgram Nova 3 (diarize + smart_format) → enrich proper nouns
    → chunk (800 chars) → embed (Gemini) → write article (Claude/GLM)

BoardDocs API → fetch agenda + minutes → store in meetings table
    (accessed via IPRoyal proxy from VPS, or phone relay)

YouTube RSS → discover BOE videos → download audio (phone relay)
    → Deepgram transcription → ingest → write article
```

## Cron Jobs (all UTC; all wrapped via `rag/run_job.sh` since 2026-07-13)

Every job runs through `run_job.sh JOB_NAME [hint] -- CMD`, which records the
run in `rag/job_runs.db`, logs to `/var/log/croton-jobs/JOB_NAME.log`, and
emails `ALERT_EMAIL` on failure (6h cooldown per job).

| Time | Job name | Command | What |
|------|----------|---------|------|
| 5:00 | db-backup | `rag/backup_db.sh` | Backup rag/comments/tips/photos/ecode DBs (keep 7); WireClaw box pulls offsite at 5:40 |
| 6:00 | daily-pipeline | `rag/auto_discover.sh` | Discover → agendas → minutes → transcribe → ingest → summaries |
| 7:15 | boarddocs-sync | `boarddocs.py sync` | BOE agendas/minutes from BoardDocs (IPRoyal proxy) |
| 7:30 | boe-poll | `poll_boe.py --write` | Poll CHUFSD YouTube for new BOE videos |
| :05 hourly | upcoming-agendas | `auto_pipeline.py` | Upcoming previews, placeholders, `static/upcoming_agendas.json` |
| 6h | scrapers | `run_scrapers.sh` | RSS scrapers for community news |
| 9:00 | — | `pipeline_watch.py` | **Watchdog**: outcome checks + consolidated alert email (see below) |
| Sun 3:00 | — | `cleanup_videos.sh` | Prune videos older than 60 days |

Article writing happens on the **WireClaw box** (107.173.0.190) at 8:00 via
`/root/enrich-transcripts.sh` (WireClaw agents enrich speakers + write
articles, then publish back). Its cron entry emails on failure too.

Logs: `/var/log/croton-pipeline.log` (daily pipeline stages),
`/var/log/croton-jobs/*.log` (per-job), `/var/log/croton-watch.log`
(watchdog), `/var/log/croton-notify.log` (mailer fallback). Logrotate weekly.

## Publish Gate (article quality)

Every article publish runs through a deterministic validator before touching
the DB — `rag/validate_article.py`, called inside `rag/publish_article.py`:

- quote attribution must match the transcript speaker at the `{{quote:T}}` timestamp
- quoted strings (≥6 words) must appear verbatim in the transcript
- person names must have provenance in transcript/minutes/agenda/packets/entities
- names that exist ONLY as enricher speaker labels must be corroborated by
  this meeting's minutes when minutes exist (`name-attendance`) — a label is
  an enricher assertion, not evidence. Incident: the Village Attorney changed
  2026-06-24 (Subin → Lori Lee Dickson); a stale roster labeled the new
  attorney's voice "Joshua Subin" and the name, absent from both audio and
  minutes, reached a published article.
- dollar figures must appear in a source document (1% rounding tolerance)
- caption transcripts (all "Unknown Speaker"): named attributions need minutes support

On violation: publish exits 3, nothing is published, and the report is saved
to `rag/validation/article-<id>-report.json` for the writer's retry.
`--force` bypasses (manual use only). Check a published article by hand:
`venv/bin/python rag/validate_article.py --published <meeting_id>`.
The daily watchdog also validates all articles from the last 14 days.

Born from a 2026-07-14 cross-committee fact-check that found published
articles naming the wrong dissenter on a 4-1 vote, quoting a person who
wasn't at the meeting, and citing a fabricated $106.7M budget figure.

**Prior-article chunks are NOT source material.** `doc_type='article'`
chunks exist for site search only; the writer/editor are prompted to source
exclusively from `transcript`/`minutes` chunks, minutes_text, agenda_json,
and packet_pdfs. Reason: article chunks are prior AI output, and a rewrite
once resurrected the exact fabricated quotes its predecessor was retracted
for ("fabrication echo"). REVISABLE: once every published article predates
no further than the gate era (all article chunks derive from gate-verified
text — check `article_model LIKE 'wireclaw-agent-%'` coverage), this ban
can be relaxed to allow prior articles as context-not-quotable background.
When queueing an article for rewrite (`article=NULL`), also delete its
`doc_type='article'` chunks so the echo is physically impossible.

**Speaker-label integrity** — `rag/validate_speakers.py`: deterministic
check that every personal-name speaker label in a transcript is provable
from the meeting record (minutes when present; else agenda / spoken content /
120-day committee minutes / verified entities). `--meeting N`, `--event E`,
`--recent DAYS`, `--fix` (relabels violators to "Unknown Speaker" in chunks
AND the transcript JSON; FTS rebuilt — the FTS table indexes `speaker`).
The daily watchdog sweeps the last 45 days (`check_speaker_labels`).
Roster note: officials change — the enricher/writer/editor prompt rosters
carry date ranges (Dickson since 2026-06-24, Subin through 2026-06-23) and
the enricher rule is that THIS meeting's minutes attendance OVERRIDES the
roster. Deepgram keyterms also harvest the attendance blocks of the 8 most
recent minutes unconditionally so brand-new officials are recognizable
(frequency ranking alone buried Dickson under months of Subin).

## Reliability & Alerting

- `rag/notify.py SUBJECT BODY` — send an alert email (SMTP creds in `.env`,
  recipient `ALERT_EMAIL`). `--topic X --cooldown SECONDS` dedups repeats.
- `rag/pipeline_watch.py --dry-run` — run all health checks and print.
  Checks: job cadence (job_runs.db), transcript/article/BOE freshness,
  CHUFSD YouTube RSS vs DB (→ phone-relay actions), agendas staleness,
  site up, backup age/size, disk. Emails only on problems/actions, plus a
  Monday all-healthy digest.
- `sqlite3 rag/job_runs.db "SELECT job, exit_code, finished_at FROM job_runs ORDER BY id DESC LIMIT 10"` — recent job history.

## Phone Relay (Residential IP)

YouTube and some services block VPS IPs. These scripts run from the phone (Termux):

| Script | Purpose |
|--------|---------|
| `~/bin/boe-fetch` | Fetch YouTube auto-captions for BOE meetings — **last resort only**: captions have no speaker identity and heavy garbling. Prefer the audio route (poll_boe.py downloads via WireClaw yt-dlp → Deepgram diarized), which is now tried first automatically. |
| `~/bin/boarddocs-fetch` | Fetch BoardDocs agendas/minutes (alternative to VPS proxy route). After fetching, run `boarddocs.py sync-local` on the VPS to load minutes into the DB (the 7:15 cron does this automatically). |

The `/status` page at croton.news/status flags when phone relay actions are needed.

## Proxy Configuration

**IPRoyal Web Unblocker** — used for BoardDocs access from VPS (`.gov` sites blocked):
- Proxy: `http://unblocker.iproyal.com:12323`
- Credentials in `rag/.env` as `IPROYAL_PROXY`
- Used by `boarddocs.py` automatically

## Environment Variables (rag/.env)

| Variable | Service |
|----------|---------|
| `DEEPGRAM_API_KEY` | Deepgram Nova 3 transcription |
| `GEMINI_API_KEY` | Gemini embeddings + Flash article generation |
| `ZAI_KEY` | z.ai API (GLM 5.0 Turbo for article writing) |
| `REPLICATE_API_TOKEN` | Image upscaling (Real-ESRGAN) |
| `GROQ_API_KEY` | Groq inference (backup) |
| `IPROYAL_PROXY` | IPRoyal proxy URL for BoardDocs |

## Routes (grouped)

**Content:**
`/` (home), `/editorials`, `/article/<id>`, `/meetings`, `/meeting/<id>`, `/calendar`, `/documents`

**Knowledge graph:**
`/topics`, `/topic/<slug>`, `/entities`, `/entity/<slug>`, `/author/<slug>`, `/staff`

**Media:**
`/watch/<event_id>` (video player), `/transcript/<event_id>`, `/gallery`, `/photos/`, `/videos/`, `/audio/`

**Search:**
`/search`, `/api/search`, `/api/ask` (LLM-powered answers), `/api/search/documents`

**API:**
`/api/weather`, `/api/articles`, `/api/calendar/events`, `/api/health`, `/api/photos/*`, `/api/comments`

**SEO/Feeds:**
`/feed` (RSS), `/feeds/<file>` (ICS calendars), `/sitemap.xml`, `/news-sitemap.xml`, `/robots.txt`

**Admin/Review:**
`/status` (pipeline health dashboard), `/review` (editorial review for draft articles)

## Development Commands

```bash
# SSH to VPS
ssh croton

# Restart the app — ALWAYS via systemd, NEVER nohup/fuser
# (the unit has Restart=always; a manually-started app.py races it for port 3260
#  and causes a crash loop — this took the site down on 2026-07-13)
systemctl restart croton-news

# Check logs
journalctl -u croton-news -n 50 --no-pager
tail -50 /var/log/croton-pipeline.log

# Run pipeline manually
cd /opt/croton-news/rag
/opt/croton-news/venv/bin/python pipeline.py status
/opt/croton-news/venv/bin/python pipeline.py process-new
/opt/croton-news/venv/bin/python boarddocs.py list
/opt/croton-news/venv/bin/python boarddocs.py sync

# Test routes
curl -s -o /dev/null -w "%{http_code}" http://localhost:3260/
curl -s -o /dev/null -w "%{http_code}" http://localhost:3260/status
```

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main Flask application |
| `rag/pipeline.py` | Master pipeline: discover → download → transcribe → ingest |
| `rag/write_article.py` | Article generation agent (737 lines) |
| `rag/boarddocs.py` | BoardDocs API client for BOE agendas/minutes |
| `rag/auto_pipeline.py` | Hourly: upcoming meetings, previews, agenda cache |
| `rag/poll_boe.py` | YouTube BOE video poller |
| `rag/search.py` | RAG search: FTS5 + vector + RRF fusion |
| `rag/enrich_transcript.py` | Post-Deepgram transcript cleanup |
| `rag/entities.py` | Knowledge graph entity extraction |
| `rag/embeddings.py` | Gemini embedding pipeline |
| `rag/ingest.py` | Chunk and insert transcripts into rag.db |

## Meeting Sources

| Source | Boards | Method |
|--------|--------|--------|
| ChampDS API | BOT, Planning, ZBA, WAC, VEB, CAC | Event ID scan (pipeline.py) |
| BoardDocs | Board of Education | POST API via IPRoyal proxy |
| YouTube (CHUFSD) | Board of Education | RSS feed + caption/audio download |
| Village calendar | All | Tavily search API (Cloudflare blocks direct access) |


## Status Page (`/status`)

Live health dashboard at `croton.news/status`. Checks run on each page load.

### Sections

| Section | What It Monitors |
|---------|-----------------|
| Summary Stats | Total meetings, articles, days since last ingestion |
| ChampDS API | API reachability, portal access, new event detection |
| Cron Jobs | Schedule, last run time, error detection in log output |
| Photo Enhancement Pipeline | ffmpeg, Pillow, frame_extract, Replicate token, coverage % |
| Python Dependencies | Critical venv packages: pymupdf, deepgram, google.generativeai, openai, bs4, requests |
| Expected Meeting Schedule | Known upcoming meetings vs. DB coverage (flags missing) |
| Phone Relay Actions | Items requiring residential IP (YouTube, BoardDocs) |
| Recent Logs | Tail of each pipeline log file |

### Adding New Checks

1. Add data gathering in `app.py` `status_page()` route (before `render_template`)
2. Pass new variable to template
3. Add HTML section in `templates/status.html` (before `{% endblock %}`)
4. Use badge classes: `badge-ok`, `badge-warn`, `badge-error`, `badge-dead`

## Photo Enhancement Pipeline

Runs as Step 8 of `pipeline.py process-new` after article generation.

**Flow:** Video -> ffmpeg frame extract -> layout detection (quad/podium/wide) -> auto-crop -> Replicate upscale (Real-ESRGAN + face enhance) -> sharpen -> save to `/photos/` -> insert `{{photo:EVENT:SECONDS:CAPTION}}` tags in article

**Key files:**
- `rag/insert_photos.py` — Orchestrator (picks moments via LLM, runs pipeline)
- `rag/frame_extract.py` — Frame extraction, layout detection, cropping, upscaling

**Dependencies:** ffmpeg, Pillow, Replicate API token (in `rag/.env`)

**Manual run:**
```bash
cd /opt/croton-news/rag
/opt/croton-news/venv/bin/python insert_photos.py EVENT_ID
/opt/croton-news/venv/bin/python insert_photos.py --pending  # all missing
```

## Agenda Packet Pipeline

Upcoming meetings get preview articles from PDF agenda packets.

**Flow:** ChampDS API -> extract agenda items + attachments -> download PDFs -> pymupdf text extraction -> cache in `packet_pdfs` table -> dispatch packet-writer agent on WireClaw -> GLM 5.0 Turbo writes forward-looking preview article

**Key dependency:** `pymupdf` — if missing, PDFs download but extraction silently fails (all `pdf_error` results).

**Manual test:**
```bash
cd /opt/croton-news/rag
/opt/croton-news/venv/bin/python rag_tool.py fetch_agenda_packet EVENT_ID
```

## Jinja Filters

Registered in `app.py` after `app = Flask(...)`:

| Filter | Purpose |
|--------|---------|
| `from_json` | Parse JSON string in templates (used in `meeting.html` for `agenda_json`) |

## Service Management

```bash
systemctl restart croton-news   # Restart app
systemctl status croton-news    # Check status
journalctl -u croton-news -n 50 # View logs
```

App runs under **gunicorn** (1 worker × 8 threads, `MemoryMax=2G`) bound to
**127.0.0.1:3260** (localhost only); nginx proxies from 443. API keys for the
service live in `/opt/croton-news/secrets.env` (mode 600, loaded via
`EnvironmentFile=`) — rotate values there, then `systemctl restart croton-news`.
Never start `app.py` by hand: it fights the unit for the port.
