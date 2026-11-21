import { api, ensureCatalog, ensureKnowledgePoints, ensurePromptGrid } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, fill, icon, button, badge, empty, heading, loading, notice, field, input, select, query,
  dateText, downloadText, downloadBlob, busy, toast, wireTabKeyboard,
} from '../core/dom.js';
import { current, go, renderRoute } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker } from '../components/filePicker.js';
import { createPromptGrid } from '../components/promptGrid.js';
import { renderMindmapWidget, markdownToMindmapTree } from '../components/mindmap.js';
import { renderTopicReportsPanel, renderHandoutsPanel, m6t } from '../components/learningDocs.js';
import { t } from '../core/i18n.js';

// OCR 逐页状态由后端给出：text=资料自带文本层，pending=待识别，recognized=已识别，
// reviewed=人工校对，failed=识别失败。页面按覆盖度展示，而不是只显示“需 OCR”。
function pageNumberList(pages) {
  return [...new Set((pages || []).map(Number).filter(Number.isFinite))].sort((a, b) => a - b);
}

// 把页码压缩成 “1–3、7、31” 这样的可读区间（第 31 页与第 1 页不会互相吞掉）。
function pageRangesText(pages) {
  const numbers = pageNumberList(pages);
  if (!numbers.length) return t('common.none');
  const parts = [];
  for (let index = 0; index < numbers.length; index += 1) {
    const start = numbers[index];
    let end = start;
    while (index + 1 < numbers.length && numbers[index + 1] === end + 1) {
      end = numbers[++index];
    }
    parts.push(start === end ? `${start}` : `${start}–${end}`);
  }
  return parts.join('、');
}

function pageRanges(pages) {
  const ranges = [];
  for (const page of pageNumberList(pages)) {
    const last = ranges[ranges.length - 1];
    if (last && page === last.to + 1) last.to = page;
    else ranges.push({ from: page, to: page });
  }
  return ranges;
}

function coverageSummary(doc) {
  const coverage = (doc && doc.coverage) || {};
  const range = (doc && doc.ocr_range) || {};
  const totalPages = Number((doc && doc.pages) || coverage.pages) || 0;
  const pageFrom = Number(range.page_from) || Number(coverage.page_from) || 1;
  const pageTo = Number(range.page_to) || Number(coverage.page_to) || totalPages || 1;
  const covered = pageNumberList(coverage.covered_pages);
  const missing = pageNumberList(coverage.missing_pages);
  const failed = pageNumberList(coverage.failed_pages);
  const pending = pageNumberList(coverage.pending_pages)
    .filter(page => !failed.includes(page));
  const reviewed = pageNumberList(coverage.reviewed_pages);
  const pageCount = Number(coverage.page_count) || Math.max(0, pageTo - pageFrom + 1);
  return {
    covered,
    missing,
    failed,
    pending,
    reviewed,
    pageFrom,
    pageTo,
    pageCount,
    coveredCount: Number(coverage.covered_count ?? covered.length),
    complete: Boolean(coverage.complete),
    nextFrom: coverage.next_page_from ?? (missing.length ? missing[0] : null),
    nextTo: coverage.next_page_to ?? (missing.length ? missing[missing.length - 1] : null),
    docVersion: Number(doc && doc.doc_version) || 1,
  };
}

// 知识清单三个入口：我的笔记 / 资料库 / AI 整理。用模块级变量记住当前分区，
// 让 renderRoute 重新渲染（保存、回滚、任务完成）后仍停留在同一入口。
let noteSection = 'notes';
// M6 学习文档（专题报告 / 互动讲义）组件实例：页面重渲染或登出/401 时销毁，
// 关闭可能残留的弹层并移除 window 级监听；接口不可用时由组件内部中文降级。
let learningDocsControllers = [];

function destroyLearningDocs() {
  for (const controller of learningDocsControllers) {
    try { controller.destroy(); } catch { /* 单个组件清理失败不影响页面 */ }
  }
  learningDocsControllers = [];
}

// 登出/401 后清空分区记忆，避免下一个账号一进知识清单就落在 AI 整理表单。
onSessionReset(() => { noteSection = 'notes'; destroyLearningDocs(); });

function statusTag(text, tone, glyph) {
  return el('span', { class: 'pg-tag', 'data-tone': tone },
    glyph ? el('span', { class: 'pg-tag-glyph', 'aria-hidden': 'true' }, glyph) : null,
    text);
}

// OCR 四种状态全部来自现有 coverage 字段：
// covered → 可识别，pending_pages → 待识别，failed_pages → 失败，reviewed_pages → 人工校对。
function renderDocStatusTags(info) {
  const tags = el('span', { class: 'pg-status-row' });
  if (info.coveredCount > 0) tags.append(statusTag(t('notes.recognizablePages', { v1: info.coveredCount }), 'ok', '✓'));
  if (info.pending.length) tags.append(statusTag(t('notes.pagesToRecognize', { v1: info.pending.length }), 'warn', '!'));
  if (info.failed.length) tags.append(statusTag(t('notes.failedPages', { v1: info.failed.length }), 'bad', '✕'));
  if (info.reviewed.length) tags.append(statusTag(t('notes.manuallyReviewedPages', { v1: info.reviewed.length }), 'info', t('notes.r')));
  if (!tags.childNodes.length) tags.append(statusTag(t('home.importStatus.draft'), 'warn', '!'));
  return tags;
}

function renderDocCoverage(doc) {
  const info = coverageSummary(doc);
  if (!info.pageCount) return null;
  const rows = [
    el('span', {}, t('notes.coveredPagesRangePages', { v1: info.coveredCount, v2: info.pageCount, v3: info.pageFrom, v4: info.pageTo })),
  ];
  if (info.pending.length) {
    rows.push(el('span', { class: 'pg-ocr-flag', 'data-tone': 'warn' }, t('notes.toRecognizePage', { v1: pageRangesText(info.pending) })));
  }
  if (info.failed.length) {
    rows.push(el('span', { class: 'pg-ocr-flag', 'data-tone': 'bad' }, t('notes.recognitionFailedPage', { v1: pageRangesText(info.failed) })));
  }
  if (info.reviewed.length) rows.push(el('span', {}, t('notes.manuallyReviewedPage', { v1: pageRangesText(info.reviewed) })));
  if (!info.complete && info.nextFrom !== null) {
    rows.push(el('span', {}, t('notes.canContinueRecognizingPages', { v1: info.nextFrom, v2: info.nextTo })));
  }
  return el('div', { class: 'pg-ocr-coverage' },
    el('b', {}, t('notes.ocrCoverageMaterialV', { v1: info.docVersion })), ...rows);
}


