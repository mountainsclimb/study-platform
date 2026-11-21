import { api, ensureCatalog, ensureKnowledgePoints, ensurePreferences } from '../core/api.js';
import { state, saveFilters } from '../core/state.js';
import {
  el, icon, button, badge, empty, loading, field, input, select,
  query, number, typeLabel, highlightKeyword, readableSummary,
} from '../core/dom.js';
import { current, setHashParams } from '../core/router.js';
import { renderKpTree } from '../components/kpTree.js';
import { openQuestion } from './question.js';
import { t } from '../core/i18n.js';

// 通用筛选字段：[参数名, 文案键, 目录字段]；文案在渲染时再取，避免模块加载时固定语言。
const COMMON_FILTERS = [
  ['subject', 'common.subject', 'subjects'],
  ['year', 'practice.year', 'years'],
  ['type', 'upload.fieldType', 'types'],
];

// 难度 / 掌握度是后端下发的中文枚举值：提交时保持原值，展示时按当前语言映射。
const DIFFICULTY_KEYS = {
  '待标注': 'practice.difficultyUnlabeled',
  '简单': 'practice.difficultyEasy',
  '中等': 'practice.difficultyMedium',
  '困难': 'practice.difficultyHard',
};
const MASTERY_KEYS = {
  '未掌握': 'practice.masteryNotMastered',
  '初步掌握': 'practice.masteryInitial',
  '比较掌握': 'practice.masteryFair',
  '熟练掌握': 'practice.masteryMastered',
};

function difficultyLabel(value) {
  const key = DIFFICULTY_KEYS[value];
  return key ? t(key) : String(value);
}

function masteryLabel(value) {
  const key = MASTERY_KEYS[value];
  return key ? t(key) : String(value);
}

// 平台精选来源能力探测（WS19 契约：`/api/questions?source=bank|official|all`，
// 合法 official 请求返回 200 且带 official_total）。旧后端会忽略未知参数、响应体没有
// official_total → 视为未就绪。这里用**合法请求**探测：非法 source 虽然会 422，
// 但浏览器会把 4xx 记成控制台资源错误，污染页面验收；200 探测更干净也不伪造能力。
let officialSourceSupportPromise = null;

export function detectOfficialSource() {
  if (!officialSourceSupportPromise) {
    officialSourceSupportPromise = api('/questions?page=1&page_size=1&source=official')
      .then(data => Boolean(data && typeof data === 'object' && 'official_total' in data))
      .catch(() => false);
  }
  return officialSourceSupportPromise;
}

// 来源与审核状态徽标：后端未下发 source 时按高考题库处理（旧接口无该字段）。
const SOURCE_BADGE_KEYS = {
  bank: 'practice.source.bank',
  official: 'practice.source.official',
  app: 'practice.source.app',
};
const REVIEW_STATUS_KEYS = {
  community: 'practice.source.reviewCommunity',
  community_review: 'practice.source.reviewCommunity',
  verified: 'practice.source.reviewVerified',
};

function sourceKindOf(q) {
  // WS19 契约：official 项带 source/public_id；旧式/精简 payload 只有 official:<id>
  // 引用时按 qid 前缀兜底，避免把平台精选题误标成「高考题库」。
  if (q?.source === 'official' || q?.official || String(q?.qid || '').startsWith('official:')) return 'official';
  if (q?.source === 'app') return 'app';
  return 'bank';
}

function sourceBadge(q) {
  const kind = sourceKindOf(q);
  const tone = kind === 'official' ? 'blue' : kind === 'app' ? 'teal' : '';
  return badge(t(SOURCE_BADGE_KEYS[kind]), tone);
}

// 平台精选题额外显示审核状态；已发布时来源徽标本身就是「平台精选」，不重复。
function reviewStatusBadge(q) {
  if (sourceKindOf(q) !== 'official') return null;
  const status = String(q?.review_status || '');
  const key = REVIEW_STATUS_KEYS[status];
  if (!key) return null;
  return badge(t(key), status === 'verified' ? 'teal' : 'amber');
}

function parseCsv(value) {
  return String(value || '').split(',').map(item => item.trim()).filter(Boolean);
}

/**
 * 轻量多选控件（题型 / 地区）：用后端已经支持的 `type=a,b`、`province=a,b` 契约，
 * 折叠面板避免在手机首屏占高度。选择变化即回调逗号串。
 */
