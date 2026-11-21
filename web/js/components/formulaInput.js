/* 公式输入组件（知序 W0/W1：OCR-first 拍题入库的公式校对入口）。
 *
 * 设计要点：
 * - **值模型是 LaTeX 字符串（不含 `$`/`\\(` 等定界符）**：组件内部负责定界符容错、
 *   实时预览（复用本地 KaTeX，经 core/markdown.js 的 renderMath + sanitizeMath）与
 *   无 KaTeX 时的纯文本回退（toPlainText）。不引入任何新依赖、不从 CDN 加载。
 * - **编辑面以 textarea 为主**：中文输入法（composition 事件期间不打断）、触屏、
 *   多行粘贴都沿用原生输入框行为；工具栏按钮只做「在光标处插入模板」，`mousedown`
 *   与 `pointerdown` 均 preventDefault，点击按钮不会把焦点从编辑框抢走。
 * - 所有用户可见文案走 `study.formula.*` i18n 键。语言包由 Lead 统一合并；合并前
 *   `formulaText()` 先用 t() 取词，缺键时回退到本文件内置的中文兜底文案，避免界面
 *   直接显示键名（合并后以语言包为准）。
 * - 安全：用户内容一律经 textContent / el() 写入；只有 KaTeX 生成的公式 HTML 会经
 *   sanitizeMath() 白名单净化后写入 template.innerHTML（与内容块渲染同一策略，fail-closed）。
 */

import { el } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { renderMath, sanitizeMath } from '../core/markdown.js';

//: LaTeX 输入上限（超出截断并提示；与契约「公式块单块长度上限」保持一致）。
export const FORMULA_MAX_LENGTH = 2000;
//: 后端 PATCH confirm 对识别块字段的上限（与 app/ai/upload_pipeline.py 的校验口径一致）。
const BLOCK_TEXT_MAX = 4000;
const BLOCK_ORDER_MAX = 500;
const BLOCK_PAGE_MAX = 200;
//: 内容块类型（契约 intake-blocks-v1）。
const BLOCK_KINDS = new Set(['text', 'math', 'table', 'image']);

/* ------------------------------------------------------------------------- *
 * i18n：study.formula.* 键 + 中文兜底（语言包由 Lead 合并，这里只是过渡文案）
 * ------------------------------------------------------------------------- */

const FORMULA_TEXT = {
  'study.formula.editor.aria': 'LaTeX 公式编辑框',
  'study.formula.editor.placeholder': '输入 LaTeX（不含 $ 定界符），如 \\frac{a}{b} 或 x^2+1',
  'study.formula.toolbar.aria': '公式快捷插入工具栏',
  'study.formula.tool.power': '插入上标',
  'study.formula.tool.subscript': '插入下标',
  'study.formula.tool.frac': '插入分数',
  'study.formula.tool.sqrt': '插入根号',
  'study.formula.tool.pi': '插入圆周率 π',
  'study.formula.tool.le': '插入小于等于',
  'study.formula.tool.ge': '插入大于等于',
  'study.formula.tool.ne': '插入不等于',
  'study.formula.tool.times': '插入乘号',
  'study.formula.tool.div': '插入除号',
  'study.formula.tool.degree': '插入度数符号',
  'study.formula.tool.integral': '插入积分号',
  'study.formula.tool.sum': '插入求和号',
  'study.formula.tool.triangle': '插入三角形符号',
  'study.formula.tool.pm': '插入正负号',
  'study.formula.tool.angle': '插入角符号',
  'study.formula.tool.infinity': '插入无穷符号',
  'study.formula.tool.arrow': '插入推出箭头',
  'study.formula.preview.title': '实时预览',
  'study.formula.preview.empty': '输入 LaTeX 后这里会实时显示公式',
  'study.formula.preview.error': '公式暂无法渲染，请检查 LaTeX，或使用纯文本',
  'study.formula.preview.katexMissing': '当前页面未加载公式渲染器（KaTeX），已按纯文本显示',
  'study.formula.preview.plainLabel': '纯文本预览：{text}',
  'study.formula.hint.count': '{count}/{max} 字符',
  'study.formula.hint.overflow': '已按 {max} 字符上限截断',
  'study.formula.hint.tab': 'Tab 插入两个空格',
  'study.formula.hint.escape': 'Esc 取消焦点',
  'study.formula.hint.plainFallback': '也可直接输入纯文本（如 a/b、x^2）',
  'study.formula.validate.empty': '请输入公式',
  'study.formula.validate.braces': '花括号不配对：请检查 LaTeX 中的 { 与 } 是否成对',

  'study.formula.review.sectionTitle': '识别块校对（可选）',
  'study.formula.review.sectionSub': '按原图阅读顺序校对识别块；公式块需粘贴或修正 LaTeX，文字块可直接改字。',
  'study.formula.review.engineLine': '引擎：{engine} · 版本 {version}',
  'study.formula.review.engineOnly': '引擎：{engine}',
  'study.formula.review.engineUnknown': '未知引擎',
  'study.formula.review.engineHint': '公式块需人工校对；本机引擎暂不做公式识别。',
  'study.formula.review.mathUnsupported': 'capabilities.math_recognition=false：本机引擎暂不做公式识别，公式块请人工粘贴或修正 LaTeX。',
  'study.formula.review.mathSupported': '公式识别已开启（仍需人工校对）',
  'study.formula.review.mathUnsupportedBadge': '公式识别未开启',
  'study.formula.review.blockCount': '共 {count} 个识别块，其中 {review} 个需校对',
  'study.formula.review.lowConfidenceCount': '{count} 个低置信',
  'study.formula.review.confidence': '置信度 {percent}%',
  'study.formula.review.lowConfidence': '低置信',
  'study.formula.review.kind.text': '文字',
  'study.formula.review.kind.math': '公式',
  'study.formula.review.kind.table': '表格',
  'study.formula.review.kind.image': '图片',
  'study.formula.review.keepOriginal': '保持原图，不做编辑；如需修改请回到原图重拍或人工补录。',
  'study.formula.review.formulaPlaceholder': '粘贴或输入该块的 LaTeX（不含 $ 定界符）',
  'study.formula.review.textPlaceholder': '修正这段识别文字',
  'study.formula.review.mathEditorAria': '识别块 {id} 的公式 LaTeX',
  'study.formula.review.textEditorAria': '识别块 {id} 的文字',
  'study.formula.review.insertFormula': '插入公式',
  'study.formula.review.useAsStem': '采用为题干',
  'study.formula.review.markReviewed': '标记已校对',
  'study.formula.review.reviewed': '已校对',
  'study.formula.review.needsReview': '需校对',
  'study.formula.review.inserted': '已把公式插入题干光标处。',
  'study.formula.review.adopted': '已把该块内容追加到题干，请核对。',
  'study.formula.review.marked': '已标记为已校对。',
  'study.formula.review.emptyLatex': '该公式块还没有 LaTeX，请先粘贴或输入公式。',
  'study.formula.review.blockEmpty': '该块没有可追加的文本。',
  'study.formula.review.noBlocks': '本次没有返回可校对的识别块；可继续手动录入，原有流程不受影响。',
  'study.formula.review.ocrUnavailable': '识别不可用，已保留原图；可继续手动录入题干，确认后正常入库。',
  'study.formula.review.katexMissing': '公式渲染器不可用：公式块将以纯文本方式显示，仍可粘贴 LaTeX 保存。',
  'study.formula.review.blocksRejected': '识别块提交未通过校验：{message}。本地编辑已保留，可修正后重试，或直接确认题干。',
  'study.formula.review.overlayHint': '点击原图上的识别框可定位到对应块。',
  'study.formula.review.noPreview': '没有可定位的原图预览；仍可校对识别块。',
  'study.formula.review.overlayAria': '识别块 {id}：{location}',
  'study.formula.review.location.unknown': '位置未提供',
  'study.formula.review.location.page': '第 {page} 页',
  'study.formula.review.location.order': '阅读顺序 {order}',
  'study.formula.review.location.bbox': '原图位置 ({x}, {y}) {w}×{h}',
  'study.formula.review.location.norm': '原图相对位置 {x}% / {y}%',
};

