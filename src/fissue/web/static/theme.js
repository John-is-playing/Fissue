/* 主题切换（三态：跟随系统 → 浅色 → 深色）。
 *
 * 为什么需要 JS：`prefers-color-scheme` 只能表达"系统的选择"，表达不了"这个人
 * 在这个站点上的选择"。默认跟随系统，点一次固定成浅色，再点固定成深色。
 *
 * 首屏不靠这个文件：head 里有一段内联脚本先定 `data-theme`，否则深色用户
 * 会先看到白底闪一下。这里只负责**切换**与**跟随系统时实时响应**。
 */
(function () {
  "use strict";

  var KEY = "fissue.ui.theme";
  var ORDER = ["auto", "light", "dark"];
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function currentPref() {
    var pref = root.getAttribute("data-theme-pref");
    return ORDER.indexOf(pref) >= 0 ? pref : "auto";
  }

  function resolve(pref) {
    if (pref === "light" || pref === "dark") return pref;
    return media && media.matches ? "dark" : "light";
  }

  function apply(pref) {
    root.setAttribute("data-theme-pref", pref);
    root.setAttribute("data-theme", resolve(pref));
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.setAttribute("data-pref", pref);
      btn.setAttribute(
        "title",
        pref === "auto" ? "当前：跟随系统（点击切成浅色）"
          : pref === "light" ? "当前：浅色（点击切成深色）"
          : "当前：深色（点击跟随系统）"
      );
    }
  }

  function save(pref) {
    try {
      localStorage.setItem(KEY, pref);
    } catch (e) {
      /* 隐私模式下存不了：本次会话仍生效，只是下次回来会回默认值 */
    }
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest ? event.target.closest("#theme-toggle") : null;
    if (!btn) return;
    var next = ORDER[(ORDER.indexOf(currentPref()) + 1) % ORDER.length];
    apply(next);
    save(next);
  });

  if (media) {
    var onSystemChange = function () {
      // 只有"跟随系统"时才跟着变；用户已经明确选了浅/深就别覆盖他。
      if (currentPref() === "auto") apply("auto");
    };
    if (media.addEventListener) media.addEventListener("change", onSystemChange);
    else if (media.addListener) media.addListener(onSystemChange);
  }

  apply(currentPref());
})();