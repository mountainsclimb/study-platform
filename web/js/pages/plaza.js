/**
 * 广场公开只读页（Phase 2，访客与登录用户共用；Phase 3 接入收藏/纠错/举报）。
 *
 * 路由：`#/plaza`（列表）与 `#/plaza?id=<public_id>`（详情）。
 * - 匿名可浏览列表与详情，答案/解析在详情页由用户点击「展开」后展示；
 * - 社区待核验（community_review）显著标注「社区待核验」并显示评审进度摘要；
 * - 撤回（withdrawn）/不可见内容显示清晰状态而不是静默 404；
 * - 配图只使用 payload 中明确给出的公开 URL（figure_urls / images[].url），
 *   缺失时优先展示 payload 的 figure_notice，再退占位，绝不拼接私有存储路径；
 * - 登录用户可收藏/取消收藏、纠错与举报（WS21 接口，成功后 toast 并刷新状态）；
 *   匿名点击这些动作跳转登录页（hash 保持，登录后回到原公开题）；
 * - 收藏状态优先读详情 payload 的 favorited（匿名 false），缺失时探测 GET /favorites。
 */
import { api, isAborted } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, button, badge, empty, heading, loading, notice, select, input, field, icon,
  busy, query, number, toast,
} from '../core/dom.js';
import { go, current, setHashParams } from '../core/router.js';
import { t } from '../core/i18n.js';
import { markdown } from '../core/markdown.js';
import {
  plazaCard, plazaSourceDeclaration, plazaStatusMeta, plazaTypeLabel,
} from '../components/plazaCard.js';

const PAGE_SIZE = 12;

// 学科筛选：公开题库以数学/物理为主，这里给出常用学科；未列出的学科仍可用关键词检索。
const SUBJECT_OPTIONS = ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治'];

// 题型筛选值 = paperlab schema 归一化后的枚举（后端 normalize_qtype 只接受这些别名）。
const TYPE_VALUES = ['SINGLE', 'MULTIPLE', 'BLANK', 'SUBJECTIVE', 'MIXED'];

function typeOptions() {
  return [
    { value: '', label: t('plaza.filter.typeAll') },
    ...TYPE_VALUES.map(value => ({ value, label: plazaTypeLabel(value) })),
  ];
}

function subjectOptions() {
  return [
    { value: '', label: t('plaza.filter.subjectAll') },
    ...SUBJECT_OPTIONS.map(subject => ({ value: subject, label: subject })),
  ];
}

function sourceOptions() {
  return [
    { value: '', label: t('plaza.filter.sourceAll') },
    { value: 'community', label: t('plaza.filter.sourceCommunity') },
    { value: 'featured', label: t('plaza.filter.sourceFeatured') },
  ];
}

