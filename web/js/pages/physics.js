/**
 * 物理演示页：10 个确定性物理动画教学案例（physics-in-motion，MIT）。
 * 物理计算由 vendored 的 physics-core.js 在前端即时完成；渲染/播放为原生实现。
 */
import { el, badge, button, heading, notice, empty, loading } from '../core/dom.js';
import { go, current } from '../core/router.js';
import { t } from '../core/i18n.js';
import { renderPhysicsDemo } from '../components/physicsPlayer.js';

const CORE_URL = '/static/vendor/physics-in-motion/physics-core.js';
const DEMOS_URL = '/static/vendor/physics-in-motion/demos.js';

let assetsPromise = null;
let activePlayer = null;
let activeSandbox = null;

function loadAssets() {
  if (!assetsPromise) {
    assetsPromise = Promise.all([import(CORE_URL), import(DEMOS_URL)])
      .then(([core, demoModule]) => ({
        buildSim: core.buildSim,
        demos: demoModule.DEMOS || demoModule.default || [],
      }))
      .catch((error) => {
        assetsPromise = null;
        throw error;
      });
  }
  return assetsPromise;
}

// 难度标签用 getter 方式惰性取文案，避免模块加载时固定首屏语言。
const DIFFICULTY_KEYS = {
  1: 'physics.difficultyBasic',
  2: 'physics.difficultyIntermediate',
  3: 'physics.difficultyComprehensive',
};

function difficultyLabel(value) {
  const key = DIFFICULTY_KEYS[value];
  return key ? t(key) : t('physicsPage.difficulty', { v1: value });
}

export async function renderPhysics(container, version, params) {
  if (activePlayer) {
    try { activePlayer.destroy(); } catch { /* 旧播放器可能已随页面移除 */ }
    activePlayer = null;
  }
  if (activeSandbox) {
    try { activeSandbox.destroy(); } catch { /* 旧沙盒可能已随页面移除 */ }
    activeSandbox = null;
  }
  if (String(params?.get('sandbox') || '') === '1') {
    await renderSandboxPage(container, version);
    return;
  }
  container.replaceChildren(loading(t('physicsPage.loadingPhysicsDemos')));
  let assets;
  try {
    assets = await loadAssets();
  } catch (error) {
    if (!current(version)) return;
    container.replaceChildren(
      heading('PHYSICS IN MOTION', t('nav.physics'), t('physicsPage.deterministicPhysicsAnimationsToHelpYouUnderstand')),
      notice(t('physicsPage.failedToLoadPhysicsDemoAssets', { v1: error.message }), 'error'),
    );
    return;
  }
  if (!current(version)) return;
  if (!assets.demos.length) {
    container.replaceChildren(
      heading('PHYSICS IN MOTION', t('nav.physics'), t('physicsPage.deterministicPhysicsAnimationsToHelpYouUnderstand')),
      empty(t('physicsPage.noDemosAvailable'), t('physicsPage.checkWhetherWebVendorPhysicsInMotion'), null, 'atom'),
    );
    return;
  }

  const demoId = params?.get('demo') || '';
  const selected = assets.demos.find((demo) => demo.id === demoId);
  if (selected) {
    renderDemo(container, assets, selected);
    return;
  }
  renderList(container, assets);
}

function sandboxPresetLabel(preset) {
  const english = /^en/i.test(document.documentElement.lang || '')
    || /^en/i.test(String(navigator.language || ''));
  return english ? preset.en : preset.zh;
}

function physicsTabs(active) {
  const tab = key => el('button', {
    type: 'button',
    class: 'pg-tab',
    role: 'tab',
    'aria-selected': active === key ? 'true' : 'false',
    onClick: key === 'list' ? () => go('physics') : () => go('physics', null, { sandbox: '1' }),
  }, key === 'list' ? t('physicsPage.deterministicDemos') : t('study.oss.sandbox.tab'));
  return el('div', { class: 'pg-tabs pg-phys-tabs', role: 'tablist' }, tab('list'), tab('sandbox'));
}

function renderList(container, assets) {
  const cards = assets.demos.map((demo) => el('button', {
    type: 'button',
    class: 'pg-phys-card',
    onClick: () => go('physics', null, { demo: demo.id }),
  },
  el('div', { class: 'pg-phys-card-head' },
    el('strong', {}, demo.title),
    demo.difficulty ? badge(difficultyLabel(demo.difficulty)) : null),
  el('p', { class: 'pg-phys-card-question' }, (demo.question || '').slice(0, 96)
    + ((demo.question || '').length > 96 ? '…' : '')),
  el('div', { class: 'pg-phys-card-tags' },
    ...(demo.modules || []).slice(0, 4).map((module) => badge(module, 'teal')))));

  container.replaceChildren(
    heading('PHYSICS IN MOTION', t('nav.physics'),
      t('physicsPage.deterministicPhysicsAnimationsHelpYouFollowA')),
    physicsTabs('list'),
    notice(t('physicsPage.animationsAreAidsForUnderstandingPhysicalRelations'), ''),
    el('div', { class: 'pg-phys-list' }, ...cards),
    el('p', { class: 'footer-note' },
      t('physicsPage.animationAndSolverComeFromPhysicsIn')
      + t('physicsPage.thisProjectRewritesItsPhysicsCoreAs'),
      el('br'),
      t('physicsPage.seeThirdPartyNoticesMdInThe')),
  );
}

