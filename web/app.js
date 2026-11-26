"use strict";
// Tesla page. Everything is relative so it works at :9877/ and behind the gateway at /tesla/.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { status: null, units: { distance: "mi", pressure: "psi", temp: "F" }, view: null, hours: 24, days: 30, actKind: "all" };

async function api(path, opts = {}) {
  const o = { headers: { "X-Requested-With": "tesla" }, ...opts };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 30000); o.signal = ctl.signal;
  try {
    const r = await fetch(path, o);
    if (r.status === 401) { location.reload(); throw new Error("signed out"); }
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  } finally { clearTimeout(t); }
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(toast._t); toast._t = setTimeout(() => (t.style.display = "none"), 3500); }

// ---------- units / formatting
const U = () => S.units;
const dist = (mi, d = 0) => mi == null ? "—" : (U().distance === "km" ? (mi * 1.609344).toFixed(d) : (+mi).toFixed(d));
const distU = () => U().distance;
const temp = (c) => c == null ? "—" : (U().temp === "F" ? Math.round(c * 9 / 5 + 32) + "°F" : (+c).toFixed(1) + "°C");
const pres = (bar) => bar == null ? "—" : (U().pressure === "psi" ? Math.round(bar * 14.5038) : (+bar).toFixed(2));
const presU = () => U().pressure;
const whmi = (v) => v == null ? "—" : (U().distance === "km" ? Math.round(v / 1.609344) + " Wh/km" : v + " Wh/mi");
const speed = (mph) => mph == null ? "—" : (U().distance === "km" ? Math.round(mph * 1.609344) + " km/h" : Math.round(mph) + " mph");
const dtFmt = (ts, o = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) => ts ? new Date(ts * 1000).toLocaleString([], o) : "—";
function ago(ts) {
  if (!ts) return "never"; const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now"; if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400 * 2) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " days ago";
}
const durFmt = (s) => { const m = Math.round(s / 60); return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`; };
const money = (d) => "$" + (+d).toFixed(2);

// ---------- router
const LOADERS = { dash: loadDash, trips: loadTrips, charging: loadCharging, supercharging: loadSupercharging, battery: loadBattery, service: loadService, activity: loadActivity, settings: loadSettings };
function route() {
  const v = (location.hash.slice(1) || "dash").split("?")[0];
  const view = LOADERS[v] ? v : "dash";
  S.view = view;
  $$("section.view").forEach((s) => s.classList.toggle("on", s.id === "v-" + view));
  $$(".rig-tabs a").forEach((a) => a.classList.toggle("on", a.dataset.v === view));
  LOADERS[view]().catch((e) => toast(e.message));
  setTimeout(() => { for (const m of [MAP, TMAP]) if (m) m.invalidateSize(); }, 50);
}
addEventListener("hashchange", route);

// ---------- maps
let MAP, TMAP, carMarker, trackLine;
// Esri tiles by default (no Referer rules); the choice is remembered per browser.
const esri = (svc) => `https://server.arcgisonline.com/ArcGIS/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`;
const esriAttr = "Tiles &copy; Esri";
function baseLayers() {
  return {
    "Dark": L.layerGroup([
      L.tileLayer(esri("Canvas/World_Dark_Gray_Base"), { maxZoom: 19, maxNativeZoom: 16, attribution: esriAttr }),
      L.tileLayer(esri("Canvas/World_Dark_Gray_Reference"), { maxZoom: 19, maxNativeZoom: 16 })]),
    "Streets": L.tileLayer(esri("World_Street_Map"), { maxZoom: 19, maxNativeZoom: 19, className: "tiles-dark", attribution: esriAttr }),
    "Satellite": L.layerGroup([
      L.tileLayer(esri("World_Imagery"), { maxZoom: 19, maxNativeZoom: 19, attribution: esriAttr + ", Maxar, Earthstar Geographics" }),
      L.tileLayer(esri("Reference/World_Boundaries_and_Places"), { maxZoom: 19, maxNativeZoom: 19 })]),
    "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19, className: "tiles-dark", attribution: "&copy; OpenStreetMap contributors" }),
  };
}
function addBase(map) {
  const layers = baseLayers();
  const want = localStorage.getItem("teslaMapLayer");
  (layers[want] || layers["Streets"]).addTo(map);
  L.control.layers(layers, null, { position: "topright" }).addTo(map);
  map.on("baselayerchange", (e) => localStorage.setItem("teslaMapLayer", e.name));
}
function initMap() {
  if (MAP) return;
  MAP = L.map("map", { zoomControl: true, attributionControl: true }).setView([37.7, -122.2], 11);
  addBase(MAP);
}
const carIcon = () => L.divIcon({ className: "", html: '<div class="carpin"></div>', iconSize: [26, 26], iconAnchor: [13, 13] });

// ---------- status (header + dashboard)
async function refreshStatus() {
  const s = await api("api/status");
  S.status = s; S.units = s.units || S.units;
  if (!S.appsDone) { S.appsDone = true; if (window.RigApps) RigApps.render(s.apps, "Tesla"); }
  $("#carName").textContent = s.name || "Tesla";
  const d = s.details || {};
  $("#carSub").textContent = [d.car_type === "modelx" ? "Model X" : d.car_type, d.trim, d.software].filter(Boolean).join(" · ");
  const c = s.car || {};
  const awake = s.state === "online";
  setLed("#ledAwake", s.waking ? "amber" : awake ? "ok" : s.state === "asleep" ? "sleep" : "", s.waking ? "Waking…" : awake ? (s.mode === "driving" ? "Driving" : s.mode === "charging" ? "Charging" : "Awake") : s.state === "asleep" ? "Asleep" : (s.state || "Unknown"));
  setLed("#ledLock", c.locked === 0 || c.locked === false ? "err" : c.locked ? "ok" : "", c.locked === 0 || c.locked === false ? "Unlocked" : c.locked ? "Locked" : "Lock ?");
  const plugged = c.charging_state && c.charging_state !== "Disconnected";
  const charging = ["Charging", "Starting"].includes(c.charging_state);
  setLed("#ledPlug", charging ? "amber" : plugged ? "ok" : "", charging ? "Charging" : "Plugged");
  $("#ledPlug").title = charging ? "Charging" : plugged ? "Plugged in" : "Not plugged in";
  return s;
}
function setLed(sel, cls, text) { const el = $(sel); el.className = "lamp " + (cls === "amber" ? "accent" : cls); $("span", el).textContent = text; }

function kv(el, rows) {
  el.innerHTML = rows.filter((r) => r).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
}
const yesno = (v, on = "On", off = "Off") => v == null ? '<span class="mute">—</span>' : v ? `<span class="amb-t">${on}</span>` : `<span class="dim">${off}</span>`;

async function loadDash() {
  initMap();
  const s = await refreshStatus();
  const c = s.car || {}, d = s.details || {};
  $("#roBatt").innerHTML = `${c.battery_level != null ? Math.round(c.battery_level) : "--"}<small>%</small>`;
  $("#roRange").innerHTML = `${c.battery_range != null ? dist(c.battery_range) : "---"}<small>${distU()}</small>`;
  $("#battFill").style.width = (c.battery_level || 0) + "%";
  $("#battLimit").style.left = (d.charge_limit || 80) + "%";
  const charging = ["Charging", "Starting"].includes(c.charging_state);
  kv($("#kvNow"), [
    ["Where", s.place ? esc(s.place) : c.latitude != null ? `<a href="https://maps.google.com/?q=${c.latitude},${c.longitude}" target="_blank" rel="noopener">map</a>` : "—"],
    ["Last reading", `${ago(s.last_read_ts)} <span class="mute">(${dtFmt(s.last_read_ts, { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })})</span>`],
    charging ? ["Charging", `${c.charger_power ?? 0} kW · ${d.charger_voltage ?? "—"} V · ${d.charger_current ?? "—"} A`] : null,
    charging && d.minutes_to_full ? ["Full in", durFmt(d.minutes_to_full * 60)] : null,
    ["Inside / outside", `${temp(c.inside_temp)} / ${temp(c.outside_temp)}`],
    ["Climate", d.asleep_since_read ? '<span class="dim">Off</span> <span class="mute">(asleep)</span>' : yesno(c.is_climate_on)],
    ["Sentry", d.asleep_since_read ? '<span class="dim">Off</span> <span class="mute">(asleep: Sentry would keep it awake)</span>' : yesno(d.sentry)],
    ["Odometer", `${c.odometer != null ? Number(dist(c.odometer)).toLocaleString() : "—"} ${distU()}`],
    d.update ? ["Software update", `<span class="amb-t">${esc(d.update)} ${esc(d.update_version || "")}</span>`] : null,
  ]);
  const nxt = s.next_poll_at ? Math.max(0, Math.round((s.next_poll_at - Date.now() / 1000) / 60)) : null;
  $("#pollText").textContent = `Car is ${s.state || "unknown"} (checked ${ago(s.state_ts)}). Next check in ${nxt ?? "?"} min. ` +
    `It is only read when it is already awake, and woken only if there has been no reading for a day.`;
  renderTyres(c);
  kv($("#kvCar"), [
    ["Charge limit", d.charge_limit != null ? d.charge_limit + "%" : "—"],
    ["Charge current", d.charge_amps != null ? `${d.charge_amps} A <span class="mute">of ${d.charge_amps_max ?? "?"}</span>` : "—"],
    ["Scheduled charging", d.scheduled_charging && d.scheduled_charging !== "Off" ? esc(d.scheduled_charging) : yesno(false)],
    ["Off-peak charging", yesno(d.off_peak)],
    ["Preconditioning", yesno(d.preconditioning)],
    ["Cabin overheat protection", esc(d.cabin_overheat ?? "—")],
    ["Climate set to", d.driver_temp != null ? temp(d.driver_temp) : "—"],
    ["Valet mode", yesno(d.valet)],
    ["Speed limit mode", yesno(d.speed_limit)],
    ["Doors / boot / frunk", openings(d)],
  ]);
  $("#roSpend").textContent = (+s.budget.month).toFixed(2);
  $("#roCap").textContent = (+s.budget.cap).toFixed(2);
  $("#spendFill").style.width = Math.min(100, (s.budget.month / Math.max(0.01, s.budget.cap)) * 100) + "%";
  if (c.latitude != null) {
    const ll = [c.latitude, c.longitude];
    if (!carMarker) { carMarker = L.marker(ll, { icon: carIcon(), zIndexOffset: 1000 }).addTo(MAP); MAP.setView(ll, 14); }
    else carMarker.setLatLng(ll);
    carMarker.bindTooltip(`${esc(s.name)} · ${ago(s.last_read_ts)}`);
    $("#whereText").textContent = s.place ? `Parked at ${s.place}` : "";
  }
  await loadTrack();
  await loadNearby();
}
let nearbyLayer;
async function loadNearby() {
  const n = await api("api/nearby");
  const sc = (n && n.data && n.data.superchargers) || [];
  const fresh = n && Date.now() / 1000 - n.ts < 6 * 3600;
  $("#nearbyPanel").style.display = sc.length ? "" : "none";
  if (nearbyLayer) { nearbyLayer.remove(); nearbyLayer = null; }
  if (!sc.length) return;
  $("#nearbyAge").textContent = `(as of ${ago(n.ts)}${fresh ? "" : ", stall counts may be stale"})`;
  $("#nearbyList").innerHTML = '<div class="nb"><span class="mute">Site</span><span class="mute">Free</span><span class="mute">Away</span>' +
    sc.slice(0, 6).map((x) => `<span>${esc(x.name)}${x.site_closed ? ' <span class="err-t">closed</span>' : ""}</span>
      <span class="free ${x.available_stalls ? "ok-t" : "err-t"}">${x.available_stalls ?? "?"}/${x.total_stalls ?? "?"}</span>
      <span>${dist(x.distance_miles, 1)} ${distU()}</span>`).join("") + "</div>";
  nearbyLayer = L.layerGroup(sc.filter((x) => x.location).map((x) => L.marker([x.location.lat, x.location.long], {
    icon: L.divIcon({ className: "", html: '<div class="scpin"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) })
    .bindTooltip(`${esc(x.name)} · ${x.available_stalls ?? "?"}/${x.total_stalls ?? "?"} free`))).addTo(MAP);
}
function openings(d) {
  const any = (o) => Object.values(o || {}).some((v) => v);
  const doors = any(d.doors), trunks = any(d.trunks), wins = any(d.windows);
  if (d.doors == null) return "—";
  const bits = [];
  if (doors) bits.push("door open"); if (trunks) bits.push("boot/frunk open"); if (wins) bits.push("window open");
  return bits.length ? `<span class="err-t">${bits.join(", ")}</span>` : '<span class="ok-t">all closed</span>';
}
function renderTyres(c) {
  const low = (b) => b != null && b < 2.7;
  const t = (k, label) => `<div class="t ${low(c["tpms_" + k]) ? "low" : ""}"><b>${pres(c["tpms_" + k])}</b><span>${label} · ${presU()}</span></div>`;
  $("#tyres").innerHTML = t("fl", "Front left") + '<div class="car" aria-hidden="true"></div>' + t("fr", "Front right") + t("rl", "Rear left") + t("rr", "Rear right");
}
async function loadTrack() {
  const pts = await api(`api/track?hours=${S.hours}`);
  if (trackLine) trackLine.remove();
  const lls = pts.map((p) => [p[1], p[2]]);
  if (lls.length > 1) {
    trackLine = L.polyline(lls, { color: "#ffb23e", weight: 3, opacity: .85 }).addTo(MAP);
    if (S.hours > 24) MAP.fitBounds(trackLine.getBounds(), { padding: [30, 30], maxZoom: 15 });
  }
}
$$("[data-hours]").forEach((b) => b.addEventListener("click", () => {
  S.hours = +b.dataset.hours; $$("[data-hours]").forEach((x) => x.classList.toggle("on", x === b)); loadTrack().catch((e) => toast(e.message));
}));
async function action(kind, btn) {
  if (kind === "wake_read" && !confirm("Wake the car and read it once? This costs about 2 cents and the car stays awake for ~15 minutes.")) return;
  btn.disabled = true;
  const label = btn.textContent, before = (S.status || {}).last_read_ts || 0;
  try {
    await api("api/action", { method: "POST", body: { action: kind } });
    toast(kind === "read" ? "Asked. If the car is awake it will be read in a few seconds; if asleep it is left alone." : "Wake sent. The car usually answers within a minute or two.");
    // watch for the new reading instead of guessing when it lands: the car takes anything from 20 s to a few minutes
    const until = Date.now() + (kind === "read" ? 20000 : 6 * 60000), t0 = Date.now();
    while (Date.now() < until) {
      await new Promise((r) => setTimeout(r, 5000));
      if (kind === "wake_read") btn.textContent = `Waiting for the car… ${Math.round((Date.now() - t0) / 1000)} s`;
      const s = await refreshStatus().catch(() => null);
      if (s && (s.last_read_ts || 0) > before) {
        await loadDash().catch(() => {});
        toast(`New reading at ${dtFmt(s.last_read_ts, { hour: "2-digit", minute: "2-digit" })}`);
        return;
      }
    }
    if (kind === "wake_read") toast("No reading yet: the car has not come online. The poller keeps checking for a few minutes.");
    loadDash().catch(() => {});
  } catch (e) { toast(e.message); } finally { btn.textContent = label; btn.disabled = false; }
}
$("#btnRead").addEventListener("click", (e) => action("read", e.currentTarget));
$("#btnWake").addEventListener("click", (e) => action("wake_read", e.currentTarget));

// ---------- charts (single series, SVG, crosshair + tooltip)
// Drawn at the container's real pixel width (not a stretched viewBox) so text stays 11px.
const CHARTS = new Map();
let _rz; addEventListener("resize", () => { clearTimeout(_rz); _rz = setTimeout(() => {
  for (const [el, draw] of CHARTS) if (el.isConnected && el.offsetParent) draw(); }, 150); });
function chartFrame(el, w, h) {
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img"></svg><div class="tip"></div>`;
  return [$("svg", el), $(".tip", el)];
}
function niceTicks(lo, hi, n = 4) {
  if (hi === lo) { hi = lo + 1; }
  const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / n))); const err = (hi - lo) / n / step;
  const s = step * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  const out = []; for (let v = Math.ceil(lo / s) * s; v <= hi + 1e-9; v += s) out.push(+v.toFixed(6)); return out;
}
function lineChart(el, pts, o = {}) {
  if (!pts.length) { el.innerHTML = '<div class="empty">No readings in this period yet.</div>'; return; }
  CHARTS.set(el, () => lineChart(el, pts, o));
  const W = Math.max(280, el.clientWidth || 640), H = 220, L0 = 44, R0 = 12, T0 = 10, B0 = 26;
  const [svg, tip] = chartFrame(el, W, H);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1;
  let y0 = o.yMin ?? Math.min(...ys), y1 = o.yMax ?? Math.max(...ys);
  if (o.yMin == null) { const pad = Math.max((y1 - y0) * 0.1, Math.abs(y1) * 0.02, 1); y0 -= pad; y1 += pad; }
  const X = (v) => L0 + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (W - L0 - R0);
  const Y = (v) => T0 + (1 - (v - y0) / (y1 - y0)) * (H - T0 - B0);
  let g = '<g class="grid">', ax = '<g class="axis">';
  for (const t of niceTicks(y0, y1)) { g += `<line x1="${L0}" x2="${W - R0}" y1="${Y(t)}" y2="${Y(t)}"/>`; ax += `<text x="${L0 - 6}" y="${Y(t) + 4}" text-anchor="end">${o.fmtY ? o.fmtY(t) : t}</text>`; }
  const span = x1 - x0, nx = Math.max(2, Math.min(6, Math.floor(W / 120)));
  for (let i = 0; i <= nx; i++) {
    const v = x0 + span * i / nx;
    ax += `<text x="${X(v)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === nx ? "end" : "middle"}">${dtFmt(v, span > 3 * 86400 ? { day: "numeric", month: "short" } : { weekday: "short", hour: "2-digit", minute: "2-digit" })}</text>`;
  }
  g += "</g>"; ax += "</g>";
  // step-after line: values hold until the next reading
  let d = `M${X(xs[0])},${Y(ys[0])}`;
  for (let i = 1; i < pts.length; i++) d += o.step === false ? `L${X(xs[i])},${Y(ys[i])}` : `H${X(xs[i])}V${Y(ys[i])}`;
  const area = `${d}V${Y(y0)}H${X(xs[0])}Z`;
  svg.innerHTML = g + ax + `<path class="area" d="${area}"/><path class="line" d="${d}"/>` +
    (pts.length === 1 ? `<circle class="dot" cx="${X(xs[0])}" cy="${Y(ys[0])}" r="4"/>` : "") +
    `<line class="xh" y1="${T0}" y2="${H - B0}" style="display:none"/><circle class="dot hov" r="4" style="display:none"/>` +
    `<rect x="${L0}" y="${T0}" width="${W - L0 - R0}" height="${H - T0 - B0}" fill="transparent"/>`;
  const xh = $(".xh", svg), dot = $(".hov", svg), hit = $("rect", svg);
  hit.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) / r.width * W;
    let best = 0; for (let i = 0; i < pts.length; i++) if (Math.abs(X(xs[i]) - sx) < Math.abs(X(xs[best]) - sx)) best = i;
    const px = X(xs[best]), py = Y(ys[best]);
    xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.style.display = "";
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.style.display = "";
    tip.style.display = "block"; tip.style.left = (px / W * r.width) + "px"; tip.style.top = (py / H * r.height) + "px";
    tip.innerHTML = `<b>${o.fmtTip ? o.fmtTip(ys[best]) : ys[best]}</b> <span class="mute">${dtFmt(xs[best])}</span>`;
  });
  hit.addEventListener("mouseleave", () => { xh.style.display = dot.style.display = tip.style.display = "none"; });
}
function barChart(el, bars, o = {}) {
  if (!bars.length) { el.innerHTML = '<div class="empty">Nothing recorded in this period yet.</div>'; return; }
  CHARTS.set(el, () => barChart(el, bars, o));
  const W = Math.max(280, el.clientWidth || 640), H = 200, L0 = 40, R0 = 8, T0 = 10, B0 = 26;
  const [svg, tip] = chartFrame(el, W, H);
  const y1 = Math.max(...bars.map((b) => b[1]), o.yMin1 ?? 1) * 1.1;
  const Y = (v) => T0 + (1 - v / y1) * (H - T0 - B0);
  const bw = (W - L0 - R0) / bars.length, gap = Math.min(4, bw * 0.25);
  let g = '<g class="grid">', ax = '<g class="axis">';
  for (const t of niceTicks(0, y1)) { g += `<line x1="${L0}" x2="${W - R0}" y1="${Y(t)}" y2="${Y(t)}"/>`; ax += `<text x="${L0 - 6}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`; }
  const every = Math.ceil(bars.length / Math.max(3, Math.floor(W / 70)));
  let rects = "";
  bars.forEach((b, i) => {
    const x = L0 + i * bw + gap / 2, w = Math.max(1, bw - gap), y = Y(b[1]), h = Math.max(0, H - B0 - y);
    const r = Math.min(4, w / 2, h);
    rects += h > 0 ? `<path class="bar" data-i="${i}" d="M${x},${H - B0}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${H - B0}Z"/>` : "";
    rects += `<rect data-i="${i}" x="${L0 + i * bw}" y="${T0}" width="${bw}" height="${H - T0 - B0}" fill="transparent"/>`;
    if (i % every === 0) ax += `<text x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(b[2] ?? b[0])}</text>`;
  });
  svg.innerHTML = g + "</g>" + ax + "</g>" + rects;
  svg.addEventListener("mousemove", (e) => {
    const i = e.target.dataset?.i; if (i == null) return;
    const b = bars[i], r = svg.getBoundingClientRect();
    $$(".bar", svg).forEach((p) => p.classList.toggle("hl", p.dataset.i === i));
    tip.style.display = "block"; tip.style.left = ((L0 + (+i + 0.5) * bw) / W * r.width) + "px"; tip.style.top = (Y(b[1]) / H * r.height) + "px";
    tip.innerHTML = `<b>${o.fmt ? o.fmt(b[1]) : b[1]}</b> <span class="mute">${esc(b[3] ?? b[0])}</span>`;
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; $$(".bar", svg).forEach((p) => p.classList.remove("hl")); });
}
const tile = (v, k) => `<div class="tile"><b>${v}</b><span>${esc(k)}</span></div>`;