function interpolateText(text, vars) {
  if (!vars || typeof text !== 'string') return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => {
    const value = vars[name];
    return value === undefined || value === null ? match : String(value);
  });
}

/**
 * 取 `study.formula.*` 文案：优先 t() 语言包；语言包尚未合并该键时回退中文兜底。
 * 合并语言包后本函数行为与 t() 完全一致（仍然只是 i18n 键查询）。
 */
export function formulaText(key, vars) {
  const translated = t(key, vars);
  if (translated !== key) return translated;
  const fallback = FORMULA_TEXT[key];
  return fallback === undefined ? translated : interpolateText(fallback, vars);
}

/* ------------------------------------------------------------------------- *
 * 通用小工具
 * ------------------------------------------------------------------------- */

function clampNumber(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function readFinite(value) {
  // 注意：Number(null) === 0、Number('') === 0，缺字段必须当作「没有值」。
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function readPointNumber(value) {
  const number = readFinite(value);
  if (number === null) return null;
  return Math.abs(number - Math.round(number)) < 0.001 ? String(Math.round(number)) : String(Math.round(number * 10) / 10);
}

/** 去掉整段包裹的 `$...$` / `$$...$$` / `\(...\)` / `\[...\]` 定界符（值模型不含定界符）。 */
function normalizeLatex(value) {
  let text = String(value === undefined || value === null ? '' : value).replace(/\r\n?/g, '\n').trim();
  const wrappers = [
    ['$$', '$$'],
    ['$', '$'],
    ['\\(', '\\)'],
    ['\\[', '\\]'],
  ];
  for (const [open, close] of wrappers) {
    if (text.length > open.length + close.length && text.startsWith(open) && text.endsWith(close)) {
      const inner = text.slice(open.length, text.length - close.length).trim();
      // 只在内部不再出现同种开定界符时剥离，避免把「两个行内公式」误当成一整段。
      if (inner && !inner.includes(open)) {
        text = inner;
        break;
      }
    }
  }
  return text;
}

/** 花括号配对检查（忽略 `\{` / `\}` 等转义字符）。 */
function checkBraceBalance(value) {
  let depth = 0;
  let open = 0;
  let close = 0;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === '\\') {
      index += 1;
      continue;
    }
    if (char === '{') {
      depth += 1;
      open += 1;
    } else if (char === '}') {
      depth -= 1;
      close += 1;
      if (depth < 0) return { ok: false, open, close };
    }
  }
  return { ok: depth === 0 && open === close, open, close };
}

/* ------------------------------------------------------------------------- *
 * LaTeX → 可读纯文本（无 KaTeX 时的回退；未知命令保留原样并去掉反斜杠）
 * ------------------------------------------------------------------------- */

const SYMBOL_COMMANDS = {
  pi: 'π', alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', Delta: 'Δ', epsilon: 'ε',
  varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', lambda: 'λ',
  mu: 'μ', nu: 'ν', xi: 'ξ', rho: 'ρ', sigma: 'σ', Sigma: 'Σ', tau: 'τ',
  upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', Psi: 'Ψ', omega: 'ω', Omega: 'Ω',
  Gamma: 'Γ', Lambda: 'Λ', Theta: 'Θ', Phi: 'Φ', Xi: 'Ξ',
  le: '≤', leq: '≤', ge: '≥', geq: '≥', ne: '≠', neq: '≠', approx: '≈', equiv: '≡',
  sim: '∼', propto: '∝', lt: '<', gt: '>', ll: '≪', gg: '≫',
  times: '×', div: '÷', cdot: '·', pm: '±', mp: '∓', ast: '∗', star: '⋆',
  circ: '°', degree: '°', infty: '∞', partial: '∂', nabla: '∇',
  angle: '∠', triangle: '△', perp: '⊥', parallel: '∥',
  because: '∵', therefore: '∴', in: '∈', notin: '∉', ni: '∋',
  subset: '⊂', subseteq: '⊆', supset: '⊃', supseteq: '⊇', cup: '∪', cap: '∩',
  emptyset: '∅', varnothing: '∅', forall: '∀', exists: '∃', neg: '¬', land: '∧', lor: '∨',
  to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
  leftrightarrow: '↔', Leftrightarrow: '⇔', mapsto: '↦', uparrow: '↑', downarrow: '↓',
  int: '∫', iint: '∬', oint: '∮', sum: '∑', prod: '∏', coprod: '∐',
  ldots: '…', cdots: '⋯', dots: '…', vdots: '⋮', ddots: '⋱',
  sin: 'sin', cos: 'cos', tan: 'tan', cot: 'cot', sec: 'sec', csc: 'csc',
  arcsin: 'arcsin', arccos: 'arccos', arctan: 'arctan',
  log: 'log', ln: 'ln', lg: 'lg', exp: 'exp', lim: 'lim', max: 'max', min: 'min', mod: 'mod',
  dotsc: '…', quad: ' ', qquad: '  ',
};

