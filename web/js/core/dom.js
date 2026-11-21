import { renderMath, sanitizeMath } from './markdown.js';
// P8 i18n：日期/数字格式与题型/作答结果等共享语义标签跟随界面语言；
// 页面标题、按钮等各页自有的界面文案仍由各页面用 t() 替换。
import { t, formatNumber as intlNumber, formatDate as intlDate } from './i18n.js';

export const $ = (selector, root = document) => root.querySelector(selector);

export function fill(node, ...children) {
  node.replaceChildren(...children.filter(c => c !== null && c !== undefined && c !== false));
}

export function appendSafe(node, ...children) {
  node.append(...children.filter(c => c !== null && c !== undefined && c !== false));
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (['checked', 'disabled', 'required', 'hidden', 'multiple', 'selected', 'open'].includes(key)) {
      node[key] = Boolean(value);
    } else if (key === 'value') {
      node.value = value;
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const ICON_PATHS = {
  home: ['M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z'],
  book: ['M12 5v16M12 5C9 3 5 3 2 4v15c4-1 7 0 10 2 3-2 6-3 10-2V4c-3-1-7-1-10 1Z'],
  repeat: ['M20 7v5h-5M4 17v-5h5M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9'],
  scan: ['M8 3H4a1 1 0 0 0-1 1v4M16 3h4a1 1 0 0 1 1 1v4M3 16v4a1 1 0 0 0 1 1h4M21 16v4a1 1 0 0 1-1 1h-4M7 8h10M7 12h7M7 16h10'],
  notes: ['M7 3H4v18h16V3h-3M9 2h6v4H9ZM8 10h8M8 14h8M8 18h5'],
  sliders: ['M4 4v6M4 14v6M12 4v10M12 18v2M20 4v2M20 10v10M1 10h6M9 14h6M17 6h6'],
  shield: ['m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6ZM8 12l3 3 5-6'],
  sparkle: ['m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2ZM20 2v4M18 4h4'],
  search: ['M21 21l-5-5', 'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0'],
  upload: ['M12 16V3M7 8l5-5 5 5M4 15v6h16v-6'],
  plus: ['M12 5v14M5 12h14'],
  check: ['m5 12 4 4L19 6'],
  clock: ['M12 7v5l3 2', 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0'],
  target: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0', 'M17 12a5 5 0 1 1-10 0 5 5 0 0 1 10 0', 'M12 11v2'],
  chart: ['M4 20V10M10 20V4M16 20v-8M22 20H2'],
  logout: ['M9 3H3v18h6M14 7l5 5-5 5M7 12h12'],
  lock: ['M7 10V7a5 5 0 0 1 10 0v3M5 10h14v11H5ZM12 14v3'],
  eye: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  close: ['M6 6l12 12M18 6 6 18'],
  file: ['M14 2H4v20h16V8ZM14 2v6h6M8 13h8M8 17h5'],
  key: ['M14 6a4 4 0 1 1-4 4 4 4 0 0 1 4-4ZM11 13l-8 8M5 19l-2-2M8 16l-2-2'],
  trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
  edit: ['m15 4 5 5M3 21l5-1L21 7l-5-5L3 15Z'],
  chevron: ['m9 5 7 7-7 7'],
  download: ['M12 3v12M7 10l5 5 5-5M4 17v4h16v-4'],
  leaf: ['M4 20 17 7M5 17C0 8 7 2 21 3c1 14-6 20-14 15M12 12V7M12 12h5'],
  copy: ['M9 9h11v11H9z', 'M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1'],
  stop: ['M6 6h12v12H6z'],
  rotate: ['M21 12a9 9 0 1 1-3-6.7L21 8', 'M21 3v5h-5'],
  history: ['M3 12a9 9 0 1 0 3-6.7L3 8', 'M3 3v5h5', 'M12 7v5l3 2'],
  tag: ['M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z', 'M7 7h.01'],
  compass: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0', 'm16 8-2.6 5.4L8 16l2.6-5.4L16 8Z'],
  atom: ['M12 12m-1.6 0a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0', 'M2.5 12a9.5 5 0 1 0 19 0a9.5 5 0 1 0-19 0', 'M6.2 5.1a5.1 9.6 0 1 0 11.6 13.8a5.1 9.6 0 1 0-11.6-13.8'],
  message: ['M21 11.5a8.4 8.4 0 0 1-8.5 8.3 9 9 0 0 1-3.9-.9L3 21l1.9-4.6a8.1 8.1 0 0 1-1.4-4.9A8.4 8.4 0 0 1 12 3.2a8.4 8.4 0 0 1 9 8.3Z', 'M8.5 11.5h7M8.5 15h4.5'],
  presentation: ['M3 4h18v12H3z', 'M12 16v4M8 21l4-5 4 5'],
  printer: ['M6 9V3h12v6', 'M6 18H4v-7h16v7h-2', 'M6 14h12v7H6z'],
  layers: ['m12 3 9 5-9 5-9-5 9-5Z', 'm3 13 9 5 9-5', 'm3 17 9 5 9-5'],
  timer: ['M12 8v5l3 2', 'M9 2h6', 'M21 13a9 9 0 1 1-18 0 9 9 0 0 1 18 0'],
  flame: ['M12 22c4.4 0 7-2.9 7-6.5 0-3.1-2.2-5.6-4.2-7.9C13.4 6 12.3 4.1 12 2c-.3 2.1-1.4 4-2.8 5.6C7.2 9.9 5 12.4 5 15.5 5 19.1 7.6 22 12 22Z', 'M12 17.5c1.7 0 2.8-1.2 2.8-2.6 0-2.2-2.8-4.4-2.8-4.4s-2.8 2.2-2.8 4.4c0 1.4 1.1 2.6 2.8 2.6Z'],
  university: ['m3 9 9-6 9 6v11H3z', 'M8 20v-6h8v6', 'M12 3v4'],
  play: ['M8 5l11 7-11 7V5Z'],
  pause: ['M9 5v14M15 5v14'],
  star: ['m12 3 2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 18.2 5.9 21.6l1.4-6.8L2.2 10.1l6.9-.8L12 3Z'],
  'star-off': ['m12 3 2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 18.2 5.9 21.6l1.4-6.8L2.2 10.1l6.9-.8L12 3Z', 'M3 3l18 18'],
  // P8 暗色主题切换按钮使用：浅色=太阳、暗色=月亮。
  sun: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z', 'M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41'],
  moon: ['M21 12.8A8.5 8.5 0 1 1 11.2 3 6.8 6.8 0 0 0 21 12.8Z'],
};

export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of ICON_PATHS[name] || ICON_PATHS.book) {
    const p = document.createElementNS(svg.namespaceURI, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

export function button(text, action, variant = '', iconName = null, attrs = {}) {
  return el('button', { type: 'button', class: `button ${variant}`.trim(), onClick: action, ...attrs },
    iconName ? icon(iconName) : null, text);
}

export function badge(text, type = '') {
  return el('span', { class: `badge ${type}`.trim() }, text);
}

export function toast(message, error = false) {
  const region = $('#toast-region');
  if (!region) return;
  const node = el('div', { class: `toast${error ? ' error' : ''}`, role: 'status' }, message);
  region.append(node);
  setTimeout(() => node.remove(), error ? 8500 : 4500);
}

export async function busy(node, action) {
  if (!node || node.disabled) return;
  node.disabled = true;
  node.setAttribute('aria-busy', 'true');
  try {
    return await action();
  } catch (error) {
    toast(error.message || t('common.actionFailed'), true);
  } finally {
    // 提交/保存后页面可能已重建按钮：旧节点已脱离文档时不要再改它（否则会
    // 把重建后的同名按钮状态还原成旧值），只恢复仍在页面上的那个按钮。
    if (node.isConnected) {
      node.disabled = false;
      node.removeAttribute('aria-busy');
    }
  }
}

export function loading(text = t('common.loading')) {
  return el('div', { class: 'loading' }, el('span', { class: 'spinner' }), text);
}

export function empty(title, text, action = null, name = 'book') {
  return el('div', { class: 'empty' },
    el('div', { class: 'empty-icon' }, icon(name)),
    el('h3', {}, title),
    el('p', {}, text),
    action);
}

export function notice(text, type = '') {
  return el('div', { class: `notice ${type}`.trim() }, text);
}

export function heading(eyebrow, title, subtitle, action = null) {
  return el('div', { class: 'page-heading' },
    el('div', {},
      el('div', { class: 'eyebrow' }, eyebrow),
      el('h1', {}, title),
      el('p', {}, subtitle)),
    action);
}

let fieldSequence = 0;
export function field(label, control, hint = null) {
  const id = control.id || `field-${++fieldSequence}`;
  control.id = id;
  return el('div', { class: 'field' },
    el('label', { for: id }, label),
    control,
    hint ? el('p', { class: 'help' }, hint) : null);
}

export function input(placeholder = '', attrs = {}) {
  return el('input', { class: 'input', placeholder, ...attrs });
}

export function select(options, value = '', attrs = {}) {
  const node = el('select', attrs);
  for (const option of options) {
    if (option && option.optgroup) {
      const og = el('optgroup', { label: option.optgroup });
      for (const child of option.options || []) {
        og.append(el('option', { value: child.value, disabled: child.disabled }, child.label));
      }
      node.append(og);
    } else {
      const item = typeof option === 'object' ? option : { value: option, label: option };
      node.append(el('option', { value: item.value, disabled: item.disabled }, item.label));
    }
  }
  node.value = String(value ?? '');
  if (node.selectedIndex < 0 && node.options.length) node.selectedIndex = 0;
  return node;
}

export function query(values) {
  return new URLSearchParams(
    Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== '' && v !== null && v !== undefined && v !== false)
    )
  ).toString();
}

export function dateText(value) {
  if (!value) return t('common.noRecord');
  return intlDate(value, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function number(value) {
  return intlNumber(Number(value || 0));
}

export function sourceOf(q) {
  return [q.year ? t('common.yearLabel', { year: q.year }) : null, q.province, q.paper_name].filter(Boolean).join(' · ');
}

export function typeLabel(q, catalog = null) {
  const fallback = q.type_label || catalog?.type_labels?.[q.type] || ({
    single_choice: '单选题', multi_choice: '多选题', fill_blank: '填空题',
    calculation: '计算题', short_answer: '简答题', sub_short_answer: '材料子题',
    material_group: '材料题', essay: '作文', unknown: '其他',
  })[q.type] || q.type || '未分类';
  // 有后端/题库提供的 type_label 时优先使用（数据内容不翻译）；否则按题型枚举本地化。
  if (q.type_label || catalog?.type_labels?.[q.type]) return fallback;
  const translated = t(`question.type.${q.type || 'unclassified'}`);
  return translated.startsWith('question.type.') ? fallback : translated;
}

export function resultLabel(result) {
  if (!result) return t('question.result.unanswered');
  const translated = t(`question.result.${result}`);
  if (!translated.startsWith('question.result.')) return translated;
  return ({ correct: '回答正确', incorrect: '需要再练', pending: '待核验', ungraded: '待核验' })[result] || t('question.result.unanswered');
}

export function resultBadge(result) {
  return badge(resultLabel(result), result === 'correct' ? 'teal' : result === 'incorrect' ? 'red' : 'amber');
}

export function statCard(label, value, unit, caption, name) {
  return el('div', { class: 'card stat-card' },
    el('div', { class: 'stat-top' }, label, el('span', { class: 'stat-icon' }, icon(name))),
    el('div', { class: 'stat-value' }, value, el('span', { class: 'stat-unit' }, unit)),
    el('div', { class: 'stat-caption' }, caption));
}

export function showImage(url, alt) {
  const dialog = el('dialog', { class: 'image-dialog' },
    el('img', { src: url, alt }),
    el('button', { class: 'close-dialog', type: 'button', 'aria-label': t('common.closeImage'), onClick: () => dialog.close() }, icon('close')));
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
}

export function bankImage(subject, image, context = {}) {
  if (!image?.file) return el('span', { class: 'image-error' }, t('question.imageMissing'));
  // 契约 §6.1：素材按题库版本、当前小题与素材 ID 校验读权限；答案/解析图在作答后放行。
  const params = { subject, file: image.file };
  if (context.qid) params.qid = context.qid;
  if (context.bankVersion) params.bank_version = context.bankVersion;
  if (image.asset_id) params.asset_id = image.asset_id;
  if (context.preview) params.preview = 'true';
  const url = `/api/bank-image?${query(params)}`;
  const isFormula = image.role === 'formula';
  const node = el('img', {
    class: `bank-image ${isFormula ? 'formula' : ''}`,
    src: url,
    alt: isFormula ? t('question.sourceFormula') : t('question.sourceFigure'),
    loading: 'lazy',
    tabindex: '0',
    role: 'button',
    'aria-label': isFormula ? t('question.zoomFormula') : t('question.zoomFigure'),
    onClick: () => showImage(url, isFormula ? t('question.formula') : t('question.figure')),
    onKeydown: event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        showImage(url, isFormula ? t('question.formula') : t('question.figure'));
      }
    },
  });
  node.addEventListener('error', () => {
    node.replaceWith(el('span', { class: 'image-error' }, isFormula ? t('question.formulaLoadFailed') : t('question.imageLoadFailed')));
  });
  return node;
}

export function highlightKeyword(text, keyword) {
  const frag = document.createDocumentFragment();
  const str = String(text || '');
  const terms = String(keyword || '').trim().split(/\s+/).filter(Boolean);
  if (!terms.length) {
    frag.append(document.createTextNode(str));
    return frag;
  }
  const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const rx = new RegExp(`(${escaped})`, 'gi');
  let last = 0;
  for (const m of str.matchAll(rx)) {
    if (m.index > last) frag.append(document.createTextNode(str.slice(last, m.index)));
    frag.append(el('mark', { class: 'kw-mark' }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < str.length) frag.append(document.createTextNode(str.slice(last)));
  return frag;
}

export function questionText(text, images = [], subject = '', keyword = '', context = {}) {
  const node = el('div', { class: 'question-text' });
  const parts = String(text || '').split(/(\[(?:图|公式)\d+\])/g);
  for (const part of parts) {
    const match = part.match(/^\[(图|公式)(\d+)\]$/);
    if (!match) {
      node.append(keyword ? highlightKeyword(part, keyword) : document.createTextNode(part));
      continue;
    }
    const role = match[1] === '公式' ? 'formula' : 'figure';
    const image = images.filter(i => i.role === role)[Number(match[2]) - 1];
    node.append(image ? bankImage(subject, image, context) : el('span', { class: 'image-error' }, t('question.partMissing', { part })));
  }
  return node;
}

/* ------------------------------------------------------------------------- *
 * 列表摘要可读化（4.1）：列表接口不保证返回 stem_blocks，且 stem_plain 里保留
 * `[公式N]` / `[图N]` 占位。这里纯前端降级：公式占位换成「公式」，图片占位保留
 * 「图」提示；若某项返回了 stem_blocks，则优先按块拼成可读纯文本。
 * ------------------------------------------------------------------------- */

const SUMMARY_FORMULA_RE = /\[公式\s*\d+\]/g;
const SUMMARY_FIGURE_RE = /\[图\s*\d+\]/g;

function summaryBlockText(block) {
  if (!block || typeof block !== 'object') return '';
  const kind = blockKindOf(block);
  if (kind === 'math') {
    if (typeof block.text === 'string' && block.text.trim()) return block.text.trim();
    return t('question.formulaPlaceholder');
  }
  if (kind === 'image') return t('question.figurePlaceholder');
  if (kind === 'table') return typeof block.text === 'string' && block.text.trim() ? block.text.trim() : t('question.tablePlaceholder');
  return typeof block.text === 'string' ? block.text : '';
}

/**
 * 把题目卡片摘要转成可读文本：优先 stem_blocks 拼装，其次 stem_plain；
 * 最后把残留的 `[公式N]` / `[图N]` 占位替换为可读提示（不改后端字段契约）。
 *
 * @param {{stem_blocks?: any, stem_plain?: string, stem?: string}|string} question
 * @param {number} limit 截断长度（≤0 表示不截断）
 */
export function readableSummary(question, limit = 120) {
  let text = '';
  if (question && typeof question === 'object') {
    const blocks = blockList(question.stem_blocks);
    if (blocks.length) {
      text = blocks.map(summaryBlockText).filter(Boolean).join('');
    } else {
      text = String(question.stem_plain || question.stem || '');
    }
  } else {
    text = String(question || '');
  }
  text = text
    .replace(SUMMARY_FORMULA_RE, t('question.formulaPlaceholder'))
    .replace(SUMMARY_FIGURE_RE, t('question.figurePlaceholder'))
    .replace(/\s+/g, ' ')
    .trim();
  if (limit > 0 && text.length > limit) text = `${text.slice(0, limit)}…`;
  return text;
}

/* ===== 契约 §3 内容块（Block）渲染 ===== */

const BLOCK_KINDS = new Set(['text', 'math', 'image', 'table']);

/** 兼容后端字段：数组原样使用；JSON 字符串尝试解析；其它返回空数组。 */
export function blockList(value) {
  if (Array.isArray(value)) return value.filter(item => item && typeof item === 'object');
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.filter(item => item && typeof item === 'object') : [];
    } catch {
      return [];
    }
  }
  return [];
}

function blockKindOf(block) {
  const kind = String(block.kind || block.type || '').toLowerCase();
  if (BLOCK_KINDS.has(kind)) return kind;
  if (block.table) return 'table';
  if (block.asset) return 'image';
  if (block.latex || block.fallback_asset) return 'math';
  return 'text';
}

function blockAssetOf(value) {
  if (!value) return null;
  if (typeof value === 'string') return value ? { file: value } : null;
  if (typeof value === 'object' && typeof value.file === 'string' && value.file) return value;
  return null;
}

// 契约 §3：不同 block_id/line_index 是原卷的不同段落行，行内切分的块保持相连。
function blockLineKey(block) {
  const base = block.block_id ?? block.paragraph_id ?? block.block_order ?? block.order;
  if (base === undefined || base === null || base === '') return null;
  const line = block.line_index;
  return line === undefined || line === null ? String(base) : `${base}#${line}`;
}

// 契约 §3：仅解析 _{...} 下标与 ^{...} 上标；不嵌套，字面量花括号不转义。
// 只创建文本节点 / <sub> / <sup>，不把块内容当 HTML 插入，关键词高亮只作用于文字。
function markedText(text, keyword = '') {
  const fragment = document.createDocumentFragment();
  const source = String(text ?? '');
  const literal = value => (keyword ? highlightKeyword(value, keyword) : document.createTextNode(value));
  const pattern = /([_^])\{([^{}]*)\}/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > cursor) fragment.append(literal(source.slice(cursor, match.index)));
    fragment.append(el(match[1] === '_' ? 'sub' : 'sup', {}, literal(match[2])));
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) fragment.append(literal(source.slice(cursor)));
  return fragment;
}

