import { api } from '../core/api.js';
import { state } from '../core/state.js';
import { el, badge, button, loading, notice } from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { go } from '../core/router.js';
import { t } from '../core/i18n.js';

export async function openKpDetailDialog(kpId, fallbackName = '') {
  const dlg = el('dialog', { class: 'modal-dialog' });
  const body = el('div', {}, loading(t('kp.loadingTheOfficialKnowledgeListAndStatistics')));
  dlg.append(
    el('div', { class: 'card-header' },
      el('h3', {}, t('kp.knowledgePointDetails', { v1: fallbackName ? ` · ${fallbackName}` : '' })),
      button(t('common.close'), () => dlg.close(), 'ghost small', 'close')),
    body
  );
  document.body.append(dlg);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  dlg.showModal();

  try {
    const data = await api(`/knowledge-points/${encodeURIComponent(kpId)}/detail`);
    const kp = data.kp;
    const actions = el('div', { class: 'row', style: 'margin:14px 0 18px' },
      button(t('kp.practiceRealQuestionsOnThisPoint', { v1: data.question_count }), () => {
        dlg.close();
        state.filters = { subject: kp.subject, kp_id: String(kp.id) };
        go('practice', null, { subject: kp.subject, kp_id: kp.id });
      }, '', 'book'),
      button(t('kp.generateAMistakeListForThisPoint', { v1: data.wrong_count }), () => {
        dlg.close();
        state.notePreselectKp = { kp_id: kp.id, subject: kp.subject, name: kp.name, source_mode: 'wrongbook' };
        go('notes');
      }, 'soft', 'sparkle'));

    const officialBox = el('div');
    if (data.official_notes?.length) {
      for (const note of data.official_notes) {
        officialBox.append(el('section', { class: 'ai-mode-card' },
          el('div', { class: 'ai-mode-card-head' },
            el('div', { class: 'row' }, badge(t('home.noteOfficial'), 'blue'), el('strong', {}, note.title)),
            button(t('kp.openInKnowledgeNotes'), () => {
              dlg.close();
              state.selectedNote = note.id;
              state.noteTab = 'official';
              go('notes');
            }, 'ghost small', 'notes')),
          markdown(note.content || '')));
      }
    } else {
      officialBox.append(notice(t('kp.noOfficialKnowledgeListHasBeenPublished')));
    }

    body.replaceChildren(
      el('div', { class: 'row' }, badge(kp.subject, 'teal'), badge(t('kp.questionsInTheBank', { v1: data.question_count }), 'blue'), badge(t('kp.ofMyWrongAnswers', { v1: data.wrong_count }), data.wrong_count ? 'red' : '')),
      kp.description ? el('p', { class: 'help', style: 'margin-top:8px' }, kp.description) : null,
      actions,
      officialBox
    );
  } catch (err) {
    body.replaceChildren(notice(err.message || t('kp.failedToLoadKnowledgePointDetails'), 'error'));
  }
}
