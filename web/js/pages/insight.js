/* 学情分析页：实时聚合的作答/错题/掌握度报告 + 原生 SVG 图表 + AI 文字建议任务。
 *
 * 数据全部来自 `/api/insight/report`（后端实时聚合，不建表）。图表不引入第三方库：
 * 学科正确率、活动柱状、错题类型为横向条，错题原因为 SVG 环形图。
 * AI 建议走 `POST /api/insight/narrative` 创建 insight-report 任务，用 core/jobs.js
 * 的 mountJob 展示进度/取消，完成后用 markdown() 渲染建议并显式标注「AI 生成」。
 */
import { api, ensureCatalog } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, icon, heading, button, select, field, badge, notice, loading, busy, toast,
  number, dateText, query,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { t } from '../core/i18n.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const DEFAULT_DAYS = 90;
// 知识点样本门槛：与后端 app/home_stats.py 的 MIN_WEAK_SAMPLE 保持一致（样本不足只报真实样本数）。
const MIN_WEAK_SAMPLE = 3;
// 选项文案在渲染时取 t()（模块加载时读取会固定成首屏语言）。
const dayOptions = () => [
  { value: '30', label: t('insight.day.30') },
  { value: '90', label: t('insight.day.90') },
  { value: '180', label: t('insight.day.180') },
];
// 后端 mastery_counts 的键是中文（数据契约，不能改）；展示时映射到语言包。
const MASTERY_LEVELS = ['未掌握', '初步掌握', '比较掌握', '熟练掌握'];
const MASTERY_LABEL_KEYS = {
  未掌握: 'insight.mastery.none', 初步掌握: 'insight.mastery.basic',
  比较掌握: 'insight.mastery.good', 熟练掌握: 'insight.mastery.mastered',
};
const MASTERY_SHORT_KEYS = {
  未掌握: 'insight.mastery.short.none', 初步掌握: 'insight.mastery.short.basic',
  比较掌握: 'insight.mastery.short.good', 熟练掌握: 'insight.mastery.short.mastered',
};
function masteryLabel(level) {
  const key = MASTERY_LABEL_KEYS[level];
  return key ? t(key) : String(level ?? '');
}
function masteryShort(level) {
  const key = MASTERY_SHORT_KEYS[level];
  return key ? t(key) : String(level ?? '');
}
const REASON_COLORS = ['#286552', '#904537', '#7d5a24', '#3d5b8e', '#6b7f86', '#8db89a', '#c9a27a'];

let days = DEFAULT_DAYS;
let narrativeJobId = null;
let lastAdvice = null;
// M3 掌握路径：跨重渲染保留已勾选的薄弱知识点（切换学科/时间范围时裁剪到可见项）。
const masteryKpSelection = new Set();
// 一次生成掌握路径最多使用的知识点数（交付规格 ≤8）。
const MAX_PATH_KPS = 8;
// 学情报告每次重渲染递增；旧控制器的异步响应不得再改页面（避免筛选快速切换时串数据）。
let insightPathRenderSeq = 0;

onSessionReset(() => {
  narrativeJobId = null;
  lastAdvice = null;
  days = DEFAULT_DAYS;
  masteryKpSelection.clear();
});

/* ------------------------------------------------------------------------- *
 * 原生 SVG 工具（与 pages/admission.js 的写法一致，不引入图表库）
 * ------------------------------------------------------------------------- */

function svgNode(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null && value !== false) {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function svgText(x, y, text, attrs = {}) {
  return svgNode('text', { x, y, ...attrs }, [document.createTextNode(String(text))]);
}

function formatPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return `${Number.isInteger(num) ? num : num.toFixed(1)}%`;
}

function svgTitle(text) {
  const node = document.createElementNS(SVG_NS, 'title');
  node.textContent = text;
  return node;
}

/** 横向条形图：学科正确率（0–100）或错题题型计数（maxValue 自适应）。 */
function horizontalBars(rows, {
  ariaLabel = t('insight.chart.hbarAria'), maxValue = null, unit = '%', toneOf = () => '', formatValue = null,
} = {}) {
  const items = (rows || []).filter(row => row && row.label !== undefined);
  if (!items.length) return null;
  const width = 640;
  const rowHeight = 32;
  const pad = { top: 6, right: 62, bottom: 6, left: 118 };
  const innerWidth = width - pad.left - pad.right;
  const height = pad.top + items.length * rowHeight + pad.bottom;
  const max = Math.max(1, Number(maxValue) || Math.max(...items.map(row => Number(row.value) || 0)));
  const bars = [];
  items.forEach((row, index) => {
    const value = Math.max(0, Number(row.value) || 0);
    const y = pad.top + index * rowHeight;
    const barWidth = Math.max(value > 0 ? 3 : 0, (value / max) * innerWidth);
    const tone = toneOf(row) || '';
    bars.push(svgText(pad.left - 10, y + 16, row.label, { 'text-anchor': 'end', class: 'ins-chart-label' }));
    bars.push(svgNode('rect', {
      x: pad.left, y: y + 7, width: innerWidth, height: 12, rx: 6,
      fill: '#edf1ee', stroke: 'none',
    }));
    if (barWidth > 0) {
      bars.push(svgNode('rect', {
        x: pad.left, y: y + 7, width: barWidth, height: 12, rx: 6,
        class: `ins-chart-bar ${tone}`.trim(),
      }, [svgTitle(t('insight.chart.barTitle', { label: row.label, value: formatValue ? formatValue(value) : `${value}${unit}` }))]));
    }
    const label = formatValue ? formatValue(value) : `${Math.round(value)}${unit}`;
    bars.push(svgText(pad.left + innerWidth + 8, y + 16, label, { class: 'ins-chart-value' }));
  });
  return svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'ins-chart',
    role: 'img',
    'aria-label': ariaLabel,
  }, bars);
}

/** 近 N 天活动柱状图：每根柱 = 一天；有正确作答的柱用主色，其余浅色。 */
function activityChart(activity, windowDays) {
  const daysList = activity || [];
  if (!daysList.length) return null;
  const width = 640;
  const height = 168;
  const pad = { top: 14, right: 8, bottom: 26, left: 30 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const max = Math.max(1, ...daysList.map(day => Number(day.attempts) || 0));
  const step = innerWidth / daysList.length;
  const barWidth = Math.max(1.5, step * 0.72);
  const parts = [];
  // 纵轴只标 0 / 中间值 / 最大值；最大值 < 3 时省略中间值，避免出现重复刻度。
  const gridValues = max >= 3 ? [max, Math.round(max / 2), 0] : [max, 0];
  gridValues.forEach((value, index) => {
    const y = pad.top + (innerHeight / Math.max(1, gridValues.length - 1)) * index;
    parts.push(svgNode('line', {
      x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: 'ins-chart-grid',
    }));
    parts.push(svgText(pad.left - 8, y + 4, String(value), {
      'text-anchor': 'end', class: 'ins-chart-label',
    }));
  });
  daysList.forEach((day, index) => {
    const value = Number(day.attempts) || 0;
    const barHeight = value > 0 ? Math.max(2, (value / max) * innerHeight) : 0;
    const x = pad.left + index * step + (step - barWidth) / 2;
    if (barHeight > 0) {
      parts.push(svgNode('rect', {
        x, y: pad.top + innerHeight - barHeight, width: barWidth, height: barHeight,
        rx: Math.min(2.5, barWidth / 2),
        class: `ins-chart-bar activity${day.correct > 0 ? ' has-correct' : ''}`,
      }, [svgTitle(t('insight.chart.activityTitle', { date: day.date, count: value, correct: Number(day.correct) || 0 }))]));
    }
  });
  const labelIndexes = [0, Math.floor((daysList.length - 1) / 2), daysList.length - 1];
  for (const index of labelIndexes) {
    const day = daysList[index];
    if (!day) continue;
    const anchor = index === 0 ? 'start' : index === daysList.length - 1 ? 'end' : 'middle';
    parts.push(svgText(pad.left + index * step + step / 2, height - 8, String(day.date).slice(5), {
      'text-anchor': anchor, class: 'ins-chart-label',
    }));
  }
  return svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'ins-chart',
    role: 'img',
    'aria-label': t('insight.chart.activityAria', { days: windowDays }),
  }, parts);
}

