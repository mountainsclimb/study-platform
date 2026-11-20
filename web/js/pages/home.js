import { api } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, heading, number, dateText, resultLabel, resultBadge, readableSummary,
} from '../core/dom.js';
import { go, current } from '../core/router.js';

function inlineStat(value, label) {
  return el('div', { class: 'pg-inline-stat' },
    el('strong', {}, value),
    el('span', {}, label));
}

// 按实际活动决定首屏主操作：未完成的待核验作答 → 到期错题 → 新账号 → 常规继续。
function resolveNextStep(stats, dueItems) {
  const attempts = stats.recent_attempts || [];
  const pending = attempts.find(item => item.result === 'pending' || item.result === 'ungraded');
  if (pending) {
    return {
      title: '继续上次的练习',
      text: `最近一次作答（${dateText(pending.created_at)}）结果为「${resultLabel(pending.result)}」，回到原题核对步骤与答案。`,
      label: '继续练习',
      icon: 'book',
      run: () => go('practice', pending.qid),
      meta: `待核验作答：${dateText(pending.created_at)} · 作答记录会保留`,
    };
  }
  if (!Number(stats.practiced) && !Number(stats.attempts)) {
    return {
      title: '从选一个学科开始',
      text: '新账号还没有作答记录。先挑学科和年份，从一道真题开始建立自己的学习记录。',
      label: '去选学科',
      icon: 'book',
      run: () => go('practice'),
      meta: '题库共 ' + number(stats.total_questions) + ' 道真题，支持按学科、年份和题型筛选。',
    };
  }
  if (Number(stats.due_count) > 0 || dueItems.length) {
    return {
      title: '先把到期的错题过一遍',
      text: `错题本里有 ${number(stats.due_count)} 道到期。先回顾错因，再回到题库继续新题。`,
      label: '开始复习',
      icon: 'repeat',
      run: () => go('wrongbook'),
      meta: '连续复习会按掌握度自动调整下次出现时间。',
    };
  }
  return {
    title: '继续练习，保持手感',
    text: `已经练过 ${number(stats.practiced)} 道题。接着上次的节奏，从题库里再挑一题。`,
    label: '继续练习',
    icon: 'book',
    run: () => go('practice'),
    meta: '每道题都可以先独立作答，再对照答案与解析。',
  };
}

function dueQuestionButton(item) {
  const q = item.question || item;
  return el('button', {
    type: 'button',
    class: 'pg-due-item',
    onClick: () => go('practice', q.qid || item.qid),
  },
    el('span', { class: 'pg-due-subject' }, [q.subject, q.number_label ? `原卷第${q.number_label}题` : null].filter(Boolean).join(' · ') || '到期错题'),
    el('span', { class: 'pg-due-stem' }, readableSummary(q, 60) || '打开题目重新练习'),
    icon('chevron'));
}

function recentAttemptButton(attempt) {
  const answer = String(attempt.answer_text || '').trim();
  return el('button', {
    type: 'button',
    class: 'pg-recent-item',
    onClick: () => go('practice', attempt.qid),
    'aria-label': `打开这道题：${resultLabel(attempt.result)}`,
  },
    el('span', { class: 'pg-recent-main' },
      el('strong', {}, answer ? `作答 ${answer.slice(0, 24)}` : '图片作答'),
      el('small', {}, `${dateText(attempt.created_at)} · ${resultLabel(attempt.result)}`)),
    resultBadge(attempt.result));
}

function noteLink(item) {
  return el('button', {
    type: 'button',
    class: 'pg-note-link',
    onClick: () => { state.selectedNote = item.id; go('notes'); },
  },
    el('span', { class: 'pg-note-link-main' },
      el('strong', {}, item.title),
      el('small', {}, `${item.origin === 'official' ? '官方知识清单' : '私有笔记'} · ${dateText(item.updated_at || item.created_at)}`)),
    icon('chevron'));
}

function shortcutCard(title, text, name, run) {
  return el('button', { type: 'button', class: 'pg-shortcut', onClick: run },
    icon(name),
    el('strong', {}, title),
    el('small', {}, text));
}

