/**
 * M5 机械 3D / 工厂仿真离线预设（方案 §5.4「机械与工厂」/ §8 M5）。
 *
 * 用途：后端 `GET /api/demos/templates`、`POST /api/demos/build`、
 * `POST /api/demos/simulate` 未就绪或网络失败时的本地只读兜底数据；接口可用时
 * 页面优先使用服务端返回的固定种子数据，本文件只负责「至少能演示」。
 *
 * 数据约束（硬性）：
 * - 全部为本地常量，不含外部请求、eval / new Function / 用户输入；
 * - 几何/数值均为「本地教学示意」，不是厂商图纸、不是产线实测数据，每个预设都带
 *   assumptions 与 source_note，页面必须原样展示；
 * - 固定种子：seed / params.seed 是明确写死的整数，工厂事件是一条固定事件表，
 *   前端只回放事件、不做随机模拟，也不会在运行时生成新事件；
 * - template_id 与后端模板库保持一致（gearbox-3d / pump-3d / factory-line），
 *   后端接口就绪后可直接按 template_id 对齐同主题模板。
 *
 * 数据结构与后端契约一致：
 * - mech_3d：{version, basis, seed, parts[], steps[], readouts[], camera,
 *   assumptions, source_note}
 * - factory_sim：{version, basis, seed, flow:{nodes[],edges[]}, station_labels[],
 *   metrics, events[], assumptions, source_note}
 */

/** 机械/工厂预设统一出处：页面必须原样展示，不得声称工程精度。 */
export const MECH_PRESET_SOURCE = '本地教学示意（非厂商图纸/非产线实测数据）';

/** 本地预设固定种子（与 params.seed 一起展示，便于说明「固定种子可复现」）。 */
const LOCAL_SEED = 20261121;

/* ------------------------------------------------------------------------- *
 * 预设一：齿轮减速箱（mech_3d）
 * 部件树示例：轴（rod）→ 齿轮（gear，parent 指向轴，position 为相对坐标）；
 * 步骤给出拆解顺序、高亮部件与目标 explode 比例；读数来自教学公式取值。
 * ------------------------------------------------------------------------- */

const GEARBOX_PARAMS = {
  seed: 7,
  input_rpm: 1500,
  driver_teeth: 12,
  driven_teeth: 48,
};

const GEARBOX_PARAM_LABELS = {
  seed: '固定种子',
  input_rpm: '输入转速（r/min）',
  driver_teeth: '主动轮齿数',
  driven_teeth: '从动轮齿数',
};

