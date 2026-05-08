/* install.js — OS detection, copy-to-clipboard, and integrate tabs */
(function () {
  "use strict";

  var COMMANDS = {
    mac:     "pip install tourniquet-dev && tourniquet",
    linux:   "pip install tourniquet-dev && tourniquet",
    windows: "pip install tourniquet-dev; tourniquet",
    mobile:  null
  };

  /* ── OS tabs ─────────────────────────────────────────────────────────── */

  function detectOS() {
    var ua = navigator.userAgent || "";
    var pl = (navigator.platform || "").toLowerCase();
    if (/iphone|ipad|ipod|android/i.test(ua)) return "mobile";
    if (/win/i.test(pl)) return "windows";
    if (/mac/i.test(pl)) return "mac";
    if (/linux/i.test(pl)) return "linux";
    return "linux";
  }

  function setActiveTab(os) {
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.os === os);
      btn.setAttribute("aria-selected", btn.dataset.os === os ? "true" : "false");
    });
    document.querySelectorAll(".os-panel").forEach(function (panel) {
      panel.hidden = panel.dataset.os !== os;
    });
    var block = document.getElementById("install-block");
    if (block) block.dataset.os = os;
  }

  function showMobile() {
    var mobileNote = document.getElementById("mobile-note");
    if (mobileNote) mobileNote.hidden = false;
    var mainCode = document.getElementById("primary-install-code");
    if (mainCode) mainCode.hidden = true;
  }

  /* ── Integrate (agent) tabs ───────────────────────────────────────────── */

  function setActiveIntegrateTab(tool) {
    document.querySelectorAll(".integrate-tab-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tool === tool);
      btn.setAttribute("aria-selected", btn.dataset.tool === tool ? "true" : "false");
    });
    document.querySelectorAll(".integrate-snippet").forEach(function (pane) {
      pane.hidden = pane.dataset.tool !== tool;
    });
  }

  /* ── Copy button ──────────────────────────────────────────────────────── */

  function bindCopyButton(btn) {
    btn.addEventListener("click", function () {
      var targetId = btn.dataset.copyTarget;
      var codeEl = targetId ? document.getElementById(targetId) : btn.closest(".integrate-panel, .install-block");
      if (!codeEl) return;

      /* For integrate panels, find the <pre> inside */
      var preEl = codeEl.tagName === "PRE" ? codeEl : codeEl.querySelector("pre");
      var text = preEl
        ? (preEl.textContent || preEl.innerText || "").trim()
        : (codeEl.textContent || codeEl.innerText || "").trim();
      if (!text) return;

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          flashCopied(btn);
        }).catch(function () {
          fallbackCopy(text, btn);
        });
      } else {
        fallbackCopy(text, btn);
      }
    });
  }

  function fallbackCopy(text, btn) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand("copy"); flashCopied(btn); } catch (e) {}
    document.body.removeChild(ta);
  }

  function flashCopied(btn) {
    var orig = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(function () {
      btn.textContent = orig;
      btn.classList.remove("copied");
    }, 2000);
  }

  /* ── Reduced-motion: disable video autoplay ───────────────────────────── */

  function respectReducedMotion() {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.querySelectorAll("video[autoplay]").forEach(function (v) {
        v.removeAttribute("autoplay");
        v.pause();
      });
    }
  }

  /* ── Init ─────────────────────────────────────────────────────────────── */

  function init() {
    var os = detectOS();
    document.documentElement.dataset.os = os;
    setActiveTab(os);
    if (os === "mobile") showMobile();

    /* OS tab switching */
    document.querySelectorAll(".tab-btn[data-os]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveTab(btn.dataset.os);
      });
    });

    /* Integrate tab switching — default to "claude-code" */
    var integrateTabs = document.querySelectorAll(".integrate-tab-btn[data-tool]");
    if (integrateTabs.length) {
      setActiveIntegrateTab("claude-code");
      integrateTabs.forEach(function (btn) {
        btn.addEventListener("click", function () {
          setActiveIntegrateTab(btn.dataset.tool);
        });
      });
    }

    /* Bind all copy buttons */
    document.querySelectorAll(".copy-btn").forEach(bindCopyButton);

    respectReducedMotion();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
