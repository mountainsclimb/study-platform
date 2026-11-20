-- Schema migration 001: Knowledge points, enhanced wrongbook, model capabilities,
-- usage tracking, persisted AI explanations, note versions, prompt templates, and FTS metadata.

CREATE TABLE IF NOT EXISTS bank_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_points (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    parent_id INTEGER REFERENCES knowledge_points(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    sort INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'official'
);
CREATE INDEX IF NOT EXISTS idx_kp_subject_parent ON knowledge_points(subject, parent_id, sort);

CREATE TABLE IF NOT EXISTS question_kp (
    qid TEXT NOT NULL,
    kp_id INTEGER NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'admin',
    model TEXT,
    confidence REAL,
    reviewed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(qid, kp_id)
);
CREATE INDEX IF NOT EXISTS idx_qkp_kp ON question_kp(kp_id);
CREATE INDEX IF NOT EXISTS idx_qkp_source ON question_kp(source);

CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(subject, name)
);

CREATE TABLE IF NOT EXISTS question_method (
    qid TEXT NOT NULL,
    method_id INTEGER NOT NULL REFERENCES methods(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'ai',
    PRIMARY KEY(qid, method_id)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    prefs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_models (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    caps_json TEXT NOT NULL DEFAULT '{}',
    input_price REAL,
    output_price REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_models_user ON ai_models(user_id, provider_id);

CREATE TABLE IF NOT EXISTS ai_task_defaults (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task TEXT NOT NULL,
    primary_provider_id INTEGER,
    primary_model TEXT,
    verify_provider_id INTEGER,
    verify_model TEXT,
    judge_provider_id INTEGER,
    judge_model TEXT,
    mode TEXT NOT NULL DEFAULT 'fast',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, task)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id INTEGER,
    provider_id INTEGER,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_user_date ON ai_usage(user_id, created_at);

CREATE TABLE IF NOT EXISTS ai_allowed_hosts (
    hostname TEXT PRIMARY KEY,
    added_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_explanations (
    id INTEGER PRIMARY KEY,
    qid TEXT,
    upload_id INTEGER,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'detailed',
    models_json TEXT NOT NULL DEFAULT '[]',
    content_json TEXT NOT NULL,
    verification_json TEXT,
    prompt_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_exp_user_qid ON ai_explanations(user_id, qid, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_exp_user_upload ON ai_explanations(user_id, upload_id, id DESC);

CREATE TABLE IF NOT EXISTS note_versions (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_note_versions_note ON note_versions(note_id, version_no DESC);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    custom_requirements TEXT NOT NULL DEFAULT '',
    is_recommended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompt_tpl_user ON prompt_templates(user_id);
