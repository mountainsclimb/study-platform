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

const NS = 'http://www.w3.org/2000/svg';
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
export function projectPoint(x, y, z, unit = 26) {
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
export function normalizeIso(data) {
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
export function createIsoView(host, data, options = {}) {
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
export function renderIsoPoster(data, options = {}) {
  return buildIsoSvg(normalizeIso(data), {
    poster: true,
    title: options.title ? String(options.title) : '',
    footer: options.footer ? String(options.footer) : '',
    ariaLabel: options.ariaLabel ? String(options.ariaLabel) : '',
    staticMode: true,
  });
}
