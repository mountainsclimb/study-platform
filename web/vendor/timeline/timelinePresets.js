/* 高中历史时间轴预设与数据规范化（vis-timeline 8.5.6；方案 §6.5）。
 *
 * 纪律（静态检查覆盖，见 tests/test_oss_vendor_assets.py 的 vis-timeline 组）：
 * - 全部事件为人工校对的史实条目，日期一律用 `YYYY` / `YYYY-MM` / `YYYY-MM-DD`
 *   （公元 1–9999 年，四位年份；不收录公元前日期，避免各引擎的纪年显示差异）；
 * - 不使用 Math.random，条目按开始日期稳定排序，保存的参数可完全重建画面；
 * - 事件标题是纯文本，组件进入 vis-timeline 前统一 HTML 转义。
 *
 * 预设结构：
 *   { id, zh, en, description, source,
 *     groups: [{ id, zh, en }],
 *     events: [{ start, end?, title, group? }] }
 *
 * 附件数据（`timeline`）：
 *   { preset_id, events: [...], options: { height?, start?, end?, zoomMin?, zoomMax? } }
 */

const DATE_RE = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/;
const MAX_EVENTS = 200;
const MAX_TITLE_CHARS = 120;
const MAX_GROUP_CHARS = 40;

function daysInMonth(year, month) {
  // setFullYear 避免 Date 构造器把 0–99 年映射到 1900+。
  const date = new Date(0);
  date.setFullYear(year, month, 0);
  return date.getDate();
}

/** 严格解析 `YYYY[-MM[-DD]]`；非法返回 null（不做 Date 的隐式宽松解析）。 */
export function parseTimelineDate(value) {
  if (typeof value !== 'string') return null;
  const match = DATE_RE.exec(value.trim());
  if (!match) return null;
  const year = Number(match[1]);
  const month = match[2] ? Number(match[2]) : 1;
  const day = match[3] ? Number(match[3]) : 1;
  if (year < 1 || year > 9999) return null;
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > daysInMonth(year, month)) return null;
  const date = new Date(0);
  date.setFullYear(year, month - 1, day);
  date.setHours(0, 0, 0, 0);
  return date;
}

function charLength(value) {
  return Array.from(value).length;
}

const HTML_ESCAPES = Object.freeze({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
});

/**
 * vis-timeline 把事件 content 当 HTML 写入 DOM；进入引擎前统一转义，
 * 让笔记/题目来源的标题按纯文本渲染（§6.5 先做纯文本事件）。
 */
export function escapeTimelineHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
}

/** 规范化单条事件；非法返回 null。 */
export function normalizeTimelineEvent(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const title = typeof raw.title === 'string' ? raw.title.trim() : '';
  if (!title || charLength(title) > MAX_TITLE_CHARS) return null;
  const start = parseTimelineDate(raw.start);
  if (!start) return null;
  const hasEnd = raw.end !== undefined && raw.end !== null && raw.end !== '';
  const end = hasEnd ? parseTimelineDate(raw.end) : null;
  if (hasEnd && (!end || end.getTime() < start.getTime())) return null;
  let group = '';
  if (raw.group !== undefined && raw.group !== null && raw.group !== '') {
    if (typeof raw.group !== 'string') return null;
    group = raw.group.trim();
    if (!group || charLength(group) > MAX_GROUP_CHARS) return null;
  }
  return {
    start: String(raw.start).trim(),
    ...(hasEnd ? { end: String(raw.end).trim() } : {}),
    title,
    ...(group ? { group } : {}),
  };
}

