const TOKEN = window.KURATOR_TOKEN;
const $ = (id) => document.getElementById(id);

const state = {
  // Точные дубли — последняя вкладка: их мало, проверяются быстро и
  // новые появляются нечасто. Первая вкладка открывается по умолчанию,
  // поэтому дефолт должен совпадать с ней, иначе интерфейс включается
  // на пустой третьей вкладке.
  tab: "near-strong",
  // Заводские пороги приходят из /api/state. Зашивать их здесь нельзя:
  // страница переживает смену версии, и зашитое число рано или поздно
  // становится ложью — кнопка «сбросить» молча вернёт прежние значения.
  factory: null,
  skipped: {},
  groups: [],
  clusters: [],
  root: "",
  selected: new Map(),
  jobTimer: null,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "X-Kurator-Token": TOKEN, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await res.text();
  let payload = {};
  try { payload = JSON.parse(text); } catch { payload = { raw: text }; }
  if (!res.ok) throw new Error(payload.error || payload.raw || `${res.status}`);
  return payload;
}

// Сервер гаснет, пока вкладка ещё открыта. Обычная ошибка тут
// обманывает: кажется, что что-то сломалось, хотя всё в порядке —
// просто пора закрывать вкладку.
function offlineError(err) {
  if (err instanceof TypeError) return new Error("kurator больше не отвечает");
  return err;
}

function showFarewell() {
  document.body.classList.add("farewell");
  document.body.innerHTML = `
    <div class="farewell-box">
      <p class="farewell-mark">🐔</p>
      <h1>kurator остановлен</h1>
      <p class="muted">Сервер погашен, папка с фотографиями не тронута.<br>
        Карантин и индекс остались на диске ровно там, где были.</p>
      <p class="muted small">Запустить снова: откройте kurator в «Программах»
        или выполните <code>kurator ui</code>. Эту вкладку можно закрыть.</p>
    </div>`;
}

function whenRu(stamp) {
  return new Date(stamp * 1000).toLocaleString("ru-RU",
    { dateStyle: "short", timeStyle: "short" });
}