function textBlockNode(block, keyword) {
  const text = typeof block.text === 'string' ? block.text : (block.text === null || block.text === undefined ? '' : String(block.text));
  if (!text) return null;
  return el('span', { class: 'block block-text' }, markedText(text, keyword));
}

function imageBlockNode(block, subject, context = {}) {
  const asset = blockAssetOf(block.asset);
  if (!asset) return el('span', { class: 'block block-image' }, el('span', { class: 'image-error' }, t('question.imageMissing')));
  return el('span', { class: 'block block-image' }, bankImage(subject, asset, context));
}

function mathBlockNode(block, subject, keyword, context = {}) {
  const latex = typeof block.latex === 'string' ? block.latex.trim() : '';
  // 契约 §3：latex 仅供增强；先用 KaTeX，失败/缺失时展示原卷公式图，再退可读文本。
  // 内容块要求 KaTeX 对非法 LaTeX 抛错（throwOnError:true），并由本函数接管回退；
  // 即使 KaTeX 抛错或输出 katex-error/math-fallback，都视为渲染失败，改显示 fallback_asset。
  if (latex && window.katex && typeof window.katex.renderToString === 'function') {
    try {
      const html = renderMath(latex, false, { throwOnError: true });
      if (html && !html.includes('katex-error') && !html.includes('math-fallback')) {
        const holder = el('span', { class: 'block block-math math-inline' });
        const template = document.createElement('template');
        template.innerHTML = sanitizeMath(html);
        holder.append(template.content);
        if (holder.childNodes.length) return holder;
      }
    } catch {
      /* 继续走原卷公式图兜底 */
    }
  }
  const fallback = blockAssetOf(block.fallback_asset);
  if (fallback) {
    const node = el('span', { class: 'block block-math block-math-asset' },
      bankImage(subject, { role: 'formula', ...fallback }, context));
    node.dataset.mathFallback = 'asset';
    return node;
  }
  const readable = typeof block.text === 'string' ? block.text : '';
  if (readable.trim()) {
    return el('span', { class: 'block block-math block-math-text' }, markedText(readable, keyword));
  }
  return el('span', { class: 'block block-math' }, el('span', { class: 'image-error' }, t('question.formulaMissing')));
}

