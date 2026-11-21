/* 试卷工坊 · 答题卡扫描识别（OMR，个人自测）：上传多页照片 → 识别预览 → 人工纠错 → 确认判分。
 *
 * 只使用既有设施：
 * - POST /api/paperlab/papers/{id}/scan（multipart files[] + card_id/threshold）→ 生成扫描记录；
 * - GET /api/paperlab/scans/{id} / POST /scans/{id}/confirm / GET /papers/{id}/scans；
 * - GET /api/paperlab/papers/{id}/cards 选择答题卡；GET /api/paperlab/scan/meta 读取上限与阈值；
 * - 识别不确定项（页面码不符/缺定位点/气泡冲突/填涂不清晰）由后端标 unreadable 并给中文原因，
 *   前端只做「人工选择/标记未作答」，不替后端猜答案；确认前不展示正式成绩。
 * - 上传图片不落服务端：本会话用本地 File 生成 object URL 做 canvas 叠加（定位点/页面码/选中气泡），
 *   从历史打开时展示结构化识别结果（无原图，逐项人工改选仍可用）。
 */
import { api } from '../core/api.js';
import { onSessionReset } from '../core/state.js';
import {
  el, button, badge, notice, empty, loading, busy, toast, number, dateText,
} from '../core/dom.js';
import { current } from '../core/router.js';
import { t } from '../core/i18n.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TYPE_LABELS = {
  get SINGLE() { return t('question.type.single_choice'); },
  get MULTIPLE() { return t('question.type.multi_choice'); },
  get BLANK() { return t('question.type.fill_blank'); },
  get SUBJECTIVE() { return t('mybank.subjective'); },
  get MIXED() { return t('mybank.mixed'); },
};
const SCAN_STATUS_META = {
  get recognized() { return [t('assistant.pendingConfirmation'), 'amber']; },
  get confirmed() { return [t('paper.scan.confirmed'), 'teal']; },
  get unreadable() { return [t('paper.scan.unrecognized'), 'red']; },
};
const PAGE_STATUS_LABELS = {
  get ok() { return t('paper.scan.recognitionSucceeded'); },
  get unreadable() { return t('home.importStatus.failed'); },
};
const OPTION_LIMIT = 8;
const FALLBACK_META = { max_pages: 10, max_page_mb: 30, threshold: 0.45 };
const ACCEPT_TYPES = 'image/jpeg,image/png,image/webp';

let metaCache = null;
let studioState = null;

onSessionReset(() => {
  metaCache = null;
  studioState = null;
});

function typeLabel(type) {
  return TYPE_LABELS[String(type || '').toUpperCase()] || t('physics.question');
}

function statusBadge(status) {
  const meta = SCAN_STATUS_META[status] || [status || t('tasks.unknownStatus'), ''];
  return badge(meta[0], meta[1]);
}

function scoreText(score, maxScore) {
  return t('paper.attempt.pts', { v1: number(score), v2: number(maxScore) });
}

async function scanMeta() {
  if (!metaCache) {
    try {
      metaCache = await api('/paperlab/scan/meta');
    } catch {
      metaCache = { ...FALLBACK_META };
    }
  }
  return metaCache;
}

function formatMb(bytes) {
  return `${(Number(bytes || 0) / (1024 * 1024)).toFixed(1)}MB`;
}

/* ---------------------------------------------------------------------------
 * 试卷详情里的「答题卡扫描」区块：入口 + 扫描历史
 * ------------------------------------------------------------------------- */

