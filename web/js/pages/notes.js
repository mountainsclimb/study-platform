import { api, ensureCatalog, ensureKnowledgePoints, ensurePromptGrid } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, fill, icon, button, badge, empty, heading, loading, notice, field, input, select,
  dateText, downloadText, busy, toast, wireTabKeyboard,
} from '../core/dom.js';
import { current, renderRoute } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker } from '../components/filePicker.js';
import { renderSources } from '../components/aiResultCard.js';
import { createPromptGrid } from '../components/promptGrid.js';
import { renderMindmapWidget, markdownToMindmapTree } from '../components/mindmap.js';

// OCR 逐页状态由后端给出：text=资料自带文本层，pending=待识别，recognized=已识别，
// reviewed=人工校对，failed=识别失败。页面按覆盖度展示，而不是只显示“需 OCR”。
function pageNumberList(pages) {
  return [...new Set((pages || []).map(Number).filter(Number.isFinite))].sort((a, b) => a - b);
}

// 把页码压缩成 “1–3、7、31” 这样的可读区间（第 31 页与第 1 页不会互相吞掉）。
function pageRangesText(pages) {
  const numbers = pageNumberList(pages);
  if (!numbers.length) return '无';
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
  if (info.coveredCount > 0) tags.append(statusTag(`可识别 ${info.coveredCount} 页`, 'ok', '✓'));
  if (info.pending.length) tags.append(statusTag(`待识别 ${info.pending.length} 页`, 'warn', '!'));
  if (info.failed.length) tags.append(statusTag(`失败 ${info.failed.length} 页`, 'bad', '✕'));
  if (info.reviewed.length) tags.append(statusTag(`人工校对 ${info.reviewed.length} 页`, 'info', '校'));
  if (!tags.childNodes.length) tags.append(statusTag('待识别', 'warn', '!'));
  return tags;
}