const TEXT_LIKE_COMMANDS = new Set([
  'text', 'textrm', 'textbf', 'textit', 'textsf', 'texttt', 'textnormal', 'mbox',
  'mathrm', 'mathbf', 'mathit', 'mathcal', 'mathbb', 'mathsf', 'operatorname', 'boxed',
]);

const ACCENT_COMMANDS = {
  bar: '\u0304', hat: '\u0302', vec: '\u20d7', dot: '\u0307', ddot: '\u0308',
  tilde: '\u0303', overline: '\u0305', widehat: '\u0302', widetilde: '\u0303',
};

const IGNORED_COMMANDS = new Set([
  'left', 'right', 'big', 'Big', 'bigg', 'Bigg', 'bigl', 'bigr', 'Bigl', 'Bigr',
  'displaystyle', 'textstyle', 'limits', 'nolimits', 'not', 'nonumber', 'label', 'tag',
]);

const SINGLE_CHAR_COMMANDS = {
  ',': '', ';': ' ', ':': ' ', '!': '', ' ': ' ', '\\': '\n',
  '{': '{', '}': '}', '%': '%', '&': '&', '_': '_', '$': '$', '#': '#',
  '(': '', ')': '', '[': '', ']': '', '-': '', '/': '', '@': '', '~': ' ',
};

const SUPERSCRIPT_MAP = {
  0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹',
  '+': '⁺', '-': '⁻', '−': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
  n: 'ⁿ', i: 'ⁱ', a: 'ᵃ', b: 'ᵇ', c: 'ᶜ', d: 'ᵈ', e: 'ᵉ', f: 'ᶠ', g: 'ᵍ', h: 'ʰ',
  j: 'ʲ', k: 'ᵏ', l: 'ˡ', m: 'ᵐ', o: 'ᵒ', p: 'ᵖ', r: 'ʳ', s: 'ˢ', t: 'ᵗ', u: 'ᵘ',
  v: 'ᵛ', w: 'ʷ', x: 'ˣ', y: 'ʸ', z: 'ᶻ',
};

const SUBSCRIPT_MAP = {
  0: '₀', 1: '₁', 2: '₂', 3: '₃', 4: '₄', 5: '₅', 6: '₆', 7: '₇', 8: '₈', 9: '₉',
  '+': '₊', '-': '₋', '−': '₋', '=': '₌', '(': '₍', ')': '₎',
  a: 'ₐ', e: 'ₑ', h: 'ₕ', i: 'ᵢ', j: 'ⱼ', k: 'ₖ', l: 'ₗ', m: 'ₘ', n: 'ₙ',
  o: 'ₒ', p: 'ₚ', r: 'ᵣ', s: 'ₛ', t: 'ₜ', u: 'ᵤ', v: 'ᵥ', x: 'ₓ',
};

function skipWhitespace(source, index) {
  let cursor = index;
  while (cursor < source.length && (source[cursor] === ' ' || source[cursor] === '\t')) cursor += 1;
  return cursor;
}

/** 从 index 处读取 `{...}` 分组（内部允许嵌套与转义）；缺配对返回 null。 */
function readBraceGroup(source, index) {
  let start = skipWhitespace(source, index);
  if (source[start] !== '{') return null;
  let depth = 0;
  for (let cursor = start; cursor < source.length; cursor += 1) {
    const char = source[cursor];
    if (char === '\\') {
      cursor += 1;
      continue;
    }
    if (char === '{') depth += 1;
    else if (char === '}') {
      depth -= 1;
      if (depth === 0) {
        return { inner: source.slice(start + 1, cursor), next: cursor + 1 };
      }
    }
  }
  return null;
}

/** 从 index 处读取 `[...]` 可选参数（根号次数等）；缺配对返回 null。 */
function readBracketGroup(source, index) {
  if (source[index] !== '[') return null;
  let depth = 0;
  for (let cursor = index; cursor < source.length; cursor += 1) {
    const char = source[cursor];
    if (char === '\\') {
      cursor += 1;
      continue;
    }
    if (char === '[') depth += 1;
    else if (char === ']') {
      depth -= 1;
      if (depth === 0) {
        return { inner: source.slice(index + 1, cursor), next: cursor + 1 };
      }
    }
  }
  return null;
}

function wrapFractionOperand(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  return /^[A-Za-z0-9.]+$/.test(value) ? value : `(${value})`;
}

function scriptText(text, isSuperscript) {
  const value = String(text || '');
  if (!value) return '';
  if (value === '°' || value === '∘') return '°';
  const map = isSuperscript ? SUPERSCRIPT_MAP : SUBSCRIPT_MAP;
  const chars = [...value];
  if (chars.every(char => Object.prototype.hasOwnProperty.call(map, char))) {
    return chars.map(char => map[char]).join('');
  }
  const wrapped = /^[A-Za-z0-9.]+$/.test(value) ? value : `(${value})`;
  return `${isSuperscript ? '^' : '_'}${wrapped}`;
}

function convertCommand(name, source, index) {
  if (name === 'frac' || name === 'dfrac' || name === 'tfrac') {
    const numerator = readBraceGroup(source, index);
    if (!numerator) return { text: name, next: index };
    const denominator = readBraceGroup(source, numerator.next);
    if (!denominator) return { text: `${name}{${convertLatex(numerator.inner)}}`, next: numerator.next };
    const top = wrapFractionOperand(convertLatex(numerator.inner));
    const bottom = wrapFractionOperand(convertLatex(denominator.inner));
    return { text: `${top}/${bottom}`, next: denominator.next };
  }
  if (name === 'sqrt') {
    const bracket = readBracketGroup(source, skipWhitespace(source, index));
    const degree = bracket ? convertLatex(bracket.inner) : '';
    const group = readBraceGroup(source, bracket ? bracket.next : index);
    if (!group) return { text: '√', next: index };
    const inner = convertLatex(group.inner);
    if (degree === '3') return { text: `∛(${inner})`, next: group.next };
    if (degree && degree !== '2') return { text: `√[${degree}](${inner})`, next: group.next };
    return { text: `√(${inner})`, next: group.next };
  }
  if (TEXT_LIKE_COMMANDS.has(name)) {
    const group = readBraceGroup(source, index);
    if (!group) return { text: '', next: index };
    const inner = convertLatex(group.inner);
    return { text: name === 'boxed' ? `[${inner}]` : inner, next: group.next };
  }
  if (Object.prototype.hasOwnProperty.call(ACCENT_COMMANDS, name)) {
    const group = readBraceGroup(source, index);
    if (!group) return { text: name, next: index };
    const inner = convertLatex(group.inner);
    if (inner.length === 1) return { text: inner + ACCENT_COMMANDS[name], next: group.next };
    return { text: `${name}(${inner})`, next: group.next };
  }
  if (name === 'begin' || name === 'end') {
    const group = readBraceGroup(source, index);
    return { text: '', next: group ? group.next : index };
  }
  if (IGNORED_COMMANDS.has(name)) return { text: '', next: index };
  if (Object.prototype.hasOwnProperty.call(SYMBOL_COMMANDS, name)) {
    return { text: SYMBOL_COMMANDS[name], next: index };
  }
  // 未知命令：去掉反斜杠保留命令名（后面的花括号按字面字符保留）。
  return { text: name, next: index };
}