function tableCellText(cell) {
  if (cell === null || cell === undefined) return '';
  if (typeof cell === 'string' || typeof cell === 'number') return String(cell);
  if (typeof cell === 'object') return String(cell.text ?? cell.value ?? '');
  return String(cell);
}

function tableBlockNode(block, keyword) {
  const rows = Array.isArray(block.table?.rows)
    ? block.table.rows
    : (Array.isArray(block.rows) ? block.rows : null);
  const wrapper = el('div', { class: 'table-wrap block-table' });
  if (!rows || !rows.length) {
    const fallback = typeof block.text === 'string' ? block.text.trim() : '';
    if (!fallback) return null;
    wrapper.append(el('span', { class: 'block block-text' }, markedText(fallback, keyword)));
    return wrapper;
  }
  const table = el('table');
  for (const row of rows) {
    const cells = Array.isArray(row) ? row : [row];
    const tr = el('tr');
    for (const cell of cells) tr.append(el('td', {}, markedText(tableCellText(cell), keyword)));
    table.append(tr);
  }
  wrapper.append(table);
  return wrapper;
}

/**
 * 按契约 §3 把 Block 数组渲染为文档片段。
 * - text：解析 _{...}/^{...} 为 <sub>/<sup>，只创建文本节点（关键词高亮仅作用于文字）；
 * - image：`asset` 经 bankImage 走 /api/bank-image；
 * - math：KaTeX 优先（markdown.js 的 renderMath + sanitizeMath），失败/缺 latex 时用
 *   `fallback_asset` 原卷公式图（契约要求有图必须能显示），再退 `text` 可读线性文本；
 * - table：`table.rows` 二维数组渲染为 `.table-wrap.block-table` 内表格。
 * 同一 block_id/line_index 的块行内相连，跨段落行以 <br> 分隔。
 *
 * @param {Array|string} blocks Block 数组（也容忍 JSON 字符串）
 * @param {string} subject 学科（素材接口需要）
 * @param {string} keyword 检索关键词（只高亮文字节点）
 * @param {object} context 素材授权上下文 `{qid, bankVersion, preview}`
 * @returns {DocumentFragment}
 */
