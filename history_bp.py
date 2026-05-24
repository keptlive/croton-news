"""
History Blueprint — Croton-on-Hudson Historical Document Archive

Serves all routes under /history/* when registered with the main app.
Migrated from the standalone history.croton.news Flask app.
"""

import json
import os
import re
import sqlite3
import math
import sys

from flask import (
    Blueprint, g, request, render_template, abort, jsonify, Response,
    current_app,
)

history_bp = Blueprint(
    'history',
    __name__,
)


# ─── Template filter ─────────────────────────────────────────────

@history_bp.app_template_filter("mcdonald_marks")
def mcdonald_marks(text):
    """Format McDonald transcription paragraph: rejoin manuscript line-break hyphenation,
    fold hard line breaks, and wrap [marg: ...], [illegible], [?guess] in styled spans."""
    from markupsafe import Markup, escape
    if not text:
        return ""
    text = re.sub(r"-\n-", "", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"\n", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    out = str(escape(text))
    out = re.sub(r"\[marg:\s*([^\]]+)\]",
                 r'<span class="mark-marg">[marg: \1]</span>', out)
    out = re.sub(r"\[illegible\]",
                 r'<span class="mark-illeg">[illegible]</span>', out)
    out = re.sub(r"\[\?([^\]]+)\]",
                 r'<span class="mark-illeg">[?\1]</span>', out)
    return Markup(out)


# ─── Paths ────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# RAG_DIR: sibling to site/ locally, child on VPS
_rag_sibling = os.path.join(os.path.dirname(BASE_DIR), "rag")
_rag_child = os.path.join(BASE_DIR, "rag")
RAG_DIR = _rag_child if os.path.isdir(_rag_child) else _rag_sibling

# history.db lives at rag/history.db (same level as rag.db)
# mcdonald.db lives at rag/history/mcdonald.db
HISTORY_DB_PATH = os.path.join(RAG_DIR, "history.db")
MCDONALD_DB_PATH = os.path.join(RAG_DIR, "history", "mcdonald.db")

# Load .env
_env_path = os.path.join(BASE_DIR, ".env")
if not os.path.exists(_env_path):
    _env_path = os.path.join(os.path.dirname(BASE_DIR), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "qwen/qwen3-32b"

PER_PAGE = 30


# ─── Featured topics for homepage ─────────────────────────────────

FEATURED_TOPICS = [
    {
        "title": "The Kitchawank: Croton's First People",
        "query": "Kitchawank Wappinger Navish",
        "image": "indians_manhattan_vicinity_1921.jpg",
        "summary": "For thousands of years before European contact, the Kitchawank band of the Wappinger Confederacy made their home at the mouth of the Croton River. Their fortified village Navish stood on Croton Point, surrounded by shell middens dating back 7,000 years.",
        "era": "Pre-contact — 1680s",
    },
    {
        "title": "Van Cortlandt Manor & Colonial Settlement",
        "query": "Van Cortlandt manor deed Cortlandt",
        "image": "sauthier_1778_ny_province.jpg",
        "summary": "Stephanus Van Cortlandt began purchasing Kitchawank lands in the 1670s, eventually assembling 86,000 acres into the Manor of Cortlandt — one of the largest colonial estates in New York. The manor shaped settlement patterns that persist today.",
        "era": "1680s — 1780s",
    },
    {
        "title": "The Croton Aqueduct: Engineering Marvel",
        "query": "Croton aqueduct dam water",
        "image": "sanborn_croton_1903_p1.jpg",
        "summary": "The Old Croton Aqueduct (1842) was New York City's first major water supply system. The dam and 41-mile gravity-fed aqueduct transformed both the village of Croton and the city it served, and remains a National Historic Landmark.",
        "era": "1837 — 1890s",
    },
    {
        "title": "Croton Point: 7,000 Years of History",
        "query": "Croton Point shell midden archaeology oyster",
        "image": "novi_belgii_1685_excerpt.jpg",
        "summary": "The shell middens at Croton Point are the oldest on the North Atlantic coast. Louis Brennan's 1960s excavations revealed layers of Virginia oyster shell dating to 5000 BC, making this one of the most important archaeological sites in the Northeast.",
        "era": "5000 BC — present",
    },
]


# ─── Source metadata ──────────────────────────────────────────────

COLLECTIONS = [
    {
        "id": "indigenous",
        "name": "Indigenous Peoples & Archaeology",
        "icon": "\U0001f3f9",
        "color": "#8b4513",
        "desc": "The Kitchawank, Wappinger, and Lenape peoples who lived here for 7,000+ years",
        "sources": [
            "Edward Manning Ruttenber (1872)",
            "Edward Manning Ruttenber (1906)",
            "Reginald Pelham Bolton (1922)",
            "Louis A. Brennan et al. (1962)",
            "Various (1967)",
            "Various (1971)",
            "Herbert C. Kraft et al. (1994)",
        ],
    },
    {
        "id": "colonial",
        "name": "Colonial & Dutch Records",
        "icon": "\U0001f4dc",
        "color": "#6b3a2a",
        "desc": "Dutch colonial documents, Van Cortlandt deeds, and early Westchester settlement",
        "sources": [
            "Robert Bolton, Jr. (1848)",
            "E.B. O'Callaghan (ed.) (1856)",
            "E.B. O'Callaghan (1849)",
        ],
    },
    {
        "id": "county",
        "name": "Westchester County Histories",
        "icon": "\U0001f4d6",
        "color": "#2c5282",
        "desc": "Comprehensive histories of the county and Town of Cortlandt",
        "sources": [
            "J. Thomas Scharf (1886)",
            "Frederic Shonnard & W.W. Spooner (1900)",
        ],
    },
    {
        "id": "local",
        "name": "Croton Local History",
        "icon": "\U0001f3d8\ufe0f",
        "color": "#38713d",
        "desc": "Blog posts, articles, and community histories by local historians",
        "sources": [
            "crotonhistory.org",
            "Croton Friends of History",
            "Wikipedia",
            "Friends of the Old Croton Aqueduct",
        ],
    },
    {
        "id": "government",
        "name": "Government Documents",
        "icon": "\U0001f3db\ufe0f",
        "color": "#555",
        "desc": "Village comprehensive plan, housing reports, environmental assessments",
        "sources": [],
    },
]

SOURCE_TO_COLLECTION = {}
for col in COLLECTIONS:
    for s in col["sources"]:
        SOURCE_TO_COLLECTION[s] = col["id"]

SOURCE_URLS = {
    "Edward Manning Ruttenber (1872)": "https://archive.org/details/ruttenberindians00ruttrich",
    "Edward Manning Ruttenber (1906)": "https://archive.org/details/footprintsofredm02rutt",
    "Robert Bolton, Jr. (1848)": "https://archive.org/details/historyofcountyo01bolt",
    "Reginald Pelham Bolton (1922)": "https://archive.org/details/indianpathsingre01bolt",
    "J. Thomas Scharf (1886)": "https://archive.org/details/historyofwestche00scha_0",
    "Frederic Shonnard & W.W. Spooner (1900)": "https://archive.org/details/historyofwestche00inshon",
    "E.B. O'Callaghan (ed.) (1856)": "https://archive.org/details/documentsrelativ01brod",
    "E.B. O'Callaghan (1849)": "https://archive.org/details/documentaryhist01ocal",
    "Louis A. Brennan et al. (1962)": "https://nysarchaeology.org/download/nysaa/bulletin/number_026.pdf",
    "Various (1967)": "https://nysarchaeology.org/download/nysaa/bulletin/number_039.pdf",
    "Various (1971)": "https://nysarchaeology.org/download/nysaa/bulletin/number_052.pdf",
    "Herbert C. Kraft et al. (1994)": "https://nysarchaeology.org/download/nysaa/bulletin/number_107.pdf",
}


# ─── Photo categories ────────────────────────────────────────────

PHOTO_CATEGORIES = [
    {"id": "colonial_maps", "name": "Colonial & Dutch Maps", "icon": "\U0001f5fa\ufe0f",
     "desc": "Maps of New Netherland and colonial New York showing indigenous territories",
     "match": lambda f: any(x in f for x in ['novi_belgii', 'neobelgii', 'mitchell', 'sauthier', 'ny_province', 'new_amsterdam'])},
    {"id": "indigenous", "name": "Indigenous Peoples", "icon": "\U0001f3f9",
     "desc": "Maps and images related to the Kitchawank, Wappinger, and Lenape",
     "match": lambda f: any(x in f for x in ['indians_', 'native_american', 'nimham', 'ruttenber', 'bolton_1922'])},
    {"id": "revolution", "name": "American Revolution", "icon": "\u2694\ufe0f",
     "desc": "The Arnold-Andre affair, Battle of Kingsbridge, and wartime Westchester",
     "match": lambda f: any(x in f for x in ['andre', 'arnold', 'benedict', 'vulture', 'van_cortlandt', 'kieft', 'willem_kieft', 'pavonia'])},
    {"id": "aqueduct_dam", "name": "Aqueducts & Dams", "icon": "\U0001f3d7\ufe0f",
     "desc": "Construction of the Old Croton Aqueduct and New Croton Dam",
     "match": lambda f: any(x in f for x in ['dam', 'aqueduct', 'tower_', 'spillway', 'croton_dam'])},
    {"id": "village_life", "name": "Village Life & Buildings", "icon": "\U0001f3d8\ufe0f",
     "desc": "Sanborn maps, manor houses, vineyards, and daily life in Croton",
     "match": lambda f: any(x in f for x in ['sanborn', 'underhill', 'harmon', 'manor', 'scharf', 'lossing', 'bolton_vol', 'wca_atlas'])},
    {"id": "park_photos", "name": "Croton Point Park (1920s-1950s)", "icon": "\U0001f4f7",
     "desc": "Westchester County Park Commission photographs of Croton Point, the beach, dam, and reservoir",
     "match": lambda f: (f.startswith('wca_') and 'atlas' not in f) or (f.startswith('nara_'))},
    {"id": "nyhs_photos", "name": "Historical Photographs (1898-1903)", "icon": "\U0001f4f8",
     "desc": "George Stonebridge glass plate negatives — Croton Point excursions and the 1900 dam strike",
     "match": lambda f: f.startswith('dcmny_') or (f.startswith('nyheritage_') and ('strike' in f or 'crowd' in f or 'beach' in f or 'daisy' in f or 'excursion' in f or 'sirius' in f or 'biddle' in f))},
    {"id": "engineering", "name": "Engineering Drawings & Surveys", "icon": "\U0001f4d0",
     "desc": "Aqueduct plans, dam surveys, HAER documentation, and reservoir maps",
     "match": lambda f: f.startswith('loc_haer') or (f.startswith('nyheritage_') and ('aqueduct' in f or 'reservoir' in f or 'map' in f or 'topo' in f or 'watershed' in f)) or f.startswith('wikimedia_haer')},
    {"id": "prohibition", "name": "Prohibition Era", "icon": "\U0001f943",
     "desc": "Speakeasies, rum runners, and Roaring Twenties Croton",
     "match": lambda f: any(x in f for x in ['prohibition', 'curtiss', 'speakeasy', 'teatown'])},
]


# ─── Database ─────────────────────────────────────────────────────

def get_history_db():
    if "history_db" not in g:
        g.history_db = sqlite3.connect(HISTORY_DB_PATH)
        g.history_db.row_factory = sqlite3.Row
    return g.history_db


def get_mcdonald_db():
    if "mcdonald_db" not in g:
        g.mcdonald_db = sqlite3.connect(MCDONALD_DB_PATH)
        g.mcdonald_db.row_factory = sqlite3.Row
    return g.mcdonald_db


# DB cleanup is handled by the main app's teardown_appcontext


# ─── LLM Report Generation ───────────────────────────────────────

def generate_report(query, chunks):
    """Generate a synthesized historical report from search results."""
    if not GROQ_KEY or not chunks:
        return None

    context_parts = []
    for chunk in chunks[:10]:
        d = dict(chunk)
        label = d.get("source") or d.get("source_file", "")
        title = d.get("title", "")
        text = d["content"]
        context_parts.append(f"[{label} — {title}]\n{text}")

    context = "\n\n---\n\n".join(context_parts)

    system_prompt = """You are a historical research assistant for the Croton-on-Hudson Historical Archive.
Given primary source documents, write a clear, well-organized research synopsis.

Rules:
- Synthesize information across multiple sources into a coherent narrative
- Cite specific sources by author name and year (e.g., "Ruttenber (1872) notes that...")
- Include specific dates, names, and places from the documents
- Note where sources disagree or provide different perspectives
- Use clear section headings if the report covers multiple aspects
- Write 3-6 paragraphs
- Be scholarly but accessible
- End with a "Sources consulted" list"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Research topic: {query}\n\nDocuments:\n{context}"},
    ]

    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": 1500,
        "temperature": 0.3,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_KEY}",
    }

    try:
        import requests as _http
        resp = _http.post(GROQ_URL, data=payload, headers=headers, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        content = re.sub(r'<\|think\|>.*?<\|/think\|>', '', content, flags=re.DOTALL)
        content = content.strip()
        content = re.sub(r'^(?:Okay|Sure|Alright|Let me)[,.]?\s*', '', content)
        return content if len(content) > 50 else None
    except Exception as e:
        print(f"LLM report error: {e}", file=sys.stderr)
        return None


# ─── Stories ──────────────────────────────────────────────────────

STORIES_DIR = os.path.join(RAG_DIR, "history", "stories")


def _parse_story(filepath):
    """Parse a markdown story file into structured data."""
    with open(filepath) as f:
        text = f.read()

    lines = text.split("\n")
    title = ""
    subtitle = ""
    photo = ""
    photo2 = ""
    photo2_caption = ""
    sources = []
    body_lines = []
    in_sources = False

    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("*") and not subtitle and not body_lines:
            subtitle = line.strip("* \n")
        elif line.startswith("**Photo:**"):
            photo = line.replace("**Photo:**", "").strip().split(" — ")[0].strip()
        elif line.startswith("**Photo2:**"):
            parts = line.replace("**Photo2:**", "").strip().split(" — ", 1)
            photo2 = parts[0].strip()
            photo2_caption = parts[1].strip() if len(parts) > 1 else ""
        elif line.startswith("**Sources:**"):
            in_sources = True
        elif in_sources and line.startswith("- "):
            sources.append(line[2:].strip())
        elif in_sources and not line.strip():
            in_sources = False
        elif line.strip() == "---":
            continue
        elif not in_sources:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    body = re.sub(r'"([^"]+)"', '\u201c' + r'\1' + '\u201d', body)

    slug = os.path.splitext(os.path.basename(filepath))[0]
    slug = slug.replace("_long", "")

    return {
        "title": title,
        "subtitle": subtitle,
        "photo": photo,
        "photo2": photo2,
        "photo2_caption": photo2_caption,
        "sources": sources,
        "body": body,
        "slug": slug,
        "word_count": len(body.split()),
    }


def _load_stories():
    """Load all story files, preferring _long versions when available."""
    if not os.path.exists(STORIES_DIR):
        return []
    files = sorted(os.listdir(STORIES_DIR))
    bases = {}
    for f in files:
        if not f.endswith(".md"):
            continue
        base = f.replace("_long.md", ".md")
        is_long = "_long.md" in f
        if base not in bases or is_long:
            bases[base] = f
    stories = []
    for base in sorted(bases):
        try:
            stories.append(_parse_story(os.path.join(STORIES_DIR, bases[base])))
        except Exception as e:
            print(f"Error loading story {bases[base]}: {e}", file=sys.stderr)
    return stories


# ─── Timeline data ────────────────────────────────────────────────

TIMELINE = [
    # ── Pre-Contact ──
    {"era": "Pre-Contact (before 1609)", "date": "~7000 BC", "title": "First human habitation at Croton Point",
     "desc": "Post-glacial peoples begin occupying the peninsula. The earliest undated artifacts suggest habitation roughly contemporaneous with the retreat of the Wisconsin ice sheet.",
     "source": "Brennan (1962), NYSAA Bulletin No. 26", "link": "/history/search?q=shell+midden+Croton+Point"},
    {"date": "~5000 BC", "title": "Shell middens begin accumulating at Kettle Rock",
     "desc": "Virginia oyster shell deposits at Croton Point \u2014 the oldest on the North Atlantic coast. Radiocarbon dating established these as among the earliest evidence of sustained habitation in the Northeast.",
     "source": "Brennan, NYSAA Bulletin No. 26 (1962)", "link": "/history/search?q=shell+midden+oyster"},
    {"date": "~3000 BC", "title": "Vinette I pottery appears at Croton Point",
     "desc": "The earliest ceramic tradition in the Northeast, found in stratigraphic position in the middens \u2014 a key discovery for the regional chronological sequence.",
     "source": "Brennan, NYSAA Bulletin No. 26 (1962)", "link": "/history/search?q=Vinette+pottery"},
    {"date": "~1600", "title": "Kitchawank village of Navish at Croton Point",
     "desc": "The Kitchawank, a band of the Wappinger Confederacy, maintain a fortified stockade at the neck of Croton Point, guarding oyster beds in Haverstraw Bay. Territory extends from the Croton River north to Anthony's Nose.",
     "source": "Ruttenber (1872), Scharf (1886)", "link": "/history/search?q=Kitchawank+Navish"},

    # ── European Contact & Dutch Era ──
    {"era": "European Contact & Dutch Era (1609\u20131664)", "date": "1609", "title": "Henry Hudson's Half Moon passes Croton Point",
     "desc": "Officer Robert Juet documents encounters with indigenous peoples who traded tobacco for knives and beads, wearing deer skins and displaying copper items.",
     "source": "Wikipedia (Wappinger)", "link": "/history/search?q=Hudson+Half+Moon"},
    {"date": "1624", "title": "Dutch West India Company establishes New Netherland",
     "desc": "Permanent Dutch settlement begins. The Wappinger Confederacy \u2014 18 bands including the Kitchawank \u2014 controls the east bank of the Hudson from Manhattan to Poughkeepsie.",
     "source": "Shonnard (1900)", "link": "/history/search?q=Dutch+settlement+Wappinger"},
    {"date": "1626", "title": "Weckquaesgeek elder murdered near the Collect Pond",
     "desc": "Three laborers rob and kill a man carrying furs to trade. His young nephew escapes and vows revenge \u2014 a vow he will fulfill fifteen years later.",
     "source": "Ruttenber (1872), Shonnard (1900)", "link": "/history/story/02_fifteen_year_revenge",
     "sub_id": "kiefts_war"},
    {"date": "1641", "title": "The nephew kills Claes Smits \u2014 the 15-year revenge",
     "desc": "The boy, now a man, enters the wheelwright's shop near Turtle Bay with beaver skins and murders him with an axe. Director Kieft demands the Weckquaesgeek surrender the killer.",
     "source": "Ruttenber (1872), Shonnard (1900)", "link": "/history/story/02_fifteen_year_revenge",
     "sub_id": "kiefts_war"},
    {"date": "Aug 1641", "title": "Council of Twelve Men rejects Kieft's call for war",
     "desc": "The first popularly elected body in New Netherland urges Kieft to demand the killer 'once, twice, yea for a third time' in a 'friendly manner.' Kieft refuses and dissolves the council.",
     "source": "Ruttenber (1872), O'Callaghan (1856), chunk 2155", "link": "/history/search?q=Council+Twelve+Men",
     "sub_id": "kiefts_war"},
    {"date": "Late 1643", "title": "Anne Hutchinson killed as war spreads",
     "desc": "The famous religious dissident, living in exile in the Bronx, is killed by Siwanoy warriors as the war engulfs the entire region. ~1,500 indigenous warriors attack across New Netherland.",
     "source": "Wikipedia (Kieft's War)", "link": "/history/search?q=Hutchinson+killed",
     "sub_id": "kiefts_war"},
    {"date": "Feb 25, 1643", "title": "Pavonia Massacre",
     "desc": "Dutch soldiers kill ~120 sleeping Wappinger refugees at Pavonia (Jersey City), including women and children. 40 more killed at Corlears Hook that same night. Unified Algonquian resistance follows.",
     "source": "Wikipedia (Kieft's War), Ruttenber (1872)", "link": "/history/story/02_fifteen_year_revenge",
     "sub_id": "kiefts_war"},
    {"date": "Mar 1644", "title": "Pound Ridge Massacre \u2014 500-700 killed",
     "desc": "Captain John Underhill's forces attack a Weckquaesgeek village. Many burned alive in their dwellings. One of the deadliest single events in the colonial Indian wars.",
     "source": "Shonnard (1900)", "link": "/history/search?q=Pound+Ridge+massacre",
     "sub_id": "kiefts_war"},
    {"date": "Aug 1645", "title": "Peace treaty signed at Croton Point",
     "desc": "After two years of war costing over 1,500 indigenous lives, the Kitchawank and 68 other tribes sign peace with the Dutch. A plaque at Croton Point Park marks the site.",
     "source": "Ruttenber (1872), Wikipedia", "link": "/history/search?q=1645+treaty+Croton",
     "sub_id": "kiefts_war"},
    {"date": "1655", "title": "Peach War \u2014 Wappinger Confederacy fractures",
     "desc": "A second Dutch-indigenous conflict results in ~100 settler and 60 Wappinger deaths. Surviving bands begin relocating to Stockbridge, Massachusetts.",
     "source": "Wikipedia (Wappinger)", "link": "/history/search?q=Peach+War"},
    {"date": "1664", "title": "English take New Netherland \u2014 becomes New York",
     "desc": "Peter Stuyvesant surrenders to the English fleet. Dutch colonial rule ends. The Wappinger bands continue to lose territory under the new administration.",
     "source": "Shonnard (1900), Scharf (1886)", "link": "/history/search?q=Stuyvesant+surrender+English"},

    # ── Colonial Westchester ──
    {"era": "Colonial Westchester (1664\u20131775)", "date": "1677\u20131683", "title": "Stephanus Van Cortlandt begins purchasing Kitchawank lands",
     "desc": "Van Cortlandt acquires territory between Croton and Peekskill from indigenous peoples, building toward the largest manor in the region.",
     "source": "Bolton (1848), Wikipedia", "link": "/history/search?q=Van+Cortlandt+purchase"},
    {"date": "Jun 3, 1682", "title": "Croton Point sold to Cornelius Van Bursum",
     "desc": "The Kitchawank sell the peninsula. The deed preserves four indigenous place-names: Navish, Senasqua, Tanracken, and Sepperack.",
     "source": "Scharf (1886), chunk 3989", "link": "/history/search?q=Van+Bursum+Navish+Senasqua"},
    {"date": "1693", "title": "Adolph Philipse obtains the Highland Patent",
     "desc": "Original grant covers ~15,000 acres. Philipse reportedly 'cut down the tree marking the eastern border, rode all day and remarked a tree near the CT border' \u2014 expanding the claim to 205,000 acres.",
     "source": "Cutul (2025)", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "1697", "title": "Van Cortlandt Manor chartered \u2014 86,000 acres",
     "desc": "King William III grants Stephanus Van Cortlandt a royal charter for 200 square miles. The manor encompasses much of present-day northern Westchester.",
     "source": "Bolton (1848), Wikipedia", "link": "/history/search?q=Van+Cortlandt+charter+1697"},
    {"date": "1705", "title": "Earliest record of Native American slavery in Westchester",
     "desc": "Elizabeth Legget of Westchester deeds her daughter 'my two negro children, born of the body of Hannah, my negro woman, of the issue of the body of Robin, my Indian slave.'",
     "source": "Bolton (1848), chunk 1551", "link": "/history/story/07_slavery_patriots_manor"},

    # ── Revolution ──
    {"era": "The American Revolution (1765\u20131783)", "date": "~1746", "title": "Nimham leads 200-300 surviving Wappinger as nomads",
     "desc": "Daniel Nimham's band \u2014 Mahican and Munsee speakers \u2014 survives across five colonies through basket weaving, broom crafting, and seasonal farm labor. He maintains annual pilgrimages to Mount Nimham in Putnam County.",
     "source": "Wikipedia (Daniel Nimham), chunk 5391", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "1755", "title": "Nimham enlists at Albany during King George's War",
     "desc": "Around 200 Wappinger relocate to the Stockbridge Mission in Massachusetts to protect their families while men serve in colonial forces.",
     "source": "Wikipedia (Daniel Nimham), chunk 5391", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "1765", "title": "Nimham sues over the Philipse land fraud",
     "desc": "The last Wappinger sachem challenges the fraudulently expanded patent in court. He loses; his attorney Samuel Munroe is arrested.",
     "source": "Cutul (2025), Bolton (1848)", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "1766", "title": "Nimham travels to London to petition the Crown",
     "desc": "Nimham and three Mohican chiefs sail to England. The Lords of Trade acknowledge 'frauds and abuses' but restore nothing. The deed is snatched from Munroe's hands before he can prove fraud.",
     "source": "Cutul (2025), Wikipedia", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "~1776", "title": "The Westchester Tea Party \u2014 thirty women on horseback",
     "desc": "According to local tradition, women led by Madam Orser ride to John Arthur's home to seize tea stocks. The incident may have given Teatown its name.",
     "source": "Croton Friends of History (Macdonald oral history)", "link": "/history/story/06_westchester_tea_party"},
    {"date": "1776\u20131783", "title": "Westchester becomes the 'Neutral Ground'",
     "desc": "The county is contested territory between American and British lines. 'Skinners' and 'Cowboys' plunder civilians. 'Neither of them stopped to ask the politics of horse or cow which they drove into their lines.'",
     "source": "Shonnard (1900)", "link": "/history/search?q=neutral+ground+Skinners+Cowboys"},
    {"date": "1775\u20131778", "title": "Nimham and son serve under Washington at Valley Forge",
     "desc": "Abraham Nimham becomes captain of the Stockbridge Militia \u2014 Mohicans, Wappingers, Munsee. Both father and son serve under Washington and later with Lafayette.",
     "source": "Wikipedia (Daniel Nimham), Bolton (1848)", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "Aug 31, 1778", "title": "Daniel Nimham killed at Battle of Kingsbridge",
     "desc": "The last Wappinger sachem and ~40 warriors die fighting for the Continental Army. 'He called out to his people to fly, that he himself was old, and would die there.'",
     "source": "Bolton (1848), chunk 1758", "link": "/history/story/03_nimham_last_stand",
     "sub_id": "nimham"},
    {"date": "Sep 21\u201323, 1780", "title": "Cannon from Teller's Point exposes Arnold's treason",
     "desc": "Croton militia fire on HMS Vulture, forcing it downstream and stranding Major Andre behind enemy lines. Andre is captured at Tarrytown with plans of West Point in his stockings.",
     "source": "Bolton (1848), Shonnard (1900)", "link": "/history/story/01_cannon_tellers_point"},
    {"date": "Jan 1782", "title": "The surprise at Orser's",
     "desc": "A military engagement at the Orser family property near Croton, recorded in Shonnard's index of wartime events.",
     "source": "Shonnard (1900), chunk 5092", "link": "/history/search?q=Orser+surprise"},

    # ── 19th Century ──
    {"era": "The Aqueduct Era (1827\u20131890s)", "date": "1827", "title": "Underhill plants the first large vineyard in America",
     "desc": "Dr. Richard T. Underhill begins cultivating 75 acres of grapevines at Croton Point. He will breed the 'Senasqua' variety, named after the Kitchawank meadow.",
     "source": "crotonhistory.org, chunk 823", "link": "/history/story/04_grape_king_senasqua"},
    {"date": "1835", "title": "New York City Water Commission established",
     "desc": "After decades of epidemics and fires fueled by contaminated wells, the city creates a commission to find a clean water source. The Croton River is chosen.",
     "source": "Timeline records, chunk 142", "link": "/history/search?q=Water+Commission+established",
     "sub_id": "aqueduct"},
    {"date": "1836", "title": "John B. Jervis appointed Chief Engineer",
     "desc": "The West Point-trained engineer designs a 41-mile gravity-fed system from the Croton Dam to Manhattan \u2014 one of the greatest engineering projects of the era.",
     "source": "King (1843), chunk 684", "link": "/history/search?q=Jervis+engineer+Croton",
     "sub_id": "aqueduct"},
    {"date": "1837", "title": "Land acquisition begins in Westchester",
     "desc": "Water Commissioners begin purchasing land along the aqueduct route. The New York Sun warns that 'landholders are seldom diffident in taking advantage of public improvements, to enhance the price of property.'",
     "source": "Old Croton Aqueduct records, chunk 697", "link": "/history/search?q=aqueduct+land+purchase",
     "sub_id": "aqueduct"},
    {"date": "Apr 1838", "title": "Irish aqueduct workers strike \u2014 overseer killed",
     "desc": "Laborers demand wages of 87.5 to 100 cents per day and march from the dam site to Sing Sing. Engineer Edmund French reports 'the affair that resulted in the death of one of the overseers on Section 10.'",
     "source": "Old Croton Aqueduct records, chunk 696", "link": "/history/search?q=Irish+strike+aqueduct",
     "sub_id": "aqueduct"},
    {"date": "1839\u20131841", "title": "Aqueduct tunnel and bridge construction",
     "desc": "Workers build the High Bridge across the Harlem River (the oldest standing bridge in NYC), ventilator towers, weirs at Ossining and Yonkers, and the Murray Hill Reservoir.",
     "source": "Timeline records, chunk 142", "link": "/history/search?q=High+Bridge+aqueduct",
     "sub_id": "aqueduct"},
    {"date": "Jun 22, 1842", "title": "Water first flows through the aqueduct",
     "desc": "Croton River water reaches the receiving reservoir at what is now Central Park. The 41-mile journey by gravity takes roughly 22 hours.",
     "source": "King (1843)", "link": "/history/search?q=aqueduct+water+flows",
     "sub_id": "aqueduct"},
    {"date": "Oct 14, 1842", "title": "Croton Water Celebration \u2014 NYC's greatest jubilee",
     "desc": "'The greatest jubilee New York has ever boasted.' A parade, fountains, and a commemorative medal by engraver Robert Lovett Sr. mark the arrival of clean water. John Quincy Adams was invited but sent his regrets.",
     "source": "crotonhistory.org, chunk 932", "link": "/history/search?q=Croton+water+celebration+1842",
     "sub_id": "aqueduct"},
    {"date": "1857", "title": "Central Park reservoir design competition",
     "desc": "The Central Park Commission holds a competition for the reservoir design. Thirty-three entries are submitted.",
     "source": "crotonhistory.org, chunk 878", "link": "/history/search?q=reservoir+Central+Park+design",
     "sub_id": "aqueduct"},
    {"date": "1884\u20131890", "title": "New Croton Aqueduct built",
     "desc": "A second, larger aqueduct with three times the capacity, tapping lakes across a watershed of several hundred square miles. The old aqueduct continues to operate.",
     "source": "Lossing (1866), crotonhistory.org", "link": "/history/search?q=New+Croton+Aqueduct",
     "sub_id": "aqueduct"},
    {"date": "1865", "title": "Underhill's Croton and Senasqua grapes first fruit",
     "desc": "Two new varieties: the Croton (Delaware x Chasselas) and the Senasqua (Concord x Black Prince). The Senasqua grape carries a 7,000-year-old Kitchawank name.",
     "source": "crotonhistory.org, chunk 823", "link": "/history/story/04_grape_king_senasqua"},
    {"date": "1880s", "title": "Underhill brickyard replaces the vineyards",
     "desc": "The same family pivots from winemaking to brickmaking as the vineyards decline. Underhill bricks are shipped by barge to build New York City.",
     "source": "crotonhistory.org", "link": "/history/story/09_five_lives_croton_point"},
    {"date": "1884\u20131890", "title": "New Croton Aqueduct built",
     "desc": "A second, larger aqueduct with three times the capacity of the original, tapping numerous lakes across a watershed of several hundred square miles.",
     "source": "Lossing (1866)", "link": "/history/search?q=New+Croton+Aqueduct",
     "sub_id": "aqueduct"},

    # ── Turn of Century ──
    {"era": "Modern Croton (1892\u2013present)", "date": "May 1892", "title": "New Croton Dam contract drawing completed",
     "desc": "The contract drawing for the Cornell Site reveals 'buildings, bridges and roads behind the dam which were destroyed when the valley was flooded.' Construction begins.",
     "source": "crotonhistory.org, chunk 928", "link": "/history/search?q=dam+site+1892",
     "sub_id": "dam"},
    {"date": "1892\u20131895", "title": "Excavation begins \u2014 Italian workers arrive under padrone system",
     "desc": "Workers dig 131 feet below the riverbed. Italian immigrants controlled by padrones who 'managed up to 150 workers' and kept them in permanent debt. 'A man lost his life for every stone set on the dam.'",
     "source": "Croton Friends of History, chunk 975", "link": "/history/story/05_little_italy_dam",
     "sub_id": "dam"},
    {"date": "1895\u20131896", "title": "Blacksmiths and stone work \u2014 Scientific American takes notice",
     "desc": "Skilled blacksmiths run forges constantly. An October 1896 Scientific American engraving documents the excavation with 'noteworthy accuracy.'",
     "source": "crotonhistory.org, chunks 837, 946", "link": "/history/search?q=Scientific+American+dam",
     "sub_id": "dam"},
    {"date": "~1897", "title": "'Little Italy' settlement emerges near the dam",
     "desc": "Dormitories for 60 workers each, saloons, a chapel, a schoolhouse. 'It was a rough area. Fellas would get a few drinks, you couldn't tell what the dickens they would do.'",
     "source": "Croton Friends of History, chunk 975", "link": "/history/story/05_little_italy_dam",
     "sub_id": "dam"},
    {"date": "Apr 1900", "title": "Workers strike \u2014 Governor Roosevelt sends the Seventh Regiment",
     "desc": "After NY mandates an 8-hour day, workers demand higher wages. Contractors refuse. Roosevelt establishes 'Camp Roosevelt.' The strike ends after three weeks without improvements.",
     "source": "Croton Friends of History, chunk 975", "link": "/history/story/05_little_italy_dam",
     "sub_id": "dam"},
    {"date": "Jan 10, 1906", "title": "Final stone placed \u2014 champagne and shamrocks",
     "desc": "A 3,200-pound stone is lowered by steam machinery. Comptroller Metz places an Irish shamrock beneath it. The reservoir begins filling.",
     "source": "Croton Friends of History, chunk 975", "link": "/history/story/05_little_italy_dam",
     "sub_id": "dam"},
    {"date": "Jan 1, 1907", "title": "Dam transferred to New York City \u2014 world's tallest masonry dam",
     "desc": "1,168 feet across, 297 feet high, 206 feet at the base. The reservoir extends 20 miles upstream. Acclaimed internationally as the 'Croton Profile.' Little Italy vanishes.",
     "source": "Croton Friends of History, chunk 975", "link": "/history/story/05_little_italy_dam",
     "sub_id": "dam"},
    {"date": "1907", "title": "Harmon development begins \u2014 Nikko Inn opens",
     "desc": "Clifford Harmon builds 'the most important and extensive suburban development in the history of New York.' The Japanese-themed Nikko Inn opens as a tea house.",
     "source": "crotonhistory.org", "link": "/history/story/08_other_harmon"},
    {"date": "1921", "title": "Admiral Moto acquitted in first Westchester liquor case",
     "desc": "The Mikado Inn proprietor is acquitted in 'the first case to be tried in Westchester County for alleged violation of the New York State liquor law.'",
     "source": "crotonhistory.org, chunk 922", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "May 15, 1922", "title": "Rum plane crashes near Croton",
     "desc": "A Curtis biplane carrying 250 quarts of Scotch and Irish whiskey from Montreal crashes near the Tumble Inn. A route map reveals an aerial smuggling corridor.",
     "source": "crotonhistory.org, NYT (1922)", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "Jun 17, 1922", "title": "Undercover agents fiddle, sing, and dance at Nikko Inn",
     "desc": "'McKay fiddled, Reager sang and Gallante danced' \u2014 then arrested the proprietor for serving $1.50 highballs.",
     "source": "NYT, June 17, 1922; chunk 853", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "May 1925", "title": "Nikko Inn padlocked by federal judge",
     "desc": "Federal Judge John C. Knox padlocks Roy Kojima's Nikko Inn for two months \u2014 described as 'the first place run by Japanese to be closed in padlock proceedings.'",
     "source": "crotonhistory.org, chunk 852", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "1924", "title": "Submarine shapes photographed near Croton Point",
     "desc": "An aerial photograph shows two 250-foot objects in the Hudson. The Navy confirms none of its vessels are in the area. The photo is filed with Coast Guard Intelligence.",
     "source": "Lawson (2013); chunk 910", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "1922", "title": "16-year-old Oscar Levant plays piano at the Mikado Inn",
     "desc": "The future concert pianist and television personality sleeps in the cellar with 'twenty or thirty Japanese waiters' while performing at the speakeasy.",
     "source": "Levant, Memoirs of an Amnesiac (1965); chunk 861", "link": "/history/story/10_prohibitions_wild_croton",
     "sub_id": "prohibition"},
    {"date": "1927", "title": "Croton Point becomes a county landfill",
     "desc": "Westchester County begins dumping waste on the peninsula that holds 7,000-year-old shell middens. The landfill will operate for 59 years.",
     "source": "Wikipedia, Croton Point Landfill Review (2019)", "link": "/history/story/09_five_lives_croton_point"},
    {"date": "1928", "title": "Harmon Foundation sponsors first African-American art exhibition",
     "desc": "William E. Harmon's foundation \u2014 funded by the same real estate fortune that built Croton-Harmon \u2014 becomes 'one of the first major supporters of African American creativity.'",
     "source": "crotonhistory.org, chunk 974", "link": "/history/story/08_other_harmon"},
    {"date": "Apr 1, 1948", "title": "Croton-on-Hudson gets hyphenated",
     "desc": "The village officially adds hyphens to its name, becoming Croton-on-Hudson.",
     "source": "crotonhistory.org", "link": "/history/search?q=Croton+hyphenated"},
    {"date": "1960\u20131963", "title": "Brennan excavates Croton Point shell middens",
     "desc": "Louis Brennan discovers Vinette I pottery in stratigraphic position and establishes the middens as among the oldest on the Atlantic coast. Founds the Lower Hudson Chapter of the NYSAA.",
     "source": "NYSAA Bulletin No. 26 (1962)", "link": "/history/search?q=Brennan+Croton+excavation"},
    {"date": "1986", "title": "Croton Point landfill closes",
     "desc": "After 59 years of operation, the dump is closed. Environmental remediation and capping begins \u2014 a process that will take decades.",
     "source": "Croton Point Landfill Review (2019)", "link": "/history/story/09_five_lives_croton_point"},
    {"date": "2022", "title": "Daniel Nimham statue dedicated in Fishkill",
     "desc": "An eight-foot bronze statue by sculptor Michael Keropian is unveiled, honoring the last Wappinger sachem who traveled to London and died at Kingsbridge.",
     "source": "Wikipedia (Daniel Nimham)", "link": "/history/story/03_nimham_last_stand"},
]

# Group events by sub_id for sub-timelines
SUB_TIMELINES = {}
for item in TIMELINE:
    sid = item.get("sub_id")
    if sid:
        SUB_TIMELINES.setdefault(sid, []).append(item)

SUB_TIMELINE_META = {
    "kiefts_war": {"title": "Kieft's War (1626\u20131645)", "desc": "From a roadside robbery to a two-year war that shattered the Wappinger Confederacy"},
    "nimham": {"title": "Daniel Nimham & the Wappinger Land Fight (1693\u20131778)", "desc": "A century of dispossession, from the Philipse fraud to the Battle of Kingsbridge"},
    "aqueduct": {"title": "The Croton Water System (1837\u20131890)", "desc": "Two aqueducts and a celebration that brought fresh water to New York City"},
    "dam": {"title": "New Croton Dam Construction (1892\u20131906)", "desc": "Immigrant labor, the padrone system, a strike, and an engineering marvel"},
    "prohibition": {"title": "Prohibition in Croton (1921\u20131933)", "desc": "Rum planes, submarines, speakeasies, and undercover fiddlers"},
}


# ─── Routes ───────────────────────────────────────────────────────

@history_bp.route("/", strict_slashes=False)
def index():
    db = get_history_db()

    stats = []
    for col in COLLECTIONS:
        if col["sources"]:
            placeholders = ",".join("?" for _ in col["sources"])
            row = db.execute(f"""
                SELECT count(*) as chunks, sum(word_count) as words
                FROM chunks WHERE source IN ({placeholders})
            """, col["sources"]).fetchone()
        else:
            row = db.execute("""
                SELECT count(*) as chunks, sum(word_count) as words
                FROM chunks WHERE (source = '' OR source IS NULL)
                AND source_file LIKE '%_raw.txt'
            """).fetchone()
        stats.append({**col, "chunks": row["chunks"] or 0, "words": row["words"] or 0})

    totals = db.execute("""
        SELECT count(*) as chunks, sum(word_count) as words,
               count(DISTINCT source) as sources,
               count(DISTINCT source_file) as files
        FROM chunks
    """).fetchone()

    sources = db.execute("""
        SELECT source, count(*) as chunks, sum(word_count) as words
        FROM chunks WHERE source != '' AND source IS NOT NULL
        GROUP BY source ORDER BY words DESC
    """).fetchall()

    map_count = db.execute("SELECT count(*) FROM chunks WHERE source = 'Historical Map'").fetchone()[0]

    stories = _load_stories()

    mdb = get_mcdonald_db()
    mcdonald_stats = mdb.execute("""
        SELECT COUNT(*) AS n, SUM(word_count) AS w, SUM(page_count) AS p
        FROM interviews
    """).fetchone()
    mcdonald_findings = mdb.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    mcdonald_highlights = mdb.execute("""
        SELECT i.slug, i.interviewee, i.date,
               (SELECT COUNT(*) FROM findings f WHERE f.interview_id = i.id) AS fc
        FROM interviews i
        WHERE (SELECT COUNT(*) FROM findings f WHERE f.interview_id = i.id) > 0
        ORDER BY fc DESC, i.word_count DESC LIMIT 6
    """).fetchall()

    return render_template("history/index.html",
        collections=stats,
        sources=sources,
        totals=totals,
        source_urls=SOURCE_URLS,
        featured=FEATURED_TOPICS,
        map_count=map_count,
        latest_stories=stories,
        story_count=len(stories),
        mcdonald_stats=mcdonald_stats,
        mcdonald_findings=mcdonald_findings,
        mcdonald_highlights=mcdonald_highlights,
    )


@history_bp.route("/search")
def search():
    db = get_history_db()
    query = request.args.get("q", "").strip()
    collection = request.args.get("collection", "").strip()
    page = max(1, int(request.args.get("page", 1)))

    if not query:
        return render_template("history/search.html", query="", results=[], total=0, page=1, pages=1,
                               collections=COLLECTIONS, source_urls=SOURCE_URLS)

    fts_query = re.sub(r'[^\w\s"*]', '', query)
    if not fts_query.strip():
        return render_template("history/search.html", query=query, results=[], total=0, page=1, pages=1,
                               collections=COLLECTIONS, source_urls=SOURCE_URLS)

    source_filter = ""
    params = [fts_query]
    if collection:
        col = next((c for c in COLLECTIONS if c["id"] == collection), None)
        if col and col["sources"]:
            placeholders = ",".join("?" for _ in col["sources"])
            source_filter = f"AND c.source IN ({placeholders})"
            params.extend(col["sources"])
        elif col and col["id"] == "government":
            source_filter = "AND (c.source = '' OR c.source IS NULL)"

    total = db.execute(f"""
        SELECT count(*) FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ? {source_filter}
    """, params).fetchone()[0]
    pages = max(1, math.ceil(total / PER_PAGE))

    offset = (page - 1) * PER_PAGE
    results = db.execute(f"""
        SELECT c.id, c.source_file, c.title, c.source, c.word_count,
               snippet(chunks_fts, 0, '<mark>', '</mark>', '\u2026', 40) as snippet
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ? {source_filter}
        ORDER BY rank
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, offset]).fetchall()

    return render_template("history/search.html",
        query=query, collection=collection, results=results,
        total=total, page=page, pages=pages,
        collections=COLLECTIONS, source_urls=SOURCE_URLS,
        has_llm=bool(GROQ_KEY),
    )


