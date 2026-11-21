/**
 * 结构化流程渲染器（SVG，无外部依赖）。
 *
 * 数据契约（与 SeeCode 类流程 JSON 对齐）：
 * {
 *   nodes: [{ id, label, kind?: 'start'|'process'|'branch'|'end', x?, y? }],
 *   edges: [{ id?, from, to, label? }],
 *   steps: [{ label, detail?, node_id?, node_ids?, edge_ids? }],
 *   notes?: string[],
 *   stepIntervalMs?: number
 * }
 *
 * 布局：节点同时给出 x/y 时按给定坐标（教学排版坐标）绘制；否则按 edges 做简单
 * 分层（Kahn 拓扑层 + 环上节点兜底），层内居中排列。步骤高亮对应节点/边。
 *
 * 安全性：所有文本都用 textContent / createTextNode 写入，不拼接 innerHTML；
 * 数据非法的节点/边会被忽略而不是抛错，便于播放器降级。
 */

const NS = 'http://www.w3.org/2000/svg';

const NODE_SIZE = {
  start: { w: 132, h: 46 },
  end: { w: 132, h: 46 },
  process: { w: 150, h: 52 },
  branch: { w: 156, h: 84 },
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

function nodeSize(node) {
  return NODE_SIZE[node.kind] || NODE_SIZE.process;
}

/* ------------------------------------------------------------------------- *
 * 数据归一化与布局
 * ------------------------------------------------------------------------- */

function normalizeNode(raw, index) {
  if (!raw || raw.id === undefined || raw.id === null) return null;
  const kind = ['start', 'process', 'branch', 'end'].includes(raw.kind) ? raw.kind : 'process';
  return {
    id: String(raw.id),
    label: String(raw.label ?? raw.id),
    kind,
    x: Number.isFinite(Number(raw.x)) ? Number(raw.x) : null,
    y: Number.isFinite(Number(raw.y)) ? Number(raw.y) : null,
    order: index,
  };
}

function normalizeEdges(rawEdges, nodeById) {
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
    edges.push({ id, from, to, label: raw.label ? String(raw.label) : '' });
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
    maxColumnWidth = Math.max(maxColumnWidth, ...items.map((node) => nodeSize(node).w));
  }
  for (const [col, items] of columns.entries()) {
    let cursorY = 90;
    for (const node of items) {
      node.x = 46 + maxColumnWidth / 2 + col * (maxColumnWidth + columnGap);
      node.y = cursorY + nodeSize(node).h / 2;
      cursorY += nodeSize(node).h + rowGap;
    }
  }
}

function hasExplicitLayout(nodes) {
  return nodes.length > 0 && nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y));
}

function layoutModel(nodes, edges) {
  if (!hasExplicitLayout(nodes)) autoLayout(nodes, edges);
  for (const node of nodes) {
    const size = nodeSize(node);
    node.w = size.w;
    node.h = size.h;
    node.cx = Number(node.x);
    node.cy = Number(node.y);
  }
}

/* ------------------------------------------------------------------------- *
 * 步骤归一化
 * ------------------------------------------------------------------------- */

function normalizeSteps(rawSteps, nodes, edges) {
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edgeIds = new Set(edges.map((edge) => edge.id));
  const cleanIds = (value, allowed) => {
    const list = Array.isArray(value) ? value : (value === undefined || value === null ? [] : [value]);
    return [...new Set(list.map(String).filter((id) => allowed.has(id)))];
  };
  const raw = Array.isArray(rawSteps) ? rawSteps.filter((step) => step && typeof step === 'object') : [];
  if (!raw.length) {
    // 没有 steps 时按节点顺序补一组「逐节点」步骤，保证播放器仍可单步演示。
    return nodes.map((node) => ({
      label: node.label,
      detail: '',
      node_id: node.id,
      node_ids: [node.id],
      edge_ids: edges.filter((edge) => edge.from === node.id).map((edge) => edge.id),
    }));
  }
  return raw.map((step, index) => {
    const primary = step.node_id === undefined || step.node_id === null ? '' : String(step.node_id);
    const primaryValid = nodeIds.has(primary) ? primary : '';
    const nodeList = cleanIds(step.node_ids, nodeIds);
    if (primaryValid && !nodeList.includes(primaryValid)) nodeList.unshift(primaryValid);
    return {
      label: step.label === undefined || step.label === null ? '' : String(step.label),
      detail: step.detail === undefined || step.detail === null ? '' : String(step.detail),
      node_id: primaryValid,
      node_ids: nodeList,
      edge_ids: cleanIds(step.edge_ids, edgeIds),
      index,
    };
  });
}

