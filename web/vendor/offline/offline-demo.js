/* 自动生成，勿手改：python scripts/build_offline_demo_bundle.py */
(function (global) {
"use strict";

/* ---- 来源：web/vendor/principles/flow.js（去 ESM export 后原样内联） ---- */
var __zhixuFlow = (function () {
"use strict";
/**
 * 结构化流程渲染器（SVG，无外部依赖）。
 *
 * 数据契约（与 SeeCode 类流程 JSON 对齐，向后兼容原 flow 数据）：
 * {
 *   layout?: 'flow'|'loop'|'timeline'|'state',        // 缺省 'flow'
 *   style?:  'clean'|'sketch',                        // 缺省 'clean'
 *   axisLabel?: string,                               // 仅 timeline：时间轴标签（可选）
 *   nodes: [{ id, label, kind?: 'start'|'process'|'branch'|'end'|'state'|'event',
 *             x?, y? }],
 *   edges: [{ id?, from, to, label?, kind?: 'solid'|'dashed'|'feedback' }],
 *   steps: [{ label, detail?, node_id?, node_ids?, edge_ids? }],
 *   notes?: string[],
 *   stepIntervalMs?: number
 * }
 *
 * 布局：
 * - `flow`：默认；节点同时给出 x/y 时按给定坐标（教学排版坐标）绘制，否则按 edges 做
 *   分层（Kahn 拓扑层 + 环上节点兜底），层内居中排列；回环/反向边走底部绕行。
 * - `loop`：流程 + 回边（循环箭头）；回边按序号分道走底部通道，自动避开节点。
 * - `timeline`：水平时间轴 + 刻度 + 事件卡片；steps 与节点按 order 对应并排序。
 * - `state`：状态机；kind=state 画圆形状态节点、其它画圆角矩形，迁移边带标签，
 *   当前步骤高亮；缺坐标时自动环形排列。
 *
 * 风格：
 * - `clean`：现有简约显示（默认，渲染结果与旧版一致）。
 * - `sketch`：手绘风格——以节点/边 id 为种子做**确定性**伪随机抖动（不使用
 *   Math.random，重放/导出海报一致），节点双描边 + 轻微旋转，边双描边，只使用
 *   SVG 与系统字体，不加载任何外部字体/品牌资源。
 *
 * 安全性：所有文本都用 textContent / createTextNode 写入，不拼接 innerHTML；
 * 数据非法的节点/边会被忽略而不是抛错，便于播放器降级；未知 layout/style/kind
 * 回退到缺省值，未知 shape/字段一律忽略。
 */

const NS = 'http' + '://www.w3.org/2000/svg';

const LAYOUTS = ['flow', 'loop', 'timeline', 'state'];
const STYLES = ['clean', 'sketch'];
const NODE_KINDS = ['start', 'process', 'branch', 'end', 'state', 'event'];
const EDGE_KINDS = ['solid', 'dashed', 'feedback'];

const NODE_SIZE = {
  start: { w: 132, h: 46 },
  end: { w: 132, h: 46 },
  process: { w: 150, h: 52 },
  branch: { w: 156, h: 84 },
  state: { w: 132, h: 52 },
  event: { w: 138, h: 48 },
};

/** state 布局专用尺寸：状态节点画圆，事件节点画圆角矩形。 */
const STATE_NODE_SIZE = {
  state: { w: 110, h: 110 },
  event: { w: 152, h: 56 },
  start: { w: 132, h: 46 },
  end: { w: 132, h: 46 },
  process: { w: 150, h: 52 },
  branch: { w: 156, h: 84 },
};

/** timeline 布局常量：水平轴 + 上下交替的事件卡片。 */
const TIMELINE = {
  startX: 150,
  gap: 212,
  axisY: 226,
  aboveY: 128,
  belowY: 324,
  cardW: 188,
  cardH: 66,
  axisLeft: 120,
  axisRight: 136,
};

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children).flat(Infinity)) {
    if (child instanceof Node) node.append(child);
    else if (child !== null && child !== undefined && child !== false) node.append(document.createTextNode(String(child)));
  }
  return node;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

/** 中文字符串按视觉宽度近似折行（最多 maxLines 行，超出用省略号）。 */
function wrapLabel(label, maxChars = 8, maxLines = 2) {
  const chars = [...String(label || '').trim()];
  if (!chars.length) return [''];
  if (chars.length <= maxChars) return [chars.join('')];
  const lines = [];
  for (let i = 0; i < chars.length && lines.length < maxLines; i += maxChars) {
    lines.push(chars.slice(i, i + maxChars).join(''));
  }
  if (lines.length === maxLines && chars.length > maxChars * maxLines) {
    const last = [...lines[maxLines - 1]];
    last[last.length - 1] = '…';
    lines[maxLines - 1] = last.join('');
  }
  return lines;
}

function nodeSize(node, layout) {
  const table = layout === 'state' ? STATE_NODE_SIZE : NODE_SIZE;
  return table[node.kind] || table.process;
}

/* ------------------------------------------------------------------------- *
 * 确定性伪随机：手绘抖动只用「id 种子 + 整数哈希」，不用 Math.random，
 * 保证同一份数据每次渲染（含海报导出、重播）完全一致。
 * ------------------------------------------------------------------------- */

function hashSeed(text) {
  const value = String(text || '');
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function seededRandom(seed) {
  let state = seed >>> 0;
  return function random() {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

/** 返回 `(amount) => [-amount, amount]` 的确定性抖动函数。 */
function makeJitter(seedText) {
  const random = seededRandom(hashSeed(seedText));
  return (amount = 1) => (random() * 2 - 1) * amount;
}

/** 节点手绘旋转角（度，±1.1°）。 */
function sketchRotation(seedText) {
  const random = seededRandom(hashSeed(`${seedText}|rotation`));
  return (random() * 2 - 1) * 1.1;
}

/* ------------------------------------------------------------------------- *
 * 数据归一化与布局
 * ------------------------------------------------------------------------- */

function normalizeNode(raw, index, layout) {
  if (!raw || raw.id === undefined || raw.id === null) return null;
  const kind = NODE_KINDS.includes(raw.kind) ? raw.kind : 'process';
  return {
    id: String(raw.id),
    label: String(raw.label ?? raw.id),
    kind,
    circle: layout === 'state' && kind === 'state',
    x: Number.isFinite(Number(raw.x)) ? Number(raw.x) : null,
    y: Number.isFinite(Number(raw.y)) ? Number(raw.y) : null,
    order: index,
  };
}

function normalizeEdges(rawEdges, nodeById, layout) {
  const seen = new Set();
  const edges = [];
  for (const raw of Array.isArray(rawEdges) ? rawEdges : []) {
    if (!raw || raw.from === undefined || raw.to === undefined) continue;
    const from = String(raw.from);
    const to = String(raw.to);
    if (!nodeById.has(from) || !nodeById.has(to)) continue;
    const id = String(raw.id || `e${edges.length + 1}`);
    if (seen.has(id)) continue;
    seen.add(id);
    let kind = EDGE_KINDS.includes(raw.kind) ? raw.kind : 'solid';
    // loop 布局下，指向更早节点的边即使没标 feedback 也按回边处理。
    if (layout === 'loop') {
      const source = nodeById.get(from);
      const target = nodeById.get(to);
      if (kind === 'solid' && source && target && Number.isFinite(source.x) && Number.isFinite(target.x)
        && target.x < source.x - 1) {
        kind = 'feedback';
      }
    }
    edges.push({ id, from, to, label: raw.label ? String(raw.label) : '', kind });
  }
  return edges;
}

function autoLayout(nodes, edges) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const adjacency = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    adjacency.get(edge.from)?.push(edge.to);
    indegree.set(edge.to, (indegree.get(edge.to) || 0) + 1);
  }
  const layer = new Map();
  const queue = nodes.filter((node) => (indegree.get(node.id) || 0) === 0).map((node) => node.id);
  queue.forEach((id) => layer.set(id, 0));
  for (let head = 0; head < queue.length; head += 1) {
    const id = queue[head];
    for (const next of adjacency.get(id) || []) {
      const candidate = (layer.get(id) || 0) + 1;
      if ((layer.get(next) ?? -1) < candidate) layer.set(next, candidate);
      indegree.set(next, (indegree.get(next) || 1) - 1);
      if (indegree.get(next) === 0) queue.push(next);
    }
  }
  // 环上节点（回环边另一端）不会进入队列：按原始顺序给一个有限层，避免布局卡死。
  let maxLayer = 0;
  for (const value of layer.values()) maxLayer = Math.max(maxLayer, value);
  nodes.forEach((node, index) => {
    if (layer.has(node.id)) return;
    const guess = 1 + Math.floor((index / Math.max(1, nodes.length)) * (maxLayer + 1));
    layer.set(node.id, clamp(guess, 0, maxLayer + 1));
  });

  const columns = new Map();
  for (const node of nodes) {
    const col = layer.get(node.id) || 0;
    if (!columns.has(col)) columns.set(col, []);
    columns.get(col).push(node);
  }
  const columnGap = 46;
  const rowGap = 46;
  // 先统一求最大列宽，再按「最大列宽 + 列间距」等距排布，避免不同列宽造成错位。
  let maxColumnWidth = 0;
  for (const items of columns.values()) {
    maxColumnWidth = Math.max(maxColumnWidth, ...items.map((node) => nodeSize(node, 'flow').w));
  }
  for (const [col, items] of columns.entries()) {
    let cursorY = 90;
    for (const node of items) {
      node.x = 46 + maxColumnWidth / 2 + col * (maxColumnWidth + columnGap);
      node.y = cursorY + nodeSize(node, 'flow').h / 2;
      cursorY += nodeSize(node, 'flow').h + rowGap;
    }
  }
}

/** timeline：事件卡片沿水平时间轴上下交替排列（按节点 order）。 */
function timelineLayout(nodes) {
  nodes.forEach((node, index) => {
    node.x = TIMELINE.startX + index * TIMELINE.gap;
    node.y = index % 2 === 0 ? TIMELINE.aboveY : TIMELINE.belowY;
  });
}

/** state：缺坐标时按环形排布（状态机常见画法），保证迁移边可读。 */
function stateRingLayout(nodes) {
  const count = nodes.length;
  if (!count) return;
  const radius = Math.max(150, count * 34);
  const cx = 470;
  const cy = 300;
  nodes.forEach((node, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
    node.x = cx + radius * Math.cos(angle);
    node.y = cy + Math.sin(angle) * radius;
  });
}

function hasExplicitLayout(nodes) {
  return nodes.length > 0 && nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y));
}

function layoutModel(model) {
  const { nodes, edges, layout } = model;
  if (layout === 'timeline') timelineLayout(nodes);
  else if (layout === 'state') {
    if (!hasExplicitLayout(nodes)) stateRingLayout(nodes);
  } else if (!hasExplicitLayout(nodes)) autoLayout(nodes, edges);
  for (const node of nodes) {
    if (layout === 'timeline') {
      node.w = TIMELINE.cardW;
      node.h = TIMELINE.cardH;
    } else {
      const size = nodeSize(node, layout);
      node.w = size.w;
      node.h = size.h;
    }
    node.cx = Number(node.x);
    node.cy = Number(node.y);
    node.radius = node.circle ? node.w / 2 : 0;
  }
}

/* ------------------------------------------------------------------------- *
 * 步骤归一化
 * ------------------------------------------------------------------------- */

function normalizeSteps(rawSteps, nodes, edges, layout) {
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edgeIds = new Set(edges.map((edge) => edge.id));
  const cleanIds = (value, allowed) => {
    const list = Array.isArray(value) ? value : (value === undefined || value === null ? [] : [value]);
    return [...new Set(list.map(String).filter((id) => allowed.has(id)))];
  };
  const raw = Array.isArray(rawSteps) ? rawSteps.filter((step) => step && typeof step === 'object') : [];
  if (!raw.length) {
    // 没有 steps 时按节点顺序补一组「逐节点」步骤，保证播放器仍可单步演示。
    return nodes.map((node, index) => ({
      label: node.label,
      detail: '',
      node_id: node.id,
      node_ids: [node.id],
      edge_ids: edges.filter((edge) => edge.from === node.id).map((edge) => edge.id),
      index,
    }));
  }
  const steps = raw.map((step, index) => {
    const primary = step.node_id === undefined || step.node_id === null ? '' : String(step.node_id);
    const primaryValid = nodeIds.has(primary) ? primary : '';
    const nodeList = cleanIds(step.node_ids, nodeIds);
    if (primaryValid && !nodeList.includes(primaryValid)) nodeList.unshift(primaryValid);
    const edgeList = cleanIds(step.edge_ids, edgeIds);
    return {
      label: step.label === undefined || step.label === null ? '' : String(step.label),
      detail: step.detail === undefined || step.detail === null ? '' : String(step.detail),
      node_id: primaryValid,
      node_ids: nodeList,
      edge_ids: edgeList,
      index,
    };
  });
  // 缺失字段安全降级：只写了 label 的步骤按顺序对应节点，避免「有播放、无高亮」。
  steps.forEach((step, index) => {
    if (step.node_ids.length || step.edge_ids.length) return;
    const node = nodes[index];
    if (!node) return;
    step.node_id = node.id;
    step.node_ids = [node.id];
  });
  // timeline：steps 与节点按 order 排列（节点数组顺序），保证时间轴顺序稳定。
  if (layout === 'timeline') {
    const orderOf = new Map(nodes.map((node) => [node.id, node.order]));
    const stepOrder = (step) => {
      for (const id of step.node_ids) {
        if (orderOf.has(id)) return orderOf.get(id);
      }
      return step.index;
    };
    steps.sort((a, b) => stepOrder(a) - stepOrder(b));
    steps.forEach((step, index) => { step.index = index; });
  }
  return steps;
}

/** 对外：把任意流程数据整理为渲染器可用的模型（便于测试与复用）。 */
function normalizeFlow(data) {
  const source = data && typeof data === 'object' ? data : {};
  const layout = LAYOUTS.includes(source.layout) ? source.layout : 'flow';
  const style = STYLES.includes(source.style) ? source.style : 'clean';
  const nodes = (Array.isArray(source.nodes) ? source.nodes : [])
    .map((raw, index) => normalizeNode(raw, index, layout))
    .filter(Boolean);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edges = normalizeEdges(source.edges, nodeById, layout);
  const model = { layout, style, nodes, edges };
  layoutModel(model);
  const steps = normalizeSteps(source.steps, nodes, edges, layout);
  const notes = (Array.isArray(source.notes) ? source.notes : [])
    .map((note) => String(note || '').trim())
    .filter(Boolean);
  const interval = Number(source.stepIntervalMs);
  return {
    layout,
    style,
    axisLabel: source.axisLabel === undefined || source.axisLabel === null
      ? '' : String(source.axisLabel),
    axisY: TIMELINE.axisY,
    nodes,
    edges,
    steps,
    notes,
    stepIntervalMs: Number.isFinite(interval) && interval >= 200 ? interval : 1200,
  };
}

/* ------------------------------------------------------------------------- *
 * 几何：节点锚点、边路径、箭头
 * ------------------------------------------------------------------------- */

/** state 布局：从节点中心指向目标方向、落在边界上的锚点（圆用半径，矩形用边）。 */
function anchorNode(node, target) {
  const dx = target.cx - node.cx;
  const dy = target.cy - node.cy;
  if (node.circle && Number.isFinite(node.radius) && node.radius > 0) {
    const distance = Math.hypot(dx, dy) || 1;
    return {
      x: node.cx + (dx / distance) * node.radius,
      y: node.cy + (dy / distance) * node.radius,
    };
  }
  if (Math.abs(dx) >= Math.abs(dy)) {
    return { x: node.cx + (dx >= 0 ? 1 : -1) * (node.w / 2), y: node.cy };
  }
  return { x: node.cx, y: node.cy + (dy >= 0 ? 1 : -1) * (node.h / 2) };
}

function anchorPoints(from, to, layout) {
  if (layout === 'state') {
    const a = anchorNode(from, to);
    const b = anchorNode(to, from);
    return { ax: a.x, ay: a.y, bx: b.x, by: b.y };
  }
  const dx = to.cx - from.cx;
  const dy = to.cy - from.cy;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const dir = dx >= 0 ? 1 : -1;
    return {
      ax: from.cx + dir * (from.w / 2),
      ay: from.cy,
      bx: to.cx - dir * (to.w / 2),
      by: to.cy,
    };
  }
  const dir = dy >= 0 ? 1 : -1;
  return {
    ax: from.cx,
    ay: from.cy + dir * (from.h / 2),
    bx: to.cx,
    by: to.cy - dir * (to.h / 2),
  };
}

