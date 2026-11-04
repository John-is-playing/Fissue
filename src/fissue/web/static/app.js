/* Fissue Web 的共用前端。
 *
 * 原先两个页面各自内联一份 act()，而且行为还不一致（看板那份没有二次确认、条目页那份有）。
 * 这里只有一份，并且把三件事做对：
 *   1. 反馈走页内吐司，不再用 alert() 弹一坨 JSON 原文；
 *   2. 长任务走「作业 + 轮询」，看得见进度与日志，不再是一个同步 fetch 干等；
 *   3. 确认走 <dialog>，不再用 confirm()。
 *
 * 配置由 base.html 注入到 window.FISSUE（apiPrefix / readOnly），所以这里**不拼 Jinja**，
 * 也就不会再有 `{{ settings.api.prefix if settings else ... }}` 那类模板优先级陷阱。
 */
(function () {
  "use strict";

  var CFG = window.FISSUE || {};
  var API = CFG.apiPrefix || "/api/v1";
  var READ_ONLY = !!CFG.readOnly;

  // ---------------------------------------------------------------------
  // 吐司
  // ---------------------------------------------------------------------
  function toastHost() {
    var host = document.getElementById("toasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "toasts";
      document.body.appendChild(host);
    }
    return host;
  }

  /**
   * 页内提示。
   * @param {string} message 主文案
   * @param {"ok"|"bad"|"info"} kind 颜色
   * @param {{detail?: any, timeout?: number}} [opts] detail 会以 <pre> 折好再显示
   */
  //: 最多同时显示几条吐司。批量操作连点几次就各弹一条，堆到五六条会把右下角整块盖住，
  //: 反而挡住用户正要看的表格。超出时顶掉**最旧**的那条（新提示与刚做的动作更相关）。
  var TOAST_MAX = 4;

  function toast(message, kind, opts) {
    opts = opts || {};
    var host = toastHost();
    var kindClass = kind || "info";

    // 已有同文案同色调的吐司 → 合并计数并重新计时，而不是再堆一条一模一样的。
    // 两条相同提示会让人分不清「点了两次」还是「出了两次错」。
    var existing = null;
    Array.prototype.forEach.call(host.children, function (el) {
      if (!existing && el.dataset.msg === String(message) && el.dataset.kind === kindClass) {
        existing = el;
      }
    });
    if (existing) {
      var times = parseInt(existing.dataset.count || "1", 10) + 1;
      existing.dataset.count = String(times);
      var merged = existing.querySelector(".t-count");
      if (merged) merged.textContent = "×" + times;
      if (existing._dismissTimer) clearTimeout(existing._dismissTimer);
      var again = opts.timeout !== undefined ? opts.timeout : kindClass === "bad" ? 0 : 5000;
      if (again > 0) {
        existing._dismissTimer = setTimeout(function () {
          if (existing.parentNode) existing.parentNode.removeChild(existing);
        }, again);
      }
      return function () {
        if (existing.parentNode) existing.parentNode.removeChild(existing);
      };
    }

    var box = document.createElement("div");
    box.className = "toast " + kindClass;
    box.dataset.msg = String(message);
    box.dataset.kind = kindClass;
    box.dataset.count = "1";

    var head = document.createElement("div");
    head.className = "t-head";
    var text = document.createElement("span");
    text.textContent = message;
    var close = document.createElement("span");
    close.className = "t-close";
    close.textContent = "✕";
    close.setAttribute("role", "button");
    close.setAttribute("tabindex", "0");
    close.title = "关闭";
    var countBadge = document.createElement("span");
    countBadge.className = "t-count";
    head.appendChild(text);
    head.appendChild(countBadge);
    head.appendChild(close);
    box.appendChild(head);

    if (opts.detail !== undefined && opts.detail !== null) {
      var pre = document.createElement("pre");
      pre.textContent =
        typeof opts.detail === "string" ? opts.detail : JSON.stringify(opts.detail, null, 2);
      box.appendChild(pre);
    }

    function dismiss() {
      if (box._dismissTimer) clearTimeout(box._dismissTimer);
      if (box.parentNode) box.parentNode.removeChild(box);
    }
    close.addEventListener("click", dismiss);
    close.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") dismiss();
    });
    host.appendChild(box);

    // 超出上限就顶掉最旧的一条。
    while (host.children.length > TOAST_MAX) {
      host.removeChild(host.children[0]);
    }

    // 失败信息不自动消失：它通常带着用户需要照着改的原因。
    var timeout = opts.timeout !== undefined ? opts.timeout : kindClass === "bad" ? 0 : 5000;
    if (timeout > 0) box._dismissTimer = setTimeout(dismiss, timeout);
    return dismiss;
  }

  // ---------------------------------------------------------------------
  // 请求
  // ---------------------------------------------------------------------
  function ApiError(message, status, payload) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.payload = payload;
  }
  ApiError.prototype = Object.create(Error.prototype);

  /** 从响应体里取一句可读的原因（FastAPI 的错误体形状不止一种）。 */
  function errorText(status, payload) {
    if (!payload) return "HTTP " + status;
    var detail = payload.detail !== undefined ? payload.detail : payload;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI 的校验错误：逐条拼「字段: 说明」，比丢一坨 JSON 有用。
      return detail
        .map(function (d) {
          var loc = Array.isArray(d.loc) ? d.loc.filter(function (x) { return x !== "body" && x !== "query"; }).join(".") : "";
          return (loc ? loc + ": " : "") + (d.msg || JSON.stringify(d));
        })
        .join("；");
    }
    return JSON.stringify(detail);
  }

  // ---------------------------------------------------------------------
  // 鉴权：会话 cookie + CSRF（docs/AUTH.md §3.5 / §9）
  // ---------------------------------------------------------------------
  //
  // 这里原先是一套「撞到 401 就弹框问口令、存 localStorage、原样重试」的逻辑——
  // 那是单一全局口令时代的产物。换成 cookie 会话之后它整个不成立了：
  //
  //   1. 身份是**浏览器自动带的 HttpOnly cookie**，前端拿不到、也不该拿到；
  //   2. 浏览器拿不到 cookie 就意味着**页面本身没有凭据**，唯一的正确反应是
  //      「去 /login 登录」，而不是在本页原地问一串谁也不认识的令牌；
  //   3. cookie 会被浏览器自动附加到跨站请求上，所以写请求必须带 CSRF token
  //      （服务端对会话认证的写请求强校验，Bearer/可信来源豁免）。
  var AUTH_PREFIX = CFG.authPrefix || "/api/auth";
  // 回环/可信网来源是空串（服务端也只对会话校验），所以这里「有没有值」不作判据。
  var CSRF_TOKEN = CFG.csrfToken || "";
  var redirecting = false;

  /** 未认证 → 去登录页，并记住回来的位置。 */
  function gotoLogin() {
    if (redirecting) return;
    if (window.LOGIN || /^\/login\b/.test(window.location.pathname)) return;  // 已在登录页
    redirecting = true;
    var next = window.location.pathname + window.location.search;
    window.location.replace(AUTH_PREFIX.replace(/\/api\/auth$/, "") + "/login?next=" + encodeURIComponent(next));
  }

  async function request(path, opts) {
    opts = opts || {};
    var url = API + path;
    if (opts.query) {
      var qs = new URLSearchParams();
      Object.keys(opts.query).forEach(function (k) {
        var v = opts.query[k];
        if (v !== undefined && v !== null && v !== "") qs.set(k, v);
      });
      var s = qs.toString();
      if (s) url += (url.indexOf("?") >= 0 ? "&" : "?") + s;
    }
    var method = (opts.method || "GET").toUpperCase();
    // 同源 cookie 必须显式带上：fetch 默认就是 same-origin，这里写明是为了让
    // 「靠 cookie 认证」这件事在代码里看得见，而不是一个隐含默认值。
    var init = { method: method, headers: {}, credentials: "same-origin" };
    if (opts.body !== undefined && opts.body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    // 非 GET 一律带 CSRF token（服务端只对会话身份校验；回环/自定义来源带着也无害）。
    if (CSRF_TOKEN && method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
      init.headers["X-CSRF-Token"] = CSRF_TOKEN;
    }

    var res = await fetch(url, init);

    // 401 = 没有有效会话（或 Key 失效）。浏览器唯一能做的是去登录——**不再弹框要口令**。
    // 403 不跳：它可能是只读模式、CSRF 失败、或来源被拒，那些都该在原页面说清楚。
    if (res.status === 401) {
      if (opts.silent) throw new ApiError("未认证：需要登录。", 401, null);
      gotoLogin();
      throw new ApiError("未认证：正在跳转到登录页…", 401, null);
    }

    var payload = null;
    try {
      payload = await res.json();
    } catch (e) {
      payload = null;
    }
    if (!res.ok) throw new ApiError(errorText(res.status, payload), res.status, payload);
    return payload;
  }

  /** 登出：清会话 cookie 后回登录页。 */
  async function logout() {
    try {
      await fetch(AUTH_PREFIX + "/logout", { method: "POST", credentials: "same-origin" });
    } catch (e) { /* 网络失败也照样跳：本地的会话状态已经不该继续用了 */ }
    window.location.replace("/login");
  }

  // ---------------------------------------------------------------------
  // 作业
  // ---------------------------------------------------------------------
  var POLL_MS = 1200;

  /**
   * 提交一个作业并轮询，直到它结束。
   * @param {string} kind scan|evaluate|verify|fix|flush
   * @param {object} params 传给后端的参数
   * @param {{onUpdate?: function, panel?: HTMLElement, autoReload?: boolean}} [opts]
   */
  async function runJob(kind, params, opts) {
    opts = opts || {};
    if (READ_ONLY) {
      toast("当前为只读模式（web.read_only = true），不能发起写操作。", "bad");
      return null;
    }
    var started;
    try {
      started = await request("/jobs/" + kind, { method: "POST", body: params || {} });
    } catch (err) {
      if (err.status === 409) {
        toast("已有写操作正在运行", "bad", { detail: err.message });
      } else {
        toast("无法启动任务：" + err.message, "bad");
      }
      return null;
    }

    var job = started.job || started;
    var panel = opts.panel || ensureJobPanel();
    renderJob(panel, job);
    toast("已启动：" + kindLabel(kind), "info", { timeout: 2500 });

    var seen = 0;
    while (job && (job.status === "queued" || job.status === "running")) {
      await sleep(POLL_MS);
      try {
        job = await request("/jobs/" + job.id, {silent: true});
      } catch (err) {
        toast("轮询任务状态失败：" + err.message, "bad");
        break;
      }
      renderJob(panel, job);
      (job.log || []).slice(seen).forEach(function (line) {
        seen++;
        if (opts.onLine) opts.onLine(line);
      });
      if (opts.onUpdate) opts.onUpdate(job);
    }

    if (!job) return null;
    if (job.status === "done") {
      toast("任务完成：" + kindLabel(kind), "ok", { detail: summarize(job.result) });
      if (opts.autoReload) scheduleReload();
    } else if (job.status === "canceled") {
      // 取消是预期结果，不是故障：用 info 而不是 bad，并说明中间结果保留。
      toast("任务已取消：" + kindLabel(kind), "info", { detail: job.error, timeout: 8000 });
      if (opts.autoReload) scheduleReload();
    } else if (job.status === "failed") {
      toast("任务失败：" + kindLabel(kind), "bad", { detail: job.error || "（没有错误信息）", timeout: 0 });
    }
    return job;
  }

  function kindLabel(kind) {
    var names = {
      scan: "检索新增",
      evaluate: "AI 评测",
      verify: "沙盒验证",
      fix: "自动修复",
      flush: "Flush 队列",
    };
    return names[kind] || kind;
  }

  /** 作业状态的徽标配色。取消不是故障，所以不给红色。 */
  function statusClass(status) {
    if (status === "done") return "ok";
    if (status === "failed") return "bad";
    if (status === "canceled") return "stale";
    return "unknown";
  }

  /**
   * 取消一个正在跑的作业（总览的任务面板与「任务」页共用同一份）。
   *
   * 后端回答的是「已请求取消」：`task.cancel()` 要等作业在下一个可中断点让出控制权才生效，
   * 所以这里如实提示「可能还要几秒」，然后刷新页面让轮询接管状态。不能假装「已取消」——
   * 长任务可能正卡在一次 LLM 调用或沙盒执行上。
   */
  async function cancelJob(jobId) {
    if (READ_ONLY) {
      toast("当前为只读模式，不能取消任务。", "bad");
      return;
    }
    var ok = await confirmDialog(
      "取消任务 " + jobId + "？",
      "任务会在下一个可中断点退出（可能还要几秒）。已经落库的中间结果会保留。"
    );
    if (!ok) return;
    try {
      var r = await request("/jobs/" + jobId + "/cancel", { method: "POST" });
      toast("已请求取消：" + jobId, "info", { detail: r.note, timeout: 6000 });
      setTimeout(function () { location.reload(); }, 1500);
    } catch (err) {
      toast("取消失败", "bad", { detail: err.message, timeout: 0 });
    }
  }

  /** 结果摘要：只把标量挑出来铺平，避免把整页 JSON 倒给用户。 */
  function summarize(result) {
    if (!result || typeof result !== "object") return result === undefined ? null : result;
    var out = {};
    Object.keys(result).forEach(function (k) {
      var v = result[k];
      if (v === null || ["string", "number", "boolean"].indexOf(typeof v) >= 0) out[k] = v;
      else if (Array.isArray(v)) out[k] = "共 " + v.length + " 项";
    });
    return out;
  }

  function ensureJobPanel() {
    var panel = document.getElementById("job-panel");
    if (!panel) {
      panel = document.createElement("div");
      panel.id = "job-panel";
      panel.className = "panel";
      panel.innerHTML = '<h2>任务</h2>';
      var anchor = document.getElementById("job-panel-anchor");
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor.nextSibling);
      else document.querySelector(".wrap").prepend(panel);
    }
    return panel;
  }

  function renderJob(panel, job) {
    var id = "job-" + job.id;
    var box = document.getElementById(id);
    if (!box) {
      box = document.createElement("div");
      box.className = "job";
      box.id = id;
      var head = panel.querySelector("h2");
      if (head && head.nextSibling) panel.insertBefore(box, head.nextSibling);
      else panel.appendChild(box);
    }
    var running = job.status === "queued" || job.status === "running";
    var kindBadge = statusClass(job.status);
    var lines = (job.log || []).join("\n");

    box.innerHTML =
      '<div class="j-head">' +
      '<span class="j-kind">' + escapeHtml(kindLabel(job.kind)) + "</span>" +
      (running ? '<span class="spinner"></span>' : "") +
      '<span class="tag ' + kindBadge + '">' + escapeHtml(job.status) + "</span>" +
      '<span class="tag">' + escapeHtml(job.id) + "</span>" +
      (job.duration_seconds !== null && job.duration_seconds !== undefined
        ? '<span class="muted small">' + job.duration_seconds.toFixed(1) + "s</span>"
        : "") +
      "</div>" +
      (job.progress ? '<div class="progress-line">' + escapeHtml(job.progress) + "</div>" : "") +
      (job.error ? '<pre class="bad">' + escapeHtml(job.error) + "</pre>" : "") +
      (lines ? "<pre>" + escapeHtml(lines) + "</pre>" : "") +
      (job.status === "done" && job.result
        ? '<pre class="muted">' + escapeHtml(JSON.stringify(summarize(job.result), null, 2)) + "</pre>"
        : "");
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      }
    );
  }

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  /** 作业改的是库里的数据，页面得跟着换一份——但不该在用户正挑东西时突然跳。 */
  function scheduleReload() {
    var auto = document.getElementById("auto-refresh");
    if (auto && !auto.checked) {
      toast("任务已改动数据，勾选「完成后自动刷新」可自动更新页面。", "info", { timeout: 0 });
      return;
    }
    toast("正在刷新页面…", "info", { timeout: 1200 });
    setTimeout(function () { location.reload(); }, 700);
  }

  // ---------------------------------------------------------------------
  // 确认弹窗
  // ---------------------------------------------------------------------
  /**
   * 把调用方写的 `**重点**` 渲染成 <strong>，其余按纯文本。
   *
   * 为什么不用 innerHTML：这些文案里会拼进条目标题、Key 名称这类**外部输入**
   * （平台上的 Issue 标题、用户自建的 Key 名），innerHTML 等于把它们当 HTML 执行。
   * 这里只切分、不解析，每段都走 createTextNode。
   *
   * 顺带修掉一个真实观感问题：调用方一直在文案里写 `**真提 PR**`，而旧实现用的是
   * textContent——用户看到的是字面的星号。
   */
  function emphasize(el, text) {
    var parts = String(text === undefined || text === null ? "" : text).split(/\*\*([^*]+)\*\*/g);
    for (var i = 0; i < parts.length; i++) {
      if (!parts[i]) continue;
      if (i % 2 === 1) {
        var strong = document.createElement("strong");
        strong.textContent = parts[i];
        el.appendChild(strong);
      } else {
        el.appendChild(document.createTextNode(parts[i]));
      }
    }
  }

  /**
   * 页内确认弹窗（替代原生 confirm）。
   * @param {string} title 标题
   * @param {string} message 正文，支持 `**加粗**`
   * @param {{danger?: boolean, yesText?: string, noText?: string}} [opts]
   *   danger：确认按钮用红色，用于删除/吊销这类不可撤销的操作；
   *   yesText：确认按钮文案，写成动词（「删除」「吊销」）比「确认」更明确。
   * @returns {Promise<boolean>}
   */
  function confirmDialog(title, message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = document.createElement("dialog");
      if (opts.danger) dlg.className = "danger";
      dlg.innerHTML =
        "<h3></h3><div class='dlg-body muted'></div>" +
        "<div class='dlg-actions'><button class='ghost' value='no'>取消</button>" +
        "<button class='primary' value='yes'></button></div>";
      dlg.querySelector("h3").textContent = title;
      emphasize(dlg.querySelector("div.dlg-body"), message);

      var yesBtn = dlg.querySelector("button[value=yes]");
      yesBtn.textContent = opts.yesText || "确认";
      // 危险操作换红色按钮：让「点在哪个按钮上」在配色上也说得通，而不只是靠读标题。
      if (opts.danger) {
        yesBtn.className = "danger";
        yesBtn.setAttribute("data-danger", "1");
      }
      var noBtn = dlg.querySelector("button[value=no]");
      noBtn.textContent = opts.noText || "取消";

      document.body.appendChild(dlg);
      dlg.addEventListener("close", function () {
        var yes = dlg.returnValue === "yes";
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(yes);
      });
      noBtn.addEventListener("click", function () { dlg.close("no"); });
      yesBtn.addEventListener("click", function () { dlg.close("yes"); });
      if (typeof dlg.showModal === "function") {
        dlg.showModal();
        // showModal 会把焦点丢到第一个可聚焦元素；危险弹窗显式挪到取消按钮。
        if (opts.danger) noBtn.focus();
      } else {
        resolve(window.confirm(title + "\n" + String(message).replace(/\*\*/g, "")));
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      }
    });
  }

  // ---------------------------------------------------------------------
  // 勾选批量
  // ---------------------------------------------------------------------
  function selectedKeys() {
    return Array.prototype.slice
      .call(document.querySelectorAll("input[name=keypick]:checked"))
      .map(function (cb) { return cb.value; });
  }

  function wireSelectAll() {
    var all = document.getElementById("pick-all");
    if (!all) return;
    all.addEventListener("change", function () {
      Array.prototype.forEach.call(document.querySelectorAll("input[name=keypick]"), function (cb) {
        cb.checked = all.checked;
      });
      updateBulkCount();
    });
    document.addEventListener("change", function (e) {
      if (e.target && e.target.name === "keypick") updateBulkCount();
    });
    updateBulkCount();
  }

  /**
   * 界面详略模式：simple / expert。
   *
   * 初始值由 base.html 的 head 内联脚本先写进 `<html data-mode>`（先于样式绘制，
   * 否则简单模式下会闪一下专家内容）。这里只负责「切换 + 记住 + 改写按钮文案」。
   * 存 localStorage 而不是服务端配置：这是本地观看偏好，换台机器不必跟着走。
   */
  var UI_MODE_KEY = "fissue.ui.mode";

  function currentMode() {
    return document.documentElement.getAttribute("data-mode") === "expert" ? "expert" : "simple";
  }

  function syncModeButton() {
    var btn = document.getElementById("mode-toggle");
    var hint = document.getElementById("mode-hint");
    var simple = currentMode() === "simple";
    // 按钮说的是「点了会变成什么」，而不是「现在是什么」——后者容易让人以为点它没生效。
    if (btn) {
      btn.textContent = simple ? "显示专家细节" : "回到简单模式";
      btn.setAttribute("aria-pressed", simple ? "false" : "true");
    }
    // 常驻那行说的是「现在是什么」：页面上少了一半信息时，得有个地方能看出原因是这个开关。
    if (hint) {
      hint.textContent = simple
        ? "界面：简单（只显示日常操作）"
        : "界面：专家（显示全部细节）";
    }
  }

  function toggleMode() {
    var next = currentMode() === "simple" ? "expert" : "simple";
    document.documentElement.setAttribute("data-mode", next);
    try {
      localStorage.setItem(UI_MODE_KEY, next);
    } catch (e) { /* 隐私模式写不了，本次生效即可 */ }
    syncModeButton();
    toast(next === "expert" ? "已显示专家细节" : "已切回简单模式", "info", { timeout: 2000 });
  }

  function wireModeToggle() {
    syncModeButton();
    var btn = document.getElementById("mode-toggle");
    if (btn) btn.addEventListener("click", toggleMode);
  }

  /**
   * 复制到剪贴板。
   *
   * 为什么不能只用 `navigator.clipboard`：它要求**安全上下文**（https 或 localhost）。
   * Fissue 常被装在局域网机器上、用 `http://192.168.x.x:8000` 打开——在那种页面上
   * `navigator.clipboard` 是 undefined，原先只有 apikeys 页用到它，一按就「不支持自动复制」。
   * 这里加一条 `execCommand('copy')` 回退，把这种情况也覆盖掉。
   */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      // 必须留在文档里且能选中才复制得到，所以移出视口而不是 display:none。
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error("这个浏览器不允许自动复制"));
    });
  }

  function updateBulkCount() {
    var keys = selectedKeys();
    var label = document.getElementById("pick-count");
    if (label) label.textContent = String(keys.length);
    Array.prototype.forEach.call(document.querySelectorAll("[data-needs-pick]"), function (btn) {
      btn.disabled = keys.length === 0;
    });
  }

  /**
   * 给 `.kv` 布局里的控件补可读名称。
   *
   * `.kv` 是「左边一个 `<div class="k">端点</div>`、右边一个 `<div class="v"><input></div>`」的
   * 视觉表格——看得见的人知道那个输入框叫「端点」，读屏用户只知道「编辑框」。设置页、
   * 初始化向导、条目详情大量使用这套结构，逐个模板加 `aria-label` 既啰嗦又容易漏，
   * 所以在渲染后统一按 `.k` 的文字补上。
   *
   * 只补**没有名称**的控件：已经有 label / aria-label / placeholder / title 的一律不动，
   * 免得把作者特意写好的名字覆盖成一个更笼统的词。
   */
  function wireKvLabels() {
    Array.prototype.forEach.call(document.querySelectorAll(".kv"), function (kv) {
      var children = Array.prototype.slice.call(kv.children);
      children.forEach(function (el, index) {
        if (!el.classList || !el.classList.contains("k")) return;
        var label = (el.textContent || "").trim();
        var valueCell = children[index + 1];
        if (!label || !valueCell) return;
        Array.prototype.forEach.call(
          valueCell.querySelectorAll("input, select, textarea"),
          function (control) {
            if (control.type === "hidden" || control.type === "checkbox" || control.type === "radio") return;
            if (control.getAttribute("aria-label") || control.getAttribute("aria-labelledby")) return;
            if (control.getAttribute("placeholder") || control.getAttribute("title")) return;
            if (control.labels && control.labels.length) return;
            control.setAttribute("aria-label", label);
          }
        );
      });
    });
  }

  // ---------------------------------------------------------------------
  // 表单 → 查询串
  // ---------------------------------------------------------------------
  function formQuery(form) {
    var out = {};
    new FormData(form).forEach(function (value, key) {
      if (key === "keypick") return;
      var el = form.elements[key];
      var isCheckbox = el && el.type === "checkbox";
      if (isCheckbox) {
        if (value !== "on") return;               // 未勾选的 checkbox 不会进 FormData
        out[key] = "1";
      } else {
        out[key] = value;
      }
    });
    return out;
  }

  // ---------------------------------------------------------------------
  // 常驻实时角标（左栏底部，每个页面都有）
  // ---------------------------------------------------------------------
  //
  // 为什么要有它：流水线是后台跑的，用户在「条目」页翻列表时看不到有没有东西在动，只能从
  // 「条目怎么不变」倒推。这里每几秒拉一次 /api/v1/status，把「谁在写、有什么任务」摆在明面上。
  var STATUS_IDLE_MS = 5000;
  var STATUS_BUSY_MS = 2000;

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text || "";
  }

  /** 已跑时长的短文本。拿不到就返回空串——跨进程时确实拿不到，别编一个数字出来。 */
  function writerDur(seconds) {
    if (seconds === null || seconds === undefined) return "";
    var total = Math.round(seconds);
    if (total < 60) return "（已跑 " + total + "s）";
    var minutes = Math.floor(total / 60);
    if (minutes < 60) return "（已跑 " + minutes + "m" + (total % 60) + "s）";
    return "（已跑 " + Math.floor(minutes / 60) + "h" + (minutes % 60) + "m）";
  }

  function renderLive(box, st) {
    var job = st.jobs && st.jobs.current;
    var busy = (st.writer && st.writer.busy) || !!job;
    box.dataset.state = busy ? "busy" : "idle";

    if (st.writer && st.writer.busy) {
      // 说清「是谁在写、跑了多久」。角标是**全站可见**的，原先这里只有一个 pid：用户既看不出
      // 那是常驻服务的轮次还是自己刚点的操作，也判断不出它是在干活还是卡住了。
      setText("live-text", st.writer.kind_label ? "写操作进行中：" + st.writer.kind_label : "写操作进行中");
      setText("live-sub", (st.writer.who || "") + writerDur(st.writer.seconds));
    } else if (job) {
      setText("live-text", "任务进行中：" + kindLabel(job.kind));
      setText("live-sub", job.progress || job.status);
    } else {
      setText("live-text", "空闲");
      var waiting = (st.queues || []).reduce(function (n, q) { return n + (q.waiting || 0); }, 0);
      var pending = (st.queues || []).reduce(function (n, q) { return n + (q.pending || 0); }, 0);
      var last = (st.scans || [])[0];
      var bits = [];
      if (waiting) bits.push("待定论 " + waiting);
      if (pending) bits.push("队列待处理 " + pending);
      bits.push(last ? "最近抓取 " + (last.started_at || "").replace("T", " ").slice(0, 16) : "尚未抓取过");
      setText("live-sub", bits.join(" · "));
    }

    var jobsRow = document.getElementById("live-jobs-row");
    if (jobsRow) {
      jobsRow.hidden = !job;
      if (job) setText("live-jobs", kindLabel(job.kind) + " " + job.id + "（" + job.status + "）");
    }
    return busy;
  }

  async function pollStatus() {
    var box = document.getElementById("live-box");
    if (!box) return null;
    try {
      // silent：角标每几秒拉一次，401 时不能触发跳转（否则页面会在用户打字时被抢走）。
      // 未登录时的正常路径是「页面守卫直接 303 到 /login」，走到这里说明会话刚过期。
      var st = await request("/status", {silent: true});
      return renderLive(box, st);
    } catch (err) {
      box.dataset.state = "error";
      setText("live-text", err.status === 401 ? "会话已过期" : "状态读取失败");
      setText("live-sub", err.status === 401 ? "刷新页面会重新登录" : err.message);
      return null;
    }
  }

  // 只保留「当前那一张票」：续期前先清掉旧票，否则「定时器还挂着 + 用户切回前台补一次」
  // 就会各起一条链、永久并行下去（状态页真出过这个事故：点一次按钮多一条轮询链）。
  var liveTimer = null;

  function scheduleLive(delay) {
    if (liveTimer !== null) clearTimeout(liveTimer);
    liveTimer = setTimeout(tickLive, delay);
  }

  async function tickLive() {
    liveTimer = null;
    // 页面看不见时只排期不发请求：后台标签页每 2~5 秒打一次服务端，既是白烧数据库往返，
    // 也会让「最后更新时间」在没人看的时候一直变。浏览器虽然会把后台定时器节流到分钟级，
    // 但那是**定时器**的节流——只要醒过来就仍然是一次真实请求，不如自己判掉。
    if (document.hidden) {
      scheduleLive(STATUS_IDLE_MS);
      return;
    }
    var busy = await pollStatus();
    // 忙的时候看得紧一点：用户正是此刻在等结果。await 期间可能又切了页签，
    // 所以这里统一走 scheduleLive：它清票重排，排队者最多只有一个定时器。
    scheduleLive(busy ? STATUS_BUSY_MS : STATUS_IDLE_MS);
  }

  function startLiveBadge() {
    if (!document.getElementById("live-box")) return;

    // 回到前台时立刻补一次：否则角标会停在切走那一刻的旧状态，看上去像「卡住了」。
    // 补一次也走 scheduleLive(0)，不是直接 pollStatus：清票重排保证它顶掉在途的那条链，
    // 而不是另起一条。
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) scheduleLive(0);
    });

    scheduleLive(0);
  }

  /** 停止**当前这一轮**（常驻服务的定时轮次）。
   *
   * 为什么单独有它：轮次在整轮期间持有写闸门、又不在作业表里，所以「取消任务」够不着它——
   * 原先只能去跑 `fissue serve` 的终端按 Ctrl-C，等于把整台服务停掉。这里让它可以在页面上停。
   * 代价必须当面说清：取消是打断式的，正在进行的**单条**操作可能只完成一半。
   */
  async function stopRound(btn) {
    var ok = await confirmDialog(
      "停止当前轮次",
      "这一轮会在最近的等待点中断；正在进行的单条操作（例如正在建 PR）可能只完成一半。" +
      "停的只是这一轮，服务与之后按间隔的轮次照常。确认停止？"
    );
    if (!ok) return;
    btn.disabled = true;
    try {
      var res = await request("/round/stop", {method: "POST"});
      toast(
        "已请求停止：" + (res.stopped || "当前轮次") + "（已跑 " + Math.round(res.seconds || 0) + "s）",
        "info",
        {timeout: 6000}
      );
      setTimeout(function () { window.location.reload(); }, 1500);
    } catch (err) {
      toast("没能停止", "bad", {detail: err.message, timeout: 0});
      btn.disabled = false;
    }
  }

  /**
   * 相对时间格式化：将 ISO 8601 时间戳转换为「2 小时前」「3 天前」等友好格式。
   * @param {string} isoString ISO 8601 格式的时间戳（如 "2024-03-15T14:23:45"）
   * @param {boolean} withAbsolute 是否在 title 属性中保留绝对时间（用于 hover 显示）
   * @returns {string} HTML 字符串，如果 withAbsolute=true 则带 title 属性
   */
  function relativeTime(isoString, withAbsolute) {
    if (!isoString) return "—";
    var date = new Date(isoString);
    if (isNaN(date.getTime())) return escapeHtml(isoString);
    
    var now = new Date();
    var diffMs = now - date;
    var diffSec = Math.floor(diffMs / 1000);
    var diffMin = Math.floor(diffSec / 60);
    var diffHour = Math.floor(diffMin / 60);
    var diffDay = Math.floor(diffHour / 24);
    
    var relative;
    if (diffSec < 60) {
      relative = "刚刚";
    } else if (diffMin < 60) {
      relative = diffMin + " 分钟前";
    } else if (diffHour < 24) {
      relative = diffHour + " 小时前";
    } else if (diffDay < 7) {
      relative = diffDay + " 天前";
    } else if (diffDay < 30) {
      var weeks = Math.floor(diffDay / 7);
      relative = weeks + " 周前";
    } else if (diffDay < 365) {
      var months = Math.floor(diffDay / 30);
      relative = months + " 个月前";
    } else {
      var years = Math.floor(diffDay / 365);
      relative = years + " 年前";
    }
    
    if (withAbsolute) {
      var absolute = isoString.replace("T", " ").slice(0, 19);
      return '<span title="' + escapeHtml(absolute) + '">' + escapeHtml(relative) + '</span>';
    }
    return escapeHtml(relative);
  }

  window.Fissue = {
    api: request,
    toast: toast,
    runJob: runJob,
    cancelJob: cancelJob,
    confirm: confirmDialog,
    selectedKeys: selectedKeys,
    updateBulkCount: updateBulkCount,
    formQuery: formQuery,
    escapeHtml: escapeHtml,
    relativeTime: relativeTime,
    copyText: copyText,
    kindLabel: kindLabel,
    statusClass: statusClass,
    pollStatus: pollStatus,
    logout: logout,
    readOnly: READ_ONLY,
  };

  document.addEventListener("DOMContentLoaded", function () {
    // `[data-time]`：相对时间占位。
    //
    // 模板以前写 `<script>document.write(Fissue.relativeTime(...))</script>`，
    // 但那段脚本在 content 块里，**比本文件先执行**——那时 window.Fissue 还不存在，
    // 于是每行一条 "Fissue is not defined"，时间列也永远渲染不出来。
    // 现在模板只输出 ISO 时间，格式化统一放到这里。
    Array.prototype.forEach.call(document.querySelectorAll("[data-time]"), function (el) {
      el.innerHTML = relativeTime(el.getAttribute("data-time"), true);
    });

    startLiveBadge();
    wireSelectAll();
    wireKvLabels();
    wireModeToggle();
    // `[data-autosubmit]`：筛选类下拉框选完就提交。用事件委托而不是逐个绑定，
    // 因为这类控件会随页面/模板增减（流水线的仓库筛选、条目页的每页条数）。
    // 仍然保留 <noscript> 里的提交按钮：禁用 JS 时表单还得能用。
    document.addEventListener("change", function (e) {
      var el = e.target;
      if (el && el.matches && el.matches("[data-autosubmit]") && el.form) {
        if (typeof el.form.requestSubmit === "function") el.form.requestSubmit();
        else el.form.submit();
      }
    });
    // 登出按钮只在 base.html 判断「当前是会话身份」时才渲染出来；这里无脑接一下。
    var logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) logoutBtn.addEventListener("click", logout);
    // `[data-copy]`：任何标了这个属性的元素点一下就复制它自身内容。
    // 用事件委托，模板侧只要写 `<button class="copy-btn" data-copy="{{ 值 }}">`，
    // 不必每处再挂一次 onclick（onclick 里插值还会被 JSON 的双引号截断）。
    document.addEventListener("click", function (e) {
      var el = e.target && e.target.closest ? e.target.closest("[data-copy]") : null;
      if (!el) return;
      e.preventDefault();
      var text = el.dataset.copy || "";
      if (!text) return;
      copyText(text).then(
        function () {
          toast("已复制：" + (text.length > 48 ? text.slice(0, 48) + "…" : text), "ok", { timeout: 2000 });
        },
        function () {
          // 复制失败时把原文摊在提示里，用户至少能手动选中它。
          toast("没能自动复制，请手动选中下面的内容", "bad", { detail: text, timeout: 0 });
        }
      );
    });
    // 取消按钮在所有页面上共用这一处接线（总览的任务面板与「任务」页都用 data-act=cancel-job）。
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest("button[data-act=cancel-job]") : null;
      if (btn) cancelJob(btn.dataset.job);
    });
    // 「停止这一轮」只在「任务」页那条「不在本表」提示里出现（常驻轮次才给）。
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest("button[data-act=stop-round]") : null;
      if (btn) stopRound(btn);
    });
    // 「全选」表头的 indeterminate 态：勾了一部分时给个可见的中间态。
    var all = document.getElementById("pick-all");
    if (all) {
      var sync = function () {
        var boxes = Array.prototype.slice.call(document.querySelectorAll("input[name=keypick]"));
        var picked = boxes.filter(function (b) { return b.checked; }).length;
        all.indeterminate = picked > 0 && picked < boxes.length;
        all.checked = boxes.length > 0 && picked === boxes.length;
      };
      document.addEventListener("change", sync);
      sync();
    }
  });
})();
