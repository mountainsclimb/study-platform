/* 语音输入（ASR，OpenMAIC 语音提问 / 小纸语音输入的原生化）：
 *
 * 两条路径（自动选择，可用 localStorage `zhixu.voice-asr` 或 `window.__ZHIXU_VOICE_ASR__`
 * 覆盖为 `browser` / `server`，便于验收与排障）：
 * 1. **浏览器原生识别**（`SpeechRecognition` / `webkitSpeechRecognition`，zh-CN）：
 *    不录音上传、不发网络请求，最终文本填入目标输入框（光标处插入）；
 * 2. **服务端识别兜底**（浏览器无原生识别时）：`MediaRecorder` 录 webm/opus（≤60s），
 *    停止后 `POST /api/classroom/asr`（multipart file）转写；识别文本同样只填入输入框。
 *
 * 共性约束：**不自动发送**；识别前 `GET /api/classroom/asr/info` 预检服务端可用性；
 * 权限拒绝 / 无麦克风 / 服务不可用 / 超过 60s 或 25MB 全部给中文提示；
 * 不支持任何识别方式时按钮禁用并常驻说明；组件不依赖框架，CSS 类名前缀由调用方传入。
 */
import { el } from '../core/dom.js';
import { api } from '../core/api.js';

const BROWSER_ERROR_MESSAGES = {
  'not-allowed': '麦克风权限被拒绝：请在浏览器地址栏允许本页使用麦克风后重试。',
  'service-not-allowed': '浏览器/系统禁用了语音识别服务，请检查系统隐私与浏览器设置。',
  'no-speech': '没有听到声音，请靠近麦克风再说一次。',
  'audio-capture': '没有找到可用的麦克风，请检查设备连接。',
  'network': '语音识别服务网络不可用（浏览器识别需要网络）。',
  'language-not-supported': '当前浏览器不支持中文（zh-CN）语音识别。',
  'bad-grammar': '语音识别语法不受支持，请改用键盘输入。',
  'aborted': '',
};

const SUPPORTED_HINT = '支持语音输入（浏览器识别，录音不会上传到知序）';
const SERVER_HINT = '当前浏览器不支持原生语音识别，已切换到服务端转写（录音仅上传到本机知序服务）';
const UNSUPPORTED_HINT = '当前浏览器不支持语音输入，可继续用键盘输入。';
const SERVER_UNAVAILABLE_HINT = '当前浏览器不支持语音识别，服务端语音识别也不可用：'
  + '请在「模型设置」添加支持语音识别的连接（如小米 MiMo 的 mimo-v2.5-asr）后重试，或继续用键盘输入。';

//: 服务端 ASR 录音上限：60 秒（音频文件上限与服务端一致：25MB）。
const MAX_RECORD_MS = 60 * 1000;
const MAX_ASR_BYTES = 25 * 1024 * 1024;
const TIMER_TICK_MS = 200;
const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

const OVERRIDE_KEY = 'zhixu.voice-asr';

function readOverride() {
  try {
    const fromWindow = typeof window !== 'undefined' ? window.__ZHIXU_VOICE_ASR__ : '';
    const fromStorage = typeof localStorage !== 'undefined' ? localStorage.getItem(OVERRIDE_KEY) : '';
    return String(fromWindow || fromStorage || '').trim().toLowerCase();
  } catch {
    return '';
  }
}

function mediaRecorderSupported() {
  if (typeof window === 'undefined') return false;
  const recorder = window.MediaRecorder;
  const mediaDevices = navigator && navigator.mediaDevices;
  return typeof recorder === 'function' && Boolean(mediaDevices && typeof mediaDevices.getUserMedia === 'function');
}

function pickMimeType() {
  const recorder = typeof window !== 'undefined' ? window.MediaRecorder : null;
  if (!recorder || typeof recorder.isTypeSupported !== 'function') return '';
  for (const type of MIME_CANDIDATES) {
    try {
      if (recorder.isTypeSupported(type)) return type;
    } catch {
      /* 某些浏览器 isTypeSupported 会抛错：按未知处理 */
    }
  }
  return '';
}

function extensionForMime(mime) {
  const value = String(mime || '').toLowerCase();
  if (value.includes('ogg')) return 'ogg';
  if (value.includes('mp4') || value.includes('m4a') || value.includes('aac')) return 'm4a';
  if (value.includes('wav')) return 'wav';
  return 'webm';
}

