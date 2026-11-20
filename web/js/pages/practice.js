import { api, ensureCatalog, ensureKnowledgePoints, ensurePreferences } from '../core/api.js';
import { state, saveFilters } from '../core/state.js';
import {
  el, icon, button, badge, empty, loading, field, input, select,
  query, number, typeLabel, highlightKeyword, readableSummary,
} from '../core/dom.js';
import { current, setHashParams } from '../core/router.js';
import { renderKpTree } from '../components/kpTree.js';
import { openQuestion } from './question.js';

const COMMON_FILTERS = [
  ['subject', '学科', 'subjects'],
  ['year', '年份', 'years'],
  ['type', '题型', 'types'],
];

function parseCsv(value) {
  return String(value || '').split(',').map(item => item.trim()).filter(Boolean);
}

/**
 * 轻量多选控件（题型 / 地区）：用后端已经支持的 `type=a,b`、`province=a,b` 契约，
 * 折叠面板避免在手机首屏占高度。选择变化即回调逗号串。
 */
function createMultiSelect({ label, emptyLabel, options, value = '', onChange }) {
  const selected = new Set(parseCsv(value).map(String));
  const summary = el('span', { class: 'multi-select-summary' });
  const panel = el('div', { class: 'multi-select-panel' });
  const root = el('details', { class: 'multi-select' },
    el('summary', { 'aria-label': label }, summary), panel);

  function optionLabel(key) {
    const opt = options.find(item => String(item.value) === String(key));
    return opt ? opt.label : String(key);
  }

  function renderSummary() {
    const labels = [...selected].map(optionLabel);
    summary.textContent = labels.length
      ? (labels.length <= 2 ? labels.join('、') : `${labels.slice(0, 2).join('、')} 等 ${labels.length} 项`)
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
    if (!options.length) panel.append(el('p', { class: 'help' }, '暂无可选项'));
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
    [q.year, q.province].filter(Boolean).join(' · ') || '来源见详情',
    q.number_label ? `原卷第${q.number_label}题` : '',
  ].filter(Boolean).join(' · ');
  const badges = el('span', { class: 'result-card-meta' });
  if (isReview) badges.append(badge('复习', 'amber'));
  badges.append(badge(q.subject || '真题', 'teal'), badge(typeLabel(q, state.catalog)));
  if (q.group_id) badges.append(badge('题组', 'blue'));
  if (q.difficulty && q.difficulty !== '待标注') badges.append(badge(q.difficulty, 'blue'));
  if (q.has_ai_explanation) badges.append(badge('AI讲解', 'teal'));

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
    el('span', { class: 'result-card-title' },
      // 4.1：列表摘要不直接露 [公式1]，公式/图片占位换成可读提示。
      highlightKeyword(readableSummary(q, 120) || '查看完整题目', keyword)),
    badges,
    el('span', { class: 'result-card-meta' },
      el('span', {}, meta),
      isReview && q.review_label ? el('span', {}, q.review_label) : null,
      onSkipReview ? el('span', {
        class: 'skip-review-link', 'aria-hidden': 'true',
        title: '本次跳过，下次再出现（仅鼠标/触屏可用）',
        onClick: event => { event.stopPropagation(); onSkipReview(q.qid); },
      }, '跳过') : null));
  return card;
}

