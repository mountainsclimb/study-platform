/* 试卷工坊 · 组卷工作台（手工 / AI）与错题巩固卷。
 *
 * 手工组卷：左侧「我的题库 / 高考真题库」双来源检索 → 加入试题篮（跨筛选保留）→
 * 右侧调题序/分值/分节 → POST /api/paperlab/papers（items 用 ref=`app:ID` / `bank:qid`）。
 * AI 组卷：POST /api/paperlab/papers/compose → paper-compose 任务（core/jobs.js SSE 进度/取消/续跑）；
 * 同步预检 422（候选不足/题型不足/知识点配额/难度范围）与任务失败的差额原因原样展示，不缩水。
 * 错题巩固卷：GET /revision/options → replay 直接建卷 / consolidate 走 paper-revision AI 任务。
 */
import { api, ensureCatalog, ensureKnowledgePoints, refreshProviders, errorText } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, button, badge, notice, empty, loading, busy, toast, number, field, input,
  select, readableSummary,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { defaultScoreFor, typeLabelOf } from './paperDocument.js';
import { t } from '../core/i18n.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TYPE_OPTIONS = [
  { value: '', get label() { return t('practice.allTypes'); } },
  { value: 'SINGLE', get label() { return t('question.type.single_choice'); } },
  { value: 'MULTIPLE', get label() { return t('question.type.multi_choice'); } },
  { value: 'BLANK', get label() { return t('question.type.fill_blank'); } },
  { value: 'SUBJECTIVE', get label() { return t('mybank.subjective'); } },
  { value: 'MIXED', get label() { return t('mybank.mixed'); } },
];
const BANK_TYPE_TO_QTYPE = {
  single_choice: 'SINGLE',
  multi_choice: 'MULTIPLE',
  fill_blank: 'BLANK',
  calculation: 'SUBJECTIVE',
  subjective: 'SUBJECTIVE',
  mixed: 'MIXED',
};
const COMPOSE_MODES = [
  { value: 'smart', get label() { return t('home.paperKind.smart'); }, get description() { return t('paper.compose.autoSelectQuestionsBalancedByCountType'); } },
  { value: 'knowledge', get label() { return t('home.paperKind.knowledge'); }, get description() { return t('paper.compose.selectQuestionsAroundSpecifiedKnowledgePointWeights'); } },
  { value: 'wrong', get label() { return t('paper.compose.wrongAnswerThemedComposition'); }, get description() { return t('paper.compose.strengthenAroundKnowledgePointsFromYourOwn'); } },
  { value: 'layered', get label() { return t('home.paperKind.layered'); }, get description() { return t('paper.compose.organizeQuestionsByDifficultyTiers'); } },
];
const PICKER_PAGE_SIZE = 8;
const MAX_PAPER_ITEMS = 60;
const MAX_COMPOSE_COUNT = 60;

let basket = [];
let draft = { title: '', subject: '', grade: '', instructions: '' };
let composeMode = 'manual';
let pickerSource = 'app';
let pickerPage = 1;
let pickerKeyword = '';
let pickerType = '';
let pickerGrade = '';
let aiForm = {
  mode: 'smart',
  subject: '',
  grade: '',
  count: 10,
  typeCounts: { SINGLE: 0, MULTIPLE: 0, BLANK: 0, SUBJECTIVE: 0 },
  difficultyTarget: '',
  tolerance: '0.1',
  kpWeights: [],
  source: 'all',
  knowledgeTags: '',
};
let revisionState = { subject: '', mode: 'replay', count: 5, kpIds: new Set() };

onSessionReset(() => {
  basket = [];
  draft = { title: '', subject: '', grade: '', instructions: '' };
  composeMode = 'manual';
  pickerSource = 'app';
  pickerPage = 1;
  pickerKeyword = '';
  pickerType = '';
  pickerGrade = '';
  aiForm = {
    mode: 'smart', subject: '', grade: '', count: 10,
    typeCounts: { SINGLE: 0, MULTIPLE: 0, BLANK: 0, SUBJECTIVE: 0 },
    difficultyTarget: '', tolerance: '0.1', kpWeights: [], source: 'all', knowledgeTags: '',
  };
  revisionState = { subject: '', mode: 'replay', count: 5, kpIds: new Set() };
});

function basketRefs() {
  return new Set(basket.map(item => item.ref));
}

function basketTotal() {
  return basket.reduce((sum, item) => sum + (Number(item.score) || 0), 0);
}

function subjectOptions(meta) {
  return (meta?.subjects || []).map(subject => ({ value: subject, label: subject }));
}

function gradeOptions(meta) {
  return (meta?.grades || []).map(grade => ({ value: grade, label: grade }));
}

function bankCardToBasket(item) {
  const rawType = String(item.type || '');
  const qtype = BANK_TYPE_TO_QTYPE[rawType] || 'SUBJECTIVE';
  return {
    ref: `bank:${item.qid}`,
    source: 'bank',
    id: item.qid,
    type: qtype,
    type_label: item.type_label || typeLabelOf(qtype),
    subject: item.subject || '',
    knowledge_tags: (item.knowledge_points || []).map(point => point.name).filter(Boolean),
    difficulty: item.difficulty && item.difficulty !== '待标注' ? item.difficulty : '',
    stem: String(item.stem_plain || ''),
    origin: [item.year, item.province, item.paper_name, item.number_label].filter(Boolean).join(' · '),
    score: defaultScoreFor(qtype),
    section: '',
  };
}

function appCardToBasket(item) {
  const content = item.content || {};
  return {
    ref: `app:${item.id}`,
    source: 'app',
    id: item.id,
    type: item.type || 'SUBJECTIVE',
    type_label: typeLabelOf(item.type),
    subject: item.subject || '',
    knowledge_tags: (item.knowledge_tags || []).slice(0, 5),
    difficulty: item.difficulty === null || item.difficulty === undefined ? '' : item.difficulty,
    stem: String(content.stem || item.stem_preview || ''),
    origin: '',
    score: defaultScoreFor(item.type),
    section: '',
  };
}

// 平台精选卡 → 试题篮条目：ref 用统一题目引用 `official:<public_id>`
// （WS19 建卷快照支持该引用），前端只做展示与引用，不复制题目内容。
function officialCardToBasket(item) {
  const publicId = String(item.public_id || item.qid || '').replace(/^official:/, '');
  const rawType = String(item.type || '');
  const qtype = BANK_TYPE_TO_QTYPE[rawType] || 'SUBJECTIVE';
  // 平台精选卡的知识点是 knowledge_tags 字符串数组（knowledge_points 为空）；
  // 题库卡则是 knowledge_points 对象数组，两种来源都兼容。
  const kpSource = Array.isArray(item.knowledge_points) && item.knowledge_points.length
    ? item.knowledge_points
    : (item.knowledge_tags || []);
  return {
    ref: `official:${publicId}`,
    source: 'official',
    id: publicId,
    type: qtype,
    type_label: item.type_label || typeLabelOf(qtype),
    subject: item.subject || '',
    knowledge_tags: kpSource
      .map(point => (typeof point === 'string' ? point : point?.name))
      .filter(Boolean),
    difficulty: item.difficulty && item.difficulty !== '待标注' ? item.difficulty : '',
    stem: String(item.stem_plain || ''),
    origin: publicId ? t('paperlab.source.publicId', { v1: publicId }) : '',
    score: defaultScoreFor(qtype),
    section: '',
  };
}

