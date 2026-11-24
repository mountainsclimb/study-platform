/* AI 课堂 P6a 幻灯片结构化编辑器（原生 ESM，无构建、无第三方库）。
 *
 * 路由：`#/classroom?course=ID&view=edit[&scene=SID]`，由 pages/classroom.js 挂载。
 * 数据：`GET /api/classroom/courses/{id}/scenes/{sid}/content` 读取 `content + version`；
 *      `PATCH .../content {version, content}` 保存（version CAS，409 时弹「重新加载 / 覆盖保存」）；
 *      `POST  .../duplicate` 复制场景。保存后播放器/导出读同一 content_json，立即生效。
 *
 * 能力：1000×562.5 等比舞台（适应宽度 + 50%–200% 缩放）、元素点选、拖动、
 * 8 向缩放（最小 20×20、画布内钳制）、方向键微调（1px / Shift 10px）、Delete、
 * 50 步撤销/重做（Ctrl/⌘+Z、Ctrl/⌘+Shift+Z / Ctrl+Y）、复制粘贴（Ctrl/⌘+C/V）、
 * 属性面板（文字/字号/颜色/对齐/加粗、形状底色、线条颜色粗细线型与端点、公式、图片素材替换、
 * 坐标尺寸、层级）、新增文本/形状/线条/公式/图片、元素列表、800ms 防抖自动保存 + 手动保存 +
 * 未保存标记 + 离开确认。
 *
 * 安全：文本 HTML 由本编辑器用 escapeHtml 逐段转义生成；回读走 classroomShared.slideTextNode
 * （DOMPurify 白名单 + inline style 二次过滤）；公式走 classroomShared.latexNode（KaTeX 清洗）。
 * 禁止任何 innerHTML 拼接用户内容，禁止 eval / Function。
 */
import { api } from '../core/api.js';
import { el, button, notice, loading, toast } from '../core/dom.js';
import { current, go } from '../core/router.js';
import { formulaEditorButton } from './formulaEditor.js';
import {
  SLIDE_HEIGHT, SLIDE_WIDTH, escapeHtml, latexNode, slideTextNode,
  typeLabel as sharedTypeLabel,
} from './classroomShared.js';

const MIN_SIZE = 20;
const HISTORY_LIMIT = 50;
const AUTOSAVE_MS = 800;
const HANDLE_DIRS = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const BLOCK_TAGS = new Set(['P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL']);

const TYPE_LABELS = {
  text: '文本', shape: '形状', line: '线条', latex: '公式',
  image: '图片', table: '表格', chart: '图表', code: '代码',
};

const DEFAULT_THEME = {
  backgroundColor: '#ffffff',
  themeColors: ['#1f6feb', '#e8590c', '#2f9e44', '#7048e8', '#c2255c'],
  fontColor: '#222222',
  fontName: 'Microsoft YaHei',
};

const ZOOM_OPTIONS = [
  { value: 'fit', label: '适应宽度' },
  { value: '0.5', label: '50%' },
  { value: '0.75', label: '75%' },
  { value: '1', label: '100%' },
  { value: '1.25', label: '125%' },
  { value: '1.5', label: '150%' },
  { value: '2', label: '200%' },
];

const HEX_COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

// ---------------------------------------------------------------------------
// 纯工具
// ---------------------------------------------------------------------------

function clamp(value, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number)) return min;
  return Math.max(min, Math.min(max, number));
}

function num(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function round2(value) {
  return Math.round(num(value) * 100) / 100;
}

function clone(value) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return null;
  }
}

function sceneTypeOf(scene) {
  return String(scene?.scene_type || scene?.type || '');
}

function isSlideScene(scene) {
  return sceneTypeOf(scene) === 'slide';
}

function typeLabelOf(type) {
  const key = String(type || '');
  return TYPE_LABELS[key] || sharedTypeLabel(key) || '元素';
}

function makeSvg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
  }
  return node;
}

function normalizeTheme(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const colors = Array.isArray(source.themeColors)
    ? source.themeColors.filter(color => HEX_COLOR_RE.test(String(color || ''))).slice(0, 8)
    : [];
  return {
    backgroundColor: HEX_COLOR_RE.test(String(source.backgroundColor || '')) ? source.backgroundColor : DEFAULT_THEME.backgroundColor,
    themeColors: colors.length ? colors : [...DEFAULT_THEME.themeColors],
    fontColor: HEX_COLOR_RE.test(String(source.fontColor || '')) ? source.fontColor : DEFAULT_THEME.fontColor,
    fontName: String(source.fontName || '').slice(0, 60) || DEFAULT_THEME.fontName,
  };
}

function absoluteStyle(item, extra = '') {
  const parts = [
    `left:${num(item.left)}px`,
    `top:${num(item.top)}px`,
    `width:${Math.max(0, num(item.width))}px`,
    `height:${Math.max(0, num(item.height))}px`,
  ];
  if (num(item.rotate)) parts.push(`transform:rotate(${num(item.rotate)}deg)`);
  return `${parts.join(';')}${extra ? ';' + extra : ''}`;
}

function lineBBox(item) {
  const start = Array.isArray(item.start) ? item.start : [0, 0];
  const end = Array.isArray(item.end) ? item.end : [0, 0];
  const x1 = num(item.left) + num(start[0]);
  const y1 = num(item.top) + num(start[1]);
  const x2 = num(item.left) + num(end[0]);
  const y2 = num(item.top) + num(end[1]);
  return {
    left: Math.min(x1, x2),
    top: Math.min(y1, y2),
    width: Math.max(2, Math.abs(x2 - x1)),
    height: Math.max(2, Math.abs(y2 - y1)),
    x1, y1, x2, y2,
  };
}

function elementBox(item) {
  if (item?.type === 'line') {
    const box = lineBBox(item);
    return { left: box.left, top: box.top, width: box.width, height: box.height };
  }
  return {
    left: num(item?.left),
    top: num(item?.top),
    width: Math.max(MIN_SIZE, num(item?.width, 100)),
    height: Math.max(MIN_SIZE, num(item?.height, 60)),
  };
}

function resolveAssetSrc(raw) {
  const value = String(raw || '');
  if (/^\d+$/.test(value)) return `/api/classroom/assets/${value}`;
  return value;
}

/** 取文本元素的纯文本（按块级元素/换行拆行；回读走 DOMPurify 白名单渲染结果）。 */
function textPlainOf(item) {
  const content = String(item?.content || '');
  if (!content.trim()) return String(item?.text || '');
  const holder = slideTextNode(content, item?.text);
  const lines = [];
  let current = '';
  const flush = () => { lines.push(current.replace(/[ \t]+$/g, '')); current = ''; };
  (function walk(node) {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { // TEXT_NODE
        current += child.textContent;
      } else if (child.nodeName === 'BR') {
        flush();
      } else if (BLOCK_TAGS.has(child.nodeName)) {
        if (current) flush();
        walk(child);
        flush();
      } else {
        walk(child);
      }
    }
  })(holder);
  if (current) lines.push(current);
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  return lines.length ? lines.join('\n') : String(item?.text || '');
}

