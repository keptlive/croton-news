"""
Article-writing agent for croton.news.

Writes journalism articles with access to:
1. Full meeting transcript/minutes (source material)
2. RAG search (cross-meeting context on same topics)
3. Web search via contextwire.dev (NY standards, comparisons)

Usage:
    python3 write_article.py <event_id>              # Write article for a meeting
    python3 write_article.py <event_id> --dry-run     # Print without saving
    python3 write_article.py <event_id> --model opus  # Choose model
    python3 write_article.py topic <topic_slug>       # Write topic wrap-up article
"""

import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

# LLM endpoints
ZAI_KEY = os.environ.get("ZAI_KEY", "")
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"

# Search API for external research
SEARCH_API_URL = "https://search.ourweb.ink/api/search"
SEARCH_API_KEY = "2e0a3ba74a3ea90c894fd23233d3592d53e491b287a6a62e"

# RAG search
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")


def web_search(query, max_results=5):
    """Search the web via contextwire.dev/search API."""
    try:
        url = f"{SEARCH_API_URL}?q={urllib.request.quote(query)}&profile=web&max_results={max_results}&format=json"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {SEARCH_API_KEY}",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", r.get("snippet", ""))[:300],
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def rag_search(query, limit=10):
    """Search our RAG database for cross-meeting context."""
    try:
        from search import rag_search as _rag_search
        results = _rag_search(query, limit=limit)
        return [{
            "date": r["date"],
            "committee": r["committee"],
            "speaker": r.get("speaker"),
            "content": r["content"][:400],
            "doc_type": r["doc_type"],
        } for r in results]
    except Exception as e:
        return [{"error": str(e)}]


def load_transcript(event_id):
    """Load full transcript for a meeting."""
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_meeting(db, event_id=None, date=None, committee=None):
    """Load meeting record from rag.db."""
    if event_id:
        return db.execute(
            "SELECT * FROM meetings WHERE event_id = ?", (event_id,)
        ).fetchone()
    elif date and committee:
        return db.execute(
            "SELECT * FROM meetings WHERE date = ? AND committee = ?", (date, committee)
        ).fetchone()
    return None


def call_llm(system_prompt, user_prompt, model="claude-sonnet-4-20250514", max_tokens=4000):
    """Call LLM via z.ai (Anthropic-compatible API)."""
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode()

    req = urllib.request.Request(ZAI_URL, data=payload, headers={
        "Content-Type": "application/json",
        "x-api-key": ZAI_KEY,
        "anthropic-version": "2023-06-01",
    }, method="POST")

    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    return data["content"][0]["text"]


def gather_context(transcript, topics):
    """Gather cross-meeting and external context for article writing."""
    context = {"rag_results": [], "web_results": []}

    # Extract key topics from transcript for research
    if not topics:
        # Simple extraction: most discussed subjects
        topics = []
        if transcript:
            text = transcript.get("full_text", "")[:2000]
            # Will be extracted by the LLM itself via tool calls

    # RAG: find related coverage from other meetings
    for topic in topics[:3]:
        results = rag_search(topic, limit=5)
        context["rag_results"].extend(results)

    # Web: search for external context/comparisons
    for topic in topics[:2]:
        query = f"{topic} New York village municipality"
        results = web_search(query, max_results=3)
        context["web_results"].extend(results)

    return context