// 选题来源徽标：我的题库 / 高考题库 / 平台精选（缺省按高考题库）。
const PICKER_SOURCE_KEYS = {
  app: 'paperlab.source.app',
  bank: 'paperlab.source.bank',
  official: 'paperlab.source.official',
  all: 'paperlab.source.all',
};
const PICKER_REVIEW_KEYS = {
  community: 'paperlab.source.reviewCommunity',
  community_review: 'paperlab.source.reviewCommunity',
  verified: 'paperlab.source.reviewVerified',
};

function pickerSourceBadge(source) {
  const key = PICKER_SOURCE_KEYS[source];
  if (!key) return null;
  return badge(t(key), source === 'official' ? 'blue' : source === 'app' ? 'teal' : '');
}

function pickerReviewBadge(item) {
  const status = String(item?.review_status || '');
  const key = PICKER_REVIEW_KEYS[status];
  return key ? badge(t(key), status === 'verified' ? 'teal' : 'amber') : null;
}

function addToBasket(card) {
  if (basket.some(item => item.ref === card.ref)) {
    toast(t('paper.compose.thisQuestionIsAlreadyInTheBasket'), true);
    return;
  }
  if (basket.length >= MAX_PAPER_ITEMS) {
    toast(t('paper.compose.aPaperCanHoldAtMostQuestions', { v1: MAX_PAPER_ITEMS }), true);
    return;
  }
  basket.push({ ...card });
  toast(t('paper.compose.addedToTheBasketQuestionsNow', { v1: basket.length }));
}

function difficultyLabelOf(value) {
  if (value === '' || value === null || value === undefined) return t('mybank.difficultyUnlabeled');
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (num <= 0.3) return t('practice.difficultyEasy');
  if (num <= 0.45) return t('mybank.easier');
  if (num <= 0.65) return t('practice.difficultyMedium');
  if (num <= 0.85) return t('mybank.harder');
  return t('practice.difficultyHard');
}

/* ---------------------------------------------------------------------------
 * 手工组卷
 * ------------------------------------------------------------------------- */

