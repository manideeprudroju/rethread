/**
 * content-script.js
 * Bridges the extension's isolated world into the actual page. Listens
 * for events relayed from background.js, then re-broadcasts them via
 * window.postMessage so sessionStore.js (running as normal page code)
 * can pick them up. This is the only way an extension can hand data to
 * a page's own JavaScript.
 */

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "RETHREAD_TAB_EVENT") {
    window.postMessage({ source: "rethread-extension", payload: message.payload }, window.location.origin);
  }
});
