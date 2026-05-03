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

## Cron Jobs

All run from `/opt/croton-news/rag/` using the project venv.

| Time | Script | What |
|------|--------|------|
| 7:00 AM | `pipeline.py process-new` | Discover + process new ChampDS meetings |
| 7:15 AM | `boarddocs.py sync` | Sync BOE agendas/minutes from BoardDocs (via IPRoyal proxy) |
| 7:30 AM | `poll_boe.py --write` | Poll CHUFSD YouTube for new BOE videos |
| 8:00 AM | `story_miner.py scan --email` | Mine story ideas from recent meetings |
| Hourly | `auto_pipeline.py` | Upcoming meeting previews, placeholders, agenda cache |

Logs: `/var/log/croton-pipeline.log`, `/var/log/croton-boe.log`, `/var/log/croton-boarddocs.log`, `/var/log/croton-stories.log`, `/var/log/auto_pipeline.log`

## Phone Relay (Residential IP)

YouTube and some services block VPS IPs. These scripts run from the phone (Termux):

| Script | Purpose |
|--------|---------|
| `~/bin/boe-fetch` | Fetch YouTube auto-captions for BOE meetings via `youtube-transcript-api` |
| `~/bin/boarddocs-fetch` | Fetch BoardDocs agendas/minutes (alternative to VPS proxy route) |

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

# Restart the app
cd /opt/croton-news
fuser -k 3260/tcp; sleep 1
nohup /opt/croton-news/venv/bin/python app.py > /tmp/croton-news.log 2>&1 &

# Check logs
tail -50 /var/log/croton-pipeline.log
tail -50 /tmp/croton-news.log

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