function createMultiSelect({ label, emptyLabel, options, value = '', onChange }) {
  const selected = new Set(parseCsv(value).map(String));
  const summary = el('span', { class: 'multi-select-summary', 'data-no-i18n': 'true' });
  const panel = el('div', { class: 'multi-select-panel', 'data-no-i18n': 'true' });
  const root = el('details', { class: 'multi-select' },
    el('summary', { 'aria-label': label }, summary), panel);

  function optionLabel(key) {
    const opt = options.find(item => String(item.value) === String(key));
    return opt ? opt.label : String(key);
  }

  function renderSummary() {
    const labels = [...selected].map(optionLabel);
    summary.textContent = labels.length
      ? (labels.length <= 2 ? labels.join(t('common.listSeparator')) : t('practice.andMore', { v1: labels.slice(0, 2).join(t('common.listSeparator')), v2: labels.length }))
      : emptyLabel;
    summary.classList.toggle('muted', !labels.length);
  }

  function renderPanel() {
    panel.replaceChildren();
    for (const opt of options) {
      const checkbox = el('input', {
        type: 'checkbox', value: String(opt.value), checked: selected.has(String(opt.value)),
      });
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(checkbox.value);
        else selected.delete(checkbox.value);
        renderSummary();
        onChange([...selected]);
      });
      panel.append(el('label', { class: 'multi-select-option' }, checkbox, el('span', {}, opt.label)));
    }
    if (!options.length) panel.append(el('p', { class: 'help' }, t('practice.noOptionsAvailable')));
  }

  // 点击控件外部自动收起；控件被路由重建后自动解绑，避免监听器泄漏。
  const onDocumentClick = event => {
    if (!root.isConnected) {
      document.removeEventListener('click', onDocumentClick);
      window.removeEventListener('resize', alignPanel);
      return;
    }
    if (root.open && !root.contains(event.target)) root.open = false;
  };
  document.addEventListener('click', onDocumentClick);

  // 靠近视口右缘（390px 手机的三列筛选）时自动右对齐，避免下拉面板溢出屏幕。
  function alignPanel() {
    if (!root.open) return;
    const rect = root.getBoundingClientRect();
    const panelWidth = panel.getBoundingClientRect().width || 210;
    const rightAligned = rect.left + panelWidth > window.innerWidth - 8;
    root.classList.toggle('align-right', rightAligned);
    panel.style.left = rightAligned ? 'auto' : '0';
    panel.style.right = rightAligned ? '0' : 'auto';
    panel.style.maxWidth = `${Math.min(320, Math.max(180, window.innerWidth - 24))}px`;
  }
  root.addEventListener('toggle', alignPanel);
  window.addEventListener('resize', alignPanel);

  renderSummary();
  renderPanel();
  return {
    node: el('div', { class: 'field' }, el('label', { class: 'field-label' }, label), root),
    get value() { return [...selected].join(','); },
    set value(next) {
      selected.clear();
      for (const item of parseCsv(next)) selected.add(String(item));
      renderSummary();
      renderPanel();
    },
  };
}

// 结果卡：来源 + 原卷题号 + 题干摘要；题组用标签帮助定位，命中统计仍按小题计。
function resultCard(q, { selected, keyword, onOpen, onSkipReview }) {
  const isReview = q.slot === 'review';
  const meta = [
    [q.year, q.province].filter(Boolean).join(' · ') || t('wrongbook.sourceSeeDetail'),
    q.number_label ? t('question.originalNumber', { label: q.number_label }) : '',
  ].filter(Boolean).join(' · ');
  const badges = el('span', { class: 'result-card-meta' });
  if (isReview) badges.append(badge(t('practice.review'), 'amber'));
  // 来源徽标（高考题库/平台精选/我的题库）+ 平台精选题的审核状态。
  badges.append(sourceBadge(q));
  const reviewBadge = reviewStatusBadge(q);
  if (reviewBadge) badges.append(reviewBadge);
  badges.append(badge(q.subject || t('practice.realExam'), 'teal'), badge(typeLabel(q, state.catalog)));
  if (q.group_id) badges.append(badge(t('practice.questionSet'), 'blue'));
  if (q.difficulty && q.difficulty !== '待标注') badges.append(badge(difficultyLabel(q.difficulty), 'blue'));
  if (q.has_ai_explanation) badges.append(badge(t('practice.aiExplanation'), 'teal'));

  const card = el('button', {
    type: 'button',
    class: `result-card${selected ? ' selected' : ''}`,
    'data-qid': q.qid,
    'aria-current': selected ? 'true' : null,
    onClick: event => {
      // 「跳过」是卡片内的 pointer-only 辅助操作（aria-hidden，避免按钮嵌套按钮），
      // 命中它时不再打开题目。
      if (event.target.closest?.('.skip-review-link')) return;
      onOpen(q);
    },
  },
    el('span', { class: 'result-card-title', 'data-no-i18n': 'true' },
      // 4.1：列表摘要不直接露 [公式1]，公式/图片占位换成可读提示。
      highlightKeyword(readableSummary(q, 120) || t('practice.viewFullQuestion'), keyword)),
    badges,
    el('span', { class: 'result-card-meta', 'data-no-i18n': 'true' },
      el('span', {}, meta),
      isReview && q.review_label ? el('span', {}, q.review_label) : null,
      onSkipReview ? el('span', {
        class: 'skip-review-link', 'aria-hidden': 'true',
        title: t('practice.skipThisTimeItWillAppearAgain'),
        onClick: event => { event.stopPropagation(); onSkipReview(q.qid); },
      }, t('practice.skip')) : null));
  return card;
}

