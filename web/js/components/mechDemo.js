/**
 * M5 机械 3D / 工厂仿真演示组件（方案 §5.4「机械与工厂」/ §8 M5）。
 *
 * 对外导出：
 * - createMechView(container, demo, options)    机械 3D：拆解 / 剖切 / 运转 / 读数；
 * - createFactoryView(container, demo, options) 工厂仿真：指标卡 / 三工位流程 / 队列曲线 /
 *                                                事件时间轴回放；
 * - openMechDemo(options)                       题目页等入口的统一弹层（模板选择 + 只读参数 +
 *                                                固定种子说明 + 保存为学习附件）。
 *
 * 依赖策略（硬性约束）：
 * - three.js 只从仓库内本地文件 `web/vendor/interactive/three.module.min.js` 动态 import，
 *   不新增依赖、不访问 CDN；该文件是仓库既有的 three.js r186 IIFE 构建（执行后暴露
 *   globalThis.THREE），因此同时兼容 `module.THREE` 与全局 `THREE`。
 * - 没有 WebGL、three.js 加载失败或运行中丢上下文时，自动降级为本地 SVG 等距示意图
 *   并给出中文提示（getMode() 返回 "svg"），绝不把异常抛到页面。
 * - 不拼接 innerHTML、不使用 eval / new Function；所有服务端字段只经 textContent /
 *   setAttribute / 几何数值使用，颜色与尺寸都先做白名单与数值收敛。
 *
 * 数据契约（与后端 /api/demos/build、/api/demos/simulate 一致，字段全部容错）：
 * - mech_3d：{version,basis,seed,parts[],steps[],readouts[],camera,assumptions,source_note}
 * - factory_sim：{version,basis,seed,flow:{nodes[],edges[]},station_labels[],metrics,events[]}
 * 组件本身不计算公式、不伪造指标：读数与 metrics 原样展示，固定种子只用于说明可复现，
 * 工厂事件按时间回放（前端不做随机模拟）。
 */

import { el, toast } from '../core/dom.js';
import { api } from '../core/api.js';
import { t } from '../core/i18n.js';
import { getMechPreset, mechTemplateItems, MECH_PRESETS } from '../../vendor/principles/mechPresets.js';

const THREE_URL = new URL('../../vendor/interactive/three.module.min.js', import.meta.url).href;

const MECH_KIND = 'mech_3d';
const FACTORY_KIND = 'factory_sim';
const DEMO_KINDS = new Set([MECH_KIND, FACTORY_KIND]);
const SPEED_OPTIONS = [0.5, 1, 2];
const SVG_NS = 'http://www.w3.org/2000/svg';
const PART_PALETTE = ['#8fa7ae', '#b7c2c6', '#d9a45c', '#c98645', '#6e8790', '#a9c6cf'];
const ISO_COS30 = Math.cos(Math.PI / 6);
const ISO_SIN30 = 0.5;
const FACTORY_STATE_KEYS = {
  idle: { key: 'study.m5.factory.stateIdle', fallback: '空闲' },
  busy: { key: 'study.m5.factory.stateBusy', fallback: '忙碌' },
  blocked: { key: 'study.m5.factory.stateBlocked', fallback: '阻塞' },
};
const FACTORY_EVENT_KEYS = {
  arrival: { key: 'study.m5.factory.eventArrival', fallback: '到达' },
  start: { key: 'study.m5.factory.eventStart', fallback: '开始加工' },
  service_start: { key: 'study.m5.factory.eventStart', fallback: '开始加工' },
  finish: { key: 'study.m5.factory.eventFinish', fallback: '完成' },
  service_end: { key: 'study.m5.factory.eventFinish', fallback: '完成' },
  blocked: { key: 'study.m5.factory.eventBlocked', fallback: '阻塞' },
  unblock: { key: 'study.m5.factory.eventUnblock', fallback: '恢复' },
  queue: { key: 'study.m5.factory.eventQueue', fallback: '队列更新' },
  idle: { key: 'study.m5.factory.eventIdle', fallback: '空闲' },
};

const ARRIVAL_TYPES = new Set(['arrival', 'arrive', 'item_arrival', 'in', 'enter', 'source']);
const START_TYPES = new Set(['start', 'process', 'processing', 'service_start', 'begin', 'busy', 'dequeue']);
const FINISH_TYPES = new Set(['finish', 'finished', 'done', 'complete', 'completed', 'service_end', 'depart', 'departure', 'out']);
const BLOCK_TYPES = new Set(['block', 'blocked', 'block_start']);
const UNBLOCK_TYPES = new Set(['unblock', 'unblocked', 'resume', 'block_end']);
const IDLE_TYPES = new Set(['idle', 'starved', 'wait', 'waiting']);

/** three.js 动态加载共享 Promise（失败后重置，允许其它演示重试）。 */
let threePromise = null;

/* ------------------------------------------------------------------------- *
 * 通用小工具
 * ------------------------------------------------------------------------- */

/** M5 文案：locale 缺键时回退中文常量并做 `{name}` 插值（不直接拼 t('字面量')）。 */
function mt(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function clamp(value, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number)) return min;
  return Math.max(min, Math.min(max, number));
}

function num(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function vec3(value, fallback = [0, 0, 0]) {
  if (!Array.isArray(value)) return [...fallback];
  return [num(value[0], fallback[0]), num(value[1], fallback[1]), num(value[2], fallback[2])];
}

function normalizeAxis(value, fallback) {
  const axis = vec3(value, fallback);
  const length = Math.hypot(axis[0], axis[1], axis[2]);
  if (!Number.isFinite(length) || length < 1e-6) return vec3(fallback, [0, 1, 0]);
  return [axis[0] / length, axis[1] / length, axis[2] / length];
}

function colorText(value, fallback) {
  const raw = String(value === undefined || value === null ? '' : value).trim();
  if (/^#[0-9a-f]{3}$/i.test(raw)) {
    return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}`.toLowerCase();
  }
  if (/^#[0-9a-f]{6}$/i.test(raw)) return raw.toLowerCase();
  return fallback;
}

/** 颜色明暗混合（SVG 等距视图做简单明暗面；只接受已归一化的 #rrggbb）。 */
function shadeColor(color, amount) {
  const base = colorText(color, '#8fa7ae');
  const target = amount >= 0 ? 255 : 8;
  const ratio = Math.min(1, Math.abs(amount));
  const channels = [1, 3, 5].map((index) => {
    const channel = parseInt(base.slice(index, index + 2), 16);
    return Math.round(channel + (target - channel) * ratio);
  });
  return `#${channels.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`;
}

function displayValue(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (Array.isArray(value)) return value.map((item) => displayValue(item)).join(' / ');
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return '—';
    }
  }
  return String(value);
}

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function projectIso(x, y, z, unit = 1) {
  return {
    x: (x - z) * ISO_COS30 * unit,
    y: ((x + z) * ISO_SIN30 - y) * unit,
  };
}

function kindLabel(kind) {
  if (kind === MECH_KIND) return mt('study.m5.kind.mech3d', '机械 3D');
  if (kind === FACTORY_KIND) return mt('study.m5.kind.factorySim', '工厂仿真');
  return mt('study.m5.kind.unknown', '机械/工厂演示');
}

function basisLabel(basis) {
  const key = String(basis || '').trim();
  if (key === 'local-teaching') return mt('study.m5.basis.localTeaching', '本地教学示意');
  if (key === 'deterministic-des') return mt('study.m5.basis.deterministic', '确定性离散事件（固定种子可复现）');
  return key || '';
}

function seedNote(seed) {
  if (seed === null || seed === undefined || seed === '') return '';
  return mt('study.m5.seedNote', '固定种子 {v1} · 固定种子可复现', { v1: seed });
}

function metaNode({ seed, basis, assumptions, sourceNote }) {
  const host = el('div', { class: 'm5-meta' });
  const chips = el('div', { class: 'm5-chips' });
  const seedText = seedNote(seed);
  if (seedText) chips.append(el('span', { class: 'm5-chip m5-chip-seed' }, seedText));
  const basisText = basisLabel(basis);
  if (basisText) chips.append(el('span', { class: 'm5-chip' }, basisText));
  if (chips.childNodes.length) host.append(chips);
  const note = String(sourceNote || '').trim();
  if (note) host.append(el('p', { class: 'm5-source', 'data-no-i18n': 'true' }, note));
  const list = (Array.isArray(assumptions) ? assumptions : [])
    .map((item) => String(item === undefined || item === null ? '' : item).trim())
    .filter(Boolean);
  if (list.length) {
    host.append(el('details', { class: 'm5-assumptions' },
      el('summary', {}, mt('study.m5.assumptions', '假设与说明')),
      el('ul', {}, ...list.map((item) => el('li', { 'data-no-i18n': 'true' }, item)))));
  }
  return host;
}

/* ------------------------------------------------------------------------- *
 * three.js 加载与 WebGL 能力探测
 * ------------------------------------------------------------------------- */

/** 检测 WebGL 是否可用（探测后立即释放上下文，避免占用 GPU 上下文配额）。 */
export function isWebGLAvailable() {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) return false;
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) lose.loseContext();
    return true;
  } catch {
    return false;
  }
}

/** 动态 import 本地 three.js：同时兼容 IIFE 全局 THREE 与 ESM 导出。 */
async function loadThree() {
  const existing = globalThis.THREE;
  if (existing && typeof existing.WebGLRenderer === 'function') return existing;
  if (!threePromise) {
    threePromise = import(THREE_URL)
      .then((module) => {
        const THREE = module?.THREE || globalThis.THREE;
        if (!THREE || typeof THREE.WebGLRenderer !== 'function') {
          throw new Error('本地 three.js 未正确导出');
        }
        return THREE;
      })
      .catch((error) => {
        threePromise = null;
        throw error;
      });
  }
  return threePromise;
}

/* ------------------------------------------------------------------------- *
 * 机械数据归一化
 * ------------------------------------------------------------------------- */

const MECH_PART_KINDS = new Set(['box', 'cylinder', 'gear', 'rod', 'plate']);

function defaultAxisFor(kind) {
  if (kind === 'rod') return [1, 0, 0];
  // 后端 contract：gear / cylinder 的 size=[直径, 直径, 厚度]，圆盘面在 XY 平面，轴向为 Z。
  if (kind === 'gear' || kind === 'cylinder') return [0, 0, 1];
  return [0, 1, 0];
}

function normalizeMechPart(raw, index) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.id === undefined || raw.id === null || String(raw.id) === '') return null;
  const kindRaw = String(raw.kind || raw.shape || 'box').toLowerCase();
  const kind = MECH_PART_KINDS.has(kindRaw) ? kindRaw : 'box';
  const sizeRaw = vec3(raw.size, [1, 1, 1]);
  const size = sizeRaw.map((value) => Math.max(0.02, value));
  let radius = Number(raw.radius);
  let length = Number(raw.length);
  if (kind === 'rod') {
    if (!Number.isFinite(radius)) radius = Math.max(0.02, Math.min(size[1], size[2]) / 2);
    if (!Number.isFinite(length)) length = size[0];
  } else if (kind === 'gear') {
    if (!Number.isFinite(radius)) radius = Math.max(0.04, Math.max(size[0], size[1]) / 2);
    if (!Number.isFinite(length)) length = Math.max(0.04, size[2]);
  } else if (kind === 'cylinder') {
    if (!Number.isFinite(radius)) radius = Math.max(0.02, Math.min(size[0], size[1]) / 2);
    if (!Number.isFinite(length)) length = Math.max(0.04, size[2]);
  } else {
    radius = Number.isFinite(radius) ? Math.abs(radius) : 0;
    length = Number.isFinite(length) ? Math.abs(length) : 0;
  }
  const teethRaw = Number(raw.teeth);
  const spinRaw = Number(raw.spin);
  const opacityRaw = Number(raw.opacity);
  return {
    id: String(raw.id),
    label: String(raw.label === undefined || raw.label === null ? raw.id : raw.label),
    kind,
    position: vec3(raw.position, [0, 0, 0]),
    size,
    radius: Math.max(0.02, radius),
    length: Math.max(0.02, length),
    axis: normalizeAxis(raw.axis, defaultAxisFor(kind)),
    color: colorText(raw.color, PART_PALETTE[index % PART_PALETTE.length]),
    parent: raw.parent === undefined || raw.parent === null || raw.parent === '' ? '' : String(raw.parent),
    teeth: Number.isFinite(teethRaw) ? clamp(Math.round(teethRaw), 6, 48) : 0,
    spin: Number.isFinite(spinRaw) ? spinRaw : null,
    opacity: Number.isFinite(opacityRaw) ? clamp(opacityRaw, 0.15, 1) : 1,
    explode: Array.isArray(raw.explode) ? vec3(raw.explode, [0, 0, 0]) : null,
  };
}

function normalizeMechSteps(rawSteps, partIds) {
  const list = Array.isArray(rawSteps) ? rawSteps : [];
  const steps = [];
  for (const raw of list) {
    if (!raw || typeof raw !== 'object') continue;
    const ids = (Array.isArray(raw.part_ids) ? raw.part_ids : (raw.part_id === undefined ? [] : [raw.part_id]))
      .map((id) => String(id))
      .filter((id) => partIds.has(id));
    steps.push({
      label: String(raw.label === undefined || raw.label === null ? '' : raw.label),
      detail: String(raw.detail || raw.description || ''),
      part_ids: ids,
      explode: clamp(raw.explode === undefined || raw.explode === null ? 0 : raw.explode, 0, 1),
    });
  }
  if (!steps.length) {
    steps.push({
      label: mt('study.m5.mech.defaultStep', '整机总览'),
      detail: '',
      part_ids: [...partIds],
      explode: 0,
    });
  }
  return steps.map((step, index) => {
    const explode = Number.isFinite(Number(step.explode))
      ? step.explode
      : (steps.length > 1 ? index / (steps.length - 1) : 0);
    return { ...step, explode: clamp(explode, 0, 1) };
  });
}

function normalizeReadouts(rawReadouts) {
  const list = Array.isArray(rawReadouts) ? rawReadouts : [];
  return list
    .filter((item) => item && typeof item === 'object')
    .map((item, index) => ({
      id: String(item.id === undefined || item.id === null ? `readout-${index + 1}` : item.id),
      label: String(item.label === undefined || item.label === null ? item.id || '' : item.label),
      value: item.value,
      unit: String(item.unit === undefined || item.unit === null ? '' : item.unit),
      note: String(item.note === undefined || item.note === null ? '' : item.note),
    }));
}

