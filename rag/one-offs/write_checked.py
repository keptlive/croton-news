import json, os, sqlite3, sys
sys.path.insert(0, '/opt/croton-news/rag')

with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

from write_article import load_transcript, load_meeting, call_llm

os.chdir('/opt/croton-news/rag')
db = sqlite3.connect('rag.db')
db.row_factory = sqlite3.Row

transcript = load_transcript('yt-_3mId9iaSps')
meeting = load_meeting(db, event_id='yt-_3mId9iaSps')

full_text = transcript.get('full_text', '')
speaker_map = transcript.get('speaker_map', {})

system = """You are a local news journalist covering Croton-on-Hudson, NY schools.
Write a clear, engaging article about a Board of Education candidate forum.

CRITICAL — FACT CHECK REQUIREMENTS:
You MUST use ONLY the verified facts below. Do NOT invent biographical details.
If a fact is not in the transcript or the verified facts section, do NOT include it.

VERIFIED FACTS (use these, not guesses):
- Neal Haber: INCUMBENT, 27 years on the board (since 1998), former Board President and Vice President, chairs Policy Committee. Two sons attended Croton schools K-12.
- Sarah Carrier: INCUMBENT, elected to third term in 2023 (now seeking fourth term), former Board President 2019-2024, chairs Advocacy and Communications committees.
- Anamika Bhatnagar: INCUMBENT, Board Vice President, Yale BA in English Lit, 20 years in children's book industry, now at Benchmark Education developing supplementary reading programs. Chair of Audit Committee.
- Jake Day: CHALLENGER, first-time candidate, parent of child entering CET in the fall, 15 years in community development and housing finance, Universal Pre-K advocate.
- Betsy Laird: CHALLENGER, first-time candidate. Her name is Elizabeth Laird but she goes by Betsy. Clinical psychologist turned data leader, specializes in helping vulnerable children/trauma victims.
- Three seats are open (not five). Five candidates are running for three seats.
- Election date: May 19, 2026 (NOT May 21)
- Budget: approximately $56 million (NOT $60 million)
- Forum hosted by Croton Community Collective (CCC), founded by Jill Anderson
- Co-moderators: Dani Zelliger (English teacher) and Nicole Curran (third grade teacher)
- CCC submitted 120 community questions organized into 6 themes
- Venue: St. Augustine's Episcopal Church

IMPORTANT — Quote timestamps:
Tag direct quotes with {{quote:SECONDS}} for video linking.
Only quote text that appears verbatim in the transcript.
Keep it 800-1000 words. No AI disclaimers."""

user_parts = []
user_parts.append("Write a news article about the CHUFSD Board of Education Candidate Forum on April 23, 2026.\n")
user_parts.append(f"## Full Meeting Transcript\n\n{full_text[:14000]}\n")
user_parts.append("""
## Output Format

HEADLINE: <headline>
QUICK_SUMMARY: <1-2 sentence summary>
KEY_ACTIONS:
- Key point 1
- Key point 2
ARTICLE:
<your article>""")

user_prompt = "\n".join(user_parts)
print(f"Calling LLM ({len(user_prompt)} chars)...")
response = call_llm(system, user_prompt, model="claude-sonnet-4-20250514", max_tokens=6000)

# Parse
headline = quick_summary = key_actions = article = ""
lines = response.split("\n")
section = None
for line in lines:
    if line.startswith("HEADLINE:"):
        headline = line[9:].strip()
    elif line.startswith("QUICK_SUMMARY:"):
        quick_summary = line[14:].strip()
    elif line.startswith("KEY_ACTIONS:"):
        section = "key_actions"
    elif line.startswith("ARTICLE:"):
        section = "article"
    elif section == "key_actions":
        key_actions += line + "\n"
    elif section == "article":
        article += line + "\n"

key_actions = key_actions.strip()
article = article.strip()

print(f"HEADLINE: {headline}")
print(f"SUMMARY: {quick_summary[:100]}...")
print(f"Article: {len(article)} chars")

db.execute("""UPDATE meetings SET headline=?, quick_summary=?, complete_summary=?, article=?,
    article_model='claude-sonnet-4-20250514', article_generated_at=datetime('now')
    WHERE event_id=?""",
    (headline, quick_summary, key_actions, article, 'yt-_3mId9iaSps'))
db.commit()
db.close()
print("Saved.")