function activeFilterChips(filters, catalog, onChange, names = {}) {
  const chips = el('div', { class: 'filter-chips' });
  const labels = {
    q: t('practice.keyword'), subject: t('common.subject'), type: t('upload.fieldType'), year: t('practice.year'), year_from: t('practice.startYear'),
    year_to: t('practice.endYear'), difficulty: t('practice.difficulty'), province: t('practice.region'), status: t('practice.myStatus'),
    source: t('practice.source.label'),
    sort: t('practice.sort'), has_answer: t('practice.hasValidAnswer'), has_solution: t('practice.hasOriginalSolution'),
    has_ai_explanation: t('practice.hasAiExplanation'), include_ai_difficulty: t('practice.includeAiEstimatedDifficulty'),
    kp_id: t('practice.knowledgePoint'), method_id: t('practice.questionTypeSolution'),
  };
  const kpNames = names.knowledgePoints || new Map();
  const methodNames = names.methods || new Map();
  for (const [key, label] of Object.entries(labels)) {
    const raw = filters[key];
    if (!raw) continue;
    let text = String(raw);
    if (key === 'subject') text = raw;
    else if (key === 'province') text = parseCsv(raw).join(t('common.listSeparator'));
    else if (key === 'type') text = parseCsv(raw).map(t => catalog?.type_labels?.[t] || t).join(t('common.listSeparator'));
    else if (key === 'sort') text = { year_desc: t('practice.newestFirst'), year_asc: t('practice.oldestFirst'), difficulty: t('practice.difficultyFirst'), accuracy: t('practice.accuracyAscending'), random: t('practice.random') }[raw] || raw;
    else if (key === 'status') text = { unattempted: t('practice.notAttempted'), wrong: t('practice.inWrongBook'), due: t('practice.dueForReview') }[raw] || raw;
    else if (key === 'source') text = { bank: t('practice.source.bank'), official: t('practice.source.official'), all: t('practice.source.all') }[raw] || raw;
    else if (['has_answer', 'has_solution', 'has_ai_explanation', 'include_ai_difficulty'].includes(key)) text = t('kp.selected');
    else if (key === 'kp_id') {
      // 已选知识点显示中文名；映射缺失时退回 `#编号`，绝不出现空白芯片。
      text = parseCsv(raw).map(id => kpNames.get(String(id)) || t('practice.knowledgePoint_2', { v1: id })).join(t('common.listSeparator'));
    } else if (key === 'method_id') {
      text = parseCsv(raw).map(id => methodNames.get(String(id)) || t('practice.type', { v1: id })).join(t('common.listSeparator'));
    }
    chips.append(el('span', { class: 'filter-chip' }, t('practice.filterChip', { label, text }),
      el('button', {
        type: 'button', 'aria-label': t('practice.clearTheFilter', { v1: label }),
        onClick: () => onChange(key),
      }, '×')));
  }
  return chips.childNodes.length ? chips : null;
}