export function questionBlocks(blocks, subject = '', keyword = '', context = {}) {
  const fragment = document.createDocumentFragment();
  let lastKey = null;
  let hasContent = false;
  for (const block of blockList(blocks)) {
    const kind = blockKindOf(block);
    const node = kind === 'table'
      ? tableBlockNode(block, keyword)
      : kind === 'image'
        ? imageBlockNode(block, subject, context)
        : kind === 'math'
          ? mathBlockNode(block, subject, keyword, context)
          : textBlockNode(block, keyword);
    if (!node) continue;
    const key = blockLineKey(block);
    if (hasContent && lastKey !== null && key !== null && key !== lastKey) {
      fragment.append(document.createElement('br'));
    }
    fragment.append(node);
    hasContent = true;
    if (key !== null) lastKey = key;
  }
  return fragment;
}

export function downloadText(filename, content, mime = 'text/markdown;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = el('a', { href: url, download: filename.replace(/[\\/:*?"<>|]/g, '_') });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 下载二进制内容（如 .apkg 牌组）：Blob 需要更长的回收延迟，避免下载被提前取消。 */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: filename.replace(/[\\/:*?"<>|]/g, '_') });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function wireTabKeyboard(tabBar) {
  tabBar.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...tabBar.querySelectorAll('[role="tab"]')];
    const idx = tabs.indexOf(document.activeElement);
    if (idx < 0 || !tabs.length) return;
    event.preventDefault();
    let next = idx;
    if (event.key === 'ArrowRight') next = (idx + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (idx - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    tabs[next].focus();
    tabs[next].click();
  });
}

export function copyText(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(() => toast(t('common.copied')));
  }
  const ta = el('textarea', { value: text, style: 'position:fixed;opacity:0' });
  document.body.append(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
  toast(t('common.copied'));
}