function insertionText(value, text) {
  const source = String(value || '');
  const needsSpace = Boolean(source) && !/\s$/.test(source) && !/^[，。！？、,.!?；;：:]/.test(text);
  return (needsSpace ? ' ' : '') + text;
}

function describeMediaError(error) {
  const name = String(error && error.name || '');
  const message = String(error && error.message || '');
  if (/NotAllowedError|PermissionDeniedError|SecurityError/i.test(name) || /permission|denied/i.test(message)) {
    return '麦克风权限被拒绝：请在浏览器地址栏允许本页使用麦克风后重试。';
  }
  if (/NotFoundError|DevicesNotFoundError|OverconstrainedError/i.test(name)) {
    return '没有找到可用的麦克风，请检查设备连接后重试。';
  }
  if (/NotReadableError|TrackStartError|AbortError/i.test(name)) {
    return '麦克风被其它程序占用或暂时不可用，请关闭占用后重试。';
  }
  return '无法开始录音：请检查麦克风权限与设备后重试。';
}

function describeAsrError(error) {
  const message = String(error && error.message || error || '').trim();
  if (!message) return '语音识别失败，请重试或改用键盘输入。';
  if (/权限|not-allowed|permission/i.test(message)) return BROWSER_ERROR_MESSAGES['not-allowed'];
  if (/没有找到|设备|NotFound/i.test(message)) return BROWSER_ERROR_MESSAGES['audio-capture'];
  if (/25MB|过大|413/.test(message)) return '录音文件超过 25MB，请缩短录音后重试。';
  if (/网络连接失败|Failed to fetch|NetworkError/i.test(message)) {
    return '语音识别服务暂不可用：请确认本机服务正常后重试。';
  }
  if (/语音识别/.test(message)) return message;
  return `语音转写失败：${message}`;
}

