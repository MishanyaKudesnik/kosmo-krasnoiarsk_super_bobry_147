"use strict";

/* =============================================================================
   FOREST BEAVER — фронтенд спутниковой верификации лесных углеродных проектов.
   Вся логика расчёта — на сервере (app/): здесь только представление реальных
   значений из ответа /analysis, /overview, /forecast, /control-area.
   ============================================================================= */

const API = "";
const YEAR_MIN = 2019;
const YEAR_MAX = 2024;
const LS_ROLE = "fb_role";
const LS_NAME = "fb_name";
const MAX_AREA_KM2 = 20;

const BLANK_STYLE = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#f2f3f7" } }] };

// Подложка. Тайлы tile.openstreetmap.org отдаёт волонтёрская инфраструктура OSM, и приложения,
// которые ходят туда напрямую, она блокирует по своей tile usage policy (в ответ приходят
// картинки «Access blocked»). Поэтому подложка берётся у провайдеров, которые такое использование
// разрешают и не требуют ключа, с обязательной атрибуцией. Если провайдер не отвечает, карта
// переключается на следующего, а в крайнем случае остаётся без подложки: контуры, потери и
// портфель видны всегда — подложка нужна только для ориентирования на местности.
const BASEMAPS = [
  {
    id: "esri-topo",
    tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"],
    maxzoom: 19, attribution: "Esri, HERE, Garmin, USGS, © OpenStreetMap contributors",
  },
  {
    id: "esri-imagery",  // запасная: спутниковый снимок, тот же сервис, другой слой
    tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
    maxzoom: 19, attribution: "Esri, Maxar, Earthstar Geographics",
  },
];
const BASEMAP_ERRORS_TO_SWITCH = 6;  // единичная потерянная плитка — не повод менять провайдера

const INK = "#0b0b12", ACCENT = "#6d4aff", ACCENT_DEEP = "#4a28e0";
const RED = "#c4362a", GREEN = "#1c7a50", AMBER = "#9b6a14";
const LEVEL_COLOR = { low: GREEN, medium: AMBER, high: RED };
const LEVEL_RU = { low: "низкий", medium: "средний", high: "высокий" };
const SEVERITY_RU = { critical: "Критично", warning: "Внимание", info: "К сведению", positive: "Хорошо" };
const STATUS_RU = { pass: "пройдено", warn: "внимание", fail: "не пройдено", info: "сведения" };
const CHECK_MARK = { pass: "✓", warn: "!", fail: "×", info: "i" };

// Регионы для быстрого выбора: центры проверены — данные ESA CCI и Hansen GFC для их тайлов доступны.
const PRESETS = [
  { group: "Россия", name: "Карелия", lat: 62.75, lon: 33.65 },
  { group: "Россия", name: "Архангельская область", lat: 63.9, lon: 42.3 },
  { group: "Россия", name: "Ленинградская область", lat: 59.4, lon: 32.1 },
  { group: "Россия", name: "Республика Коми", lat: 62.2, lon: 51.8 },
  { group: "Россия", name: "Ярославская область", lat: 57.8, lon: 39.3 },
  { group: "Россия", name: "Пермский край", lat: 58.9, lon: 56.6 },
  { group: "Россия", name: "Свердловская область", lat: 58.5, lon: 60.7 },
  { group: "Россия", name: "Красноярский край (Приангарье)", lat: 58.2, lon: 94.6 },
  { group: "Россия", name: "Иркутская область", lat: 56.9, lon: 102.9 },
  { group: "Россия", name: "Хабаровский край", lat: 50.7, lon: 136.8 },
  { group: "Россия", name: "Приморский край", lat: 45.4, lon: 134.1 },
  { group: "Мир", name: "Швеция (бореальный лес)", lat: 63.3, lon: 16.4 },
  { group: "Мир", name: "Канада, Онтарио", lat: 49.6, lon: -85.2 },
  { group: "Мир", name: "Бразилия, Амазония", lat: -3.1, lon: -60.3 },
  { group: "Мир", name: "Индонезия, Борнео", lat: 0.9, lon: 114.2 },
];

const ROLE_KEY_EN = { verifier: "VERIFIER", investor: "INVESTOR", owner: "PROJECT OWNER" };
const ROLE_TABS = {
  verifier: ["checklist", "changes", "data", "uncertainty", "evidence", "calc"],
  investor: ["risks", "forecast", "summary"],
  owner: ["recommendations", "dynamics", "changes"],
};
const ROLE_VIEW = { verifier: "evidence", investor: "result", owner: "result" };
const ROLE_FALLBACK = {
  verifier: { title: "Верификатор", description: "Проверяет заявленный результат проекта.", focus: [], landing_tab: "checklist" },
  investor: { title: "Инвестор", description: "Оценивает результат проекта и риски.", focus: [], landing_tab: "risks" },
  owner: { title: "Владелец проекта", description: "Задаёт территорию и хочет улучшить результат.", focus: [], landing_tab: "recommendations" },
};

const FLOW = ["POLYGON", "SATELLITE", "CHANGE", "BIOMASS", "CARBON", "CO₂e", "EVIDENCE"];

const state = {
  role: null, name: "", roles: ROLE_FALLBACK, official: [], external: [], overview: null, overviewLoading: false,
  sel: null, result: null, body: null, control: {}, showAllRoles: false,
  mode: null, drawPoints: [], squareCenter: null, dragVertex: null,
  map: null, mapReady: false, basemap: null, chart: null,
  view: "map", tab: { result: "summary", evidence: "evidence", audit: "calc" },
  chainNode: null, layers: { basemap: true, grid: true, portfolio: true, loss: true },
  rutTabsSeen: {},
};

/* ------------------------------------------------------------------ утилиты */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toLocaleString("ru-RU", { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const signed = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n) ? "—" : (n > 0 ? "+" : "") + fmt(n, d));
const role = () => (state.role && state.roles[state.role] ? state.role : "owner");
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

async function api(path, opts) {
  const resp = await fetch(API + path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* пустое тело */ }
  if (!resp.ok) {
    const detail = data && data.detail !== undefined
      ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
      : (data && data.error && data.error.message ? data.error.message : `HTTP ${resp.status}`);
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return data;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "run-status" + (kind ? " " + kind : "");
}

function areaKm2(geometry) {
  const ring = geometry.type === "Polygon" ? geometry.coordinates[0] : geometry.coordinates.flat().map((p) => p[0]).flat();
  const lat0 = ring.reduce((s, p) => s + p[1], 0) / ring.length;
  const kx = 111.32 * Math.cos((lat0 * Math.PI) / 180), ky = 110.57;
  let sum = 0;
  for (let i = 0; i < ring.length - 1; i++) sum += ring[i][0] * kx * ring[i + 1][1] * ky - ring[i + 1][0] * kx * ring[i][1] * ky;
  return Math.abs(sum) / 2;
}

function squareAround(lng, lat, sideKm) {
  const half = sideKm / 2;
  const dLat = half / 111.32, dLon = half / (111.32 * Math.cos((lat * Math.PI) / 180));
  const r = (v) => Math.round(v * 1e5) / 1e5;
  const [w, e, s, n] = [r(lng - dLon), r(lng + dLon), r(lat - dLat), r(lat + dLat)];
  return { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] };
}

