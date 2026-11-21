/**
 * 物理演示播放器 —— 基于 physics-in-motion 的确定性物理内核（MIT，Copyright (c) 2026 xuxuezhou）。
 *
 * 上游把物理计算（physics/、sim/）与 React 渲染层分离；这里保留其 SimResult 数据契约，
 * 把 SVG 场景、播放器、图表、物理量面板改写成原生 DOM/SVG，避免在知序中引入 React/Next
 * 构建链。动画、公式、图表、能量条读取同一份 SimResult，播放时钟只驱动“显示时间”。
 */
import { el, badge } from '../core/dom.js';
import { renderMath } from '../core/markdown.js';
import { t } from '../core/i18n.js';

const NS = 'http://www.w3.org/2000/svg';
const VIEW_W = 720;
const VIEW_H = 480;
const MIN_BALL_PX = 9;
const MIN_BLOCK_W_PX = 26;
const MIN_BLOCK_H_PX = 16;

export const DEFAULT_TOGGLES = {
  velocity: true,
  acceleration: false,
  forces: true,
  netForce: false,
  trail: true,
  axes: false,
  labels: true,
};

function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child) node.append(child);
  }
  return node;
}

function text(value, attrs = {}) {
  return svg('text', attrs, [document.createTextNode(String(value))]);
}

export function frameIndexAt(sim, t) {
  const index = Math.round(t / sim.dt);
  return Math.max(0, Math.min(sim.frames.length - 1, index));
}

export function phaseAt(sim, t) {
  return sim.phases.find((phase) => t >= phase.tStart - 1e-9 && t <= phase.tEnd + 1e-9);
}

/** 每个 SimResult 只计算一次矢量缩放，避免每次重绘扫描全部帧。 */
const scaleCache = new WeakMap();

function scalesOf(sim) {
  const cached = scaleCache.get(sim);
  if (cached) return cached;
  const { world } = sim;
  const span = Math.max(world.xMax - world.xMin, world.yMax - world.yMin);
  let maxV = 1e-6;
  let maxF = 1e-6;
  let maxA = 1e-6;
  for (const frame of sim.frames) {
    for (const body of Object.values(frame.bodies)) {
      maxV = Math.max(maxV, Math.hypot(body.vel.x, body.vel.y));
      maxA = Math.max(maxA, Math.hypot(body.acc.x, body.acc.y));
      for (const force of body.forces) {
        maxF = Math.max(maxF, Math.hypot(force.vec.x, force.vec.y));
      }
    }
  }
  const scales = { v: (span * 0.16) / maxV, f: (span * 0.14) / maxF, a: (span * 0.12) / maxA };
  scaleCache.set(sim, scales);
  return scales;
}

/* ------------------------------------------------------------------------- *
 * 场景渲染：静态几何 + 当前帧物体/矢量/轨迹/波形/光线
 * ------------------------------------------------------------------------- */