// Русские числительные: 1 группа, 2 группы, 5 групп. Существовавший раньше
// вид «файла(ов)» годится для таблиц, но в обычной фразе он читается как
// каша, поэтому для новых надписей здесь честные склонения.
function plural(n, one, few, many) {
  const mod100 = n % 100;
  const mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = n, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(1)} ${units[i]}`;
}

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), bad ? 6000 : 2600);
}

function thumb(path, w = 256) {
  return `/api/thumb?token=${TOKEN}&path=${encodeURIComponent(path)}&w=${w}`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function groupKeyOf(group) {
  return group.detail?.sig || `${group.kind}:${group.members[0]}:${group.members.length}`;
}

function groupKey(group) {
  return groupKeyOf(group);
}

function isSelected(group, path) {
  const key = groupKey(group);
  if (!state.selected.has(key)) {
    return group.files.filter((f) => f.role !== "keeper").map((f) => f.path);
  }
  return state.selected.get(key);
}

function toggle(group, path) {
  const key = groupKey(group);
  const current = new Set(isSelected(group, path));
  if (current.has(path)) current.delete(path);
  else current.add(path);
  state.selected.set(key, [...current].filter((p) => group.files.some((f) => f.path === p)));
  render();
}

async function loadState() {
  const data = await api("/api/state");
  state.root = data.root;
  $("rootpath").textContent = data.root;
  $("stats").textContent =
    `${data.files} файлов · ${fmtBytes(data.bytes)} · эмбеддингов ${data.embedded}/${data.files} · ` +
    `модель ${data.model.present ? "на месте" : "нет (только хеши)"}` +
    (data.skipped ? ` · пропущено ${data.skipped}` : "");
  if (!$("phash").dataset.touched) $("phash").value = data.defaults.phash;
  if (!$("cosine").dataset.touched) $("cosine").value = data.defaults.cosine;
  const when = data.last_index_at
    ? new Date(data.last_index_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })
    : "ни разу";
  $("indexnote").textContent = `последний проход: ${when}`;
  if (data.factory) state.factory = data.factory;
  if (data.job && data.job.status === "running") pollJob();
}

function polling() {
  $("btn-index").disabled = true;
  $("btn-reindex").disabled = true;
}

function idle() {
  $("btn-index").disabled = false;
  $("btn-reindex").disabled = false;
  $("progress").hidden = true;
}

async function pollJob() {
  polling();
  $("progress").hidden = false;
  try {
    const job = await api("/api/job");
    if (job.status === "running" || job.status === "idle") {
      $("progress-text").textContent = job.progress || "запускаю…";
      state.jobTimer = setTimeout(pollJob, 700);
      return;
    }
    if (job.status === "error") { toast(`ошибка: ${job.error}`, true); idle(); return; }
    const s = job.stats || {};
    if (!s.indexed) {
      toast("новых и изменённых файлов нет — индекс уже актуален");
    } else {
      toast(`готово: просмотрено ${s.seen}, новых ${s.indexed}, эмбеддингов ${s.embedded}`);
    }
    idle();
    await loadState();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
    idle();
  }
}

async function loadFindings() {
  try {
    const data = await api(`/api/findings?phash=${$("phash").value}&cosine=${$("cosine").value}`);
    state.groups = data.groups || [];
    state.clusters = data.clusters || [];
    state.selected.clear();
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

function tile(group, file) {
  const key = groupKey(group);
  const path = escapeHtml(file.path);
  const selected = isSelected(group, file.path).includes(file.path);
  const size = file.size === null || file.size === undefined ? "" : ` · ${fmtBytes(file.size)}`;
  const when = file.taken_at ? file.taken_at.slice(0, 10) : "";
  return `<div class="tile ${selected ? "picked" : ""}">
    <button class="pick" data-role="toggle" data-key="${escapeHtml(key)}" data-path="${path}"
            title="${selected ? "Снять отметку" : "Отметить для переноса"}"
            aria-pressed="${selected}">${selected ? "✓" : ""}</button>
    <a href="${thumb(file.path, 512)}" target="_blank"><img src="${thumb(file.path)}" loading="lazy" alt=""></a>
    <div class="name">${escapeHtml(file.path.split("/").pop())}</div>
    <div class="meta">${escapeHtml(when)}${size}</div>
  </div>`;
}

function skipStrip(group, createdAt) {
  const sig = groupKeyOf(group);
  const title = (group.keeper || group.members[0]).split("/").pop();
  const bytes = group.files.reduce((sum, f) => sum + (f.size || 0), 0);
  return `<div class="card skipped" data-sig="${escapeHtml(sig)}">
    <div class="strip">
      <span class="stripdone">оставлено как есть</span>
      <span class="striptitle">${escapeHtml(title)}</span>
      <span class="stripmeta">${group.size} файла(ов) · ${fmtBytes(bytes)}${
        createdAt ? ` · скрыто ${whenRu(createdAt)}` : ""}</span>
      <button class="ghost" data-role="unskip" data-sig="${escapeHtml(sig)}">Вернуть в разбор</button>
    </div>
  </div>`;
}

async function refreshSituation() {
  const data = await api("/api/state");
  const map = {};
  for (const item of data.skipped_groups || []) map[item.sig] = item.created_at;
  state.skipped = map;
  const count = Object.keys(map).length;
  $("btn-unskip").hidden = !count;
  $("btn-unskip").textContent = `Вернуть все скрытые (${count})`;
}

function groupCard(group, index) {
  const chosen = isSelected(group);
  const paths = group.files.map((f) => f.path);
  const keeper = group.keeper || "";
  const bytes = group.files.filter((f) => chosen.includes(f.path)).reduce((sum, f) => sum + f.size, 0);
  const detail = Object.entries(group.detail || {})
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${v}`).join(", ");
  const counts = `${chosen.length} из ${group.size}`;
  return `<div class="card" data-kind="${escapeHtml(group.kind)}">
    <h3>${index + 1}. ${escapeHtml(keeper.split("/").pop() || group.members[0].split("/").pop())}
      <span class="badge">${group.size} файла(ов)</span></h3>
    <p class="meta">${escapeHtml(detail)}</p>
    <div class="grid">${group.files.map((f) => tile(group, f)).join("")}</div>
    <div class="card-actions">
      <span class="pickcount">перенести: ${counts}${bytes ? ` · ${fmtBytes(bytes)}` : ""}</span>
      <button class="danger" data-role="remove" data-key="${escapeHtml(groupKey(group))}">В карантин</button>
      <button class="ghost" data-role="skip" data-key="${escapeHtml(groupKey(group))}">Оставить оба</button>
      <button class="ghost" data-role="reset" data-key="${escapeHtml(groupKey(group))}">Сброс</button>
    </div>
  </div>`;
}