function convertLatex(source) {
  const text = String(source || '');
  let out = '';
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (char === '\\') {
      const nextChar = text[index + 1];
      if (nextChar === undefined) {
        out += '\\';
        index += 1;
        continue;
      }
      if (/[A-Za-z]/.test(nextChar)) {
        let cursor = index + 1;
        while (cursor < text.length && /[A-Za-z]/.test(text[cursor])) cursor += 1;
        const name = text.slice(index + 1, cursor);
        const converted = convertCommand(name, text, cursor);
        out += converted.text;
        index = Math.max(converted.next, cursor);
        continue;
      }
      if (Object.prototype.hasOwnProperty.call(SINGLE_CHAR_COMMANDS, nextChar)) {
        out += SINGLE_CHAR_COMMANDS[nextChar];
      } else {
        out += nextChar;
      }
      index += 2;
      continue;
    }
    if (char === '^' || char === '_') {
      let cursor = skipWhitespace(text, index + 1);
      let inner = '';
      if (text[cursor] === '{') {
        const group = readBraceGroup(text, cursor);
        if (!group) {
          out += char;
          index += 1;
          continue;
        }
        inner = convertLatex(group.inner);
        cursor = group.next;
      } else if (cursor < text.length) {
        inner = convertLatex(text[cursor]);
        cursor += 1;
      } else {
        out += char;
        index += 1;
        continue;
      }
      out += scriptText(inner, char === '^');
      index = cursor;
      continue;
    }
    if (char === '$') {
      // 值模型不含定界符：残留的 `$` 直接去掉，避免纯文本预览出现多余符号。
      index += 1;
      continue;
    }
    if (char === '&') {
      out += ' ';
      index += 1;
      continue;
    }
    out += char;
    index += 1;
  }
  return out
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/ ?\n ?/g, '\n')
    .trim();
}

/**
 * 把 LaTeX 退化为可读纯文本（无 KaTeX / 渲染失败时展示）。
 * 例：`\frac{a}{b}` → `a/b`，`x^{2}` → `x²`，`\sqrt{x+1}` → `√(x+1)`，
 * `\triangle ABC` → `△ABC`；未知命令 `\foo{x}` → `foo{x}`（去掉反斜杠）。
 *
 * @param {string} latex
 * @returns {string}
 */
export function toPlainText(latex) {
  const source = normalizeLatex(latex);
  if (!source) return '';
  return convertLatex(source);
}

/* ------------------------------------------------------------------------- *
 * 内容块（intake-blocks-v1）编辑态转换
 * ------------------------------------------------------------------------- */

function normalizeBlockKind(value, source) {
  const kind = String(value || '').toLowerCase();
  if (BLOCK_KINDS.has(kind)) return kind;
  const block = source && typeof source === 'object' ? source : {};
  if (block.table || Array.isArray(block.rows)) return 'table';
  if (block.asset || block.image_url) return 'image';
  if (block.latex || block.fallback_asset) return 'math';
  return 'text';
}

function kindLabel(kind) {
  const key = `study.formula.review.kind.${BLOCK_KINDS.has(kind) ? kind : 'text'}`;
  return formulaText(key);
}

function readNormRect(rect) {
  if (!rect || typeof rect !== 'object') return null;
  const x = readFinite(rect.x);
  const y = readFinite(rect.y);
  const w = readFinite(rect.w === undefined || rect.w === null ? rect.width : rect.w);
  const h = readFinite(rect.h === undefined || rect.h === null ? rect.height : rect.h);
  if (x === null || y === null || w === null || h === null) return null;
  if (w <= 0 || h <= 0) return null;
  // 契约 norm 是 0–1 归一化值；容错为 0–100 的百分比写法。
  const asPercent = x > 1 || y > 1 || w > 1 || h > 1;
  const scale = asPercent ? 100 : 1;
  const nx = clampNumber(x / scale, 0, 1);
  const ny = clampNumber(y / scale, 0, 1);
  const nw = clampNumber(w / scale, 0.002, 1 - nx);
  const nh = clampNumber(h / scale, 0.002, 1 - ny);
  if (nw <= 0 || nh <= 0) return null;
  return { x: nx, y: ny, w: nw, h: nh };
}

function readBBoxRect(rect) {
  if (!rect || typeof rect !== 'object') return null;
  const x = readFinite(rect.x);
  const y = readFinite(rect.y);
  const w = readFinite(rect.w === undefined || rect.w === null ? rect.width : rect.w);
  const h = readFinite(rect.h === undefined || rect.h === null ? rect.height : rect.h);
  if (x === null || y === null || w === null || h === null) return null;
  if (w <= 0 || h <= 0) return null;
  return { x, y, w, h };
}

/**
 * 提交体的防御性清洗：只保留契约允许的有限数值坐标轴，丢弃非法值。
 * 后端 confirm 会 422 拒绝非数值 bbox/norm，这里提前保证请求体可用（合法值原样保留）。
 */
function sanitizeBlockRect(rect) {
  if (!rect || typeof rect !== 'object' || Array.isArray(rect)) return null;
  const cleaned = {};
  for (const axis of ['x', 'y', 'w', 'h']) {
    const value = rect[axis];
    if (value === undefined || value === null || value === '') continue;
    const number = readFinite(value);
    if (number === null) continue;
    cleaned[axis] = number;
  }
  return Object.keys(cleaned).length ? cleaned : null;
}

/** 提交体 order 约定：0–500 的整数；非法值回落为数组下标（1 基）。 */
function sanitizeBlockOrder(value, fallback) {
  const number = readFinite(value);
  if (number === null || !Number.isInteger(number) || number < 0 || number > BLOCK_ORDER_MAX) return fallback;
  return number;
}

