/**
 * Mafs 兼容函数图（原生 SVG，无构建、无第三方依赖、无 WebGL）。
 *
 * 方案 §5.4「Mafs 与 Excalidraw」：函数图保存函数类型、参数、范围与说明，表达式走有限
 * 运算；题目、错题、笔记与课堂共用同一份版本化学习附件（kind=`function_graph`）。
 * 本组件是主站的 Mafs 兼容替代实现：数据模型与 Mafs 场景同构（expressions / params /
 * xRange / yRange / points / labels），渲染用原生 SVG，不引入 React、不联网、不装依赖。
 *
 * 安全与正确性：
 * - 表达式求值**不使用 eval / new Function**：tokenize → 递归下降 AST → 逐点求值，
 *   只允许 数字、x、参数名、+ - * / ^、括号、sin/cos/tan/sqrt/abs/exp/ln/log、pi、e；
 *   非法/未知标识返回中文错误并标红输入，不绘制错误曲线。
 * - 所有外部数据（附件 data / 标题 / 题目编号）只走 textContent 与属性赋值，不拼 innerHTML。
 * - SVG 只通过 createElementNS 构建；`^` 右结合，`-`/`+` 为一元运算符。
 *
 * 对外 API（题目页 / 错题页共用）：
 *   createFunctionGraph(options) -> { element, getDefinition(), setDefinition(def), destroy(), redraw() }
 *     options = { definition?, readOnly?, ariaLabel?, onChange? }
 *   openFunctionGraph({ ref, artifact, readOnly, onSaved, trigger, titleHint }) -> controller
 *     controller = { element, dialog, graph, getDefinition(), setDefinition(), getArtifact(),
 *                    close(), destroy() }
 *   openFunctionGraphForTarget({ ref, trigger, titleHint?, fallbackItems?, readOnly?, onSaved? })
 *     -> Promise<controller | null>：先 GET /api/artifacts/by-target 探测已保存附件，
 *     探测失败（接口未上线/弱网/超时）时用 fallbackItems 或默认定义兜底，不阻塞打开。
 *   functionGraphEligible(question) -> boolean：数学题或含公式的题目才显示入口。
 *
 * 保存（POST /api/artifacts；已有附件时 PATCH 新版本）：
 *   kind='function_graph'，data=当前定义，preview={kind:'function-graph',...}，
 *   links=[{target_kind:'question', target_key:ref}]；失败/409 都按服务端中文原因提示，
 *   绝不伪造成功。
 */

import { blockList, busy, button, el, icon, query, toast } from '../core/dom.js';
import { api } from '../core/api.js';
import { t } from '../core/i18n.js';

/* ------------------------------------------------------------------ i18n */

/**
 * 取新 UI 文案：locale JSON 由 Lead 统一合并，缺键时回退到中文常量（不写死到页面 HTML）。
 * 兼容 `{v1}` 占位符（t() 命中键时由 i18n 插值，回退时在这里插值）。
 */
function toolText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

/* ------------------------------------------------------- 表达式：词法/语法/求值 */

/**
 * 表达式文法（有限运算，与方案 §5.4「表达式走有限运算」一致）：
 *   expression := term (('+' | '-') term)*
 *   term       := unary (('*' | '/') unary | implicit-multiply)*
 *   unary      := ('-' | '+') unary | power
 *   power      := primary ('^' unary)?        // 右结合：2^3^2 = 2^(3^2)
 *   primary    := number | 'x' | ident '(' expression ')' | constant | param | '(' expression ')'
 * 额外容忍隐式乘法：2x、3(x+1)、(x+1)(x-1)、2pi。
 */

const FUNCTIONS = Object.freeze({
  sin: Math.sin,
  cos: Math.cos,
  tan: Math.tan,
  sqrt: Math.sqrt,
  abs: Math.abs,
  exp: Math.exp,
  ln: Math.log,
  log: Math.log10,
});

const CONSTANTS = Object.freeze({ pi: Math.PI, e: Math.E });

class ExpressionError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ExpressionError';
  }
}

const PARSE_ERROR_TEXT = '表达式语法有误，请检查';

function parseError() {
  return new ExpressionError(toolText('study.tools.graph.parseError', PARSE_ERROR_TEXT));
}

function tokenize(source) {
  const text = String(source ?? '');
  const tokens = [];
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (/\s/.test(char)) {
      index += 1;
      continue;
    }
    if (/[0-9]/.test(char) || (char === '.' && /[0-9]/.test(text[index + 1] || ''))) {
      let end = index;
      while (end < text.length && /[0-9.]/.test(text[end])) end += 1;
      const raw = text.slice(index, end);
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        throw parseError();
      }
      tokens.push({ type: 'number', value, pos: index });
      index = end;
      continue;
    }
    if (/[A-Za-z_]/.test(char)) {
      let end = index + 1;
      while (end < text.length && /[A-Za-z0-9_]/.test(text[end])) end += 1;
      tokens.push({ type: 'ident', name: text.slice(index, end), pos: index });
      index = end;
      continue;
    }
    if ('+-*/^'.includes(char)) {
      tokens.push({ type: 'op', value: char, pos: index });
      index += 1;
      continue;
    }
    if (char === '(' || char === ')') {
      tokens.push({ type: char, pos: index });
      index += 1;
      continue;
    }
    throw new ExpressionError(toolText(
      'study.tools.graph.badChar',
      '表达式第 {v1} 个字符「{v2}」无法识别',
      { v1: index + 1, v2: char },
    ));
  }
  return tokens;
}

class Parser {
  constructor(tokens, paramNames) {
    this.tokens = tokens;
    this.index = 0;
    this.params = paramNames;
  }

  peek() {
    return this.tokens[this.index];
  }

  next() {
    const token = this.tokens[this.index];
    this.index += 1;
    return token;
  }

  parse() {
    const node = this.parseExpression();
    if (this.index < this.tokens.length) {
      throw new ExpressionError(toolText(
        'study.tools.graph.unexpectedToken',
        '表达式在「{v1}」附近无法解析',
        { v1: this.describeToken(this.tokens[this.index]) },
      ));
    }
    return node;
  }

  describeToken(token) {
    if (!token) return '';
    if (token.type === 'ident') return token.name;
    if (token.type === 'number') return String(token.value);
    if (token.type === 'op') return token.value;
    return token.type;
  }

  parseExpression() {
    let node = this.parseTerm();
    for (;;) {
      const token = this.peek();
      if (!token || token.type !== 'op' || (token.value !== '+' && token.value !== '-')) return node;
      this.next();
      node = { type: 'binary', op: token.value, left: node, right: this.parseTerm() };
    }
  }

  parseTerm() {
    let node = this.parseUnary();
    for (;;) {
      const token = this.peek();
      if (token && token.type === 'op' && (token.value === '*' || token.value === '/')) {
        this.next();
        node = { type: 'binary', op: token.value, left: node, right: this.parseUnary() };
        continue;
      }
      // 隐式乘法：primary/')' 之后紧跟数字、标识符或左括号（2x、3(x+1)、(x+1)(x-1)）。
      if (token && (token.type === 'number' || token.type === 'ident' || token.type === '(')) {
        node = { type: 'binary', op: '*', left: node, right: this.parseUnary() };
        continue;
      }
      return node;
    }
  }

