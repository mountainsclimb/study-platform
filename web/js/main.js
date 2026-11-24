import { api, onAuthExpired, refreshProviders, initBuildGuard, remountBuildBanner } from './core/api.js';
import { routes, navGroups, state, loadUserFilters, clearUserSession, routeLabel, isPublicRoute } from './core/state.js';
import { $, el, icon, input, field, busy, toast } from './core/dom.js';
import { go, renderRoute, registerRenderers, parseHash } from './core/router.js';
import { renderHome } from './pages/home.js';
import { renderPractice } from './pages/practice.js';
import { renderPhysics } from './pages/physics.js';
import { renderAdmission } from './pages/admission.js';
import { renderWrongbook } from './pages/wrongbook.js';
import { renderUpload } from './pages/upload.js';
import { renderNotes } from './pages/notes.js';
import { renderSettings } from './pages/settings.js';
import { renderAdmin } from './pages/admin.js';
import { renderClassroom } from './pages/classroom.js';
import { renderMyBank } from './pages/mybank.js';
import { renderPaperlab } from './pages/paperlab.js';
import { renderInsight } from './pages/insight.js';
import { renderAssistant } from './pages/assistant.js';
import { renderTasks } from './pages/tasks.js';
import { renderPlaza } from './pages/plaza.js';
import { renderSubmission } from './pages/submission.js';
import { renderReview } from './pages/review.js';
import { renderPlazaAdmin } from './pages/plazaAdmin.js';
import { renderVocab } from './pages/vocab.js';
import { mountFocusWidget } from './components/focusTimer.js';
import { mountDailyQuoteTeaser, maybeShowDailyQuote } from './components/dailyQuote.js';
// 广场通知中心（Phase 3）：仅登录主壳顶栏挂载；访客壳不挂。接口未就绪时组件自行隐藏。
import { mountPlazaNotifications } from './components/plazaNotifications.js';
import { initTheme, cycleThemeMode, getThemeMode, getResolvedTheme, describeTheme } from './core/theme.js';
import {
  initI18n, t, getLocale, setLocale, formatDate, applyI18n,
} from './core/i18n.js';

const app = $('#app');

registerRenderers({
  home: renderHome,
  practice: renderPractice,
  physics: renderPhysics,
  admission: renderAdmission,
  wrongbook: renderWrongbook,
  upload: renderUpload,
  notes: renderNotes,
  settings: renderSettings,
  admin: renderAdmin,
  // 整合模块（OpenMAIC / Oh My Paper / 小鹿老师原生化）。
  classroom: renderClassroom,
  mybank: renderMyBank,
  paperlab: renderPaperlab,
  insight: renderInsight,
  vocab: renderVocab,
  assistant: renderAssistant,
  tasks: renderTasks,
  // 广场公开只读页（Phase 2）：访客与登录用户共用同一渲染器。
  plaza: renderPlaza,
  // 投稿/评审/编辑台：登录后工作台路由（访客壳下不可达；权限不足由页面内提示）。
  submission: renderSubmission,
  review: renderReview,
  'plaza-admin': renderPlazaAdmin,
});

function brand() {
  return el('div', { class: 'brand' },
    el('span', { class: 'brand-mark', 'data-no-i18n': '' }, '知'),
    el('div', {},
      // 「知序」是专有名词，中英文界面都保留中文品牌名。
      el('div', { class: 'brand-name', 'data-no-i18n': '' }, '知序'),
      el('div', { class: 'brand-sub', 'data-i18n': 'common.brandSub' }, t('common.brandSub'))));
}