/** 提交体 page 约定：1–200 的整数；非法值回落为第 1 页。 */
function sanitizeBlockPage(value) {
  const number = readFinite(value);
  if (number === null || !Number.isInteger(number) || number < 1 || number > BLOCK_PAGE_MAX) return 1;
  return number;
}

function blockPageOf(block) {
  const page = readFinite(block && block.page);
  return page !== null && page >= 1 ? Math.floor(page) : 1;
}

/**
 * 把 OCR 内容块数组规范化为编辑态数组；math 块转 `{id, kind, text, latexInput, needsReview}`。
 * 容错：非数组 / 缺字段 / 非对象条目全部安全跳过；重复 id 自动加后缀保持唯一。
 *
 * @param {any} blocks 契约 `draft.blocks` / `blocks`（intake-blocks-v1）
 * @returns {Array<object>} 编辑态块（附带 original 原始块，供 buildConfirmBlocks 还原）
 */
export function parseBlocksWithFormulas(blocks) {
  if (!Array.isArray(blocks)) return [];
  const seen = new Set();
  const result = [];
  blocks.forEach((raw, index) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    // 契约外/缺字段的条目：既没有 kind/type，也没有任何可展示内容时跳过（避免出现空块）。
    const hasKind = raw.kind !== undefined || raw.type !== undefined;
    const hasContent = typeof raw.text === 'string' || typeof raw.latex === 'string'
      || Boolean(raw.bbox) || Boolean(raw.norm);
    if (!hasKind && !hasContent) return;
    const kind = normalizeBlockKind(raw.kind || raw.type, raw);
    let id = raw.id === undefined || raw.id === null || raw.id === '' ? `block-${index + 1}` : raw.id;
    if (seen.has(id)) {
      let suffix = 2;
      while (seen.has(`${id}-${suffix}`)) suffix += 1;
      id = `${id}-${suffix}`;
    }
    seen.add(id);
    const text = typeof raw.text === 'string' ? raw.text : raw.text === undefined || raw.text === null ? '' : String(raw.text);
    const latexSource = typeof raw.latex === 'string' ? raw.latex : typeof raw.latex_input === 'string' ? raw.latex_input : '';
    const latexInput = kind === 'math' ? normalizeLatex(latexSource) : '';
    const confidence = readFinite(raw.confidence);
    result.push({
      id,
      kind,
      text,
      latexInput,
      // 初始「需校对」：原块标记需要，或公式块还没有 LaTeX。
      needsReview: Boolean(raw.needs_review) || (kind === 'math' && !latexInput),
      changed: false,
      original: raw,
      order: raw.order === undefined || raw.order === null ? index + 1 : raw.order,
      page: raw.page === undefined || raw.page === null ? 1 : raw.page,
      bbox: raw.bbox === undefined ? null : raw.bbox,
      norm: raw.norm === undefined ? null : raw.norm,
      confidence: confidence === null ? null : confidence,
      source: typeof raw.source === 'string' ? raw.source : 'rapidocr',
    });
  });
  return result;
}

/**
 * 把编辑态数组还原成 PATCH confirm 的提交体。
 * 提交体字段：`{id, kind, text, latex, order, page, bbox, norm, confidence, needs_review, source}`
 * （保留原块的其它字段，如 rotation/engine/engine_version）。
 * `needs_review`：公式块 LaTeX 为空 → true；用户改动过（编辑或标记已校对）→ false；
 * 否则沿用原块 `needs_review`。
 *
 * @param {Array<object>} stateBlocks parseBlocksWithFormulas 的返回值
 * @returns {Array<object>}
 */
export function buildConfirmBlocks(stateBlocks) {
  if (!Array.isArray(stateBlocks)) return [];
  const result = [];
  stateBlocks.forEach((state, index) => {
    if (!state || typeof state !== 'object') return;
    const original = state.original && typeof state.original === 'object' ? state.original : {};
    const kind = normalizeBlockKind(state.kind || original.kind, original);
    const latexInput = typeof state.latexInput === 'string' ? normalizeLatex(state.latexInput) : '';
    const rawLatex = kind === 'math'
      ? (latexInput || null)
      : (typeof original.latex === 'string' && original.latex.trim() ? original.latex.trim() : null);
    const latex = rawLatex ? rawLatex.slice(0, FORMULA_MAX_LENGTH) : null;
    const confidence = readFinite(state.confidence);
    let needsReview;
    if (kind === 'math' && !latex) needsReview = true;
    else if (state.changed) needsReview = false;
    else needsReview = Boolean(state.needsReview === undefined ? original.needs_review : state.needsReview);
    result.push({
      ...original,
      id: state.id === undefined || state.id === null ? `block-${index + 1}` : state.id,
      kind,
      text: (typeof state.text === 'string' ? state.text : '').slice(0, BLOCK_TEXT_MAX),
      latex,
      order: sanitizeBlockOrder(state.order === undefined || state.order === null ? index + 1 : state.order, index + 1),
      page: sanitizeBlockPage(state.page === undefined || state.page === null ? 1 : state.page),
      bbox: sanitizeBlockRect(state.bbox),
      norm: sanitizeBlockRect(state.norm),
      confidence: confidence === null ? 0 : confidence,
      needs_review: needsReview,
      source: state.source || original.source || 'rapidocr',
    });
  });
  return result;
}

/**
 * 识别块的中文定位说明：如「第 2 页 · 阅读顺序 5 · 原图位置 (120, 340) 240×36」。
 * bbox 优先（原图像素坐标）；无 bbox 时退 norm 百分比；都缺时返回「位置未提供」。
 *
 * @param {object} block
 * @returns {string}
 */
export function renderBlockLocation(block) {
  const source = block && typeof block === 'object' ? block : {};
  const parts = [];
  const page = readFinite(source.page);
  if (page !== null && page >= 1) parts.push(formulaText('study.formula.review.location.page', { page: Math.floor(page) }));
  const order = readFinite(source.order);
  if (order !== null && order >= 0) parts.push(formulaText('study.formula.review.location.order', { order }));
  const bbox = readBBoxRect(source.bbox);
  if (bbox) {
    parts.push(formulaText('study.formula.review.location.bbox', {
      x: readPointNumber(bbox.x),
      y: readPointNumber(bbox.y),
      w: readPointNumber(bbox.w),
      h: readPointNumber(bbox.h),
    }));
  } else {
    const norm = readNormRect(source.norm);
    if (norm) {
      parts.push(formulaText('study.formula.review.location.norm', {
        x: Math.round(norm.x * 1000) / 10,
        y: Math.round(norm.y * 1000) / 10,
      }));
    }
  }
  if (!parts.length) return formulaText('study.formula.review.location.unknown');
  return parts.join(' · ');
}

