"""
LLM-based speaker attribution sanity check.

Sends transcript segments to Gemini and asks it to verify:
1. Does each speaker's content match who they're labeled as?
2. Are there any obvious misattributions?
3. Do moderator/candidate roles stay consistent?
"""
import json
import os
import urllib.request
import urllib.parse
import time

TRANSCRIPT_PATH = "/opt/croton-news/rag/transcripts/transcript-yt-_3mId9iaSps.json"

# Load env
_env_path = "/opt/croton-news/rag/.env"
with open(_env_path) as f:
    for line in f:
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"


def call_gemini(prompt, max_tokens=4096):
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
    }
    req = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"ERROR: {e}"


def chunk_transcript(utterances, chunk_size=50):
    """Split utterances into chunks for LLM review."""
    chunks = []
    for i in range(0, len(utterances), chunk_size):
        chunk = utterances[i:i + chunk_size]
        text = "\n".join(
            f"[{u['timestamp']}] {u['speaker']}: {u['text']}"
            for u in chunk
        )
        chunks.append({"index": i, "text": text, "utterances": chunk})
    return chunks


def verify_chunk(chunk_text, speaker_info, chunk_num, total_chunks):
    prompt = f"""You are a transcript editor verifying speaker attribution accuracy.

CONTEXT: This is a Board of Education candidate forum in Croton-on-Hudson, NY (April 23, 2026).

CONFIRMED SPEAKERS AND ROLES:
{speaker_info}

TRANSCRIPT SEGMENT ({chunk_num}/{total_chunks}):
{chunk_text}

TASK: Review this segment for speaker attribution errors. Check:
1. Does each person's dialogue match their role? (Moderators ask questions, candidates answer)
2. Are there any lines where a candidate seems to be speaking as a moderator or vice versa?
3. Are there any lines where someone refers to themselves in third person (sign of misattribution)?
4. Do adjacent utterances from the same speaker flow naturally, or does it seem like two different people?
5. Any other inconsistencies?

Respond in this exact JSON format:
{{
  "issues": [
    {{
      "timestamp": "MM:SS",
      "speaker": "Name",
      "problem": "description of the issue",
      "suggested_fix": "what it should probably be"
    }}
  ],
  "notes": "any general observations about this segment",
  "confidence": "high/medium/low — how confident you are the attributions are correct"
}}

If no issues found, return {{"issues": [], "notes": "...", "confidence": "high"}}
"""
    return call_gemini(prompt)


def main():
    with open(TRANSCRIPT_PATH) as f:
        t = json.load(f)

    speaker_info = "\n".join(
        f"- {name}: {role}"
        for name, role in t.get("confirmed_speakers", {}).items()
    )

    utterances = t["utterances"]
    chunks = chunk_transcript(utterances, chunk_size=60)
    total = len(chunks)

    print(f"Verifying {len(utterances)} utterances in {total} chunks...")
    print(f"Speakers: {speaker_info}\n")

    all_issues = []
    all_notes = []

    for i, chunk in enumerate(chunks):
        chunk_num = i + 1
        print(f"  Chunk {chunk_num}/{total} (utterances {chunk['index']}-{chunk['index']+len(chunk['utterances'])-1})...", end=" ", flush=True)

        result_text = verify_chunk(chunk["text"], speaker_info, chunk_num, total)

        try:
            # Extract JSON from response (may be wrapped in markdown)
            json_str = result_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            result = json.loads(json_str.strip())
            issues = result.get("issues", [])
            confidence = result.get("confidence", "unknown")
            notes = result.get("notes", "")

            if issues:
                print(f"⚠ {len(issues)} issues (confidence: {confidence})")
                for issue in issues:
                    print(f"    [{issue.get('timestamp','')}] {issue.get('speaker','')}: {issue.get('problem','')}")
                    if issue.get("suggested_fix"):
                        print(f"      → Fix: {issue['suggested_fix']}")
                    all_issues.append(issue)
            else:
                print(f"✓ clean (confidence: {confidence})")

            if notes:
                all_notes.append(f"Chunk {chunk_num}: {notes}")

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"⚠ parse error: {e}")
            print(f"    Raw: {result_text[:200]}")

        time.sleep(1)  # Rate limiting

    # Summary
    print(f"\n{'='*60}")
    print(f"VERIFICATION COMPLETE")
    print(f"{'='*60}")
    print(f"Total issues found: {len(all_issues)}")

    if all_issues:
        print("\nAll issues:")
        for issue in all_issues:
            print(f"  [{issue.get('timestamp','')}] {issue.get('speaker','')}")
            print(f"    Problem: {issue.get('problem','')}")
            if issue.get("suggested_fix"):
                print(f"    Fix: {issue['suggested_fix']}")

    if all_notes:
        print("\nNotes:")
        for note in all_notes:
            print(f"  {note}")

    # Save verification results
    t["llm_verification"] = {
        "issues": all_issues,
        "notes": all_notes,
        "total_chunks": total,
        "model": "gemini-2.0-flash",
    }

    with open(TRANSCRIPT_PATH, "w") as f:
        json.dump(t, f, indent=2)

    print(f"\nResults saved to transcript.")
    return all_issues


if __name__ == "__main__":
    main()
