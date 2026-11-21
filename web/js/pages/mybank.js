/* 我的题库：应用内可写题库（/api/paperlab/questions）的检索、编辑、核验与 AI 补全。
 *
 * - 列表：GET /api/paperlab/meta 取选项；GET /api/paperlab/questions 分页 + 关键词 LIKE 筛选；
 * - 编辑：PATCH /api/paperlab/questions/{id}（带 version CAS，409 时保留未保存内容并提示刷新）；
 * - 核验：POST /api/paperlab/questions/{id}/verify；DELETE 软删除；
 * - AI 补全：勾选多题 → core/jobs.js mountJob 展示「补全 N/M 题」进度/取消/失败重试；
 *   已核验题目由后端跳过，不会被覆盖。
 */
import { api, isAborted, refreshProviders } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, heading, button, input, select, field, badge, notice, empty, loading, busy,
  toast, number, dateText, query,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { openQuestionEditor, typeLabel } from '../components/paperQuestionForm.js';
import { t } from '../core/i18n.js';

// 后端/用户数据容器：界面语言切换不翻译（题目、学科/年级、知识点标签等）。
function withNoI18n(node) {
  if (node && typeof node.setAttribute === 'function') node.setAttribute('data-no-i18n', '');
  return node;
}

const PAGE_SIZE = 12;
// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TYPE_FILTERS = [
  { value: '', get label() { return t('practice.allTypes'); } },
  { value: 'SINGLE', get label() { return t('question.type.single_choice'); } },
  { value: 'MULTIPLE', get label() { return t('question.type.multi_choice'); } },
  { value: 'BLANK', get label() { return t('question.type.fill_blank'); } },
  { value: 'SUBJECTIVE', get label() { return t('mybank.subjective'); } },
  { value: 'MIXED', get label() { return t('mybank.mixed'); } },
];
const STATUS_FILTERS = [
  { value: '', get label() { return t('practice.allStatuses'); } },
  { value: 'draft', get label() { return t('mybank.draft'); } },
  { value: 'ready', get label() { return t('admission.available'); } },
  { value: 'archived', get label() { return t('common.archived'); } },
];
const QUESTION_STATUS_LABELS = {
  get draft() { return t('mybank.draft'); },
  get ready() { return t('admission.available'); },
  get archived() { return t('common.archived'); },
};
const VERIFY_LABELS = {
  get pending() { return t('home.importStatus.ready'); },
  get verified() { return t('mybank.verified'); },
  get rejected() { return t('mybank.rejected'); },
};
const VERIFY_VARIANTS = { pending: 'amber', verified: 'teal', rejected: 'red' };
const DIFFICULTY_LABELS = [
  { max: 0.3, get label() { return t('practice.difficultyEasy'); } },
  { max: 0.45, get label() { return t('mybank.easier'); } },
  { max: 0.65, get label() { return t('practice.difficultyMedium'); } },
  { max: 0.85, get label() { return t('mybank.harder'); } },
  { max: 1.01, get label() { return t('practice.difficultyHard'); } },
];

let metaCache = null;
let page = 1;
let currentVersion = 0;
let loadingError = null;
let listState = { items: [], total: 0, page: 1, page_size: PAGE_SIZE };
let jobId = null;

function difficultyLabel(value) {
  if (value === null || value === undefined || value === '') return t('mybank.difficultyUnlabeled');
  const num = Number(value);
  if (!Number.isFinite(num)) return t('mybank.difficultyUnlabeled');
  return (DIFFICULTY_LABELS.find(item => num <= item.max) || DIFFICULTY_LABELS.at(-1)).label;
}

function filters() {
  if (!state.mybankFilters || typeof state.mybankFilters !== 'object') state.mybankFilters = {};
  return state.mybankFilters;
}

function selection() {
  if (!(state.mybankSelected instanceof Set)) state.mybankSelected = new Set();
  return state.mybankSelected;
}

onSessionReset(() => {
  metaCache = null;
  page = 1;
  currentVersion = 0;
  loadingError = null;
  listState = { items: [], total: 0, page: 1, page_size: PAGE_SIZE };
  jobId = null;
});

function searchQuery(extra = {}) {
  const f = filters();
  return query({
    subject: f.subject || '',
    grade: f.grade || '',
    qtype: f.qtype || '',
    keyword: f.keyword || '',
    status: f.status || '',
    page,
    page_size: PAGE_SIZE,
    ...extra,
  });
}

async function loadMeta() {
  if (metaCache) return metaCache;
  metaCache = await api('/paperlab/meta');
  return metaCache;
}

function questionStatusBadge(status) {
  const variant = status === 'ready' ? 'teal' : status === 'archived' ? '' : 'blue';
  return badge(QUESTION_STATUS_LABELS[status] || status || t('mybank.draft'), variant);
}

function verifyBadge(verifyStatus) {
  const status = verifyStatus || 'pending';
  return badge(VERIFY_LABELS[status] || status, VERIFY_VARIANTS[status] || '');
}

function answersSummary(question) {
  const content = question.content || {};
  const answers = Array.isArray(content.answers) ? content.answers.filter(Boolean) : [];
  if (answers.length) return answers.join('；');
  const parts = Array.isArray(content.parts) ? content.parts : [];
  const partAnswers = parts.flatMap(part => (Array.isArray(part.answers) ? part.answers : []))
    .map(String).filter(Boolean);
  return partAnswers.length ? partAnswers.join('；') : '';
}

