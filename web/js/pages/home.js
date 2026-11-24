import { api } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, heading, number, dateText, resultLabel, resultBadge, readableSummary, badge,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { taskNames, statusNames } from '../core/jobs.js';
import { t } from '../core/i18n.js';

// 平台精选（WS19 `/api/questions?source=`）能力探测 + 可练数量。
// 用合法 official 请求探测：旧后端忽略未知参数、响应体没有 official_total → 未就绪，
// 首页不显示平台精选计数（不伪造数字）。避免用非法 source 触发 4xx 控制台错误。
let featuredPracticePromise = null;

function detectFeaturedPractice() {
  if (!featuredPracticePromise) {
    featuredPracticePromise = (async () => {
      try {
        const data = await api('/questions?page=1&page_size=1&source=official');
        if (!data || typeof data !== 'object' || !('official_total' in data)) return null;
        const total = Number(data.official_total);
        return Number.isFinite(total) ? total : null;
      } catch {
        return null;
      }
    })();
  }
  return featuredPracticePromise;
}

function inlineStat(value, label) {
  return el('div', { class: 'pg-inline-stat' },
    el('strong', {}, value),
    el('span', {}, label));
}

/* =========================================================================
 * M2 复习到期统一口径（服务端唯一到期值）
 *
 * 后端契约（app/home_stats.py::_review_payload → GET /api/insight/home 顶层 review 块，
 * 先读代码核对）：
 *   {available, due_now, due_today, total, algorithm, algorithm_version, due_source, caliber, note}
 * - available=true：due_now/due_today 来自服务端四档复习卡（fsrs-reference v5.0-local），
 *   是首页/错题本/复习队列/学情共用的同一个到期值；
 * - available=false：复习排程降级，due_now 为 null（不是 0）——必须回退旧字段
 *   personal.due_reviews（同源镜像）并显示中文降级提示，绝不显示 0 冒充数据；
 * - 旧后端没有 review 块：同样回退旧字段，但不提示降级（避免把「字段未就绪」误报成故障）。
 * ========================================================================= */

/** 算法展示串：``fsrs-reference v5.0-local``；服务端缺字段时按契约默认值兜底（不参与计算）。 */
function reviewAlgorithmText(review) {
  const algorithm = String(review?.algorithm || 'fsrs-reference').trim() || 'fsrs-reference';
  let version = String(review?.algorithm_version || '').trim();
  if (!version) return algorithm;
  if (!version.startsWith('v')) version = `v${version}`;
  return `${algorithm} ${version}`;
}

/**
 * 首页到期数字快照：优先 review 块，降级/旧后端回退旧错题到期字段（镜像值），不做本地估算。
 * @returns {{review: object|null, available: boolean, degraded: boolean, dueNow: number|null,
 *   dueToday: number|null, legacy: number|null, count: number|null, algorithmText: string,
 *   degradeNote: string}}
 */
function reviewDueSnapshot(homeStats, personal, stats) {
  const review = homeStats?.review && typeof homeStats.review === 'object' ? homeStats.review : null;
  const rawDueNow = review?.due_now;
  const available = Boolean(review
    && review.available === true
    && rawDueNow !== null && rawDueNow !== undefined
    && Number.isFinite(Number(rawDueNow)));
  const rawDueToday = review?.due_today;
  const dueToday = rawDueToday === null || rawDueToday === undefined || !Number.isFinite(Number(rawDueToday))
    ? null
    : Number(rawDueToday);
  const legacy = personal
    ? (Number.isFinite(Number(personal.due_reviews)) ? Number(personal.due_reviews) : 0)
    : (Number.isFinite(Number(stats?.due_count)) ? Number(stats.due_count) : null);
  return {
    review,
    available,
    // 只有「接口返回了 review 且明确 available=false」才提示降级；旧后端缺块保持原有空态/隐藏。
    degraded: Boolean(review && review.available === false),
    dueNow: available ? Number(rawDueNow) : null,
    dueToday: available ? dueToday : null,
    legacy,
    count: available ? Number(rawDueNow) : legacy,
    algorithmText: reviewAlgorithmText(review),
    // 服务端降级说明（含异常类型）优先；没有时用固定文案键兜底。
    degradeNote: review && review.available === false ? String(review.note || '').trim() : '',
  };
}

/* 平台精选学情口径（WS26 契约）：`/api/insight/home` 顶层 official 对象
 * （practice_pool / practiced / wrong_count 等，撤回/争议题不计入）；`/api/stats`
 * 同样下发顶层 official（既有平铺 official_* 字段保持兼容）。字段缺失即未就绪：
 * 整卡不渲染，绝不写死数字。 */
function officialCaliberOf(stats, homeStats) {
  const official = homeStats?.official || stats?.official;
  if (!official || typeof official !== 'object') return null;
  const firstNumber = (...values) => values.find(value =>
    value !== null && value !== undefined && Number.isFinite(Number(value)));
  const values = {
    pool: firstNumber(official.practice_pool, official.total_questions,
      homeStats?.bank?.official_practice_pool),
    practiced: firstNumber(official.practiced),
    wrong: firstNumber(official.wrong_count),
  };
  if ([values.pool, values.practiced, values.wrong].every(value => value === undefined)) return null;
  return { ...values, note: String(official.caliber || official.note || '').trim() };
}

function officialCaliberCard(stats, homeStats) {
  const caliber = officialCaliberOf(stats, homeStats);
  if (!caliber) return null;
  const items = [];
  if (caliber.pool !== undefined) {
    items.push(inlineStat(t('home.featured.value', { count: number(caliber.pool) }),
      t('home.featured.practiceLabel')));
  }
  if (caliber.practiced !== undefined) {
    items.push(inlineStat(t('home.featured.value', { count: number(caliber.practiced) }),
      t('home.official.practicedLabel')));
  }
  if (caliber.wrong !== undefined) {
    items.push(inlineStat(t('home.featured.value', { count: number(caliber.wrong) }),
      t('home.official.wrongLabel')));
  }
  return el('section', { class: 'card home-official-caliber', 'data-official-caliber': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('home.official.title')),
        el('p', { class: 'pg-card-sub' }, caliber.note || t('home.official.sub'))),
      button(t('home.featured.goPractice'), () => go('practice', null, { source: 'official' }),
        'ghost small', 'layers')),
    el('div', { class: 'pg-inline-stats', 'aria-label': t('home.official.aria') }, ...items));
}

