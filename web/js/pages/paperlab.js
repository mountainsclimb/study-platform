/* 试卷工坊：识别导入 / 组卷 / 我的试卷 / 错题巩固卷（P0 + P1 前端）。
 *
 * - 识别导入：上传→分页→视觉模型逐页识别→人工核验草稿→批量入库（components/paperImport.js）；
 * - 组卷：手工双来源选题（我的题库 / 高考真题库，另按能力探测追加平台精选与全部来源）
 *   + AI 组卷任务（components/paperCompose.js）；
 * - 我的试卷：列表/详情/题序分值编辑/发布/Word 导出/打印（components/paperDocument.js）；
 * - 错题巩固卷：replay 直接建卷 / consolidate AI 任务（components/paperCompose.js）。
 *
 * 深链接：`#/paperlab`、`#/paperlab?tab=compose|papers|revision`、
 * `#/paperlab?import=ID`（导入详情）、`#/paperlab?paper=ID`（试卷详情）、`#/paperlab?tab=history`（导入历史）。
 * 答题卡制作属后续里程碑：页面仅一行说明，不放不可用按钮。
 */
import { api } from '../core/api.js';
import { onSessionReset } from '../core/state.js';
import {
  el, heading, button, notice, loading, empty, busy, toast, query,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import {
  createImportUploader, renderImportDetail, importHistoryCard,
} from '../components/paperImport.js';
import {
  renderComposeWorkbench, renderRevisionStudio,
} from '../components/paperCompose.js';
import {
  renderPaperList, renderPaperDetail,
} from '../components/paperDocument.js';
import { t } from '../core/i18n.js';

const HISTORY_PAGE_SIZE = 10;
// 平台精选组卷来源能力探测：与练习页同一契约（WS19 `/api/questions?source=bank|official|all`）。
// 用合法 official 请求探测：旧后端忽略未知参数、响应体无 official_total → 未就绪；
// 避免用非法 source 触发 4xx 控制台错误。
let officialSourceSupportPromise = null;

function detectOfficialSource() {
  if (!officialSourceSupportPromise) {
    officialSourceSupportPromise = api('/questions?page=1&page_size=1&source=official')
      .then(data => Boolean(data && typeof data === 'object' && 'official_total' in data))
      .catch(() => false);
  }
  return officialSourceSupportPromise;
}
// 模块级常量不能直接 t()（语言切换后模块不重载），用 getter 惰性求值。
const MAIN_TABS = [
  { value: 'imports', get label() { return t('home.status.imports'); } },
  { value: 'compose', get label() { return t('paperlab.compose'); } },
  { value: 'papers', get label() { return t('paperlab.myPapers'); } },
  { value: 'revision', get label() { return t('home.ai.revision.title'); } },
];

let metaCache = null;
let historyPage = 1;
let historyTotal = 0;

onSessionReset(() => {
  metaCache = null;
  historyPage = 1;
  historyTotal = 0;
});

async function loadMeta() {
  if (metaCache) return metaCache;
  metaCache = await api('/paperlab/meta');
  return metaCache;
}

function navigate(tab, extra = null) {
  const params = { ...(extra || {}) };
  if (tab && tab !== 'imports') params.tab = tab;
  go('paperlab', null, params);
}

function openPaper(paperId) {
  if (!paperId) { navigate('papers'); return; }
  navigate('papers', { paper: paperId });
}

function sectionOf(params) {
  const raw = String(params?.get?.('tab') || 'imports');
  if (MAIN_TABS.some(tab => tab.value === raw)) return raw;
  if (raw === 'history') return 'imports';
  return 'imports';
}

/* ---------------------------------------------------------------------------
 * 识别导入：首页 / 历史
 * ------------------------------------------------------------------------- */

async function renderHistory(host, container, version) {
  host.replaceChildren(loading(t('paperlab.loadingImportHistory')));
  let data;
  try {
    data = await api(`/paperlab/imports?${query({ page: historyPage, page_size: HISTORY_PAGE_SIZE })}`);
  } catch (error) {
    if (!current(version)) return;
    host.replaceChildren(
      notice(error.message || t('paperlab.failedToLoadImportRecordsPleaseTry'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderHistory(host, container, version), '', 'repeat'),
        button(t('paperlab.uploadAPaper'), () => navigate('imports'), 'secondary')));
    return;
  }
  if (!current(version)) return;
  historyTotal = Number(data.total) || 0;
  const items = Array.isArray(data.items) ? data.items : [];
  const totalPages = Math.max(1, Math.ceil(historyTotal / HISTORY_PAGE_SIZE));
  host.replaceChildren();
  if (!items.length) {
    host.append(empty(t('paperlab.noImportsYet'),
      t('paperlab.uploadAPaperOrHandoutImagePdf'),
      button(t('paperlab.uploadAPaper'), () => navigate('imports'), '', 'upload'), 'file'));
    return;
  }
  const list = el('div', { class: 'pl-history-list' });
  for (const row of items) {
    list.append(importHistoryCard(row, {
      onOpen: item => navigate('imports', { import: item.id }),
      onContinue: item => navigate('imports', { import: item.id }),
    }));
  }
  host.append(
    el('div', { class: 'pl-history-toolbar' },
      el('span', { class: 'help' }, t('paperlab.importsPageOf', { v1: historyTotal, v2: historyPage, v3: totalPages })),
      el('div', { class: 'row' },
        button(t('paperlab.backToUpload'), () => navigate('imports'), 'ghost small', 'chevron'),
        button(t('tasks.refresh'), event => busy(event.currentTarget, () => renderHistory(host, container, version)),
          'secondary small', 'repeat'))),
    list,
    el('div', { class: 'pagination pl-history-pagination' },
      el('span', {}, t('common.pageOf', { page: historyPage, pages: totalPages })),
      el('div', { class: 'row' },
        button(t('common.prevPage'), () => {
          if (historyPage <= 1) return;
          historyPage -= 1;
          renderHistory(host, container, version);
        }, 'secondary small', 'chevron', { disabled: historyPage <= 1 }),
        button(t('common.nextPage'), () => {
          if (historyPage >= totalPages) return;
          historyPage += 1;
          renderHistory(host, container, version);
        }, 'secondary small', 'chevron', { disabled: historyPage >= totalPages }))));
}

async function renderImportsHome(host, container, version, meta) {
  host.replaceChildren(loading(t('paperlab.loadingImports')));
  let recent = [];
  let total = 0;
  try {
    const data = await api(`/paperlab/imports?${query({ page: 1, page_size: 5 })}`);
    recent = Array.isArray(data.items) ? data.items : [];
    total = Number(data.total) || 0;
  } catch {
    recent = [];
  }
  if (!current(version)) return;
  host.replaceChildren();
  const uploader = createImportUploader({
    meta,
    onCreated: imported => {
      toast(t('paperlab.uploadCompleteOpeningRecognitionDetails'));
      navigate('imports', { import: imported.id });
    },
  });
  host.append(uploader.node);

  const recentCard = el('section', { class: 'card pl-recent-card' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paperlab.recentImports')),
        el('p', { class: 'help' }, t('paperlab.uploadRecognizeVerifyManuallyThenCommitTo'))),
      button(t('paperlab.viewAllImports'), () => navigate('imports', { tab: 'history' }), 'secondary small', 'history')));
  if (!recent.length) {
    recentCard.append(empty(t('paperlab.noImportsYet'), t('paperlab.uploadYourFirstPaperToStartRecognition'), null, 'file'));
  } else {
    const list = el('div', { class: 'pl-recent-list' });
    for (const row of recent) {
      list.append(importHistoryCard(row, {
        onOpen: item => navigate('imports', { import: item.id }),
        onContinue: item => navigate('imports', { import: item.id }),
      }));
    }
    recentCard.append(list);
    if (total > recent.length) {
      recentCard.append(el('div', { class: 'row end pl-recent-more' },
        button(t('paperlab.viewMore', { v1: total - recent.length }),
          () => navigate('imports', { tab: 'history' }), 'ghost small', 'history')));
    }
  }
  host.append(recentCard);
}

