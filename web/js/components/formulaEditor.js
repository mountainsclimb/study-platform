/* MathLive 可视化公式编辑（《开源组件整合实施方案》§3.3）。
 *
 * 用法：
 *   const latex = await openFormulaEditor({ initialLatex, title });
 *   // 插入返回 LaTeX 字符串（不含 $ 定界符），取消返回 null。
 *
 * 边界：
 * * MathLive 主脚本与字体随仓库分发在 /static/vendor/mathlive/，首次打开时才注入，
 *   不进首屏；加载失败只弹中文提示并回退到现有 createFormulaInput 文本编辑。
 * * 主站 CSP 为 script-src 'self'，本组件只注入同源脚本/样式，不引入 eval。
 * * 弹层用原生 <dialog> + showModal()，主控件不小于 44px（见 styles-oss.css）。
 */
import { el, button } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { createFormulaInput } from './formulaInput.js';

const SCRIPT_URL = '/static/vendor/mathlive/mathlive.min.js';
const STYLE_URL = '/static/vendor/mathlive/mathlive-static.css';
const SCRIPT_MARK = 'ossMathliveScript';
const STYLE_MARK = 'ossMathliveStyle';
//: 脚本加载成功后等待 <math-field> 定义的最长时间；超时回退文本输入，避免 Promise 永久挂起。
const DEFINE_TIMEOUT_MS = 15000;

let loaderPromise = null;
let dialogSequence = 0;

/** 去掉用户可能带上的 $...$ / $$...$$ / \(...\) 定界符。 */
export function normalizeLatex(value) {
  let text = String(value ?? '').trim();
  if (text.startsWith('$$') && text.endsWith('$$') && text.length >= 4) {
    text = text.slice(2, -2).trim();
  } else if (text.startsWith('$') && text.endsWith('$') && text.length >= 2) {
    text = text.slice(1, -1).trim();
  } else if (text.startsWith('\\(') && text.endsWith('\\)') && text.length >= 4) {
    text = text.slice(2, -2).trim();
  } else if (text.startsWith('\\[') && text.endsWith('\\]') && text.length >= 4) {
    text = text.slice(2, -2).trim();
  }
  return text;
}

