// Sidekick MV3 service worker. Click the toolbar button (or Alt+Shift+S):
//   1. read text from every frame of the active tab (injected function)
//   2. POST it to the local Sidekick server /api/ask
//   3. draw the answer in a panel on the page (injected function)

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const MIN_TEXT = 80; // below this the page is probably a canvas/image quiz

// ---- injected into pages: must be fully self-contained -------------------
function readPage() {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "SVG", "CANVAS", "IFRAME"]);
  const BLOCK = new Set(["P", "DIV", "LI", "UL", "OL", "TR", "TABLE", "H1", "H2", "H3", "H4", "H5", "H6", "SECTION", "ARTICLE", "FORM", "FIELDSET", "LEGEND", "LABEL", "BR", "DT", "DD", "BLOCKQUOTE", "PRE", "OPTION"]);
  const out = [];
  let size = 0;
  const LIMIT = 40000;

  const push = (s) => { if (size < LIMIT) { out.push(s); size += s.length; } };
  const visible = (el) => {
    try {
      if (el.hidden || el.getAttribute("aria-hidden") === "true") return false;
      const cs = getComputedStyle(el);
      return cs.display !== "none" && cs.visibility !== "hidden" && cs.visibility !== "collapse";
    } catch (e) { return true; }
  };
  function walk(node) {
    if (size >= LIMIT) return;
    if (node.nodeType === 3) { const t = node.nodeValue.replace(/\s+/g, " "); if (t.trim()) push(t); return; }
    if (node.nodeType !== 1 && node.nodeType !== 11) return;
    if (node.nodeType === 1) {
      const tag = node.tagName.toUpperCase();
      if (SKIP.has(tag) || node.id === "sidekick-answer-panel" || !visible(node)) return;
      if (tag === "INPUT") {
        const type = (node.type || "text").toLowerCase();
        if (type === "checkbox" || type === "radio") push(node.checked ? " [x] " : " [ ] ");
        else if (type === "text" || type === "number" || type === "search") push(" [input: " + (node.value || "") + "] ");
        return;
      }
      if (tag === "TEXTAREA") { push(" [input: " + (node.value || "") + "] "); return; }
      if (tag === "SELECT") {
        const sel = node.selectedOptions && node.selectedOptions[0];
        push(" [select: " + (sel ? sel.textContent.trim() : "") + " of: " + Array.from(node.options || []).map((o) => o.textContent.trim()).join(" | ") + "] ");
        return;
      }
      const block = BLOCK.has(tag);
      if (block) push("\n");
      if (node.shadowRoot) walk(node.shadowRoot);
      for (const c of node.childNodes) walk(c);
      if (block) push("\n");
      return;
    }
    for (const c of node.childNodes) walk(c);  // shadow root / fragment
  }

  let selected = "";
  try { selected = String(window.getSelection ? window.getSelection() : "").trim(); } catch (e) {}
  if (selected) return { text: selected, selected: true, url: location.href, title: document.title, frame: window === window.top ? "top" : location.href };
  walk(document.body || document.documentElement);
  const text = out.join("").replace(/[ \t]+/g, " ").replace(/ ?\n ?/g, "\n").replace(/\n{3,}/g, "\n\n").trim().slice(0, LIMIT);
  return { text, selected: false, url: location.href, title: document.title, frame: window === window.top ? "top" : location.href };
}

function showPanel(payload) {
  const ID = "sidekick-answer-panel";
  const old = document.getElementById(ID);
  if (old) old.remove();
  const host = document.createElement("div");
  host.id = ID;
  host.style.cssText = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;";
  const root = host.attachShadow({ mode: "open" });
  const box = document.createElement("div");
  box.style.cssText = "font:14px/1.45 system-ui,sans-serif;background:#111;color:#f2f2f2;border:1px solid #555;border-radius:8px;padding:10px 12px;max-width:420px;max-height:60vh;overflow:auto;box-shadow:0 4px 18px rgba(0,0,0,.5);white-space:pre-wrap;";
  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;justify-content:space-between;gap:12px;color:#9a9a9a;font-size:12px;margin-bottom:6px;";
  const label = document.createElement("span");
  label.textContent = payload.label || "Sidekick";
  const x = document.createElement("button");
  x.textContent = "\u2715";
  x.style.cssText = "all:unset;cursor:pointer;color:#ccc;padding:0 4px;";
  const body = document.createElement("div");
  body.textContent = payload.text;          // textContent only: never interpret model output as HTML
  if (payload.error) body.style.color = "#ff8a80";
  const close = () => { host.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  x.onclick = close;
  document.addEventListener("keydown", onKey, true);
  bar.append(label, x);
  box.append(bar, body);
  root.append(box);
  document.documentElement.appendChild(host);
}

// ---- pure helpers (unit-tested in tests/test_extension.mjs) --------------
function mergeFrames(results) {
  // results: array of {text, selected, url, title, frame}. A selection anywhere wins.
  const frames = results.filter((r) => r && typeof r.text === "string" && r.text.trim());
  const sel = frames.find((f) => f.selected);
  if (sel) return { text: sel.text.trim(), selected: true, url: sel.url, title: sel.title };
  const top = frames.find((f) => f.frame === "top");
  const parts = [];
  const seen = new Set();
  for (const f of frames) {
    const t = f.text.trim();
    if (seen.has(t)) continue;
    seen.add(t);
    parts.push(f.frame === "top" ? t : "[embedded frame]\n" + t);
  }
  return { text: parts.join("\n\n").slice(0, 60000), selected: false, url: (top || frames[0] || {}).url || "", title: (top || frames[0] || {}).title || "" };
}

function describeError(e, server) {
  const m = String((e && e.message) || e);
  if (/Failed to fetch|NetworkError|load failed/i.test(m)) return `Cannot reach Sidekick at ${server}. Is ./run.sh running?`;
  return m;
}

// ---- main ----------------------------------------------------------------
async function getServer() {
  try {
    const v = await chrome.storage.sync.get({ server: DEFAULT_SERVER });
    return v.server || DEFAULT_SERVER;
  } catch (e) { return DEFAULT_SERVER; }
}

async function run(tab) {
  const server = await getServer();
  const show = (text, error, label) => chrome.scripting.executeScript({ target: { tabId: tab.id }, func: showPanel, args: [{ text, error, label }] }).catch(() => {});
  try {
    await show("Reading page and asking...", false, "Sidekick");
    const res = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: readPage });
    const page = mergeFrames(res.map((r) => r.result));
    let image = null;
    if (page.text.length < MIN_TEXT) {
      try { image = (await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 })).split(",")[1]; } catch (e) {}
    }
    if (!page.text && !image) { await show("No readable text on this page, and no screenshot could be taken.", true); return; }
    const r = await fetch(server + "/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: page.text, page_url: page.url, image, video: "current" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "server error " + r.status);
    await show(j.answer, false, `Sidekick · ${j.model}` + (page.selected ? " · selection" : ""));
  } catch (e) {
    await show(describeError(e, server), true);
  }
}

if (typeof chrome !== "undefined" && chrome.action) {
  chrome.action.onClicked.addListener((tab) => { if (tab && tab.id != null) run(tab); });
}
if (typeof module !== "undefined") module.exports = { readPage, mergeFrames, describeError, showPanel };
