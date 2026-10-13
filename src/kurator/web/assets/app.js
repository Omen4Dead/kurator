const TOKEN = window.KURATOR_TOKEN;
const $ = (id) => document.getElementById(id);

const state = {
  tab: "exact",
  factory: { phash: 10, cosine: 0.9 },
  skipped: {},
  groups: [],
  clusters: [],
  root: "",
  selected: new Map(),
  strips: [],
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

function whenRu(stamp) {
  return new Date(stamp * 1000).toLocaleString("ru-RU",
    { dateStyle: "short", timeStyle: "short" });
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

function stripCard(strip) {
  return `<div class="card moved" data-run="${strip.run_id}">
    <div class="strip">
      <span class="stripdone">перенесено в карантин</span>
      <span class="striptitle">${escapeHtml(strip.title)}</span>
      <span class="stripmeta">${strip.paths.length} файл(ов) · ${fmtBytes(strip.bytes)}</span>
      <button class="ghost" data-role="restore" data-run="${strip.run_id}">Вернуть</button>
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

async function loadStrips() {
  try {
    const { runs } = await api("/api/runs");
    $("btn-undo").hidden = !runs.length;
    $("btn-undo").textContent = runs.length
      ? `Вернуть из карантина (${runs.length})` : "Вернуть из карантина";
    state.strips = runs
      .filter((r) => r.kind === "by-hand" && r.paths.length)
      .map((r) => ({
        run_id: r.run_id,
        paths: r.paths,
        title: r.paths[0].split("/").pop(),
        bytes: r.bytes || 0,
      }));
  } catch (err) {
    state.strips = [];
    $("btn-undo").hidden = true;
  }
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
    main.innerHTML = `<p class="empty">Здесь пусто. Проверь индекс, другую вкладку или нажми «Снять все отметки».</p>`;
    return;
  }
  const total = groups.reduce((sum, g) =>
    sum + g.files.filter((f) => isSelected(g).includes(f.path)).reduce((s, f) => s + f.size, 0), 0);
  const hidden = state.skipped || {};
  const strips = state.strips.map(stripCard).join("");
  const cards = groups.map((g, i) => {
    const sig = groupKeyOf(g);
    return hidden[sig] !== undefined ? skipStrip(g, hidden[sig]) : groupCard(g, i);
  });
  main.innerHTML = (strips ? `<div class="strips">${strips}</div>` : "") +
    `<p class="meta">${groups.length} групп · отмечено на ${fmtBytes(total)}.
    Красная рамка — уедет в карантин, без рамки останется. Чтобы оставить
    другой кадр вместо отмеченного, снимите ✓ здесь и поставьте там.</p>` +
    cards.join("");
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
      if (result.run_id) {
        const group = findGroupByKey(groupKeyByFirstPath(paths));
        const bytes = group
          ? group.files.filter((f) => paths.includes(f.path))
                       .reduce((sum, f) => sum + (f.size || 0), 0)
          : (result.bytes || 0);
        state.strips.unshift({
          run_id: result.run_id,
          paths,
          title: (groupTitle(paths) || paths[0] || "").split("/").pop(),
          bytes,
        });
      }
      render();
      toast(`перенесено ${result.moved ?? 0}. Полоса с кнопкой «Вернуть» — сверху.`);
    } else {
      toast(`пропущено ${result.skipped ?? 0}`);
    }
    await loadState();
    await refreshSituation();
    await loadFindings();
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

async function restoreRun(runId) {
  try {
    const result = await api("/api/restore", {
      method: "POST", body: JSON.stringify({ run_id: Number(runId) }),
    });
    state.strips = state.strips.filter((s) => String(s.run_id) !== String(runId));
    toast(result.message ? result.message : `возвращено ${result.moved ?? 0}`);
    await loadState();
    await loadStrips();
    await refreshSituation();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
}

document.addEventListener("click", (event) => {
  const node = event.target.closest("[data-role]");
  if (!node) return;
  const { role, key, path, run, sig } = node.dataset;

  if (role === "restore") { restoreRun(run); return; }
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

$("btn-reset").onclick = () => {
  const groups = state.groups.filter((g) => g.kind === state.tab);
  let touched = 0;
  for (const group of groups) {
    const key = groupKey(group);
    if (!state.selected.has(key)) continue;
    touched += state.selected.get(key).length;
    state.selected.delete(key);
  }
  render();
  toast(touched ? `снято галочек: ${touched}` : "галочек и не было стояло");
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
  if (!confirm("Вернуть из карантина последний перенос?")) return;
  try {
    const result = await api("/api/undo", { method: "POST", body: "{}" });
    toast(result.message ? result.message : `возвращено ${result.moved}`);
    await loadState();
    await loadStrips();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

loadState()
  .then(() => Promise.all([loadFindings(), loadStrips(), refreshSituation()]))
  .then(() => render())
  .catch((err) => toast(err.message, true));