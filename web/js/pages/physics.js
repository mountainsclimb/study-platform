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