/** 按 parent 依赖做拓扑排序：父件先建，子件后挂；循环依赖时断开 parent。 */
function orderPartsByParent(parts) {
  const map = new Map(parts.map((part) => [part.id, part]));
  const ordered = [];
  const state = new Map();
  const visit = (part) => {
    if (!part) return;
    const status = state.get(part.id);
    if (status === 2) return;
    if (status === 1) {
      part.parent = '';
      return;
    }
    state.set(part.id, 1);
    if (part.parent && map.has(part.parent)) visit(map.get(part.parent));
    state.set(part.id, 2);
    ordered.push(part);
  };
  for (const part of parts) visit(part);
  return ordered;
}

/**
 * 判断子件的 position 是否按「世界坐标」书写（兼容部分服务端部件树）。
 * 契约约定 parent 存在时 position 为相对坐标；但服务端教学模板里部分子件把 position
 * 写成了与父件同一坐标系的世界坐标（例如箱盖 x 与箱体 x 完全相同）。
 * 只有出现「父件某轴偏移非零，且子件该轴坐标与父件完全相同」这种强特征时才按世界坐标处理，
 * 其余仍按契约的相对坐标处理，避免误伤本地预设。
 */
function looksAbsoluteAuthored(partPosition, parentPosition) {
  for (let axis = 0; axis < 3; axis += 1) {
    const parentValue = parentPosition[axis];
    if (Math.abs(parentValue) > 1e-6 && Math.abs(partPosition[axis] - parentValue) < 1e-6) {
      return true;
    }
  }
  return false;
}

/** 计算每件世界坐标：默认「父件相对定位」，world 坐标书写的子件按世界坐标处理。 */
function computeWorldPositions(orderedParts) {
  const world = new Map();
  for (const part of orderedParts) {
    const parent = part.parent ? world.get(part.parent) : null;
    if (part.absoluteAuthored) {
      world.set(part.id, { x: part.position[0], y: part.position[1], z: part.position[2] });
      continue;
    }
    world.set(part.id, {
      x: part.position[0] + (parent ? parent.x : 0),
      y: part.position[1] + (parent ? parent.y : 0),
      z: part.position[2] + (parent ? parent.z : 0),
    });
  }
  return world;
}

/** 每件在父件坐标系下的局部位置（Three.js 挂到父对象时使用）。 */
function computeLocalPositions(orderedParts, world) {
  const local = new Map();
  for (const part of orderedParts) {
    if (!part.parent || !world.has(part.parent)) {
      local.set(part.id, { x: part.position[0], y: part.position[1], z: part.position[2] });
      continue;
    }
    const parentWorld = world.get(part.parent);
    const worldPoint = world.get(part.id);
    local.set(part.id, {
      x: worldPoint.x - parentWorld.x,
      y: worldPoint.y - parentWorld.y,
      z: worldPoint.z - parentWorld.z,
    });
  }
  return local;
}

/**
 * 拆解偏移：数据显式给 `explode` 时直接用；否则按「父件相对定位」推导——
 * 有 parent 的部件沿自身局部位移方向（零位移时沿回转轴）滑出，顶层部件从模型中心向外散开。
 * 只做视觉分离，不改变任何工程读数。
 */
function computeExplodeOffsets(orderedParts, world, localPositions) {
  const offsets = new Map();
  const center = { x: 0, y: 0, z: 0 };
  for (const part of orderedParts) {
    const point = world.get(part.id);
    center.x += point.x;
    center.y += point.y;
    center.z += point.z;
  }
  const count = orderedParts.length || 1;
  center.x /= count;
  center.y /= count;
  center.z /= count;
  orderedParts.forEach((part, index) => {
    if (part.explode) {
      offsets.set(part.id, [...part.explode]);
      return;
    }
    let direction;
    if (part.parent) {
      const local = localPositions.get(part.id) || { x: 0, y: 0, z: 0 };
      const localLength = Math.hypot(local.x, local.y, local.z);
      direction = localLength > 0.08
        ? [local.x / localLength, local.y / localLength, local.z / localLength]
        : [...part.axis];
    } else {
      const point = world.get(part.id);
      const dx = point.x - center.x;
      const dy = point.y - center.y;
      const dz = point.z - center.z;
      const distance = Math.hypot(dx, dy, dz);
      direction = distance > 0.3 ? [dx / distance, dy / distance, dz / distance] : [...part.axis];
    }
    const directionLength = Math.hypot(direction[0], direction[1], direction[2]) || 1;
    let magnitude = 0.9 + index * 0.07;
    if (part.kind === 'box' || part.kind === 'plate') magnitude *= 0.72;
    if (part.parent) magnitude *= 0.82;
    offsets.set(part.id, [
      (direction[0] / directionLength) * magnitude,
      (direction[1] / directionLength) * magnitude,
      (direction[2] / directionLength) * magnitude,
    ]);
  });
  return offsets;
}

/** 归一化 mech_3d 数据；缺少 parts 时抛错，由调用方渲染中文降级。 */
function normalizeMechData(rawData, demo) {
  const source = rawData && typeof rawData === 'object' ? rawData : {};
  const parts = (Array.isArray(source.parts) ? source.parts : [])
    .map((raw, index) => normalizeMechPart(raw, index))
    .filter(Boolean);
  if (!parts.length) throw new Error('机械演示数据缺少 parts');
  const byId = new Map(parts.map((part) => [part.id, part]));
  for (const part of parts) {
    if (part.parent && !byId.has(part.parent)) {
      part.parent = '';
      continue;
    }
    if (part.parent) {
      part.absoluteAuthored = looksAbsoluteAuthored(part.position, byId.get(part.parent).position);
    }
  }
  const orderedParts = orderPartsByParent(parts);
  const worldPositions = computeWorldPositions(orderedParts);
  const localPositions = computeLocalPositions(orderedParts, worldPositions);
  const partIds = new Set(parts.map((part) => part.id));
  const steps = normalizeMechSteps(source.steps, partIds);
  const readouts = normalizeReadouts(source.readouts);
  const camera = source.camera && typeof source.camera === 'object' ? source.camera : {};
  const bounds = { minX: 0, maxX: 0, minY: 0, maxY: 0, minZ: 0, maxZ: 0 };
  let first = true;
  for (const part of orderedParts) {
    const point = worldPositions.get(part.id);
    const radius = part.kind === 'box' || part.kind === 'plate'
      ? Math.max(part.size[0], part.size[1], part.size[2]) / 2
      : part.radius + part.length / 2;
    for (const axis of ['x', 'y', 'z']) {
      const value = point[axis];
      if (first) {
        bounds[`min${axis.toUpperCase()}`] = value - radius;
        bounds[`max${axis.toUpperCase()}`] = value + radius;
      } else {
        bounds[`min${axis.toUpperCase()}`] = Math.min(bounds[`min${axis.toUpperCase()}`], value - radius);
        bounds[`max${axis.toUpperCase()}`] = Math.max(bounds[`max${axis.toUpperCase()}`], value + radius);
      }
    }
    first = false;
  }
  return {
    title: String(demo?.title || source.title || mt('study.m5.mech.title', '机械 3D 演示')),
    seed: demo?.seed !== undefined && demo?.seed !== null ? demo.seed : (source.seed ?? null),
    basis: String(source.basis || ''),
    version: num(source.version, 1),
    parts,
    orderedParts,
    worldPositions,
    localPositions,
    explodeOffsets: computeExplodeOffsets(orderedParts, worldPositions, localPositions),
    steps,
    readouts,
    camera: {
      position: Array.isArray(camera.position) ? vec3(camera.position, [4, 3, 5]) : null,
      target: Array.isArray(camera.target) ? vec3(camera.target, [0, 0, 0]) : null,
    },
    bounds,
    assumptions: Array.isArray(source.assumptions) ? source.assumptions : [],
    sourceNote: String(source.source_note || ''),
  };
}

/* ------------------------------------------------------------------------- *
 * 本地 SVG 等距示意图（无 WebGL / three 加载失败的降级视图）
 * ------------------------------------------------------------------------- */

function partExtent(part) {
  if (part.kind === 'box' || part.kind === 'plate') {
    return { x: part.size[0] / 2, y: part.size[1] / 2, z: part.size[2] / 2 };
  }
  return {
    x: part.radius + Math.abs(part.axis[0]) * part.length / 2,
    y: part.radius + Math.abs(part.axis[1]) * part.length / 2,
    z: part.radius + Math.abs(part.axis[2]) * part.length / 2,
  };
}

function isoCorners(center, extent) {
  const corners = [];
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      for (const sz of [-1, 1]) {
        corners.push([center.x + sx * extent.x, center.y + sy * extent.y, center.z + sz * extent.z]);
      }
    }
  }
  return corners;
}

function drawIsoBox(svg, part, center, extent, project, active) {
  const corner = (sx, sy, sz) => project(
    center.x + sx * extent.x,
    center.y + sy * extent.y,
    center.z + sz * extent.z,
  );
  const points = (list) => list.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  const base = part.color;
  const faces = [
    {
      keys: [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]],
      fill: shadeColor(base, -0.16),
    },
    {
      keys: [[1, -1, -1], [1, -1, 1], [1, 1, 1], [1, 1, -1]],
      fill: shadeColor(base, -0.34),
    },
    {
      keys: [[-1, 1, -1], [1, 1, -1], [1, 1, 1], [-1, 1, 1]],
      fill: shadeColor(base, 0.2),
    },
  ];
  for (const face of faces) {
    svg.append(svgEl('polygon', {
      points: points(face.keys.map(([sx, sy, sz]) => corner(sx, sy, sz))),
      fill: face.fill,
      stroke: shadeColor(base, -0.45),
      'stroke-width': active ? 1.5 : 0.7,
      'stroke-linejoin': 'round',
    }));
  }
}

function drawIsoRound(svg, part, center, project, active) {
  const axisEnd = project(
    center.x + part.axis[0] * part.length / 2,
    center.y + part.axis[1] * part.length / 2,
    center.z + part.axis[2] * part.length / 2,
  );
  const middle = project(center.x, center.y, center.z);
  const dx = axisEnd.x - middle.x;
  const dy = axisEnd.y - middle.y;
  const halfLength = Math.hypot(dx, dy);
  const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
  const scale = Math.max(1, project.unit || 1);
  const radius = Math.max(2, part.radius * scale);
  const squash = part.kind === 'rod' ? 0.42 : 0.56;
  const rx = radius;
  const ry = Math.max(1.4, radius * squash);
  const base = part.color;
  const stroke = shadeColor(base, -0.45);
  const strokeWidth = active ? 1.5 : 0.7;
  const group = svgEl('g', {
    class: 'm5-iso-round',
    transform: `translate(${middle.x.toFixed(2)} ${middle.y.toFixed(2)}) rotate(${angle.toFixed(2)})`,
    stroke,
    'stroke-width': strokeWidth,
    'stroke-linejoin': 'round',
  });
  // 圆柱侧面（旋转后的矩形），再补两端椭圆，形成简单的等距圆柱。
  group.append(svgEl('rect', {
    x: (-halfLength).toFixed(2),
    y: (-ry).toFixed(2),
    width: Math.max(0.5, halfLength * 2).toFixed(2),
    height: (ry * 2).toFixed(2),
    rx: Math.min(ry, 6).toFixed(2),
    fill: shadeColor(base, -0.08),
  }));
  group.append(svgEl('ellipse', {
    cx: halfLength.toFixed(2),
    cy: '0',
    rx: rx.toFixed(2),
    ry: ry.toFixed(2),
    fill: shadeColor(base, 0.16),
  }));
  group.append(svgEl('ellipse', {
    cx: (-halfLength).toFixed(2),
    cy: '0',
    rx: rx.toFixed(2),
    ry: ry.toFixed(2),
    fill: shadeColor(base, -0.22),
  }));
  if (part.kind === 'gear') {
    const teeth = part.teeth > 0 ? part.teeth : 14;
    const inner = 0.9;
    const outer = 1.16;
    for (let index = 0; index < teeth; index += 1) {
      const theta = (index / teeth) * Math.PI * 2;
      group.append(svgEl('line', {
        x1: (halfLength + Math.cos(theta) * rx * inner).toFixed(2),
        y1: (Math.sin(theta) * ry * inner).toFixed(2),
        x2: (halfLength + Math.cos(theta) * rx * outer).toFixed(2),
        y2: (Math.sin(theta) * ry * outer).toFixed(2),
        stroke,
        'stroke-width': active ? 1.6 : 1,
      }));
    }
    group.append(svgEl('circle', {
      cx: halfLength.toFixed(2),
      cy: '0',
      r: Math.max(1.4, radius * 0.28).toFixed(2),
      fill: shadeColor(base, -0.3),
      stroke,
    }));
  }
  group.append(svgEl('title', {}, part.label));
  svg.append(group);
}

