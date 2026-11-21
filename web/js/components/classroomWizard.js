/* AI 课堂生成工作台：模型选择 → course 任务 → awaiting_outline 结构化大纲编辑 → 逐场景生成。
 *
 * 与后端契约（app/classroom/API-SAMPLES.md）对齐：
 * - POST /api/classroom/courses/{id}/generate {stage, provider_id, model, scene_types, scene_count, depth}
 * - 任务进度/取消复用 core/jobs.js 的 mountJob（SSE + 轮询兜底）；
 * - 任务进入 awaiting_outline 时用 job.result.outline（对象或 JSON 字符串）渲染可编辑大纲，
 *   确认走 POST /api/ai/jobs/{job_id}/confirm-outline {outline, sections}；
 * - 完成后展示场景清单与失败场景的单场重新生成入口。
 */
import { api, refreshProviders } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, button, badge, notice, loading, busy, toast, empty, field, input, select,
} from '../core/dom.js';
import { current, go, setHashParams } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from './modelPicker.js';
import {
  COURSE_STATUS, DEPTH_LABELS, SCENE_TYPE_LABELS, SCENE_STATUS, openExportDialog,
  statusBadgeParts, typeLabel, truncate,
} from './classroomShared.js';
import { t } from '../core/i18n.js';

const SCENE_TYPE_ORDER = ['slide', 'quiz', 'interactive', 'pbl'];

