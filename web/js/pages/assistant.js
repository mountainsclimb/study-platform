import { api } from '../core/api.js';
import { state, readLocal, saveLocal, onSessionReset } from '../core/state.js';
import {
  el, icon, button, badge, empty, heading, loading, notice, busy, toast, dateText,
} from '../core/dom.js';
import { current, go } from '../core/router.js';
import { markdown } from '../core/markdown.js';
import { mountJob } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { openKpDetailDialog } from '../components/kpDetailModal.js';
import { createVoiceInput } from '../components/voiceInput.js';
import { t } from '../core/i18n.js';

// AI 助手（整合自 Oh My Paper 小纸）：会话 + 工具白名单 + 可停止的 Agent 循环。
// 页面只做展示与交互；真正的权限、工具执行、写操作确认都在服务端。

// 后端/AI/用户数据容器：界面语言切换不翻译（会话标题、AI 正文、技能名、工具输出等）。
function withNoI18n(node) {
  if (node && typeof node.setAttribute === 'function') node.setAttribute('data-no-i18n', '');
  return node;
}

const MAX_CONTENT = 4000;
const TOOL_STEPS_KEY = 'zhixu.assistant.toolSteps';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TOOL_LABELS = {
  get search_source_bank() { return t('assistant.searchQuestionBank'); },
  get read_question() { return t('assistant.readQuestion'); },
  get list_wrongs() { return t('assistant.viewWrongAnswers'); },
  get get_stats() { return t('assistant.readLearningAnalytics'); },
  get list_notes() { return t('assistant.findNotes'); },
  get read_note() { return t('assistant.readNote'); },
  get list_knowledge_points() { return t('assistant.viewKnowledgePoint'); },
  get list_mybank_questions() { return t('assistant.searchMyQuestionBank'); },
  get create_explain_job() { return t('assistant.createExplanationTask'); },
  get create_revision_paper() { return t('assistant.generatePracticePaper'); },
  get compose_paper() { return t('paperlab.compose'); },
};

const STATUS_LABELS = {
  get idle() { return t('home.conversationStatus.idle'); },
  get queued() { return t('home.conversationStatus.queued'); },
  get running() { return t('home.conversationStatus.running'); },
  get failed() { return t('home.conversationStatus.failed'); },
  get done() { return t('focus.completed'); },
};

const ACTIVE_STATUSES = new Set(['queued', 'running']);

let toolsInfoPromise = null;

function ensureToolsInfo() {
  if (!toolsInfoPromise) {
    toolsInfoPromise = api('/assistant/tools').catch(() => ({
      items: [],
      write_tools: [],
      limits: {},
      scope_note: '',
    }));
  }
  return toolsInfoPromise;
}

function trimText(value, limit) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (!limit || text.length <= limit) return text;
  return `${text.slice(0, limit)}…`;
}

function messageText(message) {
  if (!message) return '';
  return (message.parts || [])
    .filter(part => part && part.type === 'text' && part.text)
    .map(part => part.text)
    .join('\n\n')
    .trim();
}

function optionsCount(model) {
  const list = messageText(model).length;
  return `${list} / ${MAX_CONTENT}`;
}

function jsonPreview(value, limit = 3000) {
  try {
    const text = JSON.stringify(value ?? {}, null, 2);
    return text.length > limit ? `${text.slice(0, limit)}\n…` : text;
  } catch {
    return String(value ?? '');
  }
}

function qidLabel(qid) {
  const text = String(qid || '');
  return text.length > 22 ? `${text.slice(0, 22)}…` : text;
}

// 全部清掉模块级缓存：换账号后不能沿用上一个账号的会话视图。
onSessionReset(() => {
  toolsInfoPromise = null;
});