/** 生成整张等距示意图（纯函数；explode / 剖切 / 高亮由 viewState 决定）。 */
function renderMechIsoSvg(model, viewState) {
  const width = 760;
  const height = 440;
  const padding = 54;
  const explode = clamp(viewState.explode, 0, 1);
  const activeIds = new Set(Array.isArray(viewState.activeIds) ? viewState.activeIds : []);
  const projectedParts = model.orderedParts.map((part) => {
    const base = model.worldPositions.get(part.id);
    const offset = model.explodeOffsets.get(part.id) || [0, 0, 0];
    const center = {
      x: base.x + offset[0] * explode,
      y: base.y + offset[1] * explode,
      z: base.z + offset[2] * explode,
    };
    return { part, center, extent: partExtent(part) };
  });
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const item of projectedParts) {
    for (const corner of isoCorners(item.center, item.extent)) {
      const point = projectIso(corner[0], corner[1], corner[2], 1);
      minX = Math.min(minX, point.x);
      maxX = Math.max(maxX, point.x);
      minY = Math.min(minY, point.y);
      maxY = Math.max(maxY, point.y);
    }
  }
  const spanX = Math.max(0.5, maxX - minX);
  const spanY = Math.max(0.5, maxY - minY);
  const unit = clamp(Math.min((width - padding * 2) / spanX, (height - padding * 2) / spanY), 8, 140);
  const offsetX = width / 2 - ((minX + maxX) / 2) * unit;
  const offsetY = height / 2 - ((minY + maxY) / 2) * unit;
  const project = (x, y, z) => {
    const point = projectIso(x, y, z, 1);
    return { x: point.x * unit + offsetX, y: point.y * unit + offsetY };
  };
  project.unit = unit;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'm5-iso-svg',
    role: 'img',
    'aria-label': mt('study.m5.mech.isoAria', '{v1}（等距示意图）', { v1: model.title }),
    preserveAspectRatio: 'xMidYMid meet',
  });
  const sorted = [...projectedParts].sort((a, b) => {
    const depthA = (a.center.x + a.center.z) * 10 + a.center.y;
    const depthB = (b.center.x + b.center.z) * 10 + b.center.y;
    return depthA - depthB;
  });
  const cutX = viewState.clip
    ? model.bounds.maxX - (model.bounds.maxX - model.bounds.minX) * clamp(viewState.clipFraction, 0, 1)
    : null;
  for (const item of sorted) {
    const active = activeIds.has(item.part.id);
    const clipped = cutX !== null && item.center.x > cutX;
    const group = svgEl('g', {
      class: `m5-iso-part${active ? ' is-active' : ''}${clipped ? ' is-clipped' : ''}`,
      opacity: clipped ? 0.16 : 1,
    });
    if (item.part.kind === 'box' || item.part.kind === 'plate') {
      drawIsoBox(group, item.part, item.center, item.extent, project, active);
    } else {
      drawIsoRound(group, item.part, item.center, project, active);
    }
    group.append(svgEl('title', {}, item.part.label));
    svg.append(group);
  }
  // 当前步骤部件标注：只给高亮部件画引线标签，避免整图文字拥挤。
  const labeled = projectedParts.filter((item) => activeIds.has(item.part.id)).slice(0, 6);
  for (const item of labeled) {
    const point = project(item.center.x, item.center.y, item.center.z);
    const labelY = point.y - Math.max(20, item.extent.y * unit + 14);
    svg.append(svgEl('line', {
      x1: point.x.toFixed(2),
      y1: (point.y - 4).toFixed(2),
      x2: point.x.toFixed(2),
      y2: labelY.toFixed(2),
      class: 'm5-iso-leader',
    }));
    svg.append(svgEl('text', {
      x: point.x.toFixed(2),
      y: labelY.toFixed(2),
      class: 'm5-iso-label',
      'text-anchor': 'middle',
    }, item.part.label));
  }
  if (cutX !== null) {
    svg.append(svgEl('text', {
      x: padding,
      y: height - 16,
      class: 'm5-iso-cut-note',
    }, mt('study.m5.mech.cutNote', '剖切示意：切面 X ≈ {v1}（示意图为整体隐去，非真实裁剪）', {
      v1: cutX.toFixed(2),
    })));
  }
  svg.append(svgEl('text', {
    x: width - 14,
    y: 20,
    class: 'm5-iso-watermark',
    'text-anchor': 'end',
  }, mt('study.m5.mech.isoBadge', '本地等距示意图')));
  return svg;
}

/* ------------------------------------------------------------------------- *
 * Three.js 机械视图
 * ------------------------------------------------------------------------- */

/**
 * 创建 Three.js 机械场景（初始化即构建；失败由调用方降级）。
 * @param {object} THREE 本地 three.js 命名空间
 * @param {object} model normalizeMechData 结果
 * @param {HTMLElement} host 画布容器
 * @param {{onContextLost?: Function}} hooks
 */
function createThreeMech(THREE, model, host, hooks = {}) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch {
    throw new Error('无法创建 WebGL 渲染器');
  }
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 16 / 9, 0.1, 2000);
  const root = new THREE.Group();
  scene.add(root);

  scene.add(new THREE.AmbientLight(0xffffff, 0.62));
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.55);
  keyLight.position.set(14, 22, 16);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xbcd4dc, 0.48);
  fillLight.position.set(-16, 8, -12);
  scene.add(fillLight);

  const geometries = [];
  const materials = [];
  const entries = new Map();
  const spins = [];

  function pushMaterial(material) {
    materials.push(material);
    return material;
  }

  function createMaterial(part) {
    return pushMaterial(new THREE.MeshStandardMaterial({
      color: new THREE.Color(part.color),
      roughness: 0.62,
      metalness: part.kind === 'gear' || part.kind === 'rod' ? 0.3 : 0.12,
      transparent: part.opacity < 0.999,
      opacity: part.opacity,
      depthWrite: part.opacity >= 0.999,
    }));
  }

  function buildGear(part, material) {
    const group = new THREE.Group();
    const bodyGeometry = new THREE.CylinderGeometry(part.radius, part.radius, part.length, 28);
    geometries.push(bodyGeometry);
    group.add(new THREE.Mesh(bodyGeometry, material));
    const teeth = part.teeth > 0 ? part.teeth : clamp(Math.round(part.radius * 16), 10, 40);
    const toothHeight = Math.max(0.04, part.radius * 0.16);
    const tangent = Math.max(0.04, (Math.PI * 2 * part.radius / teeth) * 0.5);
    const toothGeometry = new THREE.BoxGeometry(tangent, Math.max(0.02, part.length * 0.9), toothHeight);
    geometries.push(toothGeometry);
    for (let index = 0; index < teeth; index += 1) {
      const theta = (index / teeth) * Math.PI * 2;
      const tooth = new THREE.Mesh(toothGeometry, material);
      tooth.position.set(
        Math.cos(theta) * (part.radius + toothHeight * 0.32),
        0,
        Math.sin(theta) * (part.radius + toothHeight * 0.32),
      );
      tooth.rotation.y = -theta;
      group.add(tooth);
    }
    const hubGeometry = new THREE.CylinderGeometry(
      Math.max(0.04, part.radius * 0.3),
      Math.max(0.04, part.radius * 0.3),
      Math.max(0.05, part.length * 1.12),
      20,
    );
    geometries.push(hubGeometry);
    group.add(new THREE.Mesh(hubGeometry, material));
    return { object: group, material };
  }

  function buildRound(part, material) {
    const geometry = new THREE.CylinderGeometry(part.radius, part.radius, part.length, 24);
    geometries.push(geometry);
    const object = new THREE.Mesh(geometry, material);
    return { object, material };
  }

  function buildBox(part, material) {
    const [w, h, d] = part.size;
    const geometry = new THREE.BoxGeometry(w, h, d);
    geometries.push(geometry);
    const object = new THREE.Mesh(geometry, material);
    const edgesGeometry = new THREE.EdgesGeometry(geometry);
    geometries.push(edgesGeometry);
    const edgesMaterial = pushMaterial(new THREE.LineBasicMaterial({
      color: 0x1f373f,
      transparent: true,
      opacity: 0.35,
    }));
    object.add(new THREE.LineSegments(edgesGeometry, edgesMaterial));
    return { object, material };
  }

  function orientAlong(object, axis) {
    const from = new THREE.Vector3(0, 1, 0);
    const to = new THREE.Vector3(axis[0], axis[1], axis[2]);
    if (to.lengthSq() < 1e-8) return;
    to.normalize();
    object.quaternion.setFromUnitVectors(from, to);
  }

  // 建件：父件先建；子件挂到父件的对象上（position 为相对坐标）。
  for (const part of model.orderedParts) {
    const material = createMaterial(part);
    let built;
    if (part.kind === 'gear') built = buildGear(part, material);
    else if (part.kind === 'rod' || part.kind === 'cylinder') built = buildRound(part, material);
    else built = buildBox(part, material);
    if (part.kind === 'rod' || part.kind === 'cylinder' || part.kind === 'gear') {
      orientAlong(built.object, part.axis);
    }
    const local = model.localPositions.get(part.id) || { x: part.position[0], y: part.position[1], z: part.position[2] };
    built.object.position.set(local.x, local.y, local.z);
    built.object.userData.partId = part.id;
    const parent = part.parent ? entries.get(part.parent) : null;
    if (parent) parent.object.add(built.object);
    else root.add(built.object);
    entries.set(part.id, {
      part,
      object: built.object,
      material: built.material,
      base: { x: local.x, y: local.y, z: local.z },
      offset: model.explodeOffsets.get(part.id) || [0, 0, 0],
      active: false,
    });
    if (part.kind === 'gear' || part.spin !== null) {
      const omega = part.spin !== null ? part.spin : 1;
      spins.push({ object: built.object, omega });
    }
  }

  // 由读数/数据中的 rpm 推导视觉转速（只影响动画快慢，不改任何读数数值）。
  const rpmReadout = model.readouts.find((item) => String(item.id).toLowerCase().includes('rpm'));
  const rpmValue = num(rpmReadout?.value, 0);
  const visualBase = clamp((rpmValue > 0 ? rpmValue : 1200) / 1100, 0.45, 2.4);
  const gearList = model.orderedParts.filter((part) => part.kind === 'gear');
  const firstGearRadius = gearList.length ? gearList[0].radius : 1;
  for (const spin of spins) {
    const entry = entries.get(spin.object.userData.partId);
    if (!entry) continue;
    let omega = spin.omega;
    if (entry.part.kind === 'gear' && entry.part.spin === null && gearList.length > 1) {
      const index = gearList.indexOf(entry.part);
      omega = (firstGearRadius / entry.part.radius) * (index % 2 === 0 ? 1 : -1);
    }
    spin.omega = omega * visualBase;
  }

  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.localClippingEnabled = true;
  renderer.setPixelRatio(Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1));
  const canvas = renderer.domElement;
  canvas.className = 'm5-canvas';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', mt('study.m5.mech.canvasAria', '{v1}：可拖动旋转、滚轮缩放', {
    v1: model.title,
  }));
  host.replaceChildren(canvas);

  // 包围盒：包含拆解到底时的位置，避免部件飞出取景。
  root.updateMatrixWorld(true);
  const baseBox = new THREE.Box3().setFromObject(root);
  const savedPositions = new Map();
  for (const entry of entries.values()) {
    savedPositions.set(entry.part.id, { x: entry.object.position.x, y: entry.object.position.y, z: entry.object.position.z });
  }
  for (const entry of entries.values()) {
    entry.object.position.set(
      entry.base.x + entry.offset[0],
      entry.base.y + entry.offset[1],
      entry.base.z + entry.offset[2],
    );
  }
  root.updateMatrixWorld(true);
  const explodedBox = new THREE.Box3().setFromObject(root);
  const bounds = baseBox.union(explodedBox);
  for (const entry of entries.values()) {
    const saved = savedPositions.get(entry.part.id);
    entry.object.position.set(saved.x, saved.y, saved.z);
  }
  root.updateMatrixWorld(true);

  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  bounds.getCenter(center);
  bounds.getSize(size);
  const sceneRadius = Math.max(0.8, size.length() / 2);
  const target = model.camera.target
    ? new THREE.Vector3(model.camera.target[0], model.camera.target[1], model.camera.target[2])
    : center.clone();
  let orbitRadius = sceneRadius * 2.2 + 0.6;
  let theta = 0.92;
  let phi = 1.06;
  if (model.camera.position) {
    const offset = new THREE.Vector3(
      model.camera.position[0] - target.x,
      model.camera.position[1] - target.y,
      model.camera.position[2] - target.z,
    );
    const distance = offset.length();
    if (distance > 0.2) {
      orbitRadius = distance;
      theta = Math.atan2(offset.x, offset.z);
      phi = Math.acos(clamp(offset.y / distance, -1, 1));
    }
  }
  camera.near = Math.max(0.05, sceneRadius * 0.02);
  camera.far = Math.max(100, sceneRadius * 40);

  function updateCamera() {
    phi = clamp(phi, 0.14, Math.PI - 0.14);
    orbitRadius = clamp(orbitRadius, Math.max(0.4, sceneRadius * 0.6), Math.max(4, sceneRadius * 12));
    const sinPhi = Math.sin(phi);
    camera.position.set(
      target.x + orbitRadius * sinPhi * Math.sin(theta),
      target.y + orbitRadius * Math.cos(phi),
      target.z + orbitRadius * sinPhi * Math.cos(theta),
    );
    camera.lookAt(target);
  }
  updateCamera();

  let disposed = false;
  let contextLost = false;

  function render() {
    if (disposed || contextLost) return;
    renderer.render(scene, camera);
  }

  function resize() {
    if (disposed) return;
    const width = Math.max(1, host.clientWidth || 640);
    const height = Math.max(1, host.clientHeight || 380);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    render();
  }

  const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => resize()) : null;
  if (resizeObserver) resizeObserver.observe(host);
  window.addEventListener('resize', resize, { passive: true });

  const onContextLost = (event) => {
    event.preventDefault();
    contextLost = true;
    if (typeof hooks.onContextLost === 'function') hooks.onContextLost();
  };
  canvas.addEventListener('webglcontextlost', onContextLost, false);

  /* 鼠标/触屏旋转与缩放（OrbitControls 不在本地 vendor 中，这里实现最小轨道控制）。 */
  const pointers = new Map();
  let dragging = false;
  let pinchDistance = 0;
  let lastX = 0;
  let lastY = 0;

  function pointerDistance() {
    const list = [...pointers.values()];
    if (list.length < 2) return 0;
    return Math.hypot(list[0].x - list[1].x, list[0].y - list[1].y);
  }

  const onPointerDown = (event) => {
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    dragging = pointers.size === 1;
    lastX = event.clientX;
    lastY = event.clientY;
    pinchDistance = pointerDistance();
    try { canvas.setPointerCapture(event.pointerId); } catch { /* 老浏览器无指针捕获 */ }
  };
  const onPointerMove = (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size >= 2) {
      const distance = pointerDistance();
      if (pinchDistance > 0 && distance > 0) {
        orbitRadius *= pinchDistance / distance;
        pinchDistance = distance;
        updateCamera();
        render();
      }
      return;
    }
    if (!dragging) return;
    theta -= (event.clientX - lastX) * 0.006;
    phi -= (event.clientY - lastY) * 0.006;
    lastX = event.clientX;
    lastY = event.clientY;
    updateCamera();
    render();
  };
  const onPointerUp = (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size === 0) dragging = false;
    pinchDistance = pointerDistance();
    try { canvas.releasePointerCapture(event.pointerId); } catch { /* 忽略 */ }
  };
  const onWheel = (event) => {
    event.preventDefault();
    orbitRadius *= 1 + clamp(event.deltaY, -240, 240) * 0.0012;
    updateCamera();
    render();
  };
  canvas.addEventListener('pointerdown', onPointerDown, { passive: true });
  canvas.addEventListener('pointermove', onPointerMove, { passive: true });
  canvas.addEventListener('pointerup', onPointerUp, { passive: true });
  canvas.addEventListener('pointercancel', onPointerUp, { passive: true });
  canvas.addEventListener('wheel', onWheel, { passive: false });

  /* 拆解 / 高亮 / 剖切 */
  let explodeFactor = 0;
  function setExplode(factor) {
    explodeFactor = clamp(factor, 0, 1);
    for (const entry of entries.values()) {
      entry.object.position.set(
        entry.base.x + entry.offset[0] * explodeFactor,
        entry.base.y + entry.offset[1] * explodeFactor,
        entry.base.z + entry.offset[2] * explodeFactor,
      );
    }
    root.updateMatrixWorld(true);
    render();
  }

  function setActive(ids) {
    const activeIds = new Set(Array.isArray(ids) ? ids.map((id) => String(id)) : []);
    for (const entry of entries.values()) {
      const active = activeIds.has(entry.part.id);
      if (entry.active === active) continue;
      entry.active = active;
      const material = entry.material;
      if (material && material.emissive) {
        material.emissive.setHex(active ? 0x1d5b4b : 0x000000);
        material.emissiveIntensity = active ? 0.55 : 0;
      }
    }
    render();
  }

  const clipPlane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), bounds.max.x);
  let clipEnabled = false;
  function applyClipping() {
    for (const material of materials) {
      material.clippingPlanes = clipEnabled ? [clipPlane] : null;
      material.clipShadows = false;
      material.needsUpdate = true;
    }
    render();
  }
  function setClipEnabled(enabled) {
    clipEnabled = Boolean(enabled);
    applyClipping();
  }
  function setClipFraction(fraction) {
    const ratio = clamp(fraction, 0, 1);
    const range = bounds.max.x - bounds.min.x;
    clipPlane.constant = bounds.max.x - range * ratio;
    render();
  }

  /* 运转：只在播放时跑 requestAnimationFrame，暂停即停。 */
  let running = false;
  let speedMultiplier = 1;
  let rafId = 0;
  let lastFrame = 0;
  function tick(timestamp) {
    if (!running || disposed || contextLost) return;
    if (!lastFrame) lastFrame = timestamp;
    const delta = Math.min(0.08, (timestamp - lastFrame) / 1000);
    lastFrame = timestamp;
    for (const spin of spins) {
      spin.object.rotateY(spin.omega * speedMultiplier * delta);
    }
    render();
    rafId = requestAnimationFrame(tick);
  }
  function setRunning(value) {
    const next = Boolean(value);
    if (next === running) return;
    running = next;
    if (running) {
      lastFrame = 0;
      rafId = requestAnimationFrame(tick);
    } else if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = 0;
      render();
    }
  }
  function setSpeed(multiplier) {
    speedMultiplier = clamp(multiplier, 0.25, 4);
  }

  resize();
  setExplode(0);
  setActive([]);
  render();

  return {
    mode: 'three',
    element: canvas,
    setExplode,
    setActive,
    setClipEnabled,
    setClipFraction,
    setRunning,
    setSpeed,
    render,
    dispose() {
      if (disposed) return;
      disposed = true;
      setRunning(false);
      if (resizeObserver) resizeObserver.disconnect();
      window.removeEventListener('resize', resize);
      canvas.removeEventListener('webglcontextlost', onContextLost);
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      for (const geometry of geometries) {
        try { geometry.dispose(); } catch { /* 单件释放失败不影响其余资源 */ }
      }
      for (const material of materials) {
        try { material.dispose(); } catch { /* 同上 */ }
      }
      scene.clear();
      renderer.dispose();
      try { renderer.forceContextLoss(); } catch { /* 某些环境不支持 */ }
      canvas.remove();
      host.replaceChildren();
    },
  };
}

