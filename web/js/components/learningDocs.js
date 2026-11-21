/**
 * M6 学习文档组件：专题报告（topic reports）与互动讲义（handouts）。
 * ---------------------------------------------------------------------------
 * 服务端「规则汇编」能力，前端只读陈列 + 创建/导出/删除；不生成、不补写、不伪造
 * 任何服务端内容。数据契约（后端由并行线实现，按此编码）：
 *
 *   POST   /api/topic-reports              {topic,title?,document_ids?,note_ids?,artifact_ids?,kp_ids?}
 *          → 201 {report:{report_id,topic,title,generator:"rules",status,version,created_at,updated_at},
 *                 content_md, sources:[{kind,id,title,ref?,page?,chunk_id?,quote?,version?}]}
 *   GET    /api/topic-reports?limit=20     → {items:[report],total,limit}
 *   GET    /api/topic-reports/{id}         → {report,content_md,sources}
 *   PATCH  /api/topic-reports/{id}         {title?,content_md?,expected_version?} → 同创建结构；版本冲突 409
 *   DELETE /api/topic-reports/{id}
 *   GET    /api/topic-reports/{id}/markdown → text/markdown 附件（fetch blob 下载）
 *
 *   POST   /api/handouts                   {title?,course_id?,note_ids?,artifact_ids?,kp_ids?}
 *          → 201 {handout:{handout_id,title,course_id,version,generator:"rules",created_at,updated_at},
 *                 sections:[{id,kind,title,paragraphs,quotes,items}]}
 *   GET    /api/handouts?limit=20          → {items:[handout summary],total}
 *   GET    /api/handouts/{id}              → {handout,sections}
 *   DELETE /api/handouts/{id}
 *   GET    /api/handouts/{id}/export?format=html|pdf → 文件流（PDF 依赖不可用 503 中文 detail）
 *
 * 安全与降级：
 * - Markdown 一律走 core/markdown.js 的 markdown()（内部 DOMPurify 净化），服务端文本
 *   只通过 textContent 写入，绝不用 innerHTML 拼接；
 * - 讲义 items.preview_data_uri 仅在 `data:image/png;base64,` 前缀且长度受限时渲染 <img>；
 * - 列表接口不可用时区块显式中文降级 + 重试，并隐藏创建入口；不伪造任何条目；
 * - 创建表单的可选来源分组（资料/笔记/附件/课程）异步加载，单个失败只隐藏该分组并给
 *   中文提示（全部失败时退化为仅主题/标题输入，交由服务端校验并展示中文原因）；
 * - 下载统一 fetch → Blob → a[download]（downloadBlob），文件名只由本地标题拼装。
 *
 * i18n：新文案统一 `study.m6.<key>`；语言包尚未补键时由 m6t() 回退内置中文，
 * 补齐 locales/*.json 后英文自动生效（详见交付汇报的 zh/en 键清单）。
 */

import { api, errorText } from '../core/api.js';
import {
  el, button, badge, busy, dateText, downloadBlob, empty, field, input, notice,
  select, toast,
} from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { t } from '../core/i18n.js';

/* ===========================================================================
 * 1. i18n：study.m6.* + 内置中文兜底
 * =========================================================================== */

const M6_TEXT = Object.freeze({
  // 页面级区块（eyebrow 与既有页面一致保留英文设计字）
  sectionEyebrow: 'LEARNING DOCUMENTS',
  sectionTitle: '学习文档',
  sectionHint: '服务端规则汇编，前端只读陈列；引用真实资料片段与摘录，不含 AI 生成。',
  // 专题报告
  topicReportsTitle: '专题报告',
  topicReportsSummary: '按主题引用真实资料片段与摘录的规则汇编',
  topicReportsDesc: '规则汇编：引用真实资料片段与摘录，不含 AI 生成。',
  topicReportsEmptyTitle: '还没有专题报告',
  topicReportsEmptyText: '选择主题与资料来源，生成一份带页码和 chunk 出处的规则汇编报告。',
  newTopicReport: '新建专题报告',
  createReport: '生成报告',
  reportTopic: '报告主题',
  reportTopicPlaceholder: '例如：函数的单调性与导数',
  topicLengthHint: '2–200 字，用于在资料中检索真实片段。',
  topicInvalid: '报告主题需为 2–200 字。',
  reportTitleOptional: '报告标题（可选）',
  reportTitlePlaceholder: '留空时按主题自动命名',
  reportSources: '引用来源（至少选择一项）',
  reportSourcesHint: '只引用勾选的资料、笔记与学习附件；报告中的页码、chunk 与引用均来自真实内容。',
  generatorRules: '规则汇编',
  topicReportsUnavailable: '专题报告服务暂不可用',
  loadingReports: '正在加载专题报告…',
  reportSourcesTitle: '来源（{count} 条）',
  reportSourcesEmpty: '这份报告没有来源记录。',
  reportNoContent: '报告正文为空。',
  reportContentLabel: '正文（Markdown）',
  reportContentHint: '支持 Markdown 与 KaTeX；保存后版本号加一，可重新下载 Markdown。',
  reportTitleLabel: '标题',
  editReport: '编辑标题/正文',
  saveReport: '保存修改',
  reportSaved: '专题报告已更新',
  reportConflict: '报告已被更新，请刷新后重试',
  reportCreated: '专题报告已生成',
  creatingReport: '正在生成专题报告…',
  reportDeleted: '专题报告已删除',
  deleteReportConfirm: '删除这份专题报告？删除后无法恢复。',
  reportReadFailed: '读取报告失败：{reason}',
  downloadMarkdown: '下载 Markdown',
  downloadReportFailed: '下载失败：{reason}',
  versionLabel: '版本 v{version}',
  topicLabel: '主题：{topic}',
  reportUntitled: '未命名报告',
  handoutUntitled: '未命名讲义',
  // 可选来源
  sourceGroupDocuments: '资料',
  sourceGroupNotes: '笔记',
  sourceGroupArtifacts: '学习附件',
  sourceGroupCourses: '课程',
  sourcesLoading: '正在加载可选来源…',
  sourcesUnavailable: '「{group}」列表暂不可用，已隐藏该分组，可继续使用其它来源。',
  sourcesAllUnavailable: '可选来源暂不可用，请稍后重试。',
  sourceEmpty: '暂无可选{group}。',
  sourceGroupTruncated: '仅显示前 {shown} 项（共 {total} 项）。',
  sourceDocChunks: '{count} 个内容块',
  reportSourcesRequired: '请至少选择一份资料、一条笔记或一个学习附件作为报告来源。',
  noSourcesAvailable: '暂无可引用的资料、笔记或学习附件，请先上传资料或整理笔记。',
  // 来源条目
  'sourceKind.document': '资料',
  'sourceKind.note': '笔记',
  'sourceKind.artifact': '学习附件',
  'sourceKind.knowledgePoint': '知识点',
  'sourceKind.question': '题目',
  'sourceKind.course': '课程',
  'sourceKind.excerpt': '摘录',
  'sourceKind.other': '来源',
  sourcePage: '第 {page} 页',
  sourceChunk: 'chunk {chunk}',
  sourceRef: '出处：{ref}',
  sourceVersion: '版本 v{version}',
  // 互动讲义
  handoutsTitle: '互动讲义',
  handoutsSummary: '按课程与来源组织、可导出 HTML/PDF 的规则汇编讲义',
  handoutsDesc: '规则汇编：按课程大纲、学习附件、笔记摘录与练习组织，不含 AI 生成。',
  handoutsEmptyTitle: '还没有互动讲义',
  handoutsEmptyText: '选择课程、笔记或学习附件，生成一份可导出 HTML/PDF 的规则汇编讲义。',
  newHandout: '新建互动讲义',
  createHandout: '生成讲义',
  handoutTitleLabel: '讲义标题（可选）',
  handoutTitlePlaceholder: '留空时按课程与来源自动命名',
  handoutCourseLabel: '关联课程（可选）',
  handoutCourseNone: '不指定课程',
  handoutSources: '讲义来源（至少选择一项）',
  handoutSourcesHint: '课程、笔记与学习附件至少选择一项；讲义将按所选来源生成规则汇编小节。',
  handoutSourcesRequired: '请至少选择课程、笔记或学习附件中的一项作为讲义来源。',
  handoutArtifactsLimit: '学习附件最多选择 {count} 个。',
  creatingHandout: '正在生成互动讲义…',
  handoutCreated: '互动讲义已生成',
  handoutsUnavailable: '互动讲义服务暂不可用',
  loadingHandouts: '正在加载互动讲义…',
  exportHtml: '导出 HTML',
  exportPdf: '导出 PDF',
  exportFailed: '导出失败：{reason}',
  exportPdfUnavailable: 'PDF 导出暂不可用（服务端 PDF 依赖缺失），请改用「导出 HTML」或浏览器打印。',
  handoutDeleted: '互动讲义已删除',
  deleteHandoutConfirm: '删除这份互动讲义？删除后无法恢复。',
  handoutDetailTitle: '互动讲义',
  handoutSectionsTitle: '讲义小节（{count} 个）',
  handoutSectionsEmpty: '这份讲义还没有小节。',
  handoutSectionEmpty: '本小节暂无内容。',
  handoutRulesNotice: '规则汇编：讲义由服务端按来源规则整理，不含 AI 生成；引用内容来自真实资料与摘录。',
  handoutPreviewAlt: '讲义配图：{title}',
  handoutReadFailed: '读取讲义失败：{reason}',
  sectionCount: '{count} 个小节',
  'sectionKind.courseOutline': '课程大纲',
  'sectionKind.artifact': '学习附件',
  'sectionKind.excerpt': '笔记摘录',
  'sectionKind.practice': '伴读练习',
  'sectionKind.evidence': '学习证据',
  'sectionKind.meta': '说明',
  'sectionKind.other': '其他',
  // 打开/通用
  open: '查看',
  unexpectedResponse: '服务返回格式异常，请稍后重试。',
  networkFailed: '网络连接失败，请稍后重试。',
  unknownError: '未知原因',
});

