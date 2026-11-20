import { api, onAuthExpired, refreshProviders, initBuildGuard, remountBuildBanner } from './core/api.js';
import { routes, state, loadUserFilters, clearUserSession } from './core/state.js';
import { $, el, icon, input, field, busy, toast } from './core/dom.js';
import { go, renderRoute, registerRenderers } from './core/router.js';
import { renderHome } from './pages/home.js';
import { renderPractice } from './pages/practice.js';
import { renderAdmission } from './pages/admission.js';
import { renderWrongbook } from './pages/wrongbook.js';
import { renderUpload } from './pages/upload.js';
import { renderNotes } from './pages/notes.js';
import { renderSettings } from './pages/settings.js';
import { renderAdmin } from './pages/admin.js';

const app = $('#app');

registerRenderers({
  home: renderHome,
  practice: renderPractice,
  admission: renderAdmission,
  wrongbook: renderWrongbook,
  upload: renderUpload,
  notes: renderNotes,
  settings: renderSettings,
  admin: renderAdmin,
});

function brand() {
  return el('div', { class: 'brand' },
    el('span', { class: 'brand-mark' }, '知'),
    el('div', {},
      el('div', { class: 'brand-name' }, '知序'),
      el('div', { class: 'brand-sub' }, 'AI 学习工作台')));
}

function renderResetPassword() {
  document.title = '找回密码 · 知序';
  const username = input('请输入用户名', { autocomplete: 'username', required: true, minlength: 2, maxlength: 40 });
  const code = input('例如：ABCD-1234-EF56', { autocomplete: 'off', required: true, minlength: 8, maxlength: 64 });
  const newPass = input('至少 8 位新密码', { type: 'password', autocomplete: 'new-password', required: true, minlength: 8, maxlength: 128 });
  const errors = el('div', { class: 'auth-error hide', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'button wide' }, '使用恢复码重置密码');
  const form = el('form', {
    onSubmit: async event => {
      event.preventDefault();
      errors.classList.add('hide');
      submit.disabled = true;
      try {
        await api('/auth/reset-password', {
          method: 'POST',
          data: { username: username.value.trim(), recovery_code: code.value.trim(), new_password: newPass.value },
        });
        toast('密码已重置，请使用新密码登录。');
        renderAuth(false);
      } catch (error) {
        errors.textContent = error.message;
        errors.classList.remove('hide');
      } finally {
        submit.disabled = false;
      }
    },
  }, field('用户名', username), field('一次性恢复码', code), field('新密码', newPass), errors, submit);

  app.replaceChildren(el('div', { class: 'auth-screen' },
    el('section', { class: 'auth-story' },
      brand(),
      el('div', {},
        el('div', { class: 'auth-headline' }, '离线也能安全找回账号。'),
        el('p', {}, '使用在「模型设置 · 账号安全」中保存的一次性恢复码重置密码，重置后旧会话自动失效。')),
      el('div', { class: 'auth-story-foot' }, '本地安全 · 恢复码一次有效')),
    el('section', { class: 'auth-form-area' },
      el('div', { class: 'auth-form-card' },
        el('div', { class: 'eyebrow' }, 'ACCOUNT RECOVERY'),
        el('h1', {}, '通过恢复码重置密码'),
        el('p', {}, '无需邮件服务器，输入保存过的任意一组恢复码即可。'),
        form,
        el('p', { class: 'auth-switch' },
          el('button', { class: 'text-button', type: 'button', onClick: () => renderAuth(false) }, '← 返回登录'))))));
}

