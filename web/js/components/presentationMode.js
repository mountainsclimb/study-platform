/**
 * 讲错题 / 大屏讲解 / 原卷留痕（整合自小鹿老师：讲错题、大屏讲题、页面留痕、原卷导出）。
 *
 * 设计约束：
 * - 纯原生 DOM/Canvas，无第三方依赖、无内联脚本；题目内容一律通过既有安全渲染入口
 *   （`core/markdown.js` 的 markdown / `core/dom.js` 的 questionBlocks / questionText）输出，不拼 innerHTML。
 * - 只读既有只读 API：`GET /api/reveal?qid=`（服务端已做「先作答后解锁」门控）；
 *   入口页也可以直接传已加载的题目对象（question.js 解锁后的 q 已经包含答案/解析块）。
 * - 批注只存在于本地画布，不写回题目、不产生任何请求；关闭即丢弃。
 * - 「原卷留痕」用 Canvas 重绘：文字按当前揭示步骤重排 + 画面内配图按原位置绘制 + 批注合成；
 *   浏览器限制导致画布被污染（跨源图片）时自动降级为「仅批注层 PNG」并提示。
 * - 录制：优先 getDisplayMedia 录屏（含讲解画面），不支持时退回 canvas.captureStream；都没有则禁用并说明。
 */
import {
  el, icon, button, badge, loading, notice, toast,
  questionBlocks, blockList, questionText, query, dateText, typeLabel, sourceOf,
} from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { t } from '../core/i18n.js';
import { go } from '../core/router.js';

const PEN_COLORS = [
  { id: 'red', value: '#c0392b', get label() { return t('presentation.penRed'); } },
  { id: 'blue', value: '#1f5fa9', get label() { return t('presentation.penBlue'); } },
  { id: 'ink', value: '#1b2a2f', get label() { return t('presentation.penInk'); } },
];
const HIGHLIGHT_COLOR = '#f2c94c';
const PEN_WIDTH = 3;
const HIGHLIGHT_WIDTH = 20;
const ERASER_WIDTH = 28;
const MAX_REVEAL_STEPS = 8;
const DARK_KEY = 'zhixu.presentation.dark';
const BIG_KEY = 'zhixu.presentation.big';

let activeSession = null;

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function storageFlag(key) {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function saveFlag(key, value) {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    /* 隐私模式禁用存储不影响讲解 */
  }
}

function textOfAnswer(data) {
  const answer = data?.answer;
  if (typeof answer === 'string') return answer.trim();
  if (answer && typeof answer === 'object') {
    return String(answer.text || answer.content || '').trim();
  }
  return String(data?.answer_text || '').trim();
}

function textOfSolution(data) {
  const answer = data?.answer;
  const solution = data?.solution || (answer && typeof answer === 'object' ? answer.solution : '') || '';
  return String(solution || '').trim();
}

function splitTextIntoSteps(raw, limit = MAX_REVEAL_STEPS) {
  const text = String(raw || '').trim();
  if (!text) return [];
  let paragraphs = text.split(/\n\s*\n+/).map(item => item.trim()).filter(Boolean);
  // 单段超长解析按句号/分号切分，避免一整屏文字一次出现。
  if (paragraphs.length === 1 && paragraphs[0].length > 220) {
    const pieces = [];
    let buffer = '';
    for (const sentence of paragraphs[0].split(/(?<=[。；;！!？?])/)) {
      if (buffer && buffer.length + sentence.length > 180) {
        pieces.push(buffer.trim());
        buffer = '';
      }
      buffer += sentence;
    }
    if (buffer.trim()) pieces.push(buffer.trim());
    paragraphs = pieces;
  }
  if (paragraphs.length <= limit) return paragraphs;
  // 过多段落按比例合并成 limit 段，保证「步骤条」不会长到无法操作。
  const size = Math.ceil(paragraphs.length / limit);
  const merged = [];
  for (let index = 0; index < paragraphs.length; index += size) {
    merged.push(paragraphs.slice(index, index + size).join('\n\n'));
  }
  return merged.slice(0, limit);
}

function chunkBlocks(blocks, limit = MAX_REVEAL_STEPS) {
  if (!blocks.length) return [];
  const size = Math.max(1, Math.ceil(blocks.length / limit));
  const chunks = [];
  for (let index = 0; index < blocks.length; index += size) {
    chunks.push(blocks.slice(index, index + size));
  }
  return chunks.slice(0, limit);
}

function buildSteps(data) {
  const steps = [];
  const answerBlocks = blockList(data.answer_blocks);
  const answerText = textOfAnswer(data);
  if (answerBlocks.length || (data.has_answer !== false && answerText)) {
    steps.push({ kind: 'answer', title: t('question.referenceAnswer'), blocks: answerBlocks, text: answerText });
  }
  const solutionBlocks = blockList(data.solution_blocks);
  const solutionText = textOfSolution(data);
  if (solutionBlocks.length) {
    const chunks = chunkBlocks(solutionBlocks);
    for (let index = 0; index < chunks.length; index += 1) {
      steps.push({
        kind: 'solution',
        title: chunks.length > 1 ? t('presentation.solutionStep', { v1: index + 1 }) : t('question.solution'),
        blocks: chunks[index],
        text: '',
      });
    }
  } else if (solutionText) {
    const paragraphs = splitTextIntoSteps(solutionText);
    for (let index = 0; index < paragraphs.length; index += 1) {
      steps.push({
        kind: 'solution',
        title: paragraphs.length > 1 ? t('presentation.solutionStep', { v1: index + 1 }) : t('question.solution'),
        blocks: [],
        text: paragraphs[index],
      });
    }
  }
  return steps;
}

function correctChoiceSet(data) {
  const type = String(data.type || '');
  if (!['single_choice', 'multi_choice'].includes(type)) return new Set();
  const raw = `${textOfAnswer(data)}`.toUpperCase();
  const letters = raw.match(/[A-H]/g);
  if (!letters || !letters.length) return new Set();
  // 参考答案若混入大段解析（出现过多字母）则不猜选项，避免误导。
  return new Set(letters.slice(0, 8));
}

function questionBlocksNode(blocks, data, context) {
  return el('div', { class: 'pm-blocks question-text question-blocks' },
    questionBlocks(blocks, data.subject || '', '', context));
}

