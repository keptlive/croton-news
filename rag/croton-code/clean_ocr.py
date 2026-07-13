#!/usr/bin/env python3
"""
Clean OCR artifacts from local law text files using Gemini Flash.
Preserves all factual content — only fixes formatting and spelling.
"""

import json
import os
import sys
import time
import requests

TEXT_DIR = "local-laws-text"
ORIGINALS_DIR = "local-laws-text-originals"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.0-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = """You are an OCR correction tool for legal documents. You will receive text extracted from scanned PDFs of Village of Croton-on-Hudson local laws.

Your job:
1. Fix OCR spelling errors (e.g. "constmction" → "construction", "stmcture" → "structure", "ofthe" → "of the")
2. Fix broken words and random symbols inserted by OCR (e.g. "p r o v i d e d" → "provided")
3. Remove garbled header/footer text from the scanned form (e.g. "7" y^i«/7/" or "^l-v.:. N p j")
4. Fix ligature artifacts (ﬁ → fi, ﬂ → fl)
5. Clean up spacing and formatting for readability
6. Preserve section symbols § and all legal numbering exactly

CRITICAL: Do NOT change any factual content. Every number, date, section reference, name, dollar amount, measurement, and legal term must remain exactly as stated. If you are unsure whether something is an OCR error or intentional, leave it as-is.

Keep the YAML frontmatter (--- block at the top) exactly as-is.

Return ONLY the cleaned text, nothing else."""


def clean_file(filepath):
    """Send file to Gemini Flash for OCR cleanup."""
    with open(filepath) as f:
        text = f.read()

    # Skip tiny files
    if len(text.strip()) < 100:
        return text

    url = f"{BASE_URL}/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{"text": f"{SYSTEM_PROMPT}\n\n---\n\nDocument to clean:\n\n{text}"}]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        }
    }

    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return text

    cleaned = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    if not cleaned or len(cleaned) < len(text) * 0.5:
        # Safety check: if output is way shorter, something went wrong
        print(f"  WARNING: output too short ({len(cleaned)} vs {len(text)}), keeping original")
        return text

    return cleaned


def main():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY not set")
        sys.exit(1)

    files = sorted(f for f in os.listdir(TEXT_DIR) if f.endswith(".txt"))
    print(f"Cleaning {len(files)} files with {MODEL}...")

    cleaned = 0
    skipped = 0
    errors = 0

    for i, fname in enumerate(files):
        filepath = os.path.join(TEXT_DIR, fname)

        try:
            result = clean_file(filepath)
            with open(filepath, "w") as f:
                f.write(result)
            cleaned += 1
        except Exception as e:
            print(f"  ERROR {fname}: {e}")
            errors += 1
            if "429" in str(e):
                print("  Rate limited, waiting 30s...")
                time.sleep(30)

        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(files)} ({cleaned} cleaned, {errors} errors)")

        time.sleep(0.5)  # rate limit

    print(f"\nDone: {cleaned} cleaned, {skipped} skipped, {errors} errors")

    # Verify no data loss
    print("\nVerifying no data loss...")
    orig_words = 0
    clean_words = 0
    for fname in files:
        orig = os.path.join(ORIGINALS_DIR, fname)
        clean = os.path.join(TEXT_DIR, fname)
        if os.path.exists(orig) and os.path.exists(clean):
            orig_words += len(open(orig).read().split())
            clean_words += len(open(clean).read().split())

    pct = (clean_words / orig_words * 100) if orig_words else 0
    print(f"  Original: {orig_words:,} words")
    print(f"  Cleaned:  {clean_words:,} words ({pct:.1f}%)")
    if pct < 90:
        print("  WARNING: significant word count drop — check for data loss!")
    elif pct > 110:
        print("  Note: word count increased (likely from fixing broken words)")
    else:
        print("  Word count within expected range.")


if __name__ == "__main__":
    main()