function m6Interpolate(text, vars) {
  if (!vars || typeof text !== 'string') return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => (
    vars[name] === undefined || vars[name] === null ? match : String(vars[name])
  ));
}

/**
 * 取 `study.m6.*` 文案：语言包缺键时回退内置中文（支持 `{name}` 插值）。
 * 除组件内部使用外也导出给 notes.js 拼接区块标题，避免在 notes.js 里写字面量新键。
 *
 * @param {string} key 不含 `study.m6.` 前缀的键名（可用点号分命名空间，如 sourceKind.document）
 * @param {Record<string, unknown>} [vars]
 * @returns {string}
 */
export function m6t(key, vars = null) {
  const full = `study.m6.${key}`;
  const translated = t(full, vars);
  if (translated !== full) return translated;
  const fallback = Object.prototype.hasOwnProperty.call(M6_TEXT, key) ? M6_TEXT[key] : key;
  return m6Interpolate(fallback, vars);
}

/* ===========================================================================
 * 2. 常量与通用工具
 * =========================================================================== */

const REPORT_LIST_LIMIT = 20;
const HANDOUT_LIST_LIMIT = 20;
const SOURCE_ITEM_LIMIT = 50;
const MIN_TOPIC_LENGTH = 2;
const MAX_TOPIC_LENGTH = 200;
const MAX_HANDOUT_ARTIFACTS = 10;
const PREVIEW_PNG_PREFIX = 'data:image/png;base64,';
const PREVIEW_MAX_LENGTH = 4_000_000;

/** 规则类枚举 → 中文文案键（未知枚举统一「来源/其他」，不把英文枚举拼进界面）。 */
const SOURCE_KIND_KEYS = Object.freeze({
  document: 'sourceKind.document',
  doc: 'sourceKind.document',
  material: 'sourceKind.document',
  note: 'sourceKind.note',
  artifact: 'sourceKind.artifact',
  learning_artifact: 'sourceKind.artifact',
  knowledge_point: 'sourceKind.knowledgePoint',
  knowledgepoint: 'sourceKind.knowledgePoint',
  kp: 'sourceKind.knowledgePoint',
  question: 'sourceKind.question',
  bank: 'sourceKind.question',
  course: 'sourceKind.course',
  excerpt: 'sourceKind.excerpt',
  annotation: 'sourceKind.excerpt',
});

const SECTION_KIND_KEYS = Object.freeze({
  course_outline: 'sectionKind.courseOutline',
  artifact: 'sectionKind.artifact',
  excerpt: 'sectionKind.excerpt',
  practice: 'sectionKind.practice',
  evidence: 'sectionKind.evidence',
  meta: 'sectionKind.meta',
});

function m6SourceKindLabel(kind) {
  const key = SOURCE_KIND_KEYS[String(kind || '').toLowerCase()];
  return key ? m6t(key) : m6t('sourceKind.other');
}

function m6SectionKindLabel(kind) {
  const key = SECTION_KIND_KEYS[String(kind || '').toLowerCase()];
  return key ? m6t(key) : m6t('sectionKind.other');
}

function m6Text(value) {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : String(value);
}

function m6Trimmed(value) {
  return m6Text(value).trim();
}

/** 错误 → 中文可读原因（api()/m6RequestJson 已给出中文 detail 时直接复用）。 */
function m6ErrorText(error) {
  return m6Trimmed(error && error.message) || m6t('unknownError');
}