// ---------- trips
let tripLayer;
async function loadTrips() {
  if (!S.status) await refreshStatus();
  const trips = await api("api/trips?days=90");
  const month = trips.filter((t) => new Date(t.start * 1000).getMonth() === new Date().getMonth() && Date.now() / 1000 - t.start < 32 * 86400);
  const miles = month.reduce((a, t) => a + (t.miles || 0), 0);
  const eff = month.filter((t) => t.wh_per_mile); const avgEff = eff.length ? Math.round(eff.reduce((a, t) => a + t.wh_per_mile * t.miles, 0) / eff.reduce((a, t) => a + t.miles, 0)) : null;
  $("#tripTiles").innerHTML = tile(month.length, "trips this month") + tile(`${dist(miles)} ${distU()}`, "driven this month") +
    tile(whmi(avgEff), "average efficiency (est.)") + tile(trips.length ? speed(Math.max(...trips.map((t) => t.max_mph || 0))) : "—", "top speed seen (90 days)");
  if (!trips.length) {
    $("#tripTable").innerHTML = '<div class="empty">No trips recorded yet. Drives longer than about 30 minutes will show up here.</div>';
  } else {
    $("#tripTable").innerHTML = `<table><thead><tr><th>When</th><th>From → to</th><th class="n">${distU()}</th><th class="n">Battery</th><th class="n">Efficiency</th><th class="n">Top speed</th></tr></thead><tbody>` +
      trips.map((t, i) => `<tr data-i="${i}"><td>${dtFmt(t.start)}<div class="mute">${durFmt(t.end - (t.baseline_ts && t.start - t.baseline_ts < 3600 ? t.baseline_ts : t.start))}</div></td>
        <td>${esc(t.from || "elsewhere")} → ${esc(t.to || "elsewhere")}</td><td class="n">${dist(t.miles, 1)}</td>
        <td class="n">${t.battery_start != null ? Math.round(t.battery_start) : "—"}% → ${t.battery_end != null ? Math.round(t.battery_end) : "—"}%</td>
        <td class="n">${whmi(t.wh_per_mile)}</td><td class="n">${speed(t.max_mph)}</td></tr>`).join("") + "</tbody></table>";
  }
  if (!TMAP) { TMAP = L.map("tripMap").setView([37.7, -122.2], 10); addBase(TMAP); }
  setTimeout(() => TMAP.invalidateSize(), 30);
  const show = (i) => {
    $$("#tripTable tr[data-i]").forEach((r) => r.classList.toggle("sel", r.dataset.i == i));
    if (tripLayer) tripLayer.remove();
    const t = trips[i]; if (!t || t.points.length < 1) return;
    tripLayer = L.layerGroup([L.polyline(t.points, { color: "#ffb23e", weight: 4 }),
      L.circleMarker(t.points[0], { radius: 6, color: "#63d46f", fillOpacity: 1 }).bindTooltip("Start"),
      L.circleMarker(t.points[t.points.length - 1], { radius: 6, color: "#ff5446", fillOpacity: 1 }).bindTooltip("End")]).addTo(TMAP);
    TMAP.fitBounds(L.latLngBounds(t.points), { padding: [30, 30], maxZoom: 15 });
  };
  $$("#tripTable tr[data-i]").forEach((r) => r.addEventListener("click", () => show(+r.dataset.i)));
  if (trips.length) show(0);
}