function ringOf(geometry) {
  if (!geometry) return [];
  return geometry.type === "Polygon" ? geometry.coordinates[0] : geometry.coordinates.flatMap((p) => p[0]);
}
function centroidOf(geometry) {
  const ring = ringOf(geometry);
  if (!ring.length) return [0, 0];
  return [ring.reduce((s, p) => s + p[0], 0) / ring.length, ring.reduce((s, p) => s + p[1], 0) / ring.length];
}
const dms = (v, pos, neg) => `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;

/* ---------------------------------------------------- топографический мотив */

// Детерминированные «горизонтали»: тот же визуальный язык, что на топокартах,
// и одновременно метафора продукта — вложенные контуры вокруг участка.
function contourPath(cx, cy, r, wob, seed, steps = 64) {
  let d = "";
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * Math.PI * 2;
    const k = Math.sin(a * 3 + seed) * 0.6 + Math.sin(a * 5 + seed * 1.7) * 0.28 + Math.sin(a * 2 - seed * 0.8) * 0.4;
    const rr = r + k * wob;
    const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr * 0.72;
    d += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
  }
  return d + "Z";
}
function contourField(cx, cy, count, step, wob, seed, opts = {}) {
  const stroke = opts.stroke || "#d9d4f2", base = opts.base || 26;
  let out = "";
  for (let i = 0; i < count; i++) {
    const op = opts.fade ? (0.14 + 0.5 * (1 - i / count)) : 0.5;
    out += `<path d="${contourPath(cx, cy, base + i * step, wob * (0.5 + i / count), seed + i * 0.45)}" fill="none" stroke="${stroke}" stroke-width="${opts.w || 1}" opacity="${op.toFixed(2)}"/>`;
  }
  return out;
}

const ICON = {
  polygon: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 5.5 8 2l5 3.5v5L8 14l-5-3.5v-5Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  satellite: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="2.2" stroke="currentColor" stroke-width="1.3"/><path d="M3.1 3.1a7 7 0 0 0 0 9.8M12.9 3.1a7 7 0 0 1 0 9.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  change: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 11.5 6 7l3 2.6 5-6.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M14 7V3.5h-3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  biomass: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 14V8.5M8 8.5 4.6 5.2M8 8.5l3.4-3.3M8 5.4 5.6 3M8 5.4 10.4 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  carbon: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.6" stroke="currentColor" stroke-width="1.3"/><path d="M10.2 6.2a2.9 2.9 0 1 0 0 3.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  co2: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4.6 10.5a2.6 2.6 0 1 1 1.2-4.9A3.4 3.4 0 0 1 12.3 6a2.3 2.3 0 0 1-.4 4.5H4.6Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  evidence: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3.5 2.5h6L13 6v7.5H3.5v-11Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M9.2 2.6V6H12.8M5.8 9.6l1.5 1.5 3-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  arrow: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 7h8M7.6 3.6 11 7l-3.4 3.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  down: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 3v9M4.6 8.6 8 12l3.4-3.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chev: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3.5 5.2 7 8.8l3.5-3.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  map: '<svg width="42" height="42" viewBox="0 0 42 42" fill="none"><path d="M5 11.5 16 7l10 4.5L37 7v23.5L26 35l-10-4.5L5 35V11.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M16 7v23.5M26 11.5V35" stroke="currentColor" stroke-width="1.6"/></svg>',
};

/* =============================================================================
   ENTRY — стартовый экран и его анимация
   POLYGON → SATELLITE → CHANGE → BIOMASS → CARBON → CO₂e → EVIDENCE
   ============================================================================= */

const ENTRY_STAGES = [
  { at: 0,    label: "ORBIT ACQUISITION" },
  { at: 900,  label: "FOREST CANOPY" },
  { at: 1500, label: "TERRAIN CONTOURS" },
  { at: 2000, label: "POLYGON LOCKED" },
  { at: 2600, label: "DATA SIGNAL" },
  { at: 3200, label: "EVIDENCE READY" },
];

function entryAnimation() {
  const svg = $("entry-anim");
  if (!svg) return;
  const trees = [];
  for (let i = 0; i < 26; i++) {
    const x = 40 + (i % 13) * 42 + (i > 12 ? 20 : 0);
    const y = 486 + (i > 12 ? 40 : 0) + ((i * 37) % 11);
    const h = 40 + ((i * 53) % 26);
    trees.push(`<g class="rise-in" style="--del:${(0.62 + i * 0.022).toFixed(2)}s">
      <path d="M${x} ${y} L${x - h * 0.3} ${y + h * 0.42} M${x} ${y} L${x + h * 0.3} ${y + h * 0.42}" stroke="#b9b1e0" stroke-width="1.1" stroke-linecap="round" opacity=".55"/>
      <path d="M${x} ${y - h} L${x - h * 0.31} ${y} L${x + h * 0.31} ${y} Z" fill="${i % 4 === 0 ? "#cfc6ef" : "#ddd8f0"}" stroke="#a99ddd" stroke-width=".8"/>
      <path d="M${x} ${y + h * 0.42} v${h * 0.18}" stroke="#a99ddd" stroke-width="1.4" stroke-linecap="round"/>
    </g>`);
  }

  svg.innerHTML = `
    <defs>
      <linearGradient id="skyG" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#ffffff"/><stop offset=".62" stop-color="#faf9ff"/><stop offset="1" stop-color="#f3f0fc"/>
      </linearGradient>
      <linearGradient id="beamG" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#6d4aff" stop-opacity=".30"/><stop offset="1" stop-color="#6d4aff" stop-opacity="0"/>
      </linearGradient>
      <clipPath id="polyClip"><path d="M198 392 L392 356 L438 470 L268 512 Z"/></clipPath>
    </defs>

    <rect width="600" height="700" fill="url(#skyG)"/>

    <!-- горизонтали рельефа -->
    <g class="fade-in" style="--del:1.45s">${contourField(320, 430, 9, 26, 15, 1.2, { stroke: "#cdc4ee", fade: true, base: 44, w: 1 })}</g>

    <!-- орбита -->
    <g>
      <ellipse cx="300" cy="300" rx="238" ry="96" fill="none" stroke="#d6cffa" stroke-width="1.2"
               class="draw-in" style="--len:1100;--dur:1.5s" transform="rotate(-18 300 300)"/>
      <ellipse cx="300" cy="300" rx="176" ry="66" fill="none" stroke="#e3ddfb" stroke-width="1" stroke-dasharray="3 6"
               class="fade-in" style="--del:.5s" transform="rotate(-18 300 300)"/>
      <g class="fade-in" style="--del:.25s">
        <g>
          <rect x="-9" y="-5" width="18" height="10" rx="2.4" fill="#0b0b12"/>
          <rect x="-19" y="-3.2" width="8" height="6.4" rx="1.2" fill="#6d4aff"/>
          <rect x="11" y="-3.2" width="8" height="6.4" rx="1.2" fill="#6d4aff"/>
          <animateMotion dur="7s" repeatCount="indefinite" rotate="auto"
            path="M300 204 a238 96 0 1 1 0 192 a238 96 0 1 1 0 -192"
            transform="rotate(-18 300 300)"/>
        </g>
      </g>
    </g>

    <!-- луч съёмки -->
    <g class="fade-in" style="--del:2.35s">
      <path d="M318 262 L214 386 L430 346 Z" fill="url(#beamG)"/>
    </g>

    <!-- лес -->
    <g>${trees.join("")}</g>

    <!-- полигон участка -->
    <g class="fade-in" style="--del:1.95s">
      <path d="M198 392 L392 356 L438 470 L268 512 Z" fill="#6d4aff" fill-opacity=".10"/>
      <path d="M198 392 L392 356 L438 470 L268 512 Z" fill="none" stroke="#6d4aff" stroke-width="2"
            class="draw-in" style="--len:760;--dur:1.1s;--del:1.95s"/>
      <g fill="#ffffff" stroke="#6d4aff" stroke-width="2">
        <circle cx="198" cy="392" r="4.6"/><circle cx="392" cy="356" r="4.6"/>
        <circle cx="438" cy="470" r="4.6"/><circle cx="268" cy="512" r="4.6"/>
      </g>
      <g clip-path="url(#polyClip)">
        <rect x="180" y="420" width="280" height="2.4" fill="#6d4aff" opacity=".85" class="scan-line" style="--del:2.5s"/>
      </g>
    </g>

    <!-- сигнал данных -->
    <g class="fade-in" style="--del:2.75s">
      <path d="M268 566 h34 l14 -26 l20 52 l16 -38 l14 12 h64" fill="none" stroke="#6d4aff" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round" class="draw-in" style="--len:290;--dur:.8s;--del:2.8s"/>
    </g>

    <!-- evidence -->
    <g class="rise-in" style="--del:3.25s">
      <rect x="228" y="604" width="150" height="40" rx="10" fill="#0b0b12"/>
      <path d="M248 624 l7 7 l13 -14" fill="none" stroke="#7de2b0" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
      <text x="280" y="629" fill="#ffffff" font-family="ui-monospace, monospace" font-size="12.5" letter-spacing="2.4">EVIDENCE</text>
    </g>`;

  const label = $("entry-anim-label");
  ENTRY_STAGES.forEach((s) => setTimeout(() => { if (label) label.textContent = s.label; }, s.at));
}

function renderEntryFlow() {
  const box = $("entry-flow");
  if (!box) return;
  box.innerHTML = FLOW.map((f, i) =>
    `<span data-fi="${i}">${esc(f)}</span>${i < FLOW.length - 1 ? `<i>→</i>` : ""}`).join("");
  let i = 0;
  setInterval(() => {
    box.querySelectorAll("span").forEach((el, k) => el.classList.toggle("hot", k === i));
    i = (i + 1) % FLOW.length;
  }, 620);
}

/* ------------------------------------------------------------------- роль */

function loadStored(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function store(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* приватный режим */ } }

function roleCardsHTML(selected) {
  return Object.entries(state.roles).map(([key, r]) => `
    <label class="role-opt ${key === selected ? "selected" : ""}" data-role="${key}">
      <div class="rk">${esc(ROLE_KEY_EN[key] || key.toUpperCase())}</div>
      <div class="rt">${esc(r.title)}</div>
      <div class="rd">${esc(r.description)}</div>
      <ul>${(r.focus || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    </label>`).join("");
}

function showRoleScreen(canClose) {
  const screen = $("role-screen");
  $("role-cards").innerHTML = roleCardsHTML(state.role);
  $("role-cards").querySelectorAll(".role-opt").forEach((el) => el.addEventListener("click", () => pickRole(el.dataset.role)));
  $("role-screen-close").classList.toggle("hidden", !canClose);
  $("entry-name").value = state.name || "";
  screen.classList.remove("hidden", "leaving");
  entryAnimation();
}

function pickRole(key) {
  state.role = key;
  $("role-cards").querySelectorAll(".role-opt").forEach((el) => el.classList.toggle("selected", el.dataset.role === key));
}

function closeEntry() {
  const screen = $("role-screen");
  screen.classList.add("leaving");
  setTimeout(() => screen.classList.add("hidden"), 400);
}

function enterApp(demo) {
  if (!state.role) { pickRole("owner"); }
  state.name = ($("entry-name").value || "").trim().slice(0, 60);
  store(LS_ROLE, state.role);
  store(LS_NAME, state.name);
  applyRole();
  closeEntry();
  if (demo) {
    setTimeout(() => {
      openSheet(true);
      const first = (state.overview && state.overview.regions[0]) || state.official[0];
      if (first) openRegion(first.aoi_id);
      else setStatus("Демо-участки ещё считаются — секунду…");
    }, 460);
    rutSay(`<b>Бобёр РУТ:</b> Открываю демо: участок из кейса считается прямо сейчас — числа настоящие, из движка.`, 8000);
  } else {
    rutSay(`Вы вошли как <b>${esc(state.roles[state.role].title)}</b>. Выберите участок слева или нарисуйте свой контур на карте.`, 8000);
  }
}

function applyRole() {
  const key = state.role;
  $("role-chip").classList.toggle("hidden", !key);
  if (!key) return;
  const r = state.roles[key] || ROLE_FALLBACK[key];
  $("user-role").textContent = r.title;
  $("user-name").textContent = state.name || (ROLE_KEY_EN[key] || "");
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("role-hint", (ROLE_TABS[key] || []).includes(b.dataset.tab)));
  $("role-card").innerHTML = `
    <div class="rail-head"><span class="step">i</span><h2>Ваша роль</h2></div>
    <div style="font-size:13px;font-weight:600;margin-bottom:4px">${esc(r.title)}</div>
    <p class="hint" style="margin-bottom:8px">${esc(r.description)}</p>
    <div class="eyebrow" style="margin-bottom:5px">Смотреть в первую очередь</div>
    <ul class="hint" style="margin:0;padding-left:16px">${(r.focus || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    <p class="fine" style="margin-top:9px">Разделы с фиолетовой точкой отмечены для вашей роли.</p>`;
  if (state.result) renderAll(state.result, false);
  if (state.overview) renderPortfolio();
}

function initEntry() {
  $("change-role-btn").addEventListener("click", () => showRoleScreen(true));
  $("role-screen-close").addEventListener("click", closeEntry);
  $("start-btn").addEventListener("click", () => enterApp(false));
  $("demo-btn").addEventListener("click", () => enterApp(true));
  $("entry-name").addEventListener("keydown", (e) => { if (e.key === "Enter") enterApp(false); });
  renderEntryFlow();
}

/* =============================================================================
   КАРТА — инструмент анализа: слои, сетка, масштаб, рисование и правка контура
   ============================================================================= */

function addBasemap(map, index) {
  state.basemap = { index, errors: 0 };
  if (map.getLayer("basemap")) map.removeLayer("basemap");
  if (map.getSource("basemap")) map.removeSource("basemap");
  if (index >= BASEMAPS.length) { basemapNote("Подложка недоступна — карта работает на локальных контурах (Natural Earth)."); return; }

  const src = BASEMAPS[index];
  map.addSource("basemap", { type: "raster", tiles: src.tiles, tileSize: 256, maxzoom: src.maxzoom, attribution: src.attribution });
  const below = map.getLayer("grid-line") ? "grid-line" : (map.getLayer("portfolio-fill") ? "portfolio-fill" : undefined);
  map.addLayer({ id: "basemap", type: "raster", source: "basemap", paint: { "raster-opacity": state.layers.basemap ? 1 : 0 } }, below);
  basemapNote("");
}

function basemapNote(text) {
  let note = $("map-note");
  if (!text) { if (note) note.remove(); return; }
  if (!note) {
    note = document.createElement("div");
    note.id = "map-note";
    note.className = "map-note";
    $("map").appendChild(note);
  }
  note.textContent = text;
}

function onBasemapError(map, event) {
  if (!state.basemap || event.sourceId !== "basemap") return;
  if (++state.basemap.errors < BASEMAP_ERRORS_TO_SWITCH) return;
  const next = state.basemap.index + 1;
  basemapNote(next < BASEMAPS.length ? "Подложка не отвечает — переключаюсь на запасную…" : "");
  addBasemap(map, next);
}

const GRID_STEPS = [30, 15, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01, 0.005];
function graticule(map) {
  const z = map.getZoom();
  const step = GRID_STEPS[clamp(Math.round(z * 0.78), 0, GRID_STEPS.length - 1)];
  const b = map.getBounds();
  const w = Math.floor(b.getWest() / step) * step, e = Math.ceil(b.getEast() / step) * step;
  const s = Math.floor(b.getSouth() / step) * step, n = Math.ceil(b.getNorth() / step) * step;
  const f = [];
  if ((e - w) / step < 400) for (let x = w; x <= e; x += step) f.push(featureOf({ type: "LineString", coordinates: [[x, s], [x, n]] }));
  if ((n - s) / step < 400) for (let y = s; y <= n; y += step) f.push(featureOf({ type: "LineString", coordinates: [[w, y], [e, y]] }));
  return FC(f);
}

function updateHud() {
  const map = state.map;
  if (!map) return;
  const c = map.getCenter();
  $("hud-lat").textContent = dms(c.lat, "N", "S");
  $("hud-lon").textContent = dms(c.lng, "E", "W");
  $("hud-zoom").textContent = map.getZoom().toFixed(1);
  // масштабная линейка: подбираем круглое расстояние под ~70–120 px
  const mPerPx = 156543.03392 * Math.cos((c.lat * Math.PI) / 180) / Math.pow(2, map.getZoom());
  const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000];
  let pick = nice[0];
  for (const v of nice) { if (v / mPerPx <= 120) pick = v; }
  $("hud-scale-bar").style.width = Math.max(28, Math.round(pick / mPerPx)) + "px";
  $("hud-scale-label").textContent = pick >= 1000 ? `${pick / 1000} км` : `${pick} м`;
  if (state.layers.grid) setSource("grid", graticule(map));
}

function initMap() {
  if (typeof maplibregl === "undefined") {
    $("map").innerHTML = `<div class="placeholder">Карта недоступна (не загрузилась библиотека MapLibre). Расчёты работают без карты.</div>`;
    return;
  }
  const map = new maplibregl.Map({ container: "map", style: BLANK_STYLE, center: [45, 58], zoom: 3, attributionControl: { compact: true } });
  state.map = map;
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.on("error", (e) => onBasemapError(map, e));

  map.on("load", () => {
    // Контуры суши лежат в проекте (Natural Earth, public domain) и рисуются ВСЕГДА:
    // даже без интернета и без подложки видно, где находится участок.
    map.addSource("land", { type: "geojson", data: "./vendor/world-110m.geojson" });
    map.addLayer({ id: "land-fill", type: "fill", source: "land", paint: { "fill-color": "#eceaf6" } });
    map.addLayer({ id: "land-line", type: "line", source: "land", paint: { "line-color": "#d8d4ea", "line-width": 0.8 } });

    const empty = FC([]);
    map.addSource("grid", { type: "geojson", data: empty });
    map.addLayer({ id: "grid-line", type: "line", source: "grid", paint: { "line-color": "#0b0b12", "line-opacity": 0.09, "line-width": 0.7 } });

    const levelColor = ["match", ["get", "level"], "low", LEVEL_COLOR.low, "medium", LEVEL_COLOR.medium, "high", LEVEL_COLOR.high, ACCENT];
    map.addSource("portfolio", { type: "geojson", data: empty });
    map.addLayer({ id: "portfolio-fill", type: "fill", source: "portfolio", paint: { "fill-color": levelColor, "fill-opacity": 0.12 } });
    map.addLayer({ id: "portfolio-line", type: "line", source: "portfolio", paint: { "line-color": levelColor, "line-width": 1.4, "line-opacity": 0.8 } });
    map.addSource("portfolio-pts", { type: "geojson", data: empty });
    map.addLayer({
      id: "portfolio-pts", type: "circle", source: "portfolio-pts",
      paint: { "circle-radius": 7, "circle-color": "#ffffff", "circle-stroke-color": levelColor, "circle-stroke-width": 3 },
    });

    map.addSource("selected", { type: "geojson", data: empty });
    map.addLayer({ id: "selected-halo", type: "line", source: "selected", paint: { "line-color": ACCENT, "line-width": 10, "line-opacity": 0.12, "line-blur": 3 } });
    map.addLayer({ id: "selected-fill", type: "fill", source: "selected", paint: { "fill-color": ACCENT, "fill-opacity": 0.1 } });
    map.addLayer({ id: "selected-line", type: "line", source: "selected", paint: { "line-color": ACCENT, "line-width": 2.2 } });

    map.addSource("loss", { type: "geojson", data: empty });
    map.addLayer({ id: "loss-fill", type: "fill", source: "loss", paint: { "fill-color": RED, "fill-opacity": 0.5 } });
    map.addLayer({ id: "loss-line", type: "line", source: "loss", paint: { "line-color": RED, "line-width": 1.2 } });

    map.addSource("draw", { type: "geojson", data: empty });
    map.addLayer({ id: "draw-line", type: "line", source: "draw", filter: ["==", "$type", "LineString"], paint: { "line-color": ACCENT, "line-width": 2, "line-dasharray": [2, 1.4] } });

    map.addSource("verts", { type: "geojson", data: empty });
    map.addLayer({ id: "verts-pt", type: "circle", source: "verts", paint: { "circle-radius": 5.5, "circle-color": "#ffffff", "circle-stroke-color": ACCENT, "circle-stroke-width": 2.2 } });

    map.on("click", onMapClick);
    map.on("dblclick", onMapDblClick);
    map.on("move", updateHud);
    map.on("moveend", updateHud);
    for (const layer of ["portfolio-pts", "portfolio-fill"]) {
      map.on("mouseenter", layer, () => { if (!state.mode) map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => { if (!state.mode) map.getCanvas().style.cursor = ""; });
    }
    initVertexDrag(map);
    addBasemap(map, 0);
    state.mapReady = true;
    updateHud();
    drawPortfolioOnMap();
    if (state.sel) drawSelection();
    if (state.result) drawLoss(state.result.change_detection);
  });
}

function setSource(id, data) {
  if (state.mapReady && state.map.getSource(id)) state.map.getSource(id).setData(data);
}
const featureOf = (geometry, props = {}) => ({ type: "Feature", geometry, properties: props });
const FC = (features) => ({ type: "FeatureCollection", features });

function setLayerVisible(ids, on) {
  if (!state.mapReady) return;
  for (const id of ids) if (state.map.getLayer(id)) state.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
}

function initLayerControls() {
  $("layers-toggle").addEventListener("click", () => {
    const body = $("layers-body");
    const open = !body.classList.toggle("hidden");
    $("layers-chev").textContent = open ? "▾" : "▸";
  });
  document.querySelectorAll(".lrow").forEach((row) => {
    row.addEventListener("click", () => {
      const key = row.dataset.layer;
      const on = !state.layers[key];
      state.layers[key] = on;
      row.classList.toggle("on", on);
      if (key === "basemap") { if (state.mapReady && state.map.getLayer("basemap")) state.map.setPaintProperty("basemap", "raster-opacity", on ? 1 : 0); }
      if (key === "grid") { setLayerVisible(["grid-line"], on); if (on) updateHud(); }
      if (key === "portfolio") setLayerVisible(["portfolio-fill", "portfolio-line", "portfolio-pts"], on);
      if (key === "loss") setLayerVisible(["loss-fill", "loss-line"], on);
    });
  });
}

function drawPortfolioOnMap() {
  if (!state.mapReady || !state.overview) return;
  const regions = state.overview.regions;
  setSource("portfolio", FC(regions.map((r) => featureOf(r.geometry, { id: r.aoi_id, level: r.risk.overall_level }))));
  setSource("portfolio-pts", FC(regions.map((r) => featureOf({ type: "Point", coordinates: centroidOf(r.geometry) }, { id: r.aoi_id, level: r.risk.overall_level }))));
  if (!state.map._portfolioBound) {
    state.map._portfolioBound = true;
    for (const layer of ["portfolio-pts", "portfolio-fill"]) {
      state.map.on("click", layer, (e) => {
        if (state.mode) return;
        const f = e.features && e.features[0];
        const r = f && state.overview.regions.find((x) => x.aoi_id === f.properties.id);
        if (!r) return;
        if (state.popup) state.popup.remove();
        state.popup = new maplibregl.Popup({ closeButton: true, maxWidth: "290px" }).setLngLat(e.lngLat)
          .setHTML(`<div class="pop-title">${esc(r.name)}</div>
            <div class="pop-meta">${esc(r.region).toUpperCase()} · ${fmt(r.area_ha, 0)} ГА · РИСК ${esc(LEVEL_RU[r.risk.overall_level]).toUpperCase()}</div>
            <div class="pop-txt">${esc(r.headline)}</div>
            <button class="pop-btn" data-open="${esc(r.aoi_id)}">Запустить верификацию</button>`).addTo(state.map);
      });
    }
  }
}

function selectionGeometry() {
  if (!state.sel) return null;
  return state.sel.geometry || (state.official.concat(state.external).find((a) => a.aoi_id === state.sel.aoiId) || {}).geometry || null;
}

function fitToSelection(duration = 700) {
  const geometry = selectionGeometry();
  if (!state.mapReady || !geometry) return;
  const pts = ringOf(geometry);
  const b = pts.reduce((bb, c) => bb.extend(c), new maplibregl.LngLatBounds(pts[0], pts[0]));
  state.map.fitBounds(b, { padding: { top: 70, bottom: 110, left: 70, right: 220 }, maxZoom: 12.5, duration });
}

function drawSelection() {
  const geometry = selectionGeometry();
  if (!geometry) { setSource("selected", FC([])); setSource("verts", FC([])); return; }
  setSource("selected", featureOf(geometry));
  setSource("verts", state.mode === "edit"
    ? FC(ringOf(geometry).slice(0, -1).map((p, i) => featureOf({ type: "Point", coordinates: p }, { i })))
    : FC([]));
  if (state.sel.fit !== false) fitToSelection();
}

function drawLoss(cd) {
  setSource("loss", cd && cd.loss_footprint_geojson ? featureOf(cd.loss_footprint_geojson) : FC([]));
}

/* ------------------------------------------------------- рисование и правка */

function setMode(mode) {
  state.mode = mode;
  $("place-btn").classList.toggle("on", mode === "place");
  $("draw-btn").classList.toggle("on", mode === "draw");
  $("edit-btn").classList.toggle("on", mode === "edit");
  $("draw-btn").textContent = mode === "draw" ? "Завершить" : "Нарисовать";
  drawHint(mode);
  if (state.mapReady) {
    state.map.getCanvas().style.cursor = mode === "edit" ? "" : mode ? "crosshair" : "";
    if (mode === "draw") state.map.doubleClickZoom.disable(); else state.map.doubleClickZoom.enable();
  }
  if (mode !== "draw") { state.drawPoints = []; drawDrawing(); }
  drawSelection();
}

function drawHint(mode) {
  let el = $("draw-hint");
  const text = {
    place: `Кликните по карте — вокруг точки появится квадрат <kbd>${$("square-size") ? $("square-size").value : 3}×${$("square-size") ? $("square-size").value : 3} км</kbd>`,
    draw: `Кликайте — добавляется вершина · <kbd>двойной клик</kbd> замкнуть · <kbd>Backspace</kbd> убрать · <kbd>Esc</kbd> отмена`,
    edit: `Тяните вершины мышью · <kbd>Alt + клик</kbd> удалить вершину · <kbd>Esc</kbd> выйти`,
  }[mode];
  if (!text) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "draw-hint";
    el.className = "draw-hint";
    $("map").parentElement.appendChild(el);
  }
  el.innerHTML = text;
}

function drawDrawing() {
  const f = [];
  if (state.drawPoints.length >= 2) f.push(featureOf({ type: "LineString", coordinates: state.drawPoints }));
  setSource("draw", FC(f));
  if (state.mode === "draw") setSource("verts", FC(state.drawPoints.map((p, i) => featureOf({ type: "Point", coordinates: p }, { i }))));
}

function onMapClick(e) {
  if (state.mode === "place") {
    setSquareAt(e.lngLat.lng, e.lngLat.lat, { fit: false });
    setMode(null);
  } else if (state.mode === "draw") {
    const p = [e.lngLat.lng, e.lngLat.lat];
    if (state.drawPoints.length >= 3) {
      const first = state.drawPoints[0];
      const a = state.map.project(first), b = state.map.project(p);
      if (Math.hypot(a.x - b.x, a.y - b.y) < 12) { finishDrawing(); return; }
    }
    state.drawPoints.push(p);
    drawDrawing();
    setStatus(`Вершин: ${state.drawPoints.length}${state.drawPoints.length >= 3 ? " — двойной клик или клик по первой точке, чтобы замкнуть" : ""}`);
  }
}

function onMapDblClick(e) {
  if (state.mode !== "draw") return;
  e.preventDefault();
  finishDrawing();
}

function finishDrawing() {
  if (state.drawPoints.length < 3) { setStatus("Нужно минимум 3 вершины", "error"); return; }
  const ring = [...state.drawPoints, state.drawPoints[0]].map((p) => [Math.round(p[0] * 1e5) / 1e5, Math.round(p[1] * 1e5) / 1e5]);
  setMode(null);
  setSource("draw", FC([]));
  setSelection({ aoiId: null, geometry: { type: "Polygon", coordinates: [ring] }, label: "Нарисованный контур", kind: "custom", fit: false });
  setStatus("Контур замкнут. Можно править вершины или запускать верификацию.", "ok");
}

function initVertexDrag(map) {
  const canvas = map.getCanvas();
  map.on("mousedown", "verts-pt", (e) => {
    if (state.mode !== "edit") return;
    e.preventDefault();
    if (e.originalEvent.altKey) { deleteVertex(e.features[0].properties.i); return; }
    state.dragVertex = e.features[0].properties.i;
    canvas.style.cursor = "grabbing";
    map.dragPan.disable();
  });
  map.on("mouseenter", "verts-pt", () => { if (state.mode === "edit") canvas.style.cursor = "grab"; });
  map.on("mouseleave", "verts-pt", () => { if (state.mode === "edit" && state.dragVertex === null) canvas.style.cursor = ""; });
  map.on("mousemove", (e) => {
    if (state.dragVertex === null || state.dragVertex === undefined) return;
    moveVertex(state.dragVertex, [e.lngLat.lng, e.lngLat.lat], true);
  });
  map.on("mouseup", () => {
    if (state.dragVertex === null || state.dragVertex === undefined) return;
    state.dragVertex = null;
    canvas.style.cursor = "grab";
    map.dragPan.enable();
    setSelection({ ...state.sel, fit: false });
  });
}

function editableRing() {
  const g = selectionGeometry();
  if (!g || g.type !== "Polygon") return null;
  return g.coordinates[0].slice(0, -1);
}

function moveVertex(i, p, live) {
  const ring = editableRing();
  if (!ring) return;
  ring[i] = [Math.round(p[0] * 1e5) / 1e5, Math.round(p[1] * 1e5) / 1e5];
  const geometry = { type: "Polygon", coordinates: [[...ring, ring[0]]] };
  state.sel = { ...state.sel, aoiId: null, geometry, kind: "custom", label: state.sel.kind === "custom" ? state.sel.label : `${state.sel.label} (изменён)` };
  if (live) { setSource("selected", featureOf(geometry)); setSource("verts", FC(ring.map((q, k) => featureOf({ type: "Point", coordinates: q }, { i: k })))); }
}

function deleteVertex(i) {
  const ring = editableRing();
  if (!ring || ring.length <= 3) { setStatus("В контуре должно остаться минимум 3 вершины", "error"); return; }
  ring.splice(i, 1);
  const geometry = { type: "Polygon", coordinates: [[...ring, ring[0]]] };
  setSelection({ ...state.sel, aoiId: null, geometry, kind: "custom", fit: false });
  setStatus(`Вершина удалена. Осталось ${ring.length}.`);
}

function clearSelection() {
  state.sel = null;
  state.drawPoints = [];
  setMode(null);
  setSource("draw", FC([]));
  setSource("verts", FC([]));
  setSource("selected", FC([]));
  $("aoi-select").value = "";
  $("preset-select").value = "";
  document.querySelectorAll(".aoi-item").forEach((el) => el.classList.remove("on"));
  $("selection-info").classList.add("hidden");
  $("analyze-btn").disabled = true;
  $("edit-btn").disabled = true;
  $("clear-btn").disabled = true;
  setStatus("");
}

/* =============================================================================
   ВЫБОР ТЕРРИТОРИИ И ПЕРИОДА
   ============================================================================= */

function knownRegion(id) { return state.official.concat(state.external).find((a) => a.aoi_id === id); }

function selectionReadout(sel, geometry, km2, tooBig) {
  const ring = ringOf(geometry);
  const c = geometry ? centroidOf(geometry) : null;
  const known = sel.aoiId ? knownRegion(sel.aoiId) : null;
  const ha = known ? known.area_ha : (km2 !== null ? km2 * 100 : null);
  const avail = sel.aoiId
    ? { cls: "", txt: "Полный локальный набор: ESA CCI, Hansen GFC" + (sel.aoiId.indexOf("MORDOVIA") >= 0 ? ", MODIS, Sentinel-2" : ", Sentinel-2") }
    : { cls: "", txt: "Данные ESA CCI и Hansen GFC будут загружены из открытых источников: ≈10–20 с для нового места, повторно — мгновенно" };
  return `
    <div class="readout ${tooBig ? "warnish" : ""}">
      <div class="rt"><span>${esc(sel.label)}</span>${sel.kind === "custom" ? `<span class="badge accent">СВОЙ КОНТУР</span>` : `<span class="badge muted">КАТАЛОГ</span>`}</div>
      <div class="rg">
        <div class="cell"><div class="k">Area</div><div class="v area-pop">${ha === null ? "—" : fmt(ha, 0) + " га"}</div></div>
        <div class="cell"><div class="k">Площадь, км²</div><div class="v">${km2 === null ? "—" : fmt(km2, 2)}</div></div>
        <div class="cell"><div class="k">Центр</div><div class="v mono">${c ? `${c[1].toFixed(4)}, ${c[0].toFixed(4)}` : "—"}</div></div>
        <div class="cell"><div class="k">Вершин</div><div class="v">${ring.length ? ring.length - 1 : "—"}</div></div>
      </div>
      <div class="foot">${tooBig
        ? `<b style="color:var(--loss)">Площадь больше ${MAX_AREA_KM2} км² — уменьшите контур, расчёт не запустится.</b>`
        : `<span class="eyebrow">Data availability</span><br>${esc(avail.txt)}`}</div>
    </div>`;
}

function setSelection(sel) {
  state.sel = sel;
  $("aoi-select").value = sel.aoiId || "";
  document.querySelectorAll(".aoi-item").forEach((el) => el.classList.toggle("on", el.dataset.aoi === sel.aoiId));
  if (sel.kind !== "preset" && sel.presetIndex === undefined) $("preset-select").value = "";
  drawSelection();

  const geometry = sel.geometry || (knownRegion(sel.aoiId) || {}).geometry;
  const km2 = geometry ? areaKm2(geometry) : null;
  const tooBig = km2 !== null && km2 > MAX_AREA_KM2;
  const info = $("selection-info");
  info.classList.remove("hidden");
  info.innerHTML = selectionReadout(sel, geometry, km2, tooBig);

  $("analyze-btn").disabled = tooBig;
  $("edit-btn").disabled = !(geometry && geometry.type === "Polygon");
  $("clear-btn").disabled = false;
}

function setSquareAt(lng, lat, opts = {}) {
  const side = parseInt($("square-size").value, 10);
  state.squareCenter = [lng, lat];
  setSelection({
    aoiId: null, geometry: squareAround(lng, lat, side), kind: "custom",
    label: opts.label || `Квадрат ${side}×${side} км · ${lat.toFixed(3)}, ${lng.toFixed(3)}`,
    fit: opts.fit !== false, presetIndex: opts.presetIndex,
  });
}

function renderAoiList() {
  const box = $("aoi-list");
  const all = state.official.concat(state.external);
  if (!all.length) { box.innerHTML = `<div class="hint">Загружаю участки…</div>`; return; }
  const lv = (id) => {
    const r = state.overview && state.overview.regions.find((x) => x.aoi_id === id);
    return r ? r.risk.overall_level : null;
  };
  box.innerHTML = all.map((a) => {
    const level = lv(a.aoi_id);
    return `<button class="aoi-item ${state.sel && state.sel.aoiId === a.aoi_id ? "on" : ""}" data-aoi="${esc(a.aoi_id)}" type="button">
      <span class="ic">${miniShape(a.geometry)}</span>
      <span class="tx"><span class="nm">${esc(a.name)}</span><span class="mt">${fmt(a.area_ha, 0)} ГА · ${esc(a.aoi_id)}</span></span>
      ${level ? `<span class="lv lv-${level}" title="риск: ${LEVEL_RU[level]}"></span>` : ""}
    </button>`;
  }).join("");
  box.querySelectorAll(".aoi-item").forEach((el) => el.addEventListener("click", () => {
    const r = knownRegion(el.dataset.aoi);
    setSelection({ aoiId: r.aoi_id, geometry: null, label: r.name, kind: r.selection_role === "загружен по запросу" ? "external" : "official" });
  }));
}

function miniShape(geometry) {
  const ring = ringOf(geometry);
  if (!ring.length) return "";
  const xs = ring.map((p) => p[0]), ys = ring.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = (v) => 2 + ((v - x0) / (x1 - x0 || 1)) * 18;
  const sy = (v) => 20 - ((v - y0) / (y1 - y0 || 1)) * 18;
  const d = ring.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)} ${sy(p[1]).toFixed(1)}`).join("") + "Z";
  return `<svg viewBox="0 0 22 22" fill="none"><path d="${d}" fill="${ACCENT}" fill-opacity=".14" stroke="${ACCENT}" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
}

function renderPeriodRail() {
  const track = $("period-track");
  const a = parseInt($("year-start").value, 10), b = parseInt($("year-end").value, 10);
  const n = YEAR_MAX - YEAR_MIN;
  const pos = (y) => ((y - YEAR_MIN) / n) * 100;
  let html = `<div class="period-line"></div><div class="period-fill" style="left:${pos(a)}%;width:${pos(b) - pos(a)}%"></div>`;
  for (let y = YEAR_MIN; y <= YEAR_MAX; y++) {
    const edge = y === a || y === b, inside = y > a && y < b;
    html += `<div class="period-yr ${edge ? "edge" : inside ? "inside" : ""}" style="left:${pos(y)}%"><i></i><span>${y}</span></div>`;
  }
  track.innerHTML = html;
}

function populateSelectors() {
  const sel = $("aoi-select");
  sel.innerHTML = `<option value="">— выбрать —</option>`;
  const group = (label, items) => items.length ? `<optgroup label="${esc(label)}">${items.map((a) => `<option value="${esc(a.aoi_id)}">${esc(a.name)} (${fmt(a.area_ha, 0)} га)</option>`).join("")}</optgroup>` : "";
  sel.insertAdjacentHTML("beforeend", group("Готовые участки кейса", state.official) + group("Загруженные регионы", state.external));
  if (state.sel && state.sel.aoiId) sel.value = state.sel.aoiId;

  const preset = $("preset-select");
  if (preset.options.length <= 1) {
    for (const g of ["Россия", "Мир"]) {
      preset.insertAdjacentHTML("beforeend", `<optgroup label="${g}">${PRESETS.map((p, i) => [p, i]).filter(([p]) => p.group === g).map(([p, i]) => `<option value="${i}">${esc(p.name)}</option>`).join("")}</optgroup>`);
    }
  }
  for (let y = YEAR_MIN; y <= YEAR_MAX; y++) {
    for (const id of ["year-start", "year-end"]) if (!$(id).querySelector(`option[value="${y}"]`)) $(id).insertAdjacentHTML("beforeend", `<option value="${y}">${y}</option>`);
  }
  if (!$("year-start").dataset.init) { $("year-start").value = YEAR_MIN; $("year-end").value = YEAR_MAX; $("year-start").dataset.init = "1"; }
  renderPeriodRail();
  renderAoiList();
}

async function loadRegions() {
  try { state.official = await api("/aois"); } catch (err) { setStatus("Не удалось загрузить участки: " + err.message, "error"); }
  try { state.external = await api("/regions"); } catch (_) { state.external = []; }
  populateSelectors();
}

async function searchPlace() {
  const q = $("place-search").value.trim();
  if (!q) return;
  setStatus("Ищу место…");
  try {
    const resp = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`, { headers: { "Accept-Language": "ru" } });
    const found = (await resp.json())[0];
    if (!found) { setStatus("Ничего не найдено — уточните запрос", "error"); return; }
    const lat = parseFloat(found.lat), lon = parseFloat(found.lon);
    if (state.mapReady) state.map.flyTo({ center: [lon, lat], zoom: 11, duration: 900 });
    setSquareAt(lon, lat, { label: `${found.display_name.split(",").slice(0, 2).join(",")}`, fit: false });
    setStatus("Место найдено — поправьте размер квадрата и запускайте верификацию", "ok");
  } catch (err) {
    setStatus("Поиск места недоступен (нет сети?). Выберите регион из списка или укажите точку на карте.", "error");
  }
}

