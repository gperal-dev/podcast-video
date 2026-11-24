"use strict";

const state = {
  jobsIndex: [],
  activeJobId: null,
  report: null,
  notesByJob: {},
  selectedClipId: null,
  charts: {},
};

const esc = (v) =>
  String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

function fmtSec(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function fmtDurHuman(sec) {
  const s = Number(sec) || 0;
  if (s >= 60) {
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    return `${m}m ${r}s`;
  }
  return `${s.toFixed(1)}s`;
}

function destroyChart(key) {
  if (state.charts[key]) {
    state.charts[key].destroy();
    delete state.charts[key];
  }
}

async function fetchJson(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function initTabs() {
  const tablist = document.querySelector('[role="tablist"]');
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const panels = tabs.map((t) => document.querySelector(`[role="tabpanel"][aria-labelledby="${t.id}"]`));

  const activate = (targetTab, focusTab = false) => {
    tabs.forEach((tab, idx) => {
      const selected = tab === targetTab;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
      if (panels[idx]) panels[idx].hidden = !selected;
    });
    targetTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (focusTab) targetTab.focus();
    if (targetTab.id === "tab-compare") renderGlobalComparisonTab();
  };

  tabs.forEach((tab, idx) => {
    tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") activate(tabs[(idx + 1) % tabs.length], true);
      else if (e.key === "ArrowLeft") activate(tabs[(idx - 1 + tabs.length) % tabs.length], true);
      else if (e.key === "Home") activate(tabs[0], true);
      else if (e.key === "End") activate(tabs[tabs.length - 1], true);
    });
  });

  return { activateById: (id) => activate(document.getElementById(id)) };
}

async function loadIndex(refresh = false) {
  const items = await fetchJson(`/api/jobs${refresh ? "?refresh=true" : ""}`);
  state.jobsIndex = items;
  const sel = document.getElementById("job-select");
  sel.innerHTML = items
    .map((j) => {
      const tag = j.status === "completed" ? "Completada" : j.status.toUpperCase();
      return `<option value="${esc(j.job_id)}">${esc(j.episode_name)} — ${tag} (${j.clips_delivered}/${j.clips_requested} vídeos · $${(j.cost_usd || 0).toFixed(3)})</option>`;
    })
    .join("");

  if (!state.activeJobId || !items.some((x) => x.job_id === state.activeJobId)) {
    const preferred =
      items.find((x) => x.job_id === "job_20261124_203725_8304d3") ||
      items.find((x) => x.videos_playable > 0) ||
      items[0];
    state.activeJobId = preferred ? preferred.job_id : null;
  }
  if (state.activeJobId) {
    sel.value = state.activeJobId;
    await loadJob(state.activeJobId, refresh);
  }
  renderGlobalComparisonTab();
}

async function loadJob(jobId, refresh = false) {
  state.activeJobId = jobId;
  const [report, notes] = await Promise.all([
    fetchJson(`/api/jobs/${encodeURIComponent(jobId)}${refresh ? "?refresh=true" : ""}`),
    fetchJson(`/api/notes?job_id=${encodeURIComponent(jobId)}`),
  ]);
  state.report = report;
  state.notesByJob = notes || {};
  state.selectedClipId = report.clips && report.clips.length ? report.clips[0].id : null;
  renderRunTab();
}

function renderRunTab() {
  const r = state.report;
  if (!r) return;
  renderBasicInfo(r);
  renderMasterTimeline(r);
  renderVideoStudio(r);
  renderPerformanceSection(r);
  renderJudgeSection(r);
}

