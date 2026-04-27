const el = (id) => document.getElementById(id);

let inspectorResizeObserver = null;
let galleryRefreshTimer = null;
const GALLERY_AUTO_REFRESH_WINDOW_MS = 30000;
const UPLOAD_STATUS_POLL_INTERVAL_MS = 2000;
const UPLOAD_STATUS_POLL_TIMEOUT_MS = 120000;

const state = {
  pets: [],
  activePetId: null,
  galleryView: "ALL",
  galleryItems: [],
  originalGalleryItems: [],
  galleryTotalCount: 0,
  galleryPage: 1,
  galleryPageSize: 100,
  galleryFilters: {
    state: "ALL",
    multiplicity: "ALL",
    petId: "",
  },
  selectedImageIds: new Set(),
  imageMetaCache: new Map(),
  inspectedImageId: null,
  searchScores: {},
  activeExemplars: [],
  lastBucketManifest: null,
  bucketSummary: null,
  calendarMonth: null,
  calendarCounts: {},
  singleSeedMode: "append",
  folderSeedPolicy: "append",
  seedContextTarget: null,
  galleryAutoRefreshEnabled: false,
  galleryAutoRefreshUntil: 0,
  galleryLoadInFlight: false,
};

function nowIso() {
  return new Date().toISOString();
}

function log(msg, obj) {
  const box = el("logBox");
  if (!box) return;
  const line = `[${nowIso()}] ${msg}`;
  const text = obj ? `${line}\n${JSON.stringify(obj, null, 2)}\n` : `${line}\n`;
  box.textContent = text + box.textContent;
}

function clearGalleryRefreshTimer() {
  if (galleryRefreshTimer) {
    window.clearTimeout(galleryRefreshTimer);
    galleryRefreshTimer = null;
  }
}

function hasPendingGalleryItems(items) {
  return Array.isArray(items) && items.some((item) => {
    const status = String(item?.ingest_status || "").toUpperCase();
    return status === "PENDING" || status === "PROCESSING";
  });
}

function hasPendingActiveExemplars() {
  return hasPendingGalleryItems(state.activeExemplars);
}

function scheduleGalleryAutoRefresh() {
  clearGalleryRefreshTimer();
  if (!state.galleryAutoRefreshEnabled) return;
  galleryRefreshTimer = window.setTimeout(async () => {
    if (state.galleryLoadInFlight) {
      scheduleGalleryAutoRefresh();
      return;
    }
    try {
      if (Date.now() < Number(state.galleryAutoRefreshUntil || 0) || hasPendingActiveExemplars()) {
        await loadPets();
      }
      await loadGallery({ reason: "auto-refresh" });
    } catch (err) {
      log("Gallery auto-refresh failed", { error: err.message || String(err) });
      scheduleGalleryAutoRefresh();
    }
  }, 2500);
}

function startGalleryAutoRefreshWindow(durationMs = GALLERY_AUTO_REFRESH_WINDOW_MS) {
  state.galleryAutoRefreshUntil = Date.now() + Math.max(0, Number(durationMs || 0));
  state.galleryAutoRefreshEnabled = true;
  scheduleGalleryAutoRefresh();
}

function value(id) {
  const target = el(id);
  return target ? target.value || "" : "";
}

function currentDate() {
  return value("workspaceDate").trim();
}

function renderWorkspaceDateDisplay() {
  const display = el("workspaceDateDisplay");
  const toggle = el("btnWorkspaceDateToggle");
  const value = currentDate();
  if (display) display.textContent = value || "날짜 선택";
  if (toggle) toggle.setAttribute("aria-expanded", isWorkspaceCalendarOpen() ? "true" : "false");
}

function isWorkspaceCalendarOpen() {
  const popover = el("workspaceCalendarPopover");
  return !!popover && !popover.hidden;
}

function closeWorkspaceCalendar() {
  const popover = el("workspaceCalendarPopover");
  const toggle = el("btnWorkspaceDateToggle");
  const panel = el("workspacePanel");
  if (popover) popover.hidden = true;
  if (toggle) toggle.setAttribute("aria-expanded", "false");
  if (panel) panel.classList.remove("is-calendar-open");
}

function toggleWorkspaceCalendar() {
  const popover = el("workspaceCalendarPopover");
  const toggle = el("btnWorkspaceDateToggle");
  const panel = el("workspacePanel");
  if (!popover) return;
  const next = popover.hidden;
  popover.hidden = !next;
  if (toggle) toggle.setAttribute("aria-expanded", next ? "true" : "false");
  if (panel) panel.classList.toggle("is-calendar-open", next);
}

function workspaceCapturedAt() {
  const date = currentDate();
  return date ? `${date}T12:00:00` : "";
}

function monthKeyFromDate(dateStr) {
  const raw = String(dateStr || "").trim();
  return raw ? raw.slice(0, 7) : "";
}

function parseMonthKey(monthKey) {
  const raw = String(monthKey || "").trim();
  const match = raw.match(/^(\d{4})-(\d{2})$/);
  if (!match) return null;
  return { year: Number(match[1]), month: Number(match[2]) };
}