/* 首屏三动作主卡（方案 §1）：拍题入库 / 我的题库 / 错题复习。
 * 计数全部来自 /api/insight/home 的真实统计（题库只读库 + 应用库），接口失败时
 * 显示「暂不可用」并保留入口，不在前端估算或写死数字。 */
function actionCard({ title, text, meta, iconName, onOpen, countText, actionLabel }) {
  return el('section', { class: 'pg-focus home-action', 'data-action': iconName },
    el('p', { class: 'pg-kicker' }, t('home.action.kicker')),
    el('h2', {}, title),
    countText ? el('p', { class: 'home-action-count', 'data-no-i18n': '' }, countText) : null,
    el('p', {}, text),
    el('div', { class: 'pg-focus-actions' },
      button(actionLabel || t('home.action.open'), onOpen, '', iconName)),
    el('p', { class: 'pg-focus-meta' }, meta || ''));
}

function actionCards(personal, bank, reviewDue = null, vocabInfo = null) {
  const library = personal?.library || {};
  // M2：到期数字优先用服务端四档复习卡口径（review.due_now）；降级/旧后端回退旧字段。
  // 数字与「到期复习」卡片同源（镜像保证一致），避免首页出现两个互相矛盾的数字。
  const due = reviewDue?.available
    ? reviewDue.dueNow
    : (personal ? Number(personal.due_reviews || 0) : null);
  const pendingReview = personal ? Number(library.pending_review || 0) : null;
  const bankCountText = personal
    ? t('home.action.mybank.count', { count: number(library.questions || 0) })
    : null;
  const dueCountText = personal
    ? (due > 0
      ? (reviewDue?.available
        ? t('study.review.homeDue', { count: number(due) })
        : t('home.action.review.count', { count: number(due) }))
      : t('home.action.review.none'))
    : null;
  return el('section', { class: 'pg-focus-grid home-actions', 'aria-label': t('home.action.aria') },
    actionCard({
      title: t('home.action.upload.title'),
      text: t('home.action.upload.text'),
      meta: t('home.action.upload.meta'),
      iconName: 'scan',
      actionLabel: t('home.action.upload.label'),
      onOpen: () => go('upload'),
    }),
    actionCard({
      title: t('home.action.mybank.title'),
      text: t('home.action.mybank.text'),
      meta: personal
        ? (pendingReview > 0
          ? t('home.action.mybank.pending', { count: number(pendingReview) })
          : t('home.action.mybank.nonePending'))
        : t('common.unavailable'),
      iconName: 'tag',
      countText: bankCountText,
      actionLabel: t('home.action.mybank.label'),
      onOpen: () => go('mybank'),
    }),
    actionCard({
      title: t('home.action.review.title'),
      text: t('home.action.review.text'),
      meta: personal && due > 0 ? t('home.action.review.dueMeta', { count: number(due) }) : t('home.action.review.meta'),
      iconName: 'repeat',
      countText: dueCountText,
      actionLabel: t('home.action.review.label'),
      onOpen: () => go('wrongbook'),
    }),
    actionCard({
      title: t('home.action.vocab.title'),
      text: t('home.action.vocab.text'),
      meta: vocabInfo?.available
        ? t('home.action.vocab.meta', { count: number(vocabInfo.due_today || 0) })
        : t('common.unavailable'),
      iconName: 'tag',
      countText: vocabInfo?.available
        ? t('home.action.vocab.count', { count: number(vocabInfo.total || 0) })
        : null,
      actionLabel: t('home.action.vocab.label'),
      onOpen: () => go('vocab'),
    }));
}

/* 学习统计行：待复习 / 今日练习 / 首次正确率 / 当前最弱知识点（带真实样本数）。
 * M2 口径说明：这里的「待复习」仍按旧错题本到期字段展示（标签已写明「错题本到期」）；
 * 首页「到期复习」卡片与行动卡已切到服务端四档复习卡口径（review.due_now），两者由服务端
 * 同事务镜像保证一致，降级时同样回退到这里，因此不会自相矛盾。 */
function learningStats(personal) {
  if (!personal) {
    return el('div', { class: 'pg-inline-stats', 'aria-label': t('home.stat.aria') },
      inlineStat('—', t('home.stats.unavailable')));
  }
  const first = personal.first_attempt || {};
  const weak = (personal.weak_knowledge || [])[0];
  const today = personal.today || {};
  const dueStat = inlineStat(
    t('home.stats.dueValue', { count: number(personal.due_reviews || 0) }),
    t('home.stats.dueLabel'));
  // 旧口径说明（服务端 CALIBER.due）：鼠标悬停可核对与其他卡片的关系。
  if (personal.caliber?.due) dueStat.setAttribute('title', String(personal.caliber.due));
  return el('div', { class: 'pg-inline-stats', 'aria-label': t('home.stat.aria') },
    dueStat,
    inlineStat(
      t('home.stats.todayValue', { count: number(today.total || 0) }),
      t('home.stats.todayLabel', { practice: number(today.practice || 0), paper: number(today.paper || 0), classroom: number(today.classroom || 0) })),
    inlineStat(
      first.samples ? `${Math.round(Number(first.accuracy) || 0)}%` : '—',
      first.samples
        ? t('home.stats.firstAccuracyLabel', { samples: number(first.samples) })
        : t('home.stats.firstAccuracyNone')),
    inlineStat(
      weak ? weak.title : t('home.stats.weakNone'),
      weak
        ? t('home.stats.weakLabel', { count: number(weak.graded_attempts || 0), pct: Math.round(Number(weak.accuracy) || 0) })
        : t('home.stats.weakLabelEmpty')));
}

/* 题库全局口径：可练 / 可自动判分 / 待核验，来自只读题库与练习同一套门禁；
 * 平台精选可练数量：/api/insight/home 已下发 official 口径时优先使用，否则用
 * `/api/questions?source=official` 能力探测（未就绪时 featurePractice 为 null，不显示）。 */