/** 规范化时间轴选项；非法返回 null，缺省返回空对象。 */
export function normalizeTimelineOptions(raw) {
  if (raw === undefined || raw === null) return {};
  if (typeof raw !== 'object' || Array.isArray(raw)) return null;
  const options = {};
  if (raw.height !== undefined) {
    const height = Number(raw.height);
    if (!Number.isFinite(height) || height < 280 || height > 720) return null;
    options.height = Math.round(height);
  }
  for (const key of ['start', 'end']) {
    if (raw[key] === undefined || raw[key] === null || raw[key] === '') continue;
    if (!parseTimelineDate(raw[key])) return null;
    options[key] = String(raw[key]).trim();
  }
  for (const key of ['zoomMin', 'zoomMax']) {
    if (raw[key] === undefined || raw[key] === null) continue;
    const value = Number(raw[key]);
    if (!Number.isInteger(value) || value <= 0) return null;
    options[key] = value;
  }
  if (options.start && options.end) {
    const start = parseTimelineDate(options.start);
    const end = parseTimelineDate(options.end);
    if (!start || !end || end.getTime() <= start.getTime()) return null;
  }
  return options;
}

function sortedEvents(events) {
  return [...events].sort((left, right) => {
    if (left.start !== right.start) return left.start < right.start ? -1 : 1;
    return left.title < right.title ? -1 : left.title > right.title ? 1 : 0;
  });
}