function shiftMonthKey(monthKey, delta) {
  const parsed = parseMonthKey(monthKey);
  if (!parsed) return monthKeyFromDate(new Date().toISOString().slice(0, 10));
  const dt = new Date(Date.UTC(parsed.year, parsed.month - 1 + Number(delta || 0), 1));
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, "0")}`;
}

function formatMonthLabel(monthKey) {
  const parsed = parseMonthKey(monthKey);
  if (!parsed) return "-";
  return `${parsed.year}.${String(parsed.month).padStart(2, "0")}`;
}

function daysInMonth(monthKey) {
  const parsed = parseMonthKey(monthKey);
  if (!parsed) return 30;
  return new Date(Date.UTC(parsed.year, parsed.month, 0)).getUTCDate();
}

function weekdayOfMonthStart(monthKey) {
  const parsed = parseMonthKey(monthKey);
  if (!parsed) return 0;
  return new Date(Date.UTC(parsed.year, parsed.month - 1, 1)).getUTCDay();
}

async function loadCalendarMonth(monthKey = state.calendarMonth) {
  const target = String(monthKey || "").trim() || monthKeyFromDate(currentDate()) || monthKeyFromDate(new Date().toISOString().slice(0, 10));
  state.calendarMonth = target;
  try {
    const data = await api(`/images/calendar${toQuery({ month: target })}`);
    state.calendarCounts = Object.fromEntries((data.days || []).map((item) => [item.date, Number(item.count || 0)]));
  } catch (err) {
    state.calendarCounts = {};
    log("Calendar month load failed", { month: target, error: err.message || String(err) });
  }
  renderWorkspaceCalendar();
}

function renderWorkspaceCalendar() {
  renderWorkspaceDateDisplay();
  const grid = el("calendarGrid");
  const label = el("calendarMonthLabel");
  if (!grid || !label) return;
  const monthKey = String(state.calendarMonth || monthKeyFromDate(currentDate()) || "").trim();
  label.textContent = formatMonthLabel(monthKey);
  const selectedDate = currentDate();
  const today = new Date().toISOString().slice(0, 10);
  const totalDays = daysInMonth(monthKey);
  const offset = weekdayOfMonthStart(monthKey);
  const cells = [];
  for (let i = 0; i < offset; i += 1) {
    cells.push('<div class="workspace-calendar-cell empty" aria-hidden="true"></div>');
  }
  for (let day = 1; day <= totalDays; day += 1) {
    const date = `${monthKey}-${String(day).padStart(2, "0")}`;
    const count = Number(state.calendarCounts?.[date] || 0);
    const classes = ["workspace-calendar-cell"];
    if (count > 0) classes.push("has-count");
    if (date === selectedDate) classes.push("is-selected");
    if (date === today) classes.push("is-today");
    cells.push(`
      <div class="${classes.join(" ")}">
        <button type="button" class="workspace-calendar-button" data-calendar-date="${date}">
          <span class="workspace-calendar-day">${day}</span>
          ${count > 0 ? `<span class="workspace-calendar-count">${count}</span>` : `<span class="workspace-calendar-count">&nbsp;</span>`}
        </button>
      </div>
    `);
  }
  grid.innerHTML = cells.join("");
  grid.querySelectorAll("[data-calendar-date]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const nextDate = String(btn.dataset.calendarDate || "").trim();
      if (!nextDate) return;
      el("workspaceDate").value = nextDate;
      state.calendarMonth = monthKeyFromDate(nextDate);
      resetBucketExportState();
      closeWorkspaceCalendar();
      renderWorkspaceCalendar();
      try {
        await loadWorkspace();
      } catch (err) {
        alert(err.message || String(err));
        log("Load workspace failed", { error: err.message || String(err) });
      }
    });
  });
}

function apiBase() {
  return value("apiBase").trim().replace(/\/$/, "");
}

function apiPrefixPath() {
  try {
    return new URL(apiBase()).pathname.replace(/\/$/, "") || "/v1";
  } catch (_err) {
    return "/v1";
  }
}

function normalizeApiPath(path) {
  const raw = String(path || "").trim();
  const prefix = apiPrefixPath();
  if (!raw) return raw;
  if (raw.startsWith(prefix + "/")) return raw.slice(prefix.length);
  if (raw === prefix) return "/";
  return raw;
}

function dailyZipHref(mode = "all") {
  const date = currentDate();
  if (!date) return "";
  return `${apiBase()}/daily/${encodeURIComponent(date)}/zip${toQuery({ mode })}`;
}

function exemplarZipHref() {
  return `${apiBase()}/exemplars/zip`;
}

function bucketZipHref() {
  const date = currentDate();
  if (!date) return "";
  const manifest = state.lastBucketManifest || null;
  return `${apiBase()}/buckets/${encodeURIComponent(date)}/zip${toQuery({ manifest })}`;
}

function resetBucketExportState() {
  state.lastBucketManifest = null;
  state.bucketSummary = null;
  renderBucketSummary();
}

function setInspectorDrawerOpen(open) {
  const drawer = el("inspectorDrawer");
  const backdrop = el("inspectorBackdrop");
  if (!drawer || !backdrop) return;
  drawer.classList.toggle("is-open", !!open);
  drawer.setAttribute("aria-hidden", open ? "false" : "true");
  backdrop.hidden = !open;
  document.body.classList.toggle("inspector-open", !!open);
}

function closeInspectorDrawer() {
  state.inspectedImageId = null;
  renderInspector(null);
}

window.closeInspectorDrawer = closeInspectorDrawer;

window.handleDownloadExemplarsZip = function handleDownloadExemplarsZip() {
  window.open(exemplarZipHref(), "_blank", "noopener,noreferrer");
};

function closeDailyDownloadMenu() {
  const menu = el("dailyDownloadMenu");
  if (menu) menu.hidden = true;
}

window.handleDownloadDailyZip = function handleDownloadDailyZip() {
  const menu = el("dailyDownloadMenu");
  if (!currentDate()) {
    alert("먼저 날짜를 선택하세요.");
    return;
  }
  if (menu) menu.hidden = !menu.hidden;
};

window.handleDownloadDailyZipMode = function handleDownloadDailyZipMode(mode) {
  const href = dailyZipHref(mode || "all");
  if (!href) {
    alert("먼저 날짜를 선택하세요.");
    return;
  }
  closeDailyDownloadMenu();
  window.open(href, "_blank", "noopener,noreferrer");
};

window.handleDownloadZip = function handleDownloadZip() {
  const href = bucketZipHref();
  if (!href || !state.lastBucketManifest) {
    alert("먼저 버킷 확정을 완료하세요.");
    return;
  }
  window.open(href, "_blank", "noopener,noreferrer");
};

async function api(path, init = {}) {
  const headers = { ...(init.headers || {}) };
  if (!(init.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers });
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    throw new Error(typeof body === "string" ? body : JSON.stringify(body));
  }
  return body;
}

function toQuery(params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    q.set(k, String(v));
  });
  const s = q.toString();
  return s ? `?${s}` : "";
}

function selectedImageIds() {
  return Array.from(state.selectedImageIds);
}

function selectedCountText() {
  return `선택된 이미지 ${state.selectedImageIds.size}장`;
}

function getSingleSelectedGalleryItem() {
  if (state.selectedImageIds.size !== 1) return null;
  const [imageId] = Array.from(state.selectedImageIds);
  return state.originalGalleryItems.find((item) => item.image_id === imageId) || null;
}

function syncSelectionActionButtons() {
  const assignButton = el("btnAssignSelected");
  if (assignButton) {
    const selectedItem = getSingleSelectedGalleryItem();
    const instanceCount = Number(selectedItem?.instance_count || 0);
    const canAssign = !!selectedItem && instanceCount === 1 && !!state.activePetId;
    assignButton.disabled = !canAssign;
  }
}

function currentTabForApi() {
  if (state.galleryView === "PET") return "PET";
  if (state.galleryView === "UNCLASSIFIED") return "UNCLASSIFIED";
  return "ALL";
}

function setButtonBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

async function withButtonBusy(button, task) {
  setButtonBusy(button, true);
  try {
    return await task();
  } finally {
    setButtonBusy(button, false);
  }
}

function setUploadProgress(progressId, { title = "", meta = "", detail = "", value = 0, max = 1, active = true } = {}) {
  const box = el(progressId);
  if (!box) return;
  const progress = box.querySelector("progress");
  const titleEl = box.querySelector(".upload-progress-title");
  const metaEl = box.querySelector(".upload-progress-meta");
  const detailEl = box.querySelector(".upload-progress-detail");
  box.dataset.active = active ? "true" : "false";
  if (progress) {
    progress.max = Math.max(1, Number(max || 1));
    progress.value = Math.min(progress.max, Math.max(0, Number(value || 0)));
  }
  if (titleEl) titleEl.textContent = title;
  if (metaEl) metaEl.textContent = meta;
  if (detailEl) {
    detailEl.textContent = detail;
    detailEl.hidden = !detail;
  }
}

function resetUploadProgress(progressId, meta = "") {
  setUploadProgress(progressId, {
    title: "대기 중",
    meta,
    value: 0,
    max: 1,
    active: false,
  });
}

function clearFileInput(id) {
  const input = el(id);
  if (input) input.value = "";
}

function classifyUploadStatus(status) {
  const requestStatus = String(status?.request_status || "").toUpperCase();
  const jobStatus = String(status?.job_status || "").toUpperCase();
  const ingestStatus = String(status?.ingest_status || "").toUpperCase();

  if (ingestStatus === "READY" || requestStatus === "COMPLETED" || requestStatus === "SUCCEEDED" || jobStatus === "SUCCEEDED") return "completed";
  if (ingestStatus === "FAILED" || requestStatus === "FAILED" || jobStatus === "FAILED") return "failed";
  if (jobStatus === "RUNNING" || jobStatus === "LEASED" || requestStatus === "PROCESSING" || ingestStatus === "PROCESSING") return "processing";
  if (jobStatus === "QUEUED" || requestStatus === "RECEIVED" || requestStatus === "QUEUED" || ingestStatus === "PENDING") return "queued";
  return "queued";
}

function uploadStatusTitle(status) {
  const phase = classifyUploadStatus(status);
  if (phase === "completed") return "완료";
  if (phase === "failed") return "실패";
  if (phase === "processing") return "처리 중";
  return "대기 중";
}

function uploadStatusMeta(status) {
  const pieces = [];
  if (status?.image_role) pieces.push(String(status.image_role).toUpperCase());
  if (status?.pipeline_stage) pieces.push(String(status.pipeline_stage).toUpperCase());
  if (status?.job_status) pieces.push(`job:${String(status.job_status).toUpperCase()}`);
  if (status?.ingest_status) pieces.push(`image:${String(status.ingest_status).toUpperCase()}`);
  return pieces.join(" · ");
}

function createUploadProgressTracker(progressId, { total = 1, metaPrefix = "" } = {}) {
  const trackedUrls = new Set();
  const latestStatuses = new Map();
  const normalizedTotal = Math.max(1, Number(total || 1));
  let requesting = 0;
  let completedImmediate = 0;
  let failedImmediate = 0;
  let uploadsFinished = false;
  let pollStarted = false;
  let pollTimedOut = false;
  let latestStatusList = [];
  let pollPromise = null;

  function counts() {
    let queued = 0;
    let processing = 0;
    let completedTracked = 0;
    let failedTracked = 0;
    for (const status of latestStatuses.values()) {
      const phase = classifyUploadStatus(status);
      if (phase === "completed") completedTracked += 1;
      else if (phase === "failed") failedTracked += 1;
      else if (phase === "processing") processing += 1;
      else queued += 1;
    }
    return {
      requesting,
      queued,
      processing,
      completed: completedImmediate + completedTracked,
      failed: failedImmediate + failedTracked,
    };
  }

  function detailText(currentCounts) {
    const parts = [
      `요청 중 ${currentCounts.requesting}`,
      `대기 중 ${currentCounts.queued}`,
      `처리 중 ${currentCounts.processing}`,
    ];
    if (currentCounts.failed > 0) parts.push(`실패 ${currentCounts.failed}`);
    return parts.join(" | ");
  }

  function render() {
    const currentCounts = counts();
    const terminal = currentCounts.completed + currentCounts.failed;
    let title = "요청 중";
    if (terminal >= normalizedTotal && currentCounts.failed === 0) title = "완료";
    else if (terminal >= normalizedTotal && currentCounts.failed > 0 && currentCounts.completed === 0) title = "실패";
    else if (currentCounts.processing > 0) title = "처리 중";
    else if (currentCounts.queued > 0) title = "대기 중";
    else if (currentCounts.requesting > 0) title = "요청 중";
    else if (currentCounts.failed > 0) title = "실패";

    const metaParts = [
      metaPrefix,
      `${terminal} / ${normalizedTotal} 완료`,
      currentCounts.completed > 0 ? `${currentCounts.completed} 완료됨` : "",
      currentCounts.failed > 0 ? `${currentCounts.failed} 실패` : "",
      pollTimedOut ? "상태 확인 중" : "",
    ].filter(Boolean);
    setUploadProgress(progressId, {
      title,
      meta: metaParts.join(" · "),
      detail: detailText(currentCounts),
      value: terminal,
      max: normalizedTotal,
      active: true,
    });
  }

  async function pollLoop() {
    const startedAt = Date.now();
    while (true) {
      const urls = Array.from(trackedUrls);
      if (urls.length > 0) {
        const results = await Promise.allSettled(urls.map((url) => api(normalizeApiPath(url))));
        latestStatusList = [];
        for (let index = 0; index < results.length; index += 1) {
          const result = results[index];
          if (result.status !== "fulfilled" || !result.value) continue;
          latestStatuses.set(urls[index], result.value);
          latestStatusList.push(result.value);
        }
      }

      if (!pollTimedOut && Date.now() - startedAt >= UPLOAD_STATUS_POLL_TIMEOUT_MS) {
        pollTimedOut = true;
        render();
      }

      render();
      const currentCounts = counts();
      const terminal = currentCounts.completed + currentCounts.failed;
      if (uploadsFinished && currentCounts.requesting === 0 && terminal >= normalizedTotal) {
        return latestStatusList;
      }
      await new Promise((resolve) => window.setTimeout(resolve, UPLOAD_STATUS_POLL_INTERVAL_MS));
    }
  }

  function startPolling() {
    if (pollStarted) return;
    pollStarted = true;
    pollPromise = pollLoop();
  }

  render();

  return {
    markRequestStarted(count = 1) {
      requesting += Math.max(0, Number(count || 0));
      render();
    },
    markRequestResolved(count = 1) {
      requesting = Math.max(0, requesting - Math.max(0, Number(count || 0)));
      render();
    },
    recordQueued(statusUrl, status = null) {
      const normalizedUrl = String(statusUrl || "").trim();
      if (!normalizedUrl) return;
      trackedUrls.add(normalizedUrl);
      latestStatuses.set(normalizedUrl, status || { request_status: "RECEIVED", job_status: "QUEUED", ingest_status: "PENDING" });
      startPolling();
      render();
    },
    recordImmediateSuccess(count = 1) {
      completedImmediate += Math.max(0, Number(count || 0));
      render();
    },
    recordImmediateFailure(count = 1) {
      failedImmediate += Math.max(0, Number(count || 0));
      render();
    },
    markUploadsFinished() {
      uploadsFinished = true;
      render();
    },
    async waitForCompletion() {
      if (!pollPromise) {
        render();
        return [];
      }
      return pollPromise;
    },
  };
}

function renderPetButtons() {
  const box = el("petButtons");
  box.innerHTML = "";
  el("petMeta").textContent = state.activePetId ? "active pet=" + state.activePetId : "";
  if (!state.pets.length) {
    box.innerHTML = '<div class="empty-state">초기 등록 pet이 아직 없습니다.</div>';
    return;
  }
  state.pets.forEach((pet) => {
    const btn = document.createElement("button");
    btn.className = `pet-pill${state.activePetId === pet.pet_id ? " active" : ""}`;
    btn.textContent = pet.pet_name || pet.pet_id;
    btn.addEventListener("click", async () => {
      state.activePetId = pet.pet_id;
      state.galleryView = "PET";
      resetSearchRanking();
      await Promise.all([loadActiveExemplars(), loadGallery()]);
      renderPetButtons();
      syncViewButtons();
    });
    box.appendChild(btn);
  });
}

function renderActiveExemplars() {
  const strip = el("activePetStrip");
  strip.innerHTML = "";
  if (!state.activePetId) {
    strip.innerHTML = '<div class="empty-state">pet 버튼을 선택하면 해당 seed 이미지가 표시됩니다.</div>';
    return;
  }
  if (!state.activeExemplars.length) {
    strip.innerHTML = '<div class="empty-state">선택된 pet의 seed exemplar가 없습니다.</div>';
    return;
  }
  state.activeExemplars.forEach((item) => {
    const card = document.createElement("div");
    card.className = "seed-card";
    card.dataset.instanceId = String(item.instance_id || "");
    card.dataset.petId = String(item.pet_id || "");
    card.dataset.imageId = String(item.image_id || "");
    const src = item.image_id ? `${apiBase()}/images/${encodeURIComponent(item.image_id)}?variant=thumb` : "";
    const imgName = displayImageName(item);
    card.innerHTML = `
      <img src="${src}" alt="${escapeHtml(item.pet_id)}" loading="lazy" />
      <span>${escapeHtml(item.pet_id)}</span>
      <div class="img-name" title="${escapeHtml(imgName)}">${escapeHtml(imgName)}</div>
    `;
    card.addEventListener("click", async () => {
      if (item.image_id) await inspectImage(String(item.image_id));
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openSeedContextMenu(event, item);
    });
    strip.appendChild(card);
  });
}

function inferCardState(item) {
  if (Array.isArray(item.pet_ids) && item.pet_ids.length > 0) return "accepted";
  return "unreviewed";
}

function inferCardMultiplicity(item) {
  const instanceCount = Number(item?.instance_count || 0);
  return instanceCount > 1 ? "MULTI" : "SINGLE";
}

function filteredGalleryItems() {
  return state.originalGalleryItems.filter((item) => {
    const cardState = inferCardState(item).toUpperCase();
    const multiplicity = inferCardMultiplicity(item);
    const petIds = Array.isArray(item.pet_ids) ? item.pet_ids.map((petId) => String(petId || "").trim()).filter(Boolean) : [];
    const targetPetQuery = String(state.galleryFilters.petId || "").trim().toLowerCase();
    const petTokens = petIds.flatMap((petId) => {
      const petName = petDisplayNameById(petId);
      return [String(petId || "").trim().toLowerCase(), String(petName || "").trim().toLowerCase()].filter(Boolean);
    });
    const hasTargetPet = targetPetQuery
      ? petTokens.some((token) => token.includes(targetPetQuery))
      : false;
    if (state.galleryFilters.state !== "ALL" && cardState !== state.galleryFilters.state) return false;
    if (state.galleryFilters.multiplicity !== "ALL" && multiplicity !== state.galleryFilters.multiplicity) return false;
    if (targetPetQuery && !hasTargetPet) return false;
    return true;
  });
}

function syncGalleryFilterControls() {
  const stateFilter = el("galleryStateFilter");
  const multiplicityFilter = el("galleryMultiplicityFilter");
  const petFilter = el("galleryPetFilter");
  const petFilterList = el("galleryPetFilterList");
  if (stateFilter) stateFilter.value = state.galleryFilters.state;
  if (multiplicityFilter) multiplicityFilter.value = state.galleryFilters.multiplicity;
  if (petFilter) petFilter.value = String(state.galleryFilters.petId || "");
  if (petFilterList) {
    const options = [];
    state.pets.forEach((pet) => {
      const petId = String(pet.pet_id || "").trim();
      const petName = String(pet.pet_name || pet.pet_id || "").trim() || petId;
      if (!petId) return;
      options.push(`<option value="${escapeHtml(petName)}"></option>`);
      if (petName !== petId) {
        options.push(`<option value="${escapeHtml(petId)}"></option>`);
      }
    });
    petFilterList.innerHTML = options.join('');
  }
}

function inferInstanceState(inst) {
  const status = String(inst.assignment_status || "").toUpperCase();
  if (status === "ACCEPTED") return "accepted";
  if (status === "REJECTED") return "rejected";
  return "unreviewed";
}

function petDisplayNameById(petId) {
  const key = String(petId || "").trim();
  if (!key) return "";
  const pet = state.pets.find((item) => String(item.pet_id || "") === key);
  return String(pet?.pet_name || pet?.pet_id || key).trim();
}

function displayInstancePet(inst, fallbackPetLabel = "") {
  const petId = String(inst?.pet_id || "").trim();
  if (petId) return petDisplayNameById(petId) || petId;
  if (inst?.auto_pet_id) return `${inst.auto_pet_id} (candidate)`;
  const fallback = String(fallbackPetLabel || "").trim();
  if (fallback) return fallback;
  return "미지정";
}

function petOptionsMarkup(selectedPetId = "") {
  const selected = String(selectedPetId || "");
  const options = ['<option value="">펫 선택</option>'];
  state.pets.forEach((pet) => {
    const value = String(pet.pet_id || "");
    const label = String(pet.pet_name || pet.pet_id || "");
    options.push(`<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`);
  });
  return options.join("");
}

function closeSeedContextMenu() {
  const menu = el("seedContextMenu");
  if (!menu) return;
  menu.hidden = true;
  state.seedContextTarget = null;
  const moveOptions = el("seedContextMoveOptions");
  if (moveOptions) moveOptions.hidden = true;
}

window.closeSeedContextMenu = closeSeedContextMenu;

function openSeedContextMenu(event, item) {
  const menu = el("seedContextMenu");
  if (!menu) return;
  state.seedContextTarget = item;
  const width = 190;
  const height = 220;
  const left = Math.min(event.clientX, window.innerWidth - width - 12);
  const top = Math.min(event.clientY, window.innerHeight - height - 12);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  menu.hidden = false;
}

function toggleSeedMoveOptions() {
  const options = el("seedContextMoveOptions");
  if (!options) return;
  options.hidden = !options.hidden;
}

async function moveSeedToDaily(mode) {
  const target = state.seedContextTarget;
  if (!target?.instance_id) return;
  const data = await api(`/exemplars/${encodeURIComponent(String(target.instance_id))}/move-to-daily`, {
    method: "POST",
    body: JSON.stringify({
      mode,
      updated_by: "admin_dashboard",
      target_date: currentDate() || null,
    }),
  });
  log("Exemplar moved to daily", data);
  if (mode === "ACCEPTED" && data.pet_id) {
    state.activePetId = String(data.pet_id);
    state.galleryView = "PET";
  } else {
    state.galleryView = "UNCLASSIFIED";
  }
  resetSearchRanking();
  closeSeedContextMenu();
  await loadPets();
  await Promise.all([loadActiveExemplars(), loadGallery()]);
  renderPetButtons();
  syncViewButtons();
  if (state.inspectedImageId && state.inspectedImageId === String(target.image_id || "")) {
    state.imageMetaCache.delete(state.inspectedImageId);
    state.inspectedImageId = null;
    renderInspector(null);
  }
}

async function deleteSeedExemplar() {
  const target = state.seedContextTarget;
  if (!target?.instance_id) return;
  const ok = window.confirm(`exemplar를 삭제할까요?
