import { el } from './dom.js';
import { renderMindmapWidget } from '../components/mindmap.js';

let mdInstance = null;

function getMarkdownIt() {
  if (mdInstance) return mdInstance;
  if (typeof window.markdownit === 'function') {
    mdInstance = window.markdownit({
      html: true,
      linkify: true,
      breaks: true,
      typographer: false,
    });
    const defaultLinkOpen = mdInstance.renderer.rules.link_open
      || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options));
    mdInstance.renderer.rules.link_open = (tokens, idx, options, env, self) => {
      tokens[idx].attrSet('target', '_blank');
      tokens[idx].attrSet('rel', 'noopener noreferrer');
      return defaultLinkOpen(tokens, idx, options, env, self);
    };
  }
  return mdInstance;
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 渲染一段 LaTeX。默认 throwOnError:false（Markdown 行内公式保持红色可读回退），
 * 内容块渲染传 { throwOnError: true } 以便调用方接管失败回退（例如退回原卷公式图）。
 */
function renderLatex(tex, displayMode, { throwOnError = false } = {}) {
  if (window.katex && typeof window.katex.renderToString === 'function') {
    try {
      return window.katex.renderToString(tex.trim(), {
        displayMode,
        throwOnError,
        strict: 'ignore',
        output: 'htmlAndMathml',
      });
    } catch (error) {
      if (throwOnError) throw error;
      /* fallback below */
    }
  }
  const esc = escapeHtml(tex);
  return displayMode ? `<pre class="math-fallback">$$${esc}$$</pre>` : `<code class="math-fallback">$${esc}$</code>`;
}

/**
 * 渲染一段 LaTeX 为 HTML 字符串（KaTeX 优先，KaTeX 缺失/异常时退化为转义后的可读占位）。
 * 供内容块（Block）渲染复用；返回值未经 DOMPurify 处理，插入页面前请配合 sanitizeMath。
 *
 * @param {string} tex       LaTeX 源码（契约 §3：仅 data 侧通过语法检查的 latex 才会下发）
 * @param {boolean} displayMode 是否独立公式（默认 false，行内公式）
 * @param {{throwOnError?: boolean}} options 内容块可传 throwOnError:true 由调用方回退原卷公式图
 * @returns {string} HTML 字符串；KaTeX 不可用时含 `math-fallback` 类
 */
export function renderMath(tex, displayMode = false, options = {}) {
  return renderLatex(String(tex ?? ''), Boolean(displayMode), options || {});
}

/* ------------------------------------------------------------------------- *
 * 两套净化配置：用户 Markdown 与 KaTeX MathML 严格分离。
 * - 用户 Markdown：禁止 style 属性、禁止数学/表单/脚本标签，仅白名单保留辅助标签；
 * - KaTeX MathML：仅用于渲染由本地 KaTeX 生成的公式标记，才允许 math 标签与排版属性。
 * ------------------------------------------------------------------------- */

/** KaTeX/MathML 需要的标签与属性白名单（仅由 renderMath 生成的内容使用）。 */
export const MATH_SANITIZE_OPTIONS = {
  ADD_TAGS: ['details', 'summary', 'math', 'semantics', 'mrow', 'mi', 'mn', 'mo', 'msup', 'msub', 'msubsup',
    'mfrac', 'msqrt', 'mroot', 'mtable', 'mtr', 'mtd', 'mtext', 'mspace', 'mover', 'munder', 'munderover',
    'mpadded', 'menclose', 'annotation', 'annotation-xml'],
  ADD_ATTR: ['target', 'rel', 'open', 'aria-hidden', 'xmlns', 'encoding', 'mathvariant', 'stretchy', 'fence',
    'separator', 'lspace', 'rspace', 'columnalign', 'rowspacing', 'columnspacing', 'style'],
  FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form'],
  FORBID_ATTR: ['onerror', 'onload', 'onclick'],
};