  parseUnary() {
    const token = this.peek();
    if (token && token.type === 'op' && (token.value === '-' || token.value === '+')) {
      this.next();
      const operand = this.parseUnary();
      return token.value === '-' ? { type: 'negate', operand } : operand;
    }
    return this.parsePower();
  }

  parsePower() {
    const base = this.parsePrimary();
    const token = this.peek();
    if (token && token.type === 'op' && token.value === '^') {
      this.next();
      // 指数侧允许一元负号且右结合：x^-2、2^3^2。
      const exponent = this.parseUnary();
      return { type: 'binary', op: '^', left: base, right: exponent };
    }
    return base;
  }

  expectCloseParen() {
    const token = this.peek();
    if (!token || token.type !== ')') {
      throw new ExpressionError(toolText(
        'study.tools.graph.unbalanced',
        '括号不匹配，请检查左右括号是否成对',
      ));
    }
    this.next();
  }

  parsePrimary() {
    const token = this.next();
    if (!token) {
      throw new ExpressionError(toolText(
        'study.tools.graph.unexpectedEnd',
        '表达式不完整，末尾缺少内容',
      ));
    }
    if (token.type === 'number') return { type: 'number', value: token.value };
    if (token.type === '(') {
      const node = this.parseExpression();
      this.expectCloseParen();
      return node;
    }
    if (token.type === 'ident') {
      const name = token.name;
      const next = this.peek();
      if (next && next.type === '(') {
        if (!Object.prototype.hasOwnProperty.call(FUNCTIONS, name)) {
          throw new ExpressionError(toolText(
            'study.tools.graph.unknownFunction',
            '未知函数「{v1}」，支持 sin/cos/tan/sqrt/abs/exp/ln/log',
            { v1: name },
          ));
        }
        this.next();
        const argument = this.parseExpression();
        this.expectCloseParen();
        return { type: 'call', name, argument };
      }
      if (name === 'x') return { type: 'variable', name: 'x' };
      if (Object.prototype.hasOwnProperty.call(CONSTANTS, name)) return { type: 'constant', name };
      if (this.params.has(name)) return { type: 'param', name };
      if (Object.prototype.hasOwnProperty.call(FUNCTIONS, name)) {
        throw new ExpressionError(toolText(
          'study.tools.graph.functionNeedsParen',
          '函数「{v1}」需要写成 {v1}(...)',
          { v1: name },
        ));
      }
      throw new ExpressionError(toolText(
        'study.tools.graph.unknownIdentifier',
        '未知标识「{v1}」，请检查拼写，或先添加同名参数',
        { v1: name },
      ));
    }
    if (token.type === 'op' || token.type === ')') {
      throw new ExpressionError(toolText(
        'study.tools.graph.unexpectedToken',
        '表达式在「{v1}」附近无法解析',
        { v1: this.describeToken(token) },
      ));
    }
    throw new ExpressionError(toolText('study.tools.graph.parseError', PARSE_ERROR_TEXT));
  }
}

/**
 * 编译表达式：返回 `{ ok, ast, error, empty }`，绝不抛错。
 * @param {string} source LaTeX 风格线性表达式（如 `a*x^2+b*x+c`）
 * @param {Iterable<string>} paramNames 当前可用参数名
 */
export function compileExpression(source, paramNames = []) {
  const text = String(source ?? '').trim();
  if (!text) return { ok: false, empty: true, ast: null, error: '' };
  try {
    const tokens = tokenize(text);
    const ast = new Parser(tokens, new Set(Array.from(paramNames, String))).parse();
    return { ok: true, empty: false, ast, error: '' };
  } catch (error) {
    return {
      ok: false,
      empty: false,
      ast: null,
      error: error instanceof ExpressionError
        ? error.message
        : toolText('study.tools.graph.parseError', PARSE_ERROR_TEXT),
    };
  }
}

/** 对 AST 求值；domain 错误（sqrt(-1)、除以 0 等）返回 NaN，由采样器断开折线。 */
export function evaluateAst(node, scope = {}) {
  if (!node || typeof node !== 'object') return NaN;
  switch (node.type) {
    case 'number':
      return Number(node.value);
    case 'variable':
      return Number(scope.x);
    case 'param':
      return Number(scope.params ? scope.params[node.name] : NaN);
    case 'constant':
      return CONSTANTS[node.name] !== undefined ? CONSTANTS[node.name] : NaN;
    case 'negate':
      return -evaluateAst(node.operand, scope);
    case 'binary': {
      const left = evaluateAst(node.left, scope);
      const right = evaluateAst(node.right, scope);
      switch (node.op) {
        case '+': return left + right;
        case '-': return left - right;
        case '*': return left * right;
        case '/': return left / right;
        case '^': return Math.pow(left, right);
        default: return NaN;
      }
    }
    case 'call': {
      const fn = FUNCTIONS[node.name];
      if (typeof fn !== 'function') return NaN;
      return fn(evaluateAst(node.argument, scope));
    }
    default:
      return NaN;
  }
}

/**
 * 便捷求值（调试/自检用）：非法表达式返回 NaN。
 * @param {string} source 表达式
 * @param {{x?: number, params?: Record<string, number>}} scope
 */
export function evaluateExpression(source, scope = {}) {
  const params = Object.keys(scope.params || {});
  const compiled = compileExpression(source, params);
  if (!compiled.ok) return NaN;
  try {
    return evaluateAst(compiled.ast, scope);
  } catch {
    return NaN;
  }
}

/* ------------------------------------------------ 函数图定义：默认值 / 规范化 */

const DEFAULT_EXPRESSIONS = Object.freeze([
  { id: 'f1', latex: 'a*x^2+b*x+c', color: '#2b7de9', visible: true },
]);

const DEFAULT_PARAMS = Object.freeze([
  { name: 'a', value: 1, min: -5, max: 5, step: 0.1 },
  { name: 'b', value: 2, min: -10, max: 10, step: 0.1 },
  { name: 'c', value: 0, min: -10, max: 10, step: 0.1 },
]);

const EXPRESSION_COLORS = ['#2b7de9', '#d64545', '#2f855a', '#805ad5', '#b7791f', '#0f766e'];
const MAX_EXPRESSIONS = 8;
const MAX_PARAMS = 8;

const RESERVED_IDENTIFIERS = new Set([
  'x', 'pi', 'e', 'sin', 'cos', 'tan', 'sqrt', 'abs', 'exp', 'ln', 'log',
]);

const DEFAULT_X_RANGE = Object.freeze([-6, 6]);
const DEFAULT_Y_RANGE = Object.freeze([-8, 8]);
const MAX_RANGE_SPAN = 1e6;

function finiteNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

/** 默认函数图定义（与 Mafs 兼容数据模型逐字段对齐）。 */
export function defaultFunctionGraphDefinition() {
  return {
    version: 1,
    source: 'mafs-compatible',
    expressions: DEFAULT_EXPRESSIONS.map(item => ({ ...item })),
    params: DEFAULT_PARAMS.map(item => ({ ...item })),
    xRange: [...DEFAULT_X_RANGE],
    yRange: [...DEFAULT_Y_RANGE],
    points: [],
    labels: [],
  };
}