${displayImageName(target)}`);
  if (!ok) return;
  await api(`/exemplars/${encodeURIComponent(String(target.instance_id))}${toQuery({ updated_by: "admin_dashboard" })}`, { method: "DELETE" });
  log("Exemplar deleted", {
    instance_id: target.instance_id,
    pet_id: target.pet_id,
    image_id: target.image_id,
    img_name: displayImageName(target),
  });
  closeSeedContextMenu();
  await loadPets();
  if (state.galleryView === "PET" || state.galleryView === "ALL" || state.galleryView === "UNCLASSIFIED") {
    await loadGallery();
  }
  if (state.inspectedImageId && state.inspectedImageId === String(target.image_id || "")) {
    renderInspector(null);
    state.inspectedImageId = null;
  }
}

async function deleteDailyImage(imageId, imgName) {
  const imageIdClean = String(imageId || "").trim();
  if (!imageIdClean) return;
  const ok = window.confirm(`daily 이미지를 완전히 삭제할까요?
${String(imgName || imageIdClean)}`);
  if (!ok) return;
  const data = await api(`/images/${encodeURIComponent(imageIdClean)}${toQuery({ updated_by: "admin_dashboard" })}`, { method: "DELETE" });
  log("Daily image deleted", data);
  state.selectedImageIds.delete(imageIdClean);
  state.imageMetaCache.delete(imageIdClean);
  if (state.inspectedImageId === imageIdClean) {
    state.inspectedImageId = null;
    renderInspector(null);
  }
  await loadGallery();
}

function setFolderSeedPolicy(policy) {
  state.folderSeedPolicy = ["append", "create_new", "fail"].includes(policy) ? policy : "append";
  renderFolderSeedPolicy();
}

function renderFolderSeedPolicy() {
  const mapping = {
    btnFolderPolicyAppend: "append",
    btnFolderPolicyCreate: "create_new",
    btnFolderPolicyFail: "fail",
  };
  Object.entries(mapping).forEach(([id, policy]) => {
    const button = el(id);
    if (!button) return;
    const active = state.folderSeedPolicy === policy;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function setSingleSeedMode(mode) {
  state.singleSeedMode = mode === "create" ? "create" : "append";
  renderSingleSeedMode();
}

function renderSingleSeedMode() {
  const appendButton = el("btnSingleSeedAppendMode");
  const createButton = el("btnSingleSeedCreateMode");
  const existingWrap = el("singleSeedExistingWrap");
  const newWrap = el("singleSeedNewWrap");
  const existingSelect = el("uExistingPet");

  if (!appendButton || !createButton || !existingWrap || !newWrap || !existingSelect) return;

  existingSelect.innerHTML = petOptionsMarkup();

  const hasPets = state.pets.length > 0;
  if (!hasPets && state.singleSeedMode === "append") {
    state.singleSeedMode = "create";
  }

  appendButton.disabled = !hasPets;
  appendButton.classList.toggle("is-active", state.singleSeedMode === "append");
  createButton.classList.toggle("is-active", state.singleSeedMode === "create");

  existingWrap.hidden = false;
  existingSelect.disabled = !hasPets || state.singleSeedMode === "create";
  existingWrap.classList.toggle("is-disabled", existingSelect.disabled);
  newWrap.hidden = state.singleSeedMode !== "create";
}

function explainQuickUploadError(err) {
  try {
    const parsed = JSON.parse(String(err?.message || ""));
    const detail = parsed?.detail;
    if (detail?.code === "PET_NAME_CONFLICT") {
      const existing = Array.isArray(detail.existing_pet_ids) ? detail.existing_pet_ids.join(", ") : "";
      return `${detail.message}${existing ? `
기존 pet_id: ${existing}` : ""}`;
    }
    if (typeof detail === "string") return detail;
  } catch (_err) {
    // no-op
  }
  return err?.message || "업로드 중 오류가 발생했습니다.";
}

function looksLikeHtmlErrorMessage(message) {
  const text = String(message || "").trim();
  if (!text) return false;
  return text.startsWith("<!DOCTYPE html") || text.startsWith("<html") || text.includes("<!--[if IE");
}

function explainFolderUploadError(err, context = {}) {
  const raw = String(err?.message || "").trim();
  if (looksLikeHtmlErrorMessage(raw)) {
    const batchText = context.batchIndex && context.batchCount
      ? ` (배치 ${context.batchIndex}/${context.batchCount}, ${context.fileCount || 0}장)`
      : "";
    return `서버 앞단에서 HTML 오류 페이지를 반환했습니다${batchText}.