// 语言切换按钮：顶栏 / 登录页共用。显示「中 / EN」，当前语言高亮；
// 44px 点击区、aria-label 说明动作，切换后由 zhixu:locale-changed 驱动重渲染。
function mountLanguageToggle() {
  const toggle = el('button', { type: 'button', class: 'icon-button lang-toggle', 'data-lang-toggle': '' });
  const sync = () => {
    const locale = getLocale();
    toggle.replaceChildren(
      el('span', { class: `lang-opt${locale === 'zh-CN' ? ' active' : ''}`, 'aria-hidden': 'true' }, '中'),
      el('span', { class: 'lang-sep', 'aria-hidden': 'true' }, '/'),
      el('span', { class: `lang-opt${locale === 'en-US' ? ' active' : ''}`, 'aria-hidden': 'true' }, 'EN'));
    const label = t('common.languageSwitch');
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
    toggle.dataset.locale = locale;
  };
  const onClick = () => setLocale(getLocale() === 'zh-CN' ? 'en-US' : 'zh-CN');
  const onLocaleChanged = () => {
    if (!toggle.isConnected) {
      document.removeEventListener('zhixu:locale-changed', onLocaleChanged);
      return;
    }
    sync();
  };
  toggle.addEventListener('click', onClick);
  document.addEventListener('zhixu:locale-changed', onLocaleChanged);
  sync();
  langToggleCleanup = () => {
    toggle.removeEventListener('click', onClick);
    document.removeEventListener('zhixu:locale-changed', onLocaleChanged);
  };
  return toggle;
}

// 登录页语言行：表单卡右上角，登录/注册/找回密码三种界面都可用。
function authLanguageRow() {
  return el('div', { class: 'auth-lang-row' }, mountLanguageToggle());
}

function renderResetPassword() {
  teardownShell();
  authScreen = 'reset';
  document.title = t('auth.reset.docTitle');
  const username = input(t('auth.usernamePlaceholder'), { autocomplete: 'username', required: true, minlength: 2, maxlength: 40 });
  const code = input(t('auth.reset.codePlaceholder'), { autocomplete: 'off', required: true, minlength: 8, maxlength: 64 });
  const newPass = input(t('auth.reset.newPasswordPlaceholder'), { type: 'password', autocomplete: 'new-password', required: true, minlength: 8, maxlength: 128 });
  const errors = el('div', { class: 'auth-error hide', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'button wide' }, t('auth.reset.submit'));
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
        toast(t('auth.reset.done'));
        renderAuth(false);
      } catch (error) {
        errors.textContent = error.message;
        errors.classList.remove('hide');
      } finally {
        submit.disabled = false;
      }
    },
  }, field(t('auth.username'), username), field(t('auth.recoveryCode'), code), field(t('auth.newPassword'), newPass), errors, submit);

  app.replaceChildren(el('div', { class: 'auth-screen' },
    el('section', { class: 'auth-story' },
      brand(),
      el('div', {},
        el('div', { class: 'auth-headline' }, t('auth.reset.storyHeadline')),
        el('p', {}, t('auth.reset.story'))),
      el('div', { class: 'auth-story-foot' }, t('auth.reset.foot'))),
    el('section', { class: 'auth-form-area' },
      el('div', { class: 'auth-form-card' },
        authLanguageRow(),
        el('div', { class: 'eyebrow' }, 'ACCOUNT RECOVERY'),
        el('h1', {}, t('auth.reset.heading')),
        el('p', {}, t('auth.reset.sub')),
        form,
        el('p', { class: 'auth-switch' },
          el('button', { class: 'text-button', type: 'button', onClick: () => renderAuth(false) }, t('auth.backToLogin')))))));
}


