import { t } from '../core/i18n.js';
/**
 * 顶栏专注计时（番茄钟）小组件。
 *
 * 计时内核 vendored 自 shigure（MIT，web/vendor/shigure/timer.js）：用绝对时间戳
 * `plannedEndAt - Date.now()` 推算剩余，`setInterval` 只负责重绘，因此后台标签页
 * 被节流、切走再回来都不会漂移。
 *
 * 本模块只负责三件事：把状态机接到 DOM、把面板设置持久化到 localStorage、
 * 在会话结束/停止时向后端 `POST /api/focus/sessions` 记一笔（`client_id` 幂等，
 * 失败进 localStorage 待补队列，下次挂载重试）。
 */
import { Timer } from '../../vendor/shigure/timer.js';
import { api } from '../core/api.js';
import { state } from '../core/state.js';
import { el, icon, toast } from '../core/dom.js';

const MODES = {
  focus: { get label() { return t('focus.mode.focus'); }, presets: [25, 45, 60] },
  short_break: { get label() { return t('focus.mode.shortBreak'); }, presets: [5] },
  long_break: { get label() { return t('focus.mode.longBreak'); }, presets: [15] },
};
const DEFAULT_SETTINGS = { minutes: 25, mode: 'focus', task: '', sound: true };
const MAX_PENDING = 20;
const RING_LENGTH = 56.55; // 2πr，r=9

function storageKey() {
  return state.user?.id ? `zhixu.focus.${state.user.id}` : null;
}

function readSaved() {
  const key = storageKey();
  const fallback = { timer: null, ...DEFAULT_SETTINGS, pending: [] };
  if (!key) return fallback;
  try {
    const raw = JSON.parse(localStorage.getItem(key) || '{}');
    return {
      timer: raw.timer || null,
      minutes: Number(raw.minutes) || DEFAULT_SETTINGS.minutes,
      mode: MODES[raw.mode] ? raw.mode : DEFAULT_SETTINGS.mode,
      task: typeof raw.task === 'string' ? raw.task.slice(0, 80) : '',
      sound: raw.sound !== false,
      pending: Array.isArray(raw.pending) ? raw.pending.slice(-MAX_PENDING) : [],
    };
  } catch {
    return fallback;
  }
}

function writeSaved(next) {
  const key = storageKey();
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(next));
  } catch {
    /* 隐私模式禁用存储时计时仍可用，只是刷新后不恢复 */
  }
}

/** 本地日期（YYYY-MM-DD）：中国时区 23:50 的会话计入当天，而不是 UTC 次日。 */
function localDate(ms = Date.now()) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function formatClock(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function clampMinutes(value) {
  if (!Number.isFinite(value)) return 25;
  return Math.min(180, Math.max(1, Math.round(value)));
}

/** 结束提示音用 WebAudio 合成，避免引入音频文件（离线/体积/许可都更干净）。 */
function makeBeeper() {
  let ctx = null;
  return function beep(enabled) {
    if (!enabled) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') ctx.resume();
      const now = ctx.currentTime;
      [880, 1174].forEach((freq, index) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + index * 0.18);
        gain.gain.exponentialRampToValueAtTime(0.16, now + index * 0.18 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.18 + 0.16);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + index * 0.18);
        osc.stop(now + index * 0.18 + 0.18);
      });
    } catch {
      /* 浏览器拒绝创建音频上下文时静默降级 */
    }
  };
}

function svgCircle(className, extra = {}) {
  const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  circle.setAttribute('cx', '12');
  circle.setAttribute('cy', '12');
  circle.setAttribute('r', '9');
  circle.setAttribute('class', className);
  for (const [key, value] of Object.entries(extra)) circle.setAttribute(key, String(value));
  return circle;
}

