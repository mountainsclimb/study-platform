/* 试卷工坊 · 识别导入工作台组件。
 *
 * - createImportUploader：拖拽/点选多文件（jpg/png/webp/pdf/docx）→ POST /api/paperlab/imports；
 *   前端先校验单文件 ≤30MB、≤20 文件（页数由服务端按 PDF/DOCX 实际页数校验 ≤20 页）。
 * - renderImportDetail：导入详情（页图 + bbox 叠加 + 拖动/右下角缩放）、逐题草稿核验、
 *   识别任务（core/jobs.js mountJob：SSE 进度/取消/续跑）、批量提交入库。
 *
 * 全部使用既有设施，不引入第三方库；错误信息兼容后端 pydantic detail 数组（api.js 已处理）。
 */
import { api, refreshProviders } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, field, input, select, button, badge, notice, loading, empty, busy, toast, dateText,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { openQuestionEditor, typeLabel } from '../components/paperQuestionForm.js';
import { createBlockProofreader } from '../components/blockProofreader.js';
import { filePicker } from '../components/filePicker.js';
import { renderPageAnnotator } from './pageAnnotator.js';
import { markdown } from '../core/markdown.js';
import { t } from '../core/i18n.js';

const MAX_FILE_BYTES = 30 * 1024 * 1024;
const MAX_FILES = 20;
const MAX_PAGES = 20;
const MAX_OCR_PAGES = 5;
const ALLOWED_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'webp', 'pdf', 'docx']);
// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const VERIFY_STATUS_LABELS = {
  get pending() { return t('paper.import.unverified'); },
  get verified() { return t('paper.import.modelsAgree'); },
  get diff() { return t('paper.import.modelsDisagree'); },
  get failed() { return t('paper.import.crossCheckIncomplete'); },
  get accepted() { return t('paper.import.verificationConfirmed'); },
  get skipped() { return t('paper.import.skipped'); },
};
const VERIFY_FLAG_LABELS = {
  get stem_mismatch() { return t('paper.import.stemsDiffer'); },
  get options_mismatch() { return t('paper.import.optionsDiffer'); },
  get answers_mismatch() { return t('paper.import.answersDiffer'); },
  get type_mismatch() { return t('paper.import.typesDiffer'); },
  get missing_in_verify() { return t('paper.import.theVerificationModelDidNotRecognizeThis'); },
};
const OCR_SPLIT_MODES = [
  { value: 'blank_line', get label() { return t('paper.import.mergeIntoQuestionsAtBlankLinesRecommended'); } },
  { value: 'lines', get label() { return t('paper.import.oneDraftPerLineForOneQuestion'); } },
];
const ITEM_STATUS_FILTERS = [
  { id: 'all', get label() { return t('tasks.filter.all'); } },
  { id: 'needs_review', get label() { return t('question.pendingReview'); } },
  { id: 'pending', get label() { return t('assistant.pendingConfirmation'); } },
  { id: 'accepted', get label() { return t('paper.import.accepted'); } },
  { id: 'skipped', get label() { return t('paper.import.skipped_2'); } },
  { id: 'committed', get label() { return t('home.importStatus.committed'); } },
];

const IMPORT_STATUS_LABELS = {
  get draft() { return t('home.importStatus.draft'); },
  get recognizing() { return t('home.importStatus.recognizing'); },
  get ready() { return t('home.importStatus.ready'); },
  get committed() { return t('home.importStatus.committed'); },
  get failed() { return t('paper.import.recognitionIncomplete'); },
};
const IMPORT_STATUS_VARIANTS = {
  draft: 'blue',
  recognizing: 'amber',
  ready: 'teal',
  committed: 'teal',
  failed: 'red',
};

export function importStatusBadge(status) {
  return badge(IMPORT_STATUS_LABELS[status] || status || t('tasks.unknownStatus'),
    IMPORT_STATUS_VARIANTS[status] || '');
}

export function importStatusLabel(status) {
  return IMPORT_STATUS_LABELS[status] || status || t('tasks.unknownStatus');
}

export function verifyStatusLabel(status) {
  return VERIFY_STATUS_LABELS[status] || t('paper.import.unverified');
}

/** 逐题核验状态徽标（双模型一致 / 不一致 / 未完成）。 */
function verifyBadges(item) {
  const nodes = [];
  const status = item?.verify_status || 'pending';
  if (status === 'diff') nodes.push(badge(t('paper.import.modelsDisagree'), 'red'));
  else if (status === 'failed') nodes.push(badge(t('paper.import.crossCheckIncomplete'), 'amber'));
  else if (status === 'verified') nodes.push(badge(t('paper.import.modelsAgree'), 'teal'));
  else if (status === 'accepted') nodes.push(badge(t('paper.import.verificationConfirmed'), 'blue'));
  const model = item?.verify?.model;
  if (model && ['verified', 'diff', 'accepted'].includes(status)) {
    nodes.push(badge(t('modelPicker.verifyChip', { name: model }), ''));
  }
  return nodes;
}

