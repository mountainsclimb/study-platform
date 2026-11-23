/* AI 课堂播放器：幻灯片舞台 + 动作时间轴（speech 字幕/TTS、聚光灯、激光、高亮、白板、切场）+
 * 测验判分 + 交互沙箱 iframe + PBL 只读展示 + AI 老师对话抽屉 + 学习进度续播。
 *
 * 数据全部来自 `GET /api/classroom/courses/{id}`（场景内容 DSL）与学习会话接口；
 * 不伪造任何 AI 结果：交互/PBL 的占位内容会显示「内容待生成」标识，未知动作只记录并忽略。
 */
import { api } from '../core/api.js';
import { readLocal, saveLocal } from '../core/state.js';
import { el, button, badge, notice, loading, toast, busy, icon } from '../core/dom.js';
import { go } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { t } from '../core/i18n.js';
import {
  SCENE_STATUS, SLIDE_HEIGHT, SLIDE_WIDTH, contentDigest, latexNode, markdownNode,
  openExportDialog, sleep, slideTextNode, speechDuration, statusBadgeParts, typeLabel,
} from './classroomShared.js';
import { createVoiceInput } from './voiceInput.js';
import { createMultiagentPanel } from './classroomMultiagent.js';

const MUTE_KEY = 'zhixu.classroom.muted';
const SESSION_KEY_PREFIX = 'zhixu.classroom.session.';
const SAVE_THROTTLE_MS = 5000;
const UNKNOWN_ACTION_LIMIT = 3;

/* ---------------------------------------------------------------------------
 * M4 版本化演示场景（学习附件 → 课堂固定版本）
 * ---------------------------------------------------------------------------
 * 场景形状：`{id,scene_type:'demo',title,content:{type:'demo',artifact:{artifact_id,
 * revision,kind,title,data,preview},source_note,assumptions,step_count,fixed}}`。
 * 演示 data 只交给受控组件渲染（principleDemo / functionGraph / sketchBoard），
 * 标题、说明与假设一律 textContent / textarea.value 写入，不拼 innerHTML。
 * 界面文案 key 统一为 `study.m4.*`（locale JSON 由 Lead 合并，缺键回退中文兜底）。
 */

/** kind → 文案键 + 中文兜底（kind 是后端枚举，不允许拼进 DOM 文案）。 */
const DEMO_KIND_META = Object.freeze({
  function_graph: { key: 'study.m4.kind.functionGraph', fallback: '函数图' },
  whiteboard: { key: 'study.m4.kind.whiteboard', fallback: '解题白板' },
  flow_animation: { key: 'study.m4.kind.flowAnimation', fallback: '流程动画' },
  isometric: { key: 'study.m4.kind.isometric', fallback: '等距结构' },
  science_demo: { key: 'study.m4.kind.scienceDemo', fallback: '科学演示' },
  demo_3d: { key: 'study.m4.kind.demo3d', fallback: '3D 演示' },
  // M5（§5.4 机械与工厂）：机械 3D 拆解/剖切与工厂产线仿真回放。
  mech_3d: { key: 'study.m5.kind.mech3d', fallback: '机械 3D' },
  factory_sim: { key: 'study.m5.kind.factorySim', fallback: '工厂仿真' },
  molecule_3d: { key: 'study.oss.kind.molecule', fallback: '分子模型' },
});

/** 交给 principleDemo 渲染的 kind（其余读取本地 functionGraph / sketchBoard）。 */
const PRINCIPLE_DEMO_KINDS = new Set(['flow_animation', 'isometric', 'demo_3d', 'science_demo']);

/** 取 `study.m4.*` 文案：locale 缺键时回退中文常量并做 `{name}` 插值。 */
function m4Text(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function demoKindLabel(kind) {
  const meta = DEMO_KIND_META[String(kind || '')];
  return meta ? m4Text(meta.key, meta.fallback) : m4Text('study.m4.kind.other', '学习附件');
}

/**
 * 海报只接受内联 data URL（后端 preview.png 约定），拒绝外链与脚本伪协议；
 * 长度与后端 preview ≤64KiB 的限额对齐，避免把异常大字符串塞进 img src。
 */
function demoPosterDataUrl(preview) {
  const raw = String(preview && typeof preview === 'object' ? preview.png || '' : '').trim();
  if (!raw || raw.length > 200000) return '';
  return /^data:image\/(?:png|jpe?g|webp);base64,/i.test(raw) ? raw : '';
}

/**
 * science_demo 等后端新增 kind 没有独立渲染器：按数据结构选择本地视图
 * （parts → 等距/3D，nodes → 流程），避免把可渲染数据误报成加载失败。
 */
function demoPrincipleKind(kind, data) {
  if (kind === 'flow_animation' || kind === 'isometric' || kind === 'demo_3d') return kind;
  if (data && typeof data === 'object') {
    if (Array.isArray(data.parts)) return 'isometric';
    if (Array.isArray(data.nodes)) return 'flow_animation';
  }
  return kind;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, Number.isFinite(Number(value)) ? Number(value) : min));
}

function sessionKey(courseId) {
  return SESSION_KEY_PREFIX + courseId;
}

function elementBoxList(content) {
  const elements = content?.type === 'slide' ? (content.canvas?.elements || []) : [];
  const map = new Map();
  for (const item of elements) {
    if (item && item.id) map.set(String(item.id), item);
  }
  return map;
}

function absoluteStyle(element, extra = '') {
  const parts = [
    `left:${Number(element.left) || 0}px`,
    `top:${Number(element.top) || 0}px`,
    `width:${Number(element.width) || 0}px`,
    `height:${Number(element.height) || 0}px`,
  ];
  if (Number(element.rotate)) parts.push(`transform:rotate(${Number(element.rotate)}deg)`);
  return `${parts.join(';')}${extra ? ';' + extra : ''}`;
}

function makeSvg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
  }
  return node;
}