function initSelectors() {
  document.querySelectorAll("#source-seg button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#source-seg button").forEach((x) => x.classList.toggle("on", x === b));
    $("src-catalog").classList.toggle("hidden", b.dataset.src !== "catalog");
    $("src-world").classList.toggle("hidden", b.dataset.src !== "world");
  }));

  $("aoi-select").addEventListener("change", () => {
    const id = $("aoi-select").value;
    if (!id) return;
    const r = knownRegion(id);
    setSelection({ aoiId: id, geometry: null, label: r.name, kind: r.selection_role === "загружен по запросу" ? "external" : "official" });
  });
  $("preset-select").addEventListener("change", () => {
    const i = $("preset-select").value;
    if (i === "") return;
    const p = PRESETS[parseInt(i, 10)];
    if (state.mapReady) state.map.flyTo({ center: [p.lon, p.lat], zoom: 11, duration: 1100 });
    setSquareAt(p.lon, p.lat, { label: `${p.name}`, fit: false, presetIndex: parseInt(i, 10) });
    setStatus(`Выбран регион «${p.name}». Данные загрузятся из открытых источников при запуске.`, "ok");
  });
  $("square-size").addEventListener("change", () => {
    if (state.squareCenter && state.sel && state.sel.kind === "custom") {
      const s = $("square-size").value;
      setSquareAt(state.squareCenter[0], state.squareCenter[1], { label: state.sel.label.replace(/^Квадрат \d×\d км/, `Квадрат ${s}×${s} км`), fit: false, presetIndex: state.sel.presetIndex });
    }
    drawHint(state.mode);
  });
  $("place-btn").addEventListener("click", () => setMode(state.mode === "place" ? null : "place"));
  $("draw-btn").addEventListener("click", () => {
    if (state.mode === "draw") { finishDrawing(); return; }
    clearSelection();
    setMode("draw");
  });
  $("edit-btn").addEventListener("click", () => setMode(state.mode === "edit" ? null : "edit"));
  $("clear-btn").addEventListener("click", clearSelection);
  $("place-search-btn").addEventListener("click", searchPlace);
  $("place-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPlace(); });
  $("preset-btn").addEventListener("click", () => {
    $("year-start").value = 2020; $("year-end").value = 2024; renderPeriodRail();
    setSelection({ aoiId: null, kind: "custom", label: "CHECK_TRANSFER_01 — подучасток 808,85 га в RU_VOLOGDA_02",
      geometry: { type: "Polygon", coordinates: [[[40.70175, 59.43], [40.70175, 59.47], [40.66975, 59.47], [40.66975, 59.43], [40.70175, 59.43]]] } });
  });
  for (const id of ["year-start", "year-end"]) $(id).addEventListener("change", renderPeriodRail);
  $("analyze-btn").addEventListener("click", onAnalyze);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (state.mode) setMode(null); }
    if (e.key === "Backspace" && state.mode === "draw" && state.drawPoints.length) {
      e.preventDefault(); state.drawPoints.pop(); drawDrawing();
    }
  });
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]");
    if (open) openRegion(open.dataset.open);
  });
}

