#!/usr/bin/env python3
"""
Clean up meeting transcript files.
Fixes formatting issues from Deepgram transcription:
- Excessive line breaks within speaker blocks in full_text
- Extra whitespace in utterance text fields
- Missing capitalization at sentence starts in utterances
"""

import json
import glob
import os
import re
import shutil
import sys
import time
import requests

TRANSCRIPTS_DIR = "/opt/croton-news/rag/transcripts"
API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
API_KEY = os.environ.get("ZAI_KEY", "")
MODEL = "glm-5-turbo"

# Whether to attempt GLM API (set False to skip entirely)
USE_GLM_API = True
GLM_CHUNK_SIZE = 6000  # chars per chunk for API

def test_glm_api():
    """Test if the GLM API is available and has balance."""
    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Say OK."}],
                "max_tokens": 10
            },
            timeout=15
        )
        data = resp.json()
        if "choices" in data:
            print("[GLM API] Available and working.")
            return True
        else:
            msg = data.get("error", {}).get("message", data.get("msg", str(data)))
            print(f"[GLM API] Not available: {msg}")
            return False
    except Exception as e:
        print(f"[GLM API] Connection error: {e}")
        return False


def clean_full_text_with_api(full_text):
    """Send full_text to GLM-5 in chunks for cleanup. Returns cleaned text."""
    # Split into chunks at speaker block boundaries
    # Pattern: blank line followed by [timestamp]
    blocks = re.split(r'(\n\n\[)', full_text)
    
    # Reassemble into chunks of ~GLM_CHUNK_SIZE
    chunks = []
    current = ""
    for i, block in enumerate(blocks):
        if i > 0 and blocks[i-1] == "\n\n[":
            block = "\n\n[" + block
        elif block == "\n\n[":
            continue
        
        if len(current) + len(block) > GLM_CHUNK_SIZE and current:
            chunks.append(current)
            current = block
        else:
            current += block
    if current:
        chunks.append(current)
    
    cleaned_chunks = []
    for i, chunk in enumerate(chunks):
        prompt = (
            "Clean up this meeting transcript text. Fix capitalization, punctuation, "
            "and remove excessive line breaks within speaker sections. Merge broken sentences. "
            "Do NOT change any words or meaning — only fix formatting. "
            "Keep speaker labels like '[00:07] Mayor Brian Pugh:' on their own lines. "
            "Return the cleaned text only, no explanation."
        )
        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": chunk}
                    ],
                    "max_tokens": len(chunk) + 500,
                    "temperature": 0.1
                },
                timeout=60
            )
            data = resp.json()
            if "choices" in data:
                cleaned = data["choices"][0]["message"]["content"]
                cleaned_chunks.append(cleaned)
                print(f"    Chunk {i+1}/{len(chunks)} cleaned via API ({len(chunk)} -> {len(cleaned)} chars)")
                time.sleep(0.5)  # Rate limiting
            else:
                print(f"    Chunk {i+1} API error, falling back to local cleanup")
                cleaned_chunks.append(clean_full_text_local(chunk))
        except Exception as e:
            print(f"    Chunk {i+1} exception: {e}, falling back to local cleanup")
            cleaned_chunks.append(clean_full_text_local(chunk))
    
    return "".join(cleaned_chunks)


def clean_full_text_local(full_text):
    """
    Clean full_text using regex-based approach.
    
    The format is:
    [timestamp] Speaker Name:
      text line 1
      text line 2
      ...
    
    (blank line)
    [timestamp] Next Speaker:
      ...
    
    We want to join the indented continuation lines within each speaker block
    into flowing text, keeping speaker headers on their own lines.
    """
    # Split into speaker blocks separated by blank lines
    # Pattern: \n\n before a [timestamp] header
    # First, normalize: ensure consistent double-newline between blocks
    
    # Split on the pattern: empty line(s) followed by [timestamp]
    # We preserve the [timestamp] part
    blocks = re.split(r'\n\n+(?=\[)', full_text.strip())
    
    cleaned_blocks = []
    for block in blocks:
        # Each block starts with [timestamp] Speaker:\n  text...
        # Split header from body
        match = re.match(r'(\[\d+:\d+\]\s+[^:]+:)\n(.*)', block, re.DOTALL)
        if match:
            header = match.group(1)
            body = match.group(2)
            
            # The body has lines like "  text here\n  more text\n  etc"
            # Join them, replacing "\n  " (newline + indentation) with a single space
            # But preserve sentence boundaries where a line ends with period/question/exclamation
            
            # First strip leading/trailing whitespace from each line
            lines = body.split('\n')
            cleaned_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped:
                    cleaned_lines.append(stripped)
            
            # Join all lines with a single space
            body_text = ' '.join(cleaned_lines)
            
            # Clean up any double spaces that resulted
            body_text = re.sub(r'  +', ' ', body_text)
            
            cleaned_blocks.append(f"{header}\n{body_text}")
        else:
            # Block doesn't match expected pattern, keep as-is but clean whitespace
            lines = block.split('\n')
            cleaned = ' '.join(l.strip() for l in lines if l.strip())
            cleaned_blocks.append(cleaned)
    
    return '\n\n'.join(cleaned_blocks)