@history_bp.route("/api/report")
def api_report():
    """Generate an AI-synthesized research report from search results."""
    db = get_history_db()
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"report": None, "error": "No query"})

    fts_query = re.sub(r'[^\w\s"*]', '', query)
    if not fts_query.strip():
        return jsonify({"report": None, "error": "Invalid query"})

    chunks = db.execute("""
        SELECT c.id, c.source_file, c.title, c.source, c.word_count, c.content
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ?
        ORDER BY rank LIMIT 10
    """, (fts_query,)).fetchall()

    if not chunks:
        return jsonify({"report": None, "error": "No documents found"})

    report = generate_report(query, chunks)
    sources_used = list(set(dict(c).get("source", "") for c in chunks if dict(c).get("source")))

    return jsonify({
        "report": report,
        "query": query,
        "sources_consulted": sources_used,
        "chunks_used": len(chunks),
    })


@history_bp.route("/collection/<collection_id>")
def collection_page(collection_id):
    db = get_history_db()
    col = next((c for c in COLLECTIONS if c["id"] == collection_id), None)
    if not col:
        abort(404)

    page = max(1, int(request.args.get("page", 1)))

    if col["sources"]:
        placeholders = ",".join("?" for _ in col["sources"])
        source_filter = f"source IN ({placeholders})"
        params = list(col["sources"])
    elif col["id"] == "government":
        source_filter = "(source = '' OR source IS NULL) AND source_file LIKE '%_raw.txt'"
        params = []
    else:
        source_filter = "1=1"
        params = []

    sources = db.execute(f"""
        SELECT source, source_file, count(*) as chunks, sum(word_count) as words
        FROM chunks WHERE {source_filter}
        GROUP BY source, source_file ORDER BY words DESC
    """, params).fetchall()

    total = db.execute(f"SELECT count(*) FROM chunks WHERE {source_filter}", params).fetchone()[0]
    pages = max(1, math.ceil(total / PER_PAGE))
    offset = (page - 1) * PER_PAGE

    documents = db.execute(f"""
        SELECT id, source_file, title, source, word_count,
               substr(content, 1, 300) as preview
        FROM chunks WHERE {source_filter}
        ORDER BY id LIMIT ? OFFSET ?
    """, params + [PER_PAGE, offset]).fetchall()

    return render_template("history/collection.html",
        col=col, sources=sources, documents=documents,
        total=total, page=page, pages=pages, source_urls=SOURCE_URLS)


