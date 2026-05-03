-- Meetings: the primary unit of content
-- One meeting = one evening session of a committee
-- Multiple source documents (minutes PDFs, transcript) feed into one meeting
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    committee TEXT NOT NULL,
    event_id TEXT,           -- ChampDS event ID (for transcript/video linking)

    -- Source documents
    doc_ids TEXT,             -- comma-separated eCode360 doc IDs for this meeting
    has_transcript INTEGER DEFAULT 0,
    has_minutes INTEGER DEFAULT 0,
    has_video INTEGER DEFAULT 0,
    has_audio INTEGER DEFAULT 0,

    -- Three content types
    quick_summary TEXT,       -- 1-2 sentences for search results and cards
    complete_summary TEXT,    -- structured bullet points, detailed recap
    article TEXT,             -- full journalism piece for front page
    headline TEXT,            -- article headline

    -- Metadata
    word_count INTEGER,       -- transcript word count
    speaker_count INTEGER,
    duration_seconds REAL,

    -- Generation tracking
    article_model TEXT,       -- which model wrote the article
    article_generated_at TEXT,
    summary_model TEXT,
    summary_generated_at TEXT,

    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, committee)
);

CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date);
CREATE INDEX IF NOT EXISTS idx_meetings_committee ON meetings(committee);
CREATE INDEX IF NOT EXISTS idx_meetings_event ON meetings(event_id);