function renderScene(sim, t, toggles) {
  const { world } = sim;
  const pad = 24;
  const sx = (VIEW_W - 2 * pad) / (world.xMax - world.xMin);
  const sy = (VIEW_H - 2 * pad) / (world.yMax - world.yMin);
  const scale = Math.min(sx, sy);
  const ox = pad + ((VIEW_W - 2 * pad) - scale * (world.xMax - world.xMin)) / 2;
  const oy = pad + ((VIEW_H - 2 * pad) - scale * (world.yMax - world.yMin)) / 2;
  const X = (x) => ox + (x - world.xMin) * scale;
  const Y = (y) => VIEW_H - oy - (y - world.yMin) * scale;
  const P = (point) => `${X(point.x)},${Y(point.y)}`;
  const frame = sim.frames[frameIndexAt(sim, t)];
  const scales = scalesOf(sim);

  const root = svg('svg', {
    viewBox: `0 0 ${VIEW_W} ${VIEW_H}`,
    class: 'pg-phys-svg',
    role: 'img',
    'aria-label': t('physics.physicsAnimationScene'),
  });

  root.append(svg('defs', {}, [
    [['arrow-v', 'var(--c-velocity)'], ['arrow-f', 'var(--c-force)'], ['arrow-net', 'var(--c-net)'],
      ['arrow-a', 'var(--c-accel)'], ['arrow-ink', 'var(--ink-soft)'], ['arrow-light', 'var(--c-light)']]
      .map(([id, color]) => svg('marker', {
        id, markerWidth: 10, markerHeight: 9, refX: 7.5, refY: 4, orient: 'auto',
      }, [svg('path', { d: 'M0,0.5 L8,4 L0,7.5 Z', fill: color })])),
  ]));

  if (toggles.axes) {
    const x0 = Math.max(world.xMin, 0);
    const y0 = Math.max(world.yMin, 0);
    root.append(svg('g', { class: 'pg-phys-axes' }, [
      svg('line', { x1: X(world.xMin), y1: Y(y0), x2: X(world.xMax), y2: Y(y0), markerEnd: 'url(#arrow-ink)' }),
      svg('line', { x1: X(x0), y1: Y(world.yMin), x2: X(x0), y2: Y(world.yMax), markerEnd: 'url(#arrow-ink)' }),
      text('x', { x: X(world.xMax) - 14, y: Y(y0) - 6 }),
      text('y', { x: X(x0) + 6, y: Y(world.yMax) + 14 }),
    ]));
  }

  for (const primitive of sim.statics) root.append(staticShape(primitive, X, Y, P, scale, toggles.labels));

  if (sim.waveX && frame.fields?.waveY) {
    const wave = (values, cls) => svg('polyline', {
      points: sim.waveX.map((x, i) => `${X(x)},${Y(values[i])}`).join(' '),
      class: cls,
    });
    if (frame.fields.wave1Y) root.append(wave(frame.fields.wave1Y, 'pg-phys-wave-component'));
    if (frame.fields.wave2Y) root.append(wave(frame.fields.wave2Y, 'pg-phys-wave-component pg-phys-wave-2'));
    root.append(wave(frame.fields.waveY, 'pg-phys-wave-sum'));
  }

  frame.fields?.rays?.forEach((ray) => {
    root.append(svg('line', {
      x1: X(ray.a.x), y1: Y(ray.a.y), x2: X(ray.b.x), y2: Y(ray.b.y),
      class: `pg-phys-ray pg-phys-ray-${ray.kind}`,
      markerEnd: 'url(#arrow-light)',
    }));
  });

  if (toggles.trail) {
    const step = Math.max(1, Math.floor(sim.frames.length / 400));
    const index = frameIndexAt(sim, t);
    for (const [id, def] of Object.entries(sim.bodies)) {
      if (!def.trail) continue;
      const points = [];
      for (let i = 0; i <= index; i += step) {
        const body = sim.frames[i].bodies[id];
        if (body) points.push(`${X(body.pos.x)},${Y(body.pos.y)}`);
      }
      const current = frame.bodies[id];
      if (current) points.push(`${X(current.pos.x)},${Y(current.pos.y)}`);
      root.append(svg('polyline', { points: points.join(' '), class: 'pg-phys-trail' }));
    }
  }

  Object.entries(sim.bodies).forEach(([id, def], bodyIndex) => {
    const body = frame.bodies[id];
    if (!body || body.visible === false) return;
    const anchor = bodyCenterPx(def, body, X, Y, scale);
    const group = svg('g', { class: `pg-phys-body pg-phys-body-tone-${bodyIndex % 3}` });
    group.append(bodyShape(id, def, body, X, Y, scale, toggles.labels));
    if (toggles.forces) {
      body.forces.filter((force) => !force.net).forEach((force) => {
        group.append(vectorArrow(anchor, { x: force.vec.x * scales.f, y: force.vec.y * scales.f },
          toggles.labels ? force.label : undefined, 'force', 'arrow-f', scale));
      });
    }
    if (toggles.netForce) {
      body.forces.filter((force) => force.net).forEach((force) => {
        group.append(vectorArrow(anchor, { x: force.vec.x * scales.f, y: force.vec.y * scales.f },
          toggles.labels ? force.label : undefined, 'net', 'arrow-net', scale));
      });
    }
    if (toggles.velocity && Math.hypot(body.vel.x, body.vel.y) > 1e-6) {
      group.append(vectorArrow(anchor, { x: body.vel.x * scales.v, y: body.vel.y * scales.v },
        toggles.labels ? 'v' : undefined, 'velocity', 'arrow-v', scale));
    }
    if (toggles.acceleration && Math.hypot(body.acc.x, body.acc.y) > 1e-6) {
      group.append(vectorArrow(anchor, { x: body.acc.x * scales.a, y: body.acc.y * scales.a },
        toggles.labels ? 'a' : undefined, 'accel', 'arrow-a', scale));
    }
    root.append(group);
  });

  frame.links?.forEach((link) => {
    root.append(svg('line', {
      x1: X(link.a.x), y1: Y(link.a.y), x2: X(link.b.x), y2: Y(link.b.y),
      class: `pg-phys-link pg-phys-link-${link.kind}`,
    }));
  });

  return root;
}