function bankCaliberCard(bank, featuredPractice = null) {
  const available = Boolean(bank?.available);
  const featuredValue = bank?.official_practice_pool ?? bank?.featured_practice_pool ?? featuredPractice;
  const featuredKnown = featuredValue !== null && featuredValue !== undefined
    && Number.isFinite(Number(featuredValue));
  const card = el('section', { class: 'card home-bank-caliber', 'aria-labelledby': 'home-bank-title' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', { id: 'home-bank-title' }, t('home.bank.title')),
        el('p', { class: 'pg-card-sub' },
          available
            ? t('home.bank.sub', { version: bank.bank_version || t('home.bank.versionUnknown') })
            : (bank?.note || t('home.bank.unavailable')))),
      el('div', { class: 'row' },
        featuredKnown
          ? button(t('home.featured.goPractice'), () => go('practice', null, { source: 'official' }), 'ghost small', 'layers')
          : null,
        button(t('home.bank.goPractice'), () => go('practice'), 'ghost small', 'book'))));
  if (!available) return card;
  const gates = bank.gates || {};
  card.append(
    el('div', { class: 'pg-inline-stats', 'aria-label': t('home.bank.aria') },
      inlineStat(t('home.bank.totalValue', { count: number(bank.total_questions || 0) }), t('home.bank.totalLabel')),
      inlineStat(t('home.bank.practiceValue', { count: number(bank.practice_pool || 0) }), t('home.bank.practiceLabel')),
      inlineStat(t('home.bank.autoValue', { count: number(bank.auto_gradable || 0) }), t('home.bank.autoLabel')),
      inlineStat(t('home.bank.pendingValue', { count: number(bank.pending_verification || 0) }), t('home.bank.pendingLabel')),
      featuredKnown
        ? inlineStat(t('home.featured.value', { count: number(featuredValue) }), t('home.featured.practiceLabel'))
        : null),
    el('p', { class: 'pg-card-sub' },
      t('home.bank.caliberLine', {
        excluded: number(bank.excluded_by_gates || 0),
        figures: number(gates.missing_figure || 0),
        missingAnswer: number(gates.missing_answer || 0),
        isolated: number(gates.isolated || 0),
      })));
  return card;
}

function weakKnowledgeCard(personal) {
  const weak = personal?.weak_knowledge || [];
  const card = el('section', { class: 'pg-card', 'aria-labelledby': 'home-weak-title' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', { id: 'home-weak-title' }, t('home.weak.title')),
        el('p', { class: 'pg-card-sub' }, t('home.weak.sub'))),
      button(t('home.weak.all'), () => go('insight'), 'ghost small')));
  if (!personal) {
    card.append(el('div', { class: 'pg-empty' }, el('p', {}, t('common.unavailable'))));
    return card;
  }
  if (!weak.length) {
    card.append(el('div', { class: 'pg-empty' }, el('p', {}, personal.weak_note || t('home.weak.empty'))));
    return card;
  }
  card.append(el('div', { class: 'pg-due-list' }, ...weak.map(item => el('button', {
    type: 'button',
    class: 'pg-due-item',
    onClick: () => go('practice', null, { kp_id: String(item.kp_id) }),
  },
  el('span', { class: 'pg-due-subject', 'data-no-i18n': '' },
    [item.subject, t('home.weak.sample', { count: number(item.graded_attempts || 0) })].filter(Boolean).join(' · ')),
  el('span', { class: 'pg-due-stem', 'data-no-i18n': '' },
    `${item.title} · ${Math.round(Number(item.accuracy) || 0)}%`),
  icon('chevron')))));
  if (personal.weak_note) {
    card.append(el('p', { class: 'pg-card-sub' }, personal.weak_note));
  }
  return card;
}


// 按实际活动决定首屏主操作：未完成的待核验作答 → 到期错题 → 新账号 →
// 继续上次练习（题库题/个人题，直达原题）→ 继续上次整卷 → 常规继续。
function resolveNextStep(stats, dueItems, personal, reviewDue = null) {
  const attempts = stats.recent_attempts || [];
  const last = personal?.continue?.last_attempt || attempts[0] || null;
  // M2：到期数字优先取服务端四档复习卡（review.due_now），降级时回退旧错题到期字段，
  // 与「到期复习」卡片保持同一个数字；不额外展示第二份口径。
  const dueNow = reviewDue?.available
    ? reviewDue.dueNow
    : Number(personal?.due_reviews ?? stats.due_count);
  const pending = (last && (last.result === 'pending' || last.result === 'ungraded') ? last : null)
    || attempts.find(item => item.result === 'pending' || item.result === 'ungraded');
  if (pending) {
    return {
      title: t('home.next.pending.title'),
      text: t('home.next.pending.text', { date: dateText(pending.created_at), result: resultLabel(pending.result) }),
      label: t('home.next.continue'),
      icon: 'book',
      run: () => go('practice', pending.qid),
      meta: t('home.next.pending.meta', { date: dateText(pending.created_at) }),
    };
  }
  if (!Number(stats.practiced) && !Number(stats.attempts)) {
    return {
      title: t('home.next.new.title'),
      text: t('home.next.new.text'),
      label: t('home.next.new.label'),
      icon: 'book',
      run: () => go('practice'),
      meta: t('home.next.new.meta', { count: number(stats.total_questions) }),
    };
  }
  if (dueNow > 0 || dueItems.length) {
    return {
      title: t('home.next.due.title'),
      text: t('home.next.due.text', { count: number(dueNow) }),
      label: t('home.next.due.label'),
      icon: 'repeat',
      run: () => go('wrongbook'),
      meta: t('home.next.due.meta'),
    };
  }
  // 「继续上次练习」直达：优先比最近一次作答与最近一次整卷练习哪个更新。
  const lastPaper = personal?.continue?.last_paper || null;
  if (lastPaper && timeMs(lastPaper.finished_at) > timeMs(last?.created_at)) {
    return {
      title: t('home.next.paper.title', { title: trimText(lastPaper.title || t('home.ai.unnamedPaper'), 18) }),
      text: t('home.next.paper.text', { score: number(lastPaper.score), max: number(lastPaper.max_score) }),
      label: t('home.next.paper.label'),
      icon: 'printer',
      run: () => go('paperlab', null, { tab: 'papers', paper: lastPaper.paper_id }),
      meta: t('home.next.paper.meta', { time: dateText(lastPaper.finished_at) }),
    };
  }
  if (last?.qid) {
    return {
      title: t('home.next.last.title'),
      text: t('home.next.last.text', { result: resultLabel(last.result), date: dateText(last.created_at) }),
      label: t('home.next.last.label'),
      icon: 'book',
      run: () => go('practice', last.qid),
      meta: t('home.next.last.meta'),
    };
  }
  return {
    title: t('home.next.regular.title'),
    text: t('home.next.regular.text', { count: number(stats.practiced) }),
    label: t('home.next.continue'),
    icon: 'book',
    run: () => go('practice'),
    meta: t('home.next.regular.meta'),
  };
}

