// Browser-side Whisper (Transformers.js on WebGPU) in a module worker.
// Contract with the page:
//   page -> worker: {type:'init', model, dtype}          load model (once)
//                   {type:'audio', vid, t0, audio:Float32Array(16 kHz mono)}
//   worker -> page: {type:'status', state:'loading'|'ready'|'error', message}
//                   {type:'result', vid, t0, segments:[{start,end,text}]}
//                   {type:'fail', vid, t0, audio, error}   audio handed back for server fallback
import { pipeline } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.2/dist/transformers.min.js";

let asr = null;
let initError = null;
let initPromise = null;
const queue = [];
let pumping = false;

function status(state, message = "") {
  self.postMessage({ type: "status", state, message });
}

function init(model, dtype) {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    try {
      if (!self.navigator || !self.navigator.gpu) throw new Error("WebGPU not available in this browser");
      status("loading", "downloading/initialising " + model);
      asr = await pipeline("automatic-speech-recognition", model, {
        device: "webgpu",
        dtype: dtype || { encoder_model: "fp32", decoder_model_merged: "q4" },
        progress_callback: (p) => {
          if (p && p.status === "progress" && typeof p.progress === "number") {
            status("loading", `${p.file || "model"} ${Math.round(p.progress)}%`);
          }
        },
      });
      status("ready", model);
    } catch (e) {
      initError = String((e && e.message) || e);
      status("error", initError);
    }
  })();
  return initPromise;
}

function giveBack(job, error) {
  self.postMessage({ type: "fail", vid: job.vid, t0: job.t0, audio: job.audio, error }, [job.audio.buffer]);
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    while (queue.length) {
      const job = queue.shift();
      if (initPromise) await initPromise;
      if (initError || !asr) { giveBack(job, initError || "model not loaded"); continue; }
      try {
        const out = await asr(job.audio, { return_timestamps: true, chunk_length_s: 30, language: "en" });
        const segs = [];
        if (out && Array.isArray(out.chunks) && out.chunks.length) {
          for (const c of out.chunks) {
            const [s, e] = c.timestamp || [0, null];
            segs.push({ start: job.t0 + (s || 0), end: job.t0 + (e == null ? (s || 0) : e), text: c.text || "" });
          }
        } else if (out && out.text) {
          segs.push({ start: job.t0, end: job.t0, text: out.text });
        }
        self.postMessage({ type: "result", vid: job.vid, t0: job.t0, segments: segs });
      } catch (e) {
        giveBack(job, String((e && e.message) || e));
      }
    }
  } finally {
    pumping = false;
  }
}

self.onmessage = (ev) => {
  const m = ev.data;
  if (m.type === "init") { init(m.model, m.dtype); }
  else if (m.type === "audio") { queue.push(m); pump(); }
};