function openRegion(id) {
  const r = knownRegion(id) || (state.overview && state.overview.regions.find((x) => x.aoi_id === id));
  if (!r) return;
  if (state.popup) state.popup.remove();
  setSelection({ aoiId: id, geometry: null, label: r.name, kind: r.kind === "external" || r.selection_role === "загружен по запросу" ? "external" : "official" });
  onAnalyze();
}

/* -------------------------------------------------------- нижняя панель */

function openSheet(open) {
  const s = $("portfolio-sheet");
  s.classList.toggle("open", open === undefined ? !s.classList.contains("open") : open);
}
function initSheet() {
  $("sheet-handle").addEventListener("click", () => openSheet());
}

/* =============================================================================
   ЗАПУСК ВЕРИФИКАЦИИ + анимация бобров
   ============================================================================= */

const RUN_STEPS = ["POLYGON", "SATELLITE", "CHANGE", "CARBON", "EVIDENCE"];
let runTimers = [];

function beaverScene() {
  const trees = [];
  for (let i = 0; i < 9; i++) {
    const x = 26 + i * 27, h = 34 + ((i * 41) % 22), y = 176;
    trees.push(`<g opacity=".55"><path d="M${x} ${y - h} L${x - h * 0.3} ${y} L${x + h * 0.3} ${y} Z" fill="#ddd8f0" stroke="#b6acdf" stroke-width=".8"/><path d="M${x} ${y} v7" stroke="#b6acdf" stroke-width="1.4" stroke-linecap="round"/></g>`);
  }
  return `
    <rect width="560" height="260" fill="#ffffff"/>
    <g>${contourField(300, 120, 6, 30, 14, .7, { stroke: "#ece9f9", base: 40, w: 1 })}</g>
    <g>${trees.join("")}</g>

    <!-- русло -->
    <path d="M0 214 H560" stroke="#e7e7ee" stroke-width="1"/>
    <rect id="bv-pond" x="0" y="206" width="330" height="8" fill="#6d4aff" opacity=".16" class="pond-rise"/>
    <path id="bv-stream" d="M330 210 H560" stroke="#6d4aff" stroke-width="2.4" stroke-linecap="round" opacity=".5" class="water-flow"/>

    <!-- плотина -->
    <g id="bv-dam" transform="translate(318 0)">
      ${[0, 1, 2, 3, 4].map((i) => `<rect class="dam-log" data-i="${i}" x="${-4 + (i % 2) * 5}" y="${200 - i * 9}" width="${34 - i * 3}" height="7" rx="3.2" fill="#a2734a" opacity="0"/>`).join("")}
    </g>

    <!-- бобёр 1 -->
    <g id="bv-1" opacity="0" transform="translate(150 0)">
      <g class="bv-bob">
        <ellipse cx="0" cy="190" rx="26" ry="17" fill="#a2734a"/>
        <ellipse cx="-26" cy="196" rx="13" ry="6" fill="#8a5f3b" transform="rotate(-12 -26 196)"/>
        <circle cx="19" cy="178" r="12.5" fill="#b07f52"/>
        <circle cx="14" cy="169" r="3.4" fill="#8a5f3b"/><circle cx="25" cy="169" r="3.4" fill="#8a5f3b"/>
        <circle cx="16" cy="176" r="1.7" fill="#0b0b12"/><circle cx="24" cy="176" r="1.7" fill="#0b0b12"/>
        <ellipse cx="21" cy="182" rx="3.4" ry="2.4" fill="#0b0b12"/>
        <rect x="19" y="184" width="4" height="4" rx="1" fill="#fff"/>
      </g>
      <g id="bv-branch" opacity="1"><path d="M-6 200 h40 M8 200 l-6 -6 M20 200 l6 -6" stroke="#6b4a2c" stroke-width="2.6" stroke-linecap="round"/></g>
    </g>

    <!-- бобёр 2 -->
    <g id="bv-2" opacity="0" transform="translate(408 0) scale(-1 1)">
      <g class="bv-bob">
        <ellipse cx="0" cy="192" rx="21" ry="14" fill="#8a5f3b"/>
        <ellipse cx="-21" cy="197" rx="11" ry="5" fill="#75512f" transform="rotate(-12 -21 197)"/>
        <circle cx="16" cy="182" r="10.5" fill="#a2734a"/>
        <circle cx="12" cy="174" r="2.9" fill="#75512f"/><circle cx="21" cy="174" r="2.9" fill="#75512f"/>
        <circle cx="13" cy="180" r="1.5" fill="#0b0b12"/><circle cx="20" cy="180" r="1.5" fill="#0b0b12"/>
        <ellipse cx="17" cy="185" rx="2.9" ry="2" fill="#0b0b12"/>
      </g>
    </g>

    <!-- галочка завершения -->
    <g id="bv-done" opacity="0">
      <circle cx="280" cy="112" r="30" fill="#1c7a50" opacity=".10"/>
      <circle cx="280" cy="112" r="30" fill="none" stroke="#1c7a50" stroke-width="2"/>
      <path d="M268 112 l8 9 l16 -18" fill="none" stroke="#1c7a50" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
    </g>`;
}

function runOverlay(on) {
  const ov = $("run-overlay");
  runTimers.forEach(clearTimeout);
  runTimers = [];
  if (!on) {
    ov.classList.add("out");
    setTimeout(() => { ov.classList.add("hidden"); ov.classList.remove("out"); }, 400);
    return;
  }
  ov.classList.remove("hidden", "out");
  $("beaver-scene").innerHTML = beaverScene();
  $("run-bar").classList.remove("done");
  $("run-steps").innerHTML = RUN_STEPS.map((s) => `<span class="s">${s}</span>`).join("");
  $("run-title").textContent = "VERIFYING FOREST…";
  $("run-sub").textContent = "Читаю спутниковые данные по вашему контуру";

  const svg = $("beaver-scene");
  const at = (ms, fn) => runTimers.push(setTimeout(fn, ms));
  const step = (i) => {
    const els = $("run-steps").children;
    for (let k = 0; k < els.length; k++) els[k].className = "s" + (k < i ? " done" : k === i ? " on" : "");
  };

  step(0);
  at(260, () => { svg.querySelector("#bv-1").setAttribute("opacity", "1"); svg.querySelector("#bv-1").classList.add("bv-walk"); });
  at(500, () => { step(1); $("run-sub").textContent = "Сцены Sentinel-2 и продукты ESA CCI / Hansen GFC"; });
  at(1300, () => { svg.querySelector("#bv-branch").classList.add("bv-drop"); });
  at(1500, () => { svg.querySelector('.dam-log[data-i="0"]').classList.add("in"); step(2); $("run-sub").textContent = "Ищу изменения покрова и их причину"; });
  at(1750, () => { svg.querySelector('.dam-log[data-i="1"]').classList.add("in"); const b2 = svg.querySelector("#bv-2"); b2.setAttribute("opacity", "1"); b2.classList.add("bv-walk"); });
  at(2050, () => svg.querySelector('.dam-log[data-i="2"]').classList.add("in"));
  at(2300, () => { svg.querySelector('.dam-log[data-i="3"]').classList.add("in"); step(3); $("run-sub").textContent = "Считаю биомассу → углерод → CO₂-эквивалент"; });
  at(2550, () => {
    svg.querySelector('.dam-log[data-i="4"]').classList.add("in");
    const pond = svg.querySelector("#bv-pond");
    pond.setAttribute("y", "188"); pond.setAttribute("height", "26");
  });
  at(3100, () => { step(4); $("run-sub").textContent = "Собираю доказательства и провенанс"; });
}

function runOverlayDone(ok) {
  runTimers.forEach(clearTimeout);
  runTimers = [];
  const svg = $("beaver-scene");
  svg.querySelectorAll(".dam-log").forEach((el) => el.classList.add("in"));
  const b1 = svg.querySelector("#bv-1"), b2 = svg.querySelector("#bv-2");
  if (b1) b1.setAttribute("opacity", "1");
  if (b2) b2.setAttribute("opacity", "1");
  if (ok && svg.querySelector("#bv-done")) svg.querySelector("#bv-done").setAttribute("opacity", "1");
  $("run-title").textContent = ok ? "ANALYSIS COMPLETE" : "ANALYSIS FAILED";
  $("run-sub").textContent = ok ? "Результат готов" : "Смотрите сообщение слева";
  $("run-bar").classList.add("done");
  const els = $("run-steps").children;
  for (let k = 0; k < els.length; k++) els[k].className = "s done";
}

function setBusy(busy) {
  $("analyze-btn").disabled = busy || !state.sel || (state.sel.geometry ? areaKm2(state.sel.geometry) > MAX_AREA_KM2 : false);
  $("analyze-btn").textContent = busy ? "ВЫПОЛНЯЕТСЯ…" : "RUN VERIFICATION";
}

async function onAnalyze() {
  if (!state.sel) { setStatus("Сначала выберите территорию", "error"); return; }
  const yearStart = parseInt($("year-start").value, 10), yearEnd = parseInt($("year-end").value, 10);
  if (yearEnd <= yearStart) { setStatus("Конечный год должен быть больше начального", "error"); return; }
  const body = { year_start: yearStart, year_end: yearEnd };
  if (state.sel.aoiId) body.aoi_id = state.sel.aoiId; else { body.geometry = state.sel.geometry; body.region_name = state.sel.label; }
  const isNewPlace = !state.sel.aoiId;

  setMode(null);
  setBusy(true);
  runOverlay(true);
  setStatus(isNewPlace ? "Считаю… для нового места данные ESA CCI и Hansen загружаются из открытых источников (≈10–20 с)" : "Считаю…");
  const started = Date.now();

  try {
    const data = await post("/analysis", body);
    state.result = data;
    state.body = body;
    runOverlayDone(true);
    const wait = Math.max(0, 2400 - (Date.now() - started));
    setTimeout(() => {
      runOverlay(false);
      fitToSelection(600);
      renderAll(data, true);
      setStatus(`Готово · расчёт ${data.meta.calculation_id.slice(0, 8)}`, "ok");
      goView(ROLE_VIEW[role()] || "result");
      rutReact(data);
    }, wait + 500);
    if (isNewPlace) { await loadRegions(); state.overview = null; loadPortfolio(true); }
  } catch (err) {
    runOverlayDone(false);
    setTimeout(() => runOverlay(false), 800);
    setStatus(err.status === 503 ? "Источник данных недоступен: " + err.message : "Ошибка: " + err.message, "error");
  } finally {
    setBusy(false);
  }
}

/* =============================================================================
   НАВИГАЦИЯ ПО ЭКРАНАМ (MAP → RESULT → EVIDENCE → AUDIT)
   ============================================================================= */