업로드 요청이 너무 크거나 오래 걸렸을 수 있습니다. 폴더를 더 작게 나누어 다시 시도해 주세요.`;
  }
  return raw || "폴더 업로드 중 오류가 발생했습니다.";
}

function folderGroupKey(file) {
  const rel = String(file?.webkitRelativePath || file?.name || "");
  const parts = rel.split("/").filter(Boolean);
  return parts.length >= 2 ? parts[parts.length - 2] : "__root__";
}

function buildFolderUploadBatches(files, maxFilesPerBatch = 24) {
  const grouped = [];
  const byKey = new Map();
  files.forEach((file) => {
    const key = folderGroupKey(file);
    if (!byKey.has(key)) {
      const group = { key, files: [] };
      byKey.set(key, group);
      grouped.push(group);
    }
    byKey.get(key).files.push(file);
  });

  const batches = [];
  let currentFiles = [];
  let currentFolderKeys = [];
  let currentCount = 0;
  grouped.forEach((group) => {
    const size = group.files.length;
    if (currentFiles.length && currentCount + size > maxFilesPerBatch) {
      batches.push({ files: currentFiles, folderKeys: currentFolderKeys });
      currentFiles = [];
      currentFolderKeys = [];
      currentCount = 0;
    }
    if (size > maxFilesPerBatch) {
      if (currentFiles.length) {
        batches.push({ files: currentFiles, folderKeys: currentFolderKeys });
        currentFiles = [];
        currentFolderKeys = [];
        currentCount = 0;
      }
      batches.push({ files: group.files.slice(), folderKeys: [group.key] });
      return;
    }
    currentFiles.push(...group.files);
    currentFolderKeys.push(group.key);
    currentCount += size;
  });
  if (currentFiles.length) batches.push({ files: currentFiles, folderKeys: currentFolderKeys });
  return batches;
}

function clamp01(v) {
  return Math.max(0, Math.min(1, Number(v || 0)));
}

function getContainedImageRect(img) {
  const boxWidth = img.clientWidth || 0;
  const boxHeight = img.clientHeight || 0;
  const naturalWidth = img.naturalWidth || boxWidth;
  const naturalHeight = img.naturalHeight || boxHeight;
  if (!boxWidth || !boxHeight || !naturalWidth || !naturalHeight) {
    return { left: 0, top: 0, width: boxWidth, height: boxHeight };
  }
  const scale = Math.min(boxWidth / naturalWidth, boxHeight / naturalHeight);
  const width = naturalWidth * scale;
  const height = naturalHeight * scale;
  return {
    left: (boxWidth - width) / 2,
    top: (boxHeight - height) / 2,
    width,
    height,
  };
}

function positionOverlayBox(box, bbox, imageRect) {
  const x1 = clamp01(bbox.x1);
  const y1 = clamp01(bbox.y1);
  const x2 = clamp01(bbox.x2);
  const y2 = clamp01(bbox.y2);
  const left = imageRect.left + imageRect.width * x1;
  const top = imageRect.top + imageRect.height * y1;
  const width = Math.max(0, imageRect.width * (x2 - x1));
  const height = Math.max(0, imageRect.height * (y2 - y1));
  box.style.left = `${left}px`;
  box.style.top = `${top}px`;
  box.style.width = `${width}px`;
  box.style.height = `${height}px`;
}

function layoutInspectorOverlays(preview, img, overlay, instances) {
  if (!preview || !img || !overlay) return;
  const imageRect = getContainedImageRect(img);
  overlay.style.left = '0';
  overlay.style.top = '0';
  overlay.style.width = `${preview.clientWidth}px`;
  overlay.style.height = `${preview.clientHeight}px`;
  overlay.querySelectorAll('.bbox-overlay').forEach((box, idx) => {
    positionOverlayBox(box, instances[idx]?.bbox || {}, imageRect);
  });
}

function bindInspectorRelayout(preview, img, overlay, instances) {
  if (inspectorResizeObserver) {
    inspectorResizeObserver.disconnect();
    inspectorResizeObserver = null;
  }
  const relayout = () => layoutInspectorOverlays(preview, img, overlay, instances);
  if (img) {
    if (img.complete) relayout();
    else img.addEventListener('load', relayout, { once: true });
  }
  if (preview && typeof ResizeObserver !== 'undefined') {
    inspectorResizeObserver = new ResizeObserver(() => relayout());
    inspectorResizeObserver.observe(preview);
    if (img) inspectorResizeObserver.observe(img);
  }
  return relayout;
}

function setActiveInspectorInstance(pane, instanceId) {
  if (!pane) return;
  pane.querySelectorAll(".bbox-overlay").forEach((node) => {
    node.classList.toggle("active", node.dataset.instanceId === instanceId);
  });
  pane.querySelectorAll(".instance-row").forEach((node) => {
    node.classList.toggle("active", node.dataset.instanceId === instanceId);
  });
}

function renderGallery() {
  const grid = el("galleryGrid");
  grid.innerHTML = "";
  const filteredItems = filteredGalleryItems();
  const pageSize = Math.max(1, Number(state.galleryPageSize || 100));
  const visibleStart = filteredItems.length ? ((state.galleryPage - 1) * pageSize) + 1 : 0;
  const visibleEnd = filteredItems.length ? ((state.galleryPage - 1) * pageSize) + state.galleryItems.length : 0;
  const filteredText = filteredItems.length ? `${visibleStart}-${visibleEnd}/${filteredItems.length}` : `${state.galleryItems.length}`;
  const summaryText = `${filteredText} filtered · total=${state.galleryTotalCount} · view=${state.galleryView}${state.activePetId ? ` · pet=${state.activePetId}` : ""}`;
  el("selectionMeta").textContent = selectedCountText();
  syncSelectionActionButtons();
  syncGalleryFilterControls();
  renderGalleryPagination(summaryText);

  if (!state.galleryItems.length) {
    grid.innerHTML = '<div class="empty-state">표시할 daily 이미지가 없습니다.</div>';
    return;
  }

  state.galleryItems.forEach((item) => {
    const selected = state.selectedImageIds.has(item.image_id);
    const card = document.createElement("article");
    card.className = `gallery-card${selected ? " selected" : ""}`;
    const score = state.searchScores[item.image_id];
    const petLabel = Array.isArray(item.pet_ids) && item.pet_ids.length > 0 ? item.pet_ids.join(", ") : "미지정";
    const cardState = inferCardState(item);
    const ingestStatus = String(item.ingest_status || "").toUpperCase();
    const instanceCount = Number(item.instance_count || 0);
    const multiplicity = inferCardMultiplicity(item);
    card.innerHTML = `
      <div class="gallery-thumb-wrap">
        <input class="card-check" type="checkbox" ${selected ? "checked" : ""} data-image-id="${item.image_id}" />
        <img class="gallery-thumb" src="${item.thumb_url}" alt="${item.image_id}" loading="lazy" data-inspect="${item.image_id}" />
      </div>
      <div class="card-lines">
        <div class="inline-actions wrap card-badges">
          <span class="role-badge">${item.image_role}</span>
          <span class="state-badge ${cardState}">${cardState.toUpperCase()}</span>
          ${ingestStatus && ingestStatus !== "READY" ? `<span class="state-badge unreviewed">${ingestStatus}</span>` : ""}
        </div>
        <div class="card-score-row"><span class="multiplicity-badge ${instanceCount > 1 ? "multi" : "single"}">${multiplicity}</span></div>
        ${score !== undefined ? `<div class="card-score-row"><span class="score-badge">sim ${score.toFixed(2)}</span></div>` : ""}
        <div class="pet-label">${petLabel}</div>
        <div class="pet-sub filename" title="${escapeHtml(displayImageName(item))}"><code>${escapeHtml(displayImageName(item))}</code></div>
        <div class="instance-meta">instances=${item.instance_count}${item.pipeline_stage ? ` · ${escapeHtml(String(item.pipeline_stage))}` : ""}</div>
      </div>
    `;
    grid.appendChild(card);
  });

  grid.querySelectorAll(".card-check").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const imageId = checkbox.dataset.imageId;
      if (checkbox.checked) state.selectedImageIds.add(imageId);
      else state.selectedImageIds.delete(imageId);
      renderGallery();
    });
  });

  grid.querySelectorAll("[data-inspect]").forEach((node) => {
    node.addEventListener("click", async () => {
      await inspectImage(node.dataset.inspect);
    });
  });
}

function resolveSeedInspectorTarget(meta, inst) {
  const imageId = String(meta?.image?.image_id || "").trim();
  const instanceId = String(inst?.instance_id || "").trim();
  const exact = state.activeExemplars.find((item) => String(item.instance_id || "") === instanceId);
  if (exact) return exact;
  const byImage = state.activeExemplars.find((item) => String(item.image_id || "") === imageId);
  if (byImage) return byImage;
  return {
    instance_id: instanceId || null,
    image_id: imageId || null,
    pet_id: String(inst?.pet_id || inst?.seed_pet_id || state.activePetId || "").trim() || null,
    img_name: String(meta?.image?.img_name || meta?.image?.image_id || "").trim() || null,
  };
}

function renderInspector(meta) {
  const pane = el("detailPane");
  const detailMeta = el("detailMeta");
  if (inspectorResizeObserver) {
    inspectorResizeObserver.disconnect();
    inspectorResizeObserver = null;
  }
  if (!meta) {
    pane.className = "detail-empty";
    pane.textContent = "이미지를 클릭하면 instance 상세가 표시됩니다.";
    detailMeta.textContent = "";
    setInspectorDrawerOpen(false);
    return;
  }
  detailMeta.textContent = meta.image?.image_id || "";
  setInspectorDrawerOpen(true);
  pane.className = "detail-body";
  const imageRole = String(meta.image?.image_role || "").toUpperCase();
  const isSeedImage = imageRole === "SEED";
  const petLabel = Array.isArray(meta.image?.pet_ids) && meta.image.pet_ids.length > 0
    ? meta.image.pet_ids.join(", ")
    : (isSeedImage ? String(meta.image?.pet_name || state.activePetId || "미지정") : "미지정");
  const instances = Array.isArray(meta.instances) ? meta.instances : [];
  pane.innerHTML = `
    <div class="detail-hero">
      <div class="detail-preview" title="클릭하여 확대/축소">
        <img class="detail-preview-image" src="${meta.image?.raw_url || meta.image?.thumb_url || ""}" alt="${meta.image?.image_id || "detail"}" loading="lazy" />
        <div class="detail-overlay-layer"></div>
      </div>
      <div>
        <div class="pet-label">${petLabel}</div>
        <div class="pet-sub filename" title="${escapeHtml(meta.image?.img_name || meta.image?.image_id || "")}"><code>${escapeHtml(meta.image?.img_name || meta.image?.image_id || "")}</code></div>
        <div class="pet-sub">captured_at=${meta.image?.captured_at || "n/a"}</div>
        <div class="pet-sub">role=${imageRole || "UNKNOWN"}</div>
        ${isSeedImage ? "" : `<div class="instance-action-buttons detail-image-actions"><button class="danger" data-image-delete="${escapeHtml(String(meta.image?.image_id || ""))}">이미지 완전 삭제</button></div>`}
      </div>
    </div>
    <div class="instance-list"></div>
  `;
  const preview = pane.querySelector(".detail-preview");
  const img = pane.querySelector(".detail-preview-image");
  const overlay = pane.querySelector(".detail-overlay-layer");
  const list = pane.querySelector(".instance-list");
  const relayout = bindInspectorRelayout(preview, img, overlay, instances);
  preview?.addEventListener("click", (event) => {
    if (event.target.closest(".bbox-overlay")) return;
    preview.classList.toggle("expanded");
    relayout();
  });
  instances.forEach((inst, idx) => {
    const stateClass = inferInstanceState(inst);
    const seedFallbackLabel = isSeedImage ? (String(meta.image?.pet_name || "").trim() || petDisplayNameById(state.activePetId)) : "";
    const label = displayInstancePet(inst, seedFallbackLabel);
    const number = idx + 1;
    const box = document.createElement("button");
    box.type = "button";
    box.className = `bbox-overlay ${stateClass}`;
    box.dataset.instanceId = inst.instance_id;
    box.innerHTML = `<span class="bbox-chip">#${number} ${label}</span>`;
    box.addEventListener("mouseenter", () => setActiveInspectorInstance(pane, inst.instance_id));
    box.addEventListener("focus", () => setActiveInspectorInstance(pane, inst.instance_id));
    box.addEventListener("click", () => setActiveInspectorInstance(pane, inst.instance_id));
    overlay.appendChild(box);

    const preferredPetId = String(inst.pet_id || state.activePetId || "");
    const seedTarget = isSeedImage ? resolveSeedInspectorTarget(meta, inst) : null;
    const row = document.createElement("div");
    row.className = `instance-row ${stateClass}`;
    row.dataset.instanceId = inst.instance_id;
    row.innerHTML = isSeedImage ? `
      <div class="instance-main">
        <div class="instance-title-line">
          <span class="instance-chip ${stateClass}">#${number}</span>
          <strong>${label}</strong>
        </div>
        <div class="sub">${inst.species} · conf=${Number(inst.confidence || 0).toFixed(3)}</div>
        <div class="sub"><code>${inst.instance_id}</code></div>
      </div>
      <div class="instance-actions">
        <div class="instance-select-wrap instance-exemplar-actions">
          <span>Exemplar 작업</span>
          <div class="instance-action-buttons">
            <button data-seed-move-unclassified="${inst.instance_id}">미분류로 이동</button>
            <button class="primary" data-seed-move-accepted="${inst.instance_id}">현재 pet 버킷으로 이동</button>
            <button class="danger" data-seed-delete="${inst.instance_id}">exemplar에서 삭제</button>
          </div>
        </div>
      </div>
    ` : `
      <div class="instance-main">
        <div class="instance-title-line">
          <span class="instance-chip ${stateClass}">#${number}</span>
          <strong>${label}</strong>
        </div>
        <div class="sub">${inst.species} · conf=${Number(inst.confidence || 0).toFixed(3)}</div>
        <div class="sub"><code>${inst.instance_id}</code></div>
      </div>
      <div class="instance-actions">
        <label class="instance-select-wrap">
          <span>펫 지정</span>
          <select data-instance-pet="${inst.instance_id}">${petOptionsMarkup(preferredPetId)}</select>
        </label>
        <div class="instance-action-buttons">
          <button class="primary" data-instance-assign="${inst.instance_id}">선택한 펫으로 지정</button>
          <button data-instance-clear="${inst.instance_id}">미지정으로 변경</button>
          <button class="danger" data-instance-remove="${inst.instance_id}">검출 제외</button>
        </div>
      </div>
    `;
    row.addEventListener("mouseenter", () => setActiveInspectorInstance(pane, inst.instance_id));
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, select, label")) return;
      setActiveInspectorInstance(pane, inst.instance_id);
    });
    if (seedTarget) {
      row.dataset.seedTarget = JSON.stringify(seedTarget);
    }
    list.appendChild(row);
  });
  pane.querySelectorAll("[data-instance-assign]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        const instanceId = btn.dataset.instanceAssign;
        const select = pane.querySelector(`[data-instance-pet="${instanceId}"]`);
        const petId = select ? String(select.value || "").trim() : "";
        if (!petId) throw new Error("지정할 펫을 선택하세요.");
        await withButtonBusy(btn, () => applyInstanceLabel(instanceId, "ACCEPT", petId));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-instance-clear]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await withButtonBusy(btn, () => applyInstanceLabel(btn.dataset.instanceClear, "CLEAR"));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-instance-remove]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        if (!confirm("이 검출 instance를 제거할까요? 원본 이미지는 유지됩니다.")) return;
        await withButtonBusy(btn, () => removeInspectorInstance(btn.dataset.instanceRemove));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-image-delete]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await withButtonBusy(btn, () => deleteDailyImage(btn.dataset.imageDelete, meta.image?.img_name || meta.image?.image_id));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-seed-move-unclassified]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        const row = btn.closest(".instance-row");
        state.seedContextTarget = JSON.parse(row?.dataset.seedTarget || '{}');
        await withButtonBusy(btn, () => moveSeedToDaily("UNCLASSIFIED"));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-seed-move-accepted]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        const row = btn.closest(".instance-row");
        state.seedContextTarget = JSON.parse(row?.dataset.seedTarget || '{}');
        await withButtonBusy(btn, () => moveSeedToDaily("ACCEPTED"));
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  pane.querySelectorAll("[data-seed-delete]").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        const row = btn.closest(".instance-row");
        state.seedContextTarget = JSON.parse(row?.dataset.seedTarget || '{}');
        await withButtonBusy(btn, () => deleteSeedExemplar());
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  });
  relayout();
  if (instances.length > 0) {
    setActiveInspectorInstance(pane, instances[0].instance_id);
  }
}


