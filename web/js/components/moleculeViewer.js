/* 3Dmol.js 分子查看（方案 §6.2）。
 *
 * 用法：
 *   const controller = createMoleculeView(container, {smiles:'O', label:'水', style:'stick'});
 *   await controller.ready;
 *   const png = controller.pngURI();
 *   controller.destroy();
 *
 * 或 openMoleculeViewer({links, ref, onSaved}) 打开可交互弹层。
 *
 * 边界：
 * * 3Dmol 主脚本随仓库分发在 /static/vendor/3dmol/，首次打开时才注入，同源脚本
 *   符合 script-src 'self'；不使用 eval / 外部 CDN。
 * * 只接受 SMILES 字符串作为模型数据，不拼 HTML；标题/标签一律 textContent。
 * * WebGL 不可用或脚本加载失败时中文降级，不伪造结构式。
 */
import { api } from '../core/api.js';
import { busy, button, el, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

const SCRIPT_URL = '/static/vendor/3dmol/3Dmol-min.js';
const OCL_URL = '/static/vendor/openchemlib/openchemlib.js';
const OCL_RESOURCES_URL = '/static/vendor/openchemlib/resources.json';
const SCRIPT_MARK = 'oss3DmolScript';
const LOAD_TIMEOUT_MS = 15000;
const MAX_ATOMS = 120;
export const MOLECULE_KIND = 'molecule_3d';
export const MAX_SMILES_LENGTH = 500;
export const MOLECULE_STYLES = ['stick', 'sphere', 'line'];
const PRESETS = [
  { id: 'water', smiles: 'O', zh: '水 H2O', en: 'Water H2O' },
  { id: 'carbon-dioxide', smiles: 'O=c=O', zh: '二氧化碳 CO2', en: 'Carbon dioxide CO2' },
  { id: 'methane', smiles: 'C', zh: '甲烷 CH4', en: 'Methane CH4' },
  { id: 'ethanol', smiles: 'CCO', zh: '乙醇 C2H5OH', en: 'Ethanol C2H5OH' },
  { id: 'benzene', smiles: 'c1ccccc1', zh: '苯 C6H6', en: 'Benzene C6H6' },
  { id: 'ammonia', smiles: 'N', zh: '氨 NH3', en: 'Ammonia NH3' },
];

let loaderPromise = null;
let oclPromise = null;

function ossText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function styleLabel(style) {
  const labels = {
    stick: ossText('study.oss.molecule.style.stick', '球棍模型'),
    sphere: ossText('study.oss.molecule.style.sphere', '空间填充'),
    line: ossText('study.oss.molecule.style.line', '线框'),
  };
  return labels[style] || labels.stick;
}

function presetLabel(preset) {
  const english = /^en/i.test(document.documentElement.lang || '') || /^en/i.test(String(navigator.language || ''));
  return english ? preset.en : preset.zh;
}

/** 规范分子附件数据；非法返回 null。 */
export function normalizeMoleculeDefinition(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const smiles = String(raw.smiles || '').trim();
  if (!smiles || smiles.length > MAX_SMILES_LENGTH) return null;
  if (/[\u0000-\u001f\u007f]/.test(smiles)) return null;
  const style = MOLECULE_STYLES.includes(String(raw.style || ''))
    ? String(raw.style) : 'stick';
  const label = String(raw.label || '').trim().slice(0, 80);
  return { smiles, label, style };
}

/** 首次调用时注入 3Dmol（UMD 全局 $3Dmol）；失败可重试。 */
export function load3Dmol() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('study.oss.molecule.unavailable', '当前环境不支持分子模型')));
  }
  const existing = window.$3Dmol || window['3Dmol'];
  if (existing && typeof existing.createViewer === 'function') return Promise.resolve(existing);
  if (loaderPromise) return loaderPromise;
  loaderPromise = new Promise((resolve, reject) => {
    let script = document.querySelector(`script[data-oss="${SCRIPT_MARK}"]`);
    const finish = () => {
      const library = window.$3Dmol || window['3Dmol'];
      if (library && typeof library.createViewer === 'function') {
        resolve(library);
        return;
      }
      // 脚本 200 但不是有效产物：移除后允许重试，本次回退中文提示。
      script.remove();
      reject(new Error(ossText('study.oss.molecule.unavailable', '分子模型加载失败')));
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
      reject(new Error(ossText('study.oss.molecule.unavailable', '分子模型加载超时')));
    }, LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      finish();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('study.oss.molecule.unavailable', '分子模型加载失败')));
    }, { once: true });
  }).catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

