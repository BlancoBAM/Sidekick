const u = document.getElementById("u");
chrome.storage.sync.get({ server: "http://127.0.0.1:8765" }, (v) => { u.value = v.server; });
document.getElementById("s").onclick = () => {
  const val = u.value.trim().replace(/\/+$/, "");
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(val)) { document.getElementById("m").textContent = "Must be http://127.0.0.1:PORT or http://localhost:PORT"; return; }
  chrome.storage.sync.set({ server: val }, () => { document.getElementById("m").textContent = "saved"; });
};
