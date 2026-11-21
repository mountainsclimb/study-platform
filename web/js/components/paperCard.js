/* 试卷工坊 · 答题卡制作：列表 / 生成 / 预览（SVG 安全渲染）/ 翻页缩放 / 打印 / 下载 / 编辑 / 删除。
 *
 * - 列表：GET /papers/{id}/cards（惰性 stale 标记）；生成：POST /papers/{id}/cards；
 * - 预览：GET /cards/{id} 取完整 layout 元信息；SVG 一律用 <img src="/api/paperlab/cards/{id}/svg?..."> 加载，
 *   绝不把服务端 SVG 字符串 innerHTML 插入页面（同源图片请求自带会话 Cookie，浏览器沙箱渲染）；
 * - 打印：隐藏 iframe 加载 GET /cards/{id}/export?format=html&page=all（后端自包含打印页），加载后 contentWindow.print()；
 * - 下载：逐页 fetch SVG Blob → <a download>；编辑标题 PATCH version CAS；版面参数变化无法用 PATCH 局部重算，
 *   采用「按新设置重新生成（新建卡后删除旧卡）」并在界面说明；stale 卡只读导出、提示重新生成。
 */
import { api, errorText } from '../core/api.js';
import {
  el, button, badge, notice, loading, busy, toast, dateText, field, input, select,
} from '../core/dom.js';
import { current } from '../core/router.js';
import { t } from '../core/i18n.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const CARD_STATUS_META = {
  get ready() { return [t('admission.available'), 'teal']; },
  get draft() { return [t('mybank.draft'), 'blue']; },
  get stale() { return [t('paper.card.invalidated'), 'amber']; },
};
const PRESETS = [
  { value: 'A4', get label() { return t('paper.card.presetA4'); } },
  { value: 'A3', get label() { return t('paper.card.a3Landscape420297mm'); } },
];
const COLUMN_OPTIONS = [1, 2, 3, 4].map(value => ({ value: String(value), get label() { return t('paper.card.columns', { v1: value }); } }));
const BUBBLE_OPTIONS = [
  { value: 'circle', get label() { return t('paper.card.roundBubbles'); } },
  { value: 'oval', get label() { return t('paper.card.ovalBubbles'); } },
];
const ZOOM_STEPS = [0.4, 0.5, 0.6, 0.75, 0.85, 1, 1.15, 1.3, 1.5, 1.75, 2];
const PRINT_FRAME_REMOVE_MS = 60000;

function statusBadge(status) {
  const meta = CARD_STATUS_META[status] || [status || t('tasks.unknownStatus'), ''];
  return badge(meta[0], meta[1]);
}

function cardInfoText(card) {
  return [
    t('paper.card.paper', {
      v1: card.preset || 'A4',
      v2: card.orientation === 'landscape' ? t('paper.card.landscapeSuffix') : '',
    }),
    t('paper.card.columns', { v1: card.columns || 1 }),
    t('common.countPages', { count: card.page_count || 0 }),
    card.bubble === 'oval' ? t('paper.card.ovalBubbles') : t('paper.card.roundBubbles'),
    card.include_subjective ? t('paper.card.withSubjectiveWritingArea') : t('paper.card.objectiveOnly'),
    t('common.updatedAt', { time: dateText(card.updated_at) }),
  ].join(' · ');
}

function svgUrl(card, page, color, cacheBust = '') {
  const params = new URLSearchParams({ page: String(page), color });
  if (cacheBust) params.set('v', String(cacheBust));
  return `/api/paperlab/cards/${card.id}/svg?${params}`;
}

function printUrl(card, color) {
  const params = new URLSearchParams({ format: 'html', page: 'all', color });
  return `/api/paperlab/cards/${card.id}/export?${params}`;
}

function pdfUrl(card) {
  return `/api/paperlab/cards/${card.id}/export?${new URLSearchParams({ format: 'pdf', page: 'all' })}`;
}