function questionImages(question) {
  const images = question.content?.images;
  return Array.isArray(images) ? images.filter(image => image && image.url) : [];
}

/* ---------------------------------------------------------------------------
 * 来源与版本（Phase 1 剩余：个人收录固定版本 / 题库有新版）
 * ------------------------------------------------------------------------- */

const VERSION_ORIGIN_LABELS = {
  get create() { return t('mybank.version.origin.create'); },
  get edit() { return t('mybank.version.origin.edit'); },
  get collect() { return t('mybank.version.origin.collect'); },
  get source_update() { return t('mybank.version.origin.source_update'); },
  get snapshot() { return t('mybank.version.origin.snapshot'); },
};

function isBankCollected(question) {
  if (!question) return false;
  return question.source_kind === 'bank' || String(question.source_ref || '').startsWith('bank:');
}

function sourceBadge(question) {
  if (!question) return null;
  return isBankCollected(question)
    ? badge(t('mybank.version.sourceBank'), 'amber')
    : badge(t('mybank.version.sourcePersonal'), '');
}

function versionOriginLabel(origin) {
  return VERSION_ORIGIN_LABELS[origin] || String(origin || '');
}

function versionList(versionsPayload) {
  const items = Array.isArray(versionsPayload?.items) ? versionsPayload.items : [];
  if (!items.length) return el('p', { class: 'help' }, t('mybank.version.noVersions'));
  return el('ul', { class: 'mb-version-list', style: 'list-style:none;padding:0;margin:8px 0 0' },
    items.map(item => el('li', { style: 'padding:8px 0;border-top:1px solid var(--border,#e5e7eb)' },
      el('div', { class: 'row wrap', style: 'gap:6px;align-items:center' },
        badge(`v${number(item.version)}`, item.is_current ? 'teal' : 'blue'),
        item.is_current ? badge(t('mybank.version.current'), 'teal') : null,
        badge(versionOriginLabel(item.origin), ''),
        el('span', { class: 'help' }, dateText(item.created_at))),
      el('p', { class: 'help', style: 'margin:4px 0 0', 'data-no-i18n': '' },
        item.summary?.stem_preview || ''))));
}