function renderManualCompose({ mount, meta, version, onOpenPaper, officialSourcesSupported = null }) {
  const root = el('div', { class: 'pl-compose-manual' });
  const basketHost = el('aside', { class: 'card pl-basket' });
  const resultsHost = el('div', { class: 'pl-picker-results' });
  const draftCard = el('section', { class: 'card pl-compose-draft' });

  const subjectOptionsList = [{ value: '', label: t('insight.subjectAll') }, ...subjectOptions(meta)];
  const gradeOptionsList = [{ value: '', label: t('mybank.allGrades') }, ...gradeOptions(meta)];
  const appSubject = select(subjectOptionsList, draft.subject);
  const appGrade = select(gradeOptionsList, pickerGrade);
  const appType = select(TYPE_OPTIONS, pickerType);
  const keywordInput = input(t('paper.compose.searchStemsKnowledgePoints'), { value: pickerKeyword, maxlength: '120' });
  let bankCatalog = null;
  const bankSubject = select(subjectOptionsList, draft.subject);
  const bankType = select([{ value: '', label: t('practice.allTypes') }], '');
  const bankKeyword = input(t('paper.compose.searchStemsInTheOfficialExamBank'), { maxlength: '120' });
  const pageLabel = el('span', { class: 'help pl-picker-page' });

  // ----- 试卷信息 -----
  const titleInput = input(t('paper.compose.paperTitleRequired'), { value: draft.title, maxlength: '120' });
  titleInput.addEventListener('input', () => { draft.title = titleInput.value; });
  const draftSubject = select([{ value: '', label: t('paper.compose.chooseASubject') }, ...subjectOptions(meta)], draft.subject);
  draftSubject.addEventListener('change', () => {
    draft.subject = draftSubject.value;
    appSubject.value = draft.subject;
    bankSubject.value = draft.subject;
  });
  const draftGrade = select([{ value: '', label: t('paper.import.gradeOptional') }, ...gradeOptions(meta)], draft.grade);
  draftGrade.addEventListener('change', () => { draft.grade = draftGrade.value; });
  const instructionsInput = el('textarea', { rows: 2, placeholder: t('paper.compose.examInstructionsOptional') });
  instructionsInput.value = draft.instructions;
  instructionsInput.addEventListener('input', () => { draft.instructions = instructionsInput.value; });
  const saveBtn = button(t('paper.compose.savePaper'), null, '', 'check');
  draftCard.append(
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paper.compose.manualComposition')),
        el('p', { class: 'help' }, t('paper.compose.pickQuestionsFromMyQuestionBankOr')),
        el('p', { class: 'help' }, t('paperlab.source.snapshotHint'))),
      badge(t('paper.compose.basketQuestions', { v1: basket.length }), basket.length ? 'teal' : '')),
    el('div', { class: 'pl-draft-grid' },
      field(t('paper.compose.paperTitle'), titleInput),
      field(t('common.subject'), draftSubject),
      field(t('mybank.grade'), draftGrade),
      field(t('paper.compose.examInstructions'), instructionsInput)),
    el('div', { class: 'row end' }, saveBtn));

  // ----- 试题篮 -----
  function renderBasket() {
    basketHost.replaceChildren(
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('paper.compose.questionBasket')),
          el('p', { class: 'help' }, t('paper.compose.questionsPointsTotal', { v1: basket.length, v2: MAX_PAPER_ITEMS, v3: number(basketTotal()) }))),
        button(t('settings.clear'), () => { basket = []; renderBasket(); renderDraftHeader(); }, 'ghost small', 'trash')),
    );
    if (!basket.length) {
      basketHost.append(el('p', { class: 'help pl-basket-empty' }, t('paper.compose.noQuestionsSelectedYetSearchOnThe')));
      return;
    }
    const list = el('div', { class: 'pl-basket-list' });
    basket.forEach((item, index) => {
      const scoreInput = el('input', {
        type: 'number', class: 'input pl-score-input', min: '0', max: '100', step: '0.5',
        value: String(item.score), 'aria-label': t('paper.compose.pointsForQuestion', { v1: index + 1 }),
      });
      scoreInput.addEventListener('input', () => {
        item.score = Number(scoreInput.value) || 0;
        renderBasketFooter();
        renderDraftHeader();
      });
      const sectionInput = input(t('paper.compose.sectionEGMultipleChoice'), { value: item.section, maxlength: '40' });
      sectionInput.addEventListener('input', () => { item.section = sectionInput.value.trim(); });
      list.append(el('article', { class: 'pl-basket-item', 'data-ref': item.ref },
        el('div', { class: 'pl-basket-item-head' },
          el('span', { class: 'pl-basket-index' }, `${index + 1}`),
          badge(item.type_label, 'blue'),
          pickerSourceBadge(item.source),
          badge(difficultyLabelOf(item.difficulty), ''),
          el('div', { class: 'row pl-basket-order' },
            button(t('paper.compose.moveUp'), () => {
              if (index === 0) return;
              const [moved] = basket.splice(index, 1);
              basket.splice(index - 1, 0, moved);
              renderBasket();
            }, 'ghost small', 'chevron', { disabled: index === 0 }),
            button(t('paper.compose.moveDown'), () => {
              if (index === basket.length - 1) return;
              const [moved] = basket.splice(index, 1);
              basket.splice(index + 1, 0, moved);
              renderBasket();
            }, 'ghost small', 'chevron', { disabled: index === basket.length - 1 }),
            button(t('upload.remove'), () => { basket.splice(index, 1); renderBasket(); renderDraftHeader(); },
              'ghost small', 'trash'))),
        el('p', { class: 'pl-basket-stem', 'data-no-i18n': '' },
          readableSummary({ stem_plain: item.stem }, 90)),
        item.knowledge_tags.length
          ? el('div', { class: 'pl-basket-tags', 'data-no-i18n': '' },
            item.knowledge_tags.slice(0, 4).map(tag => badge(tag, 'teal')))
          : null,
        el('div', { class: 'pl-basket-fields' },
          field(t('paper.compose.points'), scoreInput),
          field(t('paper.compose.section'), sectionInput))));
    });
    basketHost.append(list);
    renderBasketFooter();
  }

  function renderBasketFooter() {
    let footer = basketHost.querySelector('.pl-basket-footer');
    if (!footer) {
      footer = el('div', { class: 'pl-basket-footer' });
      basketHost.append(footer);
    }
    footer.replaceChildren(el('div', { class: 'row between' },
      el('strong', {}, t('paper.compose.pointsTotal', { v1: number(basketTotal()) })),
      el('span', { class: 'help' }, t('paper.compose.thePaperIsCreatedInTheCurrent'))));
    return footer;
  }

  function renderDraftHeader() {
    const headerBadge = draftCard.querySelector('.card-header .badge');
    if (headerBadge) headerBadge.textContent = t('paper.compose.basketQuestions', { v1: basket.length });
    const buttonHost = draftCard.querySelector('.card-header > .row, .card-header');
    void buttonHost;
  }

  // ----- 多来源检索（我的题库 / 高考题库 / 平台精选 / 全部） -----
  // 平台精选与全部来源依赖 WS19 `/api/questions?source=official|all`：能力探测由页面
  // （paperlab.js）完成后传入；未通过时这两个页签不渲染，行为与旧版双来源一致。
  const pickerSourceTabs = [
    { value: 'app', label: () => t('paperlab.source.app') },
    { value: 'bank', label: () => t('paperlab.source.bank') },
    { value: 'official', label: () => t('paperlab.source.official'), optional: true },
    { value: 'all', label: () => t('paperlab.source.all'), optional: true },
  ];
  const sourceTabs = el('div', {
    class: 'tabs pl-picker-tabs', role: 'tablist', 'aria-label': t('paperlab.source.label'),
  });
  const optionalSourceTabs = [];
  for (const def of pickerSourceTabs) {
    const tab = el('button', {
      type: 'button', class: 'tab', role: 'tab', 'data-source': def.value,
      onClick: () => switchSource(def.value),
    }, def.label());
    if (def.optional) {
      tab.hidden = true;
      optionalSourceTabs.push(tab);
    }
    sourceTabs.append(tab);
  }
  const appFilterRow = el('div', { class: 'pl-picker-filters' },
    field(t('common.subject'), appSubject), field(t('mybank.grade'), appGrade), field(t('upload.fieldType'), appType), field(t('practice.keyword'), keywordInput));
  const bankFilterRow = el('div', { class: 'pl-picker-filters' },
    field(t('common.subject'), bankSubject), field(t('upload.fieldType'), bankType), field(t('practice.keyword'), bankKeyword));

  // 来源能力：页面传入布尔值；null（未探测）时保守隐藏平台精选/全部来源。
  function applyOfficialSources(supported) {
    const enabled = Boolean(supported);
    for (const tab of optionalSourceTabs) tab.hidden = !enabled;
    if (!enabled && (pickerSource === 'official' || pickerSource === 'all')) pickerSource = 'bank';
    syncSourceTabs();
  }
  applyOfficialSources(officialSourcesSupported);

  function switchSource(source) {
    pickerSource = source;
    pickerPage = 1;
    syncSourceTabs();
    if (source === 'bank' || source === 'official' || source === 'all') loadBankCatalog().catch(() => {});
    loadPicker();
  }

  function syncSourceTabs() {
    [...sourceTabs.children].forEach(tab => {
      const active = tab.dataset.source === pickerSource;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    appFilterRow.classList.toggle('hide', pickerSource !== 'app');
    bankFilterRow.classList.toggle('hide', pickerSource === 'app');
  }

  async function loadBankCatalog() {
    const catalog = await ensureCatalog();
    bankCatalog = catalog;
    if (bankType.options.length > 1) return;
    const labels = catalog?.type_labels || {};
    for (const value of catalog?.types || []) {
      bankType.append(el('option', { value }, labels[value] || value));
    }
  }

  function pickerQuery() {
    if (pickerSource === 'app') {
      const params = new URLSearchParams({ page: String(pickerPage), page_size: String(PICKER_PAGE_SIZE) });
      const values = {
        subject: appSubject.value, grade: appGrade.value,
        qtype: appType.value, keyword: keywordInput.value.trim(),
      };
      for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
      return { path: `/paperlab/questions?${params}`, pageLabel: t('paper.compose.questions') };
    }
    const params = new URLSearchParams({ page: String(pickerPage), page_size: String(PICKER_PAGE_SIZE) });
    const values = { subject: bankSubject.value, type: bankType.value, q: bankKeyword.value.trim() };
    // 高考题库走默认（省略 source 保持向后兼容）；平台精选/全部显式传来源（WS19 契约）。
    if (pickerSource === 'official' || pickerSource === 'all') values.source = pickerSource;
    for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
    return { path: `/questions?${params}`, pageLabel: t('paper.compose.questions') };
  }

  async function loadPicker() {
    resultsHost.replaceChildren(loading(t('paper.compose.searchingQuestions')));
    const { path } = pickerQuery();
    try {
      const data = await api(path);
      if (!current(version)) return;
      renderResults(Array.isArray(data.items) ? data.items : [], data);
    } catch (error) {
      if (!current(version)) return;
      resultsHost.replaceChildren(
        notice(error.message || t('paper.compose.failedToSearchQuestionsPleaseTryAgain'), 'error'),
        el('div', { class: 'row' }, button(t('common.retry'), event => busy(event.currentTarget, loadPicker)), ''));
    }
  }

  function renderResults(items, data) {
    resultsHost.replaceChildren();
    const total = Number(data.total) || 0;
    const existing = basketRefs();
    pageLabel.textContent = t('mybank.questionsPageOf', { v1: total, v2: pickerPage, v3: Math.max(1, Math.ceil(total / PICKER_PAGE_SIZE)) });
    if (!items.length) {
      resultsHost.append(empty(t('paper.compose.noQuestionsFound'), t('paper.compose.tryAnotherSubjectTypeOrABroader'), null, 'search'));
      return;
    }
    const list = el('div', { class: 'pl-picker-list' });
    for (const item of items) {
      const isApp = pickerSource === 'app';
      // 全部来源时单条按 item.source 判定；平台精选页签/带 public_id/official: 引用
      // 的卡固定为 official（WS19 精简 payload 可能只有 qid 前缀）。
      const isOfficialItem = !isApp && (pickerSource === 'official' || item.source === 'official'
        || Boolean(item.public_id) || String(item.qid || '').startsWith('official:'));
      const card = isApp ? appCardToBasket(item) : isOfficialItem ? officialCardToBasket(item) : bankCardToBasket(item);
      const allowed = isApp ? true : item.practice_allowed !== false;
      const added = existing.has(card.ref);
      const knowledges = isApp
        ? (item.knowledge_tags || [])
        : isOfficialItem
          ? (item.knowledge_tags || [])
          : (item.knowledge_points || []).map(point => point.name).filter(Boolean);
      const stemText = isApp ? String(item.content?.stem || item.stem_preview || '') : String(item.stem_plain || '');
      list.append(el('article', { class: 'pl-picker-card', 'data-ref': card.ref },
        el('div', { class: 'pl-picker-card-head' },
          badge(card.type_label, 'blue'),
          pickerSourceBadge(card.source),
          isOfficialItem ? pickerReviewBadge(item) : null,
          badge(difficultyLabelOf(card.difficulty), ''),
          isApp && item.verify_status === 'verified' ? badge(t('mybank.verified'), 'teal') : null,
          item.has_answer ? badge(t('paper.compose.hasAnswer'), '') : badge(t('mybank.missingAnswer'), 'amber')),
        el('p', { class: 'pl-picker-stem', 'data-no-i18n': '' },
          readableSummary({ stem_plain: stemText }, 120)),
        knowledges.length
          ? el('div', { class: 'pl-picker-tags', 'data-no-i18n': '' },
            knowledges.slice(0, 4).map(tag => badge(tag, 'teal')))
          : null,
        card.origin ? el('p', { class: 'help', 'data-no-i18n': '' }, card.origin) : null,
        el('div', { class: 'row pl-picker-actions' },
          button(added ? t('paper.compose.alreadyInBasket') : t('paper.compose.addToBasket'), () => {
            addToBasket(card);
            renderBasket();
            renderResults(items, data);
            renderDraftHeader();
          }, added ? 'secondary small' : 'soft small', 'plus',
          { disabled: added || !allowed }),
          allowed ? null : badge(t('paper.compose.thisQuestionCannotBeUsedInA'), 'red'))));
    }
    resultsHost.append(list, el('div', { class: 'pagination pl-picker-pagination' },
      pageLabel,
      el('div', { class: 'row' },
        button(t('common.prevPage'), () => { if (pickerPage > 1) { pickerPage -= 1; loadPicker(); } },
          'secondary small', 'chevron', { disabled: pickerPage <= 1 }),
        button(t('common.nextPage'), () => {
          const totalPages = Math.max(1, Math.ceil(total / PICKER_PAGE_SIZE));
          if (pickerPage < totalPages) { pickerPage += 1; loadPicker(); }
        }, 'secondary small', 'chevron',
        { disabled: pickerPage >= Math.max(1, Math.ceil(total / PICKER_PAGE_SIZE)) }))));
  }

  for (const control of [appSubject, appGrade, appType]) {
    control.addEventListener('change', () => { pickerPage = 1; loadPicker(); });
  }
  keywordInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') { pickerPage = 1; loadPicker(); }
  });
  for (const control of [bankSubject, bankType]) {
    control.addEventListener('change', () => { pickerPage = 1; loadPicker(); });
  }
  bankKeyword.addEventListener('keydown', event => {
    if (event.key === 'Enter') { pickerPage = 1; loadPicker(); }
  });

  saveBtn.addEventListener('click', () => busy(saveBtn, async () => {
    if (!draft.title.trim()) throw new Error(t('paper.compose.enterAPaperTitle'));
    if (!draft.subject) throw new Error(t('paper.compose.chooseAPaperSubject'));
    if (!basket.length) throw new Error(t('paper.compose.theBasketIsEmptyAddQuestionsFirst'));
    const payload = {
      title: draft.title.trim(),
      kind: 'manual',
      subject: draft.subject,
      grade: draft.grade,
      instructions: draft.instructions.trim(),
      // ref 自带来源前缀（app: / bank: / official:）：服务端建卷时按引用固定
      // 题目来源与版本快照，前端不在 payload 里重复来源字段。
      items: basket.map(item => ({
        ref: item.ref,
        score: Number(item.score) || 0,
        section: item.section || '',
      })),
    };
    try {
      const result = await api('/paperlab/papers', { method: 'POST', data: payload });
      toast(t('paper.compose.paperSavedOpeningPaperDetails'));
      basket = [];
      draft = { title: '', subject: draft.subject, grade: '', instructions: '' };
      onOpenPaper(result.paper?.id);
    } catch (error) {
      const message = error.message || t('paper.compose.failedToSaveThePaperPleaseTry');
      draftCard.querySelector('.pl-draft-error')?.remove();
      draftCard.insertBefore(notice(message, 'error'), draftCard.lastElementChild);
      throw error;
    }
  }));

  const pickerCard = el('section', { class: 'card pl-picker' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paper.compose.questionPicker')),
        el('p', { class: 'help' }, t('paperlab.source.pickerHelp')))),
    sourceTabs, appFilterRow, bankFilterRow, resultsHost);
  switchSource('app');
  syncSourceTabs();
  renderBasket();
  root.append(draftCard, el('div', { class: 'pl-compose-grid' }, pickerCard, basketHost));
  mount.replaceChildren(root);
}