/** 错题原因环形图（stroke-dasharray 实现，无第三方库）。 */
function reasonDonut(byReason) {
  const all = (byReason || []).filter(row => Number(row.count) > 0);
  const total = all.reduce((sum, row) => sum + Number(row.count), 0);
  if (!total) return null;
  // 只画前 6 类，其余合并为「其他」，保证中心总数与真实错题数一致。
  const items = all.slice(0, 6).map(row => ({ ...row, color: null }));
  const restCount = all.slice(6).reduce((sum, row) => sum + Number(row.count), 0);
  if (restCount > 0) items.push({ reason: t('insight.other'), count: restCount });
  items.forEach((row, index) => { row.color = REASON_COLORS[index % REASON_COLORS.length]; });
  const width = 640;
  const height = 190;
  const cx = 118;
  const cy = height / 2;
  const radius = 62;
  const circumference = 2 * Math.PI * radius;
  const parts = [];
  let offset = 0;
  items.forEach(row => {
    const fraction = Number(row.count) / total;
    const length = fraction * circumference;
    parts.push(svgNode('circle', {
      cx, cy, r: radius, class: 'ins-donut-seg',
      stroke: row.color,
      'stroke-dasharray': `${Math.max(0.5, length - 2)} ${circumference - Math.max(0.5, length - 2)}`,
      'stroke-dashoffset': -offset,
      transform: `rotate(-90 ${cx} ${cy})`,
    }, [svgTitle(t('insight.chart.reasonTitle', { reason: row.reason, count: row.count, percent: Math.round(fraction * 100) }))]));
    offset += length;
  });
  parts.push(svgText(cx, cy + 2, t('insight.chart.totalQuestions', { count: number(total) }), {
    'text-anchor': 'middle', class: 'ins-donut-label',
  }));
  parts.push(svgText(cx, cy + 20, t('insight.chart.reasonCenter'), {
    'text-anchor': 'middle', class: 'ins-donut-center',
  }));
  items.forEach((row, index) => {
    const y = 32 + index * 22;
    parts.push(svgNode('rect', {
      x: 232, y: y - 9, width: 10, height: 10, rx: 3,
      fill: row.color,
    }));
    parts.push(svgText(250, y + 1, row.reason, { class: 'ins-chart-label' }));
    parts.push(svgText(width - 8, y + 1, t('insight.chart.totalQuestions', { count: row.count }), {
      'text-anchor': 'end', class: 'ins-chart-label',
    }));
  });
  return svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'ins-chart',
    role: 'img',
    'aria-label': t('insight.chart.reasonAria'),
  }, parts);
}

/* ------------------------------------------------------------------------- *
 * 页面内容卡
 * ------------------------------------------------------------------------- */

function sectionCard(title, subtitle, content, extraClass = '') {
  return el('section', { class: `ins-card ${extraClass}`.trim() },
    el('div', { class: 'ins-card-head' },
      el('h2', {}, title),
      subtitle ? el('p', { class: 'ins-card-sub' }, subtitle) : null),
    content);
}

function emptyHint(text) {
  return el('p', { class: 'ins-empty-block', style: 'padding:18px 14px;text-align:left' }, text);
}

function statCard(label, value, unit, caption, iconName) {
  return el('div', { class: 'ins-stat' },
    el('div', { class: 'ins-stat-label' }, label),
    el('div', { class: 'ins-stat-value' }, String(value), unit ? el('small', {}, unit) : null),
    el('div', { class: 'ins-stat-caption' }, caption));
}

function masteryStatCard(counts) {
  const safe = counts || {};
  const total = MASTERY_LEVELS.reduce((sum, level) => sum + Number(safe[level] || 0), 0);
  const track = el('div', { class: 'ins-mastery-track', role: 'img', 'aria-label': t('insight.masteryCard.trackAria') });
  MASTERY_LEVELS.forEach((level, index) => {
    const count = Number(safe[level] || 0);
    const width = total ? (count / total) * 100 : 0;
    const bar = el('span', {
      class: `l${index}`,
      style: `width:${width}%`,
      title: t('insight.masteryCard.barTitle', { level: masteryLabel(level), count }),
    });
    if (!count) bar.style.display = 'none';
    track.append(bar);
  });
  return el('div', { class: 'ins-stat' },
    el('div', { class: 'ins-stat-label' }, t('insight.masteryCard.label')),
    el('div', { class: 'ins-stat-value' }, `${number(total)}`, el('small', {}, t('insight.masteryCard.unit'))),
    track,
    el('div', { class: 'ins-stat-caption' },
      MASTERY_LEVELS.map(level => `${masteryShort(level)} ${number(safe[level] || 0)}`).join(' · ')));
}

/* ------------------------------------------------------------------------- *
 * 平台精选学情口径（WS26 契约：GET /api/insight/home 顶层 official 对象）
 * 字段：practice_pool / total_questions / practiced / wrong_count 等；
 * 撤回/争议题不计入该口径。接口未就绪或字段缺失时整块不渲染，绝不写死数字。
 * ------------------------------------------------------------------------- */

function officialCaliber(homeStats) {
  const official = homeStats?.official;
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
  return { ...values, note: String(official.note || official.caliber || '').trim() };
}

function officialCaliberCards(homeStats) {
  const caliber = officialCaliber(homeStats);
  if (!caliber) return [];
  const unit = t('insight.unit.questions');
  const caption = t('insight.official.caption');
  const cards = [];
  if (caliber.pool !== undefined) {
    cards.push(statCard(t('insight.official.pool'), number(caliber.pool), unit, caption, 'layers'));
  }
  if (caliber.practiced !== undefined) {
    cards.push(statCard(t('insight.official.practiced'), number(caliber.practiced), unit, caption, 'book'));
  }
  if (caliber.wrong !== undefined) {
    cards.push(statCard(t('insight.official.wrong'), number(caliber.wrong), unit, caption, 'notes'));
  }
  return cards;
}

function summaryCards(report, homeStats) {
  const summary = report.summary || {};
  const windowDays = report.range?.days || days;
  const personal = homeStats?.personal || null;
  const first = personal?.first_attempt || null;
  const repeat = personal?.repeat_practice || null;
  const pending = personal?.pending_grading || null;
  // 首次正确率 / 重复练习 / 待判分与首页同一后端口径（/api/insight/home）。
  const extraCards = personal
    ? [
      statCard(t('insight.summary.first'), first?.samples ? formatPct(first.accuracy) : '—', '',
        first?.samples
          ? t('insight.summary.firstCaption', { samples: number(first.samples) })
          : t('insight.summary.firstNone'), 'target'),
      statCard(t('insight.summary.repeat'), number(repeat?.questions || 0), t('insight.unit.questions'),
        t('insight.summary.repeatCaption', { attempts: number(repeat?.attempts || 0) }), 'repeat'),
      statCard(t('insight.summary.pending'), number(pending?.questions || 0), t('insight.unit.questions'),
        t('insight.summary.pendingCaption'), 'clock'),
    ]
    : [];
  const caliber = officialCaliber(homeStats);
  return el('div', { class: 'ins-summary', 'aria-label': t('insight.summary.aria') },
    statCard(t('insight.summary.attempts'), number(summary.attempts || 0), t('insight.unit.times'),
      t('insight.summary.attemptsCaption', { graded: number(summary.graded_attempts || 0), days: windowDays })),
    statCard(t('insight.summary.accuracy'), summary.graded_attempts ? formatPct(summary.correct_rate) : '—', '',
      t('insight.summary.accuracyCaption'), 'target'),
    masteryStatCard(summary.mastery_counts),
    statCard(t('insight.summary.due'), number(summary.due_reviews || 0), t('insight.unit.questions'), t('insight.summary.dueCaption'), 'repeat'),
    statCard(t('insight.summary.wrong'), number(summary.wrong_count || 0), t('insight.unit.questions'), t('insight.summary.wrongCaption'), 'notes'),
    statCard(t('insight.summary.ai'), number(summary.ai_jobs || 0), t('insight.unit.items'), t('insight.summary.aiCaption', { days: windowDays }), 'sparkle'),
    ...extraCards,
    ...officialCaliberCards(homeStats),
    caliber?.note ? el('p', { class: 'help ins-official-note', 'data-no-i18n': '' }, caliber.note) : null);
}

/* ------------------------------------------------------------------------- *
 * 跨模块学习足迹：AI 课堂 / 整卷练习 / 题库产出 / AI 助手（真实聚合，可跳转）
 * ------------------------------------------------------------------------- */

function integrationCard({ title, iconName, value, unit, lines, emptyText, onOpen, ariaLabel }) {
  const body = el('button', {
    type: 'button',
    class: 'ins-integration',
    'aria-label': ariaLabel || t('insight.integration.aria', { title, value, unit: unit || '' }),
  },
  el('div', { class: 'ins-integration-head' },
    icon(iconName),
    el('span', { class: 'ins-integration-title' }, title)),
  el('div', { class: 'ins-integration-value' }, String(value), unit ? el('small', {}, unit) : null),
  el('div', { class: 'ins-integration-lines' },
    ...(lines.length ? lines.map(text => el('span', {}, text)) : [el('span', {}, emptyText || t('common.noRecord'))])),
  onOpen ? el('span', { class: 'ins-integration-cta' }, t('insight.integration.cta')) : null);
  if (onOpen) body.addEventListener('click', onOpen);
  return body;
}