function chartSvg(element) {
  const data = element.data || {};
  const labels = Array.isArray(data.labels) ? data.labels : [];
  const series = Array.isArray(data.series) ? data.series.filter(row => Array.isArray(row)) : [];
  const width = Math.max(160, Number(element.width) || 320);
  const height = Math.max(120, Number(element.height) || 220);
  const svg = makeSvg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': '图表' });
  const colors = Array.isArray(element.themeColors) && element.themeColors.length ? element.themeColors : ['#1f6feb', '#e8590c', '#2f9e44'];
  const type = String(element.chartType || 'bar');
  const padLeft = 34;
  const padBottom = 24;
  const plotWidth = width - padLeft - 12;
  const plotHeight = height - padBottom - 16;
  const values = series.flat().map(Number).filter(Number.isFinite);
  const maxValue = Math.max(1, ...values);
  const pointX = index => padLeft + (labels.length <= 1 ? plotWidth / 2 : (plotWidth * index) / Math.max(1, labels.length - 1));
  const pointY = value => 12 + plotHeight - (Number(value) || 0) / maxValue * plotHeight;
  // 坐标轴
  svg.append(makeSvg('line', { x1: padLeft, y1: 12, x2: padLeft, y2: 12 + plotHeight, stroke: '#cbd5e1', 'stroke-width': 1 }));
  svg.append(makeSvg('line', { x1: padLeft, y1: 12 + plotHeight, x2: width - 8, y2: 12 + plotHeight, stroke: '#cbd5e1', 'stroke-width': 1 }));
  labels.forEach((label, index) => {
    const text = makeSvg('text', { x: pointX(index), y: height - 6, 'font-size': 11, 'text-anchor': 'middle', fill: '#64748b' });
    text.textContent = String(label).slice(0, 10);
    svg.append(text);
  });
  if (type === 'pie' || type === 'ring') {
    const row = series[0] || [];
    const total = row.reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0) || 1;
    let angle = -Math.PI / 2;
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) / 2 - 16;
    row.forEach((value, index) => {
      const slice = (Math.max(0, Number(value) || 0) / total) * Math.PI * 2;
      if (slice <= 0) return;
      const x1 = cx + radius * Math.cos(angle);
      const y1 = cy + radius * Math.sin(angle);
      const x2 = cx + radius * Math.cos(angle + slice);
      const y2 = cy + radius * Math.sin(angle + slice);
      const large = slice > Math.PI ? 1 : 0;
      svg.append(makeSvg('path', {
        d: `M ${cx} ${cy} L ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`,
        fill: colors[index % colors.length], 'fill-opacity': type === 'ring' ? 1 : 0.86,
      }));
      angle += slice;
    });
    if (type === 'ring') {
      svg.append(makeSvg('circle', { cx, cy, r: radius * 0.55, fill: '#ffffff' }));
    }
    return svg;
  }
  series.forEach((row, seriesIndex) => {
    const color = colors[seriesIndex % colors.length];
    if (type === 'line' || type === 'area' || type === 'radar' || type === 'scatter') {
      const points = row.map((value, index) => `${pointX(index)},${pointY(value)}`).join(' ');
      if (type === 'area') {
        svg.append(makeSvg('polygon', {
          points: `${padLeft},${12 + plotHeight} ${points} ${width - 8},${12 + plotHeight}`,
          fill: color, 'fill-opacity': 0.16,
        }));
      }
      svg.append(makeSvg('polyline', { points, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round' }));
      row.forEach((value, index) => {
        svg.append(makeSvg('circle', { cx: pointX(index), cy: pointY(value), r: 2.8, fill: color }));
      });
    } else {
      const columnWidth = Math.max(8, plotWidth / Math.max(1, labels.length) / Math.max(1, series.length) - 4);
      row.forEach((value, index) => {
        const x = padLeft + (plotWidth / Math.max(1, labels.length)) * index + seriesIndex * (columnWidth + 4) + 2;
        const y = pointY(value);
        svg.append(makeSvg('rect', {
          x, y, width: columnWidth, height: Math.max(1, 12 + plotHeight - y),
          fill: color, 'fill-opacity': 0.86, rx: 2,
        }));
      });
    }
  });
  return svg;
}

export function createPlayerView({ container, version, courseId }) {
  const root = el('div', { class: 'cls-page cls-player' });
  container.append(root);

  let detail = null;
  let course = null;
  let scenes = [];
  let session = null;
  let sceneIndex = 0;
  let actionIndex = 0;
  let playing = false;
  let runToken = 0;
  let destroyed = false;
  let muted = Boolean(readLocal(MUTE_KEY, false));

  let stageHost = null;
  let captionNode = null;
  let controlRefs = null;
  let stripNode = null;
  let fitCleanups = [];
  let currentCanvas = null;
  let currentScene = () => scenes[sceneIndex] || null;
  let currentActions = () => {
    const scene = currentScene();
    if (!scene) return [];
    const actions = Array.isArray(scene.actions) && scene.actions.length ? scene.actions : (scene.meta?.actions || []);
    return Array.isArray(actions) ? actions : [];
  };
  let quizStates = {};
  let unknownActions = [];
  let tutorRefs = null;
  let tutorMessages = [];
  let tutorVoice = null;
  let multiagent = null;
  let chatTurns = [];
  let currentFrame = null;
  let frameReady = false;
  // 交互页运行时状态（由 iframe postMessage 上报并转发到会话 progress，供 AI 老师追问使用）。
  let interactiveState = null;
  let endCardShown = false;
  let saveTimer = null;
  // P4 课堂媒体：批量旁白（course.config.narration 优先播放）、音色目录、AI 配图弹层。
  let narrationUsedScene = null;
  let narrationAudio = null;
  let narrationAudioFinish = null;
  let narrationItems = [];
  let voiceCatalog = null;
  let imageCapabilities = null;
  let mediaDialogs = new Set();
  // M4：当前演示场景的动态组件控制器（切场/离开必须 destroy，避免计时器/画布泄漏）。
  let demoController = null;
  let demoToken = 0;

  // ---------------------------------------------------------------------------
  // 语音（浏览器 speechSynthesis，可静音）
  // ---------------------------------------------------------------------------
  const synth = typeof window.speechSynthesis !== 'undefined' ? window.speechSynthesis : null;
  let speakFinish = null;

  function pickVoice() {
    if (!synth || typeof synth.getVoices !== 'function') return null;
    const voices = synth.getVoices() || [];
    return voices.find(voice => /^zh(-|_)/i.test(voice.lang || ''))
      || voices.find(voice => /chinese|中文|普通话|mandarin/i.test(voice.name || ''))
      || null;
  }

  function stopNarrationAudio() {
    const finish = narrationAudioFinish;
    narrationAudioFinish = null;
    const audio = narrationAudio;
    narrationAudio = null;
    if (audio) {
      try { audio.pause(); } catch { /* 忽略 */ }
      try { audio.removeAttribute('src'); audio.load?.(); } catch { /* 忽略 */ }
    }
    if (finish) {
      try { finish(); } catch { /* 忽略 */ }
    }
  }

  function cancelSpeech() {
    stopNarrationAudio();
    try { if (speakFinish) speakFinish(); } catch { /* 忽略 */ }
    speakFinish = null;
    try { if (synth) synth.cancel(); } catch { /* 忽略 */ }
  }

  function speak(text, token) {
    const source = String(text || '').trim();
    if (!source) return Promise.resolve();
    return new Promise(resolve => {
      let done = false;
      let timer = null;
      const finish = () => {
        if (done) return;
        done = true;
        if (speakFinish === finish) speakFinish = null;
        if (timer) clearTimeout(timer);
        resolve();
      };
      speakFinish = finish;
      timer = setTimeout(finish, Math.max(4000, source.length * 240 + 3000));
      if (muted || !synth) {
        setTimeout(finish, speechDuration(source));
        return;
      }
      if (token !== runToken || destroyed) { finish(); return; }
      try {
        const utterance = new SpeechSynthesisUtterance(source);
        utterance.lang = 'zh-CN';
        utterance.rate = 1;
        utterance.pitch = 1;
        const voice = pickVoice();
        if (voice) utterance.voice = voice;
        utterance.onend = finish;
        utterance.onerror = finish;
        synth.cancel();
        synth.speak(utterance);
      } catch {
        setTimeout(finish, speechDuration(source));
      }
    });
  }

  /**
   * 当前场景的已合成旁白（P4：``course.config.narration[scene_id]``）。
   * 有旁白时整页旁白音频优先于浏览器 TTS；没有时回退 ``speechSynthesis``。
   */
  function narrationEntryFor(scene) {
    const map = course && course.config ? course.config.narration : null;
    if (!scene || !map || typeof map !== 'object') return null;
    const entry = map[String(scene.id)];
    if (!entry || typeof entry !== 'object') return null;
    const assetId = Number(entry.asset_id);
    const url = String(entry.url || '');
    if (!(Number.isFinite(assetId) && assetId > 0) && !url) return null;
    return {
      asset_id: Number.isFinite(assetId) && assetId > 0 ? assetId : null,
      url,
      voice: String(entry.voice || ''),
      chars: Number(entry.chars) || 0,
    };
  }

  async function speakSceneText(text, token) {
    const source = String(text || '');
    const scene = currentScene();
    const entry = scene ? narrationEntryFor(scene) : null;
    if (!entry || muted) {
      await speak(source, token);
      return;
    }
    if (narrationUsedScene === String(scene.id)) {
      // 整页旁白音频已覆盖本页全部 speech：后续句子只停留字幕，避免重复朗读。
      captionNode.textContent = source;
      await sleep(Math.min(1800, Math.max(700, source.length * 90)));
      return;
    }
    narrationUsedScene = String(scene.id);
    captionNode.textContent = source;
    await playAudioAction(entry.asset_id ? { asset_id: entry.asset_id } : { url: entry.url });
  }

  /**
   * 多智能体讨论/插话的单段朗读：返回可暂停/继续/取消的句柄，供播放队列控制。
   * 不占用场景时间轴的 runToken；角色不同时用 pitch/rate 区分声音。
   */
  function speakSegment(text, { role = 'teacher', speaker = '' } = {}) {
    const source = String(text || '').trim();
    const caption = source ? `${speaker ? speaker + '：' : ''}${source}` : '';
    captionNode.textContent = caption;
    if (!source) {
      return { promise: Promise.resolve(), pause() {}, resume() {}, cancel() {} };
    }
    let done = false;
    let timer = null;
    let resolvePromise = null;
    const promise = new Promise(resolve => { resolvePromise = resolve; });
    const finish = () => {
      if (done) return;
      done = true;
      if (timer) { clearTimeout(timer); timer = null; }
      resolvePromise();
    };
    const fallbackDelay = Math.max(1600, speechDuration(source));
    const roleStyle = role === 'student'
      ? { pitch: 1.12, rate: 1.04 }
      : role === 'assistant'
        ? { pitch: 1.05, rate: 1.02 }
        : { pitch: 1, rate: 1 };
    if (muted || !synth) {
      timer = setTimeout(finish, fallbackDelay);
    } else {
      try {
        const utterance = new SpeechSynthesisUtterance(source);
        utterance.lang = 'zh-CN';
        utterance.rate = roleStyle.rate;
        utterance.pitch = roleStyle.pitch;
        const voice = pickVoice();
        if (voice) utterance.voice = voice;
        utterance.onend = finish;
        utterance.onerror = finish;
        synth.cancel();
        synth.speak(utterance);
        timer = setTimeout(finish, Math.max(6000, source.length * 260 + 4000));
      } catch {
        timer = setTimeout(finish, fallbackDelay);
      }
    }
    return {
      promise,
      pause() {
        try { synth?.pause?.(); } catch { /* 忽略 */ }
      },
      resume() {
        try { synth?.resume?.(); } catch { /* 忽略 */ }
      },
      cancel() {
        try { synth?.cancel?.(); } catch { /* 忽略 */ }
        finish();
      },
    };
  }

  function toggleMute() {
    muted = !muted;
    saveLocal(MUTE_KEY, muted);
    if (muted) cancelSpeech();
    updateControls();
  }

  // ---------------------------------------------------------------------------
  // 进度保存（5s 节流；切场/离开立即保存；页面隐藏用 keepalive）
  // ---------------------------------------------------------------------------
  function progressPayload() {
    const scene = currentScene();
    const payload = {
      scene_index: sceneIndex,
      action_index: actionIndex,
      scene_id: scene ? scene.id : null,
      updated_at: new Date().toISOString(),
    };
    // 后端把交互状态存在 progress.interactive；这里一并回写，避免进度保存覆盖它。
    if (interactiveState && scene && String(interactiveState.scene_id) === String(scene.id)) {
      payload.interactive = interactiveState;
    }
    return payload;
  }

  function saveNow({ beacon = false } = {}) {
    if (!session) return;
    const payload = progressPayload();
    session.progress = payload;
    if (beacon && typeof fetch === 'function') {
      try {
        fetch(`/api/classroom/sessions/${session.id}`, {
          method: 'PATCH',
          credentials: 'same-origin',
          keepalive: true,
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
          body: JSON.stringify({ scene_id: payload.scene_id, progress: payload }),
        }).catch(() => {});
      } catch { /* 忽略 */ }
      return;
    }
    api(`/classroom/sessions/${session.id}`, {
      method: 'PATCH',
      data: { scene_id: payload.scene_id, progress: payload },
    }).catch(() => {});
  }

  function scheduleSave(force = false) {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (force) { saveNow(); return; }
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveNow();
    }, SAVE_THROTTLE_MS);
  }

  // ---------------------------------------------------------------------------
  // 舞台渲染
  // ---------------------------------------------------------------------------
  function clearFit() {
    for (const fn of fitCleanups) {
      try { fn(); } catch { /* 忽略 */ }
    }
    fitCleanups = [];
    currentCanvas = null;
    currentFrame = null;
    frameReady = false;
  }

  function attachFit(shell, canvas) {
    const fit = () => {
      if (!shell.isConnected) return;
      const width = shell.clientWidth;
      if (!width) return;
      const scale = width / SLIDE_WIDTH;
      canvas.style.transform = `scale(${scale})`;
      shell.style.height = `${SLIDE_HEIGHT * scale}px`;
    };
    fit();
    if (typeof ResizeObserver === 'function') {
      const resizer = new ResizeObserver(fit);
      resizer.observe(shell);
      fitCleanups.push(() => resizer.disconnect());
    } else {
      window.addEventListener('resize', fit);
      fitCleanups.push(() => window.removeEventListener('resize', fit));
    }
  }

  function renderTextElement(element, theme) {
    const node = el('div', { class: 'cls-el cls-el-text', style: absoluteStyle(element, `font-family:${theme.fontName || 'Microsoft YaHei'}`) });
    node.append(slideTextNode(element.content, element.text));
    return node;
  }

  function renderShapeElement(element) {
    const box = Array.isArray(element.viewBox) && element.viewBox.length >= 2 ? element.viewBox : [1, 1];
    const viewBox = `0 0 ${Number(box[0]) || 1} ${Number(box[1]) || 1}`;
    const svg = makeSvg('svg', {
      viewBox,
      preserveAspectRatio: 'none',
      class: 'cls-shape-svg',
    });
    svg.append(makeSvg('path', { d: element.path, fill: element.fill || '#5b9bd5' }));
    const node = el('div', { class: 'cls-el cls-el-shape', style: absoluteStyle(element) }, svg);
    return node;
  }

  function renderLineElement(element) {
    const start = Array.isArray(element.start) ? element.start : [0, 0];
    const end = Array.isArray(element.end) ? element.end : [0, 0];
    const left = Number(element.left) || 0;
    const top = Number(element.top) || 0;
    const x1 = left + (Number(start[0]) || 0);
    const y1 = top + (Number(start[1]) || 0);
    const x2 = left + (Number(end[0]) || 0);
    const y2 = top + (Number(end[1]) || 0);
    const svg = makeSvg('svg', {
      class: 'cls-line-svg', viewBox: `0 0 ${SLIDE_WIDTH} ${SLIDE_HEIGHT}`,
      width: SLIDE_WIDTH, height: SLIDE_HEIGHT,
    });
    const dash = element.style === 'dashed' ? '8 6' : element.style === 'dotted' ? '2 5' : null;
    svg.append(makeSvg('line', {
      x1, y1, x2, y2, stroke: element.color || '#333333',
      'stroke-width': Math.max(1, Number(element.width) || 2),
      'stroke-linecap': 'round', ...(dash ? { 'stroke-dasharray': dash } : {}),
    }));
    const points = Array.isArray(element.points) ? element.points : ['', ''];
    const angle = Math.atan2(y2 - y1, x2 - x1);
    const arrowSize = 9;
    const addArrow = (x, y, direction) => {
      const a = angle + direction;
      svg.append(makeSvg('path', {
        d: `M ${x} ${y} l ${arrowSize * Math.cos(a)} ${arrowSize * Math.sin(a)} l ${arrowSize * Math.cos(a + 1.2)} ${arrowSize * Math.sin(a + 1.2)} Z`,
        fill: element.color || '#333333',
      }));
    };
    if (points[1] === 'arrow') addArrow(x2, y2, Math.PI);
    if (points[0] === 'arrow') addArrow(x1, y1, 0);
    if (points[1] === 'dot') svg.append(makeSvg('circle', { cx: x2, cy: y2, r: Math.max(2, (Number(element.width) || 2) * 0.9), fill: element.color || '#333333' }));
    if (points[0] === 'dot') svg.append(makeSvg('circle', { cx: x1, cy: y1, r: Math.max(2, (Number(element.width) || 2) * 0.9), fill: element.color || '#333333' }));
    const node = el('div', { class: 'cls-el cls-el-line' }, svg);
    return node;
  }

  function renderImageElement(element) {
    const raw = String(element.src || '');
    let src = raw;
    if (/^\d+$/.test(raw)) src = `/api/classroom/assets/${raw}`;
    const node = el('div', { class: 'cls-el cls-el-image', style: absoluteStyle(element, 'border-radius:6px;overflow:hidden') });
    const image = el('img', { src, alt: element.alt || '幻灯片配图', loading: 'lazy' });
    image.addEventListener('error', () => {
      image.remove();
      node.append(el('span', { class: 'cls-image-fallback' }, '图片不可用'));
    });
    node.append(image);
    return node;
  }

  function renderLatexElement(element) {
    const node = el('div', { class: 'cls-el cls-el-latex', style: absoluteStyle(element, 'display:flex;align-items:center;justify-content:center;overflow:hidden') });
    const source = String(element.latex || '');
    const width = Number(element.width) || 300;
    const height = Number(element.height) || 90;
    // 公式越长字号越小，避免超出元素框被裁切（KaTeX 字号随容器 font-size 等比变化）。
    const byHeight = height * 0.46;
    const byWidth = width / Math.max(8, source.length) * 1.15;
    const size = clamp(Math.round(Math.min(byHeight, byWidth, 36)), 9, 36);
    node.style.fontSize = `${size}px`;
    node.append(latexNode(source, { display: true }));
    return node;
  }

  function renderTableElement(element) {
    const rows = Array.isArray(element.data) ? element.data : [];
    const colWidths = Array.isArray(element.colWidths) && element.colWidths.length ? element.colWidths : [];
    const node = el('div', { class: 'cls-el cls-el-table', style: absoluteStyle(element, 'overflow:hidden') });
    const table = el('table', { class: 'cls-slide-table' });
    const colgroup = el('colgroup');
    const columnCount = Math.max(...rows.map(row => (Array.isArray(row) ? row.length : 0)), colWidths.length, 1);
    for (let index = 0; index < columnCount; index += 1) {
      colgroup.append(el('col', { style: `width:${Math.round((Number(colWidths[index]) || 1 / columnCount) * 1000) / 10}%` }));
    }
    table.append(colgroup);
    for (const row of rows) {
      const tr = el('tr');
      for (const cell of Array.isArray(row) ? row : []) {
        const td = el('td', { colspan: Number(cell.colspan) || 1, rowspan: Number(cell.rowspan) || 1 });
        td.textContent = String(cell.text ?? cell ?? '');
        tr.append(td);
      }
      table.append(tr);
    }
    node.append(table);
    return node;
  }

  function renderChartElement(element) {
    const node = el('div', { class: 'cls-el cls-el-chart', style: absoluteStyle(element) });
    try {
      node.append(chartSvg(element));
    } catch {
      node.append(el('span', { class: 'cls-image-fallback' }, '图表数据不可用'));
    }
    return node;
  }

  function renderCodeElement(element) {
    const node = el('div', { class: 'cls-el cls-el-code', style: absoluteStyle(element, 'overflow:auto') });
    const pre = el('pre', { class: 'cls-slide-code' });
    const code = el('code', {});
    code.textContent = String(element.code || '');
    pre.append(code);
    node.append(pre);
    return node;
  }

  function renderSlideElement(element, theme) {
    switch (element.type) {
      case 'text': return renderTextElement(element, theme);
      case 'shape': return renderShapeElement(element);
      case 'line': return renderLineElement(element);
      case 'image': return renderImageElement(element);
      case 'latex': return renderLatexElement(element);
      case 'table': return renderTableElement(element);
      case 'chart': return renderChartElement(element);
      case 'code': return renderCodeElement(element);
      default: return null;
    }
  }

  function renderSlideStage(scene) {
    const content = scene.content || {};
    const canvasData = content.canvas || {};
    const theme = canvasData.theme || {};
    // 深度交互模式：幻灯片底部「动手试一试」提示条（优先 content.interactive_hint，其次 meta）。
    const rawHint = content.interactive_hint || scene.meta?.interactive_hint || null;
    const hintText = rawHint && typeof rawHint === 'object'
      ? String(rawHint.text || '').trim()
      : String(rawHint || '').trim();
    const hintElementId = rawHint && typeof rawHint === 'object' && rawHint.element_id
      ? String(rawHint.element_id)
      : '';
    const shell = el('div', { class: 'cls-stage-shell' });
    const stage = el('div', { class: 'cls-stage' });
    const canvas = el('div', {
      class: 'cls-stage-canvas',
      style: `width:${SLIDE_WIDTH}px;height:${SLIDE_HEIGHT}px;background:${theme.backgroundColor || '#ffffff'}`,
    });
    const elements = Array.isArray(canvasData.elements) ? canvasData.elements : [];
    for (const element of elements) {
      // 提示条与画布 hint 元素二者择一：有 interactive_hint 时用底部强调提示条替代该元素。
      if (hintText && hintElementId && String(element?.id || '') === hintElementId) continue;
      const node = renderSlideElement(element, theme);
      if (node) canvas.append(node);
    }
    canvas.append(el('div', { class: 'cls-effect-spot', hidden: true }));
    canvas.append(el('div', { class: 'cls-effect-highlight', hidden: true }));
    canvas.append(el('div', { class: 'cls-effect-laser', hidden: true }));
    canvas.append(el('div', { class: 'cls-whiteboard', 'aria-hidden': 'true' }));
    stage.append(canvas);
    shell.append(stage);
    if (hintText) {
      const hintBody = hintText.replace(/^动手试一试\s*[:：]\s*/, '');
      shell.append(el('div', { class: 'cls-interactive-hint', role: 'note', 'aria-label': '动手试一试提示' },
        el('strong', {}, '动手试一试'),
        el('span', {}, hintBody || hintText)));
    }
    currentCanvas = canvas;
    attachFit(shell, canvas);
    return shell;
  }

  function quizState(scene) {
    if (!quizStates[scene.id]) quizStates[scene.id] = { answers: {}, result: null };
    return quizStates[scene.id];
  }

  function renderQuizStage(scene) {
    const content = scene.content || {};
    const questions = Array.isArray(content.questions) ? content.questions : [];
    const state = quizState(scene);
    const panel = el('div', { class: 'cls-quiz-panel' });

    if (state.result) {
      const result = state.result;
      const percent = result.max_score ? Math.round((result.score / result.max_score) * 100) : 0;
      panel.append(el('div', { class: 'cls-quiz-result-head' },
        el('div', {},
          el('h3', {}, '本页测验已判分'),
          el('p', { class: 'help' }, `得分 ${result.score} / ${result.max_score}（${percent}%）· 正确 ${result.correct_count}/${result.question_count} 题${result.needs_review_count ? ` · ${result.needs_review_count} 道简答待核验` : ''}`)),
        badge(percent >= 60 ? '达标' : '需要巩固', percent >= 60 ? 'teal' : 'amber')));
      for (const item of result.detail || []) {
        const variant = item.needs_review ? 'amber' : item.correct ? 'teal' : 'red';
        const label = item.needs_review ? '待核验' : item.correct ? '正确' : '错误';
        panel.append(el('div', { class: `cls-quiz-result-item ${item.correct ? 'is-correct' : item.needs_review ? 'is-review' : 'is-wrong'}` },
          el('div', { class: 'cls-quiz-result-title' },
            el('strong', {}, `${item.question_id} · ${item.question}`),
            badge(label, variant)),
          el('p', { class: 'help' }, item.needs_review
            ? `你的作答：${Array.isArray(item.given) ? item.given.join(' ') : item.given || '（未作答）'}；简答题需要人工/AI 复核，不计分。`
            : `你的作答：${(item.given || []).join('') || '（未作答）'}；正确答案：${(item.correct_answer || []).join('')}`),
          item.analysis ? el('p', { class: 'cls-quiz-analysis' }, `解析：${item.analysis}`) : null,
          item.needs_review && item.comment_prompt ? el('p', { class: 'help' }, `评分标准：${item.comment_prompt}`) : null));
      }
      panel.append(el('div', { class: 'row cls-quiz-actions' },
        button('再做一次', () => {
          quizStates[scene.id] = { answers: {}, result: null };
          renderScene(sceneIndex, { resumeAction: 0 });
        }, 'secondary', 'repeat'),
        button('下一场景', () => switchToScene(sceneIndex + 1, { keepPlaying: false }), '', 'chevron')));
      return panel;
    }

    panel.append(el('div', { class: 'cls-quiz-head' },
      el('div', {},
        el('h3', {}, scene.title || '课堂测验'),
        el('p', { class: 'help' }, `共 ${questions.length} 道题 · 单选/多选自动判分，简答题标为待核验。`)),
      badge('测验', 'blue')));

    questions.forEach((question, index) => {
      const type = String(question.type || 'single');
      const box = el('div', { class: 'cls-quiz-question' });
      box.append(el('div', { class: 'cls-quiz-question-head' },
        el('span', { class: 'cls-quiz-index' }, String(index + 1)),
        el('strong', {}, question.question || '（题干缺失）'),
        badge(type === 'single' ? '单选' : type === 'multiple' ? '多选' : '简答', type === 'short_answer' ? 'amber' : ''),
        el('span', { class: 'cls-quiz-points' }, `${Number(question.points) || 0} 分`)));
      if (type === 'short_answer') {
        const textarea = el('textarea', {
          class: 'cls-quiz-short', rows: 4, placeholder: '写下你的答案（简答题由老师/AI 复核，不硬判对错）',
          value: (state.answers[question.id] || [''])[0] || '',
          onInput: event => { state.answers[question.id] = [event.currentTarget.value]; },
        });
        box.append(textarea);
      } else {
        const options = el('div', { class: 'cls-quiz-options' });
        for (const option of Array.isArray(question.options) ? question.options : []) {
          const name = `quiz_${scene.id}_${question.id}`;
          const input = el('input', {
            type: type === 'multiple' ? 'checkbox' : 'radio',
            name,
            value: option.value,
            onChange: () => {
              const selected = new Set(state.answers[question.id] || []);
              if (type === 'multiple') {
                if (input.checked) selected.add(option.value);
                else selected.delete(option.value);
              } else {
                selected.clear();
                selected.add(option.value);
              }
              state.answers[question.id] = [...selected];
            },
          });
          options.append(el('label', { class: 'cls-quiz-option' }, input, el('span', { class: 'cls-quiz-option-value' }, option.value || ''), el('span', {}, option.label || '')));
        }
        box.append(options);
      }
      panel.append(box);
    });

    const submit = button('提交判分', null, '', 'check');
    submit.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const answers = [];
      for (const question of questions) {
        const values = state.answers[question.id] || [];
        const cleaned = values.map(value => String(value || '').trim()).filter(Boolean);
        if (cleaned.length) answers.push({ question_id: question.id, values: cleaned });
      }
      if (!answers.length) { toast('请先作答至少一道题', true); return; }
      try {
        trigger.disabled = true;
        const result = await api(`/classroom/sessions/${session.id}/quiz`, {
          method: 'POST',
          data: { scene_id: scene.id, answers },
        });
        state.result = result;
        toast(`判分完成：${result.score} / ${result.max_score} 分`);
        renderScene(sceneIndex, { resumeAction: actionIndex });
      } catch (error) {
        toast(error.message || '提交判分失败', true);
      } finally {
        if (trigger.isConnected) trigger.disabled = false;
      }
    });
    panel.append(el('div', { class: 'row cls-quiz-actions' },
      submit,
      button('播放开场旁白', event => busy(event.currentTarget, async () => {
        const speech = currentActions().find(action => action.type === 'speech');
        if (!speech) { toast('本页没有旁白'); return; }
        captionNode.textContent = speech.text || '';
        narrationUsedScene = null;
        await speakSceneText(speech.text, runToken);
      }), 'secondary small', 'presentation')));
    return panel;
  }

  function renderInteractiveStage(scene) {
    const frameWrap = el('div', { class: 'cls-interactive-wrap' });
    const height = clamp(Number(scene.content?.height) || 480, 280, 900);
    if (scene.meta?.placeholder) {
      frameWrap.append(notice('该交互场景内容尚未生成（占位）：可点击「重新生成该场景」让 AI 生成真实交互页面。', 'warning'));
    }
    const frame = el('iframe', {
      class: 'cls-interactive-frame',
      sandbox: 'allow-scripts',
      title: scene.title || '交互场景',
      loading: 'eager',
      style: `height:${height}px`,
    });
    frame.src = `/api/classroom/scenes/${scene.id}/html`;
    currentFrame = frame;
    frameReady = false;
    frame.addEventListener('load', () => { frameReady = true; });
    frameWrap.append(frame);
    const status = el('p', { class: 'help cls-interactive-status', 'aria-live': 'polite' },
      interactiveState && String(interactiveState.scene_id) === String(scene.id) && interactiveState.summary
        ? `上次交互状态：${String(interactiveState.summary).slice(0, 200)}（会同步给 AI 老师）`
        : '交互页面在沙箱 iframe 中运行（sandbox="allow-scripts"，无同源权限）；运行时状态会同步给 AI 老师。');
    const reload = () => {
      frameReady = false;
      status.classList.remove('is-error');
      status.textContent = '正在重新加载交互页面…';
      frame.src = `/api/classroom/scenes/${scene.id}/html?ts=${Date.now()}`;
    };
    setTimeout(() => {
      if (destroyed || currentFrame !== frame) return;
      if (!frameReady) status.textContent = '交互页面加载较慢：如长时间空白，可点击「重新加载」。';
    }, 8000);
    frameWrap.append(el('div', { class: 'row cls-interactive-actions' },
      button('重新加载', reload, 'secondary small', 'repeat'),
      button('重新生成该场景', event => busy(event.currentTarget, () => regenerateInteractiveScene(scene)),
        'secondary small', 'rotate'),
      button('让 AI 讲讲这一页', () => openTutor(), 'secondary small', 'sparkle')));
    frameWrap.append(status);
    return frameWrap;
  }

  async function regenerateInteractiveScene(scene) {
    let connection = null;
    try {
      const listing = await api('/ai/jobs');
      const match = (listing.items || []).find(job => job.task === 'course'
        && Number(job.request?.params?.course_id) === Number(courseId));
      if (match?.request?.provider_id && match?.request?.model) {
        connection = { provider_id: match.request.provider_id, model: match.request.model };
      }
    } catch { /* 继续用提示引导 */ }
    if (!connection) {
      toast('请先在「大纲与场景」页选择模型并重新生成该课程，再回来查看交互内容。', true);
      return;
    }
    try {
      await api(`/classroom/courses/${courseId}/scenes/${scene.id}/regenerate`, {
        method: 'POST',
        data: { ...connection, instruction: '重新生成该交互场景，保证页面真实可用' },
      });
      toast('已创建交互场景重新生成任务，完成后刷新本页即可看到新内容。');
    } catch (error) {
      toast(error.message || '重新生成失败', true);
    }
  }

  function renderPblStage(scene) {
    const project = scene.content?.project || {};
    const wrap = el('div', { class: 'cls-pbl' });
    if (scene.meta?.placeholder) wrap.append(notice('该项目式学习场景内容尚未生成：可回到「大纲与场景」页重新生成该场景。', 'warning'));
    const roleNodes = (Array.isArray(project.roles) ? project.roles : []).map(role => {
      if (typeof role === 'string') return badge(role, 'teal');
      const label = role?.name || role?.title || '';
      if (!label) return null;
      return el('div', { class: 'cls-pbl-role' },
        badge(label, 'teal'),
        role.description ? el('p', { class: 'help' }, role.description) : null);
    }).filter(Boolean);
    const rubricNodes = (Array.isArray(project.rubric) ? project.rubric : []).map(item => {
      if (typeof item === 'string') return el('li', {}, item);
      if (!item || typeof item !== 'object') return null;
      const criterion = item.criterion || item.name || item.dimension || '';
      const levelsValue = item.levels ?? item.level ?? '';
      let levelsText = '';
      if (typeof levelsValue === 'string') levelsText = levelsValue;
      else if (Array.isArray(levelsValue)) {
        levelsText = levelsValue.map(level => (typeof level === 'string'
          ? level
          : (level?.name || level?.label || level?.level || level?.title || ''))).filter(Boolean).join(' / ');
      } else if (levelsValue && typeof levelsValue === 'object') {
        levelsText = Object.keys(levelsValue).join(' / ');
      }
      return el('li', {},
        criterion ? el('strong', {}, String(criterion)) : null,
        levelsText ? el('span', { class: 'help' }, `：${levelsText}`) : null);
    }).filter(Boolean);
    wrap.append(el('div', { class: 'card cls-pbl-card' },
      el('div', { class: 'cls-pbl-head' },
        el('div', {},
          el('h3', {}, project.title || scene.title || '项目式学习'),
          el('p', { class: 'help' }, project.background || '（项目背景待生成）')),
        badge('PBL', 'blue')),
      Array.isArray(project.goals) && project.goals.length
        ? el('div', {},
          el('h4', {}, '学习目标'),
          el('ul', { class: 'cls-pbl-goals' }, project.goals.map(goal => el('li', {}, String(goal)))))
        : null,
      el('h4', {}, '角色'),
      roleNodes.length
        ? el('div', { class: 'cls-pbl-roles' }, roleNodes)
        : el('p', { class: 'help' }, '项目角色待生成。'),
      el('h4', {}, '里程碑'),
      Array.isArray(project.milestones) && project.milestones.length
        ? el('div', { class: 'cls-pbl-milestones' }, project.milestones.map((milestone, index) => el('div', { class: 'cls-pbl-milestone' },
          el('div', { class: 'cls-pbl-milestone-head' }, el('strong', {}, `${index + 1}. ${milestone.title || '未命名里程碑'}`)),
          milestone.description ? el('p', { class: 'help' }, milestone.description) : null,
          Array.isArray(milestone.tasks) && milestone.tasks.length
            ? el('ul', {}, milestone.tasks.map(task => el('li', {}, String(task?.title || task))))
            : null,
          milestone.deliverable ? el('p', { class: 'help' }, `交付物：${milestone.deliverable}`) : null,
          milestone.checkpoint ? el('p', { class: 'help' }, `完成标准：${milestone.checkpoint}`) : null)))
        : el('p', { class: 'help' }, '里程碑待生成。'),
      rubricNodes.length
        ? el('div', {},
          el('h4', {}, '评价标准'),
          el('ul', { class: 'cls-pbl-rubric' }, rubricNodes))
        : null,
      Array.isArray(project.mentor_tips || project.mentor_hints) && (project.mentor_tips || project.mentor_hints).length
        ? el('div', {},
          el('h4', {}, '导师提示'),
          el('ul', { class: 'cls-pbl-rubric' },
            (project.mentor_tips || project.mentor_hints).map(hint => el('li', {}, String(hint)))))
        : null));
    return wrap;
  }

  // ---------------------------------------------------------------------------
  // M4 演示舞台：固定版本学习附件（动态 import 受控组件，切场销毁）
  // ---------------------------------------------------------------------------

  /** 统一场景类型：后端同时给 `type` 与 `scene_type` 别名，旧课程只有 `type`。 */
  function sceneTypeOf(scene) {
    return String(scene?.scene_type || scene?.type || 'slide');
  }

  function sceneTypeLabel(scene) {
    const type = sceneTypeOf(scene);
    if (type === 'demo') return m4Text('study.m4.scene.demo', '原理演示');
    return typeLabel(type);
  }

  /** 从 demo 场景内容里取出展示与渲染所需的只读字段（全部做容错，不抛错）。 */
  function demoStageInfo(scene) {
    const content = scene && typeof scene.content === 'object' ? scene.content : {};
    const artifact = content.artifact && typeof content.artifact === 'object' ? content.artifact : {};
    const revisionRaw = Number(artifact.revision ?? content.fixed_revision);
    const revision = Number.isFinite(revisionRaw) && revisionRaw > 0 ? Math.round(revisionRaw) : 0;
    const kind = String(artifact.kind || content.kind || '');
    const assumptionsRaw = content.assumptions ?? artifact.assumptions;
    let assumptions = [];
    if (Array.isArray(assumptionsRaw)) {
      assumptions = assumptionsRaw.map(item => String(item ?? '').trim()).filter(Boolean);
    } else if (typeof assumptionsRaw === 'string' && assumptionsRaw.trim()) {
      assumptions = [assumptionsRaw.trim()];
    }
    const stepCountRaw = Number(content.step_count ?? artifact.step_count);
    // M5：机械 3D 附件没有显式 step_count 时，用 data.steps 的长度展示「共 N 步」。
    const mechStepCount = kind === 'mech_3d' && Array.isArray(artifact.data?.steps)
      ? artifact.data.steps.length
      : 0;
    return {
      kind,
      revision,
      title: String(artifact.title || scene?.title || ''),
      data: artifact.data,
      poster: demoPosterDataUrl(artifact.preview) || demoPosterDataUrl(content.preview),
      sourceNote: String(content.source_note || '').trim(),
      assumptions,
      stepCount: Number.isFinite(stepCountRaw) && stepCountRaw > 0
        ? Math.round(stepCountRaw)
        : mechStepCount,
    };
  }

  /** 销毁当前演示组件（幂等；同时让在途的动态 import 回调失效）。 */
  function destroyDemoStage() {
    demoToken += 1;
    const controller = demoController;
    demoController = null;
    if (controller && typeof controller.destroy === 'function') {
      try { controller.destroy(); } catch { /* 组件释放失败不影响切场 */ }
    }
  }

  /** 渲染完成：打 dataset 标记并广播 zhixu:demo-ready（供视频/导出等待）。 */
  function markDemoReady(stage, info, scene) {
    if (!stage || !stage.isConnected || stage.dataset.demoReady === '1') return;
    stage.dataset.demoReady = '1';
    stage.dataset.demoKind = info.kind || '';
    if (info.revision > 0) stage.dataset.demoRevision = String(info.revision);
    try {
      stage.dispatchEvent(new CustomEvent('zhixu:demo-ready', {
        bubbles: true,
        composed: true,
        detail: { sceneId: scene?.id ?? null, kind: info.kind || '', revision: info.revision || 0 },
      }));
    } catch { /* 极老浏览器不支持 CustomEvent 选项时忽略（不影响渲染） */ }
  }

  /** 演示加载/数据非法时的中文降级：保留海报入口，不伪造任何演示内容。 */
  function renderDemoFailure(stage, info, reason) {
    if (!stage) return;
    const body = stage.querySelector('.cls-demo-body');
    const poster = stage.querySelector('.cls-demo-poster');
    const detail = String(reason || '').trim();
    if (body) {
      body.replaceChildren(el('div', { class: 'cls-demo-fallback', role: 'status' },
        el('strong', {}, m4Text('study.m4.demoStage.failed', '演示加载失败，可查看海报')),
        detail
          ? el('p', { class: 'help' }, m4Text('study.m4.demoStage.failedReason', '原因：{reason}', { reason: detail }))
          : null));
    }
    if (poster) poster.hidden = !info.poster;
    try { delete stage.dataset.demoReady; } catch { /* 忽略 */ }
    stage.dataset.demoFailed = '1';
  }

  /** 按 kind 动态 import 受控组件并在 body 内挂载；任何失败都落入中文降级。 */
  async function mountDemoComponent(stage, info, scene) {
    const body = stage.querySelector('.cls-demo-body');
    if (!body) return;
    const token = ++demoToken;
    const stale = () => destroyed || token !== demoToken || !stage.isConnected;
    const host = el('div', { class: 'cls-demo-component' });
    const loadingNode = el('p', { class: 'cls-demo-loading' },
      el('span', { class: 'spinner' }), m4Text('study.m4.demoStage.loading', '正在加载演示…'));
    body.replaceChildren(host, loadingNode);
    try {
      const kind = info.kind;
      if (kind === 'function_graph' || kind === 'whiteboard') {
        const module = await import(kind === 'function_graph' ? './functionGraph.js' : './sketchBoard.js');
        if (stale()) return;
        let controller;
        if (kind === 'function_graph') {
          if (typeof module.normalizeFunctionGraphDefinition === 'function'
            && !module.normalizeFunctionGraphDefinition(info.data)) {
            throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
          }
          controller = module.createFunctionGraph({
            definition: info.data,
            readOnly: true,
            ariaLabel: info.title || demoKindLabel(kind),
          });
        } else {
          if (typeof module.normalizeSketchScene === 'function' && !module.normalizeSketchScene(info.data)) {
            throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
          }
          controller = module.createSketchBoard({
            scene: info.data,
            readOnly: true,
            ariaLabel: info.title || demoKindLabel(kind),
          });
        }
        if (!controller || !controller.element) {
          throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
        }
        if (stale()) {
          try { controller.destroy(); } catch { /* 忽略 */ }
          return;
        }
        demoController = controller;
        loadingNode.remove();
        host.replaceChildren(controller.element);
        markDemoReady(stage, info, scene);
        return;
      }
      // 批次五（§6.2）：3Dmol 分子模型只读渲染；WebGL 不可用时组件内中文降级。
      if (kind === 'molecule_3d') {
        const module = await import('./moleculeViewer.js');
        if (stale()) return;
        if (typeof module.normalizeMoleculeDefinition === 'function'
          && !module.normalizeMoleculeDefinition(info.data)) {
          throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
        }
        const view = module.createMoleculeView(host, info.data, { readOnly: true });
        if (!view || !view.element) {
          throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
        }
        if (stale()) {
          try { view.destroy(); } catch { /* 忽略 */ }
          return;
        }
        demoController = view;
        loadingNode.remove();
        host.replaceChildren(view.element);
        try {
          const outcome = await view.ready;
          if (!outcome || outcome.ok === false) throw new Error('molecule unavailable');
        } catch {
          throw new Error(m4Text('study.m4.demoStage.failed', '演示加载失败，可查看海报'));
        }
        if (stale()) return;
        markDemoReady(stage, info, scene);
        return;
      }

      // M5（方案 §5.4「机械与工厂」）：机械 3D / 工厂仿真复用固定版本附件数据，
      // 只读渲染；无 WebGL 或 three 加载失败由 mechDemo 内部降级为 SVG 示意图。
      if (kind === 'mech_3d' || kind === 'factory_sim') {
        const module = await import('./mechDemo.js');
        if (stale()) return;
        const demoData = info.data === undefined || info.data === null ? {} : info.data;
        const demoSeed = demoData && typeof demoData === 'object' ? demoData.seed : undefined;
        const viewOptions = {
          readOnly: true,
          // 就绪判断以组件帧回执为准：data-demo-ready 只在真正的首帧渲染完成后打标。
          onFrameReady: () => markDemoReady(stage, info, scene),
        };
        const demo = {
          kind,
          title: info.title || demoKindLabel(kind),
          data: demoData,
          seed: demoSeed,
        };
        const view = kind === 'mech_3d'
          ? module.createMechView(host, demo, viewOptions)
          : module.createFactoryView(host, demo, viewOptions);
        if (!view || !view.element) {
          throw new Error(m4Text('study.m4.demoStage.invalidData', '演示数据不合法'));
        }
        if (stale()) {
          try { view.destroy(); } catch { /* 忽略 */ }
          return;
        }
        demoController = view;
        loadingNode.remove();
        host.replaceChildren(view.element);
        try { await view.ready; } catch { /* ready 不应 reject；兼容异常实现 */ }
        if (stale()) return;
        // mech_3d 允许降级到 "svg"（组件内已中文提示）；factory_sim 固定为 "svg"。
        markDemoReady(stage, info, scene);
        return;
      }
      if (!PRINCIPLE_DEMO_KINDS.has(kind)) {
        throw new Error(m4Text('study.m4.demoStage.unknownKind', '未知演示类型') + (kind ? `：${kind}` : ''));
      }
      const module = await import('./principleDemo.js');
      if (stale()) return;
      // confirmed 在确认渲染器没有降级为静态海报后才置真：principleDemo 的失败分支会先
      // 同步发一次 onFrameReady，因此不能直接用它判定 ready。
      let confirmed = false;
      const controller = module.renderPrincipleDemo(host, {
        kind: demoPrincipleKind(kind, info.data),
        title: info.title || demoKindLabel(kind),
        data: info.data === undefined || info.data === null ? {} : info.data,
      }, {
        keyboard: true,
        onFrameReady: () => { if (confirmed) markDemoReady(stage, info, scene); },
      });
      if (stale()) {
        try { controller.destroy(); } catch { /* 忽略 */ }
        return;
      }
      demoController = controller;
      loadingNode.remove();
      try { await controller.ready; } catch { /* ready 不应 reject；兼容异常实现 */ }
      if (stale()) return;
      if (typeof controller.getMode === 'function' && controller.getMode() === 'poster') {
        try { controller.destroy(); } catch { /* 忽略 */ }
        if (demoController === controller) demoController = null;
        renderDemoFailure(stage, info, m4Text('study.m4.demoStage.renderFailed', '演示渲染失败'));
        return;
      }
      confirmed = true;
      markDemoReady(stage, info, scene);
    } catch (error) {
      if (stale()) return;
      if (demoController && typeof demoController.destroy === 'function') {
        try { demoController.destroy(); } catch { /* 忽略 */ }
      }
      demoController = null;
      renderDemoFailure(stage, info, error?.message || error);
    }
  }

  function renderDemoStage(scene) {
    const info = demoStageInfo(scene);
    const stage = el('div', {
      class: 'cls-demo-stage cls-demo-scene',
      'data-demo-kind': info.kind || 'unknown',
      'data-scene-id': String(scene?.id ?? ''),
      role: 'group',
      'aria-label': m4Text('study.m4.demoStage.aria', '课堂演示：{title}', {
        title: info.title || demoKindLabel(info.kind),
      }),
    });
    const badges = el('div', { class: 'cls-demo-badges' },
      el('span', { class: 'cls-demo-kind' }, demoKindLabel(info.kind)));
    if (info.revision > 0) {
      badges.append(el('span', { class: 'cls-demo-fixed' },
        m4Text('study.m4.demoStage.fixedVersion', '固定版本 v{version}', { version: info.revision })));
    }
    if (info.stepCount > 0) {
      badges.append(el('span', { class: 'cls-demo-steps' },
        m4Text('study.m4.demoStage.stepCount', '共 {count} 步', { count: info.stepCount })));
    }
    const head = el('div', { class: 'cls-demo-head' },
      el('div', { class: 'cls-demo-head-main' },
        el('h3', { class: 'cls-demo-title', 'data-no-i18n': 'true' },
          info.title || m4Text('study.m4.demoStage.untitled', '未命名演示')),
        badges),
      el('p', { class: 'help cls-demo-hint' },
        m4Text('study.m4.demoStage.hint', '按插入课堂时固定的附件版本渲染；附件后续升版不会改变这一页。')));
    const body = el('div', { class: 'cls-demo-body' });
    const poster = info.poster
      ? el('figure', { class: 'cls-demo-poster', hidden: true },
        el('img', {
          src: info.poster,
          alt: m4Text('study.m4.demoStage.posterAlt', '演示海报：{title}', { title: info.title || '' }),
          loading: 'lazy',
        }),
        el('figcaption', { class: 'help' },
          m4Text('study.m4.demoStage.posterCaption', '附件生成的海报预览（固定版本）')))
      : null;
    const actions = el('div', { class: 'cls-demo-actions' });
    if (poster) {
      const labelNode = el('span', {}, m4Text('study.m4.demoStage.viewPoster', '查看海报'));
      const toggle = el('button', {
        type: 'button',
        class: 'button secondary small',
        'aria-expanded': 'false',
        onClick: () => {
          poster.hidden = !poster.hidden;
          const shown = !poster.hidden;
          labelNode.textContent = shown
            ? m4Text('study.m4.demoStage.hidePoster', '收起海报')
            : m4Text('study.m4.demoStage.viewPoster', '查看海报');
          toggle.setAttribute('aria-expanded', shown ? 'true' : 'false');
        },
      }, icon('eye'), labelNode);
      actions.append(toggle);
    }
    const notes = el('div', { class: 'cls-demo-notes' });
    if (info.sourceNote) {
      const sourceNote = el('textarea', {
        class: 'cls-demo-note-input',
        rows: 2,
        readonly: true,
        value: info.sourceNote,
        'aria-label': m4Text('study.m4.demoStage.sourceNote', '出处与说明'),
      });
      notes.append(el('label', { class: 'cls-demo-note' },
        el('span', { class: 'cls-demo-note-label' },
          m4Text('study.m4.demoStage.sourceNote', '出处与说明')),
        sourceNote));
    }
    if (info.assumptions.length) {
      const assumptions = el('textarea', {
        class: 'cls-demo-note-input',
        rows: Math.min(6, Math.max(2, info.assumptions.length + 1)),
        readonly: true,
        value: info.assumptions.join('\n'),
        'aria-label': m4Text('study.m4.demoStage.assumptions', '假设与限制'),
      });
      notes.append(el('label', { class: 'cls-demo-note' },
        el('span', { class: 'cls-demo-note-label' },
          m4Text('study.m4.demoStage.assumptions', '假设与限制')),
        assumptions));
    }
    stage.append(head, body);
    if (poster) stage.append(poster);
    if (actions.childNodes.length) stage.append(actions);
    if (notes.childNodes.length) stage.append(notes);
    // 等场景真正挂到舞台后再 import 组件：markDemoReady 依赖 stage.isConnected。
    Promise.resolve().then(() => mountDemoComponent(stage, info, scene)).catch(() => { /* 已由降级 UI 覆盖 */ });
    return stage;
  }

  function renderSceneStage(scene) {
    const type = sceneTypeOf(scene);
    if (type === 'demo') return renderDemoStage(scene);
    if (type === 'slide') return renderSlideStage(scene);
    if (type === 'quiz') return renderQuizStage(scene);
    if (type === 'interactive') return renderInteractiveStage(scene);
    if (type === 'pbl') return renderPblStage(scene);
    return notice('未知场景类型：' + type, 'warning');
  }

  // ---------------------------------------------------------------------------
  // 播放效果
  // ---------------------------------------------------------------------------
  function effectBox(elementId) {
    const scene = currentScene();
    const content = scene?.content || {};
    const element = elementBoxList(content).get(String(elementId || ''));
    if (!element) return null;
    return {
      element,
      left: Number(element.left) || 0,
      top: Number(element.top) || 0,
      width: Number(element.width) || 10,
      height: Number(element.height) || 10,
    };
  }

  function clearEffects() {
    if (!currentCanvas) return;
    const spot = currentCanvas.querySelector('.cls-effect-spot');
    const highlight = currentCanvas.querySelector('.cls-effect-highlight');
    const laser = currentCanvas.querySelector('.cls-effect-laser');
    const whiteboard = currentCanvas.querySelector('.cls-whiteboard');
    if (spot) { spot.hidden = true; spot.style.cssText = ''; }
    if (highlight) { highlight.hidden = true; highlight.style.cssText = ''; }
    if (laser) { laser.hidden = true; laser.style.cssText = ''; }
    if (whiteboard) whiteboard.replaceChildren();
  }

  function applySpotlight(elementId, dimOpacity = 0.55) {
    const box = effectBox(elementId);
    const node = currentCanvas?.querySelector('.cls-effect-spot');
    if (!box || !node) return false;
    node.hidden = false;
    node.style.cssText = `left:${box.left}px;top:${box.top}px;width:${box.width}px;height:${box.height}px;`
      + `box-shadow:0 0 0 2400px rgba(6,18,24,${clamp(dimOpacity, 0.2, 0.85)});`;
    return true;
  }

  function applyHighlight(elementId) {
    const box = effectBox(elementId);
    const node = currentCanvas?.querySelector('.cls-effect-highlight');
    if (!box || !node) return false;
    node.hidden = false;
    node.style.cssText = `left:${box.left - 6}px;top:${box.top - 6}px;width:${box.width + 12}px;height:${box.height + 12}px;`;
    return true;
  }

  function applyLaser(elementId) {
    const box = effectBox(elementId);
    const node = currentCanvas?.querySelector('.cls-effect-laser');
    if (!box || !node) return false;
    node.hidden = false;
    node.style.cssText = `left:${box.left + box.width / 2 - 9}px;top:${box.top + box.height / 2 - 9}px;`;
    return true;
  }

  /**
   * 白板渲染：兼容课程生成管线的像素坐标（1000×562.5）与多智能体讨论脚本的
   * 0-1 归一化坐标（`action.coord_space === 'unit'`）；支持 text/formula/line/rect/circle/arrow/path。
   */
  function applyWhiteboard(ops, unit = false) {
    const layer = currentCanvas?.querySelector('.cls-whiteboard');
    if (!layer) return false;
    const W = SLIDE_WIDTH;
    const H = SLIDE_HEIGHT;
    const xOf = value => (unit ? (Number(value) || 0) * W : (Number(value) || 0));
    const yOf = value => (unit ? (Number(value) || 0) * H : (Number(value) || 0));
    const wOf = (value, fallback) => (unit ? (Number(value) || fallback) * W : (Number(value) || fallback));
    const hOf = (value, fallback) => (unit ? (Number(value) || fallback) * H : (Number(value) || fallback));
    const stroke = (op, fallback = '#333333') => op.stroke || op.color || fallback;
    const fill = (op, fallback = 'none') => {
      const color = op.fill;
      if (!color || color === 'transparent') return fallback;
      return color;
    };
    const shapeSvg = () => makeSvg('svg', {
      class: 'cls-wb-svg', viewBox: `0 0 ${W} ${H}`, width: W, height: H,
    });

    for (const op of Array.isArray(ops) ? ops : []) {
      const kind = String(op.kind || '');
      if (kind === 'text') {
        const node = el('span', {
          class: 'cls-wb-text',
          style: `left:${xOf(op.x)}px;top:${yOf(op.y)}px;font-size:${Number(op.font_size) || 18}px;color:${op.color || '#333333'}`,
        });
        node.textContent = String(op.content || '');
        layer.append(node);
      } else if (kind === 'formula') {
        const node = el('div', {
          class: 'cls-wb-formula',
          style: `left:${xOf(op.x)}px;top:${yOf(op.y)}px;width:${wOf(op.width, 300)}px`,
        });
        node.append(latexNode(op.latex || '', { display: false }));
        layer.append(node);
      } else if (kind === 'line' || kind === 'arrow') {
        const svg = shapeSvg();
        const x1 = xOf(op.x1);
        const y1 = yOf(op.y1);
        const x2 = xOf(op.x2);
        const y2 = yOf(op.y2);
        const color = stroke(op);
        const width = Math.max(1, Number(op.width) || 2);
        svg.append(makeSvg('line', {
          x1, y1, x2, y2, stroke: color, 'stroke-width': width, 'stroke-linecap': 'round',
        }));
        if (kind === 'arrow') {
          const angle = Math.atan2(y2 - y1, x2 - x1);
          const size = Math.max(8, width * 4);
          svg.append(makeSvg('path', {
            d: `M ${x2} ${y2} l ${size * Math.cos(angle + 2.6)} ${size * Math.sin(angle + 2.6)} `
              + `L ${x2 + size * 0.6 * Math.cos(angle)} ${y2 + size * 0.6 * Math.sin(angle)} `
              + `L ${size * Math.cos(angle - 2.6) + x2} ${size * Math.sin(angle - 2.6) + y2} Z`,
            fill: color,
          }));
        }
        layer.append(svg);
      } else if (kind === 'rect') {
        const svg = shapeSvg();
        svg.append(makeSvg('rect', {
          x: xOf(op.x), y: yOf(op.y),
          width: Math.max(4, wOf(op.width, 200)), height: Math.max(4, hOf(op.height, 120)),
          fill: fill(op), stroke: stroke(op, unit ? '#5b9bd5' : 'none'),
          'stroke-width': Math.max(1, Number(op.stroke_width) || 2),
          rx: 3,
        }));
        layer.append(svg);
      } else if (kind === 'circle') {
        const svg = shapeSvg();
        const width = Math.max(4, wOf(op.width, 160));
        const height = Math.max(4, hOf(op.height, 160));
        svg.append(makeSvg('ellipse', {
          cx: xOf(op.x) + width / 2, cy: yOf(op.y) + height / 2,
          rx: width / 2, ry: height / 2,
          fill: fill(op), stroke: stroke(op, unit ? '#5b9bd5' : 'none'),
          'stroke-width': Math.max(1, Number(op.stroke_width) || 2),
        }));
        layer.append(svg);
      } else if (kind === 'path') {
        const viewBox = Array.isArray(op.view_box) && op.view_box.length === 2 ? op.view_box : [1, 1];
        const vbWidth = Math.max(0.0001, Number(viewBox[0]) || 1);
        const vbHeight = Math.max(0.0001, Number(viewBox[1]) || 1);
        const width = Math.max(6, wOf(op.width, 200));
        const height = Math.max(6, hOf(op.height, 160));
        const svg = el('div', {
          class: 'cls-wb-path',
          style: `left:${xOf(op.x)}px;top:${yOf(op.y)}px;width:${width}px;height:${height}px`,
        });
        const inner = makeSvg('svg', { viewBox: `0 0 ${vbWidth} ${vbHeight}`, preserveAspectRatio: 'none', width: '100%', height: '100%' });
        inner.append(makeSvg('path', {
          d: String(op.d || ''),
          fill: fill(op),
          stroke: stroke(op),
          'stroke-width': Math.max(0.5, Number(op.stroke_width) || 2),
          'vector-effect': 'non-scaling-stroke',
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
        }));
        svg.append(inner);
        layer.append(svg);
      }
    }
    return true;
  }

  // ---------------------------------------------------------------------------
  // 动作执行
  // ---------------------------------------------------------------------------
  function warnUnknownAction(type) {
    if (unknownActions.includes(type) || unknownActions.length >= UNKNOWN_ACTION_LIMIT) return;
    unknownActions.push(type);
    toast(`已忽略未知的播放动作：${type}`, true);
  }

  function applyActionVisual(action) {
    switch (action.type) {
      case 'spotlight':
        return applySpotlight(action.element_id, action.dim_opacity);
      case 'laser':
        return applyLaser(action.element_id);
      case 'highlight':
        return applyHighlight(action.element_id);
      case 'whiteboard':
        applyWhiteboard(action.ops, action.coord_space === 'unit');
        return true;
      default:
        return false;
    }
  }

  async function playAudioAction(action) {
    let url = '';
    if (action.asset_id) url = `/api/classroom/assets/${action.asset_id}`;
    else if (action.url) url = action.url;
    if (!url) return Promise.resolve();
    return new Promise(resolve => {
      const audio = new Audio(url);
      let timer = null;
      const finish = () => {
        if (timer) clearTimeout(timer);
        if (narrationAudio === audio) narrationAudio = null;
        if (narrationAudioFinish === finish) narrationAudioFinish = null;
        resolve();
      };
      narrationAudio = audio;
      narrationAudioFinish = finish;
      audio.addEventListener('ended', finish);
      audio.addEventListener('error', finish);
      timer = setTimeout(finish, 120000);
      audio.play().catch(finish);
    });
  }

  function targetSceneIndex(action) {
    if (Number.isInteger(action.index) && scenes[action.index]) return action.index;
    if (action.scene_id) {
      const index = scenes.findIndex(scene => String(scene.id) === String(action.scene_id));
      if (index >= 0) return index;
    }
    return -1;
  }

  async function executeAction(action, token) {
    if (!action || typeof action !== 'object') return true;
    switch (action.type) {
      case 'speech':
        await speakSceneText(action.text, token);
        return true;
      case 'spotlight':
      case 'laser':
      case 'highlight':
      case 'whiteboard':
        applyActionVisual(action);
        await sleep(700);
        return true;
      case 'play_audio':
        await playAudioAction(action);
        return true;
      case 'next_scene':
        switchToScene(sceneIndex + 1, { keepPlaying: true });
        return false;
      case 'show_slide': {
        const index = targetSceneIndex(action);
        if (index >= 0) switchToScene(index, { keepPlaying: true });
        else warnUnknownAction('show_slide');
        return false;
      }
      default:
        warnUnknownAction(String(action.type || 'unknown'));
        return true;
    }
  }

  async function runTimeline() {
    const token = ++runToken;
    while (playing && token === runToken && !destroyed) {
      const scene = currentScene();
      if (!scene) break;
      const actions = currentActions();
      if (actionIndex >= actions.length) {
        await sleep(900);
        if (token !== runToken || destroyed) return;
        if (sceneIndex < scenes.length - 1) {
          const nextIndex = sceneIndex + 1;
          renderScene(nextIndex, { resumeAction: 0 });
          actionIndex = 0;
          saveNow();
          // 自动进入下一页继续播报；测验场景停下等学生作答，避免跳过练习。
          if (scenes[nextIndex]?.type === 'quiz') return;
          startPlaying();
          return;
        }
        playing = false;
        updateControls();
        showEndCard();
        break;
      }
      const action = actions[actionIndex];
      updateProgress();
      const keepGoing = await executeAction(action, token);
      if (token !== runToken || destroyed) return;
      if (!keepGoing) return;
      actionIndex += 1;
      scheduleSave();
      updateProgress();
    }
  }

  function startPlaying() {
    const scene = currentScene();
    if (!scene || destroyed) return;
    if (scene.type === 'quiz') {
      toast('测验场景请直接作答；可用测验面板上的「播放开场旁白」听引导。');
      return;
    }
    if (playing) return;
    playing = true;
    endCardShown = false;
    removeEndCard();
    updateControls();
    runTimeline().catch(() => {
      playing = false;
      updateControls();
    });
  }

  function pausePlaying() {
    playing = false;
    runToken += 1;
    cancelSpeech();
    updateControls();
    scheduleSave(true);
  }

  function togglePlay() {
    if (playing) pausePlaying();
    else startPlaying();
  }

  function switchToScene(index, { keepPlaying = false } = {}) {
    if (index < 0 || index >= scenes.length) {
      if (index >= scenes.length) {
        playing = false;
        updateControls();
        showEndCard();
      }
      return;
    }
    renderScene(index, { resumeAction: 0 });
    if (keepPlaying) startPlaying();
  }

  function stepAction(delta) {
    const actions = currentActions();
    if (!actions.length) return;
    runToken += 1;
    playing = false;
    cancelSpeech();
    actionIndex = clamp(actionIndex + delta, 0, actions.length - 1);
    const action = actions[actionIndex];
    if (action) {
      const previewToken = runToken;
      if (action.type === 'speech') {
        captionNode.textContent = action.text || '';
        speak(action.text, previewToken);
      } else if (action.type === 'next_scene' || action.type === 'show_slide') {
        executeAction(action, previewToken);
      } else {
        applyActionVisual(action);
      }
    }
    updateControls();
    updateProgress();
    scheduleSave();
  }

  // ---------------------------------------------------------------------------
  // 场景切换与 DOM
  // ---------------------------------------------------------------------------
  function removeEndCard() {
    root.querySelector('.cls-end-card')?.remove();
    endCardShown = false;
  }

  function showEndCard() {
    if (endCardShown || destroyed) return;
    endCardShown = true;
    cancelSpeech();
    const card = el('div', { class: 'cls-end-card' },
      el('div', { class: 'cls-end-card-inner' },
        el('h2', {}, '本课已播放完毕'),
        el('p', { class: 'help' }, '可以重新播放、回到第一页，或打开 AI 老师继续讨论本节课内容。'),
        el('div', { class: 'row' },
          button('重新播放', () => { removeEndCard(); switchToScene(0, { keepPlaying: true }); }, '', 'repeat'),
          button('打开 AI 老师', () => openTutor(), 'secondary', 'message'),
          button('返回课程列表', () => go('classroom'), 'secondary small', 'chevron'))));
    root.append(card);
  }

  function renderScene(index, { resumeAction = 0 } = {}) {
    if (destroyed || !scenes.length) return;
    stopPlayback();
    destroyDemoStage();
    narrationUsedScene = null;
    endCardShown = false;
    sceneIndex = clamp(index, 0, scenes.length - 1);
    actionIndex = clamp(resumeAction, 0, Math.max(0, currentActions().length - 1));
    clearFit();
    clearEffects();
    captionNode.textContent = '';
    const scene = currentScene();
    let stage;
    try {
      stage = renderSceneStage(scene);
    } catch (error) {
      stage = notice(`场景渲染失败：${error.message || error}`, 'error');
    }
    stageHost.replaceChildren(stage);
    updateHeaderMeta();
    updateSceneStrip();
    updateControls();
    updateProgress();
    scheduleSave(true);
  }

  function stopPlayback() {
    playing = false;
    runToken += 1;
    cancelSpeech();
    updateControls();
  }

  function updateHeaderMeta() {
    const scene = currentScene();
    const meta = root.querySelector('.cls-player-meta');
    if (!meta || !scene) return;
    const status = statusBadgeParts(scene.status, SCENE_STATUS);
    meta.replaceChildren(...[
      el('span', {}, `第 ${sceneIndex + 1}/${scenes.length} 页`),
      el('span', { class: 'cls-dot' }, '·'),
      el('span', {}, scene.title || '未命名场景'),
      el('span', { class: 'cls-dot' }, '·'),
      el('span', {}, sceneTypeLabel(scene)),
      scene.status && scene.status !== 'ready' ? badge(status.label, status.variant) : null,
      scene.meta?.placeholder ? badge('内容待生成', 'amber') : null,
    ].filter(Boolean));
  }

  function updateSceneStrip() {
    if (!stripNode) return;
    const buttons = stripNode.querySelectorAll('.cls-scene-chip');
    buttons.forEach((node, index) => {
      node.classList.toggle('active', index === sceneIndex);
      node.setAttribute('aria-current', index === sceneIndex ? 'true' : 'false');
    });
    const active = buttons[sceneIndex];
    if (active && typeof active.scrollIntoView === 'function') {
      active.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
    }
  }

  function updateProgress() {
    if (!controlRefs) return;
    const actions = currentActions();
    const scenePart = scenes.length ? sceneIndex / scenes.length : 0;
    const actionPart = scenes.length && actions.length ? (actionIndex / actions.length) / scenes.length : 0;
    const percent = Math.round(clamp((scenePart + actionPart) * 100, 0, 100));
    controlRefs.progressFill.style.width = `${percent}%`;
    controlRefs.progressLabel.textContent = `${percent}%`;
    controlRefs.actionLabel.textContent = actions.length
      ? `${Math.min(actionIndex + 1, actions.length)}/${actions.length} 个动作`
      : '本页没有播放动作';
  }

  function updateControls() {
    if (!controlRefs) return;
    const scene = currentScene();
    const quizVisible = Boolean(stageHost && stageHost.querySelector('.cls-quiz-panel'));
    const playable = scene && scene.type !== 'quiz';
    controlRefs.playBtn.disabled = !playable;
    controlRefs.playBtn.setAttribute('aria-label', playing ? '暂停' : '播放');
    controlRefs.playBtn.replaceChildren(el('span', {}, playing ? '⏸' : '▶'), el('span', {}, playing ? '暂停' : '播放'));
    controlRefs.prevBtn.disabled = sceneIndex <= 0;
    controlRefs.nextBtn.disabled = sceneIndex >= scenes.length - 1;
    controlRefs.prevAction.disabled = !currentActions().length;
    controlRefs.nextAction.disabled = !currentActions().length;
    controlRefs.muteBtn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    controlRefs.muteBtn.textContent = muted ? '🔇 已静音' : '🔊 语音开启';
    controlRefs.playRow.classList.toggle('hide', quizVisible);
  }

  // ---------------------------------------------------------------------------
  // P4 媒体：批量旁白（音色选择 / 进度卡 / 已合成列表）+ AI 配图（参考图编辑）
  // ---------------------------------------------------------------------------
  function mediaDialog(extraClass) {
    const dialog = el('dialog', { class: `cls-media-dialog ${extraClass || ''}`.trim() });
    const close = () => { try { dialog.close(); } catch { /* 忽略 */ } };
    const objectUrls = new Set();
    dialog.addEventListener('close', () => {
      for (const url of objectUrls) {
        try { URL.revokeObjectURL(url); } catch { /* 忽略 */ }
      }
      objectUrls.clear();
      mediaDialogs.delete(dialog);
      dialog.remove();
    });
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
    mediaDialogs.add(dialog);
    document.body.append(dialog);
    return { dialog, close, trackUrl: url => { if (url) objectUrls.add(url); } };
  }

  function voiceOptionLabel(item) {
    const provider = item.provider ? `${item.provider} · ` : '';
    const model = item.model ? `（${item.model}）` : '';
    return `${provider}${item.name || item.voice_id || ''}${model}`;
  }

  async function loadVoiceCatalog({ force = false } = {}) {
    if (voiceCatalog && !force) return voiceCatalog;
    try {
      const data = await api('/classroom/voices');
      voiceCatalog = data && typeof data === 'object' ? data : { items: [] };
    } catch (error) {
      voiceCatalog = { items: [], providers: [], warnings: [error.message || '音色列表读取失败'] };
    }
    return voiceCatalog;
  }

  async function loadNarrationItems() {
    try {
      const listing = await api(`/classroom/courses/${courseId}/narration`);
      narrationItems = Array.isArray(listing.items) ? listing.items : [];
      applyNarrationItems(narrationItems);
    } catch {
      narrationItems = [];
    }
    return narrationItems;
  }

  /** 旁白查询结果并入本地课程配置，让「播放优先使用已合成旁白」立即生效。 */
  function applyNarrationItems(items) {
    if (!course) return;
    const map = { ...((course.config && course.config.narration) || {}) };
    for (const item of Array.isArray(items) ? items : []) {
      if (!item || !item.scene_id || !item.asset_id) continue;
      map[String(item.scene_id)] = {
        asset_id: item.asset_id,
        url: item.url || '',
        voice: item.voice || '',
        chars: item.chars || 0,
      };
    }
    course = { ...course, config: { ...(course.config || {}), narration: map } };
  }

  function playNarrationItem(item) {
    const url = String(item && item.url || '');
    if (!url) { toast('这条旁白没有可用音频，请重新合成', true); return; }
    cancelSpeech();
    playAudioAction({ url }).catch(() => { /* 音频播放失败只提示 */ toast('旁白音频播放失败，请重新合成', true); });
  }

  async function openNarrationPanel() {
    const { dialog, close } = mediaDialog('cls-narration-dialog');
    const voiceSelect = el('select', { class: 'cls-voice-select', 'aria-label': '旁白音色' });
    const voiceHint = el('p', { class: 'help cls-voice-source' }, '正在读取音色列表…');
    const guideHost = el('div', { class: 'cls-narration-guide' });
    const progressHost = el('div', { class: 'cls-narration-progress', hidden: true });
    const listHost = el('div', { class: 'cls-narration-list' }, loading('正在读取已合成旁白…'));
    const countLabel = el('span', { class: 'help cls-narration-count' });
    const startLabel = el('span', {}, '开始批量合成旁白');
    const startBtn = el('button', {
      type: 'button', class: 'button cls-narration-start', disabled: true,
    }, startLabel);

    function readSelectedVoice() {
      const raw = voiceSelect.value;
      if (!raw) return null;
      try {
        const parsed = JSON.parse(raw);
        return parsed && parsed.voice_id ? parsed : null;
      } catch {
        return null;
      }
    }

    function renderVoiceOptions(catalog) {
      const items = Array.isArray(catalog.items) ? catalog.items : [];
      voiceSelect.replaceChildren();
      if (!items.length) {
        voiceSelect.append(el('option', { value: '' }, '没有可用音色'));
        voiceSelect.disabled = true;
        startBtn.disabled = true;
        startLabel.textContent = '没有可用的语音连接';
        guideHost.replaceChildren(notice(
          (catalog.warnings && catalog.warnings[0]) || '还没有可用的语音合成连接：请在「模型设置」添加支持 TTS 的连接后再合成旁白。',
          'warning',
        ), el('div', { class: 'row' },
          button('去模型设置', () => { close(); go('settings'); }, 'secondary small', 'sliders'),
          button('继续使用浏览器朗读', close, 'ghost small')));
        return;
      }
      const groups = new Map();
      for (const item of items) {
        const key = `${item.provider_id}-${item.model}`;
        if (!groups.has(key)) groups.set(key, {
          label: [item.provider, item.model].filter(Boolean).join(' · '),
          items: [],
        });
        groups.get(key).items.push(item);
      }
      const preferred = String((course.config && course.config.voice) || '');
      let selected = '';
      for (const [key, group] of groups) {
        const optgroup = el('optgroup', { label: group.label || key });
        for (const item of group.items) {
          const value = JSON.stringify({
            provider_id: item.provider_id, model: item.model, voice_id: item.voice_id,
          });
          optgroup.append(el('option', { value }, voiceOptionLabel(item)));
          if (item.voice_id === preferred) selected = value;
          else if (!selected && item.default) selected = value;
        }
        voiceSelect.append(optgroup);
      }
      if (!selected) {
        const firstOption = voiceSelect.querySelector('option');
        selected = firstOption ? firstOption.value : '';
      }
      voiceSelect.value = selected;
      voiceSelect.disabled = false;
      startBtn.disabled = false;
      startLabel.textContent = '开始批量合成旁白';
      guideHost.replaceChildren();
      const providers = Array.isArray(catalog.providers) ? catalog.providers : [];
      const sourceText = providers.map(item => `${item.provider || item.model}（${item.source === 'api' ? '接口音色' : '内置音色'}）`).join('、');
      voiceHint.textContent = `共 ${items.length} 个音色${sourceText ? `，来自 ${sourceText}` : ''}；MiniMax 音色列表缓存 10 分钟，失败时回退内置音色。`;
      if (catalog.warnings && catalog.warnings.length) {
        guideHost.replaceChildren(notice(catalog.warnings.join('；'), 'warning'));
      }
    }

    function renderNarrationList() {
      const sceneCount = scenes.length;
      countLabel.textContent = `（${narrationItems.length}/${sceneCount} 个场景）`;
      if (!narrationItems.length) {
        listHost.replaceChildren(el('p', { class: 'empty cls-empty-inline' },
          '还没有合成旁白：点击「开始批量合成旁白」为每个场景生成语音；没有旁白的场景仍会使用浏览器朗读。'));
        return;
      }
      const rows = narrationItems.map((item, index) => {
        const playBtn = button('播放', () => playNarrationItem(item), 'secondary small', 'play');
        if (item.missing_asset || !item.url) {
          playBtn.disabled = true;
          playBtn.title = '旁白素材缺失，请重新合成';
        }
        return el('div', { class: 'cls-narration-item' },
          el('span', { class: 'cls-narration-item-index' }, String(index + 1)),
          el('div', { class: 'cls-narration-item-main' },
            el('strong', {}, item.scene_title || `场景 ${item.scene_id}`),
            el('span', { class: 'help' }, [
              item.voice ? `音色 ${item.voice}` : null,
              item.chars ? `${item.chars} 字` : null,
              item.missing_asset ? '素材缺失，请重新合成' : '已合成，播放优先使用',
            ].filter(Boolean).join(' · '))),
          playBtn);
      });
      listHost.replaceChildren(...rows);
    }

    startBtn.addEventListener('click', async () => {
      const selected = readSelectedVoice();
      if (!selected) {
        toast('请先选择旁白音色（或在模型设置添加语音连接）', true);
        return;
      }
      startBtn.disabled = true;
      startLabel.textContent = '正在创建旁白任务…';
      try {
        const response = await api(`/classroom/courses/${courseId}/narration`, {
          method: 'POST',
          data: {
            provider_id: selected.provider_id,
            model: selected.model,
            voice: selected.voice_id,
          },
        });
        toast('已开始批量合成旁白：可在下方任务卡查看进度，完成后逐场景播放。');
        startLabel.textContent = '旁白合成中…（进度见下方）';
        progressHost.hidden = false;
        const container = el('div', { class: 'cls-narration-job-host' });
        progressHost.replaceChildren(container);
        const finalJob = await mountJob(container, response.job.id, null, {
          task: 'course-narration',
          titleHint: '课堂批量旁白',
        });
        await loadNarrationItems();
        renderNarrationList();
        voiceHint.textContent = `当前课程音色：${(course.config && course.config.voice) || selected.voice_id}`;
        if (finalJob && finalJob.status === 'completed') {
          toast('旁白合成完成：播放器将优先使用已合成音频。');
        } else if (finalJob && finalJob.status && finalJob.status !== 'completed') {
          toast(`旁白任务未完成（${finalJob.error || finalJob.status}），已合成的场景会保留。`, true);
        }
      } catch (error) {
        toast(error.message || '旁白任务创建失败，请重试', true);
      } finally {
        if (startBtn.isConnected) {
          startBtn.disabled = false;
          startLabel.textContent = '开始批量合成旁白';
        }
      }
    });

    dialog.append(
      el('div', { class: 'cls-media-dialog-head' },
        el('h3', {}, '课堂旁白（AI 语音合成）'),
        button('关闭', close, 'ghost small', 'close', { 'aria-label': '关闭旁白面板' })),
      el('p', { class: 'help' },
        '把每个场景的 speech 动作合成为一整段旁白音频并保存为课堂素材：播放时优先使用该音频，没有旁白的场景回退浏览器朗读；已合成且文本/音色未变的场景再次运行会直接复用，不会重复调用语音服务。'),
      el('div', { class: 'cls-narration-controls' },
        el('label', { class: 'cls-voice-field' }, el('span', {}, '旁白音色'), voiceSelect),
        startBtn),
      voiceHint,
      guideHost,
      progressHost,
      el('div', { class: 'cls-narration-list-head' },
        el('h4', {}, '已合成旁白'), countLabel),
      listHost);
    dialog.showModal();
    const catalog = await loadVoiceCatalog();
    await loadNarrationItems();
    renderVoiceOptions(catalog);
    renderNarrationList();
  }

  async function loadImageCapabilities({ force = false } = {}) {
    if (imageCapabilities && !force) return imageCapabilities;
    try {
      const meta = await api('/classroom/meta');
      imageCapabilities = (meta && meta.images) || { enabled: false };
    } catch {
      imageCapabilities = { enabled: false };
    }
    return imageCapabilities;
  }

  function imageHistoryEntries() {
    const config = (course && course.config) || {};
    return Array.isArray(config.generated_images) ? config.generated_images : [];
  }

  function rememberGeneratedImage(asset, prompt, edit) {
    if (!course || !asset || !asset.id) return;
    const entry = {
      asset_id: asset.id,
      url: asset.url || '',
      mime_type: asset.mime_type || '',
      prompt: String(prompt || '').slice(0, 600),
      size: Number((asset.meta && asset.meta.size) || 0),
      edit: Boolean(edit),
      scene_id: currentScene() ? currentScene().id : null,
      created_at: new Date().toISOString(),
    };
    const images = imageHistoryEntries().filter(item => Number(item.asset_id) !== Number(asset.id));
    images.unshift(entry);
    course = { ...course, config: { ...(course.config || {}), generated_images: images.slice(0, 20) } };
  }

  async function openImageDialog() {
    const { dialog, close, trackUrl } = mediaDialog('cls-image-dialog');
    const caps = await loadImageCapabilities();
    const referenceCaps = (caps && caps.reference) || {};
    const maxBytes = Number(referenceCaps.max_bytes) || 8 * 1024 * 1024;
    const maxSide = Number(referenceCaps.max_side) || 1600;
    const jpegQuality = Number(referenceCaps.jpeg_quality) || 85;
    const available = Boolean(caps && caps.enabled);
    const scene = currentScene();
    const promptInput = el('textarea', {
      class: 'cls-image-prompt', rows: 3, maxlength: '600',
      placeholder: '例如：把讲台换成水彩风格的教学插图，背景干净、适合初中生…',
      'aria-label': '配图描述（提示词）',
    });
    if (scene) promptInput.value = `为「${scene.title || '本页'}」生成一张教学配图`;
    const fileInput = el('input', {
      type: 'file', accept: 'image/png,image/jpeg,image/webp',
      class: 'cls-image-file', 'aria-label': '参考图（可选）',
      id: 'cls-image-file',
    });
    const preview = el('div', { class: 'cls-image-reference-preview', hidden: true });
    const status = el('p', { class: 'cls-image-status', role: 'status', 'aria-live': 'polite' });
    const resultHost = el('div', { class: 'cls-image-result' });
    const historyHost = el('div', { class: 'cls-image-history' });
    const historyCount = el('span', { class: 'help' });
    let referenceFile = null;
    const generateBtn = el('button', {
      type: 'button', class: 'button cls-image-generate', disabled: !available,
    }, '生成配图');

    function setStatus(text, tone = '') {
      status.textContent = text || '';
      status.hidden = !text;
      status.classList.toggle('is-error', tone === 'error');
    }

    function renderHistory() {
      const entries = imageHistoryEntries();
      historyCount.textContent = entries.length ? `（${entries.length} 张）` : '';
      if (!entries.length) {
        historyHost.replaceChildren(el('p', { class: 'help' }, '本课还没有生成过配图。'));
        return;
      }
      historyHost.replaceChildren(...entries.map(entry => el('figure', {
        class: 'cls-image-history-item', title: entry.prompt || 'AI 配图',
      },
      el('img', { src: entry.url, alt: entry.prompt || 'AI 生成配图', loading: 'lazy' }),
      el('figcaption', { class: 'help' }, entry.edit ? '图片编辑' : '文生图'))));
    }

    function renderResult(response) {
      const asset = response.asset || {};
      const image = el('img', {
        class: 'cls-image-result-img', src: asset.url || '', alt: 'AI 生成配图', loading: 'lazy',
      });
      const warnings = Array.isArray(response.warnings) ? response.warnings.filter(Boolean) : [];
      resultHost.replaceChildren(
        image,
        el('div', { class: 'cls-image-result-meta' },
          el('strong', {}, response.edit ? '图片编辑结果（已参考上传图片）' : '文生图结果'),
          el('p', { class: 'help' }, [
            response.provider || '', response.model || '',
            response.size ? `${(Number(response.size) / 1024).toFixed(1)}KB` : '',
          ].filter(Boolean).join(' · ')),
          warnings.length ? notice(warnings.join('；'), 'warning') : null,
          el('p', { class: 'help' }, '已保存到课程素材：可在「编辑幻灯片」中把它插入本页（导出时内联，无外链）。')));
    }

    fileInput.addEventListener('change', () => {
      const file = fileInput.files && fileInput.files[0];
      referenceFile = null;
      preview.hidden = true;
      preview.replaceChildren();
      if (!file) return;
      if (!/^image\//i.test(file.type || '')) {
        setStatus('参考图只支持 JPG / PNG / WEBP 图片', 'error');
        return;
      }
      if (file.size > maxBytes) {
        setStatus(`参考图不能超过 ${Math.round(maxBytes / 1024 / 1024)}MB，请换一张更小的图片`, 'error');
        return;
      }
      referenceFile = file;
      setStatus('');
      const objectUrl = URL.createObjectURL(file);
      trackUrl(objectUrl);
      preview.hidden = false;
      preview.replaceChildren(
        el('img', { src: objectUrl, alt: '参考图预览' }),
        el('span', { class: 'help' },
          `${file.name || '参考图'} · ${(file.size / 1024 / 1024).toFixed(2)}MB；上传后服务端自动压缩`
          + `（最长边 ${maxSide}、JPEG ${jpegQuality}），失败会给出明确提示。`));
    });

    async function generate() {
      const prompt = String(promptInput.value || '').trim();
      if (!prompt) {
        setStatus('请先填写配图描述（提示词）', 'error');
        promptInput.focus();
        return;
      }
      generateBtn.disabled = true;
      generateBtn.textContent = '正在生成配图…';
      setStatus('正在请求图片模型，通常需要十几秒…');
      const form = new FormData();
      form.append('prompt', prompt);
      if (scene && scene.id) form.append('scene_id', String(scene.id));
      if (referenceFile) form.append('reference', referenceFile, referenceFile.name || 'reference.png');
      try {
        const response = await api(`/classroom/courses/${courseId}/images`, {
          method: 'POST', body: form,
        });
        renderResult(response);
        rememberGeneratedImage(response.asset, response.prompt || prompt, response.edit);
        renderHistory();
        setStatus(response.edit
          ? '已参考上传图片生成配图并保存到课程素材。'
          : '已生成配图并保存到课程素材。');
      } catch (error) {
        setStatus(error.message || '配图生成失败，请重试。', 'error');
      } finally {
        generateBtn.disabled = !available;
        generateBtn.textContent = '生成配图';
      }
    }

    generateBtn.addEventListener('click', generate);

    dialog.append(
      el('div', { class: 'cls-media-dialog-head' },
        el('h3', {}, 'AI 配图（可上传参考图做图片编辑）'),
        button('关闭', close, 'ghost small', 'close', { 'aria-label': '关闭配图弹层' })),
      available
        ? el('p', { class: 'help' },
          `当前配图连接：${caps.provider || caps.model || '已配置'}（${caps.model || ''}）。上传参考图后会先尝试图片编辑，`
          + '编辑接口不可用时回退为文生图并明确提示，不会伪造编辑成功。')
        : notice('当前账号没有可用的图片生成连接：请在「模型设置」添加支持图片生成的连接'
          + '（OpenAI 兼容 /images/generations 或 MiniMax image-01）后再生成配图；'
          + '也可以先在生成向导里开启「AI 配图」。', 'warning'),
      el('div', { class: 'cls-image-edit-form' },
        el('label', { class: 'cls-image-field full' },
          el('span', {}, '配图描述（必填）'), promptInput),
        (caps.supports_reference === false ? null : el('div', { class: 'cls-image-field full' },
          el('label', { class: 'cls-image-file-label' },
            el('span', {}, '参考图（可选）：上传后按参考图做图片编辑'), fileInput),
          preview)),
        el('div', { class: 'row cls-image-actions' },
          generateBtn,
          available ? null : button('去模型设置', () => { close(); go('settings'); }, 'secondary small', 'sliders')),
        status),
      resultHost,
      el('div', { class: 'cls-narration-list-head' },
        el('h4', {}, '本课已生成配图'), historyCount),
      historyHost);
    dialog.showModal();
    renderHistory();
    setStatus('');
    try { promptInput.focus(); } catch { /* 忽略 */ }
  }

  // ---------------------------------------------------------------------------
  // 骨架
  // ---------------------------------------------------------------------------
  function buildSkeleton() {
    stageHost = el('div', { class: 'cls-stage-host' });
    captionNode = el('p', { class: 'cls-caption', 'aria-live': 'polite' });
    stripNode = el('div', { class: 'cls-scene-strip', role: 'tablist', 'aria-label': '场景列表' });
    scenes.forEach((scene, index) => {
      stripNode.append(el('button', {
        type: 'button',
        class: 'cls-scene-chip',
        role: 'tab',
        title: `${sceneTypeLabel(scene)} · ${scene.title || ''}`,
        onClick: () => { switchToScene(index, { keepPlaying: playing }); },
      },
      el('span', { class: 'cls-scene-chip-index' }, String(index + 1)),
      el('span', { class: 'cls-scene-chip-title' }, scene.title || contentDigest(scene) || `场景 ${index + 1}`),
      el('span', { class: 'cls-scene-chip-type' }, sceneTypeLabel(scene))));
    });

    const playBtn = el('button', { type: 'button', class: 'cls-control-play', onClick: togglePlay }, el('span', {}, '▶'), el('span', {}, '播放'));
    const prevBtn = button('上一页', () => switchToScene(sceneIndex - 1, { keepPlaying: playing }), 'secondary small', 'chevron');
    const nextBtn = button('下一页', () => switchToScene(sceneIndex + 1, { keepPlaying: playing }), 'secondary small', 'chevron');
    const prevAction = button('上一句', () => stepAction(-1), 'ghost small');
    const nextAction = button('下一句', () => stepAction(1), 'ghost small');
    const muteBtn = button(muted ? '🔇 已静音' : '🔊 语音开启', toggleMute, 'secondary small');
    const progressFill = el('span', { class: 'cls-progress-fill' });
    const progressLabel = el('span', { class: 'cls-progress-label' }, '0%');
    const actionLabel = el('span', { class: 'cls-action-label' }, '');

    const playRow = el('div', { class: 'row cls-play-row' }, playBtn, prevAction, nextAction);
    controlRefs = { playBtn, prevBtn, nextBtn, prevAction, nextAction, muteBtn, progressFill, progressLabel, actionLabel, playRow };

    const controls = el('div', { class: 'cls-controls' },
      el('div', { class: 'cls-controls-main' },
        playRow,
        el('div', { class: 'cls-controls-scene' }, prevBtn, nextBtn, muteBtn),
        el('div', { class: 'cls-controls-progress' },
          el('div', { class: 'cls-progress-track' }, progressFill),
          el('div', { class: 'cls-progress-meta' }, progressLabel, actionLabel))),
      el('div', { class: 'cls-controls-sub' }, el('span', { class: 'help' }, '← / → 切场 · 空格 播放/暂停 · M 静音')));

    tutorRefs = buildTutorDrawer();

    root.replaceChildren(
      el('div', { class: 'cls-player-top' },
        el('div', { class: 'row cls-player-title-block' },
          button('返回', () => go('classroom', null, { course: courseId, view: 'generate' }), 'ghost small', 'chevron'),
          el('div', {},
            el('h2', { class: 'cls-player-title' }, course?.title || 'AI 课堂'),
            el('p', { class: 'cls-player-meta', 'aria-live': 'polite' }))),
        el('div', { class: 'row' },
          button('旁白', () => openNarrationPanel(), 'secondary small', 'play'),
          button('AI 配图', () => openImageDialog(), 'secondary small', 'edit'),
          button('导出', () => openExportDialog({ courseId, courseTitle: course?.title || '' }), 'secondary small', 'download'),
          button('AI 老师', () => openTutor(), 'secondary small', 'message'),
          button('返回课程列表', () => go('classroom'), 'ghost small', 'chevron'))),
      el('div', { class: 'cls-player-main' },
        el('div', { class: 'cls-stage-wrap' }, stageHost, captionNode),
        el('aside', { class: 'cls-player-side' },
          el('h3', {}, '课程场景'),
          stripNode,
          el('p', { class: 'help cls-side-tip' }, '点击场景可直接跳转；学习进度会自动保存，下次进入从上次位置继续。'),
          multiagent?.sideCardNode || null)),
      controls,
      tutorRefs.node);
  }

  function buildTutorDrawer() {
    const messages = el('div', { class: 'cls-tutor-messages', 'aria-live': 'polite' });
    const modeSelect = el('select', { class: 'cls-tutor-mode', 'aria-label': '对话模式' },
      el('option', { value: 'qa' }, '问答（qa）'),
      el('option', { value: 'discussion' }, '讨论（discussion）'));
    const composer = el('textarea', { class: 'cls-tutor-input', rows: 2, placeholder: '向 AI 老师提问，例如“为什么水是原料？”（Ctrl+Enter 发送；可点麦克风语音提问）' });
    const sendBtn = button('发送', null, 'small', 'sparkle');
    const hint = el('p', { class: 'help cls-tutor-hint' }, '');
    const voice = createVoiceInput({ target: composer, prefix: 'cls', label: '语音提问' });
    tutorVoice = voice;

    const tutorPanel = el('div', { class: 'cls-chat-panel', 'data-chat-panel': 'tutor' },
      el('div', { class: 'cls-tutor-composer' }, composer, voice.node, sendBtn),
      voice.hintNode,
      hint);

    multiagent = createMultiagentPanel({
      courseId,
      getSessionId: () => session?.id,
      getScene: () => currentScene(),
      getModelPayload: () => null,
      appendHistory: message => appendChatMessage(message),
      speak: (text, meta) => speakSegment(text, meta),
      executeActions: actions => executeDiscussionActions(actions),
      onSceneHint: hintValue => applyDiscussionSceneHint(hintValue),
      onOpenDiscussion: () => { openChat('multi'); },
      pausePlayback: () => pausePlaying(),
    });
    const multiPanel = el('div', { class: 'cls-chat-panel', 'data-chat-panel': 'multi', hidden: true },
      multiagent.panelNode);

    const tabButtons = {};
    const tabBar = el('div', { class: 'cls-chat-tabs', role: 'tablist', 'aria-label': '课堂对话模式' });
    for (const [id, label] of [['tutor', '自由问答'], ['multi', '多智能体讨论']]) {
      const tab = el('button', {
        type: 'button',
        class: `cls-chat-tab${id === 'tutor' ? ' active' : ''}`,
        role: 'tab',
        'aria-selected': id === 'tutor' ? 'true' : 'false',
      }, label);
      tab.addEventListener('click', () => setChatTab(id));
      tabButtons[id] = tab;
      tabBar.append(tab);
    }

    function setChatTab(tab) {
      const isMulti = tab === 'multi';
      for (const [id, buttonNode] of Object.entries(tabButtons)) {
        buttonNode.classList.toggle('active', id === tab);
        buttonNode.setAttribute('aria-selected', id === tab ? 'true' : 'false');
      }
      tutorPanel.hidden = isMulti;
      multiPanel.hidden = !isMulti;
      if (isMulti) {
        try { tutorVoice?.stop(); } catch { /* 忽略 */ }
        multiagent?.ensureRoster?.().catch(() => {});
      } else {
        multiagent?.stopDiscussion?.();
      }
    }

    const node = el('aside', { class: 'cls-tutor', 'aria-hidden': 'true', 'aria-label': '课堂对话' },
      el('div', { class: 'cls-tutor-head' },
        el('div', {},
          el('strong', {}, '课堂对话'),
          el('span', { class: 'help' }, '自由问答 / 多智能体讨论共用历史')),
        el('div', { class: 'row' },
          button('清空', () => clearChat(), 'ghost small', 'trash'),
          button('关闭', () => closeTutor(), 'ghost small', 'close'))),
      tabBar,
      multiagent.rosterNode,
      messages,
      tutorPanel,
      multiPanel);

    async function send() {
      const text = String(composer.value || '').trim();
      if (!text || !session) return;
      const previous = tutorMessages.map(item => ({ role: item.role, content: item.content })).slice(-8);
      appendChatMessage({ role: 'user', speaker: '学生', text, actions: [], source: 'tutor' });
      tutorMessages.push({ role: 'user', content: text });
      composer.value = '';
      hint.textContent = '';
      sendBtn.disabled = true;
      try {
        const response = await api(`/classroom/sessions/${session.id}/tutor`, {
          method: 'POST',
          data: {
            scene_id: currentScene()?.id,
            message: text,
            mode: modeSelect.value,
            history: previous,
          },
        });
        const replyText = String(response.reply?.text || '');
        appendChatMessage({
          role: 'assistant',
          speaker: 'AI 老师',
          text: replyText,
          actions: Array.isArray(response.reply?.actions) ? response.reply.actions : [],
          color: multiagent?.colorOf('AI 老师', 'teacher'),
          speaker_role: 'teacher',
          source: 'tutor',
        });
        tutorMessages.push({ role: 'assistant', content: replyText });
        for (const action of response.reply?.actions || []) executeExternalAction(action);
        if (response.scene_hint?.index !== undefined || response.scene_hint?.scene_id) {
          appendTutorHint(response.scene_hint);
        }
        if (response.warnings?.length) hint.textContent = response.warnings.join('；');
      } catch (error) {
        hint.replaceChildren(el('p', { class: 'cls-tutor-error' },
          error.message || 'AI 老师暂时不可用',
          el('a', { href: '#/settings', class: 'text-button' }, '前往模型设置')));
      } finally {
        if (sendBtn.isConnected) sendBtn.disabled = false;
      }
    }

    sendBtn.addEventListener('click', () => busy(sendBtn, send));
    composer.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        busy(sendBtn, send);
      }
    });

    return {
      node,
      messages,
      composer,
      hint,
      setChatTab,
      setOpen(open) {
        node.classList.toggle('open', open);
        node.setAttribute('aria-hidden', open ? 'false' : 'true');
      },
    };
  }

  function chatRoleLabel(message) {
    if (message.role === 'user') return '我';
    if (message.role === 'teacher') return '老师';
    if (message.role === 'student') return '同学';
    if (message.role === 'assistant') {
      if (message.speaker_role === 'teacher' || /老师/.test(String(message.speaker || ''))) return '老师';
      return multiagent?.roleLabel?.('assistant') || '助教';
    }
    return multiagent?.roleLabel?.(message.role) || '';
  }

  function clearChat() {
    chatTurns = [];
    tutorMessages = [];
    if (!tutorRefs) return;
    tutorRefs.messages.replaceChildren(el('p', { class: 'help cls-tutor-empty' },
      '已清空当前对话显示（服务端历史仍保留，刷新后可从会话记录恢复）。'));
    toast('已清空当前对话显示（服务端历史保留）');
  }

  /** 统一历史气泡：学生右侧；老师/助教/同学左侧（按角色配色与标签区分）。 */
  function renderChatBubble(message) {
    if (!tutorRefs) return null;
    const isUser = message.role === 'user';
    const role = message.role || 'assistant';
    const color = isUser
      ? '#3f6f8f'
      : (message.color || multiagent?.colorOf(message.speaker, role) || '#2f6f5e');
    const actions = Array.isArray(message.actions) ? message.actions : [];
    const bubble = el('div', {
      class: `cls-tutor-message ${isUser ? 'is-user' : 'is-assistant is-role'}${message.source === 'discussion' ? ' is-discussion' : ''}`,
      'data-role': role,
    },
    isUser ? null : el('div', { class: 'cls-chat-speaker' },
      el('span', { class: 'cls-chat-dot', style: `background:${color}` }),
      el('strong', {}, message.speaker || 'AI 老师'),
      chatRoleLabel(message) ? el('span', { class: 'help' }, chatRoleLabel(message)) : null),
    el('div', { class: 'cls-tutor-bubble' },
      isUser ? el('p', {}, String(message.text || '')) : markdownNode(message.text || ''),
      !isUser && actions.length
        ? el('p', { class: 'help cls-chat-actions' }, `动作：${actions.map(action => action.type).join('、')}`)
        : null),
    message.source === 'discussion' ? el('span', { class: 'cls-chat-source' }, '多智能体') : null);
    tutorRefs.messages.append(bubble);
    tutorRefs.messages.scrollTop = tutorRefs.messages.scrollHeight;
    return bubble;
  }

  function appendChatMessage(message, { record = true } = {}) {
    if (record) chatTurns.push(message);
    if (tutorRefs) tutorRefs.messages.querySelector('.cls-tutor-empty')?.remove();
    return renderChatBubble(message);
  }

  function appendTutorHint(hint) {
    if (!tutorRefs) return;
    const index = hint.index !== undefined ? Number(hint.index) : scenes.findIndex(scene => String(scene.id) === String(hint.scene_id));
    if (!Number.isInteger(index) || index < 0 || index >= scenes.length) return;
    tutorRefs.messages.append(el('div', { class: 'cls-tutor-hint-row' },
      el('span', { class: 'help' }, hint.reason || 'AI 老师建议切换到这里继续：'),
      button(`前往第 ${index + 1} 页`, () => switchToScene(index, { keepPlaying: false }), 'secondary small', 'chevron')));
    tutorRefs.messages.scrollTop = tutorRefs.messages.scrollHeight;
  }

  async function loadChatHistory() {
    if (!session || !tutorRefs) return;
    let data;
    try {
      data = await api(`/classroom/sessions/${session.id}`);
    } catch {
      return;
    }
    const turns = Array.isArray(data.turns) ? data.turns : [];
    chatTurns = [];
    tutorMessages = [];
    tutorRefs.messages.replaceChildren();
    if (!turns.length) {
      tutorRefs.messages.append(el('p', { class: 'help cls-tutor-empty' },
        '还没有课堂对话。可以在「自由问答」直接提问，或到「多智能体讨论」让老师和同学一起讲。'));
      return;
    }
    for (const turn of turns) {
      const role = turn.role === 'user' ? 'user' : (turn.role || 'assistant');
      const message = role === 'user'
        ? { role, speaker: '学生', text: turn.content || '', actions: [] }
        : {
          role,
          speaker: turn.speaker || (role === 'teacher' ? 'AI 老师' : 'AI 同学'),
          text: turn.content || '',
          actions: Array.isArray(turn.actions) ? turn.actions : [],
          color: multiagent?.colorOf(turn.speaker, role),
          speaker_role: role,
        };
      chatTurns.push(message);
      renderChatBubble(message);
      if (role === 'user' || role === 'assistant') {
        tutorMessages.push({ role: role === 'user' ? 'user' : 'assistant', content: message.text });
      }
    }
  }

  async function openChat(tab = 'tutor') {
    if (!tutorRefs) return;
    tutorRefs.setOpen(true);
    tutorRefs.setChatTab(tab);
    await multiagent?.ensureRoster?.().catch(() => {});
    await loadChatHistory();
  }

  function openTutor() {
    return openChat('tutor');
  }

  function closeTutor() {
    try { tutorVoice?.stop(); } catch { /* 忽略 */ }
    multiagent?.stopDiscussion?.();
    tutorRefs?.setOpen(false);
  }

  function executeDiscussionActions(actions) {
    for (const action of Array.isArray(actions) ? actions : []) {
      if (!action || typeof action !== 'object') continue;
      if (action.type === 'whiteboard') {
        applyWhiteboard(action.ops, action.coord_space === 'unit');
        continue;
      }
      if (action.type === 'spotlight' || action.type === 'laser' || action.type === 'highlight') {
        if (currentScene()?.type === 'slide') applyActionVisual(action);
        continue;
      }
      if (action.type === 'next_scene') { switchToScene(sceneIndex + 1, { keepPlaying: false }); continue; }
      if (action.type === 'show_slide') {
        const index = targetSceneIndex(action);
        if (index >= 0) switchToScene(index, { keepPlaying: false });
        continue;
      }
      if (action.type === 'play_audio') { playAudioAction(action); continue; }
      // speech 由气泡文本承担；未知动作忽略。
    }
  }

  function applyDiscussionSceneHint(hint) {
    if (!hint) return;
    const index = hint.index !== undefined
      ? Number(hint.index)
      : scenes.findIndex(scene => String(scene.id) === String(hint.scene_id));
    if (Number.isInteger(index) && index >= 0 && index < scenes.length) {
      switchToScene(index, { keepPlaying: false });
    }
  }

  function executeExternalAction(action) {
    if (!action || typeof action !== 'object') return;
    if (action.type === 'speech') {
      captionNode.textContent = String(action.text || '');
      if (!muted) speak(action.text, runToken);
      return;
    }
    if (action.type === 'next_scene') { switchToScene(sceneIndex + 1, { keepPlaying: false }); return; }
    if (action.type === 'show_slide') {
      const index = targetSceneIndex(action);
      if (index >= 0) switchToScene(index, { keepPlaying: false });
      return;
    }
    if (action.type === 'play_audio') { playAudioAction(action); return; }
    applyActionVisual(action);
  }

  // ---------------------------------------------------------------------------
  // 消息 / 键盘 / 生命周期
  // ---------------------------------------------------------------------------
  const INTERACTIVE_EVENTS = new Set(['ready', 'error', 'state']);

  function interactiveStatusNode() {
    return root.querySelector('.cls-interactive-status');
  }

  function forwardInteractiveEvent(scene, type, payload) {
    if (!scene || type !== 'state' && type !== 'ready' && type !== 'error') return;
    const summary = String(payload.summary || payload.state?.summary || '').slice(0, 500);
    const state = payload.state && typeof payload.state === 'object' ? payload.state : null;
    if (type === 'state') {
      interactiveState = { scene_id: scene.id, summary, state: state || {}, updated_at: new Date().toISOString() };
    }
    if (!session?.id) return;
    api(`/classroom/scenes/${scene.id}/interactive-event`, {
      method: 'POST',
      data: {
        type,
        session_id: session.id,
        summary: summary || null,
        state,
        message: type === 'error' ? String(payload.message || '脚本错误').slice(0, 2000) : null,
      },
    }).catch(() => { /* 事件上报失败不影响交互运行 */ });
  }

  function onWindowMessage(event) {
    if (!currentFrame || event.source !== currentFrame.contentWindow) return;
    const scene = currentScene();
    if (!scene) return;
    const message = event.data;
    if (!message || typeof message !== 'object') return;
    // P1 交互页协议：{source:'zhixu-interactive', type:'ready'|'error'|'state', payload:{summary,state,message}}
    if (message.source === 'zhixu-interactive') {
      const type = String(message.type || '').toLowerCase();
      if (!INTERACTIVE_EVENTS.has(type)) return;
      const payload = message.payload && typeof message.payload === 'object' ? message.payload : {};
      frameReady = true;
      const status = interactiveStatusNode();
      if (status && status.isConnected) {
        if (type === 'error') {
          status.classList.add('is-error');
          status.textContent = `交互页面报错：${String(payload.message || '未知错误').slice(0, 200)}；可点击「重新加载」或「重新生成该场景」。`;
        } else if (type === 'state') {
          status.classList.remove('is-error');
          const summary = String(payload.summary || payload.state?.summary || '').slice(0, 240);
          status.textContent = summary
            ? `交互状态已同步给 AI 老师：${summary}`
            : '交互状态已同步给 AI 老师（可从页面控件继续操作）。';
        } else {
          status.classList.remove('is-error');
          status.textContent = '交互页面已就绪，可开始操作；状态会同步给 AI 老师。';
        }
      }
      forwardInteractiveEvent(scene, type, payload);
      return;
    }
    // 兼容旧协议的 maic:/cls: 事件（无 P1 上报脚本时的兜底展示）。
    const type = String(message.type || '');
    if (!['maic:ready', 'maic:state', 'maic:error', 'cls:ready', 'cls:state', 'cls:error'].includes(type)) return;
    const payload = message.payload && typeof message.payload === 'object' ? message.payload : message;
    frameReady = true;
    const status = interactiveStatusNode();
    if (status && status.isConnected) {
      if (/error$/i.test(type)) {
        status.classList.add('is-error');
        status.textContent = `交互页面报错：${String(payload.message || '未知错误').slice(0, 200)}；可点击「重新加载」。`;
      } else {
        status.textContent = payload.summary
          ? `交互状态：${String(payload.summary).slice(0, 240)}`
          : '交互页面已就绪（沙箱 iframe，白名单 postMessage 通信）。';
      }
    }
  }

  let keyCleanup = null;

  function bindKeys() {
    const handler = event => {
      if (destroyed || !root.isConnected) return;
      // 演示组件（principleDemo）自己处理 ←/→/空格 并 preventDefault：已消费的按键
      // 不再触发播放器级场景导航，避免一次按键同时步进演示和切场（不新增按键绑定）。
      if (event.defaultPrevented) return;
      const target = event.target;
      if (target && typeof target.closest === 'function'
        && target.closest('input,textarea,select,[contenteditable="true"]')) return;
      if (event.key === ' ' || event.code === 'Space') {
        event.preventDefault();
        togglePlay();
      } else if (event.key === 'ArrowRight') {
        switchToScene(sceneIndex + 1, { keepPlaying: playing });
      } else if (event.key === 'ArrowLeft') {
        switchToScene(sceneIndex - 1, { keepPlaying: playing });
      } else if (event.key.toLowerCase() === 'm') {
        toggleMute();
      }
    };
    document.addEventListener('keydown', handler);
    keyCleanup = () => document.removeEventListener('keydown', handler);
    window.addEventListener('message', onWindowMessage);
    const onHide = () => saveNow({ beacon: true });
    window.addEventListener('pagehide', onHide);
    keyCleanup = (() => {
      const inner = keyCleanup;
      return () => {
        inner();
        window.removeEventListener('message', onWindowMessage);
        window.removeEventListener('pagehide', onHide);
      };
    })();
  }

  // ---------------------------------------------------------------------------
  // 会话
  // ---------------------------------------------------------------------------
  async function ensureSession() {
    const saved = readLocal(sessionKey(courseId), null);
    if (saved?.id) {
      try {
        const data = await api(`/classroom/sessions/${saved.id}`);
        if (data?.session) return data.session;
      } catch {
        /* 会话失效/被删：重新建一个 */
      }
    }
    const created = await api(`/classroom/courses/${courseId}/sessions`, {
      method: 'POST',
      data: { scene_id: scenes[0]?.id },
    });
    const next = created?.session;
    if (next?.id) saveLocal(sessionKey(courseId), { id: next.id });
    return next;
  }

  // ---------------------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------------------
  async function init() {
    root.replaceChildren(loading('正在打开 AI 课堂…'));
    try {
      detail = await api(`/classroom/courses/${courseId}`);
    } catch (error) {
      if (destroyed || !version) return;
      root.replaceChildren(
        notice(error.message || '课程读取失败，请重试。', 'error'),
        el('div', { class: 'row' },
          button('重新加载', event => busy(event.currentTarget, init), '', 'repeat'),
          button('返回课程列表', () => go('classroom'), 'secondary', 'chevron')));
      return;
    }
    if (destroyed) return;
    course = detail.course || {};
    scenes = Array.isArray(detail.scenes)
      ? detail.scenes.filter(scene => scene && (scene.type || scene.scene_type))
      : [];
    if (!scenes.length) {
      root.replaceChildren(
        notice('这门课程还没有生成任何场景：请先完成大纲确认与场景生成。', 'warning'),
        el('div', { class: 'row' },
          button('去生成课程', () => go('classroom', null, { course: courseId, view: 'generate' }), '', 'sparkle'),
          button('返回课程列表', () => go('classroom'), 'secondary', 'chevron')));
      return;
    }
    try {
      session = await ensureSession();
    } catch (error) {
      root.replaceChildren(notice(error.message || '学习会话创建失败，请重试。', 'error'),
        button('重新加载', event => busy(event.currentTarget, init), '', 'repeat'));
      return;
    }
    if (destroyed) return;
    const progress = session?.progress || {};
    const savedInteractive = progress.interactive;
    if (savedInteractive && typeof savedInteractive === 'object' && savedInteractive.scene_id !== undefined) {
      interactiveState = {
        scene_id: savedInteractive.scene_id,
        summary: String(savedInteractive.summary || ''),
        state: savedInteractive.state && typeof savedInteractive.state === 'object' ? savedInteractive.state : {},
        updated_at: savedInteractive.updated_at || '',
      };
    }
    const startIndex = clamp(Number(progress.scene_index) || 0, 0, scenes.length - 1);
    const startAction = Number(progress.action_index) || 0;
    buildSkeleton();
    bindKeys();
    renderScene(startIndex, { resumeAction: startAction });
    if (startIndex > 0 || startAction > 0) {
      toast(`已从上次进度继续：第 ${startIndex + 1}/${scenes.length} 页`);
    }
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    playing = false;
    runToken += 1;
    cancelSpeech();
    destroyDemoStage();
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    saveNow({ beacon: true });
    if (keyCleanup) { try { keyCleanup(); } catch { /* 忽略 */ } keyCleanup = null; }
    try { multiagent?.destroy(); } catch { /* 忽略 */ }
    multiagent = null;
    try { tutorVoice?.destroy(); } catch { /* 忽略 */ }
    tutorVoice = null;
    for (const dialog of mediaDialogs) {
      try { dialog.close(); } catch { /* 忽略 */ }
    }
    mediaDialogs.clear();
    clearFit();
    if (session?.id) saveLocal(sessionKey(courseId), { id: session.id });
  }

  init();

  return { node: root, destroy };
}
