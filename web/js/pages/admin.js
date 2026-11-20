import { api, ensureCatalog, ensureKnowledgePoints, ensurePromptGrid } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, icon, button, badge, heading, notice, field, input, select, statCard,
  number, loading, busy, toast, dateText, query, showImage, wireTabKeyboard,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { markdown } from '../core/markdown.js';
import { createModelPicker } from '../components/modelPicker.js';
import { renderKpTree } from '../components/kpTree.js';

// 内容管理三个分区：校勘 / 题库与知识点 / 运行与账号。模块级变量让
// renderRoute 重渲染（发布、导入、保存策略）后仍停留在当前分区。
let adminTab = 'issues';
// 登出/401 后清空分区记忆，避免下一个账号进入内容管理时停在上一账号的分区。
onSessionReset(() => { adminTab = 'issues'; });

function sharedReviewCard(title, path, initial, row, emptyText) {
  const content = el('div');
  const pageSize = 20;
  let offset = 0;
  let seq = 0;
  const refresh = async (nextOffset = offset) => {
    const mine = ++seq;
    try {
      const data = await api(`${path}?offset=${nextOffset}&limit=${pageSize}`, { latest: true, latestKey: path });
      if (mine !== seq) return;
      offset = nextOffset;
      render(data);
    } catch (error) {
      if (mine !== seq || error.aborted) return;
      content.replaceChildren(notice(error.message, 'warning'));
    }
  };
  function render(data) {
    const items = data.items || [];
    content.replaceChildren();
    if (data.error) {
      content.append(notice(data.error, 'warning'));
    } else if (items.length) {
      content.append(el('div', { class: 'mini-list' }, items.map(item => row(item, refresh))));
    } else {
      content.append(el('p', { class: 'help' }, emptyText));
    }
    if (offset > 0 || data.has_more) {
      content.append(el('div', { class: 'row end', style: 'margin-top:12px' },
        button('上一页', e => busy(e.currentTarget, () => refresh(offset - pageSize)), 'ghost small', null, { disabled: offset === 0 }),
        button('下一页', e => busy(e.currentTarget, () => refresh(offset + pageSize)), 'ghost small', null, { disabled: !data.has_more })));
    }
  }
  render(initial);
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, title)), content);
}

function describeTemplateConfig(config, dimensions) {
  return Object.entries(config).map(([key, value]) => {
    const dim = dimensions[key];
    const names = (Array.isArray(value) ? value : [value]).map(id =>
      dim?.options.find(option => option.id === id)?.name || id);
    return names.length ? `${dim?.label || key}：${names.join('、')}` : '';
  }).filter(Boolean).join(' · ');
}

// ---------------------------------------------------------------------------
// P1 「题目报错 → 待核对 → 待修订 / 确认无误 → 待发布 → 已发布」审核闭环
// ---------------------------------------------------------------------------

const ISSUE_STATUS_LABELS = {
  pending_check: '待核对',
  to_revise: '待修订',
  confirmed: '确认无误',
  to_publish: '待发布',
  published: '已发布',
};
const ISSUE_NEXT = {
  pending_check: ['to_revise', 'confirmed'],
  to_revise: ['to_publish', 'confirmed'],
  confirmed: ['to_publish'],
  to_publish: ['published', 'to_revise'],
  published: ['to_revise'],
};
const ISSUE_ACTION_LABELS = {
  to_revise: '标记待修订',
  confirmed: '确认无误',
  to_publish: '转入待发布',
  published: '发布修订',
};
const ISSUE_RISK_LABELS = { high: '高风险', medium: '中风险', low: '低风险' };

function statusBadge(status) {
  const type = status === 'published' ? 'teal'
    : status === 'confirmed' ? 'blue'
      : status === 'to_publish' ? 'amber'
        : status === 'to_revise' ? 'red' : '';
  return badge(ISSUE_STATUS_LABELS[status] || status, type);
}

function issueBeforeSummary(issue) {
  const before = issue.before || {};
  const parts = [];
  if (before.stem_excerpt) parts.push(el('p', { class: 'small-text' }, `当前显示：${before.stem_excerpt}`));
  if (before.options?.length) {
    parts.push(el('p', { class: 'small-text' },
      `选项：${before.options.map(o => `${o.label}. ${String(o.text || '').slice(0, 40)}`).join(' / ')}`));
  }
  if (before.answer_text) parts.push(el('p', { class: 'small-text' }, `参考答案：${before.answer_text.slice(0, 120)}`));
  if (before.images?.length) {
    const row = el('div', { class: 'row' });
    for (const image of before.images) {
      if (!image.file) continue;
      const url = `/api/bank-image?${query({ subject: issue.subject, file: image.file, preview: 'true' })}`;
      row.append(button(
        `原卷素材预览 ${String(image.file).split('/').pop()}`,
        () => showImage(url, '原卷素材（管理员预览）'),
        'ghost small', 'eye',
      ));
    }
    if (row.childNodes.length) parts.push(row);
  }
  if (before.block_ids?.length) {
    parts.push(el('p', { class: 'help' }, `原卷块：${before.block_ids.slice(0, 12).join('、')}`));
  }
  return parts;
}