function textPropsOf(item) {
  const content = String(item?.content || '');
  const sizeMatch = /font-size\s*:\s*([\d.]+)px/i.exec(content);
  const colorMatch = /(?:^|;)\s*color\s*:\s*(#[0-9a-fA-F]{3,8})/.exec(content);
  const alignMatch = /text-align\s*:\s*(left|center|right)/i.exec(content);
  const alignRaw = String(item?.align || (alignMatch ? alignMatch[1] : 'left') || 'left').toLowerCase();
  const colorRaw = String(item?.color || (colorMatch ? colorMatch[1] : '') || '#333333');
  return {
    text: textPlainOf(item),
    fontSize: Math.round(clamp(num(item?.fontSize, sizeMatch ? Number(sizeMatch[1]) : 24), 10, 72)),
    color: HEX_COLOR_RE.test(colorRaw) ? colorRaw : '#333333',
    align: ['left', 'center', 'right'].includes(alignRaw) ? alignRaw : 'left',
    bold: /font-weight\s*:\s*(?:bold|[6-9]00)|<strong|<b[\s>]/i.test(content),
  };
}

function textHtmlFrom({ text, fontSize, color, align, bold }) {
  const styles = [`font-size: ${Math.round(fontSize)}px`];
  if (HEX_COLOR_RE.test(String(color || ''))) styles.push(`color: ${color}`);
  styles.push(`text-align: ${align || 'left'}`);
  if (bold) styles.push('font-weight: bold');
  const style = styles.join('; ');
  const lines = String(text ?? '').split(/\r?\n/);
  const html = lines
    .map(line => `<p style="${style}">${escapeHtml(line)}</p>`)
    .join('');
  return html || `<p style="${style}"></p>`;
}

function setTextProps(item, patch) {
  const props = { ...textPropsOf(item), ...patch };
  item.fontSize = Math.round(clamp(props.fontSize, 10, 72));
  item.color = HEX_COLOR_RE.test(String(props.color || '')) ? props.color : '#333333';
  item.defaultColor = item.color;
  item.align = ['left', 'center', 'right'].includes(props.align) ? props.align : 'left';
  item.text = String(props.text ?? '');
  item.content = textHtmlFrom(props);
}

function elementDigest(item) {
  if (!item || typeof item !== 'object') return '';
  switch (item.type) {
    case 'text': return textPlainOf(item).replace(/\s+/g, ' ').trim().slice(0, 40) || '空文本';
    case 'latex': return String(item.latex || '').slice(0, 40);
    case 'image': return String(item.alt || item.src || '').slice(0, 40) || '图片';
    case 'table': return `${(Array.isArray(item.data) ? item.data.length : 0)} 行表格`;
    case 'chart': return `图表 · ${String(item.chartType || 'bar')}`;
    case 'code': return String(item.code || '').slice(0, 40);
    case 'line': return `线宽 ${Math.round(num(item.width, 2))}px`;
    case 'shape': return `底色 ${String(item.fill || '')}`;
    default: return '';
  }
}

function chartSvg(element) {
  const data = element.data || {};
  const labels = Array.isArray(data.labels) ? data.labels : [];
  const series = Array.isArray(data.series) ? data.series.filter(row => Array.isArray(row)) : [];
  const width = Math.max(160, num(element.width, 320));
  const height = Math.max(120, num(element.height, 220));
  const svg = makeSvg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': '图表' });
  const colors = Array.isArray(element.themeColors) && element.themeColors.length
    ? element.themeColors : ['#1f6feb', '#e8590c', '#2f9e44'];
  const type = String(element.chartType || 'bar');
  const padLeft = 34;
  const padBottom = 24;
  const plotWidth = width - padLeft - 12;
  const plotHeight = height - padBottom - 16;
  const values = series.flat().map(Number).filter(Number.isFinite);
  const maxValue = Math.max(1, ...values);
  const pointX = index => padLeft + (labels.length <= 1 ? plotWidth / 2 : (plotWidth * index) / Math.max(1, labels.length - 1));
  const pointY = value => 12 + plotHeight - (Number(value) || 0) / maxValue * plotHeight;
  svg.append(makeSvg('line', { x1: padLeft, y1: 12, x2: padLeft, y2: 12 + plotHeight, stroke: '#cbd5e1', 'stroke-width': 1 }));
  svg.append(makeSvg('line', { x1: padLeft, y1: 12 + plotHeight, x2: width - 8, y2: 12 + plotHeight, stroke: '#cbd5e1', 'stroke-width': 1 }));
  labels.forEach((label, index) => {
    const text = makeSvg('text', { x: pointX(index), y: height - 6, 'font-size': 11, 'text-anchor': 'middle', fill: '#64748b' });
    text.textContent = String(label).slice(0, 10);
    svg.append(text);
  });
  if (type === 'pie' || type === 'ring') {
    const row = series[0] || [];
    const total = row.reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0) || 1;
    let angle = -Math.PI / 2;
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) / 2 - 16;
    row.forEach((value, index) => {
      const slice = (Math.max(0, Number(value) || 0) / total) * Math.PI * 2;
      if (slice <= 0) return;
      const x1 = cx + radius * Math.cos(angle);
      const y1 = cy + radius * Math.sin(angle);
      const x2 = cx + radius * Math.cos(angle + slice);
      const y2 = cy + radius * Math.sin(angle + slice);
      const large = slice > Math.PI ? 1 : 0;
      svg.append(makeSvg('path', {
        d: `M ${cx} ${cy} L ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`,
        fill: colors[index % colors.length], 'fill-opacity': type === 'ring' ? 1 : 0.86,
      }));
      angle += slice;
    });
    if (type === 'ring') svg.append(makeSvg('circle', { cx, cy, r: radius * 0.55, fill: '#ffffff' }));
    return svg;
  }
  series.forEach((row, seriesIndex) => {
    const color = colors[seriesIndex % colors.length];
    if (type === 'line' || type === 'area' || type === 'radar' || type === 'scatter') {
      const points = row.map((value, index) => `${pointX(index)},${pointY(value)}`).join(' ');
      if (type === 'area') {
        svg.append(makeSvg('polygon', {
          points: `${padLeft},${12 + plotHeight} ${points} ${width - 8},${12 + plotHeight}`,
          fill: color, 'fill-opacity': 0.16,
        }));
      }
      svg.append(makeSvg('polyline', { points, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round' }));
      row.forEach((value, index) => svg.append(makeSvg('circle', { cx: pointX(index), cy: pointY(value), r: 2.8, fill: color })));
    } else {
      const columnWidth = Math.max(8, plotWidth / Math.max(1, labels.length) / Math.max(1, series.length) - 4);
      row.forEach((value, index) => {
        const x = padLeft + (plotWidth / Math.max(1, labels.length)) * index + seriesIndex * (columnWidth + 4) + 2;
        const y = pointY(value);
        svg.append(makeSvg('rect', {
          x, y, width: columnWidth, height: Math.max(1, 12 + plotHeight - y),
          fill: color, 'fill-opacity': 0.86, rx: 2,
        }));
      });
    }
  });
  return svg;
}

function detailText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item?.msg || String(item)).join('；');
  if (detail && typeof detail === 'object') return String(detail.message || '');
  return '';
}

// ---------------------------------------------------------------------------
// 编辑器视图
// ---------------------------------------------------------------------------