export async function renderScanSection({ mount, paperId, version, onOpenScan }) {
  mount.replaceChildren(loading(t('paper.scan.loadingScanRecords')));
  let cards = [];
  let scans = [];
  let loadError = '';
  try {
    const [cardsData, listData, meta] = await Promise.all([
      api(`/paperlab/papers/${paperId}/cards`).catch(() => ({ items: [] })),
      api(`/paperlab/papers/${paperId}/scans`).catch(() => ({ items: [] })),
      scanMeta(),
    ]);
    cards = Array.isArray(cardsData.items) ? cardsData.items : [];
    scans = Array.isArray(listData.items) ? listData.items : [];
    if (!meta) throw new Error(t('paper.scan.failedToLoadScanningCapabilityInfo'));
  } catch (error) {
    loadError = error.message || t('paper.scan.failedToLoadScanRecordsPleaseTry');
  }
  if (!current(version)) return;
  if (loadError) {
    mount.replaceChildren(
      notice(loadError, 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderScanSection({ mount, paperId, version, onOpenScan }), '', 'repeat')));
    return;
  }
  const usableCards = cards.filter(card => card.status !== 'stale');
  const listHost = el('div', { class: 'pl-scan-history' });
  const meta = await scanMeta();
  const startBtn = button(t('paper.document.scanAnswerSheet'), () => onOpenScan(null), '', 'scan', {
    disabled: !usableCards.length,
    title: usableCards.length ? '' : t('paper.scan.thisPaperHasNoUsableAnswerSheet'),
  });

  function renderList() {
    listHost.replaceChildren();
    if (!usableCards.length) {
      listHost.append(notice(
        cards.length ? t('paper.scan.theExistingAnswerSheetIsInvalidatedBy')
          : t('paper.scan.thisPaperHasNoAnswerSheetYet'), 'warning'));
    }
    if (!scans.length) {
      listHost.append(el('p', { class: 'help' },
        t('paper.scan.noScanRecordsYetPrintAndFill')));
      return;
    }
    listHost.append(el('h4', { class: 'pl-scan-history-title' }, t('paper.scan.scanHistoryScans', { v1: scans.length })));
    for (const entry of scans.slice(0, 8)) {
      const summary = entry.summary || {};
      listHost.append(el('div', { class: 'pl-scan-history-item', 'data-scan-id': String(entry.id) },
        el('div', { class: 'pl-scan-history-main' },
          el('div', { class: 'row pl-scan-history-head' },
            el('strong', {}, scoreText(entry.score, entry.max_score)),
            statusBadge(entry.status),
            entry.student_no ? badge(t('paper.scan.studentId', { v1: entry.student_no }), '') : null),
          el('p', { class: 'help' }, [
            dateText(entry.created_at),
            t('common.countPages', { count: entry.page_count }),
            summary.readable_page_count != null
              ? t('paper.scan.pagesRecognized', { v1: summary.readable_page_count }) : null,
            summary.correct_count != null ? t('paper.attempt.correct_2', { v1: summary.correct_count }) : null,
            summary.pending_count ? t('paper.scan.questionsAwaitingManualConfirmation', { v1: summary.pending_count }) : null,
          ].filter(Boolean).join(' · '))),
        entry.status === 'confirmed'
          ? button(t('paper.scan.viewScore'), () => onOpenScan(entry.id), 'soft small', 'file')
          : button(t('paper.scan.continueCorrecting'), () => onOpenScan(entry.id), 'small', 'edit')));
    }
  }

  mount.replaceChildren(el('div', { class: 'pl-scan-section' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h3', {}, t('paper.scan.answerSheetScanRecognitionOmr')),
        el('p', { class: 'help' },
          t('paper.scan.uploadMultiPageAnswerSheetPhotosUp', { v1: meta.max_pages, v2: meta.max_page_mb })
          + t('paper.scan.pageCodesObjectiveMarksAndTheStudent'))),
      el('div', { class: 'row' },
        button(t('mybank.refreshList'), event => busy(event.currentTarget, async () => {
          await renderScanSection({ mount, paperId, version, onOpenScan });
        }), 'secondary small', 'repeat'),
        startBtn)),
    !usableCards.length ? null : el('p', { class: 'help' },
      t('paper.scan.answerSheetsAvailableTheMostRecentOne', { v1: usableCards.length, v2: meta.threshold })),
    listHost));
  renderList();
}

/* ---------------------------------------------------------------------------
 * 扫描工作台：上传 → 识别预览/纠错 → 确认成绩
 * ------------------------------------------------------------------------- */

export async function renderScanStudio({ mount, paperId, version, scanId = null, onExit }) {
  const prev = studioState;
  if (prev && prev.previewUrls) prev.previewUrls.forEach(url => URL.revokeObjectURL(url));
  studioState = {
    mount,
    version,
    onExit,
    paperId,
    scanId: scanId || null,
    mode: 'loading',
    payload: null,
    cards: [],
    cardId: null,
    meta: { ...FALLBACK_META },
    files: [],
    previews: [],
    previewUrls: [],
    answers: {},
    touched: new Set(),
    busy: false,
    error: '',
  };
  mount.replaceChildren(loading(scanId ? t('paper.scan.loadingScanRecords') : t('paper.card.loadingAnswerSheets')));
  try {
    const [cardsData, meta] = await Promise.all([
      api(`/paperlab/papers/${paperId}/cards`).catch(() => ({ items: [] })),
      scanMeta(),
    ]);
    studioState.cards = (Array.isArray(cardsData.items) ? cardsData.items : [])
      .filter(card => card.status !== 'stale');
    studioState.meta = meta || { ...FALLBACK_META };
    studioState.cardId = studioState.cards.length ? studioState.cards[0].id : null;
  } catch (error) {
    if (!current(version)) return;
    mount.replaceChildren(notice(error.message || t('paper.scan.failedToLoadTheScanWorkbenchPlease'), 'error'),
      el('div', { class: 'row' }, button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'secondary')));
    return;
  }
  if (!current(version)) return;
  if (scanId) {
    await loadScan(scanId, mount, version, onExit);
    return;
  }
  studioState.mode = 'upload';
  render();
}