/* ---------------------------------------------------------------------------
 * 页面壳
 * ------------------------------------------------------------------------- */

function buildTabs(active, container, version) {
  const tabs = el('div', { class: 'tabs pl-main-tabs', role: 'tablist', 'aria-label': t('paperlab.paperStudioSections') });
  for (const tab of MAIN_TABS) {
    tabs.append(el('button', {
      type: 'button',
      class: `tab${active === tab.value ? ' active' : ''}`,
      role: 'tab',
      'aria-selected': active === tab.value ? 'true' : 'false',
      onClick: () => navigate(tab.value),
    }, tab.label));
  }
  return tabs;
}

export async function renderPaperlab(container, version, params = null) {
  const activeTab = sectionOf(params);
  const rawTab = String(params?.get?.('tab') || '');
  const importId = Number(params?.get?.('import') || 0);
  const paperId = Number(params?.get?.('paper') || 0);
  const pageHead = heading(
    'PAPER WORKSHOP',
    t('home.ai.paperlab.title'),
    t('paperlab.importWholePapersByRecognitionComposeThem'),
    button(t('tasks.refresh'), () => renderPaperlab(container, version, params), 'secondary', 'repeat'));
  const body = el('div', { class: 'pl-page-body' });
  const tabs = buildTabs(activeTab, container, version);
  const capabilityNote = notice(
    t('paperlab.answerSheetsCanBeGeneratedFromPaper')
    + t('paperlab.uploadAPhotoScanAutomaticGradingManual')
    + t('paperlab.compositionUsesRealBankCandidatesAndA')
    + t('paperlab.andFailedHardChecksReportTheExact'));
  container.replaceChildren(pageHead, capabilityNote, tabs, body);
  body.replaceChildren(loading(t('paperlab.preparingPaperStudio')));

  let meta;
  try {
    meta = await loadMeta();
  } catch (error) {
    if (!current(version)) return;
    body.replaceChildren(
      el('div', { class: 'error-panel' },
        error.message || t('paperlab.paperStudioIsTemporarilyUnavailablePleaseTry'),
        el('div', { class: 'row', style: 'margin-top:14px' },
          button(t('common.reload'), () => renderPaperlab(container, version, params), '', 'repeat'))));
    return;
  }
  if (!current(version)) return;

  if (activeTab === 'imports') {
    if (importId) {
      await renderImportDetail({
        mount: body,
        importId,
        meta,
        version,
        onBack: () => navigate('imports'),
      });
      return;
    }
    if (rawTab === 'history') {
      await renderHistory(body, container, version);
      return;
    }
    await renderImportsHome(body, container, version, meta);
    return;
  }

  if (activeTab === 'compose') {
    // 组卷选题的来源筛选（高考真题/平台精选/全部）依赖 WS19 的 source 参数：
    // 能力探测结果交给工坊组件决定是否显示「平台精选/全部」来源页签。
    const officialSourcesSupported = await detectOfficialSource();
    if (!current(version)) return;
    await renderComposeWorkbench({
      mount: body,
      meta,
      version,
      officialSourcesSupported,
      onOpenPaper: paper => openPaper(paper),
    });
    return;
  }

  if (activeTab === 'papers') {
    if (paperId) {
      await renderPaperDetail({
        mount: body,
        paperId,
        meta,
        version,
        onBack: () => navigate('papers'),
      });
      return;
    }
    await renderPaperList({
      mount: body,
      meta,
      version,
      onOpen: id => openPaper(id),
    });
    return;
  }

  if (activeTab === 'revision') {
    await renderRevisionStudio({
      mount: body,
      meta,
      version,
      onOpenPaper: paper => openPaper(paper),
    });
    return;
  }

  body.replaceChildren(notice(t('paperlab.unknownPaperStudioSectionReturnedToRecognition'), 'warning'));
  navigate('imports');
}
