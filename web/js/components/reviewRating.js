/* 四档复习评分组件（《知序功能整合与优化总方案》§3 数学主流程第 4 步、§4「复习卡及日志」）。
 *
 * 后端口径（先读 app/study_review.py 确认，前端只做展示与接线）：
 * - POST /api/review-cards/sync：把当前用户错题幂等迁成复习卡，{created, updated, archived, total}；
 * - GET  /api/review-cards/due?limit=50&include_future=false：
 *   {items:[{card_id, card_key, card_type, question_ref, qid, due_at, state, reps, lapses,
 *            overdue_days, version(=card_version), algorithm, algorithm_version, ...}], total, now, message}；
 * - POST /api/review-cards/{card_id}/ratings {rating:1..4, card_version:int, idempotency_key:str}：
 *   服务端写入排程并返回唯一下次到期时间；**必传 card_version 与 idempotency_key**。
 *   - 409：卡片已被其它评分更新（页面负责中文提示并重新拉取 due）；
 *   - 200 + degraded=true：排程引擎暂不可用（降级），due_at 保持原日期、retry_allowed=true。
 *     前端**不得伪造新日期**，只显示「已保留最近一次有效到期时间」并允许用同一幂等键重试。
 * - 评分口径（中文）：1 忘记（重来）；2 困难；3 良好；4 容易。评分由服务端写入到期时间，
 *   首页/错题/练习/学习计划使用同一个日期。
 *
 * 用法：
 *   const rating = renderReviewRating({
 *     card,                                        // GET /due 返回的卡片对象
 *     onRate: async (rating, { idempotencyKey, card }) => result,  // 页面发分并返回服务端结果
 *   });
 *   row.append(rating.element);
 *   // 页面切走/列表重渲染时 rating.destroy()
 *
 * 本文件不改 core/*：只复用 api.js / dom.js / i18n.js / state.js 的既有 helper。
 */

import { api } from '../core/api.js';
import { el, badge, button, fill, query } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { onSessionReset } from '../core/state.js';

/** 后端本地参考算法标识与版本；只在服务端缺字段时用于展示兜底，不参与计算。 */
const DEFAULT_ALGORITHM = 'fsrs-reference';
const DEFAULT_ALGORITHM_VERSION = '5.0-local';

/** 四档评分的取值与展示顺序（1=忘记/Again，2=困难/Hard，3=良好/Good，4=容易/Easy）。 */
const RATING_VALUES = Object.freeze([1, 2, 3, 4]);

const RATING_LABEL_KEYS = Object.freeze({
  1: 'study.review.again',
  2: 'study.review.hard',
  3: 'study.review.good',
  4: 'study.review.easy',
});

const RATING_HINT_KEYS = Object.freeze({
  1: 'study.review.againHint',
  2: 'study.review.hardHint',
  3: 'study.review.goodHint',
  4: 'study.review.easyHint',
});

const RATING_CLASS_NAMES = Object.freeze({
  1: 'again',
  2: 'hard',
  3: 'good',
  4: 'easy',
});

const STATE_LABEL_KEYS = Object.freeze({
  new: 'study.review.state.new',
  learning: 'study.review.state.learning',
  review: 'study.review.state.review',
  relearning: 'study.review.state.relearning',
});

const STATE_BADGE_VARIANTS = Object.freeze({
  new: 'blue',
  learning: 'amber',
  review: 'teal',
  relearning: 'red',
});

/* ------------------------------------------------------------------------- *
 * 导出的小工具函数（页面/测试复用）
 * ------------------------------------------------------------------------- */

/** 评分档中文名：1 忘记 / 2 困难 / 3 良好 / 4 容易；非法值返回空串。 */
export function ratingLabel(rating) {
  const key = RATING_LABEL_KEYS[Number(rating)];
  return key ? t(key) : '';
}

