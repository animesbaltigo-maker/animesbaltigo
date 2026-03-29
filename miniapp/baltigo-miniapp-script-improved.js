const tg = window.Telegram?.WebApp || null;
if (tg) {
  try {
    tg.ready();
    tg.expand();
    tg.setHeaderColor?.("#05060f");
    tg.setBackgroundColor?.("#05060f");
  } catch (e) {
    console.warn("Telegram WebApp init:", e);
  }
}

const IS_TELEGRAM_WEBVIEW = !!window.Telegram?.WebApp;
const API_BASE = window.location.origin;
const STALL_TIMEOUT_MS = 14000;
const LINK_FETCH_TIMEOUT = 10000;
const MAX_ATTEMPTS = 4;
const API_TIMEOUT = 25000;
const WATCH_SAVE_MIN_SECONDS = 15;
const WATCH_SAVE_THROTTLE_MS = 5000;
const AUTO_NEXT_DELAY_MS = 3000;
const WATCH_KEY = "baltigo.watch.progress.v3";
const ROUTE_KEY = "baltigo.route.v3";

function storageGet(key, fallbackValue) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallbackValue;
  } catch (e) {
    return fallbackValue;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {}
}

function normalizeStoredWatchMap(rawMap) {
  const normalized = {};
  for (const [animeId, rawEntry] of Object.entries(rawMap || {})) {
    if (!rawEntry || typeof rawEntry !== "object") continue;
    const aid = String(rawEntry.animeId || animeId || "").trim();
    if (!aid) continue;

    if (rawEntry.episodes && typeof rawEntry.episodes === "object" && !Array.isArray(rawEntry.episodes)) {
      const episodes = {};
      for (const [episodeKey, epEntry] of Object.entries(rawEntry.episodes || {})) {
        const ep = String(epEntry?.episode || episodeKey || "").trim();
        if (!ep) continue;
        episodes[ep] = {
          episode: ep,
          watchedSeconds: Math.max(0, Number(epEntry?.watchedSeconds || 0)),
          durationSeconds: Math.max(0, Number(epEntry?.durationSeconds || 0)),
          completed: Boolean(epEntry?.completed),
          updatedAt: Number(epEntry?.updatedAt || rawEntry.updatedAt || Date.now())
        };
      }
      normalized[aid] = {
        animeId: aid,
        animeTitle: rawEntry.animeTitle || "Anime",
        cover: rawEntry.cover || "",
        latestEpisode: String(rawEntry.latestEpisode || Object.keys(episodes).sort((a, b) => Number(b) - Number(a))[0] || "").trim(),
        updatedAt: Number(rawEntry.updatedAt || Date.now()),
        episodes
      };
      continue;
    }

    const ep = String(rawEntry.episode || rawEntry.latestEpisode || "").trim();
    const watchedSeconds = Math.max(0, Number(rawEntry.watchedSeconds || 0));
    const durationSeconds = Math.max(0, Number(rawEntry.durationSeconds || 0));
    normalized[aid] = {
      animeId: aid,
      animeTitle: rawEntry.animeTitle || "Anime",
      cover: rawEntry.cover || "",
      latestEpisode: ep,
      updatedAt: Number(rawEntry.updatedAt || Date.now()),
      episodes: ep ? {
        [ep]: {
          episode: ep,
          watchedSeconds,
          durationSeconds,
          completed: Boolean(rawEntry.completed || (durationSeconds > 0 && watchedSeconds / durationSeconds >= 0.92)),
          updatedAt: Number(rawEntry.updatedAt || Date.now())
        }
      } : {}
    };
  }
  return normalized;
}

function getWatchMap() {
  return normalizeStoredWatchMap(storageGet(WATCH_KEY, {}));
}

function setWatchMap(map) {
  storageSet(WATCH_KEY, map);
}

function getStoredRoute() {
  return storageGet(ROUTE_KEY, null);
}

function setStoredRoute(route) {
  storageSet(ROUTE_KEY, route);
}

function saveWatchProgress(progress) {
  const animeId = String(progress?.animeId || "").trim();
  const episode = String(progress?.episode || "").trim();
  if (!animeId || !episode) return;

  const watchMap = getWatchMap();
  const now = Date.now();
  const watchedSeconds = Math.max(0, Number(progress?.watchedSeconds || 0));
  const durationSeconds = Math.max(0, Number(progress?.durationSeconds || 0));
  const completed = Boolean(progress?.completed || (durationSeconds > 0 && watchedSeconds / durationSeconds >= 0.92));

  if (!watchMap[animeId]) {
    watchMap[animeId] = {
      animeId,
      animeTitle: progress?.animeTitle || "Anime",
      cover: progress?.cover || "",
      latestEpisode: episode,
      updatedAt: now,
      episodes: {}
    };
  }

  const animeEntry = watchMap[animeId];
  animeEntry.animeTitle = progress?.animeTitle || animeEntry.animeTitle || "Anime";
  animeEntry.cover = progress?.cover || animeEntry.cover || "";
  animeEntry.latestEpisode = episode;
  animeEntry.updatedAt = now;
  animeEntry.episodes[episode] = {
    episode,
    watchedSeconds,
    durationSeconds,
    completed,
    updatedAt: now
  };

  setWatchMap(watchMap);
}

function getContinueWatching() {
  return Object.values(getWatchMap())
    .filter(item => {
      const latest = String(item?.latestEpisode || "").trim();
      const episodeEntry = latest ? item?.episodes?.[latest] : null;
      return Boolean(episodeEntry && episodeEntry.watchedSeconds >= WATCH_SAVE_MIN_SECONDS && !episodeEntry.completed);
    })
    .sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0));
}

function getAnimeWatch(animeId) {
  return getWatchMap()[String(animeId || "")] || null;
}

function getEpisodeWatch(animeId, episode) {
  return getWatchMap()?.[String(animeId || "")]?.episodes?.[String(episode || "")] || null;
}

const state = {
  home: null,
  currentPage: "home",
  searchQuery: "",
  searchResults: [],
  anime: null,
  animeEpisodes: [],
  currentAnimeId: "",
  currentEpisode: "",
  currentQuality: "HD",
  currentVideoUrl: "",
  currentEpisodeItem: null,
  historyStack: [],
  autoBootDone: false,
  openingEpisode: false,
  openEpisodeRequestId: 0,
  playerSessionId: 0,
  attempt: 0,
  stallTimer: null,
  videoReady: false,
  resultsMode: "section",
  currentSection: "",
  resultsPage: 1,
  totalPages: 1,
  heroBanners: [],
  heroIdx: 0,
  heroTimer: null,
  heroCurrent: null,
  genreExpanded: false,
  eventSource: null,
  cssFsActive: false,
  nativeFsActive: false,
  cinemaModeActive: false,
  requestSeq: { home: 0, section: 0, search: 0, anime: 0 },
  aborters: { home: null, section: null, search: null, anime: null, episode: null },
  nextEpisodeTimer: null,
  nextEpisodeCancelClick: null,
  lastProgressKey: "",
  lastProgressSaveAt: 0,
  activeLoadingToken: 0,
  tgBackInitialized: false
};

