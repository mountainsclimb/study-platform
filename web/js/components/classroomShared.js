/* AI 课堂共享渲染工具：场景类型/状态文案、幻灯片 HTML 安全渲染、LaTeX、时长与格式化。
 *
 * 只做「展示层安全兜底」：后端已对文本 HTML 做白名单净化，这里再用 DOMPurify
 * 以最小标签集二次净化（fail-closed：净化器缺失时退化为纯文本），并过滤 inline style
 * 只保留幻灯片需要的排版属性。禁止任何 innerHTML 直接拼接未净化内容。
 */
import { button, el } from '../core/dom.js';
import { api } from '../core/api.js';
import { mountJob, taskNames } from '../core/jobs.js';
import { escapeHtml, markdown, renderMath, sanitizeMath } from '../core/markdown.js';
import { t } from '../core/i18n.js';

export const SLIDE_WIDTH = 1000;
export const SLIDE_HEIGHT = 562.5;

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
export const SCENE_TYPE_LABELS = {
  get slide() { return t('classroom.shared.slides'); },
  get quiz() { return t('classroom.shared.classQuiz'); },
  get interactive() { return t('classroom.shared.interactiveScene'); },
  get pbl() { return t('classroom.shared.projectBasedLearning'); },
};

export const WIDGET_LABELS = {
  get simulation() { return t('classroom.shared.simulation'); },
  get mindmap() { return t('mindmap.mindMap'); },
  get game() { return t('classroom.shared.interactiveGame'); },
  get code() { return t('classroom.shared.liveCoding'); },
  get visualization3d() { return t('classroom.shared.3dVisualization'); },
};

export const COURSE_STATUS = {
  get draft() { return [t('mybank.draft'), '']; },
  get outline_ready() { return [t('classroom.shared.outlineReady'), 'amber']; },
  get generating() { return [t('classroom.shared.generating'), 'blue']; },
  get ready() { return [t('admission.available'), 'teal']; },
  get failed() { return [t('classroom.shared.generationFailed'), 'red']; },
};

export const SCENE_STATUS = {
  get pending() { return [t('classroom.shared.pending'), '']; },
  get generating() { return [t('classroom.shared.generating'), 'blue']; },
  get ready() { return [t('admin.readyState'), 'teal']; },
  get failed() { return [t('classroom.shared.generationFailed'), 'red']; },
};

export const DEPTH_LABELS = {
  get brief() { return t('classroom.shared.brief'); },
  get standard() { return t('classroom.shared.standard'); },
  get deep() { return t('classroom.shared.deep'); },
};
export const MODE_LABELS = {
  get standard() { return t('classroom.wizard.standardClassroom'); },
  get interactive() { return t('classroom.wizard.immersiveInteraction'); },
};

