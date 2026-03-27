-- db/schema.sql
-- Run this in the Supabase SQL editor to initialise the database.

-- ── Users ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT UNIQUE NOT NULL,      -- Telegram chat ID
    keyword_profile TEXT,                         -- comma-separated topic descriptions
    active          BOOLEAN DEFAULT TRUE,
    max_papers      INT DEFAULT 10,               -- max papers per daily digest
    pending_action  TEXT,                         -- "awaiting_topics" etc. for multi-step commands
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Papers ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS papers (
    id              TEXT PRIMARY KEY,             -- "arxiv:2403.12345" | "s2:abc123" | "pm:38012345"
    title           TEXT NOT NULL,
    abstract        TEXT,
    authors         TEXT[],                       -- first 5 authors
    published_date  DATE NOT NULL,
    source          TEXT NOT NULL,                -- "arxiv" | "semantic_scholar" | "pubmed"
    venue           TEXT,                         -- journal/conference name
    url             TEXT NOT NULL,
    pdf_url         TEXT,
    title_hash      TEXT UNIQUE NOT NULL,         -- SHA256 of normalised title, for dedup
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published_date DESC);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);

-- ── LLM scores ───────────────────────────────────────────────────────────────
-- Scores are stored per (paper, user) so different users with different
-- profiles can have different relevance scores for the same paper.
CREATE TABLE IF NOT EXISTS paper_scores (
    id              BIGSERIAL PRIMARY KEY,
    paper_id        TEXT REFERENCES papers(id) ON DELETE CASCADE,
    user_id         BIGINT REFERENCES users(id) ON DELETE CASCADE,
    llm_score       NUMERIC(4,2),                 -- 0.00–10.00 from LLM
    pref_score      NUMERIC(4,2),                 -- 0.00–10.00 from preference model (nullable)
    final_score     NUMERIC(4,2) GENERATED ALWAYS AS (
                        CASE
                            WHEN pref_score IS NULL THEN llm_score
                            ELSE ROUND(0.6 * llm_score + 0.4 * pref_score, 2)
                        END
                    ) STORED,
    llm_explanation TEXT,                         -- 1-sentence reason from LLM
    scored_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(paper_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_scores_user_date ON paper_scores(user_id, scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_scores_final ON paper_scores(final_score DESC);

-- ── Ratings ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ratings (
    id              BIGSERIAL PRIMARY KEY,
    user_chat_id    BIGINT NOT NULL,              -- Telegram chat_id (denormalised for speed)
    paper_id        TEXT REFERENCES papers(id) ON DELETE CASCADE,
    score           SMALLINT NOT NULL CHECK (score IN (1, 5, 10)),  -- 👎=1, 👍=5, ❤️=10
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_chat_id, paper_id)               -- one rating per user per paper
);

CREATE INDEX IF NOT EXISTS idx_ratings_user ON ratings(user_chat_id, created_at DESC);

-- ── Digest log ───────────────────────────────────────────────────────────────
-- Tracks which papers were sent on which day so we never re-send.
CREATE TABLE IF NOT EXISTS digest_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT REFERENCES users(id) ON DELETE CASCADE,
    paper_id        TEXT REFERENCES papers(id) ON DELETE CASCADE,
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, paper_id)
);
