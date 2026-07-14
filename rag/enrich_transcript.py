#!/usr/bin/env python3
"""
Transcript Enrichment Pipeline

Post-processes Deepgram transcripts to fix:
1. Proper noun corrections (Croton-specific terms, names, institutions)
2. Merge fragmented short utterances from same speaker
3. Fix common transcription errors
4. Clean formatting
5. Correct speaker names against minutes + entity DB (fuzzy matching)

Run after Deepgram transcription, before ingestion.

Usage:
    python3 enrich_transcript.py TRANSCRIPT_FILE    # Enrich one file
    python3 enrich_transcript.py --all               # Enrich all transcripts
    python3 enrich_transcript.py --fix-names         # Re-run name correction only (even on enriched)
"""

import json
import os
import re
import sqlite3
import sys
import glob
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
RAG_DB = os.path.join(BASE_DIR, "rag.db")
SUMMARIES_DB = os.path.join(os.path.dirname(BASE_DIR), "scrapers", "summaries.db")

# ═══════════════════════════════════════════════════════════════════
# PROPER NOUN CORRECTIONS
# Verified against chufsd.org, village records, and news sources.
# ═══════════════════════════════════════════════════════════════════

# District name variations Deepgram gets wrong
DISTRICT_FIXES = {
    "Courtland Harmony": "Croton-Harmon",
    "courtland harmony": "Croton-Harmon",
    "Cortland Harmony": "Croton-Harmon",
    "cortland harmony": "Croton-Harmon",
    "Kirtland Harmon": "Croton-Harmon",
    "kirtland harmon": "Croton-Harmon",
    "Croton Harmon": "Croton-Harmon",
    "Groton Harden": "Croton-Harmon",
    "Groton Harmon": "Croton-Harmon",
    "groton harden": "Croton-Harmon",
    "Preschool District": "Free School District",
    "preschool district": "Free School District",
}

# Phrase-level fixes (context-dependent)
PHRASE_FIXES = [
    # Deepgram mishearings
    (r'\barmed dimension\b', 'I am honored'),
    (r'\bludatorian\b', 'salutatorian'),
    (r'\bLudatorian\b', 'Salutatorian'),
    # Acronyms Deepgram expands or garbles
    (r'\bNisa\b(?=\s+is\s|\s+also\s|\s+recommend)', 'NYSSBA'),
    (r'\bnisba\b', 'NYSSBA'),
    (r'\bNisus\b', 'NYSUT'),
    (r'\bnisut\b', 'NYSUT'),
]

# Context-dependent Croton fixes (only when followed by school/district terms)
CROTON_CONTEXT_FIXES = [
    (r'\bCurtain(?=[\s-](?:Harmon|on|Hudson|schools?|district|community))', 'Croton'),
    (r'\bcurtain(?=[\s-](?:harmon|on|hudson|schools?|district|community))', 'Croton'),
    (r'\bCurtin(?=[\s-](?:Harmon|on|Hudson|and))', 'Croton'),
    (r'\bcurtin(?=[\s-](?:harmon|on|hudson|and))', 'Croton'),
]

# Town of Cortlandt (real municipality, often misspelled without the t)
CORTLANDT_FIXES = [
    (r'\bCortland\b(?!\s+Hudson)', 'Cortlandt'),   # Town of Cortland → Cortlandt
    (r'\bcortland\b(?!\s+hudson)', 'Cortlandt'),
    (r'\bCortland(?=\s+Hudson)', 'Croton-on-'),     # Cortland Hudson → Croton-on-Hudson
    (r'\bcortland(?=\s+hudson)', 'Croton-on-'),
]

# ═══════════════════════════════════════════════════════════════════
# VERIFIED NAMES
# Source: chufsd.org/boe/board-trustees (verified April 2026)
# ═══════════════════════════════════════════════════════════════════

# Board of Education members (current as of 2025-2026)
BOARD_MEMBERS = {
    "Ana Teague": "Board President",
    "Anamika Bhatnagar": "Board Vice President",
    "Sarah Carrier": "Board Trustee",
    "Neal Haber": "Board Trustee",
    "Omar Mayyasi": "Board Trustee",
    "Theo Oshiro": "Board Trustee",
    "Allison Samuels": "Board Trustee",
    "Filomena DiMarco": "Student Ex Officio",
}

