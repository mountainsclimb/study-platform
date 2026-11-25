/* 词汇听写练习（批次六后续）：本地 TTS 发音 + 浏览器录音 + 本地 ASR 评分。
 *
 * 流程：GET /api/vocab/dictation/next → 播放 data: audio/wav → MediaRecorder 录音
 * → blobToWav16k → POST /api/vocab/dictation/answer → 展示转写/得分/正确拼写。
 *
 * 边界：不把答案单词放进 next 响应；评分由服务端给出，前端不估算；录音只上传到
 * 本机知序服务；TTS/ASR 未启用时显示中文降级，不伪造题目或分数。
 */
import { el, button, badge, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { reportFeatureUse } from '../core/usage.js';
import { blobToWav16k } from './wavEncode.js';

const MAX_RECORD_MS = 30 * 1000;
const MAX_EXCLUDE = 200;

function ossText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function mimeCandidates() {
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
}

function pickMimeType() {
  const recorder = window.MediaRecorder;
  if (!recorder || typeof recorder.isTypeSupported !== 'function') return '';
  for (const type of mimeCandidates()) {
    try { if (recorder.isTypeSupported(type)) return type; } catch { /* 未知按下一个 */ }
  }
  return '';
}

function mediaSupported() {
  return Boolean(
    typeof window !== 'undefined'
    && window.MediaRecorder
    && navigator?.mediaDevices?.getUserMedia,
  );
}

/**
 * 在容器内创建听写练习；返回 `{element, destroy}`。
 * @param {{container?: HTMLElement}} options
 */
export function createDictationPractice(options = {}) {
  const host = el('div', { class: 'vb-dictation', 'aria-busy': 'true' });
  const statusLine = el('p', { class: 'help vb-dictation-status', role: 'status', 'aria-live': 'polite' });
  const meaningLine = el('p', { class: 'vb-dictation-meaning' });
  const phoneticLine = el('p', { class: 'vb-dictation-phonetic' });
  const resultBox = el('div', { class: 'vb-dictation-result', 'aria-live': 'polite' });
  const controls = el('div', { class: 'vb-dictation-controls' });
  const counterLine = el('p', { class: 'vb-dictation-counter' });
  const mount = el('div', { class: 'vb-dictation-main' });

  let destroyed = false;
  let status = null;
  let item = null;
  let answered = new Set();
  let audio = null;
  let recorder = null;
  let stream = null;
  let chunks = [];
  let recordTimer = null;
  let recording = false;
  let submitting = false;
  let questionNo = 0;
  let questionStartedAt = 0;

  host.append(
    el('div', { class: 'vb-dictation-head' },
      el('div', {},
        el('h1', { class: 'vb-dictation-title' }, ossText('vocab.dictation.title', '词汇听写')),
        el('p', { class: 'help' }, ossText(
          'vocab.dictation.sub',
          '听发音，写出你听到的单词；录音只上传到本机知序服务，由本地语音识别评分。',
        ))),
      badge(ossText('vocab.dictation.badge', '本地语音'), 'teal')),
    statusLine,
    mount,
    counterLine,
  );

  function setStatus(text, state = '') {
    statusLine.textContent = text;
    if (state) statusLine.dataset.state = state;
    else delete statusLine.dataset.state;
  }

  function teardownAudio() {
    if (audio) {
      try { audio.pause(); } catch { /* ignore */ }
      audio = null;
    }
  }

  function stopStream() {
    if (recordTimer) { clearTimeout(recordTimer); recordTimer = null; }
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch { /* ignore */ }
    }
    recorder = null;
    if (stream) {
      for (const track of stream.getTracks()) {
        try { track.stop(); } catch { /* ignore */ }
      }
      stream = null;
    }
    recording = false;
  }

  async function loadStatus() {
    host.setAttribute('aria-busy', 'true');
    try {
      status = await apiJson('/vocab/dictation/status');
    } catch (error) {
      status = { available: false, note: error?.message || '' };
    }
    host.removeAttribute('aria-busy');
    if (destroyed) return;
    if (!status.available) {
      mount.replaceChildren(
        el('p', { class: 'help vb-dictation-unavailable' },
          status.note || ossText('vocab.dictation.unavailable', '本地语音合成或识别不可用，听写练习暂不可用。')),
      );
      setStatus(ossText('vocab.dictation.unavailable', '听写练习暂不可用。'), 'error');
      return;
    }
    setStatus(ossText(
      'vocab.dictation.ready',
      '就绪：{tts} / {asr}',
      { tts: status.tts_model || '', asr: status.asr_model || '' },
    ), 'ready');
    mount.replaceChildren(renderStart());
  }

  function renderStart() {
    const start = button(ossText('vocab.dictation.start', '开始听写'), () => {
      start.disabled = true;
      nextQuestion().catch(error => {
        start.disabled = false;
        toast(error?.message || ossText('vocab.dictation.failed', '听写练习失败'), true);
      });
    }, '', 'play');
    return el('div', { class: 'vb-dictation-start' },
      el('p', {}, ossText(
        'vocab.dictation.hint',
        '将按到期顺序抽取你的词汇卡；每题先播放发音，再点击录音跟读/拼读。',
      )),
      start);
  }

  async function nextQuestion() {
    if (destroyed) return;
    questionNo += 1;
    teardownAudio();
    resultBox.replaceChildren();
    controlling(true);
    setStatus(ossText('vocab.dictation.loading', '正在准备下一题…'));
    try {
      const params = new URLSearchParams();
      if (answered.size) {
        params.set('exclude', [...answered].slice(-MAX_EXCLUDE).join(','));
      }
      const data = await apiJson(`/vocab/dictation/next?${params.toString()}`);
      if (destroyed) return;
      if (!data.available) throw new Error(data.note || ossText('vocab.dictation.unavailable', '听写练习暂不可用'));
      if (data.done) {
        item = null;
        mount.replaceChildren(el('p', { class: 'help vb-dictation-done' },
          data.note || ossText('vocab.dictation.done', '这批词汇已经听完；可回词表加入更多单词。')));
        counterLine.textContent = '';
        setStatus(ossText('vocab.dictation.finished', '本批听写完成。'), 'ready');
        return;
      }
      item = data;
      questionStartedAt = performance.now();
      showQuestion();
      playCurrent().catch(() => {});
    } finally {
      controlling(false);
    }
  }

  function showQuestion() {
    meaningLine.textContent = item.meaning
      ? ossText('vocab.dictation.meaning', '释义：{meaning}', { meaning: item.meaning })
      : '';
    phoneticLine.textContent = item.phonetic_hint
      ? ossText('vocab.dictation.phonetic', '发音提示：[{phonetic}]', { phonetic: item.phonetic_hint })
      : '';
    counterLine.textContent = ossText('vocab.dictation.counter', '第 {n} 题', { n: questionNo });
    mount.replaceChildren(el('div', { class: 'vb-dictation-question' },
      el('div', { class: 'vb-dictation-listen' },
        button(ossText('vocab.dictation.play', '播放发音'), () => playCurrent(), 'soft small', 'play'),
        meaningLine,
        phoneticLine),
      controls,
      resultBox));
    renderControls();
    setStatus(ossText('vocab.dictation.listening', '请听发音，然后点击「开始录音」。'));
  }

  function controlling(value) {
    submitting = value;
    renderControls();
  }

  function renderControls() {
    const nodes = [
      button(ossText('vocab.dictation.play', '播放发音'), () => playCurrent(), 'ghost small'),
      button(
        recording
          ? ossText('vocab.dictation.stop', '停止录音')
          : ossText('vocab.dictation.record', '开始录音'),
        () => (recording ? stopRecording() : startRecording()),
        recording ? 'soft small' : '',
        'message',
      ),
    ];
    if (item) nodes.push(button(ossText('vocab.dictation.next', '下一题'), () => nextQuestion(), 'ghost small'));
    controls.replaceChildren(...nodes);
    for (const node of controls.querySelectorAll('button')) {
      node.disabled = submitting;
      node.style.minHeight = '44px';
    }
  }

  async function playCurrent() {
    if (!item) return;
    teardownAudio();
    audio = new Audio(item.audio);
    try { await audio.play(); } catch { /* 浏览器自动播放限制：用户点击时已授权 */ }
  }

  async function startRecording() {
    if (!item || recording || submitting) return;
    if (!mediaSupported()) {
      setStatus(ossText('vocab.dictation.noMic', '当前浏览器不支持录音，请使用 Chrome/Edge 并授予麦克风权限。'), 'error');
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
      setStatus(ossText('vocab.dictation.recordFailed', '无法开始录音，请检查麦克风后重试。'), 'error');
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
      submitRecording(blob).catch(() => {});
    }, { once: true });
    recorder.start();
    recording = true;
    renderControls();
    setStatus(ossText('vocab.dictation.recording', '正在录音…最长 {seconds} 秒。', { seconds: MAX_RECORD_MS / 1000 }));
    recordTimer = setTimeout(() => {
      if (recording) stopRecording();
    }, MAX_RECORD_MS);
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    if (recordTimer) { clearTimeout(recordTimer); recordTimer = null; }
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch { /* ignore */ }
    }
    renderControls();
    setStatus(ossText('vocab.dictation.scoring', '正在识别并评分…'));
  }

  async function submitRecording(blob) {
    if (!item || destroyed) return;
    submitting = true;
    renderControls();
    try {
      let wav;
      try {
        wav = await blobToWav16k(blob);
      } catch (error) {
        throw new Error(error?.message || ossText('vocab.dictation.recordFailed', '录音转换失败，请重试。'));
      }
      const form = new FormData();
      form.append('card_id', String(item.card_id));
      form.append('file', wav, 'answer.wav');
      const response = await fetch('/api/vocab/dictation/answer', {
        method: 'POST', credentials: 'same-origin', body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || ossText('vocab.dictation.scoringFailed', '听写评分失败'));
      if (destroyed) return;
      answered.add(item.card_id);
      renderResult(data);
      // 阶段 5-3：评分成功即记一次；不记单词/转写内容。
      reportFeatureUse('speech', {
        source: 'dictation',
        durationMs: questionStartedAt ? performance.now() - questionStartedAt : 0,
      });
    } catch (error) {
      setStatus(error?.message || ossText('vocab.dictation.scoringFailed', '听写评分失败'), 'error');
      toast(error?.message || ossText('vocab.dictation.scoringFailed', '听写评分失败'), true);
    } finally {
      submitting = false;
      renderControls();
    }
  }

  function renderResult(data) {
    const ok = data.correct === true;
    const nodes = [
      el('div', { class: `vb-dictation-verdict ${ok ? 'is-correct' : 'is-wrong'}`, role: 'status' },
        badge(ok
          ? ossText('vocab.dictation.correct', '正确')
          : ossText('vocab.dictation.incorrect', '再听一次'), ok ? 'teal' : 'amber'),
        el('strong', {}, ossText('vocab.dictation.score', '{score} 分', { score: data.score })),
      ),
      el('p', { class: 'vb-dictation-transcript' },
        ossText('vocab.dictation.transcript', '识别结果：{text}', {
          text: data.transcript || ossText('vocab.dictation.emptyTranscript', '（未识别到文本）'),
        })),
      el('p', { class: 'vb-dictation-answer' },
        ossText('vocab.dictation.expected', '正确拼写：{word}', { word: data.expected })),
    ];
    if (data.translation) {
      nodes.push(el('p', { class: 'help' }, ossText('vocab.dictation.answerMeaning', '释义：{meaning}', { meaning: data.translation })));
    }
    if (data.note) nodes.push(el('p', { class: 'help' }, data.note));
    resultBox.replaceChildren(...nodes);
    setStatus(ok
      ? ossText('vocab.dictation.correctStatus', '本题正确。')
      : ossText('vocab.dictation.wrongStatus', '本题未通过，可播放发音后再试。'),
    ok ? 'ready' : 'error');
    renderControls();
  }

  function describeMediaError(error) {
    const name = String(error?.name || '');
    if (/NotAllowed|Permission|Security/i.test(name)) {
      return ossText('vocab.dictation.micDenied', '麦克风权限被拒绝，请在地址栏允许后重试。');
    }
    if (/NotFound|DevicesNotFound|Overconstrained/i.test(name)) {
      return ossText('vocab.dictation.noDevice', '没有找到可用的麦克风。');
    }
    return ossText('vocab.dictation.recordFailed', '无法开始录音，请检查麦克风后重试。');
  }

  loadStatus().catch(error => {
    if (!destroyed) {
      setStatus(error?.message || ossText('vocab.dictation.failed', '听写练习初始化失败'), 'error');
      host.removeAttribute('aria-busy');
    }
  });

  if (options.container) options.container.replaceChildren(host);

  return {
    element: host,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stopStream();
      teardownAudio();
      host.remove();
    },
  };
}

async function apiJson(path) {
  const response = await fetch(`/api${path}`, { credentials: 'same-origin' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（${response.status}）`);
  }
  return data;
}