function sanitizeRange(value, fallback) {
  if (!Array.isArray(value) || value.length < 2) return [...fallback];
  const min = finiteNumber(value[0], fallback[0]);
  const max = finiteNumber(value[1], fallback[1]);
  if (!(max > min)) return [...fallback];
  const span = max - min;
  if (!(span > 1e-6) || span > MAX_RANGE_SPAN || Math.abs(min) > MAX_RANGE_SPAN || Math.abs(max) > MAX_RANGE_SPAN) {
    return [...fallback];
  }
  return [min, max];
}

function sanitizeColor(value, fallback) {
  const text = String(value || '').trim();
  return /^#[0-9a-fA-F]{3,8}$/.test(text) ? text : fallback;
}

function sanitizeParamName(value) {
  const name = String(value || '').trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,15}$/.test(name)) return '';
  if (RESERVED_IDENTIFIERS.has(name)) return '';
  return name;
}

function sanitizePoint(value) {
  if (!value || typeof value !== 'object') return null;
  const x = Number(value.x);
  const y = Number(value.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const label = typeof value.label === 'string' ? value.label.slice(0, 80) : '';
  return label ? { x, y, label } : { x, y };
}

function sanitizeLabel(value) {
  if (!value || typeof value !== 'object') return null;
  const x = Number(value.x);
  const y = Number(value.y);
  const text = String(value.text ?? value.label ?? '').slice(0, 120);
  if (!Number.isFinite(x) || !Number.isFinite(y) || !text) return null;
  return { x, y, text };
}

/**
 * 规范化任意来源的 function_graph 定义（附件 data / localStorage / 旧版本）。
 * 结构不合法返回 null；合法时补齐缺省字段并裁剪到安全范围（参数/范围/点数上限）。
 * @param {object|string} raw
 */
export function normalizeFunctionGraphDefinition(raw) {
  let value = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;

  const params = [];
  const paramNames = new Set();
  const sourceParams = Array.isArray(value.params) ? value.params : [];
  for (const item of sourceParams) {
    if (params.length >= MAX_PARAMS) break;
    if (!item || typeof item !== 'object') continue;
    const name = sanitizeParamName(item.name);
    if (!name || paramNames.has(name)) continue;
    let min = finiteNumber(item.min, -10);
    let max = finiteNumber(item.max, 10);
    if (!(max > min)) {
      min = -10;
      max = 10;
    }
    const step = Math.abs(finiteNumber(item.step, 0.1)) || 0.1;
    const rawValue = finiteNumber(item.value, min);
    const valueClamped = Math.min(max, Math.max(min, rawValue));
    params.push({ name, value: valueClamped, min, max, step });
    paramNames.add(name);
  }

  const expressions = [];
  const usedIds = new Set();
  const sourceExpressions = Array.isArray(value.expressions) ? value.expressions : [];
  for (const item of sourceExpressions) {
    if (expressions.length >= MAX_EXPRESSIONS) break;
    if (!item || typeof item !== 'object') continue;
    let id = String(item.id || '').trim().slice(0, 40);
    if (!id || usedIds.has(id)) id = `f${expressions.length + 1}`;
    if (usedIds.has(id)) id = `f${expressions.length + 1}-${Date.now().toString(36)}`;
    usedIds.add(id);
    expressions.push({
      id,
      latex: String(item.latex ?? item.expression ?? '').slice(0, 500),
      color: sanitizeColor(item.color, EXPRESSION_COLORS[expressions.length % EXPRESSION_COLORS.length]),
      visible: item.visible !== false,
    });
  }
  if (!expressions.length) expressions.push({ ...DEFAULT_EXPRESSIONS[0] });

  const points = [];
  for (const item of Array.isArray(value.points) ? value.points : []) {
    if (points.length >= 50) break;
    const point = sanitizePoint(item);
    if (point) points.push(point);
  }
  const labels = [];
  for (const item of Array.isArray(value.labels) ? value.labels : []) {
    if (labels.length >= 50) break;
    const label = sanitizeLabel(item);
    if (label) labels.push(label);
  }

  return {
    version: finiteNumber(value.version, 1),
    source: 'mafs-compatible',
    expressions,
    params,
    xRange: sanitizeRange(value.xRange, DEFAULT_X_RANGE),
    yRange: sanitizeRange(value.yRange, DEFAULT_Y_RANGE),
    points,
    labels,
  };
}

/* --------------------------------------------------------------- SVG 工具 */

const SVG_NS = 'http://www.w3.org/2000/svg';
let graphSequence = 0;

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, item] of Object.entries(attrs)) {
    if (item === undefined || item === null || item === false) continue;
    if (key === 'text') node.textContent = String(item);
    else node.setAttribute(key, String(item));
  }
  return node;
}

function niceStep(span, targetTicks = 7) {
  if (!(span > 0)) return 1;
  const raw = span / Math.max(1, targetTicks);
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalized = raw / magnitude;
  const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return factor * magnitude;
}

function tickValues(min, max, targetTicks = 7) {
  const span = max - min;
  if (!(span > 0)) return [];
  const step = niceStep(span, targetTicks);
  const start = Math.ceil(min / step) * step;
  const values = [];
  for (let value = start; value <= max + step * 1e-6 && values.length < 60; value += step) {
    values.push(Number(value.toFixed(10)));
  }
  return values;
}

function tickDecimals(step) {
  if (!(step > 0)) return 0;
  return Math.min(4, Math.max(0, Math.ceil(-Math.log10(step))));
}

function formatTick(value, step) {
  const decimals = tickDecimals(step);
  const text = Number(value).toFixed(decimals);
  return text.replace(/^-0(\.0+)?$/, '0');
}

function formatCoord(value) {
  if (!Number.isFinite(value)) return '';
  if (Math.abs(value) < 1e-9) return '0';
  if (Math.abs(value) >= 1000) return String(Math.round(value));
  return Number(value.toFixed(2)).toString();
}

function paramDecimals(step) {
  if (!(step > 0)) return 1;
  if (step >= 1) return 0;
  return Math.min(4, Math.max(0, Math.ceil(-Math.log10(step))));
}

function formatParamValue(value, step) {
  return Number(value).toFixed(paramDecimals(step));
}

/* ----------------------------------------------- 渲染：SVG 坐标系与采样折线 */

const PLOT = Object.freeze({
  width: 640, height: 400, left: 46, right: 18, top: 18, bottom: 32,
});
const PLOT_WIDTH = PLOT.width - PLOT.left - PLOT.right;
const PLOT_HEIGHT = PLOT.height - PLOT.top - PLOT.bottom;
const SAMPLE_COUNT = 400;

/** 数值近二次检测：返回 `{a,b,c,x,y}`（f(x)=ax²+bx+c 的顶点），非二次返回 null。 */
function quadraticOf(evaluate) {
  const atMinus1 = evaluate(-1);
  const at0 = evaluate(0);
  const at1 = evaluate(1);
  const at2 = evaluate(2);
  if (![atMinus1, at0, at1, at2].every(value => Number.isFinite(value))) return null;
  const a = (at1 - 2 * at0 + atMinus1) / 2;
  const b = (at1 - atMinus1) / 2;
  const c = at0;
  if (!(Math.abs(a) > 1e-9) || Math.abs(a) > 1e12) return null;
  const predicted = 4 * a + 2 * b + c;
  const scale = Math.max(1, Math.abs(predicted), Math.abs(at2), Math.abs(at1), Math.abs(at0));
  if (Math.abs(predicted - at2) > scale * 1e-6) return null;
  const x = -b / (2 * a);
  const y = c - (b * b) / (4 * a);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { a, b, c, x, y };
}