# Administration
ADMINISTRATORS = {
    "Stephen Walker": "Superintendent",
    "Omar Faruk": "Assistant Superintendent for Business",
    "Laura Fjeld": "Assistant Superintendent for Curriculum & Instruction",
}

# Name corrections Deepgram commonly gets wrong
NAME_FIXES = {
    "Anika": "Anamika",        # Board VP
    "Annika": "Anamika",
    "Anaka": "Anamika",
    "Brendan Walker": "Stephen Walker",
    "Steven Walker": "Stephen Walker",
    "Stefan Walker": "Stephen Walker",
    "McTaylor": "Meccariello",  # BoT Trustee
    "Slippin": "Slippen",       # BoT Trustee Maria Slippen
    "T Town": "Teatown",        # Teatown area/road
    "t town": "Teatown",
}

# Schools and facilities
FACILITY_FIXES = {
    "CET": "CET",              # Carrie E. Tompkins Elementary
    "PVC": "PVC",              # Pierre Van Cortlandt Middle School
    "CHHS": "CHHS",            # Croton-Harmon High School
    "ILC": "ILC",              # Innovative Learning Center
    "Spencer Field": "Spencer Field",  # Athletic complex (this is correct)
}

# Laws and organizations
ORGANIZATION_FIXES = {
    "Desha's law": "Desha's Law",   # Cardiac emergency response law
    "desha's law": "Desha's Law",
    "Lorraine Hansberry": "Lorraine Hansberry",  # Coalition
    "Lothier Hansberry": "Lorraine Hansberry",
    "lothier hansberry": "Lorraine Hansberry",
}


# ═══════════════════════════════════════════════════════════════════
# TEXT CORRECTION
# ═══════════════════════════════════════════════════════════════════

def fix_text(text):
    """Apply all proper noun and phrase corrections to text."""
    # District name fixes (exact string replacement)
    for wrong, right in DISTRICT_FIXES.items():
        text = text.replace(wrong, right)

    # Organization fixes
    for wrong, right in ORGANIZATION_FIXES.items():
        text = text.replace(wrong, right)

    # Name fixes
    for wrong, right in NAME_FIXES.items():
        text = text.replace(wrong, right)

    # Phrase-level regex fixes
    for pattern, replacement in PHRASE_FIXES:
        text = re.sub(pattern, replacement, text)

    # Context-dependent Croton fixes
    for pattern, replacement in CROTON_CONTEXT_FIXES:
        text = re.sub(pattern, replacement, text)

    # Cortlandt / Croton-on-Hudson fixes
    for pattern, replacement in CORTLANDT_FIXES:
        text = re.sub(pattern, replacement, text)

    return text


# ═══════════════════════════════════════════════════════════════════
# UTTERANCE MERGING
# Deepgram often splits one speaker's sentence into many 1-2 word
# utterances. This merges consecutive same-speaker utterances that
# are close in time into natural sentences/paragraphs.
# ═══════════════════════════════════════════════════════════════════

