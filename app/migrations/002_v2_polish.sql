-- Schema migration 002: Private wrongbook for user-uploaded photo questions.

CREATE TABLE IF NOT EXISTS upload_wrong_questions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    upload_id INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    stem_preview TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '拍题加入错题本',
    error_tags_json TEXT NOT NULL DEFAULT '[]',
    next_review_at TEXT NOT NULL,
    added_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, upload_id)
);
CREATE INDEX IF NOT EXISTS idx_upload_wrong_user ON upload_wrong_questions(user_id, archived, next_review_at);