/** 从 URLSearchParams 读取列表/详情参数（params 由 router.parseHash 传入）。 */
function readParams(params) {
  const get = name => {
    if (!params) return '';
    if (typeof params.get === 'function') return params.get(name) || '';
    return params[name] || '';
  };
  const page = Number.parseInt(get('page'), 10);
  return {
    id: String(get('id') || '').trim(),
    subject: String(get('subject') || '').trim(),
    type: String(get('type') || '').trim(),
    source: String(get('source') || '').trim(),
    q: String(get('q') || '').trim(),
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

/** 富文本：复用安全的 markdown 渲染（支持 $行内公式$；DOMPurify 缺失时自动退纯文本）。 */
function richText(value, className = '') {
  const raw = String(value ?? '').trim();
  if (!raw) return null;
  const node = markdown(raw);
  if (className) node.classList.add(className);
  return node;
}

function difficultyText(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return '';
  return t('plaza.detail.difficulty', { percent: Math.round(Math.max(0, Math.min(1, numeric)) * 100) });
}

function backToList(extraClass = '') {
  return el('div', { class: `plaza-back ${extraClass}`.trim() },
    button(t('plaza.detail.backToList'), () => go('plaza'), 'ghost small', 'chevron'));
}

function pager(page, pages, onPage) {
  return el('div', { class: 'pagination plaza-pager' },
    button(t('common.prevPage'), () => onPage(page - 1), 'secondary small', null, { disabled: page <= 1 }),
    el('span', {}, t('common.pageOf', { page, pages })),
    button(t('common.nextPage'), () => onPage(page + 1), 'secondary small', null, { disabled: page >= pages }));
}

/* ------------------------------------------------------------------------- *
 * 列表
 * ------------------------------------------------------------------------- */

export async function renderPlaza(container, version, params) {
  const initial = readParams(params);
  if (initial.id) return renderPlazaDetail(container, version, initial.id);
  return renderPlazaList(container, version, initial);
}

async function renderPlazaList(container, version, initial) {
  const filters = { subject: initial.subject, type: initial.type, source: initial.source, q: initial.q };
  let page = initial.page;

  const keyword = input(t('plaza.filter.keywordPlaceholder'), {
    value: filters.q,
    type: 'search',
    autocomplete: 'off',
    'aria-label': t('plaza.filter.keywordPlaceholder'),
  });
  const subject = select(subjectOptions(), filters.subject, { 'aria-label': t('plaza.filter.subject') });
  const type = select(typeOptions(), filters.type, { 'aria-label': t('plaza.filter.type') });
  const source = select(sourceOptions(), filters.source, { 'aria-label': t('plaza.filter.source') });
  const summary = el('span', { class: 'plaza-list-summary', role: 'status', 'aria-live': 'polite' });
  const list = el('div', { class: 'plaza-list', 'aria-live': 'polite' });
  let seq = 0;

  function syncHash() {
    setHashParams('plaza', {
      subject: filters.subject,
      type: filters.type,
      source: filters.source,
      q: filters.q,
      page: page > 1 ? page : '',
    }, true);
  }

  function readForm() {
    filters.subject = subject.value;
    filters.type = type.value;
    filters.source = source.value;
    filters.q = keyword.value.trim();
  }

  function applyFilters({ reset = false } = {}) {
    readForm();
    if (reset) {
      filters.subject = '';
      filters.type = '';
      filters.source = '';
      filters.q = '';
      subject.value = '';
      type.value = '';
      source.value = '';
      keyword.value = '';
    }
    page = 1;
    load();
  }

  function goPage(nextPage) {
    page = Math.max(1, nextPage);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    load();
  }

  async function load() {
    const mine = ++seq;
    syncHash();
    list.replaceChildren(loading(t('plaza.list.loading')));
    try {
      const data = await api(`/plaza/questions?${query({
        subject: filters.subject,
        type: filters.type,
        source: filters.source,
        keyword: filters.q,
        page,
        page_size: PAGE_SIZE,
      })}`, { latest: true, latestKey: 'plaza-list' });
      if (mine !== seq || !current(version)) return;
      const items = Array.isArray(data.items) ? data.items : [];
      const total = Number(data.total) || items.length;
      const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      if (page > pages && items.length === 0) {
        // 筛选后页码越界：回到第一页，避免空态掩盖仍有结果。
        page = 1;
        return load();
      }
      summary.textContent = total
        ? t('plaza.list.count', { count: number(total), page, pages })
        : t('plaza.list.emptySummary');
      list.replaceChildren();
      if (!items.length) {
        list.append(el('div', { class: 'card' }, empty(
          t('plaza.list.emptyTitle'),
          t('plaza.list.emptyText'),
          filters.q || filters.subject || filters.type || filters.source
            ? button(t('plaza.filter.reset'), () => applyFilters({ reset: true }), 'soft small')
            : null,
          'layers')));
        return;
      }
      const grid = el('div', { class: 'plaza-grid' });
      for (const item of items) {
        const card = plazaCard(item, {
          keyword: filters.q,
          onOpen: () => go('plaza', null, { id: item.public_id }),
        });
        if (card) grid.append(card);
      }
      list.append(grid);
      if (pages > 1) list.append(pager(page, pages, goPage));
    } catch (error) {
      if (mine !== seq || isAborted(error)) return;
      list.replaceChildren(el('div', { class: 'error-panel' },
        t('plaza.list.loadFailed', { message: error.message }),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.retry'), () => load(), 'secondary small', 'repeat'))));
    }
  }

  const searchButton = button(t('plaza.filter.search'), () => applyFilters(), '', 'search');
  keyword.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      applyFilters();
    }
  });

  const pageNode = el('div', { class: 'plaza-page', 'data-plaza-view': 'list' },
    heading(t('plaza.list.eyebrow'), t('plaza.list.heading'), t('plaza.list.sub')),
    el('section', { class: 'card plaza-filter-card', 'aria-label': t('plaza.filter.aria') },
      el('div', { class: 'search-line plaza-search-line' },
        keyword,
        searchButton),
      el('div', { class: 'filter-grid plaza-filter-grid' },
        el('div', { class: 'field' }, el('label', {}, t('plaza.filter.subject')), subject),
        el('div', { class: 'field' }, el('label', {}, t('plaza.filter.type')), type),
        el('div', { class: 'field' }, el('label', {}, t('plaza.filter.source')), source),
        el('div', { class: 'field plaza-filter-reset' },
          button(t('plaza.filter.reset'), () => applyFilters({ reset: true }), 'secondary small', 'rotate')))),
    el('div', { class: 'plaza-list-head' }, summary,
      el('span', { class: 'plaza-list-hint' }, t('plaza.list.guestHint'))),
    list);

  container.replaceChildren(pageNode);
  await load();
}

/* ------------------------------------------------------------------------- *
 * 详情
 * ------------------------------------------------------------------------- */

async function renderPlazaDetail(container, version, publicId) {
  container.replaceChildren(loading(t('plaza.detail.loading')));
  let data;
  try {
    data = await api(`/plaza/questions/${encodeURIComponent(publicId)}`);
  } catch (error) {
    if (!current(version)) return;
    container.replaceChildren(el('div', { class: 'plaza-page', 'data-plaza-view': 'detail' },
      backToList(),
      el('div', { class: 'error-panel' },
        el('strong', {}, t('plaza.detail.loadFailed')),
        el('p', {}, error.message),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.retry'), () => renderPlazaDetail(container, version, publicId), 'secondary small', 'repeat')))));
    return;
  }
  if (!current(version)) return;
  container.replaceChildren(plazaDetailNode(data));
}

function statusBadges(data) {
  const status = plazaStatusMeta(data);
  const head = [data.subject, data.type ? plazaTypeLabel(data.type) : '', data.grade].filter(Boolean).join(' · ');
  return {
    head,
    node: el('div', { class: 'plaza-card-badges' },
      badge(status.label, status.tone),
      String(data.source || '') === 'featured' || status.key === 'featured'
        ? null
        : badge(t('plaza.source.community'), 'teal')),
  };
}

/** 撤回 / 争议等不可见内容：只显示状态与后端 notice，不回显受限正文。 */
function hiddenContentNode(data) {
  const status = plazaStatusMeta(data);
  const message = String(data.notice || '').trim()
    || (status.key === 'withdrawn' ? t('plaza.detail.withdrawnNotice') : t('plaza.detail.hiddenNotice'));
  return el('div', { class: 'plaza-page', 'data-plaza-view': 'detail' },
    backToList(),
    el('section', { class: 'card plaza-detail-head', 'data-plaza-hidden': 'true' },
      badge(status.label, status.tone),
      el('h1', { class: 'plaza-detail-title' }, t('plaza.detail.unavailableTitle')),
      notice(message, 'warning')));
}

function reviewSummaryNode(data) {
  const review = data.review && typeof data.review === 'object' ? data.review : {};
  const reviews = Number(review.independent_reviews) || 0;
  const approves = Number(review.approves) || 0;
  const subjectApproves = Number(review.subject_reviewer_approves) || 0;
  const status = plazaStatusMeta(data);
  if (!reviews && status.key !== 'pending' && status.key !== 'verified') return null;
  return el('div', { class: 'plaza-review-summary' },
    el('span', { class: 'plaza-review-title' }, t('plaza.detail.reviewTitle')),
    el('span', { class: 'plaza-review-counts' },
      t('plaza.detail.reviewCounts', { reviews, approves, subject: subjectApproves })),
    review.threshold_met === true
      ? badge(t('plaza.detail.reviewThresholdMet'), 'teal')
      : badge(t('plaza.detail.reviewThresholdPending'), 'amber'));
}

function optionsNode(options, className = '') {
  if (!Array.isArray(options) || !options.length) return null;
  return el('div', { class: `options plaza-options ${className}`.trim() },
    ...options.map(option => {
      const label = String(option?.label || '').trim();
      const text = String(option?.text ?? option?.content ?? '').trim();
      return el('div', { class: 'option plaza-option' },
        label ? el('span', { class: 'option-label', 'data-no-i18n': '' }, label) : null,
        el('div', { class: 'option-content' }, richText(text) || document.createTextNode('')));
    }));
}

function answersNode(answers) {
  const list = (Array.isArray(answers) ? answers : [])
    .map(value => String(value ?? '').trim())
    .filter(Boolean);
  if (!list.length) return null;
  return el('ul', { class: 'plaza-answers' },
    ...list.map(value => el('li', { class: 'plaza-answer-item', 'data-no-i18n': '' }, value)));
}

function partsNode(parts) {
  if (!Array.isArray(parts) || !parts.length) return null;
  return el('div', { class: 'plaza-parts' },
    ...parts.map((part, index) => {
      const partIndex = part && part.index !== undefined && part.index !== null ? part.index : index + 1;
      const typeLabel = part && part.type ? plazaTypeLabel(part.type) : '';
      return el('section', { class: 'plaza-part' },
        el('div', { class: 'plaza-part-head' },
          el('strong', {}, t('plaza.detail.part', { index: number(partIndex) })),
          typeLabel ? badge(typeLabel) : null),
        richText(part?.stem || '') || null,
        optionsNode(part?.options || []));
    }));
}

/* ------------------------------------------------------------------------- *
 * 配图（能力探测：优先 payload 的公开 URL；缺失时 figure_notice / 占位）
 * ------------------------------------------------------------------------- */

const FIGURE_NOTICE_KEYS = ['figure_notice'];

function figureUrlOf(entry) {
  if (typeof entry === 'string') return entry.trim();
  if (!entry || typeof entry !== 'object') return '';
  return String(entry.url || entry.src || entry.href || '').trim();
}

/** 允许站内相对路径与 http(s) 链接，其它值不渲染 <img>，避免把任意字符串当地址加载。 */
function isRenderableImageUrl(url) {
  const value = String(url || '').trim();
  return Boolean(value) && (value.startsWith('/') ? !value.startsWith('//') : /^https?:\/\//i.test(value));
}

/**
 * 收集公开配图（WS14 契约：content.images[].url 为匿名可读公开图地址）：
 * 1. payload.figure_urls（数组或对象，元素可为 URL 字符串或 {url}）；
 * 2. payload.images[]（元素可为 URL 字符串或 {url}）；
 * 3. content.images[]（同上）。
 * 兼容旧 payload / 边界情况：条目只有 asset_id 且没有 figure_notice 时，按契约的
 * GET /api/plaza/questions/{public_id}/images/{idx} 能力探测；加载失败退占位。
 * 只返回带可渲染 URL 的条目。
 */
function collectFigures(data) {
  const figures = [];
  const seen = new Set();
  const push = entry => {
    const url = figureUrlOf(entry);
    if (!isRenderableImageUrl(url) || seen.has(url)) return;
    seen.add(url);
    figures.push({ url, index: figures.length + 1 });
  };
  const publicId = String(data.public_id || '').trim();
  const figureNotice = figureNoticeOf(data);
  const pushWithAssetFallback = (entry, position) => {
    const url = figureUrlOf(entry);
    if (isRenderableImageUrl(url)) {
      push(entry);
      return;
    }
    if (!entry || typeof entry !== 'object' || figureNotice) return;
    const assetId = String(entry.asset_id || '').trim();
    if (!assetId || !publicId) return;
    const rawIndex = entry.index ?? position;
    const index = Number.isFinite(Number(rawIndex)) ? Number(rawIndex) : position;
    const fallback = `/api/plaza/questions/${encodeURIComponent(publicId)}/images/${encodeURIComponent(index)}`;
    if (seen.has(fallback)) return;
    seen.add(fallback);
    figures.push({ url: fallback, index: figures.length + 1 });
  };
  const figureUrls = data.figure_urls;
  if (Array.isArray(figureUrls)) figureUrls.forEach(entry => push(entry));
  else if (figureUrls && typeof figureUrls === 'object') Object.values(figureUrls).forEach(entry => push(entry));
  if (Array.isArray(data.images)) data.images.forEach((entry, position) => pushWithAssetFallback(entry, position));
  const contentImages = data.content && Array.isArray(data.content.images) ? data.content.images : [];
  contentImages.forEach((entry, position) => pushWithAssetFallback(entry, position));
  return figures;
}

function figureNoticeOf(data) {
  for (const key of FIGURE_NOTICE_KEYS) {
    const value = data[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function rawFigureCount(data) {
  const counts = [data.images, data.figure_urls, data.content && data.content.images]
    .filter(Array.isArray)
    .map(value => value.length);
  return Math.max(0, ...counts);
}

function figureGalleryEntry(entry) {
  const alt = t('plaza.detail.figureAlt', { index: entry.index });
  const image = el('img', {
    class: 'plaza-figure',
    src: entry.url,
    alt,
    loading: 'lazy',
    decoding: 'async',
  });
  image.addEventListener('error', () => {
    image.replaceWith(el('span', { class: 'image-error' }, t('plaza.detail.figureLoadFailed')));
  });
  return el('figure', { class: 'plaza-figure-item' }, image,
    el('figcaption', {}, t('plaza.detail.figureCaption', { index: entry.index })));
}

function figuresSection(data) {
  const figures = collectFigures(data);
  const figureNotice = figureNoticeOf(data);
  const rawCount = rawFigureCount(data);
  if (!figures.length && !figureNotice && !rawCount) return null;
  const section = el('section', { class: 'card plaza-figures' },
    el('h2', { class: 'plaza-section-title' }, t('plaza.detail.figures')));
  if (figures.length) {
    section.append(el('div', { class: 'plaza-figure-grid' }, ...figures.map(figureGalleryEntry)));
  }
  if (figureNotice) {
    section.append(notice(figureNotice, 'warning'));
  } else if (!figures.length) {
    section.append(el('p', { class: 'help plaza-figure-placeholder' }, t('plaza.detail.figureUnavailable')));
  }
  return section;
}

/* ------------------------------------------------------------------------- *
 * 答案与解析（匿名可读，默认折叠，用户主动展开）
 * ------------------------------------------------------------------------- */

function answerBlocks(data) {
  const content = data.content && typeof data.content === 'object' ? data.content : {};
  const blocks = [];
  const rootAnswers = answersNode(content.answers);
  if (rootAnswers) blocks.push(rootAnswers);
  const rootExplanation = richText(content.explanation, 'plaza-explanation');
  if (rootExplanation) {
    blocks.push(el('h3', { class: 'plaza-subtitle' }, t('plaza.detail.explanation')), rootExplanation);
  }
  const rootRubric = richText(content.rubric, 'plaza-rubric');
  if (rootRubric) {
    blocks.push(el('h3', { class: 'plaza-subtitle' }, t('plaza.detail.rubric')), rootRubric);
  }
  const parts = Array.isArray(content.parts) ? content.parts : [];
  for (const [index, part] of parts.entries()) {
    const partAnswers = answersNode(part?.answers);
    const partExplanation = richText(part?.explanation, 'plaza-explanation');
    const partRubric = richText(part?.rubric, 'plaza-rubric');
    if (!partAnswers && !partExplanation && !partRubric) continue;
    blocks.push(el('h3', { class: 'plaza-subtitle' },
      t('plaza.detail.partAnswer', { index: number(part?.index ?? index + 1) })));
    if (partAnswers) blocks.push(partAnswers);
    if (partExplanation) blocks.push(partExplanation);
    if (partRubric) blocks.push(partRubric);
  }
  return blocks;
}

function answerSection(data) {
  const blocks = answerBlocks(data);
  const section = el('section', { class: 'card plaza-answer' },
    el('h2', { class: 'plaza-section-title' }, t('plaza.detail.answersTitle')));
  if (!blocks.length) {
    section.append(notice(t('plaza.detail.answerUnavailable'), 'warning'));
    return section;
  }
  const body = el('div', { class: 'plaza-answer-body hide' }, ...blocks);
  let expanded = false;
  const toggle = button(t('plaza.detail.showAnswer'), () => {
    expanded = !expanded;
    body.classList.toggle('hide', !expanded);
    toggle.textContent = expanded ? t('plaza.detail.hideAnswer') : t('plaza.detail.showAnswer');
    toggle.setAttribute('aria-expanded', String(expanded));
  }, 'soft', 'eye', { 'aria-expanded': 'false', 'aria-controls': 'plaza-answer-body' });
  body.id = 'plaza-answer-body';
  section.append(el('p', { class: 'help' }, t('plaza.detail.answerHint')), toggle, body);
  return section;
}

/* ------------------------------------------------------------------------- *
 * 收藏 / 纠错（报错）/ 举报（Phase 3 · WS21 契约）
 * - 收藏：POST|DELETE /plaza/questions/{public_id}/favorite；详情 favorited 优先，
 *   缺失时探测 GET /plaza/favorites（接口未就绪则保持未收藏外观，点击时再提示）；
 * - 纠错：POST /plaza/questions/{public_id}/issues {field, description}；
 * - 举报：POST /plaza/questions/{public_id}/reports {reason, detail}；
 * - 匿名点击以上动作统一引导登录（hash 保持，登录后 main.js 回到原公开题）。
 * ------------------------------------------------------------------------- */

const ISSUE_FIELD_VALUES = ['stem', 'options', 'answer', 'explanation', 'figure', 'source', 'other'];
const REPORT_REASON_VALUES = ['answer_error', 'copyright', 'privacy', 'content_quality', 'spam', 'other'];

function favoritePath(publicId) {
  return `/plaza/questions/${encodeURIComponent(publicId)}/favorite`;
}

function issuePath(publicId) {
  return `/plaza/questions/${encodeURIComponent(publicId)}/issues`;
}

function reportPath(publicId) {
  return `/plaza/questions/${encodeURIComponent(publicId)}/reports`;
}

/** 匿名点击「收藏/纠错/举报」：提示并回登录页；hash 不变，登录成功后回到本题。 */
function requireLogin() {
  toast(t('plaza.ops.loginRequired'));
  go('home');
}

/** 统一的广场操作弹层：标题 + 表单 + 提交按钮；校验失败由 busy 以 toast 提示。 */
function actionDialog({ title, hint, body, submitLabel, onSubmit }) {
  const dialog = el('dialog', {
    class: 'modal-dialog plzw-dialog',
    'data-plaza-action-dialog': '',
    'aria-label': title,
  },
  el('div', { class: 'pg-card-head', style: 'margin:0' },
    el('div', {},
      el('h3', { style: 'margin:0' }, title),
      hint ? el('p', { class: 'help' }, hint) : null),
    button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
  body,
  el('div', { class: 'pg-actions', style: 'margin-top:12px' },
    button(submitLabel, event => busy(event.currentTarget, async () => {
      await onSubmit();
      dialog.close();
    }), '', 'check')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

function openIssueDialog(publicId) {
  if (!state.user) return requireLogin();
  const fieldSelect = select(ISSUE_FIELD_VALUES.map(value => ({
    value,
    label: t(`plaza.ops.issue.field.${value}`),
  })), 'answer', { 'aria-label': t('plaza.ops.issue.fieldLabel') });
  const description = el('textarea', {
    class: 'input',
    rows: 3,
    maxlength: '500',
    placeholder: t('plaza.ops.issue.descriptionPlaceholder'),
  });
  return actionDialog({
    title: t('plaza.ops.issue.title'),
    hint: t('plaza.ops.issue.hint'),
    body: el('div', { class: 'plzw-dialog-body' },
      field(t('plaza.ops.issue.fieldLabel'), fieldSelect),
      field(t('plaza.ops.issue.description'), description, t('plaza.ops.issue.descriptionHint')),
      el('p', { class: 'help' }, t('plaza.ops.issue.privacyHint'))),
    submitLabel: t('plaza.ops.issue.submit'),
    onSubmit: async () => {
      const text = String(description.value || '').trim();
      if (text.length < 4) throw new Error(t('plaza.ops.issue.descriptionRequired'));
      const result = await api(issuePath(publicId), {
        method: 'POST',
        data: { field: fieldSelect.value, description: text },
      });
      toast(result?.message || t('plaza.ops.issue.submitted'));
    },
  });
}

function openReportDialog(publicId) {
  if (!state.user) return requireLogin();
  const reasonSelect = select(REPORT_REASON_VALUES.map(value => ({
    value,
    label: t(`plaza.report.reason.${value}`),
  })), 'answer_error', { 'aria-label': t('plaza.report.reasonLabel') });
  const detail = el('textarea', {
    class: 'input',
    rows: 3,
    maxlength: '500',
    placeholder: t('plaza.report.detailPlaceholder'),
  });
  return actionDialog({
    title: t('plaza.report.title'),
    hint: t('plaza.report.hint'),
    body: el('div', { class: 'plzw-dialog-body' },
      field(t('plaza.report.reasonLabel'), reasonSelect),
      field(t('plaza.report.detail'), detail, t('plaza.report.detailHint'))),
    submitLabel: t('plaza.report.submit'),
    onSubmit: async () => {
      const text = String(detail.value || '').trim();
      const result = await api(reportPath(publicId), {
        method: 'POST',
        data: { reason: reasonSelect.value, detail: text },
      });
      toast(result?.message || t('plaza.report.submitted'));
    },
  });
}

/**
 * 登录后动作区：收藏（随状态切换文案）+ 纠错 + 举报 + 投稿入口。
 * 收藏按钮带 data-plaza-action="favorite" 与 data-favorited，便于验收脚本核对状态。
 */
function memberActions(data) {
  const publicId = String(data.public_id || '').trim();
  if (!publicId) return null;
  let favorited = data.favorited === true;
  const favoriteLabel = el('span', {}, favorited ? t('plaza.favorite.remove') : t('plaza.favorite.add'));
  const favoriteButton = el('button', {
    type: 'button',
    class: 'button secondary small',
    'data-plaza-action': 'favorite',
    'data-favorited': String(favorited),
    onClick: event => busy(event.currentTarget, async () => {
      if (!state.user) return requireLogin();
      const next = !favorited;
      await api(favoritePath(publicId), { method: next ? 'POST' : 'DELETE' });
      setFavorited(next);
      toast(next ? t('plaza.favorite.added') : t('plaza.favorite.removed'));
    }),
  }, icon('star'), favoriteLabel);

  function setFavorited(next) {
    favorited = Boolean(next);
    favoriteButton.dataset.favorited = String(favorited);
    favoriteButton.classList.toggle('soft', favorited);
    favoriteLabel.textContent = favorited ? t('plaza.favorite.remove') : t('plaza.favorite.add');
  }

  // 旧 payload（无 favorited 字段）时探测收藏列表；接口未就绪静默保持未收藏外观。
  if (typeof data.favorited !== 'boolean') {
    api(`/plaza/favorites?${query({ page: 1, page_size: 100 })}`).then(result => {
      const items = Array.isArray(result) ? result : (Array.isArray(result?.items) ? result.items : []);
      setFavorited(items.some(item => String(item?.public_id || '') === publicId && item?.available !== false));
    }).catch(() => { /* 收藏接口未就绪：保持未收藏外观，点击时能力探测 */ });
  }

  return el('div', {
    class: 'plaza-member-actions',
    'data-plaza-member-actions': 'true',
    'data-public-id': publicId,
  },
  el('span', { class: 'help' }, t('plaza.action.memberHint')),
  favoriteButton,
  button(t('plaza.ops.issue.button'), () => openIssueDialog(publicId), 'secondary small', 'edit', {
    'data-plaza-action': 'issue',
  }),
  button(t('plaza.report.button'), () => openReportDialog(publicId), 'secondary small', 'message', {
    'data-plaza-action': 'report',
  }),
  button(t('plaza.action.submit'), () => go('mybank'), 'ghost small', 'upload'));
}

/** 匿名动作区：只保留引导登录的入口（不显示任何未登录可写操作）。 */
function guestActions(data) {
  void data;
  return el('div', {
    class: 'plaza-member-actions plaza-guest-actions',
    'data-plaza-guest-actions': 'true',
  },
  el('span', { class: 'help' }, t('plaza.ops.loginHint')),
  button(t('plaza.favorite.add'), () => requireLogin(), 'secondary small', 'star', {
    'data-plaza-action': 'favorite',
  }),
  button(t('plaza.ops.issue.button'), () => requireLogin(), 'secondary small', 'edit', {
    'data-plaza-action': 'issue',
  }),
  button(t('plaza.report.button'), () => requireLogin(), 'secondary small', 'message', {
    'data-plaza-action': 'report',
  }));
}

/* ------------------------------------------------------------------------- *
 * 详情节点
 * ------------------------------------------------------------------------- */

function plazaDetailNode(data) {
  if (data && data.visible === false) return hiddenContentNode(data);

  const status = plazaStatusMeta(data);
  const { head, node: badges } = statusBadges(data);
  const title = head || t('plaza.detail.untitled');
  const content = data.content && typeof data.content === 'object' ? data.content : {};
  // 撤回/不可见内容不回显正文：即使后端漏给 visible=false，只要状态为 withdrawn
  // 且没有可公开的题面，就按隐藏态展示。
  if (status.key === 'withdrawn' && !String(content.stem || '').trim()) return hiddenContentNode(data);
  const knowledgeTags = (Array.isArray(data.knowledge_tags) ? data.knowledge_tags : [])
    .map(tag => String(tag || '').trim())
    .filter(Boolean);
  const meta = [difficultyText(data.difficulty), data.grade].filter(Boolean);

  document.title = `${title} · ${t('nav.plaza')} · 知序`;

  const page = el('div', { class: 'plaza-page', 'data-plaza-view': 'detail' },
    backToList(),
    el('section', { class: 'card plaza-detail-head' },
      badges,
      el('h1', { class: 'plaza-detail-title' }, title),
      meta.length ? el('p', { class: 'plaza-detail-meta' }, ...meta) : null,
      knowledgeTags.length
        ? el('div', { class: 'plaza-card-tags' },
          ...knowledgeTags.slice(0, 10).map(tag => el('span', { class: 'kp-chip', 'data-no-i18n': '' }, tag)))
        : null,
      status.key === 'pending'
        ? notice(t('plaza.detail.communityNotice'), 'warning')
        : (String(data.notice || '').trim() ? notice(String(data.notice).trim()) : null),
      reviewSummaryNode(data)),
    el('section', { class: 'card plaza-content' },
      el('h2', { class: 'plaza-section-title' }, t('plaza.detail.contentTitle')),
      content.stem ? richText(content.stem, 'plaza-stem') : null,
      content.parts && content.parts.length
        ? partsNode(content.parts)
        : optionsNode(content.options)),
    figuresSection(data),
    answerSection(data),
    plazaSourceDeclaration(data.source_declaration),
    // 登录用户显示收藏/纠错/举报操作区；匿名用户只显示「登录后可操作」引导行。
    state.user ? memberActions(data) : null,
    state.user ? null : guestActions(data),
    el('p', { class: 'plaza-detail-foot' }, t('plaza.detail.guestFoot')));

  return page;
}
