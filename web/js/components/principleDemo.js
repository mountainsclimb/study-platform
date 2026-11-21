/**
 * 统一原理演示播放接口（方案 §5.4 / §8 工单 D：离线示例 + 统一播放器）。
 *
 * 挂载时按 artifact.kind 分派：
 * - `flow_animation` → flow.js（结构化流程 SVG，定时器按步骤播放）；
 * - `isometric`      → iso.js（标准 30° 等距结构 SVG，可切换拆解/装配方向）；
 * - `demo_3d`        → threeLite.js 的本地 Three.js 最小视图；无 WebGL 或动态
 *                      import 失败时自动降级为等距示意图，并在顶部显示中文提示，
 *                      绝不让异常冒到页面。
 *
 * controller（返回值）：
 *   { destroy(), play(), pause(), seek(stepIndex), step(direction), getStep(),
 *     getStepCount(), getKind(), getMode() /* 'svg'|'three'|'poster' * /, element,
 *     getDirection?(), setDirection?(direction), ready? }
 * 所有渲染器共享：键盘步进（←/→、Space、Home/End）、进度条（第 n/N 步）、
 * 步骤说明、aria-live 步骤区、每次渲染后的 onFrameReady({step,mode,ts})。
 *
 * 安全：不拼 innerHTML、不 eval；three.js 只从本地 vendor 动态 import。
 */

import { el } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { createFlowView, renderFlowPoster } from '../../vendor/principles/flow.js';
import { createIsoView, renderIsoPoster } from '../../vendor/principles/iso.js';
import { createThreeView, isWebGLAvailable } from '../../vendor/principles/threeLite.js';
import { PRESET_DEMOS, PRESET_LIST } from '../../vendor/principles/presets.js';

// 离线示例预设（与 question.js 共用；只读对象）。
// M4：PRESET_DEMOS 新增 loop/timeline/state 流程与 anatomy/anima 等距示例，
// PRESET_LIST 提供稳定展示顺序，「查看原理」弹层的切换下拉按 kind 分组使用。
export { PRESET_DEMOS, PRESET_LIST };

const DEFAULT_STEP_MS = 1200;
const MIN_STEP_MS = 200;

/** kind → 内部渲染模式。 */
const KIND_MODE = Object.freeze({
  flow_animation: 'flow',
  isometric: 'iso',
  demo_3d: 'three',
});

const MODE_KEYS = Object.freeze({
  flow: 'study.demo.modeFlow',
  iso: 'study.demo.modeIso',
  three: 'study.demo.modeThree',
});

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function stepIntervalOf(artifact) {
  const raw = Number(artifact?.data?.stepIntervalMs);
  return Number.isFinite(raw) && raw >= MIN_STEP_MS ? raw : DEFAULT_STEP_MS;
}

function modeLabel(mode) {
  const key = MODE_KEYS[mode];
  return key ? t(key) : t('study.demo.modeUnknown');
}

function diagramAriaLabel(title) {
  return t('study.demo.diagramAria', { v1: title });
}

/** 把任意数据渲染成静态海报 SVG（失败时返回最小占位 SVG，绝不抛错）。 */
function safePosterSvg(artifact) {
  const art = artifact && typeof artifact === 'object' ? artifact : {};
  const title = String(art.title || '');
  const ariaLabel = t('study.demo.posterAria', { v1: title || t('study.demo.untitled') });
  let footer = t('study.demo.posterNote');
  if (art.kind === 'demo_3d') footer = `${t('study.demo.poster3d')} · ${footer}`;
  try {
    const data = art.data || {};
    const isFlow = art.kind === 'flow_animation'
      || (!Array.isArray(data.parts) && Array.isArray(data.nodes));
    if (isFlow) {
      return renderFlowPoster(data, { title, footer, ariaLabel });
    }
    return renderIsoPoster(data, { title, footer, ariaLabel });
  } catch {
    // 海报兜底：仍然返回合法 SVG，由页面展示「生成失败」而不是崩溃。
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 640 360');
    svg.setAttribute('class', 'pd-poster-svg');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', ariaLabel);
    const text = document.createElementNS(NS, 'text');
    text.setAttribute('x', '320');
    text.setAttribute('y', '180');
    text.setAttribute('text-anchor', 'middle');
    text.textContent = t('study.demo.posterFailed');
    svg.append(text);
    return svg;
  }
}

