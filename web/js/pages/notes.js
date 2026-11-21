import { api, ensureCatalog, ensureKnowledgePoints, ensurePromptGrid } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, fill, icon, button, badge, empty, heading, loading, notice, field, input, select,
  dateText, downloadText, downloadBlob, busy, toast, wireTabKeyboard,
} from '../core/dom.js';
import { current, renderRoute } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker } from '../components/filePicker.js';
import { renderSources } from '../components/aiResultCard.js';
import { createPromptGrid } from '../components/promptGrid.js';
import { renderMindmapWidget, markdownToMindmapTree } from '../components/mindmap.js';
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
// 登出/401 后清空分区记忆，避免下一个账号一进知识清单就落在 AI 整理表单。
onSessionReset(() => { noteSection = 'notes'; });

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


export async function renderNotes(container, version) {
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
        docList.append(el('div', { class: 'pg-doc-item' },
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

  // 打开生成的笔记 / 从首页或讲解卡片跳进来时，优先展示被选中的笔记，
  // 清掉上次 AI 任务与分区残留，避免一进知识清单又跳回 AI 表单。
  if (state.selectedNote) {
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

  container.replaceChildren(
    el('div', { class: 'pg-page-head' },
      heading('KNOWLEDGE WORKSPACE', t('notes.connectScatteredMaterialsIntoKnowledge'),
        t('notes.myNotesTheMaterialLibraryAndAi'),
        el('div', { class: 'pg-actions' },
          button(t('notes.newNote'), () => { switchSection('notes'); toggleManual(); }, 'secondary', 'edit'),
          button(t('notes.organizeMaterialsWithAi'), () => switchSection('ai'), '', 'sparkle')))),
    sectionTabs,
    notesPanel, docsPanel, aiPanel);
  switchSection(noteSection);

  renderDocsUI();
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
    markdown(note.content || ''),
    note.sources?.length ? renderSources(note.sources) : null
  );
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