async function loadScan(scanId, mount, version, onExit) {
  try {
    const payload = await api(`/paperlab/scans/${scanId}`);
    if (!current(version)) return;
    studioState.scanId = Number(payload.scan?.id || scanId);
    studioState.payload = payload;
    initAnswers(payload);
    studioState.mode = payload.scan?.status === 'confirmed' ? 'result' : 'review';
  } catch (error) {
    if (!current(version)) return;
    studioState.mode = 'fatal';
    studioState.error = error.message || t('paper.scan.failedToLoadScanRecordsPleaseTry');
  }
  render();
}

function initAnswers(payload) {
  const state = studioState;
  state.answers = {};
  state.touched = new Set();
  const questions = payload?.result?.questions || [];
  for (const question of questions) {
    state.answers[question.item_position] = Array.isArray(question.selected)
      ? [...question.selected] : [];
  }
  // 识别不确定项必须由用户显式确认（或标记未作答）后才能确认成绩。
  for (const entry of payload?.result?.unreadable_items || []) {
    state.answers[entry.item_position] = state.answers[entry.item_position] || [];
  }
}

function currentPayload() {
  return studioState?.payload || null;
}

function pageIssues() {
  const result = currentPayload()?.result || {};
  const pages = Array.isArray(result.pages) ? result.pages : [];
  const issues = [];
  for (const page of pages) {
    if (page.status !== 'ok') {
      issues.push(t('paper.scan.image', {
        v1: page.index,
        v2: page.reason || t('paper.scan.unrecognized'),
      }));
    }
  }
  if ((result.missing_pages || []).length) {
    issues.push(t('paper.scan.missingPage', { v1: result.missing_pages.join(t('common.listSeparator')) }));
  }
  if ((result.duplicate_pages || []).length) {
    issues.push(t('paper.scan.pageUploadedTwice', { v1: result.duplicate_pages.join(t('common.listSeparator')) }));
  }
  return issues;
}

function unresolvedItems() {
  const result = currentPayload()?.result || {};
  const unresolved = [];
  for (const entry of result.unreadable_items || []) {
    if (!studioState.touched.has(entry.item_position)) unresolved.push(entry);
  }
  return unresolved;
}

function revokePreviews() {
  const state = studioState;
  if (!state) return;
  state.previewUrls.forEach(url => URL.revokeObjectURL(url));
  state.previewUrls = [];
  state.previews = [];
  state.files = [];
}

function resetToUpload() {
  const state = studioState;
  if (!state) return;
  state.mode = 'upload';
  state.error = '';
  state.files = [];
  state.previews = [];
  revokePreviews();
  render();
}

function render() {
  const state = studioState;
  if (!state) return;
  const mount = state.mount;
  if (!mount) return;
  if (state.mode === 'loading') return;
  if (state.mode === 'fatal') {
    mount.replaceChildren(
      notice(state.error || t('paper.scan.failedToLoadScanRecordsPleaseTry'), 'error'),
      el('div', { class: 'row' },
        button(t('paper.attempt.backToPaperDetails'), () => state.onExit(), 'secondary')));
    return;
  }
  if (state.mode === 'upload') mount.replaceChildren(renderUpload());
  else if (state.mode === 'review') mount.replaceChildren(renderReview());
  else mount.replaceChildren(renderResult());
}

/* ------------------------------ 上传阶段 ------------------------------ */