function arrowPolygon(tipX, tipY, angle) {
  const size = 9;
  const spread = 0.46;
  const p1x = tipX - size * Math.cos(angle - spread);
  const p1y = tipY - size * Math.sin(angle - spread);
  const p2x = tipX - size * Math.cos(angle + spread);
  const p2y = tipY - size * Math.sin(angle + spread);
  return `${tipX},${tipY} ${p1x},${p1y} ${p2x},${p2y}`;
}

/**
 * 计算一条边的三次贝塞尔控制点。
 * 返回统一结构 `{ points: [[x,y],[x,y],[x,y],[x,y]], arrow, label, back }`，
 * 便于 clean / sketch 两种风格共用同一几何（sketch 只在输出 path 时加抖动）。
 */
function edgeGeometry(edge, byId, { layout = 'flow', bendY = 0 } = {}) {
  const from = byId.get(edge.from);
  const to = byId.get(edge.to);
  if (!from || !to) return null;
  // 目标在源左侧视为回环/反向边（flow/loop）：改走底部绕行，避免穿过中间节点。
  const back = layout !== 'state' && (edge.kind === 'feedback' || to.cx < from.cx - 1);
  if (back) {
    // 回环边：从源节点底部向下绕行，再从目标节点底部回到目标，避免穿过中间节点。
    const sx = from.cx;
    const sy = from.cy + from.h / 2 + 2;
    const tx = to.cx;
    const ty = to.cy + to.h / 2 + 2;
    // 终点再下移 7px：箭头尖留在节点外侧，否则会被节点填充色盖住。
    const tipY = ty + 7;
    return {
      points: [[sx, sy], [sx, bendY], [tx, bendY], [tx, tipY]],
      arrow: { x: tx, y: tipY, angle: -Math.PI / 2 },
      label: { x: (sx + tx) / 2, y: bendY - 8 },
      back: true,
    };
  }
  const points = anchorPoints(from, to, layout);
  const dx = points.bx - points.ax;
  const dy = points.by - points.ay;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  // 终点缩回节点边界外 7px，给箭头留出可见空间（否则被节点形状覆盖）。
  const tipX = points.bx - ux * 7;
  const tipY = points.by - uy * 7;
  const c1x = points.ax + dx * 0.45;
  const c1y = points.ay + dy * 0.45;
  const c2x = tipX - dx * 0.45;
  const c2y = tipY - dy * 0.45;
  let label;
  if (layout === 'state') {
    // 状态机迁移标签：水平迁移放上方、竖直迁移放右侧，尽量不压住圆形状态节点。
    const mx = (points.ax + points.bx) / 2;
    const my = (points.ay + points.by) / 2;
    label = Math.abs(dy) <= Math.abs(dx)
      ? { x: mx, y: my - 18 }
      : { x: mx + 26, y: my };
  } else if (Math.abs(dy) <= 8) {
    label = {
      x: (points.ax + points.bx) / 2,
      y: Math.min(points.ay, points.by) - Math.max(from.h, to.h) / 2 - 10,
    };
  } else if (Math.abs(dx) <= 8) {
    label = { x: Math.max(points.ax, points.bx) + 16, y: (points.ay + points.by) / 2 };
  } else {
    label = { x: (points.ax + points.bx) / 2 + 18, y: (points.ay + points.by) / 2 - 6 };
  }
  return {
    points: [[points.ax, points.ay], [c1x, c1y], [c2x, c2y], [tipX, tipY]],
    arrow: { x: tipX, y: tipY, angle: Math.atan2(uy, ux) },
    label,
    back: false,
  };
}

