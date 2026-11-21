/**
 * 原理演示离线预设（方案 §8 工单 D：一个干净流程、一个等距结构、一个最小 Three.js 视图）。
 *
 * 数据约束：
 * - 全部为本地常量，不含任何外部请求、eval 或用户输入；播放器只读这些数据绘制。
 * - 标题/步骤/部件名是演示内容（教学数据），界面控件文案由调用方用 t() 提供。
 * - 几何尺寸、坐标与数值均为「课堂教学示意」，不是厂商图纸，也不是工程精度数据；
 *   每个预设都带 source / assumptions 字段，页面必须原样展示出处与假设。
 * - 为了可重复演示，所有数值固定、无随机数。
 */

/** 统一出处：本地教学示意（页面必须原样展示，不得声称工程精度）。 */
export const PRESET_SOURCE = '本地教学示意（非厂商图纸）';

/** M4 新预设统一出处：同时声明「非厂商图纸 / 非教材原图」，页面必须原样展示。 */
export const PRESET_SOURCE_TEACHING = '本地教学示意（非厂商图纸/非教材原图）';

/* ------------------------------------------------------------------------- *
 * 预设一：齿轮传动原理流程（flow_animation）
 * 干净流程：动力输入 → 小齿轮 → 齿面啮合 → 大齿轮 → 降速增矩 → 输出；
 * 另含「过载 → 停机检查 → 复位重启」分支与回环，用于验证流程播放器。
 * ------------------------------------------------------------------------- */