function renderUpload() {
  const state = studioState;
  const card = state.cards.find(item => item.id === state.cardId) || null;
  const cardSelect = el('select', { class: 'input' });
  for (const item of state.cards) {
    cardSelect.append(el('option', { value: String(item.id) },
      t('paper.scan.pages', {
        v1: item.title || t('paper.card.answerSheet_2', { v1: item.id }),
        v2: item.page_count,
        v3: item.preset || 'A4',
      })));
  }
  cardSelect.value = state.cardId ? String(state.cardId) : '';
  cardSelect.addEventListener('change', () => {
    state.cardId = Number(cardSelect.value) || null;
  });

  const fileInput = el('input', {
    type: 'file',
    accept: ACCEPT_TYPES,
    multiple: true,
    class: 'pl-scan-file-input',
    'aria-label': t('paper.scan.chooseAnswerSheetPhotos'),
  });
  const fileLabel = el('label', { class: 'pl-scan-file-label' },
    el('span', { class: 'pl-scan-file-icon' }, '📷'),
    el('strong', {}, t('paper.scan.chooseAnswerSheetPhotos')),
    el('span', { class: 'help' }, t('paper.scan.supportsJpgPngWebpInAnswerSheet', { v1: state.meta.max_pages, v2: state.meta.max_page_mb })),
    fileInput);
  const previewHost = el('div', { class: 'pl-scan-preview-list' });
  const errorHost = el('div', { class: 'pl-scan-form-error' });
  const startBtn = button(t('paper.import.startRecognition'), null, '', 'scan', { disabled: !state.files.length });

  function syncFiles(files) {
    revokePreviews();
    const accepted = [];
    const errors = [];
    const limit = Number(state.meta.max_pages) || FALLBACK_META.max_pages;
    const maxBytes = (Number(state.meta.max_page_mb) || FALLBACK_META.max_page_mb) * 1024 * 1024;
    for (const file of files) {
      if (!/^image\/(jpeg|png|webp)$/i.test(file.type || '')) {
        errors.push(t('paper.scan.isNotAJpgPngWebpImage', { v1: file.name }));
        continue;
      }
      if (file.size > maxBytes) {
        errors.push(t('paper.scan.exceedsTheMbPerPageLimit', { v1: file.name, v2: formatMb(file.size), v3: state.meta.max_page_mb }));
        continue;
      }
      if (accepted.length >= limit) {
        errors.push(t('paper.scan.upToPagesPerScanExtraImages', { v1: limit }));
        break;
      }
      accepted.push(file);
    }
    state.files = accepted;
    state.previews = accepted.map(file => ({
      file,
      url: URL.createObjectURL(file),
      name: file.name,
    }));
    state.previewUrls = state.previews.map(item => item.url);
    startBtn.disabled = !accepted.length;
    errorHost.replaceChildren(...errors.map(message => notice(message, 'error')));
    previewHost.replaceChildren();
    state.previews.forEach((preview, index) => {
      previewHost.append(el('div', { class: 'pl-scan-preview-item' },
        el('img', { src: preview.url, alt: t('paper.scan.pagePreview', { v1: index + 1 }), loading: 'lazy' }),
        el('div', { class: 'pl-scan-preview-meta' },
          el('strong', {}, t('paper.scan.image_2', { v1: index + 1 })),
          el('span', { class: 'help' }, `${preview.name} · ${formatMb(preview.file.size)}`))));
    });
  }

  fileInput.addEventListener('change', () => syncFiles([...fileInput.files || []]));
  startBtn.addEventListener('click', () => busy(startBtn, startRecognition));

  const noticeHost = el('div', { class: 'pl-scan-notice' });
  if (!state.cards.length) {
    noticeHost.append(notice(
      t('paper.scan.thisPaperHasNoUsableAnswerSheet_2'), 'warning'));
  }
  if (card && card.status === 'stale') {
    noticeHost.append(notice(t('paper.scan.theSelectedAnswerSheetIsInvalidatedRegenerate'), 'warning'));
  }

  return el('section', { class: 'card pl-scan-studio' },
    el('div', { class: 'pl-scan-studio-head' },
      el('div', {},
        el('h2', { class: 'pl-scan-studio-title' }, t('paper.document.scanAnswerSheet')),
        el('p', { class: 'help' },
          t('paper.scan.photographThePrintedAndFilledInAnswer')
          + t('paper.scan.theSystemDetectsTheFourCornerFiducials'))),
      el('div', { class: 'row' },
        button(t('paper.attempt.backToPaperDetails'), () => state.onExit(), 'ghost small', 'chevron'))),
    noticeHost,
    el('div', { class: 'pl-scan-upload-grid' },
      el('div', { class: 'field' }, el('label', {}, t('paper.scan.chooseAnswerSheetTemplate')), cardSelect),
      el('div', { class: 'field' }, el('label', {}, t('paper.scan.takePhotoUploadImages')), fileLabel)),
    previewHost,
    errorHost,
    el('p', { class: 'help pl-scan-tip' },
      t('paper.scan.recognitionThresholdFailedOrUncertainQuestionsAre', { v1: state.meta.threshold })),
    el('div', { class: 'row end pl-scan-upload-actions' }, startBtn));
}

async function startRecognition() {
  const state = studioState;
  const mount = state.mount;
  if (!state.files.length) throw new Error(t('paper.scan.chooseAnswerSheetPhotosFirst'));
  if (!state.cardId) throw new Error(t('paper.scan.chooseAnAnswerSheetTemplateFirst'));
  const form = new FormData();
  state.files.forEach(file => form.append('files', file, file.name));
  form.append('card_id', String(state.cardId));
  const payload = await api(`/paperlab/papers/${state.paperId}/scan`, {
    method: 'POST', body: form,
  });
  state.payload = payload;
  state.scanId = Number(payload.scan?.id || 0);
  initAnswers(payload);
  state.mode = 'review';
  if (!current(state.version)) return;
  render();
}

/* ------------------------------ 预览 / 纠错阶段 ------------------------------ */

