import { api, ensureExplainModes } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, badge, heading, notice, field, input, select,
  questionText, dateText, busy, toast, readableSummary,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { mountJob, taskNames, statusNames, statusBadgeVariant, closeJobStreams } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker, attachPasteImage } from '../components/filePicker.js';

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
  const hint = el('p', { class: 'help' }, '在图片上按住鼠标拖动框选需要保留的题目区域，点击「确认裁剪」生效。');
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

  const confirmCropBtn = button('确认裁剪', async () => {
    if (!bmp || !rect || rect.w < 12 || rect.h < 12) {
      toast('请先在图片上框选有效裁剪区域。', true);
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
    el('div', { class: 'card-header' }, el('h3', {}, '框选裁剪题目区域'), button('取消', () => dlg.close(), 'ghost small', 'close')),
    hint,
    el('div', { class: 'crop-canvas-wrap' }, canvas),
    el('div', { class: 'row end', style: 'margin-top:14px' },
      button('重置选区', () => { rect = null; draw(); }, 'secondary small'),
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

export async function renderUpload(container, version) {
  const modes = await ensureExplainModes().catch(() => [
    { id: 'detailed', name: '详细解析', short: '题意→思路→步骤→易错点' },
    { id: 'pattern_teaching', name: '题型教学', short: '识别特征→通法框架' },
    { id: 'standard_steps', name: '标准步骤（大题）', short: '得分点与规范书写' },
    { id: 'extension', name: '知识拓展', short: '拓展结论与跨章联系' },
  ]);
  if (!current(version)) return;

  // Restore sessionStorage state so page refresh doesn't lose uploaded question (§8)
  const restored = loadUploadSession();
  if (restored?.upload && !state.questionUpload) {
    state.questionUpload = restored.upload;
  }

  const pendingFiles = [];
  const previewUrls = [];
  function clearPreviewUrls() {
    while (previewUrls.length) URL.revokeObjectURL(previewUrls.pop());
  }

  const uploadCard = el('section', { class: 'card pg-step-card', id: 'upload-step-1' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '1'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, '上传题目'),
        el('p', { class: 'pg-card-sub' }, '拍照、粘贴截图或多图上传；也可以上传 TXT/MD 文本。')),
      badge('支持 Ctrl+V 粘贴', 'teal')));

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
              button('裁剪', () => openCropDialog(pendingFiles[idx], cropped => {
                pendingFiles[idx] = cropped;
                renderPendingFiles();
                toast('已裁剪图片。');
              }), 'ghost small', 'scan'),
              button('旋转', async () => {
                pendingFiles[idx] = await rotateImageFile(pendingFiles[idx]);
                renderPendingFiles();
              }, 'ghost small', 'rotate'))
          : el('span'),
        button('移除', () => { pendingFiles.splice(idx, 1); renderPendingFiles(); }, 'ghost small', 'trash'));
      item.append(ctrls);
      filePreviewGrid.append(item);
    });
  }

  function addFiles(files) {
    const list = Array.isArray(files) ? files : [files];
    const replacingQuestion = Boolean(state.questionUpload) || uploadedInfo.childNodes.length > 0;
    for (const f of list) {
      if (pendingFiles.length >= 5) { toast('一次最多上传 5 张图片', true); break; }
      pendingFiles.push(f);
    }
    renderPendingFiles();
    // 重新选图 = 换了一道题：重置步骤 2–3 的识别/讲解状态，避免旧题干串到新题。
    if (replacingQuestion) resetForNewImage();
  }

  const picker = filePicker(
    '拍照 / 选择多图 / 拖入题目文件',
    '支持多张题干+配图（PNG/JPG/WebP）、TXT/MD 文本；也可以直接按 Ctrl+V 粘贴截图。',
    'image/png,image/jpeg,image/webp,.txt,.md',
    addFiles,
    { multiple: true, capture: 'environment' }
  );
  attachPasteImage(uploadCard, f => { addFiles([f]); toast('已从剪贴板粘贴题目截图。'); });

  const uploadBtn = button('上传题目', e => busy(e.currentTarget, async () => {
    if (!pendingFiles.length) throw new Error('请先拍照、粘贴截图或选择题目文件。');
    const fd = new FormData();
    if (pendingFiles.length === 1) fd.set('file', pendingFiles[0]);
    else for (const f of pendingFiles) fd.append('files', f);
    const result = await api('/uploads/question', { method: 'POST', body: fd });
    state.questionUpload = result;
    state.uploadJobId = null;
    saveUploadSession({ upload: result, recognized: null, confirmed: false });
    pendingFiles.length = 0;
    renderPendingFiles();
    // 新上传的题目不能复用上一题的识别结果与讲解卡片。
    recJobMount.replaceChildren();
    recResultMount.replaceChildren();
    similarMount.replaceChildren();
    solveJobMount.replaceChildren();
    toast('题目已上传，下一步请先识别并确认题干内容。');
    renderUploaded(result);
  }), '', 'upload');

  uploadCard.append(picker, filePreviewGrid, el('div', { class: 'row end', style: 'margin:14px 0' }, uploadBtn), uploadedInfo);

  // Step 2: Vision Recognition + Editable Confirmation + Similar Bank Search (§8)
  const confirmCard = el('section', { class: 'card pg-step-card', id: 'upload-step-2' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '2'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, '核对题干'),
        el('p', { class: 'pg-card-sub' }, '先确认学科、题型、题干与条件，再进入讲解。')),
      badge('先确认、再求解', 'blue')));

  const recPicker = createModelPicker('ocr', { requireVision: true, showModeToggle: true });
  const recJobMount = el('div');
  const recResultMount = el('div');
  const similarMount = el('div');

  const subjInput = select(
    ['', '数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '其他'].map(s => ({ value: s, label: s || '学科（自动/可选）' })),
    restored?.recognized?.subject || ''
  );
  const typeInput = input('例如：单选题 / 计算题', { value: restored?.recognized?.type || '' });
  const stemInput = el('textarea', {
    rows: 5,
    placeholder: '点击“视觉识别题目”自动填入，或手动修正识别结果中的公式/条件，确认无误后再求解…',
    value: restored?.recognized?.stem || state.questionUpload?.extracted_text || '',
  });
  const condInput = input('图中条件（用分号隔开，可选）', { value: (restored?.recognized?.conditions || []).join('；') });

  function renderSimilarList(similar) {
    similarMount.replaceChildren();
    if (!similar?.length) return;
    const exact = similar.find(s => s.exact_match);
    const box = el('div', { class: `notice${exact ? '' : ' warning'}`, style: 'margin-top:12px' },
      el('strong', {}, exact ? '🎯 题库中命中高度相似真题！建议直接进入题库作答并查看原卷答案：' : '🔎 题库中找到相似真题：'));
    const list = el('div', { class: 'mini-list', style: 'margin-top:8px' });
    for (const s of similar) {
      list.append(el('div', { class: 'mini-item', style: 'padding:8px 0' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, `${s.subject} · ${s.year || ''} ${s.province || ''} · 相似度 ${Math.round(s.similarity * 100)}%`),
          el('p', {}, readableSummary(s, 220))),
        button('去题库打开', () => go('practice', s.qid), 'soft small', 'book')));
    }
    box.append(list);
    similarMount.append(box);
  }

  // 4.5：识别结果卡展示学科 / 题型（method）标签；双模型识别有差异时展开对照，
  // 并明确提示「哪些地方需要重点核对」。
  function renderRecognitionResult(result) {
    recResultMount.replaceChildren();
    const rec = result?.recognized;
    if (!rec) return;
    const secondary = result?.secondary_recognized || null;
    const differences = Array.isArray(result?.differences) ? result.differences : [];
    const tags = el('div', { class: 'row', style: 'margin-bottom:8px' },
      badge(rec.subject || '学科待核对', 'teal'),
      badge(`题型：${rec.type || '待核对'}`, 'blue'),
      (rec.uncertain || []).length ? badge(`不确定 ${rec.uncertain.length} 处`, 'amber') : null);
    const box = el('div', { class: 'notice', style: 'margin-top:12px' },
      el('strong', {}, '识别结果已填入下方，可继续修改再确认'),
      tags,
      el('p', { class: 'small-text', style: 'margin:6px 0 0' }, readableSummary({ stem_plain: rec.stem || '' }, 180)));
    if (differences.length) {
      box.append(el('div', { class: 'notice warning', style: 'margin-top:10px;margin-bottom:0' },
        `双模型识别存在差异，请重点核对：${differences.join('；')}`));
    }
    if (secondary) {
      const detail = el('details', { class: 'sources', style: 'margin-top:10px' },
        el('summary', {}, differences.length ? '对照两个模型的识别结果' : '查看第二个模型的识别结果'));
      const grid = el('div', { class: 'grid-two' });
      for (const [label, model] of [['模型 A（已填入）', rec], ['模型 B（对照）', secondary]]) {
        grid.append(el('div', { class: 'source-block' },
          el('strong', {}, label),
          el('div', { class: 'small-text' }, `学科：${model.subject || '—'} · 题型：${model.type || '—'}`),
          el('p', { class: 'small-text', style: 'white-space:pre-wrap;margin:6px 0 0' },
            readableSummary({ stem_plain: model.stem || '' }, 320))));
      }
      detail.append(grid);
      box.append(detail);
    }
    recResultMount.append(box);
  }

  // 重新选图时重置步骤 2–3 的所有识别/讲解状态与 sessionStorage 快照。
  function resetForNewImage() {
    closeJobStreams();
    state.questionUpload = null;
    state.uploadJobId = null;
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
    uploadedInfo.replaceChildren();
    recJobMount.replaceChildren();
    recResultMount.replaceChildren();
    similarMount.replaceChildren();
    solveJobMount.replaceChildren();
    subjInput.value = '';
    typeInput.value = '';
    stemInput.value = '';
    condInput.value = '';
    toast('已选择新的题目图片，请重新上传、识别并核对题干。');
  }

  const recognizeBtn = button('视觉识别题目', e => busy(e.currentTarget, async () => {
    if (!state.questionUpload?.id) throw new Error('请先在左侧上传题目图片。');
    const payload = recPicker.payload(false);
    const job = await api('/ai/jobs', {
      method: 'POST',
      data: { ...payload, task: 'recognize', upload_id: state.questionUpload.id },
    });
    mountJob(recJobMount, job.id, doneJob => {
      const rec = doneJob.result?.recognized;
      if (rec) {
        subjInput.value = rec.subject || '';
        typeInput.value = rec.type || '';
        const optsText = (rec.options || []).map(o => `${o.label}. ${o.text}`).join('\n');
        stemInput.value = optsText ? `${rec.stem}\n\n${optsText}` : (rec.stem || '');
        condInput.value = (rec.conditions || []).join('；');
        saveUploadSession({ upload: state.questionUpload, recognized: rec, confirmed: false });
      }
      // 结果卡：展示 method/题型标签与双模型差异（4.5）。
      renderRecognitionResult(doneJob.result);
      if (doneJob.result?.differences?.length) {
        toast(`双模型识别存在差异：${doneJob.result.differences.join('；')}，请重点核对！`, true);
      }
      renderSimilarList(doneJob.result?.similar_questions || []);
    });
  }), 'secondary', 'scan');

  const confirmBtn = button('确认题干并检索题库相似题', e => busy(e.currentTarget, async () => {
    if (!state.questionUpload?.id) throw new Error('请先上传题目。');
    if (!stemInput.value.trim()) throw new Error('请先识别或填写题目内容。');
    const res = await api(`/uploads/${state.questionUpload.id}/confirm`, {
      method: 'PATCH',
      data: {
        subject: subjInput.value,
        type: typeInput.value.trim(),
        stem: stemInput.value.trim(),
        conditions: condInput.value.split(/[;；\n]/).map(s => s.trim()).filter(Boolean),
      },
    });
    saveUploadSession({ upload: state.questionUpload, recognized: res.confirmed, confirmed: true });
    toast('题干已确认！可直接进行下方多模式求解。');
    renderSimilarList(res.similar_questions || []);
  }), '', 'check');

  confirmCard.append(
    el('div', { class: 'pg-substep' },
      el('p', { class: 'pg-substep-label' }, 'A. 视觉识别题目（可选）'),
      recPicker.node,
      el('div', { class: 'row end', style: 'margin:12px 0' }, recognizeBtn),
      recJobMount,
      recResultMount),
    el('div', { class: 'pg-substep' },
      el('p', { class: 'pg-substep-label' }, 'B. 核对并确认题干'),
      el('div', { class: 'form-grid' }, field('学科', subjInput), field('题型', typeInput)),
      el('div', { style: 'margin-top:10px' }, field('核对 / 修改后的完整题干（支持 LaTeX）', stemInput)),
      el('div', { style: 'margin-top:10px' }, field('补充条件 / 图形说明', condInput)),
      el('div', { class: 'row end', style: 'margin-top:12px' }, confirmBtn)),
    similarMount
  );

  // Step 3: Multi-mode Solve with Cross-Verification 2.0 (§6.2, §6.3, §8)
  const solveCard = el('section', { class: 'card pg-step-card', id: 'upload-step-3' },
    el('div', { class: 'pg-step-head' },
      el('span', { class: 'pg-step-index', 'aria-hidden': 'true' }, '3'),
      el('div', { class: 'pg-step-heading' },
        el('h2', {}, '查看讲解'),
        el('p', { class: 'pg-card-sub' }, '确认题干后，按所选模式生成讲解与多维分析。')),
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
    }, el('strong', {}, m.name), el('small', {}, m.short));
    modeBox.append(btn);
  }

  const requirements = el('textarea', { placeholder: '可选：补充学习要求（例如：我卡在第二步变形，请重点展开）', rows: 2 });
  const solveJobMount = el('div');

  // 求解/分析任务终态：清掉 job 指针，结果卡已由 mountJob 渲染到结果区。
  // 路由切走再回来会按 state.uploadJobId 重新挂载并再次触发此回调。
  function handleSolveJobComplete() {
    state.uploadJobId = null;
  }

  const generateBtn = button('开始求解与分析', e => busy(e.currentTarget, async () => {
    if (!state.questionUpload?.id) throw new Error('请先上传并确认题目。');
    // 生成前把最新编辑过的题干写回；保存失败必须显式提示并中止，
    // 不能静默吞错后用旧题干生成讲解（4.5）。
    if (stemInput.value.trim()) {
      try {
        await api(`/uploads/${state.questionUpload.id}/confirm`, {
          method: 'PATCH',
          data: {
            subject: subjInput.value, type: typeInput.value.trim(),
            stem: stemInput.value.trim(),
            conditions: condInput.value.split(/[;；\n]/).map(s => s.trim()).filter(Boolean),
          },
        });
      } catch (error) {
        throw new Error(`题干保存失败，未开始求解：${error.message}`);
      }
      saveUploadSession({
        upload: state.questionUpload,
        recognized: { subject: subjInput.value, type: typeInput.value.trim(), stem: stemInput.value.trim() },
        confirmed: true,
      });
    }
    const payload = solveControls.payload(false);
    const job = await api('/ai/jobs', {
      method: 'POST',
      data: {
        ...payload,
        task: 'question-analysis',
        upload_id: state.questionUpload.id,
        modes: [...selectedModes],
        requirements: requirements.value.trim(),
      },
    });
    state.uploadJobId = job.id;
    mountJob(solveJobMount, job.id, handleSolveJobComplete, { titleHint: `${subjInput.value || '拍题'} · ${state.questionUpload.filename}` });
  }), '', 'sparkle');

  const addWrongBtn = button('加入我的错题本', e => busy(e.currentTarget, async () => {
    if (!state.questionUpload?.id) throw new Error('请先上传题目。');
    await api('/wrongbook/upload', {
      method: 'POST',
      data: {
        upload_id: state.questionUpload.id,
        subject: subjInput.value || '拍题',
        stem_preview: stemInput.value.trim().slice(0, 400) || state.questionUpload.filename,
        reason: '拍题加入错题本',
      },
    });
    toast('已加入「错题复习 · 拍题错题」。');
  }), 'soft', 'plus');

  solveCard.append(
    el('div', { class: 'pg-substep' },
      el('p', { class: 'pg-substep-label' }, 'A. 选择模型与讲解模式'),
      solveControls.node,
      el('div', { class: 'field-label', style: 'margin-top:14px' }, '勾选要生成的讲解卡片（可多选）'),
      modeBox,
      field('补充学习要求', requirements)),
    el('div', { class: 'pg-substep pg-actions pg-actions-end' }, addWrongBtn, generateBtn),
    solveJobMount
  );

  function renderUploaded(item) {
    uploadedInfo.replaceChildren(notice(`已上传：${item.filename || '上传题目'}（仅当前账号可见）。`));
    if (item.extracted_text) {
      uploadedInfo.append(el('div', { class: 'question-material' }, questionText(item.extracted_text)));
      if (!stemInput.value) stemInput.value = item.extracted_text;
    }
    if (item.preview_url && /\.(png|jpe?g|webp)$/i.test(item.filename || '')) {
      const imgWrap = el('div', { class: 'row' },
        el('img', { class: 'uploaded-preview', src: item.preview_url, alt: '已上传题目', style: 'max-height:220px' }));
      for (const ex of item.extra_previews || []) {
        imgWrap.append(el('img', { class: 'uploaded-preview', src: ex.preview_url, alt: ex.filename, style: 'max-height:220px' }));
      }
      uploadedInfo.append(imgWrap);
    }
  }

  const stepRail = el('nav', { class: 'pg-step-rail', 'aria-label': '拍题步骤' });
  for (const [index, label] of [['1', '上传'], ['2', '核对题干'], ['3', '查看讲解']]) {
    stepRail.append(el('button', {
      type: 'button',
      class: 'pg-step-link',
      'aria-label': `第 ${index} 步：${label}`,
      onClick: () => document.getElementById(`upload-step-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    }, el('span', { class: 'pg-step-link-index', 'aria-hidden': 'true' }, index), label));
  }

  container.replaceChildren(
    heading('ASK & UNDERSTAND', '拍题 · 识别确认 · 交叉求解。',
      '按「上传 → 核对题干 → 查看讲解」推进：先拍照或粘贴截图，确认题干后再生成讲解。'),
    stepRail,
    el('div', { class: 'pg-upload-stack' }, uploadCard, confirmCard, solveCard));

  if (state.questionUpload) renderUploaded(state.questionUpload);
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
      el('div', { class: 'card-header' }, el('h2', {}, '最近的 AI 任务')));
    const list = el('div', { class: 'job-history' });
    for (const job of items) {
      list.append(el('button', { type: 'button', onClick: () => mountJob(result, job.id) },
        el('span', {}, taskNames[job.task] || 'AI 学习任务',
          el('small', { class: 'muted', style: 'margin-left:12px' }, dateText(job.created_at))),
        badge(statusNames[job.status] || job.status, statusBadgeVariant(job.status))));
    }
    card.append(list, result);
    container.append(card);
  } catch (error) {
    if (current(version)) container.append(notice(`任务记录暂时无法加载：${error.message}`, 'warning'));
  }
}
