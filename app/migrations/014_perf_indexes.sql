-- 下一轮性能优化（P1）：高收益索引（SQLite / PostgreSQL 通用）。
-- sessions：登录校验与过期清理按 expires_at 过滤，避免全表扫。
-- auth_rate_events：限流记录清理/统计按时间过滤（bucket,at 组合索引已存在）。
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_rate_at ON auth_rate_events(at);