// ---------- charging
async function loadCharging() {
  if (!S.status) await refreshStatus();
  const ss = await api("api/charging");
  const now = new Date(), thisMonth = ss.filter((s) => { const d = new Date(s.start * 1000); return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear(); });
  const kwh = thisMonth.reduce((a, s) => a + (s.kwh || 0), 0), home = thisMonth.filter((s) => !s.fast).reduce((a, s) => a + (s.kwh || 0), 0);
  $("#chgTiles").innerHTML = tile(thisMonth.length, "sessions this month") + tile(kwh.toFixed(1) + " kWh", "added this month") +
    tile(home.toFixed(1) + " kWh", "at AC / home") + tile((kwh - home).toFixed(1) + " kWh", "Supercharging");
  // kWh per day, last 30 days
  const days = []; for (let i = 29; i >= 0; i--) { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - i); days.push(d); }
  const byDay = days.map((d) => {
    const k = ss.filter((s) => { const x = new Date(s.start * 1000); x.setHours(0, 0, 0, 0); return +x === +d; }).reduce((a, s) => a + (s.kwh || 0), 0);
    return [d.toISOString().slice(0, 10), +k.toFixed(1), d.toLocaleDateString([], { day: "numeric", month: "short" }), d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })];
  });
  barChart($("#chgChart"), byDay.some((b) => b[1] > 0) ? byDay : [], { fmt: (v) => v + " kWh" });
  $("#chgTable").innerHTML = !ss.length ? '<div class="empty">No charging sessions recorded yet. The next charge longer than about half an hour will show up here.</div>' :
    `<table><thead><tr><th>Started</th><th>Duration</th><th>Where</th><th>Type</th><th class="n">Battery</th><th class="n">kWh</th><th class="n">Peak kW</th><th class="n">Avg kW</th><th class="n">Max V · A</th></tr></thead><tbody>` +
    ss.map((s) => {
      const a = s.start_pct || 0, b = s.end_pct || 0;
      return `<tr><td>${dtFmt(s.start)}</td><td>${s.ongoing ? '<span class="ok-t">charging now</span>' : durFmt(s.end - s.start)}</td>
        <td>${s.place ? esc(s.place) : s.lat != null ? `<a href="https://maps.google.com/?q=${s.lat},${s.lon}" target="_blank" rel="noopener">map</a>` : "—"}</td>
        <td><span class="pill ${s.fast ? "amb-t" : "ok-t"}">${esc(s.kind)}</span></td>
        <td class="n">${Math.round(a)}% → ${Math.round(b)}%<span class="minibar"><i style="left:${a}%;width:${Math.max(b - a, 1)}%"></i></span></td>
        <td class="n">${(s.kwh || 0).toFixed(1)}</td><td class="n">${Math.round(s.peak_kw)}</td><td class="n">${Math.round(s.avg_kw || 0)}</td>
        <td class="n">${s.max_v || "—"} V · ${s.max_a || "—"} A</td></tr>`;
    }).join("") + "</tbody></table>";
}