/* ------------------------------------------------------------------------- *
 * createMechView：机械 3D（拆解 / 剖切 / 运转 / 读数 + 降级）
 * ------------------------------------------------------------------------- */

/**
 * 在容器内创建机械 3D 演示。
 * @param {HTMLElement} container 承载容器（内容会被替换）
 * @param {{kind?:string,title?:string,data:object,seed?:number}} demo
 * @param {{readOnly?:boolean,onFrameReady?:Function,onReadoutChange?:Function}} [options]
 * @returns {object} controller：{element,ready,play,pause,seek,step,setExplode,toggleClip,
 *   setClipPosition,setSpeed,destroy,getMode,getStep,getStepCount,getSeed}
 */
export function createMechView(container, demo, options = {}) {
  if (!container || typeof container.replaceChildren !== 'function') {
    throw new Error('createMechView 需要有效的容器元素');
  }
  const opts = options && typeof options === 'object' ? options : {};
  let model;
  try {
    model = normalizeMechData(demo?.data, demo);
  } catch (error) {
    return createInvalidDemoView(container, MECH_KIND, error, opts);
  }

  const root = el('div', { class: 'm5-mech', 'data-demo-kind': MECH_KIND });
  const titleText = model.title;
  const head = el('div', { class: 'm5-head' },
    el('div', { class: 'm5-head-main' },
      el('strong', { class: 'm5-title', 'data-no-i18n': 'true' }, titleText),
      el('span', { class: 'm5-badge' }, kindLabel(MECH_KIND))),
    el('p', { class: 'm5-hint' }, mt('study.m5.mech.hint', '拖动旋转 · 滚轮缩放 · 用「下一步」逐步拆解')));
  const notice = el('div', { class: 'm5-notice', role: 'status', hidden: true });
  const stageHost = el('div', {
    class: 'm5-stage',
    role: 'group',
    'aria-label': mt('study.m5.mech.stageAria', '机械 3D 舞台：{v1}', { v1: titleText }),
  });
  const loadingNode = el('div', { class: 'm5-loading' },
    el('span', { class: 'spinner' }), mt('study.m5.mech.loading', '正在加载 3D 视图…'));

  const readoutList = el('div', { class: 'm5-readouts' });
  const partsList = el('ul', { class: 'm5-parts-list' });
  const side = el('aside', { class: 'm5-side' },
    el('section', { class: 'm5-side-section' },
      el('h4', {}, mt('study.m5.mech.readouts', '读数')),
      readoutList),
    el('section', { class: 'm5-side-section' },
      el('h4', {}, mt('study.m5.mech.parts', '部件')),
      partsList));

  const playButton = el('button', { type: 'button', class: 'm5-btn m5-btn-primary', 'aria-label': mt('study.m5.mech.run', '运转') }, '▶');
  const speedGroup = el('div', { class: 'm5-speed-group', role: 'group', 'aria-label': mt('study.m5.mech.speed', '运转速度') });
  const speedButtons = SPEED_OPTIONS.map((speed) => {
    const node = el('button', {
      type: 'button',
      class: `m5-btn m5-speed${speed === 1 ? ' is-active' : ''}`,
      'data-speed': String(speed),
      'aria-pressed': String(speed === 1),
    }, `${speed}×`);
    speedGroup.append(node);
    return node;
  });
  const explodeRange = el('input', {
    type: 'range', class: 'm5-range', min: '0', max: '100', step: '1', value: '0',
    'aria-label': mt('study.m5.mech.explode', '拆解进度'),
  });
  const explodeValue = el('span', { class: 'm5-value' }, '0%');
  const clipToggle = el('input', { type: 'checkbox', class: 'm5-checkbox' });
  const clipRange = el('input', {
    type: 'range', class: 'm5-range', min: '0', max: '100', step: '1', value: '35', disabled: true,
    'aria-label': mt('study.m5.mech.clipPosition', '剖切位置'),
  });
  const stepIndexNode = el('span', { class: 'm5-step-index' });
  const prevButton = el('button', { type: 'button', class: 'm5-btn' }, '‹');
  const nextButton = el('button', { type: 'button', class: 'm5-btn' }, '›');
  const stepTitleNode = el('strong', { class: 'm5-step-title', 'data-no-i18n': 'true' });
  const stepDetailNode = el('p', { class: 'm5-step-detail', 'data-no-i18n': 'true' });
  const toolbar = el('div', { class: 'm5-toolbar' },
    el('div', { class: 'm5-tool-group' }, playButton, speedGroup),
    el('div', { class: 'm5-tool-group m5-tool-grow' },
      el('label', { class: 'm5-tool-label', for: 'm5-mech-explode' }, mt('study.m5.mech.explodeShort', '拆解')),
      explodeRange, explodeValue),
    el('div', { class: 'm5-tool-group' },
      el('label', { class: 'm5-switch' }, clipToggle, el('span', {}, mt('study.m5.mech.clip', '剖切'))),
      clipRange),
    el('div', { class: 'm5-tool-group' },
      prevButton, stepIndexNode, nextButton));
  explodeRange.id = 'm5-mech-explode';
  const stepBox = el('div', { class: 'm5-step', 'aria-live': 'polite' }, stepTitleNode, stepDetailNode);
  const meta = metaNode({
    seed: model.seed,
    basis: model.basis,
    assumptions: model.assumptions,
    sourceNote: model.sourceNote,
  });
  root.append(head, notice, el('div', { class: 'm5-mech-body' }, stageHost, side), toolbar, stepBox, meta);
  container.replaceChildren(root);

  const state = {
    mode: 'three',
    step: 0,
    explode: 0,
    clip: false,
    clipFraction: 0.35,
    playing: false,
    speed: 1,
    destroyed: false,
  };
  let threeCtl = null;
  let readyResolve = null;
  let readySettled = false;
  const ready = new Promise((resolve) => { readyResolve = resolve; });

  function settleReady() {
    if (readySettled) return;
    readySettled = true;
    if (readyResolve) readyResolve(controller);
  }

  function emitFrame() {
    if (typeof opts.onFrameReady !== 'function') return;
    try {
      opts.onFrameReady({ step: state.step, mode: state.mode, seed: model.seed });
    } catch {
      /* 帧回执回调异常不影响演示 */
    }
  }
  let frameTimer = 0;
  function scheduleFrame() {
    if (frameTimer) clearTimeout(frameTimer);
    frameTimer = setTimeout(() => {
      frameTimer = 0;
      emitFrame();
    }, 120);
  }

  function setNotice(text) {
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
    if (loading) {
      if (!loadingNode.isConnected) stageHost.append(loadingNode);
    } else {
      loadingNode.remove();
    }
  }

  function renderReadouts() {
    readoutList.replaceChildren(...model.readouts.map((item) => el('div', { class: 'm5-readout' },
      el('span', { class: 'm5-readout-label', 'data-no-i18n': 'true' }, item.label),
      el('span', { class: 'm5-readout-value' },
        el('strong', { 'data-no-i18n': 'true' }, displayValue(item.value)),
        item.unit ? el('small', { 'data-no-i18n': 'true' }, item.unit) : null),
      item.note ? el('p', { class: 'm5-readout-note', 'data-no-i18n': 'true' }, item.note) : null)));
    if (!model.readouts.length) {
      readoutList.append(el('p', { class: 'm5-empty' }, mt('study.m5.mech.noReadouts', '该演示没有提供读数。')));
    }
    if (typeof opts.onReadoutChange === 'function') {
      try {
        opts.onReadoutChange(model.readouts.map((item) => ({ ...item })));
      } catch {
        /* 读数回调异常不影响演示 */
      }
    }
  }

  function renderParts() {
    partsList.replaceChildren(...model.orderedParts.map((part) => el('li', {
      class: 'm5-part-item',
      'data-part-id': part.id,
    }, el('span', { class: 'm5-part-dot', style: `background:${part.color}` }), part.label)));
  }

  function paintPartsActive() {
    const step = model.steps[state.step] || { part_ids: [] };
    const activeIds = new Set(step.part_ids);
    for (const node of partsList.querySelectorAll('.m5-part-item')) {
      node.classList.toggle('is-active', activeIds.has(node.dataset.partId || ''));
    }
  }

  function paintStepMeta() {
    const step = model.steps[state.step] || { label: '', detail: '' };
    stepTitleNode.textContent = step.label || mt('study.m5.mech.stepFallback', '第 {v1} 步', { v1: state.step + 1 });
    stepDetailNode.textContent = step.detail || '';
    stepDetailNode.hidden = !step.detail;
    stepIndexNode.textContent = `${state.step + 1}/${model.steps.length}`;
    // 降级为示意图后仍可逐步查看拆解顺序与说明，只有「运转」动画被禁用。
    prevButton.disabled = state.step <= 0;
    nextButton.disabled = state.step >= model.steps.length - 1;
    root.dataset.step = String(state.step);
  }

  function syncExplodeUi() {
    explodeRange.value = String(Math.round(state.explode * 100));
    explodeValue.textContent = `${Math.round(state.explode * 100)}%`;
  }

  function syncClipUi() {
    clipToggle.checked = state.clip;
    clipRange.disabled = !state.clip;
    clipRange.value = String(Math.round(state.clipFraction * 100));
    root.dataset.clip = state.clip ? 'on' : 'off';
  }

  function syncPlayUi() {
    playButton.textContent = state.playing ? '⏸' : '▶';
    const label = state.playing ? mt('study.m5.mech.pause', '暂停运转') : mt('study.m5.mech.run', '运转');
    playButton.setAttribute('aria-label', label);
    playButton.setAttribute('title', label);
    root.dataset.playing = state.playing ? 'true' : 'false';
  }

  function syncSpeedUi() {
    for (const button of speedButtons) {
      const value = Number(button.dataset.speed);
      button.classList.toggle('is-active', value === state.speed);
      button.setAttribute('aria-pressed', String(value === state.speed));
    }
  }

  function renderIsoView() {
    if (state.mode !== 'svg' || state.destroyed) return;
    const step = model.steps[state.step] || { part_ids: [] };
    const svg = renderMechIsoSvg(model, {
      explode: state.explode,
      clip: state.clip,
      clipFraction: state.clipFraction,
      activeIds: step.part_ids,
    });
    stageHost.replaceChildren(svg);
  }

  function applyStep(index, { emit = true } = {}) {
    state.step = clamp(Math.round(num(index, 0)), 0, model.steps.length - 1);
    const step = model.steps[state.step];
    state.explode = clamp(step.explode, 0, 1);
    syncExplodeUi();
    if (threeCtl) {
      threeCtl.setExplode(state.explode);
      threeCtl.setActive(step.part_ids);
    }
    if (state.mode === 'svg') renderIsoView();
    paintPartsActive();
    paintStepMeta();
    if (emit) emitFrame();
  }

  function play() {
    if (state.destroyed || state.mode !== 'three' || !threeCtl) return;
    state.playing = true;
    threeCtl.setRunning(true);
    threeCtl.setSpeed(state.speed);
    syncPlayUi();
  }

  function pause() {
    if (state.destroyed) return;
    state.playing = false;
    if (threeCtl) threeCtl.setRunning(false);
    syncPlayUi();
  }

  function seek(index) {
    pause();
    applyStep(index);
  }

  function step(direction) {
    const delta = Number(direction) < 0 ? -1 : 1;
    seek(state.step + delta);
  }

  function setExplode(value) {
    state.explode = clamp(value, 0, 1);
    syncExplodeUi();
    if (threeCtl) threeCtl.setExplode(state.explode);
    if (state.mode === 'svg') renderIsoView();
    scheduleFrame();
  }

  function setClipPosition(value) {
    state.clipFraction = clamp(value, 0, 1);
    syncClipUi();
    if (threeCtl) threeCtl.setClipFraction(state.clipFraction);
    if (state.mode === 'svg') renderIsoView();
    scheduleFrame();
  }

  function toggleClip() {
    state.clip = !state.clip;
    syncClipUi();
    if (threeCtl) {
      threeCtl.setClipEnabled(state.clip);
      // 开启剖切时把当前滑块位置同步到裁剪平面，避免「开了但看不到切面」。
      threeCtl.setClipFraction(state.clipFraction);
    }
    if (state.mode === 'svg') renderIsoView();
    scheduleFrame();
  }

  function setSpeed(multiplier) {
    const next = SPEED_OPTIONS.reduce(
      (closest, option) => (Math.abs(option - multiplier) < Math.abs(closest - multiplier) ? option : closest),
      state.speed,
    );
    state.speed = clamp(next, 0.5, 2);
    syncSpeedUi();
    if (threeCtl) threeCtl.setSpeed(state.speed);
  }

  function disableRunForSvg() {
    playButton.disabled = true;
    playButton.setAttribute('title', mt('study.m5.mech.svgRunDisabled', '示意图模式不演示运转动画'));
    for (const button of speedButtons) button.disabled = true;
  }

  function enableRunForThree() {
    playButton.disabled = false;
    playButton.removeAttribute('title');
    for (const button of speedButtons) button.disabled = false;
  }

  function mountFallback(reason) {
    if (state.destroyed) return;
    if (threeCtl) {
      try { threeCtl.dispose(); } catch { /* 释放失败不影响降级 */ }
      threeCtl = null;
    }
    state.mode = 'svg';
    state.playing = false;
    root.dataset.mode = 'svg';
    setLoading(false);
    setNotice(reason === 'webgl'
      ? mt('study.m5.webglFallback', '当前设备不支持 3D，已切换为示意图')
      : mt('study.m5.threeLoadFailed', '本地 3D 资源加载失败，已切换为示意图'));
    renderIsoView();
    disableRunForSvg();
    syncPlayUi();
    paintStepMeta();
    settleReady();
    emitFrame();
  }

  async function mountThree() {
    if (!isWebGLAvailable()) {
      mountFallback('webgl');
      return;
    }
    setLoading(true);
    try {
      const THREE = await loadThree();
      if (state.destroyed) return;
      threeCtl = createThreeMech(THREE, model, stageHost, {
        onContextLost: () => mountFallback('webgl'),
      });
      setLoading(false);
      state.mode = 'three';
      root.dataset.mode = 'three';
      setNotice('');
      enableRunForThree();
      threeCtl.setSpeed(state.speed);
      threeCtl.setClipFraction(state.clipFraction);
      if (state.clip) threeCtl.setClipEnabled(true);
      applyStep(state.step, { emit: false });
      settleReady();
      emitFrame();
    } catch {
      if (!state.destroyed) mountFallback('load');
    }
  }

  /* 事件绑定 */
  playButton.addEventListener('click', () => {
    if (state.playing) pause();
    else play();
  });
  for (const button of speedButtons) {
    button.addEventListener('click', () => setSpeed(Number(button.dataset.speed)));
  }
  explodeRange.addEventListener('input', () => setExplode(Number(explodeRange.value) / 100));
  clipToggle.addEventListener('change', () => toggleClip());
  clipRange.addEventListener('input', () => setClipPosition(Number(clipRange.value) / 100));
  prevButton.addEventListener('click', () => step(-1));
  nextButton.addEventListener('click', () => step(1));
  function onKeydown(event) {
    if (state.destroyed) return;
    const tag = event.target && event.target.tagName ? String(event.target.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      step(1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      step(-1);
    } else if (event.key === ' ' || event.key === 'Spacebar') {
      if (tag === 'BUTTON') return;
      event.preventDefault();
      if (state.playing) pause();
      else play();
    }
  }
  root.addEventListener('keydown', onKeydown);

  renderReadouts();
  renderParts();
  syncExplodeUi();
  syncClipUi();
  syncPlayUi();
  syncSpeedUi();
  paintStepMeta();
  root.tabIndex = 0;

  const controller = {
    element: root,
    ready,
    play,
    pause,
    seek,
    step,
    setExplode,
    toggleClip,
    setClipPosition,
    setSpeed,
    getMode: () => state.mode,
    getStep: () => state.step,
    getStepCount: () => model.steps.length,
    getSeed: () => model.seed,
    getExplode: () => state.explode,
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      if (frameTimer) {
        clearTimeout(frameTimer);
        frameTimer = 0;
      }
      pause();
      root.removeEventListener('keydown', onKeydown);
      if (threeCtl) {
        try { threeCtl.dispose(); } catch { /* 释放失败不影响页面 */ }
        threeCtl = null;
      }
      root.remove();
      container.replaceChildren();
      settleReady();
    },
  };

  setLoading(true);
  mountThree().catch(() => {
    if (!state.destroyed) mountFallback('load');
  });
  return controller;
}

