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

/** 按预设 ID 索引的只读表（principleDemo 再导出为 PRESET_DEMOS）。 */
export const PRESET_DEMOS = Object.freeze({
  'flow-gear-transmission': GEAR_FLOW_PRESET,
  'iso-gearbox': GEARBOX_ISO_PRESET,
  'three-gearbox': GEARBOX_THREE_PRESET,
});

/** 固定顺序的预设列表（保持「流程 → 等距 → 3D」）。 */
export const PRESET_LIST = Object.freeze([
  GEAR_FLOW_PRESET,
  GEARBOX_ISO_PRESET,
  GEARBOX_THREE_PRESET,
]);

/** 取单个预设；未知 ID 返回 null，调用方自行降级，不抛错。 */
export function getPreset(id) {
  return PRESET_DEMOS[String(id || '')] || null;
}
