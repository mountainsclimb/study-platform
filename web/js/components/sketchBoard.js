/**
 * Excalidraw 兼容解题白板（原生 canvas，无构建、无第三方依赖、无 WebGL）。
 *
 * 方案 §5.4「Mafs 与 Excalidraw」：白板保存元素、必要状态、预览 PNG 与版本；题目、错题、
 * 笔记与课堂共用同一份版本化学习附件（kind=`whiteboard`）。本组件是主站的 Excalidraw
 * 兼容替代实现：场景结构与 Excalidraw 同构（`elements[]` + `appState.viewBackgroundColor`），
 * 支持 freedraw / line / arrow / rect / ellipse / text / formula 七种元素。
 *
 * 安全与限制：
 * - 不拼 innerHTML 渲染外部数据：公式先经本地 `renderMath`（KaTeX）生成，再用
 *   `sanitizeMath`（DOMPurify MathML 白名单）净化后写入临时 template，公式源文本只作
 *   纯文本兜底；文字/标注一律 textContent。
 * - 元素上限 300、撤销栈 50、单次 data 序列化 ≤256KiB（超出提示「草稿过大，请精简后保存」）；
 *   预览 PNG 自适应缩放到 ≤64KiB，仍超出则省略 preview 但保存照常成功。
 * - 无 pointer events 的老浏览器退化为鼠标事件（现代浏览器均走 pointer）。
 *
 * 对外 API：
 *   createSketchBoard(options) -> { element, getScene(), setScene(scene), exportPNG(),
 *                                   buildPreviewPng(), destroy(), isReadOnly() }
 *     options = { scene?, readOnly?, ariaLabel?, onChange? }
 *   openSketchBoard({ ref, artifact, readOnly, onSaved, trigger, titleHint }) -> controller
 *   openSketchBoardForTarget({ ref, trigger, titleHint?, fallbackItems?, readOnly?, onSaved? })
 *     -> Promise<controller | null>：先探测 by-target 已保存白板，失败用 fallbackItems 兜底。
 */

import { busy, button, el, icon, query, toast } from '../core/dom.js';
import { api } from '../core/api.js';
import { t } from '../core/i18n.js';
import { renderMath, sanitizeMath } from '../core/markdown.js';

/* ------------------------------------------------------------------ i18n */

function toolText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

/* ---------------------------------------------------------------- 常量 */

const SKETCH_KIND = 'whiteboard';
const ARTIFACT_TARGET_KIND = 'question';
const PROBE_TIMEOUT_MS = 4000;

const DESIGN_WIDTH = 960;
const DESIGN_HEIGHT = 600;
const MAX_ELEMENTS = 300;
const MAX_HISTORY = 50;
const MAX_DATA_BYTES = 256 * 1024;
//: preview 序列化上限 64KiB；dataURL 是 ASCII，长度≈字节数，留一点 JSON 余量。
const MAX_PREVIEW_CHARS = 62000;
const DEFAULT_BACKGROUND = '#ffffff';
const DEFAULT_STROKE = '#1e1e1e';
const DEFAULT_FONT_SIZE = 20;
const MAX_TEXT_LENGTH = 2000;
const MAX_POINTS = 4000;

const ELEMENT_TYPES = new Set(['freedraw', 'line', 'arrow', 'rect', 'ellipse', 'text', 'formula']);

const COLOR_CHOICES = Object.freeze([
  { key: 'black', value: '#1e1e1e' },
  { key: 'red', value: '#e03131' },
  { key: 'blue', value: '#1971c2' },
  { key: 'green', value: '#2f9e44' },
]);

const WIDTH_CHOICES = Object.freeze([1, 2, 4]);

const TOOL_DEFINITIONS = Object.freeze([
  { id: 'select', labelKey: 'study.tools.board.tool.select', fallback: '选择' },
  { id: 'pen', labelKey: 'study.tools.board.tool.pen', fallback: '画笔' },
  { id: 'line', labelKey: 'study.tools.board.tool.line', fallback: '直线' },
  { id: 'arrow', labelKey: 'study.tools.board.tool.arrow', fallback: '箭头' },
  { id: 'rect', labelKey: 'study.tools.board.tool.rect', fallback: '矩形' },
  { id: 'ellipse', labelKey: 'study.tools.board.tool.ellipse', fallback: '椭圆' },
  { id: 'text', labelKey: 'study.tools.board.tool.text', fallback: '文字' },
  { id: 'formula', labelKey: 'study.tools.board.tool.formula', fallback: '公式' },
]);

const COLOR_LABEL_KEYS = Object.freeze({
  black: ['study.tools.board.color.black', '黑色'],
  red: ['study.tools.board.color.red', '红色'],
  blue: ['study.tools.board.color.blue', '蓝色'],
  green: ['study.tools.board.color.green', '绿色'],
});

/* ------------------------------------------------------- 场景数据模型 */

/** 新建空白白板场景（与 Excalidraw 兼容结构一致）。 */
export function defaultSketchScene() {
  return {
    version: 1,
    source: 'excalidraw-compatible',
    appState: { viewBackgroundColor: DEFAULT_BACKGROUND },
    elements: [],
  };
}

function finiteNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function sanitizeColor(value, fallback) {
  const text = String(value || '').trim();
  return /^#[0-9a-fA-F]{3,8}$/.test(text) ? text : fallback;
}

function clampNumber(value, min, max, fallback) {
  const number = finiteNumber(value, fallback);
  return Math.min(max, Math.max(min, number));
}

let elementSequence = 0;

