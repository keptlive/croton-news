"""
Speaker verification + mapping for BOE Candidate Forum transcript.

Uses context clues from the transcript + entity database to:
1. Map Speaker N -> real name
2. Verify all attributions are consistent
3. Flag any speaker switches that seem wrong
"""
import json
import os
import sqlite3
import sys

RAG_DB = "/opt/croton-news/rag/rag.db"
TRANSCRIPT_PATH = "/opt/croton-news/rag/transcripts/transcript-yt-_3mId9iaSps.json"

# Canonical name spellings from entity DB + BoardDocs
KNOWN_NAMES = {
    "Anamika Bhatnagar": "Board of Education Trustee",
    "Sarah Carrier": "Board of Education Trustee",
    "Neal Haber": "Board of Education Trustee",
    "Omar Mayyasi": "Board of Education Trustee",
    "Theo Oshiro": "Board of Education Trustee",
    "Ana Teague": "Board of Education President",
    "Allison Samuels": "Board of Education Trustee",
    "Stephen Walker": "Superintendent of Schools",
    "Jill Anderson": "CCC Founder / Forum Moderator",
}

def load_transcript():
    with open(TRANSCRIPT_PATH) as f:
        return json.load(f)

def identify_speakers(transcript):
    """Use introduction text to map speakers to names."""
    mapping = {}
    evidence = {}

    for u in transcript["utterances"]:
        speaker = u["speaker"]
        text = u["text"]
        ts = u["timestamp"]

        # "My name is X" pattern
        if "my name is" in text.lower():
            # Extract the name after "my name is"
            idx = text.lower().index("my name is") + len("my name is")
            after = text[idx:idx+40].strip().rstrip(".,;:")
            name = after.split(".")[0].split(",")[0].strip()
            if len(name) > 2 and len(name) < 40:
                mapping[speaker] = name
                evidence[speaker] = f"[{ts}] Self-intro: '{text[:100]}'"

        # "I'm X" at start of an introduction
        if text.lower().startswith("hi. i'm ") or text.lower().startswith("i'm ") or "i am " in text.lower()[:30]:
            import re
            m = re.search(r"(?:I'm|I am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
            if m and speaker not in mapping:
                name = m.group(1)
                if len(name) > 3:
                    mapping[speaker] = name
                    evidence[speaker] = f"[{ts}] Self-intro: '{text[:100]}'"

        # "For those who may not know me, I'm X"
        if "i'm" in text.lower() and speaker not in mapping:
            import re
            m = re.search(r"I'm\s+([A-Z][a-z]+\s+[A-Z][a-z]+)", text)
            if m:
                mapping[speaker] = m.group(1)
                evidence[speaker] = f"[{ts}] Intro: '{text[:100]}'"

        # Moderator calling on candidates: "start with miss Carrier", "Thank you, Sarah"
        if "miss carrier" in text.lower() or "mister carrier" in text.lower() or "ms. carrier" in text.lower():
            # The NEXT different speaker is likely Carrier
            pass  # handled in cross-ref below

    return mapping, evidence

def cross_reference_names(mapping):
    """Match extracted names to canonical spellings."""
    corrected = {}
    for speaker, raw_name in mapping.items():
        raw_lower = raw_name.lower()
        matched = False
        for canonical in KNOWN_NAMES:
            # Check if raw name matches any part of canonical
            parts = canonical.lower().split()
            if any(p in raw_lower for p in parts):
                corrected[speaker] = canonical
                matched = True
                break
        if not matched:
            corrected[speaker] = raw_name  # keep as-is if no match
    return corrected

def verify_consistency(transcript, mapping):
    """Check for speaker attribution issues."""
    issues = []
    utterances = transcript["utterances"]

    for i, u in enumerate(utterances):
        speaker = u["speaker"]
        text = u["text"]

        # Check: does a speaker refer to themselves in third person?
        if speaker in mapping:
            name = mapping[speaker]
            first_name = name.split()[0].lower()
            if f"thank you, {first_name}" in text.lower() or f"thank you {first_name}" in text.lower():
                # This speaker is thanking someone with their own name — likely misattributed
                issues.append({
                    "type": "self_reference",
                    "utterance_idx": i,
                    "timestamp": u["timestamp"],
                    "speaker": speaker,
                    "mapped_name": name,
                    "text": text[:120],
                    "note": f"Speaker {speaker} ({name}) appears to thank '{first_name}' — possible misattribution"
                })

        # Check: very short utterances between long ones from different speakers
        # (could be crosstalk or diarization errors)
        if len(text.split()) <= 3 and i > 0 and i < len(utterances) - 1:
            prev_speaker = utterances[i-1]["speaker"]
            next_speaker = utterances[i+1]["speaker"]
            if prev_speaker == next_speaker and speaker != prev_speaker:
                issues.append({
                    "type": "orphan_utterance",
                    "utterance_idx": i,
                    "timestamp": u["timestamp"],
                    "speaker": speaker,
                    "text": text,
                    "note": f"Short utterance sandwiched between {prev_speaker} utterances — likely misattributed"
                })

        # Check: rapid speaker switches (less than 1 second)
        if i > 0:
            prev = utterances[i-1]
            gap = u["start"] - prev["end"]
            if gap < 0.3 and speaker != prev["speaker"] and len(text.split()) > 5:
                issues.append({
                    "type": "rapid_switch",
                    "utterance_idx": i,
                    "timestamp": u["timestamp"],
                    "speaker": speaker,
                    "prev_speaker": prev["speaker"],
                    "gap_ms": int(gap * 1000),
                    "text": text[:80],
                    "note": f"Speaker changed with only {gap*1000:.0f}ms gap"
                })

    return issues

def main():
    print("=" * 60)
    print("SPEAKER VERIFICATION — BOE Candidate Forum 2026-04-23")
    print("=" * 60)

    t = load_transcript()
    utterances = t["utterances"]
    print(f"\nTranscript: {len(utterances)} utterances, {t['word_count']} words, {t['speaker_count']} speakers")

    # Step 1: Identify speakers from self-introductions
    print("\n--- STEP 1: Speaker Identification ---")
    raw_mapping, evidence = identify_speakers(t)
    for speaker, name in sorted(raw_mapping.items()):
        print(f"  {speaker}: {name}")
        print(f"    Evidence: {evidence.get(speaker, 'none')}")

    # Step 2: Match to canonical names
    print("\n--- STEP 2: Canonical Name Matching ---")
    mapping = cross_reference_names(raw_mapping)
    for speaker, name in sorted(mapping.items()):
        role = KNOWN_NAMES.get(name, "Unknown role")
        print(f"  {speaker} -> {name} ({role})")

    # Identify unmapped speakers
    all_speakers = set(u["speaker"] for u in utterances)
    unmapped = all_speakers - set(mapping.keys())
    if unmapped:
        print(f"\n  UNMAPPED: {', '.join(sorted(unmapped))}")
        for s in sorted(unmapped):
            # Show their first few utterances for manual ID
            examples = [u for u in utterances if u["speaker"] == s][:3]
            for e in examples:
                print(f"    [{e['timestamp']}] {e['text'][:100]}")

    # Step 3: Consistency check
    print("\n--- STEP 3: Consistency Verification ---")
    issues = verify_consistency(t, mapping)
    if issues:
        print(f"  Found {len(issues)} potential issues:")
        for issue in issues[:20]:
            print(f"  [{issue['timestamp']}] {issue['type']}: {issue['note']}")
            if issue.get("text"):
                print(f"    Text: {issue['text'][:100]}")
    else:
        print("  No issues found")

    # Step 4: Speaker stats
    print("\n--- STEP 4: Speaker Stats ---")
    from collections import Counter
    sc = Counter(u["speaker"] for u in utterances)
    wc = Counter()
    for u in utterances:
        wc[u["speaker"]] += len(u["text"].split())
    for speaker, count in sc.most_common():
        name = mapping.get(speaker, "???")
        words = wc[speaker]
        print(f"  {speaker:10s} -> {name:25s}  {count:4d} utterances, {words:5d} words")

    # Save mapping
    t["speaker_map"] = mapping
    t["verified"] = False
    t["verification_issues"] = issues
    with open(TRANSCRIPT_PATH, "w") as f:
        json.dump(t, f, indent=2)
    print(f"\nSaved speaker_map to transcript. {len(issues)} issues flagged for human review.")

if __name__ == "__main__":
    main()