function renderDemo(container, assets, demo) {
  const host = el('div');
  container.replaceChildren(
    el('div', { class: 'pg-phys-demo-head' },
      button(t('physicsPage.backToDemoList'), () => go('physics'), 'secondary small', 'chevron'),
      el('div', {},
        el('div', { class: 'eyebrow' }, 'PHYSICS IN MOTION'),
        el('h1', {}, demo.title),
        el('div', { class: 'pg-phys-demo-meta' },
          ...(demo.modules || []).map((module) => badge(module, 'teal')),
          demo.difficulty ? badge(difficultyLabel(demo.difficulty)) : null,
          badge(t('physicsPage.deterministicPhysicsCore'), 'blue')))),
    host,
    el('p', { class: 'footer-note' },
      t('physicsPage.animationParameters', { v1: JSON.stringify(demo.visualization?.params || {}) }),
      el('br'),
      t('physicsPage.animationAndSolverComeFromPhysicsIn_2')),
  );
  activePlayer = renderPhysicsDemo(host, demo, assets.buildSim);
}


async function renderSandboxPage(container, version) {
  container.replaceChildren(loading(t('study.oss.sandbox.loading')));
  let module;
  try {
    module = await import('../components/physicsSandbox.js');
  } catch (error) {
    if (!current(version)) return;
    container.replaceChildren(
      heading('PHYSICS SANDBOX', t('study.oss.sandbox.title'), t('study.oss.sandbox.subtitle')),
      physicsTabs('sandbox'),
      notice(error?.message || t('study.oss.sandbox.unavailable'), 'error'),
    );
    return;
  }
  if (!current(version)) return;
  const presetSelect = el('select', { class: 'input pg-phys-sandbox-preset' },
    ...module.SANDBOX_PRESETS.map(preset => el('option', { value: preset.id }, sandboxPresetLabel(preset))));
  const bodySelect = el('select', { class: 'input pg-phys-sandbox-body' });
  const host = el('div', { class: 'pg-phys-sandbox-host' });
  const readout = el('p', { class: 'help', 'aria-live': 'polite' }, t('study.oss.sandbox.readoutIdle'));
  const status = el('p', { class: 'help', role: 'status' }, '');
  let controller = null;
  let readoutTimer = 0;
  let paused = false;
  const pauseBtn = button(t('study.oss.sandbox.pause'), () => {
    if (!controller) return;
    paused = !paused;
    if (paused) controller.pause(); else controller.resume();
    pauseBtn.textContent = paused ? t('study.oss.sandbox.resume') : t('study.oss.sandbox.pause');
  }, 'secondary small');
  const resetBtn = button(t('study.oss.sandbox.reset'), () => mount(), 'secondary small');
  const saveBtn = button(t('study.oss.sandbox.save'), () => {
    module.openPhysicsSandbox({ initialPreset: presetSelect.value });
  }, 'small');
  let selected = 0;

  function readoutText() {
    const value = controller && controller.readout ? controller.readout() : null;
    if (!value) return t('study.oss.sandbox.readoutIdle');
    return t('study.oss.sandbox.readout', value);
  }

  function populateBodies() {
    const bodies = controller ? controller.bodies : [];
    bodySelect.replaceChildren(...bodies.map((body, index) => el('option', { value: String(index) },
      body.label || t('study.oss.sandbox.bodyFallback', { n: index + 1 }))));
    bodySelect.disabled = !bodies.length;
    if (bodies.length) { bodySelect.value = '0'; controller.selectBody(0); }
    readout.textContent = readoutText();
  }

  function mount() {
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
    clearInterval(readoutTimer);
    const preset = module.SANDBOX_PRESETS.find(item => item.id === presetSelect.value) || module.SANDBOX_PRESETS[0];
    const current = module.createPhysicsSandbox(host, preset.scene, { readOnly: false });
    controller = current;
    activeSandbox = controller;
    paused = false;
    pauseBtn.textContent = t('study.oss.sandbox.pause');
    current.ready.then(outcome => {
      // 被重置/切页取代，或失败（降级文案已在 host 内）：不动状态区。
      if (controller !== current || !outcome || outcome.ok === false) return;
      status.textContent = '';
      populateBodies();
      readoutTimer = setInterval(() => {
        if (!host.isConnected) {
          clearInterval(readoutTimer);
          if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
          return;
        }
        readout.textContent = readoutText();
      }, 1000);
    }).catch(() => {});
  }

  presetSelect.addEventListener('change', mount);
  bodySelect.addEventListener('change', () => {
    if (!controller) return;
    controller.selectBody(Number(bodySelect.value));
    readout.textContent = readoutText();
  });

  container.replaceChildren(
    heading('PHYSICS SANDBOX', t('study.oss.sandbox.title'), t('study.oss.sandbox.subtitle')),
    physicsTabs('sandbox'),
    notice(t('study.oss.sandbox.determinism'), 'warning'),
    el('div', { class: 'pg-phys-sandbox-controls' },
      el('label', {}, t('study.oss.sandbox.preset'), presetSelect),
      el('label', {}, t('study.oss.sandbox.body'), bodySelect),
      pauseBtn, resetBtn, saveBtn),
    host,
    readout,
    status,
  );
  mount();
}