/** 把控制点序列化成 path d；sketch 时按 seed 抖动（clean 输出与旧版完全一致）。 */
function pathD(points, jitter) {
  const coordinates = [];
  for (const point of points) {
    for (const value of point) {
      const base = Number.isFinite(value) ? value : 0;
      if (!jitter) {
        coordinates.push(String(base));
      } else {
        coordinates.push((Math.round((base + jitter(1.2)) * 100) / 100).toString());
      }
    }
  }
  return `M ${coordinates[0]} ${coordinates[1]} C ${coordinates[2]} ${coordinates[3]} `
    + `${coordinates[4]} ${coordinates[5]} ${coordinates[6]} ${coordinates[7]}`;
}

function sketchArrowPoints(arrow, jitter) {
  const size = 9;
  const spread = 0.46;
  const list = [];
  const push = (x, y) => list.push(`${(Math.round((x + jitter(0.9)) * 100) / 100).toString()},${(Math.round((y + jitter(0.9)) * 100) / 100).toString()}`);
  push(arrow.x, arrow.y);
  push(arrow.x - size * Math.cos(arrow.angle - spread), arrow.y - size * Math.sin(arrow.angle - spread));
  push(arrow.x - size * Math.cos(arrow.angle + spread), arrow.y - size * Math.sin(arrow.angle + spread));
  return list.join(' ');
}

/**
 * 为回边分配底部通道序号：`loop` 布局下多条回边分道，避免叠在一起；
 * `flow` 布局沿用旧的单一底部通道（保持既有渲染不变）。
 */
function assignFeedbackLanes(model) {
  const lanes = new Map();
  if (model.layout !== 'flow' && model.layout !== 'loop') return lanes;
  const stagger = model.layout === 'loop';
  const byId = new Map(model.nodes.map((node) => [node.id, node]));
  let lane = 0;
  for (const edge of model.edges) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    if (edge.kind === 'feedback' || to.cx < from.cx - 1) {
      lanes.set(edge.id, stagger ? lane : 0);
      lane += 1;
    }
  }
  return lanes;
}

/** 实际用到的底部通道数（flow 恒为 0/1）。 */
function laneCountOf(lanes) {
  if (!lanes.size) return 0;
  return Math.max(...lanes.values()) + 1;
}

/* ------------------------------------------------------------------------- *
 * SVG 构建：节点
 * ------------------------------------------------------------------------- */

function cleanShapeElement(node, model) {
  const halfW = node.w / 2;
  const halfH = node.h / 2;
  if (model.layout === 'state' && node.circle) {
    return svgEl('circle', { class: 'pd-flow-shape', r: Math.min(halfW, halfH) });
  }
  if (node.kind === 'branch') {
    return svgEl('polygon', {
      class: 'pd-flow-shape',
      points: `0,${-halfH} ${halfW},0 0,${halfH} ${-halfW},0`,
    });
  }
  let radius;
  if (node.kind === 'process') radius = 10;
  else if (node.kind === 'state') radius = 12;
  else radius = halfH;
  return svgEl('rect', {
    class: 'pd-flow-shape',
    x: -halfW,
    y: -halfH,
    width: node.w,
    height: node.h,
    rx: radius,
    ry: radius,
  });
}

/** 手绘圆角矩形路径：四个角用二次贝塞尔近似，每条边带轻微抖动。 */
function sketchRectPath(width, height, radius, jitter) {
  const halfW = width / 2;
  const halfH = height / 2;
  const corner = clamp(radius, 4, Math.min(halfW, halfH));
  const j = jitter;
  return [
    `M ${(-halfW + corner + j(1.1)).toFixed(2)} ${(-halfH + j(1.1)).toFixed(2)}`,
    `L ${(halfW - corner + j(1.1)).toFixed(2)} ${(-halfH + j(1.1)).toFixed(2)}`,
    `Q ${(halfW + j(1.1)).toFixed(2)} ${(-halfH + j(1.1)).toFixed(2)} ${(halfW + j(1.1)).toFixed(2)} ${(-halfH + corner + j(1.1)).toFixed(2)}`,
    `L ${(halfW + j(1.1)).toFixed(2)} ${(halfH - corner + j(1.1)).toFixed(2)}`,
    `Q ${(halfW + j(1.1)).toFixed(2)} ${(halfH + j(1.1)).toFixed(2)} ${(halfW - corner + j(1.1)).toFixed(2)} ${(halfH + j(1.1)).toFixed(2)}`,
    `L ${(-halfW + corner + j(1.1)).toFixed(2)} ${(halfH + j(1.1)).toFixed(2)}`,
    `Q ${(-halfW + j(1.1)).toFixed(2)} ${(halfH + j(1.1)).toFixed(2)} ${(-halfW + j(1.1)).toFixed(2)} ${(halfH - corner + j(1.1)).toFixed(2)}`,
    `L ${(-halfW + j(1.1)).toFixed(2)} ${(-halfH + corner + j(1.1)).toFixed(2)}`,
    `Q ${(-halfW + j(1.1)).toFixed(2)} ${(-halfH + j(1.1)).toFixed(2)} ${(-halfW + corner + j(1.1)).toFixed(2)} ${(-halfH + j(1.1)).toFixed(2)}`,
    'Z',
  ].join(' ');
}

function sketchPolygonPoints(points, jitter) {
  return points
    .map(([x, y]) => `${(x + jitter(1.1)).toFixed(2)},${(y + jitter(1.1)).toFixed(2)}`)
    .join(' ');
}

/**
 * 手绘形状：主形状 + 错位双描边回声。返回数组，由调用方放进带旋转的 frame。
 */
function sketchShapeElements(node, model, seed, echo = false) {
  const halfW = node.w / 2;
  const halfH = node.h / 2;
  const jitter = makeJitter(seed);
  const className = echo ? 'pd-flow-shape pd-flow-shape-echo' : 'pd-flow-shape';
  if (model.layout === 'state' && node.circle) {
    return [svgEl('circle', {
      class: className,
      r: (Math.min(halfW, halfH) + jitter(1.2)).toFixed(2),
    })];
  }
  if (node.kind === 'branch') {
    const points = [[0, -halfH], [halfW, 0], [0, halfH], [-halfW, 0]];
    return [svgEl('polygon', { class: className, points: sketchPolygonPoints(points, jitter) })];
  }
  let radius;
  if (node.kind === 'process') radius = 10;
  else if (node.kind === 'state') radius = 12;
  else radius = halfH;
  return [svgEl('path', {
    class: className,
    d: sketchRectPath(node.w, node.h, radius, jitter),
  })];
}

/** 把节点图形（形状 + 文本 + tooltip）追加到已平移到节点中心的容器。 */
function appendNodeVisual(container, node, model) {
  if (model.style === 'sketch') {
    const seed = `node:${node.id}`;
    const frame = svgEl('g', {
      class: 'pd-flow-sketch-frame',
      transform: `rotate(${sketchRotation(seed).toFixed(2)})`,
    });
    frame.append(...sketchShapeElements(node, model, seed, false));
    const echoSeed = `${seed}|echo`;
    const echoJitterX = makeJitter(`${echoSeed}|x`);
    const echoJitterY = makeJitter(`${echoSeed}|y`);
    const echo = svgEl('g', {
      class: 'pd-flow-sketch-echo',
      transform: `translate(${echoJitterX(1.5).toFixed(2)},${echoJitterY(1.5).toFixed(2)}) `
        + `rotate(${sketchRotation(echoSeed).toFixed(2)})`,
    });
    echo.append(...sketchShapeElements(node, model, echoSeed, true));
    container.append(frame, echo);
  } else {
    container.append(cleanShapeElement(node, model));
  }

  // 节点标签：正文用 textContent + tspan 折行；超过两行省略。
  const circleNode = model.layout === 'state' && node.kind === 'state';
  const timeline = model.layout === 'timeline';
  const maxChars = timeline ? 12 : (circleNode ? 6 : (node.kind === 'branch' ? 7 : 8));
  const lines = wrapLabel(node.label, maxChars, 2);
  const lineHeight = circleNode || timeline ? 16 : 17;
  const firstY = -((lines.length - 1) * lineHeight) / 2;
  const text = svgEl('text', {
    class: 'pd-flow-label',
    'text-anchor': 'middle',
    'dominant-baseline': 'middle',
  });
  lines.forEach((line, index) => {
    // 注意：SVG 里文本行必须用 tspan，不能嵌套 text 元素。
    text.append(svgEl('tspan', { x: 0, y: firstY + index * lineHeight }, [document.createTextNode(line)]));
  });
  container.append(text);
  container.append(svgEl('title', {}, [document.createTextNode(`${node.label}（${node.kind}）`)]));
}

function drawNode(node, model) {
  const group = svgEl('g', {
    class: `pd-flow-node pd-flow-${node.kind}${model.style === 'sketch' ? ' is-sketch' : ''}`,
    'data-node': node.id,
    transform: `translate(${node.cx},${node.cy})`,
  });
  appendNodeVisual(group, node, model);
  return group;
}

/** timeline 事件卡片：卡片 + 引到时间轴的竖线 + 轴上的刻度点。 */
function drawTimelineNode(node, model) {
  const group = svgEl('g', {
    class: `pd-flow-node pd-flow-timeline-item pd-flow-${node.kind}`
      + `${model.style === 'sketch' ? ' is-sketch' : ''}`,
    'data-node': node.id,
  });
  const axisY = model.axisY;
  const stemStart = node.cy < axisY ? node.cy + node.h / 2 : node.cy - node.h / 2;
  group.append(svgEl('line', {
    class: 'pd-flow-timeline-stem',
    x1: node.cx,
    y1: stemStart,
    x2: node.cx,
    y2: axisY,
  }));
  group.append(svgEl('circle', {
    class: 'pd-flow-tick',
    cx: node.cx,
    cy: axisY,
    r: 5,
  }));
  const card = svgEl('g', {
    class: 'pd-flow-timeline-card',
    transform: `translate(${node.cx},${node.cy})`,
  });
  appendNodeVisual(card, node, model);
  group.append(card);
  return group;
}

/* ------------------------------------------------------------------------- *
 * SVG 构建：边
 * ------------------------------------------------------------------------- */

