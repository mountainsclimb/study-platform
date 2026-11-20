import { api, ensurePreferences, errorText, refreshProviders } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, icon, button, badge, empty, heading, loading, notice, field, input, select,
  number, busy, toast,
} from '../core/dom.js';
import { current, renderRoute } from '../core/router.js';
import { groupedModelOptions } from '../components/modelPicker.js';
import { renderJobHistory } from './upload.js';

const TASK_LABELS = {
  explain: 'AI 讲解',
  grade: '答题判分',
  judge: '独立裁判模型',
  notes: '知识清单整理',
  ocr: '拍题 / OCR 识别',
  upload: '拍题求解',
  tagging: '批量知识点打标',
};

const TASK_ORDER = ['explain', 'grade', 'judge', 'notes', 'ocr', 'upload', 'tagging'];

const TASK_HINTS = {
  explain: '讲解、知识清单等页面在没有单题选择时使用这里的默认模型。',
  grade: '判分任务缺省复用讲解模型；需要更严格的判分时可在右侧单独指定核验模型。',
  judge: '三模型交叉核验时由模型 C 定位分歧步骤，不由主模型自判。',
};

const KIND_LABELS = {
  openai: 'OpenAI 兼容接口',
  anthropic: 'Claude 原生接口',
  gemini: 'Gemini 兼容接口',
  gemini_native: 'Gemini 原生接口',
  azure_openai: 'Azure OpenAI',
};

const AUTH_MODE_LABELS = {
  auto: '自动（按接口类型选择鉴权头）',
  bearer: 'Authorization: Bearer <key>',
  'x-api-key': 'x-api-key（Claude / 部分中转）',
  'api-key': 'api-key（Azure OpenAI）',
  query_key: 'URL 查询参数 ?key=（Gemini 原生）',
  none: '无鉴权（本地模型可留空 Key）',
};

const MAX_EXTRA_HEADERS = 8;

// 「常用自定义与本地」分组：比官方预设更贴近中转站 / 本地模型场景。
const CUSTOM_PRESETS = [
  {
    group: '常用自定义与本地',
    name: '自定义 OpenAI 兼容 / 中转站',
    kind: 'openai',
    base_url: 'https://',
    models: [],
    auth_mode: 'auto',
    hint: 'One-API / New-API / OpenRouter / 企业网关等任意 OpenAI 兼容地址',
  },
  {
    group: '常用自定义与本地',
    name: '本地 Ollama',
    kind: 'openai',
    base_url: 'http://127.0.0.1:11434/v1',
    models: ['llama3.2', 'qwen2.5', 'qwen2.5-vl', 'deepseek-r1', 'nomic-embed-text'],
    auth_mode: 'none',
    local: true,
    hint: '本地无鉴权服务：API Key 可留空',
  },
  {
    group: '常用自定义与本地',
    name: '本地 LM Studio / vLLM',
    kind: 'openai',
    base_url: 'http://127.0.0.1:1234/v1',
    models: ['local-model'],
    auth_mode: 'none',
    local: true,
    hint: 'LM Studio 默认 :1234，vLLM 默认 :8000；API Key 可留空',
  },
  {
    group: '常用自定义与本地',
    name: 'Azure OpenAI',
    kind: 'azure_openai',
    base_url: 'https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT',
    models: ['gpt-4o', 'gpt-4.1-mini'],
    auth_mode: 'api-key',
    compat: { api_version: '2024-10-21' },
    hint: '地址填到 deployment；在高级选项里填写 api-version',
  },
  {
    group: '常用自定义与本地',
    name: '自定义 Claude 中转',
    kind: 'anthropic',
    base_url: 'https://',
    models: ['claude-sonnet-4-5', 'claude-3-5-haiku-latest'],
    auth_mode: 'auto',
    hint: 'Claude 原生协议，可额外加 anthropic-beta 等自定义请求头',
  },
  {
    group: '常用自定义与本地',
    name: 'Gemini 原生接口',
    kind: 'gemini_native',
    base_url: 'https://generativelanguage.googleapis.com/v1beta',
    models: ['gemini-2.5-flash', 'gemini-2.5-pro'],
    auth_mode: 'query_key',
    hint: 'Gemini 原生 generateContent 协议，Key 以 ?key= 传递',
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
  return KIND_LABELS[kind] || kind || '未知接口';
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
    if (task === 'grade') return '跟随讲解模型（可单独指定）';
    if (isJudge) return '未固定（分歧时由核验模型 B 裁定）';
    return '跟随页面模型选择器';
  }
  const provider = providerById(pid);
  if (!provider) return '原连接已删除，请重新选择';
  const parts = [provider.name];
  if (vpid && verifySelectionOf(def)) {
    const vProvider = providerById(vpid);
    parts.push(`核验：${vProvider ? vProvider.name : '原连接已删除'}`);
  }
  parts.push(def?.mode === 'accurate' ? '精确模式' : '快速模式');
  return parts.join(' · ');
}