export const TIMELINE_PRESETS = Object.freeze([
  {
    id: 'modern-china-1840-1949',
    zh: '中国近代史（1840–1949）',
    en: 'Modern China, 1840–1949',
    description: '从鸦片战争到新中国成立：侵略与条约、抗争与探索、革命与建国。',
    source: '统编版高中历史教材《中外历史纲要（上）》；事件日期以教材与权威年表校对。',
    groups: [
      { id: 'aggression', zh: '侵略与条约', en: 'Invasion & treaties' },
      { id: 'struggle', zh: '抗争与探索', en: 'Resistance & exploration' },
      { id: 'revolution', zh: '革命与建国', en: 'Revolution & founding' },
    ],
    events: [
      { start: '1840-06-28', end: '1842-08-29', title: '第一次鸦片战争', group: 'aggression' },
      { start: '1851-01-11', end: '1864-07-19', title: '太平天国运动', group: 'struggle' },
      { start: '1856-10-23', end: '1860-10-24', title: '第二次鸦片战争', group: 'aggression' },
      { start: '1861-01-01', end: '1895-04-17', title: '洋务运动', group: 'struggle' },
      { start: '1894-07-25', end: '1895-04-17', title: '甲午中日战争', group: 'aggression' },
      { start: '1898-06-11', end: '1898-09-21', title: '戊戌变法', group: 'struggle' },
      { start: '1899-01-01', end: '1901-09-07', title: '义和团运动', group: 'struggle' },
      { start: '1900-06-01', end: '1901-09-07', title: '八国联军侵华', group: 'aggression' },
      { start: '1901-09-07', title: '《辛丑条约》签订', group: 'aggression' },
      { start: '1911-10-10', end: '1912-02-12', title: '辛亥革命', group: 'revolution' },
      { start: '1912-01-01', title: '中华民国成立', group: 'revolution' },
      { start: '1915-09-15', end: '1923-12-31', title: '新文化运动', group: 'struggle' },
      { start: '1919-05-04', title: '五四运动', group: 'revolution' },
      { start: '1921-07-23', title: '中国共产党成立', group: 'revolution' },
      { start: '1924-01-01', end: '1927-07-15', title: '第一次国共合作', group: 'revolution' },
      { start: '1927-08-01', title: '南昌起义', group: 'revolution' },
      { start: '1931-09-18', title: '九一八事变', group: 'aggression' },
      { start: '1934-10-01', end: '1936-10-22', title: '红军长征', group: 'revolution' },
      { start: '1937-07-07', end: '1945-09-02', title: '抗日战争', group: 'aggression' },
      { start: '1946-06-26', end: '1949-10-01', title: '解放战争', group: 'revolution' },
      { start: '1949-10-01', title: '中华人民共和国成立', group: 'revolution' },
    ],
  },
  {
    id: 'world-modern-1492-1945',
    zh: '世界近现代史（1492–1945）',
    en: 'The modern world, 1492–1945',
    description: '大航海、思想与科学、政治革命、工业与战争：世界联为一体的四百年。',
    source: '统编版高中历史教材《中外历史纲要（下）》；事件日期以教材与权威年表校对。',
    groups: [
      { id: 'voyage', zh: '大航海与全球联系', en: 'Voyages & global connections' },
      { id: 'thought', zh: '思想与科学', en: 'Thought & science' },
      { id: 'revolution', zh: '政治革命', en: 'Political revolutions' },
      { id: 'industry-war', zh: '工业与战争', en: 'Industry & war' },
    ],
    events: [
      { start: '1492-10-12', title: '哥伦布到达美洲', group: 'voyage' },
      { start: '1498-05-20', title: '达·伽马到达印度', group: 'voyage' },
      { start: '1517-10-31', title: '马丁·路德发起宗教改革', group: 'thought' },
      { start: '1519-09-20', end: '1522-09-06', title: '麦哲伦船队环球航行', group: 'voyage' },
      { start: '1640-11-03', end: '1688-12-16', title: '英国资产阶级革命', group: 'revolution' },
      { start: '1687-07-05', title: '牛顿发表《自然哲学的数学原理》', group: 'thought' },
      { start: '1765-01-01', end: '1840-01-01', title: '第一次工业革命', group: 'industry-war' },
      { start: '1775-04-19', end: '1783-09-03', title: '美国独立战争', group: 'revolution' },
      { start: '1789-07-14', end: '1799-11-09', title: '法国大革命', group: 'revolution' },
      { start: '1848-02-21', title: '《共产党宣言》发表', group: 'thought' },
      { start: '1861-03-03', title: '俄国农奴制改革', group: 'revolution' },
      { start: '1861-04-12', end: '1865-04-09', title: '美国内战', group: 'industry-war' },
      { start: '1868-01-03', end: '1889-02-11', title: '日本明治维新', group: 'revolution' },
      { start: '1914-07-28', end: '1918-11-11', title: '第一次世界大战', group: 'industry-war' },
      { start: '1917-11-07', title: '俄国十月革命', group: 'revolution' },
      { start: '1929-10-24', end: '1933-03-04', title: '经济大危机', group: 'industry-war' },
      { start: '1939-09-01', end: '1945-09-02', title: '第二次世界大战', group: 'industry-war' },
    ],
  },
  {
    id: 'prc-1949-2021',
    zh: '新中国史（1949–2021）',
    en: 'History of New China, 1949–2021',
    description: '从开国奠基到改革开放，再到新时代：制度、国防、外交与民生的重要节点。',
    source: '统编版高中历史教材《中外历史纲要（上）》与《中华人民共和国简史》；日期以权威年表校对。',
    groups: [
      { id: 'founding', zh: '奠基与国防', en: 'Foundations & defense' },
      { id: 'reform', zh: '改革开放', en: 'Reform & opening' },
      { id: 'new-era', zh: '新时代', en: 'New era' },
    ],
    events: [
      { start: '1949-10-01', title: '中华人民共和国成立', group: 'founding' },
      { start: '1950-10-19', end: '1953-07-27', title: '抗美援朝', group: 'founding' },
      { start: '1953-01-01', end: '1957-12-31', title: '第一个五年计划', group: 'founding' },
      { start: '1956-12-31', title: '三大改造基本完成', group: 'founding' },
      { start: '1964-10-16', title: '第一颗原子弹爆炸成功', group: 'founding' },
      { start: '1971-10-25', title: '恢复联合国合法席位', group: 'founding' },
      { start: '1978-12-18', end: '1978-12-22', title: '十一届三中全会', group: 'reform' },
      { start: '1980-08-26', title: '设立深圳等经济特区', group: 'reform' },
      { start: '1997-07-01', title: '香港回归', group: 'reform' },
      { start: '1999-12-20', title: '澳门回归', group: 'reform' },
      { start: '2001-12-11', title: '加入世界贸易组织', group: 'reform' },
      { start: '2003-10-15', title: '神舟五号载人飞行', group: 'reform' },
      { start: '2008-08-08', end: '2008-08-24', title: '北京奥运会', group: 'reform' },
      { start: '2021-02-25', title: '脱贫攻坚战全面胜利', group: 'new-era' },
      { start: '2021-07-01', title: '全面建成小康社会', group: 'new-era' },
    ],
  },
  {
    id: 'science-milestones-1543-2003',
    zh: '科技史里程碑（1543–2003）',
    en: 'Milestones of science, 1543–2003',
    description: '从哥白尼到人类基因组计划：物理与宇宙、生命科学、技术与工程的里程碑。',
    source: '统编版高中历史教材与《科学技术史》通行年表；事件日期以权威资料校对。',
    groups: [
      { id: 'physics', zh: '物理与宇宙', en: 'Physics & the cosmos' },
      { id: 'life', zh: '生命科学', en: 'Life sciences' },
      { id: 'tech', zh: '技术与工程', en: 'Technology & engineering' },
    ],
    events: [
      { start: '1543-05-24', title: '哥白尼《天球运行论》出版', group: 'physics' },
      { start: '1609-08-25', title: '伽利略展示天文望远镜', group: 'physics' },
      { start: '1687-07-05', title: '牛顿《自然哲学的数学原理》出版', group: 'physics' },
      { start: '1769-01-05', title: '瓦特蒸汽机专利', group: 'tech' },
      { start: '1831-08-29', title: '法拉第发现电磁感应', group: 'physics' },
      { start: '1859-11-24', title: '达尔文《物种起源》出版', group: 'life' },
      { start: '1866', title: '孟德尔遗传定律发表', group: 'life' },
      { start: '1869-03-06', title: '门捷列夫发表元素周期表', group: 'physics' },
      { start: '1900-12-14', title: '普朗克提出量子假说', group: 'physics' },
      { start: '1905-06-30', title: '爱因斯坦发表狭义相对论', group: 'physics' },
      { start: '1915-11-25', title: '爱因斯坦发表广义相对论', group: 'physics' },
      { start: '1928-09-28', title: '弗莱明发现青霉素', group: 'life' },
      { start: '1946-02-14', title: 'ENIAC 问世', group: 'tech' },
      { start: '1953-04-25', title: 'DNA 双螺旋结构发表', group: 'life' },
      { start: '1957-10-04', title: '第一颗人造地球卫星上天', group: 'tech' },
      { start: '1969-07-20', title: '阿波罗 11 号登月', group: 'tech' },
      { start: '1991-08-06', title: '万维网向公众开放', group: 'tech' },
      { start: '2003-04-14', title: '人类基因组计划完成', group: 'life' },
    ],
  },
]);