function drawEdge(edge, geometry, model) {
  const sketch = model.style === 'sketch';
  const kind = edge.kind || 'solid';
  const classes = ['pd-flow-edge'];
  if (kind === 'dashed') classes.push('is-dashed');
  if (kind === 'feedback') classes.push('is-feedback');
  const group = svgEl('g', {
    class: classes.join(' '),
    'data-edge': edge.id,
    // 只有非 solid 边才标记类型：缺省数据保持与旧版一致的 DOM。
    'data-edge-kind': kind === 'solid' ? undefined : kind,
  });
  if (sketch) {
    const seed = `edge:${edge.id}`;
    group.append(svgEl('path', {
      class: 'pd-flow-edge-line',
      d: pathD(geometry.points, makeJitter(seed)),
    }));
    const echoSeed = `${seed}|echo`;
    const echoJitterX = makeJitter(`${echoSeed}|x`);
    const echoJitterY = makeJitter(`${echoSeed}|y`);
    const offset = `translate(${echoJitterX(1.2).toFixed(2)},${echoJitterY(1.2).toFixed(2)})`;
    group.append(svgEl('path', {
      class: 'pd-flow-edge-line pd-flow-edge-echo',
      d: pathD(geometry.points, makeJitter(echoSeed)),
      transform: offset,
    }));
    group.append(svgEl('polygon', {
      class: 'pd-flow-arrowhead',
      points: sketchArrowPoints(geometry.arrow, makeJitter(seed)),
    }));
    group.append(svgEl('polygon', {
      class: 'pd-flow-arrowhead pd-flow-arrowhead-echo',
      points: sketchArrowPoints(geometry.arrow, makeJitter(echoSeed)),
      transform: offset,
    }));
  } else {
    group.append(svgEl('path', { class: 'pd-flow-edge-line', d: pathD(geometry.points) }));
    group.append(svgEl('polygon', {
      class: 'pd-flow-arrowhead',
      points: arrowPolygon(geometry.arrow.x, geometry.arrow.y, geometry.arrow.angle),
    }));
  }
  if (edge.label) {
    const width = [...edge.label].length * 11 + 12;
    const label = svgEl('g', {
      class: 'pd-flow-edge-label',
      transform: `translate(${geometry.label.x},${geometry.label.y})`,
    });
    label.append(svgEl('rect', {
      class: 'pd-flow-edge-label-bg',
      x: -width / 2,
      y: -11,
      width,
      height: 20,
      rx: 5,
    }));
    label.append(svgEl('text', {
      class: 'pd-flow-edge-label-text',
      x: 0,
      y: 0,
      'text-anchor': 'middle',
      'dominant-baseline': 'middle',
    }, [document.createTextNode(edge.label)]));
    group.append(label);
  }
  return group;
}

/* ------------------------------------------------------------------------- *
 * viewBox 与整体构建
 * ------------------------------------------------------------------------- */

function computeViewBox(model, { title = '', footer = '', laneCount = 0 } = {}) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of model.nodes) {
    minX = Math.min(minX, node.cx - node.w / 2);
    maxX = Math.max(maxX, node.cx + node.w / 2);
    minY = Math.min(minY, node.cy - node.h / 2);
    maxY = Math.max(maxY, node.cy + node.h / 2);
  }
  if (!Number.isFinite(minX)) {
    minX = 0;
    minY = 0;
    maxX = 640;
    maxY = 360;
  }
  if (model.layout === 'timeline' && model.nodes.length) {
    // 时间轴从首卡左侧延伸、到末卡右侧带箭头，viewBox 必须把这些部分算进去。
    const firstX = Math.min(...model.nodes.map((node) => node.cx));
    const lastX = Math.max(...model.nodes.map((node) => node.cx));
    minX = Math.min(minX, firstX - TIMELINE.axisLeft);
    maxX = Math.max(maxX, lastX + TIMELINE.axisRight);
    minY = Math.min(minY, model.axisY - 26);
    maxY = Math.max(maxY, model.axisY + 26);
  }
  const padding = 34;
  const loopExtra = laneCount > 0 ? 78 + (laneCount - 1) * 18 : 0;
  const titleSpace = title ? 44 : 0;
  const footerSpace = footer ? 30 : 0;
  const x = minX - padding;
  const y = minY - padding - titleSpace;
  const width = Math.max(320, maxX - minX + padding * 2);
  const height = Math.max(220, maxY - minY + padding * 2 + loopExtra + titleSpace + footerSpace);
  return { x, y, width, height, titleSpace, footerSpace, loopExtra };
}

function drawTimelineAxis(model) {
  const nodes = model.nodes;
  if (!nodes.length) return null;
  const firstX = Math.min(...nodes.map((node) => node.cx));
  const lastX = Math.max(...nodes.map((node) => node.cx));
  const startX = firstX - TIMELINE.axisLeft;
  const endX = lastX + TIMELINE.axisRight - 22;
  const axisY = model.axisY;
  const layer = svgEl('g', { class: 'pd-flow-timeline-axis-layer' });
  layer.append(svgEl('line', {
    class: 'pd-flow-timeline-axis',
    x1: startX,
    y1: axisY,
    x2: endX,
    y2: axisY,
  }));
  layer.append(svgEl('polygon', {
    class: 'pd-flow-timeline-arrow',
    points: `${endX + 22},${axisY} ${endX},${axisY - 8} ${endX},${axisY + 8}`,
  }));
  if (model.axisLabel) {
    layer.append(svgEl('text', {
      class: 'pd-flow-axis-label',
      x: startX,
      y: axisY - 16,
      'text-anchor': 'start',
    }, [document.createTextNode(model.axisLabel)]));
  }
  return layer;
}

/**
 * 生成流程 SVG。
 * @param normalized normalizeFlow() 的结果
 * @param {object} options `{ poster?, title?, footer?, ariaLabel? }`
 * @returns {SVGSVGElement}
 */
function buildFlowSvg(normalized, options = {}) {
  const { poster = false, title = '', footer = '', ariaLabel = '' } = options;
  const lanes = assignFeedbackLanes(normalized);
  const box = computeViewBox(normalized, { title, footer, laneCount: laneCountOf(lanes) });
  const byId = new Map(normalized.nodes.map((node) => [node.id, node]));
  const loopBaseY = box.y + box.height - box.footerSpace - 28;

  const classNames = ['pd-flow-svg'];
  if (poster) classNames.push('is-poster');
  if (normalized.style === 'sketch') classNames.push('is-sketch');
  if (normalized.layout !== 'flow') classNames.push(`is-layout-${normalized.layout}`);
  const root = svgEl('svg', {
    viewBox: `${box.x} ${box.y} ${box.width} ${box.height}`,
    class: classNames.join(' '),
    role: 'img',
    'aria-label': ariaLabel || title || 'principle flow diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });

  if (title) {
    root.append(svgEl('text', {
      class: 'pd-flow-title',
      x: box.x + box.width / 2,
      y: box.y + 26,
      'text-anchor': 'middle',
    }, [document.createTextNode(title)]));
  }

  if (normalized.layout === 'timeline') {
    const axis = drawTimelineAxis(normalized);
    if (axis) root.append(axis);
  } else {
    const edgeLayer = svgEl('g', { class: 'pd-flow-edges' });
    for (const edge of normalized.edges) {
      const lane = lanes.get(edge.id) || 0;
      const geometry = edgeGeometry(edge, byId, {
        layout: normalized.layout,
        bendY: loopBaseY + lane * 18,
      });
      if (geometry) edgeLayer.append(drawEdge(edge, geometry, normalized));
    }
    root.append(edgeLayer);
  }

  const nodeLayer = svgEl('g', {
    class: normalized.layout === 'timeline' ? 'pd-flow-timeline-items' : 'pd-flow-nodes',
  });
  for (const node of normalized.nodes) {
    nodeLayer.append(normalized.layout === 'timeline'
      ? drawTimelineNode(node, normalized)
      : drawNode(node, normalized));
  }
  root.append(nodeLayer);

  if (footer) {
    root.append(svgEl('text', {
      class: 'pd-flow-footer',
      x: box.x + box.width / 2,
      y: box.y + box.height - 10,
      'text-anchor': 'middle',
    }, [document.createTextNode(footer)]));
  }
  return root;
}

/* ------------------------------------------------------------------------- *
 * 对外 API：视图与海报
 * ------------------------------------------------------------------------- */

/**
 * 在 host 内创建流程视图（不做播放定时器，播放由统一播放器控制）。
 * @returns {{mode:'svg', element:SVGElement, getStepCount:()=>number,
 *   getStep:(index:number)=>object, setStep:(index:number)=>void, destroy:()=>void}}
 */
function createFlowView(host, data, options = {}) {
  const model = normalizeFlow(data);
  const ariaLabel = options.ariaLabel
    || (options.title ? `${options.title}` : 'principle flow diagram');
  const root = buildFlowSvg(model, { ariaLabel });
  const nodeGroups = new Map();
  const edgeGroups = new Map();
  for (const group of root.querySelectorAll('[data-node]')) nodeGroups.set(group.getAttribute('data-node'), group);
  for (const group of root.querySelectorAll('[data-edge]')) edgeGroups.set(group.getAttribute('data-edge'), group);

  let current = 0;

  function setStep(index) {
    if (!model.steps.length) return;
    current = clamp(Number(index) || 0, 0, model.steps.length - 1);
    const step = model.steps[current] || {};
    const activeNodes = new Set(step.node_ids || []);
    const activeEdges = new Set(step.edge_ids || []);
    for (const [id, group] of nodeGroups) group.classList.toggle('is-active', activeNodes.has(id));
    for (const [id, group] of edgeGroups) group.classList.toggle('is-active', activeEdges.has(id));
    root.classList.toggle('has-active', activeNodes.size > 0 || activeEdges.size > 0);
    root.dataset.step = String(current);
  }

  host.replaceChildren(root);
  setStep(0);

  return {
    mode: 'svg',
    element: root,
    getStepCount: () => model.steps.length,
    getStep: (index) => model.steps[clamp(Number(index) || 0, 0, model.steps.length - 1)] || {},
    getStepInterval: () => model.stepIntervalMs,
    getNotes: () => model.notes,
    getLayout: () => model.layout,
    getStyle: () => model.style,
    setStep,
    supportsDirection: false,
    destroy() {
      if (root.parentNode) root.remove();
      host.replaceChildren();
    },
  };
}

/**
 * 无交互静态海报（导出 / 降级用）。
 * @param {object} data 流程数据
 * @param {object} options `{ title?, footer?, ariaLabel? }`
 * @returns {SVGSVGElement}
 */
function renderFlowPoster(data, options = {}) {
  return buildFlowSvg(normalizeFlow(data), {
    poster: true,
    title: options.title ? String(options.title) : '',
    footer: options.footer ? String(options.footer) : '',
    ariaLabel: options.ariaLabel ? String(options.ariaLabel) : '',
  });
}
return {
  createFlowView: createFlowView,
  renderFlowPoster: renderFlowPoster,
};
})();

