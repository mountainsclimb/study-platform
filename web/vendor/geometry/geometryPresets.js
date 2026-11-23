/* 高中几何演示预设（JSXGraph 1.14.3；方案 §6.1）。
 *
 * 纪律（静态检查覆盖，见 tests/test_oss_vendor_assets.py 的 geometry 组）：
 * - 所有函数与坐标一律写成 JS 数值/函数入参，不出现字符串形式的数学表达式
 *   （字符串表达式在无 unsafe-eval 的 CSP 下会失败）；
 * - 不使用 Math.random（演示必须可重放）；
 * - 预设只依赖数值参数对象 p（组件在可编辑模式挂滑杆维护它，只读模式用保存值），
 *   动态元素全部用函数父项声明，滑杆拖动后整板自动重算，无需手动 update。
 *
 * 预设结构：
 *   { id, zh, en, bounding: [xmin, ymax, xmax, ymin],
 *     params: [{ name, zh, en, value, min, max, step }],
 *     i18n: { zh: {...}, en: {...} },   // 板上文案（读数/说明）
 *     build(board, JXG, p, lang) }      // 返回 undefined；元素挂在 board 上
 */

export const GEOMETRY_PRESETS = Object.freeze([
  {
    id: 'conic-unified',
    zh: '圆锥曲线的统一定义',
    en: 'Unified definition of conics',
    bounding: [-5, 4.5, 6, -3.5],
    params: [
      { name: 'e', zh: '离心率 e', en: 'Eccentricity e', value: 0.6, min: 0.2, max: 2.8, step: 0.05 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { focus: '焦点', directrix: '准线', distF: '|PF|', distD: 'P 到准线的距离' },
        en: { focus: 'Focus', directrix: 'Directrix', distF: '|PF|', distD: 'Distance from P to directrix' },
      }[lang];
      const C = 1.5;
      const D = 1.5; // 准线 x = -D
      board.create('line', [[-D, -3], [-D, 3]], { straightFirst: false, straightLast: false, dash: 2, strokeColor: '#7a7a7a' });
      const focus = board.create('point', [C, 0], { name: 'F(' + L.focus + ')', fixed: true, fillColor: '#c0392b' });
      const radius = t => {
        const denominator = 1 - p.e * Math.cos(t);
        return denominator < 0.08 ? NaN : (p.e * (C + D)) / denominator;
      };
      const curve = board.create('curve', [
        t => C + radius(t) * Math.cos(t),
        t => radius(t) * Math.sin(t),
        -Math.PI, Math.PI,
      ], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const P = board.create('glider', [3.2, 1.6, curve], { name: 'P', fillColor: '#2f6fed' });
      const foot = board.create('point', [() => -D, () => P.Y()], { name: '', size: 1, strokeColor: '#7a7a7a' });
      board.create('segment', [P, foot], { dash: 2, strokeColor: '#7a7a7a' });
      board.create('segment', [P, focus], { strokeColor: '#c0392b' });
      board.create('text', [-4.8, -2.6, () => `${L.distF} = ${Math.hypot(P.X() - C, P.Y()).toFixed(2)}`], { fontSize: 13 });
      board.create('text', [-4.8, -3.1, () => `${L.distD} = ${(P.X() + D).toFixed(2)}　e = ${p.e.toFixed(2)}`], { fontSize: 13 });
    },
  },
  {
    id: 'ellipse-def1',
    zh: '椭圆的第一定义',
    en: 'First definition of the ellipse',
    bounding: [-6, 5, 6, -4],
    params: [
      { name: 'c', zh: '半焦距 c', en: 'Linear eccentricity c', value: 2, min: 1, max: 3, step: 0.1 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { sum: '|PF₁| + |PF₂|', hint: '（定值 2a = 8）' },
        en: { sum: '|PF₁| + |PF₂|', hint: '(constant 2a = 8)' },
      }[lang];
      const A = 4;
      const semiB = () => Math.sqrt(Math.max(0.01, A * A - p.c * p.c));
      const F1 = board.create('point', [() => -p.c, 0], { name: 'F₁', fillColor: '#c0392b' });
      const F2 = board.create('point', [() => p.c, 0], { name: 'F₂', fillColor: '#c0392b' });
      const curve = board.create('curve', [t => A * Math.cos(t), t => semiB() * Math.sin(t), 0, 2 * Math.PI], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const P = board.create('glider', [2.4, 2.7, curve], { name: 'P', fillColor: '#2f6fed' });
      board.create('segment', [P, F1], { strokeColor: '#c0392b' });
      board.create('segment', [P, F2], { strokeColor: '#c0392b' });
      board.create('point', [-A, 0], { name: 'A₁', fixed: true, size: 2 });
      board.create('point', [A, 0], { name: 'A₂', fixed: true, size: 2 });
      const dist = point => () => Math.hypot(P.X() - point.X(), P.Y() - point.Y());
      board.create('text', [-5.6, -3.2, () => `${L.sum} = ${(dist(F1)() + dist(F2)()).toFixed(2)}　${L.hint}`], { fontSize: 13 });
    },
  },
  {
    id: 'hyperbola-def1',
    zh: '双曲线的第一定义',
    en: 'First definition of the hyperbola',
    bounding: [-6, 5, 6, -4],
    params: [
      { name: 'c', zh: '半焦距 c', en: 'Linear eccentricity c', value: 3, min: 2, max: 4, step: 0.1 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { diff: '‖PF₁| − |PF₂‖', hint: '（定值 2a = 3）' },
        en: { diff: '||PF₁| − |PF₂||', hint: '(constant 2a = 3)' },
      }[lang];
      const A = 1.5;
      const semiB = () => Math.sqrt(Math.max(0.01, p.c * p.c - A * A));
      const F1 = board.create('point', [() => -p.c, 0], { name: 'F₁', fillColor: '#c0392b' });
      const F2 = board.create('point', [() => p.c, 0], { name: 'F₂', fillColor: '#c0392b' });
      const cosh = t => (Math.exp(t) + Math.exp(-t)) / 2;
      const sinh = t => (Math.exp(t) - Math.exp(-t)) / 2;
      const branch1 = board.create('curve', [t => A * cosh(t), t => semiB() * sinh(t), -2.2, 2.2], { strokeColor: '#2f6fed', strokeWidth: 2 });
      board.create('curve', [t => -A * cosh(t), t => -semiB() * sinh(t), -2.2, 2.2], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const P = board.create('glider', [A * cosh(0.6), semiB() * sinh(0.6), branch1], { name: 'P', fillColor: '#2f6fed' });
      board.create('segment', [P, F1], { strokeColor: '#c0392b' });
      board.create('segment', [P, F2], { strokeColor: '#c0392b' });
      const dist = point => () => Math.hypot(P.X() - point.X(), P.Y() - point.Y());
      board.create('text', [-5.6, -3.2, () => `${L.diff} = ${Math.abs(dist(F1)() - dist(F2)()).toFixed(2)}　${L.hint}`], { fontSize: 13 });
    },
  },
  {
    id: 'sine-transform',
    zh: '三角函数 y = A sin(ωx + φ)',
    en: 'Sine wave y = A sin(ωx + φ)',
    bounding: [-6.8, 4.2, 6.8, -4.2],
    params: [
      { name: 'A', zh: '振幅 A', en: 'Amplitude A', value: 1, min: 0.3, max: 3, step: 0.05 },
      { name: 'w', zh: '角频率 ω', en: 'Angular frequency ω', value: 1, min: 0.3, max: 4, step: 0.05 },
      { name: 'phi', zh: '初相 φ', en: 'Phase φ', value: 0, min: -3.14, max: 3.14, step: 0.05 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { period: '周期 T = 2π/ω' },
        en: { period: 'Period T = 2π/ω' },
      }[lang];
      board.create('functiongraph', [x => Math.sin(x)], { strokeColor: '#b8b8b8', dash: 2 });
      board.create('functiongraph', [x => p.A * Math.sin(p.w * x + p.phi)], { strokeColor: '#2f6fed', strokeWidth: 2 });
      board.create('text', [-6.4, -3.6, () => `${L.period} = ${(2 * Math.PI / p.w).toFixed(2)}　A = ${p.A.toFixed(2)}`], { fontSize: 13 });
    },
  },
  {
    id: 'derivative-tangent',
    zh: '导数的几何意义',
    en: 'Geometric meaning of the derivative',
    bounding: [-3.2, 4.2, 2.8, -4.2],
    params: [
      { name: 'h', zh: '增量 h', en: 'Increment h', value: 0.8, min: 0.03, max: 1.5, step: 0.01 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { secant: '割线斜率', tangent: "f'(x₀)" },
        en: { secant: 'Secant slope', tangent: "f'(x₀)" },
      }[lang];
      const f = x => (x * x * x) / 3 - x;
      const df = x => x * x - 1;
      const curve = board.create('functiongraph', [f], { strokeColor: '#2f6fed', strokeWidth: 2 });
      board.create('functiongraph', [df], { strokeColor: '#b8b8b8', dash: 2 });
      const A = board.create('glider', [-1.6, f(-1.6), curve], { name: 'A', fillColor: '#2f6fed' });
      const B = board.create('point', [() => A.X() + p.h, () => f(A.X() + p.h)], { name: 'B', fillColor: '#c98a3d' });
      board.create('line', [A, B], { strokeColor: '#c98a3d', straightFirst: true, straightLast: true });
      const slope = () => (f(A.X() + p.h) - f(A.X())) / p.h;
      board.create('text', [-3, -3.6, () => `${L.secant} = ${slope().toFixed(3)}　${L.tangent} = ${df(A.X()).toFixed(3)}`], { fontSize: 13 });
    },
  },
  {
    id: 'linear-program',
    zh: '线性规划的可行域',
    en: 'Feasible region of linear programming',
    bounding: [-1.8, 8.5, 8.8, -2],
    params: [
      { name: 'a', zh: '目标系数 a', en: 'Objective a', value: 2, min: -3, max: 3, step: 0.1 },
      { name: 'b', zh: '目标系数 b', en: 'Objective b', value: 1, min: -3, max: 3, step: 0.1 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { label: '目标 z = ax + by', best: '最优值' },
        en: { label: 'Objective z = ax + by', best: 'Optimum' },
      }[lang];
      board.create('line', [[6, 0], [0, 6]], { strokeColor: '#7a7a7a', straightFirst: false, straightLast: false });
      board.create('line', [[4, 0], [0, 8]], { strokeColor: '#7a7a7a', straightFirst: false, straightLast: false });
      // 文本内容一律函数入参:字符串内容会走 JessieCode 的 eval 路径,
      // 在无 unsafe-eval 的 CSP 下不可用(见 tests 的预设纪律断言)。
      board.create('text', [5.4, 6.4, () => 'x + y ≤ 6'], { fontSize: 12, strokeColor: '#7a7a7a' });
      board.create('text', [6.9, 7.6, () => '2x + y ≤ 8'], { fontSize: 12, strokeColor: '#7a7a7a' });
      const region = board.create('polygon', [[0, 0], [4, 0], [2, 4], [0, 6]], {
        fillColor: '#2f6fed', fillOpacity: 0.12, borders: { strokeColor: 'none' }, vertices: { size: 2 },
      });
      const M = board.create('point', [2, 2], { name: '', size: 1, fixed: true, visible: false });
      const N = board.create('point', [() => 2 + p.b, () => 2 - p.a], { name: '', size: 1, visible: false });
      board.create('line', [M, N], { strokeColor: '#c0392b', straightFirst: true, straightLast: true });
      const vertices = [[0, 0], [4, 0], [2, 4], [0, 6]];
      board.create('text', [-1.5, -1.5, () => {
        let best = vertices[0];
        let bestValue = -Infinity;
        for (const [x, y] of vertices) {
          const value = p.a * x + p.b * y;
          if (value > bestValue) { bestValue = value; best = [x, y]; }
        }
        return `${L.label}：${L.best} z = ${bestValue.toFixed(1)} @ (${best[0]}, ${best[1]})`;
      }], { fontSize: 13 });
      return { region };
    },
  },
  {
    id: 'quadratic-coeff',
    zh: '二次函数 y = ax² + bx + c',
    en: 'Quadratic y = ax² + bx + c',
    bounding: [-5.2, 6.5, 5.2, -6.5],
    params: [
      { name: 'a', zh: '系数 a', en: 'Coefficient a', value: 1, min: -2, max: 2, step: 0.1 },
      { name: 'b', zh: '系数 b', en: 'Coefficient b', value: -2, min: -4, max: 4, step: 0.1 },
      { name: 'c', zh: '系数 c', en: 'Coefficient c', value: 1, min: -4, max: 4, step: 0.1 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: {
          vertex: '顶点', disc: '判别式 Δ = b² − 4ac',
          linear: 'a ≈ 0：图像是一次函数（直线），无顶点与对称轴。',
        },
        en: {
          vertex: 'Vertex', disc: 'Discriminant Δ = b² − 4ac',
          linear: 'a ≈ 0: the graph is a line; no vertex or axis of symmetry.',
        },
      }[lang];
      // a 接近 0 时不再是二次函数：隐藏顶点与对称轴，只提示一次函数（审查 M3）。
      const isQuadratic = () => Math.abs(p.a) >= 0.05;
      const safeA = () => (isQuadratic() ? p.a : 0.05);
      board.create('functiongraph', [x => p.a * x * x + p.b * x + p.c], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const V = board.create('point', [() => -p.b / (2 * safeA()), () => p.c - (p.b * p.b) / (4 * safeA())],
        { name: 'V', fillColor: '#c0392b', visible: isQuadratic });
      const W = board.create('point', [() => V.X(), () => V.Y() + 1], { name: '', size: 1, visible: false });
      board.create('line', [V, W], { strokeColor: '#b8b8b8', dash: 2, straightFirst: true, straightLast: true, visible: isQuadratic });
      board.create('text', [-4.8, -5.8, () => (isQuadratic()
        ? `${L.vertex} (${V.X().toFixed(2)}, ${V.Y().toFixed(2)})　${L.disc} = ${(p.b * p.b - 4 * p.a * p.c).toFixed(2)}`
        : L.linear)], { fontSize: 13 });
    },
  },
  {
    id: 'exp-log',
    zh: '指数与对数互为反函数',
    en: 'Exponential and logarithm as inverses',
    bounding: [-4.2, 6, 6.2, -4.2],
    params: [
      { name: 'a', zh: '底数 a', en: 'Base a', value: 2, min: 0.15, max: 3.5, step: 0.05 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { note: 'a ≈ 1 时对数无定义', pair: '两图象关于 y = x 对称' },
        en: { note: 'log undefined when a ≈ 1', pair: 'Mirror images across y = x' },
      }[lang];
      board.create('functiongraph', [x => Math.pow(p.a, x)], { strokeColor: '#2f6fed', strokeWidth: 2 });
      board.create('curve', [x => x, x => (Math.abs(p.a - 1) < 0.02 || x <= 0 ? NaN : Math.log(x) / Math.log(p.a)), 0.001, 40], { strokeColor: '#c98a3d', strokeWidth: 2 });
      board.create('functiongraph', [x => x], { strokeColor: '#b8b8b8', dash: 2 });
      board.create('text', [-3.8, -3.6, () => (Math.abs(p.a - 1) < 0.02 ? L.note : `${L.pair}（a = ${p.a.toFixed(2)}）`)], { fontSize: 13 });
    },
  },
  {
    id: 'unit-circle-sine',
    zh: '单位圆与正弦曲线',
    en: 'Unit circle and the sine curve',
    bounding: [-3.8, 2.2, 7.2, -2.2],
    params: [
      { name: 't', zh: '旋转角 t（弧度）', en: 'Angle t (radians)', value: 0.8, min: 0, max: 6.28, step: 0.05 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { sine: 'sin t = ' },
        en: { sine: 'sin t = ' },
      }[lang];
      const centerX = -2.2;
      const center = board.create('point', [centerX, 0], { name: '', fixed: true, size: 1 });
      const circle = board.create('circle', [center, 1], { strokeColor: '#7a7a7a' });
      const P = board.create('point', [() => centerX + Math.cos(p.t), () => Math.sin(p.t)], { name: 'P', fillColor: '#2f6fed' });
      board.create('segment', [center, P], { strokeColor: '#2f6fed' });
      board.create('curve', [x => x, x => Math.sin(x), 0, 2 * Math.PI], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const Q = board.create('point', [() => p.t, () => Math.sin(p.t)], { name: '', size: 2, fillColor: '#c0392b' });
      board.create('segment', [[centerX, () => Math.sin(p.t)], Q], { dash: 2, strokeColor: '#c0392b' });
      board.create('segment', [P, [centerX, () => Math.sin(p.t)]], { dash: 2, strokeColor: '#c0392b' });
      board.create('text', [4.6, -1.8, () => `t = ${p.t.toFixed(2)}　${L.sine}${Math.sin(p.t).toFixed(2)}`], { fontSize: 13 });
      return { circle, P, Q };
    },
  },
  {
    id: 'circle-line-pos',
    zh: '直线与圆的位置关系',
    en: 'Position of a line and a circle',
    bounding: [-5.2, 5.2, 5.2, -5.2],
    params: [
      { name: 'r', zh: '半径 r', en: 'Radius r', value: 1.5, min: 0.5, max: 3, step: 0.05 },
      { name: 'k', zh: '斜率 k', en: 'Slope k', value: 0.5, min: -1.5, max: 1.5, step: 0.05 },
      { name: 'm', zh: '截距 m', en: 'Intercept m', value: 2, min: -4, max: 4, step: 0.1 },
    ],
    build(board, JXG, p, lang) {
      const L = {
        zh: { intersect: '相交', tangent: '相切', separate: '相离', distance: '圆心距 d' },
        en: { intersect: 'Intersecting', tangent: 'Tangent', separate: 'Separate', distance: 'Center distance d' },
      }[lang];
      const circle = board.create('circle', [[0, 0], () => p.r], { strokeColor: '#2f6fed', strokeWidth: 2 });
      const line = board.create('line', [[0, () => p.m], [4, () => 4 * p.k + p.m]], { strokeColor: '#c0392b' });
      board.create('intersection', [circle, line, 0], { name: '', fillColor: '#c0392b', size: 2 });
      board.create('intersection', [circle, line, 1], { name: '', fillColor: '#c0392b', size: 2 });
      board.create('text', [-4.9, -4.6, () => {
        const d = Math.abs(p.m) / Math.sqrt(1 + p.k * p.k);
        const state = Math.abs(d - p.r) < 0.03 ? L.tangent : (d < p.r ? L.intersect : L.separate);
        return `${L.distance} = ${d.toFixed(2)}　r = ${p.r.toFixed(2)}　→ ${state}`;
      }], { fontSize: 13 });
    },
  },
]);

/** 按预设 id 查找；未知返回 null。 */
export function findGeometryPreset(id) {
  const presetId = String(id || '').trim();
  return GEOMETRY_PRESETS.find(preset => preset.id === presetId) || null;
}

/** 规范化参数对象：只保留该预设声明的参数，数值非法回退默认并收敛到范围。
 *  仅接受真数值或可完整解析为有限数的非空字符串——null/空串/数组/布尔
 *  一律回退默认值，不做隐式转换（审查 M2）。 */
export function normalizeGeometryParams(preset, raw) {
  const values = {};
  const source = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  for (const param of preset.params) {
    const rawValue = source[param.name];
    let number = NaN;
    if (typeof rawValue === 'number') {
      number = rawValue;
    } else if (typeof rawValue === 'string' && rawValue.trim() !== '') {
      const parsed = Number(rawValue);
      number = Number.isFinite(parsed) ? parsed : NaN;
    }
    const fallback = Number.isFinite(param.value) ? param.value : param.min;
    values[param.name] = Number.isFinite(number)
      ? Math.min(param.max, Math.max(param.min, number))
      : fallback;
  }
  return values;
}
