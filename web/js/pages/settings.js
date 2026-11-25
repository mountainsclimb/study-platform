import { api, ensurePreferences, errorText, refreshProviders } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, badge, empty, heading, loading, notice, field, input, select,
  number, dateText, busy, toast,
} from '../core/dom.js';
import { current, renderRoute } from '../core/router.js';
import { groupedModelOptions } from '../components/modelPicker.js';
import { renderJobHistory } from './upload.js';
import { t } from '../core/i18n.js';

// 任务名用 getter 形式按当前语言取 t()：模块加载时读取会固定成首屏语言。
const TASK_LABELS = {
  get explain() { return t('settings.task.explain'); },
  get grade() { return t('settings.task.grade'); },
  get judge() { return t('settings.task.judge'); },
  get notes() { return t('settings.task.notes'); },
  get ocr() { return t('settings.task.ocr'); },
  get upload() { return t('settings.task.upload'); },
  get tagging() { return t('settings.task.tagging'); },
  // 整合模块新增任务（task-13）：与任务中心/各功能页的任务名保持一致。
  get course() { return t('settings.task.course'); },
  get 'paper-import'() { return t('settings.task.paper-import'); },
  get 'paper-enrich'() { return t('settings.task.paper-enrich'); },
  get 'paper-compose'() { return t('settings.task.paper-compose'); },
  get 'paper-revision'() { return t('settings.task.paper-revision'); },
  get 'insight-report'() { return t('settings.task.insight-report'); },
  get 'assistant-turn'() { return t('settings.task.assistant-turn'); },
};

const TASK_ORDER = [
  'explain', 'grade', 'judge', 'notes', 'ocr', 'upload', 'tagging',
  'course', 'paper-import', 'paper-enrich', 'paper-compose', 'paper-revision',
  'insight-report', 'assistant-turn',
];

// 主模型 / 核验模型 / 裁判模型的适用范围（行内提示；核验与裁判都要选「独立连接 + 独立模型」）。
const TASK_HINTS = {
  get explain() { return t('settings.taskHint.explain'); },
  get grade() { return t('settings.taskHint.grade'); },
  get judge() { return t('settings.taskHint.judge'); },
  get notes() { return t('settings.taskHint.notes'); },
  get ocr() { return t('settings.taskHint.ocr'); },
  get upload() { return t('settings.taskHint.upload'); },
  get tagging() { return t('settings.taskHint.tagging'); },
  get course() { return t('settings.taskHint.course'); },
  get 'paper-import'() { return t('settings.taskHint.paper-import'); },
  get 'paper-enrich'() { return t('settings.taskHint.paper-enrich'); },
  get 'paper-compose'() { return t('settings.taskHint.paper-compose'); },
  get 'paper-revision'() { return t('settings.taskHint.paper-revision'); },
  get 'insight-report'() { return t('settings.taskHint.insight-report'); },
  get 'assistant-turn'() { return t('settings.taskHint.assistant-turn'); },
};

const KIND_LABELS = {
  get openai() { return t('settings.kind.openai'); },
  get anthropic() { return t('settings.kind.anthropic'); },
  get gemini() { return t('settings.kind.gemini'); },
  get gemini_native() { return t('settings.kind.gemini_native'); },
  'azure_openai': 'Azure OpenAI',
};

const AUTH_MODE_LABELS = {
  get auto() { return t('settings.authMode.auto'); },
  get bearer() { return t('settings.authMode.bearer'); },
  get 'x-api-key'() { return t('settings.authMode.x-api-key'); },
  get 'api-key'() { return t('settings.authMode.api-key'); },
  get query_key() { return t('settings.authMode.query_key'); },
  get none() { return t('settings.authMode.none'); },
};

const MAX_EXTRA_HEADERS = 8;

// 「常用自定义与本地」分组：比官方预设更贴近中转站 / 本地模型场景。
const CUSTOM_PRESETS = [
  {
    get group() { return t('settings.wizard.customGroup'); },
    get name() { return t('settings.wizard.presetOpenaiCompat'); },
    kind: 'openai',
    base_url: 'https://',
    models: [],
    auth_mode: 'auto',
    get hint() { return t('settings.wizard.presetOpenaiCompatHint'); },
  },
  {
    get group() { return t('settings.wizard.customGroup'); },
    get name() { return t('settings.wizard.presetOllama'); },
    kind: 'openai',
    base_url: 'http://127.0.0.1:11434/v1',
    models: ['llama3.2', 'qwen2.5', 'qwen2.5-vl', 'deepseek-r1', 'nomic-embed-text'],
    auth_mode: 'none',
    local: true,
    get hint() { return t('settings.wizard.presetOllamaHint'); },
  },
  {
    get group() { return t('settings.wizard.customGroup'); },
    get name() { return t('settings.wizard.presetLmStudio'); },
    kind: 'openai',
    base_url: 'http://127.0.0.1:1234/v1',
    models: ['local-model'],
    auth_mode: 'none',
    local: true,
    get hint() { return t('settings.wizard.presetLmStudioHint'); },
  },
  {
    get group() { return t('settings.wizard.customGroup'); },
    name: 'Azure OpenAI',
    kind: 'azure_openai',
    base_url: 'https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT',
    models: ['gpt-4o', 'gpt-4.1-mini'],
    auth_mode: 'api-key',
    compat: { api_version: '2024-10-21' },
    get hint() { return t('settings.wizard.presetAzureHint'); },
  },
  {
    get group() { return t('settings.wizard.customGroup'); },
    get name() { return t('settings.wizard.presetClaudeRelay'); },
    kind: 'anthropic',
    base_url: 'https://',
    models: ['claude-sonnet-4-5', 'claude-3-5-haiku-latest'],
    auth_mode: 'auto',
    get hint() { return t('settings.wizard.presetClaudeRelayHint'); },
  },
  {
    get group() { return t('settings.wizard.customGroup'); },
    get name() { return t('settings.wizard.presetGeminiNative'); },
    kind: 'gemini_native',
    base_url: 'https://generativelanguage.googleapis.com/v1beta',
    models: ['gemini-2.5-flash', 'gemini-2.5-pro'],
    auth_mode: 'query_key',
    get hint() { return t('settings.wizard.presetGeminiNativeHint'); },
  },
];

// 官方 10 家预设的推荐模型建议：选中预设后自动填入，不再留空 models。
const PRESET_MODEL_SUGGESTIONS = {
  OpenAI: ['gpt-4.1-mini', 'gpt-4o-mini', 'o4-mini', 'gpt-4.1'],
  DeepSeek: ['deepseek-chat', 'deepseek-reasoner'],
  通义千问: ['qwen-plus', 'qwen-max', 'qwen-vl-max'],
  硅基流动: ['deepseek-ai/DeepSeek-V3', 'Qwen/Qwen2.5-72B-Instruct', 'BAAI/bge-m3'],
  '智谱 GLM': ['glm-4.5', 'glm-4v-plus'],
  '月之暗面 Kimi': ['moonshot-v1-8k', 'kimi-k2-0905-preview'],
  '火山方舟（豆包）': ['doubao-1.5-vision-pro', 'doubao-1.5-pro'],
  OpenRouter: ['openai/gpt-4o', 'anthropic/claude-3.7-sonnet', 'deepseek/deepseek-chat'],
  Claude: ['claude-sonnet-4-5', 'claude-3-5-haiku-latest'],
  Gemini: ['gemini-2.5-flash', 'gemini-2.5-pro'],
};

const LOCAL_HOST_RE = /^(localhost|127\.|0\.0\.0\.0|::1|\[::1\]|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|.*\.local)/i;

function statusTag(text, tone, glyph) {
  return el('span', { class: 'pg-tag', 'data-tone': tone },
    el('span', { class: 'pg-tag-glyph', 'aria-hidden': 'true' }, glyph),
    text);
}

function parseJson(value, fallback = null) {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'object') return value;
  try { return JSON.parse(value); } catch { return fallback; }
}

function isLocalUrl(value) {
  try {
    const url = new URL(String(value || '').trim());
    return url.protocol === 'http:' || LOCAL_HOST_RE.test(url.hostname);
  } catch {
    return false;
  }
}

function providerById(id) {
  return (state.providers || []).find(item => String(item.id) === String(id)) || null;
}

function providerKindLabel(kind) {
  return KIND_LABELS[kind] || kind || t('settings.unknownKind');
}

function authModeOf(provider) {
  return provider?.auth_mode || provider?.authMode || 'auto';
}

function compatOf(provider) {
  return parseJson(provider?.compat ?? provider?.compat_json, {}) || {};
}

function extraHeaderKeys(provider) {
  const raw = provider?.extra_headers ?? provider?.extraHeaders
    ?? provider?.extra_header_names ?? provider?.extra_header_keys;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw.map(item => (typeof item === 'string' ? item : item?.key)).filter(Boolean);
  return Object.keys(raw).filter(Boolean);
}

function providerHasSavedKey(provider) {
  if (!provider) return false;
  if (typeof provider.has_api_key === 'boolean') return provider.has_api_key;
  return Boolean(provider.key_hint && !/未配置|未保存/.test(provider.key_hint));
}

function selectionOf(def, task) {
  if (!def) return '';
  if (task === 'judge') {
    return def.judge_provider_id && def.judge_model
      ? JSON.stringify([def.judge_provider_id, def.judge_model]) : '';
  }
  return def.primary_provider_id && def.primary_model
    ? JSON.stringify([def.primary_provider_id, def.primary_model]) : '';
}

function verifySelectionOf(def) {
  return def?.verify_provider_id && def.verify_model
    ? JSON.stringify([def.verify_provider_id, def.verify_model]) : '';
}

function taskDefaultProviderText(task, def) {
  const isJudge = task === 'judge';
  const pid = isJudge ? def?.judge_provider_id : def?.primary_provider_id;
  const model = isJudge ? def?.judge_model : def?.primary_model;
  const vpid = def?.verify_provider_id;
  if (!model) {
    if (task === 'grade') return t('settings.followExplain');
    if (isJudge) return t('settings.judgeUnfixed');
    return t('settings.followPicker');
  }
  const provider = providerById(pid);
  if (!provider) return t('settings.providerDeleted');
  const parts = [provider.name];
  if (vpid && verifySelectionOf(def)) {
    const vProvider = providerById(vpid);
    parts.push(t('settings.verifyWith', { name: vProvider ? vProvider.name : t('settings.providerDeleted') }));
  }
  parts.push(def?.mode === 'accurate' ? t('settings.mode.accurate') : t('settings.mode.fast'));
  return parts.join(' · ');
}

function groupedOptions() {
  const { groups } = groupedModelOptions();
  return groups;
}

/** 费用展示：元（人民币），极小金额保留 4 位小数，避免全被舍入成 0。 */
function moneyText(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '—';
  if (numeric === 0) return '0.00';
  if (Math.abs(numeric) < 0.01) return numeric.toFixed(4);
  return numeric.toFixed(2);
}

/** 模型单价展示（元/百万 tokens）；任一单价缺失即视为未配置，不做 0 元假设。 */
function priceLabel(row) {
  const inputPrice = row?.input_price;
  const outputPrice = row?.output_price;
  if (inputPrice === null || inputPrice === undefined || outputPrice === null || outputPrice === undefined) {
    return t('tasks.priceUnset');
  }
  return t('settings.pricePerMillion', { input: moneyText(inputPrice), output: moneyText(outputPrice) });
}

/* ------------------------------------------------------------------------- *
 * 与后端通信的小工具：预览 / 探测接口需要拿到 HTTP 状态码做兼容回退，
 * 因此保留一个轻量的 raw 请求；其余保存等操作仍走统一 api()。
 * ------------------------------------------------------------------------- */