/** 服务端时间 → 本地 YYYY-MM-DD（只看日期，不伪造时间）；非法值返回空串。 */
export function formatDue(dueAt) {
  if (!dueAt) return '';
  const date = dueAt instanceof Date ? dueAt : new Date(dueAt);
  if (Number.isNaN(date.getTime())) return '';
  const pad = value => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 算法展示串：``fsrs-reference v5.0-local``；版本已带 v 前缀时不重复加。 */
export function algorithmLabel(result) {
  const algorithm = String(result?.algorithm || DEFAULT_ALGORITHM).trim() || DEFAULT_ALGORITHM;
  let version = String(result?.algorithm_version || DEFAULT_ALGORITHM_VERSION).trim();
  if (version && !version.startsWith('v')) version = `v${version}`;
  return version ? `${algorithm} ${version}` : algorithm;
}

/**
 * 评分结果 → 中文提示文案。
 * - 正常/幂等重放：``下一次复习：YYYY-MM-DD（fsrs-reference v5.0-local）``；
 * - 降级（degraded）：返回服务端降级提示，调用方不得再展示任何新日期。
 */
export function formatDueNote(result) {
  if (!result || typeof result !== 'object') return '';
  if (result.degraded) return t('study.review.degraded');
  const due = formatDue(result.due_at);
  if (!due) return '';
  return t('study.review.nextDue', { date: due, algorithm: algorithmLabel(result) });
}

/** 409 版本冲突识别：后端 detail 为「复习卡已被更新，请刷新后重试」，页面据此刷新重试。 */
export function isVersionConflict(error) {
  if (!error) return false;
  if (error.conflict === true || error.status === 409) return true;
  return /复习卡已被更新|刷新后重试/.test(String(error.message || ''));
}

/* ------------------------------------------------------------------------- *
 * 页面级只读工具：迁移与到期队列（都不向页面抛异常）
 * ------------------------------------------------------------------------- */

/** 会话内只调用一次 POST /api/review-cards/sync；失败静默返回 null，不阻断页面。 */
let syncPromise = null;

export function ensureReviewCards() {
  if (!syncPromise) {
    syncPromise = api('/review-cards/sync', { method: 'POST' })
      .then(result => (result && typeof result === 'object' ? result : null))
      .catch(() => null);
  }
  return syncPromise;
}

// 退出登录/切换用户后允许新会话重新迁移一次，避免把上个用户的迁移结果当成本会话结果。
onSessionReset(() => {
  syncPromise = null;
});

/**
 * 封装 GET /api/review-cards/due：失败返回 ``{items: [], unavailable: true}``，不抛异常。
 * @param {number} limit 1..200
 * @param {{includeFuture?: boolean}} [options]
 */
export async function dueCards(limit = 50, options = {}) {
  try {
    const params = query({
      limit,
      include_future: options.includeFuture ? 'true' : '',
    });
    const data = await api(`/review-cards/due${params ? `?${params}` : ''}`);
    const items = Array.isArray(data?.items)
      ? data.items.filter(item => item && item.card_id !== undefined && item.card_id !== null)
      : [];
    return {
      items,
      total: Number(data?.total ?? items.length) || 0,
      now: data?.now || '',
      message: data?.message || '',
      unavailable: false,
    };
  } catch {
    // 接口不可用属于降级场景：页面照常展示旧错题功能，不弹错误、不阻断渲染。
    return { items: [], total: 0, now: '', message: '', unavailable: true };
  }
}

/* ------------------------------------------------------------------------- *
 * 幂等键：优先 crypto.randomUUID()，不可用时降级 r5-<时间戳>-<随机>
 * ------------------------------------------------------------------------- */

function newIdempotencyKey() {
  // 优先标准 API：crypto.randomUUID()（HTTPS / localhost 安全上下文提供）。
  // 不可用（老浏览器、非安全上下文、内嵌 WebView）时降级为 r5-<时间戳>-<随机>，
  // 仍满足服务端幂等键字符集 [A-Za-z0-9._:+-] 与 ≤120 字符长度。
  const cryptoObject = typeof window !== 'undefined' ? window.crypto : globalThis.crypto;
  if (cryptoObject && typeof cryptoObject.randomUUID === 'function') {
    try {
      return cryptoObject.randomUUID();
    } catch {
      /* 非安全上下文等异常：继续走降级键 */
    }
  }
  return `r5-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/* ------------------------------------------------------------------------- *
 * 组件主体
 * ------------------------------------------------------------------------- */

/**
 * 渲染四档评分区。
 *
 * @param {object} options
 * @param {object} options.card GET /review-cards/due 返回的卡片（含 card_id/due_at/state/version）
 * @param {(rating:number, context:{idempotencyKey:string, card:object}) => Promise<object>} options.onRate
 *        页面发分函数；返回服务端评分响应，抛错时组件展示错误并允许重试。
 * @param {{notice?: string}} [options.labels] 可选静态说明（显示在到期信息上方）。
 * @returns {{element: HTMLElement, destroy: Function, setCard: Function, setBusy: Function, setResult: Function}}
 */
export function renderReviewRating(options = {}) {
  let card = options.card && typeof options.card === 'object' ? { ...options.card } : null;
  const onRate = typeof options.onRate === 'function' ? options.onRate : null;
  const labels = options.labels && typeof options.labels === 'object' ? options.labels : {};

  let destroyed = false;
  let busy = false; // 外部 setBusy 控制
  let pending = false; // 本组件内部请求中
  let lastAttempt = null; // { rating, idempotencyKey }：弱网/降级重试复用同一键

  const noticeNode = labels.notice
    ? el('p', { class: 'review-rating-notice' }, String(labels.notice))
    : null;
  const head = el('div', { class: 'review-rating-head' });
  const buttonsWrap = el('div', { class: 'review-rating-buttons' });
  const resultArea = el('div', { class: 'review-rating-result', role: 'status', 'aria-live': 'polite' });
  const buttons = new Map();

  for (const rating of RATING_VALUES) {
    const label = ratingLabel(rating);
    const node = el('button', {
      type: 'button',
      class: `review-rating-button review-rating-button--${RATING_CLASS_NAMES[rating]}`,
      'data-rating': String(rating),
      'aria-label': t('study.review.buttonAria', { label, rating }),
      title: t(RATING_HINT_KEYS[rating]),
      onClick: () => {
        if (!onRate || destroyed || busy || pending) return;
        // 每次新的点击生成全新幂等键；「重试」按钮才复用上一次键（弱网/降级）。
        lastAttempt = { rating, idempotencyKey: newIdempotencyKey(), failed: false };
        submit(lastAttempt);
      },
    },
    el('span', { class: 'review-rating-button-name' }, label),
    el('span', { class: 'review-rating-button-key', 'aria-hidden': 'true' }, String(rating)));
    buttons.set(rating, node);
    buttonsWrap.append(node);
  }

  const group = el('section', {
    class: 'review-rating',
    role: 'group',
    'aria-label': t('study.review.groupAria'),
  }, noticeNode, head, buttonsWrap, resultArea);

  renderHead();
  syncDisabled();

  return { element: group, destroy, setCard, setBusy, setResult };

  /* ------------------------------ 渲染 ------------------------------ */

  function overdueDays() {
    const serverDays = Number(card?.overdue_days);
    if (Number.isFinite(serverDays) && serverDays >= 0) return Math.floor(serverDays);
    const dueMs = Date.parse(String(card?.due_at || ''));
    if (!Number.isFinite(dueMs)) return 0;
    const diff = Date.now() - dueMs;
    return diff > 0 ? Math.floor(diff / 86400000) : 0;
  }

  function renderHead() {
    if (destroyed) return;
    if (!card) {
      fill(head, el('span', { class: 'review-rating-due' }, t('study.review.cardMissing')));
      return;
    }
    const due = formatDue(card.due_at);
    const dueMs = Date.parse(String(card.due_at || ''));
    let dueText;
    if (Number.isFinite(dueMs) && dueMs > Date.now() + 60 * 1000) {
      // 评分成功后卡片可能已不在「到期」状态：按普通到期日展示，不再谎称今天到期。
      dueText = due ? t('study.review.due', { date: due }) : t('study.review.cardMissing');
    } else if (overdueDays() > 0) {
      dueText = `${t('study.review.due', { date: due })} · ${t('study.review.overdue', { days: overdueDays() })}`;
    } else {
      dueText = t('study.review.dueToday');
    }
    const state = String(card.state || '').toLowerCase();
    const stateKey = STATE_LABEL_KEYS[state];
    fill(head,
      el('span', { class: 'review-rating-due' }, dueText),
      stateKey ? badge(t(stateKey), STATE_BADGE_VARIANTS[state] || 'blue') : null,
      el('span', { class: 'review-rating-algo', title: t('study.review.algoTitle') }, algorithmLabel(card)));
  }

  function renderBusy(rating) {
    fill(resultArea, el('p', { class: 'review-rating-saving' },
      t('study.review.saving', { label: ratingLabel(rating) })));
  }

  function renderError(message, { retry = false } = {}) {
    fill(resultArea,
      el('p', { class: 'review-rating-error' }, message || t('common.actionFailed')),
      retry && lastAttempt
        ? button(t('study.review.retry'), () => retryLast(), 'secondary small')
        : null);
  }

  function applyResult(result) {
    if (!result || typeof result !== 'object') {
      renderError(t('study.review.invalidResult'), { retry: true });
      return;
    }
    if (result.card) setCard(result.card);
    if (result.degraded) {
      // 降级：到期时间保持原值，绝不展示/伪造新日期，允许同一幂等键重试。
      const keptDue = formatDue(result.due_at);
      fill(resultArea,
        el('p', { class: 'review-rating-degraded' }, t('study.review.degraded')),
        keptDue
          ? el('p', { class: 'review-rating-degraded-hint' }, t('study.review.degradedDue', { date: keptDue }))
          : null,
        result.retry_allowed === false
          ? null
          : button(t('study.review.retry'), () => retryLast(), 'secondary small'));
      return;
    }
    const replayed = Boolean(result.replayed || result.replay);
    const dueNote = formatDueNote(result);
    fill(resultArea,
      el('p', { class: 'review-rating-result-note' },
        t('study.review.applied', { label: ratingLabel(result.rating) }),
        replayed ? ` ${t('study.review.replayed')}` : ''),
      dueNote ? el('p', { class: 'review-rating-result-due' }, dueNote) : null);
  }

  /* ------------------------------ 状态 ------------------------------ */

  function syncDisabled() {
    const locked = destroyed || busy || pending || !onRate;
    for (const node of buttons.values()) node.disabled = locked;
    group.setAttribute('aria-busy', busy || pending ? 'true' : 'false');
  }

  /* ------------------------------ 请求 ------------------------------ */

  async function retryLast() {
    if (!lastAttempt || destroyed || busy || pending) return;
    await submit(lastAttempt);
  }

  async function submit(attempt) {
    if (!onRate || destroyed || busy || pending) return;
    pending = true;
    syncDisabled();
    renderBusy(attempt.rating);
    try {
      const result = await onRate(attempt.rating, {
        idempotencyKey: attempt.idempotencyKey,
        card,
      });
      if (destroyed) return;
      attempt.failed = false;
      applyResult(result);
    } catch (error) {
      if (destroyed) return;
      attempt.failed = true;
      const conflict = isVersionConflict(error);
      // 409 时页面重新拉到了最新卡（error.card）：先同步版本，用户可直接重新评分。
      if (error?.card) setCard(error.card);
      renderError(error?.message || t('common.actionFailed'), { retry: !conflict });
    } finally {
      if (!destroyed) {
        pending = false;
        syncDisabled();
      }
    }
  }

  /* ------------------------------ 生命周期 ------------------------------ */

  function destroy() {
    destroyed = true;
    lastAttempt = null;
    if (group.isConnected) group.remove();
  }

  function setCard(nextCard) {
    if (destroyed) return;
    card = nextCard && typeof nextCard === 'object' ? { ...nextCard } : null;
    renderHead();
  }

  function setBusy(value) {
    if (destroyed) return;
    busy = Boolean(value);
    syncDisabled();
  }

  function setResult(result) {
    if (destroyed) return;
    if (!result) {
      fill(resultArea);
      return;
    }
    applyResult(result);
  }
}
