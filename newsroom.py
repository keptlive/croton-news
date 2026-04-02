#!/usr/bin/env python3
"""
Croton News — AI Newsroom Story Writer

Reads raw meeting minutes from the ecode360 search database and generates
publish-ready local news articles using an LLM via OpenRouter.

Usage:
    python3 newsroom.py generate --doc-id 753140590
    python3 newsroom.py generate --query "budget 2025"
    python3 newsroom.py generate --committee "Board of Trustees" --latest
    python3 newsroom.py list
    python3 newsroom.py summary --doc-id 753140590
"""

import argparse
import json
import os
import sqlite3
import sys
import textwrap

import requests

ECODE360_DIR = os.path.join(os.path.dirname(__file__), "ecode360")
SEARCH_DB = os.path.join(ECODE360_DIR, "search.db")
MINUTES_DIR = os.path.join(ECODE360_DIR, "minutes")
STORIES_DIR = os.path.join(os.path.dirname(__file__), "stories")

# Load OpenRouter key
_creds_path = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "openrouter_credentials.json"
)
if os.path.exists(_creds_path):
    with open(_creds_path) as f:
        _creds = json.load(f)
    OPENROUTER_KEY = os.environ.get(
        "OPENROUTER_API_KEY", _creds.get("openrouter_api_key", "")
    )
else:
    OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.0-flash-001"  # fast, capable, free tier


SYSTEM_PROMPT = """\
You are a local newspaper reporter for croton.news, covering Croton-on-Hudson, NY.

Your job is to read raw meeting minutes, resolutions, and municipal documents and produce clear, accurate, engaging news articles for village residents.

Writing style:
- Local newspaper tone — informative, accessible, not academic
- Lead with the most newsworthy item (inverted pyramid)
- Use specific names, addresses, dollar amounts, vote tallies when present
- Keep paragraphs short (2-3 sentences max)
- Explain jargon — readers aren't municipal lawyers
- Quote directly from the minutes when interesting or significant
- Include context that helps residents understand *why* something matters
- If multiple newsworthy items exist, you may suggest splitting into separate articles

Article structure:
1. Headline (compelling, specific, under 80 chars)
2. Subhead (one sentence expanding the headline)
3. Body paragraphs
4. "What's Next" section if applicable (upcoming votes, deadlines, hearings)

Do NOT:
- Editorialize or inject opinion
- Speculate beyond what the document says
- Invent quotes not in the source material
- Add background info you don't know from the document itself

Output format: Markdown with the headline as # H1.
"""


def get_document_text(doc_id):
    """Read full text of a document."""
    path = os.path.join(MINUTES_DIR, f"{doc_id}.txt")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def get_document_meta(doc_id):
    """Get metadata for a document."""
    conn = sqlite3.connect(SEARCH_DB)
    c = conn.cursor()
    c.execute(
        "SELECT doc_id, committee, date, type, text_size, preview FROM documents WHERE doc_id = ?",
        (doc_id,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "doc_id": row[0],
        "committee": row[1],
        "date": row[2],
        "type": row[3],
        "text_size": row[4],
        "preview": row[5],
    }


def list_documents(committee=None):
    """List available documents."""
    conn = sqlite3.connect(SEARCH_DB)
    c = conn.cursor()
    if committee:
        c.execute(
            "SELECT doc_id, committee, date, type, text_size FROM documents "
            "WHERE committee = ? ORDER BY date DESC",
            (committee,),
        )
    else:
        c.execute(
            "SELECT doc_id, committee, date, type, text_size FROM documents "
            "ORDER BY committee, date DESC"
        )
    rows = c.fetchall()
    conn.close()
    return [
        {"doc_id": r[0], "committee": r[1], "date": r[2], "type": r[3], "text_size": r[4]}
        for r in rows
    ]


def search_documents(query, limit=5):
    """Search documents by keyword."""
    conn = sqlite3.connect(SEARCH_DB)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT doc_id FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    )
    doc_ids = [r[0] for r in c.fetchall()]
    conn.close()
    return [get_document_meta(d) for d in doc_ids if get_document_meta(d)]


