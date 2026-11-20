import { api } from './api.js';
import { state, onSessionReset } from './state.js';
import { el, badge, button, notice, busy, toast, dateText, icon } from './dom.js';
import { markdown } from './markdown.js';
import { renderAiResult } from '../components/aiResultCard.js';

export const taskNames = {
  explain: 'AI 详细解析',
  grade: '作答交叉核验',
  'question-analysis': '题目与题型分析',
  notes: '知识清单整理',
  recognize: '拍题视觉识别',
  tagging: '知识点批量打标',
  ocr: '扫描件 OCR 识别',
};

export const statusNames = {
  queued: '等待处理',
  pending: '等待处理',
  running: '正在处理',
  cancel_requested: '取消中',
  awaiting_outline: '待确认提纲',
  completed: '已完成',
  succeeded: '已完成',
  failed: '未完成',
  interrupted: '已中断',
  cancelled: '已取消',
};

const stageNames = {
  queued: '等待处理',
  starting: '准备中',
  preparing: '准备资料',
  extract: '解析资料',
  extracting: '解析资料',
  'reduce-chunks': '合并并去重知识条目',
  outline: '生成提纲',
  outlining: '生成提纲',
  awaiting_outline: '等待确认提纲',
  generate: '整理内容',
  generating: '整理内容',
  'generate-sections': '分节并行生成',
  verify: '独立核验',
  verifying: '独立核验',
  validate: '核对引用',
  'validate-citations': '核对来源引用',
  resuming: '从检查点续跑',
  completed: '已完成',
  done: '已完成',
  interrupted: '处理已中断',
  cancelled: '已取消',
  cancel_requested: '等待执行进程停止',
  failed: '处理未完成',
  ocr: '扫描页文字识别',
  'ocr-preparing': '准备扫描件',
  'ocr-saving': '保存识别结果',
  tagging: '批量打标',
};

export function stageLabel(stage) {
  const s = String(stage || '');
  const read = s.match(/^read-batch-(\d+)-of-(\d+)$/);
  if (read) return `阅读资料 · 第 ${read[1]} / ${read[2]} 批`;
  const mapChunk = s.match(/^map-chunk-(\d+)-of-(\d+)$/);
  if (mapChunk) return `抽取知识条目 · 第 ${mapChunk[1]} / ${mapChunk[2]} 块`;
  const verify = s.match(/^verify-batch-(\d+)-of-(\d+)$/);
  if (verify) return `核对引用 · 第 ${verify[1]} / ${verify[2]} 批`;
  const section = s.match(/^generate-sections-(\d+)-of-(\d+)$/);
  if (section) return `分节生成 · 第 ${section[1]} / ${section[2]} 节`;
  const tag = s.match(/^tagging-(\d+)\/(\d+)$/);
  if (tag) return `批量打标 · ${tag[1]} / ${tag[2]} 题`;
  const ocr = s.match(/^ocr-(\d+)\/(\d+)$/);
  if (ocr) return `识别扫描页 · 第 ${ocr[1]} / ${ocr[2]} 页`;
  const batch = s.match(/^([a-z][a-z0-9-]*)-(\d+)-of-(\d+)$/i);
  if (batch) return `${stageNames[batch[1]] || '处理中'} · 第 ${batch[2]} / ${batch[3]} 步`;
  const slash = s.match(/^([a-z][a-z0-9-]*)-(\d+)\/(\d+)$/i);
  if (slash) return `${stageNames[slash[1]] || '处理中'} · ${slash[2]} / ${slash[3]}`;
  return stageNames[s] || '正在处理';
}

/**
 * 从阶段文本解析确定进度：`read-batch-2-of-5`、`tagging-25/50`、`ocr-3/10` 等。
 * 返回 {current, total, percent} 或 null（无法确定进度时保持不确定态进度条）。
 */
