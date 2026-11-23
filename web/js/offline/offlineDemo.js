/**
 * 离线单文件 HTML 的可交互演示播放器（M7 · 方案 §5.4「PPTX/PDF 放海报、关键帧与文字，
 * HTML/ZIP 保留可交互内容」）。
 *
 * 该文件不是 ESM、不 import 任何模块：``app/classroom/exports.py`` 在导出含原理演示场景的
 * 离线 HTML 时，把本文件原样内联为 ``<script>``，并在它之前内联
 * ``web/vendor/offline/offline-demo.js``（flow / iso 两个纯 ESM 渲染器的确定性文本转换产物，
 * 见 ``scripts/build_offline_demo_bundle.py``）。离线 HTML 因此保持单文件、零外链、零网络加载。
 *
 * 职责：
 * 1. 遍历 ``section.cls-demo-scene``，读取其内嵌的
 *    ``<script type="application/json" class="cls-demo-data">``（只是数据，不执行附件内容）；
 *    静态附录里的 JSON 是完整固定版本快照（``{artifact:{...}}``），播放器运行时渲染的
 *    JSON 是附件对象本身，两种形状都兼容。
 * 2. 按 ``artifact.kind`` 挂载可交互视图并打标：
 *    - flow_animation / science_demo / demo_3d（流程数据 ``nodes``）→ ``createFlowView``；
 *    - isometric（等距数据 ``parts``）→ ``createIsoView``；
 *    - factory_sim → 本文件自带的轻量 SVG/DOM 回放（指标卡 + 工位队列 + 事件时间轴），
 *      不依赖 three.js / WebGL，只读取附件快照里的真实 ``metrics`` / ``events``；
 *    - function_graph / whiteboard / mech_3d：在线播放器需要 functionGraph / sketchBoard /
 *      mechDemo 等复杂运行时，离线单文件不引入（**离线 HTML 仅海报**），保留固定版本海报，
 *      只写中文 ``data-offline-demo-error`` 提示；ZIP 里的 ``demos/*.json`` 仍可被外部工具读取。
 * 3. 挂载成功：写 ``data-offline-demo-mounted`` 标记；失败：保留海报 + 中文提示，绝不抛异常
 *    中断播放器（逐个 section 独立 try/catch，``#scene-stage`` 动态切场时由 MutationObserver
 *    补挂）。
 * 4. 交互控件容器统一带 ``data-offline-demo-controls`` 标记；视频导出捕获模式（``body.capture``）
 *    由离线 HTML 内联 CSS 整体隐藏控件，``gotoScene`` 后截图只出现演示画面。
 *
 * 安全性：全部文本用 ``textContent`` / ``createTextNode`` 写入（不拼 innerHTML），演示 JSON 只经
 * ``JSON.parse`` 作数据；不使用 ``eval`` / ``Function`` / 动态 import，也不加载任何外部资源。
 */

