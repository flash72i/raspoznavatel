/* ==========================================================================
   «Распознаватель» v2.2 — app.js
   Одностраничный интерфейс. Все вызовы Python-API проходят через обёртку
   callApi(), которая логирует ЛЮБУЮ ошибку (в тост и в журнал программы) —
   «немых» сбоев больше не бывает.
   ========================================================================== */
"use strict";

const $ = (s) => document.querySelector(s);
const pyapi = () => (window.pywebview && window.pywebview.api) || null;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const state = {
  imagePath: null,
  imageName: null,
  mode: "text",
  resultText: "",
  settings: {
    ocr_langs: "auto", auto_copy: true, smart_hotkey: true,
    hotkey_recognize: "ctrl+shift+s", hotkey_screenshot: "ctrl+shift+p",
    autostart: false, preload: true,
  },
  shotCount: 0,
  recording: null,   // какое поле горячей клавиши сейчас записывается
};

/* ---------------------------------------------------------------------------
   ЕДИНАЯ обёртка вызовов Python-API: ошибки -> тост + журнал Python
--------------------------------------------------------------------------- */
async function callApi(method, ...args) {
  const a = pyapi();
  if (!a) {
    const m = "Python-API недоступен (окно ещё инициализируется?)";
    toast(m, "error");
    throw new Error(m);
  }
  try {
    return await a[method](...args);
  } catch (e) {
    const m = `Сбой вызова ${method}: ${e && e.message ? e.message : e}`;
    toast(m, "error", 6000);
    try { await a.js_error(m); } catch (_) {}
    throw e;
  }
}

/* Неперехваченные ошибки/обещания — тоже в журнал */
window.addEventListener("error", (e) => {
  const a = pyapi();
  if (a) { try { a.js_error(`JS: ${e.message} @${e.filename}:${e.lineno}`); } catch (_) {} }
});
window.addEventListener("unhandledrejection", (e) => {
  const a = pyapi();
  if (a) { try { a.js_error(`JS promise: ${e.reason}`); } catch (_) {} }
});

/* ---------------------------------------------------------------------------
   Тосты / статус / лоадер
--------------------------------------------------------------------------- */
const TOAST_ICONS = {
  success: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 15l-5-5 1.4-1.4L11 14.2l6.6-6.6L19 9l-8 8z"/></svg>',
  error: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm5 13.6L15.6 17 12 13.4 8.4 17 7 15.6 10.6 12 7 8.4 8.4 7 12 10.6 15.6 7 17 8.4 13.4 12 17 15.6z"/></svg>',
  info: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>',
};
function toast(message, type = "info", ms = 3400) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = (TOAST_ICONS[type] || TOAST_ICONS.info) + `<span>${escapeHtml(message)}</span>`;
  $("#toastStack").appendChild(el);
  setTimeout(() => { el.classList.add("hide"); setTimeout(() => el.remove(), 320); }, ms);
}

function setStatus(text, mode = "idle") {
  $("#statusText").textContent = text;
  $("#statusDot").className = "status-dot" + (mode === "busy" ? " busy" : mode === "error" ? " error" : "");
}
let loaderCount = 0;
function showLoader(text = "Обработка…") {
  loaderCount++;
  $("#loaderText").textContent = text;
  $("#loaderOverlay").classList.remove("hidden");
  setStatus(text, "busy");
}
function hideLoader() {
  loaderCount = Math.max(0, loaderCount - 1);
  if (loaderCount === 0) $("#loaderOverlay").classList.add("hidden");
}

/* Колбэки из Python */
window.onStatus = (t) => setStatus(t);
window.onError = (t) => { toast(t, "error", 6000); setStatus("Ошибка: " + t, "error"); };