/** 用户 Markdown 的净化配置：不放行 style 与数学标签，只保留 details/summary 等辅助标签。 */
export const MARKDOWN_SANITIZE_OPTIONS = {
  ADD_TAGS: ['details', 'summary', 'mark', 'kbd', 'del', 'ins', 'sub', 'sup'],
  ADD_ATTR: ['target', 'rel', 'class', 'title', 'open', 'align', 'colspan', 'rowspan', 'start', 'checked', 'disabled'],
  FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'textarea', 'select',
    'option', 'link', 'meta', 'base', 'math', 'semantics', 'annotation', 'annotation-xml', 'svg', 'canvas',
    'audio', 'video', 'source'],
  FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick'],
  ALLOW_DATA_ATTR: false,
};

function purifierReady() {
  const purify = window.DOMPurify;
  return Boolean(purify && purify.isSupported !== false && typeof purify.sanitize === 'function');
}

function sanitizeOrThrow(html, options, label) {
  if (!purifierReady()) {
    throw new Error(`安全过滤器 DOMPurify 不可用，已拒绝渲染${label}`);
  }
  const cleaned = window.DOMPurify.sanitize(html, options);
  if (typeof cleaned !== 'string') {
    throw new Error(`安全过滤器未返回有效${label}结果`);
  }
  return cleaned;
}

/**
 * 公式 HTML 的安全化（KaTeX MathML 专用白名单）。
 * fail-closed：DOMPurify 缺失或异常时抛错，由调用方回退到原卷公式图/可读文本，
 * 绝不把未净化的 KaTeX MathML 直接写入 innerHTML。
 */
export function sanitizeMath(html) {
  return sanitizeOrThrow(String(html ?? ''), MATH_SANITIZE_OPTIONS, '公式');
}

/**
 * 用户 Markdown 的安全化（禁止 style、禁止数学/脚本标签）。
 * fail-closed：DOMPurify 缺失或异常时抛错，markdown() 会降级为纯文本 <pre>。
 */
export function sanitizeMarkdown(html) {
  return sanitizeOrThrow(String(html ?? ''), MARKDOWN_SANITIZE_OPTIONS, 'Markdown');
}

/** 表格结构 HTML 的安全化：仅保留表格标签与 rowspan/colspan（OCR 后处理预览用）。 */
export function sanitizeTableHtml(html) {
  return sanitizeOrThrow(String(html ?? ''), {
    ALLOWED_TAGS: ['table', 'thead', 'tbody', 'tr', 'td', 'th'],
    ALLOWED_ATTR: ['rowspan', 'colspan'],
    ALLOW_DATA_ATTR: false,
  }, '表格结构');
}

/** 调试/验收用：当前页面是否具备可用的 HTML 净化器。 */
export function sanitizerAvailable() {
  return purifierReady();
}

