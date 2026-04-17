"""
write_article.py — croton.news article writer (v2, evidence-grounded).

Pipeline:
  1. Extract specific topics from the transcript via fast LLM pass.
  2. Build a quote whitelist of substantive, attributable utterances
     (with timestamps from the transcript).
  3. For each topic, RAG-search rag.db for prior coverage, group by meeting,
     and build a numbered reference pool with /article/{id} links.
  4. Call Opus with strict instructions:
       - Quotes only from the pool, marked {{quote:TS}}
       - Cross-meeting facts cited with [R#] markers
  5. Post-process:
       - Verify every quote against the pool by timestamp + normalized text.
         Flag mismatches inline as [unverified].
       - Replace [R#] with markdown links and append a References section.
  6. Save to meetings table.

Usage:
    python3 write_article.py <event_id>
    python3 write_article.py <event_id> --dry-run
    python3 write_article.py <event_id> --model opus|sonnet|claude-opus-4-5
"""

import json
import os
import re
import sqlite3
import sys
import urllib.request

# Load .env if present (for GEMINI_API_KEY etc)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

RAG_DB = os.path.join(os.path.dirname(__file__), "rag.db")
TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

# LLM endpoints
ZAI_KEY = "6b4962b3c6ec42df91c76542f1efddcf.cJfCVb6J14qGABNO"
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"

# Search API (kept for optional external context)
SEARCH_API_URL = "https://search.ourweb.ink/api/search"
SEARCH_API_KEY = "2e0a3ba74a3ea90c894fd23233d3592d53e491b287a6a62e"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

DEFAULT_MODEL = "claude-opus-4-5"
TOPIC_MODEL = "claude-sonnet-4-5"

# Procedural utterances we never want to quote
PROCEDURAL_PHRASES = (
    "all in favor", "second the motion", "call to order", "i so move",
    "any opposed", "motion carries", "we are adjourned", "stand adjourned",
    "good evening everyone", "good morning everyone", "thank you very much",
    "i make a motion", "all in favor signify", "any nays",
)


# ── LLM ──────────────────────────────────────────────────────────────

def call_llm(system_prompt, user_prompt, model=DEFAULT_MODEL, max_tokens=8000):
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
    resp = urllib.request.urlopen(req, timeout=180)
    data = json.loads(resp.read())
    return data["content"][0]["text"]


# ── Loaders ──────────────────────────────────────────────────────────

def load_transcript(event_id):
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_meeting(db, event_id):
    return db.execute(
        "SELECT * FROM meetings WHERE event_id = ? ORDER BY date DESC LIMIT 1",
        (event_id,),
    ).fetchone()


# ── Quote pool ───────────────────────────────────────────────────────

