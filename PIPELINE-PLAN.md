# Pipeline Quality Plan — from the 2026-07-14 trace-mining audit

Source: analysis of 4,500+ agent-trace events (writer / editor / enricher
sessions, June 5 – July 14) plus the cross-committee fact-check and
transcription audit. Status legend: ✅ fixed (commit) · 🔲 open (plan below).

## Status board

| # | Finding | Status |
|---|---------|--------|
| 1a | Writer runs die on a single API 529, no backoff | ✅ 2-attempt retry + 300s 529-aware backoff (`54987d4`) |
| 1b | Enricher sweeps blind-retry into the same outage; no auth-vs-outage distinction | 🔲 open — see Plan A |
| 2 | transcript-1165 re-enriched 34 days straight (unresolvable voices requalify daily) | ✅ 3-attempt cap, stamps only on real output (`1b85f85`) |
| 3a | sqlite3 CLI missing + ro-mount journal errors → 88 full-DB copies to /tmp | ✅ canonical `mode=ro&immutable=1` snippet + schemas in all 3 prompts (`54987d4`) |
| 3b | Install sqlite3 in the agent container image | 🔲 open — see Plan B |
| 3c | Editor's mandated example queries used nonexistent `meeting_id` column | ✅ 5 queries fixed to `doc_id` (`54987d4`) |
| 4 | Standard data re-hunted every run (44-66 meeting/entity queries per session) | 🔶 schemas injected; meeting row + attendance + roster injection 🔲 — see Plan C |
| 5a | Editor timed out with nothing saved; subagent consumed 14 min | ✅ time-budget + early-save mandate in prompt (`9149c16`) |
| 5b | Enforce skeleton-output-first; time-box or drop editor subagents | 🔲 open — see Plan D |
| 5c | wireclaw.yaml timeout 600000ms vs script timeout 1800s mismatch | 🔲 open — trivial: set wireclaw.yaml `timeout: 1800000` for writer+editor |
| 6 | Persistent 5-week group sessions: 33-64M cached tokens, stale-context leakage, memory-reconstruction of articles | 🔶 one-article-per-request rule (`e2fcd5e`); per-run session isolation 🔲 — see Plan E |
| 7 | Hand-built JSON in heredocs (enricher: 59 heredoc writes, several malformed) | 🔶 save-as-you-go + Write-tool rules; `save_output.py` helper 🔲 — see Plan F |
| 8 | 4-pass enrichment overruns timeout on large yt transcripts | ✅ staged writes + reduced 2-pass yt mode in prompt (`54987d4`) |
| — | "Clarkstown" opener in meeting-152 transcript | ✅ verified benign: caption garble/verbal slip; right channel/date; draft article clean |
| — | Gate loop: ssh stdin ate the meeting queue; feedback not preloaded across runs | ✅ stdin guards + report preload (`54987d4`) |

## Plans for open items

### Plan A — 529/auth triage (finding 1b)
1. `enrich-transcripts.sh`: after each enricher agent run with no output,
   grep the last log window for `API Error: 529`; on hit, run the canary:
   a 10-token request to `$ANTHROPIC_BASE_URL/v1/messages` with the same key.
   - canary OK → key works ⇒ transient; sleep 300 and retry that transcript once.
   - canary 401/403/529 → auth/quota problem ⇒ **abort the whole sweep**
     (don't burn the remaining batch), email
     "z.ai key problem (canary failed) — check rotation/quota" via notify.py.
2. Same canary in `write-articles-via-agents.sh` before starting the meeting
   loop (fail fast with a precise email instead of 3 meetings × 2 passes of
   containers).
   Effort: ~30 lines of bash. Test by temporarily pointing BASE_URL at an
   invalid path.

### Plan B — sqlite3 in the container image (finding 3b)
Add `sqlite3` to `container/Dockerfile` (wireclaw-agent image), rebuild via
`./container/build.sh` (note the buildkit cache caveat in wireclaw CLAUDE.md:
prune builder first). Low urgency now that the python snippet is standard;
do it with the next scheduled image rebuild, not as its own event.
Also: remove duplicated `readonly: true` key in croton-article-writer
wireclaw.yaml.

### Plan C — inject per-meeting context into prompts (finding 4)
In `write-articles-via-agents.sh` and `enrich-transcripts.sh`, before
spawning the agent, fetch once over ssh and append to the prompt:
1. the meeting row (id, date, committee, event_id, has_minutes, chunk count)
2. the minutes attendance block (first 800 chars of minutes_text)
3. the committee's entity roster (top 15 by mention_count)
Expected effect (from trace counts): −5–10 queries and 2–3 minutes per run,
and removes the failure mode where the agent skips the lookup entirely.
Effort: one helper `rag/prompt_context.py MEETING_ID` on croton that prints
the block; scripts interpolate its output.

### Plan D — editor output discipline (finding 5b)
1. Prompt line (editor CLAUDE.md): "Your FIRST tool call after reading the
   article: create checked-{id}.json with editor_result IN_PROGRESS; update
   it after each verification step." (early-save is mandated but not
   enforced as first-call.)