function goView(name) {
  if (!state.result && name !== "map") return;
  state.view = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  document.querySelectorAll(".navbtn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  window.scrollTo(0, 0);
}

function initNav() {
  document.querySelectorAll(".navbtn").forEach((b) => b.addEventListener("click", () => { if (!b.disabled) goView(b.dataset.view); }));
}

function unlockResultNav() {
  document.querySelectorAll('.navbtn[data-view="result"], .navbtn[data-view="evidence"], .navbtn[data-view="audit"]').forEach((b) => { b.disabled = false; });
}

/* -------------------------------------------------------------- подвкладки */

function goTab(group, name) {
  state.tab[group] = name;
  const nav = { result: "tabs", evidence: "tabs-ev", audit: "tabs-au" }[group];
  document.querySelectorAll(`#${nav} .tab-btn`).forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  const ids = {
    result: ["summary", "dynamics", "changes", "recommendations", "risks", "forecast"],
    evidence: ["evidence", "checklist", "sources"],
    audit: ["calc", "quality", "uncertainty", "data", "raw"],
  }[group];
  ids.forEach((id) => $("tab-" + id).classList.toggle("active", id === name));
  if (name === "dynamics" && state.result) renderDynamicsChart(state.result);
  if (RUT_TAB_TIPS[name] && state.role && !state.rutTabsSeen[name]) {
    state.rutTabsSeen[name] = true;
    rutSay(`<b>Бобёр РУТ:</b> ${esc(RUT_TAB_TIPS[name])}`, 7000);
  }
}

function initTabs() {
  document.querySelectorAll("#tabs .tab-btn").forEach((b) => b.addEventListener("click", () => goTab("result", b.dataset.tab)));
  document.querySelectorAll("#tabs-ev .tab-btn").forEach((b) => b.addEventListener("click", () => goTab("evidence", b.dataset.tab)));
  document.querySelectorAll("#tabs-au .tab-btn").forEach((b) => b.addEventListener("click", () => goTab("audit", b.dataset.tab)));
}

/* =============================================================================
   RESULT — главный экран: доминирующее число + провенанс
   ============================================================================= */

function heroTopo() {
  return `<svg class="hero-topo" viewBox="0 0 1200 340" preserveAspectRatio="none" aria-hidden="true">
    ${contourField(980, 60, 7, 34, 22, 2.1, { stroke: "#efeafa", base: 10, w: 1, fade: true })}
  </svg>`;
}

function renderResultHero(data) {
  const sd = data.stock_difference, ins = data.insights, cd = data.change_detection || {};
  const changed = cd.status === "ok" && (cd.loss_area_ha > 0.1 || Math.abs(sd.emissions_tco2e) > 1);
  const isLoss = sd.emissions_tco2e > 0;
  const files = data.data_provenance || [];
  const prodSet = [...new Set(files.map((f) => f.product_version).filter(Boolean))];

  $("result-hero").innerHTML = `
    <div class="result-hero">
      ${heroTopo()}
      <div class="hero-in">
        <div class="hero-tag">
          <span class="hero-status ${changed ? "change" : "stable"}"><span class="bl"></span>${changed ? "CHANGE DETECTED" : "NO SIGNIFICANT CHANGE"}</span>
          <span class="badge muted">${esc(data.request.year_start)}–${esc(data.request.year_end)}</span>
          <span class="badge muted">${esc(data.sel_label || data.request.region_name || data.request.aoi_id || "участок")}</span>
          <span class="badge accent">${fmt(data.area_ha, 0)} ГА</span>
        </div>

        <div class="hero-main">
          <div>
            <div class="big-cap">Forest verification · Δ carbon</div>
            <span class="big-num ${isLoss ? "neg" : "pos"} count-up" data-target="${signed(sd.emissions_tco2e, 0)}">${signed(sd.emissions_tco2e, 0)}<span class="u">т CO₂-экв.</span></span>
            <div class="big-sub"><span class="${sd.delta_c_tc < 0 ? "neg" : "pos"}">${signed(sd.delta_c_tc, 0)}</span><span class="u">т C</span></div>
          </div>
          <div class="hero-side">
            <p class="hero-headline">${esc(ins.headline)}</p>
          </div>
        </div>
      </div>

      <div class="hero-meta">
        <div class="m"><div class="k">Area</div><div class="v">${fmt(data.area_ha, 0)} га</div><div class="s">покрытие ${fmt(data.covered_fraction * 100, 0)}%</div></div>
        <div class="m"><div class="k">Period</div><div class="v">${data.stock_t0.year} → ${data.stock_t1.year}</div><div class="s">${fmt(data.stock_t0.mean_stock_tc_ha, 1)} → ${fmt(data.stock_t1.mean_stock_tc_ha, 1)} т C/га</div></div>
        <div class="m"><div class="k">Evidence status</div><div class="v">${cd.status === "ok" ? esc(cd.confirmed_cause) : "недоступно"}</div><div class="s">${cd.status === "ok" ? (cd.evidence || []).filter((e) => e.confirms !== null).length + " источника применимо" : "—"}</div></div>
        <div class="m"><div class="k">Data quality</div><div class="v">${data.covered_fraction >= 0.999 ? "AVAILABLE" : data.covered_fraction > 0 ? "PARTIAL" : "NOT AVAILABLE"}</div><div class="s">${files.filter((f) => !f.verified).length ? "есть расхождения SHA-256" : "все файлы проверены"}</div></div>
      </div>
      <div class="hero-in" style="padding-top:12px;padding-bottom:14px">
        <div class="prov">
          <span><b>SOURCE</b> ESA CCI Biomass · Hansen GFC${cd.burn_index && cd.burn_index.status === "ok" ? " · Sentinel-2 · MODIS" : ""}</span>
          <span><b>VERSION</b> ${esc(prodSet.join(" · ") || "—")}</span>
          <span><b>METHOD</b> stock-difference, CF=${data.meta.params.cf_agb}</span>
          <span><b>CALC ID</b> ${esc(data.meta.calculation_id.slice(0, 10))}…</span>
          <span><b>COMPUTED</b> ${esc(data.meta.computed_at.slice(0, 16).replace("T", " "))} UTC</span>
        </div>
      </div>
    </div>`;
}

/* --------------------------------------------------- цепочка верификации */

function chainNodes(data) {
  const sd = data.stock_difference, cd = data.change_detection || {}, u = data.potential_units, params = data.meta.params;
  const area = data.area_ha;
  return [
    { key: "polygon", icon: "polygon", label: "POLYGON", value: fmt(area, 0), unit: "га",
      title: "Территория анализа", desc: `Контур: ${data.request.aoi_id ? "участок каталога " + data.request.aoi_id : "произвольный полигон пользователя"}. Площадь ограничена лимитом ${MAX_AREA_KM2} км² на запрос.`,
      fields: { "Площадь": `${fmt(area, 3)} га`, "Вершин": (ringOf(data.territory_geometry).length - 1), "Покрытие данными": fmt(data.covered_fraction * 100, 1) + "%" } },
    { key: "satellite", icon: "satellite", label: "SATELLITE", value: (data.data_provenance || []).length, unit: "файлов",
      title: "Спутниковое наблюдение", desc: "Локальные копии продуктов ESA CCI Biomass и Hansen GFC (плюс MODIS/Sentinel-2 для участков каталога), сверенные по SHA-256 с каталогом источника.",
      fields: { "Источники": [...new Set((data.sources || []).map((s) => s.product))].slice(0, 3).join("; "), "Файлов проверено": (data.data_provenance || []).filter((f) => f.verified).length + " / " + (data.data_provenance || []).length } },
    { key: "change", icon: "change", label: "CHANGE", value: cd.status === "ok" ? fmt(cd.loss_area_ha, 1) : "—", unit: "га потери",
      title: "Обнаружение изменений", desc: cd.status === "ok" ? `Причина: «${cd.confirmed_cause}». Установлена по совпадению независимых источников (Hansen GFC, MODIS, NDVI Sentinel-2, ESA CCI Change).` : "Анализ изменений недоступен для этого запроса.",
      fields: cd.status === "ok" ? { "Дата события": (cd.date_range || ["—"]).join(" … "), "Не объяснено": fmt(cd.unexplained_area_ha, 1) + " га" } : {} },
    { key: "biomass", icon: "biomass", label: "BIOMASS", value: `${fmt(data.stock_t0.mean_stock_tc_ha, 1)}→${fmt(data.stock_t1.mean_stock_tc_ha, 1)}`, unit: "т C/га",
      title: "Надземная биомасса (AGB)", desc: "Средний запас углерода в живой надземной древесной биомассе по продукту ESA CCI Biomass, на начало и конец периода.",
      fields: { [`Запас ${data.stock_t0.year}`]: fmt(data.stock_t0.mean_stock_tc_ha, 2) + " т C/га", [`Запас ${data.stock_t1.year}`]: fmt(data.stock_t1.mean_stock_tc_ha, 2) + " т C/га" },
      formula: "AGB(т C/га) читается напрямую из растра ESA CCI Biomass" },
    { key: "carbon", icon: "carbon", label: "CARBON", value: signed(sd.delta_c_tc, 0), unit: "т C",
      title: "Изменение запаса углерода", desc: "Разность запасов на начало и конец периода, умноженная на площадь территории.",
      fields: { "ΔC": signed(sd.delta_c_tc, 1) + " т C", "Темп": signed(sd.annual_rate_tco2e_ha_yr, 3) + " т CO₂-экв./га/год" },
      formula: `ΔC = (Stock${data.stock_t1.year} − Stock${data.stock_t0.year}) × Area = (${fmt(data.stock_t1.mean_stock_tc_ha, 2)} − ${fmt(data.stock_t0.mean_stock_tc_ha, 2)}) × ${fmt(area, 1)} = ${signed(sd.delta_c_tc, 1)} т C` },
    { key: "co2", icon: "co2", label: "CO₂e", value: signed(sd.emissions_tco2e, 0), unit: "т",
      title: "CO₂-эквивалент", desc: "Перевод изменения запаса углерода в CO₂-эквивалент по отношению молярных масс. Положительное значение — потеря из пула, отрицательное — накопление.",
      fields: { "Коэффициент": `${fmt(params.co2_per_c, 4)} (44/12)`, "Единицы Q": u.q_units === null ? "—" : fmt(u.q_units, 0) },
      formula: `E = −ΔC × 44/12 = −(${signed(sd.delta_c_tc, 1)}) × ${fmt(params.co2_per_c, 4)} = ${signed(sd.emissions_tco2e, 0)} т CO₂-экв.` },
    { key: "evidence", icon: "evidence", label: "EVIDENCE", value: (data.sources || []).length, unit: "источников",
      title: "Досье доказательств", desc: "Каждое утверждение расчёта подкреплено источником, ограничением и уровнем доверия — полное досье в разделе EVIDENCE.",
      fields: { "Проверок пройдено": `${data.insights.checklist.counts.pass} / ${data.insights.checklist.items.length}`, "Вердикт": data.insights.checklist.verdict },
      cta: true },
  ];
}

function renderChain(data) {
  const nodes = chainNodes(data);
  const rail = nodes.map((n, i) => `
    <button class="chain-node ${state.chainNode === n.key ? "on" : ""}" data-node="${n.key}" type="button">
      <span class="ci">${ICON[n.icon]}</span>
      <div class="ck">${n.label}</div>
      <div class="cv">${esc(String(n.value))}</div>
      <div class="cu">${esc(n.unit)}</div>
    </button>`).join("");

  $("chain-block").innerHTML = `
    <div class="section-head"><h3>Verification chain</h3><span class="muted">Нажмите узел — реальные значения этого расчёта, формула и источник</span></div>
    <div class="chain-rail">${rail}</div>
    <div id="chain-detail"></div>`;

  document.querySelectorAll(".chain-node").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.node;
      state.chainNode = state.chainNode === key ? null : key;
      renderChain(data);
      if (state.chainNode) {
        const n = nodes.find((x) => x.key === state.chainNode);
        renderChainDetail(n);
      }
    });
  });
  if (state.chainNode) renderChainDetail(nodes.find((x) => x.key === state.chainNode));
}

function renderChainDetail(n) {
  if (!n) return;
  $("chain-detail").innerHTML = `
    <div class="chain-detail">
      <h4>${ICON[n.icon].replace('width="16" height="16"', 'width="15" height="15" style="vertical-align:-2px;margin-right:6px"')}${esc(n.title)}</h4>
      <div class="desc">${esc(n.desc)}</div>
      <div class="grid">${Object.entries(n.fields).map(([k, v]) => `<div class="cellx"><div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div></div>`).join("")}</div>
      ${n.formula ? `<div class="formula">${esc(n.formula)}</div>` : ""}
      <div class="acts">
        <button class="btn sm accent-soft" data-jump="evidence" type="button">Открыть в EVIDENCE</button>
        <button class="btn sm accent-soft" data-jump="calc" type="button">Открыть в AUDIT → дерево расчёта</button>
      </div>
    </div>`;
  $("chain-detail").querySelector('[data-jump="evidence"]').addEventListener("click", () => { goView("evidence"); goTab("evidence", "evidence"); });
  $("chain-detail").querySelector('[data-jump="calc"]').addEventListener("click", () => { goView("audit"); goTab("audit", "calc"); });
}