function integrationCards(report) {
  const integrations = report.integrations || {};
  const classroom = integrations.classroom || {};
  const papers = integrations.papers || {};
  const library = integrations.library || {};
  const assistant = integrations.assistant || {};
  const featureUsage = report.feature_usage || {};
  const featureItems = Array.isArray(featureUsage.items) ? featureUsage.items : [];
  const featureCount = id => Number(featureItems.find(item => item.feature === id)?.count || 0);
  const featureTotal = Number(featureUsage.total || 0);
  const featureSeconds = id => Math.round(Number(
    featureItems.find(item => item.feature === id)?.total_duration_ms || 0,
  ) / 1000);
  const cards = [
    integrationCard({
      title: t('nav.classroom'), iconName: 'compass', value: number(classroom.courses || 0), unit: t('insight.unit.courses'),
      lines: classroom.courses
        ? [
          t('insight.integration.classroomLine1', { ready: number(classroom.ready_courses || 0), done: number(classroom.scenes_completed || 0), total: number(classroom.scenes_total || 0) }),
          t('insight.integration.classroomLine2', { count: number(classroom.quiz_attempts || 0), extra: classroom.quiz_attempts ? t('insight.integration.accuracyExtra', { pct: formatPct(classroom.quiz_accuracy) }) : '' }),
        ]
        : [],
      emptyText: t('insight.integration.classroomEmpty'),
      ariaLabel: t('insight.integration.classroomAria', { count: number(classroom.courses || 0) }),
      onOpen: () => go('classroom'),
    }),
    integrationCard({
      title: t('insight.integration.papers'), iconName: 'printer', value: number(papers.practice_attempts || 0), unit: t('insight.unit.times'),
      lines: papers.practice_attempts
        ? [
          t('insight.integration.papersLine1', { score: formatPct(papers.practice_avg_score), accuracy: formatPct(papers.practice_accuracy) }),
          t('insight.integration.papersLine2', { count: Math.min(5, (papers.recent || []).length) }),
        ]
        : [],
      emptyText: t('insight.integration.papersEmpty'),
      ariaLabel: t('insight.integration.papersAria', { count: number(papers.practice_attempts || 0) }),
      onOpen: () => go('paperlab', null, { tab: 'papers' }),
    }),
    integrationCard({
      title: t('insight.integration.library'), iconName: 'tag', value: number(library.questions || 0), unit: t('insight.unit.questions'),
      lines: library.questions
        ? [
          t('insight.integration.libraryLine1', { verified: number(library.verified || 0), pending: number(library.pending_review || 0) }),
          t('insight.integration.libraryLine2', { papers: number(library.papers || 0), cards: number(library.cards || 0) }),
        ]
        : [],
      emptyText: t('insight.integration.libraryEmpty'),
      ariaLabel: t('insight.integration.libraryAria', { count: number(library.questions || 0) }),
      onOpen: () => go('mybank'),
    }),
    integrationCard({
      title: t('nav.assistant'), iconName: 'message', value: number(assistant.conversations || 0), unit: t('insight.unit.sessions'),
      lines: assistant.conversations
        ? [t('insight.integration.assistantLine', { count: number(assistant.messages || 0) })]
        : [],
      emptyText: t('insight.integration.assistantEmpty'),
      ariaLabel: t('insight.integration.assistantAria', { count: number(assistant.conversations || 0) }),
      onOpen: () => go('assistant'),
    }),
    // 阶段 5-3：演示 / 公式 / 语音成功使用频次（只统计，不记录正文）。
    integrationCard({
      title: t('insight.feature.title'), iconName: 'sparkle',
      value: number(featureTotal), unit: t('insight.unit.times'),
      lines: featureTotal
        ? [
          t('insight.feature.line', {
            demo: number(featureCount('demo')),
            formula: number(featureCount('formula')),
            speech: number(featureCount('speech')),
          }),
          t('insight.feature.duration', {
            demo: number(featureSeconds('demo')),
            formula: number(featureSeconds('formula')),
            speech: number(featureSeconds('speech')),
          }),
        ]
        : [],
      emptyText: t('insight.feature.empty'),
      ariaLabel: t('insight.feature.aria', { count: number(featureTotal) }),
    }),
  ];
  const recent = Array.isArray(papers.recent) ? papers.recent.slice(0, 3) : [];
  const recentNode = recent.length
    ? el('div', { class: 'ins-recent-papers' },
      el('span', { class: 'ins-recent-papers-title' }, t('insight.integration.recentPapers')),
      ...recent.map(item => el('span', { class: 'ins-recent-paper' },
        el('strong', {}, item.paper_title || t('home.ai.unnamedPaper')),
        t('insight.integration.scoreLine', { score: number(item.score), max: number(item.max_score) }),
        item.submitted_at ? ` · ${dateText(item.submitted_at)}` : '')))
    : null;
  return el('section', { class: 'ins-card ins-integrations-card', 'aria-label': t('insight.integration.section') },
    el('div', { class: 'ins-card-head' },
      el('h2', {}, t('insight.integration.section')),
      el('p', { class: 'ins-card-sub' }, t('insight.integration.sub'))),
    el('div', { class: 'ins-integrations' }, ...cards),
    recentNode);
}

function activityMixSection(report) {
  const mix = report.activity_mix || {};
  const items = Array.isArray(mix.items) ? mix.items : [];
  // 后端给的四类活动 key 固定（practice/classroom/paper_practice/assistant）；
  // 已知 key 用语言包，未知 key 保持后端 label（数据）。
  const mixLabel = item => {
    const key = item?.key ? `insight.activityMix.${item.key}` : '';
    const translated = key ? t(key) : '';
    return !key || translated === key ? (item.label || item.key || t('insight.activity.fallback')) : translated;
  };
  const rows = items.map(item => ({ label: mixLabel(item), value: Number(item.count) || 0 }));
  const chart = rows.length
    ? horizontalBars(rows, {
      ariaLabel: t('insight.activity.chartAria'),
      unit: t('insight.activity.unit'),
      toneOf: () => 'accent-2',
      formatValue: value => t('insight.activity.value', { count: number(value) }),
    })
    : null;
  return sectionCard(
    t('insight.activity.title'),
    t('insight.activity.sub', { days: report.range?.days || days }),
    chart || emptyHint(t('insight.activity.empty')),
    'ins-activity-mix-card');
}

function knowledgeGroup(title, items, tone, selection = null) {
  if (!items.length) return null;
  return el('div', { class: 'ins-kp-group' },
    el('h3', { class: 'ins-kp-group-title' }, title, el('span', { class: 'task-filter-count' }, t('insight.kp.groupCount', { count: items.length }))),
    el('div', { class: 'ins-kp-list' }, ...items.map(item => kpItem(item, selection))));
}

function kpItem(item, selection = null) {
  const mastery = item.mastery_level || '—';
  const graded = Number(item.graded_attempts ?? item.attempts ?? 0);
  // M3：薄弱知识点条目支持勾选后一次生成掌握路径（≤8 个，由控制器限流）。
  let pickNode = null;
  if (selection && item.weak && Number(item.kp_id)) {
    const checkbox = el('input', {
      type: 'checkbox',
      class: 'm3-kp-pick-box',
      checked: selection.isSelected(item.kp_id),
      onChange: event => {
        if (!selection.toggle(item.kp_id, event.currentTarget.checked)) {
          event.currentTarget.checked = !event.currentTarget.checked;
        }
      },
    });
    pickNode = el('label', { class: 'm3-kp-pick' }, checkbox, el('span', {}, tm('study.m3.kpPick')));
  }
  return el('details', { class: 'ins-kp-item' },
    el('summary', {},
      el('span', { class: 'ins-kp-name' }, item.title),
      item.weak ? badge(t('insight.kp.weak'), 'red') : (Number(item.accuracy) >= 85 ? badge(t('insight.kp.good'), 'teal') : badge(t('insight.kp.normal'), '')),
      el('span', { class: 'ins-kp-meta' }, t('insight.kp.meta', { count: number(item.attempts), pct: formatPct(item.accuracy) }))),
    el('div', { class: 'ins-kp-body' },
      item.path ? el('div', { class: 'ins-kp-path' }, t('insight.kp.path', { path: item.path })) : null,
      el('div', {}, t('insight.kp.line2', {
        questions: number(item.question_count || 0),
        attempts: number(item.attempts || 0),
        graded: number(item.graded_attempts ?? item.attempts ?? 0),
      })),
      // 标签覆盖不足：显示真实样本数，不做个性化推断（与首页/后端同一门槛）。
      graded < MIN_WEAK_SAMPLE
        ? el('div', { class: 'ins-kp-sample' }, t('insight.kp.sampleLimited', { graded: number(graded), min: number(MIN_WEAK_SAMPLE) }))
        : null,
      el('div', {}, t('insight.kp.line3', {
        accuracy: formatPct(item.accuracy), mastery: masteryLabel(mastery), wrong: number(item.wrong_count || 0),
      })),
      pickNode));
}

function notStartedDetails(knowledge) {
  const list = knowledge.not_started || [];
  if (!list.length) return null;
  return el('details', { class: 'ins-notstarted' },
    el('summary', {}, t('insight.kp.notStarted', { count: number(knowledge.not_started_total || list.length) })),
    el('div', { class: 'ins-notstarted-tags' },
      ...list.slice(0, 30).map(item => el('span', {}, item.title))));
}