function renderAuth(register = false) {
  document.title = `${register ? '创建账号' : '登录'} · 知序`;
  const username = input('请输入用户名', { autocomplete: 'username', required: true, minlength: 2, maxlength: 40 });
  const password = input(
    register ? '至少 8 位，建议包含字母和数字' : '请输入密码',
    { type: 'password', autocomplete: register ? 'new-password' : 'current-password', required: true, minlength: 8, maxlength: 128 }
  );
  const errors = el('div', { class: 'auth-error hide', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'button wide' }, register ? '创建我的学习账号' : '登录学习工作台');
  const authNote = el('p', { class: 'auth-note' },
    '本机初始化时，第一个注册的账号拥有内容管理权限。每个账号的作答、筛选、资料与 API 配置完全独立保存。');
  let switchButton = null;

  const form = el('form', {
    onSubmit: async event => {
      event.preventDefault();
      errors.classList.add('hide');
      submit.disabled = true;
      try {
        const response = await api(`/auth/${register ? 'register' : 'login'}`, {
          method: 'POST',
          data: { username: username.value.trim(), password: password.value },
        });
        state.user = response.user || response;
        password.value = '';
        state.catalog = null;
        loadUserFilters();
        await startSession();
      } catch (error) {
        errors.textContent = error.message;
        errors.classList.remove('hide');
      } finally {
        submit.disabled = false;
      }
    },
  }, field('用户名', username), field('密码', password), errors, submit);

  app.replaceChildren(el('div', { class: 'auth-screen' },
    el('section', { class: 'auth-story' },
      brand(),
      el('div', {},
        el('div', { class: 'auth-headline' }, '让每一次练习，', el('br'), '都有清晰的下一步。'),
        el('p', {}, '从一题的理解，到一章的掌握。把真题、错题与自己的资料，整理成有序的学习路径。')),
      el('div', { class: 'auth-story-foot' }, '真题练习 · 错题穿插 · 交叉核验 · AI 知识清单')),
    el('section', { class: 'auth-form-area' },
      el('div', { class: 'auth-form-card' },
        el('div', { class: 'eyebrow' }, 'YOUR LEARNING SPACE'),
        el('h1', {}, register ? '开始你的学习计划' : '欢迎回到知序'),
        el('p', {}, register ? '创建账号，保留属于自己的每一次进步。' : '今天，也往掌握多走一步。'),
        form,
        el('p', { class: 'auth-switch' },
          register ? '已有账号？ ' : '还没有账号？ ',
          switchButton = el('button', { class: 'text-button', type: 'button', onClick: () => renderAuth(!register) },
            register ? '立即登录' : '创建账号'),
          !register ? el('span', {}, ' · ', el('button', { class: 'text-button', type: 'button', onClick: () => renderResetPassword() }, '忘记密码？')) : null),
        authNote))));

  // 登录页渲染后再挂一次更新提示条（此前可能在主壳里、随 #app 替换被移除）。
  remountBuildBanner();

  // P2 公网策略：注册由运营显式开启；管理员初始化使用一次性口令。
  api('/auth/registration-policy').then(policy => {
    if (policy.public_mode) {
      authNote.textContent = policy.admin_ready
        ? '公网模式：注册由运营显式开启，账号、作答与 API 配置按用户隔离保存。'
        : '公网模式：请先由运营使用初始化口令创建管理员账号，再开放注册。';
    }
    if (policy.registration_open) return;
    if (register) {
      errors.textContent = '注册暂未开放。请联系管理员开通账号，或由运营在「内容管理」中开放注册后重试。';
      errors.classList.remove('hide');
      submit.disabled = true;
    } else if (switchButton) {
      switchButton.disabled = true;
      switchButton.textContent = '注册暂未开放';
      switchButton.title = '运营开启注册后可用';
    }
  }).catch(() => { /* 策略接口不可用时保持原有登录表单 */ });
}

// 主壳在窗口上注册的 resize / keydown 监听：登出或 401 过期时必须移除，
// 否则旧监听器会继续操作已经被替换掉的侧栏节点，也会跨账号残留状态。
let shellTeardown = null;

function teardownShell() {
  if (shellTeardown) {
    try { shellTeardown(); } catch { /* 清理失败不阻塞登出 */ }
    shellTeardown = null;
  }
}

onAuthExpired(() => {
  teardownShell();
  renderAuth(false);
});