function formatCount(value) {
  return Number(value || 0).toLocaleString("ko-KR");
}

function formatRatio(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function displayImageName(item) {
  const imgName = String(item?.img_name || "").trim();
  if (imgName) return imgName;
  const imageId = String(item?.image_id || "").trim();
  if (imageId) return imageId;
  return "unknown";
}

function renderBucketSummary() {
  const pane = el("bucketSummaryPane");
  const meta = el("bucketSummaryMeta");
  if (!pane || !meta) return;

  const summary = state.bucketSummary;
  if (!summary) {
    meta.textContent = "";
    pane.className = "bucket-summary-empty";
    pane.textContent = "버킷 확정 후 통계가 표시됩니다.";
    return;
  }

  const quality = summary.quality_metrics || {};
  const buckets = Array.isArray(summary.buckets) ? [...summary.buckets] : [];
  const sortedBuckets = buckets.sort((a, b) => Number(b.count || 0) - Number(a.count || 0));
  const maxBucketCount = sortedBuckets.reduce((acc, item) => Math.max(acc, Number(item.count || 0)), 0) || 1;
  const totalInstances = Number(quality.total_instances || 0);
  const acceptedInstances = Number(quality.accepted_instances || 0);
  const unreviewedInstances = Number(quality.unreviewed_instances || 0);
  const rejectedInstances = Number(quality.rejected_instances || 0);
  const statusItems = [
    { label: "확정", className: "accepted", count: acceptedInstances },
    { label: "미분류", className: "unreviewed", count: unreviewedInstances },
    { label: "제외", className: "rejected", count: rejectedInstances },
  ];
  const cards = [
    { label: "버킷 수", value: formatCount(summary.bucket_count) },
    { label: "총 이미지 수", value: formatCount(summary.total_images) },
    { label: "총 인스턴스 수", value: formatCount(totalInstances) },
    { label: "미분류 인스턴스", value: formatCount(unreviewedInstances) },
    { label: "자동 확정 수", value: formatCount(quality.accepted_auto_instances) },
  ];

  meta.textContent = `${summary.date ? `${summary.date}` : ""}${summary.finalized_at ? ` · finalized ${summary.finalized_at}` : ""}`;
  pane.className = "bucket-summary-body";
  pane.innerHTML = `
    <div class="bucket-kpi-grid">
      ${cards.map((card) => `
        <article class="bucket-kpi-card">
          <div class="bucket-kpi-label">${escapeHtml(card.label)}</div>
          <div class="bucket-kpi-value">${escapeHtml(card.value)}</div>
        </article>
      `).join("")}
    </div>

    <div class="bucket-summary-grid">
      <section class="bucket-block">
        <div class="bucket-block-head">
          <h3>인스턴스 상태</h3>
          <span>${formatCount(totalInstances)}개</span>
        </div>
        <div class="bucket-status-bar">
          ${statusItems.map((item) => {
            const width = totalInstances > 0 ? (item.count / totalInstances) * 100 : 0;
            return `<span class="bucket-status-segment ${item.className}" style="width:${width.toFixed(2)}%"></span>`;
          }).join("")}
        </div>
        <div class="bucket-status-legend">
          ${statusItems.map((item) => `
            <div class="bucket-status-item">
              <span class="bucket-status-dot ${item.className}"></span>
              <strong>${escapeHtml(item.label)}</strong>
              <span>${formatCount(item.count)} · ${formatRatio(totalInstances > 0 ? item.count / totalInstances : 0)}</span>
            </div>
          `).join("")}
        </div>
      </section>

      <section class="bucket-block">
        <div class="bucket-block-head">
          <h3>버킷별 이미지 수</h3>
          <span>${formatCount(summary.bucket_count)} buckets</span>
        </div>
        ${sortedBuckets.length ? `
          <div class="bucket-bars">
            ${sortedBuckets.map((bucket) => {
              const label = String(bucket.pet_name || bucket.pet_id || "미지정");
              const count = Number(bucket.count || 0);
              const width = (count / maxBucketCount) * 100;
              return `
                <div class="bucket-bar-row">
                  <div class="bucket-bar-label" title="${escapeHtml(label)}">${escapeHtml(label)}</div>
                  <div class="bucket-bar-track"><span class="bucket-bar-fill" style="width:${width.toFixed(2)}%"></span></div>
                  <div class="bucket-bar-value">${formatCount(count)}</div>
                </div>
              `;
            }).join("")}
          </div>
        ` : '<div class="empty-state">확정된 버킷이 아직 없습니다.</div>'}
      </section>
    </div>

    <section class="bucket-block bucket-table-block">
      <div class="bucket-block-head">
        <h3>버킷 상세</h3>
      </div>
      ${sortedBuckets.length ? `
        <div class="bucket-table-wrap compact">
          <table class="bucket-table compact">
            <thead>
              <tr>
                <th>버킷</th>
                <th>이미지 수</th>
                <th>총 개체 수</th>
              </tr>
            </thead>
            <tbody>
              ${sortedBuckets.map((bucket) => {
                const label = String(bucket.pet_name || bucket.pet_id || "미지정");
                return `
                  <tr>
                    <td>${escapeHtml(label)}</td>
                    <td>${formatCount(bucket.count)}</td>
                    <td>${formatCount(bucket.instance_count)}</td>
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        </div>
      ` : '<div class="empty-state">버킷 상세를 표시할 항목이 없습니다.</div>'}
    </section>
  `;
}

function syncViewButtons() {
  ["btnViewAll", "btnViewUnclassified", "btnViewPet"].forEach((id) => {
    const button = el(id);
    if (!button) return;
    button.classList.toggle("active", button.dataset.view === state.galleryView);
    button.classList.toggle("is-active", button.dataset.view === state.galleryView);
  });
}

function applyGalleryPageSlice() {
  const items = filteredGalleryItems();
  const pageSize = Math.max(1, Number(state.galleryPageSize || 100));
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  state.galleryPage = Math.min(Math.max(1, Number(state.galleryPage || 1)), totalPages);
  const start = (state.galleryPage - 1) * pageSize;
  state.galleryItems = items.slice(start, start + pageSize);
}

function renderGalleryPagination(summaryText) {
  const box = el("galleryMeta");
  if (!box) return;
  const totalItems = filteredGalleryItems().length;
  const pageSize = Math.max(1, Number(state.galleryPageSize || 100));
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));

  if (totalItems <= pageSize) {
    box.innerHTML = `<span class="gallery-summary-text">${escapeHtml(summaryText)}</span>`;
    return;
  }

  box.innerHTML = `
    <div class="gallery-pagination">
      <button id="btnGalleryPrevPage" type="button" class="gallery-page-button" ${state.galleryPage <= 1 ? "disabled" : ""}>&lt;</button>
      <span class="gallery-page-status">
        <input id="galleryPageInput" class="gallery-page-input" type="number" min="1" max="${totalPages}" value="${state.galleryPage}" />
        / ${totalPages}
      </span>
      <button id="btnGalleryNextPage" type="button" class="gallery-page-button" ${state.galleryPage >= totalPages ? "disabled" : ""}>&gt;</button>
    </div>
    <span class="gallery-summary-text">${escapeHtml(summaryText)}</span>
  `;

  box.querySelector("#btnGalleryPrevPage")?.addEventListener("click", () => {
    if (state.galleryPage <= 1) return;
    state.galleryPage -= 1;
    applyGalleryPageSlice();
    renderGallery();
  });

  box.querySelector("#btnGalleryNextPage")?.addEventListener("click", () => {
    if (state.galleryPage >= totalPages) return;
    state.galleryPage += 1;
    applyGalleryPageSlice();
    renderGallery();
  });

  const pageInput = box.querySelector("#galleryPageInput");
  const commitPageInput = () => {
    const nextPage = Math.min(totalPages, Math.max(1, Number(pageInput?.value || state.galleryPage)));
    if (Number.isNaN(nextPage)) {
      if (pageInput) pageInput.value = String(state.galleryPage);
      return;
    }
    state.galleryPage = nextPage;
    applyGalleryPageSlice();
    renderGallery();
  };
  pageInput?.addEventListener("change", commitPageInput);
  pageInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitPageInput();
    }
  });
}

function resetSearchRanking() {
  state.searchScores = {};
  applyGalleryPageSlice();
}

function selectAllVisibleImages() {
  state.selectedImageIds = new Set(state.galleryItems.map((item) => item.image_id));
  renderGallery();
}

function clearSelectedImages() {
  state.selectedImageIds.clear();
  renderGallery();
}

async function loadQdrantStatus() {
  const data = await api("/health/qdrant");
  const q = data.qdrant || {};
  el("qdrantStatusBox").textContent = JSON.stringify(
    {
      status: data.status,
      collection: q.collection,
      points_count: q.points_count,
      total_images: q.total_images,
      sampled_vector_dim: q.sampled_vector_dim,
      status_detail: q.status,
    },
    null,
    2
  );
}

async function loadPets() {
  const data = await api(`/pets`);
  state.pets = data.items || [];
  if (state.activePetId && !state.pets.find((item) => item.pet_id === state.activePetId)) {
    state.activePetId = null;
  }
  if (!state.activePetId && state.pets.length > 0) {
    state.activePetId = state.pets[0].pet_id;
  }
  renderPetButtons();
  renderSingleSeedMode();
  renderFolderSeedPolicy();
  if (!state.activePetId) {
    state.activeExemplars = [];
    renderActiveExemplars();
    return;
  }
  await loadActiveExemplars();
}

async function loadActiveExemplars() {
  if (!state.activePetId) {
    state.activeExemplars = [];
    renderActiveExemplars();
    return;
  }
  const data = await api(
    `/exemplars${toQuery({ pet_id: state.activePetId, active: true, limit: 30, offset: 0 })}`
  );
  state.activeExemplars = data.items || [];
  renderActiveExemplars();
}

async function loadGallery(options = {}) {
  if (state.galleryView === "PET" && !state.activePetId) {
    throw new Error("PET bucket view는 pet 버튼 선택 후 사용할 수 있습니다.");
  }
  state.galleryLoadInFlight = true;
  const pageSize = 100;
  try {
    const baseParams = {
      date: currentDate() || null,
      tab: currentTabForApi(),
      pet_id: state.galleryView === "PET" ? state.activePetId : null,
      include_seed: false,
      limit: pageSize,
    };

    let offset = 0;
    let totalCount = 0;
    const allItems = [];
    while (true) {
      const data = await api(`/images${toQuery({ ...baseParams, offset })}`);
      const pageItems = Array.isArray(data.items) ? data.items : [];
      totalCount = Number(data.count || 0);
      allItems.push(...pageItems);
      if (!pageItems.length) break;
      offset += pageItems.length;
      if (allItems.length >= totalCount) break;
      if (pageItems.length < pageSize) break;
    }

    state.originalGalleryItems = allItems;
    state.galleryTotalCount = Math.max(totalCount, allItems.length);
    state.galleryPage = 1;
    applyGalleryPageSlice();
    state.selectedImageIds.clear();
    state.imageMetaCache.clear();
    state.searchScores = {};
    state.galleryAutoRefreshEnabled = hasPendingGalleryItems(allItems) || hasPendingActiveExemplars() || Date.now() < Number(state.galleryAutoRefreshUntil || 0);
    renderGallery();
    log("Loaded gallery", {
      view: state.galleryView,
      count: state.originalGalleryItems.length,
      total_count: state.galleryTotalCount,
      pages: Math.max(1, Math.ceil(state.originalGalleryItems.length / pageSize)),
      pet_id: state.activePetId,
      auto_refresh: state.galleryAutoRefreshEnabled,
      reason: options.reason || "manual",
    });
  } finally {
    state.galleryLoadInFlight = false;
    scheduleGalleryAutoRefresh();
  }
}

async function inspectImage(imageId) {
  state.inspectedImageId = imageId;
  let meta = state.imageMetaCache.get(imageId);
  if (!meta) {
    meta = await api(`/images/${encodeURIComponent(imageId)}/meta`);
    state.imageMetaCache.set(imageId, meta);
  }
  renderInspector(meta);
}

async function refreshInspectorImage() {
  if (!state.inspectedImageId) return;
  state.imageMetaCache.delete(state.inspectedImageId);
  try {
    await inspectImage(state.inspectedImageId);
  } catch (_err) {
    renderInspector(null);
  }
}

async function applyInstanceLabel(instanceId, action, petId = null) {
  const assignments = [{
    instance_id: instanceId,
    action,
    pet_id: action === "ACCEPT" ? String(petId || "") : null,
    source: "MANUAL",
    confidence: 1.0,
  }];
  await api("/labels", {
    method: "POST",
    body: JSON.stringify({
      labeled_by: "admin_dashboard",
      assignments,
    }),
  });
  await loadGallery();
  await refreshInspectorImage();
}

async function removeInspectorInstance(instanceId) {
  await api(`/admin/instances/${encodeURIComponent(instanceId)}`, { method: "DELETE" });
  await loadGallery();
  await refreshInspectorImage();
}

async function loadBucketSummary() {
  const date = currentDate();
  if (!date) {
    state.bucketSummary = null;
    renderBucketSummary();
    return;
  }
  try {
    const data = await api(`/buckets/${encodeURIComponent(date)}`);
    state.bucketSummary = data;
    state.lastBucketManifest = (String(data.manifest_path || "").split("/").pop() || "").trim() || null;
  } catch (_err) {
    state.bucketSummary = null;
    state.lastBucketManifest = null;
  }
  renderBucketSummary();
}

async function loadWorkspace() {
  el("workspaceMeta").textContent = `${currentDate() ? `${currentDate()}` : ""}`;
  const monthKey = monthKeyFromDate(currentDate());
  if (monthKey && state.calendarMonth !== monthKey) {
    state.calendarMonth = monthKey;
  }
  await Promise.all([loadPets(), state.activePetId ? loadActiveExemplars() : Promise.resolve()]);
  await Promise.all([loadGallery(), loadBucketSummary(), loadCalendarMonth(state.calendarMonth)]);
}

async function autoClassify() {
  const date = currentDate();
  if (!date) throw new Error("자동 분류에는 date가 필요합니다.");
  const autoThreshold = Number(value("autoAcceptThreshold") || 0.6);
  const body = {
    date,
    auto_accept_threshold: autoThreshold,
    candidate_threshold: autoThreshold,
    labeled_by: "admin_dashboard",
  };
  const data = await api("/classify/auto", { method: "POST", body: JSON.stringify(body) });
  el("opsMeta").textContent = `accepted=${data.summary.accepted} · candidate=${data.summary.unreviewed_candidate} · no_candidate=${data.summary.unreviewed_no_candidate}`;
  log("Auto classify done", data.summary);
  await loadGallery();
}

async function finalizeBuckets() {
  const date = currentDate();
  if (!date) throw new Error("버킷 확정에는 date가 필요합니다.");
  const data = await api("/buckets/finalize", {
    method: "POST",
    body: JSON.stringify({ date }),
  });
  state.lastBucketManifest = (String(data.manifest_path || "").split("/").pop() || "").trim() || null;
  state.bucketSummary = data;
  renderBucketSummary();
  const quality = data.quality_metrics || {};
  el("opsMeta").textContent = `bucket_count=${data.bucket_count} · total_images=${data.total_images} · total_instances=${quality.total_instances || 0}`;
  log("Buckets finalized", { bucket_count: data.bucket_count, total_images: data.total_images, manifest_path: data.manifest_path });
}

async function resolveRepresentativeInstanceIds(imageIds) {
  const ids = [];
  for (const imageId of imageIds) {
    let meta = state.imageMetaCache.get(imageId);
    if (!meta) {
      meta = await api(`/images/${encodeURIComponent(imageId)}/meta`);
      state.imageMetaCache.set(imageId, meta);
    }
    const instances = Array.isArray(meta.instances) ? meta.instances : [];
    if (!instances.length) continue;
    let chosen = null;
    if (state.activePetId) {
      chosen = instances.find(
        (inst) => String(inst.pet_id || "") === state.activePetId && String(inst.assignment_status || "").toUpperCase() === "ACCEPTED"
      );
    }
    if (!chosen) {
      chosen = [...instances].sort((a, b) => Number(b.confidence || 0) - Number(a.confidence || 0))[0];
    }
    if (chosen?.instance_id) ids.push(chosen.instance_id);
  }
  return [...new Set(ids)];
}

async function runSimilarSearch() {
  const date = currentDate();
  const imageIds = selectedImageIds();
  if (!date) throw new Error("유사 정렬에는 date가 필요합니다.");
  if (!imageIds.length) throw new Error("유사 정렬 기준 이미지를 1장 이상 선택하세요.");
  const queryInstanceIds = await resolveRepresentativeInstanceIds(imageIds);
  if (!queryInstanceIds.length) throw new Error("선택된 이미지에서 query instance를 찾지 못했습니다.");
  const data = await api("/classify/similar", {
    method: "POST",
    body: JSON.stringify({
      date,
      tab: currentTabForApi(),
      pet_id: state.galleryView === "PET" ? state.activePetId : null,
      include_seed: false,
      query_instance_ids: queryInstanceIds,
      merge: "MAX",
      top_k_images: Number(value("similarTopK") || 80),
      per_query_limit: 400,
    }),
  });
  state.searchScores = Object.fromEntries((data.results || []).map((item) => [item.image_id, Number(item.score)]));
  const selectedOrder = new Map(imageIds.map((imageId, idx) => [imageId, idx]));
  const order = new Map((data.results || []).map((item, idx) => [item.image_id, idx]));
  state.originalGalleryItems = [...state.originalGalleryItems].sort((a, b) => {
    const aSelected = selectedOrder.has(a.image_id);
    const bSelected = selectedOrder.has(b.image_id);
    if (aSelected && bSelected) {
      return selectedOrder.get(a.image_id) - selectedOrder.get(b.image_id);
    }
    if (aSelected) return -1;
    if (bSelected) return 1;

    const ai = order.has(a.image_id) ? order.get(a.image_id) : Number.MAX_SAFE_INTEGER;
    const bi = order.has(b.image_id) ? order.get(b.image_id) : Number.MAX_SAFE_INTEGER;
    if (ai !== bi) return ai - bi;
    return 0;
  });
  state.galleryPage = 1;
  applyGalleryPageSlice();
  renderGallery();
  log("Similar ranking applied", data.query_debug);
}

async function labelSelectedImages(action) {
  const date = currentDate();
  const imageIds = selectedImageIds();
  if (!date) throw new Error("date가 필요합니다.");
  if (!imageIds.length) throw new Error("먼저 daily 이미지를 선택하세요.");
  if (action === "ACCEPT" && !state.activePetId) throw new Error("포함 작업에는 active pet 선택이 필요합니다.");
  const data = await api("/admin/images/labels", {
    method: "POST",
    body: JSON.stringify({
      date,
      image_ids: imageIds,
      action,
      pet_id: state.activePetId,
      labeled_by: "admin_dashboard",
      confidence: 1.0,
      source: "MANUAL",
      select_mode: "BEST_CONFIDENCE",
    }),
  });
  log("Applied image label action", { action, count: data.items.length, pet_id: state.activePetId });
  await loadGallery();
}

async function quickUploadExemplar() {
  const fileInput = el("uFile");
  if (!fileInput.files || !fileInput.files.length) throw new Error("seed 이미지 파일을 선택하세요.");
  const progressId = "quickUploadProgress";
  const file = fileInput.files[0];
  const tracker = createUploadProgressTracker(progressId, { total: 1, metaPrefix: file.name });
  const fd = new FormData();
  if (state.singleSeedMode === "append") {
    const petId = value("uExistingPet").trim();
    if (!petId) throw new Error("추가할 기존 pet을 선택하세요.");
    fd.append("pet_id", petId);
  } else {
    const petName = value("uPetName").trim();
    if (!petName) throw new Error("새 pet name을 입력하세요.");
    fd.append("pet_name", petName);
  }
  fd.append("updated_by", "admin_dashboard");
  fd.append("sync_label", "true");
  fd.append("apply_to_all_instances", "false");
  fd.append("file", file);
  tracker.markRequestStarted();
  try {
    const data = await api("/exemplars/upload", { method: "POST", body: fd });
    tracker.markRequestResolved();
    log("Quick seed upload done", data);
    if (data?.status_url) {
      tracker.recordQueued(data.status_url);
      startGalleryAutoRefreshWindow();
    } else {
      tracker.recordImmediateSuccess();
    }
    tracker.markUploadsFinished();
    await tracker.waitForCompletion();
    clearFileInput("uFile");
    await loadWorkspace();
    resetUploadProgress(progressId, "선택한 seed 파일을 등록합니다.");
  } catch (err) {
    tracker.markRequestResolved();
    tracker.recordImmediateFailure();
    tracker.markUploadsFinished();
    setUploadProgress(progressId, {
      title: "업로드 실패",
      meta: err.message || String(err),
      detail: "요청 중 0 | 대기 중 0 | 처리 중 0 | 실패 1",
      value: 0,
      max: 1,
      active: true,
    });
    throw new Error(explainQuickUploadError(err));
  }
}

async function folderUploadExemplars() {
  const folderInput = el("fFolder");
  const uploadButton = el("btnFolderUpload");
  const originalLabel = uploadButton?.textContent || "폴더 일괄 등록";
  if (!folderInput.files || !folderInput.files.length) throw new Error("pet 폴더를 선택하세요.");
  const progressId = "folderUploadProgress";
  const files = Array.from(folderInput.files);
  const uniqueFolderCount = new Set(files.map((file) => folderGroupKey(file))).size;
  const batches = buildFolderUploadBatches(files, 24);
  let succeeded = 0;
  let failed = 0;
  let completedFolders = 0;
  const results = [];
  const tracker = createUploadProgressTracker(progressId, {
    total: files.length,
    metaPrefix: `${files.length}개 파일`,
  });

  try {
    for (let index = 0; index < batches.length; index += 1) {
      const batch = batches[index];
      const batchFiles = batch.files || [];
      const folderCount = Array.isArray(batch.folderKeys) ? batch.folderKeys.length : 0;
      if (uploadButton) {
        const nextCompleted = Math.min(uniqueFolderCount, completedFolders + folderCount);
        uploadButton.textContent = `폴더 ${nextCompleted}/${uniqueFolderCount}`;
      }
      const fd = new FormData();
      fd.append("updated_by", "admin_dashboard");
      fd.append("sync_label", "true");
      fd.append("apply_to_all_instances", "false");
      fd.append("skip_on_error", "true");
      fd.append("existing_name_policy", state.folderSeedPolicy || "append");
      batchFiles.forEach((file) => {
        fd.append("files", file);
        fd.append("relative_paths", file.webkitRelativePath || file.name);
      });
      tracker.markRequestStarted(batchFiles.length);
      try {
        const data = await api("/exemplars/upload-folder", { method: "POST", body: fd });
        tracker.markRequestResolved(batchFiles.length);
        succeeded += Number(data.succeeded || 0);
        failed += Number(data.failed || 0);
        completedFolders += folderCount;
        if (Array.isArray(data.results)) results.push(...data.results);
        if (Array.isArray(data.results)) {
          let immediateSuccesses = 0;
          let immediateFailures = 0;
          data.results.forEach((item) => {
            const itemStatus = String(item?.status || "").toLowerCase();
            if (itemStatus === "queued" && item.status_url) {
              tracker.recordQueued(item.status_url);
              startGalleryAutoRefreshWindow();
              return;
            }
            if (itemStatus === "ok") {
              immediateSuccesses += 1;
              return;
            }
            if (itemStatus === "failed") {
              immediateFailures += 1;
            }
          });
          if (immediateSuccesses > 0) tracker.recordImmediateSuccess(immediateSuccesses);
          if (immediateFailures > 0) tracker.recordImmediateFailure(immediateFailures);
        } else {
          const immediateSuccesses = Number(data.succeeded || 0);
          const immediateFailures = Number(data.failed || 0);
          if (immediateSuccesses > 0) tracker.recordImmediateSuccess(immediateSuccesses);
          if (immediateFailures > 0) tracker.recordImmediateFailure(immediateFailures);
        }
        log("Seed folder upload batch done", {
          batch: index + 1,
          batch_count: batches.length,
          folder_count: folderCount,
          completed_folders: completedFolders,
          total_folders: uniqueFolderCount,
          file_count: batchFiles.length,
          succeeded: data.succeeded,
          failed: data.failed,
          existing_name_policy: state.folderSeedPolicy,
        });
      } catch (err) {
        tracker.markRequestResolved(batchFiles.length);
        tracker.recordImmediateFailure(batchFiles.length);
        throw new Error(explainFolderUploadError(err, {
          batchIndex: index + 1,
          batchCount: batches.length,
          fileCount: batchFiles.length,
        }));
      }
    }

    log("Seed folder upload done", {
      total_files: files.length,
      total_folders: uniqueFolderCount,
      batch_count: batches.length,
      succeeded,
      failed,
      existing_name_policy: state.folderSeedPolicy,
      results_count: results.length,
    });
    tracker.markUploadsFinished();
    if (results.some((item) => String(item?.status || "").toLowerCase() === "queued" && item.status_url)) {
      startGalleryAutoRefreshWindow();
    }
    await tracker.waitForCompletion();
    clearFileInput("fFolder");
    await loadWorkspace();
    resetUploadProgress(progressId, "폴더 내부 파일을 순차 등록합니다.");
  } finally {
    if (uploadButton) uploadButton.textContent = originalLabel;
  }
}

async function dailyUploadImages() {
  const input = el("dFiles");
  if (!input.files || !input.files.length) throw new Error("daily 이미지 파일을 선택하세요.");
  const progressId = "dailyUploadProgress";
  const files = Array.from(input.files);
  const capturedAt = workspaceCapturedAt();
  let success = 0;
  let failed = 0;
  const tracker = createUploadProgressTracker(progressId, {
    total: files.length,
    metaPrefix: `${files.length}개 파일`,
  });
  for (const file of files) {
    const fd = new FormData();
    fd.append("trainer_id", "admin_dashboard");
    fd.append("image_role", "DAILY");
    if (capturedAt) fd.append("captured_at", capturedAt);
    fd.append("file", file);
    tracker.markRequestStarted();
    try {
      const data = await api("/ingest", { method: "POST", body: fd });
      tracker.markRequestResolved();
      if (data?.status_url) {
        tracker.recordQueued(data.status_url);
        startGalleryAutoRefreshWindow();
      } else {
        tracker.recordImmediateSuccess();
      }
      success += 1;
    } catch (err) {
      tracker.markRequestResolved();
      tracker.recordImmediateFailure();
      failed += 1;
      log("Daily upload failed for file", { file: file.name, error: err.message });
    }
  }
  log("Daily upload done", { succeeded: success, failed, captured_at: capturedAt || null });
  tracker.markUploadsFinished();
  if (success > 0) {
    await tracker.waitForCompletion();
    if (failed === 0) {
      clearFileInput("dFiles");
    }
  }
  if (state.galleryView === "PET") {
    state.galleryView = "ALL";
    syncViewButtons();
  }
  await loadGallery();
  resetUploadProgress(progressId, "선택한 daily 파일을 순차 등록합니다.");
}

function closeHelpPopovers(exceptId = "") {
  document.querySelectorAll(".help-trigger").forEach((button) => {
    const targetId = button.dataset.helpTarget || "";
    const target = targetId ? el(targetId) : null;
    const isActive = targetId && targetId === exceptId;
    button.setAttribute("aria-expanded", isActive ? "true" : "false");
    if (target) target.hidden = !isActive;
  });
}

function bindHelpPopovers() {
  document.querySelectorAll(".help-trigger").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const targetId = button.dataset.helpTarget || "";
      const target = targetId ? el(targetId) : null;
      if (!target) return;
      const nextOpen = target.hidden;
      closeHelpPopovers(nextOpen ? targetId : "");
    });
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(".help-anchor")) return;
    closeHelpPopovers();
    if (!event.target.closest("#dailyDownloadMenu") && !event.target.closest("#btnDownloadDailyZip")) {
      closeDailyDownloadMenu();
    }
    if (!event.target.closest("#workspaceDatePicker")) {
      closeWorkspaceCalendar();
    }
  });
}

function bindViewButtons() {
  ["btnViewAll", "btnViewUnclassified", "btnViewPet"].forEach((id) => {
    el(id).addEventListener("click", async () => {
      const nextView = el(id).dataset.view;
      if (nextView === "PET" && !state.activePetId) {
        await loadPets();
        if (!state.activePetId) {
          alert("등록된 pet이 없습니다.");
          return;
        }
      }
      state.galleryView = nextView;
      resetSearchRanking();
      syncViewButtons();
      await loadGallery();
    });
  });
}

function bootstrapDefaults() {
  el("apiBase").value = `${window.location.origin}/v1`;
  const today = new Date().toISOString().slice(0, 10);
  el("workspaceDate").value = today;
  state.calendarMonth = monthKeyFromDate(today);
  closeWorkspaceCalendar();
  renderWorkspaceDateDisplay();
  const zipButton = el("btnDownloadZip");
  if (zipButton) {
    zipButton.disabled = false;
    zipButton.type = "button";
  }
  renderSingleSeedMode();
}

function bindEvents() {
  el("btnWorkspaceDateToggle")?.addEventListener("click", async (event) => {
    event.stopPropagation();
    const isOpening = !isWorkspaceCalendarOpen();
    if (isOpening) {
      const monthKey = state.calendarMonth || monthKeyFromDate(currentDate()) || monthKeyFromDate(new Date().toISOString().slice(0, 10));
      state.calendarMonth = monthKey;
      await loadCalendarMonth(monthKey);
    }
    toggleWorkspaceCalendar();
  });

  el("workspaceDate").addEventListener("change", async () => {
    resetBucketExportState();
    const monthKey = monthKeyFromDate(currentDate());
    if (monthKey) {
      state.calendarMonth = monthKey;
      await loadCalendarMonth(monthKey);
    } else {
      renderWorkspaceCalendar();
    }
  });

  el("btnCalendarPrevMonth")?.addEventListener("click", async () => {
    state.calendarMonth = shiftMonthKey(state.calendarMonth || monthKeyFromDate(currentDate()), -1);
    await loadCalendarMonth(state.calendarMonth);
  });

  el("btnCalendarNextMonth")?.addEventListener("click", async () => {
    state.calendarMonth = shiftMonthKey(state.calendarMonth || monthKeyFromDate(currentDate()), 1);
    await loadCalendarMonth(state.calendarMonth);
  });

  el("btnLoadWorkspace").addEventListener("click", async () => {
    const button = el("btnLoadWorkspace");
    try {
      await withButtonBusy(button, () => loadWorkspace());
    } catch (err) {
      alert(err.message);
      log("Load workspace failed", { error: err.message });
    }
  });

  el("btnQdrantStatus").addEventListener("click", async () => {
    const button = el("btnQdrantStatus");
    try {
      await withButtonBusy(button, () => loadQdrantStatus());
    } catch (err) {
      alert(err.message);
      log("Qdrant status failed", { error: err.message });
    }
  });

  el("btnAutoClassify").addEventListener("click", async () => {
    const button = el("btnAutoClassify");
    try {
      await withButtonBusy(button, () => autoClassify());
    } catch (err) {
      alert(err.message);
      log("Auto classify failed", { error: err.message });
    }
  });

  el("btnFinalize").addEventListener("click", async () => {
    const button = el("btnFinalize");
    try {
      await withButtonBusy(button, () => finalizeBuckets());
    } catch (err) {
      alert(err.message);
      log("Finalize failed", { error: err.message });
    }
  });

  el("btnReloadGallery").addEventListener("click", async () => {
    const button = el("btnReloadGallery");
    try {
      await withButtonBusy(button, async () => {
        resetSearchRanking();
        await loadGallery();
      });
    } catch (err) {
      alert(err.message);
      log("Reload gallery failed", { error: err.message });
    }
  });

  el("btnSimilarSearch").addEventListener("click", async () => {
    const button = el("btnSimilarSearch");
    try {
      await withButtonBusy(button, () => runSimilarSearch());
    } catch (err) {
      alert(err.message);
      log("Similar search failed", { error: err.message });
    }
  });

  el("btnClearRanking").addEventListener("click", () => {
    resetSearchRanking();
    renderGallery();
    log("Ranking reset");
  });

  el("btnSelectAll").addEventListener("click", () => {
    selectAllVisibleImages();
    log("Selected all visible images", { count: state.galleryItems.length });
  });

  el("btnClearSelection").addEventListener("click", () => {
    clearSelectedImages();
    log("Cleared selected images");
  });

  el("galleryStateFilter")?.addEventListener("change", () => {
    state.galleryFilters.state = value("galleryStateFilter") || "ALL";
    state.galleryPage = 1;
    applyGalleryPageSlice();
    renderGallery();
  });

  el("galleryMultiplicityFilter")?.addEventListener("change", () => {
    state.galleryFilters.multiplicity = value("galleryMultiplicityFilter") || "ALL";
    state.galleryPage = 1;
    applyGalleryPageSlice();
    renderGallery();
  });

  const applyPetFilter = () => {
    state.galleryFilters.petId = value("galleryPetFilter") || "";
    state.galleryPage = 1;
    applyGalleryPageSlice();
    renderGallery();
  };

  el("galleryPetFilter")?.addEventListener("change", applyPetFilter);
  el("galleryPetFilter")?.addEventListener("input", applyPetFilter);

  el("btnResetGalleryFilters")?.addEventListener("click", () => {
    state.galleryFilters.state = "ALL";
    state.galleryFilters.multiplicity = "ALL";
    state.galleryFilters.petId = "";
    state.galleryPage = 1;
    applyGalleryPageSlice();
    renderGallery();
  });

  el("btnAssignSelected").addEventListener("click", async () => {
    const button = el("btnAssignSelected");
    try {
      await withButtonBusy(button, () => labelSelectedImages("ACCEPT"));
    } catch (err) {
      alert(err.message);
      log("Assign selected failed", { error: err.message });
    }
  });

  el("btnClearSelected").addEventListener("click", async () => {
    const button = el("btnClearSelected");
    try {
      await withButtonBusy(button, () => labelSelectedImages("CLEAR"));
    } catch (err) {
      alert(err.message);
      log("Reset to unassigned failed", { error: err.message });
    }
  });

  el("btnSingleSeedAppendMode").addEventListener("click", () => setSingleSeedMode("append"));
  el("btnSingleSeedCreateMode").addEventListener("click", () => setSingleSeedMode("create"));
  el("btnFolderPolicyAppend")?.addEventListener("click", () => setFolderSeedPolicy("append"));
  el("btnFolderPolicyCreate")?.addEventListener("click", () => setFolderSeedPolicy("create_new"));
  el("btnFolderPolicyFail")?.addEventListener("click", () => setFolderSeedPolicy("fail"));

  el("btnQuickUpload").addEventListener("click", async () => {
    const button = el("btnQuickUpload");
    try {
      await withButtonBusy(button, () => quickUploadExemplar());
    } catch (err) {
      alert(err.message);
      log("Quick upload failed", { error: err.message });
    }
  });

  el("btnDownloadExemplarsZip")?.addEventListener("click", () => {
    window.handleDownloadExemplarsZip();
  });

  el("btnDownloadDailyZip")?.addEventListener("click", (event) => {
    event.stopPropagation();
    window.handleDownloadDailyZip();
  });

  el("btnDownloadDailyAll")?.addEventListener("click", (event) => {
    event.stopPropagation();
    window.handleDownloadDailyZipMode("all");
  });

  el("btnDownloadDailyAccepted")?.addEventListener("click", (event) => {
    event.stopPropagation();
    window.handleDownloadDailyZipMode("accepted_only");
  });

  el("btnFolderUpload").addEventListener("click", async () => {
    const button = el("btnFolderUpload");
    try {
      await withButtonBusy(button, () => folderUploadExemplars());
    } catch (err) {
      alert(err.message);
      log("Folder upload failed", { error: err.message });
    }
  });

  el("btnDailyUpload").addEventListener("click", async () => {
    const button = el("btnDailyUpload");
    try {
      await withButtonBusy(button, () => dailyUploadImages());
    } catch (err) {
      alert(err.message);
      log("Daily upload failed", { error: err.message });
    }
  });

  el("seedContextMoveDaily")?.addEventListener("click", () => {
    toggleSeedMoveOptions();
  });

  el("seedContextMoveUnclassified")?.addEventListener("click", async () => {
    try {
      await moveSeedToDaily("UNCLASSIFIED");
    } catch (err) {
      alert(err.message);
      log("Move seed to daily unclassified failed", { error: err.message });
    }
  });

  el("seedContextMoveAccepted")?.addEventListener("click", async () => {
    try {
      await moveSeedToDaily("ACCEPTED");
    } catch (err) {
      alert(err.message);
      log("Move seed to daily accepted failed", { error: err.message });
    }
  });

  const deleteButton = el("seedContextDelete");
  if (deleteButton) deleteButton.classList.add("danger");
  deleteButton?.addEventListener("click", async () => {
    try {
      await deleteSeedExemplar();
    } catch (err) {
      alert(err.message);
      log("Delete seed exemplar failed", { error: err.message });
    }
  });

  el("seedContextClose")?.addEventListener("click", () => {
    closeSeedContextMenu();
  });

  el("btnCloseInspector")?.addEventListener("click", () => {
    closeInspectorDrawer();
  });

  el("inspectorBackdrop")?.addEventListener("click", () => {
    closeInspectorDrawer();
  });

  el("inspectorDrawer")?.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".inspector-drawer-panel")) return;
    closeInspectorDrawer();
  });

  document.addEventListener("pointerdown", (event) => {
    if (event.target.closest("#seedContextMenu")) return;
    closeSeedContextMenu();
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("#seedContextMenu")) return;
    closeSeedContextMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSeedContextMenu();
      closeInspectorDrawer();
    }
  });

  window.addEventListener("scroll", closeSeedContextMenu, true);
  bindViewButtons();
  bindHelpPopovers();
}

async function init() {
  bootstrapDefaults();
  bindEvents();
  renderInspector(null);
  renderBucketSummary();
  renderPetButtons();
  renderSingleSeedMode();
  renderFolderSeedPolicy();
  renderActiveExemplars();
  renderGallery();
  syncViewButtons();
  log("Admin workspace ready");
  try {
    await Promise.all([
      loadQdrantStatus(),
      loadCalendarMonth(state.calendarMonth || monthKeyFromDate(currentDate() || new Date().toISOString().slice(0, 10))),
    ]);
  } catch (err) {
    log("Initial bootstrap skipped", { error: err.message });
  }
}

init();
