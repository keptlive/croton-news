import json, os, sqlite3, sys
sys.path.insert(0, '/opt/croton-news/rag')

with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

from write_article import load_transcript, call_llm

os.chdir('/opt/croton-news/rag')
db = sqlite3.connect('rag.db')
db.row_factory = sqlite3.Row

transcript = load_transcript('yt-_3mId9iaSps')
full_text = transcript.get('full_text', '')

system = """You are a local news journalist covering Croton-on-Hudson, NY schools.
Write a COMPREHENSIVE article (1500-2000 words) that thoroughly covers what happened at this candidate forum.

VERIFIED CANDIDATE FACTS (use ONLY these — do NOT invent or embellish):
- Neal Haber: INCUMBENT since 1998 (27 years). Former Board President and VP. Chairs Policy Committee. Two sons attended Croton schools K-12, graduated HS in 2004 and 2008.
- Sarah Carrier: INCUMBENT since 2017 (9 years). Former Board President 2019-2024. Chairs Advocacy and Communications committees. Lives in Croton 20 years, has a 10th grader.
- Anamika Bhatnagar: INCUMBENT, 3 years on board. Board Vice President. Chairs Audit Committee. Yale BA in English Lit. Nearly 30 years in children's book publishing, 20 years at Scholastic where she developed Captain Underpants and Dog Man (she stated this in her intro). Now at Benchmark Education on K-2 literacy/handwriting. Has 8th grader and 11th grader.
- Jake Day: CHALLENGER, first-time candidate. CCC member. Son entering CET (elementary) in fall, plus a 2-year-old. 15 years in community development and housing finance, manages a first-time homebuyer grant program at a federal agency. Universal Pre-K advocate.
- Betsy Laird: CHALLENGER, first-time candidate. Name is Elizabeth but goes by Betsy. Trained psychologist turned data leader focused on vulnerable communities. Moved to Croton 2020. Rising 2nd grader and rising kindergartner.

OTHER VERIFIED FACTS:
- Three seats open, five candidates running
- Election: May 19, 2026
- Forum hosted by Croton Community Collective (CCC), 300+ members, founded by Jill Anderson (public school teacher, mom of two)
- Co-moderators: Dani Zelliger (English teacher, father) and Nicole Curran (3rd grade teacher, mom)
- 120 community questions submitted, organized into 6 themes
- Venue: St. Augustine's Episcopal Church, standing room only
- League of Women Voters broader forum: May 12, 7:30 PM, high school auditorium
- CCC also launched "Enjoy a Good Read" book boxes in 45 local businesses

THE 6 QUESTIONS COVERED:
1. Perceptions of EdTech proliferation — benefits and drawbacks
2. Analog vs digital reading/writing across K-12
3. One-to-one device limits and BOE's role in screen guidelines
4. Phone ban implementation and enforcement at CHHS
5. AI in schools — role, academic integrity, urgency of policy
6. Final vision and concrete steps for tech in schools

ARTICLE REQUIREMENTS:
- Cover ALL 6 topics with specific candidate positions and quotes
- Show where candidates agreed AND where they differed
- Use direct quotes from the transcript with {{quote:SECONDS}} tags
- Include the specific examples and analogies candidates used (Betsy's forklift metaphor, Jake's evaluation framework, Anamika's avatar story, Neal's science symposium reference)
- Note the areas of consensus and tension
- The article should give a reader who wasn't there a thorough understanding of each candidate's positions
- Do NOT fabricate any biographical details beyond what's in the verified facts above
- Past tense for the event, present tense for ongoing situations"""

# Send the full transcript
user_parts = []
user_parts.append("Write a comprehensive news article about the CHUFSD Board of Education Candidate Forum held April 23, 2026.\n")
user_parts.append(f"## Full Meeting Transcript\n\n{full_text[:28000]}\n")
user_parts.append("""
## Output Format

HEADLINE: <headline>
QUICK_SUMMARY: <2-3 sentence summary>
KEY_ACTIONS:
- Key point 1
- ...
ARTICLE:
<your comprehensive article, 1500-2000 words>""")

user_prompt = "\n".join(user_parts)
print(f"Calling LLM ({len(user_prompt)} chars)...")
response = call_llm(system, user_prompt, model="claude-sonnet-4-20250514", max_tokens=8000)

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
print(f"SUMMARY: {quick_summary[:120]}...")
print(f"Article: {len(article)} chars, ~{len(article.split())} words")

db.execute("""UPDATE meetings SET headline=?, quick_summary=?, complete_summary=?, article=?,
    article_model='claude-sonnet-4-20250514', article_generated_at=datetime('now')
    WHERE event_id=?""",
    (headline, quick_summary, key_actions, article, 'yt-_3mId9iaSps'))
db.commit()
db.close()
print("Saved.")