function staticShape(primitive, X, Y, P, scale, showLabels) {
  switch (primitive.kind) {
    case 'polygon':
      return svg('polygon', { points: primitive.pts.map(P).join(' '), class: primitive.fill ? 'pg-phys-poly-fill' : 'pg-phys-poly' });
    case 'segment':
      return svg('line', {
        x1: X(primitive.a.x), y1: Y(primitive.a.y), x2: X(primitive.b.x), y2: Y(primitive.b.y),
        class: `pg-phys-seg${primitive.dashed ? ' is-dashed' : ''}${primitive.thin ? ' is-thin' : ''}`,
      });
    case 'ground': {
      const count = Math.floor(Math.abs(X(primitive.b.x) - X(primitive.a.x)) / 14);
      const hats = [];
      for (let i = 0; i < count; i += 1) {
        const x = X(primitive.a.x) + i * 14 + 6;
        const y = Y(primitive.a.y);
        hats.push(svg('line', { x1: x, y1: y, x2: x - 8, y2: y + 8, class: 'pg-phys-hatch' }));
      }
      return svg('g', {}, [
        svg('line', { x1: X(primitive.a.x), y1: Y(primitive.a.y), x2: X(primitive.b.x), y2: Y(primitive.b.y), class: 'pg-phys-seg' }),
        ...hats,
      ]);
    }
    case 'circleTrack':
      return svg('circle', { cx: X(primitive.c.x), cy: Y(primitive.c.y), r: primitive.r * scale, class: 'pg-phys-circle-track' });
    case 'region': {
      const x = X(primitive.x);
      const y = Y(primitive.y + primitive.h);
      const w = primitive.w * scale;
      const h = primitive.h * scale;
      const marks = [];
      if (primitive.style === 'magneticIn' || primitive.style === 'magneticOut') {
        const gap = 34;
        for (let i = x + 17; i < x + w; i += gap) {
          for (let j = y + 17; j < y + h; j += gap) {
            marks.push(primitive.style === 'magneticIn'
              ? svg('g', { class: 'pg-phys-bmark' }, [
                svg('line', { x1: i - 4, y1: j - 4, x2: i + 4, y2: j + 4 }),
                svg('line', { x1: i - 4, y1: j + 4, x2: i + 4, y2: j - 4 }),
              ])
              : svg('circle', { cx: i, cy: j, r: 2.4, class: 'pg-phys-bmark-dot' }));
          }
        }
      }
      return svg('g', {}, [
        svg('rect', { x, y, width: w, height: h, class: `pg-phys-region pg-phys-region-${primitive.style}` }),
        ...marks,
        showLabels && primitive.label ? text(primitive.label, { x: x + 6, y: y + 16, class: 'pg-phys-region-label' }) : null,
      ].filter(Boolean));
    }
    case 'label':
      return showLabels ? text(primitive.text, { x: X(primitive.at.x), y: Y(primitive.at.y), class: 'pg-phys-static-label' }) : svg('g');
    case 'angle': {
      const r = 26;
      const a0 = Math.min(primitive.from, primitive.to);
      const a1 = Math.max(primitive.from, primitive.to);
      const p0 = { x: X(primitive.at.x) + r * Math.cos(a0), y: Y(primitive.at.y) - r * Math.sin(a0) };
      const p1 = { x: X(primitive.at.x) + r * Math.cos(a1), y: Y(primitive.at.y) - r * Math.sin(a1) };
      const mid = (a0 + a1) / 2;
      return svg('g', { class: 'pg-phys-angle' }, [
        svg('path', { d: `M ${p0.x} ${p0.y} A ${r} ${r} 0 0 0 ${p1.x} ${p1.y}`, fill: 'none' }),
        showLabels ? text(primitive.text, {
          x: X(primitive.at.x) + (r + 16) * Math.cos(mid),
          y: Y(primitive.at.y) - (r + 12) * Math.sin(mid),
        }) : null,
      ].filter(Boolean));
    }
    case 'arrow':
      return svg('g', {}, [
        svg('line', {
          x1: X(primitive.a.x), y1: Y(primitive.a.y), x2: X(primitive.b.x), y2: Y(primitive.b.y),
          class: `pg-phys-static-arrow pg-phys-arrow-${primitive.color || 'ink'}`,
          markerEnd: `url(#arrow-${primitive.color === 'light' ? 'light' : 'ink'})`,
        }),
        showLabels && primitive.text ? text(primitive.text, {
          x: (X(primitive.a.x) + X(primitive.b.x)) / 2,
          y: Y(primitive.a.y) - 6,
          class: 'pg-phys-static-label pg-phys-label-small',
        }) : null,
      ].filter(Boolean));
    case 'dim':
      return svg('g', { class: 'pg-phys-dim' }, [
        svg('line', { x1: X(primitive.a.x), y1: Y(primitive.a.y), x2: X(primitive.b.x), y2: Y(primitive.b.y) }),
        showLabels ? text(primitive.text, {
          x: (X(primitive.a.x) + X(primitive.b.x)) / 2 + 4,
          y: (Y(primitive.a.y) + Y(primitive.b.y)) / 2 - 5,
        }) : null,
      ].filter(Boolean));
    default:
      return svg('g');
  }
}