/* --- цветной журнал: ERROR — красный, WARN — жёлтый, OK — зелёный --- */
function logLevelOf(line) {
  if (line.indexOf("[ERROR]") >= 0) return "err";
  if (line.indexOf("[WARN]") >= 0) return "warn";
  if (line.indexOf("[OK]") >= 0) return "ok";
  return "info";
}
function appendLogLines(lines) {
  const box = $("#logBox");
  const empty = $("#logEmpty");
  if (empty) empty.remove();
  const frag = document.createDocumentFragment();
  for (const ln of lines) {
    if (!ln) continue;
    const d = document.createElement("div");
    d.className = "log-line log-" + logLevelOf(ln);
    d.textContent = ln;
    frag.appendChild(d);
  }
  box.appendChild(frag);
  if ($("#settingsDrawer").classList.contains("open")) box.scrollTop = box.scrollHeight;
}
function logBoxText() {
  return Array.from(document.querySelectorAll("#logBox .log-line"))
    .map((d) => d.textContent).join("\n");
}
window.onLogEntry = (line) => appendLogLines([line]);
/* Пакетная доставка строк журнала, накопленных до загрузки страницы */
window.onLogBulk = (lines) => appendLogLines(Array.isArray(lines) ? lines : [lines]);

/* ---------------------------------------------------------------------------
   Typewriter (React Bits)
--------------------------------------------------------------------------- */
function typeWriter(el, text, speed = 12) {
  el.textContent = "";
  el.classList.add("typing-caret");
  let i = 0;
  const step = () => {
    i = Math.min(text.length, i + Math.max(1, Math.ceil(text.length / 240)));
    el.textContent = text.slice(0, i);
    el.scrollTop = el.scrollHeight;
    if (i < text.length) setTimeout(step, speed);
    else el.classList.remove("typing-caret");
  };
  step();
}
function typeIntoTextarea(ta, text, speed = 10) {
  ta.value = "";
  let i = 0;
  const step = () => {
    i = Math.min(text.length, i + Math.max(1, Math.ceil(text.length / 280)));
    ta.value = text.slice(0, i);
    ta.scrollTop = ta.scrollHeight;
    if (i < text.length) setTimeout(step, speed);
  };
  step();
}

/* ---------------------------------------------------------------------------
   Ripple
--------------------------------------------------------------------------- */
document.addEventListener("pointerdown", (e) => {
  const btn = e.target.closest(".ripple");
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const size = Math.max(r.width, r.height);
  const m = document.createElement("span");
  m.className = "ripple-mark";
  m.style.width = m.style.height = size + "px";
  m.style.left = e.clientX - r.left - size / 2 + "px";
  m.style.top = e.clientY - r.top - size / 2 + "px";
  btn.appendChild(m);
  setTimeout(() => m.remove(), 600);
});

/* ---------------------------------------------------------------------------
   ФОН: частицы
--------------------------------------------------------------------------- */
function initParticles() {
  const canvas = $("#particles");
  const ctx = canvas.getContext("2d");
  let w, h, pts = [];
  const N = 42, D = 115;
  function resize() { w = canvas.width = innerWidth; h = canvas.height = innerHeight; }
  function spawn() {
    pts = Array.from({ length: N }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22,
      r: Math.random() * 1.5 + .5,
      c: Math.random() < .5 ? "34,211,238" : "139,92,246",
    }));
  }
  let last = 0;
  function frame(ts) {
    requestAnimationFrame(frame);
    if (ts - last < 33) return;
    last = ts;
    ctx.clearRect(0, 0, w, h);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > w) p.vx *= -1;
      if (p.y < 0 || p.y > h) p.vy *= -1;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283);
      ctx.fillStyle = `rgba(${p.c},.45)`; ctx.fill();
    }
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const a = pts[i], b = pts[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy;
      if (d2 < D * D) {
        ctx.strokeStyle = `rgba(125,160,255,${(1 - Math.sqrt(d2) / D) * .12})`;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
    }
  }
  resize(); spawn();
  addEventListener("resize", () => { resize(); spawn(); });
  requestAnimationFrame(frame);
}