const $ = id => document.getElementById(id);
const els = {
  topbar: $("topbar"),
  headerSubtitle: $("headerSubtitle"),
  loadingOverlay: $("loadingOverlay"),
  loadingTitle: $("loadingTitle"),
  loadingText: $("loadingText"),
  toast: $("toast"),
  backBtn: $("backBtn"),
  refreshBtn: $("refreshBtn"),
  homeBtn: $("homeBtn"),
  homePage: $("homePage"),
  resultsPage: $("resultsPage"),
  animePage: $("animePage"),
  playerPage: $("playerPage"),
  heroArea: $("heroArea"),
  continueSection: $("continueSection"),
  searchInput: $("searchInput"),
  searchClear: $("searchClear"),
  homeSections: $("homeSections"),
  resultsTitle: $("resultsTitle"),
  resultsSubtitle: $("resultsSubtitle"),
  resultsGrid: $("resultsGrid"),
  resultsPagination: $("resultsPagination"),
  resultsBackBtn: $("resultsBackBtn"),
  animeCover: $("animeCover"),
  animePrefix: $("animePrefix"),
  animeTitle: $("animeTitle"),
  animeMeta: $("animeMeta"),
  animeDesc: $("animeDesc"),
  animeGenres: $("animeGenres"),
  descToggle: $("descToggle"),
  openFirstEpisodeBtn: $("openFirstEpisodeBtn"),
  goToPlayerBtn: $("goToPlayerBtn"),
  backToHomeFromAnimeBtn: $("backToHomeFromAnimeBtn"),
  episodesInfo: $("episodesInfo"),
  episodesGrid: $("episodesGrid"),
  playerPageSubtitle: $("playerPageSubtitle"),
  playerTopBar: $("playerTopBar"),
  videoContainer: $("videoContainer"),
  videoPlayer: $("videoPlayer"),
  iframePlayer: $("iframePlayer"),
  videoStatusOverlay: $("videoStatusOverlay"),
  videoStatusIconSlot: $("videoStatusIconSlot"),
  videoStatusText: $("videoStatusText"),
  videoStatusSub: $("videoStatusSub"),
  playerTitle: $("playerTitle"),
  playerMeta: $("playerMeta"),
  qualityRow: $("qualityRow"),
  prevEpisodeBtn: $("prevEpisodeBtn"),
  nextEpisodeBtn: $("nextEpisodeBtn"),
  episodeListBtn: $("episodeListBtn"),
  backToAnimeBtn: $("backToAnimeBtn"),
  backToHomeFromPlayerBtn: $("backToHomeFromPlayerBtn"),
  openInBrowserBtn: $("openInBrowserBtn"),
  retryPlayerBtn: $("retryPlayerBtn"),
  fullscreenBtn: $("fullscreenBtn"),
  cssFsExit: $("cssFsExit"),
  playerDesc: $("playerDesc"),
  playerNote: $("playerNote")
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalizeQuality(value) {
  const quality = String(value || "HD").trim().toUpperCase();
  if (["FULLHD", "FHD", "1080P", "HD", "720P"].includes(quality)) return "HD";
  if (["SD", "480P", "360P"].includes(quality)) return "SD";
  return quality || "HD";
}

function oppositeQuality(quality) {
  return normalizeQuality(quality) === "HD" ? "SD" : "HD";
}

function imgSrc(src, title) {
  if (src && String(src).startsWith("http")) return src;
  return `https://placehold.co/600x900/0d1128/7c3aed?text=${encodeURIComponent(title || "Anime")}`;
}

function formatSeconds(totalSeconds) {
  const safe = Math.max(0, Math.floor(Number(totalSeconds || 0)));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const seconds = safe % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

let toastTimer = null;
function showToast(message) {
  if (!message) return;
  clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.classList.add("show");
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 3200);
}

function showLoading(title = "Carregando...", text = "Aguarde.") {
  const token = ++state.activeLoadingToken;
  els.loadingTitle.textContent = title;
  els.loadingText.textContent = text;
  els.loadingOverlay.classList.add("active");
  return token;
}

function hideLoading(token) {
  if (token && token !== state.activeLoadingToken) return;
  els.loadingOverlay.classList.remove("active");
}

function setSubtitle(text) {
  els.headerSubtitle.textContent = text || "BALTIGO";
}

function saveRoute(payload) {
  setStoredRoute(payload || null);
}

function parseRoute() {
  const params = new URLSearchParams(window.location.search);
  return {
    anime: params.get("anime") || "",
    ep: params.get("ep") || params.get("episode") || "",
    q: normalizeQuality(params.get("q") || "HD"),
    search: params.get("search") || "",
    section: params.get("section") || "",
    page: parseInt(params.get("page") || "1", 10) || 1
  };
}

function updateUrlParams({ anime, ep, q, search, section, page } = {}, options = {}) {
  const mode = options.mode === "push" ? "push" : "replace";
  const url = new URL(window.location.href);
  ["anime", "ep", "q", "search", "section", "page"].forEach(key => url.searchParams.delete(key));

  const normalizedRoute = {
    anime: anime ? String(anime) : "",
    ep: ep ? String(ep) : "",
    q: q ? normalizeQuality(q) : "",
    search: search ? String(search) : "",
    section: section ? String(section) : "",
    page: Number(page || 1) || 1
  };

  if (normalizedRoute.anime) url.searchParams.set("anime", normalizedRoute.anime);
  if (normalizedRoute.ep) url.searchParams.set("ep", normalizedRoute.ep);
  if (normalizedRoute.q) url.searchParams.set("q", normalizedRoute.q);
  if (normalizedRoute.search) url.searchParams.set("search", normalizedRoute.search);
  if (normalizedRoute.section) url.searchParams.set("section", normalizedRoute.section);
  if ((normalizedRoute.search || normalizedRoute.section) && normalizedRoute.page > 1) {
    url.searchParams.set("page", String(normalizedRoute.page));
  }

  const next = url.toString();
  if (mode === "push" && next !== window.location.href) history.pushState({}, "", next);
  else history.replaceState({}, "", next);
  saveRoute(normalizedRoute);
}

function syncTelegramBackButton() {
  if (!tg?.BackButton) return;
  if (!state.tgBackInitialized) {
    tg.BackButton.onClick(() => goBack());
    state.tgBackInitialized = true;
  }
  if (state.currentPage === "home") tg.BackButton.hide();
  else tg.BackButton.show();
}

function updateActiveSectionChip(sectionKey) {
  document.querySelectorAll("[data-section]").forEach(chip => {
    chip.classList.toggle("active", chip.dataset.section === sectionKey);
  });
}

function clearHeroTimer() {
  if (state.heroTimer) {
    clearTimeout(state.heroTimer);
    state.heroTimer = null;
  }
}

function clearNextEpisodeTimer() {
  if (state.nextEpisodeTimer) {
    clearTimeout(state.nextEpisodeTimer);
    state.nextEpisodeTimer = null;
  }
  if (state.nextEpisodeCancelClick) {
    document.removeEventListener("click", state.nextEpisodeCancelClick);
    state.nextEpisodeCancelClick = null;
  }
}

function clearStallTimer() {
  if (state.stallTimer) {
    clearTimeout(state.stallTimer);
    state.stallTimer = null;
  }
}

function showVideoOverlay(type, text, sub = "") {
  if (type === "loading") {
    els.videoStatusIconSlot.innerHTML = '<div class="spinner-ring"></div>';
  } else if (String(type || "").startsWith("icon:")) {
    els.videoStatusIconSlot.innerHTML = `<div class="overlay-icon">${esc(String(type).slice(5))}</div>`;
  } else {
    els.videoStatusIconSlot.innerHTML = "";
  }
  els.videoStatusText.textContent = text;
  els.videoStatusSub.textContent = sub;
  els.videoStatusOverlay.classList.add("active");
}

function hideVideoOverlay() {
  els.videoStatusOverlay.classList.remove("active");
}

function buildProxyUrl(rawUrl, bust = 0) {
  const proxyUrl = new URL(`${API_BASE}/api/proxy-stream`);
  proxyUrl.searchParams.set("url", rawUrl);
  if (bust) proxyUrl.searchParams.set("_t", String(bust));
  return proxyUrl.toString();
}

function abortNamedRequest(name) {
  try {
    state.aborters[name]?.abort?.();
  } catch (e) {}
  state.aborters[name] = null;
}

async function apiGet(path, ms = API_TIMEOUT, requestName = "") {
  if (requestName) abortNamedRequest(requestName);
  const ctrl = new AbortController();
  if (requestName) state.aborters[requestName] = ctrl;
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: ctrl.signal
    });
    if (!response.ok) {
      let detail = `Erro ${response.status}`;
      try {
        detail = (await response.json())?.detail || detail;
      } catch (e) {}
      throw new Error(detail);
    }
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("Tempo limite excedido. O servidor demorou para responder.");
    throw error;
  } finally {
    clearTimeout(timer);
    if (requestName && state.aborters[requestName] === ctrl) state.aborters[requestName] = null;
  }
}

function enterCinemaMode() {
  if (state.cinemaModeActive) return;
  state.cinemaModeActive = true;
  els.topbar.classList.add("cinema-mode");
  els.playerPage.classList.add("cinema-expand");
}

function exitCinemaMode() {
  if (!state.cinemaModeActive) return;
  state.cinemaModeActive = false;
  els.topbar.classList.remove("cinema-mode");
  els.playerPage.classList.remove("cinema-expand");
}

function resetPlayerNavButtons() {
  els.prevEpisodeBtn.disabled = true;
  els.nextEpisodeBtn.disabled = true;
  els.prevEpisodeBtn.dataset.episode = "";
  els.nextEpisodeBtn.dataset.episode = "";
}

function resetPlayerTransport({ clearContext = false } = {}) {
  clearNextEpisodeTimer();
  clearStallTimer();
  state.videoReady = false;
  state.attempt = 0;
  try {
    els.videoPlayer.pause();
  } catch (e) {}
  els.videoPlayer.removeAttribute("src");
  els.videoPlayer.load();
  els.videoPlayer.style.display = "block";
  els.videoPlayer.dataset.rawUrl = "";
  els.iframePlayer.onload = null;
  els.iframePlayer.onerror = null;
  els.iframePlayer.src = "about:blank";
  els.iframePlayer.style.display = "none";
  hideVideoOverlay();
  if (clearContext) {
    state.currentVideoUrl = "";
    state.currentEpisodeItem = null;
  }
}

function clearPlayerState() {
  state.playerSessionId += 1;
  abortNamedRequest("episode");
  resetPlayerTransport({ clearContext: true });
}

function resetEpisodeStateForAnimeSwitch() {
  clearPlayerState();
  state.currentEpisode = "";
  state.currentEpisodeItem = null;
  state.currentVideoUrl = "";
  state.currentQuality = "HD";
  els.openInBrowserBtn.disabled = true;
  els.openInBrowserBtn.dataset.url = "";
  resetPlayerNavButtons();
}

