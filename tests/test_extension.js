// Run: node tests/test_extension.js   (no dependencies; uses a minimal fake DOM)
const assert = require("assert");
const { readPage, mergeFrames, describeError } = require("../extension/background.js");

const T = (s) => ({ nodeType: 3, nodeValue: s });
const E = (tag, kids = [], p = {}) => Object.assign({ nodeType: 1, tagName: tag, childNodes: kids, id: "", hidden: false, _display: "block", _vis: "visible", attrs: {}, getAttribute(k) { return this.attrs[k] || null; } }, p);

function run(body, { selection = "", top = true } = {}) {
  const win = {};
  global.window = win;
  if (top) win.top = win;
  global.getComputedStyle = (el) => ({ display: el._display, visibility: el._vis });
  win.getSelection = () => selection;
  global.location = { href: "https://course.example/quiz" };
  global.document = { title: "Quiz", body, documentElement: body };
  return readPage();
}

// 1. visible text, radios, hidden answer key, script text, shadow DOM, select
const shadowHost = E("DIV", [], { shadowRoot: { nodeType: 11, childNodes: [E("P", [T("Shadow question text")])] } });
const body = E("BODY", [
  E("H2", [T("Question 1: What makes ATP?")]),
  E("LABEL", [E("INPUT", [], { type: "radio", checked: true }), T("Mitochondria")]),
  E("LABEL", [E("INPUT", [], { type: "radio", checked: false }), T("Nucleus")]),
  E("DIV", [T("SECRET ANSWER KEY")], { _display: "none" }),
  E("DIV", [T("aria hidden junk")], { attrs: { "aria-hidden": "true" } }),
  E("DIV", [T("hidden attr")], { hidden: true }),
  E("SCRIPT", [T("var leak = 1;")]),
  E("DIV", [T("invisible")], { _vis: "hidden" }),
  E("SELECT", [], { selectedOptions: [{ textContent: "Two" }], options: [{ textContent: "One" }, { textContent: "Two" }] }),
  E("INPUT", [], { type: "text", value: "my typed answer" }),
  E("DIV", [T("Sidekick panel text")], { id: "sidekick-answer-panel" }),
  shadowHost,
]);
let r = run(body);
assert.strictEqual(r.selected, false);
for (const need of ["Question 1: What makes ATP?", "[x] Mitochondria", "[ ] Nucleus", "Shadow question text", "[select: Two of: One | Two]", "[input: my typed answer]"])
  assert(r.text.includes(need), "missing: " + need + "\n" + r.text);
for (const bad of ["SECRET", "aria hidden", "hidden attr", "var leak", "invisible", "Sidekick panel"])
  assert(!r.text.includes(bad), "leaked: " + bad);

// 2. selection wins
r = run(body, { selection: "  just this  " });
assert.deepStrictEqual([r.text, r.selected], ["just this", true]);

// 3. sub-frame is labelled; text limit enforced
const parent = { x: 1 };
r = run(E("BODY", [E("P", [T("x".repeat(50000))])]), { top: false });
assert(r.text.length <= 40000 + 10 && r.frame !== "top");

// 4. mergeFrames
const m = mergeFrames([
  { text: "TOP text", frame: "top", url: "u1", title: "t1", selected: false },
  { text: "iframe quiz", frame: "https://quiz.example/f", url: "u2", title: "t2", selected: false },
  { text: "iframe quiz", frame: "https://quiz.example/g", url: "u3", title: "t3", selected: false },
  { text: "  ", frame: "x" }, null, { text: 5 },
]);
assert.strictEqual(m.text, "TOP text\n\n[embedded frame]\niframe quiz");
assert.strictEqual(m.url, "u1");
assert.deepStrictEqual(mergeFrames([{ text: "a", frame: "top", selected: false, url: "u" }, { text: "picked", frame: "f", selected: true, url: "v", title: "z" }]).text, "picked");
assert.strictEqual(mergeFrames([]).text, "");

// 5. errors
assert(/Is .\/run.sh running/.test(describeError(new TypeError("Failed to fetch"), "http://127.0.0.1:8765")));
assert.strictEqual(describeError(new Error("HTTP 502"), "s"), "HTTP 502");

console.log("extension tests: all passed");
