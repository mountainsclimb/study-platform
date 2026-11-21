/**
 * 最小 Three.js 视图（demo_3d）。
 *
 * 依赖策略（硬性约束）：
 * - three.js 只从仓库内本地文件 `web/vendor/interactive/three.module.min.js`
 *   动态 import，同一来源、不新增依赖、不访问 CDN；该文件是仓库既有的
 *   three.js r186 IIFE 构建（执行后暴露 globalThis.THREE），见同目录 README。
 * - 动态 import 失败或 WebGL 不可用时抛出 ThreeUnavailableError（中文 message），
 *   由 principleDemo 捕获并自动降级为等距示意图，绝不把异常抛到页面。
 *
 * 渲染循环为「按需渲染」：初始化、切换步骤、装配补间时用 requestAnimationFrame
 * 驱动有限时长动画，动画结束立即停止；不做无限循环。destroy() 释放
 * geometry / material / renderer，并主动 forceContextLoss。
 *
 * 数据契约：与 iso.js 相同的 parts（x/y/z + w/h/d + color + explode）与
 * steps（{label, detail?, part_ids}）；步骤表示装配顺序。
 */

import { normalizeIso } from './iso.js';

/** three.js 本地路径：相对本文件 ../../vendor/interactive/three.module.min.js。 */
const THREE_URL = new URL('../../vendor/interactive/three.module.min.js', import.meta.url).href;
const ASSEMBLY_EXPLODE_SCALE = 1.6;

/** 3D 不可用（无 WebGL / three.js 加载失败）时抛出的专用错误。 */
export class ThreeUnavailableError extends Error {
  constructor(message = '当前环境无法使用 3D 视图，已切换为等距示意图') {
    super(message);
    this.name = 'ThreeUnavailableError';
  }
}

let threePromise = null;

/** 检测 WebGL 是否可用（会立即释放探测用上下文，避免占用 GPU 上下文配额）。 */
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

/** 动态 import 本地 three.js；失败统一转成 ThreeUnavailableError。 */
async function loadThree() {
  const existing = globalThis.THREE;
  if (existing && typeof existing.WebGLRenderer === 'function') return existing;
  if (!threePromise) {
    threePromise = (async () => {
      let module = null;
      try {
        module = await import(THREE_URL);
      } catch {
        throw new ThreeUnavailableError('本地 three.js 加载失败，已切换为等距示意图');
      }
      // IIFE 构建没有 ESM 导出，执行后通过 globalThis.THREE 暴露；两种方式都兼容。
      const THREE = module?.THREE || globalThis.THREE;
      if (!THREE || typeof THREE.WebGLRenderer !== 'function') {
        throw new ThreeUnavailableError('本地 three.js 未正确导出，已切换为等距示意图');
      }
      return THREE;
    })().catch((error) => {
      threePromise = null;
      if (error instanceof ThreeUnavailableError) throw error;
      throw new ThreeUnavailableError('本地 three.js 加载失败，已切换为等距示意图');
    });
  }
  return threePromise;
}

function normalizeThreeData(data) {
  const model = normalizeIso(data);
  const stepInstall = new Map();
  model.steps.forEach((step, index) => {
    for (const id of step.part_ids) if (!stepInstall.has(id)) stepInstall.set(id, index);
  });
  const parts = model.parts.map((part) => ({
    ...part,
    installIndex: stepInstall.has(part.id) ? stepInstall.get(part.id) : -1,
  }));
  const steps = model.steps.length
    ? model.steps
    : [{ label: '', detail: '', part_ids: [] }];
  return { parts, steps };
}

function targetsForStep(model, stepIndex) {
  const factors = new Map();
  for (const part of model.parts) {
    if (part.installIndex < 0) factors.set(part.id, 0);
    else factors.set(part.id, part.installIndex <= stepIndex ? 0 : 1);
  }
  return factors;
}

function easeOutCubic(value) {
  const t = Math.max(0, Math.min(1, value));
  return 1 - (1 - t) ** 3;
}

/* ------------------------------------------------------------------------- *
 * 视图主体
 * ------------------------------------------------------------------------- */

