/* 浏览器录音 → 16k 单声道 PCM16 WAV（离线语音识别 §4 的前置转换）。
 *
 * 服务端本地识别引擎只接受 16k、单声道、16-bit PCM WAV；MediaRecorder 录出的是
 * webm/opus 或 mp4，因此在前端用 WebAudio 解码后重采样，不引入后端音频解码依赖。
 *
 * 失败时抛出中文 Error，由调用方提示用户重试或改用键盘输入。
 */

const TARGET_SAMPLE_RATE = 16000;

function audioContextCtor() {
  if (typeof window === 'undefined') return null;
  return window.AudioContext || window.webkitAudioContext || null;
}

function offlineContextCtor() {
  if (typeof window === 'undefined') return null;
  return window.OfflineAudioContext || window.webkitOfflineAudioContext || null;
}

function writeAscii(view, offset, text) {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index));
  }
}

function encodePcm16(samples) {
  const pcm = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, Number(samples[index]) || 0));
    pcm[index] = value < 0 ? Math.round(value * 0x8000) : Math.round(value * 0x7fff);
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  writeAscii(view, 0, 'RIFF');
  view.setUint32(4, 36 + pcm.length * 2, true);
  writeAscii(view, 8, 'WAVE');
  writeAscii(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, TARGET_SAMPLE_RATE, true);
  view.setUint32(28, TARGET_SAMPLE_RATE * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeAscii(view, 36, 'data');
  view.setUint32(40, pcm.length * 2, true);
  for (let index = 0; index < pcm.length; index += 1) {
    view.setInt16(44 + index * 2, pcm[index], true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

function resampleLinear(channel, sourceRate, targetRate) {
  if (!channel || !channel.length) return new Float32Array(0);
  if (!sourceRate || sourceRate === targetRate) return channel;
  const ratio = sourceRate / targetRate;
  const length = Math.max(1, Math.ceil(channel.length / ratio));
  const output = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(channel.length - 1, left + 1);
    const fraction = position - left;
    output[index] = channel[left] * (1 - fraction) + channel[right] * fraction;
  }
  return output;
}

/**
 * 把任意浏览器可解码的录音 Blob 转成 16k 单声道 PCM16 WAV。
 *
 * @param {Blob} blob MediaRecorder 录制的 webm/ogg/mp4 音频
 * @param {{maxSeconds?: number}} [options] 超过该秒数时截断（与服务端识别上限对齐）
 * @returns {Promise<Blob>} `audio/wav`
 */
export async function blobToWav16k(blob, options = {}) {
  if (!blob || !blob.size) throw new Error('没有录到声音，请靠近麦克风再说一次');
  const requestedMax = Number(options && options.maxSeconds);
  const maxFrames = Number.isFinite(requestedMax) && requestedMax > 0
    ? Math.floor(requestedMax * TARGET_SAMPLE_RATE)
    : 0;
  const AudioCtor = audioContextCtor();
  if (!AudioCtor) throw new Error('当前浏览器不支持 WebAudio，无法转换录音格式');
  const raw = await blob.arrayBuffer();
  const context = new AudioCtor();
  let decoded;
  try {
    decoded = await context.decodeAudioData(raw.slice(0));
  } catch {
    throw new Error('录音无法解码，请重试或改用键盘输入');
  } finally {
    try { context.close(); } catch { /* ignore */ }
  }
  if (!decoded || !decoded.length || !decoded.numberOfChannels) {
    throw new Error('录音为空，请靠近麦克风再说一次');
  }
  const sourceRate = Number(decoded.sampleRate) || TARGET_SAMPLE_RATE;
  const OfflineCtor = offlineContextCtor();
  if (OfflineCtor) {
    try {
      const frames = Math.max(1, Math.ceil(decoded.duration * TARGET_SAMPLE_RATE));
      const offline = new OfflineCtor(1, frames, TARGET_SAMPLE_RATE);
      const source = offline.createBufferSource();
      source.buffer = decoded;
      source.connect(offline.destination);
      source.start(0);
      const rendered = await offline.startRendering();
      let samples = rendered.getChannelData(0);
      if (maxFrames > 0 && samples.length > maxFrames) samples = samples.slice(0, maxFrames);
      return encodePcm16(samples);
    } catch {
      /* 回退到线性重采样 */
    }
  }
  const channel = decoded.getChannelData(0).slice();
  let samples = resampleLinear(channel, sourceRate, TARGET_SAMPLE_RATE);
  if (maxFrames > 0 && samples.length > maxFrames) samples = samples.slice(0, maxFrames);
  return encodePcm16(samples);
}