/* ---------------------------------------------------------------------------
   Панель заголовка + ресайз
--------------------------------------------------------------------------- */
function initWindowControls() {
  $("#btnMin").addEventListener("click", async () => {
    try { await callApi("win_minimize"); } catch (e) {}
  });
  $("#btnMax").addEventListener("click", async () => {
    try { await callApi("win_maximize_toggle"); } catch (e) {}
  });
  $("#btnClose").addEventListener("click", async () => {
    try { await callApi("win_close"); } catch (e) {}
  });
  $("#titlebar").addEventListener("dblclick", async (e) => {
    if (e.target.closest(".tb-btn")) return;
    try { await callApi("win_maximize_toggle"); } catch (err) {}
  });

  window.onMaximized = (on) => {
    $("#btnMax").innerHTML = on
      ? '<svg viewBox="0 0 12 12"><rect x="2.2" y="3.8" width="6.2" height="6.2" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M3.8 3.8v-1a.8.8 0 0 1 .8-.8h5.2a.8.8 0 0 1 .8.8v5.2a.8.8 0 0 1-.8.8h-1" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>'
      : '<svg viewBox="0 0 12 12"><rect x="2.2" y="2.2" width="7.6" height="7.6" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
    $("#btnMax").setAttribute("data-tooltip", on ? "Восстановить размер" : "Развернуть");
  };

  function bindResize(el, useX, useY) {
    let sx = 0, sy = 0, sw = 0, sh = 0, raf = null, cur = null;
    const down = (e) => {
      e.preventDefault();
      sx = e.screenX; sy = e.screenY;
      sw = document.documentElement.clientWidth;
      sh = document.documentElement.clientHeight;
      addEventListener("mousemove", move);
      addEventListener("mouseup", up);
    };
    const move = (e) => {
      cur = [useX ? Math.max(1024, sw + (e.screenX - sx)) : null,
             useY ? Math.max(640, sh + (e.screenY - sy)) : null];
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        if (cur) callApi("win_resize", cur[0] != null ? cur[0] : sw, cur[1] != null ? cur[1] : sh).catch(() => {});
      });
    };
    const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
    el.addEventListener("mousedown", down);
  }
  bindResize($("#rzR"), true, false);
  bindResize($("#rzB"), false, true);
  bindResize($("#rzBR"), true, true);
}

/* ---------------------------------------------------------------------------
   Изображения: кнопка + Drag&Drop
--------------------------------------------------------------------------- */
function setPreview(base64, name) {
  $("#previewImg").src = base64;
  $("#previewName").textContent = name || "";
  $("#previewCard").classList.remove("hidden");
}

async function pickImage() {
  showLoader("Открытие файла…");
  try {
    const r = await callApi("pick_image");
    if (r && r.path) {
      state.imagePath = r.path; state.imageName = r.name;
      setPreview(r.base64, r.name);
      setStatus("Файл загружен: " + r.name);
      toast("Изображение загружено", "success");
    } else if (r && r.error) {
      toast(r.error, "error", 6000);
    }
  } catch (e) { /* callApi уже сообщил */ }
  finally { hideLoader(); }
}

async function handleDrop(file) {
  if (!/\.(png|jpe?g|bmp|gif|tiff?|webp)$/i.test(file.name)) {
    toast("Поддерживаются изображения: PNG, JPG, BMP, GIF, TIFF, WEBP", "error");
    return;
  }
  showLoader("Чтение файла…");
  try {
    const buf = await file.arrayBuffer();
    let bin = "";
    const b = new Uint8Array(buf);
    for (let i = 0; i < b.length; i += 0x8000) bin += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    const r = await callApi("save_dropped_file", file.name, btoa(bin));
    if (r && r.ok) {
      state.imagePath = r.path; state.imageName = r.name;
      setPreview(r.base64, r.name);
      toast("Изображение загружено", "success");
    } else toast((r && r.error) || "Не удалось сохранить файл", "error");
  } catch (e) { /* уже сообщено */ }
  finally { hideLoader(); }
}