export function findTimelinePreset(id) {
  const key = typeof id === 'string' ? id.trim() : '';
  if (!key) return null;
  return TIMELINE_PRESETS.find(preset => preset.id === key) || null;
}

/** 预设事件的深拷贝（调用方可在弹层里增删，不污染冻结的预设）。 */
export function timelinePresetEvents(presetId) {
  const preset = findTimelinePreset(presetId);
  if (!preset) return [];
  return preset.events.map(event => ({ ...event }));
}

/**
 * 规范化任意来源的 timeline 定义（附件 data / 弹层入参）。
 * 预设不存在、事件非法或选项非法返回 null；事件按开始日期稳定排序。
 */
export function normalizeTimelineDefinition(raw) {
  let value = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const preset = findTimelinePreset(value.preset_id || value.presetId || value.id);
  if (!preset) return null;
  const rawEvents = value.events === undefined || value.events === null
    ? preset.events
    : value.events;
  if (!Array.isArray(rawEvents) || rawEvents.length < 1 || rawEvents.length > MAX_EVENTS) {
    return null;
  }
  const events = [];
  for (const item of rawEvents) {
    const event = normalizeTimelineEvent(item);
    if (!event) return null;
    events.push(event);
  }
  const options = normalizeTimelineOptions(value.options);
  if (!options) return null;
  return { preset_id: preset.id, events: sortedEvents(events), options };
}

export { MAX_EVENTS as TIMELINE_MAX_EVENTS };