export async function renderNotes(container, version, params = null) {
  // 重新渲染前先销毁上一轮 M6 学习文档组件（含打开的弹层），避免残留弹层与请求。
  destroyLearningDocs();
  const [notesRes, docsRes, gridSpec, catalog, kps] = await Promise.all([
    api('/notes'),
    api('/documents'),
    ensurePromptGrid(),
    ensureCatalog(),
    ensureKnowledgePoints(),
  ]);
  if (!current(version)) return;

  let allNotes = notesRes.items || [];
  let allDocs = docsRes.items || [];
  let noteOpenCounter = 0;
  // 资料列表刷新序号：OCR 完成、删除与索引重建可能并发触发刷新，旧响应丢弃。
  let docsRefreshSeq = 0;

  const composer = el('section', { class: 'card', id: 'notes-composer', style: 'margin-bottom:22px' });
  const manualComposer = el('section', { class: 'card hide', style: 'margin-bottom:22px' });
  const listArea = el('div');
  const detailArea = el('section', { class: 'card pg-note-detail' });

  const tabBar = el('div', { class: 'pg-mini-tabs', role: 'tablist', 'aria-label': t('notes.filterNotesBySource') });
  for (const [key, label] of [['all', t('home.notes.all')], ['private', t('notes.myNotes')], ['official', t('notes.officialList')]]) {
    tabBar.append(el('button', {
      type: 'button',
      class: 'pg-mini-tab',
      role: 'tab',
      'aria-selected': String(state.noteTab === key),
      onClick: () => {
        state.noteTab = key;
        state.selectedNote = null;
        renderList(true);
      },
    }, label));
  }
  wireTabKeyboard(tabBar);

  // Documents area — updates IN-PLACE without wiping the page or composer form (§9.1 bugfix!)
  const docCountBadge = badge(t('notes.documents', { v1: allDocs.length }));
  const docList = el('div');
  const uploadFeedback = el('div');
  const selectedDocIds = new Set();
  const docCheckboxesWrap = el('div', { class: 'doc-check-list' });

  function openOcrDialog(docItem, options = {}) {
    const dlg = el('dialog', { class: 'modal-dialog' });
    const ocrPicker = createModelPicker('ocr', { requireVision: true, showModeToggle: false });
    const info = coverageSummary(docItem);
    const totalPages = Math.max(1, Number(docItem.pages) || 1);
    const startFrom = Math.min(totalPages, Math.max(1, Number(info.nextFrom) || 1));
    const startTo = Math.min(totalPages, Math.max(startFrom, Number(info.nextTo) || totalPages));
    const pFrom = input(t('notes.startPage'), { type: 'number', value: startFrom, min: 1, max: totalPages });
    const pTo = input(t('notes.endPage'), {
      type: 'number', value: Math.min(startTo, startFrom + 4), min: 1, max: totalPages,
    });
    const statusBox = el('div');
    const retryBox = el('div');

    async function runOcr(payload, ranges) {
      // 按页合并：每段最多 30 页（服务端单次上限），失败页可单独成段重试。
      const jobs = [];
      for (const range of ranges) {
        const from = Math.max(1, Number(range.from) || 1);
        const to = Math.min(totalPages, Number(range.to) || 1);
        for (let start = from; start <= to; start += 30) {
          jobs.push({ from: start, to: Math.min(to, start + 29) });
        }
      }
      if (!jobs.length) throw new Error(t('notes.noPagesNeedRecognition'));
      // 逐段向服务端确认页数与费用，再一次性确认执行。
      const confirmations = [];
      for (const range of jobs) {
        const estimate = await api(`/documents/${docItem.id}/ocr`, {
          method: 'POST',
          data: { ...payload, page_from: range.from, page_to: range.to, confirmed: false },
        });
        confirmations.push(estimate.notice || t('notes.aboutToRecognizePagesWith', { v1: payload.model, v2: range.from, v3: range.to }));
      }
      if (!window.confirm(confirmations.join('\n\n'))) return null;
      const results = [];
      for (const range of jobs) {
        statusBox.replaceChildren(loading(t('notes.recognizingPagesWith', { v1: payload.model, v2: range.from, v3: range.to })));
        const response = await api(`/documents/${docItem.id}/ocr`, {
          method: 'POST',
          data: { ...payload, page_from: range.from, page_to: range.to, confirmed: true },
        });
        // 长耗时 OCR（>3 页或服务端强制异步）返回 {async:true, job}：改走任务流式进度。
        if (response && response.async === true && response.job?.id) {
          const result = await mountOcrJob(response, range);
          if (!result) return results.length ? results : null;
          results.push(result);
        } else {
          results.push(response);
        }
      }
      return results;
    }

    // 把异步 OCR 任务结果规整成与同步响应一致的形状，复用既有结果编辑/失败页重试界面。
    function ocrResultFromJob(doneJob) {
      const result = (doneJob && doneJob.result) || {};
      const document = result.document || (doneJob && doneJob.document) || null;
      const coverage = (document && document.coverage) || {};
      const failed = result.failed_pages
        || (document && document.failed_pages)
        || coverage.failed_pages
        || [];
      return {
        document,
        ocr_pages: result.ocr_pages || [],
        preserved_pages: result.preserved_pages || (document && document.preserved_pages) || [],
        failed_pages: failed,
        notices: result.notices || (result.notice ? [result.notice] : []),
        async_job: true,
        job_status: doneJob?.status || 'completed',
      };
    }

    // 挂载后台 OCR 任务：modal 内显示流式进度，任务终态后刷新资料列表与当前资料卡。
    function mountOcrJob(response, range) {
      return new Promise(resolve => {
        const progressMount = el('div', { class: 'pg-ocr-job-mount' });
        statusBox.replaceChildren(
          notice(t('notes.recognitionMovedToABackgroundTaskPages', { v1: range.from, v2: range.to })
            + (response.estimated_calls ? t('notes.estimatedModelCalls', { v1: response.estimated_calls }) : '')
            + t('notes.periodEnd')
            + t('notes.youCanCloseThisDialogAndOrganize')),
          progressMount);
        let finished = false;
        const finish = async job => {
          if (finished) return;
          finished = true;
          try { await refreshDocsInPlace(); } catch { /* 列表刷新失败不阻塞结果展示 */ }
          if (!job || !['completed', 'succeeded'].includes(job.status)) {
            statusBox.prepend(notice(t('notes.theOcrBackgroundTaskDidNotFinish'), 'warning'));
            resolve(null);
            return;
          }
          resolve(ocrResultFromJob(job));
        };
        mountJob(progressMount, response.job.id, job => { finish(job); }, {
          task: 'ocr',
          titleHint: `${docItem.title} · OCR`,
          onSettled: job => { finish(job); },
        });
      });
    }

    function retryFailedButton(failedPages) {
      return button(t('notes.retryFailedPagesOnlyPage', { v1: pageRangesText(failedPages) }), ev => busy(ev.currentTarget, async () => {
        const results = await runOcr(ocrPicker.payload(false), pageRanges(failedPages));
        if (!results) return;
        showResult(results);
        await refreshDocsInPlace();
      }), 'soft small', 'rotate');
    }

    function showResult(results) {
      const merged = results.reduce((accumulator, item) => {
        accumulator.ocr_pages.push(...(item.ocr_pages || []));
        accumulator.preserved.push(...(item.preserved_pages || []));
        accumulator.failed.push(...(item.failed_pages || []));
        accumulator.document = item.document || accumulator.document;
        accumulator.notices.push(...(item.notices || (item.notice ? [item.notice] : [])));
        return accumulator;
      }, { ocr_pages: [], preserved: [], failed: [], document: null, notices: [] });
      const preserved = pageNumberList(merged.preserved);
      const failed = pageNumberList(merged.failed);
      // 异步任务返回的 document 优先；否则用刚刷新的资料列表，避免覆盖度停留在旧值。
      const liveDoc = (merged.document && merged.document.id)
        ? merged.document
        : (allDocs.find(item => Number(item.id) === Number(docItem.id)) || docItem);
      const updated = coverageSummary(liveDoc);
      const blocks = [
        notice(t('notes.recognitionResultsMergedAndSavedPerPage', { v1: updated.coveredCount, v2: updated.pageCount, v3: updated.docVersion })),
      ];
      if (preserved.length) {
        blocks.push(notice(
          t('notes.pagesWerePreviouslyManuallyReviewedAndWere', { v1: pageRangesText(preserved) }),
          'warning',
        ));
      }
      if (failed.length) {
        const details = merged.ocr_pages
          .filter(page => failed.includes(Number(page.page)) && page.error)
          .map(page => t('notes.page', { v1: page.page, v2: page.error }))
          .join('；');
        blocks.push(notice(
          t('notes.pageFailedRecognitionOtherPagesWereSaved', { v1: pageRangesText(failed), v2: details ? `（${details}）` : '' }),
          'error',
        ));
      }
      // 后端 notices（异步任务必带）里可能还有人工校对保留、失败页等说明；去重后补展示。
      const shown = blocks.map(node => node.textContent).join('\n');
      for (const text of merged.notices) {
        if (text && !shown.includes(text)) blocks.push(notice(text));
      }
      const editable = merged.ocr_pages.filter(page => !preserved.includes(Number(page.page)));
      const editors = editable.map(page => ({
        page: Number(page.page),
        failed: page.state === 'failed' || Boolean(page.error),
        ta: el('textarea', {
          rows: 5,
          value: page.text || '',
          placeholder: page.state === 'failed' ? t('notes.recognitionFailedForThisPageYouCan') : '',
        }),
      }));
      if (editors.length) {
        blocks.push(notice(t('notes.toCorrectRecognizedTextEditItAnd')));
      }
      const saveButton = editors.length ? button(t('notes.saveReviewedTextMarkedAsManuallyReviewed'), ev => busy(ev.currentTarget, async () => {
        const saved = await api(`/documents/${docItem.id}/ocr-text`, {
          method: 'PATCH',
          data: { pages: editors.map(ed => ({ page: ed.page, text: ed.ta.value })) },
        });
        const savedCoverage = coverageSummary(saved);
        toast(savedCoverage.complete
          ? t('notes.reviewedTextSavedTheMaterialIsReady')
          : t('notes.reviewedTextSavedPagesInTheRange', { v1: savedCoverage.missing.length }),
        !savedCoverage.complete);
        dlg.close();
        await refreshDocsInPlace();
      }), '', 'check') : null;
      statusBox.replaceChildren(
        ...blocks,
        ...editors.map(ed => field(
          t('notes.pageRecognizedText', { v1: ed.page })
            + (ed.failed ? t('notes.recognitionFailedManual') : ''),
          ed.ta,
        )),
        el('div', { class: 'row end', style: 'margin-top:12px' }, saveButton),
      );
      if (failed.length) retryBox.replaceChildren(retryFailedButton(failed));
      else retryBox.replaceChildren();
      if (updated.complete) toast(t('notes.everyPageInTheRangeHasText'));
    }

    const runBtn = button(t('notes.confirmPagesAndRunVisualOcr'), e => busy(e.currentTarget, async () => {
      const from = Math.max(1, Number(pFrom.value) || 1);
      const to = Math.min(totalPages, Number(pTo.value) || from);
      if (to < from) throw new Error(t('notes.theEndPageCannotBeBeforeThe'));
      const results = await runOcr(ocrPicker.payload(false), [{ from, to }]);
      if (!results) return;
      showResult(results);
      await refreshDocsInPlace();
    }), '', 'scan');

    const coverageNotices = [];
    coverageNotices.push(
      t('notes.thisPdfHasPagesTheRecognitionRange', { v1: totalPages, v2: info.pageFrom, v3: info.pageTo, v4: info.coveredCount, v5: info.pageCount }),
    );
    if (info.pending.length) {
      coverageNotices.push(t('notes.toRecognizePageTheNextRangeAvailable', { v1: pageRangesText(info.pending), v2: info.nextFrom, v3: info.nextTo }));
    }
    if (info.failed.length) {
      coverageNotices.push(t('notes.recognitionFailedPageYouCanRetryJust', { v1: pageRangesText(info.failed) }));
    }
    if (info.reviewed.length) {
      coverageNotices.push(t('notes.manuallyReviewedPageReRecognitionKeepsThe', { v1: pageRangesText(info.reviewed) }));
    }

    dlg.append(
      el('div', { class: 'card-header' }, el('h3', {}, t('notes.scanOcr', { v1: docItem.title })), button(t('common.close'), () => dlg.close(), 'ghost small', 'close')),
      notice(coverageNotices.join(' '), info.failed.length ? 'warning' : ''),
      ocrPicker.node,
      el('div', { class: 'form-grid', style: 'margin:12px 0' }, field(t('notes.startPage'), pFrom), field(t('notes.endPageMax30PerRun'), pTo)),
      el('div', { class: 'row end', style: 'margin-bottom:12px' }, runBtn),
      retryBox,
      statusBox
    );
    document.body.append(dlg);
    dlg.addEventListener('close', () => dlg.remove());
    dlg.showModal();
    if (info.failed.length && options.retryFailed) {
      retryBox.replaceChildren(retryFailedButton(info.failed));
    }
  }

  function renderDocsUI() {
    docCountBadge.textContent = t('notes.documents', { v1: allDocs.length });
    docList.replaceChildren();
    docCheckboxesWrap.replaceChildren();

    if (allDocs.length) {
      for (const d of allDocs) {
        const info = coverageSummary(d);
        const canOcr = d.status !== 'ready';
        docList.append(el('div', { class: 'pg-doc-item', 'data-document-id': String(d.id) },
          el('span', { class: 'pg-doc-icon' }, icon('file')),
          el('div', { class: 'pg-doc-main' },
            el('div', { class: 'pg-doc-head' },
              el('strong', { class: 'pg-doc-title' }, d.title),
              d.index_status === 'stale' ? statusTag(t('notes.indexNeedsRebuilding'), 'warn', '!') : null,
              renderDocStatusTags(info)),
            el('p', { class: 'pg-doc-meta' }, t('notes.pagesChunks', { v1: d.pages || 0, v2: d.chunks || 0 })),
            renderDocCoverage(d),
            info.failed.length
              ? el('p', { class: 'pg-doc-hint' },
                t('notes.somePagesFailedRecognitionYouCanRetry'),
                button(t('notes.retryPage', { v1: pageRangesText(info.failed) }), () => openOcrDialog(d, { retryFailed: true }),
                  'ghost small', 'rotate'))
              : null),
          el('div', { class: 'pg-doc-actions' },
            d.index_status === 'stale'
              ? button(t('notes.rebuildIndex'), e => busy(e.currentTarget, async () => {
                // 4.5/3.2-12：OCR 校对后向量可能过期，需要显式重建语义索引。
                const res = await api(`/documents/${d.id}/reindex`, { method: 'POST' });
                toast(res.notice || t('notes.indexRebuiltSemanticSearchIsAvailableAgain'));
                await refreshDocsInPlace();
              }), 'soft small', 'rotate')
              : null,
            canOcr ? button(d.status === 'partial' ? t('notes.continueOcr') : t('notes.ocrRecognition'),
              () => openOcrDialog(d), 'soft small', 'scan') : null,
            info.failed.length ? button(t('notes.retryFailedPagesOnly'),
              () => openOcrDialog(d, { retryFailed: true }), 'ghost small', 'rotate') : null,
            // M3：按资料查看已保存的原文摘录（失效可重新定位、可删除）。
            button(tm('study.m3.viewExcerpts'), () => openDocExcerptDialog(d), 'ghost small', 'notes'),
            button('', e => busy(e.currentTarget, async () => {
              if (!window.confirm(t('notes.deleteMaterial', { v1: d.title }))) return;
              await api(`/documents/${d.id}`, { method: 'DELETE' });
              selectedDocIds.delete(d.id);
              await refreshDocsInPlace();
            }), 'ghost small', 'trash', { 'aria-label': t('notes.delete', { v1: d.title }) }))));

        if (d.status === 'ready') {
          const cb = el('input', {
            type: 'checkbox',
            checked: selectedDocIds.has(d.id),
            onChange: () => {
              if (cb.checked) selectedDocIds.add(d.id);
              else selectedDocIds.delete(d.id);
              updateMapEstimate();
            },
          });
          docCheckboxesWrap.append(el('label', { class: 'doc-check-pill' }, cb, el('span', {}, t('notes.chunks', { v1: d.title, v2: d.chunks }))));
        }
      }
    } else {
      docList.append(el('p', { class: 'help' }, t('notes.noMaterialsYetUploadSlidesNotesOr')));
    }
    if (!docCheckboxesWrap.childNodes.length) {
      docCheckboxesWrap.append(el('p', { class: 'help' }, t('notes.noTextMaterialsAvailableUploadAboveOr')));
    }
    updateMapEstimate();
  }

  async function refreshDocsInPlace() {
    const mine = ++docsRefreshSeq;
    const res = await api('/documents');
    if (mine !== docsRefreshSeq) return; // 旧刷新响应丢弃，避免覆盖刚更新的资料卡
    allDocs = res.items || [];
    renderDocsUI();
  }

  const docPicker = filePicker(
    t('notes.addStudyMaterialWithoutRefreshingTheForm'),
    t('notes.supportsPdfDocxTxtAndMarkdownSmart'),
    '.pdf,.docx,.txt,.md',
    async file => {
      uploadFeedback.replaceChildren(loading(t('notes.parsingTheMaterialByHeadingStructure')));
      try {
        const fd = new FormData();
        fd.set('file', file);
        const result = await api('/documents', { method: 'POST', body: fd });
        uploadFeedback.replaceChildren();
        if (result.status === 'ready') selectedDocIds.add(result.id);
        const coverage = result.coverage || {};
        toast(result.status === 'needs_ocr'
          ? t('notes.detectedScannedPagesWithNoExtractableText', { v1: result.pages })
          : result.status === 'partial'
            ? t('notes.materialHasTextOnOnlySomePages', { v1: result.title, v2: coverage.covered_count || 0, v3: coverage.page_count || result.pages })
            : t('notes.materialParsedIntoChunks', { v1: result.title, v2: result.chunks }));
        await refreshDocsInPlace();
      } catch (error) {
        uploadFeedback.replaceChildren(notice(error.message, 'error'));
      }
    }
  );
  docPicker.style.marginTop = '14px';
  docPicker.style.padding = '16px 12px';

  const docsArea = el('section', { class: 'card', style: 'margin-bottom:20px' },
    el('div', { class: 'card-header' }, el('h3', {}, t('notes.myMaterials')), docCountBadge),
    docsRes.embedding_enabled ? notice(t('notes.semanticSearchIsEnabledOnThisSite')) : null,
    docList, docPicker, uploadFeedback);

  const toggleManual = () => {
    manualComposer.classList.toggle('hide');
    if (!manualComposer.classList.contains('hide')) manualComposer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  // Manual Note Composer (also allows linking to Subject & KP for official notes!)
  const manualTitle = input(t('notes.eGGeometricMeaningOfDerivativesKnowledge'));
  const manualSubj = select([{ value: '', label: t('notes.linkedSubjectOptional') }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))]);
  const manualKp = select([{ value: '', label: t('notes.linkedKnowledgePointOptional') }]);
  manualSubj.addEventListener('change', () => {
    const filtered = kps.filter(k => !manualSubj.value || k.subject === manualSubj.value);
    manualKp.replaceChildren(
      el('option', { value: '' }, t('notes.linkedKnowledgePointOptional')),
      ...filtered.map(k => el('option', { value: k.id }, `${k.level === 1 ? t('notes.chapterPrefix') : k.level === 2 ? '▸ ' : '\u3000· '}${k.name}`))
    );
  });
  const manualContent = el('textarea', { class: 'note-editor', rows: 9, placeholder: t('notes.supportsMarkdownAndLatexFormulasAnAdministrator') });
  manualComposer.append(
    el('div', { class: 'card-header' }, el('h2', {}, t('notes.newKnowledgeNote')), button(t('question.collapse'), toggleManual, 'ghost small')),
    field(t('notes.noteTitle'), manualTitle),
    el('div', { class: 'form-grid', style: 'margin-top:12px' }, field(t('common.subject'), manualSubj), field(t('practice.knowledgePoint'), manualKp)),
    el('div', { style: 'margin-top:14px' }, field(t('notes.body'), manualContent)),
    el('div', { class: 'row end', style: 'margin-top:16px' },
      button(t('notes.saveNote'), e => busy(e.currentTarget, async () => {
        if (!manualTitle.value.trim() || !manualContent.value.trim()) throw new Error(t('notes.pleaseFillInTheNoteTitleAnd'));
        const note = await api('/notes', {
          method: 'POST',
          data: {
            title: manualTitle.value.trim(),
            content: manualContent.value,
            subject: manualSubj.value || null,
            kp_id: manualKp.value ? Number(manualKp.value) : null,
          },
        });
        state.selectedNote = note.id;
        state.noteTab = 'private';
        toast(t('notes.noteSavedAndRecordedAsVersion1'));
        await renderRoute();
      }), '', 'check'))
  );

  // AI Notes Agent Composer（三步：选资料 → 设目标 → 确认生成；高级项收进 <details>）
  const pre = state.notePreselectKp;
  state.notePreselectKp = null;

  const sourceModeSel = select([
    { value: 'user_docs', label: t('notes.useOnlyMyUploadedMaterialsMultiDocument') },
    { value: 'official_plus_docs', label: t('notes.officialListAsTheSkeletonMyMaterials') },
    { value: 'wrongbook', label: t('notes.generateAPersonalMistakeListFromMy') },
  ], pre?.source_mode || 'user_docs');

  const agentSubj = select(
    [{ value: '', label: t('notes.chooseASubjectEnablesSubjectSpecificPrompts') }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    pre?.subject || ''
  );
  const agentKp = select([{ value: '', label: t('notes.linkedKnowledgePointOptionalLinksTheQuestion') }]);
  function populateAgentKps() {
    const filtered = kps.filter(k => !agentSubj.value || k.subject === agentSubj.value);
    agentKp.replaceChildren(
      el('option', { value: '' }, t('notes.linkedKnowledgePointOptional')),
      ...filtered.map(k => el('option', { value: k.id }, `${k.level === 1 ? t('notes.chapterPrefix') : k.level === 2 ? '▸ ' : '\u3000· '}${k.name}`))
    );
    if (pre?.kp_id) agentKp.value = String(pre.kp_id);
  }
  populateAgentKps();

  const requirements = el('textarea', {
    rows: 2,
    placeholder: t('notes.optionalAdditionalRequirementsL4OnlyAffectsForm'),
  });
  const promptGridComp = createPromptGrid(gridSpec, {
    initialConfig: pre?.source_mode === 'wrongbook' ? { format: 'mistakes', extras: ['my_wrongs'] } : null,
    onChange: ({ customRequirements }) => {
      if (customRequirements !== undefined) requirements.value = customRequirements;
      updateEstimate();
    },
  });
  agentSubj.addEventListener('change', () => { populateAgentKps(); promptGridComp.setSubject(agentSubj.value); });
  requirements.addEventListener('input', () => promptGridComp.setRequirements(requirements.value));

  const confirmOutlineChk = el('input', { type: 'checkbox', checked: false });
  const mapExtractChk = el('input', { type: 'checkbox', checked: false, 'aria-describedby': 'notes-map-estimate' });
  const mapEstimate = el('p', { class: 'help', id: 'notes-map-estimate', 'aria-live': 'polite' });
  function updateMapEstimate() {
    const chunks = allDocs
      .filter(d => d.status === 'ready' && selectedDocIds.has(d.id))
      .reduce((count, d) => count + d.chunks, 0);
    mapExtractChk.disabled = chunks === 0;
    mapEstimate.textContent = chunks
      ? t('notes.aboutModelCallsBeforeTheOutlineBlocks', { v1: chunks })
      : t('notes.availableAfterCheckingParsedMaterials');
    updateEstimate();
  }
  const controls = createModelPicker('notes', { showModeToggle: true });
  const jobMount = el('div');

  // AI 整理任务终态回调：清掉 job 指针并刷新笔记列表与结果区。
  // 路由切走再回来时 mountJob 会重新挂载并再次触发（结果已持久化，不重复计费）。
  async function handleNoteJobComplete(doneJob) {
    state.noteJobId = null;
    const noteId = doneJob?.result?.note_id;
    try {
      const refreshed = await api('/notes');
      allNotes = refreshed.items || [];
      renderList(false);
      if (noteId) {
        state.selectedNote = noteId;
        await openNote(noteId);
      }
    } catch (error) {
      toast(t('notes.failedToRefreshTheNoteResult', { v1: error.message }), true);
    }
  }

  const docPickerField = el('div', { style: 'margin:14px 0' },
    el('div', { class: 'field-label', style: 'margin-bottom:8px' }, t('notes.chooseMaterialsToOrganizeMultiSelect')),
    docCheckboxesWrap);

  const estimateBox = el('div', { class: 'pg-estimate', 'aria-live': 'polite' });

  function selectedDocStats() {
    const docs = allDocs.filter(d => selectedDocIds.has(d.id));
    return { count: docs.length, chunks: docs.reduce((sum, d) => sum + Number(d.chunks || 0), 0) };
  }

  function modelSummary() {
    try {
      const payload = controls.payload(false);
      return payload.cross_verify ? t('notes.crossVerified', { v1: payload.model, v2: payload.verify_model }) : payload.model;
    } catch (error) {
      return t('notes.notReadyYet', { v1: error.message });
    }
  }

  // 生成前把来源、资料块估算、模型与高级项摊开，避免用户盲点付费调用。
  function updateEstimate() {
    const mode = sourceModeSel.value;
    const stats = selectedDocStats();
    const modeLabel = ({
      user_docs: t('notes.myUploadedMaterials'),
      official_plus_docs: t('notes.officialListSkeletonMyMaterials'),
      wrongbook: t('notes.myWrongAnswerMistakeList'),
    })[mode] || mode;
    const extra = mode !== 'wrongbook' && mapExtractChk.checked ? t('notes.extractBlockByBlockThenGenerateThe') : t('notes.generateDirectlyFromTheMaterialStructure');
    estimateBox.replaceChildren(
      el('p', {}, el('strong', {}, t('notes.confirmBeforeGenerating')), t('notes.sourceChunkEstimateAndModelsUsed')),
      el('ul', { class: 'pg-estimate-list' },
        el('li', {}, t('notes.source', { v1: modeLabel })),
        el('li', {}, mode === 'wrongbook'
          ? t('notes.materialChunksUsesYourWrongAnswerRecords')
          : t('notes.materialChunksDocumentsSelectedAboutChunks', { v1: stats.count, v2: stats.chunks })),
        el('li', {}, t('notes.modelsUsed', { v1: modelSummary() })),
        el('li', {}, t('notes.generationMethod', { v1: extra })
          + (confirmOutlineChk.checked ? t('notes.generateOutlineFirstPause') : ''))),
      el('p', { class: 'help' }, t('notes.noExtraPaidCallsAreMadeAutomatically')));
  }

  sourceModeSel.addEventListener('change', () => {
    docPickerField.classList.toggle('hide', sourceModeSel.value === 'wrongbook');
    updateEstimate();
  });
  if (sourceModeSel.value === 'wrongbook') docPickerField.classList.add('hide');
  agentSubj.addEventListener('change', updateEstimate);
  agentKp.addEventListener('change', updateEstimate);
  confirmOutlineChk.addEventListener('change', updateEstimate);
  mapExtractChk.addEventListener('change', updateMapEstimate);
  requirements.addEventListener('input', updateEstimate);
  controls.node.addEventListener('change', updateEstimate);
  promptGridComp.node.addEventListener('click', updateEstimate);

  const generateButton = button(t('notes.startTheKnowledgeOrganizingAgent'), e => busy(e.currentTarget, async () => {
    const sMode = sourceModeSel.value;
    const docIds = [...selectedDocIds];
    if (sMode === 'user_docs' && !docIds.length) {
      throw new Error(t('notes.pleaseCheckAtLeastOneParsedStudy'));
    }
    const payload = controls.payload(false);
    const cfg = promptGridComp.getConfig();
    const job = await api('/ai/jobs', {
      method: 'POST',
      data: {
        ...payload,
        task: 'notes',
        source_mode: sMode,
        document_id: docIds[0] || null,
        document_ids: docIds,
        subject: agentSubj.value || null,
        kp_ids: agentKp.value ? [Number(agentKp.value)] : [],
        // P0：输出形态由「方格选择」直接决定，不再把思维导图/Anki/对比表等强压成 checklist。
        template: cfg.format,
        prompt_config: cfg,
        confirm_outline: confirmOutlineChk.checked,
        map_extract: sMode !== 'wrongbook' && docIds.length > 0 && mapExtractChk.checked,
        requirements: requirements.value.trim(),
      },
    });
    state.noteJobId = job.id;
    mountJob(jobMount, job.id, handleNoteJobComplete);
  }), '', 'sparkle');

  const advancedDetails = el('details', { class: 'pg-advanced' },
    el('summary', {}, t('notes.advancedSettingsModelsMultiModelVerificationOutline')),
    el('div', { class: 'pg-advanced-body' },
      el('div', {},
        el('div', { class: 'field-label', style: 'margin-bottom:8px' }, t('notes.modelsCrossVerification')),
        controls.node),
      el('label', { class: 'check-label', style: 'margin-top:12px' },
        confirmOutlineChk, t('notes.humanInTheLoopGenerateTheOutline')),
      el('label', { class: 'check-label', style: 'margin-top:10px' },
        mapExtractChk, t('notes.extractConceptsFormulasConditionsAndExamplesBlock')),
      mapEstimate));

  const stepOne = el('section', { class: 'pg-step', 'aria-label': t('notes.step1ChooseMaterials') },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '1'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, t('notes.chooseMaterials')),
      el('p', { class: 'pg-card-sub' }, t('notes.generateFromMyMaterialsOfficialListMaterials')),
      field(t('notes.sourceMode'), sourceModeSel),
      docPickerField));

  const stepTwo = el('section', { class: 'pg-step', 'aria-label': t('notes.step2SetGoalsAndOutputFormat') },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '2'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, t('notes.setGoalsAndOutputFormat')),
      el('p', { class: 'pg-card-sub' },
        t('notes.chooseOutputFormatStudyStageDetailLevel')),
      el('div', { class: 'pg-form-grid' },
        field(t('notes.subjectAdaptationL2'), agentSubj),
        field(t('notes.linkedKnowledgePointUsedToCallQuestion'), agentKp)),
      el('div', { class: 'pg-grid-config', style: 'margin-top:16px' },
        el('div', {},
          el('div', { class: 'field-label', style: 'margin-bottom:8px' }, t('notes.promptGridCombination6DimensionsSuchAs')),
          promptGridComp.node),
        field(t('notes.customAdditionalRequirementsL4'), requirements))));

  const stepThree = el('section', { class: 'pg-step', 'aria-label': t('notes.step3ConfirmGeneration') },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '3'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, t('notes.confirmGeneration')),
      advancedDetails,
      estimateBox,
      el('div', { class: 'pg-form-actions' }, generateButton)));

  composer.append(
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('p', { class: 'pg-kicker' }, t('notes.aiOrganizationThreeSteps')),
        el('h2', {}, t('notes.fromMaterialsToKnowledgeNotes')),
        el('p', { class: 'pg-card-sub' },
          t('notes.chooseMaterialsSetGoalsAndOutputFormat')))),
    el('div', { class: 'pg-steps' }, stepOne, stepTwo, stepThree),
    jobMount);
  updateEstimate();

  // 任务中心深链（与 tasks.js 深链约定一致）：#/notes?note=<id> 打开指定笔记；
  // #/notes?document=<id> 打开资料库并定位该资料；#/notes?section=docs 只切到资料库。
  const deepNoteId = Number(params?.get?.('note') || 0);
  const deepDocumentId = Number(params?.get?.('document') || 0);
  const deepSection = String(params?.get?.('section') || '');
  if (deepNoteId) state.selectedNote = deepNoteId;

  // 打开生成的笔记 / 从首页或讲解卡片跳进来时，优先展示被选中的笔记，
  // 清掉上次 AI 任务与分区残留，避免一进知识清单又跳回 AI 表单。
  if (deepNoteId) {
    noteSection = 'notes';
  } else if (deepDocumentId || deepSection === 'docs') {
    noteSection = 'docs';
  } else if (state.selectedNote) {
    noteSection = 'notes';
  } else if (pre || state.noteJobId) {
    noteSection = 'ai';
    if (pre) toast(t('notes.presetTheMistakeListGenerationFor', { v1: pre.name }));
  }

  // ---------- 三个入口：我的笔记 / 资料库 / AI 整理 ----------
  const sectionDefs = [
    ['notes', t('notes.myNotes'), 'notes'],
    ['docs', t('home.activity.library'), 'file'],
    ['ai', t('notes.aiOrganize'), 'sparkle'],
  ];
  const sectionTabs = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': t('notes.knowledgeNoteSections') });
  const sectionButtons = new Map();
  for (const [key, label, iconName] of sectionDefs) {
    const tab = el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      id: `notes-tab-${key}`,
      'aria-controls': `notes-panel-${key}`,
      'aria-selected': 'false',
      onClick: () => switchSection(key),
    }, icon(iconName), label);
    sectionButtons.set(key, tab);
    sectionTabs.append(tab);
  }
  wireTabKeyboard(sectionTabs);

  const noteCount = el('span', { class: 'pg-filter-summary' });
  const notesPanel = el('section', {
    class: 'pg-panel', role: 'tabpanel', id: 'notes-panel-notes', 'aria-labelledby': 'notes-tab-notes',
  },
    manualComposer,
    el('div', { class: 'pg-list-toolbar' },
      el('div', { class: 'pg-list-toolbar-main' }, tabBar, noteCount)),
    el('div', { class: 'note-layout' }, el('div', {}, listArea), detailArea));

  const docsPanel = el('section', {
    class: 'pg-panel', role: 'tabpanel', id: 'notes-panel-docs', 'aria-labelledby': 'notes-tab-docs',
  },
    docsArea,
    el('p', { class: 'pg-panel-note' },
      t('notes.scansShowOcrStatusPerPageRecognizable')));

  const aiPanel = el('section', {
    class: 'pg-panel', role: 'tabpanel', id: 'notes-panel-ai', 'aria-labelledby': 'notes-tab-ai',
  }, composer);

  const panels = { notes: notesPanel, docs: docsPanel, ai: aiPanel };
  function switchSection(key, { scroll = false } = {}) {
    noteSection = panels[key] ? key : 'notes';
    for (const [name, panel] of Object.entries(panels)) panel.hidden = name !== noteSection;
    for (const [name, tab] of sectionButtons) tab.setAttribute('aria-selected', String(name === noteSection));
    if (scroll) sectionTabs.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (noteSection === 'ai') updateEstimate();
  }

  // M6 学习文档：在既有三个入口与阅读区之后追加「专题报告 / 互动讲义」两个折叠区块，
  // 服务端规则汇编内容只读陈列（创建/编辑/导出/删除由组件接管）。区块放在页面底部，
  // 不改动既有 tab 与列表渲染逻辑；接口不可用时组件内部显式中文降级，不影响笔记/资料/摘录。
  const topicReportsPanel = renderTopicReportsPanel({ heading: false });
  const handoutsPanel = renderHandoutsPanel({ heading: false });
  learningDocsControllers = [topicReportsPanel, handoutsPanel];
  const learningDocsSection = el('section', {
    class: 'm6-learning-docs',
    id: 'notes-learning-docs',
    'aria-label': m6t('sectionTitle'),
  },
  el('div', { class: 'm6-learning-docs-head' },
    el('p', { class: 'pg-kicker' }, m6t('sectionEyebrow')),
    el('h2', { class: 'm6-learning-docs-title' }, m6t('sectionTitle')),
    el('p', { class: 'help' }, m6t('sectionHint'))),
  el('details', { class: 'm6-fold', 'data-m6-fold': 'topic-reports' },
    el('summary', { class: 'm6-fold-summary' },
      el('span', { class: 'm6-fold-title' }, m6t('topicReportsTitle')),
      el('span', { class: 'm6-fold-note' }, m6t('topicReportsSummary'))),
    topicReportsPanel.element),
  el('details', { class: 'm6-fold', 'data-m6-fold': 'handouts' },
    el('summary', { class: 'm6-fold-summary' },
      el('span', { class: 'm6-fold-title' }, m6t('handoutsTitle')),
      el('span', { class: 'm6-fold-note' }, m6t('handoutsSummary'))),
    handoutsPanel.element));

  container.replaceChildren(
    el('div', { class: 'pg-page-head' },
      heading('KNOWLEDGE WORKSPACE', t('notes.connectScatteredMaterialsIntoKnowledge'),
        t('notes.myNotesTheMaterialLibraryAndAi'),
        el('div', { class: 'pg-actions' },
          button(t('notes.newNote'), () => { switchSection('notes'); toggleManual(); }, 'secondary', 'edit'),
          button(t('notes.organizeMaterialsWithAi'), () => switchSection('ai'), '', 'sparkle')))),
    sectionTabs,
    notesPanel, docsPanel, aiPanel, learningDocsSection);
  switchSection(noteSection);

  renderDocsUI();
  // 任务中心「资料 OCR」深链：资料库已渲染后滚动定位到目标资料卡（不自动打开 OCR 弹窗）。
  if (deepDocumentId) {
    const target = docList.querySelector(`[data-document-id="${deepDocumentId}"]`);
    if (target) {
      target.classList.add('is-deep-linked');
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }
  if (state.noteJobId) mountJob(jobMount, state.noteJobId, handleNoteJobComplete);

  // Fix L432/L433/L434: single clean openNote flow with counter guard & accurate active class!
  function highlightActiveNote(id) {
    listArea.querySelectorAll('.note-card').forEach(card => {
      const active = Number(card.dataset.noteId) === Number(id);
      card.classList.toggle('active', active);
      card.setAttribute('aria-current', active ? 'true' : 'false');
    });
  }

  async function openNote(id) {
    const seq = ++noteOpenCounter;
    state.selectedNote = id;
    highlightActiveNote(id);
    detailArea.replaceChildren(loading(t('notes.openingTheNote')));
    try {
      const note = await api(`/notes/${encodeURIComponent(id)}`);
      if (!current(version) || seq !== noteOpenCounter) return;
      renderNoteDetail(note, detailArea, () => openNote(id), patch => {
        // 重命名后同步更新内存列表与侧栏标题（§2.4）：不整页重渲染，保留阅读位置。
        if (patch?.title) {
          note.title = patch.title;
          const item = allNotes.find(n => Number(n.id) === Number(id));
          if (item) item.title = patch.title;
        }
        renderList(false);
        highlightActiveNote(id);
      });
    } catch (error) {
      if (seq === noteOpenCounter) detailArea.replaceChildren(notice(error.message, 'error'));
    }
  }

  function renderList(autoOpen = false) {
    tabBar.querySelectorAll('button').forEach((n, i) => {
      const key = ['all', 'private', 'official'][i];
      n.classList.toggle('active', state.noteTab === key);
      n.setAttribute('aria-selected', String(state.noteTab === key));
    });
    const items = allNotes.filter(n => state.noteTab === 'all' || n.origin === state.noteTab);
    noteCount.textContent = t('notes.notes', { v1: items.length });
    listArea.replaceChildren();
    if (!items.length) {
      // Fix empty-state copy mismatch (§9.1): match actual button label "AI 整理资料"
      listArea.append(el('div', { class: 'card' }, empty(
        state.noteTab === 'official' ? t('notes.noOfficialListPublishedYet') : t('notes.noKnowledgeNotesYet'),
        state.noteTab === 'official'
          ? t('notes.itemsAppearHereAfterAnAdministratorReviews')
          : t('notes.switchToAiOrganizeOrClickNew'),
        null, 'notes'
      )));
      if (!state.selectedNote) {
        detailArea.replaceChildren(empty(t('notes.giveKnowledgeAClearStructure'), t('notes.selectANoteOnTheLeftTo'),
          button(t('notes.organizeMaterialsWithAi'), () => switchSection('ai'), 'soft small', 'sparkle'), 'notes'));
      }
      return;
    }
    const list = el('div', { class: 'note-list' });
    for (const note of items) {
      const active = Number(state.selectedNote) === Number(note.id);
      list.append(el('button', {
        type: 'button',
        class: `note-card pg-note-card${active ? ' active' : ''}`,
        'data-note-id': note.id,
        'aria-current': active ? 'true' : 'false',
        onClick: () => openNote(note.id),
      },
        el('div', { class: 'row' },
          badge(note.origin === 'official' ? t('notes.officialList') : t('notes.myNotes'), note.origin === 'official' ? 'blue' : 'teal'),
          note.subject ? badge(note.subject) : null),
        el('h3', {}, note.title),
        el('p', {}, dateText(note.updated_at || note.created_at))));
    }
    listArea.append(list);

    if (autoOpen) {
      const targetId = (state.selectedNote && items.some(n => Number(n.id) === Number(state.selectedNote)))
        ? state.selectedNote
        : items[0].id;
      openNote(targetId);
    }
  }

  renderList(true);
}

