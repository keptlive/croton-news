# croton.news

AI-powered hyperlocal news for Croton-on-Hudson, NY. Meeting videos and
official documents go in; verified, source-linked journalism comes out.

## Documentation map

| Doc | What it covers |
|---|---|
| **CLAUDE.md** | The operating manual: architecture, cron jobs, publish gate, reliability/alerting, runbooks, phone relay, service management |
| **AUDIT-2026-07-13.md** | The full 2026-07-13/14 audit: every defect found (security, UX, content, pipeline) and its fix, with commits |
| **PIPELINE-PLAN.md** | Trace-audit status board + plans for open items + standing backlog |
| **ops/** | Versioned deployment artifacts (nginx vhost, systemd unit, logrotate, crontabs) + deploy instructions |
| **wireclaw/** | The article pipeline that runs on the WireClaw box: scripts + agent prompts (versioned copies; deploy notes in its README) |
| **rag/croton-code/README.md** | Village code corpus build notes |
| **research/**, **stories/** | Source material for history features (content, not ops) |

## The processes, at a glance

**Two machines**: the croton VPS (site + data pipelines) and the WireClaw box
(AI agents). All croton cron jobs run through `rag/run_job.sh` → history in
`rag/job_runs.db`, failure emails to ALERT_EMAIL.

| When (UTC) | Where | Process |
|---|---|---|
| 4:30 | croton | Community calendar refresh (`scrapers/update-calendar.sh`) |
| 5:00 | croton | All-DB backups (keep 7) |
| 5:40 | WireClaw | Offsite backup pull (gzipped, keep 3) |
| 6:00 | croton | Daily pipeline: discover → agendas → minutes → transcribe (Deepgram+keyterms) → ingest → index (minutes/articles chunks, FTS, embeddings, entity verification) |
| 6h | croton | Community news RSS scrapers |
| 7:15 | croton | BoardDocs sync + `sync-local` (BOE minutes) |
| 7:30 | croton | BOE YouTube poll (audio-first → Deepgram; captions last resort) |
| :05 hourly | croton | Upcoming agendas + preview dispatch |
| 8:00 | WireClaw | Article pipeline: enrich speakers (batch 6, minutes-attendance rules) → write (fresh session, 2 attempts × 2 gate passes) → editor fact-check → **publish gate** → photos → index. Per-run archives in `/root/croton-pipeline-runs/` |
| 9:00 | croton | Watchdog: outcome checks + 14-day article quality sweep + consolidated email (Monday digest when healthy) |

**The publish gate** (`rag/validate_article.py`): no article publishes unless
every quote matches its transcript speaker verbatim, and every name and
dollar figure traces to a source document. Blocked drafts retry immediately
with the violation report in the writer's prompt. See CLAUDE.md § Publish Gate.

**Manual procedures** (all emailed to you when needed):
- Phone relay (YouTube/BoardDocs blocks): CLAUDE.md § Phone Relay
- Packet backfill: `venv/bin/python rag/rag_tool.py fetch_agenda_packet EVENT --max-pdfs 60 --force`
- Check any article: `venv/bin/python rag/validate_article.py --published <id>`
- Restart the app: `systemctl restart croton-news` (never run app.py by hand)

**Standing constraints**: never `git push` until the credentials purge is done
(see AUDIT log); key rotation is owner-only; the article model (GLM via z.ai)
is fixed by owner decision.