/**
 * 渲染函数图编辑器。
 * @param {{definition?: object|string, readOnly?: boolean, ariaLabel?: string, onChange?: Function}} options
 * @returns {{element: HTMLElement, getDefinition: Function, setDefinition: Function,
 *            redraw: Function, destroy: Function, isReadOnly: Function}}
 */
export function createFunctionGraph(options = {}) {
  const readOnly = options.readOnly === true;
  let definition = normalizeFunctionGraphDefinition(options.definition) || defaultFunctionGraphDefinition();
  let destroyed = false;
  let pendingFrame = 0;
  let expressionSequence = definition.expressions.length + 1;
  const compiled = new Map();
  const rowRefs = new Map();
  const clipId = `st-graph-clip-${++graphSequence}`;

  const root = el('div', { class: 'st-graph', 'data-study-tool': 'function-graph' });
  const hintNode = el('p', { class: 'st-graph-hint' }, toolText(
    'study.tools.graph.hint',
    '拖动参数滑块可实时观察图像变化；表达式支持 + - * / ^、括号、sin/cos/tan/sqrt/abs/exp/ln/log、pi、e 与 x。',
  ));
  const plotWrap = el('div', { class: 'st-graph-plot' });
  const svg = svgNode('svg', {
    class: 'st-graph-svg',
    viewBox: `0 0 ${PLOT.width} ${PLOT.height}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'img',
    'aria-label': toolText('study.tools.graph.aria', '函数图像'),
  });
  svg.setAttribute('width', '100%');
  svg.style.height = 'auto';
  svg.style.display = 'block';
  plotWrap.append(svg);
  const statusNode = el('p', { class: 'st-graph-status', 'aria-live': 'polite' });

  const expressionList = el('div', { class: 'st-graph-expressions' });
  const expressionHead = el('div', { class: 'st-graph-section-head' },
    el('strong', {}, toolText('study.tools.graph.expressions', '表达式')));
  if (!readOnly) {
    expressionHead.append(el('button', {
      type: 'button',
      class: 'st-mini-button',
      onClick: addExpression,
      'aria-label': toolText('study.tools.graph.addExpression', '添加表达式'),
    }, icon('plus'), toolText('study.tools.graph.addExpression', '添加表达式')));
  }
  const expressionSection = el('section', { class: 'st-graph-section' }, expressionHead, expressionList);

  const paramList = el('div', { class: 'st-graph-params' });
  const paramHead = el('div', { class: 'st-graph-section-head' },
    el('strong', {}, toolText('study.tools.graph.params', '参数')));
  let paramForm = null;
  if (!readOnly) {
    const paramName = el('input', {
      class: 'st-param-name-input',
      type: 'text',
      maxlength: '8',
      placeholder: toolText('study.tools.graph.paramName', '参数名'),
      'aria-label': toolText('study.tools.graph.paramName', '参数名'),
    });
    const paramValue = el('input', {
      class: 'st-param-value-input',
      type: 'number',
      step: 'any',
      value: '1',
      placeholder: toolText('study.tools.graph.paramValue', '初始值'),
      'aria-label': toolText('study.tools.graph.paramValue', '初始值'),
    });
    const addButton = el('button', {
      type: 'button',
      class: 'st-mini-button',
      onClick: () => addParam(paramName, paramValue),
    }, icon('plus'), toolText('study.tools.graph.addParam', '添加参数'));
    paramForm = el('div', { class: 'st-param-form' }, paramName, paramValue, addButton);
  }
  const paramSection = el('section', { class: 'st-graph-section' }, paramHead, paramList, paramForm);

  root.append(hintNode, plotWrap, statusNode, expressionSection, paramSection);

  /* -------- 表达式行 / 参数行 -------- */

  function nextExpressionId() {
    for (;;) {
      const id = `f${expressionSequence}`;
      expressionSequence += 1;
      if (!definition.expressions.some(expression => expression.id === id)) return id;
    }
  }

  function addExpression() {
    if (readOnly || definition.expressions.length >= MAX_EXPRESSIONS) {
      if (!readOnly) {
        toast(toolText('study.tools.graph.expressionLimit', '最多 {v1} 条表达式', { v1: MAX_EXPRESSIONS }), true);
      }
      return;
    }
    definition.expressions.push({
      id: nextExpressionId(),
      latex: '',
      color: EXPRESSION_COLORS[definition.expressions.length % EXPRESSION_COLORS.length],
      visible: true,
    });
    renderExpressionRows();
    scheduleRedraw();
  }

  function removeExpression(expressionId) {
    if (readOnly || definition.expressions.length <= 1) return;
    definition.expressions = definition.expressions.filter(expression => expression.id !== expressionId);
    renderExpressionRows();
    scheduleRedraw();
  }

  function addParam(nameInput, valueInput) {
    if (readOnly) return;
    if (definition.params.length >= MAX_PARAMS) {
      toast(toolText('study.tools.graph.paramLimit', '最多 {v1} 个参数', { v1: MAX_PARAMS }), true);
      return;
    }
    const name = sanitizeParamName(nameInput.value);
    if (!name) {
      toast(toolText('study.tools.graph.paramInvalid', '参数名需以字母或下划线开头，只含字母、数字、下划线'), true);
      return;
    }
    if (definition.params.some(param => param.name === name)) {
      toast(toolText('study.tools.graph.paramDuplicate', '参数「{v1}」已存在', { v1: name }), true);
      return;
    }
    const value = finiteNumber(valueInput.value, 1);
    const min = Math.min(-10, value);
    const max = Math.max(10, value);
    definition.params.push({ name, value, min, max, step: 0.1 });
    nameInput.value = '';
    valueInput.value = '1';
    renderParamRows();
    scheduleRedraw();
  }

  function removeParam(name) {
    if (readOnly) return;
    definition.params = definition.params.filter(param => param.name !== name);
    renderParamRows();
    scheduleRedraw();
  }

  function renderExpressionRows() {
    rowRefs.clear();
    const rows = definition.expressions.map((expression, index) => {
      const row = el('div', { class: 'st-expr-row', 'data-expression-id': expression.id });
      const colorDot = el('span', { class: 'st-expr-color', 'aria-hidden': 'true' });
      colorDot.style.background = expression.color;
      const label = el('span', { class: 'st-expr-label' }, 'y =');
      const input = el('input', {
        class: 'st-expr-input',
        type: 'text',
        spellcheck: 'false',
        autocomplete: 'off',
        'aria-label': toolText('study.tools.graph.expressionLabel', '表达式 {v1}', { v1: index + 1 }),
      });
      input.value = expression.latex;
      input.disabled = readOnly;
      input.addEventListener('input', () => {
        expression.latex = input.value;
        scheduleRedraw();
      });
      const visible = el('input', {
        type: 'checkbox',
        class: 'st-expr-visible',
        checked: expression.visible !== false,
        disabled: readOnly,
        'aria-label': toolText('study.tools.graph.visibleToggle', '显示或隐藏这条曲线'),
        title: toolText('study.tools.graph.visibleToggle', '显示或隐藏这条曲线'),
      });
      visible.addEventListener('change', () => {
        expression.visible = visible.checked;
        scheduleRedraw();
      });
      const remove = el('button', {
        type: 'button',
        class: 'st-icon-button st-expr-remove',
        'aria-label': toolText('study.tools.graph.removeExpression', '删除此表达式'),
        title: toolText('study.tools.graph.removeExpression', '删除此表达式'),
        disabled: readOnly || definition.expressions.length <= 1,
        onClick: () => removeExpression(expression.id),
      }, icon('trash'));
      const errorNode = el('p', { class: 'st-expr-error', hidden: true });
      row.append(colorDot, label, input, visible, remove, errorNode);
      rowRefs.set(expression.id, { row, input, errorNode });
      return row;
    });
    expressionList.replaceChildren(...rows);
  }

  function renderParamRows() {
    if (!definition.params.length) {
      paramList.replaceChildren(el('p', { class: 'st-graph-params-empty' },
        toolText('study.tools.graph.noParams', '当前没有参数；表达式可直接使用数字与 x。')));
      return;
    }
    const rows = definition.params.map((param) => {
      const label = el('span', { class: 'st-param-label' });
      const range = el('input', {
        type: 'range',
        class: 'st-param-slider',
        min: String(param.min),
        max: String(param.max),
        step: String(param.step),
        'aria-label': toolText('study.tools.graph.paramLabel', '{v1} = {v2}', {
          v1: param.name, v2: formatParamValue(param.value, param.step),
        }),
      });
      range.value = String(param.value);
      range.disabled = readOnly;
      const sync = () => {
        param.value = Math.min(param.max, Math.max(param.min, finiteNumber(range.value, param.min)));
        label.textContent = toolText('study.tools.graph.paramLabel', '{v1} = {v2}', {
          v1: param.name,
          v2: formatParamValue(param.value, param.step),
        });
        range.setAttribute('aria-label', label.textContent);
      };
      sync();
      range.addEventListener('input', () => {
        sync();
        scheduleRedraw();
      });
      const remove = el('button', {
        type: 'button',
        class: 'st-icon-button st-param-remove',
        'aria-label': toolText('study.tools.graph.paramRemove', '删除参数 {v1}', { v1: param.name }),
        title: toolText('study.tools.graph.paramRemove', '删除参数 {v1}', { v1: param.name }),
        disabled: readOnly,
        onClick: () => removeParam(param.name),
      }, icon('close'));
      return el('div', { class: 'st-param-row', 'data-param-name': param.name }, label, range, remove);
    });
    paramList.replaceChildren(...rows);
  }

  /* -------- 绘制 -------- */

  function paramScope() {
    const params = Object.create(null);
    for (const param of definition.params) params[param.name] = param.value;
    return params;
  }

  function compileAll() {
    const names = definition.params.map(param => param.name);
    compiled.clear();
    for (const expression of definition.expressions) {
      const result = compileExpression(expression.latex, names);
      compiled.set(expression.id, result);
      const refs = rowRefs.get(expression.id);
      if (!refs) continue;
      const invalid = !result.ok && !result.empty;
      refs.input.classList.toggle('is-invalid', invalid);
      refs.input.setAttribute('aria-invalid', invalid ? 'true' : 'false');
      refs.errorNode.hidden = !invalid;
      refs.errorNode.textContent = invalid ? result.error : '';
      refs.row.classList.toggle('has-error', invalid);
    }
  }

  function makeEvaluator(ast) {
    const params = paramScope();
    return (x) => {
      try {
        const value = evaluateAst(ast, { x, params });
        return Number.isFinite(value) ? value : NaN;
      } catch {
        return NaN;
      }
    };
  }

  function redraw() {
    if (destroyed) return;
    compileAll();
    const [xMin, xMax] = definition.xRange;
    const [yMin, yMax] = definition.yRange;
    const mapX = x => PLOT.left + ((x - xMin) / (xMax - xMin)) * PLOT_WIDTH;
    const mapY = y => PLOT.top + ((yMax - y) / (yMax - yMin)) * PLOT_HEIGHT;
    const children = [];
    const gridGroup = svgNode('g', { class: 'st-graph-grid' });
    const axisGroup = svgNode('g', { class: 'st-graph-axes' });
    const tickGroup = svgNode('g', { class: 'st-graph-ticks' });
    const curveGroup = svgNode('g', { class: 'st-graph-curves' });
    const markGroup = svgNode('g', { class: 'st-graph-marks' });

    const defs = svgNode('defs');
    const clip = svgNode('clipPath', { id: clipId });
    clip.append(svgNode('rect', {
      x: PLOT.left, y: PLOT.top, width: PLOT_WIDTH, height: PLOT_HEIGHT,
    }));
    defs.append(clip);

    const background = svgNode('rect', {
      class: 'st-graph-bg', x: PLOT.left, y: PLOT.top, width: PLOT_WIDTH, height: PLOT_HEIGHT,
    });
    const frame = svgNode('rect', {
      class: 'st-graph-frame', x: PLOT.left, y: PLOT.top, width: PLOT_WIDTH, height: PLOT_HEIGHT,
    });

    const xSpan = xMax - xMin;
    const ySpan = yMax - yMin;
    const xStep = niceStep(xSpan);
    const yStep = niceStep(ySpan);
    const xTicks = tickValues(xMin, xMax);
    const yTicks = tickValues(yMin, yMax);
    const hasXAxis = yMin < 0 && yMax > 0;
    const hasYAxis = xMin < 0 && xMax > 0;

    for (const value of xTicks) {
      if (Math.abs(value) < 1e-9 && hasYAxis) continue;
      const px = mapX(value);
      gridGroup.append(svgNode('line', {
        class: 'st-graph-grid-line', x1: px, y1: PLOT.top, x2: px, y2: PLOT.top + PLOT_HEIGHT,
      }));
    }
    for (const value of yTicks) {
      if (Math.abs(value) < 1e-9 && hasXAxis) continue;
      const py = mapY(value);
      gridGroup.append(svgNode('line', {
        class: 'st-graph-grid-line', x1: PLOT.left, y1: py, x2: PLOT.left + PLOT_WIDTH, y2: py,
      }));
    }

    if (hasYAxis) {
      const px = mapX(0);
      axisGroup.append(svgNode('line', {
        class: 'st-graph-axis-line', x1: px, y1: PLOT.top, x2: px, y2: PLOT.top + PLOT_HEIGHT,
      }));
    }
    if (hasXAxis) {
      const py = mapY(0);
      axisGroup.append(svgNode('line', {
        class: 'st-graph-axis-line', x1: PLOT.left, y1: py, x2: PLOT.left + PLOT_WIDTH, y2: py,
      }));
    }
    if (!hasXAxis) {
      const py = yMin >= 0 ? PLOT.top + PLOT_HEIGHT : PLOT.top;
      axisGroup.append(svgNode('line', {
        class: 'st-graph-axis-line', x1: PLOT.left, y1: py, x2: PLOT.left + PLOT_WIDTH, y2: py,
      }));
    }
    if (!hasYAxis) {
      const px = xMin >= 0 ? PLOT.left : PLOT.left + PLOT_WIDTH;
      axisGroup.append(svgNode('line', {
        class: 'st-graph-axis-line', x1: px, y1: PLOT.top, x2: px, y2: PLOT.top + PLOT_HEIGHT,
      }));
    }

    const xTickBaseline = hasXAxis
      ? Math.max(PLOT.top + 12, Math.min(PLOT.top + PLOT_HEIGHT - 4, mapY(0) + 16))
      : PLOT.top + PLOT_HEIGHT - 6;
    for (const value of xTicks) {
      if (Math.abs(value) < 1e-9 && hasYAxis) continue;
      const px = mapX(value);
      tickGroup.append(svgNode('line', {
        class: 'st-graph-tick-line', x1: px, y1: xTickBaseline - 4, x2: px, y2: xTickBaseline,
      }));
      tickGroup.append(svgNode('text', {
        class: 'st-graph-tick-label', x: px, y: xTickBaseline + 12, 'text-anchor': 'middle',
        text: formatTick(value, xStep),
      }));
    }
    for (const value of yTicks) {
      if (Math.abs(value) < 1e-9 && hasXAxis) continue;
      const py = mapY(value);
      const anchorX = hasYAxis ? Math.min(PLOT.left + PLOT_WIDTH - 6, Math.max(PLOT.left + 6, mapX(0) - 8)) : PLOT.left - 8;
      tickGroup.append(svgNode('line', {
        class: 'st-graph-tick-line', x1: anchorX - 4, y1: py, x2: anchorX, y2: py,
      }));
      tickGroup.append(svgNode('text', {
        class: 'st-graph-tick-label', x: anchorX - 6, y: py + 4, 'text-anchor': 'end',
        text: formatTick(value, yStep),
      }));
    }

    const xAxisLabelX = PLOT.left + PLOT_WIDTH - 2;
    const xAxisLabelY = hasXAxis
      ? Math.min(PLOT.top + PLOT_HEIGHT - 6, mapY(0) + 16)
      : PLOT.top + PLOT_HEIGHT + 18;
    tickGroup.append(svgNode('text', {
      class: 'st-graph-axis-label', x: xAxisLabelX, y: xAxisLabelY, 'text-anchor': 'end', text: 'x',
    }));
    tickGroup.append(svgNode('text', {
      class: 'st-graph-axis-label',
      x: hasYAxis ? mapX(0) + 8 : PLOT.left + 8,
      y: PLOT.top + 14,
      text: 'y',
    }));

    const curveLayer = svgNode('g', { 'clip-path': `url(#${clipId})` });
    const vertexItems = [];
    const validExpressions = definition.expressions.filter(expression => expression.visible !== false);

    for (const expression of validExpressions) {
      const result = compiled.get(expression.id);
      if (!result || !result.ok) continue;
      const evaluate = makeEvaluator(result.ast);
      const segments = [];
      let segment = [];
      const guardLow = yMin - ySpan * 2;
      const guardHigh = yMax + ySpan * 2;
      for (let index = 0; index < SAMPLE_COUNT; index += 1) {
        const x = xMin + (index / (SAMPLE_COUNT - 1)) * xSpan;
        const y = evaluate(x);
        if (!Number.isFinite(y) || y < guardLow || y > guardHigh) {
          if (segment.length > 1) segments.push(segment);
          segment = [];
          continue;
        }
        segment.push([mapX(x), mapY(y)]);
      }
      if (segment.length > 1) segments.push(segment);
      for (const points of segments) {
        const polyline = svgNode('polyline', {
          class: 'st-graph-curve',
          points: points.map(pair => `${pair[0].toFixed(2)},${pair[1].toFixed(2)}`).join(' '),
          fill: 'none',
          stroke: expression.color,
          'stroke-width': 2.2,
          'stroke-linecap': 'round',
          'stroke-linejoin': 'round',
        });
        curveLayer.append(polyline);
      }
      const quadratic = quadraticOf(evaluate);
      if (quadratic && quadratic.x >= xMin && quadratic.x <= xMax
        && quadratic.y >= yMin && quadratic.y <= yMax) {
        vertexItems.push({ expression, quadratic, evaluate, color: expression.color });
      }
    }

    // 顶点标注（二次函数）：坐标按 f(x)=ax²+bx+c 的 (-b/2a, c-b²/4a) 计算。
    for (const item of vertexItems) {
      const { quadratic, expression } = item;
      const px = mapX(quadratic.x);
      const py = mapY(quadratic.y);
      const group = svgNode('g', { class: 'st-graph-vertex', 'data-vertex': 'true' });
      const titleNode = svgNode('title', {});
      titleNode.textContent = toolText(
        'study.tools.graph.vertexTitle',
        '顶点：(-b/2a, c - b²/4a) ≈ ({v1}, {v2})',
        { v1: formatCoord(quadratic.x), v2: formatCoord(quadratic.y) },
      );
      group.append(titleNode);
      group.append(svgNode('circle', {
        class: 'st-graph-vertex-dot', cx: px, cy: py, r: 4, fill: expression.color,
      }));
      const flip = px > PLOT.left + PLOT_WIDTH - 120;
      group.append(svgNode('text', {
        class: 'st-graph-vertex-label',
        x: flip ? px - 8 : px + 8,
        y: Math.max(PLOT.top + 12, py - 8),
        'text-anchor': flip ? 'end' : 'start',
        fill: expression.color,
        text: toolText('study.tools.graph.vertexAt', '顶点 ({v1}, {v2})', {
          v1: formatCoord(quadratic.x), v2: formatCoord(quadratic.y),
        }),
      }));
      markGroup.append(group);
    }

    // 定义里的点与文字标注（与 Mafs 数据模型兼容，跨页面重开时原样显示）。
    for (const point of definition.points) {
      if (point.x < xMin || point.x > xMax || point.y < yMin || point.y > yMax) continue;
      markGroup.append(svgNode('circle', {
        class: 'st-graph-point', cx: mapX(point.x), cy: mapY(point.y), r: 3.5,
      }));
      if (point.label) {
        markGroup.append(svgNode('text', {
          class: 'st-graph-point-label', x: mapX(point.x) + 7, y: mapY(point.y) - 6,
          text: point.label,
        }));
      }
    }
    for (const label of definition.labels) {
      if (label.x < xMin || label.x > xMax || label.y < yMin || label.y > yMax) continue;
      markGroup.append(svgNode('text', {
        class: 'st-graph-custom-label', x: mapX(label.x), y: mapY(label.y), text: label.text,
      }));
    }

    children.push(defs, background, gridGroup, axisGroup, curveLayer, markGroup, tickGroup, frame);
    svg.replaceChildren(...children);

    const errors = [];
    for (const expression of definition.expressions) {
      const result = compiled.get(expression.id);
      if (result && !result.ok && !result.empty) errors.push(result.error);
    }
    if (errors.length) {
      statusNode.textContent = toolText('study.tools.graph.invalidNotice', '表达式有误，未绘制错误曲线：{v1}', {
        v1: errors[0],
      });
      statusNode.classList.add('is-error');
    } else {
      statusNode.textContent = '';
      statusNode.classList.remove('is-error');
    }
    const visibleLabels = definition.expressions
      .filter(expression => expression.visible !== false && !String(expression.latex || '').trim())
      .length;
    if (!errors.length && visibleLabels && !definition.expressions.some(expression => {
      const result = compiled.get(expression.id);
      return result && result.ok;
    })) {
      statusNode.textContent = toolText('study.tools.graph.emptyExpressions', '输入表达式后即可绘制图像。');
    }
    updateAriaLabel();
    root.setAttribute('data-graph-ready', 'true');
  }

  function updateAriaLabel() {
    const parts = [];
    for (const expression of definition.expressions) {
      const result = compiled.get(expression.id);
      if (expression.visible !== false && result && result.ok) parts.push(`y = ${expression.latex}`);
    }
    const summary = parts.length
      ? parts.join('；')
      : toolText('study.tools.graph.empty', '暂无可绘制曲线');
    svg.setAttribute('aria-label', toolText('study.tools.graph.aria', '函数图像：{v1}', { v1: summary }));
  }

  function scheduleRedraw() {
    if (destroyed || pendingFrame) return;
    const run = () => {
      pendingFrame = 0;
      redraw();
    };
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      pendingFrame = window.requestAnimationFrame(run);
    } else {
      pendingFrame = setTimeout(run, 16);
    }
  }

  /* -------- 对外 controller -------- */

  function getDefinition() {
    return {
      version: 1,
      source: 'mafs-compatible',
      expressions: definition.expressions.map(expression => ({
        id: expression.id,
        latex: expression.latex,
        color: expression.color,
        visible: expression.visible !== false,
      })),
      params: definition.params.map(param => ({
        name: param.name,
        value: param.value,
        min: param.min,
        max: param.max,
        step: param.step,
      })),
      xRange: [...definition.xRange],
      yRange: [...definition.yRange],
      points: definition.points.map(point => ({ ...point })),
      labels: definition.labels.map(label => ({ ...label })),
    };
  }

  function setDefinition(next) {
    const normalized = normalizeFunctionGraphDefinition(next);
    if (!normalized) return false;
    definition = normalized;
    renderExpressionRows();
    renderParamRows();
    redraw();
    return true;
  }

  renderExpressionRows();
  renderParamRows();
  redraw();

  return {
    element: root,
    getDefinition,
    setDefinition,
    redraw,
    isReadOnly: () => readOnly,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (pendingFrame) {
        if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
          window.cancelAnimationFrame(pendingFrame);
        } else {
          clearTimeout(pendingFrame);
        }
        pendingFrame = 0;
      }
      compiled.clear();
      rowRefs.clear();
      root.remove();
    },
  };
}