/* ---------------------------------------------------------------------------
 * 站内题目引用（统一引用 bank:<qid> / app:<id> / official:<public_id>）
 * 笔记正文与来源里出现的引用渲染为可点击链接，可回到练习页继续作答；
 * 已知题库/内容版本时一并标注（与 Anki 导出的题目引用/版本字段同一口径）。
 * ------------------------------------------------------------------------- */

const QUESTION_REF_PATTERN = /(?:^|[^A-Za-z0-9_.:-])((?:bank:[A-Za-z0-9_.:-]+|app:\d+|official:plz-[A-Za-z0-9]+))/g;
const QUESTION_REF_LABEL_KEYS = {
  bank: 'notes.questionRef.bank',
  app: 'notes.questionRef.app',
  official: 'notes.questionRef.official',
};

function normalizeQuestionRef(value) {
  return String(value || '').trim().replace(/[.,;:!?)\]】]+$/, '');
}

function questionRefSource(ref) {
  const source = String(ref || '').split(':', 1)[0];
  return QUESTION_REF_LABEL_KEYS[source] ? source : '';
}

function questionRefLabel(ref) {
  const source = questionRefSource(ref);
  return source ? t(QUESTION_REF_LABEL_KEYS[source]) : t('notes.questionRef.generic');
}