function ensureStyle() {
  if (document.querySelector(`link[data-oss="${STYLE_MARK}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = STYLE_URL;
  link.dataset.oss = STYLE_MARK;
  document.head.append(link);
}

/** 首次调用时注入 MathLive；重复调用复用同一个 Promise。 */
export function loadMathLive() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error('当前环境不支持公式编辑器'));
  }
  if (window.customElements && window.customElements.get('math-field')) {
    return Promise.resolve();
  }
  if (loaderPromise) return loaderPromise;
  loaderPromise = new Promise((resolve, reject) => {
    ensureStyle();
    let script = document.querySelector(`script[data-oss="${SCRIPT_MARK}"]`);
    const settle = () => {
      if (!window.customElements) {
        reject(new Error('浏览器不支持 Web Components'));
        return;
      }
      const timer = setTimeout(() => {
        script.remove();
        reject(new Error(t('study.oss.formula.loadFailed')));
      }, DEFINE_TIMEOUT_MS);
      window.customElements.whenDefined('math-field').then(
        () => { clearTimeout(timer); resolve(); },
        error => { clearTimeout(timer); reject(error); },
      );
    };
    if (script && script.dataset.loaded === '1') {
      settle();
      return;
    }
    if (!script) {
      script = document.createElement('script');
      script.src = SCRIPT_URL;
      script.async = true;
      script.dataset.oss = SCRIPT_MARK;
      document.head.append(script);
    }
    const onLoad = () => {
      script.dataset.loaded = '1';
      settle();
    };
    script.addEventListener('load', onLoad, { once: true });
    script.addEventListener('error', () => {
      // 移除失败的 script，允许用户稍后重试；本次调用仍回退文本输入。
      script.remove();
      reject(new Error(t('study.oss.formula.loadFailed')));
    }, { once: true });
  }).catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

function makeDialog(title) {
  const headingId = `oss-formula-title-${++dialogSequence}`;
  const dialog = el('dialog', {
    class: 'oss-dialog oss-formula-dialog',
    'aria-labelledby': headingId,
  });
  dialog.append(el('h2', { id: headingId, class: 'oss-dialog-title' }, title));
  return dialog;
}

function closeAndResolve(dialog, resolve, value, state) {
  if (state.settled) return;
  state.settled = true;
  try { dialog.close(); } catch { /* 已关闭 */ }
  dialog.remove();
  resolve(value);
}

function openFallback({ initialLatex, title }) {
  return new Promise(resolve => {
    const state = { settled: false };
    const dialog = makeDialog(title);
    const editor = createFormulaInput({
      value: normalizeLatex(initialLatex),
      rows: 3,
      ariaLabel: t('study.oss.formula.editorAria'),
      placeholder: t('study.oss.formula.placeholder'),
    });
    const note = el('p', { class: 'help oss-formula-fallback' }, t('study.oss.formula.loadFailed'));
    const actions = el('div', { class: 'oss-formula-toolbar' },
      button(t('study.oss.formula.cancel'), () => closeAndResolve(dialog, resolve, null, state), 'secondary'),
      button(t('study.oss.formula.confirm'), () => {
        const value = String(editor.getValue() || '').trim();
        closeAndResolve(dialog, resolve, value || null, state);
      }, '', 'check'));
    dialog.append(note, editor.element, actions);
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      closeAndResolve(dialog, resolve, null, state);
    });
    document.body.append(dialog);
    dialog.showModal();
    if (editor.textarea) editor.textarea.focus();
  });
}

/**
 * 打开可视化公式编辑器。
 *
 * @param {object} [options]
 * @param {string} [options.initialLatex] 初始 LaTeX（可带 $ 定界符，会自动剥离）
 * @param {string} [options.title] 弹层标题
 * @returns {Promise<string|null>} 插入时返回 LaTeX，取消/关闭返回 null
 */
export async function openFormulaEditor(options = {}) {
  const settings = options && typeof options === 'object' ? options : {};
  const initialLatex = normalizeLatex(settings.initialLatex);
  const title = String(settings.title || t('study.oss.formula.dialogTitle'));
  try {
    await loadMathLive();
  } catch {
    return openFallback({ initialLatex, title });
  }
  return new Promise(resolve => {
    const state = { settled: false };
    const dialog = makeDialog(title);
    let mathField;
    try {
      mathField = document.createElement('math-field');
    } catch {
      closeAndResolve(dialog, resolve, null, state);
      return;
    }
    mathField.className = 'oss-mathfield';
    mathField.setAttribute('aria-label', t('study.oss.formula.editorAria'));
    mathField.setAttribute('math-virtual-keyboard-policy', 'manual');
    mathField.value = initialLatex;
    const hint = el('p', { class: 'help' }, t('study.oss.formula.insertHint'));
    const actions = el('div', { class: 'oss-formula-toolbar' },
      button(t('study.oss.formula.cancel'), () => closeAndResolve(dialog, resolve, null, state), 'secondary'),
      button(t('study.oss.formula.confirm'), () => {
        let value = '';
        try { value = String(mathField.getValue('latex') || '').trim(); } catch { value = ''; }
        closeAndResolve(dialog, resolve, value || null, state);
      }, '', 'check'));
    dialog.append(mathField, hint, actions);
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      closeAndResolve(dialog, resolve, null, state);
    });
    dialog.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        let value = '';
        try { value = String(mathField.getValue('latex') || '').trim(); } catch { value = ''; }
        closeAndResolve(dialog, resolve, value || null, state);
      }
    });
    document.body.append(dialog);
    dialog.showModal();
    try { mathField.focus(); } catch { /* 忽略聚焦失败 */ }
  });
}

/**
 * 把 LaTeX 以 `$...$` 插入 textarea/input 的光标处（照搬拍题页 insertIntoStem 的做法）。
 * 不支持的控件返回 false。
 */
export function insertFormulaAtCursor(control, latex) {
  const value = normalizeLatex(latex);
  if (!control || !value || typeof control.value !== 'string') return false;
  const snippet = `$${value}$`;
  const source = control.value;
  let start = Number.isInteger(control.selectionStart) ? control.selectionStart : source.length;
  let end = Number.isInteger(control.selectionEnd) ? control.selectionEnd : start;
  start = Math.min(Math.max(0, start), source.length);
  end = Math.min(Math.max(start, end), source.length);
  control.value = `${source.slice(0, start)}${snippet}${source.slice(end)}`;
  const caret = Math.min(control.value.length, start + snippet.length);
  try { control.setSelectionRange(caret, caret); } catch { /* 个别环境不支持选区 */ }
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.focus();
  return true;
}