/* ---------------------------------------------------------------------------
 * AI 组卷
 * ------------------------------------------------------------------------- */

function renderAiCompose({ mount, meta, version, onOpenPaper }) {
  const host = el('div', { class: 'pl-compose-ai' });
  const errorBox = el('div', { class: 'pl-compose-error' });
  const candidateBox = el('div', { class: 'pl-compose-candidates' });
  const jobHost = el('div', { class: 'pl-compose-job' });
  const jobSummary = el('div', { class: 'pl-compose-job-summary' });
  const picker = createModelPicker('paper-compose', { showModeToggle: false });
  const noProvider = !(state.providers || []).length;
  const startBtn = button(t('paper.compose.startAiComposition'), null, '', 'sparkle');
  const kpListHost = el('div', { class: 'pl-kp-weight-list' });

  const subjectSelect = select([{ value: '', label: t('paper.compose.chooseASubject') }, ...subjectOptions(meta)], aiForm.subject);
  const gradeSelect = select([{ value: '', label: t('paper.import.gradeOptional') }, ...gradeOptions(meta)], aiForm.grade);
  const modeSelect = select(COMPOSE_MODES.map(item => ({ value: item.value, label: item.label })), aiForm.mode);
  const modeHelp = el('p', { class: 'help' });
  const countInput = el('input', {
    type: 'number', class: 'input', min: '1', max: String(MAX_COMPOSE_COUNT),
    value: String(aiForm.count), 'aria-label': t('paper.compose.numberOfQuestions'),
  });
  const typeCountInputs = {};
  const typeCountRow = el('div', { class: 'pl-type-counts' },
    Object.entries({ SINGLE: t('question.type.single_choice'), MULTIPLE: t('question.type.multi_choice'), BLANK: t('question.type.fill_blank'), SUBJECTIVE: t('mybank.subjective') })
      .map(([type, label]) => {
        const control = el('input', {
          type: 'number', class: 'input', min: '0', max: String(MAX_COMPOSE_COUNT),
          value: String(aiForm.typeCounts[type] || 0), 'aria-label': t('paper.compose.count', { v1: label }),
        });
        control.addEventListener('input', () => {
          aiForm.typeCounts[type] = Number(control.value) || 0;
        });
        typeCountInputs[type] = control;
        return field(label, control);
      }));
  const difficultySelect = select([
    { value: '', label: t('paper.compose.anyDifficulty') },
    { value: '0.2', label: t('paper.compose.easy020') },
    { value: '0.35', label: t('paper.compose.easier035') },
    { value: '0.5', label: t('paper.compose.medium050') },
    { value: '0.65', label: t('paper.compose.harder065') },
    { value: '0.8', label: t('paper.compose.hard080') },
  ], aiForm.difficultyTarget);
  const toleranceSelect = select([
    { value: '0.05', label: '±0.05' }, { value: '0.1', label: '±0.10' },
    { value: '0.15', label: '±0.15' }, { value: '0.2', label: '±0.20' },
    { value: '0.3', label: '±0.30' },
  ], aiForm.tolerance);
  const kpNameSelect = select([{ value: '', label: t('paper.compose.chooseKnowledgePoints') }], '');
  const kpWeightInput = el('input', {
    type: 'number', class: 'input pl-kp-weight-input', min: '1', max: '100', value: '50',
    'aria-label': t('paper.compose.knowledgePointWeights'),
  });
  const sourceSelect = select([
    { value: 'all', label: t('paper.compose.allSources') }, { value: 'app', label: t('home.status.bankTotal') },
    { value: 'bank', label: t('paper.compose.officialExamBank') },
  ], aiForm.source);
  const tagsInput = input(t('paper.compose.knowledgePointTagFilterCommaSeparatedOptional'), { value: aiForm.knowledgeTags });
  let knowledgePoints = [];

  function syncModeHelp() {
    const mode = COMPOSE_MODES.find(item => item.value === modeSelect.value);
    modeHelp.textContent = mode ? mode.description : '';
  }

  async function loadKnowledgePoints() {
    kpListHost.replaceChildren(el('p', { class: 'help' }, t('paper.compose.loadingKnowledgePoints')));
    kpNameSelect.replaceChildren(el('option', { value: '' }, t('paper.compose.chooseKnowledgePoints')));
    knowledgePoints = [];
    if (!subjectSelect.value) {
      kpListHost.replaceChildren(el('p', { class: 'help' }, t('paper.compose.chooseASubjectThenComposeByKnowledge')));
      return;
    }
    try {
      const items = await ensureKnowledgePoints(true);
      knowledgePoints = (items || []).filter(item => item.subject === subjectSelect.value);
    } catch {
      knowledgePoints = [];
    }
    for (const point of knowledgePoints) {
      kpNameSelect.append(el('option', { value: point.name }, t('paper.compose.q', { v1: point.name, v2: point.question_count })));
    }
    renderKpWeights();
  }

  function renderKpWeights() {
    kpListHost.replaceChildren();
    if (!aiForm.kpWeights.length) {
      kpListHost.append(el('p', { class: 'help' }, t('paper.compose.noKnowledgePointWeightsSpecifiedSelectNone')));
      return;
    }
    let total = 0;
    for (const entry of aiForm.kpWeights) {
      total += Number(entry.weight) || 0;
      const weightInput = el('input', {
        type: 'number', class: 'input pl-kp-weight-input', min: '1', max: '100',
        value: String(entry.weight), 'aria-label': t('paper.compose.weight', { v1: entry.name }),
      });
      weightInput.addEventListener('input', () => {
        entry.weight = Number(weightInput.value) || 0;
        renderKpWeights();
      });
      kpListHost.append(el('div', { class: 'pl-kp-weight-row' },
        el('span', { class: 'pl-kp-weight-name' }, entry.name),
        weightInput, el('span', { class: 'help' }, '%'),
        button(t('upload.remove'), () => {
          aiForm.kpWeights = aiForm.kpWeights.filter(item => item.name !== entry.name);
          renderKpWeights();
        }, 'ghost small', 'trash')));
    }
    kpListHost.append(el('p', { class: total > 100 ? 'help pl-kp-total is-over' : 'help pl-kp-total' },
      t('paper.compose.totalWeightOver100IsRejectedBy', { v1: total })));
  }

  function addKpWeight() {
    const name = kpNameSelect.value;
    if (!name) { toast(t('paper.compose.chooseTheKnowledgePointsToWeight'), true); return; }
    if (aiForm.kpWeights.some(item => item.name === name)) {
      toast(t('paper.compose.thatKnowledgePointIsAlreadyAdded'), true);
      return;
    }
    aiForm.kpWeights = [...aiForm.kpWeights, { name, weight: Number(kpWeightInput.value) || 50 }];
    renderKpWeights();
  }

  subjectSelect.addEventListener('change', () => {
    aiForm.subject = subjectSelect.value;
    loadKnowledgePoints();
  });
  gradeSelect.addEventListener('change', () => { aiForm.grade = gradeSelect.value; });
  modeSelect.addEventListener('change', () => { aiForm.mode = modeSelect.value; syncModeHelp(); });
  countInput.addEventListener('input', () => { aiForm.count = Number(countInput.value) || 1; });
  difficultySelect.addEventListener('change', () => { aiForm.difficultyTarget = difficultySelect.value; });
  toleranceSelect.addEventListener('change', () => { aiForm.tolerance = toleranceSelect.value; });
  sourceSelect.addEventListener('change', () => { aiForm.source = sourceSelect.value; });
  tagsInput.addEventListener('input', () => { aiForm.knowledgeTags = tagsInput.value; });

  startBtn.addEventListener('click', () => busy(startBtn, async () => {
    if (noProvider) throw new Error(t('mybank.configureAModelInModelSettingsFirst_2'));
    if (!subjectSelect.value) { subjectSelect.focus(); throw new Error(t('paper.compose.chooseACompositionSubject')); }
    const count = Number(countInput.value) || 0;
    if (count < 1 || count > MAX_COMPOSE_COUNT) throw new Error(t('paper.compose.theQuestionCountMustBeBetween1', { v1: MAX_COMPOSE_COUNT }));
    const typeCounts = Object.fromEntries(
      Object.entries(aiForm.typeCounts).filter(([, value]) => Number(value) > 0),
    );
    const typeSum = Object.values(typeCounts).reduce((sum, value) => sum + Number(value), 0);
    if (typeSum > 0 && typeSum !== count) {
      throw new Error(t('paper.compose.typeDistributionTotalsQuestionsButTheTotal', { v1: typeSum, v2: count }));
    }
    const kpTotal = aiForm.kpWeights.reduce((sum, entry) => sum + (Number(entry.weight) || 0), 0);
    if (kpTotal > 100) throw new Error(t('paper.compose.knowledgePointWeightsTotalOver100Please', { v1: kpTotal }));
    const payload = picker.payload(false);
    const spec = {
      mode: aiForm.mode,
      subject: subjectSelect.value,
      grade: gradeSelect.value,
      count,
      provider_id: payload.provider_id,
      model: payload.model,
      tolerance: Number(aiForm.tolerance) || 0.1,
      filters: {
        source: sourceSelect.value,
        knowledge_tags: tagsInput.value.split(/[,，、]/).map(item => item.trim()).filter(Boolean),
      },
    };
    if (Object.keys(typeCounts).length) spec.type_counts = typeCounts;
    if (aiForm.difficultyTarget !== '') spec.difficulty_target = Number(aiForm.difficultyTarget);
    if (aiForm.kpWeights.length) {
      spec.kp_weights = Object.fromEntries(aiForm.kpWeights.map(entry => [entry.name, Number(entry.weight) || 0]));
    }
    errorBox.replaceChildren();
    candidateBox.replaceChildren();
    jobHost.replaceChildren();
    jobSummary.replaceChildren();
    let response;
    try {
      response = await api('/paperlab/papers/compose', { method: 'POST', data: spec });
    } catch (error) {
      // 同步预检 422：候选总数/题型/知识点配额/难度范围不足，后端给出具体差额。
      errorBox.replaceChildren(
        notice(t('paper.compose.compositionPreCheckFailed', {
          v1: error.message || t('paper.compose.parametersNotSatisfied'),
        }), 'error'),
        el('p', { class: 'help' }, t('paper.compose.thisIsAHardBackendValidationIt')));
      throw error;
    }
    const candidates = response.candidates || {};
    candidateBox.replaceChildren(notice(
      t('paper.compose.candidatePoolQuestions', { v1: number(candidates.total) })
      + (candidates.type_availability
        ? t('paper.compose.availablePerType', { v1: Object.entries(candidates.type_availability)
          .map(([type, value]) => `${typeLabelOf(type)} ${value}`)
          .join(t('common.listSeparator')) })
        : '')
      + (candidates.difficulty_range
        ? t('paper.compose.candidateDifficultyRange', { v1: number(candidates.difficulty_range[0]), v2: number(candidates.difficulty_range[1]) })
        : '')
      + (candidates.kp_quotas && Object.keys(candidates.kp_quotas).length
        ? t('paper.compose.knowledgePointQuotas', {
          v1: Object.entries(candidates.kp_quotas)
            .map(([name, value]) => t('mybank.questions', { v1: name, v2: value }))
            .join(t('common.listSeparator')),
        })
        : ''),
      ''));
    const job = response.job || response;
    toast(t('paper.compose.aiCompositionTaskSubmittedProgressAppearsBelow'));
    mountJob(jobHost, job.id, null, {
      task: 'paper-compose',
      onSettled: settled => {
        if (!settled) return;
        if (settled.status === 'completed' && settled.result?.paper_id) {
          toast(t('paper.compose.aiCompositionCompleteQuestionsOpeningThePaper', { v1: settled.result.item_count || 0 }));
          onOpenPaper(settled.result.paper_id);
          return;
        }
        if (settled.status === 'cancelled') {
          jobSummary.replaceChildren(notice(t('paper.compose.aiCompositionCancelledAnyGeneratedPaperStays'), 'warning'));
          return;
        }
        jobSummary.replaceChildren(
          notice(t('paper.compose.aiCompositionDidNotFinish', {
            v1: settled.error || t('paper.compose.modelOutputFailedHardValidation'),
          }), 'error'),
          el('div', { class: 'row' },
            button(t('paper.compose.adjustParametersAndRetry'), () => startBtn.click(), 'soft small', 'repeat'),
            button(t('paper.compose.viewMyPapers'), () => go('paperlab', null, { tab: 'papers' }), 'ghost small', 'file')));
      },
    });
  }));

  syncModeHelp();
  loadKnowledgePoints().catch(() => {});
  const formCard = el('section', { class: 'card pl-compose-form' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('settings.task.paper-compose')),
        el('p', { class: 'help' }, t('paper.compose.theModelMayOnlyPickRefsFrom')))),
    el('div', { class: 'pl-ai-grid' },
      field(t('paper.compose.compositionMode'), modeSelect, modeHelp),
      field(t('common.subject'), subjectSelect),
      field(t('paper.import.gradeOptional'), gradeSelect),
      field(t('admin.questions'), countInput),
      field(t('paper.compose.targetDifficultyOptional'), difficultySelect),
      field(t('paper.compose.difficultyTolerance'), toleranceSelect),
      field(t('paper.compose.candidateSource'), sourceSelect),
      field(t('paper.compose.knowledgePointTagFilter'), tagsInput)),
    el('div', { class: 'pl-ai-subsection' },
      el('h3', {}, t('paper.compose.typeDistributionOptionalIfSetItMust')),
      typeCountRow),
    el('div', { class: 'pl-ai-subsection' },
      el('h3', {}, t('paper.compose.knowledgePointQuotasOptionalTotalWeight100')),
      el('div', { class: 'row pl-kp-add' }, kpNameSelect, kpWeightInput,
        el('span', { class: 'help' }, '%'), button(t('question.addKnowledgePoint'), addKpWeight, 'soft small', 'plus')),
      kpListHost),
    el('div', { class: 'pl-ai-panel' }, picker.node),
    errorBox, candidateBox,
    el('div', { class: 'row end' }, startBtn),
    jobHost, jobSummary);
  if (noProvider) {
    formCard.append(notice(t('paper.compose.aiCompositionNeedsAModelConnectionAdd'), 'warning'));
    formCard.append(el('div', { class: 'row' },
      button(t('mybank.goToModelSettings'), () => go('settings'), 'soft', 'sliders')));
    startBtn.disabled = true;
    startBtn.title = t('mybank.configureAModelInModelSettingsFirst');
  }
  host.append(formCard);
  mount.replaceChildren(host);
}