function bodyCenterPx(def, body, X, Y, scale) {
  const cx = X(body.pos.x);
  const cy = Y(body.pos.y);
  if (def.shape.kind === 'ball') return { x: cx, y: cy };
  const minW = def.shape.kind === 'car' ? 46 : MIN_BLOCK_W_PX;
  const k = Math.max(1, minW / (def.shape.w * scale), MIN_BLOCK_H_PX / (def.shape.h * scale));
  const h = def.shape.h * scale * k;
  const nx = -Math.sin(body.angle ?? 0);
  const ny = Math.cos(body.angle ?? 0);
  return { x: cx + nx * (h / 2), y: cy - ny * (h / 2) };
}

function bodyShape(id, def, body, X, Y, scale, showLabel) {
  const cx = X(body.pos.x);
  const cy = Y(body.pos.y);
  if (def.shape.kind === 'ball') {
    const r = Math.max(def.shape.r * scale, MIN_BALL_PX);
    return svg('g', {}, [
      svg('circle', { cx, cy, r, class: 'pg-phys-ball' }),
      svg('circle', { cx, cy, r: 1.8, class: 'pg-phys-body-dot' }),
      showLabel && def.label ? text(def.label, { x: cx + r + 5, y: cy - r - 5, class: 'pg-phys-body-label' }) : null,
    ].filter(Boolean));
  }
  const minW = def.shape.kind === 'car' ? 46 : MIN_BLOCK_W_PX;
  const k = Math.max(1, minW / (def.shape.w * scale), MIN_BLOCK_H_PX / (def.shape.h * scale));
  const w = def.shape.w * scale * k;
  const h = def.shape.h * scale * k;
  const angle = -(body.angle ?? 0) * (180 / Math.PI);
  const nx = -Math.sin(body.angle ?? 0);
  const ny = Math.cos(body.angle ?? 0);
  const bx = cx + nx * (h / 2);
  const by = cy - ny * (h / 2);
  if (def.shape.kind === 'car') {
    return svg('g', { transform: `translate(${bx},${by}) rotate(${angle})` }, [
      svg('rect', { x: -w / 2, y: -h / 2, width: w, height: h * 0.62, rx: 3, class: 'pg-phys-block' }),
      svg('rect', { x: -w * 0.24, y: -h, width: w * 0.46, height: h * 0.55, rx: 3, class: 'pg-phys-block' }),
      svg('circle', { cx: -w * 0.28, cy: h * 0.18, r: h * 0.26, class: 'pg-phys-wheel' }),
      svg('circle', { cx: w * 0.28, cy: h * 0.18, r: h * 0.26, class: 'pg-phys-wheel' }),
      showLabel && def.label ? text(def.label, { x: 0, y: -h - 6, class: 'pg-phys-body-label', textAnchor: 'middle' }) : null,
    ].filter(Boolean));
  }
  return svg('g', { transform: `translate(${bx},${by}) rotate(${angle})`, 'data-body': id }, [
    svg('rect', { x: -w / 2, y: -h / 2, width: w, height: h, class: 'pg-phys-block' }),
    showLabel && def.label ? text(def.label, { x: 0, y: -h / 2 - 5, class: 'pg-phys-body-label', textAnchor: 'middle' }) : null,
  ].filter(Boolean));
}

function vectorArrow(anchor, vec, label, cls, marker, scale) {
  const mag = Math.hypot(vec.x, vec.y);
  if (mag < 1e-9) return svg('g');
  const x1 = anchor.x;
  const y1 = anchor.y;
  const x2 = x1 + vec.x * scale;
  const y2 = y1 - vec.y * scale;
  return svg('g', { class: `pg-phys-vec pg-phys-vec-${cls}` }, [
    svg('line', { x1, y1, x2, y2, markerEnd: `url(#${marker})` }),
    label ? text(label, {
      x: x2 + (x2 - x1) * 0.12 + 3,
      y: y2 + (y2 - y1) * 0.12 - 3,
      class: 'pg-phys-vec-label',
    }) : null,
  ].filter(Boolean));
}

/* ------------------------------------------------------------------------- *
 * 图表与能量条
 * ------------------------------------------------------------------------- */