function openTransitionDialog(issue, target, onDone) {
  const decision = el('textarea', { rows: 2, value: issue.decision || '' });
  const evidence = el('textarea', { rows: 2, value: issue.evidence || '' });
  const after = el('textarea', {
    rows: 6,
    value: issue.after ? JSON.stringify(issue.after, null, 2) : '',
    placeholder: '{"stem_plain": "校勘后的题干", "options": [{"label": "A", "text": "…"}], "answer_text": "A"}',
  });
  const dialog = el('dialog', { class: 'issue-dialog', 'aria-label': '填写校勘决定' },
    el('div', { class: 'row between' },
      el('h3', {}, `${ISSUE_STATUS_LABELS[issue.status] || issue.status} → ${ISSUE_STATUS_LABELS[target]}`),
      el('button', { type: 'button', class: 'close-dialog', 'aria-label': '关闭', onClick: () => dialog.close() }, '×')),
    el('p', { class: 'help' }, `题目 ${issue.qid} · ${issue.issue_type_label || ''}。发布前必须留下决定理由、原卷依据与拟修改内容，供题库重建消费。`),
    field('修改说明 / 决定理由', decision),
    field('原卷依据（页码、题号或块范围）', evidence),
    field('拟修改内容（JSON 对象）', after),
    el('div', { class: 'row end', style: 'margin-top:12px' },
      button('提交', event => busy(event.currentTarget, async () => {
        let afterValue = null;
        if (after.value.trim()) {
          try {
            afterValue = JSON.parse(after.value);
          } catch {
            throw new Error('拟修改内容必须是合法 JSON 对象');
          }
          if (typeof afterValue !== 'object' || Array.isArray(afterValue)) {
            throw new Error('拟修改内容必须是 JSON 对象');
          }
        }
        await api(`/admin/content-issues/${issue.id}/transition`, {
          method: 'POST',
          data: {
            status: target,
            decision: decision.value.trim(),
            evidence: evidence.value.trim(),
            after: afterValue,
          },
        });
        toast(`报错 #${issue.id} 已更新为「${ISSUE_STATUS_LABELS[target]}」。`);
        dialog.close();
        onDone();
      }), '', 'check')));
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
}

