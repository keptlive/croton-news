#!/usr/bin/env python3
"""validate_article.py — deterministic publish gate for AI-written articles.

Born from the 2026-07-14 cross-committee fact-check, where four published
articles showed: quotes attributed to the wrong trustee (6/9 in one article),
a wrong dissenter on a 4-1 vote, fabricated dollar figures ($106.7M budget),
invented names ("Brendan Walker", "Jesse Landeau", "Eva Thaddeus"), and
caption garbles propagated into headlines. All of those are mechanically
detectable — this gate blocks them before publication, independent of which
model wrote the article.

Checks:
  1. quote-attribution  — each "..." {{quote:T}} NAME: chunk at T (±neighbors)
                          must be spoken by NAME (when transcript has names)
  2. quote-verbatim     — quoted strings ≥6 words must appear verbatim
                          (normalized) in the transcript
  3. quote-timestamp    — T must fall within the transcript's time range
  4. caption-attribution— if all speakers are generic, any named attribution
                          must be supported by the official minutes
  5. name-provenance    — person names in article+summaries must appear in
                          transcript/minutes/agenda/packets/entities
  6. dollar-provenance  — dollar figures must appear in a source document

Usage:
  validate_article.py ARTICLE_JSON MEETING_ID        # gate mode (exit 0/2)
  validate_article.py --published MEETING_ID          # check a published row

Exit codes: 0 = pass, 2 = violations (report saved to
rag/validation/article-<id>-report.json), 1 = operational error.
"""
import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(BASE_DIR, "rag.db")
REPORT_DIR = os.path.join(BASE_DIR, "validation")

# tokens that disqualify a capitalized bigram from being a person name
NON_PERSON = {
    "Board", "Village", "School", "District", "Committee", "Commission",
    "Council", "County", "State", "York", "Hall", "Street", "Avenue",
    "Drive", "Road", "Park", "Point", "Library", "Department", "Law",
    "Act", "Fund", "Meeting", "Session", "Hearing", "Plan", "Code",
    "Trustees", "Education", "Hudson", "Condos", "Company", "Inc",
    "Corporation", "Partners", "Associates", "Engineers", "Architecture",
    "May", "June", "July", "August", "September", "October", "November",
    "December", "January", "February", "March", "April", "Monday",
    "Tuesday", "Wednesday", "Thursday", "Friday", "Croton", "Harmon",
    "Cortlandt", "Ossining", "Riverside", "Scenic", "Wyck", "Bay",
    "Advisory", "Zoning", "Planning", "Appeals", "Environment", "Water",
    "Control", "Fire", "Police", "Recreation", "Conservation",
    # titles — so "Superintendent Brendan" extracts as "Brendan Walker"
    "Superintendent", "Trustee", "Mayor", "Chairman", "Chairperson",
    "Director", "Doctor", "Dr", "Manager", "Attorney", "Engineer",
    "President", "Principal", "Deputy", "Chief", "Chair", "Detective",
    "Sergeant", "Officer", "Clerk", "Treasurer", "Secretary", "Liaison",
    # places/orgs/conjunctions that pair into fake person bigrams
    "Lake", "Legion", "American", "But", "And", "Not", "For", "With",
    # heading/bullet nouns
    "Donations", "Total", "Ruling", "Impact", "Operational", "Items",
    "Science", "Faculty", "Members", "Seven", "Instructional",
    "Nominated", "Recognized", "Report", "Update", "Overview", "Summary",
    "Streets", "Roads", "Avenues", "Station", "Overhaul", "Sweeping",
    "Consistency", "Route", "Pond", "Professional", "Signage", "Review",
    "Preview", "Agenda", "Hearing", "Applications", "Permit", "Permits",
    "Chargers", "Relocated", "Approves", "Landscaping", "Outdoors",
    # common orgs that look like person bigrams
    "Con", "Edison", "Consortium",
    # street/place suffixes
    "Circle", "Lane", "Court", "Place", "Terrace", "Manor", "Trail",
    # Title-Case headline/bullet words that pair into fake bigrams
    "Moves", "Forward", "Tweaks", "Winter", "Parking", "Rules", "Storage",
    "Revenue", "Income", "Seniors", "Rescued", "Federal", "Earmark", "The",
    "During", "Curriculum", "Math", "Delta", "Appointed", "Approves",
    "Adopts", "Grants", "Debates", "Hears", "Approved", "Adopted",
    "Granted", "Authorized", "Announces", "Announced", "Proposes",
    "Proposed", "Style", "Work", "Special", "Regular", "Business",
    "Budget", "Public", "Annual", "General", "Camera", "Speed", "Energy",
    "Battery", "Animal", "Leaf", "Blower", "Home", "Rule", "Rewrite",
}