function nextElementId(seen) {
  for (;;) {
    elementSequence += 1;
    const id = `e${elementSequence.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    if (!seen || !seen.has(id)) return id;
  }
}

function sanitizePoints(value) {
  if (!Array.isArray(value)) return [];
  const points = [];
  for (const item of value) {
    if (points.length >= MAX_POINTS) break;
    if (!Array.isArray(item) || item.length < 2) continue;
    const x = Number(item[0]);
    const y = Number(item[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    points.push([Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000]);
  }
  return points;
}

function normalizeElement(raw, seen) {
  if (!raw || typeof raw !== 'object') return null;
  const type = String(raw.type || '').trim();
  if (!ELEMENT_TYPES.has(type)) return null;
  let id = String(raw.id || '').trim().slice(0, 80);
  if (!id || seen.has(id)) id = nextElementId(seen);
  seen.add(id);
  const element = {
    id,
    type,
    x: finiteNumber(raw.x, 0),
    y: finiteNumber(raw.y, 0),
    strokeColor: sanitizeColor(raw.strokeColor, DEFAULT_STROKE),
    strokeWidth: clampNumber(raw.strokeWidth, 0.5, 32, 2),
    opacity: clampNumber(raw.opacity, 0, 100, 100),
  };
  if (type === 'freedraw' || type === 'line' || type === 'arrow') {
    let points = sanitizePoints(raw.points);
    if (!points.length && type !== 'freedraw') {
      const width = finiteNumber(raw.width, 0);
      const height = finiteNumber(raw.height, 0);
      if (width || height) points = [[0, 0], [width, height]];
    }
    if (!points.length) return null;
    if (type === 'freedraw') points[0] = [0, 0];
    element.points = points;
  } else if (type === 'rect' || type === 'ellipse') {
    const rawWidth = finiteNumber(raw.width, 0);
    const rawHeight = finiteNumber(raw.height, 0);
    const left = Math.min(element.x, element.x + rawWidth);
    const top = Math.min(element.y, element.y + rawHeight);
    element.x = left;
    element.y = top;
    element.width = Math.abs(rawWidth);
    element.height = Math.abs(rawHeight);
    element.backgroundColor = sanitizeColor(raw.backgroundColor, 'transparent');
  } else if (type === 'text') {
    element.text = String(raw.text ?? '').slice(0, MAX_TEXT_LENGTH);
    element.fontSize = clampNumber(raw.fontSize, 8, 120, DEFAULT_FONT_SIZE);
  } else if (type === 'formula') {
    element.latex = String(raw.latex ?? '').slice(0, MAX_TEXT_LENGTH);
    element.fontSize = clampNumber(raw.fontSize, 8, 120, DEFAULT_FONT_SIZE);
  }
  return element;
}

/**
 * 规范化白板场景：非法元素丢弃、坐标裁剪、元素数 ≤300；结构不合法返回 null。
 * @param {object|string} raw
 */
export function normalizeSketchScene(raw) {
  let value = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const seen = new Set();
  const elements = [];
  for (const item of Array.isArray(value.elements) ? value.elements : []) {
    if (elements.length >= MAX_ELEMENTS) break;
    const element = normalizeElement(item, seen);
    if (element) elements.push(element);
  }
  const background = value.appState && typeof value.appState === 'object'
    ? value.appState.viewBackgroundColor
    : undefined;
  return {
    version: 1,
    source: 'excalidraw-compatible',
    appState: { viewBackgroundColor: sanitizeColor(background, DEFAULT_BACKGROUND) },
    elements,
  };
}

/** 场景序列化后的字节数（UTF-8）；用于保存前的 256KiB 上限自检。 */
export function sketchSceneBytes(scene) {
  let text;
  try {
    text = JSON.stringify(scene);
  } catch {
    return Infinity;
  }
  if (typeof TextEncoder === 'function') return new TextEncoder().encode(text).length;
  return unescape(encodeURIComponent(text)).length;
}

function cloneScene(scene) {
  return JSON.parse(JSON.stringify({
    version: 1,
    source: 'excalidraw-compatible',
    appState: { viewBackgroundColor: scene.appState.viewBackgroundColor },
    elements: scene.elements,
  }));
}

function roundRectValue(value) {
  return Math.round(value * 1000) / 1000;
}

/* ------------------------------------------------------- 公式纯文本（导出用） */

const formulaTextCache = new Map();

/** 公式的线性可读文本（KaTeX 渲染后的 textContent），仅用于 canvas 导出/测量。 */
function formulaPlainText(latex) {
  const key = String(latex || '');
  if (formulaTextCache.has(key)) return formulaTextCache.get(key);
  let text = key;
  try {
    const html = renderMath(key, false);
    const safe = sanitizeMath(html);
    const template = document.createElement('template');
    template.innerHTML = safe;
    const plain = String(template.content.textContent || '').replace(/\s+/g, '').trim();
    if (plain) text = plain;
  } catch {
    text = key;
  }
  if (formulaTextCache.size > 200) formulaTextCache.clear();
  formulaTextCache.set(key, text);
  return text;
}

/* ------------------------------------------------------------- 画布渲染 */

function applyCanvasStyle(context, element) {
  context.strokeStyle = element.strokeColor;
  context.fillStyle = element.strokeColor;
  context.lineWidth = Math.max(0.5, Number(element.strokeWidth) || 1);
  context.globalAlpha = Math.min(1, Math.max(0, Number(element.opacity) || 0) / 100);
  context.lineCap = 'round';
  context.lineJoin = 'round';
}

function elementBoundingBox(element, context) {
  if (!element) return null;
  if (element.type === 'freedraw' || element.type === 'line' || element.type === 'arrow') {
    const points = element.points || [];
    if (!points.length) return { x: element.x, y: element.y, width: 1, height: 1 };
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const point of points) {
      const x = element.x + point[0];
      const y = element.y + point[1];
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
    return { x: minX, y: minY, width: Math.max(1, maxX - minX), height: Math.max(1, maxY - minY) };
  }
  if (element.type === 'rect' || element.type === 'ellipse') {
    return {
      x: element.x,
      y: element.y,
      width: Math.max(1, element.width || 0),
      height: Math.max(1, element.height || 0),
    };
  }
  if (element.type === 'text') {
    const fontSize = Number(element.fontSize) || DEFAULT_FONT_SIZE;
    const lines = String(element.text || '').split('\n');
    let width = 1;
    if (context) {
      context.save();
      context.font = `${fontSize}px "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`;
      for (const line of lines) width = Math.max(width, context.measureText(line).width);
      context.restore();
    } else {
      width = Math.max(1, String(element.text || '').length * fontSize * 0.6);
    }
    return { x: element.x, y: element.y, width, height: fontSize * 1.4 * Math.max(1, lines.length) };
  }
  if (element.type === 'formula') {
    const fontSize = Number(element.fontSize) || DEFAULT_FONT_SIZE;
    const plain = formulaPlainText(element.latex);
    let width = Math.max(1, plain.length * fontSize * 0.58);
    if (context) {
      context.save();
      context.font = `${fontSize}px "Noto Sans SC", system-ui, sans-serif`;
      width = Math.max(width, context.measureText(plain).width);
      context.restore();
    }
    return { x: element.x, y: element.y, width, height: fontSize * 1.7 };
  }
  return null;
}

