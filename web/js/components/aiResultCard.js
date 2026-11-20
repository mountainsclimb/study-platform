import { api } from '../core/api.js';
import { state } from '../core/state.js';
import { el, badge, button, notice, busy, toast, copyText, resultLabel, resultBadge } from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { go } from '../core/router.js';
import { openKpDetailDialog } from './kpDetailModal.js';

export function renderSources(sources) {
  const details = el('details', { class: 'sources' }, el('summary', {}, `资料来源与引用（${sources.length}）`));
  for (const source of sources) {
    details.append(el('div', { class: 'source-block' },
      el('strong', {}, [
        source.title || `资料 ${source.document_id}`,
        source.page ? `第 ${source.page} 页` : null,
        source.heading_path || null,
        source.chunk_id ? `片段 ${source.chunk_id}` : null,
      ].filter(Boolean).join(' · ')),
      source.text ? el('div', {}, source.text) : null));
  }
  return details;
}

function confidenceBadge(v) {
  if (!v) return null;
  if (v.reference_conflict) return badge('🟡 原卷存疑 · 两说并列', 'amber');
  if (v.confidence === 'high') return badge('🟢 双模型一致 · 高可信', 'teal');
  if (v.confidence === 'medium') return badge('🟡 一致（无原卷对照/互审）', 'amber');
  if (v.status === 'disagreed' || v.confidence === 'low') return badge('🔴 分歧未解决 · 请核对', 'red');
  if (v.status === 'agreed') return badge('🟢 核验一致', 'teal');
  if (v.status === 'partial') return badge('🟡 部分完成', 'amber');
  return null;
}

function renderDivergenceSplit(verification) {
  const indep = verification?.independent_results || [];
  if (indep.length < 2) return null;
  const judge = verification.judge || verification.comparison;
  const divStep = judge?.diverge_at_step;
  const isDisagreed = verification.status === 'disagreed';

  const split = el('div', { class: 'divergence-grid' });
  for (const ans of indep) {
    const col = el('section', { class: 'divergence-col' },
      el('div', { class: 'row between', style: 'margin-bottom:10px' },
        el('strong', {}, `模型 ${ans.label || ''} · ${ans.provider_name || ''} / ${ans.model || ''}`),
        judge?.which_is_right === ans.label ? badge('裁判倾向此解', 'teal') : null));

    if (ans.structured?.steps?.length) {
      const stepsWrap = el('div', { class: 'structured-steps' });
      for (const st of ans.structured.steps) {
        const isDiv = divStep && Number(st.no) === Number(divStep);
        stepsWrap.append(el('div', { class: `step-item${isDiv ? ' step-diverge' : ''}` },
          el('div', { class: 'row between' },
            el('strong', {}, `${st.no}. ${st.title || ''}`),
            isDiv ? badge('分歧定位步骤', 'red') : (st.score_point ? badge('得分点', 'blue') : null)),
          markdown(st.content || '')));
      }
      col.append(stepsWrap);
    } else {
      col.append(markdown(ans.content || ''));
    }
    split.append(col);
  }

  if (isDisagreed) {
    return el('div', { class: 'divergence-panel' },
      el('h4', {}, '双模型独立解答并排对照'),
      split);
  }
  return el('details', { class: 'sources' },
    el('summary', {}, '对照两个模型的独立题解'),
    split);
}

