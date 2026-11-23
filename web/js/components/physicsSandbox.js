/* Matter.js 物理自由沙盒（方案 §6.3）。
 *
 * 用法：
 *   const controller = createPhysicsSandbox(container, scene, {readOnly:true});
 *   await controller.ready;
 *   controller.getScene(); controller.pngURI(); controller.destroy();
 *
 * 或 openPhysicsSandbox({links, ref, onSaved}) 打开可交互弹层并保存为
 * `physics_sandbox` 学习附件。
 *
 * 确定性边界：Matter.js 的逐步积分不保证跨运行逐位一致，这里只用于「探索模式」，
 * 页面固定提示「每次运行结果可能有细微差异」；需要回放与对账的教学演示继续使用
 * physics-in-motion 的确定性内核。
 *
 * 视口无关性：模拟固定在 640×380 设计坐标运行，视口只做渲染缩放
 * （Render hasBounds + lookAt；鼠标经 Mouse.setScale/setOffset 换算回设计坐标），
 * 轨迹与读数和容器宽度逐位一致，Matter 的 slop/静止阈值等世界常量不受视口影响。
 */
import { api } from '../core/api.js';
import { busy, button, el, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

const SCRIPT_URL = '/static/vendor/matter/matter.min.js';
const SCRIPT_MARK = 'ossMatterScript';
const LOAD_TIMEOUT_MS = 15000;
export const SANDBOX_KIND = 'physics_sandbox';
export const MAX_BODIES = 60;
export const MAX_CONSTRAINTS = 40;
export const SANDBOX_SHAPES = ['rect', 'circle', 'slope'];
const DESIGN_WIDTH = 640;
const DESIGN_HEIGHT = 380;

let loaderPromise = null;

function ossText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function finite(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function isEnglishLocale() {
  return /^en/i.test(document.documentElement.lang || '') || /^en/i.test(String(navigator.language || ''));
}

function presetLabel(preset) {
  return isEnglishLocale() ? preset.en : preset.zh;
}

/** 刚体显示名：英文界面优先 labelEn；都没有时按序号给本地化兜底。 */
function sandboxBodyLabel(body, index) {
  if (!body) return '';
  const label = isEnglishLocale() && body.labelEn ? body.labelEn : body.label;
  return label || ossText('study.oss.sandbox.bodyFallback', '物体 {n}', { n: index + 1 });
}

/** 预设场景（设计坐标 640×380；渲染时按容器缩放）。 */
export const SANDBOX_PRESETS = Object.freeze([
  {
    id: 'spring',
    zh: '弹簧振子',
    en: 'Spring oscillator',
    scene: {
      gravity: { x: 0, y: 1 },
      bodies: [
        { id: 'anchor', shape: 'rect', x: 320, y: 42, width: 16, height: 16, static: true, label: '固定点', labelEn: 'Anchor' },
        { id: 'bob', shape: 'circle', x: 320, y: 190, radius: 24, restitution: 0.15, friction: 0.08, label: '振子', labelEn: 'Bob' },
      ],
      constraints: [
        { bodyA: 0, bodyB: 1, stiffness: 0.02, length: 120, damping: 0.02 },
      ],
    },
  },
  {
    id: 'incline',
    zh: '斜面滑块',
    en: 'Inclined plane',
    scene: {
      gravity: { x: 0, y: 1 },
      bodies: [
        { id: 'ramp', shape: 'slope', x: 330, y: 285, width: 430, height: 18, angle: -18, static: true, label: '斜面', labelEn: 'Inclined plane' },
        { id: 'block', shape: 'rect', x: 230, y: 170, width: 42, height: 30, restitution: 0.1, friction: 0.22, label: '滑块', labelEn: 'Block' },
      ],
      constraints: [],
    },
  },
  {
    id: 'collision',
    zh: '弹性碰撞',
    en: 'Elastic collisions',
    scene: {
      gravity: { x: 0, y: 0 },
      bodies: [
        { id: 'a', shape: 'circle', x: 175, y: 190, radius: 26, vx: 2.8, vy: 0, restitution: 0.98, friction: 0, label: '小球 A', labelEn: 'Ball A' },
        { id: 'b', shape: 'circle', x: 465, y: 190, radius: 26, vx: -1.6, vy: 0, restitution: 0.98, friction: 0, label: '小球 B', labelEn: 'Ball B' },
        { id: 'c', shape: 'circle', x: 320, y: 130, radius: 20, vx: 0, vy: 0, restitution: 0.9, friction: 0, label: '小球 C', labelEn: 'Ball C' },
      ],
      constraints: [],
    },
  },
]);

function presetScene(id) {
  const preset = SANDBOX_PRESETS.find(item => item.id === id) || SANDBOX_PRESETS[0];
  return JSON.parse(JSON.stringify(preset.scene));
}

function normalizeBody(raw, index) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const shape = String(raw.shape || '').toLowerCase();
  if (!SANDBOX_SHAPES.includes(shape)) return null;
  const body = {
    id: String(raw.id || `body-${index + 1}`).slice(0, 40),
    shape,
    x: clamp(finite(raw.x, DESIGN_WIDTH / 2), -1000, 1000),
    y: clamp(finite(raw.y, DESIGN_HEIGHT / 2), -1000, 1000),
    width: clamp(finite(raw.width, shape === 'circle' ? 36 : 60), 4, 800),
    height: clamp(finite(raw.height, shape === 'circle' ? 36 : 24), 4, 800),
    radius: clamp(finite(raw.radius, 20), 3, 400),
    angle: clamp(finite(raw.angle, 0), -180, 180),
    static: raw.static === true,
    restitution: clamp(finite(raw.restitution, 0.15), 0, 1),
    friction: clamp(finite(raw.friction, 0.2), 0, 1),
    vx: clamp(finite(raw.vx, 0), -50, 50),
    vy: clamp(finite(raw.vy, 0), -50, 50),
    label: String(raw.label || raw.id || `物体 ${index + 1}`).slice(0, 40),
    labelEn: String(raw.labelEn || '').slice(0, 40),
  };
  return body;
}

/** 校验场景；未知形状/超限/约束引用非法返回 null，数值字段非法回退默认值并收敛到安全范围。 */
export function normalizeSandboxScene(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const bodiesRaw = raw.bodies;
  if (!Array.isArray(bodiesRaw) || !bodiesRaw.length || bodiesRaw.length > MAX_BODIES) return null;
  const bodies = bodiesRaw.map(normalizeBody);
  if (bodies.some(item => item === null)) return null;
  const constraintsRaw = raw.constraints === undefined || raw.constraints === null
    ? [] : raw.constraints;
  if (!Array.isArray(constraintsRaw) || constraintsRaw.length > MAX_CONSTRAINTS) return null;
  const constraints = [];
  for (const item of constraintsRaw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    const bodyA = Number(item.bodyA);
    const bodyB = Number(item.bodyB);
    if (!Number.isInteger(bodyA) || !Number.isInteger(bodyB)) return null;
    if (bodyA < 0 || bodyA >= bodies.length || bodyB < 0 || bodyB >= bodies.length) return null;
    if (bodyA === bodyB) return null;
    constraints.push({
      bodyA,
      bodyB,
      stiffness: clamp(finite(item.stiffness, 0.08), 0.001, 1),
      length: clamp(finite(item.length, 80), 0, 800),
      damping: clamp(finite(item.damping, 0.02), 0, 1),
    });
  }
  const gravityRaw = raw.gravity && typeof raw.gravity === 'object' ? raw.gravity : {};
  return {
    gravity: {
      x: clamp(finite(gravityRaw.x, 0), -20, 20),
      y: clamp(finite(gravityRaw.y, 1), -20, 20),
    },
    bodies,
    constraints,
  };
}

/** 首次调用时注入 Matter.js（UMD 全局 Matter）；失败可重试。 */
export function loadMatter() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('study.oss.sandbox.unavailable', '当前环境不支持物理沙盒')));
  }
  const existing = window.Matter || window.matter;
  if (existing && typeof existing.Engine === 'object') return Promise.resolve(existing);
  if (loaderPromise) return loaderPromise;
  loaderPromise = new Promise((resolve, reject) => {
    let script = document.querySelector(`script[data-oss="${SCRIPT_MARK}"]`);
    const finish = () => {
      const library = window.Matter || window.matter;
      if (library && typeof library.Engine === 'object') {
        resolve(library);
        return;
      }
      script.remove();
      reject(new Error(ossText('study.oss.sandbox.unavailable', '物理沙盒加载失败')));
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
      script.remove();
      reject(new Error(ossText('study.oss.sandbox.unavailable', '物理沙盒加载超时')));
    }, LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      finish();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('study.oss.sandbox.unavailable', '物理沙盒加载失败')));
    }, { once: true });
  }).catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