ATTRIB_VERBS = r"(?:said|asked|added|noted|told|replied|responded|explained|warned|urged|argued|continued|recalled|emphasized|stressed|acknowledged)"


def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[‘’]", "'", s)
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[^a-z0-9']+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(name):
    parts = (name or "").strip().split()
    return parts[-1].lower() if parts else ""


def load_sources(db, meeting_id):
    m = db.execute(
        "SELECT id, event_id, committee, date, minutes_text, agenda_json "
        "FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not m:
        raise SystemExit(f"no meeting {meeting_id}")
    chunks = db.execute(
        "SELECT content, speaker, start_time, end_time FROM chunks "
        "WHERE doc_id = ? AND doc_type = 'transcript' ORDER BY start_time",
        (str(m["event_id"]),)).fetchall()
    packets = db.execute(
        "SELECT COALESCE(text,'') t FROM packet_pdfs WHERE event_id = ?",
        (str(m["event_id"]),)).fetchall()
    packet_text = " ".join(p["t"] for p in packets)
    entity_names = [r[0] for r in db.execute(
        "SELECT name FROM entities WHERE type='person'").fetchall()]
    # committee-wide minutes corpus (names often only appear in a nearby
    # meeting's minutes, e.g. superintendent identity)
    com_minutes = " ".join(r[0] or "" for r in db.execute(
        "SELECT minutes_text FROM meetings WHERE committee = ? "
        "AND minutes_text IS NOT NULL", (m["committee"],)).fetchall())
    return m, chunks, packet_text, entity_names, com_minutes


def named(speaker):
    return bool(speaker) and not speaker.startswith(("Speaker ", "Unknown"))


def chunk_near(chunks, ts, slack=45):
    """Chunk containing ts, else nearest within slack seconds."""
    best, best_d = None, None
    for c in chunks:
        s, e = c["start_time"] or 0, c["end_time"] or 0
        if s - 5 <= ts <= e + 5:
            return c, 0
        d = min(abs(ts - s), abs(ts - e))
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is not None and best_d <= slack:
        return best, best_d
    return None, None


def neighbors(chunks, center, k=2):
    try:
        i = chunks.index(center)
    except ValueError:
        return [center]
    return chunks[max(0, i - k):i + k + 1]


def _base_token(tok):
    """Strip possessive and hyphen suffix for NON_PERSON checks."""
    tok = re.sub(r"[''']s$", "", tok)
    return tok.split("-")[0]


def extract_person_names(text):
    names = set()
    text = text or ""
    for mm in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-zA-Z'\-]{2,})\b", text):
        first, last = mm.group(1), mm.group(2)
        if _base_token(first) in NON_PERSON or _base_token(last) in NON_PERSON:
            continue
        # skip sentence/bullet-initial bigrams: "- Nominated Cheryl ..." and
        # "New Science curriculum" are Title-Case artifacts, not names. A real
        # name's surname still gets checked when it appears mid-sentence.
        pre = text[max(0, mm.start() - 6):mm.start()]
        if mm.start() == 0 or re.search(r"(^|[.!?:\n]|[-•*#]\s*|\d+\.\s*)\s*$", pre):
            continue
        # skip bigrams inside Title-Case runs of 3+ words ("Cameras Will
        # Bring", "Any Bird, Pigeons Count" — quoted headlines/headings, not
        # names). A real flagged name is followed by lowercase prose
        # ("Brendan Walker recommended...") and still fires.
        nxt = re.match(r"[,:;\s]+([A-Z][a-z]+)", text[mm.end():])
        if nxt:
            continue
        names.add(f"{first} {last}")
    return names


def _is_subset_sum(target, values, max_size=6, node_budget=200000):
    """True if target equals the sum of 2..max_size distinct source values.

    Bounded DFS over all candidate values (an earlier top-N-by-size cap
    dropped small components like the $2,000 in a $43,000 donations total).
    """
    vals = sorted({round(v, 2) for v in values if 0 < v <= target + 0.01},
                  reverse=True)
    n = len(vals)
    budget = [node_budget]

    def dfs(i, remaining, depth):
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        if abs(remaining) < 0.01:
            return depth >= 2
        if depth == max_size or i >= n:
            return False
        # prune: even the largest remaining values can't cover the gap
        if vals[i] * (max_size - depth) < remaining - 0.01:
            return False
        for j in range(i, n):
            v = vals[j]
            if v > remaining + 0.01:
                continue
            if dfs(j + 1, remaining - v, depth + 1):
                return True
        return False

    return dfs(0, float(target), 0)


def validate(data, meeting_id, db):
    violations = []
    article = data.get("article") or ""
    headline = data.get("headline") or ""
    quick = data.get("quick_summary") or ""
    complete = data.get("complete_summary") or data.get("key_actions") or ""

    m, chunks, packet_text, entity_names, com_minutes = load_sources(db, meeting_id)
    transcript_text = " ".join(c["content"] for c in chunks)
    transcript_norm = normalize(transcript_text)
    minutes = m["minutes_text"] or ""
    agenda = m["agenda_json"] or ""
    all_named_speakers = {c["speaker"] for c in chunks if named(c["speaker"])}
    caption_mode = bool(chunks) and not all_named_speakers

    source_text = " ".join([transcript_text, minutes, agenda, packet_text, com_minutes])
    source_norm = normalize(source_text)
    source_digits = re.sub(r"[,$]", "", source_text)
    max_end = max((c["end_time"] or 0 for c in chunks), default=0)

    # ── 1-4: quotes ─────────────────────────────────────────────
    quote_re = re.compile(
        r'["“]([^"“”]{20,600})["”]\s*'
        r"\{\{quote:(?:yt-[\w\-]+:|\d{4,}:)?(\d+)\}\}"
        r"(.{0,120})", re.S)
    for qm in quote_re.finditer(article):
        quoted, ts_s, after = qm.group(1), qm.group(2), qm.group(3)
        ts = int(ts_s)
        before = article[max(0, qm.start() - 160):qm.start()]

        # attribution name near the quote (after: ", NAME said" / before: "NAME said:")
        PRONOUNS = {"She", "He", "They", "We", "It", "You", "I", "Who"}
        attrib = None
        am = re.search(r"[,\s]*([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-]+)?)\s+" + ATTRIB_VERBS, after)
        if am and am.group(1).split()[0] not in NON_PERSON | PRONOUNS:
            attrib = am.group(1)
        if not attrib:
            bm = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-]+)?)\s+" + ATTRIB_VERBS + r"[^.\n]{0,40}$", before)
            if bm and bm.group(1).split()[0] not in NON_PERSON | PRONOUNS:
                attrib = bm.group(1)

        # 3. timestamp in range
        if chunks and ts > max_end + 180:
            violations.append({
                "type": "quote-timestamp",
                "detail": f"{{{{quote:{ts}}}}} is beyond transcript end ({int(max_end)}s)"})

        # 2. verbatim (≥6 words must match transcript)
        if len(quoted.split()) >= 6:
            if normalize(quoted) not in transcript_norm:
                violations.append({
                    "type": "quote-verbatim",
                    "detail": f"quoted text not found verbatim in transcript: \"{quoted[:90]}...\" "
                              "— quote exactly or paraphrase without quotation marks"})

        # 1/4. attribution
        c, _ = chunk_near(chunks, ts)
        if attrib:
            if caption_mode:
                if surname(attrib) not in normalize(minutes + com_minutes):
                    violations.append({
                        "type": "caption-attribution",
                        "detail": f"'{attrib}' attributed on a caption transcript with no "
                                  "minutes support — use generic attribution"})
            elif c is not None:
                window = [x for x in neighbors(chunks, c) if named(x["speaker"])]
                if window and not any(
                        surname(attrib) == surname(x["speaker"]) for x in window):
                    # single-token speaker labels ("Ralph") carry no surname —
                    # fall back to minutes/agenda support for the attributed
                    # name (minutes confirmed "Ralph Rossi"; label was just
                    # first-name-only). Multi-token labels stay strict.
                    # ANY single-token label in the window means the true
                    # speaker may lack a surname — then minutes support for
                    # the attribution suffices ("Ralph" + minutes' "Ralph
                    # Rossi"). Windows of only full names stay strict.
                    has_single_token = any(
                        len((x["speaker"] or "").split()) == 1 for x in window)
                    auth_norm = normalize(minutes + " " + agenda + " " + packet_text)
                    if has_single_token and surname(attrib) and re.search(
                            r"\b" + re.escape(surname(attrib)) + r"\b", auth_norm):
                        pass  # authoritative support for the attribution
                    else:
                        speakers_here = ", ".join(sorted({x["speaker"] for x in window}))
                        violations.append({
                            "type": "quote-attribution",
                            "detail": f"quote at t={ts} attributed to '{attrib}' but transcript "
                                      f"speaker(s) there: {speakers_here}"})

    # ── 5: person-name provenance ───────────────────────────────
    # headline excluded: Title-Case headline words pair into fake bigrams
    # ("Moves Forward"); fabricated names always also appear in body/summary
    name_zone = " ".join([article, quick, complete])
    check_zone = " ".join([headline, article, quick, complete])
    known_norm = normalize(" ".join(entity_names) + " " + " ".join(all_named_speakers))
    for name in sorted(extract_person_names(name_zone)):
        n = normalize(name)
        # full-name match required: a surname-only fallback would wave
        # through wrong first names ("Brendan Walker" for Stephen Walker)
        if n in source_norm or n in known_norm:
            continue
        # agendas often list names surname-first ("Shoenholt, Lisa")
        rev = " ".join(reversed(n.split()))
        if rev in source_norm or rev in known_norm:
            continue
        violations.append({
            "type": "name-provenance",
            "detail": f"person name '{name}' appears in no source "
                      "(transcript/minutes/agenda/packets/entities)"})

    # ── 6: dollar-figure provenance ─────────────────────────────
    # all dollar values present in sources, for rounding tolerance
    src_values = set()
    for sm in re.finditer(r"\$\s?([\d][\d,]*(?:\.\d+)?)(\s*(million|billion|thousand))?",
                          source_text):
        try:
            v = float(sm.group(1).replace(",", ""))
            if sm.group(3):
                v *= 10 ** {"thousand": 3, "million": 6, "billion": 9}[sm.group(3).lower()]
            src_values.add(v)
        except ValueError:
            pass

    seen_figs = set()
    for dm in re.finditer(r"\$\s?([\d][\d,]*(?:\.\d+)?)(\s*(million|billion|thousand))?",
                          check_zone):
        num, scale = dm.group(1), (dm.group(3) or "").lower()
        digits = num.replace(",", "")
        key = (digits, scale)
        if key in seen_figs:
            continue
        seen_figs.add(key)
        variants = [digits]
        value = None
        try:
            value = float(digits)
            if scale:
                value *= 10 ** {"thousand": 3, "million": 6, "billion": 9}[scale]
                variants.append(str(int(value)))
        except ValueError:
            pass
        if scale:
            variants.append(f"{digits} {scale}")
        if any(v in source_digits or v in source_norm for v in variants):
            continue
        # rounding tolerance: "about $702,000" vs source $702,461 (≤1%)
        if value and any(sv and abs(value - sv) / max(sv, 1) <= 0.01 for sv in src_values):
            continue
        # verified-sum tolerance: totals of source figures are legitimate
        # journalism ("accepted $43,000 in donations" = 25k+10k+6k+2k). Only
        # exact subset sums count — still deterministic.
        if value and _is_subset_sum(value, src_values):
            continue
        violations.append({
            "type": "dollar-provenance",
            "detail": f"figure '${num}{' ' + scale if scale else ''}' appears in no source document"})

    return violations


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    db = sqlite3.connect(f"file:{RAG_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    if args[0] == "--published":
        meeting_id = int(args[1])
        row = db.execute(
            "SELECT headline, quick_summary, complete_summary, article "
            "FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        data = {k: row[k] for k in row.keys()}
    else:
        json_path, meeting_id = args[0], int(args[1])
        with open(json_path) as f:
            data = json.loads(f.read(), strict=False)

    violations = validate(data, meeting_id, db)
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"article-{meeting_id}-report.json")
    report = {"meeting_id": meeting_id, "passed": not violations,
              "violations": violations}
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if violations:
        print(f"FAIL: {len(violations)} violation(s) — {report_path}")
        for v in violations:
            print(f"  [{v['type']}] {v['detail']}")
        return 2
    print("PASS: no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
