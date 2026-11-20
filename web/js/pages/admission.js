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

const TABS = [
  { key: 'rank', label: '一分一段', icon: 'chart' },
  { key: 'schools', label: '找学校', icon: 'search' },
  { key: 'school', label: '查学校 / 专业', icon: 'university' },
  { key: 'plans', label: '招生计划', icon: 'file' },
];

let metaPromise = null;

/** 数据集元信息在会话内复用；未导入时返回 ready:false 供页面给出操作指引。 */
function loadMeta(force = false) {
  if (!metaPromise || force) {
    metaPromise = api('/admission/meta').catch(error => ({ ready: false, error: error.message }));
  }
  return metaPromise;
}

export async function renderAdmission(container, version) {
  container.replaceChildren(loading('正在加载志愿数据…'));
  const meta = await loadMeta();
  if (!current(version)) return;

  if (!meta.ready) {
    container.replaceChildren(
      heading('GAOKAO COMPASS', '志愿查询', '用一分一段表把分数换成位次，再看清可选院校与专业。'),
      notice('志愿数据尚未导入，志愿查询暂不可用。', 'warning'),
      empty(
        '还没有志愿数据',
        meta.error || '请在 study-platform 目录运行 python scripts/import_admission.py 生成 data/admission.sqlite3 后刷新页面。',
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
  const yearSelect = select(years.slice().reverse().map(item => ({ value: item, label: `${item} 年` })), view.year);
  const categorySelect = select([{ value: '', label: '自动（数据最多的科类）' }], '');
  const statsGrid = el('div', { class: 'stats-grid' });
  const completenessBar = el('div', { class: 'pg-admit-completeness' });
  const tabBar = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': '志愿查询分区' });
  const panel = el('div', { class: 'stack' });
  let panelSeq = 0;
  let filterSeq = 0;

  /* ---------------- 顶部筛选与看板 ---------------- */

  function rebuildCategoryOptions() {
    const options = [{ value: '', label: '自动（数据最多的科类）' }];
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
      ['位次数据', Boolean(filters.rank_supported), '该省年投档线缺少位次，按位次匹配不可用，请改用分数查询'],
      ['批次线', Boolean(filters.control_score_supported), '该省年一分一段缺少批次控制线'],
      ['科类', Boolean(filters.category_available), '该省年录取表缺少科类字段，科类筛选会自动忽略'],
      ['计划人数', Boolean(filters.plan_supported), '该省年招生计划缺少计划人数，表格显示「未提供」'],
    ];
    return [
      el('span', { class: 'pg-admit-completeness-label' }, '本省本年数据完整度'),
      ...items.map(([label, ok, hint]) => el('span', {
        class: 'pg-tag',
        'data-tone': ok ? 'ok' : 'warn',
        title: ok ? `${label}可用` : hint,
      }, `${label}：${ok ? '可用' : '缺失'}`)),
    ];
  }

  function buildStatCards(overview, filters) {
    const exam = overview?.exam_overview || {};
    const university = overview?.university_stats || {};
    const enrollment = overview?.enrollment_stats || {};
    const counts = filters?.counts || {};
    if (overview) {
      return [
        statCard('高考人数', number(exam.total_candidates || 0), '人', '省考试院公布口径'),
        statCard('招生院校', number(university.total || enrollment.university_count || 0), '所', '本省该年出现过的院校'),
        statCard('招生计划', enrollment.total_plan ? number(enrollment.total_plan) : '未提供', '', '部分年份缺少计划数'),
        statCard('985 / 211', `${number(university.is_985 || 0)} / ${number(university.is_211 || 0)}`, '所', '院校属性标记'),
      ];
    }
    return [
      statCard('一分一段', number(counts['score-range'] || 0), '条', '分数—位次记录'),
      statCard('院校投档线', number(counts['school-admission'] || 0), '条', '院校最低分与位次'),
      statCard('专业录取线', number(counts['major-admission'] || 0), '条', '专业维度录取分数'),
      statCard('招生计划', number(counts['enrollment-plan'] || 0), '条', '院校专业计划明细'),
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
    }, tab.label)));
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
    const scoreInput = input('例如 600', { type: 'number', min: '0', max: '1000', inputmode: 'numeric' });
    const result = el('div');
    const form = el('div', { class: 'pg-admit-form pg-admit-form--narrow' },
      field('高考分数', scoreInput),
      el('div', { class: 'pg-admit-actions' }, button('查询位次', run, '', 'chart')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, '一分一段 · 分数换位次'),
          el('p', { class: 'help' }, '位次为累计人数口径；同一分数可能出现在多个批次，结果中会分别列出。')),
        view.filters?.rank_supported ? badge('支持位次', 'teal') : badge('缺位次数据', 'amber')),
      form,
      result);

    async function run() {
      const score = Number(scoreInput.value);
      if (!Number.isFinite(score) || score < 0 || score > 1000) {
        toast('请输入 0–1000 之间的有效分数。', true);
        return;
      }
      result.replaceChildren(loading('正在查询…'));
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
          `${data.year} 年 · ${data.province_name} · ${data.category || '未分科类'} · ${data.score} 分`),
        el('div', { class: 'pg-admit-big' }, rank ? number(rank) : '暂无',
          el('span', { class: 'pg-admit-big-unit' }, '位')),
        el('p', { class: 'help' }, data.rank_range ? `位次区间 ${data.rank_range}` : '')),
      el('div', { class: 'pg-admit-facts' },
        factRow('批次', data.batch || '未标注'),
        factRow('批次线', data.control_score ? `${data.control_score} 分` : '数据源未提供'),
        factRow('本段人数', data.segment_count ? number(data.segment_count) : '—'),
        factRow('该科类总人数', data.total ? number(data.total) : '—'),
        factRow('精确匹配', data.exact ? '是' : `否，最接近 ${data.nearest_score} 分`)));
    const chart = lineChart(
      (data.distribution || []).map(point => ({ x: point.score, y: point.cumulative_count })),
      { xLabel: '分数', yLabel: '累计位次', height: 220 }
    );
    const matches = data.matches?.length > 1
      ? el('div', { class: 'pg-admit-table' },
        el('h3', {}, '同分数各批次记录'),
        dataTable(['批次', '批次线', '本段人数', '累计位次', '位次区间'], data.matches.map(row => [
          row.batch || '—', row.control_score ?? '—', number(row.segment_count || 0),
          number(row.cumulative_count || 0), row.rank_range || '—',
        ])))
      : null;
    return el('div', { class: 'stack' },
      el('section', { class: 'card' }, head,
        chart ? el('div', { class: 'pg-admit-chart-wrap' }, chart) : null,
        matches));
  }

  function factRow(label, value) {
    return el('div', { class: 'pg-admit-fact' }, el('span', {}, label), el('strong', {}, value));
  }

  /* ---------------- 找学校 ---------------- */

  function schoolsTab() {
    const seq = panelSeq;
    const modeSelect = select([
      { value: 'score', label: '按分数匹配' },
      { value: 'rank', label: '按位次匹配' },
    ], 'score');
    const valueInput = input('例如 600', { type: 'number', inputmode: 'numeric' });
    const bandInput = input('15', { type: 'number', min: '1', max: '80' });
    const keywordInput = input('如：浙江大学（可选）', { type: 'search' });
    const result = el('div');
    const bandField = field('分数浮动', bandInput, '上下浮动分数，默认 ±15 分');
    const modeHint = el('p', { class: 'help' }, '输入分数后，按投档线与分数的差值分为冲、稳、保三档。');
    modeSelect.addEventListener('change', () => {
      const rankMode = modeSelect.value === 'rank';
      bandField.classList.toggle('hide', rankMode);
      modeHint.textContent = rankMode
        ? '位次模式按投档位次与你的位次的比例划分：低于 0.85 为冲，0.85–1.2 为稳，其余为保。'
        : '输入分数后，按投档线与分数的差值分为冲、稳、保三档。';
    });
    const form = el('div', { class: 'pg-admit-form pg-admit-form--wide' },
      field('匹配方式', modeSelect),
      field('分数 / 位次', valueInput),
      bandField,
      field('院校关键词', keywordInput),
      el('div', { class: 'pg-admit-actions' }, button('开始匹配', () => run(1), '', 'search')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, '找学校 · 冲稳保'), modeHint),
        view.filters?.rank_supported ? badge('位次可用', 'teal') : badge('仅分数', 'amber')),
      form,
      result);

    async function run(page) {
      const value = Number(valueInput.value);
      if (!Number.isFinite(value) || value <= 0) {
        toast('请输入有效的分数或位次。', true);
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
      result.replaceChildren(loading('正在匹配院校…'));
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
      return notice(data.note || '该省份年份的投档线缺少位次数据，请改用分数查询。', 'warning');
    }
    if (!data.items?.length) {
      return empty('没有匹配到院校', '换一个分数区间或年份试试；老年份可能缺少位次或批次数据。', null, 'search');
    }
    const note = data.category_filtered ? null
      : notice('该省份年份的录取数据缺少科类字段，已忽略科类筛选。', 'warning');
    return el('div', { class: 'stack' },
      note,
      el('div', { class: 'pg-list-toolbar' },
        el('span', { class: 'pg-filter-summary' }, `共 ${number(data.total)} 所院校 · 第 ${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))} 页`),
        el('span', { class: 'help' }, data.mode === 'score' ? `分数浮动 ±${params.band} 分` : '按位次比例匹配')),
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
        el('span', {}, item.min_score != null ? `最低分 ${item.min_score}` : '最低分暂无'),
        el('span', {}, item.min_rank != null ? `位次 ${number(item.min_rank)}` : '位次暂无'),
        item.delta != null ? el('span', {}, `分差 ${item.delta > 0 ? '+' : ''}${item.delta}`) : null,
        item.ratio != null ? el('span', {}, `位次比 ${item.ratio}`) : null)),
    item.level ? el('span', { class: `badge ${levelTone} pg-admit-level` }, item.level) : null);
  }

  /* ---------------- 查学校 / 专业 ---------------- */

  function schoolTab() {
    const seq = panelSeq;
    const searchInput = input('输入院校名称，如：浙江大学', { type: 'search', autocomplete: 'off' });
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
            ...(data.universities || []).map(row => ({ name: row.name, meta: '院校' })),
            ...(data.majors || []).map(row => ({ name: row.name, meta: '专业' })),
          ];
          if (!items.length) { suggestBox.hidden = true; return; }
          suggestBox.replaceChildren(...items.map(item => el('button', {
            type: 'button',
            class: 'pg-admit-suggest-item',
            onClick: () => {
              suggestBox.hidden = true;
              if (item.meta === '院校') {
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
      result.replaceChildren(loading('正在读取院校数据…'));
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
        el('div', {}, el('h2', {}, '查学校 / 专业'),
          el('p', { class: 'help' }, '院校投档线、专业录取线、招生计划与历年趋势一次看全。'))),
      el('div', { class: 'pg-admit-search' },
        el('div', { class: 'search-wrap' }, searchInput, suggestBox),
        button('查询', () => { if (searchInput.value.trim()) load(searchInput.value.trim()); }, '', 'search')),
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
          `${data.year} 年 · ${data.province_name} · ${data.category || '未分科类'}`
          + (data.plan_summary?.rows
            ? ` · 招生计划 ${number(data.plan_summary.rows)} 条（已知计划数合计 ${number(data.plan_summary.people)} 人）`
            : ''))),
      el('div', { class: 'pg-admit-facts' },
        factRow('投档线记录', `${data.cutoffs?.length || 0} 条`),
        factRow('专业录取记录', `${data.majors?.length || 0} 条`),
        factRow('招生计划记录', `${data.plans?.length || 0} 条`)));

    const trendChart = lineChart(
      (data.trend || []).filter(row => row.min_score != null).map(row => ({ x: row.year, y: row.min_score })),
      { xLabel: '年份', yLabel: '最低分', height: 190 }
    );

    return el('div', { class: 'stack' },
      el('section', { class: 'card' }, head,
        (data.trend || []).filter(row => row.min_score != null).length > 1
          ? el('div', { class: 'pg-admit-chart-wrap' }, trendChart) : null),
      data.cutoffs?.length ? sectionCard('院校投档线', dataTable(
        ['批次', '科类', '最低分', '最低位次', '录取人数', '选科要求'],
        data.cutoffs.slice(0, 60).map(row => [
          row.batch || '—', row.category || '—', row.min_score ?? '—',
          row.min_rank != null ? number(row.min_rank) : '—',
          row.admit_count != null ? number(row.admit_count) : '—', row.subject_req || '—',
        ])
      )) : null,
      data.majors?.length ? sectionCard('专业录取线', dataTable(
        ['专业', '批次', '最低分', '最低位次', '平均分', '录取人数'],
        data.majors.slice(0, 80).map(row => [
          row.major_name || '—', row.batch || '—', row.min_score ?? '—',
          row.min_rank != null ? number(row.min_rank) : '—',
          row.avg_score != null ? row.avg_score : '—',
          row.admit_count != null ? number(row.admit_count) : '—',
        ])
      ), data.majors.length > 80 ? `仅显示前 80 条，共 ${data.majors.length} 条` : '') : null,
      data.plans?.length ? sectionCard('招生计划', dataTable(
        ['专业', '批次', '计划数', '学制', '学费', '选科要求'],
        data.plans.slice(0, 80).map(row => [
          row.major_name || '—', row.batch || '—',
          row.plan_count != null ? number(row.plan_count) : '未提供',
          row.duration || '—', row.tuition != null ? `¥${number(Math.round(row.tuition))}` : '—',
          row.subject_req || '—',
        ])
      ), data.plans.length > 80 ? `仅显示前 80 条，共 ${data.plans.length} 条` : '') : null,
      notice('表格中的「—」表示数据源缺字段；2017–2021 年多数省份缺少位次与批次线，请以分数趋势为主。'));
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
    const keywordInput = input('专业关键词，如：计算机', { type: 'search' });
    const universityInput = input('院校名称（可选）', { type: 'search' });
    const result = el('div');
    const form = el('div', { class: 'pg-admit-form pg-admit-form--three' },
      field('专业关键词', keywordInput),
      field('院校名称', universityInput),
      el('div', { class: 'pg-admit-actions' }, button('查询计划', () => run(1), '', 'search')));
    const card = el('section', { class: 'card' },
      el('div', { class: 'card-header' },
        el('div', {}, el('h2', {}, '招生计划检索'),
          el('p', { class: 'help' }, '2023–2025 年部分省份的计划数缺失，表格中显示「未提供」。'))),
      form,
      result);

    async function run(page) {
      if (!keywordInput.value.trim() && !universityInput.value.trim()) {
        toast('请输入专业关键词或院校名称。', true);
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
      result.replaceChildren(loading('正在查询…'));
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
      return empty('没有查到计划记录', '换一个专业关键词、院校或年份再试。', null, 'file');
    }
    const note = data.category_filtered ? null
      : notice('该省份年份的招生计划缺少科类字段，已忽略科类筛选。', 'warning');
    return el('div', { class: 'stack' },
      note,
      el('div', { class: 'pg-list-toolbar' },
        el('span', { class: 'pg-filter-summary' },
          `共 ${number(data.total)} 条计划 · 已知计划数合计 ${number(data.total_people)} 人`
          + ` · 第 ${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))} 页`)),
      el('div', { class: 'table-wrap' }, dataTable(
        ['院校', '专业', '批次', '计划数', '学制', '学费', '选科要求'],
        data.items.map(row => [
          row.university_name || '—', row.major_name || '—', row.batch || '—',
          row.plan_count != null ? number(row.plan_count) : '未提供',
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
      el('span', {}, `第 ${page} / ${pages} 页`),
      el('div', { class: 'row' },
        button('上一页', () => onPage(page - 1), 'secondary small', null, { disabled: page <= 1 }),
        button('下一页', () => onPage(page + 1), 'secondary small', null, { disabled: page >= pages })));
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
    if (valid.length < 2) return el('p', { class: 'help' }, '数据点不足，暂无法绘制趋势图。');
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
      'aria-label': `${yLabel}趋势图`,
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
  }

  provinceSelect.addEventListener('change', onFiltersChange);
  yearSelect.addEventListener('change', onFiltersChange);
  categorySelect.addEventListener('change', () => {
    view.category = categorySelect.value;
    renderPanel();
  });

  const filterCard = el('section', { class: 'card filter-card' },
    el('div', { class: 'pg-admit-filters' },
      field('省份', provinceSelect),
      field('年份', yearSelect),
      field('科类', categorySelect, '留空自动选择数据最多的科类')));

  renderTabs();
  container.replaceChildren(
    heading('GAOKAO COMPASS', '志愿查询',
      '用一分一段表把分数换算成位次，再按冲 / 稳 / 保查看可报院校、专业录取线与招生计划。'),
    filterCard,
    statsGrid,
    completenessBar,
    tabBar,
    panel,
    el('p', { class: 'footer-note' },
      '数据来源：GaokaoCompass（Hugging Face，MIT 许可，Copyright (c) 2025 choucisan），整理自各省教育考试院等公开渠道。',
      el('br'),
      '非官方数据，可能存在缺字段或误差，志愿填报请以各省考试院正式公布为准。',
      el('br'),
      '完整来源、许可与修改说明见项目根目录 THIRD_PARTY_NOTICES.md。')
  );

  await loadFilters(++filterSeq);
  if (!current(version)) return;
  renderPanel();
}