const GEARBOX_PARTS = [
  {
    id: 'base-plate',
    label: '底座',
    kind: 'plate',
    position: [0, -1.5, 0],
    size: [6.8, 0.3, 3.6],
    color: '#6b7a86',
  },
  {
    id: 'housing-lower',
    label: '箱体下盖',
    kind: 'box',
    position: [0, -0.65, 0],
    size: [5.8, 1.3, 2.9],
    color: '#5d7180',
  },
  {
    id: 'housing-upper',
    label: '箱体上盖',
    kind: 'box',
    position: [0, 0.55, 0],
    size: [5.8, 1.0, 2.9],
    color: '#728794',
  },
  {
    id: 'top-cover',
    label: '顶盖',
    kind: 'plate',
    position: [0, 1.12, 0],
    size: [6.0, 0.12, 3.05],
    color: '#93a2ac',
  },
  {
    id: 'oil-window',
    label: '油位观察窗',
    kind: 'plate',
    position: [1.6, 0.2, 1.47],
    size: [1.2, 0.7, 0.05],
    color: '#a9c6cf',
    opacity: 0.55,
  },
  {
    id: 'name-plate',
    label: '铭牌',
    kind: 'plate',
    position: [-1.6, 0.2, 1.47],
    size: [1.2, 0.5, 0.04],
    color: '#c9b45a',
  },
  {
    id: 'input-shaft',
    label: '输入轴',
    kind: 'rod',
    position: [-1.95, 0.2, 0],
    radius: 0.09,
    length: 2.7,
    axis: [1, 0, 0],
    color: '#cfd6db',
    spin: 1,
  },
  {
    id: 'idler-shaft',
    label: '中间轴',
    kind: 'rod',
    position: [0.15, 0.2, 0],
    radius: 0.1,
    length: 2.3,
    axis: [1, 0, 0],
    color: '#cfd6db',
    spin: -0.8,
  },
  {
    id: 'output-shaft',
    label: '输出轴',
    kind: 'rod',
    position: [2.35, 0.2, 0],
    radius: 0.12,
    length: 2.5,
    axis: [1, 0, 0],
    color: '#cfd6db',
    spin: 0.55,
  },
  {
    id: 'input-gear',
    label: '输入小齿轮 z₁=14',
    kind: 'gear',
    parent: 'input-shaft',
    position: [0.4, 0, 0],
    radius: 0.82,
    length: 0.34,
    axis: [1, 0, 0],
    teeth: 14,
    color: '#d9a45c',
    spin: 1,
  },
  {
    id: 'idler-gear',
    label: '惰轮 z₂=18',
    kind: 'gear',
    parent: 'idler-shaft',
    position: [0, 0, 0],
    radius: 1.06,
    length: 0.36,
    axis: [1, 0, 0],
    teeth: 18,
    color: '#c98645',
    spin: -0.77,
  },
  {
    id: 'output-gear',
    label: '输出大齿轮 z₃=22',
    kind: 'gear',
    parent: 'output-shaft',
    position: [0, 0, 0],
    radius: 1.38,
    length: 0.38,
    axis: [1, 0, 0],
    teeth: 22,
    color: '#b8732f',
    spin: 0.5,
  },
  {
    id: 'input-bearing',
    label: '输入轴承座',
    kind: 'cylinder',
    position: [-3.1, 0.2, 0],
    radius: 0.17,
    length: 0.22,
    axis: [1, 0, 0],
    color: '#9aa7b0',
  },
  {
    id: 'output-bearing',
    label: '输出轴承座',
    kind: 'cylinder',
    position: [3.3, 0.2, 0],
    radius: 0.19,
    length: 0.24,
    axis: [1, 0, 0],
    color: '#9aa7b0',
  },
  {
    id: 'coupling',
    label: '联轴器',
    kind: 'cylinder',
    position: [-3.45, 0.2, 0],
    radius: 0.34,
    length: 0.42,
    axis: [1, 0, 0],
    color: '#4e6b7d',
  },
  {
    id: 'breather',
    label: '通气帽',
    kind: 'cylinder',
    position: [-2.2, 1.32, 0],
    radius: 0.1,
    length: 0.3,
    axis: [0, 1, 0],
    color: '#93a2ac',
  },
];

const GEARBOX_STEPS = [
  {
    label: '整机总览',
    detail: '减速箱由输入轴、中间轴与输出轴三级齿轮组成；先看两端转速，再逐步拆解。',
    part_ids: ['housing-lower', 'housing-upper', 'input-gear', 'idler-gear', 'output-gear'],
    explode: 0,
  },
  {
    label: '拆下顶盖与上盖',
    detail: '先拆顶盖、再取下箱体上盖，露出齿轮传动副与润滑腔。',
    part_ids: ['top-cover', 'housing-upper', 'breather'],
    explode: 0.2,
  },
  {
    label: '取出输入轴与小齿轮',
    detail: '输入端联轴器→输入轴→14 齿小齿轮，是减速传动的第一级。',
    part_ids: ['input-shaft', 'input-gear', 'input-bearing', 'coupling'],
    explode: 0.45,
  },
  {
    label: '取出中间轴与惰轮',
    detail: '惰轮改变转动方向，并参与传动比的分配；两轮节圆半径之比决定转速比。',
    part_ids: ['idler-shaft', 'idler-gear'],
    explode: 0.7,
  },
  {
    label: '取出输出轴与大齿轮',
    detail: '22 齿大齿轮转速最低、转矩最大，通过输出轴把动力送出箱体。',
    part_ids: ['output-shaft', 'output-gear', 'output-bearing'],
    explode: 0.88,
  },
  {
    label: '拆到箱体与底座',
    detail: '最后检查箱体、底座、油位观察窗与铭牌，整理装配关系。',
    part_ids: ['housing-lower', 'base-plate', 'oil-window', 'name-plate'],
    explode: 1,
  },
];

