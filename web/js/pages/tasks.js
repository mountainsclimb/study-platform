/* 任务中心：统一查看全部 AI 任务（讲解/评分/笔记/识别/课堂/组卷/学情报告/助手…）。
 *
 * 数据来自 `GET /api/ai/jobs`（含核心任务与各整合模块注册的扩展任务）。
 * - 列表：任务中文名（未知任务显示原名）、状态徽章、阶段、时间、模型、token 用量、结果摘要、错误；
 * - 操作：取消、续跑、打开结果（按任务类型跳转对应页面）、详情；
 * - 详情：请求参数、结果 JSON 摘要、模型调用信息；运行中任务用 core/jobs.js 的 mountJob 展示
 *   SSE/轮询进度（含取消/提纲确认 UI）；
 * - 有运行中任务时每 3 秒静默刷新；离开页面（路由切换/版本变化/登出）自动停止。
 */
import { api } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import { t, getLocale } from '../core/i18n.js';
import {
  el, heading, button, select, field, badge, notice, empty, loading, busy, toast,
  number, dateText,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import {
  mountJob, taskNames, statusNames, stageLabel, statusBadgeVariant, parseStageProgress,
} from '../core/jobs.js';

// 任务类型/状态名：走 i18n（tasks.task.* / tasks.status.*），缺键回退 core/jobs.js 的中文映射，
// 仍未知的扩展任务直接显示原任务名，不报错。
const ACTIVE_STATUSES = new Set(['queued', 'running', 'cancel_requested']);
const RESUME_STATUSES = new Set(['failed', 'interrupted', 'cancelled']);
const STATUS_FILTERS = [
  { id: 'all', labelKey: 'tasks.filter.all', match: () => true },
  { id: 'running', labelKey: 'tasks.filter.running', match: status => ACTIVE_STATUSES.has(status) || status === 'awaiting_outline' },
  { id: 'failed', labelKey: 'tasks.filter.failed', match: status => status === 'failed' || status === 'interrupted' },
  { id: 'completed', labelKey: 'tasks.filter.completed', match: status => status === 'completed' || status === 'succeeded' },
];

// 路由切走 / 登出时清掉轮询与筛选残留。
let pollTimer = null;
function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}
onSessionReset(() => {
  stopPolling();
  state.tasksTaskFilter = '';
});

function taskLabel(task) {
  const key = String(task || '');
  const translated = t(`tasks.task.${key}`);
  if (translated !== `tasks.task.${key}`) return translated;
  return taskNames[key] || key || t('tasks.unknownTask');
}

function statusLabel(status) {
  const key = String(status || '');
  const translated = t(`tasks.status.${key}`);
  if (translated !== `tasks.status.${key}`) return translated;
  return statusNames[key] || key || t('tasks.unknownStatus');
}

function statusFilter(id) {
  return STATUS_FILTERS.find(item => item.id === id) || STATUS_FILTERS[0];
}

/* ------------------------------------------------------------------------- *
 * 耗时 / 重试 / 今日用量（WS23：AI 结果可追踪）
 * 服务端账本见 GET /api/ai/usage/me；接口未就绪时只隐藏用量面板，不影响任务列表。
 * ------------------------------------------------------------------------- */

