import { api } from '../core/api.js';
import { el, button, busy, toast } from '../core/dom.js';

export function createPromptGrid(gridSpec, { initialConfig = null, onChange = null } = {}) {
  const dims = gridSpec.dimensions || {};
  const defaultConfig = {
    format: dims.format?.default || 'checklist',
    stage: dims.stage?.default || 'gaokao_r1',
    depth: dims.depth?.default || 'standard',
    focus: dims.focus?.default || [],
    extras: dims.extras?.default || [],
    style: dims.style?.default || 'textbook',
  };
  const config = { ...defaultConfig, ...initialConfig };

  let subject = '';
  let customReq = '';
  const root = el('div', { class: 'prompt-grid-composer' });
  const dimContainers = el('div', { class: 'prompt-grid-dims' });
  const previewContent = el('pre', { class: 'prompt-preview-pre' }, '点击展开查看拼装后的提示词要求（L1–L4）…');
  const previewDetails = el('details', { class: 'sources', style: 'margin-top:14px' },
    el('summary', {}, '最终要求预览（可展开核对 L1–L4，安全规则 L0 由系统固定保护）'),
    previewContent);

  const tplBar = el('div', { class: 'row between', style: 'margin-bottom:14px' });
  const tplList = el('div', { class: 'row' });

  async function refreshTemplates() {
    try {
      const res = await api('/prompt-templates');
      tplList.replaceChildren();
      for (const tpl of res.items || []) {
        const chip = el('div', { class: 'tpl-chip' },
          el('button', {
            type: 'button',
            class: 'tpl-load-btn',
            onClick: () => {
              Object.assign(config, defaultConfig, tpl.config || {});
              customReq = tpl.custom_requirements || '';
              renderGrid();
              updatePreview();
              if (onChange) onChange({ config: getConfig(), customRequirements: customReq });
              toast(`已载入模板「${tpl.name}」`);
            },
          }, `${tpl.is_recommended ? '推荐 · ' : tpl.is_shared ? '已共享 · ' : ''}${tpl.name}`),
          tpl.own
            ? el('button', {
                type: 'button',
                class: 'tpl-load-btn',
                onClick: e => busy(e.currentTarget, async () => {
                  const shared = !tpl.is_shared;
                  if (shared && !window.confirm('模板名称、方格选项和自定义要求将对其他登录用户可见。确认公开分享？')) return;
                  await api(`/prompt-templates/${tpl.id}/share`, { method: 'PUT', data: { shared } });
                  toast(shared ? '模板已公开分享。' : '已取消分享。');
                  await refreshTemplates();
                }),
              }, tpl.is_shared ? '取消分享' : '公开分享')
            : null,
          tpl.own
            ? el('button', {
                type: 'button',
                class: 'tpl-del-btn',
                title: '删除我的模板',
                onClick: e => busy(e.currentTarget, async () => {
                  await api(`/prompt-templates/${tpl.id}`, { method: 'DELETE' });
                  await refreshTemplates();
                }),
              }, '×')
            : null);
        tplList.append(chip);
      }
      if (!tplList.childNodes.length) {
        tplList.append(el('span', { class: 'help' }, '把常用方格组合存为模板，下次一键复用。'));
      }
    } catch (err) {
      tplList.replaceChildren(el('p', { class: 'help' }, `模板加载失败：${err.message}`));
    }
  }

  const saveTplBtn = button('存为我的模板', event => busy(event.currentTarget, async () => {
    const name = window.prompt('给这套提示词组合起个名字（例如：高考数学导数一轮清单）：');
    if (!name || !name.trim()) return;
    await api('/prompt-templates', {
      method: 'POST',
      data: { name: name.trim(), config: getConfig(), custom_requirements: customReq },
    });
    toast('已保存到「我的模板」。');
    await refreshTemplates();
  }), 'ghost small', 'plus');

  tplBar.append(tplList, saveTplBtn);

  async function updatePreview() {
    if (!previewDetails.open) return;
    try {
      const res = await api('/prompt-grid/preview', {
        method: 'POST',
        data: { config: getConfig(), subject: subject || null, requirements: customReq },
      });
      previewContent.textContent = res.preview || '';
    } catch (err) {
      previewContent.textContent = `预览生成失败：${err.message}`;
    }
  }
  previewDetails.addEventListener('toggle', () => { if (previewDetails.open) updatePreview(); });

  function renderGrid() {
    dimContainers.replaceChildren();
    for (const [dimKey, dimSpec] of Object.entries(dims)) {
      const row = el('div', { class: 'grid-dim-row' },
        el('div', { class: 'grid-dim-label' }, dimSpec.label));
      const cardsWrap = el('div', { class: 'grid-cards' });
      for (const opt of dimSpec.options || []) {
        const isActive = dimSpec.multi
          ? (config[dimKey] || []).includes(opt.id)
          : config[dimKey] === opt.id;
        cardsWrap.append(el('button', {
          type: 'button',
          class: `grid-card${isActive ? ' active' : ''}`,
          'aria-pressed': String(isActive),
          title: opt.desc || opt.prompt || opt.name,
          onClick: () => {
            if (dimSpec.multi) {
              const arr = new Set(config[dimKey] || []);
              if (arr.has(opt.id)) arr.delete(opt.id);
              else arr.add(opt.id);
              config[dimKey] = [...arr];
            } else {
              config[dimKey] = opt.id;
            }
            renderGrid();
            updatePreview();
            if (onChange) onChange({ config: getConfig() });
          },
        },
          el('strong', {}, opt.name),
          opt.desc ? el('small', {}, opt.desc) : null));
      }
      row.append(cardsWrap);
      dimContainers.append(row);
    }
  }

  function getConfig() {
    return JSON.parse(JSON.stringify(config));
  }

  root.append(tplBar, dimContainers, previewDetails);
  renderGrid();
  refreshTemplates();

  return {
    node: root,
    getConfig,
    setSubject(s) { subject = s || ''; updatePreview(); },
    setRequirements(r) { customReq = r || ''; updatePreview(); },
  };
}