/** 本地拼装下载文件名：标题只作为文件名片段，不做任何 HTML 拼接。 */
function m6FileName(title, suffix) {
  const base = m6Text(title).replace(/[\\/:*?"<>|\s]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 80);
  return `${base || 'zhixu-document'}-${suffix}`;
}

function m6VersionText(version) {
  const number = Number(version);
  return Number.isFinite(number) && number > 0 ? m6t('versionLabel', { version: number }) : '';
}

function m6ReportTitle(report) {
  const title = m6Trimmed(report && report.title);
  if (title) return title;
  const topic = m6Trimmed(report && report.topic);
  return topic || m6t('reportUntitled');
}

function m6HandoutTitle(handout) {
  return m6Trimmed(handout && handout.title) || m6t('handoutUntitled');
}

function m6SetBusy(node, busyState) {
  if (!node) return;
  node.disabled = Boolean(busyState);
  if (busyState) node.setAttribute('aria-busy', 'true');
  else node.removeAttribute('aria-busy');
}

/** 服务端进度占位（列表加载/来源加载/生成中），样式为 .m6-progress。 */
function m6Progress(text) {
  return el('div', { class: 'm6-progress', role: 'status', 'aria-live': 'polite' },
    el('span', { class: 'm6-progress-track', 'aria-hidden': 'true' },
      el('span', { class: 'm6-progress-bar' })),
    el('span', { class: 'm6-progress-text' }, text));
}

/**
 * 弹层骨架：head（标题/关闭）+ body + footer（操作）。关闭时移除 DOM 并回调清理。
 * 所有服务端内容由调用方用 textContent/markdown() 放入，禁止 innerHTML 拼接。
 */
function m6DialogShell({ className = '', label = '' } = {}) {
  const dialog = el('dialog', {
    class: `modal-dialog m6-dialog ${className}`.trim(),
    'aria-label': label || t('common.close'),
  });
  const head = el('div', { class: 'm6-dialog-head' });
  const body = el('div', { class: 'm6-dialog-body' });
  const footer = el('div', { class: 'm6-dialog-actions' });
  let onClose = null;
  dialog.append(head, body, footer);

  function close() {
    if (dialog.open) {
      try { dialog.close(); } catch { dialog.remove(); }
    } else {
      dialog.remove();
    }
  }

  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  dialog.addEventListener('close', () => {
    dialog.remove();
    const callback = onClose;
    onClose = null;
    if (callback) {
      try { callback(); } catch { /* 清理失败不影响页面 */ }
    }
  });

  return {
    dialog,
    head,
    body,
    footer,
    close,
    mount() {
      document.body.append(dialog);
      dialog.showModal();
    },
    setCloseHandler(callback) { onClose = callback; },
  };
}

/** Download via fetch + Blob；不经过 api()，便于拿到 HTTP 状态做 503/422 分支。 */
async function m6FetchBlob(path) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
  } catch {
    throw new Error(m6t('networkFailed'));
  }
  if (!response.ok) {
    let detail = '';
    try {
      const data = await response.json();
      const reason = data && (data.detail || data.error);
      if (reason) detail = m6Trimmed(errorText(reason));
    } catch {
      detail = '';
    }
    const error = new Error(detail || `下载失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return response.blob();
}

/** 仅 CAS 修改需要区分 409：PATCH 走同源 fetch 并在错误上标记 conflict/status。 */
async function m6RequestJson(path, options = {}) {
  const method = String(options.method || 'POST').toUpperCase();
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  };
  if (options.data !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.data);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new Error(m6t('networkFailed'));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      m6Trimmed(errorText(data && (data.detail || data.error))) || `请求失败（${response.status}）`
    );
    error.status = response.status;
    if (response.status === 409) error.conflict = true;
    throw error;
  }
  return data;
}

/**
 * 讲义 item 预览图：只有服务端约定的内联 PNG data URL 才渲染 <img>；
 * 拒绝外链、其它 MIME 与超长内容（不做任何 URL 拼接）。
 */
function m6PreviewImage(raw, title) {
  const value = m6Trimmed(raw);
  if (!value || value.length > PREVIEW_MAX_LENGTH) return null;
  if (!value.startsWith(PREVIEW_PNG_PREFIX)) return null;
  if (!/^data:image\/png;base64,[A-Za-z0-9+/=\s]+$/.test(value)) return null;
  return el('img', {
    class: 'm6-section-item-preview',
    src: value,
    alt: m6t('handoutPreviewAlt', { title: m6Text(title) }),
    loading: 'lazy',
    decoding: 'async',
  });
}

/* ===========================================================================
 * 3. 可选来源分组（创建表单共用）
 * =========================================================================== */

const SOURCE_GROUP_DEFS = Object.freeze({
  documents: {
    key: 'documents',
    endpoint: '/documents',
    groupKey: 'sourceGroupDocuments',
    valueOf: item => item.id,
    labelOf: item => item.title,
    metaOf: item => (Number(item.chunks) > 0 ? m6t('sourceDocChunks', { count: Number(item.chunks) }) : ''),
  },
  notes: {
    key: 'notes',
    endpoint: '/notes',
    groupKey: 'sourceGroupNotes',
    valueOf: item => item.id,
    labelOf: item => item.title,
    metaOf: item => [
      m6Trimmed(item.subject),
      item.updated_at ? t('common.updatedAt', { time: dateText(item.updated_at) }) : '',
    ].filter(Boolean).join(' · '),
  },
  artifacts: {
    key: 'artifacts',
    endpoint: '/artifacts?page_size=50',
    groupKey: 'sourceGroupArtifacts',
    valueOf: item => item.artifact_id,
    labelOf: item => item.title,
    metaOf: item => m6VersionText(item.current_revision),
  },
  courses: {
    key: 'courses',
    endpoint: '/classroom/courses?page_size=50',
    groupKey: 'sourceGroupCourses',
    valueOf: item => item.id,
    labelOf: item => item.title,
    metaOf: () => '',
  },
});

/** 并行加载多个可选来源分组；失败只记录该分组，不抛出（不阻塞创建表单）。 */
async function m6LoadGroups(defs) {
  const results = await Promise.allSettled(defs.map(def => api(def.endpoint)));
  return defs.map((def, index) => {
    const result = results[index];
    if (result.status === 'fulfilled') {
      const source = result.value && Array.isArray(result.value.items) ? result.value.items : [];
      return {
        def,
        ok: true,
        items: source.filter(item => item && typeof item === 'object'),
      };
    }
    return { def, ok: false, items: [], error: result.reason };
  });
}

/**
 * 复选来源分组：items 渲染为可滚动复选列表；limit>0 时限制勾选数量并中文提示。
 * @returns {{node: HTMLElement, selected: () => string[]}}
 */
function m6ChecklistGroup(def, items, { limit = 0, limitMessage = '' } = {}) {
  const visible = items.slice(0, SOURCE_ITEM_LIMIT);
  const entries = [];
  const list = el('div', {
    class: 'm6-check-list',
    role: 'group',
    'aria-label': m6t(def.groupKey),
  });

  function syncLimitState() {
    if (!limit) return;
    const checkedCount = entries.filter(entry => entry.box.checked).length;
    for (const entry of entries) entry.box.disabled = !entry.box.checked && checkedCount >= limit;
  }

  for (const item of visible) {
    const labelText = m6Trimmed(def.labelOf(item)) || m6t(def.groupKey);
    const metaText = m6Trimmed(def.metaOf(item));
    const box = el('input', {
      type: 'checkbox',
      class: 'm6-check-box',
      value: m6Text(def.valueOf(item)),
    });
    box.addEventListener('change', () => {
      if (limit) {
        const checkedCount = entries.filter(entry => entry.box.checked).length;
        if (checkedCount > limit) {
          box.checked = false;
          toast(limitMessage || m6t('handoutArtifactsLimit', { count: limit }), true);
        }
      }
      syncLimitState();
    });
    entries.push({ box, item });
    list.append(el('label', { class: 'm6-check' },
      box,
      el('span', { class: 'm6-check-text' },
        el('span', { class: 'm6-check-title', title: labelText, 'data-no-i18n': '' }, labelText),
        metaText ? el('span', { class: 'm6-check-meta' }, metaText) : null)));
  }

  const children = [
    el('div', { class: 'm6-source-group-head' },
      el('h4', { class: 'm6-source-group-title' }, m6t(def.groupKey)),
      el('span', { class: 'm6-source-group-count' }, t('common.countItems', { count: items.length }))),
  ];
  if (!items.length) {
    children.push(el('p', { class: 'help m6-source-empty' }, m6t('sourceEmpty', { group: m6t(def.groupKey) })));
  } else {
    children.push(list);
    if (items.length > visible.length) {
      children.push(el('p', { class: 'help' },
        m6t('sourceGroupTruncated', { shown: visible.length, total: items.length })));
    }
  }

  return {
    node: el('section', { class: 'm6-source-group', 'data-source-group': def.key }, ...children),
    selected: () => entries.filter(entry => entry.box.checked).map(entry => entry.box.value),
  };
}

/* ===========================================================================
 * 4. 专题报告面板
 * =========================================================================== */

/**
 * 渲染专题报告区块。
 * @param {{heading?: boolean}} [options] heading=false 时用于嵌在页面折叠区块内
 *   （区块标题由调用方提供，组件只保留说明与操作）。
 * @returns {{element: HTMLElement, refresh: () => Promise<void>, destroy: () => void}}
 */
export function renderTopicReportsPanel(options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const showHeading = opts.heading !== false;
  const openDialogs = new Set();
  let destroyed = false;
  let listSeq = 0;

  const statusHost = el('div', { class: 'm6-panel-status' });
  const listHost = el('div', { class: 'm6-card-list m6-report-list' });
  const createButton = button(m6t('newTopicReport'), () => { void openCreateDialog(); }, '', 'plus');
  createButton.classList.add('m6-create');
  const refreshButton = button(t('common.reload'), event => {
    void busy(event.currentTarget, () => load());
  }, 'ghost small', 'rotate');
  refreshButton.classList.add('m6-refresh');

  const host = el('section', {
    class: 'm6-panel m6-panel-reports',
    'aria-label': m6t('topicReportsTitle'),
  });
  host.append(
    el('div', { class: 'm6-panel-head' },
      el('div', { class: 'm6-panel-head-text' },
        showHeading ? el('p', { class: 'm6-kicker' }, m6t('generatorRules')) : null,
        showHeading ? el('h3', { class: 'm6-panel-title' }, m6t('topicReportsTitle')) : null,
        el('p', { class: 'm6-panel-desc' }, m6t('topicReportsDesc'))),
      el('div', { class: 'm6-panel-actions' }, createButton, refreshButton)),
    statusHost,
    listHost
  );

  /* ---- 列表状态 ---- */

  function setDegraded(message) {
    host.classList.add('is-degraded');
    createButton.classList.add('hide');
    createButton.hidden = true;
    listHost.replaceChildren();
    statusHost.replaceChildren(
      notice(message, 'warning'),
      el('div', { class: 'm6-panel-retry' },
        button(t('common.retry'), event => { void busy(event.currentTarget, () => load()); }, 'soft small', 'rotate'))
    );
  }

  function clearDegraded() {
    host.classList.remove('is-degraded');
    createButton.classList.remove('hide');
    createButton.hidden = false;
    statusHost.replaceChildren();
  }

  async function load() {
    if (destroyed) return;
    const mine = ++listSeq;
    statusHost.replaceChildren(m6Progress(m6t('loadingReports')));
    try {
      const result = await api(`/topic-reports?limit=${REPORT_LIST_LIMIT}`);
      if (destroyed || mine !== listSeq) return;
      clearDegraded();
      const items = (result && Array.isArray(result.items) ? result.items : [])
        .filter(item => item && typeof item === 'object' && item.report_id);
      renderList(items);
    } catch (error) {
      if (destroyed || mine !== listSeq) return;
      const reason = m6Trimmed(error && error.message);
      setDegraded(reason ? `${m6t('topicReportsUnavailable')}（${reason}）` : m6t('topicReportsUnavailable'));
    }
  }

  function renderList(items) {
    listHost.replaceChildren();
    if (!items.length) {
      listHost.append(empty(
        m6t('topicReportsEmptyTitle'),
        m6t('topicReportsEmptyText'),
        button(m6t('newTopicReport'), () => { void openCreateDialog(); }, 'soft small', 'plus'),
        'notes'
      ));
      return;
    }
    listHost.append(...items.map(reportCardNode));
  }

  function reportCardNode(report) {
    const reportId = m6Text(report.report_id);
    const topic = m6Trimmed(report.topic);
    const titleText = m6ReportTitle(report);
    const version = Number(report.version) || 0;
    const meta = el('div', { class: 'm6-card-meta' },
      m6Trimmed(report.generator) === 'rules' ? badge(m6t('generatorRules'), 'teal') : null,
      version ? el('span', {}, m6VersionText(version)) : null,
      report.updated_at ? el('span', {}, t('common.updatedAt', { time: dateText(report.updated_at) })) : null);
    const actions = el('div', { class: 'm6-card-actions' },
      button(m6t('open'), () => { void openTopicReportDetail(null, reportId); }, 'secondary small', 'eye'),
      button(m6t('downloadMarkdown'), event => {
        void downloadReportMarkdown(report, event.currentTarget);
      }, 'ghost small', 'download'),
      button(t('common.delete'), event => busy(event.currentTarget, async () => {
        if (!window.confirm(m6t('deleteReportConfirm'))) return;
        await api(`/topic-reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' });
        toast(m6t('reportDeleted'));
        await load();
      }), 'danger small', 'trash'));
    return el('article', { class: 'm6-card m6-report-card', 'data-report-id': reportId },
      el('div', { class: 'm6-card-head' },
        el('div', { class: 'm6-card-title-wrap' },
          el('h3', { class: 'm6-card-title', 'data-no-i18n': '', title: titleText }, titleText),
          topic && topic !== titleText
            ? el('p', { class: 'm6-card-sub' }, m6t('topicLabel', { topic }))
            : null)),
      meta,
      actions);
  }

  /* ---- 下载 Markdown ---- */

  async function downloadReportMarkdown(report, trigger) {
    const reportId = m6Text(report && report.report_id);
    if (!reportId) return;
    m6SetBusy(trigger, true);
    try {
      const blob = await m6FetchBlob(`/topic-reports/${encodeURIComponent(reportId)}/markdown`);
      const version = Number(report.version) || 1;
      downloadBlob(blob, m6FileName(m6ReportTitle(report), `v${version}.md`));
    } catch (error) {
      toast(m6t('downloadReportFailed', { reason: m6ErrorText(error) }), true);
    } finally {
      if (trigger && trigger.isConnected) m6SetBusy(trigger, false);
    }
  }

  /* ---- 创建弹层 ---- */

  function openCreateDialog() {
    if (destroyed) return;
    const shell = m6DialogShell({ className: 'm6-report-create', label: m6t('newTopicReport') });
    openDialogs.add(shell);
    shell.setCloseHandler(() => openDialogs.delete(shell));

    const topicInput = input(m6t('reportTopicPlaceholder'), {
      required: true,
      minlength: MIN_TOPIC_LENGTH,
      maxlength: MAX_TOPIC_LENGTH,
      autocomplete: 'off',
      autofocus: true,
    });
    const titleInput = input(m6t('reportTitlePlaceholder'), { maxlength: 120, autocomplete: 'off' });
    const sourcesHost = el('div', { class: 'm6-source-groups' });
    const errorBox = el('div', { class: 'm6-dialog-error hide', role: 'alert' });
    const statusBox = el('div', { class: 'm6-dialog-status', role: 'status', 'aria-live': 'polite' });
    const submitButton = button(m6t('createReport'), () => { void submit(); }, '', 'sparkle');
    const sourceState = { loaded: false, anyLoaded: false, candidateCount: 0, checks: {} };

    shell.head.append(
      el('div', { class: 'm6-dialog-title-wrap' },
        el('h3', { class: 'm6-dialog-title' }, m6t('newTopicReport')),
        el('p', { class: 'm6-dialog-sub' }, m6t('topicReportsDesc'))),
      button(t('common.close'), () => shell.close(), 'ghost small', 'close')
    );
    shell.body.append(
      field(m6t('reportTopic'), topicInput, m6t('topicLengthHint')),
      el('div', { class: 'm6-field-block' }, field(m6t('reportTitleOptional'), titleInput)),
      el('div', { class: 'm6-source-section-head' },
        el('div', { class: 'field-label' }, m6t('reportSources')),
        el('p', { class: 'help' }, m6t('reportSourcesHint'))),
      sourcesHost,
      errorBox,
      statusBox
    );
    shell.footer.append(submitButton);
    shell.mount();

    function showError(message) {
      errorBox.textContent = message;
      errorBox.classList.remove('hide');
    }

    async function loadSources() {
      sourceState.loaded = false;
      m6SetBusy(submitButton, true);
      sourcesHost.replaceChildren(m6Progress(m6t('sourcesLoading')));
      const groups = await m6LoadGroups([
        SOURCE_GROUP_DEFS.documents,
        SOURCE_GROUP_DEFS.notes,
        SOURCE_GROUP_DEFS.artifacts,
      ]);
      if (!shell.dialog.isConnected) return;
      const nodes = [];
      const failed = [];
      const checks = {};
      let anyLoaded = false;
      let candidateCount = 0;
      for (const group of groups) {
        if (!group.ok) {
          failed.push(m6t(group.def.groupKey));
          continue;
        }
        anyLoaded = true;
        candidateCount += group.items.length;
        const checklist = m6ChecklistGroup(group.def, group.items);
        checks[group.def.key] = checklist.selected;
        nodes.push(checklist.node);
      }
      sourceState.loaded = true;
      sourceState.anyLoaded = anyLoaded;
      sourceState.candidateCount = candidateCount;
      sourceState.checks = checks;
      const children = [...nodes];
      if (failed.length) {
        children.push(notice(m6t('sourcesUnavailable', { group: failed.join('、') }), 'warning'));
      }
      if (!anyLoaded) {
        children.push(notice(m6t('sourcesAllUnavailable'), 'warning'));
        children.push(el('div', { class: 'm6-panel-retry' },
          button(t('common.retry'), () => { void loadSources(); }, 'soft small', 'rotate')));
      }
      sourcesHost.replaceChildren(...children);
      m6SetBusy(submitButton, false);
    }

    function buildPayload() {
      const topic = topicInput.value.trim();
      if (topic.length < MIN_TOPIC_LENGTH || topic.length > MAX_TOPIC_LENGTH) {
        throw new Error(m6t('topicInvalid'));
      }
      const payload = { topic };
      const title = titleInput.value.trim();
      if (title) payload.title = title;
      const documentIds = (sourceState.checks.documents ? sourceState.checks.documents() : [])
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0);
      const noteIds = (sourceState.checks.notes ? sourceState.checks.notes() : [])
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0);
      const artifactIds = (sourceState.checks.artifacts ? sourceState.checks.artifacts() : [])
        .map(value => m6Text(value))
        .filter(Boolean);
      if (documentIds.length) payload.document_ids = documentIds;
      if (noteIds.length) payload.note_ids = noteIds;
      if (artifactIds.length) payload.artifact_ids = artifactIds;
      // 可选来源全部加载失败时退化为「仅主题」提交，由服务端 422 中文原因兜底；
      // 否则前端先拦一次，避免为无来源请求浪费一次生成。
      if (sourceState.anyLoaded && !documentIds.length && !noteIds.length && !artifactIds.length) {
        throw new Error(sourceState.candidateCount ? m6t('reportSourcesRequired') : m6t('noSourcesAvailable'));
      }
      return payload;
    }

    async function submit() {
      if (!sourceState.loaded || submitButton.disabled) return;
      errorBox.classList.add('hide');
      errorBox.textContent = '';
      let payload;
      try {
        payload = buildPayload();
      } catch (error) {
        showError(m6ErrorText(error));
        return;
      }
      m6SetBusy(submitButton, true);
      statusBox.replaceChildren(m6Progress(m6t('creatingReport')));
      try {
        const result = await api('/topic-reports', { method: 'POST', data: payload });
        if (!result || !result.report || !result.report.report_id) {
          throw new Error(m6t('unexpectedResponse'));
        }
        shell.close();
        toast(m6t('reportCreated'));
        void load();
        openTopicReportDetail(result);
      } catch (error) {
        statusBox.replaceChildren();
        showError(m6ErrorText(error));
      } finally {
        if (submitButton.isConnected) m6SetBusy(submitButton, false);
      }
    }

    topicInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); void submit(); }
    });
    titleInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); void submit(); }
    });

    void loadSources();
  }

  /* ---- 详情 / 编辑弹层 ---- */

  function openTopicReportDetail(initial, reportId = '') {
    if (destroyed) return;
    const shell = m6DialogShell({ className: 'm6-report-dialog', label: m6t('topicReportsTitle') });
    openDialogs.add(shell);
    shell.setCloseHandler(() => openDialogs.delete(shell));

    let detail = initial && initial.report ? initial : null;
    let viewNode = null;

    const titleNode = el('h3', { class: 'm6-dialog-title', 'data-no-i18n': '' });
    const metaNode = el('div', { class: 'm6-dialog-meta' });
    const bodyHost = el('div', { class: 'm6-dialog-body m6-report-body' });
    const errorBox = el('div', { class: 'm6-dialog-error hide', role: 'alert' });
    const editButton = button(m6t('editReport'), () => { enterEditMode(); }, 'secondary small', 'edit');
    const downloadButton = button(m6t('downloadMarkdown'), event => {
      if (detail) void downloadReportMarkdown(detail.report, event.currentTarget);
    }, 'ghost small', 'download');

    shell.head.append(
      el('div', { class: 'm6-dialog-title-wrap' }, titleNode, metaNode),
      button(t('common.close'), () => shell.close(), 'ghost small', 'close')
    );
    shell.body.append(errorBox, bodyHost);
    shell.footer.append(editButton, downloadButton);
    shell.mount();

    function refreshHead() {
      if (!detail) return;
      const report = detail.report;
      titleNode.textContent = m6ReportTitle(report);
      const version = Number(report.version) || 0;
      metaNode.replaceChildren(
        m6Trimmed(report.generator) === 'rules' ? badge(m6t('generatorRules'), 'teal') : null,
        version ? el('span', {}, m6VersionText(version)) : null,
        report.topic ? el('span', {}, m6t('topicLabel', { topic: m6Trimmed(report.topic) })) : null,
        report.updated_at ? el('span', {}, t('common.updatedAt', { time: dateText(report.updated_at) })) : null
      );
    }

    function renderView() {
      if (!detail) return;
      const contentMd = m6Text(detail.content_md);
      const contentNode = el('div', { class: 'm6-report-content' });
      if (contentMd.trim()) contentNode.append(markdown(contentMd));
      else contentNode.append(notice(m6t('reportNoContent'), 'warning'));
      viewNode = el('div', { class: 'm6-report-view' }, contentNode, reportSourcesNode(detail.sources));
      bodyHost.replaceChildren(viewNode);
      editButton.disabled = false;
      downloadButton.disabled = false;
      errorBox.classList.add('hide');
    }

    function reportSourcesNode(sources) {
      const list = (Array.isArray(sources) ? sources : [])
        .filter(source => source && typeof source === 'object');
      if (!list.length) {
        return el('div', { class: 'm6-report-sources' },
          el('p', { class: 'help' }, m6t('reportSourcesEmpty')));
      }
      const details = el('details', { class: 'm6-sources' },
        el('summary', {}, m6t('reportSourcesTitle', { count: list.length })));
      for (const source of list) details.append(reportSourceNode(source));
      return details;
    }

    function reportSourceNode(source) {
      const kindText = m6SourceKindLabel(source.kind);
      const titleText = m6Trimmed(source.title) || kindText;
      const meta = [];
      if (Number(source.page) > 0) meta.push(m6t('sourcePage', { page: Number(source.page) }));
      if (m6Trimmed(source.chunk_id)) meta.push(m6t('sourceChunk', { chunk: m6Trimmed(source.chunk_id) }));
      if (Number(source.version) > 0) meta.push(m6t('sourceVersion', { version: Number(source.version) }));
      if (m6Trimmed(source.ref)) meta.push(m6t('sourceRef', { ref: m6Trimmed(source.ref) }));
      const quote = m6Trimmed(source.quote);
      return el('article', { class: 'm6-source', 'data-source-kind': m6Text(source.kind) },
        el('div', { class: 'm6-source-head' },
          badge(kindText, 'teal'),
          el('strong', { class: 'm6-source-title', 'data-no-i18n': '' }, titleText),
          meta.length ? el('span', { class: 'm6-source-meta' }, meta.join(' · ')) : null),
        quote ? el('blockquote', { class: 'm6-source-quote', 'data-no-i18n': '' }, quote) : null);
    }

    function enterEditMode() {
      if (!detail) return;
      errorBox.classList.add('hide');
      const report = detail.report;
      const titleField = input(m6t('reportTitlePlaceholder'), {
        value: m6Trimmed(report.title),
        maxlength: 120,
      });
      const contentArea = el('textarea', {
        class: 'input m6-editor',
        rows: 16,
        value: m6Text(detail.content_md),
      });
      const saveButton = button(m6t('saveReport'), () => { void save(); }, '', 'check');
      const cancelButton = button(t('common.cancel'), () => { renderView(); }, 'secondary small');
      bodyHost.replaceChildren(el('div', { class: 'm6-report-edit' },
        field(m6t('reportTitleLabel'), titleField),
        field(m6t('reportContentLabel'), contentArea, m6t('reportContentHint')),
        el('div', { class: 'row end m6-edit-actions' }, cancelButton, saveButton)));
      editButton.disabled = true;
      downloadButton.disabled = true;
      titleField.focus();

      async function save() {
        if (saveButton.disabled) return;
        errorBox.classList.add('hide');
        errorBox.textContent = '';
        const payload = { content_md: contentArea.value };
        const nextTitle = titleField.value.trim();
        if (nextTitle) payload.title = nextTitle;
        const version = Number(report.version);
        if (Number.isFinite(version) && version > 0) payload.expected_version = version;
        m6SetBusy(saveButton, true);
        try {
          const data = await m6RequestJson(
            `/topic-reports/${encodeURIComponent(m6Text(report.report_id))}`,
            { method: 'PATCH', data: payload }
          );
          if (!data || !data.report) throw new Error(m6t('unexpectedResponse'));
          detail = data;
          refreshHead();
          renderView();
          toast(m6t('reportSaved'));
          void load();
        } catch (error) {
          if (error && error.conflict) {
            toast(m6t('reportConflict'), true);
            try {
              const fresh = await api(`/topic-reports/${encodeURIComponent(m6Text(report.report_id))}`);
              if (fresh && fresh.report) {
                detail = fresh;
                refreshHead();
                renderView();
              }
            } catch {
              // 重新拉取也失败时保留编辑内容，交给用户自行刷新，不覆盖已输入文本。
              errorBox.textContent = m6t('reportConflict');
              errorBox.classList.remove('hide');
            }
          } else {
            errorBox.textContent = m6ErrorText(error);
            errorBox.classList.remove('hide');
          }
          if (saveButton.isConnected) m6SetBusy(saveButton, false);
        }
      }
    }

    async function loadDetail() {
      bodyHost.replaceChildren(m6Progress(t('common.loading')));
      editButton.disabled = true;
      downloadButton.disabled = true;
      try {
        const data = await api(`/topic-reports/${encodeURIComponent(reportId)}`);
        if (!shell.dialog.isConnected) return;
        if (!data || !data.report) throw new Error(m6t('unexpectedResponse'));
        detail = data;
        refreshHead();
        renderView();
      } catch (error) {
        if (!shell.dialog.isConnected) return;
        bodyHost.replaceChildren(notice(m6t('reportReadFailed', { reason: m6ErrorText(error) }), 'error'));
      }
    }

    if (detail) {
      refreshHead();
      renderView();
    } else {
      void loadDetail();
    }
  }

  /* ---- 生命周期 ---- */

  const onHashChange = () => {
    window.setTimeout(() => {
      if (!host.isConnected) destroy();
    }, 0);
  };
  window.addEventListener('hashchange', onHashChange);

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    listSeq += 1;
    window.removeEventListener('hashchange', onHashChange);
    for (const shell of [...openDialogs]) {
      try { shell.close(); } catch { shell.dialog.remove(); }
      try { shell.dialog.remove(); } catch { /* 已移除 */ }
    }
    openDialogs.clear();
    host.remove();
  }

  void load();

  return { element: host, refresh: load, destroy };
}