function verifyDiffSummary(diff) {
  const flags = Array.isArray(diff?.flags) ? diff.flags : [];
  const parts = [];
  const ratio = Number(diff?.stem_ratio);
  if (Number.isFinite(ratio)) parts.push(t('paper.import.stemSimilarity', { v1: Math.round(ratio * 100) }));
  if (diff?.options_equal === false) parts.push(t('paper.import.optionsDiffer_2'));
  if (diff?.answers_equal === false) parts.push(t('paper.import.answersDiffer_2'));
  if (!parts.length) parts.push(flags.map(flag => VERIFY_FLAG_LABELS[flag] || flag).join(t('common.listSeparator')) || t('paper.import.differences'));
  return parts.join(' · ');
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)}MB`;
  if (value >= 1024) return `${Math.round(value / 1024)}KB`;
  return `${value}B`;
}

function extensionOf(name) {
  const match = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
  return match ? match[1] : '';
}

function toBboxStyle(bbox) {
  const safe = bbox || {};
  const left = Math.max(0, Math.min(1, Number(safe.x) || 0));
  const top = Math.max(0, Math.min(1, Number(safe.y) || 0));
  const width = Math.max(0.002, Math.min(1 - left, Number(safe.w) || 0.1));
  const height = Math.max(0.002, Math.min(1 - top, Number(safe.h) || 0.1));
  return `left:${(left * 100).toFixed(3)}%;top:${(top * 100).toFixed(3)}%;`
    + `width:${(width * 100).toFixed(3)}%;height:${(height * 100).toFixed(3)}%`;
}

function clampBbox(bbox) {
  const x = Math.max(0, Math.min(1, Number(bbox.x) || 0));
  const y = Math.max(0, Math.min(1, Number(bbox.y) || 0));
  const w = Math.max(0.02, Math.min(1 - x, Number(bbox.w) || 0.02));
  const h = Math.max(0.02, Math.min(1 - y, Number(bbox.h) || 0.02));
  return { x: Number(x.toFixed(4)), y: Number(y.toFixed(4)), w: Number(w.toFixed(4)), h: Number(h.toFixed(4)) };
}

function subjectGradeText(importRow) {
  return [importRow?.subject, importRow?.grade].filter(Boolean).join(' · ');
}

/* ---------------------------------------------------------------------------
 * 上传区
 * ------------------------------------------------------------------------- */

export function createImportUploader({ meta = {}, onCreated = null } = {}) {
  const subjects = meta.subjects || [];
  const grades = meta.grades || [];
  const files = [];
  const fileList = el('div', { class: 'pl-file-list' });
  const titleInput = input(t('paper.import.paperNameDefaultsToTheFileName'), { maxlength: '120' });
  const subjectSelect = el('select', { class: 'input' });
  subjectSelect.append(el('option', { value: '' }, t('paper.import.subjectOptional')));
  for (const subject of subjects) subjectSelect.append(el('option', { value: subject }, subject));
  const gradeSelect = el('select', { class: 'input' });
  gradeSelect.append(el('option', { value: '' }, t('paper.import.gradeOptional')));
  for (const grade of grades) gradeSelect.append(el('option', { value: grade }, grade));
  const uploadBtn = button(t('paper.import.startUpload'), null, '', 'upload');
  const limits = notice(
    t('paper.import.supportsJpgPngWebpPdfDocxUp', { v1: formatBytes(MAX_FILE_BYTES) })
    + t('paper.import.upToFilesAndPagesPerUpload', { v1: MAX_FILES, v2: MAX_PAGES }),
  );

  function renderFiles() {
    fileList.replaceChildren();
    if (!files.length) {
      fileList.append(el('p', { class: 'help pl-file-empty' }, t('paper.import.noFilesSelectedYetDragThemTo')));
      return;
    }
    files.forEach((file, index) => {
      fileList.append(el('div', { class: 'pl-file-item' },
        el('span', { class: 'pl-file-name' }, file.name),
        badge(formatBytes(file.size), 'blue'),
        el('span', { class: 'pl-file-ext' }, extensionOf(file.name).toUpperCase()),
        button(t('upload.remove'), () => { files.splice(index, 1); renderFiles(); }, 'ghost small', 'trash')));
    });
    fileList.append(el('p', { class: 'help' },
      t('paper.import.filesSelectedAfterRecognitionYouCanVerify', { v1: files.length })));
  }

  function addFiles(list) {
    const incoming = Array.isArray(list) ? list : [list];
    for (const file of incoming) {
      if (!file) continue;
      const extension = extensionOf(file.name);
      if (!ALLOWED_EXTENSIONS.has(extension)) {
        toast(t('paper.import.isNotSupportedOnlyJpgPngWebp', { v1: file.name }), true);
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        toast(t('paper.import.exceedsThePerFileLimitCompressIt', { v1: file.name, v2: formatBytes(MAX_FILE_BYTES) }), true);
        continue;
      }
      if (files.length >= MAX_FILES) {
        toast(t('paper.import.upToFilesPerUploadPleaseSplit', { v1: MAX_FILES }), true);
        break;
      }
      files.push(file);
    }
    renderFiles();
  }

  uploadBtn.addEventListener('click', () => busy(uploadBtn, async () => {
    if (!files.length) throw new Error(t('paper.import.chooseFilesFirstJpgPngWebpPdf'));
    const form = new FormData();
    for (const file of files) form.append('files', file, file.name);
    if (titleInput.value.trim()) form.set('title', titleInput.value.trim());
    if (subjectSelect.value) form.set('subject', subjectSelect.value);
    if (gradeSelect.value) form.set('grade', gradeSelect.value);
    const result = await api('/paperlab/imports', { method: 'POST', body: form });
    files.length = 0;
    renderFiles();
    titleInput.value = '';
    toast(t('paper.import.filesUploadedAndPaginatedNextChooseA'));
    if (onCreated) onCreated(result.import);
  }));

  renderFiles();
  const node = el('section', { class: 'card pl-upload-card' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paper.import.uploadPaperHandout')),
        el('p', { class: 'help' }, t('paper.import.photosScansOrDigitalDocumentsAllWork'))),
      badge(t('paper.import.p0RecognitionImport'), 'teal')),
    filePicker(
      t('paper.import.chooseFilesOrDragThemHere'),
      t('paper.import.jpgPngWebpPdfDocxMultipleAllowed'),
      '.jpg,.jpeg,.png,.webp,.pdf,.docx',
      addFiles,
      { multiple: true },
    ),
    fileList,
    el('div', { class: 'pl-upload-fields' },
      field(t('paper.import.paperNameOptional'), titleInput),
      field(t('paper.import.subjectOptional'), subjectSelect),
      field(t('paper.import.gradeOptional'), gradeSelect)),
    limits,
    el('div', { class: 'row end' }, uploadBtn));
  return { node, addFiles };
}

/* ---------------------------------------------------------------------------
 * 导入详情
 * ------------------------------------------------------------------------- */

export async function renderImportDetail({
  mount, importId, meta = {}, version = state.renderVersion, onBack = null,
} = {}) {
  mount.replaceChildren(loading(t('paper.import.loadingImportDetails')));
  let detail;
  try {
    if (!(state.providers || []).length) {
      await refreshProviders().catch(() => {});
    }
    detail = await api(`/paperlab/imports/${encodeURIComponent(importId)}`);
  } catch (error) {
    mount.replaceChildren(el('div', { class: 'pl-detail-error' },
      notice(error.message || t('paper.import.failedToLoadImportDetailsPleaseTry'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderImportDetail({ mount, importId, meta, version, onBack }), '', 'repeat'),
        button(t('paper.import.backToImports'), () => { if (onBack) onBack(); }, 'secondary'))));
    return;
  }
  if (!current(version)) return;

  const ui = {
    detail,
    pageNo: 1,
    selectedItemId: null,
    filter: 'all',
    selected: new Set(),
    committing: false,
    recognizing: false,
    jobSummary: null,
    pdf: null,
    ocrBusy: false,
    ocrNotice: '',
    ocrPreview: [],
  };
  if (detail.items.length) ui.selectedItemId = detail.items[0].id;

  const pageTabs = el('div', { class: 'pl-page-tabs', role: 'tablist', 'aria-label': t('paper.import.switchPages') });
  const pageTitle = el('p', { class: 'pl-page-title' });
  const canvas = el('div', { class: 'pl-page-canvas' });
  const pageHint = el('p', { class: 'help pl-page-hint' });
  const summaryBox = el('div', { class: 'pl-summary' });
  const jobHost = el('div', { class: 'pl-job-host' });
  const jobSummaryBox = el('div', { class: 'pl-job-summary' });
  const itemsBox = el('div', { class: 'pl-item-list' });
  const commitBox = el('div', { class: 'pl-commit-box' });
  const recognizeBox = el('div', { class: 'pl-recognize-box' });
  const ocrBox = el('div', { class: 'pl-ocr-box' });
  const ocrResultBox = el('div', { class: 'pl-ocr-result' });
  const filterTabs = el('div', { class: 'pl-item-filters', role: 'tablist', 'aria-label': t('paper.import.draftFilter') });
  const selectBar = el('div', { class: 'pl-select-bar' });
  const filterCount = el('span', { class: 'pl-filter-count' });

  const picker = createModelPicker('paper-import', { requireVision: true, showModeToggle: false });
  const rangeFrom = el('input', {
    type: 'number', class: 'input pl-range-input', min: '1', max: String(detail.import.page_count),
    value: '1', 'aria-label': t('paper.import.recognitionStartPage'),
  });
  const rangeTo = el('input', {
    type: 'number', class: 'input pl-range-input', min: '1', max: String(detail.import.page_count),
    value: String(detail.import.page_count), 'aria-label': t('paper.import.recognitionEndPage'),
  });
  const recognizeBtn = button(t('paper.import.startRecognition'), null, '', 'sparkle');
  const noProvider = !(state.providers || []).length;

  function currentPage() {
    return detail.pages.find(page => page.page_no === ui.pageNo) || detail.pages[0] || null;
  }
  function itemPage(item) {
    return Number(item.page_no) || 1;
  }
  function itemDraft(item) {
    return item.draft || {};
  }
  function itemBbox(item) {
    const draft = itemDraft(item);
    const bbox = draft.bbox || item.bbox || {};
    return Number(bbox.w) > 0 && Number(bbox.h) > 0 ? bbox : null;
  }
  function selectedItem() {
    return detail.items.find(item => item.id === ui.selectedItemId) || null;
  }

  function ensurePageVisible(item) {
    const pageNo = itemPage(item);
    if (pageNo !== ui.pageNo) {
      ui.pageNo = pageNo;
      renderPageTabs();
      renderPageViewer();
    }
  }

  function selectItem(itemId, { scrollCard = false } = {}) {
    ui.selectedItemId = itemId;
    const item = selectedItem();
    if (item) ensurePageVisible(item);
    renderOverlay();
    renderItems();
    if (scrollCard) {
      const card = itemsBox.querySelector(`[data-item-id="${itemId}"]`);
      if (card && typeof card.scrollIntoView === 'function') {
        card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    }
  }

  /* ---------------- 页切换 / 页图 / bbox 叠加 ---------------- */

  function renderPageTabs() {
    pageTabs.replaceChildren();
    detail.pages.forEach(page => {
      const active = page.page_no === ui.pageNo;
      const tab = button(String(page.page_no), () => {
        ui.pageNo = page.page_no;
        renderPageTabs();
        renderPageViewer();
        renderOverlay();
      }, `pl-page-tab${active ? ' active' : ''}`, null, {
        'aria-pressed': active ? 'true' : 'false',
        'aria-label': t('result.page', { v1: page.page_no }),
      });
      pageTabs.append(tab);
    });
  }

  function renderPageViewer() {
    const page = currentPage();
    canvas.replaceChildren();
    pageHint.replaceChildren();
    if (!page) {
      pageTitle.textContent = t('paper.import.noPages');
      return;
    }
    pageTitle.textContent = t('common.pageOf', { page: page.page_no, pages: detail.import.page_count })
      + (page.width && page.height ? ` · ${page.width}×${page.height}` : '');
    if (!page.image_url) {
      const itemCount = detail.items.filter(item => itemPage(item) === page.page_no).length;
      canvas.append(el('div', { class: 'pl-text-page' },
        el('p', {}, t('paper.import.thisIsAPlainTextPageExtracted')),
        el('p', { class: 'help' }, t('paper.import.draftsOnThisPage', { v1: itemCount }))));
      pageHint.append(el('span', {}, t('paper.import.plainTextPagesCannotBeCropped')));
      return;
    }
    const image = el('img', {
      class: 'pl-page-image', src: page.image_url, alt: t('paper.import.pageOriginalImage', { v1: page.page_no }),
      loading: 'lazy',
    });
    const overlay = el('div', { class: 'pl-page-overlay' });
    image.addEventListener('error', () => {
      canvas.replaceChildren(notice(t('paper.import.failedToLoadThePageImageRe'), 'error'));
    });
    canvas.append(image, overlay);
    canvas.onclick = () => { /* 点击空白处保持当前选择，不额外处理 */ };
    renderOverlayInto(overlay);
    pageHint.append(el('span', {}, t('paper.import.clickAQuestionBoxToSelectIt')));
    if (page.has_ocr) {
      pageHint.append(el('span', { class: 'pl-page-ocr-hint' },
        t('paper.import.localOcrExtractedCharactersUsedAsA', { v1: page.ocr_chars || 0 })));
    }
    if (page.has_blocks) {
      pageHint.append(button(
        t('paper.import.pageBlocks', { count: page.ocr_block_count || 0 }),
        () => openPageBlockProofreader(page),
        'secondary small',
      ));
    }
  }

  /** 工坊逐页块校对：读取本页块 → 通用组件编辑 → 乐观锁保存。 */
  async function openPageBlockProofreader(page) {
    if (!page || !page.has_blocks) return;
    let data;
    try {
      data = await api(`/paperlab/imports/${importId}/pages/${page.page_no}/blocks`);
    } catch (error) {
      toast(error && error.message ? error.message : t('paper.import.pageBlocksEmpty'), true);
      return;
    }
    if (!Array.isArray(data.blocks) || !data.blocks.length) {
      toast(t('paper.import.pageBlocksEmpty'), true);
      return;
    }
    let version = Number(data.version) || 0;
    const dialog = el('dialog', { class: 'modal-dialog pl-block-proof-dialog' });
    const title = el('h2', {}, t('paper.import.pageBlockTitle', { page: page.page_no }));
    const preview = page.image_url ? el('div', { class: 'pl-block-proof-preview' }) : null;
    const image = page.image_url
      ? el('img', {
        class: 'pl-page-image',
        src: page.image_url,
        alt: t('paper.import.pageOriginalImage', { v1: page.page_no }),
      })
      : null;
    const shell = page.image_url ? el('div', { class: 'pl-page-overlay' }) : null;
    if (preview) preview.append(image, shell);
    const reader = createBlockProofreader({
      blocks: data.blocks,
      previews: preview ? [{ img: image, shell, pageIndex: 0 }] : [],
      showOverlays: Boolean(preview),
      statusText: t('paper.import.pageBlocksHint'),
      onSave: async blocks => {
        const saved = await api(
          `/paperlab/imports/${importId}/pages/${page.page_no}/blocks`,
          { method: 'PUT', data: { blocks, expected_version: version } },
        );
        version = Number(saved.version) || version + 1;
        page.has_blocks = Boolean(saved.has_blocks);
        page.ocr_block_count = Number(saved.ocr_block_count) || 0;
        page.ocr_blocks_version = version;
        toast(t('study.formula.review.saved'));
        dialog.close();
        renderPageViewer();
      },
    });
    dialog.append(
      title,
      el('p', { class: 'help' }, t('paper.import.pageBlocksHint')),
      ...(preview ? [preview] : []),
      reader.element,
      el('div', { class: 'row', style: 'justify-content:flex-end' },
        button(t('common.close'), () => dialog.close(), 'ghost')),
    );
    dialog.addEventListener('close', () => {
      reader.destroy();
      dialog.remove();
    });
    document.body.append(dialog);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function renderOverlay() {
    const overlay = canvas.querySelector('.pl-page-overlay');
    if (overlay) renderOverlayInto(overlay);
  }

  function renderOverlayInto(overlay) {
    overlay.replaceChildren();
    const items = detail.items.filter(item => itemPage(item) === ui.pageNo);
    let missing = 0;
    for (const item of items) {
      const bbox = itemBbox(item);
      if (!bbox) { missing += 1; continue; }
      const isSelected = item.id === ui.selectedItemId;
      const classes = ['pl-bbox', `pl-bbox-${item.status}`];
      if (isSelected) classes.push('is-selected');
      if (item.needs_review) classes.push('needs-review');
      const box = el('div', {
        class: classes.join(' '),
        style: toBboxStyle(bbox),
        'data-item-id': String(item.id),
        tabindex: '0',
        role: 'button',
        'aria-label': t('paper.import.cropBoxForQuestion', { v1: item.item_no }),
      });
      box.append(el('span', { class: 'pl-bbox-label' }, `#${item.item_no}`));
      box.addEventListener('click', event => {
        event.stopPropagation();
        selectItem(item.id, { scrollCard: true });
      });
      box.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectItem(item.id, { scrollCard: true });
        }
      });
      if (isSelected && !item.question_id) {
        box.append(el('span', {
          class: 'pl-bbox-resize', title: t('paper.import.dragTheBottomRightCornerToResize'), 'aria-hidden': 'true',
        }));
        enableDrag(box, item);
        enableResize(box.querySelector('.pl-bbox-resize'), item);
      }
      overlay.append(box);
    }
    if (!items.length) {
      overlay.append(el('div', { class: 'pl-overlay-empty' },
        el('p', {}, t('paper.import.noRecognizedDraftsOnThisPageYet'))));
    } else if (missing) {
      overlay.append(el('div', { class: 'pl-overlay-hint' },
        t('paper.import.draftsOnThisPageHaveNoValid', { v1: missing })));
    }
  }

  async function saveBbox(item, bbox) {
    if (item.question_id) {
      toast(t('paper.import.thisQuestionIsAlreadyInTheBank'), true);
      return;
    }
    const next = clampBbox(bbox);
    try {
      const result = await api(`/paperlab/imports/${encodeURIComponent(importId)}/items/${item.id}`, {
        method: 'PATCH',
        data: { bbox: next },
      });
      const target = detail.items.find(entry => entry.id === item.id);
      if (target) Object.assign(target, result.item);
      toast(t('paper.import.cropBoxSaved'));
      renderOverlay();
      renderItems();
    } catch (error) {
      toast(error.message || t('paper.import.failedToSaveTheCropBoxPlease'), true);
      renderOverlay();
    }
  }

  function enableDrag(box, item) {
    let drag = null;
    let moved = false;
    box.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.classList.contains('pl-bbox-resize')) return;
      const overlay = box.parentElement;
      const rect = overlay.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const bbox = itemBbox(item);
      if (!bbox) return;
      event.preventDefault();
      box.classList.add('is-dragging');
      moved = false;
      drag = { startX: event.clientX, startY: event.clientY, width: rect.width, height: rect.height, bbox: { ...bbox } };
      if (box.setPointerCapture) {
        try { box.setPointerCapture(event.pointerId); } catch { /* ignore */ }
      }
    });
    box.addEventListener('pointermove', event => {
      if (!drag) return;
      const dx = (event.clientX - drag.startX) / drag.width;
      const dy = (event.clientY - drag.startY) / drag.height;
      if (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001) moved = true;
      const next = clampBbox({
        x: drag.bbox.x + dx,
        y: drag.bbox.y + dy,
        w: drag.bbox.w,
        h: drag.bbox.h,
      });
      box.setAttribute('style', toBboxStyle(next));
    });
    box.addEventListener('pointerup', event => {
      if (!drag) return;
      const dx = (event.clientX - drag.startX) / drag.width;
      const dy = (event.clientY - drag.startY) / drag.height;
      const next = clampBbox({
        x: drag.bbox.x + dx,
        y: drag.bbox.y + dy,
        w: drag.bbox.w,
        h: drag.bbox.h,
      });
      drag = null;
      box.classList.remove('is-dragging');
      if (moved) saveBbox(item, next);
    });
    box.addEventListener('pointercancel', () => {
      drag = null;
      box.classList.remove('is-dragging');
      renderOverlay();
    });
  }

  function enableResize(handle, item) {
    if (!handle) return;
    let resize = null;
    let moved = false;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      const overlay = handle.closest('.pl-page-overlay');
      if (!overlay) return;
      const rect = overlay.getBoundingClientRect();
      const bbox = itemBbox(item);
      if (!bbox || !rect.width || !rect.height) return;
      event.preventDefault();
      event.stopPropagation();
      moved = false;
      resize = { startX: event.clientX, startY: event.clientY, width: rect.width, height: rect.height, bbox: { ...bbox } };
      if (handle.setPointerCapture) {
        try { handle.setPointerCapture(event.pointerId); } catch { /* ignore */ }
      }
    });
    handle.addEventListener('pointermove', event => {
      if (!resize) return;
      const dw = (event.clientX - resize.startX) / resize.width;
      const dh = (event.clientY - resize.startY) / resize.height;
      if (Math.abs(dw) > 0.001 || Math.abs(dh) > 0.001) moved = true;
      const next = clampBbox({
        x: resize.bbox.x,
        y: resize.bbox.y,
        w: resize.bbox.w + dw,
        h: resize.bbox.h + dh,
      });
      const box = handle.closest('.pl-bbox');
      if (box) box.setAttribute('style', toBboxStyle(next));
    });
    handle.addEventListener('pointerup', event => {
      if (!resize) return;
      const dw = (event.clientX - resize.startX) / resize.width;
      const dh = (event.clientY - resize.startY) / resize.height;
      const next = clampBbox({
        x: resize.bbox.x,
        y: resize.bbox.y,
        w: resize.bbox.w + dw,
        h: resize.bbox.h + dh,
      });
      resize = null;
      if (moved) saveBbox(item, next);
    });
    handle.addEventListener('pointercancel', () => { resize = null; renderOverlay(); });
  }

  /* ---------------- 摘要 / 题目列表 ---------------- */

  function renderSummary() {
    const counts = detail.counts || {};
    const importStatus = detail.import?.status;
    const noteNode = detail.import?.note ? notice(detail.import.note, 'warning') : null;
    if (noteNode) noteNode.setAttribute('data-no-i18n', '');
    summaryBox.replaceChildren(
      el('div', { class: 'pl-summary-row' },
        badge(importStatusLabel(importStatus), ''),
        badge(t('common.countPages', { count: detail.import?.page_count || 0 }), 'blue'),
        badge(t('paper.import.draft', { v1: counts.total || 0 }), ''),
        badge(t('paper.import.toReview', { v1: counts.needs_review || 0 }), (counts.needs_review || 0) ? 'amber' : ''),
        badge(t('paper.import.accepted_2', { v1: counts.accepted || 0 }), 'teal'),
        badge(t('paper.import.inBank', { v1: counts.committed || 0 }), 'teal'),
        badge(t('paper.import.skipped_3', { v1: counts.skipped || 0 }), '')),
      subjectGradeText(detail.import)
        ? el('p', { class: 'help', 'data-no-i18n': '' }, t('paper.import.uploaded', { v1: subjectGradeText(detail.import), v2: dateText(detail.import?.created_at) }))
        : el('p', { class: 'help' }, t('paper.import.uploaded_2', { v1: dateText(detail.import?.created_at) })),
      noteNode);
  }

  function filteredItems() {
    const items = detail.items || [];
    if (ui.filter === 'all') return items;
    if (ui.filter === 'needs_review') return items.filter(item => item.needs_review || item.status === 'skipped');
    if (ui.filter === 'committed') return items.filter(item => item.question_id);
    return items.filter(item => item.status === ui.filter);
  }

  function renderFilterTabs() {
    filterTabs.replaceChildren();
    const items = detail.items || [];
    const counts = {
      all: items.length,
      needs_review: items.filter(item => item.needs_review || item.status === 'skipped').length,
      pending: items.filter(item => item.status === 'pending').length,
      accepted: items.filter(item => item.status === 'accepted').length,
      skipped: items.filter(item => item.status === 'skipped').length,
      committed: items.filter(item => item.question_id).length,
    };
    for (const filter of ITEM_STATUS_FILTERS) {
      filterTabs.append(button(`${filter.label} ${counts[filter.id] || 0}`, () => {
        ui.filter = filter.id;
        renderFilterTabs();
        renderItems();
      }, `pl-item-filter${ui.filter === filter.id ? ' active' : ''}`, null, {
        'aria-pressed': ui.filter === filter.id ? 'true' : 'false',
      }));
    }
    filterCount.textContent = t('paper.import.draftsInTotal', { v1: items.length });
  }

  function itemStatusBadges(item) {
    const nodes = [];
    if (item.question_id) nodes.push(badge(t('home.importStatus.committed'), 'teal'));
    else if (item.status === 'accepted') nodes.push(badge(t('paper.import.accepted'), 'teal'));
    else if (item.status === 'skipped') nodes.push(badge(t('paper.import.skipped_2'), 'amber'));
    else nodes.push(badge(t('assistant.pendingConfirmation'), 'blue'));
    if (item.needs_review) nodes.push(badge(t('question.pendingReview'), 'amber'));
    if (item.confidence !== null && item.confidence !== undefined) {
      nodes.push(badge(t('paper.import.confidence', { v1: Math.round(Number(item.confidence) * 100) }), Number(item.confidence) < 0.65 ? 'amber' : ''));
    }
    nodes.push(...verifyBadges(item));
    return nodes;
  }

  function optionText(draft) {
    const options = Array.isArray(draft?.options) ? draft.options : [];
    if (!options.length) return null;
    return options.map(option => `${option.label}. ${option.text}`).join('　');
  }

  function answersText(draft) {
    const answers = Array.isArray(draft?.answers) ? draft.answers.filter(Boolean) : [];
    if (answers.length) return answers.join(t('common.semicolonSeparator'));
    const parts = Array.isArray(draft?.parts) ? draft.parts : [];
    const partAnswers = parts.flatMap(part => (Array.isArray(part.answers) ? part.answers : []))
      .map(String).filter(Boolean);
    return partAnswers.length ? partAnswers.join(t('common.semicolonSeparator')) : '';
  }

  function diffColumn(title, draft, extraClass = '') {
    const column = el('div', { class: `pl-diff-col${extraClass ? ` ${extraClass}` : ''}` },
      el('h4', {}, title));
    if (!draft) {
      column.append(el('p', { class: 'help' }, t('paper.import.noResult')));
      return column;
    }
    const stem = String(draft.stem || '').trim();
    if (stem) column.append(markdown(stem.slice(0, 600)));
    const options = optionText(draft);
    if (options) column.append(el('p', { class: 'pl-diff-options' }, options));
    const answers = answersText(draft);
    column.append(el('p', { class: 'pl-diff-answers' }, answers ? t('mybank.answer', { v1: answers }) : t('paper.import.answerNone')));
    return column;
  }

  /** 逐题核验结果：一致提示 / 未完成核验降级提示 / 三栏差异面板。 */
  function renderVerifyPanel(item) {
    const status = item.verify_status || 'pending';
    if (status === 'pending' || status === 'skipped') return null;
    const verify = item.verify || {};
    const verifyDraft = verify.draft && typeof verify.draft === 'object' ? verify.draft : null;
    const verifyModel = verify.model || t('paper.import.verificationModel');
    if (status === 'failed') {
      return el('div', { class: 'pl-verify-panel pl-verify-failed' },
        notice(
          t('paper.import.crossCheckIncomplete_2', {
            v1: item.verify_error || t('paper.import.verificationModelCallFailed'),
          })
          + t('paper.import.theResultComesFromThePrimaryModel'),
          'warning',
        ));
    }
    const head = el('p', { class: 'pl-verify-head' },
      badge(verifyStatusLabel(status), status === 'diff' ? 'red' : 'teal'),
      el('span', { class: 'help' },
        t('paper.import.primaryModelVerificationModel', {
          v1: itemDraft(item)._ai?.model || t('paper.import.modelA'),
          v2: verifyModel,
        })));
    if (status === 'verified') {
      return el('div', { class: 'pl-verify-panel pl-verify-ok' }, head,
        el('p', { class: 'help' }, t('paper.import.bothModelsIndependentlyRecognizedTheSameStem')));
    }
    const diff = item.diff || {};
    const ratio = Number(diff.stem_ratio);
    const threshold = Number(diff.stem_threshold);
    const summary = status === 'accepted'
      ? t('paper.import.manuallyConfirmedResult', {
        v1: verify.accepted === 'verify' ? t('paper.import.verificationModel') : t('paper.import.primaryModel'),
      })
      : verifyDiffSummary(diff);
    const flags = Array.isArray(diff.flags) ? diff.flags : [];
    const panel = el('details', { class: 'pl-verify-panel pl-verify-diff' },
      el('summary', {},
        el('strong', {}, status === 'accepted' ? t('paper.import.verificationResultConfirmed') : t('paper.import.viewModelDifferences')),
        el('span', { class: 'pl-diff-brief' }, summary)));
    panel.append(el('div', { class: 'pl-diff-grid' },
      diffColumn(t('paper.import.primaryModelResult'), itemDraft(item)),
      diffColumn(t('paper.import.verificationModelResult', { v1: verifyModel }), verifyDraft),
      el('div', { class: 'pl-diff-col pl-diff-metrics' },
        el('h4', {}, t('paper.import.differences_2')),
        el('ul', {},
          el('li', {},
            t('paper.import.stemSimilarity_2', {
              v1: Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : t('admin.unknown'),
            })
            + (Number.isFinite(threshold) ? t('paper.import.threshold', { v1: Math.round(threshold * 100) }) : '')),
          el('li', {}, t('admin.options', {
            v1: diff.options_equal === false ? t('paper.import.mismatch') : t('paper.import.match'),
          })),
          el('li', {}, t('mybank.answer', {
            v1: diff.answers_equal === false ? t('paper.import.mismatch') : t('paper.import.match'),
          })),
          ...flags.map(flag => el('li', { class: 'pl-diff-flag' }, VERIFY_FLAG_LABELS[flag] || flag))))));
    if (status !== 'accepted') {
      panel.append(el('div', { class: 'row pl-verify-actions' },
        button(t('paper.import.useThePrimaryModel'), () => acceptVerify(item, 'primary'), 'secondary small', 'check'),
        verifyDraft
          ? button(t('paper.import.useTheVerificationModel'), () => acceptVerify(item, 'verify'), 'soft small', 'check')
          : null));
    }
    return panel;
  }

  async function acceptVerify(item, source) {
    try {
      const result = await api(
        `/paperlab/imports/${encodeURIComponent(importId)}/items/${item.id}/accept-verify`,
        { method: 'POST', data: { source } },
      );
      Object.assign(item, result.item);
      ui.selectedItemId = item.id;
      toast(source === 'verify' ? t('paper.import.adoptedTheVerificationModelResultYouCan') : t('paper.import.confirmedThePrimaryModelResultAsAuthoritative'));
      renderSummary();
      renderFilterTabs();
      renderItems();
    } catch (error) {
      toast(error.message || t('paper.import.failedToConfirmTheVerificationResultPlease'), true);
    }
  }

  function renderItems() {
    itemsBox.replaceChildren();
    const items = filteredItems();
    if (!items.length) {
      itemsBox.append(empty(t('paper.import.noDraftsMatchTheFilter'), t('paper.import.changeTheFilterOrChooseAVision'), null, 'file'));
      renderSelectBar();
      return;
    }
    for (const item of items) {
      const draft = itemDraft(item);
      const selectable = !item.question_id && item.status !== 'skipped';
      const checkbox = el('input', {
        type: 'checkbox', class: 'pl-item-check', checked: ui.selected.has(item.id),
        'aria-label': t('paper.import.selectQuestion', { v1: item.item_no }),
      });
      checkbox.disabled = !selectable;
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) ui.selected.add(item.id);
        else ui.selected.delete(item.id);
        renderSelectBar();
      });
      const head = el('div', { class: 'pl-item-head' },
        checkbox,
        el('strong', { class: 'pl-item-title' }, t('paper.import.question', { v1: item.item_no })),
        badge(typeLabel(draft.type), 'blue'),
        badge(t('result.page', { v1: item.item_no ? itemPage(item) : 1 }), ''),
        itemStatusBadges(item));
      const card = el('article', {
        class: `pl-item-card${item.id === ui.selectedItemId ? ' is-selected' : ''}`,
        'data-item-id': String(item.id),
        tabindex: '0',
      }, head);
      card.addEventListener('click', event => {
        // 允许在卡片内展开/收起差异面板与操作控件，不触发整卡选中重渲染
        if (event.target.closest('button') || event.target.closest('input')
          || event.target.closest('summary') || event.target.closest('details')) return;
        selectItem(item.id);
      });
      card.addEventListener('keydown', event => {
        if ((event.key === 'Enter' || event.key === ' ')
          && !event.target.closest('button') && !event.target.closest('summary')) {
          event.preventDefault();
          selectItem(item.id);
        }
      });
      if (item.status === 'skipped') {
        card.append(el('p', { class: 'pl-item-skip' },
          t('paper.import.skipReason', { v1: item.skip_reason || t('paper.import.unrecognizable') })));
      } else {
        if (draft.stem) {
          const stemNode = markdown(String(draft.stem).slice(0, 1600));
          stemNode.setAttribute('data-no-i18n', '');
          card.append(stemNode);
        }
        const options = optionText(draft);
        if (options) card.append(el('p', { class: 'pl-item-options', 'data-no-i18n': '' }, options));
      }
      const answers = answersText(draft);
      card.append(el('div', { class: 'pl-item-meta', 'data-no-i18n': '' },
        el('span', {}, answers ? t('mybank.answer', { v1: answers.slice(0, 160) }) : t('paper.import.answerNotRecognizedAiFillInAvailable')),
        (Array.isArray(draft.knowledge_tags) && draft.knowledge_tags.length)
          ? el('span', {}, t('paper.import.knowledgePoint', { v1: draft.knowledge_tags.join(t('common.listSeparator')) }))
          : null,
        item.bbox ? el('span', {}, t('paper.import.cropBox', { v1: Number(item.bbox.x).toFixed(2), v2: Number(item.bbox.y).toFixed(2), v3: Number(item.bbox.w).toFixed(2), v4: Number(item.bbox.h).toFixed(2) })) : null));
      if (Array.isArray(item.warnings) && item.warnings.length) {
        const warningsNode = notice(item.warnings.join(t('common.semicolonSeparator')), 'warning');
        warningsNode.setAttribute('data-no-i18n', '');
        card.append(warningsNode);
      }
      const verifyPanel = renderVerifyPanel(item);
      if (verifyPanel) card.append(verifyPanel);
      const actions = el('div', { class: 'row pl-item-actions' },
        button(t('paper.import.locateOnPageImage'), () => { selectItem(item.id); renderPageViewer(); }, 'ghost small', 'search'),
        item.question_id
          ? button(t('paper.import.viewInBank'), () => go('mybank'), 'soft small', 'tag')
          : button(t('paper.import.editDraft'), () => openItemEditor(item), 'soft small', 'edit'));
      if (!item.question_id) {
        if (item.status === 'skipped') {
          actions.append(button(t('paper.import.restoreToPending'), () => patchItemStatus(item, 'pending'), 'ghost small', 'repeat'));
        } else {
          if (item.status !== 'accepted') {
            actions.append(button(t('paper.import.accept'), () => patchItemStatus(item, 'accepted'), 'small', 'check'));
          }
          actions.append(button(t('practice.skip'), () => patchItemStatus(item, 'skipped', t('paper.import.skipManually')), 'ghost small'));
        }
      }
      card.append(actions);
      itemsBox.append(card);
    }
    renderSelectBar();
  }

  function renderSelectBar() {
    selectBar.replaceChildren();
    const selectable = filteredItems().filter(item => !item.question_id && item.status !== 'skipped');
    const allSelected = selectable.length > 0 && selectable.every(item => ui.selected.has(item.id));
    selectBar.append(
      el('div', { class: 'row' },
        el('span', { class: 'pl-select-count' }, t('paper.import.selected', { v1: ui.selected.size })),
        button(allSelected ? t('paper.import.clearSelection') : t('paper.import.selectCurrentList'), () => {
          if (allSelected) ui.selected.clear();
          else selectable.forEach(item => ui.selected.add(item.id));
          renderItems();
        }, 'ghost small', 'check'),
        button(t('mybank.clearSelection'), () => { ui.selected.clear(); renderItems(); }, 'ghost small')),
      filterCount);
  }

  function renderCommitBox() {
    commitBox.replaceChildren();
    commitBox.append(el('div', { class: 'row' },
      button(t('paper.import.commitSelectedQuestions'), async event => {
        if (!ui.selected.size) {
          toast(t('paper.import.selectTheQuestionsToCommitFirst'), true);
          return;
        }
        await busy(event.currentTarget, () => commitItems([...ui.selected]));
      }, '', 'upload'),
      button(t('paper.import.commitAllAcceptedQuestions'), async event => {
        await busy(event.currentTarget, () => commitItems(null));
      }, 'secondary'),
      el('span', { class: 'help' }, t('paper.import.committedQuestionsGoToMyQuestionBank'))));
  }

  async function commitItems(itemIds) {
    if (ui.committing) return;
    ui.committing = true;
    try {
      const result = await api(`/paperlab/imports/${encodeURIComponent(importId)}/commit`, {
        method: 'POST',
        data: itemIds ? { item_ids: itemIds } : {},
      });
      ui.selected.clear();
      const created = result.created_question_ids || [];
      const existing = result.existing_question_ids || [];
      const messages = [];
      if (created.length) messages.push(t('paper.import.committedQuestions', { v1: created.length }));
      if (existing.length) messages.push(t('paper.import.wereAlreadyInTheBankNotRe', { v1: existing.length }));
      if (result.warnings?.length) messages.push(result.warnings.map(w => w.message || w).join(t('common.semicolonSeparator')));
      toast(messages.join(t('common.semicolonSeparator')) || t('paper.import.noNewQuestionsToCommit'));
      await refreshDetail();
      commitBox.append(el('div', { class: 'notice pl-commit-result' },
        el('strong', {}, t('paper.import.commitCompleteNewQuestions', {
          v1: created.length,
          v2: existing.length ? t('paper.import.alreadyExisted', { v1: existing.length }) : '',
        })),
        el('div', { class: 'row', style: 'margin-top:10px' },
          button(t('paper.import.viewInMyQuestionBank'), () => go('mybank'), 'small', 'tag'),
          button(t('paper.import.continueReviewing'), () => { commitBox.querySelectorAll('.pl-commit-result').forEach(node => node.remove()); },
            'ghost small'))));
    } finally {
      ui.committing = false;
    }
  }

  async function patchItemStatus(item, status, skipReason = '') {
    try {
      const result = await api(`/paperlab/imports/${encodeURIComponent(importId)}/items/${item.id}`, {
        method: 'PATCH',
        data: skipReason ? { status, skip_reason: skipReason } : { status },
      });
      Object.assign(item, result.item);
      toast(status === 'skipped' ? t('paper.import.draftSkipped') : status === 'accepted' ? t('paper.import.draftAcceptedYouCanCommitInBulk') : t('paper.import.restoredToPending'));
      renderSummary();
      renderFilterTabs();
      renderItems();
      renderPageViewer();
    } catch (error) {
      toast(error.message || t('paper.import.theActionFailedPleaseTryAgain'), true);
    }
  }

  function openItemEditor(item) {
    const initial = {
      type: itemDraft(item).type || 'SUBJECTIVE',
      subject: itemDraft(item).subject || detail.import?.subject || '',
      grade: itemDraft(item).grade || detail.import?.grade || '',
      difficulty: itemDraft(item).difficulty,
      knowledge_tags: itemDraft(item).knowledge_tags || [],
      content: itemDraft(item),
      verify_status: 'pending',
    };
    openQuestionEditor({
      title: t('paper.import.editRecognitionDraftQuestion', { v1: item.item_no }),
      meta,
      question: initial,
      saveLabel: t('paper.import.saveDraft'),
      onSave: async values => {
        await api(`/paperlab/imports/${encodeURIComponent(importId)}/items/${item.id}`, {
          method: 'PATCH',
          data: {
            draft: {
              type: values.type,
              subject: values.subject,
              grade: values.grade,
              difficulty: values.difficulty,
              knowledge_tags: values.knowledge_tags,
              ...values.content,
            },
          },
        });
        await refreshDetail();
      },
    });
  }

  async function refreshDetail() {
    try {
      detail = await api(`/paperlab/imports/${encodeURIComponent(importId)}`);
      ui.detail = detail;
      if (!detail.items.some(item => item.id === ui.selectedItemId)) {
        ui.selectedItemId = detail.items.length ? detail.items[0].id : null;
      }
      if (ui.pageNo > (detail.import?.page_count || 1)) ui.pageNo = 1;
      renderAll();
    } catch (error) {
      toast(error.message || t('paper.import.failedToRefreshImportDetailsPleaseTry'), true);
    }
  }

  /* ---------------- 识别任务 ---------------- */

  function renderRecognizePanel() {
    recognizeBox.replaceChildren();
    const head = el('div', { class: 'card-header' },
      el('div', {},
        el('h3', {}, t('paper.import.aiRecognition')),
        el('p', { class: 'help' }, t('paper.import.callsTheVisionModelPageByPage'))));
    const rangeRow = el('div', { class: 'pl-range-row' },
      field(t('notes.startPage'), rangeFrom),
      field(t('notes.endPage'), rangeTo),
      el('p', { class: 'help' }, t('paper.import.upToPagesPerRecognition', { v1: MAX_PAGES })));
    recognizeBox.append(head, rangeRow, picker.node, jobHost, jobSummaryBox,
      el('div', { class: 'row end' }, recognizeBtn));
    if (noProvider) {
      recognizeBox.append(notice(
        t('paper.import.noModelConnectionAvailableYetAddAn'),
        'warning'));
      recognizeBox.append(el('div', { class: 'row' },
        button(t('paper.import.goToModelSettingsForAVision'), () => go('settings'), 'soft', 'sliders')));
      recognizeBtn.disabled = true;
      recognizeBtn.title = t('paper.import.configureAVisionModelInModelSettings');
    }
    if (detail.import?.status === 'committed') {
      recognizeBox.append(notice(t('paper.import.thisImportHasBeenCommittedYouCan')));
    }
  }

  recognizeBtn.addEventListener('click', () => busy(recognizeBtn, async () => {
    if (noProvider) throw new Error(t('paper.import.configureAVisionCapableModelInModel'));
    if (ui.recognizing) throw new Error(t('paper.import.aRecognitionTaskIsInProgressWait'));
    const total = Number(detail.import?.page_count) || 0;
    const from = Number(rangeFrom.value) || 1;
    const to = Number(rangeTo.value) || total;
    if (from < 1 || from > total || to < from || to > total) {
      throw new Error(t('paper.import.invalidPageRangeItMustBeWithin', { v1: total }));
    }
    if (to - from + 1 > MAX_PAGES) throw new Error(t('paper.import.upToPagesPerRecognitionPleaseRun', { v1: MAX_PAGES }));
    const payload = picker.payload(false);
    // modelPicker 的「独立交叉验证」= 双模型核验：payload 里已带核验连接/模型并校验不能同模型
    const crossPayload = payload.cross_verify
      ? {
        cross_verify: true,
        verify_provider_id: payload.verify_provider_id,
        verify_model: payload.verify_model,
      }
      : { cross_verify: false };
    const response = await api(`/paperlab/imports/${encodeURIComponent(importId)}/recognize`, {
      method: 'POST',
      data: {
        page_from: from,
        page_to: to,
        provider_id: payload.provider_id,
        model: payload.model,
        ...crossPayload,
      },
    });
    const job = response.job || response;
    ui.recognizing = true;
    recognizeBtn.disabled = true;
    jobSummaryBox.replaceChildren();
    jobHost.replaceChildren();
    toast(crossPayload.cross_verify
      ? t('paper.import.recognitionTaskSubmittedDualModelCrossCheck')
      : t('paper.import.recognitionTaskSubmittedProgressAppearsBelow'));
    mountJob(jobHost, job.id, null, {
      task: 'paper-import',
      onSettled: settled => {
        ui.recognizing = false;
        if (document.body.contains(recognizeBtn)) {
          recognizeBtn.disabled = noProvider;
        }
        renderJobSummary(settled, { from, to });
      },
    });
    renderRecognizeSummaryHint(job);
  }));

  function renderRecognizeSummaryHint(job) {
    jobSummaryBox.replaceChildren(el('p', { class: 'help' },
      t('paper.import.taskSubmittedRecognizingPagesUseCancelTask', { v1: job.id, v2: rangeFrom.value, v3: rangeTo.value })));
  }

  function renderJobSummary(settled, range = {}) {
    if (!settled) return;
    const status = settled.status;
    const result = settled.result || {};
    const failedPages = Array.isArray(result.failed_pages) ? result.failed_pages : [];
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const box = el('div', { class: 'pl-job-result' });
    if (status === 'completed') {
      box.append(el('p', { class: 'pl-job-result-title' },
        t('paper.import.recognitionCompletePagesProcessed', { v1: range.from || 1, v2: range.to || detail.import?.page_count })
        + t('paper.import.pagesOfDraftsThisTime', { v1: Array.isArray(result.done_pages) ? result.done_pages.length : 0 })));
    } else if (status === 'cancelled') {
      box.append(notice(t('paper.import.recognitionTaskCancelledDraftsForFinishedPages'), 'warning'));
    } else if (status === 'failed' || status === 'interrupted') {
      box.append(notice(settled.error || t('paper.import.theRecognitionTaskDidNotFinishClick'), 'error'));
    }
    if (failedPages.length) {
      box.append(notice(t('paper.import.thesePagesFailedRecognitionYouCanRetry', {
        v1: failedPages.map(page => t('result.page', { v1: page })).join(t('common.listSeparator')),
      }), 'warning'));
      box.append(el('div', { class: 'row' },
        button(t('notes.retryFailedPagesOnly'), () => {
          rangeFrom.value = String(Math.min(...failedPages));
          rangeTo.value = String(Math.max(...failedPages));
          toast(t('paper.import.theRecognitionRangeIsSetToThe'));
        }, 'soft small', 'repeat')));
    }
    const verify = result.verify && typeof result.verify === 'object' ? result.verify : null;
    if (verify && verify.enabled) {
      const counts = verify.counts || {};
      box.append(el('p', { class: 'pl-verify-job-summary' },
        el('strong', {}, t('paper.import.dualModelVerification')),
        t('paper.import.agreeDifferIncomplete', { v1: counts.verified || 0, v2: counts.diff || 0, v3: counts.failed || 0 }),
        verify.model ? t('paper.import.verificationModel_2', { v1: verify.model }) : ''));
      if (counts.diff) {
        box.append(notice(
          t('paper.import.questionsHaveDifferingDualModelResultsThey', { v1: counts.diff })
          + t('paper.import.openViewModelDifferencesToCompareThe'),
          'warning',
        ));
      }
      if (counts.failed) {
        box.append(notice(
          t('paper.import.questionsHaveAnIncompleteCrossCheckPrimary', { v1: counts.failed }),
          'warning',
        ));
      }
    }
    if (warnings.length) box.append(notice(warnings.join('\n'), 'warning'));
    jobSummaryBox.replaceChildren(box);
    refreshDetail();
  }

  /* ---------------- 本地 OCR（无视觉模型降级） ---------------- */

  function ocrRangePages() {
    const total = Number(detail.import?.page_count) || 0;
    const from = Math.max(1, Number(rangeFrom.value) || 1);
    const to = Math.min(total, Number(rangeTo.value) || total);
    if (from < 1 || to < from || to > total) {
      throw new Error(t('paper.import.invalidPageRangeItMustBeWithin', { v1: total }));
    }
    if (to - from + 1 > MAX_OCR_PAGES) {
      throw new Error(t('paper.import.localOcrHandlesUpToPagesPer', { v1: MAX_OCR_PAGES }));
    }
    const pages = [];
    for (let page = from; page <= to; page += 1) pages.push(page);
    return pages;
  }

  function renderOcrPanel() {
    ocrBox.replaceChildren();
    const info = detail.ocr || {};
    const available = info.available !== false;
    const head = el('div', { class: 'card-header' },
      el('div', {},
        el('h3', {}, t('paper.import.localOcrFallbackWithoutAVisionModel')),
        el('p', { class: 'help' },
          t('paper.import.readsPageTextByMachineWithoutCalling')
          + t('paper.import.orWithoutAVisionModelGenerateDrafts'))),
      badge(available ? t('paper.import.availableLocally') : t('paper.import.unavailableLocally'), available ? 'teal' : 'amber'));
    const splitSelect = select(OCR_SPLIT_MODES, ui.ocrSplit || 'blank_line');
    splitSelect.addEventListener('change', () => { ui.ocrSplit = splitSelect.value; });
    const pagesWithOcr = (detail.pages || []).filter(page => page.has_ocr);
    const ocrSummary = pagesWithOcr.length
      ? el('div', { class: 'pl-ocr-pages' },
        el('p', { class: 'help' }, t('paper.import.extractedTextFromPagesUsableAsAn', { v1: pagesWithOcr.length })),
        ...pagesWithOcr.map(page => el('div', { class: 'pl-ocr-page-row' },
          badge(t('result.page', { v1: page.page_no }), 'blue'),
          el('span', { class: 'help' },
            t('paper.import.charactersLines', { v1: page.ocr_chars || 0, v2: page.ocr_engine || 'OCR', v3: page.ocr_line_count || 0 })),
          page.ocr_preview ? el('span', { class: 'pl-ocr-preview' }, page.ocr_preview) : null)))
      : el('p', { class: 'help' }, t('paper.import.noOcrTextExtractedFromAnyPage'));

    const extractBtn = button(t('paper.import.extractTextWithLocalOcr'), async event => {
      await busy(event.currentTarget, async () => {
        const pages = ocrRangePages();
        if (ui.ocrBusy) throw new Error(t('paper.import.ocrIsRunningPleaseWait'));
        ui.ocrBusy = true;
        try {
          const result = await api(`/paperlab/imports/${encodeURIComponent(importId)}/ocr`, {
            method: 'POST', data: { pages },
          });
          // 局部更新页数据，避免整页重渲染丢失任务卡状态
          for (const pageResult of result.pages || []) {
            const target = (detail.pages || []).find(page => page.page_no === pageResult.page_no);
            if (!target) continue;
            target.has_ocr = true;
            target.ocr_chars = pageResult.chars;
            target.ocr_engine = pageResult.engine;
            target.ocr_line_count = pageResult.line_count;
            target.ocr_preview = pageResult.preview;
          }
          ui.ocrPreview = result.pages || [];
          if (result.errors?.length) {
            toast(result.errors.map(item => t('notes.page', { v1: item.page_no, v2: item.error }))
      .join(t('common.semicolonSeparator')), true);
          } else {
            toast(t('paper.import.localOcrFinishedContinueWithAiRecognition'));
          }
          renderOcrPanel();
          renderPageViewer();
        } finally {
          ui.ocrBusy = false;
        }
      });
    }, '', 'search');
    const draftBtn = button(t('paper.import.generateDraftsFromOcr'), async event => {
      await busy(event.currentTarget, async () => {
        const pages = ocrRangePages();
        if (ui.ocrBusy) throw new Error(t('paper.import.ocrIsRunningPleaseWait'));
        ui.ocrBusy = true;
        try {
          const result = await api(`/paperlab/imports/${encodeURIComponent(importId)}/ocr-draft`, {
            method: 'POST', data: { pages, split: splitSelect.value },
          });
          ui.ocrNotice = t('paper.import.generatedLocalOcrDraftsNoAnswersUnclassified', { v1: result.created || 0 });
          toast(ui.ocrNotice);
          await refreshDetail();
        } finally {
          ui.ocrBusy = false;
        }
      });
    }, 'soft', 'edit');
    if (!available) {
      extractBtn.disabled = true;
      draftBtn.disabled = true;
      extractBtn.title = t('paper.import.localOcrIsCurrentlyUnavailable');
      draftBtn.title = t('paper.import.localOcrIsCurrentlyUnavailable');
    }
    const previewNode = ui.ocrPreview?.length
      ? el('div', { class: 'pl-ocr-preview-box' },
        el('p', { class: 'help' }, t('paper.import.lastOcrExtractionResult')),
        ...ui.ocrPreview.map(page => el('div', { class: 'pl-ocr-page-row' },
          badge(t('result.page', { v1: page.page_no }), 'blue'),
          el('span', { class: 'help' }, t('paper.import.characters', { v1: page.chars, v2: page.engine || 'OCR' })),
          el('p', { class: 'pl-ocr-preview' }, page.preview || t('paper.import.noText')))))
      : null;
    // Node.append 会把 null 变成 "null" 文本，这里统一过滤空节点
    ocrBox.append(...[
      head,
      el('p', { class: 'help' },
        t('paper.import.thePageRangeFollowsStartPageEnd', { v1: MAX_OCR_PAGES })
        + t('paper.import.ocrDraftsMergeTextLinesByBlank')),
      el('div', { class: 'pl-ocr-fields' }, field(t('paper.import.draftMergeMode'), splitSelect)),
      el('div', { class: 'row' }, extractBtn, draftBtn),
      ui.ocrNotice ? notice(ui.ocrNotice, '') : null,
      info.notice ? notice(String(info.notice), 'warning') : null,
      previewNode,
      ocrSummary,
    ].filter(Boolean));
  }

  function renderAll() {
    renderSummary();
    renderPageTabs();
    renderPageViewer();
    renderFilterTabs();
    renderItems();
    renderCommitBox();
    renderRecognizePanel();
    renderOcrPanel();
  }

  /* ---------------- 顶部操作 ---------------- */
  const backBtn = button(t('paper.import.backToImports'), () => { if (onBack) onBack(); }, 'ghost small', 'chevron');
  const refreshBtn = button(t('tasks.refresh'), (event) => busy(event.currentTarget, refreshDetail), 'secondary small', 'repeat');
  // P1：切题与答题区标注编辑器（components/pageAnnotator.js；只读该导入的页/草稿数据）。
  const annotateBtn = button(t('paper.import.questionSegmentation'), () => {
    renderPageAnnotator({
      mount, importId, meta, version,
      onBack: () => renderImportDetail({ mount, importId, meta, version, onBack }),
    });
  }, 'soft small', 'edit', { title: t('paper.import.openTheSegmentationEditorSelectQuestionAnswer') });
  const head = el('div', { class: 'pl-detail-head' },
    el('div', { class: 'row' },
      backBtn,
      el('div', {},
        el('h2', { class: 'pl-detail-title', 'data-no-i18n': '' }, detail.import?.title || t('paper.import.import', { v1: importId })),
        el('p', { class: 'help' }, t('paper.import.pagesStatus', { v1: detail.import?.page_count || 0, v2: importStatusLabel(detail.import?.status) })))),
    el('div', { class: 'row' }, importStatusBadge(detail.import?.status), annotateBtn, refreshBtn));

  mount.replaceChildren(el('section', { class: 'pl-detail' },
    head,
    el('div', { class: 'pl-detail-grid' },
      el('div', { class: 'pl-detail-left' },
        el('div', { class: 'card pl-page-card' },
          el('div', { class: 'pl-page-bar' }, pageTitle, pageTabs),
          canvas,
          pageHint),
        summaryBox),
      el('div', { class: 'pl-detail-right' },
        el('section', { class: 'card pl-recognize-card' }, recognizeBox),
        el('section', { class: 'card pl-ocr-card' }, ocrBox),
        el('section', { class: 'card pl-items-card' },
          el('div', { class: 'card-header' },
            el('div', {},
              el('h3', {}, t('paper.import.recognitionDraftReview')),
              el('p', { class: 'help' }, t('paper.import.confirmStemAnswerKnowledgePointQuestionBy'))),
            filterTabs),
          itemsBox,
          selectBar,
          commitBox)))));
  renderAll();
}

/** 页面级导入历史列表（供我的题库/试卷工坊共用）。 */
export function importHistoryCard(importRow, { onOpen, onContinue } = {}) {
  const actions = el('div', { class: 'row' },
    button(t('paper.import.viewDetails'), () => { if (onOpen) onOpen(importRow); }, 'soft small', 'search'));
  if (importRow.status === 'failed' || importRow.status === 'draft') {
    actions.append(button(t('paper.import.continueRecognition'), () => { if (onContinue) onContinue(importRow); }, 'ghost small', 'sparkle'));
  }
  return el('article', { class: 'pl-history-card' },
    el('div', { class: 'pl-history-head' },
      el('strong', { 'data-no-i18n': '' }, importRow.title || t('paper.import.import', { v1: importRow.id })),
      importStatusBadge(importRow.status)),
    el('p', { class: 'help pl-history-meta', 'data-no-i18n': '' },
      [subjectGradeText(importRow), t('common.countPages', { count: importRow.page_count || 0 }), dateText(importRow.created_at)]
        .filter(Boolean).join(' · ')),
    actions);
}