function knowledgeSection(knowledge, selection = null) {
  const items = knowledge.items || [];
  const head = el('div', { class: 'ins-card-head' },
    el('h2', {}, t('insight.kp.title')),
    el('p', { class: 'ins-card-sub' }, t('insight.kp.sub')));
  if (!items.length) {
    return el('section', { class: 'ins-card ins-card-wide' }, head,
      el('p', { class: 'ins-kp-empty', 'data-no-i18n': '' }, knowledge.note || t('insight.kp.empty')));
  }
  const weak = items.filter(item => item.weak);
  // 样本不足（已判定 < MIN_WEAK_SAMPLE）且正确率偏低：不标「薄弱」，也不能混进「其他」里消失
  // ——「练了两次都错」是真实早期信号，单独成组只报样本数，不给结论。
  const limitedIds = new Set(
    items.filter(item => item.sample_limited && !item.weak).map(item => item.kp_id));
  const limited = items.filter(item => limitedIds.has(item.kp_id));
  const strongIds = new Set((knowledge.strong || []).map(item => item.kp_id));
  const strong = items.filter(item => strongIds.has(item.kp_id) && !limitedIds.has(item.kp_id));
  const shown = new Set([...weak, ...limited, ...strong].map(item => item.kp_id));
  const rest = items.filter(item => !shown.has(item.kp_id));
  return el('section', { class: 'ins-card ins-card-wide' }, head,
    knowledge.note ? el('div', { 'data-no-i18n': '' }, notice(knowledge.note, 'warning')) : null,
    el('div', { class: 'ins-kp-groups' },
      knowledgeGroup(t('insight.kp.groupWeak'), weak, 'red', selection),
      knowledgeGroup(t('insight.kp.groupLimited'), limited, 'amber'),
      knowledgeGroup(t('insight.kp.groupStrong'), strong, 'teal'),
      knowledgeGroup(t('insight.kp.groupRest'), rest, ''),
      notStartedDetails(knowledge)));
}

/** 批次五（§6.6）：知识图谱。首次点击时才拉取数据并懒加载 sigma/graphology。 */
function knowledgeGraphSection(subject, days) {
  const mount = el('div', { class: 'ins-kp-graph-mount' });
  let controller = null;
  const loadButton = button(t('insight.graph.load'), event => busy(event.currentTarget, async () => {
    const qs = query({ subject: subject || undefined, days });
    const data = await api(`/insight/kp-graph?${qs}`);
    const module = await import('../components/knowledgeGraph.js');
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } }
    controller = module.renderKnowledgeGraph(mount, data, {
      onSelect: node => go('practice', null, {
        subject: data.subject || subject || '',
        kp_id: String(node.kp_id),
      }),
    });
    loadButton.hidden = true;
  }), 'soft small', 'layers');
  const note = el('p', { class: 'help ins-kp-graph-note' }, t('insight.graph.hint'));
  return sectionCard(t('insight.graph.title'), t('insight.graph.sub'),
    el('div', { class: 'ins-kp-graph-body' }, loadButton, note, mount));
}

function recommendationAction(item) {
  const action = String(item?.action || '');
  const preset = {
    wrongbook: { label: t('insight.rec.wrongbook'), route: 'wrongbook', params: null, icon: 'repeat' },
    classroom: { label: t('insight.rec.classroom'), route: 'classroom', params: null, icon: 'compass' },
    paperlab: { label: t('insight.rec.paperlab'), route: 'paperlab', params: { tab: 'papers' }, icon: 'printer' },
    mybank: { label: t('insight.rec.mybank'), route: 'mybank', params: null, icon: 'tag' },
    assistant: { label: t('insight.rec.assistant'), route: 'assistant', params: null, icon: 'message' },
  }[action];
  return preset || { label: t('insight.rec.practice'), route: 'practice', params: null, icon: 'book' };
}

function recommendationsSection(recommendations) {
  const items = recommendations || [];
  const content = items.length
    ? el('div', { class: 'ins-recommendations' }, ...items.map(item => {
      const action = recommendationAction(item);
      return el('div', { class: 'ins-rec' },
        el('div', { class: 'ins-rec-main' },
          el('strong', { 'data-no-i18n': '' }, item.title || t('insight.rec.itemFallback')),
          el('p', { 'data-no-i18n': '' }, item.reason || '')),
        button(action.label, () => go(action.route, null, action.params), 'soft small', action.icon));
    }))
    : emptyHint(t('insight.rec.empty'));
  return sectionCard(t('insight.rec.title'), t('insight.rec.sub'), content);
}

/* ------------------------------------------------------------------------- *
 * AI 学习建议卡
 * ------------------------------------------------------------------------- */

function aiMark(text = t('insight.ai.mark')) {
  return el('div', { class: 'ins-ai-mark' }, badge(t('insight.ai.badge'), 'amber'), el('span', { 'data-no-i18n': '' }, text));
}

function adviceSnapshotFoot(result) {
  const snapshot = result.statistics_snapshot || {};
  const summary = snapshot.summary || {};
  const range = snapshot.range || result;
  const parts = [];
  if (range && range.days) parts.push(t('insight.ai.snapWindow', { days: range.days }));
  if (range && range.subject) parts.push(t('insight.ai.snapSubject', { subject: range.subject }));
  if (summary.attempts !== undefined) parts.push(t('insight.ai.snapAttempts', { count: number(summary.attempts) }));
  if (summary.correct_rate !== undefined) parts.push(t('insight.ai.snapAccuracy', { pct: formatPct(summary.correct_rate) }));
  if (summary.due_reviews !== undefined) parts.push(t('insight.ai.snapDue', { count: number(summary.due_reviews) }));
  return parts.join(' · ');
}

function createAdviceCard(report, version) {
  const windowDays = report.range?.days || days;
  const subjectLabel = state.insightSubject || t('insight.subjectAll');
  const card = el('section', { class: 'ins-card ins-card-wide ins-advice' });
  const head = el('div', { class: 'ins-card-head' },
    el('h2', {}, t('insight.ai.title')),
    el('p', { class: 'ins-card-sub' },
      t('insight.ai.sub', { days: windowDays, subject: subjectLabel })));
  card.append(head);

  const form = el('div', { class: 'ins-advice-form' });
  const jobMount = el('div', { class: 'ins-advice-job' });
  const resultMount = el('div', { class: 'ins-advice-result' });
  const statusHint = el('p', { class: 'ins-card-sub', 'aria-live': 'polite' });

  function clearJob() {
    if (jobMount.querySelector('.job-card')) jobMount.replaceChildren();
  }

  function showResult(result) {
    lastAdvice = { result, generatedAt: Date.now() };
    clearJob();
    const snapshotFoot = adviceSnapshotFoot(result);
    const box = el('div', { class: 'ins-ai-result' },
      aiMark(result.ai_notice ? t('insight.ai.markPrefix', { notice: result.ai_notice }) : undefined),
      el('div', { class: 'ins-ai-result-head' },
        el('strong', {}, t('insight.ai.resultTitle')),
        result.prompt_version ? badge(`prompt ${result.prompt_version}`, '') : null),
      markdown(result.markdown || result.content || t('insight.ai.noContent')),
      el('div', { class: 'ins-ai-foot' },
        result.generated_at ? t('insight.ai.generatedAt', { time: dateText(result.generated_at) }) : null,
        snapshotFoot || null));
    resultMount.replaceChildren(box);
    statusHint.textContent = '';
  }

  function mountActiveJob(jobId) {
    narrativeJobId = jobId;
    resultMount.replaceChildren();
    statusHint.textContent = t('insight.ai.generating');
    generateBtn.disabled = true;
    mountJob(jobMount, jobId, doneJob => {
      generateBtn.disabled = false;
      narrativeJobId = null;
      statusHint.textContent = '';
      if (doneJob && doneJob.result) {
        showResult(doneJob.result);
        toast(t('insight.ai.generatedToast'));
      }
    }, {
      task: 'insight-report',
      onSettled: job => {
        generateBtn.disabled = false;
        // 仅当任务真正进入终态时才清掉任务指针；路由切走（job=null）时保留，
        // 回到页面后仍可重新挂载正在运行的任务。
        if (job) narrativeJobId = null;
        if (job && ['failed', 'interrupted', 'cancelled'].includes(job.status)) {
          statusHint.textContent = job.error || t('insight.ai.failed');
        }
      },
    });
  }

  async function generate() {
    if (!report.has_data) throw new Error(t('insight.ai.noDataError'));
    if (!picker) throw new Error(t('insight.ai.noProviderError'));
    const payload = picker.payload(false);
    generateBtn.disabled = true;
    try {
      const job = await api('/insight/narrative', {
        method: 'POST',
        data: {
          provider_id: payload.provider_id,
          model: payload.model,
          subject: state.insightSubject || null,
          days: windowDays,
        },
      });
      if (!current(version)) return;
      mountActiveJob(job.id);
    } finally {
      if (current(version)) generateBtn.disabled = false;
    }
  }

  const generateBtn = button(t('insight.ai.generate'), event => busy(event.currentTarget, generate), '', 'sparkle');
  let picker = null;

  if (!(state.providers || []).length) {
    generateBtn.disabled = true;
    form.append(
      notice(t('insight.ai.noProviders')),
      el('div', { class: 'ins-advice-actions' },
        button(t('insight.ai.goSettings'), () => go('settings'), 'secondary', 'sliders'),
        generateBtn));
  } else {
    picker = createModelPicker('insight-report', { showModeToggle: false });
    form.append(picker.node,
      el('div', { class: 'ins-advice-actions' },
        generateBtn,
        button(t('insight.ai.goTasks'), () => go('tasks'), 'ghost small', 'history')));
    if (!report.has_data) {
      generateBtn.disabled = true;
      form.append(notice(t('insight.ai.noDataHint'), 'warning'));
    }
  }
  form.append(jobMount, statusHint, resultMount);
  card.append(form);

  return {
    node: card,
    mountActiveJob,
    showResult,
    resume() {
      if (narrativeJobId) mountActiveJob(narrativeJobId);
      else if (lastAdvice) showResult(lastAdvice.result);
    },
  };
}

