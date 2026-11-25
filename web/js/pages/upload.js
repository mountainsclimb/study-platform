/* 拍题入库（OCR-first 主路径）+ 旧拍题讲解流程。
 *
 * 主路径（Phase 1，无需视觉模型 Key）：
 *   1. 拍照/选图/粘贴/文本上传 → POST /api/uploads/question 建立私人草稿；
 *   2. 默认本地 OCR（POST /api/uploads/{id}/ocr-draft）预填题干/选项/答案；
 *      视觉模型只作为「增强识别」可选动作（原 recognize 任务，保留旧行为）；
 *   3. 人工校对 + PATCH /api/uploads/{id}/confirm（带 expected_version 乐观锁），
 *      识别草稿与人工确认分离；确认后到达的异步识别结果只显示为「模型草稿」；
 *   4. POST /api/uploads/{id}/save-to-bank 写入「我的题库」（pl_questions，原图转资产）；
 *      成功页提供 练这道（app:<id> 统一引用） / 加入复习 / 继续拍题 / 投稿到广场；
 *   5. AI 解出的答案标记为「AI 建议答案」（POST /api/uploads/{id}/ai-answer），逐字段采用。
 * 旧「讲解/上传错题」步骤保留在保存之后。
 *
 * 深链接：`#/upload?upload=ID` 恢复指定草稿（任务中心 recognize 任务完成后跳转）。
 */
import { t } from '../core/i18n.js';
import { api, ensureExplainModes } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, badge, heading, notice, field, input, select,
  questionText, dateText, busy, toast, readableSummary,
} from '../core/dom.js';
import { go, current, setHashParams, renderRoute } from '../core/router.js';
import { sanitizeTableHtml } from '../core/markdown.js';
import { mountJob, taskNames, statusNames, statusBadgeVariant, closeJobStreams } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker, attachPasteImage } from '../components/filePicker.js';
// W0/W1：识别块（OCR blocks）校对与公式输入组件；语言包键 study.formula.* 由 Lead 统一合并。
import {
  attachBlockOverlay, buildConfirmBlocks, createFormulaInput, formulaText,
  parseBlocksWithFormulas, renderBlockLocation,
} from '../components/formulaInput.js';
import { openFormulaEditor } from '../components/formulaEditor.js';

const STORAGE_KEY = 'zhixu.uploadState';

function saveUploadSession(data) {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch { /* ignore */ }
}
function loadUploadSession() {
  try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY)); } catch { return null; }
}

async function rotateImageFile(file) {
  const bmp = await createImageBitmap(file);
  const canvas = document.createElement('canvas');
  canvas.width = bmp.height;
  canvas.height = bmp.width;
  const ctx = canvas.getContext('2d');
  ctx.translate(canvas.width / 2, canvas.height / 2);
  ctx.rotate(Math.PI / 2);
  ctx.drawImage(bmp, -bmp.width / 2, -bmp.height / 2);
  bmp.close();
  return new Promise(resolve => {
    canvas.toBlob(blob => {
      resolve(new File([blob], file.name, { type: 'image/jpeg' }));
    }, 'image/jpeg', 0.92);
  });
}

function openCropDialog(file, onCropped) {
  const dlg = el('dialog', { class: 'modal-dialog' });
  const canvas = el('canvas', { class: 'crop-canvas' });
  const hint = el('p', { class: 'help' }, t('upload.cropHint'));
  let bmp = null;
  let scale = 1;
  let rect = null;
  let dragging = false;
  let startX = 0;
  let startY = 0;

  function draw() {
    if (!bmp) return;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    if (rect && rect.w > 4 && rect.h > 4) {
      ctx.fillStyle = 'rgba(16, 43, 53, 0.45)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.clearRect(rect.x, rect.y, rect.w, rect.h);
      ctx.drawImage(
        bmp,
        rect.x / scale, rect.y / scale, rect.w / scale, rect.h / scale,
        rect.x, rect.y, rect.w, rect.h
      );
      ctx.strokeStyle = '#147d70';
      ctx.lineWidth = 2;
      ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
    }
  }

  function pointerPos(e) {
    const box = canvas.getBoundingClientRect();
    const cx = (e.touches ? e.touches[0].clientX : e.clientX) - box.left;
    const cy = (e.touches ? e.touches[0].clientY : e.clientY) - box.top;
    return [
      Math.max(0, Math.min(canvas.width, cx * (canvas.width / box.width))),
      Math.max(0, Math.min(canvas.height, cy * (canvas.height / box.height))),
    ];
  }

  const onDown = e => {
    e.preventDefault();
    dragging = true;
    [startX, startY] = pointerPos(e);
    rect = { x: startX, y: startY, w: 0, h: 0 };
  };
  const onMove = e => {
    if (!dragging) return;
    e.preventDefault();
    const [x, y] = pointerPos(e);
    rect = { x: Math.min(startX, x), y: Math.min(startY, y), w: Math.abs(x - startX), h: Math.abs(y - startY) };
    draw();
  };
  const onUp = () => { dragging = false; };

  canvas.addEventListener('mousedown', onDown);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  window.addEventListener('touchend', onUp);
  window.addEventListener('touchcancel', onUp);
  canvas.addEventListener('touchstart', onDown, { passive: false });
  canvas.addEventListener('touchmove', onMove, { passive: false });

  const confirmCropBtn = button(t('upload.confirmCrop'), async () => {
    if (!bmp || !rect || rect.w < 12 || rect.h < 12) {
      toast(t('upload.cropRequired'), true);
      return;
    }
    const sx = Math.round(rect.x / scale);
    const sy = Math.round(rect.y / scale);
    const sw = Math.max(10, Math.round(rect.w / scale));
    const sh = Math.max(10, Math.round(rect.h / scale));
    const out = document.createElement('canvas');
    out.width = sw;
    out.height = sh;
    out.getContext('2d').drawImage(bmp, sx, sy, sw, sh, 0, 0, sw, sh);
    out.toBlob(blob => {
      onCropped(new File([blob], file.name, { type: 'image/jpeg' }));
      dlg.close();
    }, 'image/jpeg', 0.92);
  }, '', 'check');

  dlg.append(
    el('div', { class: 'card-header' }, el('h3', {}, t('upload.cropTitle')), button(t('common.cancel'), () => dlg.close(), 'ghost small', 'close')),
    hint,
    el('div', { class: 'crop-canvas-wrap' }, canvas),
    el('div', { class: 'row end', style: 'margin-top:14px' },
      button(t('upload.resetRegion'), () => { rect = null; draw(); }, 'secondary small'),
      confirmCropBtn)
  );
  document.body.append(dlg);
  dlg.addEventListener('close', () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    window.removeEventListener('touchend', onUp);
    window.removeEventListener('touchcancel', onUp);
    if (bmp) bmp.close();
    dlg.remove();
  });
  dlg.showModal();

  createImageBitmap(file).then(loaded => {
    bmp = loaded;
    const maxW = 560;
    const maxH = 420;
    scale = Math.min(1, maxW / bmp.width, maxH / bmp.height);
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    draw();
  });
}

// 讲解模式名来自后端目录（数据），已知 id 用本地语言包覆盖，未知 id 保持后端文案。
function localizedModeText(mode, field) {
  if (!mode?.id) return mode?.[field] || '';
  const key = `upload.mode.${mode.id}.${field}`;
  const value = t(key);
  return value === key ? (mode[field] || '') : value;
}

function splitList(value) {
  return String(value || '').split(/[;；\n,，、]+/).map(item => item.trim()).filter(Boolean);
}

/** 答案只按分号/换行拆分：数学答案里的逗号（如「x=1, y=2」）必须保留。 */
function splitAnswers(value) {
  return String(value || '').split(/[;；\n]+/).map(item => item.trim()).filter(Boolean);
}

function parseOptionsText(value) {
  return String(value || '').split('\n').map(line => line.trim()).filter(Boolean).map((line, index) => {
    const matched = line.match(/^([A-La-l])\s*[.．、:：)）]\s*(.+)$/);
    if (matched) return { label: matched[1].toUpperCase(), text: matched[2].trim() };
    return { label: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[index] || String(index + 1), text: line };
  });
}

function optionsToText(options) {
  return (options || []).map(option => `${option.label}. ${option.text}`).join('\n');
}

function intakeRecordLabel(record) {
  const source = record?.source || '';
  const key = {
    local_ocr: 'upload.intake.sourceLocal',
    vision: 'upload.intake.sourceVision',
    text: 'upload.intake.sourceText',
    manual: 'upload.intake.sourceManual',
  }[source] || 'upload.intake.sourceLocal';
  const translated = t(key);
  return translated === key ? source || t('upload.intake.sourceLocal') : translated;
}