function contentIssuesCard(catalog) {
  const pageSize = 10;
  let offset = 0;
  let loadSeq = 0;
  const filters = { status: 'pending_check', subject: '', issue_type: '', risk: '' };
  const listMount = el('div');
  const totalBadge = el('span');

  const statusSel = select(
    [{ value: '', label: '全部状态' }, ...Object.entries(ISSUE_STATUS_LABELS).map(([value, label]) => ({ value, label }))],
    filters.status,
  );
  const subjectSel = select(
    [{ value: '', label: '全部学科' }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    '',
  );
  const typeSel = select([
    { value: '', label: '全部类型' },
    { value: 'formula', label: '公式' }, { value: 'stem_missing', label: '题干缺失' },
    { value: 'option', label: '选项' }, { value: 'image', label: '图片' },
    { value: 'answer', label: '参考答案' }, { value: 'other', label: '其他' },
  ], '');
  const riskSel = select([
    { value: '', label: '全部风险' },
    { value: 'high', label: '高风险' }, { value: 'medium', label: '中风险' }, { value: 'low', label: '低风险' },
  ], '');

  async function toggleIsolation(issue, refresh) {
    const active = !issue.isolated;
    let reason = '';
    if (active) {
      reason = window.prompt('请写明隔离依据（原卷位置、缺失内容或报错证据）', issue.evidence || issue.note || '');
      if (!reason || !reason.trim()) return;
    }
    await api(`/admin/questions/${encodeURIComponent(issue.qid)}/isolation`, {
      method: 'POST',
      data: { active, reason: reason.trim(), issue_id: issue.id },
    });
    toast(active ? '已临时隔离该题，作答与判分入口已暂停。' : '已撤销隔离。');
    await refresh(offset);
  }

  function render(data) {
    totalBadge.textContent = `共 ${data.total ?? 0} 条`;
    const items = data.items || [];
    listMount.replaceChildren();
    if (!items.length) {
      listMount.append(el('p', { class: 'help' },
        '当前筛选下没有题目报错。用户在题目页点击「报告问题」后，会按状态进入这里。'));
    }
    for (const issue of items) {
      const q = issue.question || {};
      const location = [q.year ? `${q.year} 年` : null, q.province, q.number_label ? `原卷第 ${q.number_label} 题` : null]
        .filter(Boolean).join(' · ');
      const numberText = q.number_label ? `原卷第 ${q.number_label} 题` : '题号待补';
      const evidenceText = String(issue.evidence || issue.note || '').trim();
      const actions = el('div', { class: 'pg-review-actions' },
        button(issue.isolated ? '撤销隔离' : '临时隔离', event => busy(event.currentTarget, () => toggleIsolation(issue, load)),
          issue.isolated ? 'soft small' : 'danger small', issue.isolated ? 'rotate' : 'stop'),
        ...(ISSUE_NEXT[issue.status] || []).map(target => button(
          ISSUE_ACTION_LABELS[target] || ISSUE_STATUS_LABELS[target],
          () => openTransitionDialog(issue, target, () => load(offset)),
          target === 'published' ? '' : 'secondary small',
          target === 'published' ? 'check' : null,
        )),
        issue.screenshot_url
          ? button('查看用户截图', () => showImage(issue.screenshot_url, '用户报错截图'), 'ghost small', 'eye')
          : null);
      const snapshot = el('div', { class: 'pg-review-snapshot' },
        el('p', {}, `题库版本：${issue.bank_version || '未知'} · 字段：${issue.field_label || issue.field || '—'} · 类型：${issue.issue_type_label || issue.issue_type || '—'}`),
        location ? el('p', {}, `原卷位置：${location}`) : el('p', {}, '原卷位置：未填写'),
        issue.revision
          ? el('p', {}, `生效状态：${issue.revision.label}`
            + (issue.revision.after_revision ? `（产出内容修订 ${issue.revision.after_revision}）` : '')
            + (issue.revision.current_revision ? ` · 题库当前内容修订 ${issue.revision.current_revision}` : '')
            + (issue.revision.revision_matches === true ? ' · 一致'
              : issue.revision.revision_matches === false ? ' · 不一致，需核对是否被后续修订覆盖' : ''))
          : null,
        ...issueBeforeSummary(issue));
      const body = el('div', { class: 'pg-review-body' },
        el('div', { class: 'pg-review-body-inner' },
          el('p', { class: 'pg-review-sub' }, '题目快照与证据'),
          snapshot,
          issue.note ? el('p', {}, issue.note) : null,
          issue.after
            ? el('details', { class: 'sources' },
              el('summary', {}, '拟修改内容（待发布后由题库重建消费）'),
              el('pre', { class: 'code-block' }, JSON.stringify(issue.after, null, 2)))
            : null,
          issue.decision ? el('p', { class: 'small-text' }, `决定：${issue.decision}`) : null,
          issue.evidence ? el('p', { class: 'help' }, `依据：${issue.evidence}`) : null,
          el('p', { class: 'help' },
            `${issue.reporter ? `报错人 ${issue.reporter}` : '匿名报错'}${issue.reviewer ? ` · 审核 ${issue.reviewer}` : ''}`),
          el('p', { class: 'pg-review-sub' }, '处理决定与发布确认'),
          actions));
      listMount.append(el('details', { class: 'pg-review-item' },
        el('summary', { class: 'pg-review-summary' },
          el('div', { class: 'pg-review-status' },
            statusBadge(issue.status),
            badge(ISSUE_RISK_LABELS[issue.risk] || issue.risk, issue.risk === 'high' ? 'red' : issue.risk === 'medium' ? 'amber' : ''),
            issue.revision?.state === 'applied'
              ? badge(`生效于 ${issue.revision.applied_in || '发布版本'}`, 'teal')
              : issue.revision?.state === 'pending_build'
                ? badge('待进入构建', 'amber')
                : null,
            issue.revision?.revision_matches === false ? badge('修订号不一致', 'red') : null,
            issue.isolated ? badge('已隔离', 'red') : null,
            badge(issue.field_label || issue.field),
            badge(issue.issue_type_label || issue.issue_type)),
          el('div', { class: 'pg-review-id' },
            el('strong', {}, numberText),
            el('small', {}, [location, issue.qid, `#${issue.id}`, dateText(issue.created_at), issue.bank_version].filter(Boolean).join(' · '))),
          el('p', { class: 'pg-review-evidence' },
            evidenceText ? `证据：${evidenceText}` : '证据：待补充原卷位置或缺失内容'),
          el('span', { class: 'pg-review-chevron' }, icon('chevron'))),
        body));
    }
    if (offset > 0 || data.has_more) {
      listMount.append(el('div', { class: 'row end', style: 'margin-top:12px' },
        button('上一页', () => load(offset - pageSize), 'ghost small', null, { disabled: offset === 0 }),
        button('下一页', () => load(offset + pageSize), 'ghost small', null, { disabled: !data.has_more })));
    }
  }

  async function load(nextOffset = offset) {
    const mine = ++loadSeq;
    offset = Math.max(0, nextOffset);
    filters.status = statusSel.value;
    filters.subject = subjectSel.value;
    filters.issue_type = typeSel.value;
    filters.risk = riskSel.value;
    listMount.replaceChildren(el('div', { class: 'loading' }, '正在加载报错队列…'));
    try {
      const data = await api(`/admin/content-issues?${query({ ...filters, offset, limit: pageSize })}`,
        { latest: true, latestKey: 'admin-content-issues' });
      if (mine !== loadSeq) return; // 旧筛选响应丢弃，避免覆盖新结果
      render(data);
    } catch (error) {
      if (mine !== loadSeq || error.aborted) return;
      listMount.replaceChildren(notice(error.message, 'warning'),
        button('重试', () => load(offset), 'secondary small', 'repeat'));
    }
  }

  const revisionLine = el('p', { class: 'help', style: 'margin-bottom:12px' },
    '修订生效状态：已审核（本条记录）→ 待进入构建（已发布但下一版题库尚未重建）→ 生效于版本（题库重建报告已收录）。');
  for (const control of [statusSel, subjectSel, typeSel, riskSel]) {
    control.addEventListener('change', () => load(0));
  }

  const card = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, '内容报错审核与校勘'),
      totalBadge),
    el('p', { class: 'help', style: 'margin-bottom:8px' },
      '用户报错自动带题库版本、题号、字段与问题类型。按「待核对 → 待修订 / 确认无误 → 待发布 → 已发布」推进；有证据时可先临时隔离，隔离只影响应用层作答入口，题库保持只读。'),
    revisionLine,
    el('div', { class: 'pg-admin-filters' },
      statusSel, subjectSel, typeSel, riskSel),
    el('div', { class: 'pg-actions', style: 'margin-bottom:12px' },
      button('导出已发布修订', () => window.open('/api/admin/content-issues/export?status=published', '_blank'),
        'secondary small', 'download')),
    listMount);
  load(0);
  return card;
}