function asOutline(value) {
  if (!value) return null;
  if (typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

function outlineScenes(outline) {
  return Array.isArray(outline?.scenes) ? outline.scenes.filter(item => item && typeof item === 'object') : [];
}

/** 联网检索来源（P3）：`course.outline.sources` / `job.result.outline.sources`。 */
function outlineSources(outline) {
  return Array.isArray(outline?.sources)
    ? outline.sources.filter(item => item && typeof item === 'object' && (item.url || item.title))
    : [];
}

function webSearchHint(course) {
  const config = course?.config?.web_search;
  if (!config || typeof config !== 'object') return '';
  const count = Number(config.count) || 0;
  if (config.used && count > 0) return t('classroom.wizard.theLastGenerationUsedWebSourcesSaved', { v1: count });
  if (config.enabled) {
    return t('classroom.wizard.theLastGenerationUsedNoWebSources', {
      v1: truncate(config.skipped || t('classroom.wizard.noUsableSourcesFound'), 120),
    });
  }
  return '';
}

/** 来源预览列表：标题 / 站点 / 日期 / 链接（外部链接新窗口打开，文本先经 DOM 安全创建）。 */
function sourcesSection(outline, { open = false, label = t('classroom.wizard.references') } = {}) {
  const sources = outlineSources(outline);
  if (!sources.length) return null;
  const list = el('ol', { class: 'cls-source-list' });
  for (const item of sources) {
    const metaBits = [item.site, item.date].map(value => String(value || '').trim()).filter(Boolean).join(' · ');
    const url = String(item.url || '').trim();
    const title = String(item.title || url || t('classroom.wizard.untitledSource'));
    const link = /^https?:\/\//i.test(url)
      ? el('a', {
        class: 'cls-source-url',
        href: url,
        target: '_blank',
        rel: 'noopener noreferrer',
      }, url)
      : el('span', { class: 'cls-source-url' }, url || t('classroom.wizard.noLink'));
    list.append(el('li', { class: 'cls-source-item' },
      el('span', { class: 'cls-source-index' }, `[${Number(item.index) || sources.indexOf(item) + 1}]`),
      el('div', { class: 'cls-source-body' },
        el('div', { class: 'cls-source-title-line' },
          el('span', { class: 'cls-source-title', 'data-no-i18n': '' }, truncate(title, 120)),
          metaBits ? el('span', { class: 'help cls-source-meta', 'data-no-i18n': '' }, metaBits) : null),
        link,
        item.summary
          ? el('p', { class: 'help cls-source-summary', 'data-no-i18n': '' }, truncate(item.summary, 200))
          : null)));
  }
  const details = el('details', { class: 'cls-sources-block' },
    el('summary', {}, t('classroom.wizard.itemsExpandable', { v1: label, v2: sources.length })),
    el('p', { class: 'help cls-source-hint' },
      t('classroom.wizard.theMaterialBelowComesFromWebSearch')),
    list);
  details.open = Boolean(open);
  return details;
}

function defaultSceneTypes(course) {
  if (String(course?.mode || '') === 'interactive') return ['slide', 'quiz', 'interactive'];
  return ['slide', 'quiz'];
}

function sceneCountOptions(selected) {
  const options = [];
  for (let value = 3; value <= 12; value += 1) options.push({ value: String(value), label: t('home.activity.scenes', { count: value }) });
  return select(options, String(selected || 6));
}

function depthOptions(selected) {
  return select(
    Object.entries(DEPTH_LABELS).map(([value, label]) => ({ value, label })),
    String(selected || 'standard'),
  );
}

export function createWizardView({ container, version, courseId, jobId }) {
  const root = el('div', { class: 'cls-page cls-wizard' });
  const toolbar = el('div', { class: 'cls-wizard-toolbar row between' });
  const head = el('div', { class: 'cls-wizard-head' });
  const jobHost = el('div', { class: 'cls-job-host' });
  const body = el('div', { class: 'cls-wizard-body' });
  root.append(toolbar, head, jobHost, body);
  container.append(root);

  let detail = null;
  let meta = null;
  let destroyed = false;

  function courseInteractive() {
    const course = detail?.course || {};
    return course.interactive_mode === true || String(course.mode || '') === 'interactive';
  }

  function imageMaxPerScene() {
    return Number(meta?.limits?.images_per_scene_max) || Number(meta?.images?.max_per_scene) || 2;
  }

  function interactiveWarningsText() {
    const info = meta?.interactive_mode || {};
    return t('classroom.wizard.interactiveScenesForTheWholeCoursePreferring', { v1: info.interactive_scenes_min ?? 1, v2: info.interactive_scenes_max ?? 2 });
  }

  function backButton() {
    return button(t('classroom.wizard.backToClassroomList'), () => go('classroom'), 'secondary small', 'chevron');
  }

  function renderToolbar(extra = null) {
    toolbar.replaceChildren(backButton(), extra || el('span'));
  }

  function refreshHeader() {
    if (!detail) {
      head.replaceChildren();
      return;
    }
    const course = detail.course || {};
    const status = statusBadgeParts(course.status, COURSE_STATUS);
    const sceneCount = (detail.scenes || []).length || (course.outline?.scenes || []).length || Number(course.scene_count) || 0;
    head.replaceChildren(
      el('div', { class: 'cls-wizard-title' },
        el('div', { class: 'cls-wizard-title-main' },
          el('h2', { 'data-no-i18n': '' }, course.title || course.topic || t('home.ai.unnamedCourse')),
          el('div', { class: 'row cls-wizard-badges' },
            badge(status.label, status.variant),
            badge(course.interactive_mode || course.mode === 'interactive' ? t('classroom.wizard.immersiveInteraction') : t('classroom.wizard.standardClassroom'),
              course.interactive_mode || course.mode === 'interactive' ? 'blue' : ''),
            course.config?.images === false ? badge(t('classroom.wizard.noImages'), '') : null)),
        el('p', { class: 'help', 'data-no-i18n': '' },
          [course.subject, course.grade, t('home.activity.scenes', { count: sceneCount })].filter(Boolean).join(' · '))),
      el('div', { class: 'row' },
        course.status === 'ready'
          ? button(t('classroom.wizard.enterClassroomPlayback'), () => go('classroom', null, { course: courseId, view: 'play' }), 'small', 'presentation')
          : null));
  }

  async function loadDetail() {
    const data = await api(`/classroom/courses/${courseId}`);
    detail = data;
    return data;
  }

  function setBody(...nodes) {
    body.replaceChildren(...nodes.filter(Boolean));
  }

  function setJobHost(...nodes) {
    jobHost.replaceChildren(...nodes.filter(Boolean));
  }

  // -------------------------------------------------------------------------
  // 生成设置
  // -------------------------------------------------------------------------

  function sceneTypeCheckboxes(course) {
    const chosen = new Set(course.config?.scene_types || defaultSceneTypes(course));
    const nodes = [];
    const inputs = {};
    for (const type of SCENE_TYPE_ORDER) {
      const checkbox = el('input', { type: 'checkbox', checked: chosen.has(type) });
      inputs[type] = checkbox;
      nodes.push(el('label', { class: 'cls-check-pill' }, checkbox, SCENE_TYPE_LABELS[type]));
    }
    return {
      node: el('div', { class: 'cls-check-pills' }, nodes),
      values: () => SCENE_TYPE_ORDER.filter(type => inputs[type].checked),
    };
  }

  function renderSetup(options = {}) {
    setJobHost();
    const course = detail.course || {};
    const hasOutline = outlineScenes(asOutline(course.outline)).length > 0;
    const configHint = hasOutline
      ? t('classroom.wizard.theCourseAlreadyHasAnOutlineContinue')
      : t('classroom.wizard.noCourseOutlineYetChooseAModel');
    const status = statusBadgeParts(course.status, COURSE_STATUS);
    const card = el('section', { class: 'card cls-setup-card' },
      el('div', { class: 'card-header' }, el('h3', {}, t('classroom.wizard.generateCourseContent')), badge(status.label, status.variant)),
      el('p', { class: 'help' }, configHint));
    const typePicker = sceneTypeCheckboxes(course);
    const countSelect = sceneCountOptions(course.config?.scene_count);
    const depthSelect = depthOptions(course.config?.depth);
    const interactive = courseInteractive();
    const interactiveInfo = meta?.interactive_mode || {};
    const optionGrid = el('div', { class: 'cls-option-grid' },
      field(t('classroom.wizard.numberOfScenes'), countSelect, t('classroom.wizard.theOutlineGeneratesThatManyScenes3')),
      field(t('classroom.wizard.generationDepth'), depthSelect, t('classroom.wizard.briefIsShorterDeepIncludesDerivationsCommon')),
      field(t('classroom.wizard.sceneTypes'), typePicker.node, interactive
        ? t('classroom.wizard.selectAtLeastOneImmersiveModeBenefits')
        : t('classroom.wizard.selectAtLeastOneInteractivePblScenes')));

    if (interactive) {
      optionGrid.append(el('div', { class: 'cls-interactive-note full' },
        badge(t('classroom.wizard.immersiveInteraction_2', {
          v1: interactiveInfo.enabled === false ? t('classroom.wizard.notEnabledSuffix') : '',
        }), 'blue'),
        el('p', { class: 'help' }, interactiveInfo.enabled === false
          ? t('classroom.wizard.thisInstanceDoesNotEnableImmersiveInteraction')
          : interactiveWarningsText())));
    }

    // 配图能力（/meta.images）：有连接才可开启；关闭时显式传 images=false，避免后端自动配图。
    const imagesInfo = meta?.images || {};
    const imagesAvailable = Boolean(imagesInfo.enabled);
    const imagesMax = imageMaxPerScene();
    const imagesCheck = el('input', { type: 'checkbox', checked: imagesAvailable, disabled: !imagesAvailable });
    const imagesCountSelect = select(
      [{ value: '0', label: t('classroom.wizard.noImages') }, { value: '1', label: t('classroom.wizard.1PerSlide') }, { value: '2', label: t('classroom.wizard.2PerSlide') }],
      String(Math.min(1, imagesMax)),
      { disabled: !imagesAvailable, 'aria-label': t('classroom.wizard.imagesPerSlide') },
    );
    imagesCountSelect.disabled = !imagesAvailable || !imagesCheck.checked;
    const imagesHelp = el('p', { class: 'help' }, imagesAvailable
      ? t('classroom.wizard.onByDefaultAutomaticallyUsesTheAvailable', {
        v1: imagesInfo.provider || imagesInfo.model || t('admin.configured'),
        v2: imagesMax,
      })
      : t('classroom.wizard.thisAccountHasNoImageGenerationConnection'));
    const imagesField = el('div', { class: 'cls-image-field full' },
      el('div', { class: 'cls-image-field-head' },
        el('label', { class: 'check-label' }, imagesCheck, t('classroom.wizard.aiImagesTextbookIllustrationsForSlides')),
        imagesCountSelect),
      imagesHelp);
    imagesCheck.addEventListener('change', () => {
      imagesCountSelect.disabled = !imagesAvailable || !imagesCheck.checked;
    });
    optionGrid.append(imagesField);

    // 联网查找资料（P3）：/meta.search.available=false 时禁用并给出配置引导。
    const searchInfo = meta?.search || {};
    const searchAvailable = Boolean(searchInfo.available);
    const searchCheck = el('input', { type: 'checkbox', disabled: !searchAvailable });
    const searchLimitSelect = select(
      Array.from({ length: 8 }, (_, index) => ({ value: String(index + 1), label: t('common.countItems', { count: index + 1 }) })),
      '5',
      { disabled: !searchAvailable, 'aria-label': t('classroom.wizard.webSourceCount') },
    );
    searchLimitSelect.disabled = !searchAvailable || !searchCheck.checked;
    const searchHelp = el('p', { class: 'help' }, searchAvailable
      ? t('classroom.wizard.beforeGeneratingTheOutlineRealWebPages', { v1: searchInfo.provider || 'DeepSeek' })
      : t('classroom.wizard.thisAccountHasNoWebSearchConnection'));
    const searchField = el('div', { class: 'cls-search-field full' },
      el('div', { class: 'cls-search-field-head' },
        el('label', { class: 'check-label' }, searchCheck, t('classroom.wizard.lookUpWebSourcesDeepseekSearch')),
        searchLimitSelect),
      searchHelp,
      !searchAvailable
        ? el('div', { class: 'row' },
          button(t('modelPicker.goSettings'), () => go('settings'), 'secondary small', 'sliders'))
        : null);
    searchCheck.addEventListener('change', () => {
      searchLimitSelect.disabled = !searchAvailable || !searchCheck.checked;
    });
    optionGrid.append(searchField);

    const normNote = el('p', { class: 'help cls-export-note' },
      t('classroom.wizard.afterGenerationDownloadPptxOfflineSingleFile')
      + t('classroom.wizard.orHaveTheServerExportAPdf')
      + t('classroom.wizard.coursesThatUsedWebSourcesIncludeA'));

    let picker = null;
    const actionRow = el('div', { class: 'row cls-setup-actions' });
    const errorRow = el('div', { class: 'cls-setup-error' });

    function showGenerateError(error) {
      const message = error.message || t('classroom.wizard.failedToCreateTheGenerationTask');
      if (/配图|图片|图像|联网|搜索/.test(message)) {
        errorRow.replaceChildren(el('div', { class: 'cls-generate-error row' },
          el('span', { class: 'help' }, message),
          button(t('modelPicker.goSettings'), () => go('settings'), 'secondary small', 'sliders')));
      } else {
        errorRow.replaceChildren();
      }
      toast(message, true);
    }

    setBody(loading(t('classroom.wizard.loadingModelConnections')));
    refreshProviders().then(() => {
      if (destroyed || !current(version)) return;
      const hasProviders = Array.isArray(state.providers) && state.providers.length > 0;
      if (!hasProviders) {
        setBody(
          card,
          notice(t('classroom.wizard.noAiModelConnectionConfiguredYetAdd'), 'warning'),
          el('div', { class: 'row' },
            button(t('modelPicker.goSettings'), () => go('settings'), '', 'sliders'),
            button(t('classroom.wizard.refreshConnections'), event => busy(event.currentTarget, () => renderSetup(options)), 'secondary', 'repeat')));
        return;
      }
      picker = createModelPicker('classroom', { showModeToggle: false });
      card.append(el('div', { class: 'cls-setup-models' }, el('h4', {}, t('classroom.wizard.primaryModelGeneratesTheOutlineAndScene')), picker.node), optionGrid);

      const startGeneration = generationStage => async (event) => {
        const trigger = event.currentTarget;
        try {
          const payload = picker.payload();
          const sceneTypes = typePicker.values();
          if (!sceneTypes.length) throw new Error(t('classroom.wizard.selectAtLeastOneSceneType'));
          const data = {
            stage: generationStage,
            provider_id: payload.provider_id,
            model: payload.model,
            scene_types: sceneTypes,
            scene_count: Number(countSelect.value) || 6,
            depth: depthSelect.value,
          };
          // 配图：可用时按开关传 images/images_per_scene；不可用时显式关闭（避免后端自动选择其它连接）。
          if (imagesAvailable) {
            data.images = imagesCheck.checked;
            if (imagesCheck.checked) data.images_per_scene = Number(imagesCountSelect.value) || 1;
          } else {
            data.images = false;
          }
          // 联网资料：能力可用且用户开启时才请求检索；不可用/未开启时显式传 false（后端默认亦为 false）。
          data.web_search = searchAvailable && searchCheck.checked;
          if (data.web_search) data.web_search_limit = Number(searchLimitSelect.value) || 5;
          trigger.disabled = true;
          errorRow.replaceChildren();
          const result = await api(`/classroom/courses/${courseId}/generate`, { method: 'POST', data });
          const notes = [];
          if (data.images === false) notes.push(t('classroom.wizard.noImagesThisTime'));
          if (data.web_search) notes.push(t('classroom.wizard.searchWebSourcesFirst', { v1: data.web_search_limit }));
          toast(t('classroom.wizard.classroomGenerationTaskCreatedGeneratingTheOutline', { v1: notes.length ? `（${notes.join(t('common.commaSeparator'))}）` : '' }));
          setHashParams('classroom', { course: courseId, view: 'generate', job: result.job.id });
          watchJob(result.job.id);
        } catch (error) {
          showGenerateError(error);
        } finally {
          if (trigger.isConnected) trigger.disabled = false;
        }
      };

      actionRow.replaceChildren(...[
        button(t('classroom.wizard.generateCourseOutlineScenes'), startGeneration('all'), '', 'sparkle'),
        hasOutline ? button(t('classroom.wizard.generateScenesOnlyReuseTheConfirmedOutline'), startGeneration('scenes'), 'secondary', 'layers') : null,
        button(t('classroom.wizard.refreshStatus'), event => busy(event.currentTarget, () => reload()), 'ghost small', 'repeat'),
      ].filter(Boolean));
      const searchHint = webSearchHint(course);
      card.append(actionRow, errorRow, normNote);
      if (searchHint) card.append(el('p', { class: 'help cls-search-history' }, searchHint));
      const knownSources = sourcesSection(asOutline(course.outline), { open: false });
      if (knownSources) card.append(knownSources);
      setBody(card);
    }).catch(() => {
      if (destroyed || !current(version)) return;
      setBody(card, notice(t('classroom.wizard.failedToLoadModelConnectionsCheckThe'), 'error'),
        button(t('common.reload'), event => busy(event.currentTarget, () => renderSetup(options)), 'secondary', 'repeat'));
    });
  }

  // -------------------------------------------------------------------------
  // 任务观察
  // -------------------------------------------------------------------------

  function watchJob(id) {
    if (destroyed || !current(version)) return;
    body.replaceChildren();
    jobHost.replaceChildren();
    mountJob(jobHost, id, null, {
      task: 'course',
      onSettled: job => {
        if (destroyed || !current(version)) return;
        if (!job) return;
        setJobHost();
        handleSettled(job);
      },
    });
  }

  async function reload() {
    try {
      await loadDetail();
    } catch (error) {
      if (!current(version)) return;
      setBody(notice(error.message || t('classroom.wizard.failedToLoadTheCourse'), 'error'), backButton());
      return;
    }
    refreshHeader();
    const course = detail.course || {};
    if (course.status === 'ready' && (detail.scenes || []).length) {
      renderDone(null);
      return;
    }
    renderSetup();
  }

  function handleSettled(job) {
    switch (job.status) {
      case 'awaiting_outline':
        // 刷新课程详情，让顶部状态从「草稿」更新为「大纲就绪」。
        loadDetail().then(() => {
          if (destroyed || !current(version)) return;
          refreshHeader();
          renderOutline(job);
        }).catch(() => renderOutline(job));
        break;
      case 'completed':
        loadDetail().then(() => {
          if (destroyed || !current(version)) return;
          refreshHeader();
          renderDone(job);
        }).catch(() => renderDone(job));
        break;
      case 'failed':
      case 'interrupted':
        renderFailed(job);
        break;
      case 'cancelled':
        renderCancelled();
        break;
      default:
        loadDetail().then(() => renderDone(job)).catch(() => renderDone(job));
    }
  }

  // -------------------------------------------------------------------------
  // 大纲确认（结构化编辑）
  // -------------------------------------------------------------------------

  function outlineRowsFrom(outline, course) {
    const scenes = outlineScenes(outline);
    if (scenes.length) {
      return scenes.map(item => ({
        title: String(item.title || ''),
        type: SCENE_TYPE_ORDER.includes(item.type) ? item.type : 'slide',
        outline_points: Array.isArray(item.outline_points) ? item.outline_points.map(point => String(point)) : [],
        description: String(item.description || ''),
        quiz: item.quiz && typeof item.quiz === 'object' ? item.quiz : null,
        widget: item.widget || item.widgetType || null,
        pbl: item.pbl && typeof item.pbl === 'object' ? item.pbl : null,
      }));
    }
    // 兼容「编辑后的纯文本大纲」：按行拆成场景标题。
    const raw = typeof outline === 'string' ? outline : '';
    const lines = raw.split('\n').map(line => line.replace(/^\s*#{1,6}\s*/, '').replace(/^\s*\d+[.、)]\s*/, '').trim()).filter(Boolean);
    if (lines.length) {
      return lines.map(title => ({ title: truncate(title, 60), type: 'slide', outline_points: [], description: '' }));
    }
    return [{ title: course?.title || t('classroom.wizard.courseImport'), type: 'slide', outline_points: [], description: '' }];
  }

  function renderOutline(job) {
    const outline = asOutline(job.result?.outline) || { title: detail?.course?.title || '', scenes: [] };
    const course = detail?.course || {};
    const rows = outlineRowsFrom(outline, course);
    const interactive = courseInteractive();
    const interactiveInfo = meta?.interactive_mode || {};
    const interactiveMax = Number(interactiveInfo.interactive_scenes_max) || Number(meta?.limits?.interactive_scenes_max) || 2;

    // 大纲 warnings（深度交互策略：超限改 slide / 缺 widget / 0 个 interactive 等）。
    const outlineWarnings = [];
    const pushWarnings = (list) => {
      for (const item of Array.isArray(list) ? list : []) {
        const text = String(item || '').trim();
        if (text && !outlineWarnings.includes(text)) outlineWarnings.push(text);
      }
    };
    pushWarnings(outline.warnings);
    pushWarnings(outline.outline_warnings);
    pushWarnings(job.result?.warnings);
    pushWarnings(job.result?.outline_warnings);
    pushWarnings(detail?.course?.outline?.warnings);
    const warningBlock = outlineWarnings.length
      ? el('div', { class: 'cls-outline-warnings' },
        el('strong', {}, t('classroom.wizard.outlinePrompt')),
        el('ul', { class: 'cls-warning-list' }, outlineWarnings.slice(0, 8).map(text => el('li', {}, text))))
      : null;

    // P3：来源预览（大纲确认前可展开核对；确认后生成场景会带上来源摘要）。
    const outlineWithSources = outlineSources(outline).length ? outline : (course.outline || {});
    const sourceBlock = sourcesSection(outlineWithSources, { open: true, label: t('classroom.wizard.referencesForThisCourse') });
    const searchMeta = job.result?.web_search || course.config?.web_search || {};
    const searchNotice = searchMeta && searchMeta.enabled && !searchMeta.used
      ? notice(
        t('classroom.wizard.noWebSourcesWereUsedThisTime', {
          v1: truncate(searchMeta.skipped || t('classroom.wizard.noUsableSourcesFound'), 120),
        })
        + t('classroom.wizard.theCourseIsGeneratedFromTheModel'), 'warning')
      : null;

    let titleValue = String(outline.title || course.title || detail?.course?.topic || '');
    let descriptionValue = String(outline.description || course.description || '');
    let languageDirective = String(outline.language_directive || t('classroom.wizard.teachInSimplifiedChineseExplainEachTerm'));

    const list = el('div', { class: 'cls-outline-list' });
    const outlineModeHint = el('p', { class: 'help cls-outline-mode-hint' });

    function refreshOutlineModeHint() {
      if (!interactive) {
        outlineModeHint.hidden = true;
        return;
      }
      const count = rows.filter(row => row.type === 'interactive').length;
      outlineModeHint.hidden = false;
      if (interactiveInfo.enabled === false) {
        outlineModeHint.textContent = t('classroom.wizard.thisInstanceDoesNotEnableImmersiveInteraction_2');
      } else if (count > interactiveMax) {
        outlineModeHint.textContent = t('classroom.wizard.thereAreInteractiveScenesOverTheLimit', { v1: count, v2: interactiveMax });
      } else if (count === 0) {
        outlineModeHint.textContent = t('classroom.wizard.noInteractiveScenesKeepAtLeast1');
      } else {
        outlineModeHint.textContent = t('classroom.wizard.immersiveInteractionInteractiveScenes1RecommendedSli', { v1: count, v2: interactiveMax });
      }
    }

    function moveRow(index, delta) {
      const target = index + delta;
      if (target < 0 || target >= rows.length) return;
      const [item] = rows.splice(index, 1);
      rows.splice(target, 0, item);
      paintRows();
    }

    function paintRows() {
      list.replaceChildren();
      rows.forEach((row, index) => {
        const pointsArea = el('textarea', {
          class: 'cls-points-input',
          rows: 3,
          value: row.outline_points.join('\n'),
          placeholder: t('classroom.wizard.onePointPerLine'),
          onInput: event => { row.outline_points = event.currentTarget.value.split('\n').map(item => item.trim()).filter(Boolean); },
        });
        list.append(el('div', { class: 'cls-outline-row' },
          el('div', { class: 'cls-outline-index' }, String(index + 1)),
          el('div', { class: 'cls-outline-fields' },
            el('div', { class: 'cls-outline-line' },
              input(t('classroom.wizard.sceneTitle'), {
                value: row.title,
                maxlength: 80,
                onInput: event => { row.title = event.currentTarget.value; },
              }),
              select(SCENE_TYPE_ORDER.map(type => ({ value: type, label: SCENE_TYPE_LABELS[type] })), row.type,
                { onChange: event => { row.type = event.currentTarget.value; } }),
              el('span', { class: 'row cls-outline-tools' },
                button('↑', () => moveRow(index, -1), 'ghost small', null, { disabled: index === 0, 'aria-label': t('classroom.wizard.moveSceneUp') }),
                button('↓', () => moveRow(index, 1), 'ghost small', null, { disabled: index === rows.length - 1, 'aria-label': t('classroom.wizard.moveSceneDown') }),
                button(t('common.delete'), () => { rows.splice(index, 1); paintRows(); }, 'ghost small', 'trash'))),
            pointsArea)));
      });
      refreshOutlineModeHint();
    }

    paintRows();

    const titleInput = input(t('classroom.wizard.courseTitle'), { value: titleValue, maxlength: 60, onInput: event => { titleValue = event.currentTarget.value; } });
    const descriptionInput = input(t('classroom.wizard.courseDescriptionOptional'), { value: descriptionValue, maxlength: 500, onInput: event => { descriptionValue = event.currentTarget.value; } });

    const confirmButton = button(t('classroom.wizard.confirmOutlineAndGenerateScenes'), null, '', 'check');
    confirmButton.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const cleanRows = rows
        .map(row => ({ ...row, title: String(row.title || '').trim(), outline_points: row.outline_points.map(point => String(point).trim()).filter(Boolean) }))
        .filter(row => row.title);
      if (!cleanRows.length) { toast(t('classroom.wizard.keepAtLeastOneScene'), true); return; }
      const payload = {
        title: String(titleValue || '').trim() || course.title || t('home.ai.unnamedCourse'),
        description: String(descriptionValue || '').trim(),
        language_directive: languageDirective,
        scenes: cleanRows.map((row, index) => {
          const scene = {
            id: `scene_${index + 1}`,
            title: row.title,
            type: row.type,
            description: row.description || '',
            outline_points: row.outline_points,
            order: index + 1,
          };
          if (row.type === 'quiz' && row.quiz) scene.quiz = row.quiz;
          if (row.type === 'quiz' && !row.quiz) scene.quiz = { question_count: 3, difficulty: 'medium' };
          if (row.type === 'interactive') scene.widget = row.widget || 'simulation';
          if (row.type === 'pbl' && row.pbl) scene.pbl = row.pbl;
          return scene;
        }),
      };
      try {
        trigger.disabled = true;
        await api(`/ai/jobs/${job.id}/confirm-outline`, {
          method: 'POST',
          data: { outline: JSON.stringify(payload), sections: cleanRows.map(row => row.title) },
        });
        toast(t('classroom.wizard.outlineConfirmedGeneratingContentSceneByScene'));
        setHashParams('classroom', { course: courseId, view: 'generate', job: job.id });
        watchJob(job.id);
      } catch (error) {
        toast(error.message || t('classroom.wizard.failedToConfirmTheOutline'), true);
      } finally {
        if (trigger.isConnected) trigger.disabled = false;
      }
    });

    setBody(
      el('section', { class: 'card cls-outline-card' },
        el('div', { class: 'card-header' },
          el('div', {},
            el('h3', {}, t('classroom.wizard.theOutlineIsReadyReviewAndConfirm')),
            el('p', { class: 'help' }, job.result?.notice || t('classroom.wizard.youCanChangeTheTitleAddRemove'))),
          badge(t('tasks.status.awaiting_outline'), 'amber')),
        el('div', { class: 'cls-outline-form' },
          field(t('classroom.wizard.courseTitle'), titleInput),
          field(t('classroom.wizard.courseDescription'), descriptionInput)),
        interactive ? el('div', { class: 'cls-outline-mode-row' }, badge(t('classroom.wizard.immersiveInteraction'), 'blue'), outlineModeHint) : outlineModeHint,
        warningBlock,
        searchNotice,
        sourceBlock,
        el('h4', { class: 'cls-outline-list-title' }, t('classroom.wizard.scenes', { v1: rows.length })),
        list,
        el('div', { class: 'row cls-outline-actions' },
          button(t('classroom.wizard.addScene'), () => {
            rows.push({ title: t('classroom.wizard.scene', { v1: rows.length + 1 }), type: 'slide', outline_points: [], description: '' });
            paintRows();
          }, 'secondary small', 'plus'),
          el('span', { class: 'cls-grow' }),
          button(t('tasks.cancelTask'), event => busy(event.currentTarget, async () => {
            await api(`/ai/jobs/${job.id}/cancel`, { method: 'POST' });
            toast(t('classroom.wizard.cancellationRequested'));
          }), 'secondary small', 'stop'),
          confirmButton)));
  }

  // -------------------------------------------------------------------------
  // 完成 / 失败 / 取消
  // -------------------------------------------------------------------------

  function sceneList() {
    const scenes = detail?.scenes || [];
    if (!scenes.length) return el('p', { class: 'help' }, t('classroom.wizard.noScenesYet'));
    const list = el('div', { class: 'cls-scene-result-list' });
    for (const scene of scenes) {
      const status = statusBadgeParts(scene.status, SCENE_STATUS);
      const canRegenerate = scene.status !== 'ready' || scene.meta?.placeholder;
      const meta = scene.meta || {};
      const content = scene.content || {};
      const imageAssets = Array.isArray(content.image_assets) ? content.image_assets : [];
      const imageCount = Number(meta.image_count) || (Array.isArray(meta.images) ? meta.images.length : 0) || imageAssets.length;
      const hasHint = Boolean(content.interactive_hint || meta.interactive_hint);
      const sceneWarnings = Array.isArray(meta.warnings) ? meta.warnings.filter(Boolean).map(String) : [];
      list.append(el('div', { class: 'cls-scene-result' },
        el('span', { class: 'cls-scene-result-index' }, String(scene.order ?? '')),
        el('div', { class: 'cls-scene-result-main' },
          el('strong', { 'data-no-i18n': '' }, scene.title || t('classroom.wizard.untitledScene')),
          el('p', { class: 'help' }, [typeLabel(scene.type), scene.meta?.placeholder ? t('classroom.wizard.contentPending') : null].filter(Boolean).join(' · ')),
          sceneWarnings.length
            ? el('ul', { class: 'cls-warning-list cls-scene-warnings' },
              sceneWarnings.slice(0, 3).map(text => el('li', { 'data-no-i18n': '' }, text)))
            : null),
        el('div', { class: 'row cls-scene-badges' },
          badge(status.label, status.variant),
          imageCount ? badge(t('classroom.wizard.image', { v1: imageCount }), 'blue') : null,
          hasHint ? badge(t('classroom.wizard.tryItYourself'), 'teal') : null,
          canRegenerate
            ? button(t('paper.card.regenerate'), () => regenerateScene(scene), 'secondary small', 'rotate')
            : null)));
    }
    return list;
  }

  async function lastCourseModel() {
    // 单场景重新生成需要 provider_id/model：优先复用最近一次 course 任务请求里的模型。
    try {
      const listing = await api('/ai/jobs');
      const match = (listing.items || []).find(job => job.task === 'course'
        && Number(job.request?.params?.course_id) === Number(courseId));
      if (match?.request?.provider_id && match?.request?.model) {
        return { provider_id: match.request.provider_id, model: match.request.model };
      }
    } catch {
      /* 任务列表不可用时回退到第一个连接的第一个模型 */
    }
    if (!Array.isArray(state.providers) || !state.providers.length) await refreshProviders();
    for (const provider of state.providers || []) {
      const models = provider.models || [];
      if (models.length) return { provider_id: provider.id, model: models[0] };
    }
    return null;
  }

  async function regenerateScene(scene) {
    const connection = await lastCourseModel();
    if (!connection) {
      toast(t('classroom.wizard.configureAnAiConnectionInModelSettings'), true);
      return;
    }
    try {
      const result = await api(`/classroom/courses/${courseId}/scenes/${scene.id}/regenerate`, {
        method: 'POST',
        data: { ...connection, instruction: t('classroom.wizard.regenerateThisSceneFromTheExistingOutline') },
      });
      toast(t('classroom.wizard.createdASingleSceneRegenerationTask'));
      setHashParams('classroom', { course: courseId, view: 'generate', job: result.job.id });
      watchJob(result.job.id);
    } catch (error) {
      toast(error.message || t('classroom.wizard.regenerationFailed'), true);
    }
  }

  function renderDone(job) {
    const course = detail?.course || {};
    const failed = job?.result?.failed_scene_ids?.length || 0;
    const summary = job?.result?.summary || t('classroom.wizard.courseGeneratedScenes', { v1: (detail?.scenes || []).length });
    // 汇总生成 warnings（大纲策略/配图失败/名册自动生成失败等），去重展示。
    const warnings = [];
    const pushWarnings = (list) => {
      for (const item of Array.isArray(list) ? list : []) {
        const text = String(item || '').trim();
        if (text && !warnings.includes(text)) warnings.push(text);
      }
    };
    pushWarnings(job?.result?.warnings);
    pushWarnings(job?.result?.outline_warnings);
    pushWarnings(job?.result?.roster_warnings);
    pushWarnings(asOutline(course.outline)?.warnings);
    pushWarnings(course.config?.warnings);
    for (const scene of detail?.scenes || []) pushWarnings(scene.meta?.warnings);
    const rosterWarnings = Array.isArray(job?.result?.roster_warnings) ? job.result.roster_warnings : [];
    const warningBlock = warnings.length
      ? el('div', { class: 'cls-done-warnings' },
        el('strong', {}, t('classroom.wizard.generationNotes', { v1: warnings.length })),
        el('ul', { class: 'cls-warning-list' }, warnings.slice(0, 8).map(text => el('li', {}, text))),
        rosterWarnings.length
          ? el('p', { class: 'help' }, t('classroom.wizard.aFailedAutoGeneratedCastDoesNot'))
          : null)
      : null;
    // P3：课程详情展示「参考来源 N 条」（可展开），导出对话框说明会附带来源页/清单。
    const doneOutline = asOutline(course.outline) || {};
    const doneSourceCount = outlineSources(doneOutline).length;
    const doneSources = sourcesSection(doneOutline, { open: false });
    setBody(el('section', { class: 'card cls-done-card' },
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('classroom.wizard.courseGenerationComplete')),
          el('p', { class: 'help' }, summary)),
        el('div', { class: 'row' },
          course.interactive_mode ? badge(t('classroom.wizard.immersiveInteraction'), 'blue') : null,
          badge(failed ? t('classroom.wizard.scenesFailed', { v1: failed }) : t('classroom.wizard.allReady'), failed ? 'red' : 'teal'))),
      failed ? notice(t('classroom.wizard.someScenesFailedToGenerateClickRegenerate'), 'warning') : null,
      warningBlock,
      doneSources,
      el('h4', { class: 'cls-outline-list-title' }, t('classroom.wizard.sceneList')),
      sceneList(),
      el('div', { class: 'row cls-done-actions' },
        button(t('classroom.wizard.enterClassroomPlayback'), () => go('classroom', null, { course: courseId, view: 'play' }), '', 'presentation'),
        button(t('classroom.wizard.exportClassroom'), () => {
          const dialog = openExportDialog({ courseId, courseTitle: course.title || '' });
          const sourceNote = el('p', { class: 'help cls-export-source-note' },
            doneSourceCount > 0
              ? t('classroom.wizard.thisCourseUsedWebSourcesTheLast', { v1: doneSourceCount })
              : t('classroom.wizard.thisCourseDidNotUseWebSources'));
          const buttonsRow = dialog.querySelector('.cls-export-buttons');
          if (buttonsRow) buttonsRow.before(sourceNote);
          else dialog.append(sourceNote);
        }, 'secondary', 'download'),
        button(t('classroom.wizard.regenerateTheWholeCourse'), event => busy(event.currentTarget, async () => {
          await loadDetail();
          renderSetup();
        }), 'secondary', 'repeat'),
        backButton()),
      el('p', { class: 'help cls-export-note' },
        t('classroom.wizard.exportPptxForLessonPrepASingle')
        + t('classroom.wizard.orHaveTheServerGenerateAPdf')
        + t('classroom.wizard.coursesThatUsedWebSourcesIncludeReferences'))));
  }

  function renderFailed(job) {
    setBody(
      notice(t('classroom.wizard.generationTask', {
        v1: job.status === 'interrupted' ? t('tasks.status.interrupted') : t('tasks.status.failed'),
        v2: job.error || t('settings.embeddingUnknownError'),
      }), 'error'),
      el('div', { class: 'row' },
        job.has_checkpoint
          ? button(t('job.stage.resuming'), event => busy(event.currentTarget, async () => {
            await api(`/ai/jobs/${job.id}/resume`, { method: 'POST' });
            toast(t('classroom.wizard.taskReDispatchedGenerationContinues'));
            watchJob(job.id);
          }), '', 'repeat')
          : null,
        button(t('classroom.wizard.chooseAModelAndGenerateAgain'), event => busy(event.currentTarget, () => reload()), 'secondary', 'sliders'),
        backButton()));
  }

  function renderCancelled() {
    setBody(
      notice(t('classroom.wizard.taskCancelledScenesGeneratedBeforeCancellationAre')),
      el('div', { class: 'row' },
        button(t('classroom.wizard.viewCourseAndGeneratedScenes'), event => busy(event.currentTarget, () => reload()), 'secondary', 'eye'),
        button(t('classroom.wizard.regenerateTheWholeCourse'), event => busy(event.currentTarget, () => { loadDetail().then(renderSetup); }), '', 'repeat'),
        backButton()));
  }

  // -------------------------------------------------------------------------
  // 启动
  // -------------------------------------------------------------------------

  async function init() {
    toolbar.replaceChildren(backButton(), el('span', { class: 'help' }, t('classroom.wizard.aiClassroomGenerationWorkbench')));
    setBody(loading(t('classroom.wizard.loadingCourse')));
    try {
      const [data, metaData] = await Promise.all([
        loadDetail(),
        api('/classroom/meta').catch(() => null),
      ]);
      detail = data;
      meta = metaData || {};
    } catch (error) {
      if (!current(version)) return;
      setBody(notice(error.message || t('classroom.wizard.failedToLoadTheCoursePleaseTry'), 'error'), backButton());
      return;
    }
    if (destroyed || !current(version)) return;
    refreshHeader();
    if (jobId) {
      watchJob(jobId);
      return;
    }
    const course = detail.course || {};
    if (course.status === 'ready' && (detail.scenes || []).length) {
      renderDone(null);
    } else {
      renderSetup();
    }
  }

  function destroy() {
    destroyed = true;
  }

  init();
  return { node: root, destroy };
}