// 公式占位：stash 返回 token，渲染结果单独保存，最终按需用 MathML 白名单净化。
function preprocessMath(raw) {
  const placeholders = [];
  const stash = (html) => {
    const token = `@@MATH_TOKEN_${placeholders.length}@@`;
    placeholders.push(html);
    return token;
  };

  // Preserve fenced code blocks & inline code first so $ inside code is untouched
  const codeStash = [];
  let text = String(raw || '').replace(/```[\s\S]*?```|`[^`\n]+`/g, match => {
    const tok = `@@CODE_TOKEN_${codeStash.length}@@`;
    codeStash.push(match);
    return tok;
  });

  // Display math: $$...$$ and \[...\]
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => stash(`<div class="math-block">${renderLatex(expr, true)}</div>`));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_, expr) => stash(`<div class="math-block">${renderLatex(expr, true)}</div>`));

  // Inline math: $...$ (avoid matching prices like $100 followed by space/punctuation or multi-line spans)
  text = text.replace(/(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$/g, (_, expr) => {
    if (/^\d+(?:\.\d+)?$/.test(expr.trim())) return `$${expr}$`;
    return stash(`<span class="math-inline">${renderLatex(expr, false)}</span>`);
  });
  text = text.replace(/\\\((.+?)\\\)/g, (_, expr) => stash(`<span class="math-inline">${renderLatex(expr, false)}</span>`));

  // Restore code blocks before markdown-it parses them
  text = text.replace(/@@CODE_TOKEN_(\d+)@@/g, (_, i) => codeStash[Number(i)]);
  return { text, placeholders };
}

function decorateCitations(html) {
  return html.replace(/\[D(\d+):([^\]\s]+)\]/g, '<span class="cite-chip" title="来源引用">$&</span>')
    .replace(/\[官方\]/g, '<span class="origin-tag official">官方</span>')
    .replace(/\[我的补充\]/g, '<span class="origin-tag supplement">我的补充</span>')
    .replace(/\[课标:([^\]]+)\]/g, '<span class="origin-tag official">课标 · $1</span>')
    .replace(/\[资料特有\]/g, '<span class="origin-tag supplement">资料特有</span>');
}

function parseTaskList(node) {
  for (const li of node.querySelectorAll('li')) {
    const txt = li.firstChild?.nodeType === 3 ? li.firstChild.textContent : '';
    if (/^\[[ xX]\]\s/.test(txt)) {
      const checked = /^\[[xX]\]/.test(txt);
      li.firstChild.textContent = txt.slice(4);
      li.prepend(el('input', { type: 'checkbox', checked, disabled: true, class: 'md-task-box' }));
      li.classList.add('md-task-item');
    }
  }
}

/**
 * 渲染 Markdown（AI 结果、笔记、讲解卡片等所有用户可见 Markdown 的唯一入口）。
 * fail-closed：DOMPurify 缺失或净化异常时不写 innerHTML，转为纯文本 <pre> 展示原文。
 */
export function markdown(text) {
  const node = el('div', { class: 'markdown' });
  const raw = String(text || '');
  if (!raw.trim()) return node;

  const md = getMarkdownIt();
  if (!md) {
    // vendor 脚本未加载时同样 fail-closed：纯文本，不解析 HTML。
    const pre = el('pre', { class: 'markdown-fallback', style: 'white-space:pre-wrap' });
    pre.textContent = raw;
    node.append(pre);
    return node;
  }

  try {
    const { text: prepped, placeholders } = preprocessMath(raw);
    let html = md.render(prepped);
    html = decorateCitations(html);
    // 先按“用户 Markdown”白名单净化（此时公式占位仍是纯文本 token），再逐条替换为
    // 经过 MathML 白名单净化的 KaTeX 片段，避免把数学标签混入用户 Markdown 放行范围。
    html = sanitizeMarkdown(html);
    html = html.replace(/@@MATH_TOKEN_(\d+)@@/g, (_, i) => sanitizeMathPlaceholder(placeholders[Number(i)]));
    node.innerHTML = html;
  } catch (error) {
    node.replaceChildren(el('pre', { class: 'markdown-fallback', style: 'white-space:pre-wrap' }, raw));
    return node;
  }

  // Wrap tables in scrollable container
  for (const tbl of node.querySelectorAll('table')) {
    if (tbl.parentElement?.classList.contains('table-wrap')) continue;
    const wrap = el('div', { class: 'table-wrap' });
    tbl.replaceWith(wrap);
    wrap.append(tbl);
  }
  parseTaskList(node);
  // Render ```mermaid mindmap ...``` blocks into interactive inline SVG mindmaps (§7.4)
  for (const codeEl of node.querySelectorAll('pre > code.language-mermaid')) {
    const src = codeEl.textContent || '';
    if (/^\s*mindmap\b/.test(src)) {
      const pre = codeEl.parentElement;
      pre.replaceWith(renderMindmapWidget(src, { title: '知识思维导图' }));
    }
  }
  return node;
}

function sanitizeMathPlaceholder(html) {
  try {
    return sanitizeMath(html);
  } catch {
    // 单个公式净化失败时只丢这一个公式，不影响整段 Markdown 的可读文本。
    return '<code class="math-fallback">[公式渲染不可用]</code>';
  }
}