function drawCanvasElement(context, element, { formulasAsText = false } = {}) {
  if (!element) return;
  context.save();
  applyCanvasStyle(context, element);
  switch (element.type) {
    case 'freedraw': {
      const points = element.points || [];
      if (points.length === 1) {
        context.beginPath();
        context.arc(element.x + points[0][0], element.y + points[0][1], Math.max(1, element.strokeWidth / 2), 0, Math.PI * 2);
        context.fill();
        break;
      }
      context.beginPath();
      context.moveTo(element.x + points[0][0], element.y + points[0][1]);
      for (let index = 1; index < points.length; index += 1) {
        context.lineTo(element.x + points[index][0], element.y + points[index][1]);
      }
      context.stroke();
      break;
    }
    case 'line':
    case 'arrow': {
      const points = element.points || [];
      const startX = element.x + (points[0] ? points[0][0] : 0);
      const startY = element.y + (points[0] ? points[0][1] : 0);
      const end = points[points.length - 1] || [0, 0];
      const endX = element.x + end[0];
      const endY = element.y + end[1];
      context.beginPath();
      context.moveTo(startX, startY);
      context.lineTo(endX, endY);
      context.stroke();
      if (element.type === 'arrow') {
        const angle = Math.atan2(endY - startY, endX - startX);
        const size = Math.max(8, element.strokeWidth * 4);
        context.beginPath();
        context.moveTo(endX, endY);
        context.lineTo(endX - size * Math.cos(angle - Math.PI / 7), endY - size * Math.sin(angle - Math.PI / 7));
        context.lineTo(endX - size * Math.cos(angle + Math.PI / 7), endY - size * Math.sin(angle + Math.PI / 7));
        context.closePath();
        context.fill();
      }
      break;
    }
    case 'rect': {
      if (element.backgroundColor && element.backgroundColor !== 'transparent') {
        context.fillStyle = element.backgroundColor;
        context.fillRect(element.x, element.y, element.width, element.height);
        context.fillStyle = element.strokeColor;
      }
      context.strokeRect(element.x, element.y, element.width, element.height);
      break;
    }
    case 'ellipse': {
      const radiusX = Math.max(0.5, element.width / 2);
      const radiusY = Math.max(0.5, element.height / 2);
      context.beginPath();
      context.ellipse(element.x + radiusX, element.y + radiusY, radiusX, radiusY, 0, 0, Math.PI * 2);
      if (element.backgroundColor && element.backgroundColor !== 'transparent') {
        context.fillStyle = element.backgroundColor;
        context.fill();
        context.fillStyle = element.strokeColor;
      }
      context.stroke();
      break;
    }
    case 'text': {
      const fontSize = Number(element.fontSize) || DEFAULT_FONT_SIZE;
      context.font = `${fontSize}px "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`;
      context.textBaseline = 'top';
      context.fillStyle = element.strokeColor;
      const lines = String(element.text || '').split('\n');
      lines.forEach((line, index) => {
        context.fillText(line, element.x, element.y + index * fontSize * 1.4);
      });
      break;
    }
    case 'formula': {
      // 主画布用 HTML 覆盖层显示 KaTeX；canvas 只在导出时用线性文本近似（导出为图像+文字）。
      if (!formulasAsText) break;
      const fontSize = Number(element.fontSize) || DEFAULT_FONT_SIZE;
      context.font = `${fontSize}px "Noto Sans SC", system-ui, sans-serif`;
      context.textBaseline = 'top';
      context.fillStyle = element.strokeColor;
      context.fillText(formulaPlainText(element.latex), element.x, element.y);
      break;
    }
    default:
      break;
  }
  context.restore();
}

/* ------------------------------------------------------------- 白板组件 */

/**
 * 创建解题白板。
 * @param {{scene?: object|string, readOnly?: boolean, ariaLabel?: string, onChange?: Function}} options
 * @returns {{element: HTMLElement, getScene: Function, setScene: Function,
 *            exportPNG: Function, buildPreviewPng: Function, destroy: Function, isReadOnly: Function}}
 */