/** 首次调用时动态 import OpenChemLib 并注册 3D 构象所需资源；失败可重试。 */
export function loadOpenChemLib() {
  if (oclPromise) return oclPromise;
  oclPromise = (async () => {
    const module = await import(OCL_URL);
    if (!module || !module.Molecule || !module.ConformerGenerator || !module.Resources) {
      throw new Error(ossText('study.oss.molecule.unavailable', '分子解析器不可用'));
    }
    const response = await fetch(OCL_RESOURCES_URL, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(ossText('study.oss.molecule.unavailable', '分子资源加载失败'));
    const resources = await response.json();
    module.Resources.register(resources);
    return module;
  })().catch(error => {
    oclPromise = null;
    throw error;
  });
  return oclPromise;
}

function buildMolfile(ocl, definition) {
  const molecule = ocl.Molecule.fromSmiles(definition.smiles);
  if (!molecule || molecule.getAllAtoms() < 1) throw new Error('invalid smiles');
  if (molecule.getAllAtoms() > MAX_ATOMS) {
    throw new Error(ossText('study.oss.molecule.tooLarge', '分子原子数超过 {n}，暂不支持 3D 预览。', { n: MAX_ATOMS }));
  }
  const generator = new ocl.ConformerGenerator(1);
  if (!generator.getOneConformerAsMolecule(molecule)) throw new Error('conformer failed');
  if (molecule.getAllAtoms() > MAX_ATOMS) {
    throw new Error(ossText('study.oss.molecule.tooLarge', '分子原子数超过 {n}，暂不支持 3D 预览。', { n: MAX_ATOMS }));
  }
  return molecule.toMolfile();
}

function styleSpec(style) {
  if (style === 'sphere') return { sphere: { scale: 0.9, colorscheme: 'Jmol' } };
  if (style === 'line') return { line: { colorscheme: 'Jmol' } };
  return { stick: { radius: 0.15, colorscheme: 'Jmol' } };
}

/**
 * 在容器内创建分子视图；返回 `{element, ready, pngURI, destroy}`。
 * 组件内部会保留 WebGL 失败的中文提示，不抛到页面。
 */
export function createMoleculeView(container, rawDefinition, options = {}) {
  const definition = normalizeMoleculeDefinition(rawDefinition);
  const host = el('div', { class: 'oss-molecule-view' });
  let viewer = null;
  let destroyed = false;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (viewer) {
      try { viewer.clear(); } catch { /* 释放失败不影响页面 */ }
      viewer = null;
    }
    host.remove();
    if (container && typeof container.replaceChildren === 'function') container.replaceChildren();
    if (resolveReady) resolveReady({ ok: false, error: 'destroyed' });
  }

  function fail(message) {
    host.replaceChildren(el('p', { class: 'help oss-molecule-unavailable', role: 'status' }, message));
    resolveReady({ ok: false, error: message });
  }

  if (!definition) {
    host.append(el('p', { class: 'help oss-molecule-unavailable', role: 'status' },
      ossText('study.oss.molecule.invalid', '分子数据不合法：请检查 SMILES 是否为空或过长。')));
    if (container) container.replaceChildren(host);
    resolveReady({ ok: false, error: 'invalid' });
    return { element: host, ready, pngURI: () => '', destroy };
  }

  if (container && typeof container.replaceChildren === 'function') container.replaceChildren(host);
  host.append(el('p', { class: 'help oss-molecule-loading' }, ossText('study.oss.molecule.loading', '正在加载分子模型…')));

  (async () => {
    try {
      const [library, ocl] = await Promise.all([load3Dmol(), loadOpenChemLib()]);
      if (destroyed) return;
      const molfile = buildMolfile(ocl, definition);
      viewer = library.createViewer(host, { backgroundColor: 'white' });
      const model = viewer.addModel(molfile, 'sdf');
      const atomCount = model && typeof model.selectedAtoms === 'function'
        ? model.selectedAtoms({}).length : 0;
      if (!atomCount) throw new Error(ossText('study.oss.molecule.invalid', '分子数据不合法：请检查 SMILES 是否有效。'));
      viewer.setStyle({}, styleSpec(definition.style));
      viewer.zoomTo();
      viewer.render();
      const renderer = typeof viewer.getRenderer === 'function' ? viewer.getRenderer() : null;
      const gl = renderer && typeof renderer.getContext === 'function' ? renderer.getContext() : null;
      if (!gl) throw new Error(ossText('study.oss.molecule.unavailable', '当前浏览器无法渲染分子模型（需要 WebGL）。'));
      const loading = host.querySelector('.oss-molecule-loading');
      if (loading) loading.remove();
      try { viewer.resize(); viewer.render(); } catch { /* 尺寸异常只影响首帧 */ }
      resolveReady({ ok: true });
    } catch (error) {
      if (!destroyed) fail(error?.message || ossText(
        'study.oss.molecule.unavailable',
        '当前浏览器无法渲染分子模型（需要 WebGL）。',
      ));
    }
  })();

  return {
    element: host,
    ready,
    pngURI() {
      if (!viewer) return '';
      try {
        const value = viewer.pngURI();
        return typeof value === 'string' ? value : '';
      } catch {
        return '';
      }
    },
    destroy,
    get viewer() { return viewer; },
    get definition() { return definition; },
    readOnly: options.readOnly === true,
  };
}