function setPage(page, push = true) {
  const prev = state.currentPage;
  const leavingPlayer = prev === "player" && page !== "player";
  if (leavingPlayer) {
    if (state.cssFsActive) exitCssFullscreen();
    clearPlayerState();
  }

  exitCinemaMode();
  ["homePage", "resultsPage", "animePage", "playerPage"].forEach(key => els[key].classList.add("hidden"));
  if (page === "home") els.homePage.classList.remove("hidden");
  if (page === "results") els.resultsPage.classList.remove("hidden");
  if (page === "anime") els.animePage.classList.remove("hidden");
  if (page === "player") {
    els.playerPage.classList.remove("hidden");
    setTimeout(() => {
      if (state.currentPage === "player" && window.innerWidth <= 768 && !state.cssFsActive) enterCinemaMode();
    }, 800);
  }

  state.currentPage = page;
  const titles = { home: "Anime Streaming", results: "Resultados", anime: state.anime?.title || "Anime", player: "Player" };
  setSubtitle(titles[page] || "BALTIGO");
  if (push && prev !== page) state.historyStack.push(prev);
  syncTelegramBackButton();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goBack() {
  if (state.cssFsActive) exitCssFullscreen();
  exitCinemaMode();
  const prev = state.historyStack.pop() || "home";
  if (prev === "player" && state.currentAnimeId && state.currentEpisode) {
    setPage("player", false);
    updateUrlParams({ anime: state.currentAnimeId, ep: state.currentEpisode, q: state.currentQuality }, { mode: "replace" });
    return;
  }
  if (prev === "anime" && state.currentAnimeId) {
    setPage("anime", false);
    updateUrlParams({ anime: state.currentAnimeId }, { mode: "replace" });
    return;
  }
  if (prev === "results") {
    setPage("results", false);
    if (state.resultsMode === "search" && state.searchQuery) updateUrlParams({ search: state.searchQuery, page: state.resultsPage }, { mode: "replace" });
    else if (state.currentSection) updateUrlParams({ section: state.currentSection, page: state.resultsPage }, { mode: "replace" });
    else updateUrlParams({}, { mode: "replace" });
    return;
  }
  setPage("home", false);
  updateUrlParams({}, { mode: "replace" });
  if (!state.home) loadHome();
}

function enterCssFullscreen() {
  state.cssFsActive = true;
  const isPortrait = window.screen.height > window.screen.width;
  els.videoContainer.classList.add("css-fs");
  if (isPortrait) els.videoContainer.classList.add("portrait-rotate");
  document.body.classList.add("in-css-fs");
  els.cssFsExit.style.display = "flex";
  window.scrollTo({ top: 0 });
  screen.orientation?.lock?.("landscape").catch(() => {});
  showToast("Toque ✕ para sair da tela cheia");
}

function exitCssFullscreen() {
  if (!state.cssFsActive) return;
  state.cssFsActive = false;
  els.videoContainer.classList.remove("css-fs", "portrait-rotate");
  document.body.classList.remove("in-css-fs");
  els.cssFsExit.style.display = "none";
  screen.orientation?.unlock?.();
}

async function requestFullscreen() {
  const video = els.videoPlayer;
  if (state.cssFsActive) {
    exitCssFullscreen();
    return;
  }

  const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
  if (fullscreenElement) {
    const exitFullscreen = document.exitFullscreen || document.webkitExitFullscreen;
    if (exitFullscreen) {
      try {
        const maybePromise = exitFullscreen.call(document);
        if (maybePromise?.catch) maybePromise.catch(() => {});
      } catch (e) {}
    }
    return;
  }

  const isVideoVisible = video.style.display !== "none";
  if (IS_TELEGRAM_WEBVIEW) {
    enterCssFullscreen();
    return;
  }

  if (isVideoVisible && video.requestFullscreen) {
    try {
      await video.requestFullscreen({ navigationUI: "hide" });
      state.nativeFsActive = true;
      screen.orientation?.lock?.("landscape").catch(() => {});
      return;
    } catch (e) {}
  }

  if (isVideoVisible && video.webkitEnterFullscreen) {
    try {
      video.webkitEnterFullscreen();
      state.nativeFsActive = true;
      return;
    } catch (e) {}
  }

  const fullscreenMethod = els.videoContainer.requestFullscreen || els.videoContainer.webkitRequestFullscreen || els.videoContainer.mozRequestFullScreen;
  if (fullscreenMethod) {
    try {
      await fullscreenMethod.call(els.videoContainer, { navigationUI: "hide" });
      state.nativeFsActive = true;
      screen.orientation?.lock?.("landscape").catch(() => {});
      return;
    } catch (e) {}
  }

  enterCssFullscreen();
}

function _onNativeFsChange() {
  if (!state.nativeFsActive) return;
  if (!(document.fullscreenElement || document.webkitFullscreenElement)) state.nativeFsActive = false;
}

function startStallTimer(sessionId = state.playerSessionId) {
  clearStallTimer();
  state.stallTimer = setTimeout(() => {
    if (sessionId !== state.playerSessionId) return;
    _escalatePlayerFailure(sessionId);
  }, STALL_TIMEOUT_MS);
}

function _resetVideoElement() {
  clearStallTimer();
  state.videoReady = false;
  try {
    els.videoPlayer.pause();
  } catch (e) {}
  els.videoPlayer.removeAttribute("src");
  els.videoPlayer.load();
  els.videoPlayer.style.display = "block";
  els.videoPlayer.dataset.rawUrl = "";
  els.iframePlayer.onload = null;
  els.iframePlayer.onerror = null;
  els.iframePlayer.src = "about:blank";
  els.iframePlayer.style.display = "none";
}

function _isDirectVideo(url) {
  const lower = String(url || "").toLowerCase();
  return lower.includes(".m3u8") || lower.includes(".mp4") || lower.includes(".webm") || lower.includes("videoplayback") || lower.includes("googlevideo");
}

function isIframeLikeUrl(url) {
  const lower = String(url || "").toLowerCase();
  return /blogger\.com\/video\.g|youtube\.com\/embed|player|embed|iframe/.test(lower);
}

function pickEpisodeVideoUrl(item) {
  if (!item || typeof item !== "object") return "";
  const directCandidates = [
    item.stream_url,
    item.stream,
    item.video_url,
    item.video,
    item.file,
    item.src,
    item.m3u8,
    item.mp4
  ].filter(Boolean).map(value => String(value).trim()).filter(Boolean);
  const embedCandidates = [
    item.video_iframe,
    item.iframe,
    item.iframe_url,
    item.embed,
    item.embed_url,
    item.player,
    item.player_url
  ].filter(Boolean).map(value => String(value).trim()).filter(Boolean);

  const preferredDirect = directCandidates.find(_isDirectVideo) || directCandidates.find(value => !isIframeLikeUrl(value));
  if (preferredDirect) return preferredDirect;
  const preferredEmbed = embedCandidates.find(isIframeLikeUrl);
  return preferredEmbed || directCandidates[0] || embedCandidates[0] || "";
}

function _loadVideoUrl(rawUrl, sessionId = state.playerSessionId) {
  if (sessionId !== state.playerSessionId) return;
  state.currentVideoUrl = rawUrl;
  state.videoReady = false;
  _resetVideoElement();
  showVideoOverlay("loading", "Carregando vídeo...", "Conectando ao servidor");
  els.videoPlayer.dataset.rawUrl = rawUrl;
  els.videoPlayer.src = buildProxyUrl(rawUrl, Date.now());
  els.videoPlayer.load();
  startStallTimer(sessionId);
}

function _loadIframeUrl(url, sessionId = state.playerSessionId) {
  if (sessionId !== state.playerSessionId) return;
  state.currentVideoUrl = url;
  state.videoReady = false;
  _resetVideoElement();
  showVideoOverlay("loading", "Abrindo player incorporado...", "Preparando embed");
  els.videoPlayer.style.display = "none";
  els.iframePlayer.style.display = "block";
  els.iframePlayer.onload = () => {
    if (sessionId !== state.playerSessionId) return;
    state.videoReady = true;
    clearStallTimer();
    hideVideoOverlay();
  };
  els.iframePlayer.onerror = () => {
    if (sessionId !== state.playerSessionId) return;
    _escalatePlayerFailure(sessionId);
  };
  els.iframePlayer.src = url;
  startStallTimer(sessionId);
}

async function _fetchEpisodeWithTimeout(animeId, episode, quality, refresh = false) {
  const path = `/api/anime/${encodeURIComponent(animeId)}/episode/${encodeURIComponent(episode)}?quality=${encodeURIComponent(quality)}${refresh ? "&refresh=1" : ""}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), LINK_FETCH_TIMEOUT);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: ctrl.signal
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("Tempo limite ao atualizar o link do episódio.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function _showFinalError() {
  clearStallTimer();
  showVideoOverlay("icon:⚠️", "Vídeo indisponível", "Tente recarregar ou use outra qualidade");
  els.playerNote.textContent = "Não foi possível carregar esse vídeo. Recarregue ou troque a qualidade.";
  showToast("Vídeo indisponível. Tente novamente.");
}

async function _escalatePlayerFailure(sessionId = state.playerSessionId) {
  if (sessionId !== state.playerSessionId) return;
  clearStallTimer();
  const animeId = state.currentAnimeId;
  const episode = state.currentEpisode;
  const currentQuality = state.currentQuality;
  if (!animeId || !episode) {
    _showFinalError();
    return;
  }

  state.attempt += 1;

  if (state.attempt === 1) {
    const rawUrl = state.currentVideoUrl;
    if (!rawUrl) {
      _showFinalError();
      return;
    }
    showToast("Reconectando...");
    showVideoOverlay("loading", "Reconectando...", `Tentativa 2 de ${MAX_ATTEMPTS}`);
    _resetVideoElement();
    els.videoPlayer.dataset.rawUrl = rawUrl;
    els.videoPlayer.src = buildProxyUrl(rawUrl, Date.now());
    els.videoPlayer.load();
    startStallTimer(sessionId);
    return;
  }

  if (state.attempt === 2) {
    showToast("Buscando link atualizado...");
    showVideoOverlay("loading", "Buscando link atualizado...", "O link anterior expirou");
    try {
      const data = await _fetchEpisodeWithTimeout(animeId, episode, currentQuality, true);
      if (sessionId !== state.playerSessionId) return;
      const newUrl = pickEpisodeVideoUrl(data?.item);
      if (!newUrl) throw new Error("Sem vídeo");
      state.currentEpisodeItem = data.item;
      state.currentVideoUrl = newUrl;
      if (isIframeLikeUrl(newUrl) || !_isDirectVideo(newUrl)) _loadIframeUrl(newUrl, sessionId);
      else _loadVideoUrl(newUrl, sessionId);
      return;
    } catch (e) {
      if (sessionId !== state.playerSessionId) return;
      await _escalatePlayerFailure(sessionId);
      return;
    }
  }

  if (state.attempt === 3) {
    const alternativeQuality = oppositeQuality(currentQuality);
    showToast(`Tentando qualidade ${alternativeQuality}...`);
    showVideoOverlay("loading", `Tentando ${alternativeQuality}...`, "Última tentativa automática");
    try {
      const data = await _fetchEpisodeWithTimeout(animeId, episode, alternativeQuality, true);
      if (sessionId !== state.playerSessionId) return;
      const newUrl = pickEpisodeVideoUrl(data?.item);
      if (!newUrl) throw new Error("Sem vídeo");
      state.currentQuality = alternativeQuality;
      state.currentEpisodeItem = data.item;
      state.currentVideoUrl = newUrl;
      renderQualityButtons(data.item);
      updateUrlParams({ anime: animeId, ep: episode, q: state.currentQuality }, { mode: "replace" });
      if (isIframeLikeUrl(newUrl) || !_isDirectVideo(newUrl)) _loadIframeUrl(newUrl, sessionId);
      else _loadVideoUrl(newUrl, sessionId);
      return;
    } catch (e) {
      if (sessionId !== state.playerSessionId) return;
      _showFinalError();
      return;
    }
  }

  _showFinalError();
}

function restoreWatchProgressForCurrentEpisode() {
  const episodeInfo = getEpisodeWatch(state.currentAnimeId, state.currentEpisode);
  const video = els.videoPlayer;
  if (!episodeInfo || !video.duration || !episodeInfo.watchedSeconds) return;
  const safeTime = Math.min(episodeInfo.watchedSeconds, Math.max(0, video.duration - 8));
  if (safeTime > 10 && Math.abs(video.currentTime - safeTime) > 3) {
    try {
      video.currentTime = safeTime;
      showToast(`Retomado em ${formatSeconds(safeTime)}`);
    } catch (e) {}
  }
}

function persistWatchProgress(force = false, completed = false) {
  if (!state.currentAnimeId || !state.currentEpisode) return;
  if (els.videoPlayer.style.display === "none") return;

  const video = els.videoPlayer;
  const watchedSeconds = Math.floor(video.currentTime || 0);
  const durationSeconds = Math.floor(video.duration || 0);
  if (!force && watchedSeconds < WATCH_SAVE_MIN_SECONDS) return;

  const progressKey = `${state.currentAnimeId}:${state.currentEpisode}`;
  const now = Date.now();
  if (!force && state.lastProgressKey === progressKey && now - state.lastProgressSaveAt < WATCH_SAVE_THROTTLE_MS) return;

  state.lastProgressKey = progressKey;
  state.lastProgressSaveAt = now;
  saveWatchProgress({
    animeId: state.currentAnimeId,
    animeTitle: state.anime?.title || "Anime",
    episode: state.currentEpisode,
    watchedSeconds,
    durationSeconds,
    cover: state.anime?.cover_url || state.anime?.banner_url || "",
    completed: completed || (durationSeconds > 0 && watchedSeconds / Math.max(durationSeconds, 1) >= 0.92)
  });
}

function bindVideoEvents() {
  const video = els.videoPlayer;

  const onReady = () => {
    state.videoReady = true;
    clearStallTimer();
    hideVideoOverlay();
    restoreWatchProgressForCurrentEpisode();
  };

  const onBuffering = (title, sub) => {
    if (video.style.display === "none") return;
    if (!state.currentVideoUrl) return;
    if (video.paused || video.ended) return;
    showVideoOverlay("loading", title, sub);
    startStallTimer(state.playerSessionId);
  };

  video.addEventListener("loadedmetadata", onReady);
  video.addEventListener("canplay", () => {
    state.videoReady = true;
    clearStallTimer();
    hideVideoOverlay();
  });
  video.addEventListener("playing", () => {
    state.videoReady = true;
    clearStallTimer();
    hideVideoOverlay();
    state.attempt = 0;
  });
  video.addEventListener("waiting", () => onBuffering("Buffering...", "Aguardando dados"));
  video.addEventListener("stalled", () => onBuffering("Conexão lenta...", "Tentando recuperar"));
  video.addEventListener("error", async () => {
    if (!video.src || video.src === window.location.href) return;
    await _escalatePlayerFailure(state.playerSessionId);
  });
  video.addEventListener("timeupdate", () => persistWatchProgress(false));
  video.addEventListener("pause", () => persistWatchProgress(true));
  video.addEventListener("ended", () => {
    if (!state.currentAnimeId || !state.currentEpisode) return;
    persistWatchProgress(true, true);
    renderContinueWatching();

    const nextEpisode = els.nextEpisodeBtn.dataset.episode;
    const animeId = state.currentAnimeId;
    const quality = state.currentQuality;
    clearNextEpisodeTimer();
    if (nextEpisode && !els.nextEpisodeBtn.disabled) {
      showToast("Próximo episódio em 3s... Toque para cancelar");
      state.nextEpisodeTimer = setTimeout(() => {
        if (animeId === state.currentAnimeId && state.currentPage === "player") {
          openEpisode(animeId, nextEpisode, quality, { pushHistory: false, force: true });
        }
      }, AUTO_NEXT_DELAY_MS);
      const cancel = () => {
        clearNextEpisodeTimer();
        showToast("Próximo episódio cancelado.");
      };
      state.nextEpisodeCancelClick = cancel;
      setTimeout(() => {
        document.addEventListener("click", cancel, { once: true });
      }, 250);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) persistWatchProgress(true);
  });
  window.addEventListener("pagehide", () => persistWatchProgress(true));
}

function renderGenreTags(genres, expanded = false) {
  const list = Array.isArray(genres) ? genres.filter(Boolean) : [];
  const visible = expanded ? list : list.slice(0, 5);
  const tags = visible.map(genre => `<span class="genre-tag" data-genre-search="${esc(genre)}">${esc(genre)}</span>`).join("");
  const toggle = list.length > 5
    ? `<div class="genre-toggle-row"><button class="btn btn-ghost btn-xs" id="genreToggleBtn">${expanded ? "Ver menos" : "Ver mais"}</button></div>`
    : "";
  els.animeGenres.innerHTML = tags + toggle;
}

function buildSuggestionChips(suggestions) {
  if (!Array.isArray(suggestions) || !suggestions.length) return "";
  return `<div style="margin-top:14px"><div style="font-size:12px;color:var(--text3);margin-bottom:8px;">Talvez você quis dizer:</div><div class="chips">${suggestions.map(suggestion => `<button class="chip" data-suggest-search="${esc(suggestion)}">${esc(suggestion)}</button>`).join("")}</div></div>`;
}

function _normalizeCatalogResponse(data) {
  if (Array.isArray(data)) return { items: data, page: 1, total_pages: 1, total_items: data.length, title: "Seção" };
  if (Array.isArray(data?.items)) return data;
  if (Array.isArray(data?.results)) return { ...data, items: data.results };
  if (Array.isArray(data?.data)) return { ...data, items: data.data };
  if (Array.isArray(data?.animes)) return { ...data, items: data.animes };
  if (Array.isArray(data?.list)) return { ...data, items: data.list };
  for (const key of Object.keys(data || {})) {
    if (Array.isArray(data[key]) && (!data.items || !data.items.length)) return { ...data, items: data[key] };
  }
  return { ...(data || {}), items: [] };
}

function _normalizeSearchResponse(data) {
  if (Array.isArray(data)) return { items: data, count: data.length, page: 1, total_pages: 1 };
  if (Array.isArray(data?.items)) return data;
  if (Array.isArray(data?.results)) return { ...data, items: data.results };
  if (Array.isArray(data?.data)) return { ...data, items: data.data };
  if (Array.isArray(data?.animes)) return { ...data, items: data.animes };
  if (Array.isArray(data?.list)) return { ...data, items: data.list };
  for (const key of Object.keys(data || {})) {
    if (Array.isArray(data[key]) && data[key].length > 0 && data[key][0]?.id) return { ...data, items: data[key] };
  }
  return { items: [], count: 0, page: 1, total_pages: 1 };
}

function _parseEpNumber(episode) {
  const raw = episode?.number ?? episode?.episode ?? episode?.ep ?? episode?.slug ?? episode?.num ?? "";
  const parsed = parseFloat(String(raw).replace(",", "."));
  return Number.isNaN(parsed) ? null : parsed;
}

function _sortEpisodes(episodes) {
  return [...(episodes || [])].sort((a, b) => {
    const aNumber = _parseEpNumber(a);
    const bNumber = _parseEpNumber(b);
    if (aNumber === null && bNumber === null) return 0;
    if (aNumber === null) return 1;
    if (bNumber === null) return -1;
    return aNumber - bNumber;
  });
}

function _getEpisodeDisplayTotal(episodes = []) {
  const numbers = (Array.isArray(episodes) ? episodes : []).map(_parseEpNumber).filter(number => typeof number === "number" && !Number.isNaN(number));
  const maxFromList = numbers.length ? Math.max(...numbers) : 0;
  const metaTotal = parseInt(state.anime?.episodes, 10) || 0;
  const playerTotal = parseInt(state.currentEpisodeItem?.total_episodes, 10) || 0;
  const currentEpisode = parseInt(state.currentEpisode, 10) || 0;
  return Math.max(maxFromList, metaTotal, playerTotal, currentEpisode, Array.isArray(episodes) ? episodes.length : 0);
}

function _buildFullEpisodeList(episodes, totalFromMetadata) {
  if (!episodes || episodes.length === 0) return [];
  const sorted = _sortEpisodes(episodes);
  const metaTotal = parseInt(totalFromMetadata, 10) || 0;
  if (metaTotal > 0 && sorted.length < metaTotal) {
    const existingNumbers = new Set(sorted.map(item => _parseEpNumber(item)));
    const filled = [...sorted];
    for (let index = 1; index <= metaTotal; index += 1) {
      if (!existingNumbers.has(index)) filled.push({ number: index, episode: String(index), synthetic: true });
    }
    return _sortEpisodes(filled);
  }
  return sorted;
}

function renderEpisodeButtons(episodes) {
  const metaTotal = _getEpisodeDisplayTotal(episodes);
  const fullList = _buildFullEpisodeList(episodes, metaTotal);
  if (!fullList.length) {
    els.episodesInfo.textContent = "Sem episódios disponíveis.";
    els.episodesGrid.innerHTML = `<div class="state-box"><div class="state-box-icon">📭</div><div class="state-box-title">Sem episódios</div><div class="state-box-text">Esse anime não retornou episódios agora.</div></div>`;
    return;
  }

  const realCount = Array.isArray(episodes) ? episodes.length : 0;
  const syntheticCount = fullList.filter(item => item.synthetic).length;
  els.episodesInfo.textContent = syntheticCount
    ? `${realCount} confirmado(s) · ${syntheticCount} previsto(s) pela metadata`
    : `${Math.max(realCount, metaTotal, fullList.length)} episódio(s) disponível(is)`;

  const animeWatch = getAnimeWatch(state.currentAnimeId);
  els.episodesGrid.innerHTML = fullList.map(item => {
    const num = String(_parseEpNumber(item) ?? item.slug ?? "").trim();
    const label = num || (item.title || "—");
    const isActive = String(state.currentEpisode) === String(num);
    const isWatched = Boolean(animeWatch?.episodes?.[String(num)]?.completed);
    const isSynthetic = item.synthetic === true;
    const title = isSynthetic ? `Episódio ${label} ainda não confirmado pela API` : `Abrir episódio ${label}`;
    return `<button class="ep-btn ${isActive ? "active" : ""} ${isWatched ? "watched" : ""} ${isSynthetic ? "synthetic" : ""}" ${isSynthetic || !num ? "disabled" : `data-open-episode="${esc(num)}"`} title="${esc(title)}">${esc(label)}</button>`;
  }).join("");
}

function getAllQualities(playerItem) {
  const raw = Array.isArray(playerItem?.available_qualities) ? playerItem.available_qualities : [];
  const set = new Set(raw.map(normalizeQuality));
  set.add(normalizeQuality(playerItem?.quality || state.currentQuality || "HD"));
  if (!set.size) set.add("HD");
  return [...set].filter(Boolean);
}

function renderQualityButtons(playerItem) {
  const qualities = getAllQualities(playerItem);
  els.qualityRow.innerHTML = qualities.map(quality => {
    const active = normalizeQuality(quality) === normalizeQuality(state.currentQuality);
    return `<button class="quality-btn ${active ? "active" : ""}" data-quality="${esc(quality)}">${esc(quality)}</button>`;
  }).join("");
}

function buildCard(item) {
  const cover = imgSrc(item.banner_url || item.cover_url, item.title);
  const isDubbed = item.is_dubbed || item.prefix === "DUB";
  const badge = isDubbed ? "DUB" : "LEG";
  const episodeTag = item.episode ? `<div class="card-ep-tag">EP ${esc(item.episode)}</div>` : "";
  const watchKey = item.anime_id || item.id;
  const hasWatched = Boolean(getAnimeWatch(watchKey)?.latestEpisode);
  const watchBadge = hasWatched ? `<div class="card-watched-badge">✓</div>` : "";
  const meta = [item.status, item.year, item.episodes ? `${item.episodes} eps` : item.episode ? `Episódio ${item.episode}` : ""].filter(Boolean).map(esc);
  const attrs = item.open_mode === "episode" && item.anime_id && item.episode
    ? `data-open-direct="1" data-anime-id="${esc(item.anime_id)}" data-episode="${esc(item.episode)}"`
    : `data-anime-id="${esc(item.id)}"`;
  return `<article class="anime-card" ${attrs}>
    <div class="card-thumb">
      <div class="card-badge ${isDubbed ? "dub" : "leg"}">${badge}</div>
      ${watchBadge}${episodeTag}
      <div class="card-play-overlay"><div class="play-circle">▶</div></div>
      <img src="${esc(cover)}" alt="${esc(item.title || "Anime")}" loading="lazy" data-fallback-title="${esc(item.title || "Anime")}" />
    </div>
    <div class="card-body">
      <div class="card-title">${esc(item.title || "Sem título")}</div>
      <div class="card-meta">${meta.join(" · ") || "Toque para abrir"}</div>
    </div>
  </article>`;
}

function skeletonGrid(count = 12) {
  let html = '<div class="card-grid">';
  for (let index = 0; index < count; index += 1) {
    html += `<div class="skeleton-card"><div class="skeleton skeleton-thumb"></div><div class="skeleton-lines"><div class="skeleton skeleton-line w-80"></div><div class="skeleton skeleton-line w-50"></div></div></div>`;
  }
  return `${html}</div>`;
}

function renderSectionBlock(section) {
  const items = Array.isArray(section?.items) ? section.items : [];
  return `<div class="section">
    <div class="section-head">
      <div class="section-label">
        <div class="section-accent"></div>
        <div>
          <div class="section-title">${esc(section?.title || "Seção")}</div>
          <div class="section-count">${items.length} títulos</div>
        </div>
      </div>
      <button class="btn btn-ghost btn-sm" data-open-section="${esc(section?.key || "")}">Ver mais →</button>
    </div>
    <div class="card-grid">${items.map(buildCard).join("")}</div>
  </div>`;
}

function renderAnimeMeta(item) {
  const isDubbed = item.is_dubbed || item.prefix === "DUB";
  const pills = [];
  pills.push(`<span class="pill">${isDubbed ? "🎙️ Dublado" : "📝 Legendado"}</span>`);
  if (item.score) pills.push(`<span class="pill">⭐ ${esc(item.score)}</span>`);
  if (item.status) pills.push(`<span class="pill">${esc(item.status)}</span>`);
  if (item.year) pills.push(`<span class="pill">📅 ${esc(item.year)}</span>`);
  if (item.episodes) pills.push(`<span class="pill">🎬 ${esc(item.episodes)} eps</span>`);
  if (item.studio) pills.push(`<span class="pill">🏢 ${esc(item.studio)}</span>`);
  return pills.join("");
}

function renderStaticHero() {
  clearHeroTimer();
  els.heroArea.innerHTML = `
    <div class="hero-static">
      <div class="hero-static-inner">
        <div>
          <div class="hero-eyebrow">Anime Streaming</div>
          <h1 style="font-family:var(--font-d);font-size:clamp(26px,4vw,48px);font-weight:800;line-height:1.05;letter-spacing:-.03em;margin-bottom:12px">
            Assista seus<br/>
            <span style="background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">animes favoritos</span>
          </h1>
          <p style="color:var(--text2);font-size:14px;line-height:1.7;max-width:480px">Pesquise, explore e assista direto no Telegram.</p>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:20px">
            <button class="btn btn-primary" id="heroExploreBtn">▶ Explorar catálogo</button>
            <button class="btn btn-ghost" id="heroSearchBtn">🔎 Buscar anime</button>
          </div>
        </div>
        <div class="hero-art"><div>⛩️</div></div>
      </div>
    </div>`;
  $("heroExploreBtn")?.addEventListener("click", () => loadSectionResults("top", 1));
  $("heroSearchBtn")?.addEventListener("click", () => focusSearch());
}

function setupHeroBanner(items) {
  const banners = (Array.isArray(items) ? items : []).filter(item => item?.banner_url || item?.cover_url).slice(0, 5);
  if (!banners.length) {
    renderStaticHero();
    return;
  }
  state.heroBanners = banners;
  renderHeroBanner(0);
  if (banners.length > 1) startHeroTimer();
}

function renderHeroBanner(index) {
  const item = state.heroBanners[index];
  if (!item) return;
  state.heroIdx = index;
  state.heroCurrent = item;
  const cover = encodeURI(imgSrc(item.banner_url || item.cover_url, item.title));
  const isDubbed = item.is_dubbed || item.prefix === "DUB";
  const pills = [
    item.status ? `<span class="hero-pill">${esc(item.status)}</span>` : "",
    item.year ? `<span class="hero-pill">📅 ${esc(item.year)}</span>` : "",
    item.episodes ? `<span class="hero-pill">🎬 ${esc(item.episodes)} eps</span>` : "",
    `<span class="hero-pill">${isDubbed ? "🎙️ DUB" : "📝 LEG"}</span>`
  ].join("");
  const dots = state.heroBanners.map((_, dotIndex) => `<button class="hero-dot ${dotIndex === index ? "on" : ""}" data-hidx="${dotIndex}"></button>`).join("");
  els.heroArea.innerHTML = `
    <div class="hero-banner" id="heroBannerEl">
      <div class="hero-bg" style="background-image:url('${cover}')"></div>
      <div class="hero-grad"></div>
      <div class="hero-body">
        <div class="hero-eyebrow">Em Destaque</div>
        <div class="hero-title-el">${esc(item.title || "Anime")}</div>
        <div class="hero-meta-row">${pills}</div>
        <p class="hero-desc-el">${esc(item.description || "")}</p>
        <div class="hero-actions">
          <button class="btn btn-primary" id="heroWatchBtn">▶ Assistir Episódio 1</button>
          <button class="btn btn-ghost" id="heroMoreBtn">+ Detalhes</button>
        </div>
      </div>
      <div class="hero-dots">${dots}</div>
    </div>`;

  $("heroWatchBtn")?.addEventListener("click", async () => {
    const id = state.heroCurrent?.id;
    if (!id) return;
    if (!state.anime || state.currentAnimeId !== String(id)) {
      await openAnime(id, { pushHistory: false, showPage: false, skipRoute: true, suppressLoading: true });
    }
    const firstEpisode = getFirstEpisodeNumber(state.animeEpisodes);
    if (firstEpisode) await openEpisode(id, firstEpisode, state.currentQuality || "HD");
  });
  $("heroMoreBtn")?.addEventListener("click", () => {
    if (state.heroCurrent?.id) openAnime(state.heroCurrent.id);
  });
  $("heroBannerEl")?.addEventListener("click", event => {
    if (event.target.closest("button") || event.target.closest(".hero-dot")) return;
    if (state.heroCurrent?.id) openAnime(state.heroCurrent.id);
  });
}

function startHeroTimer() {
  clearHeroTimer();
  state.heroTimer = setTimeout(() => {
    const nextIndex = (state.heroIdx + 1) % state.heroBanners.length;
    renderHeroBanner(nextIndex);
    startHeroTimer();
  }, 6000);
}

function renderContinueWatching() {
  const items = getContinueWatching().slice(0, 8);
  if (!items.length) {
    els.continueSection.innerHTML = "";
    return;
  }

  const pct = item => {
    const latestEpisode = String(item.latestEpisode || "").trim();
    const episodeInfo = item.episodes?.[latestEpisode];
    return episodeInfo?.durationSeconds > 0 ? Math.min(100, Math.round((episodeInfo.watchedSeconds / episodeInfo.durationSeconds) * 100)) : 0;
  };

  const cards = items.map(item => `
    <div class="cw-card" data-resume-anime="${esc(item.animeId)}" data-resume-ep="${esc(item.latestEpisode)}">
      <div class="cw-thumb">
        <img src="${esc(imgSrc(item.cover, item.animeTitle))}" alt="${esc(item.animeTitle)}" loading="lazy" data-fallback-title="${esc(item.animeTitle || "Anime")}" />
        <div class="cw-prog-bar"><div class="cw-prog-fill" style="width:${pct(item)}%"></div></div>
        <div class="cw-ep-badge">EP ${esc(item.latestEpisode)}</div>
        <div class="cw-play-ov"><div class="cw-play-ico">▶</div></div>
      </div>
      <div class="cw-info">
        <div class="cw-title">${esc(item.animeTitle || item.animeId)}</div>
        <div class="cw-sub">Continuar EP ${esc(item.latestEpisode)} · ${pct(item)}%</div>
      </div>
    </div>`).join("");

  els.continueSection.innerHTML = `
    <div class="section" style="margin-bottom:24px">
      <div class="section-head">
        <div class="section-label">
          <div class="section-accent"></div>
          <div>
            <div class="section-title">Continue Assistindo</div>
            <div class="section-count">Retome de onde parou</div>
          </div>
        </div>
      </div>
      <div class="cw-scroll">${cards}</div>
    </div>`;
}

function renderPagination(current, total, onPage) {
  if (total <= 1) {
    els.resultsPagination.innerHTML = "";
    return;
  }
  const delta = 2;
  const range = [];
  for (let index = Math.max(2, current - delta); index <= Math.min(total - 1, current + delta); index += 1) range.push(index);
  if (current - delta > 2) range.unshift("...");
  if (current + delta < total - 1) range.push("...");
  const pages = [1, ...range, total];
  const buttons = pages.map(page => {
    if (page === "...") return `<span class="pg-btn" style="border:none;pointer-events:none;opacity:.4">…</span>`;
    return `<button class="pg-btn ${page === current ? "active-pg" : ""}" data-pg="${page}">${page}</button>`;
  }).join("");
  const gotoHtml = `<div class="pg-input-wrap"><span>Ir para</span><input class="pg-input" id="pgGotoInput" type="number" min="1" max="${total}" value="${current}" /><button class="pg-btn" id="pgGotoBtn">→</button></div>`;
  els.resultsPagination.innerHTML = `
    <div class="pagination-row">
      <button class="pg-btn" data-pg="${current - 1}" ${current <= 1 ? "disabled" : ""}>← Ant</button>
      ${buttons}
      <button class="pg-btn" data-pg="${current + 1}" ${current >= total ? "disabled" : ""}>Próx →</button>
      <button class="pg-btn" data-pg="${total}" ${current >= total ? "disabled" : ""}>Última ⏭</button>
    </div>
    <div class="pagination-row" style="margin-top:8px">${gotoHtml}</div>`;

  els.resultsPagination.querySelectorAll("[data-pg]").forEach(button => {
    const page = parseInt(button.dataset.pg || "", 10);
    if (Number.isNaN(page) || button.disabled) return;
    button.addEventListener("click", () => onPage(page));
  });
  $("pgGotoBtn")?.addEventListener("click", () => {
    const value = parseInt($("pgGotoInput")?.value || "", 10);
    if (!Number.isNaN(value) && value >= 1 && value <= total) onPage(value);
  });
  $("pgGotoInput")?.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    const value = parseInt(event.target.value || "", 10);
    if (!Number.isNaN(value) && value >= 1 && value <= total) onPage(value);
  });
}

function getFirstEpisodeNumber(episodes) {
  if (!Array.isArray(episodes) || !episodes.length) return "";
  const sorted = _sortEpisodes(episodes);
  for (const episode of sorted) {
    const number = String(_parseEpNumber(episode) ?? episode.slug ?? "").trim();
    if (number) return number;
  }
  return "";
}

function _sanitizeAnimeTitle(title) {
  if (!title) return title;
  const patterns = [
    /\s*[-–]\s*Epis[oó]dio\s+\d+.*$/i,
    /\s*[-–]\s*Episode\s+\d+.*$/i,
    /\s*[-–]\s*Ep\.?\s*\d+.*$/i,
    /\s*\|\s*Epis[oó]dio\s+\d+.*$/i
  ];
  let clean = title;
  for (const pattern of patterns) clean = clean.replace(pattern, "").trim();
  return clean;
}

function updateAnimeActionButtons() {
  const firstEpisode = getFirstEpisodeNumber(state.animeEpisodes);
  const animeWatch = getAnimeWatch(state.currentAnimeId);
  const resumeEpisode = String(animeWatch?.latestEpisode || "").trim();
  els.openFirstEpisodeBtn.disabled = !firstEpisode;

  if (state.currentEpisode && state.currentAnimeId) {
    els.goToPlayerBtn.disabled = false;
    els.goToPlayerBtn.textContent = `🎬 Voltar ao EP ${state.currentEpisode}`;
    return;
  }
  if (resumeEpisode) {
    els.goToPlayerBtn.disabled = false;
    els.goToPlayerBtn.textContent = `▶ Continuar EP ${resumeEpisode}`;
    return;
  }
  els.goToPlayerBtn.disabled = !firstEpisode;
  els.goToPlayerBtn.textContent = firstEpisode ? "🎬 Assistir agora" : "🎬 Player";
}

async function loadHome() {
  const requestId = ++state.requestSeq.home;
  renderContinueWatching();
  clearHeroTimer();
  renderStaticHero();
  els.homeSections.innerHTML = skeletonGrid(12);
  setSubtitle("Carregando...");
  try {
    const data = await apiGet("/api/catalog/home", API_TIMEOUT, "home");
    if (requestId !== state.requestSeq.home) return;
    state.home = data;
    setSubtitle("Anime Streaming");
    const sections = Array.isArray(data?.sections) ? data.sections : [];
    if (!sections.length) {
      els.homeSections.innerHTML = `<div class="state-box"><div class="state-box-icon">📭</div><div class="state-box-title">Nada por aqui</div><div class="state-box-text">Nenhuma seção encontrada.</div></div>`;
      return;
    }
    els.homeSections.innerHTML = sections.map(renderSectionBlock).join("");
    const featuredSource = data?.featured ? [data.featured] : (Array.isArray(sections[1]?.items) && sections[1].items.length ? sections[1].items : sections[0]?.items || []);
    if (featuredSource.length) setupHeroBanner(featuredSource.slice(0, 5));
    updateActiveSectionChip("");
  } catch (error) {
    if (requestId !== state.requestSeq.home) return;
    setSubtitle("Erro");
    els.homeSections.innerHTML = `<div class="state-box error"><div class="state-box-icon">⚠️</div><div class="state-box-title">Falha ao carregar</div><div class="state-box-text">${esc(error.message || "Erro desconhecido")}</div><button class="btn btn-primary btn-sm" data-retry-home style="margin-top:4px">↺ Tentar novamente</button></div>`;
    showToast("Falha ao carregar catálogo.");
  }
}

async function loadSectionResults(sectionKey, page = 1, options = {}) {
  const requestId = ++state.requestSeq.section;
  const push = options.pushHistory !== false;
  const loadingToken = showLoading("Carregando seção", "Buscando animes...");
  setPage("results", push);
  state.resultsMode = "section";
  state.currentSection = sectionKey;
  state.searchQuery = "";
  els.searchClear.style.display = "none";
  updateActiveSectionChip(sectionKey);

  try {
    const raw = await apiGet(`/api/catalog/list?section=${encodeURIComponent(sectionKey)}&page=${page}`, API_TIMEOUT, "section");
    if (requestId !== state.requestSeq.section) return;
    const data = _normalizeCatalogResponse(raw);
    const items = Array.isArray(data?.items) ? data.items : [];
    state.searchResults = items;
    state.resultsPage = Number(data?.page || page);
    state.totalPages = Number(data?.total_pages || 1);
    els.resultsTitle.textContent = data?.title || "Seção";
    els.resultsSubtitle.textContent = `${data?.total_items || data?.count || items.length} título(s) · Pág. ${state.resultsPage}/${state.totalPages}`;
    els.resultsGrid.innerHTML = items.length ? `<div class="card-grid">${items.map(buildCard).join("")}</div>` : `<div class="state-box"><div class="state-box-icon">📭</div><div class="state-box-title">Nenhum anime</div></div>`;
    renderPagination(state.resultsPage, state.totalPages, nextPage => loadSectionResults(sectionKey, nextPage));
    updateUrlParams({ section: sectionKey, page: state.resultsPage }, { mode: push ? "push" : "replace" });
  } catch (error) {
    if (requestId !== state.requestSeq.section) return;
    els.resultsTitle.textContent = "Erro";
    els.resultsSubtitle.textContent = error.message || "";
    els.resultsGrid.innerHTML = `<div class="state-box error"><div class="state-box-icon">⚠️</div><div class="state-box-title">Falha ao carregar</div><div class="state-box-text">${esc(error.message || "")}</div></div>`;
    showToast("Erro ao abrir seção.");
  } finally {
    hideLoading(loadingToken);
  }
}

async function performSearch(query, page = 1, options = {}) {
  const q = String(query || "").trim();
  const silent = options.silent === true;
  const push = options.pushHistory !== false;
  if (q.length < 2) {
    if (!silent) showToast("Digite pelo menos 2 caracteres.");
    return;
  }

  const requestId = ++state.requestSeq.search;
  state.resultsMode = "search";
  state.searchQuery = q;
  state.currentSection = "";
  els.searchInput.value = q;
  els.searchClear.style.display = q ? "block" : "none";
  updateActiveSectionChip("");

  let loadingToken = null;
  if (!silent) {
    loadingToken = showLoading("Buscando anime", "Procurando resultados...");
    setPage("results", push);
  }

  try {
    const rawData = await apiGet(`/api/search?q=${encodeURIComponent(q)}&page=${page}`, API_TIMEOUT, "search");
    if (requestId !== state.requestSeq.search) return;
    const data = _normalizeSearchResponse(rawData);
    const items = data?.items || [];
    state.searchResults = items;
    state.resultsPage = Number(data?.page || page);
    state.totalPages = Number(data?.total_pages || 1);

    if (!silent || state.currentPage === "results") {
      els.resultsTitle.textContent = `"${q}"`;
      els.resultsSubtitle.textContent = `${data?.count ?? items.length} resultado(s)`;
      els.resultsGrid.innerHTML = items.length
        ? `<div class="card-grid">${items.map(buildCard).join("")}</div>`
        : `<div class="state-box"><div class="state-box-icon">🔍</div><div class="state-box-title">Nada encontrado</div><div class="state-box-text">Nenhum anime para "${esc(q)}".</div><div style="font-size:12px;color:var(--text3);margin-top:8px;">Tente variações do nome ou palavras-chave diferentes.</div>${buildSuggestionChips(data?.suggestions || [])}</div>`;
      renderPagination(state.resultsPage, state.totalPages, nextPage => performSearch(q, nextPage));
    }

    if (!silent) updateUrlParams({ search: q, page: state.resultsPage }, { mode: push ? "push" : "replace" });
    else if (state.currentPage === "results") updateUrlParams({ search: q, page: state.resultsPage }, { mode: "replace" });
  } catch (error) {
    if (requestId !== state.requestSeq.search) return;
    if (!silent || state.currentPage === "results") {
      els.resultsTitle.textContent = "Erro na busca";
      els.resultsSubtitle.textContent = error.message || "";
      els.resultsGrid.innerHTML = `<div class="state-box error"><div class="state-box-icon">⚠️</div><div class="state-box-title">Falha na busca</div><div class="state-box-text">${esc(error.message || "Verifique sua conexão.")}</div></div>`;
    }
    if (!silent) showToast("Falha na busca.");
  } finally {
    if (loadingToken) hideLoading(loadingToken);
  }
}

async function openAnime(animeId, options = {}) {
  if (!animeId) return;
  const requestId = ++state.requestSeq.anime;
  const push = options.pushHistory !== false;
  const showPage = options.showPage !== false;
  const skipRoute = options.skipRoute === true;
  const suppressLoading = options.suppressLoading === true;
  const switchingAnime = Boolean(state.currentAnimeId) && state.currentAnimeId !== String(animeId);
  const loadingToken = suppressLoading ? null : showLoading("Abrindo anime", "Carregando detalhes...");

  try {
    if (switchingAnime) resetEpisodeStateForAnimeSwitch();
    const data = await apiGet(`/api/anime/${encodeURIComponent(animeId)}`, API_TIMEOUT, "anime");
    if (requestId !== state.requestSeq.anime) return;
    const item = data?.item || null;
    const episodes = Array.isArray(data?.episodes) ? data.episodes : [];
    if (!item) throw new Error("Anime não encontrado");

    item.title = _sanitizeAnimeTitle(item.title);
    state.anime = item;
    state.currentAnimeId = String(animeId);
    state.animeEpisodes = episodes;

    const isDubbed = item.is_dubbed || item.prefix === "DUB";
    els.animeCover.src = imgSrc(item.banner_url || item.cover_url, item.title);
    els.animeCover.alt = item.title || "Anime";
    els.animePrefix.textContent = isDubbed ? "🎙️ DUBLADO" : "📝 LEGENDADO";
    els.animePrefix.className = `detail-kicker ${isDubbed ? "dub" : "leg"}`;
    els.animeTitle.textContent = item.title || "Sem título";
    els.animeMeta.innerHTML = renderAnimeMeta(item);
    els.animeDesc.textContent = item.description || "Sem descrição disponível.";
    els.animeDesc.className = "desc-text coll";
    els.descToggle.textContent = "Ver mais ▾";
    state.genreExpanded = false;
    renderGenreTags(item.genres || [], false);
    renderEpisodeButtons(episodes);
    updateAnimeActionButtons();

    if (showPage) {
      setPage("anime", push);
      if (!skipRoute) updateUrlParams({ anime: animeId }, { mode: push ? "push" : "replace" });
    }
  } catch (error) {
    if (requestId !== state.requestSeq.anime) return;
    showToast("Não foi possível abrir esse anime.");
  } finally {
    if (loadingToken) hideLoading(loadingToken);
  }
}

async function openEpisode(animeId, episode, quality = "HD", options = {}) {
  if (!animeId || !episode) {
    showToast("Anime ou episódio inválido.");
    return;
  }
  if (state.openingEpisode && !options.force) return;

  const requestId = ++state.openEpisodeRequestId;
  const sessionId = ++state.playerSessionId;
  const push = options.pushHistory !== false;
  const normalizedQuality = normalizeQuality(quality || state.currentQuality || "HD");
  state.openingEpisode = true;
  clearNextEpisodeTimer();
  clearStallTimer();
  abortNamedRequest("episode");
  resetPlayerTransport({ clearContext: false });

  const loadingToken = showLoading("Abrindo episódio", `Carregando EP ${episode} em ${normalizedQuality}...`);
  try {
    if (!state.anime || state.currentAnimeId !== String(animeId)) {
      await openAnime(animeId, { pushHistory: false, showPage: false, skipRoute: true, suppressLoading: true });
    }
    if (requestId !== state.openEpisodeRequestId || sessionId !== state.playerSessionId) return;

    const data = await apiGet(`/api/anime/${encodeURIComponent(animeId)}/episode/${encodeURIComponent(episode)}?quality=${encodeURIComponent(normalizedQuality)}`, API_TIMEOUT, "episode");
    if (requestId !== state.openEpisodeRequestId || sessionId !== state.playerSessionId) return;
    const item = data?.item || null;
    if (!item) throw new Error("Episódio não encontrado");

    state.currentAnimeId = String(animeId);
    state.currentEpisode = String(episode);
    state.currentQuality = normalizeQuality(item.quality || normalizedQuality);
    state.currentEpisodeItem = item;
    state.currentVideoUrl = "";
    state.attempt = 0;

    const animeTitle = state.anime?.title || "Anime";
    const displayTotalEpisodes = Math.max(parseInt(item.total_episodes, 10) || 0, _getEpisodeDisplayTotal(state.animeEpisodes), parseInt(episode, 10) || 0);
    els.playerPageSubtitle.textContent = `${animeTitle} • EP ${episode}`;
    els.playerTitle.textContent = animeTitle;
    els.playerMeta.textContent = [`EP ${episode}`, state.currentQuality, displayTotalEpisodes ? `de ${displayTotalEpisodes}` : "", item.is_dubbed ? "Dublado" : "Legendado"].filter(Boolean).join(" · ");
    els.playerDesc.textContent = item.description || state.anime?.description || "";

    const previewVideoUrl = pickEpisodeVideoUrl(item);
    els.playerNote.textContent = previewVideoUrl
      ? (isIframeLikeUrl(previewVideoUrl) ? "Esse episódio está usando player incorporado como fallback de estabilidade." : "Player reforçado: reconecta automaticamente se o link expirar.")
      : "Esse episódio não retornou URL de vídeo.";

    renderQualityButtons(item);
    els.prevEpisodeBtn.disabled = !item.prev_episode;
    els.nextEpisodeBtn.disabled = !item.next_episode;
    els.prevEpisodeBtn.dataset.episode = item.prev_episode || "";
    els.nextEpisodeBtn.dataset.episode = item.next_episode || "";

    const resolvedVideoUrl = pickEpisodeVideoUrl(item);
    if (resolvedVideoUrl) {
      if (isIframeLikeUrl(resolvedVideoUrl) || !_isDirectVideo(resolvedVideoUrl)) _loadIframeUrl(resolvedVideoUrl, sessionId);
      else _loadVideoUrl(resolvedVideoUrl, sessionId);
      els.openInBrowserBtn.disabled = false;
      els.openInBrowserBtn.dataset.url = resolvedVideoUrl;
    } else {
      clearPlayerState();
      showVideoOverlay("icon:⚠️", "Sem vídeo", "Esse episódio não tem URL de vídeo");
      els.openInBrowserBtn.disabled = true;
      els.openInBrowserBtn.dataset.url = "";
      showToast("Esse episódio não retornou vídeo.");
    }

    renderEpisodeButtons(state.animeEpisodes);
    updateAnimeActionButtons();
    setPage("player", push);
    updateUrlParams({ anime: animeId, ep: episode, q: state.currentQuality }, { mode: push ? "push" : "replace" });
  } catch (error) {
    if (requestId === state.openEpisodeRequestId && sessionId === state.playerSessionId) {
      showToast("Não foi possível abrir esse episódio.");
      showVideoOverlay("icon:⚠️", "Falha ao abrir episódio", error.message || "Tente novamente.");
    }
  } finally {
    hideLoading(loadingToken);
    if (requestId === state.openEpisodeRequestId) state.openingEpisode = false;
  }
}

function focusSearch() {
  els.searchInput.focus();
  els.searchInput.scrollIntoView({ behavior: "smooth", block: "center" });
}

function connectRealtimeUpdates() {
  try {
    state.eventSource?.close?.();
  } catch (e) {}

  try {
    state.eventSource = new EventSource("/api/events");
    state.eventSource.addEventListener("catalog", async event => {
      let payload = null;
      try {
        payload = JSON.parse(event.data || "{}");
      } catch (e) {}
      if (!payload) return;
      if (state.currentPage === "home") await loadHome();
      if (state.currentPage === "results" && (state.currentSection === "recentes" || state.currentSection === "em_lancamento")) {
        await loadSectionResults(state.currentSection, state.resultsPage || 1, { pushHistory: false });
      }
      if (state.currentPage === "anime" && state.currentAnimeId) {
        await openAnime(state.currentAnimeId, { pushHistory: false });
      }
    });
  } catch (e) {}
}

async function bootFromRoute() {
  if (state.autoBootDone) return;
  state.autoBootDone = true;

  const route = parseRoute();
  els.searchInput.value = route.search || "";
  els.searchClear.style.display = route.search ? "block" : "none";

  if (route.search) {
    await performSearch(route.search, route.page, { pushHistory: false });
    return;
  }
  if (route.section) {
    await loadSectionResults(route.section, route.page, { pushHistory: false });
    return;
  }
  if (route.anime && route.ep) {
    await openEpisode(route.anime, route.ep, route.q, { pushHistory: false, force: true });
    return;
  }
  if (route.anime) {
    await openAnime(route.anime, { pushHistory: false });
    return;
  }

  const storedRoute = getStoredRoute();
  if (storedRoute?.anime) {
    if (storedRoute.ep) {
      await openEpisode(storedRoute.anime, storedRoute.ep, storedRoute.q || "HD", { pushHistory: false, force: true });
      return;
    }
    await openAnime(storedRoute.anime, { pushHistory: false });
    return;
  }

  setPage("home", false);
  updateUrlParams({}, { mode: "replace" });
  await loadHome();
}

function openExternalLink(url) {
  if (!url) return;
  try {
    if (tg?.openLink) {
      tg.openLink(url, { try_browser: true });
      return;
    }
  } catch (e) {}
  window.open(url, "_blank", "noopener,noreferrer");
}

document.addEventListener("error", event => {
  const target = event.target;
  if (!(target instanceof HTMLImageElement)) return;
  if (target.dataset.fallbackApplied === "1") return;
  target.dataset.fallbackApplied = "1";
  target.src = imgSrc("", target.alt || target.dataset.fallbackTitle || "Anime");
}, true);

els.descToggle.addEventListener("click", () => {
  const isCollapsed = els.animeDesc.classList.contains("coll");
  els.animeDesc.classList.toggle("coll", !isCollapsed);
  els.animeDesc.classList.toggle("exp", isCollapsed);
  els.descToggle.textContent = isCollapsed ? "Ver menos ▴" : "Ver mais ▾";
});

els.videoPlayer.addEventListener("click", () => {
  if (state.currentPage === "player" && !document.fullscreenElement && !state.cssFsActive) {
    if (state.cinemaModeActive) exitCinemaMode();
    else enterCinemaMode();
  }
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.cssFsActive) exitCssFullscreen();
});

els.cssFsExit.addEventListener("click", exitCssFullscreen);
els.videoPlayer.addEventListener("dblclick", requestFullscreen);
document.addEventListener("fullscreenchange", _onNativeFsChange);
document.addEventListener("webkitfullscreenchange", _onNativeFsChange);
window.addEventListener("orientationchange", () => {
  if (!state.cssFsActive) return;
  setTimeout(() => {
    const isPortrait = window.screen.height > window.screen.width;
    els.videoContainer.classList.toggle("portrait-rotate", isPortrait);
  }, 200);
});

document.addEventListener("click", async event => {
  const heroDot = event.target.closest("[data-hidx]");
  if (heroDot) {
    const index = parseInt(heroDot.dataset.hidx || "", 10);
    if (!Number.isNaN(index)) {
      clearHeroTimer();
      renderHeroBanner(index);
      startHeroTimer();
    }
    return;
  }

  const continueCard = event.target.closest("[data-resume-anime]");
  if (continueCard) {
    await openEpisode(continueCard.dataset.resumeAnime, continueCard.dataset.resumeEp, state.currentQuality || "HD");
    return;
  }

  const directCard = event.target.closest(".anime-card[data-open-direct]");
  if (directCard) {
    await openEpisode(directCard.dataset.animeId, directCard.dataset.episode, state.currentQuality || "HD");
    return;
  }

  const animeCard = event.target.closest(".anime-card[data-anime-id]:not([data-open-direct])");
  if (animeCard) {
    await openAnime(animeCard.dataset.animeId);
    return;
  }

  const sectionButton = event.target.closest("[data-open-section]");
  if (sectionButton) {
    await loadSectionResults(sectionButton.dataset.openSection);
    return;
  }

  const retryHomeButton = event.target.closest("[data-retry-home]");
  if (retryHomeButton) {
    await loadHome();
    return;
  }

  const episodeButton = event.target.closest("[data-open-episode]");
  if (episodeButton) {
    await openEpisode(state.currentAnimeId, episodeButton.dataset.openEpisode, state.currentQuality || "HD");
    return;
  }

  const qualityButton = event.target.closest("[data-quality]");
  if (qualityButton) {
    await openEpisode(state.currentAnimeId, state.currentEpisode, qualityButton.dataset.quality, { pushHistory: false, force: true });
    return;
  }

  const genreToggle = event.target.closest("#genreToggleBtn");
  if (genreToggle) {
    state.genreExpanded = !state.genreExpanded;
    renderGenreTags(state.anime?.genres || [], state.genreExpanded);
    return;
  }

  const genreTag = event.target.closest("[data-genre-search]");
  if (genreTag) {
    els.searchInput.value = genreTag.dataset.genreSearch;
    els.searchClear.style.display = "block";
    await performSearch(genreTag.dataset.genreSearch);
    return;
  }

  const suggestion = event.target.closest("[data-suggest-search]");
  if (suggestion) {
    els.searchInput.value = suggestion.dataset.suggestSearch;
    els.searchClear.style.display = "block";
    await performSearch(suggestion.dataset.suggestSearch);
    return;
  }

  const filterChip = event.target.closest("[data-section]");
  if (filterChip) {
    await loadSectionResults(filterChip.dataset.section);
  }
});

els.searchInput.addEventListener("keydown", async event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  await performSearch(els.searchInput.value);
});

let searchDebounce = null;
els.searchInput.addEventListener("input", () => {
  const value = els.searchInput.value.trim();
  els.searchClear.style.display = value ? "block" : "none";
  clearTimeout(searchDebounce);
  if (value.length >= 2 && state.currentPage === "results" && state.resultsMode === "search") {
    searchDebounce = setTimeout(() => {
      performSearch(value, 1, { silent: true, pushHistory: false });
    }, 550);
  }
});

els.searchClear.addEventListener("click", async () => {
  els.searchInput.value = "";
  els.searchClear.style.display = "none";
  els.searchInput.focus();
  if (state.currentPage === "results" && state.resultsMode === "search") {
    setPage("home");
    updateUrlParams({}, { mode: "push" });
    if (!state.home) await loadHome();
  }
});

els.resultsBackBtn.addEventListener("click", () => goBack());
els.backBtn.addEventListener("click", () => goBack());

async function goHome(push = true) {
  if (state.cssFsActive) exitCssFullscreen();
  exitCinemaMode();
  clearPlayerState();
  setPage("home", push);
  updateUrlParams({}, { mode: push ? "push" : "replace" });
  await loadHome();
}

els.homeBtn.addEventListener("click", async () => {
  await goHome(true);
});

els.refreshBtn.addEventListener("click", async () => {
  const page = state.currentPage;
  if (page === "home") {
    await loadHome();
    return;
  }
  if (page === "results") {
    if (state.resultsMode === "search" && state.searchQuery) await performSearch(state.searchQuery, state.resultsPage, { pushHistory: false });
    else await loadSectionResults(state.currentSection || "dublados", state.resultsPage, { pushHistory: false });
    return;
  }
  if (page === "anime" && state.currentAnimeId) {
    await openAnime(state.currentAnimeId, { pushHistory: false });
    return;
  }
  if (page === "player" && state.currentAnimeId && state.currentEpisode) {
    state.attempt = 0;
    await openEpisode(state.currentAnimeId, state.currentEpisode, state.currentQuality, { pushHistory: false, force: true });
  }
});

els.backToHomeFromAnimeBtn.addEventListener("click", async () => {
  await goHome(true);
});

els.backToAnimeBtn.addEventListener("click", () => {
  exitCinemaMode();
  setPage("anime");
  updateUrlParams({ anime: state.currentAnimeId }, { mode: "push" });
});

els.backToHomeFromPlayerBtn.addEventListener("click", async () => {
  await goHome(true);
});

els.openFirstEpisodeBtn.addEventListener("click", async () => {
  const firstEpisode = getFirstEpisodeNumber(state.animeEpisodes);
  if (!firstEpisode) {
    showToast("Sem episódios disponíveis.");
    return;
  }
  await openEpisode(state.currentAnimeId, firstEpisode, state.currentQuality || "HD");
});

els.goToPlayerBtn.addEventListener("click", async () => {
  if (state.currentEpisode) {
    setPage("player");
    updateUrlParams({ anime: state.currentAnimeId, ep: state.currentEpisode, q: state.currentQuality }, { mode: "push" });
    return;
  }

  const animeWatch = getAnimeWatch(state.currentAnimeId);
  const resumeEpisode = String(animeWatch?.latestEpisode || "").trim();
  if (resumeEpisode) {
    await openEpisode(state.currentAnimeId, resumeEpisode, state.currentQuality || "HD");
    return;
  }

  const firstEpisode = getFirstEpisodeNumber(state.animeEpisodes);
  if (!firstEpisode) {
    showToast("Sem episódio.");
    return;
  }
  await openEpisode(state.currentAnimeId, firstEpisode, state.currentQuality || "HD");
});

els.prevEpisodeBtn.addEventListener("click", async () => {
  const episode = els.prevEpisodeBtn.dataset.episode;
  if (episode) await openEpisode(state.currentAnimeId, episode, state.currentQuality, { pushHistory: false, force: true });
});

els.nextEpisodeBtn.addEventListener("click", async () => {
  const episode = els.nextEpisodeBtn.dataset.episode;
  if (episode) await openEpisode(state.currentAnimeId, episode, state.currentQuality, { pushHistory: false, force: true });
});

els.episodeListBtn.addEventListener("click", () => {
  exitCinemaMode();
  setPage("anime");
  updateUrlParams({ anime: state.currentAnimeId }, { mode: "push" });
  setTimeout(() => {
    $("episodesGrid")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 100);
});

els.openInBrowserBtn.addEventListener("click", () => {
  openExternalLink(els.openInBrowserBtn.dataset.url);
});

els.retryPlayerBtn.addEventListener("click", async () => {
  if (!state.currentAnimeId || !state.currentEpisode) return;
  state.attempt = 0;
  await openEpisode(state.currentAnimeId, state.currentEpisode, state.currentQuality, { pushHistory: false, force: true });
});

els.fullscreenBtn.addEventListener("click", requestFullscreen);

window.addEventListener("popstate", async () => {
  state.autoBootDone = false;
  await bootFromRoute();
});

bindVideoEvents();
connectRealtimeUpdates();
syncTelegramBackButton();
bootFromRoute();