// ---------- supercharging (Tesla's history, real site prices)
let SC = null, scShown = 50;
const usd = (v, d = 2) => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
async function loadSupercharging() {
  if (!S.status) await refreshStatus();
  SC = await api("api/supercharging");
  const L = SC.lifetime, Y = SC.year, M = SC.month;
  $("#scLife").innerHTML = `${Math.round(L.value)}<small>USD</small>`;
  $("#scYear").innerHTML = `${Math.round(Y.value)}<small>USD</small>`;
  $("#scSince").textContent = `${usd(L.value)} across ${L.sessions.toLocaleString()} sessions and ${Math.round(L.kwh).toLocaleString()} kWh since ${dtFmt(SC.since, { month: "long", year: "numeric" })}, at the price each site charged at the time.`;
  const cong = L.congestion_recorded + L.congestion_estimated;
  const hr = SC.home_rate;
  $("#scTiles").innerHTML = tile(usd(M.value), `this month (${M.sessions} sessions)`) + tile(`${Math.round(L.kwh).toLocaleString()} kWh`, "Supercharged in total") +
    tile(usd(L.avg_rate, 3) + "/kWh", "average price you would have paid") + tile(usd(cong), "congestion fees (would have been)") +
    tile(usd(L.idle_paid), "idle fees you actually paid (not covered)") +
    tile(`<span class="athome">${usd(L.home_cost)}</span>`, `same charge at home: ${esc(SC.home_rate_label)} ${usd(hr)}/kWh + ${SC.home_loss_pct}% charging loss`) +
    tile(usd(L.value - L.home_cost), "Supercharging over home charging, lifetime") +
    tile(`<span class="athome">${usd(Y.home_cost)}</span> vs ${usd(Y.value)}`, `this year: home (${esc(SC.home_rate_label)}) vs Supercharger`);
  const months = SC.by_month.slice(-36).map(([k, v, kwh, n]) => {
    const d = new Date(k + "-01T12:00"); return [k, v, d.toLocaleDateString([], { month: "short", year: "2-digit" }), `${d.toLocaleDateString([], { month: "long", year: "numeric" })} · ${n} sessions · ${Math.round(kwh)} kWh`];
  });
  barChart($("#scChart"), months, { fmt: (v) => usd(v) });
  const periodTable = (rows, label) => `<table><thead><tr><th>${label}</th><th class="n">Sessions</th><th class="n">kWh</th><th class="n">Avg $/kWh</th><th class="n">Supercharger value</th><th class="n athome">At home (${esc(SC.home_rate_label)})</th><th class="n">Difference</th></tr></thead><tbody>` +
    rows.map(([k, t]) => `<tr><td>${k}</td><td class="n">${t.sessions}</td><td class="n">${Math.round(t.kwh).toLocaleString()}</td><td class="n">${usd(t.avg_rate, 3)}</td>
      <td class="n">${usd(t.value)}</td><td class="n athome">${usd(t.home_cost)}</td><td class="n">${usd(t.value - t.home_cost)}</td></tr>`).join("") + "</tbody></table>";
  $("#scYears").innerHTML = periodTable(SC.by_year.slice().reverse(), "Year");
  $("#scMonths").innerHTML = periodTable(SC.months.slice().reverse().map(([k, t]) => [new Date(k + "-01T12:00").toLocaleDateString([], { month: "long", year: "numeric" }), t]), "Month");
  $("#scSites").innerHTML = `<table><thead><tr><th>Site</th><th class="n">Visits</th><th class="n">kWh</th><th class="n">Price range</th><th class="n">Value</th><th class="n athome">At home (${esc(SC.home_rate_label)})</th></tr></thead><tbody>` +
    SC.sites.slice(0, 15).map((x) => `<tr><td title="Last visit ${dtFmt(x.last)}">${esc(x.site)}</td><td class="n">${x.sessions}</td><td class="n">${Math.round(x.kwh).toLocaleString()}</td>
      <td class="n">${x.rate_min === x.rate_max ? usd(x.rate_min) : `${usd(x.rate_min)}–${usd(x.rate_max)}`}</td><td class="n">${usd(x.value)}</td><td class="n athome">${usd(x.home_cost)}</td></tr>`).join("") + "</tbody></table>" +
    (SC.sites.length > 15 ? `<p class="mute" style="font-size:12px">${SC.sites.length - 15} more sites not shown.</p>` : "");
  renderScSessions();
}
function renderScSessions() {
  const rows = SC.sessions.slice(0, scShown);
  $("#scSessions").innerHTML = `<table><thead><tr><th>When</th><th>Site</th><th class="n">kWh</th><th class="n">Time</th><th class="n">Avg kW</th><th class="n">Price</th><th class="n">Congestion</th><th class="n">Would have cost</th><th class="n athome">At home (${esc(SC.home_rate_label)})</th><th class="n">Idle paid</th></tr></thead><tbody>` +
    rows.map((r) => {
      const cg = r.congestion_rate == null ? '<span class="mute">none at site</span>'
        : r.congestion_min ? `${r.congestion_min} min · ${usd(r.congestion_value)}`
        : r.congestion_est_min != null ? `<span title="Estimated from the poller's readings: minutes at 80%+ while charging">${r.congestion_est_min} min · ${usd(r.congestion_est_value)} if busy</span>`
        : `<span class="mute">0 min (${usd(r.congestion_rate)}/min)</span>`;
      return `<tr><td>${dtFmt(r.start)}</td><td>${esc(r.site)}</td><td class="n">${r.kwh.toFixed(1)}</td><td class="n">${r.minutes ? r.minutes + " min" : "—"}</td>
        <td class="n">${r.avg_kw ?? "—"}</td><td class="n">${usd(r.rate)}/kWh</td><td class="n">${cg}</td>
        <td class="n"><b>${usd(r.value + (r.congestion_est_value || 0))}</b>${r.free ? ' <span class="pill ok-t">free</span>' : ""}</td>
        <td class="n athome">${usd(r.home_cost)}</td>
        <td class="n">${r.idle_paid ? `<span class="err-t">${usd(r.idle_paid)}</span>` : '<span class="mute">—</span>'}</td></tr>`;
    }).join("") + "</tbody></table>";
  $("#scMore").style.display = scShown < SC.sessions.length ? "" : "none";
}
$("#scMore").addEventListener("click", () => { scShown += 100; renderScSessions(); });
$("#scSync").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try { await api("api/action", { method: "POST", body: { action: "sc_sync" } }); toast("Syncing with Tesla…"); setTimeout(() => loadSupercharging().catch(() => {}), 6000); }
  catch (err) { toast(err.message); } finally { setTimeout(() => (btn.disabled = false), 6000); }
});