export function mountFocusWidget(host) {
  if (!host || !state.user) return () => {};

  let saved = readSaved();
  let disposed = false;
  let summaryTimer = null;
  const beep = makeBeeper();
  const submitted = new Set();

  /* ------------------------------------------------------------------ DOM */

  const ring = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  ring.setAttribute('viewBox', '0 0 24 24');
  ring.setAttribute('class', 'fc-ring');
  ring.setAttribute('aria-hidden', 'true');
  const ringBar = svgCircle('fc-ring-bar', { 'stroke-dasharray': RING_LENGTH });
  ring.append(svgCircle('fc-ring-track'), ringBar);

  const pillText = el('span', { class: 'fc-pill-text' }, t('focus.pill'));
  const panelId = 'fc-panel';
  const pill = el('button', {
    type: 'button',
    class: 'fc-pill',
    'aria-expanded': 'false',
    'aria-controls': panelId,
    'aria-label': t('focus.openAria'),
  }, ring, pillText);

  const modeRow = el('div', { class: 'fc-modes', role: 'group', 'aria-label': t('focus.modeAria') });
  const presetRow = el('div', { class: 'fc-presets', role: 'group', 'aria-label': t('focus.presetAria') });
  const customMinutes = el('input', {
    class: 'input fc-custom', type: 'number', min: '1', max: '180', step: '1',
    'aria-label': t('focus.customMinutesAria'), inputmode: 'numeric',
  });
  const taskInput = el('input', {
    class: 'input fc-task', type: 'text', maxlength: '80',
    placeholder: t('focus.taskPlaceholder'),
    'aria-label': t('focus.taskAria'),
  });
  const soundToggle = el('input', { type: 'checkbox' });
  const primaryBtn = el('button', { type: 'button', class: 'button fc-primary' }, t('focus.start'));
  const stopBtn = el('button', { type: 'button', class: 'button ghost fc-stop', hidden: true }, t('focus.stop'));
  const clock = el('div', { class: 'fc-clock' }, '25:00');
  const clockCaption = el('div', { class: 'fc-clock-caption' }, t('focus.ready'));
  const statsLine = el('div', { class: 'fc-stats' }, t('focus.todayZero'));

  const panel = el('div', {
    class: 'fc-panel', id: panelId, role: 'dialog', 'aria-label': t('focus.panelAria'), hidden: true,
  },
    el('div', { class: 'fc-head' },
      el('strong', {}, t('focus.panelTitle')),
      el('button', {
        type: 'button', class: 'icon-button', 'aria-label': t('focus.closeAria'),
        onClick: () => togglePanel(false),
      }, icon('close'))),
    el('div', { class: 'fc-clock-wrap' }, clock, clockCaption),
    modeRow,
    presetRow,
    el('div', { class: 'fc-preset-custom' }, customMinutes, el('span', {}, t('focus.minutesUnit'))),
    taskInput,
    el('div', { class: 'fc-actions' }, primaryBtn, stopBtn),
    el('label', { class: 'check-label fc-sound' }, soundToggle, t('focus.soundLabel')),
    statsLine);

  const wrap = el('div', { class: 'fc-wrap' }, pill, panel);
  host.append(wrap);

  /* -------------------------------------------------------------- 设置持久化 */

  taskInput.value = saved.task;
  soundToggle.checked = saved.sound;
  customMinutes.value = String(saved.minutes);

  function persistSettings() {
    saved = {
      ...saved,
      minutes: clampMinutes(Number(customMinutes.value) || saved.minutes),
      task: taskInput.value.trim().slice(0, 80),
      sound: soundToggle.checked,
    };
    customMinutes.value = String(saved.minutes);
    writeSaved(saved);
    renderModes();
    renderPresets();
  }

  function persistTimer(timerState) {
    saved = { ...saved, timer: timerState };
    writeSaved(saved);
  }

  /* ------------------------------------------------------------ 状态与渲染 */

  let baseTitle = document.title;
  const timer = new Timer(handleTick, persistTimer);

  function busyStatus() {
    const status = timer.state.status;
    return status === 'running' || status === 'paused';
  }

  function renderModes() {
    modeRow.replaceChildren(...Object.entries(MODES).map(([key, cfg]) => el('button', {
      type: 'button',
      class: `fc-chip${key === saved.mode ? ' active' : ''}`,
      'aria-pressed': String(key === saved.mode),
      disabled: busyStatus(),
      onClick: () => {
        if (busyStatus()) return;
        saved.mode = key;
        saved.minutes = cfg.presets[0];
        customMinutes.value = String(saved.minutes);
        writeSaved(saved);
        renderModes();
        renderPresets();
        renderState();
      },
    }, cfg.label)));
  }

  function renderPresets() {
    presetRow.replaceChildren(...MODES[saved.mode].presets.map((minutes) => el('button', {
      type: 'button',
      class: `fc-chip${minutes === saved.minutes ? ' active' : ''}`,
      'aria-pressed': String(minutes === saved.minutes),
      disabled: busyStatus(),
      onClick: () => {
        if (busyStatus()) return;
        saved.minutes = minutes;
        customMinutes.value = String(minutes);
        writeSaved(saved);
        renderPresets();
        renderState();
      },
    }, t('focus.minutesShort', { minutes }))));
  }

  function renderState() {
    if (disposed) return;
    const status = timer.state.status;
    const session = timer.state.session;
    const busy = status === 'running' || status === 'paused';
    const remaining = Number(timer.state.remainingMs) || 0;

    if (busy && session) {
      const duration = session.durationMs || saved.minutes * 60000;
      const ratio = Math.max(0, Math.min(1, 1 - remaining / duration));
      ringBar.setAttribute('stroke-dashoffset', String(RING_LENGTH * (1 - ratio)));
      pillText.textContent = formatClock(remaining);
      pill.classList.add('running');
      pill.classList.toggle('paused', status === 'paused');
      clock.textContent = formatClock(remaining);
      const modeLabel = MODES[session.mode]?.label || t('focus.mode.focus');
      clockCaption.textContent = t('focus.runningCaption', { mode: modeLabel, task: session.task || t('focus.unnamedTask') });
      primaryBtn.textContent = status === 'running' ? t('focus.pause') : t('focus.resume');
      stopBtn.hidden = false;
      document.title = t('focus.docTitle', { clock: formatClock(remaining), mode: modeLabel });
    } else {
      ringBar.setAttribute('stroke-dashoffset', String(RING_LENGTH));
      pill.classList.remove('running', 'paused');
      pillText.textContent = status === 'completed' ? t('focus.completed') : t('focus.pill');
      clock.textContent = formatClock(saved.minutes * 60000);
      clockCaption.textContent = status === 'completed' ? t('focus.completedCaption') : t('focus.ready');
      primaryBtn.textContent = t('focus.start');
      stopBtn.hidden = true;
      if (baseTitle && document.title !== baseTitle) document.title = baseTitle;
    }
    modeRow.querySelectorAll('button').forEach((node) => { node.disabled = busy; });
    presetRow.querySelectorAll('button').forEach((node) => { node.disabled = busy; });
    customMinutes.disabled = busy;
  }

  /* ------------------------------------------------------------- 会话提交 */

  function buildPayload(session, status) {
    const endedAt = session.endedAt || Date.now();
    const planned = clampMinutes(Math.round((session.durationMs || 0) / 60000) || saved.minutes);
    const rawElapsed = Math.round((endedAt - session.startedAt) / 1000);
    const elapsed = status === 'completed'
      ? Math.min(planned * 60, Math.max(0, rawElapsed))
      : Math.max(0, rawElapsed);
    return {
      client_id: String(session.id).slice(0, 64),
      mode: session.mode || saved.mode,
      planned_minutes: planned,
      elapsed_seconds: elapsed,
      status,
      task_label: (session.task || '').slice(0, 80),
      local_date: localDate(session.startedAt),
      started_ms: session.startedAt,
      ended_ms: endedAt,
    };
  }

  async function submit(session, status) {
    if (!session || submitted.has(session.id)) return;
    submitted.add(session.id);
    const payload = buildPayload(session, status);
    try {
      await api('/focus/sessions', { method: 'POST', data: payload });
      refreshStats();
    } catch (error) {
      saved = { ...saved, pending: [...(saved.pending || []), payload].slice(-MAX_PENDING) };
      writeSaved(saved);
      toast(t('focus.saveQueued', { message: error.message }), true);
    }
  }

  async function flushPending() {
    const queue = saved.pending || [];
    if (!queue.length) return;
    const remaining = [];
    for (const payload of queue) {
      try {
        await api('/focus/sessions', { method: 'POST', data: payload });
        submitted.add(payload.client_id);
      } catch {
        remaining.push(payload);
      }
    }
    const changed = remaining.length !== queue.length;
    saved = { ...saved, pending: remaining };
    writeSaved(saved);
    if (changed) refreshStats();
  }

  async function refreshStats() {
    clearTimeout(summaryTimer);
    try {
      const data = await api(`/focus/summary?days=7&today=${localDate()}`);
      statsLine.replaceChildren(
        el('span', {}, t('focus.today'), el('strong', {}, String(data.today_minutes ?? 0)), t('focus.minutesWord')),
        el('span', { class: 'fc-dot' }, '·'),
        el('span', {}, t('focus.week'), el('strong', {}, String(data.week_minutes ?? 0)), t('focus.minutesWord')),
        el('span', { class: 'fc-dot' }, '·'),
        el('span', {}, t('focus.streak'), el('strong', {}, String(data.streak_days ?? 0)), t('focus.daysWord')),
      );
    } catch {
      statsLine.textContent = t('focus.statsUnavailable');
    }
    if (!disposed) summaryTimer = setTimeout(refreshStats, 60000);
  }

  /* --------------------------------------------------------- Timer 回调 */

  function handleTick(timerState) {
    renderState();
    if (timerState?.status === 'completed' && timerState.session) {
      submit(timerState.session, 'completed');
      beep(saved.sound);
      toast(t('focus.completeToast'));
      setTimeout(() => {
        if (!disposed && timer.state.status === 'completed') timer.reset();
      }, 1400);
    }
  }

  /* ------------------------------------------------------------ 交互事件 */

  function togglePanel(force) {
    const open = typeof force === 'boolean' ? force : panel.hidden;
    panel.hidden = !open;
    pill.setAttribute('aria-expanded', String(open));
    if (open) {
      taskInput.value = saved.task;
      renderState();
      refreshStats();
      setTimeout(() => primaryBtn.focus(), 0);
    }
  }

  pill.addEventListener('click', () => togglePanel());
  primaryBtn.addEventListener('click', () => {
    const status = timer.state.status;
    if (status === 'running') { timer.pause(); return; }
    if (status === 'paused') { timer.resume(); return; }
    persistSettings();
    beep(saved.sound); // 借一次用户手势解锁 WebAudio
    timer.start({ task: saved.task, mode: saved.mode, minutes: saved.minutes });
  });
  stopBtn.addEventListener('click', () => {
    const session = timer.stop('stopped');
    if (!session) return;
    submit(session, 'stopped');
    toast(t('focus.recordedToast'));
    timer.reset();
  });
  customMinutes.addEventListener('change', persistSettings);
  taskInput.addEventListener('change', persistSettings);
  soundToggle.addEventListener('change', persistSettings);

  const onKeydown = (event) => {
    if (event.key === 'Escape' && !panel.hidden) {
      togglePanel(false);
      pill.focus();
    }
  };
  const onVisibility = () => {
    if (document.visibilityState === 'visible' && timer.state.status === 'running') {
      timer.checkpoint();
      renderState();
    }
  };
  const onStorage = (event) => {
    if (event.key !== storageKey() || busyStatus()) return;
    const remote = readSaved();
    if (!remote.timer?.session) return;
    // 另一个标签页在计时：本页只读跟随，避免两个页面各记一笔。
    saved = { ...remote, task: saved.task, minutes: saved.minutes, mode: saved.mode, sound: saved.sound, pending: saved.pending };
    timer.restore(remote.timer);
  };
  const onPageHide = () => {
    if (timer.state.status === 'running') timer.checkpoint();
    const payload = saved.pending?.[0];
    if (!payload) return;
    try {
      fetch('/api/focus/sessions', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch {
      /* 忽略：下次挂载继续补交 */
    }
  };

  document.addEventListener('keydown', onKeydown);
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('storage', onStorage);
  window.addEventListener('pagehide', onPageHide);

  renderModes();
  renderPresets();
  renderState();
  if (saved.timer?.session) timer.restore(saved.timer);
  flushPending();
  refreshStats();

  return function dispose() {
    disposed = true;
    clearTimeout(summaryTimer);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('storage', onStorage);
    window.removeEventListener('pagehide', onPageHide);
    if (timer.state.status === 'running') timer.pause();
    if (baseTitle) document.title = baseTitle;
    wrap.remove();
  };
}
