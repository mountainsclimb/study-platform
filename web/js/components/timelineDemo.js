/* vis-timeline 历史时间轴（方案 §6.5）。
 *
 * 用法：
 *   const controller = createTimelineView(container, definition, {readOnly:true});
 *   await controller.ready;
 *   controller.getDefinition(); controller.pngURI(); controller.destroy();
 *
 * 或 openTimelineDemo({links, ref, onSaved}) 打开可交互弹层并保存为 `timeline`
 * 学习附件。
 *
 * 纪律：事件标题一律按纯文本处理（进入 vis-timeline 前 HTML 转义），
 * 主站 CSP 无 `unsafe-eval`；日期格式在 vendor/timeline/timelinePresets.js
 * 中严格校验，保存后可按数据完全重建。
 */
import { api } from '../core/api.js';
import { busy, button, el, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import {
  TIMELINE_PRESETS,
  escapeTimelineHtml,
  findTimelinePreset,
  normalizeTimelineDefinition,
  normalizeTimelineEvent,
  parseTimelineDate,
  timelinePresetEvents,
} from '../../vendor/timeline/timelinePresets.js';

export { normalizeTimelineDefinition };

const SCRIPT_URL = '/static/vendor/vis-timeline/vis-timeline-graph2d.min.js';
const CSS_URL = '/static/vendor/vis-timeline/vis-timeline-graph2d.min.css';
const SCRIPT_MARK = 'ossVisTimelineScript';
const CSS_MARK = 'ossVisTimelineCss';
const LOAD_TIMEOUT_MS = 15000;
export const TIMELINE_KIND = 'timeline';
const HEIGHT_DEFAULT = 420;
const POSTER_WIDTH = 640;
const POSTER_HEIGHT = 360;
const GROUP_COLORS = ['#2f6fed', '#c0392b', '#1f9d55', '#8e44ad', '#d68910', '#0e7490'];
const MAX_ADDED_EVENTS = 200;

let loaderPromise = null;

function ossText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function isEnglishLocale() {
  return /^en/i.test(document.documentElement.lang || '') || /^en/i.test(String(navigator.language || ''));
}

function presetLabel(preset) {
  return isEnglishLocale() ? preset.en : preset.zh;
}

function groupLabel(preset, groupId) {
  const group = (preset.groups || []).find(item => item.id === groupId);
  if (!group) return groupId || ossText('study.oss.timeline.otherGroup', '其他');
  return isEnglishLocale() ? group.en : group.zh;
}

/** 首次调用时注入 vis-timeline（UMD 全局 vis）与样式；失败可重试。 */
export function loadVisTimeline() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('study.oss.timeline.unavailable', '当前环境不支持时间轴')));
  }
  const existing = window.vis;
  if (existing && typeof existing.Timeline === 'function' && existing.DataSet) {
    return Promise.resolve(existing);
  }
  if (loaderPromise) return loaderPromise;
  loaderPromise = new Promise((resolve, reject) => {
    if (!document.querySelector(`link[data-oss="${CSS_MARK}"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = CSS_URL;
      link.dataset.oss = CSS_MARK;
      document.head.append(link);
    }
    let script = document.querySelector(`script[data-oss="${SCRIPT_MARK}"]`);
    const finish = () => {
      const library = window.vis;
      if (library && typeof library.Timeline === 'function' && library.DataSet) {
        resolve(library);
        return;
      }
      if (script) script.remove();
      reject(new Error(ossText('study.oss.timeline.unavailable', '时间轴加载失败')));
    };
    if (script && script.dataset.loaded === '1') {
      finish();
      return;
    }
    if (!script) {
      script = document.createElement('script');
      script.src = SCRIPT_URL;
      script.async = true;
      script.dataset.oss = SCRIPT_MARK;
      document.head.append(script);
    }
    const timer = setTimeout(() => {
      if (script) script.remove();
      reject(new Error(ossText('study.oss.timeline.unavailable', '时间轴加载超时')));
    }, LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      finish();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('study.oss.timeline.unavailable', '时间轴加载失败')));
    }, { once: true });
  }).catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

const MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function asDate(value) {
  return value instanceof Date ? value : new Date(value);
}

/** vis-timeline 的刻度文案；中文站默认年月日，英文站用短月名。 */
function minorLabel(value, scale) {
  const date = asDate(value);
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  const day = date.getDate();
  if (isEnglishLocale()) {
    if (scale === 'year') return String(year);
    if (scale === 'month') return `${MONTHS_EN[month - 1]} ${year}`;
    if (scale === 'hour' || scale === 'minute' || scale === 'second') {
      return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
    }
    return `${day} ${MONTHS_EN[month - 1]}`;
  }
  if (scale === 'year') return `${year}年`;
  if (scale === 'month') return `${year}年${month}月`;
  if (scale === 'hour' || scale === 'minute' || scale === 'second') {
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }
  return `${month}月${day}日`;
}

function majorLabel(value, scale) {
  const date = asDate(value);
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  const day = date.getDate();
  if (isEnglishLocale()) {
    if (scale === 'year') return `${Math.floor(year / 10) * 10}s`;
    if (scale === 'month') return String(year);
    if (scale === 'day' || scale === 'week') return `${MONTHS_EN[month - 1]} ${year}`;
    return `${day} ${MONTHS_EN[month - 1]}`;
  }
  if (scale === 'year') return `${Math.floor(year / 10) * 10}年代`;
  if (scale === 'month') return `${year}年`;
  if (scale === 'day' || scale === 'week') return `${year}年${month}月`;
  return `${month}月${day}日`;
}

/** 事件分组：预设分组在前，自建分组按出现顺序追加。content 供引擎，label 供海报。 */
function buildGroups(preset, events) {
  const labels = new Map();
  for (const group of preset.groups || []) labels.set(group.id, groupLabel(preset, group.id));
  let hasUngrouped = false;
  for (const event of events) {
    if (!event.group) { hasUngrouped = true; continue; }
    if (!labels.has(event.group)) labels.set(event.group, event.group);
  }
  if (hasUngrouped) labels.set('_default', ossText('study.oss.timeline.otherGroup', '其他'));
  return [...labels.entries()].map(([id, label]) => ({
    id,
    label,
    content: escapeTimelineHtml(label),
  }));
}

function buildItems(library, events) {
  return new library.DataSet(events.map((event, index) => {
    const start = parseTimelineDate(event.start);
    const end = event.end ? parseTimelineDate(event.end) : null;
    const isRange = Boolean(end && end.getTime() > start.getTime());
    return {
      id: index + 1,
      content: escapeTimelineHtml(event.title),
      title: escapeTimelineHtml(event.title),
      start,
      ...(isRange ? { end } : {}),
      group: event.group || '_default',
      className: 'oss-timeline-item',
    };
  }));
}

function timelineOptions(definition) {
  const options = definition.options || {};
  const resolved = {
    height: options.height || HEIGHT_DEFAULT,
    stack: true,
    showCurrentTime: false,
    selectable: false,
    editable: false,
    multiselect: false,
    zoomMin: 1000 * 60 * 60 * 24 * 30,
    margin: { item: { horizontal: 8, vertical: 6 } },
    format: { minorLabels: minorLabel, majorLabels: majorLabel },
  };
  const start = options.start ? parseTimelineDate(options.start) : null;
  const end = options.end ? parseTimelineDate(options.end) : null;
  if (start && end) {
    resolved.start = start;
    resolved.end = end;
  }
  if (options.zoomMin) resolved.zoomMin = options.zoomMin;
  if (options.zoomMax) resolved.zoomMax = options.zoomMax;
  return resolved;
}

/** 海报：按事件的时间范围与分组画一张确定性的横轴图（不依赖页面 DOM 截图）。 */
function drawPoster(definition, preset) {
  const canvas = document.createElement('canvas');
  canvas.width = POSTER_WIDTH;
  canvas.height = POSTER_HEIGHT;
  const context = canvas.getContext('2d');
  if (!context) return '';
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, POSTER_WIDTH, POSTER_HEIGHT);
  context.fillStyle = '#12263a';
  context.font = 'bold 20px "Microsoft YaHei", "PingFang SC", sans-serif';
  context.fillText(presetLabel(preset).slice(0, 28), 24, 38);
  context.fillStyle = '#5b6b7a';
  context.font = '12px "Microsoft YaHei", "PingFang SC", sans-serif';
  context.fillText(String(preset.description || '').slice(0, 52), 24, 60);

  const events = definition.events;
  const spans = events.map(event => {
    const start = parseTimelineDate(event.start).getTime();
    const end = event.end ? parseTimelineDate(event.end).getTime() : start;
    return { start, end: Math.max(start, end) };
  });
  let min = Math.min(...spans.map(span => span.start));
  let max = Math.max(...spans.map(span => span.end));
  if (max <= min) { max = min + 1000 * 60 * 60 * 24 * 365; }
  const years = (max - min) / (1000 * 60 * 60 * 24 * 365.25);
  if (years >= 2) { min -= (max - min) * 0.03; max += (max - min) * 0.03; }
  const left = 24;
  const right = POSTER_WIDTH - 24;
  const xOf = value => left + ((value - min) / (max - min)) * (right - left);

  const groups = buildGroups(preset, events);
  const laneTop = 88;
  const laneHeight = groups.length ? Math.min(46, (POSTER_HEIGHT - laneTop - 56) / groups.length) : 40;
  groups.forEach((group, index) => {
    const y = laneTop + index * laneHeight;
    context.fillStyle = '#eef2f6';
    context.fillRect(left, y + laneHeight - 14, right - left, 1);
    context.fillStyle = '#41505f';
    context.font = '11px "Microsoft YaHei", "PingFang SC", sans-serif';
    context.fillText(String(group.label).slice(0, 10), left, y + laneHeight - 20);
  });

  const groupIndex = new Map(groups.map((group, index) => [group.id, index]));
  events.forEach((event, index) => {
    const span = spans[index];
    const row = groupIndex.get(event.group || '_default') ?? 0;
    const y = laneTop + row * laneHeight + laneHeight / 2;
    const x1 = xOf(span.start);
    const x2 = xOf(span.end);
    const color = GROUP_COLORS[row % GROUP_COLORS.length];
    context.strokeStyle = color;
    context.fillStyle = color;
    context.lineWidth = 6;
    context.lineCap = 'round';
    if (x2 - x1 >= 3) {
      context.beginPath();
      context.moveTo(x1, y);
      context.lineTo(x2, y);
      context.stroke();
    } else {
      context.beginPath();
      context.arc(x1, y, 3.5, 0, Math.PI * 2);
      context.fill();
    }
  });

  // 年份刻度：最多 6 个，避免拥挤。
  context.fillStyle = '#7a8b9a';
  context.font = '11px "Microsoft YaHei", "PingFang SC", sans-serif';
  const tickCount = 5;
  const startDate = new Date(min);
  const endDate = new Date(max);
  const startYear = startDate.getFullYear();
  const endYear = endDate.getFullYear();
  for (let tick = 0; tick <= tickCount; tick += 1) {
    const year = Math.round(startYear + ((endYear - startYear) * tick) / tickCount);
    const value = new Date(0);
    value.setFullYear(year, 0, 1);
    const x = xOf(value.getTime());
    context.fillRect(x, POSTER_HEIGHT - 44, 1, 6);
    const label = year < 0 ? `${-year} BC` : String(year);
    const width = context.measureText(label).width;
    context.fillText(label, Math.max(4, Math.min(POSTER_WIDTH - width - 4, x - width / 2)), POSTER_HEIGHT - 24);
  }
  context.fillStyle = '#8a97a3';
  context.fillText(
    ossText('study.oss.timeline.posterNote', '时间轴海报 · {count} 条事件', { count: events.length }),
    24, POSTER_HEIGHT - 6,
  );
  try {
    return canvas.toDataURL('image/png');
  } catch {
    return '';
  }
}

/**
 * 在容器内创建时间轴视图；返回 `{element, ready, getDefinition, pngURI, destroy}`。
 * 只读模式不改变交互（仍可缩放、拖动时间窗），只关闭选择与编辑。
 */
export function createTimelineView(container, rawDefinition, options = {}) {
  const definition = normalizeTimelineDefinition(rawDefinition);
  const host = el('div', { class: 'oss-timeline-view' });
  let destroyed = false;
  let library = null;
  let timeline = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function fail(message, detail) {
    if (detail) console.warn('[timelineDemo]', detail);
    host.replaceChildren(el('p', { class: 'help oss-timeline-unavailable', role: 'status' }, message));
    resolveReady({ ok: false, reason: 'failed' });
  }

  if (!definition) {
    host.append(el('p', { class: 'help oss-timeline-unavailable', role: 'status' },
      ossText('study.oss.timeline.invalid', '时间轴数据不合法：专题、日期或选项无法识别。')));
    if (container) container.replaceChildren(host);
    resolveReady({ ok: false, reason: 'invalid' });
    return {
      element: host,
      ready,
      getDefinition: () => null,
      pngURI: async () => '',
      destroy: () => {
        host.remove();
        if (container) container.replaceChildren();
        resolveReady({ ok: false, reason: 'destroyed' });
      },
    };
  }

  if (container && typeof container.replaceChildren === 'function') container.replaceChildren(host);
  const mount = el('div', { class: 'oss-timeline-mount' });
  host.append(mount, el('p', { class: 'help oss-timeline-loading' },
    ossText('study.oss.timeline.loading', '正在加载时间轴…')));
  const preset = findTimelinePreset(definition.preset_id);

  (async () => {
    try {
      library = await loadVisTimeline();
    } catch (error) {
      if (!destroyed) {
        fail(ossText('study.oss.timeline.unavailable', '时间轴不可用（vis-timeline 加载失败）。'), error);
      }
      return;
    }
    if (destroyed) return;
    try {
      const groups = buildGroups(preset, definition.events);
      const items = buildItems(library, definition.events);
      timeline = new library.Timeline(mount, items, groups, timelineOptions(definition));
      const loading = host.querySelector('.oss-timeline-loading');
      if (loading) loading.remove();
      resolveReady({ ok: true });
    } catch (error) {
      try { if (timeline) timeline.destroy(); } catch { /* 忽略 */ }
      timeline = null;
      if (!destroyed) {
        fail(ossText('study.oss.timeline.buildFailed', '时间轴构建失败：事件数据无法渲染。'), error);
      }
    }
  })();

  return {
    element: host,
    ready,
    getDefinition: () => JSON.parse(JSON.stringify(definition)),
    async pngURI() {
      return drawPoster(definition, preset);
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      try { if (timeline) timeline.destroy(); } catch { /* 忽略 */ }
      timeline = null;
      host.remove();
      if (container && typeof container.replaceChildren === 'function') container.replaceChildren();
      resolveReady({ ok: false, reason: 'destroyed' });
    },
    get library() { return library; },
    get timeline() { return timeline; },
  };
}

/**
 * 打开时间轴弹层（切换专题、自建事件、保存为学习附件）。
 * @param {{initialPreset?: string, links?: Array<object>, ref?: string, titleHint?: string,
 *          onSaved?: Function, trigger?: HTMLElement, readOnly?: boolean}} options
 */
export function openTimelineDemo(options = {}) {
  const readOnly = options.readOnly === true;
  const dialog = el('dialog', {
    class: 'oss-dialog oss-timeline-dialog',
    'data-study-tool': 'timeline-demo',
    'aria-label': ossText('study.oss.timeline.title', '时间轴'),
  });
  const titleNode = el('h2', { class: 'oss-dialog-title' }, ossText('study.oss.timeline.title', '时间轴'));
  const presetSelect = el('select', {
    class: 'input', 'aria-label': ossText('study.oss.timeline.preset', '历史专题'),
  }, ...TIMELINE_PRESETS.map(preset => el('option', { value: preset.id }, presetLabel(preset))));
  const description = el('p', { class: 'help oss-timeline-description' });
  const sourceNode = el('p', { class: 'help oss-timeline-source' });
  const canvasHost = el('div', { class: 'oss-timeline-canvas' });
  const customList = el('div', { class: 'oss-timeline-custom-list' });
  const countNode = el('p', { class: 'help oss-timeline-count', role: 'status' });
  let controller = null;
  let customEvents = [];

  const initialId = TIMELINE_PRESETS.some(item => item.id === options.initialPreset)
    ? options.initialPreset : TIMELINE_PRESETS[0].id;
  presetSelect.value = initialId;

  const titleInput = el('input', {
    class: 'input',
    placeholder: ossText('study.oss.timeline.eventTitle', '事件标题'),
    'aria-label': ossText('study.oss.timeline.eventTitle', '事件标题'),
    maxlength: '120',
  });
  const startInput = el('input', {
    class: 'input',
    placeholder: ossText('study.oss.timeline.eventStart', '开始日期'),
    'aria-label': ossText('study.oss.timeline.eventStart', '开始日期'),
    maxlength: '10',
  });
  const endInput = el('input', {
    class: 'input',
    placeholder: ossText('study.oss.timeline.eventEnd', '结束日期（可选）'),
    'aria-label': ossText('study.oss.timeline.eventEnd', '结束日期（可选）'),
    maxlength: '10',
  });
  const groupSelect = el('select', {
    class: 'input', 'aria-label': ossText('study.oss.timeline.eventGroup', '分组'),
  });
  const dateHint = el('p', { class: 'help' }, ossText(
    'study.oss.timeline.dateHint', '日期格式：1840、1840-06 或 1840-06-28（公元四位年份）。'));

  function currentPreset() {
    return findTimelinePreset(presetSelect.value) || TIMELINE_PRESETS[0];
  }

  function allEvents() {
    return [...timelinePresetEvents(presetSelect.value), ...customEvents];
  }

  function refreshGroupOptions() {
    const preset = currentPreset();
    const previous = groupSelect.value;
    groupSelect.replaceChildren(...(preset.groups || []).map(group => (
      el('option', { value: group.id }, groupLabel(preset, group.id))
    )));
    if (preset.groups && preset.groups.some(group => group.id === previous)) {
      groupSelect.value = previous;
    }
  }

  function refreshCustomList() {
    if (!customEvents.length) {
      customList.replaceChildren(el('p', { class: 'help' },
        ossText('study.oss.timeline.customEmpty', '还没有自建事件；可在上方添加。')));
      return;
    }
    customList.replaceChildren(...customEvents.map((event, index) => (
      el('div', { class: 'oss-timeline-custom-row' },
        el('span', { class: 'oss-timeline-custom-title' },
          `${event.start}${event.end ? ` ~ ${event.end}` : ''} · ${event.title}`),
        button(ossText('study.oss.timeline.removeEvent', '删除'), () => {
          customEvents = customEvents.filter((_, current) => current !== index);
          refreshCustomList();
          rebuild();
        }, 'ghost small', 'trash'))
    )));
  }

  function rebuild() {
    if (controller) {
      try { controller.destroy(); } catch { /* 忽略 */ }
      controller = null;
    }
    saveButton.disabled = true;
    const definition = {
      preset_id: presetSelect.value,
      events: allEvents(),
      options: { height: HEIGHT_DEFAULT },
    };
    const current = createTimelineView(canvasHost, definition, { readOnly });
    controller = current;
    countNode.textContent = ossText('study.oss.timeline.eventCount', '当前 {count} 条事件', {
      count: definition.events.length,
    });
    current.ready.then(outcome => {
      if (controller !== current) return;
      saveButton.disabled = !outcome || outcome.ok === false;
    }).catch(() => {});
  }

  function applyPreset() {
    const preset = currentPreset();
    description.textContent = String(preset.description || '');
    sourceNode.textContent = String(preset.source || '');
    customEvents = [];
    refreshGroupOptions();
    refreshCustomList();
    rebuild();
  }

  presetSelect.addEventListener('change', applyPreset);

  function addEvent() {
    const event = normalizeTimelineEvent({
      title: titleInput.value,
      start: startInput.value,
      end: endInput.value,
      group: groupSelect.value,
    });
    if (!event) {
      toast(ossText('study.oss.timeline.eventInvalid',
        '事件不合法：请检查标题与日期（YYYY、YYYY-MM 或 YYYY-MM-DD）。'), true);
      return;
    }
    if (customEvents.length >= MAX_ADDED_EVENTS) {
      toast(ossText('study.oss.timeline.eventLimit', '自建事件最多 {max} 条。', { max: MAX_ADDED_EVENTS }), true);
      return;
    }
    customEvents = [...customEvents, event];
    titleInput.value = '';
    startInput.value = '';
    endInput.value = '';
    refreshCustomList();
    rebuild();
    toast(ossText('study.oss.timeline.eventAdded', '已添加：{title}', { title: event.title }));
  }

  const addButton = button(ossText('study.oss.timeline.addEvent', '添加事件'), addEvent, 'secondary', 'layers');
  const resetButton = button(ossText('study.oss.timeline.reset', '重置'), () => applyPreset(), 'secondary');
  const closeButton = button(ossText('study.oss.timeline.close', '关闭'), () => dialog.close(), 'secondary');
  const saveButton = button(ossText('study.oss.timeline.save', '保存为学习附件'), event => busy(event.currentTarget, () => save()), '', 'download');

  async function save() {
    if (!controller || !controller.getDefinition()) {
      toast(ossText('study.oss.timeline.invalid', '时间轴数据不合法'), true);
      return;
    }
    const definition = normalizeTimelineDefinition({
      preset_id: presetSelect.value,
      events: allEvents(),
      options: { height: HEIGHT_DEFAULT },
    });
    if (!definition) {
      toast(ossText('study.oss.timeline.invalid', '时间轴数据不合法'), true);
      return;
    }
    const preset = currentPreset();
    const png = await controller.pngURI();
    const links = Array.isArray(options.links) ? options.links : [];
    const ref = String(options.ref || '').trim();
    const body = {
      kind: TIMELINE_KIND,
      title: (String(options.titleHint || '').trim()
        || `${ossText('study.oss.timeline.title', '时间轴')} · ${presetLabel(preset)}`).slice(0, 120),
      data: definition,
      preview: {
        kind: 'timeline',
        preset_id: definition.preset_id,
        events: definition.events,
        options: definition.options,
        ...(png && png.length < 60000 && png.startsWith('data:image/png;base64,') ? { png } : {}),
      },
      links,
    };
    if (ref) { body.source_kind = 'question'; body.source_ref = ref; }
    try {
      const created = await api('/artifacts', { method: 'POST', data: body });
      const artifact = created && created.artifact ? created.artifact : {};
      toast(ossText('study.oss.timeline.saved', '已保存为学习附件：{id}', { id: artifact.artifact_id || '' }));
      if (typeof options.onSaved === 'function') {
        try { options.onSaved({ artifact, created }); } catch { /* 忽略回调失败 */ }
      }
      dialog.close();
    } catch (error) {
      toast(error?.message || ossText('study.oss.timeline.saveFailed', '保存时间轴失败'), true);
    }
  }

  dialog.append(
    titleNode,
    el('div', { class: 'oss-timeline-toolbar' },
      el('label', { class: 'oss-timeline-field' },
        el('span', {}, ossText('study.oss.timeline.preset', '历史专题')), presetSelect)),
    description,
    sourceNode,
    canvasHost,
    countNode,
    el('div', { class: 'oss-timeline-editor' },
      el('div', { class: 'oss-timeline-form' },
        titleInput, startInput, endInput, groupSelect, addButton),
      dateHint,
      customList),
    el('div', { class: 'oss-formula-toolbar' }, resetButton, closeButton, saveButton),
  );
  if (readOnly) {
    presetSelect.disabled = true;
    for (const node of [titleInput, startInput, endInput, groupSelect, addButton, resetButton]) {
      node.disabled = true;
    }
    saveButton.hidden = true;
  }
  dialog.addEventListener('close', () => {
    if (controller) { try { controller.destroy(); } catch { /* 忽略 */ } controller = null; }
    dialog.remove();
    if (options.trigger && typeof options.trigger.focus === 'function') {
      try { options.trigger.focus(); } catch { /* 忽略 */ }
    }
  });
  document.body.append(dialog);
  dialog.showModal();
  applyPreset();
  return dialog;
}