function renderReview() {
  const state = studioState;
  const payload = state.payload;
  const result = payload?.result || {};
  const scan = payload?.scan || {};
  const issues = pageIssues();
  const unresolved = unresolvedItems();
  const headBox = el('div', { class: 'pl-scan-studio-head' },
    el('div', {},
      el('h2', { class: 'pl-scan-studio-title' }, t('paper.scan.recognitionResultsAndManualConfirmation')),
      el('p', { class: 'help' },
        t('paper.scan.scanRecordPagesStudentId', { v1: scan.id, v2: result.page_count || 0 })
        + t('paper.scan.recognizedScoreFinalAfterConfirmation', { v1: studentNoText(result), v2: scoreText(scan.score, scan.max_score) }))),
    el('div', { class: 'row' },
      button(t('paper.scan.reUpload'), () => resetToUpload(), 'secondary small', 'repeat'),
      button(t('paper.attempt.backToPaperDetails'), () => state.onExit(), 'ghost small', 'chevron')));

  const noticeHost = el('div', { class: 'pl-scan-notice' });
  if (issues.length) {
    noticeHost.append(notice(
      t('paper.scan.somePagesWereNotRecognizedUseRe', { v1: issues.join(t('common.semicolonSeparator')) }),
      'error'));
  }
  const unresolvedText = unresolved.length
    ? t('paper.scan.questionsAreStillUncertainQuestionSelectAn', { v1: unresolved.length, v2: unresolved.map(item => item.item_position).join(t('common.listSeparator')) })
    : '';
  if (unresolvedText) noticeHost.append(notice(unresolvedText, 'warning'));
  if (!issues.length && (result.student_no?.conflict)) {
    noticeHost.append(notice(t('paper.scan.theStudentIdDiffersAcrossPagesCheck'), 'warning'));
  }

  const pagesHost = el('div', { class: 'pl-scan-pages' });
  const previewsByIndex = {};
  state.previews.forEach((preview, index) => { previewsByIndex[index + 1] = preview.url; });
  for (const page of result.pages || []) {
    pagesHost.append(renderPageCard(page, previewsByIndex[page.index]));
  }

  const correctionHost = el('div', { class: 'pl-scan-corrections' });
  const actionHost = el('div', { class: 'row end pl-scan-confirm-actions' });
  const blocked = issues.length > 0;

  function renderCorrections() {
    correctionHost.replaceChildren(
      el('h3', { class: 'pl-scan-section-title' }, t('paper.scan.checkQuestionByQuestionUncertainItemsAre')));
    const questions = result.questions || [];
    if (!questions.length) {
      correctionHost.append(el('p', { class: 'help' }, t('paper.scan.thisScanHasNoObjectiveQuestionsTo')));
      return;
    }
    correctionHost.append(...questions.map(question => renderQuestionRow(question, () => {
      renderCorrections();
      renderActions();
    })));
  }

  function renderActions() {
    const freshUnresolved = unresolvedItems();
    const confirmBtn = button(t('paper.scan.confirmScore'), null, '', 'check', {
      disabled: blocked || freshUnresolved.length > 0,
    });
    confirmBtn.addEventListener('click', () => busy(confirmBtn, confirmScan));
    actionHost.replaceChildren(
      el('span', { class: 'help pl-scan-confirm-hint' }, blocked
        ? t('paper.scan.notAllPagesWereRecognizedSoThe')
        : freshUnresolved.length
          ? t('paper.scan.resolveAllHighlightedUncertainItemsFirst')
          : t('paper.scan.afterConfirmationAnswersAreGradedAgainstThe')),
      confirmBtn);
  }

  const root = el('section', { class: 'card pl-scan-studio' },
    headBox, noticeHost,
    el('h3', { class: 'pl-scan-section-title' }, t('paper.scan.pageRecognitionStatus')),
    pagesHost,
    correctionHost, actionHost);
  renderCorrections();
  renderActions();
  return root;
}

function studentNoText(result) {
  const student = result?.student_no || {};
  if (student.value) return student.value;
  if (student.conflict) return t('paper.scan.inconsistentRecognition');
  return t('paper.scan.unrecognized_2');
}

function renderPageCard(page, previewUrl) {
  const status = String(page.status || 'unreadable');
  const header = el('div', { class: 'pl-scan-page-head' },
    el('strong', {}, t('paper.scan.image_2', { v1: page.index })),
    badge(PAGE_STATUS_LABELS[status] || status, status === 'ok' ? 'teal' : 'red'),
    page.page_no ? badge(t('paper.card.answerSheetPage', { v1: page.page_no }), '') : null,
    page.code_ok ? badge(t('paper.scan.pageCodeVerified'), 'teal') : null);
  const metaLine = el('p', { class: 'help pl-scan-page-meta' }, [
    page.image_width ? `${page.image_width}×${page.image_height}px` : null,
    page.code ? t('paper.scan.pageCode', { v1: String(page.code).slice(0, 12) }) : t('paper.scan.pageCodeNotDecoded'),
    page.code_quality ? t('paper.scan.separation', { v1: page.code_quality }) : null,
    page.anchors?.length ? t('paper.scan.fiducials4', { v1: page.anchors.length }) : t('paper.scan.fiducialsMissing'),
  ].filter(Boolean).join(' · '));
  const body = el('div', { class: 'pl-scan-page-body' });
  if (previewUrl) {
    body.append(el('div', { class: 'pl-scan-canvas-wrap' }, canvasFor(page, previewUrl)));
  } else if (page.anchors?.length) {
    body.append(el('p', { class: 'help' },
      t('paper.scan.historicalRecordsDoNotKeepTheOriginal')));
  }
  if (status !== 'ok') {
    const reasonNotice = notice(
      page.reason || t('paper.scan.thisPageCouldNotBeRecognizedPlease'), 'error');
    if (page.reason) reasonNotice.setAttribute('data-no-i18n', '');
    body.append(reasonNotice);
  }
  const items = (page.unreadable_items || [])
    .map(entry => t('paper.scan.question', { v1: entry.item_position, v2: entry.reason }))
    .join(t('common.semicolonSeparator'));
  if (items) body.append(el('p', { class: 'help pl-scan-page-unreadable' }, items));
  return el('article', { class: 'pl-scan-page' }, header, metaLine, body);
}