/** 服务端 PDF：与浏览器打印同源渲染；引擎不可用时后端 503，提示改用打印对话框。 */
async function downloadCardPdf(card) {
  const blob = await fetchBlob(pdfUrl(card));
  const base = String(card.title || t('paper.card.answerSheet', { v1: card.id })).replace(/[\\/:*?"<>|]+/g, '_').slice(0, 80);
  downloadBlob(blob, `${base}.pdf`);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const link = el('a', { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

async function fetchBlob(path) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(errorText(payload.detail || t('settings.wizard.requestFailed', { v1: response.status })));
  }
  return response.blob();
}

function cardFilename(card, page = null) {
  const base = String(card.title || t('paper.card.answerSheet', { v1: card.id })).replace(/[\\/:*?"<>|]+/g, '_').slice(0, 80);
  return page ? t('paper.card.pageSvg', { v1: base, v2: page }) : `${base}.svg`;
}

/* ---------------------------------------------------------------------------
 * 试卷详情里的「答题卡」区块：列表 + 生成表单
 * ------------------------------------------------------------------------- */

export async function renderCardSection({ mount, paperId, version, onOpenCard }) {
  mount.replaceChildren(loading(t('paper.card.loadingAnswerSheets')));
  let cards = [];
  let paperItems = [];
  let loadError = null;
  try {
    const [cardsData, paperData] = await Promise.all([
      api(`/paperlab/papers/${paperId}/cards`),
      api(`/paperlab/papers/${paperId}?include_snapshot=false`),
    ]);
    cards = Array.isArray(cardsData.items) ? cardsData.items : [];
    paperItems = Array.isArray(paperData.items) ? paperData.items : [];
  } catch (error) {
    loadError = error.message || t('paper.card.failedToLoadTheAnswerSheetList');
  }
  if (!current(version)) return;
  if (loadError) {
    mount.replaceChildren(
      notice(loadError, 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderCardSection({ mount, paperId, version, onOpenCard }), '', 'repeat')));
    return;
  }

  const objectiveCount = paperItems.filter(item =>
    item.type === 'SINGLE' || item.type === 'MULTIPLE').length;
  const subjectiveCount = paperItems.filter(item =>
    item.type === 'SUBJECTIVE' || item.type === 'MIXED').length;
  const formHost = el('div', { class: 'pl-card-form-host', hidden: true });
  const listHost = el('div', { class: 'pl-card-list' });
  const errorHost = el('div', { class: 'pl-card-error' });

  function renderList() {
    listHost.replaceChildren();
    if (!cards.length) {
      listHost.append(el('p', { class: 'help pl-card-empty' },
        t('paper.card.noAnswerSheetsYetClickGenerateAnswer')));
      return;
    }
    cards.forEach(card => {
      const stale = card.status === 'stale';
      listHost.append(el('div', { class: 'pl-card-row', 'data-card-id': String(card.id) },
        el('div', { class: 'pl-card-row-main' },
          el('div', { class: 'row pl-card-row-title' },
            el('strong', { 'data-no-i18n': '' }, card.title || t('paper.card.answerSheet_2', { v1: card.id })),
            statusBadge(card.status),
            card.template_signature ? badge(t('paper.card.signature', { v1: card.template_signature }), '') : null),
          el('p', { class: 'help' }, cardInfoText(card)),
          stale
            ? notice(t('paper.card.thePaperStructureChangedAndThisAnswer'), 'warning')
            : null),
        el('div', { class: 'row pl-card-row-actions' },
          button(t('paper.card.preview'), () => onOpenCard(card.id), 'soft small', 'eye'),
          stale
            ? button(t('paper.card.regenerate'), () => openForm(true), 'small', 'repeat')
            : null,
          button(t('common.delete'), async event => {
            if (!window.confirm(t('paper.card.deleteAnswerSheetThisCannotBeUndone', { v1: card.title || card.id }))) return;
            await busy(event.currentTarget, async () => {
              try {
                await api(`/paperlab/cards/${card.id}`, { method: 'DELETE' });
                toast(t('paper.card.answerSheetDeleted'));
                await reload();
              } catch (err) {
                toast(err.message || t('mybank.deleteFailedPleaseTryAgain'), true);
              }
            });
          }, 'ghost small', 'trash'))));
    });
  }

  async function reload() {
    try {
      const data = await api(`/paperlab/papers/${paperId}/cards`);
      if (!current(version)) return;
      cards = Array.isArray(data.items) ? data.items : [];
      renderList();
    } catch (error) {
      errorHost.replaceChildren(notice(error.message || t('paper.card.failedToRefreshTheAnswerSheetList'), 'error'));
    }
  }

  function openForm(regenerate = false) {
    formHost.hidden = false;
    renderForm(regenerate);
    formHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function renderForm(regenerate = false) {
    const titleInput = input(t('paper.card.answerSheetTitleOptionalDefaultsToThe'), { maxlength: '120' });
    const presetSelect = select(PRESETS, 'A4');
    const columnsSelect = select(COLUMN_OPTIONS, '2');
    const bubbleSelect = select(BUBBLE_OPTIONS, 'circle');
    const includeSubjective = el('input', { type: 'checkbox', checked: true });
    const studentDigits = select([
      { value: '6', label: t('paper.card.6DigitStudentId') }, { value: '8', label: t('paper.card.8DigitStudentId') },
    ], '6');
    const studentBubble = el('input', { type: 'checkbox', checked: true });
    const submitBtn = button(regenerate ? t('paper.card.regenerateWithNewSettings') : t('paper.card.generateAnswerSheet'), null, '', 'check');
    const formError = el('div', { class: 'pl-card-form-error' });
    const hasObjective = objectiveCount > 0;

    includeSubjective.addEventListener('change', () => {
      syncSubmit();
    });
    function syncSubmit() {
      const wouldBeEmpty = !hasObjective && !includeSubjective.checked;
      submitBtn.disabled = wouldBeEmpty || !paperItems.length;
      submitBtn.title = !paperItems.length
        ? t('paper.card.thePaperHasNoQuestionsComposeIt')
        : wouldBeEmpty ? t('paper.card.thePaperHasNoObjectiveQuestionsTick') : '';
    }
    syncSubmit();

    submitBtn.addEventListener('click', () => busy(submitBtn, async () => {
      formError.replaceChildren();
      const payload = {
        preset: presetSelect.value,
        columns: Number(columnsSelect.value) || 2,
        bubble: bubbleSelect.value,
        include_subjective: includeSubjective.checked,
        student_no: {
          digits: Number(studentDigits.value) || 6,
          bubble: studentBubble.checked,
        },
      };
      if (titleInput.value.trim()) payload.title = titleInput.value.trim();
      try {
        const created = await api(`/paperlab/papers/${paperId}/cards`, {
          method: 'POST', data: payload,
        });
        toast(regenerate ? t('paper.card.answerSheetGeneratedWithTheNewSettings') : t('paper.card.answerSheetGeneratedOpeningThePreview'));
        formHost.hidden = true;
        onOpenCard(created.card.id);
      } catch (error) {
        // 空卷/几何校验失败等后端 422 中文原因原样展示
        formError.replaceChildren(notice(error.message || t('paper.card.failedToGenerateTheAnswerSheetAdjust'), 'error'));
        throw error;
      }
    }));

    formHost.replaceChildren(el('section', { class: 'card pl-card-form' },
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, regenerate ? t('paper.card.regenerateAnswerSheet') : t('paper.card.generateAnswerSheet')),
          el('p', { class: 'help' },
            t('paper.card.currentPaperQuestionsObjectiveSubjective', { v1: paperItems.length, v2: objectiveCount, v3: subjectiveCount })
            + t('paper.card.answerSheetsAreLaidOutFromThe'))),
        button(t('question.collapse'), () => { formHost.hidden = true; }, 'ghost small', 'close')),
      el('div', { class: 'pl-card-form-grid' },
        field(t('notes.title'), titleInput),
        field(t('paper.card.paper_2'), presetSelect),
        field(t('paper.card.columns_2'), columnsSelect),
        field(t('paper.card.bubbleShape'), bubbleSelect),
        field(t('paper.card.studentIdDigits'), studentDigits),
        field(t('paper.card.studentIdMarking'), el('label', { class: 'pl-card-check' }, studentBubble,
          el('span', {}, t('paper.card.10BubblesPerDigitOffHandwritingLine')))),
        field(t('paper.card.withSubjectiveWritingArea'), el('label', { class: 'pl-card-check' }, includeSubjective,
          el('span', {}, t('paper.card.estimate314RuledLinesByScore')))),
      ),
      formError,
      el('div', { class: 'row end' },
        button(t('common.cancel'), () => { formHost.hidden = true; }, 'secondary'),
        submitBtn)));
  }

  mount.replaceChildren(el('div', { class: 'pl-card-section' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h3', {}, t('paper.document.answerSheet')),
        el('p', { class: 'help' }, t('paper.card.generatesA4A3AnswerSheetsFromThe'))),
      el('div', { class: 'row' },
        button(t('paper.card.generateAnswerSheet'), () => openForm(false), '', 'plus'),
        button(t('mybank.refreshList'), event => busy(event.currentTarget, reload), 'secondary small', 'repeat'))),
    !paperItems.length
      ? notice(t('paper.card.thePaperHasNoQuestionsSoNo'), 'warning')
      : null,
    errorHost,
    formHost,
    listHost));
  renderList();
}

/* ---------------------------------------------------------------------------
 * 答题卡预览 / 编辑（全屏工作台）
 * ------------------------------------------------------------------------- */

export async function renderCardStudio({ mount, paperId, version, cardId, onExit }) {
  mount.replaceChildren(loading(t('paper.card.loadingAnswerSheets')));
  let card = null;
  let layout = null;
  try {
    const data = await api(`/paperlab/cards/${cardId}`);
    card = data.card;
    layout = data.layout || {};
  } catch (error) {
    if (!current(version)) return;
    mount.replaceChildren(
      notice(error.message || t('paper.card.failedToLoadTheAnswerSheetPlease'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderCardStudio({ mount, paperId, version, cardId, onExit }), '', 'repeat'),
        button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'secondary')));
    return;
  }
  if (!current(version)) return;

  const ui = { card, layout, page: 1, zoom: 1, color: 'black', editing: false };
  const totalPages = () => Math.max(1, Number(ui.card.page_count) || (ui.layout.pages || []).length || 1);
  const pageMeta = () => ((ui.layout.pages || [])[ui.page - 1] || {});
  const pageCode = () => {
    const footer = pageMeta().footer || {};
    const cell = footer.page_code || {};
    return String(cell.text || '');
  };

  const headHost = el('div', { class: 'pl-card-studio-head' });
  const toolbarHost = el('div', { class: 'pl-card-toolbar' });
  const canvasHost = el('div', { class: 'pl-card-canvas' });
  const infoHost = el('div', { class: 'pl-card-info' });
  const noticeHost = el('div', { class: 'pl-card-notice' });
  const editHost = el('div', { class: 'pl-card-edit-host' });

  function renderHead() {
    headHost.replaceChildren(
      el('div', { class: 'row' },
        button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'ghost small', 'chevron'),
        el('div', {},
          el('h2', { class: 'pl-card-studio-title', 'data-no-i18n': '' }, ui.card.title || t('paper.card.answerSheet_2', { v1: ui.card.id })),
          el('p', { class: 'help' }, cardInfoText(ui.card)))),
      el('div', { class: 'row' },
        statusBadge(ui.card.status),
        button(t('settings.edit'), () => { ui.editing = !ui.editing; renderEdit(); renderToolbar(); }, 'secondary small', 'edit'),
        button(t('common.delete'), async event => {
          if (!window.confirm(t('paper.card.deleteThisAnswerSheetThisCannotBe'))) return;
          await busy(event.currentTarget, async () => {
            try {
              await api(`/paperlab/cards/${ui.card.id}`, { method: 'DELETE' });
              toast(t('paper.card.answerSheetDeleted'));
              onExit();
            } catch (error) {
              toast(error.message || t('mybank.deleteFailedPleaseTryAgain'), true);
            }
          });
        }, 'ghost small', 'trash')));
  }

  function renderNotice() {
    noticeHost.replaceChildren();
    if (ui.card.status === 'stale') {
      noticeHost.append(notice(
        t('paper.card.thePaperChangedRegenerateTheAnswerSheet'), 'warning'));
      noticeHost.append(el('div', { class: 'row' },
        button(t('paper.card.regenerateWithTheSameSettings'), async event => busy(event.currentTarget, async () => {
          await regenerateCard({ preset: ui.card.preset, orientation: ui.card.orientation,
            columns: ui.card.columns, bubble: ui.card.bubble,
            include_subjective: ui.card.include_subjective,
            student_no: (ui.layout.student_no || { digits: 6, bubble: true }) });
        }), 'soft small', 'repeat')));
    }
    noticeHost.append(notice(t('paper.card.answerSheetsOnlyGenerateATemplateAnd')));
  }

  function zoomIndex() {
    return ZOOM_STEPS.findIndex(value => Math.abs(value - ui.zoom) < 0.001);
  }
  function setZoom(next) {
    const clamped = Math.min(ZOOM_STEPS[ZOOM_STEPS.length - 1], Math.max(ZOOM_STEPS[0], next));
    ui.zoom = ZOOM_STEPS.reduce((best, value) =>
      (Math.abs(value - clamped) < Math.abs(best - clamped) ? value : best), ZOOM_STEPS[0]);
    renderCanvas();
    renderToolbar();
  }

  function renderToolbar() {
    const pages = totalPages();
    const colorSelect = select([
      { value: 'black', label: t('paper.card.blackPrint') }, { value: 'red', label: t('paper.card.redPrint') },
    ], ui.color);
    colorSelect.addEventListener('change', () => {
      ui.color = colorSelect.value;
      renderCanvas();
    });
    toolbarHost.replaceChildren(
      el('div', { class: 'row pl-card-pager' },
        button(t('common.prevPage'), () => { if (ui.page > 1) { ui.page -= 1; renderCanvas(); renderToolbar(); } },
          'secondary small', 'chevron', { disabled: ui.page <= 1 }),
        el('span', { class: 'pl-card-page-indicator' }, t('common.pageOf', { page: ui.page, pages: pages })),
        button(t('common.nextPage'), () => { if (ui.page < pages) { ui.page += 1; renderCanvas(); renderToolbar(); } },
          'secondary small', 'chevron', { disabled: ui.page >= pages })),
      el('div', { class: 'row pl-card-zoom' },
        button(t('mindmap.zoomOut'), () => setZoom(ZOOM_STEPS[Math.max(0, zoomIndex() - 1)]), 'ghost small', '', { title: t('mindmap.zoomOut') }),
        el('span', { class: 'pl-card-zoom-label' }, `${Math.round(ui.zoom * 100)}%`),
        button(t('mindmap.zoomIn'), () => setZoom(ZOOM_STEPS[Math.min(ZOOM_STEPS.length - 1, zoomIndex() + 1)]),
          'ghost small', ''),
        button(t('paper.card.fitWidth'), () => setZoom(1), 'ghost small', '')),
      el('div', { class: 'row pl-card-tools' },
        colorSelect,
        button(t('paper.card.printAnswerSheet'), () => printCard(), '', 'printer'),
        button(t('paper.card.downloadThisPageSvg'), () => downloadPage(), 'secondary small', 'download'),
        button(t('paper.card.downloadAllPages'), () => downloadAll(), 'secondary small', 'download'),
        button(t('paper.card.downloadPdf'), async event => busy(event.currentTarget, async () => {
          try {
            await downloadCardPdf(ui.card);
            toast(t('paper.card.answerSheetPdfDownloadStarted'));
          } catch (error) {
            toast(t('paper.card.youCanUsePrintAnswerSheetAnd', {
              v1: error.message || t('paper.document.pdfExportFailed'),
            }), true);
            throw error;
          }
        }), 'secondary small', 'download')));
  }

  function renderCanvas() {
    const cardData = ui.card;
    const wrapper = el('div', { class: 'pl-card-preview-scroll' });
    const img = el('img', {
      class: 'pl-card-preview-img',
      src: svgUrl(cardData, ui.page, ui.color, cardData.version),
      alt: t('paper.card.answerSheetPage', { v1: ui.page }),
      style: `width:${Math.round(ui.zoom * 100)}%`,
    });
    img.addEventListener('error', () => {
      wrapper.replaceChildren(notice(t('paper.card.failedToLoadTheAnswerSheetPreview'), 'error'));
    });
    wrapper.append(img);
    canvasHost.replaceChildren(wrapper);
    renderInfo();
  }

  function renderInfo() {
    const meta = pageMeta();
    const anchors = Array.isArray(meta.anchors) ? meta.anchors : [];
    const footer = meta.footer || {};
    const pageText = String((footer.page_text || {}).text || '');
    const widthMm = (ui.layout.paper || {}).width_mm;
    const heightMm = (ui.layout.paper || {}).height_mm;
    infoHost.replaceChildren(
      el('p', { class: 'help', 'data-no-i18n': '' },
        t('paper.card.pageSizeMm', {
          v1: widthMm || '—',
          v2: heightMm || '—',
          v3: pageText || t('result.page', { v1: ui.page }),
        })),
      el('p', { class: 'help' }, anchors.length
        ? t('paper.card.fiducials125mmTheScannerAppliesFour', { v1: anchors.map(anchor => anchor.corner).join(' / ') })
        : t('paper.card.fiducialsNoneOnThisPageHistoricalTemplate')),
      el('p', { class: 'help pl-card-code' },
        t('paper.card.pageCode'),
        el('code', {}, pageCode() || '—'),
        t('paper.card.templateSignature', { v1: ui.card.template_signature || (ui.layout.template || {}).signature || '—' })));
  }

  async function downloadPage() {
    try {
      const blob = await fetchBlob(svgUrl(ui.card, ui.page, ui.color, ui.card.version));
      downloadBlob(blob, cardFilename(ui.card, ui.page));
      toast(t('paper.card.thisPageSSvgDownloadStarted'));
    } catch (error) {
      toast(error.message || t('paper.card.svgDownloadFailedPleaseTryAgain'), true);
    }
  }

  async function downloadAll() {
    const pages = totalPages();
    try {
      for (let page = 1; page <= pages; page += 1) {
        const blob = await fetchBlob(svgUrl(ui.card, page, ui.color, ui.card.version));
        downloadBlob(blob, cardFilename(ui.card, page));
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      toast(t('paper.card.downloadedPagesOfSvg', { v1: pages }));
    } catch (error) {
      toast(error.message || t('paper.card.svgDownloadFailedPleaseTryAgain'), true);
    }
  }

  function printCard() {
    const frame = el('iframe', {
      class: 'pl-card-print-frame',
      title: t('paper.card.answerSheetPrintPreview'),
      src: printUrl(ui.card, ui.color),
      'aria-hidden': 'true',
    });
    frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
    const cleanup = () => { try { frame.remove(); } catch { /* ignore */ } };
    frame.addEventListener('load', () => {
      try {
        const win = frame.contentWindow;
        win.focus();
        win.print();
        toast(t('paper.card.theBrowserPrintDialogIsOpenChoose'));
      } catch (error) {
        toast(t('paper.card.cannotOpenThePrintDialogAutomaticallyOpen', {
          v1: error.message || t('paper.document.browserRestriction'),
        }), true);
      }
      setTimeout(cleanup, PRINT_FRAME_REMOVE_MS);
    });
    frame.addEventListener('error', () => {
      toast(t('paper.document.failedToLoadThePrintPagePlease'), true);
      cleanup();
    });
    document.body.append(frame);
  }

  async function patchCard(payload) {
    const response = await api(`/paperlab/cards/${ui.card.id}`, {
      method: 'PATCH', data: { version: ui.card.version, ...payload },
    });
    ui.card = response.card;
    ui.layout = response.layout || ui.layout;
    if (ui.page > totalPages()) ui.page = totalPages();
    renderHead();
    renderNotice();
    renderToolbar();
    renderCanvas();
  }

  async function regenerateCard(settings) {
    if (!window.confirm(t('paper.card.thisRegeneratesAnAnswerSheetFromThe'))) return;
    try {
      const created = await api(`/paperlab/papers/${ui.card.paper_id}/cards`, {
        method: 'POST',
        data: {
          preset: settings.preset || 'A4',
          orientation: settings.orientation || undefined,
          columns: settings.columns || 2,
          bubble: settings.bubble || 'circle',
          include_subjective: settings.include_subjective !== false,
          student_no: settings.student_no || { digits: 6, bubble: true },
          title: ui.card.title,
        },
      });
      try {
        await api(`/paperlab/cards/${ui.card.id}`, { method: 'DELETE' });
      } catch { /* 新卡已生成，旧卡删除失败不阻塞 */ }
      toast(t('paper.card.answerSheetRegenerated'));
      onExit({ regeneratedCardId: created.card.id });
    } catch (error) {
      noticeHost.replaceChildren(notice(
        t('paper.card.regenerationFailed', {
          v1: error.message || t('paper.card.regenerateFailedFallback'),
        }), 'error'));
      throw error;
    }
  }

  function renderEdit() {
    editHost.replaceChildren();
    if (!ui.editing) return;
    const titleInput = input(t('paper.card.answerSheetTitle'), { value: ui.card.title || '', maxlength: '120' });
    const saveTitle = button(t('paper.card.saveTitle'), async event => busy(event.currentTarget, async () => {
      try {
        await patchCard({ title: titleInput.value.trim() || ui.card.title });
        toast(t('paper.card.answerSheetTitleSaved'));
        ui.editing = false;
        renderEdit();
        renderToolbar();
      } catch (error) {
        const message = error.message || t('paper.question.saveFailedPleaseTryAgain');
        editHost.append(notice(message, 'error'));
        if (/版本冲突|已被修改/.test(message)) {
          editHost.append(el('div', { class: 'row' },
            button(t('paper.question.reloadTheLatestVersion'), async () => {
              try {
                const data = await api(`/paperlab/cards/${ui.card.id}`);
                ui.card = data.card;
                ui.layout = data.layout || ui.layout;
                renderHead();
                renderNotice();
                renderToolbar();
                renderCanvas();
                renderEdit();
                toast(t('paper.card.latestVersionLoaded'));
              } catch (reloadError) {
                toast(reloadError.message || t('common.loadFailed'), true);
              }
            }, 'soft small', 'repeat')));
        }
        throw error;
      }
    }), '', 'check');

    // 版面参数变化不能用 PATCH 局部重算（几何坐标需服务端重建），采用重新生成。
    const currentStudentNo = ui.layout.student_no || { digits: 6, bubble: true };
    const presetSelect = select(PRESETS, ui.card.preset || 'A4');
    const columnsSelect = select(COLUMN_OPTIONS, String(ui.card.columns || 2));
    const bubbleSelect = select(BUBBLE_OPTIONS, ui.card.bubble || 'circle');
    const includeSubjective = el('input', { type: 'checkbox', checked: ui.card.include_subjective !== false });
    const studentDigits = select([
      { value: '6', label: t('paper.card.6DigitStudentId') }, { value: '8', label: t('paper.card.8DigitStudentId') },
    ], String(currentStudentNo.digits || 6));
    const studentBubble = el('input', { type: 'checkbox', checked: currentStudentNo.bubble !== false });
    const regenerateBtn = button(t('paper.card.regenerateWithNewSettings'), null, 'secondary', 'repeat');
    regenerateBtn.addEventListener('click', () => busy(regenerateBtn, () => regenerateCard({
      preset: presetSelect.value,
      columns: Number(columnsSelect.value) || 2,
      bubble: bubbleSelect.value,
      include_subjective: includeSubjective.checked,
      student_no: { digits: Number(studentDigits.value) || 6, bubble: studentBubble.checked },
    })));

    editHost.append(el('section', { class: 'card pl-card-edit' },
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('paper.card.editAnswerSheet')),
          el('p', { class: 'help' }, t('paper.card.theTitleSavesDirectlyVersionCasPaper'))),
        button(t('question.collapse'), () => { ui.editing = false; renderEdit(); renderToolbar(); }, 'ghost small', 'close')),
      el('div', { class: 'pl-card-form-grid' },
        field(t('paper.card.titleSavedViaPatch'), titleInput),
        field(t('paper.card.paperRegenerate'), presetSelect),
        field(t('paper.card.columnsRegenerate'), columnsSelect),
        field(t('paper.card.bubblesRegenerate'), bubbleSelect),
        field(t('paper.card.studentIdDigitsRegenerate'), studentDigits),
        field(t('paper.card.studentIdMarkingRegenerate'), el('label', { class: 'pl-card-check' }, studentBubble,
          el('span', {}, t('paper.card.10BubblesPerDigit')))),
        field(t('paper.card.includeSubjectiveQuestionsRegenerate'), el('label', { class: 'pl-card-check' }, includeSubjective,
          el('span', {}, t('paper.card.estimateWritingAreaByScore'))))),
      el('div', { class: 'row end' }, saveTitle, regenerateBtn)));
  }

  mount.replaceChildren(el('section', { class: 'card pl-card-studio' },
    headHost, noticeHost, toolbarHost, canvasHost, infoHost, editHost));
  renderHead();
  renderNotice();
  renderToolbar();
  renderCanvas();
  renderEdit();
}