function groupedOptions() {
  const { groups } = groupedModelOptions();
  return groups;
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
    const error = new Error(errorText(json?.detail || json?.error || `请求失败（${response.status}）`));
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
    openai: '发送 OpenAI 兼容的 /chat/completions 请求',
    anthropic: '发送 Claude 原生 /messages 请求',
    gemini: '通过 Gemini 的 OpenAI 兼容层请求',
    gemini_native: '发送 Gemini 原生 generateContent 请求',
    azure_openai: '通过 Azure deployment 地址请求，需要 api-version',
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
    'aria-label': editing ? `编辑连接 ${provider.name}` : '添加模型连接',
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
    optgroup: '常用自定义与本地',
    options: CUSTOM_PRESETS.map((item, index) => ({ value: `custom-${index}`, label: item.name })),
  });
  const officialPresets = state.presets || [];
  if (officialPresets.length) {
    presetOptions.push({
      optgroup: '官方厂商预设（含推荐模型）',
      options: officialPresets.map((item, index) => ({ value: `preset-${index}`, label: item.name })),
    });
  }
  const presetSel = select([
    { value: '', label: editing ? '保持当前配置' : '选择厂商预设（可选）' },
    ...presetOptions,
  ], '', { 'data-field': 'preset' });
  const presetHint = el('p', { class: 'help' });

  const nameIn = input('例如：我的 DeepSeek', {
    value: provider?.name || '', 'data-field': 'name', required: true,
  });
  const kindSel = select(Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })),
    provider?.kind || 'openai', { 'data-field': 'kind' });
  const baseUrlIn = input('https://api.example.com/v1', {
    type: 'url', value: provider?.base_url || '', 'data-field': 'base-url', required: true,
  });
  const apiKeyIn = input(
    editing
      ? (providerHasSavedKey(provider) ? '留空表示沿用已保存的密钥' : '留空表示保持当前（未保存密钥 / 无鉴权）')
      : '本地无鉴权服务可留空',
    { type: 'password', autocomplete: 'off', 'data-field': 'api-key' },
  );
  const keyHint = el('p', { class: 'help' },
    editing && providerHasSavedKey(provider)
      ? `当前密钥：${provider.key_hint || '已加密保存'}；不修改时不会回传完整 Key。`
      : '密钥只在保存时发送到本机后端并加密存储；本地无鉴权服务可留空（需把鉴权方式设为「无鉴权」）。');

  // ---- 地址规范化预览 + 模型列表（不截断）
  const previewMount = el('div', { class: 'provider-preview-mount' });
  const modelSearch = input('搜索模型（支持 200+ 列表）…', {
    'data-field': 'model-search', type: 'search', 'aria-label': '搜索模型',
  });
  const selectAllBtn = el('button', { type: 'button', class: 'button secondary small', 'data-action': 'model-select-all' }, '全选当前结果');
  const modelCount = el('span', { class: 'provider-model-count' }, '尚未拉取模型列表');
  const modelList = el('div', { class: 'provider-model-list', 'data-role': 'model-list' });
  const manualModels = el('textarea', {
    rows: 3,
    placeholder: '每行一个模型 ID；用于后端暂不支持保存前预览时手动填写',
    'data-field': 'manual-models',
  });
  manualModels.value = [...selectedModels].join('\n');

  const syncModelsToTextarea = () => {
    manualModels.value = [...selectedModels].join('\n');
  };

  const manualModelsDetails = el('details', { class: 'pg-advanced', style: 'margin-top:8px' },
    el('summary', {}, '手动维护模型 ID（每行一个）'),
    el('div', { class: 'pg-advanced-body' }, manualModels,
      el('p', { class: 'help' }, '厂商返回的模型列表会全部展示、不截断；也可在这里手动补充或粘贴模型 ID。')));
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
        availableModels.length ? '没有匹配的模型' : '还没有模型列表；填写地址后会自动生成规范化预览，或点「拉取厂商模型」。'));
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
        item.caps?.vision ? badge('视觉', 'teal') : null,
        item.caps?.reasoning ? badge('推理', 'blue') : null));
    }
    updateModelCount();
  }

  function updateModelCount() {
    modelCount.textContent = availableModels.length
      ? `共 ${availableModels.length} 个模型 · 已选 ${selectedModels.size}`
      : `已选 ${selectedModels.size} 个模型`;
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
  const addHeaderBtn = el('button', { type: 'button', class: 'button secondary small', 'data-action': 'add-header' }, '添加请求头');

  const compat = compatOf(provider);
  const maxTokensSel = select([
    { value: 'auto', label: '自动（推荐）' },
    { value: 'max_tokens', label: '始终使用 max_tokens' },
    { value: 'max_completion_tokens', label: '始终使用 max_completion_tokens' },
  ], compat.max_tokens_param || 'auto', { 'data-field': 'max-tokens-param' });
  const timeoutIn = input('120', {
    type: 'number', min: 15, max: 600, step: 5,
    value: compat.timeout_seconds ?? 120, 'data-field': 'timeout',
  });
  const apiVersionIn = input('例如 2024-10-21', {
    value: compat.api_version || '', 'data-field': 'api-version',
  });
  const proxyChk = el('input', {
    type: 'checkbox', checked: Boolean(compat.use_proxy_env), 'data-field': 'use-proxy',
  });
  const proxyRow = el('label', { class: 'check-label', style: 'margin-top:6px' },
    proxyChk, '允许该连接使用系统代理（HTTP_PROXY / HTTPS_PROXY）');
  const proxyHint = el('p', { class: 'help' },
    ctx.localMode === false
      ? '仅本地模式生效：公网部署下会忽略该开关。'
      : '仅本地模式生效；通过本机代理访问海外厂商时打开。');

  function buildHeaderRow() {
    const keyInput = input('例如 HTTP-Referer', { 'data-field': 'header-key' });
    const valueInput = input('请求头值（保存后仅回显键名）', { 'data-field': 'header-value' });
    const removeBtn = el('button', { type: 'button', class: 'button ghost small', 'aria-label': '删除该请求头' }, '删除');
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
    addHeaderBtn.title = total >= MAX_EXTRA_HEADERS ? `最多 ${MAX_EXTRA_HEADERS} 条自定义请求头` : '';
    updateHeaderReplaceHint();
  }

  function updateHeaderReplaceHint() {
    const editingSaved = Boolean(provider?.id) && savedHeaderKeys.length > 0;
    if (!editingSaved) { headerReplaceHint.textContent = ''; return; }
    headerReplaceHint.textContent = removedHeaderKeys.size || newHeaderRows.length
      ? '注意：已保存请求头的值无法回显，保存后会以本次填写的请求头整体替换原有请求头。'
      : '已保存请求头的值已脱敏，仅显示键名；不修改时不会发送 extra_headers。';
  }

  function renderSavedHeaders() {
    headersMount.replaceChildren();
    const kept = savedHeaderKeys.filter(key => !removedHeaderKeys.has(key));
    if (!kept.length) { updateHeaderReplaceHint(); return; }
    headersMount.append(el('p', { class: 'help' }, '已保存的请求头（脱敏，只回显键名）：'));
    const tags = el('div', { class: 'provider-header-saved' });
    for (const key of kept) {
      tags.append(el('span', { class: 'badge blue' }, key,
        el('button', {
          type: 'button', 'aria-label': `删除已保存请求头 ${key}`,
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
    if (res?.ok === false && res?.diagnosis) notes.push(`厂商连接失败：${res.diagnosis}`);
    else if (res?.diagnosis && res?.ok === true) notes.push(res.diagnosis);
    if (listEndpoint && listEndpoint !== chatEndpoint) {
      notes.push(`已向厂商请求模型列表：${listEndpoint}`);
    }
    const rawUrl = baseUrlIn.value.trim();
    const hasNormalizeNote = () => notes.some(item => /清洗|规范化|剥离/.test(item));
    if (normalized && normalized.replace(/\/+$/, '') !== rawUrl.replace(/\/+$/, '') && !hasNormalizeNote()) {
      notes.push(`已自动规范化地址为 ${normalized}（提交时将使用该地址）。`);
    }
    if (/\/(chat\/completions|messages|responses|models)\/?$/i.test(rawUrl) && !hasNormalizeNote()) {
      notes.push('检测到误粘贴的接口尾部路径，已自动清洗为服务根地址，避免重复拼接。');
    }
    if (res?.capabilities && typeof res.capabilities === 'object') {
      const caps = res.capabilities;
      const values = Object.values(caps);
      const isMap = values.length > 0 && values.every(value => value && typeof value === 'object');
      if (isMap) {
        const visionCount = values.filter(value => value.vision).length;
        if (visionCount) notes.push(`能力推断：${visionCount} 个模型支持视觉输入。`);
      } else {
        const pieces = [
          caps.stream === false ? '不支持流式' : null,
          caps.json === false ? '不支持 JSON 输出' : null,
          caps.vision ? '支持视觉' : null,
        ].filter(Boolean);
        if (pieces.length) notes.push(`能力推断：${pieces.join(' · ')}`);
      }
    }
    if (!models.length && res?.ok === false) {
      preview.append(notice(`未能拉取模型列表：${res?.diagnosis || '厂商未返回可用模型'}。可直接在下方手动填写模型 ID。`, 'warning'));
    }
    if (notes.length) {
      preview.append(el('p', { class: 'provider-preview-note' }, notes.join('；')));
    }
    if (!chatEndpoint && !notes.length) {
      preview.append(el('p', { class: 'help' }, '填写接口地址后，这里会显示实际请求端点与自动清洗结果。'));
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
      previewMount.replaceChildren(loading('正在生成规范化预览并拉取厂商模型…'));
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
            notice('当前后端尚未提供「保存前预览」接口（/api/providers/fetch-models-preview 未就绪），可先手动填写模型 ID，保存后仍可编辑。', 'warning'),
            modelsArea);
          updateModelCount();
          return;
        }
        previewMount.replaceChildren(notice(`规范化预览失败：${error.message}`, 'error'));
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
    icon('search'), '拉取厂商模型');
  fetchBtn.addEventListener('click', () => {
    if (!baseUrlIn.value.trim()) { showError('请先填写接口地址。'); return; }
    clearError();
    runPreview({ immediate: true });
  });

  const probeBtn = el('button', { type: 'button', class: 'button secondary', 'data-action': 'probe' },
    icon('check'), '保存前测试连接');
  probeBtn.addEventListener('click', () => busy(probeBtn, async () => {
    clearError();
    const model = pickModelForProbe();
    if (!model) { showError('请先拉取并勾选至少 1 个模型，再测试连接。'); return; }
    if (!baseUrlIn.value.trim()) { showError('请先填写接口地址。'); return; }
    probeResult.replaceChildren(loading('正在用最小请求探测连通性…'));
    try {
      const res = await rawApi('/providers/probe', { method: 'POST', data: draftPayload({ includeModel: true }) });
      if (res.ok === false) {
        probeResult.replaceChildren(notice(`连接未通过：${res.diagnosis || '厂商未返回可用响应'}`, 'error'));
        return;
      }
      const latency = Number(res.latency_ms);
      const parts = [
        `连接成功${Number.isFinite(latency) ? `，首字节约 ${Math.round(latency)} ms` : ''}`,
        res.model ? `模型：${res.model}` : `模型：${model}`,
        res.diagnosis ? `诊断：${res.diagnosis}` : '',
        res.request_url ? `请求：${res.request_url}` : '',
      ].filter(Boolean);
      probeResult.replaceChildren(notice(parts.join('；')));
    } catch (error) {
      if ((error.status === 404 || error.status === 405) && provider?.id) {
        // 旧后端兼容：编辑已有连接时退回旧的已保存连接测试。
        try {
          const legacy = await rawApi(`/providers/${provider.id}/test`, {
            method: 'POST', data: { model },
          });
          probeResult.replaceChildren(notice(`连接成功（旧接口）：${legacy.content || '模型已响应'}`, 'warning'));
          return;
        } catch (legacyError) {
          probeResult.replaceChildren(notice(`测试失败：${legacyError.message}`, 'error'));
          return;
        }
      }
      probeResult.replaceChildren(notice(`测试失败：${error.message}`, 'error'));
    }
  }));

  const saveBtn = el('button', { type: 'button', class: 'button', 'data-action': 'save' },
    icon('check'), editing ? '保存修改' : '保存连接');
  saveBtn.addEventListener('click', () => busy(saveBtn, async () => {
    clearError();
    const name = nameIn.value.trim();
    const baseUrl = baseUrlIn.value.trim();
    const models = [...selectedModels];
    if (!name) { showError('请填写配置名称。'); return; }
    if (!baseUrl || baseUrl.length < 8) { showError('请填写有效的接口地址（含协议）。'); return; }
    if (!models.length) { showError('请至少勾选或填写 1 个模型。'); return; }
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
          showError('未填写 API Key：本地无鉴权服务请在「高级连接选项」把鉴权方式选为「无鉴权（none）」；远程服务请填写 Key。');
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
      toast(editing ? '连接配置已更新。' : '模型连接已加密保存。');
      dlg.close();
      await ctx.afterProviderSaved(saved);
    } catch (error) {
      // 保存失败就地显示，不关闭弹窗，避免用户重填。
      showError(`保存失败：${error.message}`);
    }
  }));

  const cancelBtn = el('button', { type: 'button', class: 'button ghost', onClick: () => dlg.close() }, '取消');

  // 初始渲染
  renderSavedHeaders();
  updateAddHeaderState();
  renderModelList();
  syncModelsToTextarea();
  renderPreviewArea();

  const advancedBody = el('div', { class: 'pg-advanced-body' },
    el('div', { class: 'form-grid' },
      field('鉴权方式', authModeSel, '默认自动：按接口类型选择 Bearer / x-api-key 等鉴权头。'),
      field('超时秒数（15–600）', timeoutIn, '网络较慢的中转站可调大，最大 600 秒。')),
    el('div', { class: 'form-grid' },
      field('max_tokens 兼容策略', maxTokensSel, 'o1/o3/gpt-5 等模型不接受 max_tokens 时可显式切换。'),
      field('api-version（Azure 等）', apiVersionIn, '留空则由后端按服务类型决定。')),
    el('div', {},
      el('p', { class: 'field-label' }, `自定义请求头（最多 ${MAX_EXTRA_HEADERS} 条，保存后仅回显键名）`),
      headersMount, headerRowsMount, headerReplaceHint,
      el('div', { class: 'row', style: 'margin-top:8px' }, addHeaderBtn)),
    proxyRow, proxyHint);

  const advanced = el('details', { class: 'pg-advanced provider-advanced' },
    el('summary', {}, '高级连接选项 · 鉴权 / 请求头 / 超时 / 代理'),
    advancedBody);

  dlg.append(
    el('div', { class: 'row between', style: 'margin-bottom:14px' },
      el('h3', {}, icon('key'), editing ? `编辑连接 · ${provider.name}` : '添加模型连接'),
      el('button', { type: 'button', class: 'button ghost small', onClick: () => dlg.close(), 'aria-label': '关闭' }, '关闭')),
    el('div', { class: 'provider-dialog-body' },
      el('div', { class: 'form-grid' },
        field('厂商预设', presetSel, '顶部为常用自定义与本地分组；官方预设会带出推荐模型。'),
        field('配置名称', nameIn)),
      presetHint,
      el('div', { class: 'form-grid' },
        field('接口类型', kindSel, describeKindHint(kindSel.value)),
        field('接口地址', baseUrlIn, '可直接粘贴厂商文档地址；误粘贴的 /chat/completions 会自动清洗。')),
      previewMount,
      el('div', { class: 'form-grid' },
        field(editing ? '更换 API Key（可选）' : 'API Key（本地可留空）', apiKeyIn), keyHint),
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
  if (!detail?.id) { toast('请先保存连接后再编辑模型属性。', true); return; }
  const dlg = el('dialog', { class: 'modal-dialog', 'data-role': 'model-meta' });
  const displayName = input('显示名称', { value: detail.display_name || detail.model_id });
  const vChk = el('input', { type: 'checkbox', checked: Boolean(detail.caps?.vision) });
  const jChk = el('input', { type: 'checkbox', checked: detail.caps?.json !== false });
  const rChk = el('input', { type: 'checkbox', checked: Boolean(detail.caps?.reasoning) });
  const sChk = el('input', { type: 'checkbox', checked: detail.caps?.stream !== false });
  const enabledChk = el('input', { type: 'checkbox', checked: detail.enabled !== false });
  const ctxIn = input('例如 128000', {
    type: 'number', min: 0, step: 1000, value: detail.caps?.context ?? '',
  });
  const inPrice = input('例如 2.0（元/百万 tokens，留空则费用显示未知）', {
    type: 'number', step: '0.01', min: 0, value: detail.input_price ?? '',
  });
  const outPrice = input('例如 8.0（元/百万 tokens，留空则费用显示未知）', {
    type: 'number', step: '0.01', min: 0, value: detail.output_price ?? '',
  });

  dlg.append(
    el('div', { class: 'card-header' }, el('h3', {}, `模型能力与单价 · ${detail.model_id}`),
      button('关闭', () => dlg.close(), 'ghost small', 'close')),
    field('显示名称', displayName),
    el('div', { class: 'row', style: 'margin:14px 0' },
      el('label', { class: 'check-label' }, vChk, '👁 支持视觉（拍题/OCR）'),
      el('label', { class: 'check-label' }, jChk, '{} 支持结构化 JSON'),
      el('label', { class: 'check-label' }, rChk, '🧠 推理模型'),
      el('label', { class: 'check-label' }, sChk, '流式输出'),
      el('label', { class: 'check-label' }, enabledChk, '在模型选择器中启用')),
    el('div', { class: 'form-grid' },
      field('上下文长度（tokens，可选）', ctxIn),
      field('输入单价（元/1M tokens，可选）', inPrice),
      field('输出单价（元/1M tokens，可选）', outPrice)),
    el('div', { class: 'row end', style: 'margin-top:16px' },
      button('保存属性', e => busy(e.currentTarget, async () => {
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
        toast('模型能力标签与单价已更新。');
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
  const [, prefs, usage, live] = await Promise.all([
    refreshProviders(),
    ensurePreferences(true),
    api('/ai/usage?days=30').catch(() => ({ by_model: [], by_day: [], notice: '' })),
    api('/health/live').catch(() => null),
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
      toast(`默认模型保存失败：${error.message}`, true);
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
          ? '不固定（分歧时由核验模型 B 裁定）'
          : task === 'grade' ? '跟随讲解模型（可单独指定）' : '跟随页面模型选择器',
      },
      ...groups,
    ];
    const primaryVal = selectionOf(def, task);
    const verifyVal = verifySelectionOf(def);
    const primary = select(primaryOptions, primaryVal, {
      'data-field': 'task-primary', 'data-task': task, 'aria-label': `${TASK_LABELS[task]}主模型`,
    });
    const verify = select([{ value: '', label: '不指定核验模型（快速模式）' }, ...groups], verifyVal, {
      'data-field': 'task-verify', 'data-task': task, 'aria-label': `${TASK_LABELS[task]}核验模型`,
    });

    const providerText = el('span', {
      class: 'pg-default-provider pg-task-provider',
      'data-task-provider': task,
      'data-empty': def.primary_provider_id || def.judge_provider_id ? 'false' : 'true',
    }, taskDefaultProviderText(task, def));

    const clearBtn = el('button', {
      type: 'button', class: 'button ghost small pg-task-clear', 'data-action': 'clear-task', 'data-task': task,
      'aria-label': `清空${TASK_LABELS[task]}默认模型`,
    }, '清空');
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
        el('span', { class: 'help' }, '不适用：分歧时由核验模型 B 裁定'))
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
          el('p', { class: 'pg-kicker' }, 'API 状态与默认模型'),
          el('h2', {}, connectedCount ? `${connectedCount} 个 API 连接已保存` : '先连接一个模型'),
          el('p', { class: 'pg-api-summary' }, connectedCount
            ? `共 ${modelCount} 个可用模型。可直接在下方为每个任务指定默认模型，清空后跟随页面模型选择器。`
            : '还没有保存任何模型连接。AI 讲解、判分、OCR 与知识整理都需要你自己的 API。')),
        statusTag(connectedCount ? '可用' : '待配置', connectedCount ? 'ok' : 'warn', connectedCount ? '✓' : '!')),
      el('div', { class: 'pg-api-actions' },
        button(connectedCount ? '添加另一个模型连接' : '添加模型连接',
          () => openProviderDialog(null, ctx), 'secondary', 'plus'),
        connectedCount
          ? button('测试已有连接', () => ctx.providersGrid.scrollIntoView({ behavior: 'smooth', block: 'start' }), 'ghost', 'check')
          : null,
        el('button', { type: 'button', class: 'button ghost', onClick: () => openEmbeddingCard(ctx) }, '配置语义检索')),
      el('div', { class: 'pg-task-defaults-head' },
        el('h3', {}, '各任务默认模型'),
        el('p', { class: 'help' }, '行内修改即时保存；「清空」会真正写入空配置，恢复跟随页面选择。')),
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
      'aria-label': '测试使用的模型',
    });
    const testing = el('div');
    const modelBadges = el('div', { class: 'provider-models' });
    for (const detail of provider.model_details || []) {
      const icons = [detail.caps?.vision ? '👁' : '', detail.caps?.reasoning ? '🧠' : ''].filter(Boolean).join('');
      const label = `${detail.model_id}${icons ? ' ' + icons : ''}${detail.enabled === false ? '（已停用）' : ''}`;
      if (!detail.id) {
        modelBadges.append(el('span', { class: 'badge blue', title: '保存连接后可编辑模型属性' }, label));
        continue;
      }
      modelBadges.append(el('button', {
        type: 'button',
        class: 'badge blue clickable-badge',
        title: '点击编辑该模型能力标签、上下文与单价',
        'data-model-id': detail.model_id,
        onClick: () => openModelMetaDialog(provider, detail, ctx),
      }, label));
    }
    const compat = compatOf(provider);
    const hasKey = providerHasSavedKey(provider);
    const metaBadges = el('div', { class: 'provider-card-meta' },
      statusTag(hasKey ? '密钥已保存' : (authModeOf(provider) === 'none' ? '无鉴权连接' : '未配置密钥'),
        hasKey || authModeOf(provider) === 'none' ? 'ok' : 'warn',
        hasKey || authModeOf(provider) === 'none' ? '✓' : '!'),
      statusTag(`${(provider.models || []).length} 个模型`, 'info', '·'),
      authModeOf(provider) !== 'auto'
        ? statusTag(`鉴权：${(AUTH_MODE_LABELS[authModeOf(provider)] || authModeOf(provider)).split('（')[0]}`, 'info', '·')
        : null,
      isLocalUrl(provider.base_url) ? statusTag('本地 / 自建服务', 'ok', '·') : null);
    const headers = extraHeaderKeys(provider);
    const compatText = [
      compat.max_tokens_param && compat.max_tokens_param !== 'auto' ? `max_tokens：${compat.max_tokens_param}` : '',
      compat.timeout_seconds ? `超时 ${compat.timeout_seconds}s` : '',
      compat.use_proxy_env ? '走系统代理' : '',
      compat.api_version ? `api-version：${compat.api_version}` : '',
    ].filter(Boolean).join(' · ');

    const testBtn = button('测试连接', e => busy(e.currentTarget, async () => {
      const model = modelSel.value || (provider.models || [])[0];
      if (!model) { testing.replaceChildren(notice('该连接还没有模型，请先编辑添加模型。', 'warning')); return; }
      if (!providerHasSavedKey(provider) && authModeOf(provider) !== 'none' && !isLocalUrl(provider.base_url)) {
        testing.replaceChildren(notice('该连接尚未保存密钥，测试可能失败；可编辑连接补填 Key。', 'warning'));
      }
      testing.replaceChildren(loading('正在调用所选模型…'));
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
        testing.replaceChildren(notice(`连接成功${Number.isFinite(latency) ? `（${Math.round(latency)}ms）` : ''}：${res.diagnosis || '模型已响应'}`));
      } catch (error) {
        if (error.status === 404 || error.status === 405) {
          try {
            const res = await api(`/providers/${provider.id}/test`, { method: 'POST', data: { model } });
            testing.replaceChildren(notice(`连接成功：${res.content || '模型已响应'}（${res.notice || '旧接口'}）`));
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
          el('p', {}, `${providerKindLabel(provider.kind)} · ${isLocalUrl(provider.base_url) ? '本地地址' : '远程地址'}`)),
        button('编辑', () => openProviderDialog(provider, ctx), 'ghost small', 'edit')),
      el('div', { class: 'provider-key' }, el('span', {}, provider.key_hint || (hasKey ? '密钥已加密保存' : '无鉴权 / 未保存密钥')), icon('lock')),
      el('p', { class: 'help', style: 'margin-bottom:8px' }, provider.base_url),
      metaBadges,
      el('details', { class: 'pg-advanced provider-advanced' },
        el('summary', {}, `模型能力标签与单价（${(provider.model_details || []).length} 个模型）`),
        el('div', { class: 'pg-advanced-body' },
          el('p', { class: 'help' }, '点击模型标签，编辑该模型的视觉 / JSON / 推理能力、上下文与参考单价；保存只更新本卡片。'),
          modelBadges.childNodes.length
            ? modelBadges
            : el('p', { class: 'help' }, '还没有保存模型属性；保存连接后可在这里补充。'),
          headers.length
            ? el('div', {},
              el('p', { class: 'help' }, `自定义请求头（脱敏）：${headers.join('、')}`))
            : null,
          compatText ? el('p', { class: 'help' }, `连接兼容参数：${compatText}`) : null)),
      field('测试使用的模型', modelSel),
      el('div', { class: 'row between', style: 'margin-top:14px' },
        testBtn,
        button('删除配置', e => busy(e.currentTarget, async () => {
          if (!window.confirm(`删除“${provider.name}”的 API 配置？`)) return;
          await api(`/providers/${provider.id}`, { method: 'DELETE' });
          toast('模型配置已删除。');
          await ctx.afterProviderDeleted(provider.id);
        }), 'danger small', 'trash')),
      testing);
    return card;
  }

  ctx.renderProviders = () => {
    const children = state.providers.map(provider => buildProviderCard(provider));
    if (!children.length) {
      children.push(el('div', { class: 'card', style: 'grid-column:1/-1' },
        empty('先连接一个你自己的模型',
          '已内置 OpenAI、DeepSeek、通义千问、硅基流动、智谱 GLM、Kimi、火山方舟、OpenRouter、Claude、Gemini 预设，也支持自定义中转站与本地 Ollama / LM Studio / vLLM。',
          button('添加模型连接', () => openProviderDialog(null, ctx), 'soft small', 'plus'), 'key')));
    }
    ctx.providersGrid.replaceChildren(...children);
  };

  ctx.afterProviderSaved = async () => {
    try {
      await refreshProviders();
    } catch (error) {
      toast(`连接已保存，但刷新列表失败：${error.message}`, true);
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
  const addSubmit = el('button', { class: 'button', type: 'submit' }, icon('plus'), '打开连接向导');
  const addForm = el('form', { class: 'card provider-form' });
  addForm.append(
    el('div', { class: 'card-header' }, el('h2', {}, '添加模型连接'), badge('保存前可预览与测通', 'teal')),
    el('p', { class: 'help', style: 'margin-bottom:14px' },
      '在向导里选择厂商预设或自定义地址：会自动生成规范化请求端点、拉取全量厂商模型，并在保存前测试连通性。支持本地 Ollama / LM Studio / vLLM 与 Azure OpenAI。'),
    addSubmit);
  addForm.addEventListener('submit', event => {
    event.preventDefault();
    openProviderDialog(null, ctx);
  });

  /* ---------- 4. 复习穿插与偏好（保留高级折叠） ---------- */
  const interleaveSel = select([
    { value: '0', label: '关闭搜索结果穿插' },
    { value: '1', label: '每页穿插 1 道到期错题（第 4 位）' },
    { value: '2', label: '每页穿插 2 道到期错题（第 4、9 位，推荐）' },
  ], String(prefs.interleave_per_page ?? 2));
  const sameKpChk = el('input', { type: 'checkbox', checked: Boolean(prefs.same_kp_only) });
  const stripChk = el('input', { type: 'checkbox', checked: prefs.show_review_strip !== false });

  const prefsCard = el('section', { class: 'card pg-fold-card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, '复习穿插与搜索页偏好'),
      badge('高级参数', 'blue')),
    el('details', { class: 'pg-advanced' },
      el('summary', {}, '展开调整错题穿插频率与搜索页提示'),
      el('div', { class: 'pg-advanced-body' },
        el('div', { class: 'form-grid' },
          field('搜索页错题穿插频率', interleaveSel, '仅穿插符合当前筛选条件的到期错题，不改变检索总数。')),
        el('label', { class: 'check-label' }, sameKpChk, '只穿插与当前知识点相同的到期错题'),
        el('label', { class: 'check-label' }, stripChk, '在搜索页顶部同时保留独立「顺便温习」横条'),
        el('div', { class: 'row end', style: 'margin-top:16px' },
          button('保存偏好设置', e => busy(e.currentTarget, async () => {
            await api('/preferences', {
              method: 'PATCH',
              data: {
                interleave_per_page: Number(interleaveSel.value),
                same_kp_only: sameKpChk.checked,
                show_review_strip: stripChk.checked,
              },
            });
            toast('偏好设置已保存。');
            await ensurePreferences(true);
          }), '', 'check')))));

  /* ---------- 5. 语义检索（Embedding）配置卡片（懒加载） ---------- */
  const embCard = el('section', { class: 'card pg-fold-card pg-embedding-card' },
    el('div', { class: 'card-header' },
      el('h2', {}, '语义检索（Embedding）配置'),
      badge('可选', 'blue')));
  const embStatus = el('div', { class: 'pg-embedding-status' }, el('p', { class: 'help' }, '展开后可查看当前语义检索状态并测试向量接口。'));
  const embMode = select([
    { value: 'off', label: '关闭（回退本地词频检索）' },
    { value: 'inherit', label: '复用已有模型连接' },
    { value: 'custom', label: '自定义地址 + 模型 + Key' },
  ], 'off');
  const embProviderSel = select([{ value: '', label: '选择用于 Embedding 的连接' },
    ...(state.providers || []).map(provider => ({ value: String(provider.id), label: provider.name }))]);
  const embModelIn = input('例如 text-embedding-3-small / BAAI/bge-m3 / nomic-embed-text');
  const embBaseUrlIn = input('https://api.openai.com/v1');
  const embKeyIn = input('API Key（可留空）', { type: 'password', autocomplete: 'off' });
  const embReuseField = field('复用的模型连接', embProviderSel);
  const embCustomBox = el('div', { class: 'pg-embedding-custom hide' },
    field('Embedding 接口地址', embBaseUrlIn),
    field('Embedding API Key', embKeyIn));
  const embResult = el('div', { class: 'pg-embedding-result', 'aria-live': 'polite' });
  const embSaveBtn = button('保存 Embedding 配置', null, 'small');
  const embTestBtn = button('测试向量接口', null, 'secondary small');
  const embForm = el('div', { class: 'pg-embedding-form' },
    field('配置方式', embMode),
    embReuseField,
    field('Embedding 模型', embModelIn),
    embCustomBox,
    el('div', { class: 'row' }, embTestBtn, embSaveBtn),
    embResult);
  const embDetails = el('details', { class: 'pg-advanced' },
    el('summary', {}, '展开配置语义检索（自动向量召回）'),
    el('div', { class: 'pg-advanced-body' }, embStatus, embForm));
  embCard.append(embDetails);

  let embLoaded = false;
  let embConfig = null;

  function syncEmbeddingMode() {
    const mode = embMode.value;
    embReuseField.classList.toggle('hide', mode !== 'inherit');
    embCustomBox.classList.toggle('hide', mode !== 'custom');
    embModelIn.closest('.field')?.classList?.toggle('hide', mode === 'off');
  }
  embMode.addEventListener('change', syncEmbeddingMode);

  function populateEmbeddingProviders(items) {
    const list = Array.isArray(items) && items.length
      ? items
      : (state.providers || [])
        .filter(provider => ['openai', 'gemini', 'azure_openai'].includes(provider.kind))
        .map(provider => ({ id: provider.id, name: provider.name, models: provider.models || [] }));
    embProviderSel.replaceChildren(
      el('option', { value: '' }, '选择用于 Embedding 的连接'),
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
      if (!embLoaded) return statusTag('正在读取配置…', 'info', '…');
      if (mode === 'off' && source !== 'env') return statusTag('未启用（回退本地词频检索）', 'warn', '!');
      if (source === 'env') return statusTag('由环境变量提供', 'info', '·');
      if (source === 'inherit') return statusTag('已启用 · 复用模型连接', ready ? 'ok' : 'warn', ready ? '✓' : '!');
      if (source === 'custom') return statusTag('已启用 · 自定义向量接口', ready ? 'ok' : 'warn', ready ? '✓' : '!');
      if (cfg.env_configured) return statusTag('未启用（环境变量配置可回退）', 'warn', '!');
      if (ready) return statusTag('已启用', 'ok', '✓');
      return statusTag('未配置', 'warn', '!');
    })();
    const rows = [statusTagNode];
    const model = effective.model || cfg.model;
    if (model) rows.push(el('span', { class: 'help' }, `模型：${model}`));
    if (cfg.provider_id) {
      const provider = providerById(cfg.provider_id);
      rows.push(el('span', { class: 'help' }, `连接：${provider ? provider.name : `#${cfg.provider_id}`}`));
    }
    if (cfg.has_api_key || cfg.key_hint) rows.push(el('span', { class: 'help' }, '独立 Key 已加密保存'));
    if (cfg.env_configured && source !== 'env') {
      rows.push(el('span', { class: 'help' }, '环境变量 STUDY_EMBEDDING_* 已配置；账号配置优先。'));
    }
    if (Array.isArray(cfg.compatible_providers) && cfg.compatible_providers.length) {
      rows.push(el('span', { class: 'help' }, `可用于 Embedding 的连接：${cfg.compatible_providers.length} 个`));
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
    const mode = String(cfg?.mode || '').toLowerCase();
    if (mode === 'off' || (!mode && cfg?.effective_source === 'off')) embMode.value = 'off';
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
    embStatus.replaceChildren(loading('正在读取 Embedding 配置…'));
    const [cfgResult, readyResult] = await Promise.allSettled([
      rawApi('/providers/embeddings-config'),
      api('/health/ready').catch(() => null),
    ]);
    if (cfgResult.status === 'fulfilled') {
      applyEmbeddingConfig(cfgResult.value);
    } else if (cfgResult.reason?.status === 404 || cfgResult.reason?.status === 405) {
      embStatus.replaceChildren(notice('当前后端尚未提供 Embedding 配置接口，暂只能通过 STUDY_EMBEDDING_* 环境变量配置。', 'warning'));
    } else {
      embStatus.replaceChildren(notice(`读取 Embedding 配置失败：${cfgResult.reason?.message || '未知错误'}`, 'error'));
    }
    // 管理员 /health/ready 详情里可能带 embedding 状态：只展示状态字段，绝不渲染路径。
    const ready = readyResult.status === 'fulfilled' ? readyResult.value : null;
    const embReady = ready?.embedding || ready?.embeddings || ready?.checks?.embedding || ready?.checks?.embeddings
      || ready?.embedding_status || null;
    if (embReady) {
      const text = typeof embReady === 'string'
        ? embReady
        : [embReady.ready === false || embReady.configured === false ? '未就绪' : '就绪',
          embReady.detail,
          embReady.source && embReady.source !== 'none' ? `来源 ${embReady.source}` : '',
          embReady.model ? `模型 ${embReady.model}` : '',
          embReady.dimension ? `维度 ${embReady.dimension}` : '',
          embReady.recent_error?.message || embReady.recent_error?.detail || ''].filter(Boolean).join(' · ');
      embStatus.append(el('p', { class: 'help' }, `就绪检查：${text}`));
    }
  }

  embDetails.addEventListener('toggle', () => {
    if (embDetails.open) loadEmbeddingConfig();
  });

  embTestBtn.addEventListener('click', () => busy(embTestBtn, async () => {
    const payload = embeddingPayload();
    if (payload.mode === 'off') {
      embResult.replaceChildren(notice('当前为关闭状态；选择复用连接或自定义接口后再测试。', 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.provider_id) {
      embResult.replaceChildren(notice('请选择要复用的模型连接。', 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.model) {
      embResult.replaceChildren(notice('复用连接时还需填写 Embedding 模型名，例如 text-embedding-3-small。', 'warning'));
      return;
    }
    if (payload.mode === 'custom' && !payload.model) {
      embResult.replaceChildren(notice('请填写 Embedding 模型名称。', 'warning'));
      return;
    }
    embResult.replaceChildren(loading('正在调用向量接口…'));
    try {
      const res = await rawApi('/providers/embeddings-test', { method: 'POST', data: payload });
      if (res.ok === false) {
        embResult.replaceChildren(notice(`向量接口不可用：${res.diagnosis || res.error || '未知原因'}`, 'error'));
        return;
      }
      const parts = [
        res.diagnosis || '向量接口正常',
        res.model ? `模型：${res.model}` : '',
        res.dimension ? `维度：${res.dimension}` : '',
        Number.isFinite(Number(res.latency_ms)) ? `耗时：${Math.round(Number(res.latency_ms))} ms` : '',
        res.source ? `来源：${res.source}` : '',
      ].filter(Boolean);
      embResult.replaceChildren(notice(parts.join('；')));
    } catch (error) {
      if (error.status === 404 || error.status === 405) {
        embResult.replaceChildren(notice('当前后端尚未提供向量测试接口（/api/providers/embeddings-test 未就绪）。', 'warning'));
      } else {
        embResult.replaceChildren(notice(`向量接口测试失败：${error.message}`, 'error'));
      }
    }
  }));

  embSaveBtn.addEventListener('click', () => busy(embSaveBtn, async () => {
    const payload = embeddingPayload();
    if (payload.mode === 'inherit' && !payload.provider_id) {
      embResult.replaceChildren(notice('请选择要复用的模型连接。', 'warning'));
      return;
    }
    if (payload.mode === 'inherit' && !payload.model) {
      embResult.replaceChildren(notice('复用连接时还需填写 Embedding 模型名。', 'warning'));
      return;
    }
    if (payload.mode === 'custom' && (!payload.model || !payload.base_url)) {
      embResult.replaceChildren(notice('自定义模式需要同时填写接口地址与模型名称。', 'warning'));
      return;
    }
    embResult.replaceChildren(loading('正在保存 Embedding 配置…'));
    try {
      const res = await rawApi('/providers/embeddings-config', { method: 'PUT', data: payload });
      embConfig = res || payload;
      applyEmbeddingConfig(embConfig);
      embResult.replaceChildren(notice('Embedding 配置已保存。'));
      toast('语义检索配置已保存。');
    } catch (error) {
      if (error.status === 404 || error.status === 405) {
        embResult.replaceChildren(notice('当前后端尚未提供 Embedding 配置接口，请暂时使用 STUDY_EMBEDDING_* 环境变量。', 'warning'));
      } else {
        embResult.replaceChildren(notice(`保存失败：${error.message}`, 'error'));
      }
    }
  }));

  function openEmbeddingCard() {
    embCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (!embDetails.open) embDetails.open = true;
    loadEmbeddingConfig();
  }

  /* ---------- 6. Token 用量账本 ---------- */
  const usageCard = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, '近 30 天 Token 用量账本'),
      badge(`${usage.by_model?.length || 0} 个模型`)),
    el('p', { class: 'help', style: 'margin-bottom:14px' },
      usage.notice || '按模型统计真实调用 token；单价由用户自行填写，未填显示「未知」。'));
  if (usage.by_model?.length) {
    const tbl = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, '模型'), el('th', {}, '调用次数'), el('th', {}, '输入 Tokens'),
        el('th', {}, '输出 Tokens'), el('th', {}, '估算费用'))),
      el('tbody', {}, ...(usage.by_model || []).map(row => el('tr', {},
        el('td', {}, row.model),
        el('td', {}, number(row.calls)),
        el('td', {}, number(row.input_tokens)),
        el('td', {}, number(row.output_tokens)),
        el('td', {}, row.estimated_cost !== null && row.estimated_cost !== undefined
          ? `¥ ${row.estimated_cost}` : '未知（未填单价）')))));
    usageCard.append(el('div', { class: 'table-wrap' }, tbl));
  } else {
    usageCard.append(el('p', { class: 'help' }, '暂无用量记录。发起 AI 讲解、核验或笔记任务后会自动记账。'));
  }

  /* ---------- 7. 账号安全 ---------- */
  const curPwd = input('输入当前密码', { type: 'password', autocomplete: 'current-password' });
  const newPwd = input('输入新密码（至少 8 位）', { type: 'password', autocomplete: 'new-password' });
  const codesMount = el('div');
  const secCard = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, '账号安全与找回密码恢复码'), badge('单次有效', 'teal')),
    el('div', { class: 'form-grid' }, field('当前密码', curPwd), field('新密码（≥8 位）', newPwd)),
    el('div', { class: 'row between', style: 'margin-top:14px' },
      button('生成 8 组找回密码恢复码', e => busy(e.currentTarget, async () => {
        if (!window.confirm('生成新恢复码会使此前未使用的恢复码失效，继续？')) return;
        if (!curPwd.value) throw new Error('生成恢复码前请输入当前密码。');
        const res = await api('/auth/recovery-codes', {
          method: 'POST', data: { current_password: curPwd.value },
        });
        codesMount.replaceChildren(
          notice(res.notice, 'warning'),
          el('pre', { class: 'prompt-preview-pre' }, (res.codes || []).join('\n'))
        );
      }), 'secondary small', 'key'),
      button('修改密码', e => busy(e.currentTarget, async () => {
        if (!curPwd.value || newPwd.value.length < 8) throw new Error('请输入当前密码和至少 8 位新密码。');
        const res = await api('/auth/change-password', {
          method: 'POST', data: { current_password: curPwd.value, new_password: newPwd.value },
        });
        curPwd.value = ''; newPwd.value = '';
        toast(res.message || '密码已修改。');
      }), '', 'lock')),
    codesMount);

  container.replaceChildren(
    heading('MODEL CONNECTIONS', '选好你的学习搭档。',
      '先看 API 状态与各任务默认模型（可行内直接修改），再添加自定义中转站或本地模型；模型能力、预算与语义检索收在下方。'),
    apiStatusCard,
    ctx.providersGrid,
    addForm,
    prefsCard,
    embCard,
    usageCard,
    secCard
  );
  await renderJobHistory(container, version);
}