/* ------------------------------------------------------------------------- *
 * 工厂仿真数据归一化与回放
 * ------------------------------------------------------------------------- */

function normalizeFactoryNode(raw, index, stationLabels) {
  if (!raw || typeof raw !== 'object') return null;
  const id = String(raw.id === undefined || raw.id === null ? `station-${index + 1}` : raw.id);
  return {
    id,
    label: String(raw.label || stationLabels[index] || mt('study.m5.factory.stationFallback', '工位 {v1}', {
      v1: index + 1,
    })),
    kind: String(raw.kind || 'station'),
  };
}

function normalizeFactoryMetrics(rawMetrics) {
  const source = rawMetrics && typeof rawMetrics === 'object' ? rawMetrics : {};
  const bottleneckRaw = source.bottleneck_station;
  let bottleneckIndex = -1;
  let bottleneckLabel = '';
  if (typeof bottleneckRaw === 'number' && Number.isFinite(bottleneckRaw)) {
    bottleneckIndex = Math.round(bottleneckRaw);
  } else if (typeof bottleneckRaw === 'string' && bottleneckRaw.trim()) {
    bottleneckLabel = bottleneckRaw.trim();
  }
  return {
    sim_seconds: Math.max(1, num(source.sim_seconds, 60)),
    arrivals: Math.max(0, Math.round(num(source.arrivals, 0))),
    completed: Math.max(0, Math.round(num(source.completed, 0))),
    throughput_per_min: Math.max(0, num(source.throughput_per_min, 0)),
    avg_wait_s: Array.isArray(source.avg_wait_s) ? source.avg_wait_s.map((value) => Math.max(0, num(value, 0))) : [],
    max_queue: Array.isArray(source.max_queue) ? source.max_queue.map((value) => Math.max(0, Math.round(num(value, 0)))) : [],
    utilization: Array.isArray(source.utilization)
      ? source.utilization.map((value) => clamp(num(value, 0), 0, 1))
      : [],
    bottleneckIndex,
    bottleneckLabel,
    bottleneckReason: String(source.bottleneck_reason || ''),
  };
}

/**
 * 归一化 factory_sim 数据。station 索引既可来自 flow.nodes 顺序，也可来自 station_labels；
 * 事件里 station 允许是序号、节点 id 或工位名。
 */