function renderShell() {
  const sidebar = el('aside', { class: 'sidebar', id: 'app-sidebar' },
    brand(),
    el('div', { class: 'nav-label' }, '我的学习空间'));
  const backdrop = el('div', { class: 'sidebar-backdrop' });
  let mobileToggleBtn = null;

  function setSidebarOpen(open) {
    sidebar.classList.toggle('open', open);
    backdrop.classList.toggle('open', open);
    if (mobileToggleBtn) mobileToggleBtn.setAttribute('aria-expanded', String(open));
    // 手机上侧栏移出屏幕时必须退出焦点序列，否则键盘仍能聚焦「退出登录」等隐藏控件。
    const compact = window.matchMedia('(max-width: 700px)').matches;
    const hidden = compact && !open;
    sidebar.inert = hidden;
    if (hidden) sidebar.setAttribute('aria-hidden', 'true');
    else sidebar.removeAttribute('aria-hidden');
    const current = document.activeElement;
    if (hidden && current && sidebar.contains(current)) mobileToggleBtn?.focus();
  }
  // 视口变化时重新判定（桌面始终可见，手机默认收起）。
  const syncSidebarMode = () => {
    if (window.matchMedia('(max-width: 700px)').matches) setSidebarOpen(sidebar.classList.contains('open'));
    else {
      sidebar.inert = false;
      sidebar.removeAttribute('aria-hidden');
    }
  };
  window.addEventListener('resize', syncSidebarMode);
  syncSidebarMode();

  backdrop.addEventListener('click', () => setSidebarOpen(false));
  const escHandler = e => {
    if (e.key === 'Escape' && sidebar.classList.contains('open')) {
      setSidebarOpen(false);
      mobileToggleBtn?.focus();
    }
  };
  window.addEventListener('keydown', escHandler);
  shellTeardown = () => {
    window.removeEventListener('resize', syncSidebarMode);
    window.removeEventListener('keydown', escHandler);
  };

  const nav = el('nav', { class: 'nav', 'aria-label': '主导航' });
  for (const [key, [label, name]] of Object.entries(routes)) {
    if (key === 'admin' && state.user.role !== 'admin') continue;
    nav.append(el('button', {
      type: 'button',
      class: 'nav-item',
      'data-route': key,
      onClick: () => { setSidebarOpen(false); go(key); },
    }, icon(name), label));
  }

  sidebar.append(nav,
    el('div', { class: 'sidebar-bottom' },
      el('div', { class: 'sidebar-note' },
        icon('leaf'),
        el('strong', {}, '把知识，慢慢变成自己的'),
        el('p', {}, '不急着看答案。先独立尝试，再把不确定的地方弄明白。')),
      el('div', { class: 'user-menu' },
        el('span', { class: 'avatar' }, state.user.username.slice(0, 1).toUpperCase()),
        el('div', { class: 'user-info' },
          el('strong', {}, state.user.username),
          el('small', {}, state.user.role === 'admin' ? '内容管理员' : '学习者')),
        el('button', {
          type: 'button',
          class: 'icon-button',
          title: '退出登录',
          'aria-label': '退出登录',
          onClick: event => busy(event.currentTarget, async () => {
            try {
              await api('/auth/logout', { method: 'POST' });
            } finally {
              teardownShell();
              clearUserSession();
              renderAuth(false);
            }
          }),
        }, icon('logout')))));

  mobileToggleBtn = el('button', {
    type: 'button',
    class: 'icon-button mobile-toggle',
    'aria-label': '打开导航',
    'aria-controls': 'app-sidebar',
    'aria-expanded': 'false',
    onClick: () => setSidebarOpen(!sidebar.classList.contains('open')),
  }, icon('menu'));

  const topbar = el('header', { class: 'topbar' },
    el('div', { class: 'row' },
      mobileToggleBtn,
      el('div', { class: 'breadcrumb' },
        el('span', {}, '学习工作台'),
        el('span', { class: 'subtle-divider' }, '/'),
        el('strong', { id: 'breadcrumb-title' }, '学习概览'))),
    el('div', { class: 'topbar-right' },
      el('span', {}, new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })),
      el('span', { class: 'topbar-pill' }, icon('lock'), '独立学习空间'),
      el('span', { class: 'avatar' }, state.user.username.slice(0, 1).toUpperCase())));

  app.replaceChildren(
    el('div', { class: 'layout' },
      backdrop,
      sidebar,
      el('main', { class: 'main' },
        topbar,
        el('div', { class: 'content', id: 'main-content', tabindex: '-1' })))
  );
  // 登录页 ↔ 主壳切换会替换 #app：把「应用已更新」提示条重新挂到新壳层。
  remountBuildBanner();
}

async function startSession() {
  loadUserFilters();
  renderShell();
  try {
    await refreshProviders();
  } catch (error) {
    toast(`模型配置加载失败：${error.message}`, true);
  }
  await renderRoute();
}

window.addEventListener('hashchange', () => {
  if (state.user) renderRoute();
});

window.addEventListener('unhandledrejection', event => {
  event.preventDefault();
  toast(event.reason?.message || '操作未完成，请重试。', true);
});

(async function initialize() {
  // 先对齐构建号：之后任何 API 响应带上不同的 X-Zhixu-Build 时提示刷新旧标签页。
  await initBuildGuard();
  try {
    const user = await api('/auth/me');
    state.user = user.user || user;
    await startSession();
  } catch {
    renderAuth(false);
  }
})();