function render() {
  const main = $("main");
  const hidden = state.skipped || {};
  if (state.tab === "clusters") {
    if (!state.clusters.length) {
      main.innerHTML = `<p class="empty">Кластеров нет. Понизь порог cosine или проиндексируй больше.</p>`;
      return;
    }
    main.innerHTML = state.clusters.map((cluster, i) => `<div class="card">
        <h3>${i + 1}. кластер <span class="badge">${cluster.size} файла(ов)</span></h3>
        <p class="meta">${escapeHtml(cluster.members[0].split("/").slice(0, -1).join("/"))}</p>
        <div class="grid">${cluster.members.map((m) =>
          `<div class="tile"><a href="${thumb(m, 512)}" target="_blank"><img src="${thumb(m)}" loading="lazy" alt=""></a>
           <div class="name">${escapeHtml(m.split("/").pop())}</div></div>`).join("")}</div>
      </div>`).join("");
    return;
  }

  const groups = state.groups.filter((g) => g.kind === state.tab);
  if (!groups.length) {
    main.innerHTML = `<p class="empty">Здесь пусто. Проверь индекс или открой другую вкладку.</p>`;
    return;
  }
  // Пропущенные группы остаются в списке полосой, но в счёт не идут:
  // нажать на них уже нельзя, и называть их «группами» значит обещать
  // работу, которой не будет. Размер тоже считается только по тем, что
  // в работе, — отметки пропущенной группы остаются в памяти, но
  // недостижимы, и раньше из-за этого «отмечено на» показывало лишнее.
  const isHidden = (g) => hidden[groupKeyOf(g)] !== undefined;
  const live = groups.filter((g) => !isHidden(g));
  const putAside = groups.length - live.length;
  const total = live.reduce((sum, g) =>
    sum + g.files.filter((f) => isSelected(g).includes(f.path)).reduce((s, f) => s + f.size, 0), 0);
  const cards = groups.map((g, i) => {
    const sig = groupKeyOf(g);
    return hidden[sig] !== undefined ? skipStrip(g, hidden[sig]) : groupCard(g, i);
  });
  const headline = live.length
    ? `${live.length} ${plural(live.length, "группа", "группы", "групп")} в работе` +
      (putAside ? `, ещё ${putAside} ${plural(putAside, "оставлена", "оставлены", "оставлено")} как есть` : "") +
      ` · отмечено на ${fmtBytes(total)}`
    : `Все ${groups.length} ${plural(groups.length, "группа", "группы", "групп")} оставлены как есть`;
  // Пояснение про рамки имеет смысл, только когда есть что выбирать.
  const howto = live.length
    ? ` Красная рамка — уедет в карантин, без рамки останется. Чтобы оставить
    другой кадр вместо отмеченного, снимите ✓ здесь и поставьте там.` : "";
  main.innerHTML = `<p class="meta">${headline}.${howto}</p>` + cards.join("");
}

function findGroupByKey(key) {
  return state.groups.find((g) => groupKey(g) === key);
}

function groupKeyByFirstPath(paths) {
  const first = paths[0] || "";
  const hit = state.groups.find((g) => g.members.includes(first));
  return hit ? groupKeyOf(hit) : "";
}

function groupTitle(paths) {
  const first = paths[0] || "";
  const folder = first.split("/").slice(0, -1).join("/");
  const hit = state.groups.find((g) => g.members.includes(first));
  if (hit) return hit.keeper || hit.members[0];
  return folder || first;
}