function buildPreview(controller, definition) {
  const preview = {
    kind: 'molecule-3d',
    smiles: definition.smiles,
    style: definition.style,
  };
  const png = controller && typeof controller.pngURI === 'function' ? controller.pngURI() : '';
  if (png && png.length < 60000 && png.startsWith('data:image/png;base64,')) preview.png = png;
  return preview;
}

/**
 * 打开分子模型弹层。
 * @param {{initialSmiles?: string, links?: Array<object>, ref?: string, titleHint?: string,
 *          onSaved?: Function, trigger?: HTMLElement}} options
 */
export function openMoleculeViewer(options = {}) {
  const dialog = el('dialog', {
    class: 'oss-dialog oss-molecule-dialog',
    'data-study-tool': 'molecule-viewer',
    'aria-label': ossText('study.oss.molecule.title', '分子模型'),
  });
  const headingId = `oss-molecule-title-${Date.now()}`;
  const titleNode = el('h2', { id: headingId, class: 'oss-dialog-title' },
    ossText('study.oss.molecule.title', '分子模型'));
  const smileInput = el('input', {
    class: 'input',
    value: String(options.initialSmiles || 'O').slice(0, MAX_SMILES_LENGTH),
    maxlength: String(MAX_SMILES_LENGTH),
    spellcheck: 'false',
    autocomplete: 'off',
    'aria-label': ossText('study.oss.molecule.smiles', 'SMILES'),
    placeholder: ossText('study.oss.molecule.smilesPlaceholder', '例如：O、CCO、c1ccccc1'),
  });
  const labelInput = el('input', {
    class: 'input',
    value: String(options.titleHint || '').slice(0, 80),
    maxlength: '80',
    'aria-label': ossText('study.oss.molecule.label', '显示名称'),
    placeholder: ossText('study.oss.molecule.labelPlaceholder', '可选：水分子'),
  });
  const presetSelect = el('select', { class: 'input', 'aria-label': ossText('study.oss.molecule.preset', '常见分子') },
    el('option', { value: '' }, ossText('study.oss.molecule.presetPlaceholder', '选择常见分子')),
    ...PRESETS.map(preset => el('option', { value: preset.smiles }, presetLabel(preset))));
  const styleSelect = el('select', { class: 'input', 'aria-label': ossText('study.oss.molecule.style', '显示样式') },
    ...MOLECULE_STYLES.map(style => el('option', { value: style }, styleLabel(style))));
  const canvas = el('div', { class: 'oss-molecule-canvas' });
  const hint = el('p', { class: 'help' }, ossText(
    'study.oss.molecule.hint',
    '输入 SMILES 后自动重建模型；保存后可在笔记附件面板只读打开，也可插入课堂演示场景。',
  ));
  const saveButton = button(
    ossText('study.oss.molecule.save', '保存为学习附件'),
    event => busy(event.currentTarget, () => save()),
    '', 'download',
  );
  const closeButton = button(ossText('study.oss.molecule.close', '关闭'), () => dialog.close(), 'secondary');
  let controller = null;
  let rebuildTimer = 0;

  function currentDefinition() {
    const definition = normalizeMoleculeDefinition({
      smiles: smileInput.value,
      label: labelInput.value,
      style: styleSelect.value,
    });
    return definition;
  }

  function rebuild() {
    const definition = currentDefinition();
    if (!definition) {
      if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
      canvas.replaceChildren(el('p', { class: 'help notice warning' },
        ossText('study.oss.molecule.invalid', '分子数据不合法：请检查 SMILES 是否为空或过长。')));
      return;
    }
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } }
    controller = createMoleculeView(canvas, definition, { readOnly: false });
  }

  function scheduleRebuild() {
    clearTimeout(rebuildTimer);
    rebuildTimer = setTimeout(rebuild, 220);
  }

  presetSelect.addEventListener('change', () => {
    if (!presetSelect.value) return;
    smileInput.value = presetSelect.value;
    const preset = PRESETS.find(item => item.smiles === presetSelect.value);
    if (preset && !labelInput.value.trim()) {
      labelInput.value = presetLabel(preset).replace(/\s+[A-Z0-9]+$/, '');
    }
    rebuild();
  });
  smileInput.addEventListener('input', scheduleRebuild);
  styleSelect.addEventListener('change', rebuild);

  async function save() {
    const definition = currentDefinition();
    if (!definition) {
      toast(ossText('study.oss.molecule.invalid', '分子数据不合法：请检查 SMILES 是否为空或过长。'), true);
      return;
    }
    if (!controller) rebuild();
    if (controller) {
      const outcome = await controller.ready;
      if (!outcome || outcome.ok === false) {
        toast(ossText('study.oss.molecule.invalid', '分子数据不合法：请检查 SMILES 是否有效。'), true);
        return;
      }
    }
    const preview = buildPreview(controller, definition);
    const links = Array.isArray(options.links) ? options.links : [];
    const ref = String(options.ref || '').trim();
    const body = {
      kind: MOLECULE_KIND,
      title: (definition.label || definition.smiles).slice(0, 120),
      data: definition,
      preview,
      links,
    };
    if (ref) {
      body.source_kind = 'question';
      body.source_ref = ref;
    }
    try {
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      toast(ossText('study.oss.molecule.saved', '已保存为学习附件：{id}', {
        id: artifact.artifact_id || '',
      }));
      if (typeof options.onSaved === 'function') {
        try { options.onSaved({ artifact, created }); } catch { /* 界面回调失败不影响保存结果 */ }
      }
      dialog.close();
    } catch (error) {
      toast(error?.message || ossText('study.oss.molecule.saveFailed', '保存分子模型失败'), true);
    }
  }

  const initialStyle = String(options.initialStyle || 'stick');
  if (MOLECULE_STYLES.includes(initialStyle)) styleSelect.value = initialStyle;
  dialog.append(
    titleNode,
    el('div', { class: 'oss-molecule-toolbar' },
      el('label', { class: 'oss-molecule-field' },
        el('span', {}, 'SMILES'), smileInput),
      el('label', { class: 'oss-molecule-field' },
        el('span', {}, ossText('study.oss.molecule.label', '显示名称')), labelInput)),
    el('div', { class: 'oss-molecule-toolbar' },
      el('label', { class: 'oss-molecule-field' },
        el('span', {}, ossText('study.oss.molecule.preset', '常见分子')), presetSelect),
      el('label', { class: 'oss-molecule-field' },
        el('span', {}, ossText('study.oss.molecule.style', '显示样式')), styleSelect)),
    canvas,
    hint,
    el('div', { class: 'oss-formula-toolbar' }, closeButton, saveButton),
  );
  dialog.addEventListener('close', () => {
    clearTimeout(rebuildTimer);
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
    dialog.remove();
  });
  document.body.append(dialog);
  dialog.showModal();
  rebuild();
  smileInput.focus();
  if (options.trigger && typeof options.trigger.focus === 'function') {
    dialog.addEventListener('close', () => { try { options.trigger.focus(); } catch { /* 忽略 */ } });
  }
  return dialog;
}