/* ===========================================================================
 * 5. 互动讲义面板
 * =========================================================================== */

/**
 * 渲染互动讲义区块。
 * @param {{heading?: boolean}} [options] 同专题报告面板。
 * @returns {{element: HTMLElement, refresh: () => Promise<void>, destroy: () => void}}
 */
export function renderHandoutsPanel(options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const showHeading = opts.heading !== false;
  const openDialogs = new Set();
  let destroyed = false;
  let listSeq = 0;

  const statusHost = el('div', { class: 'm6-panel-status' });
  const listHost = el('div', { class: 'm6-card-list m6-handout-list' });
  const createButton = button(m6t('newHandout'), () => { void openCreateDialog(); }, '', 'plus');
  createButton.classList.add('m6-create');
  const refreshButton = button(t('common.reload'), event => {
    void busy(event.currentTarget, () => load());
  }, 'ghost small', 'rotate');
  refreshButton.classList.add('m6-refresh');

  const host = el('section', {
    class: 'm6-panel m6-panel-handouts',
    'aria-label': m6t('handoutsTitle'),
  });
  host.append(
    el('div', { class: 'm6-panel-head' },
      el('div', { class: 'm6-panel-head-text' },
        showHeading ? el('p', { class: 'm6-kicker' }, m6t('generatorRules')) : null,
        showHeading ? el('h3', { class: 'm6-panel-title' }, m6t('handoutsTitle')) : null,
        el('p', { class: 'm6-panel-desc' }, m6t('handoutsDesc'))),
      el('div', { class: 'm6-panel-actions' }, createButton, refreshButton)),
    statusHost,
    listHost
  );

  /* ---- 列表状态 ---- */

  function setDegraded(message) {
    host.classList.add('is-degraded');
    createButton.classList.add('hide');
    createButton.hidden = true;
    listHost.replaceChildren();
    statusHost.replaceChildren(
      notice(message, 'warning'),
      el('div', { class: 'm6-panel-retry' },
        button(t('common.retry'), event => { void busy(event.currentTarget, () => load()); }, 'soft small', 'rotate'))
    );
  }

  function clearDegraded() {
    host.classList.remove('is-degraded');
    createButton.classList.remove('hide');
    createButton.hidden = false;
    statusHost.replaceChildren();
  }

  async function load() {
    if (destroyed) return;
    const mine = ++listSeq;
    statusHost.replaceChildren(m6Progress(m6t('loadingHandouts')));
    try {
      const result = await api(`/handouts?limit=${HANDOUT_LIST_LIMIT}`);
      if (destroyed || mine !== listSeq) return;
      clearDegraded();
      const items = (result && Array.isArray(result.items) ? result.items : [])
        .filter(item => item && typeof item === 'object' && item.handout_id);
      renderList(items);
    } catch (error) {
      if (destroyed || mine !== listSeq) return;
      const reason = m6Trimmed(error && error.message);
      setDegraded(reason ? `${m6t('handoutsUnavailable')}（${reason}）` : m6t('handoutsUnavailable'));
    }
  }

  function renderList(items) {
    listHost.replaceChildren();
    if (!items.length) {
      listHost.append(empty(
        m6t('handoutsEmptyTitle'),
        m6t('handoutsEmptyText'),
        button(m6t('newHandout'), () => { void openCreateDialog(); }, 'soft small', 'plus'),
        'layers'
      ));
      return;
    }
    listHost.append(...items.map(handoutCardNode));
  }

  function handoutCardNode(handout) {
    const handoutId = m6Text(handout.handout_id);
    const version = Number(handout.version) || 0;
    const sectionCount = Number(handout.section_count) || 0;
    const meta = el('div', { class: 'm6-card-meta' },
      m6Trimmed(handout.generator) === 'rules' ? badge(m6t('generatorRules'), 'teal') : null,
      version ? el('span', {}, m6VersionText(version)) : null,
      sectionCount ? el('span', {}, m6t('sectionCount', { count: sectionCount })) : null,
      handout.updated_at ? el('span', {}, t('common.updatedAt', { time: dateText(handout.updated_at) })) : null);
    const actions = el('div', { class: 'm6-card-actions' },
      button(m6t('open'), () => { openHandoutDetail({ handout }, handoutId); }, 'secondary small', 'eye'),
      button(m6t('exportHtml'), event => {
        void exportHandout(handout, 'html', event.currentTarget);
      }, 'ghost small', 'download'),
      button(m6t('exportPdf'), event => {
        void exportHandout(handout, 'pdf', event.currentTarget);
      }, 'ghost small', 'printer'),
      button(t('common.delete'), event => busy(event.currentTarget, async () => {
        if (!window.confirm(m6t('deleteHandoutConfirm'))) return;
        await api(`/handouts/${encodeURIComponent(handoutId)}`, { method: 'DELETE' });
        toast(m6t('handoutDeleted'));
        await load();
      }), 'danger small', 'trash'));
    return el('article', { class: 'm6-card m6-handout-card', 'data-handout-id': handoutId },
      el('div', { class: 'm6-card-head' },
        el('div', { class: 'm6-card-title-wrap' },
          el('h3', { class: 'm6-card-title', 'data-no-i18n': '' }, m6HandoutTitle(handout)))),
      meta,
      actions);
  }

  /* ---- 导出 ---- */

  async function exportHandout(handout, format, trigger) {
    const handoutId = m6Text(handout && handout.handout_id);
    if (!handoutId) return;
    m6SetBusy(trigger, true);
    try {
      const blob = await m6FetchBlob(
        `/handouts/${encodeURIComponent(handoutId)}/export?format=${encodeURIComponent(format)}`
      );
      const version = Number(handout.version) || 1;
      downloadBlob(blob, m6FileName(m6HandoutTitle(handout), `v${version}.${format}`));
    } catch (error) {
      if (format === 'pdf' && (error && (error.status === 503 || /pdf|依赖/i.test(m6ErrorText(error))))) {
        toast(m6t('exportPdfUnavailable'), true);
      } else {
        toast(m6t('exportFailed', { reason: m6ErrorText(error) }), true);
      }
    } finally {
      if (trigger && trigger.isConnected) m6SetBusy(trigger, false);
    }
  }

  /* ---- 创建弹层 ---- */

  function openCreateDialog() {
    if (destroyed) return;
    const shell = m6DialogShell({ className: 'm6-handout-create', label: m6t('newHandout') });
    openDialogs.add(shell);
    shell.setCloseHandler(() => openDialogs.delete(shell));

    const titleInput = input(m6t('handoutTitlePlaceholder'), {
      maxlength: 120,
      autocomplete: 'off',
      autofocus: true,
    });
    const sourcesHost = el('div', { class: 'm6-source-groups' });
    const errorBox = el('div', { class: 'm6-dialog-error hide', role: 'alert' });
    const statusBox = el('div', { class: 'm6-dialog-status', role: 'status', 'aria-live': 'polite' });
    const submitButton = button(m6t('createHandout'), () => { void submit(); }, '', 'sparkle');
    const sourceState = { loaded: false, anyLoaded: false, candidateCount: 0, course: null, checks: {} };

    shell.head.append(
      el('div', { class: 'm6-dialog-title-wrap' },
        el('h3', { class: 'm6-dialog-title' }, m6t('newHandout')),
        el('p', { class: 'm6-dialog-sub' }, m6t('handoutsDesc'))),
      button(t('common.close'), () => shell.close(), 'ghost small', 'close')
    );
    shell.body.append(
      field(m6t('handoutTitleLabel'), titleInput),
      el('div', { class: 'm6-source-section-head' },
        el('div', { class: 'field-label' }, m6t('handoutSources')),
        el('p', { class: 'help' }, m6t('handoutSourcesHint'))),
      sourcesHost,
      errorBox,
      statusBox
    );
    shell.footer.append(submitButton);
    shell.mount();

    function showError(message) {
      errorBox.textContent = message;
      errorBox.classList.remove('hide');
    }

    function buildCourseGroup(items) {
      const selectNode = select(
        [
          { value: '', label: m6t('handoutCourseNone') },
          ...items.map(course => ({
            value: m6Text(course.id),
            label: m6Trimmed(course.title) || m6Text(course.id),
          })),
        ],
        '',
        { class: 'input m6-course-select', 'aria-label': m6t('handoutCourseLabel') }
      );
      return {
        selectNode,
        node: el('section', { class: 'm6-source-group', 'data-source-group': 'courses' },
          el('div', { class: 'm6-source-group-head' },
            el('h4', { class: 'm6-source-group-title' }, m6t('handoutCourseLabel')),
            el('span', { class: 'm6-source-group-count' }, t('common.countItems', { count: items.length }))),
          items.length
            ? selectNode
            : el('p', { class: 'help m6-source-empty' },
              m6t('sourceEmpty', { group: m6t('sourceGroupCourses') }))),
      };
    }

    async function loadSources() {
      sourceState.loaded = false;
      m6SetBusy(submitButton, true);
      sourcesHost.replaceChildren(m6Progress(m6t('sourcesLoading')));
      const groups = await m6LoadGroups([
        SOURCE_GROUP_DEFS.courses,
        SOURCE_GROUP_DEFS.notes,
        SOURCE_GROUP_DEFS.artifacts,
      ]);
      if (!shell.dialog.isConnected) return;
      const nodes = [];
      const failed = [];
      const checks = {};
      let anyLoaded = false;
      let candidateCount = 0;
      let courseSelectNode = null;
      for (const group of groups) {
        if (!group.ok) {
          failed.push(m6t(group.def.groupKey));
          continue;
        }
        anyLoaded = true;
        candidateCount += group.items.length;
        if (group.def.key === 'courses') {
          const courseGroup = buildCourseGroup(group.items);
          courseSelectNode = courseGroup.selectNode;
          nodes.push(courseGroup.node);
        } else {
          const isArtifacts = group.def.key === 'artifacts';
          const checklist = m6ChecklistGroup(group.def, group.items, isArtifacts
            ? {
              limit: MAX_HANDOUT_ARTIFACTS,
              limitMessage: m6t('handoutArtifactsLimit', { count: MAX_HANDOUT_ARTIFACTS }),
            }
            : {});
          checks[group.def.key] = checklist.selected;
          nodes.push(checklist.node);
        }
      }
      sourceState.loaded = true;
      sourceState.anyLoaded = anyLoaded;
      sourceState.candidateCount = candidateCount;
      sourceState.course = courseSelectNode;
      sourceState.checks = checks;
      const children = [...nodes];
      if (failed.length) {
        children.push(notice(m6t('sourcesUnavailable', { group: failed.join('、') }), 'warning'));
      }
      if (!anyLoaded) {
        children.push(notice(m6t('sourcesAllUnavailable'), 'warning'));
        children.push(el('div', { class: 'm6-panel-retry' },
          button(t('common.retry'), () => { void loadSources(); }, 'soft small', 'rotate')));
      }
      sourcesHost.replaceChildren(...children);
      m6SetBusy(submitButton, false);
    }

    function buildPayload() {
      const payload = {};
      const title = titleInput.value.trim();
      if (title) payload.title = title;
      const courseId = Number(sourceState.course ? sourceState.course.value : 0);
      if (Number.isFinite(courseId) && courseId > 0) payload.course_id = courseId;
      const noteIds = (sourceState.checks.notes ? sourceState.checks.notes() : [])
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0);
      const artifactIds = (sourceState.checks.artifacts ? sourceState.checks.artifacts() : [])
        .map(value => m6Text(value))
        .filter(Boolean);
      if (artifactIds.length > MAX_HANDOUT_ARTIFACTS) {
        throw new Error(m6t('handoutArtifactsLimit', { count: MAX_HANDOUT_ARTIFACTS }));
      }
      if (noteIds.length) payload.note_ids = noteIds;
      if (artifactIds.length) payload.artifact_ids = artifactIds;
      if (sourceState.anyLoaded && !payload.course_id && !noteIds.length && !artifactIds.length) {
        throw new Error(sourceState.candidateCount ? m6t('handoutSourcesRequired') : m6t('noSourcesAvailable'));
      }
      return payload;
    }

    async function submit() {
      if (!sourceState.loaded || submitButton.disabled) return;
      errorBox.classList.add('hide');
      errorBox.textContent = '';
      let payload;
      try {
        payload = buildPayload();
      } catch (error) {
        showError(m6ErrorText(error));
        return;
      }
      m6SetBusy(submitButton, true);
      statusBox.replaceChildren(m6Progress(m6t('creatingHandout')));
      try {
        const result = await api('/handouts', { method: 'POST', data: payload });
        if (!result || !result.handout || !result.handout.handout_id) {
          throw new Error(m6t('unexpectedResponse'));
        }
        shell.close();
        toast(m6t('handoutCreated'));
        void load();
        openHandoutDetail(result);
      } catch (error) {
        statusBox.replaceChildren();
        showError(m6ErrorText(error));
      } finally {
        if (submitButton.isConnected) m6SetBusy(submitButton, false);
      }
    }

    titleInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); void submit(); }
    });

    void loadSources();
  }

  /* ---- 详情弹层 ---- */

  function openHandoutDetail(initial, handoutId = '') {
    if (destroyed) return;
    const shell = m6DialogShell({ className: 'm6-handout-dialog', label: m6t('handoutDetailTitle') });
    openDialogs.add(shell);
    shell.setCloseHandler(() => openDialogs.delete(shell));

    let currentHandout = null;
    const titleNode = el('h3', { class: 'm6-dialog-title', 'data-no-i18n': '' });
    const metaNode = el('div', { class: 'm6-dialog-meta' });
    const bodyHost = el('div', { class: 'm6-dialog-body m6-handout-body' });
    const errorBox = el('div', { class: 'm6-dialog-error hide', role: 'alert' });
    const exportHtmlButton = button(m6t('exportHtml'), event => {
      if (currentHandout) void exportHandout(currentHandout, 'html', event.currentTarget);
    }, 'secondary small', 'download');
    const exportPdfButton = button(m6t('exportPdf'), event => {
      if (currentHandout) void exportHandout(currentHandout, 'pdf', event.currentTarget);
    }, 'secondary small', 'printer');

    shell.head.append(
      el('div', { class: 'm6-dialog-title-wrap' }, titleNode, metaNode),
      button(t('common.close'), () => shell.close(), 'ghost small', 'close')
    );
    shell.body.append(errorBox, bodyHost);
    shell.footer.append(exportHtmlButton, exportPdfButton);
    shell.mount();

    function refreshHead(handout) {
      currentHandout = handout || null;
      titleNode.textContent = m6HandoutTitle(handout);
      const version = Number(handout && handout.version) || 0;
      metaNode.replaceChildren(
        m6Trimmed(handout && handout.generator) === 'rules' ? badge(m6t('generatorRules'), 'teal') : null,
        version ? el('span', {}, m6VersionText(version)) : null,
        handout && handout.updated_at
          ? el('span', {}, t('common.updatedAt', { time: dateText(handout.updated_at) }))
          : null
      );
    }

    function render(data) {
      if (!data || !data.handout) throw new Error(m6t('unexpectedResponse'));
      refreshHead(data.handout);
      const sections = (Array.isArray(data.sections) ? data.sections : [])
        .filter(section => section && typeof section === 'object');
      const children = [el('p', { class: 'm6-rules-note' }, m6t('handoutRulesNotice'))];
      if (!sections.length) {
        children.push(notice(m6t('handoutSectionsEmpty'), 'warning'));
      } else {
        children.push(el('div', { class: 'm6-handout-sections-head' },
          el('h4', { class: 'm6-handout-sections-title' }, m6t('handoutSectionsTitle', { count: sections.length }))));
        children.push(el('div', { class: 'm6-handout-sections' },
          ...sections.map(handoutSectionNode)));
      }
      bodyHost.replaceChildren(...children);
      exportHtmlButton.disabled = false;
      exportPdfButton.disabled = false;
    }

    async function loadDetail(id) {
      bodyHost.replaceChildren(m6Progress(t('common.loading')));
      exportHtmlButton.disabled = true;
      exportPdfButton.disabled = true;
      try {
        const data = await api(`/handouts/${encodeURIComponent(id)}`);
        if (!shell.dialog.isConnected) return;
        render(data);
      } catch (error) {
        if (!shell.dialog.isConnected) return;
        bodyHost.replaceChildren(notice(m6t('handoutReadFailed', { reason: m6ErrorText(error) }), 'error'));
      }
    }

    if (initial && Array.isArray(initial.sections)) {
      try {
        render(initial);
      } catch (error) {
        bodyHost.replaceChildren(notice(m6ErrorText(error), 'error'));
      }
    } else {
      const id = m6Text(handoutId) || m6Text(initial && initial.handout && initial.handout.handout_id);
      if (id) void loadDetail(id);
      else bodyHost.replaceChildren(notice(m6t('unexpectedResponse'), 'error'));
    }
  }

  /* ---- 小节渲染 ---- */

  function handoutSectionNode(section) {
    const kindText = m6SectionKindLabel(section.kind);
    const titleText = m6Trimmed(section.title);
    const children = [
      el('div', { class: 'm6-section-head' },
        el('span', { class: 'm6-section-kind', 'data-kind': m6Text(section.kind) }, kindText),
        titleText ? el('h4', { class: 'm6-section-title', 'data-no-i18n': '' }, titleText) : null),
    ];
    const paragraphs = (Array.isArray(section.paragraphs) ? section.paragraphs : [])
      .map(m6Trimmed)
      .filter(Boolean);
    for (const paragraph of paragraphs) {
      children.push(el('p', { class: 'm6-section-paragraph', 'data-no-i18n': '' }, paragraph));
    }
    const quotes = (Array.isArray(section.quotes) ? section.quotes : [])
      .filter(quote => quote && typeof quote === 'object');
    for (const quote of quotes) {
      const text = m6Trimmed(quote.text) || m6Trimmed(quote.quote);
      if (!text) continue;
      const source = m6Trimmed(quote.source);
      children.push(el('blockquote', { class: 'm6-section-quote' },
        el('span', { class: 'm6-section-quote-text', 'data-no-i18n': '' }, text),
        source ? el('cite', { class: 'm6-section-quote-source', 'data-no-i18n': '' }, source) : null));
    }
    const items = (Array.isArray(section.items) ? section.items : [])
      .filter(item => item && typeof item === 'object');
    if (items.length) {
      children.push(el('ul', { class: 'm6-section-items' }, ...items.map(handoutItemNode)));
    }
    if (!paragraphs.length && !quotes.length && !items.length) {
      children.push(el('p', { class: 'help' }, m6t('handoutSectionEmpty')));
    }
    return el('section', {
      class: 'm6-handout-section',
      'data-section-kind': m6Text(section.kind),
    }, ...children);
  }

  function handoutItemNode(item) {
    const children = [];
    const titleText = m6Trimmed(item.title);
    const detailText = m6Trimmed(item.detail);
    if (titleText) children.push(el('strong', { class: 'm6-section-item-title', 'data-no-i18n': '' }, titleText));
    if (detailText) children.push(el('p', { class: 'm6-section-item-detail', 'data-no-i18n': '' }, detailText));
    const ref = m6Trimmed(item.ref);
    if (ref) children.push(el('span', { class: 'm6-section-item-ref', 'data-no-i18n': '' }, ref));
    const preview = m6PreviewImage(item.preview_data_uri, titleText);
    if (preview) children.push(preview);
    return el('li', { class: 'm6-section-item' }, ...children);
  }

  /* ---- 生命周期 ---- */

  const onHashChange = () => {
    window.setTimeout(() => {
      if (!host.isConnected) destroy();
    }, 0);
  };
  window.addEventListener('hashchange', onHashChange);

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    listSeq += 1;
    window.removeEventListener('hashchange', onHashChange);
    for (const shell of [...openDialogs]) {
      try { shell.close(); } catch { shell.dialog.remove(); }
      try { shell.dialog.remove(); } catch { /* 已移除 */ }
    }
    openDialogs.clear();
    host.remove();
  }

  void load();

  return { element: host, refresh: load, destroy };
}