function renderAuth(register = false) {
  // 访客壳 → 登录页切换时先解绑顶栏语言/主题按钮等监听，避免旧壳残留。
  teardownShell();
  authScreen = register ? 'register' : 'login';
  document.title = register ? t('auth.register.docTitle') : t('auth.login.docTitle');
  const username = input(t('auth.usernamePlaceholder'), { autocomplete: 'username', required: true, minlength: 2, maxlength: 40 });
  const password = input(
    register ? t('auth.register.passwordPlaceholder') : t('auth.passwordPlaceholder'),
    { type: 'password', autocomplete: register ? 'new-password' : 'current-password', required: true, minlength: 8, maxlength: 128 }
  );
  const errors = el('div', { class: 'auth-error hide', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'button wide' },
    register ? t('auth.register.submit') : t('auth.login.submit'));
  const authNote = el('p', { class: 'auth-note' }, t('auth.note'));
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
        // 从访客壳登录后回到当前 hash（可能是广场列表或某道公开题详情）。
        state.guest = false;
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
  }, field(t('auth.username'), username), field(t('auth.password'), password), errors, submit);

  app.replaceChildren(el('div', { class: 'auth-screen' },
    el('section', { class: 'auth-story' },
      brand(),
      el('div', {},
        el('div', { class: 'auth-headline' }, t('auth.story.headline1'), el('br'), t('auth.story.headline2')),
        el('p', {}, t('auth.story.body'))),
      el('div', { class: 'auth-story-foot' }, t('auth.story.foot'))),
    el('section', { class: 'auth-form-area' },
      el('div', { class: 'auth-form-card' },
        authLanguageRow(),
        el('div', { class: 'eyebrow' }, 'YOUR LEARNING SPACE'),
        el('h1', {}, register ? t('auth.register.heading') : t('auth.login.heading')),
        el('p', {}, register ? t('auth.register.sub') : t('auth.login.sub')),
        form,
        el('p', { class: 'auth-switch' },
          register ? t('auth.haveAccount') : t('auth.noAccount'),
          switchButton = el('button', { class: 'text-button', type: 'button', onClick: () => renderAuth(!register) },
            register ? t('auth.toLogin') : t('auth.toRegister')),
          !register ? el('span', {}, ' · ', el('button', { class: 'text-button', type: 'button', onClick: () => renderResetPassword() }, t('auth.forgot'))) : null),
        // 从访客壳进入登录/注册时保留「返回广场」入口；hash 不变，登录成功后仍回到原页面。
        state.guest
          ? el('p', { class: 'auth-switch auth-guest-back' },
            el('button', {
              class: 'text-button',
              type: 'button',
              'data-guest-back': '',
              onClick: () => backToGuestPlaza(),
            }, t('plaza.guest.backToPlaza')))
          : null,
        authNote))));

  // 登录页渲染后再挂一次更新提示条（此前可能在主壳里、随 #app 替换被移除）。
  remountBuildBanner();

  // P2 公网策略：注册由运营显式开启；管理员初始化使用一次性口令。
  api('/auth/registration-policy').then(policy => {
    if (policy.public_mode) {
      authNote.textContent = policy.admin_ready
        ? t('auth.policy.publicModeReady')
        : t('auth.policy.publicModeSetup');
    }
    if (policy.registration_open) return;
    if (register) {
      errors.textContent = t('auth.registrationClosed');
      errors.classList.remove('hide');
      submit.disabled = true;
    } else if (switchButton) {
      switchButton.disabled = true;
      switchButton.textContent = t('auth.registrationClosedShort');
      switchButton.title = t('auth.registrationClosedTitle');
    }
  }).catch(() => { /* 策略接口不可用时保持原有登录表单 */ });
}

// 主壳在窗口上注册的 resize / keydown 监听：登出或 401 过期时必须移除，
// 否则旧监听器会继续操作已经被替换掉的侧栏节点，也会跨账号残留状态。
let shellTeardown = null;
// 顶栏专注计时组件的清理函数：登出/切壳时必须停表并移除监听，避免旧壳的定时器继续跑。
let focusWidgetCleanup = null;
// 顶栏主题切换按钮的事件解绑函数（auto → light → dark 循环）。
let themeToggleCleanup = null;
// 顶栏语言按钮的事件解绑函数（中 ↔ EN）。
let langToggleCleanup = null;
// 顶栏广场通知铃铛的清理函数：停轮询/移除监听，避免登出后继续请求通知接口。
let notificationCleanup = null;
// 当前登录页的形态：login | register | reset（切换语言时原样重渲染）。
let authScreen = 'login';