/* ------------------------------------------------------------------------- *
 * 原图叠加框：按 norm/bbox 在原图上画出识别块位置，点击回调块 id
 * ------------------------------------------------------------------------- */

function blockNormRect(block, natural) {
  const norm = readNormRect(block && block.norm);
  if (norm) return norm;
  const bbox = readBBoxRect(block && block.bbox);
  if (bbox && natural && natural.width > 0 && natural.height > 0) {
    return readNormRect({
      x: bbox.x / natural.width,
      y: bbox.y / natural.height,
      w: bbox.w / natural.width,
      h: bbox.h / natural.height,
    });
  }
  return null;
}

/**
 * 在原图上叠加识别块定位框。
 *
 * 约束：容器（containerEl）需 `position: relative`；若计算样式为 static，本函数会临时
 * 置为 relative 并在 destroy() 时还原。容器尺寸需与图片一致（调用方可包一层只为图片
 * 服务的定位容器，如 `.pg-block-preview`）。
 *
 * @param {HTMLImageElement} imgEl 已显示的原图
 * @param {HTMLElement} containerEl 图片容器（position: relative）
 * @param {Array<object>} blocks 识别块（含 norm 或 bbox/page/order）
 * @param {(id:any, block:object)=>void} onPick 点击/键盘选中框的回调
 * @returns {{element: HTMLElement|null, refresh: Function, setActive: Function, destroy: Function}}
 */
export function attachBlockOverlay(imgEl, containerEl, blocks, onPick) {
  const safeDestroy = () => {};
  if (!imgEl || !containerEl || typeof containerEl.append !== 'function') {
    return { element: null, refresh: safeDestroy, setActive: safeDestroy, destroy: safeDestroy };
  }
  const list = Array.isArray(blocks) ? blocks.filter(item => item && typeof item === 'object') : [];
  const overlay = el('div', { class: 'block-overlay' });
  containerEl.append(overlay);
  let destroyed = false;
  let activeId = null;
  let boxes = [];
  let imageNatural = { width: 0, height: 0 };
  let changedPosition = false;
  let previousPosition = '';
  let resizeObserver = null;

  const computed = typeof window !== 'undefined' && window.getComputedStyle ? window.getComputedStyle(containerEl) : null;
  if (computed && computed.position === 'static') {
    previousPosition = containerEl.style.position;
    containerEl.style.position = 'relative';
    changedPosition = true;
  }

  function updateGeometry() {
    if (destroyed) return;
    const imageRect = imgEl.getBoundingClientRect();
    const containerRect = containerEl.getBoundingClientRect();
    const left = imageRect.left - containerRect.left - (containerEl.clientLeft || 0);
    const top = imageRect.top - containerRect.top - (containerEl.clientTop || 0);
    overlay.style.left = `${left}px`;
    overlay.style.top = `${top}px`;
    overlay.style.width = `${imageRect.width}px`;
    overlay.style.height = `${imageRect.height}px`;
  }

  function renderBoxes() {
    if (destroyed) return;
    overlay.replaceChildren();
    boxes = [];
    list.forEach((block, index) => {
      const rect = blockNormRect(block, imageNatural);
      if (!rect) return;
      const kind = normalizeBlockKind(block.kind || block.type, block);
      const box = el('div', {
        class: `block-overlay-box kind-${kind}`,
        role: 'button',
        tabindex: '0',
        'data-block-id': String(block.id === undefined || block.id === null ? index + 1 : block.id),
        'aria-label': formulaText('study.formula.review.overlayAria', {
          id: block.id === undefined || block.id === null ? index + 1 : block.id,
          location: renderBlockLocation(block),
        }),
        title: `${kindLabel(kind)} · ${renderBlockLocation(block)}`,
        style: `left:${(rect.x * 100).toFixed(3)}%;top:${(rect.y * 100).toFixed(3)}%;`
          + `width:${(rect.w * 100).toFixed(3)}%;height:${(rect.h * 100).toFixed(3)}%;`,
      });
      if (block.needs_review || block.needsReview) box.classList.add('needs-review');
      if (kind === 'math') box.classList.add('is-formula');
      const number = readFinite(block.order);
      box.append(el('span', { class: 'block-overlay-label', 'aria-hidden': 'true' },
        String(number === null ? index + 1 : number)));
      const pick = event => {
        if (event) event.stopPropagation();
        setActive(block.id === undefined || block.id === null ? index + 1 : block.id);
        if (typeof onPick === 'function') onPick(block.id === undefined || block.id === null ? index + 1 : block.id, block);
      };
      box.addEventListener('click', pick);
      box.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pick(event);
        }
      });
      if (activeId !== null && String(activeId) === String(block.id)) box.classList.add('is-active');
      boxes.push({ id: block.id, node: box });
      overlay.append(box);
    });
    overlay.hidden = boxes.length === 0;
  }

  function refresh(nextBlocks) {
    if (destroyed) return;
    if (Array.isArray(nextBlocks)) {
      list.length = 0;
      nextBlocks.filter(item => item && typeof item === 'object').forEach(item => list.push(item));
    }
    imageNatural = { width: imgEl.naturalWidth || 0, height: imgEl.naturalHeight || 0 };
    updateGeometry();
    renderBoxes();
  }

  function setActive(id) {
    activeId = id;
    boxes.forEach(entry => {
      const isActive = id !== null && String(entry.id) === String(id);
      entry.node.classList.toggle('is-active', isActive);
    });
  }

  function handleLoad() {
    refresh();
  }

  function handleResize() {
    if (!destroyed) updateGeometry();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    imgEl.removeEventListener('load', handleLoad);
    if (resizeObserver) {
      try { resizeObserver.disconnect(); } catch { /* 忽略 */ }
      resizeObserver = null;
    }
    if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') {
      window.removeEventListener('resize', handleResize);
    }
    overlay.remove();
    if (changedPosition) containerEl.style.position = previousPosition;
  }

  imgEl.addEventListener('load', handleLoad);
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('resize', handleResize);
  }
  if (typeof window !== 'undefined' && typeof window.ResizeObserver === 'function') {
    try {
      resizeObserver = new window.ResizeObserver(handleResize);
      resizeObserver.observe(imgEl);
      resizeObserver.observe(containerEl);
    } catch {
      resizeObserver = null;
    }
  }
  refresh();

  return { element: overlay, refresh, setActive, destroy };
}