export async function renderUpload(container, version, params = null) {
  const modes = await ensureExplainModes().catch(() => [
    { id: 'detailed', name: t('upload.mode.detailed.name'), short: t('upload.mode.detailed.short') },
    { id: 'pattern_teaching', name: t('upload.mode.pattern_teaching.name'), short: t('upload.mode.pattern_teaching.short') },
    { id: 'standard_steps', name: t('upload.mode.standard_steps.name'), short: t('upload.mode.standard_steps.short') },
    { id: 'extension', name: t('upload.mode.extension.name'), short: t('upload.mode.extension.short') },
  ]);
  if (!current(version)) return;

  // Restore sessionStorage state so page refresh doesn't lose uploaded question (§8)
  const restored = loadUploadSession();
  if (restored?.upload && !state.questionUpload) {
    state.questionUpload = restored.upload;
  }
  if (restored?.intake && !state.uploadIntake) {
    state.uploadIntake = restored.intake;
  }
  let intake = state.uploadIntake || null;
  let lastSaved = null;
  let lastSimilar = [];

  const pendingFiles = [];
  const previewUrls = [];
  function clearPreviewUrls() {
    while (previewUrls.length) URL.revokeObjectURL(previewUrls.pop());
  }

  const uploadCard = el('section', { class: 'card pg-step-card', id: 'upload-step-1' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '1'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, t('upload.step1.title')),
        el('p', { class: 'pg-card-sub' }, t('upload.step1.sub'))),
      badge(t('upload.pasteBadge'), 'teal')));

  const filePreviewGrid = el('div', { class: 'upload-thumbs' });
  const uploadedInfo = el('div');

  function renderPendingFiles() {
    clearPreviewUrls();
    filePreviewGrid.replaceChildren();
    pendingFiles.forEach((f, idx) => {
      const item = el('div', { class: 'thumb-item' });
      if (f.type.startsWith('image/')) {
        const u = URL.createObjectURL(f);
        previewUrls.push(u);
        item.append(el('img', { src: u, alt: f.name }));
      } else {
        item.append(el('div', { class: 'thumb-text' }, icon('file'), f.name));
      }
      const ctrls = el('div', { class: 'row between', style: 'margin-top:6px;gap:4px' },
        f.type.startsWith('image/')
          ? el('div', { class: 'row', style: 'gap:4px' },
              button(t('upload.crop'), () => openCropDialog(pendingFiles[idx], cropped => {
                pendingFiles[idx] = cropped;
                renderPendingFiles();
                toast(t('upload.cropped'));
              }), 'ghost small', 'scan'),
              button(t('upload.rotate'), async () => {
                pendingFiles[idx] = await rotateImageFile(pendingFiles[idx]);
                renderPendingFiles();
              }, 'ghost small', 'rotate'))
          : el('span'),
        button(t('common.remove'), () => { pendingFiles.splice(idx, 1); renderPendingFiles(); }, 'ghost small', 'trash'));
      item.append(ctrls);
      filePreviewGrid.append(item);
    });
  }

  function addFiles(files) {
    const list = Array.isArray(files) ? files : [files];
    const replacingQuestion = Boolean(state.questionUpload) || uploadedInfo.childNodes.length > 0;
    for (const f of list) {
      if (pendingFiles.length >= 5) { toast(t('upload.maxFiles'), true); break; }
      pendingFiles.push(f);
    }
    renderPendingFiles();
    // 重新选图 = 换了一道题：重置步骤 2–4 的识别/讲解状态，避免旧题干串到新题。
    if (replacingQuestion) resetForNewImage();
  }

  const picker = filePicker(
    t('upload.filePickerTitle'),
    t('upload.filePickerHint'),
    'image/png,image/jpeg,image/webp,.txt,.md',
    addFiles,
    { multiple: true, capture: 'environment' }
  );
  attachPasteImage(uploadCard, f => { addFiles([f]); toast(t('upload.pasted')); });

  // Step 2: OCR-first draft + Editable confirmation + Similar bank search
  const confirmCard = el('section', { class: 'card pg-step-card', id: 'upload-step-2' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '2'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, t('upload.intake.recognizeTitle')),
        el('p', { class: 'pg-card-sub' }, t('upload.intake.recognizeSub'))),
      badge(t('upload.verifyBadge'), 'blue')));

  const recPicker = createModelPicker('ocr', { requireVision: true, showModeToggle: true });
  const aiPicker = createModelPicker('intake', { showModeToggle: false });
  const recJobMount = el('div');
  const recResultMount = el('div');
  const similarMount = el('div');
  const ocrStatusMount = el('div');
  const draftMetaMount = el('div');
  const pendingMount = el('div');
  const aiSuggestionMount = el('div');
  const versionHintMount = el('div');

  // ===== W0/W1：识别块校对（OCR blocks → 公式输入组件）状态 =====
  // blockState 是 parseBlocksWithFormulas() 的编辑态数组；blockMeta 汇总引擎能力与降级信息。
  const blockReviewMount = el('div', { class: 'upload-block-review' });
  // 按阅读顺序合并识别块（函数声明在下方，提升可用）。
  const mergeBlocksBtn = button(formulaText('study.formula.review.mergeAll'), () => mergeBlocksIntoStem(), 'ghost small', 'scan');
  const blockSection = el('div', { class: 'pg-substep upload-block-section', hidden: true },
    el('p', { class: 'pg-substep-label' }, formulaText('study.formula.review.sectionTitle')),
    el('p', { class: 'help' }, formulaText('study.formula.review.sectionSub')),
    el('div', { class: 'row' }, mergeBlocksBtn),
    blockReviewMount);
  let blockState = [];
  const blockMeta = {
    attempted: false, engine: '', engineVersion: '', capabilities: null, message: '', ocrAvailable: true,
  };
  let blockGeneration = 0;
  let blockRenderSignature = '';
  const blockEditors = new Map(); // 编辑态块 → createFormulaInput 实例（提交前同步实时值）
  let blockOverlays = [];
  const previewShells = []; // 原图预览容器（pageIndex 从 0 开始），供 attachBlockOverlay 使用
  let blockActiveId = null;

  const subjInput = select(
    ['', '数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '其他'].map(s => ({ value: s, label: s || t('upload.subjectPlaceholder') })),
    restored?.recognized?.subject || ''
  );
  const typeInput = input(t('upload.typePlaceholder'), { value: restored?.recognized?.type || '' });
  const stemInput = el('textarea', {
    rows: 5,
    placeholder: t('upload.stemPlaceholder'),
    value: restored?.recognized?.stem || state.questionUpload?.extracted_text || '',
  });
  const optionsInput = el('textarea', { rows: 3, placeholder: t('upload.intake.optionsPlaceholder') });
  const answersInput = input(t('upload.intake.answersPlaceholder'));
  const explanationInput = el('textarea', { rows: 3, placeholder: t('upload.intake.explanationPlaceholder') });
  const tagsInput = input(t('upload.intake.tagsPlaceholder'));
  const condInput = input(t('upload.condPlaceholder'), { value: (restored?.recognized?.conditions || []).join('；') });

  function uploadId() {
    return state.questionUpload?.id || null;
  }

  function readForm() {
    return {
      subject: subjInput.value,
      type: typeInput.value.trim(),
      stem: stemInput.value.trim(),
      options: parseOptionsText(optionsInput.value),
      answers: splitAnswers(answersInput.value),
      explanation: explanationInput.value.trim(),
      knowledge_tags: splitList(tagsInput.value),
      conditions: splitList(condInput.value),
    };
  }

  function fillForm(record, { force = false } = {}) {
    if (!record) return;
    const canFill = value => force || !String(value || '').trim();
    if (record.subject && canFill(subjInput.value)) subjInput.value = record.subject;
    if (record.type && canFill(typeInput.value)) typeInput.value = record.type;
    if (record.stem && canFill(stemInput.value)) stemInput.value = record.stem;
    if (record.options?.length && canFill(optionsInput.value)) optionsInput.value = optionsToText(record.options);
    if (record.answers?.length && canFill(answersInput.value)) answersInput.value = (record.answers || []).join('；');
    if (record.explanation && canFill(explanationInput.value)) explanationInput.value = record.explanation;
    if (record.knowledge_tags?.length && canFill(tagsInput.value)) tagsInput.value = (record.knowledge_tags || []).join('；');
    if (record.conditions?.length && canFill(condInput.value)) condInput.value = (record.conditions || []).join('；');
  }

  function clearForm() {
    subjInput.value = '';
    typeInput.value = '';
    stemInput.value = '';
    optionsInput.value = '';
    answersInput.value = '';
    explanationInput.value = '';
    tagsInput.value = '';
    condInput.value = '';
  }

  function setIntake(next) {
    intake = next || null;
    state.uploadIntake = intake;
    saveUploadSession({ upload: state.questionUpload, intake });
  }

  function renderSimilarList(similar) {
    lastSimilar = similar || [];
    similarMount.replaceChildren();
    if (!similar?.length) return;
    const exact = similar.find(s => s.exact_match);
    const box = el('div', { class: `notice${exact ? '' : ' warning'}`, style: 'margin-top:12px' },
      el('strong', {}, exact ? t('upload.similarExact') : t('upload.similarFound')));
    const list = el('div', { class: 'mini-list', style: 'margin-top:8px' });
    for (const s of similar) {
      list.append(el('div', { class: 'mini-item', style: 'padding:8px 0' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, `${s.subject} · ${s.year || ''} ${s.province || ''} · ${t('upload.similarity', { percent: Math.round(s.similarity * 100) })}`),
          el('p', {}, readableSummary(s, 220))),
        button(t('upload.goBank'), () => go('practice', s.qid), 'soft small', 'book')));
    }
    box.append(list);
    similarMount.append(box);
  }

  // 识别结果卡展示学科 / 题型标签；双模型识别有差异时展开对照，并提示重点核对项。
  function renderRecognitionResult(result) {
    recResultMount.replaceChildren();
    const rec = result?.recognized;
    if (!rec) return;
    const secondary = result?.secondary_recognized || null;
    const differences = Array.isArray(result?.differences) ? result.differences : [];
    const tags = el('div', { class: 'row', style: 'margin-bottom:8px' },
      badge(rec.subject || t('upload.subjectPending'), 'teal'),
      badge(t('upload.typeBadge', { type: rec.type || t('upload.typePending') }), 'blue'),
      (rec.uncertain || []).length ? badge(t('upload.uncertain', { count: rec.uncertain.length }), 'amber') : null);
    const box = el('div', { class: 'notice', style: 'margin-top:12px' },
      el('strong', {}, t('upload.recFilled')),
      tags,
      el('p', { class: 'small-text', style: 'margin:6px 0 0' }, readableSummary({ stem_plain: rec.stem || '' }, 180)));
    if (differences.length) {
      box.append(el('div', { class: 'notice warning', style: 'margin-top:10px;margin-bottom:0' },
        t('upload.recDiff', { items: differences.join(t('common.listSeparator')) })));
    }
    if (secondary) {
      const detail = el('details', { class: 'sources', style: 'margin-top:10px' },
        el('summary', {}, differences.length ? t('upload.recCompare') : t('upload.recSecond')));
      const grid = el('div', { class: 'grid-two' });
      for (const [label, model] of [[t('upload.recModelA'), rec], [t('upload.recModelB'), secondary]]) {
        grid.append(el('div', { class: 'source-block' },
          el('strong', {}, label),
          el('div', { class: 'small-text' }, t('upload.recSubjectLine', { subject: model.subject || '—', type: model.type || '—' })),
          el('p', { class: 'small-text', style: 'white-space:pre-wrap;margin:6px 0 0' },
            readableSummary({ stem_plain: model.stem || '' }, 320))));
      }
      detail.append(grid);
      box.append(detail);
    }
    recResultMount.append(box);
  }

  function renderDraftMeta() {
    draftMetaMount.replaceChildren();
    if (!intake?.draft) return;
    const draft = intake.draft;
    const row = el('div', { class: 'row', style: 'margin:10px 0;flex-wrap:wrap' },
      badge(intakeRecordLabel(draft), draft.source === 'vision' ? 'blue' : 'teal'),
      badge(t('upload.intake.versionBadge', { v: intake.revision }), ''),
      (draft.uncertain || []).length ? badge(t('upload.uncertain', { count: draft.uncertain.length }), 'amber') : null);
    draftMetaMount.append(row);
    if ((draft.uncertain || []).length) {
      draftMetaMount.append(notice(
        t('upload.intake.uncertain', { items: (draft.uncertain || []).slice(0, 4).join(t('common.listSeparator')) }),
        'warning'));
    }
    if (draft.ocr_text) {
      draftMetaMount.append(el('details', { class: 'sources' },
        el('summary', {}, t('upload.intake.ocrTextTitle')),
        el('pre', { class: 'small-text', style: 'white-space:pre-wrap;max-height:220px;overflow:auto' }, draft.ocr_text)));
    }
    if (draft.ocr_notice) {
      draftMetaMount.append(el('p', { class: 'help' }, draft.ocr_notice));
    }
  }

  function renderPendingDraft() {
    pendingMount.replaceChildren();
    if (!intake?.pending_draft) return;
    const pending = intake.pending_draft;
    pendingMount.append(
      el('div', { class: 'notice warning', style: 'margin-top:12px' },
        el('strong', {}, t('upload.intake.pendingTitle')),
        el('p', { class: 'small-text', style: 'margin:6px 0' }, t('upload.intake.pendingNotice')),
        el('p', { class: 'small-text', style: 'white-space:pre-wrap;margin:0 0 8px' },
          readableSummary({ stem_plain: pending.stem || '' }, 260)),
        el('div', { class: 'row' },
          button(t('upload.intake.pendingAdopt'), () => {
            fillForm(pending, { force: true });
            toast(t('upload.intake.pendingAdopted'));
            confirmCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 'secondary small', 'check'))));
  }

  function renderAiSuggestion() {
    aiSuggestionMount.replaceChildren();
    const suggestion = intake?.ai_suggestion;
    if (!suggestion) return;
    const badgeNode = badge(t('upload.intake.aiBadge'), 'amber');
    const box = el('div', { class: 'notice', style: 'margin-top:12px' },
      el('div', { class: 'row between', style: 'align-items:center' },
        el('strong', {}, t('upload.intake.aiTitle')), badgeNode),
      el('p', { class: 'help', style: 'margin:6px 0' }, t('upload.intake.aiHint')));
    const answers = (suggestion.answers || []).join('；');
    if (answers) {
      box.append(el('p', { class: 'small-text', style: 'margin:4px 0' },
        el('strong', {}, t('upload.intake.aiAnswerLabel') + '：'), answers));
    }
    if ((suggestion.uncertain || []).length) {
      box.append(el('p', { class: 'help' }, t('upload.intake.uncertain', {
        items: suggestion.uncertain.slice(0, 4).join(t('common.listSeparator')),
      })));
    }
    if (suggestion.explanation) {
      box.append(el('details', { class: 'sources' },
        el('summary', {}, t('upload.intake.aiExplanationLabel')),
        el('p', { class: 'small-text', style: 'white-space:pre-wrap' }, suggestion.explanation)));
    }
    const adoptRow = el('div', { class: 'row', style: 'margin-top:10px;flex-wrap:wrap' },
      answers ? button(t('upload.intake.adoptAnswers'), () => {
        answersInput.value = (suggestion.answers || []).join('；');
        toast(t('upload.intake.adopted', { field: t('upload.intake.aiAnswerLabel') }));
      }, 'ghost small', 'check') : null,
      suggestion.explanation ? button(t('upload.intake.adoptExplanation'), () => {
        explanationInput.value = suggestion.explanation;
        toast(t('upload.intake.adopted', { field: t('upload.intake.aiExplanationLabel') }));
      }, 'ghost small', 'check') : null,
      (suggestion.knowledge_tags || []).length ? button(t('upload.intake.adoptTags'), () => {
        tagsInput.value = (suggestion.knowledge_tags || []).join('；');
        toast(t('upload.intake.adopted', { field: t('upload.intake.tagsLabel') }));
      }, 'ghost small', 'check') : null);
    box.append(adoptRow);
    aiSuggestionMount.append(box);
  }

  function renderVersionHint() {
    if (!intake) return null;
    return el('p', { class: 'help' },
      t('upload.intake.versionBadge', { v: intake.revision }) + ' · ' + t('upload.intake.confirmHint'));
  }

  /* --------------------------------------------------------------------- *
   * W0/W1：识别块校对（OCR blocks ↔ 公式输入组件）
   * - 契约：POST /ocr-draft 与 GET /intake 的 draft/confirmed 里可带 `blocks`
   *   （intake-blocks-v1），PATCH /confirm 可选回传人工校对后的 `blocks`；
   * - 兼容：旧后端没有 blocks 时本区整体隐藏，原有手工录入流程完全不变；
   * - 安全：块文本一律 textContent / el() 渲染，公式预览由组件内部经 KaTeX 净化。
   * --------------------------------------------------------------------- */

  const BLOCK_LOW_CONFIDENCE = 0.8;
  const BLOCK_KIND_BADGES = { text: '', math: 'blue', table: 'amber', image: 'teal' };

  function blockKindLabel(kind) {
    return formulaText(`study.formula.review.kind.${BLOCK_KIND_BADGES[kind] === undefined ? 'text' : kind}`);
  }

  function blockPageOf(block) {
    const page = Number(block?.page);
    return Number.isFinite(page) && page >= 1 ? Math.floor(page) : 1;
  }

  function blockConfidenceOf(block) {
    const value = Number(block?.confidence);
    return Number.isFinite(value) ? value : null;
  }

  /** 从 OCR/入库响应里取识别块：顶层 blocks 优先，其次 confirmed / draft / intake 内嵌。 */
  function extractResponseBlocks(payload) {
    if (!payload || typeof payload !== 'object') return null;
    const candidates = [
      payload.blocks,
      payload.confirmed?.blocks,
      payload.draft?.blocks,
      payload.intake?.blocks,
      payload.intake?.confirmed?.blocks,
      payload.intake?.draft?.blocks,
    ];
    for (const candidate of candidates) {
      if (Array.isArray(candidate)) return candidate;
    }
    return null;
  }

  /** 从响应里取引擎/能力/提示信息（缺字段时用空值，不覆盖已有信息）。 */
  function extractBlockMeta(payload) {
    if (!payload || typeof payload !== 'object') return {};
    const draft = payload.draft && typeof payload.draft === 'object' ? payload.draft : {};
    const capabilities = payload.capabilities || draft.capabilities || undefined;
    const engine = payload.engine || draft.engine || '';
    const engineVersion = payload.engine_version || draft.engine_version || '';
    const message = typeof payload.message === 'string' ? payload.message : '';
    const meta = {
      capabilities,
      engine: String(engine || ''),
      engineVersion: String(engineVersion || ''),
      message,
    };
    if (typeof payload.ocr_available === 'boolean') meta.ocrAvailable = payload.ocr_available;
    return meta;
  }

  function destroyBlockEditors() {
    for (const editor of blockEditors.values()) {
      try { editor.destroy(); } catch { /* 单个组件销毁异常不影响页面 */ }
    }
    blockEditors.clear();
    destroyBlockOverlays();
  }

  function destroyBlockOverlays() {
    for (const overlay of blockOverlays) {
      try { overlay.destroy(); } catch { /* 忽略 */ }
    }
    blockOverlays = [];
  }

  function clearBlockReviewState() {
    destroyBlockEditors();
    blockState = [];
    blockMeta.attempted = false;
    blockMeta.engine = '';
    blockMeta.engineVersion = '';
    blockMeta.capabilities = null;
    blockMeta.message = '';
    blockMeta.ocrAvailable = true;
    blockGeneration += 1;
    blockRenderSignature = '';
    blockActiveId = null;
    previewShells.length = 0;
    blockReviewMount.replaceChildren();
    blockSection.hidden = true;
  }

  function blockReviewSignature() {
    const katexReady = typeof window !== 'undefined' && Boolean(window.katex);
    return [
      blockGeneration,
      blockMeta.attempted ? '1' : '0',
      blockMeta.engine,
      blockMeta.engineVersion,
      JSON.stringify(blockMeta.capabilities || null),
      blockMeta.ocrAvailable ? '1' : '0',
      katexReady ? '1' : '0',
      previewShells.length,
    ].join('|');
  }

  function updateBlockSummary() {
    const node = blockReviewMount.querySelector('.upload-block-summary');
    if (!node) return;
    const reviewCount = blockState.filter(block => block.needsReview).length;
    const lowCount = blockState.filter(block => {
      const confidence = blockConfidenceOf(block);
      return confidence !== null && confidence < BLOCK_LOW_CONFIDENCE;
    }).length;
    let text = formulaText('study.formula.review.blockCount', { count: blockState.length, review: reviewCount });
    if (lowCount) text += ` · ${formulaText('study.formula.review.lowConfidenceCount', { count: lowCount })}`;
    node.textContent = text;
  }

  function setBlockActive(id) {
    blockActiveId = id;
    for (const overlay of blockOverlays) {
      try { overlay.setActive(id); } catch { /* 忽略 */ }
    }
    for (const item of blockReviewMount.querySelectorAll('.upload-block-item')) {
      item.classList.toggle('is-active', item.dataset.blockId === String(id));
    }
  }

  /** 点击原图叠加框 → 高亮并滚动到对应识别块。 */
  function pickBlockFromOverlay(id) {
    setBlockActive(id);
    const target = [...blockReviewMount.querySelectorAll('.upload-block-item')]
      .find(item => item.dataset.blockId === String(id));
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function syncBlockItemState(item, block) {
    if (!item) return;
    item.classList.toggle('needs-review', Boolean(block.needsReview));
    item.classList.toggle('is-edited', Boolean(block.changed));
    // 同步原图叠加框的「需校对」虚线样式（不改位置，不重绘整个叠加层）。
    for (const overlay of blockOverlays) {
      if (!overlay || !overlay.element) continue;
      const box = [...overlay.element.querySelectorAll('.block-overlay-box')]
        .find(node => node.dataset.blockId === String(block.id));
      if (box) box.classList.toggle('needs-review', Boolean(block.needsReview));
    }
    const stateBadge = item.querySelector('.upload-block-review-state');
    if (stateBadge) {
      stateBadge.className = `badge upload-block-review-state${block.needsReview ? ' amber' : ' teal'}`;
      stateBadge.textContent = block.needsReview
        ? formulaText('study.formula.review.needsReview')
        : formulaText('study.formula.review.reviewed');
    }
    const reviewBtn = item.querySelector('.upload-block-review-btn');
    if (reviewBtn) reviewBtn.disabled = !block.needsReview;
  }

  function syncStemInput() {
    stemInput.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /** 在题干编辑器光标处插入片段（「插入公式」用 `$latex$`）。 */
  function insertIntoStem(snippet) {
    const source = stemInput.value;
    let start = Number.isInteger(stemInput.selectionStart) ? stemInput.selectionStart : source.length;
    let end = Number.isInteger(stemInput.selectionEnd) ? stemInput.selectionEnd : start;
    start = Math.min(Math.max(0, start), source.length);
    end = Math.min(Math.max(start, end), source.length);
    stemInput.value = `${source.slice(0, start)}${snippet}${source.slice(end)}`;
    const caret = Math.min(stemInput.value.length, start + snippet.length);
    try { stemInput.setSelectionRange(caret, caret); } catch { /* 个别环境不支持选区 */ }
    syncStemInput();
    stemInput.focus();
  }

  function appendToStem(content) {
    const current = String(stemInput.value || '').trimEnd();
    stemInput.value = current ? `${current}\n${content}` : content;
    syncStemInput();
    stemInput.focus();
  }

  function insertBlockFormula(block) {
    const latex = String(block.latexInput || '').trim();
    if (!latex) {
      toast(formulaText('study.formula.review.emptyLatex'), true);
      return;
    }
    insertIntoStem(`$${latex}$`);
    toast(formulaText('study.formula.review.inserted'));
  }

  function adoptBlockAsStem(block) {
    const latex = String(block.latexInput || '').trim();
    const content = block.kind === 'math' && latex ? `$${latex}$` : String(block.text || '').trim();
    if (!content) {
      toast(formulaText('study.formula.review.blockEmpty'), true);
      return;
    }
    appendToStem(content);
    toast(formulaText('study.formula.review.adopted'));
  }

  /** 按阅读顺序把识别块合并重建题干：text 块取原文，公式块取 `$latex$`（无 LaTeX 回退原文）。 */
  function mergeBlocksIntoStem() {
    if (!blockState.length) {
      toast(formulaText('study.formula.review.mergeEmpty'), true);
      return;
    }
    // 以公式输入组件的实时值为准（与 currentBlocksPayload 同一口径），避免合并到旧值。
    for (const [block, editor] of blockEditors) {
      block.latexInput = editor.getValue();
    }
    const lines = [];
    for (const block of blockState) {
      if (!block || String(block.kind || '') === 'image') continue; // 识别层不产图片块；预留类型不入题干
      const latex = String(block.kind || '') === 'math' ? String(block.latexInput || '').trim() : '';
      const text = String(block.text || '').trim();
      const content = latex ? `$${latex}$` : text;
      if (content) lines.push(content);
    }
    if (!lines.length) {
      toast(formulaText('study.formula.review.mergeEmpty'), true);
      return;
    }
    stemInput.value = lines.join('\n').slice(0, 20000); // 与后端 stem 上限一致
    syncStemInput();
    toast(formulaText('study.formula.review.mergeDone'));
  }

  function markBlockReviewed(block, item) {
    block.changed = true;
    block.needsReview = false;
    syncBlockItemState(item, block);
    updateBlockSummary();
    toast(formulaText('study.formula.review.marked'));
  }

  function renderBlockActions(block, item) {
    const actions = el('div', { class: 'upload-block-actions' });
    if (block.kind === 'math') {
      actions.append(button(formulaText('study.formula.review.insertFormula'),
        () => insertBlockFormula(block), 'ghost small', 'plus'));
    }
    actions.append(button(formulaText('study.formula.review.useAsStem'),
      () => adoptBlockAsStem(block), 'ghost small', 'check'));
    const reviewBtn = el('button', {
      type: 'button',
      class: 'button ghost small upload-block-review-btn',
      disabled: !block.needsReview,
      onClick: () => markBlockReviewed(block, item),
    }, icon('check'), el('span', {}, block.needsReview
      ? formulaText('study.formula.review.markReviewed')
      : formulaText('study.formula.review.reviewed')));
    actions.append(reviewBtn);
    return actions;
  }

  function renderBlockItem(block) {
    const item = el('div', {
      class: 'upload-block-item',
      'data-block-id': String(block.id),
    });
    item.dataset.blockId = String(block.id);
    if (block.needsReview) item.classList.add('needs-review');
    if (block.changed) item.classList.add('is-edited');
    if (blockActiveId !== null && String(blockActiveId) === String(block.id)) item.classList.add('is-active');

    const head = el('div', { class: 'upload-block-head' },
      badge(blockKindLabel(block.kind), BLOCK_KIND_BADGES[block.kind] || ''),
      el('button', {
        type: 'button',
        class: 'upload-block-location',
        title: formulaText('study.formula.review.overlayHint'),
        'aria-label': `${blockKindLabel(block.kind)} · ${renderBlockLocation(block)}`,
        onClick: () => setBlockActive(block.id),
      }, renderBlockLocation(block)));
    const confidence = blockConfidenceOf(block);
    if (confidence !== null) {
      const confidenceNode = el('span', { class: 'upload-block-confidence' },
        formulaText('study.formula.review.confidence', { percent: Math.round(confidence * 100) }));
      if (confidence < BLOCK_LOW_CONFIDENCE) {
        confidenceNode.append(badge(formulaText('study.formula.review.lowConfidence'), 'amber'));
      }
      head.append(confidenceNode);
    }
    head.append(el('span', {
      class: `badge upload-block-review-state${block.needsReview ? ' amber' : ' teal'}`,
    }, block.needsReview
      ? formulaText('study.formula.review.needsReview')
      : formulaText('study.formula.review.reviewed')));
    item.append(head);

    if (block.kind === 'math') {
      // 公式块：createFormulaInput 编辑 latex（值模型不含 $ 定界符），改动即视为已校对。
      const editor = createFormulaInput({
        value: block.latexInput,
        placeholder: formulaText('study.formula.review.formulaPlaceholder'),
        ariaLabel: formulaText('study.formula.review.mathEditorAria', { id: block.id }),
        rows: 2,
        allowPlainFallback: true,
        onChange: value => {
          block.latexInput = value;
          block.changed = true;
          block.needsReview = false;
          syncBlockItemState(item, block);
          updateBlockSummary();
        },
      });
      blockEditors.set(block, editor);
      if (editor.textarea) {
        // 实时同步编辑值（提交前 currentBlocksPayload 还会再取一次组件值兜底）。
        editor.textarea.addEventListener('input', () => {
          block.latexInput = editor.getValue();
          block.changed = true;
          block.needsReview = false;
          syncBlockItemState(item, block);
          updateBlockSummary();
        });
      }
      const visualEdit = button(formulaText('study.oss.formula.visualEdit'), async () => {
        try {
          const value = await openFormulaEditor({
            initialLatex: block.latexInput,
            title: formulaText('study.oss.formula.dialogTitle'),
          });
          if (value === null) return;
          block.latexInput = value;
          block.changed = true;
          block.needsReview = false;
          editor.setValue?.(value);
          syncBlockItemState(item, block);
          updateBlockSummary();
          toast(formulaText('study.oss.formula.inserted'));
        } catch {
          toast(formulaText('study.oss.formula.loadFailed'), true);
        }
      }, 'secondary');
      item.append(editor.element,
        el('div', { class: 'oss-formula-actions' }, visualEdit));
    } else if (block.kind === 'text') {
      const editor = el('textarea', {
        class: 'upload-block-text',
        rows: '2',
        maxlength: '4000',
        placeholder: formulaText('study.formula.review.textPlaceholder'),
        'aria-label': formulaText('study.formula.review.textEditorAria', { id: block.id }),
        value: block.text,
      });
      editor.addEventListener('input', () => {
        block.text = editor.value;
        block.changed = true;
        block.needsReview = false;
        syncBlockItemState(item, block);
        updateBlockSummary();
      });
      item.append(editor);
    } else if (block.kind === 'table') {
      // 表格块：有结构时展示经 DOMPurify 兜底的只读预览，文字仍可对照原图校对。
      const tableHtml = String(block.original?.table_html || block.table_html || '').trim();
      if (tableHtml) {
        try {
          const template = document.createElement('template');
          template.innerHTML = sanitizeTableHtml(tableHtml);
          const table = template.content.querySelector('table');
          if (table) {
            item.append(el('span', { class: 'badge teal' },
              formulaText('study.oss.table.structure')));
            item.append(el('div', { class: 'table-wrap upload-block-table-preview' }, table));
          }
        } catch {
          /* 净化器不可用时只显示文字，不插入未净化 HTML。 */
        }
      }
      if (block.text) item.append(el('pre', { class: 'upload-block-readonly-text' }, block.text));
      item.append(el('p', { class: 'upload-block-readonly' },
        formulaText('study.formula.review.keepOriginal')));
    } else {
      // 图片块只读：保持原图，不做重绘或转换。
      if (block.text) item.append(el('pre', { class: 'upload-block-readonly-text' }, block.text));
      item.append(el('p', { class: 'upload-block-readonly' },
        formulaText('study.formula.review.keepOriginal')));
    }
    item.append(renderBlockActions(block, item));
    return item;
  }

  function attachBlockOverlays() {
    destroyBlockOverlays();
    if (!blockState.length) return;
    if (!previewShells.length) {
      blockReviewMount.append(el('p', { class: 'help upload-block-overlay-hint' },
        formulaText('study.formula.review.noPreview')));
      return;
    }
    let attached = 0;
    for (const preview of previewShells) {
      const pageBlocks = blockState.filter(block => blockPageOf(block) === preview.pageIndex + 1);
      if (!pageBlocks.length) continue;
      const overlay = attachBlockOverlay(preview.img, preview.shell, pageBlocks, id => pickBlockFromOverlay(id));
      blockOverlays.push(overlay);
      attached += 1;
      if (blockActiveId !== null) overlay.setActive(blockActiveId);
    }
    blockReviewMount.append(el('p', { class: 'help upload-block-overlay-hint' },
      formulaText(attached ? 'study.formula.review.overlayHint' : 'study.formula.review.noPreview')));
  }

  function renderBlockReview() {
    if (!blockMeta.attempted && !blockState.length) {
      blockSection.hidden = true;
      return;
    }
    blockSection.hidden = false;
    const nextSignature = blockReviewSignature();
    if (nextSignature === blockRenderSignature) {
      // 结构未变时只刷新统计，避免重绘打断正在编辑的输入框。
      updateBlockSummary();
      return;
    }
    blockRenderSignature = nextSignature;
    destroyBlockEditors();
    blockActiveId = null;
    blockReviewMount.replaceChildren();

    const katexReady = typeof window !== 'undefined' && Boolean(window.katex);
    const mathRecognition = blockMeta.capabilities && typeof blockMeta.capabilities === 'object'
      ? blockMeta.capabilities.math_recognition
      : null;
    // 引擎行：有名称才显示「引擎：…」，有版本再补版本；都没有时显示「未知引擎」。
    const engineText = blockMeta.engine
      ? (blockMeta.engineVersion
        ? formulaText('study.formula.review.engineLine', { engine: blockMeta.engine, version: blockMeta.engineVersion })
        : formulaText('study.formula.review.engineOnly', { engine: blockMeta.engine }))
      : formulaText('study.formula.review.engineUnknown');
    const statusBox = el('div', { class: 'upload-block-status' },
      el('div', { class: 'row', style: 'flex-wrap:wrap;gap:8px' },
        badge(engineText, 'blue'),
        mathRecognition === false
          ? badge(formulaText('study.formula.review.mathUnsupportedBadge'), 'amber')
          : mathRecognition === true
            ? badge(formulaText('study.formula.review.mathSupported'), 'teal')
            : null));
    // 默认引擎不做公式识别：常驻中文提示「公式块需人工校对；本机引擎暂不做公式识别」。
    if (mathRecognition !== true) {
      statusBox.append(el('p', { class: 'help' }, formulaText('study.formula.review.engineHint')));
    }
    if (mathRecognition === false) {
      statusBox.append(el('p', { class: 'help' }, formulaText('study.formula.review.mathUnsupported')));
    }
    if (!katexReady) {
      statusBox.append(el('p', { class: 'help' }, formulaText('study.formula.review.katexMissing')));
    }
    statusBox.append(el('p', { class: 'help upload-block-summary' }));

    if (!blockState.length) {
      // 纯文本/手工录入回退：OCR 不可用或本次没有 blocks 时页面行为与原来一致。
      const degradedKey = blockMeta.ocrAvailable === false
        ? 'study.formula.review.ocrUnavailable'
        : 'study.formula.review.noBlocks';
      blockReviewMount.append(statusBox,
        el('div', { class: 'upload-block-degraded' }, formulaText(degradedKey)));
      return;
    }

    const list = el('div', { class: 'upload-block-list' });
    for (const block of blockState) list.append(renderBlockItem(block));
    blockReviewMount.append(statusBox, list);
    if (blockMeta.message) blockReviewMount.append(el('p', { class: 'help' }, blockMeta.message));
    attachBlockOverlays();
    updateBlockSummary();
  }

  /**
   * 把响应里的 blocks 灌入编辑态。
   * force=true 时以服务端数据重置本地编辑（OCR 重新识别 / 深链加载 / 确认成功回包）；
   * force=false 时只在本地还没有块时初始化，避免刷新把用户正在改的内容覆盖掉。
   */
  function ingestBlocksFrom(payload, { attempted = true, force = false } = {}) {
    const meta = extractBlockMeta(payload);
    if (meta.capabilities !== undefined) blockMeta.capabilities = meta.capabilities;
    if (meta.engine) blockMeta.engine = meta.engine;
    if (meta.engineVersion) blockMeta.engineVersion = meta.engineVersion;
    if (meta.message) blockMeta.message = meta.message;
    if (meta.ocrAvailable !== undefined) blockMeta.ocrAvailable = meta.ocrAvailable;
    if (attempted) blockMeta.attempted = true;
    const blocks = extractResponseBlocks(payload);
    if (Array.isArray(blocks) && blocks.length) {
      const first = blocks.find(item => item && typeof item === 'object');
      if (first && !blockMeta.engine && typeof first.engine === 'string') blockMeta.engine = first.engine;
      if (first && !blockMeta.engineVersion && typeof first.engine_version === 'string') {
        blockMeta.engineVersion = first.engine_version;
      }
    }
    const shouldReset = force || (!blockState.length && Array.isArray(blocks) && blocks.length > 0);
    if (shouldReset) {
      blockState = parseBlocksWithFormulas(Array.isArray(blocks) ? blocks : []);
      blockGeneration += 1;
      blockRenderSignature = '';
      blockActiveId = null;
    }
    renderBlockReview();
  }

  /** 提交前收集识别块：以组件实时值为准，再还原成契约 block 数组。 */
  function currentBlocksPayload() {
    if (!blockState.length) return null;
    for (const [block, editor] of blockEditors) {
      block.latexInput = editor.getValue();
    }
    return buildConfirmBlocks(blockState);
  }

  async function refreshIntake({ fill = false } = {}) {
    const id = uploadId();
    if (!id) return null;
    try {
      const data = await api(`/uploads/${id}/intake`);
      setIntake(data);
      // 新契约 GET /intake 的 draft/confirmed 也可能带 blocks；已有本地编辑时不重置。
      ingestBlocksFrom(data, { attempted: blockMeta.attempted, force: !blockState.length });
      renderIntakePanels();
      if (fill) {
        const record = data.confirmed || data.draft;
        if (record) fillForm(record, { force: true });
      }
      return data;
    } catch (error) {
      toast(error.message || t('common.actionFailed'), true);
      return null;
    }
  }

  const ocrBtn = button(t('upload.intake.ocrBtn'), event => busy(event.currentTarget, () => runOcrDraft({ auto: false })), 'secondary', 'scan');

  async function runOcrDraft({ auto = false } = {}) {
    if (!uploadId()) throw new Error(t('upload.recognizeNeedUpload'));
    ocrStatusMount.replaceChildren(notice(t('upload.intake.ocrRunning')));
    try {
      const result = await api(`/uploads/${uploadId()}/ocr-draft`, { method: 'POST' });
      // 兼容新旧响应：旧后端返回 intake；新契约在顶层/草稿里带 blocks 与引擎能力。
      setIntake(result.intake || (result.draft
        ? { ...(intake || {}), upload_id: uploadId(), revision: result.revision, draft: result.draft }
        : null));
      ingestBlocksFrom(result, { attempted: true, force: true });
      if (result.ocr_available === false) {
        ocrStatusMount.replaceChildren(notice(
          t('upload.intake.ocrUnavailable', { message: result.ocr_notice || result.notice || '' }), 'warning'));
        renderIntakePanels();
        return;
      }
      if (result.draft) fillForm(result.draft, { force: true });
      ocrStatusMount.replaceChildren(
        notice(result.draft?.ocr_notice
          ? `${t('upload.intake.ocrDone')} ${result.draft.ocr_notice}`
          : t('upload.intake.ocrDone')));
      if (!auto) toast(t('upload.intake.ocrDone'));
      renderIntakePanels();
    } catch (error) {
      ocrStatusMount.replaceChildren(notice(
        t('upload.intake.ocrFailed', { message: error.message }), 'error'));
      throw error;
    }
  }

  const recognizeBtn = button(t('upload.intake.enhanceBtn'), e => busy(e.currentTarget, async () => {
    if (!uploadId()) throw new Error(t('upload.recognizeNeedUpload'));
    const payload = recPicker.payload(false);
    const job = await api('/ai/jobs', {
      method: 'POST',
      data: { ...payload, task: 'recognize', upload_id: uploadId() },
    });
    mountJob(recJobMount, job.id, async doneJob => {
      const result = doneJob.result || {};
      const applied = result.draft_applied !== false;
      if (intake?.confirmed && !applied) {
        toast(t('upload.intake.pendingToast'), true);
      } else if (result.recognized) {
        fillForm({ ...result.recognized, source: 'vision' });
      }
      renderRecognitionResult(result);
      if (result.differences?.length) {
        toast(t('upload.recDiffToast', { items: result.differences.join(t('common.listSeparator')) }), true);
      }
      renderSimilarList(result.similar_questions || []);
      await refreshIntake();
    });
  }), 'secondary', 'scan');

  const aiAnswerBtn = button(t('upload.intake.aiBtn'), e => busy(e.currentTarget, async () => {
    if (!uploadId()) throw new Error(t('upload.needFiles'));
    if (!String(stemInput.value || '').trim()) throw new Error(t('upload.confirmNeedStem'));
    const payload = aiPicker.payload(false);
    try {
      const result = await api(`/uploads/${uploadId()}/ai-answer`, {
        method: 'POST',
        data: { provider_id: payload.provider_id, model: payload.model, expected_version: intake?.revision },
      });
      setIntake(result.intake || null);
      renderIntakePanels();
      toast(t('upload.intake.aiDone'));
    } catch (error) {
      if (/版本冲突/.test(error.message || '')) await refreshIntake();
      throw error;
    }
  }), 'soft', 'sparkle');

  const confirmBtn = button(t('upload.confirmBtn'), e => busy(e.currentTarget, async () => {
    if (!uploadId()) throw new Error(t('upload.confirmNeedUpload'));
    const payload = readForm();
    if (!payload.stem.trim()) throw new Error(t('upload.confirmNeedStem'));
    // W0/W1：人工确认时把校对后的识别块一起提交（本页没收集到 blocks 时保持旧请求体）。
    const blocks = currentBlocksPayload();
    const data = { ...payload, expected_version: intake?.revision };
    if (blocks) data.blocks = blocks;
    try {
      const res = await api(`/uploads/${uploadId()}/confirm`, { method: 'PATCH', data });
      setIntake(res.intake || null);
      // 后端回包若带 blocks（人工确认后的权威版本），以它刷新编辑态与 needs_review 展示。
      if (extractResponseBlocks(res)) ingestBlocksFrom(res, { attempted: true, force: true });
      renderSimilarList(res.similar_questions || []);
      renderIntakePanels();
      toast(t('upload.confirmDone'));
    } catch (error) {
      const message = String(error?.message || '');
      // 后端明确拒绝 blocks（契约 422）时保留本地编辑，只给中文提示，不刷新/清空表单。
      const blocksRejected = Boolean(blocks) && /blocks?|识别块|非法|校验|invalid/i.test(message);
      if (blocksRejected) {
        toast(formulaText('study.formula.review.blocksRejected', { message }), true);
        return;
      }
      if (/版本冲突/.test(message)) {
        await refreshIntake({ fill: true });
        toast(t('upload.intake.conflict'), true);
      }
      throw error;
    }
  }), '', 'check');

  const ocrSection = el('div', { class: 'pg-substep' },
    el('p', { class: 'pg-substep-label' }, t('upload.intake.substepOcr')),
    el('div', { class: 'row end', style: 'margin:4px 0 8px' }, ocrBtn),
    ocrStatusMount,
    draftMetaMount);

  const enhanceSection = el('details', { class: 'sources', style: 'margin-top:14px' },
    el('summary', {}, t('upload.intake.enhanceTitle')),
    el('p', { class: 'help' }, t('upload.intake.enhanceHint')),
    recPicker.node,
    el('div', { class: 'row end', style: 'margin:12px 0' }, recognizeBtn),
    recJobMount,
    recResultMount,
    el('div', { class: 'card-header', style: 'margin-top:16px' }, el('h3', {}, t('upload.intake.aiTitle'))),
    el('p', { class: 'help' }, t('upload.intake.aiHint')),
    aiPicker.node,
    el('div', { class: 'row end', style: 'margin:12px 0' }, aiAnswerBtn),
    aiSuggestionMount);

  confirmCard.append(
    ocrSection,
    blockSection,
    el('div', { class: 'pg-substep' },
      el('p', { class: 'pg-substep-label' }, t('upload.substepB')),
      el('div', { class: 'form-grid' }, field(t('common.subject'), subjInput), field(t('upload.fieldType'), typeInput)),
      el('div', { style: 'margin-top:10px' }, field(t('upload.fieldStem'), stemInput)),
      el('div', { class: 'grid-two', style: 'margin-top:10px' },
        field(t('upload.intake.fieldOptions'), optionsInput),
        field(t('upload.intake.fieldAnswers'), answersInput)),
      el('div', { class: 'grid-two', style: 'margin-top:10px' },
        field(t('upload.intake.fieldExplanation'), explanationInput),
        field(t('upload.intake.fieldTags'), tagsInput)),
      el('div', { style: 'margin-top:10px' }, field(t('upload.fieldCond'), condInput)),
      el('div', { class: 'row between', style: 'margin-top:12px;align-items:center;flex-wrap:wrap' },
        versionHintMount,
        confirmBtn)),
    enhanceSection,
    pendingMount,
    similarMount);

  // Step 3: 保存到我的题库
  const saveSection = el('section', { class: 'card pg-step-card', id: 'upload-step-3' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '3'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, t('upload.intake.saveTitle')),
        el('p', { class: 'pg-card-sub' }, t('upload.intake.saveSub'))),
      icon('tag')));
  const savePanelMount = el('div');
  saveSection.append(savePanelMount);

  async function loadSavedQuestion(savedId) {
    if (!savedId) return null;
    try {
      const data = await api(`/paperlab/questions/${encodeURIComponent(savedId)}`);
      return data.question || null;
    } catch {
      return null;
    }
  }

  function renderSavedPanel(question) {
    savePanelMount.replaceChildren();
    if (!intake?.saved_question_id) return;
    const savedId = intake.saved_question_id;
    const box = el('div', { class: 'notice', style: 'margin-top:12px' },
      el('strong', {}, t('upload.intake.savedTitle')),
      el('p', { class: 'small-text', style: 'margin:6px 0' },
        t('upload.intake.savedNotice', { id: savedId })),
      question ? el('p', { class: 'small-text', style: 'white-space:pre-wrap;margin:0 0 8px' },
        readableSummary({ stem_plain: question.stem_preview || question.content?.stem || '' }, 260)) : null,
      el('div', { class: 'row', style: 'flex-wrap:wrap;gap:8px' },
        // 统一题目引用：保存成功后的个人题一律 app:<id>（WS11 已支持该引用）。
        button(t('upload.intake.practiceThis'), () => practiceSaved(question), 'soft', 'book',
          { 'data-action': 'practice-personal', 'data-ref': `app:${savedId}` }),
        button(t('upload.intake.addReview'), event => busy(event.currentTarget, () => addToReview(savedId)), 'secondary', 'repeat'),
        // 投稿到广场：POST /plaza/submissions 固定当前保存版本，再跳投稿台填写来源/授权声明。
        button(t('upload.intake.submitToPlaza'), event => busy(event.currentTarget, () => submitSavedToPlaza(savedId)),
          'ghost small', 'upload', { 'data-action': 'submit-to-plaza', 'data-question-id': String(savedId) }),
        button(t('upload.intake.goMyBank'), () => go('mybank', null, { edit: savedId }), 'ghost small', 'tag'),
        button(t('upload.intake.continueShoot'), () => continueShoot(), '', 'scan')));
    savePanelMount.append(box);
  }

  async function renderSavePanel() {
    savePanelMount.replaceChildren();
    if (!intake) {
      savePanelMount.append(notice(t('upload.intake.saveNeedConfirm')));
      return;
    }
    if (intake.saved_question_id) {
      const question = lastSaved?.question || await loadSavedQuestion(intake.saved_question_id);
      if (question) lastSaved = { ...(lastSaved || {}), question };
      renderSavedPanel(question);
      return;
    }
    if (!intake.confirmed) {
      savePanelMount.append(notice(t('upload.intake.saveNeedConfirm')));
      return;
    }
    const confirmed = intake.confirmed;
    const preview = el('div', { class: 'notice', style: 'margin-top:12px' },
      el('div', { class: 'row', style: 'margin-bottom:6px;flex-wrap:wrap' },
        badge(confirmed.subject || t('upload.subjectPending'), 'teal'),
        badge(t('upload.typeBadge', { type: confirmed.type || t('upload.typePending') }), 'blue'),
        confirmed.answers?.length ? badge(t('upload.intake.answerBadge', { answer: confirmed.answers.join('；') }), 'amber') : badge(t('upload.intake.noAnswerBadge'), 'red')),
      el('p', { class: 'small-text', style: 'white-space:pre-wrap;margin:6px 0 0' },
        readableSummary({ stem_plain: confirmed.stem || '' }, 320)));
    const saveBtn = button(t('upload.intake.saveBtn'), event => busy(event.currentTarget, () => saveToBank()), '', 'tag');
    savePanelMount.append(preview, el('div', { class: 'row end', style: 'margin-top:12px' }, saveBtn));
  }

  async function saveToBank() {
    const payload = readForm();
    if (!payload.stem) throw new Error(t('upload.intake.saveNeedConfirm'));
    try {
      const result = await api(`/uploads/${uploadId()}/save-to-bank`, {
        method: 'POST',
        data: { ...payload, expected_version: intake?.revision },
      });
      lastSaved = result;
      setIntake(result.intake || null);
      renderIntakePanels();
      if (result.notice) toast(result.notice);
    } catch (error) {
      if (/版本冲突/.test(error.message || '')) {
        await refreshIntake({ fill: true });
        toast(t('upload.intake.conflict'), true);
        throw error;
      }
      savePanelMount.append(
        notice(t('upload.intake.saveFailed', { message: error.message }), 'error'),
        el('div', { class: 'row', style: 'margin-top:10px' },
          button(t('upload.intake.saveRetry'), event => busy(event.currentTarget, async () => {
            event.currentTarget.closest('.notice.error')?.remove();
            event.currentTarget.parentElement?.remove();
            await saveToBank();
          }), 'secondary small', 'repeat')));
      throw error;
    }
  }

  function practiceSaved(question) {
    // 统一题目引用：保存成功页的「练这道」直达个人题练习 #/practice?qid=app:<id>。
    const personalId = Number(question?.id || intake?.saved_question_id || 0);
    if (personalId) {
      go('practice', `app:${personalId}`);
      return;
    }
    // 兜底：没有个人题 ID 时沿用相似题命中（不应发生，仅防旧数据）。
    const exact = lastSimilar.find(item => item.exact_match);
    if (exact?.qid) {
      go('practice', exact.qid);
      return;
    }
    go('mybank');
  }

  /* 投稿到广场（Phase 2）：POST /plaza/submissions 以保存的个人题为不可变快照创建投稿草稿，
   * 随后跳到投稿台 #/submission?id= 填来源/授权声明并送审（不传空声明也能创建草稿）。 */
  async function submitSavedToPlaza(savedId) {
    const questionId = Number(savedId || intake?.saved_question_id || 0);
    if (!questionId) throw new Error(t('upload.intake.submitNeedSaved'));
    const result = await api('/plaza/submissions', {
      method: 'POST',
      data: { question_id: questionId, source: {}, license_confirmed: false },
    });
    const submissionId = Number(result?.submission?.id || 0);
    if (!submissionId) throw new Error(t('upload.intake.submitFailed'));
    toast(t('upload.intake.submitCreated'));
    go('submission', null, { id: submissionId });
  }

  async function addToReview(savedId) {
    if (!uploadId()) throw new Error(t('upload.confirmNeedUpload'));
    await api('/wrongbook/upload', {
      method: 'POST',
      data: {
        upload_id: uploadId(),
        subject: readForm().subject || '拍题',
        stem_preview: readForm().stem.slice(0, 400) || state.questionUpload?.filename || '',
        reason: '拍题入库加入复习',
      },
    });
    toast(t('upload.intake.addReviewDone'));
    return savedId;
  }

  async function continueShoot() {
    clearPageState();
    await renderRoute();
  }

  function renderIntakePanels() {
    renderDraftMeta();
    renderPendingDraft();
    renderAiSuggestion();
    renderBlockReview();
    versionHintMount.replaceChildren(renderVersionHint() || '');
    renderSavePanel();
  }

  const uploadBtn = button(t('upload.uploadBtn'), e => busy(e.currentTarget, async () => {
    if (!pendingFiles.length) throw new Error(t('upload.needFiles'));
    const fd = new FormData();
    if (pendingFiles.length === 1) fd.set('file', pendingFiles[0]);
    else for (const f of pendingFiles) fd.append('files', f);
    const result = await api('/uploads/question', { method: 'POST', body: fd });
    state.questionUpload = result;
    state.uploadJobId = null;
    setIntake(null);
    // 新题的识别块校对状态整体清空（旧题的 blocks/叠加框不能串到新题）。
    clearBlockReviewState();
    setHashParams('upload', { upload: result.id }, true);
    saveUploadSession({ upload: result, intake: null });
    pendingFiles.length = 0;
    renderPendingFiles();
    // 新上传的题目不能复用上一题的识别结果与讲解卡片。
    recJobMount.replaceChildren();
    recResultMount.replaceChildren();
    similarMount.replaceChildren();
    solveJobMount.replaceChildren();
    savePanelMount.replaceChildren();
    lastSaved = null;
    lastSimilar = [];
    clearForm();
    recResultMount.replaceChildren();
    renderUploaded(result);
    toast(t('upload.uploadedToast'));
    // OCR-first：上传后默认立刻跑本地 OCR 预填（无需视觉模型 Key）。
    try {
      await runOcrDraft({ auto: true });
    } catch { /* runOcrDraft 已展示失败提示，保留原图可重试/手录 */ }
  }), '', 'upload');

  uploadCard.append(
    notice(t('upload.intake.entryHint')),
    picker, filePreviewGrid,
    el('div', { class: 'row end', style: 'margin:14px 0' }, uploadBtn),
    uploadedInfo);

  // Step 4: Multi-mode Solve with Cross-Verification 2.0（旧「讲解」动作保留在保存之后）
  const solveCard = el('section', { class: 'card pg-step-card', id: 'upload-step-4' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '4'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, t('upload.step3.title')),
        el('p', { class: 'pg-card-sub' }, t('upload.step3.sub'))),
      icon('sparkle')));

  const solveControls = createModelPicker('upload', { showModeToggle: true, showJudge: true });
  const selectedModes = new Set(['detailed', 'pattern_teaching']);
  const modeBox = el('div', { class: 'grid-cards', style: 'margin:14px 0' });
  for (const m of modes) {
    const active = selectedModes.has(m.id);
    const btn = el('button', {
      type: 'button',
      class: `grid-card${active ? ' active' : ''}`,
      'aria-pressed': String(active),
      onClick: () => {
        if (selectedModes.has(m.id)) {
          if (selectedModes.size === 1) return;
          selectedModes.delete(m.id);
        } else {
          selectedModes.add(m.id);
        }
        btn.classList.toggle('active', selectedModes.has(m.id));
        btn.setAttribute('aria-pressed', String(selectedModes.has(m.id)));
      },
    }, el('strong', {}, localizedModeText(m, 'name')), el('small', {}, localizedModeText(m, 'short')));
    modeBox.append(btn);
  }

  const requirements = el('textarea', { placeholder: t('upload.requirementsPlaceholder'), rows: 2 });
  const solveJobMount = el('div');

  function handleSolveJobComplete() {
    state.uploadJobId = null;
  }

  const generateBtn = button(t('upload.generate'), e => busy(e.currentTarget, async () => {
    if (!uploadId()) throw new Error(t('upload.generateNeed'));
    // 生成前把最新编辑过的题干写回；保存失败必须显式提示并中止。
    if (stemInput.value.trim()) {
      try {
        const saved = await api(`/uploads/${uploadId()}/confirm`, {
          method: 'PATCH',
          data: { ...readForm(), expected_version: intake?.revision },
        });
        if (saved?.intake) setIntake(saved.intake);
      } catch (error) {
        throw new Error(t('upload.stemSaveFailed', { message: error.message }));
      }
    }
    const payload = solveControls.payload(false);
    const job = await api('/ai/jobs', {
      method: 'POST',
      data: {
        ...payload,
        task: 'question-analysis',
        upload_id: uploadId(),
        modes: [...selectedModes],
        requirements: requirements.value.trim(),
      },
    });
    state.uploadJobId = job.id;
    mountJob(solveJobMount, job.id, handleSolveJobComplete, { titleHint: `${subjInput.value || '拍题'} · ${state.questionUpload.filename}` });
  }), '', 'sparkle');

  const addWrongBtn = button(t('upload.addWrong'), e => busy(e.currentTarget, async () => {
    if (!uploadId()) throw new Error(t('upload.confirmNeedUpload'));
    await api('/wrongbook/upload', {
      method: 'POST',
      data: {
        upload_id: uploadId(),
        subject: subjInput.value || '拍题',
        stem_preview: stemInput.value.trim().slice(0, 400) || state.questionUpload.filename,
        reason: '拍题加入错题本',
      },
    });
    toast(t('upload.addWrongDone'));
  }), 'soft', 'plus');

  solveCard.append(
    el('div', { class: 'pg-substep' },
      el('p', { class: 'pg-substep-label' }, t('upload.substepMode')),
      solveControls.node,
      el('div', { class: 'field-label', style: 'margin-top:14px' }, t('upload.modeCheck')),
      modeBox,
      field(t('upload.requirements'), requirements)),
    el('div', { class: 'pg-substep pg-actions pg-actions-end' }, addWrongBtn, generateBtn),
    solveJobMount
  );

  function renderUploaded(item) {
    uploadedInfo.replaceChildren(notice(t('upload.uploadedNotice', { name: item.filename || t('upload.uploadBtn') })));
    if (item.extracted_text) {
      uploadedInfo.append(el('div', { class: 'question-material' }, questionText(item.extracted_text)));
      if (!stemInput.value) stemInput.value = item.extracted_text;
    }
    // W0/W1：原图预览改为「定位容器 + 图片」，供识别块叠加框（attachBlockOverlay）使用；
    // 多图时 pageIndex 从 0 递增，与 block.page（从 1 开始）对应。
    destroyBlockOverlays();
    previewShells.length = 0;
    if (item.preview_url && /\.(png|jpe?g|webp)$/i.test(item.filename || '')) {
      const imgWrap = el('div', { class: 'pg-uploaded-images' });
      const addPreview = (src, alt, pageIndex) => {
        const img = el('img', {
          class: 'uploaded-preview', src, alt, loading: 'lazy', style: 'max-height:220px',
        });
        const shell = el('div', { class: 'pg-block-preview' }, img);
        imgWrap.append(shell);
        previewShells.push({ shell, img, pageIndex });
      };
      addPreview(item.preview_url, t('upload.uploadedAlt'), 0);
      (item.extra_previews || []).forEach((extra, index) => {
        addPreview(extra.preview_url, extra.filename || t('upload.uploadedAlt'), index + 1);
      });
      uploadedInfo.append(imgWrap);
    }
  }

  // 清空当前拍题页的所有草稿/识别/讲解状态（不含提示）。
  function clearPageState() {
    closeJobStreams();
    state.questionUpload = null;
    state.uploadJobId = null;
    state.uploadIntake = null;
    intake = null;
    lastSaved = null;
    lastSimilar = [];
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
    setHashParams('upload', {}, true);
    uploadedInfo.replaceChildren();
    recJobMount.replaceChildren();
    recResultMount.replaceChildren();
    similarMount.replaceChildren();
    solveJobMount.replaceChildren();
    ocrStatusMount.replaceChildren();
    draftMetaMount.replaceChildren();
    pendingMount.replaceChildren();
    aiSuggestionMount.replaceChildren();
    savePanelMount.replaceChildren();
    // 识别块校对区（组件实例/叠加框/编辑态）一并清空。
    clearBlockReviewState();
    clearForm();
  }

  // 重新选图时重置步骤 2–4 的所有识别/讲解状态与 sessionStorage 快照。
  function resetForNewImage() {
    clearPageState();
    renderPendingFiles();
    toast(t('upload.newImageToast'));
  }

  const deepUploadId = Number(params?.get?.('upload') || 0);

  async function loadIntakeFromServer(uploadIdValue) {
    try {
      const data = await api(`/uploads/${uploadIdValue}/intake`);
      if (!current(version)) return;
      state.questionUpload = {
        id: data.upload_id,
        filename: data.filename,
        media_type: data.media_type,
        preview_url: data.preview_url,
      };
      setIntake(data);
      setHashParams('upload', { upload: data.upload_id }, true);
      renderUploaded(state.questionUpload);
      // 新契约：GET /intake 的 draft/confirmed 里也可能带 blocks（深链/刷新恢复校对区）。
      if (extractResponseBlocks(data) !== null) {
        ingestBlocksFrom(data, { attempted: true, force: true });
      }
      const record = data.confirmed || data.draft;
      if (record) fillForm(record, { force: true });
      renderIntakePanels();
      if (!record && !data.pending_draft) {
        // 任务中心跳到刚上传、还没识别的草稿：补跑一次本地 OCR。
        try { await runOcrDraft({ auto: true }); } catch { /* 已给出失败提示 */ }
      }
      toast(t('upload.intake.deepLinkLoaded', { status: data.status || 'uploaded' }));
    } catch (error) {
      toast(t('upload.intake.deepLinkMissing'), true);
      return;
    }
  }

  const stepRail = el('nav', { class: 'pg-step-rail', 'aria-label': t('upload.stepRailAria') });
  for (const [index, label] of [
    ['1', t('upload.step1.short')],
    ['2', t('upload.step2.short')],
    ['3', t('upload.intake.saveShort')],
    ['4', t('upload.step3.short')],
  ]) {
    stepRail.append(el('button', {
      type: 'button',
      class: 'pg-step-link',
      'aria-label': t('upload.stepAria', { index, label }),
      onClick: () => document.getElementById(`upload-step-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    }, el('span', { class: 'pg-step-link-index', 'aria-hidden': 'true' }, index), label));
  }

  container.replaceChildren(
    heading('ASK & UNDERSTAND', t('upload.heading'),
      t('upload.subheading')),
    stepRail,
    el('div', { class: 'pg-upload-stack' }, uploadCard, confirmCard, saveSection, solveCard));

  if (deepUploadId) {
    await loadIntakeFromServer(deepUploadId);
  } else if (state.questionUpload) {
    renderUploaded(state.questionUpload);
    if (intake) {
      const record = intake.confirmed || intake.draft;
      if (record) fillForm(record, { force: true });
      // 会话恢复：若快照里已有 blocks，恢复识别块校对区（没有则保持隐藏）。
      if (extractResponseBlocks(intake)?.length) {
        ingestBlocksFrom(intake, { attempted: true, force: true });
      }
      renderIntakePanels();
    } else if (state.questionUpload.id) {
      await loadIntakeFromServer(state.questionUpload.id);
    }
  }
  if (state.uploadJobId) mountJob(solveJobMount, state.uploadJobId, handleSolveJobComplete);
  await renderJobHistory(container, version, ['recognize', 'question-analysis', 'explain', 'grade']);
}

export async function renderJobHistory(container, version, tasks = null) {
  try {
    const jobs = await api('/ai/jobs');
    if (!current(version)) return;
    const items = (jobs.items || []).filter(j => !tasks || tasks.includes(j.task)).slice(0, 10);
    if (!items.length) return;
    const result = el('div');
    const card = el('section', { class: 'card', style: 'margin-top:24px' },
      el('div', { class: 'card-header' }, el('h2', {}, t('upload.jobHistoryTitle'))));
    const list = el('div', { class: 'job-history' });
    for (const job of items) {
      const openBtn = el('button', { type: 'button', onClick: () => mountJob(result, job.id) },
        el('span', {}, taskNames[job.task] || t('upload.aiTaskFallback'),
          el('small', { class: 'muted', style: 'margin-left:12px' }, dateText(job.created_at))),
        badge(statusNames[job.status] || job.status, statusBadgeVariant(job.status)));
      const draftId = Number(job.request?.upload_id || 0);
      list.append(openBtn);
      if (job.task === 'recognize' && draftId) {
        list.append(button(t('upload.intake.openDraft'),
          () => go('upload', null, { upload: draftId }), 'secondary small', 'scan'));
      }
    }
    card.append(list, result);
    container.append(card);
  } catch (error) {
    if (current(version)) container.append(notice(t('upload.jobHistoryFailed', { message: error.message }), 'warning'));
  }
}
