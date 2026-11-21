# shigure（专注计时内核，vendored）

- 上游：<https://github.com/hoochanlon/shigure>（MIT License，Copyright (c) 2026 Hoochanlon）
- 固定提交：`5909fe7aa602bb99d9278ade9816ae1d62c5bf58`（2026-08-05）
- 本目录只保留 `timer.js`（107 行，原样未改）与上游 `LICENSE` 全文。

## 取了什么

`timer.js` 的 `Timer` 类：`start / pause / resume / stop / reset / restore / checkpoint`，
内部用 `plannedEndAt - Date.now()` 推算剩余时间，`setInterval(250ms)` 只做重绘。
这样后台标签页被节流、休眠后回来都不会漂移——这是本项目自己写计时器最容易写错的一点。

## 没取什么

上游的 Vditor 看板（`index.html` 与 `js/kanban.js` 从 jsDelivr 引 CDN）、壁纸、白噪音、
i18n、秒表、禅模式都没有引入：本项目要求「离线可用 + 无外部 CDN + CSP `script-src 'self'`」，
且只需要一个专注计时胶囊。UI 在 `web/js/components/focusTimer.js` 用本项目的 `el()` 重写。

## 使用方式

```js
import { Timer } from '../../vendor/shigure/timer.js';

const timer = new Timer(onTick, persistState, onTitleTick);
timer.start({ task: '导数专题', mode: 'focus', minutes: 25 });
```

`onTick(state)` 在开始/暂停/恢复/停止/到点（`status === 'completed'`）时回调；
`persistState(state|null)` 用于把状态落到 localStorage，刷新后可 `timer.restore(saved)` 续上。
本项目在此之上补了：结束/停止时 `POST /api/focus/sessions`（`client_id` 幂等）、
失败进待补队列、跨标签页只读跟随、`document.title` 同步剩余时间、WebAudio 提示音。

升级上游时请同时更新本文件的提交号与 `THIRD_PARTY_NOTICES.md` 第 8 节。