/* ------------------------------------------------------------------------- *
 * 工具栏模板（点击后插入光标处，光标落在第一个 `{}` 内）
 * ------------------------------------------------------------------------- */

const TOOLBAR_TOOLS = [
  { id: 'power', label: '^', template: '^{}', textKey: 'study.formula.tool.power' },
  { id: 'subscript', label: '_', template: '_{}', textKey: 'study.formula.tool.subscript' },
  { id: 'frac', label: '\\frac', template: '\\frac{}{}', textKey: 'study.formula.tool.frac', wide: true },
  { id: 'sqrt', label: '\\sqrt', template: '\\sqrt{}', textKey: 'study.formula.tool.sqrt', wide: true },
  { id: 'pi', label: 'π', template: '\\pi ', textKey: 'study.formula.tool.pi' },
  { id: 'le', label: '≤', template: '\\le ', textKey: 'study.formula.tool.le' },
  { id: 'ge', label: '≥', template: '\\ge ', textKey: 'study.formula.tool.ge' },
  { id: 'ne', label: '≠', template: '\\ne ', textKey: 'study.formula.tool.ne' },
  { id: 'times', label: '×', template: '\\times ', textKey: 'study.formula.tool.times' },
  { id: 'div', label: '÷', template: '\\div ', textKey: 'study.formula.tool.div' },
  { id: 'degree', label: '°', template: '^{\\circ}', textKey: 'study.formula.tool.degree' },
  { id: 'integral', label: '∫', template: '\\int_{}^{} ', textKey: 'study.formula.tool.integral' },
  { id: 'sum', label: '∑', template: '\\sum_{}^{} ', textKey: 'study.formula.tool.sum' },
  { id: 'triangle', label: '△', template: '\\triangle ', textKey: 'study.formula.tool.triangle' },
  { id: 'pm', label: '±', template: '\\pm ', textKey: 'study.formula.tool.pm' },
  { id: 'angle', label: '∠', template: '\\angle ', textKey: 'study.formula.tool.angle' },
  { id: 'infinity', label: '∞', template: '\\infty ', textKey: 'study.formula.tool.infinity' },
  { id: 'arrow', label: '⇒', template: '\\Rightarrow ', textKey: 'study.formula.tool.arrow' },
];

/** 工具栏模板的第一个 `{}` 内作为光标落点；无 `{}` 时落在模板末尾。 */
function templateCaretOffset(template) {
  const index = template.indexOf('{}');
  return index < 0 ? template.length : index + 1;
}

let formulaInputSequence = 0;

/**
 * 创建公式输入组件。
 *
 * @param {object} [options]
 * @param {string} [options.value] 初始 LaTeX（不含定界符；传入 `$...$` 会自动剥离）
 * @param {string} [options.placeholder]
 * @param {string} [options.ariaLabel]
 * @param {(value: string) => void} [options.onChange] Ctrl/Cmd+Enter 或失焦且内容变化时回调
 * @param {number} [options.rows] 编辑框行数（默认 3）
 * @param {boolean} [options.allowPlainFallback] 无 KaTeX / 渲染失败时是否显示纯文本回退（默认 true）
 * @returns {{element: HTMLElement, getValue: Function, setValue: Function, focus: Function,
 *            destroy: Function, validate: Function, textarea: HTMLTextAreaElement,
 *            preview: HTMLElement, toolbar: HTMLElement}}
 */
