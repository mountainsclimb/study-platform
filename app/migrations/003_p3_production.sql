-- Schema migration 003: P3 recovery codes, chunk embeddings cache & eval runs.

CREATE TABLE IF NOT EXISTS recovery_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON recovery_codes(user_id, used_at);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    document_id INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(document_id, chunk_id, model)
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    verify_model TEXT,
    prompt_version TEXT NOT NULL,
    total INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