async function openVersionDialog(question, containerRef, versionRef) {
  let status = null;
  let versionsPayload = null;
  try {
    [status, versionsPayload] = await Promise.all([
      api(`/paperlab/questions/${encodeURIComponent(question.id)}/source-status`),
      api(`/paperlab/questions/${encodeURIComponent(question.id)}/versions`),
    ]);
  } catch (error) {
    toast(error.message || t('mybank.version.loadFailed'), true);
    return;
  }
  const body = el('div', { class: 'mb-version-body' });
  body.append(el('div', { class: 'mb-version-meta' },
    el('div', { class: 'row wrap', style: 'gap:6px;align-items:center' },
      sourceBadge(question),
      badge(t('mybank.version.currentVersion', { v1: number(status.version || question.version || 1) }), 'blue'),
      status.source_available === false ? badge(t('mybank.version.sourceUnavailable'), 'red') : null),
    status.source_ref
      ? el('p', { class: 'help', 'data-no-i18n': '' }, t('mybank.version.sourceRef', { v1: status.source_ref }))
      : null,
    status.collected_at
      ? el('p', { class: 'help' }, t('mybank.version.collectedAt', { v1: dateText(status.collected_at) }))
      : null,
    status.collected_revision
      ? el('p', { class: 'help', 'data-no-i18n': '' }, t('mybank.version.collectedRevision', { v1: status.collected_revision }))
      : null,
    status.current_revision
      ? el('p', { class: 'help', 'data-no-i18n': '' }, t('mybank.version.currentRevision', { v1: status.current_revision }))
      : null));

  if (!isBankCollected(question)) {
    body.append(notice(t('mybank.version.upToDate'), ''));
  } else if (status.source_available === false) {
    body.append(notice(
      status.source_restricted ? t('mybank.version.sourceRestricted') : t('mybank.version.sourceUnavailable'),
      'warning'));
  } else if (status.has_new_version) {
    const changed = Array.isArray(status.diff?.changed) ? status.diff.changed : [];
    body.append(notice(t('mybank.version.hasNewVersion'), 'warning'));
    body.append(el('p', { class: 'help' }, changed.length
      ? t('mybank.version.changedFields', { v1: changed.join('、') })
      : t('mybank.version.noChangedFields')));
    if (status.local_edited) body.append(el('p', { class: 'help' }, t('mybank.version.localEdited')));
    body.append(el('p', { class: 'help' }, t('mybank.version.updateConfirm')));
  } else {
    body.append(notice(t('mybank.version.upToDate'), ''));
  }

  body.append(el('h3', { style: 'margin:16px 0 0' }, t('mybank.version.versionList')));
  body.append(versionList(versionsPayload));
  body.append(el('p', { class: 'help', style: 'margin:10px 0 0' }, t('mybank.version.contentReadonly')));

  const actions = el('div', { class: 'row end', style: 'gap:8px;margin-top:16px' });
  if (isBankCollected(question) && status.source_available !== false && status.has_new_version) {
    const updateBtn = button(t('mybank.version.updateToNew'), null, '', 'download');
    updateBtn.addEventListener('click', event => busy(event.currentTarget, async () => {
      const result = await api(`/paperlab/questions/${encodeURIComponent(question.id)}/update-from-source`, {
        method: 'POST',
        data: { confirm: true, expected_version: question.version },
      });
      if (result.updated) {
        toast(t('mybank.version.updated', { v1: number(result.version || '') }));
        dialog.close();
        await reload(containerRef, versionRef);
      } else {
        toast(result.notice || t('mybank.version.upToDate'), false);
      }
    }));
    actions.append(updateBtn);
  }
  actions.append(button(
    status.has_new_version && status.source_available !== false
      ? t('mybank.version.keepOld')
      : t('common.close'),
    () => dialog.close(), 'secondary'));

  const dialog = el('dialog', { class: 'card mb-version-dialog', 'aria-label': t('mybank.version.dialogTitle'),
    style: 'max-width:640px;width:calc(100% - 32px);padding:20px' },
  el('div', { class: 'card-header' },
    el('div', {}, el('h3', {}, t('mybank.version.dialogTitle'))),
    button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
  body,
  actions);
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

/* ---------------------------------------------------------------------------
 * 页面骨架 + 列表
 * ------------------------------------------------------------------------- */

export async function renderMyBank(container, version, params = null) {
  const currentRun = ++currentVersion;
  container.replaceChildren(
    heading('QUESTION LIBRARY', t('home.status.bankTotal'),
      t('mybank.questionsFromRecognitionImportsOrYourOwn')),
    loading(t('mybank.loadingYourBank')));

  let meta = null;
  let providersReady = true;
  try {
    meta = await loadMeta();
    if (!(state.providers || []).length) {
      await refreshProviders().catch(() => { providersReady = false; });
    }
  } catch (error) {
    if (currentRun !== currentVersion || !current(version)) return;
    container.replaceChildren(
      heading('QUESTION LIBRARY', t('home.status.bankTotal'), t('mybank.theBankIsTemporarilyUnavailable')),
      el('div', { class: 'error-panel' },
        error.message || t('mybank.failedToLoadYourBankPleaseTry'),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.reload'), () => renderMyBank(container, version, params), '', 'repeat'))));
    return;
  }
  if (currentRun !== currentVersion || !current(version)) return;

  page = Math.max(1, page);
  selection().clear();
  const body = el('div', { class: 'mb-page' });
  const statsHost = el('section', { class: 'card mb-stats-card', 'aria-live': 'polite' });
  const listHost = el('div', { class: 'mb-list' });
  const toolbarHost = el('div', { class: 'mb-toolbar' });
  const filterHost = el('div', { class: 'card mb-filter-card' });
  const selectionHost = el('div', { class: 'mb-selection-bar', hidden: true });
  const paginationHost = el('div', { class: 'pagination mb-pagination', hidden: true });
  const enrichCard = el('section', { class: 'card mb-enrich-card', hidden: true });
  const jobHost = el('div', { class: 'mb-job-host' });
  const jobSummaryHost = el('div', { class: 'mb-job-summary' });
  body.append(statsHost, filterHost, toolbarHost, selectionHost, enrichCard, jobSummaryHost, listHost, paginationHost);
  container.replaceChildren(
    heading('QUESTION LIBRARY', t('home.status.bankTotal'),
      t('mybank.questionsFromRecognitionImportsOrYourOwn_2'),
      button(t('mybank.refreshBank'), event => busy(event.currentTarget, () => reload(container, version)),
        'secondary', 'repeat')),
    body);

  const filtersState = filters();
  const filterCountBadge = badge(t('mybank.0Questions'), 'teal');
  const keywordInput = input(t('mybank.searchStemsOrKnowledgePointsEG'), { value: filtersState.keyword || '', maxlength: '120' });
  const subjectSelect = select(
    [{ value: '', label: t('insight.subjectAll') }, ...(meta.subjects || []).map(item => ({ value: item, label: item }))],
    filtersState.subject || '');
  const gradeSelect = select(
    [{ value: '', label: t('mybank.allGrades') }, ...(meta.grades || []).map(item => ({ value: item, label: item }))],
    filtersState.grade || '');
  const typeSelect = select(TYPE_FILTERS, filtersState.qtype || '');
  const statusSelect = select(STATUS_FILTERS, filtersState.status || '');
  function applyFilterSelection() {
    filtersState.keyword = keywordInput.value.trim();
    filtersState.subject = subjectSelect.value;
    filtersState.grade = gradeSelect.value;
    filtersState.qtype = typeSelect.value;
    filtersState.status = statusSelect.value;
  }

  function clearFilterSelection() {
    // 注意：必须原地清空 filtersState（统计区也引用它），不能只替换 state.mybankFilters。
    Object.keys(filtersState).forEach(key => delete filtersState[key]);
    state.mybankFilters = filtersState;
    keywordInput.value = '';
    subjectSelect.value = '';
    gradeSelect.value = '';
    typeSelect.value = '';
    statusSelect.value = '';
    page = 1;
    selection().clear();
  }

  const searchBtn = button(t('common.search'), event => busy(event.currentTarget, () => {
    applyFilterSelection();
    page = 1;
    selection().clear();
    return reload(container, version);
  }), '', 'search');
  const resetBtn = button(t('practice.clearFilters'), () => {
    clearFilterSelection();
    reload(container, version);
  }, 'secondary');
  keywordInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') searchBtn.click();
  });

  filterHost.append(
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('mybank.filterQuestions')),
        el('p', { class: 'help' }, t('mybank.keywordsUseLikeMatchingOnStemsAnd'))),
      filterCountBadge),
    el('div', { class: 'mb-filter-grid' },
      field(t('practice.keyword'), keywordInput),
      field(t('common.subject'), subjectSelect),
      field(t('mybank.grade'), gradeSelect),
      field(t('upload.fieldType'), typeSelect),
      field(t('mybank.status'), statusSelect)),
    el('div', { class: 'row end', style: 'margin-top:14px' }, resetBtn, searchBtn));

  /* -------------------------------------------------------------------------
   * 题库统计（实时聚合；学科/年级筛选生效）
   * ----------------------------------------------------------------------- */

  function statCell(label, value, caption) {
    return el('div', { class: 'mb-stat' },
      el('strong', { class: 'mb-stat-value' }, value),
      el('span', { class: 'mb-stat-label' }, label),
      caption ? el('small', { class: 'mb-stat-caption' }, caption) : null);
  }

  function renderStats(stats) {
    const byType = stats.by_type || {};
    const typeRows = TYPE_FILTERS.slice(1).map(item => ({
      value: item.value,
      label: item.label,
      count: Number(byType[item.value] || 0),
    }));
    const maxType = Math.max(1, ...typeRows.map(row => row.count));
    const knowledge = stats.knowledge || { total: 0, top: [] };
    const scope = `${filtersState.subject || t('insight.subjectAll')} · ${filtersState.grade || t('mybank.allGrades')}`;
    const byStatus = stats.by_status || {};
    const byVerify = stats.by_verify || {};
    const children = [
      el('div', { class: 'card-header' },
        el('div', {},
          el('h2', {}, t('mybank.bankStatistics')),
          el('p', { class: 'help' }, t('mybank.liveStatsForYourOwnBankRefreshed', { v1: scope }))),
        badge(t('home.activity.items', { count: number(stats.total || 0) }), 'teal')),
      el('div', { class: 'mb-stats-grid' },
        statCell(t('mybank.totalQuestions'), number(stats.total || 0),
          t('mybank.draftReadyArchived', { v1: number(byStatus.draft || 0), v2: number(byStatus.ready || 0), v3: number(byStatus.archived || 0) })),
        statCell(t('home.importStatus.ready'), number(byVerify.pending || 0),
          t('mybank.verifiedRejected', { v1: number(byVerify.verified || 0), v2: number(byVerify.rejected || 0) })),
        statCell(t('mybank.missingAnswer'), number(stats.missing_answers || 0), t('mybank.fillInAnswersBeforeGradingOrComposing')),
        statCell(t('mybank.missingSolution'), number(stats.missing_explanation || 0), t('mybank.selectQuestionsToFillInWithAi')),
        statCell(t('mybank.missingKnowledgePoint'), number(stats.missing_tags || 0), t('mybank.affectsCompositionQuotasAndLearningAnalytics')),
        statCell(t('mybank.addedInLast7Days'), number(stats.recent_7d || 0), t('mybank.countedByCreationTime')),
        statCell(t('insight.ai.badge'), number(stats.ai_generated || 0), t('mybank.questionsGeneratedByRecognitionFillInTasks'))),
      el('div', { class: 'mb-stats-columns' },
        el('div', { class: 'mb-stats-block' },
          el('h3', {}, t('mybank.questionTypes')),
          el('div', { class: 'mb-stat-bars' },
            typeRows.map(row => el('div', { class: 'mb-stat-bar-row' },
              el('span', { class: 'mb-stat-bar-label' }, row.label),
              el('div', { class: 'mb-stat-bar-track', role: 'img',
                'aria-label': t('mybank.questions', { v1: row.label, v2: row.count }) },
                el('span', { style: `width:${Math.round((row.count / maxType) * 100)}%` })),
              el('span', { class: 'mb-stat-bar-value' }, number(row.count))))),
        ),
        el('div', { class: 'mb-stats-block' },
          el('h3', {}, t('mybank.topKnowledgePoints', { v1: number(knowledge.total || 0) })),
          (knowledge.top || []).length
            ? el('div', { class: 'mb-stat-tags', 'data-no-i18n': '' },
              (knowledge.top || []).map(item => badge(`${item.tag} · ${number(item.count)}`, '')))
            : el('p', { class: 'help' }, t('mybank.noKnowledgePointTagsYetAddThem')))),
    ];
    if (!Number(stats.total || 0)) {
      children.push(el('p', { class: 'help mb-empty-hint' },
        t('mybank.yourBankIsEmptyClickImportBank')));
    }
    statsHost.replaceChildren(...children.filter(Boolean));
  }

  async function loadStats() {
    try {
      const data = await api(`/paperlab/questions/stats?${query({
        subject: filtersState.subject || '',
        grade: filtersState.grade || '',
      })}`);
      if (!current(version)) return;
      renderStats(data);
    } catch (error) {
      if (!current(version) || isAborted(error)) return;
      statsHost.replaceChildren(
        el('div', { class: 'card-header' }, el('h2', {}, t('mybank.bankStatistics'))),
        notice(t('mybank.failedToLoadStats', { v1: error.message }), 'error'));
    }
  }

  const picker = createModelPicker('paper-enrich', { requireVision: false, showModeToggle: false });
  const enrichBtn = button(t('mybank.fillAnswersAndSolutionsWithAi'), null, '', 'sparkle');
  const enrichStartBtn = button(t('mybank.startFillIn'), null, '', 'sparkle');
  const enrichNote = el('div', { class: 'mb-enrich-note' });

  /* -------------------------------------------------------------------------
   * 导入 / 导出（JSON / CSV）
   * ----------------------------------------------------------------------- */

  function downloadExport(format) {
    const qs = query({
      format,
      subject: filtersState.subject || '',
      grade: filtersState.grade || '',
      status: filtersState.status || '',
    });
    const link = el('a', {
      href: `/api/paperlab/questions/export?${qs}`,
      download: '',
      hidden: true,
      'aria-hidden': 'true',
    });
    document.body.append(link);
    link.click();
    link.remove();
    toast(t('mybank.exportingBankFile', { v1: format.toUpperCase() }));
  }

  function openImportDialog() {
    const fileInput = el('input', {
      type: 'file', accept: '.json,.csv,application/json,text/csv', class: 'mb-import-file',
    });
    const statusHost = el('div', { class: 'mb-import-status', 'aria-live': 'polite' });
    const resultHost = el('div', { class: 'mb-import-result' });
    const previewBtn = button(t('mybank.previewCheckDryRun'), null, 'secondary', 'search');
    const confirmBtn = button(t('mybank.confirmImport'), null, '', 'upload');
    const closeBtn = button(t('admin.closed'), null, 'ghost');
    confirmBtn.disabled = true;
    const dialog = el('dialog', { class: 'mb-import-dialog', 'aria-label': t('mybank.importBank') },
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('mybank.importBankJsonCsv')),
          el('p', { class: 'help' },
            t('mybank.oneFileUpTo10mbAnd2000')
            + t('mybank.questionsWithTheSameContentAreDeduplicated'))),
        closeBtn),
      el('div', { class: 'mb-import-file-row' },
        el('label', { class: 'field-label', for: 'mb-import-file' }, t('mybank.chooseBankFile')),
        fileInput),
      el('div', { class: 'row mb-import-actions' }, previewBtn, confirmBtn),
      statusHost,
      resultHost);

    async function submit(dryRun) {
      const file = fileInput.files && fileInput.files[0];
      if (!file) throw new Error(t('mybank.chooseAJsonOrCsvFileFirst'));
      if (file.size > 10 * 1024 * 1024) throw new Error(t('mybank.theFileExceedsThe10mbLimitSplit'));
      const form = new FormData();
      form.append('file', file);
      if (dryRun) form.append('dry_run', 'true');
      const data = await api('/paperlab/questions/import', { method: 'POST', body: form });
      renderImportResult(data, dryRun);
      return data;
    }

    function renderImportResult(data, dryRun) {
      const errors = Array.isArray(data.errors) ? data.errors : [];
      const canConfirm = Boolean(dryRun && Number(data.would_create || 0) > 0);
      confirmBtn.disabled = !canConfirm;
      const summary = dryRun
        ? t('mybank.previewRowsInTotalExpectedNew', { v1: number(data.total || 0), v2: number(data.would_create || 0) })
          + t('mybank.duplicatesSkippedErrors', { v1: number(data.would_skip || 0), v2: number(errors.length) })
        : t('mybank.importCompleteAddedDuplicatesSkipped', { v1: number(data.created || 0), v2: number(data.skipped || 0) })
          + t('mybank.errors', { v1: number(errors.length) });
      const parts = [notice(summary, errors.length ? 'warning' : '')];
      if (errors.length) {
        const table = el('table', { class: 'mb-import-errors' },
          el('thead', {}, el('tr', {}, el('th', {}, t('mybank.row')), el('th', {}, t('mybank.reason')))),
          el('tbody', {}, ...errors.slice(0, 50).map(item => el('tr', {},
            el('td', {}, number(item.row || 0)),
            el('td', {}, String(item.error || t('mybank.invalidData')))))));
        parts.push(el('div', { class: 'table-wrap' }, table));
        if (errors.length > 50) {
          parts.push(el('p', { class: 'help' }, t('mybank.showingTheFirst50OfErrorsFix', { v1: number(errors.length) })));
        }
      }
      if (dryRun) {
        parts.push(el('p', { class: 'help' }, canConfirm
          ? t('mybank.previewPassedConfirmingWritesValidQuestionsAs')
          : t('mybank.noValidQuestionsToImportOrAll')));
      } else {
        parts.push(el('p', { class: 'help' }, t('mybank.bankListAndStatsRefreshedImportedQuestions')));
      }
      resultHost.replaceChildren(...parts);
      statusHost.replaceChildren();
    }

    previewBtn.addEventListener('click', event => busy(event.currentTarget, () => submit(true)));
    confirmBtn.addEventListener('click', event => busy(event.currentTarget, async () => {
      await submit(false);
      await reload(container, version);
    }));
    closeBtn.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    fileInput.focus();
  }

  function renderToolbar() {
    filterCountBadge.textContent = t('home.activity.items', { count: listState.total });
    toolbarHost.replaceChildren(el('div', { class: 'card mb-toolbar-card' },
      el('div', { class: 'row between mb-toolbar-main' },
        el('div', { class: 'row' },
          el('strong', {}, t('practice.questionList')),
          badge(t('home.activity.items', { count: listState.total }), 'blue'),
          filtersState.keyword ? badge(t('mybank.keyword', { v1: filtersState.keyword }), 'amber') : null,
          el('span', { class: 'help' }, t('common.pageOf', { page: listState.page, pages: Math.max(1, Math.ceil(listState.total / PAGE_SIZE)) }))),
        el('div', { class: 'row wrap mb-toolbar-actions' },
          button(t('mybank.importBank'), () => openImportDialog(), 'soft small', 'upload'),
          button(t('mybank.exportJson'), () => downloadExport('json'), 'secondary small', 'download'),
          button(t('mybank.exportCsv'), () => downloadExport('csv'), 'secondary small', 'download'),
          button(t('mybank.importViaPaperStudio'), () => go('paperlab'), 'secondary small', 'printer'),
          enrichBtn)),
      el('p', { class: 'help' }, t('mybank.selectQuestionsToFillInWithAi_2'))));
    enrichBtn.disabled = selection().size === 0;
    enrichBtn.title = selection().size ? t('mybank.fillInTheSelectedQuestionsWithAi', { v1: selection().size }) : t('mybank.selectTheQuestionsToFillInFirst');
  }

  function renderSelectionBar() {
    const count = selection().size;
    selectionHost.hidden = count === 0;
    selectionHost.replaceChildren();
    if (!count) return;
    const pageItems = (listState.items || []);
    selectionHost.append(el('div', { class: 'row between mb-selection-inner' },
      el('div', { class: 'row' },
        el('strong', {}, t('mybank.selected', { v1: count })),
        button(t('mybank.selectThisPage'), () => {
          pageItems.forEach(item => selection().add(item.id));
          renderList();
        }, 'ghost small', 'check'),
        button(t('mybank.clearSelection'), () => { selection().clear(); renderList(); }, 'ghost small')),
      el('div', { class: 'row' },
        badge(t('mybank.aiFillIn'), 'amber'),
        button(t('mybank.fillInTheseQuestionsWithAi', { v1: count }), () => {
          enrichCard.hidden = false;
          enrichCard.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }, 'soft small', 'sparkle'))));
    renderToolbar();
  }

  function renderEnrichCard() {
    enrichCard.replaceChildren(
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('mybank.aiFillInOfAnswersSolutionsKnowledge')),
          el('p', { class: 'help' }, t('mybank.callsTheModelQuestionByQuestionVerified'))),
        button(t('question.collapse'), () => { enrichCard.hidden = true; }, 'ghost small', 'close')),
      picker.node,
      enrichNote,
      el('div', { class: 'row end' }, enrichStartBtn),
      jobHost,
      jobSummaryHost);
    if (!(state.providers || []).length) {
      enrichStartBtn.disabled = true;
      enrichStartBtn.title = t('mybank.configureAModelInModelSettingsFirst');
      enrichNote.replaceChildren(notice(
        t('mybank.noModelConnectionAvailableYetAddAn'), 'warning'));
      enrichNote.append(el('div', { class: 'row' },
        button(t('mybank.goToModelSettings'), () => go('settings'), 'soft', 'sliders')));
    }
  }

  function renderList() {
    listHost.replaceChildren();
    if (loadingError) {
      listHost.append(el('div', { class: 'error-panel' },
        loadingError,
        el('div', { class: 'row', style: 'margin-top:14px' },
          button(t('common.retry'), event => busy(event.currentTarget, () => reload(container, version)), '', 'repeat'))));
      paginationHost.hidden = true;
      renderToolbar();
      renderSelectionBar();
      return;
    }
    if (!listState.items.length) {
      const hasFilters = Object.values(filters()).some(Boolean);
      listHost.append(empty(
        hasFilters ? t('mybank.noMatchingQuestions') : t('mybank.yourBankIsStillEmpty'),
        hasFilters
          ? t('mybank.tryClearingTheFiltersOrUsingA')
          : t('mybank.uploadPapersInPaperStudioForRecognition'),
        hasFilters
          ? button(t('practice.clearFilters'), () => {
            clearFilterSelection();
            reload(container, version);
          }, 'soft', 'repeat')
          : el('div', { class: 'row wrap mb-empty-actions' },
            button(t('mybank.importBank'), () => openImportDialog(), '', 'upload'),
            button(t('mybank.importViaPaperStudio'), () => go('paperlab'), 'secondary', 'printer')),
        'tag'));
      paginationHost.hidden = true;
      renderToolbar();
      renderSelectionBar();
      return;
    }
    for (const question of listState.items) {
      listHost.append(questionCard(question, container, version));
    }
    const totalPages = Math.max(1, Math.ceil(listState.total / PAGE_SIZE));
    paginationHost.hidden = false;
    paginationHost.replaceChildren(
      el('span', {}, t('mybank.questionsPageOf', { v1: listState.total, v2: listState.page, v3: totalPages })),
      el('div', { class: 'row' },
        button(t('common.prevPage'), () => {
          if (page <= 1) return;
          page -= 1;
          selection().clear();
          reload(container, version);
        }, 'secondary small', 'chevron', { disabled: page <= 1 }),
        button(t('common.nextPage'), () => {
          if (page >= totalPages) return;
          page += 1;
          selection().clear();
          reload(container, version);
        }, 'secondary small', 'chevron', { disabled: page >= totalPages })));
    renderToolbar();
    renderSelectionBar();
  }

  function questionCard(question, containerRef, versionRef) {
    const content = question.content || {};
    const selected = selection().has(question.id);
    const checkbox = el('input', {
      type: 'checkbox', class: 'mb-card-check', checked: selected,
      'aria-label': t('mybank.selectQuestion', { v1: question.id }),
    });
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selection().add(question.id);
      else selection().delete(question.id);
      card.classList.toggle('is-selected', checkbox.checked);
      renderSelectionBar();
    });
    const tags = Array.isArray(question.knowledge_tags) ? question.knowledge_tags : [];
    const images = questionImages(question);
    const card = el('article', {
      class: `mb-card${selected ? ' is-selected' : ''}${question.verify_status === 'verified' ? ' is-verified' : ''}`,
      'data-question-id': String(question.id),
    },
    el('div', { class: 'mb-card-head' },
      checkbox,
      badge(typeLabel(question.type), 'blue'),
      sourceBadge(question),
      question.has_new_version ? button(t('mybank.version.newVersionBadge'),
        () => openVersionDialog(question, containerRef, versionRef), 'soft small', 'history') : null,
      withNoI18n(badge(question.subject || t('mybank.subjectUnlabeled'), 'teal')),
      question.grade ? withNoI18n(badge(question.grade, '')) : null,
      badge(difficultyLabel(question.difficulty), ''),
      questionStatusBadge(question.status),
      verifyBadge(question.verify_status),
      question.ai_model ? badge(`AI · ${question.ai_model}`, 'amber') : null,
      !question.has_answer ? badge(t('mybank.missingAnswer'), 'red') : null),
    withNoI18n(markdown(String(content.stem || '').slice(0, 1200))),
    tags.length
      ? el('div', { class: 'mb-tags', 'data-no-i18n': '' }, tags.map(tag => badge(tag, '')))
      : null,
    images.length
      ? el('div', { class: 'mb-card-images' }, images.slice(0, 3).map(image =>
        el('img', { src: image.url, alt: t('question.figure'), loading: 'lazy' })))
      : null,
    el('div', { class: 'mb-card-meta', 'data-no-i18n': '' },
      el('span', {}, answersSummary(question) ? t('mybank.answer', { v1: answersSummary(question).slice(0, 120) }) : t('mybank.answerNotFilledIn')),
      el('span', {}, t('common.updatedAt', { time: dateText(question.updated_at) })),
      el('span', {}, t('mybank.versionV', { v1: question.version }))),
    el('div', { class: 'row mb-card-actions' },
      // 统一题目引用：个人题一律 app:<id>（WS11 已支持，练习/错题/复习同一条链路）。
      button(t('mybank.practiceThis'), () => go('practice', `app:${question.id}`), 'soft small', 'book',
        { 'data-action': 'practice-personal', 'data-ref': `app:${question.id}` }),
      // 投稿到广场：先创建不可变投稿草稿，再跳到投稿台填写来源/授权声明（POST /plaza/submissions）。
      button(t('mybank.submit.entry'), event => busy(event.currentTarget, () => submitToPlaza(question)),
        'ghost small', 'upload', { 'data-action': 'submit-to-plaza', 'data-question-id': String(question.id) }),
      button(t('mybank.editQuestion'), () => openEditor(question, meta, containerRef, versionRef), 'soft small', 'edit'),
      button(t('mybank.version.openDialog'), () => openVersionDialog(question, containerRef, versionRef), 'ghost small', 'history'),
      question.verify_status === 'verified'
        ? button(t('mybank.unverify'), () => setVerify(question, 'pending', containerRef, versionRef), 'ghost small', 'repeat')
        : button(t('mybank.verify'), () => setVerify(question, 'verified', containerRef, versionRef), 'small', 'check'),
      button(t('common.delete'), () => removeQuestion(question, containerRef, versionRef), 'ghost small', 'trash')));
    return card;
  }

  function openEditor(question, metaInfo, containerRef, versionRef) {
    openQuestionEditor({
      title: t('mybank.editQuestion_2', { v1: question.id }),
      meta: metaInfo,
      question,
      reload: () => api(`/paperlab/questions/${encodeURIComponent(question.id)}`).then(data => data.question),
      onSave: async values => {
        await api(`/paperlab/questions/${encodeURIComponent(question.id)}`, {
          method: 'PATCH',
          data: { version: question.version, ...values },
        });
        await reload(containerRef, versionRef);
      },
    });
  }

  /* 投稿到广场（Phase 2）：POST /plaza/submissions 固定当前个人题版本生成不可变快照，
   * 跳到投稿台 #/submission?id= 继续填写来源/授权声明并送审。来源声明先留空（服务端
   * 校验会逐项标出阻断项，不阻断草稿创建）。重复点击会新建草稿，可在投稿台撤回。 */
  async function submitToPlaza(question) {
    const result = await api('/plaza/submissions', {
      method: 'POST',
      data: { question_id: question.id, source: {}, license_confirmed: false },
    });
    const submissionId = Number(result?.submission?.id || 0);
    if (!submissionId) throw new Error(t('mybank.submit.failed'));
    toast(t('mybank.submit.created'));
    go('submission', null, { id: submissionId });
  }

  async function setVerify(question, status, containerRef, versionRef) {
    try {
      const data = await api(`/paperlab/questions/${encodeURIComponent(question.id)}/verify`, {
        method: 'POST', data: { status },
      });
      toast(status === 'verified'
        ? t('mybank.verifiedFutureAiFillInsWillNot')
        : t('mybank.verificationRemovedTheQuestionIsBackTo'));
      Object.assign(question, data.question);
      await reload(containerRef, versionRef);
    } catch (error) {
      toast(error.message || t('mybank.verificationFailedFillInTheAnswerFirst'), true);
    }
  }

  async function removeQuestion(question, containerRef, versionRef) {
    if (!window.confirm(t('mybank.deleteQuestionThisCannotBeUndoneIn', { v1: question.id }))) return;
    try {
      await api(`/paperlab/questions/${encodeURIComponent(question.id)}`, { method: 'DELETE' });
      toast(t('mybank.questionDeleted'));
      await reload(containerRef, versionRef);
    } catch (error) {
      toast(error.message || t('mybank.deleteFailedPleaseTryAgain'), true);
    }
  }

  async function reload(containerRef, versionRef) {
    if (!current(versionRef)) return;
    loadingError = null;
    try {
      listState = await api(`/paperlab/questions?${searchQuery()}`);
      if (!current(versionRef)) return;
    } catch (error) {
      if (isAborted(error)) return;
      loadingError = error.message || t('mybank.failedToLoadYourBankPleaseTry');
    }
    renderList();
    if (current(versionRef)) await loadStats();
  }

  enrichBtn.addEventListener('click', () => {
    enrichCard.hidden = false;
    renderEnrichCard();
    enrichCard.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  enrichStartBtn.addEventListener('click', () => busy(enrichStartBtn, async () => {
    const ids = [...selection()];
    if (!ids.length) throw new Error(t('mybank.selectTheQuestionsToFillInFirst_2'));
    if (!(state.providers || []).length) throw new Error(t('mybank.configureAModelInModelSettingsFirst_2'));
    const payload = picker.payload(false);
    const response = await api('/paperlab/questions/enrich', {
      method: 'POST',
      data: { question_ids: ids, provider_id: payload.provider_id, model: payload.model },
    });
    const job = response.job || response;
    jobId = job.id;
    jobSummaryHost.replaceChildren();
    toast(t('mybank.aiFillInTaskSubmittedProgressAppears'));
    renderEnrichCard();
    mountJob(jobHost, job.id, null, {
      task: 'paper-enrich',
      onSettled: settled => {
        if (!settled) return;
        renderEnrichResult(settled, container, version);
      },
    });
  }));

  function renderEnrichResult(settled, containerRef, versionRef) {
    const result = settled.result || {};
    const status = settled.status;
    const box = el('div', { class: 'mb-enrich-result' });
    if (status === 'completed') {
      box.append(el('p', { class: 'mb-enrich-result-title' },
        t('mybank.aiFillInCompleteSucceeded', { v1: number(result.enriched_count || 0) })
        + t('mybank.skippedFailed', { v1: number((result.skipped || []).length), v2: number((result.failed || []).length) })));
      if ((result.skipped || []).length) {
        box.append(el('p', { class: 'help' }, t('mybank.manuallyVerifiedQuestionsAreNeverOverwrittenBy')));
      }
      if ((result.failed || []).length) {
        box.append(notice(t('mybank.theseQuestionsFailedToFillIn', { v1: result.failed.map(item => `#${item.question_id} ${item.error || ''}`).join('；') }), 'warning'));
      }
    } else if (status === 'cancelled') {
      box.append(notice(t('mybank.fillInTaskCancelledResultsAlreadyProduced'), 'warning'));
    } else {
      box.append(notice(settled.error || t('mybank.aiFillInDidNotFinishTry'), 'error'));
    }
    const failedIds = (result.failed || []).map(item => Number(item.question_id)).filter(Boolean);
    box.append(el('div', { class: 'row' },
      button(t('mybank.refreshList'), event => busy(event.currentTarget, () => reload(containerRef, versionRef)), 'soft small', 'repeat'),
      failedIds.length
        ? button(t('mybank.retryFailedQuestions', { v1: failedIds.length }), async event => busy(event.currentTarget, async () => {
          const payload = picker.payload(false);
          const response = await api('/paperlab/questions/enrich', {
            method: 'POST',
            data: { question_ids: failedIds, provider_id: payload.provider_id, model: payload.model },
          });
          mountJob(jobHost, (response.job || response).id, null, {
            task: 'paper-enrich',
            onSettled: settledJob => { if (settledJob) renderEnrichResult(settledJob, containerRef, versionRef); },
          });
        }), 'secondary small', 'repeat')
        : null));
    jobSummaryHost.replaceChildren(box);
    selection().clear();
    reload(containerRef, versionRef);
  }

  renderToolbar();
  renderSelectionBar();
  renderList();
  await reload(container, version);
  if (!current(version)) return;

  // 深链接：?#/mybank?edit=ID 直接打开编辑弹层（供分享/验收脚本使用）。
  const editId = Number(params?.get?.('edit') || 0);
  if (editId) {
    try {
      const data = await api(`/paperlab/questions/${encodeURIComponent(editId)}`);
      if (current(version)) openEditor(data.question, meta, container, version);
    } catch (error) {
      toast(error.message || t('mybank.questionToEditNotFound'), true);
    }
  }
  if (!providersReady && !(state.providers || []).length) {
    // 模型列表加载失败时仍然展示题库；AI 补全入口给出明确引导。
    renderEnrichCard();
  }
}