const HEALTH_LABELS = {
  app_db: '应用数据库',
  bank: '只读题库',
  storage: '文件与对象存储',
  redis: '任务队列 Redis',
  worker: 'AI Worker',
};

function readinessCard(healthRes) {
  if (healthRes.error) {
    return el('section', { class: 'card', style: 'margin-top:22px' },
      el('div', { class: 'card-header' }, el('h2', {}, '服务就绪状态'), badge('不可用', 'red')),
      notice(`就绪检查读取失败：${healthRes.error}`, 'warning'));
  }
  const checks = Object.entries(healthRes.checks || {});
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, '服务就绪状态'),
      badge(healthRes.status === 'ok' ? '就绪' : '降级', healthRes.status === 'ok' ? 'teal' : 'amber')),
    el('p', { class: 'help', style: 'margin-bottom:12px' },
      `生成时间 ${dateText(healthRes.generated_at)} · 题库版本 ${healthRes.checks?.bank?.bank_version || '未知'}`
      + ` · 数据库 ${healthRes.checks?.app_db?.engine || '未知'}`),
    healthRes.status === 'ok'
      ? notice('所有已配置依赖正常。')
      : notice(`当前降级：受影响功能包括 ${(healthRes.affected_features || []).join('、') || '相关功能'}。`
        + '请按下方依赖明细恢复后重新检查。', 'warning'),
    el('div', { class: 'mini-list' }, ...checks.map(([name, check]) => el('div', { class: 'mini-item' },
      el('div', { class: 'mini-item-content' },
        el('div', { class: 'row' },
          el('strong', {}, HEALTH_LABELS[name] || name),
          badge(check.configured === false ? '未配置' : check.ok ? '正常' : '异常',
            check.configured === false ? '' : check.ok ? 'teal' : 'red')),
        el('p', {}, check.detail || ''))))));
}

// ---------------------------------------------------------------------------
// 分区卡片：每个卡片自带局部刷新方法，操作后只重读本卡片依赖的接口。
// ---------------------------------------------------------------------------

function knowledgeCard(catalog) {
  let kps = state.knowledgePoints || [];
  let refreshSeq = 0;
  const kpSubjSel = select((catalog.subjects || []).map(s => ({ value: s, label: s })), catalog.subjects?.[0] || '数学');
  const kpTreeMount = el('div', { style: 'max-height:320px;overflow:auto;margin-top:12px' });
  const renderTree = () => kpTreeMount.replaceChildren(renderKpTree(kps, { subject: kpSubjSel.value }));
  kpSubjSel.addEventListener('change', renderTree);
  renderTree();

  const newKpName = input('新增知识点名称（例如：隐零点代换）');
  const newKpLvl = select([{ value: '1', label: '1 级（章）' }, { value: '2', label: '2 级（节）' }, { value: '3', label: '3 级（知识点）' }], '3');
  const importInput = el('input', { type: 'file', accept: '.json,.csv', style: 'display:none' });

  async function refreshTree() {
    const mine = ++refreshSeq;
    try {
      const items = await ensureKnowledgePoints(true);
      if (mine !== refreshSeq) return;
      kps = items;
      renderTree();
    } catch (error) {
      if (mine === refreshSeq) toast(`知识点刷新失败：${error.message}`, true);
    }
  }

  importInput.addEventListener('change', async () => {
    const f = importInput.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.set('file', f);
    try {
      const res = await api('/admin/knowledge-points/import', { method: 'POST', body: fd });
      toast(`已成功导入 ${res.imported} 个新知识点节点。`);
      await refreshTree();
    } catch (err) { toast(err.message, true); }
    finally { importInput.value = ''; }
  });

  return el('section', { class: 'card' },
    el('div', { class: 'card-header' }, el('h2', {}, '知识点体系管理（章 → 节 → 知识点）'), badge('已内置九科课标树', 'teal')),
    el('div', { class: 'row between' },
      el('div', { class: 'row' }, kpSubjSel,
        button('导入 CSV / JSON 目录', () => importInput.click(), 'secondary small', 'upload'), importInput),
      button('管理官方知识点清单', () => go('notes'), 'ghost small', 'notes')),
    el('div', { class: 'row', style: 'margin-top:12px' },
      el('div', { style: 'flex:1' }, newKpName),
      el('div', { style: 'width:150px' }, newKpLvl),
      button('添加节点', e => busy(e.currentTarget, async () => {
        if (!newKpName.value.trim()) return;
        await api('/admin/knowledge-points', {
          method: 'POST', data: { subject: kpSubjSel.value, name: newKpName.value.trim(), level: Number(newKpLvl.value) },
        });
        toast('已添加知识点节点。');
        newKpName.value = '';
        await refreshTree();
      }), 'soft small', 'plus')),
    kpTreeMount);
}