export function parseStageProgress(stage) {
  const text = String(stage || '').trim();
  if (!text) return null;
  let match = text.match(/^[a-z][a-z0-9-]*-(\d+)-of-(\d+)$/i);
  if (!match) match = text.match(/^[a-z][a-z0-9-]*-(\d+)\/(\d+)$/i);
  if (!match) return null;
  const current = Number(match[1]);
  const total = Number(match[2]);
  if (!Number.isFinite(current) || !Number.isFinite(total) || total <= 0 || current < 0) return null;
  const safeCurrent = Math.min(current, total);
  return {
    current: safeCurrent,
    total,
    percent: Math.max(0, Math.min(100, Math.round((safeCurrent / total) * 100))),
  };
}

/** 已耗时文本：mm:ss（超过 1 小时为 h:mm:ss）。 */
export function elapsedText(ms) {
  const safe = Math.max(0, Number(ms) || 0);
  const totalSeconds = Math.floor(safe / 1000);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  const pad = value => String(value).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

export function statusBadgeVariant(status) {
  if (['completed', 'succeeded'].includes(status)) return 'teal';
  if (['failed', 'interrupted', 'cancelled'].includes(status)) return 'red';
  if (status === 'awaiting_outline' || status === 'cancel_requested') return 'amber';
  return 'blue';
}

const TERMINAL = new Set(['completed', 'succeeded', 'failed', 'interrupted', 'cancelled', 'awaiting_outline']);

// 流式期间正文与阶段文本的最小刷新间隔：避免每个 delta 都重建整段 Markdown。
const STREAM_PAINT_MS = 150;
// SSE 偶发瞬断后的有限重连次数，仍失败才退回轮询。
const STREAM_MAX_RETRIES = 2;

/* ------------------------------------------------------------------------- *
 * 后台运行中任务胶囊：路由切走后任务仍在服务端继续，顶栏保留一个轻量入口，
 * 点击回到发起任务时的页面；非流式（已切页/断线）时用 4s 轻量轮询刷新状态。
 * ------------------------------------------------------------------------- */
const jobPills = new Map();
let pillNode = null;
let pillPollTimer = null;

function isJobStreaming(jobId) {
  const key = String(jobId);
  for (const source of state.activeEventSources) {
    if (source && String(source.__zhixuJobId || '') === key) return true;
  }
  return false;
}

function jobPillRoute() {
  const route = location.hash || '#/';
  return route.startsWith('#/') ? route : '#/';
}

function syncJobPill() {
  if (!state.user || !jobPills.size) {
    if (pillNode) { pillNode.remove(); pillNode = null; }
    return;
  }
  const topbar = document.querySelector('.main > .topbar');
  const host = topbar ? (topbar.querySelector('.topbar-right') || topbar) : null;
  if (!host) return;
  if (!pillNode) {
    pillNode = el('button', {
      type: 'button',
      class: 'topbar-pill job-pill',
      'data-job-pill': 'true',
      title: '回到后台运行中的 AI 任务',
      onClick: () => {
        const first = [...jobPills.values()][0];
        const target = (first && first.route) || '#/';
        if ((location.hash || '#/') === target) return;
        location.hash = target;
      },
    }, icon('clock'), el('span', { class: 'job-pill-text' }, '后台任务运行中'));
  }
  if (!pillNode.isConnected) host.append(pillNode);
  const items = [...jobPills.values()];
  const text = items.length > 1
    ? `${items.length} 个任务运行中`
    : `${items[0].label}运行中`;
  const textNode = pillNode.querySelector('.job-pill-text');
  if (textNode) textNode.textContent = text;
  pillNode.setAttribute('aria-label', `${text}，点击查看`);
}

async function pollBackgroundJobs() {
  if (!state.user || !jobPills.size) { schedulePillPoll(); return; }
  for (const [jobId, info] of [...jobPills]) {
    if (isJobStreaming(jobId)) continue;
    try {
      const job = await api(`/ai/jobs/${encodeURIComponent(jobId)}`);
      if (!jobPills.has(jobId)) continue;
      if (TERMINAL.has(job.status)) {
        unregisterJobPill(jobId);
      } else {
        info.label = taskNames[job.task] || info.label;
        syncJobPill();
      }
    } catch {
      // 任务已不可见（删除 / 非本人）：不再保留胶囊。
      unregisterJobPill(jobId);
    }
  }
  schedulePillPoll();
}

function schedulePillPoll() {
  const needsPoll = [...jobPills.keys()].some(jobId => !isJobStreaming(jobId));
  if (!needsPoll || !state.user) {
    if (pillPollTimer) { clearInterval(pillPollTimer); pillPollTimer = null; }
    return;
  }
  if (!pillPollTimer) pillPollTimer = setInterval(pollBackgroundJobs, 4000);
}

function registerJobPill(jobId, task) {
  const key = String(jobId);
  const existing = jobPills.get(key);
  jobPills.set(key, {
    id: key,
    label: taskNames[task] || existing?.label || 'AI 任务',
    route: existing?.route || jobPillRoute(),
    since: existing?.since || Date.now(),
  });
  syncJobPill();
  schedulePillPoll();
}

function unregisterJobPill(jobId) {
  if (!jobPills.delete(String(jobId))) return;
  syncJobPill();
  schedulePillPoll();
}

// 登出 / 401：清空后台任务胶囊与轮询。
onSessionReset(() => {
  jobPills.clear();
  if (pillPollTimer) { clearInterval(pillPollTimer); pillPollTimer = null; }
  if (pillNode) { pillNode.remove(); pillNode = null; }
});

// 主壳重建（登录页 ↔ 主壳）后把胶囊重新挂回顶栏。
export function remountJobPills() {
  syncJobPill();
}

/**
 * 关闭当前账号仍在使用的所有非全局 SSE 连接。
 * 路由切换、换题时调用：任务结果已经按 job_id 持久化，重进页面会按需重新挂载，
 * 旧标签页/旧题目上的流不应继续占用浏览器的连接池。
 * 后台任务胶囊不受影响：任务仍在服务端运行，胶囊改为轻量轮询等待完成。
 */
export function closeJobStreams() {
  for (const es of [...state.activeEventSources]) {
    try { es.close(); } catch { /* ignore */ }
    state.activeEventSources.delete(es);
  }
  syncJobPill();
  schedulePillPoll();
}

/**
 * 把一条 AI 任务挂到容器里：优先 SSE 流式推送，失败时有限重连后退回轮询。
 *
 * 流式期间使用「固定壳」：标题、状态徽章、阶段/进度条、取消按钮与耗时计时只创建
 * 一次，只按 150ms 节流局部刷新正文与阶段文本——取消按钮因此在整段流式输出中保持
 * 同一个 DOM 节点、始终可点击；任务进入终态后才恢复完整结果渲染。
 *
 * @param {HTMLElement} container 挂载容器
 * @param {number|string} id 任务 id
 * @param {(job: object) => void} [onComplete] 任务完成（completed/succeeded）时回调
 * @param {object} [options] 透传给结果卡的选项，额外支持：
 *   - task: 已知任务类型（用于首次渲染标题）
 *   - titleHint: 结果卡标题提示
 *   - onSettled(job): 任一终态（含失败/取消）或容器被移除时回调，参数可能为 null
 * @returns {Promise<object|null>} 终态任务（容器被移除时为 null）
 */
export async function mountJob(container, id, onComplete = null, options = {}) {
  let settleResolve = null;
  const settled = new Promise(resolve => { settleResolve = resolve; });
  if (!container || !container.isConnected) {
    settleResolve(null);
    return settled;
  }

  // 同一容器重新挂载任务（例如提交后重渲染）时先断开上一条流，避免旧任务继续推送。
  const previous = container.__zhixuJobSource;
  if (previous) {
    try { previous.close(); } catch { /* ignore */ }
    state.activeEventSources.delete(previous);
    container.__zhixuJobSource = null;
  }

  const card = el('div', { class: 'job-card job-card-live', 'data-job-id': String(id) });
  container.replaceChildren(card);

  let streamBuffer = '';
  let thinkingChars = 0;
  let closed = false;
  let completedCallbackFired = false;
  let settledFired = false;
  let activeEs = null;
  let pollTimer = null;
  let reconnectTimer = null;
  let watchdogTimer = null;
  let elapsedTimer = null;
  let paintTimer = null;
  let lastPaintAt = 0;
  let streamRetries = 0;
  let reconnectPending = false;
  let cancelRequested = false;
  let finalizing = false;
  let startAt = Date.now();
  let elapsedAnchored = false;
  // SSE 断线重连时复用最后已知快照，避免重连后界面从「准备中」重新开始。
  let lastJob = { id, status: 'running', stage: 'preparing', task: options.task || '' };
  let refs = null;

  function settleWith(job) {
    if (settleResolve) {
      const resolve = settleResolve;
      settleResolve = null;
      resolve(job || null);
    }
  }

  function fireDone(job) {
    if (completedCallbackFired || !onComplete || !['completed', 'succeeded'].includes(job.status)) return;
    completedCallbackFired = true;
    try { onComplete(job); } catch { /* 回调里的页面刷新失败不应打断流清理 */ }
  }

  function fireSettled(job) {
    if (settledFired) return;
    settledFired = true;
    if (typeof options.onSettled === 'function') {
      try { options.onSettled(job); } catch { /* ignore */ }
    }
    settleWith(job);
  }

  function applyJobData(job) {
    if (!job || typeof job !== 'object') return;
    lastJob = { ...lastJob, ...job };
    if (job.payload && !job.request) lastJob.request = job.payload;
  }

  function setButtonText(node, text) {
    const last = node.lastChild;
    if (last && last.nodeType === 3) last.nodeValue = text;
    else node.append(document.createTextNode(text));
  }

  function syncShell(job) {
    if (!refs || !card.isConnected) return;
    const status = job.status || 'running';
    const variant = statusBadgeVariant(status);
    refs.title.textContent = taskNames[job.task] || 'AI 学习任务';
    refs.statusBadge.textContent = statusNames[status] || status;
    refs.statusBadge.className = `badge job-status-badge ${variant}`.trim();
    refs.statusBadge.setAttribute('data-job-status', status);
    refs.attentionBadge.hidden = !job.needs_attention;
    refs.meta.textContent = [stageLabel(job.stage), job.request?.model, job.created_at ? dateText(job.created_at) : null]
      .filter(Boolean).join(' · ');
    const progress = parseStageProgress(job.stage);
    const cancelling = status === 'cancel_requested' || cancelRequested;
    refs.progressLabel.textContent = cancelling
      ? '正在取消…'
      : (progress ? `进度 ${progress.percent}%` : '处理中');
    if (progress) refs.progressLabel.setAttribute('data-progress-percent', String(progress.percent));
    else refs.progressLabel.removeAttribute('data-progress-percent');
    refs.progress.classList.toggle('job-progress-determinate', Boolean(progress) && !cancelling);
    refs.progressFill.style.width = progress ? `${progress.percent}%` : '';
    if (cancelling) {
      refs.cancelBtn.disabled = true;
      setButtonText(refs.cancelBtn, '正在取消…');
      refs.cancelBtn.setAttribute('aria-label', '正在取消任务');
    } else {
      refs.cancelBtn.disabled = false;
      setButtonText(refs.cancelBtn, '取消任务');
      refs.cancelBtn.setAttribute('aria-label', '取消任务');
    }
  }

  function paintBody() {
    if (!refs || !card.isConnected) return;
    const thinkingVisible = thinkingChars > 0 && !streamBuffer;
    refs.thinking.hidden = !thinkingVisible;
    if (thinkingVisible) {
      refs.thinking.textContent = `模型正在深度思考…（已思考 ${thinkingChars} 字）`;
    }
    refs.liveBody.replaceChildren(streamBuffer ? markdown(streamBuffer) : '');
    refs.body.classList.toggle('is-empty', !streamBuffer && !thinkingVisible);
  }

  function paint() {
    if (closed || !card.isConnected || !refs) return;
    lastPaintAt = Date.now();
    syncShell(lastJob);
    paintBody();
  }

  function schedulePaint(immediate = false) {
    if (closed || !card.isConnected || !refs) return;
    if (immediate) {
      if (paintTimer) { clearTimeout(paintTimer); paintTimer = null; }
      paint();
      return;
    }
    const wait = STREAM_PAINT_MS - (Date.now() - lastPaintAt);
    if (wait <= 0) { paint(); return; }
    if (paintTimer) return;
    paintTimer = setTimeout(() => {
      paintTimer = null;
      paint();
    }, wait);
  }

  function buildShell() {
    const title = el('strong', { class: 'small-text job-title' }, taskNames[lastJob.task] || 'AI 学习任务');
    const statusBadge = badge(statusNames[lastJob.status] || 'running', statusBadgeVariant(lastJob.status));
    statusBadge.classList.add('job-status-badge');
    const attentionBadge = badge('需处理', 'amber');
    attentionBadge.classList.add('job-attention-badge');
    attentionBadge.hidden = true;
    const cancelBtn = button('取消任务', event => requestCancel(event.currentTarget),
      'danger small', 'stop', { 'data-action': 'job-cancel' });
    const meta = el('p', { class: 'job-meta job-stage-line' });
    const progressFill = el('span', { class: 'job-progress-fill' });
    const progress = el('div', { class: 'job-progress job-progress-shell', 'aria-hidden': 'true' }, progressFill);
    const progressLabel = el('span', { class: 'job-progress-label', 'aria-live': 'polite' }, '处理中');
    const elapsed = el('span', { class: 'job-elapsed' }, '已用 00:00');
    const thinking = el('p', { class: 'job-thinking' });
    thinking.hidden = true;
    const liveBody = el('div', { class: 'job-live-body', 'data-job-live-body': 'true' });
    const body = el('div', { class: 'job-stream-body' }, thinking, liveBody);
    card.replaceChildren(
      el('div', { class: 'job-header' }, title, el('div', { class: 'row job-header-actions' },
        statusBadge, attentionBadge, cancelBtn)),
      meta,
      el('div', { class: 'job-progress-row' }, progress, el('div', { class: 'job-progress-meta' }, progressLabel, elapsed)),
      body);
    refs = { title, statusBadge, attentionBadge, cancelBtn, meta, progress, progressFill, progressLabel, elapsed, thinking, liveBody, body };
    syncShell(lastJob);
    paintBody();
  }

  function startElapsedTimer() {
    if (elapsedTimer) clearInterval(elapsedTimer);
    const createdMs = Date.parse(lastJob.created_at || '');
    if (Number.isFinite(createdMs)) { startAt = createdMs; elapsedAnchored = true; }
    const tick = () => {
      if (closed || !refs || !card.isConnected) return;
      refs.elapsed.textContent = `已用 ${elapsedText(Date.now() - startAt)}`;
    };
    tick();
    elapsedTimer = setInterval(tick, 1000);
  }

  function stopTransport() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (watchdogTimer) { clearTimeout(watchdogTimer); watchdogTimer = null; }
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = null; }
    if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    if (activeEs) {
      try { activeEs.close(); } catch { /* ignore */ }
      state.activeEventSources.delete(activeEs);
      if (container.__zhixuJobSource === activeEs) container.__zhixuJobSource = null;
      activeEs = null;
    }
    syncJobPill();
    schedulePillPoll();
  }

  function cleanup() {
    if (closed) return;
    closed = true;
    stopTransport();
    // 容器被路由切换销毁时任务仍在服务端运行：保留顶栏胶囊，由轻量轮询等待终态。
    if (!settledFired) fireSettled(null);
  }

  async function requestCancel(trigger) {
    if (closed || cancelRequested) return;
    cancelRequested = true;
    if (trigger) trigger.disabled = true;
    syncShell(lastJob);
    try {
      await api(`/ai/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      toast('已发出取消请求，等待执行进程停止。');
      try {
        const latest = await api(`/ai/jobs/${encodeURIComponent(id)}`);
        if (closed || !card.isConnected) return;
        applyJobData(latest);
        if (TERMINAL.has(latest.status)) finishTerminal(latest);
        else { syncShell(lastJob); schedulePaint(true); }
      } catch { /* 等 SSE done 或轮询兜底 */ }
    } catch (error) {
      cancelRequested = false;
      if (!closed) toast(error.message, true);
    } finally {
      if (!closed && card.isConnected) { syncShell(lastJob); schedulePaint(true); }
    }
  }

  function finishTerminal(job) {
    if (!job || !TERMINAL.has(job.status)) return false;
    applyJobData(job);
    if (closed) { unregisterJobPill(lastJob.id ?? id); fireSettled(lastJob); return true; }
    stopTransport();
    try {
      if (card.isConnected) renderFullJob(lastJob);
    } catch {
      /* 终态渲染失败也要保证等待者结算 */
    }
    fireDone(lastJob);
    fireSettled(lastJob);
    unregisterJobPill(lastJob.id ?? id);
    cleanup();
    return true;
  }

  /**
   * 断线重连后的服务端文本对齐：以 snapshot.partial_content 或 replace 事件为准重置缓冲，
   * 避免「重连后把历史正文再追加一遍」。
   */
  function applyServerBuffer(text, authoritative = false) {
    const next = String(text || '');
    if (!next) return false;
    if (next === streamBuffer) return false;
    if (!streamBuffer) { streamBuffer = next; return true; }
    if (authoritative) { streamBuffer = next; return true; }
    // 服务端补齐了比本地更多的历史（例如本地缓冲还没收到完整前缀）：直接采用。
    if (next.startsWith(streamBuffer)) { streamBuffer = next; return true; }
    // 本地缓冲领先（增量比落库快）：保留本地，避免正文回退闪烁。
    return false;
  }

  function handleSnapshot(data) {
    if (!data || typeof data !== 'object') return;
    const wasReconnect = reconnectPending;
    reconnectPending = false;
    applyJobData(data);
    if (!elapsedAnchored && lastJob.created_at) startElapsedTimer();
    if (TERMINAL.has(lastJob.status)) {
      finishTerminal(lastJob);
      return;
    }
    if (typeof data.partial_content === 'string' && data.partial_content) {
      applyServerBuffer(data.partial_content, wasReconnect);
    }
    if (data.task) registerJobPill(id, data.task);
    schedulePaint(wasReconnect);
  }

  function handleStage(data) {
    if (!data || typeof data !== 'object') return;
    applyJobData(data);
    if (TERMINAL.has(lastJob.status)) {
      // 阶段事件可能携带终态（如 cancelled）：回读完整任务后再进入终态渲染。
      finalizeFromServer();
      return;
    }
    const wasCancel = cancelRequested;
    cancelRequested = cancelRequested || lastJob.status === 'cancel_requested';
    if (!wasCancel && cancelRequested) schedulePaint(true);
    else schedulePaint();
  }

  function handleDelta(data) {
    if (!data || typeof data !== 'object') return;
    const text = typeof data.text === 'string' ? data.text : '';
    if (data.replace) streamBuffer = text;
    else if (text) streamBuffer += text;
    schedulePaint();
  }

  async function finalizeFromServer() {
    if (closed || finalizing) return;
    finalizing = true;
    stopTransport();
    try {
      const latest = await api(`/ai/jobs/${encodeURIComponent(id)}`);
      if (closed || !card.isConnected) {
        applyJobData(latest);
        unregisterJobPill(lastJob.id ?? id);
        fireSettled(latest || null);
        cleanup();
        return;
      }
      applyJobData(latest);
      if (!elapsedAnchored && lastJob.created_at) startElapsedTimer();
      if (TERMINAL.has(lastJob.status)) finishTerminal(lastJob);
      else {
        finalizing = false;
        syncShell(lastJob);
        schedulePaint(true);
        if (typeof window.EventSource === 'function') connectStream();
        else startPolling(1500, 0);
      }
    } catch {
      finalizing = false;
      if (!closed) startPolling(1500, 0);
    }
  }

  function showPollError(error) {
    if (!refs || !card.isConnected) return;
    refs.thinking.hidden = true;
    refs.liveBody.replaceChildren(
      el('p', { class: 'job-error' }, error.message || '任务状态暂时无法读取。'),
      button('重新获取任务', () => startPolling(2000, 0), 'secondary small', 'repeat'));
    refs.body.classList.remove('is-empty');
  }

  // Exponential backoff polling fallback (2s -> 3.2s -> 5.1s -> 15s cap)
  async function startPolling(delayMs = 2000, polls = 0) {
    if (closed || !card.isConnected || !state.user) return;
    try {
      const job = await api(`/ai/jobs/${encodeURIComponent(id)}`);
      if (closed || !card.isConnected) return;
      applyJobData(job);
      if (!elapsedAnchored && lastJob.created_at) startElapsedTimer();
      if (TERMINAL.has(lastJob.status)) {
        finishTerminal(lastJob);
        return;
      }
      if (job.task) registerJobPill(id, job.task);
      syncShell(lastJob);
      schedulePaint(true);
      if (polls >= 30) {
        refs.liveBody.append(el('div', { class: 'row between notice', style: 'margin-top:12px' },
          el('span', {}, '任务耗时较长，仍在后台处理中。'),
          button('继续查看', () => startPolling(2000, 0), 'secondary small', 'repeat')));
        refs.body.classList.remove('is-empty');
        return;
      }
      const nextDelay = Math.min(15000, Math.round(delayMs * 1.6));
      pollTimer = setTimeout(() => startPolling(nextDelay, polls + 1), delayMs);
    } catch (error) {
      if (closed) return;
      if (card.isConnected) showPollError(error);
    }
  }

  // Try SSE streaming first, fall back cleanly to exponential backoff polling.
  // 偶发瞬断不立即永久放弃：先有限重连 2 次，仍失败才退回轮询。
  function connectStream() {
    if (closed || !card.isConnected || !state.user) return;
    if (typeof window.EventSource !== 'function') { startPolling(2000, 0); return; }
    let es;
    try {
      es = new EventSource(`/api/ai/jobs/${encodeURIComponent(id)}/stream`, { withCredentials: true });
    } catch {
      startPolling(2000, 0);
      return;
    }
    es.__zhixuJobId = String(id);
    activeEs = es;
    container.__zhixuJobSource = es;
    state.activeEventSources.add(es);
    syncJobPill();
    schedulePillPoll();

    es.addEventListener('snapshot', event => {
      try { handleSnapshot(JSON.parse(event.data)); } catch { /* 心跳/非法帧忽略 */ }
    });
    es.addEventListener('stage', event => {
      try { handleStage(JSON.parse(event.data)); } catch { /* 忽略 */ }
    });
    es.addEventListener('thinking', event => {
      try {
        const info = JSON.parse(event.data);
        const chars = Number(info.chars);
        thinkingChars = Number.isFinite(chars) && chars >= 0
          ? chars
          : thinkingChars + String(info.text || '').length;
        schedulePaint();
      } catch { /* 忽略 */ }
    });
    es.addEventListener('delta', event => {
      try { handleDelta(JSON.parse(event.data)); } catch { /* 忽略 */ }
    });
    es.addEventListener('done', () => { finalizeFromServer(); });

    es.onerror = () => {
      if (closed) return;
      try { es.close(); } catch { /* ignore */ }
      state.activeEventSources.delete(es);
      if (container.__zhixuJobSource === es) container.__zhixuJobSource = null;
      if (activeEs === es) activeEs = null;
      syncJobPill();
      schedulePillPoll();
      if (streamRetries < STREAM_MAX_RETRIES) {
        streamRetries += 1;
        reconnectPending = true;
        reconnectTimer = setTimeout(connectStream, 600 * streamRetries);
        return;
      }
      // 有限重连仍失败：退回轮询，任务结果仍可从 /api/ai/jobs 拉取。
      startPolling(2000, 0);
    };
  }

  // 容器被路由切换 / 局部重渲染销毁时，主动结束轮询、重连与 SSE，避免
  // 后台定时器在页面元素消失后继续请求。后台任务胶囊保留，改为轮询等待完成。
  function watchContainer() {
    if (closed) return;
    if (!card.isConnected) { cleanup(); return; }
    watchdogTimer = setTimeout(watchContainer, 4000);
  }

  function renderFullJob(job) {
    if (!card.isConnected) return;
    const isTerminal = TERMINAL.has(job.status);
    const headerActions = el('div', { class: 'row' },
      badge(statusNames[job.status] || job.status, statusBadgeVariant(job.status)),
      job.needs_attention ? badge('需处理', 'amber') : null);
    if (!isTerminal) {
      headerActions.append(job.status === 'cancel_requested'
        ? button('正在取消…', null, 'ghost small', 'stop', { disabled: true })
        : button('取消任务', event => busy(event.currentTarget, async () => {
          await api(`/ai/jobs/${id}/cancel`, { method: 'POST' });
          toast('已发出取消请求，等待执行进程停止。');
          renderFullJob(await api(`/ai/jobs/${id}`));
        }), 'danger small', 'stop'));
    } else if (['interrupted', 'failed', 'cancelled'].includes(job.status) && job.has_checkpoint) {
      headerActions.append(button('续跑任务', event => busy(event.currentTarget, async () => {
        await api(`/ai/jobs/${id}/resume`, { method: 'POST' });
        toast('已开始续跑任务。');
        mountJob(container, id, onComplete, options);
      }), 'soft small', 'repeat'));
    }

    card.replaceChildren(
      el('div', { class: 'job-header' },
        el('strong', { class: 'small-text' }, taskNames[job.task] || 'AI 学习任务'),
        headerActions),
      el('p', { class: 'job-meta' }, [stageLabel(job.stage), job.request?.model, job.created_at ? dateText(job.created_at) : null].filter(Boolean).join(' · '))
    );

    if (!isTerminal) card.append(el('div', { class: 'job-progress' }, el('span')));
    if (job.error) card.append(el('p', { class: 'job-error' }, job.error));
    if (job.needs_attention) {
      card.append(notice('任务需要处理：可点击「续跑任务」从检查点继续，或重新发起。', 'warning'));
    } else if (job.status === 'cancelled') {
      card.append(notice('任务已取消。取消前已保存的讲解、笔记或评分会按任务规则保留，可在历史记录中查看。'));
    }

    // Human-in-the-loop outline confirmation UI
    if (job.status === 'awaiting_outline' && job.result?.outline) {
      const outlineArea = el('textarea', { class: 'note-editor', style: 'min-height:220px', value: job.result.outline });
      card.append(
        notice(job.result.notice || '提纲已生成，可自由增删、调整章节顺序，确认后将分节并行生成正文。'),
        outlineArea,
        el('div', { class: 'row end', style: 'margin-top:12px' },
          button('取消任务', event => busy(event.currentTarget, async () => {
            await api(`/ai/jobs/${id}/cancel`, { method: 'POST' });
            renderFullJob(await api(`/ai/jobs/${id}`));
          }), 'secondary small', 'stop'),
          button('确认提纲并生成正文', event => busy(event.currentTarget, async () => {
            if (!outlineArea.value.trim()) throw new Error('提纲不能为空');
            await api(`/ai/jobs/${id}/confirm-outline`, { method: 'POST', data: { outline: outlineArea.value.trim() } });
            toast('提纲已确认，开始分节生成正文…');
            mountJob(container, id, onComplete, options);
          }), '', 'sparkle'))
      );
      return;
    }

    if (job.result) {
      card.append(renderAiResult(job.result, { task: job.task, qid: job.request?.qid, ...options }));
    } else if (job.partial_content && !isTerminal) {
      card.append(markdown(job.partial_content));
    }
  }

  buildShell();
  startElapsedTimer();
  registerJobPill(id, options.task || lastJob.task);
  watchdogTimer = setTimeout(watchContainer, 4000);
  connectStream();

  return settled;
}