function renderDocCoverage(doc) {
  const info = coverageSummary(doc);
  if (!info.pageCount) return null;
  const rows = [
    el('span', {}, `覆盖 ${info.coveredCount}/${info.pageCount} 页（识别范围第 ${info.pageFrom}–${info.pageTo} 页）`),
  ];
  if (info.pending.length) {
    rows.push(el('span', { class: 'pg-ocr-flag', 'data-tone': 'warn' }, `待识别：第 ${pageRangesText(info.pending)} 页`));
  }
  if (info.failed.length) {
    rows.push(el('span', { class: 'pg-ocr-flag', 'data-tone': 'bad' }, `识别失败：第 ${pageRangesText(info.failed)} 页`));
  }
  if (info.reviewed.length) rows.push(el('span', {}, `人工校对：第 ${pageRangesText(info.reviewed)} 页`));
  if (!info.complete && info.nextFrom !== null) {
    rows.push(el('span', {}, `可继续识别：第 ${info.nextFrom}–${info.nextTo} 页`));
  }
  return el('div', { class: 'pg-ocr-coverage' },
    el('b', {}, `OCR 覆盖度 · 资料 v${info.docVersion}`), ...rows);
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

  const tabBar = el('div', { class: 'pg-mini-tabs', role: 'tablist', 'aria-label': '笔记来源筛选' });
  for (const [key, label] of [['all', '全部笔记'], ['private', '我的笔记'], ['official', '官方清单']]) {
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
  const docCountBadge = badge(`${allDocs.length} 份`);
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
    const pFrom = input('起始页', { type: 'number', value: startFrom, min: 1, max: totalPages });
    const pTo = input('结束页', {
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
      if (!jobs.length) throw new Error('没有需要识别的页码。');
      // 逐段向服务端确认页数与费用，再一次性确认执行。
      const confirmations = [];
      for (const range of jobs) {
        const estimate = await api(`/documents/${docItem.id}/ocr`, {
          method: 'POST',
          data: { ...payload, page_from: range.from, page_to: range.to, confirmed: false },
        });
        confirmations.push(estimate.notice || `即将用 ${payload.model} 识别第 ${range.from}–${range.to} 页。`);
      }
      if (!window.confirm(confirmations.join('\n\n'))) return null;
      const results = [];
      for (const range of jobs) {
        statusBox.replaceChildren(loading(`正在用 ${payload.model} 识别第 ${range.from}–${range.to} 页…`));
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
          notice(`识别已转入后台任务：第 ${range.from}–${range.to} 页`
            + `${response.estimated_calls ? `，预计 ${response.estimated_calls} 次模型调用` : ''}。`
            + '可关闭对话框先去整理其它资料，完成后资料卡会自动刷新。'),
          progressMount);
        let finished = false;
        const finish = async job => {
          if (finished) return;
          finished = true;
          try { await refreshDocsInPlace(); } catch { /* 列表刷新失败不阻塞结果展示 */ }
          if (!job || !['completed', 'succeeded'].includes(job.status)) {
            statusBox.prepend(notice('OCR 后台任务未完成（已取消或失败）。已识别的页面会保留，可稍后重试。', 'warning'));
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
      return button(`只重试失败页（第 ${pageRangesText(failedPages)} 页）`, ev => busy(ev.currentTarget, async () => {
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
        notice(`识别结果已按页合并保存：覆盖 ${updated.coveredCount}/${updated.pageCount} 页，资料版本 v${updated.docVersion}。`),
      ];
      if (preserved.length) {
        blocks.push(notice(
          `第 ${pageRangesText(preserved)} 页此前已人工校对，本次识别结果没有改写这些页，已保留人工校对版本，请审阅新旧识别结果。`,
          'warning',
        ));
      }
      if (failed.length) {
        const details = merged.ocr_pages
          .filter(page => failed.includes(Number(page.page)) && page.error)
          .map(page => `第 ${page.page} 页：${page.error}`)
          .join('；');
        blocks.push(notice(
          `第 ${pageRangesText(failed)} 页识别失败${details ? `（${details}）` : ''}。其它页已正常保存，可只重试失败页。`,
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
          placeholder: page.state === 'failed' ? '该页识别失败，可人工补录该页文字后保存' : '',
        }),
      }));
      if (editors.length) {
        blocks.push(notice('如需修正识别文字，请修改后保存；保存的页会标记为「人工校对」，之后重跑不会被改写。'));
      }
      const saveButton = editors.length ? button('保存校对后的文本（标记为人工校对）', ev => busy(ev.currentTarget, async () => {
        const saved = await api(`/documents/${docItem.id}/ocr-text`, {
          method: 'PATCH',
          data: { pages: editors.map(ed => ({ page: ed.page, text: ed.ta.value })) },
        });
        const savedCoverage = coverageSummary(saved);
        toast(savedCoverage.complete
          ? '校对文本已保存，资料已可用于整理。'
          : `校对文本已保存；识别范围内还有 ${savedCoverage.missing.length} 页没文本。`,
        !savedCoverage.complete);
        dlg.close();
        await refreshDocsInPlace();
      }), '', 'check') : null;
      statusBox.replaceChildren(
        ...blocks,
        ...editors.map(ed => field(`第 ${ed.page} 页识别文本${ed.failed ? '（识别失败，可人工补录）' : ''}`, ed.ta)),
        el('div', { class: 'row end', style: 'margin-top:12px' }, saveButton),
      );
      if (failed.length) retryBox.replaceChildren(retryFailedButton(failed));
      else retryBox.replaceChildren();
      if (updated.complete) toast('识别范围内所有页都已有文本，资料可用于整理。');
    }

    const runBtn = button('确认页数并执行视觉 OCR', e => busy(e.currentTarget, async () => {
      const from = Math.max(1, Number(pFrom.value) || 1);
      const to = Math.min(totalPages, Number(pTo.value) || from);
      if (to < from) throw new Error('结束页不能小于起始页。');
      const results = await runOcr(ocrPicker.payload(false), [{ from, to }]);
      if (!results) return;
      showResult(results);
      await refreshDocsInPlace();
    }), '', 'scan');

    const coverageNotices = [];
    coverageNotices.push(
      `该 PDF 共 ${totalPages} 页；识别范围第 ${info.pageFrom}–${info.pageTo} 页，已覆盖 ${info.coveredCount}/${info.pageCount} 页。`,
    );
    if (info.pending.length) {
      coverageNotices.push(`待识别：第 ${pageRangesText(info.pending)} 页；可继续识别范围为第 ${info.nextFrom}–${info.nextTo} 页。`);
    }
    if (info.failed.length) {
      coverageNotices.push(`识别失败：第 ${pageRangesText(info.failed)} 页，可只重试这些页。`);
    }
    if (info.reviewed.length) {
      coverageNotices.push(`已人工校对：第 ${pageRangesText(info.reviewed)} 页；重新识别会保留人工校对文本，只在结果中提示审阅。`);
    }

    dlg.append(
      el('div', { class: 'card-header' }, el('h3', {}, `扫描件 OCR · ${docItem.title}`), button('关闭', () => dlg.close(), 'ghost small', 'close')),
      notice(coverageNotices.join(' '), info.failed.length ? 'warning' : ''),
      ocrPicker.node,
      el('div', { class: 'form-grid', style: 'margin:12px 0' }, field('起始页', pFrom), field('结束页（单次≤30页）', pTo)),
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
    docCountBadge.textContent = `${allDocs.length} 份`;
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
              d.index_status === 'stale' ? statusTag('索引待重建', 'warn', '!') : null,
              renderDocStatusTags(info)),
            el('p', { class: 'pg-doc-meta' }, `${d.pages || 0} 页 · ${d.chunks || 0} 个结构块`),
            renderDocCoverage(d),
            info.failed.length
              ? el('p', { class: 'pg-doc-hint' },
                '有页面识别失败，可只重试这些页：',
                button(`重试第 ${pageRangesText(info.failed)} 页`, () => openOcrDialog(d, { retryFailed: true }),
                  'ghost small', 'rotate'))
              : null),
          el('div', { class: 'pg-doc-actions' },
            d.index_status === 'stale'
              ? button('重建索引', e => busy(e.currentTarget, async () => {
                // 4.5/3.2-12：OCR 校对后向量可能过期，需要显式重建语义索引。
                const res = await api(`/documents/${d.id}/reindex`, { method: 'POST' });
                toast(res.notice || '索引已重建，语义检索已恢复。');
                await refreshDocsInPlace();
              }), 'soft small', 'rotate')
              : null,
            canOcr ? button(d.status === 'partial' ? '继续 OCR' : 'OCR 识别',
              () => openOcrDialog(d), 'soft small', 'scan') : null,
            info.failed.length ? button('只重试失败页',
              () => openOcrDialog(d, { retryFailed: true }), 'ghost small', 'rotate') : null,
            button('', e => busy(e.currentTarget, async () => {
              if (!window.confirm(`删除资料「${d.title}」？`)) return;
              await api(`/documents/${d.id}`, { method: 'DELETE' });
              selectedDocIds.delete(d.id);
              await refreshDocsInPlace();
            }), 'ghost small', 'trash', { 'aria-label': `删除 ${d.title}` }))));

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
          docCheckboxesWrap.append(el('label', { class: 'doc-check-pill' }, cb, el('span', {}, `${d.title}（${d.chunks}块）`)));
        }
      }
    } else {
      docList.append(el('p', { class: 'help' }, '还没有资料。上传课件、笔记或教材节选（支持多文档合并整理）。'));
    }
    if (!docCheckboxesWrap.childNodes.length) {
      docCheckboxesWrap.append(el('p', { class: 'help' }, '暂无可用文字资料，请在上方上传或对扫描 PDF 执行 OCR。'));
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
    '添加学习资料（不刷新表单）',
    '支持 PDF、DOCX、TXT、Markdown；按标题结构智能分块；扫描 PDF 支持视觉 OCR。',
    '.pdf,.docx,.txt,.md',
    async file => {
      uploadFeedback.replaceChildren(loading('正在按标题结构解析资料…'));
      try {
        const fd = new FormData();
        fd.set('file', file);
        const result = await api('/documents', { method: 'POST', body: fd });
        uploadFeedback.replaceChildren();
        if (result.status === 'ready') selectedDocIds.add(result.id);
        const coverage = result.coverage || {};
        toast(result.status === 'needs_ocr'
          ? `检测到 ${result.pages} 页扫描件，没有可提取文字；点击资料右侧「OCR 识别」后即可整理。`
          : result.status === 'partial'
            ? `资料「${result.title}」只有部分页有文本（${coverage.covered_count || 0}/${coverage.page_count || result.pages} 页），可继续 OCR 未识别页。`
            : `资料「${result.title}」已解析为 ${result.chunks} 个结构块。`);
        await refreshDocsInPlace();
      } catch (error) {
        uploadFeedback.replaceChildren(notice(error.message, 'error'));
      }
    }
  );
  docPicker.style.marginTop = '14px';
  docPicker.style.padding = '16px 12px';

  const docsArea = el('section', { class: 'card', style: 'margin-bottom:20px' },
    el('div', { class: 'card-header' }, el('h3', {}, '我的资料'), docCountBadge),
    docsRes.embedding_enabled ? notice('本站已启用语义检索。上传或执行 OCR 后，解析出的资料文字会发送到站点配置的向量服务。') : null,
    docList, docPicker, uploadFeedback);

  const toggleManual = () => {
    manualComposer.classList.toggle('hide');
    if (!manualComposer.classList.contains('hide')) manualComposer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  // Manual Note Composer (also allows linking to Subject & KP for official notes!)
  const manualTitle = input('例如：导数的几何意义 · 知识清单');
  const manualSubj = select([{ value: '', label: '关联学科（可选）' }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))]);
  const manualKp = select([{ value: '', label: '关联知识点（可选）' }]);
  manualSubj.addEventListener('change', () => {
    const filtered = kps.filter(k => !manualSubj.value || k.subject === manualSubj.value);
    manualKp.replaceChildren(
      el('option', { value: '' }, '关联知识点（可选）'),
      ...filtered.map(k => el('option', { value: k.id }, `${k.level === 1 ? '【章】' : k.level === 2 ? '▸ ' : '　· '}${k.name}`))
    );
  });
  const manualContent = el('textarea', { class: 'note-editor', rows: 9, placeholder: '支持 Markdown 与 LaTeX 公式（$...$ / $$...$$）。管理员保存后可发布为官方知识点清单。' });
  manualComposer.append(
    el('div', { class: 'card-header' }, el('h2', {}, '新建知识笔记'), button('收起', toggleManual, 'ghost small')),
    field('笔记标题', manualTitle),
    el('div', { class: 'form-grid', style: 'margin-top:12px' }, field('学科', manualSubj), field('知识点', manualKp)),
    el('div', { style: 'margin-top:14px' }, field('正文', manualContent)),
    el('div', { class: 'row end', style: 'margin-top:16px' },
      button('保存笔记', e => busy(e.currentTarget, async () => {
        if (!manualTitle.value.trim() || !manualContent.value.trim()) throw new Error('请填写笔记标题和正文。');
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
        toast('笔记已保存并记录 v1 版本。');
        await renderRoute();
      }), '', 'check'))
  );

  // AI Notes Agent Composer（三步：选资料 → 设目标 → 确认生成；高级项收进 <details>）
  const pre = state.notePreselectKp;
  state.notePreselectKp = null;

  const sourceModeSel = select([
    { value: 'user_docs', label: '① 纯用我的上传资料（支持多文档合并）' },
    { value: 'official_plus_docs', label: '② 官方清单为骨架 + 我的资料补充（双色标注）' },
    { value: 'wrongbook', label: '③ 从我的错题本生成个人易错清单' },
  ], pre?.source_mode || 'user_docs');

  const agentSubj = select(
    [{ value: '', label: '选择学科（启用学科专属提示词）' }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    pre?.subject || ''
  );
  const agentKp = select([{ value: '', label: '关联知识点（可选，用于联动题库/官方/错题）' }]);
  function populateAgentKps() {
    const filtered = kps.filter(k => !agentSubj.value || k.subject === agentSubj.value);
    agentKp.replaceChildren(
      el('option', { value: '' }, '关联知识点（可选）'),
      ...filtered.map(k => el('option', { value: k.id }, `${k.level === 1 ? '【章】' : k.level === 2 ? '▸ ' : '　· '}${k.name}`))
    );
    if (pre?.kp_id) agentKp.value = String(pre.kp_id);
  }
  populateAgentKps();

  const requirements = el('textarea', {
    rows: 2,
    placeholder: '可选补充要求（L4 层，仅影响形式与侧重，不能覆盖安全与引用规则）…',
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
      ? `提纲前预计调用模型 ${chunks} 次。返回格式有误的块会重试一次。`
      : '勾选已解析的资料后可使用。';
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
      toast(`笔记结果刷新失败：${error.message}`, true);
    }
  }

  const docPickerField = el('div', { style: 'margin:14px 0' },
    el('div', { class: 'field-label', style: 'margin-bottom:8px' }, '选择要整理的资料（可多选）'),
    docCheckboxesWrap);

  const estimateBox = el('div', { class: 'pg-estimate', 'aria-live': 'polite' });

  function selectedDocStats() {
    const docs = allDocs.filter(d => selectedDocIds.has(d.id));
    return { count: docs.length, chunks: docs.reduce((sum, d) => sum + Number(d.chunks || 0), 0) };
  }

  function modelSummary() {
    try {
      const payload = controls.payload(false);
      return payload.cross_verify ? `${payload.model} + ${payload.verify_model}（交叉核验）` : payload.model;
    } catch (error) {
      return `尚未就绪：${error.message}`;
    }
  }

  // 生成前把来源、资料块估算、模型与高级项摊开，避免用户盲点付费调用。
  function updateEstimate() {
    const mode = sourceModeSel.value;
    const stats = selectedDocStats();
    const modeLabel = ({
      user_docs: '我的上传资料',
      official_plus_docs: '官方清单为骨架 + 我的资料补充',
      wrongbook: '我的错题本易错清单',
    })[mode] || mode;
    const extra = mode !== 'wrongbook' && mapExtractChk.checked ? '逐块抽取后再生成提纲' : '直接按资料结构生成';
    estimateBox.replaceChildren(
      el('p', {}, el('strong', {}, '生成前确认'), ' · 来源、资料块估算与使用模型'),
      el('ul', { class: 'pg-estimate-list' },
        el('li', {}, `整理来源：${modeLabel}`),
        el('li', {}, mode === 'wrongbook'
          ? '资料块：使用你的错题记录，不占用资料库结构块'
          : `资料块：已选 ${stats.count} 份资料 · 预计 ${stats.chunks} 个结构块`),
        el('li', {}, `使用模型：${modelSummary()}`),
        el('li', {}, `生成方式：${extra}${confirmOutlineChk.checked ? '；先生成提纲并暂停确认' : ''}`)),
      el('p', { class: 'help' }, '生成前不会自动发起额外付费调用；模型调用按所选模型的单价计费。'));
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

  const generateButton = button('启动知识整理 Agent', e => busy(e.currentTarget, async () => {
    const sMode = sourceModeSel.value;
    const docIds = [...selectedDocIds];
    if (sMode === 'user_docs' && !docIds.length) {
      throw new Error('请先在上方勾选至少一份已解析的学习资料。');
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
    el('summary', {}, '高级设置 · 模型与多模型核验 / 提纲暂停 / 逐块抽取'),
    el('div', { class: 'pg-advanced-body' },
      el('div', {},
        el('div', { class: 'field-label', style: 'margin-bottom:8px' }, '模型与交叉核验'),
        controls.node),
      el('label', { class: 'check-label', style: 'margin-top:12px' },
        confirmOutlineChk, '人在环中：先生成提纲并暂停，待我勾选/修改章节后再分节并行生成正文'),
      el('label', { class: 'check-label', style: 'margin-top:10px' },
        mapExtractChk, '逐块抽取概念、公式、适用条件和例子（Map-Reduce），合并去重后再生成提纲'),
      mapEstimate));

  const stepOne = el('section', { class: 'pg-step', 'aria-label': '第 1 步：选资料' },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '1'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, '选资料'),
      el('p', { class: 'pg-card-sub' }, '从我的资料、官方清单 + 资料，或错题本生成；扫描件先完成 OCR。'),
      field('整理来源模式', sourceModeSel),
      docPickerField));

  const stepTwo = el('section', { class: 'pg-step', 'aria-label': '第 2 步：设目标与输出形态' },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '2'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, '设目标与输出形态'),
      el('p', { class: 'pg-card-sub' },
        '输出形态、学段、详略、内容侧重与自定义要求直接在这里选择；模型与核验等技术参数收在「高级设置」。'),
      el('div', { class: 'pg-form-grid' },
        field('学科适配（L2）', agentSubj),
        field('联动知识点（用于调用题库真题、官方清单与个人错题工具）', agentKp)),
      el('div', { class: 'pg-grid-config', style: 'margin-top:16px' },
        el('div', {},
          el('div', { class: 'field-label', style: 'margin-bottom:8px' }, '方格提示词组合（输出形态等 6 维）'),
          promptGridComp.node),
        field('自定义补充要求（L4）', requirements))));

  const stepThree = el('section', { class: 'pg-step', 'aria-label': '第 3 步：确认生成' },
    el('div', { class: 'pg-step-index', 'aria-hidden': 'true' }, '3'),
    el('div', { class: 'pg-step-body' },
      el('h3', { class: 'pg-step-title' }, '确认生成'),
      advancedDetails,
      estimateBox,
      el('div', { class: 'pg-form-actions' }, generateButton)));

  composer.append(
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('p', { class: 'pg-kicker' }, 'AI 整理 · 三步完成'),
        el('h2', {}, '从资料到知识清单'),
        el('p', { class: 'pg-card-sub' },
          '选资料 → 设目标与输出形态 → 确认生成。方格选择与自定义要求在主步骤可见；提纲暂停、Map-Reduce 与多模型核验保留在高级设置。'))),
    el('div', { class: 'pg-steps' }, stepOne, stepTwo, stepThree),
    jobMount);
  updateEstimate();

  // 打开生成的笔记 / 从首页或讲解卡片跳进来时，优先展示被选中的笔记，
  // 清掉上次 AI 任务与分区残留，避免一进知识清单又跳回 AI 表单。
  if (state.selectedNote) {
    noteSection = 'notes';
  } else if (pre || state.noteJobId) {
    noteSection = 'ai';
    if (pre) toast(`已为你预设「${pre.name}」错题易错清单生成配置。`);
  }

  // ---------- 三个入口：我的笔记 / 资料库 / AI 整理 ----------
  const sectionDefs = [
    ['notes', '我的笔记', 'notes'],
    ['docs', '资料库', 'file'],
    ['ai', 'AI 整理', 'sparkle'],
  ];
  const sectionTabs = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': '知识清单分区' });
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
      '扫描件按页显示 OCR 状态：可识别 / 待识别 / 失败 / 人工校对。人工校对过的页不会被重新识别覆盖。'));

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
      heading('KNOWLEDGE WORKSPACE', '把零散的资料，连成知识。',
        '我的笔记、资料库与 AI 整理分开展示；整理按「选资料 → 设目标 → 确认生成」推进。',
        el('div', { class: 'pg-actions' },
          button('新建笔记', () => { switchSection('notes'); toggleManual(); }, 'secondary', 'edit'),
          button('AI 整理资料', () => switchSection('ai'), '', 'sparkle')))),
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
    detailArea.replaceChildren(loading('正在打开笔记…'));
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
    noteCount.textContent = `共 ${items.length} 份笔记`;
    listArea.replaceChildren();
    if (!items.length) {
      // Fix empty-state copy mismatch (§9.1): match actual button label "AI 整理资料"
      listArea.append(el('div', { class: 'card' }, empty(
        state.noteTab === 'official' ? '官方清单尚未发布' : '还没有知识笔记',
        state.noteTab === 'official'
          ? '管理员审核发布后会在这里展示，不会用自动生成内容冒充官方资料。'
          : '切换到「AI 整理」或点击「新建笔记」，开始沉淀你的知识清单。',
        null, 'notes'
      )));
      if (!state.selectedNote) {
        detailArea.replaceChildren(empty('给知识，一个清晰的结构', '左侧选择笔记，在这里阅读、编辑、查看版本历史与资料来源。',
          button('AI 整理资料', () => switchSection('ai'), 'soft small', 'sparkle'), 'notes'));
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
          badge(note.origin === 'official' ? '官方清单' : '我的笔记', note.origin === 'official' ? 'blue' : 'teal'),
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
  if (own) actions.append(button('编辑', () => editNote(note, container, reload, onPatched), 'secondary small', 'edit'));
  actions.append(
    button('思维导图视图', () => {
      if (mindmapMount.childNodes.length) { mindmapMount.replaceChildren(); return; }
      const tree = markdownToMindmapTree(note.title, note.content || '');
      mindmapMount.replaceChildren(renderMindmapWidget(tree, { title: `${note.title} · 思维导图` }));
    }, 'soft small', 'eye'),
    button('导出 MD', () => downloadText(`${note.title}.md`, note.content || ''), 'ghost small', 'download'),
    button('Anki CSV', async e => busy(e.currentTarget, async () => {
      const csv = await api(`/notes/${note.id}/export?format=anki`, { rawText: true });
      downloadText(`${note.title}_anki.csv`, csv, 'text/csv;charset=utf-8');
      toast('已导出 Anki 卡片 CSV，可直接导入 Anki。');
    }), 'ghost small', 'download'),
    button('导出 .mmd', async e => busy(e.currentTarget, async () => {
      const mm = await api(`/notes/${note.id}/export?format=mindmap`, { rawText: true });
      downloadText(`${note.title}_mindmap.mmd`, mm, 'text/plain;charset=utf-8');
      toast('已导出 Mermaid Mindmap 文件。');
    }), 'ghost small', 'download'),
    button('打印/PDF', () => window.print(), 'ghost small'),
    button('版本历史', e => busy(e.currentTarget, async () => {
      if (versionsMount.childNodes.length) { versionsMount.replaceChildren(); return; }
      const res = await api(`/notes/${note.id}/versions`);
      const box = el('div', { class: 'sources', style: 'margin:12px 0' },
        el('strong', { class: 'small-text' }, `历史版本（共 ${res.items?.length || 0} 个）`));
      for (const v of res.items || []) {
        box.append(el('div', { class: 'row between source-block' },
          el('div', {}, el('strong', {}, `v${v.version_no} · ${v.change_note || '保存'}`), el('div', { class: 'help' }, `${dateText(v.created_at)} · ${v.chars} 字`)),
          own ? button('回滚到此版本', ev => busy(ev.currentTarget, async () => {
            if (!window.confirm(`确认回滚到版本 v${v.version_no}？当前内容也会保留在版本记录中。`)) return;
            await api(`/notes/${note.id}/rollback/${v.id}`, { method: 'POST' });
            toast(`已回滚至 v${v.version_no}。`);
            reload();
          }), 'soft small', 'history') : null));
      }
      versionsMount.replaceChildren(box);
    }), 'ghost small', 'history')
  );

  if (state.user.role === 'admin' && note.origin !== 'official') {
    actions.append(button('发布为官方清单', e => busy(e.currentTarget, async () => {
      if (!window.confirm('发布后所有账号都能阅读这份笔记。请确认内容与来源已经审核。')) return;
      await api(`/notes/${note.id}/publish`, { method: 'POST' });
      toast('已发布为官方清单。');
      await renderRoute();
    }), 'soft small', 'shield'));
  }
  if (own) {
    actions.append(button('删除', e => busy(e.currentTarget, async () => {
      if (!window.confirm('删除这份笔记？此操作会移除保存的笔记及版本历史。')) return;
      await api(`/notes/${note.id}`, { method: 'DELETE' });
      state.selectedNote = null;
      toast('笔记已删除。');
      await renderRoute();
    }), 'danger small', 'trash'));
  }

  fill(container,
    el('div', { class: 'row between', style: 'margin-bottom:12px' },
      el('div', { class: 'row' },
        badge(note.origin === 'official' ? '官方知识清单' : '私有笔记', note.origin === 'official' ? 'blue' : 'teal'),
        note.subject ? badge(note.subject) : null),
      el('span', { class: 'help' }, dateText(note.updated_at || note.created_at))),
    el('h2', {}, note.title),
    actions,
    mindmapMount,
    versionsMount,
    note.origin === 'official'
      ? null
      : notice('这份笔记保存在你的私有空间，支持随时编辑与版本回滚。AI 生成内容与补充知识请结合资料原文核对。'),
    markdown(note.content || ''),
    note.sources?.length ? renderSources(note.sources) : null
  );
}

function editNote(note, container, reload, onPatched = null) {
  const title = input('笔记标题', { value: note.title });
  const changeNote = input('本次修改说明（例如：补充易错反例）', { value: '手动编辑' });
  const content = el('textarea', { class: 'note-editor', value: note.content || '' });
  container.replaceChildren(
    el('div', { class: 'card-header' }, el('h2', {}, '编辑知识笔记'), badge('Markdown + KaTeX')),
    field('标题', title),
    el('div', { style: 'margin-top:12px' }, field('修改说明（记录到版本历史）', changeNote)),
    el('div', { style: 'margin-top:14px' }, field('笔记内容', content)),
    el('div', { class: 'row end', style: 'margin-top:17px' },
      button('取消', reload, 'secondary'),
      button('保存笔记', e => busy(e.currentTarget, async () => {
        if (!title.value.trim()) throw new Error('请填写笔记标题。');
        const nextTitle = title.value.trim();
        await api(`/notes/${note.id}`, {
          method: 'PATCH',
          data: { title: nextTitle, content: content.value, change_note: changeNote.value.trim() || '手动编辑' },
        });
        onPatched?.({ title: nextTitle });
        toast('笔记已保存并生成新版本。');
        reload();
      }), '', 'check'))
  );
}
