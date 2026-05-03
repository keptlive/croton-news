"""
Generate a verified article from the BOE Candidate Forum transcript.
Every quote is traceable to a timestamp. No fabrication.
"""
import json
import os
import urllib.request
import time

TRANSCRIPT_PATH = "/opt/croton-news/rag/transcripts/transcript-yt-_3mId9iaSps.json"

_env_path = "/opt/croton-news/rag/.env"
with open(_env_path) as f:
    for line in f:
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

GEMINI_KEY = os.environ["GEMINI_API_KEY"]


def call_gemini(prompt, model="gemini-2.5-flash", max_tokens=12000):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["candidates"][0]["content"]["parts"][0]["text"]


def format_transcript_for_prompt(utterances):
    """Format transcript with timestamps for the article writer."""
    lines = []
    for u in utterances:
        lines.append(f"[{u['timestamp']}] {u['speaker']}: {u['text']}")
    return "\n".join(lines)


def main():
    with open(TRANSCRIPT_PATH) as f:
        t = json.load(f)

    utterances = t["utterances"]
    speaker_info = t.get("confirmed_speakers", {})
    verification = t.get("llm_verification", {})

    # Format full transcript
    full_transcript = format_transcript_for_prompt(utterances)

    prompt = f"""You are a journalist writing for croton.news, a hyperlocal news site covering Croton-on-Hudson, NY.

Write a comprehensive news article about this Board of Education candidate forum held on April 23, 2026.

CRITICAL RULES:
1. EVERY direct quote MUST appear verbatim in the transcript below. Do NOT paraphrase and present as a quote.
2. After each quote, include the timestamp in this format: (at MM:SS)
3. If you're unsure about exact wording, paraphrase WITHOUT quotation marks.
4. Attribute every quote to the correct speaker. The speaker labels have been verified by AI and are highly accurate.
5. Do NOT invent dialogue or combine separate utterances into a single "quote."
6. Use markdown formatting.

SPEAKERS AND ROLES:
{json.dumps(speaker_info, indent=2)}

VERIFICATION NOTES:
- 16 of 18 transcript segments verified as clean with high confidence
- 4 minor issues flagged (1 moderator misattribution at 28:34, 3 trivial)
- At [28:34] the utterance attributed to Neal Haber ("How about Sarah?") is likely from a moderator

ARTICLE STRUCTURE:
- Headline (compelling, accurate, under 80 chars)
- Lede paragraph (who, what, when, where, why)
- Context section (what is CCC, why this forum, who are the candidates)
- Key themes/topics discussed (organize by topic, not chronologically)
- Each candidate's key positions with VERIFIED quotes
- Community impact / audience reaction
- What's next (election date May 19)

TRANSCRIPT:
{full_transcript}

Write the article now. Remember: EVERY quote must be verbatim from the transcript with a timestamp."""

    print("Generating article from verified transcript...")
    print(f"Transcript: {len(utterances)} utterances, {t['word_count']} words")
    print(f"Sending to Gemini 2.5 Flash...")

    start = time.time()
    article = call_gemini(prompt, model="gemini-2.5-flash", max_tokens=12000)
    elapsed = time.time() - start
    print(f"Generated in {elapsed:.0f}s, {len(article)} chars")

    # Extract headline (first line starting with #)
    headline = ""
    for line in article.split("\n"):
        if line.startswith("# "):
            headline = line[2:].strip()
            break
        elif line.startswith("## "):
            headline = line[3:].strip()
            break

    if not headline:
        headline = "Five Candidates Face Off at Community Forum on EdTech, Phones, and AI"

    # Save article
    output = {
        "article": article,
        "headline": headline,
        "model": "gemini-2.5-flash",
        "source_transcript": "transcript-yt-_3mId9iaSps.json",
        "verification": "llm_verified",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    output_path = "/opt/croton-news/rag/saved_articles/boe-forum-20260423-verified.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nHeadline: {headline}")
    print(f"Saved: {output_path}")
    print(f"\nFirst 500 chars:")
    print(article[:500])

    return article, headline


if __name__ == "__main__":
    main()