export function createFormulaInput(options = {}) {
  const settings = options && typeof options === 'object' ? options : {};
  const allowPlainFallback = settings.allowPlainFallback !== false;
  const rowCount = clampNumber(Math.round(Number(settings.rows) || 3), 1, 12);
  const instanceId = `formula-input-${++formulaInputSequence}`;
  const hintId = `${instanceId}-hint`;

  const textarea = el('textarea', {
    class: 'formula-input-editor',
    id: instanceId,
    rows: String(rowCount),
    spellcheck: 'false',
    autocomplete: 'off',
    autocapitalize: 'off',
    'aria-label': settings.ariaLabel || formulaText('study.formula.editor.aria'),
    placeholder: settings.placeholder || formulaText('study.formula.editor.placeholder'),
  });
  textarea.value = normalizeLatex(settings.value);

  const toolbar = el('div', {
    class: 'formula-toolbar',
    role: 'toolbar',
    'aria-label': formulaText('study.formula.toolbar.aria'),
  });
  for (const tool of TOOLBAR_TOOLS) {
    const toolLabel = formulaText(tool.textKey);
    const node = el('button', {
      type: 'button',
      class: `formula-tool${tool.wide ? ' is-wide' : ''}`,
      title: toolLabel,
      'aria-label': toolLabel,
      'data-tool': tool.id,
    }, tool.label);
    // 点击工具栏不抢编辑框焦点（触屏/鼠标都需要）。
    node.addEventListener('pointerdown', event => event.preventDefault());
    node.addEventListener('mousedown', event => event.preventDefault());
    node.addEventListener('click', () => {
      insertTemplate(tool.template);
      textarea.focus();
    });
    toolbar.append(node);
  }

  const counterNode = el('span', { class: 'formula-hint-counter' });
  const warningNode = el('span', { class: 'formula-hint-warning', role: 'status', hidden: true });
  const hintNode = el('div', { class: 'formula-hint', id: hintId },
    counterNode,
    el('span', { class: 'formula-hint-item' }, formulaText('study.formula.hint.tab')),
    el('span', { class: 'formula-hint-item' }, formulaText('study.formula.hint.escape')),
    allowPlainFallback ? el('span', { class: 'formula-hint-item' }, formulaText('study.formula.hint.plainFallback')) : null,
    warningNode);
  textarea.setAttribute('aria-describedby', hintId);

  const previewBody = el('div', { class: 'formula-preview-body' });
  const preview = el('div', { class: 'formula-preview', 'aria-live': 'polite', 'aria-atomic': 'true' },
    el('span', { class: 'formula-preview-title' }, formulaText('study.formula.preview.title')),
    previewBody);

  const element = el('div', { class: 'formula-input' },
    toolbar,
    el('div', { class: 'formula-input-editor-wrap' }, textarea),
    preview,
    hintNode);

  let destroyed = false;
  let composing = false;
  let lastEmitted = normalizeLatex(settings.value);
  let warnedOverflow = false;

  function showOverflowWarning() {
    if (warnedOverflow) return;
    warnedOverflow = true;
    warningNode.textContent = formulaText('study.formula.hint.overflow', { max: FORMULA_MAX_LENGTH });
    warningNode.hidden = false;
  }

  function truncateValue(value) {
    const text = String(value === undefined || value === null ? '' : value);
    if (text.length <= FORMULA_MAX_LENGTH) return text;
    showOverflowWarning();
    return text.slice(0, FORMULA_MAX_LENGTH);
  }

  function currentValue() {
    return normalizeLatex(textarea.value);
  }

  function updateCounter() {
    const count = textarea.value.length;
    counterNode.textContent = formulaText('study.formula.hint.count', { count, max: FORMULA_MAX_LENGTH });
    if (warnedOverflow && count < FORMULA_MAX_LENGTH) {
      warnedOverflow = false;
      warningNode.hidden = true;
      warningNode.textContent = '';
    }
  }

  function renderPlainFallback(latex) {
    const plain = toPlainText(latex);
    if (plain) {
      previewBody.append(el('p', { class: 'formula-preview-plain' },
        formulaText('study.formula.preview.plainLabel', { text: plain })));
    }
  }

  function renderPreviewFallback(latex, { katexMissing = false } = {}) {
    preview.classList.add('is-error');
    previewBody.append(el('p', { class: 'formula-preview-error' }, formulaText('study.formula.preview.error')));
    if (katexMissing) {
      previewBody.append(el('p', { class: 'formula-preview-note' }, formulaText('study.formula.preview.katexMissing')));
    }
    if (allowPlainFallback) renderPlainFallback(latex);
  }

  function renderPreview() {
    if (destroyed) return;
    const latex = currentValue();
    previewBody.replaceChildren();
    preview.classList.remove('is-error', 'is-empty');
    if (!latex) {
      preview.classList.add('is-empty');
      previewBody.append(el('span', { class: 'formula-preview-placeholder' }, formulaText('study.formula.preview.empty')));
      return;
    }
    const katex = typeof window !== 'undefined' ? window.katex : null;
    if (!katex || typeof katex.renderToString !== 'function') {
      renderPreviewFallback(latex, { katexMissing: true });
      return;
    }
    try {
      const html = renderMath(latex, false, { throwOnError: true });
      if (!html || html.includes('katex-error') || html.includes('math-fallback')) throw new Error('katex-fallback');
      const safeHtml = sanitizeMath(html);
      const template = document.createElement('template');
      template.innerHTML = safeHtml;
      if (!template.content.childNodes.length) throw new Error('empty-render');
      previewBody.append(template.content);
    } catch {
      renderPreviewFallback(latex, { katexMissing: false });
    }
  }

  function emitChange({ force = false } = {}) {
    if (destroyed) return;
    const value = currentValue();
    if (!force && value === lastEmitted) return;
    lastEmitted = value;
    if (typeof settings.onChange === 'function') {
      try {
        settings.onChange(value);
      } catch {
        /* 调用方回调异常不应打断输入（错误由调用方自行处理）。 */
      }
    }
  }

  function insertTemplate(template) {
    if (destroyed) return;
    const source = textarea.value;
    let start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : source.length;
    let end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : start;
    start = clampNumber(start, 0, source.length);
    end = clampNumber(end, start, source.length);
    const next = truncateValue(`${source.slice(0, start)}${template}${source.slice(end)}`);
    textarea.value = next;
    const caret = clampNumber(start + templateCaretOffset(template), 0, next.length);
    try {
      textarea.setSelectionRange(caret, caret);
    } catch {
      /* 个别环境不支持选区：退化为末尾光标。 */
    }
    updateCounter();
    renderPreview();
  }

  function handleInput(event) {
    // 中文/日文输入法组合期间不截断、不重绘预览，等 compositionend 再统一处理。
    if (composing || (event && event.isComposing)) {
      updateCounter();
      return;
    }
    const limited = truncateValue(textarea.value);
    if (limited !== textarea.value) {
      const rawCaret = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : limited.length;
      const caret = clampNumber(rawCaret, 0, limited.length);
      textarea.value = limited;
      try { textarea.setSelectionRange(caret, caret); } catch { /* 忽略 */ }
    }
    updateCounter();
    renderPreview();
  }

  function handlePaste(event) {
    const clipboard = event.clipboardData || window.clipboardData;
    if (!clipboard || typeof clipboard.getData !== 'function') return;
    const pasted = clipboard.getData('text/plain');
    if (!pasted) return;
    // 多行粘贴保留换行；仅当整段被 $...$ / $$...$$ / \(...\) 包裹时去定界符。
    const unwrapped = normalizeLatex(pasted);
    if (unwrapped !== pasted.trim()) {
      event.preventDefault();
      insertTemplate(unwrapped);
    }
  }

  function handleKeydown(event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      // 明确要求：Ctrl/Cmd+Enter 一定触发 onChange（即使内容未变）并失焦。
      emitChange({ force: true });
      textarea.blur();
      return;
    }
    if (event.key === 'Tab' && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      insertTemplate('  ');
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      textarea.blur();
    }
  }

  function handleCompositionStart() {
    composing = true;
  }

  function handleCompositionEnd() {
    composing = false;
    handleInput(null);
  }

  textarea.addEventListener('input', handleInput);
  textarea.addEventListener('paste', handlePaste);
  textarea.addEventListener('keydown', handleKeydown);
  textarea.addEventListener('blur', () => emitChange());
  textarea.addEventListener('compositionstart', handleCompositionStart);
  textarea.addEventListener('compositionend', handleCompositionEnd);

  function getValue() {
    return currentValue();
  }

  function setValue(value) {
    if (destroyed) return;
    const normalized = truncateValue(normalizeLatex(value));
    textarea.value = normalized;
    lastEmitted = normalized;
    updateCounter();
    renderPreview();
  }

  function focus() {
    if (!destroyed) textarea.focus();
  }

  function validate() {
    const value = currentValue();
    if (!value) return { ok: false, message: formulaText('study.formula.validate.empty') };
    const balance = checkBraceBalance(value);
    if (!balance.ok) return { ok: false, message: formulaText('study.formula.validate.braces') };
    return { ok: true };
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (element.isConnected) element.remove();
  }

  updateCounter();
  renderPreview();

  return { element, getValue, setValue, focus, destroy, validate, textarea, preview, toolbar };
}