async function rawApi(path, { method = 'GET', data } = {}) {
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  };
  if (data !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(data);
  }
  const response = await fetch(`/api${path}`, init);
  let json = null;
  try { json = await response.json(); } catch { json = null; }
  if (!response.ok) {
    const error = new Error(errorText(json?.detail || json?.error || t('settings.wizard.requestFailed', { v1: response.status })));
    error.status = response.status;
    error.data = json;
    throw error;
  }
  return json;
}

function normalizeModelList(payload) {
  let raw = payload?.models || payload?.data || payload?.items;
  if ((!raw || !raw.length) && Array.isArray(payload?.model_ids)) raw = payload.model_ids;
  const seen = new Set();
  const models = [];
  for (const item of Array.isArray(raw) ? raw : []) {
    const id = typeof item === 'string' ? item : (item?.id || item?.model_id || item?.name);
    if (!id || typeof id !== 'string') continue;
    const clean = id.trim().replace(/^models\//, '');
    if (!clean || seen.has(clean) || clean.length > 160) continue;
    seen.add(clean);
    models.push({ id: clean, caps: item?.caps || item?.capabilities || {} });
  }
  return models;
}

function guessEndpoint(kind, baseUrl, apiVersion = '') {
  const base = String(baseUrl || '').trim().replace(/\/+$/, '');
  if (!base) return '';
  const lower = base.toLowerCase();
  if (lower.endsWith('/chat/completions') || lower.endsWith('/messages')
    || lower.endsWith('/responses') || lower.endsWith('/models')) return base;
  if (kind === 'anthropic') return `${base}/messages`;
  if (kind === 'gemini_native') return `${base}/models/{model}:generateContent`;
  if (kind === 'azure_openai') {
    return `${base}/chat/completions${apiVersion ? `?api-version=${apiVersion}` : ''}`;
  }
  return `${base}/chat/completions`;
}

function describeKindHint(kind) {
  return ({
    openai: t('settings.wizard.sendsAnOpenaiCompatibleChatCompletionsRequest'),
    anthropic: t('settings.wizard.sendsANativeClaudeMessagesRequest'),
    gemini: t('settings.wizard.requestsViaGeminiSOpenaiCompatibleLayer'),
    gemini_native: t('settings.wizard.sendsANativeGeminiGeneratecontentRequest'),
    azure_openai: t('settings.wizard.requestsViaTheAzureDeploymentUrlApi'),
  })[kind] || '';
}

/* ------------------------------------------------------------------------- *
 * 添加 / 编辑连接向导
 * ------------------------------------------------------------------------- */
function openProviderDialog(provider, ctx) {
  const editing = Boolean(provider?.id);
  const dlg = el('dialog', {
    class: 'provider-dialog',
    'data-mode': editing ? 'edit' : 'create',
    'aria-label': editing ? t('settings.wizard.editConnection', { v1: provider.name }) : t('settings.addCardTitle'),
  });

  const selectedModels = new Set(provider?.models || []);
  let availableModels = (provider?.model_details || []).map(item => ({
    id: item.model_id || item.id,
    caps: item.caps || {},
  })).filter(item => item.id);
  if (!availableModels.length) {
    availableModels = (provider?.models || []).map(id => ({ id, caps: {} }));
  }
  let resolvedEndpoint = '';
  let previewTimer = null;
  let previewSeq = 0;
  let savedHeaderKeys = extraHeaderKeys(provider);
  const removedHeaderKeys = new Set();
  const newHeaderRows = [];

  // ---- 基础字段
  const presetOptions = [];
  presetOptions.push({
    optgroup: t('settings.wizard.commonCustomLocal'),
    options: CUSTOM_PRESETS.map((item, index) => ({ value: `custom-${index}`, label: item.name })),
  });
  const officialPresets = state.presets || [];
  if (officialPresets.length) {
    presetOptions.push({
      optgroup: t('settings.wizard.officialVendorPresetsWithRecommendedModels'),
      options: officialPresets.map((item, index) => ({ value: `preset-${index}`, label: item.name })),
    });
  }
  const presetSel = select([
    { value: '', label: editing ? t('settings.wizard.keepCurrentConfiguration') : t('settings.wizard.selectAVendorPresetOptional') },
    ...presetOptions,
  ], '', { 'data-field': 'preset' });
  const presetHint = el('p', { class: 'help' });

  const nameIn = input(t('settings.wizard.eGMyDeepseek'), {
    value: provider?.name || '', 'data-field': 'name', required: true,
  });
  const kindSel = select(Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })),
    provider?.kind || 'openai', { 'data-field': 'kind' });
  const baseUrlIn = input('https://api.example.com/v1', {
    type: 'url', value: provider?.base_url || '', 'data-field': 'base-url', required: true,
  });
  const apiKeyIn = input(
    editing
      ? (providerHasSavedKey(provider) ? t('settings.wizard.leaveEmptyToReuseTheSavedKey') : t('settings.wizard.leaveEmptyToKeepTheCurrentState'))
      : t('settings.wizard.canBeLeftEmptyForLocalServices'),
    { type: 'password', autocomplete: 'off', 'data-field': 'api-key' },
  );
  const keyHint = el('p', { class: 'help' },
    editing && providerHasSavedKey(provider)
      ? t('settings.wizard.currentKeyHint', { v1: provider.key_hint || t('settings.wizard.encryptedSaved') })
      : t('settings.wizard.theKeyIsOnlySentToThe'));

  // ---- 地址规范化预览 + 模型列表（不截断）
  const previewMount = el('div', { class: 'provider-preview-mount' });
  const modelSearch = input(t('settings.wizard.searchModelsListsOf200'), {
    'data-field': 'model-search', type: 'search', 'aria-label': t('settings.wizard.searchModels'),
  });
  const selectAllBtn = el('button', { type: 'button', class: 'button secondary small', 'data-action': 'model-select-all' }, t('settings.wizard.selectAllCurrentResults'));
  const modelCount = el('span', { class: 'provider-model-count' }, t('settings.wizard.modelListNotFetchedYet'));
  const modelList = el('div', { class: 'provider-model-list', 'data-role': 'model-list' });
  const manualModels = el('textarea', {
    rows: 3,
    placeholder: t('settings.wizard.oneModelIdPerLineUsedFor'),
    'data-field': 'manual-models',
  });
  manualModels.value = [...selectedModels].join('\n');

  const syncModelsToTextarea = () => {
    manualModels.value = [...selectedModels].join('\n');
  };

  const manualModelsDetails = el('details', { class: 'pg-advanced', style: 'margin-top:8px' },
    el('summary', {}, t('settings.wizard.maintainModelIdsManuallyOnePerLine')),
    el('div', { class: 'pg-advanced-body' }, manualModels,
      el('p', { class: 'help' }, t('settings.wizard.theFullModelListReturnedByThe'))));
  const modelsArea = el('div', {},
    el('div', { class: 'provider-model-tools' }, modelSearch, selectAllBtn, modelCount),
    modelList,
    manualModelsDetails);

  function renderPreviewArea(previewNode = null) {
    previewMount.replaceChildren(...[previewNode, modelsArea].filter(Boolean));
  }

  function renderModelList() {
    const keyword = modelSearch.value.trim().toLowerCase();
    const filtered = keyword
      ? availableModels.filter(item => item.id.toLowerCase().includes(keyword))
      : availableModels;
    modelList.replaceChildren();
    if (!filtered.length) {
      modelList.append(el('p', { class: 'provider-model-empty' },
        availableModels.length ? t('settings.wizard.noMatchingModels') : t('settings.wizard.noModelListYetAfterFillingIn')));
    }
    for (const item of filtered) {
      const checkbox = el('input', {
        type: 'checkbox',
        'data-model-id': item.id,
        checked: selectedModels.has(item.id),
      });
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selectedModels.add(item.id);
        else selectedModels.delete(item.id);
        syncModelsToTextarea();
        updateModelCount();
      });
      modelList.append(el('label', { class: 'provider-model-item', title: item.id },
        checkbox, el('span', {}, item.id),
        item.caps?.vision ? badge(t('settings.wizard.vision'), 'teal') : null,
        item.caps?.reasoning ? badge(t('settings.wizard.reasoning'), 'blue') : null));
    }
    updateModelCount();
  }

  function updateModelCount() {
    modelCount.textContent = availableModels.length
      ? t('settings.wizard.modelsSelected', { v1: availableModels.length, v2: selectedModels.size })
      : t('settings.wizard.modelsSelected_2', { v1: selectedModels.size });
  }

  modelSearch.addEventListener('input', renderModelList);
  selectAllBtn.addEventListener('click', () => {
    const keyword = modelSearch.value.trim().toLowerCase();
    const filtered = keyword
      ? availableModels.filter(item => item.id.toLowerCase().includes(keyword))
      : availableModels;
    const allSelected = filtered.length > 0 && filtered.every(item => selectedModels.has(item.id));
    for (const item of filtered) {
      if (allSelected) selectedModels.delete(item.id);
      else selectedModels.add(item.id);
    }
    renderModelList();
    syncModelsToTextarea();
  });
  manualModels.addEventListener('input', () => {
    selectedModels.clear();
    for (const item of manualModels.value.split(/[\n,，]/).map(text => text.trim()).filter(Boolean)) {
      selectedModels.add(item);
    }
    updateModelCount();
  });

  // ---- 高级连接选项
  const authModeSel = select(
    Object.entries(AUTH_MODE_LABELS).map(([value, label]) => ({ value, label })),
    authModeOf(provider), { 'data-field': 'auth-mode' });
  const headersMount = el('div');
  const headerRowsMount = el('div', { class: 'provider-header-rows' });
  const headerReplaceHint = el('p', { class: 'help' });
  const addHeaderBtn = el('button', { type: 'button', class: 'button secondary small', 'data-action': 'add-header' }, t('settings.wizard.addHeader'));

  const compat = compatOf(provider);
  const maxTokensSel = select([
    { value: 'auto', label: t('settings.wizard.autoRecommended') },
    { value: 'max_tokens', label: t('settings.wizard.alwaysUseMaxTokens') },
    { value: 'max_completion_tokens', label: t('settings.wizard.alwaysUseMaxCompletionTokens') },
  ], compat.max_tokens_param || 'auto', { 'data-field': 'max-tokens-param' });
  const timeoutIn = input('120', {
    type: 'number', min: 15, max: 600, step: 5,
    value: compat.timeout_seconds ?? 120, 'data-field': 'timeout',
  });
  const apiVersionIn = input(t('settings.wizard.eG20241021'), {
    value: compat.api_version || '', 'data-field': 'api-version',
  });
  const proxyChk = el('input', {
    type: 'checkbox', checked: Boolean(compat.use_proxy_env), 'data-field': 'use-proxy',
  });
  const proxyRow = el('label', { class: 'check-label', style: 'margin-top:6px' },
    proxyChk, t('settings.wizard.allowThisConnectionToUseTheSystem'));
  const proxyHint = el('p', { class: 'help' },
    ctx.localMode === false
      ? t('settings.wizard.onlyTakesEffectInLocalModeIgnored')
      : t('settings.wizard.onlyTakesEffectInLocalModeEnable'));

  function buildHeaderRow() {
    const keyInput = input(t('settings.wizard.eGHttpReferer'), { 'data-field': 'header-key' });
    const valueInput = input(t('settings.wizard.headerValueOnlyTheNameIsShown'), { 'data-field': 'header-value' });
    const removeBtn = el('button', { type: 'button', class: 'button ghost small', 'aria-label': t('settings.wizard.deleteThisHeader') }, t('common.delete'));
    const row = el('div', { class: 'provider-header-row' }, keyInput, valueInput, removeBtn);
    removeBtn.addEventListener('click', () => {
      row.remove();
      const index = newHeaderRows.indexOf(row);
      if (index >= 0) newHeaderRows.splice(index, 1);
      updateAddHeaderState();
    });
    newHeaderRows.push(row);
    return row;
  }

  function updateAddHeaderState() {
    const total = savedHeaderKeys.length + newHeaderRows.length;
    addHeaderBtn.disabled = total >= MAX_EXTRA_HEADERS;
    addHeaderBtn.title = total >= MAX_EXTRA_HEADERS ? t('settings.wizard.upToCustomHeaders', { v1: MAX_EXTRA_HEADERS }) : '';
    updateHeaderReplaceHint();
  }

  function updateHeaderReplaceHint() {
    const editingSaved = Boolean(provider?.id) && savedHeaderKeys.length > 0;
    if (!editingSaved) { headerReplaceHint.textContent = ''; return; }
    headerReplaceHint.textContent = removedHeaderKeys.size || newHeaderRows.length
      ? t('settings.wizard.noteSavedHeaderValuesCannotBeShown')
      : t('settings.wizard.savedHeaderValuesAreMaskedAndOnly');
  }

  function renderSavedHeaders() {
    headersMount.replaceChildren();
    const kept = savedHeaderKeys.filter(key => !removedHeaderKeys.has(key));
    if (!kept.length) { updateHeaderReplaceHint(); return; }
    headersMount.append(el('p', { class: 'help' }, t('settings.wizard.savedHeadersMaskedNamesOnly')));
    const tags = el('div', { class: 'provider-header-saved' });
    for (const key of kept) {
      tags.append(el('span', { class: 'badge blue' }, key,
        el('button', {
          type: 'button', 'aria-label': t('settings.wizard.deleteSavedHeader', { v1: key }),
          onClick: () => { removedHeaderKeys.add(key); renderSavedHeaders(); updateAddHeaderState(); },
        }, '×')));
    }
    headersMount.append(tags);
    updateHeaderReplaceHint();
  }

  function collectExtraHeaders() {
    const headers = {};
    for (const row of newHeaderRows) {
      const key = row.querySelector('[data-field="header-key"]').value.trim();
      const value = row.querySelector('[data-field="header-value"]').value.trim();
      if (key) headers[key] = value;
    }
    return headers;
  }

  addHeaderBtn.addEventListener('click', () => {
    if (savedHeaderKeys.length + newHeaderRows.length >= MAX_EXTRA_HEADERS) return;
    headerRowsMount.append(buildHeaderRow());
    updateAddHeaderState();
  });

  // ---- 保存前探测结果
  const probeResult = el('div', { class: 'provider-probe-result', 'aria-live': 'polite' });
  const dialogError = el('div', { class: 'provider-dialog-error' });

  function showError(message) {
    dialogError.replaceChildren(notice(message, 'error'));
    dialogError.scrollIntoView({ block: 'nearest' });
  }

  function clearError() {
    dialogError.replaceChildren();
  }

  function pickModelForProbe() {
    const selected = [...selectedModels];
    if (selected.length) return selected[0];
    if (availableModels.length) return availableModels[0].id;
    return '';
  }

  function draftPayload({ includeModel = false } = {}) {
    const payload = {
      kind: kindSel.value,
      base_url: baseUrlIn.value.trim(),
      auth_mode: authModeSel.value,
    };
    if (provider?.id) payload.provider_id = provider.id;
    const key = apiKeyIn.value.trim();
    if (key) payload.api_key = key;
    const headers = collectExtraHeaders();
    if (Object.keys(headers).length) payload.extra_headers = headers;
    if (removedHeaderKeys.size) payload.extra_headers_remove = [...removedHeaderKeys];
    payload.compat = {
      max_tokens_param: maxTokensSel.value,
      timeout_seconds: Number(timeoutIn.value) || 120,
      use_proxy_env: proxyChk.checked,
    };
    if (apiVersionIn.value.trim()) payload.compat.api_version = apiVersionIn.value.trim();
    if (includeModel) payload.model = pickModelForProbe();
    return payload;
  }

  function applyPreviewPayload(res) {
    resolvedEndpoint = res?.resolved_endpoint || res?.endpoint || '';
    const models = normalizeModelList(res);
    if (models.length) {
      // 合并已选模型：厂商返回的列表保留当前勾选状态。
      const byId = new Map(availableModels.map(item => [item.id, item]));
      for (const item of models) byId.set(item.id, item);
      availableModels = [...byId.values()];
    }
    const normalized = res?.normalized_base_url || '';
    // 预览接口的 resolved_endpoint 是「拉模型列表」的端点；界面要展示的是
    // 实际对话请求端点（POST .../chat/completions），两者分别提示。
    const chatEndpoint = guessEndpoint(
      kindSel.value,
      normalized || baseUrlIn.value.trim(),
      res?.compat?.api_version || apiVersionIn.value.trim(),
    );
    const listEndpoint = resolvedEndpoint || '';
    const preview = el('div', { class: 'provider-preview' });
    if (chatEndpoint) {
      preview.append(el('p', { class: 'provider-preview-endpoint' },
        el('span', { class: 'provider-preview-method' }, 'POST'),
        el('code', {}, chatEndpoint)));
    }
    const notes = [];
    if (Array.isArray(res?.notes) && res.notes.length) notes.push(...res.notes.filter(Boolean));
    if (res?.notice) notes.push(res.notice);
    if (res?.ok === false && res?.diagnosis) notes.push(t('settings.wizard.vendorConnectionFailed', { v1: res.diagnosis }));
    else if (res?.diagnosis && res?.ok === true) notes.push(res.diagnosis);
    if (listEndpoint && listEndpoint !== chatEndpoint) {
      notes.push(t('settings.wizard.requestedTheModelListFromTheVendor', { v1: listEndpoint }));
    }
    const rawUrl = baseUrlIn.value.trim();
    const hasNormalizeNote = () => notes.some(item => /清洗|规范化|剥离/.test(item));
    if (normalized && normalized.replace(/\/+$/, '') !== rawUrl.replace(/\/+$/, '') && !hasNormalizeNote()) {
      notes.push(t('settings.wizard.urlNormalizedToAutomaticallyThisAddressIs', { v1: normalized }));
    }
    if (/\/(chat\/completions|messages|responses|models)\/?$/i.test(rawUrl) && !hasNormalizeNote()) {
      notes.push(t('settings.wizard.aPastedEndpointTailPathWasDetected'));
    }
    if (res?.capabilities && typeof res.capabilities === 'object') {
      const caps = res.capabilities;
      const values = Object.values(caps);
      const isMap = values.length > 0 && values.every(value => value && typeof value === 'object');
      if (isMap) {
        const visionCount = values.filter(value => value.vision).length;
        if (visionCount) notes.push(t('settings.wizard.capabilityInferenceModelsSupportVisionInput', { v1: visionCount }));
      } else {
        const pieces = [
          caps.stream === false ? t('settings.wizard.noStreaming') : null,
          caps.json === false ? t('settings.wizard.noJsonOutput') : null,
          caps.vision ? t('settings.wizard.visionSupported') : null,
        ].filter(Boolean);
        if (pieces.length) notes.push(t('settings.wizard.capabilityInference', { v1: pieces.join(' · ') }));
      }
    }
    if (!models.length && res?.ok === false) {
      preview.append(notice(t('settings.wizard.fetchModelsFailed', { v1: res?.diagnosis || t('settings.wizard.vendorReturnedNoModels') }), 'warning'));
    }
    if (notes.length) {
      preview.append(el('p', { class: 'provider-preview-note' }, notes.join('；')));
    }
    if (!chatEndpoint && !notes.length) {
      preview.append(el('p', { class: 'help' }, t('settings.wizard.afterEnteringTheApiUrlTheActual')));
    }
    renderPreviewArea(preview);
    renderModelList();
  }

  async function runPreview({ immediate = false } = {}) {
    clearTimeout(previewTimer);
    const url = baseUrlIn.value.trim();
    if (url.length < 8) {
      previewMount.replaceChildren();
      return;
    }
    const run = async () => {
      const seq = ++previewSeq;
      previewMount.replaceChildren(loading(t('settings.wizard.generatingTheNormalizedPreviewAndFetchingVendor')));
      try {
        const payload = draftPayload();
        const res = await rawApi('/providers/fetch-models-preview', { method: 'POST', data: payload });
        if (seq !== previewSeq) return;
        applyPreviewPayload(res);
      } catch (error) {
        if (seq !== previewSeq) return;
        if (error.status === 404 || error.status === 405) {
          // 旧后端兼容：编辑已有连接时退回旧的 /fetch-models（用已保存的 Key）。
          if (provider?.id) {
            try {
              const legacy = await rawApi(`/providers/${provider.id}/fetch-models`, { method: 'POST' });
              if (seq !== previewSeq) return;
              applyPreviewPayload({
                resolved_endpoint: guessEndpoint(kindSel.value, baseUrlIn.value.trim()),
                models: normalizeModelList(legacy),
                notice: legacy?.notice,
              });
              return;
            } catch { /* 继续展示提示 */ }
          }
          previewMount.replaceChildren(
            notice(t('settings.wizard.theBackendDoesNotProvideThePre'), 'warning'),
            modelsArea);
          updateModelCount();
          return;
        }
        previewMount.replaceChildren(notice(t('settings.wizard.normalizedPreviewFailed', { v1: error.message }), 'error'));
      }
    };
    if (immediate) await run();
    else previewTimer = setTimeout(run, 500);
  }

  baseUrlIn.addEventListener('input', () => runPreview());
  kindSel.addEventListener('change', () => runPreview());

  presetSel.addEventListener('change', () => {
    const value = presetSel.value;
    if (!value) return;
    let preset = null;
    if (value.startsWith('custom-')) preset = CUSTOM_PRESETS[Number(value.slice('custom-'.length))];
    else if (value.startsWith('preset-')) {
      const raw = (state.presets || [])[Number(value.slice('preset-'.length))];
      if (raw) {
        preset = {
          name: raw.name,
          kind: raw.kind,
          base_url: raw.base_url,
          models: PRESET_MODEL_SUGGESTIONS[raw.name] || raw.models || [],
        };
      }
    }
    if (!preset) return;
    nameIn.value = editing ? nameIn.value || preset.name : preset.name;
    if (preset.kind) kindSel.value = preset.kind;
    if (preset.base_url) baseUrlIn.value = preset.base_url;
    if (preset.auth_mode) authModeSel.value = preset.auth_mode;
    if (preset.compat?.api_version) apiVersionIn.value = preset.compat.api_version;
    if (!editing && preset.models?.length) {
      selectedModels.clear();
      for (const model of preset.models) selectedModels.add(model);
      availableModels = preset.models.map(id => ({ id, caps: {} }));
      syncModelsToTextarea();
      updateModelCount();
    }
    if (preset.hint) presetHint.textContent = preset.hint;
    if (preset.base_url && preset.base_url !== 'https://') runPreview({ immediate: true });
  });

  // ---- 底部流程按钮
  const fetchBtn = el('button', { type: 'button', class: 'button secondary', 'data-action': 'fetch-models' },
    icon('search'), t('settings.wizard.fetchVendorModels'));
  fetchBtn.addEventListener('click', () => {
    if (!baseUrlIn.value.trim()) { showError(t('settings.wizard.pleaseFillInTheApiUrlFirst')); return; }
    clearError();
    runPreview({ immediate: true });
  });

  const probeBtn = el('button', { type: 'button', class: 'button secondary', 'data-action': 'probe' },
    icon('check'), t('settings.wizard.testConnectionBeforeSaving'));
  probeBtn.addEventListener('click', () => busy(probeBtn, async () => {
    clearError();
    const model = pickModelForProbe();
    if (!model) { showError(t('settings.wizard.fetchAndCheckAtLeastOneModel')); return; }
    if (!baseUrlIn.value.trim()) { showError(t('settings.wizard.pleaseFillInTheApiUrlFirst')); return; }
    probeResult.replaceChildren(loading(t('settings.wizard.probingConnectivityWithAMinimalRequest')));
    try {
      const res = await rawApi('/providers/probe', { method: 'POST', data: draftPayload({ includeModel: true }) });
      if (res.ok === false) {
        probeResult.replaceChildren(notice(t('settings.wizard.connectionFailedDetail', { v1: res.diagnosis || t('settings.wizard.vendorReturnedNoResponse') }), 'error'));
        return;
      }
      const latency = Number(res.latency_ms);
      const parts = [
        t('settings.wizard.connectionOk') + (Number.isFinite(latency) ? t('settings.wizard.firstByteLatency', { v1: Math.round(latency) }) : ''),
        res.model ? t('settings.embeddingModelLine', { model: res.model }) : t('settings.embeddingModelLine', { model: model }),
        res.diagnosis ? t('settings.wizard.diagnosis', { v1: res.diagnosis }) : '',
        res.request_url ? t('settings.wizard.request', { v1: res.request_url }) : '',
      ].filter(Boolean);
      probeResult.replaceChildren(notice(parts.join('；')));
    } catch (error) {
      if ((error.status === 404 || error.status === 405) && provider?.id) {
        // 旧后端兼容：编辑已有连接时退回旧的已保存连接测试。
        try {
          const legacy = await rawApi(`/providers/${provider.id}/test`, {
            method: 'POST', data: { model },
          });
          probeResult.replaceChildren(notice(t('settings.wizard.connectionOkLegacy', { v1: legacy.content || t('settings.wizard.modelResponded') }), 'warning'));
          return;
        } catch (legacyError) {
          probeResult.replaceChildren(notice(t('settings.wizard.testFailed', { v1: legacyError.message }), 'error'));
          return;
        }
      }
      probeResult.replaceChildren(notice(t('settings.wizard.testFailed', { v1: error.message }), 'error'));
    }
  }));

  const saveBtn = el('button', { type: 'button', class: 'button', 'data-action': 'save' },
    icon('check'), editing ? t('settings.wizard.saveChanges') : t('settings.wizard.saveConnection'));
  saveBtn.addEventListener('click', () => busy(saveBtn, async () => {
    clearError();
    const name = nameIn.value.trim();
    const baseUrl = baseUrlIn.value.trim();
    const models = [...selectedModels];
    if (!name) { showError(t('settings.wizard.pleaseFillInTheConfigurationName')); return; }
    if (!baseUrl || baseUrl.length < 8) { showError(t('settings.wizard.pleaseFillInAValidApiUrl')); return; }
    if (!models.length) { showError(t('settings.wizard.pleaseCheckOrEnterAtLeastOne')); return; }
    const data = {
      name,
      kind: kindSel.value,
      base_url: baseUrl,
      models,
      auth_mode: authModeSel.value,
      compat: {
        max_tokens_param: maxTokensSel.value,
        timeout_seconds: Number(timeoutIn.value) || 120,
        use_proxy_env: proxyChk.checked,
      },
    };
    if (apiVersionIn.value.trim()) data.compat.api_version = apiVersionIn.value.trim();
    const key = apiKeyIn.value.trim();
    if (key) data.api_key = key;
    // api_key 可以留空，但后端要求显式 auth_mode='none'（本地 Ollama / LM Studio 等）。
    if (!key) {
      const savedKeyExists = editing && providerHasSavedKey(provider);
      if (!savedKeyExists && data.auth_mode === 'auto') {
        if (isLocalUrl(baseUrl)) {
          data.auth_mode = 'none';
        } else {
          showError(t('settings.wizard.noApiKeyEnteredForLocalServices'));
          return;
        }
      }
    }
    const headers = collectExtraHeaders();
    const headersChanged = newHeaderRows.length > 0 || removedHeaderKeys.size > 0;
    if (!editing) {
      if (Object.keys(headers).length) data.extra_headers = headers;
    } else if (headersChanged && (!savedHeaderKeys.length || removedHeaderKeys.size || newHeaderRows.length)) {
      // PATCH 的 extra_headers 是整体替换语义：只有用户确实改动了请求头才发送，
      // 未改动时省略以保留服务端已加密保存的值。
      data.extra_headers = headers;
    }
    try {
      const saved = editing
        ? await api(`/providers/${provider.id}`, { method: 'PATCH', data })
        : await api('/providers', { method: 'POST', data });
      toast(editing ? t('settings.wizard.connectionConfigurationUpdated') : t('settings.wizard.modelConnectionSavedEncrypted'));
      dlg.close();
      await ctx.afterProviderSaved(saved);
    } catch (error) {
      // 保存失败就地显示，不关闭弹窗，避免用户重填。
      showError(t('settings.embeddingSaveFailed', { message: error.message }));
    }
  }));

  const cancelBtn = el('button', { type: 'button', class: 'button ghost', onClick: () => dlg.close() }, t('common.cancel'));

  // 初始渲染
  renderSavedHeaders();
  updateAddHeaderState();
  renderModelList();
  syncModelsToTextarea();
  renderPreviewArea();

  const advancedBody = el('div', { class: 'pg-advanced-body' },
    el('div', { class: 'form-grid' },
      field(t('settings.wizard.authMode'), authModeSel, t('settings.wizard.autoByDefaultChoosesBearerXApi')),
      field(t('settings.wizard.timeoutSeconds15600'), timeoutIn, t('settings.wizard.increaseForSlowRelaysMaximum600Seconds'))),
    el('div', { class: 'form-grid' },
      field(t('settings.wizard.maxTokensCompatibility'), maxTokensSel, t('settings.wizard.switchExplicitlyWhenModelsSuchAsO1')),
      field(t('settings.wizard.apiVersionAzureEtc'), apiVersionIn, t('settings.wizard.leaveEmptyToLetTheBackendDecide'))),
    el('div', {},
      el('p', { class: 'field-label' }, t('settings.wizard.customHeadersUpToOnlyNamesAre', { v1: MAX_EXTRA_HEADERS })),
      headersMount, headerRowsMount, headerReplaceHint,
      el('div', { class: 'row', style: 'margin-top:8px' }, addHeaderBtn)),
    proxyRow, proxyHint);

  const advanced = el('details', { class: 'pg-advanced provider-advanced' },
    el('summary', {}, t('settings.wizard.advancedConnectionOptionsAuthHeadersTimeoutProxy')),
    advancedBody);

  dlg.append(
    el('div', { class: 'row between', style: 'margin-bottom:14px' },
      el('h3', {}, icon('key'), editing ? t('settings.wizard.editConnection_2', { v1: provider.name }) : t('settings.addCardTitle')),
      el('button', { type: 'button', class: 'button ghost small', onClick: () => dlg.close(), 'aria-label': t('common.close') }, t('common.close'))),
    el('div', { class: 'provider-dialog-body' },
      el('div', { class: 'form-grid' },
        field(t('settings.wizard.vendorPreset'), presetSel, t('settings.wizard.theTopGroupContainsCommonCustomAnd')),
        field(t('settings.wizard.configurationName'), nameIn)),
      presetHint,
      el('div', { class: 'form-grid' },
        field(t('settings.wizard.apiType'), kindSel, describeKindHint(kindSel.value)),
        field(t('settings.wizard.apiUrl'), baseUrlIn, t('settings.wizard.pasteTheVendorSDocumentedUrlDirectly'))),
      previewMount,
      el('div', { class: 'form-grid' },
        field(editing ? t('settings.wizard.replaceApiKeyOptional') : t('settings.wizard.apiKeyOptionalForLocal'), apiKeyIn), keyHint),
      advanced,
      probeResult,
      dialogError),
    el('div', { class: 'provider-dialog-actions' },
      el('div', { class: 'row' }, fetchBtn, probeBtn),
      el('div', { class: 'row' }, cancelBtn, saveBtn))
  );

  document.body.append(dlg);
  dlg.addEventListener('close', () => {
    clearTimeout(previewTimer);
    dlg.remove();
  });
  dlg.showModal();

  // 编辑已有连接时先用「已存 Key + 当前表单地址」生成一次预览（未改 Key 只传 provider_id）。
  if (editing && baseUrlIn.value.trim().length >= 8) runPreview();
  else if (!editing && baseUrlIn.value.trim().length >= 8) runPreview();
}