function teardownShell() {
  if (focusWidgetCleanup) {
    try { focusWidgetCleanup(); } catch { /* 计时器清理失败不阻塞登出 */ }
    focusWidgetCleanup = null;
  }
  if (themeToggleCleanup) {
    try { themeToggleCleanup(); } catch { /* 主题按钮清理失败不阻塞登出 */ }
    themeToggleCleanup = null;
  }
  if (langToggleCleanup) {
    try { langToggleCleanup(); } catch { /* 语言按钮清理失败不阻塞登出 */ }
    langToggleCleanup = null;
  }
  if (notificationCleanup) {
    try { notificationCleanup(); } catch { /* 通知轮询清理失败不阻塞登出 */ }
    notificationCleanup = null;
  }
  document.querySelectorAll('.dq-dialog').forEach(dialog => dialog.remove());
  if (shellTeardown) {
    try { shellTeardown(); } catch { /* 清理失败不阻塞登出 */ }
    shellTeardown = null;
  }
}

onAuthExpired(() => {
  teardownShell();
  renderAuth(false);
});

// 顶栏主题切换按钮：auto → light → dark 循环。
// 图标显示当前生效主题（浅色=太阳 / 暗色=月亮），aria-label 与 title 说明
// 当前模式、当前生效主题和下一步动作；系统偏好或其它标签页变化时同步刷新。
function mountThemeToggle() {
  const toggle = el('button', {
    type: 'button',
    class: 'icon-button theme-toggle',
    'data-theme-toggle': '',
  });
  const sync = () => {
    const mode = getThemeMode();
    const resolved = getResolvedTheme();
    const label = describeTheme(mode);
    toggle.replaceChildren(icon(resolved === 'dark' ? 'moon' : 'sun'));
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
    toggle.dataset.themeMode = mode;
    toggle.dataset.themeResolved = resolved;
  };
  const onClick = () => { cycleThemeMode(); sync(); };
  toggle.addEventListener('click', onClick);
  // boot 与 core/theme 都会广播该事件（用户切换 / 系统跟随 / 跨标签页同步）。
  document.addEventListener('zhixu:theme-changed', sync);
  sync();
  themeToggleCleanup = () => {
    toggle.removeEventListener('click', onClick);
    document.removeEventListener('zhixu:theme-changed', sync);
  };
  return toggle;
}