export function createEditorView({ container, version: renderVersion, courseId, sceneId = 0 }) {
  const state = {
    destroyed: false,
    course: null,
    scenes: [],
    slideScenes: [],
    sceneId: 0,
    sceneTitle: '',
    version: 1,
    theme: { ...DEFAULT_THEME, themeColors: [...DEFAULT_THEME.themeColors] },
    elements: [],
    selectedId: null,
    dirty: false,
    saveState: 'loading',
    revision: 0,
    history: [],
    future: [],
    clipboard: null,
    zoom: 'fit',
    fitScale: 1,
    scale: 1,
    sideTab: 'props',
  };
  let savePromise = null;
  const nodeById = new Map();
  let canvas = null;
  let stage = null;
  let stageScroll = null;
  let overlay = null;
  let drag = null;
  let resizeObserver = null;
  let autoTimer = null;
  let conflictDialog = null;

  // -------------------------------------------------------------------------
  // 骨架
  // -------------------------------------------------------------------------
  const root = el('div', { class: 'cls-page cls-editor-page' });
  const shell = el('div', { class: 'cls-editor' });
  const toolbar = el('div', { class: 'cls-editor-toolbar' });
  const backButton = button('返回播放', () => requestLeave(() => {
    go('classroom', null, { course: courseId, view: 'play' });
  }), 'ghost small', 'chevron');
  const titleNode = el('div', { class: 'cls-editor-title' });
  const sceneSelect = el('select', { class: 'cls-editor-scene-select', 'aria-label': '切换幻灯片场景' });
  const statusNode = el('span', { class: 'cls-editor-status', 'data-state': 'loading', role: 'status', 'aria-live': 'polite' }, '加载中…');
  const undoButton = button('撤销', undo, 'ghost small', 'history');
  const redoButton = button('重做', redo, 'ghost small', 'repeat');
  const saveButton = button('保存', () => save({ manual: true }), 'small', 'check');
  const duplicateButton = button('复制场景', duplicateScene, 'secondary small', 'copy');
  const zoomSelect = el('select', { class: 'cls-editor-zoom-select', 'aria-label': '画布缩放比例' });
  for (const option of ZOOM_OPTIONS) {
    zoomSelect.append(el('option', { value: option.value }, option.label));
  }
  zoomSelect.value = state.zoom;

  toolbar.append(
    el('div', { class: 'cls-editor-toolbar-group cls-editor-toolbar-left' },
      backButton,
      titleNode,
      sceneSelect,
      statusNode),
    el('div', { class: 'cls-editor-toolbar-group cls-editor-toolbar-right' },
      el('span', { class: 'help cls-editor-zoom-label' }, '缩放'),
      zoomSelect,
      undoButton,
      redoButton,
      saveButton,
      duplicateButton),
  );

  stageScroll = el('div', { class: 'cls-editor-stage-scroll' });
  stage = el('div', { class: 'cls-editor-stage' });
  canvas = el('div', { class: 'cls-editor-canvas', role: 'application', 'aria-label': '幻灯片画布' });
  stage.append(canvas);
  stageScroll.append(stage);
  const canvasHint = el('p', { class: 'help cls-editor-canvas-hint' },
    '拖动元素移动；拖 8 个控制点缩放（最小 20×20）；方向键微调（Shift 10px）；Delete 删除；'
    + 'Ctrl/⌘+Z 撤销、Ctrl/⌘+Shift+Z 重做；Ctrl/⌘+C/V 复制粘贴；Ctrl/⌘+S 保存。');
  const canvasCol = el('div', { class: 'cls-editor-canvas-col' }, stageScroll, canvasHint);

  const sidePanel = el('aside', { class: 'cls-editor-side', 'aria-label': '元素属性与列表' });
  const tabs = el('div', { class: 'tabs cls-editor-tabs', role: 'tablist' });
  const propsTab = el('button', { type: 'button', class: 'tab active', role: 'tab', 'aria-selected': 'true' }, '属性');
  const listTab = el('button', { type: 'button', class: 'tab', role: 'tab', 'aria-selected': 'false' }, '元素');
  tabs.append(propsTab, listTab);
  const panelHost = el('div', { class: 'cls-editor-panels' });
  sidePanel.append(tabs, panelHost);

  shell.append(toolbar, el('div', { class: 'cls-editor-body' }, canvasCol, sidePanel));
  root.append(shell);
  container.append(root);

  // -------------------------------------------------------------------------
  // 小工具
  // -------------------------------------------------------------------------
  function endpoint() {
    return `/api/classroom/courses/${courseId}/scenes/${state.sceneId}/content`;
  }

  function selectedItem() {
    return state.elements.find(item => String(item.id) === String(state.selectedId)) || null;
  }

  function snapshotElements() {
    return JSON.stringify(state.elements);
  }

  function recordHistory(snapshot) {
    if (typeof snapshot !== 'string') return;
    if (state.history[state.history.length - 1] === snapshot) return;
    state.history.push(snapshot);
    if (state.history.length > HISTORY_LIMIT) state.history.shift();
    state.future = [];
    updateToolbar();
  }

  function applyMutation(mutator) {
    const snapshot = snapshotElements();
    mutator();
    recordHistory(snapshot);
    touch();
  }

  function touch() {
    state.dirty = true;
    state.revision += 1;
    if (state.saveState !== 'saving') setSaveState('dirty');
    scheduleAutoSave();
  }

  function scheduleAutoSave() {
    clearTimeout(autoTimer);
    autoTimer = setTimeout(() => {
      autoTimer = null;
      if (!state.destroyed && state.dirty) save({ manual: false, silent: true });
    }, AUTOSAVE_MS);
  }

  function setSaveState(name) {
    state.saveState = name;
    const labels = {
      loading: '加载中…',
      saved: `已保存 · v${state.version}`,
      dirty: '未保存修改',
      saving: '正在保存…',
      error: '保存失败，点保存重试',
      conflict: '版本冲突，待处理',
    };
    statusNode.textContent = labels[name] || name;
    statusNode.dataset.state = name;
    updateToolbar();
  }

  function updateToolbar() {
    undoButton.disabled = state.history.length === 0;
    redoButton.disabled = state.future.length === 0;
    saveButton.disabled = state.saveState === 'saving';
    duplicateButton.disabled = state.saveState === 'loading';
  }

  function updateTitle() {
    titleNode.replaceChildren(
      el('strong', { class: 'cls-editor-course-title' }, state.course?.title || 'AI 课堂'),
      el('span', { class: 'help cls-editor-scene-title' }, state.sceneTitle || ''),
    );
  }

  function updateSceneSelect() {
    sceneSelect.replaceChildren();
    for (const scene of state.slideScenes) {
      const index = num(scene.order, num(scene.order_index));
      const label = `${index ? `第 ${index} 页 · ` : ''}${scene.title || '未命名场景'}`;
      sceneSelect.append(el('option', { value: String(scene.id) }, label));
    }
    sceneSelect.value = String(state.sceneId);
    sceneSelect.disabled = state.slideScenes.length <= 1;
  }

  // -------------------------------------------------------------------------
  // 舞台缩放
  // -------------------------------------------------------------------------
  function applyStageScale() {
    if (!stageScroll || !canvas) return;
    const available = Math.max(240, (stageScroll.clientWidth || 960) - 4);
    state.fitScale = Math.min(1, available / SLIDE_WIDTH);
    state.scale = state.zoom === 'fit' ? state.fitScale : clamp(Number(state.zoom) || 1, 0.3, 3);
    stage.style.width = `${Math.max(80, Math.round(SLIDE_WIDTH * state.scale))}px`;
    stage.style.height = `${Math.max(45, Math.round(SLIDE_HEIGHT * state.scale))}px`;
    canvas.style.width = `${SLIDE_WIDTH}px`;
    canvas.style.height = `${SLIDE_HEIGHT}px`;
    canvas.style.transform = `scale(${state.scale})`;
    canvas.style.background = state.theme.backgroundColor || '#ffffff';
  }

  function onWindowResize() {
    if (!state.destroyed) applyStageScale();
  }

  // -------------------------------------------------------------------------
  // 元素渲染（只读视觉复用播放器的安全渲染路径）
  // -------------------------------------------------------------------------
  function renderElementNode(item) {
    if (!item || typeof item !== 'object' || !item.id) return null;
    const id = String(item.id);
    const common = {
      class: 'cls-el cls-editor-element',
      'data-element-id': id,
      'data-element-type': String(item.type || ''),
      tabindex: '-1',
      title: `${typeLabelOf(item.type)} · ${elementDigest(item)}`,
    };
    let node;
    switch (item.type) {
      case 'text': {
        node = el('div', {
          ...common,
          class: `${common.class} cls-el-text`,
          style: absoluteStyle(item, `font-family:${state.theme.fontName || 'Microsoft YaHei'}`),
        });
        node.append(slideTextNode(item.content, item.text));
        break;
      }
      case 'shape': {
        node = el('div', { ...common, class: `${common.class} cls-el-shape`, style: absoluteStyle(item) });
        const box = Array.isArray(item.viewBox) && item.viewBox.length >= 2 ? item.viewBox : [1, 1];
        const svg = makeSvg('svg', { viewBox: `0 0 ${num(box[0], 1) || 1} ${num(box[1], 1) || 1}`, preserveAspectRatio: 'none', class: 'cls-shape-svg' });
        const path = makeSvg('path', {
          d: String(item.path || 'M 0 0 L 1 0 L 1 1 L 0 1 Z'),
          fill: String(item.fill || '#5b9bd5'),
        });
        svg.append(path);
        node.append(svg);
        break;
      }
      case 'line': {
        const bbox = lineBBox(item);
        const pad = 6;
        const width = Math.max(12, bbox.width + pad * 2);
        const height = Math.max(12, bbox.height + pad * 2);
        node = el('div', {
          ...common,
          class: `${common.class} cls-el-line`,
          'data-line-left': String(num(item.left)),
          'data-line-top': String(num(item.top)),
          style: `left:${round2(bbox.left - pad)}px;top:${round2(bbox.top - pad)}px;width:${width}px;height:${height}px`,
        });
        const svg = makeSvg('svg', { class: 'cls-line-svg', viewBox: `0 0 ${width} ${height}`, width, height });
        const x1 = bbox.x1 - (bbox.left - pad);
        const y1 = bbox.y1 - (bbox.top - pad);
        const x2 = bbox.x2 - (bbox.left - pad);
        const y2 = bbox.y2 - (bbox.top - pad);
        const dash = item.style === 'dashed' ? '8 6' : item.style === 'dotted' ? '2 5' : null;
        svg.append(makeSvg('line', {
          x1, y1, x2, y2, stroke: String(item.color || '#333333'),
          'stroke-width': Math.max(1, num(item.width, 2)),
          'stroke-linecap': 'round',
          ...(dash ? { 'stroke-dasharray': dash } : {}),
        }));
        const points = Array.isArray(item.points) ? item.points : ['', ''];
        const angle = Math.atan2(y2 - y1, x2 - x1);
        const arrowSize = 9;
        const addArrow = (x, y, direction) => {
          const a = angle + direction;
          svg.append(makeSvg('path', {
            d: `M ${x} ${y} l ${arrowSize * Math.cos(a)} ${arrowSize * Math.sin(a)} l ${arrowSize * Math.cos(a + 1.2)} ${arrowSize * Math.sin(a + 1.2)} Z`,
            fill: String(item.color || '#333333'),
          }));
        };
        if (points[1] === 'arrow') addArrow(x2, y2, Math.PI);
        if (points[0] === 'arrow') addArrow(x1, y1, 0);
        const dotRadius = Math.max(2, num(item.width, 2) * 0.9);
        if (points[1] === 'dot') svg.append(makeSvg('circle', { cx: x2, cy: y2, r: dotRadius, fill: String(item.color || '#333333') }));
        if (points[0] === 'dot') svg.append(makeSvg('circle', { cx: x1, cy: y1, r: dotRadius, fill: String(item.color || '#333333') }));
        node.append(svg);
        break;
      }
      case 'image': {
        node = el('div', { ...common, class: `${common.class} cls-el-image`, style: absoluteStyle(item, 'border-radius:6px;overflow:hidden') });
        const src = resolveAssetSrc(item.src);
        if (src) {
          const image = el('img', { src, alt: String(item.alt || '幻灯片配图'), loading: 'lazy' });
          image.addEventListener('error', () => {
            image.remove();
            node.append(el('span', { class: 'cls-image-fallback' }, '图片不可用'));
          }, { once: true });
          node.append(image);
        } else {
          node.append(el('span', { class: 'cls-image-fallback' }, '未设置图片素材'));
        }
        break;
      }
      case 'latex': {
        node = el('div', {
          ...common,
          class: `${common.class} cls-el-latex`,
          style: absoluteStyle(item, 'display:flex;align-items:center;justify-content:center;overflow:hidden'),
        });
        const source = String(item.latex || '');
        const width = num(item.width, 300);
        const height = num(item.height, 90);
        const byHeight = height * 0.46;
        const byWidth = width / Math.max(8, source.length) * 1.15;
        node.style.fontSize = `${clamp(Math.round(Math.min(byHeight, byWidth, 36)), 9, 36)}px`;
        node.append(latexNode(source, { display: true }));
        break;
      }
      case 'table': {
        node = el('div', { ...common, class: `${common.class} cls-el-table`, style: absoluteStyle(item, 'overflow:hidden') });
        const rows = Array.isArray(item.data) ? item.data : [];
        const colWidths = Array.isArray(item.colWidths) && item.colWidths.length ? item.colWidths : [];
        const columnCount = Math.max(1, ...rows.map(row => (Array.isArray(row) ? row.length : 0)), colWidths.length);
        const table = el('table', { class: 'cls-slide-table' });
        const colgroup = el('colgroup');
        for (let index = 0; index < columnCount; index += 1) {
          colgroup.append(el('col', { style: `width:${Math.round((num(colWidths[index], 1 / columnCount) * 1000)) / 10}%` }));
        }
        table.append(colgroup);
        for (const row of rows) {
          const tr = el('tr');
          for (const cell of (Array.isArray(row) ? row : [])) {
            const td = el('td', { colspan: num(cell?.colspan, 1), rowspan: num(cell?.rowspan, 1) });
            td.textContent = String(cell?.text ?? cell ?? '');
            tr.append(td);
          }
          table.append(tr);
        }
        node.append(table);
        break;
      }
      case 'chart': {
        node = el('div', { ...common, class: `${common.class} cls-el-chart`, style: absoluteStyle(item) });
        try {
          node.append(chartSvg(item));
        } catch {
          node.append(el('span', { class: 'cls-image-fallback' }, '图表数据不可用'));
        }
        break;
      }
      case 'code': {
        node = el('div', { ...common, class: `${common.class} cls-el-code`, style: absoluteStyle(item, 'overflow:auto') });
        const pre = el('pre', { class: 'cls-slide-code' });
        const code = el('code');
        code.textContent = String(item.code || '');
        pre.append(code);
        node.append(pre);
        break;
      }
      default:
        return null;
    }
    return node;
  }

  function buildOverlay() {
    overlay = el('div', { class: 'cls-editor-selection', hidden: true, 'aria-hidden': 'true' });
    overlay.append(el('span', { class: 'cls-editor-selection-label' }));
    for (const dir of HANDLE_DIRS) {
      overlay.append(el('span', { class: `cls-editor-handle handle-${dir}`, 'data-handle': dir }));
    }
    return overlay;
  }

  function positionOverlay() {
    if (!overlay) return;
    const item = selectedItem();
    if (!item) {
      overlay.hidden = true;
      return;
    }
    const box = elementBox(item);
    overlay.hidden = false;
    overlay.dataset.elementType = String(item.type || '');
    overlay.style.left = `${round2(box.left)}px`;
    overlay.style.top = `${round2(box.top)}px`;
    overlay.style.width = `${round2(Math.max(4, box.width))}px`;
    overlay.style.height = `${round2(Math.max(4, box.height))}px`;
    const label = overlay.querySelector('.cls-editor-selection-label');
    if (label) label.textContent = `${typeLabelOf(item.type)} · ${Math.round(box.width)}×${Math.round(box.height)}`;
    overlay.classList.toggle('is-line', item.type === 'line');
  }

  function renderCanvas() {
    canvas.replaceChildren();
    nodeById.clear();
    for (const item of state.elements) {
      const node = renderElementNode(item);
      if (!node) continue;
      nodeById.set(String(item.id), node);
      canvas.append(node);
    }
    if (!overlay) buildOverlay();
    canvas.append(overlay);
    updateSelectionClasses();
    positionOverlay();
    applyStageScale();
  }

  function updateSelectionClasses() {
    for (const [id, node] of nodeById) {
      node.classList.toggle('is-selected', String(id) === String(state.selectedId));
    }
  }

  function refreshElementNode(item) {
    if (!item) return;
    const id = String(item.id);
    const existing = nodeById.get(id);
    const fresh = renderElementNode(item);
    if (!fresh) return;
    if (existing) existing.replaceWith(fresh);
    else if (overlay && overlay.parentNode === canvas) canvas.insertBefore(fresh, overlay);
    else canvas.append(fresh);
    nodeById.set(id, fresh);
    updateSelectionClasses();
    positionOverlay();
  }

  function refreshAllGeometry() {
    for (const item of state.elements) {
      const node = nodeById.get(String(item.id));
      if (!node) continue;
      applyNodeGeometry(node, item);
    }
    positionOverlay();
  }

  function applyNodeGeometry(node, item) {
    if (item.type === 'line') {
      const bbox = lineBBox(item);
      const pad = 6;
      node.style.left = `${round2(bbox.left - pad)}px`;
      node.style.top = `${round2(bbox.top - pad)}px`;
      node.style.width = `${Math.max(12, bbox.width + pad * 2)}px`;
      node.style.height = `${Math.max(12, bbox.height + pad * 2)}px`;
      const svg = node.querySelector('svg');
      if (svg) {
        const width = Math.max(12, bbox.width + pad * 2);
        const height = Math.max(12, bbox.height + pad * 2);
        svg.setAttribute('width', width);
        svg.setAttribute('height', height);
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        const x1 = bbox.x1 - (bbox.left - pad);
        const y1 = bbox.y1 - (bbox.top - pad);
        const x2 = bbox.x2 - (bbox.left - pad);
        const y2 = bbox.y2 - (bbox.top - pad);
        const line = svg.querySelector('line');
        if (line) {
          line.setAttribute('x1', x1);
          line.setAttribute('y1', y1);
          line.setAttribute('x2', x2);
          line.setAttribute('y2', y2);
        }
      }
      return;
    }
    node.style.left = `${num(item.left)}px`;
    node.style.top = `${num(item.top)}px`;
    node.style.width = `${Math.max(0, num(item.width))}px`;
    node.style.height = `${Math.max(0, num(item.height))}px`;
    node.style.transform = num(item.rotate) ? `rotate(${num(item.rotate)}deg)` : '';
  }

  // -------------------------------------------------------------------------
  // 选择 / 拖拽 / 缩放
  // -------------------------------------------------------------------------
  function selectElement(id, { focus = false } = {}) {
    const next = id && state.elements.some(item => String(item.id) === String(id)) ? String(id) : null;
    state.selectedId = next;
    updateSelectionClasses();
    positionOverlay();
    renderSidePanels();
    if (focus) root.focus({ preventScroll: true });
  }

  function toCanvasPoint(event) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = rect.width / SLIDE_WIDTH || 1;
    const scaleY = rect.height / SLIDE_HEIGHT || 1;
    return {
      x: (event.clientX - rect.left) / scaleX,
      y: (event.clientY - rect.top) / scaleY,
    };
  }

  function onCanvasPointerDown(event) {
    if (event.button !== undefined && event.button !== 0 && event.pointerType === 'mouse') return;
    const handle = event.target.closest?.('[data-handle]');
    if (handle) {
      // 缩放手柄在选中框（overlay）里，不属于元素节点：直接用当前选中元素开缩放手势。
      const item = selectedItem();
      if (!item || item.type === 'line') return;
      drag = {
        id: String(item.id),
        mode: 'resize',
        dir: String(handle.dataset.handle || ''),
        start: toCanvasPoint(event),
        box: elementBox(item),
        origLeft: num(item.left),
        origTop: num(item.top),
        snapshot: snapshotElements(),
        moved: false,
        pointerId: event.pointerId,
      };
      try { canvas.setPointerCapture(event.pointerId); } catch { /* 忽略不支持捕获的浏览器 */ }
      event.preventDefault();
      return;
    }
    const elementNode = event.target.closest?.('.cls-editor-element');
    if (!elementNode) {
      selectElement(null);
      return;
    }
    const item = state.elements.find(entry => String(entry.id) === String(elementNode.dataset.elementId));
    if (!item) return;
    selectElement(item.id);
    const box = elementBox(item);
    drag = {
      id: String(item.id),
      mode: 'move',
      dir: '',
      start: toCanvasPoint(event),
      box,
      origLeft: num(item.left),
      origTop: num(item.top),
      snapshot: snapshotElements(),
      moved: false,
      pointerId: event.pointerId,
    };
    try { canvas.setPointerCapture(event.pointerId); } catch { /* 忽略不支持捕获的浏览器 */ }
    event.preventDefault();
  }

  function resizeBox(box, dir, dx, dy) {
    let left = box.left;
    let top = box.top;
    let right = box.left + box.width;
    let bottom = box.top + box.height;
    if (dir.includes('w')) left = clamp(left + dx, 0, right - MIN_SIZE);
    if (dir.includes('e')) right = clamp(right + dx, left + MIN_SIZE, SLIDE_WIDTH);
    if (dir.includes('n')) top = clamp(top + dy, 0, bottom - MIN_SIZE);
    if (dir.includes('s')) bottom = clamp(bottom + dy, top + MIN_SIZE, SLIDE_HEIGHT);
    return {
      left: round2(left),
      top: round2(top),
      width: round2(Math.max(MIN_SIZE, right - left)),
      height: round2(Math.max(MIN_SIZE, bottom - top)),
    };
  }

  function onCanvasPointerMove(event) {
    if (!drag) return;
    const point = toCanvasPoint(event);
    const dx = point.x - drag.start.x;
    const dy = point.y - drag.start.y;
    const item = state.elements.find(entry => String(entry.id) === drag.id);
    if (!item) return;
    if (Math.abs(dx) > 0.2 || Math.abs(dy) > 0.2) drag.moved = true;
    if (!drag.moved) return;
    if (drag.mode === 'move') {
      if (item.type === 'line') {
        item.left = round2(clamp(drag.origLeft + dx, -SLIDE_WIDTH, SLIDE_WIDTH));
        item.top = round2(clamp(drag.origTop + dy, -SLIDE_HEIGHT, SLIDE_HEIGHT));
      } else {
        item.left = round2(clamp(drag.box.left + dx, 0, SLIDE_WIDTH - drag.box.width));
        item.top = round2(clamp(drag.box.top + dy, 0, SLIDE_HEIGHT - drag.box.height));
      }
    } else {
      const box = resizeBox(drag.box, drag.dir, dx, dy);
      item.left = box.left;
      item.top = box.top;
      item.width = box.width;
      item.height = box.height;
    }
    const node = nodeById.get(String(item.id));
    if (node) applyNodeGeometry(node, item);
    positionOverlay();
    state.dirty = true;
    state.revision += 1;
    if (state.saveState !== 'saving' && state.saveState !== 'dirty') setSaveState('dirty');
    scheduleAutoSave();
  }

  function finishDrag(event) {
    if (!drag) return;
    const { snapshot, moved } = drag;
    const pointerId = drag.pointerId;
    drag = null;
    try { canvas.releasePointerCapture(pointerId); } catch { /* 忽略 */ }
    if (moved) {
      recordHistory(snapshot);
      refreshAllGeometry();
      renderSidePanels();
      touch();
    } else if (event?.type === 'pointerup') {
      renderSidePanels();
    }
  }

  // -------------------------------------------------------------------------
  // 历史
  // -------------------------------------------------------------------------
  function undo() {
    if (!state.history.length) return;
    state.future.push(snapshotElements());
    if (state.future.length > HISTORY_LIMIT) state.future.shift();
    state.elements = JSON.parse(state.history.pop());
    if (!state.elements.some(item => String(item.id) === String(state.selectedId))) state.selectedId = null;
    renderCanvas();
    renderSidePanels();
    touch();
  }

  function redo() {
    if (!state.future.length) return;
    state.history.push(snapshotElements());
    if (state.history.length > HISTORY_LIMIT) state.history.shift();
    state.elements = JSON.parse(state.future.pop());
    if (!state.elements.some(item => String(item.id) === String(state.selectedId))) state.selectedId = null;
    renderCanvas();
    renderSidePanels();
    touch();
  }

  function copySelected() {
    const item = selectedItem();
    if (!item) return;
    state.clipboard = clone(item);
    toast(`已复制${typeLabelOf(item.type)}元素`);
  }

  function pasteClipboard() {
    if (!state.clipboard) {
      toast('剪贴板为空：先选中元素后按 Ctrl/⌘+C', true);
      return;
    }
    const pasted = clone(state.clipboard);
    if (!pasted) return;
    pasted.id = nextElementId(pasted.type);
    if (pasted.type === 'line') {
      pasted.left = round2(clamp(num(pasted.left) + 20, -SLIDE_WIDTH, SLIDE_WIDTH));
      pasted.top = round2(clamp(num(pasted.top) + 20, -SLIDE_HEIGHT, SLIDE_HEIGHT));
    } else {
      const box = elementBox(pasted);
      pasted.left = round2(clamp(box.left + 20, 0, Math.max(0, SLIDE_WIDTH - box.width)));
      pasted.top = round2(clamp(box.top + 20, 0, Math.max(0, SLIDE_HEIGHT - box.height)));
    }
    applyMutation(() => { state.elements.push(pasted); });
    state.selectedId = String(pasted.id);
    renderCanvas();
    renderSidePanels();
  }

  function nextElementId(type) {
    const prefix = String(type || 'elem');
    const used = new Set(state.elements.map(item => String(item.id || '')));
    for (let index = 1; index <= 999; index += 1) {
      const candidate = `${prefix}_${String(index).padStart(3, '0')}`;
      if (!used.has(candidate)) return candidate;
    }
    return `${prefix}_${Date.now().toString(36)}`;
  }

  function deleteSelected() {
    const item = selectedItem();
    if (!item) return;
    if (state.elements.length <= 1) {
      toast('幻灯片至少需要保留 1 个元素', true);
      return;
    }
    applyMutation(() => {
      state.elements = state.elements.filter(entry => String(entry.id) !== String(item.id));
    });
    state.selectedId = null;
    renderCanvas();
    renderSidePanels();
  }

  function nudge(dx, dy) {
    const item = selectedItem();
    if (!item) return;
    applyMutation(() => {
      if (item.type === 'line') {
        item.left = round2(clamp(num(item.left) + dx, -SLIDE_WIDTH, SLIDE_WIDTH));
        item.top = round2(clamp(num(item.top) + dy, -SLIDE_HEIGHT, SLIDE_HEIGHT));
      } else {
        const box = elementBox(item);
        item.left = round2(clamp(box.left + dx, 0, Math.max(0, SLIDE_WIDTH - box.width)));
        item.top = round2(clamp(box.top + dy, 0, Math.max(0, SLIDE_HEIGHT - box.height)));
      }
    });
    refreshElementNode(item);
    renderSidePanels();
  }

  function addElement(type) {
    let item = null;
    if (type === 'text') {
      item = {
        id: nextElementId('text'), type: 'text', left: 340, top: 230, width: 320, height: 80, rotate: 0,
        fontSize: 24, color: '#333333', defaultColor: '#333333', align: 'left', lineHeight: 1.5,
      };
      setTextProps(item, { text: '双击右侧文本区编辑', fontSize: 24, color: '#333333', align: 'left', bold: false });
    } else if (type === 'shape') {
      item = {
        id: nextElementId('shape'), type: 'shape', left: 360, top: 210, width: 260, height: 160, rotate: 0,
        path: 'M 0 0 L 1 0 L 1 1 L 0 1 Z', viewBox: [1, 1], fill: '#5b9bd5', fixedRatio: false,
      };
    } else if (type === 'line') {
      item = {
        id: nextElementId('line'), type: 'line', left: 320, top: 300, width: 2,
        start: [0, 0], end: [320, 0], style: 'solid', color: '#333333', points: ['', ''],
      };
    } else if (type === 'latex') {
      item = {
        id: nextElementId('latex'), type: 'latex', left: 330, top: 250, width: 340, height: 90, rotate: 0,
        latex: 'E = mc^2', color: '#000000', align: 'center',
      };
    } else if (type === 'image') {
      const assets = collectImageAssets();
      if (!assets.length) {
        toast('课程还没有图片素材：可先返回生成课堂并开启 AI 配图，或从其他场景复制图片元素', true);
        return;
      }
      item = {
        id: nextElementId('image'), type: 'image', left: 360, top: 180, width: 280, height: 200, rotate: 0,
        src: assets[0].src, alt: assets[0].alt || '幻灯片配图', fixedRatio: true,
      };
    }
    if (!item) return;
    applyMutation(() => { state.elements.push(item); });
    state.selectedId = String(item.id);
    renderCanvas();
    renderSidePanels();
  }

  function moveLayer(direction) {
    const item = selectedItem();
    if (!item) return;
    const index = state.elements.findIndex(entry => String(entry.id) === String(item.id));
    if (index < 0) return;
    let target = index;
    if (direction === 'up') target = Math.min(state.elements.length - 1, index + 1);
    else if (direction === 'down') target = Math.max(0, index - 1);
    else if (direction === 'top') target = state.elements.length - 1;
    else if (direction === 'bottom') target = 0;
    if (target === index) return;
    applyMutation(() => {
      const [entry] = state.elements.splice(index, 1);
      state.elements.splice(target, 0, entry);
    });
    renderCanvas();
    renderSidePanels();
  }

  // -------------------------------------------------------------------------
  // 保存
  // -------------------------------------------------------------------------
  function buildContent() {
    return {
      type: 'slide',
      schemaVersion: 1,
      canvas: {
        width: SLIDE_WIDTH,
        height: SLIDE_HEIGHT,
        theme: normalizeTheme(state.theme),
        elements: clone(state.elements) || [],
      },
    };
  }

  function applyServerContent(content) {
    const canvasData = content && typeof content === 'object' && content.canvas && typeof content.canvas === 'object'
      ? content.canvas : {};
    state.elements = Array.isArray(canvasData.elements) ? (clone(canvasData.elements) || []) : [];
    if (canvasData.theme) state.theme = normalizeTheme(canvasData.theme);
    if (state.selectedId && !state.elements.some(item => String(item.id) === String(state.selectedId))) {
      state.selectedId = null;
    }
    renderCanvas();
    renderSidePanels();
    updateToolbar();
  }

  async function performSave({ manual = false, silent = false } = {}) {
    const targetScene = state.sceneId;
    const targetVersion = state.version;
    const revisionAtStart = state.revision;
    const content = buildContent();
    setSaveState('saving');
    updateToolbar();
    try {
      const response = await fetch(endpoint(), {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ version: targetVersion, content }),
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) {
        setSaveState('error');
        toast('登录已过期，请刷新页面重新登录后再保存。', true);
        return false;
      }
      if (response.status === 409) {
        const detail = payload && typeof payload.detail === 'object' && payload.detail ? payload.detail : {};
        state.version = Number(detail.version) || state.version;
        setSaveState('conflict');
        openConflictDialog(detail, targetVersion);
        return false;
      }
      if (!response.ok) throw new Error(detailText(payload.detail) || `保存失败（${response.status}）`);
      if (state.destroyed || state.sceneId !== targetScene) return false;
      state.version = Number(payload.scene?.version) || targetVersion + 1;
      if (state.revision === revisionAtStart) {
        // 常规情况下保留本地编辑模型，避免重建属性面板抢走输入焦点；仅当服务端
        // 归一化丢弃/重排了元素（例如非法图片被移除）时才用服务端结果回写。
        const serverCanvas = payload.scene?.content?.canvas || {};
        const serverElements = Array.isArray(serverCanvas.elements) ? serverCanvas.elements : [];
        const localIds = state.elements.map(item => String(item.id)).join('|');
        const serverIds = serverElements.map(item => String(item.id)).join('|');
        if (localIds !== serverIds) applyServerContent(payload.scene?.content || {});
        state.dirty = false;
        setSaveState('saved');
        if (!silent) {
          toast(manual ? `已保存（版本 v${state.version}）` : `已自动保存（版本 v${state.version}）`);
        }
      } else {
        setSaveState('dirty');
        scheduleAutoSave();
      }
      const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter(Boolean) : [];
      if (warnings.length && !silent) toast(`保存时已自动修正：${warnings[0]}`, true);
      return true;
    } catch (error) {
      if (state.destroyed) return false;
      setSaveState('error');
      if (!silent) toast(error.message || '保存失败，请重试。', true);
      return false;
    } finally {
      updateToolbar();
    }
  }

  async function save({ manual = false, silent = false } = {}) {
    if (state.destroyed) return false;
    if (savePromise) {
      try { await savePromise; } catch { /* 上一轮保存结果由它自己提示 */ }
    }
    if (!state.dirty) {
      if (manual && !silent) toast(`没有未保存的修改（当前版本 v${state.version}）`);
      return true;
    }
    savePromise = performSave({ manual, silent });
    try {
      return await savePromise;
    } finally {
      savePromise = null;
    }
  }

  function openConflictDialog(detail, localVersion) {
    closeDialogs();
    const currentVersion = Number(detail?.version) || state.version;
    const baseVersion = Number(localVersion) || Math.max(1, currentVersion - 1);
    conflictDialog = el('dialog', { class: 'cls-editor-dialog cls-editor-conflict' },
      el('div', { class: 'cls-editor-dialog-head' },
        el('h3', {}, '版本冲突：幻灯片已被修改'),
        button('关闭', () => conflictDialog?.close(), 'ghost small', 'close', { 'aria-label': '关闭冲突提示' })),
      el('p', {}, `服务端当前版本为 v${currentVersion}，你的编辑基于 v${baseVersion}。`),
      el('p', { class: 'help' },
        '可能是在其他页面/窗口改过这张幻灯片。可「重新加载最新内容」放弃本地修改，'
        + '或「覆盖保存」以服务端最新版本为基线提交当前编辑。'),
      el('div', { class: 'row cls-editor-dialog-actions' },
        button('重新加载最新内容', async () => {
          conflictDialog.close();
          await loadScene(state.sceneId);
          toast('已重新加载服务端最新内容');
        }, 'secondary', 'repeat'),
        button('覆盖保存', async () => {
          conflictDialog.close();
          state.version = currentVersion;
          state.dirty = true;
          await save({ manual: true });
        }, '', 'check'),
        button('取消', () => conflictDialog?.close(), 'ghost')));
    document.body.append(conflictDialog);
    conflictDialog.addEventListener('close', () => {
      conflictDialog?.remove();
      conflictDialog = null;
    });
    conflictDialog.showModal();
  }

  function closeDialogs() {
    if (conflictDialog) {
      try { conflictDialog.close(); } catch { /* 忽略 */ }
      conflictDialog = null;
    }
  }

  // -------------------------------------------------------------------------
  // 场景切换 / 离开确认
  // -------------------------------------------------------------------------
  async function requestLeave(action) {
    if (state.dirty) {
      const saved = await save({ manual: true, silent: true });
      if (!saved && !window.confirm('有未保存的幻灯片修改，离开将丢失这些修改。是否继续？')) return;
    }
    action();
  }

  async function requestSceneSwitch(targetId) {
    const nextId = Number(targetId) || 0;
    if (!nextId || nextId === state.sceneId) {
      sceneSelect.value = String(state.sceneId);
      return;
    }
    if (state.dirty) {
      const saved = await save({ manual: true, silent: true });
      if (!saved && !window.confirm('保存未完成，切换场景会丢失未保存修改。是否继续？')) {
        sceneSelect.value = String(state.sceneId);
        return;
      }
    }
    go('classroom', null, { course: courseId, view: 'edit', scene: nextId });
  }

  async function duplicateScene() {
    if (!state.sceneId) return;
    if (state.dirty) {
      const saved = await save({ manual: true, silent: true });
      if (!saved) {
        toast('请先处理未保存的修改（或版本冲突），再复制场景。', true);
        return;
      }
    }
    try {
      const data = await api(`/classroom/courses/${courseId}/scenes/${state.sceneId}/duplicate`, { method: 'POST' });
      const newId = Number(data?.scene?.id) || 0;
      toast(`已复制场景：${data?.scene?.title || ''}`);
      if (newId) go('classroom', null, { course: courseId, view: 'edit', scene: newId });
    } catch (error) {
      toast(error.message || '复制场景失败，请重试。', true);
    }
  }

  function onBeforeUnload(event) {
    if (!state.dirty || state.destroyed) return '';
    event.preventDefault();
    event.returnValue = '';
    return '';
  }

  function onDocumentClickCapture(event) {
    if (state.destroyed || !state.dirty) return;
    if (root.contains(event.target)) return;
    const target = event.target.closest?.('.nav-item, a[href^="#/"]');
    if (!target) return;
    if (window.confirm('有未保存的幻灯片修改，离开将丢失这些修改。是否继续？')) return;
    event.preventDefault();
    event.stopPropagation();
  }

  // -------------------------------------------------------------------------
  // 键盘
  // -------------------------------------------------------------------------
  function onKeyDown(event) {
    if (state.destroyed) return;
    const target = event.target;
    const tag = String(target?.tagName || '').toLowerCase();
    const editing = tag === 'input' || tag === 'textarea' || tag === 'select' || target?.isContentEditable;
    const mod = event.ctrlKey || event.metaKey;
    if (mod && (event.key === 's' || event.key === 'S')) {
      event.preventDefault();
      save({ manual: true });
      return;
    }
    if (editing) return;
    if (mod && (event.key === 'z' || event.key === 'Z')) {
      event.preventDefault();
      if (event.shiftKey) redo(); else undo();
      return;
    }
    if (mod && (event.key === 'y' || event.key === 'Y')) {
      event.preventDefault();
      redo();
      return;
    }
    if (mod && (event.key === 'c' || event.key === 'C')) {
      event.preventDefault();
      copySelected();
      return;
    }
    if (mod && (event.key === 'v' || event.key === 'V')) {
      event.preventDefault();
      pasteClipboard();
      return;
    }
    if (event.key === 'Escape') {
      selectElement(null);
      return;
    }
    if (!state.selectedId) return;
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault();
      deleteSelected();
      return;
    }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[event.key]) {
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      nudge(arrows[event.key][0] * step, arrows[event.key][1] * step);
    }
  }

  // -------------------------------------------------------------------------
  // 属性面板
  // -------------------------------------------------------------------------
  function compactField(label, control, hint = null) {
    return el('label', { class: 'cls-editor-field' },
      el('span', {}, label),
      control,
      hint ? el('span', { class: 'help cls-editor-field-hint' }, hint) : null);
  }

  function colorControl(value, onChange) {
    const input = el('input', {
      type: 'color',
      class: 'cls-editor-color',
      value: HEX_COLOR_RE.test(String(value || '')) ? value : '#333333',
    });
    const text = el('span', { class: 'cls-editor-color-value' }, input.value);
    input.addEventListener('input', () => {
      text.textContent = input.value;
      onChange(input.value);
    });
    return el('div', { class: 'cls-editor-color-wrap' }, input, text);
  }

  function selectControl(options, value, onChange) {
    const control = el('select', { class: 'cls-editor-select' });
    for (const option of options) {
      const item = typeof option === 'object' ? option : { value: option, label: option };
      control.append(el('option', { value: String(item.value) }, item.label));
    }
    control.value = String(value);
    control.addEventListener('change', () => onChange(control.value));
    return control;
  }

  function numberControl(value, { min, max, step = 1, onChange, disabled = false }) {
    const control = el('input', {
      type: 'number',
      class: 'input cls-editor-number',
      value: String(round2(value)),
      min: String(min),
      max: String(max),
      step: String(step),
      disabled,
    });
    control.addEventListener('input', () => {
      const number = Number(control.value);
      if (!Number.isFinite(number)) return;
      const next = clamp(number, min, max);
      onChange(next);
      // 输入越界时把钳制后的值回显到输入框，避免「显示 5000 实际保存 900」的错觉。
      if (String(round2(next)) !== String(number)) control.value = String(round2(next));
    });
    return control;
  }

  function geometrySection(item) {
    const box = elementBox(item);
    const start = Array.isArray(item.start) ? item.start : [0, 0];
    const startX = num(start[0]);
    const startY = num(start[1]);
    const maxX = item.type === 'line' ? SLIDE_WIDTH : Math.max(0, SLIDE_WIDTH - box.width);
    const maxY = item.type === 'line' ? SLIDE_HEIGHT : Math.max(0, SLIDE_HEIGHT - box.height);
    const fields = [
      compactField('X', numberControl(box.left, {
        min: item.type === 'line' ? -SLIDE_WIDTH : 0, max: maxX,
        onChange: (value) => {
          applyMutation(() => {
            // 线条的 X/Y 展示的是端点包围盒；内部 left/top 需要减去 start 偏移。
            item.left = item.type === 'line'
              ? round2(clamp(value - startX, -SLIDE_WIDTH, SLIDE_WIDTH))
              : round2(clamp(value, 0, SLIDE_WIDTH - num(item.width)));
          });
          refreshElementNode(item);
        },
      })),
      compactField('Y', numberControl(box.top, {
        min: item.type === 'line' ? -SLIDE_HEIGHT : 0, max: maxY,
        onChange: (value) => {
          applyMutation(() => {
            item.top = item.type === 'line'
              ? round2(clamp(value - startY, -SLIDE_HEIGHT, SLIDE_HEIGHT))
              : round2(clamp(value, 0, SLIDE_HEIGHT - num(item.height)));
          });
          refreshElementNode(item);
        },
      })),
    ];
    if (item.type !== 'line') {
      fields.push(
        compactField('宽', numberControl(num(item.width), {
          min: MIN_SIZE, max: SLIDE_WIDTH, step: 1,
          onChange: (value) => {
            applyMutation(() => { item.width = round2(clamp(value, MIN_SIZE, SLIDE_WIDTH - num(item.left))); });
            refreshElementNode(item);
          },
        })),
        compactField('高', numberControl(num(item.height), {
          min: MIN_SIZE, max: SLIDE_HEIGHT, step: 1,
          onChange: (value) => {
            applyMutation(() => { item.height = round2(clamp(value, MIN_SIZE, SLIDE_HEIGHT - num(item.top))); });
            refreshElementNode(item);
          },
        })),
      );
    }
    return el('div', { class: 'cls-editor-geometry' }, fields);
  }

  function layerButtons() {
    return el('div', { class: 'cls-editor-layer-buttons' },
      button('上移一层', () => moveLayer('up'), 'ghost small', 'chevron'),
      button('下移一层', () => moveLayer('down'), 'ghost small', 'chevron'),
      button('置顶', () => moveLayer('top'), 'secondary small', 'layers'),
      button('置底', () => moveLayer('bottom'), 'secondary small', 'layers'));
  }

  function textSection(item) {
    const props = textPropsOf(item);
    const textarea = el('textarea', { class: 'cls-editor-textarea', rows: 5, value: props.text, spellcheck: 'false' });
    textarea.addEventListener('input', () => {
      applyMutation(() => setTextProps(item, { text: textarea.value }));
      refreshElementNode(item);
    });
    const sizeInput = numberControl(props.fontSize, {
      min: 10, max: 72,
      onChange: (value) => {
        applyMutation(() => setTextProps(item, { fontSize: value }));
        refreshElementNode(item);
      },
    });
    const alignSelect = selectControl([
      { value: 'left', label: '左对齐' },
      { value: 'center', label: '居中' },
      { value: 'right', label: '右对齐' },
    ], props.align, (value) => {
      applyMutation(() => setTextProps(item, { align: value }));
      refreshElementNode(item);
    });
    const boldCheck = el('input', { type: 'checkbox', checked: props.bold });
    boldCheck.addEventListener('change', () => {
      applyMutation(() => setTextProps(item, { bold: boldCheck.checked }));
      refreshElementNode(item);
    });
    return el('div', { class: 'cls-editor-section' },
      el('h4', {}, '文字'),
      compactField('文字内容（每行一个段落，保存时转义为简单 <p>）', textarea),
      el('div', { class: 'cls-editor-grid-2' },
        compactField('字号', sizeInput),
        compactField('颜色', colorControl(props.color, (value) => {
          applyMutation(() => setTextProps(item, { color: value }));
          refreshElementNode(item);
        }))),
      el('div', { class: 'cls-editor-grid-2' },
        compactField('对齐', alignSelect),
        el('label', { class: 'cls-editor-check' }, boldCheck, el('span', {}, '加粗'))));
  }

  function shapeSection(item) {
    return el('div', { class: 'cls-editor-section' },
      el('h4', {}, '形状'),
      compactField('底色', colorControl(item.fill, (value) => {
        applyMutation(() => { item.fill = value; });
        refreshElementNode(item);
      })),
      el('p', { class: 'help cls-editor-note' },
        '描边：当前幻灯片 DSL 的形状元素只保留底色（播放器/导出同源），暂不支持形状描边；'
        + '需要描边效果可改用「线条」元素或生成时由版式模板控制。'));
  }

  function lineSection(item) {
    const points = Array.isArray(item.points) ? item.points : ['', ''];
    return el('div', { class: 'cls-editor-section' },
      el('h4', {}, '线条'),
      el('div', { class: 'cls-editor-grid-2' },
        compactField('颜色', colorControl(item.color, (value) => {
          applyMutation(() => { item.color = value; });
          refreshElementNode(item);
        })),
        compactField('粗细（1–8px）', numberControl(num(item.width, 2), {
          min: 1, max: 8, step: 1,
          onChange: (value) => {
            applyMutation(() => { item.width = round2(value); });
            refreshElementNode(item);
          },
        }))),
      el('div', { class: 'cls-editor-grid-2' },
        compactField('线型', selectControl([
          { value: 'solid', label: '实线' }, { value: 'dashed', label: '虚线' }, { value: 'dotted', label: '点线' },
        ], item.style || 'solid', (value) => {
          applyMutation(() => { item.style = value; });
          refreshElementNode(item);
        })),
        compactField('起点端点', selectControl([
          { value: '', label: '无' }, { value: 'arrow', label: '箭头' }, { value: 'dot', label: '圆点' },
        ], points[0] || '', (value) => {
          applyMutation(() => { item.points = [value, points[1] || '']; });
          refreshElementNode(item);
        }))),
      compactField('终点端点', selectControl([
        { value: '', label: '无' }, { value: 'arrow', label: '箭头' }, { value: 'dot', label: '圆点' },
      ], points[1] || '', (value) => {
        applyMutation(() => { item.points = [points[0] || '', value]; });
        refreshElementNode(item);
      })));
  }

  function latexSection(item) {
    const textarea = el('textarea', { class: 'cls-editor-textarea cls-editor-latex', rows: 3, value: String(item.latex || ''), spellcheck: 'false' });
    textarea.addEventListener('input', () => {
      applyMutation(() => { item.latex = textarea.value.slice(0, 2000); });
      refreshElementNode(item);
    });
    const visualEdit = formulaEditorButton(textarea, {
      title: '可视化公式编辑',
      wrap: false,
      variant: 'secondary small',
    });
    visualEdit.classList.add('cls-editor-latex-visual');
    return el('div', { class: 'cls-editor-section' },
      el('h4', {}, '公式（LaTeX）'),
      compactField('LaTeX 文本', el('div', { class: 'cls-editor-latex-field' },
        textarea, visualEdit)),
      el('div', { class: 'cls-editor-grid-2' },
        compactField('颜色', colorControl(item.color, (value) => {
          applyMutation(() => { item.color = value; });
          refreshElementNode(item);
        })),
        compactField('对齐', selectControl([
          { value: 'left', label: '左对齐' }, { value: 'center', label: '居中' }, { value: 'right', label: '右对齐' },
        ], item.align || 'center', (value) => {
          applyMutation(() => { item.align = value; });
          refreshElementNode(item);
        }))));
  }

  function collectImageAssets() {
    const seen = new Map();
    const addFrom = (elementList) => {
      for (const item of elementList || []) {
        if (!item || item.type !== 'image' || !item.src) continue;
        const src = String(item.src);
        if (!seen.has(src)) seen.set(src, { src, alt: String(item.alt || '') });
      }
    };
    for (const scene of state.scenes) addFrom(scene?.content?.canvas?.elements);
    addFrom(state.elements);
    return [...seen.values()];
  }

  function imageSection(item) {
    const assets = collectImageAssets();
    const previewSrc = resolveAssetSrc(item.src);
    const preview = previewSrc
      ? el('img', { class: 'cls-editor-image-preview', src: previewSrc, alt: String(item.alt || '图片预览') })
      : el('span', { class: 'help' }, '尚未设置图片素材');
    const options = assets.map(asset => ({ value: asset.src, label: asset.src }));
    if (options.length && !options.some(option => option.value === String(item.src || ''))) {
      options.unshift({ value: String(item.src || ''), label: String(item.src || '（当前素材）') });
    }
    const assetSelect = options.length
      ? selectControl(options, String(item.src || ''), (value) => {
        applyMutation(() => { item.src = value; });
        refreshElementNode(item);
        renderSidePanels();
      })
      : el('p', { class: 'help' }, '课程中还没有可选的图片素材：请先生成带配图的课堂，或从其他场景复制图片元素。');
    const altInput = el('input', { class: 'input', value: String(item.alt || ''), maxlength: 120, placeholder: '图片替代文字（无障碍）' });
    altInput.addEventListener('input', () => {
      applyMutation(() => { item.alt = altInput.value.slice(0, 120); });
    });
    return el('div', { class: 'cls-editor-section' },
      el('h4', {}, '图片'),
      preview,
      compactField('替换素材（课程已生成图片）', assetSelect),
      compactField('替代文字', altInput));
  }

  function readonlySection(type) {
    return el('div', { class: 'cls-editor-section' },
      el('p', { class: 'help cls-editor-note' },
        `${typeLabelOf(type)}元素暂不支持内容编辑，可移动 / 缩放 / 调整层级 / 删除；`
        + '如需改内容可回到课程生成或复制为新场景。'));
  }

  function renderPropertyPanel(host) {
    const item = selectedItem();
    if (!item) {
      host.append(
        el('div', { class: 'cls-editor-empty-panel' },
          el('p', {}, '在画布或「元素」列表中选择一个元素查看/编辑属性。'),
          el('p', { class: 'help' }, '也可以直接新增元素：'),
          el('div', { class: 'cls-editor-add-buttons' },
            button('文本', () => addElement('text'), 'secondary small', 'notes'),
            button('形状', () => addElement('shape'), 'secondary small', 'target'),
            button('线条', () => addElement('line'), 'secondary small', 'chevron'),
            button('公式', () => addElement('latex'), 'secondary small', 'atom'),
            button('图片', () => addElement('image'), 'secondary small', 'scan'))));
      return;
    }
    const head = el('div', { class: 'cls-editor-element-head' },
      el('div', {},
        el('strong', {}, typeLabelOf(item.type)),
        el('span', { class: 'help cls-editor-element-id' }, String(item.id || ''))),
      el('div', { class: 'row' },
        button('复制', copySelected, 'ghost small', 'copy'),
        button('删除', deleteSelected, 'ghost small', 'trash')));
    host.append(head, geometrySection(item), layerButtons());
    if (item.type === 'text') host.append(textSection(item));
    else if (item.type === 'shape') host.append(shapeSection(item));
    else if (item.type === 'line') host.append(lineSection(item));
    else if (item.type === 'latex') host.append(latexSection(item));
    else if (item.type === 'image') host.append(imageSection(item));
    else host.append(readonlySection(item.type));
  }

  function renderElementList(host) {
    const list = el('ol', { class: 'cls-editor-element-list' });
    for (const item of [...state.elements].reverse()) {
      const selected = String(item.id) === String(state.selectedId);
      const row = el('li', {},
        el('button', {
          type: 'button',
          class: `cls-editor-element-item${selected ? ' is-selected' : ''}`,
          onClick: () => {
            selectElement(item.id);
            state.sideTab = 'props';
            renderTabs();
            renderSidePanels();
          },
        },
        el('span', { class: 'cls-editor-element-type' }, typeLabelOf(item.type)),
        el('span', { class: 'cls-editor-element-digest' }, elementDigest(item) || item.id)));
      list.append(row);
    }
    const addRow = el('div', { class: 'cls-editor-add-buttons' },
      button('新增文本', () => addElement('text'), 'secondary small', 'plus'),
      button('新增形状', () => addElement('shape'), 'secondary small', 'plus'),
      button('新增线条', () => addElement('line'), 'secondary small', 'plus'),
      button('新增公式', () => addElement('latex'), 'secondary small', 'plus'),
      button('新增图片', () => addElement('image'), 'secondary small', 'plus'));
    host.append(
      el('p', { class: 'help cls-editor-list-hint' }, `共 ${state.elements.length} 个元素（列表按自顶层到最底层排序）。`),
      list,
      addRow);
  }

  function renderTabs() {
    propsTab.classList.toggle('active', state.sideTab === 'props');
    listTab.classList.toggle('active', state.sideTab === 'list');
    propsTab.setAttribute('aria-selected', state.sideTab === 'props' ? 'true' : 'false');
    listTab.setAttribute('aria-selected', state.sideTab === 'list' ? 'true' : 'false');
  }

  function renderSidePanels() {
    renderTabs();
    panelHost.replaceChildren();
    if (state.sideTab === 'list') renderElementList(panelHost);
    else renderPropertyPanel(panelHost);
  }

  // -------------------------------------------------------------------------
  // 加载 / 空态
  // -------------------------------------------------------------------------
  function showStageMessage(node) {
    stage.hidden = Boolean(node);
    if (node) stageScroll.replaceChildren(node);
    else stageScroll.replaceChildren(stage);
  }

  function showLoadError(message, retryLabel = '重新加载') {
    statusNode.textContent = '加载失败';
    statusNode.dataset.state = 'error';
    showStageMessage(el('div', { class: 'cls-editor-message' },
      notice(message || '幻灯片加载失败，请重试。', 'error'),
      button(retryLabel, () => bootstrap(), 'secondary', 'repeat')));
  }

  async function loadScene(targetId) {
    showStageMessage(loading('正在加载幻灯片…'));
    renderSidePanels();
    try {
      const data = await api(`/classroom/courses/${courseId}/scenes/${targetId}/content`);
      if (state.destroyed) return;
      const scene = data?.scene || {};
      if (sceneTypeOf(scene) !== 'slide') {
        showStageMessage(notice('只有幻灯片场景支持编辑。', 'warning'));
        return;
      }
      const content = scene.content && typeof scene.content === 'object' ? scene.content : {};
      const canvasData = content.canvas && typeof content.canvas === 'object' ? content.canvas : {};
      state.sceneId = Number(scene.id) || Number(targetId) || 0;
      state.sceneTitle = String(scene.title || '');
      state.version = Math.max(1, Number(scene.version) || 1);
      state.theme = normalizeTheme(canvasData.theme);
      state.elements = Array.isArray(canvasData.elements) ? (clone(canvasData.elements) || []) : [];
      state.selectedId = null;
      state.history = [];
      state.future = [];
      state.clipboard = null;
      state.dirty = false;
      state.revision = 0;
      showStageMessage(null);
      renderCanvas();
      renderSidePanels();
      updateTitle();
      updateSceneSelect();
      updateToolbar();
      setSaveState('saved');
    } catch (error) {
      if (state.destroyed) return;
      showLoadError(error.message || '幻灯片内容加载失败，请重试。');
    }
  }

  async function bootstrap() {
    statusNode.textContent = '加载中…';
    statusNode.dataset.state = 'loading';
    showStageMessage(loading('正在加载课程与场景…'));
    try {
      const detail = await api(`/classroom/courses/${courseId}`);
      if (state.destroyed) return;
      state.course = detail?.course || {};
      state.scenes = Array.isArray(detail?.scenes) ? detail.scenes : [];
      state.slideScenes = state.scenes.filter(isSlideScene);
      titleNode.replaceChildren(el('strong', { class: 'cls-editor-course-title' }, state.course.title || 'AI 课堂'));
      updateToolbar();
      if (!state.slideScenes.length) {
        updateSceneSelect();
        showStageMessage(el('div', { class: 'cls-editor-message' },
          notice('这门课程还没有幻灯片场景，暂无可编辑内容。可先回到生成工作台生成场景。', 'warning'),
          el('div', { class: 'row' },
            button('去生成工作台', () => go('classroom', null, { course: courseId, view: 'generate' }), '', 'sparkle'),
            button('返回播放', () => go('classroom', null, { course: courseId, view: 'play' }), 'secondary', 'presentation'))));
        return;
      }
      const requested = Number(sceneId) || 0;
      const target = state.slideScenes.find(item => Number(item.id) === requested) || state.slideScenes[0];
      state.sceneTitle = String(target.title || '');
      updateSceneSelect();
      await loadScene(target.id);
    } catch (error) {
      if (state.destroyed || !current(renderVersion)) return;
      showLoadError(`课程加载失败：${error.message || '请检查网络后重试。'}`);
    }
  }

  // -------------------------------------------------------------------------
  // 事件绑定
  // -------------------------------------------------------------------------
  propsTab.addEventListener('click', () => { state.sideTab = 'props'; renderSidePanels(); });
  listTab.addEventListener('click', () => { state.sideTab = 'list'; renderSidePanels(); });
  canvas.addEventListener('pointerdown', onCanvasPointerDown);
  canvas.addEventListener('pointermove', onCanvasPointerMove);
  canvas.addEventListener('pointerup', finishDrag);
  canvas.addEventListener('pointercancel', finishDrag);
  canvas.addEventListener('dblclick', (event) => {
    const elementNode = event.target.closest?.('.cls-editor-element');
    if (!elementNode) return;
    const item = state.elements.find(entry => String(entry.id) === String(elementNode.dataset.elementId));
    if (!item || item.type !== 'text') return;
    selectElement(item.id);
    state.sideTab = 'props';
    renderSidePanels();
    const textarea = panelHost.querySelector('.cls-editor-textarea');
    if (textarea) { textarea.focus(); textarea.select?.(); }
  });
  sceneSelect.addEventListener('change', () => requestSceneSwitch(sceneSelect.value));
  zoomSelect.addEventListener('change', () => {
    state.zoom = zoomSelect.value;
    applyStageScale();
  });

  const teardown = [];
  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    teardown.push(() => target.removeEventListener(type, handler, options));
  }
  listen(window, 'keydown', onKeyDown);
  listen(window, 'beforeunload', onBeforeUnload);
  listen(window, 'pagehide', flushOnDestroy);
  listen(document, 'click', onDocumentClickCapture, true);
  listen(window, 'resize', onWindowResize);
  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(() => { if (!state.destroyed) applyStageScale(); });
    resizeObserver.observe(stageScroll);
  }

  function destroy() {
    if (state.destroyed) return;
    // 关闭/切换页面前尽力提交未保存修改（keepalive 保证请求在页面卸载后仍可发出）。
    flushOnDestroy();
    state.destroyed = true;
    clearTimeout(autoTimer);
    autoTimer = null;
    if (resizeObserver) {
      try { resizeObserver.disconnect(); } catch { /* 忽略 */ }
      resizeObserver = null;
    }
    teardown.forEach(fn => { try { fn(); } catch { /* 忽略 */ } });
    closeDialogs();
    root.remove();
  }

  function flushOnDestroy() {
    if (!state.dirty || !state.sceneId) return;
    try {
      fetch(endpoint(), {
        method: 'PATCH',
        credentials: 'same-origin',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ version: state.version, content: buildContent() }),
      });
    } catch { /* 尽力保存，失败不阻塞离开 */ }
  }

  // 启动
  updateToolbar();
  bootstrap();

  return { node: root, destroy };
}