function canvasFor(page, previewUrl) {
  const canvas = el('canvas', { class: 'pl-scan-canvas' });
  const image = new Image();
  image.onload = () => {
    const naturalWidth = image.naturalWidth || page.image_width || 800;
    const targetWidth = Math.min(1100, naturalWidth);
    const scale = targetWidth / naturalWidth;
    canvas.width = Math.max(1, Math.round(naturalWidth * scale));
    canvas.height = Math.max(1, Math.round((image.naturalHeight || page.image_height || 1100) * scale));
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const pageWidth = Number(page.image_width) || naturalWidth;
    drawPageOverlay(ctx, page, canvas.width / pageWidth, canvas.width);
  };
  image.onerror = () => {
    canvas.replaceWith(notice(t('paper.scan.failedToLoadTheOriginalImagePreview'), 'warning'));
  };
  image.src = previewUrl;
  return canvas;
}

function drawPageOverlay(ctx, page, scale, canvasWidth) {
  const anchors = Array.isArray(page.anchors) ? page.anchors : [];
  const code = page.markers?.page_code || null;
  const lineWidth = Math.max(1, canvasWidth * 0.0022);
  ctx.save();
  ctx.lineWidth = lineWidth;
  // 页面码区域
  if (code && code.width) {
    ctx.strokeStyle = page.code_ok ? '#1f7a5c' : '#b34b48';
    ctx.setLineDash([Math.max(4, canvasWidth * 0.008), Math.max(3, canvasWidth * 0.005)]);
    ctx.strokeRect(code.x * scale, code.y * scale, code.width * scale, code.height * scale);
    ctx.setLineDash([]);
    ctx.fillStyle = page.code_ok ? '#1f7a5c' : '#b34b48';
    ctx.font = `${Math.max(11, Math.round(canvasWidth * 0.014))}px sans-serif`;
    ctx.fillText(page.code_ok ? t('paper.scan.pageCode_2') : t('paper.scan.pageCode_3'), code.x * scale, Math.max(14, code.y * scale - 4));
  }
  // 四角定位点
  const radius = Math.max(5, canvasWidth * 0.009);
  for (const anchor of anchors) {
    ctx.beginPath();
    ctx.arc(anchor.x * scale, anchor.y * scale, radius, 0, Math.PI * 2);
    ctx.strokeStyle = '#0f766e';
    ctx.lineWidth = lineWidth * 1.5;
    ctx.stroke();
  }
  // 选中 / 不确定气泡
  for (const bubble of page.bubbles || []) {
    const x = Number(bubble.img_x);
    const y = Number(bubble.img_y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    ctx.beginPath();
    ctx.arc(x * scale, y * scale, radius * 0.9, 0, Math.PI * 2);
    ctx.fillStyle = bubble.unreadable || bubble.ambiguous
      ? 'rgba(179,75,72,0.75)' : 'rgba(31,122,92,0.75)';
    ctx.fill();
  }
  // 页面状态角标
  const label = page.status === 'ok'
    ? t('paper.scan.pageRecognized', { v1: page.page_no || '?' })
    : t('paper.scan.imageRecognitionFailed', { v1: page.index });
  ctx.font = `bold ${Math.max(12, Math.round(canvasWidth * 0.016))}px sans-serif`;
  const textWidth = ctx.measureText(label).width + 16;
  ctx.fillStyle = page.status === 'ok' ? 'rgba(31,122,92,0.9)' : 'rgba(179,75,72,0.9)';
  ctx.fillRect(0, 0, textWidth, Math.max(22, canvasWidth * 0.03));
  ctx.fillStyle = '#ffffff';
  ctx.fillText(label, 8, Math.max(16, canvasWidth * 0.023));
  ctx.restore();
}

function renderQuestionRow(question, rerender) {
  const state = studioState;
  const position = question.item_position;
  const unreadable = (currentPayload()?.result?.unreadable_items || [])
    .some(entry => entry.item_position === position);
  const selected = state.answers[position] || [];
  const options = Array.isArray(question.options) ? question.options.slice(0, OPTION_LIMIT) : [];
  const touched = state.touched.has(position);
  const head = el('div', { class: 'pl-scan-question-head' },
    el('strong', {}, t('paper.import.question', { v1: position })),
    badge(typeLabel(question.type), 'blue'),
    badge(t('admission.scorePoints', { v1: number(question.score) }), ''),
    unreadable ? badge(t('paper.scan.uncertain'), 'red') : null,
    question.judgement && question.judgement !== 'pending'
      ? badge(question.judgement === 'correct' ? t('paper.scan.recognitionGradedCorrect') : t('paper.scan.recognitionGradedWrong'),
        question.judgement === 'correct' ? 'teal' : 'red')
      : null);
  const stem = String(question.stem || '').trim();
  const body = el('div', { class: 'pl-scan-question-body', 'data-no-i18n': '' });
  if (stem) body.append(el('p', { class: 'pl-scan-stem' }, stem));
  if (!options.length) {
    body.append(el('p', { class: 'help' }, t('paper.scan.thisQuestionHasNoBubblesItMay')));
  }
  const chips = el('div', { class: 'pl-scan-options' });
  for (const option of options) {
    const label = String(option.label || '').toUpperCase();
    const active = selected.includes(label);
    chips.append(el('button', {
      type: 'button',
      class: `pl-scan-option${active ? ' selected' : ''}${unreadable ? ' unreadable' : ''}`,
      'aria-pressed': active ? 'true' : 'false',
      onClick: () => {
        const current = state.answers[position] || [];
        let next;
        if (question.type === 'MULTIPLE') {
          next = current.includes(label)
            ? current.filter(item => item !== label)
            : [...current, label];
        } else {
          next = current.includes(label) ? [] : [label];
        }
        state.answers[position] = next;
        state.touched.add(position);
        rerender();
      },
    }, el('strong', {}, label), el('span', { class: 'pl-scan-option-text' }, String(option.text || ''))));
  }
  body.append(chips);
  const tools = el('div', { class: 'row pl-scan-question-tools' },
    el('span', { class: 'help' },
      unreadable
        ? (touched
          ? t('paper.scan.selectionManuallyConfirmed')
          : t('paper.scan.uncertainRecognition', {
            v1: question.reason || t('paper.scan.selectManuallyOrMarkUnanswered'),
          }))
        : (question.selected?.length ? t('paper.scan.recognizedReading', { v1: question.selected.join(t('common.listSeparator')) }) : t('paper.scan.recognitionResultUnansweredBlank'))),
    unreadable || touched
      ? button(t('paper.scan.markUnanswered'), () => {
        state.answers[position] = [];
        state.touched.add(position);
        rerender();
      }, 'ghost small', 'close')
      : null);
  return el('div', { class: `pl-scan-question${unreadable ? ' unreadable' : ''}` }, head, body, tools);
}

async function confirmScan() {
  const state = studioState;
  const payload = currentPayload();
  const result = payload?.result || {};
  const answers = {};
  // 只提交人工改过的题 + 全部识别不确定题（后端会补全其余已确定读数）。
  const unreadablePositions = new Set(
    (result.unreadable_items || []).map(entry => entry.item_position));
  for (const question of result.questions || []) {
    const position = question.item_position;
    if (!state.touched.has(position) && !unreadablePositions.has(position)) continue;
    answers[String(position)] = [...(state.answers[position] || [])];
  }
  const response = await api(`/paperlab/scans/${state.scanId}/confirm`, {
    method: 'POST', data: { answers },
  });
  state.payload = response;
  initAnswers(response);
  state.mode = 'result';
  toast(t('paper.scan.scanResultConfirmedAndScoreGenerated'));
  render();
}

/* ------------------------------ 成绩阶段 ------------------------------ */

function renderResult() {  const state = studioState;
  const payload = state.payload || {};
  const result = payload.result || {};
  const scan = payload.scan || {};
  const summary = result.summary || {};
  const accuracy = summary.accuracy != null ? `${(Number(summary.accuracy) * 100).toFixed(1)}%` : '—';
  const confirmed = scan.status === 'confirmed';
  const questions = result.questions || [];
  const wrong = questions.filter(question => question.judgement === 'incorrect');
  const pending = questions.filter(question => question.judgement === 'pending');

  const head = el('div', { class: 'pl-scan-studio-head' },
    el('div', {},
      el('h2', { class: 'pl-scan-studio-title' }, confirmed ? t('paper.scan.scanScore') : t('paper.scan.recognitionResultPendingConfirmation')),
      el('p', { class: 'help' },
        t('paper.scan.scanRecordPagesStudentId_2', { v1: scan.id, v2: result.page_count || 0, v3: studentNoText(result) })
        + `${dateText(scan.created_at)}`)),
    el('div', { class: 'row' },
      statusBadge(scan.status),
      button(t('paper.scan.scanAgain'), () => resetToUpload(), 'secondary small', 'scan'),
      button(t('paper.attempt.backToPaperDetails'), () => state.onExit(), 'ghost small', 'chevron')));

  const scoreBox = el('div', { class: 'pl-scan-score' },
    el('div', { class: 'pl-scan-score-main' },
      el('span', { class: 'pl-scan-score-value' }, number(scan.score)),
      el('span', { class: 'pl-scan-score-max' }, t('paper.scan.pts', { v1: number(scan.max_score) }))),
    el('div', { class: 'pl-scan-score-meta' },
      el('span', {}, t('insight.ai.snapAccuracy', { pct: accuracy })),
      el('span', {}, t('paper.attempt.correct_2', { v1: summary.correct_count || 0 })),
      el('span', {}, t('paper.attempt.wrong_2', { v1: summary.incorrect_count || 0 })),
      el('span', {}, t('paper.attempt.pendingReview', { v1: summary.pending_count || 0 }))));

  const noticeHost = el('div', { class: 'pl-scan-notice' });
  if (scan.status === 'unreadable') {
    noticeHost.append(notice(t('paper.scan.thisScanHasNoRecognizablePagesPlease'), 'error'));
  }
  if (payload.attempt) {
    noticeHost.append(notice(
      t('paper.scan.generatedPracticeRecordAnswerSheetScanObjective', { v1: payload.attempt.id })
      + t('paper.attempt.pts', { v1: number(result.score), v2: number(result.max_score) })
      + (payload.attempt.wrong_added?.length
        ? t('paper.scan.wrongAnswersAddedToTheWrongAnswer', { v1: payload.attempt.wrong_added.length })
        : t('paper.scan.viewItInTheFullPaperPractice')), ''));
  }
  if (pending.length) {
    noticeHost.append(notice(
      t('paper.scan.subjectiveQuestionsAreNotAutoGradedPending', { v1: pending.length })
      + t('paper.scan.scanningOnlyAutoGradesTheObjectiveQuestions'), ''));
  }

  const wrongHost = el('div', { class: 'pl-scan-wrong-list' });
  if (!wrong.length) {
    wrongHost.append(el('p', { class: 'help' }, t('paper.scan.noObjectiveQuestionsWereGradedWrong')));
  } else {
    wrongHost.append(el('h3', { class: 'pl-scan-section-title' }, t('paper.scan.wrongAnswerHints', { v1: wrong.length })));
    for (const question of wrong) {
      wrongHost.append(el('div', { class: 'pl-scan-wrong-item', 'data-no-i18n': '' },
        el('div', { class: 'pl-scan-question-head' },
          el('strong', {}, t('paper.import.question', { v1: question.item_position })),
          badge(typeLabel(question.type), 'blue'),
          badge(t('paper.scan.wrong'), 'red')),
        el('p', { class: 'help' },
          t('paper.scan.yourChoice', {
            v1: (question.selected || []).join(t('common.listSeparator')) || t('question.notAnswered'),
          })
          + t('paper.scan.correctAnswer', {
            v1: (question.reference_answers || []).join(t('common.listSeparator'))
              || t('paper.scan.snapshotNotProvided'),
          }))));
    }
  }

  const pagesHost = el('div', { class: 'pl-scan-pages' });
  for (const page of result.pages || []) {
    pagesHost.append(el('article', { class: 'pl-scan-page' },
      el('div', { class: 'pl-scan-page-head' },
        el('strong', {}, t('paper.scan.image_2', { v1: page.index })),
        badge(PAGE_STATUS_LABELS[page.status] || page.status, page.status === 'ok' ? 'teal' : 'red'),
        page.page_no ? badge(t('paper.card.answerSheetPage', { v1: page.page_no }), '') : null,
        page.code_ok ? badge(t('paper.scan.pageCodePassed'), 'teal') : null),
      el('p', { class: 'help' }, [
        page.code ? t('paper.scan.pageCode', { v1: String(page.code).slice(0, 16) }) : t('paper.scan.pageCodeNotDecoded'),
        page.code_quality ? t('paper.scan.separation', { v1: page.code_quality }) : null,
        page.student_no?.value ? t('paper.scan.studentId', { v1: page.student_no.value }) : t('paper.scan.studentIdNotRecognized'),
        (page.unreadable_items || []).length
          ? t('paper.scan.uncertainQuestionsManuallyConfirmed', { v1: page.unreadable_items.length }) : null,
      ].filter(Boolean).join(' · '))));
  }

  return el('section', { class: 'card pl-scan-studio' },
    head, noticeHost, scoreBox, wrongHost,
    el('h3', { class: 'pl-scan-section-title' }, t('paper.scan.perPageRecognitionStatus')),
    pagesHost,
    el('div', { class: 'row end pl-scan-upload-actions' },
      button(t('paper.scan.scanAgain'), () => resetToUpload(), 'secondary', 'scan'),
      button(t('paper.attempt.backToPaperDetails'), () => {
        state.onExit();
      }, '', 'file')));
}