/* ------------------------------------------------------------------------- *
 * 模型能力与单价（局部更新，不整页重渲染）
 * ------------------------------------------------------------------------- */
function openModelMetaDialog(provider, detail, ctx) {
  if (!detail?.id) { toast(t('settings.wizard.saveTheConnectionBeforeEditingModelAttributes'), true); return; }
  const dlg = el('dialog', { class: 'modal-dialog', 'data-role': 'model-meta' });
  const displayName = input(t('settings.wizard.displayName'), { value: detail.display_name || detail.model_id });
  const vChk = el('input', { type: 'checkbox', checked: Boolean(detail.caps?.vision) });
  const jChk = el('input', { type: 'checkbox', checked: detail.caps?.json !== false });
  const rChk = el('input', { type: 'checkbox', checked: Boolean(detail.caps?.reasoning) });
  const sChk = el('input', { type: 'checkbox', checked: detail.caps?.stream !== false });
  const enabledChk = el('input', { type: 'checkbox', checked: detail.enabled !== false });
  const ctxIn = input(t('settings.wizard.eG128000'), {
    type: 'number', min: 0, step: 1000, value: detail.caps?.context ?? '',
  });
  const inPrice = input(t('settings.wizard.eG20CnyPerMillion'), {
    type: 'number', step: '0.01', min: 0, value: detail.input_price ?? '',
  });
  const outPrice = input(t('settings.wizard.eG80CnyPerMillion'), {
    type: 'number', step: '0.01', min: 0, value: detail.output_price ?? '',
  });

  dlg.append(
    el('div', { class: 'card-header' }, el('h3', {}, t('settings.wizard.modelCapabilitiesPrices', { v1: detail.model_id })),
      button(t('common.close'), () => dlg.close(), 'ghost small', 'close')),
    field(t('settings.wizard.displayName'), displayName),
    el('div', { class: 'row', style: 'margin:14px 0' },
      el('label', { class: 'check-label' }, vChk, t('settings.wizard.visionSupportPhotoOcr')),
      el('label', { class: 'check-label' }, jChk, t('settings.wizard.structuredJsonSupport')),
      el('label', { class: 'check-label' }, rChk, t('settings.wizard.reasoningModel')),
      el('label', { class: 'check-label' }, sChk, t('settings.wizard.streamingOutput')),
      el('label', { class: 'check-label' }, enabledChk, t('settings.wizard.enableInTheModelPicker'))),
    el('div', { class: 'form-grid' },
      field(t('settings.wizard.contextLengthTokensOptional'), ctxIn),
      field(t('settings.wizard.inputPriceCnyPer1mTokensOptional'), inPrice),
      field(t('settings.wizard.outputPriceCnyPer1mTokensOptional'), outPrice)),
    el('div', { class: 'row end', style: 'margin-top:16px' },
      button(t('settings.wizard.saveAttributes'), e => busy(e.currentTarget, async () => {
        const context = ctxIn.value !== '' ? Number(ctxIn.value) : undefined;
        const res = await api(`/models/${detail.id}`, {
          method: 'PATCH',
          data: {
            display_name: displayName.value.trim() || detail.model_id,
            enabled: enabledChk.checked,
            caps: {
              ...detail.caps,
              vision: vChk.checked,
              json: jChk.checked,
              reasoning: rChk.checked,
              stream: sChk.checked,
              ...(context ? { context } : {}),
            },
            input_price: inPrice.value !== '' ? Number(inPrice.value) : null,
            output_price: outPrice.value !== '' ? Number(outPrice.value) : null,
          },
        });
        // 局部同步：只更新当前 provider 的模型行与卡片，不调用 renderRoute()。
        const target = (state.providers || []).find(item => String(item.id) === String(provider.id));
        const row = target?.model_details?.find(item => String(item.model_id) === String(detail.model_id));
        if (row) {
          Object.assign(row, {
            display_name: res.display_name ?? row.display_name,
            caps: res.caps || row.caps,
            enabled: res.enabled ?? row.enabled,
            input_price: res.input_price ?? row.input_price,
            output_price: res.output_price ?? row.output_price,
          });
        }
        if (target && res.enabled === false && Array.isArray(target.models)) {
          // 保持连接模型列表与详情一致（禁用只影响选择器，不删除模型）。
        }
        toast(t('settings.wizard.modelCapabilityTagsAndPricesUpdated'));
        dlg.close();
        ctx.renderProviders();
      }), '', 'check'))
  );
  document.body.append(dlg);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.showModal();
}

