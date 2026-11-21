/* 多智能体课堂（P1）：角色名册 + 多角色讨论 / 自由问答 / 讲授 + 学生插话。
 *
 * 接口依据 `app/classroom/MULTIAGENT-API.md`（classroom-multiagent-2026-11-21-v1）：
 * - GET  /api/classroom/courses/{id}/agents            角色名册（含 has_roster / limits / roles）
 * - POST /api/classroom/courses/{id}/agents/generate   生成名册（同步，无 provider → 422）
 * - PATCH/api/classroom/courses/{id}/agents            编辑名册（teacher/classmates 局部改或 clear）
 * - POST /api/classroom/sessions/{id}/discussion       1-6 段多角色脚本（同步）
 * - POST /api/classroom/sessions/{id}/interject        学生插话后的 1-2 段回应（同步）
 *
 * 本组件只负责「名册 + 发起请求 + 播放队列控制」；气泡渲染、TTS、动作执行（含 unit 坐标白板）
 * 由播放器通过回调注入，保证与自由问答（tutor）共享同一份历史与舞台效果。
 */
import { api } from '../core/api.js';
import { el, button, badge, notice, loading, toast, select, field, input } from '../core/dom.js';

const REQUEST_TIMEOUT_MS = 120000;
const ROLE_FALLBACK_COLORS = {
  teacher: '#2f6f5e',
  assistant: '#6d63b8',
  student: '#c47f2c',
};
const ROLE_LABELS = { teacher: '老师', assistant: '助教', student: '同学' };

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function withTimeout(promise, ms, message) {
  let timer = null;
  const timeout = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

function initialOf(name) {
  const text = String(name || '').trim();
  return text ? text.slice(0, 1) : '?';
}

function normalizeColor(value, fallback) {
  const text = String(value || '').trim();
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(text) ? text : fallback;
}

function agentRoleLabel(role) {
  return ROLE_LABELS[String(role || '')] || '课堂角色';
}

export function createMultiagentPanel({
  courseId,
  getSessionId,
  getScene,
  getModelPayload,
  appendHistory,
  speak,
  executeActions,
  onSceneHint,
  onOpenDiscussion,
  pausePlayback,
}) {
  let payload = null;
  let loadingRoster = false;
  let generating = false;
  let busyRequest = false;
  let destroyed = false;
  let activeBubble = null;
  const rosterList = el('div', { class: 'cls-ma-roster-list' });
  const rosterStatus = el('p', { class: 'help cls-ma-roster-status' });
  const sideAvatars = el('div', { class: 'cls-ma-side-avatars' });
  const sideStatus = el('p', { class: 'help cls-ma-side-status' });
  const statusNode = el('p', { class: 'help cls-ma-status', role: 'status', 'aria-live': 'polite' });
  const hintChip = el('div', { class: 'cls-ma-hint-chip', hidden: true });

  // -------------------------------------------------------------------------
  // 播放队列（逐条气泡 + TTS + 动作；暂停/跳过/重播/停止）
  // -------------------------------------------------------------------------
  const queue = {
    turns: [],
    index: 0,
    running: false,
    paused: false,
    stopped: false,
    replay: false,
    handle: null,
    token: 0,
    label: '讨论',
  };

  function setStatus(text, tone = '') {
    statusNode.textContent = text || '';
    statusNode.hidden = !text;
    statusNode.classList.toggle('is-error', tone === 'error');
    statusNode.classList.toggle('is-active', tone === 'active');
    if (typeof setStatusHook === 'function') setStatusHook(text, tone);
  }

  let setStatusHook = null;

  function setActiveBubble(bubble) {
    if (activeBubble && activeBubble !== bubble) activeBubble.classList.remove('is-speaking');
    activeBubble = bubble || null;
    if (activeBubble) activeBubble.classList.add('is-speaking');
  }

  function updateQueueControls() {
    const running = queue.running;
    playControls.hidden = !running;
    pauseBtn.textContent = queue.paused ? '继续' : '暂停';
    pauseBtn.setAttribute('aria-pressed', queue.paused ? 'true' : 'false');
    if (running) {
      setStatus(`${queue.label}：第 ${queue.index + 1}/${queue.turns.length} 段${queue.paused ? '（已暂停）' : ''}`, 'active');
    }
  }

  async function playTurn(turn, bubble, { repeat = false } = {}) {
    const token = queue.token;
    if (!repeat) setActiveBubble(bubble);
    statusNode.hidden = false;
    const handle = speak(turn.text || '', { role: turn.role || 'student', speaker: turn.speaker_name || '' });
    queue.handle = handle;
    try {
      await handle.promise;
    } finally {
      if (queue.handle === handle) queue.handle = null;
    }
    if (token !== queue.token || queue.stopped) return;
    executeActions(turn.actions || []);
    if (Array.isArray(turn.actions) && turn.actions.length) await sleep(320);
  }

  async function runQueue(turns, label) {
    if (!Array.isArray(turns) || !turns.length) return;
    stopQueue();
    queue.token += 1;
    const token = queue.token;
    queue.turns = turns;
    queue.index = 0;
    queue.running = true;
    queue.paused = false;
    queue.stopped = false;
    queue.replay = false;
    queue.label = label;
    hintChip.hidden = true;
    hintChip.replaceChildren();
    pausePlayback?.();
    updateQueueControls();
    for (let index = 0; index < turns.length; index += 1) {
      if (queue.stopped || token !== queue.token) break;
      queue.index = index;
      const turn = turns[index];
      // 重播本条时复用同一条气泡，避免历史里出现重复发言。
      let bubble = turn.__bubble;
      if (!bubble) {
        bubble = appendHistory({
          role: turn.role || 'student',
          speaker: turn.speaker_name || agentRoleLabel(turn.role),
          color: normalizeColor(turn.color, colorOf(turn.speaker_name, turn.role)),
          text: turn.text || '',
          actions: Array.isArray(turn.actions) ? turn.actions : [],
          source: 'discussion',
          speaker_role: turn.role || 'student',
        });
        turn.__bubble = bubble;
      }
      updateQueueControls();
      await playTurn(turn, bubble);
      if (queue.stopped || token !== queue.token) break;
      if (queue.replay) {
        queue.replay = false;
        index -= 1;
        continue;
      }
      if (queue.paused) {
        await waitForResume(token);
        if (queue.stopped || token !== queue.token) break;
      }
    }
    if (token === queue.token) {
      queue.running = false;
      setActiveBubble(null);
      playControls.hidden = true;
      setStatus(`${queue.label}已结束（共 ${turns.length} 段）。`, 'active');
    }
  }

  function waitForResume(token) {
    return new Promise(resolve => {
      const timer = setInterval(() => {
        if (queue.stopped || token !== queue.token || !queue.paused) {
          clearInterval(timer);
          resolve();
        }
      }, 150);
    });
  }

  function stopQueue() {
    queue.stopped = true;
    queue.running = false;
    queue.token += 1;
    try { queue.handle?.cancel?.(); } catch { /* 忽略 */ }
    queue.handle = null;
    setActiveBubble(null);
    playControls.hidden = true;
    if (!destroyed) setStatus('');
  }

  function togglePause() {
    if (!queue.running) return;
    queue.paused = !queue.paused;
    try {
      if (queue.paused) queue.handle?.pause?.();
      else queue.handle?.resume?.();
    } catch { /* 忽略 */ }
    updateQueueControls();
  }

  function skipTurn() {
    if (!queue.running) return;
    try { queue.handle?.cancel?.(); } catch { /* 忽略 */ }
  }

  function replayTurn() {
    if (!queue.running) return;
    queue.replay = true;
    try { queue.handle?.cancel?.(); } catch { /* 忽略 */ }
  }

  // -------------------------------------------------------------------------
  // 名册
  // -------------------------------------------------------------------------
  function agentsList() {
    return Array.isArray(payload?.agents) ? payload.agents : [];
  }

  function teacherAgent() {
    return payload?.roster?.teacher || agentsList().find(agent => agent.role === 'teacher') || null;
  }

  function colorOf(speaker, role) {
    const name = String(speaker || '').trim();
    if (name) {
      const hit = agentsList().find(agent => String(agent.name || '') === name);
      if (hit?.color) return hit.color;
    }
    return normalizeColor(payload?.roster?.teacher?.color, ROLE_FALLBACK_COLORS[String(role || '')] || '#5b7480');
  }

  function renderRoster() {
    const agents = agentsList();
    const hasRoster = Boolean(payload?.has_roster);
    rosterList.replaceChildren();
    sideAvatars.replaceChildren();
    if (!hasRoster || !agents.length) {
      rosterStatus.textContent = '还没有课堂角色名册：生成后即可开始多智能体讨论。';
      rosterStatus.hidden = false;
      rosterList.append(el('p', { class: 'help' }, '名册需 1 名老师 + 1-5 名同学，由 AI 按课程内容设计。'));
      sideStatus.textContent = '未生成名册';
      generateBtn.hidden = generating;
      editBtn.hidden = true;
      clearBtn.hidden = true;
      return;
    }
    rosterStatus.hidden = true;
    generateBtn.hidden = false;
    generateBtn.textContent = '重新生成角色';
    editBtn.hidden = false;
    clearBtn.hidden = false;
    sideStatus.textContent = `${agents.length} 个课堂角色`;
    for (const agent of agents) {
      const color = normalizeColor(agent.color, ROLE_FALLBACK_COLORS[agent.role] || '#5b7480');
      rosterList.append(el('div', { class: 'cls-ma-agent', title: agent.persona || '' },
        el('span', { class: 'cls-ma-avatar', style: `background:${color}` }, initialOf(agent.name)),
        el('div', { class: 'cls-ma-agent-main' },
          el('strong', {}, agent.name || '未命名角色'),
          el('span', { class: 'help' }, agentRoleLabel(agent.role))),
        agent.role === 'teacher' ? badge('老师', 'teal') : null));
      sideAvatars.append(el('span', {
        class: 'cls-ma-avatar small',
        style: `background:${color}`,
        title: `${agent.name || ''}（${agentRoleLabel(agent.role)}）`,
      }, initialOf(agent.name)));
    }
  }

  async function loadRoster(force = false) {
    if (loadingRoster) return payload;
    if (payload && !force) return payload;
    loadingRoster = true;
    if (!payload) {
      rosterList.replaceChildren(loading('正在读取课堂角色…'));
      rosterStatus.hidden = true;
    }
    try {
      payload = await api(`/classroom/courses/${courseId}/agents`);
    } catch (error) {
      rosterStatus.hidden = false;
      rosterStatus.classList.add('is-error');
      rosterStatus.textContent = error.message || '课堂角色名册读取失败';
      sideStatus.textContent = '名册读取失败';
      generateBtn.hidden = false;
      return null;
    } finally {
      loadingRoster = false;
    }
    if (destroyed) return payload;
    renderRoster();
    return payload;
  }

  async function generateRoster() {
    if (generating || busyRequest) return;
    generating = true;
    generateBtn.disabled = true;
    generateBtn.textContent = '正在生成角色…';
    setStatus('正在按课程内容生成角色名册（1 名老师 + 同学）…', 'active');
    try {
      const body = { classmate_count: 3 };
      const model = typeof getModelPayload === 'function' ? getModelPayload() : null;
      if (model?.provider_id && model?.model) {
        body.provider_id = model.provider_id;
        body.model = model.model;
      }
      payload = await withTimeout(
        api(`/classroom/courses/${courseId}/agents/generate`, { method: 'POST', data: body }),
        REQUEST_TIMEOUT_MS,
        '角色名册生成超时（120 秒），请稍后在课堂里重试。',
      );
      renderRoster();
      setStatus('角色名册已生成，可以开始多智能体讨论。', 'active');
      toast('课堂角色名册已生成');
    } catch (error) {
      const message = error.message || '角色名册生成失败';
      setStatus(message, 'error');
      if (message.includes('模型设置') || message.includes('AI 连接')) {
        rosterStatus.hidden = false;
        rosterStatus.classList.add('is-error');
        rosterStatus.replaceChildren(
          el('span', {}, message + ' '),
          el('a', { href: '#/settings', class: 'text-button' }, '前往模型设置'),
        );
      }
      toast(message, true);
    } finally {
      generating = false;
      generateBtn.disabled = false;
      generateBtn.textContent = payload?.has_roster ? '重新生成角色' : '生成课堂角色';
    }
  }

  function openEditDialog() {
    if (!payload?.has_roster) return;
    const teacher = teacherAgent();
    const classmates = agentsList().filter(agent => agent.role !== 'teacher');
    const rows = [];
    const buildRow = (agent) => {
      const nameInput = input('角色名', { maxlength: '24', value: agent?.name || '' });
      const colorInput = el('input', { type: 'color', class: 'cls-ma-color-input', value: normalizeColor(agent?.color, ROLE_FALLBACK_COLORS[agent?.role] || '#5b7480') });
      const personaInput = el('textarea', { class: 'cls-ma-persona-input', rows: 2, maxlength: '400', value: agent?.persona || '' });
      rows.push({ agent, nameInput, colorInput, personaInput });
      return el('div', { class: 'cls-ma-edit-row' },
        el('div', { class: 'cls-ma-edit-line' },
          el('strong', {}, agent?.role === 'teacher' ? '老师' : '同学'),
          nameInput,
          colorInput),
        personaInput);
    };
    const list = el('div', { class: 'cls-ma-edit-list' },
      teacher ? buildRow(teacher) : null,
      classmates.map(buildRow));
    const saveBtn = button('保存名册', null, '', 'check');
    const dialog = el('dialog', { class: 'cls-ma-edit-dialog' },
      el('div', { class: 'row between' },
        el('h3', {}, '编辑课堂角色'),
        button('关闭', () => dialog.close(), 'ghost small', 'close')),
      el('p', { class: 'help' }, '可修改角色名、人设与配色；保存后讨论气泡与头像立即使用新配色。'),
      list,
      el('div', { class: 'row end' }, saveBtn));
    saveBtn.addEventListener('click', async () => {
      const teacherRow = rows.find(row => row.agent?.role === 'teacher');
      const classmateRows = rows.filter(row => row.agent?.role !== 'teacher');
      const data = {};
      if (teacherRow) {
        data.teacher = {
          id: teacherRow.agent.id,
          name: teacherRow.nameInput.value.trim(),
          persona: teacherRow.personaInput.value.trim(),
          color: teacherRow.colorInput.value,
        };
      }
      data.classmates = classmateRows.map(row => ({
        id: row.agent.id,
        name: row.nameInput.value.trim(),
        persona: row.personaInput.value.trim(),
        color: row.colorInput.value,
      }));
      try {
        saveBtn.disabled = true;
        payload = await api(`/classroom/courses/${courseId}/agents`, { method: 'PATCH', data });
        renderRoster();
        dialog.close();
        toast('角色名册已保存');
      } catch (error) {
        toast(error.message || '保存名册失败', true);
      } finally {
        if (saveBtn.isConnected) saveBtn.disabled = false;
      }
    });
    document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.showModal();
  }

  async function clearRoster() {
    if (!payload?.has_roster) return;
    if (!window.confirm('清空课堂角色名册？清空后多智能体讨论将不可用（可重新生成）。')) return;
    try {
      payload = await api(`/classroom/courses/${courseId}/agents`, { method: 'PATCH', data: { clear: true } });
      renderRoster();
      toast('角色名册已清空');
    } catch (error) {
      toast(error.message || '清空名册失败', true);
    }
  }

  // -------------------------------------------------------------------------
  // 讨论 / 插话
  // -------------------------------------------------------------------------
  function discussionData() {
    const scene = getScene();
    const model = typeof getModelPayload === 'function' ? getModelPayload() : null;
    const data = {
      scene_id: scene ? scene.id : null,
      mode: modeSelect.value,
      rounds: Number(roundsSelect.value) || 1,
      topic: String(topicInput.value || '').trim() || null,
      user_message: String(userMessageInput.value || '').trim() || null,
    };
    if (model?.provider_id && model?.model) {
      data.provider_id = model.provider_id;
      data.model = model.model;
    }
    return data;
  }

  async function startDiscussion() {
    if (busyRequest) return;
    const sessionId = getSessionId();
    if (!sessionId) { toast('学习会话还没有就绪，请稍后重试。', true); return; }
    if (!payload?.has_roster) {
      toast('请先生成课堂角色名册，再开始多智能体讨论。', true);
      await loadRoster();
      return;
    }
    const scene = getScene();
    if (!scene) { toast('还没有可讨论的场景。', true); return; }
    busyRequest = true;
    startBtn.disabled = true;
    startBtn.textContent = '角色准备中…';
    setStatus('正在组织多角色讨论（最长等待 120 秒）…', 'active');
    try {
      const data = discussionData();
      const response = await withTimeout(
        api(`/classroom/sessions/${sessionId}/discussion`, { method: 'POST', data }),
        REQUEST_TIMEOUT_MS,
        '多角色讨论生成超时（120 秒），请稍后重试。',
      );
      if (data.user_message) {
        appendHistory({ role: 'user', speaker: '学生', text: data.user_message, actions: [], source: 'discussion' });
      }
      topicInput.value = '';
      userMessageInput.value = '';
      const turns = Array.isArray(response.turns) ? response.turns : [];
      setStatus(`已生成 ${turns.length} 段发言，开始播放…`, 'active');
      await runQueue(turns, response.mode === 'interject' ? '插话回应' : '多角色讨论');
      showSceneHint(response.scene_hint);
      if (response.warnings?.length) setStatus(response.warnings.join('；'), 'error');
    } catch (error) {
      setStatus(error.message || '多角色讨论暂时不可用', 'error');
      toast(error.message || '多角色讨论暂时不可用', true);
    } finally {
      busyRequest = false;
      startBtn.disabled = false;
      startBtn.textContent = '开始讨论';
    }
  }

  async function sendInterject() {
    if (busyRequest) return;
    const message = String(interjectInput.value || '').trim();
    if (!message) { toast('请输入要插话的内容', true); return; }
    const sessionId = getSessionId();
    if (!sessionId) return;
    if (!payload?.has_roster) { toast('请先生成课堂角色名册，再插话。', true); return; }
    const scene = getScene();
    busyRequest = true;
    interjectBtn.disabled = true;
    setStatus('正在等待角色回应插话（最长等待 120 秒）…', 'active');
    try {
      const model = typeof getModelPayload === 'function' ? getModelPayload() : null;
      const data = { scene_id: scene ? scene.id : null, message };
      if (model?.provider_id && model?.model) {
        data.provider_id = model.provider_id;
        data.model = model.model;
      }
      interjectInput.value = '';
      appendHistory({ role: 'user', speaker: '学生', text: message, actions: [], source: 'discussion' });
      const response = await withTimeout(
        api(`/classroom/sessions/${sessionId}/interject`, { method: 'POST', data }),
        REQUEST_TIMEOUT_MS,
        '角色回应超时（120 秒），请稍后重试。',
      );
      const turns = Array.isArray(response.turns) ? response.turns : [];
      await runQueue(turns, '插话回应');
      showSceneHint(response.scene_hint);
    } catch (error) {
      setStatus(error.message || '课堂插话暂时不可用', 'error');
      toast(error.message || '课堂插话暂时不可用', true);
    } finally {
      busyRequest = false;
      interjectBtn.disabled = false;
    }
  }

  function showSceneHint(hint) {
    if (!hint || hint.index === undefined && !hint.scene_id) { hintChip.hidden = true; return; }
    const label = hint.reason || '讨论建议切换到相关场景继续。';
    hintChip.hidden = false;
    hintChip.replaceChildren(
      el('span', { class: 'help' }, label),
      button('前往该场景', () => {
        onSceneHint?.(hint);
        hintChip.hidden = true;
      }, 'secondary small', 'chevron'));
  }

  // -------------------------------------------------------------------------
  // DOM
  // -------------------------------------------------------------------------
  const generateBtn = button('生成课堂角色', () => generateRoster(), 'secondary small', 'sparkle');
  const editBtn = button('编辑名册', () => openEditDialog(), 'ghost small', 'edit');
  const clearBtn = button('清空名册', () => clearRoster(), 'ghost small', 'trash');
  generateBtn.hidden = true;
  editBtn.hidden = true;
  clearBtn.hidden = true;

  const rosterNode = el('div', { class: 'cls-ma-roster' },
    el('div', { class: 'cls-ma-roster-head' },
      el('strong', {}, '课堂角色'),
      el('div', { class: 'row' }, generateBtn, editBtn, clearBtn)),
    rosterStatus,
    rosterList);

  const modeSelect = select([
    { value: 'discussion', label: '多角色讨论' },
    { value: 'qa', label: '自由问答' },
    { value: 'lecture', label: '多角色讲授' },
  ], 'discussion', { class: 'cls-ma-mode', 'aria-label': '讨论模式' });
  const roundsSelect = select([
    { value: '1', label: '1 轮' },
    { value: '2', label: '2 轮' },
    { value: '3', label: '3 轮' },
  ], '1', { class: 'cls-ma-rounds', 'aria-label': '讨论轮次' });
  const topicInput = input('讨论主题（可选，例如：光合作用为什么离不开光？）', { maxlength: '200', class: 'cls-ma-topic' });
  const userMessageInput = input('以学生身份说一句（可选，会写入讨论开头）', { maxlength: '2000', class: 'cls-ma-user-message' });
  const interjectInput = input('随时插话，例如：老师我有个疑问…', { maxlength: '2000', class: 'cls-ma-interject-input' });
  const startBtn = button('开始讨论', () => startDiscussion(), '', 'message');
  const interjectBtn = button('发送插话', () => sendInterject(), 'secondary small', 'message');
  const pauseBtn = button('暂停', () => togglePause(), 'secondary small', 'stop');
  const skipBtn = button('跳过本条', () => skipTurn(), 'ghost small', 'chevron');
  const replayBtn = button('重播本条', () => replayTurn(), 'ghost small', 'repeat');
  const stopBtn = button('停止', () => stopQueue(), 'ghost small', 'stop');
  const playControls = el('div', { class: 'row cls-ma-play-controls', hidden: true },
    el('span', { class: 'help cls-ma-play-label' }, '讨论播放'),
    pauseBtn, skipBtn, replayBtn, stopBtn);

  const panelNode = el('div', { class: 'cls-ma-panel' },
    el('div', { class: 'cls-ma-compose' },
      el('div', { class: 'cls-ma-compose-row' },
        field('讨论模式', modeSelect),
        field('轮次', roundsSelect)),
      field('讨论主题（可选）', topicInput),
      field('你的开场发言（可选）', userMessageInput),
      el('div', { class: 'row' }, startBtn,
        el('span', { class: 'help' }, '角色按名册顺序发言，气泡逐条播放并朗读；可随时插话。'))),
    playControls,
    el('div', { class: 'cls-ma-interject' },
      el('strong', { class: 'cls-ma-subtitle' }, '学生插话'),
      el('div', { class: 'cls-ma-interject-row' }, interjectInput, interjectBtn)),
    statusNode,
    hintChip);

  const sideCardNode = el('section', { class: 'cls-ma-side-card' },
    el('div', { class: 'cls-ma-side-head' },
      el('strong', {}, '多智能体讨论'),
      badge('P1', 'blue')),
    sideStatus,
    sideAvatars,
    el('div', { class: 'row cls-ma-side-actions' },
      button('开始讨论', () => onOpenDiscussion?.(), '', 'message'),
      button('角色名册', () => onOpenDiscussion?.(), 'secondary small', 'users')));

  function ensureRosterAndOpen() {
    onOpenDiscussion?.();
    loadRoster().catch(() => {});
  }

  // 初始化：先静默读取名册（不阻塞播放器首屏）。
  loadRoster().catch(() => {});

  return {
    rosterNode,
    panelNode,
    sideCardNode,
    ensureRoster: () => loadRoster(),
    loadRoster,
    colorOf,
    roleLabel: agentRoleLabel,
    openDiscussion: ensureRosterAndOpen,
    stopDiscussion: stopQueue,
    isPlaying: () => queue.running,
    setStatusHook: hook => { setStatusHook = hook; },
    destroy() {
      destroyed = true;
      stopQueue();
      generateBtn.remove();
      editBtn.remove();
      clearBtn.remove();
    },
  };
}
