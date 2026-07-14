# WireClaw-side pipeline (versioned snapshots)

These files RUN on the WireClaw box (107.173.0.190), not the croton VPS.
This directory is the version-controlled copy — after editing here, deploy:

| File here | Deploys to (WireClaw box) |
|---|---|
| `enrich-transcripts.sh` | `/root/enrich-transcripts.sh` (cron 8:00 UTC) |
| `write-articles-via-agents.sh` | `/root/write-articles-via-agents.sh` |
| `croton-article-writer.CLAUDE.md` | `/root/wireclaw-cli/groups/croton-article-writer/CLAUDE.md` |
| `croton-article-editor.CLAUDE.md` | `/root/wireclaw-cli/groups/croton-article-editor/CLAUDE.md` |

Deploy with plain `cp` (or better: mv a copy into place — the shell scripts
must never be overwritten while running).

Pipeline: sync data from croton → enrich transcripts (speaker naming, incl.
"Unknown Speaker" caption transcripts, max 3 attempts each) → write articles
(writer agent, 2 attempts) → fact-check (editor agent, 1800s) → publish via
publish_article.py (venv) → index (chunks/FTS/embeddings). Failures email
ALERT_EMAIL via croton's rag/notify.py.

History: see AUDIT-2026-07-13.md. Key incidents encoded in the prompts:
caption transcripts mis-hearing "PVC" as "PBC" (headline), attribution risk
on diarization-free transcripts, strict-JSON output requirement, editor
900s timeout blocking publication, unscoped chunk DELETE wiping
minutes/article chunks.