export function createSketchBoard(options = {}) {
  const readOnly = options.readOnly === true;
  let scene = normalizeSketchScene(options.scene) || defaultSketchScene();
  let destroyed = false;
  let pendingFrame = 0;
  let history = [];
  let historyIndex = -1;
  let selectedId = null;
  let draft = null;
  let activePointerId = null;
  let tool = readOnly ? 'select' : 'pen';
  let color = DEFAULT_STROKE;
  let strokeWidth = 2;
  let undoButton = null;
  let redoButton = null;

  const toolButtons = new Map();
  const colorButtons = new Map();
  const widthButtons = new Map();
  const formulaNodes = new Map();

  const root = el('div', {
    class: `st-board${readOnly ? ' is-read-only' : ''}`,
    'data-study-tool': 'whiteboard',
    'data-read-only': readOnly ? 'true' : 'false',
    role: 'application',
    'aria-label': options.ariaLabel || toolText('study.tools.board.aria', '解题白板：可用鼠标或触屏书写解题思路'),
  });
  const toolbar = el('div', {
    class: 'st-board-toolbar',
    role: 'toolbar',
    'aria-label': toolText('study.tools.board.toolbar', '白板工具'),
  });
  const stage = el('div', { class: 'st-board-stage' });
  stage.style.position = 'relative';
  const canvas = el('canvas', { class: 'st-board-canvas' });
  canvas.style.display = 'block';
  canvas.style.width = '100%';
  canvas.style.height = 'auto';
  canvas.style.touchAction = 'none';
  const overlay = el('div', { class: 'st-board-overlay', 'aria-hidden': 'true' });
  overlay.style.position = 'absolute';
  overlay.style.inset = '0';
  overlay.style.overflow = 'hidden';
  overlay.style.pointerEvents = 'none';
  stage.append(canvas, overlay);
  const hintNode = el('p', { class: 'st-board-hint' }, toolText(
    'study.tools.board.hint',
    '支持鼠标与触屏；文字/公式创建后可双击编辑；导出 PNG 用于粘贴到笔记或作业。',
  ));
  root.append(toolbar, stage, hintNode);

  if (readOnly) {
    toolbar.hidden = true;
  } else {
    buildToolbar();
  }

  const context = canvas.getContext ? canvas.getContext('2d') : null;

  /* -------- 工具条 -------- */

  function buildToolbar() {
    const toolGroup = el('div', { class: 'st-board-group', role: 'group', 'aria-label': toolText('study.tools.board.toolGroup', '绘图工具') });
    for (const definition of TOOL_DEFINITIONS) {
      const label = toolText(definition.labelKey, definition.fallback);
      const node = el('button', {
        type: 'button',
        class: 'st-tool',
        'data-tool': definition.id,
        'aria-pressed': 'false',
        title: label,
        onClick: () => setTool(definition.id),
      }, label);
      toolButtons.set(definition.id, node);
      toolGroup.append(node);
    }
    const colorGroup = el('div', { class: 'st-board-group', role: 'group', 'aria-label': toolText('study.tools.board.colorGroup', '颜色') });
    for (const choice of COLOR_CHOICES) {
      const labels = COLOR_LABEL_KEYS[choice.key] || ['study.tools.board.color.black', '黑色'];
      const node = el('button', {
        type: 'button',
        class: 'st-swatch',
        'data-color': choice.value,
        'aria-pressed': 'false',
        'aria-label': toolText(labels[0], labels[1]),
        title: toolText(labels[0], labels[1]),
        onClick: () => setColor(choice.value),
      });
      node.style.background = choice.value;
      colorButtons.set(choice.value, node);
      colorGroup.append(node);
    }
    const widthGroup = el('div', { class: 'st-board-group', role: 'group', 'aria-label': toolText('study.tools.board.widthGroup', '线宽') });
    for (const width of WIDTH_CHOICES) {
      const label = toolText('study.tools.board.widthOption', '线宽 {v1}', { v1: width });
      const node = el('button', {
        type: 'button',
        class: 'st-width',
        'data-width': String(width),
        'aria-pressed': 'false',
        'aria-label': label,
        title: label,
        onClick: () => setStrokeWidth(width),
      }, String(width));
      widthButtons.set(width, node);
      widthGroup.append(node);
    }
    const actionGroup = el('div', { class: 'st-board-group st-board-actions', role: 'group', 'aria-label': toolText('study.tools.board.actionGroup', '编辑操作') });
    undoButton = el('button', {
      type: 'button', class: 'st-action', onClick: undo,
      'aria-label': toolText('study.tools.board.undo', '撤销'),
      title: toolText('study.tools.board.undo', '撤销'),
    }, icon('history'), toolText('study.tools.board.undo', '撤销'));
    redoButton = el('button', {
      type: 'button', class: 'st-action', onClick: redo,
      'aria-label': toolText('study.tools.board.redo', '重做'),
      title: toolText('study.tools.board.redo', '重做'),
    }, icon('repeat'), toolText('study.tools.board.redo', '重做'));
    const clearButton = el('button', {
      type: 'button', class: 'st-action', onClick: clearAll,
      'aria-label': toolText('study.tools.board.clear', '清空'),
      title: toolText('study.tools.board.clear', '清空'),
    }, icon('trash'), toolText('study.tools.board.clear', '清空'));
    const deleteButton = el('button', {
      type: 'button', class: 'st-action', onClick: deleteSelected,
      'aria-label': toolText('study.tools.board.delete', '删除选中'),
      title: toolText('study.tools.board.delete', '删除选中'),
    }, icon('close'), toolText('study.tools.board.delete', '删除选中'));
    actionGroup.append(undoButton, redoButton, clearButton, deleteButton);
    toolbar.append(toolGroup, colorGroup, widthGroup, actionGroup);
    root.setAttribute('data-tool', tool);
    syncToolState();
  }

  function syncToolState() {
    for (const [id, node] of toolButtons) node.setAttribute('aria-pressed', String(id === tool));
    for (const [value, node] of colorButtons) node.setAttribute('aria-pressed', String(value === color));
    for (const [width, node] of widthButtons) node.setAttribute('aria-pressed', String(Number(width) === strokeWidth));
    toolbar.querySelectorAll('.st-tool').forEach((node) => {
      node.classList.toggle('is-active', node.getAttribute('data-tool') === tool);
    });
    toolbar.querySelectorAll('.st-swatch').forEach((node) => {
      node.classList.toggle('is-active', node.getAttribute('data-color') === color);
    });
    toolbar.querySelectorAll('.st-width').forEach((node) => {
      node.classList.toggle('is-active', Number(node.getAttribute('data-width')) === strokeWidth);
    });
    root.setAttribute('data-tool', tool);
    canvas.style.cursor = tool === 'select' ? 'default' : 'crosshair';
  }

  function setTool(next) {
    if (readOnly) return;
    const valid = next === 'select' || TOOL_DEFINITIONS.some(definition => definition.id === next);
    tool = valid ? next : 'pen';
    syncToolState();
  }

  function setColor(next) {
    if (readOnly) return;
    const value = sanitizeColor(next, DEFAULT_STROKE);
    color = value;
    // 选中元素时改色：与画图工具一致，对选中元素即时生效。
    if (selectedId) {
      const element = scene.elements.find(item => item.id === selectedId);
      if (element) {
        element.strokeColor = value;
        pushHistory();
        schedulePaint();
      }
    }
    syncToolState();
  }

  function setStrokeWidth(next) {
    if (readOnly) return;
    const value = Number(next);
    strokeWidth = WIDTH_CHOICES.includes(value) ? value : 2;
    if (selectedId) {
      const element = scene.elements.find(item => item.id === selectedId);
      if (element) {
        element.strokeWidth = strokeWidth;
        pushHistory();
        schedulePaint();
      }
    }
    syncToolState();
  }

  function updateActionState() {
    if (readOnly) return;
    if (undoButton) undoButton.disabled = historyIndex <= 0;
    if (redoButton) redoButton.disabled = historyIndex >= history.length - 1;
  }

  /* -------- 历史（撤销/重做，栈 ≤50） -------- */

  function snapshotElements() {
    try {
      return JSON.stringify(scene.elements);
    } catch {
      return '[]';
    }
  }

  function pushHistory() {
    const snapshot = snapshotElements();
    if (historyIndex >= 0 && history[historyIndex] === snapshot) return;
    history = history.slice(0, historyIndex + 1);
    history.push(snapshot);
    if (history.length > MAX_HISTORY) history.shift();
    historyIndex = history.length - 1;
    updateActionState();
  }

  function applyHistory() {
    try {
      scene.elements = JSON.parse(history[historyIndex] || '[]');
    } catch {
      scene.elements = [];
    }
    selectedId = null;
    updateActionState();
    paint();
  }

  function undo() {
    if (readOnly || historyIndex <= 0) return;
    historyIndex -= 1;
    applyHistory();
  }

  function redo() {
    if (readOnly || historyIndex >= history.length - 1) return;
    historyIndex += 1;
    applyHistory();
  }

  function commitElement(element) {
    if (!element) return;
    if (scene.elements.length >= MAX_ELEMENTS) {
      toast(toolText('study.tools.board.limit', '元素已达上限（{v1} 个），请先删除部分内容', { v1: MAX_ELEMENTS }), true);
      return;
    }
    scene.elements.push(element);
    selectedId = element.id;
    pushHistory();
    paint();
  }

  function deleteSelected() {
    if (readOnly) return;
    if (!selectedId) {
      toast(toolText('study.tools.board.deleteNone', '请先选择一个元素再删除'), true);
      return;
    }
    scene.elements = scene.elements.filter(element => element.id !== selectedId);
    selectedId = null;
    pushHistory();
    paint();
  }

  function clearAll() {
    if (readOnly || !scene.elements.length) return;
    if (typeof window.confirm === 'function'
      && !window.confirm(toolText('study.tools.board.clearConfirm', '清空整块白板？可用撤销恢复。'))) return;
    scene.elements = [];
    selectedId = null;
    formulaNodes.forEach(entry => entry.node.remove());
    formulaNodes.clear();
    pushHistory();
    paint();
  }

  /* -------- 指针交互（鼠标/触屏统一 pointer events） -------- */

  function pointFromEvent(event) {
    const rect = canvas.getBoundingClientRect();
    const width = rect.width || DESIGN_WIDTH;
    const height = rect.height || DESIGN_HEIGHT;
    return {
      x: ((event.clientX - rect.left) / width) * DESIGN_WIDTH,
      y: ((event.clientY - rect.top) / height) * DESIGN_HEIGHT,
    };
  }

  function hitTest(point) {
    for (let index = scene.elements.length - 1; index >= 0; index -= 1) {
      const element = scene.elements[index];
      const box = elementBoundingBox(element, context);
      if (!box) continue;
      const pad = Math.max(6, (Number(element.strokeWidth) || 1) * 2);
      if (point.x >= box.x - pad && point.x <= box.x + box.width + pad
        && point.y >= box.y - pad && point.y <= box.y + box.height + pad) return element;
    }
    return null;
  }

  function onPointerDown(event) {
    if (readOnly) return;
    if (typeof event.button === 'number' && event.button !== 0) return;
    if (event.target !== canvas) return;
    if (typeof event.preventDefault === 'function') event.preventDefault();
    const point = pointFromEvent(event);
    if (tool === 'select') {
      const hit = hitTest(point);
      selectedId = hit ? hit.id : null;
      paint();
      return;
    }
    if (tool === 'text' || tool === 'formula') {
      createTextOrFormula(point);
      return;
    }
    activePointerId = event.pointerId === undefined ? null : event.pointerId;
    if (activePointerId !== null && typeof canvas.setPointerCapture === 'function') {
      try { canvas.setPointerCapture(activePointerId); } catch { /* 触屏被系统接管时忽略 */ }
    }
    draft = {
      tool,
      start: point,
      current: point,
      points: [[0, 0]],
    };
    schedulePaint();
  }

  function onPointerMove(event) {
    if (readOnly || !draft) return;
    if (activePointerId !== null && event.pointerId !== activePointerId) return;
    const point = pointFromEvent(event);
    draft.current = point;
    if (draft.tool === 'pen') {
      const last = draft.points[draft.points.length - 1];
      const nextX = point.x - draft.start.x;
      const nextY = point.y - draft.start.y;
      if (!last || Math.hypot(nextX - last[0], nextY - last[1]) >= 1.6) {
        if (draft.points.length < MAX_POINTS) draft.points.push([nextX, nextY]);
      }
    }
    schedulePaint();
  }

  function finishPointer(event) {
    if (readOnly) return;
    if (activePointerId !== null && event && event.pointerId !== undefined && event.pointerId !== activePointerId) return;
    if (activePointerId !== null && typeof canvas.releasePointerCapture === 'function') {
      try { canvas.releasePointerCapture(activePointerId); } catch { /* 已释放时忽略 */ }
    }
    activePointerId = null;
    const currentDraft = draft;
    draft = null;
    if (!currentDraft) return;
    const element = finalizeDraft(currentDraft);
    if (element) commitElement(element);
    else paint();
  }

  function createTextOrFormula(point) {
    if (readOnly) return;
    if (scene.elements.length >= MAX_ELEMENTS) {
      toast(toolText('study.tools.board.limit', '元素已达上限（{v1} 个），请先删除部分内容', { v1: MAX_ELEMENTS }), true);
      return;
    }
    if (typeof window.prompt !== 'function') return;
    if (tool === 'text') {
      const value = window.prompt(toolText('study.tools.board.textPrompt', '输入文字内容'), '');
      if (value === null) return;
      const text = String(value).trim();
      if (!text) return;
      commitElement({
        id: nextElementId(new Set(scene.elements.map(element => element.id))),
        type: 'text',
        x: roundRectValue(point.x),
        y: roundRectValue(point.y),
        text: text.slice(0, MAX_TEXT_LENGTH),
        fontSize: DEFAULT_FONT_SIZE,
        strokeColor: color,
        opacity: 100,
      });
      return;
    }
    const value = window.prompt(toolText('study.tools.board.formulaPrompt', '输入 LaTeX 公式（本地 KaTeX 渲染）'), '');
    if (value === null) return;
    const latex = String(value).trim();
    if (!latex) {
      toast(toolText('study.tools.board.emptyFormula', '公式不能为空'), true);
      return;
    }
    commitElement({
      id: nextElementId(new Set(scene.elements.map(element => element.id))),
      type: 'formula',
      x: roundRectValue(point.x),
      y: roundRectValue(point.y),
      latex: latex.slice(0, MAX_TEXT_LENGTH),
      fontSize: DEFAULT_FONT_SIZE,
      strokeColor: color,
      opacity: 100,
    });
  }

  function finalizeDraft(currentDraft) {
    const seen = new Set(scene.elements.map(element => element.id));
    const id = nextElementId(seen);
    const start = currentDraft.start;
    const current = currentDraft.current;
    if (currentDraft.tool === 'pen') {
      const points = currentDraft.points.map(point => [roundRectValue(point[0]), roundRectValue(point[1])]);
      if (!points.length) return null;
      if (points.length === 1) points.push([points[0][0] + 0.5, points[0][1] + 0.5]);
      return {
        id,
        type: 'freedraw',
        x: roundRectValue(start.x),
        y: roundRectValue(start.y),
        points,
        strokeColor: color,
        strokeWidth,
        opacity: 100,
      };
    }
    if (currentDraft.tool === 'line' || currentDraft.tool === 'arrow') {
      const deltaX = current.x - start.x;
      const deltaY = current.y - start.y;
      if (Math.hypot(deltaX, deltaY) < 2) return null;
      return {
        id,
        type: currentDraft.tool,
        x: roundRectValue(start.x),
        y: roundRectValue(start.y),
        points: [[0, 0], [roundRectValue(deltaX), roundRectValue(deltaY)]],
        strokeColor: color,
        strokeWidth,
        opacity: 100,
      };
    }
    const left = Math.min(start.x, current.x);
    const top = Math.min(start.y, current.y);
    const width = Math.abs(current.x - start.x);
    const height = Math.abs(current.y - start.y);
    if (width < 2 && height < 2) return null;
    return {
      id,
      type: currentDraft.tool,
      x: roundRectValue(left),
      y: roundRectValue(top),
      width: roundRectValue(width),
      height: roundRectValue(height),
      strokeColor: color,
      backgroundColor: 'transparent',
      strokeWidth,
      opacity: 100,
    };
  }

  function onDoubleClick(event) {
    if (readOnly) return;
    const point = pointFromEvent(event);
    const element = hitTest(point);
    if (!element || typeof window.prompt !== 'function') return;
    if (element.type === 'text') {
      const value = window.prompt(toolText('study.tools.board.editText', '编辑文字内容'), element.text);
      if (value === null) return;
      element.text = String(value).slice(0, MAX_TEXT_LENGTH);
      selectedId = element.id;
      pushHistory();
      paint();
      return;
    }
    if (element.type === 'formula') {
      const value = window.prompt(toolText('study.tools.board.editFormula', '编辑 LaTeX 公式（本地 KaTeX 渲染）'), element.latex);
      if (value === null) return;
      const latex = String(value).trim();
      if (!latex) {
        toast(toolText('study.tools.board.emptyFormula', '公式不能为空'), true);
        return;
      }
      element.latex = latex.slice(0, MAX_TEXT_LENGTH);
      selectedId = element.id;
      pushHistory();
      paint();
    }
  }

  /* -------- 绘制 -------- */

  function schedulePaint() {
    if (destroyed || pendingFrame) return;
    const run = () => {
      pendingFrame = 0;
      paint();
    };
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      pendingFrame = window.requestAnimationFrame(run);
    } else {
      pendingFrame = setTimeout(run, 16);
    }
  }

  function canvasMetrics() {
    const rect = canvas.getBoundingClientRect();
    const cssWidth = rect.width > 0 ? rect.width : DESIGN_WIDTH;
    return { rect, cssWidth };
  }

  function paint() {
    if (destroyed || !context) return;
    const { cssWidth } = canvasMetrics();
    const dpr = Math.min(2.5, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
    const pixelWidth = Math.max(1, Math.round(cssWidth * dpr));
    const pixelHeight = Math.max(1, Math.round((cssWidth * DESIGN_HEIGHT / DESIGN_WIDTH) * dpr));
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    const scaleX = canvas.width / DESIGN_WIDTH;
    const scaleY = canvas.height / DESIGN_HEIGHT;
    context.setTransform(scaleX, 0, 0, scaleY, 0, 0);
    context.fillStyle = scene.appState.viewBackgroundColor || DEFAULT_BACKGROUND;
    context.fillRect(0, 0, DESIGN_WIDTH, DESIGN_HEIGHT);

    for (const element of scene.elements) drawCanvasElement(context, element);

    if (draft) {
      const preview = finalizeDraftDraft(draft);
      if (preview) drawCanvasElement(context, preview, { formulasAsText: true });
    }
    if (selectedId) drawSelection();

    syncFormulaOverlay(cssWidth);
    root.setAttribute('data-board-ready', 'true');
  }

  /** 草稿预览元素（不改动场景、不占编号）。 */
  function finalizeDraftDraft(currentDraft) {
    const start = currentDraft.start;
    const current = currentDraft.current;
    if (currentDraft.tool === 'pen') {
      const points = currentDraft.points.length ? currentDraft.points : [[0, 0]];
      return {
        type: 'freedraw', x: start.x, y: start.y, points,
        strokeColor: color, strokeWidth, opacity: 100,
      };
    }
    if (currentDraft.tool === 'line' || currentDraft.tool === 'arrow') {
      return {
        type: currentDraft.tool,
        x: start.x,
        y: start.y,
        points: [[0, 0], [current.x - start.x, current.y - start.y]],
        strokeColor: color,
        strokeWidth,
        opacity: 100,
      };
    }
    return {
      type: currentDraft.tool,
      x: Math.min(start.x, current.x),
      y: Math.min(start.y, current.y),
      width: Math.abs(current.x - start.x),
      height: Math.abs(current.y - start.y),
      strokeColor: color,
      backgroundColor: 'transparent',
      strokeWidth,
      opacity: 100,
    };
  }

  function drawSelection() {
    const element = scene.elements.find(item => item.id === selectedId);
    if (!element) return;
    const box = elementBoundingBox(element, context);
    if (!box) return;
    context.save();
    context.strokeStyle = '#127963';
    context.lineWidth = 1.5;
    context.setLineDash([6, 4]);
    context.strokeRect(box.x - 4, box.y - 4, box.width + 8, box.height + 8);
    context.restore();
  }

  function syncFormulaOverlay(cssWidth) {
    const scale = cssWidth / DESIGN_WIDTH;
    const alive = new Set();
    for (const element of scene.elements) {
      if (element.type !== 'formula') continue;
      alive.add(element.id);
      const key = `${element.latex}::${element.fontSize}`;
      let entry = formulaNodes.get(element.id);
      if (!entry || entry.key !== key) {
        if (entry) entry.node.remove();
        const node = el('div', { class: 'st-board-formula', 'data-formula-id': element.id });
        try {
          const html = renderMath(element.latex, false);
          const safe = sanitizeMath(html);
          const template = document.createElement('template');
          template.innerHTML = safe;
          node.append(template.content);
        } catch {
          // 净化器/KaTeX 不可用时 fail-closed 为纯文本公式源。
          node.textContent = element.latex;
        }
        overlay.append(node);
        entry = { node, key };
        formulaNodes.set(element.id, entry);
      }
      const node = entry.node;
      node.style.position = 'absolute';
      node.style.left = `${(element.x / DESIGN_WIDTH) * 100}%`;
      node.style.top = `${(element.y / DESIGN_HEIGHT) * 100}%`;
      node.style.fontSize = `${(Number(element.fontSize) || DEFAULT_FONT_SIZE) * scale}px`;
      node.style.color = element.strokeColor;
      node.style.opacity = String(Math.min(100, Math.max(0, Number(element.opacity) || 0)) / 100);
      node.style.whiteSpace = 'nowrap';
      node.style.lineHeight = '1';
    }
    for (const [id, entry] of [...formulaNodes]) {
      if (alive.has(id)) continue;
      entry.node.remove();
      formulaNodes.delete(id);
    }
  }

  /* -------- 导出 -------- */

  function renderSceneCanvas(pixelWidth) {
    const width = Math.max(120, Math.round(pixelWidth));
    const height = Math.max(75, Math.round(width * DESIGN_HEIGHT / DESIGN_WIDTH));
    const exportCanvas = document.createElement('canvas');
    exportCanvas.width = width;
    exportCanvas.height = height;
    const exportContext = exportCanvas.getContext('2d');
    if (!exportContext) throw new Error(toolText('study.tools.exportFailed', '导出失败：{v1}', { v1: 'canvas' }));
    const scale = width / DESIGN_WIDTH;
    exportContext.setTransform(scale, 0, 0, scale, 0, 0);
    exportContext.fillStyle = scene.appState.viewBackgroundColor || DEFAULT_BACKGROUND;
    exportContext.fillRect(0, 0, DESIGN_WIDTH, DESIGN_HEIGHT);
    for (const element of scene.elements) drawCanvasElement(exportContext, element, { formulasAsText: true });
    return exportCanvas;
  }

  /** 导出 PNG（2 倍设计分辨率，公式按线性文本近似绘制）。 */
  function exportPNG() {
    return renderSceneCanvas(DESIGN_WIDTH * 2).toDataURL('image/png');
  }

  /** 生成 ≤64KiB 预览 PNG dataURL；仍超出返回 null（保存时省略 preview）。 */
  function buildPreviewPng() {
    for (const width of [480, 384, 320, 256, 200, 160, 120]) {
      try {
        const url = renderSceneCanvas(width).toDataURL('image/png');
        if (url.length <= MAX_PREVIEW_CHARS) return url;
      } catch {
        return null;
      }
    }
    return null;
  }

  /* -------- 对外 controller -------- */

  function getScene() {
    return cloneScene(scene);
  }

  function setScene(next) {
    const normalized = normalizeSketchScene(next);
    if (!normalized) return false;
    scene = normalized;
    selectedId = null;
    draft = null;
    history = [snapshotElements()];
    historyIndex = 0;
    formulaNodes.forEach(entry => entry.node.remove());
    formulaNodes.clear();
    updateActionState();
    paint();
    return true;
  }

  function onWindowResize() {
    schedulePaint();
  }

  // 弹层挂载/移动端全屏/窗口缩放都会改变画布 CSS 尺寸：重新按实际尺寸设置像素比后重绘。
  let resizeObserver = null;
  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(() => schedulePaint());
    resizeObserver.observe(stage);
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', finishPointer);
  canvas.addEventListener('pointercancel', finishPointer);
  canvas.addEventListener('lostpointercapture', event => {
    if (activePointerId !== null && event.pointerId === activePointerId) finishPointer(event);
  });
  canvas.addEventListener('dblclick', onDoubleClick);
  if (typeof window !== 'undefined') window.addEventListener('resize', onWindowResize);

  history = [snapshotElements()];
  historyIndex = 0;
  updateActionState();
  paint();

  return {
    element: root,
    getScene,
    setScene,
    exportPNG,
    buildPreviewPng,
    isReadOnly: () => readOnly,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (pendingFrame) {
        if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
          window.cancelAnimationFrame(pendingFrame);
        } else {
          clearTimeout(pendingFrame);
        }
        pendingFrame = 0;
      }
      if (typeof window !== 'undefined') window.removeEventListener('resize', onWindowResize);
      if (resizeObserver) {
        try { resizeObserver.disconnect(); } catch { /* 已断开时忽略 */ }
        resizeObserver = null;
      }
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', finishPointer);
      canvas.removeEventListener('pointercancel', finishPointer);
      canvas.removeEventListener('dblclick', onDoubleClick);
      formulaNodes.forEach(entry => entry.node.remove());
      formulaNodes.clear();
      root.remove();
    },
  };
}