const GEARBOX_DATA = {
  version: 1,
  basis: 'local-teaching',
  seed: GEARBOX_PARAMS.seed,
  title: '齿轮减速箱（本地教学示意）',
  parts: GEARBOX_PARTS,
  steps: GEARBOX_STEPS,
  readouts: [
    {
      id: 'input_rpm',
      label: '输入转速 n₁',
      value: GEARBOX_PARAMS.input_rpm,
      unit: 'r/min',
      note: '教学取值，对应常见三相异步电机额定转速。',
    },
    {
      id: 'ratio',
      label: '传动比 i',
      value: 4,
      unit: '',
      note: '按齿数比 i = z₂ / z₁ = 48 / 12 计算，忽略齿面滑动与效率损失。',
    },
    {
      id: 'output_rpm',
      label: '输出转速 n₂',
      value: 375,
      unit: 'r/min',
      note: 'n₂ = n₁ / i = 1500 / 4，为教学近似值。',
    },
    {
      id: 'output_torque',
      label: '输出转矩（相对）',
      value: 4,
      unit: '×T₁',
      note: '理想情况 T₂ = i · T₁；实际需乘以传动效率，本演示不给出工程精度。',
    },
  ],
  camera: { position: [5.2, 4.2, 6.6], target: [0, -0.2, 0] },
  assumptions: [
    '齿轮按直齿圆柱齿轮简化，忽略摩擦、弹性变形、轴承损耗与齿面滑动。',
    '传动比只按齿数比 i = z₂ / z₁ 展示，不对应任何厂商减速箱型号。',
    '几何尺寸、转速与转矩为课堂教学示意，不代表工程精度或真实设备参数。',
    '拆解步骤只表达装配关系，未包含螺纹紧固、密封件与润滑油的更换流程。',
  ],
  source_note: MECH_PRESET_SOURCE,
};

/* ------------------------------------------------------------------------- *
 * 预设二：单级离心泵（mech_3d）
 * 用来验证不同几何（轴向叶轮、竖直出水管）与运转读数；同样为教学示意。
 * ------------------------------------------------------------------------- */

const PUMP_PARAMS = {
  seed: 11,
  input_rpm: 1450,
  impeller_diameter_mm: 180,
  flow_m3_h: 25,
};

const PUMP_PARAM_LABELS = {
  seed: '固定种子',
  input_rpm: '输入转速（r/min）',
  impeller_diameter_mm: '叶轮直径（mm）',
  flow_m3_h: '额定流量（m³/h）',
};

const PUMP_PARTS = [
  {
    id: 'pump-base',
    label: '泵底座',
    kind: 'plate',
    position: [0.35, -0.85, 0],
    size: [3.6, 0.28, 1.8],
    color: '#66757f',
  },
  {
    id: 'volute',
    label: '蜗壳泵体',
    kind: 'cylinder',
    position: [0, 0, 0],
    radius: 1.08,
    length: 0.78,
    axis: [0, 0, 1],
    color: '#5e7b8c',
  },
  {
    id: 'volute-cover',
    label: '泵盖',
    kind: 'cylinder',
    position: [0, 0, 0.44],
    radius: 1.0,
    length: 0.1,
    axis: [0, 0, 1],
    color: '#8199a6',
  },
  {
    id: 'suction-flange',
    label: '吸入口法兰',
    kind: 'cylinder',
    position: [-1.15, 0, 0],
    radius: 0.44,
    length: 0.52,
    axis: [1, 0, 0],
    color: '#7d929e',
  },
  {
    id: 'suction-pipe',
    label: '吸入管',
    kind: 'cylinder',
    position: [-1.66, 0, 0],
    radius: 0.3,
    length: 0.5,
    axis: [1, 0, 0],
    color: '#9db2bd',
  },
  {
    id: 'discharge-flange',
    label: '排出口法兰',
    kind: 'cylinder',
    position: [0.86, 0.98, 0],
    radius: 0.36,
    length: 0.4,
    axis: [0, 1, 0],
    color: '#7d929e',
  },
  {
    id: 'discharge-pipe',
    label: '排出管',
    kind: 'cylinder',
    position: [0.86, 1.46, 0],
    radius: 0.27,
    length: 0.56,
    axis: [0, 1, 0],
    color: '#9db2bd',
  },
  {
    id: 'shaft',
    label: '泵轴',
    kind: 'rod',
    position: [0.9, 0, 0],
    radius: 0.11,
    length: 2.1,
    axis: [1, 0, 0],
    color: '#cfd6db',
    spin: 1,
  },
  {
    id: 'impeller',
    label: '叶轮',
    kind: 'gear',
    parent: 'shaft',
    position: [-0.88, 0, 0],
    radius: 0.78,
    length: 0.28,
    axis: [1, 0, 0],
    teeth: 12,
    color: '#d9a45c',
    spin: 1,
  },
  {
    id: 'front-seal',
    label: '前密封环',
    kind: 'cylinder',
    position: [-0.55, 0, 0],
    radius: 0.24,
    length: 0.1,
    axis: [1, 0, 0],
    color: '#b87333',
  },
  {
    id: 'bearing-housing',
    label: '轴承体',
    kind: 'cylinder',
    position: [1.28, 0, 0],
    radius: 0.34,
    length: 0.42,
    axis: [1, 0, 0],
    color: '#8799a3',
  },
  {
    id: 'coupling',
    label: '联轴器',
    kind: 'cylinder',
    position: [1.82, 0, 0],
    radius: 0.26,
    length: 0.36,
    axis: [1, 0, 0],
    color: '#4e6b7d',
  },
  {
    id: 'motor',
    label: '驱动电机',
    kind: 'cylinder',
    position: [2.55, 0, 0],
    radius: 0.56,
    length: 1.16,
    axis: [1, 0, 0],
    color: '#4f6f82',
  },
  {
    id: 'motor-fan-cover',
    label: '电机风罩',
    kind: 'cylinder',
    position: [3.28, 0, 0],
    radius: 0.42,
    length: 0.34,
    axis: [1, 0, 0],
    color: '#3f5868',
  },
  {
    id: 'pump-nameplate',
    label: '泵铭牌',
    kind: 'plate',
    position: [-0.55, 0.42, 0.45],
    size: [0.72, 0.36, 0.04],
    color: '#c9b45a',
  },
];

