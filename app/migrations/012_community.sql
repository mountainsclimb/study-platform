-- Private until the creator opts in. Older explanations have no recorded share provenance.
ALTER TABLE ai_explanations ADD COLUMN share_eligible INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_explanations ADD COLUMN shared_at TEXT;
ALTER TABLE ai_explanations ADD COLUMN featured_at TEXT;
CREATE INDEX IF NOT EXISTS idx_ai_explanations_community
    ON ai_explanations(qid, featured_at, id) WHERE shared_at IS NOT NULL;

ALTER TABLE prompt_templates ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_prompt_templates_shared
    ON prompt_templates(is_recommended, updated_at, id) WHERE is_shared=1;