function fmt(value) {
  if (!Number.isFinite(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(1);
  return value.toFixed(2);
}

function createGraphPanel(sim, onSeek) {
  let active = 0;
  const host = el('div', { class: 'pg-phys-graph-host' });
  const tabs = el('div', { class: 'pg-phys-graph-tabs' });
  const node = el('div', { class: 'pg-phys-graph' }, tabs, host);

  function drawTabs() {
    tabs.replaceChildren(...sim.graphs.map((graph, index) => el('button', {
      type: 'button',
      class: `pg-phys-graph-tab${index === active ? ' is-active' : ''}`,
      onClick: () => { active = index; drawTabs(); draw(lastT); },
    }, graph.label)));
  }

  function draw(t) {
    lastT = t;
    const graph = sim.graphs[active];
    if (!graph) { host.replaceChildren(); return; }
    host.replaceChildren(lineChart(sim, graph, t, onSeek));
  }

  let lastT = 0;
  drawTabs();
  return { node, draw };
}

function lineChart(sim, graph, t, onSeek) {
  const W = 320;
  const H = 150;
  const padL = 40;
  const padB = 22;
  const padT = 10;
  const padR = 8;
  const values = graph.values;
  const finite = values.filter((value) => Number.isFinite(value));
  const yMin = Math.min(0, ...finite);
  const yMax = Math.max(0.001, ...finite);
  const X = (index) => padL + (index / Math.max(1, values.length - 1)) * (W - padL - padR);
  const Y = (value) => padT + (1 - (value - yMin) / (yMax - yMin || 1)) * (H - padT - padB);
  const points = values
    .map((value, index) => (Number.isFinite(value) ? `${X(index)},${Y(value)}` : null))
    .filter(Boolean)
    .join(' ');
  const index = frameIndexAt(sim, t);
  const current = values[index];

  const root = svg('svg', {
    viewBox: `0 0 ${W} ${H}`,
    class: 'pg-phys-line-chart',
    role: 'img',
    'aria-label': t('physics.chart', { v1: graph.label }),
  });
  root.addEventListener('click', (event) => {
    const rect = root.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const fraction = Math.max(0, Math.min(1, (px - padL) / (W - padL - padR)));
    onSeek(fraction * sim.duration);
  });
  root.append(
    svg('line', { x1: padL, y1: Y(0), x2: W - padR, y2: Y(0), class: 'pg-phys-chart-axis' }),
    svg('line', { x1: padL, y1: padT, x2: padL, y2: H - padB, class: 'pg-phys-chart-axis' }),
    text(`${graph.yLabel}/${graph.unit}`, { x: 4, y: padT + 10, class: 'pg-phys-chart-label' }),
    text('t/s', { x: W - 30, y: H - 6, class: 'pg-phys-chart-label' }),
    text(fmt(yMax), { x: padL - 4, y: Y(yMax) + 4, class: 'pg-phys-chart-tick', textAnchor: 'end' }),
    text(fmt(yMin), { x: padL - 4, y: Y(yMin) + 4, class: 'pg-phys-chart-tick', textAnchor: 'end' }),
    svg('polyline', { points, class: 'pg-phys-chart-line' }),
    svg('line', { x1: X(index), y1: padT, x2: X(index), y2: H - padB, class: 'pg-phys-chart-cursor' }),
    Number.isFinite(current) ? svg('circle', { cx: X(index), cy: Y(current), r: 3.5, class: 'pg-phys-chart-dot' }) : null,
    Number.isFinite(current) ? text(`${fmt(current)} ${graph.unit}`, {
      x: Math.min(X(index) + 6, W - 60), y: padT + 12, class: 'pg-phys-chart-cur',
    }) : null,
  );
  return root;
}

function createEnergyPanel(sim) {
  const keys = sim.energyKeys || [];
  if (!keys.length) return null;
  let maxEnergy = 1e-9;
  for (const frame of sim.frames) {
    for (const key of keys) maxEnergy = Math.max(maxEnergy, frame.energy?.[key.key] ?? 0);
  }
  const bars = el('div', { class: 'pg-phys-energy-bars' });
  const node = el('div', { class: 'pg-phys-energy' },
    el('div', { class: 'pg-phys-panel-title' }, t('physics.energyBarChart'), el('span', { class: 'pg-phys-unit' }, '(J)')),
    bars);
  const items = keys.map((key, index) => {
    const fill = el('div', { class: `pg-phys-bar-fill pg-phys-bar-fill-${index % 3}` });
    const value = el('div', { class: 'pg-phys-bar-value' });
    bars.append(el('div', { class: 'pg-phys-bar-item' },
      el('div', { class: 'pg-phys-bar-track' }, fill),
      value,
      el('div', { class: 'pg-phys-bar-label' }, key.label)));
    return { key, fill, value };
  });
  function draw(t) {
    const frame = sim.frames[frameIndexAt(sim, t)];
    for (const item of items) {
      const value = frame.energy?.[item.key.key] ?? 0;
      item.fill.style.height = `${Math.max(1, (value / maxEnergy) * 100)}%`;
      item.value.textContent = value.toFixed(1);
    }
  }
  return { node, draw };
}

/* ------------------------------------------------------------------------- *
 * 物理量、阶段公式、检查结果
 * ------------------------------------------------------------------------- */

function createQuantityPanel(sim) {
  const grid = el('div', { class: 'pg-phys-quantity-grid' });
  const node = el('div', { class: 'pg-phys-quantity' },
    el('div', { class: 'pg-phys-panel-title' }, t('physics.currentQuantities')), grid);
  function draw(t) {
    const frame = sim.frames[frameIndexAt(sim, t)];
    const entries = Object.entries(frame.quantities).filter(([key]) => !key.startsWith('_'));
    grid.replaceChildren(...entries.map(([key, value]) => el('div', { class: 'pg-phys-quantity-item' },
      el('span', {}, key),
      el('strong', {}, Number.isFinite(value) ? value.toFixed(2) : '—'))));
  }
  return { node, draw };
}

function createEquationPanel(sim) {
  const body = el('div', { class: 'pg-phys-equation-body' });
  const title = el('span', { class: 'pg-phys-phase-chip' });
  const node = el('div', { class: 'pg-phys-equation' },
    el('div', { class: 'pg-phys-panel-title' }, t('physics.currentStageFormulas'), title), body);
  function draw(t) {
    const phase = phaseAt(sim, t);
    title.textContent = phase?.label || '—';
    body.replaceChildren(...(phase?.equations || []).map((equation) => {
      const math = el('div', { class: 'pg-phys-eq-math' });
      math.innerHTML = renderMath(equation.latex, true);
      return el('div', { class: 'pg-phys-eq-item' }, math,
        equation.note ? el('div', { class: 'pg-phys-eq-note' }, equation.note) : null);
    }));
  }
  return { node, draw };
}

function checksCard(sim) {
  const passed = sim.checks.filter((check) => check.passed).length;
  return el('div', { class: 'pg-phys-checks' },
    el('div', { class: 'pg-phys-panel-title' },
      t('physics.autoVerify'), badge(t('physics.passed', { v1: passed, v2: sim.checks.length }), passed === sim.checks.length ? 'teal' : 'amber')),
    el('ul', {}, ...sim.checks.map((check) => el('li', {},
      el('span', { class: `pg-phys-check-dot${check.passed ? '' : ' is-fail'}` }),
      el('span', {}, `${check.label}：${check.detail}`)))),
    sim.notes?.length ? el('p', { class: 'pg-phys-note' }, ...sim.notes.map((note) => el('span', {}, note))) : null);
}

/* ------------------------------------------------------------------------- *
 * 题目、答案与解析
 * ------------------------------------------------------------------------- */

function questionCard(demo, onSeek) {
  const answerBox = el('div', { class: 'pg-phys-answer-box', hidden: true });
  const toggle = el('button', {
    type: 'button',
    class: 'button secondary small',
    onClick: () => {
      answerBox.hidden = !answerBox.hidden;
      toggle.textContent = answerBox.hidden ? t('physics.showAnswerAndSolution') : t('physics.hideAnswerAndSolution');
    },
  }, t('physics.showAnswerAndSolution'));

  const subQuestions = (demo.subQuestions || []).map((item) => {
    const answerLine = item.answer ? el('p', { class: 'pg-phys-subq-answer' },
      t('physics.answer', { v1: item.answer.value ?? '', v2: item.answer.unit ?? '' })) : null;
    if (answerLine && item.answer.expression) {
      const expression = el('span', { class: 'pg-phys-subq-expr' });
      expression.innerHTML = ` （${renderMath(item.answer.expression, false)}）`;
      answerLine.append(expression);
    }
    return el('div', { class: 'pg-phys-subq' }, el('p', {}, item.text), answerLine);
  });

  const solution = demo.solution ? el('div', { class: 'pg-phys-solution' },
    el('div', { class: 'pg-phys-topics' }, ...(demo.solution.topics || []).map((topic) => badge(topic))),
    el('details', { class: 'pg-phys-model' },
      el('summary', {}, t('physics.modelingAssumptions')),
      el('p', {}, demo.solution.modelDescription || ''),
      el('p', {}, (demo.solution.assumptions || []).join('；'))),
    el('div', { class: 'pg-phys-panel-title' }, t('physics.stepByStepSolution'), el('span', { class: 'pg-phys-unit' }, t('physics.clickAStepToJumpToIt'))),
    el('ol', { class: 'pg-phys-steps' }, ...(demo.solution.steps || []).map((step) => {
      const target = stepTarget(demo, step);
      const math = el('div', { class: 'pg-phys-step-math' });
      math.innerHTML = renderMath(step.latex || '', true);
      return el('li', {
        class: `pg-phys-step${target !== null ? ' is-clickable' : ''}`,
        onClick: () => { if (target !== null) onSeek(target); },
      },
      el('div', { class: 'pg-phys-step-head' },
        el('strong', {}, step.title), el('span', { class: 'pg-phys-step-law' }, step.law || ''),
        target !== null ? el('span', { class: 'pg-phys-step-jump' }, '⇢') : null),
      math,
      el('p', {}, step.derivation || ''),
      el('p', { class: 'pg-phys-step-why' }, step.why || ''));
    })),
    demo.solution.finalConclusion ? el('p', { class: 'pg-phys-conclusion' }, demo.solution.finalConclusion) : null,
    (demo.solution.commonMistakes || []).length ? el('details', { class: 'pg-phys-mistakes' },
      el('summary', {}, t('physics.commonMistakes')),
      el('ul', {}, ...demo.solution.commonMistakes.map((item) => el('li', {}, item)))) : null,
  ) : null;

  answerBox.append(
    el('p', { class: 'pg-phys-answer-summary' }, t('physics.referenceAnswer', { v1: demo.answerSummary || '—' })),
    ...subQuestions,
    solution,
  );

  return el('div', { class: 'pg-phys-question' },
    el('div', { class: 'pg-phys-panel-title' }, t('physics.question'), toggle),
    el('p', { class: 'pg-phys-question-text' }, demo.question || t('physics.questionTextNotEnteredYet')),
    answerBox);
}

function stepTarget(demo, step) {
  const sim = demo._sim;
  if (!sim) return null;
  if (step.bindEvent) {
    const event = sim.events.find((item) => item.id === step.bindEvent);
    if (event) return event.t;
  }
  if (step.bindPhase) {
    const phase = sim.phases.find((item) => item.id === step.bindPhase);
    if (phase) return phase.tStart;
  }
  return null;
}

/* ------------------------------------------------------------------------- *
 * 播放器主体
 * ------------------------------------------------------------------------- */

export function renderPhysicsDemo(host, demo, buildSim) {
  if (!demo.visualization) {
    host.replaceChildren(el('div', { class: 'error-panel' }, t('physics.thisDemoHasNoAnimationConfiguration')));
    return null;
  }
  let sim;
  try {
    sim = buildSim(demo.visualization.builder, demo.visualization.params);
  } catch (error) {
    host.replaceChildren(el('div', { class: 'error-panel' }, t('physics.theAnimationFailedToRun', { v1: error.message })));
    return null;
  }
  demo._sim = sim;

  const toggles = { ...DEFAULT_TOGGLES };
  const state = { t: 0, playing: false, speed: 1 };
  let raf = 0;
  let lastWall = 0;

  const sceneHost = el('div', { class: 'pg-phys-scene' });
  const caption = el('p', { class: 'pg-phys-caption' },
    demo.visualization.schematicParams
      ? t('physics.simulatedWithTheIdealModelFromThe')
      : t('physics.simulatedWithTheIdealModelTheAnimation'));
  const playButton = el('button', { type: 'button', class: 'pg-phys-ctrl is-primary', 'aria-label': t('physics.play') }, '▶');
  const backButton = el('button', { type: 'button', class: 'pg-phys-ctrl', 'aria-label': t('physics.stepBack'), title: t('physics.stepBack') }, '⏮');
  const forwardButton = el('button', { type: 'button', class: 'pg-phys-ctrl', 'aria-label': t('physics.stepForward'), title: t('physics.stepForward') }, '⏭');
  const resetButton = el('button', { type: 'button', class: 'pg-phys-ctrl', 'aria-label': t('physics.reset'), title: t('physics.reset') }, '↺');
  const timeline = el('input', {
    type: 'range', min: '0', max: String(sim.duration), step: 'any', value: '0',
    class: 'pg-phys-timeline', 'aria-label': t('physics.timeline'),
  });
  const eventMarks = el('div', { class: 'pg-phys-event-marks' });
  for (const event of sim.events) {
    eventMarks.append(el('button', {
      type: 'button',
      class: 'pg-phys-event-mark',
      style: `left:${(event.t / sim.duration) * 100}%`,
      title: `${event.label}（t=${event.t.toFixed(2)}s）`,
      onClick: () => seek(event.t),
    }, el('span', { class: 'pg-phys-event-dot' }), el('span', { class: 'pg-phys-event-label' }, event.label)));
  }
  const speedGroup = el('div', { class: 'pg-phys-speed', role: 'group', 'aria-label': t('physics.playbackSpeed') });
  const timeReadout = el('span', { class: 'pg-phys-time' }, 't = 0.00 s');
  const phaseChip = el('span', { class: 'pg-phys-phase-chip' });

  const graph = createGraphPanel(sim, (time) => seek(time));
  const energy = createEnergyPanel(sim);
  const quantities = createQuantityPanel(sim);
  const equations = createEquationPanel(sim);

  const togglePanel = el('div', { class: 'pg-phys-toggles' });
  const toggleItems = [
    ['velocity', t('physics.velocity'), Object.keys(sim.bodies).length > 0],
    ['acceleration', t('physics.acceleration'), Object.keys(sim.bodies).length > 0],
    ['forces', t('physics.forces'), Object.keys(sim.bodies).length > 0],
    ['netForce', t('physics.netForce'), Object.keys(sim.bodies).length > 0],
    ['trail', t('physics.trajectory'), Object.keys(sim.bodies).length > 0],
    ['axes', t('physics.coordinates'), true],
    ['labels', t('physics.labels'), true],
  ].filter(([, , show]) => show);
  function renderToggles() {
    togglePanel.replaceChildren(...toggleItems.map(([key, label]) => el('button', {
      type: 'button',
      class: `pg-phys-toggle${toggles[key] ? ' is-on' : ''}`,
      'aria-pressed': String(toggles[key]),
      onClick: () => { toggles[key] = !toggles[key]; renderToggles(); paint(); },
    }, label)));
  }
  renderToggles();

  function renderSpeeds() {
    speedGroup.replaceChildren(...[0.25, 0.5, 1, 2].map((speed) => el('button', {
      type: 'button',
      class: `pg-phys-speed-btn${state.speed === speed ? ' is-active' : ''}`,
      onClick: () => { state.speed = speed; renderSpeeds(); },
    }, `${speed}×`)));
  }
  renderSpeeds();

  function paint() {
    if (!sceneHost.isConnected && state.playing) {
      // 路由切换后容器已脱离文档：停止时钟，避免后台空转。
      state.playing = false;
      cancelAnimationFrame(raf);
    }
    sceneHost.replaceChildren(renderScene(sim, state.t, toggles));
    timeline.value = String(state.t);
    playButton.textContent = state.playing ? '⏸' : '▶';
    playButton.setAttribute('aria-label', state.playing ? t('focus.pause') : t('physics.play'));
    timeReadout.textContent = `t = ${state.t.toFixed(2)} s`;
    phaseChip.textContent = phaseAt(sim, state.t)?.label || '';
    graph.draw(state.t);
    energy?.draw(state.t);
    quantities.draw(state.t);
    equations.draw(state.t);
  }

  function tick(now) {
    if (!state.playing) return;
    const dt = Math.min(0.1, (now - lastWall) / 1000);
    lastWall = now;
    state.t += dt * state.speed;
    if (state.t >= sim.duration) {
      state.t = sim.duration;
      state.playing = false;
    }
    paint();
    if (state.playing) raf = requestAnimationFrame(tick);
  }

  function play() {
    if (state.playing) return;
    if (state.t >= sim.duration - 1e-9) state.t = 0;
    state.playing = true;
    lastWall = performance.now();
    raf = requestAnimationFrame(tick);
    paint();
  }

  function pause() {
    state.playing = false;
    cancelAnimationFrame(raf);
    paint();
  }

  function seek(time) {
    state.t = Math.max(0, Math.min(sim.duration, time));
    paint();
  }

  playButton.addEventListener('click', () => (state.playing ? pause() : play()));
  backButton.addEventListener('click', () => { pause(); seek(state.t - sim.dt); });
  forwardButton.addEventListener('click', () => { pause(); seek(state.t + sim.dt); });
  resetButton.addEventListener('click', () => { pause(); seek(0); });
  timeline.addEventListener('input', () => seek(Number(timeline.value)));

  const controls = el('div', { class: 'pg-phys-controls' },
    el('div', { class: 'pg-phys-timeline-wrap' }, timeline, eventMarks),
    el('div', { class: 'pg-phys-control-row' },
      el('div', { class: 'pg-phys-ctrl-group' }, backButton, playButton, forwardButton, resetButton),
      speedGroup,
      timeReadout,
      phaseChip));

  const sceneCard = el('section', { class: 'card pg-phys-scene-card' },
    sceneHost, caption, controls, togglePanel);

  const chartsRow = el('div', { class: 'pg-phys-charts' }, graph.node, energy?.node || null);

  const panels = el('div', { class: 'pg-phys-panels' }, quantities.node, equations.node);

  const node = el('div', { class: 'pg-phys-layout' },
    el('div', { class: 'pg-phys-main' }, sceneCard, chartsRow, panels),
    el('div', { class: 'pg-phys-side' },
      questionCard(demo, (time) => { pause(); seek(time); }),
      checksCard(sim)));

  host.replaceChildren(node);
  paint();
  // 让答案区的步骤跳转使用同一实例的 seek
  demo._seek = (time) => { pause(); seek(time); };
  demo._sim = sim;
  // 步骤点击使用 demo._seek，而不是创建时捕获的旧函数
  return {
    destroy() { state.playing = false; cancelAnimationFrame(raf); },
  };
}
