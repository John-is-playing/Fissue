/* 新手引导页的行为（docs/DEPLOY.md §15）。
 *
 * 三条约束写在最前面，改动时别绕过：
 *   1. 页面能点的只有 GUIDE_ACTIONS 里的固定动作；引导内容里的 action 只是一个 key，
 *      真正的 URL 由这里拼死（`/guides/actions`），模型没法指定别的地方。
 *   2. 半自动/自动档执行前必须把「将调用哪个接口、带什么参数」明明白白摆出来。
 *   3. 一键部署任何一步失败 → 自动切回只读档、展开手动命令、高亮失败原因。
 *      不是弹个错就完了：新手最常见的死法是「点了一键、红了、不知道该干什么」。
 *
 * 凭据不在这里回显：登记主机的私钥/密码只进请求体，提交后立刻清空输入框。
 */
(function () {
  "use strict";

  var root = document.getElementById("guide-root");
  if (!root) return;

  var api = window.Fissue.api;
  var toast = window.Fissue.toast || function () {};
  var confirmDialog = window.Fissue.confirm;
  var copyText = window.Fissue.copyText;
  var PREFIX = (window.FISSUE && window.FISSUE.apiPrefix) || "/api/v1";
  var ACTIONS_URL = PREFIX + "/guides/actions";

  var MODE_KEY = "fissue.guide.mode";
  var MODE_TEXT = {
    readonly: "只读：只展示命令与解释，自己复制执行。第一次看、生产谨慎操作时用它。",
    half: "半自动：每步一个按钮；点之前会先把将调用的接口与参数显示出来。适合已经看懂在做什么的人。",
    auto: "一键：按固定顺序跑「生成计划 → 执行 → 检查」。失败会自动切回只读档并展开手动命令。"
  };

  var repoInput = document.getElementById("guide-repo");
  var hostSelect = document.getElementById("guide-host");
  var genBtn = document.getElementById("guide-regenerate");
  var genStatus = document.getElementById("guide-gen-status");
  var modeDesc = document.getElementById("guide-mode-desc");
  var autoBox = document.getElementById("guide-auto-box");
  var autoBtn = document.getElementById("guide-auto-run");
  var autoProgress = document.getElementById("guide-auto-progress");
  var autoSteps = document.getElementById("guide-auto-steps");
  var autoFail = document.getElementById("guide-auto-fail");
  var autoFailReason = document.getElementById("guide-auto-fail-reason");

  var mode = "readonly";
  try {
    var saved = localStorage.getItem(MODE_KEY);
    if (saved === "half" || saved === "auto" || saved === "readonly") mode = saved;
  } catch (e) { /* 隐私模式下 localStorage 不可用，用默认值 */ }

  // -- 当前选择 -----------------------------------------------------------
  function currentRepo() { return repoInput ? repoInput.value.trim() : (root.dataset.repo || ""); }
  function currentHost() { return hostSelect ? hostSelect.value : (root.dataset.host || ""); }
  function deployEnabled() { return root.dataset.deployEnabled === "1"; }

  function actionBody(action) {
    // 只带固定字段；模型写的步骤无法往这里注入 URL。
    var body = { action: action, repo_slug: currentRepo(), host: currentHost() };
    if (action === "check") {
      // 一键流程里没有 deployment_id：后端会取该仓库在该主机上最近一次成功部署。
      body.allow_dangerous = false;
    }
    return body;
  }

  // -- 档位切换 -----------------------------------------------------------
  function setMode(next, opts) {
    opts = opts || {};
    mode = next;
    try { localStorage.setItem(MODE_KEY, next); } catch (e) { /* 忽略 */ }
    Array.prototype.forEach.call(document.querySelectorAll(".guide-mode"), function (btn) {
      var on = btn.dataset.mode === next;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    if (modeDesc) modeDesc.textContent = MODE_TEXT[next];
    // 半自动才有「执行这一步」；自动档用一键按钮，避免两种入口同时出现。
    Array.prototype.forEach.call(document.querySelectorAll(".guide-run-row"), function (row) {
      var btn = row.querySelector(".guide-run");
      if (btn) btn.hidden = next !== "half";
    });
    if (autoBox) autoBox.hidden = next !== "auto";
    if (next !== "auto" && !opts.keepProgress && autoProgress) autoProgress.hidden = true;
  }

  Array.prototype.forEach.call(document.querySelectorAll(".guide-mode"), function (btn) {
    btn.addEventListener("click", function () { setMode(btn.dataset.mode); });
  });

  // -- 复制（失败时选中文本，让人至少能手动 Ctrl+C） ----------------------
  function selectNodeText(el) {
    if (!el || !window.getSelection) return false;
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
  }

  function copyStep(stepEl) {
    var pre = stepEl ? stepEl.querySelector(".guide-command") : null;
    var text = pre ? pre.textContent : "";
    var note = stepEl ? stepEl.querySelector("[data-step-note]") : null;
    if (!text) return;
    copyText(text).then(
      function () {
        if (note) note.textContent = "已复制";
        toast("已复制命令", "ok", { timeout: 2000 });
      },
      function () {
        // 降级：选中代码块文本，用户按 Ctrl+C 即可。
        selectNodeText(pre);
        if (note) note.textContent = "自动复制失败，已选中文本，请按 Ctrl+C";
        toast("没能自动复制，已选中命令文本，请按 Ctrl+C", "bad", { timeout: 0 });
      }
    );
  }

  // -- 执行单个固定动作（半自动） -----------------------------------------
  async function runStep(btn) {
    var action = btn.dataset.runAction;
    var stepEl = btn.closest(".guide-step");
    var resultEl = stepEl ? stepEl.querySelector("[data-run-result]") : null;

    if (action === "add-host") {
      // add-host 要结构化主机信息，直接跳到下面的表单，不弹确认。
      var panel = document.getElementById("guide-add-host-panel");
      if (panel) {
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
        var nameEl = document.getElementById("guide-host-name");
        if (nameEl) nameEl.focus();
      }
      if (resultEl) resultEl.textContent = "请在下面的「登记主机」表单里填写。";
      return;
    }
    if (!currentRepo()) {
      toast("先选一个仓库，再执行这一步。", "bad");
      return;
    }
    if (resultEl) {
      resultEl.textContent = "接口：" + ACTIONS_URL + "　参数：" + JSON.stringify(actionBody(action));
    }
    var ok = await confirmDialog(
      "执行固定动作：" + action,
      "接口：POST " + ACTIONS_URL +
      "；参数：" + JSON.stringify(actionBody(action), null, 2) +
      "。这是引导页白名单里的固定动作，确认执行？",
      { yesText: "执行" }
    );
    if (!ok) return;

    btn.disabled = true;
    if (resultEl) resultEl.textContent = "执行中…";
    try {
      var res = await api("/guides/actions", { method: "POST", body: actionBody(action) });
      var text = summarize(action, res);
      if (resultEl) resultEl.textContent = text;
      toast("已执行：" + action, "ok", { detail: text, timeout: 6000 });
    } catch (ex) {
      var reason = (ex && ex.message) || String(ex);
      if (resultEl) resultEl.textContent = "失败：" + reason;
      toast("执行失败：" + action, "bad", { detail: reason, timeout: 0 });
    } finally {
      btn.disabled = false;
    }
  }

  function summarize(action, res) {
    if (!res) return "已完成";
    if (action === "generate-plan") {
      return "已生成计划：部署 id=" + res.deployment_id + "，共 " + res.steps + " 步" +
        (res.warning ? "（提醒：" + res.warning + "）" : "");
    }
    if (action === "apply") {
      return "已受理部署：后台执行中，进度见「部署记录」。";
    }
    if (action === "check") {
      var dep = res.deployment || {};
      if (!dep.id) return "还没有可检查的部署记录。";
      return "部署 id=" + dep.id + " 状态=" + dep.status +
        (dep.error ? "，错误：" + dep.error : "") +
        "，步骤 " + (res.steps || []).length + " 条";
    }
    if (action === "rollback") {
      return "回滚结果：" + ((res.report && res.report.status) || "已提交");
    }
    return JSON.stringify(res);
  }

  // -- 一键部署：generate-plan → apply → check ----------------------------
  function appendProgress(label, text, kind) {
    if (!autoSteps) return null;
    var row = document.createElement("div");
    row.className = "row mt small";
    var tag = document.createElement("span");
    tag.className = "tag" + (kind === "bad" ? " status-failed" : kind === "ok" ? " status-verifier_ready" : "");
    tag.textContent = label;
    var body = document.createElement("span");
    body.className = "muted";
    body.style.whiteSpace = "pre-wrap";
    body.textContent = text || "";
    row.appendChild(tag);
    row.appendChild(body);
    autoSteps.appendChild(row);
    return { row: row, tag: tag, body: body };
  }

  function failureReason(ex, action, res) {
    if (ex) return (ex && ex.message) || String(ex);
    if (res) {
      var dep = res.deployment || {};
      if (dep.error) return dep.error;
      var failed = (res.steps || []).filter(function (s) {
        return s && (s.status === "failed" || s.error);
      })[0];
      if (failed) {
        return "步骤 #" + failed.seq + " " + failed.step_id + "：" + (failed.error || "执行失败");
      }
    }
    return action + " 返回了失败状态";
  }

  function highlightFailed(action, reason) {
    // 展开所有阶段：降级后的手动命令必须直接看得见，不能再让人去点折叠。
    Array.prototype.forEach.call(document.querySelectorAll("details.guide-stage"), function (d) {
      d.open = true;
    });
    // 上一次失败的标记先清掉，避免反复点「一键部署」把同一段原因堆成好几份。
    Array.prototype.forEach.call(document.querySelectorAll("[data-auto-fail]"), function (box) {
      box.parentNode.removeChild(box);
    });
    var target = null;
    Array.prototype.forEach.call(document.querySelectorAll(".guide-step"), function (el) {
      if (action && el.dataset.action === action && !target) target = el;
    });
    if (!target) return;
    target.style.outline = "2px solid var(--bad, #ff6b6b)";
    target.style.outlineOffset = "4px";
    target.setAttribute("data-failed", "1");
    // 失败原因直接贴在对应步骤上：新手在这一步停下，就该在这一步看到为什么。
    var alert = document.createElement("div");
    alert.className = "alert error mt";
    alert.setAttribute("data-auto-fail", "1");
    var level = document.createElement("span");
    level.className = "a-level";
    level.textContent = "失败原因";
    var body = document.createElement("div");
    body.textContent = reason || "这一步失败了";
    alert.appendChild(level);
    alert.appendChild(body);
    target.insertBefore(alert, target.firstChild);
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function autoFailed(action, ex, res) {
    var reason = failureReason(ex, action, res);
    // 硬性要求：失败就降级到只读，而不是留在自动档反复重试。
    setMode("readonly", { keepProgress: true });
    highlightFailed(action, reason);
    if (autoFail) autoFail.hidden = false;
    if (autoFailReason) {
      autoFailReason.textContent = "自动部署在第「" + action + "」步失败：" + reason;
    }
    if (autoProgress) autoProgress.hidden = false;
    toast("一键部署失败，已切回只读档", "bad", { detail: reason, timeout: 0 });
  }

  async function runAuto() {
    if (!currentRepo()) { toast("先选一个仓库。", "bad"); return; }
    if (!deployEnabled()) { toast("部署能力未开启（deploy.enabled=false）。", "bad"); return; }
    autoBtn.disabled = true;
    if (autoProgress) autoProgress.hidden = false;
    if (autoFail) autoFail.hidden = true;
    if (autoSteps) autoSteps.textContent = "";

    var plan = ["generate-plan", "apply", "check"];
    for (var i = 0; i < plan.length; i += 1) {
      var action = plan[i];
      var body = actionBody(action);
      var line = appendProgress(
        "第 " + (i + 1) + " 步 · " + action,
        "POST " + ACTIONS_URL + "  参数 " + JSON.stringify(body) + "\n执行中…",
        "run"
      );
      var res;
      try {
        res = await api("/guides/actions", { method: "POST", body: body });
      } catch (ex) {
        if (line) { line.tag.className = "tag status-failed"; line.tag.textContent = "失败 · " + action; line.body.textContent = "失败：" + ((ex && ex.message) || ex); }
        autoBtn.disabled = false;
        autoFailed(action, ex, null);
        return;
      }
      // check 是「看结果」：显式的 failed 状态也要当成失败来处理；还没跑完则提示稍后再看。
      if (action === "check" && res && res.deployment && res.deployment.status === "failed") {
        if (line) { line.tag.className = "tag status-failed"; line.tag.textContent = "失败 · check"; }
        autoBtn.disabled = false;
        autoFailed(action, null, res);
        return;
      }
      var text = summarize(action, res);
      if (action === "check" && (!res || !res.deployment || !res.deployment.id)) {
        text = "部署已提交到后台执行，暂时还没有可检查的成功记录；完成后回到本页再点「检查」。";
      }
      if (line) {
        line.tag.className = "tag status-verifier_ready";
        line.tag.textContent = "完成 · " + action;
        line.body.textContent = text;
      }
    }
    autoBtn.disabled = false;
    if (autoFail) autoFail.hidden = true;
    toast("一键部署流程已跑完", "ok", { detail: "生成计划 → 执行 → 检查；失败细节见页面进度。", timeout: 6000 });
  }

  // -- 重新生成 + 轮询 ----------------------------------------------------
  function setGenStatus(text) { if (genStatus) genStatus.textContent = text || ""; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function regenerate() {
    var repo = currentRepo();
    if (!repo) { toast("先填仓库（owner/name）。", "bad"); return; }
    var host = currentHost();
    var before = root.dataset.updated || "";
    genBtn.disabled = true;
    setGenStatus("生成中…（可能要几十秒，别关页面）");
    try {
      await api("/guides", { method: "POST", body: { repo_slug: repo, host: host } });
    } catch (ex) {
      genBtn.disabled = false;
      setGenStatus("生成失败：" + ((ex && ex.message) || ex));
      toast("生成引导失败", "bad", { detail: (ex && ex.message) || String(ex), timeout: 0 });
      return;
    }
    var deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      await sleep(2000);
      try {
        var fresh = await api("/guides", { query: { repo_slug: repo, host: host } });
        var updated = (fresh && fresh.updated_at) || "";
        if (updated && updated !== before) {
          setGenStatus("已生成，正在刷新…");
          toast("引导已更新", "ok");
          window.setTimeout(function () { window.location.reload(); }, 400);
          return;
        }
      } catch (ex) {
        // 404 = 后台还在写；其它错误也不该打断轮询（生成是后台任务，失败也会落兜底）。
        if (ex && ex.status && ex.status !== 404 && ex.status !== 401 && ex.status !== 403) {
          setGenStatus("轮询出错了，继续等：" + ((ex && ex.message) || ex));
        }
      }
    }
    genBtn.disabled = false;
    setGenStatus("生成超时（120 秒）。后台可能仍在跑；稍后刷新页面看看。");
    toast("生成引导超时", "bad", { detail: "120 秒内没看到新的 updated_at。", timeout: 0 });
  }

  // -- 登记主机（add-host） ------------------------------------------------
  function numValue(id, fallback) {
    var el = document.getElementById(id);
    var v = el ? parseInt(el.value, 10) : NaN;
    return isNaN(v) ? fallback : v;
  }

  async function submitHost() {
    var secretEl = document.getElementById("guide-host-secret");
    var resultEl = document.getElementById("guide-host-result");
    var spec = {
      name: (document.getElementById("guide-host-name") || {}).value || "",
      address: (document.getElementById("guide-host-address") || {}).value || "",
      user: (document.getElementById("guide-host-user") || {}).value || "root",
      port: numValue("guide-host-port", 22),
      auth_method: (document.getElementById("guide-host-auth") || {}).value || "key",
      credential: secretEl ? secretEl.value : "",
      app_root: null
    };
    spec.name = spec.name.trim();
    spec.address = spec.address.trim();
    var appRoot = document.getElementById("guide-host-app-root");
    if (appRoot && appRoot.value.trim()) spec.app_root = appRoot.value.trim();

    if (!spec.name || !spec.address || !spec.credential) {
      sayHost("名字、地址、凭据都要填。", true);
      return;
    }
    if (!currentRepo()) {
      sayHost("先在页面顶部选一个仓库。", true);
      return;
    }

    var btn = document.getElementById("guide-host-submit");
    if (btn) btn.disabled = true;
    sayHost("提交中…", false);
    try {
      var res = await api("/guides/actions", {
        method: "POST",
        body: {
          action: "add-host",
          repo_slug: currentRepo(),
          host: currentHost(),
          host_spec: spec
        }
      });
      // 成功之后凭据框必须立刻清空：它已经交出去了，留在页面上只是多一份明文副本。
      if (secretEl) secretEl.value = "";
      sayHost("已登记：" + (res.name || spec.name) + "。凭据已加密入库，不做回显。", false);
      toast("已登记主机：" + (res.name || spec.name), "ok");
      // 主机名会进入下拉；刷新一次页面最省事（也刷新引导内容里的 host）。
      window.setTimeout(function () { window.location.reload(); }, 600);
    } catch (ex) {
      // 失败也清空：重试要重新粘贴，避免旧的明文一直挂在 DOM 里。
      if (secretEl) secretEl.value = "";
      sayHost("失败：" + ((ex && ex.message) || ex), true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function sayHost(text, bad) {
    var el = document.getElementById("guide-host-result");
    if (el) el.textContent = text || "";
    if (bad) toast(text, "bad", { timeout: 0 });
  }

  // -- 事件接线 -----------------------------------------------------------
  if (genBtn) genBtn.addEventListener("click", regenerate);
  if (autoBtn) autoBtn.addEventListener("click", runAuto);

  var submitBtn = document.getElementById("guide-host-submit");
  if (submitBtn) submitBtn.addEventListener("click", submitHost);

  document.addEventListener("click", function (e) {
    var copyBtn = e.target && e.target.closest ? e.target.closest("[data-copy-step]") : null;
    if (copyBtn) {
      copyStep(copyBtn.closest(".guide-step"));
      return;
    }
    var runBtn = e.target && e.target.closest ? e.target.closest("[data-run-action]") : null;
    if (runBtn && !runBtn.hidden) runStep(runBtn);
  });

  setMode(mode);
})();