export function createVoiceInput({
  target,
  prefix = 'voice',
  label = '语音输入',
  lang = 'zh-CN',
  maxLength = 0,
} = {}) {
  if (!target || typeof target !== 'object' || !('value' in target)) {
    throw new Error('语音输入需要一个文本输入框作为 target');
  }
  const Recognition = typeof window !== 'undefined'
    ? (window.SpeechRecognition || window.webkitSpeechRecognition)
    : null;
  const recognitionSupported = typeof Recognition === 'function';
  const recorderSupported = mediaRecorderSupported();
  const override = readOverride();
  // 路径选择：显式覆盖优先；默认原生识别优先，缺失时用服务端转写兜底。
  let mode = 'off';
  if (override === 'server' && recorderSupported) mode = 'server';
  else if (override === 'browser' && recognitionSupported) mode = 'browser';
  else if (recognitionSupported) mode = 'browser';
  else if (recorderSupported) mode = 'server';
  const className = suffix => `${prefix}-voice-${suffix}`;

  const waveNode = el('span', {
    class: 'voice-input-wave', 'aria-hidden': 'true', hidden: true,
  });
  for (let index = 0; index < 5; index += 1) {
    waveNode.append(el('span', { class: 'voice-input-wave-bar' }));
  }
  const timerNode = el('span', { class: 'voice-input-timer', hidden: true }, '0.0s');
  const buttonNode = el('button', {
    type: 'button',
    class: `${className('btn')} voice-input-btn`,
    'aria-label': label,
    'aria-pressed': 'false',
    title: mode === 'off' ? UNSUPPORTED_HINT : (mode === 'server' ? SERVER_HINT : SUPPORTED_HINT),
    disabled: mode === 'off',
  });
  buttonNode.append(
    el('span', { class: `${className('icon')} voice-input-icon`, 'aria-hidden': 'true' }, '🎤'),
    el('span', { class: `${className('label')} voice-input-label` }, '语音'),
    waveNode,
    timerNode,
  );

  const hintNode = el('p', {
    class: `${className('hint')} voice-input-hint`,
    role: 'status',
    'aria-live': 'polite',
  });

  let recognition = null;
  let recorder = null;
  let mediaStream = null;
  let chunks = [];
  let listening = false;
  let recording = false;
  let starting = false;
  let busyTranscribing = false;
  let cancelledRecording = false;
  let finalInserted = '';
  let destroyed = false;
  let resetTimer = null;
  let watchdog = null;
  let recordTimer = null;
  let recordStartedAt = 0;
  let errorText = '';
  // 服务端识别预检状态：checking（检查中）/ ready / unavailable。
  let asrState = mode === 'server' ? 'checking' : 'idle';
  let asrInfo = null;

  function setHint(text, tone = '') {
    hintNode.textContent = text || '';
    hintNode.hidden = !text;
    hintNode.classList.toggle('is-error', tone === 'error');
    hintNode.classList.toggle('is-active', tone === 'active');
  }

  function idleHint() {
    if (mode === 'server') {
      if (asrState === 'checking') return '正在检查服务端语音识别能力…';
      if (asrState === 'unavailable') return SERVER_UNAVAILABLE_HINT;
      const model = asrInfo && asrInfo.model ? `（${asrInfo.model}）` : '';
      return `服务端语音转写${model}：录音最长 60 秒，识别结果只填入输入框，不会自动发送。`;
    }
    if (mode === 'browser') return '';
    return UNSUPPORTED_HINT;
  }

  function canUse() {
    if (mode === 'browser') return recognitionSupported;
    if (mode === 'server') return recorderSupported && asrState === 'ready';
    return false;
  }

  function syncButton() {
    const active = listening || recording;
    buttonNode.disabled = destroyed || starting || busyTranscribing || !canUse() || (mode === 'server' && asrState === 'checking' && !active);
    buttonNode.setAttribute('aria-pressed', active ? 'true' : 'false');
    buttonNode.classList.toggle('is-listening', active);
    buttonNode.classList.toggle('is-recording', recording);
    const labelNode = buttonNode.querySelector('.voice-input-label');
    if (labelNode) labelNode.textContent = busyTranscribing ? '识别中' : (active ? '停止' : '语音');
    waveNode.hidden = !recording;
    timerNode.hidden = !recording && !busyTranscribing;
    buttonNode.title = !canUse()
      ? (mode === 'server' ? SERVER_UNAVAILABLE_HINT : UNSUPPORTED_HINT)
      : (active ? '点击停止语音输入' : (mode === 'server' ? SERVER_HINT : SUPPORTED_HINT));
  }

  function clearResetTimer() {
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
  }

  function insertFinalText(text) {
    const value = String(text || '').trim();
    if (!value) return false;
    const source = String(target.value || '');
    let start = typeof target.selectionStart === 'number' ? target.selectionStart : source.length;
    let end = typeof target.selectionEnd === 'number' ? target.selectionEnd : start;
    start = Math.min(Math.max(0, start), source.length);
    end = Math.min(Math.max(start, end), source.length);
    const prefixText = insertionText(source.slice(0, start), value);
    let next = source.slice(0, start) + prefixText + source.slice(end);
    const limit = Number(target.maxLength) > 0 ? Number(target.maxLength) : Number(maxLength) || 0;
    if (limit && next.length > limit) next = next.slice(0, limit);
    target.value = next;
    const position = Math.min(next.length, start + prefixText.length);
    try {
      target.setSelectionRange(position, position);
    } catch {
      /* 部分输入框不支持选区 */
    }
    target.dispatchEvent(new Event('input', { bubbles: true }));
    try { target.focus(); } catch { /* 忽略 */ }
    finalInserted = value;
    return true;
  }

  function afterTextInserted() {
    errorText = '';
    setHint('已填入输入框，可编辑后发送。', 'active');
    clearResetTimer();
    resetTimer = setTimeout(() => {
      if (!listening && !recording) setHint(idleHint());
    }, 2600);
  }

  // -------------------------------------------------------------------------
  // 路径 1：浏览器原生 SpeechRecognition（不上传录音）
  // -------------------------------------------------------------------------
  function handleResult(event) {
    let interim = '';
    const results = event && event.results ? event.results : [];
    for (let index = event.resultIndex || 0; index < results.length; index += 1) {
      const result = results[index];
      if (!result) continue;
      const transcript = String(result[0]?.transcript || '').trim();
      if (!transcript) continue;
      if (result.isFinal) {
        errorText = '';
        insertFinalText(transcript);
      } else {
        interim += transcript;
      }
    }
    if (interim) setHint(`正在聆听：${interim}`, 'active');
  }

  function handleBrowserError(event) {
    const code = String(event?.error || 'unknown');
    if (code === 'aborted') return;
    errorText = BROWSER_ERROR_MESSAGES[code] || `语音识别失败（${code}），请重试或改用键盘输入。`;
    listening = false;
    syncButton();
    setHint(errorText, 'error');
  }

  function finishBrowserListening() {
    listening = false;
    syncButton();
    if (errorText) setHint(errorText, 'error');
    else if (finalInserted) afterTextInserted();
    else setHint(idleHint());
  }

  function startBrowser() {
    if (destroyed || !recognitionSupported || listening) return;
    clearResetTimer();
    errorText = '';
    finalInserted = '';
    try {
      if (!recognition) {
        recognition = new Recognition();
        recognition.lang = lang;
        recognition.continuous = false;
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;
        recognition.onstart = () => {
          listening = true;
          syncButton();
          setHint('正在聆听…再次点击麦克风可停止。', 'active');
        };
        recognition.onresult = handleResult;
        recognition.onerror = handleBrowserError;
        recognition.onend = () => {
          if (listening) finishBrowserListening();
          else syncButton();
        };
      }
      listening = true;
      syncButton();
      setHint('正在启动语音识别…', 'active');
      recognition.start();
    } catch {
      listening = false;
      syncButton();
      setHint('无法启动语音识别：请检查麦克风权限后重试。', 'error');
    }
  }

  function stopBrowser() {
    if (!recognition || !listening) return;
    try {
      recognition.stop();
    } catch {
      finishBrowserListening();
    }
  }

  // -------------------------------------------------------------------------
  // 路径 2：服务端 ASR 兜底（MediaRecorder 录音 → POST /api/classroom/asr）
  // -------------------------------------------------------------------------
  function stopMediaTracks() {
    if (mediaStream) {
      try { mediaStream.getTracks().forEach(track => track.stop()); } catch { /* 忽略 */ }
      mediaStream = null;
    }
  }

  function stopRecordTicker() {
    if (recordTimer) {
      clearInterval(recordTimer);
      recordTimer = null;
    }
  }

  function recordingSeconds() {
    return Math.min(MAX_RECORD_MS, Date.now() - recordStartedAt) / 1000;
  }

  function tickRecording() {
    if (!recording) return;
    const seconds = recordingSeconds();
    timerNode.textContent = `${seconds.toFixed(1)}s`;
    setHint(`正在录音 ${seconds.toFixed(1)}s / 60s：再次点击停止，识别文本只填入输入框。`, 'active');
    if (Date.now() - recordStartedAt >= MAX_RECORD_MS) {
      setHint('已到 60 秒上限，正在识别…', 'active');
      stopServerRecording({ autoLimit: true });
    }
  }

  function resetRecordingState() {
    recording = false;
    chunks = [];
    stopRecordTicker();
    stopMediaTracks();
    syncButton();
  }

  async function transcribe(blob) {
    if (!blob || !blob.size) {
      setHint('没有录到声音，请靠近麦克风再说一次。', 'error');
      return;
    }
    if (blob.size > MAX_ASR_BYTES) {
      setHint('录音文件超过 25MB，请缩短录音后重试。', 'error');
      return;
    }
    busyTranscribing = true;
    syncButton();
    setHint('正在识别语音，请稍候…', 'active');
    const extension = extensionForMime(blob.type);
    const form = new FormData();
    form.append('file', blob, `voice-${Date.now()}.${extension}`);
    try {
      const result = await api('/classroom/asr', { method: 'POST', body: form });
      const text = String(result && result.text || '').trim();
      if (!text) {
        setHint('没有识别到文字，请靠近麦克风、放慢语速后重试。', 'error');
        return;
      }
      insertFinalText(text);
      afterTextInserted();
    } catch (error) {
      errorText = describeAsrError(error);
      setHint(errorText, 'error');
    } finally {
      busyTranscribing = false;
      syncButton();
    }
  }

  async function startServerRecording() {
    if (destroyed || listening || recording || starting || busyTranscribing || !canUse()) return;
    clearResetTimer();
    errorText = '';
    finalInserted = '';
    if (!asrState || asrState === 'checking') {
      setHint('正在检查服务端语音识别能力…', 'active');
      return;
    }
    if (asrState === 'unavailable') {
      setHint(SERVER_UNAVAILABLE_HINT, 'error');
      return;
    }
    starting = true;
    syncButton();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      starting = false;
      syncButton();
      errorText = describeMediaError(error);
      setHint(errorText, 'error');
      return;
    }
    starting = false;
    if (destroyed) {
      try { stream.getTracks().forEach(track => track.stop()); } catch { /* 忽略 */ }
      syncButton();
      return;
    }
    mediaStream = stream;
    const mimeType = pickMimeType();
    try {
      recorder = mimeType
        ? new window.MediaRecorder(stream, { mimeType })
        : new window.MediaRecorder(stream);
    } catch (error) {
      resetRecordingState();
      errorText = describeMediaError(error);
      setHint(errorText, 'error');
      return;
    }
    chunks = [];
    cancelledRecording = false;
    recorder.ondataavailable = event => {
      if (event && event.data && event.data.size) chunks.push(event.data);
    };
    recorder.onerror = event => {
      errorText = describeMediaError(event && event.error);
      cancelledRecording = true;
      try { recorder.stop(); } catch { /* 忽略 */ }
    };
    recorder.onstop = () => {
      const type = (recorder && recorder.mimeType) || mimeType || 'audio/webm';
      const blob = new Blob(chunks, { type });
      const shouldUpload = !cancelledRecording && !destroyed;
      resetRecordingState();
      recorder = null;
      if (shouldUpload) {
        transcribe(blob).catch(() => { /* transcribe 内部已给出中文提示 */ });
      } else if (!destroyed && errorText) {
        setHint(errorText, 'error');
      }
    };
    try {
      recorder.start(250);
    } catch (error) {
      resetRecordingState();
      errorText = describeMediaError(error);
      setHint(errorText, 'error');
      return;
    }
    recording = true;
    recordStartedAt = Date.now();
    stopRecordTicker();
    recordTimer = setInterval(tickRecording, TIMER_TICK_MS);
    tickRecording();
    syncButton();
  }

  function stopServerRecording({ autoLimit = false } = {}) {
    if (!recording || !recorder) return;
    if (autoLimit) {
      setHint('已到 60 秒上限，正在识别…', 'active');
    } else {
      setHint('正在结束录音并准备识别…', 'active');
    }
    try {
      recorder.stop();
    } catch {
      cancelledRecording = true;
      resetRecordingState();
      if (!autoLimit) setHint('录音结束失败，请重试。', 'error');
    }
  }

  async function preflightServerAsr() {
    if (mode !== 'server') return;
    asrState = 'checking';
    syncButton();
    setHint(idleHint());
    try {
      const info = await api('/classroom/asr/info');
      asrInfo = info && typeof info === 'object' ? info : null;
      asrState = info && info.available ? 'ready' : 'unavailable';
    } catch {
      asrInfo = null;
      asrState = 'unavailable';
    }
    if (destroyed) return;
    syncButton();
    setHint(idleHint());
  }

  // -------------------------------------------------------------------------
  // 交互 / 生命周期
  // -------------------------------------------------------------------------
  function start() {
    if (destroyed || busyTranscribing) return;
    if (mode === 'server') startServerRecording();
    else startBrowser();
  }

  function stop() {
    if (mode === 'server') stopServerRecording();
    else stopBrowser();
  }

  function cancelActiveRecording() {
    if (!recording || !recorder) return;
    cancelledRecording = true;
    try { recorder.stop(); } catch { /* 忽略 */ }
    resetRecordingState();
    recorder = null;
    syncButton();
  }

  buttonNode.addEventListener('click', () => {
    if (busyTranscribing) return;
    if (listening || recording) stop();
    else start();
  });

  // 页面切走/组件被移除后自动清理（避免麦克风/识别会话在后台残留）。
  watchdog = setInterval(() => {
    if (destroyed) return;
    if (!buttonNode.isConnected) destroy();
  }, 2500);
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('hashchange', function onHashChange() {
      if (!destroyed && !buttonNode.isConnected) destroy();
    });
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearResetTimer();
    stopRecordTicker();
    if (watchdog) {
      clearInterval(watchdog);
      watchdog = null;
    }
    try { recognition?.abort?.(); } catch { /* 忽略 */ }
    recognition = null;
    cancelActiveRecording();
    listening = false;
    if (buttonNode.isConnected) buttonNode.remove();
    if (hintNode.isConnected) hintNode.remove();
  }

  syncButton();
  setHint(idleHint());
  if (mode === 'server') {
    preflightServerAsr().catch(() => { /* 预检失败按不可用处理，错误已在 setHint */ });
  }

  return {
    node: buttonNode,
    hintNode,
    get mode() { return mode; },
    get supported() { return mode !== 'off'; },
    isListening: () => listening || recording,
    start,
    stop,
    destroy,
  };
}

export const VOICE_SUPPORTED = typeof window !== 'undefined'
  && typeof (window.SpeechRecognition || window.webkitSpeechRecognition) === 'function';
