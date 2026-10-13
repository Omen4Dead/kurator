const TOKEN = window.KURATOR_TOKEN;
const $ = (id) => document.getElementById(id);

const state = {
  tab: "exact",
  groups: [],
  clusters: [],
  root: "",
  jobTimer: null,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "X-Kurator-Token": TOKEN, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text.slice(0, 200)}`);
  }
  return res.json();
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

async function loadState() {
  const data = await api("/api/state");
  state.root = data.root;
  $("root").textContent = data.root;
  $("stats").textContent =
    `${data.files} файлов · ${fmtBytes(data.bytes)} · эмбеддингов ${data.embedded}/${data.files} · ` +
    `модель ${data.model.present ? "на месте" : "нет (только хеши)"}`;
  if (!$("phash").dataset.touched) $("phash").value = data.defaults.phash;
  if (!$("cosine").dataset.touched) $("cosine").value = data.defaults.cosine;
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
    if (job.status === "error") {
      toast(`ошибка: ${job.error}`, true);
      idle();
      return;
    }
    const s = job.stats || {};
    toast(`готово: ${s.seen} просмотрено, ${s.indexed} новых, эмбеддингов ${s.embedded}`);
    idle();
    await loadState();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
    idle();
  }
}

async function loadFindings() {
  const phash = $("phash").value;
  const cosine = $("cosine").value;
  try {
    const data = await api(`/api/findings?phash=${phash}&cosine=${cosine}`);
    state.groups = data.groups || [];
    state.clusters = data.clusters || [];
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

function tile(file, keeper) {
  const role = file.role || "member";
  const label = role === "keeper" ? "оставляю" : role === "victim" ? "кандидат" : "в группе";
  const size = file.size === null || file.size === undefined ? "" : ` · ${fmtBytes(file.size)}`;
  return `<div class="tile ${role === "keeper" ? "keeper" : ""}">
    <a href="${thumb(file.path, 512)}" target="_blank"><img src="${thumb(file.path)}" loading="lazy" alt=""></a>
    <div class="name">${escapeHtml(file.path.split("/").pop())}</div>
    <div class="role ${role === "victim" ? "victim" : ""}">${label}${size}</div>
  </div>`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function groupCard(group, index) {
  const keeper = group.files.find((f) => f.role === "keeper");
  const title = keeper ? keeper.path.split("/").pop() : group.members[0].split("/").pop();
  const detail = Object.entries(group.detail || {})
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
  const bytes = group.files.filter((f) => f.role === "victim").reduce((sum, f) => sum + f.size, 0);
  return `<div class="card">
    <h3>${index + 1}. ${escapeHtml(title)} <span class="badge">${group.size} файла(ов)</span></h3>
    <p class="meta">${escapeHtml(detail)}${bytes ? ` · можно освободить ${fmtBytes(bytes)}` : ""}</p>
    <div class="grid">${group.files.map((f) => tile(f)).join("")}</div>
  </div>`;
}

function render() {
  const main = $("main");
  if (state.tab === "clusters") {
    if (!state.clusters.length) {
      main.innerHTML = `<p class="empty">Кластеров нет. Понизь порог cosine или проиндексируй больше.</p>`;
      return;
    }
    main.innerHTML = state.clusters.map((cluster, i) => {
      const bytes = 0;
      return `<div class="card">
        <h3>${i + 1}. кластер <span class="badge">${cluster.size} файла(ов)</span></h3>
        <p class="meta">${escapeHtml(cluster.members[0].split("/").slice(0, -1).join("/"))}</p>
        <div class="grid">${cluster.members.map((m) => tile({ path: m, size: 0 }, false)).join("")}</div>
      </div>`;
    }).join("");
    return;
  }

  const groups = state.groups.filter((g) => g.kind === state.tab);
  if (!groups.length) {
    main.innerHTML = `<p class="empty">Пусто. Проверь, что индекс построен, или посмотри другие вкладки.</p>`;
    return;
  }
  const totalBytes = groups.reduce(
    (sum, g) => sum + g.files.filter((f) => f.role === "victim").reduce((s, f) => s + f.size, 0), 0);
  main.innerHTML = `<p class="meta">${groups.length} групп · кандидатов на ${fmtBytes(totalBytes)}</p>` +
    groups.map(groupCard).join("");
}

$("btn-index").onclick = async () => {
  await api("/api/index", { method: "POST", body: JSON.stringify({}) });
  pollJob();
};

$("btn-reindex").onclick = async () => {
  await api("/api/index", { method: "POST", body: JSON.stringify({ rehash: true }) });
  pollJob();
};

$("btn-refresh").onclick = loadFindings;

$("phash").oninput = (e) => { e.target.dataset.touched = "1"; };
$("cosine").oninput = (e) => { e.target.dataset.touched = "1"; };
$("phash").onchange = loadFindings;
$("cosine").onchange = loadFindings;

document.querySelectorAll(".tabs button").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    state.tab = button.dataset.tab;
    render();
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

$("btn-apply").onclick = async () => {
  try {
    const plan = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify({ phash: +$("phash").value, cosine: +$("cosine").value, kinds: ["exact"] }),
    });
    if (!plan.count) { toast("нечего переносить"); return; }
    $("plan-summary").innerHTML =
      `Перенесу <b>${plan.count}</b> файла(ов) на <b>${fmtBytes(plan.bytes)}</b>.`;
    $("plan-list").innerHTML = plan.sample
      .map((m) => `<li>${escapeHtml(m.from)} → ${escapeHtml(m.to)}</li>`).join("");
    $("plan-confirm").dataset.kinds = JSON.stringify(["exact"]);
    $("plan").showModal();
  } catch (err) {
    toast(err.message, true);
  }
};

$("plan-cancel").onclick = () => $("plan").close();

$("plan-confirm").onclick = async () => {
  const kinds = JSON.parse($("plan-confirm").dataset.kinds || '["exact"]');
  try {
    const result = await api("/api/apply/confirm", {
      method: "POST",
      body: JSON.stringify({ phash: +$("phash").value, cosine: +$("cosine").value, kinds }),
    });
    $("plan").close();
    toast(`перенесено ${result.moved}. Откат — кнопка «Откатить».`);
    await loadState();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

$("btn-undo").onclick = async () => {
  if (!confirm("Вернуть файлы из последнего карантина на место?")) return;
  try {
    const result = await api("/api/undo", { method: "POST", body: "{}" });
    toast(result.message ? result.message : `возвращено ${result.moved}`);
    await loadState();
    await loadFindings();
  } catch (err) {
    toast(err.message, true);
  }
};

loadState()
  .then(loadFindings)
  .catch((err) => toast(err.message, true));