function initDropZone() {
  const z = $("#dropZone");
  z.addEventListener("click", pickImage);
  ["dragenter", "dragover"].forEach(ev => z.addEventListener(ev, e => { e.preventDefault(); z.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev => z.addEventListener(ev, e => { e.preventDefault(); z.classList.remove("dragover"); }));
  z.addEventListener("drop", e => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleDrop(f);
  });
}

/* ---------------------------------------------------------------------------
   Результаты
--------------------------------------------------------------------------- */
function showTextResult(text, title) {
  state.mode = "text";
  state.resultText = text;
  $("#qrResults").classList.add("hidden");
  const ta = $("#mainOutput");
  ta.classList.remove("hidden");
  typeIntoTextarea(ta, text);
  if (title) $("#resultTitle").textContent = title;
}

function showQrResult(results, title) {
  state.mode = "qr";
  state.resultText = (results || []).map(r => r.text).join("\n");
  $("#mainOutput").classList.add("hidden");
  const box = $("#qrResults");
  box.classList.remove("hidden");
  box.innerHTML = "";
  if (!results || !results.length) {
    box.innerHTML = '<p class="placeholder">Коды не найдены. Попробуйте выделить область ближе к коду.</p>';
    return;
  }
  results.forEach((r, i) => {
    const d = document.createElement("div");
    d.className = "qr-item";
    d.style.animationDelay = i * 80 + "ms";
    const isLink = /^https?:\/\//i.test(r.text.trim());
    d.innerHTML = `<div class="qr-type">${escapeHtml(r.type)}${r.engine ? " · " + escapeHtml(r.engine) : ""}</div>` +
      `<div class="qr-text${isLink ? " is-link" : ""}"></div>`;
    box.appendChild(d);
    typeWriter(d.querySelector(".qr-text"), r.text);
  });
  if (title) $("#resultTitle").textContent = title;
}

function currentResultText() {
  return state.mode === "qr" ? state.resultText : $("#mainOutput").value;
}

async function copyText(t, what) {
  if (!t || !t.trim()) { toast("Нечего копировать", "info"); return; }
  let ok = false;
  try { const r = await callApi("copy_to_clipboard", t); ok = r && r.ok; } catch (e) {}
  if (!ok) { try { await navigator.clipboard.writeText(t); ok = true; } catch (e) {} }
  toast(ok ? `${what} скопирован в буфер обмена` : "Не удалось скопировать", ok ? "success" : "error");
}

function autoCopy(t) {
  if (state.settings.auto_copy && t && t.trim() && pyapi()) {
    pyapi().copy_to_clipboard(t).catch(() => {});
  }
}

/* ---------------------------------------------------------------------------
   Действия
--------------------------------------------------------------------------- */
async function runOcrOnFile() {
  if (!state.imagePath) { toast("Сначала загрузите изображение", "info"); return; }
  showLoader("Определяю язык и распознаю текст…");
  try {
    const r = await callApi("ocr_image", state.imagePath, state.settings.ocr_langs);
    if (!r || !r.ok) { if (r && r.error) toast(r.error, "error", 6000); return; }
    if (!r.text || !r.text.trim()) { toast("Текст на изображении не найден", "info"); return; }
    const det = r.detected ? ` · язык: ${r.detected}` : "";
    showTextResult(r.text, "Распознанный текст" + det);
    toast(`Текст распознан${r.confidence != null ? ` (${r.confidence}%)` : ""}${det}`, "success");
    autoCopy(r.text);
  } catch (e) {}
  finally { hideLoader(); }
}

async function runQrOnFile() {
  if (!state.imagePath) { toast("Сначала загрузите изображение", "info"); return; }
  showLoader("Поиск QR и штрих-кодов…");
  try {
    const r = await callApi("decode_image", state.imagePath);
    if (!r || !r.ok) { if (r && r.error) toast(r.error, "error", 6000); return; }
    showQrResult(r.results, r.count ? `Найдено кодов: ${r.count}` : "Коды не найдены");
    toast(r.count ? `Найдено кодов: ${r.count}` : "Коды не найдены", r.count ? "success" : "info");
    if (r.count) autoCopy(state.resultText);
  } catch (e) {}
  finally { hideLoader(); }
}

async function runScreenCapture() {
  showLoader("Выделите область на экране…");
  try {
    const smart = state.settings.smart_hotkey;
    const r = await callApi(smart ? "capture_smart" : "capture_and_ocr");
    hideLoader();
    if (!r || r.cancelled) return;
    if (!r.ok) { if (r.error) toast(r.error, "error", 6000); return; }
    applyCaptureResult(r);
  } catch (e) { hideLoader(); }
}

function applyCaptureResult(r) {
  if (r.mode === "qr") {
    showQrResult(r.results || [], r.count ? `QR с экрана: найдено ${r.count}` : "QR с экрана: коды не найдены");
    if (r.count) { toast(`QR распознан (${r.count} шт.)`, "success"); autoCopy(state.resultText); }
    else toast("QR-код в области не найден", "info");
  } else {
    if (r.text && r.text.trim()) {
      const det = r.detected ? ` · язык: ${r.detected}` : "";
      showTextResult(r.text, "Текст с экрана" + det);
      toast("Текст с экрана распознан" + det, "success");
      autoCopy(r.text);
    } else toast("Текст в выделенной области не найден", "info");
  }
}

window.onHotkeyResult = (r) => {
  if (!r || r.cancelled) return;
  if (!r.ok) { if (r.error) toast(r.error, "error", 6000); return; }
  applyCaptureResult(r);
};

async function takeScreenshot(askFolder) {
  showLoader("Выделите область для скриншота…");
  try {
    const r = await callApi("capture_screenshot", askFolder === true);
    hideLoader();
    if (!r || r.cancelled) return;
    if (!r.ok) { if (r.error) toast(r.error, "error", 6000); return; }
    state.shotCount++;
    toast(`Скриншот сохранён: ${r.path}`, "success", 5200);
  } catch (e) { hideLoader(); }
}

window.onScreenshotResult = (r) => {
  if (!r || r.cancelled) return;
  if (!r.ok) { if (r.error) toast(r.error, "error", 6000); return; }
  state.shotCount++;
  toast(`Скриншот сохранён: ${r.path}`, "success", 5200);
};

async function translateCurrent() {
  const t = currentResultText();
  if (!t || !t.trim()) { toast("Сначала распознайте или введите английский текст", "info"); return; }
  showLoader("Перевод (офлайн)…");
  try {
    const r = await callApi("translate_text", t);
    if (!r || !r.ok) { if (r && r.error) toast(r.error, "error", 6000); return; }
    typeIntoTextarea($("#trOutput"), r.translated);
    toast("Перевод готов", "success");
    autoCopy(r.translated);
  } catch (e) {}
  finally { hideLoader(); }
}

/* ---------------------------------------------------------------------------
   Настройки: drawer, переключатели, горячие клавиши, автозапуск, журнал
--------------------------------------------------------------------------- */
function initDrawer() {
  const d = $("#settingsDrawer"), b = $("#drawerBackdrop");
  const open = async () => {
    d.classList.add("open"); b.classList.remove("hidden"); d.setAttribute("aria-hidden", "false");
    await refreshLog();
    await syncAutostartUI();
  };
  const close = () => { d.classList.remove("open"); b.classList.add("hidden"); d.setAttribute("aria-hidden", "true"); };
  $("#settingsBtn").addEventListener("click", open);
  $("#drawerClose").addEventListener("click", close);
  b.addEventListener("click", close);
  addEventListener("keydown", e => { if (e.key === "Escape" && d.classList.contains("open")) close(); });
}

async function refreshLog() {
  try {
    const text = await callApi("get_log");
    const box = $("#logBox");
    box.innerHTML = "";
    appendLogLines(String(text || "").split("\n"));
    box.scrollTop = 1e9;
  } catch (e) {}
}

async function pushSettings() {
  try {
    const r = await callApi("save_settings", state.settings);
    if (r && r.ok === false) {
      toast(r.error || "Не удалось сохранить настройки", "error", 6000);
      if (r.settings) state.settings = Object.assign({}, state.settings, r.settings);
      refreshHotkeyFields();
      return false;
    }
    if (r && typeof r === "object") {
      state.settings = Object.assign({}, state.settings, r);
      refreshHotkeyFields();
      updateHotkeysStatus();
    }
    return true;
  } catch (e) { return false; }
}

async function syncAutostartUI() {
  try {
    const r = await callApi("get_autostart");
    if (r && typeof r.enabled === "boolean") {
      $("#setAutostart").checked = r.enabled;
      state.settings.autostart = r.enabled;
    }
  } catch (e) {}
}

/* --- запись комбинаций горячих клавиш ---
   Используем e.code — ФИЗИЧЕСКУЮ позицию клавиши, поэтому запись не зависит
   от раскладки: Ctrl+Shift+S при русской раскладке запишется как ctrl+shift+s,
   а не «ctrl+shift+ы». Имена клавиш — как в Python-библиотеке `keyboard`.
   Минимум — 2 клавиши (модификатор + основная), например ctrl+f9 или alt+q. */
const CODEMAP = {
  Space: "space", Tab: "tab", Enter: "enter", Escape: "esc",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  Delete: "delete", Insert: "insert", Home: "home", End: "end",
  PageUp: "page up", PageDown: "page down",
  Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
  Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".",
  Slash: "/", Backquote: "`",
  Numpad0: "num 0", Numpad1: "num 1", Numpad2: "num 2", Numpad3: "num 3",
  Numpad4: "num 4", Numpad5: "num 5", Numpad6: "num 6", Numpad7: "num 7",
  Numpad8: "num 8", Numpad9: "num 9", NumpadAdd: "num +", NumpadSubtract: "num -",
  NumpadMultiply: "num *", NumpadDivide: "num /", NumpadDecimal: "num .",
  NumpadEnter: "num enter",
};
const MOD_CODES = { Control: 1, Alt: 1, AltGraph: 1, Shift: 1, OS: 1, Meta: 1 };

function comboFromEvent(e) {
  if (e.key === "Escape") return null;         // Esc — отмена записи
  const code = e.code || "";
  if (!code || MOD_CODES[code]) return null;   // нажат только модификатор — ждём основную клавишу
  const parts = [];
  if (e.ctrlKey) parts.push("ctrl");
  if (e.altKey) parts.push("alt");
  if (e.shiftKey) parts.push("shift");
  if (e.metaKey) parts.push("windows");
  let key = null, m;
  if ((m = /^Key([A-Z])$/.exec(code))) key = m[1].toLowerCase();
  else if ((m = /^Digit([0-9])$/.exec(code))) key = m[1];
  else if (/^F([1-9]|1\d|2[0-4])$/.test(code)) key = code.toLowerCase();
  else key = CODEMAP[code] || null;
  if (!key || parts.includes(key)) return null;
  parts.push(key);
  return parts.length >= 2 ? parts.join("+") : null;   // минимум 2 клавиши
}

/* Красивое отображение: "ctrl+shift+s" -> "Ctrl + Shift + S" */
function formatHotkey(combo) {
  return String(combo || "").split("+").map(p => p.trim()).filter(Boolean)
    .map(p => p.length === 1 ? p.toUpperCase() : p.charAt(0).toUpperCase() + p.slice(1))
    .join(" + ");
}
function updateHotkeysStatus() {
  const el = $("#statusHotkeys");
  if (!el) return;
  el.textContent = `${formatHotkey(state.settings.hotkey_recognize)} — распознать · `
                 + `${formatHotkey(state.settings.hotkey_screenshot)} — скриншот`;
}
function refreshHotkeyFields() {
  const a = $("#hkRecognize"), b = $("#hkScreenshot");
  if (a) a.value = formatHotkey(state.settings.hotkey_recognize);
  if (b) b.value = formatHotkey(state.settings.hotkey_screenshot);
}

function initHotkeyRecorder() {
  const fields = {
    hkRecognize: { key: "hotkey_recognize", btn: "hkRecBtn" },
    hkScreenshot: { key: "hotkey_screenshot", btn: "hkShotBtn" },
  };
  refreshHotkeyFields();
  for (const [inputId, cfg] of Object.entries(fields)) {
    const input = $("#" + inputId), btn = $("#" + cfg.btn);
    btn.addEventListener("click", () => {
      if (state.recording) return;
      state.recording = cfg.key;
      input.classList.add("recording");
      btn.classList.add("recording");
      btn.textContent = "Нажмите клавиши…";
      input.value = "";
      const onKey = async (e) => {
        e.preventDefault(); e.stopPropagation();
        const combo = comboFromEvent(e);
        if (!combo) return;   // ждём полноценную комбинацию (≥2 клавиш)
        cleanup();
        input.value = formatHotkey(combo);
        state.settings[cfg.key] = combo;
        btn.textContent = "Проверяю…";
        const ok = await pushSettings();   // Python проверит, сохранит и перерегистрирует
        btn.textContent = "Записать";
        if (ok) toast(`Комбинация назначена: ${formatHotkey(combo)}`, "success");
        // при ошибке pushSettings уже откатил значение и показал тост
      };
      const onEsc = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cleanup();
          input.value = formatHotkey(state.settings[cfg.key] || ""); btn.textContent = "Записать"; }
      };
      const cleanup = () => {
        state.recording = null;
        input.classList.remove("recording");
        btn.classList.remove("recording");
        window.removeEventListener("keydown", onKey, true);
        window.removeEventListener("keydown", onEsc, true);
      };
      window.addEventListener("keydown", onKey, true);
      window.addEventListener("keydown", onEsc, true);
    });
  }
  $("#hkResetBtn").addEventListener("click", async () => {
    state.settings.hotkey_recognize = "ctrl+shift+s";
    state.settings.hotkey_screenshot = "ctrl+shift+p";
    refreshHotkeyFields();
    if (await pushSettings()) toast("Комбинации сброшены по умолчанию", "success");
  });
}