export async function renderAssistant(container, version) {
  const [info, conversationsRes, skillsRes] = await Promise.all([
    ensureToolsInfo(),
    api('/assistant/conversations'),
    api('/assistant/skills').catch(() => ({ items: [], limits: {}, builtin_count: 0 })),
  ]);
  if (!current(version)) return;

  const toolStepsInitial = readLocal(TOOL_STEPS_KEY, true) !== false;
  let toolStepsVisible = toolStepsInitial;
  let conversations = conversationsRes.items || [];
  let activeId = null;
  let detail = null;
  let running = false;
  let loadingOlder = false;
  let renderedActionIds = new Set();
  let skillLibrary = Array.isArray(skillsRes.items) ? skillsRes.items : [];
  let skillLimits = skillsRes.limits || {};
  let selectedSkillIds = new Set();

  // -------------------------------------------------------------------------
  // DOM 骨架
  // -------------------------------------------------------------------------
  const page = el('div', { class: 'asst-page' });
  page.append(
    heading(
      'AI ASSISTANT',
      t('insight.activityMix.assistant'),
      t('assistant.searchTheBankReviewWrongAnswersAnd')
    )
  );

  const layout = el('div', { class: 'asst-layout' });

  // 会话侧栏
  const sidebar = el('aside', { class: 'asst-side', 'aria-label': t('assistant.conversations') });
  const newConversationBtn = button(t('assistant.newConversation'), () => busy(newConversationBtn, createNewConversation), 'soft', 'plus');
  newConversationBtn.classList.add('asst-new-btn');
  const conversationList = el('div', { class: 'asst-conv-list', role: 'list' });
  const sideEmpty = el('p', { class: 'asst-side-hint' },
    t('assistant.conversationsAreYoursAloneTheAssistantCan'));
  sidebar.append(
    el('div', { class: 'asst-side-head' }, el('span', { class: 'asst-side-title' }, t('assistant.myConversations')), newConversationBtn),
    conversationList,
    sideEmpty
  );

  // 聊天区
  const chat = el('section', { class: 'asst-chat', 'aria-label': t('assistant.aiAssistantChat') });
  const titleEl = el('h2', { class: 'asst-chat-title', 'data-no-i18n': '' }, t('home.ai.assistant.newConversation'));
  const metaEl = el('p', { class: 'asst-chat-meta' }, t('assistant.chooseAModelThenSendYourFirst'));
  const skillChipsEl = el('div', { class: 'asst-skill-chips', hidden: true, 'aria-label': t('assistant.skillsEnabledInThisConversation') });
  const headMain = el('div', { class: 'asst-chat-head-main' }, titleEl, metaEl, skillChipsEl);

  const toolToggle = el('input', { type: 'checkbox', checked: toolStepsVisible });
  toolToggle.setAttribute('aria-label', t('assistant.showToolCallSteps'));
  toolToggle.addEventListener('change', () => {
    toolStepsVisible = Boolean(toolToggle.checked);
    saveLocal(TOOL_STEPS_KEY, toolStepsVisible);
    renderMessages();
  });
  const renameBtn = button(t('assistant.rename'), () => startRename(), 'ghost small', 'edit', {
    'aria-label': t('assistant.renameConversation'), title: t('assistant.renameConversation'),
  });
  const deleteBtn = button(t('common.delete'), () => removeConversation(), 'ghost small', 'trash', {
    'aria-label': t('assistant.deleteConversation'), title: t('assistant.deleteConversation'),
  });
  renameBtn.disabled = true;
  deleteBtn.disabled = true;
  const headActions = el('div', { class: 'asst-chat-actions' },
    el('label', { class: 'asst-tool-toggle' }, toolToggle, t('assistant.toolSteps')),
    renameBtn,
    deleteBtn);
  const head = el('div', { class: 'asst-chat-head' }, headMain, headActions);

  // 手机上侧栏隐藏，用会话下拉 + 新建按钮代替（桌面端由 CSS 隐藏本行）。
  const mobileSelect = el('select', { class: 'asst-conv-select', 'data-no-i18n': '', 'aria-label': t('assistant.switchConversation') });
  mobileSelect.addEventListener('change', () => {
    const value = Number(mobileSelect.value);
    if (value && value !== activeId) selectConversation(value);
  });
  const mobileNewBtn = button(t('common.create'), () => busy(mobileNewBtn, createNewConversation), 'soft small', 'plus');
  const mobileBar = el('div', { class: 'asst-mobile-bar' }, mobileSelect, mobileNewBtn);

  const messagesEl = el('div', {
    class: 'asst-messages',
    role: 'log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
  });
  messagesEl.append(loading(t('assistant.loadingConversations')));

  const liveArea = el('div', { class: 'asst-live' });

  // 输入区
  const composer = el('div', { class: 'asst-composer' });
  const skillsSummary = el('summary', { class: 'asst-skills-summary' });
  const skillsBody = el('div', { class: 'asst-skills-body' });
  const skillsDetails = el('details', { class: 'asst-skills' }, skillsSummary, skillsBody);
  const modelPicker = createModelPicker('assistant', { showModeToggle: false });
  modelPicker.node.classList.add('asst-model-picker');
  const textarea = el('textarea', {
    class: 'asst-input',
    rows: 3,
    maxlength: String(MAX_CONTENT),
    placeholder: t('assistant.eGLookAtMyRecentWrong'),
    'aria-label': t('assistant.typeAMessage'),
  });
  // 语音输入：优先浏览器原生识别；浏览器不支持时自动用 MediaRecorder 录音 + 服务端 /api/classroom/asr
  // 转写（≤60s）。两条路径都只把文本填入输入框，不自动发送。
  const voice = createVoiceInput({
    target: textarea, prefix: 'asst', label: t('assistant.voiceInput'), maxLength: MAX_CONTENT,
  });
  const counter = el('span', { class: 'asst-counter' }, `0 / ${MAX_CONTENT}`);
  const sendBtn = button(t('assistant.send'), () => submitTurn(textarea.value), '', 'message', {
    class: 'button asst-send', 'aria-label': t('assistant.sendMessage'), title: t('assistant.sendMessageEnter'),
  });
  const stopBtn = button(t('assistant.stopReplying'), () => stopTurn(), 'danger', 'stop', {
    class: 'button danger asst-stop', 'aria-label': t('assistant.stopThisReply'), title: t('assistant.stopThisReply'),
  });
  stopBtn.classList.add('asst-stop');
  stopBtn.hidden = true;

  textarea.addEventListener('input', () => {
    counter.textContent = optionsCount(textarea.value);
  });
  textarea.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitTurn(textarea.value);
    }
  });

  const scopeDetails = el('details', { class: 'asst-scope' });
  const scopeSummary = el('summary', {}, t('assistant.knowledgeScopeAndLimits'));
  const scopeBody = el('div', { class: 'asst-scope-body', 'data-no-i18n': '' });
  scopeBody.append(
    el('p', {}, info.scope_note || t('assistant.theAssistantCanOnlyReadYourOwn')),
    el('p', { class: 'asst-scope-tools' },
      t('assistant.readOnlyTools'),
      (info.items || []).filter(item => !item.side_effect).map(item => TOOL_LABELS[item.name] || item.name).join(' / ')),
    el('p', {},
      t('assistant.writeActionsRunAfterYourConfirmation'),
      (info.write_tools || []).map(name => TOOL_LABELS[name] || name).join(' / '),
      t('assistant.theAssistantNeverConfirmsGradesOrPublishes'))
  );
  scopeDetails.append(scopeSummary, scopeBody);

  const composerMeta = el('div', { class: 'asst-composer-meta' },
    el('span', { class: 'asst-hint' }, t('assistant.enterToSendShiftEnterForA')),
    counter);
  const inputRow = el('div', { class: 'asst-input-row' }, textarea, voice.node);
  const actionRow = el('div', { class: 'asst-action-row' },
    scopeDetails,
    el('div', { class: 'asst-send-row' }, stopBtn, sendBtn));
  composer.append(
    skillsDetails,
    el('div', { class: 'asst-composer-model' }, modelPicker.node),
    inputRow,
    voice.hintNode,
    el('div', { class: 'asst-composer-foot' }, composerMeta, actionRow)
  );

  chat.append(mobileBar, head, messagesEl, liveArea, composer);
  layout.append(sidebar, chat);
  page.append(layout);

  // -------------------------------------------------------------------------
  // 会话列表
  // -------------------------------------------------------------------------
  function upsertConversation(conversation) {
    if (!conversation || !conversation.id) return;
    const index = conversations.findIndex(item => item.id === conversation.id);
    if (index >= 0) conversations[index] = { ...conversations[index], ...conversation };
    else conversations.unshift(conversation);
    conversations.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  }

  function renderSidebar() {
    conversationList.replaceChildren();
    const selected = String(activeId || '');
    mobileSelect.replaceChildren();
    for (const conversation of conversations) {
      const status = conversation.status || 'idle';
      const label = trimText(conversation.title || t('home.ai.assistant.newConversation'), 24)
        + (ACTIVE_STATUSES.has(status) ? `（${STATUS_LABELS[status]}）` : '');
      mobileSelect.append(el('option', { value: String(conversation.id) }, label));
    }
    if (!conversations.length) {
      mobileSelect.append(el('option', { value: '' }, t('assistant.noConversationsYet')));
    }
    mobileSelect.value = selected && mobileSelect.querySelector(`option[value="${selected}"]`) ? selected : (conversations[0] ? String(conversations[0].id) : '');
    if (!conversations.length) {
      sideEmpty.hidden = false;
      return;
    }
    sideEmpty.hidden = true;
    for (const conversation of conversations) {
      const title = trimText(conversation.title || t('home.ai.assistant.newConversation'), 26) || t('home.ai.assistant.newConversation');
      const status = conversation.status || 'idle';
      const item = el('div', {
        class: `asst-conv-item${conversation.id === activeId ? ' active' : ''}`,
        role: 'listitem',
      });
      const open = el('button', {
        type: 'button',
        class: 'asst-conv-open',
        'aria-current': conversation.id === activeId ? 'true' : 'false',
        onClick: () => selectConversation(conversation.id),
      },
        el('span', { class: 'asst-conv-title', 'data-no-i18n': '' }, title),
        el('span', { class: 'asst-conv-meta' },
          [ACTIVE_STATUSES.has(status) ? STATUS_LABELS[status] : null,
            conversation.message_count ? t('common.countItems', { count: conversation.message_count }) : t('assistant.emptyConversation'),
            dateText(conversation.updated_at)].filter(Boolean).join(' · ')));
      const remove = el('button', {
        type: 'button',
        class: 'asst-conv-delete',
        title: t('assistant.deleteConversation'),
        'aria-label': t('assistant.deleteConversation_2', { v1: title }),
        onClick: event => {
          event.stopPropagation();
          deleteConversation(conversation);
        },
      }, icon('trash'));
      item.append(open, remove);
      conversationList.append(item);
    }
  }

  // -------------------------------------------------------------------------
  // 技能库：会话技能选择器 / chips / 管理弹层
  // -------------------------------------------------------------------------
  function skillKey(item) {
    return String(item.id);
  }

  function skillByKey(key) {
    return skillLibrary.find(item => skillKey(item) === String(key));
  }

  function activeSkillItems() {
    return skillLibrary.filter(item => selectedSkillIds.has(skillKey(item)) && item.enabled !== false);
  }

  function skillsGrouped() {
    return {
      builtin: skillLibrary.filter(item => item.builtin),
      custom: skillLibrary.filter(item => !item.builtin),
    };
  }

  function applyConversationSkills(rawIds) {
    const next = new Set();
    for (const key of (Array.isArray(rawIds) ? rawIds : []).map(String)) {
      const item = skillByKey(key);
      if (item && item.enabled !== false) next.add(key);
    }
    selectedSkillIds = next;
  }

  function pruneSelectedSkills() {
    for (const key of [...selectedSkillIds]) {
      const item = skillByKey(key);
      if (!item || item.enabled === false) selectedSkillIds.delete(key);
    }
  }

  function skillOptionRow(item) {
    const checkbox = el('input', {
      type: 'checkbox',
      checked: selectedSkillIds.has(skillKey(item)),
      'aria-label': t('assistant.enableSkill', { v1: item.name }),
    });
    checkbox.addEventListener('change', () => {
      const key = skillKey(item);
      if (checkbox.checked) selectedSkillIds.add(key);
      else selectedSkillIds.delete(key);
      renderSkillsUI();
      persistSkills();
    });
    return el('label', { class: 'asst-skill-option', title: item.description || '' },
      checkbox,
      el('span', { class: 'asst-skill-option-main' },
        el('span', { class: 'asst-skill-option-name', 'data-no-i18n': '' },
          item.name,
          item.builtin ? badge(t('assistant.builtIn'), 'blue') : null,
          item.enabled === false ? badge(t('assistant.disabled'), '') : null),
        el('span', { class: 'asst-skill-option-desc', 'data-no-i18n': '' }, item.description || t('assistant.noDescription'))));
  }

  function renderSkillsPanel() {
    const active = activeSkillItems();
    const { builtin, custom } = skillsGrouped();
    skillsSummary.replaceChildren(
      icon('sparkle'),
      el('span', { class: 'asst-skills-title' },
        active.length ? t('assistant.skillsEnabled', { v1: active.length }) : t('assistant.skillsNoneEnabled')),
      el('span', { class: 'asst-skills-hint' }, active.length
        ? el('span', { 'data-no-i18n': '' }, active.map(item => item.name).join(' · '))
        : t('assistant.pickBuiltInOrCustomSkillsTo')));
    skillsBody.replaceChildren(
      el('div', { class: 'asst-skills-group' },
        el('h4', {}, t('assistant.builtInSkills')),
        ...builtin.map(skillOptionRow)),
      el('div', { class: 'asst-skills-group' },
        el('h4', {}, t('assistant.mySkills')),
        custom.length
          ? custom.map(skillOptionRow)
          : el('p', { class: 'asst-help' }, t('assistant.noCustomSkillsYetCreateOneIn'))),
      el('p', { class: 'asst-help asst-skills-note' },
        t('assistant.skillsOnlyInjectMethodologyIntoTheSystem')
        + t('assistant.atMostCanBeEnabledAtOnce', { v1: skillLimits.max_selected || 5 })),
      el('div', { class: 'asst-skills-actions' },
        button(t('assistant.manageSkillLibrary'), () => openSkillsManager(), 'soft small', 'sliders'),
        active.length
          ? button(t('assistant.clearSkills'), () => {
            selectedSkillIds.clear();
            renderSkillsUI();
            persistSkills();
          }, 'ghost small', 'close')
          : null));
  }

  function renderSkillChips() {
    const active = activeSkillItems();
    skillChipsEl.hidden = active.length === 0;
    const chips = active.map(item => {
      const removeBtn = el('button', {
        type: 'button',
        class: 'asst-skill-chip-remove',
        'aria-label': t('assistant.removeSkill', { v1: item.name }),
        title: t('assistant.removeSkill_2'),
      }, '×');
      removeBtn.addEventListener('click', () => {
        selectedSkillIds.delete(skillKey(item));
        renderSkillsUI();
        persistSkills();
      });
      return el('span', { class: 'asst-skill-chip' }, icon('sparkle'), el('span', { 'data-no-i18n': '' }, item.name), removeBtn);
    });
    skillChipsEl.replaceChildren(
      el('span', { class: 'asst-skill-chips-label' }, t('assistant.skills')),
      ...chips);
  }

  function renderSkillsUI() {
    renderSkillsPanel();
    renderSkillChips();
  }

  async function persistSkills() {
    if (!activeId) return;
    try {
      const response = await api(`/assistant/conversations/${activeId}`, {
        method: 'PATCH',
        data: { skill_ids: [...selectedSkillIds] },
      });
      if (response.conversation) {
        if (detail) detail.conversation = { ...detail.conversation, ...response.conversation };
        upsertConversation(response.conversation);
        renderSidebar();
      }
    } catch (error) {
      toast(error.message || t('assistant.failedToSaveTheSkillSelectionIt'), true);
    }
  }

  function openSkillsManager() {
    const dialog = el('dialog', { class: 'asst-skill-dialog', 'aria-label': t('assistant.skillLibrary') });
    const listHost = el('div', { class: 'asst-skill-manager-list' });
    const formHost = el('div', { class: 'asst-skill-form', hidden: true });
    const closeBtn = button(t('admin.closed'), () => dialog.close(), 'ghost');
    const newBtn = button(t('assistant.newSkill'), () => showSkillForm(null), 'soft small', 'plus');
    dialog.append(
      el('div', { class: 'asst-skill-dialog-head' },
        el('div', {},
          el('h3', {}, t('assistant.skillLibrary')),
          el('p', { class: 'asst-help' },
            t('assistant.builtInSkillsReadOnly', { v1: skillLibrary.filter(item => item.builtin).length })
            + t('assistant.upToCustomSkillsEachUpTo', { v1: skillLimits.max_skills_per_user || 100, v2: skillLimits.max_content_chars || 20000 })
            + t('assistant.skillsOnlyInjectMethodologyTheyNeverChange'))),
        closeBtn),
      el('div', { class: 'asst-skill-dialog-actions' }, newBtn),
      formHost,
      listHost);

    async function refreshLibrary() {
      try {
        const response = await api('/assistant/skills');
        skillLibrary = Array.isArray(response.items) ? response.items : [];
        skillLimits = response.limits || skillLimits;
        pruneSelectedSkills();
        renderSkillsUI();
        renderManagerList();
      } catch (error) {
        toast(error.message || t('assistant.failedToRefreshTheSkillLibrary'), true);
      }
    }

    function skillPreview(item) {
      return el('details', { class: 'asst-skill-preview' },
        el('summary', {}, t('assistant.viewMethodology')),
        el('pre', { class: 'asst-skill-content-pre', 'data-no-i18n': '' }, item.content || ''));
    }

    function renderManagerList() {
      const { builtin, custom } = skillsGrouped();
      const builtinNodes = builtin.map(item => el('div', { class: 'asst-skill-manager-item' },
        el('div', { class: 'asst-skill-manager-head' },
          el('strong', { 'data-no-i18n': '' }, item.name),
          badge(t('assistant.builtInReadOnly'), 'blue')),
        el('p', { class: 'asst-help', 'data-no-i18n': '' }, item.description || ''),
        skillPreview(item)));
      const customNodes = custom.map(item => el('div', { class: 'asst-skill-manager-item' },
        el('div', { class: 'asst-skill-manager-head' },
          el('strong', { 'data-no-i18n': '' }, item.name),
          badge(item.enabled === false ? t('assistant.disabled') : t('settings.embeddingStatusEnabled'), item.enabled === false ? '' : 'teal'),
          el('span', { class: 'asst-skill-version' }, `v${item.version}`)),
        el('p', { class: 'asst-help', 'data-no-i18n': '' }, item.description || ''),
        el('div', { class: 'asst-skill-manager-item-actions' },
          button(t('settings.edit'), () => showSkillForm(item), 'ghost small', 'edit'),
          button(item.enabled === false ? t('assistant.enable') : t('assistant.disable'), event => busy(event.currentTarget, async () => {
            await api(`/assistant/skills/${item.id}`, {
              method: 'PATCH', data: { version: item.version, enabled: item.enabled === false },
            });
            toast(item.enabled === false ? t('assistant.skillEnabled') : t('assistant.skillDisabledItWillBeIgnoredIn'));
            await refreshLibrary();
          }), 'ghost small', item.enabled === false ? 'check' : 'close'),
          button(t('common.delete'), event => busy(event.currentTarget, async () => {
            if (!window.confirm(t('assistant.deleteSkillThisCannotBeUndone', { v1: item.name }))) return;
            await api(`/assistant/skills/${item.id}`, { method: 'DELETE' });
            toast(t('assistant.skillDeleted'));
            await refreshLibrary();
          }), 'ghost small', 'trash')),
        skillPreview(item)));
      listHost.replaceChildren(
        el('h4', { class: 'asst-skill-manager-title' }, t('assistant.builtInSkills')),
        ...(builtinNodes.length ? builtinNodes : [el('p', { class: 'asst-help' }, t('assistant.noBuiltInSkills'))]),
        el('h4', { class: 'asst-skill-manager-title' }, t('assistant.mySkills')),
        ...(customNodes.length ? customNodes : [el('p', { class: 'asst-help' }, t('assistant.noCustomSkillsYet'))]));
    }

    function showSkillForm(skill) {
      const nameInput = el('input', {
        class: 'input', value: skill?.name || '', maxlength: '40',
        placeholder: t('assistant.eGThreeStepWrongAnswerReview'), 'aria-label': t('assistant.skillName'),
      });
      const descInput = el('input', {
        class: 'input', value: skill?.description || '', maxlength: '200',
        placeholder: t('assistant.oneSentenceDescription'), 'aria-label': t('assistant.skillDescription'),
      });
      const contentArea = el('textarea', {
        class: 'input asst-skill-content-input', rows: 7, maxlength: '20000',
        placeholder: t('assistant.describeTheStepsOutputStructureAndBoundaries'),
        'aria-label': t('assistant.skillMethodology'),
      });
      contentArea.value = skill?.content || '';
      const enabledToggle = el('input', { type: 'checkbox', checked: skill ? skill.enabled !== false : true });
      const saveBtn = button(skill ? t('settings.wizard.saveChanges') : t('assistant.createSkill'), event => busy(event.currentTarget, async () => {
        const name = nameInput.value.trim();
        const content = contentArea.value.trim();
        if (!name) throw new Error(t('assistant.skillNameIsRequired'));
        if (!content) throw new Error(t('assistant.skillContentIsRequired'));
        if (skill) {
          await api(`/assistant/skills/${skill.id}`, {
            method: 'PATCH',
            data: {
              version: skill.version,
              name,
              description: descInput.value.trim(),
              content,
              enabled: Boolean(enabledToggle.checked),
            },
          });
          toast(t('assistant.skillUpdated'));
        } else {
          await api('/assistant/skills', {
            method: 'POST',
            data: { name, description: descInput.value.trim(), content, enabled: Boolean(enabledToggle.checked) },
          });
          toast(t('assistant.skillCreated'));
        }
        formHost.hidden = true;
        await refreshLibrary();
      }), '', 'check');
      const cancelBtn = button(t('common.cancel'), () => { formHost.hidden = true; }, 'ghost');
      formHost.replaceChildren(
        el('div', { class: 'asst-skill-form-head' }, el('h4', {}, skill ? t('assistant.editSkill') : t('assistant.newSkill')),
          el('span', { class: 'asst-help' }, t('assistant.takesEffectImmediatelyAfterSavingTickIt'))),
        el('label', { class: 'asst-skill-field' }, t('assistant.name'), nameInput),
        el('label', { class: 'asst-skill-field' }, t('assistant.description'), descInput),
        el('label', { class: 'asst-skill-field' }, t('assistant.methodology'), contentArea),
        el('label', { class: 'asst-skill-enabled' }, enabledToggle, t('assistant.enableThisSkill')),
        el('div', { class: 'asst-skill-form-actions' }, cancelBtn, saveBtn));
      formHost.hidden = false;
      formHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      nameInput.focus();
    }

    dialog.addEventListener('close', () => dialog.remove());
    renderManagerList();
    document.body.append(dialog);
    dialog.showModal();
  }

  // -------------------------------------------------------------------------
  // 头部 / 消息渲染
  // -------------------------------------------------------------------------
  function renderHead() {
    const conversation = detail ? detail.conversation : null;
    if (!conversation) {
      titleEl.textContent = t('home.ai.assistant.newConversation');
      metaEl.textContent = t('assistant.chooseAModelAndSendYourFirst');
      renameBtn.disabled = true;
      deleteBtn.disabled = true;
      return;
    }
    titleEl.textContent = conversation.title || t('home.ai.assistant.newConversation');
    const status = conversation.status || 'idle';
    const parts = [
      conversation.model ? t('tasks.modelN', { n: conversation.model }) : t('assistant.noModelSelected'),
      STATUS_LABELS[status] || status,
      conversation.message_count ? t('home.activity.messages', { count: conversation.message_count }) : null,
    ].filter(Boolean);
    metaEl.textContent = parts.join(' · ');
    renameBtn.disabled = false;
    deleteBtn.disabled = false;
  }

  function emptyMessagesNode() {
    return empty(
      t('assistant.startChattingWithTheAssistant'),
      t('assistant.tryOneOfTheQuestionsBelowThe'),
      null,
      'message'
    );
  }

  function welcomeNode() {
    const suggestions = [
      t('assistant.reviewMyRecentWrongAnswersAndExplain'),
      t('assistant.whichMathKnowledgePointsAmIWeakest'),
      t('assistant.generateAConsolidationPracticePaperFromMy'),
    ];
    const actions = el('div', { class: 'asst-suggestions' });
    for (const text of suggestions) {
      actions.append(button(
        text,
        () => {
          textarea.value = text;
          counter.textContent = optionsCount(text);
          textarea.focus();
          submitTurn(text);
        },
        'ghost small',
        'sparkle'
      ));
    }
    return el('div', { class: 'asst-welcome' },
      el('div', { class: 'asst-welcome-icon', 'aria-hidden': 'true' }, icon('sparkle')),
      el('h3', {}, t('assistant.assistantReadyWhenYouAre')),
      el('p', {}, t('assistant.iAnswerUsingTheQuestionBankWrong')),
      actions);
  }

  function refChip(item) {
    const kind = item.kind;
    const title = trimText(item.title || t('assistant.references'), 18);
    if (kind === 'question' && item.qid) {
      return button(t('assistant.q', { v1: qidLabel(item.qid) }), () => go('practice', item.qid), 'ghost small', 'book',
        { title: t('assistant.openQuestion', { v1: item.qid }) });
    }
    if (kind === 'note' && item.note_id) {
      return button(t('assistant.note', { v1: title }), () => {
        state.selectedNote = item.note_id;
        state.noteTab = 'all';
        go('notes');
      }, 'ghost small', 'notes', { 'data-no-i18n': '' });
    }
    if (kind === 'kp' && item.kp_id) {
      return button(t('assistant.knowledgePoint', { v1: title }), () => openKpDetailDialog(item.kp_id, item.title || ''), 'ghost small', 'compass', { 'data-no-i18n': '' });
    }
    if (kind === 'mybank_question' && item.question_id) {
      return button(t('assistant.myQuestions'), () => go('mybank'), 'ghost small', 'tag');
    }
    if (kind === 'paper' && item.paper_id) {
      return button(t('home.status.papers'), () => {
        state.paperlabPaperId = item.paper_id;
        go('paperlab');
      }, 'ghost small', 'printer');
    }
    return el('span', { class: 'badge', 'data-no-i18n': '' }, title);
  }

  function refsNode(items) {
    const list = (items || []).filter(item => item && item.kind);
    if (!list.length) return null;
    const row = el('div', { class: 'asst-refs' }, el('span', { class: 'asst-refs-label' }, t('assistant.references')));
    for (const item of list) row.append(refChip(item));
    return row;
  }

  async function resolveAction(actionId, confirm) {
    if (!activeId) return;
    const conversationId = activeId;
    try {
      const result = await api(`/assistant/conversations/${conversationId}/confirm`, {
        method: 'POST',
        data: { action_id: actionId, confirm },
      });
      if (result.confirmed) toast(result.summary || t('assistant.actionCompleted'));
      else toast(result.notice || t('assistant.actionCancelled'));
      await refreshConversation(conversationId);
      await refreshConversations();
    } catch (error) {
      toast(error.message || t('common.actionFailed'), true);
    }
  }

  function confirmCard(part) {
    renderedActionIds.add(part.id);
    const confirmBtn = button(t('assistant.confirmAndRun'), () => busy(confirmBtn, () => resolveAction(part.id, true)), '', 'check');
    const cancelBtn = button(t('common.cancel'), () => busy(cancelBtn, () => resolveAction(part.id, false)), 'secondary', 'close');
    return el('div', { class: 'asst-confirm-card' },
      el('div', { class: 'asst-confirm-head' },
        badge(t('assistant.needsYourConfirmation'), 'amber'),
        el('strong', {}, TOOL_LABELS[part.tool] || t('assistant.writeAction'))),
      el('p', { class: 'asst-confirm-summary', 'data-no-i18n': '' }, part.summary || t('assistant.theAssistantPreparedAWriteActionNothing')),
      el('details', { class: 'asst-confirm-detail' },
        el('summary', {}, t('assistant.viewActionParameters')),
        el('pre', { class: 'asst-json' }, jsonPreview(part.args || {}, 1200))),
      el('p', { class: 'asst-help' }, t('assistant.nothingIsCreatedUntilYouClickConfirm')),
      el('div', { class: 'asst-confirm-actions' }, cancelBtn, confirmBtn));
  }

  function effectCard(part) {
    const effect = part.effect || {};
    const actions = [];
    if (effect.kind === 'explain_job' && effect.job_id) {
      actions.push(button(t('assistant.viewTask'), () => go('tasks'), 'ghost small', 'history'));
    }
    if (effect.kind === 'paper' && effect.paper_id) {
      actions.push(button(t('assistant.openPaperStudio'), () => {
        state.paperlabPaperId = effect.paper_id;
        go('paperlab');
      }, 'ghost small', 'printer'));
    }
    return el('div', { class: 'asst-effect' },
      badge(t('assistant.completed'), 'teal'),
      el('span', { class: 'asst-effect-text', 'data-no-i18n': '' }, part.summary || t('assistant.actionCompleted_2')),
      ...actions);
  }

  function toolStepNode(message) {
    const result = message.tool_result || {};
    const ok = result.ok !== false;
    const pending = Boolean(result.requires_confirmation);
    const label = TOOL_LABELS[message.tool_name] || message.tool_name || t('assistant.tool');
    const brief = trimText(result.summary || result.error || '', 90);
    const tone = pending ? 'amber' : (ok ? 'teal' : 'red');
    const toneLabel = pending ? t('assistant.pendingConfirmation') : (ok ? t('tasks.status.succeeded') : t('tasks.status.failed'));
    const input = message.tool_input && Object.keys(message.tool_input).length
      ? el('div', { class: 'asst-tool-part' },
        el('strong', {}, t('assistant.callParameters')),
        el('pre', { class: 'asst-json', 'data-no-i18n': '' }, jsonPreview(message.tool_input, 1200)))
      : null;
    const output = el('div', { class: 'asst-tool-part' },
      el('strong', {}, t('assistant.toolResult')),
      el('pre', { class: 'asst-json', 'data-no-i18n': '' }, jsonPreview(message.tool_result, 2600)));
    return el('details', { class: 'asst-tool' },
      el('summary', { class: 'asst-tool-summary' },
        icon('layers'),
        el('span', { class: 'asst-tool-label' }, label),
        el('code', { class: 'asst-tool-name' }, message.tool_name || ''),
        badge(toneLabel, tone),
        brief ? el('span', { class: 'asst-tool-brief', 'data-no-i18n': '' }, brief) : null),
      el('div', { class: 'asst-tool-body' }, input, output));
  }

  function userNode(message) {
    return el('div', { class: 'asst-msg asst-msg-user' },
      el('div', { class: 'asst-bubble asst-bubble-user', 'data-no-i18n': '' }, markdown(messageText(message))),
      el('span', { class: 'asst-msg-time' }, dateText(message.created_at)));
  }

  function assistantNode(message) {
    const bubble = el('div', { class: 'asst-bubble asst-bubble-assistant' });
    for (const part of message.parts || []) {
      if (!part || typeof part !== 'object') continue;
      if (part.type === 'text' && part.text) bubble.append(withNoI18n(markdown(part.text)));
      else if (part.type === 'action_confirm') bubble.append(confirmCard(part));
      else if (part.type === 'side_effect') bubble.append(effectCard(part));
      else if (part.type === 'refs') {
        const refs = refsNode(part.items);
        if (refs) bubble.append(refs);
      }
    }
    return el('div', { class: 'asst-msg asst-msg-assistant' },
      el('div', { class: 'asst-avatar', 'aria-hidden': 'true' }, t('assistant.paper')),
      el('div', { class: 'asst-msg-body' },
        bubble,
        el('span', { class: 'asst-msg-time' }, dateText(message.created_at))));
  }

  function systemNode(message) {
    return el('div', { class: 'asst-system', role: 'status', 'data-no-i18n': '' }, messageText(message));
  }

  function messageNode(message) {
    if (!message || !message.role) return null;
    if (message.role === 'user') return userNode(message);
    if (message.role === 'assistant') return assistantNode(message);
    if (message.role === 'system') return systemNode(message);
    if (message.role === 'tool') return toolStepsVisible ? toolStepNode(message) : null;
    return null;
  }

  function errorNode(conversation) {
    if (!conversation || conversation.status !== 'failed' || !conversation.last_error) return null;
    const lastUser = [...(detail ? detail.messages : [])].reverse().find(item => item.role === 'user');
    const retry = lastUser
      ? button(t('common.retry'), event => busy(event.currentTarget, () => submitTurn(messageText(lastUser))), 'soft small', 'repeat')
      : null;
    return el('div', { class: 'asst-error-bubble', role: 'alert' },
      el('div', { class: 'asst-error-main' },
        el('strong', {}, t('assistant.thisReplyDidnTFinish')),
        el('p', {}, conversation.last_error)),
      retry);
  }

  function betterScrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderMessages({ scrollToBottom = false } = {}) {
    const previousScrollHeight = messagesEl.scrollHeight;
    const previousScrollTop = messagesEl.scrollTop;
    renderedActionIds = new Set();
    messagesEl.replaceChildren();

    if (!detail) {
      messagesEl.append(welcomeNode());
      return;
    }
    const messages = detail.messages || [];
    if (!messages.length) {
      messagesEl.append(welcomeNode());
    } else {
      if (detail.has_older) {
        const olderBtn = button(loadingOlder ? t('common.loading') : t('assistant.loadEarlierMessages'), () => loadOlder(), 'ghost small', 'history');
        olderBtn.disabled = loadingOlder;
        messagesEl.append(el('div', { class: 'asst-load-older' }, olderBtn));
      }
      for (const message of messages) {
        const node = messageNode(message);
        if (node) messagesEl.append(node);
      }
    }
    const conversation = detail.conversation;
    const pending = conversation && conversation.pending_action;
    if (pending && !renderedActionIds.has(pending.id)) {
      messagesEl.append(confirmCard({ ...pending, type: 'action_confirm' }));
    }
    const error = errorNode(conversation);
    if (error) messagesEl.append(error);

    if (scrollToBottom) {
      betterScrollToBottom();
    } else if (previousScrollHeight && messagesEl.scrollHeight < previousScrollHeight) {
      // 向上加载更早消息后保持阅读位置不跳动。
      messagesEl.scrollTop = messagesEl.scrollHeight - previousScrollHeight + previousScrollTop;
    }
  }

  // -------------------------------------------------------------------------
  // 数据加载
  // -------------------------------------------------------------------------
  async function refreshConversations() {
    try {
      const response = await api('/assistant/conversations');
      if (!page.isConnected || !current(version)) return;
      conversations = response.items || [];
      renderSidebar();
      renderHead();
    } catch {
      /* 列表刷新失败不影响当前对话展示 */
    }
  }

  async function fetchConversation(conversationId, { scrollToBottom = false } = {}) {
    const response = await api(`/assistant/conversations/${conversationId}?page=1&page_size=100`);
    if (!page.isConnected || !current(version)) return null;
    detail = {
      conversation: response.conversation,
      messages: response.messages || [],
      page: response.page || 1,
      has_older: Boolean(response.has_older),
    };
    activeId = conversationId;
    state.assistantConversationId = conversationId;
    applyConversationSkills(response.conversation?.skill_ids);
    upsertConversation(response.conversation);
    renderSidebar();
    renderHead();
    renderSkillsUI();
    renderMessages({ scrollToBottom });
    return detail;
  }

  async function refreshConversation(conversationId) {
    if (!conversationId || !page.isConnected) return;
    try {
      await fetchConversation(conversationId, { scrollToBottom: true });
    } catch (error) {
      if (page.isConnected) toast(error.message || t('assistant.failedToRefreshConversation'), true);
    }
  }

  async function loadOlder() {
    if (!activeId || !detail || loadingOlder || !detail.has_older) return;
    loadingOlder = true;
    try {
      const response = await api(
        `/assistant/conversations/${activeId}?page=${(detail.page || 1) + 1}&page_size=100`
      );
      if (!page.isConnected || !current(version) || !detail) return;
      detail.messages = [...(response.messages || []), ...detail.messages];
      detail.page = response.page || detail.page + 1;
      detail.has_older = Boolean(response.has_older);
      renderMessages({ scrollToBottom: false });
    } catch (error) {
      toast(error.message || t('assistant.failedToLoadEarlierMessages'), true);
    } finally {
      loadingOlder = false;
    }
  }

  async function selectConversation(conversationId) {
    if (running) {
      toast(t('assistant.aReplyIsInProgressWaitFor'), true);
      return;
    }
    if (conversationId === activeId) return;
    messagesEl.replaceChildren(loading(t('assistant.loadingConversations')));
    try {
      await fetchConversation(conversationId, { scrollToBottom: true });
      attachRunningJobIfAny();
    } catch (error) {
      if (page.isConnected) {
        messagesEl.replaceChildren(notice(error.message || t('assistant.failedToLoadConversation'), 'error'));
      }
    }
  }

  async function createNewConversation() {
    if (running) {
      toast(t('assistant.aReplyIsInProgressStopIt'), true);
      return;
    }
    const response = await api('/assistant/conversations', {
      method: 'POST',
      data: { skill_ids: [...selectedSkillIds] },
    });
    upsertConversation(response.conversation);
    detail = {
      conversation: response.conversation,
      messages: [],
      page: 1,
      has_older: false,
    };
    activeId = response.conversation.id;
    state.assistantConversationId = activeId;
    renderedActionIds = new Set();
    applyConversationSkills(response.conversation?.skill_ids);
    renderSidebar();
    renderHead();
    renderSkillsUI();
    renderMessages({ scrollToBottom: true });
    textarea.focus();
  }

  async function deleteConversation(conversation) {
    if (!conversation || !conversation.id) return;
    if (!window.confirm(t('assistant.deleteConversationThisCannotBeUndone', {
      v1: trimText(conversation.title || t('home.ai.assistant.newConversation'), 30),
    }))) return;
    try {
      await api(`/assistant/conversations/${conversation.id}`, { method: 'DELETE' });
      conversations = conversations.filter(item => item.id !== conversation.id);
      if (activeId === conversation.id) {
        activeId = null;
        detail = null;
        state.assistantConversationId = null;
        renderHead();
        if (conversations.length) {
          await fetchConversation(conversations[0].id, { scrollToBottom: true });
        } else {
          selectedSkillIds.clear();
          renderSkillsUI();
          renderMessages();
        }
      }
      renderSidebar();
      toast(t('assistant.conversationDeleted'));
    } catch (error) {
      toast(error.message || t('assistant.failedToDeleteConversation'), true);
    }
  }

  async function removeConversation() {
    if (!detail) return;
    await deleteConversation(detail.conversation);
  }

  function startRename() {
    if (!detail) return;
    const conversation = detail.conversation;
    if (conversation.status && ACTIVE_STATUSES.has(conversation.status)) {
      toast(t('assistant.aReplyIsInProgressRenameAfter'), true);
      return;
    }
    const inputNode = el('input', {
      class: 'input asst-rename-input',
      value: conversation.title || '',
      maxlength: '80',
      'aria-label': t('assistant.conversationTitle'),
    });
    const saveBtn = button(t('common.save'), () => busy(saveBtn, async () => {
      const nextTitle = inputNode.value.trim();
      if (!nextTitle) {
        toast(t('assistant.conversationTitleIsRequired'), true);
        inputNode.focus();
        return;
      }
      const response = await api(`/assistant/conversations/${conversation.id}`, {
        method: 'PATCH',
        data: { title: nextTitle },
      });
      detail.conversation = { ...detail.conversation, ...response.conversation };
      upsertConversation(response.conversation);
      renderHead();
      renderSidebar();
      toast(t('assistant.renamed'));
    }), '', 'check');
    const cancelBtn = button(t('common.cancel'), () => renderHead(), 'ghost', 'close');
    const row = el('div', { class: 'asst-rename-row' }, inputNode, saveBtn, cancelBtn);
    headMain.replaceChildren(row);
    inputNode.focus();
    inputNode.select();
    inputNode.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        saveBtn.click();
      } else if (event.key === 'Escape') {
        renderHead();
      }
    });
  }

  // -------------------------------------------------------------------------
  // 任务进度（复用 core/jobs.js 的 SSE/轮询任务壳）
  // -------------------------------------------------------------------------
  function syncComposer() {
    sendBtn.disabled = running;
    stopBtn.hidden = !running;
    textarea.disabled = false;
    if (running) {
      sendBtn.replaceChildren(icon('clock'), document.createTextNode(t('assistant.waitingForAReply')));
    } else {
      sendBtn.replaceChildren(icon('message'), document.createTextNode(t('assistant.send')));
    }
  }

  async function attachRunningJob(jobId) {
    running = true;
    state.assistantJobId = jobId;
    syncComposer();
    liveArea.replaceChildren(el('div', { class: 'asst-live-label' },
      el('span', { class: 'spinner' }), t('assistant.theAssistantIsThinkingAndCallingTools')));
    const shell = el('div', { class: 'asst-job-shell' });
    liveArea.append(shell);
    try {
      await mountJob(shell, jobId, null, { task: 'assistant-turn' });
    } catch {
      /* mountJob 自身已兜底轮询；这里只负责收尾刷新 */
    } finally {
      running = false;
      state.assistantJobId = null;
      liveArea.replaceChildren();
      syncComposer();
      if (page.isConnected && activeId) {
        await refreshConversation(activeId);
        await refreshConversations();
      }
    }
  }

  function attachRunningJobIfAny() {
    if (!detail || running) return;
    const conversation = detail.conversation;
    if (!conversation || !ACTIVE_STATUSES.has(conversation.status) || !conversation.job_id) return;
    attachRunningJob(conversation.job_id);
  }

  async function stopTurn() {
    if (!activeId || !running) return;
    try {
      const result = await api(`/assistant/conversations/${activeId}/stop`, { method: 'POST' });
      toast(result.cancelled ? t('assistant.thisReplyWasStopped') : (result.notice || t('assistant.theReplyHasAlreadyEnded')));
    } catch (error) {
      toast(error.message || t('assistant.failedToStopTryAgainLater'), true);
    }
  }

  async function submitTurn(rawContent) {
    const content = String(rawContent || '').trim();
    if (!content) {
      toast(t('assistant.enterAMessage'), true);
      textarea.focus();
      return;
    }
    if (content.length > MAX_CONTENT) {
      toast(t('assistant.aMessageCannotExceedCharacters', { v1: MAX_CONTENT }), true);
      return;
    }
    if (running) {
      toast(t('assistant.thePreviousReplyIsStillInProgress'), true);
      return;
    }
    let payload;
    try {
      payload = modelPicker.payload();
    } catch (error) {
      toast(error.message || t('assistant.addAnAvailableModelInModelSettings'), true);
      return;
    }

    textarea.value = '';
    counter.textContent = optionsCount('');
    running = true;
    syncComposer();
    liveArea.replaceChildren();
    let conversationId = activeId;
    try {
      if (!conversationId) {
        const created = await api('/assistant/conversations', {
          method: 'POST',
          data: { skill_ids: [...selectedSkillIds] },
        });
        conversationId = created.conversation.id;
        activeId = conversationId;
        state.assistantConversationId = conversationId;
        upsertConversation(created.conversation);
        detail = { conversation: created.conversation, messages: [], page: 1, has_older: false };
        applyConversationSkills(created.conversation?.skill_ids);
        renderSidebar();
        renderHead();
        renderSkillsUI();
      }
      const response = await api(`/assistant/conversations/${conversationId}/messages`, {
        method: 'POST',
        data: {
          content,
          provider_id: payload.provider_id,
          model: payload.model,
          skill_ids: [...selectedSkillIds],
        },
      });
      state.assistantJobId = response.job.id;
      upsertConversation(response.conversation);
      await refreshConversation(conversationId);
      await attachRunningJob(response.job.id);
    } catch (error) {
      toast(error.message || t('assistant.failedToSendPleaseTryAgain'), true);
      running = false;
      state.assistantJobId = null;
      syncComposer();
      liveArea.replaceChildren();
      if (activeId) await refreshConversation(activeId);
    }
  }

  // -------------------------------------------------------------------------
  // 初始化
  // -------------------------------------------------------------------------
  container.replaceChildren(page);
  renderSidebar();
  renderHead();
  renderSkillsUI();
  syncComposer();

  const wanted = state.assistantConversationId;
  const initial = conversations.find(item => item.id === wanted) || conversations[0];
  if (initial) {
    try {
      await fetchConversation(initial.id, { scrollToBottom: true });
      if (!current(version)) return;
      attachRunningJobIfAny();
    } catch (error) {
      if (current(version)) {
        messagesEl.replaceChildren(notice(error.message || t('assistant.failedToLoadConversation'), 'error'));
      }
    }
  } else {
    renderMessages({ scrollToBottom: true });
  }
}