def merge_utterances(utterances, max_gap_seconds=2.0, max_merged_words=300):
    """Merge consecutive same-speaker utterances that are close in time.

    Deepgram Nova 3 often produces utterances like:
        Speaker 1: "I"
        Speaker 1: "think"
        Speaker 1: "we should"
        Speaker 1: "consider this carefully."

    This merges them into:
        Speaker 1: "I think we should consider this carefully."
    """
    if not utterances:
        return []

    merged = []
    current = {
        "speaker": utterances[0].get("speaker", ""),
        "text": utterances[0].get("transcript", utterances[0].get("text", "")),
        "start": utterances[0].get("start", 0),
        "end": utterances[0].get("end", 0),
        "sentiment": utterances[0].get("sentiment", "neutral"),
    }

    for u in utterances[1:]:
        speaker = u.get("speaker", "")
        text = u.get("transcript", u.get("text", ""))
        start = u.get("start", 0)
        end = u.get("end", 0)
        sentiment = u.get("sentiment", "neutral")

        same_speaker = speaker == current["speaker"]
        close_in_time = (start - current["end"]) < max_gap_seconds
        not_too_long = len(current["text"].split()) < max_merged_words

        if same_speaker and close_in_time and not_too_long:
            # Merge
            current["text"] = current["text"] + " " + text
            current["end"] = end
            # Keep the more expressive sentiment
            if sentiment in ("positive", "negative") and current["sentiment"] == "neutral":
                current["sentiment"] = sentiment
        else:
            # Flush current, start new
            merged.append(current)
            current = {
                "speaker": speaker,
                "text": text,
                "start": start,
                "end": end,
                "sentiment": sentiment,
            }

    merged.append(current)

    # Add timestamps
    for m in merged:
        start = m["start"]
        minutes = int(start // 60)
        seconds = int(start % 60)
        m["timestamp"] = f"{minutes:02d}:{seconds:02d}"

    return merged


# ═══════════════════════════════════════════════════════════════════
# LLM SPEAKER IDENTIFICATION
# When Deepgram gives only "Speaker 0/1/2...", use an LLM to identify
# who is speaking based on context clues in the transcript text:
# roll call, self-introductions, being addressed by name, etc.
# ═══════════════════════════════════════════════════════════════════

import urllib.request

ZAI_KEY = os.environ.get("ZAI_KEY", "")
ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"


def identify_speakers_llm(utterances, committee="", date="", known_names=None, agenda_items=None):
    """Use an LLM to map Speaker 0/1/2... to real names based on transcript context.

    Reads the first ~50 utterances (where roll call and introductions happen)
    plus any utterances that mention names, and asks the LLM to build a speaker_map.

    If agenda_items is provided (list of dicts from ChampDS), includes them as context
    to help identify who presents which agenda items.

    Returns dict like {"0": "Brian Pugh", "1": "Bryan Healy", ...}
    """
    if not utterances:
        return {}

    # Build context: first 50 utterances + any that mention known officials
    context_utts = utterances[:50]

    # Also grab utterances that mention names (further in the transcript)
    name_keywords = [
        "mayor", "manager", "trustee", "chair", "treasurer", "attorney",
        "superintendent", "chief", "i'm ", "my name is", "thank you,",
    ]
    if known_names:
        # Add last names of known people as keywords
        for n in known_names:
            parts = n.split()
            if len(parts) >= 2:
                name_keywords.append(parts[-1].lower())

    seen_indices = set(range(min(50, len(utterances))))
    for i, u in enumerate(utterances[50:], start=50):
        text_lower = u.get("text", "").lower()
        if any(kw in text_lower for kw in name_keywords):
            if len(seen_indices) < 80:  # cap total context
                seen_indices.add(i)

    # Format context for LLM
    context_lines = []
    for i in sorted(seen_indices):
        u = utterances[i]
        context_lines.append(f'[{i}] {u.get("speaker", "?")}: {u.get("text", "")[:200]}')

    context_text = "\n".join(context_lines)

    # Build the known names reference
    known_ref = ""
    if known_names:
        known_ref = "\n\nKnown officials and frequent participants:\n"
        for name, role in list(known_names.items())[:40]:
            if role:
                known_ref += f"- {name} ({role})\n"
            else:
                known_ref += f"- {name}\n"

    # Build agenda context if available
    agenda_ref = ""
    if agenda_items:
        agenda_lines = []
        def _walk_agenda(items, depth=0):
            for item in items:
                prefix = "  " * depth + "- "
                agenda_lines.append(f"{prefix}{item.get('title', '')}")
                _walk_agenda(item.get("children", []), depth + 1)
        _walk_agenda(agenda_items)
        if agenda_lines:
            agenda_ref = "\n\nOfficial meeting agenda (helps identify who presents what):\n" + "\n".join(agenda_lines[:40])

    prompt = f"""You are analyzing a transcript of a {committee} meeting held on {date} in Croton-on-Hudson, NY.

The transcript uses generic speaker labels (Speaker 0, Speaker 1, etc.). Your task is to identify who each speaker is based on context clues in the text:
- Self-identification: "I'm mayor Brian Pugh"
- Being addressed: "Thank you, manager" (next speaker is the manager)
- Roll call: "Motion by trustee Simon, second by trustee Nicholson"
- References: "as trustee Slippen said" (tells us who Speaker N is if they spoke before)
- Known patterns: Speaker 0 often opens the meeting (usually the chair/mayor)
{known_ref}{agenda_ref}
Here is the transcript (first ~50 utterances plus any with name mentions):

{context_text}

Based on the evidence above, provide a JSON object mapping speaker numbers to real names.
ONLY include speakers you can confidently identify. Use the format:
{{"0": "Full Name", "1": "Full Name", ...}}

Rules:
- Only use names that appear in the transcript text or known officials list
- If you cannot identify a speaker, omit them from the map
- Use the official spelling from the known officials list when available
- For the mayor/chair who opens the meeting, use their full name (not title)

Respond with ONLY the JSON object, no other text."""

    try:
        req_data = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(ZAI_URL, data=req_data, headers={
            "Content-Type": "application/json",
            "x-api-key": ZAI_KEY,
            "anthropic-version": "2023-06-01",
        })

        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        text = result.get("content", [{}])[0].get("text", "")

        # Extract JSON from response
        # Handle cases where LLM wraps in ```json blocks
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        speaker_map = json.loads(text)
        return speaker_map

    except Exception as e:
        print(f"  LLM speaker identification failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════
# SPEAKER NAME CORRECTION
# Uses official meeting minutes as source of truth for name spellings.
# Minutes are in summaries.db index_json (LLM-extracted from ecode360).
# Entity DB (rag.db) provides additional known names.
# ═══════════════════════════════════════════════════════════════════

# Title prefixes to strip before fuzzy matching
SPEAKER_TITLE_PREFIXES = [
    "Mayor", "Trustee", "Chair", "Chairman", "Chairwoman", "Chairperson",
    "Vice Chair", "Acting Chair", "Village Manager", "Village Engineer",
    "Village Attorney", "Village Treasurer", "Treasurer",
    "Superintendent", "DPW Superintendent", "Assistant Superintendent",
    "Deputy Mayor", "Board Member", "ZBA Chair", "Planning Board Chair",
    "Police Chief", "Fire Chief", "Chief", "Inspector", "Director",
    "Lieutenant", "Sergeant", "Officer", "Commissioner", "Councilmember",
]

# Minimum fuzzy ratio to accept a correction
NAME_MATCH_THRESHOLD = 0.82


def _strip_speaker_title(name):
    """Remove title prefixes from a speaker name."""
    for prefix in sorted(SPEAKER_TITLE_PREFIXES, key=len, reverse=True):
        if name.startswith(prefix + " "):
            return name[len(prefix) + 1:].strip()
    return name


def _load_canonical_names():
    """Load canonical name spellings from minutes (summaries.db) and entity DB (rag.db).

    Minutes index_json is the primary source of truth.
    Entity DB fills in names not found in minutes.

    Returns (all_names, minutes_names) where minutes_names is the authoritative subset.
    """
    minutes_names = {}
    all_names = {}

    # 1. Minutes names (source of truth) — from summaries.db index_json
    #    Use majority voting: the most frequent spelling wins, since the LLM
    #    extraction sometimes "corrects" unusual real spellings to common ones.
    if os.path.exists(SUMMARIES_DB):
        try:
            db = sqlite3.connect(SUMMARIES_DB)
            db.row_factory = sqlite3.Row

            # Count occurrences of each name spelling
            name_counts = {}  # base_name -> count
            name_roles = {}   # base_name -> role (from first occurrence)

            for row in db.execute(
                "SELECT index_json FROM summaries WHERE index_json IS NOT NULL"
            ).fetchall():
                try:
                    idx = json.loads(row["index_json"])
                except json.JSONDecodeError:
                    continue
                for p in idx.get("people", []):
                    name = p.get("name", "").strip()
                    role = p.get("role", "")
                    base = _strip_speaker_title(name)
                    if len(base.split()) >= 2 and len(base) >= 5:
                        name_counts[base] = name_counts.get(base, 0) + 1
                        if base not in name_roles:
                            name_roles[base] = role

            # Group similar names and pick the majority spelling
            # e.g. "Bryan Healy" (26x) beats "Brian Healy" (3x)
            from difflib import SequenceMatcher as SM
            used = set()
            for name in sorted(name_counts, key=lambda n: -name_counts[n]):
                if name in used:
                    continue
                # This is the majority spelling — add it
                minutes_names[name] = name_roles.get(name, "")
                used.add(name)
                # Mark similar minority spellings as used (don't add them)
                for other in name_counts:
                    if other != name and other not in used:
                        score = SM(None, name.lower(), other.lower()).ratio()
                        if score >= 0.85:
                            used.add(other)

            db.close()
        except Exception as e:
            print(f"  Warning: could not read summaries.db: {e}")

    all_names.update(minutes_names)

    # 2. Entity DB names — fill gaps not covered by minutes
    if os.path.exists(RAG_DB):
        try:
            db = sqlite3.connect(RAG_DB)
            db.row_factory = sqlite3.Row
            for row in db.execute(
                "SELECT name, metadata_json FROM entities WHERE type='person'"
            ).fetchall():
                name = row["name"]
                if len(name.split()) >= 2 and name not in all_names:
                    try:
                        md = json.loads(row["metadata_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        md = {}
                    all_names[name] = md.get("role", "")
            db.close()
        except Exception as e:
            print(f"  Warning: could not read rag.db: {e}")

    return all_names, minutes_names


def correct_speaker_names(speaker_map, canonical_names, minutes_names=None):
    """Correct speaker_map values by fuzzy-matching against canonical names.

    Matching strategies (in priority order):
    1. Role-based: if name has a title/role prefix, find the person with that role
       in minutes (e.g. "Police Chief Nick Natopoulos" → "John Nikitopoulos" because
       minutes say the Police Chief is John Nikitopoulos)
    2. Full-name fuzzy: compare stripped names with SequenceMatcher
    3. Last-name fallback: when full-name score is too low, try last-name only

    Minutes names override entity DB names when they conflict.

    Returns (corrected_map, corrections_log).
    """
    if not speaker_map or not canonical_names:
        return speaker_map, []

    if minutes_names is None:
        minutes_names = {}

    # Build a role → list of names lookup from minutes for role-based matching
    # e.g. "police chief" → ["John Nikitopoulos"], "mayor" → ["Brian Pugh", "Paul Pugh"]
    role_lookup = {}  # role_key → [name, ...]
    for mname, role in (minutes_names or {}).items():
        if role:
            role_key = role.lower().strip()
            role_lookup.setdefault(role_key, []).append(mname)
            # Also index by shorter/normalized role forms
            for prefix in ("village ", "acting ", "deputy ", "assistant ", "croton "):
                if role_key.startswith(prefix):
                    short = role_key[len(prefix):]
                    role_lookup.setdefault(short, []).append(mname)

    corrections = []
    canonical_list = list(canonical_names.keys())
    corrected = dict(speaker_map)

    for key, name in speaker_map.items():
        if not name or name.startswith("Speaker"):
            continue

        # Strip title prefix
        base_name = _strip_speaker_title(name)
        if len(base_name.split()) < 2:
            continue

        # Skip if name is already an exact match in canonical names
        if base_name in canonical_names and base_name == name:
            continue

        # Strategy 1: Role-based matching
        # If the original name had a title prefix, find the person with that role
        # in minutes. Only use if the base name is NOT already a close fuzzy match
        # for something else (avoids replacing "Mayor Brian Pugh" with a different Mayor).
        role_match = None
        if name != base_name:
            title_used = name[:len(name) - len(base_name)].strip()
            title_key = title_used.lower()
            candidates = role_lookup.get(title_key, [])
            if not candidates:
                for rk, rnames in role_lookup.items():
                    if title_key in rk or rk in title_key:
                        candidates = rnames
                        break

            if candidates:
                # Pick the candidate whose name is closest to base_name
                best_cand = None
                best_cand_score = 0
                for cand in candidates:
                    s = SequenceMatcher(None, base_name.lower(), cand.lower()).ratio()
                    if s > best_cand_score:
                        best_cand_score = s
                        best_cand = cand
                role_match = best_cand

            if role_match and role_match != base_name:
                # Also check if base_name matches ANY canonical name well
                best_canonical = 0
                for cname in canonical_list:
                    s = SequenceMatcher(None, base_name.lower(), cname.lower()).ratio()
                    if s > best_canonical:
                        best_canonical = s

                # Use role match only if base_name has no good canonical match
                # (i.e. the name is clearly wrong, not just title-prefixed)
                if best_canonical < NAME_MATCH_THRESHOLD:
                    corrected[key] = role_match
                    corrections.append((name, role_match, 1.0, "role-matched"))
                    continue

        # Strategy 2: Full-name fuzzy match
        base_words = len(base_name.split())
        best_match = None
        best_score = 0

        for cname in canonical_list:
            cname_words = len(cname.split())
            if abs(base_words - cname_words) > 1:
                continue

            score = SequenceMatcher(None, base_name.lower(), cname.lower()).ratio()
            if score > best_score:
                best_score = score
                best_match = cname

        # Strategy 3: Last-name fallback when full-name score is too low.
        # Catches cases like "Nick Natopoulos" → "John Nikitopoulos" where
        # Deepgram got both first and last name wrong but the last name is close.
        if (not best_match or best_score < NAME_MATCH_THRESHOLD) and base_words >= 2:
            base_last = base_name.split()[-1].lower()
            for cname in canonical_list:
                cname_parts = cname.split()
                if len(cname_parts) < 2:
                    continue
                cname_last = cname_parts[-1].lower()
                last_score = SequenceMatcher(None, base_last, cname_last).ratio()
                if last_score >= NAME_MATCH_THRESHOLD and last_score > best_score:
                    best_score = last_score
                    best_match = cname

        if best_match and best_score >= NAME_MATCH_THRESHOLD:
            # If best match is from minutes and is different from base_name, prefer it
            # If best match IS base_name (exact), check if minutes has a better spelling
            if best_match == base_name:
                # Check if minutes has a fuzzy-better spelling
                for mname in minutes_names:
                    mscore = SequenceMatcher(None, base_name.lower(), mname.lower()).ratio()
                    if mscore >= NAME_MATCH_THRESHOLD and mname != base_name:
                        best_match = mname
                        best_score = mscore
                        break

            if best_match != name:
                corrected[key] = best_match
                method = "minutes" if best_match in minutes_names else "fuzzy"
                if name != base_name and best_match == base_name:
                    method = "title-stripped"
                corrections.append((name, best_match, best_score, method))

    return corrected, corrections


# ═══════════════════════════════════════════════════════════════════
# MAIN ENRICHMENT
# ═══════════════════════════════════════════════════════════════════

def enrich_transcript(path):
    """Enrich a single Deepgram transcript."""
    with open(path) as f:
        data = json.load(f)

    # Only skip caption-based transcripts. The old exact-match gate
    # (platform != "deepgram-nova-3") silently skipped every retranscribed
    # file because retranscribe.py never wrote a platform key — the
    # proper-noun fix pass ("Slippin"→"Slippen" etc.) never ran on them.
    if data.get("platform") == "youtube" or not data.get("utterances"):
        print(f"  Skip {os.path.basename(path)} — caption-based or no utterances")
        return False

    if data.get("enriched"):
        print(f"  Skip {os.path.basename(path)} — already enriched")
        return False

    utterances = data.get("utterances", [])
    original_count = len(utterances)

    # Step 1: Merge fragmented utterances
    merged = merge_utterances(utterances)
    print(f"  Merged: {original_count} → {len(merged)} utterances")

    # Step 2: Fix proper nouns in all text
    fixes_count = 0
    for u in merged:
        original = u["text"]
        u["text"] = fix_text(u["text"])
        if u["text"] != original:
            fixes_count += 1

    # Also fix full_text
    data["full_text"] = fix_text(data.get("full_text", ""))

    # Fix summary
    if data.get("dg_summary"):
        data["dg_summary"] = fix_text(data["dg_summary"])

    print(f"  Fixed proper nouns in {fixes_count} utterances")

    # Step 3: Identify speakers via LLM if speaker_map is empty
    speaker_map = data.get("speaker_map", {})
    canonical_names, minutes_names = _load_canonical_names()

    has_real_names = any(
        v and not v.startswith("Speaker") for v in speaker_map.values()
    )

    if not has_real_names and merged:
        # No speakers identified — use LLM to analyze transcript context
        committee = data.get("title", "")
        date = data.get("date", "")
        # Load agenda from DB if available (helps identify presenters)
        agenda_items = None
        event_id = data.get("event_id")
        if event_id:
            try:
                _db = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(path)), "rag.db"))
                _db.row_factory = sqlite3.Row
                _row = _db.execute("SELECT agenda_json FROM meetings WHERE event_id = ?", (str(event_id),)).fetchone()
                if _row and _row["agenda_json"]:
                    agenda_items = json.loads(_row["agenda_json"])
                _db.close()
            except Exception:
                pass
        print(f"  Identifying speakers via LLM...{' (with agenda)' if agenda_items else ''}")
        llm_map = identify_speakers_llm(merged, committee, date, minutes_names, agenda_items=agenda_items)
        if llm_map:
            speaker_map = llm_map
            data["speaker_map"] = speaker_map
            for num, name in sorted(llm_map.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99):
                print(f"  Speaker {num} → {name}")
            print(f"  Identified {len(llm_map)} speakers")
        else:
            print(f"  Could not identify speakers")

    # Step 4: Correct speaker names against minutes + entity DB
    if canonical_names:
        corrected_map, name_corrections = correct_speaker_names(
            speaker_map, canonical_names, minutes_names
        )
        if name_corrections:
            data["speaker_map"] = corrected_map
            for old, new, score, method in name_corrections:
                print(f"  Name fix: \"{old}\" → \"{new}\" ({score:.2f}, {method})")
            print(f"  Corrected {len(name_corrections)} speaker names")
        else:
            print(f"  Speaker names OK ({len(canonical_names)} reference names checked)")

    # Step 5: Rebuild full_text from merged utterances (with resolved speaker names)
    final_map = data.get("speaker_map", {})

    def _resolve(speaker):
        if final_map:
            num = speaker.replace("Speaker ", "")
            return final_map.get(num, final_map.get(speaker, speaker))
        return speaker

    data["full_text"] = "\n\n".join(
        f"[{u['timestamp']}] {_resolve(u['speaker'])}: {u['text']}"
        for u in merged
    )

    # Step 6: Update metadata
    data["utterances"] = merged
    data["word_count"] = sum(len(u["text"].split()) for u in merged)
    data["speaker_count"] = len(set(u["speaker"] for u in merged))
    data["enriched"] = True
    data["enrichment_note"] = (
        "Post-processed: merged fragmented utterances, "
        "corrected proper nouns (district name, board members, facilities), "
        "corrected speaker names against official minutes. "
        "Verified against chufsd.org and ecode360 minutes."
    )

    # Save
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Saved: {data['word_count']} words, {data['speaker_count']} speakers, "
          f"{len(merged)} utterances")
    return True