function bodyOptions(body, index) {
  const fill = body.static ? '#8a9ba3' : body.shape === 'circle' ? '#2f6fed' : body.shape === 'slope' ? '#6b8f7f' : '#c98a3d';
  return {
    isStatic: body.static,
    restitution: body.restitution,
    friction: body.friction,
    frictionAir: 0.002,
    angle: (body.angle * Math.PI) / 180,
    render: {
      fillStyle: fill,
      lineWidth: 1,
      strokeStyle: '#ffffff',
      opacity: 0.95,
    },
    label: sandboxBodyLabel(body, index),
  };
}

function buildBodies(library, scene) {
  const { Bodies, Body } = library;
  const bodies = scene.bodies.map((item, index) => {
    const options = bodyOptions(item, index);
    let body;
    if (item.shape === 'circle') {
      body = Bodies.circle(item.x, item.y, Math.max(3, item.radius), options, 32);
    } else {
      body = Bodies.rectangle(item.x, item.y, item.width, item.height, options);
    }
    if (item.vx || item.vy) {
      Body.setVelocity(body, { x: item.vx, y: item.vy });
    }
    body.__sandboxIndex = item.id;
    return body;
  });
  return bodies;
}

function buildWalls(library) {
  const { Bodies } = library;
  const thickness = 24;
  return [
    Bodies.rectangle(DESIGN_WIDTH / 2, DESIGN_HEIGHT + thickness / 2 - 1,
      DESIGN_WIDTH + thickness * 2, thickness, {
        isStatic: true, render: { fillStyle: '#eef3f5', lineWidth: 0 },
      }),
    Bodies.rectangle(-thickness / 2 + 1, DESIGN_HEIGHT / 2,
      thickness, DESIGN_HEIGHT + thickness * 2, {
        isStatic: true, render: { fillStyle: '#eef3f5', lineWidth: 0 },
      }),
    Bodies.rectangle(DESIGN_WIDTH + thickness / 2 - 1, DESIGN_HEIGHT / 2,
      thickness, DESIGN_HEIGHT + thickness * 2, {
        isStatic: true, render: { fillStyle: '#eef3f5', lineWidth: 0 },
      }),
  ];
}

