-- Croton Knowledge Graph + Vector + Keyword RAG
-- SQLite with sqlite-vec for vectors, FTS5 for keyword search

-- ═══ KNOWLEDGE GRAPH ═══

-- Entities: people, places, organizations, projects, topics
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,  -- 'person', 'street', 'organization', 'project', 'topic', 'building'
    slug TEXT UNIQUE,
    metadata_json TEXT,  -- role, address, description, etc.
    first_seen_date TEXT,
    last_seen_date TEXT,
    mention_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

-- Relationships between entities
CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES entities(id),
    target_id INTEGER REFERENCES entities(id),
    type TEXT NOT NULL,  -- 'spoke_at', 'lives_on', 'member_of', 'voted_on', 'funded_by', 'located_at'
    weight REAL DEFAULT 1.0,
    context TEXT,  -- brief description of the relationship
    doc_id TEXT,   -- which document established this relationship
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(type);

-- ═══ DOCUMENT CHUNKS ═══

-- Chunks of text from meetings, transcripts, articles
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,        -- meeting/transcript ID
    doc_type TEXT NOT NULL,      -- 'minutes', 'transcript', 'article'
    committee TEXT,
    date TEXT,
    chunk_index INTEGER,
    content TEXT NOT NULL,
    speaker TEXT,                -- from diarized transcripts
    start_time REAL,             -- timestamp in seconds (for transcripts)
    end_time REAL,
    sentiment TEXT,              -- 'positive', 'neutral', 'negative'
    sentiment_score REAL,
    char_count INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_date ON chunks(date);
CREATE INDEX IF NOT EXISTS idx_chunks_speaker ON chunks(speaker);

-- Full-text search on chunks
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    speaker,
    committee,
    content=chunks,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- ═══ VECTOR EMBEDDINGS ═══

-- Stored as blobs (sqlite-vec or manual cosine similarity)
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
    embedding BLOB NOT NULL,  -- float32 array serialized
    model TEXT DEFAULT 'gemini-embedding-2-preview',
    dimension INTEGER DEFAULT 3072
);

-- ═══ ENTITY-CHUNK LINKS ═══

-- Which entities appear in which chunks
CREATE TABLE IF NOT EXISTS entity_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER REFERENCES entities(id),
    chunk_id INTEGER REFERENCES chunks(id),
    role TEXT,  -- 'speaker', 'mentioned', 'subject', 'location'
    UNIQUE(entity_id, chunk_id, role)
);
CREATE INDEX IF NOT EXISTS idx_em_entity ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_em_chunk ON entity_mentions(chunk_id);

-- ═══ TOPIC THREADS ═══

-- Track topics across meetings (court consolidation, Gouveia Park, etc.)
CREATE TABLE IF NOT EXISTS topic_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    description TEXT,
    first_date TEXT,
    last_date TEXT,
    meeting_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',  -- 'active', 'resolved', 'dormant'
    created_at TEXT DEFAULT (datetime('now'))
);

-- Link topics to chunks
CREATE TABLE IF NOT EXISTS topic_mentions (
    topic_id INTEGER REFERENCES topic_threads(id),
    chunk_id INTEGER REFERENCES chunks(id),
    relevance REAL DEFAULT 1.0,
    PRIMARY KEY(topic_id, chunk_id)
);

-- ═══ DOLLAR TRACKING ═══

-- Track spending/revenue across meetings
CREATE TABLE IF NOT EXISTS financial_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    description TEXT,
    type TEXT,  -- 'expenditure', 'revenue', 'grant', 'contract', 'tax', 'fee'
    resolution TEXT,  -- resolution number if applicable
    doc_id TEXT,
    date TEXT,
    entity_id INTEGER REFERENCES entities(id),
    topic_id INTEGER REFERENCES topic_threads(id)
);
CREATE INDEX IF NOT EXISTS idx_fin_date ON financial_items(date);
CREATE INDEX IF NOT EXISTS idx_fin_type ON financial_items(type);