function normalizeFactoryData(rawData, demo) {
  const source = rawData && typeof rawData === 'object' ? rawData : {};
  const stationLabels = (Array.isArray(source.station_labels) ? source.station_labels : [])
    .map((item) => String(item === undefined || item === null ? '' : item))
    .filter(Boolean);
  const flow = source.flow && typeof source.flow === 'object' ? source.flow : {};
  const rawNodes = Array.isArray(flow.nodes) ? flow.nodes : [];
  // 后端 flow 可能带 source/sink 等端点节点；流程图与指标只围绕 kind=station 的工位，
  // 事件里的 station 序号（0..N-1）也对应这些工位的顺序。
  const markedStations = rawNodes.filter(
    (node) => node && typeof node === 'object' && String(node.kind || '').toLowerCase() === 'station',
  );
  const stationSource = markedStations.length ? markedStations : rawNodes;
  const nodes = [];
  const nodeCount = Math.max(stationSource.length, stationLabels.length, 1);
  for (let index = 0; index < nodeCount; index += 1) {
    const node = normalizeFactoryNode(stationSource[index], index, stationLabels);
    if (node) nodes.push(node);
  }
  const stationIndexById = new Map();
  nodes.forEach((node, index) => {
    stationIndexById.set(node.id, index);
    stationIndexById.set(node.label, index);
    stationIndexById.set(String(index), index);
    stationIndexById.set(`station-${index + 1}`, index);
  });
  const rawEdges = Array.isArray(flow.edges) ? flow.edges : [];
  const edges = [];
  const seen = new Set();
  for (const raw of rawEdges) {
    if (!raw || typeof raw !== 'object') continue;
    const from = resolveStationIndex(raw.from, stationIndexById, nodes);
    const to = resolveStationIndex(raw.to, stationIndexById, nodes);
    if (from < 0 || to < 0 || from === to) continue;
    const key = `${from}>${to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ from, to });
  }
  if (!edges.length && nodes.length > 1) {
    for (let index = 0; index < nodes.length - 1; index += 1) edges.push({ from: index, to: index + 1 });
  }
  const metrics = normalizeFactoryMetrics(source.metrics);
  metrics.sim_seconds = Math.max(metrics.sim_seconds, 1);
  const rawEvents = Array.isArray(source.events) ? source.events : [];
  const events = [];
  for (const raw of rawEvents) {
    if (!raw || typeof raw !== 'object') continue;
    const t = num(raw.t, NaN);
    if (!Number.isFinite(t)) continue;
    const station = resolveStationIndex(raw.station, stationIndexById, nodes);
    if (station < 0) continue;
    events.push({
      t: Math.max(0, t),
      type: String(raw.type === undefined || raw.type === null ? '' : raw.type).toLowerCase(),
      station,
      itemId: raw.item_id === undefined || raw.item_id === null ? '' : String(raw.item_id),
      queue: Number.isFinite(Number(raw.queue)) ? Math.max(0, Math.round(Number(raw.queue))) : null,
    });
  }
  events.sort((a, b) => (a.t === b.t ? 0 : a.t - b.t));
  const maxEventTime = events.length ? events[events.length - 1].t : 0;
  const simSeconds = Math.max(metrics.sim_seconds, maxEventTime, 1);
  if (metrics.bottleneckIndex >= nodes.length) metrics.bottleneckIndex = -1;
  const bottleneckNode = metrics.bottleneckIndex >= 0 ? nodes[metrics.bottleneckIndex] : null;
  return {
    title: String(demo?.title || source.title || mt('study.m5.factory.title', '工厂仿真演示')),
    seed: demo?.seed !== undefined && demo?.seed !== null ? demo.seed : (source.seed ?? null),
    basis: String(source.basis || ''),
    version: num(source.version, 1),
    nodes,
    edges,
    stationLabels: nodes.map((node) => node.label),
    metrics,
    bottleneckLabel: metrics.bottleneckLabel
      || (bottleneckNode ? bottleneckNode.label : (metrics.bottleneckIndex >= 0 ? nodes[metrics.bottleneckIndex]?.label || '' : '')),
    events,
    simSeconds,
    assumptions: Array.isArray(source.assumptions) ? source.assumptions : [],
    sourceNote: String(source.source_note || ''),
  };
}

function resolveStationIndex(value, stationIndexById, nodes) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const index = Math.round(value);
    return index >= 0 && index < nodes.length ? index : -1;
  }
  const key = String(value === undefined || value === null ? '' : value).trim();
  if (!key) return -1;
  if (stationIndexById.has(key)) return stationIndexById.get(key);
  return -1;
}

/** 由固定事件表推导每个时刻的队列/工位状态（纯回放，不做随机模拟）。 */
function buildFactoryReplay(model) {
  const count = model.nodes.length;
  const queues = new Array(count).fill(0);
  const status = new Array(count).fill('idle');
  const nextOf = new Array(count).fill(-1);
  for (const edge of model.edges) {
    if (nextOf[edge.from] < 0) nextOf[edge.from] = edge.to;
  }
  let arrivals = 0;
  let completed = 0;
  const snapshots = [{
    t: 0,
    queues: [...queues],
    status: [...status],
    arrivals,
    completed,
    event: null,
  }];
  for (const event of model.events) {
    const index = event.station;
    let recognized = true;
    if (ARRIVAL_TYPES.has(event.type)) {
      arrivals += 1;
      if (event.queue === null) queues[index] += 1;
    } else if (START_TYPES.has(event.type)) {
      if (event.queue === null) queues[index] = Math.max(0, queues[index] - 1);
      status[index] = 'busy';
    } else if (FINISH_TYPES.has(event.type)) {
      status[index] = 'idle';
      const next = nextOf[index];
      if (next >= 0) {
        if (event.queue === null) queues[next] += 1;
      } else {
        completed += 1;
      }
    } else if (BLOCK_TYPES.has(event.type)) {
      status[index] = 'blocked';
    } else if (UNBLOCK_TYPES.has(event.type)) {
      status[index] = 'busy';
    } else if (IDLE_TYPES.has(event.type)) {
      status[index] = 'idle';
    } else if (event.type !== 'queue' && event.type !== '') {
      recognized = false;
    }
    if (event.queue !== null) {
      queues[index] = event.queue;
    } else if (!recognized) {
      // 未识别类型且无 queue：保守不动队列，只记录快照。
      recognized = false;
    }
    snapshots.push({
      t: event.t,
      queues: [...queues],
      status: [...status],
      arrivals,
      completed,
      event,
    });
  }
  if (!snapshots.length || snapshots[snapshots.length - 1].t < model.simSeconds) {
    const last = snapshots[snapshots.length - 1];
    snapshots.push({
      t: model.simSeconds,
      queues: [...last.queues],
      status: [...last.status],
      arrivals: last.arrivals,
      completed: last.completed,
      event: null,
    });
  }
  return { snapshots, nextOf };
}

function snapshotAt(replay, time) {
  const snapshots = replay.snapshots;
  let low = 0;
  let high = snapshots.length - 1;
  let found = snapshots[0];
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (snapshots[middle].t <= time) {
      found = snapshots[middle];
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

function factoryStateText(stateKey) {
  const meta = FACTORY_STATE_KEYS[stateKey] || FACTORY_STATE_KEYS.idle;
  return mt(meta.key, meta.fallback);
}

function factoryEventText(type) {
  const key = String(type || '');
  const meta = FACTORY_EVENT_KEYS[key];
  if (meta) return mt(meta.key, meta.fallback);
  return key || mt('study.m5.factory.eventOther', '事件');
}

function formatSeconds(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '0';
  return String(Number(number.toFixed(digits)));
}

/** 流程图/产线示意里的工位名可能带括号说明（如「工位 1（平均服务 6.0 秒）」），
 * SVG 文本不换行，这里截成短标签；完整名字放进 <title> 供悬停/读屏使用。 */
function shortLabel(text, max = 14) {
  const value = String(text === undefined || text === null ? '' : text);
  const head = value.split(/[（(]/)[0].trim();
  const base = head || value;
  return base.length > max ? `${base.slice(0, max)}…` : base;
}

const FACTORY_PALETTE = ['#147d70', '#c08a3e', '#7a6bb0', '#b3554e', '#4a7c9b', '#8a8f3d'];

/* ------------------------------------------------------------------------- *
 * createFactoryView：工厂仿真回放（SVG + DOM，无 WebGL 依赖）
 * ------------------------------------------------------------------------- */

/**
 * 在容器内创建工厂仿真回放视图。
 * @param {HTMLElement} container
 * @param {{kind?:string,title?:string,data:object,seed?:number}} demo
 * @param {{readOnly?:boolean,onFrameReady?:Function}} [options]
 * @returns {object} controller：{element,ready,play,pause,seek,setSpeed,getTime,getMode,destroy}
 */
export function createFactoryView(container, demo, options = {}) {
  if (!container || typeof container.replaceChildren !== 'function') {
    throw new Error('createFactoryView 需要有效的容器元素');
  }
  const opts = options && typeof options === 'object' ? options : {};
  let model;
  try {
    model = normalizeFactoryData(demo?.data, demo);
  } catch (error) {
    return createInvalidDemoView(container, FACTORY_KIND, error, opts);
  }
  const replay = buildFactoryReplay(model);
  const hasEvents = model.events.length > 0;

  const root = el('div', { class: 'm5-factory', 'data-demo-kind': FACTORY_KIND });
  const head = el('div', { class: 'm5-head' },
    el('div', { class: 'm5-head-main' },
      el('strong', { class: 'm5-title', 'data-no-i18n': 'true' }, model.title),
      el('span', { class: 'm5-badge' }, kindLabel(FACTORY_KIND))),
    el('p', { class: 'm5-hint' }, mt('study.m5.factory.hint', '拖动时间轴或点播放，观察三工位队列与状态变化')));
  const notice = el('div', { class: 'm5-notice', role: 'status', hidden: true });
  const metricsNode = renderFactoryMetrics(model);
  const flowSvg = renderFactoryFlow(model, replay);
  const chartSvg = renderFactoryChart(model, replay);
  const chartLegend = el('div', { class: 'm5-chart-legend' }, ...model.nodes.map((node, index) => el('span', {
    class: 'm5-legend-item', title: node.label, 'data-no-i18n': 'true',
  },
  el('span', { class: 'm5-legend-dot', style: `background:${FACTORY_PALETTE[index % FACTORY_PALETTE.length]}` }),
  shortLabel(node.label, 12))));
  const lineNode = el('div', { class: 'm5-line', role: 'group', 'aria-label': mt('study.m5.factory.lineAria', '产线示意') });

  const playButton = el('button', { type: 'button', class: 'm5-btn m5-btn-primary', 'aria-label': mt('study.m5.factory.play', '播放回放') }, '▶');
  const speedGroup = el('div', { class: 'm5-speed-group', role: 'group', 'aria-label': mt('study.m5.factory.speed', '回放速度') });
  const speedButtons = SPEED_OPTIONS.map((speed) => {
    const node = el('button', {
      type: 'button',
      class: `m5-btn m5-speed${speed === 1 ? ' is-active' : ''}`,
      'data-speed': String(speed),
      'aria-pressed': String(speed === 1),
    }, `${speed}×`);
    speedGroup.append(node);
    return node;
  });
  const range = el('input', {
    type: 'range', class: 'm5-range', min: '0', max: String(model.simSeconds), step: '0.5', value: '0',
    'aria-label': mt('study.m5.factory.timelineAria', '回放时间'),
  });
  const timeText = el('span', { class: 'm5-time' }, `0s / ${formatSeconds(model.simSeconds, 0)}s`);
  const eventText = el('p', { class: 'm5-event-line', 'aria-live': 'polite' });
  const timelineNode = el('div', { class: 'm5-timeline' },
    el('div', { class: 'm5-toolbar m5-toolbar-flat' },
      el('div', { class: 'm5-tool-group' }, playButton, speedGroup),
      el('div', { class: 'm5-tool-group m5-tool-grow' }, range, timeText)),
    eventText);
  const sections = el('div', { class: 'm5-factory-body' },
    metricsNode,
    el('div', { class: 'm5-factory-grid' },
      el('section', { class: 'm5-card m5-flow-card' },
        el('h4', {}, mt('study.m5.factory.flowTitle', '三工位流程图')),
        flowSvg.node),
      el('section', { class: 'm5-card' },
        el('h4', {}, mt('study.m5.factory.lineTitle', '产线队列示意')),
        lineNode)),
    el('section', { class: 'm5-card' },
      el('h4', {}, mt('study.m5.factory.chartTitle', '队列长度时间序列')),
      chartSvg.node,
      chartLegend),
    el('section', { class: 'm5-card' },
      el('h4', {}, mt('study.m5.factory.timelineTitle', '事件回放时间轴')),
      timelineNode));

  root.append(head, notice, sections, metaNode({
    seed: model.seed,
    basis: model.basis,
    assumptions: model.assumptions,
    sourceNote: model.sourceNote,
  }));
  container.replaceChildren(root);

  if (!hasEvents) {
    notice.hidden = false;
    notice.textContent = mt('study.m5.factory.noEvents', '该演示数据缺少事件序列，仅展示指标与流程结构，不伪造回放。');
  }
  if (!model.nodes.length) {
    notice.hidden = false;
    notice.textContent = mt('study.m5.factory.noStations', '该演示数据缺少工位信息，暂时无法回放产线。');
  }

  /* 迷你产线：每个工位一个方块 + 队列条 + 状态点。 */
  const lineStations = [];
  for (const node of model.nodes) {
    const queueFill = el('span', { class: 'm5-line-queue-fill' });
    const queueValue = el('strong', {}, '0');
    const stateDot = el('span', { class: 'm5-line-dot' });
    const station = el('div', { class: 'm5-line-station' },
      el('div', { class: 'm5-line-station-head' }, stateDot,
        el('span', { 'data-no-i18n': 'true', title: node.label }, shortLabel(node.label, 12))),
      el('div', { class: 'm5-line-queue' }, queueFill),
      el('div', { class: 'm5-line-meta' },
        el('span', {}, mt('study.m5.factory.queueShort', '队列')), queueValue));
    lineNode.append(station);
    lineStations.push({ node, stateDot, queueFill, queueValue });
  }

  const state = {
    t: 0,
    speed: 1,
    playing: false,
    destroyed: false,
    lastEmit: -1,
  };
  let rafId = 0;
  let lastTimestamp = 0;
  let readyResolve = null;
  let readySettled = false;
  const ready = new Promise((resolve) => { readyResolve = resolve; });

  function settleReady() {
    if (readySettled) return;
    readySettled = true;
    if (readyResolve) readyResolve(controller);
  }

  function emitFrame(force = false) {
    if (typeof opts.onFrameReady !== 'function') return;
    if (!force && Math.abs(state.t - state.lastEmit) < 0.15) return;
    state.lastEmit = state.t;
    try {
      opts.onFrameReady({ t: Number(state.t.toFixed(1)) });
    } catch {
      /* 帧回执回调异常不影响回放 */
    }
  }

  function syncPlayUi() {
    playButton.textContent = state.playing ? '⏸' : '▶';
    const label = state.playing ? mt('study.m5.factory.pause', '暂停回放') : mt('study.m5.factory.play', '播放回放');
    playButton.setAttribute('aria-label', label);
    playButton.setAttribute('title', label);
    root.dataset.playing = state.playing ? 'true' : 'false';
  }

  function syncSpeedUi() {
    for (const button of speedButtons) {
      const value = Number(button.dataset.speed);
      button.classList.toggle('is-active', value === state.speed);
      button.setAttribute('aria-pressed', String(value === state.speed));
    }
  }

  function applyTime(value, { force = false } = {}) {
    state.t = clamp(value, 0, model.simSeconds);
    const snapshot = snapshotAt(replay, state.t);
    root.dataset.t = state.t.toFixed(1);
    range.value = String(state.t);
    timeText.textContent = `${formatSeconds(state.t)}s / ${formatSeconds(model.simSeconds, 0)}s`;
    applyFactoryFlowState(flowSvg, snapshot, model, state.t);
    applyFactoryLineState(lineStations, snapshot, model);
    applyFactoryChartCursor(chartSvg, snapshot, model, state.t);
    const event = snapshot.event;
    if (event) {
      const stationLabel = model.nodes[event.station]?.label || '';
      eventText.textContent = mt(
        'study.m5.factory.eventLine',
        '{v1}s · {v2} · {v3}{v4}',
        {
          v1: formatSeconds(event.t),
          v2: factoryEventText(event.type),
          v3: stationLabel,
          v4: event.itemId ? ` · ${mt('study.m5.factory.item', '工件 {v1}', { v1: event.itemId })}` : '',
        },
      );
    } else {
      eventText.textContent = mt('study.m5.factory.eventWaiting', '起点：等待第一个工件到达');
    }
    emitFrame(force);
  }

  function pause() {
    if (state.destroyed) return;
    state.playing = false;
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = 0;
    }
    syncPlayUi();
  }

  function tick(timestamp) {
    if (!state.playing || state.destroyed) return;
    if (!lastTimestamp) lastTimestamp = timestamp;
    const delta = Math.min(0.2, (timestamp - lastTimestamp) / 1000);
    lastTimestamp = timestamp;
    const next = state.t + delta * state.speed;
    if (next >= model.simSeconds) {
      applyTime(model.simSeconds, { force: true });
      pause();
      return;
    }
    applyTime(next);
    rafId = requestAnimationFrame(tick);
  }

  function play() {
    if (state.destroyed || !hasEvents) return;
    if (state.t >= model.simSeconds) applyTime(0);
    if (state.playing) return;
    state.playing = true;
    lastTimestamp = 0;
    syncPlayUi();
    rafId = requestAnimationFrame(tick);
  }

  function seek(value) {
    pause();
    applyTime(value, { force: true });
  }

  function setSpeed(multiplier) {
    const next = SPEED_OPTIONS.reduce(
      (closest, option) => (Math.abs(option - multiplier) < Math.abs(closest - multiplier) ? option : closest),
      state.speed,
    );
    state.speed = next;
    syncSpeedUi();
  }

  playButton.addEventListener('click', () => {
    if (state.playing) pause();
    else play();
  });
  playButton.disabled = !hasEvents;
  for (const button of speedButtons) {
    button.addEventListener('click', () => setSpeed(Number(button.dataset.speed)));
  }
  range.addEventListener('input', () => seek(Number(range.value)));

  const controller = {
    element: root,
    ready,
    play,
    pause,
    seek,
    setSpeed,
    getTime: () => Number(state.t.toFixed(1)),
    getMode: () => 'svg',
    getSeed: () => model.seed,
    getMetrics: () => ({ ...model.metrics }),
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      pause();
      root.remove();
      container.replaceChildren();
      settleReady();
    },
  };

  if (!hasEvents) playButton.disabled = true;
  applyTime(0, { force: true });
  settleReady();
  return controller;
}

/* ------------------------------------------------------------------------- *
 * 工厂仿真各区块渲染
 * ------------------------------------------------------------------------- */

function renderFactoryMetrics(model) {
  const metrics = model.metrics;
  const cards = [
    {
      label: mt('study.m5.factory.metricThroughput', '吞吐'),
      value: displayValue(metrics.throughput_per_min),
      unit: mt('study.m5.factory.unitPerMin', '件/分'),
    },
    {
      label: mt('study.m5.factory.metricCompleted', '完成 / 到达'),
      value: `${metrics.completed} / ${metrics.arrivals}`,
      unit: mt('study.m5.factory.unitItems', '件'),
    },
    {
      label: mt('study.m5.factory.metricBottleneck', '瓶颈工位'),
      value: model.bottleneckLabel || '—',
      unit: '',
      note: metrics.bottleneckReason,
    },
    {
      label: mt('study.m5.factory.metricSimSeconds', '仿真时长'),
      value: displayValue(metrics.sim_seconds),
      unit: 's',
    },
  ];
  return el('div', { class: 'm5-metrics' }, ...cards.map((card) => el('div', { class: 'm5-metric-card' },
    el('span', { class: 'm5-metric-label' }, card.label),
    el('span', { class: 'm5-metric-value' },
      el('strong', { 'data-no-i18n': 'true' }, displayValue(card.value)),
      card.unit ? el('small', { 'data-no-i18n': 'true' }, card.unit) : null),
    card.note ? el('p', { class: 'm5-metric-note', 'data-no-i18n': 'true' }, card.note) : null)));
}

function renderFactoryFlow(model, replay) {
  const width = 760;
  const height = 220;
  const padding = 26;
  const count = model.nodes.length;
  const gap = 46;
  const boxWidth = count > 1
    ? Math.max(90, (width - padding * 2 - gap * (count - 1)) / count)
    : width - padding * 2;
  const boxHeight = 132;
  const top = 34;
  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'm5-flow-svg',
    role: 'img',
    'aria-label': mt('study.m5.factory.flowAria', '三工位流程图'),
  });
  const stations = [];
  model.nodes.forEach((node, index) => {
    const x = padding + index * (boxWidth + gap);
    const group = svgEl('g', { class: 'm5-flow-station', transform: `translate(${x.toFixed(2)} ${top})` });
    const util = model.metrics.utilization[index];
    const fill = svgEl('rect', {
      x: 0, y: 0, width: boxWidth, height: boxHeight, rx: 12,
      class: 'm5-flow-box',
    });
    const label = svgEl('text', {
      x: 14, y: 26, class: 'm5-flow-label',
    }, shortLabel(node.label));
    label.append(svgEl('title', {}, node.label));
    const stateText = svgEl('text', {
      x: boxWidth - 14, y: 26, class: 'm5-flow-state', 'text-anchor': 'end',
    }, factoryStateText('idle'));
    const queueText = svgEl('text', {
      x: 14, y: 52, class: 'm5-flow-queue',
    }, mt('study.m5.factory.queueLine', '队列 {v1}', { v1: 0 }));
    const utilLabel = svgEl('text', {
      x: 14, y: 78, class: 'm5-flow-util-label',
    }, mt('study.m5.factory.utilLine', '利用率 {v1}', { v1: `${Math.round((util ?? 0) * 100)}%` }));
    const utilTrack = svgEl('rect', {
      x: 14, y: 88, width: boxWidth - 28, height: 8, rx: 4, class: 'm5-flow-util-track',
    });
    const utilFill = svgEl('rect', {
      x: 14, y: 88, width: Math.max(0, (boxWidth - 28) * clamp(util ?? 0, 0, 1)), height: 8, rx: 4,
      class: 'm5-flow-util-fill',
    });
    const waitText = svgEl('text', {
      x: 14, y: 116, class: 'm5-flow-wait',
    }, mt('study.m5.factory.waitLine', '平均等待 {v1}s · 最大队列 {v2}', {
      v1: formatSeconds(model.metrics.avg_wait_s[index], 1),
      v2: model.metrics.max_queue[index] ?? 0,
    }));
    group.append(fill, label, stateText, queueText, utilLabel, utilTrack, utilFill, waitText);
    svg.append(group);
    stations.push({ group, stateText, queueText, fill, utilFill, boxWidth, boxHeight });
  });
  // 工位之间的流向箭头（用真实 flow.edges，不假设一定是链式）。
  for (const edge of model.edges) {
    const from = stations[edge.from];
    const to = stations[edge.to];
    if (!from || !to) continue;
    const fromX = padding + edge.from * (boxWidth + gap) + boxWidth;
    const toX = padding + edge.to * (boxWidth + gap);
    const y = top + boxHeight / 2;
    if (toX > fromX) {
      svg.append(svgEl('line', {
        x1: fromX + 4, y1: y, x2: toX - 10, y2: y, class: 'm5-flow-arrow',
        'marker-end': 'url(#m5-arrow)',
      }));
    } else {
      const midY = top - 16;
      const fromCenter = fromX - boxWidth / 2;
      const toCenter = toX + boxWidth / 2;
      svg.append(svgEl('path', {
        d: `M ${fromCenter} ${top} L ${fromCenter} ${midY} L ${toCenter} ${midY} L ${toCenter} ${top}`,
        class: 'm5-flow-arrow', fill: 'none', 'marker-end': 'url(#m5-arrow)',
      }));
    }
  }
  const defs = svgEl('defs');
  const marker = svgEl('marker', {
    id: 'm5-arrow', viewBox: '0 0 10 10', refX: 8, refY: 5,
    markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse',
  });
  marker.append(svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', class: 'm5-flow-arrow-head' }));
  defs.append(marker);
  svg.prepend(defs);
  return { node: svg, stations };
}

function applyFactoryFlowState(flow, snapshot, model) {
  if (!flow || !flow.stations) return;
  flow.stations.forEach((station, index) => {
    const queue = snapshot.queues[index] || 0;
    const status = snapshot.status[index] || 'idle';
    station.stateText.textContent = factoryStateText(status);
    station.queueText.textContent = mt('study.m5.factory.queueLine', '队列 {v1}', { v1: queue });
    station.fill.setAttribute('data-state', status);
    station.group.setAttribute('data-state', status);
  });
}

function renderFactoryChart(model, replay) {
  const width = 760;
  const height = 220;
  const left = 42;
  const right = 18;
  const top = 18;
  const bottom = 34;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const stationCount = Math.max(1, model.nodes.length);
  let maxQueue = 1;
  for (const snapshot of replay.snapshots) {
    for (const value of snapshot.queues) maxQueue = Math.max(maxQueue, value);
  }
  for (const value of model.metrics.max_queue) maxQueue = Math.max(maxQueue, value);
  maxQueue = Math.ceil(maxQueue) + 1;
  const xOf = (t) => left + (clamp(t, 0, model.simSeconds) / model.simSeconds) * plotWidth;
  const yOf = (value) => top + plotHeight - (clamp(value, 0, maxQueue) / maxQueue) * plotHeight;
  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`,
    class: 'm5-chart-svg',
    role: 'img',
    'aria-label': mt('study.m5.factory.chartAria', '队列长度时间序列'),
  });
  // 网格与坐标
  for (let value = 0; value <= maxQueue; value += Math.max(1, Math.ceil(maxQueue / 4))) {
    const y = yOf(value);
    svg.append(svgEl('line', { x1: left, y1: y, x2: width - right, y2: y, class: 'm5-chart-grid' }));
    svg.append(svgEl('text', { x: left - 8, y: y + 4, class: 'm5-chart-tick', 'text-anchor': 'end' }, value));
  }
  for (let ratio = 0; ratio <= 1; ratio += 0.25) {
    const time = model.simSeconds * ratio;
    const x = xOf(time);
    svg.append(svgEl('line', { x1: x, y1: top, x2: x, y2: top + plotHeight, class: 'm5-chart-grid' }));
    svg.append(svgEl('text', { x, y: height - 12, class: 'm5-chart-tick', 'text-anchor': 'middle' }, `${formatSeconds(time, 0)}s`));
  }
  const palette = FACTORY_PALETTE;
  const lines = [];
  for (let index = 0; index < stationCount; index += 1) {
    const points = replay.snapshots.map((snapshot) => `${xOf(snapshot.t).toFixed(2)},${yOf(snapshot.queues[index] || 0).toFixed(2)}`).join(' ');
    const color = palette[index % palette.length];
    svg.append(svgEl('polyline', {
      points, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round',
      class: 'm5-chart-line',
    }));
    lines.push({ index, color });
  }
  const cursor = svgEl('line', {
    x1: left, y1: top, x2: left, y2: top + plotHeight, class: 'm5-chart-cursor',
  });
  svg.append(cursor);
  return { node: svg, xOf, cursor, lines, left, plotWidth };
}

