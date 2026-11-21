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

onSessionReset(() => {
  narrativeJobId = null;
  lastAdvice = null;
  days = DEFAULT_DAYS;
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
  return el('div', { class: 'ins-summary', 'aria-label': t('insight.summary.aria') },
    statCard(t('insight.summary.attempts'), number(summary.attempts || 0), t('insight.unit.times'),
      t('insight.summary.attemptsCaption', { graded: number(summary.graded_attempts || 0), days: windowDays })),
    statCard(t('insight.summary.accuracy'), summary.graded_attempts ? formatPct(summary.correct_rate) : '—', '',
      t('insight.summary.accuracyCaption'), 'target'),
    masteryStatCard(summary.mastery_counts),
    statCard(t('insight.summary.due'), number(summary.due_reviews || 0), t('insight.unit.questions'), t('insight.summary.dueCaption'), 'repeat'),
    statCard(t('insight.summary.wrong'), number(summary.wrong_count || 0), t('insight.unit.questions'), t('insight.summary.wrongCaption'), 'notes'),
    statCard(t('insight.summary.ai'), number(summary.ai_jobs || 0), t('insight.unit.items'), t('insight.summary.aiCaption', { days: windowDays }), 'sparkle'),
    ...extraCards);
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
  el('span', { class: 'ins-integration-cta' }, t('insight.integration.cta')));
  if (onOpen) body.addEventListener('click', onOpen);
  return body;
}

function integrationCards(report) {
  const integrations = report.integrations || {};
  const classroom = integrations.classroom || {};
  const papers = integrations.papers || {};
  const library = integrations.library || {};
  const assistant = integrations.assistant || {};
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

function knowledgeGroup(title, items, tone) {
  if (!items.length) return null;
  return el('div', { class: 'ins-kp-group' },
    el('h3', { class: 'ins-kp-group-title' }, title, el('span', { class: 'task-filter-count' }, t('insight.kp.groupCount', { count: items.length }))),
    el('div', { class: 'ins-kp-list' }, ...items.map(kpItem)));
}

function kpItem(item) {
  const mastery = item.mastery_level || '—';
  const graded = Number(item.graded_attempts ?? item.attempts ?? 0);
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
      }))));
}

function notStartedDetails(knowledge) {
  const list = knowledge.not_started || [];
  if (!list.length) return null;
  return el('details', { class: 'ins-notstarted' },
    el('summary', {}, t('insight.kp.notStarted', { count: number(knowledge.not_started_total || list.length) })),
    el('div', { class: 'ins-notstarted-tags' },
      ...list.slice(0, 30).map(item => el('span', {}, item.title))));
}

function knowledgeSection(knowledge) {
  const items = knowledge.items || [];
  const head = el('div', { class: 'ins-card-head' },
    el('h2', {}, t('insight.kp.title')),
    el('p', { class: 'ins-card-sub' }, t('insight.kp.sub')));
  if (!items.length) {
    return el('section', { class: 'ins-card ins-card-wide' }, head,
      el('p', { class: 'ins-kp-empty', 'data-no-i18n': '' }, knowledge.note || t('insight.kp.empty')));
  }
  const weak = items.filter(item => item.weak);
  const strongIds = new Set((knowledge.strong || []).map(item => item.kp_id));
  const strong = items.filter(item => strongIds.has(item.kp_id));
  const shown = new Set([...weak, ...strong].map(item => item.kp_id));
  const rest = items.filter(item => !shown.has(item.kp_id));
  return el('section', { class: 'ins-card ins-card-wide' }, head,
    knowledge.note ? el('div', { 'data-no-i18n': '' }, notice(knowledge.note, 'warning')) : null,
    el('div', { class: 'ins-kp-groups' },
      knowledgeGroup(t('insight.kp.groupWeak'), weak, 'red'),
      knowledgeGroup(t('insight.kp.groupStrong'), strong, 'teal'),
      knowledgeGroup(t('insight.kp.groupRest'), rest, ''),
      notStartedDetails(knowledge)));
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
      knowledgeSection(report.knowledge || {}),
      recommendationsSection(report.recommendations),
      advice.node);

    const nodes = report.has_data ? [grid] : [overallEmpty(report), grid];
    bodyHost.replaceChildren(...nodes, footer(report));
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
