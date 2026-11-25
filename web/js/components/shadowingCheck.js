/* 跟读检查（阶段 4-3）：录音 → 本地/云端识别 → 文本相似度反馈。
 *
 * 明确不是发音评测：只比较 ASR 转写与原文的文本相似度，给出分数与可重试反馈；
 * 识别不可用时只显示原文，不给分数。录音只上传到本机知序服务，不写入学习事件，
 * 也不保存音频正文。
 */
import { el, button, badge, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { reportFeatureUse } from '../core/usage.js';
import { blobToWav16k } from './wavEncode.js';
import { createReadAloudButton } from './readAloud.js';

const MAX_RECORD_MS = 60 * 1000;
const activePanels = new Set();
let routeHookBound = false;

function bindRouteHook() {
  if (routeHookBound || typeof window === 'undefined') return;
  routeHookBound = true;
  window.addEventListener('hashchange', () => stopAllShadowing());
}

/** 停止/销毁当前页面全部跟读面板（路由切换或换题时调用）。 */
export function stopAllShadowing() {
  for (const panel of [...activePanels]) {
    try { panel.destroy(); } catch { /* 单个失败不影响其它面板 */ }
  }
}

function mediaSupported() {
  return Boolean(
    typeof window !== 'undefined'
    && window.MediaRecorder
    && navigator?.mediaDevices?.getUserMedia,
  );
}

function pickMimeType() {
  const recorder = typeof window !== 'undefined' ? window.MediaRecorder : null;
  if (!recorder || typeof recorder.isTypeSupported !== 'function') return '';
  for (const type of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
    try { if (recorder.isTypeSupported(type)) return type; } catch { /* 试下一个 */ }
  }
  return '';
}

function describeMediaError(error) {
  const name = String(error?.name || '');
  if (/NotAllowed|Permission|Security/i.test(name)) return t('speech.shadow.micDenied');
  if (/NotFound|DevicesNotFound|Overconstrained/i.test(name)) return t('speech.shadow.noDevice');
  return t('speech.shadow.recordFailed');
}

/**
 * 创建跟读检查面板。
 * @param {{ getExpected: () => string, compact?: boolean }} options
 * @returns {{ element: HTMLElement, destroy: () => void }}
 */
export function createShadowingPanel(options = {}) {
  bindRouteHook();
  const getExpected = typeof options.getExpected === 'function'
    ? options.getExpected : () => '';
  const host = el('section', {
    class: `speech-shadow${options.compact ? ' is-compact' : ''}`,
    'aria-busy': 'true',
  });
  const statusLine = el('p', {
    class: 'help speech-shadow-status', role: 'status', 'aria-live': 'polite',
  });
  const expectedLine = el('p', { class: 'speech-shadow-expected' });
  const resultBox = el('div', { class: 'speech-shadow-result', 'aria-live': 'polite' });
  const controls = el('div', { class: 'speech-shadow-controls' });
  const readHolder = el('span', { class: 'speech-shadow-read' });

  let destroyed = false;
  let info = null;
  let recording = false;
  let submitting = false;
  let recorder = null;
  let stream = null;
  let chunks = [];
  let timer = null;
  let lastTranscript = '';
  let recordingStartedAt = 0;

  host.append(
    el('div', { class: 'speech-shadow-head' },
      el('div', {},
        el('h3', {}, t('speech.shadow.title')),
        el('p', { class: 'help' }, t('speech.shadow.hint'))),
      readHolder),
    expectedLine,
    statusLine,
    controls,
    resultBox,
  );

  const readButton = createReadAloudButton({
    getText: () => getExpected(),
    label: t('speech.shadow.listen'),
  });
  readHolder.append(readButton.node);

  function setStatus(text, tone = '') {
    statusLine.textContent = text || '';
    if (tone) statusLine.dataset.tone = tone;
    else delete statusLine.dataset.tone;
  }

  function refreshExpected() {
    const expected = String(getExpected() || '').trim();
    expectedLine.textContent = expected ? t('speech.shadow.expected', { text: expected }) : t('speech.shadow.noExpected');
    return expected;
  }

  function stopStream() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch { /* 忽略 */ }
    }
    recorder = null;
    if (stream) {
      for (const track of stream.getTracks()) {
        try { track.stop(); } catch { /* 忽略 */ }
      }
      stream = null;
    }
    recording = false;
  }

  function renderControls() {
    const nodes = [];
    if (recording) {
      nodes.push(button(t('speech.shadow.stopRecord'), () => stopRecording(), 'soft small', 'stop'));
    } else {
      nodes.push(button(
        lastTranscript ? t('speech.shadow.retry') : t('speech.shadow.start'),
        () => startRecording(),
        'secondary small',
        'message',
      ));
    }
    for (const node of nodes) {
      node.disabled = submitting || destroyed || !info?.asr?.available;
      node.style.minHeight = '44px';
    }
    controls.replaceChildren(...nodes);
  }

  async function loadInfo() {
    try {
      const response = await fetch('/api/speech/info', { credentials: 'same-origin' });
      info = await response.json().catch(() => ({}));
      if (!response.ok) info = null;
    } catch {
      info = null;
    }
    host.removeAttribute('aria-busy');
    if (destroyed) return;
    refreshExpected();
    renderControls();
    if (!info?.asr?.available) {
      setStatus(info?.asr?.notice || t('speech.shadow.unavailable'), 'error');
    } else {
      setStatus(t('speech.shadow.ready'));
    }
  }

  async function startRecording() {
    if (recording || submitting || destroyed) return;
    const expected = refreshExpected();
    if (!expected) {
      setStatus(t('speech.shadow.noExpected'), 'error');
      return;
    }
    if (!info?.asr?.available) {
      setStatus(info?.asr?.notice || t('speech.shadow.unavailable'), 'error');
      return;
    }
    if (!mediaSupported()) {
      setStatus(t('speech.shadow.noMic'), 'error');
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      setStatus(describeMediaError(error), 'error');
      return;
    }
    const mime = pickMimeType();
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      stopStream();
      setStatus(t('speech.shadow.recordFailed'), 'error');
      return;
    }
    chunks = [];
    recorder.addEventListener('dataavailable', event => {
      if (event.data && event.data.size) chunks.push(event.data);
    });
    recorder.addEventListener('stop', () => {
      const blob = new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' });
      stopStream();
      if (destroyed) return;
      submitRecording(blob, expected).catch(() => {});
    }, { once: true });
    recorder.start();
    recording = true;
    recordingStartedAt = performance.now();
    renderControls();
    setStatus(t('speech.shadow.recording', { seconds: MAX_RECORD_MS / 1000 }));
    timer = setTimeout(() => {
      if (recording) stopRecording();
    }, MAX_RECORD_MS);
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch { /* 忽略 */ }
    }
    renderControls();
    setStatus(t('speech.shadow.checking'));
  }

  async function submitRecording(blob, expected) {
    submitting = true;
    renderControls();
    try {
      let wav;
      try {
        wav = await blobToWav16k(blob);
      } catch {
        throw new Error(t('speech.shadow.recordFailed'));
      }
      const form = new FormData();
      form.append('expected', expected);
      form.append('file', wav, 'shadowing.wav');
      const response = await fetch('/api/speech/shadowing/check', {
        method: 'POST', credentials: 'same-origin', body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || t('speech.shadow.failed'));
      if (destroyed) return;
      lastTranscript = data.transcript || '';
      renderResult(data);
      // 阶段 5-3：识别与比较成功即记一次；不记原文/转写内容。
      reportFeatureUse('speech', {
        source: 'shadowing',
        durationMs: recordingStartedAt ? performance.now() - recordingStartedAt : 0,
      });
    } catch (error) {
      setStatus(error?.message || t('speech.shadow.failed'), 'error');
      toast(error?.message || t('speech.shadow.failed'), true);
    } finally {
      submitting = false;
      renderControls();
    }
  }

  function renderResult(data) {
    const similar = data.similar === true;
    const transcript = String(data.transcript || '').trim();
    resultBox.replaceChildren(
      el('div', { class: `speech-shadow-verdict ${similar ? 'is-similar' : 'is-different'}`, role: 'status' },
        badge(similar ? t('speech.shadow.similar') : t('speech.shadow.different'), similar ? 'teal' : 'amber'),
        el('strong', {}, t('speech.shadow.score', { score: data.score ?? 0 }))),
      el('p', { class: 'speech-shadow-transcript' },
        transcript
          ? t('speech.shadow.transcript', { text: transcript })
          : t('speech.shadow.emptyTranscript')),
      el('p', { class: 'help' }, data.note || t('speech.shadow.note')),
    );
    setStatus(similar ? t('speech.shadow.pass') : t('speech.shadow.retryHint'),
      similar ? 'ready' : 'error');
    renderControls();
  }

  loadInfo().catch(() => {
    if (destroyed) return;
    host.removeAttribute('aria-busy');
    setStatus(t('speech.shadow.unavailable'), 'error');
  });

  const panel = {
    element: host,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stopStream();
      try { readButton.destroy(); } catch { /* 忽略 */ }
      host.remove();
      activePanels.delete(panel);
    },
  };
  activePanels.add(panel);
  return panel;
}