function initSettings() {
  const map = { setAutoCopy: "auto_copy", setSmart: "smart_hotkey", setPreload: "preload" };
  for (const [id, key] of Object.entries(map)) {
    const el = $("#" + id);
    if (!el) continue;
    el.checked = state.settings[key] !== false;
    el.addEventListener("change", () => { state.settings[key] = el.checked; pushSettings(); });
  }
  $("#setAutostart").addEventListener("change", async () => {
    state.settings.autostart = $("#setAutostart").checked;
    await pushSettings();   // Python создаст/удалит ярлык в папке «Автозагрузка»
  });

  $("#copyLogBtn").addEventListener("click", () => copyText(logBoxText(), "Журнал"));
  $("#clearLogBtn").addEventListener("click", async () => {
    try { await callApi("clear_log"); $("#logBox").innerHTML = ""; toast("Журнал очищен", "success"); } catch (e) {}
  });
  $("#openShotsBtn").addEventListener("click", () => callApi("open_screenshots_dir").catch(() => {}));
  $("#openDataBtn").addEventListener("click", () => callApi("open_data_dir").catch(() => {}));
  $("#checkBtn").addEventListener("click", runDiagnostics);
  initHotkeyRecorder();
}

const DIAG_NAMES = [
  ["ocr", "OCR (Tesseract)"], ["qr", "QR и штрих-коды"], ["screen", "Захват экрана"],
  ["clipboard", "Буфер обмена"], ["translator", "Перевод EN→RU"], ["tray", "Иконка в трее"],
];
async function runDiagnostics() {
  const box = $("#diagResult");
  box.innerHTML = '<p class="placeholder">Проверка…</p>';
  try {
    const r = await callApi("check_components");
    box.innerHTML = "";
    DIAG_NAMES.forEach(([key, name], i) => {
      const d = r[key] || {};
      let ok = false, note = "";
      if (key === "ocr") { ok = !!d.ok; note = ok ? `v${d.version}, языков: ${(d.langs || []).length}` : (d.error || ""); }
      else if (key === "qr") { ok = !!d.ok; note = ok ? Object.entries(d).filter(([k, v]) => k !== "ok" && v).map(([k]) => k).join(", ") : "нет движков"; }
      else if (key === "translator") { ok = !!d.ok; note = ok ? `моделей: ${(d.models || []).length}` : (d.error || ""); }
      else { ok = !!d; note = ok ? "доступно" : "недоступно"; }
      const el = document.createElement("div");
      el.className = "diag-item";
      el.style.animationDelay = i * 70 + "ms";
      el.innerHTML = `<span class="diag-dot ${ok ? "diag-ok" : "diag-bad"}"></span><span class="diag-name">${name}</span><span class="diag-note">${escapeHtml(note)}</span>`;
      box.appendChild(el);
    });
    toast("Диагностика завершена", "success");
  } catch (e) { box.innerHTML = `<p class="placeholder">Ошибка проверки: ${escapeHtml(e)}</p>`; }
}