function renderBasicInfo(r) {
  const pill = document.getElementById("job-status-pill");
  if (r.status === "completed") {
    pill.className = "pill pill-ok";
    pill.textContent = "Completada correctamente";
  } else if (r.status === "failed") {
    pill.className = "pill pill-err";
    pill.textContent = "Fallida";
  } else {
    pill.className = "pill pill-warn";
    pill.textContent = r.status;
  }

  const req = r.quotas?.requested?.total ?? 0;
  const deliv = r.quotas?.delivered ?? 0;
  const costUsd = Number(r.cost?.cost_usd || 0);
  const tokens = Number(r.cost?.total_tokens || 0);
  const evalCnt = r.judge_funnel?.total_candidates_evaluated ?? 0;
  const discCnt = r.judge_funnel?.discarded_or_rejected ?? 0;

  document.getElementById("kpi-grid").innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Episodio y Duración</div>
      <div class="kpi-val">${esc(fmtSec(r.audio_duration_sec))}</div>
      <div class="kpi-sub">${esc(r.episode_name)} · ${(r.timeline?.sentences || []).length} cláusulas</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Tiempo Total Proceso</div>
      <div class="kpi-val">${esc(fmtDurHuman(r.total_processing_sec))}</div>
      <div class="kpi-sub">Perfil: ${esc(r.profile_id || "-")}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Vídeos Generados</div>
      <div class="kpi-val">${deliv} / ${req}</div>
      <div class="kpi-sub">${(r.clips || []).filter((c) => c.video_exists).length} MP4 reproducibles</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Coste IA (OpenRouter)</div>
      <div class="kpi-val">$${costUsd.toFixed(4)}</div>
      <div class="kpi-sub">${tokens.toLocaleString("es-ES")} tokens · ${(r.llm_calls || []).length} llamadas</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Juez Ciego y NLE</div>
      <div class="kpi-val">${evalCnt} eval.</div>
      <div class="kpi-sub">${discCnt} descartados · ${r.judge_funnel?.escalated_strong ?? 0} a experto</div>
    </div>
  `;
}

function seekPodcastAudio(sec, autoPlay = true) {
  const audio = document.getElementById("source-audio");
  if (!audio.src) return;
  audio.currentTime = Math.max(0, Number(sec) || 0);
  if (autoPlay) audio.play().catch(() => {});
}

function selectClip(clipId, scrollIntoView = false) {
  state.selectedClipId = clipId;
  renderMasterTimeline(state.report);
  renderVideoStudio(state.report);
  if (scrollIntoView) {
    document.getElementById("video-studio-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function renderMasterTimeline(r) {
  const audio = document.getElementById("source-audio");
  const statusLine = document.getElementById("audio-status-line");
  if (r.source_audio_exists) {
    const mediaUrl = `/media/audio/${encodeURIComponent(r.job_id)}`;
    if (audio.dataset.srcJob !== r.job_id) {
      audio.src = mediaUrl;
      audio.dataset.srcJob = r.job_id;
    }
    statusLine.textContent = `Audio original completo (${fmtSec(r.audio_duration_sec)})`;
  } else {
    audio.removeAttribute("src");
    statusLine.textContent = "Archivo de audio original no disponible en disco.";
  }

  const totalDur = Math.max(1, Number(r.audio_duration_sec || 0));
  const pct = (t) => `${Math.max(0, Math.min(100, (Number(t) / totalDur) * 100)).toFixed(3)}%`;
  const wid = (a, b) => `${Math.max(0.45, ((Number(b) - Number(a)) / totalDur) * 100).toFixed(3)}%`;

  // Time ruler every 5 min (or 1 min for short files)
  const stepMin = totalDur > 900 ? 5 : 1;
  let rulerHtml = "";
  for (let t = 0; t <= totalDur; t += stepMin * 60) {
    rulerHtml += `<span class="daw-tick" style="left:${pct(t)}">${Math.round(t / 60)}m</span>`;
  }
  const rulerEl = document.getElementById("daw-ruler");
  rulerEl.innerHTML = rulerHtml;

  const sentences = r.timeline?.sentences || [];
  const promoSents = sentences.filter((s) => s.is_promo);
  const musicIntervals = r.timeline?.music_intervals || [];
  const spans = r.timeline?.clip_spans || [];
  const excised = r.timeline?.excised_gaps || [];

  const spansByClip = new Map();
  const excByClip = new Map();
  for (const sp of spans) {
    spansByClip.set(sp.clip_id, [...(spansByClip.get(sp.clip_id) || []), sp]);
  }
  for (const ex of excised) {
    excByClip.set(ex.clip_id, [...(excByClip.get(ex.clip_id) || []), ex]);
  }

  let html = `
    <div class="daw-track">
      <div class="daw-track-btn" style="cursor:default">
        <strong>Descartes Automáticos</strong>
        <span>${promoSents.length} promos · ${musicIntervals.length} músicas</span>
      </div>
      <div class="daw-lane lane-master">
        <div class="daw-playhead" style="left:0%"></div>
        ${promoSents
          .map(
            (ps) =>
              `<div class="daw-block kind-promo" style="left:${pct(ps.start)};width:${wid(ps.start, ps.end)}" title="Promo/Despedida bloqueada (${fmtSec(ps.start)})"></div>`
          )
          .join("")}
        ${musicIntervals
          .map(
            (m) =>
              `<div class="daw-block kind-music" style="left:${pct(m[0])};width:${wid(m[0], m[1])}" title="Música / silencio (${fmtSec(m[0])}–${fmtSec(m[1])})"></div>`
          )
          .join("")}
      </div>
    </div>
  `;

  for (const c of r.clips || []) {
    const isActive = c.id === state.selectedClipId;
    const cSpans = spansByClip.get(c.id) || [];
    const cExc = excByClip.get(c.id) || [];
    const cutsCnt = (c.splices || []).length;

    const blocksHtml = [
      ...cExc.map(
        (ex) =>
          `<div class="daw-block kind-excised" style="left:${pct(ex.orig_start)};width:${wid(ex.orig_start, ex.orig_end)}" title="Recortado fuera (${ex.duration_sec}s)">✂️ -${Math.round(ex.duration_sec)}s</div>`
      ),
      ...cSpans.map(
        (sp) =>
          `<div class="daw-block role-${esc(sp.role)}" style="left:${pct(sp.orig_start)};width:${wid(sp.orig_start, sp.orig_end)}" title="${esc(sp.role)} · ${fmtSec(sp.orig_start)}–${fmtSec(sp.orig_end)}">${esc(sp.role)}</div>`
      ),
    ].join("");

    html += `
      <div class="daw-track">
        <button type="button" class="daw-track-btn ${isActive ? "is-active" : ""}" data-select-clip="${esc(c.id)}">
          <strong>${esc(c.title)}</strong>
          <span>${esc(c.clip_type)} · ${fmtDurHuman(c.edl?.edited_duration)} · ${cutsCnt} corte(s)</span>
        </button>
        <div class="daw-lane ${isActive ? "is-active" : ""}" data-lane-clip="${esc(c.id)}">
          <div class="daw-playhead" style="left:0%"></div>
          ${blocksHtml}
        </div>
      </div>
    `;
  }

  const container = document.getElementById("daw-container");
  container.innerHTML = html;

  container.querySelectorAll("[data-select-clip]").forEach((btn) => {
    btn.addEventListener("click", () => selectClip(btn.dataset.selectClip, true));
  });
  container.querySelectorAll("[data-lane-clip]").forEach((lane) => {
    lane.addEventListener("click", () => selectClip(lane.dataset.laneClip, true));
  });
  container.querySelectorAll(".lane-master").forEach((lane) => {
    lane.addEventListener("click", (e) => {
      const rect = lane.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
      seekPodcastAudio(ratio * totalDur, true);
    });
  });
}

function seekClipVideo(sec) {
  const vid = document.getElementById("clip-video-player");
  if (!vid || vid.hidden || !vid.getAttribute("src")) return;
  vid.currentTime = Math.max(0, Number(sec) || 0);
  vid.play().catch(() => {});
}

function renderVideoStudio(r) {
  const clips = r.clips || [];
  const pillsBar = document.getElementById("clip-pills-bar");
  pillsBar.innerHTML = clips
    .map((c, idx) => {
      const active = c.id === state.selectedClipId;
      return `<button type="button" class="btn btn-sm ${active ? "btn-primary" : "btn-secondary"}" data-pick-clip="${esc(c.id)}">Vídeo ${idx + 1}: ${esc(c.clip_type)}</button>`;
    })
    .join("");
  pillsBar.querySelectorAll("[data-pick-clip]").forEach((b) => {
    b.addEventListener("click", () => selectClip(b.dataset.pickClip, false));
  });

  const clip = clips.find((c) => c.id === state.selectedClipId) || clips[0];
  if (!clip) {
    document.getElementById("video-studio-title").textContent = "Esta ejecución no generó vídeos finales";
    return;
  }

  document.getElementById("video-studio-title").textContent = clip.title;
  document.getElementById("clip-edl-badge").textContent = `${clip.clip_type} · ${clip.score ?? "--"}/100`;

  const videoEl = document.getElementById("clip-video-player");
  const missingEl = document.getElementById("clip-video-missing");
  if (clip.video_exists && clip.video_filename) {
    videoEl.hidden = false;
    missingEl.hidden = true;
    const vidUrl = `/media/video/${encodeURIComponent(r.job_id)}/${encodeURIComponent(clip.video_filename)}`;
    if (videoEl.getAttribute("src") !== vidUrl) videoEl.setAttribute("src", vidUrl);
    if (clip.thumb_exists && clip.thumb_filename) {
      videoEl.poster = `/media/thumb/${encodeURIComponent(r.job_id)}/${encodeURIComponent(clip.thumb_filename)}`;
    } else {
      videoEl.removeAttribute("poster");
    }
  } else {
    videoEl.hidden = true;
    videoEl.removeAttribute("src");
    missingEl.hidden = false;
  }

  const introDur = clip.intro_curtain?.enabled ? Number(clip.intro_curtain.duration_sec || 0) : 0;
  const bodyDur = Number(clip.edl?.edited_duration || 0);
  const outroDur = Number(clip.outro_curtain_duration_sec || 0);
  const resGaps = clip.edl?.residual_gaps || [];
  const splices = clip.splices || [];

  // Quick Jump Buttons under Video Player
  const jumpBtns = [];
  if (introDur > 0) jumpBtns.push({ label: `Intro IA (0:00)`, sec: 0, cls: "btn-secondary" });
  jumpBtns.push({ label: `Inicio Podcast (${fmtSec(introDur)})`, sec: introDur, cls: "btn-secondary" });
  for (const sp of splices) {
    jumpBtns.push({
      label: `Escuchar Empalme #${sp.index} (${fmtSec(Math.max(0, sp.video_time_sec - 2))})`,
      sec: Math.max(0, sp.video_time_sec - 2),
      cls: "btn-secondary",
    });
  }
  for (const g of resGaps) {
    const absStart = Math.max(0, introDur + g.edited_start - 2);
    jumpBtns.push({
      label: `⚠ Hueco ${g.gap_sec}s (${fmtSec(absStart)})`,
      sec: absStart,
      cls: "btn-danger",
    });
  }
  if (outroDur > 0) {
    jumpBtns.push({ label: `Outro (${fmtSec(introDur + bodyDur)})`, sec: introDur + bodyDur, cls: "btn-secondary" });
  }

  const jumpWrap = document.getElementById("video-jump-buttons");
  jumpWrap.innerHTML = jumpBtns
    .map((b) => `<button type="button" class="btn btn-sm ${b.cls}" data-vjump="${b.sec}">${esc(b.label)}</button>`)
    .join("");
  jumpWrap.querySelectorAll("[data-vjump]").forEach((b) => {
    b.addEventListener("click", () => seekClipVideo(Number(b.dataset.vjump)));
  });

  // Assembly clickable bar
  const gapSum = resGaps.reduce((acc, g) => acc + Number(g.gap_sec || 0), 0);
  const cleanBody = Math.max(0.5, bodyDur - gapSum);
  const tot = Math.max(1, introDur + bodyDur + outroDur);
  const asm = [];
  if (introDur > 0) {
    asm.push(`<button type="button" class="asm-seg asm-intro" style="flex:${introDur / tot}" data-vjump="0">Intro IA ${introDur.toFixed(1)}s</button>`);
  }
  asm.push(`<button type="button" class="asm-seg asm-body" style="flex:${cleanBody / tot}" data-vjump="${introDur}">Audio Podcast ${cleanBody.toFixed(1)}s</button>`);
  if (gapSum > 0) {
    const g0 = Math.max(0, introDur + (resGaps[0]?.edited_start || 0) - 1);
    asm.push(`<button type="button" class="asm-seg asm-gap" style="flex:${gapSum / tot}" data-vjump="${g0}">Silencio ${gapSum.toFixed(1)}s</button>`);
  }
  if (outroDur > 0) {
    asm.push(`<button type="button" class="asm-seg asm-outro" style="flex:${outroDur / tot}" data-vjump="${introDur + bodyDur}">Outro ${outroDur.toFixed(1)}s</button>`);
  }
  const asmBar = document.getElementById("clip-assembly-bar");
  asmBar.innerHTML = asm.join("");
  asmBar.querySelectorAll("[data-vjump]").forEach((b) => {
    b.addEventListener("click", () => seekClipVideo(Number(b.dataset.vjump)));
  });

  document.getElementById("clip-edl-stats").innerHTML = `
    <span>Bruto seleccionado: <strong>${Number(clip.edl?.orig_duration || 0).toFixed(1)}s</strong></span>
    <span>Audio final: <strong>${bodyDur.toFixed(1)}s</strong></span>
    <span>Saltos internos: <strong>${splices.length}</strong></span>
    <span>Música quitada: <strong>${clip.edl?.music_gaps_removed_count || 0}</strong></span>
    <span>Render MP4: <strong>${clip.render_duration_sec != null ? `${clip.render_duration_sec}s` : "N/D"}</strong></span>
  `;

  // Visual Cut-by-Cut Cards
  const splicesList = document.getElementById("splices-list");
  if (!splices.length) {
    splicesList.innerHTML = `<div class="splice-card"><span class="muted">Este vídeo es un tramo continuo sin empalmes ni saltos internos.</span></div>`;
  } else {
    splicesList.innerHTML = splices
      .map(
        (sp) => `
        <div class="splice-card">
          <div class="row-between">
            <strong>Empalme #${sp.index} · ${esc(sp.from_role)} → ${esc(sp.to_role)} (${fmtSec(sp.video_time_sec)} en el vídeo)</strong>
            <button type="button" class="btn btn-primary btn-sm splice-listen" data-vjump="${Math.max(0, sp.video_time_sec - 2)}">
              Escuchar corte (-2s)
            </button>
          </div>
          <div class="splice-flow">
            <div class="splice-quote">
              <div class="muted" style="font-size:0.75rem">Termina en ${fmtSec(sp.orig_end_prev)}:</div>
              "...${esc(sp.before_text || sp.from_span)}"
            </div>
            <div class="splice-cut-pill" title="${esc(sp.cut_out_preview)}">
              ✂️ SALTO<br />-${Math.round(sp.cut_out_sec)}s quitados<br />(${sp.cut_out_sentences} cláusulas)
            </div>
            <div class="splice-quote">
              <div class="muted" style="font-size:0.75rem">Retoma en ${fmtSec(sp.orig_start_next)}:</div>
              "${esc(sp.after_text || sp.to_span)}..."
            </div>
          </div>
        </div>`
      )
      .join("");
    splicesList.querySelectorAll(".splice-listen").forEach((b) => {
      b.addEventListener("click", () => seekClipVideo(Number(b.dataset.vjump)));
    });
  }

  document.getElementById("clip-meta-details").innerHTML = `
    <div><strong>Gancho en pantalla:</strong> ${esc(clip.hook_text || "(Ninguno)")}</div>
    ${clip.cliffhanger_question ? `<div><strong>Cliffhanger:</strong> ${esc(clip.cliffhanger_question)}</div>` : ""}
    <div><strong>Decisión auditor:</strong> ${esc(clip.standalone_packaging?.audit_action || "-")} (${esc(clip.angle_theme || clip.summary)})</div>
  `;
  const ic = clip.intro_curtain || {};
  document.getElementById("clip-intro-box").textContent = ic.enabled
    ? `Cortinilla IA (${ic.duration_sec}s · ${ic.word_count} palabras): "${ic.text}"`
    : "Sin cortinilla IA (arranque directo con voz del podcast).";
  document.getElementById("clip-spoken-body").textContent = clip.spoken_body || "(Sin palabras mapeadas)";

  document.getElementById("clip-storyboard-strip").innerHTML = (clip.storyboard_shots || [])
    .map(
      (sh) => `
      <div class="shot-chip ${sh.anachronism_flag ? "anach" : ""}">
        <div><strong>${sh.start.toFixed(1)}s–${sh.end.toFixed(1)}s</strong> · ${esc(sh.source_type)}</div>
        <div><code>${esc(sh.stock_query || "-")}</code></div>
        <div class="muted">${esc(sh.reason)}</div>
      </div>`
    )
    .join("");

  const saved = state.notesByJob[clip.id] || { tags: [], note: "" };
  const tagSet = new Set(saved.tags || []);
  document.querySelectorAll('#qa-tag-checks input[type="checkbox"]').forEach((cb) => {
    cb.checked = tagSet.has(cb.value);
  });
  document.getElementById("qa-note-text").value = saved.note || "";
  document.getElementById("qa-note-status").textContent = saved.updated_at ? `Guardado ${saved.updated_at.slice(0, 16)}` : "";
}