/** 收集笔记里的站内题目引用：来源条目（ref/qid/source_ref）与正文中的统一引用文本。 */
function questionRefsOf(note) {
  const found = new Map();
  const add = (value, meta = null) => {
    const ref = normalizeQuestionRef(value);
    if (!ref || !questionRefSource(ref)) return;
    const existing = found.get(ref) || { ref, version: '' };
    const version = meta
      ? (meta.version ?? meta.published_version ?? meta.content_revision ?? meta.bank_version ?? '')
      : '';
    if (!existing.version && version !== '' && version !== null && version !== undefined) {
      existing.version = String(version);
    }
    found.set(ref, existing);
  };
  for (const source of Array.isArray(note?.sources) ? note.sources : []) {
    if (!source || typeof source !== 'object') continue;
    add(source.ref || source.qid || source.source_ref, source);
  }
  for (const match of String(note?.content || '').matchAll(QUESTION_REF_PATTERN)) add(match[1]);
  return [...found.values()];
}

function noteQuestionRefsNode(note) {
  const refs = questionRefsOf(note);
  if (!refs.length) return null;
  return el('section', { class: 'sources', 'data-note-question-refs': 'true' },
    el('strong', { class: 'small-text' }, t('notes.questionRefsTitle')),
    el('p', { class: 'help' }, t('notes.questionRefsHint')),
    el('div', { class: 'note-question-refs' },
      ...refs.map(entry => el('div', { class: 'row between source-block', 'data-question-ref': entry.ref },
        button(`${questionRefLabel(entry.ref)} · ${entry.ref}`,
          () => go('practice', entry.ref), 'ghost small', 'book'),
        entry.version
          ? el('span', { class: 'help', 'data-no-i18n': 'true' },
            t('notes.questionRefVersion', { v1: entry.version }))
          : null))));
}