function taggingCard(catalog, onJobComplete) {
  const tagSubj = select((catalog.subjects || []).map(s => ({ value: s, label: s })), '数学');
  const tagLimit = input('每次处理题数（例如 50）', { type: 'number', value: 50, min: 5, max: 500 });
  const tagPicker = createModelPicker('tagging', { showModeToggle: false });
  const tagEstMount = el('div');
  const tagJobMount = el('div');
  return el('section', { class: 'card' },
    el('div', { class: 'card-header' }, el('h2', {}, 'AI 批量知识点与难度预估打标'), badge('可中断 · 可续跑', 'blue')),
    notice('模型只能从系统知识点树中选择 kp_id，绝不自由发明。置信度 ≥ 0.8 自动标「AI 标注」，低于 0.8 进入下方人工复核队列。'),
    el('div', { class: 'form-grid' }, field('目标学科', tagSubj), field('本次处理上限（题）', tagLimit)),
    el('div', { style: 'margin-top:12px' }, tagPicker.node),
    el('div', { class: 'row end', style: 'margin-top:14px' },
      button('预估题量与费用', e => busy(e.currentTarget, async () => {
        const est = await api('/admin/tagging/estimate', {
          method: 'POST', data: { subject: tagSubj.value, limit: Number(tagLimit.value) || 50 },
        });
        tagEstMount.replaceChildren(notice(est.notice));
      }), 'secondary small', 'chart'),
      button('确认启动批量打标', e => busy(e.currentTarget, async () => {
        const est = await api('/admin/tagging/estimate', {
          method: 'POST', data: { subject: tagSubj.value, limit: Number(tagLimit.value) || 50 },
        });
        if (!est.will_process) { toast('该学科暂无未标注题目。'); return; }
        if (!window.confirm(`${est.notice}\n确定调用你的付费模型开始批量打标吗？`)) return;
        const payload = tagPicker.payload(false);
        const job = await api('/ai/jobs', {
          method: 'POST',
          data: { ...payload, task: 'tagging', subject: tagSubj.value, tagging_limit: Number(tagLimit.value) || 50 },
        });
        mountJob(tagJobMount, job.id, () => { onJobComplete?.(); });
      }), '', 'sparkle')),
    tagEstMount, tagJobMount);
}

function reviewQueueCard(initialItems) {
  let seq = 0;
  const mount = el('div');
  const card = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, 'AI 知识点标注复核队列'), badge(`${initialItems?.length || 0} 条待审`, 'amber')));
  const badgeNode = card.querySelector('.card-header .badge');

  function render(items) {
    mount.replaceChildren();
    if (!items.length) {
      mount.append(el('p', { class: 'help' }, '暂无待复核的 AI 知识点标注。运行上方批量打标后，结果会在此展示供审核。'));
      if (badgeNode) { badgeNode.textContent = '0 条待审'; badgeNode.className = 'badge amber'; }
      return;
    }
    if (badgeNode) { badgeNode.textContent = `${items.length} 条待审`; badgeNode.className = 'badge amber'; }
    const list = el('div', { class: 'mini-list' });
    for (const r of items) {
      list.append(el('div', { class: 'mini-item' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, `[${r.subject}] ${r.kp_name}（置信度 ${Math.round((r.confidence || 0) * 100)}%）`),
          el('p', {}, r.stem_plain)),
        el('div', { class: 'row' },
          button('通过', e => busy(e.currentTarget, async () => {
            await api('/admin/question-kp/review', { method: 'POST', data: { qid: r.qid, kp_id: r.kp_id, action: 'approve' } });
            toast('已审核通过。');
            await refresh();
          }), 'soft small', 'check'),
          button('驳回', e => busy(e.currentTarget, async () => {
            await api('/admin/question-kp/review', { method: 'POST', data: { qid: r.qid, kp_id: r.kp_id, action: 'reject' } });
            toast('已移除该 AI 标注。');
            await refresh();
          }), 'danger small', 'trash'))));
    }
    mount.append(list);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/admin/kp-review-queue?limit=30', { latest: true, latestKey: 'admin-kp-review-queue' });
      if (mine !== seq) return;
      render(data.items || []);
    } catch (error) {
      if (mine === seq && !error.aborted) mount.replaceChildren(notice(`复核队列刷新失败：${error.message}`, 'warning'));
    }
  }

  render(initialItems || []);
  card.append(mount);
  return { card, refresh };
}