/* ------------------------------------------------------------------------- *
 * M3 掌握路径（/api/mastery-paths）
 * -------------------------------------------------------------------------
 * 列表：GET /api/mastery-paths?status=&limit=
 * 生成：POST /api/mastery-paths {subject?,kp_ids?:[int],title?,source_kind?}
 *       无数据自动生成会 422（中文「样本不足」原因），前端直接展示后端原因。
 * 详情：GET /api/mastery-paths/{id}；节点操作 restate / refresh / archive。
 * 接口未就绪或失败时整块中文降级（隐藏生成入口），不影响学情其它卡片与 AI 建议。
 * 完成状态只来自后端返回的真实节点状态与学习证据，前端不推断「已掌握」。
 * ------------------------------------------------------------------------- */

// 语言包缺键兜底：新键由 Lead 统一补进 locales；缺键时先显示内置中文。
const M3_TEXT = {
  'study.m3.masteryPath': '掌握路径',
  'study.m3.masteryPathSub': '把薄弱知识点拆成「变式练习 / 概念复述」的可核验步骤，完成条件来自明确学习证据。',
  'study.m3.createPath': '生成掌握路径',
  'study.m3.createPathForSelected': '用所选知识点生成',
  'study.m3.selectWeakHint': '在下方「薄弱知识点」中勾选要加入路径的知识点（最多 {max} 个）。',
  'study.m3.selectedCount': '已选 {count} 个知识点',
  'study.m3.selectionLimit': '一次最多选择 {max} 个知识点',
  'study.m3.noWeakItems': '当前没有薄弱知识点，可按当前筛选自动选择知识点生成路径。',
  'study.m3.kpPick': '加入掌握路径',
  'study.m3.pathsEmpty': '还没有掌握路径',
  'study.m3.pathsEmptyHint': '从薄弱知识点生成掌握路径，按变式练习与概念复述逐步核验。',
  'study.m3.pathsUnavailable': '掌握路径服务暂不可用，学情其它数据不受影响。',
  'study.m3.pathProgress': '进度 {done}/{total} · {percent}%',
  'study.m3.openPath': '查看路径',
  'study.m3.untitledPath': '未命名掌握路径',
  'study.m3.untitledNode': '未命名节点',
  'study.m3.pathStatus.active': '进行中',
  'study.m3.pathStatus.in_progress': '进行中',
  'study.m3.pathStatus.pending': '待完成',
  'study.m3.pathStatus.done': '已完成',
  'study.m3.pathStatus.completed': '已完成',
  'study.m3.pathStatus.archived': '已归档',
  'study.m3.pathKind.practice': '变式练习',
  'study.m3.pathKind.concept': '概念复述',
  'study.m3.sampleInsufficient': '样本不足：当前证据不足以确认掌握，路径不会伪造完成状态。',
  'study.m3.sampleInsufficientBadge': '样本不足',
  'study.m3.sampleSufficient': '样本充足',
  'study.m3.sampleNoteLine': '样本说明：{note}',
  'study.m3.requirementPractice': '独立答对 {count} 道不同题',
  'study.m3.requirementRestate': '用自己的话复述（至少 {min} 字）',
  'study.m3.refreshEvidence': '刷新核验',
  'study.m3.restatePlaceholder': '用不少于 20 字复述这个概念的关键条件、步骤与易错点',
  'study.m3.restateHint': '复述会被记录，节点完成与否以真实学习证据为准。',
  'study.m3.restateCount': '{count}/{max} 字（至少 {min} 字）',
  'study.m3.submitRestate': '提交复述',
  'study.m3.restateTooShort': '复述内容至少需要 20 个字',
  'study.m3.restateDone': '复述已提交，节点已完成',
  'study.m3.pathCompleted': '路径已完成 · 完成条件来自明确学习证据',
  'study.m3.archivePath': '归档',
  'study.m3.archiveConfirm': '归档后该路径将从进行中列表移除（{title}），确认归档？',
  'study.m3.archived': '路径已归档',
  'study.m3.pathCreated': '掌握路径已生成',
  'study.m3.pathReplayed': '已复用现有掌握路径',
  'study.m3.pathCreateFailed': '生成掌握路径失败：{message}',
  'study.m3.pathDetailLoading': '正在加载路径详情…',
  'study.m3.pathDetailFailed': '路径详情加载失败：{message}',
  'study.m3.requirementsLine': '完成要求：',
  'study.m3.evidenceLine': '证据：',
  'study.m3.evidenceMatched': '已匹配 {count} 道不同题',
  'study.m3.evidencePartial': '目前只有 {count} 道题的独立答对证据，仍需补充',
  'study.m3.evidenceMissing': '缺少独立答对证据',
  'study.m3.evidencePending': '证据待核验',
  'study.m3.needMoreEvidence': '仍需证据：至少两道不同题独立答对',
  'study.m3.needMoreEvidenceCount': '仍需证据：还差 {count} 道不同题独立答对',
  'study.m3.nodeDoneToast': '节点已完成',
  'study.m3.recentEvents': '最近事件',
  'study.m3.completedAt': '完成于 {time}',
  'study.m3.pathListAria': '掌握路径列表',
};

function tm(key, vars = null) {
  const resolved = t(key, vars);
  if (resolved !== key) return resolved;
  const fallback = Object.prototype.hasOwnProperty.call(M3_TEXT, key) ? M3_TEXT[key] : key;
  if (!vars) return fallback;
  return fallback.replace(/\{(\w+)\}/g, (match, name) => (
    vars[name] === undefined || vars[name] === null ? match : String(vars[name])
  ));
}

/** 外部标题/文本字段只做字符串安全化，不拼接 HTML。 */
function m3Title(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/\s+/g, ' ').trim();
}

const PATH_STATUS_KEYS = {
  active: 'study.m3.pathStatus.active',
  in_progress: 'study.m3.pathStatus.in_progress',
  pending: 'study.m3.pathStatus.pending',
  done: 'study.m3.pathStatus.done',
  completed: 'study.m3.pathStatus.completed',
  archived: 'study.m3.pathStatus.archived',
};

function pathStatusLabel(status) {
  const key = PATH_STATUS_KEYS[String(status || '')];
  return key ? tm(key) : String(status || '');
}

function pathStatusTone(status) {
  const value = String(status || '');
  if (value === 'done' || value === 'completed') return 'teal';
  if (value === 'archived') return '';
  if (value === 'in_progress' || value === 'active') return 'amber';
  return '';
}

function pathKindLabel(kind) {
  const value = String(kind || '');
  if (value === 'practice') return tm('study.m3.pathKind.practice');
  if (value === 'concept') return tm('study.m3.pathKind.concept');
  return value;
}

function progressNumbers(progress, nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  const total = Number(progress?.nodes_total ?? list.length) || 0;
  const done = Number(progress?.nodes_done ?? list.filter(node => node.status === 'done').length) || 0;
  // 进度百分比以真实节点计数推导，避免后端 percent 是 0–1 还是 0–100 的歧义；
  // 只有缺少 nodes_total 时才回退使用后端 percent。
  const computed = total ? Math.round((done / total) * 100) : null;
  const rawPercent = Number(progress?.percent);
  const percent = computed !== null
    ? Math.max(0, Math.min(100, computed))
    : (Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, Math.round(rawPercent))) : 0);
  return { total, done, percent };
}

// 样本不足口径：契约值为 insufficient；同时容忍中文数据值，绝不隐藏真实状态。
function isSampleInsufficient(value) {
  return value === 'insufficient' || value === '样本不足';
}