function applyFactoryChartCursor(chart, snapshot, model, time) {
  if (!chart || !chart.cursor) return;
  const x = chart.xOf(time);
  chart.cursor.setAttribute('x1', x.toFixed(2));
  chart.cursor.setAttribute('x2', x.toFixed(2));
}

function applyFactoryLineState(lineStations, snapshot, model) {
  const maxQueue = Math.max(
    1,
    ...model.metrics.max_queue,
    ...snapshot.queues,
  );
  lineStations.forEach((station, index) => {
    const queue = snapshot.queues[index] || 0;
    const status = snapshot.status[index] || 'idle';
    station.queueValue.textContent = String(queue);
    station.queueFill.style.width = `${Math.round((queue / maxQueue) * 100)}%`;
    station.queueFill.dataset.state = status;
    station.stateDot.dataset.state = status;
    station.stateDot.setAttribute('title', factoryStateText(status));
  });
}

/* ------------------------------------------------------------------------- *
 * 数据非法时的中文降级视图（不伪造演示内容）
 * ------------------------------------------------------------------------- */

function createInvalidDemoView(container, kind, error, options) {
  const root = el('div', {
    class: 'm5-demo m5-demo-invalid',
    'data-demo-kind': kind,
    'data-demo-error': '1',
    role: 'status',
  },
  el('div', { class: 'm5-head' },
    el('div', { class: 'm5-head-main' },
      el('strong', { class: 'm5-title' }, mt('study.m5.invalidTitle', '演示数据不完整')),
      el('span', { class: 'm5-badge' }, kindLabel(kind)))),
  el('p', { class: 'm5-notice' },
    mt('study.m5.invalidData', '该演示的数据不完整，暂时无法渲染：{v1}', {
      v1: error?.message || mt('study.m5.invalidReason', '缺少必要字段'),
    })),
  el('p', { class: 'm5-hint' },
    mt('study.m5.invalidHint', '可以返回重试，或先查看其它演示；页面不会用占位数据冒充结果。')));
  container.replaceChildren(root);
  let resolveReady = null;
  const ready = new Promise((resolve) => { resolveReady = resolve; });
  const controller = {
    element: root,
    ready,
    getMode: () => 'svg',
    destroy() {
      root.remove();
      container.replaceChildren();
      if (resolveReady) resolveReady(controller);
    },
  };
  if (resolveReady) resolveReady(controller);
  if (typeof options?.onFrameReady === 'function') {
    try { options.onFrameReady({ step: 0, mode: 'svg', seed: null }); } catch { /* 忽略 */ }
  }
  return controller;
}

/* ------------------------------------------------------------------------- *
 * openMechDemo：统一弹层（模板选择 + 只读参数 + 固定种子 + 保存为学习附件）
 * ------------------------------------------------------------------------- */

function normalizeDemoLinks(options) {
  const raw = Array.isArray(options?.links)
    ? options.links
    : (options?.ref ? [{ target_kind: 'question', target_key: options.ref }] : []);
  const links = [];
  const seen = new Set();
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const targetKind = String(entry.target_kind || '').trim();
    const targetKey = String(entry.target_key || '').trim();
    if (!targetKind || !targetKey) continue;
    const key = `${targetKind}:${targetKey}`;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({ target_kind: targetKind, target_key: targetKey });
    if (links.length >= 20) break;
  }
  return links;
}

function templateParamEntries(template) {
  if (Array.isArray(template?.params)) {
    return template.params
      .filter((item) => item && typeof item === 'object')
      .map((item) => ({
        name: String(item.name ?? ''),
        label: String(item.label || item.name || ''),
        value: item.default,
      }));
  }
  if (template?.params && typeof template.params === 'object') {
    return Object.entries(template.params).map(([name, value]) => ({ name, label: name, value }));
  }
  return [];
}

function templateDefaults(template) {
  const defaults = {};
  for (const entry of templateParamEntries(template)) {
    if (entry.name) defaults[entry.name] = entry.value;
  }
  return defaults;
}

function isUsableTemplate(item) {
  return Boolean(item && typeof item === 'object'
    && typeof item.template_id === 'string' && item.template_id
    && DEMO_KINDS.has(String(item.kind || '')));
}

/**
 * 打开机械/工厂演示弹层（题目页「机械演示」入口等）。
 *
 * @param {object} options
 * @param {string} [options.template_id] 期望模板 ID（缺失时取列表第一个）；
 * @param {{kind?:string,title?:string,data?:object,seed?:number}} [options.artifact]
 *        已保存的学习附件（提供时只读打开，不允许重复保存）；
 * @param {HTMLElement} [options.trigger] 触发按钮（关闭后归还焦点）；
 * @param {boolean} [options.readOnly] 只读（隐藏保存按钮）；
 * @param {Function} [options.onSaved] 保存成功回调 ({artifactId,revision,kind,title,templateId,local})；
 * @param {Array<{target_kind:string,target_key:string}>|string} [options.links] 保存时携带的关联目标；
 * @param {Function} [options.onError] 加载失败回调（可选，不传时弹层内中文提示）。
 * @returns {object} controller：{element,ready,destroy,getMode,getTemplateId,getData,reload,save,setTemplate}
 */