const GEAR_FLOW_PRESET = {
  id: 'flow-gear-transmission',
  kind: 'flow_animation',
  title: '齿轮传动原理流程',
  source: PRESET_SOURCE,
  assumptions: [
    '齿轮按直齿圆柱齿轮简化，忽略摩擦、弹性变形与轴承损耗。',
    '传动比只写 i = z₂ / z₁ 的定性关系，不给出具体齿数。',
    '过载分支只说明保护逻辑，不代表真实保护装置的整定参数。',
    '流程节点与边为教学整理，不是某台设备的实际控制程序。',
  ],
  data: {
    // 节点坐标为教学排版坐标（渲染器按 SVG 像素使用，缺省时自动分层）。
    nodes: [
      { id: 'input', label: '动力输入', kind: 'start', x: 150, y: 110 },
      { id: 'pinion', label: '小齿轮转动', kind: 'process', x: 330, y: 110 },
      { id: 'mesh', label: '齿面啮合', kind: 'process', x: 510, y: 110 },
      { id: 'gear', label: '大齿轮转动', kind: 'process', x: 690, y: 110 },
      { id: 'ratio', label: '降速增矩', kind: 'process', x: 870, y: 110 },
      { id: 'output', label: '输出动力', kind: 'end', x: 430, y: 300 },
      { id: 'check', label: '是否过载？', kind: 'branch', x: 690, y: 300 },
      { id: 'stop', label: '停机检查', kind: 'process', x: 870, y: 300 },
    ],
    edges: [
      { id: 'e1', from: 'input', to: 'pinion', label: '输入转矩' },
      { id: 'e2', from: 'pinion', to: 'mesh', label: '轮齿接触' },
      { id: 'e3', from: 'mesh', to: 'gear', label: '圆周力' },
      { id: 'e4', from: 'gear', to: 'ratio', label: 'i = z₂ / z₁' },
      { id: 'e5', from: 'ratio', to: 'check', label: '负载监测' },
      { id: 'e6', from: 'check', to: 'output', label: '正常' },
      { id: 'e7', from: 'check', to: 'stop', label: '过载' },
      // 回环边：停机检查后复位，动力重新从输入开始。
      { id: 'e8', from: 'stop', to: 'input', label: '复位重启' },
    ],
    steps: [
      {
        label: '动力输入',
        detail: '电机（或人力）的转矩经输入轴进入减速箱。',
        node_id: 'input',
        node_ids: ['input'],
        edge_ids: ['e1'],
      },
      {
        label: '小齿轮转动',
        detail: '输入轴带动小齿轮（齿数 z₁）旋转。',
        node_id: 'pinion',
        node_ids: ['pinion'],
        edge_ids: ['e2'],
      },
      {
        label: '齿面啮合',
        detail: '主动轮齿面推动从动轮齿面，把圆周力传给下一级。',
        node_id: 'mesh',
        node_ids: ['mesh'],
        edge_ids: ['e3'],
      },
      {
        label: '大齿轮转动',
        detail: '大齿轮（齿数 z₂ > z₁）被带动，转速按齿数比下降。',
        node_id: 'gear',
        node_ids: ['gear'],
        edge_ids: ['e4'],
      },
      {
        label: '降速增矩',
        detail: '转速降低、转矩增大；理想情况下 i = z₂ / z₁。',
        node_id: 'ratio',
        node_ids: ['ratio'],
        edge_ids: ['e5'],
      },
      {
        label: '负载正常则输出',
        detail: '过载监测判定正常时，动力继续传到输出轴。',
        node_id: 'check',
        node_ids: ['check', 'output'],
        edge_ids: ['e6'],
      },
      {
        label: '过载则停机检查',
        detail: '负载过大时触发保护，停机检查齿面与轴承。',
        node_id: 'check',
        node_ids: ['check', 'stop'],
        edge_ids: ['e7'],
      },
      {
        label: '复位后重新启动（回环）',
        detail: '排除故障后复位，动力重新从输入轴进入，形成回环。',
        node_id: 'stop',
        node_ids: ['stop', 'input'],
        edge_ids: ['e8'],
      },
    ],
    stepIntervalMs: 1200,
    notes: [
      '演示为教学示意：齿轮副按直齿圆柱齿轮简化，忽略摩擦、弹性变形与轴承损耗。',
      '过载分支只说明逻辑，不代表真实保护装置的整定参数。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设二：二级齿轮减速箱等距结构（isometric）
 * 9 个部件：箱体 / 箱盖 / 输入轴 / 小齿轮 / 中间轴 / 大齿轮 / 输出轴 /
 * 输入轴承座 / 输出轴承座；步骤为「拆解 4 步 + 装配 4 步」。
 * 等距投影公式见 iso.js：screenX=(x-z)*cos30, screenY=(x+z)*sin30-y。
 * ------------------------------------------------------------------------- */

const GEARBOX_ISO_PRESET = {
  id: 'iso-gearbox',
  kind: 'isometric',
  title: '二级齿轮减速箱等距结构',
  source: PRESET_SOURCE,
  assumptions: [
    '箱体、齿轮与轴按教学需要简化成长方体近似，不等于真实零件几何。',
    '只表现拆装顺序与啮合关系，未绘制轴承滚子、油封、润滑与紧固件。',
    '拆解方向按「箱盖向上、各轴组件向外」的教学方向设定，不保证真实维修工艺。',
    '装配步骤用于核对顺序，不涉及配合公差与拧紧力矩。',
  ],
  data: {
    // 1 个坐标单位 = unit 像素；x/z 为水平面，y 为竖直方向（向上为正）。
    unit: 26,
    parts: [
      { id: 'housing', label: '箱体', x: 0, y: 1.4, z: 0, w: 10.4, h: 2.8, d: 6.4, color: '#8fa7ae', opacity: 0.52, shell: true, explode: [0, 0, 0], description: '固定各轴与轴承座的基准件。' },
      { id: 'cover', label: '箱盖', x: 0, y: 3.25, z: 0, w: 10.4, h: 0.5, d: 6.4, color: '#a9bdc2', opacity: 0.6, shell: true, explode: [0, 2.4, 0], description: '合箱后封闭箱体；拆解时先取下。' },
      { id: 'input-shaft', label: '输入轴', x: -3.1, y: 1.8, z: 0, w: 0.5, h: 0.5, d: 8, color: '#b7c2c6', explode: [0, 0.8, 2.2], description: '接收外部转矩，带动小齿轮。' },
      { id: 'pinion', label: '小齿轮', x: -3.1, y: 1.8, z: 1.7, w: 2.2, h: 2.2, d: 0.7, color: '#d9a45c', explode: [0, 0.8, 2.2], description: '第一级主动轮（齿数少、转速高）。' },
      { id: 'intermediate-shaft', label: '中间轴', x: 0, y: 1.7, z: 0, w: 0.55, h: 0.55, d: 8, color: '#b7c2c6', explode: [0, 2, 0], description: '同时装大齿轮与小齿轮，完成两级传动。' },
      { id: 'large-gear', label: '大齿轮', x: 0, y: 1.7, z: -1.4, w: 3.4, h: 3.4, d: 0.9, color: '#c98645', explode: [0, 2, 0], description: '第一级从动轮：转速下降、转矩增大。' },
      { id: 'output-shaft', label: '输出轴', x: 3.1, y: 1.8, z: 0, w: 0.5, h: 0.5, d: 8, color: '#b7c2c6', explode: [0, 0.8, -2.2], description: '把降速增矩后的动力输出。' },
      { id: 'bearing-input', label: '输入轴承座', x: -3.1, y: 1.8, z: 3.1, w: 1.3, h: 1.3, d: 0.6, color: '#6e8790', explode: [-1.2, 0.5, 2.6], description: '支承输入轴，拆装时最先松开。' },
      { id: 'bearing-output', label: '输出轴承座', x: 3.1, y: 1.8, z: -3.1, w: 1.3, h: 1.3, d: 0.6, color: '#6e8790', explode: [1.2, 0.5, -2.6], description: '支承输出轴。' },
    ],
    steps: [
      { phase: 'disassemble', label: '卸下箱盖与输入轴承座', detail: '先松开箱盖与输入侧轴承座，露出箱内组件。', part_ids: ['cover', 'bearing-input'] },
      { phase: 'disassemble', label: '取出输入轴与小齿轮', detail: '沿轴线抽出输入轴组件，第一级主动轮随轴取出。', part_ids: ['input-shaft', 'pinion'] },
      { phase: 'disassemble', label: '取出中间轴与大齿轮', detail: '向上取出中间轴组件，第一级从动轮与大齿轮一同离开箱体。', part_ids: ['intermediate-shaft', 'large-gear'] },
      { phase: 'disassemble', label: '取下输出轴与输出轴承座', detail: '沿另一侧取出输出轴与轴承座，拆解完成。', part_ids: ['output-shaft', 'bearing-output'] },
      { phase: 'assemble', label: '装回中间轴与大齿轮', detail: '按拆解的相反顺序先装中间轴组件，注意齿面啮合位置。', part_ids: ['intermediate-shaft', 'large-gear'] },
      { phase: 'assemble', label: '装回输出轴与输出轴承座', detail: '装入输出轴并对准轴承座，检查转动是否顺畅。', part_ids: ['output-shaft', 'bearing-output'] },
      { phase: 'assemble', label: '装回输入轴与小齿轮', detail: '装入输入轴组件，使小齿轮与中间轴大齿轮正确啮合。', part_ids: ['input-shaft', 'pinion'] },
      { phase: 'assemble', label: '合上箱盖并装回轴承座', detail: '合箱、装回输入轴承座，装配完成后再手动盘车检查。', part_ids: ['cover', 'bearing-input'] },
    ],
    notes: [
      '箱体/箱盖用半透明等距画法表示「剖开看内部」，不是真实剖视图。',
      '拆解与装配各 4 步，步骤仅演示顺序，不代表维修工时与工艺要求。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设三：二级齿轮减速箱最小 3D 装配视图（demo_3d）
 * 6 个 Box 部件 + 3 步装配，用于验证 3D 分支、首帧回执与无 WebGL 降级。
 * 无 WebGL / three 加载失败时由 principleDemo 自动改用同一份数据的等距示意图。
 * ------------------------------------------------------------------------- */

const GEARBOX_THREE_PRESET = {
  id: 'three-gearbox',
  kind: 'demo_3d',
  title: '二级齿轮减速箱 3D 装配（最小示例）',
  source: PRESET_SOURCE,
  assumptions: [
    '3D 视图只保留 6 个 Box 部件，用于验证 Three.js 分支与降级，不是完整设备模型。',
    '材质为纯色漫反射示意，未做贴图、阴影与物理渲染。',
    '装配动画只演示部件就位顺序，不计算碰撞与配合。',
  ],
  data: {
    // parts 结构与等距预设一致：x/z 水平、y 向上，单位与相机尺度一致。
    parts: [
      { id: 'housing', label: '箱体', x: 0, y: 1.2, z: 0, w: 9, h: 2.4, d: 5.6, color: '#4f6b74', opacity: 0.3, explode: [0, 0, 0], description: '固定各部件的底座（半透明以便看到内部轴与齿轮）。' },
      { id: 'cover', label: '箱盖', x: 0, y: 2.65, z: 0, w: 9, h: 0.5, d: 5.6, color: '#6d8790', opacity: 0.4, explode: [0, 1.4, 0], description: '最后合上，封闭箱体。' },
      { id: 'input-shaft', label: '输入轴', x: -2.4, y: 1.2, z: 0, w: 0.45, h: 0.45, d: 6.4, color: '#b7c2c6', explode: [-1.8, 0.9, 1.6], description: '第一级动力输入。' },
      { id: 'pinion', label: '小齿轮', x: -2.4, y: 1.2, z: 1.2, w: 2, h: 2, d: 0.6, color: '#d9a45c', explode: [-1.8, 0.9, 1.6], description: '第一级主动轮。' },
      { id: 'large-gear', label: '大齿轮', x: 0.6, y: 1.2, z: -0.8, w: 2.8, h: 2.8, d: 0.7, color: '#c98645', explode: [0, 0.6, -2.2], description: '第一级从动轮，与小齿轮啮合。' },
      { id: 'output-shaft', label: '输出轴', x: 2.6, y: 1.2, z: 0, w: 0.45, h: 0.45, d: 6.4, color: '#b7c2c6', explode: [1.8, 0.9, 1.6], description: '动力输出轴。' },
    ],
    steps: [
      { label: '安装输入轴与小齿轮', detail: '把输入轴组件移到箱体内，与小齿轮一起就位。', part_ids: ['input-shaft', 'pinion'] },
      { label: '安装大齿轮与输出轴', detail: '装入大齿轮与输出轴，检查齿面啮合关系。', part_ids: ['large-gear', 'output-shaft'] },
      { label: '合上箱盖', detail: '合上箱盖完成装配；实际设备还需要轴承、油封与紧固件。', part_ids: ['cover'] },
    ],
    notes: [
      '这是「最小 Three.js 视图」示例：只有 6 个 Box 部件，用来验证 3D 分支与降级。',
      '无 WebGL 或 three.js 加载失败时会自动切换为等距示意图。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设四：错题复习闭环手绘流程（flow_animation，loop + sketch）
 * 「答错 → 归因 → 变式练习 → 独立复述 → 四档评分 → 记录证据 → 排期 → 到期复习」，
 * 两条回边（未掌握回环、到期复习回环）走底部通道，用于验证 loop 布局与手绘风格。
 * ------------------------------------------------------------------------- */

const REVIEW_LOOP_FLOW_PRESET = {
  id: 'flow-review-loop',
  kind: 'flow_animation',
  title: '错题复习闭环（手绘流程）',
  source: PRESET_SOURCE_TEACHING,
  subjectHints: ['物理', '机械', '科学'],
  assumptions: [
    '流程按「错题 → 归因 → 变式 → 复述 → 四档评分 → 排期 → 到期复习」的教学整理，不是平台实际调度算法。',
    '四档评分沿用错题复习的四档口径（未掌握/初步掌握/比较掌握/熟练掌握），评分由学生自评，不代表自动判分。',
    '排期间隔为定性描述，具体间隔由复习排程模块按四档规则计算。',
    '手绘风格为确定性伪随机抖动，同一份数据每次重放与导出的海报完全一致。',
  ],
  data: {
    layout: 'loop',
    style: 'sketch',
    // 节点坐标为教学排版坐标（SVG 像素）；row1 从左到右，row2 承接评分结果。
    nodes: [
      { id: 'wrong', label: '答错', kind: 'start', x: 150, y: 110 },
      { id: 'attribute', label: '错因归类', kind: 'process', x: 340, y: 110 },
      { id: 'variant', label: '变式练习', kind: 'process', x: 530, y: 110 },
      { id: 'restate', label: '独立复述', kind: 'process', x: 720, y: 110 },
      { id: 'rate', label: '四档评分', kind: 'branch', x: 930, y: 110 },
      { id: 'evidence', label: '记录证据', kind: 'process', x: 930, y: 300 },
      { id: 'schedule', label: '排期到期', kind: 'process', x: 1110, y: 300 },
      { id: 'due', label: '到期复习', kind: 'end', x: 1110, y: 470 },
    ],
    edges: [
      { id: 'r1', from: 'wrong', to: 'attribute', label: '先定位错因', kind: 'solid' },
      { id: 'r2', from: 'attribute', to: 'variant', label: '针对薄弱点', kind: 'solid' },
      { id: 'r3', from: 'variant', to: 'restate', label: '不看答案复述', kind: 'dashed' },
      { id: 'r4', from: 'restate', to: 'rate', label: '按四档自评', kind: 'solid' },
      { id: 'r5', from: 'rate', to: 'evidence', label: '给出档位', kind: 'solid' },
      { id: 'r6', from: 'evidence', to: 'schedule', label: '写入学习证据', kind: 'solid' },
      { id: 'r7', from: 'schedule', to: 'due', label: '按间隔排期', kind: 'solid' },
      // 两条回边：走底部通道并自动分道，避免与节点重叠。
      { id: 'r8', from: 'due', to: 'attribute', label: '到期后再归因（回环）', kind: 'feedback' },
      { id: 'r9', from: 'rate', to: 'attribute', label: '未掌握：回到归因', kind: 'feedback' },
    ],
    steps: [
      {
        label: '答错先归因',
        detail: '答错后先不急着重做，先判断错在概念、审题还是计算，再进入针对性的变式练习。',
        node_id: 'wrong',
        node_ids: ['wrong', 'attribute'],
        edge_ids: ['r1'],
      },
      {
        label: '针对薄弱点做变式',
        detail: '按归因结果选变式题，只改变一个条件，确认同一思路能否迁移。',
        node_id: 'variant',
        node_ids: ['variant'],
        edge_ids: ['r2'],
      },
      {
        label: '不看答案独立复述',
        detail: '合上答案，用自己的话复述关键步骤；复述卡住的地方就是下一轮归因重点。',
        node_id: 'restate',
        node_ids: ['restate'],
        edge_ids: ['r3'],
      },
      {
        label: '四档自评',
        detail: '按未掌握 / 初步掌握 / 比较掌握 / 熟练掌握四档自评，档位决定后续排期。',
        node_id: 'rate',
        node_ids: ['rate'],
        edge_ids: ['r4'],
      },
      {
        label: '未掌握则回环重做',
        detail: '评为未掌握时从归因重新开始，形成内层回环，避免只刷题不复述。',
        node_id: 'attribute',
        node_ids: ['rate', 'attribute'],
        edge_ids: ['r9'],
      },
      {
        label: '记录学习证据',
        detail: '记录本次错因、变式结果与自评档位，作为复习与学情的证据。',
        node_id: 'evidence',
        node_ids: ['evidence'],
        edge_ids: ['r5', 'r6'],
      },
      {
        label: '排期到下次到期',
        detail: '按四档规则排出下次到期时间，等待错题复习队列提醒。',
        node_id: 'schedule',
        node_ids: ['schedule'],
        edge_ids: ['r7'],
      },
      {
        label: '到期复习后再次归因',
        detail: '到期复习时重新做一遍并回到归因步骤，形成外层回环，直到熟练掌握。',
        node_id: 'due',
        node_ids: ['due', 'attribute'],
        edge_ids: ['r8'],
      },
    ],
    stepIntervalMs: 1400,
    notes: [
      '演示为教学整理：四档评分口径与排期规则以错题复习模块的说明为准。',
      '手绘抖动由数据 id 决定，同一份数据每次重放、导出的海报完全一致。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设五：函数概念发展时间轴（flow_animation，timeline + clean）
 * 水平时间轴 + 刻度 + 事件卡片，steps 与节点按 order 对应。
 * ------------------------------------------------------------------------- */

const FUNCTION_TIMELINE_FLOW_PRESET = {
  id: 'flow-function-timeline',
  kind: 'flow_animation',
  title: '函数概念发展时间轴',
  source: PRESET_SOURCE_TEACHING,
  subjectHints: ['数学', '科学'],
  assumptions: [
    '时间轴为教学梳理，年代与人物归属采用通行说法，不逐条对应原始文献。',
    '只列出函数概念发展的关键节点，省略同时期其他数学分支的贡献。',
    '「现代定义」按集合论中的映射表述，不展开有序对的具体形式化细节。',
    '卡片按教学顺序排列，不代表严格的纪年刻度比例。',
  ],
  data: {
    layout: 'timeline',
    style: 'clean',
    axisLabel: '时间（教学梳理，非等距刻度）→',
    nodes: [
      { id: 'ancient', label: '古希腊：对应思想', kind: 'start' },
      { id: 'analytic', label: '17 世纪：解析几何', kind: 'event' },
      { id: 'calculus', label: '17 世纪：微积分', kind: 'event' },
      { id: 'notation', label: '18 世纪：函数记号', kind: 'event' },
      { id: 'dirichlet', label: '19 世纪：对应定义', kind: 'event' },
      { id: 'modern', label: '20 世纪：集合与映射', kind: 'end' },
    ],
    edges: [],
    steps: [
      {
        label: '古希腊：对应思想',
        detail: '古希腊数学家研究曲线与比例关系，用「对应」描述天体与几何对象，尚未出现「函数」一词。',
        node_id: 'ancient',
      },
      {
        label: '17 世纪：解析几何',
        detail: '笛卡尔与费马用坐标把几何曲线与方程对应起来，为「变量之间的关系」提供表达工具。',
        node_id: 'analytic',
      },
      {
        label: '17 世纪：微积分',
        detail: '牛顿、莱布尼茨研究变化率与累积量，函数作为「随变量变化的量」被系统使用。',
        node_id: 'calculus',
      },
      {
        label: '18 世纪：函数记号',
        detail: '欧拉引入 f(x) 记号，函数从曲线附属物变成可以独立运算的研究对象。',
        node_id: 'notation',
      },
      {
        label: '19 世纪：对应定义',
        detail: '狄利克雷给出不依赖解析式的对应定义：每个自变量对应唯一的函数值。',
        node_id: 'dirichlet',
      },
      {
        label: '20 世纪：集合与映射',
        detail: '集合论把函数定义为有序对集合（映射），形成今天教材中的现代定义。',
        node_id: 'modern',
      },
    ],
    stepIntervalMs: 1500,
    notes: [
      '时间轴为教学梳理，人物与年代采用通行说法，不逐条对应原始文献。',
      '卡片间距相同，只表示先后顺序，不是严格的时间刻度比例。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设六：光合作用光反应/暗反应状态机（flow_animation，state + sketch）
 * 7 个状态节点 + 8 条带标签迁移边，验证 state 布局（圆形状态 + 迁移标签 + 高亮）。
 * ------------------------------------------------------------------------- */

const PHOTOSYNTHESIS_STATE_FLOW_PRESET = {
  id: 'flow-photosynthesis-states',
  kind: 'flow_animation',
  title: '光合作用：光反应与暗反应状态',
  source: PRESET_SOURCE_TEACHING,
  subjectHints: ['生物', '化学', '科学'],
  assumptions: [
    '状态划分按高中教材的光反应 / 暗反应两阶段简化，省略光呼吸与其他代谢支路。',
    'ATP 与 NADPH 统称「同化力」，不展开电子传递链的中间载体。',
    'C3、C5 只标出碳反应的关键中间产物，不写具体的分子数与酶。',
    '状态迁移为教学示意，不代表真实反应速率与化学计量。',
  ],
  data: {
    layout: 'state',
    style: 'sketch',
    nodes: [
      // 状态节点按闭环排布：光反应在上、碳反应在下，输出糖类放在右侧（横向铺开便于播放器展示）。
      { id: 'light', label: '光能捕获', kind: 'state', x: 620, y: 190 },
      { id: 'split', label: '水的光解', kind: 'state', x: 932, y: 275 },
      { id: 'assimil', label: '同化力生成', kind: 'state', x: 932, y: 445 },
      { id: 'fix', label: 'CO2 固定', kind: 'state', x: 620, y: 530 },
      // C3 还原单独下移，避免「C5 再生 → CO2 固定」的循环迁移边穿过该节点。
      { id: 'reduce', label: 'C3 还原', kind: 'state', x: 308, y: 445 },
      { id: 'regenerate', label: 'C5 再生', kind: 'state', x: 308, y: 275 },
      { id: 'sugar', label: '输出糖类', kind: 'end', x: 1220, y: 360 },
    ],
    edges: [
      { id: 'p1', from: 'light', to: 'split', label: '吸收光能', kind: 'solid' },
      { id: 'p2', from: 'split', to: 'assimil', label: '电子与质子', kind: 'solid' },
      { id: 'p3', from: 'assimil', to: 'fix', label: 'ATP/NADPH', kind: 'solid' },
      { id: 'p4', from: 'fix', to: 'reduce', label: '还原 C3', kind: 'solid' },
      { id: 'p5', from: 'reduce', to: 'regenerate', label: '再生 C5', kind: 'solid' },
      { id: 'p6', from: 'regenerate', to: 'fix', label: '继续固定 CO2（循环）', kind: 'feedback' },
      { id: 'p7', from: 'reduce', to: 'sugar', label: '输出糖类', kind: 'solid' },
    ],
    steps: [
      {
        label: '光能捕获',
        detail: '叶绿体中的色素吸收光能，光反应开始。',
        node_id: 'light',
        node_ids: ['light'],
        edge_ids: ['p1'],
      },
      {
        label: '水的光解',
        detail: '水在光下分解，产生氧气、电子与质子，为同化力生成提供原料。',
        node_id: 'split',
        node_ids: ['split'],
        edge_ids: ['p1', 'p2'],
      },
      {
        label: '同化力生成',
        detail: '电子传递与质子梯度驱动生成 ATP 与 NADPH，合称同化力。',
        node_id: 'assimil',
        node_ids: ['assimil'],
        edge_ids: ['p2', 'p3'],
      },
      {
        label: 'CO2 固定',
        detail: 'CO2 与 C5 结合生成 C3，进入暗反应（碳反应）。',
        node_id: 'fix',
        node_ids: ['fix'],
        edge_ids: ['p3', 'p4'],
      },
      {
        label: 'C3 还原',
        detail: 'ATP 供能、NADPH 供氢，把 C3 还原为糖类前体。',
        node_id: 'reduce',
        node_ids: ['reduce'],
        edge_ids: ['p4', 'p5'],
      },
      {
        label: 'C5 再生',
        detail: '部分还原产物经过循环再生 C5，使暗反应可以继续固定 CO2。',
        node_id: 'regenerate',
        node_ids: ['regenerate'],
        edge_ids: ['p5', 'p6'],
      },
      {
        label: '循环继续固定 CO2',
        detail: '再生的 C5 回到固定步骤形成循环；光反应的同化力持续供给暗反应。',
        node_id: 'fix',
        node_ids: ['regenerate', 'fix'],
        edge_ids: ['p6'],
      },
      {
        label: '输出糖类',
        detail: '部分还原产物输出为糖类，成为植物的有机物（图中输出糖类节点）。',
        node_id: 'sugar',
        node_ids: ['reduce', 'sugar'],
        edge_ids: ['p7'],
      },
    ],
    stepIntervalMs: 1500,
    notes: [
      '状态划分按高中教材的光反应 / 暗反应简化，省略光呼吸等支路。',
      '「同化力」为 ATP 与 NADPH 的统称，具体载体不展开。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设七：anatomy 等距机械结构（isometric，sketch）
 * 箱体 / 箱盖 / 轴（rod）/ 齿轮（gear）/ 轴承座（cylinder）等距近似，
 * 拆解 4 步 + 装配 4 步，每个部件带 description（悬停 tooltip）。
 * ------------------------------------------------------------------------- */

const ANATOMY_GEARBOX_ISO_PRESET = {
  id: 'iso-anatomy-gearbox',
  kind: 'isometric',
  title: 'anatomy 等距机械结构：齿轮箱拆装',
  source: PRESET_SOURCE_TEACHING,
  subjectHints: ['物理', '机械'],
  assumptions: [
    '箱体、齿轮与轴承座按等距教学示意简化，不等于真实零件的几何与配合。',
    '圆柱/齿轮用等距椭圆与齿块近似绘制，齿数、模数与厚度只作示意。',
    '只表现拆装顺序与啮合关系，未绘制轴承滚子、油封、润滑与紧固件。',
    '拆解方向按「先取箱盖、再取各轴组件」的教学方向设定，不保证真实维修工艺。',
  ],
  data: {
    unit: 26,
    style: 'sketch',
    parts: [
      { id: 'housing', label: '箱体', shape: 'box', x: 0, y: 1.4, z: 0, w: 10.4, h: 2.8, d: 6.4, color: '#8fa7ae', opacity: 0.5, shell: true, explode: [0, 0, 0], description: '固定各轴与轴承座的基准件；半透明画法用于「剖开看内部」。' },
      { id: 'cover', label: '箱盖', shape: 'plate', x: 0, y: 3.25, z: 0, w: 10.4, h: 0.5, d: 6.4, color: '#a9bdc2', opacity: 0.6, shell: true, explode: [0, 2.4, 0], description: '合箱后封闭箱体；拆解时先取下。' },
      { id: 'input-shaft', label: '输入轴', shape: 'rod', x: -3.1, y: 1.8, z: 0, w: 0.5, h: 0.5, d: 8, color: '#b7c2c6', explode: [0, 0.8, 2.2], description: '接收外部转矩，带动小齿轮；沿轴线抽出。' },
      { id: 'pinion', label: '小齿轮', shape: 'gear', x: -3.1, y: 1.8, z: 1.7, w: 2.2, h: 0.7, d: 2.2, color: '#d9a45c', explode: [0, 0.8, 2.2], description: '第一级主动轮：齿数少、转速高，与中间轴大齿轮啮合。' },
      { id: 'intermediate-shaft', label: '中间轴', shape: 'rod', x: 0, y: 1.7, z: 0, w: 0.55, h: 0.55, d: 8, color: '#b7c2c6', explode: [0, 2, 0], description: '同时装大齿轮与小齿轮，完成两级传动。' },
      { id: 'large-gear', label: '大齿轮', shape: 'gear', x: 0, y: 1.7, z: -1.4, w: 3.4, h: 0.9, d: 3.4, color: '#c98645', explode: [0, 2, 0], description: '第一级从动轮：转速下降、转矩增大。' },
      { id: 'output-shaft', label: '输出轴', shape: 'rod', x: 3.1, y: 1.8, z: 0, w: 0.5, h: 0.5, d: 8, color: '#b7c2c6', explode: [0, 0.8, -2.2], description: '把降速增矩后的动力输出。' },
      { id: 'bearing-input', label: '输入轴承座', shape: 'cylinder', x: -3.1, y: 1.8, z: 3.1, w: 1.4, h: 1.4, d: 1.4, color: '#6e8790', explode: [-1.2, 0.5, 2.6], description: '支承输入轴并承受径向载荷，拆装时最先松开。' },
      { id: 'bearing-output', label: '输出轴承座', shape: 'cylinder', x: 3.1, y: 1.8, z: -3.1, w: 1.4, h: 1.4, d: 1.4, color: '#6e8790', explode: [1.2, 0.5, -2.6], description: '支承输出轴，保证输出轴与齿轮同轴。' },
    ],
    labels: [
      { part_id: 'pinion', text: ['小齿轮（第一级主动轮）', '齿数少、转速高'] },
      { part_id: 'bearing-input', text: ['输入轴承座', '支承输入轴并承受径向载荷'] },
    ],
    steps: [
      { phase: 'disassemble', label: '卸下箱盖与输入轴承座', detail: '先松开箱盖与输入侧轴承座，露出箱内组件。', part_ids: ['cover', 'bearing-input'] },
      { phase: 'disassemble', label: '取出输入轴与小齿轮', detail: '沿轴线抽出输入轴组件，第一级主动轮随轴取出。', part_ids: ['input-shaft', 'pinion'] },
      { phase: 'disassemble', label: '取出中间轴与大齿轮', detail: '向上取出中间轴组件，大齿轮随中间轴离开箱体。', part_ids: ['intermediate-shaft', 'large-gear'] },
      { phase: 'disassemble', label: '取下输出轴与输出轴承座', detail: '沿另一侧取出输出轴与轴承座，拆解完成。', part_ids: ['output-shaft', 'bearing-output'] },
      { phase: 'assemble', label: '装回中间轴与大齿轮', detail: '按拆解的相反顺序先装中间轴组件，注意齿面啮合位置。', part_ids: ['intermediate-shaft', 'large-gear'] },
      { phase: 'assemble', label: '装回输出轴与输出轴承座', detail: '装入输出轴并对准轴承座，检查转动是否顺畅。', part_ids: ['output-shaft', 'bearing-output'] },
      { phase: 'assemble', label: '装回输入轴与小齿轮', detail: '装入输入轴组件，使小齿轮与中间轴大齿轮正确啮合。', part_ids: ['input-shaft', 'pinion'] },
      { phase: 'assemble', label: '合上箱盖并装回轴承座', detail: '合箱、装回输入轴承座，装配完成后再手动盘车检查。', part_ids: ['cover', 'bearing-input'] },
    ],
    notes: [
      '齿轮与轴承座用等距近似绘制（圆盘 + 齿块 / 上下椭圆 + 侧壁），不是真实零件几何。',
      '拆解与装配各 4 步，步骤仅演示顺序，不代表维修工时与工艺要求。',
    ],
  },
};

/* ------------------------------------------------------------------------- *
 * 预设八：anima 生物示例：气孔开闭（isometric，clean）
 * 保卫细胞 ×2（rod）、气孔（plate）、叶绿体（cylinder）、表皮细胞（plate），
 * 4 步单序列高亮 + 多行引线标注；结构与单位对照教材示意。
 * ------------------------------------------------------------------------- */

const BIOLOGY_STOMATA_ISO_PRESET = {
  id: 'iso-biology-stomata',
  kind: 'isometric',
  title: '气孔开闭：保卫细胞与叶绿体（生物示意）',
  source: '生物结构与单位对照教材示意（本地教学示意，非教材原图）',
  subjectHints: ['生物', '科学'],
  assumptions: [
    '生物结构与单位对照教材示意：保卫细胞、气孔、叶绿体只画形状与相对位置。',
    '保卫细胞用长条近似、叶绿体用等距圆盘近似，不代表真实细胞形态与数量。',
    '气孔开闭以「保卫细胞吸水膨胀 / 失水收缩」定性说明，省略离子通道与激素调节。',
    '各部件尺寸为教学示意比例，不是显微测量的实际尺寸。',
  ],
  data: {
    unit: 26,
    style: 'clean',
    parts: [
      { id: 'epidermis', label: '表皮细胞', shape: 'plate', x: 0, y: 0, z: 0, w: 8, h: 0.3, d: 4.6, color: '#c9d8b8', explode: [0, 0, 0], description: '叶片表皮示意，气孔由两个保卫细胞围成。' },
      { id: 'guard-left', label: '保卫细胞（左）', shape: 'rod', x: -1.6, y: 1.7, z: 0, w: 1.2, h: 3.4, d: 1.2, color: '#7fae6a', explode: [0, 0, 0], description: '吸水膨胀时弯曲，使气孔张开。' },
      { id: 'guard-right', label: '保卫细胞（右）', shape: 'rod', x: 1.6, y: 1.7, z: 0, w: 1.2, h: 3.4, d: 1.2, color: '#7fae6a', explode: [0, 0, 0], description: '失水收缩时伸直，使气孔关闭。' },
      { id: 'stoma', label: '气孔', shape: 'plate', x: 0, y: 0.3, z: 0, w: 1.8, h: 0.3, d: 1.2, color: '#4f6b53', explode: [0, 0, 0], description: 'CO₂ 进入、O₂ 与水汽排出的通道。' },
      { id: 'chloroplast-left', label: '叶绿体（左）', shape: 'cylinder', x: -1.6, y: 2.6, z: 0, w: 0.9, h: 0.5, d: 0.9, color: '#3f7f4f', explode: [0, 0, 0], description: '光合作用场所；也参与保卫细胞的渗透调节。' },
      { id: 'chloroplast-right', label: '叶绿体（右）', shape: 'cylinder', x: 1.6, y: 2.6, z: 0, w: 0.9, h: 0.5, d: 0.9, color: '#3f7f4f', explode: [0, 0, 0], description: '与左侧叶绿体共同完成光合与渗透调节示意。' },
    ],
    labels: [
      { part_id: 'guard-left', text: ['保卫细胞（左）', '吸水膨胀 → 气孔张开'] },
      { part_id: 'guard-right', text: ['保卫细胞（右）', '失水收缩 → 气孔关闭'] },
      { part_id: 'stoma', text: ['气孔', 'CO₂ 进入、O₂ 与水汽排出'] },
      { part_id: 'chloroplast-left', text: ['叶绿体（示意）', '光合作用与渗透调节'] },
      { part_id: 'epidermis', text: ['表皮细胞（示意）', '结构与单位对照教材'] },
    ],
    steps: [
      { label: '气孔关闭', detail: '保卫细胞失水收缩，气孔闭合，水汽与气体交换减少。', part_ids: ['guard-left', 'guard-right', 'stoma'] },
      { label: '保卫细胞吸水膨胀', detail: '保卫细胞吸水后膨胀弯曲，两个保卫细胞之间的间隙变大。', part_ids: ['guard-left', 'guard-right'] },
      { label: '叶绿体参与调节', detail: '叶绿体进行光合作用并参与渗透调节，为保卫细胞提供物质基础。', part_ids: ['chloroplast-left', 'chloroplast-right'] },
      { label: '气孔张开', detail: '气孔张开后 CO₂ 进入叶肉细胞，O₂ 与水汽经气孔排出。', part_ids: ['stoma', 'epidermis'] },
    ],
    notes: [
      '生物结构与单位对照教材示意：形状与相对位置为教学近似，不是显微照片或教材原图。',
      '气孔开闭只作定性说明，省略离子通道、激素与光照强度的具体影响。',
    ],
  },
};

/** 按预设 ID 索引的只读表（principleDemo 再导出为 PRESET_DEMOS）。 */
export const PRESET_DEMOS = Object.freeze({
  'flow-gear-transmission': GEAR_FLOW_PRESET,
  'iso-gearbox': GEARBOX_ISO_PRESET,
  'three-gearbox': GEARBOX_THREE_PRESET,
  'flow-review-loop': REVIEW_LOOP_FLOW_PRESET,
  'flow-function-timeline': FUNCTION_TIMELINE_FLOW_PRESET,
  'flow-photosynthesis-states': PHOTOSYNTHESIS_STATE_FLOW_PRESET,
  'iso-anatomy-gearbox': ANATOMY_GEARBOX_ISO_PRESET,
  'iso-biology-stomata': BIOLOGY_STOMATA_ISO_PRESET,
});

/** 固定顺序的预设列表（按「流程 → 等距 → 3D」分组，组内保持教学顺序）。 */
export const PRESET_LIST = Object.freeze([
  GEAR_FLOW_PRESET,
  REVIEW_LOOP_FLOW_PRESET,
  FUNCTION_TIMELINE_FLOW_PRESET,
  PHOTOSYNTHESIS_STATE_FLOW_PRESET,
  GEARBOX_ISO_PRESET,
  ANATOMY_GEARBOX_ISO_PRESET,
  BIOLOGY_STOMATA_ISO_PRESET,
  GEARBOX_THREE_PRESET,
]);

/** 取单个预设；未知 ID 返回 null，调用方自行降级，不抛错。 */
export function getPreset(id) {
  return PRESET_DEMOS[String(id || '')] || null;
}