async function decide(paths, action, sig) {
  if (!paths.length) { toast("ничего не отмечено"); return; }
  try {
    const result = await api("/api/decide", {
      method: "POST",
      body: JSON.stringify({ paths, action, sig }),
    });
    if (result.blocked && result.blocked.length) {
      toast(`burst-кадры не трогаю: ${result.blocked.length} пропущено`, true);
    }
    if (action === "skip") {
      await refreshSituation();
      render();
      return;
    }
    if (action === "remove") {
      toast(`перенесено ${result.moved ?? 0}. Вернуть можно в корзине.`);
    } else {
      toast(`пропущено ${result.skipped ?? 0}`);
    }
    await loadState();
    await loadTrashCount();
    await refreshSituation();
    await loadFindings();
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

async function unskipGroup(sig) {
  try {
    await api("/api/decide", {
      method: "POST", body: JSON.stringify({ action: "unskip", paths: [], sig }),
    });
    toast("группа вернулась в разбор");
    await refreshSituation();
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

document.addEventListener("click", (event) => {
  const node = event.target.closest("[data-role]");
  if (!node) return;
  const { role, key, path, sig } = node.dataset;

  if (role === "unskip") { unskipGroup(sig); return; }

  const group = findGroupByKey(key);
  if (!group) return;

  if (role === "toggle") { toggle(group, path); return; }
  if (role === "remove") { decide(isSelected(group), "remove"); return; }
  if (role === "skip") { decide(group.members, "skip", groupKeyOf(group)); return; }
  if (role === "unskip") { unskipGroup(sig); return; }
  if (role === "reset") { state.selected.delete(key); render(); return; }
});

$("btn-root").onclick = async () => {
  try {
    const data = await api("/api/roots");
    const current = data.current;
    $("picker-list").innerHTML = data.suggestions.map((sug) => {
      const active = sug.path === current ? " current" : "";
      let note = sug.kind;
      if (sug.index) {
        const when = sug.index.last_index_at
          ? new Date(sug.index.last_index_at * 1000).toLocaleString("ru-RU",
              { dateStyle: "short", timeStyle: "short" })
          : "дата неизвестна";
        note = `индекс: ${sug.index.files} файлов · ${when}`;
      } else {
        note = `${sug.kind} · ещё не индексирована`;
      }
      return `<button class="sug${active}" data-path="${escapeHtml(sug.path)}">
        <span class="sugpath">${escapeHtml(sug.path)}</span>
        <span class="sugkind">${escapeHtml(note)}</span>
      </button>`;
    }).join("");
    $("picker-path").value = current;
    $("picker").showModal();
  } catch (err) {
    toast(err.message, true);
  }
};

$("picker-cancel").onclick = () => $("picker").close();

$("picker-list").addEventListener("click", (event) => {
  const button = event.target.closest(".sug");
  if (button) switchRoot(button.dataset.path);
});

$("picker-path").onkeydown = (event) => {
  if (event.key === "Enter") { event.preventDefault(); switchRoot($("picker-path").value); }
};

$("picker-go").onclick = () => switchRoot($("picker-path").value);

async function switchRoot(path) {
  if (!path) return;
  try {
    $("picker-go").disabled = true;
    await api("/api/root", { method: "POST", body: JSON.stringify({ path }) });
    $("picker").close();
    state.selected.clear();
    toast(`папка сменена, запускаю индексацию`);
    await loadState();
    await pollJob();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("picker-go").disabled = false;
  }
}

$("btn-index").onclick = async () => {
  await api("/api/index", { method: "POST", body: JSON.stringify({}) });
  pollJob();
};

$("btn-reindex").onclick = async () => {
  const data = await api("/api/state");
  const files = data.files || 0;
  const seconds = Math.max(1, Math.round(files / 12));
  const text = seconds > 90
    ? `${files} файлов, примерно ${Math.round(seconds / 60)} мин на этой машине.`
    : `${files} файлов, примерно ${seconds} сек.`;
  $("rehash-cost").textContent = text;
  $("rehash").showModal();
};

$("rehash-cancel").onclick = () => $("rehash").close();

$("rehash-confirm").onclick = async () => {
  $("rehash").close();
  await api("/api/index", { method: "POST", body: JSON.stringify({ rehash: true }) });
  pollJob();
};

$("phash").oninput = (e) => { e.target.dataset.touched = "1"; };
$("cosine").oninput = (e) => { e.target.dataset.touched = "1"; };
async function saveTuning() {
  try {
    await api("/api/tuning", {
      method: "POST",
      body: JSON.stringify({ phash: +$("phash").value, cosine: +$("cosine").value }),
    });
    // Пороги не участвуют в индексации: пересчитались находки, а база
    // осталась нетронутой. Без этой подсказки человек после смены порога
    // разумно жмёт «пересчитать всё заново» и платит за это минутами.
    toast("пороги применены — переиндексация не нужна");
  } catch (err) {
    toast(err.message, true);
  }
}

$("phash").onchange = () => { saveTuning(); loadFindings(); };
$("cosine").onchange = () => { saveTuning(); loadFindings(); };

document.querySelectorAll(".tabs button").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    state.tab = button.dataset.tab;
    render();
    loadFindings().catch(() => {});
  };
});

$("btn-report").onclick = async () => {
  try {
    const info = await api("/api/report", {
      method: "POST",
      body: JSON.stringify({ phash: +$("phash").value, cosine: +$("cosine").value }),
    });
    toast(`отчёт готов: ${info.sheets} листов, кандидатов ${info.reclaimable_count}`);
    window.open("/out/report.html");
  } catch (err) {
    toast(err.message, true);
  }
};

$("btn-factory").onclick = async () => {
  if (!state.factory) {
    toast("заводские значения ещё не загрузились, попробуйте через мгновение", true);
    return;
  }
  const { phash, cosine } = state.factory;
  const nowP = $("phash").value;
  const nowC = $("cosine").value;
  if (String(+nowP) === String(phash) && String(+nowC) === String(cosine)) {
    toast("уже заводские значения");
    return;
  }
  $("phash").value = phash;
  $("cosine").value = cosine;
  delete $("phash").dataset.touched;
  delete $("cosine").dataset.touched;
  try {
    await api("/api/tuning", { method: "POST", body: JSON.stringify({ phash, cosine }) });
    toast(`сброшено к ${phash} и ${cosine}`);
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

$("btn-unskip").onclick = async () => {
  const count = Object.keys(state.skipped || {}).length;
  if (!count) { toast("скрытых групп нет"); return; }
  if (!confirm(`Вернуть в разбор все скрытые группы (${count})?`)) return;
  try {
    const result = await api("/api/decide", {
      method: "POST", body: JSON.stringify({ action: "clear", paths: ["-"] }),
    });
    toast(result.cleared ? `вернулось пропущенных: ${result.cleared}` : "пропущенных не было");
    await loadState();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

$("btn-undo").onclick = async () => {
  try {
    const runs = (await api("/api/runs")).runs;
    if (!runs.length) { toast("в карантине пусто, возвращать нечего"); return; }
  } catch (err) {
    toast(err.message, true);
    return;
  }
  if (!confirm("Вернуть из карантина всё, что там лежит?")) return;
  try {
    const result = await api("/api/undo", { method: "POST", body: "{}" });
    toast(result.message ? result.message : `возвращено ${result.moved}`);
    await loadState();
    await loadTrashCount();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

/* ---- Вкладка: сказать серверу, что мы тут ----
 *
 * Сервер держит модель и индекс в памяти и по простою выключается.
 * Он может это делать, только если знает, что вкладка открыта: без
 * счётчика он гас бы под работающей страницей. Приход при загрузке,
 * уход — на `pagehide`, потому что `beforeunload` браузеры блокируют, а
 * `sendBeacon` доживает до сервера при закрытии вкладки.
 */
function announceOpen() {
  api("/api/hello", { method: "POST", body: "{}" }).catch(() => {});
}

function announceClose() {
  // Токен в заголовке, но sendBeacon не даёт его задать. Поэтому адрес
  // с токеном в строке — единственный способ, который переживает
  // закрытие страницы. Токен и так лежит в адресе открытой вкладки.
  const url = new URL(window.location.href);
  if (!navigator.sendBeacon) return;
  navigator.sendBeacon(url.pathname.replace(/\/$/, "") + "/api/bye?token=" + encodeURIComponent(url.searchParams.get("token") || TOKEN));
}

window.addEventListener("pagehide", announceClose);
window.addEventListener("pageshow", announceOpen);
announceOpen();

loadState()
  .then(() => Promise.all([loadFindings(), loadTrashCount(), refreshSituation()]))
  .then(() => render())
  .catch((err) => {
    // Открытая вкладка после выхода или после аварии сервера:
    // объясняем, что случилось, вместо голого «Failed to fetch».
    if (err instanceof TypeError) { showFarewell(); return; }
    toast(offlineError(err).message, true);
  });
/* ---- Корзина: содержимое карантина, удаление выбранного или всего ---- */

state.trash = { items: [], chosen: new Set(), armed: null };

function trashKeyLabel(item) {
  return item.from_path || "неизвестно, манифест потерян";
}

function renderTrash() {
  const box = state.trash;
  $("trash-badge").textContent = box.items.length;
  $("trash-selected").textContent = `выбрано ${box.chosen.size}`;

  $("trash-list").innerHTML = box.items.map((item) => {
    const checked = box.chosen.has(item.key) ? " checked" : "";
    const note = item.junk
      ? `<div class="orphan">служебный файл macOS, не фотография</div>`
      : item.orphan
      ? `<div class="orphan">без манифеста — вернуть нечем</div>` : "";
    // Отметить файл без манифеста нельзя: вернуть его нечем, и галочка
    // была бы обещанием, которое интерфейс не сможет выполнить.
    const locked = item.orphan ? " disabled" : "";
    return `<li class="trashitem">
      <input type="checkbox" data-trash="${escapeHtml(item.key)}"${checked}${locked}>
      <img src="${thumb(item.to_path)}" loading="lazy" alt="">
      <div class="trashbody">
        <div class="trashname">${escapeHtml(item.to_path.split("/").pop())}</div>
        <div class="trashpath" title="${escapeHtml(item.to_path)}">${escapeHtml(trashKeyLabel(item))}</div>
        ${note}
      </div>
      <span class="trashsize">${fmtBytes(item.size)}</span>
    </li>`;
  }).join("");

  const n = box.chosen.size;
  $("trash-confirm").disabled = !n;
  $("trash-confirm").textContent = n ? `Удалить выбранные (${n})` : "Удалить выбранные";
  $("trash-wipe").disabled = !box.items.length;

  // Вернуть можно не всё: у файла без манифеста нет адреса, откуда он
  // пришёл. Такую галочку не отмечаем и молча не считаем, иначе кнопка
  // обещала бы вернуть то, чего вернуть нечем.
  const restorable = box.items.filter((i) => !i.orphan);
  const restorableKeys = new Set(restorable.map((i) => i.key));
  const restorableChosen = [...box.chosen].filter((k) => restorableKeys.has(k)).length;
  const restore = $("trash-restore");
  restore.disabled = !restorableChosen;
  restore.textContent = restorableChosen
    ? `Вернуть выбранные (${restorableChosen})`
    : "Вернуть выбранные";
  restore.title = restorableChosen < n
    ? "Часть отмеченных вернуть нельзя: манифест потерян"
    : "";

  const armed = box.armed;
  $("trash-confirm").classList.toggle("armed", armed === "chosen");
  $("trash-wipe").classList.toggle("armed", armed === "all");
  if (armed === "chosen" && n) {
    $("trash-confirm").textContent = `Точно удалить ${n} файл(ов)? Отменить нельзя`;
  }
  if (armed === "all" && box.items.length) {
    $("trash-wipe").textContent = `Точно удалить все ${box.items.length}? Отменить нельзя`;
  } else {
    $("trash-wipe").textContent = "Удалить всё";
  }
}

async function openTrash() {
  try {
    const data = await api("/api/trash");
    state.trash = { items: data.items, chosen: new Set(), armed: null };
    renderTrash();
    $("trash").showModal();
  } catch (err) {
    toast(err.message, true);
  }
}

async function runTrashDelete(mode) {
  const box = state.trash;
  const keys = mode === "all" ? box.items.map((i) => i.key) : [...box.chosen];
  if (!keys.length) { toast("ничего не выбрано", true); return; }
  try {
    const result = await api("/api/trash/delete", {
      method: "POST",
      body: JSON.stringify({ keys, confirm: true, mode }),
    });
    const freed = fmtBytes(result.bytes || 0);
    if (result.failed && result.failed.length) {
      toast(`удалено ${result.deleted}, не вышло ${result.failed.length}`, true);
    } else {
      toast(`удалено навсегда ${result.deleted} (${freed})`);
    }
    box.armed = null;
    await loadTrashCount();
    const data = await api("/api/trash");
    state.trash = { items: data.items, chosen: new Set(), armed: null };
    renderTrash();
    if (!data.items.length) $("trash").close();
    await loadTrashCount();
  } catch (err) {
    toast(err.message, true);
  }
}

async function runTrashRestore() {
  const box = state.trash;
  const restorable = new Set(box.items.filter((i) => !i.orphan).map((i) => i.key));
  const keys = [...box.chosen].filter((k) => restorable.has(k));
  if (!keys.length) { toast("нечего возвращать", true); return; }
  try {
    const result = await api("/api/trash/restore", {
      method: "POST",
      body: JSON.stringify({ keys }),
    });
    const problems = (result.failed || []).length + (result.orphans || []).length;
    if (problems) {
      toast(
        `возвращено ${result.restored}, не вышло ${problems}` +
          (result.failed && result.failed[0]
            ? ` — «${result.failed[0].error}»`
            : ""),
        true,
      );
    } else {
      toast(`возвращено на место ${result.restored} (${fmtBytes(result.bytes || 0)})`);
    }
    box.armed = null;
    const data = await api("/api/trash");
    state.trash = { items: data.items, chosen: new Set(), armed: null };
    renderTrash();
    if (!data.items.length) $("trash").close();
    await loadTrashCount();
    await loadFindings();
    await loadState();
  } catch (err) {
    toast(err.message, true);
  }
}

$("trash-restore").onclick = runTrashRestore;

async function loadTrashCount() {
  // Оба счётчика — и у кнопки отката, и у кнопки корзины — берутся
  // из одного места. Раньше откат считал по `/api/runs`, где стоит
  // предел в двадцать переносов, и при большем количестве переносов
  // показывалось число меньше настоящего: часть переносов молча
  // исчезала из интерфейса вместе с возможностью их откатить.
  try {
    const data = await api("/api/trash");
    $("btn-trash").hidden = !data.count;
    $("btn-trash").textContent = data.count
      ? `Корзина (${data.count})` : "Корзина";
    $("btn-undo").hidden = !data.count;
    $("btn-undo").textContent = data.count
      ? `Вернуть из карантина (${data.count})` : "Вернуть из карантина";
    state.trashFiles = data.count;
  } catch {
    $("btn-trash").hidden = true;
    $("btn-undo").hidden = true;
  }
}

$("btn-trash").onclick = openTrash;
$("trash-cancel").onclick = () => { state.trash.armed = null; $("trash").close(); };
$("trash-all").onclick = () => {
  state.trash.chosen = new Set(state.trash.items.filter((i) => !i.orphan).map((i) => i.key));
  state.trash.armed = null;
  renderTrash();
};
$("trash-none").onclick = () => {
  state.trash.chosen = new Set();
  state.trash.armed = null;
  renderTrash();
};
$("trash-list").addEventListener("change", (event) => {
  const box = event.target.closest("input[data-trash]");
  if (!box) return;
  if (box.checked) state.trash.chosen.add(box.dataset.trash);
  else state.trash.chosen.delete(box.dataset.trash);
  state.trash.armed = null;
  renderTrash();
});
$("trash-confirm").onclick = () => {
  if (state.trash.armed !== "chosen") { state.trash.armed = "chosen"; renderTrash(); return; }
  runTrashDelete("chosen");
};
$("trash-wipe").onclick = () => {
  if (state.trash.armed !== "all") { state.trash.armed = "all"; renderTrash(); return; }
  runTrashDelete("all");
};

/* ---- Выход: погасить сервер и уйти по-человечески ---- */

$("btn-quit").onclick = async () => {
  const busy = document.querySelectorAll("header button");
  busy.forEach((b) => { b.disabled = true; });
  if (!confirm(
    "Остановить kurator?\n\n" +
    "Индекс и карантин останутся на диске, фотографии не пострадают.\n" +
    "Запустить снова можно будет двойным щелчком по значку."
  )) {
    busy.forEach((b) => { b.disabled = false; });
    return;
  }
  try {
    await api("/api/quit", { method: "POST", body: "{}" });
  } catch (err) {
    // Сервер не ответил — значит, он уже ушёл. Это не ошибка.
  }
  showFarewell();
};
