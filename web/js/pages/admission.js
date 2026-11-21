/**
 * 志愿查询：把高考分数换算成位次，再按「冲 / 稳 / 保」查看可报院校、专业录取线与招生计划。
 *
 * 数据来自 GaokaoCompass（Hugging Face，MIT 许可），由 `scripts/import_admission.py`
 * 导入为只读的 data/admission.sqlite3；页面只读取后端 /api/admission/*，不内置数据。
 * 数据为非官方整理，页面明确提示「仅供参考，以官方公布为准」。
 */
import { api } from '../core/api.js';
import {
  el, button, badge, empty, heading, loading, notice, select, input, field,
  number, query, toast,
} from '../core/dom.js';
import { current } from '../core/router.js';
import { t } from '../core/i18n.js';

const TABS = [
  { key: 'rank', labelKey: 'admission.tabRank', icon: 'chart' },
  { key: 'schools', labelKey: 'admission.tabSchools', icon: 'search' },
  { key: 'school', labelKey: 'admission.tabSchool', icon: 'university' },
  { key: 'plans', labelKey: 'admission.tabPlans', icon: 'file' },
];

// 冲 / 稳 / 保 是后端下发的中文档位值：比较保持原值，展示时按语言映射。
const LEVEL_KEYS = { '冲': 'admission.levelReach', '稳': 'admission.levelMatch', '保': 'admission.levelSafety' };

function levelText(level) {
  const key = LEVEL_KEYS[level];
  return key ? t(key) : String(level);
}

let metaPromise = null;

/** 数据集元信息在会话内复用；未导入时返回 ready:false 供页面给出操作指引。 */
function loadMeta(force = false) {
  if (!metaPromise || force) {
    metaPromise = api('/admission/meta').catch(error => ({ ready: false, error: error.message }));
  }
  return metaPromise;
}