function durationText(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  const minutes = Math.floor(value / 60_000);
  const seconds = Math.round((value % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

/** 任务耗时：优先 started_at → finished_at；未开始执行的任务按创建时间起算。 */
function jobDurationMs(job) {
  const start = Date.parse(job?.started_at || job?.created_at || '');
  const end = Date.parse(job?.finished_at || job?.updated_at || '');
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return end - start;
}

function retriesOf(job) {
  const value = Number(job?.retry_count ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function moneyMicroText(micro) {
  const value = Number(micro);
  if (micro === null || micro === undefined || !Number.isFinite(value)) return null;
  return (value / 1_000_000).toFixed(4);
}

function quotaEntryText(entry) {
  const limit = entry?.daily_limit;
  if (limit === null || limit === undefined || Number(limit) === 0) {
    return t('tasks.usage.quotaUnlimited');
  }
  return t('tasks.usage.quotaEntry', {
    used: number(entry.used_today || 0),
    limit: number(limit),
  });
}

/** 本人今日用量与剩余配额（数据来自服务端账本，失败降级为隐藏）。 */
function todayUsagePanel(usage) {
  const today = usage?.today || {};
  const quota = usage?.quota || {};
  const entries = Array.isArray(quota.kinds) ? quota.kinds : [];
  const visible = entries.filter(entry => Number(entry.used_today) > 0
    || ['explain', 'grade', 'notes', 'recognize', 'ocr', 'course'].includes(entry.kind));
  const chips = [];
  chips.push(el('span', { class: 'task-usage-chip' }, t('tasks.usage.jobs', { count: number(today.jobs || 0) })));
  if (Number(today.tokens_in) || Number(today.tokens_out)) {
    chips.push(el('span', { class: 'task-usage-chip' },
      t('tasks.usage.tokens', { input: number(today.tokens_in || 0), output: number(today.tokens_out || 0) })));
  }
  if (Number(today.failed)) {
    chips.push(el('span', { class: 'task-usage-chip task-usage-chip-warn' },
      t('tasks.usage.failed', { count: number(today.failed) })));
  }
  const quotaChips = visible.map(entry => el('span', {
    class: `task-usage-chip${entry.limit_reached ? ' task-usage-chip-warn' : ''}`,
    title: entry.remaining_today === null || entry.remaining_today === undefined
      ? ''
      : t('tasks.usage.remaining', { count: number(entry.remaining_today) }),
  }, `${taskLabel(entry.kind)} · ${quotaEntryText(entry)}`));
  const cost = moneyMicroText(today.cost_micro);
  if (cost) {
    chips.push(el('span', { class: 'task-usage-chip' }, t('tasks.usage.cost', { amount: cost })));
  }
  return el('section', { class: 'task-usage-panel', 'aria-live': 'polite' },
    el('div', { class: 'task-usage-head' },
      el('strong', {}, t('tasks.usage.title')),
      el('span', { class: 'task-usage-date' }, quota.date || '')),
    el('div', { class: 'task-usage-chips' }, ...chips),
    quotaChips.length ? el('div', { class: 'task-usage-chips' }, ...quotaChips) : null,
    el('p', { class: 'task-usage-note' }, t('tasks.usage.notice')));
}

function truncate(text, limit = 180) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  if (!value) return '';
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function stripMarkdown(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`_~|-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokenText(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const input = Number(usage.prompt_tokens ?? usage.input_tokens ?? 0);
  const output = Number(usage.completion_tokens ?? usage.output_tokens ?? 0);
  const total = Number(usage.total_tokens ?? (input + output));
  const parts = [];
  if (input) parts.push(t('tasks.tokensIn', { count: number(input) }));
  if (output) parts.push(t('tasks.tokensOut', { count: number(output) }));
  if (!input && !output && total) parts.push(t('tasks.tokensTotal', { count: number(total) }));
  return parts.join(' / ');
}

/** 从常见的 result 形状里汇总 token 用量（顶层 usage、calls[]、verification.calls[]）。 */
function usageSummary(result) {
  if (!result || typeof result !== 'object') return '';
  const top = tokenText(result.usage);
  if (top) return t('tasks.tokenLine', { tokens: top });
  const calls = [
    ...(Array.isArray(result.calls) ? result.calls : []),
    ...(Array.isArray(result.verification?.calls) ? result.verification.calls : []),
  ];
  let input = 0;
  let output = 0;
  let found = false;
  for (const call of calls) {
    const usage = call?.usage;
    if (!usage || typeof usage !== 'object') continue;
    found = true;
    input += Number(usage.prompt_tokens ?? usage.input_tokens ?? 0);
    output += Number(usage.completion_tokens ?? usage.output_tokens ?? 0);
  }
  return found ? t('tasks.tokenLineInOut', { input: number(input), output: number(output) }) : '';
}

/* ------------------------------------------------------------------------- *
 * AI 费用估算（只展示估算，不扣减）
 * 口径：任务结果里记录的 token 用量 ×「模型设置 → 模型能力与单价」中用户填写的人民币
 * 单价（元/百万 tokens）。未配置单价的模型显示「单价未配置」，绝不按 0 元/未知单价编造费用。
 * ------------------------------------------------------------------------- */

// 费用口径说明：渲染时再取 t()，切语言后重新渲染即为新语言。
const costEstimateNote = () => t('tasks.cost.note');

function moneyText(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '—';
  if (numeric === 0) return '0.00';
  if (Math.abs(numeric) < 0.01) return numeric.toFixed(4);
  return numeric.toFixed(2);
}

function normalizeUsage(usage) {
  if (!usage || typeof usage !== 'object') return null;
  let input = Number(usage.prompt_tokens ?? usage.input_tokens ?? 0);
  let output = Number(usage.completion_tokens ?? usage.output_tokens ?? 0);
  const total = Number(usage.total_tokens ?? 0);
  if (!Number.isFinite(input)) input = 0;
  if (!Number.isFinite(output)) output = 0;
  if (!input && !output && total) input = Number.isFinite(total) ? total : 0;
  if (!input && !output) return null;
  return { input, output, total: total || input + output };
}

/** 优先取顶层 usage（多数任务只记录一处），否则合计 calls / verification.calls，避免重复计数。 */
function collectUsageEntries(result) {
  if (!result || typeof result !== 'object') return [];
  const models = Array.isArray(result.models) ? result.models : [];
  const top = normalizeUsage(result.usage);
  if (top) {
    return [{
      ...top,
      model: models[0]?.model || '',
      providerId: models[0]?.provider_id ?? null,
      providerName: '',
    }];
  }
  const calls = Array.isArray(result.calls) && result.calls.length
    ? result.calls
    : (Array.isArray(result.verification?.calls) ? result.verification.calls : []);
  const entries = [];
  for (const call of calls) {
    const usage = normalizeUsage(call?.usage);
    if (!usage) continue;
    entries.push({
      ...usage,
      model: call?.model || '',
      providerId: call?.provider_id ?? null,
      providerName: call?.provider_name || '',
    });
  }
  return entries;
}

/** 按连接+模型查设置里填写的单价；同名模型跨连接时优先任务记录的连接。 */
function findModelPrice(providerId, model) {
  const name = String(model || '').trim();
  if (!name) return null;
  const providers = Array.isArray(state.providers) ? state.providers : [];
  const matches = [];
  for (const provider of providers) {
    const details = Array.isArray(provider.model_details) && provider.model_details.length
      ? provider.model_details
      : (provider.models || []).map(modelId => ({ model_id: modelId, input_price: null, output_price: null }));
    const detail = details.find(item => String(item.model_id) === name);
    if (detail) matches.push({ provider, detail });
  }
  if (!matches.length) return null;
  let chosen = null;
  if (providerId !== null && providerId !== undefined) {
    chosen = matches.find(item => String(item.provider.id) === String(providerId)) || null;
  }
  if (!chosen && matches.length === 1) chosen = matches[0];
  if (!chosen) return null;
  const inputPrice = chosen.detail.input_price;
  const outputPrice = chosen.detail.output_price;
  const priced = inputPrice !== null && inputPrice !== undefined
    && outputPrice !== null && outputPrice !== undefined;
  return {
    providerId: chosen.provider.id,
    providerName: chosen.provider.name,
    inputPrice: priced ? Number(inputPrice) : null,
    outputPrice: priced ? Number(outputPrice) : null,
    priced,
  };
}

function estimateJobCost(job) {
  const entries = collectUsageEntries(job?.result);
  if (!entries.length) return null;
  const details = [];
  let totalCost = 0;
  let pricedCount = 0;
  let input = 0;
  let output = 0;
  for (const entry of entries) {
    input += entry.input;
    output += entry.output;
    const price = findModelPrice(entry.providerId, entry.model);
    const priced = Boolean(price && price.priced);
    const cost = priced
      ? (entry.input / 1_000_000) * price.inputPrice + (entry.output / 1_000_000) * price.outputPrice
      : null;
    if (priced) {
      totalCost += cost;
      pricedCount += 1;
    }
    details.push({ ...entry, price, cost, priced });
  }
  return {
    details,
    input,
    output,
    pricedCount,
    unpricedCount: details.length - pricedCount,
    totalCost: pricedCount ? totalCost : null,
  };
}

function costBadge(cost) {
  const text = cost.totalCost === null
    ? t('tasks.cost.unknown', { count: number(cost.input + cost.output) })
    : t('tasks.cost.estimate', { amount: moneyText(cost.totalCost) })
      + (cost.unpricedCount ? t('tasks.cost.unpricedModels', { count: cost.unpricedCount }) : '');
  return el('p', {
    class: 'task-cost',
    'data-cost-known': String(cost.totalCost !== null),
    title: costEstimateNote(),
  }, text);
}

function costDetailSection(job) {
  const cost = estimateJobCost(job);
  if (!cost) {
    return detailSection(t('tasks.cost.section'), el('p', { class: 'task-detail-note' },
      t('tasks.cost.noUsage')));
  }
  const head = el('tr', {},
    el('th', {}, t('tasks.cost.headModel')), el('th', {}, t('tasks.cost.headInput')), el('th', {}, t('tasks.cost.headOutput')),
    el('th', {}, t('tasks.cost.headPrice')), el('th', {}, t('tasks.cost.headCost')));
  const rows = cost.details.map(entry => el('tr', {},
    el('td', {},
      el('strong', {}, entry.model || t('tasks.unknownModel')),
      (entry.providerName || entry.price?.providerName)
        ? el('div', { class: 'task-cost-connect' }, entry.providerName || entry.price.providerName)
        : null),
    el('td', {}, number(entry.input)),
    el('td', {}, number(entry.output)),
    el('td', {}, entry.priced && entry.price
      ? `¥${moneyText(entry.price.inputPrice)} / ¥${moneyText(entry.price.outputPrice)}`
      : t('tasks.priceUnset')),
    el('td', { class: 'task-cost-cell' }, entry.priced
      ? `¥ ${moneyText(entry.cost)}`
      : t('tasks.priceUnset'))));
  const table = el('table', { class: 'task-cost-table' },
    el('thead', {}, head), el('tbody', {}, ...rows));
  const summary = el('p', { class: 'task-cost-summary' },
    t('tasks.cost.summary', { input: number(cost.input), output: number(cost.output) }),
    cost.totalCost === null
      ? t('tasks.cost.summaryUnpriced')
      : t('tasks.cost.summaryEstimate', { amount: moneyText(cost.totalCost) })
        + (cost.unpricedCount ? t('tasks.cost.summaryExtra', { count: cost.unpricedCount }) : ''));
  return detailSection(t('tasks.cost.section'), el('div', { class: 'task-cost-detail' },
    el('div', { class: 'table-wrap' }, table),
    summary,
    el('p', { class: 'task-detail-note' }, costEstimateNote())));
}

function resultSummary(job, limit = 180) {
  const result = job?.result;
  if (!result || typeof result !== 'object') return '';
  for (const value of [result.markdown, result.summary, result.content, result.message, result.outline]) {
    if (typeof value === 'string' && value.trim()) return truncate(stripMarkdown(value), limit);
  }
  if (result.reply?.text) return truncate(stripMarkdown(result.reply.text), limit);
  if (result.note_id) return t('tasks.result.note', { id: result.note_id });
  if (result.course_id) return t('tasks.result.course', { id: result.course_id });
  if (Array.isArray(result.items) && result.items.length) return t('tasks.result.items', { count: number(result.items.length) });
  if (Array.isArray(result.question_ids) && result.question_ids.length) {
    return t('tasks.result.questions', { count: number(result.question_ids.length) });
  }
  return '';
}

function resultModels(job) {
  const rows = [];
  for (const model of job?.result?.models || []) {
    rows.push([t('tasks.modelN', { n: rows.length + 1 }), t('tasks.modelWithProvider', { model: model.model || '—', id: model.provider_id ?? '—' })]);
  }
  const calls = [
    ...(job?.result?.calls || []),
    ...(job?.result?.verification?.calls || []),
  ];
  calls.forEach((call, index) => {
    if (!call || typeof call !== 'object') return;
    const usage = tokenText(call.usage);
    const parts = [call.provider_name || '', call.model || ''].filter(Boolean).join(' / ');
    rows.push([t('tasks.callN', { n: index + 1 }), [parts || t('tasks.modelCall'), usage ? t('tasks.tokenLine', { tokens: usage }) : null].filter(Boolean).join(' · ')]);
  });
  return rows;
}

function formatValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? t('common.yes') : t('common.no');
  if (Array.isArray(value)) return value.map(item => formatValue(item)).join(t('common.listSeparator'));
  if (typeof value === 'object') {
    try {
      return truncate(JSON.stringify(value, null, 0), 220);
    } catch {
      return t('tasks.complexValue');
    }
  }
  return truncate(String(value), 220);
}

function safeJson(value, limit = 4000) {
  if (value === null || value === undefined) return '';
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > limit ? `${text.slice(0, limit)}\n${t('tasks.truncated')}` : text;
  } catch {
    return String(value);
  }
}

/** 任务结果对应的页面入口；未知任务返回 null（只显示结果摘要，不报错）。 */
function resultRoute(job) {
  const task = job.task;
  const request = job.request || {};
  const result = job.result || {};
  switch (task) {
    case 'notes':
      return { route: 'notes', before: () => { state.selectedNote = result.note_id || null; } };
    case 'explain':
    case 'question-analysis': {
      const qid = request.qid || result.qid;
      return qid ? { route: 'practice', qid } : null;
    }
    case 'paper-import':
    case 'paper-enrich':
    case 'paper-compose':
    case 'paper-revision':
      return { route: 'paperlab' };
    case 'course':
      return { route: 'classroom' };
    case 'insight-report':
      return { route: 'insight' };
    case 'assistant-turn':
      return { route: 'assistant' };
    case 'recognize': {
      // 识别任务完成后回到原拍题草稿（upload_id 深链），而不是只打开空白拍题页。
      const uploadId = request.upload_id || result.upload_id;
      return uploadId
        ? { route: 'upload', params: { upload: uploadId } }
        : { route: 'upload' };
    }
    case 'ocr':
      return { route: 'upload' };
    case 'tagging':
      return { route: 'admin' };
    default:
      return null;
  }
}

function openResult(job) {
  const target = resultRoute(job);
  if (!target) {
    toast(t('tasks.noRouteToast'), true);
    return;
  }
  try {
    if (target.before) target.before();
  } catch {
    /* 跳转前状态设置失败不应阻断打开页面 */
  }
  go(target.route, target.qid || null, target.params || null);
}

function errorPanel(message, retry) {
  return el('div', { class: 'task-error-panel' },
    el('span', {}, message || t('tasks.loadFailed')),
    button(t('common.reload'), retry, 'secondary small', 'repeat'));
}

function detailSection(title, content) {
  return el('div', {}, el('h3', {}, title), content);
}

function kvList(entries) {
  const dl = el('dl', { class: 'task-kv' });
  for (const [key, value] of entries) {
    dl.append(el('dt', {}, String(key)), el('dd', {}, String(value)));
  }
  return dl;
}

function paramsEntries(request) {
  const entries = [];
  const params = request?.params && typeof request.params === 'object' ? request.params : {};
  for (const [key, value] of Object.entries(params)) entries.push([key, formatValue(value)]);
  for (const [key, label] of [
    ['qid', t('tasks.param.qid')], ['upload_id', t('tasks.param.uploadId')], ['document_id', t('tasks.param.documentId')],
    ['subject', t('common.subject')], ['provider_id', t('tasks.param.providerId')], ['model', t('tasks.param.model')],
    ['cross_verify', t('tasks.param.crossVerify')], ['requirements', t('tasks.param.requirements')],
  ]) {
    if (request?.[key] !== undefined && request?.[key] !== null && request?.[key] !== false) {
      entries.push([label, formatValue(request[key])]);
    }
  }
  return entries;
}

/** 任务详情「运行信息」：模型、耗时、重试次数等可追踪字段（服务端已随任务返回）。 */
function detailRuntimeEntries(job) {
  const entries = [
    [t('tasks.usage.runtimeTask'), taskLabel(job.task)],
    [t('tasks.usage.runtimeStatus'), statusLabel(job.status)],
  ];
  if (job.request?.model) entries.push([t('tasks.usage.runtimeModel'), job.request.model]);
  if (job.started_at) entries.push([t('tasks.usage.runtimeStarted'), dateText(job.started_at)]);
  if (job.finished_at) entries.push([t('tasks.usage.runtimeFinished'), dateText(job.finished_at)]);
  const duration = jobDurationMs(job);
  if (duration !== null) entries.push([t('tasks.usage.runtimeDuration'), durationText(duration)]);
  entries.push([t('tasks.usage.runtimeRetries'), String(retriesOf(job))]);
  return entries;
}

function jobRow(job, context) {
  const status = String(job.status || '');
  const active = ACTIVE_STATUSES.has(status);
  const card = el('article', {
    class: 'task-card',
    'data-job-id': String(job.id),
    'data-status': status,
    'data-active': active ? 'true' : 'false',
  }, el('div', { class: 'task-card-head' },
    el('span', { class: 'task-task-name' }, taskLabel(job.task)),
    el('span', { class: 'task-task-id' }, `#${job.id}`),
    badge(statusLabel(status), statusBadgeVariant(status)),
    job.needs_attention ? badge(t('tasks.needsAttention'), 'amber') : null));

  const durationMs = jobDurationMs(job);
  const meta = el('p', { class: 'task-meta' },
    el('span', {}, t('tasks.meta.stage', { stage: stageLabel(job.stage) })),
    job.request?.model ? el('span', {}, t('tasks.meta.model', { model: job.request.model })) : null,
    job.created_at ? el('span', {}, t('tasks.meta.created', { time: dateText(job.created_at) })) : null,
    job.started_at ? el('span', {}, t('tasks.usage.metaStarted', { time: dateText(job.started_at) })) : null,
    job.finished_at ? el('span', {}, t('tasks.meta.finished', { time: dateText(job.finished_at) })) : null,
    durationMs !== null ? el('span', { class: 'task-duration' }, t('tasks.usage.metaDuration', { duration: durationText(durationMs) })) : null,
    retriesOf(job) ? el('span', { class: 'task-retries' }, t('tasks.usage.metaRetries', { count: number(retriesOf(job)) })) : null);
  card.append(meta);

  const progress = parseStageProgress(job.stage);
  if (active) {
    card.append(el('div', {
      class: 'task-progress',
      role: 'progressbar',
      'aria-label': t('tasks.progressAria'),
      'aria-valuemin': '0',
      'aria-valuemax': '100',
      'aria-valuenow': progress ? String(progress.percent) : '0',
    }, el('span', { style: progress ? `width:${progress.percent}%` : 'width:30%' })));
    if (progress) card.append(el('p', { class: 'task-usage' }, t('tasks.progress', { percent: progress.percent, current: progress.current, total: progress.total })));
  }

  const summary = resultSummary(job);
  if (summary) card.append(el('p', { class: 'task-summary' }, t('tasks.meta.result', { summary })));
  if (job.error) card.append(el('p', { class: 'task-error-line' }, t('tasks.meta.error', { message: truncate(job.error, 240) })));
  const usage = usageSummary(job.result);
  if (usage) card.append(el('p', { class: 'task-usage' }, usage));
  const cost = estimateJobCost(job);
  if (cost) card.append(costBadge(cost));

  const actions = el('div', { class: 'task-actions' });
  if (['queued', 'running', 'cancel_requested'].includes(status)) {
    const cancelling = status === 'cancel_requested';
    actions.append(button(cancelling ? t('tasks.cancelling') : t('tasks.cancelTask'), event => busy(event.currentTarget, async () => {
      await api(`/ai/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' });
      toast(t('tasks.cancelSent'));
      await context.reload({ silent: true });
    }), 'danger small', 'stop', { disabled: cancelling || undefined }));
  }
  if (RESUME_STATUSES.has(status)) {
    actions.append(button(job.has_checkpoint ? t('tasks.resume') : t('tasks.rerun'), event => busy(event.currentTarget, async () => {
      const resumed = await api(`/ai/jobs/${encodeURIComponent(job.id)}/resume`, { method: 'POST' });
      toast(t('tasks.requeued', { id: resumed.id }));
      await context.reload({ silent: true });
    }), 'soft small', 'repeat'));
  }
  const route = resultRoute(job);
  if (route && (status === 'completed' || status === 'succeeded')) {
    actions.append(button(t('tasks.openResult'), () => openResult(job), 'secondary small', 'chevron'));
  }

  const detailHost = el('div', { class: 'task-detail-host' });
  actions.append(button(t('tasks.detail'), () => context.openDetail(job, card, detailHost), 'ghost small', 'history'));
  card.append(actions, detailHost);
  return card;
}

export async function renderTasks(container, version, params) {
  let jobs = [];
  let filterId = 'all';
  let taskFilter = state.tasksTaskFilter || (params?.get ? params.get('task') || '' : '');

  const headingNode = heading(
    'TASK CENTER', t('tasks.heading'),
    t('tasks.subheading'),
    button(t('tasks.refresh'), () => loadJobs(), 'secondary', 'repeat'));

  const filtersRow = el('div', { class: 'task-filters', role: 'group', 'aria-label': t('tasks.filterAria') });
  const taskFilterField = el('div', { class: 'task-task-filter' });
  const refreshNote = el('p', { class: 'task-refresh-note', 'aria-live': 'polite' });
  const toolbar = el('div', { class: 'task-toolbar' }, filtersRow, taskFilterField);
  // 今日用量 / 剩余配额：接口未就绪（404/501）时整块隐藏，任务列表照常工作。
  const usageHost = el('div', { class: 'task-usage-host' });
  usageHost.hidden = true;
  const listHost = el('div', { class: 'task-list' });
  const page = el('div', { class: 'task-page' }, headingNode, toolbar, refreshNote, usageHost, listHost);
  container.replaceChildren(page);

  let usageData = null;

  async function loadUsage() {
    try {
      usageData = await api('/ai/usage/me?days=1');
    } catch {
      usageData = null;
    }
    if (!current(version) || !container.isConnected) return;
    if (!usageData) {
      usageHost.hidden = true;
      usageHost.replaceChildren();
      return;
    }
    usageHost.hidden = false;
    usageHost.replaceChildren(todayUsagePanel(usageData));
  }

  function availableTasks() {
    const set = new Set(jobs.map(job => String(job.task || '')).filter(Boolean));
    if (taskFilter) set.add(taskFilter);
    return [...set].sort((a, b) => taskLabel(a).localeCompare(taskLabel(b), getLocale()));
  }

  function renderTaskOptions() {
    const options = [{ value: '', label: t('tasks.allTasks') }];
    for (const task of availableTasks()) {
      const known = Boolean(taskNames[task] || t(`tasks.task.${task}`) !== `tasks.task.${task}`);
      options.push({
        value: task,
        label: known ? taskLabel(task) : t('tasks.unknownTaskType', { name: task }),
      });
    }
    const next = select(options, taskFilter);
    next.setAttribute('aria-label', t('tasks.taskFilterAria'));
    next.addEventListener('change', () => {
      taskFilter = next.value;
      state.tasksTaskFilter = taskFilter;
      renderAll();
    });
    taskFilterField.replaceChildren(field(t('tasks.taskType'), next));
  }

  function renderFilters() {
    filtersRow.replaceChildren(...STATUS_FILTERS.map(item => {
      const count = jobs.filter(job => item.match(String(job.status || ''))).length;
      return el('button', {
        type: 'button',
        class: 'task-filter',
        'aria-pressed': String(filterId === item.id),
        onClick: () => {
          filterId = item.id;
          renderAll();
        },
      }, t(item.labelKey), el('span', { class: 'task-filter-count' }, String(count)));
    }));
  }

  function filteredJobs() {
    const match = statusFilter(filterId).match;
    return jobs.filter(job => {
      if (taskFilter && String(job.task || '') !== taskFilter) return false;
      return match(String(job.status || ''));
    });
  }

  const context = {
    reload: options => loadJobs(options),
    openDetail,
  };

  function renderList() {
    const rows = filteredJobs();
    if (!rows.length) {
      if (!jobs.length) {
        listHost.replaceChildren(empty(
          t('tasks.emptyTitle'),
          t('tasks.emptyText'),
          el('div', { class: 'task-empty-actions' },
            button(t('tasks.goUpload'), () => go('upload'), '', 'scan'),
            button(t('tasks.goInsight'), () => go('insight'), 'secondary', 'chart'),
            button(t('tasks.goAssistant'), () => go('assistant'), 'secondary', 'message')),
          'history'));
      } else {
        listHost.replaceChildren(el('div', { class: 'empty' },
          el('h3', {}, t('tasks.emptyFilteredTitle')),
          el('p', {}, t('tasks.emptyFilteredText'))));
      }
      return;
    }
    listHost.replaceChildren(...rows.map(job => jobRow(job, context)));
  }

  function renderAll() {
    renderFilters();
    renderTaskOptions();
    renderList();
    schedulePoll();
  }

  function hasRunning() {
    return jobs.some(job => ACTIVE_STATUSES.has(String(job.status || '')));
  }

  function schedulePoll() {
    stopPolling();
    if (!hasRunning()) {
      refreshNote.textContent = jobs.length ? t('tasks.refreshIdle') : '';
      return;
    }
    refreshNote.textContent = t('tasks.refreshRunning');
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (!current(version) || !container.isConnected) return;
      await loadJobs({ silent: true });
    }, 3000);
  }

  function hasOpenDetail() {
    return Boolean(listHost.querySelector('.task-detail-host[data-open="1"]'));
  }

  async function loadJobs(options = {}) {
    const silent = Boolean(options.silent);
    if (!silent) listHost.replaceChildren(loading(t('tasks.loadingList')));
    try {
      const data = await api('/ai/jobs');
      if (!current(version) || !container.isConnected) return;
      jobs = Array.isArray(data.items) ? data.items : [];
      if (!silent) loadUsage();
      // 自动刷新时不重建列表：避免把用户正在展开的详情/实时进度卡冲掉。
      if (silent && hasOpenDetail()) {
        renderFilters();
        renderTaskOptions();
        schedulePoll();
      } else {
        renderAll();
      }
    } catch (error) {
      if (!current(version) || !container.isConnected) return;
      stopPolling();
      if (silent) {
        refreshNote.textContent = t('tasks.autoRefreshFailed', { message: error.message });
      } else {
        listHost.replaceChildren(errorPanel(error.message, () => loadJobs()));
      }
    }
  }

  async function openDetail(job, card, detailHost) {
    // 已展开则收起
    if (detailHost.dataset.open === '1') {
      detailHost.dataset.open = '0';
      detailHost.replaceChildren();
      return;
    }
    detailHost.dataset.open = '1';
    detailHost.replaceChildren(loading(t('tasks.loadingDetail')));
    let detail;
    try {
      detail = await api(`/ai/jobs/${encodeURIComponent(job.id)}`);
    } catch (error) {
      if (detailHost.isConnected) detailHost.replaceChildren(notice(error.message, 'error'));
      return;
    }
    if (!detailHost.isConnected || !current(version)) return;

    const body = el('div', { class: 'task-detail' });
    body.append(el('h3', {}, t('tasks.detailTitle', { id: detail.id })));

    const requestEntries = paramsEntries(detail.request || {});
    if (requestEntries.length) body.append(detailSection(t('tasks.requestParams'), kvList(requestEntries)));

    const runtimeEntries = detailRuntimeEntries(detail);
    if (runtimeEntries.length) body.append(detailSection(t('tasks.usage.runtimeTitle'), kvList(runtimeEntries)));

    const modelRows = resultModels(detail);
    if (modelRows.length) body.append(detailSection(t('tasks.modelCalls'), kvList(modelRows)));
    body.append(costDetailSection(detail));

    if (detail.result) {
      body.append(detailSection(t('tasks.resultPreview'),
        el('pre', { class: 'task-pre' }, safeJson(detail.result))));
    }
    if (detail.error) body.append(el('p', { class: 'task-error-line' }, t('tasks.meta.error', { message: detail.error })));
    if (detail.needs_attention) {
      body.append(notice(t('tasks.resumable'), 'warning'));
    }

    if (ACTIVE_STATUSES.has(String(detail.status || '')) || detail.status === 'awaiting_outline') {
      body.append(detailSection(t('tasks.liveProgress'), el('p', { class: 'task-detail-note' },
        detail.status === 'awaiting_outline'
          ? t('tasks.awaitingOutline')
          : t('tasks.liveStreamNote'))));
      const live = el('div', { class: 'task-detail-live' });
      body.append(live);
      detailHost.replaceChildren(body);
      mountJob(live, detail.id, () => loadJobs({ silent: true }), { task: detail.task });
      return;
    }
    if (!detail.result && !detail.error) {
      body.append(el('p', { class: 'task-detail-note' }, t('tasks.noResult')));
    }
    if (!resultRoute(detail)) {
      body.append(el('p', { class: 'task-detail-note' },
        t('tasks.noRoute', { task: detail.task })));
    }
    detailHost.replaceChildren(body);
  }

  // 初始骨架：先渲染筛选（计数为 0），loadJobs 完成后再按真实数据重渲染。
  renderFilters();
  renderTaskOptions();
  listHost.replaceChildren(loading(t('tasks.loadingList')));
  await loadJobs();
  await loadUsage();
}
