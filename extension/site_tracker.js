(() => {
  if (window.__tabbiSiteTrackerInstalled) {
    return;
  }
  window.__tabbiSiteTrackerInstalled = true;

  function sendPresence() {
    if (location.protocol !== "http:" && location.protocol !== "https:") {
      return;
    }

    chrome.runtime.sendMessage({
      type: "sitePresence",
      host: location.hostname,
      href: location.href,
      visible: document.visibilityState === "visible"
    }, () => {
      void chrome.runtime.lastError;
    });
  }

  sendPresence();
  window.addEventListener("focus", sendPresence, { passive: true });
  window.addEventListener("pageshow", sendPresence, { passive: true });
  document.addEventListener("visibilitychange", sendPresence, { passive: true });
  setInterval(sendPresence, 15000);
})();