export async function renderAdmission(container, version) {
  container.replaceChildren(loading(t('admission.loadingAdmissionData')));
  const meta = await loadMeta();
  if (!current(version)) return;

  if (!meta.ready) {
    container.replaceChildren(
      heading('GAOKAO COMPASS', t('nav.admission'), t('admission.turnYourScoreIntoARankWith')),
      notice(t('admission.admissionDataHasNotBeenImportedYet'), 'warning'),
      empty(
        t('admission.noAdmissionDataYet'),
        meta.error || t('admission.runPythonScriptsImportAdmissionPyIn'),
        null,
        'compass'
      )
    );
    return;
  }

  const provinces = Array.isArray(meta.provinces) ? meta.provinces : [];
  const years = Array.isArray(meta.years) && meta.years.length ? meta.years : [2024];
  const view = {
    province: provinces[0]?.slug || '',
    provinceName: provinces[0]?.name || '—',
    year: years[years.length - 1],
    category: '',
    filters: null,
    tab: 'rank',
    pendingSchool: null,
    pendingMajor: null,
  };

  const provinceSelect = select(provinces.map(item => ({ value: item.slug, label: item.name })), view.province);
  const yearSelect = select(years.slice().reverse().map(item => ({ value: item, label: t('common.yearLabel', { year: item }) })), view.year);
  const categorySelect = select([{ value: '', label: t('admission.autoCategoryWithTheMostData') }], '');
  const statsGrid = el('div', { class: 'stats-grid' });
  const completenessBar = el('div', { class: 'pg-admit-completeness' });
  const tabBar = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': t('admission.admissionSearchSections') });
  const panel = el('div', { class: 'stack' });
  let panelSeq = 0;
  let filterSeq = 0;

  /* ---------------- 顶部筛选与看板 ---------------- */

  function rebuildCategoryOptions() {
    const options = [{ value: '', label: t('admission.autoCategoryWithTheMostData') }];
    for (const item of (view.filters?.categories || [])) options.push({ value: item, label: item });
    categorySelect.replaceChildren(...options.map(option => el('option', { value: option.value }, option.label)));
    if (view.filters && view.filters.categories.includes(view.category)) {
      categorySelect.value = view.category;
    } else {
      view.category = '';
      categorySelect.value = '';
    }
  }

  async function loadFilters(seq) {
    let filters;
    try {
      filters = await api(`/admission/filters?${query({ province: view.province, year: view.year })}`);
    } catch (error) {
      filters = { categories: [], batches: [], rank_supported: false, counts: {}, error: error.message };
    }
    if (seq !== filterSeq) return false;
    view.filters = filters;
    rebuildCategoryOptions();
    let overview = null;
    try {
      const data = await api(`/admission/overview?${query({ province: view.province, year: view.year })}`);
      overview = data.overview || null;
    } catch {
      overview = null; // 2017–2021 等年份数据源未提供 meta.json：退回明细行数统计。
    }
    if (seq !== filterSeq) return false;
    statsGrid.replaceChildren(...buildStatCards(overview, view.filters));
    completenessBar.replaceChildren(...buildCompleteness(view.filters));
    return true;
  }

  /** 把数据源的完整度如实摊开：缺位次/批次线/科类/计划人数时在页面上直接说明。 */
  function buildCompleteness(filters) {
    if (!filters) return [];
    const items = [
      [t('admission.rankData'), Boolean(filters.rank_supported), t('admission.theProvinceYearCutoffLinesLackRanks')],
      [t('admission.controlScoreLine'), Boolean(filters.control_score_supported), t('admission.theProvinceYearScoreRankTableLacks')],
      [t('admission.category'), Boolean(filters.category_available), t('admission.theProvinceYearAdmissionTableLacksA')],
      [t('admission.plannedEnrollment'), Boolean(filters.plan_supported), t('admission.theProvinceYearEnrollmentPlanLacksPlanned')],
    ];
    return [
      el('span', { class: 'pg-admit-completeness-label' }, t('admission.dataCompletenessForThisProvinceYear')),
      ...items.map(([label, ok, hint]) => el('span', {
        class: 'pg-tag',
        'data-tone': ok ? 'ok' : 'warn',
        title: ok ? t('admission.itemAvailable', { v1: label }) : hint,
      }, t('admission.itemStatus', { v1: label, v2: ok ? t('admission.available') : t('admission.missing') }))),
    ];
  }

  function buildStatCards(overview, filters) {
    const exam = overview?.exam_overview || {};
    const university = overview?.university_stats || {};
    const enrollment = overview?.enrollment_stats || {};
    const counts = filters?.counts || {};
    if (overview) {
      return [
        statCard(t('admission.gaokaoCandidates'), number(exam.total_candidates || 0), t('admission.people'), t('admission.asPublishedByTheProvincialExamAuthority')),
        statCard(t('admission.admittingUniversities'), number(university.total || enrollment.university_count || 0), t('admission.schools'), t('admission.universitiesAppearingInThisProvinceYear')),
        statCard(t('admission.enrollmentPlan'), enrollment.total_plan ? number(enrollment.total_plan) : t('admission.notProvided'), '', t('admission.plannedNumbersMissingForSomeYears')),
        statCard('985 / 211', `${number(university.is_985 || 0)} / ${number(university.is_211 || 0)}`, t('admission.schools'), t('admission.universityAttributeTags')),
      ];
    }
    return [
      statCard(t('admission.scoreRankTable'), number(counts['score-range'] || 0), t('admission.records'), t('admission.scoreRankRecords')),
      statCard(t('admission.universityCutoffLines'), number(counts['school-admission'] || 0), t('admission.records'), t('admission.minimumScoreAndRankByUniversity')),
      statCard(t('admission.majorAdmissionLines'), number(counts['major-admission'] || 0), t('admission.records'), t('admission.admissionScoresByMajor')),
      statCard(t('admission.enrollmentPlan'), number(counts['enrollment-plan'] || 0), t('admission.records'), t('admission.universityMajorPlanDetails')),
    ];
  }

  function statCard(label, value, unit, caption) {
    return el('div', { class: 'card stat-card' },
      el('div', { class: 'stat-top' }, label),
      el('div', { class: 'stat-value' }, value, unit ? el('span', { class: 'stat-unit' }, unit) : null),
      el('div', { class: 'stat-caption' }, caption));
  }

  /* ---------------- 标签页 ---------------- */

  function renderTabs() {
    tabBar.replaceChildren(...TABS.map(tab => el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      'aria-selected': String(tab.key === view.tab),
      onClick: () => {
        if (view.tab === tab.key) return;
        view.tab = tab.key;
        renderTabs();
        renderPanel();
      },
    }, t(tab.labelKey))));
  }

  function renderPanel() {
    panelSeq += 1;
    const node = buildPanel();
    if (node) panel.replaceChildren(node);
  }

  function buildPanel() {
    switch (view.tab) {
      case 'rank': return rankTab();
      case 'schools': return schoolsTab();
      case 'school': return schoolTab();
      case 'plans': return plansTab();
      default: return null;
    }
  }

  function guardStale(seq) {
    return seq === panelSeq && current(version);
  }

  /* ---------------- 一分一段 ---------------- */

  function rankTab() {
    const seq = panelSeq;
    const scoreInput = input(t('admission.eG600'), { type: 'number', min: '0', max: '1000', inputmode: 'numeric' });
    const result = el('div');
    const form = el('div', { class: 'pg-admit-form pg-admit-form--narrow' },
      field(t('admission.gaokaoScore'), scoreInput),
      el('div', { class: 'pg-admit-actions' }, button(t('admission.lookUpRank'), run, '', 'chart')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, t('admission.scoreRankTableConvertScoreToRank')),
          el('p', { class: 'help' }, t('admission.ranksAreCumulativeCountsTheSameScore'))),
        view.filters?.rank_supported ? badge(t('admission.rankSupported'), 'teal') : badge(t('admission.rankDataMissing'), 'amber')),
      form,
      result);

    async function run() {
      const score = Number(scoreInput.value);
      if (!Number.isFinite(score) || score < 0 || score > 1000) {
        toast(t('admission.pleaseEnterAValidScoreBetween0'), true);
        return;
      }
      result.replaceChildren(loading(t('admission.searching')));
      try {
        const data = await api(`/admission/score-rank?${query({
          province: view.province, year: view.year, category: view.category, score,
        })}`);
        if (!guardStale(seq) || !result.isConnected) return;
        result.replaceChildren(rankResult(data));
      } catch (error) {
        if (result.isConnected) result.replaceChildren(notice(error.message, 'error'));
      }
    }
    scoreInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); run(); }
    });
    return card;
  }

  function rankResult(data) {
    const rank = data.cumulative_count;
    const head = el('div', { class: 'pg-admit-summary' },
      el('div', {},
        el('p', { class: 'pg-admit-label' },
          t('admission.rankSummary', { year: data.year, province: data.province_name, category: data.category || t('admission.uncategorized'), score: data.score })),
        el('div', { class: 'pg-admit-big' }, rank ? number(rank) : t('admission.nA'),
          el('span', { class: 'pg-admit-big-unit' }, t('admission.rank'))),
        el('p', { class: 'help' }, data.rank_range ? t('admission.rankRange', { v1: data.rank_range }) : '')),
      el('div', { class: 'pg-admit-facts' },
        factRow(t('admission.batch'), data.batch || data.control_batch || t('admission.notLabeled')),
        factRow(t('admission.controlScoreLine'), data.control_score
          ? t('admission.scorePoints', { v1: data.control_score }) + (
            data.control_source === 'official' ? t('admission.officialSupplement_2')
              : data.control_source === 'official-inferred' ? t('admission.officialSupplementBatchMatchedByScore') : '')
          : t('admission.notProvidedByTheDataSource')),
        factRow(t('admission.candidatesInThisSegment'), data.segment_count ? number(data.segment_count) : '—'),
        factRow(t('admission.totalCandidatesInThisCategory'), data.total ? number(data.total) : '—'),
        factRow(t('admission.exactMatch'), data.exact ? t('common.yes') : t('admission.noClosestIsPoints', { v1: data.nearest_score }))));
    const chart = lineChart(
      (data.distribution || []).map(point => ({ x: point.score, y: point.cumulative_count })),
      { xLabel: t('admission.score'), yLabel: t('admission.cumulativeRank'), height: 220 }
    );
    const matches = data.matches?.length > 1
      ? el('div', { class: 'pg-admit-table' },
        el('h3', {}, t('admission.recordsForTheSameScoreInDifferent')),
        dataTable([t('admission.batch'), t('admission.controlScoreLine'), t('admission.candidatesInThisSegment'), t('admission.cumulativeRank'), t('admission.rankRange_2')], data.matches.map(row => [
          row.batch || '—', row.control_score ?? '—', number(row.segment_count || 0),
          number(row.cumulative_count || 0), row.rank_range || '—',
        ])))
      : null;
    const official = data.official_batches?.length
      ? el('div', { class: 'pg-admit-table' },
        el('h3', {}, t('admission.officiallySupplementedControlScoresSince2020')),
        dataTable([t('admission.batch'), t('admission.category'), t('admission.scoreLine')], data.official_batches.map(row => [
          row.batch || '—', row.category || t('admission.allCategories'), t('admission.scorePoints', { v1: row.score }),
        ])))
      : null;
    return el('div', { class: 'stack' },
      el('section', { class: 'card' }, head,
        chart ? el('div', { class: 'pg-admit-chart-wrap' }, chart) : null,
        matches,
        official));
  }

  function factRow(label, value) {
    return el('div', { class: 'pg-admit-fact' }, el('span', {}, label), el('strong', {}, value));
  }

  /* ---------------- 找学校 ---------------- */

  function schoolsTab() {
    const seq = panelSeq;
    const modeSelect = select([
      { value: 'score', label: t('admission.matchByScore') },
      { value: 'rank', label: t('admission.matchByRank') },
    ], 'score');
    const valueInput = input(t('admission.eG600'), { type: 'number', inputmode: 'numeric' });
    const bandInput = input('15', { type: 'number', min: '1', max: '80' });
    const keywordInput = input(t('admission.eGZhejiangUniversityOptional'), { type: 'search' });
    const result = el('div');
    const bandField = field(t('admission.scoreRange'), bandInput, t('admission.scoreRangeAroundYourScoreDefault15'));
    const modeHint = el('p', { class: 'help' }, t('admission.afterEnteringAScoreUniversitiesAreGrouped'));
    modeSelect.addEventListener('change', () => {
      const rankMode = modeSelect.value === 'rank';
      bandField.classList.toggle('hide', rankMode);
      modeHint.textContent = rankMode
        ? t('admission.rankModeDividesByTheRatioOf')
        : t('admission.afterEnteringAScoreUniversitiesAreGrouped');
    });
    const form = el('div', { class: 'pg-admit-form pg-admit-form--wide' },
      field(t('admission.matchingMode'), modeSelect),
      field(t('admission.scoreRank'), valueInput),
      bandField,
      field(t('admission.universityKeyword'), keywordInput),
      el('div', { class: 'pg-admit-actions' }, button(t('admission.startMatching'), () => run(1), '', 'search')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, t('admission.findSchoolsReachMatchSafety')), modeHint),
        view.filters?.rank_supported ? badge(t('admission.rankAvailable'), 'teal') : badge(t('admission.scoreOnly'), 'amber')),
      form,
      result);

    async function run(page) {
      const value = Number(valueInput.value);
      if (!Number.isFinite(value) || value <= 0) {
        toast(t('admission.pleaseEnterAValidScoreOrRank'), true);
        return;
      }
      const params = {
        province: view.province,
        year: view.year,
        category: view.category,
        band: bandInput.value || 15,
        keyword: keywordInput.value.trim(),
        page: page || 1,
        page_size: 20,
      };
      if (modeSelect.value === 'rank') params.rank = Math.round(value);
      else params.score = Math.round(value);
      result.replaceChildren(loading(t('admission.matchingUniversities')));
      try {
        const data = await api(`/admission/schools?${query(params)}`);
        if (!guardStale(seq) || !result.isConnected) return;
        result.replaceChildren(schoolsResult(data, params, nextPage => run(nextPage)));
      } catch (error) {
        if (result.isConnected) result.replaceChildren(notice(error.message, 'error'));
      }
    }
    valueInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); run(1); }
    });
    return card;
  }

  function schoolsResult(data, params, onPage) {
    if (!data.rank_supported && data.mode === 'rank') {
      return notice(data.note || t('admission.theCutoffLinesForThisProvinceYear'), 'warning');
    }
    if (!data.items?.length) {
      return empty(t('admission.noUniversitiesMatched'), t('admission.tryADifferentScoreRangeOrYear'), null, 'search');
    }
    const note = data.category_filtered ? null
      : notice(t('admission.theAdmissionDataForThisProvinceYear'), 'warning');
    return el('div', { class: 'stack' },
      note,
      el('div', { class: 'pg-list-toolbar' },
        el('span', { class: 'pg-filter-summary' }, t('admission.universitiesPage', { v1: number(data.total), v2: data.page, v3: Math.max(1, Math.ceil(data.total / data.page_size)) })),
        el('span', { class: 'help' }, data.mode === 'score' ? t('admission.scoreRangePoints', { v1: params.band }) : t('admission.matchByRankRatio'))),
      el('div', { class: 'pg-admit-school-list' },
        ...data.items.map(item => schoolCard(item))),
      pager(data.total, data.page, data.page_size, onPage));
  }

  function schoolCard(item) {
    const badges = [];
    if (item.is_985) badges.push(badge('985', 'teal'));
    if (item.is_211) badges.push(badge('211', 'blue'));
    if (item.school_nature) badges.push(badge(item.school_nature));
    if (item.school_province) badges.push(badge(item.school_province));
    const levelTone = item.level === '冲' ? 'amber' : item.level === '稳' ? 'teal' : 'blue';
    return el('button', {
      type: 'button',
      class: 'pg-admit-school',
      onClick: () => {
        view.pendingSchool = item.university_name;
        view.tab = 'school';
        renderTabs();
        renderPanel();
      },
    },
    el('div', { class: 'pg-admit-school-main' },
      el('div', { class: 'pg-admit-school-title' }, el('strong', {}, item.university_name), ...badges),
      el('div', { class: 'pg-admit-school-meta' },
        el('span', {}, item.min_score != null ? t('admission.minScore', { v1: item.min_score }) : t('admission.minScoreNA')),
        el('span', {}, item.min_rank != null ? t('admission.rank_2', { v1: number(item.min_rank) }) : t('admission.rankNA')),
        item.delta != null ? el('span', {}, t('admission.gap', { v1: item.delta > 0 ? '+' : '', v2: item.delta })) : null,
        item.ratio != null ? el('span', {}, t('admission.rankRatio', { v1: item.ratio })) : null)),
    item.level ? el('span', { class: `badge ${levelTone} pg-admit-level` }, levelText(item.level)) : null);
  }

  /* ---------------- 查学校 / 专业 ---------------- */

  function schoolTab() {
    const seq = panelSeq;
    const searchInput = input(t('admission.enterAUniversityNameEGZhejiang'), { type: 'search', autocomplete: 'off' });
    const suggestBox = el('div', { class: 'pg-admit-suggest-list', hidden: true });
    const result = el('div');
    let suggestSeq = 0;
    let hideTimer = null;

    searchInput.addEventListener('input', () => {
      const term = searchInput.value.trim();
      if (!term) { suggestBox.hidden = true; return; }
      const mine = ++suggestSeq;
      setTimeout(async () => {
        if (mine !== suggestSeq) return;
        try {
          const data = await api(`/admission/suggest?${query({ q: term })}`);
          if (mine !== suggestSeq || !suggestBox.isConnected) return;
          const items = [
            ...(data.universities || []).map(row => ({ name: row.name, meta: 'university' })),
            ...(data.majors || []).map(row => ({ name: row.name, meta: 'major' })),
          ];
          if (!items.length) { suggestBox.hidden = true; return; }
          suggestBox.replaceChildren(...items.map(item => el('button', {
            type: 'button',
            class: 'pg-admit-suggest-item',
            onClick: () => {
              suggestBox.hidden = true;
              if (item.meta === 'university') {
                searchInput.value = item.name;
                load(item.name);
              } else {
                view.pendingMajor = item.name;
                view.tab = 'plans';
                renderTabs();
                renderPanel();
              }
            },
          }, el('span', {}, item.name), el('small', {}, item.meta))));
          suggestBox.hidden = false;
        } catch {
          suggestBox.hidden = true;
        }
      }, 220);
    });
    searchInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        suggestBox.hidden = true;
        if (searchInput.value.trim()) load(searchInput.value.trim());
      } else if (event.key === 'Escape') {
        suggestBox.hidden = true;
      }
    });
    searchInput.addEventListener('blur', () => {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => { suggestBox.hidden = true; }, 160);
    });
    searchInput.addEventListener('focus', () => { clearTimeout(hideTimer); });

    async function load(name) {
      result.replaceChildren(loading(t('admission.loadingUniversityData')));
      try {
        const data = await api(`/admission/school?${query({
          province: view.province, year: view.year, category: view.category, name,
        })}`);
        if (!guardStale(seq) || !result.isConnected) return;
        result.replaceChildren(schoolDetail(data));
      } catch (error) {
        if (result.isConnected) result.replaceChildren(notice(error.message, 'error'));
      }
    }

    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, t('admission.lookUpSchoolMajor')),
          el('p', { class: 'help' }, t('admission.universityCutoffLinesMajorAdmissionLinesEnrollment')))),
      el('div', { class: 'pg-admit-search' },
        el('div', { class: 'search-wrap' }, searchInput, suggestBox),
        button(t('admission.search'), () => { if (searchInput.value.trim()) load(searchInput.value.trim()); }, '', 'search')),
      result);

    if (view.pendingSchool) {
      const name = view.pendingSchool;
      view.pendingSchool = null;
      searchInput.value = name;
      load(name);
    }
    return card;
  }

  function schoolDetail(data) {
    const university = data.university || {};
    const badges = [];
    if (university.is_985) badges.push(badge('985', 'teal'));
    if (university.is_211) badges.push(badge('211', 'blue'));
    if (university.school_nature) badges.push(badge(university.school_nature));
    if (university.school_province) badges.push(badge(university.school_province));

    const head = el('div', { class: 'pg-admit-detail-head' },
      el('div', {},
        el('div', { class: 'pg-admit-school-title' }, el('h3', {}, data.name), ...badges),
        el('p', { class: 'help' },
          t('admission.schoolSummary', { year: data.year, province: data.province_name, category: data.category || t('admission.uncategorized') })
          + (data.plan_summary?.rows
            ? t('admission.enrollmentPlanRecordsPlannedPlacesKnownIn', { v1: number(data.plan_summary.rows), v2: number(data.plan_summary.people) })
            : ''))),
      el('div', { class: 'pg-admit-facts' },
        factRow(t('admission.cutoffRecords'), t('common.countItems', { count: data.cutoffs?.length || 0 })),
        factRow(t('admission.majorAdmissionRecords'), t('common.countItems', { count: data.majors?.length || 0 })),
        factRow(t('admission.enrollmentPlanRecords'), t('common.countItems', { count: data.plans?.length || 0 }))));

    const trendChart = lineChart(
      (data.trend || []).filter(row => row.min_score != null).map(row => ({ x: row.year, y: row.min_score })),
      { xLabel: t('practice.year'), yLabel: t('admission.minimumScore'), height: 190 }
    );

    return el('div', { class: 'stack' },
      el('section', { class: 'card' }, head,
        (data.trend || []).filter(row => row.min_score != null).length > 1
          ? el('div', { class: 'pg-admit-chart-wrap' }, trendChart) : null),
      data.cutoffs?.length ? sectionCard(t('admission.universityCutoffLines'), dataTable(
        [t('admission.batch'), t('admission.category'), t('admission.minimumScore'), t('admission.minimumRank'), t('admission.admittedCount'), t('admission.subjectRequirements')],
        data.cutoffs.slice(0, 60).map(row => [
          row.batch || '—', row.category || '—', row.min_score ?? '—',
          row.min_rank != null ? number(row.min_rank) : '—',
          row.admit_count != null ? number(row.admit_count) : '—', row.subject_req || '—',
        ])
      )) : null,
      data.majors?.length ? sectionCard(t('admission.majorAdmissionLines'), dataTable(
        [t('admission.major'), t('admission.batch'), t('admission.minimumScore'), t('admission.minimumRank'), t('admission.averageScore'), t('admission.admittedCount')],
        data.majors.slice(0, 80).map(row => [
          row.major_name || '—', row.batch || '—', row.min_score ?? '—',
          row.min_rank != null ? number(row.min_rank) : '—',
          row.avg_score != null ? row.avg_score : '—',
          row.admit_count != null ? number(row.admit_count) : '—',
        ])
      ), data.majors.length > 80 ? t('admission.showingTheFirst80OfRecords', { v1: data.majors.length }) : '') : null,
      data.plans?.length ? sectionCard(t('admission.enrollmentPlan'), dataTable(
        [t('admission.major'), t('admission.batch'), t('admission.plannedCount'), t('admission.duration'), t('admission.tuition'), t('admission.subjectRequirements')],
        data.plans.slice(0, 80).map(row => [
          row.major_name || '—', row.batch || '—',
          row.plan_count != null ? number(row.plan_count) : t('admission.notProvided'),
          row.duration || '—', row.tuition != null ? `¥${number(Math.round(row.tuition))}` : '—',
          row.subject_req || '—',
        ])
      ), data.plans.length > 80 ? t('admission.showingTheFirst80OfRecords', { v1: data.plans.length }) : '') : null,
      notice(t('admission.meansTheFieldIsMissingInThe')));
  }

  function sectionCard(title, content, hint = null) {
    return el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, title), hint ? el('p', { class: 'help' }, hint) : null)),
      content);
  }

  /* ---------------- 招生计划 ---------------- */

  function plansTab() {
    const seq = panelSeq;
    const keywordInput = input(t('admission.majorKeywordEGComputerScience'), { type: 'search' });
    const universityInput = input(t('admission.universityNameOptional'), { type: 'search' });
    const result = el('div');
    const form = el('div', { class: 'pg-admit-form pg-admit-form--three' },
      field(t('admission.majorKeyword'), keywordInput),
      field(t('admission.universityName'), universityInput),
      el('div', { class: 'pg-admit-actions' }, button(t('admission.searchPlans'), () => run(1), '', 'search')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, t('admission.enrollmentPlanSearch')),
          el('p', { class: 'help' }, t('admission.plannedNumbersAreMissingForSomeProvinces')))),
      form,
      result);

    async function run(page) {
      if (!keywordInput.value.trim() && !universityInput.value.trim()) {
        toast(t('admission.pleaseEnterAMajorKeywordOrUniversity'), true);
        return;
      }
      const params = {
        province: view.province,
        year: view.year,
        category: view.category,
        keyword: keywordInput.value.trim(),
        university: universityInput.value.trim(),
        page: page || 1,
        page_size: 20,
      };
      result.replaceChildren(loading(t('admission.searching')));
      try {
        const data = await api(`/admission/plans?${query(params)}`);
        if (!guardStale(seq) || !result.isConnected) return;
        result.replaceChildren(plansResult(data, params, nextPage => run(nextPage)));
      } catch (error) {
        if (result.isConnected) result.replaceChildren(notice(error.message, 'error'));
      }
    }
    keywordInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); run(1); }
    });
    if (view.pendingMajor) {
      keywordInput.value = view.pendingMajor;
      view.pendingMajor = null;
      run(1);
    }
    return card;
  }

  function plansResult(data, params, onPage) {
    if (!data.items?.length) {
      return empty(t('admission.noPlanRecordsFound'), t('admission.tryAnotherMajorKeywordUniversityOrYear'), null, 'file');
    }
    const note = data.category_filtered ? null
      : notice(t('admission.theEnrollmentPlanForThisProvinceYear'), 'warning');
    return el('div', { class: 'stack' },
      note,
      el('div', { class: 'pg-list-toolbar' },
        el('span', { class: 'pg-filter-summary' },
          t('admission.planRecordsKnownPlannedTotalPeople', { v1: number(data.total), v2: number(data.total_people) })
          + t('admission.page', { v1: data.page, v2: Math.max(1, Math.ceil(data.total / data.page_size)) }))),
      el('div', { class: 'table-wrap' }, dataTable(
        [t('admission.university'), t('admission.major'), t('admission.batch'), t('admission.plannedCount'), t('admission.duration'), t('admission.tuition'), t('admission.subjectRequirements')],
        data.items.map(row => [
          row.university_name || '—', row.major_name || '—', row.batch || '—',
          row.plan_count != null ? number(row.plan_count) : t('admission.notProvided'),
          row.duration || '—', row.tuition != null ? `¥${number(Math.round(row.tuition))}` : '—',
          row.subject_req || '—',
        ])
      )),
      pager(data.total, data.page, data.page_size, onPage));
  }

  /* ---------------- 公共渲染 ---------------- */

  function dataTable(headers, rows) {
    const head = el('thead', {}, el('tr', {}, ...headers.map(text => el('th', {}, text))));
    const body = el('tbody', {}, ...rows.map(cells => el('tr', {}, ...cells.map(cell => el('td', {}, cell)))));
    return el('table', {}, head, body);
  }

  function pager(total, page, pageSize, onPage) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    return el('div', { class: 'pagination' },
      el('span', {}, t('common.pageOf', { page: page, pages: pages })),
      el('div', { class: 'row' },
        button(t('common.prevPage'), () => onPage(page - 1), 'secondary small', null, { disabled: page <= 1 }),
        button(t('common.nextPage'), () => onPage(page + 1), 'secondary small', null, { disabled: page >= pages })));
  }

  /* ---------------- 简易折线图（SVG，无第三方库） ---------------- */

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgNode(tag, attrs = {}, children = []) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    }
    for (const child of children) node.append(child);
    return node;
  }

  function lineChart(points, { xLabel = '', yLabel = '', height = 200 } = {}) {
    const valid = (points || []).filter(point => Number.isFinite(Number(point.y)));
    if (valid.length < 2) return el('p', { class: 'help' }, t('admission.notEnoughDataPointsToDrawA'));
    const width = 640;
    const pad = { top: 16, right: 18, bottom: 30, left: 54 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    const ys = valid.map(point => Number(point.y));
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const spanY = maxY - minY || 1;
    const stepX = innerW / Math.max(1, valid.length - 1);
    const toX = index => pad.left + index * stepX;
    const toY = value => pad.top + innerH - ((value - minY) / spanY) * innerH;
    const grid = [];
    for (let index = 0; index <= 4; index += 1) {
      const y = pad.top + (innerH / 4) * index;
      grid.push(svgNode('line', { x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: 'pg-chart-grid' }));
      grid.push(svgNode('text', {
        x: pad.left - 8, y: y + 4, class: 'pg-chart-label', 'text-anchor': 'end',
      }, [document.createTextNode(String(Math.round(maxY - (spanY / 4) * index)))]));
    }
    const linePath = valid
      .map((point, index) => `${index === 0 ? 'M' : 'L'}${toX(index).toFixed(1)},${toY(Number(point.y)).toFixed(1)}`)
      .join(' ');
    const areaPath = `${linePath} L${toX(valid.length - 1).toFixed(1)},${(pad.top + innerH).toFixed(1)}`
      + ` L${toX(0).toFixed(1)},${(pad.top + innerH).toFixed(1)} Z`;
    return svgNode('svg', {
      viewBox: `0 0 ${width} ${height}`,
      class: 'pg-admit-chart',
      role: 'img',
      'aria-label': t('admission.trendChart', { v1: yLabel }),
    }, [
      ...grid,
      svgNode('path', { d: areaPath, class: 'pg-chart-area' }),
      svgNode('path', { d: linePath, class: 'pg-chart-line' }),
      svgNode('text', { x: pad.left, y: height - 8, class: 'pg-chart-label' }, [document.createTextNode(String(valid[0].x))]),
      svgNode('text', {
        x: width - pad.right, y: height - 8, class: 'pg-chart-label', 'text-anchor': 'end',
      }, [document.createTextNode(String(valid[valid.length - 1].x))]),
      svgNode('text', { x: pad.left, y: 12, class: 'pg-chart-label' }, [document.createTextNode(yLabel)]),
      svgNode('text', {
        x: width - pad.right, y: 12, class: 'pg-chart-label', 'text-anchor': 'end',
      }, [document.createTextNode(xLabel)]),
    ]);
  }

  /* ---------------- 数据缺失声明 ---------------- */

  let gapsData = null;
  const gapContext = el('div', { class: 'pg-admit-gap-current' });
  const gapsBody = el('div', { class: 'pg-admit-gaps-body' }, loading(t('admission.readingTheMissingDataStatement')));
  const gapsBox = el('details', { class: 'pg-admit-gaps' },
    el('summary', {}, t('admission.missingDataStatementReportedHonestlyByProvince')),
    gapsBody);

  const GAP_LABELS = {
    ok: t('admission.complete'), partial: t('admission.partiallyMissing'), missing: t('admission.missing'), no_data: t('admission.noDataForThisTable'),
    official: t('admission.officialSupplement'), native: t('admission.fromPrimaryData'),
  };
  const TABLE_LABELS = {
    'score-range': t('admission.scoreRankTable'), 'school-admission': t('admission.universityCutoffLines'),
    'major-admission': t('admission.majorAdmissionLines'), 'enrollment-plan': t('admission.enrollmentPlan'),
  };

  async function loadGaps() {
    try {
      const data = await api('/admission/gaps');
      if (!data.available) {
        gapsBody.replaceChildren(notice(data.detail || t('admission.noMissingDataStatementGeneratedYet'), 'warning'));
        return;
      }
      gapsData = data;
      const summary = data.summary || {};
      const count = group => {
        const bucket = summary[group] || {};
        return (bucket.missing || 0) + (bucket.no_data || 0);
      };
      const officialYears = (data.official?.years || []).join(t('common.listSeparator'));
      gapsBody.replaceChildren(
        el('p', {},
          t('admission.thisPageCoversProvinceYearsMissingValues', { v1: number(summary.province_years || 0) }),
          t('admission.officialCoverage', { v1: officialYears || t('admission.noneYet') })),
        el('div', { class: 'pg-admit-gap-tags' },
          ...([
            [t('admission.missingRank'), count('rank')],
            [t('admission.missingControlScore'), count('control')],
            [t('admission.missingPlannedCount'), count('plan')],
            [t('admission.missingCategory'), count('category')],
            [t('admission.provinceYearsWithMissingTables'), summary.province_years_missing_tables || 0],
          ].map(([label, value]) => el('span', { class: 'pg-tag', 'data-tone': value ? 'warn' : 'ok' },
            t('admission.provinceYears', { v1: label, v2: number(value) }))))),
        gapContext,
        el('p', { class: 'help' },
          t('admission.seeTheDataGapStatementDocumentIn_2')));
      renderGapContext();
    } catch (error) {
      gapsBody.replaceChildren(notice(t('admission.failedToReadTheMissingDataStatement', { v1: error.message }), 'error'));
    }
  }

  function renderGapContext() {
    if (!gapsData) return;
    const entry = (gapsData.province_years || []).find(
      item => item.province === view.province && item.year === view.year);
    if (!entry) {
      gapContext.replaceChildren(el('span', { class: 'pg-tag', 'data-tone': 'warn' }, t('admission.thisProvinceYearIsOutsideTheStatement')));
      return;
    }
    const statusTag = (label, status) => {
      const good = ['ok', 'native', 'official'].includes(status);
      return el('span', { class: 'pg-tag', 'data-tone': good ? 'ok' : 'warn' },
        `${label}：${GAP_LABELS[status] || status}`);
    };
    const children = [
      el('strong', {}, t('admission.text', { v1: entry.province_name, v2: entry.year })),
      statusTag(t('admission.rank_3'), entry.rank),
      statusTag(t('admission.controlScoreLine'), entry.control),
      statusTag(t('admission.plannedCount'), entry.plan),
      statusTag(t('admission.category'), entry.category),
      (entry.missing_tables || []).length
        ? el('span', { class: 'pg-tag', 'data-tone': 'warn' },
          t('admission.missingTables', { v1: entry.missing_tables.map(table => TABLE_LABELS[table] || table).join('、') }))
        : null,
    ].filter(Boolean);
    gapContext.replaceChildren(...children);
  }

  /* ---------------- 初始化 ---------------- */

  async function onFiltersChange() {
    const seq = ++filterSeq;
    view.province = provinceSelect.value;
    view.provinceName = provinces.find(item => item.slug === view.province)?.name || view.province;
    view.year = Number(yearSelect.value);
    view.category = '';
    const ok = await loadFilters(seq);
    if (!ok || !current(version)) return;
    renderPanel();
    renderGapContext();
  }

  provinceSelect.addEventListener('change', onFiltersChange);
  yearSelect.addEventListener('change', onFiltersChange);
  categorySelect.addEventListener('change', () => {
    view.category = categorySelect.value;
    renderPanel();
  });

  const filterCard = el('section', { class: 'card filter-card' },
    el('div', { class: 'pg-admit-filters' },
      field(t('admission.province'), provinceSelect),
      field(t('practice.year'), yearSelect),
      field(t('admission.category'), categorySelect, t('admission.leaveBlankToAutoSelectTheCategory'))));

  renderTabs();
  container.replaceChildren(
    heading('GAOKAO COMPASS', t('nav.admission'),
      t('admission.convertYourScoreIntoARankWith')),
    filterCard,
    statsGrid,
    completenessBar,
    tabBar,
    panel,
    gapsBox,
    el('p', { class: 'footer-note' },
      t('admission.dataSourceGaokaocompassHuggingFaceMitLicense'),
      el('br'),
      t('admission.unofficialDataFieldsMayBeMissingOr'),
      el('br'),
      t('admission.seeThirdPartyNoticesMdInThe'))
  );

  await loadFilters(++filterSeq);
  if (!current(version)) return;
  renderPanel();
  loadGaps();
}
