/* 设置页的前端逻辑（原先内联在 settings.html 里，320 行）。
 *
 * 为什么搬出来：settings.html 一度 788 行，其中四成是这段 JS。它与页面内容耦合很紧
 * （同一套 id 约定），但**与模板无关**——只依赖两个值：`view.writable`（本机是否允许
 * 从浏览器改配置，与全局 `readOnly` 不是一回事）与 auth 前缀（已在 window.FISSUE 里）。
 * 拆出来之后：
 *   * settings.html 只剩结构，能一眼看出页面分了哪几块；
 *   * 这段 JS 可以被 lint / 编辑器正常对待（内联在 .html 里时，多数工具当文本处理）；
 *   * 改前端不用在 788 行的模板里翻。
 *
 * 依赖注入：window.FISSUE_SETTINGS 由 settings.html 用一小段内联脚本设好
 * （只有 writable 一个布尔值，不值得为它单开一个请求）。
 */
(function () {
  "use strict";

  var SETTINGS = window.FISSUE_SETTINGS || {};
  var WRITABLE = !!SETTINGS.writable;
  var FISSUE = window.FISSUE || {};
  var AUTH_PREFIX = FISSUE.authPrefix || "/api/auth";

  var SECRETS = ["FISSUE_LLM_API_KEY", "FISSUE_GITHUB_TOKEN", "FISSUE_GITEE_TOKEN",
                 "FISSUE_ATOMGIT_TOKEN", "FISSUE_GITLAB_TOKEN",
                 "FISSUE_DATABASE_URL"];     // 连接串内含口令，按密钥处理
  var PLAIN = ["FISSUE_LLM_BASE_URL", "FISSUE_LLM_MODEL", "FISSUE_LLM_FALLBACK_MODEL",
               "FISSUE_WEB_HOST", "FISSUE_WEB_PORT"];

  function val(id) {
    var el = document.getElementById(id);
    return el ? el.value.trim() : "";
  }
  function checked(id) {
    var el = document.getElementById(id);
    return !!(el && el.checked);
  }
  function intOr(id, fallback) {
    var raw = val(id);
    if (!raw) return fallback;
    var n = parseInt(raw, 10);
    return isNaN(n) ? fallback : n;
  }

  /** 浮点版（单价这类值用）：解析不出来就回落到当前生效值，绝不把 NaN 写进配置。 */
  function numOr(id, fallback) {
    var raw = val(id);
    if (!raw) return fallback;
    var n = parseFloat(raw);
    return isNaN(n) ? fallback : n;
  }

  /** 组装 .env 的改动。
   *
   * 密钥的规则是**留空 = 不改动**：输入框里是空的（只显示掩码占位符），若把空串当值写下去，
   * 用户点一次「保存设置」就会把已有的 key 抹掉。要清空必须显式勾「清空」。
   */
  function envPayload() {
    var out = {};
    SECRETS.forEach(function (k) {
      var v = val("env-" + k);
      if (v) out[k] = v;
      else if (checked("clear-" + k)) out[k] = "";
    });
    PLAIN.forEach(function (k) {
      var v = val("env-" + k);
      if (v) out[k] = v;
    });
    return out;
  }

  /** 组装覆盖层的改动（鉴权与访问控制那一组）。
   *
   * 可信网段按行拆、去空、去重：让用户可以直接粘一段带空行的列表。
   * 服务端会再校验一次 CIDR（写不合法就 400），所以这里不做格式判断。
   */
  function overlayPayload() {
    var nets = val("overlay-trusted").split("\n")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    var seen = {};
    nets = nets.filter(function (ip) { if (seen[ip]) return false; seen[ip] = true; return true; });
    return {
      web: {
        allow_remote_config_write: checked("overlay-allow-remote"),
        auth: {
          enabled: checked("overlay-auth-enabled"),
          session_ttl_minutes: intOr("overlay-session-ttl", 480),
          secure_cookie: checked("overlay-secure-cookie"),
          login_max_failures: intOr("overlay-max-failures", 5)
        },
        trusted_networks: nets
      },
      // 计费 / 预算 / 生成参数：整组一起交（页面上的值是**生效值**回填，所见即所写）。
      // 边界由服务端的 pydantic 约束兜底，这里不做格式判断——写不合法就 400、文件一字不动。
      llm: llmPayload()
    };
  }

  /** 模型参数与计费那一组。 */
  function llmPayload() {
    return {
      temperature: numOr("overlay-llm-temperature", 0.2),
      max_tokens: intOr("overlay-llm-max-tokens", 16384),
      max_tokens_ceiling: intOr("overlay-llm-ceiling", 32768),
      timeout_seconds: intOr("overlay-llm-timeout", 120),
      max_retries: intOr("overlay-llm-retries", 4),
      concurrency: intOr("overlay-llm-concurrency", 4),
      pricing: {
        input_per_1k: numOr("overlay-llm-price-in", 0.00014),
        output_per_1k: numOr("overlay-llm-price-out", 0.00028)
      },
      budget: {
        daily_tokens_per_repo: intOr("overlay-llm-budget-tokens", 2000000),
        daily_usd_per_repo: numOr("overlay-llm-budget-usd", 5),
        hard_stop: checked("overlay-llm-hard-stop")
      }
    };
  }

  function fail(err) {
    Fissue.toast("操作失败", "bad", {detail: err.message, timeout: 0});
  }

  // ---------------------------------------------------------------------
  // 未保存改动：提示 + 离页拦截
  // ---------------------------------------------------------------------
  //
  // 为什么必须有：这一页是「填完要点保存」的模型，而我原先只在最下面一个无关面板里放了保存
  // 按钮、也没有任何「还没保存」的提示。实测结果是用户改完模型直接离开，一次 PUT 都没发出去，
  // 表现为「保存不了」——**这不是后端问题，是界面没告诉他还有一步**。
  var dirty = false;

  function markDirty() {
    if (dirty) return;
    dirty = true;
    var bar = document.getElementById("save-bar");
    if (bar) bar.classList.add("dirty");
    var hint = document.getElementById("dirty-hint");
    if (hint) {
      hint.className = "small";
      hint.innerHTML = "有未保存的改动 —— 点左边的「保存设置」才会写进文件（现在点「测试连通」只是试一下，不保存）。";
    }
  }

  document.addEventListener("input", function (e) {
    if (e.target && e.target.closest && e.target.closest("#save-bar")) return;  // 不把按钮本身当编辑
    markDirty();
  });
  document.addEventListener("change", markDirty);

  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";      // 现代浏览器据此弹「离开此页面？」确认框
  });

  function saveSettings() {
    if (!WRITABLE) { Fissue.toast("本机当前不允许从浏览器改写配置。", "bad"); return; }
    Fissue.api("/settings", {method: "PUT", body: {env: envPayload(), overlay: overlayPayload()}})
      .then(function (r) {
        dirty = false;    // 先清脏标记，否则下面那句 reload 会被自己的离页拦截弹框挡住
        var msg = "已保存并热重载：" + (r.written_env.length ? r.written_env.join("、") : "（无 .env 改动）");
        if (r.written_overlay && r.written_overlay.length) msg += "；覆盖层：" + r.written_overlay.join("、");
        if (r.restart_required && r.restart_required.length) msg += "；需重启才生效：" + r.restart_required.join("、");
        Fissue.toast(msg, "ok", {timeout: 8000});
        setTimeout(function () { location.reload(); }, 1200);
      })
      .catch(function (err) {
        // 「可信网段写错了」这类会被服务端 400 打回，那时文件一字未动——
        // 说清楚这一点，否则用户会以为「部分保存了」。
        Fissue.toast("保存失败（文件未改动）", "bad", {detail: err.message, timeout: 0});
      });
  }

  // ---------------------------------------------------------------------
  // 管理员账号：改口令 / 2FA（即点即生效，不经过「保存设置」）
  // ---------------------------------------------------------------------
  //
  // 与上面那套刻意分开：这些值住在数据库（auth_users / sessions / api_keys），
  // 不是配置文件。把它们做成「填完要点保存」会让用户以为口令也是一个配置项。
  function authCall(path, opts) {
    opts = opts || {};
    var method = opts.method || "POST";
    var headers = {"Content-Type": "application/json"};
    if (FISSUE.csrfToken && method !== "GET") headers["X-CSRF-Token"] = FISSUE.csrfToken;
    return fetch(AUTH_PREFIX + path, {
      method: method,
      headers: headers,
      credentials: "same-origin",
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var detail = data && data.detail;
          if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
          throw new Error(detail || ("HTTP " + res.status));
        }
        return data || {};
      });
    });
  }

  function setText(id, text, ok) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = text ? (ok ? '<span class="ok">' : '<span class="bad">') + Fissue.escapeHtml(text) + "</span>" : "";
  }

  var pwBtn = document.getElementById("pw-save");
  if (pwBtn) pwBtn.addEventListener("click", function () {
    setText("pw-result", "提交中…");
    authCall("/password", {
      body: {current_password: val("pw-current"), new_password: val("pw-new")}
    }).then(function () {
      setText("pw-result", "已修改；其它会话已被注销。", true);
      document.getElementById("pw-current").value = "";
      document.getElementById("pw-new").value = "";
      dirty = false;    // 口令改动已经落库，别让离页拦截以为还有未保存的配置
    }).catch(function (e) { setText("pw-result", e.message); });
  });

  var enrollBtn = document.getElementById("tfa-enroll");
  if (enrollBtn) enrollBtn.addEventListener("click", function () {
    setText("tfa-result", "生成中…");
    authCall("/2fa/enroll").then(function (r) {
      showPending(r);
      setText("tfa-result", "密钥已生成（尚未生效）：扫码或手抄进认证器，再输一次验证码确认。", true);
    }).catch(function (e) { setText("tfa-result", e.message); });
  });

  /** 把「待确认的密钥」画出来（密钥 / otpauth / 二维码）。
   *
   * 二维码是服务端算好的内联 SVG 字符串，直接塞进容器——它只由黑白模块坐标组成，
   * 不含任何用户输入，所以这里用 innerHTML 没有注入面。
   * `qr_svg` 为空（编码失败）时只少一张图，手抄那条路照旧可用。
   */
  function showPending(r) {
    if (!r || !r.pending && !r.secret) return;
    document.getElementById("tfa-secret").textContent = r.secret || "";
    document.getElementById("tfa-uri").textContent = r.otpauth_uri || "";
    var box = document.getElementById("tfa-qr");
    if (box) box.innerHTML = r.qr_svg || "";
    document.getElementById("tfa-enroll-box").hidden = false;
  }

  // 刷新页面后把「已生成、还没确认」的那把找回来：否则用户扫完码一刷新，
  // 二维码就没了，只能重新生成一把（旧的那把白生成了）。
  if (document.getElementById("tfa-qr")) {
    authCall("/2fa/pending", {method: "GET"}).then(function (r) {
      if (r && r.pending) showPending(r);
    }).catch(function () { /* 拿不到就当没有：不打扰用户，也不影响手抄那条路 */ });
  }

  var activateBtn = document.getElementById("tfa-activate");
  if (activateBtn) activateBtn.addEventListener("click", function () {
    setText("tfa-result", "校验中…");
    authCall("/2fa/activate", {body: {code: val("tfa-activate-code")}}).then(function (r) {
      showRecoveryCodes(r.recovery_codes);
      setText("tfa-result", "两步验证已开启。", true);
      setTimeout(function () { location.reload(); }, 4000);
    }).catch(function (e) { setText("tfa-result", e.message); });
  });

  var disableBtn = document.getElementById("tfa-disable");
  if (disableBtn) disableBtn.addEventListener("click", function () {
    setText("tfa-result", "关闭中…");
    authCall("/2fa/disable", {
      body: {password: val("tfa-off-password"), code: val("tfa-off-code")}
    }).then(function () {
      setText("tfa-result", "两步验证已关闭。", true);
      setTimeout(function () { location.reload(); }, 1200);
    }).catch(function (e) { setText("tfa-result", e.message); });
  });

  var recoveryBtn = document.getElementById("tfa-recovery");
  if (recoveryBtn) recoveryBtn.addEventListener("click", function () {
    setText("tfa-result", "生成中…");
    authCall("/2fa/recovery", {body: {current_password: val("tfa-recovery-password")}})
      .then(function (r) {
        showRecoveryCodes(r.recovery_codes);
        setText("tfa-result", "已重新生成，旧码全部作废。", true);
      })
      .catch(function (e) { setText("tfa-result", e.message); });
  });

  function showRecoveryCodes(codes) {
    if (!codes || !codes.length) return;
    document.getElementById("tfa-code-list").textContent = codes.join("\n");
    document.getElementById("tfa-codes").hidden = false;
  }

  function testLlm() {
    var out = document.getElementById("llm-result");
    out.innerHTML = '<span class="spinner"></span> 请求中…';
    Fissue.api("/setup/test-llm", {
      method: "POST",
      body: {base_url: val("env-FISSUE_LLM_BASE_URL") || null,
             model: val("env-FISSUE_LLM_MODEL") || null,
             api_key: val("env-FISSUE_LLM_API_KEY") || null}
    }).then(function (r) {
      out.innerHTML = r.ok
        ? '<span class="ok">连通</span>：模型 ' + Fissue.escapeHtml(r.model) + " · " + r.seconds + "s · tokens " + r.tokens.completion
        : '<span class="bad">失败：' + Fissue.escapeHtml(r.error) + "</span>";
    }).catch(function (e) { out.innerHTML = '<span class="bad">' + Fissue.escapeHtml(e.message) + "</span>"; });
  }

  function testDb() {
    var out = document.getElementById("db-result");
    out.innerHTML = '<span class="spinner"></span> 连接中…';
    // 留空 = 测「当前生效的那个库」（后端会在 url 为空时回落），与其它密钥字段同语义。
    Fissue.api("/setup/test-db", {method: "POST", body: {url: val("env-FISSUE_DATABASE_URL") || null}})
      .then(function (r) {
        out.innerHTML = r.ok
          ? '<span class="ok">连接正常</span>（' + Fissue.escapeHtml(r.backend) + " · " + r.seconds + "s）"
          : '<span class="bad">失败：' + Fissue.escapeHtml(r.error || "未知") + "</span>";
      })
      .catch(function (e) { out.innerHTML = '<span class="bad">' + Fissue.escapeHtml(e.message) + "</span>"; });
  }

  function testPlatform(plat) {
    var key = "FISSUE_" + plat.toUpperCase() + "_TOKEN";
    var out = document.getElementById("plat-" + plat);
    out.innerHTML = '<span class="spinner"></span> 校验中…';
    Fissue.api("/setup/test-platform", {method: "POST", body: {platform: plat, token: val("env-" + key)}})
      .then(function (r) {
        out.innerHTML = r.ok
          ? '<span class="ok">' + Fissue.escapeHtml(r.login) + "</span>（" + r.seconds + "s）"
          : '<span class="bad">' + Fissue.escapeHtml(r.error || "失败") + "</span>";
      })
      .catch(function (e) { out.innerHTML = '<span class="bad">' + Fissue.escapeHtml(e.message) + "</span>"; });
  }

  // 按钮的值走 data-*，不写进 onclick：值一旦含引号，插值写法会把属性截断（按钮静默失效）。
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("button[data-act=test-platform]") : null;
    if (btn) testPlatform(btn.dataset.platform);
  });

  // 页面上的按钮走 inline onclick（saveSettings / testLlm / testDb …），挂到 window 上，
  // 否则模块作用域里的函数在 inline 事件处理器里找不到（那属于全局作用域）。
  window.saveSettings = saveSettings;
  window.testLlm = testLlm;
  window.testDb = testDb;
  window.fail = fail;
})();