function buildConstraints(library, scene, bodies) {
  const { Constraint } = library;
  return scene.constraints.map(item => Constraint.create({
    bodyA: bodies[item.bodyA],
    bodyB: bodies[item.bodyB],
    stiffness: item.stiffness,
    length: item.length,
    damping: item.damping,
    render: { strokeStyle: '#956a32', lineWidth: 1.5 },
  }));
}

/**
 * 在容器内创建物理沙盒；返回 `{element, ready, getScene, pngURI, pause, resume, destroy}`。
 * 只读模式不挂鼠标约束，但保留播放/暂停/重置。
 */
export function createPhysicsSandbox(container, rawScene, options = {}) {
  const scene = normalizeSandboxScene(rawScene);
  const host = el('div', { class: 'oss-sandbox-view' });
  let destroyed = false;
  let library = null;
  let engine = null;
  let render = null;
  let runner = null;
  let interval = null;
  let pausedIntent = false;
  let selectedIndex = 0;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  /** 停掉 Runner/Render/Engine 并移除画布；destroy 与加载失败路径共用。 */
  function stopEngine() {
    if (interval) { clearInterval(interval); interval = null; }
    try { if (runner) library.Runner.stop(runner); } catch { /* 忽略 */ }
    try { if (render) library.Render.stop(render); } catch { /* 忽略 */ }
    try { if (render && render.canvas) render.canvas.remove(); } catch { /* 忽略 */ }
    try { if (engine) library.Engine.clear(engine); } catch { /* 忽略 */ }
  }

  /** 中文降级提示；原始错误只进 console.warn，不进界面。 */
  function fail(message, detail) {
    if (detail) console.warn('[physics-sandbox]', detail);
    host.replaceChildren(el('p', { class: 'help oss-sandbox-unavailable', role: 'status' }, message));
    resolveReady({ ok: false, reason: 'failed' });
  }

  if (!scene) {
    host.append(el('p', { class: 'help oss-sandbox-unavailable', role: 'status' },
      ossText('study.oss.sandbox.invalid', '场景数据不合法：请检查刚体数量、形状与约束。')));
    if (container) container.replaceChildren(host);
    resolveReady({ ok: false, reason: 'invalid' });
    return {
      element: host,
      ready,
      getScene: () => null,
      pngURI: () => '',
      pause: () => {},
      resume: () => {},
      destroy: () => {
        host.remove();
        if (container) container.replaceChildren();
        resolveReady({ ok: false, reason: 'destroyed' });
      },
    };
  }

  if (container && typeof container.replaceChildren === 'function') container.replaceChildren(host);
  host.append(el('p', { class: 'help oss-sandbox-loading' }, ossText('study.oss.sandbox.loading', '正在加载物理沙盒…')));

  (async () => {
    try {
      library = await loadMatter();
      if (destroyed) return;
      const { Engine, Render, Runner, Composite, Events, Mouse, MouseConstraint } = library;
      const width = clamp(container?.clientWidth || DESIGN_WIDTH, 320, 900);
      const height = Math.round(width * (DESIGN_HEIGHT / DESIGN_WIDTH));
      host.style.width = `${width}px`;
      host.style.height = `${height}px`;
      // 模拟世界固定为 640×380 设计坐标：视口只影响画布尺寸，不影响物理过程。
      engine = Engine.create({ enableSleeping: true });
      engine.gravity.x = scene.gravity.x;
      engine.gravity.y = scene.gravity.y;
      render = Render.create({
        element: host,
        engine,
        options: {
          width,
          height,
          wireframes: false,
          background: '#ffffff',
          pixelRatio: Math.min(2, window.devicePixelRatio || 1),
          hasBounds: true,
        },
      });
      if (!render.context) {
        // Canvas 2D 不可用：同步拒绝，避免 Render.run 后 rAF 每帧抛未捕获异常。
        throw new Error(ossText('study.oss.sandbox.unavailable',
          '物理沙盒不可用（Matter.js 加载失败或浏览器不支持 Canvas 2D）。'));
      }
      Render.lookAt(render, {
        min: { x: 0, y: 0 },
        max: { x: DESIGN_WIDTH, y: DESIGN_HEIGHT },
      });
      const bodies = buildBodies(library, scene);
      const constraints = buildConstraints(library, scene, bodies);
      Composite.add(engine.world, [...buildWalls(library), ...bodies, ...constraints]);
      Render.run(render);
      runner = Runner.create();
      Runner.run(runner, engine);
      if (pausedIntent) RunnerStop();
      if (!options.readOnly) {
        const mouse = Mouse.create(render.canvas);
        // hasBounds 视口换算：世界 = 画布像素 × scale + offset（设计坐标 → 画布的逆变换）。
        Mouse.setScale(mouse, {
          x: (render.bounds.max.x - render.bounds.min.x) / width,
          y: (render.bounds.max.y - render.bounds.min.y) / height,
        });
        Mouse.setOffset(mouse, {
          x: render.bounds.min.x,
          y: render.bounds.min.y,
        });
        const mouseConstraint = MouseConstraint.create(engine, {
          mouse,
          constraint: { stiffness: 0.18, render: { visible: false } },
        });
        Composite.add(engine.world, mouseConstraint);
        render.mouse = mouse;
      }
      const removeLoading = () => {
        const loading = host.querySelector('.oss-sandbox-loading');
        if (loading) loading.remove();
      };
      const onFirstFrame = () => {
        removeLoading();
        Events.off(render, 'afterRender', onFirstFrame);
        resolveReady({ ok: true });
      };
      Events.on(render, 'afterRender', onFirstFrame);
      // afterRender 在部分浏览器首帧前不触发时兜底。
      setTimeout(() => { if (!destroyed) { removeLoading(); resolveReady({ ok: true }); } }, 120);
      interval = setInterval(() => {
        if (destroyed) return;
        // 页面路由切换后容器被移除：自动停掉 Runner/Render，避免后台空转。
        if (!host.isConnected) {
          destroy();
          return;
        }
        const dynamic = engine.world.bodies.filter(body => !body.isStatic && body.__sandboxIndex);
        if (!dynamic.length) return;
        if (selectedIndex >= dynamic.length) selectedIndex = 0;
      }, 500);
    } catch (error) {
      stopEngine();
      if (!destroyed) {
        fail(ossText('study.oss.sandbox.unavailable',
          '物理沙盒不可用（Matter.js 加载失败或浏览器不支持 Canvas 2D）。'), error);
      }
    }
  })();

  function dynamicBodies() {
    return engine ? engine.world.bodies.filter(body => !body.isStatic && body.__sandboxIndex) : [];
  }

  return {
    element: host,
    ready,
    getScene: () => JSON.parse(JSON.stringify(scene)),
    readout() {
      const body = dynamicBodies()[selectedIndex];
      if (!body) return null;
      // 世界即 640×380 设计坐标；速度按 Matter 默认 60 步/秒换算为设计单位/秒。
      return {
        label: body.label || '',
        x: Number(body.position.x.toFixed(1)),
        y: Number(body.position.y.toFixed(1)),
        speed: Number((Math.hypot(body.velocity.x, body.velocity.y) * 60).toFixed(2)),
      };
    },
    selectBody(index) {
      const dynamic = dynamicBodies();
      if (!dynamic.length) return null;
      selectedIndex = clamp(Number(index) || 0, 0, dynamic.length - 1);
      return this.readout();
    },
    pause() { pausedIntent = true; if (runner) RunnerStop(); },
    resume() { pausedIntent = false; if (runner) RunnerStart(); },
    pngURI() {
      try {
        return render && render.canvas ? render.canvas.toDataURL('image/png') : '';
      } catch {
        return '';
      }
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stopEngine();
      host.remove();
      if (container && typeof container.replaceChildren === 'function') container.replaceChildren();
      resolveReady({ ok: false, reason: 'destroyed' });
    },
    get library() { return library; },
    get engine() { return engine; },
    get render() { return render; },
    get runner() { return runner; },
    get bodies() { return dynamicBodies(); },
  };

  function RunnerStart() {
    try { library.Runner.run(runner, engine); } catch { /* 已在运行 */ }
  }

  function RunnerStop() {
    try { library.Runner.stop(runner); } catch { /* 已停止 */ }
  }
}

