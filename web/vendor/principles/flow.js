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

const NS = 'http://www.w3.org/2000/svg';

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
export function normalizeFlow(data) {
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
export function createFlowView(host, data, options = {}) {
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
export function renderFlowPoster(data, options = {}) {
  return buildFlowSvg(normalizeFlow(data), {
    poster: true,
    title: options.title ? String(options.title) : '',
    footer: options.footer ? String(options.footer) : '',
    ariaLabel: options.ariaLabel ? String(options.ariaLabel) : '',
  });
}