2. Ban or time-box subagents: "Do not spawn subagents for transcripts under
   300 chunks; if you spawn one, cap it at 5 minutes of work."
3. Post-run check in the script: if checked-{id}.json exists with
   IN_PROGRESS, treat as editor-incomplete (retry pass) rather than publish.

### Plan E — per-run session isolation (finding 6)
Investigate wireclaw batch-agent session handling: scheduled tasks support
`context_mode: isolated`; determine the batch-agent equivalent (flag or a
fresh session id per invocation). Wins: no stale-meeting leakage, no
article-from-memory reconstruction, ~150k fewer cached input tokens per
call. Depends on findings 3a/4 landing first (session "memory" of
workarounds becomes unnecessary). Verify cost impact before enabling —
cache reads are cheap; correctness is the driver, not spend.

### Plan F — `save_output.py` helper (finding 7)
Ship in each group dir (mounted at /workspace/group):
`save_output.py <name> --json-file draft.json` → validates required keys,
strict-parses, rejects raw control chars, writes atomically. Prompt line:
"never emit JSON inside python -c/heredoc strings; build a dict and
json.dump it, or use save_output.py". Primary beneficiary is the enricher
(59 heredoc writes, several malformed).

## Standing backlog (not from traces)
- Wire `validate_speakers.py --event <id>` (report + notify.py email) into enrich-transcripts.sh right after the re-ingest step — deferred 2026-07-14 because the loop was running (never edit a running bash script); edit the repo wireclaw/ copy, deploy by atomic mv when idle. Watchdog daily sweep already covers detection.
- validate_speakers sweep found label-only names on 2025 meetings (Toone/Nachtaler/Braddick/Gallelli/Skrelja, meetings 10/11/14/22/27/28/48/51/82) — no article impact (checked); most stem from exec-session-only minutes or minutes that omit staff. Triage when minutes coverage improves; the gate's name-attendance rule blocks these names from future articles regardless. NOTE meeting 48/51: Nachtaler labeled speaking Oct 2025, pre-oath — labels likely wrong but no article impact.
- Meeting 37 (event 1105) minutes OCR is corrupted ("ayor Pugh", Slippen's attendance line missing though the 4-0 vote implies she was present) — re-fetch from BoardDocs; the transcript label is probably RIGHT and the minutes wrong.
- Fabrication-echo ban (writer may not source doc_type='article' chunks) is REVISABLE once all published articles are gate-verified — see CLAUDE.md § Publish Gate for the condition
- packet_pdfs is nickname-keyed — attachment name collisions silently drop one PDF per collision (events 1174/1175 each lost 1 of ~38); re-key on source_url
- scraped community news (data/croton.db, 1,679 items) is write-only — surface via /api/community-news + homepage section, or retire the 6-hour scrapers job
- write_from_minutes.py path for pre-2026 BOE meetings (ids 103, 106) — must route through the publish gate before use
- OCR backlog: 235 scanned packet pages (rag_tool.py ocr-scanned, extend to pdf_no_text)
- history.db: 694 chunks unembedded, 5,850 orphan embeddings, dawson_westchester_revolution_1886.txt never ingested
- 8 dead {{photo}} refs in articles 16/31/45/66/67/69 (source videos pruned — strip tags); insert_photos.py not in daily path
- ecode360 snapshot current through 2025-06-01 — 16 newer local laws unconsolidated; re-scrape + rebuild_code_db.py
- Watchdog canary check (overlaps Plan A) — distinguish auth vs outage in alert emails
- Entities staleness pass (Eva Thaddeus-class rows: role + last-seen verification)
- One-time re-ingest of ~1,100 old garbled/vote-dropped chunks (after keyterm+vote fixes; needs a re-chunk sweep over transcripts with `ingest.py` skip-guard lifted)
- Topic/feature writer revival (owner-approved, after meeting articles are stable)
- Comments moderation default (owner decision pending)
- Key rotation → git history purge → resume GitHub pushes (owner)
- BOE audio route needs yt-dlp cookies maintenance on WireClaw (audio-first now depends on it)