def write_meeting_article(event_id, model="claude-sonnet-4-20250514", dry_run=False):
    """Write an article for a specific meeting."""
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    # Load transcript
    transcript = load_transcript(event_id)
    if not transcript:
        print(f"No transcript found for event {event_id}")
        return

    # Load existing meeting record
    meeting = load_meeting(db, event_id=event_id)
    date = transcript.get("date", "")
    committee = transcript.get("title", "")

    # Get full text (with speaker names)
    full_text = transcript.get("full_text", "")
    speaker_map = transcript.get("speaker_map", {})

    # Extract top topics from existing topic_threads
    topic_chunks = db.execute("""
        SELECT DISTINCT t.name FROM topic_threads t
        JOIN topic_mentions tm ON tm.topic_id = t.id
        JOIN chunks c ON c.id = tm.chunk_id
        WHERE c.doc_id = ?
    """, (str(event_id),)).fetchall()
    topics = [r[0] for r in topic_chunks]

    print(f"Meeting: {committee} ({date}), event {event_id}")
    print(f"Transcript: {transcript.get('word_count', 0)} words, {len(transcript.get('utterances', []))} utterances")
    print(f"Topics: {', '.join(topics) if topics else 'none identified'}")
    print(f"Gathering context...")

    # Gather cross-meeting and external context
    context = gather_context(transcript, topics)
    print(f"  RAG results: {len(context['rag_results'])}")
    print(f"  Web results: {len(context['web_results'])}")

    # Build a timestamp index so the LLM can reference quotes by time
    utterance_index = []
    for u in transcript.get("utterances", []):
        speaker = u.get("speaker", "Unknown")
        if speaker_map:
            num = speaker.replace("Speaker ", "")
            if num in speaker_map:
                speaker = speaker_map[num]
        ts = int(u.get("start", 0))
        text_preview = u.get("text", "")[:80]
        utterance_index.append(f"[{ts}s] {speaker}: {text_preview}")

    # Build the article prompt — adjust for committee type
    is_boe = "Board of Education" in (committee or "")

    if is_boe:
        system = """You are a local news journalist covering the Croton-Harmon school district (CHUFSD) in Croton-on-Hudson, NY.
You write clear, engaging education journalism that makes school board decisions accessible to parents and residents.

Style guidelines:
- Lead with what matters most to families and taxpayers, not procedural details
- Use direct quotes from the meeting when they're compelling
- Provide context: why does this matter, what's the history, what happens next
- Name speakers by their role and name (e.g., "Superintendent Brendan Walker")
- Mention specific dollar amounts, vote counts, enrollment figures, and dates
- Keep it 400-800 words
- Write a compelling headline (not clickbait, but interesting)
- No AI disclaimers, no "this article was generated" notices
- Write in past tense for events, present tense for ongoing situations

CRITICAL — Speaker attribution:
Many speakers in this transcript are labeled "Unknown Speaker" because the source
is YouTube auto-captions without speaker identification.
- When quoting an Unknown Speaker, attribute as "a board member said," "one speaker noted,"
  "an audience member commented," "a parent asked," etc. based on context.
- NEVER fabricate or guess a speaker's name. Only use proper names that appear in the
  transcript's speaker labels or are explicitly mentioned in the text.
- If the transcript says "Superintendent Walker" or "President Chaudhuri," you may use those names.

IMPORTANT — Quote timestamps:
When you use a direct quote, tag it with the timestamp from the transcript like this:
  "Quote text here," said Speaker Name. {{quote:SECONDS}}
where SECONDS is the start time in seconds from the timestamp index provided.
Only include quotes that have a matching timestamp. This lets us link to the source video."""
    else:
        system = """You are a local news journalist covering Croton-on-Hudson, NY village government.
You write clear, engaging civic journalism that makes local government accessible to residents.

Style guidelines:
- Lead with what matters most to residents, not procedural details
- Use direct quotes from the meeting when they're compelling
- Provide context: why does this matter, what's the history, what happens next
- Name speakers by their role and name (e.g., "Village Manager Bryan Healy")
- Mention specific dollar amounts, vote counts, and dates
- Keep it 400-800 words
- Write a compelling headline (not clickbait, but interesting)
- No AI disclaimers, no "this article was generated" notices
- Write in past tense for events, present tense for ongoing situations

IMPORTANT — Quote timestamps:
When you use a direct quote, tag it with the timestamp from the transcript like this:
  "Quote text here," said Speaker Name. {{quote:SECONDS}}
where SECONDS is the start time in seconds from the timestamp index provided.
This lets us embed a video clip of the speaker at that moment."""

    user_parts = []
    user_parts.append(f"Write a news article about the {committee} meeting on {date}.\n")

    # Include official meeting agenda if available
    agenda_json = None
    if meeting:
        agenda_json = meeting.get("agenda_json") if isinstance(meeting, dict) else meeting["agenda_json"] if "agenda_json" in meeting.keys() else None
    if agenda_json:
        try:
            agenda_items = json.loads(agenda_json) if isinstance(agenda_json, str) else agenda_json
            if agenda_items:
                agenda_lines = []
                def _walk(items, depth=0):
                    for item in items:
                        prefix = "  " * depth + "- "
                        agenda_lines.append(f"{prefix}{item.get('title', '')}")
                        _walk(item.get("children", []), depth + 1)
                _walk(agenda_items)
                user_parts.append("## Official Meeting Agenda\n")
                user_parts.append("\n".join(agenda_lines[:50]))
                user_parts.append("\nUse this agenda to structure your article and ensure all key items are covered.\n")
        except (json.JSONDecodeError, TypeError):
            pass

    # Source transcript (truncated if too long)
    transcript_text = full_text[:12000] if len(full_text) > 12000 else full_text
    user_parts.append(f"## Full Meeting Transcript\n\n{transcript_text}\n")

    # Timestamp index for quote referencing
    if utterance_index:
        # Sample every few utterances to keep it manageable
        sampled = utterance_index[::3][:200]
        user_parts.append("## Timestamp Index (for quote references)\n")
        user_parts.append("\n".join(sampled))
        user_parts.append("")

    # Cross-meeting context
    if context["rag_results"]:
        user_parts.append("## Related Coverage From Other Meetings\n")
        for r in context["rag_results"][:8]:
            if "error" not in r:
                speaker = f" ({r['speaker']})" if r.get("speaker") else ""
                user_parts.append(f"- [{r['date']}] {r['committee']}{speaker}: {r['content'][:200]}")
        user_parts.append("")

    # External context
    if context["web_results"]:
        user_parts.append("## External Context (from web research)\n")
        for r in context["web_results"]:
            if "error" not in r:
                user_parts.append(f"- {r['title']}: {r['snippet'][:200]}")
                user_parts.append(f"  Source: {r['url']}")
        user_parts.append("")

    user_parts.append("""
## Output Format

HEADLINE: <your headline>
QUICK_SUMMARY: <1-2 sentence summary for search results>
KEY_ACTIONS:
- Action or decision 1 (include vote counts, dollar amounts)
- Action or decision 2
- ...up to 8 key items, most important first

ARTICLE:
<your full article, with {{quote:SECONDS}} after each direct quote>""")

    user_prompt = "\n".join(user_parts)

    print(f"Calling {model} ({len(user_prompt)} chars input)...")
    response = call_llm(system, user_prompt, model=model)

    # Parse response
    headline = ""
    quick_summary = ""
    key_actions = ""
    article = ""

    lines = response.split("\n")
    section = None
    for line in lines:
        if line.startswith("HEADLINE:"):
            headline = line[9:].strip()
            section = None
        elif line.startswith("QUICK_SUMMARY:"):
            quick_summary = line[14:].strip()
            section = None
        elif line.startswith("KEY_ACTIONS:"):
            section = "key_actions"
        elif line.startswith("ARTICLE:"):
            section = "article"
        elif section == "key_actions":
            key_actions += line + "\n"
        elif section == "article":
            article += line + "\n"

    key_actions = key_actions.strip()
    article = article.strip()

    print(f"\n{'='*60}")
    print(f"HEADLINE: {headline}")
    print(f"QUICK_SUMMARY: {quick_summary}")
    print(f"KEY_ACTIONS:\n{key_actions}")
    print(f"ARTICLE ({len(article)} chars):")
    print(article[:500] + "..." if len(article) > 500 else article)
    print(f"{'='*60}")

    if dry_run:
        print("\n[DRY RUN — not saved]")
    else:
        # Save to meetings table
        if meeting:
            db.execute("""
                UPDATE meetings SET
                    headline = ?, quick_summary = ?, complete_summary = ?,
                    article = ?,
                    article_model = ?, article_generated_at = datetime('now')
                WHERE event_id = ?
            """, (headline, quick_summary, key_actions, article, model, event_id))
        else:
            db.execute("""
                INSERT OR REPLACE INTO meetings
                    (date, committee, event_id, headline, quick_summary, article,
                     has_transcript, has_video, has_audio,
                     article_model, article_generated_at,
                     word_count, speaker_count)
                VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, ?, datetime('now'), ?, ?)
            """, (date, committee, event_id, headline, quick_summary, article,
                  model, transcript.get("word_count"), transcript.get("speaker_count")))
        db.commit()
        print(f"\nSaved to meetings table.")

    db.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    event_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    model = "claude-sonnet-4-20250514"
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        model_name = sys.argv[idx + 1]
        model_map = {
            "opus": "claude-opus-4-20250514",
            "sonnet": "claude-sonnet-4-20250514",
            "glm": "glm-5-turbo",
        }
        model = model_map.get(model_name, model_name)

    if event_id == "topic":
        topic_slug = sys.argv[2]
        print(f"Topic article writing not yet implemented for: {topic_slug}")
        # TODO: write_topic_article(topic_slug, model, dry_run)
    else:
        write_meeting_article(event_id, model=model, dry_run=dry_run)


if __name__ == "__main__":
    main()