/* ------------------------------------------------- 学习附件：读取 / 保存 */

function withTimeout(promise, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('timeout'));
    }, timeoutMs);
    promise.then((value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    }, (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
  });
}

/** 把 by-target 条目 / 已保存 artifact 对象归一化成 `{id, kind, title, revision, data}`。 */
export function normalizeSketchArtifact(input) {
  if (!input || typeof input !== 'object') return null;
  const artifact = input.artifact && typeof input.artifact === 'object' ? input.artifact : null;
  let data = input.data;
  if (data === undefined && input.revision && typeof input.revision === 'object') {
    data = input.revision.data;
  }
  if (typeof data === 'string') {
    try {
      data = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  let revision = input.revision;
  if (revision && typeof revision === 'object') revision = revision.revision;
  const revisionNumber = Number(revision);
  return {
    id: String((artifact && artifact.artifact_id) || input.artifact_id || input.id || ''),
    kind: String((artifact && artifact.kind) || input.kind || SKETCH_KIND),
    title: String((artifact && artifact.title) || input.title || ''),
    revision: Number.isFinite(revisionNumber) && revisionNumber > 0 ? revisionNumber : null,
    data,
  };
}

function isRevisionConflict(error) {
  const message = String(error && error.message ? error.message : '');
  return /附件已被更新|刷新后重试|版本冲突/.test(message);
}

function formatTimestamp(date = new Date()) {
  const pad = value => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function clampTitle(text) {
  return String(text || '').slice(0, 120);
}

function downloadDataUrl(dataUrl, filename) {
  const link = el('a', { href: dataUrl, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
}

/**
 * 打开解题白板弹层（原生 dialog：桌面大窗 / 手机全屏由 CSS 控制，Esc 关闭并归还焦点）。
 * @param {{ref?: string, artifact?: object, readOnly?: boolean, onSaved?: Function,
 *          trigger?: HTMLElement, titleHint?: string}} options
 */
export function openSketchBoard(options = {}) {
  const ref = String(options.ref || '').trim();
  const readOnly = options.readOnly === true;
  const loadedArtifact = normalizeSketchArtifact(options.artifact);
  const hint = String(options.titleHint || '').trim();
  const state = {
    artifactId: loadedArtifact ? loadedArtifact.id : '',
    revision: loadedArtifact ? loadedArtifact.revision : null,
    title: loadedArtifact && loadedArtifact.title
      ? clampTitle(loadedArtifact.title)
      : clampTitle(toolText('study.tools.board.titleDefault', '解题白板 · {v1}', {
        v1: hint || ref || formatTimestamp(),
      })),
  };

  const dialog = el('dialog', {
    class: 'st-dialog st-board-dialog',
    'data-study-tool': 'whiteboard-dialog',
    'aria-label': toolText('study.tools.title.board', '解题白板'),
  });
  const titleNode = el('h3', { class: 'st-dialog-title', 'data-no-i18n': 'true' }, state.title);
  const metaNode = el('p', { class: 'st-dialog-meta' });
  const board = createSketchBoard({
    scene: loadedArtifact ? loadedArtifact.data : defaultSketchScene(),
    readOnly,
  });
  const saveButton = button(
    toolText('study.tools.save', '保存为学习附件'),
    event => busy(event.currentTarget, () => saveArtifact()),
    '',
    'download',
    { 'data-study-tool-save': 'true' },
  );
  const exportButton = button(
    toolText('study.tools.exportPng', '导出 PNG'),
    () => exportImage(),
    'secondary',
    'download',
    { 'data-study-tool-export': 'true' },
  );
  const closeButton = button(toolText('study.tools.close', '关闭'), () => dialog.close(), 'secondary');

  dialog.append(
    el('div', { class: 'st-dialog-head' },
      el('div', {},
        el('div', { class: 'st-dialog-eyebrow' }, toolText('study.tools.artifactEyebrow', '学习附件')),
        titleNode),
      el('button', {
        type: 'button',
        class: 'st-dialog-close',
        'aria-label': toolText('study.tools.close', '关闭'),
        title: toolText('study.tools.close', '关闭'),
        onClick: () => dialog.close(),
      }, icon('close'))),
    el('div', { class: 'st-dialog-body' },
      board.element,
      el('div', { class: 'st-dialog-info' },
        metaNode,
        el('p', { class: 'st-dialog-hint' }, toolText(
          'study.tools.board.saveHint',
          '保存的是可继续编辑的场景（不是截图）；另存新版本会保留历史版本。',
        )))),
    el('div', { class: 'st-dialog-actions' }, closeButton, exportButton, saveButton),
  );

  function updateMeta() {
    if (state.artifactId) {
      metaNode.textContent = toolText('study.tools.artifactMeta', '附件 {v1} · 第 {v2} 版', {
        v1: state.artifactId,
        v2: state.revision === null ? '—' : state.revision,
      });
    } else {
      metaNode.textContent = toolText('study.tools.notSaved', '尚未保存为学习附件');
    }
    const saved = Boolean(state.artifactId);
    saveButton.textContent = saved
      ? toolText('study.tools.saveNewVersion', '另存新版本')
      : toolText('study.tools.save', '保存为学习附件');
    saveButton.classList.toggle('is-saved', saved);
  }

  function buildPreview() {
    let png = '';
    try {
      png = board.buildPreviewPng() || '';
    } catch {
      png = '';
    }
    if (!png) return null;
    return { kind: 'whiteboard-image', png };
  }

  function exportImage() {
    try {
      const dataUrl = board.exportPNG();
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      downloadDataUrl(dataUrl, `whiteboard-${stamp}.png`);
      toast(toolText('study.tools.exportDone', '已导出 PNG'));
    } catch (error) {
      toast(toolText('study.tools.exportFailed', '导出失败：{v1}', {
        v1: error && error.message ? error.message : '',
      }), true);
    }
  }

  async function saveArtifact() {
    const scene = board.getScene();
    if (sketchSceneBytes(scene) > MAX_DATA_BYTES) {
      toast(toolText('study.tools.board.tooLarge', '草稿过大，请精简后保存'), true);
      return;
    }
    const preview = buildPreview();
    if (!preview) {
      // 预览超出 64KiB 时省略，但保存照常继续（服务端 data 上限另有 256KiB 校验）。
      toast(toolText('study.tools.board.previewOmitted', '白板预览过大已省略，场景内容仍会保存。'));
    }
    try {
      if (state.artifactId) {
        const payload = { data: scene, title: state.title };
        if (preview) payload.preview = preview;
        else payload.preview = null;
        if (state.revision !== null) payload.expected_revision = state.revision;
        const updated = await api(`/artifacts/${encodeURIComponent(state.artifactId)}`, {
          method: 'PATCH',
          data: payload,
        });
        const artifact = updated && updated.artifact ? updated.artifact : {};
        const revision = updated && updated.revision ? updated.revision.revision : null;
        state.revision = Number.isFinite(Number(revision)) ? Number(revision) : state.revision;
        if (artifact.title) state.title = clampTitle(artifact.title);
        titleNode.textContent = state.title;
        updateMeta();
        toast(toolText('study.tools.saved', '已保存为学习附件：{v1} · 第 {v2} 版', {
          v1: state.artifactId,
          v2: state.revision === null ? '—' : state.revision,
        }));
        if (typeof options.onSaved === 'function') {
          try {
            options.onSaved({ artifactId: state.artifactId, revision: state.revision, kind: SKETCH_KIND });
          } catch { /* 回调异常不影响保存结果 */ }
        }
        return;
      }
      const body = {
        kind: SKETCH_KIND,
        title: state.title,
        data: scene,
        preview,
        source_kind: ref ? ARTIFACT_TARGET_KIND : undefined,
        source_ref: ref || undefined,
        links: ref ? [{ target_kind: ARTIFACT_TARGET_KIND, target_key: ref }] : [],
      };
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      const artifactId = String(artifact.artifact_id || '');
      if (!artifactId) throw new Error(toolText('study.tools.missingArtifactId', '服务端未返回学习附件 ID'));
      const revision = created && created.revision ? created.revision.revision : null;
      state.artifactId = artifactId;
      state.revision = Number.isFinite(Number(revision)) ? Number(revision) : null;
      if (artifact.title) state.title = clampTitle(artifact.title);
      titleNode.textContent = state.title;
      updateMeta();
      toast(toolText('study.tools.saved', '已保存为学习附件：{v1} · 第 {v2} 版', {
        v1: artifactId,
        v2: state.revision === null ? '1' : state.revision,
      }));
      if (typeof options.onSaved === 'function') {
        try {
          options.onSaved({ artifactId, revision: state.revision, kind: SKETCH_KIND });
        } catch { /* 同上 */ }
      }
    } catch (error) {
      if (isRevisionConflict(error)) {
        toast(toolText(
          'study.tools.saveConflict',
          '附件已在别处更新，请刷新后重试（当前版本 {v1}）',
          { v1: state.revision === null ? '—' : state.revision },
        ), true);
      } else {
        toast(toolText('study.tools.saveFailed', '保存学习附件失败：{v1}', {
          v1: error && error.message ? error.message : '',
        }), true);
      }
    }
  }

  updateMeta();
  document.body.append(dialog);
  dialog.addEventListener('close', () => {
    try {
      board.destroy();
    } catch {
      /* 释放失败不影响关闭 */
    }
    dialog.remove();
    try {
      options.trigger?.focus?.();
    } catch {
      /* 焦点归还失败忽略 */
    }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');

  return {
    element: dialog,
    dialog,
    board,
    getScene: () => board.getScene(),
    setScene: scene => board.setScene(scene),
    exportPNG: () => board.exportPNG(),
    getArtifact: () => ({
      artifactId: state.artifactId,
      revision: state.revision,
      kind: SKETCH_KIND,
      title: state.title,
    }),
    close: () => dialog.close(),
    destroy: () => dialog.close(),
  };
}

function pickArtifactItem(items, kind) {
  if (!Array.isArray(items)) return null;
  let picked = null;
  for (const item of items) {
    const artifact = item && item.artifact ? item.artifact : null;
    if (artifact && String(artifact.kind || '') === kind) picked = item;
  }
  return picked ? normalizeSketchArtifact(picked) : null;
}

/**
 * 打开某道题目的解题白板：先探测 by-target 已保存附件（超时 4s，失败静默），
 * 命中则载入最新场景，否则打开空白白板。
 */
export async function openSketchBoardForTarget(options = {}) {
  const ref = String(options.ref || '').trim();
  let artifact = null;
  if (ref) {
    try {
      const response = await withTimeout(
        api(`/artifacts/by-target?${query({ target_kind: ARTIFACT_TARGET_KIND, target_key: ref })}`),
        PROBE_TIMEOUT_MS,
      );
      artifact = pickArtifactItem(response && response.items, SKETCH_KIND);
    } catch {
      artifact = pickArtifactItem(options.fallbackItems, SKETCH_KIND);
    }
  }
  return openSketchBoard({
    ref,
    artifact,
    readOnly: options.readOnly === true,
    onSaved: options.onSaved,
    trigger: options.trigger,
    titleHint: options.titleHint,
  });
}