function blocksOrTextNode(blocks, text, data, context) {
  if (blocks && blocks.length) return questionBlocksNode(blocks, data, context);
  return questionText(text || '', data.images || [], data.subject || '', '', context);
}

function numberText(data) {
  const number = String(data.number_label || '').trim();
  if (!number) return '';
  return number.startsWith('第') ? number : t('question.originalNumber', { label: number });
}

// ---------------------------------------------------------------------------
// 页面搭建
// ---------------------------------------------------------------------------

function gateView(error) {
  const detail = String(error?.message || '');
  return el('div', { class: 'pm-gate' },
    el('div', { class: 'pm-gate-icon', 'aria-hidden': 'true' }, icon('lock')),
    el('h2', {}, t('presentation.answerFirstToUnlockTheWalkthrough')),
    notice(detail || t('presentation.submitAnAnswerInQuestionBankPractice'), 'warning'),
    el('div', { class: 'pm-gate-actions' }),
  );
}

function errorView(error) {
  return el('div', { class: 'pm-gate' },
    el('div', { class: 'pm-gate-icon', 'aria-hidden': 'true' }, icon('close')),
    el('h2', {}, t('presentation.failedToOpenPresentationView')),
    notice(error?.message || t('presentation.failedToLoadTheQuestionPleaseClose'), 'error'));
}

/**
 * 打开全屏讲解视图。
 * @param {{qid?:string, question?:object, data?:object, onClose?:Function}} options
 *   question: question.js 已加载的题目对象（含答案/解析时不再请求）
 * @returns {Promise<{dialog: HTMLDialogElement, close: Function}|null>}
 */
export async function openPresentationMode(options = {}) {
  if (activeSession) {
    try { activeSession.close(); } catch { /* 关闭旧视图失败不阻塞新视图 */ }
    activeSession = null;
  }
  const qid = String(options.qid || options.question?.qid || options.data?.qid || '').trim();
  if (!qid) {
    toast(t('presentation.missingQuestionIdCannotOpenThePresentation'), true);
    return null;
  }

  const dialog = el('dialog', {
    class: `pm-dialog${storageFlag(DARK_KEY) ? ' pm-dark' : ''}`,
    'aria-label': t('question.presentationMode'),
  });
  const shell = el('div', { class: 'pm-shell' });
  const body = el('div', { class: 'pm-body' }, loading(t('presentation.preparingThePresentationView')));
  shell.append(body);
  dialog.append(shell);
  document.body.append(dialog);
  dialog.showModal();

  const onClose = typeof options.onClose === 'function' ? options.onClose : null;
  let cleaned = false;
  let controller = null;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    try { controller?.dispose(); } catch { /* 资源释放失败不影响关闭 */ }
    dialog.remove();
    activeSession = null;
    try { onClose(); } catch { /* 回调异常不影响关闭 */ }
  };
  dialog.addEventListener('close', cleanup);
  dialog.addEventListener('cancel', () => { /* 浏览器 Esc 会关闭 dialog，close 事件统一清理 */ });
  // 背景点击（点 dialog 自身而非内容）关闭，便于触屏操作。
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  const showGate = error => {
    const view = gateView(error);
    const actions = view.querySelector('.pm-gate-actions');
    actions.append(
      button(t('presentation.answerInTheQuestionBank'), () => { dialog.close(); go('practice', qid); }, '', 'book'),
      button(t('common.close'), () => dialog.close(), 'secondary', 'close')
    );
    body.replaceChildren(el('div', { class: 'pm-center' }, view));
  };

  let data = options.data || options.question || null;
  if (data && data.unlocked === false) {
    showGate(null);
    return { dialog, close: () => dialog.close() };
  }
  if (!data) {
    try {
      data = await apiReveal(qid);
    } catch (error) {
      if (String(error?.message || '').includes('作答') || String(error?.message || '').includes('解锁')) {
        showGate(error);
      } else {
        body.replaceChildren(el('div', { class: 'pm-center' }, errorView(error)));
      }
      return { dialog, close: () => dialog.close() };
    }
  }
  data = data.question || data;
  if (!dialog.isConnected) return null;

  controller = buildPresentation({ dialog, shell, data, qid });
  activeSession = controller;
  return controller;
}

async function apiReveal(qid) {
  // 延迟 import 避免与 core/api 形成模块环（api 依赖 state/dom，本组件被页面按需加载）。
  const { api } = await import('../core/api.js');
  return api(`/reveal?${query({ qid })}`);
}

// ---------------------------------------------------------------------------
// 讲解主体
// ---------------------------------------------------------------------------