const PUMP_STEPS = [
  {
    label: '整机总览',
    detail: '单级离心泵由泵体、叶轮、泵轴、密封与电机组成；先观察进出口方向。',
    part_ids: ['volute', 'impeller', 'motor', 'shaft'],
    explode: 0,
  },
  {
    label: '拆下进出口管路',
    detail: '吸入口沿轴向、排出口竖直向上；管路法兰与泵体通过螺栓连接。',
    part_ids: ['suction-pipe', 'suction-flange', 'discharge-pipe', 'discharge-flange'],
    explode: 0.22,
  },
  {
    label: '打开泵盖取出叶轮',
    detail: '叶轮是能量转换的核心：电机带动叶轮旋转，把机械能传给液体。',
    part_ids: ['volute-cover', 'impeller', 'front-seal', 'pump-nameplate'],
    explode: 0.5,
  },
  {
    label: '抽出泵轴与轴承',
    detail: '泵轴穿过轴承体与密封环，把电机转矩传到叶轮。',
    part_ids: ['shaft', 'bearing-housing', 'coupling'],
    explode: 0.76,
  },
  {
    label: '拆到电机与泵体',
    detail: '最后分离电机与泵体，检查底座、风罩与联轴器的配合关系。',
    part_ids: ['motor', 'motor-fan-cover', 'volute', 'pump-base'],
    explode: 1,
  },
];

const PUMP_DATA = {
  version: 1,
  basis: 'local-teaching',
  seed: PUMP_PARAMS.seed,
  title: '单级离心泵（本地教学示意）',
  parts: PUMP_PARTS,
  steps: PUMP_STEPS,
  readouts: [
    {
      id: 'input_rpm',
      label: '泵轴转速 n',
      value: PUMP_PARAMS.input_rpm,
      unit: 'r/min',
      note: '电机经联轴器直连，教学取值。',
    },
    {
      id: 'flow',
      label: '额定流量 Q',
      value: PUMP_PARAMS.flow_m3_h,
      unit: 'm³/h',
      note: '教学示例值，不代表具体型号的性能曲线。',
    },
    {
      id: 'head',
      label: '额定扬程 H',
      value: 20,
      unit: 'm',
      note: '教学示例值；实际扬程随流量变化，需查泵的性能曲线。',
    },
    {
      id: 'efficiency',
      label: '效率 η',
      value: 68,
      unit: '%',
      note: '教学近似值，用于说明能量损失存在，不用于选型。',
    },
  ],
  camera: { position: [4.6, 3.6, 5.4], target: [0.2, 0.4, 0] },
  assumptions: [
    '泵体、叶轮与管路为教学简化几何，不含真实水力设计细节。',
    '流量、扬程、效率为教学示例值，不对应任何厂商型号或性能曲线。',
    '演示不计算水力损失、汽蚀余量与电机效率，读数不可用于工程选型。',
  ],
  source_note: MECH_PRESET_SOURCE,
};

