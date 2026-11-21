import { api } from '../core/api.js';
import { state } from '../core/state.js';
import { el, badge, button, notice, busy, toast, copyText, resultLabel, resultBadge } from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { go } from '../core/router.js';
import { openKpDetailDialog } from './kpDetailModal.js';
import { t } from '../core/i18n.js';

export function renderSources(sources) {
  const details = el('details', { class: 'sources' }, el('summary', {}, t('result.sourcesCitations', { v1: sources.length })));
  for (const source of sources) {
    details.append(el('div', { class: 'source-block' },
      el('strong', {}, [
        source.title || t('result.source', { v1: source.document_id }),
        source.page ? t('result.page', { v1: source.page }) : null,
        source.heading_path || null,
        source.chunk_id ? t('result.chunk', { v1: source.chunk_id }) : null,
      ].filter(Boolean).join(' · ')),
      source.text ? el('div', {}, source.text) : null));
  }
  return details;
}

function confidenceBadge(v) {
  if (!v) return null;
  if (v.reference_conflict) return badge(t('result.originalPaperUncertainTwoAccounts'), 'amber');
  if (v.confidence === 'high') return badge(t('result.bothModelsAgreeHighConfidence'), 'teal');
  if (v.confidence === 'medium') return badge(t('result.agreeNoOriginalPaperCrossCheckOr'), 'amber');
  if (v.status === 'disagreed' || v.confidence === 'low') return badge(t('result.disagreementUnresolvedPleaseVerify'), 'red');
  if (v.status === 'agreed') return badge(t('result.verificationConsistent'), 'teal');
  if (v.status === 'partial') return badge(t('result.partiallyCompleted'), 'amber');
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
        el('strong', {}, t('result.model', { v1: ans.label || '', v2: ans.provider_name || '', v3: ans.model || '' })),
        judge?.which_is_right === ans.label ? badge(t('result.theJudgeFavorsThisAnswer'), 'teal') : null));

    if (ans.structured?.steps?.length) {
      const stepsWrap = el('div', { class: 'structured-steps' });
      for (const st of ans.structured.steps) {
        const isDiv = divStep && Number(st.no) === Number(divStep);
        stepsWrap.append(el('div', { class: `step-item${isDiv ? ' step-diverge' : ''}` },
          el('div', { class: 'row between' },
            el('strong', {}, `${st.no}. ${st.title || ''}`),
            isDiv ? badge(t('result.divergenceStep'), 'red') : (st.score_point ? badge(t('result.scorePoint'), 'blue') : null)),
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
      el('h4', {}, t('result.sideBySideIndependentSolutions')),
      split);
  }
  return el('details', { class: 'sources' },
    el('summary', {}, t('result.compareTheTwoModelsIndependentSolutions')),
    split);
}

export function renderAiResult(result, { task = '', qid = null, titleHint = '' } = {}) {
  const root = el('div', { class: 'ai-result-root' });
  if (!result) return root;

  // Verification banner
  if (result.verification) {
    const v = result.verification;
    const labels = {
      not_requested: t('result.fastModeNotCrossVerified'),
      agreed: t('result.bothModelsReachedTheSameConclusion'),
      disagreed: t('result.modelConclusionsDifferPleaseCheckCarefully'),
      partial: t('result.verificationIncompleteConclusionPending'),
      pending: t('result.answerPendingConfirmation'),
    };
    const vBox = el('div', { class: `verification${['disagreed', 'partial', 'pending'].includes(v.status) || v.reference_conflict ? ' warning' : ''}` },
      el('div', { class: 'row between', style: 'margin-bottom:6px' },
        el('strong', {}, labels[v.status] || v.status),
        confidenceBadge(v)),
      v.verdict ? el('div', {}, t('result.answerConclusion', { v1: resultLabel(v.verdict) })) : null,
      v.notice ? el('div', {}, v.notice) : null);

    if (v.programmatic?.comparable) {
      vBox.append(el('div', { class: 'small-text', style: 'margin-top:6px' },
        t('result.programmaticComparisonModelAVsModelB', { v1: v.programmatic.method, v2: v.programmatic.value_a, v3: v.programmatic.value_b })));
    }
    const judge = v.judge || v.comparison;
    if (judge) {
      vBox.append(el('div', { class: 'judge-box' },
        el('strong', {}, t('result.independentJudgeOpinion', { v1: judge.model ? `（${judge.model}）` : '' })),
        judge.diverge_at_step ? badge(t('result.divergenceAtStep', { v1: judge.diverge_at_step }), 'red') : null,
        judge.which_is_right && judge.which_is_right !== 'unsure' ? badge(t('result.favors', { v1: judge.which_is_right }), 'blue') : null,
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
      badge(t('result.questionTypeGeneralMethod'), 'blue'),
      el('span', { class: 'small-text' }, String(result.method))));
  }

  // Clickable Knowledge Point tags extracted by AI
  if (result.knowledge_points?.length) {
    const kpRow = el('div', { class: 'row', style: 'margin:10px 0 14px' }, el('span', { class: 'help' }, t('result.relatedKnowledgePoints')));
    for (const kpName of result.knowledge_points) {
      const matched = (state.knowledgePoints || []).find(k => k.name === kpName || kpName.includes(k.name));
      kpRow.append(el('button', {
        type: 'button',
        class: 'kp-chip',
        title: matched ? t('result.viewTheOfficialKnowledgeListAndSimilar') : t('result.clickToSearchSimilarQuestionsByKeyword'),
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
      button(t('result.copy'), () => copyText(card.content || ''), 'ghost small', 'copy'),
      button(t('result.saveAsNote'), event => busy(event.currentTarget, async () => {
        const title = `${titleHint || (qid ? t('result.questionWalkthrough', { v1: qid }) : t('settings.task.explain'))} · ${card.name || t('result.notes')}`.slice(0, 120);
        const saved = await api('/notes', { method: 'POST', data: { title, content: card.content || '' } });
        toast(t('result.savedToKnowledgeNotesMyNotes'));
        state.selectedNote = saved.id;
      }), 'ghost small', 'notes'));
    if (card.name || cards.length > 1) {
      cardBox.append(el('div', { class: 'ai-mode-card-head' },
        badge(card.name || t('settings.task.explain'), 'blue'),
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
    const prDet = el('details', { class: 'sources' }, el('summary', {}, t('result.viewOneRoundOfPeerReviewCorrections')));
    for (const rev of result.verification.peer_review.revisions) {
      prDet.append(el('div', { class: 'source-block' },
        el('strong', {}, t('result.modelPeerReviewResponse', { v1: rev.label, v2: rev.model })),
        markdown(rev.content || '')));
    }
    root.append(prDet);
  }

  if (result.note_id) {
    root.append(el('div', { class: 'row', style: 'margin-top:12px' },
      button(t('result.openTheGeneratedNote'), () => {
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
      t('result.sourceCoverageProcessedChunksBatches', { v1: cov.included_chunks, v2: cov.total_chunks, v3: cov.batches || 1 })
      + (cov.complete ? t('result.allChunksWereProcessed') : t('result.someContentWasNotProcessedPleaseCheck')),
      cov.complete ? '' : 'warning'
    ));
  }
  if (result.rubric_source) root.append(el('p', { class: 'help', style: 'margin-top:12px' }, result.rubric_source));

  if (result.verification?.graders) {
    const detail = el('details', { class: 'sources' }, el('summary', {}, t('result.viewStepByStepScoresFromBoth')));
    result.verification.graders.forEach((grader, index) => {
      if (!grader) {
        detail.append(el('div', { class: 'source-block' }, t('result.modelReturnedNoUsableScore', { v1: index + 1 })));
        return;
      }
      const block = el('div', { class: 'source-block' },
        el('strong', {}, t('result.scoringModel', { v1: index + 1, v2: grader.score, v3: grader.max_score || 100 })));
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