/* ------------------------------------------------- 学习附件：读取 / 序列化 / 保存 */

const FUNCTION_GRAPH_KIND = 'function_graph';
const ARTIFACT_TARGET_KIND = 'question';
const PROBE_TIMEOUT_MS = 4000;

function withTimeout(promise, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('timeout'));
    }, timeoutMs);
    promise.then((value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    }, (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
  });
}

/** 把 by-target 条目 / 已保存 artifact 对象归一化成 `{id, kind, title, revision, data}`。 */
export function normalizeFunctionGraphArtifact(input) {
  if (!input || typeof input !== 'object') return null;
  const artifact = input.artifact && typeof input.artifact === 'object' ? input.artifact : null;
  let data = input.data;
  if (data === undefined && input.revision && typeof input.revision === 'object') {
    data = input.revision.data;
  }
  if (typeof data === 'string') {
    try {
      data = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  let revision = input.revision;
  if (revision && typeof revision === 'object') revision = revision.revision;
  const revisionNumber = Number(revision);
  return {
    id: String((artifact && artifact.artifact_id) || input.artifact_id || input.id || ''),
    kind: String((artifact && artifact.kind) || input.kind || FUNCTION_GRAPH_KIND),
    title: String((artifact && artifact.title) || input.title || ''),
    revision: Number.isFinite(revisionNumber) && revisionNumber > 0 ? revisionNumber : null,
    data,
  };
}

function isRevisionConflict(error) {
  const message = String(error && error.message ? error.message : '');
  return /附件已被更新|刷新后重试|版本冲突/.test(message);
}

function formatTimestamp(date = new Date()) {
  const pad = value => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function clampTitle(text) {
  return String(text || '').slice(0, 120);
}

function buildFunctionGraphPreview(definition) {
  const params = Object.create(null);
  for (const param of definition.params) params[param.name] = param.value;
  return {
    kind: 'function-graph',
    expressions: definition.expressions
      .filter(expression => String(expression.latex || '').trim())
      .map(expression => ({ latex: expression.latex })),
    params,
  };
}

/** 生成保存请求体（POST 创建 / PATCH 新版本共用）。 */
function functionGraphSaveBody(definition, title, ref, preview) {
  const body = {
    kind: FUNCTION_GRAPH_KIND,
    title,
    data: definition,
    preview,
    source_kind: ref ? ARTIFACT_TARGET_KIND : undefined,
    source_ref: ref || undefined,
  };
  return body;
}

/**
 * 打开函数图弹层（原生 dialog：桌面大窗 / 手机全屏由 CSS 控制，Esc 关闭并归还焦点）。
 * @param {{ref?: string, artifact?: object, readOnly?: boolean, onSaved?: Function,
 *          trigger?: HTMLElement, titleHint?: string}} options
 */
export function openFunctionGraph(options = {}) {
  const ref = String(options.ref || '').trim();
  const readOnly = options.readOnly === true;
  const loadedArtifact = normalizeFunctionGraphArtifact(options.artifact);
  const hint = String(options.titleHint || '').trim();
  const state = {
    artifactId: loadedArtifact ? loadedArtifact.id : '',
    revision: loadedArtifact ? loadedArtifact.revision : null,
    title: loadedArtifact && loadedArtifact.title
      ? clampTitle(loadedArtifact.title)
      : clampTitle(toolText('study.tools.graph.titleDefault', '函数图像 · {v1}', {
        v1: hint || ref || formatTimestamp(),
      })),
    savedKind: FUNCTION_GRAPH_KIND,
  };

  const dialog = el('dialog', {
    class: 'st-dialog st-graph-dialog',
    'data-study-tool': 'function-graph-dialog',
    'aria-label': toolText('study.tools.title.graph', '函数图像'),
  });
  const titleNode = el('h3', { class: 'st-dialog-title', 'data-no-i18n': 'true' }, state.title);
  const metaNode = el('p', { class: 'st-dialog-meta' });
  const graph = createFunctionGraph({
    definition: loadedArtifact ? loadedArtifact.data : defaultFunctionGraphDefinition(),
    readOnly,
  });
  const saveButton = button(
    toolText('study.tools.save', '保存为学习附件'),
    event => busy(event.currentTarget, () => saveArtifact()),
    '',
    'download',
    { 'data-study-tool-save': 'true' },
  );
  const closeButton = button(toolText('study.tools.close', '关闭'), () => dialog.close(), 'secondary');

  dialog.append(
    el('div', { class: 'st-dialog-head' },
      el('div', {},
        el('div', { class: 'st-dialog-eyebrow' }, toolText('study.tools.artifactEyebrow', '学习附件')),
        titleNode),
      el('button', {
        type: 'button',
        class: 'st-dialog-close',
        'aria-label': toolText('study.tools.close', '关闭'),
        title: toolText('study.tools.close', '关闭'),
        onClick: () => dialog.close(),
      }, icon('close'))),
    el('div', { class: 'st-dialog-body' },
      graph.element,
      el('div', { class: 'st-dialog-info' },
        metaNode,
        el('p', { class: 'st-dialog-hint' }, toolText(
          'study.tools.graph.saveHint',
          '保存后可在题目、错题与复习入口重新打开这份函数图；再次保存会追加新版本。',
        )))),
    el('div', { class: 'st-dialog-actions' }, closeButton, saveButton),
  );

  function updateMeta() {
    if (state.artifactId) {
      metaNode.textContent = toolText('study.tools.artifactMeta', '附件 {v1} · 第 {v2} 版', {
        v1: state.artifactId,
        v2: state.revision === null ? '—' : state.revision,
      });
    } else {
      metaNode.textContent = toolText('study.tools.notSaved', '尚未保存为学习附件');
    }
    const saved = Boolean(state.artifactId);
    saveButton.textContent = saved
      ? toolText('study.tools.saveNewVersion', '另存新版本')
      : toolText('study.tools.save', '保存为学习附件');
    saveButton.classList.toggle('is-saved', saved);
  }

  async function saveArtifact() {
    const definition = graph.getDefinition();
    const preview = buildFunctionGraphPreview(definition);
    try {
      if (state.artifactId) {
        const payload = {
          data: definition,
          preview,
          title: state.title,
        };
        if (state.revision !== null) payload.expected_revision = state.revision;
        const updated = await api(`/artifacts/${encodeURIComponent(state.artifactId)}`, {
          method: 'PATCH',
          data: payload,
        });
        const artifact = updated && updated.artifact ? updated.artifact : {};
        const revision = updated && updated.revision ? updated.revision.revision : null;
        state.revision = Number.isFinite(Number(revision)) ? Number(revision) : state.revision;
        if (artifact.title) state.title = clampTitle(artifact.title);
        titleNode.textContent = state.title;
        updateMeta();
        toast(toolText('study.tools.saved', '已保存为学习附件：{v1} · 第 {v2} 版', {
          v1: state.artifactId,
          v2: state.revision === null ? '—' : state.revision,
        }));
        if (typeof options.onSaved === 'function') {
          try {
            options.onSaved({ artifactId: state.artifactId, revision: state.revision, kind: state.savedKind });
          } catch {
            /* 保存回调异常不影响弹层 */
          }
        }
        return;
      }
      const body = functionGraphSaveBody(definition, state.title, ref, preview);
      body.links = ref ? [{ target_kind: ARTIFACT_TARGET_KIND, target_key: ref }] : [];
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      const artifactId = String(artifact.artifact_id || '');
      if (!artifactId) throw new Error(toolText('study.tools.missingArtifactId', '服务端未返回学习附件 ID'));
      const revision = created && created.revision ? created.revision.revision : null;
      state.artifactId = artifactId;
      state.revision = Number.isFinite(Number(revision)) ? Number(revision) : null;
      if (artifact.title) state.title = clampTitle(artifact.title);
      titleNode.textContent = state.title;
      updateMeta();
      toast(toolText('study.tools.saved', '已保存为学习附件：{v1} · 第 {v2} 版', {
        v1: artifactId,
        v2: state.revision === null ? '1' : state.revision,
      }));
      if (typeof options.onSaved === 'function') {
        try {
          options.onSaved({ artifactId, revision: state.revision, kind: state.savedKind });
        } catch {
          /* 同上 */
        }
      }
    } catch (error) {
      if (isRevisionConflict(error)) {
        toast(toolText(
          'study.tools.saveConflict',
          '附件已在别处更新，请刷新后重试（当前版本 {v1}）',
          { v1: state.revision === null ? '—' : state.revision },
        ), true);
      } else {
        toast(toolText('study.tools.saveFailed', '保存学习附件失败：{v1}', {
          v1: error && error.message ? error.message : '',
        }), true);
      }
    }
  }

  updateMeta();
  document.body.append(dialog);
  dialog.addEventListener('close', () => {
    try {
      graph.destroy();
    } catch {
      /* 释放失败不影响关闭 */
    }
    dialog.remove();
    try {
      options.trigger?.focus?.();
    } catch {
      /* 焦点归还失败忽略 */
    }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');

  const controller = {
    element: dialog,
    dialog,
    graph,
    getDefinition: () => graph.getDefinition(),
    setDefinition: definition => graph.setDefinition(definition),
    getArtifact: () => ({
      artifactId: state.artifactId,
      revision: state.revision,
      kind: state.savedKind,
      title: state.title,
    }),
    close: () => dialog.close(),
    destroy: () => dialog.close(),
  };
  return controller;
}

function pickArtifactItem(items, kind) {
  if (!Array.isArray(items)) return null;
  let picked = null;
  for (const item of items) {
    const artifact = item && item.artifact ? item.artifact : null;
    if (artifact && String(artifact.kind || '') === kind) picked = item;
  }
  return picked ? normalizeFunctionGraphArtifact(picked) : null;
}

/**
 * 打开某道题目的函数图：先探测 by-target 已保存附件（超时 4s，失败静默），
 * 命中则载入最新版本，否则打开默认函数图。返回 controller；无法打开时抛错由调用方提示。
 * @param {{ref?: string, trigger?: HTMLElement, titleHint?: string, fallbackItems?: Array,
 *          readOnly?: boolean, onSaved?: Function}} options
 */
export async function openFunctionGraphForTarget(options = {}) {
  const ref = String(options.ref || '').trim();
  let artifact = null;
  if (ref) {
    try {
      const response = await withTimeout(
        api(`/artifacts/by-target?${query({ target_kind: ARTIFACT_TARGET_KIND, target_key: ref })}`),
        PROBE_TIMEOUT_MS,
      );
      artifact = pickArtifactItem(response && response.items, FUNCTION_GRAPH_KIND);
    } catch {
      artifact = pickArtifactItem(options.fallbackItems, FUNCTION_GRAPH_KIND);
    }
  }
  return openFunctionGraph({
    ref,
    artifact,
    readOnly: options.readOnly === true,
    onSaved: options.onSaved,
    trigger: options.trigger,
    titleHint: options.titleHint,
  });
}

/**
 * 函数图入口是否适用：数学题、后端标记 has_formula，或题干内容块含公式（含 latex）。
 * @param {object} question 题目详情（题库/个人题/精选均可）
 */
export function functionGraphEligible(question) {
  if (!question || typeof question !== 'object') return false;
  const subject = String(question.subject || '').trim();
  if (subject === '数学') return true;
  if (question.has_formula === true) return true;
  const blocks = blockList(question.stem_blocks);
  return blocks.some((block) => {
    if (!block || typeof block !== 'object') return false;
    const kind = String(block.kind || block.type || '').toLowerCase();
    if (kind === 'math') return true;
    return typeof block.latex === 'string' && block.latex.trim() !== '';
  });
}
