"use strict";
let stateless = false,
  collecting = false,
  collected = new Map(),
  trackTimer,
  frozenRegion,
  bundleUrls = [];
const configReady = fetch("/healthz")
  .then((r) => r.json())
  .then((c) => {
    stateless = c.stateless;
  });
const $ = (id) => document.getElementById(id);
let region = { lat: 48.86, lon: 2.35, radius_km: 25 },
  mode = "record",
  recorder,
  session,
  startTime,
  chunks = [],
  timer,
  stream,
  audioContext,
  animation,
  truth = null,
  result = null,
  working = false;
const picker = L.map("picker").setView([region.lat, region.lon], 10),
  map = L.map("result-map").setView([48.86, 2.35], 10);
for (const m of [picker, map])
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(m);
let circle = L.circle([region.lat, region.lon], {
    radius: 25000,
    color: "#66d9c4",
    weight: 2,
    fillOpacity: 0.1,
  }).addTo(picker),
  center = L.marker([region.lat, region.lon]).addTo(picker),
  layers = L.layerGroup().addTo(map);
function status(s, error = false) {
  $("status").textContent = s;
  $("status").classList.toggle("error", error);
}
async function api(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) {
    let e;
    try {
      e = await r.json();
    } catch {
      throw Error(`Server returned ${r.status}`);
    }
    throw Error(
      typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail || e),
    );
  }
  return r.json();
}
function updateRegion(lat, lon) {
  region.lat = lat;
  region.lon = lon;
  center.setLatLng([lat, lon]);
  circle.setLatLng([lat, lon]);
  $("coordinates").textContent = `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
  nearby();
}
picker.on("click", (e) => {
  if (!working && !collecting && (!recorder || recorder.state !== "recording"))
    updateRegion(e.latlng.lat, e.latlng.lng);
});
$("radius").oninput = () => {
  region.radius_km = +$("radius").value;
  circle.setRadius(region.radius_km * 1000);
  $("radius-value").textContent = region.radius_km + " km";
};
$("radius").onchange = nearby;
async function nearby() {
  if (collecting) return;
  try {
    const r = await api("/api/aircraft?" + new URLSearchParams(region));
    $("nearby").textContent =
      `${r.count} aircraft recently seen near this region`;
  } catch (e) {
    $("nearby").textContent = e.message;
  }
}
for (const b of document.querySelectorAll("[data-tab]"))
  b.onclick = () => {
    if (working || recorder?.state === "recording") return;
    mode = b.dataset.tab;
    document
      .querySelectorAll("[data-tab]")
      .forEach((v) => v.classList.toggle("selected", v === b));
    for (const t of ["record", "upload", "demo"]) $(t).hidden = t !== mode;
    $("region-controls").hidden = mode === "demo";
    picker.invalidateSize();
  };
let lastSearch = 0;
$("search").onsubmit = async (e) => {
  e.preventDefault();
  if (collecting || working || Date.now() - lastSearch < 1100) return;
  lastSearch = Date.now();
  try {
    const r = await api(
      "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" +
        encodeURIComponent($("city").value),
    );
    if (!r.length) throw Error("City not found");
    updateRegion(+r[0].lat, +r[0].lon);
    picker.setView([region.lat, region.lon], 10);
  } catch (e) {
    status(e.message, true);
  }
};
$("clock").oninput = () =>
  ($("clock-value").textContent = "±" + $("clock").value + " s");
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
let zones = [
  browserZone,
  "UTC",
  ...(Intl.supportedValuesOf
    ? Intl.supportedValuesOf("timeZone")
    : ["Europe/Paris", "America/New_York"]),
];
for (const z of [...new Set(zones)]) {
  let o = document.createElement("option");
  o.textContent = z;
  o.value = z;
  $("timezone").append(o);
}
const now = new Date();
$("start-time").value = new Date(now - now.getTimezoneOffset() * 60000)
  .toISOString()
  .slice(0, 19);
function utcFromLocal(value, zone) {
  if (!value) throw Error("Choose the recording start time");
  let nominal = Date.parse(value + "Z"),
    guess = nominal;
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  for (let i = 0; i < 3; i++) {
    const p = Object.fromEntries(
      fmt.formatToParts(guess).map((p) => [p.type, p.value]),
    );
    const wall = Date.UTC(
      +p.year,
      +p.month - 1,
      +p.day,
      +p.hour,
      +p.minute,
      +p.second,
    );
    guess += nominal - wall;
  }
  return new Date(guess).toISOString();
}
async function analyze(
  blob,
  start,
  reg,
  sid = null,
  clock = 20,
  positions = [],
) {
  working = true;
  $("badge").textContent = "ANALYZING";
  status("Matching Doppler ridges against aircraft tracks…");
  try {
    await configReady;
    blob = await normalizeAudio(blob, positions);
  } catch (e) {
    status(e.message, true);
    working = false;
    return;
  }
  const form = new FormData();
  form.append("file", blob, blob.name || "recording.wav");
  if (positions.length) form.append("tracks_json", JSON.stringify(positions));
  form.append("start_utc", String(start));
  Object.entries(reg).forEach(([k, v]) => form.append(k, v));
  form.append("clock_search_s", clock);
  if (sid) form.append("session_id", sid);
  try {
    result = await api("/api/analyze", { method: "POST", body: form });
    render(result);
    downloadBundle(blob, positions, start, reg);
  } catch (e) {
    status(e.message, true);
    $("badge").textContent = "NO FIT";
  } finally {
    working = false;
  }
}
$("analyze-upload").onclick = async () => {
  if (working) return;
  truth = null;
  try {
    if (!$("file").files[0]) throw Error("Choose an audio or video file");
    let positions = [];
    if ($("tracks-file").files[0]) {
      const bundle = JSON.parse(await $("tracks-file").files[0].text());
      positions = Array.isArray(bundle) ? bundle : bundle.positions;
      if (!Array.isArray(positions)) throw Error("Invalid tracks file");
    }
    analyze(
      $("file").files[0],
      utcFromLocal($("start-time").value, $("timezone").value),
      { ...region },
      null,
      +$("clock").value,
      positions,
    );
  } catch (e) {
    status(e.message, true);
  }
};
$("record-button").onclick = async () => {
  if (recorder?.state === "recording") {
    recorder.stop();
    return;
  }
  if (working) return;
  let frozen = { ...region };
  frozenRegion = frozen;
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder)
      throw Error(
        "Recording requires a supported browser on HTTPS or localhost. Upload a file instead.",
      );
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    session = (
      await api("/api/session/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...frozen, client_time_ms: Date.now() }),
      })
    ).session_id;
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find(
      (t) => MediaRecorder.isTypeSupported(t),
    );
    recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
    chunks = [];
    truth = null;
    recorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    recorder.onstart = () => {
      startTime = Date.now();
      $("record-button").textContent = "■ Stop & analyze";
      $("radius").disabled = true;
      timer = setInterval(() => {
        const s = Math.floor((Date.now() - startTime) / 1000);
        $("timer").textContent =
          `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
        if (s >= 180 && recorder.state === "recording") recorder.stop();
      }, 250);
    };
    recorder.onstop = async () => {
      clearInterval(timer);
      cancelAnimationFrame(animation);
      stream.getTracks().forEach((t) => t.stop());
      audioContext?.close();
      $("level").value = 0;
      $("record-button").textContent = "● Start recording";
      working = true;
      try {
        await api(`/api/session/${session}/stop`, { method: "POST" });
      } catch (e) {
        status(e.message, true);
      }
      const blob = new Blob(chunks, { type: recorder.mimeType });
      status("Collecting the final 20 seconds of aircraft positions…");
      await new Promise((resolve) => setTimeout(resolve, 20000));
      collecting = false;
      clearInterval(trackTimer);
      await collectTracks();
      $("radius").disabled = false;
      await analyze(
        blob,
        new Date(startTime).toISOString(),
        frozen,
        session,
        4,
        [...collected.values()],
      );
    };
    collected = new Map();
    collecting = true;
    await collectTracks();
    trackTimer = setInterval(collectTracks, 4000);
    recorder.start(1000);
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioContext.createMediaStreamSource(stream),
      analyser = audioContext.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const data = new Uint8Array(1024);
    function meter() {
      analyser.getByteTimeDomainData(data);
      $("level").value = Math.sqrt(
        data.reduce((s, v) => s + ((v - 128) / 128) ** 2, 0) / data.length,
      );
      animation = requestAnimationFrame(meter);
    }
    meter();
    status("Recording. Let the aircraft pass before stopping.");
  } catch (e) {
    collecting = false;
    clearInterval(trackTimer);
    stream?.getTracks().forEach((t) => t.stop());
    status(e.message, true);
  }
};
$("generate").onclick = async () => {
  if (working) return;
  working = true;
  $("generate").disabled = true;
  status("Building a test from the recent watch cache…");
  try {
    const d = await api("/api/demo/synthetic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    truth = [d.true_lat, d.true_lon];
    const wav = d.wav_b64
      ? new Blob([Uint8Array.from(atob(d.wav_b64), (c) => c.charCodeAt(0))], {
          type: "audio/wav",
        })
      : await (await fetch(d.wav_url)).blob();
    $("demo-audio").src = URL.createObjectURL(wav);
    $("demo-audio").hidden = false;
    await analyze(wav, d.start_utc, d.region, null, 4, d.tracks_json || []);
  } catch (e) {
    status(e.message, true);
  } finally {
    working = false;
    $("generate").disabled = false;
  }
};
function render(r) {
  layers.clearLayers();
  $("results").hidden = false;
  $("badge").textContent = r.confidence.toUpperCase();
  $("explanation").textContent = r.explanation;
  status(
    `Best estimate: ${r.best_guess.lat.toFixed(4)}, ${r.best_guess.lon.toFixed(4)}`,
  );
  let max = Math.max(...r.heatmap.map((c) => c.score));
  for (const c of r.heatmap) {
    const half = c.size_m / 2 / 111320,
      dx = half / Math.cos((c.lat * Math.PI) / 180),
      v = c.score / max;
    L.rectangle(
      [
        [c.lat - half, c.lon - dx],
        [c.lat + half, c.lon + dx],
      ],
      {
        stroke: false,
        fillColor: `hsl(${260 - 260 * v} 80% 55%)`,
        fillOpacity: v * 0.48,
      },
    ).addTo(layers);
  }
  for (const a of r.tracks)
    L.polyline(a.points, {
      color: a.hex === r.winning_aircraft.hex ? "#66d9c4" : "#7e8fa8",
      weight: a.hex === r.winning_aircraft.hex ? 3 : 1,
    }).addTo(layers);
  r.top_peaks.forEach((p, i) =>
    L.circleMarker([p.lat, p.lon], {
      radius: i ? 5 : 9,
      color: "#fff",
      fillColor: "#66d9c4",
      fillOpacity: 1,
    })
      .bindPopup(`Peak ${i + 1} · score ${p.score.toFixed(2)}`)
      .addTo(layers),
  );
  map.setView([r.best_guess.lat, r.best_guess.lon], 11);
  map.invalidateSize();
  $("spectrum").src = r.spectrogram;
  $("raw-spectrum").src = r.raw_spectrogram;
  const table = $("aircraft-table");
  table.replaceChildren();
  for (const a of r.aircraft) {
    const row = document.createElement("tr");
    for (const value of [
      a.flight || a.hex,
      a.type || "—",
      a.f0.toFixed(1) + " Hz",
      a.z_score.toFixed(1),
      (a.closest_approach_m / 1000).toFixed(1) + " km",
    ]) {
      let td = document.createElement("td");
      td.textContent = value;
      row.append(td);
    }
    table.append(row);
  }
  $("error-distance").textContent = "";
  if (truth) {
    L.marker(truth).addTo(layers).bindPopup("Hidden true point");
    $("error-distance").textContent =
      `Synthetic error: ${(map.distance(truth, [r.best_guess.lat, r.best_guess.lon]) / 1000).toFixed(2)} km`;
  }
}
$("compare").onclick = () => {
  if (!result) return;
  navigator.geolocation.getCurrentPosition(
    (p) => {
      const point = [p.coords.latitude, p.coords.longitude];
      L.marker(point)
        .addTo(layers)
        .bindPopup("Actual location (only on this device)")
        .openPopup();
      $("error-distance").textContent =
        `Distance from your location: ${(map.distance(point, [result.best_guess.lat, result.best_guess.lon]) / 1000).toFixed(2)} km · GPS accuracy ±${Math.round(p.coords.accuracy)} m`;
    },
    (e) => status(e.message, true),
  );
};
api("/api/coverage")
  .then((c) => {
    $("coverage").textContent =
      c.regions
        .map(
          (r) =>
            `${r.name}: ${r.earliest ? new Date(r.earliest * 1000).toISOString() : "no fixes yet"} → ${r.latest ? new Date(r.latest * 1000).toISOString() : "—"}`,
        )
        .join("\n") +
      "\n" +
      c.feed_status;
  })
  .catch((e) => ($("coverage").textContent = e.message));
nearby();
setInterval(nearby, 20000);

async function collectTracks() {
  try {
    const data = await api(
      "/api/aircraft?" + new URLSearchParams(frozenRegion),
    );
    for (const p of data.aircraft) {
      const key = p.hex + ":" + Math.round(p.t_unix * 2);
      if (collected.size < 20000 || collected.has(key)) collected.set(key, p);
    }
    $("collected").textContent =
      `${new Set([...collected.values()].map((p) => p.hex)).size} aircraft / ${collected.size} positions collected`;
  } catch (e) {
    $("collected").textContent = "Track collection: " + e.message;
  }
}

async function normalizeAudio(blob, positions) {
  // Decode MP4 on Safari or WebM/Opus on Chrome before uploading portable PCM.
  status("Converting audio to mono 8 kHz WAV…");
  let context;
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    const Offline =
      window.OfflineAudioContext || window.webkitOfflineAudioContext;
    context = new Audio();
    const buffer = await new Promise((resolve, reject) => {
      const promise = context.decodeAudioData;
      // Callback form also works on older iOS Safari.
      blob
        .arrayBuffer()
        .then((data) => promise.call(context, data, resolve, reject), reject);
    });
    const trackBytes = new TextEncoder().encode(
      JSON.stringify(positions),
    ).length;
    const duration = Math.min(
      buffer.duration,
      270,
      stateless ? (4400000 - trackBytes - 12000) / 16000 : 270,
    );
    if (duration < 20)
      throw Error(
        "Tracks leave insufficient upload space. Use a shorter recording.",
      );
    const frames = Math.floor(duration * 8000);
    const mono = context.createBuffer(
      1,
      Math.min(buffer.length, Math.ceil(duration * buffer.sampleRate)),
      buffer.sampleRate,
    );
    const samples = mono.getChannelData(0);
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const input = buffer.getChannelData(channel);
      for (let i = 0; i < samples.length; i++)
        samples[i] += input[i] / buffer.numberOfChannels;
    }
    const offline = new Offline(1, frames, 8000);
    const source = offline.createBufferSource();
    source.buffer = mono;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    const data = new ArrayBuffer(44 + frames * 2),
      view = new DataView(data);
    function text(offset, value) {
      for (let i = 0; i < value.length; i++)
        view.setUint8(offset + i, value.charCodeAt(i));
    }
    text(0, "RIFF");
    view.setUint32(4, 36 + frames * 2, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 8000, true);
    view.setUint32(28, 16000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, frames * 2, true);
    const output = rendered.getChannelData(0);
    for (let i = 0; i < frames; i++) {
      const v = Math.max(-1, Math.min(1, output[i]));
      view.setInt16(44 + i * 2, v < 0 ? v * 32768 : v * 32767, true);
    }
    if (duration < buffer.duration)
      status(
        `Audio trimmed to ${duration.toFixed(0)} seconds for the upload limit.`,
      );
    return new Blob([data], { type: "audio/wav" });
  } catch (error) {
    if (stateless)
      throw Error(
        "Could not convert this audio in your browser. Hosted mode needs a decodable audio file or PCM WAV under 4.4 MB. " +
          error.message,
      );
    status("Browser decoding failed; the box will decode the original file.");
    return blob;
  } finally {
    if (context) await context.close();
  }
}

function downloadBundle(blob, positions, start, reg) {
  bundleUrls.forEach((url) => URL.revokeObjectURL(url));
  bundleUrls = [
    URL.createObjectURL(blob),
    URL.createObjectURL(
      new Blob([JSON.stringify({ start_utc: start, region: reg, positions })], {
        type: "application/json",
      }),
    ),
  ];
  $("download-wav").href = bundleUrls[0];
  $("download-tracks").href = bundleUrls[1];
  $("downloads").hidden = false;
}
