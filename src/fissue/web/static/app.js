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
  function toast(message, kind, opts) {
    opts = opts || {};
    var host = toastHost();
    var box = document.createElement("div");
    box.className = "toast " + (kind || "info");

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
    head.appendChild(text);
    head.appendChild(close);
    box.appendChild(head);

    if (opts.detail !== undefined && opts.detail !== null) {
      var pre = document.createElement("pre");
      pre.textContent =
        typeof opts.detail === "string" ? opts.detail : JSON.stringify(opts.detail, null, 2);
      box.appendChild(pre);
    }

    function dismiss() {
      if (box.parentNode) box.parentNode.removeChild(box);
    }
    close.addEventListener("click", dismiss);
    close.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") dismiss();
    });
    host.appendChild(box);

    // 失败信息不自动消失：它通常带着用户需要照着改的原因。
    var timeout = opts.timeout !== undefined ? opts.timeout : kind === "bad" ? 0 : 5000;
    if (timeout > 0) setTimeout(dismiss, timeout);
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
  function confirmDialog(title, message) {
    return new Promise(function (resolve) {
      var dlg = document.createElement("dialog");
      dlg.innerHTML =
        "<h3></h3><div class='muted'></div>" +
        "<div class='dlg-actions'><button class='ghost' value='no'>取消</button>" +
        "<button class='primary' value='yes'>确认</button></div>";
      dlg.querySelector("h3").textContent = title;
      dlg.querySelector("div.muted").textContent = message;
      document.body.appendChild(dlg);
      dlg.addEventListener("close", function () {
        var yes = dlg.returnValue === "yes";
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(yes);
      });
      dlg.querySelector("button[value=no]").addEventListener("click", function () { dlg.close("no"); });
      dlg.querySelector("button[value=yes]").addEventListener("click", function () { dlg.close("yes"); });
      if (typeof dlg.showModal === "function") dlg.showModal();
      else { resolve(window.confirm(title + "\n" + message)); if (dlg.parentNode) dlg.parentNode.removeChild(dlg); }
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

  function updateBulkCount() {
    var keys = selectedKeys();
    var label = document.getElementById("pick-count");
    if (label) label.textContent = String(keys.length);
    Array.prototype.forEach.call(document.querySelectorAll("[data-needs-pick]"), function (btn) {
      btn.disabled = keys.length === 0;
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

  function startLiveBadge() {
    if (!document.getElementById("live-box")) return;
    (async function loop() {
      var busy = await pollStatus();
      // 忙的时候看得紧一点：用户正是此刻在等结果。
      setTimeout(loop, busy ? STATUS_BUSY_MS : STATUS_IDLE_MS);
    })();
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
    kindLabel: kindLabel,
    statusClass: statusClass,
    pollStatus: pollStatus,
    logout: logout,
    readOnly: READ_ONLY,
  };

  document.addEventListener("DOMContentLoaded", function () {
    startLiveBadge();
    wireSelectAll();
    // 登出按钮只在 base.html 判断「当前是会话身份」时才渲染出来；这里无脑接一下。
    var logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) logoutBtn.addEventListener("click", logout);
    // 取消按钮在所有页面上共用这一处接线（总览的任务面板与「任务」页都用 data-act=cancel-job）。
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest("button[data-act=cancel-job]") : null;
      if (btn) cancelJob(btn.dataset.job);
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