export function openMechDemo(options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const links = normalizeDemoLinks(opts);
  const artifact = opts.artifact && typeof opts.artifact === 'object' ? opts.artifact : null;
  const readOnly = Boolean(opts.readOnly) || Boolean(artifact);
  const dialog = el('dialog', {
    class: 'm5-dialog',
    'data-mech-demo': '',
    'aria-label': mt('study.m5.dialogTitle', '机械与工厂演示'),
  });
  const titleNode = el('h3', { class: 'm5-dialog-title', 'data-no-i18n': 'true' },
    mt('study.m5.dialogTitle', '机械与工厂演示'));
  const closeButton = el('button', {
    type: 'button', class: 'm5-dialog-close', 'aria-label': mt('study.m5.close', '关闭'),
    title: mt('study.m5.close', '关闭'),
  }, '×');
  const statusNode = el('p', { class: 'm5-dialog-status' },
    el('span', { class: 'spinner' }), mt('study.m5.loading', '正在加载演示…'));
  const selectNode = el('select', {
    class: 'm5-preset-select', 'aria-label': mt('study.m5.presetAria', '选择演示模板'),
  });
  const sourceNode = el('span', { class: 'm5-preset-source', hidden: true });
  const switchBar = el('div', { class: 'm5-switch-bar' },
    el('label', { class: 'm5-tool-label', for: 'm5-preset-select' }, mt('study.m5.preset', '演示模板')),
    selectNode, sourceNode);
  selectNode.id = 'm5-preset-select';
  const stageHost = el('div', { class: 'm5-dialog-stage' });
  const paramsHost = el('div', { class: 'm5-params' });
  const metaHost = el('div', { class: 'm5-dialog-meta' });
  const saveButton = el('button', {
    type: 'button', class: 'button secondary small m5-save-btn',
  }, mt('study.m5.saveArtifact', '保存为学习附件'));
  const errorsNode = el('div', { class: 'm5-dialog-error', role: 'status', hidden: true });
  const actions = el('div', { class: 'm5-dialog-actions' },
    el('p', { class: 'm5-dialog-note' },
      mt('study.m5.artifactNotice', '保存为学习附件后，可在题目、笔记与课堂中按固定版本只读打开。')),
    saveButton);
  if (readOnly) saveButton.hidden = true;

  dialog.append(
    el('div', { class: 'm5-dialog-head' },
      el('div', { class: 'm5-dialog-heading' },
        el('span', { class: 'm5-eyebrow' }, mt('study.m5.eyebrow', '机械与工厂')),
        titleNode),
      closeButton),
    el('div', { class: 'm5-dialog-body' },
      statusNode, switchBar, stageHost, paramsHost, metaHost, errorsNode),
    actions);
  document.body.append(dialog);

  const state = {
    templates: [],
    source: 'api',
    template: null,
    data: null,
    title: '',
    seed: null,
    basis: '',
    assumptions: [],
    sourceNote: '',
    origin: 'api',
    sourceRef: '',
    view: null,
    destroyed: false,
    seq: 0,
    saved: false,
  };
  let readyResolve = null;
  let readySettled = false;
  const ready = new Promise((resolve) => { readyResolve = resolve; });

  function settleReady() {
    if (readySettled) return;
    readySettled = true;
    if (readyResolve) readyResolve(controller);
  }

  function setStatus(text, { error = false } = {}) {
    if (!text) {
      statusNode.hidden = true;
      statusNode.replaceChildren();
      return;
    }
    statusNode.hidden = false;
    statusNode.classList.toggle('is-error', error);
    statusNode.textContent = text;
  }

  function showError(text) {
    errorsNode.hidden = !text;
    errorsNode.textContent = text || '';
  }

  function destroyView() {
    if (state.view && typeof state.view.destroy === 'function') {
      try { state.view.destroy(); } catch { /* 释放失败不影响弹层 */ }
    }
    state.view = null;
  }

  function renderSelect() {
    selectNode.replaceChildren();
    const groups = new Map();
    for (const item of state.templates) {
      const label = String(item.title || item.template_id);
      const option = el('option', { value: item.template_id }, label);
      if (!groups.has(item.kind)) {
        const group = el('optgroup', { label: kindLabel(item.kind) });
        groups.set(item.kind, group);
        selectNode.append(group);
      }
      groups.get(item.kind).append(option);
    }
  }

  function renderParams(template, builtParams) {
    const entries = templateParamEntries(template);
    const values = builtParams && typeof builtParams === 'object' ? builtParams : null;
    paramsHost.replaceChildren();
    if (!entries.length) return;
    const list = el('ul', { class: 'm5-param-list' });
    for (const entry of entries) {
      const value = values && values[entry.name] !== undefined ? values[entry.name] : entry.value;
      list.append(el('li', { class: 'm5-param' },
        el('span', { class: 'm5-param-label', 'data-no-i18n': 'true' }, entry.label || entry.name),
        el('strong', { class: 'm5-param-value', 'data-no-i18n': 'true' }, displayValue(value))));
    }
    paramsHost.append(
      el('div', { class: 'm5-param-head' },
        el('span', {}, mt('study.m5.params', '参数（只读）')),
        el('small', {}, mt('study.m5.paramsNote', '来自预设/服务端；前端不做数值模拟，也不修改公式。'))),
      list);
  }

  function renderMeta() {
    metaHost.replaceChildren(metaNode({
      seed: state.seed,
      basis: state.basis,
      assumptions: state.assumptions,
      sourceNote: state.sourceNote,
    }));
  }

  function setSelectedTemplate(template) {
    state.template = template;
    if (!template) {
      titleNode.textContent = mt('study.m5.dialogTitle', '机械与工厂演示');
      return;
    }
    titleNode.textContent = String(template.title || template.template_id);
    if (selectNode.value !== template.template_id) selectNode.value = template.template_id;
  }

  function findTemplate(templateId) {
    const wanted = String(templateId || '').trim();
    if (wanted) {
      const exact = state.templates.find((item) => item.template_id === wanted);
      if (exact) return exact;
      const preset = getMechPreset(wanted);
      if (preset) {
        const sameKind = state.templates.find((item) => item.kind === preset.kind);
        if (sameKind) return sameKind;
      }
    }
    return state.templates[0] || null;
  }

  function pickInitialTemplate() {
    const wanted = String(opts.template_id || artifact?.source_ref || '').trim();
    return findTemplate(wanted);
  }

  /** 从服务端构建，接口不可用时依次回退到 simulate 与本地预设（每次回退都在弹层内中文说明）。 */
  async function resolveDemoData(template, { initial = false } = {}) {
    const exactPreset = getMechPreset(template.template_id);
    // 后端可能新增同 kind 的新模板（例如 factory-line-2d）：本地兜底按 kind 找最近预设，
    // 并在弹层里明确标注「本地教学示意」，不冒充服务端固定种子结果。
    const fallbackPreset = exactPreset
      || MECH_PRESETS.find((preset) => preset.kind === template.kind)
      || null;
    const defaults = templateDefaults(template);
    const params = Object.keys(defaults).length ? defaults : { ...(fallbackPreset?.params || {}) };
    // 1) 已保存附件（仅初始模板且结构可用时直接只读展示）。
    if (initial && artifact && artifact.data && typeof artifact.data === 'object'
      && String(artifact.kind || template.kind) === template.kind) {
      return {
        data: artifact.data,
        title: String(artifact.title || template.title || ''),
        seed: artifact.seed ?? artifact.data?.seed ?? null,
        basis: String(artifact.data?.basis || ''),
        assumptions: Array.isArray(artifact.assumptions) ? artifact.assumptions : (artifact.data?.assumptions || []),
        sourceNote: String(artifact.data?.source_note || ''),
        sourceRef: template.template_id,
        origin: 'artifact',
      };
    }

    const fromBuild = async () => {
      const built = await api('/demos/build', {
        method: 'POST',
        data: { template_id: template.template_id, params },
      });
      if (!built || !built.data || typeof built.data !== 'object') throw new Error('返回数据为空');
      return {
        data: built.data,
        title: String(built.title || template.title || ''),
        seed: built.seed ?? built.data.seed ?? null,
        basis: String(built.data.basis || ''),
        assumptions: Array.isArray(built.assumptions) ? built.assumptions : (built.data.assumptions || []),
        sourceNote: String(built.source_note || built.data.source_note || ''),
        params: built.params,
        sourceRef: template.template_id,
        origin: 'api',
      };
    };

    const fromSimulate = async () => {
      const simulated = await api('/demos/simulate', {
        method: 'POST',
        data: { template_id: template.template_id, params },
      });
      const run = simulated?.run;
      if (!run || !run.result || typeof run.result !== 'object') throw new Error('仿真返回数据为空');
      const result = run.result;
      return {
        data: result,
        title: String(template.title || ''),
        seed: run.seed ?? result.seed ?? null,
        basis: String(result.basis || ''),
        assumptions: Array.isArray(result.assumptions) && result.assumptions.length
          ? result.assumptions
          : (Array.isArray(template.assumptions) ? template.assumptions : []),
        sourceNote: String(result.source_note || template.source_note || ''),
        params: run.params,
        sourceRef: template.template_id,
        origin: 'api',
      };
    };

    // 2) 服务端固定种子构建 / 固定种子 DES 仿真：按模板 kind 决定优先接口。
    const attempts = template.kind === FACTORY_KIND
      ? [fromSimulate, fromBuild]
      : [fromBuild];
    let lastError = null;
    for (const attempt of attempts) {
      try {
        return await attempt();
      } catch (error) {
        lastError = error;
      }
    }

    // 3) 本地预设兜底：明确告知这是本地教学示意，不冒充服务端结果。
    if (fallbackPreset) {
      sourceNode.hidden = false;
      sourceNode.textContent = mt('study.m5.localPreset', '本地预设');
      return {
        data: fallbackPreset.data,
        title: fallbackPreset.title,
        seed: fallbackPreset.data.seed ?? fallbackPreset.params?.seed ?? null,
        basis: String(fallbackPreset.data.basis || ''),
        assumptions: fallbackPreset.data.assumptions || [],
        sourceNote: String(fallbackPreset.data.source_note || ''),
        params: { ...fallbackPreset.params },
        sourceRef: fallbackPreset.template_id,
        origin: 'local',
      };
    }
    throw lastError || new Error(mt('study.m5.noData', '没有可用的演示数据'));
  }

  async function loadTemplate(templateId) {
    const mine = ++state.seq;
    const template = findTemplate(templateId);
    if (!template) {
      setStatus(mt('study.m5.noTemplate', '没有可用的演示模板，请稍后重试。'), { error: true });
      saveButton.disabled = true;
      return;
    }
    setSelectedTemplate(template);
    setStatus(mt('study.m5.loading', '正在加载演示…'));
    showError('');
    saveButton.disabled = true;
    saveButton.textContent = state.saved ? mt('study.m5.savedBtn', '已保存') : mt('study.m5.saveArtifact', '保存为学习附件');
    destroyView();
    stageHost.replaceChildren();
    let resolved;
    try {
      resolved = await resolveDemoData(template, { initial: true });
    } catch (error) {
      if (state.destroyed || mine !== state.seq) return;
      setStatus(mt('study.m5.loadFailed', '演示加载失败'), { error: true });
      showError(mt('study.m5.loadFailedReason', '原因：{v1}', { v1: error?.message || '' }));
      stageHost.replaceChildren(createInvalidDemoView(el('div'), template.kind, error, {}).element);
      if (typeof opts.onError === 'function') {
        try { opts.onError(error); } catch { /* 忽略 */ }
      }
      settleReady();
      return;
    }
    if (state.destroyed || mine !== state.seq) return;
    state.data = resolved.data;
    state.title = resolved.title || template.title || '';
    state.seed = resolved.seed;
    state.basis = resolved.basis;
    state.assumptions = Array.isArray(resolved.assumptions) ? resolved.assumptions : [];
    state.sourceNote = resolved.sourceNote;
    state.origin = resolved.origin;
    state.sourceRef = String(resolved.sourceRef || template.template_id || '');
    state.saved = false;
    renderParams(template, resolved.params);
    renderMeta();
    showError('');
    if (state.origin === 'local') {
      statusNode.classList.add('is-warning');
      setStatus(mt('study.m5.localFallback', '演示服务暂不可用，已使用本地教学示意预设（只读数据）'));
    } else {
      statusNode.classList.remove('is-warning');
      setStatus('');
    }
    const host = el('div', { class: 'm5-dialog-demo-host' });
    stageHost.replaceChildren(host);
    try {
      const viewOptions = {
        readOnly,
        onFrameReady: () => {
          if (state.origin !== 'local') setStatus('');
          showError('');
        },
      };
      state.view = template.kind === FACTORY_KIND
        ? createFactoryView(host, { kind: template.kind, title: state.title, data: state.data, seed: state.seed }, viewOptions)
        : createMechView(host, { kind: template.kind, title: state.title, data: state.data, seed: state.seed }, viewOptions);
      try { await state.view.ready; } catch { /* ready 不应 reject；兼容异常实现 */ }
      if (state.destroyed || mine !== state.seq) return;
      if (state.origin !== 'local') setStatus('');
      const mode = typeof state.view.getMode === 'function' ? state.view.getMode() : 'svg';
      if (mode === 'svg' && template.kind === MECH_KIND && state.origin !== 'local') {
        // 3D 降级是正常路径：组件内部已给出中文提示，这里不再重复报错。
        setStatus('');
      }
      saveButton.disabled = readOnly || !state.data;
      saveButton.textContent = mt('study.m5.saveArtifact', '保存为学习附件');
    } catch (error) {
      if (state.destroyed || mine !== state.seq) return;
      setStatus(mt('study.m5.renderFailed', '演示渲染失败'), { error: true });
      showError(mt('study.m5.loadFailedReason', '原因：{v1}', { v1: error?.message || '' }));
      saveButton.disabled = true;
      if (typeof opts.onError === 'function') {
        try { opts.onError(error); } catch { /* 忽略 */ }
      }
    }
    settleReady();
  }

  async function saveArtifact() {
    if (readOnly || state.saved || !state.data || !state.template) return;
    const template = state.template;
    const sourceRef = state.sourceRef || template.template_id;
    const payload = {
      kind: template.kind,
      title: state.title || template.title || mt('study.m5.dialogTitle', '机械与工厂演示'),
      data: state.data,
      source_ref: sourceRef,
      source_kind: 'mech_demo_template',
      preview: { kind: 'mech-demo', preset_id: sourceRef },
    };
    if (links.length) payload.links = links;
    saveButton.disabled = true;
    saveButton.setAttribute('aria-busy', 'true');
    showError('');
    try {
      const created = await api('/artifacts', { method: 'POST', data: payload });
      const artifactId = created?.artifact?.artifact_id;
      if (!artifactId) throw new Error(mt('study.m5.saveNoId', '保存接口未返回附件编号'));
      state.saved = true;
      saveButton.textContent = mt('study.m5.savedBtn', '已保存');
      saveButton.classList.add('is-saved');
      toast(mt('study.m5.saved', '已保存为学习附件'));
      setStatus(mt('study.m5.savedHint', '已保存为学习附件；可在题目、笔记与课堂中按固定版本打开。'));
      statusNode.classList.remove('is-warning');
      if (typeof opts.onSaved === 'function') {
        try {
          opts.onSaved({
            artifactId,
            revision: created?.revision?.revision ?? null,
            kind: payload.kind,
            title: payload.title,
            templateId: payload.source_ref,
            local: state.origin === 'local',
          });
        } catch {
          /* 回调异常不影响保存结果 */
        }
      }
    } catch (error) {
      // 不伪造成功：失败时保留「保存为学习附件」按钮并中文提示服务端原因。
      saveButton.disabled = false;
      saveButton.textContent = mt('study.m5.saveArtifact', '保存为学习附件');
      saveButton.classList.remove('is-saved');
      showError(mt('study.m5.saveFailed', '保存失败：{v1}', { v1: error?.message || '' }));
      toast(mt('study.m5.saveFailed', '保存失败：{v1}', { v1: error?.message || '' }), true);
    } finally {
      saveButton.removeAttribute('aria-busy');
    }
  }

  async function boot() {
    let templates = [];
    let source = 'api';
    try {
      const response = await api('/demos/templates');
      templates = (Array.isArray(response?.items) ? response.items : []).filter(isUsableTemplate);
      if (!templates.length) throw new Error('模板列表为空');
    } catch {
      templates = mechTemplateItems();
      source = 'local';
    }
    if (state.destroyed) return;
    state.templates = templates;
    state.source = source;
    if (source === 'local') {
      sourceNode.hidden = false;
      sourceNode.textContent = mt('study.m5.localPreset', '本地预设');
    }
    renderSelect();
    const initial = pickInitialTemplate();
    if (!initial) {
      setStatus(mt('study.m5.noTemplate', '没有可用的演示模板，请稍后重试。'), { error: true });
      settleReady();
      return;
    }
    selectNode.value = initial.template_id;
    selectNode.addEventListener('change', () => {
      loadTemplate(selectNode.value).catch(() => { /* 已由 loadTemplate 内降级 */ });
    });
    await loadTemplate(initial.template_id);
  }

  const controller = {
    element: dialog,
    ready,
    getMode: () => (state.view && typeof state.view.getMode === 'function' ? state.view.getMode() : 'svg'),
    getTemplateId: () => state.template?.template_id || '',
    getData: () => (state.data && typeof state.data === 'object' ? state.data : null),
    reload: () => loadTemplate(state.template?.template_id || ''),
    save: () => saveArtifact(),
    setTemplate: (templateId) => {
      selectNode.value = String(templateId || '');
      return loadTemplate(templateId);
    },
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      state.seq += 1;
      destroyView();
      if (dialog.open) {
        try { dialog.close(); } catch { /* 已关闭时忽略 */ }
      }
      dialog.remove();
      try { opts.trigger?.focus?.(); } catch { /* 触发按钮可能已不在 DOM */ }
      settleReady();
    },
  };

  closeButton.addEventListener('click', () => controller.destroy());
  dialog.addEventListener('close', () => controller.destroy());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) controller.destroy();
  });
  saveButton.addEventListener('click', () => {
    saveArtifact().catch(() => { /* saveArtifact 内部已提示 */ });
  });

  try {
    dialog.showModal();
  } catch {
    /* 极老浏览器不支持 showModal 时退化为普通展示 */
    dialog.setAttribute('open', '');
  }
  boot().catch((error) => {
    if (state.destroyed) return;
    setStatus(mt('study.m5.loadFailed', '演示加载失败'), { error: true });
    showError(mt('study.m5.loadFailedReason', '原因：{v1}', { v1: error?.message || '' }));
    settleReady();
  });
  return controller;
}

/** 供页面判断可展示的演示 kind（不导出也可用，这里显式导出便于接线复用）。 */
export const MECH_DEMO_KINDS = Object.freeze([MECH_KIND, FACTORY_KIND]);
export { MECH_KIND as MECH_DEMO_MECH_KIND, FACTORY_KIND as MECH_DEMO_FACTORY_KIND };