/** 笔记来源资料版本（资料被替换后旧引用仍指向生成时版本）。 */
function noteSourceVersionsNode(note) {
  const versions = note?.source_versions && typeof note.source_versions === 'object'
    ? Object.entries(note.source_versions) : [];
  if (!versions.length) return null;
  const text = versions
    .map(([documentId, version]) => `D${documentId} v${version}`)
    .join(t('common.listSeparator'));
  return el('p', { class: 'help', 'data-no-i18n': 'true' }, t('notes.sourceVersionsLine', { v1: text }));
}

/* ===========================================================================
 * M3：资料原文定位与摘录
 * ---------------------------------------------------------------------------
 * 数据来源（M3 后端接口，由后端并行实现；不可用时整体中文降级，不影响原有的
 * 资料上传 / OCR / 笔记生成 / 编辑 / 发布 / 导出流程）：
 *   POST   /api/documents/{document_id}/locate  {page?,chunk_id?,quote?}
 *   POST   /api/doc-annotations                 {document_id,quote,note?,page?,chunk_id?,note_id?,color?}
 *   GET    /api/doc-annotations?document_id=&note_id=&page=&limit=
 *   POST   /api/doc-annotations/{id}/refresh     （migrated/valid 语义）
 *   DELETE /api/doc-annotations/{id}
 * 来源条目字段兼容 /notes/{id} 生成的 sources（text）以及导出/引用条目
 * （quote / snippet / excerpt），缺失 document_id 时隐藏定位入口，不伪造数据。
 * =========================================================================== */

// 语言包缺键兜底：本轮新键由 Lead 统一补进 locales/*.json；缺键时先显示内置中文，
// 避免界面回退成 `study.m3.*` 键名。补齐语言包后英文自动生效。
const M3_TEXT = {
  'study.m3.locateOriginal': '定位原文',
  'study.m3.locating': '正在定位原文…',
  'study.m3.locateFailedVersion': '当前资料版本中找不到这段摘录，请重新选择资料来源',
  'study.m3.docMissing': '资料不存在或无权访问',
  'study.m3.locateUnavailable': '原文定位服务暂不可用，请稍后再试',
  'study.m3.locateInvalidWithReason': '无法定位：{reason}。请重新选择资料来源。',
  'study.m3.matchExact': '精确匹配',
  'study.m3.matchNormalized': '规范化匹配',
  'study.m3.matchFuzzy': '近似匹配',
  'study.m3.matchedQuote': '命中摘录',
  'study.m3.pageLabel': '第 {page} 页',
  'study.m3.docVersion': '版本 v{version}',
  'study.m3.saveExcerpt': '保存为摘录',
  'study.m3.saveExcerptHint': '保存后可在此笔记的「我的摘录」里回看，并在原文失效时重新定位。',
  'study.m3.excerptNotePlaceholder': '备注（可选，便于以后回忆为什么要摘这段）',
  'study.m3.excerptSave': '保存',
  'study.m3.excerptCancel': '取消',
  'study.m3.excerptSaved': '摘录已保存',
  'study.m3.excerptExists': '这条摘录已保存，无需重复保存',
  'study.m3.excerpts': '摘录',
  'study.m3.myExcerpts': '我的摘录',
  'study.m3.docExcerpts': '资料摘录',
  'study.m3.viewExcerpts': '查看摘录',
  'study.m3.excerptValid': '有效',
  'study.m3.invalidExcerpt': '摘录已失效',
  'study.m3.invalidReason': '失效原因：{reason}',
  'study.m3.invalidReasonUnknown': '资料已更新，无法在原文中定位这段摘录',
  'study.m3.excerptQuoteMissing': '（摘录内容缺失）',
  'study.m3.excerptEmptyQuote': '这条来源没有可保存的摘录文本，请先定位原文',
  'study.m3.refreshLocate': '重新定位',
  'study.m3.refreshMigrated': '摘录已迁移到资料当前版本',
  'study.m3.refreshSuccess': '摘录已重新定位到原文',
  'study.m3.refreshStillInvalid': '仍无法定位：{reason}',
  'study.m3.deleteExcerpt': '删除',
  'study.m3.deleteExcerptConfirm': '删除这条摘录？删除后无法恢复。',
  'study.m3.excerptDeleted': '摘录已删除',
  'study.m3.excerptUnavailable': '摘录服务暂不可用，不影响笔记阅读与编辑',
  'study.m3.excerptsEmpty': '还没有摘录：在下方来源条目点「保存为摘录」即可建立摘录。',
  'study.m3.docExcerptsEmpty': '这份资料还没有摘录。',
  'study.m3.excerptLoading': '正在加载摘录…',
  'study.m3.createdAt': '保存于 {time}',
  'study.m3.sourcesTitle': '参考来源（{count} 条）',
};

function tm(key, vars = null) {
  const resolved = t(key, vars);
  if (resolved !== key) return resolved;
  const fallback = Object.prototype.hasOwnProperty.call(M3_TEXT, key) ? M3_TEXT[key] : key;
  if (!vars) return fallback;
  return fallback.replace(/\{(\w+)\}/g, (match, name) => (
    vars[name] === undefined || vars[name] === null ? match : String(vars[name])
  ));
}

const LOCATE_MATCH_KEYS = {
  exact: 'study.m3.matchExact',
  normalized: 'study.m3.matchNormalized',
  fuzzy: 'study.m3.matchFuzzy',
  approximate: 'study.m3.matchFuzzy',
};

// 与后端 app/doc_location.py 的校验上限对齐：摘录文本 ≤500 字、备注 ≤1000 字。
const MAX_EXCERPT_QUOTE = 500;
const MAX_EXCERPT_NOTE = 1000;

function m3String(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}

function m3Number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

/**
 * 规整来源条目的定位锚点。字段名兼容多种来源形状：
 * - 笔记生成 sources：{document_id,title,page,chunk_id,heading_path,text}
 * - 导出/引用条目：{document_id,page,chunk_id,quote} 或 {doc_id,page_no,snippet}
 * 缺少 document_id 或（页码/chunk/摘录）任一要素时返回 null（入口隐藏）。
 */
function locateAnchorOf(source) {
  if (!source || typeof source !== 'object') return null;
  const documentId = m3Number(source.document_id ?? source.doc_id ?? source.documentId);
  if (!documentId) return null;
  const page = m3Number(source.page ?? source.page_no ?? source.pageNo);
  const chunkId = m3String(source.chunk_id ?? source.chunkId ?? source.chunk);
  const quote = m3String(source.quote ?? source.text ?? source.snippet ?? source.excerpt);
  if (!page && !chunkId && !quote) return null;
  return { documentId, page, chunkId, quote };
}

/** 笔记 sources 容错：数组或 JSON 字符串；其它形状返回空数组。 */
function noteSourceList(note) {
  const raw = note?.sources;
  if (Array.isArray(raw)) return raw.filter(item => item && typeof item === 'object');
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter(item => item && typeof item === 'object') : [];
    } catch {
      return [];
    }
  }
  return [];
}

function locatePayload(anchor) {
  const payload = {};
  if (anchor.page) payload.page = anchor.page;
  if (anchor.chunkId) payload.chunk_id = anchor.chunkId;
  // 来源里的 text 可能是整段 chunk（约 2000 字）；定位只需一段可检索的摘录，
  // 截到 300 字既保持语义，也避免后端对 quote 长度设限导致 422。
  if (anchor.quote) payload.quote = anchor.quote.length > 300 ? anchor.quote.slice(0, 300) : anchor.quote;
  return payload;
}

function locateMatchLabel(match) {
  const key = LOCATE_MATCH_KEYS[String(match || '').toLowerCase()];
  return key ? tm(key) : '';
}