export function renderAiResult(result, { task = '', qid = null, titleHint = '' } = {}) {
  const root = el('div', { class: 'ai-result-root' });
  if (!result) return root;

  // Verification banner
  if (result.verification) {
    const v = result.verification;
    const labels = {
      not_requested: '快速模式（未交叉核验）',
      agreed: '双模型独立结论一致',
      disagreed: '模型结论存在分歧，请重点核对',
      partial: '核验未完整完成，结论待确认',
      pending: '作答待确认',
    };
    const vBox = el('div', { class: `verification${['disagreed', 'partial', 'pending'].includes(v.status) || v.reference_conflict ? ' warning' : ''}` },
      el('div', { class: 'row between', style: 'margin-bottom:6px' },
        el('strong', {}, labels[v.status] || v.status),
        confidenceBadge(v)),
      v.verdict ? el('div', {}, `作答结论：${resultLabel(v.verdict)}`) : null,
      v.notice ? el('div', {}, v.notice) : null);

    if (v.programmatic?.comparable) {
      vBox.append(el('div', { class: 'small-text', style: 'margin-top:6px' },
        `程序化比对（${v.programmatic.method}）：模型 A =「${v.programmatic.value_a}」 vs 模型 B =「${v.programmatic.value_b}」`));
    }
    const judge = v.judge || v.comparison;
    if (judge) {
      vBox.append(el('div', { class: 'judge-box' },
        el('strong', {}, `独立裁判意见${judge.model ? `（${judge.model}）` : ''}：`),
        judge.diverge_at_step ? badge(`分歧位于第 ${judge.diverge_at_step} 步`, 'red') : null,
        judge.which_is_right && judge.which_is_right !== 'unsure' ? badge(`倾向：${judge.which_is_right}`, 'blue') : null,
        el('div', { style: 'margin-top:4px' }, judge.reason || judge.summary || ''),
        ...(judge.differences || []).map(d => el('div', {}, `· ${d}`))));
    }
    root.append(vBox);
  }

  if (result.verdict) {
    root.append(el('div', { class: 'row', style: 'margin-bottom:12px' }, resultBadge(result.verdict)));
  }

  // 结果卡展示题型/通法标签（4.5）：讲解与拍题结果都带 method 字段。
  if (result.method) {
    root.append(el('div', { class: 'row', style: 'margin:0 0 12px' },
      badge('题型 / 通法', 'blue'),
      el('span', { class: 'small-text' }, String(result.method))));
  }

  // Clickable Knowledge Point tags extracted by AI
  if (result.knowledge_points?.length) {
    const kpRow = el('div', { class: 'row', style: 'margin:10px 0 14px' }, el('span', { class: 'help' }, '相关知识点：'));
    for (const kpName of result.knowledge_points) {
      const matched = (state.knowledgePoints || []).find(k => k.name === kpName || kpName.includes(k.name));
      kpRow.append(el('button', {
        type: 'button',
        class: 'kp-chip',
        title: matched ? '查看官方知识清单与题库同类题' : '点击按关键词搜索同类题',
        onClick: () => {
          if (matched) {
            openKpDetailDialog(matched.id, matched.name);
          } else {
            state.filters = { q: kpName };
            go('practice', null, { q: kpName });
          }
        },
      }, `# ${kpName}`));
    }
    root.append(kpRow);
  }

  // Render Mode Cards (§6.3) or single content block
  const cards = result.mode_cards?.length ? result.mode_cards : (result.content ? [{ mode: result.mode || 'detailed', name: '', content: result.content }] : []);
  for (const card of cards) {
    const cardBox = el('div', { class: 'ai-mode-card' });
    const actions = el('div', { class: 'row' },
      button('复制', () => copyText(card.content || ''), 'ghost small', 'copy'),
      button('存为笔记', event => busy(event.currentTarget, async () => {
        const title = `${titleHint || (qid ? `题目讲解 ${qid}` : 'AI 讲解')} · ${card.name || '笔记'}`.slice(0, 120);
        const saved = await api('/notes', { method: 'POST', data: { title, content: card.content || '' } });
        toast('已保存到「知识清单 · 我的笔记」。');
        state.selectedNote = saved.id;
      }), 'ghost small', 'notes'));
    if (card.name || cards.length > 1) {
      cardBox.append(el('div', { class: 'ai-mode-card-head' },
        badge(card.name || 'AI 讲解', 'blue'),
        actions));
    } else if (task !== 'notes' && task !== 'grade') {
      cardBox.append(el('div', { class: 'row end', style: 'margin-bottom:8px' }, actions));
    }
    cardBox.append(markdown(typeof card.content === 'string' ? card.content : JSON.stringify(card.content, null, 2)));
    root.append(cardBox);
  }

  // Divergence split view or collapsible peer solutions
  const splitView = renderDivergenceSplit(result.verification);
  if (splitView) root.append(splitView);

  // 1-round Peer Review results
  if (result.verification?.peer_review?.revisions?.length) {
    const prDet = el('details', { class: 'sources' }, el('summary', {}, '查看 1 轮互审修正意见'));
    for (const rev of result.verification.peer_review.revisions) {
      prDet.append(el('div', { class: 'source-block' },
        el('strong', {}, `模型 ${rev.label}（${rev.model}）互审回应`),
        markdown(rev.content || '')));
    }
    root.append(prDet);
  }

  if (result.note_id) {
    root.append(el('div', { class: 'row', style: 'margin-top:12px' },
      button('打开生成的笔记', () => {
        // 清掉 AI 任务指针，避免知识清单页把「打开笔记」误判成回到进行中的 AI 整理表单。
        state.noteJobId = null;
        state.selectedNote = result.note_id;
        go('notes');
      }, 'soft small', 'notes')));
  }
  if (result.sources?.length) root.append(renderSources(result.sources));
  if (result.coverage) {
    const cov = result.coverage;
    root.append(notice(
      `资料覆盖：已整理 ${cov.included_chunks} / ${cov.total_chunks} 个片段（${cov.batches || 1} 批处理）。${cov.complete ? '全部片段已纳入处理。' : '尚有未处理内容，请核对完整性。'}`,
      cov.complete ? '' : 'warning'
    ));
  }
  if (result.rubric_source) root.append(el('p', { class: 'help', style: 'margin-top:12px' }, result.rubric_source));

  if (result.verification?.graders) {
    const detail = el('details', { class: 'sources' }, el('summary', {}, '查看两个模型的分步评分'));
    result.verification.graders.forEach((grader, index) => {
      if (!grader) {
        detail.append(el('div', { class: 'source-block' }, `模型 ${index + 1} 未返回可用评分。`));
        return;
      }
      const block = el('div', { class: 'source-block' },
        el('strong', {}, `评分模型 ${index + 1} · ${grader.score} / ${grader.max_score || 100} 分`));
      if (grader.feedback) block.append(el('p', {}, grader.feedback));
      for (const step of grader.steps || []) {
        block.append(el('div', {},
          el('strong', {}, `${step.label}：${step.earned} / ${step.possible}`),
          el('div', {}, step.feedback || '')));
      }
      detail.append(block);
    });
    for (const err of result.verification.errors || []) {
      detail.append(el('p', { class: 'job-error' }, err));
    }
    root.append(detail);
  }

  return root;
}