/** sample_status 徽标：sufficient 明确标「样本充足」，insufficient 标「样本不足」。 */
function sampleStatusBadge(value) {
  if (isSampleInsufficient(value)) return badge(tm('study.m3.sampleInsufficientBadge'), 'amber');
  if (value === 'sufficient') return badge(tm('study.m3.sampleSufficient'), 'teal');
  return null;
}

/**
 * 节点完成条件摘要。后端 requirements 是结构化 JSON：
 * practice = {kind:'two_independent_correct', count:2}；concept = {kind:'restate', min_chars:20}。
 * 已知 kind 用语言包翻译，未知结构退回通用文本摘要（不拼 HTML、不臆造字段）。
 */
function requirementSummary(node) {
  const requirements = node?.requirements;
  if (requirements && typeof requirements === 'object' && !Array.isArray(requirements)) {
    const kind = String(requirements.kind || '');
    if (kind === 'two_independent_correct') {
      return tm('study.m3.requirementPractice', { count: Number(requirements.count) || 2 });
    }
    if (kind === 'restate') {
      return tm('study.m3.requirementRestate', { min: Number(requirements.min_chars) || 20 });
    }
  }
  return summaryText(requirements, 140);
}

/** 需求/证据等结构化字段 → 可读摘要（只做文本渲染，不拼 HTML）。 */
function summaryText(value, limit = 140) {
  const text = (() => {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(item => summaryText(item, 60)).filter(Boolean).join('；');
    if (typeof value === 'object') {
      const direct = value.text || value.summary || value.note || value.description || value.label;
      if (typeof direct === 'string' && direct.trim()) return direct;
      const parts = [];
      for (const [key, item] of Object.entries(value)) {
        if (item === null || item === undefined || item === '') continue;
        if (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean') {
          parts.push(`${key}: ${item}`);
        } else if (Array.isArray(item)) {
          const inner = summaryText(item, 40);
          if (inner) parts.push(`${key}: ${inner}`);
        }
      }
      return parts.join('；');
    }
    return String(value);
  })().replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** 证据摘要：按「两道不同题独立答对」口径读数；字段缺失时明确写「缺少证据」。 */
function evidenceSummary(node) {
  const evidence = node?.evidence;
  if (evidence === null || evidence === undefined) {
    return node?.kind === 'practice' ? tm('study.m3.evidenceMissing') : tm('study.m3.evidencePending');
  }
  if (typeof evidence === 'string') {
    const text = evidence.trim();
    return text || tm('study.m3.evidencePending');
  }
  if (Array.isArray(evidence)) {
    const distinct = new Set(evidence.map(item => (
      item && typeof item === 'object'
        ? String(item.qid ?? item.question_id ?? item.attempt_id ?? item.id ?? JSON.stringify(item))
        : String(item)
    ))).size;
    if (!distinct) return tm('study.m3.evidenceMissing');
    return distinct >= 2
      ? tm('study.m3.evidenceMatched', { count: distinct })
      : tm('study.m3.evidencePartial', { count: distinct });
  }
  if (typeof evidence === 'object') {
    const candidates = [
      evidence.distinct_questions, evidence.matched_questions, evidence.distinct_count,
      evidence.question_count, evidence.correct_questions, evidence.distinct_correct,
      evidence.questions, evidence.count,
    ];
    const distinctQuestions = candidates.find(item => (
      item !== null && item !== undefined && item !== ''
      && !Array.isArray(item) && typeof item !== 'object'
      && Number.isFinite(Number(item))
    ));
    if (distinctQuestions !== undefined) {
      const count = Math.max(0, Math.round(Number(distinctQuestions)));
      if (count >= 2) return tm('study.m3.evidenceMatched', { count });
      if (count === 1) return tm('study.m3.evidencePartial', { count });
      return tm('study.m3.evidenceMissing');
    }
    const text = summaryText(evidence, 120);
    if (text) return text;
    const satisfied = evidence.satisfied ?? evidence.completed ?? evidence.done;
    if (satisfied === true) return tm('study.m3.evidenceMatched', { count: 2 });
    if (satisfied === false) return tm('study.m3.evidenceMissing');
  }
  return tm('study.m3.evidencePending');
}

/** 事件条目 → 可读文本（事件字段由后端定义，未知字段只做文本展示）。 */
function pathEventText(event) {
  if (!event || typeof event !== 'object') return String(event || '');
  const body = event.note || event.message || event.summary || event.text || event.detail
    || event.event_type || event.type || '';
  const when = event.created_at ? ` · ${dateText(event.created_at)}` : '';
  return `${summaryText(body, 160)}${when}`;
}

/** 掌握路径控制器：负责列表、生成入口、薄弱知识点勾选与详情弹层。 */
function createMasteryPathController({ version, page, weakItems = [], subject = '', renderId = 0 }) {
  const weakIds = new Set(weakItems.map(item => Number(item.kp_id)).filter(Boolean));
  for (const kpId of [...masteryKpSelection]) {
    if (!weakIds.has(kpId)) masteryKpSelection.delete(kpId);
  }

  const actionMount = el('div', { class: 'm3-path-actions' });
  const noticeMount = el('div', { class: 'm3-path-notice' });
  const listMount = el('div', {
    class: 'm3-path-list',
    'aria-label': tm('study.m3.pathListAria'),
  }, loading(t('common.loading')));
  const body = el('div', {}, actionMount, noticeMount, listMount);
  const node = sectionCard(
    tm('study.m3.masteryPath'),
    tm('study.m3.masteryPathSub'),
    body,
    'm3-paths-card',
  );
  const pathsCard = node;
  const state = { available: null, items: [], seq: 0 };
  const isActive = () => current(version) && renderId === insightPathRenderSeq;

  function setDegraded(degraded) {
    state.available = !degraded;
    if (page) page.classList.toggle('m3-paths-degraded', degraded);
  }

  // ---------- 薄弱知识点勾选（加入掌握路径） ----------
  const selection = {
    isSelected: kpId => masteryKpSelection.has(Number(kpId)),
    toggle: (kpId, checked) => {
      const id = Number(kpId);
      if (!id) return false;
      if (checked) {
        if (masteryKpSelection.size >= MAX_PATH_KPS) {
          toast(tm('study.m3.selectionLimit', { max: MAX_PATH_KPS }), true);
          return false;
        }
        masteryKpSelection.add(id);
      } else {
        masteryKpSelection.delete(id);
      }
      renderActions();
      return true;
    },
  };

  function renderActions() {
    if (!state.available) {
      actionMount.replaceChildren();
      return;
    }
    const children = [];
    if (masteryKpSelection.size) {
      children.push(el('span', { class: 'm3-path-selected' }, tm('study.m3.selectedCount', { count: masteryKpSelection.size })));
      children.push(button(tm('study.m3.createPathForSelected'), event => busy(event.currentTarget, () => createPath({
        kp_ids: [...masteryKpSelection].slice(0, MAX_PATH_KPS),
      })), '', 'target'));
    } else if (!weakItems.length) {
      children.push(el('span', { class: 'help' }, tm('study.m3.noWeakItems')));
      children.push(button(tm('study.m3.createPath'), event => busy(event.currentTarget, () => createPath({})), '', 'sparkle'));
    } else {
      children.push(el('span', { class: 'help' }, tm('study.m3.selectWeakHint', { max: MAX_PATH_KPS })));
    }
    actionMount.replaceChildren(...children);
  }

  // ---------- 列表 ----------
  function pathCard(item) {
    const { total, done, percent } = progressNumbers(item?.progress, null);
    return el('article', { class: 'm3-path-card', 'data-path-id': String(item?.path_id ?? '') },
      el('div', { class: 'm3-path-card-head' },
        el('div', { class: 'm3-path-card-title-wrap' },
          el('h3', { class: 'm3-path-card-title' }, m3Title(item?.title) || tm('study.m3.untitledPath')),
          el('div', { class: 'm3-path-badges' },
            item?.subject ? badge(item.subject) : null,
            item?.status ? badge(pathStatusLabel(item.status), pathStatusTone(item.status)) : null,
            sampleStatusBadge(item?.sample_status))),
        item?.path_id ? button(tm('study.m3.openPath'), () => openDetail(item.path_id), 'soft small', 'chevron') : null),
      el('div', { class: 'm3-path-progress' },
        el('div', {
          class: 'm3-progress-track', role: 'img',
          'aria-label': tm('study.m3.pathProgress', { done, total, percent }),
        }, el('span', { style: `width:${percent}%` })),
        el('span', { class: 'm3-path-progress-text' }, tm('study.m3.pathProgress', { done, total, percent })),
        item?.updated_at ? el('span', { class: 'help' }, dateText(item.updated_at)) : null));
  }

  function renderList() {
    listMount.classList.toggle('is-empty', !state.items.length);
    if (!state.items.length) {
      listMount.replaceChildren(el('div', { class: 'm3-path-empty' },
        el('p', {}, tm('study.m3.pathsEmpty')),
        el('p', { class: 'help' }, tm('study.m3.pathsEmptyHint'))));
      return;
    }
    listMount.replaceChildren(...state.items.map(pathCard));
  }

  async function load() {
    const mine = ++state.seq;
    try {
      const result = await api(`/mastery-paths?${query({ limit: 20 })}`);
      if (!isActive() || mine !== state.seq) return;
      setDegraded(false);
      state.items = Array.isArray(result?.items) ? result.items : [];
      renderActions();
      renderList();
    } catch (error) {
      if (!isActive() || mine !== state.seq) return;
      setDegraded(true);
      actionMount.replaceChildren();
      listMount.classList.add('is-empty');
      listMount.replaceChildren(
        el('div', { class: 'm3-path-empty' },
          notice(tm('study.m3.pathsUnavailable') + (error?.message ? `（${error.message}）` : ''), 'warning'),
          button(t('common.reload'), () => load(), 'secondary small', 'repeat')));
    }
  }

  // ---------- 生成 ----------
  async function createPath(extra = {}) {
    if (!state.available) {
      toast(tm('study.m3.pathsUnavailable'), true);
      return;
    }
    const payload = { ...extra };
    if (subject) payload.subject = subject;
    noticeMount.replaceChildren();
    try {
      const result = await api('/mastery-paths', { method: 'POST', data: payload });
      masteryKpSelection.clear();
      toast(result?.replayed ? tm('study.m3.pathReplayed') : tm('study.m3.pathCreated'));
      await load();
      if (!isActive()) return;
      const pathId = Number(result?.path?.path_id ?? result?.path_id) || 0;
      if (pathId) openDetail(pathId);
    } catch (error) {
      // 422（样本不足）等后端中文原因原样展示，不伪造可生成的路径。
      const reason = String(error?.message || '').trim() || tm('study.m3.sampleInsufficient');
      const text = tm('study.m3.pathCreateFailed', { message: reason });
      noticeMount.replaceChildren(notice(text, 'warning'));
      toast(text, true);
    }
  }

  // ---------- 详情弹层 ----------
  async function openDetail(pathId) {
    const dialog = el('dialog', { class: 'modal-dialog m3-path-dialog' });
    const headMount = el('div', { class: 'm3-path-dialog-head' });
    const bodyMount = el('div', { class: 'm3-path-dialog-body' }, loading(tm('study.m3.pathDetailLoading')));
    dialog.append(headMount, bodyMount);
    document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.showModal();
    try {
      const detail = await api(`/mastery-paths/${encodeURIComponent(pathId)}`);
      if (!dialog.isConnected) return;
      renderDetail(dialog, headMount, bodyMount, pathId, detail);
    } catch (error) {
      if (!dialog.isConnected) return;
      bodyMount.replaceChildren(notice(
        tm('study.m3.pathDetailFailed', { message: String(error?.message || '') }), 'error'));
    }
  }

  function mergeNode(detail, result) {
    const next = { ...detail };
    if (result?.path) next.path = result.path;
    if (result?.progress) next.progress = result.progress;
    if (result?.node && Array.isArray(next.nodes)) {
      next.nodes = next.nodes.map(item => (
        Number(item.node_id) === Number(result.node.node_id) ? result.node : item
      ));
    }
    return next;
  }

  function nodeCard(dialog, headMount, bodyMount, pathId, detail, nodeData) {
    const status = String(nodeData?.status || 'pending');
    const kind = String(nodeData?.kind || '');
    const done = status === 'done';

    function rerender(nextDetail) {
      renderDetail(dialog, headMount, bodyMount, pathId, nextDetail);
      load();
    }

    const actions = el('div', { class: 'm3-node-actions' });
    if (!done && kind === 'practice' && nodeData?.node_id) {
      actions.append(button(tm('study.m3.refreshEvidence'), event => busy(event.currentTarget, async () => {
        const result = await api(
          `/mastery-paths/${encodeURIComponent(pathId)}/nodes/${encodeURIComponent(nodeData.node_id)}/refresh`,
          { method: 'POST' },
        );
        const nextDetail = mergeNode(detail, result);
        const becameDone = result?.node?.status === 'done';
        const missing = Number(result?.node?.evidence?.missing);
        rerender(nextDetail);
        if (becameDone) toast(tm('study.m3.nodeDoneToast'));
        else if (Number.isFinite(missing) && missing > 0) {
          toast(tm('study.m3.needMoreEvidenceCount', { count: missing }), true);
        } else {
          toast(tm('study.m3.needMoreEvidence'), true);
        }
      }), 'soft small', 'rotate'));
    } else if (!done && kind === 'concept' && nodeData?.node_id) {
      actions.append(restateForm(dialog, headMount, bodyMount, pathId, detail, nodeData));
    }

    return el('article', {
      class: `m3-path-node is-${status}`,
      'data-node-id': String(nodeData?.node_id ?? ''),
    },
    el('div', { class: 'm3-node-head' },
      el('span', { class: 'm3-node-index', 'aria-hidden': 'true' }, `#${(Number(nodeData?.position) || 0) + 1}`),
      el('div', { class: 'm3-node-title-wrap' },
        el('strong', { class: 'm3-node-title' }, m3Title(nodeData?.title) || m3Title(nodeData?.kp_name) || tm('study.m3.untitledNode')),
        el('div', { class: 'm3-node-badges' },
          pathKindLabel(kind) ? badge(pathKindLabel(kind), kind === 'concept' ? 'blue' : '') : null,
          badge(pathStatusLabel(status), pathStatusTone(status)),
          sampleStatusBadge(nodeData?.sample_status)))),
    el('div', { class: 'm3-node-body' },
      el('p', { class: 'm3-node-line' },
        el('strong', {}, tm('study.m3.requirementsLine')),
        requirementSummary(nodeData) || '—'),
      el('p', { class: 'm3-node-line' },
        el('strong', {}, tm('study.m3.evidenceLine')),
        evidenceSummary(nodeData)),
      nodeData?.completed_at
        ? el('p', { class: 'help' }, tm('study.m3.completedAt', { time: dateText(nodeData.completed_at) }))
        : null,
      actions.childNodes.length ? actions : null));
  }

  function restateForm(dialog, headMount, bodyMount, pathId, detail, nodeData) {
    const textarea = el('textarea', {
      class: 'm3-restate-input',
      rows: 3,
      maxlength: '2000',
      placeholder: tm('study.m3.restatePlaceholder'),
    });
    const count = el('span', { class: 'm3-restate-count' },
      tm('study.m3.restateCount', { count: 0, max: 2000, min: 20 }));
    const submit = button(tm('study.m3.submitRestate'), event => busy(event.currentTarget, async () => {
      const text = textarea.value.trim();
      if (text.length < 20) throw new Error(tm('study.m3.restateTooShort'));
      const result = await api(
        `/mastery-paths/${encodeURIComponent(pathId)}/nodes/${encodeURIComponent(nodeData.node_id)}/restate`,
        { method: 'POST', data: { text } },
      );
      renderDetail(dialog, headMount, bodyMount, pathId, mergeNode(detail, result));
      toast(tm('study.m3.restateDone'));
      load();
    }), 'soft small', 'check');
    submit.disabled = true;
    textarea.addEventListener('input', () => {
      const length = textarea.value.trim().length;
      count.textContent = tm('study.m3.restateCount', { count: length, max: 2000, min: 20 });
      submit.disabled = length < 20;
    });
    const wrap = el('div', { class: 'm3-node-restate' },
      el('p', { class: 'help' }, tm('study.m3.restateHint')),
      textarea,
      el('div', { class: 'm3-restate-foot' }, count, submit));
    return wrap;
  }

  function renderDetail(dialog, headMount, bodyMount, pathId, detail) {
    const pathInfo = detail?.path || {};
    const nodes = (Array.isArray(detail?.nodes) ? [...detail.nodes] : [])
      .sort((left, right) => (Number(left.position) || 0) - (Number(right.position) || 0));
    const { total, done, percent } = progressNumbers(detail?.progress, nodes);
    const completed = total > 0 && done >= total;
    const sampleInsufficient = isSampleInsufficient(pathInfo.sample_status)
      || nodes.some(item => isSampleInsufficient(item?.sample_status));

    headMount.replaceChildren(
      el('div', { class: 'm3-path-dialog-title' },
        el('h3', {}, m3Title(pathInfo.title) || tm('study.m3.masteryPath')),
        el('div', { class: 'm3-path-badges' },
          pathInfo.subject ? badge(pathInfo.subject) : null,
          pathInfo.status ? badge(pathStatusLabel(pathInfo.status), pathStatusTone(pathInfo.status)) : null,
          sampleStatusBadge(pathInfo.sample_status),
          el('span', { class: 'help' }, tm('study.m3.pathProgress', { done, total, percent })))),
      el('div', { class: 'm3-path-dialog-actions' },
        pathInfo.status !== 'archived' && pathId
          ? button(tm('study.m3.archivePath'), event => busy(event.currentTarget, async () => {
            if (!window.confirm(tm('study.m3.archiveConfirm', {
              title: m3Title(pathInfo.title) || tm('study.m3.masteryPath'),
            }))) return;
            await api(`/mastery-paths/${encodeURIComponent(pathId)}/archive`, { method: 'POST' });
            toast(tm('study.m3.archived'));
            dialog.close();
            await load();
          }), 'secondary small', 'layers')
          : null,
        button(t('common.close'), () => dialog.close(), 'ghost small', 'close')));

    const children = [
      el('div', { class: 'm3-dialog-progress' },
        el('div', { class: 'm3-progress-track' }, el('span', { style: `width:${percent}%` }))),
    ];
    if (sampleInsufficient) children.push(notice(tm('study.m3.sampleInsufficient'), 'warning'));
    const sampleNote = m3Title(detail?.sample_note);
    if (sampleNote) {
      children.push(el('p', { class: 'help m3-sample-note', 'data-no-i18n': '' }, tm('study.m3.sampleNoteLine', { note: sampleNote })));
    }
    if (completed) {
      children.push(el('div', { class: 'm3-path-completed' }, icon('check'), tm('study.m3.pathCompleted')));
    }
    children.push(el('div', { class: 'm3-node-list' },
      ...nodes.map(nodeData => nodeCard(dialog, headMount, bodyMount, pathId, detail, nodeData))));
    const events = Array.isArray(detail?.recent_events) ? detail.recent_events.slice(-8) : [];
    if (events.length) {
      children.push(el('details', { class: 'm3-path-events' },
        el('summary', {}, tm('study.m3.recentEvents')),
        el('div', { class: 'm3-path-events-list', 'data-no-i18n': '' },
          ...events.map(event => el('p', {}, pathEventText(event))))));
    }
    bodyMount.replaceChildren(...children);
  }

  renderActions();

  return { node: pathsCard, load, selection };
}

/* ------------------------------------------------------------------------- *
 * 页面
 * ------------------------------------------------------------------------- */

function errorPanel(message, retry) {
  return el('div', { class: 'ins-error' },
    el('span', {}, message || t('insight.loadFailed')),
    button(t('common.reload'), retry, 'secondary small', 'repeat'));
}

function overallEmpty(report) {
  const windowDays = report.range?.days || days;
  return el('div', { class: 'ins-empty-block' },
    el('h3', {}, t('insight.empty.title')),
    el('p', {}, t('insight.empty.text', { days: windowDays })),
    el('div', { class: 'ins-empty-actions' },
      button(t('common.goPractice'), () => go('practice'), '', 'book'),
      button(t('insight.empty.review'), () => go('wrongbook'), 'secondary', 'repeat')));
}

function footer(report) {
  return el('p', { class: 'ins-footer' },
    t('insight.footer.generated', {
      time: dateText(report.generated_at),
      days: report.range?.days || days,
      subject: report.range?.subject ? t('insight.footer.subject', { subject: report.range.subject }) : t('insight.footer.allSubjects'),
    }),
    el('br'),
    t('insight.footer.caliber'));
}

export async function renderInsight(container, version, params) {
  const paramSubject = params?.get ? params.get('subject') : null;
  if (paramSubject !== null && paramSubject !== undefined) state.insightSubject = paramSubject;
  if (!state.insightSubject && params?.subject) state.insightSubject = params.subject;

  let catalog = null;
  try {
    catalog = await ensureCatalog();
  } catch {
    catalog = null;
  }
  if (!current(version)) return;
  const subjects = catalog?.subjects || [];
  const subjectSelect = select(
    [{ value: '', label: t('insight.subjectAll') }, ...subjects.map(subject => ({ value: subject, label: subject }))],
    state.insightSubject || '');
  const daysSelect = select(dayOptions(), String(days));
  const printBtn = button(t('insight.print'), () => window.print(), 'secondary', 'printer');

  const filter = el('div', { class: 'ins-filter' },
    field(t('common.subject'), subjectSelect),
    field(t('insight.filterRange'), daysSelect));
  const toolbar = el('div', { class: 'ins-toolbar' },
    filter,
    el('div', { class: 'ins-toolbar-actions' },
      el('span', { class: 'ins-range-note', 'aria-live': 'polite' })));

  const summaryHost = el('div');
  const bodyHost = el('div');
  const page = el('div', { class: 'ins-page' },
    heading('LEARNING INSIGHT', t('insight.heading'),
      t('insight.subheading'), printBtn),
    toolbar,
    summaryHost,
    bodyHost);
  container.replaceChildren(page);

  let seq = 0;
  let advice = null;

  function renderReport(report, homeStats) {
    summaryHost.replaceChildren(summaryCards(report, homeStats), integrationCards(report));
    const windowDays = report.range?.days || days;
    const activity = activityChart(report.activity, windowDays);
    const subjectBars = horizontalBars(
      (report.subjects || []).map(row => ({
        label: row.subject, value: row.accuracy, weak: Number(row.accuracy) < 70,
      })),
      {
        ariaLabel: t('insight.chart.subjectAria'),
        maxValue: 100,
        unit: '%',
        toneOf: row => (row.weak ? 'weak' : ''),
        formatValue: value => formatPct(value),
      });
    const typeBars = horizontalBars(
      (report.mistakes?.by_type || []).map(row => ({ label: row.type, value: row.count })),
      {
        ariaLabel: t('insight.chart.mistakeTypeAria'),
        unit: t('insight.unit.questionsSuffix'),
        toneOf: () => 'accent-2',
        formatValue: value => t('insight.chart.totalQuestions', { count: number(value) }),
      });
    const reasonChart = reasonDonut(report.mistakes?.by_reason || []);

    advice = createAdviceCard(report, version);
    // M3：掌握路径区块（薄弱知识点附近）；异步加载，失败只影响本区块。
    const pathRenderId = ++insightPathRenderSeq;
    const weakKpItems = Array.isArray(report.knowledge?.items)
      ? report.knowledge.items.filter(item => item.weak && Number(item.kp_id))
      : [];
    const pathController = createMasteryPathController({
      version,
      page,
      renderId: pathRenderId,
      weakItems: weakKpItems,
      subject: state.insightSubject || '',
    });
    const grid = el('div', { class: 'ins-grid' },
      sectionCard(t('insight.subjectAccuracy.title'), t('insight.subjectAccuracy.sub'),
        subjectBars || emptyHint(t('insight.subjectAccuracy.empty'))),
      sectionCard(t('insight.activityChart.title', { days: windowDays }), t('insight.activityChart.sub'),
        activity || emptyHint(t('insight.activityChart.empty'))),
      activityMixSection(report),
      sectionCard(t('insight.reason.title'), t('insight.reason.sub'),
        reasonChart || emptyHint((report.mistakes?.total || 0) === 0
          ? t('insight.reason.emptyNew')
          : report.mistakes?.note || t('insight.reason.empty'))),
      sectionCard(t('insight.mistakeType.title'), t('insight.mistakeType.sub'),
        typeBars || emptyHint(t('insight.reason.emptyNew'))),
      knowledgeSection(report.knowledge || {}, pathController.selection),
      pathController.node,
      recommendationsSection(report.recommendations),
      advice.node);

    const nodes = report.has_data ? [grid] : [overallEmpty(report), grid];
    bodyHost.replaceChildren(...nodes, footer(report));
    pathController.load();
    // 从任务中心返回或筛选变化后重新挂载仍在运行的 AI 建议任务。
    if (narrativeJobId) advice.mountActiveJob(narrativeJobId);
    else if (lastAdvice) advice.showResult(lastAdvice.result);
  }

  async function load({ silent = false } = {}) {
    const mySeq = ++seq;
    if (!silent) {
      summaryHost.replaceChildren(loading(t('insight.loading')));
      bodyHost.replaceChildren();
    }
    const rangeNote = toolbar.querySelector('.ins-range-note');
    try {
      // 报告与首页统计口径并行读取：待判分/重复练习/首次正确率以 /insight/home 为准（同一后端）。
      const [reportBody, homeBody] = await Promise.all([
        api(`/insight/report?${query({
          subject: state.insightSubject || '',
          days,
        })}`, { latest: true, latestKey: 'insight-report' }),
        api(`/insight/home?${query({ days })}`, { latest: true, latestKey: 'insight-home' })
          .catch(() => null),
      ]);
      if (!current(version) || mySeq !== seq) return;
      if (rangeNote) {
        rangeNote.textContent = t('insight.rangeNote', { days: reportBody.range?.days || days });
      }
      renderReport(reportBody, homeBody);
    } catch (error) {
      if (!current(version) || mySeq !== seq) return;
      summaryHost.replaceChildren();
      bodyHost.replaceChildren(errorPanel(error.message, () => load()));
    }
  }

  subjectSelect.addEventListener('change', () => {
    state.insightSubject = subjectSelect.value;
    load();
  });
  daysSelect.addEventListener('change', () => {
    days = Number(daysSelect.value) || DEFAULT_DAYS;
    load();
  });

  await load();
}