function activeFilterChips(filters, catalog, onChange, names = {}) {
  const chips = el('div', { class: 'filter-chips' });
  const labels = {
    q: '关键词', subject: '学科', type: '题型', year: '年份', year_from: '起始年份',
    year_to: '截止年份', difficulty: '难度', province: '地区', status: '我的状态',
    sort: '排序', has_answer: '有有效答案', has_solution: '有原卷解析',
    has_ai_explanation: '已有AI讲解', include_ai_difficulty: '纳入AI预估难度',
    kp_id: '知识点', method_id: '题型解法',
  };
  const kpNames = names.knowledgePoints || new Map();
  const methodNames = names.methods || new Map();
  for (const [key, label] of Object.entries(labels)) {
    const raw = filters[key];
    if (!raw) continue;
    let text = String(raw);
    if (key === 'subject') text = raw;
    else if (key === 'province') text = parseCsv(raw).join('、');
    else if (key === 'type') text = parseCsv(raw).map(t => catalog?.type_labels?.[t] || t).join('、');
    else if (key === 'sort') text = { year_desc: '最新年份', year_asc: '最早年份', difficulty: '难度优先', accuracy: '正确率由低到高', random: '随机抽题' }[raw] || raw;
    else if (key === 'status') text = { unattempted: '未做过', wrong: '错题本中', due: '待复习错题' }[raw] || raw;
    else if (['has_answer', 'has_solution', 'has_ai_explanation', 'include_ai_difficulty'].includes(key)) text = '已选';
    else if (key === 'kp_id') {
      // 已选知识点显示中文名；映射缺失时退回 `#编号`，绝不出现空白芯片。
      text = parseCsv(raw).map(id => kpNames.get(String(id)) || `知识点#${id}`).join('、');
    } else if (key === 'method_id') {
      text = parseCsv(raw).map(id => methodNames.get(String(id)) || `题型#${id}`).join('、');
    }
    chips.append(el('span', { class: 'filter-chip' }, `${label}：${text}`,
      el('button', {
        type: 'button', 'aria-label': `清除${label}筛选项`,
        onClick: () => onChange(key),
      }, '×')));
  }
  return chips.childNodes.length ? chips : null;
}