export async function renderPractice(container, version, urlParams) {
  const [catalog, kps, prefs, methodsRes, officialSourceReady] = await Promise.all([
    ensureCatalog(),
    ensureKnowledgePoints(),
    ensurePreferences(),
    api('/methods').catch(() => ({ items: [] })),
    // 平台精选来源筛选能力探测：旧后端未接 source 参数时整体隐藏来源筛选。
    detectOfficialSource(),
  ]);
  if (!current(version)) return;
  const allMethods = methodsRes.items || [];
  // 已选筛选芯片的中文名映射：知识点用目录树，题型/解法用 /methods 列表。
  const kpNameById = new Map((kps || []).map(item => [String(item.id), item.name]));
  const methodNameById = new Map(allMethods.map(item => [String(item.id), `${item.subject}·${item.name}`]));

  // 跨页跳题（3.2-6）：从学习概览/错题/拍题带 qid 进入时，即使该题不在当前页
  // 也要单独拉取并在阅读区打开；qid 不属于筛选条件，单独保存。
  let pendingTargetQid = urlParams?.get('qid') || null;

  // 从 URL 深链恢复筛选；返回列表时沿用同一份本地状态。
  if (urlParams && [...urlParams.keys()].length > 0) {
    const f = {};
    for (const k of ['q', 'subject', 'type', 'difficulty', 'province', 'year', 'year_from', 'year_to',
      'kp_id', 'method_id', 'status', 'sort', 'has_answer', 'has_solution', 'has_ai_explanation',
      'include_ai_difficulty', 'source']) {
      if (urlParams.has(k)) f[k] = urlParams.get(k);
    }
    state.filters = f;
    state.page = Math.max(1, Number(urlParams.get('page')) || 1);
    saveFilters();
  }
  // 后端未接入来源筛选时忽略 URL/本地状态里的 source，避免出现不存在的能力。
  if (!officialSourceReady && state.filters.source) {
    delete state.filters.source;
    saveFilters();
  }
  // 兼容只设置 state.selectedQid 的旧入口（例如从首页「继续练习」）。
  if (!pendingTargetQid && urlParams && [...urlParams.keys()].length === 0 && state.selectedQid) {
    pendingTargetQid = state.selectedQid;
  }
  if (!state.practiceView) state.practiceView = 'results';

  const filterCard = el('section', { class: 'card filter-card' });
  const keyword = input(t('practice.searchQuestionTextMaterialOrPaperName'), {
    value: state.filters.q || '', 'aria-label': t('practice.searchQuestions'), type: 'search',
  });
  const runSearch = async () => {
    state.filters.q = keyword.value.trim();
    state.page = 1;
    state.selectedQid = null;
    pendingTargetQid = null;
    saveFilters();
    await loadSearch();
  };
  const searchButton = button(t('common.search'), runSearch, 'search-submit', 'search', { 'aria-label': t('practice.searchQuestions') });
  // 回车直接搜索（4.1）：输入框内 Enter 不再只移动焦点。
  keyword.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    runSearch();
  });

  const makeSelect = (key, label, options, onChange) => {
    const control = select(options, state.filters[key] || '', {
      'aria-label': label,
      onChange: async () => {
        state.filters[key] = control.value;
        state.page = 1;
        state.selectedQid = null;
        saveFilters();
        await onChange();
      },
    });
    return control;
  };
  const subjectSel = makeSelect('subject', t('common.subject'),
    [{ value: '', label: t('insight.subjectAll') }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    async () => { refreshFilters(); await loadSearch(); });
  const yearSel = makeSelect('year', t('practice.year'),
    [{ value: '', label: t('practice.allYears') }, ...(catalog.years || []).map(y => ({ value: String(y), label: t('common.yearLabel', { year: y }) }))],
    async () => { refreshFilters(); await loadSearch(); });
  const typeMulti = createMultiSelect({
    label: t('practice.questionTypesMultiSelect'),
    emptyLabel: t('practice.allTypes'),
    options: (catalog.types || []).map(t => ({ value: t, label: catalog.type_labels?.[t] || t })),
    value: state.filters.type || '',
    onChange: async values => {
      state.filters.type = values.join(',');
      if (!values.length) delete state.filters.type;
      state.page = 1;
      state.selectedQid = null;
      saveFilters();
      refreshFilters();
      await loadSearch();
    },
  });

  const filterMore = el('div', { class: 'filter-more', hidden: true });
  const moreToggle = el('button', {
    type: 'button', class: 'filter-toggle', 'aria-expanded': 'false',
    onClick: () => {
      const open = filterMore.hidden;
      filterMore.hidden = !open;
      moreToggle.setAttribute('aria-expanded', String(open));
    },
  }, icon('sliders'), t('practice.moreFilters'), el('span', { class: 'filter-badge' }, '0'));

  const difficultySel = select(
    [{ value: '', label: t('practice.allDifficulties') }, ...(catalog.difficulties || ['待标注', '简单', '中等', '困难']).map(d => ({ value: d, label: difficultyLabel(d) }))],
    state.filters.difficulty || '', { 'aria-label': t('practice.difficulty') });
  const provinceMulti = createMultiSelect({
    label: t('practice.regionsMultiSelect'),
    emptyLabel: t('practice.allRegions'),
    options: (catalog.provinces || []).map(p => ({ value: p, label: p })),
    value: state.filters.province || '',
    onChange: () => applyAdvanced(),
  });
  const statusSel = select([
    { value: '', label: t('practice.allStatuses') },
    { value: 'unattempted', label: t('practice.notAttempted') },
    { value: 'wrong', label: t('practice.inWrongBook') },
    { value: 'due', label: t('practice.dueForReview') },
    { value: '未掌握', label: masteryLabel('未掌握') },
    { value: '初步掌握', label: masteryLabel('初步掌握') },
    { value: '比较掌握', label: masteryLabel('比较掌握') },
    { value: '熟练掌握', label: masteryLabel('熟练掌握') },
  ], state.filters.status || '', { 'aria-label': t('practice.myStatus') });
  const sortSel = select([
    { value: 'year_desc', label: t('practice.newestFirst') },
    { value: 'year_asc', label: t('practice.oldestFirst') },
    { value: 'difficulty', label: t('practice.difficultyFirst') },
    { value: 'accuracy', label: t('practice.accuracyAscending') },
    { value: 'random', label: t('practice.random') },
  ], state.filters.sort || 'year_desc', { 'aria-label': t('practice.sort') });
  // 来源筛选：默认空值=高考题库（后端默认 bank，省略参数保持向后兼容）；
  // 平台精选/全部来源依赖 WS19 的 source 参数，探测不通过时整个控件不渲染。
  const sourceSel = select([
    { value: '', label: t('practice.source.bank') },
    { value: 'official', label: t('practice.source.official') },
    { value: 'all', label: t('practice.source.all') },
  ], state.filters.source || '', { 'aria-label': t('practice.source.label') });
  const yearFrom = select([{ value: '', label: t('practice.any') }, ...(catalog.years || []).map(y => ({ value: String(y), label: `${y}` }))],
    state.filters.year_from || '', { 'aria-label': t('practice.startYear') });
  const yearTo = select([{ value: '', label: t('practice.any') }, ...(catalog.years || []).map(y => ({ value: String(y), label: `${y}` }))],
    state.filters.year_to || '', { 'aria-label': t('practice.endYear') });
  const hasAnsChk = el('input', { type: 'checkbox', checked: state.filters.has_answer === 'true' });
  const hasSolChk = el('input', { type: 'checkbox', checked: state.filters.has_solution === 'true' });
  const hasAiChk = el('input', { type: 'checkbox', checked: state.filters.has_ai_explanation === 'true' });
  const aiDiffChk = el('input', { type: 'checkbox', checked: state.filters.include_ai_difficulty === 'true' });
  const methodSel = select(
    [{ value: '', label: t('practice.allTypesMethods') }, ...allMethods.map(m => ({ value: String(m.id), label: `${m.subject}·${m.name} (${m.question_count})` }))],
    state.filters.method_id || '', { 'aria-label': t('practice.questionTypesMethods') });

  const advancedControls = [difficultySel, statusSel, sourceSel, sortSel, yearFrom, yearTo, methodSel,
    hasAnsChk, hasSolChk, hasAiChk, aiDiffChk];
  const applyAdvanced = async () => {
    state.filters.difficulty = difficultySel.value;
    state.filters.province = provinceMulti.value;
    state.filters.status = statusSel.value;
    state.filters.source = officialSourceReady ? sourceSel.value : '';
    state.filters.sort = sortSel.value || 'year_desc';
    state.filters.year_from = yearFrom.value;
    state.filters.year_to = yearTo.value;
    state.filters.method_id = methodSel.value;
    state.filters.has_answer = hasAnsChk.checked ? 'true' : '';
    state.filters.has_solution = hasSolChk.checked ? 'true' : '';
    state.filters.has_ai_explanation = hasAiChk.checked ? 'true' : '';
    state.filters.include_ai_difficulty = aiDiffChk.checked ? 'true' : '';
    state.page = 1;
    state.selectedQid = null;
    saveFilters();
    refreshFilters();
    await loadSearch();
  };
  for (const control of advancedControls) {
    control.addEventListener('change', applyAdvanced);
  }

  const kpPanel = el('div', { class: 'kp-drawer hide' });
  const kpToggle = button(t('practice.filterByKnowledgePoint'), () => {
    kpPanel.classList.toggle('hide');
    refreshKpPanel();
  }, 'ghost small', 'tag');

  const chipsMount = el('div');
  function refreshFilters() {
    moreToggle.lastChild.textContent = String(
      ['difficulty', 'province', 'status', 'source', 'year_from', 'year_to', 'method_id',
        'has_answer', 'has_solution', 'has_ai_explanation', 'include_ai_difficulty', 'kp_id']
        .filter(key => state.filters[key]).length,
    );
    chipsMount.replaceChildren(activeFilterChips(state.filters, catalog, async key => {
      if (key === 'method_id') state.filters.method_id = '';
      else if (['has_answer', 'has_solution', 'has_ai_explanation', 'include_ai_difficulty'].includes(key)) state.filters[key] = '';
      else state.filters[key] = '';
      state.page = 1;
      state.selectedQid = null;
      saveFilters();
      await reloadControls();
      await loadSearch();
    }, { knowledgePoints: kpNameById, methods: methodNameById }) || '');
  }

  async function reloadControls() {
    // 关键词输入框也是受控状态：清空筛选 / 清除芯片 / 从 URL 恢复时必须同步，
    // 否则搜索框里仍留着旧关键词，看起来「清空没生效」。
    keyword.value = state.filters.q || '';
    subjectSel.value = state.filters.subject || '';
    yearSel.value = state.filters.year || '';
    typeMulti.value = state.filters.type || '';
    difficultySel.value = state.filters.difficulty || '';
    provinceMulti.value = state.filters.province || '';
    statusSel.value = state.filters.status || '';
    sortSel.value = state.filters.sort || 'year_desc';
    if (officialSourceReady) sourceSel.value = state.filters.source || '';
    yearFrom.value = state.filters.year_from || '';
    yearTo.value = state.filters.year_to || '';
    methodSel.value = state.filters.method_id || '';
    hasAnsChk.checked = state.filters.has_answer === 'true';
    hasSolChk.checked = state.filters.has_solution === 'true';
    hasAiChk.checked = state.filters.has_ai_explanation === 'true';
    aiDiffChk.checked = state.filters.include_ai_difficulty === 'true';
    refreshKpPanel();
    refreshFilters();
  }

  function refreshKpPanel() {
    if (kpPanel.classList.contains('hide')) return;
    const selIds = parseCsv(state.filters.kp_id);
    kpPanel.replaceChildren(
      el('div', { class: 'row between', style: 'margin-bottom:10px' },
        el('strong', { class: 'small-text' }, state.filters.subject
          ? t('practice.knowledgePointOutlineMultiSelect', { v1: state.filters.subject })
          : t('practice.allSubjectsKnowledgePointOutlineSelectA')),
        selIds.length ? button(t('practice.clearKnowledgePointFilter'), async () => {
          state.filters.kp_id = '';
          saveFilters();
          refreshKpPanel();
          refreshFilters();
          await loadSearch();
        }, 'ghost small') : null),
      renderKpTree(kps, {
        subject: state.filters.subject || '',
        selectedIds: selIds,
        multi: true,
        onSelect: async ids => {
          state.filters.kp_id = ids.join(',');
          state.page = 1;
          state.selectedQid = null;
          saveFilters();
          refreshKpPanel();
          refreshFilters();
          await loadSearch();
        },
      }),
    );
  }

  filterMore.append(
    el('div', { class: 'filter-more-grid' },
      officialSourceReady ? field(t('practice.source.label'), sourceSel) : null,
      field(t('practice.difficulty'), difficultySel),
      provinceMulti.node,
      field(t('practice.myStatus'), statusSel),
      field(t('practice.sort'), sortSel),
      field(t('practice.yearRange'), el('div', { class: 'row' }, yearFrom, el('span', { class: 'help' }, t('practice.to')), yearTo)),
      allMethods.length ? field(t('practice.questionTypesMethods'), methodSel) : null),
    el('div', { class: 'row wrap', style: 'margin-top:12px' },
      el('label', { class: 'check-label' }, hasAnsChk, t('practice.hasValidAnswer')),
      el('label', { class: 'check-label' }, hasSolChk, t('practice.hasOriginalSolution')),
      el('label', { class: 'check-label' }, hasAiChk, t('practice.hasAiExplanation')),
      el('label', { class: 'check-label' }, aiDiffChk, t('practice.includeAiEstimatedDifficulty')),
      kpToggle),
    kpPanel);

  filterCard.append(
    el('div', { class: 'filter-head' },
      el('h2', {}, t('insight.activityMix.practice')),
      el('div', { class: 'row' }, moreToggle,
        button(t('practice.clearFilters'), async () => {
          state.filters = {};
          state.page = 1;
          state.selectedQid = null;
          pendingTargetQid = null;
          saveFilters();
          setHashParams('practice', {});
          await reloadControls();
          await loadSearch();
        }, 'ghost small', 'repeat'))),
    el('div', { class: 'filter-compact' },
      el('div', { class: 'search-wrap' }, icon('search'), keyword, searchButton),
      field(t('common.subject'), subjectSel), field(t('practice.year'), yearSel), typeMulti.node),
    filterMore,
    chipsMount);

  const dueContainer = el('div');
  const resultsList = el('div', { class: 'question-list' });
  const resultsScroll = el('div', { class: 'results-scroll' }, resultsList);
  const resultsCount = el('p', { class: 'results-count' }, t('common.loading'));
  const resultsPane = el('aside', { class: 'results-pane', 'aria-label': t('practice.questionList') },
    el('header', { class: 'results-heading' },
      el('h2', {}, t('practice.questionList')), resultsCount),
    dueContainer, resultsScroll);

  const readingBack = el('button', {
    type: 'button', class: 'reading-back',
    onClick: () => showResults(true),
  }, t('practice.backToResults'));
  const detailContainer = el('div', { class: 'reading-content', id: 'question-detail' });
  const actionsHost = el('div', { class: 'reading-actions', id: 'reading-actions', hidden: true });
  const readingPane = el('section', { class: 'reading-pane', 'aria-label': t('practice.questionReadingAnswering') },
    el('div', { class: 'reading-main' }, readingBack, detailContainer),
    actionsHost);

  const workspace = el('div', { class: 'practice-workspace', 'data-view': state.practiceView },
    resultsPane, readingPane);
  // 记录结果列滚动位置：手机「返回结果」时恢复原位置与焦点。
  resultsScroll.addEventListener('scroll', () => {
    state.resultsScrollTop = resultsScroll.scrollTop;
  }, { passive: true });
  // 阅读区换题（包括题组小问 tab）时同步结果卡选中态；页面重建后监听器自动解绑。
  const onQuestionOpened = event => {
    if (!workspace.isConnected) {
      window.removeEventListener('zhixu:question-opened', onQuestionOpened);
      return;
    }
    if (event.detail?.qid) {
      state.selectedQid = event.detail.qid;
      highlightSelectedCard();
    }
  };
  window.addEventListener('zhixu:question-opened', onQuestionOpened);

  container.replaceChildren(
    el('div', { class: 'page-heading practice-page-heading', style: 'margin-bottom:12px' },
      el('div', {},
        el('div', { class: 'eyebrow' }, 'QUESTION LIBRARY'),
        el('h1', {}, t('practice.understandEverySingleQuestion')),
        el('p', {}, t('practice.onDesktopFilterOnTheLeftAnd')))),
    filterCard,
    workspace);

  function showQuestionView() {
    state.practiceView = 'question';
    workspace.dataset.view = 'question';
    actionsHost.hidden = false;
  }
  function showResults(focus = false) {
    state.practiceView = 'results';
    workspace.dataset.view = 'results';
    actionsHost.hidden = true;
    if (focus) {
      const target = resultsList.querySelector('.result-card.selected') || resultsList.querySelector('.result-card');
      target?.focus();
      resultsScroll.scrollTop = state.resultsScrollTop || 0;
    }
  }

  async function openResult(item) {
    state.selectedQid = item.qid;
    workspace.querySelectorAll('.result-card').forEach(node => {
      const selected = node.dataset.qid === item.qid;
      node.classList.toggle('selected', selected);
      node.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    // 把当前题写进 #/practice?...&qid=：刷新后仍能回到同一题。
    setHashParams('practice', {
      ...state.filters,
      page: state.page > 1 ? state.page : '',
      qid: item.qid,
    }, true);
    showQuestionView();
    detailContainer.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    await openQuestion(item.qid, detailContainer, { onSubmitted: () => loadSearch({ keepSelection: true }) });
  }

  // 高亮与 state.selectedQid 同步：找不到卡片时不强行滚动或切换阅读区。
  function highlightSelectedCard() {
    let found = false;
    workspace.querySelectorAll('.result-card').forEach(node => {
      const selected = Boolean(state.selectedQid) && node.dataset.qid === state.selectedQid;
      found = found || selected;
      node.classList.toggle('selected', selected);
      node.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    return found;
  }

  let searchCounter = 0;
  let lastReviewCount = 0;
  let lastResults = null;

  function renderDueStrip(reviews) {
    dueContainer.replaceChildren();
    if (!reviews?.items?.length || prefs.show_review_strip === false) return;
    const strip = el('div', { class: 'due-strip' },
      el('div', { class: 'due-strip-title' }, icon('clock'),
        t('practice.wrongAnswersDue', { v1: reviews.total }),
        el('button', {
          type: 'button', class: 'reading-back', style: 'margin-left:auto;min-height:32px;display:inline-block',
          onClick: () => { dueContainer.replaceChildren(); },
          'aria-label': t('practice.dismissTheDueReviewNotice'),
        }, '×')));
    const row = el('div', { class: 'due-items' });
    for (const item of reviews.items.slice(0, 4)) {
      const q = item.question || item;
      row.append(el('button', {
        type: 'button', class: 'due-item', 'data-no-i18n': 'true',
        onClick: () => openResult({ qid: q.qid || item.qid }),
      }, `${q.subject || t('practice.wrongAnswerFallback')} · ${readableSummary(q, 20) || t('practice.practiceAgain')}`));
    }
    strip.append(row);
    dueContainer.append(strip);
  }

  async function loadReviews(count) {
    try {
      const reviews = await api(`/reviews?${query(state.filters)}`);
      if (!current(version) || count !== searchCounter) return;
      lastResults = { ...(lastResults || {}), reviews };
      renderDueStrip(reviews);
    } catch (error) {
      if (!current(version) || count !== searchCounter) return;
      dueContainer.replaceChildren(el('div', { class: 'notice warning' },
        t('practice.failedToLoadDueWrongAnswers', { v1: error.message }),
        button(t('common.retry'), () => loadReviews(count), 'secondary small', 'repeat')));
    }
  }

  async function loadSearch({ keepSelection = false } = {}) {
    const count = ++searchCounter;
    const hashParams = { ...state.filters, page: state.page > 1 ? state.page : '' };
    if (state.selectedQid) hashParams.qid = state.selectedQid;
    setHashParams('practice', hashParams, true);
    resultsCount.textContent = t('common.loading');
    resultsList.replaceChildren(loading(t('practice.filteringQuestions')));
    let results;
    try {
      results = await api(`/questions?${query({
        ...state.filters,
        page: state.page,
        page_size: state.pageSize,
        include_reviews: prefs.interleave_per_page > 0 ? 'true' : 'false',
        seen_reviews: [...state.seenReviewQids].join(',') || undefined,
      })}`);
    } catch (error) {
      if (!current(version) || count !== searchCounter) return;
      resultsList.replaceChildren(el('div', { class: 'error-panel' },
        t('practice.failedToLoadTheQuestionList', { v1: error.message }),
        button(t('practice.searchAgain'), () => loadSearch(), 'secondary small', 'repeat')));
      resultsCount.textContent = t('common.loadFailed');
      return;
    }
    if (!current(version) || count !== searchCounter) return;
    state.search = results;
    lastResults = { ...results };
    lastReviewCount = (results.review_slots || []).length;
    const items = results.items || [];
    // 目标题在本次跨页进入时单独保留，不参与「筛选结果找不到就清空」的逻辑。
    if (pendingTargetQid) state.selectedQid = pendingTargetQid;
    else if (state.selectedQid && !keepSelection && !items.some(item => item.qid === state.selectedQid)) {
      state.selectedQid = null;
    }
    resultsCount.textContent = t('practice.questionsFoundPage', { v1: number(results.total), v2: results.page || state.page })
      + (lastReviewCount ? t('practice.includesInterleavedReviewQuestions', { v1: lastReviewCount }) : '');
    resultsList.replaceChildren();
    // WS19 的 official 列表只支持学科/题型/关键词筛选：明确提示哪些高级筛选不适用，
    // 避免用户以为年份/地区/难度筛选「没生效」。
    if (state.filters.source === 'official') {
      resultsList.append(el('div', { class: 'notice' }, t('practice.source.officialFilterNotice')));
    }
    for (const item of items) {
      resultsList.append(resultCard(item, {
        selected: item.qid === state.selectedQid,
        keyword: state.filters.q || '',
        onOpen: () => openResult(item),
        onSkipReview: item.slot === 'review' ? qid => {
          state.seenReviewQids.add(qid);
          loadSearch({ keepSelection: true });
        } : null,
      }));
    }
    if (!items.length) {
      // 空态按来源给不同提示：平台精选刚上线时「无匹配」容易被误认为筛选出错。
      const officialOnly = state.filters.source === 'official';
      resultsList.append(empty(
        officialOnly ? t('practice.source.emptyOfficial') : t('practice.noMatchingQuestions'),
        officialOnly ? t('practice.source.emptyOfficialHint') : t('practice.tryFewerFiltersOrADifferentKeyword')));
    }
    const pages = Math.max(1, Math.ceil((results.total || 0) / state.pageSize));
    if (items.length) {
      resultsList.append(el('div', { class: 'pagination', style: 'margin:14px 4px 4px' },
        button(t('common.prevPage'), () => {
          state.page = Math.max(1, state.page - 1);
          resultsScroll.scrollTop = 0;
          loadSearch();
        }, 'secondary small', null, { disabled: state.page <= 1 }),
        el('span', {}, `${state.page} / ${pages}`),
        button(t('common.nextPage'), () => {
          state.page++;
          resultsScroll.scrollTop = 0;
          loadSearch();
        }, 'secondary small', null, { disabled: state.page >= pages })));
    }
    if (prefs.show_review_strip !== false) loadReviews(count);

    const isMobile = window.matchMedia('(max-width: 900px)').matches;

    // 跨页跳题：目标 qid 不在当前页结果里时，也必须单独在阅读区打开目标题，
    // 不能静默改开第 1 题；命中当前页时才高亮选中。
    if (pendingTargetQid) {
      const targetQid = pendingTargetQid;
      pendingTargetQid = null;
      state.selectedQid = targetQid;
      // 目标题如果不在当前页，也写进 hash，刷新/分享后仍能回到同一题。
      setHashParams('practice', {
        ...state.filters,
        page: state.page > 1 ? state.page : '',
        qid: targetQid,
      }, true);
      const inPage = items.some(item => item.qid === targetQid);
      highlightSelectedCard();
      showQuestionView();
      if (isMobile) window.scrollTo({ top: 0, behavior: 'smooth' });
      await openQuestion(targetQid, detailContainer, {
        onSubmitted: () => loadSearch({ keepSelection: true }),
      });
      if (!inPage && state.currentQuestion?.qid === targetQid) {
        detailContainer.prepend(el('p', { class: 'help', style: 'margin:0 0 12px' },
          targetQid.startsWith('app:')
            ? t('practice.fromMyBankHint')
            : t('practice.thisQuestionIsNotOnTheCurrent')));
      }
      return;
    }

    // 提交后刷新列表（keepSelection）：阅读区永远保持当前题与已解锁的答案/解析，
    // 即使当前筛选（未做/待复习）把刚答的题刷出列表，也不自动跳下一题。
    if (keepSelection && state.selectedQid) {
      highlightSelectedCard();
      return;
    }

    const target = items.find(item => item.qid === state.selectedQid) || items[0];
    if (target && (state.selectedQid || !isMobile) && state.practiceView === 'question') {
      await openQuestion(state.selectedQid || target.qid, detailContainer, {
        onSubmitted: () => loadSearch({ keepSelection: true }),
      });
    } else if (target && !isMobile) {
      // 桌面默认展示首条题目，但不高亮为「已选」，避免与点击选中混淆。
      await openQuestion(target.qid, detailContainer, {
        onSubmitted: () => loadSearch({ keepSelection: true }),
      });
    } else {
      detailContainer.replaceChildren(empty(t('practice.pickAQuestionToStartPracticing'), t('practice.selectAQuestionOnTheLeftTo')));
    }
    // 手机上默认停在结果列表，进入题目后再切换视图。
    if (isMobile && !state.selectedQid) showResults();
  }

  refreshFilters();
  await reloadControls();
  await loadSearch();
}