@history_bp.route("/document/<int:doc_id>")
def document_page(doc_id):
    db = get_history_db()
    doc = db.execute("SELECT * FROM chunks WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        abort(404)

    siblings = db.execute("""
        SELECT id, title, chunk_index, word_count,
               substr(content, 1, 200) as preview
        FROM chunks WHERE source_file = ? ORDER BY chunk_index
    """, (doc["source_file"],)).fetchall()

    return render_template("history/document.html", doc=doc, siblings=siblings, source_urls=SOURCE_URLS)


@history_bp.route("/source/<path:source_file>")
def source_page(source_file):
    db = get_history_db()
    chunks = db.execute("""
        SELECT id, title, source, chunk_index, word_count, content
        FROM chunks WHERE source_file = ? ORDER BY chunk_index
    """, (source_file,)).fetchall()

    if not chunks:
        abort(404)

    total_words = sum(c["word_count"] for c in chunks)
    return render_template("history/source.html",
        source_file=source_file, chunks=chunks, total_words=total_words, source_urls=SOURCE_URLS)


@history_bp.route("/documents")
def documents_list():
    db = get_history_db()
    sources = db.execute("""
        SELECT source, source_file, count(*) as chunks,
               sum(word_count) as words, min(title) as sample_title
        FROM chunks GROUP BY source_file ORDER BY source, source_file
    """).fetchall()

    totals = db.execute("""
        SELECT count(DISTINCT source_file) as files,
               count(*) as chunks, sum(word_count) as words
        FROM chunks
    """).fetchone()

    return render_template("history/documents.html", sources=sources, totals=totals, source_urls=SOURCE_URLS)


@history_bp.route("/maps")
def maps_page():
    """Photo library organized by category."""
    maps_dir = os.path.join(RAG_DIR, "history", "sources", "maps")
    if not os.path.exists(maps_dir):
        return render_template("history/maps.html", categories=[], uncategorized=[], total=0)

    all_images = []
    for f in sorted(os.listdir(maps_dir)):
        if not f.lower().endswith(('.jpg', '.jpeg', '.png')):
            continue
        size = os.path.getsize(os.path.join(maps_dir, f))
        if size < 5000:
            continue
        all_images.append({"file": f, "size": size})

    # Categorize
    categories = []
    used = set()
    for cat in PHOTO_CATEGORIES:
        images = [img for img in all_images if cat["match"](img["file"])]
        for img in images:
            used.add(img["file"])
        if images:
            categories.append({**cat, "images": images, "count": len(images)})

    uncategorized = [img for img in all_images if img["file"] not in used]

    # Load metadata
    meta_path = os.path.join(maps_dir, "metadata.json")
    photo_meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            photo_meta = json.load(f)

    # Also get DB-registered maps with titles as fallback
    db = get_history_db()
    db_maps = {}
    for row in db.execute("SELECT source_file, title FROM chunks WHERE source = 'Historical Map'").fetchall():
        db_maps[row["source_file"]] = row["title"]

    return render_template("history/maps.html",
        categories=categories,
        uncategorized=uncategorized,
        db_maps=db_maps,
        photo_meta=photo_meta,
        total=len(all_images))


@history_bp.route("/maps/<path:filename>")
def serve_map(filename):
    maps_dir = os.path.join(RAG_DIR, "history", "sources", "maps")
    filepath = os.path.join(maps_dir, filename)
    if not os.path.exists(filepath):
        abort(404)
    ext = os.path.splitext(filename)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
    with open(filepath, "rb") as f:
        resp = Response(f.read(), mimetype=mime)
        resp.cache_control.max_age = 86400
        return resp


# ─── Stories routes ───────────────────────────────────────────────

@history_bp.route("/stories")
def stories_index():
    stories = _load_stories()
    return render_template("history/stories_index.html", stories=stories)


@history_bp.route("/story/<slug>")
def story_page(slug):
    stories = _load_stories()
    story = next((s for s in stories if s["slug"] == slug), None)
    if not story:
        abort(404)
    idx = next(i for i, s in enumerate(stories) if s["slug"] == slug)
    prev_story = stories[idx - 1] if idx > 0 else None
    next_story = stories[idx + 1] if idx < len(stories) - 1 else None
    return render_template("history/story.html", story=story, prev_story=prev_story, next_story=next_story)


# ─── McDonald routes ──────────────────────────────────────────────

@history_bp.route("/mcdonald")
def mcdonald_index():
    db = get_mcdonald_db()
    interviews = db.execute("""
        SELECT i.id, i.slug, i.interviewee, i.label, i.date, i.location,
               i.page_count, i.word_count, i.wchs_url, i.wchs_item_id,
               i.catalog_descr,
               (SELECT COUNT(*) FROM findings f WHERE f.interview_id = i.id) AS finding_count
        FROM interviews i
        ORDER BY CASE WHEN date IS NULL OR date = '' THEN 1 ELSE 0 END,
                 date, interviewee
    """).fetchall()
    totals = db.execute("""
        SELECT COUNT(*) AS n, SUM(word_count) AS w, SUM(page_count) AS p
        FROM interviews
    """).fetchone()
    findings_count = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    highlights = db.execute("""
        SELECT i.slug, i.interviewee, i.date, i.word_count,
               (SELECT COUNT(*) FROM findings f WHERE f.interview_id = i.id) AS fc,
               (SELECT group_concat(title, ' \u2022 ')
                  FROM (SELECT title FROM findings WHERE interview_id = i.id
                        ORDER BY rank LIMIT 2)) AS finding_titles
        FROM interviews i
        WHERE (SELECT COUNT(*) FROM findings f WHERE f.interview_id = i.id) > 0
        ORDER BY fc DESC, word_count DESC
        LIMIT 8
    """).fetchall()
    return render_template("history/mcdonald_index.html",
        interviews=interviews,
        highlights=highlights,
        total_interviews=totals["n"],
        total_words=totals["w"] or 0,
        total_pages=totals["p"] or 0,
        findings_count=findings_count,
    )


@history_bp.route("/mcdonald/<slug>")
def mcdonald_interview_page(slug):
    db = get_mcdonald_db()
    # Try slug as-is first, then with mcdonald_ prefix
    iv = db.execute("SELECT * FROM interviews WHERE slug = ?", (slug,)).fetchone()
    if not iv and not slug.startswith("mcdonald_"):
        slug = f"mcdonald_{slug}"
        iv = db.execute("SELECT * FROM interviews WHERE slug = ?", (slug,)).fetchone()
    if not iv:
        abort(404)
    pages = db.execute("""
        SELECT page_index, wchs_page_id, iiif_image_url, iiif_thumb_url
        FROM pages WHERE interview_id = ? ORDER BY page_index
    """, (iv["id"],)).fetchall()
    findings = db.execute("""
        SELECT title, body, related_story, rank
        FROM findings WHERE interview_id = ? ORDER BY rank
    """, (iv["id"],)).fetchall()
    mentions = db.execute("""
        SELECT entity, entity_type, context
        FROM mentions WHERE interview_id = ?
        ORDER BY entity_type, entity LIMIT 200
    """, (iv["id"],)).fetchall()
    nav = db.execute("""
        SELECT slug, interviewee FROM interviews ORDER BY date, interviewee
    """).fetchall()
    nav_idx = next((i for i, n in enumerate(nav) if n["slug"] == slug), None)
    prev_iv = nav[nav_idx - 1] if nav_idx and nav_idx > 0 else None
    next_iv = nav[nav_idx + 1] if nav_idx is not None and nav_idx < len(nav) - 1 else None
    return render_template("history/mcdonald_interview.html",
        iv=iv, pages=pages, findings=findings, mentions=mentions,
        prev_iv=prev_iv, next_iv=next_iv,
    )


@history_bp.route("/mcdonald/search")
def mcdonald_search():
    db = get_mcdonald_db()
    q = request.args.get("q", "").strip()
    results = []
    if q:
        fts_q = re.sub(r'[^\w\s"*]', '', q)
        if fts_q:
            try:
                rows = db.execute("""
                    SELECT f.passage_id, f.interviewee, f.slug,
                           snippet(passages_fts, 1, '<mark>', '</mark>', '\u2026', 30) AS snip,
                           p.passage_index
                    FROM passages_fts f
                    JOIN passages p ON p.id = f.passage_id
                    WHERE passages_fts MATCH ? ORDER BY rank LIMIT 60
                """, (fts_q,)).fetchall()
                results = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
    return render_template("history/mcdonald_search.html", q=q, results=results)


# ─── Timeline ────────────────────────────────────────────────────

@history_bp.route("/timeline")
def timeline_page():
    sub_id = request.args.get("sub")
    if sub_id and sub_id in SUB_TIMELINES:
        meta = SUB_TIMELINE_META.get(sub_id, {})
        return render_template("history/timeline.html",
            timeline=SUB_TIMELINES[sub_id],
            sub_title=meta.get("title", sub_id),
            sub_desc=meta.get("desc", ""),
            is_sub=True)
    return render_template("history/timeline.html",
        timeline=TIMELINE, sub_timelines=SUB_TIMELINE_META, is_sub=False)


# ─── API routes ───────────────────────────────────────────────────

@history_bp.route("/api/event")
def api_event():
    """Generate a mini-article for a timeline event with source document links."""
    db = get_history_db()
    title = request.args.get("title", "").strip()
    date = request.args.get("date", "").strip()
    desc = request.args.get("desc", "").strip()

    if not title:
        return jsonify({"summary": None, "docs": []})

    search_terms = re.sub(r'[^\w\s]', '', title)[:60]
    fts_query = ' OR '.join(search_terms.split()[:5])

    try:
        rows = db.execute("""
            SELECT c.id, c.source_file, c.title, c.source, c.word_count,
                   snippet(chunks_fts, 0, '', '', '...', 25) as snippet
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank LIMIT 6
        """, (fts_query,)).fetchall()
    except Exception:
        rows = []

    docs = []
    for r in rows:
        d = dict(r)
        docs.append({
            "id": d["id"],
            "title": d["title"] or d["source_file"],
            "source": d["source"] or "",
            "snippet": d["snippet"][:200],
            "word_count": d["word_count"],
        })

    summary = None
    if GROQ_KEY and docs:
        context = "\n".join(f"[{d['source']}] {d['snippet']}" for d in docs[:4])
        try:
            import requests as _http
            resp = _http.post(GROQ_URL,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": "Write a 2-3 sentence summary of this historical event for a timeline. Cite the source by author name. Be specific with dates and names. No thinking tags."},
                        {"role": "user", "content": f"Event: {title} ({date})\nDescription: {desc}\n\nSource documents:\n{context}"},
                    ],
                    "max_tokens": 250,
                    "temperature": 0.3,
                },
                timeout=20,
            )
            if resp.ok:
                content = resp.json()["choices"][0]["message"]["content"]
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
                content = re.sub(r'<\|think\|>.*?<\|/think\|>', '', content, flags=re.DOTALL)
                content = content.strip()
                if len(content) > 30:
                    summary = content
        except Exception:
            pass

    return jsonify({"summary": summary, "docs": docs, "title": title, "date": date})


@history_bp.route("/api/search")
def api_search():
    db = get_history_db()
    query = request.args.get("q", "").strip()
    limit = min(50, max(1, int(request.args.get("limit", 20))))

    if not query:
        return jsonify({"results": [], "total": 0})

    fts_query = re.sub(r'[^\w\s"*]', '', query)
    if not fts_query.strip():
        return jsonify({"results": [], "total": 0})

    results = db.execute("""
        SELECT c.id, c.source_file, c.title, c.source, c.word_count,
               snippet(chunks_fts, 0, '', '', '\u2026', 40) as snippet
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?
    """, (fts_query, limit)).fetchall()

    return jsonify({"query": query, "total": len(results), "results": [dict(r) for r in results]})
