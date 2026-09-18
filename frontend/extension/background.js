/**
 * background.js
 * Captures ONLY title, domain, timestamp — never page content, per
 * API_CONTRACT.md's capture spec. Fires on every tab switch and every
 * completed navigation, then relays each event to any open tab matching
 * the app's URL (via chrome.tabs.sendMessage) so content-script.js can
 * hand it to the running page in real time. No server involved.
 */

// TODO: update to your deployed app's origin when you're not testing
// against the Vite dev server anymore.
const APP_URL_PATTERN = "http://localhost:5173/*";

function domainFromUrl(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

async function relayToAppTabs(event) {
  const tabs = await chrome.tabs.query({ url: APP_URL_PATTERN });
  for (const tab of tabs) {
    chrome.tabs.sendMessage(tab.id, { type: "RETHREAD_TAB_EVENT", payload: event }).catch(() => {
      // App tab not listening yet (e.g. still loading) — safe to ignore.
    });
  }
}

function sendEvent(tab) {
  if (!tab || !tab.url || !tab.title) return;

  const domain = domainFromUrl(tab.url);
  if (!domain || tab.url.startsWith("chrome://") || tab.url.startsWith("about:")) return;

  relayToAppTabs({
    ts: new Date().toISOString(),
    title: tab.title.slice(0, 160),
    domain,
  });
}

// Person switches to a different existing tab.
chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId, sendEvent);
});

// A tab finishes loading a new page (typed URL, followed a link, etc.).
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete") sendEvent(tab);
});