function buildPresentation({ dialog, shell, data, qid }) {
  const subject = data.subject || '';
  const assetContext = { qid: data.qid || qid, bankVersion: data.bank_version || '' };
  const steps = buildSteps(data);
  const choices = correctChoiceSet(data);
  const answerText = textOfAnswer(data);
  const hasAnswer = Boolean(answerText || blockList(data.answer_blocks).length);
  const hasSolution = Boolean(textOfSolution(data) || blockList(data.solution_blocks).length);
  const initialStep = -1; // 先只看题干，按「下一步」逐步揭示

  let stepIndex = initialStep;
  let dark = dialog.classList.contains('pm-dark');
  let big = storageFlag(BIG_KEY);
  let annotate = false;
  let tool = 'pen';
  let penColor = PEN_COLORS[0].value;
  let recordState = null;
  let recordTimer = null;
  let recordSeconds = 0;
  const strokes = [];
  let drawingStroke = null;

  dialog.classList.toggle('pm-big', big);

  // ---------- 头部 ----------
  const titleEl = el('h2', { class: 'pm-title' }, numberText(data) || t('presentation.originalPaperWalkthrough'));
  const metaEl = el('p', { class: 'pm-meta-line' },
    [subject, typeLabel(data, null), sourceOf(data), data.qid || qid].filter(Boolean).join(' · '));
  const closeBtn = button(t('presentation.exitPresentation'), () => dialog.close(), 'ghost small', 'close');
  const helpBtn = button(t('presentation.shortcuts'), () => toggleHelp(), 'ghost small', null, { 'aria-expanded': 'false' });
  helpBtn.classList.add('pm-desktop-only');
  const darkBtn = button(t('presentation.dark'), () => toggleDark(), 'ghost small', null, { 'aria-pressed': String(dark) });
  const bigBtn = button(t('presentation.largeText'), () => toggleBig(), 'ghost small', null, { 'aria-pressed': String(big) });
  const fullBtn = button(t('presentation.fullscreen'), () => toggleFullscreen(), 'ghost small', 'presentation');
  fullBtn.classList.add('pm-desktop-only');
  const saveBtn = button(t('presentation.saveOriginalPaperTrace'), event => busyNode(event.currentTarget, exportTracePng), 'soft small', 'download');
  const recordBtn = button(t('presentation.recordWalkthrough'), event => toggleRecording(event.currentTarget), 'ghost small', 'timer');
  recordBtn.classList.add('pm-record-btn');
  const recordHint = el('span', { class: 'pm-record-hint' });
  const recordSupport = detectRecordSupport();
  if (!recordSupport.ok) {
    recordBtn.disabled = true;
    recordBtn.title = recordSupport.reason;
    recordHint.textContent = recordSupport.reason;
  }
  const head = el('header', { class: 'pm-head' },
    el('div', { class: 'pm-head-main' }, titleEl, metaEl),
    el('div', { class: 'pm-head-actions' },
      darkBtn, bigBtn, fullBtn, recordBtn, saveBtn, helpBtn, closeBtn),
    recordHint);

  // ---------- 题目纸面 ----------
  const optionNodes = new Map();
  const paper = el('div', { class: 'pm-paper' });

  const group = data.group || {};
  const materialBlocks = blockList(data.shared_blocks);
  if (group.material_text || materialBlocks.length || (group.material_images || []).length) {
    const material = el('section', { class: 'pm-section pm-material' },
      el('h3', { class: 'pm-label' }, t('presentation.sharedMaterial')));
    if (materialBlocks.length) {
      material.append(questionBlocksNode(materialBlocks, data, assetContext));
    } else {
      material.append(questionText(
        group.material_text || t('presentation.materialImagesBelow'),
        group.material_images || [],
        subject,
        '',
        assetContext
      ));
    }
    if (materialBlocks.length && (group.material_images || []).length) {
      material.append(el('div', { class: 'pm-material-images' },
        questionText('', group.material_images, subject, '', assetContext)));
    }
    paper.append(material);
  }

  const stemTemplate = String(data.stem_plain || data.stem || '').trim();
  const stemBlocks = blockList(data.stem_blocks);
  const stemNode = blocksOrTextNode(stemBlocks, stemTemplate || t('presentation.stemNotAvailable'), data, assetContext);
  paper.append(el('section', { class: 'pm-section pm-stem' },
    el('h3', { class: 'pm-label' }, t('physics.question')),
    stemNode));

  const options = Array.isArray(data.options) ? data.options : [];
  if (options.length) {
    const optionList = el('ol', { class: 'pm-options' });
    for (const option of options) {
      const label = String(option?.label || '').trim();
      const node = el('li', { class: 'pm-option', 'data-label': label });
      node.append(el('span', { class: 'pm-option-label' }, label ? `${label}.` : '·'));
      const blocks = blockList(option?.blocks);
      const optionText = option?.text || option?.content || '';
      node.append(blocks.length
        ? questionBlocksNode(blocks, data, assetContext)
        : questionText(optionText, [], subject, '', assetContext));
      if (label) optionNodes.set(label, node);
      optionList.append(node);
    }
    paper.append(el('section', { class: 'pm-section pm-option-section' },
      el('h3', { class: 'pm-label' }, t('question.options')), optionList));
  }

  const revealArea = el('div', { class: 'pm-reveal-area', 'aria-live': 'polite' });
  const stage = el('div', { class: 'pm-stage' }, paper, revealArea);
  const canvas = el('canvas', { class: 'pm-canvas', 'aria-label': t('presentation.walkthroughAnnotationCanvas') });
  stage.append(canvas);
  const scroll = el('div', { class: 'pm-scroll' }, stage);

  // ---------- 底部：批注工具 + 步骤控制 ----------
  const toolRow = el('div', { class: 'pm-tools', role: 'toolbar', 'aria-label': t('presentation.annotationTools') });
  const annotateBtn = button(t('presentation.annotate'), () => toggleAnnotate(), 'ghost', 'edit', { 'aria-pressed': 'false' });
  const penButtons = new Map();

  function makeToolButton(label, onClick, attrs = {}) {
    return el('button', { type: 'button', class: 'pm-tool-btn', onClick, ...attrs }, label);
  }

  const toolButtons = [];
  for (const color of PEN_COLORS) {
    const node = makeToolButton(color.label, () => selectTool('pen', color.value), {
      'data-tool': 'pen', 'data-color': color.value, 'aria-label': color.label, title: `'${color.label}`,
    });
    toolButtons.push({ node, tool: 'pen', color: color.value });
    penButtons.set(color.value, node);
  }
  const highlightBtn = makeToolButton(t('presentation.highlight'), () => selectTool('highlight'), {
    'data-tool': 'highlight', 'aria-label': t('presentation.highlightPen'), title: t('presentation.semiTransparentHighlightPen'),
  });
  const eraserBtn = makeToolButton(t('presentation.eraser'), () => selectTool('eraser'), {
    'data-tool': 'eraser', 'aria-label': t('presentation.eraser_2'), title: t('presentation.eraser_2'),
  });
  toolButtons.push({ node: highlightBtn, tool: 'highlight' });
  toolButtons.push({ node: eraserBtn, tool: 'eraser' });
  const undoBtn = makeToolButton(t('presentation.undo'), () => undoStroke(), { title: t('presentation.undoTheLastStroke') });
  const clearBtn = makeToolButton(t('settings.clear'), () => clearStrokes(), { title: t('presentation.clearAllAnnotations') });
  const strokeCount = el('span', { class: 'pm-stroke-count' }, t('presentation.0Strokes'));
  toolRow.append(annotateBtn,
    el('span', { class: 'pm-tool-sep', 'aria-hidden': 'true' }),
    ...PEN_COLORS.map(color => penButtons.get(color.value)),
    highlightBtn, eraserBtn,
    el('span', { class: 'pm-tool-sep', 'aria-hidden': 'true' }),
    undoBtn, clearBtn, strokeCount);

  const prevBtn = button(t('common.previous'), () => goStep(-1), 'secondary', null, { 'aria-label': t('presentation.previousStep') });
  const nextBtn = button(t('common.next'), () => goStep(1), '', null, { 'aria-label': t('presentation.nextStep') });
  const showAllBtn = button(t('presentation.showAll'), () => showAll(), 'secondary', 'eye', { title: t('presentation.showAllAnswersAndSolutionsAtOnce') });
  const exitBtn = button(t('presentation.exit'), () => dialog.close(), 'secondary', 'close', { 'aria-label': t('presentation.exitPresentation') });
  exitBtn.classList.add('pm-exit');
  const stepLabel = el('span', { class: 'pm-step-count', 'aria-live': 'polite' }, '');
  const stepBar = el('div', { class: 'pm-stepbar' },
    prevBtn, stepLabel, nextBtn, showAllBtn, exitBtn);
  const foot = el('footer', { class: 'pm-foot' }, toolRow, stepBar);

  // ---------- 快捷键帮助 ----------
  const helpPanel = el('div', { class: 'pm-help', hidden: true },
    el('h3', {}, t('presentation.shortcuts')),
    el('ul', {},
      el('li', {}, t('presentation.spaceNextStepPreviousStepHomeEnd')),
      el('li', {}, t('presentation.fBrowserFullscreenAToggleAnnotationD')),
      el('li', {}, t('presentation.annotationPickAPenColorAndWrite'))),
    el('h3', {}, t('presentation.capabilitiesLimits')),
    el('ul', {},
      el('li', {}, t('presentation.saveOriginalPaperTraceRedrawsTheText')),
      el('li', {}, recordSupport.ok
        ? (recordSupport.mode === 'screen'
          ? t('presentation.recordWalkthroughTheBrowserAsksToCapture')
          : t('presentation.recordWalkthroughThisBrowserDoesNotSupport'))
        : t('presentation.recordingUnavailableThisBrowserDoesNotSupport')),
      el('li', {}, t('presentation.annotationsStayOnThisPageOnlyAnd'))));

  const bodyWrap = el('div', { class: 'pm-body' },
    head,
    el('div', { class: 'pm-scroll-wrap' }, scroll),
    foot,
    helpPanel);
  shell.replaceChildren(bodyWrap);

  // 初始化：只显示题干，下一步才揭示答案。
  renderReveal();
  syncStepUi();
  syncToolUi();
  applyCanvasMode();
  resizeCanvas();
  const observer = typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => { resizeCanvas(); })
    : null;
  observer?.observe(stage);

  // 依赖注入的 api 通过延迟 import 使用（组件可被任意页面按需加载）。

  // ---------- 揭示 / 步骤 ----------
  function renderReveal() {
    revealArea.replaceChildren();
    if (stepIndex < 0) {
      revealArea.append(el('p', { class: 'pm-reveal-hint' },
        steps.length
          ? t('presentation.theAnswerAndSolutionAreNotRevealed')
          : t('presentation.noDisplayableReferenceAnswerOrSolutionYet')));
      syncOptionHighlight();
      return;
    }
    if (!steps.length) {
      revealArea.append(el('p', { class: 'pm-reveal-hint' }, t('presentation.noDisplayableReferenceAnswerOrSolutionYet')));
      syncOptionHighlight();
      return;
    }
    for (let index = 0; index <= stepIndex && index < steps.length; index += 1) {
      const step = steps[index];
      const node = el('section', {
        class: `pm-step pm-step-${step.kind}${index === stepIndex ? ' pm-step-current' : ''}`,
        'data-step-index': String(index),
      },
        el('h3', { class: 'pm-step-title' },
          el('span', { class: 'pm-step-dot', 'aria-hidden': 'true' }, index === stepIndex ? icon('chevron') : ''),
          step.title),
      );
      if (step.blocks.length) node.append(questionBlocksNode(step.blocks, data, assetContext));
      else if (step.text) node.append(markdown(step.text));
      revealArea.append(node);
    }
    if (stepIndex >= steps.length - 1) {
      revealArea.append(el('p', { class: 'pm-reveal-end' }, t('presentation.theAnswerAndSolutionAreFullyRevealed')));
    }
    syncOptionHighlight();
  }

  function goStep(delta) {
    const maxIndex = steps.length - 1;
    stepIndex = Math.max(-1, Math.min(maxIndex, stepIndex + delta));
    renderReveal();
    syncStepUi();
    if (delta > 0 && stepIndex >= -1) {
      const latest = revealArea.querySelector('.pm-step-current');
      latest?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function showAll() {
    stepIndex = steps.length - 1;
    renderReveal();
    syncStepUi();
    revealArea.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function syncStepUi() {
    const total = steps.length;
    if (!total) {
      stepLabel.textContent = t('presentation.noAnswerSolutionYet');
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      showAllBtn.disabled = true;
      dialog.dataset.step = '-1';
      return;
    }
    const current = stepIndex + 1;
    stepLabel.textContent = stepIndex < 0 ? t('presentation.questionSteps', { v1: total }) : t('presentation.step', { v1: current, v2: total });
    prevBtn.disabled = stepIndex <= -1;
    nextBtn.disabled = stepIndex >= total - 1;
    showAllBtn.disabled = stepIndex >= total - 1;
    dialog.dataset.step = String(stepIndex);
  }

  function syncOptionHighlight() {
    if (!optionNodes.size) return;
    const revealed = stepIndex >= 0;
    for (const [label, node] of optionNodes.entries()) {
      const correct = revealed && choices.has(label);
      node.classList.toggle('pm-option-correct', correct);
      if (correct) node.setAttribute('data-correct', 'true');
      else node.removeAttribute('data-correct');
    }
  }

  // ---------- 深色 / 大字 / 全屏 ----------
  function toggleDark() {
    dark = !dark;
    dialog.classList.toggle('pm-dark', dark);
    darkBtn.setAttribute('aria-pressed', String(dark));
    saveFlag(DARK_KEY, dark);
    redrawStrokes();
  }

  function toggleBig() {
    big = !big;
    dialog.classList.toggle('pm-big', big);
    bigBtn.setAttribute('aria-pressed', String(big));
    saveFlag(BIG_KEY, big);
  }

  function toggleFullscreen() {
    const target = dialog;
    try {
      if (!document.fullscreenElement) {
        target.requestFullscreen?.().catch(() => toast(t('presentation.thisBrowserDoesNotAllowPageFullscreen')));
      } else {
        document.exitFullscreen?.().catch(() => {});
      }
    } catch {
      toast(t('presentation.thisBrowserDoesNotAllowPageFullscreen'));
    }
  }

  function onFullscreenChange() {
    const isFull = Boolean(document.fullscreenElement);
    fullBtn.textContent = '';
    fullBtn.append(icon('presentation'), document.createTextNode(isFull ? t('presentation.exitFullscreen') : t('presentation.fullscreen')));
  }
  document.addEventListener('fullscreenchange', onFullscreenChange);

  // ---------- 快捷键 ----------
  function onKeydown(event) {
    if (!dialog.open) return;
    const target = event.target;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    const key = event.key;
    if (key === 'ArrowRight' || key === 'PageDown' || key === ' ') {
      event.preventDefault();
      goStep(1);
    } else if (key === 'ArrowLeft' || key === 'PageUp') {
      event.preventDefault();
      goStep(-1);
    } else if (key === 'Home') {
      event.preventDefault();
      stepIndex = -1;
      renderReveal();
      scrollToTop();
      syncStepUi();
    } else if (key === 'End') {
      event.preventDefault();
      showAll();
    } else if (key === 'f' || key === 'F') {
      event.preventDefault();
      toggleFullscreen();
    } else if (key === 'a' || key === 'A') {
      event.preventDefault();
      toggleAnnotate();
    } else if (key === 'd' || key === 'D') {
      event.preventDefault();
      toggleDark();
    } else if (key === 'b' || key === 'B') {
      event.preventDefault();
      toggleBig();
    } else if (key === '?' || (key === '/' && event.shiftKey)) {
      event.preventDefault();
      toggleHelp();
    }
  }
  dialog.addEventListener('keydown', onKeydown);

  function toggleHelp() {
    const open = helpPanel.hidden;
    helpPanel.hidden = !open;
    helpBtn.setAttribute('aria-expanded', String(open));
  }

  function scrollToTop() {
    scroll.scrollTop = 0;
  }

  // ---------- 批注画布 ----------
  function canvasMetrics() {
    const rect = stage.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    return { rect, dpr, width: Math.max(1, rect.width), height: Math.max(1, rect.height) };
  }

  function resizeCanvas() {
    const { rect, dpr } = canvasMetrics();
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width === width && canvas.height === height) {
      redrawStrokes();
      return;
    }
    canvas.width = width;
    canvas.height = height;
    redrawStrokes();
  }

  function redrawStrokes() {
    const { rect, dpr } = canvasMetrics();
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const stroke of strokes) {
      drawStroke(ctx, stroke, rect.width, rect.height);
    }
  }

  // 深色模式下把深色笔迹换成亮色，保证在深色纸面上仍可读（导出仍用原色，白底不受影响）。
  const DARK_INK = { '#c0392b': '#ff8a7a', '#1f5fa9': '#8fb8ff', '#1b2a2f': '#eef6f2' };

  function drawStroke(ctx, stroke, width, height) {
    if (!stroke.points.length) return;
    const inkColor = dark ? (DARK_INK[stroke.color] || stroke.color) : stroke.color;
    ctx.save();
    if (stroke.tool === 'eraser') {
      ctx.globalCompositeOperation = 'destination-out';
      ctx.globalAlpha = 1;
      ctx.lineWidth = ERASER_WIDTH;
    } else if (stroke.tool === 'highlight') {
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 0.3;
      ctx.lineWidth = HIGHLIGHT_WIDTH;
    } else {
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
      ctx.lineWidth = PEN_WIDTH;
    }
    ctx.strokeStyle = stroke.tool === 'highlight' ? HIGHLIGHT_COLOR : inkColor;
    ctx.beginPath();
    stroke.points.forEach((point, index) => {
      const x = point.x * width;
      const y = point.y * height;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (stroke.points.length === 1) {
      // 单点轻触也留下一个圆点，符合「点一下标记」的直觉。
      const point = stroke.points[0];
      ctx.arc(point.x * width, point.y * height, Math.max(1.4, ctx.lineWidth / 2), 0, Math.PI * 2);
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fill();
    } else {
      ctx.stroke();
    }
    ctx.restore();
  }

  function normalizedPoint(event) {
    const rect = stage.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    };
  }

  function strokeWidthFor(toolId) {
    if (toolId === 'eraser') return ERASER_WIDTH;
    if (toolId === 'highlight') return HIGHLIGHT_WIDTH;
    return PEN_WIDTH;
  }

  function onPointerDown(event) {
    if (!annotate) return;
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    canvas.setPointerCapture?.(event.pointerId);
    drawingStroke = {
      tool,
      color: penColor,
      width: strokeWidthFor(tool),
      points: [normalizedPoint(event)],
    };
    strokes.push(drawingStroke);
    redrawStrokes();
    updateStrokeCount();
  }

  function onPointerMove(event) {
    if (!annotate || !drawingStroke) return;
    event.preventDefault();
    const last = drawingStroke.points[drawingStroke.points.length - 1];
    const next = normalizedPoint(event);
    // 采样阈值避免一笔笔尖抖动产生过多点。
    if (last && Math.abs(last.x - next.x) * canvas.width < 1.5 && Math.abs(last.y - next.y) * canvas.height < 1.5) return;
    drawingStroke.points.push(next);
    redrawStrokes();
  }

  function endStroke(event) {
    if (!drawingStroke) return;
    drawingStroke = null;
    updateStrokeCount();
    if (event) {
      try { canvas.releasePointerCapture?.(event.pointerId); } catch { /* ignore */ }
    }
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', endStroke);
  canvas.addEventListener('pointercancel', endStroke);
  canvas.addEventListener('pointerleave', event => {
    if (drawingStroke && event.buttons === 0) endStroke(event);
  });

  function updateStrokeCount() {
    strokeCount.textContent = t('presentation.strokes', { v1: strokes.filter(item => item.tool !== 'eraser').length });
    dialog.dataset.strokeCount = String(strokes.length);
  }

  function toggleAnnotate() {
    annotate = !annotate;
    annotateBtn.setAttribute('aria-pressed', String(annotate));
    annotateBtn.classList.toggle('active', annotate);
    applyCanvasMode();
    if (annotate) toast(t('presentation.annotationIsOnWriteDirectlyOnThe'));
  }

  function applyCanvasMode() {
    canvas.classList.toggle('pm-canvas-active', annotate);
    stage.classList.toggle('pm-annotating', annotate);
    dialog.dataset.annotating = String(annotate);
    if (!annotate) endStroke();
  }

  function selectTool(nextTool, color = null) {
    tool = nextTool;
    if (color) penColor = color;
    if (!annotate) toggleAnnotate();
    syncToolUi();
  }

  function syncToolUi() {
    for (const item of toolButtons) {
      if (!item.node) continue;
      const active = item.tool === tool && (item.color ? item.color === penColor : true);
      item.node.classList.toggle('active', Boolean(active));
      item.node.setAttribute('aria-pressed', String(Boolean(active)));
    }
    annotateBtn.classList.toggle('active', annotate);
    annotateBtn.setAttribute('aria-pressed', String(annotate));
  }

  function undoStroke() {
    if (!strokes.length) {
      toast(t('presentation.noAnnotationsToUndo'));
      return;
    }
    strokes.pop();
    redrawStrokes();
    updateStrokeCount();
  }

  function clearStrokes() {
    strokes.length = 0;
    redrawStrokes();
    updateStrokeCount();
  }

  // 页面高度变化（揭示答案、字体切换）由 ResizeObserver 触发画布重算，笔迹按归一化坐标跟随。

  // ---------- 保存原卷留痕（Canvas 重绘，无 html2canvas 依赖） ----------
  async function exportTracePng() {
    try {
      const stageRect = stage.getBoundingClientRect();
      const stageWidth = Math.max(320, stageRect.width);
      const stageHeight = Math.max(320, stageRect.height);
      const scale = Math.max(1, Math.min(2, 1800 / stageWidth, 4096 / stageHeight));
      const width = Math.round(stageWidth * scale);
      const height = Math.round(stageHeight * scale);
      const out = document.createElement('canvas');
      out.width = width;
      out.height = height;
      const ctx = out.getContext('2d');
      if (!ctx) throw new Error(t('presentation.thisBrowserDoesNotSupportCanvasExport'));
      ctx.fillStyle = '#fffefa';
      ctx.fillRect(0, 0, width, height);
      // 文字重绘（标题/题目/选项/已揭示答案解析），配图随后按屏幕位置绘制。
      let y = drawTraceText(ctx, { data, steps, stepIndex, width, height, scale, subject });
      y = await drawTraceImages(ctx, { stageRect, scale, width, y });
      drawTraceStrokes(ctx, width, height, scale);
      drawTraceFooter(ctx, { width, height, scale, qid });
      await ensureCanvasBlob(out, width, height);
      downloadBlob(await toBlob(out, 'image/png'), t('presentation.originalPaperTracePng', { v1: safeName(data.qid || qid), v2: Date.now() }));
      toast(t('presentation.originalPaperTracePngGeneratedTextRedraw'));
    } catch (error) {
      // 降级：浏览器拒绝导出含跨源图片的画布时，只导出批注层并明确说明。
      try {
        const stageRect = stage.getBoundingClientRect();
        const scale = 2;
        const width = Math.round(stageRect.width * scale);
        const height = Math.round(stageRect.height * scale);
        const fallback = document.createElement('canvas');
        fallback.width = width;
        fallback.height = height;
        const ctx = fallback.getContext('2d');
        ctx.fillStyle = '#fffefa';
        ctx.fillRect(0, 0, width, height);
        drawTraceStrokes(ctx, width, height, scale);
        downloadBlob(await toBlob(fallback, 'image/png'), t('presentation.originalPaperTraceAnnotationsPng', { v1: safeName(data.qid || qid) }));
        toast(t('presentation.theBrowserBlockedTheQuestionSnapshotSo'), true);
      } catch (fallbackError) {
        toast(t('presentation.saveTraceFailed', { v1: error?.message || fallbackError?.message || t('presentation.browserUnsupported') }), true);
      }
    }
  }

  // ---------- 录制（MediaRecorder） ----------
  async function toggleRecording(trigger) {
    if (recordState) {
      stopRecording();
      return;
    }
    if (!recordSupport.ok) {
      toast(recordSupport.reason, true);
      return;
    }
    trigger.disabled = true;
    try {
      const stream = await captureRecordingStream();
      if (!stream) {
        toast(t('presentation.noCapturableScreenWasProvidedScreenSharing'), true);
        trigger.disabled = false;
        return;
      }
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType, videoBitsPerSecond: 4_000_000 } : undefined);
      const chunks = [];
      const extraStreams = stream.__extraStreams || [];
      recorder.addEventListener('dataavailable', event => {
        if (event.data && event.data.size > 0) chunks.push(event.data);
      });
      recorder.addEventListener('stop', () => {
        try {
          const blob = new Blob(chunks, { type: recorder.mimeType || 'video/webm' });
          if (blob.size > 0) downloadBlob(blob, t('presentation.walkthroughRecordingWebm', { v1: safeName(data.qid || qid), v2: Date.now() }));
          toast(t('presentation.recordingStoppedTheWebmDownloadHasStarted'));
        } catch {
          toast(t('presentation.failedToGenerateTheRecordingFilePlease'), true);
        }
        stopAllTracks([stream, ...extraStreams]);
        cleanupRecording();
        trigger.disabled = false;
      });
      recordState = { recorder, stream, extraStreams, startedAt: Date.now() };
      recorder.start(1000);
      recordSeconds = 0;
      recordTimer = window.setInterval(() => { recordSeconds += 1; syncRecordUi(); }, 1000);
      syncRecordUi();
      trigger.disabled = false;
      toast(recordSupport.mode === 'screen'
        ? t('presentation.startRecordingTheWalkthroughASystemPrompt')
        : t('presentation.thisBrowserCanOnlyRecordTheAnnotation'));
    } catch (error) {
      cleanupRecording();
      trigger.disabled = false;
      toast(t('presentation.recordingFailed', { v1: error?.message || t('presentation.browserOrPermissionLimit') }), true);
    }
  }

  function syncRecordUi() {
    if (recordState) {
      const mins = String(Math.floor(recordSeconds / 60)).padStart(2, '0');
      const secs = String(recordSeconds % 60).padStart(2, '0');
      recordBtn.classList.add('pm-recording');
      recordBtn.replaceChildren(el('span', { class: 'pm-record-dot', 'aria-hidden': 'true' }), document.createTextNode(t('presentation.stopRecording', { v1: mins, v2: secs })));
      recordBtn.setAttribute('aria-label', t('presentation.stopRecordingTheWalkthrough'));
    } else {
      recordBtn.classList.remove('pm-recording');
      recordBtn.replaceChildren(icon('timer'), document.createTextNode(t('presentation.recordWalkthrough')));
      recordBtn.setAttribute('aria-label', t('presentation.recordWalkthrough'));
    }
  }

  function stopRecording() {
    if (!recordState) return;
    try { recordState.recorder.stop(); } catch { cleanupRecording(); }
  }

  function cleanupRecording() {
    if (recordTimer) {
      window.clearInterval(recordTimer);
      recordTimer = null;
    }
    if (recordState) {
      stopAllTracks([recordState.stream, ...(recordState.extraStreams || [])]);
    }
    recordState = null;
    syncRecordUi();
  }

  // 清理
  function dispose() {
    document.removeEventListener('fullscreenchange', onFullscreenChange);
    if (observer) {
      try { observer.disconnect(); } catch { /* ignore */ }
    }
    try { cleanupRecording(); } catch { /* ignore */ }
    try {
      if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    } catch { /* ignore */ }
    canvas.removeEventListener('pointerdown', onPointerDown);
    canvas.removeEventListener('pointermove', onPointerMove);
    canvas.removeEventListener('pointerup', endStroke);
    canvas.removeEventListener('pointercancel', endStroke);
  }

  // Esc 由 dialog 原生处理；F/A/D/B/空格等自定义键只监听 dialog 内部（含焦点在按钮上）。
  dialog.addEventListener('keydown', onKeydown);
  dialog.__pmSession = {
    get strokes() { return strokes; },
    debug: () => ({ step: stepIndex, steps: steps.length, strokes: strokes.length, annotating: annotate }),
  };

  return {
    dialog,
    close: () => dialog.close(),
    dispose,
  };
}