export async function renderHome(container, version) {
  const [stats, reviews, notes] = await Promise.all([api('/stats'), api('/reviews'), api('/notes')]);
  if (!current(version)) return;
  const dueItems = reviews.items || [];
  const next = resolveNextStep(stats, dueItems);
  const recent = (stats.recent_attempts || []).slice(0, 4);
  const greet = new Date().getHours() < 12 ? '上午好' : new Date().getHours() < 18 ? '下午好' : '晚上好';
  const mastered = stats.mastery_counts
    ? Number(stats.mastery_counts['比较掌握'] || 0) + Number(stats.mastery_counts['熟练掌握'] || 0)
    : Number(stats.mastered || 0);

  const focusPrimary = el('section', { class: 'pg-focus pg-focus--primary', 'aria-labelledby': 'home-next-title' },
    el('p', { class: 'pg-kicker' }, '下一步 · NEXT STEP'),
    el('h2', { id: 'home-next-title' }, next.title),
    el('p', {}, next.text),
    el('p', { class: 'pg-focus-meta' }, next.meta),
    el('div', { class: 'pg-focus-actions' },
      button(next.label, next.run, '', next.icon)));

  const dueCard = el('section', { class: 'pg-focus', 'aria-labelledby': 'home-due-title' },
    el('p', { class: 'pg-kicker' }, '到期复习'),
    el('h2', { id: 'home-due-title' }, dueItems.length ? `${number(stats.due_count || dueItems.length)} 道待复习` : '暂无到期错题'),
    dueItems.length
      ? el('div', { class: 'pg-due-list' }, ...dueItems.slice(0, 3).map(dueQuestionButton))
      : el('p', {}, '完成练习后，答错的题会按掌握度安排复习，到期后出现在这里。'),
    el('div', { class: 'pg-focus-actions' },
      dueItems.length
        ? button('开始复习', () => go('wrongbook'), 'secondary small', 'repeat')
        : button('去练习', () => go('practice'), 'secondary small', 'book')));

  const recentCard = el('section', { class: 'pg-focus', 'aria-labelledby': 'home-recent-title' },
    el('p', { class: 'pg-kicker' }, '近况'),
    el('h2', { id: 'home-recent-title' }, recent.length ? '最近作答' : '还没有作答记录'),
    recent.length
      ? el('div', { class: 'pg-recent' }, ...recent.map(recentAttemptButton))
      : el('p', {}, '完成第一道题后，最近几次作答与结果会显示在这里。'),
    el('p', { class: 'pg-focus-meta' },
      stats.graded_attempts
        ? `已判定 ${number(stats.graded_attempts)} 次 · 正确率 ${Math.round(Number(stats.accuracy))}%`
        : '暂无已判定的作答'));

  const secondaryStats = el('div', { class: 'pg-inline-stats', 'aria-label': '学习统计' },
    inlineStat(`${number(stats.practiced)} 道`, '已练习题目'),
    inlineStat(`${number(stats.attempts)} 次`, '真实作答'),
    inlineStat(stats.graded_attempts ? `${Math.round(Number(stats.accuracy))}%` : '—', '正确率（已判定）'),
    inlineStat(`${number(stats.wrong_count)} 道`, `错题本 · 到期 ${number(stats.due_count)} 道`),
    inlineStat(`${number(mastered)} 道`, '已掌握（比较/熟练）'));

  const notesCard = el('section', { class: 'pg-card', 'aria-labelledby': 'home-notes-title' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', { id: 'home-notes-title' }, '知识积累'),
        el('p', { class: 'pg-card-sub' }, '把资料与错题整理成可复习的知识清单。')),
      button('全部笔记', () => go('notes'), 'ghost small')));
  if (notes.items?.length) {
    notesCard.append(el('div', { class: 'pg-card-list' }, ...notes.items.slice(0, 3).map(noteLink)));
  } else {
    notesCard.append(el('div', { class: 'pg-empty' },
      el('p', {}, '还没有知识笔记。上传学习资料，或用错题本生成个人易错清单。'),
      el('div', { class: 'pg-actions' }, button('整理我的资料', () => go('notes'), 'soft small', 'notes'))));
  }

  const shortcutsCard = el('section', { class: 'pg-card', 'aria-labelledby': 'home-shortcuts-title' },
    el('h2', { id: 'home-shortcuts-title', style: 'margin:0 0 14px;font-size:19px' }, '其他学习入口'),
    el('div', { class: 'pg-shortcut-row' },
      shortcutCard('拍题讲解', '拍照识别 → 核对题干 → 讲解', 'scan', () => go('upload')),
      shortcutCard('错题回访', '连续复习 · 错因与知识点聚合', 'repeat', () => go('wrongbook')),
      shortcutCard('整理知识清单', '资料库 · 方格提示词 · 多模型核验', 'notes', () => go('notes'))));

  container.replaceChildren(
    heading('LEARNING OVERVIEW', `${greet}，${state.user.username}`,
      '先完成下一件小事：继续练习、到期复习，或看看最近的进展。'),
    el('section', { class: 'pg-focus-grid', 'aria-label': '下一步学习任务' }, focusPrimary, dueCard, recentCard),
    secondaryStats,
    el('div', { class: 'pg-grid-2' }, notesCard, shortcutsCard));

  if (stats.subject_progress?.length) {
    const rows = stats.subject_progress
      .filter(item => Number(item.total))
      .map(item => {
        const total = Number(item.total) || 0;
        const done = Number(item.practiced || item.attempted || 0);
        return el('div', { class: 'pg-subject-row' },
          el('div', { class: 'row' }, el('strong', {}, item.subject), el('span', { class: 'pg-card-sub' }, `${number(done)} / ${number(total)}`)),
          el('div', { class: 'pg-progress-track' }, el('span', { style: `width:${total ? Math.min(100, done / total * 100) : 0}%` })));
      });
    if (rows.length) {
      container.append(el('section', { class: 'card pg-fold-card' },
        el('details', { class: 'pg-advanced' },
          el('summary', {}, `各学科练习进度（${rows.length} 个学科，按实际作答记录）`),
          el('div', { class: 'pg-advanced-body' }, el('div', { class: 'pg-subject-progress' }, ...rows)))));
    }
  }

  container.append(el('div', { class: 'footer-note' },
    '学习记录来自你的真实作答。AI 提供学习辅助，原卷答案、平台标注与 AI 生成内容分别标明来源。'));
}