export async function renderPractice(container, version, urlParams) {
  const [catalog, kps, prefs, methodsRes] = await Promise.all([
    ensureCatalog(),
    ensureKnowledgePoints(),
    ensurePreferences(),
    api('/methods').catch(() => ({ items: [] })),
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
      'include_ai_difficulty']) {
      if (urlParams.has(k)) f[k] = urlParams.get(k);
    }
    state.filters = f;
    state.page = Math.max(1, Number(urlParams.get('page')) || 1);
    saveFilters();
  }
  // 兼容只设置 state.selectedQid 的旧入口（例如从首页「继续练习」）。
  if (!pendingTargetQid && urlParams && [...urlParams.keys()].length === 0 && state.selectedQid) {
    pendingTargetQid = state.selectedQid;
  }
  if (!state.practiceView) state.practiceView = 'results';

  const filterCard = el('section', { class: 'card filter-card' });
  const keyword = input('搜索题干、材料或试卷名称', {
    value: state.filters.q || '', 'aria-label': '搜索题目', type: 'search',
  });
  const runSearch = async () => {
    state.filters.q = keyword.value.trim();
    state.page = 1;
    state.selectedQid = null;
    pendingTargetQid = null;
    saveFilters();
    await loadSearch();
  };
  const searchButton = button('搜索', runSearch, 'search-submit', 'search', { 'aria-label': '搜索题目' });
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
  const subjectSel = makeSelect('subject', '学科',
    [{ value: '', label: '全部学科' }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    async () => { refreshFilters(); await loadSearch(); });
  const yearSel = makeSelect('year', '年份',
    [{ value: '', label: '全部年份' }, ...(catalog.years || []).map(y => ({ value: String(y), label: `${y} 年` }))],
    async () => { refreshFilters(); await loadSearch(); });
  const typeMulti = createMultiSelect({
    label: '题型（可多选）',
    emptyLabel: '全部题型',
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
  }, icon('sliders'), '更多筛选', el('span', { class: 'filter-badge' }, '0'));

  const difficultySel = select(
    [{ value: '', label: '全部难度' }, ...(catalog.difficulties || ['待标注', '简单', '中等', '困难']).map(d => ({ value: d, label: d }))],
    state.filters.difficulty || '', { 'aria-label': '难度' });
  const provinceMulti = createMultiSelect({
    label: '地区（可多选）',
    emptyLabel: '全部地区',
    options: (catalog.provinces || []).map(p => ({ value: p, label: p })),
    value: state.filters.province || '',
    onChange: () => applyAdvanced(),
  });
  const statusSel = select([
    { value: '', label: '全部状态' },
    { value: 'unattempted', label: '未做过' },
    { value: 'wrong', label: '错题本中' },
    { value: 'due', label: '待复习错题' },
    { value: '未掌握', label: '未掌握' },
    { value: '初步掌握', label: '初步掌握' },
    { value: '比较掌握', label: '比较掌握' },
    { value: '熟练掌握', label: '熟练掌握' },
  ], state.filters.status || '', { 'aria-label': '我的状态' });
  const sortSel = select([
    { value: 'year_desc', label: '最新年份' },
    { value: 'year_asc', label: '最早年份' },
    { value: 'difficulty', label: '难度优先' },
    { value: 'accuracy', label: '正确率由低到高' },
    { value: 'random', label: '随机抽题' },
  ], state.filters.sort || 'year_desc', { 'aria-label': '排序' });
  const yearFrom = select([{ value: '', label: '不限' }, ...(catalog.years || []).map(y => ({ value: String(y), label: `${y}` }))],
    state.filters.year_from || '', { 'aria-label': '起始年份' });
  const yearTo = select([{ value: '', label: '不限' }, ...(catalog.years || []).map(y => ({ value: String(y), label: `${y}` }))],
    state.filters.year_to || '', { 'aria-label': '截止年份' });
  const hasAnsChk = el('input', { type: 'checkbox', checked: state.filters.has_answer === 'true' });
  const hasSolChk = el('input', { type: 'checkbox', checked: state.filters.has_solution === 'true' });
  const hasAiChk = el('input', { type: 'checkbox', checked: state.filters.has_ai_explanation === 'true' });
  const aiDiffChk = el('input', { type: 'checkbox', checked: state.filters.include_ai_difficulty === 'true' });
  const methodSel = select(
    [{ value: '', label: '全部题型/解法' }, ...allMethods.map(m => ({ value: String(m.id), label: `${m.subject}·${m.name} (${m.question_count})` }))],
    state.filters.method_id || '', { 'aria-label': '题型与解法' });

  const advancedControls = [difficultySel, statusSel, sortSel, yearFrom, yearTo, methodSel,
    hasAnsChk, hasSolChk, hasAiChk, aiDiffChk];
  const applyAdvanced = async () => {
    state.filters.difficulty = difficultySel.value;
    state.filters.province = provinceMulti.value;
    state.filters.status = statusSel.value;
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
  const kpToggle = button('按知识点筛选', () => {
    kpPanel.classList.toggle('hide');
    refreshKpPanel();
  }, 'ghost small', 'tag');

  const chipsMount = el('div');
  function refreshFilters() {
    moreToggle.lastChild.textContent = String(
      ['difficulty', 'province', 'status', 'year_from', 'year_to', 'method_id',
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
          ? `${state.filters.subject} · 知识点目录（支持多选）`
          : '全部学科 · 知识点目录（建议先选学科）'),
        selIds.length ? button('清除知识点筛选', async () => {
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
      field('难度', difficultySel),
      provinceMulti.node,
      field('我的状态', statusSel),
      field('排序', sortSel),
      field('年份区间', el('div', { class: 'row' }, yearFrom, el('span', { class: 'help' }, '至'), yearTo)),
      allMethods.length ? field('题型与解法', methodSel) : null),
    el('div', { class: 'row wrap', style: 'margin-top:12px' },
      el('label', { class: 'check-label' }, hasAnsChk, '有有效答案'),
      el('label', { class: 'check-label' }, hasSolChk, '有原卷解析'),
      el('label', { class: 'check-label' }, hasAiChk, '已有AI讲解'),
      el('label', { class: 'check-label' }, aiDiffChk, '纳入AI预估难度'),
      kpToggle),
    kpPanel);

  filterCard.append(
    el('div', { class: 'filter-head' },
      el('h2', {}, '题库练习'),
      el('div', { class: 'row' }, moreToggle,
        button('清空筛选', async () => {
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
      field('学科', subjectSel), field('年份', yearSel), typeMulti.node),
    filterMore,
    chipsMount);

  const dueContainer = el('div');
  const resultsList = el('div', { class: 'question-list' });
  const resultsScroll = el('div', { class: 'results-scroll' }, resultsList);
  const resultsCount = el('p', { class: 'results-count' }, '正在加载…');
  const resultsPane = el('aside', { class: 'results-pane', 'aria-label': '题目列表' },
    el('header', { class: 'results-heading' },
      el('h2', {}, '题目列表'), resultsCount),
    dueContainer, resultsScroll);

  const readingBack = el('button', {
    type: 'button', class: 'reading-back',
    onClick: () => showResults(true),
  }, '← 返回结果');
  const detailContainer = el('div', { class: 'reading-content', id: 'question-detail' });
  const actionsHost = el('div', { class: 'reading-actions', id: 'reading-actions', hidden: true });
  const readingPane = el('section', { class: 'reading-pane', 'aria-label': '题目阅读与作答' },
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
        el('h1', {}, '把每一题，做明白。'),
        el('p', {}, '桌面在左侧筛选、右侧阅读；手机先看结果，进入题目后可随时返回。'))),
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
        `到期错题 ${reviews.total} 道`,
        el('button', {
          type: 'button', class: 'reading-back', style: 'margin-left:auto;min-height:32px;display:inline-block',
          onClick: () => { dueContainer.replaceChildren(); },
          'aria-label': '关闭到期复习提示',
        }, '×')));
    const row = el('div', { class: 'due-items' });
    for (const item of reviews.items.slice(0, 4)) {
      const q = item.question || item;
      row.append(el('button', {
        type: 'button', class: 'due-item',
        onClick: () => openResult({ qid: q.qid || item.qid }),
      }, `${q.subject || '错题'} · ${readableSummary(q, 20) || '重新练习'}`));
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
        `到期错题暂时加载失败：${error.message}`,
        button('重试', () => loadReviews(count), 'secondary small', 'repeat')));
    }
  }

  async function loadSearch({ keepSelection = false } = {}) {
    const count = ++searchCounter;
    const hashParams = { ...state.filters, page: state.page > 1 ? state.page : '' };
    if (state.selectedQid) hashParams.qid = state.selectedQid;
    setHashParams('practice', hashParams, true);
    resultsCount.textContent = '正在加载…';
    resultsList.replaceChildren(loading('正在筛选题目…'));
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
        `题目列表加载失败：${error.message}`,
        button('重新筛选', () => loadSearch(), 'secondary small', 'repeat')));
      resultsCount.textContent = '加载失败';
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
    resultsCount.textContent = `找到 ${number(results.total)} 道题 · 第 ${results.page || state.page} 页`
      + (lastReviewCount ? ` · 含 ${lastReviewCount} 道穿插复习` : '');
    resultsList.replaceChildren();
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
      resultsList.append(empty('没有匹配的题目', '试试减少筛选条件，或者换一个关键词。'));
    }
    const pages = Math.max(1, Math.ceil((results.total || 0) / state.pageSize));
    if (items.length) {
      resultsList.append(el('div', { class: 'pagination', style: 'margin:14px 4px 4px' },
        button('上一页', () => {
          state.page = Math.max(1, state.page - 1);
          resultsScroll.scrollTop = 0;
          loadSearch();
        }, 'secondary small', null, { disabled: state.page <= 1 }),
        el('span', {}, `${state.page} / ${pages}`),
        button('下一页', () => {
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
          '该题不在当前筛选结果的这一页中，已单独打开；可点「返回结果」继续浏览列表。'));
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
      detailContainer.replaceChildren(empty('选一题，开始练习', '在左侧点选题目后，这里显示材料、选项与作答。'));
    }
    // 手机上默认停在结果列表，进入题目后再切换视图。
    if (isMobile && !state.selectedQid) showResults();
  }

  refreshFilters();
  await reloadControls();
  await loadSearch();
}