def normalize_quote(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def deflutter(s):
    # Collapse repeated consecutive words: "you you know" → "you know"
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", s)
    return s


def resolve_speaker(raw, speaker_map):
    speaker = raw or "Unknown"
    if speaker_map:
        num = speaker.replace("Speaker ", "")
        if num in speaker_map:
            return speaker_map[num]
    return speaker


def build_quote_pool(transcript, max_quotes=80, min_chars=60, max_chars=420):
    speaker_map = transcript.get("speaker_map") or {}
    pool = []
    for u in transcript.get("utterances", []):
        text = (u.get("text") or "").strip()
        if len(text) < min_chars or len(text) > max_chars:
            continue
        lc = text.lower()
        if any(p in lc for p in PROCEDURAL_PHRASES):
            continue
        # Skip utterances that don't end with sentence-ending punctuation.
        # This prevents the LLM from lifting mid-sentence fragments as "quotes".
        if not text.rstrip().endswith((".", "!", "?", '"', "'")):
            continue
        speaker = resolve_speaker(u.get("speaker", ""), speaker_map)
        # Skip pure "Speaker N" labels with no name
        speaker_label = speaker
        if re.match(r"^speaker\s*\d*$", speaker.lower()):
            speaker_label = "Unknown speaker"
        ts = int(u.get("start", 0))
        pool.append({
            "ts": ts,
            "speaker": speaker_label,
            "text": text,
            "norm": normalize_quote(text),
        })
    # Prefer longer (often more substantive) quotes
    pool.sort(key=lambda x: -len(x["text"]))
    pool = pool[:max_quotes]
    pool.sort(key=lambda x: x["ts"])
    return pool


def build_photo_pool(transcript, quote_pool, max_photos=6):
    """Select 4-6 moments that would make good illustrative speaker-frame photos.

    Criteria: substantive utterances spread across the meeting timeline,
    avoiding the first and last 5% (openings/adjournments). Prefer named
    speakers when available, but fall back to unnamed speakers when the
    meeting has no speaker_map (common case) — the photo still illustrates
    what was happening on camera at that moment.
    """
    utterances = transcript.get("utterances", [])
    if not utterances:
        return []
    total_ts = max((u.get("end", 0) or u.get("start", 0)) for u in utterances)
    if total_ts <= 0:
        return []
    head_buffer = total_ts * 0.05
    tail_buffer = total_ts * 0.05
    # Prefer named speakers; fall back to all quotes if no named ones in the window.
    named = [q for q in quote_pool
             if q["speaker"] != "Unknown speaker"
             and head_buffer <= q["ts"] <= total_ts - tail_buffer]
    if named:
        candidates = named
    else:
        candidates = [q for q in quote_pool
                      if head_buffer <= q["ts"] <= total_ts - tail_buffer]
    if not candidates:
        return []
    candidates.sort(key=lambda x: x["ts"])
    if len(candidates) <= max_photos:
        return candidates
    # Evenly-spaced sampling across the meeting timeline.
    step = len(candidates) / max_photos
    return [candidates[int(i * step)] for i in range(max_photos)]


# ── Topic extraction ─────────────────────────────────────────────────

def extract_topics(transcript, model=TOPIC_MODEL):
    text = transcript.get("full_text", "")[:18000]
    if not text:
        return []
    system = (
        "You analyze transcripts of local-government meetings in Croton-on-Hudson, NY "
        "and extract the specific substantive topics discussed. Output 5-8 short topic "
        "phrases, one per line, no numbering. Focus on concrete subjects (e.g. "
        "'body-worn cameras', 'Half Moon Bay Bridge reconstruction', 'fee schedule for "
        "recreation department'). Avoid generic phrases like 'budget' or 'discussion'."
    )
    user = f"Transcript excerpt:\n\n{text}\n\nList 5-8 specific topics, one per line."
    try:
        out = call_llm(system, user, model=model, max_tokens=400)
    except Exception as e:
        print(f"  topic extraction failed: {e}", file=sys.stderr)
        return []
    topics = []
    for line in out.split("\n"):
        s = line.strip(" -•*\t")
        if 4 <= len(s) <= 100:
            topics.append(s)
    return topics[:8]


# ── Reference gathering ──────────────────────────────────────────────

def gather_references(topics, current_event_id, refs_per_topic=4, total_cap=20):
    try:
        from search import rag_search
    except Exception as e:
        print(f"  rag_search import failed: {e}", file=sys.stderr)
        return []

    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    refs = []
    seen_meetings = set()
    cur = str(current_event_id)

    for topic in topics:
        try:
            hits = rag_search(topic, limit=25)
        except Exception as e:
            print(f"  rag_search('{topic}') failed: {e}", file=sys.stderr)
            continue

        topic_refs = 0
        for h in hits:
            raw_doc = h.get("doc_id") or ""
            # Strip "-transcript" / "-minutes" suffixes that some chunks carry
            clean_doc = raw_doc.split("-")[0] if raw_doc else ""
            if not clean_doc or clean_doc == cur:
                continue
            chunk_date = h.get("date") or ""
            mtg = db.execute(
                "SELECT id, headline, quick_summary, date, committee, article_model "
                "FROM meetings WHERE event_id = ? "
                "ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1",
                (clean_doc, chunk_date or "1970-01-01"),
            ).fetchone()
            if not mtg or not mtg["id"]:
                continue
            key = mtg["id"]
            if key in seen_meetings:
                continue
            seen_meetings.add(key)
            refs.append({
                "topic": topic,
                "meeting_id": mtg["id"],
                "doc_id": clean_doc,
                "date": mtg["date"],
                "committee": mtg["committee"],
                "headline": mtg["headline"] or "",
                "quick_summary": mtg["quick_summary"] or "",
                "snippet": (h.get("content") or "")[:320],
                "speaker": h.get("speaker") or "",
                "url": f"/article/{mtg['id']}",
            })
            topic_refs += 1
            if topic_refs >= refs_per_topic:
                break
            if len(refs) >= total_cap:
                break
        if len(refs) >= total_cap:
            break

    db.close()
    return refs


# ── Prompt formatting ────────────────────────────────────────────────

def format_quote_pool(pool):
    lines = [
        "## Approved Quote Pool",
        "These are the ONLY direct quotes you may use. Quote them exactly. After "
        "each quote, append `{{quote:TS}}` where TS is the timestamp shown.",
        "",
    ]
    for q in pool:
        lines.append(f"[{q['ts']}s] {q['speaker']}: \"{q['text']}\"")
    return "\n".join(lines)


def format_photo_pool(pool, event_id):
    if not pool:
        return ""
    lines = [
        "## Approved Photo Pool",
        f"You MAY illustrate the article with 2-4 speaker-frame photos from this "
        f"meeting. Place each photo marker on its own line between paragraphs at "
        f"a narrative transition, using EXACTLY this format (where EVENT_ID is "
        f"always `{event_id}` for this meeting):",
        "",
        f"    {{{{photo:{event_id}:<TS>:<CAPTION>}}}}",
        "",
        f"Example: `{{{{photo:{event_id}:462:Mayor Pugh opens discussion on the "
        f"cannabis application.}}}}`",
        "",
        "The <TS> MUST be one of the timestamps listed below (integer seconds, "
        "no 's' suffix). The <CAPTION> is a short descriptive line (5-15 words) "
        "identifying what's happening in the frame. Do NOT use timestamps that "
        "aren't in this list.",
        "",
        f"Available photo timestamps for event {event_id}:",
    ]
    for q in pool:
        lines.append(f"- TS={q['ts']} — {q['speaker']} (context: {q['text'][:140]})")
    return "\n".join(lines)


def format_references(refs):
    if not refs:
        return ""
    lines = [
        "## Reference Pool — prior croton.news coverage",
        "When stating any cross-meeting fact (history, prior decisions, ongoing "
        "initiatives), cite the relevant entry below using `[R#]` markers in your "
        "article. Each [R#] will be rendered as a clickable link to the source.",
        "",
    ]
    for i, r in enumerate(refs, 1):
        lines.append(
            f"[R{i}] ({r['date']} {r['committee']}) {r['headline'] or r['quick_summary'][:80]}"
        )
        if r["speaker"]:
            lines.append(f"     speaker: {r['speaker']}")
        lines.append(f"     topic: {r['topic']}")
        lines.append(f"     snippet: {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


# ── Verification ─────────────────────────────────────────────────────

QUOTE_RE = re.compile(r'"([^"\n]{10,500}?)"\s*[^{]*?\{\{quote:(\d+)s?\}\}')


def verify_quotes(article, quote_pool):
    by_ts = {q["ts"]: q for q in quote_pool}
    issues = []
    verified = []

    def _unverified(text):
        # Short unverified phrases: strip quotation marks and the marker entirely
        # (they're common terminology the LLM shouldn't have quoted).
        # Long unverified quotes: keep text + [unverified] tag (author should notice).
        word_count = len(text.split())
        if word_count < 12:
            return text
        return f'"{text}" [unverified]'

    def replace(m):
        text = m.group(1)
        ts = int(m.group(2))
        norm = normalize_quote(text)
        norm_df = deflutter(norm)
        if ts not in by_ts:
            issues.append(f"  ✗ ts={ts} not in pool: \"{text[:80]}\"")
            return _unverified(text)
        pool_norm = by_ts[ts]["norm"]
        pool_df = deflutter(pool_norm)
        # Accept if normalized substring (with or without stutters) or strong overlap
        if norm and (
            norm in pool_norm or pool_norm in norm
            or norm_df in pool_df or pool_df in norm_df
            or _fuzzy_overlap(norm_df, pool_df) > 0.85
        ):
            verified.append(ts)
            return m.group(0)
        issues.append(
            f"  ✗ ts={ts} mismatch\n     article: \"{text[:120]}\"\n     pool   : \"{by_ts[ts]['text'][:200]}\""
        )
        return _unverified(text)

    cleaned = QUOTE_RE.sub(replace, article)
    return cleaned, {"verified": verified, "issues": issues}


def _fuzzy_overlap(a, b):
    if not a or not b:
        return 0.0
    aw = set(a.split())
    bw = set(b.split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(len(aw), len(bw))


REF_RE = re.compile(r"\[R(\d+)\]")


def link_references(text, refs, append_list=False):
    used = set()

    def replace(m):
        n = int(m.group(1))
        if 1 <= n <= len(refs):
            used.add(n)
            r = refs[n - 1]
            label = f"{r['committee']} {r['date']}"
            return f"[{label}]({r['url']})"
        return m.group(0)

    linked = REF_RE.sub(replace, text)
    if append_list and used:
        linked += "\n\n---\n\n**References used in this article:**\n"
        for n in sorted(used):
            r = refs[n - 1]
            head = r["headline"] or r["quick_summary"][:80] or r["committee"]
            linked += f"\n- [{r['committee']} — {r['date']}]({r['url']}) · {head}"
    return linked, used


# ── Main writer ──────────────────────────────────────────────────────

def parse_response(response):
    headline = quick_summary = ""
    key_actions = ""
    article = ""
    section = None
    for line in response.split("\n"):
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
    return headline, quick_summary, key_actions.strip(), article.strip()


SYSTEM_VILLAGE = """You are a senior local news journalist for croton.news covering the Village of Croton-on-Hudson, NY. You write clear, evidence-grounded civic journalism that makes local government accessible to residents — with the depth and craft of a Hudson Valley weekly, not a bureaucratic minute-taking bot.

YOUR JOB: write a compelling 900-1200 word article that a resident would actually want to read. Lead with the stakes for residents. Use scene, character, and consequence — not just procedural decisions. Every factual claim must be grounded in the transcript, quote pool, or reference pool.

NON-NEGOTIABLE RULES:

1. QUOTES — use 4-6 direct quotes per article, but ONLY verbatim from the pool:
   - You SHOULD include 4-6 compelling direct quotes — they are essential for
     making the article come alive. But every quote MUST come from the
     "Approved Quote Pool" below.
   - Every quote MUST be a COMPLETE SENTENCE or COMPLETE THOUGHT. Never end
     a quote mid-sentence. If the pool entry ends with an incomplete phrase
     (e.g. "…This strip is really" or "…there will be ongoing"), DO NOT lift
     that fragment — either quote a complete earlier sentence from the same
     entry, or paraphrase. A quote must read as a finished statement on its own.
   - Copy the quoted text CHARACTER-FOR-CHARACTER from a single pool entry.
     - Do NOT paraphrase, abbreviate, modernize, or "clean up" the wording.
     - Do NOT stitch fragments from different pool entries together.
     - Do NOT add words inside the quotation marks that aren't in the pool entry.
     - You MAY quote a contiguous substring of a pool entry — as long as that
       substring is itself a COMPLETE sentence or complete clause with natural
       ending punctuation, and appears verbatim in the entry (including filler
       words like "you know" if present).
   - Format: `"<exact text>" {{quote:TS}}` where TS is the integer timestamp
     shown next to the pool entry (e.g. `{{quote:326}}`, NOT `{{quote:326s}}`).
   - If you cannot find a clean verbatim quote for a point you want to make,
     paraphrase it in your own words with NO quotation marks. Misquoting is
     worse than paraphrasing — every mismatch will be flagged `[unverified]`.
   - NEVER put quotation marks around short phrases (under 10 words) that
     are not in the pool, even if they read like common terminology.
     Examples of phrases you should NOT quote: "time, place, and manner",
     "change of use", "customer parking only", "good partner". Write these
     without quotation marks when you don't have a verbatim pool entry.
   - WORKED EXAMPLE — valid complete-thought quote: if the pool contains
       `[326s] Chief Natopoulos: "We've trained every officer on the new camera protocols. It's been a big lift, but we're finally done."`
     then a valid use is:
       `"We've trained every officer on the new camera protocols." {{quote:326}}`
     Invalid uses:
       - `"We've trained every officer on the new" {{quote:326}}` (cut off)
       - `"We've trained every officer on the new camera protocols. It's been" {{quote:326}}` (cut off mid-sentence)

PHOTO MARKERS — illustrate the article with 2-4 photos:
   - Place photo markers on their own line between paragraphs at narrative
     transitions (introducing a speaker, shifting to a new topic, closing).
   - Use EXACTLY the format `{{photo:EVENT_ID:TS:CAPTION}}` with the event ID
     and timestamp from the Approved Photo Pool.
   - CAPTION should be a short, descriptive line (5-12 words) identifying
     who is pictured and the moment — e.g. "Mayor Pugh questions the developer
     about traffic concerns."
   - Do NOT invent timestamps or captions for speakers not in the photo pool.
   - Skip photos if no photo pool is provided (then no {{photo:}} markers).

2. REFERENCES — REQUIRED, not optional:
   - The "Reference Pool" below contains prior croton.news coverage that
     overlaps with this meeting's topics. READ EVERY ENTRY before drafting.
   - You MUST cite AT LEAST 3 references via `[R#]` markers inline in the
     article body. Also repeat them in REFERENCES_USED. If the pool has
     fewer than 3 entries, cite all of them.
   - Place `[R#]` INSIDE the article prose, right after the fact it backs —
     not at the end of a paragraph, not lumped at the article bottom. Each
     [R#] becomes a clickable link in the rendered article, so it must
     appear AT THE POINT in the text where the historical context belongs.
   - EVERY article about an ongoing topic (cannabis, zoning, housing,
     body cameras, Gouveia Park, etc.) MUST ground the reader in prior
     coverage with at least 2 [R#] citations in the first 200 words.
   - WORKED EXAMPLE — inline references:
     "The board first reviewed this application back in October [R3], when
     trustees debated whether state law limited them to 'time, place, and
     manner' objections [R7]. That question carried into tonight's session."
   - NEVER write [R#] without the square brackets. Format matters — `R3`
     alone won't render as a link.

3. NO INVENTION: Do not fabricate names, votes, dollar amounts, dates, or attendees.
   If the source materials don't support a fact, omit it.

4. SPEAKER ATTRIBUTION: Use the speaker name shown in the quote pool exactly. If the
   pool says "Unknown speaker," attribute as "a trustee said," "a board member noted,"
   etc. Do not invent names.

Style:
- Lead with what matters most to residents, not procedural details
- 900-1200 words — aim for substance, not padding
- Compelling, accurate headline (8-14 words, specific not generic)
- First paragraph answers: what happened, who it affects, why it matters
- Second paragraph gives historical context with [R#] citations
- Body paragraphs alternate: evidence (quotes + data) and implication
- Closing paragraph: what's next / when residents can weigh in
- Past tense for events, present tense for ongoing situations
- No AI disclaimers, no meta commentary, no "In conclusion"
- Use active voice. Cut hedging ("seems to", "appears to").
- When attributing, NAME speakers from the pool. Never "a trustee said"
  if the pool gives you the trustee's name.

OUTPUT FORMAT (use these exact section labels):

HEADLINE: <headline>
QUICK_SUMMARY: <1-2 sentence summary>
KEY_ACTIONS:
- Decision or action 1 (with vote counts and dollar amounts when present)
- Decision or action 2
- ...up to 8 items, most important first
REFERENCES_USED: R1, R3, R7
(comma-separated list of the [R#] numbers you cited; minimum 3 if pool size allows)
ARTICLE:
<full article body, with {{quote:TS}} after every direct quote and [R#] for every cross-meeting reference>"""

SYSTEM_BOE = SYSTEM_VILLAGE.replace(
    "Village of Croton-on-Hudson, NY", "Croton-Harmon school district (CHUFSD)"
).replace(
    "civic journalism that makes local government accessible to residents",
    "education journalism that makes school board decisions accessible to parents and residents",
)


def write_meeting_article(event_id, model=DEFAULT_MODEL, dry_run=False):
    db = sqlite3.connect(RAG_DB)
    db.row_factory = sqlite3.Row

    transcript = load_transcript(event_id)
    if not transcript:
        print(f"No transcript for event {event_id}")
        db.close()
        return

    meeting = load_meeting(db, event_id)
    date = transcript.get("date", "")
    committee = transcript.get("title", "")
    full_text = transcript.get("full_text", "") or ""

    print(f"Meeting: {committee} ({date}), event {event_id}")
    print(f"Transcript: {transcript.get('word_count', 0)} words, "
          f"{len(transcript.get('utterances', []))} utterances")

    # Phase 1: topic extraction
    print("Extracting topics...")
    topics = extract_topics(transcript)
    print(f"  Topics ({len(topics)}): {topics}")

    # Phase 2: quote pool
    print("Building quote pool...")
    quote_pool = build_quote_pool(transcript)
    print(f"  Quote pool: {len(quote_pool)} quotes")

    # Phase 2b: photo pool (named speakers spread across meeting)
    photo_pool = build_photo_pool(transcript, quote_pool)
    print(f"  Photo pool: {len(photo_pool)} candidates")

    # Phase 3: references
    print("Gathering references from prior meetings...")
    refs = gather_references(topics, event_id)
    print(f"  References: {len(refs)}")
    for r in refs[:8]:
        print(f"    [{r['date']}] {r['committee']} (id={r['meeting_id']}) — {r['topic']}")

    # Phase 4: build prompt
    is_boe = "Board of Education" in (committee or "")
    system = SYSTEM_BOE if is_boe else SYSTEM_VILLAGE

    ref_count_hint = ""
    user_parts = [
        f"Write a 900-1200 word news article about the {committee} on {date}.",
        "",
        "Before you draft, read the Reference Pool and the Approved Quote Pool in full.",
        "",
        "Your draft must include:",
        "  • 4-6 direct quotes from the Approved Quote Pool, each a complete sentence",
        "  • At least 3 inline [R#] citations to the Reference Pool (placed where the",
        "    historical context lands in the prose, NOT lumped at the end)",
        "  • 2-4 {{photo:EVENT_ID:TS:CAPTION}} markers on their own lines between paragraphs",
        "  • No quotation marks around short phrases not in the pool",
        "",
    ]
    if refs:
        user_parts.append(format_references(refs))
        user_parts.append("")
    user_parts.append(format_quote_pool(quote_pool))
    user_parts.append("")
    if photo_pool:
        user_parts.append(format_photo_pool(photo_pool, event_id))
        user_parts.append("")
    user_parts.append("## Full Meeting Transcript")
    user_parts.append("")
    user_parts.append(full_text[:90000])

    user_prompt = "\n".join(user_parts)
    print(f"Calling {model} ({len(user_prompt)} chars)...")

    response = call_llm(system, user_prompt, model=model, max_tokens=8000)

    headline, quick_summary, key_actions, article = parse_response(response)

    # Phase 5: verify quotes
    article, qreport = verify_quotes(article, quote_pool)
    print(f"  Quotes verified: {len(qreport['verified'])}")
    if qreport["issues"]:
        print("  Quote issues:")
        for line in qreport["issues"][:10]:
            print(line)

    # Phase 6: link references in BOTH key_actions and article body
    key_actions, used_refs_ka = link_references(key_actions, refs, append_list=False)
    article, used_refs_a = link_references(article, refs, append_list=True)
    used_refs = used_refs_ka | used_refs_a
    print(f"  References linked: {len(used_refs)}/{len(refs)}")

    print()
    print("=" * 60)
    print(f"HEADLINE: {headline}")
    print(f"QUICK_SUMMARY: {quick_summary}")
    print(f"KEY_ACTIONS:\n{key_actions}")
    print(f"ARTICLE ({len(article)} chars):")
    if dry_run:
        print(article)
    else:
        preview = article[:600] + "..." if len(article) > 600 else article
        print(preview)
    print("=" * 60)

    if dry_run:
        print("\n[DRY RUN — not saved]")
        db.close()
        return

    if meeting:
        db.execute("""
            UPDATE meetings SET
                headline = ?, quick_summary = ?, complete_summary = ?,
                article = ?, article_model = ?,
                article_generated_at = datetime('now')
            WHERE event_id = ?
        """, (headline, quick_summary, key_actions, article, model, event_id))
    else:
        db.execute("""
            INSERT OR REPLACE INTO meetings
                (date, committee, event_id, headline, quick_summary, complete_summary, article,
                 has_transcript, has_video, has_audio,
                 article_model, article_generated_at,
                 word_count, speaker_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 1, ?, datetime('now'), ?, ?)
        """, (date, committee, event_id, headline, quick_summary, key_actions, article,
              model, transcript.get("word_count"), transcript.get("speaker_count")))
    db.commit()
    print("\nSaved to meetings table.")
    db.close()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    event_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    model = DEFAULT_MODEL
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        model_name = sys.argv[idx + 1]
        model_map = {
            "opus": "claude-opus-4-5",
            "sonnet": "claude-sonnet-4-5",
            "glm": "glm-5-turbo",
        }
        model = model_map.get(model_name, model_name)

    if event_id == "topic":
        topic_slug = sys.argv[2]
        print(f"Topic article writing not yet implemented for: {topic_slug}")
    else:
        write_meeting_article(event_id, model=model, dry_run=dry_run)


if __name__ == "__main__":
    main()
