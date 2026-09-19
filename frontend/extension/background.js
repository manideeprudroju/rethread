/**
 * background.js
 * Captures ONLY title, domain, timestamp — never page content, per
 * API_CONTRACT.md's capture spec. Fires when the tab they are looking at
 * changes, then relays each event to any open tab of the app (via
 * chrome.tabs.sendMessage) so content-script.js can hand it to the running
 * page in real time. No server involved.
 */

// The app's own pages come from manifest.json, so there is ONE place to
// edit when the dashboard is deployed: add its URL to content_scripts.matches
// (and host_permissions) there.
const APP_PATTERNS = chrome.runtime.getManifest().content_scripts[0].matches;
const APP_PREFIXES = APP_PATTERNS.map((p) => p.replace(/\*$/, ""));

// Browser and extension pages are never the user's work.
const SKIP_PREFIXES = [
  "chrome:", "chrome-extension:", "chrome-search:", "chrome-untrusted:",
  "edge:", "about:", "devtools:", "view-source:",
];

// The last event sent, to drop the same tab reported twice in a row.
let lastKey = null;

function domainFromUrl(url) {
  // Local files have no hostname, but they are often the work itself (a PDF
  // someone is studying from). They count, under a generic label: the title
  // is the file name, and the path never leaves the extension.
  if (url.startsWith("file:")) return "local file";
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

async function relayToAppTabs(event) {
  const tabs = await chrome.tabs.query({ url: APP_PATTERNS });
  for (const tab of tabs) {
    chrome.tabs.sendMessage(tab.id, { type: "RETHREAD_TAB_EVENT", payload: event }).catch(() => {
      // App tab not listening yet (e.g. still loading) — safe to ignore.
    });
  }
}

function sendEvent(tab) {
  if (!tab || !tab.url || !tab.title) return;
  const url = tab.url;

  if (SKIP_PREFIXES.some((p) => url.startsWith(p))) return;

  // The dashboard is not activity. Recording it put a "Rethread" tab into
  // every trail and every reconstruction, and made each look at the
  // re-entry panel count as a new tab.
  if (APP_PREFIXES.some((p) => url.startsWith(p))) return;

  const domain = domainFromUrl(url);
  if (!domain) return;

  const title = tab.title.slice(0, 160);

  // One visit, reported twice (the switch, then the page finishing its
  // load), would split that tab's time in two and inflate the churn
  // detector's count of returns to the same page.
  const key = `${domain}|${title}`;
  if (key === lastKey) return;
  lastKey = key;

  relayToAppTabs({
    ts: new Date().toISOString(),
    title,
    domain,
  });
}

// Person switches to a different existing tab.
chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError) return; // closed before we looked
    // Still loading: onUpdated reports it with its real title when done.
    if (tab.status === "loading") return;
    sendEvent(tab);
  });
});

// A page finishes loading. Only counts if it's the tab they are looking at:
// links opened in background tabs (Ctrl+click) used to be recorded as visits.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.active) sendEvent(tab);
});

// Moving to another Chrome window changes what they're looking at without
// any tab being activated.
chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return; // left Chrome
  chrome.tabs.query({ active: true, windowId }, (tabs) => {
    if (chrome.runtime.lastError) return;
    sendEvent(tabs[0]);
  });
});