/* ---- 来源：web/vendor/principles/iso.js（去 ESM export 后原样内联） ---- */
var __zhixuIso = (function () {
"use strict";
/**
 * 等距结构渲染器（SVG，无外部依赖；Three.js 分支的降级视图）。
 *
 * 数据契约（向后兼容原 iso 数据）：
 * {
 *   unit?: number,                       // 1 个坐标单位对应的屏幕像素（默认 26）
 *   style?: 'clean'|'sketch',            // 缺省 clean；sketch=确定性抖动描边（同流程）
 *   parts: [{ id, label, x, y, z,        // y 向上为正；x/z 为水平面
 *             w, h, d,                   // 三个方向尺寸（坐标单位；圆柱/齿轮按外接尺寸）
 *             shape?: 'box'|'cylinder'|'gear'|'plate'|'rod',  // 缺省 box；未知回退 box
 *             color?, opacity?, shell?,  // shell=true 用半透明「剖开看内部」画法
 *             explode?: [dx,dy,dz],      // 完全拆开时相对原位的偏移向量
 *             description? }],           // 悬停 tooltip（原生 <title>）与标注说明
 *   labels?: [{ part_id?, text: string|string[], anchor?: [x,y,z], side?: 'left'|'right' }],
 *                                        // 引线标注增强：多行文本；无 part_id 时按 anchor 画自由标注
 *   steps: [{ label, detail?, part_ids: string[],
 *             phase?: 'disassemble'|'assemble', offset?: number }],
 *   notes?: string[]
 * }
 *
 * 投影用标准 30° 等距公式（与方案 §5.4 一致）：
 *   screenX = (x - z) * cos30 * unit
 *   screenY = ((x + z) * sin30 - y) * unit
 * 每个部件默认画顶/侧两面（+y、+x、+z 三个可见面）并用明暗色区分；新增形状用等距近似：
 * - cylinder：上/下椭圆 + 侧壁；
 * - gear：等距圆盘 + 齿块 + 轴孔；
 * - plate：扁 box；
 * - rod：长条 box。
 *
 * 步骤：
 * - 步骤带 phase 时按「拆解 / 装配」两段播放，可在两段之间切换方向；
 * - 没有 phase 的步骤按单一序列高亮（用于 3D 降级时的静态等距示意图）。
 *
 * 风格：`sketch` 用与 flow.js 相同的「id 种子 + 整数哈希」确定性伪随机（不使用
 * Math.random），做错位双描边与轻微旋转；海报导出与重播完全一致。
 *
 * 安全性：只用 createElementNS / textContent 写 SVG 文本，不拼 innerHTML。
 */

const NS = 'http' + '://www.w3.org/2000/svg';
const COS30 = Math.cos(Math.PI / 6);
const SIN30 = 0.5;
const MAX_EXPLODE_FACTOR = 2;

const PART_SHAPES = ['box', 'cylinder', 'gear', 'plate', 'rod'];
const STYLES = ['clean', 'sketch'];

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children).flat(Infinity)) {
    if (child instanceof Node) node.append(child);
    else if (child !== null && child !== undefined && child !== false) node.append(document.createTextNode(String(child)));
  }
  return node;
}