function renderShell() {
  // 侧栏：品牌 + 按「学习 / 内容 / 工具」分组的导航（分组标签见 navGroups）。
  const sidebar = el('aside', { class: 'sidebar', id: 'app-sidebar' }, brand());
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

  const nav = el('nav', {
    class: 'nav',
    'data-i18n-aria-label': 'shell.mainNav',
    'aria-label': t('shell.mainNav'),
    // 分组后条目略增：允许侧栏导航自身滚动，矮屏/手机上仍能到达底部条目。
    style: 'flex:1 1 auto;min-height:0;overflow-y:auto',
  });
  // 导航按「学习 / 内容 / 工具」分组渲染（分组元数据在 core/state.js）。
  // 桌面与移动端共用同一份 DOM；admin 继续按角色隐藏；评审台/编辑台按广场能力隐藏，
  // 未注册路由不会出现。
  const plazaCaps = state.plazaCapabilities || {};
  for (const group of navGroups) {
    const entries = group.routes.filter(key => {
      if (!routes[key]) return false;
      if (key === 'admin') return state.user.role === 'admin';
      if (key === 'review') return Boolean(plazaCaps.review || plazaCaps.editor);
      if (key === 'plaza-admin') return Boolean(plazaCaps.editor);
      return true;
    });
    if (!entries.length) continue;
    const groupNode = el('div', { class: 'nav-group', 'data-nav-group': group.key, style: 'display:grid;gap:6px' },
      el('div', { class: 'nav-label', 'data-i18n': group.labelKey }, t(group.labelKey)));
    for (const key of entries) {
      const [label, name] = routes[key];
      groupNode.append(el('button', {
        type: 'button',
        class: 'nav-item',
        'data-route': key,
        onClick: () => { setSidebarOpen(false); go(key); },
      }, icon(name), el('span', { 'data-i18n': `nav.${key}` }, label)));
    }
    nav.append(groupNode);
  }

  // 侧栏底部换成「今日一句」：点击可重新打开日签弹窗（数据缺失时保留原提示文案）。
  // 日签内容来自后端（数据，不翻译），用 data-no-i18n 标记；提示文案本身走 t()。
  const quoteTeaser = el('p', { 'data-no-i18n': '' }, t('shell.quote'));
  const roleEl = el('small', {}, state.user.role === 'admin' ? t('common.roleAdmin') : t('common.roleLearner'));
  sidebar.append(nav,
    el('div', { class: 'sidebar-bottom' },
      el('div', { class: 'sidebar-note' },
        icon('leaf'),
        el('strong', { 'data-i18n': 'shell.dailyQuote' }, t('shell.dailyQuote')),
        quoteTeaser),
      el('div', { class: 'user-menu' },
        el('span', { class: 'avatar', 'data-no-i18n': '' }, state.user.username.slice(0, 1).toUpperCase()),
        el('div', { class: 'user-info' },
          el('strong', { 'data-no-i18n': '' }, state.user.username),
          roleEl),
        el('button', {
          type: 'button',
          class: 'icon-button',
          'data-i18n-title': 'shell.logout',
          'data-i18n-aria-label': 'shell.logout',
          title: t('shell.logout'),
          'aria-label': t('shell.logout'),
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
    'aria-label': t('shell.openNav'),
    'aria-controls': 'app-sidebar',
    'aria-expanded': 'false',
    onClick: () => setSidebarOpen(!sidebar.classList.contains('open')),
  }, icon('menu'));

  // 顶栏右侧：日期 → 专注胶囊 → 语言切换 → 主题切换 → 独立空间胶囊 → 头像。
  // 专注胶囊放在日期之后，既有「≤900px 隐藏日期(首个 span)」的规则继续生效，
  // 手机上顶栏先看到计时，而不是被日期挤成两行。
  const todayStamp = el('span', {}, formatDate(new Date(), { month: 'long', day: 'numeric', weekday: 'long' }));
  const focusSlot = el('div', { class: 'fc-slot' });
  // 广场通知铃铛槽位：登录用户才挂载（访客壳不渲染主壳顶栏，因此天然不显示）。
  const notificationSlot = el('span', { class: 'notify-slot', 'data-plaza-notify-slot': '' });
  const languageToggle = mountLanguageToggle();
  const themeToggle = mountThemeToggle();

  const topbar = el('header', { class: 'topbar' },
    el('div', { class: 'row' },
      mobileToggleBtn,
      el('div', { class: 'breadcrumb' },
        el('span', { 'data-i18n': 'shell.workspace' }, t('shell.workspace')),
        el('span', { class: 'subtle-divider' }, '/'),
        el('strong', { id: 'breadcrumb-title' }, routeLabel(state.route)))),
    el('div', { class: 'topbar-right' },
      todayStamp,
      focusSlot,
      notificationSlot,
      languageToggle,
      themeToggle,
      el('span', { class: 'topbar-pill' },
        icon('lock'),
        el('span', { 'data-i18n': 'shell.isolatedSpace' }, t('shell.isolatedSpace'))),
      el('span', { class: 'avatar', 'data-no-i18n': '' }, state.user.username.slice(0, 1).toUpperCase())));

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
  // 顶栏专注计时 + 侧栏「今日一句」：壳层重建后重新挂载（旧实例先清理）。
  if (focusWidgetCleanup) {
    try { focusWidgetCleanup(); } catch { /* ignore */ }
    focusWidgetCleanup = null;
  }
  focusWidgetCleanup = mountFocusWidget(focusSlot);
  mountDailyQuoteTeaser(quoteTeaser);
  // 广场通知铃铛：登录用户顶栏；接口未就绪时组件自行隐藏。壳层重建前先清理旧实例。
  if (notificationCleanup) {
    try { notificationCleanup(); } catch { /* ignore */ }
    notificationCleanup = null;
  }
  notificationSlot.replaceChildren();
  notificationCleanup = mountPlazaNotifications(notificationSlot);

  // 语言切换：壳层的日期/角色/移动端导航 aria 是动态格式化结果，需显式同步；
  // 其余静态文案由 applyI18n(document) 处理（切语言后 main.js 统一重渲染当前路由）。
  const syncShellI18n = () => {
    todayStamp.textContent = formatDate(new Date(), { month: 'long', day: 'numeric', weekday: 'long' });
    roleEl.textContent = state.user.role === 'admin' ? t('common.roleAdmin') : t('common.roleLearner');
    mobileToggleBtn.setAttribute('aria-label', t('shell.openNav'));
  };
  document.addEventListener('zhixu:locale-changed', syncShellI18n);
  const previousShellTeardown = shellTeardown;
  shellTeardown = () => {
    document.removeEventListener('zhixu:locale-changed', syncShellI18n);
    previousShellTeardown?.();
  };
}

/* ------------------------------------------------------------------------- *
 * 访客壳（Phase 2）：未登录访客浏览广场公开页时的最小壳层。
 * 只保留「品牌 + 广场入口 + 语言/主题切换 + 登录/注册」；
 * 不渲染个人导航（练习/我的题库/错题/任务等）与个人组件（专注、每日一句、AI 工作台）。
 * ------------------------------------------------------------------------- */
function renderGuestShell() {
  teardownShell();
  state.user = null;
  const plazaEntry = el('button', {
    type: 'button',
    class: 'guest-nav-item',
    'data-route': 'plaza',
    'data-guest-plaza': '',
    onClick: () => go('plaza'),
  },
  icon('layers'),
  el('span', { 'data-i18n': 'nav.plaza' }, t('nav.plaza')));

  const loginButton = el('button', {
    type: 'button',
    class: 'button secondary small guest-auth-button',
    'data-guest-login': '',
    onClick: () => renderAuth(false),
  }, icon('lock'), el('span', { 'data-i18n': 'plaza.guest.login' }, t('plaza.guest.login')));

  const registerButton = el('button', {
    type: 'button',
    class: 'button small guest-auth-button',
    'data-guest-register': '',
    onClick: () => renderAuth(true),
  }, el('span', { 'data-i18n': 'plaza.guest.register' }, t('plaza.guest.register')));

  const topbar = el('header', { class: 'topbar guest-topbar' },
    el('div', { class: 'row guest-topbar-left' },
      brand(),
      el('nav', { class: 'guest-nav', 'aria-label': t('plaza.guest.navAria') }, plazaEntry)),
    el('div', { class: 'topbar-right guest-topbar-actions' },
      mountLanguageToggle(),
      mountThemeToggle(),
      loginButton,
      registerButton));

  app.replaceChildren(el('div', { class: 'layout guest-layout', 'data-guest-shell': '' },
    el('main', { class: 'main guest-main' },
      topbar,
      el('div', { class: 'content guest-content', id: 'main-content', tabindex: '-1' }))));
  // 访客壳替换 #app 后重新挂「应用已更新」提示条（与登录页/主壳同一入口）。
  remountBuildBanner();
  // 访客壳没有窗口级监听；语言/主题按钮的清理由 teardownShell 在切壳时统一处理。
  shellTeardown = null;
}

/** 进入/刷新访客壳并渲染当前公开路由；非公开路由由调用方转登录页。 */
async function startGuestSession() {
  state.guest = true;
  state.user = null;
  renderGuestShell();
  await renderRoute();
}

/**
 * 从登录/注册页返回广场：先把 hash 归位到公开路由（访客手改到非公开路由时
 * hash 仍停在目标页），再进入访客壳，避免落在空白的非公开 hash 上。
 */
function backToGuestPlaza() {
  if (!isPublicRoute(parseHash().route)) {
    if (window.history && typeof window.history.replaceState === 'function') {
      window.history.replaceState(null, '', '#/plaza');
    } else {
      location.hash = '#/plaza';
    }
  }
  void startGuestSession();
}

async function startSession() {
  // 登录成功后退出访客态，避免公开页的访客标记影响后续个人页面渲染。
  state.guest = false;
  loadUserFilters();
  // 广场能力（评审台/编辑台导航入口是否显示）；取不到时按无能力处理，不影响主流程。
  try {
    const plazaMe = await api('/plaza/me');
    state.plazaCapabilities = plazaMe.capabilities || {};
  } catch {
    state.plazaCapabilities = {};
  }
  renderShell();
  try {
    await refreshProviders();
  } catch (error) {
    toast(t('shell.providerLoadFailed', { message: error.message }), true);
  }
  await renderRoute();
  // 每天首次进入工作台弹一次「今日一句」；不阻塞首屏，取不到数据就静默跳过。
  maybeShowDailyQuote();
}

/* 语言切换统一入口：
 * - 登录页：原样重渲染当前形态（登录/注册/找回密码）；
 * - 主壳 / 访客壳内：重渲染当前路由（hash 不变），并尽量恢复切换前的滚动位置；
 * - 无论哪边：先 translate 一遍带 data-i18n 的静态节点（侧栏/顶栏等壳层）。 */
document.addEventListener('zhixu:locale-changed', async () => {
  applyI18n(document);
  if ((state.user || state.guest) && $('#main-content')) {
    const scrollY = window.scrollY;
    try {
      await renderRoute();
    } finally {
      if (window.scrollY !== scrollY) window.scrollTo({ top: scrollY, left: 0, behavior: 'auto' });
    }
  } else if (!state.user) {
    if (authScreen === 'reset') renderResetPassword();
    else renderAuth(authScreen === 'register');
  }
});

window.addEventListener('hashchange', () => {
  if (state.user) {
    renderRoute();
    return;
  }
  if (!state.guest) return;
  // 访客态：公开路由继续在访客壳内渲染（含广场详情 #/plaza?id=...）；
  // 手改 hash 到非公开路由时回登录页，登录成功后仍按该 hash 回到目标页面。
  const { route } = parseHash();
  if (isPublicRoute(route)) {
    if ($('#main-content')) renderRoute();
    else startGuestSession();
    return;
  }
  renderAuth(false);
});

window.addEventListener('unhandledrejection', event => {
  event.preventDefault();
  toast(event.reason?.message || t('common.actionFailed'), true);
});

(async function initialize() {
  // 语言：先加载中英文语言包，保证登录/主壳首屏就用正确语言渲染。
  await initI18n();
  // 主题：boot 已在首屏前应用；这里对齐运行期状态并接上系统主题监听。
  try { initTheme(); } catch { /* 主题初始化失败不阻塞登录/主壳 */ }
  // 先对齐构建号：之后任何 API 响应带上不同的 X-Zhixu-Build 时提示刷新旧标签页。
  await initBuildGuard();
  try {
    const user = await api('/auth/me');
    state.user = user.user || user;
    state.guest = false;
    await startSession();
  } catch {
    // 未登录：公开路由直接进入访客壳（可浏览广场），其余路由进登录页。
    // hash 保持不变，登录成功后 startSession 仍按它渲染，回到原页面/原公开题。
    const { route } = parseHash();
    if (isPublicRoute(route)) await startGuestSession();
    else renderAuth(false);
  }
})();