/* ---------------------------------------------------------------------------
 * 组卷工作台入口
 * ------------------------------------------------------------------------- */

export async function renderComposeWorkbench({ mount, meta, version, onOpenPaper, officialSourcesSupported = null }) {
  mount.replaceChildren(loading(t('paper.compose.preparingTheCompositionWorkbench')));
  if (!(state.providers || []).length) {
    await refreshProviders().catch(() => {});
  }
  if (!current(version)) return;
  const tabs = el('div', { class: 'tabs pl-compose-tabs', role: 'tablist', 'aria-label': t('paper.compose.compositionMethod') },
    el('button', {
      type: 'button', class: 'tab', role: 'tab',
      onClick: () => switchMode('manual'),
    }, t('paper.compose.manualComposition')),
    el('button', {
      type: 'button', class: 'tab', role: 'tab',
      onClick: () => switchMode('ai'),
    }, t('settings.task.paper-compose')));
  const body = el('div', { class: 'pl-compose-body' });
  const wrap = el('div', { class: 'pl-compose' }, tabs, body);
  mount.replaceChildren(wrap);

  function switchMode(mode) {
    composeMode = mode;
    [...tabs.children].forEach((tab, index) => {
      const active = (index === 0 && mode === 'manual') || (index === 1 && mode === 'ai');
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    try {
      // 平台精选来源页签由页面（paperlab.js）做能力探测后传入；null/未传时保守隐藏。
      if (mode === 'manual') renderManualCompose({ mount: body, meta, version, onOpenPaper, officialSourcesSupported });
      else renderAiCompose({ mount: body, meta, version, onOpenPaper });
    } catch (error) {
      body.replaceChildren(notice(error.message || t('paper.compose.failedToLoadTheCompositionWorkbenchPlease'), 'error'));
    }
  }
  switchMode(composeMode);
}

/* ---------------------------------------------------------------------------
 * 错题巩固卷
 * ------------------------------------------------------------------------- */

export async function renderRevisionStudio({ mount, meta, version, onOpenPaper }) {
  mount.replaceChildren(loading(t('paper.compose.loadingWrongAnswerSetOptions')));
  if (!(state.providers || []).length) {
    await refreshProviders().catch(() => {});
  }
  const noProvider = !(state.providers || []).length;
  let options = null;
  const subjectSelect = select([{ value: '', label: t('insight.subjectAll') }], revisionState.subject);
  const modeBox = el('div', { class: 'pl-revision-modes' });
  const kpBox = el('div', { class: 'pl-revision-kps' });
  const countInput = el('input', {
    type: 'number', class: 'input', min: '1', max: '20',
    value: String(revisionState.count), 'aria-label': t('paper.compose.questionsInTheSet'),
  });
  const modelHost = el('div', { class: 'pl-revision-model' });
  const errorBox = el('div', { class: 'pl-revision-error' });
  const jobHost = el('div', { class: 'pl-revision-job' });
  const jobSummary = el('div', { class: 'pl-revision-summary' });
  const resultBox = el('div', { class: 'pl-revision-result' });
  const picker = createModelPicker('paper-revision', { showModeToggle: false });
  const replayBtn = button(t('paper.compose.generateWrongAnswerReplayPaper'), null, '', 'repeat');
  const consolidateBtn = button(t('paper.compose.generateAiConsolidationPaper'), null, '', 'sparkle');

  function selectedKpIds() {
    return [...revisionState.kpIds];
  }

  function renderModes() {
    modeBox.replaceChildren();
    const modes = options?.modes || [
      { value: 'replay', label: t('paper.compose.wrongAnswerReplayPaper'), description: t('paper.compose.buildAReplayPaperDirectlyFromWrong'), ai_required: false },
      { value: 'consolidate', label: t('home.paperKind.consolidate'), description: t('paper.compose.aiPicksUnseenExamQuestionsFromThe'), ai_required: true },
    ];
    for (const mode of modes) {
      const active = revisionState.mode === mode.value;
      const card = el('button', {
        type: 'button',
        class: `pl-revision-mode${active ? ' active' : ''}`,
        'aria-pressed': active ? 'true' : 'false',
        onClick: () => {
          revisionState.mode = mode.value;
          renderModes();
          syncButtons();
        },
      },
      el('strong', {}, mode.label),
      el('span', { class: 'help' }, mode.description || ''),
      mode.ai_required ? badge(t('paper.compose.needsAModel'), 'amber') : badge(t('paper.compose.noModelNeeded'), 'teal'));
      modeBox.append(card);
    }
  }

  function renderKps() {
    kpBox.replaceChildren();
    const kpOptions = options?.kp_options || [];
    if (!kpOptions.length) {
      kpBox.append(el('p', { class: 'help' }, t('paper.compose.noWrongAnswerKnowledgePointsAreAvailable')));
      return;
    }
    kpBox.append(el('p', { class: 'help' }, t('paper.compose.filterByKnowledgePointAllWrongAnswers')));
    const list = el('div', { class: 'pl-revision-kp-list' });
    for (const point of kpOptions) {
      const checked = revisionState.kpIds.has(point.kp_id);
      const checkbox = el('input', { type: 'checkbox', checked });
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) revisionState.kpIds.add(point.kp_id);
        else revisionState.kpIds.delete(point.kp_id);
        syncButtons();
      });
      list.append(el('label', { class: 'pl-revision-kp' },
        checkbox, el('span', {}, point.name), badge(t('paper.compose.wrong', { v1: point.wrong_count }), 'amber')));
    }
    kpBox.append(list);
  }

  function syncButtons() {
    const list = Array.isArray(options?.kp_options) ? options.kp_options : [];
    const filteredByKp = revisionState.kpIds.size
      ? list.filter(point => revisionState.kpIds.has(point.kp_id)).reduce((sum, point) => sum + point.wrong_count, 0)
      : (options?.wrong_count || 0);
    replayBtn.disabled = !(options?.wrong_count) || revisionState.mode !== 'replay';
    consolidateBtn.disabled = noProvider || revisionState.mode !== 'consolidate';
    modelHost.classList.toggle('hide', revisionState.mode !== 'consolidate');
    errorBox.replaceChildren();
    if (options && !options.wrong_count) {
      resultBox.replaceChildren(empty(
        t('wrongbook.emptyTitle'),
        t('paper.compose.doPracticeOrPhotoToExplainFirst'),
        button(t('paper.compose.viewWrongAnswerReview'), () => go('wrongbook'), 'soft', 'repeat'),
        'repeat'));
      return;
    }
    const info = el('p', { class: 'help' },
      options
        ? t('paper.compose.wrongQuestionsUnderTheCurrentFilters1', {
          v1: options.wrong_count,
          v2: revisionState.kpIds.size
            ? t('paper.compose.knowledgePointMatchesApprox', { v1: filteredByKp })
            : '',
        })
        : '');
    resultBox.replaceChildren(info);
  }

  async function loadOptions() {
    options = null;
    kpBox.replaceChildren(loading(t('paper.compose.loadingWrongAnswerOptions')));
    try {
      const params = revisionState.subject ? `?subject=${encodeURIComponent(revisionState.subject)}` : '';
      options = await api(`/paperlab/revision/options${params}`);
      if (!current(version)) return;
      subjectSelect.replaceChildren(el('option', { value: '' }, t('insight.subjectAll')));
      for (const subject of options.subjects || []) {
        subjectSelect.append(el('option', { value: subject }, subject));
      }
      if (revisionState.subject && !(options.subjects || []).includes(revisionState.subject)) {
        revisionState.subject = '';
        subjectSelect.value = '';
      } else {
        subjectSelect.value = revisionState.subject;
      }
      const limits = options.limits || {};
      countInput.min = String(limits.count_min || 1);
      countInput.max = String(limits.count_max || 20);
      renderModes();
      renderKps();
      syncButtons();
    } catch (error) {
      if (!current(version)) return;
      kpBox.replaceChildren(notice(error.message || t('paper.compose.failedToLoadWrongAnswerOptionsPlease'), 'error'));
    }
  }

  subjectSelect.addEventListener('change', () => {
    revisionState.subject = subjectSelect.value;
    revisionState.kpIds.clear();
    loadOptions();
  });
  countInput.addEventListener('input', () => {
    revisionState.count = Number(countInput.value) || 5;
  });

  replayBtn.addEventListener('click', () => busy(replayBtn, async () => {
    if (!options?.wrong_count) throw new Error(t('paper.compose.theWrongAnswerSetHasNoQuestions'));
    const payload = {
      mode: 'replay',
      subject: revisionState.subject || undefined,
      kp_ids: selectedKpIds(),
      count: Number(countInput.value) || 5,
    };
    try {
      const result = await api('/paperlab/revision', { method: 'POST', data: payload });
      toast(result.message || t('paper.compose.replayPaperGeneratedOpeningThePaper'));
      if (result.paper?.id) onOpenPaper(result.paper.id);
      else loadOptions();
    } catch (error) {
      errorBox.replaceChildren(notice(error.message || t('paper.compose.failedToGenerateTheReplayPaperPlease'), 'error'));
      throw error;
    }
  }));

  consolidateBtn.addEventListener('click', () => busy(consolidateBtn, async () => {
    if (noProvider) throw new Error(t('paper.compose.aiConsolidationNeedsAModelConnectionConfigure'));
    if (!options?.wrong_count) throw new Error(t('paper.compose.theWrongAnswerSetHasNoQuestions_2'));
    errorBox.replaceChildren();
    jobHost.replaceChildren();
    jobSummary.replaceChildren();
    const payload = picker.payload(false);
    const request = {
      mode: 'consolidate',
      subject: revisionState.subject || undefined,
      kp_ids: selectedKpIds(),
      count: Number(countInput.value) || 5,
      provider_id: payload.provider_id,
      model: payload.model,
    };
    let response;
    try {
      response = await api('/paperlab/revision', { method: 'POST', data: request });
    } catch (error) {
      // 同步预检 422（候选不足等）：后端明确拒绝，不生成缩水卷。
      errorBox.replaceChildren(
        notice(t('paper.compose.consolidationPreCheckFailed', {
          v1: error.message || t('paper.compose.notEnoughCandidateQuestions'),
        }), 'error'),
        el('p', { class: 'help' }, t('paper.compose.theBackendNeverGeneratesAReducedPaper')));
      throw error;
    }
    const job = response.job || response;
    toast(t('paper.compose.aiConsolidationTaskSubmittedProgressAppearsBelow'));
    const candidateCount = response.candidates?.candidate_count ?? response.candidates?.total;
    jobSummary.replaceChildren(notice(
      candidateCount === undefined || candidateCount === null
        ? t('paper.compose.consolidationTaskSubmittedQuestionsInTheSource', { v1: number(response.count) })
        : t('paper.compose.candidatePoolQuestionsInTheSourceSet', { v1: number(candidateCount), v2: number(response.count) }), ''));
    mountJob(jobHost, job.id, null, {
      task: 'paper-revision',
      onSettled: settled => {
        if (!settled) return;
        if (settled.status === 'completed' && settled.result?.paper_id) {
          toast(t('paper.compose.aiConsolidationGeneratedQuestionsOpeningThePaper', { v1: settled.result.item_count || 0 }));
          onOpenPaper(settled.result.paper_id);
          return;
        }
        if (settled.status === 'cancelled') {
          jobSummary.replaceChildren(notice(t('paper.compose.consolidationTaskCancelled'), 'warning'));
          return;
        }
        jobSummary.replaceChildren(
          notice(t('paper.compose.consolidationDidNotFinish', {
            v1: settled.error || t('paper.compose.modelOutputFailedHardValidation'),
          }), 'error'),
          el('div', { class: 'row' },
            button(t('paper.compose.viewMyPapers'), () => go('paperlab', null, { tab: 'papers' }), 'ghost small', 'file')));
      },
    });
  }));

  // 初始骨架
  const card = el('section', { class: 'card pl-revision' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('home.ai.revision.title')),
        el('p', { class: 'help' }, t('paper.compose.theReplayPaperReusesTheOriginalWrong')))),
    el('div', { class: 'pl-revision-grid' },
      field(t('paper.compose.subjectFilter'), subjectSelect),
      field(t('paper.compose.questionCount'), countInput)),
    el('div', { class: 'pl-ai-subsection' }, el('h3', {}, t('paper.compose.sourceSetMode')), modeBox),
    el('div', { class: 'pl-ai-subsection' }, el('h3', {}, t('paper.compose.knowledgePointFilter')), kpBox),
    modelHost,
    errorBox, resultBox, jobSummary,
    el('div', { class: 'row end' }, replayBtn, consolidateBtn),
    jobHost);
  modelHost.append(el('div', { class: 'pl-ai-panel' }, picker.node));
  if (noProvider) {
    modelHost.append(notice(t('paper.compose.aiConsolidationNeedsAModelConnectionConfigure_2'), 'warning'),
      el('div', { class: 'row' }, button(t('mybank.goToModelSettings'), () => go('settings'), 'soft', 'sliders')));
  }
  mount.replaceChildren(card);
  await loadOptions();
}