function textEl(value, attrs = {}) {
  return svgEl('text', attrs, [document.createTextNode(String(value))]);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function num(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function vec3(value) {
  if (!Array.isArray(value) || value.length < 3) return [0, 0, 0];
  return [num(value[0], 0), num(value[1], 0), num(value[2], 0)];
}

/* ------------------------------------------------------------------------- *
 * 确定性伪随机（与 flow.js 同一规则）：手绘抖动只用 id 种子，保证重放一致。
 * ------------------------------------------------------------------------- */

function hashSeed(text) {
  const value = String(text || '');
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function seededRandom(seed) {
  let state = seed >>> 0;
  return function random() {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function makeJitter(seedText) {
  const random = seededRandom(hashSeed(seedText));
  return (amount = 1) => (random() * 2 - 1) * amount;
}

function sketchRotation(seedText) {
  const random = seededRandom(hashSeed(`${seedText}|rotation`));
  return (random() * 2 - 1) * 0.9;
}

/* ------------------------------------------------------------------------- *
 * 颜色工具：把部件基色派生为顶面（提亮）/侧面（压暗）与描边色。
 * ------------------------------------------------------------------------- */

function parseColor(value, fallback = '#9aa9ad') {
  const text = String(value || '').trim();
  const short = text.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i);
  if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toLowerCase();
  if (/^#[0-9a-f]{6}$/i.test(text)) return text.toLowerCase();
  return fallback;
}

function tint(hex, amount) {
  const value = parseColor(hex);
  const target = amount >= 0 ? 255 : 0;
  const ratio = Math.min(1, Math.abs(amount));
  const channels = [1, 3, 5].map((index) => {
    const channel = parseInt(value.slice(index, index + 2), 16);
    return Math.round(channel + (target - channel) * ratio);
  });
  return `#${channels.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`;
}

/* ------------------------------------------------------------------------- *
 * 投影与数据归一化
 * ------------------------------------------------------------------------- */

/** 标准 30° 等距投影（不含任何平移，便于把爆炸偏移当成线性平移叠加）。 */
function projectPoint(x, y, z, unit = 26) {
  return {
    x: (x - z) * COS30 * unit,
    y: ((x + z) * SIN30 - y) * unit,
  };
}

/** 把爆炸向量投影成屏幕平移量。 */
function explodeDelta(explode, unit) {
  const [dx, dy, dz] = explode;
  return {
    x: (dx - dz) * COS30 * unit,
    y: ((dx + dz) * SIN30 - dy) * unit,
  };
}

function normalizePart(raw, index) {
  if (!raw || raw.id === undefined || raw.id === null) return null;
  return {
    id: String(raw.id),
    label: String(raw.label ?? raw.id),
    shape: PART_SHAPES.includes(raw.shape) ? raw.shape : 'box',
    x: num(raw.x, 0),
    y: num(raw.y, 0),
    z: num(raw.z, 0),
    w: Math.max(0.05, num(raw.w, 1)),
    h: Math.max(0.05, num(raw.h, 1)),
    d: Math.max(0.05, num(raw.d, 1)),
    color: parseColor(raw.color, ['#8fa7ae', '#b7c2c6', '#d9a45c', '#c98645', '#6e8790'][index % 5]),
    opacity: clamp(num(raw.opacity, 1), 0.15, 1),
    shell: Boolean(raw.shell),
    explode: vec3(raw.explode),
    description: raw.description === undefined || raw.description === null ? '' : String(raw.description),
  };
}

/** 自定义引线标注：text 支持字符串（\n 换行）或字符串数组；无 part_id 时用 anchor 画自由标注。 */
function normalizeLabel(raw, index, partIds) {
  if (!raw || typeof raw !== 'object') return null;
  const rawText = raw.text === undefined || raw.text === null ? '' : raw.text;
  const lines = (Array.isArray(rawText) ? rawText : String(rawText).split(/\r?\n/))
    .map((line) => String(line === undefined || line === null ? '' : line).trim())
    .filter(Boolean);
  if (!lines.length) return null;
  const partRaw = raw.part_id ?? raw.part ?? raw.target ?? '';
  const partId = partIds.has(String(partRaw)) ? String(partRaw) : '';
  const anchor = Array.isArray(raw.anchor) && raw.anchor.length >= 3
    ? [num(raw.anchor[0], 0), num(raw.anchor[1], 0), num(raw.anchor[2], 0)]
    : null;
  if (!partId && !anchor) return null;
  return {
    id: raw.id === undefined || raw.id === null ? `label${index + 1}` : String(raw.id),
    partId,
    lines,
    anchor,
    side: raw.side === 'left' ? -1 : (raw.side === 'right' ? 1 : 0),
  };
}

function normalizeStepList(rawSteps, partIds) {
  const raw = Array.isArray(rawSteps) ? rawSteps.filter((step) => step && typeof step === 'object') : [];
  return raw.map((step, index) => ({
    label: step.label === undefined || step.label === null ? '' : String(step.label),
    detail: step.detail === undefined || step.detail === null ? '' : String(step.detail),
    part_ids: (Array.isArray(step.part_ids) ? step.part_ids : (step.part_id === undefined ? [] : [step.part_id]))
      .map(String)
      .filter((id) => partIds.has(id)),
    phase: step.phase === 'assemble' ? 'assemble' : (step.phase === 'disassemble' ? 'disassemble' : ''),
    offset: Number.isFinite(Number(step.offset)) ? clamp(Number(step.offset), 0, MAX_EXPLODE_FACTOR) : null,
    index,
  }));
}

/** 对外：整理等距数据，拆出拆解/装配两段步骤。 */
function normalizeIso(data) {
  const source = data && typeof data === 'object' ? data : {};
  const parts = (Array.isArray(source.parts) ? source.parts : [])
    .map((raw, index) => normalizePart(raw, index))
    .filter(Boolean);
  const partIds = new Set(parts.map((part) => part.id));
  const labels = (Array.isArray(source.labels) ? source.labels : [])
    .map((raw, index) => normalizeLabel(raw, index, partIds))
    .filter(Boolean);
  const steps = normalizeStepList(source.steps, partIds);
  const declarePhase = steps.some((step) => step.phase);
  if (declarePhase) {
    for (const step of steps) if (!step.phase) step.phase = 'disassemble';
  }
  const disassembly = steps.filter((step) => step.phase === 'disassemble');
  const assembly = steps.filter((step) => step.phase === 'assemble');
  const hasDirection = disassembly.length > 0 && assembly.length > 0;
  const unit = clamp(num(source.unit, 26), 6, 120);
  const notes = (Array.isArray(source.notes) ? source.notes : [])
    .map((note) => String(note || '').trim())
    .filter(Boolean);
  const style = STYLES.includes(source.style) ? source.style : 'clean';

  // 部件在拆解/装配段中的「第几步被拆下/装回」，用于计算爆炸进度。
  const removalAt = new Map();
  disassembly.forEach((step, index) => {
    for (const id of step.part_ids) if (!removalAt.has(id)) removalAt.set(id, index);
  });
  const installAt = new Map();
  assembly.forEach((step, index) => {
    for (const id of step.part_ids) if (!installAt.has(id)) installAt.set(id, index);
  });

  return {
    parts,
    labels,
    steps,
    disassembly,
    assembly,
    hasDirection,
    removalAt,
    installAt,
    unit,
    style,
    notes,
  };
}

/* ------------------------------------------------------------------------- *
 * 几何
 * ------------------------------------------------------------------------- */

function partCorners(part) {
  const x0 = part.x - part.w / 2;
  const x1 = part.x + part.w / 2;
  const y0 = part.y - part.h / 2;
  const y1 = part.y + part.h / 2;
  const z0 = part.z - part.d / 2;
  const z1 = part.z + part.d / 2;
  return {
    x0, x1, y0, y1, z0, z1,
    // 8 个角，顺序固定，便于取面。
    p000: { x: x0, y: y0, z: z0 },
    p100: { x: x1, y: y0, z: z0 },
    p010: { x: x0, y: y1, z: z0 },
    p110: { x: x1, y: y1, z: z0 },
    p001: { x: x0, y: y0, z: z1 },
    p101: { x: x1, y: y0, z: z1 },
    p011: { x: x0, y: y1, z: z1 },
    p111: { x: x1, y: y1, z: z1 },
  };
}

function projectCorners(part, unit) {
  const corners = partCorners(part);
  const projected = {};
  for (const key of ['p000', 'p100', 'p010', 'p110', 'p001', 'p101', 'p011', 'p111']) {
    projected[key] = projectPoint(corners[key].x, corners[key].y, corners[key].z, unit);
  }
  return { corners, projected };
}

function facePoints(projected, keys) {
  return keys.map((key) => `${projected[key].x.toFixed(2)},${projected[key].y.toFixed(2)}`).join(' ');
}

function isRoundPart(part) {
  return part.shape === 'cylinder' || part.shape === 'gear';
}

/** 等距圆盘近似：投影后是轴对齐椭圆，半宽 = √2·cos30·r，半高 = √2·sin30·r。 */
function roundGeometry(part, unit) {
  const radius = (part.w + part.d) / 4;
  const rx = Math.SQRT2 * COS30 * radius * unit;
  const ry = Math.SQRT2 * SIN30 * radius * unit;
  const center = projectPoint(part.x, part.y, part.z, unit);
  return {
    radius: radius * unit,
    rx,
    ry,
    center,
    top: { x: center.x, y: center.y - (part.h / 2) * unit },
    bottom: { x: center.x, y: center.y + (part.h / 2) * unit },
  };
}

function ellipsePoint(center, rx, ry, theta) {
  return [center.x + rx * Math.cos(theta), center.y + ry * Math.sin(theta)];
}

function polygonString(points) {
  return points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
}

/** 部件的屏幕包围盒（形状感知：圆盘类用椭圆范围，其余用 8 角投影范围）。 */
function projectExtents(part, unit) {
  if (isRoundPart(part)) {
    const geometry = roundGeometry(part, unit);
    return {
      minX: geometry.center.x - geometry.rx,
      maxX: geometry.center.x + geometry.rx,
      minY: geometry.top.y - geometry.ry,
      maxY: geometry.bottom.y + geometry.ry,
    };
  }
  const { projected } = projectCorners(part, unit);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of Object.values(projected)) {
    minX = Math.min(minX, point.x);
    maxX = Math.max(maxX, point.x);
    minY = Math.min(minY, point.y);
    maxY = Math.max(maxY, point.y);
  }
  return { minX, minY, maxX, maxY };
}

function partBounds(part, unit, delta) {
  const extents = projectExtents(part, unit);
  const dx = delta ? delta.x : 0;
  const dy = delta ? delta.y : 0;
  return {
    minX: extents.minX + dx,
    maxX: extents.maxX + dx,
    minY: extents.minY + dy,
    maxY: extents.maxY + dy,
  };
}

/* ------------------------------------------------------------------------- *
 * 标签布局（支持 labels[].text 多行与自由标注）
 * ------------------------------------------------------------------------- */

function placeLabels(parts, unit, customLabels = []) {
  const customByPart = new Map();
  for (const label of customLabels) {
    if (label.partId) customByPart.set(label.partId, label);
  }
  const items = parts.map((part) => {
    const center = projectPoint(part.x, part.y, part.z, unit);
    const bounds = partBounds(part, unit, null);
    const custom = customByPart.get(part.id);
    const lines = custom ? custom.lines.slice() : [part.label];
    const textWidth = Math.max(24, ...lines.map((line) => [...line].length * 12 + 8));
    return {
      key: part.id,
      part,
      free: false,
      center,
      halfWidth: Math.max(8, (bounds.maxX - bounds.minX) / 2),
      side: 0,
      sideHint: custom ? custom.side : 0,
      labelX: 0,
      labelY: 0,
      textWidth,
      lines,
    };
  });
  for (const label of customLabels) {
    if (label.partId || !label.anchor) continue;
    items.push({
      key: label.id,
      part: null,
      free: true,
      center: projectPoint(label.anchor[0], label.anchor[1], label.anchor[2], unit),
      halfWidth: 6,
      side: 0,
      sideHint: label.side,
      labelX: 0,
      labelY: 0,
      textWidth: Math.max(24, ...label.lines.map((line) => [...line].length * 12 + 8)),
      lines: label.lines.slice(),
    });
  }
  const partsOnly = items.filter((item) => !item.free);
  const centerX = partsOnly.length
    ? (Math.min(...partsOnly.map((item) => item.center.x)) + Math.max(...partsOnly.map((item) => item.center.x))) / 2
    : 0;
  for (const item of items) item.side = item.sideHint || (item.center.x < centerX ? -1 : 1);
  for (const side of [-1, 1]) {
    const list = items.filter((item) => item.side === side).sort((a, b) => a.center.y - b.center.y);
    let previousBottom = -Infinity;
    for (const item of list) {
      // 多行标注按文本块高度避让：单行沿用旧的 20px 行距；多行块之间额外留
      // 一个行高左右的间距，避免相邻多行标注在视觉上连成一段（15px 行距 + 9px）。
      const halfHeight = ((item.lines.length - 1) * 15) / 2;
      const gap = item.lines.length > 1 ? 24 : 20;
      let labelY = item.center.y - 10;
      if (labelY - halfHeight < previousBottom + gap) labelY = previousBottom + gap + halfHeight;
      previousBottom = labelY + halfHeight;
      item.labelY = labelY;
      item.labelX = item.center.x + side * (item.halfWidth + 44);
    }
  }
  return items;
}

function labelAnchor(item) {
  const direction = item.side;
  return {
    x: item.labelX - direction * 6,
    y: item.labelY - 4,
  };
}

function isoBounds(model, labels) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const part of model.parts) {
    const base = partBounds(part, model.unit, null);
    const exploded = partBounds(part, model.unit, explodeDelta(part.explode, model.unit));
    minX = Math.min(minX, base.minX, exploded.minX);
    minY = Math.min(minY, base.minY, exploded.minY);
    maxX = Math.max(maxX, base.maxX, exploded.maxX);
    maxY = Math.max(maxY, base.maxY, exploded.maxY);
  }
  for (const item of labels) {
    const labelRight = item.labelX + item.side * item.textWidth;
    const halfText = ((item.lines.length - 1) * 15) / 2;
    minX = Math.min(minX, item.labelX, labelRight);
    maxX = Math.max(maxX, item.labelX, labelRight);
    minY = Math.min(minY, item.labelY - 14 - halfText);
    maxY = Math.max(maxY, item.labelY + 10 + halfText);
  }
  if (!Number.isFinite(minX)) {
    minX = 0;
    minY = 0;
    maxX = 640;
    maxY = 360;
  }
  return { minX, minY, maxX, maxY };
}

/* ------------------------------------------------------------------------- *
 * SVG 构建：部件形状
 * ------------------------------------------------------------------------- */

function faceEl(tag, attrs, echo) {
  const merged = { ...attrs, class: `pd-iso-face${echo ? ' pd-iso-face-echo' : ''}` };
  if (echo) merged.fill = 'none';
  return svgEl(tag, merged);
}

/** box / plate / rod：沿用三可见面画法（plate=扁 box、rod=长条 box）。 */
function boxFaceElements(part, unit, baseColor, echo) {
  const { projected } = projectCorners(part, unit);
  const faces = [
    { keys: ['p010', 'p110', 'p111', 'p011'], fill: tint(baseColor, 0.3), stroke: tint(baseColor, -0.45) },
    { keys: ['p100', 'p110', 'p111', 'p101'], fill: tint(baseColor, -0.1), stroke: tint(baseColor, -0.45) },
    { keys: ['p001', 'p101', 'p111', 'p011'], fill: tint(baseColor, -0.26), stroke: tint(baseColor, -0.45) },
  ];
  return faces.map((face) => faceEl('polygon', {
    points: facePoints(projected, face.keys),
    fill: echo ? 'none' : face.fill,
    stroke: face.stroke,
    'stroke-width': 0.8,
    'stroke-linejoin': 'round',
  }, echo));
}

/** cylinder：上/下椭圆 + 侧壁（等距近似，轴沿 y 方向）。 */
function cylinderFaceElements(part, unit, baseColor, echo) {
  const geometry = roundGeometry(part, unit);
  const { rx, ry, top, bottom } = geometry;
  const sideFill = tint(baseColor, -0.16);
  const topFill = tint(baseColor, 0.28);
  const stroke = tint(baseColor, -0.45);
  const sidePoints = [
    ellipsePoint(top, rx, ry, 0),
    ellipsePoint(bottom, rx, ry, 0),
  ];
  const steps = 16;
  for (let index = 1; index <= steps; index += 1) {
    sidePoints.push(ellipsePoint(bottom, rx, ry, (index / steps) * Math.PI));
  }
  sidePoints.push(ellipsePoint(top, rx, ry, Math.PI));
  const elements = [
    faceEl('polygon', {
      points: polygonString(sidePoints),
      fill: echo ? 'none' : sideFill,
      stroke,
      'stroke-width': 0.8,
      'stroke-linejoin': 'round',
    }, echo),
  ];
  // 底部椭圆的下半圈描边：强化圆柱落地感，不填充。
  const rimPoints = [];
  for (let index = 0; index <= steps; index += 1) {
    rimPoints.push(ellipsePoint(bottom, rx, ry, (index / steps) * Math.PI));
  }
  elements.push(faceEl('polyline', {
    points: polygonString(rimPoints),
    fill: 'none',
    stroke,
    'stroke-width': 0.7,
    'stroke-linejoin': 'round',
    'stroke-linecap': 'round',
  }, echo));
  elements.push(faceEl('ellipse', {
    cx: top.x.toFixed(2),
    cy: top.y.toFixed(2),
    rx: rx.toFixed(2),
    ry: ry.toFixed(2),
    fill: echo ? 'none' : topFill,
    stroke,
    'stroke-width': 0.8,
  }, echo));
  return elements;
}

/** gear：等距圆盘 + 齿块 + 轴孔（在 cylinder 基础上加顶面齿与中心孔）。 */
function gearFaceElements(part, unit, baseColor, echo) {
  const geometry = roundGeometry(part, unit);
  const { rx, ry, top, bottom } = geometry;
  const sideFill = tint(baseColor, -0.2);
  const topFill = tint(baseColor, 0.26);
  const toothFill = tint(baseColor, 0.08);
  const stroke = tint(baseColor, -0.45);
  const sidePoints = [
    ellipsePoint(top, rx, ry, 0),
    ellipsePoint(bottom, rx, ry, 0),
  ];
  const steps = 16;
  for (let index = 1; index <= steps; index += 1) {
    sidePoints.push(ellipsePoint(bottom, rx, ry, (index / steps) * Math.PI));
  }
  sidePoints.push(ellipsePoint(top, rx, ry, Math.PI));
  const elements = [
    faceEl('polygon', {
      points: polygonString(sidePoints),
      fill: echo ? 'none' : sideFill,
      stroke,
      'stroke-width': 0.8,
      'stroke-linejoin': 'round',
    }, echo),
  ];
  // 齿块：外圈略大于盘面的梯形，先画侧面齿、再盖顶面盘，只露出外缘。
  const teeth = 14;
  const outerScale = 1.12;
  const toothGap = 0.34;
  const toothElements = [];
  for (let index = 0; index < teeth; index += 1) {
    const start = (index / teeth) * Math.PI * 2 + toothGap / 2;
    const end = ((index + 1) / teeth) * Math.PI * 2 - toothGap / 2;
    const points = [
      ellipsePoint(top, rx, ry, start),
      ellipsePoint(top, rx * outerScale, ry * outerScale, start),
      ellipsePoint(top, rx * outerScale, ry * outerScale, end),
      ellipsePoint(top, rx, ry, end),
    ];
    toothElements.push(faceEl('polygon', {
      points: polygonString(points),
      fill: echo ? 'none' : toothFill,
      stroke,
      'stroke-width': 0.7,
      'stroke-linejoin': 'round',
    }, echo));
  }
  elements.push(...toothElements);
  elements.push(faceEl('ellipse', {
    cx: top.x.toFixed(2),
    cy: top.y.toFixed(2),
    rx: rx.toFixed(2),
    ry: ry.toFixed(2),
    fill: echo ? 'none' : topFill,
    stroke,
    'stroke-width': 0.8,
  }, echo));
  // 轴孔：中心小椭圆（更深的同心圆）；半径按坐标单位计算后再投影，避免重复乘 unit。
  const boreRadius = Math.max(0.06, ((part.w + part.d) / 4) * 0.26);
  elements.push(faceEl('ellipse', {
    cx: top.x.toFixed(2),
    cy: top.y.toFixed(2),
    rx: (Math.SQRT2 * COS30 * boreRadius * unit).toFixed(2),
    ry: (Math.SQRT2 * SIN30 * boreRadius * unit).toFixed(2),
    fill: echo ? 'none' : tint(baseColor, -0.55),
    stroke,
    'stroke-width': 0.7,
  }, echo));
  return elements;
}

function shapeElements(part, unit, baseColor, echo) {
  if (part.shape === 'cylinder') return cylinderFaceElements(part, unit, baseColor, echo);
  if (part.shape === 'gear') return gearFaceElements(part, unit, baseColor, echo);
  // box / plate / rod 都是长方体画法（plate=扁 box、rod=长条 box）；未知形状已回退 box。
  return boxFaceElements(part, unit, baseColor, echo);
}

function drawPart(item, model) {
  const { part } = item;
  const baseColor = parseColor(part.color);
  // 只有非缺省形状才加形状类/属性：box 部件保持与旧版一致的 DOM。
  const shapeClass = part.shape === 'box' ? '' : ` pd-iso-shape-${part.shape}`;
  const group = svgEl('g', {
    class: `pd-iso-part${shapeClass}${part.shell ? ' is-shell' : ''}`
      + `${model.style === 'sketch' ? ' is-sketch' : ''}`,
    'data-part': part.id,
    'data-part-shape': part.shape === 'box' ? undefined : part.shape,
    opacity: part.opacity < 1 ? part.opacity : undefined,
  });
  const visual = model.style === 'sketch' ? svgEl('g', { class: 'pd-iso-part-visual' }) : null;
  if (visual) {
    // 以部件投影中心为旋转中心做轻微手绘倾斜。
    const center = projectPoint(part.x, part.y, part.z, model.unit);
    const seed = `iso:${part.id}`;
    visual.setAttribute('transform', `rotate(${sketchRotation(seed).toFixed(2)} ${center.x.toFixed(2)} ${center.y.toFixed(2)})`);
    visual.append(...shapeElements(part, model.unit, baseColor, false));
    group.append(visual);
  } else {
    group.append(...shapeElements(part, model.unit, baseColor, false));
  }
  if (model.style === 'sketch') {
    const center = projectPoint(part.x, part.y, part.z, model.unit);
    const seed = `iso:${part.id}|echo`;
    const jitterX = makeJitter(`${seed}|x`);
    const jitterY = makeJitter(`${seed}|y`);
    const echo = svgEl('g', {
      class: 'pd-iso-part-echo',
      transform: `translate(${jitterX(1.2).toFixed(2)},${jitterY(1.2).toFixed(2)}) `
        + `rotate(${sketchRotation(seed).toFixed(2)} ${center.x.toFixed(2)} ${center.y.toFixed(2)})`,
    });
    echo.append(...shapeElements(part, model.unit, baseColor, true));
    group.append(echo);
  }

  // 引线与标签放到独立的顶层图层（见 drawLabelItem），避免标签被后画的部件面遮住。
  const title = [part.label, part.description].filter(Boolean).join('：');
  group.append(svgEl('title', {}, [document.createTextNode(title)]));
  return group;
}

/** 单个部件的标签组（引线 + 圆点 + 文本，可多行），单独成层，永远画在所有部件之上。 */
function drawLabelItem(item) {
  const anchor = labelAnchor(item);
  const startX = item.center.x + item.side * item.halfWidth * 0.85;
  const startY = item.center.y - 10;
  const group = svgEl('g', item.free
    ? { class: 'pd-iso-label-item is-free', 'data-label': item.key }
    : { class: 'pd-iso-label-item', 'data-label-part': item.part.id });
  group.append(svgEl('line', {
    class: 'pd-iso-leader',
    x1: startX,
    y1: startY,
    x2: anchor.x,
    y2: anchor.y,
  }));
  group.append(svgEl('circle', {
    class: 'pd-iso-leader-dot',
    cx: startX,
    cy: startY,
    r: 2,
  }));
  const textX = anchor.x + (item.side < 0 ? -4 : 4);
  const textAttrs = {
    class: 'pd-iso-label',
    x: textX,
    y: anchor.y,
    'text-anchor': item.side < 0 ? 'end' : 'start',
    'dominant-baseline': 'middle',
  };
  if (item.lines.length === 1) {
    // 单行标注沿用旧版的单一 text 节点（DOM 结构与旧渲染一致）。
    group.append(textEl(item.lines[0], textAttrs));
    return group;
  }
  const text = svgEl('text', textAttrs);
  const lineHeight = 15;
  const firstY = anchor.y - ((item.lines.length - 1) * lineHeight) / 2;
  item.lines.forEach((line, index) => {
    text.append(svgEl('tspan', { x: textX, y: firstY + index * lineHeight }, [document.createTextNode(line)]));
  });
  group.append(text);
  return group;
}

function buildIsoSvg(model, options = {}) {
  const { poster = false, title = '', footer = '', ariaLabel = '', staticMode = false } = options;
  const labels = placeLabels(model.parts, model.unit, model.labels);
  const bounds = isoBounds(model, labels);
  const padding = 26;
  let minX = bounds.minX - padding;
  let minY = bounds.minY - padding;
  let maxX = bounds.maxX + padding;
  let maxY = bounds.maxY + padding;
  if (title) minY -= 42;
  if (footer) maxY += 30;

  const classNames = ['pd-iso-svg'];
  if (poster) classNames.push('is-poster');
  if (model.style === 'sketch') classNames.push('is-sketch');
  const root = svgEl('svg', {
    viewBox: `${minX.toFixed(1)} ${minY.toFixed(1)} ${(maxX - minX).toFixed(1)} ${(maxY - minY).toFixed(1)}`,
    class: classNames.join(' '),
    role: 'img',
    'aria-label': ariaLabel || title || 'principle isometric diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });

  if (title) {
    root.append(textEl(title, {
      class: 'pd-iso-title',
      x: (minX + maxX) / 2,
      y: minY + 26,
      'text-anchor': 'middle',
    }));
  }

  // 绘制顺序：半透明外壳先画，再按 x+y+z 从远到近画实体部件（等距画家算法）。
  const ordered = [...labels].sort((a, b) => {
    const shellA = a.part && a.part.shell ? 0 : 1;
    const shellB = b.part && b.part.shell ? 0 : 1;
    if (shellA !== shellB) return shellA - shellB;
    const sumA = a.part ? a.part.x + a.part.y + a.part.z : 0;
    const sumB = b.part ? b.part.x + b.part.y + b.part.z : 0;
    return sumA - sumB;
  });
  const partsLayer = svgEl('g', { class: 'pd-iso-parts' });
  for (const item of ordered) if (item.part) partsLayer.append(drawPart(item, model));
  root.append(partsLayer);

  // 标签层最后追加：标签/引线不会被任何部件面遮住，步骤高亮也作用在标签组上。
  const labelLayer = svgEl('g', { class: 'pd-iso-labels' });
  for (const item of labels) labelLayer.append(drawLabelItem(item));
  root.append(labelLayer);

  if (footer) {
    root.append(textEl(footer, {
      class: 'pd-iso-footer',
      x: (minX + maxX) / 2,
      y: maxY - 10,
      'text-anchor': 'middle',
    }));
  }
  root.dataset.staticMode = staticMode ? 'true' : 'false';
  return root;
}

/* ------------------------------------------------------------------------- *
 * 爆炸进度
 * ------------------------------------------------------------------------- */

function targetsFor(model, direction, stepIndex) {
  const factors = new Map(model.parts.map((part) => [part.id, 0]));
  if (!model.parts.length) return factors;
  if (!model.hasDirection) {
    // 单序列（3D 降级）：视为装配序列——已出现的部件就位，未出现的保持分离。
    const installed = new Set();
    const steps = model.steps;
    for (let index = 0; index <= stepIndex && index < steps.length; index += 1) {
      for (const id of steps[index].part_ids) installed.add(id);
    }
    for (const part of model.parts) factors.set(part.id, installed.has(part.id) ? 0 : 1);
    return factors;
  }
  if (direction === 'assemble') {
    const steps = model.assembly;
    const step = steps[stepIndex] || null;
    for (const part of model.parts) {
      const installIndex = model.installAt.get(part.id);
      if (installIndex === undefined) {
        factors.set(part.id, 0);
        continue;
      }
      let factor = installIndex <= stepIndex ? 0 : 1;
      if (installIndex === stepIndex && step && step.offset !== null) factor = step.offset;
      factors.set(part.id, factor);
    }
    return factors;
  }
  const steps = model.disassembly;
  const step = steps[stepIndex] || null;
  for (const part of model.parts) {
    const removalIndex = model.removalAt.get(part.id);
    if (removalIndex === undefined) {
      factors.set(part.id, 0);
      continue;
    }
    let factor = removalIndex <= stepIndex ? 1 : 0;
    if (removalIndex === stepIndex && step && step.offset !== null) factor = step.offset;
    factors.set(part.id, factor);
  }
  return factors;
}

function initialFactors(model, direction) {
  if (!model.hasDirection) return targetsFor(model, null, 0);
  // 切到装配方向时从「完全拆开」开始，切到拆解方向时从「完全装好」开始。
  const value = direction === 'assemble' ? 1 : 0;
  return new Map(model.parts.map((part) => [part.id, value]));
}

function easeOutCubic(value) {
  const t = clamp(value, 0, 1);
  return 1 - (1 - t) ** 3;
}

/* ------------------------------------------------------------------------- *
 * 对外 API：视图与海报
 * ------------------------------------------------------------------------- */

/**
 * 在 host 内创建等距视图（播放时钟由统一播放器控制）。
 * @param {HTMLElement} host
 * @param {object} data
 * @param {{ staticMode?: boolean, ariaLabel?: string, title?: string }} [options]
 * @returns {{mode:'svg', element:SVGElement, getStepCount:()=>number, getStep:(i:number)=>object,
 *   supportsDirection:boolean, getDirection:()=>('disassemble'|'assemble'|null),
 *   setDirection:(d:'disassemble'|'assemble')=>boolean, setStep:(i:number, opts?:{animate?:boolean})=>void,
 *   destroy:()=>void}}
 */
function createIsoView(host, data, options = {}) {
  const model = normalizeIso(data);
  const staticMode = Boolean(options.staticMode);
  const ariaLabel = options.ariaLabel || options.title || 'principle isometric diagram';
  const root = buildIsoSvg(model, { ariaLabel, staticMode });
  const groupByPart = new Map();
  const labelGroupByPart = new Map();
  for (const group of root.querySelectorAll('[data-part]')) groupByPart.set(group.getAttribute('data-part'), group);
  for (const group of root.querySelectorAll('[data-label-part]')) {
    labelGroupByPart.set(group.getAttribute('data-label-part'), group);
  }

  let direction = model.hasDirection && !staticMode ? 'disassemble' : null;
  let stepIndex = 0;
  let factors = staticMode ? new Map(model.parts.map((part) => [part.id, 0])) : initialFactors(model, direction);
  let tweenFrame = 0;

  const deltas = new Map(model.parts.map((part) => [part.id, explodeDelta(part.explode, model.unit)]));

  function stepsForCurrent() {
    if (!model.hasDirection || staticMode) return model.steps;
    return direction === 'assemble' ? model.assembly : model.disassembly;
  }

  function paintFactors() {
    for (const part of model.parts) {
      const factor = clamp(factors.get(part.id) || 0, 0, MAX_EXPLODE_FACTOR);
      const delta = deltas.get(part.id) || { x: 0, y: 0 };
      const transform = `translate(${(delta.x * factor).toFixed(2)},${(delta.y * factor).toFixed(2)})`;
      groupByPart.get(part.id)?.setAttribute('transform', transform);
      // 标签层与部件层分开平移，保证标签始终可见、并与部件同步运动。
      labelGroupByPart.get(part.id)?.setAttribute('transform', transform);
    }
  }

  function paintActive() {
    const steps = stepsForCurrent();
    const step = steps[stepIndex] || null;
    const active = new Set(step ? step.part_ids : []);
    for (const [id, group] of groupByPart) group.classList.toggle('is-active', active.has(id));
    for (const [id, group] of labelGroupByPart) group.classList.toggle('is-active', active.has(id));
    root.classList.toggle('has-active', active.size > 0);
    root.dataset.direction = direction || '';
    root.dataset.step = String(stepIndex);
  }

  function stopTween() {
    if (tweenFrame) {
      cancelAnimationFrame(tweenFrame);
      tweenFrame = 0;
    }
  }

  function animateTo(target, duration = 320, onRendered = null) {
    stopTween();
    const start = new Map(factors);
    const startTime = performance.now();
    const tick = (now) => {
      const progress = duration > 0 ? easeOutCubic((now - startTime) / duration) : 1;
      for (const part of model.parts) {
        const from = start.get(part.id) || 0;
        const to = target.get(part.id) || 0;
        factors.set(part.id, from + (to - from) * progress);
      }
      paintFactors();
      if (progress < 1) {
        tweenFrame = requestAnimationFrame(tick);
      } else {
        tweenFrame = 0;
        factors = new Map(target);
        if (typeof onRendered === 'function') onRendered();
      }
    };
    tweenFrame = requestAnimationFrame(tick);
  }

  function applyStep(index, { animate = true, onRendered = null } = {}) {
    const steps = stepsForCurrent();
    if (!steps.length) {
      paintActive();
      if (typeof onRendered === 'function') onRendered();
      return;
    }
    stepIndex = clamp(Number(index) || 0, 0, steps.length - 1);
    const target = targetsFor(model, direction, stepIndex);
    if (staticMode) {
      factors = new Map(model.parts.map((part) => [part.id, 0]));
      paintFactors();
      paintActive();
      if (typeof onRendered === 'function') onRendered();
    } else if (animate) {
      animateTo(target, 320, () => {
        paintActive();
        if (typeof onRendered === 'function') onRendered();
      });
      paintActive();
    } else {
      stopTween();
      factors = new Map(target);
      paintFactors();
      paintActive();
      if (typeof onRendered === 'function') onRendered();
    }
  }

  host.replaceChildren(root);
  applyStep(0, { animate: false });

  return {
    mode: 'svg',
    element: root,
    // 告诉统一播放器：等距视图有 320ms 补间，应在补间完成后才算「渲染完成」。
    deferredFrame: true,
    getStepCount: () => stepsForCurrent().length,
    getStep: (index) => {
      const steps = stepsForCurrent();
      const safe = clamp(Number(index) || 0, 0, Math.max(0, steps.length - 1));
      return steps[safe] || {};
    },
    getNotes: () => model.notes,
    getStyle: () => model.style,
    supportsDirection: model.hasDirection && !staticMode,
    getDirection: () => direction,
    setDirection(next) {
      if (!model.hasDirection || staticMode) return false;
      const target = next === 'assemble' ? 'assemble' : 'disassemble';
      if (target === direction) return false;
      stopTween();
      direction = target;
      stepIndex = 0;
      factors = initialFactors(model, direction);
      paintFactors();
      applyStep(0, { animate: true });
      return true;
    },
    setStep: applyStep,
    destroy() {
      stopTween();
      root.remove();
      host.replaceChildren();
    },
  };
}

/**
 * 无交互静态海报（导出 / 降级用）。
 * @returns {SVGSVGElement}
 */
function renderIsoPoster(data, options = {}) {
  return buildIsoSvg(normalizeIso(data), {
    poster: true,
    title: options.title ? String(options.title) : '',
    footer: options.footer ? String(options.footer) : '',
    ariaLabel: options.ariaLabel ? String(options.ariaLabel) : '',
    staticMode: true,
  });
}
return {
  createIsoView: createIsoView,
  renderIsoPoster: renderIsoPoster,
};
})();

global.ZhixuOfflineDemo = {
  createFlowView: __zhixuFlow.createFlowView,
  renderFlowPoster: __zhixuFlow.renderFlowPoster,
  createIsoView: __zhixuIso.createIsoView,
  renderIsoPoster: __zhixuIso.renderIsoPoster,
  OFFLINE_BUNDLE_VERSION: "1",
  OFFLINE_BUNDLE_SOURCE: "web/vendor/principles/flow.js + iso.js",
};
})(window);