/* ------------------------------------------------------------------------- *
 * 预设三：三工位装配线（factory_sim）
 * 固定事件表：7 件完成、8 件到达、加工工位为瓶颈；前端只回放事件，
 * 队列长度与工位状态由事件推导，不重新做随机模拟。
 * ------------------------------------------------------------------------- */

const FACTORY_PARAMS = {
  seed: 42,
  sim_seconds: 70,
  arrival_interval_s: 5,
  service_s: [5, 8, 6],
  buffer_capacity: 3,
};

const FACTORY_PARAM_LABELS = {
  seed: '固定种子',
  sim_seconds: '仿真时长（s）',
  arrival_interval_s: '到达间隔（s）',
  service_s: '各工位工时（s）',
  buffer_capacity: '缓冲容量（件）',
};

const FACTORY_STATIONS = ['上料工位', '加工工位', '装配工位'];

const FACTORY_NODES = [
  { id: 'station-1', label: FACTORY_STATIONS[0], kind: 'station' },
  { id: 'station-2', label: FACTORY_STATIONS[1], kind: 'station' },
  { id: 'station-3', label: FACTORY_STATIONS[2], kind: 'station' },
];

const FACTORY_EDGES = [
  { from: 'station-1', to: 'station-2' },
  { from: 'station-2', to: 'station-3' },
];

/**
 * 固定事件表（非随机）：[t, type, station, item_id]。
 * - t：仿真秒；
 * - type：arrival（到达第一站）/ service_start（开始加工）/ service_end（完成，流入下一站），
 *   与后端 DES 事件枚举一致；
 * - station：工位序号 0..2；
 * - item_id：工件编号 1..8。
 * 共 54 条事件，均由固定节拍手工排出，与 metrics 的到达/完成/瓶颈一致。
 * 前端按契约也接受 {t,type,station,item_id,queue} 形态：queue 存在时以事件为准。
 */
const FACTORY_EVENT_TABLE = [
  [1, 'arrival', 0, 1],
  [1, 'service_start', 0, 1],
  [6, 'service_end', 0, 1],
  [6, 'arrival', 0, 2],
  [6, 'service_start', 0, 2],
  [6, 'service_start', 1, 1],
  [11, 'service_end', 0, 2],
  [11, 'arrival', 0, 3],
  [11, 'service_start', 0, 3],
  [14, 'service_end', 1, 1],
  [14, 'service_start', 1, 2],
  [14, 'service_start', 2, 1],
  [16, 'service_end', 0, 3],
  [16, 'arrival', 0, 4],
  [16, 'service_start', 0, 4],
  [20, 'service_end', 2, 1],
  [21, 'service_end', 0, 4],
  [21, 'arrival', 0, 5],
  [21, 'service_start', 0, 5],
  [22, 'service_end', 1, 2],
  [22, 'service_start', 1, 3],
  [22, 'service_start', 2, 2],
  [26, 'service_end', 0, 5],
  [26, 'arrival', 0, 6],
  [26, 'service_start', 0, 6],
  [28, 'service_end', 2, 2],
  [30, 'service_end', 1, 3],
  [30, 'service_start', 1, 4],
  [30, 'service_start', 2, 3],
  [31, 'service_end', 0, 6],
  [31, 'arrival', 0, 7],
  [31, 'service_start', 0, 7],
  [36, 'service_end', 0, 7],
  [36, 'arrival', 0, 8],
  [36, 'service_start', 0, 8],
  [36, 'service_end', 2, 3],
  [38, 'service_end', 1, 4],
  [38, 'service_start', 1, 5],
  [38, 'service_start', 2, 4],
  [41, 'service_end', 0, 8],
  [44, 'service_end', 2, 4],
  [46, 'service_end', 1, 5],
  [46, 'service_start', 1, 6],
  [46, 'service_start', 2, 5],
  [52, 'service_end', 2, 5],
  [54, 'service_end', 1, 6],
  [54, 'service_start', 1, 7],
  [54, 'service_start', 2, 6],
  [60, 'service_end', 2, 6],
  [62, 'service_end', 1, 7],
  [62, 'service_start', 1, 8],
  [62, 'service_start', 2, 7],
  [68, 'service_end', 2, 7],
  [70, 'service_end', 1, 8],
];

