/* 学习事件上报（阶段 5-3）：演示 / 公式 / 语音的成功使用频次与耗时。
 *
 * 隐私边界：只上报 `feature + action + source + subject + duration_ms`，不包含朗读
 * 文本、公式内容、音频或文件名；上报失败静默忽略，不影响正在使用的功能。
 * 只在功能真正成功后调用，不在点击入口/取消/失败时调用。
 */
const ENDPOINT = '/api/evidence/feature-event';
const MAX_DURATION_MS = 3_600_000;

/**
 * 上报一次成功使用。
 * @param {'demo'|'formula'|'speech'} feature
 * @param {{ source?: string, subject?: string, durationMs?: number }} detail
 */
export function reportFeatureUse(feature, detail = {}) {
  const featureId = String(feature || '').trim();
  if (!featureId) return;
  const duration = Math.max(
    0,
    Math.min(MAX_DURATION_MS, Math.round(Number(detail.durationMs) || 0)),
  );
  const payload = {
    feature: featureId,
    action: 'success',
    source: String(detail.source || '').slice(0, 40),
    subject: String(detail.subject || '').slice(0, 40),
    duration_ms: duration,
  };
  try {
    fetch(ENDPOINT, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
      body: JSON.stringify(payload),
    }).catch(() => { /* 上报失败不影响使用 */ });
  } catch {
    /* 网络层同步异常同样忽略 */
  }
}