// api() 只抛出中文 detail，不暴露 HTTP 状态码；这里按后端中文原因做兜底归类，
// 保证 404 / 422 两种关键失败都有明确的中文提示。
function locateErrorText(error) {
  const raw = m3String(error?.message);
  // 接口整体缺失（后端未就绪）或网络失败时 api() 只给通用文案：按「服务暂不可用」降级，
  // 不能误报成「资料不存在」。
  if (!raw || /^请求失败（\d+）$/.test(raw) || /网络连接失败|Failed to fetch/i.test(raw)) {
    return tm('study.m3.locateUnavailable');
  }
  if (/无权|不存在|not\s*found/i.test(raw)) return tm('study.m3.docMissing');
  if (/版本|摘录|定位|找不到|未找到|无法匹配|不匹配|校验/i.test(raw)) {
    return raw ? tm('study.m3.locateInvalidWithReason', { reason: raw }) : tm('study.m3.locateFailedVersion');
  }
  return raw || tm('study.m3.locateUnavailable');
}

function annotationInvalidReason(annotation) {
  return m3String(annotation?.invalid_reason ?? annotation?.reason) || tm('study.m3.invalidReasonUnknown');
}

/** 前端简单判重：同一资料/笔记、同页码或 chunk、摘录正文一致时视为已存在。 */
function sameExcerpt(annotation, anchor) {
  if (!annotation || typeof annotation !== 'object') return false;
  if (Number(annotation.document_id) !== Number(anchor.documentId)) return false;
  if (anchor.chunkId && annotation.chunk_id && String(annotation.chunk_id) !== anchor.chunkId) return false;
  if (anchor.page && annotation.page && Number(annotation.page) !== Number(anchor.page)) return false;
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const left = normalize(annotation.quote || annotation.snippet);
  const right = normalize(anchor.quote);
  if (!left || !right) return false;
  if (left === right) return true;
  const head = right.slice(0, 120);
  return head.length >= 20 && (left.includes(head) || right.includes(left.slice(0, 120)));
}

/** 保存摘录（POST /api/doc-annotations）；已存在时返回 {duplicate:true} 并提示。 */
async function saveExcerpt(anchor) {
  const payload = { document_id: Number(anchor.documentId) };
  // 后端 quote 上限 500 字：来源里的整段 chunk 文本需要先截成一段可保存的摘录。
  const quote = m3String(anchor.quote);
  if (!quote) throw new Error(tm('study.m3.excerptEmptyQuote'));
  payload.quote = quote.length > MAX_EXCERPT_QUOTE ? quote.slice(0, MAX_EXCERPT_QUOTE) : quote;
  if (anchor.page) payload.page = Number(anchor.page);
  if (anchor.chunkId) payload.chunk_id = anchor.chunkId;
  if (anchor.note) payload.note = anchor.note.slice(0, MAX_EXCERPT_NOTE);
  if (anchor.noteId) payload.note_id = Number(anchor.noteId);
  // 后端不保证幂等：先按同一资料 + 笔记查一次，避免重复写入。
  try {
    const existing = await api(`/doc-annotations?${query({
      document_id: anchor.documentId, note_id: anchor.noteId || '', limit: 50,
    })}`);
    const items = Array.isArray(existing?.items) ? existing.items : [];
    if (items.some(item => sameExcerpt(item, anchor))) {
      toast(tm('study.m3.excerptExists'));
      return { duplicate: true };
    }
  } catch {
    /* 判重查询失败不阻塞保存：后端可能再次判重或允许重复。 */
  }
  return api('/doc-annotations', { method: 'POST', data: payload });
}

/** 定位命中后展开的摘要：页码、出处标题、heading_path、匹配方式与原文片段。 */
function locateResultNode(result, anchor, noteId, onSaved) {
  const page = m3Number(result?.page) || anchor.page;
  const title = m3String(result?.title);
  const version = m3Number(result?.doc_version);
  const headingPath = m3String(result?.heading_path ?? result?.heading_path_text);
  const matchLabel = locateMatchLabel(result?.match);
  const quote = m3String(result?.quote) || anchor.quote;
  const snippetText = m3String(result?.snippet);
  const meta = [];
  if (page) meta.push(tm('study.m3.pageLabel', { page }));
  if (title) meta.push(title);
  if (version) meta.push(tm('study.m3.docVersion', { version }));

  const box = el('div', { class: 'm3-locate-panel' },
    el('div', { class: 'm3-locate-head' },
      el('strong', { class: 'm3-locate-meta', 'data-no-i18n': '' }, meta.join(' · ') || tm('study.m3.locateOriginal')),
      matchLabel ? badge(matchLabel, 'blue') : null));
  if (headingPath) {
    box.append(el('p', { class: 'm3-locate-path', 'data-no-i18n': '' }, headingPath));
  }
  if (snippetText) {
    box.append(el('blockquote', { class: 'm3-snippet-quote', 'data-no-i18n': '' }, snippetText));
  }
  if (quote && quote !== snippetText) {
    box.append(
      el('p', { class: 'm3-snippet-label' }, tm('study.m3.matchedQuote')),
      el('blockquote', { class: 'm3-snippet-quote is-original', 'data-no-i18n': '' }, quote));
  }
  // 后端定位提示（例如「已按忽略空白/大小写匹配」「原片段失效已重新定位」）。
  const noticeText = m3String(result?.notice);
  if (noticeText) box.append(el('p', { class: 'help m3-locate-notice', 'data-no-i18n': '' }, noticeText));
  const saveMount = el('div', { class: 'm3-source-save' });
  // 仅当定位结果确实带生效摘录时才提供「保存为摘录」；纯页码锚点 quote 为空不伪造摘录。
  if (quote) {
    box.append(el('div', { class: 'm3-source-actions' },
      button(tm('study.m3.saveExcerpt'), () => {
        openExcerptComposer({
          documentId: m3Number(result?.document_id) || anchor.documentId,
          page: page || 0,
          chunkId: m3String(result?.chunk_id) || anchor.chunkId,
          quote: quote || anchor.quote,
          noteId,
        }, saveMount, onSaved);
      }, 'soft small', 'notes')), saveMount);
  }
  return box;
}

/** 定位：POST /api/documents/{document_id}/locate，结果展开在条目下方。 */
async function locateSource(anchor, mount, noteId, onSaved) {
  mount.replaceChildren(loading(tm('study.m3.locating')));
  try {
    const result = await api(`/documents/${encodeURIComponent(anchor.documentId)}/locate`, {
      method: 'POST',
      data: locatePayload(anchor),
    });
    if (!result || result.valid === false) {
      throw new Error(m3String(result?.invalid_reason) || tm('study.m3.locateFailedVersion'));
    }
    if (!mount.isConnected) return;
    mount.replaceChildren(locateResultNode(result, anchor, noteId, onSaved));
  } catch (error) {
    if (!mount.isConnected) return;
    mount.replaceChildren(notice(locateErrorText(error), 'warning'));
  }
}

/** 行内摘录备注表单（定位成功或来源自带 quote 时显示）。 */
function openExcerptComposer(anchor, mount, onSaved) {
  if (mount.querySelector('.m3-excerpt-composer')) return;
  const noteInput = input(tm('study.m3.excerptNotePlaceholder'), { maxlength: String(MAX_EXCERPT_NOTE) });
  const form = el('div', { class: 'm3-excerpt-composer' },
    el('p', { class: 'help' }, tm('study.m3.saveExcerptHint')),
    el('div', { class: 'm3-excerpt-composer-main' }, noteInput,
      el('div', { class: 'm3-excerpt-composer-actions' },
        button(tm('study.m3.excerptCancel'), () => mount.replaceChildren(), 'ghost small'),
        button(tm('study.m3.excerptSave'), event => busy(event.currentTarget, async () => {
          const result = await saveExcerpt({ ...anchor, note: noteInput.value.trim() });
          if (result?.duplicate) {
            mount.replaceChildren();
            return;
          }
          mount.replaceChildren();
          toast(tm('study.m3.excerptSaved'));
          onSaved?.();
        }), '', 'check'))));
  mount.replaceChildren(form);
  noteInput.focus();
}

/**
 * 摘录面板：GET /api/doc-annotations；失效条目展示 invalid_reason 与「重新定位」，
 * 删除需二次确认。接口不可用时显示中文降级说明（不伪造摘录数据）。
 * alwaysVisible=false 时无摘录/加载前整块隐藏（对应「无数据隐藏入口」）。
 */
function createExcerptPanel({ params = {}, title = '', emptyHint = '', alwaysVisible = false } = {}) {
  // 资料弹层始终可见：先用加载态占位，避免打开弹层后出现空白区域。
  const host = el('section', { class: `m3-excerpt-panel${alwaysVisible ? '' : ' hide'}` });
  if (alwaysVisible) host.append(loading(tm('study.m3.excerptLoading')));
  let seq = 0;

  function panelHead() {
    return el('div', { class: 'm3-excerpt-panel-head' },
      el('h3', {}, title || tm('study.m3.excerpts')),
      button(t('common.reload'), () => load(), 'ghost small', 'rotate'));
  }

  function itemActions(annotation) {
    const invalid = annotation?.valid === false;
    const actions = el('div', { class: 'm3-excerpt-actions' });
    if (invalid && annotation?.annotation_id) {
      actions.append(button(tm('study.m3.refreshLocate'), event => busy(event.currentTarget, async () => {
        const result = await api(`/doc-annotations/${encodeURIComponent(annotation.annotation_id)}/refresh`, { method: 'POST' });
        const valid = result?.valid === true || (result?.annotation && result.annotation.valid !== false);
        // 后端只要重新定位成功就返回 migrated:true；是否有「版本迁移」看 doc_version 是否变化。
        const versionChanged = Boolean(result?.annotation && annotation?.doc_version
          && String(result.annotation.doc_version) !== String(annotation.doc_version));
        if (valid) {
          toast(versionChanged ? tm('study.m3.refreshMigrated') : tm('study.m3.refreshSuccess'));
        } else {
          toast(tm('study.m3.refreshStillInvalid', {
            reason: annotationInvalidReason(result?.annotation || annotation),
          }), true);
        }
        await load();
      }), 'soft small', 'rotate'));
    }
    if (annotation?.annotation_id) {
      actions.append(button(tm('study.m3.deleteExcerpt'), event => busy(event.currentTarget, async () => {
        if (!window.confirm(tm('study.m3.deleteExcerptConfirm'))) return;
        await api(`/doc-annotations/${encodeURIComponent(annotation.annotation_id)}`, { method: 'DELETE' });
        toast(tm('study.m3.excerptDeleted'));
        await load();
      }), 'ghost small', 'trash'));
    }
    return actions;
  }

  function itemNode(annotation) {
    const invalid = annotation?.valid === false;
    const quote = m3String(annotation?.quote) || m3String(annotation?.snippet);
    const meta = [];
    if (annotation?.page) meta.push(tm('study.m3.pageLabel', { page: annotation.page }));
    if (annotation?.heading_path) meta.push(String(annotation.heading_path));
    if (annotation?.created_at) meta.push(tm('study.m3.createdAt', { time: dateText(annotation.created_at) }));
    const node = el('article', {
      class: `m3-excerpt-item${invalid ? ' is-invalid' : ''}`,
      'data-annotation-id': String(annotation?.annotation_id ?? ''),
    },
    el('div', { class: 'm3-excerpt-item-head' },
      badge(invalid ? tm('study.m3.invalidExcerpt') : tm('study.m3.excerptValid'), invalid ? 'red' : 'teal'),
      meta.length ? el('span', { class: 'm3-excerpt-meta', 'data-no-i18n': '' }, meta.join(' · ')) : null),
    el('blockquote', { class: 'm3-excerpt-quote', 'data-no-i18n': '' }, quote || tm('study.m3.excerptQuoteMissing')),
    annotation?.note ? el('p', { class: 'm3-excerpt-note' }, annotation.note) : null);
    if (invalid) {
      node.append(el('p', { class: 'm3-invalid-reason' }, tm('study.m3.invalidReason', {
        reason: annotationInvalidReason(annotation),
      })));
    }
    const actions = itemActions(annotation);
    if (actions.childNodes.length) node.append(actions);
    return node;
  }

  function renderItems(items) {
    if (!items.length) {
      host.classList.toggle('hide', !alwaysVisible);
      if (!alwaysVisible) {
        host.replaceChildren();
        return;
      }
      host.replaceChildren(panelHead(), el('p', { class: 'm3-excerpt-empty' }, emptyHint || tm('study.m3.excerptsEmpty')));
      return;
    }
    host.classList.remove('hide');
    host.replaceChildren(
      panelHead(),
      el('div', { class: 'm3-excerpt-list' }, ...items.map(itemNode)),
    );
  }

  function renderError(error) {
    const raw = m3String(error?.message);
    host.classList.remove('hide');
    host.replaceChildren(
      panelHead(),
      notice(tm('study.m3.excerptUnavailable') + (raw ? `（${raw}）` : ''), 'warning'),
    );
  }

  async function load() {
    const mine = ++seq;
    try {
      const result = await api(`/doc-annotations?${query({ ...params, limit: 50 })}`);
      if (mine !== seq) return;
      renderItems(Array.isArray(result?.items) ? result.items : []);
    } catch (error) {
      if (mine !== seq) return;
      renderError(error);
    }
  }

  return { node: host, load };
}