// ---------------------------------------------------------------------------
// 录制公共能力
// ---------------------------------------------------------------------------

function detectRecordSupport() {
  if (typeof window.MediaRecorder !== 'function') {
    return { ok: false, reason: t('presentation.thisBrowserDoesNotSupportMediarecorderSo') };
  }
  const media = navigator.mediaDevices;
  if (media && typeof media.getDisplayMedia === 'function') {
    return { ok: true, mode: 'screen' };
  }
  if (typeof HTMLCanvasElement.prototype.captureStream === 'function') {
    return { ok: true, mode: 'canvas' };
  }
  return { ok: false, reason: t('presentation.thisBrowserDoesNotSupportScreenCapture') };
}

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = [
    'video/webm;codecs=vp9,opus',
    'video/webm;codecs=vp8,opus',
    'video/webm;codecs=vp9',
    'video/webm',
  ];
  return candidates.find(type => {
    try { return MediaRecorder.isTypeSupported(type); } catch { return false; }
  }) || '';
}

async function captureRecordingStream() {
  const extraStreams = [];
  let stream = null;
  if (detectRecordSupport().mode === 'screen') {
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: false });
    } catch {
      return null;
    }
  } else {
    // 退化为批注画布录制（题目文字不在画布内，UI 帮助面板已说明）。
    const target = document.querySelector('.pm-canvas');
    stream = target?.captureStream ? target.captureStream(30) : null;
  }
  if (!stream) return null;
  try {
    const audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    extraStreams.push(audioStream);
    const combined = new MediaStream([...stream.getVideoTracks(), ...audioStream.getAudioTracks()]);
    combined.__extraStreams = extraStreams;
    return combined;
  } catch {
    // 麦克风被拒绝时继续录制画面。
    const combined = new MediaStream(stream.getVideoTracks());
    combined.__extraStreams = extraStreams;
    return combined;
  }
}