/**
 * 无交互静态海报（导出 / 降级用）。
 * @param {{kind:string,title?:string,data:object}} artifact
 * @returns {SVGSVGElement}
 */
export function renderPrinciplePoster(artifact) {
  return safePosterSvg(artifact);
}

/**
 * 渲染统一原理演示播放器。
 * @param {HTMLElement} container 承载容器（会被替换内容）
 * @param {{kind:string,title?:string,data:object,source?:string,assumptions?:string[]}} artifact
 * @param {{onStepChange?:Function,onFrameReady?:Function,autoplay?:boolean,keyboard?:boolean}} [options]
 * @returns {object} controller
 */
export function renderPrincipleDemo(container, artifact, options = {}) {
  if (!container || typeof container.replaceChildren !== 'function') {
    throw new Error('renderPrincipleDemo 需要有效的容器元素');
  }
  const art = artifact && typeof artifact === 'object' ? artifact : {};
  const kind = String(art.kind || '');
  const baseMode = KIND_MODE[kind] || '';
  const titleText = String(art.title || t('study.demo.untitled'));

  const root = el('div', {
    class: 'pd-demo',
    role: 'application',
    'aria-label': t('study.demo.ariaLabel', { v1: titleText }),
    tabindex: '0',
    'data-demo-kind': kind || 'unknown',
  });
  const notice = el('div', { class: 'pd-notice', role: 'status', hidden: true });
  const stage = el('div', { class: 'pd-stage' });
  const notesHost = el('div', { class: 'pd-notes', hidden: true });
  const head = el('div', { class: 'pd-head' },
    el('div', { class: 'pd-head-main' },
      el('strong', { class: 'pd-title', 'data-no-i18n': 'true' }, titleText),
      el('span', { class: 'pd-kind-badge' }, modeLabel(baseMode))),
    el('p', { class: 'pd-hint' }, t('study.demo.keyboardHint')));

  const prevButton = el('button', {
    type: 'button', class: 'pd-ctrl', 'aria-label': t('study.demo.prevStep'), title: t('study.demo.prevStep'),
  }, '⏮');
  const playButton = el('button', {
    type: 'button', class: 'pd-ctrl is-primary', 'aria-label': t('study.demo.play'), title: t('study.demo.play'),
  }, '▶');
  const nextButton = el('button', {
    type: 'button', class: 'pd-ctrl', 'aria-label': t('study.demo.nextStep'), title: t('study.demo.nextStep'),
  }, '⏭');
  const resetButton = el('button', {
    type: 'button', class: 'pd-ctrl', 'aria-label': t('study.demo.reset'), title: t('study.demo.reset'),
  }, '↺');

  const directionGroup = el('div', {
    class: 'pd-direction', role: 'group', 'aria-label': t('study.demo.directionLabel'), hidden: true,
  });
  const disassembleButton = el('button', {
    type: 'button', class: 'pd-direction-btn', 'aria-pressed': 'false',
  }, t('study.demo.disassemble'));
  const assembleButton = el('button', {
    type: 'button', class: 'pd-direction-btn', 'aria-pressed': 'false',
  }, t('study.demo.assemble'));
  directionGroup.append(disassembleButton, assembleButton);

  const range = el('input', {
    type: 'range', class: 'pd-range', min: '0', max: '0', step: '1', value: '0',
    'aria-label': t('study.demo.progressLabel'),
  });
  const progressText = el('span', { class: 'pd-progress-text' });
  const progressRow = el('div', { class: 'pd-progress-row' }, range, progressText);

  const stepTitle = el('strong', { class: 'pd-step-title', 'data-no-i18n': 'true' });
  const stepDetail = el('p', { class: 'pd-step-detail', 'data-no-i18n': 'true' });
  const stepBox = el('div', {
    class: 'pd-step', 'aria-live': 'polite', 'aria-atomic': 'true',
  }, stepTitle, stepDetail);

  const controls = el('div', { class: 'pd-controls' },
    el('div', { class: 'pd-ctrl-row' },
      el('div', { class: 'pd-ctrl-group' }, prevButton, playButton, nextButton, resetButton),
      directionGroup),
    progressRow,
    stepBox);

  root.append(head, notice, stage, controls, notesHost);
  container.replaceChildren(root);

  /* ----------------------------------------------------------------- *
   * 内部状态
   * ----------------------------------------------------------------- */
  const state = {
    view: null,
    mode: 'poster',
    step: 0,
    playing: false,
    timer: 0,
    destroyed: false,
    direction: null,
  };
  let readyResolve = null;
  const ready = new Promise((resolve) => { readyResolve = resolve; });
  let readySettled = false;

  function settleReady() {
    if (readySettled) return;
    readySettled = true;
    if (readyResolve) readyResolve(controller);
  }

  function emitFrame() {
    if (typeof options.onFrameReady !== 'function') return;
    try {
      options.onFrameReady({ step: state.step, mode: state.mode, ts: Date.now() });
    } catch {
      /* 帧回执回调异常不应该影响演示播放 */
    }
  }

  function emitStepChange() {
    if (typeof options.onStepChange !== 'function') return;
    try {
      options.onStepChange(state.step);
    } catch {
      /* 同上 */
    }
  }

  function showNotice(text) {
    if (!text) {
      notice.hidden = true;
      notice.textContent = '';
      return;
    }
    notice.hidden = false;
    notice.textContent = String(text);
  }

  function setLoading(loading) {
    root.dataset.loading = loading ? 'true' : 'false';
    const old = stage.querySelector('.pd-loading');
    if (loading) {
      if (!old) stage.append(el('div', { class: 'pd-loading' }, el('span', { class: 'spinner' }), t('study.demo.loading')));
      setControlsEnabled(false);
    } else if (old) {
      old.remove();
    }
  }

  function setControlsEnabled(enabled) {
    for (const node of [prevButton, playButton, nextButton, resetButton, range, disassembleButton, assembleButton]) {
      if (node) node.disabled = !enabled;
    }
    root.dataset.ready = enabled ? 'true' : 'false';
  }

  function updatePlayButton() {
    playButton.textContent = state.playing ? '⏸' : '▶';
    const label = state.playing ? t('study.demo.pause') : t('study.demo.play');
    playButton.setAttribute('aria-label', label);
    playButton.setAttribute('title', label);
    root.dataset.playing = state.playing ? 'true' : 'false';
  }

  function refreshDirectionUi() {
    const view = state.view;
    const supports = Boolean(view && view.supportsDirection && typeof view.setDirection === 'function');
    directionGroup.hidden = !supports;
    if (!supports) {
      state.direction = null;
      root.dataset.direction = '';
      return;
    }
    const current = view.getDirection ? view.getDirection() : state.direction;
    state.direction = current === 'assemble' ? 'assemble' : 'disassemble';
    disassembleButton.classList.toggle('is-active', state.direction === 'disassemble');
    assembleButton.classList.toggle('is-active', state.direction === 'assemble');
    disassembleButton.setAttribute('aria-pressed', String(state.direction === 'disassemble'));
    assembleButton.setAttribute('aria-pressed', String(state.direction === 'assemble'));
    root.dataset.direction = state.direction;
  }

  function currentStepCount() {
    const view = state.view;
    if (!view || typeof view.getStepCount !== 'function') return 0;
    return Math.max(0, Number(view.getStepCount()) || 0);
  }

  function paintStepMeta() {
    const count = currentStepCount();
    const step = state.view && typeof state.view.getStep === 'function'
      ? (state.view.getStep(state.step) || {})
      : {};
    range.disabled = count < 2;
    range.max = String(Math.max(0, count - 1));
    range.value = String(clamp(state.step, 0, Math.max(0, count - 1)));
    progressText.textContent = count
      ? t('study.demo.stepProgress', { v1: state.step + 1, v2: count })
      : '';
    stepTitle.textContent = step.label
      ? String(step.label)
      : t('study.demo.stepFallback', { v1: state.step + 1 });
    stepDetail.textContent = step.detail ? String(step.detail) : (step.description ? String(step.description) : '');
    root.dataset.step = String(state.step);
    root.dataset.stepCount = String(count);
  }

  /** 只同步 UI 文案（不调用 view.setStep）；方向切换后由渲染器自己播放首步。 */
  function syncUi() {
    paintStepMeta();
    updatePlayButton();
    refreshDirectionUi();
    emitStepChange();
    emitFrame();
  }

  /**
   * 渲染指定步骤并同步 UI。
   * 等距/3D 视图带有短补间：帧回执（onFrameReady）在补间最后一帧渲染完成后才发出；
   * SVG 同步视图立即发出。另设 900ms 兜底，保证每次操作至少有一次回执。
   */
  function renderStep(index, { animate = true } = {}) {
    const count = currentStepCount();
    state.step = count ? clamp(Number(index) || 0, 0, count - 1) : 0;
    const view = state.view;
    let settled = false;
    const finishFrame = () => {
      if (settled || state.destroyed) return;
      settled = true;
      paintStepMeta();
      emitFrame();
    };
    if (view && typeof view.setStep === 'function') {
      try {
        view.setStep(state.step, { animate, onRendered: finishFrame });
      } catch (error) {
        // 单步渲染失败不应该让整个播放器崩溃：退回静态海报并提示。
        showRenderFailure(error);
        return;
      }
    }
    // 文案立即更新（键盘/阅读器马上有反馈），帧回执按渲染器是否异步决定时机。
    paintStepMeta();
    emitStepChange();
    if (view && view.deferredFrame) window.setTimeout(finishFrame, 900);
    else finishFrame();
  }

  function applyView(view, mode) {
    if (state.destroyed) {
      try { view.destroy(); } catch { /* ignore */ }
      return;
    }
    state.view = view;
    state.mode = mode;
    root.dataset.mode = mode;
    setLoading(false);
    setControlsEnabled(true);
    controls.hidden = false;
    refreshDirectionUi();
    renderStep(0, { animate: false });
    settleReady();
    if (options.autoplay) play();
  }

  function showRenderFailure(error) {
    state.view = null;
    state.mode = 'poster';
    root.dataset.mode = 'poster';
    setLoading(false);
    setControlsEnabled(false);
    controls.hidden = true;
    stage.replaceChildren(safePosterSvg(art));
    showNotice(t('study.demo.renderFailed', { v1: error?.message || t('study.demo.modeUnknown') }));
    syncUi();
    settleReady();
  }

  function mountSvg(mode) {
    try {
      const ariaLabel = diagramAriaLabel(titleText);
      // M4：数据里的 layout(flow/loop/timeline/state)、style(clean/sketch)、
      // nodes[].kind、edges[].kind、parts[].shape、labels[] 等字段整体透传给渲染器，
      // 播放器不再逐字段挑选，新增布局/形状无需改动本文件。
      const view = mode === 'flow'
        ? createFlowView(stage, art.data, { ariaLabel })
        : createIsoView(stage, art.data, { ariaLabel });
      applyView(view, 'svg');
    } catch (error) {
      showRenderFailure(error);
    }
  }

  function fallbackToIso(reason) {
    showLoading(false);
    showNotice(reason === 'webgl' ? t('study.demo.webglFallback') : t('study.demo.threeLoadFailed'));
    root.dataset.fallback = reason === 'webgl' ? 'webgl' : 'load';
    try {
      const view = createIsoView(stage, art.data, {
        staticMode: true,
        ariaLabel: diagramAriaLabel(titleText),
      });
      applyView(view, 'svg');
    } catch (error) {
      showRenderFailure(error);
    }
  }

  function showLoading(loading) {
    setLoading(loading);
  }

  function mountThree() {
    if (!isWebGLAvailable()) {
      fallbackToIso('webgl');
      return;
    }
    showLoading(true);
    createThreeView(stage, art.data, { ariaLabel: diagramAriaLabel(titleText) })
      .then((view) => {
        if (state.destroyed) {
          try { view.destroy(); } catch { /* ignore */ }
          return;
        }
        applyView(view, 'three');
      })
      .catch(() => {
        if (state.destroyed) return;
        fallbackToIso('load');
      });
  }

  /* ----------------------------------------------------------------- *
   * 播放控制
   * ----------------------------------------------------------------- */
  function clearTimer() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = 0;
    }
  }

  function pause() {
    clearTimer();
    if (!state.playing) return;
    state.playing = false;
    updatePlayButton();
  }

  function play() {
    if (state.destroyed || !state.view) return;
    const count = currentStepCount();
    if (count < 2) return;
    clearTimer();
    if (state.step >= count - 1) renderStep(0, { animate: false });
    state.playing = true;
    updatePlayButton();
    state.timer = setInterval(() => {
      const total = currentStepCount();
      if (!state.view || state.destroyed || total < 2) {
        pause();
        return;
      }
      if (state.step >= total - 1) {
        pause();
        return;
      }
      renderStep(state.step + 1, { animate: true });
    }, stepIntervalOf(art));
  }

  function seek(index) {
    pause();
    renderStep(index, { animate: false });
  }

  function step(direction) {
    pause();
    const count = currentStepCount();
    const delta = Number(direction) < 0 ? -1 : 1;
    renderStep(clamp(state.step + delta, 0, Math.max(0, count - 1)), { animate: true });
  }

  function togglePlay() {
    if (state.playing) pause();
    else play();
  }

  function changeDirection(next) {
    const view = state.view;
    if (!view || typeof view.setDirection !== 'function' || !view.supportsDirection) return;
    const direction = next === 'assemble' ? 'assemble' : 'disassemble';
    if (view.getDirection && view.getDirection() === direction) return;
    pause();
    const changed = view.setDirection(direction);
    if (!changed) return;
    state.step = 0;
    refreshDirectionUi();
    // 渲染器在 setDirection 内部已经把首步播放出来，这里只同步文案与回执。
    paintStepMeta();
    updatePlayButton();
    emitStepChange();
    emitFrame();
  }

  /* ----------------------------------------------------------------- *
   * 事件绑定
   * ----------------------------------------------------------------- */
  playButton.addEventListener('click', togglePlay);
  prevButton.addEventListener('click', () => step(-1));
  nextButton.addEventListener('click', () => step(1));
  resetButton.addEventListener('click', () => seek(0));
  range.addEventListener('input', () => seek(Number(range.value)));
  disassembleButton.addEventListener('click', () => changeDirection('disassemble'));
  assembleButton.addEventListener('click', () => changeDirection('assemble'));

  function onKeydown(event) {
    if (state.destroyed) return;
    if (options.keyboard === false) return;
    const target = event.target;
    const tag = target && target.tagName ? String(target.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.key === ' ' || event.key === 'Spacebar') {
      // 焦点在按钮上时保留浏览器默认的「空格=点击」，避免双击播放/暂停。
      if (tag === 'BUTTON' || tag === 'A') return;
      event.preventDefault();
      togglePlay();
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      step(-1);
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      step(1);
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      seek(0);
      return;
    }
    if (event.key === 'End') {
      event.preventDefault();
      seek(currentStepCount() - 1);
    }
  }
  root.addEventListener('keydown', onKeydown);

  function renderNotes() {
    const notes = (Array.isArray(art.data?.notes) ? art.data.notes : [])
      .map((note) => String(note || '').trim())
      .filter(Boolean);
    notesHost.hidden = notes.length === 0;
    notesHost.replaceChildren(...notes.map((note) => el('p', { class: 'pd-note', 'data-no-i18n': 'true' }, note)));
  }
  renderNotes();

  // 先声明 controller：SVG 视图在挂载时会同步触发 ready，回调里需要拿到它。
  const controller = {
    element: root,
    ready,
    play,
    pause,
    seek,
    step,
    getStep: () => state.step,
    getStepCount: currentStepCount,
    getKind: () => kind,
    getMode: () => state.mode,
    getDirection: () => state.direction,
    setDirection: (direction) => { changeDirection(direction); return state.direction; },
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      pause();
      if (state.view && typeof state.view.destroy === 'function') {
        try { state.view.destroy(); } catch { /* 释放失败不影响页面 */ }
      }
      state.view = null;
      root.removeEventListener('keydown', onKeydown);
      root.remove();
      container.replaceChildren();
      settleReady();
    },
  };

  // 挂载：3D 异步（含降级），SVG 同步。
  if (baseMode === 'three') mountThree();
  else if (baseMode === 'flow' || baseMode === 'iso') mountSvg(baseMode);
  else showRenderFailure(new Error(t('study.demo.modeUnknown')));

  if (options.keyboard !== false) {
    try {
      root.focus({ preventScroll: true });
    } catch {
      /* 极老浏览器不支持 focus 选项时忽略 */
    }
  }

  return controller;
}