function dueQuestionButton(item) {
  const q = item.question || item;
  return el('button', {
    type: 'button',
    class: 'pg-due-item',
    onClick: () => go('practice', q.qid || item.qid),
  },
    el('span', { class: 'pg-due-subject' }, [q.subject, q.number_label ? t('question.originalNumber', { label: q.number_label }) : null].filter(Boolean).join(' · ') || t('home.dueItemFallback')),
    el('span', { class: 'pg-due-stem' }, readableSummary(q, 60) || t('home.openQuestion')),
    icon('chevron'));
}

function recentAttemptButton(attempt) {
  const answer = String(attempt.answer_text || '').trim();
  return el('button', {
    type: 'button',
    class: 'pg-recent-item',
    onClick: () => go('practice', attempt.qid),
    'aria-label': t('home.openAttemptAria', { result: resultLabel(attempt.result) }),
  },
    el('span', { class: 'pg-recent-main' },
      el('strong', {}, answer ? t('home.answeredText', { text: answer.slice(0, 24) }) : t('home.imageAnswer')),
      el('small', {}, `${dateText(attempt.created_at)} · ${resultLabel(attempt.result)}`)),
    resultBadge(attempt.result));
}

function noteLink(item) {
  return el('button', {
    type: 'button',
    class: 'pg-note-link',
    onClick: () => { state.selectedNote = item.id; go('notes'); },
  },
    el('span', { class: 'pg-note-link-main' },
      el('strong', {}, item.title),
      el('small', {}, `${item.origin === 'official' ? t('home.noteOfficial') : t('home.notePrivate')} · ${dateText(item.updated_at || item.created_at)}`)),
    icon('chevron'));
}

function shortcutCard(title, text, name, run) {
  return el('button', { type: 'button', class: 'pg-shortcut', onClick: run },
    icon(name),
    el('strong', {}, title),
    el('small', {}, text));
}

/* =========================================================================
 * AI 工作台：把整合进来的课堂/试卷工坊/我的题库/错题巩固卷/助手/任务中心
 * 汇总成 6 个真实计数入口 + 最近动态。数据全部来自各模块真实只读 API，
 * 单个接口失败只降级该卡片（显示「暂不可用」并保留入口），不伪造计数。
 * ========================================================================= */

// 枚举标签：渲染时按当前语言取 t()（模块加载时读会固定成首屏语言）。
function enumLabel(prefix, value) {
  const raw = String(value || '');
  if (!raw) return '';
  const key = `${prefix}.${raw}`;
  const translated = t(key);
  return translated === key ? raw : translated;
}
const paperKindLabel = kind => enumLabel('home.paperKind', kind);
const importStatusLabel = status => enumLabel('home.importStatus', status);
const conversationStatusLabel = status => enumLabel('home.conversationStatus', status);
const RUNNING_JOB_STATUSES = new Set(['queued', 'running', 'cancel_requested']);