/* animate the big number once per render */
function animateCountUp() {
  document.querySelectorAll(".count-up").forEach((el) => {
    const text = el.dataset.target || el.textContent;
    const m = text.match(/-?[\d\s]+/);
    if (!m) return;
    const target = parseFloat(m[0].replace(/\s/g, ""));
    const suffix = el.querySelector(".u") ? el.querySelector(".u").outerHTML : "";
    const prefix = target > 0 ? "+" : "";
    const dur = 700, t0 = performance.now();
    function tick(t) {
      const p = Math.min(1, (t - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      const v = Math.round(target * eased);
      el.innerHTML = prefix + fmt(v, 0) + suffix;
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  });
}

/* =============================================================================
   ПОДВКЛАДКИ RESULT: сводка, динамика, изменения, рекомендации, риски, прогноз
   ============================================================================= */

function spark(series, w = 132, h = 34) {
  const v = series.map((p) => p.mean_stock_tc_ha);
  const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
  const pts = v.map((y, i) => `${(i / (v.length - 1)) * (w - 4) + 2},${h - 4 - ((y - min) / span) * (h - 8)}`).join(" ");
  const color = v[v.length - 1] < v[0] ? RED : GREEN;
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function ringSVG(score, level) {
  const r = 44, c = 2 * Math.PI * r;
  return `<svg width="170" height="170" viewBox="0 0 110 110"><circle cx="55" cy="55" r="${r}" fill="none" stroke="#eeecf7" stroke-width="10"/>
    <circle cx="55" cy="55" r="${r}" fill="none" stroke="${LEVEL_COLOR[level]}" stroke-width="10" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - score / 100)}" transform="rotate(-90 55 55)"/></svg>`;
}

function riskBlock(ins, withFactors) {
  const rk = ins.risk;
  const meters = Object.values(rk.categories).map((c) => `
    <div class="meter"><div class="head"><span>${esc(c.title)}</span><span class="badge ${c.level}">${esc(c.level_label)} · ${c.score}/100</span></div>
      <div class="track"><div class="fill ${c.level}" style="width:${Math.max(c.score, 3)}%"></div></div>
      ${withFactors ? `<ul>${c.factors.map((f) => `<li><b>${esc(f.name)}</b> (+${f.points}) — ${esc(f.detail)}</li>`).join("") || "<li>факторов риска нет</li>"}</ul>` : ""}
    </div>`).join("");
  return `<div class="risk-layout">
    <div class="ring">${ringSVG(rk.overall_score, rk.overall_level)}<div class="center"><div class="num">${rk.overall_score}</div><div class="cap">риск: ${esc(rk.overall_label)}</div></div></div>
    <div>${meters}<div class="hint">Индекс риска — презентационное представление VRI и его слоёв из расчётного движка; это не вероятность ошибки и не отдельный сертификационный вердикт.</div></div></div>`;
}

function recCard(rec) {
  const names = { verifier: "верификатор", investor: "инвестор", owner: "владелец" };
  return `<div class="rec ${rec.severity}"><div class="top"><span class="badge ${rec.severity}">${SEVERITY_RU[rec.severity]}</span><span class="title">${esc(rec.title)}</span></div>
    <div class="text">${esc(rec.text)}</div>
    ${rec.actions.length ? `<ul class="actions">${rec.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
    <div class="aud">для: ${rec.audience.map((a) => names[a]).join(", ")}</div></div>`;
}
const recsForRole = (ins, all) => ins.recommendations.filter((r) => all || r.audience.includes(role()));

function renderSummary(data) {
  const sd = data.stock_difference, ins = data.insights, cd = data.change_detection || {}, u = data.potential_units;
  const pct = ins.key_facts[0].value;
  const cls = sd.emissions_tco2e > 0 ? "negative" : "positive";
  const top = recsForRole(ins, false).filter((r) => r.severity !== "info").slice(0, 3);
  $("tab-summary").innerHTML = `
    <div class="note"><span class="eyebrow accent">Простыми словами</span><br>${esc(ins.headline)}</div>
    <div class="cards">
      <div class="card"><div class="label">Площадь расчёта</div><div class="value">${fmt(data.area_ha, 0)} га</div><div class="sub">покрытие данными ${fmt(data.covered_fraction * 100)}%</div></div>
      <div class="card"><div class="label">Запас ${data.stock_t0.year} → ${data.stock_t1.year}</div><div class="value">${fmt(data.stock_t0.mean_stock_tc_ha, 1)} → ${fmt(data.stock_t1.mean_stock_tc_ha, 1)}</div><div class="sub">т C/га · <b class="${cls === "negative" ? "neg" : "pos"}">${esc(pct)}</b></div></div>
      <div class="card"><div class="label">Результат E, т CO₂-экв.</div><div class="value ${cls}">${signed(sd.emissions_tco2e)}</div><div class="sub">${sd.emissions_tco2e > 0 ? "потеря углерода из пула" : "накопление углерода"} · ${signed(sd.annual_rate_tco2e_ha_yr, 2)} т/га/год</div></div>
      <div class="card"><div class="label">Потеря леса (GFC)</div><div class="value ${cd.loss_area_ha > 0.1 ? "warn" : ""}">${fmt(cd.loss_area_ha, 1)} га</div><div class="sub">${esc(cd.confirmed_cause || "—")}</div></div>
      <div class="card"><div class="label">Потенциальные единицы</div><div class="value">${u.q_units === null ? "—" : fmt(u.q_units, 0)}</div><div class="sub">${u.status === "ok" ? "R = " + signed(u.r_tco2e) + " т CO₂-экв." : "не рассчитаны"}</div></div>
    </div>
    <div class="section-head"><h3>Оценка рисков</h3></div>${riskBlock(ins, false)}
    ${top.length ? `<div class="section-head"><h3>Главное для вас — ${esc(state.roles[role()].title)}</h3></div>${top.map(recCard).join("")}<p class="hint">Все рекомендации — во вкладке «Рекомендации».</p>` : ""}
    <p class="export-row">
      <a class="btn accent-soft" href="${API}/report/${data.meta.calculation_id}" target="_blank" rel="noopener">Открыть отчёт (печать → PDF)</a>
      <a class="btn" href="${API}/export/${data.meta.calculation_id}.csv">Скачать таблицу (CSV)</a>
      <a class="btn" href="${API}/export/${data.meta.calculation_id}.geojson">Скачать контуры (GeoJSON)</a>
    </p>
    <table class="kv"><tr><td>ID расчёта</td><td><code>${esc(data.meta.calculation_id)}</code></td></tr>
      <tr><td>Время расчёта (UTC)</td><td>${esc(data.meta.computed_at)}</td></tr>
      <tr><td>Период</td><td>${data.request.year_start}–${data.request.year_end}</td></tr>
      <tr><td>Пул / коэффициенты</td><td>надземная биомасса, CF = ${data.meta.params.cf_agb}, CO₂/C = ${fmt(data.meta.params.co2_per_c, 4)}</td></tr></table>
    <p class="hint">${esc(ins.disclaimer)}</p>`;
}

function renderRecommendations(data) {
  const ins = data.insights;
  const list = recsForRole(ins, state.showAllRoles);
  $("tab-recommendations").innerHTML = `
    <div class="toolbar"><span><b>${list.length}</b> рекомендаций${state.showAllRoles ? " (все роли)" : ` для роли «${esc(state.roles[role()].title)}»`}</span>
      <label><input type="checkbox" id="all-roles" ${state.showAllRoles ? "checked" : ""}/> показать для всех ролей</label></div>
    ${list.map(recCard).join("") || `<div class="placeholder">Для вашей роли рекомендаций нет.</div>`}
    <p class="hint">${esc(ins.disclaimer)}</p>`;
  $("all-roles").addEventListener("change", (e) => { state.showAllRoles = e.target.checked; renderRecommendations(data); });
}

function renderRisks(data) {
  const ins = data.insights, b = data.baseline, pu = data.potential_units;
  const parts = b.parts.map((p) => `<tr><td>${esc(p.aoi_id)} (доля ${fmt(p.fraction * 100)}%)</td><td>${fmt(p.area_ha)} га, E_base = ${signed(p.emissions_tco2e)}</td></tr>`).join("");
  $("tab-risks").innerHTML = `
    <div class="section-head" style="margin-top:0"><h3>Оценка рисков</h3></div>${riskBlock(ins, true)}
    <div class="section-head"><h3>Базовая линия</h3></div>
    <table class="kv"><tr><td>Статус</td><td><span class="badge ${b.status === "ok" ? "ok" : "warn"}">${b.status}</span></td></tr>
      <tr><td>E_base, т CO₂-экв.</td><td>${signed(b.emissions_tco2e)}</td></tr>
      <tr><td>Покрытие</td><td>${fmt(b.covered_fraction * 100)}% (недоступно ${fmt(b.unavailable_fraction * 100)}%)</td></tr>${parts}
      ${b.reason ? `<tr><td>Причина</td><td>${esc(b.reason)}</td></tr>` : ""}</table>
    <div class="section-head"><h3>Контроль: сравнение с окружением</h3></div>
    <p class="hint">Базовая линия выше — историческая (тренд самого участка за 2015–2019). Она не отвечает на вопрос,
    отличается ли динамика участка от фона вокруг. Контроль берёт кольцо земли вокруг участка и считает по нему тот же
    удельный результат из того же продукта за тот же период.</p>
    <div id="control-box"><button class="btn" id="control-btn" type="button">Сравнить с окружением</button>
      <span class="hint"> Для нового места данные кольца догружаются из открытых источников — 5–20 с.</span></div>
    <div class="section-head"><h3>Потенциальные единицы</h3></div>
    <div class="cards">
      <div class="card"><div class="label">R = E_base − E_proj − LK</div><div class="value ${pu.r_tco2e > 0 ? "negative" : "positive"}">${signed(pu.r_tco2e)}</div><div class="sub">т CO₂-экв.</div></div>
      <div class="card"><div class="label">H / R</div><div class="value">${pu.h_over_r === null ? "—" : fmt(pu.h_over_r, 2)}</div><div class="sub">H = ${fmt(pu.h_tco2e)}</div></div>
      <div class="card"><div class="label">Единицы Q</div><div class="value">${pu.q_units === null ? "—" : fmt(pu.q_units, 0)}</div><div class="sub">резерв B = ${fmt(pu.b_tco2e)}</div></div>
      <div class="card"><div class="label">Стоимость (демо), руб.</div><div class="value" style="font-size:16px">${fmt(pu.v_low, 0)} / ${fmt(pu.v_base, 0)} / ${fmt(pu.v_high, 0)}</div><div class="sub">500 / 1500 / 4000 руб./ед.</div></div>
    </div>
    ${pu.reason ? `<p class="hint">${esc(pu.reason)}</p>` : ""}
    <p class="hint">Стоимость демонстрационная и не является прогнозом рынка. ${esc(ins.disclaimer)}</p>`;
  const cached = (state.control || {})[data.meta.calculation_id];
  if (cached) $("control-box").innerHTML = controlBlock(cached);
  else $("control-btn").onclick = () => runControl(data);
}

function controlBlock(c) {
  if (c.status === "unavailable") return `<div class="placeholder">Сравнение недоступно: ${esc(c.reason || "нет данных окружения")}. Кольцо построено, но данные по нему не получены.</div>`;
  if (c.status !== "ok") return `<div class="placeholder">Данных окружения не хватает: ${esc(c.reason || "кольцо не покрыто данными")}.</div>`;
  const worse = c.difference_tco2e_ha_yr > c.band_tco2e_ha_yr;
  const better = c.difference_tco2e_ha_yr < -c.band_tco2e_ha_yr;
  return `<div class="verdict ${worse ? "warn" : better ? "pass" : "info"}"><b>${esc(c.verdict)}.</b> ${esc(c.explanation)}</div>
    <div class="cards">
      <div class="card"><div class="label">Участок</div><div class="value ${c.project_rate_tco2e_ha_yr > 0 ? "negative" : "positive"}">${signed(c.project_rate_tco2e_ha_yr, 2)}</div><div class="sub">т CO₂-экв./га/год</div></div>
      <div class="card"><div class="label">Окружение</div><div class="value">${signed(c.control_rate_tco2e_ha_yr, 2)}</div><div class="sub">кольцо ${fmt(c.buffer_m, 0)} м · ${fmt(c.area_ha, 0)} га</div></div>
      <div class="card"><div class="label">Разница</div><div class="value"><span class="badge ${worse ? "warn" : better ? "ok" : "muted"}">${signed(c.difference_tco2e_ha_yr, 2)}</span></div><div class="sub">полоса различимости ±${fmt(c.band_tco2e_ha_yr, 2)}</div></div>
      <div class="card"><div class="label">Хуже секторов фона</div><div class="value">${c.worse_than_sectors} из ${c.sectors}</div><div class="sub">${esc(c.band_source)}</div></div>
    </div>
    <table class="kv"><tr><td>Средний запас окружения ${fmt(c.control_mean_stock_t0_tc_ha, 1)} → ${fmt(c.control_mean_stock_t1_tc_ha, 1)}</td><td>т C/га</td></tr>
      <tr><td>Удельный результат по секторам</td><td>${(c.sector_rates_tco2e_ha_yr || []).map((r) => signed(r, 2)).join(" · ")}</td></tr>
      <tr><td>Покрытие кольца данными</td><td>${fmt(c.covered_fraction * 100, 0)}%</td></tr></table>
    <ul class="hint">${(c.assumptions || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
}

async function runControl(data) {
  const box = $("control-box");
  const cached = (state.control || {})[data.meta.calculation_id];
  if (cached) { box.innerHTML = controlBlock(cached); return; }
  box.innerHTML = `<div class="placeholder">Считаю окружение участка…</div>`;
  try {
    const resp = await post("/control-area", { calculation_id: data.meta.calculation_id });
    (state.control = state.control || {})[data.meta.calculation_id] = resp.control_area;
    box.innerHTML = controlBlock(resp.control_area);
  } catch (err) {
    box.innerHTML = `<div class="placeholder">Не удалось сравнить с окружением: ${esc(err.message)}</div>`;
  }
}

function renderDynamicsChart(data) {
  const canvas = $("dynamics-chart");
  if (typeof Chart === "undefined") { canvas.parentElement.innerHTML = `<div class="placeholder">График недоступен (не загрузилась библиотека Chart.js) — данные в таблице ниже.</div>`; return; }
  if (state.chart) state.chart.destroy();
  const retro = data.retrospective_2019_2024, rq = data.request;
  state.chart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels: retro.map((p) => p.year), datasets: [
      { label: "Средний запас, т C/га (ретроспектива 2019–2024)", data: retro.map((p) => p.mean_stock_tc_ha), borderColor: "#c9c2ea", backgroundColor: "rgba(109,74,255,.06)", fill: true, tension: 0.25, pointRadius: 2 },
      { label: "Запрошенный период", data: retro.map((p) => (p.year >= rq.year_start && p.year <= rq.year_end ? p.mean_stock_tc_ha : null)), borderColor: ACCENT, backgroundColor: ACCENT, borderWidth: 3, pointRadius: 5, pointBackgroundColor: "#fff", pointBorderWidth: 2 },
    ] },
    options: { responsive: true, maintainAspectRatio: false, scales: { x: { ticks: { color: "#6a6a7c", font: { family: "Inter" } }, grid: { color: "#f0f0f5" } }, y: { ticks: { color: "#6a6a7c", font: { family: "Inter" } }, grid: { color: "#f0f0f5" }, title: { display: true, text: "т C/га", color: "#6a6a7c" } } }, plugins: { legend: { labels: { color: "#0b0b12", font: { family: "Inter", size: 11.5 } } } } },
  });
}

function renderDynamicsExtra(data) {
  const retro = Array.isArray(data.retrospective_2019_2024) ? data.retrospective_2019_2024 : [];
  if (!retro.length) {
    $("dynamics-extra").innerHTML = `<div class="placeholder">Историческая динамика недоступна: для этого контура нет покрытия ESA CCI Biomass.</div>`;
    return;
  }
  const base = retro[0].total_stock_tc;
  $("dynamics-extra").innerHTML = `<table class="kv grid"><tr><th>Год</th><th>Средний запас, т C/га</th><th>Суммарный запас, т C</th><th>Накопленное изменение, т C</th></tr>
    ${retro.map((p) => `<tr><td>${p.year}</td><td>${fmt(p.mean_stock_tc_ha, 2)}</td><td>${fmt(p.total_stock_tc)}</td><td class="${p.total_stock_tc < base ? "neg" : "pos"}">${signed(p.total_stock_tc - base)}</td></tr>`).join("")}</table>`;
}

function confirmsBadge(v) {
  return v === true ? `<span class="badge ok">подтверждает</span>` : v === false ? `<span class="badge muted">не подтверждает</span>` : `<span class="badge muted">не применимо</span>`;
}

function burnCard(b) {
  if (!b || b.status === "unavailable") return "";
  const cls = b.fire_signature ? "warn" : "";
  return `<div class="card"><div class="label">След гари (dNBR)</div><div class="value ${cls}">${fmt(b.burned_area_ha, 1)} га</div><div class="sub">${fmt(b.burned_fraction * 100)}% площади · ${esc(b.severity)}</div></div>`;
}

function burnBlock(b) {
  if (!b) return "";
  if (b.status === "unavailable") return `<p class="hint">Спектральная проверка гари недоступна: ${esc((b.notes || []).join("; ") || "нет сцен")}.</p>`;
  const verdict = b.fire_signature ? `<span class="badge warn">след гари подтверждён</span>` : `<span class="badge ok">спектральных признаков гари нет</span>`;
  return `<div class="section-head"><h3>Спектральный след гари (dNBR по Sentinel-2)</h3></div>
    <p class="hint">Индекс NBR использует коротковолновый инфракрасный канал: на гари он резко меняется, поэтому dNBR отличает пожар от рубки и работает независимо от MODIS. Шкала тяжести — USGS/FIREMON.</p>
    <table class="kv"><tr><td>Вывод</td><td>${verdict}</td></tr>
      <tr><td>Площадь со следом гари</td><td>${fmt(b.burned_area_ha, 1)} га (${fmt(b.burned_fraction * 100)}% площади)</td></tr>
      <tr><td>Тяжесть по очагу</td><td>${esc(b.severity)}${Number.isFinite(b.dnbr_burned) ? ", dNBR очага " + fmt(b.dnbr_burned, 2) : ""}</td></tr>
      <tr><td>Средний dNBR по участку</td><td>${fmt(b.dnbr, 3)}</td></tr>
      <tr><td>Сцены</td><td>${esc(b.scene_start)} (${esc(b.date_start || "—")}) → ${esc(b.scene_end)} (${esc(b.date_end || "—")})</td></tr>
      <tr><td>Источник сцен</td><td>${esc(b.source)}</td></tr>
      <tr><td>Надёжных пикселей</td><td>${fmt(b.valid_fraction * 100, 0)}%</td></tr></table>
    ${b.status === "insufficient_data" ? `<p class="hint">${esc((b.notes || []).join("; "))}</p>` : ""}`;
}

function renderChanges(data) {
  const cd = data.change_detection;
  if (!cd || cd.status !== "ok") { $("tab-changes").innerHTML = `<div class="placeholder">Анализ изменений недоступен${cd && cd.reason ? ": " + esc(cd.reason) : ""}.</div>`; return; }
  const und = cd.confirmed_cause === "причина не установлена";
  const cc = cd.carbon_contribution;
  const dr = cd.date_range ? `${cd.date_range[0]} … ${cd.date_range[1]}` : "—";
  $("tab-changes").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">Вероятная причина</div><div class="value" style="font-size:18px">${esc(cd.confirmed_cause)}</div><div class="sub"><span class="badge ${und ? "warn" : "ok"}">${und ? "данных для причины нет" : "подтверждено источниками"}</span></div></div>
      <div class="card"><div class="label">Потеря в периоде</div><div class="value">${fmt(cd.loss_area_ha, 1)} га</div><div class="sub">${cd.analyzed_area_ha ? fmt((cd.loss_area_ha / cd.analyzed_area_ha) * 100) : "—"}% площади · красным на карте</div></div>
      <div class="card"><div class="label">Интервал дат</div><div class="value" style="font-size:16px">${esc(dr)}</div></div>
      <div class="card"><div class="label">Не объяснено</div><div class="value">${fmt(cd.unexplained_area_ha, 1)} га</div></div>
      ${burnCard(cd.burn_index)}
    </div>
    ${burnBlock(cd.burn_index)}
    <table class="kv"><tr><td>Потеря до периода (не входит в ΔC)</td><td>${fmt(cd.pre_period_loss_ha, 1)} га</td></tr>
      <tr><td>Потеря после периода</td><td>${fmt(cd.post_period_loss_ha, 1)} га</td></tr>
      <tr><td>Прирост (CCI Change, 2019→2020)</td><td>${fmt(cd.gain_area_ha, 1)} га</td></tr>
      ${cc ? `<tr><td>Вклад потери в ΔC</td><td>${signed(cc.delta_c_tc)} т C (${cc.share_of_total_delta_c === null ? "—" : fmt(cc.share_of_total_delta_c * 100)}% ΔC территории), ${signed(cc.emissions_tco2e)} т CO₂-экв.</td></tr>` : ""}
      <tr><td>Растры взяты из</td><td>${esc(cd.source_aoi_id)}</td></tr></table>
    <div class="section-head"><h3>Доказательства по источникам</h3></div>
    <table class="kv grid">${cd.evidence.map((e) => `<tr><td><b>${esc(e.source)}</b></td><td>${esc(e.summary)}</td><td>${confirmsBadge(e.confirms)}</td></tr>`).join("")}</table>
    ${cd.notes.length ? `<div class="section-head"><h3>Примечания</h3></div><ul>${cd.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
}

function renderForecastShell(data) {
  const end = data.request.year_end;
  const years = [2026, 2027, 2028, 2029].filter((y) => y > end);
  $("tab-forecast").innerHTML = `
    <p class="hint">Сценарный прогноз единиц и их демонстрационной стоимости до конца периода кредитования (по правилам кейса — до 2029). Сценарий, а не предсказание.</p>
    <div class="row" style="max-width:420px"><label class="field grow"><span>Горизонт, год</span><select id="horizon-select">${(years.length ? years : [2029]).map((y) => `<option ${y === 2029 ? "selected" : ""}>${y}</option>`).join("")}</select></label>
      <button id="forecast-btn" class="btn primary" type="button">Рассчитать прогноз</button></div>
    <div id="forecast-result"></div>`;
  $("forecast-btn").addEventListener("click", runForecast);
}

async function runForecast() {
  const box = $("forecast-result");
  box.innerHTML = `<p class="hint">Считаю…</p>`;
  try {
    const d = await post("/forecast", { ...state.body, horizon_year: parseInt($("horizon-select").value, 10) });
    const rows = d.scenarios.flatMap((s) => ["correlated", "independent"].map((m) => {
      const p = s.at_horizon[m];
      return `<tr><td>${esc(s.title)}</td><td>${m}</td><td>${fmt(p.e_proj_tco2e, 0)}</td><td>${fmt(p.e_base_tco2e, 0)}</td><td>${fmt(p.r_tco2e, 0)}</td><td>${p.h_over_r === null ? "—" : fmt(p.h_over_r, 2)}</td><td><b>${p.q_units === null ? "—" : fmt(p.q_units, 0)}</b></td><td>${fmt(p.v_low, 0)} / ${fmt(p.v_base, 0)} / ${fmt(p.v_high, 0)}</td></tr>`;
    })).join("");
    box.innerHTML = `<p>Наблюдённый удельный темп: <b>${fmt(d.observed_annual_rate_tc_ha_yr, 3)}</b> т C/га/год; период кредитования ${d.year_start}–${d.horizon_year}.</p>
      <table class="kv grid"><tr><th>Сценарий</th><th>Ошибки</th><th>E_proj</th><th>E_base</th><th>R</th><th>H/R</th><th>Q</th><th>Стоимость, руб.</th></tr>${rows}</table>
      ${d.risks.length ? `<div class="section-head"><h3>Риски</h3></div><ul>${d.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <p class="hint">${esc(d.uncertainty_assumption)}</p><p class="hint">${esc(d.disclaimer)}</p>`;
  } catch (err) {
    box.innerHTML = `<p class="hint">Ошибка: ${esc(err.message)}</p>`;
  }
}

/* =============================================================================
   EVIDENCE — досье доказательств (CASE FILE)
   Каждое утверждение: OBSERVATION · SOURCE · EVIDENCE · INTERPRETATION · LIMITATION
   ============================================================================= */

function buildClaims(data) {
  const cd = data.change_detection || {}, sd = data.stock_difference, u = data.potential_units, b = data.baseline;
  const claims = [];

  claims.push({
    o: `Запас углерода изменился с ${fmt(data.stock_t0.mean_stock_tc_ha, 1)} до ${fmt(data.stock_t1.mean_stock_tc_ha, 1)} т C/га (${signed((data.stock_t1.mean_stock_tc_ha - data.stock_t0.mean_stock_tc_ha) / data.stock_t0.mean_stock_tc_ha * 100, 1)}%)`,
    source: "ESA CCI Biomass v7.0 — надземная биомасса (AGB)",
    evidence: `Растры за ${data.stock_t0.year} и ${data.stock_t1.year} год, покрытие ${fmt(data.covered_fraction * 100, 1)}% территории.`,
    interpretation: `Разность среднего запаса × площадь = ${signed(sd.delta_c_tc, 1)} т C, что соответствует ${signed(sd.emissions_tco2e, 0)} т CO₂-экв.`,
    limitation: "Продукт даёт годовую модельную оценку. Точность зависит от структуры древостоя и погрешности AGB_SD (см. AUDIT → Неопределённость).",
  });

  if (cd.status === "ok") {
    claims.push({
      o: `Потеря лесного покрова: ${fmt(cd.loss_area_ha, 1)} га (${cd.analyzed_area_ha ? fmt(cd.loss_area_ha / cd.analyzed_area_ha * 100, 1) : "—"}% площади)`,
      source: "Hansen Global Forest Change 2025 v1.13",
      evidence: `Пиксели с годом потери внутри периода ${data.request.year_start}–${data.request.year_end}, отображены красным на карте.`,
      interpretation: `Вклад в ΔC территории: ${cd.carbon_contribution ? signed(cd.carbon_contribution.delta_c_tc, 0) + " т C" : "не оценён (нет значимой потери)"}.`,
      limitation: "GFC даёт один год потери на пиксель и не определяет причину — причина устанавливается по независимым источникам ниже.",
    });

    claims.push({
      o: `Причина потери: «${cd.confirmed_cause}»`,
      source: (cd.evidence || []).map((e) => e.source).join(", ") || "—",
      evidence: (cd.evidence || []).map((e) => `${e.source}: ${e.summary} (${e.confirms === true ? "подтверждает" : e.confirms === false ? "не подтверждает" : "не применимо"})`).join(" · "),
      interpretation: cd.confirmed_cause === "причина не установлена"
        ? "Независимые источники не сходятся на одной причине — сервис не домысливает вывод."
        : `Причина подтверждена совпадением как минимум двух независимых источников (${(cd.evidence || []).filter((e) => e.confirms).length} из ${(cd.evidence || []).length}).`,
      limitation: cd.date_range ? `Даты события — с погрешностью источника (MODIS: до 7 суток). Интервал: ${cd.date_range.join(" … ")}.` : "Точная дата события не определена всеми источниками одинаково.",
    });

    if (cd.burn_index && cd.burn_index.status === "ok") {
      claims.push({
        o: `Спектральный след гари: ${cd.burn_index.fire_signature ? "подтверждён" : "не обнаружен"}, dNBR очага ${fmt(cd.burn_index.dnbr_burned, 2)}`,
        source: "Sentinel-2 L2A, индекс dNBR",
        evidence: `Сцены ${cd.burn_index.scene_start} → ${cd.burn_index.scene_end}, валидных пикселей ${fmt(cd.burn_index.valid_fraction * 100, 0)}%.`,
        interpretation: `Тяжесть по шкале USGS/FIREMON: ${cd.burn_index.severity}. Метод независим от MODIS — использует другой физический принцип (отражение SWIR).`,
        limitation: (cd.burn_index.notes || []).join("; ") || "Требуется безоблачная пара сцен до/после события.",
      });
    }
  }

  claims.push({
    o: `Базовая линия: E_base = ${signed(b.emissions_tco2e)} т CO₂-экв.`,
    source: "Продолжение тренда 2015–2019 участка-родителя (условие кейса)",
    evidence: `Покрытие ${fmt(b.covered_fraction * 100)}%, статус «${b.status}».`,
    interpretation: "Это сценарное допущение кейса, а не независимая оценка дополнительности проекта.",
    limitation: "Знак и величина R чувствительны к выбору окна истории — см. AUDIT → чувствительность в исследовании.",
  });

  claims.push({
    o: `Потенциальные единицы: ${u.q_units === null ? "не рассчитаны" : fmt(u.q_units, 0)}`,
    source: "Правила расчёта единиц кейса (R, H, H/R, UNC, резерв B, округление вниз)",
    evidence: `R = ${fmt(u.r_tco2e)}, H = ${fmt(u.h_tco2e)}, H/R = ${u.h_over_r === null ? "—" : fmt(u.h_over_r, 2)}.`,
    interpretation: u.reason || "Единицы возникают только при превышении результата над базовой линией за вычетом неопределённости и резерва.",
    limitation: "Единицы НЕ сертифицированы и не заменяют верификацию по методике сертификации.",
  });

  return claims;
}

function renderEvidenceHead(data) {
  $("evidence-head").innerHTML = `
    <div class="hero-tag" style="margin-bottom:18px">
      <span class="eyebrow accent">CASE FILE / EVIDENCE</span>
      <span class="badge muted">${esc(data.request.aoi_id || data.request.region_name || "участок")}</span>
      <span class="badge muted">${data.request.year_start}–${data.request.year_end}</span>
      <span class="badge accent">${(data.sources || []).length} источников</span>
    </div>`;
}

function renderEvidence(data) {
  const claims = buildClaims(data);
  $("tab-evidence").innerHTML = `
    <p class="hint" style="margin-bottom:16px">Каждое утверждение расчёта раскрыто по пяти полям: что наблюдали, из какого источника, какое именно доказательство, как это интерпретируется и какое у него ограничение. Нажмите строку, чтобы развернуть.</p>
    <div class="ledger">${claims.map((c, i) => `
      <div class="claim" data-i="${i}">
        <div class="claim-head">
          <div class="claim-no">${String(i + 1).padStart(2, "0")}</div>
          <div class="claim-ttl"><div class="t">${esc(c.o)}</div><div class="o">${esc(c.source)}</div></div>
          <span class="chev">${ICON.chev}</span>
        </div>
        <div class="claim-body">
          <div class="claim-grid">
            <div class="claim-cell"><div class="k">Observation</div><div class="v">${esc(c.o)}</div></div>
            <div class="claim-cell"><div class="k">Source</div><div class="v">${esc(c.source)}</div></div>
            <div class="claim-cell"><div class="k">Evidence</div><div class="v">${esc(c.evidence)}</div></div>
            <div class="claim-cell"><div class="k">Interpretation</div><div class="v">${esc(c.interpretation)}</div></div>
            <div class="claim-cell lim"><div class="k">Limitation</div><div class="v">${esc(c.limitation)}</div></div>
          </div>
        </div>
      </div>`).join("")}</div>`;
  document.querySelectorAll(".claim-head").forEach((h) => h.addEventListener("click", () => h.closest(".claim").classList.toggle("open")));
}

function renderChecklist(data) {
  const ck = data.insights.checklist;
  const verdict = { pass: "Все проверки пройдены", warn: "Есть замечания, требующие внимания", fail: "Есть непройденные проверки — результат принимать нельзя" }[ck.verdict];
  $("tab-checklist").innerHTML = `
    <div class="verdict ${ck.verdict}"><b>${verdict}.</b> Пройдено: ${ck.counts.pass}, внимание: ${ck.counts.warn}, не пройдено: ${ck.counts.fail}, сведения: ${ck.counts.info}.</div>
    ${ck.items.map((i) => `<div class="check"><span class="dot ${i.status}">${CHECK_MARK[i.status]}</span><div><div class="t">${esc(i.title)} <span class="badge ${i.status}">${STATUS_RU[i.status]}</span></div><div class="d">${esc(i.detail)}</div></div></div>`).join("")}
    <p class="hint" style="margin-top:12px">Чек-лист формируется автоматически из результата расчёта и не заменяет работу верификатора.</p>`;
}

function renderSources(data) {
  $("tab-sources").innerHTML = data.sources.map((s) => `<div class="src-item"><div class="st">${esc(s.product)}</div><div class="sa">${esc(s.required_attribution)}</div><div class="sm">Доступ: ${esc(s.access_date)}. Ограничения: ${esc(s.limitations)}</div></div>`).join("");
}

/* =============================================================================
   AUDIT — дерево расчёта, качество данных, неопределённость, файлы, JSON
   ============================================================================= */

function renderAuditHead(data) {
  $("audit-head").innerHTML = `
    <div class="hero-tag" style="margin-bottom:18px">
      <span class="eyebrow accent">CALCULATION · PROVENANCE · UNCERTAINTY</span>
      <span class="badge muted">calc_id ${esc(data.meta.calculation_id.slice(0, 10))}…</span>
    </div>`;
}

function renderCalcTree(data) {
  const t0 = data.stock_t0, t1 = data.stock_t1, sd = data.stock_difference, params = data.meta.params;
  const steps = [
    { name: "INPUT · AGB RASTER", out: `${fmt(t0.mean_stock_tc_ha, 2)} / ${fmt(t1.mean_stock_tc_ha, 2)}`, unit: "т C/га",
      fx: null, meta: [`ESA CCI Biomass v7.0`, `${t0.year} → ${t1.year}`, `покрытие ${fmt(data.covered_fraction * 100, 1)}%`] },
    { name: "STOCK", out: `${fmt(t0.total_stock_tc, 0)} → ${fmt(t1.total_stock_tc, 0)}`, unit: "т C",
      fx: `Stock = mean_stock_tc_ha × Area(${fmt(data.area_ha, 1)} га)`, meta: [`Area = ${fmt(data.area_ha, 3)} га`] },
    { name: "ΔC · CARBON", out: signed(sd.delta_c_tc, 0), unit: "т C", hi: true,
      fx: `ΔC = Stock(${t1.year}) − Stock(${t0.year}) = ${fmt(t1.total_stock_tc, 0)} − ${fmt(t0.total_stock_tc, 0)} = ${signed(sd.delta_c_tc, 0)}`,
      meta: [`годовой темп: ${signed(sd.delta_c_tc / (t1.year - t0.year), 2)} т C/год`] },
    { name: "CO₂e · EMISSIONS", out: signed(sd.emissions_tco2e, 0), unit: "т CO₂-экв.", hi: true,
      fx: `E = −ΔC × 44/12 = −(${signed(sd.delta_c_tc, 0)}) × ${fmt(params.co2_per_c, 4)} = ${signed(sd.emissions_tco2e, 0)}`,
      meta: [`Пул: живая надземная биомасса (AGB)`, `CF = ${params.cf_agb}`] },
    { name: "R · POTENTIAL", out: data.potential_units.r_tco2e === null ? "—" : signed(data.potential_units.r_tco2e, 0), unit: "т CO₂-экв.",
      fx: `R = E_base − E_proj − LK = ${signed(data.baseline.emissions_tco2e, 0)} − ${signed(sd.emissions_tco2e, 0)} − ${params.lk} = ${data.potential_units.r_tco2e === null ? "—" : signed(data.potential_units.r_tco2e, 0)}`,
      meta: [`E_base из базовой линии`, `LK = ${params.lk} (нет утечки за пределы участка — допущение сценария)`] },
    { name: "Q · UNITS", out: data.potential_units.q_units === null ? "—" : fmt(data.potential_units.q_units, 0), unit: "единиц",
      fx: `Q = floor(max(0, R − H) × (1 − BUF)), H/R ≥ 1 ⇒ Q = 0`,
      meta: [`UNC = ${(params.unc_allowance * 100).toFixed(0)}%`, `BUF = ${(params.buf * 100).toFixed(0)}%`, data.potential_units.reason || "правила соблюдены"] },
  ];
  $("tab-calc").innerHTML = `
    <p class="hint" style="margin-bottom:16px">Каждый шаг — с исходными значениями и формулой этого конкретного расчёта, не примером.</p>
    <div class="calc-tree">${steps.map((s, i) => `
      ${i ? `<div class="calc-link">${ICON.down}</div>` : ""}
      <div class="calc-step ${s.hi ? "hi" : ""}">
        <div class="sh"><span class="nm">${esc(s.name)}</span><span class="out">${esc(s.out)}<span class="u">${esc(s.unit)}</span></span></div>
        ${s.fx ? `<div class="fx">${esc(s.fx)}</div>` : ""}
        <div class="meta">${s.meta.map((m) => `<span class="prov"><span>${esc(m)}</span></span>`).join("")}</div>
      </div>`).join("")}</div>`;
}

function dqStatus(data) {
  const cov = data.covered_fraction;
  const cd = data.change_detection || {};
  const items = [
    { n: "Запас углерода (AGB)", ok: cov >= 0.999 ? "available" : cov > 0 ? "partial" : "missing", d: `Покрытие ${fmt(cov * 100, 1)}% площади продуктом ESA CCI Biomass.` },
    { n: "Потеря лесного покрова", ok: cd.status === "ok" ? "available" : "missing", d: cd.status === "ok" ? "Hansen GFC покрывает территорию полностью." : "Анализ изменений недоступен для этого запроса." },
    { n: "Причина изменений", ok: cd.status !== "ok" ? "missing" : cd.confirmed_cause === "причина не установлена" ? "limited" : "available", d: cd.status !== "ok" ? "Нет данных для установления причины." : cd.confirmed_cause === "причина не установлена" ? "Независимые источники не сходятся — причина не установлена намеренно, а не по ошибке." : "Причина подтверждена независимыми источниками." },
    { n: "Спектральный след гари", ok: !cd.burn_index ? "missing" : cd.burn_index.status === "ok" ? "available" : "limited", d: (!cd.burn_index || cd.burn_index.status !== "ok") ? "Нет безоблачной пары сцен Sentinel-2 до/после события." : "dNBR посчитан по паре сцен Sentinel-2." },
    { n: "Базовая линия", ok: data.baseline.status === "ok" ? "available" : data.baseline.status === "partial" ? "partial" : "missing", d: data.baseline.reason || "Покрытие " + fmt(data.baseline.covered_fraction * 100, 0) + "%." },
    { n: "Целостность файлов (SHA-256)", ok: (data.data_provenance || []).every((f) => f.verified) ? "available" : "limited", d: `${(data.data_provenance || []).filter((f) => f.verified).length} из ${(data.data_provenance || []).length} файлов совпали с каталогом.` },
  ];
  return items;
}

function renderDataQuality(data) {
  const items = dqStatus(data);
  const STATUS_LABEL = { available: "AVAILABLE", partial: "PARTIAL", limited: "LIMITED", missing: "NOT AVAILABLE" };
  const canDo = [], cannotDo = [];
  for (const it of items) {
    if (it.ok === "available") canDo.push(it.n);
    else cannotDo.push(`${it.n} — ${it.d}`);
  }
  const u = data.potential_units;
  if (u.status !== "ok") cannotDo.push(`Потенциальные единицы — ${u.reason || "недостаточно данных"}`);
  else canDo.push("Потенциальные единицы (по правилам кейса)");

  $("tab-quality").innerHTML = `
    <div class="dq-grid">${items.map((it) => `<div class="dq ${it.ok}"><div class="n">${esc(it.n)}</div><div class="st">${STATUS_LABEL[it.ok]}</div><div class="d">${esc(it.d)}</div></div>`).join("")}</div>
    <div class="can-cannot">
      <div class="cc yes"><h4>Что можно посчитать</h4><ul>${canDo.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>
      <div class="cc no"><h4>Что нельзя посчитать</h4><ul>${cannotDo.length ? cannotDo.map((c) => `<li>${esc(c)}</li>`).join("") : "<li>Ограничений для этого расчёта нет.</li>"}</ul></div>
    </div>
    <p class="hint">Статусы соответствуют кодам сервиса (<code>NO_BIOMASS_DATA</code>, <code>OUT_OF_BASELINE_COVERAGE</code> и т.п.) — при отсутствии данных сайт не достраивает результат, а объясняет причину.</p>`;
}

function renderUncertainty(data) {
  const u = data.uncertainty, si = u.sensitivity_independent;
  $("tab-uncertainty").innerHTML = `
    <table class="kv"><tr><td>Диапазон [L, U] — correlated (основной)</td><td>[${fmt(u.l, 0)}; ${fmt(u.u, 0)}] т CO₂-экв.</td></tr>
      <tr><td>SD(E), correlated</td><td>${fmt(u.sd_e, 0)}</td></tr>
      <tr><td>Диапазон [L, U] — independent (чувствительность)</td><td>[${fmt(si.l, 0)}; ${fmt(si.u, 0)}] т CO₂-экв.</td></tr>
      <tr><td>SD(E), independent</td><td>${fmt(si.sd_e, 0)}</td></tr>
      <tr><td>Метод</td><td><code>${esc(u.method)}</code></td></tr></table>
    <div class="section-head"><h3>Допущения</h3></div><div class="assumptions">${esc(u.assumptions)}</div>
    <p class="hint" style="margin-top:12px">Это диапазон сценариев расчёта, а не статистический доверительный интервал: Data Quality Score не отражает вероятность правильности результата.</p>`;
}

function renderDataTab(data) {
  const rows = data.data_provenance.map((f) => `<tr><td>${esc(f.relative_path)}</td><td>${esc(f.product_version || "—")}</td><td>${esc(f.period || "—")}</td><td>${esc(f.retrieved_or_created_date || "—")}</td><td><code>${f.sha256 ? esc(f.sha256.slice(0, 12)) + "…" : "—"}</code></td><td><span class="badge ${f.verified ? "ok" : "fail"}">${f.verified ? "совпал" : "НЕ совпал"}</span></td></tr>`).join("");
  $("tab-data").innerHTML = `
    <div class="section-head" style="margin-top:0"><h3>Файлы, прочитанные расчётом</h3></div>
    <p class="hint">Локальные копии сверяются по размеру и SHA-256 — расчёт воспроизводим без внешних источников.</p>
    <table class="kv grid"><tr><th>Файл</th><th>Версия</th><th>Период</th><th>Получен</th><th>SHA-256</th><th>Проверка</th></tr>${rows}</table>
    <div class="section-head"><h3>Открытый источник по параметрам запроса</h3></div>
    <p class="hint">Живой запрос сцен Sentinel-2 L2A из каталога Earth Search STAC по территории и периоду (лето каждого года).</p>
    <button id="s2-btn" class="btn" style="width:auto" type="button">Запросить сцены Sentinel-2</button><div id="s2-result"></div>`;
  $("s2-btn").addEventListener("click", searchSentinel2);
}

async function searchSentinel2() {
  const box = $("s2-result");
  box.innerHTML = `<p class="hint">Запрос к Earth Search…</p>`;
  try {
    const d = await post("/data/sentinel2", state.body);
    if (d.status !== "live") { box.innerHTML = `<p style="margin-top:8px"><span class="badge warn">источник недоступен</span> ${esc(d.reason)}</p>`; return; }
    const local = d.scenes.filter((s) => s.in_local_dataset).length;
    box.innerHTML = `<p style="margin-top:8px"><span class="badge ok">live</span> ${esc(d.source)}: ${d.scenes.length} сцен, в локальном наборе: ${local}</p>
      <table class="kv grid"><tr><th>Сцена</th><th>Дата</th><th>Облачность</th><th>Локально</th></tr>${d.scenes.map((s) => `<tr><td>${esc(s.item_id)}</td><td>${esc(s.datetime_utc.slice(0, 10))}</td><td>${s.cloud_cover === null ? "—" : fmt(s.cloud_cover) + "%"}</td><td><span class="badge ${s.in_local_dataset ? "ok" : "muted"}">${s.in_local_dataset ? "есть" : "только в каталоге"}</span></td></tr>`).join("")}</table>`;
  } catch (err) {
    box.innerHTML = `<p style="margin-top:8px"><span class="badge warn">ошибка</span> ${esc(err.message)}</p>`;
  }
}

/* =============================================================================
   ПОРТФЕЛЬ (нижняя панель экрана карты)
   ============================================================================= */

async function loadPortfolio(silent) {
  const box = $("tab-portfolio");
  if (state.overviewLoading) return;
  state.overviewLoading = true;
  if (!silent || !state.overview) box.innerHTML = `<div class="placeholder"><b>Считаю портфель…</b><br>Сводка по каждому региону собирается из полного расчёта (первый раз до ~20 с).</div>`;
  try {
    state.overview = await api("/overview");
    renderPortfolio();
    renderAoiList();
    drawPortfolioOnMap();
  } catch (err) {
    box.innerHTML = `<div class="placeholder">Не удалось построить портфель: ${esc(err.message)}</div>`;
  } finally {
    state.overviewLoading = false;
  }
}

function regionCard(r) {
  const level = r.risk.overall_level;
  return `<div class="pf-card" data-open="${esc(r.aoi_id)}">
    <div class="h"><div><div class="nm">${esc(r.name)}</div><div class="rg">${esc(r.region).toUpperCase()} · ${fmt(r.area_ha, 0)} ГА</div></div>
      <span class="badge ${level}">${LEVEL_RU[level]}</span></div>
    <div class="sp">${spark(r.series)}</div>
    <div class="st">
      <div><div class="k">ЗАПАС 19–24</div><div class="v" style="color:${r.stock_change_pct < 0 ? RED : GREEN}">${signed(r.stock_change_pct, 1)}%</div></div>
      <div><div class="k">E, T CO2E</div><div class="v">${signed(r.emissions_tco2e)}</div></div>
      <div><div class="k">ПОТЕРЯ ЛЕСА</div><div class="v">${fmt(r.loss_area_ha, 1)} га</div></div>
      <div><div class="k">ПРИЧИНА</div><div class="v" style="font-size:11px">${esc(r.cause || "—")}</div></div>
    </div>
  </div>`;
}

function renderPortfolio() {
  const o = state.overview;
  if (!o) return;
  const t = o.totals;
  $("sheet-count").textContent = `${t.regions} УЧАСТКОВ`;
  $("tab-portfolio").innerHTML = `
    <div class="pf-totals">
      <div class="t"><div class="k">Regions</div><div class="v">${t.regions}</div></div>
      <div class="t"><div class="k">Area, ha</div><div class="v">${fmt(t.area_ha, 0)}</div></div>
      <div class="t"><div class="k">Loss, ha</div><div class="v">${fmt(t.loss_area_ha, 0)}</div></div>
      <div class="t"><div class="k">Net E, tCO2e</div><div class="v" style="color:${t.net_emissions_tco2e > 0 ? RED : GREEN}">${signed(t.net_emissions_tco2e)}</div></div>
      <div class="t"><div class="k">High risk</div><div class="v" style="color:${RED}">${t.risk_levels.high}</div></div>
      <div class="t"><div class="k">Units</div><div class="v">${fmt(t.units, 0)}</div></div>
    </div>
    <div class="pf-grid">${o.regions.map(regionCard).join("")}</div>`;
}

/* =============================================================================
   renderAll — вызывается после успешного /analysis
   ============================================================================= */

function renderAll(data, gotoLanding) {
  unlockResultNav();
  renderResultHero(data);
  renderChain(data);
  renderSummary(data);
  renderDynamicsExtra(data);
  renderChanges(data);
  renderRecommendations(data);
  renderRisks(data);
  renderForecastShell(data);

  renderEvidenceHead(data);
  renderEvidence(data);
  renderChecklist(data);
  renderSources(data);

  renderAuditHead(data);
  renderCalcTree(data);
  renderDataQuality(data);
  renderUncertainty(data);
  renderDataTab(data);
  $("raw-json").textContent = JSON.stringify(data, null, 2);

  drawLoss(data.change_detection);
  animateCountUp();
  if (gotoLanding) { goTab("result", "summary"); goTab("evidence", "evidence"); goTab("audit", "calc"); }
  else if (state.tab.result === "dynamics") renderDynamicsChart(data);
}

/* =============================================================================
   БОБЁР РУТ — плавающий помощник
   ============================================================================= */

const RUT_FACTS = [
  "Бобров называют «инженерами экосистем»: их плотины меняют ландшафт и водный режим целых долин.",
  "Резцы бобра растут всю жизнь — поэтому ему всегда есть что грызть.",
  "Лес хранит углерод в стволах, корнях и почве. Мы считаем только живую надземную биомассу.",
  "Пожар выпускает не весь углерод сразу: часть остаётся в мёртвой древесине и почве.",
];
const RUT_ROLE_TIPS = {
  verifier: ["Начните с EVIDENCE → «Проверка»: если файлы не совпали по SHA-256, дальше можно не читать.", "«Причина не установлена» — честный ответ, а не ошибка. Просите у заявителя документы.", "Сравните оба сценария неопределённости в AUDIT: они отличаются на порядки."],
  investor: ["Единицы возникают только при превышении над базовой линией — смотрите RESULT → «Риски и единицы».", "Стоимость единиц демонстрационная. Не закладывайте её в финансовую модель.", "Загляните в «Прогноз»: как результат меняется до 2029 года."],
  owner: ["Во вкладке «Рекомендации» виден разрыв до базовой линии в тоннах и гектарах.", "Заявляйте полный контур проекта целиком: выборочный отбор верификатор не примет.", "Защита от пожаров — самый быстрый способ удержать запас."],
};
const RUT_TAB_TIPS = {
  dynamics: "На графике — средний запас углерода по годам. Падение — потеря, рост — накопление.",
  changes: "Красным на карте — пиксели потери леса. Если причины нет в доказательствах, сайт так и скажет.",
  forecast: "Прогноз — это сценарий, а не предсказание. Допущения написаны под таблицей.",
  evidence: "Каждая карточка — наблюдение, источник, доказательство, интерпретация и ограничение.",
  calc: "Дерево расчёта — с реальными числами этого запуска на каждом шаге, не примером.",
};
let rutTimer = null, rutTips = 0, rutClicks = [];

function rutSay(html, ms = 6500) {
  const bubble = $("rut-bubble");
  bubble.innerHTML = html;
  bubble.classList.remove("hidden");
  clearTimeout(rutTimer);
  rutTimer = setTimeout(() => bubble.classList.add("hidden"), ms);
}

function rutNextTip() {
  const pool = [];
  if (RUT_TAB_TIPS[state.tab[state.view]]) pool.push(RUT_TAB_TIPS[state.tab[state.view]]);
  pool.push(...(RUT_ROLE_TIPS[role()] || []), ...RUT_FACTS);
  rutSay(`<b>Бобёр РУТ:</b> ${esc(pool[rutTips++ % pool.length])}`, 9000);
}

function rutEasterEgg() {
  const btn = $("rut-btn");
  btn.classList.remove("wiggle"); void btn.offsetWidth; btn.classList.add("wiggle");
  rutSay("<b>Привет от команды РУТ!</b> Бобры строят плотины, а мы — прозрачную проверку лесных проектов. Спасибо, что проверяете леса вместе с нами.", 10000);
  const colors = [GREEN, ACCENT, "#a2734a", AMBER];
  for (let i = 0; i < 32; i++) {
    const leaf = document.createElement("div");
    leaf.className = "leaf";
    leaf.style.left = Math.random() * 100 + "vw";
    leaf.style.background = colors[i % colors.length];
    leaf.style.animationDuration = 2.2 + Math.random() * 2.4 + "s";
    leaf.style.animationDelay = Math.random() * 0.7 + "s";
    document.body.appendChild(leaf);
    setTimeout(() => leaf.remove(), 6000);
  }
}

function initRut() {
  const click = () => {
    const now = Date.now();
    rutClicks = rutClicks.filter((t) => now - t < 2500);
    rutClicks.push(now);
    if (rutClicks.length >= 5) { rutClicks = []; rutEasterEgg(); } else rutNextTip();
  };
  $("rut-btn").addEventListener("click", click);
  $("credit-btn").addEventListener("click", (e) => { e.preventDefault(); rutEasterEgg(); });
  $("rut-bubble").addEventListener("click", () => $("rut-bubble").classList.add("hidden"));
}

function rutReact(data) {
  const level = data.insights.risk.overall_level;
  const msg = { high: "Ого, риск высокий. Загляните в «Рекомендации» — там, что можно сделать.", medium: "Риск средний: есть над чем подумать. Посмотрите факторы в «Риски и единицы».", low: "Тут всё спокойно. Проверьте, что причины изменений и данные подтверждены." }[level];
  rutSay(`<b>Бобёр РУТ:</b> ${esc(msg)}`, 8000);
}

/* =============================================================================
   ЗАПУСК
   ============================================================================= */

async function init() {
  initEntry();
  initNav();
  initTabs();
  initRut();
  initSelectors();
  initLayerControls();
  initSheet();
  initMap();

  try { state.roles = await api("/roles"); } catch (_) { state.roles = ROLE_FALLBACK; }

  const savedRole = loadStored(LS_ROLE), savedName = loadStored(LS_NAME);
  if (savedRole && state.roles[savedRole]) {
    state.role = savedRole;
    state.name = savedName || "";
    applyRole();
  } else {
    showRoleScreen(false);
  }

  await loadRegions();
  loadPortfolio();
}

document.addEventListener("DOMContentLoaded", init);
