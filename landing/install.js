/* install.js — OS detection and copy-to-clipboard for install block */
(function () {
  "use strict";

  var COMMANDS = {
    mac:     "pip install tourniquet && tourniquet",
    linux:   "pip install tourniquet && tourniquet",
    windows: "pip install tourniquet; tourniquet",
    mobile:  null
  };

  function detectOS() {
    var ua = navigator.userAgent || "";
    var pl = (navigator.platform || "").toLowerCase();
    if (/iphone|ipad|ipod|android/i.test(ua)) return "mobile";
    if (/win/i.test(pl)) return "windows";
    if (/mac/i.test(pl)) return "mac";
    if (/linux/i.test(pl)) return "linux";
    return "linux"; // sensible default
  }

  function setActiveTab(os) {
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.os === os);
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

  function init() {
    var os = detectOS();

    // Mark body with OS for any CSS hooks
    document.documentElement.dataset.os = os;

    setActiveTab(os);

    if (os === "mobile") showMobile();

    // Tab switching
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveTab(btn.dataset.os);
      });
    });

    // Copy buttons
    document.querySelectorAll(".copy-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var targetId = btn.dataset.copyTarget;
        var codeEl = document.getElementById(targetId);
        if (!codeEl) return;
        var text = (codeEl.textContent || codeEl.innerText || "").trim();
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