/** 事件表 → 契约事件对象；queue 字段按同一套确定性回放规则补全（不是随机模拟）。 */
function buildFactoryEvents() {
  const nextStation = [1, 2, -1];
  const queues = [0, 0, 0];
  return FACTORY_EVENT_TABLE.map(([t, type, station, itemId]) => {
    if (type === 'arrival') queues[station] += 1;
    else if (type === 'service_start') queues[station] = Math.max(0, queues[station] - 1);
    else if (type === 'service_end' && nextStation[station] >= 0) queues[nextStation[station]] += 1;
    return { t, type, station, item_id: itemId, queue: queues[station] };
  });
}

const FACTORY_DATA = {
  version: 1,
  basis: 'deterministic-des',
  seed: FACTORY_PARAMS.seed,
  flow: { nodes: FACTORY_NODES, edges: FACTORY_EDGES },
  station_labels: FACTORY_STATIONS,
  metrics: {
    sim_seconds: FACTORY_PARAMS.sim_seconds,
    arrivals: 8,
    completed: 7,
    throughput_per_min: 6,
    avg_wait_s: [0, 10.5, 0],
    max_queue: [0, 3, 0],
    utilization: [0.57, 0.91, 0.6],
    bottleneck_station: 1,
    bottleneck_reason:
      '加工工位（第 2 站）单件工时 8s，慢于上料 5s；平均等待 10.5s、最大队列 3 件、利用率约 91%，是本线瓶颈。',
  },
  events: buildFactoryEvents(),
  assumptions: [
    '离散事件教学模型：到达间隔与各工位工时均为固定值，无随机扰动、无故障与换型。',
    '每站缓冲容量按 3 件、单件依次加工简化，不计搬运时间与操作人员差异。',
    '事件与指标来自固定种子的事件表；前端只按时间回放，不重新计算或伪造结果。',
  ],
  source_note: MECH_PRESET_SOURCE,
};

/* ------------------------------------------------------------------------- *
 * 对外导出
 * ------------------------------------------------------------------------- */

/**
 * 本地兜底预设（固定顺序：减速箱 → 离心泵 → 三工位装配线）。
 * 每项：{template_id, kind, title, description, params, param_labels, data}。
 */
export const MECH_PRESETS = [
  {
    template_id: 'gearbox-3d',
    kind: 'mech_3d',
    title: '齿轮减速箱（本地教学示意）',
    description: '拆解/剖切演示：输入轴、中间轴与输出轴三级直齿轮减速，读数含传动比与输出转速。',
    params: { ...GEARBOX_PARAMS },
    param_labels: { ...GEARBOX_PARAM_LABELS },
    data: GEARBOX_DATA,
  },
  {
    template_id: 'pump-3d',
    kind: 'mech_3d',
    title: '单级离心泵（本地教学示意）',
    description: '拆解演示：叶轮、泵轴、密封与电机，读数含转速、流量、扬程与效率。',
    params: { ...PUMP_PARAMS },
    param_labels: { ...PUMP_PARAM_LABELS },
    data: PUMP_DATA,
  },
  {
    template_id: 'factory-line',
    kind: 'factory_sim',
    title: '三工位装配线（固定种子示例）',
    description: '固定种子离散事件回放：三工位流程、队列长度时间序列、吞吐/等待/利用率与瓶颈工位。',
    params: { ...FACTORY_PARAMS, service_s: [...FACTORY_PARAMS.service_s] },
    param_labels: { ...FACTORY_PARAM_LABELS },
    data: FACTORY_DATA,
  },
];

/** 按 template_id 取本地预设；未知模板返回 null（调用方中文降级，不伪造预设）。 */
export function getMechPreset(template_id) {
  const key = String(template_id || '').trim();
  if (!key) return null;
  return MECH_PRESETS.find((preset) => preset.template_id === key) || null;
}

/** 本地预设列表 → 与 `GET /api/demos/templates` 同形的模板元数据（不含 data）。 */
export function mechTemplateItems() {
  return MECH_PRESETS.map((preset) => ({
    template_id: preset.template_id,
    kind: preset.kind,
    title: preset.title,
    description: preset.description,
    params: Object.entries(preset.params).map(([name, defaultValue]) => ({
      name,
      label: preset.param_labels?.[name] || name,
      default: Array.isArray(defaultValue) ? [...defaultValue] : defaultValue,
    })),
    assumptions: [...(preset.data.assumptions || [])],
    source_note: preset.data.source_note || MECH_PRESET_SOURCE,
  }));
}

/** 本地固定种子（页面说明栏展示用；与各预设 params.seed 一起出现）。 */
export const MECH_LOCAL_SEED = LOCAL_SEED;
