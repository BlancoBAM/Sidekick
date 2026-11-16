// Run: node tests/test_worker.js  - exercises whisper-worker.js against a mocked Transformers.js.
const fs = require("fs"), vm = require("vm"), assert = require("assert"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "../static/whisper-worker.js"), "utf8")
  .replace(/^import .*$/m, "const pipeline = __pipeline;");

function boot({ gpu = true, pipelineImpl }) {
  const out = [];
  const self = { navigator: gpu ? { gpu: {} } : {}, postMessage: (m) => out.push(m) };
  vm.runInNewContext(src, { self, __pipeline: pipelineImpl, console, Array, Promise, String });
  return { self, out };
}
const audio = (n = 4) => new Float32Array(n);
const tick = () => new Promise((r) => setTimeout(r, 20));

(async () => {
  // 1. success: chunks get offset by t0; jobs processed in order
  let w = boot({ pipelineImpl: async (task, model, opts) => {
    assert.strictEqual(task, "automatic-speech-recognition"); assert.strictEqual(opts.device, "webgpu");
    assert.strictEqual(JSON.stringify(opts.dtype), JSON.stringify({ encoder_model: "fp32", decoder_model_merged: "q4" }));
    return async (a, o) => ({ text: "x", chunks: [{ timestamp: [0, 2], text: " hello" }, { timestamp: [2, null], text: " world" }] });
  } });
  w.self.onmessage({ data: { type: "init", model: "m" } });
  w.self.onmessage({ data: { type: "audio", vid: 3, t0: 30, audio: audio() } });
  w.self.onmessage({ data: { type: "audio", vid: 3, t0: 40, audio: audio() } });
  await tick(); await tick();
  const res = w.out.filter((m) => m.type === "result");
  assert.strictEqual(res.length, 2);
  assert.strictEqual(JSON.stringify(res[0].segments.map((s) => [s.start, s.text])), JSON.stringify([[30, " hello"], [32, " world"]]));
  assert.strictEqual(res[1].t0, 40);
  assert(w.out.some((m) => m.type === "status" && m.state === "ready"));

  // 2. no WebGPU: error status, audio handed back with original t0/vid
  w = boot({ gpu: false, pipelineImpl: async () => { throw new Error("should not be called"); } });
  w.self.onmessage({ data: { type: "init", model: "m" } });
  w.self.onmessage({ data: { type: "audio", vid: 1, t0: 7, audio: audio(8) } });
  await tick(); await tick();
  assert(w.out.some((m) => m.type === "status" && m.state === "error" && /WebGPU/.test(m.message)));
  const f = w.out.find((m) => m.type === "fail");
  assert.strictEqual([f.vid, f.t0, f.audio.length].join(), "1,7,8");

  // 3. audio arriving BEFORE init still waits for the model, and is not lost
  w = boot({ pipelineImpl: async () => async () => ({ text: "late", chunks: [] }) });
  w.self.onmessage({ data: { type: "audio", vid: 1, t0: 0, audio: audio() } });
  w.self.onmessage({ data: { type: "init", model: "m" } });
  await tick(); await tick();
  // (pump ran with no initPromise yet -> must have been handed back, never dropped)
  assert.strictEqual(w.out.filter((m) => m.type === "result" || m.type === "fail").length, 1);

  // 4. per-chunk runtime failure hands audio back and the worker keeps going
  let n = 0;
  w = boot({ pipelineImpl: async () => async () => { if (n++ === 0) throw new Error("GPU lost"); return { text: "ok", chunks: [] }; } });
  w.self.onmessage({ data: { type: "init", model: "m" } });
  w.self.onmessage({ data: { type: "audio", vid: 1, t0: 0, audio: audio() } });
  w.self.onmessage({ data: { type: "audio", vid: 1, t0: 10, audio: audio() } });
  await tick(); await tick();
  assert.strictEqual(w.out.filter((m) => m.type === "fail").length, 1);
  const ok = w.out.find((m) => m.type === "result");
  assert.strictEqual(JSON.stringify(ok.segments), JSON.stringify([{ start: 10, end: 10, text: "ok" }]));  // text-only fallback path

  console.log("worker tests: all passed");
})().catch((e) => { console.error(e); process.exit(1); });