/* ---------------------------------------------------------------------------
   Кнопки + старт
--------------------------------------------------------------------------- */
function bindButtons() {
  $("#pickBtn").addEventListener("click", pickImage);
  $("#screenBtn").addEventListener("click", runScreenCapture);
  $("#shotBtn").addEventListener("click", () => takeScreenshot(false));
  $("#shotAsBtn").addEventListener("click", () => takeScreenshot(true));
  $("#runOcrBtn").addEventListener("click", runOcrOnFile);
  $("#runQrBtn").addEventListener("click", runQrOnFile);
  $("#copyBtn").addEventListener("click", () => copyText(currentResultText(), "Результат"));
  $("#translateBtn").addEventListener("click", translateCurrent);
  $("#clearBtn").addEventListener("click", () => {
    $("#mainOutput").value = ""; $("#qrResults").innerHTML = "";
    state.resultText = ""; state.mode = "text";
    $("#mainOutput").classList.remove("hidden"); $("#qrResults").classList.add("hidden");
    $("#resultTitle").textContent = "Результат распознавания";
    setStatus("Результат очищен");
  });
  $("#copyTrBtn").addEventListener("click", () => copyText($("#trOutput").value, "Перевод"));
  $("#clearTrBtn").addEventListener("click", () => { $("#trOutput").value = ""; });
  $("#mainOutput").addEventListener("input", () => { if (state.mode === "text") state.resultText = $("#mainOutput").value; });
}

async function onReady() {
  initParticles();
  initWindowControls();
  initDropZone();
  initDrawer();
  bindButtons();

  if (!pyapi()) {
    setStatus("Python-API не подключён — демонстрационный режим", "error");
    toast("Запустите программу через Raspoznavatel.exe", "error", 6000);
    initSettings();
    return;
  }
  try {
    state.settings = Object.assign({}, state.settings, await callApi("get_settings"));
    state.settings.ocr_langs = "auto";
    initSettings();
    updateHotkeysStatus();
    const info = await callApi("get_info");
    $("#versionLabel").textContent = info.version;
    setStatus(`Готов к работе · ${formatHotkey(state.settings.hotkey_recognize)} — распознать · ${formatHotkey(state.settings.hotkey_screenshot)} — скриншот`);
    callApi("preload_translator").catch(() => {});
    pyapi().js_log("Интерфейс v2.2 загружен").catch(() => {});
  } catch (e) {
    setStatus("Ошибка инициализации: " + e, "error");
  }
}

if (window.pywebview) onReady();
else {
  addEventListener("pywebviewready", onReady, { once: true });
  setTimeout(() => { if (!window.pywebview) onReady(); }, 1500);
}
