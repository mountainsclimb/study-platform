-- Schema migration 013: P1 审核/评测工作流与 P2 公网准备所需的持久化结构。

-- P1: 用户题目报错与管理员校勘闭环（报错 → 待核对 → 待修订/确认无误 → 待发布 → 已发布）。
-- 题库本身保持只读，校勘结果作为可回放记录写入应用库，供数据侧重建消费。
CREATE TABLE IF NOT EXISTS content_issues (
    id INTEGER PRIMARY KEY,
    qid TEXT NOT NULL,
    group_id TEXT,
    bank_version TEXT,
    subject TEXT,
    field TEXT NOT NULL DEFAULT 'stem',
    issue_type TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending_check',
    risk TEXT NOT NULL DEFAULT 'medium',
    reporter_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decision TEXT,
    evidence TEXT,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_issues_status ON content_issues(status, created_at);
CREATE INDEX IF NOT EXISTS idx_content_issues_qid ON content_issues(qid, id);
CREATE INDEX IF NOT EXISTS idx_content_issues_filter ON content_issues(subject, risk, issue_type, status);

-- P1: 管理员按证据临时隔离。题库只读，隔离在应用层生效并保留撤销记录。
CREATE TABLE IF NOT EXISTS content_isolations (
    qid TEXT PRIMARY KEY,
    group_id TEXT,
    issue_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_isolation_group ON content_isolations(group_id);

-- P2: 运行策略（注册开关等），以运营显式开启动作为准。
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P2: 跨进程认证限流。进程内计数在两个 Web 进程之间不一致，改为数据库记录滑动窗口。
CREATE TABLE IF NOT EXISTS auth_rate_events (
    bucket TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'login',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_rate_bucket ON auth_rate_events(bucket, at);

-- P2: 存活/就绪检查所需的进程心跳（Worker、定时任务等）。
CREATE TABLE IF NOT EXISTS service_heartbeat (
    name TEXT PRIMARY KEY,
    beat_at TEXT NOT NULL,
    payload_json TEXT
);

-- P1: 评测分层与可追溯（题库版本、分层、输入完整率、完整报告快照）。
ALTER TABLE eval_runs ADD COLUMN bank_version TEXT;
ALTER TABLE eval_runs ADD COLUMN strata TEXT;
ALTER TABLE eval_runs ADD COLUMN input_stats_json TEXT;
ALTER TABLE eval_runs ADD COLUMN report_json TEXT;
