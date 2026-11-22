// The login page for other devices (Settings → Web Server). In a file of its own: the pages
// only run scripts from files (Content-Security-Policy).
(() => {
  "use strict";
  const { $, t } = window.LT;
  const next = new URLSearchParams(location.search).get("next") || "/admin";
  // Only back to a page of the admin itself (the server checks this as well).
  const ours = next.startsWith("/admin") || next === "/qr" || next.startsWith("/qr?");
  const target = ours && !next.startsWith("/admin/login") && !next.includes("//") && !next.includes("\\") ? next : "/admin";

  function showError(text) {
    $("loginError").textContent = text;
    $("loginError").hidden = !text;
  }

  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");
    $("loginBtn").disabled = true;
    try {
      const res = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("password").value }),
      });
      if (res.ok) {
        location.replace(target);
        return;
      }
      if (res.status === 429) {
        const minutes = Math.max(1, Math.ceil(Number(res.headers.get("Retry-After") || 900) / 60));
        showError(t("loginBlocked").replace("{n}", minutes));
      } else if (res.status === 401) {
        showError(t("loginWrong"));
        $("password").select();
      } else {
        showError(t("loginOff"));
      }
    } catch {
      showError(t("loginNoConnection"));
    } finally {
      $("loginBtn").disabled = false;
    }
  });
  window.LT.applyTexts();
})();