/**
 * 在 host 内创建最小 Three.js 装配视图。
 * @param {HTMLElement} host
 * @param {object} data parts/steps 数据
 * @param {{ ariaLabel?: string, onContextLost?: Function }} [options]
 * @returns {Promise<{mode:'three', element:HTMLCanvasElement, getStepCount:()=>number,
 *   getStep:(i:number)=>object, setStep:(i:number, opts?:{animate?:boolean})=>void,
 *   supportsDirection:boolean, destroy:()=>void}>}
 * @throws {ThreeUnavailableError} 无 WebGL / three.js 加载失败 / 渲染器创建失败
 */
export async function createThreeView(host, data, options = {}) {
  if (!isWebGLAvailable()) {
    throw new ThreeUnavailableError('当前设备不支持 3D，已切换为等距示意图');
  }
  const THREE = await loadThree();
  const model = normalizeThreeData(data);

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch {
    throw new ThreeUnavailableError('当前环境无法创建 3D 渲染器，已切换为等距示意图');
  }

  // 场景、相机、灯光：纯色材质 + 环境光/平行光，保持最小资源占用。
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 16 / 9, 0.1, 800);
  const geometries = [];
  const materials = [];
  const meshes = new Map();
  const basePositions = new Map();
  const explodeOffsets = new Map();

  const ambient = new THREE.HemisphereLight(0xffffff, 0x33424a, 1.05);
  scene.add(ambient);
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.75);
  keyLight.position.set(14, 22, 16);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0xbcd4dc, 0.5);
  fillLight.position.set(-16, 8, -12);
  scene.add(fillLight);

  let bounds = new THREE.Box3();
  for (const part of model.parts) {
    const geometry = new THREE.BoxGeometry(part.w, part.h, part.d);
    geometries.push(geometry);
    const material = new THREE.MeshStandardMaterial({
      color: new THREE.Color(part.color),
      roughness: 0.68,
      metalness: 0.05,
      transparent: part.opacity < 1,
      opacity: part.opacity,
      depthWrite: part.opacity >= 1,
    });
    materials.push(material);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(part.x, part.y, part.z);
    mesh.userData.partId = part.id;
    mesh.userData.baseEmissive = new THREE.Color(0x000000);
    // 细描边让纯色方块在无贴图时也能看清棱线（6 个部件，资源开销可以忽略）。
    const edgesGeometry = new THREE.EdgesGeometry(geometry);
    geometries.push(edgesGeometry);
    const edgesMaterial = new THREE.LineBasicMaterial({
      color: 0x1f373f,
      transparent: true,
      opacity: 0.4,
    });
    materials.push(edgesMaterial);
    mesh.add(new THREE.LineSegments(edgesGeometry, edgesMaterial));
    scene.add(mesh);
    meshes.set(part.id, mesh);
    basePositions.set(part.id, { x: part.x, y: part.y, z: part.z });
    const offset = {
      x: part.explode[0] * ASSEMBLY_EXPLODE_SCALE,
      y: part.explode[1] * ASSEMBLY_EXPLODE_SCALE,
      z: part.explode[2] * ASSEMBLY_EXPLODE_SCALE,
    };
    explodeOffsets.set(part.id, offset);
    const box = new THREE.Box3().setFromCenterAndSize(
      new THREE.Vector3(part.x, part.y, part.z),
      new THREE.Vector3(part.w, part.h, part.d),
    );
    bounds.union(box);
    // 拆开后的包围盒也计入取景，避免部件飞出画面。
    bounds.union(box.clone().translate(new THREE.Vector3(offset.x, offset.y, offset.z)));
  }

  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  bounds.getCenter(center);
  bounds.getSize(size);
  const radius = Math.max(1, Math.hypot(size.x, size.y, size.z) / 2);
  const cameraDirection = new THREE.Vector3(0.85, 0.72, 1.15).normalize();
  camera.position.copy(center).addScaledVector(cameraDirection, radius * 2.5);
  camera.near = Math.max(0.1, radius * 0.02);
  camera.far = radius * 12;
  camera.lookAt(center);
  scene.add(new THREE.AmbientLight(0xffffff, 0.15));

  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setPixelRatio(Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1));
  const canvas = renderer.domElement;
  canvas.className = 'pd-three-canvas';
  canvas.setAttribute('aria-label', options.ariaLabel || '3D assembly diagram');
  canvas.setAttribute('role', 'img');
  host.replaceChildren(canvas);

  let stepIndex = 0;
  let factors = targetsForStep(model, 0);
  let activeParts = new Set(model.steps[0]?.part_ids || []);
  let tweenFrame = 0;
  let destroyed = false;
  let contextLost = false;

  const onContextLost = (event) => {
    event.preventDefault();
    contextLost = true;
    if (typeof options.onContextLost === 'function') options.onContextLost();
  };
  canvas.addEventListener('webglcontextlost', onContextLost, false);

  function paintFactors() {
    for (const part of model.parts) {
      const mesh = meshes.get(part.id);
      if (!mesh) continue;
      const factor = Math.max(0, Math.min(1, factors.get(part.id) || 0));
      const base = basePositions.get(part.id);
      const offset = explodeOffsets.get(part.id);
      mesh.position.set(
        base.x + offset.x * factor,
        base.y + offset.y * factor,
        base.z + offset.z * factor,
      );
      // 未安装的部件略微透明，凸显「待装配」状态；已安装的部件保持预设不透明度
      //（箱体/箱盖预设为半透明，便于看到内部轴与齿轮）。
      const baseOpacity = part.opacity;
      const opacity = factor > 0.001 ? Math.min(baseOpacity, 0.42) : baseOpacity;
      const transparent = opacity < 0.999;
      const material = mesh.material;
      material.transparent = transparent;
      material.opacity = opacity;
      material.depthWrite = !transparent;
      material.needsUpdate = true;
      // 当前步骤涉及的部件用自发光描边，和等距视图的 is-active 高亮语义一致。
      if (activeParts.has(part.id)) {
        material.emissive.setHex(0x1d5b4b);
        material.emissiveIntensity = 0.65;
      } else {
        material.emissive.setHex(0x000000);
        material.emissiveIntensity = 0;
      }
    }
  }

  function renderFrame() {
    if (destroyed || contextLost) return;
    renderer.render(scene, camera);
  }

  function resize() {
    if (destroyed) return;
    const width = Math.max(1, host.clientWidth || 640);
    const height = Math.max(1, host.clientHeight || 380);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderFrame();
  }

  const resizeObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => resize())
    : null;
  if (resizeObserver) resizeObserver.observe(host);
  window.addEventListener('resize', resize, { passive: true });

  function stopTween() {
    if (tweenFrame) {
      cancelAnimationFrame(tweenFrame);
      tweenFrame = 0;
    }
  }

  function animateTo(target, duration = 360, onRendered = null) {
    stopTween();
    const start = new Map(factors);
    const startTime = performance.now();
    const tick = (now) => {
      if (destroyed) return;
      const progress = easeOutCubic(duration > 0 ? (now - startTime) / duration : 1);
      for (const part of model.parts) {
        const from = start.get(part.id) || 0;
        const to = target.get(part.id) || 0;
        factors.set(part.id, from + (to - from) * progress);
      }
      paintFactors();
      renderFrame();
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

  function setStep(index, { animate = true, onRendered = null } = {}) {
    const count = model.steps.length;
    stepIndex = Math.max(0, Math.min(count - 1, Number(index) || 0));
    activeParts = new Set(model.steps[stepIndex]?.part_ids || []);
    const target = targetsForStep(model, stepIndex);
    if (animate) {
      animateTo(target, 360, onRendered);
    } else {
      stopTween();
      factors = new Map(target);
      paintFactors();
      renderFrame();
      if (typeof onRendered === 'function') onRendered();
    }
  }

  paintFactors();
  resize();
  setStep(0, { animate: false });

  return {
    mode: 'three',
    element: canvas,
    // 3D 步骤有 360ms 装配补间：补间最后一帧渲染完成后才回调 onRendered/onFrameReady。
    deferredFrame: true,
    getStepCount: () => model.steps.length,
    getStep: (index) => {
      const safe = Math.max(0, Math.min(model.steps.length - 1, Number(index) || 0));
      return model.steps[safe] || {};
    },
    supportsDirection: false,
    setStep,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stopTween();
      if (resizeObserver) resizeObserver.disconnect();
      window.removeEventListener('resize', resize);
      canvas.removeEventListener('webglcontextlost', onContextLost);
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      scene.clear();
      renderer.dispose();
      // 主动释放 WebGL 上下文，避免反复开关演示时耗尽浏览器上下文配额。
      try {
        renderer.forceContextLoss();
      } catch {
        /* 某些浏览器/无 GPU 环境下 forceContextLoss 可能不可用，忽略即可。 */
      }
      canvas.remove();
      host.replaceChildren();
    },
  };
}
