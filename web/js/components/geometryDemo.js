/* JSXGraph 交互几何（方案 §6.1）。
 *
 * 用法：
 *   const controller = createGeometryView(container, definition, {readOnly:true});
 *   await controller.ready;
 *   controller.getDefinition(); await controller.pngURI(); controller.destroy();
 *
 * 或 openGeometryDemo({links, ref, onSaved}) 打开可交互弹层并保存为
 * `geometry_demo` 学习附件。
 *
 * 纪律（与预设文件一致）：全部几何元素用 JS 函数/数值入参构建，不用字符串表达式；
 * 主站 CSP 无 `unsafe-eval`；演示确定性由纯函数保证，可按保存的参数完全重建。
 */
import { api } from '../core/api.js';
import { busy, button, el, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import {
  GEOMETRY_PRESETS,
  findGeometryPreset,
  normalizeGeometryParams,
} from '../../vendor/geometry/geometryPresets.js';

const SCRIPT_URL = '/static/vendor/jsxgraph/jsxgraphcore.js';
const CSS_URL = '/static/vendor/jsxgraph/jsxgraph.css';
const SCRIPT_MARK = 'ossJsxGraphScript';
const CSS_MARK = 'ossJsxGraphCss';
const LOAD_TIMEOUT_MS = 15000;
export const GEOMETRY_KIND = 'geometry_demo';
const BOARD_WIDTH = 640;
const BOARD_HEIGHT = 380;

let loaderPromise = null;
let boardSeq = 0;

function ossText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function isEnglishLocale() {
  return /^en/i.test(document.documentElement.lang || '') || /^en/i.test(String(navigator.language || ''));
}

/** 当前语言的预设名/参数名。 */
function presetLabel(preset) {
  return isEnglishLocale() ? preset.en : preset.zh;
}

function paramLabel(param) {
  return isEnglishLocale() ? param.en : param.zh;
}

/**
 * 规范化任意来源的 geometry_demo 定义（附件 data / 弹层入参）。
 * 预设不存在返回 null；参数逐项收敛到预设声明的范围。
 * @param {object|string} raw
 */
export function normalizeGeometryDefinition(raw) {
  let value = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const preset = findGeometryPreset(value.preset_id || value.presetId || value.id);
  if (!preset) return null;
  return {
    preset_id: preset.id,
    params: normalizeGeometryParams(preset, value.params),
  };
}

/** 首次调用时注入 JSXGraph（UMD 全局 JXG）与样式；失败可重试。 */
export function loadJsxGraph() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('study.oss.geometry.unavailable', '当前环境不支持几何演示')));
  }
  const existing = window.JXG;
  if (existing && existing.JSXGraph && typeof existing.JSXGraph.initBoard === 'function') {
    return Promise.resolve(existing);
  }
  if (loaderPromise) return loaderPromise;
  loaderPromise = new Promise((resolve, reject) => {
    if (!document.querySelector(`link[data-oss="${CSS_MARK}"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = CSS_URL;
      link.dataset.oss = CSS_MARK;
      document.head.append(link);
    }
    let script = document.querySelector(`script[data-oss="${SCRIPT_MARK}"]`);
    const finish = () => {
      const library = window.JXG;
      if (library && library.JSXGraph && typeof library.JSXGraph.initBoard === 'function') {
        // CSP 防御(审查 H1):本组件不使用 JessieCode 表达式;
        // 关掉编译开关后,任何字符串内容走解释器而非 eval,严格 CSP 下零违规。
        if (library.Options && library.Options.jc && 'compile' in library.Options.jc) {
          library.Options.jc.compile = false;
        }
        // 海报完整性(审查 H2):文本默认渲染为 SVG 外的 HTML 覆盖层,
        // 序列化海报会丢失;改为板内 SVG 文本,海报可保留全部标签与读数。
        if (library.Options && library.Options.text && 'display' in library.Options.text) {
          library.Options.text.display = 'internal';
        }
        resolve(library);
        return;
      }
      if (script) script.remove();
      reject(new Error(ossText('study.oss.geometry.unavailable', '几何演示加载失败')));
    };
    if (script && script.dataset.loaded === '1') {
      finish();
      return;
    }
    if (!script) {
      script = document.createElement('script');
      script.src = SCRIPT_URL;
      script.async = true;
      script.dataset.oss = SCRIPT_MARK;
      document.head.append(script);
    }
    const timer = setTimeout(() => {
      if (script) script.remove();
      reject(new Error(ossText('study.oss.geometry.unavailable', '几何演示加载超时')));
    }, LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      finish();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('study.oss.geometry.unavailable', '几何演示加载失败')));
    }, { once: true });
  }).catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

/** 板上滑杆区：预设参数对象 p 用 getter 读滑杆当前值，拖动后整板自动重算。 */
function buildParamSource(preset, params, editable, board) {
  const source = { ...params };
  if (editable && preset.params.length) {
    const [xmin, ymax, xmax, ymin] = board.getBoundingBox();
    const spanX = xmax - xmin;
    const spanY = ymax - ymin;
    let slot = 0;
    for (const param of preset.params) {
      const x1 = xmin + spanX * 0.04;
      const y = ymax - spanY * (0.09 + slot * 0.085);
      const slider = board.create('slider',
        [[x1, y], [x1 + spanX * 0.22, y], [param.min, params[param.name], param.max]],
        { name: `${paramLabel(param)}：`, snapWidth: param.step, strokeColor: '#5a7a96', fillColor: '#2f6fed' });
      Object.defineProperty(source, param.name, { get: () => slider.Value() });
      slot += 1;
    }
  }
  return source;
}

/** SVG → PNG data URI（同源内联 SVG，无外部引用，CSP img-src data: 允许）。
 * 源 SVG 无 viewBox：按其真实宽高等比缩放到目标宽,否则按新视口裁切会截掉右端。 */
function svgToPngURI(svg, targetWidth) {
  return new Promise(resolve => {
    try {
      const sourceWidth = parseFloat(svg.getAttribute('width')) || svg.clientWidth || 0;
      const sourceHeight = parseFloat(svg.getAttribute('height')) || 0;
      if (!sourceWidth || !sourceHeight) { resolve(''); return; }
      const width = targetWidth;
      const height = Math.round(sourceHeight * (targetWidth / sourceWidth));
      const clone = svg.cloneNode(true);
      clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      clone.setAttribute('viewBox', `0 0 ${sourceWidth} ${sourceHeight}`);
      clone.setAttribute('width', String(width));
      clone.setAttribute('height', String(height));
      const data = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(clone));
      const image = new Image();
      image.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          const context = canvas.getContext('2d');
          if (!context) { resolve(''); return; }
          context.fillStyle = '#ffffff';
          context.fillRect(0, 0, width, height);
          context.drawImage(image, 0, 0, width, height);
          resolve(canvas.toDataURL('image/png'));
        } catch {
          resolve('');
        }
      };
      image.onerror = () => resolve('');
      image.src = data;
    } catch {
      resolve('');
    }
  });
}

/**
 * 在容器内创建几何演示视图；返回 `{element, ready, getDefinition, pngURI, destroy}`。
 * 只读模式不挂滑杆，按保存的参数值静态重建。
 */
export function createGeometryView(container, rawDefinition, options = {}) {
  const definition = normalizeGeometryDefinition(rawDefinition);
  const host = el('div', { class: 'oss-geometry-view' });
  let destroyed = false;
  let library = null;
  let board = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  /** 中文降级提示；原始错误只进 console.warn，不进界面。 */
  function fail(message, detail) {
    if (detail) console.warn('[geometryDemo]', detail);
    host.replaceChildren(el('p', { class: 'help oss-geometry-unavailable', role: 'status' }, message));
    resolveReady({ ok: false, reason: 'failed' });
  }

  if (!definition) {
    host.append(el('p', { class: 'help oss-geometry-unavailable', role: 'status' },
      ossText('study.oss.geometry.invalid', '几何演示数据不合法：预设或参数无法识别。')));
    if (container) container.replaceChildren(host);
    resolveReady({ ok: false, reason: 'invalid' });
    return {
      element: host,
      ready,
      getDefinition: () => null,
      pngURI: async () => '',
      destroy: () => {
        host.remove();
        if (container) container.replaceChildren();
        resolveReady({ ok: false, reason: 'destroyed' });
      },
    };
  }

  if (container && typeof container.replaceChildren === 'function') container.replaceChildren(host);
  const boardDiv = el('div', { class: 'oss-geometry-board' });
  boardDiv.style.height = `${BOARD_HEIGHT}px`;
  host.append(boardDiv, el('p', { class: 'help oss-geometry-loading' },
    ossText('study.oss.geometry.loading', '正在加载几何演示…')));

  const preset = findGeometryPreset(definition.preset_id);
  const lang = isEnglishLocale() ? 'en' : 'zh';
  let paramSource = null;

  (async () => {
    // 阶段一:加载库。失败与预设/参数无关,报「加载失败」。
    try {
      library = await loadJsxGraph();
    } catch (error) {
      if (!destroyed) {
        fail(ossText('study.oss.geometry.unavailable', '几何演示不可用（JSXGraph 加载失败）。'), error);
      }
      return;
    }
    if (destroyed) return;
    // 阶段二:建板与预设构建。失败报「构建失败」,并释放半初始化的 board。
    try {
      const boardId = `oss-geometry-board-${++boardSeq}`;
      boardDiv.id = boardId;
      board = library.JSXGraph.initBoard(boardId, {
        boundingbox: preset.bounding,
        axis: true,
        showNavigation: false,
        keepaspectratio: true,
      });
      const editable = options.readOnly !== true;
      paramSource = buildParamSource(preset, definition.params, editable, board);
      preset.build(board, library, paramSource, lang);
      board.update();
      const loading = host.querySelector('.oss-geometry-loading');
      if (loading) loading.remove();
      resolveReady({ ok: true });
    } catch (error) {
      try { if (board) library.JSXGraph.freeBoard(board); } catch { /* 忽略 */ }
      board = null;
      if (!destroyed) {
        fail(ossText('study.oss.geometry.buildFailed', '几何演示构建失败：预设或参数无法渲染。'), error);
      }
    }
  })();

  return {
    element: host,
    ready,
    getDefinition: () => JSON.parse(JSON.stringify(definition)),
    /** 参数实值：滑杆模式下读当前拖动值（getter），只读模式回退保存值。 */
    getParams() {
      const values = {};
      const source = paramSource || definition.params;
      for (const param of preset.params) {
        const current = Number(source[param.name]);
        values[param.name] = Number.isFinite(current)
          ? Math.min(param.max, Math.max(param.min, current))
          : Number(definition.params[param.name]);
      }
      return values;
    },
    async pngURI() {
      const svg = host.querySelector('svg');
      if (!svg) return '';
      const data = await svgToPngURI(svg, BOARD_WIDTH);
      return data || '';
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      try { if (board && library) library.JSXGraph.freeBoard(board); } catch { /* 忽略 */ }
      host.remove();
      if (container && typeof container.replaceChildren === 'function') container.replaceChildren();
      resolveReady({ ok: false, reason: 'destroyed' });
    },
    get library() { return library; },
    get board() { return board; },
    get preset() { return preset; },
  };
}

function buildPreview(presetId, params) {
  // pngURI 是异步的；保存流程里先取到再组装 preview。
  return { kind: 'geometry-demo', preset_id: presetId, params };
}

/**
 * 打开几何演示弹层。
 * @param {{initialPreset?: string, links?: Array<object>, ref?: string, titleHint?: string,
 *          onSaved?: Function, trigger?: HTMLElement, readOnly?: boolean}} options
 */
export function openGeometryDemo(options = {}) {
  const readOnly = options.readOnly === true;
  const dialog = el('dialog', {
    class: 'oss-dialog oss-geometry-dialog',
    'data-study-tool': 'geometry-demo',
    'aria-label': ossText('study.oss.geometry.title', '几何演示'),
  });
  const titleNode = el('h2', { class: 'oss-dialog-title' }, ossText('study.oss.geometry.title', '几何演示'));
  const presetSelect = el('select', { class: 'input', 'aria-label': ossText('study.oss.geometry.preset', '演示主题') },
    ...GEOMETRY_PRESETS.map(preset => el('option', { value: preset.id }, presetLabel(preset))));
  const canvasHost = el('div', { class: 'oss-geometry-canvas' });
  let controller = null;

  const presetId = GEOMETRY_PRESETS.some(item => item.id === options.initialPreset)
    ? options.initialPreset : GEOMETRY_PRESETS[0].id;
  presetSelect.value = presetId;

  function definitionOf(id) {
    const preset = findGeometryPreset(id) || GEOMETRY_PRESETS[0];
    const params = {};
    for (const param of preset.params) params[param.name] = param.value;
    return { preset_id: preset.id, params };
  }

  function rebuild() {
    if (controller) {
      try { controller.destroy(); } catch { /* 忽略 */ }
      controller = null;
    }
    saveButton.disabled = true;
    const current = createGeometryView(canvasHost, definitionOf(presetSelect.value), { readOnly });
    controller = current;
    current.ready.then(outcome => {
      // 被重置/关闭取代：不动按钮；失败时保持禁用（降级文案已在 host 内）。
      if (controller !== current) return;
      saveButton.disabled = !outcome || outcome.ok === false;
    }).catch(() => {});
  }

  presetSelect.addEventListener('change', rebuild);

  const resetButton = button(ossText('study.oss.geometry.reset', '重置'), () => rebuild(), 'secondary');
  const closeButton = button(ossText('study.oss.geometry.close', '关闭'), () => dialog.close(), 'secondary');
  const saveButton = button(ossText('study.oss.geometry.save', '保存为学习附件'), event => busy(event.currentTarget, () => save()), '', 'download');

  async function save() {
    // 失败/非法视图不允许保存（getDefinition 为 null 时拒绝,防住无海报默认参数的空附件）。
    if (!controller || !controller.getDefinition()) {
      toast(ossText('study.oss.geometry.invalid', '几何演示数据不合法'), true);
      return;
    }
    const preset = findGeometryPreset(presetSelect.value) || GEOMETRY_PRESETS[0];
    const definition = normalizeGeometryDefinition({
      preset_id: preset.id,
      params: controller.getParams(),
    });
    if (!definition) {
      toast(ossText('study.oss.geometry.invalid', '几何演示数据不合法'), true);
      return;
    }
    const png = await controller.pngURI();
    const links = Array.isArray(options.links) ? options.links : [];
    const ref = String(options.ref || '').trim();
    const body = {
      kind: GEOMETRY_KIND,
      title: (String(options.titleHint || '').trim()
        || `${ossText('study.oss.geometry.title', '几何演示')} · ${presetLabel(preset)}`).slice(0, 120),
      data: { preset_id: definition.preset_id, params: definition.params },
      preview: {
        ...buildPreview(definition.preset_id, definition.params),
        ...(png && png.length < 60000 && png.startsWith('data:image/png;base64,') ? { png } : {}),
      },
      links,
    };
    if (ref) { body.source_kind = 'question'; body.source_ref = ref; }
    try {
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      toast(ossText('study.oss.geometry.saved', '已保存为学习附件：{id}', { id: artifact.artifact_id || '' }));
      if (typeof options.onSaved === 'function') {
        try { options.onSaved({ artifact, created }); } catch { /* 忽略回调失败 */ }
      }
      dialog.close();
    } catch (error) {
      toast(error?.message || ossText('study.oss.geometry.saveFailed', '保存几何演示失败'), true);
    }
  }

  dialog.append(
    titleNode,
    el('div', { class: 'oss-geometry-toolbar' },
      el('label', { class: 'oss-geometry-field' },
        el('span', {}, ossText('study.oss.geometry.preset', '演示主题')), presetSelect)),
    canvasHost,
    el('div', { class: 'oss-geometry-controls' }, resetButton),
    el('div', { class: 'oss-formula-toolbar' }, closeButton, saveButton),
  );
  if (readOnly) {
    presetSelect.disabled = true;
    saveButton.hidden = true;
  }
  dialog.addEventListener('close', () => {
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
    dialog.remove();
    if (options.trigger && typeof options.trigger.focus === 'function') {
      try { options.trigger.focus(); } catch { /* 忽略 */ }
    }
  });
  document.body.append(dialog);
  dialog.showModal();
  rebuild();
  return dialog;
}