function hostsCard(initial) {
  let seq = 0;
  const hostInput = input('例如：api.my-gateway.example.com（仅域名，不含 https://）');
  const customMount = el('div');
  const helpLine = el('p', { class: 'help', style: 'margin-bottom:12px' });
  let presets = initial?.presets || [];

  function render(data) {
    presets = data.presets?.length ? data.presets : presets;
    helpLine.textContent = data.error
      ? `白名单读取失败：${data.error}`
      : `内置已支持：${presets.join('、')}。此处添加的自定义域名存入数据库，无需改环境变量，依然强制 HTTPS 且拒绝本地/私网解析。`;
    customMount.replaceChildren();
    const custom = data.custom || [];
    if (!custom.length) {
      customMount.append(el('p', { class: 'help', style: 'margin-top:10px' },
        data.error ? '可稍后重试刷新。' : '尚未添加自定义域名。'));
      return;
    }
    const customList = el('div', { class: 'row', style: 'margin-top:12px' });
    for (const h of custom) {
      customList.append(el('div', { class: 'tpl-chip' },
        el('span', { style: 'padding:4px 8px;font-size:.8125rem' }, h.hostname),
        el('button', {
          type: 'button', class: 'tpl-del-btn', title: '移除域名',
          onClick: async () => {
            await api(`/admin/allowed-hosts/${encodeURIComponent(h.hostname)}`, { method: 'DELETE' });
            toast(`已移除白名单域名 ${h.hostname}`);
            await refresh();
          },
        }, '×')));
    }
    customMount.append(customList);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/admin/allowed-hosts', { latest: true, latestKey: 'admin-allowed-hosts' });
      if (mine !== seq) return;
      render(data);
    } catch (error) {
      if (mine === seq && !error.aborted) render({ error: error.message, presets: [], custom: [] });
    }
  }

  render(initial || { presets: [], custom: [] });
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, '自定义模型 API 域名白名单'), badge('HTTPS + 拦截私网 IP', 'teal')),
    helpLine,
    el('div', { class: 'row' },
      el('div', { style: 'flex:1' }, hostInput),
      button('添加域名', e => busy(e.currentTarget, async () => {
        if (!hostInput.value.trim()) return;
        await api('/admin/allowed-hosts', { method: 'POST', data: { hostname: hostInput.value.trim() } });
        toast('已添加到自定义域名白名单。');
        hostInput.value = '';
        await refresh();
      }), 'secondary small', 'plus')),
    customMount);
}

function registrationCard(initial) {
  let seq = 0;
  const card = el('section', { class: 'card', style: 'margin-top:22px' });
  const mount = el('div');

  function render(data) {
    card.replaceChildren(
      el('div', { class: 'card-header' },
        el('h2', {}, '账号与注册策略'),
        badge(data.public_mode ? '公网模式' : '本地模式', data.public_mode ? 'amber' : 'teal')),
      mount);
    if (data.error) {
      mount.replaceChildren(notice(`注册策略读取失败：${data.error}`, 'warning'));
      return;
    }
    mount.replaceChildren(
      el('p', { class: 'help', style: 'margin-bottom:12px' },
        `当前注册：${data.mode === 'open' ? '开放' : '关闭'}`
        + `${data.env_override ? '（由环境变量 STUDY_REGISTRATION_MODE 覆盖）' : ''}；`
        + `管理员账号：${data.admin_ready ? '已就绪' : '尚未初始化'}；`
        + `初始化口令：${data.admin_init_configured ? '已配置' : '未配置'}`),
      el('div', { class: 'row end' },
        button('开放注册', e => busy(e.currentTarget, async () => {
          await api('/auth/admin/registration', { method: 'POST', data: { mode: 'open' } });
          toast('注册已开放，新账号默认为学生。');
          await refresh();
        }), 'soft small', 'check', { disabled: data.mode === 'open' }),
        button('关闭注册', e => busy(e.currentTarget, async () => {
          await api('/auth/admin/registration', { method: 'POST', data: { mode: 'closed' } });
          toast('注册已关闭，已有账号不受影响。');
          await refresh();
        }), 'secondary small', 'lock', { disabled: data.mode === 'closed' })),
      !data.admin_ready
        ? notice('尚无可用的管理员账号：公网部署请先配置 STUDY_ADMIN_INIT_TOKEN，并通过 POST /api/auth/init-admin 完成一次性初始化。', 'warning')
        : null);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/auth/admin/registration', { latest: true, latestKey: 'admin-registration' });
      if (mine !== seq) return;
      render(data);
    } catch (error) {
      if (mine === seq && !error.aborted) render({ error: error.message });
    }
  }

  render(initial || { error: '注册策略尚未加载' });
  return card;
}