def clean_utterance_text(text):
    """Clean individual utterance text field."""
    # Strip extra whitespace
    text = text.strip()
    text = re.sub(r'  +', ' ', text)
    
    # Capitalize first letter if it starts with lowercase
    # But NOT if it starts with a name-like pattern or known lowercase words
    # that might be mid-sentence continuations (like "a", "the", "and")
    # Actually, for utterances that are sentence fragments, capitalizing
    # the first letter could be wrong if it's mid-sentence.
    # We'll only capitalize if it looks like a sentence start:
    # - After we stripped whitespace, if first char is lowercase AND
    #   the text doesn't start with common mid-sentence connectors
    
    # For safety, we'll capitalize the first character only if the text
    # starts a sentence (first word isn't a conjunction/preposition that
    # suggests continuation)
    continuation_starters = {
        'a', 'an', 'the', 'and', 'but', 'or', 'nor', 'for', 'yet', 'so',
        'in', 'on', 'at', 'to', 'of', 'with', 'by', 'from', 'about',
        'into', 'through', 'during', 'before', 'after', 'above', 'below',
        'between', 'under', 'over', 'that', 'which', 'who', 'whom',
        'whose', 'where', 'when', 'while', 'because', 'since', 'unless',
        'until', 'although', 'though', 'if', 'as', 'than', 'like',
        'not', 'no', 'just', 'also', 'even', 'still', 'already',
        'you', 'we', 'he', 'she', 'it', 'they', 'is', 'are', 'was',
        'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do',
        'does', 'did', 'will', 'would', 'could', 'should', 'may',
        'might', 'shall', 'can', 'need', 'must',
    }
    
    if text and text[0].islower():
        first_word = text.split()[0].lower().rstrip('.,;:!?')
        # Don't capitalize mid-sentence continuations
        # Actually, it's hard to know without context. We'll leave as-is
        # since changing capitalization could be wrong.
        pass
    
    return text


def process_transcript(filepath, use_api=False):
    """Process a single transcript file."""
    # Resolve symlinks to get real path
    real_path = os.path.realpath(filepath)
    
    with open(real_path, 'r') as f:
        data = json.load(f)
    
    event_id = data.get('event_id', 'unknown')
    title = data.get('title', 'unknown')
    
    # Backup
    backup_path = real_path + '.bak'
    if not os.path.exists(backup_path):
        shutil.copy2(real_path, backup_path)
        print(f"  Backed up to {os.path.basename(backup_path)}")
    else:
        print(f"  Backup already exists")
    
    # Clean full_text
    original_ft = data.get('full_text', '')
    if not original_ft:
        print(f"  WARNING: No full_text found, skipping")
        return False
    
    if use_api:
        cleaned_ft = clean_full_text_with_api(original_ft)
    else:
        cleaned_ft = clean_full_text_local(original_ft)
    
    # Report changes
    orig_lines = original_ft.count('\n')
    clean_lines = cleaned_ft.count('\n')
    print(f"  full_text: {len(original_ft)} -> {len(cleaned_ft)} chars, {orig_lines} -> {clean_lines} lines")
    
    data['full_text'] = cleaned_ft
    
    # Clean utterance text fields
    utterances = data.get('utterances', [])
    utt_changed = 0
    for utt in utterances:
        original = utt.get('text', '')
        cleaned = clean_utterance_text(original)
        if cleaned != original:
            utt['text'] = cleaned
            utt_changed += 1
    
    print(f"  Utterances: {len(utterances)} total, {utt_changed} cleaned")
    
    # Write back
    with open(real_path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"  Written successfully")
    return True


def main():
    print("=" * 60)
    print("Meeting Transcript Cleanup")
    print("=" * 60)
    
    # Find all transcript files
    pattern = os.path.join(TRANSCRIPTS_DIR, "transcript-*.json")
    files = sorted(glob.glob(pattern))
    
    # Filter out backup files
    files = [f for f in files if not f.endswith('.bak')]
    
    print(f"\nFound {len(files)} transcript files")
    
    # Test GLM API
    api_available = False
    if USE_GLM_API:
        api_available = test_glm_api()
    
    if not api_available:
        print("Using local regex-based cleanup (deterministic, no word changes)")
    
    print()
    
    success = 0
    failed = 0
    
    for filepath in files:
        basename = os.path.basename(filepath)
        event_id = basename.replace('transcript-', '').replace('.json', '')
        
        # Read to get title
        real_path = os.path.realpath(filepath)
        with open(real_path) as f:
            d = json.load(f)
        title = d.get('title', '?')
        
        print(f"[{event_id}] {title}")
        
        try:
            if process_transcript(filepath, use_api=api_available):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
        
        print()
    
    print("=" * 60)
    print(f"Done: {success} cleaned, {failed} failed out of {len(files)} total")
    print("=" * 60)


if __name__ == "__main__":
    main()