const TEXT_TAGS = [
  'p', 'span', 'strong', 'b', 'em', 'i', 'u', 's', 'br', 'div',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'sub', 'sup',
];
const STYLE_PROPS = new Set(['font-size', 'color', 'text-align', 'line-height', 'font-weight', 'font-family']);
const DANGEROUS_TAGS = ['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'form', 'input', 'button', 'svg', 'math'];

export function typeLabel(type) {
  return SCENE_TYPE_LABELS[String(type || '')] || t('classroom.shared.unknownScene');
}

export function statusBadgeParts(status, table = COURSE_STATUS) {
  const item = table[String(status || '')];
  return item ? { label: item[0], variant: item[1] } : { label: String(status || t('admin.unknown')), variant: '' };
}

export function modeLabel(mode) {
  return MODE_LABELS[String(mode || '')] || String(mode || t('classroom.wizard.standardClassroom'));
}

/** 把幻灯片 text 元素的 HTML 过滤成安全的 DocumentFragment（DOMPurify 缺失时退化为纯文本）。 */
export function slideTextNode(rawHtml, fallbackText = '') {
  const node = el('div', { class: 'cls-text-html' });
  const raw = String(rawHtml || '').trim();
  if (!raw) {
    if (fallbackText) node.textContent = String(fallbackText);
    return node;
  }
  const purify = window.DOMPurify;
  if (purify && typeof purify.sanitize === 'function' && purify.isSupported !== false) {
    let cleaned = '';
    try {
      cleaned = purify.sanitize(raw, {
        ALLOWED_TAGS: TEXT_TAGS,
        ALLOWED_ATTR: ['style'],
        FORBID_TAGS: DANGEROUS_TAGS,
        FORBID_ATTR: ['onerror', 'onload', 'onclick', 'href', 'src'],
        ALLOW_DATA_ATTR: false,
      });
    } catch {
      cleaned = '';
    }
    if (!cleaned) {
      node.textContent = fallbackText || stripTags(raw);
      return node;
    }
    node.innerHTML = cleaned;
    // 二次过滤 inline style：只保留排版属性，去掉 url()/expression()/javascript:。
    for (const child of node.querySelectorAll('[style]')) {
      const kept = [];
      for (const declaration of String(child.getAttribute('style') || '').split(';')) {
        const [prop, ...rest] = declaration.split(':');
        const name = String(prop || '').trim().toLowerCase();
        const value = rest.join(':').trim();
        if (!STYLE_PROPS.has(name) || !value) continue;
        if (/url\s*\(|expression\s*\(|javascript:/i.test(value)) continue;
        kept.push(`${name}: ${value}`);
      }
      if (kept.length) child.setAttribute('style', kept.join('; '));
      else child.removeAttribute('style');
    }
    for (const child of node.querySelectorAll('*')) {
      for (const attr of [...child.attributes]) {
        if (attr.name.toLowerCase() !== 'style') child.removeAttribute(attr.name);
      }
    }
    return node;
  }
  node.textContent = fallbackText || stripTags(raw);
  return node;
}

export function stripTags(raw) {
  return String(raw || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

/** LaTeX 渲染：KaTeX 优先，失败时退回可读源码（不抛错、不阻塞页面）。 */
export function latexNode(tex, { display = true } = {}) {
  const wrap = el('div', { class: 'cls-latex' + (display ? ' display' : '') });
  const source = String(tex || '').trim();
  if (!source) return wrap;
  try {
    wrap.innerHTML = sanitizeMath(renderMath(source, display));
  } catch {
    wrap.textContent = source;
    wrap.classList.add('fallback');
  }
  return wrap;
}

/** 安全的 Markdown 渲染（core/markdown.js fail-closed）。 */
export function markdownNode(text) {
  return markdown(String(text || ''));
}

export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
}

/** 无语音时的字幕停留时长估计（毫秒）。 */
export function speechDuration(text) {
  const length = String(text || '').length;
  return Math.min(9000, Math.max(1400, length * 170));
}

export function truncate(text, limit = 120) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

/** 幻灯片元素的可读摘要（场景侧栏/大纲提示用）。 */
export function contentDigest(scene) {
  const content = scene?.content || {};
  if (content.type === 'slide') {
    const elements = content.canvas?.elements || [];
    const firstText = elements.find(item => item.type === 'text' && (item.text || item.content));
    return truncate(firstText ? (firstText.text || stripTags(firstText.content)) : t('classroom.shared.slides'), 60);
  }
  if (content.type === 'quiz') return t('common.countQuestions', { count: (content.questions || []).length });
  if (content.type === 'interactive') return WIDGET_LABELS[content.widget] || t('classroom.shared.interactiveContent');
  if (content.type === 'pbl') return truncate(content.project?.title || t('classroom.shared.projectBasedLearning'), 40);
  return '';
}

export function sceneSummaryLine(scene) {
  const meta = scene?.meta || {};
  const parts = [typeLabel(scene?.type)];
  if (meta.placeholder) parts.push(t('classroom.wizard.contentPending'));
  if (scene?.status && scene.status !== 'ready') parts.push(statusBadgeParts(scene.status, SCENE_STATUS).label);
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// 课程导出（P1：PPTX / 离线 HTML / 课程 ZIP；P5：服务端 PDF / MP4 视频）
// ---------------------------------------------------------------------------

// 任务中心标题：core/jobs.js 是 Lead 独占文件，这里只补充本模块扩展任务的中文名（不改其实现）。
if (!taskNames['course-video']) taskNames['course-video'] = t('job.task.courseVideo');

function formatSize(bytes) {
  const value = Number(bytes) || 0;
  if (value <= 0) return t('classroom.shared.unknownSize');
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(2)} MB`;
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return t('classroom.shared.s', { v1: value.toFixed(1) });
  const minutes = Math.floor(value / 60);
  return t('classroom.shared.mS', { v1: minutes, v2: Math.round(value % 60) });
}

function filenameFromDisposition(header, fallback) {
  const raw = String(header || '');
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(raw);
  if (extended) {
    try { return decodeURIComponent(extended[1].trim()); } catch { /* 继续尝试普通 filename */ }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(raw);
  if (plain) {
    const value = plain[1].trim();
    try { return decodeURIComponent(value); } catch { return value; }
  }
  return fallback;
}

/**
 * 下载课程导出文件（§EXPORT-INTERACTIVE.md §2.3）。
 * `format=html` 体积过大时后端会降级为 ZIP 并带 `X-Zhixu-Export-Fallback: zip`。
 * P5：`format=pdf` 为服务端讲义 PDF；`format=mp4` 下载最近一次成功生成的视频。
 */
export async function downloadCourseExport(courseId, format, { includeTts = false } = {}) {
  const params = new URLSearchParams({ format: String(format || 'pptx') });
  if (includeTts) params.set('include_tts', 'true');
  const response = await fetch(`/api/classroom/courses/${courseId}/export?${params.toString()}`, {
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (!response.ok) {
    let detail = t('wrongbook.exportFailed', { status: response.status });
    try {
      const payload = await response.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch { /* 非 JSON 错误体 */ }
    throw new Error(detail);
  }
  const blob = await response.blob();
  const fallback = String(response.headers.get('X-Zhixu-Export-Fallback') || '').toLowerCase();
  const expectedFormat = fallback === 'zip' ? 'zip' : format;
  const filename = filenameFromDisposition(
    response.headers.get('Content-Disposition'),
    `classroom-${courseId}.${expectedFormat}`,
  );
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: filename, style: 'display:none' });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return { filename, contentType: response.headers.get('Content-Type') || '', fallback, size: blob.size };
}

/** 查询课程 PDF/视频导出能力、最近视频产物与最近任务（用于禁用按钮并说明原因）。 */
export async function loadCourseVideoState(courseId) {
  return api(`/classroom/courses/${encodeURIComponent(courseId)}/video`);
}

/**
 * 导出对话框：PPTX / 离线 HTML / 课程 ZIP / 服务端 PDF / MP4 视频。
 * - PDF：服务端渲染 A4 横向讲义（无渲染引擎时按钮禁用并说明，可改用 HTML + 浏览器打印）；
 * - MP4：先创建 `course-video` 任务（逐场景截图 + ffmpeg 合成），任务卡显示进度/取消，
 *   完成后自动下载，也可在「已有视频」区域再次下载；失败且留有 checkpoint 时可续跑。
 */
export function openExportDialog({ courseId, courseTitle = '' }) {
  const ttsCheck = el('input', { type: 'checkbox' });
  const status = el('p', { class: 'help cls-export-status' }, t('classroom.shared.pptxDoesNotSupportEmbeddedAudioVoice'));
  const capabilityNote = el('p', { class: 'help cls-export-capabilities' }, t('classroom.shared.checkingServerPdfVideoExportCapability'));
  capabilityNote.style.margin = '6px 0 0';
  const latestRow = el('div', { class: 'row cls-export-video-latest' });
  latestRow.style.margin = '8px 0 0';
  const jobHost = el('div', { class: 'cls-export-job' });
  jobHost.style.marginTop = '10px';
  let capabilities = null;
  let videoButtonBusy = false;

  function setStatus(text, isError = false) {
    status.textContent = text;
    status.classList.toggle('is-error', Boolean(isError));
  }

  function setBusyButton(trigger, busy, text) {
    if (!trigger) return;
    trigger.disabled = Boolean(busy);
    if (busy && text) trigger.dataset.busyText = text;
  }

  const formatButtons = {};
  const formatLabels = {
    pptx: t('classroom.shared.exportPptx'), html: t('classroom.shared.exportOfflineHtml'), zip: t('classroom.shared.exportCourseZip'),
    pdf: t('classroom.shared.exportPdf'), mp4: t('classroom.shared.generateMp4Video'),
  };
  const formatVariants = { pptx: '', html: 'secondary', zip: 'secondary', pdf: 'secondary', mp4: 'secondary' };
  for (const format of ['pptx', 'html', 'zip', 'pdf']) {
    formatButtons[format] = button(formatLabels[format], async (event) => {
      const trigger = event.currentTarget;
      setBusyButton(trigger, true);
      setStatus(
        format === 'pdf'
          ? t('classroom.shared.renderingPdfTheFirstRenderStartsA')
          : t('classroom.shared.generatingExportFile'),
      );
      try {
        const result = await downloadCourseExport(courseId, format, { includeTts: ttsCheck.checked });
        if (format === 'pdf') {
          setStatus(t('classroom.shared.exported', { v1: result.filename, v2: formatSize(result.size) }));
        } else if (result.fallback === 'zip') {
          setStatus(t('classroom.shared.offlineHtmlExceededTheSizeLimitAnd', { v1: result.filename, v2: formatSize(result.size) }));
        } else {
          setStatus(t('classroom.shared.exported', { v1: result.filename, v2: formatSize(result.size) }));
        }
      } catch (error) {
        setStatus(error.message || t('classroom.shared.exportFailedPleaseTryAgain'), true);
      } finally {
        if (trigger.isConnected) trigger.disabled = false;
      }
    }, formatVariants[format], format === 'pdf' ? 'printer' : 'download');
  }

  const mp4Button = button(t('classroom.shared.generateMp4Video'), () => {
    startVideoJob();
  }, 'secondary', 'presentation');
  formatButtons.mp4 = mp4Button;

  function renderLatestAsset(asset) {
    if (!asset) {
      latestRow.replaceChildren();
      return;
    }
    const meta = asset.meta || {};
    latestRow.replaceChildren(
      el('span', { class: 'help' },
        t('classroom.shared.latestVideoScenes', { v1: meta.scene_count || '?', v2: formatDuration(meta.duration_seconds), v3: formatSize(meta.size) })),
      button(t('classroom.shared.downloadExistingVideo'), async () => {
        setStatus(t('classroom.shared.downloadingTheExistingVideo'));
        try {
          const result = await downloadCourseExport(courseId, 'mp4');
          setStatus(t('classroom.shared.exported', { v1: result.filename, v2: formatSize(result.size) }));
        } catch (error) {
          setStatus(error.message || t('classroom.shared.downloadFailedPleaseTryAgain'), true);
        }
      }, 'secondary small', 'download'),
    );
  }

  function renderResumeJob(job) {
    if (!job || !job.has_checkpoint) return;
    if (!['failed', 'interrupted', 'cancelled'].includes(job.status)) return;
    latestRow.append(button(t('classroom.shared.resumeTheLastVideoTask'), async (event) => {
      const trigger = event.currentTarget;
      setBusyButton(trigger, true);
      try {
        await api(`/ai/jobs/${encodeURIComponent(job.id)}/resume`, { method: 'POST' });
        setStatus(t('classroom.shared.videoTaskReDispatchedReusingRenderedFrames'));
        mountVideoJob(job.id);
      } catch (error) {
        setStatus(error.message || t('classroom.shared.resumeFailedPleaseTryAgain'), true);
        if (trigger.isConnected) trigger.disabled = false;
      }
    }, 'secondary small', 'repeat'));
  }

  function mountVideoJob(jobId) {
    jobHost.replaceChildren();
    void mountJob(jobHost, jobId, (job) => {
      void handleVideoComplete(job);
    }, {
      task: 'course-video',
      onSettled: (job) => {
        if (!job || job.status === 'completed') return;
        videoButtonBusy = false;
        if (mp4Button.isConnected) mp4Button.disabled = Boolean(capabilities?.video?.available === false);
        const label = job.status === 'cancelled' ? t('tasks.status.cancelled') : t('tasks.status.failed');
        setStatus(
          t('classroom.shared.videoTask', { v1: label, v2: job.error || t('settings.embeddingUnknownError') })
          + (job.has_checkpoint ? t('classroom.shared.finishedFramesAreKeptClickResumeThe') : ''),
          true,
        );
      },
    });
  }

  async function handleVideoComplete(job) {
    const result = job?.result || {};
    videoButtonBusy = false;
    if (mp4Button.isConnected) mp4Button.disabled = Boolean(capabilities?.video?.available === false);
    setStatus(
      t('classroom.shared.videoGeneratedScenes', { v1: result.scene_count || '?', v2: formatDuration(result.duration_seconds) })
      + t('classroom.shared.downloadingAutomatically', { v1: formatSize(result.size_bytes) }),
    );
    const meta = {
      scene_count: result.scene_count,
      duration_seconds: result.duration_seconds,
      size: result.size_bytes,
    };
    renderLatestAsset({ meta });
    try {
      const file = await downloadCourseExport(courseId, 'mp4');
      setStatus(t('classroom.shared.exported', { v1: file.filename, v2: formatSize(file.size) }));
    } catch (error) {
      setStatus(t('classroom.shared.videoGeneratedButAutomaticDownloadFailed', {
        v1: error.message || t('classroom.wizard.clickDownloadExistingVideoToRetry'),
      }), true);
    }
  }

  async function startVideoJob() {
    if (videoButtonBusy) return;
    videoButtonBusy = true;
    mp4Button.disabled = true;
    jobHost.replaceChildren();
    setStatus(t('classroom.shared.creatingTheVideoExportTask'));
    try {
      const response = await api(`/classroom/courses/${encodeURIComponent(courseId)}/video`, {
        method: 'POST',
        data: {},
      });
      const job = response.job || {};
      setStatus(
        t('classroom.shared.createdVideoTaskScenesFps', { v1: response.scene_count, v2: response.fps })
        + t('classroom.shared.renderingScreenshotsSceneBySceneAndComposing'),
      );
      mountVideoJob(job.id);
    } catch (error) {
      videoButtonBusy = false;
      if (mp4Button.isConnected) mp4Button.disabled = Boolean(capabilities?.video?.available === false);
      setStatus(error.message || t('classroom.shared.failedToCreateTheVideoTaskPlease'), true);
    }
  }

  function applyCapabilities(caps) {
    capabilities = caps || null;
    const pdf = caps?.pdf || {};
    const mp4 = caps?.video || {};
    const pdfUnavailable = pdf.available === false;
    const videoUnavailable = mp4.available === false;
    if (formatButtons.pdf.isConnected) {
      formatButtons.pdf.disabled = pdfUnavailable;
      formatButtons.pdf.title = pdfUnavailable ? (pdf.detail || t('classroom.shared.serverPdfExportUnavailable')) : '';
    }
    if (mp4Button.isConnected) {
      mp4Button.disabled = videoUnavailable || videoButtonBusy;
      mp4Button.title = videoUnavailable ? (mp4.detail || t('classroom.shared.videoExportUnavailable')) : '';
    }
    const reasons = [];
    const cleanReason = (text) => String(text || t('admin.unavailable')).replace(/[。；;]+$/u, '');
    if (pdfUnavailable) reasons.push(cleanReason(pdf.detail));
    if (videoUnavailable) reasons.push(cleanReason(mp4.detail));
    if (reasons.length) {
      capabilityNote.textContent = t('classroom.shared.someExportsUnavailable', { v1: reasons.join(t('common.semicolonSeparator')) });
      capabilityNote.style.color = '#a05d55';
    } else {
      capabilityNote.textContent = t('classroom.shared.serverPdfHandoutsCanBeExportedMp4');
      capabilityNote.style.color = '';
    }
  }

  async function detectCapabilities() {
    try {
      const info = await loadCourseVideoState(courseId);
      applyCapabilities(info.capabilities || {});
      renderLatestAsset(info.asset);
      renderLatestJob(info.latest_job);
    } catch (error) {
      capabilityNote.textContent = t('classroom.shared.failedToDetectServerExportCapabilityYou');
    }
  }

  function renderLatestJob(job) {
    renderResumeJob(job);
  }

  const dialog = el('dialog', { class: 'cls-export-dialog' },
    el('div', { class: 'cls-export-dialog-head' },
      el('h3', {}, t('classroom.wizard.exportClassroom')),
      button(t('admin.closed'), () => dialog.close(), 'ghost small', 'close', { 'aria-label': t('classroom.shared.closeExportDialog') })),
    el('p', { class: 'help', 'data-no-i18n': '' }, t('classroom.shared.courseFilesOpenInTheBrowserDownload', { v1: courseTitle || `#${courseId}` })),
    el('div', { class: 'row cls-export-buttons' },
      Object.values(formatButtons)),
    el('label', { class: 'check-label' }, ttsCheck, t('classroom.shared.includesAiVoiceOverOfflineHtmlZip')),
    capabilityNote,
    latestRow,
    jobHost,
    status);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
  void detectCapabilities();
  return dialog;
}

export { escapeHtml };