def fix_names_only(path):
    """Re-run speaker name correction on an already-enriched transcript."""
    with open(path) as f:
        data = json.load(f)

    speaker_map = data.get("speaker_map", {})
    if not speaker_map:
        print(f"  Skip {os.path.basename(path)} — no speaker_map")
        return False

    canonical_names, minutes_names = _load_canonical_names()
    if not canonical_names:
        print("  No canonical names found")
        return False

    corrected_map, corrections = correct_speaker_names(
        speaker_map, canonical_names, minutes_names
    )
    if not corrections:
        print(f"  {os.path.basename(path)}: names OK")
        return False

    data["speaker_map"] = corrected_map
    for old, new, score, method in corrections:
        print(f"  \"{old}\" → \"{new}\" ({score:.2f}, {method})")

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Saved {len(corrections)} corrections")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    if sys.argv[1] == "--all":
        # Enrich all transcripts (both ChampDS and YouTube)
        paths = sorted(
            glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-*.json"))
        )
        enriched = 0
        for path in paths:
            print(f"\n{os.path.basename(path)}:")
            if enrich_transcript(path):
                enriched += 1
        print(f"\nEnriched {enriched}/{len(paths)} transcripts")

    elif sys.argv[1] == "--fix-names":
        # Re-run name correction only (works on already-enriched transcripts)
        paths = sorted(
            glob.glob(os.path.join(TRANSCRIPTS_DIR, "transcript-*.json"))
        )
        fixed = 0
        for path in paths:
            if fix_names_only(path):
                fixed += 1
        print(f"\nFixed names in {fixed}/{len(paths)} transcripts")

    else:
        path = sys.argv[1]
        if not os.path.exists(path):
            path = os.path.join(TRANSCRIPTS_DIR, sys.argv[1])
        if not os.path.exists(path):
            print(f"File not found: {sys.argv[1]}")
            return
        enrich_transcript(path)


if __name__ == "__main__":
    main()
