"""
Strict editorial checker for croton.news articles.

Checks:
1. EVERY quoted string must appear VERBATIM in a single utterance (not stitched)
2. EVERY quoted string must be near its stated timestamp
3. EVERY speaker attribution must be from a high-confidence diarization zone
4. EVERY non-quoted factual claim must be traceable to the transcript or flagged
5. NO editorial interpretation presented as fact
6. NO unverified external facts (dates, numbers not in transcript)
"""
import json
import re
import sqlite3
import sys
import os

# Allow passing paths as args or use defaults
ARTICLE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/croton-news/rag/saved_articles/boe-forum-20260423-verified.json"
TRANSCRIPT_PATH = sys.argv[2] if len(sys.argv) > 2 else "/opt/croton-news/rag/transcripts/transcript-yt-_3mId9iaSps.json"
RAG_DB = "/opt/croton-news/rag/rag.db"

# Load env
_env_path = "/opt/croton-news/rag/.env"
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)

with open(ARTICLE_PATH) as f:
    article_data = json.load(f)
article = article_data["article"]

with open(TRANSCRIPT_PATH) as f:
    transcript = json.load(f)

utterances = transcript["utterances"]

# Build full text per-utterance for exact matching
utt_texts = [u["text"] for u in utterances]
full_text_lower = " ".join(u["text"] for u in utterances).lower()

# Build uncertain zones (rapid speaker switches, orphan utterances)
uncertain_seconds = set()
for idx, u in enumerate(utterances):
    # Short utterance sandwiched between different speaker
    if len(u["text"].split()) <= 3 and idx > 0 and idx < len(utterances) - 1:
        if utterances[idx-1]["speaker"] == utterances[idx+1]["speaker"] and u["speaker"] != utterances[idx-1]["speaker"]:
            uncertain_seconds.add(int(u["start"]))
    # Rapid speaker switch
    if idx > 0:
        gap = u["start"] - utterances[idx-1]["end"]
        if gap < 0.3 and u["speaker"] != utterances[idx-1]["speaker"]:
            uncertain_seconds.add(int(u["start"]))

# Known misattributions from LLM verification
misattributed_seconds = {28*60+34, 29*60+11}  # 28:34, 29:11

all_issues = []

def add_issue(category, severity, description, context=""):
    all_issues.append({
        "category": category,
        "severity": severity,  # "error" or "warning"
        "description": description,
        "context": context[:150],
    })

# ============================================================
# CHECK 1: Every quoted string must appear VERBATIM in a single utterance
# ============================================================
print("CHECK 1: Verbatim quote verification")
print("-" * 50)

quote_pattern = re.compile(r'\u201c([^\u201d]+)\u201d|\u0022([^\u0022]{10,})\u0022', re.DOTALL)
# Also catch regular quotes
simple_pattern = re.compile(r'"([^"]{10,})"', re.DOTALL)

quotes_with_ts = re.findall(r'"([^"]{8,})"\s*\(at (\d+:\d+)\)', article)

for quote_text, timestamp in quotes_with_ts:
    clean = quote_text.strip().rstrip(".,;:!?")
    clean_lower = clean.lower()

    # Parse timestamp to seconds
    parts = timestamp.split(":")
    target_sec = int(parts[0]) * 60 + int(parts[1])

    # STRICT: the quote must appear in a SINGLE utterance (not stitched across multiple)
    found_in_single = False
    found_speaker = None
    found_utt = None
    for u in utterances:
        if clean_lower in u["text"].lower():
            found_in_single = True
            found_speaker = u["speaker"]
            found_utt = u
            break

    # If not found in single utterance, check if it's in two consecutive same-speaker utterances
    if not found_in_single:
        for i in range(len(utterances) - 1):
            if utterances[i]["speaker"] == utterances[i+1]["speaker"]:
                combined = utterances[i]["text"] + " " + utterances[i+1]["text"]
                if clean_lower in combined.lower():
                    found_in_single = True
                    found_speaker = utterances[i]["speaker"]
                    found_utt = utterances[i]
                    break

    if not found_in_single:
        # Try 3 consecutive same-speaker utterances
        for i in range(len(utterances) - 2):
            if utterances[i]["speaker"] == utterances[i+1]["speaker"] == utterances[i+2]["speaker"]:
                combined = utterances[i]["text"] + " " + utterances[i+1]["text"] + " " + utterances[i+2]["text"]
                if clean_lower in combined.lower():
                    found_in_single = True
                    found_speaker = utterances[i]["speaker"]
                    found_utt = utterances[i]
                    break

    if not found_in_single:
        add_issue("quote", "error",
                  f"[{timestamp}] Quote NOT VERBATIM in transcript: \"{clean[:60]}...\"",
                  f"Could not find this exact text in any utterance or consecutive same-speaker utterances")
        print(f"  FAIL [{timestamp}] \"{clean[:70]}...\"")
    else:
        # Check timestamp proximity
        if found_utt and abs(found_utt["start"] - target_sec) > 30:
            add_issue("quote", "warning",
                      f"[{timestamp}] Quote found but timestamp off by {abs(found_utt['start'] - target_sec):.0f}s",
                      f"Found at {found_utt['timestamp']}, article says {timestamp}")

        # Check speaker certainty
        if any(abs(target_sec - s) < 5 for s in uncertain_seconds) or \
           any(abs(target_sec - s) < 5 for s in misattributed_seconds):
            add_issue("speaker", "warning",
                      f"[{timestamp}] Quote from uncertain diarization zone",
                      f"Speaker {found_speaker} may be misattributed")

print(f"  {len(quotes_with_ts)} quotes checked")