// ---------- alerts & service
let SV = null, alertSev = "all";
const SEV_CLASS = { fault: "err-t", warning: "warn-t", info: "dim" };
async function loadService() {
  if (!S.status) await refreshStatus();
  SV = await api("api/service");
  const faults = SV.alerts.filter((a) => a.severity === "fault").length;
  $("#svcTiles").innerHTML = tile(SV.alerts.length, "car alerts saved") + tile(faults, "faults among them") +
    tile(SV.visits.length, "service visits") + tile(usd(SV.paid_year), `paid for service in ${new Date().getFullYear()}`) + tile(usd(SV.paid_total), "paid for service in total") +
    tile(esc((SV.software[0] || {}).version || S.status?.details?.software || "—"), SV.staged ? `software (update ${esc(SV.staged)} waiting)` : "software installed");
  $("#alertFilters").innerHTML = ["all", "fault", "warning", "info"].map((k) =>
    `<button class="chip ${alertSev === k ? "on" : ""}" data-sev="${k}">${k === "all" ? "All" : k[0].toUpperCase() + k.slice(1) + "s"}</button>`).join("");
  $$("#alertFilters .chip").forEach((c) => c.addEventListener("click", () => { alertSev = c.dataset.sev; loadService(); }));
  const al = SV.alerts.filter((a) => alertSev === "all" || a.severity === alertSev);
  $("#alertTable").innerHTML = al.length ? `<table><thead><tr><th>When</th><th>Kind</th><th>Message</th><th>Code</th></tr></thead><tbody>` +
    al.map((a) => `<tr><td>${dtFmt(a.ts)}</td><td><span class="pill sev ${SEV_CLASS[a.severity]}">${a.severity}</span></td>
      <td style="white-space:normal">${esc((a.text || "").replace("/", " / ")) || '<span class="mute">(no message)</span>'}</td><td class="mono mute">${esc(a.name)}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No alerts saved yet.</div>';
  $("#visitTable").innerHTML = SV.visits.length ? `<table><thead><tr><th>Date</th><th>Where</th><th>Type</th><th>Work done</th><th class="n">Paid</th></tr></thead><tbody>` +
    SV.visits.map((v, i) => {
      const w = v.work.length ? `<ul class="work">${v.work.map((x) => `<li>${esc(x.text)}<span class="pay">${esc((x.pay || "").replace("_", " "))}</span></li>`).join("")}</ul>` : '<span class="mute">details not fetched yet</span>';
      return `<tr class="visit"><td>${esc((v.done || v.in || "").slice(0, 10))}<div class="mute">${esc(v.status || "")}</div></td><td>${esc(v.location || "—")}</td>
        <td>${esc((v.type || "").replace("_", " "))}</td><td style="white-space:normal;min-width:360px">${w}</td>
        <td class="n">${v.cost && v.cost.total != null ? usd(v.cost.total) : "—"}</td></tr>`;
    }).join("") + "</tbody></table>" : '<div class="empty">No service visits.</div>';
  const cur = SV.software[0];
  $("#swBox").innerHTML = `<dl class="kv"><dt>Installed</dt><dd>${esc(cur?.version || S.status?.details?.software || "—")}</dd>
      <dt>Update waiting</dt><dd>${SV.staged ? `<span class="amb-t">${esc(SV.staged)}</span>` : '<span class="dim">none</span>'}</dd></dl>
    <h3 style="margin-top:14px">Versions seen</h3><dl class="kv">${(SV.versions_seen.length ? SV.versions_seen : SV.software.map((x) => ({ version: x.version, first: x.first_seen }))).map((v) =>
      `<dt class="mono">${esc(v.version)}</dt><dd>${dtFmt(v.first, { day: "numeric", month: "short", year: "numeric" })}</dd>`).join("")}</dl>
    ${cur && cur.notes.length ? `<h3 style="margin-top:14px">Release notes (${esc(cur.version)})</h3><div class="notes">${cur.notes.filter((n, i, a) =>
      a.findIndex((m) => m.title === n.title && m.description === n.description) === i).map((n) =>
      `<h4>${esc(n.title)}</h4>${n.subtitle ? `<p class="mute">${esc(n.subtitle)}</p>` : ""}<p>${esc(n.description || "")}</p>`).join("")}</div>` : ""}`;
  const sd = SV.service_data && SV.service_data.data;
  kv($("#accessKv"), [
    ...(SV.drivers.length ? SV.drivers.map((d) => ["Shared with", `${esc([d.first, d.last].filter(Boolean).join(" ") || "—")} <span class="mute">${
      d.access && d.access.hide_private ? "(private data hidden)" : "(full access)"}</span>`]) : [["Shared with", '<span class="dim">nobody</span>']]),
    ["Service status", sd && Object.keys(sd).length ? esc(sd.service_status || JSON.stringify(sd)) : '<span class="ok-t">no open service visit</span>'],
  ]);
  $("#svcSynced").textContent = SV.last_sync ? `synced ${ago(SV.last_sync)}` : "";
}
$("#svcSync").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try { await api("api/action", { method: "POST", body: { action: "extras_sync" } }); toast("Syncing alerts, service and software…"); setTimeout(() => loadService().catch(() => {}), 8000); }
  catch (err) { toast(err.message); } finally { setTimeout(() => (btn.disabled = false), 8000); }
});

// ---------- battery
async function loadBattery() {
  if (!S.status) await refreshStatus();
  const b = await api(`api/battery?days=${S.days}`);
  const ser = b.series;
  const full = ser.filter((p) => p[3]).map((p) => [p[0], U().distance === "km" ? p[3] * 1.609344 : p[3]]);
  const wear = b.range_at_100_first && b.range_at_100_latest ? ((b.range_at_100_latest - b.range_at_100_first) / b.range_at_100_first * 100) : null;
  $("#battTiles").innerHTML = tile(b.range_at_100_latest ? `${dist(b.range_at_100_latest)} ${distU()}` : "—", "range at 100% (latest)") +
    tile(wear != null ? (wear >= 0 ? "+" : "") + wear.toFixed(1) + "%" : "—", `change over ${S.days} days`) +
    tile(b.drain_median != null ? b.drain_median.toFixed(2) + " %/day" : "—", "typical loss while parked") +
    tile(b.awake_by_day.length ? Math.round(b.awake_by_day.reduce((a, d) => a + d[1], 0) / b.awake_by_day.length) + "%" : "—", "time awake (average)");
  lineChart($("#battChart"), ser.map((p) => [p[0], p[1]]), { yMin: 0, yMax: 100, fmtY: (v) => v + "%", fmtTip: (v) => Math.round(v) + "%" });
  lineChart($("#fullChart"), full, { fmtY: (v) => Math.round(v), fmtTip: (v) => Math.round(v) + " " + distU(), step: false });
  barChart($("#drainChart"), b.drains.map((d) => [d.to, d.pct_per_day, new Date(d.to * 1000).toLocaleDateString([], { day: "numeric", month: "short" }),
    `${d.hours} h parked, ending ${dtFmt(d.to)}`]), { fmt: (v) => v.toFixed(2) + " %/day" });
  barChart($("#awakeChart"), b.awake_by_day.map((d) => [d[0], d[1], d[0].slice(5), d[0]]), { fmt: (v) => v + "% awake", yMin1: 100 });
}
$$("[data-days]").forEach((btn) => btn.addEventListener("click", () => {
  S.days = +btn.dataset.days; $$("[data-days]").forEach((x) => x.classList.toggle("on", x === btn)); loadBattery().catch((e) => toast(e.message));
}));

// ---------- activity
const KINDS = { all: "Everything", read: "Readings", wake: "Wakes", manual: "Buttons", alert: "Alerts", mode: "Awake / asleep", service: "Service", supercharging: "Supercharging", settings: "Settings" };
async function loadActivity() {
  const ev = await api("api/activity?days=14");
  $("#actFilters").innerHTML = Object.entries(KINDS).map(([k, v]) => `<button class="chip ${S.actKind === k ? "on" : ""}" data-k="${k}">${v}</button>`).join("");
  $$("#actFilters .chip").forEach((c) => c.addEventListener("click", () => { S.actKind = c.dataset.k; loadActivity(); }));
  const rows = ev.filter((e) => S.actKind === "all" || e.kind === S.actKind);
  $("#feed").innerHTML = rows.length ? rows.map((e) => `<li><time>${dtFmt(e.ts, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>
    <span class="k k-${esc(e.kind)}">${esc(KINDS[e.kind] || e.kind)}</span><span>${esc(e.text)}</span></li>`).join("")
    : '<li class="empty" style="display:block">Nothing in the last 14 days.</li>';
}

// ---------- settings
let SET = null;
async function loadSettings() {
  SET = await api("api/settings");
  const groups = {};
  for (const s of SET.settings) (groups[s.group] = groups[s.group] || []).push(s);
  let html = "";
  for (const [g, items] of Object.entries(groups)) {
    html += `<div class="panel"><h3>${esc(g)}</h3><div class="form">`;
    for (const s of items) {
      if (s.kind === "num") {
        html += `<div class="field"><label for="s-${s.key}">${esc(s.label)}</label><input type="number" id="s-${s.key}" data-key="${s.key}" min="${s.min}" max="${s.max}" step="any" value="${s.value}">
          ${s.help ? `<div class="help">${esc(s.help)}</div>` : ""}</div>`;
      } else if (s.kind === "flags") {
        const names = { unlocked: "Parked & unlocked", tyre: "Low tyre", battery: "Low battery at night", car_alert: "Car faults / service required", budget: "Budget reached", broken: "Poller broken" };
        html += `<div><div class="dim" style="margin-bottom:6px">${esc(s.label)}</div><div class="checks">` +
          Object.entries(s.value).map(([k, v]) => `<label><input type="checkbox" data-flag="${k}" ${v ? "checked" : ""}> ${esc(names[k] || k)}</label>`).join("") + "</div></div>";
      } else if (s.kind === "units") {
        const sel = (k, opts) => `<select data-unit="${k}">${opts.map(([v, l]) => `<option value="${v}" ${s.value[k] === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
        html += `<div class="field"><label>Distance</label>${sel("distance", [["mi", "miles"], ["km", "kilometres"]])}</div>
          <div class="field"><label>Tyre pressure</label>${sel("pressure", [["psi", "psi"], ["bar", "bar"]])}</div>
          <div class="field"><label>Temperature</label>${sel("temp", [["F", "°F"], ["C", "°C"]])}</div>`;
      }
    }
    html += "</div></div>";
  }
  html += `<div class="panel"><h3>Remote access</h3><div class="form">
    <p class="dim" style="margin:0">On your home network this page opens without a password. Away from home (through the gateway's tunnel) it asks for this password.
    ${SET.password_set ? '<span class="ok-t">A password is set.</span>' : '<span class="warn-t">No password set: the page cannot be opened from outside.</span>'}</p>
    ${SET.can_set_password ? `<div class="field"><label for="pw1">New password (10+ characters)</label><input type="password" id="pw1" autocomplete="new-password"></div>
      <div class="actions"><button class="rb" id="btnPw">Set password</button>${SET.password_set ? '<button class="rb" id="btnPwOff">Turn off remote access</button>' : ""}</div>`
      : '<p class="mute">The password can only be changed from home.</p>'}
    </div></div>`;
  $("#setGrid").innerHTML = html;
  $("#costText").textContent = `Background checks alone cost about ${money(SET.baseline_cost_month)} a month at this setting (if Tesla bills them).`;
  $("#btnPw")?.addEventListener("click", async () => {
    try { SET = await api("api/password", { method: "POST", body: { password: $("#pw1").value } }); toast("Password set. Remote access is on."); loadSettings(); } catch (e) { toast(e.message); }
  });
  $("#btnPwOff")?.addEventListener("click", async () => {
    if (!confirm("Remove the password? The page will no longer open from outside your home network.")) return;
    try { await api("api/password", { method: "POST", body: { clear: true } }); toast("Remote access turned off."); loadSettings(); } catch (e) { toast(e.message); }
  });
}
$("#btnSave").addEventListener("click", async () => {
  const values = {};
  $$("#setGrid input[data-key]").forEach((i) => (values[i.dataset.key] = i.value));
  const flags = {}; $$("#setGrid input[data-flag]").forEach((i) => (flags[i.dataset.flag] = i.checked)); values.ALERT_ON = flags;
  const units = {}; $$("#setGrid select[data-unit]").forEach((s) => (units[s.dataset.unit] = s.value)); values.UNITS = units;
  try { await api("api/settings", { method: "POST", body: { values } }); S.status = null; toast("Saved. Takes effect on the next check."); loadSettings(); }
  catch (e) { toast(e.message); }
});
$("#btnTestMail").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try { const r = await api("api/test_email", { method: "POST" }); toast(r.result === "sent" ? "Test email sent." : "Email: " + r.result); }
  catch (err) { toast(err.message); } finally { btn.disabled = false; }
});

// ---------- go
route();
setInterval(() => { if (document.hidden) return; if (S.view === "dash") loadDash().catch(() => {}); else refreshStatus().catch(() => {}); }, 60000);
