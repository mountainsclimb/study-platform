/* 统一朗读（阶段 4-2）：题目 / 笔记 / 词汇 / 解析面板共用。
 *
 * 路径与降级：
 * 1. 先请求 `POST /api/speech/tts`（本地或已配置的云端合成），拿到音频 Blob 后播放；
 * 2. 服务端没有可用合成（503）或请求失败时，回退浏览器 `speechSynthesis`；
 *    浏览器也不支持时明确提示不可用，不伪造播放状态。
 *
 * 约束：不自动播放；长文本按标点切成 ≤600 字分段，逐段合成/播放；路由切换或
 * 再次点击会立即停止并释放 Blob URL；朗读正文不写学习事件。
 */
import { el, icon, fill } from '../core/dom.js';
import { t } from '../core/i18n.js';

const SEGMENT_CHARS = 600;
const activePlayers = new Set();
let routeHookBound = false;

function bindRouteHook() {
  if (routeHookBound || typeof window === 'undefined') return;
  routeHookBound = true;
  // 路由切换（hash 变化）时停止所有朗读；同页重渲染由组件 destroy 负责。
  window.addEventListener('hashchange', () => stopAllReadAloud());
}

/** 停止当前页面所有朗读（路由切换/登出时调用）。 */
export function stopAllReadAloud() {
  for (const player of [...activePlayers]) {
    try { player.stop(); } catch { /* 单个组件失败不应阻塞其它组件 */ }
  }
}