// ---------------------------------------------------------------------------
// 内容管理主页面：三个分区按 Tab 懒加载，切换后缓存；操作只做卡片级局部刷新。
// ---------------------------------------------------------------------------
export async function renderAdmin(container, version) {
  const catalogPromise = ensureCatalog();
  const panelEls = {
    issues: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-issues', 'aria-labelledby': 'admin-tab-issues' }),
    content: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-content', 'aria-labelledby': 'admin-tab-content' }),
    ops: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-ops', 'aria-labelledby': 'admin-tab-ops' }),
  };
  // 每个分区只加载一次并缓存 DOM；序号用于丢弃切换期间的旧响应。
  const panelState = {
    issues: { loaded: false, seq: 0 },
    content: { loaded: false, seq: 0 },
    ops: { loaded: false, seq: 0 },
  };

  function panelFailure(key, error) {
    const panel = panelEls[key];
    panelState[key].loaded = false;
    panel.replaceChildren(
      notice(`分区加载失败：${error.message}`, 'warning'),
      el('div', { class: 'row', style: 'margin-top:12px' },
        button('重新加载分区', () => loadPanel(key), 'secondary small', 'repeat')));
  }

  async function loadIssues() {
    const panel = panelEls.issues;
    const state = panelState.issues;
    const mine = ++state.seq;
    panel.replaceChildren(loading('正在加载校勘队列…'));
    const catalog = await catalogPromise;
    if (!current(version) || mine !== state.seq) return;
    panel.replaceChildren(contentIssuesCard(catalog));
    state.loaded = true;
  }

  async function loadContent() {
    const panel = panelEls.content;
    const state = panelState.content;
    const mine = ++state.seq;
    panel.replaceChildren(loading('正在加载题库与知识点…'));
    const [overview, catalog, kps, reviewQueue, templatesRes, communityRes, gridSpec] = await Promise.all([
      api('/admin/overview'),
      catalogPromise,
      ensureKnowledgePoints(true),
      api('/admin/kp-review-queue?limit=30').catch(() => ({ items: [] })),
      api('/admin/prompt-templates').catch(err => ({ error: err.message })),
      api('/admin/community-explanations').catch(err => ({ error: err.message })),
      ensurePromptGrid().catch(() => ({})),
    ]);
    if (!current(version) || mine !== state.seq) return;
    const counts = overview.counts || overview;
    const metrics = el('div', { class: 'stats-grid' },
      statCard('源题库', number(catalog.source_total || catalog.total), '道', '原题库保持只读', 'book'),
      statCard('开放练习', number(catalog.available_total || catalog.total), '道', '排除内容异常题', 'check'),
      statCard('知识点节点', number(kps.length), '个', '覆盖九大学科课标树', 'tag'),
      statCard('注册用户', number(counts.users || counts.user_count), '位', '账号数据独立隔离', 'chart'));
    const review = reviewQueueCard(reviewQueue.items || []);
    const tagging = taggingCard(catalog, () => { review.refresh(); });
    const templateCard = sharedReviewCard('共享提示词模板审核', '/admin/prompt-templates', templatesRes, (tpl, refresh) =>
      el('div', { class: 'mini-item' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, tpl.name),
          el('p', {}, describeTemplateConfig(tpl.config, gridSpec.dimensions || {})),
          tpl.custom_requirements ? el('p', {}, tpl.custom_requirements) : null),
        button(tpl.is_recommended ? '取消推荐' : '设为推荐', e => busy(e.currentTarget, async () => {
          await api(`/prompt-templates/${tpl.id}/recommend`, {
            method: 'POST', data: { recommended: !tpl.is_recommended },
          });
          await refresh(0);
        }), tpl.is_recommended ? 'ghost small' : 'soft small', 'check')),
      '暂无公开分享的模板。');

    const communityCard = (communityRes.enabled || communityRes.error) ? sharedReviewCard(
      '社区讲解审核', '/admin/community-explanations', communityRes, (exp, refresh) =>
        el('div', { class: 'mini-item' },
          el('div', { class: 'mini-item-content' },
            el('details', { class: 'sources' },
              el('summary', {}, `${exp.qid} · ${dateText(exp.created_at)} · ${exp.models.map(m => m.model).join(' + ')}`),
              markdown(exp.content))),
          button(exp.featured ? '取消精选' : '设为平台精选', e => busy(e.currentTarget, async () => {
            await api(`/admin/community-explanations/${exp.id}/feature`, { method: 'PUT', data: { featured: !exp.featured } });
            await refresh(0);
          }), exp.featured ? 'ghost small' : 'soft small', 'check')),
      '暂无社区讲解。') : null;

    panel.replaceChildren(
      metrics,
      el('div', { class: 'grid-two' }, knowledgeCard(catalog), tagging),
      review.card,
      templateCard,
      ...(communityCard ? [communityCard] : []));
    state.loaded = true;
  }

  async function loadOps() {
    const panel = panelEls.ops;
    const state = panelState.ops;
    const mine = ++state.seq;
    panel.replaceChildren(loading('正在加载运行与账号…'));
    const [hostsRes, evalRunsRes, registrationRes, healthRes] = await Promise.all([
      api('/admin/allowed-hosts').catch(error => ({ error: error.message, presets: [], custom: [] })),
      api('/admin/eval-runs').catch(() => ({ items: [] })),
      api('/auth/admin/registration').catch(error => ({ error: error.message })),
      api('/health/ready').catch(err => ({ error: err.message })),
    ]);
    if (!current(version) || mine !== state.seq) return;

    const backupOut = el('div');
    const opsCard = el('section', { class: 'card', style: 'margin-top:22px' },
      el('div', { class: 'card-header' },
        el('h2', {}, '数据在线热备份与评测记录'),
        button('立即热备份并校验', e => busy(e.currentTarget, async () => {
          const res = await api('/admin/backup', { method: 'POST' });
          toast('在线热备份与 SHA-256 / 完整性校验已完成。');
          backupOut.replaceChildren(notice(`备份成功：${res.path}（${res.file_count} 个文件，${Math.round(res.size_bytes / 1024)} KB，完整性检查：${res.verified?.integrity_check}）`));
        }), 'secondary small', 'download')),
      el('p', { class: 'help', style: 'margin-bottom:12px' },
        '命令行还支持 `python -m app.ops backup / verify / restore` 恢复演练，以及 `python -m app.eval` 100 题黄金评测与提示词 A/B 对比。'),
      backupOut);

    if (evalRunsRes.items?.length) {
      opsCard.append(el('div', { class: 'table-wrap', style: 'margin-top:12px' }, el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, '评测名称'), el('th', {}, '主模型 / 核验模型'), el('th', {}, '题数'),
          el('th', {}, '单模型准确率'), el('th', {}, '一致率'), el('th', {}, '一致但错'))),
        el('tbody', {}, ...evalRunsRes.items.map(r => el('tr', {},
          el('td', {}, r.name),
          el('td', {}, r.verify_model ? `${r.model} + ${r.verify_model}` : r.model),
          el('td', {}, String(r.total)),
          el('td', {}, `${r.metrics?.single_accuracy ?? '—'}%`),
          el('td', {}, r.metrics?.agreement_rate !== null && r.metrics?.agreement_rate !== undefined ? `${r.metrics.agreement_rate}%` : '—'),
          el('td', {}, r.metrics?.agreed_but_wrong_rate !== null && r.metrics?.agreed_but_wrong_rate !== undefined ? `${r.metrics.agreed_but_wrong_rate}%` : '—')))))));
    }

    panel.replaceChildren(
      readinessCard(healthRes),
      opsCard,
      registrationCard(registrationRes),
      hostsCard(hostsRes));
    state.loaded = true;
  }

  const loaders = { issues: loadIssues, content: loadContent, ops: loadOps };

  function loadPanel(key) {
    const state = panelState[key];
    if (state.loaded) return;
    state.loaded = true; // 立即置位：切换/重入不重复发起请求
    loaders[key]().catch(error => {
      if (current(version)) panelFailure(key, error);
      else panelState[key].loaded = false;
    });
  }

  const sectionDefs = [
    ['issues', '校勘', 'shield'],
    ['content', '题库与知识点', 'book'],
    ['ops', '运行与账号', 'sliders'],
  ];
  const tabBar = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': '内容管理分区' });
  const tabButtons = new Map();
  for (const [key, label, iconName] of sectionDefs) {
    const tab = el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      id: `admin-tab-${key}`,
      'aria-controls': `admin-panel-${key}`,
      'aria-selected': 'false',
      onClick: () => selectTab(key),
    }, icon(iconName), label);
    tabButtons.set(key, tab);
    tabBar.append(tab);
  }
  wireTabKeyboard(tabBar);

  function selectTab(key) {
    adminTab = panelEls[key] ? key : 'issues';
    for (const [name, panel] of Object.entries(panelEls)) panel.hidden = name !== adminTab;
    for (const [name, tab] of tabButtons) tab.setAttribute('aria-selected', String(name === adminTab));
    loadPanel(adminTab);
  }

  container.replaceChildren(
    heading('CONTENT MANAGEMENT', '认真维护每一份学习内容。',
      '校勘队列先看题号、状态与证据；题库与知识点、依赖健康与备份分到各自分区。'),
    tabBar,
    panelEls.issues, panelEls.content, panelEls.ops);
  selectTab(adminTab);
}