def call_llm(system, user_msg, max_tokens=4000):
    """Call OpenRouter LLM."""
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def generate_article(doc_id):
    """Generate a news article from a document."""
    meta = get_document_meta(doc_id)
    if not meta:
        print(f"Document {doc_id} not found in index.")
        return None

    text = get_document_text(doc_id)
    if not text:
        print(f"No text file for document {doc_id}.")
        return None

    # Truncate very long documents to fit context
    if len(text) > 60000:
        text = text[:60000] + "\n\n[... document truncated for length ...]"

    user_msg = (
        f"Here are the raw {meta['type']} from the {meta['committee']}, "
        f"dated {meta['date'] or 'unknown date'}.\n\n"
        f"Read through them carefully and write one or more news articles "
        f"covering the most newsworthy items for Croton-on-Hudson residents.\n\n"
        f"---\n\n{text}"
    )

    print(f"Generating article from: {meta['committee']} — {meta['date']} ({meta['text_size']:,} chars)")
    print(f"Using model: {MODEL}")

    article = call_llm(SYSTEM_PROMPT, user_msg)

    # Save article
    os.makedirs(STORIES_DIR, exist_ok=True)
    safe_date = (meta["date"] or "unknown").replace(" ", "-").replace(",", "").replace("/", "-")
    safe_committee = meta["committee"].replace(" ", "-").lower()
    filename = f"{safe_committee}_{safe_date}_{doc_id}.md"
    filepath = os.path.join(STORIES_DIR, filename)
    with open(filepath, "w") as f:
        f.write(f"<!-- Source: ecode360 doc {doc_id} | {meta['committee']} | {meta['date']} -->\n")
        f.write(f"<!-- Generated: {__import__('datetime').datetime.now().isoformat()} -->\n\n")
        f.write(article)

    print(f"Saved: {filepath}")
    return article


def generate_summary(doc_id):
    """Generate a brief summary of a document (no full article)."""
    meta = get_document_meta(doc_id)
    text = get_document_text(doc_id)
    if not meta or not text:
        print(f"Document {doc_id} not found.")
        return None

    if len(text) > 60000:
        text = text[:60000] + "\n\n[... truncated ...]"

    user_msg = (
        f"Summarize the key decisions, votes, and newsworthy items from these "
        f"{meta['committee']} {meta['type']} ({meta['date'] or 'unknown date'}) "
        f"in 5-10 bullet points. Focus on what matters to Croton residents.\n\n"
        f"---\n\n{text}"
    )

    summary = call_llm(SYSTEM_PROMPT, user_msg, max_tokens=1500)
    print(summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Croton News AI Newsroom")
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List available documents")
    p_list.add_argument("--committee", help="Filter by committee name")

    # generate
    p_gen = sub.add_parser("generate", help="Generate article from document")
    p_gen.add_argument("--doc-id", help="Document ID")
    p_gen.add_argument("--query", help="Search query to find document")
    p_gen.add_argument("--committee", help="Committee name (with --latest)")
    p_gen.add_argument("--latest", action="store_true", help="Use most recent doc for committee")
    p_gen.add_argument("--all-recent", action="store_true", help="Generate from all recent meeting minutes")

    # summary
    p_sum = sub.add_parser("summary", help="Generate brief summary of a document")
    p_sum.add_argument("--doc-id", required=True)

    args = parser.parse_args()

    if args.command == "list":
        docs = list_documents(args.committee)
        current_committee = ""
        for d in docs:
            if d["committee"] != current_committee:
                current_committee = d["committee"]
                print(f"\n{current_committee}:")
            print(f"  {d['doc_id']:>12}  {d['date'] or '?':<28}  {d['type']:<12}  {d['text_size']:>8} chars")

    elif args.command == "generate":
        if args.all_recent:
            # Generate articles from the most recent minutes of each committee
            committees = {}
            for d in list_documents():
                if d["type"] == "minutes" and d["committee"] not in committees:
                    committees[d["committee"]] = d["doc_id"]
            print(f"Generating articles for {len(committees)} committees...\n")
            for committee, doc_id in committees.items():
                print(f"\n{'='*60}")
                article = generate_article(doc_id)
                if article:
                    print(article[:200] + "...\n")

        elif args.doc_id:
            article = generate_article(args.doc_id)
            if article:
                print("\n" + article)

        elif args.query:
            results = search_documents(args.query, limit=1)
            if results:
                article = generate_article(results[0]["doc_id"])
                if article:
                    print("\n" + article)
            else:
                print(f"No documents found for '{args.query}'")

        elif args.committee and args.latest:
            docs = list_documents(args.committee)
            if docs:
                article = generate_article(docs[0]["doc_id"])
                if article:
                    print("\n" + article)
            else:
                print(f"No documents for committee '{args.committee}'")

        else:
            print("Specify --doc-id, --query, --committee --latest, or --all-recent")

    elif args.command == "summary":
        generate_summary(args.doc_id)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