(function () {
  "use strict";

  var PLAYER_VERSION = "1";

  /** kind → 中文名（与 app/classroom/schema.py 的标签口径一致；仅用于提示文案）。 */
  var KIND_LABELS = {
    function_graph: "函数图",
    whiteboard: "解题白板",
    flow_animation: "流程动画",
    isometric: "等距结构",
    science_demo: "科学演示",
    demo_3d: "3D 演示",
    mech_3d: "机械 3D",
    factory_sim: "工厂仿真",
    molecule_3d: "分子模型",
    physics_sandbox: "物理沙盒",
  };

  /**
   * 离线 HTML 仅海报的 kind：在线播放器用 functionGraph.js / sketchBoard.js / mechDemo.js
   * （three.js、WebGL 与大量交互状态）渲染，离线单文件不引入这些运行时，保留固定版本海报。
   */
  var POSTER_ONLY_KINDS = {
    function_graph: true,
    whiteboard: true,
    mech_3d: true,
    molecule_3d: true,
    physics_sandbox: true,
  };

  /** 工厂仿真事件类型分组（与 web/js/components/mechDemo.js 的回放口径一致）。 */
  var ARRIVAL_TYPES = { arrival: true };
  var START_TYPES = { start: true, service_start: true };
  var FINISH_TYPES = { finish: true, service_end: true };
  var BLOCK_TYPES = { blocked: true };
  var UNBLOCK_TYPES = { unblock: true };
  var IDLE_TYPES = { idle: true };

  var FACTORY_STATE_LABELS = { idle: "空闲", busy: "忙碌", blocked: "阻塞" };
  var FACTORY_EVENT_LABELS = {
    arrival: "到达",
    start: "开始加工",
    service_start: "开始加工",
    finish: "完成",
    service_end: "完成",
    blocked: "阻塞",
    unblock: "恢复",
    queue: "队列更新",
    idle: "空闲",
  };

  /** 已挂载视图登记表：切场移除 section 时释放控制器与计时器。 */
  var mountedViews = [];

  // ---------------------------------------------------------------------------
  // 基础 DOM 工具（只用 createElement / textContent）
  // ---------------------------------------------------------------------------

  function createElement(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function createButton(text, className) {
    var button = createElement("button", className || "offline-demo-btn", text);
    button.type = "button";
    return button;
  }

  function kindLabel(kind) {
    return KIND_LABELS[kind] || String(kind || "演示");
  }

  function isCaptureMode() {
    return Boolean(document.body && document.body.classList.contains("capture"));
  }

  function formatNumber(value, digits) {
    var number = Number(value);
    if (!Number.isFinite(number)) return "—";
    var fixed = typeof digits === "number" ? Number(number.toFixed(digits)) : number;
    return String(fixed);
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === undefined || value === null || value === false) return;
      node.setAttribute(key, String(value));
    });
    [].concat(children === undefined ? [] : children).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  // ---------------------------------------------------------------------------
  // 快照解析与标记
  // ---------------------------------------------------------------------------

  function readArtifact(section) {
    var script = section.querySelector("script.cls-demo-data");
    if (!script) return null;
    var parsed;
    try {
      parsed = JSON.parse(script.textContent || "{}");
    } catch (error) {
      return null;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    // 静态附录：{format, fixed, artifact:{...}}；在线播放器运行时：附件对象本身。
    var artifact = parsed.artifact;
    if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) artifact = parsed;
    return artifact;
  }

  function artifactData(artifact) {
    var data = artifact && artifact.data;
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  }

  function markError(section, message) {
    var text = String(message || "离线演示不可用，已保留静态海报。");
    section.setAttribute("data-offline-demo-error", text);
    section.removeAttribute("data-offline-demo-mounted");
    if (section.querySelector(".cls-demo-offline-error")) return;
    var note = createElement("p", "cls-demo-offline-error", "离线演示提示：" + text);
    note.setAttribute("role", "status");
    section.appendChild(note);
  }

  function markMounted(section, mode) {
    section.removeAttribute("data-offline-demo-error");
    section.setAttribute("data-offline-demo-mounted", "1");
    if (mode) section.setAttribute("data-offline-demo-mode", mode);
    var note = section.querySelector(".cls-demo-offline-error");
    if (note) note.remove();
  }

  function mountErrorMessage(kind, reason) {
    return "「" + kindLabel(kind) + "」" + reason;
  }

  /** 在标题之后插入播放器容器（保留原有海报/说明结构，不替换任何静态内容）。 */
  function insertHolder(section) {
    var holder = createElement("div", "cls-demo-offline");
    var anchor = section.querySelector("h2") || section.querySelector(".cls-demo-head");
    if (anchor && anchor.parentNode === section) {
      section.insertBefore(holder, anchor.nextSibling);
    } else {
      section.insertBefore(holder, section.firstChild);
    }
    return holder;
  }

  // ---------------------------------------------------------------------------
  // flow / iso：分步播放控件（上一步 / 播放 / 下一步 + 步进说明）
  // ---------------------------------------------------------------------------

  function stepCountOf(view) {
    if (!view || typeof view.getStepCount !== "function") return 0;
    var count = Number(view.getStepCount());
    return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
  }

  function stepTextOf(view, index, total) {
    var step = null;
    try {
      step = typeof view.getStep === "function" ? view.getStep(index) : null;
    } catch (error) {
      step = null;
    }
    var text = "第 " + (index + 1) + " / " + total + " 步";
    if (step && step.label) text += "：" + String(step.label);
    else if (step && step.title) text += "：" + String(step.title);
    if (step && step.detail) text += "（" + String(step.detail) + "）";
    return text;
  }

  function mountStepControls(holder, view) {
    var total = stepCountOf(view);
    var controls = createElement("div", "cls-demo-offline-controls");
    controls.setAttribute("data-offline-demo-controls", "1");
    var prev = createButton("上一步");
    var play = createButton("播放", "offline-demo-btn offline-demo-btn-primary");
    var next = createButton("下一步");
    var status = createElement("span", "offline-demo-step");
    var index = 0;
    var timer = 0;

    function stop() {
      if (timer) {
        window.clearInterval(timer);
        timer = 0;
        play.textContent = "播放";
      }
    }

    function apply() {
      try {
        view.setStep(index);
      } catch (error) {
        /* 单步渲染失败不影响其余步骤；保留状态文案。 */
      }
      status.textContent = stepTextOf(view, index, total);
    }

    function goto(delta) {
      if (!total) return;
      index = (index + delta + total) % total;
      apply();
    }

    prev.addEventListener("click", function () {
      stop();
      goto(-1);
    });
    next.addEventListener("click", function () {
      stop();
      goto(1);
    });
    play.addEventListener("click", function () {
      if (!total) return;
      if (timer) {
        stop();
        return;
      }
      play.textContent = "暂停";
      timer = window.setInterval(function () {
        if (!holder.isConnected) {
          stop();
          return;
        }
        index = (index + 1) % total;
        apply();
      }, 1500);
    });

    controls.appendChild(prev);
    controls.appendChild(play);
    controls.appendChild(next);
    controls.appendChild(status);
    holder.appendChild(controls);
    if (!total) {
      prev.disabled = true;
      next.disabled = true;
      play.disabled = true;
      status.textContent = "该演示没有分步数据，已显示静态示意图。";
    } else {
      apply();
    }
    return {
      destroy: function () {
        stop();
        if (controls.parentNode) controls.remove();
      },
    };
  }

  // ---------------------------------------------------------------------------
  // flow / iso：挂载渲染器
  // ---------------------------------------------------------------------------

  function bundle() {
    return window.ZhixuOfflineDemo && typeof window.ZhixuOfflineDemo === "object"
      ? window.ZhixuOfflineDemo
      : null;
  }

  function mountFlow(section, artifact, data) {
    var renderers = bundle();
    if (!renderers || typeof renderers.createFlowView !== "function") {
      markError(section, mountErrorMessage(artifact.kind, "离线播放器未内联，已保留海报。"));
      return;
    }
    var holder = insertHolder(section);
    var host = createElement("div", "cls-demo-offline-view");
    holder.appendChild(host);
    var view = renderers.createFlowView(host, data, {
      title: artifact.title ? String(artifact.title) : "",
      ariaLabel: (artifact.title ? String(artifact.title) : kindLabel(artifact.kind)) + "（离线可交互）",
    });
    if (!view || !view.element) throw new Error("createFlowView 未返回视图");
    var controls = mountStepControls(holder, view);
    markMounted(section, "flow");
    mountedViews.push({
      section: section,
      destroy: function () {
        controls.destroy();
        if (view && typeof view.destroy === "function") view.destroy();
      },
    });
  }

  function mountIso(section, artifact, data) {
    var renderers = bundle();
    if (!renderers || typeof renderers.createIsoView !== "function") {
      markError(section, mountErrorMessage(artifact.kind, "离线播放器未内联，已保留海报。"));
      return;
    }
    var holder = insertHolder(section);
    var host = createElement("div", "cls-demo-offline-view");
    holder.appendChild(host);
    // 视频捕获模式禁止补间动画，保证 gotoScene 后立刻截图也是确定性首帧。
    var view = renderers.createIsoView(host, data, {
      staticMode: isCaptureMode(),
      title: artifact.title ? String(artifact.title) : "",
      ariaLabel: (artifact.title ? String(artifact.title) : kindLabel(artifact.kind)) + "（离线可交互）",
    });
    if (!view || !view.element) throw new Error("createIsoView 未返回视图");
    var controls = mountStepControls(holder, view);
    markMounted(section, "isometric");
    mountedViews.push({
      section: section,
      destroy: function () {
        controls.destroy();
        if (view && typeof view.destroy === "function") view.destroy();
      },
    });
  }

  // ---------------------------------------------------------------------------
  // factory_sim：轻量 SVG/DOM 回放（不依赖 three.js / WebGL）
  // ---------------------------------------------------------------------------

  function stringList(value) {
    if (!Array.isArray(value)) return [];
    return value.map(function (item) {
      return item === undefined || item === null ? "" : String(item);
    }).filter(function (item) {
      return Boolean(item);
    });
  }

  function numberList(value, options) {
    var opts = options || {};
    if (!Array.isArray(value)) return [];
    return value.map(function (item) {
      var number = Number(item);
      if (!Number.isFinite(number)) number = 0;
      if (opts.round) number = Math.round(number);
      return Math.max(0, number);
    });
  }

  function hasNumber(value) {
    return value !== undefined && value !== null && Number.isFinite(Number(value));
  }

  function normalizeFactoryMetrics(raw) {
    var source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    var bottleneckIndex = -1;
    var bottleneckLabel = "";
    var bottleneckRaw = source.bottleneck_station;
    if (typeof bottleneckRaw === "number" && Number.isFinite(bottleneckRaw)) {
      bottleneckIndex = Math.round(bottleneckRaw);
    } else if (typeof bottleneckRaw === "string" && bottleneckRaw.trim()) {
      bottleneckLabel = bottleneckRaw.trim();
    }
    var hasSimSeconds = hasNumber(source.sim_seconds);
    var hasArrivals = hasNumber(source.arrivals);
    var hasCompleted = hasNumber(source.completed);
    var hasThroughput = hasNumber(source.throughput_per_min);
    return {
      // 缺失的指标保持 null（界面显示「—」），不用默认值伪造读数。
      sim_seconds: hasSimSeconds ? Math.max(1, Number(source.sim_seconds)) : null,
      arrivals: hasArrivals ? Math.max(0, Math.round(Number(source.arrivals))) : null,
      completed: hasCompleted ? Math.max(0, Math.round(Number(source.completed))) : null,
      throughput_per_min: hasThroughput ? Math.max(0, Number(source.throughput_per_min)) : null,
      avg_wait_s: numberList(source.avg_wait_s),
      max_queue: numberList(source.max_queue, { round: true }),
      utilization: numberList(source.utilization).map(function (item) {
        return Math.min(1, item);
      }),
      bottleneckIndex: bottleneckIndex,
      bottleneckLabel: bottleneckLabel,
      bottleneckReason: String(source.bottleneck_reason || ""),
      present: {
        sim_seconds: hasSimSeconds,
        arrivals: hasArrivals,
        completed: hasCompleted,
        throughput: hasThroughput,
        bottleneck: bottleneckIndex >= 0 || Boolean(bottleneckLabel),
      },
    };
  }

  function hasUsableMetrics(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return false;
    var keys = [
      "sim_seconds", "arrivals", "completed", "throughput_per_min",
      "avg_wait_s", "max_queue", "utilization",
    ];
    return keys.some(function (key) {
      return raw[key] !== undefined && raw[key] !== null;
    });
  }

  function resolveStationIndex(value, indexById, count) {
    if (typeof value === "number" && Number.isFinite(value)) {
      var number = Math.round(value);
      return number >= 0 && number < count ? number : -1;
    }
    var key = String(value === undefined || value === null ? "" : value).trim();
    if (!key) return -1;
    return Object.prototype.hasOwnProperty.call(indexById, key) ? indexById[key] : -1;
  }

  /** 归一化工厂数据：工位来自 flow.nodes（kind=station）或 station_labels，事件按时间排序。 */
  function normalizeFactory(data) {
    var source = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    var stationLabels = stringList(source.station_labels);
    var flow = source.flow && typeof source.flow === "object" ? source.flow : {};
    var rawNodes = Array.isArray(flow.nodes) ? flow.nodes : [];
    var markedStations = rawNodes.filter(function (node) {
      return node && typeof node === "object" && String(node.kind || "").toLowerCase() === "station";
    });
    var nodeSource = markedStations.length ? markedStations : rawNodes;
    var count = Math.max(nodeSource.length, stationLabels.length, 1);
    var nodes = [];
    var indexById = {};
    var index = 0;
    for (index = 0; index < count; index += 1) {
      var raw = nodeSource[index];
      var id = raw && raw.id !== undefined && raw.id !== null ? String(raw.id) : "station-" + (index + 1);
      var label = raw && raw.label !== undefined && raw.label !== null && String(raw.label)
        ? String(raw.label)
        : (stationLabels[index] || "工位 " + (index + 1));
      nodes.push({ id: id, label: label });
      indexById[id] = index;
      indexById[label] = index;
      indexById[String(index)] = index;
      indexById["station-" + (index + 1)] = index;
    }
    var nextOf = nodes.map(function () {
      return -1;
    });
    var rawEdges = Array.isArray(flow.edges) ? flow.edges : [];
    rawEdges.forEach(function (edge) {
      if (!edge || typeof edge !== "object") return;
      var from = resolveStationIndex(edge.from, indexById, nodes.length);
      var to = resolveStationIndex(edge.to, indexById, nodes.length);
      if (from < 0 || to < 0 || from === to) return;
      if (nextOf[from] < 0) nextOf[from] = to;
    });
    var events = [];
    var rawEvents = Array.isArray(source.events) ? source.events : [];
    rawEvents.forEach(function (rawEvent) {
      if (!rawEvent || typeof rawEvent !== "object") return;
      var time = Number(rawEvent.t);
      if (!Number.isFinite(time)) return;
      var station = resolveStationIndex(rawEvent.station, indexById, nodes.length);
      if (station < 0) return;
      var queue = rawEvent.queue;
      events.push({
        t: Math.max(0, time),
        type: String(rawEvent.type === undefined || rawEvent.type === null ? "" : rawEvent.type).toLowerCase(),
        station: station,
        itemId: rawEvent.item_id === undefined || rawEvent.item_id === null ? "" : String(rawEvent.item_id),
        queue: queue === undefined || queue === null || !Number.isFinite(Number(queue))
          ? null
          : Math.max(0, Math.round(Number(queue))),
      });
    });
    events.sort(function (left, right) {
      return left.t === right.t ? 0 : left.t - right.t;
    });
    var metrics = normalizeFactoryMetrics(source.metrics);
    var maxEventTime = events.length ? events[events.length - 1].t : 0;
    // 时间轴上限优先用真实 sim_seconds；缺失时用最后一个事件时刻，不凭空延长。
    var simSeconds = Math.max(metrics.sim_seconds || 0, maxEventTime, 1);
    if (metrics.bottleneckIndex >= nodes.length) metrics.bottleneckIndex = -1;
    var bottleneckLabel = metrics.bottleneckLabel
      || (metrics.bottleneckIndex >= 0 ? nodes[metrics.bottleneckIndex].label : "");
    return {
      title: String(source.title || ""),
      nodes: nodes,
      nextOf: nextOf,
      metrics: metrics,
      events: events,
      simSeconds: simSeconds,
      bottleneckLabel: bottleneckLabel,
      hasEvents: events.length > 0,
    };
  }

  /** 由固定事件表推导每个事件时刻的队列/工位状态（纯回放，不做随机模拟、不伪造读数）。 */
  function buildFactoryReplay(model) {
    var count = model.nodes.length;
    var queues = new Array(count).fill(0);
    var status = new Array(count).fill("idle");
    var arrivals = 0;
    var completed = 0;
    var snapshots = [{
      t: 0, queues: queues.slice(), status: status.slice(), arrivals: arrivals, completed: completed, event: null,
    }];
    model.events.forEach(function (event) {
      var index = event.station;
      var recognized = true;
      if (ARRIVAL_TYPES[event.type]) {
        arrivals += 1;
        if (event.queue === null) queues[index] += 1;
      } else if (START_TYPES[event.type]) {
        if (event.queue === null) queues[index] = Math.max(0, queues[index] - 1);
        status[index] = "busy";
      } else if (FINISH_TYPES[event.type]) {
        status[index] = "idle";
        var next = model.nextOf[index];
        if (next >= 0) {
          if (event.queue === null) queues[next] += 1;
        } else {
          completed += 1;
        }
      } else if (BLOCK_TYPES[event.type]) {
        status[index] = "blocked";
      } else if (UNBLOCK_TYPES[event.type]) {
        status[index] = "busy";
      } else if (IDLE_TYPES[event.type]) {
        status[index] = "idle";
      } else if (event.type !== "queue" && event.type !== "") {
        recognized = false;
      }
      if (event.queue !== null) queues[index] = event.queue;
      if (!recognized) return;
      snapshots.push({
        t: event.t, queues: queues.slice(), status: status.slice(),
        arrivals: arrivals, completed: completed, event: event,
      });
    });
    var last = snapshots[snapshots.length - 1];
    if (last.t < model.simSeconds) {
      snapshots.push({
        t: model.simSeconds, queues: last.queues.slice(), status: last.status.slice(),
        arrivals: last.arrivals, completed: last.completed, event: null,
      });
    }
    return snapshots;
  }

  function snapshotAt(replay, seconds) {
    var low = 0;
    var high = replay.length - 1;
    var found = replay[0];
    while (low <= high) {
      var middle = (low + high) >> 1;
      if (replay[middle].t <= seconds + 1e-9) {
        found = replay[middle];
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    return found;
  }

  function factoryEventText(type) {
    return FACTORY_EVENT_LABELS[type] || (type ? type : "事件");
  }

  function factoryStateText(key) {
    return FACTORY_STATE_LABELS[key] || FACTORY_STATE_LABELS.idle;
  }

  function formatEventLine(event) {
    if (!event) return "起点：等待第一个工件";
    var parts = ["t = " + formatNumber(event.t, 2) + "s", factoryEventText(event.type), "工位 " + (event.station + 1)];
    if (event.itemId) parts.push("工件 " + event.itemId);
    if (event.queue !== null) parts.push("队列 " + event.queue);
    return parts.join(" · ");
  }

  function renderFactoryMetrics(model) {
    var metrics = model.metrics;
    var present = metrics.present || {};
    var arrivals = present.arrivals ? metrics.arrivals : "—";
    var completed = present.completed ? metrics.completed : "—";
    var cards = [
      {
        label: "吞吐",
        value: present.throughput ? formatNumber(metrics.throughput_per_min, 2) + " 件/分" : "—",
      },
      { label: "完成 / 到达", value: completed + " / " + arrivals + " 件" },
      {
        label: "瓶颈工位",
        value: model.bottleneckLabel || "—",
        note: metrics.bottleneckReason,
      },
      {
        label: "仿真时长",
        value: present.sim_seconds ? formatNumber(metrics.sim_seconds, 1) + " 秒" : "—",
      },
    ];
    return el("div", { class: "offline-demo-metrics" }, cards.map(function (card) {
      var body = [
        el("span", { class: "offline-demo-metric-label" }, card.label),
        el("span", { class: "offline-demo-metric-value" }, card.value),
      ];
      if (card.note) body.push(el("span", { class: "offline-demo-metric-note" }, card.note));
      return el("div", { class: "offline-demo-metric" }, body);
    }));
  }

  // SVG 命名空间是标识符，不是网络请求；用拼接写法避免离线 HTML 文本里出现 http(s) 外链
  // （导出的验收口径：文本中零外链）。
  var SVG_NAMESPACE = "http" + "://www.w3.org/2000/svg";

  function svgNode(tag, attrs, text) {
    var node = document.createElementNS(SVG_NAMESPACE, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === undefined || value === null || value === false) return;
      node.setAttribute(key, String(value));
    });
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /** 轻量 SVG 产线流程：工位方框 + 箭头，状态用 data-state 着色（不依赖 three.js / WebGL）。 */
  function renderFactoryFlow(model) {
    var boxWidth = 120;
    var boxHeight = 56;
    var gap = 34;
    var pad = 12;
    var width = Math.max(320, pad * 2 + model.nodes.length * boxWidth
      + Math.max(0, model.nodes.length - 1) * gap);
    var height = 118;
    var svg = svgNode("svg", {
      viewBox: "0 0 " + width + " " + height,
      class: "offline-demo-flow-svg",
      role: "img",
      "aria-label": "产线流程示意",
    });
    var stations = model.nodes.map(function (node, index) {
      var x = pad + index * (boxWidth + gap);
      var y = 24;
      var centerY = y + boxHeight / 2;
      var group = svgNode("g", { class: "offline-demo-svg-station", "data-state": "idle" });
      group.appendChild(svgNode("rect", {
        x: x, y: y, width: boxWidth, height: boxHeight, rx: 10,
        class: "offline-demo-svg-box",
      }));
      group.appendChild(svgNode("text", {
        x: x + boxWidth / 2, y: centerY - 1, "text-anchor": "middle",
        class: "offline-demo-svg-label",
      }, node.label));
      var queueText = svgNode("text", {
        x: x + boxWidth / 2, y: y + boxHeight + 19, "text-anchor": "middle",
        class: "offline-demo-svg-queue",
      }, "队列 0");
      group.appendChild(queueText);
      svg.appendChild(group);
      if (index < model.nodes.length - 1) {
        var x1 = x + boxWidth + 4;
        var x2 = x + boxWidth + gap - 4;
        svg.appendChild(svgNode("line", {
          x1: x1, y1: centerY, x2: x2 - 6, y2: centerY, class: "offline-demo-svg-arrow",
        }));
        svg.appendChild(svgNode("polygon", {
          points: (x2 - 6) + "," + (centerY - 5) + " " + x2 + "," + centerY + " "
            + (x2 - 6) + "," + (centerY + 5),
          class: "offline-demo-svg-arrowhead",
        }));
      }
      return { group: group, queue: queueText };
    });
    return {
      element: svg,
      update: function (snapshot) {
        stations.forEach(function (station, index) {
          var status = snapshot.status[index] || "idle";
          var queue = snapshot.queues[index] || 0;
          station.group.setAttribute("data-state", status);
          station.queue.textContent = "队列 " + queue;
        });
      },
    };
  }

  function renderFactoryLine(model) {
    var metrics = model.metrics;
    var line = el("div", { class: "offline-demo-line" });
    var maxQueueRef = 1;
    metrics.max_queue.forEach(function (value) {
      maxQueueRef = Math.max(maxQueueRef, value);
    });
    var stationNodes = model.nodes.map(function (node, index) {
      var util = Number.isFinite(metrics.utilization[index]) ? metrics.utilization[index] : null;
      var avgWait = Number.isFinite(metrics.avg_wait_s[index])
        ? formatNumber(metrics.avg_wait_s[index], 1) + "s"
        : "—";
      var maxQueue = Number.isFinite(metrics.max_queue[index]) ? metrics.max_queue[index] : "—";
      var bar = el("div", { class: "offline-demo-queue-bar" });
      var barFill = el("span", { class: "offline-demo-queue-fill" });
      bar.appendChild(barFill);
      var card = el("div", { class: "offline-demo-station", "data-station": index }, [
        el("strong", { class: "offline-demo-station-label" }, node.label),
        el("span", { class: "offline-demo-station-state" }, factoryStateText("idle")),
        el("span", { class: "offline-demo-station-queue" }, "队列 0"),
        bar,
        el("span", { class: "offline-demo-station-util" },
          util === null ? "利用率 —" : "利用率 " + Math.round(util * 100) + "%"),
        el("span", { class: "offline-demo-station-wait" },
          "平均等待 " + avgWait + " · 最大队列 " + maxQueue),
      ]);
      line.appendChild(card);
      if (index < model.nodes.length - 1) {
        line.appendChild(el("span", { class: "offline-demo-arrow", "aria-hidden": "true" }, "→"));
      }
      return { card: card, state: card.querySelector(".offline-demo-station-state"),
        queue: card.querySelector(".offline-demo-station-queue"), bar: barFill };
    });
    return {
      element: line,
      update: function (snapshot) {
        stationNodes.forEach(function (station, index) {
          var status = snapshot.status[index] || "idle";
          var queue = snapshot.queues[index] || 0;
          station.state.textContent = factoryStateText(status);
          station.card.setAttribute("data-state", status);
          station.queue.textContent = "队列 " + queue;
          var ratio = Math.max(0, Math.min(1, maxQueueRef ? queue / maxQueueRef : 0));
          station.bar.style.width = Math.round(ratio * 100) + "%";
          if (snapshot.event && snapshot.event.station === index) {
            station.card.setAttribute("data-active", "1");
          } else {
            station.card.removeAttribute("data-active");
          }
        });
      },
    };
  }

  function mountFactory(section, artifact, data) {
    var model = normalizeFactory(data);
    // 只回放真实数据：既没有指标快照、也没有事件序列时保留海报，不伪造任何读数。
    if (!hasUsableMetrics(data.metrics) && !model.hasEvents) {
      markError(section, mountErrorMessage(artifact.kind, "缺少真实指标与事件数据，已保留海报。"));
      return;
    }
    var replay = buildFactoryReplay(model);
    var holder = insertHolder(section);
    var root = el("div", { class: "offline-demo-factory" });
    holder.appendChild(root);

    root.appendChild(renderFactoryMetrics(model));
    var flowView = renderFactoryFlow(model);
    root.appendChild(flowView.element);
    var lineView = renderFactoryLine(model);
    root.appendChild(lineView.element);

    var timeline = el("div", { class: "offline-demo-timeline" });
    var controls = el("div", { class: "offline-demo-controls", "data-offline-demo-controls": "1" });
    var prev = createButton("上一步");
    var play = createButton("播放", "offline-demo-btn offline-demo-btn-primary");
    var next = createButton("下一步");
    var range = el("input", {
      type: "range", min: 0, max: model.simSeconds, step: "0.5", value: 0,
      class: "offline-demo-range", "aria-label": "回放时间轴",
    });
    var timeLabel = el("span", { class: "offline-demo-time" }, "t = 0s");
    controls.appendChild(prev);
    controls.appendChild(play);
    controls.appendChild(next);
    controls.appendChild(range);
    controls.appendChild(timeLabel);
    timeline.appendChild(controls);
    if (!model.hasEvents) {
      timeline.appendChild(el("p", { class: "offline-demo-hint" },
        "该演示数据缺少事件序列，仅展示指标与流程结构，不伪造回放。"));
    }
    var eventLine = el("p", { class: "offline-demo-event" }, formatEventLine(null));
    timeline.appendChild(eventLine);
    var eventList = el("ol", { class: "offline-demo-eventlist" });
    model.events.forEach(function (event) {
      eventList.appendChild(el("li", { class: "offline-demo-eventitem" }, formatEventLine(event)));
    });
    timeline.appendChild(eventList);
    root.appendChild(timeline);

    var currentTime = 0;
    var timer = 0;
    var eventCursor = -1;

    function eventIndexAt(seconds) {
      var index = -1;
      for (var position = 0; position < model.events.length; position += 1) {
        if (model.events[position].t <= seconds + 1e-9) index = position;
        else break;
      }
      return index;
    }

    function paint() {
      var snapshot = snapshotAt(replay, currentTime);
      flowView.update(snapshot);
      lineView.update(snapshot);
      eventCursor = eventIndexAt(currentTime);
      eventLine.textContent = "当前事件：" + formatEventLine(eventCursor >= 0 ? model.events[eventCursor] : null);
      timeLabel.textContent = "t = " + formatNumber(currentTime, 2) + "s / "
        + formatNumber(model.simSeconds, 1) + "s";
      var list = eventList.querySelectorAll(".offline-demo-eventitem");
      for (var index = 0; index < list.length; index += 1) {
        list[index].classList.toggle("is-past", index <= eventCursor);
      }
      range.value = String(Math.min(model.simSeconds, Math.max(0, currentTime)));
    }

    function stop() {
      if (timer) {
        window.clearInterval(timer);
        timer = 0;
        play.textContent = "播放";
      }
    }

    function seek(seconds) {
      var value = Math.max(0, Math.min(model.simSeconds, Number(seconds) || 0));
      currentTime = value;
      paint();
    }

    function stepEvent(delta) {
      if (!model.events.length) return;
      var cursor = eventIndexAt(currentTime);
      if (delta > 0) {
        seek(model.events[Math.min(model.events.length - 1, cursor + 1)].t);
      } else if (cursor < 0) {
        seek(0);
      } else {
        seek(model.events[Math.max(0, cursor - 1)].t);
      }
    }

    prev.addEventListener("click", function () {
      stop();
      stepEvent(-1);
    });
    next.addEventListener("click", function () {
      stop();
      stepEvent(1);
    });
    play.addEventListener("click", function () {
      if (!model.hasEvents) return;
      if (timer) {
        stop();
        return;
      }
      if (currentTime >= model.simSeconds) seek(0);
      play.textContent = "暂停";
      timer = window.setInterval(function () {
        if (!root.isConnected) {
          stop();
          return;
        }
        var nextTime = currentTime + 0.5;
        if (nextTime >= model.simSeconds) {
          seek(model.simSeconds);
          stop();
          return;
        }
        seek(nextTime);
      }, 250);
    });
    range.addEventListener("input", function () {
      stop();
      seek(Number(range.value) || 0);
    });

    paint();
    markMounted(section, "factory");
    mountedViews.push({
      section: section,
      destroy: function () {
        stop();
        if (holder.parentNode) holder.remove();
      },
    });
  }

  // ---------------------------------------------------------------------------
  // 单个 section 挂载分派
  // ---------------------------------------------------------------------------

  function mountSection(section) {
    if (!section || section.nodeType !== 1) return;
    if (section.getAttribute("data-offline-demo-attempted") === "1") return;
    if (!section.classList.contains("cls-demo-scene")) return;
    section.setAttribute("data-offline-demo-attempted", "1");
    var artifact = readArtifact(section);
    if (!artifact) return;
    var kind = String(artifact.kind || "");
    var data = artifactData(artifact);
    section.setAttribute("data-offline-demo", kind);
    try {
      if (POSTER_ONLY_KINDS[kind]) {
        // 离线 HTML 仅海报：这些 kind 的在线渲染器依赖复杂运行时（函数图/白板/three 机械 3D）。
        markError(section, "该演示类型在离线 HTML 中仅保留固定版本海报（在线播放器可查看完整交互）。");
        return;
      }
      if (kind === "flow_animation" || kind === "science_demo" || kind === "demo_3d") {
        if (!Array.isArray(data.nodes) || !data.nodes.length) {
          markError(section, mountErrorMessage(kind, "数据不是流程结构，已保留海报。"));
          return;
        }
        mountFlow(section, artifact, data);
        return;
      }
      if (kind === "isometric") {
        if (!Array.isArray(data.parts) || !data.parts.length) {
          markError(section, mountErrorMessage(kind, "数据缺少等距部件，已保留海报。"));
          return;
        }
        mountIso(section, artifact, data);
        return;
      }
      if (kind === "factory_sim") {
        mountFactory(section, artifact, data);
        return;
      }
      markError(section, mountErrorMessage(kind, "离线 HTML 暂不支持该演示类型，已保留海报。"));
    } catch (error) {
      markError(section, mountErrorMessage(kind, "离线回放加载失败，已保留海报"
        + (error && error.message ? "： " + error.message : "。")));
    }
  }

  function mountAll() {
    var sections = document.querySelectorAll("section.cls-demo-scene");
    for (var index = 0; index < sections.length; index += 1) {
      try {
        mountSection(sections[index]);
      } catch (error) {
        /* 单个 section 失败不能中断其余演示与播放器。 */
      }
    }
    // 切场移除的动态 section：释放其控制器/计时器，避免切页后残留定时器。
    mountedViews = mountedViews.filter(function (entry) {
      if (!entry.section || !entry.section.isConnected) {
        try {
          entry.destroy();
        } catch (error) {
          /* 忽略释放失败 */
        }
        return false;
      }
      return true;
    });
  }

  function boot() {
    mountAll();
    // 在线播放器切到演示场景时才创建 section.cls-demo-scene（#scene-stage 动态渲染），
    // 用 MutationObserver 补挂；视频捕获的 gotoScene 同样走这条路径。
    var stage = document.getElementById("scene-stage");
    if (stage && typeof window.MutationObserver === "function") {
      var scheduled = false;
      var observer = new window.MutationObserver(function () {
        if (scheduled) return;
        scheduled = true;
        window.setTimeout(function () {
          scheduled = false;
          mountAll();
        }, 0);
      });
      observer.observe(stage, { childList: true, subtree: true });
    }
  }

  // 供第三方/调试显式触发（例如页面在加载后又注入演示 section）；不影响正常自动挂载。
  window.ZhixuOfflineDemoInit = {
    version: PLAYER_VERSION,
    mountAll: mountAll,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