/* ------------------------------------------------------------------------- *
 * 页面主体
 * ------------------------------------------------------------------------- */
export async function renderSettings(container, version) {
  const [, prefs, usage, live, usageToday] = await Promise.all([
    refreshProviders(),
    ensurePreferences(true),
    api('/ai/usage?days=30').catch(() => ({ by_model: [], by_day: [], notice: '' })),
    api('/health/live').catch(() => null),
    api('/ai/usage/me?days=1').catch(() => null),
  ]);
  if (!current(version)) return;

  // 本地模式判定：后端 /health/live 提供 public_mode / url_policy 时按它显示，
  // 缺字段时仍展示代理开关并明确「仅本地模式生效」，不猜测部署形态。
  let localMode = null;
  if (typeof state.publicMode === 'boolean') localMode = !state.publicMode;
  else if (typeof state.urlPolicy === 'string') {
    localMode = state.urlPolicy === 'local_open' || state.urlPolicy === 'local';
  } else if (live && typeof live.public_mode === 'boolean') localMode = !live.public_mode;
  else if (live && typeof live.url_policy === 'string') {
    localMode = live.url_policy === 'local_open' || live.url_policy === 'local';
  }

  const ctx = {
    localMode,
    providersGrid: el('div', { class: 'grid-three pg-provider-grid' }),
    apiStatusCard: null,
    afterProviderSaved: async () => {},
    renderProviders: () => {},
    renderApiStatus: () => {},
  };

  /* ---------- 1. 顶部「API 状态与默认模型」（行内可编辑） ---------- */
  async function saveTaskDefault(task, primaryValue, verifyValue, { clear = false } = {}) {
    const payload = {
      task,
      primary_provider_id: null,
      primary_model: null,
      verify_provider_id: null,
      verify_model: null,
      judge_provider_id: null,
      judge_model: null,
      mode: 'fast',
    };
    if (!clear) {
      if (task === 'judge') {
        if (primaryValue) {
          const [pid, mid] = JSON.parse(primaryValue);
          payload.judge_provider_id = pid;
          payload.judge_model = mid;
          payload.mode = 'accurate';
        }
      } else if (primaryValue) {
        const [pid, mid] = JSON.parse(primaryValue);
        payload.primary_provider_id = pid;
        payload.primary_model = mid;
        if (verifyValue) {
          const [vpid, vmid] = JSON.parse(verifyValue);
          payload.verify_provider_id = vpid;
          payload.verify_model = vmid;
          payload.mode = 'accurate';
        }
      }
    }
    try {
      const res = await api('/ai/task-defaults', { method: 'PUT', data: payload });
      state.taskDefaults = state.taskDefaults || {};
      const merged = Object.assign({}, payload, res || {});
      state.taskDefaults[task] = merged;
      if (!merged.primary_model && !merged.judge_model) {
        // 清空时后端返回 null 字段：保留一条空记录即可，行内展示会回退到默认文案。
      }
      ctx.renderApiStatus();
      window.dispatchEvent(new CustomEvent('zhixu:task-default-changed', { detail: { scope: task } }));
    } catch (error) {
      toast(t('settings.taskDefaultSaveFailed', { message: error.message }), true);
    }
  }

  function buildTaskRow(task) {
    const def = state.taskDefaults?.[task] || {};
    const isJudge = task === 'judge';
    const groups = groupedOptions();
    const primaryOptions = [
      {
        value: '',
        label: isJudge
          ? t('settings.judgeUnfixed')
          : task === 'grade' ? t('settings.followExplain') : t('settings.followPicker'),
      },
      ...groups,
    ];
    const primaryVal = selectionOf(def, task);
    const verifyVal = verifySelectionOf(def);
    const primary = select(primaryOptions, primaryVal, {
      'data-field': 'task-primary', 'data-task': task, 'aria-label': t('settings.primaryAria', { task: TASK_LABELS[task] }),
    });
    const verify = select([{ value: '', label: t('settings.verifyNone') }, ...groups], verifyVal, {
      'data-field': 'task-verify', 'data-task': task, 'aria-label': t('settings.verifyAria', { task: TASK_LABELS[task] }),
    });

    const providerText = el('span', {
      class: 'pg-default-provider pg-task-provider',
      'data-task-provider': task,
      'data-empty': def.primary_provider_id || def.judge_provider_id ? 'false' : 'true',
    }, taskDefaultProviderText(task, def));

    const clearBtn = el('button', {
      type: 'button', class: 'button ghost small pg-task-clear', 'data-action': 'clear-task', 'data-task': task,
      'aria-label': t('settings.clearAria', { task: TASK_LABELS[task] }),
    }, t('settings.clear'));
    clearBtn.addEventListener('click', () => busy(clearBtn, async () => {
      await saveTaskDefault(task, '', '', { clear: true });
    }));

    primary.addEventListener('change', () => saveTaskDefault(task, primary.value, verify.value));
    if (!isJudge) {
      verify.addEventListener('change', () => saveTaskDefault(task, primary.value, verify.value));
    }

    const primaryField = el('div', { class: 'pg-task-cell-primary' }, primary);
    const verifyField = isJudge
      ? el('div', { class: 'pg-task-cell-verify pg-task-cell-judge' },
        el('span', { class: 'help' }, t('settings.judgeNotApplicable')))
      : el('div', { class: 'pg-task-cell-verify' }, verify);
    const nameCell = el('div', { class: 'pg-task-cell-name' },
      el('span', { class: 'pg-default-task' }, TASK_LABELS[task]),
      TASK_HINTS[task] ? el('span', { class: 'pg-task-hint' }, TASK_HINTS[task]) : null);
    const providerCell = el('div', { class: 'pg-task-cell-provider' }, providerText);
    const clearCell = el('div', { class: 'pg-task-cell-clear' }, clearBtn);

    return el('li', { class: 'pg-task-row', 'data-task-row': task },
      nameCell, primaryField, verifyField, providerCell, clearCell);
  }

  function buildApiStatusBody() {
    const connectedCount = state.providers.length;
    const modelCount = state.providers.reduce((sum, provider) => sum + (provider.models || []).length, 0);
    return [
      el('div', { class: 'pg-api-status-head' },
        el('div', {},
          el('p', { class: 'pg-kicker' }, t('settings.apiStatusKicker')),
          el('h2', {}, connectedCount ? t('settings.connectedCount', { count: connectedCount }) : t('settings.connectFirst')),
          el('p', { class: 'pg-api-summary' }, connectedCount
            ? t('settings.apiSummary', { count: modelCount })
            : t('settings.apiSummaryEmpty'))),
        statusTag(connectedCount ? t('settings.statusAvailable') : t('settings.statusTodo'), connectedCount ? 'ok' : 'warn', connectedCount ? '✓' : '!')),
      el('div', { class: 'pg-api-actions' },
        button(connectedCount ? t('settings.addAnother') : t('settings.addConnection'),
          () => openProviderDialog(null, ctx), 'secondary', 'plus'),
        connectedCount
          ? button(t('settings.testExisting'), () => ctx.providersGrid.scrollIntoView({ behavior: 'smooth', block: 'start' }), 'ghost', 'check')
          : null,
        el('button', { type: 'button', class: 'button ghost', onClick: () => openEmbeddingCard(ctx) }, t('settings.configureSearch'))),
      el('div', { class: 'pg-task-defaults-head' },
        el('h3', {}, t('settings.taskDefaultsTitle')),
        el('p', { class: 'help' }, t('settings.taskDefaultsHelp'))),
      el('ul', { class: 'pg-default-models pg-task-defaults' },
        ...TASK_ORDER.map(task => buildTaskRow(task))),
    ];
  }

  ctx.renderApiStatus = () => {
    if (!ctx.apiStatusCard) return;
    ctx.apiStatusCard.replaceChildren(...buildApiStatusBody());
  };

  const apiStatusCard = el('section', { class: 'card pg-api-status' });
  ctx.apiStatusCard = apiStatusCard;
  ctx.renderApiStatus();

  /* ---------- 2. 连接卡片 ---------- */
  function buildProviderCard(provider) {
    const modelSel = select((provider.models || []).map(m => ({ value: m, label: m })), '', {
      'aria-label': t('settings.testModelAria'),
    });
    const testing = el('div');
    const modelBadges = el('div', { class: 'provider-models' });
    for (const detail of provider.model_details || []) {
      const icons = [detail.caps?.vision ? '👁' : '', detail.caps?.reasoning ? '🧠' : ''].filter(Boolean).join('');
      const label = `${detail.model_id}${icons ? ' ' + icons : ''}${detail.enabled === false ? t('settings.modelDisabled') : ''}`;
      if (!detail.id) {
        modelBadges.append(el('span', { class: 'badge blue', title: t('settings.modelBadgeTitle') }, label));
        continue;
      }
      modelBadges.append(el('button', {
        type: 'button',
        class: 'badge blue clickable-badge',
        title: t('settings.modelBadgeTitle2'),
        'data-model-id': detail.model_id,
        onClick: () => openModelMetaDialog(provider, detail, ctx),
      }, label));
    }
    const compat = compatOf(provider);
    const hasKey = providerHasSavedKey(provider);
    const metaBadges = el('div', { class: 'provider-card-meta' },
      statusTag(hasKey ? t('settings.keySaved') : (authModeOf(provider) === 'none' ? t('settings.keyNone') : t('settings.keyMissing')),
        hasKey || authModeOf(provider) === 'none' ? 'ok' : 'warn',
        hasKey || authModeOf(provider) === 'none' ? '✓' : '!'),
      statusTag(t('settings.modelCountTag', { count: (provider.models || []).length }), 'info', '·'),
      authModeOf(provider) !== 'auto'
        ? statusTag(t('settings.authModeTag', { mode: String(AUTH_MODE_LABELS[authModeOf(provider)] || authModeOf(provider)).split('（')[0] }), 'info', '·')
        : null,
      isLocalUrl(provider.base_url) ? statusTag(t('settings.localService'), 'ok', '·') : null);
    const headers = extraHeaderKeys(provider);
    const compatText = [
      compat.max_tokens_param && compat.max_tokens_param !== 'auto' ? `max_tokens：${compat.max_tokens_param}` : '',
      compat.timeout_seconds ? t('settings.timeoutTag', { seconds: compat.timeout_seconds }) : '',
      compat.use_proxy_env ? t('settings.proxyTag') : '',
      compat.api_version ? `api-version：${compat.api_version}` : '',
    ].filter(Boolean).join(' · ');

    const testBtn = button(t('settings.testConnection'), e => busy(e.currentTarget, async () => {
      const model = modelSel.value || (provider.models || [])[0];
      if (!model) { testing.replaceChildren(notice(t('settings.testNoModel'), 'warning')); return; }
      if (!providerHasSavedKey(provider) && authModeOf(provider) !== 'none' && !isLocalUrl(provider.base_url)) {
        testing.replaceChildren(notice(t('settings.testNoKey'), 'warning'));
      }
      testing.replaceChildren(loading(t('settings.testCalling')));
      const probePayload = {
        kind: provider.kind,
        base_url: provider.base_url,
        provider_id: provider.id,
        auth_mode: authModeOf(provider),
        compat,
        model,
      };
      try {
        const res = await rawApi('/providers/probe', { method: 'POST', data: probePayload });
        const latency = Number(res.latency_ms);
        testing.replaceChildren(notice(t('settings.testOk', { latency: Number.isFinite(latency) ? t('settings.testLatency', { ms: Math.round(latency) }) : '', diagnosis: res.diagnosis || t('settings.modelResponded') })));
      } catch (error) {
        if (error.status === 404 || error.status === 405) {
          try {
            const res = await api(`/providers/${provider.id}/test`, { method: 'POST', data: { model } });
            testing.replaceChildren(notice(t('settings.testOkLegacy', { content: res.content || t('settings.modelResponded'), notice: res.notice || t('settings.legacyApi') })));
            return;
          } catch (legacyError) {
            testing.replaceChildren(notice(legacyError.message, 'error'));
            return;
          }
        }
        testing.replaceChildren(notice(error.message, 'error'));
      }
    }), 'secondary small', 'check');

    const card = el('section', { class: 'card provider-card pg-provider-card', 'data-provider-id': provider.id },
      el('div', { class: 'provider-head' },
        el('span', { class: 'provider-logo' }, icon('sparkle')),
        el('div', { style: 'flex:1;min-width:0' },
          el('h3', {}, provider.name),
          el('p', {}, `${providerKindLabel(provider.kind)} · ${isLocalUrl(provider.base_url) ? t('settings.localAddress') : t('settings.remoteAddress')}`)),
        button(t('common.edit'), () => openProviderDialog(provider, ctx), 'ghost small', 'edit')),
      el('div', { class: 'provider-key' }, el('span', {}, provider.key_hint || (hasKey ? t('settings.keyEncrypted') : t('settings.noKeySaved'))), icon('lock')),
      el('p', { class: 'help', style: 'margin-bottom:8px' }, provider.base_url),
      metaBadges,
      el('details', { class: 'pg-advanced provider-advanced' },
        el('summary', {}, t('settings.modelDetailsSummary', { count: (provider.model_details || []).length })),
        el('div', { class: 'pg-advanced-body' },
          el('p', { class: 'help' }, t('settings.modelDetailsHelp')),
          modelBadges.childNodes.length
            ? modelBadges
            : el('p', { class: 'help' }, t('settings.modelDetailsEmpty')),
          headers.length
            ? el('div', {},
              el('p', { class: 'help' }, t('settings.headersSaved', { headers: headers.join(t('common.listSeparator')) })))
            : null,
          compatText ? el('p', { class: 'help' }, t('settings.compatParams', { params: compatText })) : null)),
      field(t('settings.testModel'), modelSel),
      el('div', { class: 'row between', style: 'margin-top:14px' },
        testBtn,
        button(t('settings.deleteConfig'), e => busy(e.currentTarget, async () => {
          if (!window.confirm(t('settings.deleteConfirm', { name: provider.name }))) return;
          await api(`/providers/${provider.id}`, { method: 'DELETE' });
          toast(t('settings.deleted'));
          await ctx.afterProviderDeleted(provider.id);
        }), 'danger small', 'trash')),
      testing);
    return card;
  }

  ctx.renderProviders = () => {
    const children = state.providers.map(provider => buildProviderCard(provider));
    if (!children.length) {
      children.push(el('div', { class: 'card', style: 'grid-column:1/-1' },
        empty(t('settings.emptyTitle'),
          t('settings.emptyText'),
          button(t('settings.addConnection'), () => openProviderDialog(null, ctx), 'soft small', 'plus'), 'key')));
    }
    ctx.providersGrid.replaceChildren(...children);
  };

  ctx.afterProviderSaved = async () => {
    try {
      await refreshProviders();
    } catch (error) {
      toast(t('settings.savedButRefreshFailed', { message: error.message }), true);
    }
    ctx.renderProviders();
    ctx.renderApiStatus();
  };

  ctx.afterProviderDeleted = async id => {
    // 先本地清理：删除连接后默认模型引用不能继续指向已删除的 provider。
    state.providers = (state.providers || []).filter(item => String(item.id) !== String(id));
    for (const [task, def] of Object.entries(state.taskDefaults || {})) {
      if (!def) continue;
      const next = { ...def };
      let changed = false;
      if (String(next.primary_provider_id) === String(id)) {
        next.primary_provider_id = null; next.primary_model = null; changed = true;
      }
      if (String(next.verify_provider_id) === String(id)) {
        next.verify_provider_id = null; next.verify_model = null; changed = true;
      }
      if (String(next.judge_provider_id) === String(id)) {
        next.judge_provider_id = null; next.judge_model = null; changed = true;
      }
      if (changed) state.taskDefaults[task] = next;
    }
    ctx.renderProviders();
    ctx.renderApiStatus();
    try {
      await refreshProviders();
      ctx.renderProviders();
      ctx.renderApiStatus();
    } catch { /* 离线时保留本地清理结果 */ }
  };

  ctx.renderProviders();

  /* ---------- 3. 添加连接入口（保持与旧界面一致的 .provider-form 锚点） ---------- */
  const addSubmit = el('button', { class: 'button', type: 'submit' }, icon('plus'), t('settings.openWizard'));
  const addForm = el('form', { class: 'card provider-form' });
  addForm.append(
    el('div', { class: 'card-header' }, el('h2', {}, t('settings.addCardTitle')), badge(t('settings.addCardBadge'), 'teal')),
    el('p', { class: 'help', style: 'margin-bottom:14px' },
      t('settings.addCardText')),
    addSubmit);
  addForm.addEventListener('submit', event => {
    event.preventDefault();
    openProviderDialog(null, ctx);
  });

  /* ---------- 4. 复习穿插与偏好（保留高级折叠） ---------- */
  const interleaveSel = select([
    { value: '0', label: t('settings.interleave.off') },
    { value: '1', label: t('settings.interleave.one') },
    { value: '2', label: t('settings.interleave.two') },
  ], String(prefs.interleave_per_page ?? 2));
  const sameKpChk = el('input', { type: 'checkbox', checked: Boolean(prefs.same_kp_only) });
  const stripChk = el('input', { type: 'checkbox', checked: prefs.show_review_strip !== false });

  const prefsCard = el('section', { class: 'card pg-fold-card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('settings.reviewPrefsTitle')),
      badge(t('settings.advancedBadge'), 'blue')),
    el('details', { class: 'pg-advanced' },
      el('summary', {}, t('settings.reviewPrefsSummary')),
      el('div', { class: 'pg-advanced-body' },
        el('div', { class: 'form-grid' },
          field(t('settings.interleaveField'), interleaveSel, t('settings.interleaveHelp'))),
        el('label', { class: 'check-label' }, sameKpChk, t('settings.sameKp')),
        el('label', { class: 'check-label' }, stripChk, t('settings.strip')),
        el('div', { class: 'row end', style: 'margin-top:16px' },
          button(t('settings.savePrefs'), e => busy(e.currentTarget, async () => {
            await api('/preferences', {
              method: 'PATCH',
              data: {
                interleave_per_page: Number(interleaveSel.value),
                same_kp_only: sameKpChk.checked,
                show_review_strip: stripChk.checked,
              },
            });
            toast(t('settings.prefsSaved'));
            await ensurePreferences(true);
          }), '', 'check')))));

  /* ---------- 5. 语义检索（Embedding）配置卡片（懒加载） ---------- */
  const embCard = el('section', { class: 'card pg-fold-card pg-embedding-card' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('settings.embeddingTitle')),
      badge(t('settings.optionalBadge'), 'blue')));
  const embStatus = el('div', { class: 'pg-embedding-status' }, el('p', { class: 'help' }, t('settings.embeddingStatusIdle')));
  const embMode = select([
    { value: 'off', label: t('settings.embeddingMode.off') },
    { value: 'inherit', label: t('settings.embeddingMode.inherit') },
    { value: 'custom', label: t('settings.embeddingMode.custom') },
    { value: 'local', label: t('settings.embeddingMode.local') },
  ], 'off');
  const embProviderSel = select([{ value: '', label: t('settings.embeddingProvider') },
    ...(state.providers || []).map(provider => ({ value: String(provider.id), label: provider.name }))]);
  const embModelIn = input(t('settings.embeddingModelPlaceholder'));
  const embBaseUrlIn = input('https://api.openai.com/v1');
  const embKeyIn = input(t('settings.embeddingKeyPlaceholder'), { type: 'password', autocomplete: 'off' });
  const embReuseField = field(t('settings.embeddingReuseField'), embProviderSel);
  const embCustomBox = el('div', { class: 'pg-embedding-custom hide' },
    field(t('settings.embeddingBaseUrl'), embBaseUrlIn),
    field('Embedding API Key', embKeyIn));
  const embResult = el('div', { class: 'pg-embedding-result', 'aria-live': 'polite' });
  const embSaveBtn = button(t('settings.embeddingSave'), null, 'small');
  const embTestBtn = button(t('settings.embeddingTest'), null, 'secondary small');
  const embForm = el('div', { class: 'pg-embedding-form' },
    field(t('settings.embeddingModeField'), embMode),
    embReuseField,
    field(t('settings.embeddingModelField'), embModelIn),
    embCustomBox,
    el('div', { class: 'row' }, embTestBtn, embSaveBtn),
    embResult);
  const embDetails = el('details', { class: 'pg-advanced' },
    el('summary', {}, t('settings.embeddingSummary')),
    el('div', { class: 'pg-advanced-body' }, embStatus, embForm));
  embCard.append(embDetails);

  let embLoaded = false;
  let embConfig = null;

  function refreshEmbeddingLocalOption() {
    const option = [...embMode.options].find(item => item.value === 'local');
    if (!option) return;
    const available = Boolean(embConfig && embConfig.local_available);
    option.disabled = !available;
    option.textContent = available
      ? t('settings.embeddingMode.local')
      : t('settings.embeddingLocalUnavailable');
  }

  function syncEmbeddingMode() {
    const mode = embMode.value;
    embReuseField.classList.toggle('hide', mode !== 'inherit');
    embCustomBox.classList.toggle('hide', mode !== 'custom');
    embModelIn.closest('.field')?.classList?.toggle('hide', mode === 'off' || mode === 'local');
    refreshEmbeddingLocalOption();
  }
  embMode.addEventListener('change', syncEmbeddingMode);

  function populateEmbeddingProviders(items) {
    const list = Array.isArray(items) && items.length
      ? items
      : (state.providers || [])
        .filter(provider => ['openai', 'gemini', 'azure_openai'].includes(provider.kind))
        .map(provider => ({ id: provider.id, name: provider.name, models: provider.models || [] }));
    embProviderSel.replaceChildren(
      el('option', { value: '' }, t('settings.embeddingProvider')),
      ...list.map(item => el('option', { value: String(item.id) }, item.name || `#${item.id}`)));
    return list;
  }
  let embeddingProviders = populateEmbeddingProviders(state.providers || []);
  embProviderSel.addEventListener('change', () => {
    const hit = embeddingProviders.find(item => String(item.id) === embProviderSel.value);
    if (hit && !embModelIn.value.trim()) {
      const preferred = (hit.models || []).find(model => /embed/i.test(model));
      embModelIn.value = preferred || (hit.models || [])[0] || '';
    }
  });
  syncEmbeddingMode();

  function renderEmbeddingStatus() {
    const cfg = embConfig || {};
    const effective = cfg.effective || {};
    const source = String(cfg.effective_source || cfg.source || cfg.mode || '').toLowerCase();
    const mode = String(cfg.mode || '').toLowerCase();
    const ready = effective.ready ?? (source === 'custom' || source === 'inherit' || source === 'env');
    const statusTagNode = (() => {
      if (!embLoaded) return statusTag(t('settings.embeddingLoadingConfig'), 'info', '…');
      if (mode === 'off' && source !== 'env') return statusTag(t('settings.embeddingStatusDisabled'), 'warn', '!');
      if (source === 'env') return statusTag(t('settings.embeddingStatusEnv'), 'info', '·');
      if (source === 'local') return statusTag(t('settings.embeddingStatusLocal'), ready ? 'ok' : 'warn', ready ? '✓' : '!');
      if (source === 'inherit') return statusTag(t('settings.embeddingStatusInherit'), ready ? 'ok' : 'warn', ready ? '✓' : '!');
      if (source === 'custom') return statusTag(t('settings.embeddingStatusCustom'), ready ? 'ok' : 'warn', ready ? '✓' : '!');
      if (cfg.env_configured) return statusTag(t('settings.embeddingStatusEnvFallback'), 'warn', '!');
      if (ready) return statusTag(t('settings.embeddingStatusEnabled'), 'ok', '✓');
      return statusTag(t('settings.embeddingStatusUnconfigured'), 'warn', '!');
    })();
    const rows = [statusTagNode];
    const model = effective.model || cfg.model;
    if (model) rows.push(el('span', { class: 'help' }, t('settings.embeddingModelRow', { model })));
    if (cfg.provider_id) {
      const provider = providerById(cfg.provider_id);
      rows.push(el('span', { class: 'help' }, t('settings.embeddingProviderRow', { provider: provider ? provider.name : `#${cfg.provider_id}` })));
    }
    if (cfg.has_api_key || cfg.key_hint) rows.push(el('span', { class: 'help' }, t('settings.embeddingKeySaved')));
    if (source === 'local' && cfg.local_available) {
      rows.push(el('span', { class: 'help' }, t('settings.embeddingLocalModel', {
        model: cfg.local_model || cfg.effective?.model || 'bge-small-zh-v1.5',
        dimension: cfg.local_dimension || '—',
      })));
    }
    if (source === 'local' && !cfg.local_available) {
      rows.push(el('span', { class: 'help' }, t('settings.embeddingLocalMissing')));
    }
    if (cfg.env_configured && source !== 'env') {
      rows.push(el('span', { class: 'help' }, t('settings.embeddingEnvConfigured')));
    }
    if (Array.isArray(cfg.compatible_providers) && cfg.compatible_providers.length) {
      rows.push(el('span', { class: 'help' }, t('settings.embeddingCompatible', { count: cfg.compatible_providers.length })));
    }
    embStatus.replaceChildren(...rows);
  }

  function embeddingPayload() {
    const mode = embMode.value;
    const payload = { mode };
    if (mode === 'inherit') {
      payload.provider_id = embProviderSel.value ? Number(embProviderSel.value) : null;
      if (embModelIn.value.trim()) payload.model = embModelIn.value.trim();
    } else if (mode === 'custom') {
      payload.model = embModelIn.value.trim();
      if (embBaseUrlIn.value.trim()) payload.base_url = embBaseUrlIn.value.trim();
      if (embKeyIn.value.trim()) payload.api_key = embKeyIn.value.trim();
    }
    return payload;
  }

  function applyEmbeddingConfig(cfg) {
    embConfig = cfg || {};
    refreshEmbeddingLocalOption();
    const mode = String(cfg?.mode || '').toLowerCase();
    if (mode === 'off' || (!mode && cfg?.effective_source === 'off')) embMode.value = 'off';
    else if (mode === 'local') embMode.value = 'local';
    else if (mode === 'inherit' || mode === 'reuse' || mode === 'provider') embMode.value = 'inherit';
    else if (mode === 'custom') embMode.value = 'custom';
    else if (cfg?.effective_source === 'env') embMode.value = 'off';
    else embMode.value = 'off';
    if (Array.isArray(cfg?.compatible_providers)) {
      embeddingProviders = populateEmbeddingProviders(cfg.compatible_providers);
    }
    if (cfg?.provider_id) embProviderSel.value = String(cfg.provider_id);
    if (cfg?.model) embModelIn.value = cfg.model;
    else if (cfg?.effective?.model) embModelIn.value = cfg.effective.model;
    if (cfg?.base_url) embBaseUrlIn.value = cfg.base_url;
    syncEmbeddingMode();
    renderEmbeddingStatus();
  }

  async function loadEmbeddingConfig() {
    if (embLoaded) return;
    embLoaded = true;
    embStatus.replaceChildren(loading(t('settings.embeddingLoading')));
    const [cfgResult, readyResult] = await Promise.allSettled([
      rawApi('/providers/embeddings-config'),
      api('/health/ready').catch(() => null),
    ]);
    if (cfgResult.status === 'fulfilled') {
      applyEmbeddingConfig(cfgResult.value);
    } else if (cfgResult.reason?.status === 404 || cfgResult.reason?.status === 405) {
      embStatus.replaceChildren(notice(t('settings.embeddingUnavailableBackend'), 'warning'));
    } else {
      embStatus.replaceChildren(notice(t('settings.embeddingReadFailed', { message: cfgResult.reason?.message || t('settings.embeddingUnknownError') }), 'error'));
    }
    // 管理员 /health/ready 详情里可能带 embedding 状态：只展示状态字段，绝不渲染路径。
    const ready = readyResult.status === 'fulfilled' ? readyResult.value : null;
    const embReady = ready?.embedding || ready?.embeddings || ready?.checks?.embedding || ready?.checks?.embeddings
      || ready?.embedding_status || null;
    if (embReady) {
      const text = typeof embReady === 'string'
        ? embReady
        : [embReady.ready === false || embReady.configured === false ? t('settings.embeddingNotReady') : t('settings.embeddingReady'),
          embReady.detail,
          embReady.source && embReady.source !== 'none' ? t('settings.embeddingSourceRow', { source: embReady.source }) : '',
          embReady.model ? t('settings.embeddingModelRowShort', { model: embReady.model }) : '',
          embReady.dimension ? t('settings.embeddingDimensionRow', { dimension: embReady.dimension }) : '',
          embReady.recent_error?.message || embReady.recent_error?.detail || ''].filter(Boolean).join(' · ');
      embStatus.append(el('p', { class: 'help' }, t('settings.embeddingReadyCheck', { text })));
    }
  }

  embDetails.addEventListener('toggle', () => {
    if (embDetails.open) loadEmbeddingConfig();
  });

  embTestBtn.addEventListener('click', () => busy(embTestBtn, async () => {
    const payload = embeddingPayload();
    if (payload.mode === 'off') {
      embResult.replaceChildren(notice(t('settings.embeddingOffNotice'), 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.provider_id) {
      embResult.replaceChildren(notice(t('settings.embeddingSelectProvider'), 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.model) {
      embResult.replaceChildren(notice(t('settings.embeddingInheritModelNeeded'), 'warning'));
      return;
    }
    if (payload.mode === 'custom' && !payload.model) {
      embResult.replaceChildren(notice(t('settings.embeddingModelNeeded'), 'warning'));
      return;
    }
    embResult.replaceChildren(loading(t('settings.embeddingCalling')));
    try {
      const res = await rawApi('/providers/embeddings-test', { method: 'POST', data: payload });
      if (res.ok === false) {
        embResult.replaceChildren(notice(t('settings.embeddingUnavailable', { reason: res.diagnosis || res.error || t('settings.embeddingUnknownReason') }), 'error'));
        return;
      }
      const parts = [
        res.diagnosis || t('settings.embeddingOk'),
        res.model ? t('settings.embeddingModelLine', { model: res.model }) : '',
        res.dimension ? t('settings.embeddingDimensionLine', { dimension: res.dimension }) : '',
        Number.isFinite(Number(res.latency_ms)) ? t('settings.embeddingLatencyLine', { ms: Math.round(Number(res.latency_ms)) }) : '',
        res.source ? t('settings.embeddingSourceRow', { source: res.source }) : '',
      ].filter(Boolean);
      embResult.replaceChildren(notice(parts.join('；')));
    } catch (error) {
      if (error.status === 404 || error.status === 405) {
        embResult.replaceChildren(notice(t('settings.embeddingTestUnavailable'), 'warning'));
      } else {
        embResult.replaceChildren(notice(t('settings.embeddingTestFailed', { message: error.message }), 'error'));
      }
    }
  }));

  embSaveBtn.addEventListener('click', () => busy(embSaveBtn, async () => {
    const payload = embeddingPayload();
    if (payload.mode === 'inherit' && !payload.provider_id) {
      embResult.replaceChildren(notice(t('settings.embeddingSelectProvider'), 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.model) {
      embResult.replaceChildren(notice(t('settings.embeddingInheritModelNeeded'), 'warning'));
      return;
    }
    if (payload.mode === 'custom' && (!payload.model || !payload.base_url)) {
      embResult.replaceChildren(notice(t('settings.embeddingCustomNeeds'), 'warning'));
      return;
    }
    embResult.replaceChildren(loading(t('settings.embeddingSaving')));
    try {
      const res = await rawApi('/providers/embeddings-config', { method: 'PUT', data: payload });
      embConfig = res || payload;
      applyEmbeddingConfig(embConfig);
      embResult.replaceChildren(notice(t('settings.embeddingSaved')));
      toast(t('settings.embeddingSavedToast'));
    } catch (error) {
      if (error.status === 404 || error.status === 405) {
        embResult.replaceChildren(notice(t('settings.embeddingUnavailableBackendSave'), 'warning'));
      } else {
        embResult.replaceChildren(notice(t('settings.embeddingSaveFailed', { message: error.message }), 'error'));
      }
    }
  }));

  function openEmbeddingCard() {
    embCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (!embDetails.open) embDetails.open = true;
    loadEmbeddingConfig();
  }

  /* ---------- 6. 本地引擎总览（阶段 5-1） ---------- */
  const engineCard = el('section', { class: 'card settings-engines-card', style: 'margin-top:22px' });
  let enginesBusy = false;

  function engineName(id) {
    const key = `settings.engines.name.${id}`;
    const value = t(key);
    return value === key ? id : value;
  }

  function engineStateText(engine) {
    if (engine.available) return t('settings.engines.available');
    if (engine.mode === 'off') return t('settings.engines.off');
    return t('settings.engines.unavailableShort');
  }

  function engineDetailLines(engine, admin) {
    const lines = [];
    const model = engine.model || engine.model_label
      || engine.tts_model_label || engine.asr_model_label;
    if (model) lines.push(t('settings.engines.model', { model }));
    if (engine.version) lines.push(t('settings.engines.version', { version: engine.version }));
    if (Number(engine.missing_count) > 0) {
      lines.push(t('settings.engines.missing', { count: number(engine.missing_count) }));
    }
    if (engine.notice) lines.push(engine.notice);
    if (engine.error) lines.push(t('settings.engines.error', { error: engine.error }));
    if (admin && engine.directories && typeof engine.directories === 'object') {
      const paths = Object.values(engine.directories).filter(Boolean).join(' · ');
      if (paths) lines.push(t('settings.engines.paths', { paths }));
    }
    if (admin && engine.model_path) {
      lines.push(t('settings.engines.paths', { paths: engine.model_path }));
    }
    return lines.filter(Boolean);
  }

  function renderEngineCard(data) {
    const engines = Array.isArray(data.engines) ? data.engines : [];
    const admin = data.can_reset === true;
    const rows = engines.map(engine => el('li', {
      class: 'settings-engine-row', 'data-engine': engine.id,
    },
    el('div', { class: 'settings-engine-head' },
      el('strong', {}, engineName(engine.id)),
      statusTag(engineStateText(engine), engine.available ? 'ok' : 'warn',
        engine.available ? '✓' : '!')),
    el('div', { class: 'settings-engine-lines' },
      ...engineDetailLines(engine, admin).map(text => el('span', {}, text))),
    admin && engine.id !== 'chem'
      ? button(t('settings.engines.reset'), event => busy(event.currentTarget, async () => {
        await api(`/engines/${engine.id}/reset`, { method: 'POST' });
        toast(t('settings.engines.resetDone'));
        await loadEngines(true);
      }), 'ghost small', 'rotate')
      : null));
    const warmup = data.warmup || {};
    const warmupLine = warmup.finished_at
      ? t('settings.engines.warmupDone', { time: dateText(warmup.finished_at) })
      : t('settings.engines.warmupPending');
    const compression = data.compression || null;
    engineCard.replaceChildren(
      el('div', { class: 'card-header' },
        el('h2', {}, t('settings.engines.title')),
        badge(t('settings.engines.badge', {
          available: Number(data.window?.available || 0),
          total: Number(data.window?.count || engines.length),
        }))),
      el('p', { class: 'help' }, t('settings.engines.help')),
      el('ul', { class: 'settings-engine-list' }, ...rows),
      el('p', { class: 'help settings-engine-meta' }, warmupLine),
      compression
        ? el('p', { class: 'help settings-engine-meta' },
          t('settings.engines.compression', {
            format: compression.format || 'wav',
            ffmpeg: compression.ffmpeg_available
              ? t('settings.engines.ffmpegOn') : t('settings.engines.ffmpegOff'),
          }))
        : null,
      !admin ? el('p', { class: 'help' }, t('settings.engines.adminOnly')) : null,
      el('div', { class: 'row', style: 'margin-top:10px' },
        button(t('settings.engines.refresh'), event => busy(event.currentTarget, () => loadEngines(true)),
          'secondary small', 'rotate')),
    );
  }

  async function loadEngines(force = false) {
    if (enginesBusy && !force) return;
    enginesBusy = true;
    engineCard.replaceChildren(
      el('div', { class: 'card-header' }, el('h2', {}, t('settings.engines.title'))),
      loading(t('settings.engines.loading')));
    try {
      const data = await api('/engines');
      renderEngineCard(data);
    } catch (error) {
      engineCard.replaceChildren(
        el('div', { class: 'card-header' }, el('h2', {}, t('settings.engines.title'))),
        notice(t('settings.engines.unavailable', { reason: error.message }), 'warning'),
        el('div', { class: 'row', style: 'margin-top:10px' },
          button(t('settings.engines.refresh'), event => busy(event.currentTarget, () => loadEngines(true)),
            'secondary small', 'rotate')));
    } finally {
      enginesBusy = false;
    }
  }

  /* ---------- 7. AI 用量与费用估算（只展示估算，不扣减） ---------- */
  const usageRows = Array.isArray(usage.by_model) ? usage.by_model : [];
  const usageTotals = usageRows.reduce((acc, row) => {
    acc.calls += Number(row.calls || 0);
    acc.input += Number(row.input_tokens || 0);
    acc.output += Number(row.output_tokens || 0);
    if (row.estimated_cost !== null && row.estimated_cost !== undefined) {
      acc.cost += Number(row.estimated_cost);
      acc.pricedModels += 1;
    } else {
      acc.unpricedModels += 1;
    }
    return acc;
  }, { calls: 0, input: 0, output: 0, cost: 0, pricedModels: 0, unpricedModels: 0 });
  const usageCard = el('section', { class: 'card settings-usage-card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, t('settings.usageTitle')),
      badge(t('settings.usageModelBadge', { count: usageRows.length }))),
    el('p', { class: 'help', style: 'margin-bottom:10px' },
      t('settings.usageNote')),
    el('div', { class: 'settings-usage-summary', 'aria-live': 'polite' },
      el('span', {}, t('settings.usageCalls', { count: number(usageTotals.calls) })),
      el('span', {}, t('settings.usageInput', { count: number(usageTotals.input) })),
      el('span', {}, t('settings.usageOutput', { count: number(usageTotals.output) })),
      el('span', { class: 'settings-usage-cost' }, usageTotals.pricedModels
        ? t('settings.usageTotalEstimate', { amount: moneyText(usageTotals.cost) })
        : t('settings.usageTotalUnknown')),
      usageTotals.unpricedModels
        ? el('span', { class: 'settings-usage-warn' }, t('settings.usageUnpriced', { count: usageTotals.unpricedModels }))
        : null),
    el('p', { class: 'help settings-usage-source', 'data-no-i18n': '' },
      usage.notice || t('settings.usageNoticeFallback')));
  // 今日用量 / 剩余配额（WS23）：接口未就绪时整块隐藏，不影响既有 30 天用量表。
  if (usageToday && usageToday.quota) {
    const quotaKinds = Array.isArray(usageToday.quota.kinds) ? usageToday.quota.kinds : [];
    const visible = quotaKinds.filter(entry => Number(entry.used_today) > 0
      || ['explain', 'grade', 'notes', 'recognize', 'ocr', 'course'].includes(entry.kind));
    const today = usageToday.today || {};
    const chips = [
      el('span', { class: 'settings-usage-quota-chip' },
        t('ai.usage.quotaTodayJobs', { count: number(today.jobs || 0) })),
      el('span', { class: 'settings-usage-quota-chip' },
        t('ai.usage.quotaTokens', {
          input: number(today.tokens_in || 0), output: number(today.tokens_out || 0),
        })),
      ...visible.map(entry => {
        const kindKey = `tasks.task.${entry.kind}`;
        const label = t(kindKey) !== kindKey ? t(kindKey) : entry.label || entry.kind;
        const limit = entry.daily_limit;
        const text = limit === null || limit === undefined || Number(limit) === 0
          ? t('ai.usage.quotaUnlimited')
          : t('ai.usage.quotaEntry', { used: number(entry.used_today || 0), limit: number(limit) });
        return el('span', {
          class: `settings-usage-quota-chip${entry.limit_reached ? ' is-warn' : ''}`,
          title: entry.remaining_today === null || entry.remaining_today === undefined
            ? ''
            : t('ai.usage.quotaRemaining', { count: number(entry.remaining_today) }),
        }, `${label} · ${text}`);
      }),
    ];
    usageCard.append(
      el('div', { class: 'settings-usage-quota', 'aria-live': 'polite' },
        el('strong', {}, t('ai.usage.quotaTitle')),
        el('div', { class: 'settings-usage-quota-chips' }, ...chips),
        el('p', { class: 'help' }, t('ai.usage.quotaNote'))));
  }
  if (usageRows.length) {
    const tbl = el('table', { class: 'settings-usage-table' },
      el('thead', {}, el('tr', {},
        el('th', {}, t('settings.usageHeadModel')), el('th', {}, t('settings.usageHeadCalls')), el('th', {}, t('settings.usageHeadInput')),
        el('th', {}, t('settings.usageHeadOutput')), el('th', {}, t('settings.usageHeadPrice')), el('th', {}, t('settings.usageHeadCost')))),
      el('tbody', {}, ...usageRows.map(row => {
        const provider = providerById(row.provider_id);
        return el('tr', {},
          el('td', {},
            el('strong', {}, row.model || t('settings.usageUnknownModel')),
            provider ? el('div', { class: 'settings-usage-connect' }, provider.name) : null),
          el('td', {}, number(row.calls)),
          el('td', {}, number(row.input_tokens)),
          el('td', {}, number(row.output_tokens)),
          el('td', {}, priceLabel(row)),
          el('td', { class: 'settings-usage-cost' }, row.estimated_cost !== null && row.estimated_cost !== undefined
            ? `¥ ${moneyText(row.estimated_cost)}` : t('tasks.priceUnset')));
      })));
    usageCard.append(el('div', { class: 'table-wrap' }, tbl));
  } else {
    usageCard.append(el('p', { class: 'help' },
      t('settings.usageEmpty')));
  }

  /* ---------- 7. 账号安全 ---------- */
  const curPwd = input(t('settings.currentPasswordPlaceholder'), { type: 'password', autocomplete: 'current-password' });
  const newPwd = input(t('settings.newPasswordPlaceholder'), { type: 'password', autocomplete: 'new-password' });
  const codesMount = el('div');
  const secCard = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, t('settings.securityTitle')), badge(t('settings.securityBadge'), 'teal')),
    el('div', { class: 'form-grid' }, field(t('settings.currentPassword'), curPwd), field(t('settings.newPasswordField'), newPwd)),
    el('div', { class: 'row between', style: 'margin-top:14px' },
      button(t('settings.generateRecovery'), e => busy(e.currentTarget, async () => {
        if (!window.confirm(t('settings.generateRecoveryConfirm'))) return;
        if (!curPwd.value) throw new Error(t('settings.generateRecoveryNeedPassword'));
        const res = await api('/auth/recovery-codes', {
          method: 'POST', data: { current_password: curPwd.value },
        });
        codesMount.replaceChildren(
          notice(res.notice, 'warning'),
          el('pre', { class: 'prompt-preview-pre' }, (res.codes || []).join('\n'))
        );
      }), 'secondary small', 'key'),
      button(t('settings.changePassword'), e => busy(e.currentTarget, async () => {
        if (!curPwd.value || newPwd.value.length < 8) throw new Error(t('settings.changePasswordNeedBoth'));
        const res = await api('/auth/change-password', {
          method: 'POST', data: { current_password: curPwd.value, new_password: newPwd.value },
        });
        curPwd.value = ''; newPwd.value = '';
        toast(res.message || t('settings.passwordChanged'));
      }), '', 'lock')),
    codesMount);

  container.replaceChildren(
    heading('MODEL CONNECTIONS', t('settings.heading'),
      t('settings.subheading')),
    apiStatusCard,
    ctx.providersGrid,
    addForm,
    prefsCard,
    embCard,
    engineCard,
    usageCard,
    secCard
  );
  loadEngines();
  await renderJobHistory(container, version);
}