function buildPreview(controller) {
  const png = controller && typeof controller.pngURI === 'function' ? controller.pngURI() : '';
  return {
    kind: 'physics-sandbox',
    ...controller.getScene(),
    ...(png && png.length < 60000 && png.startsWith('data:image/png;base64,') ? { png } : {}),
  };
}

/**
 * 打开物理自由沙盒弹层。
 * @param {{initialPreset?: string, links?: Array<object>, ref?: string, titleHint?: string,
 *          onSaved?: Function, trigger?: HTMLElement, readOnly?: boolean}} options
 */
export function openPhysicsSandbox(options = {}) {
  const readOnly = options.readOnly === true;
  const dialog = el('dialog', {
    class: 'oss-dialog oss-sandbox-dialog',
    'data-study-tool': 'physics-sandbox',
    'aria-label': ossText('study.oss.sandbox.title', '物理自由沙盒'),
  });
  const titleNode = el('h2', { class: 'oss-dialog-title' }, ossText('study.oss.sandbox.title', '物理自由沙盒'));
  const presetSelect = el('select', { class: 'input', 'aria-label': ossText('study.oss.sandbox.preset', '预设场景') },
    ...SANDBOX_PRESETS.map(preset => el('option', { value: preset.id }, presetLabel(preset))));
  const bodySelect = el('select', { class: 'input', 'aria-label': ossText('study.oss.sandbox.body', '观察对象') });
  const readout = el('p', { class: 'help oss-sandbox-readout', 'aria-live': 'polite' },
    ossText('study.oss.sandbox.readoutIdle', '选择物体后显示速度与位置。'));
  const canvasHost = el('div', { class: 'oss-sandbox-canvas' });
  const notice = el('p', { class: 'help' }, ossText(
    'study.oss.sandbox.determinism',
    '探索模式：Matter.js 的逐步积分不保证跨运行逐位一致，每次运行结果可能有细微差异；需要回放对账的演示请使用确定性物理演示。',
  ));
  let controller = null;
  let readoutTimer = 0;
  let paused = false;

  const presetId = SANDBOX_PRESETS.some(item => item.id === options.initialPreset)
    ? options.initialPreset : SANDBOX_PRESETS[0].id;
  presetSelect.value = presetId;

  function populateBodies() {
    bodySelect.replaceChildren();
    const bodies = controller ? controller.bodies : [];
    bodies.forEach((body, index) => {
      bodySelect.append(el('option', { value: String(index) },
        body.label || ossText('study.oss.sandbox.bodyFallback', '物体 {n}', { n: index + 1 })));
    });
    bodySelect.disabled = !bodies.length;
    if (bodies.length) {
      bodySelect.value = '0';
      controller.selectBody(0);
    }
    updateReadout();
  }

  function updateReadout() {
    if (!controller) return;
    const value = controller.readout();
    if (!value) {
      readout.textContent = ossText('study.oss.sandbox.readoutIdle',
        '选择物体后显示位置与速度（640×380 设计坐标，速度为设计单位/秒）。');
      return;
    }
    readout.textContent = ossText(
      'study.oss.sandbox.readout',
      '{label}：位置 ({x}, {y}) · 速度 {speed}',
      value,
    );
  }

  function startReadout() {
    clearInterval(readoutTimer);
    // 1 秒一拍：aria-live 读数不能以 250ms 频率刷屏。
    readoutTimer = setInterval(updateReadout, 1000);
  }

  function rebuild() {
    if (controller) {
      try { controller.destroy(); } catch { /* 忽略 */ }
      controller = null;
    }
    clearInterval(readoutTimer);
    const scene = presetScene(presetSelect.value);
    const current = createPhysicsSandbox(canvasHost, scene, { readOnly });
    controller = current;
    paused = false;
    playButton.textContent = ossText('study.oss.sandbox.pause', '暂停');
    current.ready.then(outcome => {
      // 已被重置/关闭取代，或场景失败（降级文案已在 host 内）：不写读数。
      if (controller !== current || !outcome || outcome.ok === false) return;
      populateBodies();
      startReadout();
    }).catch(() => {});
  }

  presetSelect.addEventListener('change', rebuild);
  bodySelect.addEventListener('change', () => {
    if (controller) { controller.selectBody(Number(bodySelect.value)); updateReadout(); }
  });

  const playButton = button(ossText('study.oss.sandbox.pause', '暂停'), () => {
    if (!controller) return;
    paused = !paused;
    if (paused) controller.pause(); else controller.resume();
    playButton.textContent = paused
      ? ossText('study.oss.sandbox.resume', '继续')
      : ossText('study.oss.sandbox.pause', '暂停');
  }, 'secondary');
  const resetButton = button(ossText('study.oss.sandbox.reset', '重置'), () => rebuild(), 'secondary');
  const closeButton = button(ossText('study.oss.sandbox.close', '关闭'), () => dialog.close(), 'secondary');
  const saveButton = button(ossText('study.oss.sandbox.save', '保存为学习附件'), event => busy(event.currentTarget, () => save()), '', 'download');

  async function save() {
    if (!controller) {
      toast(ossText('study.oss.sandbox.invalid', '场景数据不合法'), true);
      return;
    }
    const scene = controller.getScene();
    if (!normalizeSandboxScene(scene)) {
      toast(ossText('study.oss.sandbox.invalid', '场景数据不合法'), true);
      return;
    }
    const preset = SANDBOX_PRESETS.find(item => item.id === presetSelect.value);
    const links = Array.isArray(options.links) ? options.links : [];
    const ref = String(options.ref || '').trim();
    const fallbackSuffix = preset ? presetLabel(preset) : ossText('study.oss.sandbox.custom', '自定义');
    const body = {
      kind: SANDBOX_KIND,
      title: (String(options.titleHint || '').trim()
        || `${ossText('study.oss.sandbox.title', '物理自由沙盒')} · ${fallbackSuffix}`).slice(0, 120),
      data: scene,
      preview: buildPreview(controller),
      links,
    };
    if (ref) { body.source_kind = 'question'; body.source_ref = ref; }
    try {
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      toast(ossText('study.oss.sandbox.saved', '已保存为学习附件：{id}', { id: artifact.artifact_id || '' }));
      if (typeof options.onSaved === 'function') {
        try { options.onSaved({ artifact, created }); } catch { /* 忽略回调失败 */ }
      }
      dialog.close();
    } catch (error) {
      toast(error?.message || ossText('study.oss.sandbox.saveFailed', '保存物理沙盒失败'), true);
    }
  }

  dialog.append(
    titleNode,
    el('div', { class: 'oss-sandbox-toolbar' },
      el('label', { class: 'oss-sandbox-field' }, el('span', {}, ossText('study.oss.sandbox.preset', '预设场景')), presetSelect),
      el('label', { class: 'oss-sandbox-field' }, el('span', {}, ossText('study.oss.sandbox.body', '观察对象')), bodySelect)),
    canvasHost,
    readout,
    notice,
    el('div', { class: 'oss-sandbox-controls' }, playButton, resetButton),
    el('div', { class: 'oss-formula-toolbar' }, closeButton, saveButton),
  );
  if (readOnly) {
    presetSelect.disabled = true;
    saveButton.hidden = true;
  }
  dialog.addEventListener('close', () => {
    clearInterval(readoutTimer);
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