function trimText(value, limit) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (!limit || text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

function timeMs(value) {
  const parsed = Date.parse(String(value || ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function settledEntry(result) {
  return result.status === 'fulfilled'
    ? { ok: true, value: result.value }
    : { ok: false, error: result.reason };
}

function listItems(entry) {
  if (!entry.ok) return [];
  const items = entry.value?.items;
  return Array.isArray(items) ? items : [];
}

function listTotal(entry) {
  if (!entry.ok) return null;
  const total = entry.value?.total;
  if (Number.isFinite(Number(total))) return Number(total);
  return listItems(entry).length;
}

function degradedCard(reason) {
  return {
    degraded: true,
    countText: t('common.unavailable'),
    meta: reason || t('home.ai.degradedReason'),
  };
}

function runningJobCount(jobsEntry) {
  if (!jobsEntry.ok) return null;
  return listItems(jobsEntry).filter(job => RUNNING_JOB_STATUSES.has(String(job.status || ''))).length;
}

const AI_CARD_SPECS = [
  {
    key: 'classroom',
    get title() { return t('home.ai.classroom.title'); },
    icon: 'presentation',
    get desc() { return t('home.ai.classroom.desc'); },
    build({ sources }) {
      const entry = sources.classroom;
      if (!entry.ok) return degradedCard(t('home.ai.classroom.degraded'));
      const latest = listItems(entry)[0];
      const total = listTotal(entry) || 0;
      return {
        countText: t('home.ai.classroom.count', { count: number(total) }),
        meta: latest
          ? t('home.ai.latest', { name: trimText(latest.title || latest.topic || t('home.ai.unnamedCourse'), 16), time: dateText(latest.updated_at) })
          : t('home.ai.classroom.empty'),
      };
    },
    open() { go('classroom'); },
  },
  {
    key: 'paperlab',
    get title() { return t('home.ai.paperlab.title'); },
    icon: 'printer',
    get desc() { return t('home.ai.paperlab.desc'); },
    build({ sources }) {
      const papers = sources.papers;
      const imports = sources.imports;
      if (!papers.ok && !imports.ok) return degradedCard(t('home.ai.paperlab.degraded'));
      const parts = [];
      const missing = [];
      const paperTotal = listTotal(papers);
      const importTotal = listTotal(imports);
      if (paperTotal !== null) parts.push(t('home.ai.papers.count', { count: number(paperTotal) }));
      else missing.push(t('home.ai.field.papers'));
      if (importTotal !== null) parts.push(t('home.ai.imports.count', { count: number(importTotal) }));
      else missing.push(t('home.ai.field.imports'));
      // 部分接口失败时仍展示可用的真实计数，但明确标注哪一部分「暂不可用」。
      const countText = parts.length
        ? (missing.length ? t('home.ai.partialUnavailable', { parts: parts.join(' · '), missing: missing.join('/') }) : parts.join(' · '))
        : t('common.unavailable');
      const latestImport = listItems(imports)[0];
      const latestPaper = listItems(papers)[0];
      const meta = latestImport
        ? t('home.ai.latestImport', { name: trimText(latestImport.title || t('home.ai.unnamedImport'), 14), time: dateText(latestImport.updated_at) })
        : latestPaper
          ? t('home.ai.latestPaper', { name: trimText(latestPaper.title || t('home.ai.unnamedPaper'), 14), time: dateText(latestPaper.updated_at) })
          : t('home.ai.paperlab.empty');
      return {
        countText,
        meta,
        degraded: !papers.ok || !imports.ok,
      };
    },
    open() { go('paperlab'); },
  },
  {
    key: 'mybank',
    get title() { return t('home.ai.mybank.title'); },
    icon: 'tag',
    get desc() { return t('home.ai.mybank.desc'); },
    build({ sources }) {
      const total = sources.bankTotal;
      const pending = sources.bankPending;
      if (!total.ok) return degradedCard(t('home.ai.mybank.degraded'));
      const totalCount = listTotal(total) || 0;
      const pendingText = pending.ok
        ? t('home.ai.mybank.pending', { count: number(listTotal(pending) || 0) })
        : t('home.ai.mybank.pendingUnavailable');
      return {
        countText: t('common.countQuestions', { count: number(totalCount) }),
        meta: t('home.ai.mybank.meta', { pending: pendingText }),
        degraded: !pending.ok,
      };
    },
    open() { go('mybank'); },
  },
  {
    key: 'revision',
    get title() { return t('home.ai.revision.title'); },
    icon: 'repeat',
    get desc() { return t('home.ai.revision.desc'); },
    build({ stats }) {
      const wrongCount = Number(stats?.wrong_count || 0);
      const dueCount = Number(stats?.due_count || 0);
      return {
        countText: t('home.ai.revision.count', { count: number(wrongCount) }),
        meta: dueCount ? t('home.ai.revision.due', { count: number(dueCount) }) : t('home.ai.revision.emptyMeta'),
      };
    },
    open() { go('paperlab', null, { tab: 'revision' }); },
  },
  {
    key: 'assistant',
    get title() { return t('home.ai.assistant.title'); },
    icon: 'message',
    get desc() { return t('home.ai.assistant.desc'); },
    build({ sources }) {
      const entry = sources.assistant;
      if (!entry.ok) return degradedCard(t('home.ai.assistant.degraded'));
      const latest = listItems(entry)[0];
      const total = listTotal(entry) || 0;
      return {
        countText: t('home.ai.assistant.count', { count: number(total) }),
        meta: latest
          ? t('home.ai.latest', { name: trimText(latest.title || t('home.ai.assistant.newConversation'), 16), time: dateText(latest.updated_at) })
          : t('home.ai.assistant.empty'),
      };
    },
    open({ sources }) {
      const latest = listItems(sources.assistant)[0];
      if (latest?.id) state.assistantConversationId = latest.id;
      go('assistant');
    },
  },
  {
    key: 'tasks',
    get title() { return t('home.ai.tasks.title'); },
    icon: 'history',
    get desc() { return t('home.ai.tasks.desc'); },
    build({ sources }) {
      const jobs = sources.jobs;
      if (!jobs.ok) return degradedCard(t('home.ai.tasks.degraded'));
      const items = listItems(jobs);
      const running = items.filter(job => RUNNING_JOB_STATUSES.has(String(job.status || ''))).length;
      const latest = items[0];
      return {
        countText: running ? t('home.ai.tasks.runningCount', { count: number(running) }) : t('home.ai.tasks.totalCount', { count: number(items.length) }),
        meta: latest
          ? t('home.ai.tasks.latest', { task: taskNames[latest.task] || latest.task || t('home.ai.tasks.aiTask'), status: statusNames[latest.status] || latest.status, time: dateText(latest.updated_at || latest.created_at) })
          : t('home.ai.tasks.empty'),
        active: running > 0,
      };
    },
    open() { go('tasks'); },
  },
];

function aiCardNode(spec, info, sources) {
  const countText = info.countText || '—';
  const node = el('button', {
    type: 'button',
    class: `home-ai-card${info.degraded ? ' is-degraded' : ''}${info.active ? ' is-active' : ''}`,
    'data-card': spec.key,
    'aria-label': t('home.ai.cardAria', { title: spec.title, count: countText, desc: spec.desc }),
    onClick: () => spec.open({ sources }),
  },
    el('span', { class: 'home-ai-card-head' },
      el('span', { class: 'home-ai-icon', 'aria-hidden': 'true' }, icon(spec.icon)),
      el('span', { class: 'home-ai-title' }, spec.title),
      el('span', { class: 'home-ai-arrow', 'aria-hidden': 'true' }, icon('chevron'))),
    el('span', { class: 'home-ai-count', 'data-count': countText }, countText),
    el('span', { class: 'home-ai-desc' }, spec.desc),
    el('span', { class: 'home-ai-meta' }, info.meta || ''));
  return node;
}

function collectActivities(sources) {
  const items = [];
  if (sources.classroom.ok) {
    for (const course of listItems(sources.classroom).slice(0, 4)) {
      items.push({
        type: 'course',
        icon: 'presentation',
        title: course.title || course.topic || t('home.activity.course'),
        desc: [t('nav.classroom'), course.subject, course.scene_count ? t('home.activity.scenes', { count: course.scene_count }) : null]
          .filter(Boolean).join(' · '),
        time: course.updated_at || course.created_at,
        run: () => go('classroom', null, { course: course.id }),
      });
    }
  }
  if (sources.papers.ok) {
    for (const paper of listItems(sources.papers).slice(0, 3)) {
      items.push({
        type: 'paper',
        icon: 'printer',
        title: paper.title || t('home.ai.unnamedPaper'),
        desc: [t('nav.paperlab'), paperKindLabel(paper.kind), paper.item_count ? t('home.activity.items', { count: paper.item_count }) : null]
          .filter(Boolean).join(' · '),
        time: paper.updated_at || paper.created_at,
        run: () => go('paperlab', null, { tab: 'papers', paper: paper.id }),
      });
    }
  }
  if (sources.imports.ok) {
    for (const importRow of listItems(sources.imports).slice(0, 3)) {
      items.push({
        type: 'import',
        icon: 'upload',
        title: importRow.title || t('home.activity.import'),
        desc: [t('home.activity.importLabel'), importStatusLabel(importRow.status)]
          .filter(Boolean).join(' · '),
        time: importRow.updated_at || importRow.created_at,
        run: () => go('paperlab', null, { import: importRow.id }),
      });
    }
  }
  if (sources.jobs.ok) {
    for (const job of listItems(sources.jobs).slice(0, 4)) {
      const label = taskNames[job.task] || job.task || t('home.ai.tasks.aiTask');
      const status = statusNames[job.status] || job.status || '';
      items.push({
        type: 'job',
        icon: 'history',
        title: label,
        desc: [t('home.activity.jobLabel'), status, job.request?.model].filter(Boolean).join(' · '),
        time: job.updated_at || job.created_at,
        run: () => {
          state.tasksTaskFilter = job.task || '';
          go('tasks', null, job.task ? { task: job.task } : null);
        },
      });
    }
  }
  if (sources.assistant.ok) {
    for (const conversation of listItems(sources.assistant).slice(0, 3)) {
      items.push({
        type: 'assistant',
        icon: 'message',
        title: conversation.title || t('home.activity.assistantConv'),
        desc: [t('nav.assistant'), conversationStatusLabel(conversation.status),
          conversation.message_count ? t('home.activity.messages', { count: conversation.message_count }) : null]
          .filter(Boolean).join(' · '),
        time: conversation.updated_at,
        run: () => {
          state.assistantConversationId = conversation.id;
          go('assistant');
        },
      });
    }
  }
  if (sources.docs.ok) {
    for (const doc of listItems(sources.docs).slice(0, 2)) {
      items.push({
        type: 'document',
        icon: 'file',
        title: doc.title || t('home.activity.doc'),
        desc: [t('home.activity.library'), doc.pages ? t('common.countPages', { count: doc.pages }) : null].filter(Boolean).join(' · '),
        time: doc.created_at,
        run: () => go('notes'),
      });
    }
  }
  return items
    .filter(item => item.title)
    .sort((a, b) => timeMs(b.time) - timeMs(a.time))
    .slice(0, 6);
}

function activityNode(item) {
  return el('button', {
    type: 'button',
    class: `home-activity-item home-activity-${item.type}`,
    'aria-label': t('home.activity.aria', { title: item.title }),
    onClick: item.run,
  },
    el('span', { class: 'home-activity-icon', 'aria-hidden': 'true' }, icon(item.icon)),
    el('span', { class: 'home-activity-main' },
      el('strong', {}, trimText(item.title, 40)),
      el('small', {}, item.desc || '')),
    el('span', { class: 'home-activity-time' }, item.time ? dateText(item.time) : ''));
}

function renderSummary(summaryHost, sources) {
  const chips = [];
  const running = runningJobCount(sources.jobs);
  if (running !== null) {
    chips.push({
      text: running > 0 ? t('home.summary.running', { count: number(running) }) : t('home.summary.noneRunning'),
      tone: running > 0 ? 'amber' : '',
    });
  }
  if (sources.insight.ok) {
    const aiJobs = sources.insight.value?.summary?.ai_jobs;
    chips.push({ text: t('home.summary.jobs90', { count: number(aiJobs || 0) }), tone: '' });
  }
  if (sources.docs.ok) {
    chips.push({ text: t('home.summary.docs', { count: number(listItems(sources.docs).length) }), tone: '' });
  }
  if (sources.assistant.ok) {
    chips.push({ text: t('home.summary.assistant', { count: number(listTotal(sources.assistant) || 0) }), tone: '' });
  }
  summaryHost.replaceChildren();
  for (const chip of chips) {
    summaryHost.append(el('span', { class: `home-ai-chip${chip.tone ? ` ${chip.tone}` : ''}` }, chip.text));
  }
}

function renderStatus(statusHost, sources, retry) {
  const failures = [];
  const labels = {
    classroom: t('home.status.classroom'), papers: t('home.status.papers'), imports: t('home.status.imports'), bankTotal: t('home.status.bankTotal'),
    bankPending: t('home.status.bankPending'), assistant: t('home.status.assistant'), jobs: t('home.status.jobs'), insight: t('home.status.insight'), docs: t('home.status.docs'),
  };
  for (const [key, entry] of Object.entries(sources)) {
    if (entry && entry.ok === false) failures.push(labels[key] || key);
  }
  statusHost.replaceChildren();
  if (failures.length) {
    statusHost.append(
      el('span', { class: 'home-ai-status-text warning' },
        t('home.status.failed', { items: failures.join(t('common.listSeparator')) })),
      button(t('home.status.retry'), retry, 'secondary small', 'repeat'));
    return;
  }
  statusHost.append(
    el('span', { class: 'home-ai-status-text' },
      t('home.status.updated', { time: dateText(new Date().toISOString()) })));
}

function skeletonNode(className, lines) {
  const node = el('div', { class: className });
  for (let index = 0; index < lines; index += 1) node.append(el('span', { class: 'home-skeleton-line' }));
  return node;
}

function createWorkbench({ version, stats }) {
  const grid = el('div', { class: 'home-ai-grid', 'aria-busy': 'true' });
  const activityHost = el('div', { class: 'home-activity-list' });
  const summaryHost = el('div', { class: 'home-ai-summary' });
  const statusHost = el('div', { class: 'home-ai-status' });
  const retryBtn = { node: null };
  const titleEl = el('h2', { id: 'home-ai-title' }, t('home.ai.title'));
  const subEl = el('p', { class: 'pg-card-sub' }, t('home.ai.sub'));
  const head = el('div', { class: 'pg-card-head home-ai-head' },
    el('div', {}, titleEl, subEl),
    summaryHost);
  const body = el('div', { class: 'home-ai-body' },
    el('div', { class: 'home-ai-main' },
      el('h3', { class: 'home-ai-subtitle' }, t('home.ai.quickEntries')),
      grid),
    el('div', { class: 'home-activity' },
      el('h3', { class: 'home-ai-subtitle' }, t('home.ai.recent')),
      activityHost));
  const section = el('section', { class: 'card home-ai', 'aria-labelledby': 'home-ai-title' },
    head, body, statusHost);

  let loading = false;

  function showSkeleton() {
    grid.setAttribute('aria-busy', 'true');
    grid.replaceChildren(...AI_CARD_SPECS.map(() => skeletonNode('home-ai-card is-loading', 4)));
    activityHost.replaceChildren(...[0, 1, 2, 3].map(() => skeletonNode('home-activity-item is-loading', 2)));
    summaryHost.replaceChildren(el('span', { class: 'home-ai-chip' }, t('common.loadingData')));
    statusHost.replaceChildren(el('span', { class: 'home-ai-status-text' }, t('home.loadingParallel')));
  }

  async function load() {
    if (loading) return;
    loading = true;
    showSkeleton();
    const results = await Promise.allSettled([
      api('/classroom/courses?page_size=8'),
      api('/paperlab/papers?page_size=8'),
      api('/paperlab/questions?page_size=1'),
      api('/paperlab/questions?verify_status=pending&page_size=1'),
      api('/paperlab/imports?page_size=8'),
      api('/assistant/conversations?page_size=8'),
      api('/ai/jobs'),
      api('/insight/overview'),
      api('/documents'),
    ]);
    loading = false;
    if (!current(version) || !section.isConnected) return;
    const [
      classroom, papers, bankTotal, bankPending, imports,
      assistant, jobs, insight, docs,
    ] = results.map(settledEntry);
    const sources = {
      classroom, papers, bankTotal, bankPending, imports, assistant, jobs, insight, docs,
    };
    grid.setAttribute('aria-busy', 'false');
    grid.replaceChildren(...AI_CARD_SPECS.map(spec => {
      let info;
      try {
        info = spec.build({ sources, stats });
      } catch {
        info = degradedCard(t('home.ai.degradedReason'));
      }
      return aiCardNode(spec, info, sources);
    }));
    const activities = collectActivities(sources);
    if (activities.length) {
      activityHost.replaceChildren(...activities.map(activityNode));
    } else {
      activityHost.replaceChildren(el('div', { class: 'home-activity-empty' },
        el('p', {}, sources.jobs.ok || sources.classroom.ok
          ? t('home.activity.empty')
          : t('home.activity.unavailable')),
        el('div', { class: 'pg-actions' },
          button(t('home.activity.goClassroom'), () => go('classroom'), 'soft small', 'presentation'),
          button(t('home.activity.goPaperlab'), () => go('paperlab'), 'secondary small', 'printer'))));
    }
    renderSummary(summaryHost, sources);
    renderStatus(statusHost, sources, load);
  }

  return { node: section, load };
}

export async function renderHome(container, version) {
  const [stats, reviews, notes, homeStats, featuredPractice] = await Promise.all([
    api('/stats'),
    api('/reviews'),
    api('/notes'),
    // 首页统计口径（题库全局 + 个人首次正确率/今日练习/薄弱知识点）。
    // 接口失败时只降级这一块，主操作与既有 AI 工作台入口不丢失。
    api('/insight/home', { latest: true, latestKey: 'home-stats' }).catch(() => null),
    // 题库口径卡片的「平台精选」可练数量：能力探测失败/未就绪时为 null（不显示该计数）。
    detectFeaturedPractice(),
  ]);
  if (!current(version)) return;
  const personal = homeStats?.personal || null;
  const bank = homeStats?.bank || null;
  // M2：首页到期数字快照——优先 /api/insight/home 顶层 review 块（服务端四档复习卡唯一到期值），
  // 降级/旧后端回退旧错题到期字段（personal.due_reviews 同源镜像），不在前端估算或写死数字。
  const reviewDue = reviewDueSnapshot(homeStats, personal, stats);
  // 精选学情口径卡：字段未就绪（旧后端）时返回 null，不占据首屏、不显示 0 假数字。
  const officialCard = officialCaliberCard(stats, homeStats);
  const dueItems = reviews.items || [];
  const next = resolveNextStep(stats, dueItems, personal, reviewDue);
  const recent = (stats.recent_attempts || []).slice(0, 4);
  const greet = new Date().getHours() < 12 ? t('home.greet.morning') : new Date().getHours() < 18 ? t('home.greet.afternoon') : t('home.greet.evening');
  const dueCount = reviewDue.count ?? dueItems.length;
  const hasDue = dueItems.length > 0 || Number(dueCount) > 0;
  // 口径说明行（不新增第二份数字）：可用时展示「服务端排程」徽标 + fsrs-reference v5.0-local +
  // 今日到期数；降级时展示服务端 note（含异常类型）或固定文案；旧后端缺 review 块时不显示。
  const dueCaliberNode = reviewDue.available
    ? el('p', { class: 'pg-focus-meta home-due-caliber', 'data-review-source': 'review-cards' },
      badge(t('study.review.serverSchedule'), 'teal'),
      el('span', { title: String(reviewDue.review?.caliber || '') },
        t('study.review.homeDueCaliber', {
          algorithm: reviewDue.algorithmText,
          count: number(reviewDue.dueToday ?? dueCount),
        })))
    : (reviewDue.degraded && personal
      ? el('p', { class: 'pg-focus-meta home-due-caliber is-degraded', 'data-review-source': 'legacy' },
        reviewDue.degradeNote || t('study.review.homeUnavailable'))
      : null);

  const focusPrimary = el('section', { class: 'pg-focus pg-focus--primary', 'aria-labelledby': 'home-next-title' },
    el('p', { class: 'pg-kicker' }, t('home.next.kicker')),
    el('h2', { id: 'home-next-title' }, next.title),
    el('p', {}, next.text),
    el('p', { class: 'pg-focus-meta' }, next.meta),
    el('div', { class: 'pg-focus-actions' },
      button(next.label, next.run, '', next.icon)));

  // 保留旧卡片位置与文案结构，只换数据源（review 块优先、旧字段兜底）。
  const dueCard = el('section', {
    class: 'pg-focus',
    'aria-labelledby': 'home-due-title',
    // 数据来源标记：review-cards（服务端四档卡）| legacy（旧错题到期兜底）| unknown（旧后端无该块）。
    'data-review-source': reviewDue.available ? 'review-cards' : (reviewDue.degraded ? 'legacy' : 'unknown'),
  },
    el('p', { class: 'pg-kicker' }, t('home.due.kicker')),
    el('h2', { id: 'home-due-title' }, hasDue ? t('home.due.count', { count: number(dueCount) }) : t('home.due.none')),
    dueCaliberNode,
    dueItems.length
      ? el('div', { class: 'pg-due-list' }, ...dueItems.slice(0, 3).map(dueQuestionButton))
      : el('p', {}, t('home.due.emptyText')),
    el('div', { class: 'pg-focus-actions' },
      hasDue
        ? button(
          // 服务端口径可用时用「去复习」；降级时沿用旧文案，避免暗示四档排程正常。
          reviewDue.available ? t('study.review.goReview') : t('home.due.startReview'),
          () => go('wrongbook'), 'secondary small', 'repeat')
        : button(t('home.due.goPractice'), () => go('practice'), 'secondary small', 'book')));

  const pendingGrading = Number(personal?.pending_grading?.questions || 0);
  const recentMeta = stats.graded_attempts
    ? t('home.recent.gradedStat', { count: number(stats.graded_attempts), percent: Math.round(Number(stats.accuracy)) })
    : t('home.recent.noneGraded');
  const recentCard = el('section', { class: 'pg-focus', 'aria-labelledby': 'home-recent-title' },
    el('p', { class: 'pg-kicker' }, t('home.recent.kicker')),
    el('h2', { id: 'home-recent-title' }, recent.length ? t('home.recent.title') : t('home.recent.none')),
    recent.length
      ? el('div', { class: 'pg-recent' }, ...recent.map(recentAttemptButton))
      : el('p', {}, t('home.recent.emptyText')),
    el('p', { class: 'pg-focus-meta' },
      pendingGrading > 0 ? `${recentMeta} · ${t('home.recent.pendingMeta', { count: number(pendingGrading) })}` : recentMeta));

  const notesCard = el('section', { class: 'pg-card', 'aria-labelledby': 'home-notes-title' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', { id: 'home-notes-title' }, t('home.notes.title')),
        el('p', { class: 'pg-card-sub' }, t('home.notes.sub'))),
      button(t('home.notes.all'), () => go('notes'), 'ghost small')));
  if (notes.items?.length) {
    notesCard.append(el('div', { class: 'pg-card-list' }, ...notes.items.slice(0, 3).map(noteLink)));
  } else {
    notesCard.append(el('div', { class: 'pg-empty' },
      el('p', {}, t('home.notes.empty')),
      el('div', { class: 'pg-actions' }, button(t('home.notes.organize'), () => go('notes'), 'soft small', 'notes'))));
  }

  const shortcutsCard = el('section', { class: 'pg-card', 'aria-labelledby': 'home-shortcuts-title' },
    el('h2', { id: 'home-shortcuts-title', style: 'margin:0 0 14px;font-size:19px' }, t('home.shortcuts.title')),
    el('div', { class: 'pg-shortcut-row' },
      shortcutCard(t('home.shortcuts.upload.title'), t('home.shortcuts.upload.desc'), 'scan', () => go('upload')),
      shortcutCard(t('home.shortcuts.wrongbook.title'), t('home.shortcuts.wrongbook.desc'), 'repeat', () => go('wrongbook')),
      shortcutCard(t('home.shortcuts.notes.title'), t('home.shortcuts.notes.desc'), 'notes', () => go('notes'))));

  const workbench = createWorkbench({ version, stats });

  // replaceChildren 不能直接接收 null（会被转成文本节点「null」）：精选口径卡未就绪时过滤掉。
  const pageSections = [
    heading('LEARNING OVERVIEW', t('home.greeting', { greet, name: state.user.username }),
      t('home.subheading')),
    actionCards(personal, bank, reviewDue, homeStats?.vocab || null),
    learningStats(personal),
    el('section', { class: 'pg-focus-grid', 'aria-label': t('home.next.aria') }, focusPrimary, dueCard, recentCard),
    workbench.node,
    el('div', { class: 'pg-grid-2' }, weakKnowledgeCard(personal), notesCard),
    bankCaliberCard(bank, featuredPractice),
    officialCard,
    shortcutsCard,
  ].filter(Boolean);
  container.replaceChildren(...pageSections);

  if (stats.subject_progress?.length) {
    const rows = stats.subject_progress
      .filter(item => Number(item.total))
      .map(item => {
        const total = Number(item.total) || 0;
        const done = Number(item.practiced || item.attempted || 0);
        return el('div', { class: 'pg-subject-row' },
          el('div', { class: 'row' }, el('strong', { 'data-no-i18n': '' }, item.subject), el('span', { class: 'pg-card-sub' }, `${number(done)} / ${number(total)}`)),
          el('div', { class: 'pg-progress-track' }, el('span', { style: `width:${total ? Math.min(100, done / total * 100) : 0}%` })));
      });
    if (rows.length) {
      container.append(el('section', { class: 'card pg-fold-card' },
        el('details', { class: 'pg-advanced' },
          el('summary', {}, t('home.subjectProgress.summary', { count: rows.length })),
          el('div', { class: 'pg-advanced-body' }, el('div', { class: 'pg-subject-progress' }, ...rows)))));
    }
  }

  container.append(el('div', { class: 'footer-note' },
    t('home.footer')));

  // 骨架先出现在页面上，再并行读取各模块真实接口；单接口失败只降级对应卡片。
  workbench.load();
}
