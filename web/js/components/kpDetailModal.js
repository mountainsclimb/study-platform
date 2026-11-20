import { api } from '../core/api.js';
import { state } from '../core/state.js';
import { el, badge, button, loading, notice } from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { go } from '../core/router.js';

export async function openKpDetailDialog(kpId, fallbackName = '') {
  const dlg = el('dialog', { class: 'modal-dialog' });
  const body = el('div', {}, loading('正在加载知识点官方清单与统计…'));
  dlg.append(
    el('div', { class: 'card-header' },
      el('h3', {}, `📌 知识点详情${fallbackName ? ` · ${fallbackName}` : ''}`),
      button('关闭', () => dlg.close(), 'ghost small', 'close')),
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
      button(`刷该知识点真题（${data.question_count} 题）`, () => {
        dlg.close();
        state.filters = { subject: kp.subject, kp_id: String(kp.id) };
        go('practice', null, { subject: kp.subject, kp_id: kp.id });
      }, '', 'book'),
      button(`生成该知识点易错清单（错 ${data.wrong_count} 题）`, () => {
        dlg.close();
        state.notePreselectKp = { kp_id: kp.id, subject: kp.subject, name: kp.name, source_mode: 'wrongbook' };
        go('notes');
      }, 'soft', 'sparkle'));

    const officialBox = el('div');
    if (data.official_notes?.length) {
      for (const note of data.official_notes) {
        officialBox.append(el('section', { class: 'ai-mode-card' },
          el('div', { class: 'ai-mode-card-head' },
            el('div', { class: 'row' }, badge('官方知识清单', 'blue'), el('strong', {}, note.title)),
            button('在知识清单中打开', () => {
              dlg.close();
              state.selectedNote = note.id;
              state.noteTab = 'official';
              go('notes');
            }, 'ghost small', 'notes')),
          markdown(note.content || '')));
      }
    } else {
      officialBox.append(notice('该知识点暂未发布官方知识清单。你可以点击上方按钮刷真题，或在「知识清单」中用自己的资料整理。'));
    }

    body.replaceChildren(
      el('div', { class: 'row' }, badge(kp.subject, 'teal'), badge(`题库收录 ${data.question_count} 题`, 'blue'), badge(`我的错题 ${data.wrong_count} 题`, data.wrong_count ? 'red' : '')),
      kp.description ? el('p', { class: 'help', style: 'margin-top:8px' }, kp.description) : null,
      actions,
      officialBox
    );
  } catch (err) {
    body.replaceChildren(notice(err.message || '加载知识点详情失败', 'error'));
  }
}