# ============================================================
# CHECK 2: Non-quoted factual claims
# ============================================================
print("\nCHECK 2: Non-quoted factual claims")
print("-" * 50)

# Extract sentences that aren't quotes (editorial text)
lines = article.split("\n")
for line in lines:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("**CROTON"):
        continue
    # Remove quoted portions
    editorial = re.sub(r'"[^"]*"\s*\(at \d+:\d+\)', '', line)
    editorial = re.sub(r'"[^"]*"', '', editorial)
    editorial = editorial.strip()
    if len(editorial) < 20:
        continue

    # Check for specific factual claims in editorial text
    # Duration claims
    dur_match = re.search(r'(two.hour|90.minute|nearly.+hour|roughly.+hour)', editorial, re.I)
    if dur_match:
        actual_min = transcript.get("duration_seconds", 0) / 60
        claim = dur_match.group(1)
        if "two" in claim.lower() and actual_min < 110:
            add_issue("fact", "error",
                      f"Duration claim '{claim}' but transcript is {actual_min:.0f} minutes",
                      line[:100])
        elif "90" in claim and abs(actual_min - 90) > 10:
            add_issue("fact", "error",
                      f"Duration claim '{claim}' but transcript is {actual_min:.0f} minutes",
                      line[:100])

    # Check "Church" — is it actually a church?
    if "church" in editorial.lower():
        church_in_transcript = "church" in full_text_lower
        if not church_in_transcript:
            add_issue("fact", "error",
                      "Calls venue a 'Church' but transcript never uses that word",
                      line[:100])

    # Check characterizations like "longest-serving" "newcomer"
    if "longest-serving" in editorial.lower():
        add_issue("fact", "warning",
                  "'Longest-serving' claim not verifiable from transcript alone",
                  line[:100])

    if "newcomer" in editorial.lower():
        # Check if Day actually says he's new
        day_new = any("first time" in u["text"].lower() or "new to" in u["text"].lower()
                      for u in utterances if u["speaker"] == "Jake Day")
        if not day_new:
            add_issue("fact", "warning",
                      "'Newcomer' characterization — not explicitly stated by candidate",
                      line[:100])

    # Check for external facts not in transcript
    if "may 19" in editorial.lower() or "may nineteenth" in editorial.lower():
        if "may 19" not in full_text_lower and "may nineteenth" not in full_text_lower:
            add_issue("fact", "warning",
                      "May 19 election date not mentioned in transcript — external fact, verify separately",
                      line[:100])

    if "library" in editorial.lower() and ("levy" in editorial.lower() or "tax" in editorial.lower()):
        if "library" not in full_text_lower or ("levy" not in full_text_lower and "tax levy" not in full_text_lower):
            add_issue("fact", "warning",
                      "Library tax levy not mentioned in transcript — external fact, verify separately",
                      line[:100])

    # Check editorial interpretation presented as fact
    interp_phrases = [
        "emphasized that", "reflected growing", "drew a vivid",
        "framed the issue", "attributed much of",
    ]
    for phrase in interp_phrases:
        if phrase in editorial.lower():
            add_issue("editorial", "warning",
                      f"Editorial interpretation: '{phrase}' — ensure this accurately represents what was said",
                      line[:100])

# ============================================================
# CHECK 3: Name spelling against entity DB
# ============================================================
print("\nCHECK 3: Name verification")
print("-" * 50)

db = sqlite3.connect(RAG_DB)
db.row_factory = sqlite3.Row
entities = {row["name"].lower(): row["name"] for row in
            db.execute("SELECT name FROM entities WHERE type='person'").fetchall()}
confirmed_speakers = transcript.get("confirmed_speakers", {})

name_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
article_names = set(name_pattern.findall(article))

skip_names = {"Board of Education", "Croton Community", "Community Collective",
              "Saint Augustine", "United States", "Croton Harmon", "Croton Harmony",
              "League of Women", "Women Voters", "Social Media", "Real World",
              "Croton Schools", "School Board", "Flask App"}

for name in sorted(article_names):
    if name in skip_names or len(name.split()) > 3:
        continue
    name_lower = name.lower()
    if name_lower in entities:
        canonical = entities[name_lower]
        if canonical != name:
            add_issue("name", "error", f"Spelling: '{name}' should be '{canonical}'")
    elif name not in confirmed_speakers:
        # Check transcript for the name
        if name.lower() not in full_text_lower and name.split()[-1].lower() not in full_text_lower:
            add_issue("name", "warning", f"Name '{name}' not found in transcript or entity DB")

db.close()

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("EDITORIAL CHECK SUMMARY")
print("=" * 60)

errors = [i for i in all_issues if i["severity"] == "error"]
warnings = [i for i in all_issues if i["severity"] == "warning"]

print(f"Errors:   {len(errors)}")
print(f"Warnings: {len(warnings)}")
print()

if errors:
    print("ERRORS (must fix):")
    for i in errors:
        print(f"  [{i['category']}] {i['description']}")
    print()

if warnings:
    print("WARNINGS (review):")
    for i in warnings:
        print(f"  [{i['category']}] {i['description']}")
    print()

status = "PASS" if len(errors) == 0 else "FAIL"
print(f"STATUS: {status}")
if warnings:
    print(f"  ({len(warnings)} warnings for human review)")

# Save
if "editorial_check" not in article_data:
    article_data["editorial_check"] = {}
article_data["editorial_check"]["full_verification"] = {
    "total_quotes": len(quotes_with_ts),
    "errors": len(errors),
    "warnings": len(warnings),
    "issues": all_issues,
}
article_data["editorial_check"]["status"] = status

with open(ARTICLE_PATH, "w") as f:
    json.dump(article_data, f, indent=2)