function renderPerformanceSection(r) {
  const phases = r.phase_timings || [];
  let maxPh = null;
  for (const p of phases) {
    if (p.duration_sec != null && (!maxPh || p.duration_sec > maxPh.duration_sec)) maxPh = p;
  }

  document.getElementById("bottleneck-line").textContent = maxPh
    ? `Fase más costosa en tiempo (cuello de botella): ${maxPh.label} con ${fmtDurHuman(maxPh.duration_sec)} (${maxPh.pct_of_total}% del total).`
    : "Desglose de tiempos por fase.";

  document.getElementById("pipeline-flow-strip").innerHTML = phases
    .map((p) => {
      const isB = maxPh && p.phase === maxPh.phase && p.duration_sec > 0;
      return `
        <div class="phase-step-card ${isB ? "is-bottleneck" : ""}">
          <div class="row-between">
            <strong style="font-size:0.84rem">${esc(p.label)}</strong>
            ${isB ? `<span class="pill pill-warn">Más lenta</span>` : ""}
          </div>
          <div style="font-size:1.3rem;font-weight:800;margin-top:0.25rem">
            ${p.duration_sec != null ? fmtDurHuman(p.duration_sec) : "N/D"}
          </div>
          <div class="phase-bar-track"><div class="phase-bar-fill" style="width:${Math.min(100, p.pct_of_total || 0)}%"></div></div>
          <div class="muted">${p.pct_of_total || 0}% · ${esc(p.detail || p.status)}</div>
        </div>
      `;
    })
    .join("");

  destroyChart("phases");
  const ctxP = document.getElementById("phase-time-chart");
  if (window.Chart && ctxP) {
    state.charts.phases = new window.Chart(ctxP, {
      type: "bar",
      data: {
        labels: phases.map((p) => p.label.replace(/^Fase \d · /, "")),
        datasets: [
          {
            label: "Segundos",
            data: phases.map((p) => p.duration_sec || 0),
            backgroundColor: phases.map((p) => (maxPh && p.phase === maxPh.phase ? "#d97706" : "#2563eb")),
            borderRadius: 6,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
      },
    });
  }

  const clipRenders = (r.clips || [])
    .filter((c) => c.render_duration_sec != null)
    .map((c) => `<span>${esc(c.clip_type)}: <strong>${c.render_duration_sec}s</strong></span>`);
  document.getElementById("clip-render-times").innerHTML = clipRenders.length
    ? `<span>Render por vídeo:</span> ${clipRenders.join(" · ")}`
    : "";

  const byModel = r.cost?.by_model || {};
  const mKeys = Object.keys(byModel);
  destroyChart("models");
  const ctxM = document.getElementById("model-cost-chart");
  if (window.Chart && ctxM) {
    state.charts.models = new window.Chart(ctxM, {
      type: "doughnut",
      data: {
        labels: mKeys,
        datasets: [
          {
            data: mKeys.map((k) => Number(byModel[k].cost_usd || 0)),
            backgroundColor: ["#2563eb", "#7c3aed", "#059669", "#d97706", "#e11d48"],
          },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  document.getElementById("model-cost-table").innerHTML =
    mKeys
      .map(
        (k) =>
          `<span><strong>${esc(k)}:</strong> $${Number(byModel[k].cost_usd || 0).toFixed(4)} (${Number(byModel[k].total_tokens || 0).toLocaleString("es-ES")} tok · ${byModel[k].calls || 0} calls)</span>`
      )
      .join("") +
    ((r.llm_fallbacks || []).length
      ? `<span class="pill pill-warn">${r.llm_fallbacks.length} fallback(s) automático(s)</span>`
      : `<span class="pill pill-ok">0 fallbacks</span>`);
}

function renderJudgeSection(r) {
  const f = r.judge_funnel || {};
  const tot = Math.max(1, Number(f.total_candidates_evaluated || 1));
  const funnelRows = [
    { label: "Candidatos evaluados", val: f.total_candidates_evaluated || 0, color: "#64748b" },
    { label: "Reparados por NLE", val: f.repaired_attempts || 0, color: "#2563eb" },
    { label: "Escalados a Experto", val: f.escalated_strong || 0, color: "#7c3aed" },
    { label: "Aprobados c/ Intro IA", val: (f.approved_voice_only || 0) + (f.approved_with_intro || 0), color: "#059669" },
    { label: "Rescatados Best-Effort", val: f.rescued_best_effort || 0, color: "#d97706" },
    { label: "Descartados / Clonados", val: (f.discarded_or_rejected || 0) + (f.topic_dedup_blocked || 0), color: "#e11d48" },
  ];

  document.getElementById("funnel-visual").innerHTML = funnelRows
    .map((row) => {
      const pct = Math.min(100, Math.round((row.val / tot) * 100));
      return `
        <div class="funnel-row">
          <span>${esc(row.label)}</span>
          <div class="funnel-bar-bg">
            <div class="funnel-bar-fill" style="width:${pct}%;background:${row.color}"></div>
          </div>
          <span style="text-align:right">${row.val}</span>
        </div>
      `;
    })
    .join("");

  destroyChart("judge");
  const ctxJ = document.getElementById("judge-funnel-chart");
  if (window.Chart && ctxJ) {
    state.charts.judge = new window.Chart(ctxJ, {
      type: "doughnut",
      data: {
        labels: ["Voz Podcast 100%", "Aprobados + Intro IA", "Rescatados", "Descartados"],
        datasets: [
          {
            data: [
              f.approved_voice_only || 0,
              f.approved_with_intro || 0,
              f.rescued_best_effort || 0,
              f.discarded_or_rejected || 0,
            ],
            backgroundColor: ["#10b981", "#2563eb", "#d97706", "#e11d48"],
          },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  renderJudgeCards();

  document.getElementById("glossary-chips").innerHTML = (r.glossary || []).length
    ? r.glossary
        .map((g) => `<span class="gl-chip"><del class="muted">${esc(g.raw_term)}</del> → <strong>${esc(g.corrected_term)}</strong></span>`)
        .join("")
    : `<span class="muted">Sin correcciones de glosario.</span>`;

  renderLogs();
}

function renderJudgeCards() {
  const r = state.report;
  if (!r) return;
  const mode = document.getElementById("judge-filter").value;
  const cards = (r.judge_candidates || []).filter((c) => {
    const isGood = c.outcome.includes("approve") || c.outcome.includes("best_effort") || c.outcome.includes("salvage");
    if (mode === "approved") return isGood;
    if (mode === "rejected") return !isGood;
    return true;
  });

  document.getElementById("judge-cards-list").innerHTML =
    cards
      .map((c) => {
        const isOk = c.outcome.includes("approve");
        const isResc = c.outcome.includes("best_effort") || c.outcome.includes("salvage") || c.outcome === "fallback";
        const pillCls = isOk ? "pill-ok" : isResc ? "pill-warn" : "pill-err";
        const pillLbl = isOk ? "Aprobado" : isResc ? "Rescatado c/ Intro" : "Descartado";
        const ev = c.initial_eval || {};
        const meters = [
          ["Comp", ev.comprehension_score],
          ["Gancho", ev.hook_strength],
          ["Contexto", ev.context_clarity],
          ["Bordes", ev.clean_boundaries],
          ["Remate", ev.resolution_score],
        ]
          .filter(([, v]) => v != null)
          .map(
            ([lbl, v]) => `
            <div class="meter-cell">
              <div>${lbl}: <strong>${v}</strong></div>
              <div class="meter-bar"><div class="meter-fill" style="width:${Math.min(100, Number(v) || 0)}%"></div></div>
            </div>`
          )
          .join("");

        return `
          <article class="judge-card">
            <div class="row-between">
              <strong>${esc(c.title)}</strong>
              <span class="pill ${pillCls}">${pillLbl}</span>
            </div>
            <div class="muted">${esc(c.clip_type || "-")} ${c.escalated_to_strong ? "· Escalado a Experto" : ""}</div>
            ${meters ? `<div class="meter-grid">${meters}</div>` : ""}
            <div class="muted">${esc(c.outcome_detail)}</div>
            ${c.intro_curtain_text ? `<div class="callout-light">${esc(c.intro_curtain_text)}</div>` : ""}
          </article>
        `;
      })
      .join("") || `<p class="muted">Sin candidatos registrados para este filtro.</p>`;
}

function renderGlobalComparisonTab() {
  const list = state.jobsIndex || [];
  const totRuns = list.length;
  const totCost = list.reduce((a, x) => a + Number(x.cost_usd || 0), 0);
  const completed = list.filter((x) => x.status === "completed");
  const avgSec = completed.length
    ? completed.reduce((a, x) => a + Number(x.total_processing_sec || 0), 0) / completed.length
    : 0;
  const totClips = list.reduce((a, x) => a + Number(x.clips_delivered || 0), 0);

  document.getElementById("global-kpi-grid").innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Pruebas Registradas</div>
      <div class="kpi-val">${totRuns}</div>
      <div class="kpi-sub">${completed.length} completadas</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Vídeos Totales Creados</div>
      <div class="kpi-val">${totClips}</div>
      <div class="kpi-sub">Entre todas las ejecuciones</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Tiempo Medio Completadas</div>
      <div class="kpi-val">${esc(fmtDurHuman(avgSec))}</div>
      <div class="kpi-sub">Incluye Whisper + IA + FFmpeg</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Coste Acumulado Total</div>
      <div class="kpi-val">$${totCost.toFixed(4)}</div>
      <div class="kpi-sub">OpenRouter global</div>
    </div>
  `;

  const chrono = [...list].reverse();
  const labels = chrono.map((h) => `${h.episode_name} (${h.job_id.slice(9, 13)})`);

  destroyChart("histCost");
  const c1 = document.getElementById("history-cost-chart");
  if (window.Chart && c1) {
    state.charts.histCost = new window.Chart(c1, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Coste (¢ USD)",
            data: chrono.map((h) => Number(((h.cost_usd || 0) * 100).toFixed(2))),
            backgroundColor: "#2563eb",
            borderRadius: 6,
          },
          {
            label: "Vídeos generados",
            data: chrono.map((h) => h.clips_delivered || 0),
            backgroundColor: "#059669",
            borderRadius: 6,
          },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  destroyChart("histTime");
  const c2 = document.getElementById("history-time-chart");
  if (window.Chart && c2) {
    state.charts.histTime = new window.Chart(c2, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Minutos totales",
            data: chrono.map((h) => Number(((h.total_processing_sec || 0) / 60).toFixed(1))),
            borderColor: "#d97706",
            backgroundColor: "rgba(217, 119, 6, 0.14)",
            fill: true,
            tension: 0.25,
          },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  destroyChart("histJudge");
  const c3 = document.getElementById("history-judge-chart");
  if (window.Chart && c3) {
    state.charts.histJudge = new window.Chart(c3, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Evaluados",
            data: chrono.map((h) => h.judge_evaluated || 0),
            backgroundColor: "#64748b",
            borderRadius: 6,
          },
          {
            label: "Descartados",
            data: chrono.map((h) => h.judge_discarded || 0),
            backgroundColor: "#e11d48",
            borderRadius: 6,
          },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  document.getElementById("history-tbody").innerHTML = list
    .map(
      (h) => `
      <tr class="${h.job_id === state.activeJobId ? "row-selected" : ""}" data-open-job="${esc(h.job_id)}">
        <th>${esc(h.episode_name)} <span class="muted" style="font-weight:400">${esc(h.job_id.slice(4, 15))}</span></th>
        <td>${esc(h.status)}</td>
        <td>${fmtSec(h.audio_duration_sec)}</td>
        <td>${fmtDurHuman(h.total_processing_sec)}</td>
        <td>${h.clips_delivered} / ${h.clips_requested}</td>
        <td>${h.judge_evaluated} / ${h.judge_discarded}</td>
        <td>$${Number(h.cost_usd || 0).toFixed(4)}</td>
        <td>${Number(h.total_tokens || 0).toLocaleString("es-ES")}</td>
      </tr>`
    )
    .join("");
}

function renderLogs() {
  const r = state.report;
  if (!r) return;
  const q = (document.getElementById("log-search").value || "").toLowerCase().trim();
  const lvl = document.getElementById("log-level").value;
  const lines = (r.logs || [])
    .filter((l) => (lvl === "all" ? true : l.level === lvl))
    .filter((l) => (!q ? true : `${l.level} ${l.message}`.toLowerCase().includes(q)))
    .map((l) => `[${(l.ts || "").slice(11, 19)}] [${l.level}] ${l.message}`);
  document.getElementById("logs-viewer").textContent = lines.join("\n") || "(Sin logs que coincidan)";
}

window.addEventListener("DOMContentLoaded", () => {
  const tabsCtrl = initTabs();

  document.getElementById("job-select").addEventListener("change", (e) => loadJob(e.target.value));
  document.getElementById("refresh-btn").addEventListener("click", () => loadIndex(true));
  document.getElementById("judge-filter").addEventListener("change", renderJudgeCards);
  document.getElementById("log-search").addEventListener("input", renderLogs);
  document.getElementById("log-level").addEventListener("change", renderLogs);

  const srcAudio = document.getElementById("source-audio");
  const playBtn = document.getElementById("master-play-btn");
  playBtn.addEventListener("click", () => {
    if (!srcAudio.src) return;
    if (srcAudio.paused) srcAudio.play().catch(() => {});
    else srcAudio.pause();
  });
  srcAudio.addEventListener("play", () => {
    playBtn.textContent = "Pausar Podcast Completo";
  });
  srcAudio.addEventListener("pause", () => {
    playBtn.textContent = "Reproducir Podcast Completo";
  });
  srcAudio.addEventListener("timeupdate", () => {
    const dur = Math.max(1, Number(state.report?.audio_duration_sec || srcAudio.duration || 1));
    const cur = srcAudio.currentTime || 0;
    document.getElementById("audio-clock").textContent = `${fmtSec(cur)} / ${fmtSec(dur)}`;
    const leftPct = `${Math.max(0, Math.min(100, (cur / dur) * 100)).toFixed(3)}%`;
    document.querySelectorAll(".daw-playhead").forEach((el) => {
      el.style.left = leftPct;
    });
  });

  document.getElementById("daw-ruler").addEventListener("click", (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
    const totalDur = Math.max(1, Number(state.report?.audio_duration_sec || 1));
    seekPodcastAudio(ratio * totalDur, true);
  });

  document.getElementById("history-tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("[data-open-job]");
    if (!tr) return;
    const jid = tr.dataset.openJob;
    document.getElementById("job-select").value = jid;
    loadJob(jid);
    tabsCtrl.activateById("tab-run");
  });

  document.getElementById("qa-note-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeJobId || !state.selectedClipId) return;
    const tags = Array.from(document.querySelectorAll('#qa-tag-checks input[type="checkbox"]:checked')).map((c) => c.value);
    const note = document.getElementById("qa-note-text").value;
    const res = await fetchJson("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.activeJobId, clip_id: state.selectedClipId, tags, note }),
    });
    state.notesByJob[state.selectedClipId] = res.entry;
    document.getElementById("qa-note-status").textContent = "Nota guardada correctamente.";
  });

  loadIndex(false).catch((err) => {
    document.getElementById("job-status-pill").textContent = `Error: ${err.message}`;
  });
});