/** 对外：把任意流程数据整理为渲染器可用的模型（便于测试与复用）。 */
export function normalizeFlow(data) {
  const source = data && typeof data === 'object' ? data : {};
  const nodes = (Array.isArray(source.nodes) ? source.nodes : [])
    .map((raw, index) => normalizeNode(raw, index))
    .filter(Boolean);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edges = normalizeEdges(source.edges, nodeById);
  layoutModel(nodes, edges);
  const steps = normalizeSteps(source.steps, nodes, edges);
  const notes = (Array.isArray(source.notes) ? source.notes : [])
    .map((note) => String(note || '').trim())
    .filter(Boolean);
  const interval = Number(source.stepIntervalMs);
  return {
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

function anchorPoints(from, to) {
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

function edgeGeometry(edge, byId, loopY) {
  const from = byId.get(edge.from);
  const to = byId.get(edge.to);
  if (!from || !to) return null;
  // 目标在源左侧视为回环/反向边：改走底部绕行，避免穿过中间节点。
  const back = to.cx < from.cx - 1;
  if (back) {
    // 回环边：从源节点底部向下绕行，再从目标节点底部回到目标，避免穿过中间节点。
    const sx = from.cx;
    const sy = from.cy + from.h / 2 + 2;
    const tx = to.cx;
    const ty = to.cy + to.h / 2 + 2;
    const bendY = loopY;
    // 终点再下移 7px：箭头尖留在节点外侧，否则会被节点填充色盖住。
    const tipY = ty + 7;
    return {
      d: `M ${sx} ${sy} C ${sx} ${bendY} ${tx} ${bendY} ${tx} ${tipY}`,
      arrow: { x: tx, y: tipY, angle: -Math.PI / 2 },
      label: { x: (sx + tx) / 2, y: bendY - 8 },
      back: true,
    };
  }
  const points = anchorPoints(from, to);
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
  // 标签放到节点外侧：水平边在节点上方，竖直边在右侧，斜边在中点外侧。
  let label;
  if (Math.abs(dy) <= 8) {
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
    d: `M ${points.ax} ${points.ay} C ${c1x} ${c1y} ${c2x} ${c2y} ${tipX} ${tipY}`,
    arrow: { x: tipX, y: tipY, angle: Math.atan2(uy, ux) },
    label,
    back: false,
  };
}

/* ------------------------------------------------------------------------- *
 * SVG 构建
 * ------------------------------------------------------------------------- */

function drawNode(node) {
  const group = svgEl('g', {
    class: `pd-flow-node pd-flow-${node.kind}`,
    'data-node': node.id,
    transform: `translate(${node.cx},${node.cy})`,
  });
  const halfW = node.w / 2;
  const halfH = node.h / 2;
  let shape;
  if (node.kind === 'branch') {
    shape = svgEl('polygon', {
      class: 'pd-flow-shape',
      points: `0,${-halfH} ${halfW},0 0,${halfH} ${-halfW},0`,
    });
  } else {
    const radius = node.kind === 'process' ? 10 : halfH;
    shape = svgEl('rect', {
      class: 'pd-flow-shape',
      x: -halfW,
      y: -halfH,
      width: node.w,
      height: node.h,
      rx: radius,
      ry: radius,
    });
  }
  group.append(shape);

  // 节点标签：正文用 textContent + tspan 折行；超过两行省略。
  const maxChars = node.kind === 'branch' ? 7 : 8;
  const lines = wrapLabel(node.label, maxChars, 2);
  const lineHeight = 17;
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
  group.append(text);
  group.append(svgEl('title', {}, [document.createTextNode(`${node.label}（${node.kind}）`)]));
  return group;
}

function drawEdge(edge, geometry) {
  const group = svgEl('g', { class: 'pd-flow-edge', 'data-edge': edge.id });
  group.append(svgEl('path', { class: 'pd-flow-edge-line', d: geometry.d }));
  group.append(svgEl('polygon', {
    class: 'pd-flow-arrowhead',
    points: arrowPolygon(geometry.arrow.x, geometry.arrow.y, geometry.arrow.angle),
  }));
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

function computeViewBox(model, { title = '', footer = '' } = {}) {
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
  const padding = 34;
  const hasBack = model.edges.some((edge) => {
    const from = model.nodes.find((node) => node.id === edge.from);
    const to = model.nodes.find((node) => node.id === edge.to);
    return from && to && to.cx < from.cx - 1;
  });
  const loopExtra = hasBack ? 78 : 0;
  const titleSpace = title ? 44 : 0;
  const footerSpace = footer ? 30 : 0;
  const x = minX - padding;
  const y = minY - padding - titleSpace;
  const width = Math.max(320, maxX - minX + padding * 2);
  const height = Math.max(220, maxY - minY + padding * 2 + loopExtra + titleSpace + footerSpace);
  return { x, y, width, height, titleSpace, footerSpace, loopExtra };
}

/**
 * 生成流程 SVG。
 * @param normalized normalizeFlow() 的结果
 * @param {object} options `{ poster?, title?, footer?, ariaLabel? }`
 * @returns {SVGSVGElement}
 */
function buildFlowSvg(normalized, options = {}) {
  const { poster = false, title = '', footer = '', ariaLabel = '' } = options;
  const box = computeViewBox(normalized, { title, footer });
  const byId = new Map(normalized.nodes.map((node) => [node.id, node]));
  const loopY = box.y + box.height - box.footerSpace - 28;

  const root = svgEl('svg', {
    viewBox: `${box.x} ${box.y} ${box.width} ${box.height}`,
    class: `pd-flow-svg${poster ? ' is-poster' : ''}`,
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

  const edgeLayer = svgEl('g', { class: 'pd-flow-edges' });
  for (const edge of normalized.edges) {
    const geometry = edgeGeometry(edge, byId, loopY);
    if (geometry) edgeLayer.append(drawEdge(edge, geometry));
  }
  root.append(edgeLayer);

  const nodeLayer = svgEl('g', { class: 'pd-flow-nodes' });
  for (const node of normalized.nodes) nodeLayer.append(drawNode(node));
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