function stopAllTracks(streams) {
  for (const stream of streams || []) {
    for (const track of stream?.getTracks?.() || []) {
      try { track.stop(); } catch { /* ignore */ }
    }
  }
}

// ---------------------------------------------------------------------------
// 原卷留痕绘制（Canvas 重绘）
// ---------------------------------------------------------------------------

function safeName(value) {
  return String(value || 'question').replace(/[^\w\u4e00-\u9fff-]+/g, '_').slice(0, 80);
}

function wrapLines(ctx, text, maxWidth) {
  const lines = [];
  const paragraphs = String(text || '').split('\n');
  for (const paragraph of paragraphs) {
    if (!paragraph.trim()) {
      lines.push('');
      continue;
    }
    let buffer = '';
    for (const char of paragraph) {
      const next = buffer + char;
      if (ctx.measureText(next).width > maxWidth && buffer) {
        lines.push(buffer);
        buffer = char;
      } else {
        buffer = next;
      }
    }
    lines.push(buffer);
  }
  return lines;
}

function drawTraceText(ctx, { data, steps, stepIndex, width, height, scale, subject }) {
  const margin = Math.round(48 * scale);
  const maxWidth = width - margin * 2;
  let y = margin;
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#182927';
  ctx.font = `600 ${Math.round(22 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
  ctx.fillText(t('presentation.originalPaperTrace'), margin, y);
  y += Math.round(32 * scale);
  ctx.font = `${Math.round(13 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
  ctx.fillStyle = '#556761';
  ctx.fillText([
    subject, typeLabel(data, null), numberText(data), sourceOf(data), data.qid,
    dateText(new Date().toISOString()),
  ].filter(Boolean).join(' · '), margin, y);
  y += Math.round(30 * scale);

  ctx.fillStyle = '#182927';
  ctx.font = `600 ${Math.round(16 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
  ctx.fillText(t('physics.question'), margin, y);
  y += Math.round(26 * scale);
  ctx.font = `${Math.round(15 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
  const stem = String(data.stem_plain || data.stem || '').trim() || t('presentation.stemNotAvailable');
  for (const line of wrapLines(ctx, stem, maxWidth)) {
    ctx.fillText(line, margin, y);
    y += Math.round(24 * scale);
  }
  y += Math.round(6 * scale);
  const options = Array.isArray(data.options) ? data.options : [];
  for (const option of options) {
    const label = String(option?.label || '').trim();
    const optionText = option?.text || option?.content || '';
    const raw = `${label ? `${label}. ` : ''}${optionText}`.trim();
    if (!raw) continue;
    for (const line of wrapLines(ctx, raw, maxWidth)) {
      ctx.fillText(line, margin + Math.round(8 * scale), y);
      y += Math.round(22 * scale);
    }
  }
  y += Math.round(8 * scale);

  if (stepIndex >= 0 && steps.length) {
    for (let index = 0; index <= stepIndex && index < steps.length; index += 1) {
      const step = steps[index];
      ctx.font = `600 ${Math.round(15 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
      ctx.fillStyle = '#194e3e';
      ctx.fillText(step.title, margin, y);
      y += Math.round(24 * scale);
      ctx.fillStyle = '#182927';
      ctx.font = `${Math.round(14 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
      const content = step.text || blocksToPlainText(step.blocks) || '';
      for (const line of wrapLines(ctx, content, maxWidth)) {
        if (y > height - Math.round(60 * scale)) break;
        ctx.fillText(line, margin, y);
        y += Math.round(22 * scale);
      }
      y += Math.round(10 * scale);
    }
  }
  return y;
}

function blocksToPlainText(blocks) {
  return (blocks || []).map(block => {
    if (!block || typeof block !== 'object') return '';
    if (block.kind === 'math') return block.text || block.latex || '';
    if (block.kind === 'table') {
      const rows = block.table?.rows || block.rows || [];
      return rows.map(row => (Array.isArray(row) ? row.join(' | ') : String(row ?? ''))).join('\n');
    }
    if (block.kind === 'image') return t('presentation.image');
    return block.text || '';
  }).filter(Boolean).join('\n');
}

async function drawTraceImages(ctx, { stageRect, scale, width, y }) {
  const dialog = document.querySelector('.pm-dialog');
  const images = dialog ? [...dialog.querySelectorAll('.pm-stage img')] : [];
  const loaded = images.filter(img => img.complete && img.naturalWidth > 0);
  const baseY = Math.round(y);
  for (const img of loaded) {
    const rect = img.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) continue;
    try {
      ctx.drawImage(
        img,
        Math.round((rect.left - stageRect.left) * scale),
        Math.round((rect.top - stageRect.top) * scale),
        Math.round(rect.width * scale),
        Math.round(rect.height * scale)
      );
    } catch {
      // 单张图片绘制失败（跨源/权限）不阻断整体导出，后面 toBlob 失败还会整体降级。
    }
  }
  return Math.max(y, baseY);
}

function drawTraceStrokes(ctx, width, height, scale = 1) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const stroke of strokesOfStage()) {
    if (!stroke.points.length) continue;
    ctx.save();
    if (stroke.tool === 'eraser') {
      ctx.globalCompositeOperation = 'destination-out';
      ctx.globalAlpha = 1;
      ctx.lineWidth = ERASER_WIDTH * scale;
    } else if (stroke.tool === 'highlight') {
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 0.3;
      ctx.lineWidth = HIGHLIGHT_WIDTH * scale;
    } else {
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
      ctx.lineWidth = PEN_WIDTH * scale;
    }
    ctx.strokeStyle = stroke.tool === 'highlight' ? HIGHLIGHT_COLOR : stroke.color;
    ctx.beginPath();
    stroke.points.forEach((point, index) => {
      const x = point.x * width;
      const py = point.y * height;
      if (index === 0) ctx.moveTo(x, py);
      else ctx.lineTo(x, py);
    });
    if (stroke.points.length === 1) {
      const point = stroke.points[0];
      ctx.arc(point.x * width, point.y * height, Math.max(2, ctx.lineWidth / 2), 0, Math.PI * 2);
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fill();
    } else {
      ctx.stroke();
    }
    ctx.restore();
  }
  ctx.restore();
}

// 通过 dialog 上的调试引用拿当前笔迹（避免把闭包暴露到全局）。
function strokesOfStage() {
  const dialog = document.querySelector('.pm-dialog');
  const host = dialog?.__pmSession;
  return host?.strokes || [];
}

function drawTraceFooter(ctx, { width, height, scale, qid }) {
  const margin = Math.round(48 * scale);
  ctx.save();
  ctx.fillStyle = '#7a8a84';
  ctx.font = `${Math.round(11 * scale)}px "Noto Sans SC", "Microsoft YaHei", sans-serif`;
  ctx.textBaseline = 'bottom';
  ctx.fillText(
    t('presentation.originalPaperTraceTextRedrawImagePositioning', { v1: qid }),
    margin,
    height - Math.round(22 * scale)
  );
  ctx.restore();
  void width;
}

async function ensureCanvasBlob(canvas, width, height) {
  // 触发一次尺寸校验：部分浏览器对超大画布会在 toBlob 阶段才报错。
  if (!canvas.width || !canvas.height) throw new Error(t('presentation.invalidExportCanvasSize'));
  if (width * height > 40_000_000) throw new Error(t('presentation.theExportCanvasIsTooLargeCollapse'));
}

function toBlob(canvas, type) {
  return new Promise((resolve, reject) => {
    try {
      canvas.toBlob(blob => {
        if (blob) resolve(blob);
        else reject(new Error(t('presentation.theBrowserCouldNotGenerateTheImage')));
      }, type, 0.92);
    } catch (error) {
      reject(error);
    }
  });
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = el('a', { href: url, download: filename });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function busyNode(node, action) {
  if (!node || node.disabled) return Promise.resolve();
  const restore = () => {
    if (node.isConnected) node.disabled = false;
  };
  node.disabled = true;
  return Promise.resolve()
    .then(action)
    .catch(error => toast(error?.message || t('common.actionFailed'), true))
    .finally(restore);
}