/** 把 Markdown/LaTeX 文本转成适合朗读的纯文本（去掉标记，保留可读内容）。 */
export function plainTextForSpeech(value) {
  let text = String(value || '');
  text = text.replace(/```[\s\S]*?```/g, ' ');
  text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  text = text.replace(/\$\$?([^$]+)\$\$?/g, '$1');
  text = text.replace(/<[^>]+>/g, ' ');
  text = text.replace(/[#>*_`~|]/g, ' ');
  text = text.replace(/^\s*[-+]\s+/gm, '');
  return text.replace(/\s+/g, ' ').trim();
}

function splitText(text, max = SEGMENT_CHARS) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  if (clean.length <= max) return [clean];
  const parts = [];
  let rest = clean;
  while (rest.length > max) {
    let cut = -1;
    for (const mark of ['。', '！', '？', '；', '.', '!', '?', ';', '，', ',', ' ']) {
      cut = rest.lastIndexOf(mark, max);
      if (cut >= max * 0.4) { cut += mark === ' ' ? 0 : 1; break; }
      cut = -1;
    }
    if (cut <= 0) cut = max;
    const head = rest.slice(0, cut).trim();
    if (head) parts.push(head);
    rest = rest.slice(cut).trim();
  }
  if (rest) parts.push(rest);
  return parts;
}

function browserVoiceAvailable() {
  return typeof window !== 'undefined'
    && typeof window.speechSynthesis !== 'undefined'
    && typeof window.SpeechSynthesisUtterance === 'function';
}

/**
 * 创建「朗读 / 停止」按钮。
 * @param {{ getText?: () => string, text?: string, label?: string, className?: string }} options
 * @returns {{ node: HTMLElement, button: HTMLButtonElement, status: HTMLElement, stop: () => void, destroy: () => void }}
 */
export function createReadAloudButton(options = {}) {
  bindRouteHook();
  const label = options.label || t('speech.read');
  const status = el('span', { class: 'read-aloud-status', role: 'status', 'aria-live': 'polite' });
  const buttonNode = el('button', {
    type: 'button',
    class: `button ghost small read-aloud-button ${options.className || ''}`.trim(),
    'aria-pressed': 'false',
  });
  const node = el('span', { class: 'read-aloud' }, buttonNode, status);

  let destroyed = false;
  let phase = 'idle'; // idle | loading | playing
  let token = 0;
  let audio = null;
  let objectUrl = null;
  let utterance = null;

  function resolveText() {
    const raw = typeof options.getText === 'function' ? options.getText() : options.text;
    return plainTextForSpeech(raw);
  }

  function setStatus(text, tone = '') {
    status.textContent = text || '';
    if (tone) status.dataset.tone = tone;
    else delete status.dataset.tone;
  }

  function renderButton() {
    if (destroyed) return;
    const playing = phase === 'playing';
    const loading = phase === 'loading';
    fill(buttonNode,
      icon(playing ? 'stop' : 'play'),
      el('span', { class: 'read-aloud-label' },
        loading ? t('speech.loading') : (playing ? t('speech.stop') : label)));
    buttonNode.setAttribute('aria-pressed', playing ? 'true' : 'false');
    buttonNode.setAttribute('aria-busy', loading ? 'true' : 'false');
    buttonNode.classList.toggle('is-playing', playing);
    buttonNode.classList.toggle('is-loading', loading);
    buttonNode.disabled = destroyed;
  }

  function releaseAudio() {
    if (audio) {
      try { audio.pause(); } catch { /* 忽略 */ }
      audio = null;
    }
    if (objectUrl) {
      try { URL.revokeObjectURL(objectUrl); } catch { /* 忽略 */ }
      objectUrl = null;
    }
  }

  function stopBrowser() {
    if (!browserVoiceAvailable()) return;
    try { window.speechSynthesis.cancel(); } catch { /* 忽略 */ }
    utterance = null;
  }

  function stop() {
    token += 1;
    releaseAudio();
    stopBrowser();
    if (destroyed) return;
    phase = 'idle';
    setStatus('');
    renderButton();
  }

  function playBrowser(text, reason = '') {
    if (!browserVoiceAvailable()) {
      phase = 'idle';
      renderButton();
      setStatus(t('speech.unavailable'), 'error');
      return;
    }
    stopBrowser();
    utterance = new window.SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    utterance.rate = 1;
    utterance.onend = () => {
      if (token !== myToken) return;
      utterance = null;
      phase = 'idle';
      setStatus('');
      renderButton();
    };
    utterance.onerror = () => {
      if (token !== myToken) return;
      utterance = null;
      phase = 'idle';
      setStatus(t('speech.failed'), 'error');
      renderButton();
    };
    const myToken = token;
    phase = 'playing';
    renderButton();
    setStatus(reason ? t('speech.browserFallback', { reason }) : t('speech.browserPlaying'), reason ? 'info' : '');
    try { window.speechSynthesis.speak(utterance); } catch { /* 同 onerror */ }
  }

  function playBlob(blob, myToken) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      objectUrl = url;
      audio = new Audio(url);
      let settled = false;
      const finish = (error) => {
        if (settled) return;
        settled = true;
        if (error) reject(error);
        else resolve();
      };
      audio.addEventListener('ended', () => finish(null), { once: true });
      audio.addEventListener('error', () => finish(new Error(t('speech.playFailed'))), { once: true });
      audio.play().then(() => {
        if (myToken !== token) { finish(null); return; }
        phase = 'playing';
        renderButton();
        setStatus(t('speech.playing'));
      }).catch(finish);
    });
  }

  async function requestSegment(segment, myToken) {
    const response = await fetch('/api/speech/tts', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: segment }),
    });
    if (myToken !== token) return { cancelled: true };
    if (response.status === 503) return { fallback: true, reason: '' };
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || t('speech.failed'));
    }
    return { blob: await response.blob() };
  }

  async function start() {
    const text = resolveText();
    if (!text) {
      setStatus(t('speech.empty'), 'error');
      return;
    }
    token += 1;
    const myToken = token;
    releaseAudio();
    stopBrowser();
    phase = 'loading';
    renderButton();
    setStatus(t('speech.loading'));
    const segments = splitText(text);
    try {
      for (let index = 0; index < segments.length; index += 1) {
        if (myToken !== token) return;
        const result = await requestSegment(segments[index], myToken);
        if (result.cancelled) return;
        if (result.fallback) {
          playBrowser(segments.slice(index).join(' '), t('speech.localUnavailable'));
          return;
        }
        await playBlob(result.blob, myToken);
        releaseAudio();
      }
      if (myToken !== token) return;
      phase = 'idle';
      setStatus('');
      renderButton();
    } catch (error) {
      if (myToken !== token) return;
      releaseAudio();
      // 服务端不可用/合成失败都回退浏览器；浏览器也不可用时给出明确错误。
      if (browserVoiceAvailable()) {
        playBrowser(segments.join(' '), error?.message || '');
        return;
      }
      phase = 'idle';
      setStatus(error?.message || t('speech.unavailable'), 'error');
      renderButton();
    }
  }

  buttonNode.addEventListener('click', () => {
    if (phase === 'idle') start();
    else stop();
  });
  renderButton();
  if (options.container) options.container.append(node);
  const player = { stop };
  activePlayers.add(player);

  return {
    node,
    button: buttonNode,
    status,
    stop,
    destroy() {
      if (destroyed) return;
      stop();
      destroyed = true;
      activePlayers.delete(player);
      node.remove();
    },
  };
}