/** 资料摘录弹层：按 document_id 读取摘录，复用同一套失效/重新定位/删除交互。 */
function openDocExcerptDialog(doc) {
  const dialog = el('dialog', { class: 'modal-dialog m3-excerpt-dialog' });
  const panel = createExcerptPanel({
    params: { document_id: doc.id },
    title: tm('study.m3.docExcerpts'),
    emptyHint: tm('study.m3.docExcerptsEmpty'),
    alwaysVisible: true,
  });
  dialog.append(
    el('div', { class: 'card-header m3-excerpt-dialog-head' },
      el('h3', {}, tm('study.m3.viewExcerpts')),
      button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
    el('p', { class: 'help', 'data-no-i18n': '' }, doc.title || ''),
    panel.node);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
  panel.load();
}

/**
 * 来源引用区：保留原有「参考来源」折叠结构，在每条带 document_id 的来源上追加
 * 「定位原文」与（有摘录文本时）「保存为摘录」入口。
 */
function renderNoteSources(note, onExcerptSaved) {
  const sources = noteSourceList(note);
  if (!sources.length) return null;
  const details = el('details', { class: 'sources m3-sources' },
    el('summary', {}, tm('study.m3.sourcesTitle', { count: sources.length })));
  for (const source of sources) {
    const anchor = locateAnchorOf(source);
    const locateMount = el('div', { class: 'm3-locate-mount' });
    const saveMount = el('div', { class: 'm3-source-save' });
    const actionRow = el('div', { class: 'm3-source-actions' });
    if (anchor) {
      actionRow.append(button(tm('study.m3.locateOriginal'), event => busy(event.currentTarget, () => locateSource(anchor, locateMount, note?.id, onExcerptSaved)), 'ghost small', 'search'));
      if (anchor.quote) {
        actionRow.append(button(tm('study.m3.saveExcerpt'), () => openExcerptComposer({
          ...anchor, noteId: note?.id,
        }, saveMount, onExcerptSaved), 'ghost small', 'notes'));
      }
    }
    details.append(el('div', { class: 'source-block m3-source-block' },
      el('strong', {}, [
        source.title || t('result.source', { v1: source.document_id }),
        source.page ? t('result.page', { v1: source.page }) : null,
        source.heading_path || null,
        source.chunk_id ? t('result.chunk', { v1: source.chunk_id }) : null,
      ].filter(Boolean).join(' · ')),
      source.text ? el('div', { class: 'm3-source-text', 'data-no-i18n': '' }, String(source.text)) : null,
      actionRow.childNodes.length ? actionRow : null,
      locateMount,
      saveMount));
  }
  return details;
}

/* ---------------------------------------------------------------------------
 * M4 学习附件面板（笔记详情按 by-target 读取学习附件；只读打开 / 下载海报）
 * ---------------------------------------------------------------------------
 * - GET /api/artifacts/by-target?target_kind=note&target_key=<note id>
 *   → {items:[{artifact,revision,data,preview,link}],total,skipped}；
 * - 有附件才显示「学习附件」区；无数据隐藏；接口不可用中文降级，不伪造附件；
 * - 打开走本地受控组件的只读模式（principleDemo / functionGraph / sketchBoard），
 *   海报用 <a download> 下载 data URL，标题/版本/时间一律 textContent 写入。
 */

/** kind → 文案键 + 中文兜底（kind 是后端枚举，不直接拼进界面文案）。 */
const NOTE_ARTIFACT_KIND_META = Object.freeze({
  function_graph: { key: 'study.m4.kind.functionGraph', fallback: '函数图' },
  whiteboard: { key: 'study.m4.kind.whiteboard', fallback: '解题白板' },
  flow_animation: { key: 'study.m4.kind.flowAnimation', fallback: '流程动画' },
  isometric: { key: 'study.m4.kind.isometric', fallback: '等距结构' },
  science_demo: { key: 'study.m4.kind.scienceDemo', fallback: '科学演示' },
  demo_3d: { key: 'study.m4.kind.demo3d', fallback: '3D 演示' },
  reading_excerpt: { key: 'study.m4.kind.readingExcerpt', fallback: '阅读摘录' },
  // M5（§5.4 机械与工厂）：机械 3D 与工厂仿真学习附件，只读打开复用 mechDemo 组件。
  mech_3d: { key: 'study.m5.kind.mech3d', fallback: '机械 3D' },
  factory_sim: { key: 'study.m5.kind.factorySim', fallback: '工厂仿真' },
});

const NOTE_ARTIFACT_DEMO_KINDS = new Set(['flow_animation', 'isometric', 'demo_3d', 'science_demo']);

/** 取 `study.m4.*` 文案：locale 缺键时回退中文常量并做 `{name}` 插值。 */
function m4t(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function noteArtifactKindLabel(kind) {
  const meta = NOTE_ARTIFACT_KIND_META[String(kind || '')];
  return meta ? m4t(meta.key, meta.fallback) : m4t('study.m4.kind.other', '学习附件');
}

/** science_demo 等前端没有独立渲染器的 kind：按数据结构选择本地只读视图。 */
function noteArtifactDemoKind(kind, data) {
  if (kind === 'flow_animation' || kind === 'isometric' || kind === 'demo_3d') return kind;
  if (data && typeof data === 'object') {
    if (Array.isArray(data.parts)) return 'isometric';
    if (Array.isArray(data.nodes)) return 'flow_animation';
  }
  return kind;
}

/** 只接受内联图片 data URL（附件 preview.png 约定），拒绝外链/脚本伪协议。 */
function noteArtifactPosterDataUrl(preview) {
  const raw = String(preview && typeof preview === 'object' ? preview.png || '' : '').trim();
  if (!raw || raw.length > 200000) return '';
  return /^data:image\/(?:png|jpe?g|webp);base64,/i.test(raw) ? raw : '';
}

function noteArtifactPosterName(title, revision) {
  const base = String(title || '').replace(/[\\/:*?"<>|\s]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 60);
  return `${base || 'learning-artifact'}-v${Number(revision) || 1}.png`;
}

/** 在弹层里挂载只读组件；返回 controller（destroy 由调用方负责）。 */
async function mountNoteArtifactView(mount, { kind, title, data }) {
  const artTitle = String(title || '').trim() || m4t('study.m4.kind.other', '学习附件');
  if (kind === 'function_graph') {
    const module = await import('../components/functionGraph.js');
    if (typeof module.normalizeFunctionGraphDefinition === 'function'
      && !module.normalizeFunctionGraphDefinition(data)) {
      throw new Error(m4t('study.m4.notes.invalidData', '附件数据不合法'));
    }
    const graph = module.createFunctionGraph({ definition: data, readOnly: true, ariaLabel: artTitle });
    mount.replaceChildren(graph.element);
    return graph;
  }
  if (kind === 'whiteboard') {
    const module = await import('../components/sketchBoard.js');
    if (typeof module.normalizeSketchScene === 'function' && !module.normalizeSketchScene(data)) {
      throw new Error(m4t('study.m4.notes.invalidData', '附件数据不合法'));
    }
    const board = module.createSketchBoard({ scene: data, readOnly: true, ariaLabel: artTitle });
    mount.replaceChildren(board.element);
    return board;
  }
  // M5（§5.4 机械与工厂）：机械 3D / 工厂仿真学习附件只读打开，复用 mechDemo 组件；
  // 数据不合法时组件内部给中文降级，不伪造演示内容。
  if (kind === 'mech_3d' || kind === 'factory_sim') {
    const module = await import('../components/mechDemo.js');
    const host = el('div', { class: 'cls-note-artifact-view m5-note-view' });
    const demo = {
      kind,
      title: artTitle,
      data: data === undefined || data === null ? {} : data,
      seed: data && typeof data === 'object' ? data.seed : undefined,
    };
    const view = kind === 'mech_3d'
      ? module.createMechView(host, demo, { readOnly: true })
      : module.createFactoryView(host, demo, { readOnly: true });
    if (!view || !view.element) {
      throw new Error(m4t('study.m4.notes.invalidData', '附件数据不合法'));
    }
    mount.replaceChildren(host);
    try { await view.ready; } catch { /* ready 不应 reject；兼容异常实现 */ }
    return view;
  }
  if (NOTE_ARTIFACT_DEMO_KINDS.has(kind)) {
    const module = await import('../components/principleDemo.js');
    const host = el('div', { class: 'cls-note-artifact-view' });
    const controller = module.renderPrincipleDemo(host, {
      kind: noteArtifactDemoKind(kind, data),
      title: artTitle,
      data: data === undefined || data === null ? {} : data,
    }, { keyboard: true });
    mount.replaceChildren(host);
    try { await controller.ready; } catch { /* ready 不应 reject；兼容异常实现 */ }
    if (typeof controller.getMode === 'function' && controller.getMode() === 'poster') {
      try { controller.destroy(); } catch { /* 忽略 */ }
      throw new Error(m4t('study.m4.notes.renderFailed', '演示渲染失败'));
    }
    return controller;
  }
  throw new Error(m4t('study.m4.notes.unsupported', '该类型附件暂不支持在页面内打开'));
}

/** 只读查看单个学习附件：按 kind 动态 import 本地组件，关闭时销毁。 */
function openNoteArtifactDialog(item) {
  const artifact = item?.artifact || {};
  const kind = String(artifact.kind || '');
  const title = String(artifact.title || '').trim() || m4t('study.m4.kind.other', '学习附件');
  const revision = Number(item?.revision) || Number(artifact.current_revision) || 1;
  const dialog = el('dialog', { class: 'modal-dialog cls-note-artifact-dialog', 'aria-label': title });
  const mount = el('div', { class: 'cls-note-artifact-dialog-body' },
    loading(m4t('study.m4.notes.loading', '正在加载学习附件…')));
  const status = el('p', { class: 'help cls-note-artifact-dialog-status', role: 'status', 'aria-live': 'polite' });
  let controller = null;
  let closed = false;
  const close = () => { try { dialog.close(); } catch { /* 已关闭时忽略 */ } };
  dialog.append(
    el('div', { class: 'card-header cls-note-artifact-dialog-head' },
      el('div', { class: 'cls-note-artifact-dialog-title' },
        el('h3', {}, title),
        el('span', { class: 'help' }, [
          noteArtifactKindLabel(kind),
          m4t('study.m4.common.version', '版本 v{version}', { version: revision }),
        ].filter(Boolean).join(' · '))),
      button(t('common.close'), close, 'ghost small', 'close', { 'aria-label': t('common.close') })),
    mount,
    status);
  document.body.append(dialog);
  dialog.addEventListener('close', () => {
    closed = true;
    if (controller && typeof controller.destroy === 'function') {
      try { controller.destroy(); } catch { /* 释放失败不影响页面 */ }
    }
    controller = null;
    dialog.remove();
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  dialog.showModal();
  mountNoteArtifactView(mount, { kind, title, data: item?.data })
    .then((next) => {
      if (closed) {
        try { next?.destroy?.(); } catch { /* 关闭后才挂载成功：立即释放 */ }
        return;
      }
      controller = next;
      status.textContent = '';
    })
    .catch((error) => {
      if (closed) return;
      mount.replaceChildren(notice(m4t('study.m4.notes.openFailed', '附件打开失败：{reason}', {
        reason: error?.message || '未知原因',
      }), 'warning'));
    });
}

/**
 * 笔记详情「学习附件」面板：GET /api/artifacts/by-target 读取挂到该笔记的附件。
 * 无数据整块隐藏；接口不可用时显示中文降级（不伪造附件数据）。
 */
function createNoteAttachmentPanel(note) {
  const noteId = Number(note?.id) || 0;
  const host = el('section', {
    class: 'cls-note-artifacts hide',
    'data-note-attachments': noteId ? String(noteId) : '',
  });
  let seq = 0;
  if (!noteId) return { node: host, load: () => {} };

  function headNode() {
    return el('div', { class: 'cls-note-artifacts-head' },
      el('h3', {}, m4t('study.m4.notes.title', '学习附件')),
      button(t('common.reload'), () => load(), 'ghost small', 'rotate'));
  }

  function cardNode(item) {
    const artifact = item.artifact || {};
    const kind = String(artifact.kind || '');
    const title = String(artifact.title || '').trim() || m4t('study.m4.kind.other', '学习附件');
    const revision = Number(item.revision) || Number(artifact.current_revision) || 1;
    const poster = noteArtifactPosterDataUrl(item.preview);
    const metaParts = [
      noteArtifactKindLabel(kind),
      m4t('study.m4.common.version', '版本 v{version}', { version: revision }),
      artifact.updated_at
        ? m4t('study.m4.common.updatedAt', '更新于 {time}', { time: dateText(artifact.updated_at) })
        : null,
    ].filter(Boolean);
    const actions = el('div', { class: 'cls-note-artifact-actions' },
      button(m4t('study.m4.notes.open', '打开'), () => openNoteArtifactDialog(item), 'secondary small', 'eye'));
    if (poster) {
      actions.append(el('a', {
        class: 'button ghost small',
        href: poster,
        download: noteArtifactPosterName(title, revision),
        title: m4t('study.m4.notes.downloadPoster', '下载海报'),
        'data-download-poster': '1',
      }, icon('download'), m4t('study.m4.notes.downloadPoster', '下载海报')));
    }
    return el('article', {
      class: 'cls-note-artifact-card',
      'data-artifact-id': String(artifact.artifact_id || ''),
    },
    poster
      ? el('img', {
        class: 'cls-note-artifact-thumb',
        src: poster,
        alt: m4t('study.m4.notes.posterAlt', '附件海报：{title}', { title }),
        loading: 'lazy',
      })
      : null,
    el('div', { class: 'cls-note-artifact-main' },
      el('strong', { class: 'cls-note-artifact-title', 'data-no-i18n': 'true' }, title),
      el('span', { class: 'help cls-note-artifact-meta' }, metaParts.join(' · '))),
    actions);
  }

  async function load() {
    const mine = ++seq;
    try {
      const result = await api(`/artifacts/by-target?${query({
        target_kind: 'note',
        target_key: String(noteId),
      })}`);
      if (mine !== seq) return;
      const items = (Array.isArray(result?.items) ? result.items : [])
        .filter(item => item && typeof item === 'object' && item.artifact);
      if (!items.length) {
        host.classList.add('hide');
        host.replaceChildren();
        return;
      }
      const skipped = Number(result?.skipped) || 0;
      host.classList.remove('hide');
      const children = [headNode()];
      if (skipped > 0) {
        children.push(notice(m4t('study.m4.notes.skipped', '{count} 个附件已失效（可能已被删除）', {
          count: skipped,
        }), 'warning'));
      }
      children.push(el('div', { class: 'cls-note-artifact-list' }, ...items.map(cardNode)));
      host.replaceChildren(...children);
    } catch (error) {
      if (mine !== seq) return;
      const raw = String(error?.message || '').trim();
      host.classList.remove('hide');
      host.replaceChildren(
        el('div', { class: 'cls-note-artifacts-head' },
          el('h3', {}, m4t('study.m4.notes.title', '学习附件'))),
        notice(m4t('study.m4.notes.unavailable', '学习附件暂不可用') + (raw ? `（${raw}）` : ''), 'warning'));
    }
  }

  return { node: host, load };
}

function renderNoteDetail(note, container, reload, onPatched = null) {
  const own = note.origin !== 'official' || state.user.role === 'admin';
  const versionsMount = el('div');
  const mindmapMount = el('div');

  const actions = el('div', { class: 'row pg-note-actions' });
  if (own) actions.append(button(t('settings.edit'), () => editNote(note, container, reload, onPatched), 'secondary small', 'edit'));
  actions.append(
    button(t('notes.mindMapView'), () => {
      if (mindmapMount.childNodes.length) { mindmapMount.replaceChildren(); return; }
      const tree = markdownToMindmapTree(note.title, note.content || '');
      mindmapMount.replaceChildren(renderMindmapWidget(tree, { title: t('notes.mindMap', { v1: note.title }) }));
    }, 'soft small', 'eye'),
    button(t('notes.exportMd'), () => downloadText(`${note.title}.md`, note.content || ''), 'ghost small', 'download'),
    button('Anki CSV', async e => busy(e.currentTarget, async () => {
      const csv = await api(`/notes/${note.id}/export?format=anki`, { rawText: true });
      downloadText(`${note.title}_anki.csv`, csv, 'text/csv;charset=utf-8');
      toast(t('notes.ankiCardCsvExportedYouCanImport'));
    }), 'ghost small', 'download'),
    button(t('notes.ankiDeck'), async e => busy(e.currentTarget, async () => {
      // .apkg 是二进制：不能走 api(rawText)，直接 fetch 后按 Blob 下载。
      const response = await fetch(`/api/notes/${note.id}/export?format=apkg`, { credentials: 'same-origin' });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || t('notes.exportFailed', { v1: response.status }));
      }
      downloadBlob(await response.blob(), `${note.title}_anki.apkg`);
      toast(t('notes.ankiDeckExportedDoubleClickToImport'));
    }), 'ghost small', 'download'),
    button(t('notes.exportMmd'), async e => busy(e.currentTarget, async () => {
      const mm = await api(`/notes/${note.id}/export?format=mindmap`, { rawText: true });
      downloadText(`${note.title}_mindmap.mmd`, mm, 'text/plain;charset=utf-8');
      toast(t('notes.mermaidMindMapFileExported'));
    }), 'ghost small', 'download'),
    button(t('notes.printPdf'), () => window.print(), 'ghost small'),
    button(t('notes.versionHistory'), e => busy(e.currentTarget, async () => {
      if (versionsMount.childNodes.length) { versionsMount.replaceChildren(); return; }
      const res = await api(`/notes/${note.id}/versions`);
      const box = el('div', { class: 'sources', style: 'margin:12px 0' },
        el('strong', { class: 'small-text' }, t('notes.versionHistoryVersions', { v1: res.items?.length || 0 })));
      for (const v of res.items || []) {
        box.append(el('div', { class: 'row between source-block' },
          el('div', {}, el('strong', {}, t('notes.versionLabel', { v1: v.version_no, v2: v.change_note || t('common.save') })), el('div', { class: 'help' }, t('notes.changedAtChars', { v1: dateText(v.created_at), v2: v.chars }))),
          own ? button(t('notes.rollBackToThisVersion'), ev => busy(ev.currentTarget, async () => {
            if (!window.confirm(t('notes.rollBackToVersionVTheCurrent', { v1: v.version_no }))) return;
            await api(`/notes/${note.id}/rollback/${v.id}`, { method: 'POST' });
            toast(t('notes.rolledBackToV', { v1: v.version_no }));
            reload();
          }), 'soft small', 'history') : null));
      }
      versionsMount.replaceChildren(box);
    }), 'ghost small', 'history')
  );

  if (state.user.role === 'admin' && note.origin !== 'official') {
    actions.append(button(t('notes.publishAsOfficialList'), e => busy(e.currentTarget, async () => {
      if (!window.confirm(t('notes.afterPublishingAllAccountsCanReadThis'))) return;
      await api(`/notes/${note.id}/publish`, { method: 'POST' });
      toast(t('notes.publishedAsAnOfficialList'));
      await renderRoute();
    }), 'soft small', 'shield'));
  }
  if (own) {
    actions.append(button(t('common.delete'), e => busy(e.currentTarget, async () => {
      if (!window.confirm(t('notes.deleteThisNoteThisRemovesTheSaved'))) return;
      await api(`/notes/${note.id}`, { method: 'DELETE' });
      state.selectedNote = null;
      toast(t('notes.noteDeleted'));
      await renderRoute();
    }), 'danger small', 'trash'));
  }

  // M3 摘录面板：无摘录时隐藏；接口不可用时显示中文降级说明。
  const excerptPanel = createExcerptPanel({
    params: { note_id: note.id },
    title: tm('study.m3.myExcerpts'),
  });

  // M4 学习附件面板：无附件时隐藏；接口不可用时中文降级（与「我的摘录」并列）。
  const attachmentPanel = createNoteAttachmentPanel(note);

  fill(container,
    el('div', { class: 'row between', style: 'margin-bottom:12px' },
      el('div', { class: 'row' },
        badge(note.origin === 'official' ? t('home.noteOfficial') : t('home.notePrivate'), note.origin === 'official' ? 'blue' : 'teal'),
        note.subject ? badge(note.subject) : null),
      el('span', { class: 'help' }, dateText(note.updated_at || note.created_at))),
    el('h2', {}, note.title),
    actions,
    mindmapMount,
    versionsMount,
    note.origin === 'official'
      ? null
      : notice(t('notes.thisNoteIsStoredInYourPrivate')),
    noteQuestionRefsNode(note),
    noteSourceVersionsNode(note),
    markdown(note.content || ''),
    renderNoteSources(note, () => excerptPanel.load()),
    attachmentPanel.node,
    excerptPanel.node
  );
  attachmentPanel.load();
  excerptPanel.load();
}

function editNote(note, container, reload, onPatched = null) {
  const title = input(t('notes.noteTitle'), { value: note.title });
  const changeNote = input(t('notes.changeDescriptionEGAddACounter'), { value: t('notes.manualEdit') });
  const content = el('textarea', { class: 'note-editor', value: note.content || '' });
  container.replaceChildren(
    el('div', { class: 'card-header' }, el('h2', {}, t('notes.editKnowledgeNote')), badge('Markdown + KaTeX')),
    field(t('notes.title'), title),
    el('div', { style: 'margin-top:12px' }, field(t('notes.changeDescriptionRecordedInVersionHistory'), changeNote)),
    el('div', { style: 'margin-top:14px' }, field(t('notes.noteContent'), content)),
    el('div', { class: 'row end', style: 'margin-top:17px' },
      button(t('common.cancel'), reload, 'secondary'),
      button(t('notes.saveNote'), e => busy(e.currentTarget, async () => {
        if (!title.value.trim()) throw new Error(t('notes.pleaseFillInTheNoteTitle'));
        const nextTitle = title.value.trim();
        await api(`/notes/${note.id}`, {
          method: 'PATCH',
          data: { title: nextTitle, content: content.value, change_note: changeNote.value.trim() || t('notes.manualEdit') },
        });
        onPatched?.({ title: nextTitle });
        toast(t('notes.noteSavedAsANewVersion'));
        reload();
      }), '', 'check'))
  );
}
