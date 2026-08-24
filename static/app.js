(function () {
  const STORAGE_TOKEN_KEY = "mobile-terminal.token";
  const STORAGE_USER_KEY = "mobile-terminal.user";
  const STORAGE_DEVICE_ID_KEY = "mobile-terminal.device-id";
  // Tracks which user the device-cached settings belong to. localStorage is
  // per-origin, so when a different user logs in on the same browser we clear
  // the cached prefs and let them load their own per-user settings from the host.
  const STORAGE_SETTINGS_OWNER_KEY = "mobile-terminal.settings-owner";
  const STORAGE_SHORTCUTS_KEY = "mobile-terminal.shortcuts";
  const STORAGE_GESTURES_KEY = "mobile-terminal.gestures";
  const STORAGE_UI_SCALE_KEY = "mobile-terminal.ui-scale";
  const STORAGE_TERMINAL_FONT_KEY = "mobile-terminal.terminal-font";
  const STORAGE_ACTIVE_SESSION_KEY = "mobile-terminal.active-session";
  const STORAGE_OPEN_TABS_KEY = "mobile-terminal.open-tabs";
  const STORAGE_ACTIVE_PROFILE_KEY = "mobile-terminal.active-profile";
  const STORAGE_PROFILE_PREFIX = "mobile-terminal.profile.";
  const STORAGE_PASSKEY_AUTH_MODE_KEY = "mobile-terminal.passkey-auth-mode";
  const STORAGE_PASSKEY_IDLE_MINUTES_KEY = "mobile-terminal.passkey-idle-minutes";
  const STORAGE_PASSKEY_BACKGROUNDED_AT_KEY = "mobile-terminal.passkey-backgrounded-at";
  const STORAGE_EDITOR_TABS_KEY = "mobile-terminal.editor-tabs";
  const STORAGE_BTOP_ZOOM_KEY = "mobile-terminal.btop-zoom";
  const BTOP_SESSION_PREFIX = "btop-";
  // Grid btop needs to render its default layout without "terminal too small".
  // btop reports needing ~80x32 for a full box config; we target a little above
  // that (and lose one col to the fit guard) so it always renders. The tab's
  // font is auto-shrunk until at least this many cells fit; the zoom slider
  // multiplies the target up (more cells, smaller text) to zoom out.
  const BTOP_TARGET_COLS = 84;
  const BTOP_TARGET_ROWS = 34;
  const BTOP_MIN_FONT = 3;
  const DEFAULT_UI_SCALE = 0.85;
  const DEFAULT_TERMINAL_FONT = 10;
  const DEFAULT_AUTHENTICATION_MODE = "every-open";
  const DEFAULT_AUTHENTICATION_IDLE_MINUTES = 15;
  const AUTHENTICATION_MODES = new Set(["off", "idle", "every-open"]);
  const KEYBOARD_THRESHOLD = 80;
  const UI_SCALE_FIT_WIDTH = 430;
  const UI_SCALE_FIT_HEIGHT = 700;
  const EDITOR_TAB_PREFIX = "editor:";
  const FILE_REQUEST_TIMEOUT_MS = 8000;
  const FILE_BOOKMARK_LIMIT = 40;
  const ACTIVITY_REPORT_INTERVAL_MS = 1000;
  const FORCED_ACTIVITY_DEDUPE_MS = 250;
  let decoder = new TextDecoder();
  const defaultShortcuts = [
    { label: "Esc", sequence: "{ESC}", visible: true },
    { label: "📋", sequence: "{PASTE}", visible: true },
    { label: "Copy", sequence: "{COPY}", visible: true },
    { label: "Tab", sequence: "{TAB}", visible: true },
    { label: "⬆️", sequence: "{UP}", visible: true },
    { label: "⬇️", sequence: "{DOWN}", visible: true },
    { label: "⬅️", sequence: "{LEFT}", visible: false },
    { label: "➡️", sequence: "{RIGHT}", visible: false },
    { label: "^+C", sequence: "{CTRL+C}", visible: true },
    { label: "Ctrl+L", sequence: "{CTRL+L}", visible: false },
    { label: "Ctrl+R", sequence: "{CTRL+R}", visible: false },
    { label: "Ctrl+X Tab", sequence: "{CTRL+X}{TAB}", visible: false },
    { label: "Shift+Tab", sequence: "{SHIFT+TAB}", visible: false },
    { label: "↩️", sequence: "{ENTER}", visible: true },
    { label: "▶️", sequence: "{TEXT:/resume}{ENTER}", visible: true },
  ];

  // Multi-touch gesture catalog. Each entry is a slot the user can bind to a key
  // sequence (same {..} syntax as shortcut buttons, plus {FONT+}/{FONT-} to zoom)
  // in Settings → Gestures. Order here is the order shown in the editor.
  const GESTURE_DEFS = [
    { id: "swipe2-up", group: "Two-finger swipe", label: "Swipe up", default: "{UP}" },
    { id: "swipe2-down", group: "Two-finger swipe", label: "Swipe down", default: "{DOWN}" },
    { id: "swipe2-left", group: "Two-finger swipe", label: "Swipe left", default: "{LEFT}" },
    { id: "swipe2-right", group: "Two-finger swipe", label: "Swipe right", default: "{RIGHT}" },
    { id: "tap2-single", group: "Two-finger tap", label: "Single tap", default: "" },
    { id: "tap2-double", group: "Two-finger tap", label: "Double tap", default: "{ENTER}" },
    { id: "tap2-triple", group: "Two-finger tap", label: "Triple tap", default: "" },
    { id: "pinch-out", group: "Pinch / zoom", label: "Pinch out (spread apart)", default: "{FONT+}" },
    { id: "pinch-in", group: "Pinch / zoom", label: "Pinch in (together)", default: "{FONT-}" },
    { id: "swipe3-up", group: "Three-finger swipe", label: "Swipe up", default: "" },
    { id: "swipe3-down", group: "Three-finger swipe", label: "Swipe down", default: "" },
    { id: "swipe3-left", group: "Three-finger swipe", label: "Swipe left", default: "" },
    { id: "swipe3-right", group: "Three-finger swipe", label: "Swipe right", default: "" },
    { id: "tap3-single", group: "Three-finger tap", label: "Single tap", default: "" },
    { id: "tap3-double", group: "Three-finger tap", label: "Double tap", default: "" },
  ];
  const GESTURE_DEF_BY_ID = new Map(GESTURE_DEFS.map((def) => [def.id, def]));
  const specialMap = {
    TAB: "\t",
    ENTER: "\r",
    ESC: "\u001b",
    SPACE: " ",
    BACKSPACE: "\u007f",
    BACKTAB: "\u001b[Z",
    "SHIFT+TAB": "\u001b[Z",
    DELETE: "\u001b[3~",
    UP: "\u001b[A",
    DOWN: "\u001b[B",
    RIGHT: "\u001b[C",
    LEFT: "\u001b[D",
    HOME: "\u001b[H",
    END: "\u001b[F",
    PGUP: "\u001b[5~",
    PGDN: "\u001b[6~",
  };

  const tabsStrip = document.getElementById("tabsStrip");
  const tabsScroller = document.getElementById("tabsScroller");
  const shortcutBar = document.getElementById("shortcutBar");
  const shortcutsPanel = document.getElementById("shortcutsPanel");
  const composerPanel = document.getElementById("composerPanel");
  const composerInput = document.getElementById("composerInput");
  const clearComposerButton = document.getElementById("clearComposerButton");
  const loginOverlay = document.getElementById("loginOverlay");
  const loginForm = document.getElementById("loginForm");
  const userInput = document.getElementById("userInput");
  const userField = document.getElementById("userField");
  const tokenInput = document.getElementById("tokenInput");
  const tokenFieldLabel = document.getElementById("tokenFieldLabel");
  const loginSubmitButton = document.getElementById("loginSubmitButton");
  const loginMessage = document.getElementById("loginMessage");
  const accountButton = document.getElementById("accountButton");
  const accountOverlay = document.getElementById("accountOverlay");
  const accountUserLabel = document.getElementById("accountUserLabel");
  const accountHelperText = accountOverlay?.querySelector(".helper-text");
  const deviceList = document.getElementById("deviceList");
  const rotateTokenButton = document.getElementById("rotateTokenButton");
  const signOutButton = document.getElementById("signOutButton");
  const closeAccountButton = document.getElementById("closeAccountButton");
  const toast = document.getElementById("toast");
  const tabMenu = document.getElementById("tabMenu");
  const auxButton = document.getElementById("auxButton");
  const auxMenu = document.getElementById("auxMenu");
  const auxSessionsButton = document.getElementById("auxSessionsButton");
  const auxFilesButton = document.getElementById("auxFilesButton");
  const auxBtopButton = document.getElementById("auxBtopButton");
  const btopTargetMenu = document.getElementById("btopTargetMenu");
  const btopControls = document.getElementById("btopControls");
  const btopZoom = document.getElementById("btopZoom");
  const btopZoomInput = document.getElementById("btopZoomInput");
  const sessionMenu = document.getElementById("sessionMenu");
  const profileBanner = document.getElementById("profileBanner");
  const profileButton = document.getElementById("profileButton");
  const profileDot = document.getElementById("profileDot");
  const profileLabel = document.getElementById("profileLabel");
  const profileStatus = document.getElementById("profileStatus");
  const profileMenu = document.getElementById("profileMenu");
  const settingsButton = document.getElementById("settingsButton");
  const settingsMenu = document.getElementById("settingsMenu");
  const editorOverlay = document.getElementById("editorOverlay");
  const shortcutEditorList = document.getElementById("shortcutEditorList");
  const gestureOverlay = document.getElementById("gestureOverlay");
  const gestureEditorList = document.getElementById("gestureEditorList");
  const displayOverlay = document.getElementById("displayOverlay");
  const authenticationButton = document.getElementById("authenticationButton");
  const authenticationOverlay = document.getElementById("authenticationOverlay");
  const authenticationModeInput = document.getElementById("authenticationModeInput");
  const authenticationIdleControl = document.getElementById("authenticationIdleControl");
  const authenticationIdleInput = document.getElementById("authenticationIdleInput");
  const usageOverlay = document.getElementById("usageOverlay");
  const usageMetaLabel = document.getElementById("usageMeta");
  const usageStats = document.getElementById("usageStats");
  const usageRangeGroup = document.getElementById("usageRangeGroup");
  const usageDailyChart = document.getElementById("usageDailyChart");
  const usageDailyChartTitle = document.getElementById("usageDailyChartTitle");
  const usageDailyEmpty = document.getElementById("usageDailyEmpty");
  const usageHourChart = document.getElementById("usageHourChart");
  const usageHourNote = document.getElementById("usageHourNote");
  const usageBreakdown = document.getElementById("usageBreakdown");
  const usageBreakdownTitle = document.getElementById("usageBreakdownTitle");
  const usageViewToggle = document.getElementById("usageViewToggle");
  const usageEmpty = document.getElementById("usageEmpty");
  let usageView = "daily";
  let usageRange = "30d";
  let lastUsagePayload = null;
  let usageRequestTimer = null;
  const uiScaleInput = document.getElementById("uiScaleInput");
  const terminalFontInput = document.getElementById("terminalFontInput");
  const uiScaleValue = document.getElementById("uiScaleValue");
  const terminalFontValue = document.getElementById("terminalFontValue");
  const displayPreview = document.getElementById("displayPreview");
  const displayUiPreview = document.getElementById("displayUiPreview");
  const displayTerminalPreview = document.getElementById("displayTerminalPreview");
  const terminalPanel = document.getElementById("terminalPanel");
  const terminalElement = document.getElementById("terminal");
  const fileWorkspace = document.getElementById("fileWorkspace");
  const fileWorkspaceRoot = document.getElementById("fileWorkspaceRoot");
  const fileWorkspaceTitle = document.getElementById("fileWorkspaceTitle");
  const fileTreePanel = document.getElementById("fileTreePanel");
  const fileTreeScrim = document.getElementById("fileTreeScrim");
  const fileTree = document.getElementById("fileTree");
  const fileTreeToggleButton = document.getElementById("fileTreeToggleButton");
  const fileChangeRootButton = document.getElementById("fileChangeRootButton");
  const fileRefreshButton = document.getElementById("fileRefreshButton");
  const fileSaveButton = document.getElementById("fileSaveButton");
  const filePathLabel = document.getElementById("filePathLabel");
  const fileStatus = document.getElementById("fileStatus");
  const fileEditorTabs = document.getElementById("fileEditorTabs");
  const fileEditorInput = document.getElementById("fileEditorInput");
  const fileMarkdownToggleButton = document.getElementById("fileMarkdownToggleButton");
  const fileMarkdownPreview = document.getElementById("fileMarkdownPreview");
  const fileRootOverlay = document.getElementById("fileRootOverlay");
  const fileRootForm = document.getElementById("fileRootForm");
  const fileRootInput = document.getElementById("fileRootInput");
  const fileRootMessage = document.getElementById("fileRootMessage");
  const fileBookmarkList = document.getElementById("fileBookmarkList");
  const fileBookmarkButton = document.getElementById("fileBookmarkButton");
  const useHomeRootButton = document.getElementById("useHomeRootButton");

  let serverConfig = {
    requireToken: true,
    tailscaleMode: false,
    allowedClients: [],
    multiTenant: false,
    profileMode: false,
    profiles: [],
    deviceKeyAuth: false,
    passkeyAuth: false,
  };
  let profiles = [];
  let activeProfileId = localStorage.getItem(STORAGE_ACTIVE_PROFILE_KEY) || "";
  let pendingProfileId = "";
  let waitingForProxyAuth = false;
  let loginRealm = "";
  let profileMenuOpen = false;
  let currentUser = localStorage.getItem(STORAGE_USER_KEY) || "";
  let currentUserLabel = "";
  let authenticationByRealm = {};
  let hostAuthenticationDefault = normalizeAuthenticationSettings(null);
  let authenticationSettings = normalizeAuthenticationSettings(null);
  let draftAuthenticationSettings = normalizeAuthenticationSettings(null);
  let draftAuthenticationRealm = "";
  let draftAuthenticationScope = "";
  let passkeyRequiredScope = "";
  let passkeyRetryPending = false;
  let passkeyCeremonyController = null;
  let resumeDecisionPromise = null;
  let resumeHandlingReady = false;
  let initialResumeDecisionMade = false;
  let handledResumeMarker = "";
  let backgroundRecordedScope = "";
  let terminalReadyWhileHidden = false;

  function normalizeAuthenticationSettings(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const mode = AUTHENTICATION_MODES.has(source.mode)
      ? source.mode
      : DEFAULT_AUTHENTICATION_MODE;
    const rawIdleMinutes = source.idleMinutes;
    let parsedIdleMinutes = DEFAULT_AUTHENTICATION_IDLE_MINUTES;
    if (typeof rawIdleMinutes === "number" && Number.isSafeInteger(rawIdleMinutes)) {
      parsedIdleMinutes = rawIdleMinutes;
    } else if (
      typeof rawIdleMinutes === "string" &&
      /^[+-]?\d+$/.test(rawIdleMinutes.trim())
    ) {
      parsedIdleMinutes = Number(rawIdleMinutes);
      if (!Number.isSafeInteger(parsedIdleMinutes)) {
        parsedIdleMinutes = DEFAULT_AUTHENTICATION_IDLE_MINUTES;
      }
    }
    const idleMinutes = Math.min(1440, Math.max(1, parsedIdleMinutes));
    return { mode, idleMinutes };
  }

  function authenticationScope(realm = loginRealm) {
    if (realm) {
      return `realm:${realm}`;
    }
    if (serverConfig.multiTenant) {
      return `user:${currentUser || localStorage.getItem(STORAGE_USER_KEY) || "unknown"}`;
    }
    return "standalone";
  }

  function authenticationStorageKey(base, realm = loginRealm) {
    const scope = authenticationScope(realm);
    return scope === "standalone" ? base : `${base}.${encodeURIComponent(scope)}`;
  }

  function removeAuthenticationStorage() {
    const bases = [
      STORAGE_PASSKEY_AUTH_MODE_KEY,
      STORAGE_PASSKEY_IDLE_MINUTES_KEY,
      STORAGE_PASSKEY_BACKGROUNDED_AT_KEY,
    ];
    try {
      const keys = [];
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (key && bases.some((base) => key === base || key.startsWith(`${base}.`))) {
          keys.push(key);
        }
      }
      keys.forEach((key) => localStorage.removeItem(key));
    } catch (_error) {
      // User-switch cleanup is best-effort when browser storage is unavailable.
    }
  }

  function hostAuthenticationSettings(realm = loginRealm) {
    if (realm && authenticationByRealm[realm]) {
      return normalizeAuthenticationSettings(authenticationByRealm[realm]);
    }
    return normalizeAuthenticationSettings(hostAuthenticationDefault);
  }

  function loadAuthenticationSettings(realm = loginRealm) {
    const defaults = hostAuthenticationSettings(realm);
    try {
      return normalizeAuthenticationSettings({
        mode:
          localStorage.getItem(authenticationStorageKey(STORAGE_PASSKEY_AUTH_MODE_KEY, realm)) ??
          defaults.mode,
        idleMinutes:
          localStorage.getItem(authenticationStorageKey(STORAGE_PASSKEY_IDLE_MINUTES_KEY, realm)) ??
          defaults.idleMinutes,
      });
    } catch (_error) {
      return defaults;
    }
  }

  function syncAuthenticationControls(settings = draftAuthenticationSettings) {
    if (authenticationModeInput) {
      authenticationModeInput.value = settings.mode;
    }
    if (authenticationIdleInput) {
      authenticationIdleInput.value = String(settings.idleMinutes);
    }
    if (authenticationIdleControl) {
      authenticationIdleControl.classList.toggle("hidden", settings.mode !== "idle");
    }
  }

  function updateAuthenticationDraft(settings, realm = loginRealm) {
    draftAuthenticationSettings = normalizeAuthenticationSettings(settings);
    draftAuthenticationRealm = realm;
    draftAuthenticationScope = authenticationScope(realm);
    syncAuthenticationControls();
  }

  function applyAuthenticationScope(realm = loginRealm) {
    const scope = authenticationScope(realm);
    authenticationSettings = loadAuthenticationSettings(realm);
    const draftIsOpen = authenticationOverlay && !authenticationOverlay.classList.contains("hidden");
    if (!draftIsOpen || draftAuthenticationScope !== scope) {
      updateAuthenticationDraft(authenticationSettings, realm);
    }
  }

  function cancelPasskeyCeremony() {
    if (passkeyCeremonyController) {
      passkeyCeremonyController.abort();
      passkeyCeremonyController = null;
    }
  }

  function resetAuthenticationLifecycle({ locked = true } = {}) {
    cancelPasskeyCeremony();
    authenticationByRealm = {};
    hostAuthenticationDefault = normalizeAuthenticationSettings(null);
    authenticationSettings = normalizeAuthenticationSettings(null);
    draftAuthenticationSettings = normalizeAuthenticationSettings(null);
    draftAuthenticationRealm = "";
    draftAuthenticationScope = "";
    passkeyRequiredScope = "";
    passkeyRetryPending = false;
    resumeDecisionPromise = null;
    initialResumeDecisionMade = false;
    handledResumeMarker = "";
    backgroundRecordedScope = "";
    terminalReadyWhileHidden = false;
    hasDeviceKey = false;
    setPasskeyRetryUi(false);
    setPasskeyLocked(locked);
    applyAuthenticationScope();
  }

  // A stable per-device id so the host can list the devices registered to a
  // user. Generated once and kept in localStorage; not a secret.
  function getDeviceId() {
    let id = localStorage.getItem(STORAGE_DEVICE_ID_KEY);
    if (!id) {
      id =
        (window.crypto && window.crypto.randomUUID && window.crypto.randomUUID()) ||
        `dev-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e9).toString(36)}`;
      localStorage.setItem(STORAGE_DEVICE_ID_KEY, id);
    }
    return id;
  }

  // --- device-bound key (silent auth) -------------------------------------
  // A non-extractable ECDSA P-256 key pair lives in IndexedDB. JS can sign with
  // the private key but can never read it out, so unlike a stored token it can't
  // be copied to another device; to connect we just sign the server's nonce.
  const DEVICE_KEY_DB = "mobile-terminal-keys";
  const DEVICE_KEY_STORE = "keys";
  const DEVICE_KEY_ID_PREFIX = "device-ecdsa";
  const DEVICE_AUTH_PURPOSE = "mobile-terminal-device-auth-v1";
  const DEVICE_ENROLLMENT_PURPOSE = "mobile-terminal-device-enroll-v1";
  let hasDeviceKey = false; // cached for the active auth scope

  function deviceKeyId(realm = loginRealm) {
    return `${DEVICE_KEY_ID_PREFIX}:${authenticationScope(realm)}`;
  }

  function deviceKeySupported() {
    return !!(window.crypto && window.crypto.subtle && window.indexedDB);
  }
  function idbOpen() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DEVICE_KEY_DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(DEVICE_KEY_STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  function idbRun(mode, fn) {
    return idbOpen().then(
      (db) =>
        new Promise((resolve, reject) => {
          const tx = db.transaction(DEVICE_KEY_STORE, mode);
          const store = tx.objectStore(DEVICE_KEY_STORE);
          const out = fn(store);
          tx.oncomplete = () => resolve(out && out.result !== undefined ? out.result : undefined);
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        }),
    );
  }
  async function loadDeviceKey(realm = loginRealm) {
    if (!deviceKeySupported()) return null;
    try {
      return (await idbRun("readonly", (store) => store.get(deviceKeyId(realm)))) || null;
    } catch (e) {
      return null;
    }
  }
  async function ensureDeviceKey(realm = loginRealm) {
    const scopedId = deviceKeyId(realm);
    let rec = await loadDeviceKey(realm);
    if (rec && rec.privateKey) return rec;
    // extractable:false → private key can never leave the browser; the public
    // key stays exportable so we can register it with the server.
    const pair = await crypto.subtle.generateKey(
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["sign", "verify"],
    );
    rec = { privateKey: pair.privateKey, publicKey: pair.publicKey };
    await idbRun("readwrite", (store) => store.put(rec, scopedId)); // CryptoKey is structured-cloneable
    return rec;
  }
  async function forgetDeviceKey(realm = loginRealm) {
    const keyId = deviceKeyId(realm);
    try {
      await idbRun("readwrite", (store) => store.delete(keyId));
    } catch (e) {}
    hasDeviceKey = false;
  }
  function bytesToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  async function exportPublicSpki(publicKey) {
    return bytesToBase64(await crypto.subtle.exportKey("spki", publicKey));
  }
  function deviceProtocolRealm(realm = loginRealm) {
    if (serverConfig.profileMode) {
      return realm || loginRealm;
    }
    if (serverConfig.multiTenant) {
      return realm || currentUser || localStorage.getItem(STORAGE_USER_KEY) || "";
    }
    return "standalone";
  }

  function deviceTranscript(purpose, fields) {
    const values = [purpose, ...fields];
    if (values.some((value) => typeof value !== "string" || value.includes("\0"))) {
      throw new TypeError("device-key transcript fields must be NUL-free strings");
    }
    return new TextEncoder().encode(values.join("\0"));
  }

  function deviceAuthenticationTranscript(rpId, realm, profile, nonce) {
    return deviceTranscript(DEVICE_AUTH_PURPOSE, [rpId, realm, profile, nonce]);
  }

  function deviceEnrollmentTranscript(
    rpId,
    realm,
    profile,
    enrollmentId,
    nonce,
    deviceId,
    publicKey,
  ) {
    return deviceTranscript(DEVICE_ENROLLMENT_PURPOSE, [
      rpId,
      realm,
      profile,
      enrollmentId,
      nonce,
      deviceId,
      publicKey,
    ]);
  }

  async function signDeviceTranscript(privateKey, transcript) {
    const signature = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      privateKey,
      transcript,
    );
    return bytesToBase64(signature); // raw r‖s (64 bytes); the server converts to DER
  }
  async function refreshDeviceKeyFlag(realm = loginRealm) {
    hasDeviceKey = !!(await loadDeviceKey(realm));
    return hasDeviceKey;
  }

  let uiScale = loadNumericSetting(STORAGE_UI_SCALE_KEY, DEFAULT_UI_SCALE, 0.5, 1.4);
  let effectiveUiScale = uiScale;
  let terminalFontSize = loadNumericSetting(STORAGE_TERMINAL_FONT_KEY, DEFAULT_TERMINAL_FONT, 5, 24);
  let btopZoomFactor = loadNumericSetting(STORAGE_BTOP_ZOOM_KEY, 1, 1, 3);
  let draftUiScale = uiScale;
  let draftTerminalFontSize = terminalFontSize;
  document.documentElement.style.setProperty("--ui-scale", String(effectiveUiScale));
  document.documentElement.style.setProperty("--terminal-font-size", `${terminalFontSize}px`);

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: terminalFontSize,
    lineHeight: 1.2,
    // Deep local scrollback so shell/codex panes (which scroll locally, 0 RTT)
    // stay scrollable far back without round-tripping to a distant server.
    scrollback: 20000,
    theme: {
      background: "#08131a",
      foreground: "#e6edf3",
      cursor: "#ffd166",
      selectionBackground: "rgba(255, 209, 102, 0.38)",
      black: "#0b1318",
      red: "#ff6b6b",
      green: "#86efac",
      yellow: "#ffd166",
      blue: "#82cfff",
      magenta: "#ff9bd2",
      cyan: "#67e8f9",
      white: "#ecf5ff",
      brightBlack: "#5f6e7c",
      brightRed: "#ff8c82",
      brightGreen: "#b1f29d",
      brightYellow: "#ffe08a",
      brightBlue: "#a5ddff",
      brightMagenta: "#ffc2e6",
      brightCyan: "#a7f3ff",
      brightWhite: "#ffffff",
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  // Snapshots a tab's rendered buffer so switching back to it paints instantly
  // (0 round-trips) from the last-seen state, then the live re-attach reconciles.
  const serializeAddon =
    typeof SerializeAddon !== "undefined" ? new SerializeAddon.SerializeAddon() : null;
  if (serializeAddon) {
    term.loadAddon(serializeAddon);
  }
  // session name -> serialized screen (+ limited scrollback). Bounded LRU.
  const sessionSnapshots = new Map();
  const SESSION_SNAPSHOT_MAX = 12;
  const SESSION_SNAPSHOT_SCROLLBACK = 1000;

  function sessionSnapshotKey(sessionName, profileId = activeProfileId) {
    return `${profileId || "single"}:${sessionName}`;
  }

  function snapshotActiveSession() {
    // Only snapshot normal-buffer panes (shell/codex). Alt-screen TUIs (claude,
    // pagers) keep their view in the alternate buffer, which the serializer
    // doesn't capture — snapshotting them would restore stale scrollback and
    // land scrolled up. Those tabs re-attach cleanly on their full-screen redraw.
    if (!serializeAddon || !activeSessionName || !activePaneLocalScroll) {
      return;
    }
    try {
      const data = serializeAddon.serialize({ scrollback: SESSION_SNAPSHOT_SCROLLBACK });
      const snapshotKey = sessionSnapshotKey(activeSessionName);
      sessionSnapshots.delete(snapshotKey);
      sessionSnapshots.set(snapshotKey, data);
      while (sessionSnapshots.size > SESSION_SNAPSHOT_MAX) {
        sessionSnapshots.delete(sessionSnapshots.keys().next().value);
      }
    } catch (_error) {
      // Serialization is best-effort; a miss just costs one round-trip.
    }
  }

  term.open(terminalElement);
  installSemanticPromptHandlers();

  let socket = null;
  let socketMessageChain = Promise.resolve();
  let pendingTerminalOutput = null;
  let pendingTerminalSeed = null;
  let terminalEpoch = 0;
  let terminalPaneId = "";
  let terminalCutoff = 0;
  let terminalRevision = 0;
  let terminalLayoutGeneration = 0;
  let terminalSeedHistory = 0;
  let terminalHistory = 0;
  let terminalAuthoritative = false;
  let historyReseedPending = false;
  let pendingSeedScrollTarget = null;
  let selectionRequestCounter = 0;
  const pendingSelectionRequests = new Map();
  let reconnectTimer = null;
  let resumeProbeTimer = null;
  let socketConnectStartedAt = 0;
  let lastServerMessageAt = 0;
  let lastActivityReportAt = -Infinity;
  let lastForcedActivityReportAt = -Infinity;
  let fitTimer = null;
  let terminalFitScheduled = false;
  let pendingFitPreserveCols = false;
  let lastTerminalCols = 0;
  let lastTerminalRows = 0;
  let lastTerminalLayoutWidth = 0;
  let lastComposerHeight = 0;
  let lastLayoutViewportWidth = 0;
  let viewportSettleTimers = [];
  let lastStableViewportWidth = 0;
  let lastStableViewportHeight = 0;
  let currentTabs = [];
  let shortcuts = loadShortcuts();
  let gestureBindings = loadGestures();
  let openTabMenuKey = null;
  let currentSessions = [];
  let openTabNames = loadOpenTabs();
  let editorTabs = loadEditorTabs();
  const editorTabsByProfile = new Map([[activeProfileId || "single", editorTabs]]);
  let fileBookmarks = [];
  let fileBookmarksByProfile = {};
  let selectedSessionName = loadActiveSession();
  let activeTabKey = selectedSessionName ? terminalTabKey(selectedSessionName) : "";
  let activeSessionName = "";
  // The tab the user was on before the current one — used by the selection's
  // "To tab" chip to send a command to the most recent *other* tab.
  let previousSessionName = "";
  // When the "To tab" chip switches sessions, the paste is deferred until the
  // destination is ready: { session, text, ready }. Mobile composer delivery
  // waits for that session's composer-state so an existing draft is preserved.
  let pendingPasteAfterSwitch = null;
  let fileRequestCounter = 0;
  let pendingFileRequests = new Map();
  let lastDefaultFileRoot = "";
  let followOutput = true;
  // During a tab switch, pin the view to the bottom until the re-attach settles
  // (so a snapshot restore / redraw can't leave it scrolled up). A real user
  // scroll clears it. Timestamp (performance.now) until which the pin holds.
  let bottomPinUntil = 0;
  // When true, the active pane is a normal-buffer app (shell, codex, ...) whose
  // transcript lives in xterm's own buffer, so we scroll locally with no server
  // round-trip. Default true until the server reports that the active pane owns
  // scrolling through mouse tracking or an alternate screen.
  let activePaneLocalScroll = true;
  let reconnectForSessionSwitch = false;
  let authConfigPollTimer = 0;
  let hostSettingsReady = false;
  let sessionMenuOpen = false;
  let settingsMenuOpen = false;
  let auxMenuOpen = false;
  let btopTargetMenuOpen = false;
  let btopTargetRefreshTimer = null;
  let btopTargets = [{ id: "local", label: "Local (this computer)" }];
  // Non-persisted per-view state for the active btop tab.
  let btopMode = false;
  let touchScrollState = null;
  let gestureState = null;
  let gestureTap = null;
  let touchInertiaFrameId = null;
  let touchInertiaVelocity = 0;
  let touchInertiaLastAt = 0;
  let scrollLineRemainder = 0;
  let tabDragState = null;
  let suppressTabClickUntil = 0;
  let shortcutDragState = null;
  let suppressShortcutClickUntil = 0;
  let speechInputState = {
    lastPhrase: "",
    lastAt: 0,
  };
  let speechFlushTimer = null;
  const mobileComposerMode = window.matchMedia("(pointer: coarse)").matches || navigator.maxTouchPoints > 0;
  let suppressComposerSync = false;
  let composerRevision = 0;
  let latestAppliedComposerRevision = 0;
  let semanticPromptState = {
    seenMarker: false,
    commandActive: false,
    commandStart: null,
    syncScheduled: false,
    forceSync: false,
    lastSignature: "",
  };
  let terminalBufferSyncState = {
    pending: false,
    syncScheduled: false,
    forceSync: false,
    lastSignature: "",
    timers: [],
  };
  const TOUCH_INERTIA_MIN_VELOCITY = 0.02;
  const TOUCH_INERTIA_MAX_VELOCITY = 3;
  const TOUCH_INERTIA_FRICTION_PER_MS = 0.9965;
  const TOUCH_INERTIA_MAX_FRAME_MS = 34;
  const TOUCH_VELOCITY_BLEND = 0.35;
  // Multi-touch gesture recognizer tunables.
  const GESTURE_MOVE_SLOP = 16; // px a finger may drift within a pulse and still count as a tap
  const GESTURE_TAP_MAX_MS = 340; // a single tap pulse must be quicker than this
  const GESTURE_MULTITAP_GAP_MS = 340; // max gap between taps of a multi-tap
  const GESTURE_SWIPE_START = 26; // px of travel before a swipe commits + locks its axis
  const GESTURE_SWIPE_STEP = 30; // px per repeat for continuous (arrow-key) swipes
  const GESTURE_PINCH_START = 46; // px of spread change before a pinch commits
  const GESTURE_PINCH_STEP = 55; // px of spread per pinch repeat step
  const GESTURE_MAX_REPEAT = 6; // cap repeats per move event (anti-flood)
  const SHORTCUT_REPEAT_DELAY_MS = 320;
  const SHORTCUT_REPEAT_INTERVAL_MS = 70;
  const TERMINAL_BUFFER_SYNC_DELAYS_MS = [20, 60, 120, 220];
  const TERMINAL_COL_GUARD = 1;
  const EXTRACTOR_DEBUG_ENABLED = false;
  const BOX_VERTICAL_CHARS = new Set(["│", "┃", "║", "|"]);
  const BOX_HORIZONTAL_CHARS = new Set(["─", "━", "═", "-"]);
  const BOX_TOP_LEFT_CHARS = new Set(["╭", "┌", "╔", "+"]);
  const BOX_TOP_RIGHT_CHARS = new Set(["╮", "┐", "╗", "+"]);
  const BOX_BOTTOM_LEFT_CHARS = new Set(["╰", "└", "╚", "+"]);
  const BOX_BOTTOM_RIGHT_CHARS = new Set(["╯", "┘", "╝", "+"]);

  function loadNumericSetting(storageKey, fallback, min, max) {
    const raw = Number.parseFloat(localStorage.getItem(storageKey) || "");
    if (!Number.isFinite(raw)) {
      return fallback;
    }
    return Math.min(max, Math.max(min, raw));
  }

  function normalizeShortcut(shortcut) {
    if (!shortcut || !shortcut.label || !shortcut.sequence) {
      return null;
    }
    return {
      label: String(shortcut.label).trim(),
      sequence: String(shortcut.sequence).trim(),
      visible: shortcut.visible !== false,
    };
  }

  function resetSemanticPromptState() {
    semanticPromptState = {
      seenMarker: false,
      commandActive: false,
      commandStart: null,
      syncScheduled: false,
      forceSync: false,
      lastSignature: "",
    };
  }

  function resetTerminalBufferSyncState() {
    terminalBufferSyncState.timers.forEach((timer) => window.clearTimeout(timer));
    terminalBufferSyncState = {
      pending: false,
      syncScheduled: false,
      forceSync: false,
      lastSignature: "",
      timers: [],
    };
  }

  let extractorDebugPanel = null;

  function ensureExtractorDebugPanel() {
    if (!EXTRACTOR_DEBUG_ENABLED || extractorDebugPanel) {
      return;
    }
    extractorDebugPanel = document.createElement("pre");
    extractorDebugPanel.className = "extractor-debug-panel";
    document.body.appendChild(extractorDebugPanel);
  }

  function formatRowSpans(rowSpans, startY) {
    return rowSpans
      .map((span, index) => {
        if (!span) {
          return `${startY + index}: -`;
        }
        return `${startY + index}: ${span.start}-${span.end}`;
      })
      .join("\n");
  }

  function updateExtractorDebugPanel(debugState) {
    if (!EXTRACTOR_DEBUG_ENABLED || !mobileComposerMode) {
      return;
    }
    ensureExtractorDebugPanel();
    if (!extractorDebugPanel) {
      return;
    }
    if (!debugState) {
      extractorDebugPanel.textContent = "extractor: no state";
      return;
    }
    const lines = [
      `mode: ${debugState.mode || "-"}`,
      `cursor: ${debugState.cursorY ?? "-"},${debugState.cursorX ?? "-"}`,
      `source rows: ${debugState.startY ?? "-"}-${debugState.endY ?? "-"}`,
      `selected rows: ${debugState.firstRowAbs ?? "-"}-${debugState.lastRowAbs ?? "-"}`,
      debugState.box ? `box: t${debugState.box.top} b${debugState.box.bottom} l${debugState.box.left} r${debugState.box.right}` : "box: -",
      "row spans:",
      formatRowSpans(debugState.rowSpans || [], debugState.startY || 0),
      "preview:",
      String(debugState.valuePreview || ""),
    ];
    extractorDebugPanel.textContent = lines.join("\n");
  }

  function loadShortcuts() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_SHORTCUTS_KEY) || "null");
      if (Array.isArray(parsed) && parsed.length) {
        return parsed
          .map(normalizeShortcut)
          .filter((shortcut) => shortcut && shortcut.label && shortcut.sequence);
      }
    } catch (_error) {
      // Ignore bad local storage payloads.
    }
    return defaultShortcuts.map((shortcut) => ({ ...shortcut }));
  }

  // Merge stored gesture bindings over the catalog defaults so new gesture slots
  // (added in later versions) always appear with their default binding.
  function normalizeGestureBindings(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const map = {};
    GESTURE_DEFS.forEach((def) => {
      const entry = source[def.id];
      const hasEntry = entry && typeof entry === "object";
      map[def.id] = {
        sequence:
          hasEntry && typeof entry.sequence === "string" ? entry.sequence : def.default,
        enabled: hasEntry ? entry.enabled !== false : true,
      };
    });
    return map;
  }

  function loadGestures() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_GESTURES_KEY) || "null");
      if (parsed && typeof parsed === "object") {
        return normalizeGestureBindings(parsed);
      }
    } catch (_error) {
      // Ignore bad local storage payloads.
    }
    return normalizeGestureBindings(null);
  }

  function saveGestures(nextBindings) {
    gestureBindings = normalizeGestureBindings(nextBindings);
    localStorage.setItem(STORAGE_GESTURES_KEY, JSON.stringify(gestureBindings));
    saveHostSettings();
  }

  // The bound sequence for a gesture id, or "" when unbound/disabled.
  function gestureBindingSequence(id) {
    const binding = gestureBindings[id];
    if (!binding || binding.enabled === false) {
      return "";
    }
    return binding.sequence || "";
  }

  function tapGestureId(fingers, count) {
    const name = count >= 3 ? "triple" : count === 2 ? "double" : "single";
    return `tap${fingers}-${name}`;
  }

  // Run a gesture's bound sequence: honor the {COPY}/{PASTE}/{FONT+}/{FONT-}
  // action tokens, otherwise send it to the terminal like a shortcut button.
  function dispatchGestureSequence(sequence) {
    const trimmed = String(sequence || "").trim();
    if (!trimmed) {
      return;
    }
    if (trimmed === "{COPY}") {
      copyTerminalSelection();
      return;
    }
    if (trimmed === "{PASTE}") {
      pasteFromClipboard();
      return;
    }
    if (trimmed === "{FONT+}") {
      applyTerminalFontSize(terminalFontSize + 1);
      return;
    }
    if (trimmed === "{FONT-}") {
      applyTerminalFontSize(terminalFontSize - 1);
      return;
    }
    const expanded = expandShortcutSequence(trimmed);
    if (expanded) {
      sendMessage({ type: "input", data: expanded });
    }
  }

  function profileStateKey(name, profileId = activeProfileId) {
    return profileId ? `${STORAGE_PROFILE_PREFIX}${profileId}.${name}` : "";
  }

  function tokenStorageKey(realm = loginRealm) {
    return realm ? `${STORAGE_TOKEN_KEY}.${realm}` : STORAGE_TOKEN_KEY;
  }

  function loadActiveSession(profileId = activeProfileId) {
    const key = profileStateKey("active-session", profileId);
    return localStorage.getItem(key || STORAGE_ACTIVE_SESSION_KEY) || "";
  }

  function loadOpenTabs(profileId = activeProfileId) {
    try {
      const key = profileStateKey("open-tabs", profileId) || STORAGE_OPEN_TABS_KEY;
      const parsed = JSON.parse(localStorage.getItem(key) || "null");
      if (Array.isArray(parsed)) {
        return Array.from(
          new Set(
            parsed
              .map((value) => String(value || "").trim())
              .filter(Boolean),
          ),
        );
      }
    } catch (_error) {
      // Ignore bad local storage payloads.
    }
    return [];
  }

  function terminalTabKey(sessionName) {
    return `terminal:${sessionName}`;
  }

  function editorTabKey(tabId) {
    return `${EDITOR_TAB_PREFIX}${tabId}`;
  }

  function isEditorTabKey(tabKey) {
    return String(tabKey || "").startsWith(EDITOR_TAB_PREFIX);
  }

  function pathBaseName(path) {
    const value = String(path || "").replace(/[\\/]+$/, "");
    const pieces = value.split(/[\\/]/).filter(Boolean);
    return pieces[pieces.length - 1] || value || "Files";
  }

  function normalizeEditorTab(tab) {
    if (!tab || !tab.root) {
      return null;
    }
    const root = String(tab.root || "").trim();
    if (!root) {
      return null;
    }
    const id = String(tab.id || `files-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`);
    const openFiles = Array.isArray(tab.openFiles)
      ? tab.openFiles.map(normalizeOpenFile).filter(Boolean)
      : [];
    if (!openFiles.length && tab.selectedPath) {
      const migratedFile = normalizeOpenFile({
        path: tab.selectedPath,
        name: tab.selectedName || pathBaseName(tab.selectedPath),
        content: tab.content || "",
        originalContent: tab.originalContent || "",
        dirty: tab.dirty === true,
      });
      if (migratedFile) {
        openFiles.push(migratedFile);
      }
    }
    const activeFilePath =
      String(tab.activeFilePath || tab.selectedPath || "").trim() ||
      openFiles[0]?.path ||
      "";
    return {
      id,
      root,
      name: String(tab.name || pathBaseName(root)).trim() || "Files",
      tree: tab.tree && typeof tab.tree === "object" ? tab.tree : {},
      openFiles,
      activeFilePath,
      loadingPath: "",
      error: "",
      treeHidden: tab.treeHidden === true,
    };
  }

  function normalizeOpenFile(file) {
    if (!file || !file.path) {
      return null;
    }
    const path = String(file.path || "").trim();
    if (!path) {
      return null;
    }
    const content = String(file.content || "");
    return {
      path,
      name: String(file.name || pathBaseName(path)).trim() || pathBaseName(path),
      content,
      originalContent: String(file.originalContent ?? content),
      dirty: file.dirty === true,
      loaded: file.loaded === true || Object.prototype.hasOwnProperty.call(file, "content"),
      previewMode: file.previewMode === true,
    };
  }

  function loadEditorTabs(profileId = activeProfileId) {
    try {
      const key = profileStateKey("editor-tabs", profileId) || STORAGE_EDITOR_TABS_KEY;
      const parsed = JSON.parse(localStorage.getItem(key) || "null");
      if (Array.isArray(parsed)) {
        return parsed.map(normalizeEditorTab).filter(Boolean);
      }
    } catch (_error) {
      // Ignore bad local storage payloads.
    }
    return [];
  }

  function persistEditorTabs() {
    const key = profileStateKey("editor-tabs") || STORAGE_EDITOR_TABS_KEY;
    if (!editorTabs.length) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(
      key,
      JSON.stringify(
        editorTabs.map((tab) => ({
          id: tab.id,
          root: tab.root,
          name: tab.name,
          treeHidden: tab.treeHidden === true,
          activeFilePath: tab.activeFilePath || "",
          openFiles: tab.openFiles.map((file) => ({
            path: file.path,
            name: file.name,
          })),
        })),
      ),
    );
  }

  function activeEditorTab() {
    if (!isEditorTabKey(activeTabKey)) {
      return null;
    }
    const tabId = activeTabKey.slice(EDITOR_TAB_PREFIX.length);
    return editorTabs.find((tab) => tab.id === tabId) || null;
  }

  function editorTabById(tabId) {
    return editorTabs.find((tab) => tab.id === tabId) || null;
  }

  function activeOpenFile(tab) {
    if (!tab) {
      return null;
    }
    return tab.openFiles.find((file) => sameFilePath(file.path, tab.activeFilePath)) || tab.openFiles[0] || null;
  }

  function editorHasDirtyFiles(tab) {
    return Boolean(tab?.openFiles?.some((file) => file.dirty));
  }

  function normalizeFilePathForCompare(path) {
    return String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
  }

  function sameFilePath(left, right) {
    return normalizeFilePathForCompare(left) === normalizeFilePathForCompare(right);
  }

  function fileBookmarkName(path) {
    const value = String(path || "").trim();
    const remoteMatch = value.match(/^([^:]+):(.+)$/);
    if (remoteMatch) {
      const remoteName = pathBaseName(remoteMatch[2]);
      return `${remoteMatch[1]}:${remoteName ? ` ${remoteName}` : ""}`.trim();
    }
    return pathBaseName(value);
  }

  function normalizeFileBookmark(bookmark) {
    const rawPath = typeof bookmark === "string" ? bookmark : bookmark?.path;
    const path = String(rawPath || "").trim();
    if (!path) {
      return null;
    }
    const name = String(typeof bookmark === "object" && bookmark ? bookmark.name || "" : "").trim();
    return {
      path,
      name: name || fileBookmarkName(path),
    };
  }

  function normalizeFileBookmarks(bookmarks) {
    if (!Array.isArray(bookmarks)) {
      return [];
    }
    const normalized = [];
    bookmarks.forEach((bookmark) => {
      const nextBookmark = normalizeFileBookmark(bookmark);
      if (!nextBookmark || normalized.some((item) => sameFilePath(item.path, nextBookmark.path))) {
        return;
      }
      normalized.push(nextBookmark);
    });
    return normalized.slice(0, FILE_BOOKMARK_LIMIT);
  }

  function normalizeFileBookmarksByProfile(bookmarksByProfile) {
    if (!bookmarksByProfile || typeof bookmarksByProfile !== "object" || Array.isArray(bookmarksByProfile)) {
      return {};
    }
    return Object.fromEntries(
      Object.entries(bookmarksByProfile).map(([profileId, bookmarks]) => [
        profileId,
        normalizeFileBookmarks(bookmarks),
      ]),
    );
  }

  function activeAuthenticationSettings(realm = activeProfile()?.authRealm || loginRealm) {
    if (!serverConfig.profileMode || !realm) {
      return { authentication: normalizeAuthenticationSettings(authenticationSettings) };
    }
    authenticationByRealm = {
      ...authenticationByRealm,
      [realm]: normalizeAuthenticationSettings(authenticationSettings),
    };
    return { authenticationByRealm };
  }

  function activeFileBookmarkSettings() {
    if (!serverConfig.profileMode || !activeProfileId) {
      return { fileBookmarks };
    }
    fileBookmarksByProfile = {
      ...fileBookmarksByProfile,
      [activeProfileId]: normalizeFileBookmarks(fileBookmarks),
    };
    return { fileBookmarksByProfile };
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]));
  }

  function isMarkdownFile(file) {
    const name = String(file?.path || file?.name || "");
    return /\.(md|markdown|mdown|mkdn)$/i.test(name);
  }

  function safeMarkdownHref(rawHref) {
    const href = String(rawHref || "").trim();
    if (!href) {
      return "";
    }
    const lowerHref = href.toLowerCase();
    if (
      lowerHref.startsWith("http://") ||
      lowerHref.startsWith("https://") ||
      lowerHref.startsWith("mailto:") ||
      href.startsWith("#") ||
      href.startsWith("/") ||
      href.startsWith("./") ||
      href.startsWith("../")
    ) {
      return escapeHtml(href);
    }
    return "";
  }

  function renderMarkdownInline(text) {
    return String(text || "")
      .split(/(`[^`]*`)/g)
      .map((chunk) => {
        if (chunk.startsWith("`") && chunk.endsWith("`")) {
          return `<code>${escapeHtml(chunk.slice(1, -1))}</code>`;
        }
        let html = escapeHtml(chunk);
        html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, href) => {
          const safeHref = safeMarkdownHref(href);
          if (!safeHref) {
            return label;
          }
          return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${label}</a>`;
        });
        html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
        return html;
      })
      .join("");
  }

  function markdownToHtml(markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const output = [];
    let paragraph = [];
    let listType = "";
    let inCodeBlock = false;
    let codeLines = [];

    const closeParagraph = () => {
      if (!paragraph.length) {
        return;
      }
      output.push(`<p>${renderMarkdownInline(paragraph.join(" "))}</p>`);
      paragraph = [];
    };

    const closeList = () => {
      if (!listType) {
        return;
      }
      output.push(`</${listType}>`);
      listType = "";
    };

    const closeCodeBlock = () => {
      output.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      inCodeBlock = false;
      codeLines = [];
    };

    const parseListItem = (value) => String(value || "").match(/^\s{0,3}([-+*]|(\d+)\.)\s+(.+)$/);
    const listItemType = (match) => (match?.[2] ? "ol" : match ? "ul" : "");

    lines.forEach((line, index) => {
      if (/^\s*```/.test(line)) {
        if (inCodeBlock) {
          closeCodeBlock();
        } else {
          closeParagraph();
          closeList();
          inCodeBlock = true;
          codeLines = [];
        }
        return;
      }

      if (inCodeBlock) {
        codeLines.push(line);
        return;
      }

      if (!line.trim()) {
        closeParagraph();
        if (listType) {
          const nextContentLine = lines.slice(index + 1).find((candidate) => candidate.trim());
          const nextListItem = parseListItem(nextContentLine);
          if (listItemType(nextListItem) === listType) {
            return;
          }
        }
        closeList();
        return;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        closeParagraph();
        closeList();
        const level = heading[1].length;
        output.push(`<h${level}>${renderMarkdownInline(heading[2].trim())}</h${level}>`);
        return;
      }

      if (/^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(line)) {
        closeParagraph();
        closeList();
        output.push("<hr>");
        return;
      }

      const listItem = parseListItem(line);
      if (listItem) {
        closeParagraph();
        const nextListType = listItemType(listItem);
        if (listType && listType !== nextListType) {
          closeList();
        }
        if (!listType) {
          listType = nextListType;
          const start = Number(listItem[2]);
          const startAttribute = listType === "ol" && Number.isFinite(start) && start !== 1 ? ` start="${start}"` : "";
          output.push(`<${listType}${startAttribute}>`);
        }
        output.push(`<li>${renderMarkdownInline(listItem[3].trim())}</li>`);
        return;
      }

      const quote = line.match(/^\s{0,3}>\s?(.*)$/);
      if (quote) {
        closeParagraph();
        closeList();
        output.push(`<blockquote>${renderMarkdownInline(quote[1])}</blockquote>`);
        return;
      }

      paragraph.push(line.trim());
    });

    if (inCodeBlock) {
      closeCodeBlock();
    }
    closeParagraph();
    closeList();
    return output.join("\n");
  }

  function saveShortcuts(nextShortcuts) {
    shortcuts = nextShortcuts
      .map(normalizeShortcut)
      .filter((shortcut) => shortcut && shortcut.label && shortcut.sequence);
    localStorage.setItem(STORAGE_SHORTCUTS_KEY, JSON.stringify(shortcuts));
    renderShortcutBar();
    scheduleLayoutRefresh();
    saveHostSettings();
  }

  function saveHostSettings(authenticationRealm) {
    sendMessage({
      type: "save-settings",
      settings: {
        shortcuts,
        gestures: gestureBindings,
        uiScale,
        terminalFontSize,
        ...activeAuthenticationSettings(authenticationRealm),
        ...activeFileBookmarkSettings(),
      },
    });
  }

  function hasLocalSettingsOverride() {
    return [
      STORAGE_SHORTCUTS_KEY,
      STORAGE_UI_SCALE_KEY,
      STORAGE_TERMINAL_FONT_KEY,
      authenticationStorageKey(STORAGE_PASSKEY_AUTH_MODE_KEY),
      authenticationStorageKey(STORAGE_PASSKEY_IDLE_MINUTES_KEY),
    ].some((storageKey) => localStorage.getItem(storageKey) !== null);
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.remove("hidden");
    window.clearTimeout(showToast.timeoutId);
    showToast.timeoutId = window.setTimeout(() => {
      toast.classList.add("hidden");
    }, 2800);
  }

  function isEditableTarget(target) {
    if (!(target instanceof HTMLElement)) {
      return false;
    }
    return Boolean(target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
  }

  function insertComposerText(text, focus = true) {
    if (!mobileComposerMode || !text) {
      return;
    }
    openComposer(focus);
    composerInput.setRangeText(text, composerInput.selectionStart, composerInput.selectionEnd, "end");
    autoSizeComposer();
    syncComposerState();
  }

  function composerHasKeyboardFocus() {
    return mobileComposerMode && document.activeElement === composerInput;
  }

  let lastComposerInputAt = 0;
  let userRefreshExpected = false;
  const COMPOSER_INPUT_QUIET_MS = 350;

  function userIsActivelyTyping() {
    if (!mobileComposerMode) return false;
    return Date.now() - lastComposerInputAt < COMPOSER_INPUT_QUIET_MS;
  }

  function consumeUserRefreshExpectation() {
    const value = userRefreshExpected;
    userRefreshExpected = false;
    return value;
  }

  function shouldYieldToLocalComposer(extracted) {
    if (!extracted) {
      return false;
    }
    if (!composerHasKeyboardFocus() && !userIsActivelyTyping()) {
      return false;
    }
    const localValue = composerInput.value;
    const localCursor = composerInput.selectionEnd ?? localValue.length;
    return localValue !== extracted.value || localCursor !== extracted.cursor;
  }

  function restoreShortcutKeyboardState(wasFocused) {
    if (!mobileComposerMode) {
      return;
    }
    window.requestAnimationFrame(() => {
      if (wasFocused) {
        openComposer(true);
      } else if (document.activeElement === composerInput) {
        composerInput.blur();
        setComposerActive(false);
      }
    });
  }

  function terminalHasSelection() {
    return Boolean(
      typeof term.getSelectionPosition === "function" && term.getSelectionPosition(),
    );
  }

  function terminalSelectionState() {
    const selection =
      typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : null;
    if (!selection || !term.buffer?.active) {
      return null;
    }
    return {
      selection: {
        start: { x: selection.start.x, y: selection.start.y },
        end: { x: selection.end.x, y: selection.end.y },
      },
      profile: activeProfileId || "",
      session: activeSessionName,
      paneId: terminalPaneId,
      epoch: terminalEpoch,
      revision: terminalRevision,
      cutoff: terminalCutoff,
      layoutGeneration: terminalLayoutGeneration,
      cols: term.cols,
      rows: term.rows,
      baseY: term.buffer.active.baseY,
      bufferType: term.buffer.active.type,
    };
  }

  function pendingSelectionRequestIsCurrent(pending) {
    return Boolean(
      pending &&
      terminalAuthoritative &&
      pending.state &&
      pending.state.epoch === terminalEpoch
    );
  }

  function requestAuthoritativeSelection() {
    const state = terminalSelectionState();
    if (!state) {
      return Promise.resolve({ error: "Select terminal text first." });
    }
    if (
      !terminalAuthoritative ||
      !socket ||
      socket.readyState !== WebSocket.OPEN
    ) {
      return Promise.resolve({ error: "Terminal changed; select again." });
    }
    selectionRequestCounter += 1;
    const requestId = `${Date.now()}-${selectionRequestCounter}`;
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        pendingSelectionRequests.delete(requestId);
        resolve({ error: "Terminal changed; select again." });
      }, 6000);
      pendingSelectionRequests.set(requestId, { resolve, timer, state });
      const sent = sendMessage({ type: "selection-request", requestId, ...state });
      if (!sent) {
        window.clearTimeout(timer);
        pendingSelectionRequests.delete(requestId);
        resolve({ error: "Terminal changed; select again." });
      }
    });
  }

  function copyTextWithFallback(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_error) {
      copied = false;
    }
    textarea.remove();
    return copied;
  }

  // The server already joins soft-wrapped rows, so preserve its authoritative
  // selection exactly apart from normalizing platform line endings.
  function normalizeTerminalCopyText(text) {
    return text.replace(/\r\n?/g, "\n");
  }

  function beginAuthoritativeClipboardWrite(selectionPromise) {
    try {
      if (
        typeof ClipboardItem !== "function" ||
        !navigator.clipboard ||
        typeof navigator.clipboard.write !== "function"
      ) {
        return null;
      }
      const blobPromise = selectionPromise.then((result) => {
        if (result.error) {
          throw new Error(result.error);
        }
        return new Blob([normalizeTerminalCopyText(result.text)], { type: "text/plain" });
      });
      blobPromise.catch(() => {});
      const writePromise = Promise.resolve(
        navigator.clipboard.write([
          new ClipboardItem({ "text/plain": blobPromise }),
        ]),
      );
      writePromise.catch(() => {});
      return writePromise;
    } catch (error) {
      const writePromise = Promise.reject(error);
      writePromise.catch(() => {});
      return writePromise;
    }
  }

  async function copyClipboardTextWithFallback(text) {
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_error) {
      // Fall through to the legacy browser path.
    }
    try {
      return copyTextWithFallback(text);
    } catch (_error) {
      return false;
    }
  }

  // Direct PTY paste remains deliberately single-line so a multi-line paste does
  // not submit each line early. Composer paste keeps the normalized line breaks.
  function normalizeDirectPtyPasteText(text) {
    return normalizeTerminalCopyText(text)
      .replace(/[ \t]*\n[ \t]*/g, " ")
      .replace(/ {2,}/g, " ")
      .trim();
  }

  function sendDirectPtyPaste(text) {
    const normalizedText = normalizeDirectPtyPasteText(text);
    if (!normalizedText) {
      return false;
    }
    return sendMessage({ type: "input", data: normalizedText });
  }

  async function copyTerminalSelection() {
    let selectionPromise;
    try {
      selectionPromise = Promise.resolve(requestAuthoritativeSelection());
    } catch (_error) {
      showToast("Terminal changed; select again.");
      return;
    }
    const clipboardWritePromise = beginAuthoritativeClipboardWrite(selectionPromise);
    let result;
    try {
      result = await selectionPromise;
    } catch (_error) {
      showToast("Terminal changed; select again.");
      return;
    }
    if (result.error) {
      showToast(result.error);
      return;
    }
    const text = normalizeTerminalCopyText(result.text);
    if (clipboardWritePromise) {
      try {
        await clipboardWritePromise;
        showToast("Copied terminal selection.");
        return;
      } catch (_error) {
        // A delayed write may still work in browsers without promised ClipboardItem support.
      }
    }
    if (await copyClipboardTextWithFallback(text)) {
      showToast("Copied terminal selection.");
      return;
    }
    showToast("Clipboard copy is blocked by this browser.");
  }

  // The most recent tab other than the current one. Prefers the tab the user
  // last came from (previousSessionName); falls back to the nearest other open
  // tab so the "To tab" chip always has a target when >1 tab is open.
  function recentOtherSession() {
    const current = selectedSessionName || activeSessionName;
    if (
      previousSessionName &&
      previousSessionName !== current &&
      openTabNames.includes(previousSessionName)
    ) {
      return previousSessionName;
    }
    const idx = openTabNames.indexOf(current);
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (openTabNames[i] && openTabNames[i] !== current) {
        return openTabNames[i];
      }
    }
    for (let i = openTabNames.length - 1; i >= 0; i -= 1) {
      if (openTabNames[i] && openTabNames[i] !== current) {
        return openTabNames[i];
      }
    }
    return "";
  }

  // Send the current selection to the most recent other tab: switch to it, then
  // drop the text into its composer once it's ready. The paste lands in the
  // prompt for review — it is not auto-executed.
  async function pasteSelectionToRecentTab() {
    const result = await requestAuthoritativeSelection();
    if (result.error) {
      showToast(result.error);
      return;
    }
    const target = recentOtherSession();
    if (!target) {
      showToast("No other tab to send to.");
      return;
    }
    pendingPasteAfterSwitch = {
      session: target,
      text: normalizeTerminalCopyText(result.text),
      ready: false,
    };
    dismissTerminalSelection();
    switchSession(target);
  }

  function handlePendingPasteReady() {
    const pending = pendingPasteAfterSwitch;
    if (!pending || pending.session !== activeSessionName) {
      return false;
    }
    if (isBtopSession(activeSessionName) || (mobileComposerMode && composerInput.disabled)) {
      pendingPasteAfterSwitch = null;
      showToast("This tab doesn't accept pasted text.");
      return false;
    }
    if (mobileComposerMode) {
      pending.ready = true;
      return false;
    }
    const normalizedText = normalizeDirectPtyPasteText(pending.text);
    if (!normalizedText) {
      pendingPasteAfterSwitch = null;
      return false;
    }
    resetSpeechInputState();
    if (!sendDirectPtyPaste(normalizedText)) {
      return false;
    }
    pendingPasteAfterSwitch = null;
    showToast("Pasted into this tab.");
    return true;
  }

  function deliverPendingPasteToComposer(revision) {
    const pending = pendingPasteAfterSwitch;
    if (
      !pending ||
      !pending.ready ||
      pending.session !== activeSessionName ||
      !mobileComposerMode
    ) {
      return false;
    }
    if (isBtopSession(activeSessionName) || composerInput.disabled) {
      pendingPasteAfterSwitch = null;
      showToast("This tab doesn't accept pasted text.");
      return false;
    }
    composerRevision = Math.max(composerRevision, revision);
    openComposer(true);
    composerInput.setRangeText(
      pending.text,
      composerInput.selectionStart,
      composerInput.selectionEnd,
      "end",
    );
    autoSizeComposer();
    const queued = sendMessage({
      type: "composer-sync",
      value: composerInput.value,
      cursor: composerInput.selectionEnd ?? composerInput.value.length,
      revision: nextComposerRevision(),
    });
    if (!queued) {
      return false;
    }
    pendingPasteAfterSwitch = null;
    showToast("Pasted into this tab.");
    return true;
  }

  // --- Touch text selection (press-and-hold, then drag the handles) ---------
  // xterm paints its rows with `user-select: none`, so the browser never offers
  // native selection handles on a long-press. Instead we recognise a long-press
  // ourselves, drive xterm's own mouse-based selection with synthetic
  // MouseEvents, and render our own draggable handles + a Copy chip on top.
  const TERM_LONGPRESS_MS = 300; // hold this long to start selecting a word
  const TERM_LONGPRESS_SLOP = 12; // px the finger may drift before it's a scroll
  const TERM_DOUBLETAP_MS = 120; // max gap between the two taps of a double-tap
  const TERM_DOUBLETAP_DIST = 28; // px the two taps may be apart
  const TERM_SELECTION_TARGET_SIZE = 44;
  const TERM_SELECTION_KNOB_SIZE = 16;
  const TERM_SELECTION_TOOLBAR_GAP = 8;
  const TERM_SELECTION_MAGNIFIER_WIDTH = 112;
  const TERM_SELECTION_MAGNIFIER_HEIGHT = 72;
  const TERM_SELECTION_MAGNIFIER_OFFSET = 72;
  const TERM_SELECTION_MAGNIFIER_ZOOM = 1.8;
  let termSel = null; // in-flight press/drag session, or null
  let termSelHandles = null; // lazily-created selection overlay DOM
  let terminalSelectionSyncFrameId = null;
  let terminalSelectionSyncForced = false;
  let selectionDragFeedback = null;
  let selectionDragFeedbackGeneration = 0;
  let suppressNextTerminalClick = false;
  let suppressComposerOpenThisTouch = false; // set when a tap only dismisses a selection
  let selectionTapCopy = null; // {x,y} when a touch landed inside the live selection
  let lastTermTapAt = 0; // for double-tap-to-select-word recognition
  let lastTermTapX = 0;
  let lastTermTapY = 0;

  function clampSelectionValue(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
  }

  function clampSelectionPoint(point, bounds) {
    return {
      x: clampSelectionValue(point.x, bounds.left, bounds.right),
      y: clampSelectionValue(point.y, bounds.top, bounds.bottom),
    };
  }

  function computeSelectionOverlayBounds(terminalRect, screenRect, viewportRect, safeArea) {
    const safeViewport = {
      left: viewportRect.left + safeArea.left,
      top: viewportRect.top + safeArea.top,
      right: viewportRect.right - safeArea.right,
      bottom: viewportRect.bottom - safeArea.bottom,
    };
    const left = Math.max(terminalRect.left, screenRect.left, safeViewport.left);
    const top = Math.max(terminalRect.top, screenRect.top, safeViewport.top);
    const right = Math.max(left, Math.min(terminalRect.right, screenRect.right, safeViewport.right));
    const bottom = Math.max(top, Math.min(terminalRect.bottom, screenRect.bottom, safeViewport.bottom));
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }

  function computeSelectionHandlePlacement(
    boundaryX,
    lineTop,
    lineBottom,
    bounds,
    side,
    targetSize,
    knobSize,
  ) {
    const width = Math.min(targetSize, bounds.width);
    const height = Math.min(targetSize, bounds.height);
    const knobWidth = knobSize;
    const knobHeight = knobSize;
    const knobCenterY = side === "start" ? lineTop - knobSize / 2 : lineBottom + knobSize / 2;
    const left = clampSelectionValue(boundaryX - width / 2, bounds.left, bounds.right - width);
    const top = clampSelectionValue(knobCenterY - height / 2, bounds.top, bounds.bottom - height);
    const knobLeft = clampSelectionValue(
      boundaryX - knobWidth / 2,
      bounds.left,
      bounds.right - knobWidth,
    );
    const knobTop = clampSelectionValue(
      knobCenterY - knobHeight / 2,
      bounds.top,
      bounds.bottom - knobHeight,
    );
    return {
      left,
      top,
      width,
      height,
      stemX: boundaryX,
      stemTop: lineTop,
      stemHeight: Math.max(0, lineBottom - lineTop),
      knobLeft,
      knobTop,
      knobWidth,
      knobHeight,
    };
  }

  function computeSelectionToolbarPlacement(selectionRect, toolbarSize, bounds, gap) {
    const availableWidth = bounds.width;
    const availableHeight = bounds.height;
    const centeredLeft = (selectionRect.left + selectionRect.right - toolbarSize.width) / 2;
    const left = toolbarSize.width > availableWidth
      ? bounds.left
      : clampSelectionValue(centeredLeft, bounds.left, bounds.right - toolbarSize.width);
    const aboveTop = selectionRect.top - gap - toolbarSize.height;
    const belowTop = selectionRect.bottom + gap;
    const fitsAbove = aboveTop >= bounds.top;
    const fitsBelow = belowTop + toolbarSize.height <= bounds.bottom;
    const aboveSpace = selectionRect.top - gap - bounds.top;
    const belowSpace = bounds.bottom - selectionRect.bottom - gap;
    const side = fitsAbove || (!fitsBelow && aboveSpace >= belowSpace) ? "above" : "below";
    const preferredTop = side === "above" ? aboveTop : belowTop;
    const top = clampSelectionValue(
      preferredTop,
      bounds.top,
      bounds.bottom - Math.min(toolbarSize.height, availableHeight),
    );
    return {
      left,
      top,
      side,
      maxWidth: availableWidth,
      overflowX: toolbarSize.width > availableWidth,
    };
  }

  function createSelectionDragMotionState(point, timestamp) {
    return {
      point: { x: point.x, y: point.y },
      rawPoint: { x: point.x, y: point.y },
      sampledAt: timestamp,
      rawSampledAt: timestamp,
      speed: 0,
      instantaneousSpeed: 0,
      cumulativeMovement: 0,
      lowSpeedSince: null,
      lastMeaningfulAt: null,
      hasMeaningfulMovement: false,
    };
  }

  function sampleSelectionDragMotion(state, point, timestamp, options = {}) {
    const meaningfulDistance = options.meaningfulDistance ?? 4;
    const jitterDistance = options.jitterDistance ?? 0.5;
    const blend = options.blend ?? 0.35;
    const now = Math.max(state.rawSampledAt, timestamp);
    const distance = Math.hypot(point.x - state.rawPoint.x, point.y - state.rawPoint.y);
    if (distance <= jitterDistance) {
      return {
        ...state,
        rawPoint: { x: point.x, y: point.y },
        rawSampledAt: now,
      };
    }
    const elapsed = Math.max(1, now - state.rawSampledAt);
    const instantaneousSpeed = distance / elapsed;
    const speed = state.speed * (1 - blend) + instantaneousSpeed * blend;
    const cumulativeMovement = state.cumulativeMovement + distance;
    const hasMeaningfulMovement =
      state.hasMeaningfulMovement || cumulativeMovement >= meaningfulDistance;
    const showSpeed = options.showSpeed ?? 0.18;
    const continuouslySlow = instantaneousSpeed <= showSpeed && speed <= showSpeed;
    return {
      point: { x: point.x, y: point.y },
      rawPoint: { x: point.x, y: point.y },
      sampledAt: now,
      rawSampledAt: now,
      speed,
      instantaneousSpeed,
      cumulativeMovement,
      lowSpeedSince: hasMeaningfulMovement && continuouslySlow
        ? state.lowSpeedSince ?? now
        : null,
      lastMeaningfulAt: hasMeaningfulMovement ? now : null,
      hasMeaningfulMovement,
    };
  }

  function decideSelectionMagnifierVisibility(state, timestamp, visible, options = {}) {
    const now = Math.max(state.sampledAt, timestamp);
    const dwell = options.dwell ?? 100;
    const hideSpeed = options.hideSpeed ?? 0.45;
    const fastSample =
      now === state.sampledAt &&
      (state.instantaneousSpeed >= hideSpeed || state.speed >= hideSpeed);
    if (fastSample) {
      return false;
    }
    if (visible) {
      return true;
    }
    const slowDwell =
      state.hasMeaningfulMovement &&
      state.lowSpeedSince !== null &&
      now - state.lowSpeedSince >= dwell;
    const pauseDwell =
      state.hasMeaningfulMovement &&
      state.lastMeaningfulAt !== null &&
      now - state.lastMeaningfulAt >= dwell;
    return slowDwell || pauseDwell;
  }

  function selectionMagnifierDwellDeadline(state, timestamp, options = {}) {
    const now = Math.max(state.sampledAt, timestamp);
    const dwell = options.dwell ?? 100;
    const deadlines = [];
    if (state.hasMeaningfulMovement && state.lowSpeedSince !== null) {
      deadlines.push(state.lowSpeedSince + dwell);
    }
    if (state.hasMeaningfulMovement && state.lastMeaningfulAt !== null) {
      deadlines.push(state.lastMeaningfulAt + dwell);
    }
    const pending = deadlines.filter((deadline) => deadline > now);
    return pending.length ? Math.min(...pending) : null;
  }

  function computeSelectionMagnifierPlacement(point, size, bounds, offset) {
    const width = Math.min(size.width, bounds.width);
    const height = Math.min(size.height, bounds.height);
    const left = clampSelectionValue(
      point.x - width / 2,
      bounds.left,
      bounds.right - width,
    );
    const aboveTop = point.y - offset - height / 2;
    const belowTop = point.y + offset - height / 2;
    const fitsAbove = aboveTop >= bounds.top;
    const fitsBelow = belowTop + height <= bounds.bottom;
    const aboveSpace = point.y - bounds.top;
    const belowSpace = bounds.bottom - point.y;
    const side = fitsAbove || (!fitsBelow && aboveSpace >= belowSpace) ? "above" : "below";
    const preferredTop = side === "above" ? aboveTop : belowTop;
    const top = clampSelectionValue(preferredTop, bounds.top, bounds.bottom - height);
    return { left, top, width, height, side };
  }

  function computeSelectionMagnifierTransform(point, sourceRect, lensSize, zoom) {
    const sourceX = clampSelectionValue(point.x, sourceRect.left, sourceRect.right) - sourceRect.left;
    const sourceY = clampSelectionValue(point.y, sourceRect.top, sourceRect.bottom) - sourceRect.top;
    return {
      translateX: lensSize.width / 2 - sourceX * zoom,
      translateY: lensSize.height / 2 - sourceY * zoom,
      scale: zoom,
    };
  }

  function hapticPulse(ms) {
    try {
      if (typeof navigator.vibrate === "function") {
        navigator.vibrate(ms || 12);
      }
    } catch (_error) {
      // Vibration is best-effort; ignore unsupported platforms.
    }
  }

  function terminalScreenEl() {
    return terminalElement.querySelector(".xterm-screen");
  }

  function terminalSelectionSafeArea() {
    const style = window.getComputedStyle(document.documentElement);
    const readInset = (name) => Number.parseFloat(style.getPropertyValue(name)) || 0;
    return {
      top: readInset("--safe-area-inset-top"),
      right: readInset("--safe-area-inset-right"),
      bottom: readInset("--safe-area-inset-bottom"),
      left: readInset("--safe-area-inset-left"),
    };
  }

  function terminalSelectionViewportRect() {
    const viewport = window.visualViewport;
    const left = viewport ? viewport.offsetLeft : 0;
    const top = viewport ? viewport.offsetTop : 0;
    const width = viewport ? viewport.width : window.innerWidth;
    const height = viewport ? viewport.height : window.innerHeight;
    return { left, top, right: left + width, bottom: top + height };
  }

  function terminalSelectionOverlayBounds(screen) {
    return computeSelectionOverlayBounds(
      terminalPanel.getBoundingClientRect(),
      screen.getBoundingClientRect(),
      terminalSelectionViewportRect(),
      terminalSelectionSafeArea(),
    );
  }

  function clampTerminalSelectionDragPoint(clientX, clientY) {
    const screen = terminalScreenEl();
    if (!screen) {
      return { x: clientX, y: clientY };
    }
    const rect = screen.getBoundingClientRect();
    return clampSelectionPoint(
      { x: clientX, y: clientY },
      {
        left: rect.left,
        top: rect.top,
        right: Math.max(rect.left, rect.right - 0.5),
        bottom: Math.max(rect.top, rect.bottom - 0.5),
      },
    );
  }

  function mouseEventsCaptured() {
    // Apps like btop/vim enable SGR mouse reporting; xterm then disables text
    // selection and forwards clicks. Don't hijack those with a fake selection.
    const svc = term._core?._coreMouseService || term._core?.coreMouseService;
    return !!svc?.areMouseEventsActive;
  }

  function terminalCellSize() {
    const dims = term._core?._renderService?.dimensions;
    const cell = dims?.css?.cell;
    if (cell && cell.width > 0 && cell.height > 0) {
      return { width: cell.width, height: cell.height };
    }
    const screen = terminalScreenEl();
    if (screen && term.cols > 0 && term.rows > 0) {
      const rect = screen.getBoundingClientRect();
      return { width: rect.width / term.cols, height: rect.height / term.rows };
    }
    return { width: 8, height: 16 };
  }

  function terminalHelperTextarea() {
    return terminalElement.querySelector(".xterm-helper-textarea");
  }

  // Feed xterm's selection service a synthetic mouse event at a screen point.
  // xterm binds mousedown on the .xterm element and mousemove/mouseup on the
  // document, so dispatching on the screen node (which bubbles to both) drives
  // the exact same code path as a real mouse drag.
  function dispatchTerminalMouse(type, clientX, clientY, detail) {
    const target = terminalScreenEl() || terminalElement;
    // When the foreground app has mouse reporting on (tmux `mouse on`, vim,
    // btop…) xterm would forward the click instead of selecting. Holding Shift
    // makes xterm force a *local* text selection instead — exactly what we want.
    // In a plain shell, shift-click means "extend", so only force it when needed.
    const forceShift = mouseEventsCaptured();
    // xterm focuses its hidden textarea on mousedown, which in composer mode
    // would pop the keyboard on the wrong field. That focus is often deferred a
    // frame, so it can't be caught here — guardTerminalHelperTextarea() handles
    // it via a focusin listener on the textarea itself instead.
    target.dispatchEvent(
      new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        view: window,
        button: 0,
        buttons: type === "mouseup" ? 0 : 1,
        detail: detail || 1,
        shiftKey: forceShift,
        clientX,
        clientY,
      }),
    );
  }

  // xterm focuses its hidden textarea on every mousedown so it can receive
  // keyboard input. In composer mode the app never types through it (input goes
  // through the composer), so that focus only ever pops the keyboard on the
  // wrong field — and because xterm often defers the focus a frame, a
  // synchronous guard around the dispatch misses it. Instead: keep the textarea
  // permanently readonly (a readonly field can't raise the soft keyboard) and
  // bounce focus off it whenever xterm grabs it — back to the composer if the
  // user was typing there, otherwise to nothing.
  function guardTerminalHelperTextarea() {
    if (!mobileComposerMode) {
      return;
    }
    const helper = terminalHelperTextarea();
    if (!helper) {
      return;
    }
    helper.readOnly = true;
    helper.setAttribute("inputmode", "none");
    helper.addEventListener("focusin", (event) => {
      if (!btopMode && event.relatedTarget === composerInput) {
        // The user was typing — keep the keyboard where it was.
        composerInput.focus({ preventScroll: true });
      } else {
        helper.blur();
      }
    });
  }

  function hideSelectionMagnifier(clearSnapshot = false) {
    if (!termSelHandles) {
      return;
    }
    termSelHandles.magnifier.classList.remove("is-visible");
    if (clearSnapshot) {
      termSelHandles.magnifierContent.replaceChildren();
    }
  }

  function refreshSelectionMagnifierSnapshot() {
    const rows = terminalElement.querySelector(".xterm-rows");
    if (!rows || !termSelHandles) {
      hideSelectionMagnifier();
      return false;
    }
    const clone = rows.cloneNode(true);
    clone.querySelectorAll(".xterm-cursor").forEach((element) => {
      for (const className of Array.from(element.classList)) {
        if (className.startsWith("xterm-cursor")) {
          element.classList.remove(className);
        }
      }
      for (const attribute of Array.from(element.attributes)) {
        if (attribute.name.includes("cursor")) {
          element.removeAttribute(attribute.name);
        }
      }
    });
    clone.querySelectorAll(
      "textarea, input, button, select, a, [href], [contenteditable], [tabindex], " +
        "[onclick], [onmousedown], [onpointerdown], [role='button'], [role='link'], " +
        ".xterm-helper-textarea, .xterm-selection, .xterm-selection-layer, " +
        ".term-select-layer",
    ).forEach((element) => element.remove());
    if (clone.hasAttribute("id")) {
      clone.removeAttribute("id");
    }
    clone.querySelectorAll("[id]").forEach((element) => element.removeAttribute("id"));
    clone.setAttribute("aria-hidden", "true");

    const owner = rows.closest(".xterm");
    const rendererContext = document.createElement("div");
    rendererContext.className = owner?.className || "xterm";
    rendererContext.classList.add("term-select-magnifier-renderer-context");
    const screenContext = document.createElement("div");
    screenContext.className = rows.parentElement?.className || "xterm-screen";
    screenContext.appendChild(clone);
    rendererContext.appendChild(screenContext);
    termSelHandles.magnifierContent.replaceChildren(rendererContext);
    selectionDragFeedback.snapshotDirty = false;
    return true;
  }

  function renderSelectionDragFeedbackFrame(generation) {
    if (!selectionDragFeedback || selectionDragFeedback.generation !== generation) {
      return;
    }
    selectionDragFeedback.frameId = null;
    if (!selectionDragFeedback.visible) {
      hideSelectionMagnifier();
      return;
    }
    if (!terminalHasSelection()) {
      clearTerminalSelectionUI();
      return;
    }
    const screen = terminalScreenEl();
    const rows = terminalElement.querySelector(".xterm-rows");
    if (!screen || !rows) {
      hideSelectionMagnifier();
      return;
    }
    const handles = ensureSelectionHandles();
    if (selectionDragFeedback.snapshotDirty && !refreshSelectionMagnifierSnapshot()) {
      return;
    }
    const bounds = terminalSelectionOverlayBounds(screen);
    if (bounds.width <= 0 || bounds.height <= 0) {
      hideSelectionMagnifier();
      return;
    }
    const placement = computeSelectionMagnifierPlacement(
      selectionDragFeedback.point,
      { width: TERM_SELECTION_MAGNIFIER_WIDTH, height: TERM_SELECTION_MAGNIFIER_HEIGHT },
      bounds,
      TERM_SELECTION_MAGNIFIER_OFFSET,
    );
    const sourceRect = rows.getBoundingClientRect();
    if (sourceRect.width <= 0 || sourceRect.height <= 0) {
      hideSelectionMagnifier();
      return;
    }
    const transform = computeSelectionMagnifierTransform(
      selectionDragFeedback.point,
      sourceRect,
      placement,
      TERM_SELECTION_MAGNIFIER_ZOOM,
    );
    const hostRect = terminalElement.getBoundingClientRect();
    handles.magnifier.style.left = `${placement.left - hostRect.left}px`;
    handles.magnifier.style.top = `${placement.top - hostRect.top}px`;
    handles.magnifier.style.width = `${placement.width}px`;
    handles.magnifier.style.height = `${placement.height}px`;
    handles.magnifier.dataset.side = placement.side;
    handles.magnifierContent.style.width = `${sourceRect.width}px`;
    handles.magnifierContent.style.height = `${sourceRect.height}px`;
    handles.magnifierContent.style.transform =
      `translate3d(${transform.translateX}px, ${transform.translateY}px, 0) scale(${transform.scale})`;
    handles.magnifier.classList.add("is-visible");
  }

  function scheduleSelectionDragFeedbackFrame() {
    if (
      !selectionDragFeedback ||
      !selectionDragFeedback.visible ||
      selectionDragFeedback.frameId !== null
    ) {
      return;
    }
    const generation = selectionDragFeedback.generation;
    selectionDragFeedback.frameId = window.requestAnimationFrame(() => {
      renderSelectionDragFeedbackFrame(generation);
    });
  }

  function scheduleSelectionDragFeedbackDwell(timestamp) {
    if (!selectionDragFeedback) {
      return;
    }
    if (selectionDragFeedback.timerId !== null) {
      window.clearTimeout(selectionDragFeedback.timerId);
      selectionDragFeedback.timerId = null;
    }
    if (selectionDragFeedback.visible) {
      return;
    }
    const deadline = selectionMagnifierDwellDeadline(
      selectionDragFeedback.motion,
      timestamp,
    );
    if (deadline === null) {
      return;
    }
    const generation = selectionDragFeedback.generation;
    selectionDragFeedback.timerId = window.setTimeout(() => {
      if (!selectionDragFeedback || selectionDragFeedback.generation !== generation) {
        return;
      }
      selectionDragFeedback.timerId = null;
      applySelectionDragFeedbackDecision(performance.now(), generation);
    }, Math.max(0, deadline - timestamp));
  }

  function applySelectionDragFeedbackDecision(timestamp, generation) {
    if (!selectionDragFeedback || selectionDragFeedback.generation !== generation) {
      return;
    }
    const visible = decideSelectionMagnifierVisibility(
      selectionDragFeedback.motion,
      timestamp,
      selectionDragFeedback.visible,
    );
    selectionDragFeedback.visible = visible;
    if (visible) {
      scheduleSelectionDragFeedbackFrame();
    } else {
      hideSelectionMagnifier();
    }
    scheduleSelectionDragFeedbackDwell(timestamp);
  }

  function beginSelectionDragFeedback(point) {
    endSelectionDragFeedback();
    if (!point) {
      return;
    }
    const timestamp = performance.now();
    const motion = createSelectionDragMotionState(point, timestamp);
    selectionDragFeedback = {
      generation: selectionDragFeedbackGeneration,
      point: motion.point,
      motion,
      visible: false,
      snapshotDirty: true,
      frameId: null,
      timerId: null,
    };
    ensureSelectionHandles();
    scheduleSelectionDragFeedbackDwell(timestamp);
  }

  function updateSelectionDragFeedback(point) {
    if (!selectionDragFeedback || !point) {
      return;
    }
    const timestamp = performance.now();
    selectionDragFeedback.motion = sampleSelectionDragMotion(
      selectionDragFeedback.motion,
      point,
      timestamp,
    );
    selectionDragFeedback.point = selectionDragFeedback.motion.point;
    selectionDragFeedback.snapshotDirty = true;
    applySelectionDragFeedbackDecision(timestamp, selectionDragFeedback.generation);
  }

  function endSelectionDragFeedback() {
    selectionDragFeedbackGeneration += 1;
    if (selectionDragFeedback) {
      if (selectionDragFeedback.timerId !== null) {
        window.clearTimeout(selectionDragFeedback.timerId);
      }
      if (selectionDragFeedback.frameId !== null) {
        window.cancelAnimationFrame(selectionDragFeedback.frameId);
      }
    }
    selectionDragFeedback = null;
    hideSelectionMagnifier(true);
  }

  function markSelectionDragFeedbackDirty() {
    if (!selectionDragFeedback) {
      return;
    }
    selectionDragFeedback.snapshotDirty = true;
    scheduleSelectionDragFeedbackFrame();
  }

  function isSelectionUIVisible() {
    return !!(termSelHandles && termSelHandles.layer.style.display !== "none");
  }

  function selectionUIBusy() {
    return !!(termSel && (termSel.active || termSel.draggingHandle));
  }

  function scheduleTerminalSelectionUISync(force = false) {
    terminalSelectionSyncForced = terminalSelectionSyncForced || force;
    markSelectionDragFeedbackDirty();
    if (terminalSelectionSyncFrameId !== null) {
      return;
    }
    terminalSelectionSyncFrameId = window.requestAnimationFrame(() => {
      terminalSelectionSyncFrameId = null;
      const shouldSync =
        terminalSelectionSyncForced ||
        isSelectionUIVisible() ||
        selectionUIBusy() ||
        (termSel && termSel.doubleTap);
      terminalSelectionSyncForced = false;
      if (shouldSync) {
        updateTerminalSelectionUI();
      }
    });
  }

  function clearTerminalSelectionUI() {
    endSelectionDragFeedback();
    if (termSelHandles) {
      termSelHandles.layer.style.display = "none";
    }
  }

  function dismissTerminalSelection() {
    if (typeof term.clearSelection === "function") {
      term.clearSelection();
    }
    clearTerminalSelectionUI();
  }

  function ensureSelectionHandles() {
    if (termSelHandles) {
      return termSelHandles;
    }
    const layer = document.createElement("div");
    layer.className = "term-select-layer";
    const makeHandle = (side) => {
      const handle = document.createElement("div");
      handle.className = `term-select-handle term-select-handle-${side}`;
      handle.dataset.handle = side;
      const stem = document.createElement("span");
      stem.className = "term-select-stem";
      const knob = document.createElement("span");
      knob.className = "term-select-knob";
      handle.append(stem, knob);
      attachHandleDrag(handle);
      return handle;
    };
    const start = makeHandle("start");
    const end = makeHandle("end");
    // A chip is a tap target that floats over the selection. It must fully own
    // its tap: if touchstart/touchend bubble to #terminal, finishTouchScroll
    // raises the keyboard, which resizes the viewport and repositions the chip
    // mid-tap — iOS then CANCELS the synthetic click, so a `click`-only handler
    // silently never fires. So we fire on touchend directly (preventDefault to
    // kill the trailing synthetic click + stop the scroll/keyboard cascade) and
    // keep `click` only as the desktop/mouse path, deduped against the touch.
    const makeChip = (label, onActivate) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "term-select-chip";
      btn.textContent = label;
      let touchHandledAt = 0;
      let touchStartPoint = null;
      btn.addEventListener(
        "touchstart",
        (event) => {
          event.stopPropagation();
          const touch = event.touches[0];
          touchStartPoint = touch ? { x: touch.clientX, y: touch.clientY, moved: false } : null;
        },
        { passive: true },
      );
      btn.addEventListener(
        "touchmove",
        (event) => {
          event.stopPropagation();
          const touch = event.touches[0];
          if (
            touch &&
            touchStartPoint &&
            Math.hypot(touch.clientX - touchStartPoint.x, touch.clientY - touchStartPoint.y) >
              TERM_LONGPRESS_SLOP
          ) {
            touchStartPoint.moved = true;
          }
        },
        { passive: true },
      );
      btn.addEventListener(
        "touchend",
        async (event) => {
          event.preventDefault();
          event.stopPropagation();
          const moved = touchStartPoint && touchStartPoint.moved;
          touchStartPoint = null;
          if (moved) {
            return;
          }
          touchHandledAt = performance.now();
          await onActivate();
        },
        { passive: false },
      );
      btn.addEventListener("touchcancel", () => {
        touchStartPoint = null;
      });
      btn.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        // Ignore the click that some browsers still synthesize after touchend.
        if (performance.now() - touchHandledAt < 700) {
          return;
        }
        await onActivate();
      });
      return btn;
    };
    const copy = makeChip("Copy", async () => {
      await copyTerminalSelection();
      dismissTerminalSelection();
    });
    const paste = makeChip("To tab", async () => {
      await pasteSelectionToRecentTab();
    });
    const chips = document.createElement("div");
    chips.className = "term-select-chips";
    ["touchstart", "touchmove", "touchend", "touchcancel"].forEach((type) => {
      chips.addEventListener(type, (event) => event.stopPropagation(), { passive: true });
    });
    chips.append(copy, paste);
    const magnifier = document.createElement("div");
    magnifier.className = "term-select-magnifier";
    magnifier.setAttribute("aria-hidden", "true");
    const magnifierViewport = document.createElement("div");
    magnifierViewport.className = "term-select-magnifier-viewport";
    const magnifierContent = document.createElement("div");
    magnifierContent.className = "term-select-magnifier-content";
    magnifierViewport.appendChild(magnifierContent);
    magnifier.appendChild(magnifierViewport);
    layer.append(start, end, chips, magnifier);
    terminalElement.appendChild(layer);
    termSelHandles = {
      layer,
      start,
      end,
      copy,
      paste,
      chips,
      magnifier,
      magnifierContent,
    };
    return termSelHandles;
  }

  // Reposition the handles + action toolbar to the current selection. Endpoint
  // stems stay on their exact cell boundaries while their touch targets and knobs
  // are independently kept inside the visible terminal-safe rectangle.
  function updateTerminalSelectionUI() {
    const pos =
      typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : null;
    if (!pos || !terminalHasSelection()) {
      clearTerminalSelectionUI();
      return;
    }
    const screen = terminalScreenEl();
    if (!screen) {
      clearTerminalSelectionUI();
      return;
    }
    const handles = ensureSelectionHandles();
    const cell = terminalCellSize();
    const screenRect = screen.getBoundingClientRect();
    const hostRect = terminalElement.getBoundingClientRect();
    const bounds = terminalSelectionOverlayBounds(screen);
    if (bounds.width <= 0 || bounds.height <= 0) {
      clearTerminalSelectionUI();
      return;
    }
    handles.layer.style.display = "block";
    const ydisp = term.buffer.active.viewportY;
    const place = (el, col, absRow, side) => {
      const vrow = absRow - ydisp;
      if (vrow < 0 || vrow >= term.rows) {
        el.style.display = "none";
        return null;
      }
      const boundaryX = screenRect.left + clampSelectionValue(col, 0, term.cols) * cell.width;
      const lineTop = screenRect.top + vrow * cell.height;
      const lineBottom = lineTop + cell.height;
      const placement = computeSelectionHandlePlacement(
        boundaryX,
        lineTop,
        lineBottom,
        bounds,
        side,
        TERM_SELECTION_TARGET_SIZE,
        TERM_SELECTION_KNOB_SIZE,
      );
      el.style.display = "block";
      el.style.left = `${placement.left - hostRect.left}px`;
      el.style.top = `${placement.top - hostRect.top}px`;
      el.style.width = `${placement.width}px`;
      el.style.height = `${placement.height}px`;
      el.style.setProperty("--term-select-stem-left", `${placement.stemX - placement.left}px`);
      el.style.setProperty("--term-select-stem-top", `${placement.stemTop - placement.top}px`);
      el.style.setProperty("--term-select-stem-height", `${placement.stemHeight}px`);
      el.style.setProperty("--term-select-knob-left", `${placement.knobLeft - placement.left}px`);
      el.style.setProperty("--term-select-knob-top", `${placement.knobTop - placement.top}px`);
      return {
        left: placement.stemX,
        right: placement.stemX,
        top: Math.min(lineTop, placement.knobTop),
        bottom: Math.max(lineBottom, placement.knobTop + placement.knobHeight),
      };
    };
    const startBox = place(handles.start, pos.start.x, pos.start.y, "start");
    const endBox = place(handles.end, pos.end.x, pos.end.y, "end");
    const visibleBoxes = [startBox, endBox].filter(Boolean);
    const firstVisibleRow = ydisp;
    const lastVisibleRow = ydisp + term.rows - 1;
    const selectionOverlapsViewport = pos.start.y <= lastVisibleRow && pos.end.y >= firstVisibleRow;
    if (!visibleBoxes.length && !selectionOverlapsViewport) {
      handles.chips.style.display = "none";
      handles.layer.style.display = "block";
      return;
    }
    const multiRow = pos.start.y !== pos.end.y;
    const selectionRect = visibleBoxes.length
      ? {
          left: multiRow ? bounds.left : Math.min(...visibleBoxes.map((box) => box.left)),
          right: multiRow ? bounds.right : Math.max(...visibleBoxes.map((box) => box.right)),
          top: clampSelectionValue(
            Math.min(...visibleBoxes.map((box) => box.top)),
            bounds.top,
            bounds.bottom,
          ),
          bottom: clampSelectionValue(
            Math.max(...visibleBoxes.map((box) => box.bottom)),
            bounds.top,
            bounds.bottom,
          ),
        }
      : { left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom };
    handles.chips.style.display = "flex";
    handles.chips.style.maxWidth = "none";
    const toolbarRect = handles.chips.getBoundingClientRect();
    const toolbarSize = {
      width: Math.max(toolbarRect.width, handles.chips.scrollWidth),
      height: toolbarRect.height,
    };
    const toolbarPlacement = computeSelectionToolbarPlacement(
      selectionRect,
      toolbarSize,
      bounds,
      TERM_SELECTION_TOOLBAR_GAP,
    );
    handles.chips.classList.toggle("is-below", toolbarPlacement.side === "below");
    handles.chips.style.left = `${toolbarPlacement.left - hostRect.left}px`;
    handles.chips.style.top = `${toolbarPlacement.top - hostRect.top}px`;
    handles.chips.style.maxWidth = `${toolbarPlacement.maxWidth}px`;
    handles.chips.dataset.overflowX = toolbarPlacement.overflowX ? "true" : "false";
    handles.layer.style.display = "block";
  }

  // True when a screen point falls within the current text selection's range.
  // Lets a tap on the highlighted text itself trigger a copy (see touchstart).
  function pointInSelection(clientX, clientY) {
    const pos =
      typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : null;
    if (!pos) {
      return false;
    }
    const screen = terminalScreenEl();
    if (!screen) {
      return false;
    }
    const cell = terminalCellSize();
    const rect = screen.getBoundingClientRect();
    const col = Math.floor((clientX - rect.left) / cell.width);
    const vrow = Math.floor((clientY - rect.top) / cell.height);
    if (vrow < 0 || vrow >= term.rows) {
      return false;
    }
    const absRow = vrow + term.buffer.active.viewportY;
    const afterStart = absRow > pos.start.y || (absRow === pos.start.y && col >= pos.start.x);
    // pos.end.x is one past the last selected cell; <= adds a cell of tap slop.
    const beforeEnd = absRow < pos.end.y || (absRow === pos.end.y && col <= pos.end.x);
    return afterStart && beforeEnd;
  }

  // Drag a handle to move one end of the selection. We re-drive xterm from the
  // fixed (opposite) endpoint to the finger so it reselects with per-character
  // precision, reversed ranges included.
  function attachHandleDrag(handle) {
    const anchorFromFixed = () => {
      const pos = term.getSelectionPosition();
      if (!pos) {
        return null;
      }
      const cell = terminalCellSize();
      const screen = terminalScreenEl();
      if (!screen) {
        return null;
      }
      const screenRect = screen.getBoundingClientRect();
      const ydisp = term.buffer.active.viewportY;
      const side = handle.dataset.handle;
      // Dragging "start" pivots on the last selected cell; "end" on the first.
      const col = side === "start" ? Math.max(0, pos.end.x - 1) : pos.start.x;
      const row = side === "start" ? pos.end.y : pos.start.y;
      return {
        clientX: screenRect.left + (col + 0.5) * cell.width,
        clientY: screenRect.top + (row - ydisp + 0.5) * cell.height,
      };
    };
    handle.addEventListener(
      "touchstart",
      (event) => {
        if (event.touches.length !== 1) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        const anchor = anchorFromFixed();
        if (!anchor) {
          return;
        }
        termSel = { draggingHandle: handle.dataset.handle, anchor };
        const touch = event.touches[0];
        const point = clampTerminalSelectionDragPoint(touch.clientX, touch.clientY);
        dispatchTerminalMouse("mousedown", anchor.clientX, anchor.clientY, 1);
        dispatchTerminalMouse("mousemove", point.x, point.y, 1);
        beginSelectionDragFeedback(point);
        scheduleTerminalSelectionUISync();
      },
      { passive: false },
    );
    handle.addEventListener(
      "touchmove",
      (event) => {
        if (!termSel || !termSel.draggingHandle || event.touches.length !== 1) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        const touch = event.touches[0];
        const point = clampTerminalSelectionDragPoint(touch.clientX, touch.clientY);
        dispatchTerminalMouse("mousemove", point.x, point.y, 1);
        updateSelectionDragFeedback(point);
        scheduleTerminalSelectionUISync();
      },
      { passive: false },
    );
    const finishHandleDrag = (event) => {
      if (!termSel || !termSel.draggingHandle) {
        return;
      }
      event.stopPropagation();
      const touch = event.changedTouches && event.changedTouches[0];
      const clientX = touch ? touch.clientX : termSel.anchor.clientX;
      const clientY = touch ? touch.clientY : termSel.anchor.clientY;
      const point = clampTerminalSelectionDragPoint(clientX, clientY);
      dispatchTerminalMouse("mouseup", point.x, point.y, 1);
      endSelectionDragFeedback();
      termSel = null;
      scheduleTerminalSelectionUISync();
    };
    handle.addEventListener("touchend", finishHandleDrag, { passive: false });
    handle.addEventListener("touchcancel", finishHandleDrag, { passive: false });
  }

  // Select the word under a point (long-press fire or double-tap). Returns true
  // when something was selected. mouseEventsCaptured() no longer blocks this —
  // dispatchTerminalMouse() forces a local selection with Shift when needed.
  // Select the whitespace-delimited word under a point using xterm's select()
  // API directly. Unlike a synthetic mousedown, this never focuses xterm's
  // hidden textarea, so it can't pop the on-screen keyboard.
  function selectWordAt(clientX, clientY) {
    const screen = terminalScreenEl();
    if (!screen) {
      return false;
    }
    const cell = terminalCellSize();
    const rect = screen.getBoundingClientRect();
    let col = Math.floor((clientX - rect.left) / cell.width);
    const viewportRow = Math.floor((clientY - rect.top) / cell.height);
    if (viewportRow < 0 || viewportRow >= term.rows) {
      return false;
    }
    const buffer = term.buffer.active;
    const absRow = viewportRow + buffer.viewportY;
    const line = buffer.getLine(absRow);
    if (!line) {
      return false;
    }
    const text = line.translateToString(true); // trims trailing blanks
    if (col < 0) {
      col = 0;
    }
    if (col >= text.length || /\s/.test(text[col])) {
      return false; // tapped past the content or on whitespace → no word
    }
    let start = col;
    let end = col;
    while (start > 0 && !/\s/.test(text[start - 1])) {
      start -= 1;
    }
    while (end < text.length - 1 && !/\s/.test(text[end + 1])) {
      end += 1;
    }
    term.select(start, absRow, end - start + 1);
    if (!terminalHasSelection()) {
      return false;
    }
    suppressNextTerminalClick = true;
    hapticPulse();
    scheduleTerminalSelectionUISync(true);
    return true;
  }

  // Arm the long-press timer on a fresh single-finger contact.
  function beginTerminalSelectionPress(touch) {
    cancelTerminalSelectionPress();
    termSel = {
      startX: touch.clientX,
      startY: touch.clientY,
      lastX: touch.clientX,
      lastY: touch.clientY,
      active: false,
      draggingHandle: null,
      timer: null,
    };
    termSel.timer = window.setTimeout(activateTerminalSelection, TERM_LONGPRESS_MS);
  }

  // Long-press fired: word-select under the finger and keep the drag open so a
  // continued move extends the selection.
  function activateTerminalSelection() {
    if (!termSel || termSel.draggingHandle) {
      return;
    }
    termSel.timer = null;
    termSel.active = true;
    touchScrollState = null; // this contact is a selection, not a scroll
    cancelTouchInertia();
    hapticPulse();
    // Word-select under the finger as the anchor, keeping the drag "down" so a
    // continued move extends the selection.
    dispatchTerminalMouse("mousedown", termSel.startX, termSel.startY, 2);
    beginSelectionDragFeedback({ x: termSel.startX, y: termSel.startY });
  }

  function finishTerminalSelectionPress() {
    if (!termSel || termSel.draggingHandle) {
      return;
    }
    if (termSel.timer) {
      window.clearTimeout(termSel.timer);
    }
    if (termSel.active) {
      const point = clampTerminalSelectionDragPoint(termSel.lastX, termSel.lastY);
      dispatchTerminalMouse("mouseup", point.x, point.y, 1);
      endSelectionDragFeedback();
      suppressNextTerminalClick = true;
      scheduleTerminalSelectionUISync(true);
    }
    termSel = null;
  }

  function cancelTerminalSelectionPress() {
    if (termSel && !termSel.draggingHandle) {
      if (termSel.timer) {
        window.clearTimeout(termSel.timer);
      }
      endSelectionDragFeedback();
      termSel = null;
    }
  }

  function persistActiveSession(sessionName) {
    const key = profileStateKey("active-session") || STORAGE_ACTIVE_SESSION_KEY;
    if (!sessionName) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, sessionName);
  }

  // True once the server has sent its per-user open-tab set on this
  // connection; from then on every local change is pushed back so the user's
  // tabs follow them across devices.
  let openTabsServerSync = false;

  function pushOpenTabsToServer() {
    if (openTabsServerSync) {
      sendMessage({ type: "open-tabs", tabs: openTabNames });
    }
  }

  function persistOpenTabs() {
    const key = profileStateKey("open-tabs") || STORAGE_OPEN_TABS_KEY;
    if (!openTabNames.length) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify(openTabNames));
  }

  function setOpenTabs(nextNames) {
    openTabNames = Array.from(
      new Set(
        nextNames
          .map((value) => String(value || "").trim())
          .filter(Boolean),
      ),
    );
    persistOpenTabs();
    pushOpenTabsToServer();
  }

  function addOpenTab(sessionName) {
    if (!sessionName) {
      return;
    }
    if (openTabNames.includes(sessionName)) {
      return;
    }
    setOpenTabs([...openTabNames, sessionName]);
  }

  function removeOpenTab(sessionName) {
    if (!sessionName) {
      return;
    }
    setOpenTabs(openTabNames.filter((name) => name !== sessionName));
  }

  function replaceOpenTabName(previousName, nextName) {
    if (!previousName || !nextName || previousName === nextName) {
      if (nextName) {
        addOpenTab(nextName);
      }
      return;
    }
    setOpenTabs(openTabNames.map((name) => (name === previousName ? nextName : name)));
  }

  function syncOpenTabsToSessions() {
    const editorTabViews = editorTabs.map((tab) => ({
      type: "editor",
      key: editorTabKey(tab.id),
      id: tab.id,
      name: tab.name || pathBaseName(tab.root),
      root: tab.root,
      active: false,
    }));

    if (!currentSessions.length) {
      const fallbackTabs = openTabNames.length
        ? openTabNames
        : activeSessionName
          ? [activeSessionName]
          : [];
      const terminalTabs = fallbackTabs.map((name) => ({
        type: "terminal",
        key: terminalTabKey(name),
        name,
        active: false,
        attached: 0,
        windows: 0,
      }));
      currentTabs = [...terminalTabs, ...editorTabViews];
      if (!currentTabs.some((tab) => tab.key === activeTabKey)) {
        activeTabKey = terminalTabs[0]?.key || editorTabViews[0]?.key || "";
      }
      currentTabs.forEach((tab) => {
        tab.active = tab.key === activeTabKey;
      });
      renderTabs();
      renderActiveSurface();
      return;
    }

    const liveNames = new Set(currentSessions.map((session) => session.name));
    const nextOpenTabs = openTabNames.filter((name) => liveNames.has(name));
    const preferredName =
      (selectedSessionName && liveNames.has(selectedSessionName) && selectedSessionName) ||
      (activeSessionName && liveNames.has(activeSessionName) && activeSessionName) ||
      currentSessions[0]?.name ||
      "";

    if (preferredName && !nextOpenTabs.includes(preferredName)) {
      nextOpenTabs.push(preferredName);
    }

    const changed =
      nextOpenTabs.length !== openTabNames.length ||
      nextOpenTabs.some((name, index) => name !== openTabNames[index]);
    if (changed) {
      setOpenTabs(nextOpenTabs);
    }

    const sessionByName = new Map(currentSessions.map((session) => [session.name, session]));
    const terminalTabs = openTabNames
      .map((name) => {
        const session = sessionByName.get(name);
        if (!session) {
          return null;
        }
        return {
          type: "terminal",
          key: terminalTabKey(name),
          name,
          label: session.label || name,
          active: false,
          attached: session.attached,
          windows: session.windows,
        };
      })
      .filter(Boolean);

    currentTabs = [...terminalTabs, ...editorTabViews];
    if (!currentTabs.some((tab) => tab.key === activeTabKey)) {
      activeTabKey =
        terminalTabs.find((tab) => tab.name === activeSessionName)?.key ||
        terminalTabs[0]?.key ||
        editorTabViews[0]?.key ||
        "";
    }
    currentTabs.forEach((tab) => {
      tab.active = tab.key === activeTabKey;
    });
    renderTabs();
    renderActiveSurface();
  }

  function renderActiveSurface() {
    const active = activeTab();
    const editorActive = active?.type === "editor";
    const btopActive =
      !editorActive && active?.type === "terminal" && isBtopSession(active.name);
    document.body.dataset.activeSurface = editorActive ? "editor" : "terminal";
    terminalPanel.classList.toggle("hidden", editorActive);
    fileWorkspace.classList.toggle("hidden", !editorActive);
    shortcutsPanel.classList.toggle("hidden", editorActive);
    // A btop tab is a terminal, but with its own chrome (no composer, digit
    // keys instead of shortcuts) and independent auto-scaling.
    if (btopActive) {
      enterBtopMode();
    } else {
      exitBtopMode();
    }
    if (editorActive) {
      if (mobileComposerMode) {
        if (document.activeElement === composerInput) {
          composerInput.blur();
        }
        setComposerActive(false);
        composerPanel.classList.add("hidden");
      }
      document.documentElement.style.setProperty("--shortcut-height", "0px");
      document.documentElement.style.setProperty("--shortcut-reserve", "0px");
      renderFileWorkspace();
      const tab = activeEditorTab();
      const rootNode = tab ? tab.tree[tab.root] : null;
      if (tab && !rootNode?.loaded && !rootNode?.error && !tab.loadingPath) {
        requestFileList(tab, tab.root);
      }
      const file = activeOpenFile(tab);
      if (tab && file && !file.loaded && tab.loadingPath !== file.path) {
        requestFileRead(tab, file.path);
      }
      return;
    }
    shortcutsPanel.classList.remove("hidden");
    renderFileWorkspace();
    scheduleLayoutRefresh();
  }

  function longestCommonPrefixLength(left, right) {
    const limit = Math.min(left.length, right.length);
    let index = 0;
    while (index < limit && left[index] === right[index]) {
      index += 1;
    }
    return index;
  }

  function resetSpeechInputState() {
    window.clearTimeout(speechFlushTimer);
    speechInputState = {
      lastPhrase: "",
      lastAt: 0,
    };
  }

  function applySpeechPhrase(nextPhrase) {
    const now = Date.now();
    const previousPhrase = now - speechInputState.lastAt < 5000 ? speechInputState.lastPhrase : "";
    const prefixLength = longestCommonPrefixLength(previousPhrase, nextPhrase);
    const deleteCount = previousPhrase.length - prefixLength;
    if (deleteCount > 0) {
      sendMessage({ type: "input", data: "\u007f".repeat(deleteCount) });
    }
    const suffix = nextPhrase.slice(prefixLength);
    if (suffix) {
      sendMessage({ type: "input", data: suffix });
    }
    speechInputState = {
      lastPhrase: nextPhrase,
      lastAt: now,
    };
  }

  function queueSpeechPhrase(nextPhrase, delay = 60) {
    window.clearTimeout(speechFlushTimer);
    speechFlushTimer = window.setTimeout(() => {
      applySpeechPhrase(nextPhrase);
    }, delay);
  }

  function autoSizeComposer() {
    if (!mobileComposerMode) {
      return;
    }
    const previousHeight = lastComposerHeight || Math.ceil(composerInput.getBoundingClientRect().height);
    composerInput.style.height = "auto";
    const style = window.getComputedStyle(composerInput);
    const borderHeight =
      Number.parseFloat(style.borderTopWidth || "0") + Number.parseFloat(style.borderBottomWidth || "0");
    const nextHeight = Math.ceil(Math.min(composerInput.scrollHeight + borderHeight, window.innerHeight * 0.34));
    composerInput.style.height = `${nextHeight}px`;
    if (Math.abs(nextHeight - previousHeight) > 1) {
      lastComposerHeight = nextHeight;
      scheduleLayoutRefresh({ preserveTerminalCols: true });
    }
  }

  function setComposerActive(active) {
    if (!mobileComposerMode) {
      return;
    }
    document.body.dataset.composerActive = active ? "true" : "false";
    scheduleLayoutRefresh({ preserveTerminalCols: true });
    window.requestAnimationFrame(() => {
      scheduleLayoutRefresh({ preserveTerminalCols: true });
    });
  }

  function wsUrl() {
    const url = new URL("/_ws", window.location.href);
    if (activeProfileId) {
      url.searchParams.set("profile", activeProfileId);
    }
    if (selectedSessionName) {
      url.searchParams.set("session", selectedSessionName);
    }
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  function stopAuthConfigPolling() {
    if (authConfigPollTimer) {
      window.clearTimeout(authConfigPollTimer);
      authConfigPollTimer = 0;
    }
  }

  function scheduleAuthConfigPolling() {
    if (authConfigPollTimer) {
      return;
    }
    authConfigPollTimer = window.setTimeout(async () => {
      authConfigPollTimer = 0;
      const savedToken = localStorage.getItem(tokenStorageKey());
      if (!loginOverlay || loginOverlay.classList.contains("hidden") || savedToken) {
        return;
      }
      await loadServerConfig();
      if (!serverConfig.requireToken) {
        loginOverlay.classList.add("hidden");
        loginMessage.textContent = "";
        connect();
        return;
      }
      scheduleAuthConfigPolling();
    }, 1500);
  }

  function openComposer(focus = true) {
    if (!mobileComposerMode) {
      return;
    }
    // btop tabs have no prompt and keep the keyboard hidden.
    if (btopMode) {
      return;
    }
    composerPanel.classList.remove("hidden");
    autoSizeComposer();
    if (!focus) {
      return;
    }
    composerInput.focus({ preventScroll: true });
    const cursor = composerInput.selectionEnd ?? composerInput.value.length;
    composerInput.setSelectionRange(cursor, cursor);
  }

  function closeComposer() {
    if (!mobileComposerMode) {
      return;
    }
    composerInput.blur();
    setComposerActive(false);
    composerPanel.classList.add("hidden");
  }

  function setComposerValue(value, cursor = null) {
    if (!mobileComposerMode) {
      return;
    }
    const nextValue = String(value || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const numericCursor = Number.isFinite(Number(cursor)) ? Number(cursor) : nextValue.length;
    const nextCursor = Math.max(0, Math.min(nextValue.length, numericCursor));
    suppressComposerSync = true;
    composerInput.value = nextValue;
    autoSizeComposer();
    try {
      composerInput.setSelectionRange(nextCursor, nextCursor);
    } catch (_error) {
      // Ignore transient selection errors while the textarea is unfocused.
    }
    window.requestAnimationFrame(() => {
      suppressComposerSync = false;
    });
  }

  function syncComposerState() {
    if (!mobileComposerMode || suppressComposerSync) {
      return;
    }
    sendMessage({
      type: "composer-sync",
      value: composerInput.value,
      cursor: composerInput.selectionEnd ?? composerInput.value.length,
      revision: nextComposerRevision(),
    });
  }

  function clearComposer(sync = false) {
    if (!mobileComposerMode) {
      return;
    }
    setComposerValue("", 0);
    composerInput.style.height = "";
    resetSpeechInputState();
    if (sync) {
      syncComposerState();
    }
  }

  function forceClearComposer() {
    if (!mobileComposerMode) {
      return;
    }
    const wasFocused = composerHasKeyboardFocus();
    clearComposer(false);
    sendMessage({ type: "composer-force-clear", revision: nextComposerRevision() });
    restoreShortcutKeyboardState(wasFocused);
  }

  function flushComposerText() {
    if (!mobileComposerMode) {
      return "";
    }
    return composerInput.value;
  }

  function commitComposerLine() {
    if (!mobileComposerMode) {
      sendMessage({ type: "input", data: "\r" });
      return;
    }
    sendMessage({ type: "composer-enter", revision: nextComposerRevision() });
    clearComposer(false);
    openComposer(true);
  }

  function resetComposerTracking(clearValue = false) {
    if (!mobileComposerMode) {
      return;
    }
    sendMessage({ type: "composer-reset", revision: nextComposerRevision() });
    if (clearValue) {
      clearComposer(false);
    }
  }

  function navigateComposerHistory(direction, focus = true) {
    if (!mobileComposerMode) {
      sendMessage({ type: "input", data: direction === "down" ? specialMap.DOWN : specialMap.UP });
      return;
    }
    openComposer(focus);
    sendMessage({ type: "input", data: direction === "down" ? specialMap.DOWN : specialMap.UP });
    if (!semanticPromptState.seenMarker) {
      queueTerminalBufferComposerSync(true);
    }
  }

  function requestComposerRefresh() {
    if (!mobileComposerMode) {
      return;
    }
    userRefreshExpected = true;
    if (semanticPromptState.seenMarker) {
      scheduleSemanticComposerSync(true);
      return;
    }
    queueTerminalBufferComposerSync(true);
  }

  function nextComposerRevision() {
    composerRevision += 1;
    return composerRevision;
  }

  function resetComposerRevisionState() {
    composerRevision = 0;
    latestAppliedComposerRevision = 0;
  }

  function semanticTrackingActive() {
    return semanticPromptState.seenMarker && semanticPromptState.commandActive && semanticPromptState.commandStart;
  }

  function currentSemanticCursorPosition() {
    const buffer = term.buffer.active;
    return {
      x: buffer.cursorX,
      y: buffer.baseY + buffer.cursorY,
    };
  }

  function lineTextForRange(buffer, lineIndex, startColumn, endColumn = null) {
    const line = buffer.getLine(lineIndex);
    if (!line) {
      return "";
    }
    const nextLine = buffer.getLine(lineIndex + 1);
    const trimRight = !(nextLine && nextLine.isWrapped) && endColumn === null;
    return line.translateToString(trimRight, startColumn, endColumn === null ? undefined : endColumn);
  }

  function stripLeadingPromptDecor(value, cursor) {
    let nextValue = String(value || "");
    let nextCursor = Number.isFinite(Number(cursor)) ? Number(cursor) : nextValue.length;
    const patterns = [/^\s*[›❯]\s?/, /^\s*[$#>]\s?/];
    for (const pattern of patterns) {
      const match = nextValue.match(pattern);
      if (!match) {
        continue;
      }
      const prefix = match[0];
      nextValue = nextValue.slice(prefix.length);
      nextCursor = Math.max(0, nextCursor - prefix.length);
      break;
    }
    return {
      value: nextValue,
      cursor: Math.min(nextValue.length, nextCursor),
    };
  }

  function wrappedLogicalLineBounds(buffer, absoluteY) {
    let startY = absoluteY;
    while (startY > 0) {
      const line = buffer.getLine(startY);
      if (!line || !line.isWrapped) {
        break;
      }
      startY -= 1;
    }
    let endY = absoluteY;
    while (true) {
      const nextLine = buffer.getLine(endY + 1);
      if (!nextLine || !nextLine.isWrapped) {
        break;
      }
      endY += 1;
    }
    return { startY, endY };
  }

  function getBufferCell(line, column, cell) {
    if (!line) {
      return null;
    }
    return line.getCell(column, cell) || null;
  }

  function getCellChars(line, column, cell) {
    const nextCell = getBufferCell(line, column, cell);
    return nextCell ? nextCell.getChars() : "";
  }

  function isVisibleDefaultPromptCell(cell) {
    if (!cell || cell.isInvisible() || cell.isDim() || !cell.isFgDefault()) {
      return false;
    }
    const chars = cell.getChars();
    return Boolean(chars) && /\S/.test(chars);
  }

  function isBlankPromptCell(cell) {
    if (!cell || cell.isInvisible() || cell.isDim()) {
      return false;
    }
    const chars = cell.getChars();
    if (chars === "") {
      return true;
    }
    return /^\s+$/.test(chars);
  }

  function flatIndexToBufferPosition(flatIndex, startY) {
    return {
      y: startY + Math.floor(flatIndex / term.cols),
      x: flatIndex % term.cols,
    };
  }

  function flattenRegionCells(buffer, startY, endY, startX, endX) {
    const nullCell = buffer.getNullCell();
    const cells = [];
    for (let lineIndex = startY; lineIndex <= endY; lineIndex += 1) {
      const line = buffer.getLine(lineIndex);
      for (let column = startX; column < endX; column += 1) {
        const cell = getBufferCell(line, column, nullCell);
        const chars = cell ? cell.getChars() : "";
        cells.push({
          chars,
          input: isVisibleDefaultPromptCell(cell),
          blank: isBlankPromptCell(cell),
        });
      }
    }
    return cells;
  }

  function flattenWrappedPromptCells(buffer, startY, endY) {
    return flattenRegionCells(buffer, startY, endY, 0, term.cols);
  }

  function extractPromptSpanFromCells(cells, cursorFlatIndex) {
    const searchFrom = Math.max(0, Math.min(cells.length, cursorFlatIndex));
    let anchorIndex = -1;
    for (let index = searchFrom - 1; index >= 0; index -= 1) {
      if (cells[index].input) {
        anchorIndex = index;
        break;
      }
      if (!cells[index].blank) {
        break;
      }
    }
    if (anchorIndex < 0) {
      return null;
    }

    let startIndex = anchorIndex;
    while (startIndex > 0 && (cells[startIndex - 1].input || cells[startIndex - 1].blank)) {
      startIndex -= 1;
    }
    let endIndex = anchorIndex;
    while (endIndex + 1 < cells.length && (cells[endIndex + 1].input || cells[endIndex + 1].blank)) {
      endIndex += 1;
    }

    while (startIndex <= endIndex && cells[startIndex].blank) {
      startIndex += 1;
    }
    while (endIndex >= startIndex && cells[endIndex].blank) {
      endIndex -= 1;
    }
    if (startIndex > endIndex) {
      return null;
    }

    return { startIndex, endIndex };
  }

  function extractPromptSpanForRow(buffer, row, startX, endX) {
    const line = buffer.getLine(row);
    const nullCell = buffer.getNullCell();
    if (!line) {
      return null;
    }

    let firstInput = -1;
    let lastInput = -1;
    for (let column = startX; column < endX; column += 1) {
      const cell = getBufferCell(line, column, nullCell);
      if (!isVisibleDefaultPromptCell(cell)) {
        continue;
      }
      if (firstInput < 0) {
        firstInput = column;
      }
      lastInput = column;
    }
    if (firstInput < 0 || lastInput < 0) {
      return null;
    }

    while (firstInput > startX) {
      const cell = getBufferCell(line, firstInput - 1, nullCell);
      if (!isBlankPromptCell(cell)) {
        break;
      }
      firstInput -= 1;
    }
    while (lastInput + 1 < endX) {
      const cell = getBufferCell(line, lastInput + 1, nullCell);
      if (!isBlankPromptCell(cell)) {
        break;
      }
      lastInput += 1;
    }

    return {
      start: firstInput,
      end: lastInput + 1,
    };
  }

  function promptSpansOverlap(left, right) {
    if (!left || !right) {
      return false;
    }
    return Math.max(left.start, right.start) <= Math.min(left.end, right.end) + 2;
  }

  function findPromptRowRange(rowSpans, anchorIndex) {
    let effectiveAnchor = anchorIndex;
    if (!rowSpans[effectiveAnchor]) {
      let nearestDistance = Number.POSITIVE_INFINITY;
      for (let index = 0; index < rowSpans.length; index += 1) {
        if (!rowSpans[index]) {
          continue;
        }
        const distance = Math.abs(index - anchorIndex);
        if (distance >= nearestDistance) {
          continue;
        }
        nearestDistance = distance;
        effectiveAnchor = index;
      }
    }
    if (!rowSpans[effectiveAnchor]) {
      return null;
    }

    let firstRow = effectiveAnchor;
    let lastRow = effectiveAnchor;
    let previousSpan = rowSpans[effectiveAnchor];
    for (let index = effectiveAnchor - 1; index >= 0; index -= 1) {
      const span = rowSpans[index];
      if (!span || !promptSpansOverlap(span, previousSpan)) {
        break;
      }
      firstRow = index;
      previousSpan = span;
    }

    previousSpan = rowSpans[effectiveAnchor];
    for (let index = effectiveAnchor + 1; index < rowSpans.length; index += 1) {
      const span = rowSpans[index];
      if (!span || !promptSpansOverlap(span, previousSpan)) {
        break;
      }
      lastRow = index;
      previousSpan = span;
    }

    return {
      firstRow,
      lastRow,
      effectiveAnchor,
    };
  }

  function findPromptRowRangeInBox(rowSpans, anchorIndex) {
    let firstRow = -1;
    let lastRow = -1;
    for (let index = 0; index < rowSpans.length; index += 1) {
      if (!rowSpans[index]) {
        continue;
      }
      if (firstRow < 0) {
        firstRow = index;
      }
      lastRow = index;
    }
    if (firstRow < 0 || lastRow < 0) {
      return null;
    }
    return {
      firstRow,
      lastRow,
      effectiveAnchor: Math.max(firstRow, Math.min(lastRow, anchorIndex)),
    };
  }

  function extractRegionPromptState(buffer, startY, endY, startX, endX, current, boxed = false) {
    const rowSpans = [];
    for (let row = startY; row <= endY; row += 1) {
      rowSpans.push(extractPromptSpanForRow(buffer, row, startX, endX));
    }

    const anchorIndex = Math.max(0, Math.min(rowSpans.length - 1, current.y - startY));
    const rowRange = boxed ? findPromptRowRangeInBox(rowSpans, anchorIndex) : findPromptRowRange(rowSpans, anchorIndex);
    if (!rowRange) {
      return {
        value: "",
        cursor: 0,
        tracked: true,
        debug: {
          mode: boxed ? "boxed-empty" : "rows-empty",
          cursorY: current.y,
          cursorX: current.x,
          startY,
          endY,
          rowSpans,
          valuePreview: "",
        },
      };
    }
    const { firstRow, lastRow, effectiveAnchor } = rowRange;

    let value = "";
    for (let rowIndex = firstRow; rowIndex <= lastRow; rowIndex += 1) {
      if (rowIndex > firstRow) {
        value += "\n";
      }
      const span = rowSpans[rowIndex];
      if (!span) {
        continue;
      }
      value += lineTextForRange(buffer, startY + rowIndex, span.start, span.end);
    }

    let cursor = 0;
    const relativeCursorRow = Math.max(firstRow, Math.min(lastRow, current.y - startY));
    for (let rowIndex = firstRow; rowIndex <= relativeCursorRow; rowIndex += 1) {
      if (rowIndex > firstRow) {
        cursor += 1;
      }
      const span = rowSpans[rowIndex];
      if (!span) {
        continue;
      }
      const absoluteRow = startY + rowIndex;
      if (absoluteRow < current.y) {
        cursor += lineTextForRange(buffer, absoluteRow, span.start, span.end).length;
        continue;
      }
      if (absoluteRow > current.y) {
        break;
      }
      if (current.x <= span.start) {
        break;
      }
      const endColumn = Math.min(current.x, span.end);
      cursor += lineTextForRange(buffer, absoluteRow, span.start, endColumn).length;
      break;
    }

    if (current.y < startY + firstRow) {
      cursor = 0;
    } else if (current.y > startY + lastRow) {
      let tail = "";
      for (let rowIndex = firstRow; rowIndex <= lastRow; rowIndex += 1) {
        if (rowIndex > firstRow) {
          tail += "\n";
        }
        const span = rowSpans[rowIndex];
        if (!span) {
          continue;
        }
        tail += lineTextForRange(buffer, startY + rowIndex, span.start, span.end);
      }
      cursor = tail.length;
    } else if (current.y === startY + effectiveAnchor && !rowSpans[relativeCursorRow]) {
      cursor = value.length;
    }

    return {
      value,
      cursor,
      tracked: true,
      debug: {
        mode: boxed ? "boxed" : "rows",
        cursorY: current.y,
        cursorX: current.x,
        startY,
        endY,
        firstRowAbs: startY + firstRow,
        lastRowAbs: startY + lastRow,
        rowSpans,
        valuePreview: value,
      },
    };
  }

  function detectInputBox(buffer, cursorY, cursorX) {
    const nullCell = buffer.getNullCell();
    const currentLine = buffer.getLine(cursorY);
    if (!currentLine) {
      return null;
    }

    let leftBorder = -1;
    for (let column = cursorX - 1; column >= 0; column -= 1) {
      const chars = getCellChars(currentLine, column, nullCell);
      if (BOX_VERTICAL_CHARS.has(chars)) {
        leftBorder = column;
        break;
      }
    }

    let rightBorder = -1;
    for (let column = cursorX; column < term.cols; column += 1) {
      const chars = getCellChars(currentLine, column, nullCell);
      if (BOX_VERTICAL_CHARS.has(chars)) {
        rightBorder = column;
        break;
      }
    }

    if (leftBorder < 0 || rightBorder <= leftBorder + 1) {
      return null;
    }

    let topBorder = -1;
    for (let row = cursorY - 1; row >= Math.max(0, cursorY - 8); row -= 1) {
      const line = buffer.getLine(row);
      const leftChars = getCellChars(line, leftBorder, nullCell);
      const rightChars = getCellChars(line, rightBorder, nullCell);
      if (!BOX_TOP_LEFT_CHARS.has(leftChars) || !BOX_TOP_RIGHT_CHARS.has(rightChars)) {
        continue;
      }
      let horizontal = true;
      for (let column = leftBorder + 1; column < rightBorder; column += 1) {
        const chars = getCellChars(line, column, nullCell);
        if (chars !== "" && !BOX_HORIZONTAL_CHARS.has(chars)) {
          horizontal = false;
          break;
        }
      }
      if (horizontal) {
        topBorder = row;
        break;
      }
    }

    let bottomBorder = -1;
    for (let row = cursorY + 1; row <= Math.min(buffer.length - 1, cursorY + 8); row += 1) {
      const line = buffer.getLine(row);
      const leftChars = getCellChars(line, leftBorder, nullCell);
      const rightChars = getCellChars(line, rightBorder, nullCell);
      if (!BOX_BOTTOM_LEFT_CHARS.has(leftChars) || !BOX_BOTTOM_RIGHT_CHARS.has(rightChars)) {
        continue;
      }
      let horizontal = true;
      for (let column = leftBorder + 1; column < rightBorder; column += 1) {
        const chars = getCellChars(line, column, nullCell);
        if (chars !== "" && !BOX_HORIZONTAL_CHARS.has(chars)) {
          horizontal = false;
          break;
        }
      }
      if (horizontal) {
        bottomBorder = row;
        break;
      }
    }

    if (topBorder < 0 || bottomBorder <= topBorder + 1) {
      return null;
    }

    return {
      top: topBorder,
      bottom: bottomBorder,
      left: leftBorder,
      right: rightBorder,
    };
  }

  function extractBoxedComposerState(buffer, current) {
    const box = detectInputBox(buffer, current.y, current.x);
    if (!box) {
      return null;
    }

    const innerStartY = box.top + 1;
    const innerEndY = box.bottom - 1;
    const innerStartX = box.left + 1;
    const innerEndX = box.right;
    const innerWidth = Math.max(0, innerEndX - innerStartX);
    if (!innerWidth || innerEndY < innerStartY) {
      return null;
    }
    const regionState = extractRegionPromptState(buffer, innerStartY, innerEndY, innerStartX, innerEndX, current, true);
    return {
      ...regionState,
      debug: {
        ...(regionState.debug || {}),
        mode: "boxed",
        box,
      },
    };
  }

  function extractTerminalBufferComposerState() {
    const buffer = term.buffer.active;
    const current = currentSemanticCursorPosition();
    const currentLine = buffer.getLine(current.y);
    if (!currentLine) {
      return null;
    }

    const boxedState = extractBoxedComposerState(buffer, current);
    if (boxedState) {
      const normalizedBoxed = stripLeadingPromptDecor(boxedState.value, boxedState.cursor);
      updateExtractorDebugPanel({
        ...(boxedState.debug || {}),
        valuePreview: normalizedBoxed.value,
      });
      return {
        value: normalizedBoxed.value,
        cursor: normalizedBoxed.cursor,
        tracked: true,
      };
    }

    const { startY, endY } = wrappedLogicalLineBounds(buffer, current.y);
    const regionState = extractRegionPromptState(buffer, startY, endY, 0, term.cols, current);
    const normalized = stripLeadingPromptDecor(regionState.value, regionState.cursor);
    updateExtractorDebugPanel({
      ...(regionState.debug || {}),
      valuePreview: normalized.value,
    });
    return {
      value: normalized.value,
      cursor: normalized.cursor,
      tracked: true,
    };
  }

  function extractSemanticComposerState() {
    if (!semanticTrackingActive()) {
      return null;
    }
    const buffer = term.buffer.active;
    const start = semanticPromptState.commandStart;
    const current = currentSemanticCursorPosition();
    if (!start || current.y < start.y || (current.y === start.y && current.x < start.x)) {
      return { value: "", cursor: 0, tracked: true };
    }

    let endY = current.y;
    while (true) {
      const nextLine = buffer.getLine(endY + 1);
      if (!nextLine || !nextLine.isWrapped) {
        break;
      }
      endY += 1;
    }

    let value = "";
    let cursor = 0;
    for (let lineIndex = start.y; lineIndex <= endY; lineIndex += 1) {
      const line = buffer.getLine(lineIndex);
      if (!line) {
        break;
      }
      const startColumn = lineIndex === start.y ? start.x : 0;
      const prefix = lineIndex === start.y ? "" : line.isWrapped ? "" : "\n";
      value += prefix;
      if (lineIndex < current.y) {
        const segment = lineTextForRange(buffer, lineIndex, startColumn);
        value += segment;
        cursor = value.length;
        continue;
      }
      if (lineIndex === current.y) {
        const cursorSegment = lineTextForRange(buffer, lineIndex, startColumn, current.x);
        const lineRemainder = lineTextForRange(buffer, lineIndex, startColumn);
        value += lineRemainder;
        cursor = value.length - Math.max(0, lineRemainder.length - cursorSegment.length);
        continue;
      }
      value += lineTextForRange(buffer, lineIndex, startColumn);
    }

    return {
      value,
      cursor,
      tracked: true,
    };
  }

  function flushSemanticComposerState(force = false) {
    if (!mobileComposerMode) {
      return;
    }

    let nextState = extractSemanticComposerState();
    if (!nextState) {
      if (!force && semanticPromptState.lastSignature === "") {
        return;
      }
      nextState = { value: "", cursor: 0, tracked: false };
    }

    const signature = `${nextState.tracked ? "1" : "0"}:${nextState.cursor}:${nextState.value}`;
    if (!force && signature === semanticPromptState.lastSignature) {
      return;
    }

    // While the user is actively typing, the xterm buffer is one round-trip
    // behind the textarea. Yield to the local composer unless this was a
    // user-initiated refresh (history navigation, explicit refresh).
    const userInitiated = consumeUserRefreshExpectation();
    if (!userInitiated && shouldYieldToLocalComposer(nextState)) {
      return;
    }
    semanticPromptState.lastSignature = signature;

    if (nextState.tracked) {
      if (
        composerInput.value !== nextState.value ||
        (composerInput.selectionEnd ?? composerInput.value.length) !== nextState.cursor
      ) {
        setComposerValue(nextState.value, nextState.cursor);
      }
    } else if (composerInput.value !== "") {
      setComposerValue("", 0);
    }

    sendMessage({
      type: "composer-semantic-sync",
      value: nextState.value,
      cursor: nextState.cursor,
      tracked: nextState.tracked,
      revision: composerRevision,
      source: "semantic-osc133",
    });
  }

  function scheduleSemanticComposerSync(force = false) {
    if (!mobileComposerMode) {
      return;
    }
    semanticPromptState.forceSync = semanticPromptState.forceSync || force;
    if (semanticPromptState.syncScheduled) {
      return;
    }
    semanticPromptState.syncScheduled = true;
    window.requestAnimationFrame(() => {
      semanticPromptState.syncScheduled = false;
      const nextForce = semanticPromptState.forceSync;
      semanticPromptState.forceSync = false;
      flushSemanticComposerState(nextForce);
    });
  }

  function flushTerminalBufferComposerState(force = false) {
    if (!mobileComposerMode || semanticPromptState.seenMarker) {
      return;
    }

    const nextState = extractTerminalBufferComposerState();
    if (!nextState) {
      if (!force && terminalBufferSyncState.lastSignature === "") {
        return;
      }
      if (composerHasKeyboardFocus() && composerInput.value !== "" && !consumeUserRefreshExpectation()) {
        return;
      }
      terminalBufferSyncState.pending = false;
      terminalBufferSyncState.lastSignature = "0:0:";
      setComposerValue("", 0);
      sendMessage({
        type: "composer-semantic-sync",
        value: "",
        cursor: 0,
        tracked: false,
        revision: composerRevision,
        source: "terminal-buffer",
      });
      return;
    }

    const signature = `${nextState.cursor}:${nextState.value}`;
    if (!force && signature === terminalBufferSyncState.lastSignature) {
      return;
    }
    const userInitiated = consumeUserRefreshExpectation();
    if (!userInitiated && shouldYieldToLocalComposer(nextState)) {
      return;
    }
    terminalBufferSyncState.lastSignature = signature;
    terminalBufferSyncState.pending = false;
    if (
      composerInput.value !== nextState.value ||
      (composerInput.selectionEnd ?? composerInput.value.length) !== nextState.cursor
    ) {
      setComposerValue(nextState.value, nextState.cursor);
    }
    sendMessage({
      type: "composer-semantic-sync",
      value: nextState.value,
      cursor: nextState.cursor,
      tracked: true,
      revision: composerRevision,
      source: "terminal-buffer",
    });
  }

  function scheduleTerminalBufferComposerSync(force = false) {
    if (!mobileComposerMode || semanticPromptState.seenMarker) {
      return;
    }
    terminalBufferSyncState.forceSync = terminalBufferSyncState.forceSync || force;
    if (terminalBufferSyncState.syncScheduled) {
      return;
    }
    terminalBufferSyncState.syncScheduled = true;
    window.requestAnimationFrame(() => {
      terminalBufferSyncState.syncScheduled = false;
      const nextForce = terminalBufferSyncState.forceSync;
      terminalBufferSyncState.forceSync = false;
      flushTerminalBufferComposerState(nextForce);
    });
  }

  function queueTerminalBufferComposerSync(force = false) {
    if (!mobileComposerMode || semanticPromptState.seenMarker) {
      return;
    }
    terminalBufferSyncState.pending = true;
    terminalBufferSyncState.forceSync = terminalBufferSyncState.forceSync || force;
    terminalBufferSyncState.timers.forEach((timer) => window.clearTimeout(timer));
    terminalBufferSyncState.timers = TERMINAL_BUFFER_SYNC_DELAYS_MS.map((delay) =>
      window.setTimeout(() => {
        scheduleTerminalBufferComposerSync(force);
      }, delay),
    );
  }

  function installSemanticPromptHandlers() {
    if (!term.parser || typeof term.parser.registerOscHandler !== "function") {
      return;
    }
    term.parser.registerOscHandler(133, (data) => {
      const command = String(data || "").split(";", 1)[0].trim().toUpperCase();
      semanticPromptState.seenMarker = true;
      if (command === "B") {
        semanticPromptState.commandActive = true;
        semanticPromptState.commandStart = currentSemanticCursorPosition();
        semanticPromptState.lastSignature = "";
        scheduleSemanticComposerSync(true);
      } else if (command === "C" || command === "D") {
        semanticPromptState.commandActive = false;
        semanticPromptState.commandStart = null;
        semanticPromptState.lastSignature = "";
        scheduleSemanticComposerSync(true);
      }
      return false;
    });
  }

  function shortcutHistoryDirection(sequence) {
    const upper = sequence.trim().toUpperCase();
    if (upper === "{UP}") {
      return "up";
    }
    if (upper === "{DOWN}") {
      return "down";
    }
    return "";
  }

  function shortcutSupportsHoldRepeat(sequence) {
    const upper = sequence.trim().toUpperCase();
    return [
      "{BACKSPACE}",
      "{TAB}",
      "{BACKTAB}",
      "{SHIFT+TAB}",
      "{UP}",
      "{DOWN}",
      "{LEFT}",
      "{RIGHT}",
      "{HOME}",
      "{END}",
      "{PGUP}",
      "{PGDN}",
    ].includes(upper);
  }

  function shortcutShouldFlushComposer(sequence) {
    const upper = sequence.toUpperCase();
    return (
      upper.includes("{TAB}") ||
      upper.includes("{BACKTAB}") ||
      upper.includes("{SHIFT+TAB}") ||
      upper.includes("{ENTER}") ||
      upper.includes("{LEFT}") ||
      upper.includes("{RIGHT}") ||
      upper.includes("{HOME}") ||
      upper.includes("{END}") ||
      upper.includes("{PGUP}") ||
      upper.includes("{PGDN}") ||
      upper.includes("{TEXT:")
    );
  }


  function activeProfile() {
    return profiles.find((profile) => profile.id === activeProfileId) || null;
  }

  function updateProfileConnectionState() {
    profileButton.classList.toggle("is-offline", !socket || socket.readyState !== WebSocket.OPEN);
  }

  function loginRequiresToken() {
    const profileId = pendingProfileId || activeProfileId;
    const profile = profiles.find((item) => item.id === profileId);
    return profile?.requireToken ?? serverConfig.requireToken;
  }

  function loginSupportsPasskey() {
    const profileId = pendingProfileId || activeProfileId;
    const profile = profiles.find((item) => item.id === profileId);
    return Boolean(profile ? profile.deviceKeyAuth : serverConfig.passkeyAuth);
  }

  function applyActiveProfile() {
    const profile = activeProfile();
    updateProfileConnectionState();
    profileBanner.classList.toggle("hidden", !serverConfig.profileMode || !profile);
    if (!profile) {
      return;
    }
    const accent = profile.accent || "#ffd166";
    document.documentElement.style.setProperty("--profile-accent", accent);
    document.documentElement.style.setProperty("--accent", accent);
    profileDot.style.background = accent;
    profileLabel.textContent = profile.label || profile.id;
    const down = profile.available === false || profile.status === "down";
    profileStatus.classList.toggle("is-down", down);
    profileStatus.textContent = down ? profile.statusMessage || "Unavailable" : "Connected";
    profileButton.setAttribute("aria-expanded", profileMenuOpen ? "true" : "false");
  }

  function updateProfileInventory(nextProfiles, nextActiveProfile = activeProfileId) {
    if (Array.isArray(nextProfiles)) {
      profiles = nextProfiles;
    }
    if (nextActiveProfile && nextActiveProfile !== activeProfileId) {
      const previousProfileId = activeProfileId;
      activeProfileId = nextActiveProfile;
      localStorage.setItem(STORAGE_ACTIVE_PROFILE_KEY, activeProfileId);
      if (!pendingProfileId) {
        loginRealm = activeProfile()?.authRealm || "";
        applyAuthenticationScope(loginRealm);
        refreshDeviceKeyFlag(loginRealm);
      }
      loadActiveProfileState(previousProfileId);
    }
    applyActiveProfile();
    if (profileMenuOpen) {
      renderProfileMenu();
      positionProfileMenu();
    }
  }

  function migrateLegacyProfileState(profileId) {
    const activeKey = profileStateKey("active-session", profileId);
    const openTabsKey = profileStateKey("open-tabs", profileId);
    const editorTabsKey = profileStateKey("editor-tabs", profileId);
    if (activeKey && localStorage.getItem(activeKey) === null) {
      const legacyActive = localStorage.getItem(STORAGE_ACTIVE_SESSION_KEY);
      if (legacyActive) localStorage.setItem(activeKey, legacyActive);
    }
    if (openTabsKey && localStorage.getItem(openTabsKey) === null) {
      const legacyTabs = localStorage.getItem(STORAGE_OPEN_TABS_KEY);
      if (legacyTabs) localStorage.setItem(openTabsKey, legacyTabs);
    }
    if (editorTabsKey && localStorage.getItem(editorTabsKey) === null) {
      const legacyEditorTabs = localStorage.getItem(STORAGE_EDITOR_TABS_KEY);
      if (legacyEditorTabs) {
        localStorage.setItem(editorTabsKey, legacyEditorTabs);
        editorTabsByProfile.delete(profileId || "single");
      }
    }
  }

  function loadActiveProfileState(previousProfileId = activeProfileId) {
    if (previousProfileId !== activeProfileId) {
      editorTabsByProfile.set(previousProfileId || "single", editorTabs);
    }
    cancelPendingFileRequests();
    closeFileRootPicker();
    selectedSessionName = loadActiveSession();
    activeSessionName = "";
    activeTabKey = selectedSessionName ? terminalTabKey(selectedSessionName) : "";
    openTabNames = loadOpenTabs();
    const editorProfileId = activeProfileId || "single";
    editorTabs = editorTabsByProfile.get(editorProfileId) || loadEditorTabs();
    editorTabsByProfile.set(editorProfileId, editorTabs);
    fileBookmarks = activeProfileId
      ? normalizeFileBookmarks(fileBookmarksByProfile[activeProfileId])
      : fileBookmarks;
    lastDefaultFileRoot = "";
    openTabsServerSync = false;
    currentSessions = [];
    currentTabs = [];
  }

  async function loadServerConfig() {
    try {
      const response = await fetch("/config", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`config ${response.status}`);
      }
      serverConfig = await response.json();
    } catch (_error) {
      serverConfig = {
        requireToken: true,
        tailscaleMode: false,
        allowedClients: [],
        multiTenant: false,
        profileMode: false,
        profiles: [],
        deviceKeyAuth: false,
        passkeyAuth: false,
      };
    }
    profiles = Array.isArray(serverConfig.profiles) ? serverConfig.profiles : [];
    if (serverConfig.profileMode && profiles.length) {
      const saved = localStorage.getItem(STORAGE_ACTIVE_PROFILE_KEY) || "";
      const nextProfile = profiles.some((profile) => profile.id === saved)
        ? saved
        : serverConfig.activeProfile || profiles[0].id;
      const changed = nextProfile !== activeProfileId;
      const previousProfileId = activeProfileId;
      activeProfileId = nextProfile;
      localStorage.setItem(STORAGE_ACTIVE_PROFILE_KEY, activeProfileId);
      if (activeProfileId === serverConfig.activeProfile) {
        migrateLegacyProfileState(activeProfileId);
      }
      loginRealm = activeProfile()?.authRealm || "";
      applyAuthenticationScope(loginRealm);
      if (changed || !activeSessionName) {
        loadActiveProfileState(previousProfileId);
      }
    } else {
      const previousProfileId = activeProfileId;
      const hadActiveProfile = Boolean(activeProfileId);
      profiles = [];
      activeProfileId = "";
      loginRealm = "";
      applyAuthenticationScope();
      localStorage.removeItem(STORAGE_ACTIVE_PROFILE_KEY);
      if (hadActiveProfile) {
        loadActiveProfileState(previousProfileId);
      }
    }
    authenticationButton?.classList.toggle(
      "hidden",
      !Boolean(serverConfig.passkeyAuth) && !profiles.some((profile) => profile.deviceKeyAuth),
    );
    applyActiveProfile();
    syncLoginFields();
  }

  function ensurePasskeyHelper() {
    if (window.MobileTerminalPasskeys) {
      return Promise.resolve(true);
    }
    const passkeysEnabled =
      Boolean(serverConfig.passkeyAuth) || profiles.some((profile) => profile.deviceKeyAuth);
    if (!passkeysEnabled) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = "/static/passkey.js";
      script.onload = () => resolve(Boolean(window.MobileTerminalPasskeys));
      script.onerror = () => resolve(false);
      document.head.appendChild(script);
    });
  }

  async function prepareAuthenticationClient() {
    const passkeyReady = await ensurePasskeyHelper();
    resumeHandlingReady = true;
    await resumeApplication();
    if (passkeyReady) {
      return true;
    }
    closeBootSocket();
    loginOverlay.classList.remove("hidden");
    loginMessage.textContent = "Passkey support could not be loaded. Refresh and try again.";
    return false;
  }

  function activeTab() {
    return currentTabs.find((tab) => tab.active) || currentTabs[0] || null;
  }

  function updateSessionInventory(sessions, nextActiveSession = activeSessionName) {
    currentSessions = Array.isArray(sessions) ? sessions : [];
    if (nextActiveSession) {
      activeSessionName = nextActiveSession;
      if (!isEditorTabKey(activeTabKey)) {
        activeTabKey = terminalTabKey(activeSessionName);
      }
    }
    syncOpenTabsToSessions();
    renderSessionMenu();
  }

  function reportActivity(force = false) {
    const now = performance.now();
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    if (force) {
      if (now - lastForcedActivityReportAt < FORCED_ACTIVITY_DEDUPE_MS) {
        return false;
      }
    } else if (now - lastActivityReportAt < ACTIVITY_REPORT_INTERVAL_MS) {
      return false;
    }
    socket.send(JSON.stringify(force ? { type: "activity", force: true } : { type: "activity" }));
    lastActivityReportAt = now;
    if (force) {
      lastForcedActivityReportAt = now;
    }
    return true;
  }

  function reportForcedActivity() {
    return reportActivity(true);
  }

  function sendMessage(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    if (
      payload &&
      [
        "composer-sync",
        "composer-enter",
        "composer-history",
        "composer-force-clear",
      ].includes(payload.type)
    ) {
      reportActivity();
    }
    if (payload && (payload.type === "input" || payload.type === "composer-sync" || payload.type === "composer-enter")) {
      followOutput = true;
    }
    socket.send(JSON.stringify(payload));
    return true;
  }

  function writeTerminal(data) {
    return new Promise((resolve) => term.write(data, resolve));
  }

  function terminalCursorStyle(meta) {
    const shape = String(meta.cursorShape || "default");
    const blinking = shape.startsWith("blinking-") || meta.cursorBlinking;
    if (shape.endsWith("underline")) return blinking ? 3 : 4;
    if (shape.endsWith("bar")) return blinking ? 5 : 6;
    return blinking ? 1 : 2;
  }

  function terminalReplayBaselineSequence() {
    return "\x1b[0m\x0f\x1b(B\x1b[?1l\x1b[?6l\x1b[?7l\x1b[4l\x1b[?25l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b>\x1b[r\x1b[H";
  }

  function terminalTabStopsSequence(meta) {
    let sequence = "\x1b[3g";
    for (const stop of Array.isArray(meta.tabStops) ? meta.tabStops : []) {
      sequence += `\x1b[${Number(stop) + 1}G\x1bH`;
    }
    return `${sequence}\x1b[H`;
  }

  function terminalModeSequence(meta) {
    let sequence = terminalReplayBaselineSequence();
    const scrollUpper = Number(meta.scrollUpper) || 0;
    const scrollLower = Number.isFinite(Number(meta.scrollLower))
      ? Number(meta.scrollLower)
      : Number(meta.rows) - 1;
    if (scrollUpper !== 0 || scrollLower !== Number(meta.rows) - 1) {
      sequence += `\x1b[${scrollUpper + 1};${scrollLower + 1}r`;
    }
    if (meta.insert) sequence += "\x1b[4h";
    if (meta.wrap) sequence += "\x1b[?7h";
    if (meta.keypadCursor) sequence += "\x1b[?1h";
    if (meta.keypad) sequence += "\x1b=";
    if (meta.mouseStandard) sequence += "\x1b[?1000h";
    if (meta.mouseButton) sequence += "\x1b[?1002h";
    if (meta.mouseAny) sequence += "\x1b[?1003h";
    if (meta.mouseSgr) sequence += "\x1b[?1006h";
    if (meta.origin) sequence += "\x1b[?6h";
    if (meta.cursorFlag) sequence += "\x1b[?25h";
    sequence += `\x1b[${terminalCursorStyle(meta)} q`;
    const cursorRow = meta.origin ? Number(meta.cursorY) - scrollUpper : Number(meta.cursorY);
    sequence += `\x1b[${cursorRow + 1};${Number(meta.cursorX) + 1}H`;
    return sequence;
  }

  async function applyTerminalSeed(payload) {
    const meta = payload.meta;
    if (!meta || !Array.isArray(payload.physicalRows)) {
      throw new Error("Invalid terminal seed");
    }
    decoder = new TextDecoder();
    term.resize(Number(meta.cols), Number(meta.rows));
    let reset = `\x1b[?1049l${terminalReplayBaselineSequence()}\x1b[3J\x1b[2J\x1b[H`;
    if (meta.alternate) {
      reset += `\x1b[?1049h${terminalReplayBaselineSequence()}\x1b[2J\x1b[H`;
    }
    await writeTerminal(reset);
    await writeTerminal(terminalTabStopsSequence(meta));
    await writeTerminal(payload.physicalRows.join("\r\n"));
    await writeTerminal(terminalModeSequence(meta));
    terminalEpoch = Number(payload.epoch);
    terminalPaneId = String(payload.paneId || meta.paneId || "");
    terminalCutoff = Number(payload.cutoff);
    terminalRevision = terminalCutoff;
    terminalLayoutGeneration = Number(payload.layoutGeneration);
    terminalSeedHistory = Number(meta.seedHistory) || 0;
    terminalHistory = Number(meta.history) || 0;
    pendingSeedScrollTarget = Number.isFinite(Number(payload.scrollTarget))
      ? Number(payload.scrollTarget)
      : null;
  }

  async function handleTerminalBinary(data) {
    const metadata = pendingTerminalOutput;
    pendingTerminalOutput = null;
    if (!metadata) {
      throw new Error("Terminal output arrived without metadata");
    }
    let chunk = data;
    if (chunk instanceof Blob) {
      chunk = await chunk.arrayBuffer();
    }
    const bytes = new Uint8Array(chunk);
    if (
      String(metadata.paneId || "") !== terminalPaneId ||
      Number(metadata.epoch) !== terminalEpoch ||
      Number(metadata.start) !== terminalRevision ||
      Number(metadata.end) !== terminalRevision + bytes.byteLength
    ) {
      terminalAuthoritative = false;
      throw new Error("Terminal output ordering changed");
    }
    if (!terminalAuthoritative && metadata.kind === "live") {
      terminalRevision = Number(metadata.end);
      return;
    }
    await writeTerminal(decoder.decode(bytes, { stream: true }));
    terminalRevision = Number(metadata.end);
    if (followOutput || performance.now() < bottomPinUntil) {
      term.scrollToBottom();
    }
    if (semanticPromptState.seenMarker) {
      scheduleSemanticComposerSync();
    }
    if (terminalHorizontallyOverflows()) {
      scheduleLayoutRefresh();
    }
  }

  function scheduleLayoutRefresh({ preserveTerminalCols = false } = {}) {
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(() => {
      measureShortcutHeight();
      // In a btop tab, re-derive the auto-scale font (rotation/resize changes
      // the pane) so it never falls under btop's minimum grid.
      if (btopMode) {
        applyBtopFont();
      } else {
        fitTerminal({ preserveCols: preserveTerminalCols });
      }
      positionTabMenu();
      positionSessionMenu();
      positionProfileMenu();
      positionSettingsMenu();
      positionAuxMenu();
      positionBtopTargetMenu();
      scheduleTerminalSelectionUISync();
    }, 40);
  }

  function performLayoutNow({ preserveTerminalCols = false } = {}) {
    window.clearTimeout(fitTimer);
    fitTimer = 0;
    measureShortcutHeight();
    if (btopMode) {
      applyBtopFont();
    } else {
      fitTerminal({ preserveCols: preserveTerminalCols });
    }
    positionTabMenu();
    positionSessionMenu();
    positionSettingsMenu();
    positionAuxMenu();
    positionBtopTargetMenu();
    scheduleTerminalSelectionUISync();
  }

  function refreshFollowOutput() {
    // While the post-switch bottom-pin is active, keep following the bottom even
    // though the snapshot/redraw writes momentarily move the viewport.
    if (performance.now() < bottomPinUntil) {
      followOutput = true;
      return;
    }
    const buffer = term.buffer.active;
    followOutput = buffer.viewportY >= buffer.baseY;
  }

  function terminalVisibleWidth() {
    const viewport = terminalElement.querySelector(".xterm-viewport");
    return Math.floor((viewport || terminalElement).getBoundingClientRect().width);
  }

  function terminalRenderedWidth() {
    const screen = terminalElement.querySelector(".xterm-screen");
    const canvases = terminalElement.querySelectorAll(".xterm-screen canvas");
    let width = screen ? Math.ceil(screen.getBoundingClientRect().width) : 0;
    canvases.forEach((canvas) => {
      width = Math.max(width, Math.ceil(canvas.getBoundingClientRect().width));
    });
    return width;
  }

  function terminalHorizontallyOverflows() {
    const visibleWidth = terminalVisibleWidth();
    const renderedWidth = terminalRenderedWidth();
    return visibleWidth > 0 && renderedWidth > visibleWidth + 1;
  }

  function clampTerminalColumnsToVisibleWidth() {
    let attempts = 0;
    while (terminalHorizontallyOverflows() && term.cols > 20 && attempts < 4) {
      term.resize(term.cols - 1, term.rows);
      attempts += 1;
    }
  }

  function fitTerminal({ preserveCols = false } = {}) {
    if (terminalPanel.classList.contains("hidden")) {
      return;
    }
    if (terminalFitScheduled) {
      pendingFitPreserveCols = pendingFitPreserveCols && preserveCols;
      return;
    }
    terminalFitScheduled = true;
    pendingFitPreserveCols = preserveCols;
    window.requestAnimationFrame(() => {
      terminalFitScheduled = false;
      // xterm caches cell dimensions inside its render service. After a tab
      // switch / viewport change those cached values can be stale (font may
      // have loaded after the initial measure, devicePixelRatio shifts, etc.),
      // making proposeDimensions return too few rows and leaving a gap below
      // the rendered content. Force a fresh measurement before fitting.
      try {
        term._core?._charSizeService?.measure?.();
      } catch (_error) {
        // Internal API — ignore if shape changes.
      }
      const terminalWidth = Math.ceil(terminalElement.getBoundingClientRect().width);
      const widthChanged = lastTerminalLayoutWidth > 0 && Math.abs(terminalWidth - lastTerminalLayoutWidth) > 1;
      const shouldPreserveCols = pendingFitPreserveCols && !widthChanged && !terminalHorizontallyOverflows();
      pendingFitPreserveCols = false;
      if (typeof fitAddon.proposeDimensions === "function") {
        const proposed = fitAddon.proposeDimensions();
        if (proposed && Number.isFinite(proposed.rows) && proposed.rows > 0) {
          const proposedCols = Number.isFinite(proposed.cols) ? Math.floor(proposed.cols) : term.cols;
          const guardedCols = Math.max(20, proposedCols - TERMINAL_COL_GUARD);
          const nextCols = shouldPreserveCols && lastTerminalCols > 0 ? lastTerminalCols : guardedCols;
          term.resize(nextCols, proposed.rows);
        } else {
          fitAddon.fit();
        }
      } else {
        fitAddon.fit();
      }
      clampTerminalColumnsToVisibleWidth();
      lastTerminalLayoutWidth = terminalWidth;
      if (term.cols !== lastTerminalCols || term.rows !== lastTerminalRows) {
        lastTerminalCols = term.cols;
        lastTerminalRows = term.rows;
        if (terminalAuthoritative) {
          sendMessage({ type: "resize", cols: term.cols, rows: term.rows });
        }
      }
      if (followOutput) {
        term.scrollToBottom();
      }
      if (term.rows > 0) {
        term.refresh(0, term.rows - 1);
      }
      scheduleTerminalSelectionUISync();
    });
  }

  function cancelTouchInertia() {
    if (touchInertiaFrameId !== null) {
      window.cancelAnimationFrame(touchInertiaFrameId);
      touchInertiaFrameId = null;
    }
    touchInertiaVelocity = 0;
    touchInertiaLastAt = 0;
  }

  function stepTouchInertia(timestamp) {
    if (!touchInertiaLastAt) {
      touchInertiaLastAt = timestamp;
      touchInertiaFrameId = window.requestAnimationFrame(stepTouchInertia);
      return;
    }
    const deltaMs = Math.min(TOUCH_INERTIA_MAX_FRAME_MS, Math.max(1, timestamp - touchInertiaLastAt));
    touchInertiaLastAt = timestamp;
    scrollTerminalByPixels(touchInertiaVelocity * deltaMs);
    touchInertiaVelocity *= Math.pow(TOUCH_INERTIA_FRICTION_PER_MS, deltaMs);
    if (Math.abs(touchInertiaVelocity) < TOUCH_INERTIA_MIN_VELOCITY) {
      cancelTouchInertia();
      return;
    }
    touchInertiaFrameId = window.requestAnimationFrame(stepTouchInertia);
  }

  function startTouchInertia(initialVelocity) {
    const clampedVelocity = Math.max(
      -TOUCH_INERTIA_MAX_VELOCITY,
      Math.min(TOUCH_INERTIA_MAX_VELOCITY, initialVelocity),
    );
    if (Math.abs(clampedVelocity) < TOUCH_INERTIA_MIN_VELOCITY) {
      cancelTouchInertia();
      return;
    }
    cancelTouchInertia();
    touchInertiaVelocity = clampedVelocity;
    touchInertiaLastAt = 0;
    touchInertiaFrameId = window.requestAnimationFrame(stepTouchInertia);
  }

  let pendingScrollLines = 0;
  let pendingScrollFrameId = null;
  let scrollRepaintTimer = null;

  function scheduleScrollRepaint() {
    if (scrollRepaintTimer !== null) {
      window.clearTimeout(scrollRepaintTimer);
    }
    scrollRepaintTimer = window.setTimeout(() => {
      scrollRepaintTimer = null;
      if (term.rows > 0) {
        term.refresh(0, term.rows - 1);
      }
    }, 80);
  }

  function flushPendingScrollLines() {
    pendingScrollFrameId = null;
    if (pendingScrollLines === 0) {
      return;
    }
    const clamped = Math.max(-48, Math.min(48, pendingScrollLines));
    pendingScrollLines -= clamped;
    if (pendingScrollLines !== 0) {
      pendingScrollFrameId = window.requestAnimationFrame(flushPendingScrollLines);
    }
    sendMessage({ type: "scroll-history", lines: clamped });
    if (clamped > 0) {
      followOutput = false;
    }
    scheduleScrollRepaint();
  }

  function queueScrollHistory(lines) {
    // A real user scroll releases the post-switch bottom-pin immediately.
    bottomPinUntil = 0;
    // Normal-buffer panes (shell, codex) keep their transcript in xterm's own
    // buffer -> scroll it locally, instantly, with no server round-trip. xterm's
    // onScroll updates followOutput. Positive `lines` = scroll up into history,
    // which is term.scrollLines(-lines).
    if (activePaneLocalScroll) {
      const before = term.buffer.active.viewportY;
      term.scrollLines(-lines);
      if (term.buffer.active.viewportY !== before || lines <= 0) {
        return;
      }
      if (
        historyReseedPending ||
        terminalSeedHistory >= terminalHistory ||
        terminalSeedHistory >= 20000
      ) {
        return;
      }
      const scrollTarget = before - terminalSeedHistory - lines;
      const historyLines = Math.min(
        20000,
        terminalHistory,
        Math.max(terminalSeedHistory + 2000, terminalSeedHistory * 2, -scrollTarget + term.rows),
      );
      historyReseedPending = sendMessage({
        type: "history-reseed",
        historyLines,
        scrollTarget,
      });
      return;
    }
    pendingScrollLines += lines;
    if (pendingScrollFrameId === null) {
      pendingScrollFrameId = window.requestAnimationFrame(flushPendingScrollLines);
    }
  }

  function scrollTerminalByPixels(pixelDelta) {
    if (!term.rows || !Number.isFinite(pixelDelta) || pixelDelta === 0) {
      return;
    }
    const lineHeight = term.options.fontSize * (term.options.lineHeight || 1);
    if (!lineHeight) {
      return;
    }
    const lineDelta = scrollLineRemainder + pixelDelta / lineHeight;
    if (Math.abs(lineDelta) < 0.35) {
      scrollLineRemainder = lineDelta;
      return;
    }
    const roundedLines = Math.round(lineDelta);
    const lines = Math.max(-12, Math.min(12, roundedLines));
    if (lines === 0) {
      scrollLineRemainder = lineDelta;
      return;
    }
    scrollLineRemainder = lineDelta - lines;
    queueScrollHistory(lines);
  }

  // --- Unified multi-touch gesture recognizer --------------------------------
  // Classifies a 2+ finger contact into a swipe (N-finger, per-move deltas so a
  // held finger doesn't fight a moving one), a multi-tap (counts short still
  // "pulses" that reach a peak finger count — this also catches "hold one finger
  // and double-tap with the other"), or a pinch (spread change with a still
  // midpoint). The matched gesture id is looked up in `gestureBindings` and its
  // sequence dispatched. Single-finger touches are left to the scroll/tap paths.
  function touchDistance(a, b) {
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  }

  function touchListCentroid(touchList) {
    let x = 0;
    let y = 0;
    for (let i = 0; i < touchList.length; i += 1) {
      x += touchList[i].clientX;
      y += touchList[i].clientY;
    }
    const n = Math.max(1, touchList.length);
    return { x: x / n, y: y / n };
  }

  // Repeat-per-distance only when a swipe is bound to a bare arrow key, so
  // swipe-to-arrow keeps its "swipe further = more presses" feel while every
  // other binding fires exactly once per swipe.
  function gestureSwipeIsArrow(sequence) {
    const expanded = expandShortcutSequence(sequence || "");
    return (
      expanded === specialMap.UP ||
      expanded === specialMap.DOWN ||
      expanded === specialMap.LEFT ||
      expanded === specialMap.RIGHT
    );
  }

  function registerGestureTouches(touchList, reanchor) {
    for (let i = 0; i < touchList.length; i += 1) {
      const touch = touchList[i];
      const pos = gestureState.positions.get(touch.identifier);
      if (!pos) {
        gestureState.positions.set(touch.identifier, { sx: touch.clientX, sy: touch.clientY });
      } else if (reanchor) {
        pos.sx = touch.clientX;
        pos.sy = touch.clientY;
      }
      if (!gestureState.last.has(touch.identifier)) {
        gestureState.last.set(touch.identifier, { x: touch.clientX, y: touch.clientY });
      }
    }
  }

  function beginGesture(touchList) {
    gestureState = {
      peak: touchList.length,
      positions: new Map(),
      last: new Map(),
      centroidStart: touchListCentroid(touchList),
      moved: false,
      rose: true,
      riseAt: performance.now(),
      mode: null, // null until movement commits to "swipe" or "pinch"
      axis: null,
      accX: 0,
      accY: 0,
      residual: 0,
      swipeFingers: 0,
      swipeLocked: false,
      pinchAnchor: touchList.length === 2 ? touchDistance(touchList[0], touchList[1]) : 0,
      handled: false, // true once a swipe/pinch fires (suppresses tap detection)
    };
    registerGestureTouches(touchList, false);
  }

  // touchstart with >= 2 fingers: engage the recognizer, or extend it when a new
  // finger joins or re-taps during a hold. Each rise re-anchors tap stillness so
  // a held finger's slow drift never disqualifies the other finger's taps.
  function gestureTouchStart(event) {
    const touchList = event.touches;
    if (touchList.length < 2) {
      return false;
    }
    if (!gestureState) {
      beginGesture(touchList);
    } else {
      gestureState.peak = Math.max(gestureState.peak, touchList.length);
      gestureState.rose = true;
      gestureState.riseAt = performance.now();
      gestureState.moved = false;
      registerGestureTouches(touchList, true);
      if (touchList.length === 2) {
        gestureState.pinchAnchor = touchDistance(touchList[0], touchList[1]);
      }
    }
    return true;
  }

  function gestureMaxAxisDelta(touchList, axis) {
    let best = 0;
    for (let i = 0; i < touchList.length; i += 1) {
      const touch = touchList[i];
      const last = gestureState.last.get(touch.identifier);
      if (!last) {
        continue;
      }
      const delta = axis === "x" ? touch.clientX - last.x : touch.clientY - last.y;
      if (Math.abs(delta) > Math.abs(best)) {
        best = delta;
      }
    }
    return best;
  }

  function gestureRememberLast(touchList) {
    for (let i = 0; i < touchList.length; i += 1) {
      const touch = touchList[i];
      const last = gestureState.last.get(touch.identifier);
      if (last) {
        last.x = touch.clientX;
        last.y = touch.clientY;
      } else {
        gestureState.last.set(touch.identifier, { x: touch.clientX, y: touch.clientY });
      }
    }
  }

  function swipeDirection(axis, positive) {
    if (axis === "x") {
      return positive ? "right" : "left";
    }
    return positive ? "down" : "up";
  }

  function handleGestureSwipe(touchList) {
    const dx = gestureMaxAxisDelta(touchList, "x");
    const dy = gestureMaxAxisDelta(touchList, "y");
    gestureRememberLast(touchList);
    if (!gestureState.axis) {
      gestureState.accX += dx;
      gestureState.accY += dy;
      if (Math.max(Math.abs(gestureState.accX), Math.abs(gestureState.accY)) < GESTURE_SWIPE_START) {
        return;
      }
      gestureState.axis = Math.abs(gestureState.accX) >= Math.abs(gestureState.accY) ? "x" : "y";
      gestureState.swipeFingers = Math.min(3, gestureState.peak);
      gestureState.residual = gestureState.axis === "x" ? gestureState.accX : gestureState.accY;
      gestureState.mode = "swipe";
    } else {
      gestureState.residual += gestureState.axis === "x" ? dx : dy;
    }
    gestureState.handled = true;

    const axis = gestureState.axis;
    const fingers = gestureState.swipeFingers;
    const committedSeq = gestureBindingSequence(
      `swipe${fingers}-${swipeDirection(axis, gestureState.residual > 0)}`,
    );
    if (committedSeq && gestureSwipeIsArrow(committedSeq)) {
      let fired = 0;
      while (Math.abs(gestureState.residual) >= GESTURE_SWIPE_STEP && fired < GESTURE_MAX_REPEAT) {
        const positive = gestureState.residual > 0;
        dispatchGestureSequence(
          gestureBindingSequence(`swipe${fingers}-${swipeDirection(axis, positive)}`),
        );
        gestureState.residual -= (positive ? 1 : -1) * GESTURE_SWIPE_STEP;
        fired += 1;
      }
      if (fired >= GESTURE_MAX_REPEAT) {
        gestureState.residual = 0;
      }
    } else if (!gestureState.swipeLocked && Math.abs(gestureState.residual) >= GESTURE_SWIPE_STEP) {
      // Non-arrow bindings fire exactly once, when the swipe is clearly committed.
      dispatchGestureSequence(committedSeq);
      gestureState.swipeLocked = true;
    }
  }

  function handleGesturePinch(touchList) {
    gestureState.handled = true;
    const dist = touchDistance(touchList[0], touchList[1]);
    let fired = 0;
    while (
      Math.abs(dist - gestureState.pinchAnchor) >= GESTURE_PINCH_STEP &&
      fired < GESTURE_MAX_REPEAT
    ) {
      const out = dist - gestureState.pinchAnchor > 0;
      dispatchGestureSequence(gestureBindingSequence(out ? "pinch-out" : "pinch-in"));
      gestureState.pinchAnchor += (out ? 1 : -1) * GESTURE_PINCH_STEP;
      fired += 1;
    }
  }

  function gestureTouchMove(event) {
    if (!gestureState) {
      return false;
    }
    const touchList = event.touches;
    if (touchList.length < 2) {
      gestureRememberLast(touchList);
      return true;
    }
    // Two+ fingers on the pane means a gesture — never scroll or browser-zoom.
    event.preventDefault();

    let drift = 0;
    for (let i = 0; i < touchList.length; i += 1) {
      const touch = touchList[i];
      const pos = gestureState.positions.get(touch.identifier);
      if (pos) {
        drift = Math.max(drift, Math.hypot(touch.clientX - pos.sx, touch.clientY - pos.sy));
      }
    }
    if (drift > GESTURE_MOVE_SLOP) {
      gestureState.moved = true;
    }

    if (gestureState.mode === "pinch") {
      handleGesturePinch(touchList);
      return true;
    }
    if (gestureState.mode === "swipe") {
      handleGestureSwipe(touchList);
      return true;
    }
    // Undecided: a strong two-finger spread with a near-still midpoint is a pinch;
    // otherwise feed the swipe accumulator, which self-commits by locking its axis.
    if (touchList.length === 2 && gestureState.pinchAnchor > 0) {
      const spread = Math.abs(touchDistance(touchList[0], touchList[1]) - gestureState.pinchAnchor);
      const centroid = touchListCentroid(touchList);
      const centroidMove = Math.hypot(
        centroid.x - gestureState.centroidStart.x,
        centroid.y - gestureState.centroidStart.y,
      );
      if (spread >= GESTURE_PINCH_START && spread > centroidMove) {
        gestureState.mode = "pinch";
        handleGesturePinch(touchList);
        return true;
      }
    }
    handleGestureSwipe(touchList);
    return true;
  }

  function registerTapPulse(fingers) {
    if (!gestureTap || gestureTap.fingers !== fingers) {
      if (gestureTap && gestureTap.timer) {
        window.clearTimeout(gestureTap.timer);
      }
      gestureTap = { fingers, count: 0, timer: null };
    }
    gestureTap.count += 1;
    if (gestureTap.timer) {
      window.clearTimeout(gestureTap.timer);
    }
    gestureTap.timer = window.setTimeout(finalizeTapGesture, GESTURE_MULTITAP_GAP_MS);
  }

  function finalizeTapGesture() {
    if (!gestureTap) {
      return;
    }
    const { fingers, count } = gestureTap;
    gestureTap = null;
    if (fingers >= 2) {
      dispatchGestureSequence(gestureBindingSequence(tapGestureId(fingers, count)));
    }
  }

  // touchend: if this drop completes a short, still pulse that reached >= 2
  // fingers, count it toward a (possibly multi-) tap. Swipes/pinches set
  // `handled` and are excluded. `rose` guards the final all-up drop of a hold.
  function gestureTouchEnd(event) {
    if (!gestureState) {
      return false;
    }
    if (
      gestureState.rose &&
      !gestureState.moved &&
      !gestureState.handled &&
      gestureState.peak >= 2 &&
      performance.now() - gestureState.riseAt <= GESTURE_TAP_MAX_MS
    ) {
      registerTapPulse(gestureState.peak);
    }
    gestureState.rose = false;
    gestureRememberLast(event.touches);
    if (event.touches.length === 0) {
      gestureState = null;
    }
    return true;
  }

  function installTerminalScrollHandlers() {
    const terminalRoot = document.getElementById("terminal");
    if (!terminalRoot) {
      return;
    }
    // Listen on #terminal itself: in xterm 6 the .xterm-viewport node is an
    // absolutely-positioned sibling of the element that actually receives
    // pointer events, so a listener there never fires. Capture phase keeps
    // xterm from turning the wheel into SGR mouse reports for the pane.
    const wheelTarget = terminalRoot;

    if (mobileComposerMode) {
      // The keyboard is opened from touchend (see finishTouchScroll) so a
      // double-tap can pre-empt it; the browser's synthesized click is just
      // swallowed here and never opens the keyboard on its own.
      terminalRoot.addEventListener("click", () => {
        suppressNextTerminalClick = false;
      });
    }

    // Keep the selection handles glued to the text as the pane scrolls or
    // re-renders, and tear the UI down if the selection is cleared elsewhere.
    term.onRender(() => {
      if (isSelectionUIVisible()) {
        scheduleTerminalSelectionUISync();
      }
    });
    term.onSelectionChange(() => {
      if (selectionUIBusy()) {
        scheduleTerminalSelectionUISync();
        return;
      }
      if (!terminalHasSelection()) {
        clearTerminalSelectionUI();
      } else if (isSelectionUIVisible()) {
        scheduleTerminalSelectionUISync();
      }
    });

    wheelTarget.addEventListener(
      "wheel",
      (event) => {
        if (!event.deltaY) {
          return;
        }
        cancelTouchInertia();
        event.preventDefault();
        event.stopImmediatePropagation();
        // Firefox reports wheel deltas in lines (deltaMode 1), not pixels.
        const lineHeightPx = term.options.fontSize * (term.options.lineHeight || 1);
        let pixelDelta = event.deltaY;
        if (event.deltaMode === 1) {
          pixelDelta *= lineHeightPx;
        } else if (event.deltaMode === 2) {
          pixelDelta *= Math.max(1, term.rows) * lineHeightPx;
        }
        scrollTerminalByPixels(-pixelDelta);
      },
      { passive: false, capture: true },
    );

    terminalRoot.addEventListener(
      "touchstart",
      (event) => {
        cancelTouchInertia();
        if (event.touches.length >= 2) {
          // Second finger down: switch from scrolling to the gesture recognizer.
          touchScrollState = null;
          cancelTerminalSelectionPress();
          gestureTouchStart(event);
          return;
        }
        // A fresh single-finger contact ends any prior gesture episode.
        gestureState = null;
        if (event.touches.length !== 1) {
          touchScrollState = null;
          cancelTerminalSelectionPress();
          return;
        }
        const touch = event.touches[0];
        suppressComposerOpenThisTouch = false;
        // A tap anywhere on the pane dismisses a lingering selection (and its
        // handles) before it can start scrolling or a new long-press. Handle
        // drags stopPropagation before reaching here, so they're unaffected.
        if (isSelectionUIVisible()) {
          if (pointInSelection(touch.clientX, touch.clientY)) {
            // Tapping the highlighted text itself copies it — no need to hit the
            // Copy chip. Defer the copy to touchend so a drag can still scroll.
            selectionTapCopy = { x: touch.clientX, y: touch.clientY };
            suppressComposerOpenThisTouch = true;
            touchScrollState = null;
            return;
          }
          dismissTerminalSelection();
          suppressNextTerminalClick = true;
          // This tap only dismissed the selection; it shouldn't also raise the
          // keyboard on lift.
          suppressComposerOpenThisTouch = true;
        }
        // Double-tap selects the word under the finger (the standard mobile
        // gesture, alongside long-press).
        const now = performance.now();
        if (
          now - lastTermTapAt < TERM_DOUBLETAP_MS &&
          Math.hypot(touch.clientX - lastTermTapX, touch.clientY - lastTermTapY) <
            TERM_DOUBLETAP_DIST
        ) {
          lastTermTapAt = 0;
          termSel = { doubleTap: true };
          touchScrollState = null;
          // Tap 1's touchend already raised the keyboard synchronously (that's
          // what makes single-tap-to-type work on iOS). This second tap is a
          // word-select gesture, so drop the keyboard back down.
          if (document.activeElement === composerInput) {
            composerInput.blur();
          }
          // selectWordAt() uses term.select() (no synthetic mouse event), so it
          // never focuses xterm's textarea and can run synchronously without
          // popping the keyboard.
          selectWordAt(touch.clientX, touch.clientY);
          return;
        }
        touchScrollState = {
          lastY: touch.clientY,
          lastAt: performance.now(),
          velocity: 0,
          lastMoveAt: 0,
        };
        beginTerminalSelectionPress(touch);
      },
      { passive: true },
    );

    terminalRoot.addEventListener(
      "touchmove",
      (event) => {
        if (gestureState) {
          gestureTouchMove(event);
          return;
        }
        if (selectionTapCopy) {
          const t = event.touches[0];
          if (
            t &&
            Math.hypot(t.clientX - selectionTapCopy.x, t.clientY - selectionTapCopy.y) >
              TERM_LONGPRESS_SLOP
          ) {
            // Finger dragged off the tap — cancel the copy and drop the selection.
            selectionTapCopy = null;
            dismissTerminalSelection();
            suppressNextTerminalClick = true;
          }
          return; // don't scroll or select while a copy-tap is pending
        }
        if (
          event.touches.length === 1 &&
          termSel &&
          !termSel.draggingHandle &&
          !termSel.doubleTap
        ) {
          const touch = event.touches[0];
          if (termSel.active) {
            // Long-press engaged: drag extends the selection, never scrolls.
            const point = clampTerminalSelectionDragPoint(touch.clientX, touch.clientY);
            termSel.lastX = point.x;
            termSel.lastY = point.y;
            event.preventDefault();
            dispatchTerminalMouse("mousemove", point.x, point.y, 1);
            updateSelectionDragFeedback(point);
            scheduleTerminalSelectionUISync();
            return;
          }
          if (
            termSel.timer &&
            Math.hypot(touch.clientX - termSel.startX, touch.clientY - termSel.startY) >
              TERM_LONGPRESS_SLOP
          ) {
            // Finger traveled before the hold completed — treat it as a scroll.
            cancelTerminalSelectionPress();
          }
        }
        if (!touchScrollState || event.touches.length !== 1) {
          return;
        }
        const nextY = event.touches[0].clientY;
        const deltaY = touchScrollState.lastY - nextY;
        if (Math.abs(deltaY) < 2) {
          return;
        }
        const now = performance.now();
        const scrollPixels = -deltaY;
        const deltaMs = Math.max(1, now - touchScrollState.lastAt);
        const velocity = scrollPixels / deltaMs;
        touchScrollState.velocity =
          touchScrollState.velocity * (1 - TOUCH_VELOCITY_BLEND) + velocity * TOUCH_VELOCITY_BLEND;
        touchScrollState.lastY = nextY;
        touchScrollState.lastAt = now;
        touchScrollState.lastMoveAt = now;
        event.preventDefault();
        scrollTerminalByPixels(scrollPixels);
      },
      { passive: false },
    );

    const finishTouchScroll = (event) => {
      if (gestureState) {
        // Let the recognizer settle the lifted finger (tap counting, cleanup);
        // don't fall back into a scroll or inertia fling.
        gestureTouchEnd(event);
        touchScrollState = null;
        cancelTerminalSelectionPress();
        selectionTapCopy = null;
        return;
      }
      // A clean tap inside the selection copies it.
      if (selectionTapCopy) {
        selectionTapCopy = null;
        touchScrollState = null;
        suppressNextTerminalClick = true;
        copyTerminalSelection().then(dismissTerminalSelection);
        return;
      }
      // A completed double-tap selection needs no scroll/inertia handling.
      if (termSel && termSel.doubleTap) {
        termSel = null;
        touchScrollState = null;
        return;
      }
      // Did this contact engage a long-press selection? (Set before finishing.)
      const wasLongPressSelection = !!(termSel && termSel.active);
      // Finalize a long-press selection (no-op for a quick tap or a scroll).
      finishTerminalSelectionPress();
      // Remember this lift as a candidate first tap of a double-tap.
      const tapTouch = event.changedTouches && event.changedTouches[0];
      if (tapTouch) {
        lastTermTapAt = performance.now();
        lastTermTapX = tapTouch.clientX;
        lastTermTapY = tapTouch.clientY;
      }
      const scrolled = !!(touchScrollState && touchScrollState.lastMoveAt);
      // A plain tap (no scroll, no long-press selection) opens the keyboard.
      // iOS raises the soft keyboard ONLY when focus() runs synchronously inside
      // the touch gesture — a deferred focus (setTimeout/rAF) is silently
      // refused, which is what forced a second tap before. So focus now, in this
      // handler. A genuine double-tap (word select) blurs it back down on its
      // touchstart. btop tabs have no prompt — never raise the keyboard there.
      if (
        mobileComposerMode &&
        !btopMode &&
        !wasLongPressSelection &&
        !scrolled &&
        !suppressComposerOpenThisTouch
      ) {
        openComposer(true);
      }
      if (scrolled && performance.now() - touchScrollState.lastMoveAt < 80) {
        startTouchInertia(touchScrollState.velocity);
      }
      touchScrollState = null;
    };
    const cancelTouchScroll = () => {
      touchScrollState = null;
      selectionTapCopy = null;
      cancelTerminalSelectionPress();
      gestureState = null;
      if (gestureTap && gestureTap.timer) {
        window.clearTimeout(gestureTap.timer);
      }
      gestureTap = null;
      cancelTouchInertia();
    };
    terminalRoot.addEventListener("touchend", finishTouchScroll, { passive: true });
    terminalRoot.addEventListener("touchcancel", cancelTouchScroll, { passive: true });
  }

  function installTabStripScrollHandlers() {
    tabsScroller.addEventListener(
      "touchstart",
      (event) => {
        if (event.touches.length !== 1) {
          tabDragState = null;
          return;
        }
        tabDragState = {
          startX: event.touches[0].clientX,
          startScrollLeft: tabsScroller.scrollLeft,
          dragging: false,
        };
      },
      { passive: true },
    );

    tabsScroller.addEventListener(
      "touchmove",
      (event) => {
        if (!tabDragState || event.touches.length !== 1) {
          return;
        }
        const deltaX = event.touches[0].clientX - tabDragState.startX;
        if (!tabDragState.dragging && Math.abs(deltaX) < 6) {
          return;
        }
        tabDragState.dragging = true;
        tabsScroller.scrollLeft = tabDragState.startScrollLeft - deltaX;
        event.preventDefault();
      },
      { passive: false },
    );

    const finishDrag = () => {
      if (tabDragState?.dragging) {
        suppressTabClickUntil = Date.now() + 250;
      }
      tabDragState = null;
    };
    tabsScroller.addEventListener("touchend", finishDrag, { passive: true });
    tabsScroller.addEventListener("touchcancel", finishDrag, { passive: true });
  }

  function installShortcutBarScrollHandlers() {
    shortcutBar.addEventListener(
      "touchstart",
      (event) => {
        if (event.touches.length !== 1) {
          shortcutDragState = null;
          return;
        }
        shortcutDragState = {
          startX: event.touches[0].clientX,
          startScrollLeft: shortcutBar.scrollLeft,
          dragging: false,
        };
      },
      { passive: true },
    );

    shortcutBar.addEventListener(
      "touchmove",
      (event) => {
        if (!shortcutDragState || event.touches.length !== 1) {
          return;
        }
        const deltaX = event.touches[0].clientX - shortcutDragState.startX;
        if (!shortcutDragState.dragging && Math.abs(deltaX) < 6) {
          return;
        }
        shortcutDragState.dragging = true;
        shortcutBar.scrollLeft = shortcutDragState.startScrollLeft - deltaX;
        event.preventDefault();
      },
      { passive: false },
    );

    const finishDrag = () => {
      if (shortcutDragState?.dragging) {
        suppressShortcutClickUntil = Date.now() + 250;
      }
      shortcutDragState = null;
    };
    shortcutBar.addEventListener("touchend", finishDrag, { passive: true });
    shortcutBar.addEventListener("touchcancel", finishDrag, { passive: true });
  }

  function installMobileTextInputGuards() {
    if (mobileComposerMode) {
      return;
    }
    const helper = document.querySelector(".xterm-helper-textarea");
    if (!helper) {
      return;
    }

    helper.addEventListener(
      "input",
      (event) => {
        const inputType = event.inputType || "";
        const data = typeof event.data === "string" ? event.data : "";
        const value = helper.value || "";
        const recentSpeech = Date.now() - speechInputState.lastAt < 5000;
        const looksLikeSpeech =
          inputType.includes("Replacement") ||
          inputType.includes("Composition") ||
          value.length > 1 ||
          data.length > 1 ||
          (recentSpeech && inputType.startsWith("insert"));

        if (!looksLikeSpeech) {
          if (inputType === "insertText" && data.length === 1) {
            resetSpeechInputState();
          }
          return;
        }

        event.stopImmediatePropagation();
        queueSpeechPhrase(value || data, inputType.includes("Composition") ? 20 : 50);
      },
      true,
    );

    helper.addEventListener(
      "compositionend",
      () => {
        const value = helper.value || "";
        if (!value) {
          return;
        }
        queueSpeechPhrase(value, 10);
      },
      true,
    );

    helper.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Enter" || event.key === "Escape") {
          const value = helper.value || "";
          if (value) {
            applySpeechPhrase(value);
          }
          resetSpeechInputState();
          helper.value = "";
        }
      },
      true,
    );

    helper.addEventListener(
      "blur",
      () => {
        const value = helper.value || "";
        if (value) {
          applySpeechPhrase(value);
          helper.value = "";
        }
        resetSpeechInputState();
      },
      true,
    );
  }

  function computeViewportSafeUiScale(viewportWidth, viewportHeight, keyboardInset = 0, layoutHeight = window.innerHeight) {
    const safeWidth = Math.max(0, viewportWidth || window.innerWidth || UI_SCALE_FIT_WIDTH);
    const safeHeight = Math.max(0, keyboardInset > 0 ? layoutHeight : viewportHeight || window.innerHeight || UI_SCALE_FIT_HEIGHT);
    const widthScale = safeWidth / UI_SCALE_FIT_WIDTH;
    const heightScale = safeHeight / UI_SCALE_FIT_HEIGHT;
    return Math.min(1, Math.max(0.5, Math.min(widthScale, heightScale)));
  }

  function detectViewportShock(viewportWidth, viewportHeight, keyboardInset = 0) {
    if (keyboardInset > 0) {
      return false;
    }
    if (!lastStableViewportWidth || !lastStableViewportHeight) {
      lastStableViewportWidth = viewportWidth;
      lastStableViewportHeight = viewportHeight;
      return false;
    }
    const widthDelta = Math.abs(viewportWidth - lastStableViewportWidth) / Math.max(lastStableViewportWidth, 1);
    const heightDelta = Math.abs(viewportHeight - lastStableViewportHeight) / Math.max(lastStableViewportHeight, 1);
    const orientationChanged =
      (lastStableViewportWidth > lastStableViewportHeight) !== (viewportWidth > viewportHeight);
    lastStableViewportWidth = viewportWidth;
    lastStableViewportHeight = viewportHeight;
    return orientationChanged || widthDelta > 0.18 || heightDelta > 0.18;
  }

  function scheduleViewportSettlePasses() {
    viewportSettleTimers.forEach((timerId) => window.clearTimeout(timerId));
    viewportSettleTimers = [80, 220, 420].map((delay) =>
      window.setTimeout(() => {
        updateViewportMetrics();
      }, delay),
    );
  }

  function applyEffectiveUiScale(viewportWidth, viewportHeight, keyboardInset = 0, layoutHeight = window.innerHeight) {
    void viewportWidth;
    void viewportHeight;
    void keyboardInset;
    void layoutHeight;
    effectiveUiScale = uiScale;
    document.documentElement.style.setProperty("--ui-scale", String(effectiveUiScale));
    return effectiveUiScale;
  }

  function measureShortcutHeight() {
    if (shortcutsPanel.classList.contains("hidden")) {
      document.documentElement.style.setProperty("--shortcut-height", "0px");
      document.documentElement.style.setProperty("--shortcut-reserve", "0px");
      return;
    }
    const panelRect = shortcutsPanel.getBoundingClientRect();
    const shortcutRect = shortcutBar.getBoundingClientRect();
    const composerRect =
      mobileComposerMode &&
      !composerPanel.classList.contains("hidden") &&
      document.body.dataset.composerActive === "true"
        ? composerPanel.getBoundingClientRect()
        : null;
    const viewport = window.visualViewport;
    const viewportClaimsKeyboard =
      viewport && window.innerHeight - (viewport.height + viewport.offsetTop) > KEYBOARD_THRESHOLD;
    const layoutSaysKeyboardClosed = document.body.dataset.keyboardOpen !== "true";
    // Trust the layout-viewport bottom whenever we don't believe the keyboard
    // is genuinely up: stale visualViewport, no focused input, or we just
    // hard-reset --keyboard-inset to 0 (e.g. after a tab switch).
    const trustViewportBottom =
      viewport &&
      viewportClaimsKeyboard &&
      focusedElementAcceptsKeyboard() &&
      !layoutSaysKeyboardClosed;
    const viewportBottom = trustViewportBottom
      ? viewport.offsetTop + viewport.height
      : window.innerHeight;
    const top = composerRect ? Math.min(panelRect.top, composerRect.top) : panelRect.top;
    const shortcutHeight = Math.ceil(shortcutRect.height);
    if (shortcutHeight > 0) {
      document.documentElement.style.setProperty("--shortcut-height", `${shortcutHeight}px`);
    }
    const reserve = Math.max(0, Math.ceil(viewportBottom - top));
    if (reserve > 0) {
      document.documentElement.style.setProperty("--shortcut-reserve", `${reserve}px`);
    }
  }

  function focusedElementAcceptsKeyboard() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) {
      return false;
    }
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    if (tag === "textarea") return true;
    if (tag === "input") {
      const type = (el.type || "text").toLowerCase();
      const nonTextTypes = new Set([
        "button", "submit", "reset", "checkbox", "radio", "file", "image", "range", "color", "hidden",
      ]);
      return !nonTextTypes.has(type);
    }
    if (el.isContentEditable) return true;
    return false;
  }

  // Pin the currently-open settings overlay directly to the visual viewport so
  // it always sits above the on-screen keyboard. This is intentionally decoupled
  // from --app-height/updateViewportMetrics (which also drive terminal layout and
  // apply stale-viewport resets) — inline geometry on the open overlay is the
  // most reliable way to keep the sheet from drifting when the keyboard opens.
  function syncOverlayToViewport() {
    const viewport = window.visualViewport;
    const overlays = document.querySelectorAll(".overlay");
    for (let i = 0; i < overlays.length; i += 1) {
      if (viewport) {
        overlays[i].style.top = `${Math.round(viewport.offsetTop)}px`;
        overlays[i].style.height = `${Math.round(viewport.height)}px`;
      } else {
        overlays[i].style.top = "";
        overlays[i].style.height = "";
      }
    }
  }

  function updateViewportMetrics() {
    syncOverlayToViewport();
    const viewport = window.visualViewport;
    const viewportWidth = viewport ? viewport.width : window.innerWidth;
    let viewportHeight = viewport ? viewport.height : window.innerHeight;
    const offsetTop = viewport ? viewport.offsetTop : 0;
    const layoutHeight = window.innerHeight;
    let rawKeyboardInset = Math.max(0, layoutHeight - (viewportHeight + offsetTop));
    // visualViewport sometimes stays "shrunk" after a tab switch even though
    // the keyboard isn't actually open and no event will fire to refresh it.
    // If nothing focusable currently has focus, the keyboard cannot be open —
    // override the stale viewport value with the layout viewport height.
    if (rawKeyboardInset > KEYBOARD_THRESHOLD && !focusedElementAcceptsKeyboard()) {
      viewportHeight = layoutHeight;
      rawKeyboardInset = 0;
    }
    const keyboardInset = rawKeyboardInset > KEYBOARD_THRESHOLD ? rawKeyboardInset : 0;
    detectViewportShock(viewportWidth, viewportHeight, keyboardInset);
    document.documentElement.style.setProperty("--app-top", `${Math.round(offsetTop)}px`);
    document.documentElement.style.setProperty("--app-height", `${Math.round(viewportHeight)}px`);
    document.documentElement.style.setProperty("--keyboard-inset", `${Math.round(keyboardInset)}px`);
    document.body.dataset.keyboardOpen = keyboardInset > 0 ? "true" : "false";
    applyEffectiveUiScale(viewportWidth, viewportHeight, keyboardInset, layoutHeight);
    const preserveTerminalCols = lastLayoutViewportWidth > 0 && Math.abs(viewportWidth - lastLayoutViewportWidth) < 1;
    lastLayoutViewportWidth = viewportWidth;
    scheduleLayoutRefresh({ preserveTerminalCols });
    scheduleTerminalSelectionUISync();
  }

  function renderTabs() {
    tabsStrip.innerHTML = "";
    currentTabs.forEach((tab) => {
      const button = document.createElement("button");
      const btopPill = tab.type !== "editor" && isBtopSession(tab.name);
      button.className = `tab-pill tab-pill-${btopPill ? "btop" : tab.type || "terminal"}${tab.active ? " is-active" : ""}`;
      button.type = "button";
      button.textContent = tab.type === "editor"
        ? `Files: ${tab.name || "root"}`
        : btopPill
          ? tab.label || btopTabLabel(tab.name)
          : tab.label || tab.name || "session";
      button.addEventListener("click", () => {
        if (Date.now() < suppressTabClickUntil) {
          return;
        }
        if (tab.active) {
          toggleTabMenu(tab.key);
          return;
        }
        if (tab.type === "editor") {
          switchEditorTab(tab.id);
          return;
        }
        switchSession(tab.name);
      });
      button.dataset.tabKey = tab.key;
      tabsStrip.appendChild(button);
    });
    if (!currentTabs.some((tab) => tab.key === openTabMenuKey && tab.active)) {
      closeTabMenu();
    } else {
      positionTabMenu();
    }
  }

  function renderProfileMenu() {
    profileMenu.innerHTML = "";
    profiles.forEach((profile) => {
      const row = document.createElement("div");
      row.className = "profile-menu-row";
      const button = document.createElement("button");
      button.className = `tab-menu-button${profile.id === activeProfileId ? " is-active" : ""}`;
      button.type = "button";

      const dot = document.createElement("span");
      dot.className = "profile-menu-dot";
      dot.style.background = profile.accent || "#ffd166";
      const label = document.createElement("span");
      label.textContent = profile.label || profile.id;
      const state = document.createElement("span");
      const down = profile.available === false || profile.status === "down";
      state.className = `profile-menu-state${down ? " is-down" : ""}`;
      state.textContent = down ? "Down" : profile.id === activeProfileId ? "Active" : "";
      button.appendChild(dot);
      button.appendChild(label);
      button.appendChild(state);
      button.addEventListener("click", () => switchProfile(profile.id));
      row.appendChild(button);
      profileMenu.appendChild(row);
    });
  }

  function switchProfile(profileId) {
    const profile = profiles.find((candidate) => candidate.id === profileId);
    if (!profile) {
      closeProfileMenu();
      return;
    }
    if (profileId === activeProfileId) {
      closeProfileMenu();
      if (profile.available === false || profile.status === "down") {
        sendMessage({ type: "retry-profile", profile: profileId, session: selectedSessionName });
      }
      return;
    }

    snapshotActiveSession();
    const previousProfileId = activeProfileId;
    activeProfileId = profileId;
    loginRealm = profile.authRealm || "";
    pendingProfileId = profileId;
    localStorage.setItem(STORAGE_ACTIVE_PROFILE_KEY, activeProfileId);
    loadActiveProfileState(previousProfileId);
    closeProfileMenu();
    closeSessionMenu();
    closeTabMenu();
    clearTerminalSelectionUI();
    terminalAuthoritative = false;
    resetComposerTracking(true);
    term.reset();
    applyActiveProfile();
    renderProfileMenu();
    syncOpenTabsToSessions();
    sendMessage({ type: "switch-profile", profile: profileId, session: selectedSessionName });
  }

  function renderSessionMenu() {
    sessionMenu.innerHTML = "";
    if (!currentSessions.length) {
      const emptyState = document.createElement("div");
      emptyState.className = "menu-empty";
      emptyState.textContent = "No running sessions";
      sessionMenu.appendChild(emptyState);
      return;
    }

    currentSessions.forEach((session) => {
      const row = document.createElement("div");
      row.className = "tab-menu-row";

      const button = document.createElement("button");
      button.className = `tab-menu-button${session.name === activeSessionName ? " is-active" : ""}`;
      button.type = "button";
      button.textContent = session.label || session.name;
      button.addEventListener("click", () => {
        switchSession(session.name);
      });

      const kill = document.createElement("button");
      kill.className = "session-kill-button";
      kill.type = "button";
      kill.textContent = "×";
      kill.title = "Kill session";
      kill.setAttribute("aria-label", `Kill session ${session.label || session.name}`);
      kill.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!window.confirm(`Kill tmux session "${session.name}"?`)) {
          return;
        }
        sendMessage({ type: "kill-session", session: session.name });
      });

      row.appendChild(button);
      row.appendChild(kill);
      sessionMenu.appendChild(row);
    });
  }

  function renderShortcutBar() {
    shortcutBar.innerHTML = "";
    shortcuts.filter((shortcut) => shortcut.visible !== false).forEach((shortcut) => {
      const button = document.createElement("button");
      let preserveComposerFocus = false;
      let repeatDelayTimer = null;
      let repeatIntervalTimer = null;
      let suppressClickAfterRepeat = false;
      let shortcutKeyboardWasFocused = false;
      button.className = "shortcut-button";
      button.type = "button";
      button.tabIndex = -1;
      button.textContent = shortcut.label;

      const clearRepeatTimers = () => {
        window.clearTimeout(repeatDelayTimer);
        window.clearInterval(repeatIntervalTimer);
        repeatDelayTimer = null;
        repeatIntervalTimer = null;
      };

      const activateShortcut = () => {
        if (Date.now() < suppressShortcutClickUntil) {
          return;
        }
        const normalizedSequence = shortcut.sequence.trim().toUpperCase();
        if (normalizedSequence === "{PASTE}") {
          pasteFromClipboard({
            preserveKeyboardState: true,
            wasKeyboardFocused: shortcutKeyboardWasFocused,
          });
          return;
        }
        if (normalizedSequence === "{COPY}") {
          copyTerminalSelection();
          restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
          return;
        }
        if (mobileComposerMode && normalizedSequence === "{ENTER}") {
          sendMessage({ type: "composer-enter", revision: nextComposerRevision() });
          clearComposer(false);
          restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
          return;
        }
        if (mobileComposerMode && normalizedSequence === "{BACKSPACE}" && composerInput.value === "") {
          sendMessage({ type: "input", data: specialMap.BACKSPACE });
          requestComposerRefresh();
          restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
          return;
        }
        const historyDirection = mobileComposerMode ? shortcutHistoryDirection(shortcut.sequence) : "";
        if (historyDirection) {
          navigateComposerHistory(historyDirection, shortcutKeyboardWasFocused);
          restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
          return;
        }
        if (mobileComposerMode && shortcutShouldFlushComposer(shortcut.sequence)) {
          resetComposerTracking(true);
        }
        const sequence = expandShortcutSequence(shortcut.sequence);
        if (!sequence) {
          restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
          return;
        }
        sendMessage({ type: "input", data: sequence });
        restoreShortcutKeyboardState(shortcutKeyboardWasFocused);
      };

      button.addEventListener(
        "pointerdown",
        (event) => {
          if (event.pointerType === "mouse" && event.button !== 0) {
            return;
          }
          shortcutKeyboardWasFocused = composerHasKeyboardFocus();
          preserveComposerFocus = shortcutKeyboardWasFocused;
          if (preserveComposerFocus) {
            event.preventDefault();
          }
          clearRepeatTimers();
          suppressClickAfterRepeat = false;
          if (!shortcutSupportsHoldRepeat(shortcut.sequence)) {
            return;
          }
          repeatDelayTimer = window.setTimeout(() => {
            suppressClickAfterRepeat = true;
            activateShortcut();
            repeatIntervalTimer = window.setInterval(() => {
              activateShortcut();
            }, SHORTCUT_REPEAT_INTERVAL_MS);
          }, SHORTCUT_REPEAT_DELAY_MS);
        },
        { passive: false },
      );
      button.addEventListener("click", () => {
        if (suppressClickAfterRepeat) {
          suppressClickAfterRepeat = false;
          return;
        }
        activateShortcut();
      });
      ["pointerup", "pointercancel", "pointerleave", "lostpointercapture"].forEach((eventName) => {
        button.addEventListener(eventName, clearRepeatTimers);
      });
      shortcutBar.appendChild(button);
    });
    measureShortcutHeight();
  }

  function expandShortcutSequence(sequence) {
    let output = "";
    let cursor = 0;
    const matcher = /\{([^}]+)\}/g;
    let match;
    while ((match = matcher.exec(sequence)) !== null) {
      if (match.index > cursor) {
        output += sequence.slice(cursor, match.index);
      }
      output += expandToken(match[1]);
      cursor = matcher.lastIndex;
    }
    if (cursor < sequence.length) {
      output += sequence.slice(cursor);
    }
    return output;
  }

  function expandToken(token) {
    const clean = token.trim();
    const upper = clean.toUpperCase();
    if (upper === "PASTE") {
      return "";
    }
    if (upper === "COPY") {
      return "";
    }
    if (upper.startsWith("TEXT:")) {
      return clean.slice(5);
    }
    if (upper.startsWith("CTRL+")) {
      const key = clean.slice(5).trim();
      if (!key) {
        return "";
      }
      if (key.toUpperCase() === "SPACE") {
        return "\u0000";
      }
      const char = key[0].toUpperCase();
      return String.fromCharCode(char.charCodeAt(0) & 31);
    }
    if (upper.startsWith("ALT+")) {
      return "\u001b" + clean.slice(4);
    }
    return specialMap[upper] || "";
  }

  function focusTerminal() {
    if (mobileComposerMode) {
      openComposer(false);
      scheduleLayoutRefresh({ preserveTerminalCols: true });
      return;
    }
    term.focus();
    const helper = document.querySelector(".xterm-helper-textarea");
    if (helper) {
      helper.focus({ preventScroll: true });
    }
    scheduleLayoutRefresh();
  }

  function nextFileRequestId() {
    fileRequestCounter += 1;
    return `fs-${Date.now().toString(36)}-${fileRequestCounter}`;
  }

  function cancelPendingFileRequests() {
    pendingFileRequests.forEach((pending) => {
      if (pending.timeoutId) {
        window.clearTimeout(pending.timeoutId);
      }
    });
    pendingFileRequests.clear();
  }

  function sendFileCommand(type, payload = {}, context = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      showToast("Connect before opening files.");
      return "";
    }
    const requestId = nextFileRequestId();
    const timeoutId = window.setTimeout(() => {
      const pending = pendingFileRequests.get(requestId);
      if (!pending) {
        return;
      }
      pendingFileRequests.delete(requestId);
      handleFileRequestTimeout(pending);
    }, FILE_REQUEST_TIMEOUT_MS);
    pendingFileRequests.set(requestId, { ...context, profileId: activeProfileId, timeoutId });
    sendMessage({ type, requestId, ...payload });
    return requestId;
  }

  function requestDefaultFileRoot() {
    sendFileCommand("fs-default-root", {}, { kind: "default-root" });
  }

  function openFileRootPicker(mode = "new") {
    closeTabMenu();
    closeSessionMenu();
    closeSettingsMenu();
    const currentEditor = activeEditorTab();
    fileRootForm.dataset.mode = mode;
    fileRootMessage.textContent = "";
    fileRootInput.value = mode === "change" && currentEditor ? currentEditor.root : lastDefaultFileRoot || currentEditor?.root || "";
    renderFileBookmarks();
    fileRootOverlay.classList.remove("hidden");
    requestDefaultFileRoot();
    window.requestAnimationFrame(() => {
      fileRootInput.focus({ preventScroll: true });
      fileRootInput.select();
    });
  }

  function closeFileRootPicker() {
    fileRootOverlay.classList.add("hidden");
    fileRootMessage.textContent = "";
  }

  function openBookmarkedRoot(path) {
    fileRootInput.value = path;
    fileRootMessage.textContent = "";
    if (fileRootForm.dataset.mode === "change") {
      changeActiveEditorRoot(path);
      return;
    }
    createEditorTab(path);
  }

  function renderFileBookmarks() {
    fileBookmarkList.innerHTML = "";
    if (!fileBookmarks.length) {
      const empty = document.createElement("div");
      empty.className = "file-bookmark-empty";
      empty.textContent = "No bookmarks saved";
      fileBookmarkList.appendChild(empty);
      return;
    }
    fileBookmarks.forEach((bookmark) => {
      const row = document.createElement("div");
      row.className = "file-bookmark-row";

      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "file-bookmark-open";
      openButton.addEventListener("click", () => openBookmarkedRoot(bookmark.path));

      const name = document.createElement("span");
      name.className = "file-bookmark-name";
      name.textContent = bookmark.name || fileBookmarkName(bookmark.path);
      openButton.appendChild(name);

      const path = document.createElement("span");
      path.className = "file-bookmark-path";
      path.textContent = bookmark.path;
      openButton.appendChild(path);
      row.appendChild(openButton);

      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "file-bookmark-remove";
      removeButton.textContent = "Remove";
      removeButton.setAttribute("aria-label", `Remove ${bookmark.name || bookmark.path}`);
      removeButton.addEventListener("click", () => removeFileBookmark(bookmark.path));
      row.appendChild(removeButton);

      fileBookmarkList.appendChild(row);
    });
  }

  function addFileBookmark(path) {
    const nextPath = String(path || "").trim();
    if (!nextPath) {
      fileRootMessage.textContent = "Enter a path to bookmark.";
      return;
    }
    const nextBookmark = normalizeFileBookmark(nextPath);
    fileBookmarks = [
      nextBookmark,
      ...fileBookmarks.filter((bookmark) => !sameFilePath(bookmark.path, nextPath)),
    ].slice(0, FILE_BOOKMARK_LIMIT);
    fileRootMessage.textContent = "";
    saveHostSettings();
    renderFileBookmarks();
    showToast("Bookmark saved.");
  }

  function removeFileBookmark(path) {
    fileBookmarks = fileBookmarks.filter((bookmark) => !sameFilePath(bookmark.path, path));
    saveHostSettings();
    renderFileBookmarks();
    showToast("Bookmark removed.");
  }

  function createEditorTab(rootPath) {
    const root = String(rootPath || "").trim();
    if (!root) {
      fileRootMessage.textContent = "Enter a path first.";
      return;
    }
    const tab = normalizeEditorTab({
      id: `files-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
      root,
      name: pathBaseName(root),
    });
    if (!tab) {
      fileRootMessage.textContent = "Enter a valid path.";
      return;
    }
    editorTabs.push(tab);
    persistEditorTabs();
    activeTabKey = editorTabKey(tab.id);
    closeFileRootPicker();
    syncOpenTabsToSessions();
    requestFileList(tab, tab.root);
  }

  function changeActiveEditorRoot(rootPath) {
    const tab = activeEditorTab();
    const root = String(rootPath || "").trim();
    if (!tab || !root) {
      fileRootMessage.textContent = "Enter a path first.";
      return;
    }
    if (editorHasDirtyFiles(tab) && !window.confirm(`Discard unsaved changes in "${tab.name}"?`)) {
      return;
    }
    tab.root = root;
    tab.name = pathBaseName(root);
    tab.tree = {};
    tab.openFiles = [];
    tab.activeFilePath = "";
    tab.error = "";
    persistEditorTabs();
    closeFileRootPicker();
    syncOpenTabsToSessions();
    requestFileList(tab, tab.root);
  }

  function switchEditorTab(tabId) {
    const tab = editorTabById(tabId);
    if (!tab) {
      return;
    }
    activeTabKey = editorTabKey(tab.id);
    closeSessionMenu();
    closeTabMenu();
    syncOpenTabsToSessions();
  }

  function closeEditorTab(tabId) {
    const tab = editorTabById(tabId);
    if (!tab) {
      return;
    }
    if (editorHasDirtyFiles(tab) && !window.confirm(`Discard unsaved changes in "${tab.name}"?`)) {
      return;
    }
    const wasActive = activeTabKey === editorTabKey(tabId);
    editorTabs = editorTabs.filter((item) => item.id !== tabId);
    persistEditorTabs();
    if (wasActive) {
      activeTabKey = activeSessionName ? terminalTabKey(activeSessionName) : "";
    }
    syncOpenTabsToSessions();
    if (wasActive && activeSessionName) {
      focusTerminal();
    }
  }

  function ensureTreeNode(tab, path) {
    if (!tab.tree[path]) {
      tab.tree[path] = {
        loaded: false,
        expanded: false,
        entries: [],
        error: "",
        truncated: false,
      };
    }
    return tab.tree[path];
  }

  function requestFileList(tab, path) {
    if (!tab || !path) {
      return;
    }
    const node = ensureTreeNode(tab, path);
    node.expanded = true;
    node.error = "";
    tab.loadingPath = path;
    tab.error = "";
    renderFileWorkspace();
    const requestId = sendFileCommand("fs-list", { path }, { kind: "list", tabId: tab.id, path });
    if (!requestId) {
      tab.loadingPath = "";
      renderFileWorkspace();
    }
  }

  function requestFileRead(tab, path) {
    if (!tab || !path) {
      return;
    }
    const existingFile = tab.openFiles.find((file) => sameFilePath(file.path, path));
    if (existingFile && existingFile.loaded) {
      tab.activeFilePath = existingFile.path;
      tab.error = "";
      if (fileTreeUsesDrawer()) {
        tab.treeHidden = true;
      }
      persistEditorTabs();
      renderFileWorkspace();
      return;
    }
    if (existingFile) {
      tab.activeFilePath = existingFile.path;
    }
    tab.loadingPath = path;
    tab.error = "";
    renderFileWorkspace();
    const requestId = sendFileCommand("fs-read", { path }, { kind: "read", tabId: tab.id, path });
    if (!requestId) {
      tab.loadingPath = "";
      renderFileWorkspace();
    }
  }

  function saveActiveFile() {
    const tab = activeEditorTab();
    const file = activeOpenFile(tab);
    if (!tab || !file || !file.dirty) {
      return;
    }
    tab.loadingPath = file.path;
    tab.error = "";
    updateFileControls(tab);
    const requestId = sendFileCommand(
      "fs-write",
      { path: file.path, content: file.content },
      { kind: "write", tabId: tab.id, path: file.path },
    );
    if (!requestId) {
      tab.loadingPath = "";
      updateFileControls(tab);
    }
  }

  function toggleDirectory(tab, path) {
    const node = ensureTreeNode(tab, path);
    if (node.expanded) {
      node.expanded = false;
      renderFileWorkspace();
      return;
    }
    node.expanded = true;
    if (!node.loaded) {
      requestFileList(tab, path);
      return;
    }
    renderFileWorkspace();
  }

  function renderFileTreeRow(tab, entry, depth, parent) {
    const isDirectory = entry.type === "directory";
    const node = isDirectory ? ensureTreeNode(tab, entry.path) : null;
    const activePath = activeOpenFile(tab)?.path || tab.activeFilePath;
    const isActiveFile = !isDirectory && sameFilePath(entry.path, activePath);
    const row = document.createElement("button");
    row.type = "button";
    row.className = [
      "file-tree-row",
      isDirectory ? "is-directory" : "is-file",
      isActiveFile ? "is-selected is-active-file" : "",
    ].filter(Boolean).join(" ");
    row.style.setProperty("--tree-depth", String(depth));
    row.dataset.path = entry.path;
    if (isActiveFile) {
      row.dataset.currentFile = "true";
      row.setAttribute("aria-current", "true");
    }

    const marker = document.createElement("span");
    marker.className = "file-tree-marker";
    marker.textContent = isDirectory ? (node.expanded ? "v" : ">") : "";
    row.appendChild(marker);

    const label = document.createElement("span");
    label.className = "file-tree-label";
    label.textContent = entry.name || entry.path;
    row.appendChild(label);

    row.addEventListener("click", () => {
      if (isDirectory) {
        toggleDirectory(tab, entry.path);
        return;
      }
      requestFileRead(tab, entry.path);
    });
    parent.appendChild(row);

    if (isDirectory && node.expanded) {
      if (node.error) {
        renderFileTreeMessage(node.error, depth + 1, parent);
      } else if (!node.loaded) {
        renderFileTreeMessage("Loading...", depth + 1, parent);
      } else if (!node.entries.length) {
        renderFileTreeMessage("Empty", depth + 1, parent);
      } else {
        node.entries.forEach((child) => renderFileTreeRow(tab, child, depth + 1, parent));
        if (node.truncated) {
          renderFileTreeMessage("Directory truncated", depth + 1, parent);
        }
      }
    }
  }

  function renderFileTreeMessage(message, depth, parent) {
    const item = document.createElement("div");
    item.className = "file-tree-message";
    item.style.setProperty("--tree-depth", String(depth));
    item.textContent = message;
    parent.appendChild(item);
  }

  function renderFileTree(tab) {
    fileTree.innerHTML = "";
    if (!tab) {
      renderFileTreeMessage("Open a file tab", 0, fileTree);
      return;
    }
    const rootNode = ensureTreeNode(tab, tab.root);
    if (rootNode.error) {
      renderFileTreeMessage(rootNode.error, 0, fileTree);
      return;
    }
    if (!rootNode.loaded) {
      renderFileTreeMessage(tab.loadingPath === tab.root ? "Loading..." : "Open root", 0, fileTree);
      return;
    }
    if (!rootNode.entries.length) {
      renderFileTreeMessage("Empty", 0, fileTree);
      return;
    }
    revealActiveFileInTree(tab);
    rootNode.entries.forEach((entry) => renderFileTreeRow(tab, entry, 0, fileTree));
    if (rootNode.truncated) {
      renderFileTreeMessage("Directory truncated", 0, fileTree);
    }
    scheduleActiveFileTreeScroll();
  }

  function revealActiveFileInTree(tab) {
    const activePath = activeOpenFile(tab)?.path || tab?.activeFilePath || "";
    const rootNode = tab ? tab.tree[tab.root] : null;
    if (!tab || !activePath || !rootNode?.loaded) {
      return;
    }
    const ancestors = activeFileAncestorPaths(tab, activePath);
    let parentNode = rootNode;
    for (const ancestorPath of ancestors.slice(1)) {
      const entry = (parentNode.entries || []).find((item) => (
        item.type === "directory" && sameFilePath(item.path, ancestorPath)
      ));
      if (!entry) {
        break;
      }
      const node = ensureTreeNode(tab, entry.path);
      node.expanded = true;
      if (!node.loaded) {
        queueRevealDirectoryLoad(tab, entry.path);
        return;
      }
      parentNode = node;
    }

    const revealInEntries = (entries) => {
      for (const entry of entries) {
        if (sameFilePath(entry.path, activePath)) {
          return true;
        }
        if (entry.type !== "directory") {
          continue;
        }
        const node = tab.tree[entry.path];
        if (!node?.loaded || !revealInEntries(node.entries || [])) {
          continue;
        }
        node.expanded = true;
        return true;
      }
      return false;
    };

    revealInEntries(rootNode.entries || []);
  }

  function activeFileAncestorPaths(tab, activePath) {
    const root = normalizeFilePathForCompare(tab?.root || "");
    const active = normalizeFilePathForCompare(activePath);
    if (!root || !active || active === root || !active.startsWith(`${root}/`)) {
      return tab?.root ? [tab.root] : [];
    }
    const relative = active.slice(root.length).replace(/^[\\/]+/, "");
    const pieces = relative.split(/[\\/]/).filter(Boolean);
    const ancestors = [tab.root];
    let current = root;
    pieces.slice(0, -1).forEach((piece) => {
      current = `${current}/${piece}`;
      ancestors.push(current);
    });
    return ancestors;
  }

  function queueRevealDirectoryLoad(tab, path) {
    if (!tab || !path || tab.loadingPath || tab.pendingRevealPath === path) {
      return;
    }
    tab.pendingRevealPath = path;
    window.requestAnimationFrame(() => {
      if (tab.pendingRevealPath !== path) {
        return;
      }
      tab.pendingRevealPath = "";
      const node = ensureTreeNode(tab, path);
      if (node.loaded || tab.loadingPath) {
        renderFileWorkspace();
        return;
      }
      requestFileList(tab, path);
    });
  }

  function scheduleActiveFileTreeScroll() {
    window.requestAnimationFrame(() => {
      const activeRow = fileTree.querySelector('[data-current-file="true"]');
      if (!activeRow || fileTreePanel.offsetParent === null) {
        return;
      }
      activeRow.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }

  function switchOpenFile(path) {
    const tab = activeEditorTab();
    const file = tab?.openFiles.find((item) => sameFilePath(item.path, path));
    if (!tab || !path || !file) {
      return;
    }
    tab.activeFilePath = file.path;
    tab.error = "";
    persistEditorTabs();
    renderFileWorkspace();
    if (!file.loaded) {
      requestFileRead(tab, path);
      return;
    }
    if (!file.previewMode) {
      fileEditorInput.focus({ preventScroll: true });
    }
  }

  function closeOpenFile(path) {
    const tab = activeEditorTab();
    if (!tab || !path) {
      return;
    }
    const file = tab.openFiles.find((item) => sameFilePath(item.path, path));
    if (!file) {
      return;
    }
    if (file.dirty && !window.confirm(`Discard unsaved changes in "${file.name}"?`)) {
      return;
    }
    const index = tab.openFiles.findIndex((item) => sameFilePath(item.path, path));
    tab.openFiles = tab.openFiles.filter((item) => !sameFilePath(item.path, path));
    if (sameFilePath(tab.activeFilePath, path)) {
      tab.activeFilePath =
        tab.openFiles[Math.max(0, Math.min(index, tab.openFiles.length - 1))]?.path || "";
    }
    persistEditorTabs();
    renderFileWorkspace();
  }

  function renderOpenFileTabs(tab) {
    fileEditorTabs.innerHTML = "";
    if (!tab || !tab.openFiles.length) {
      fileEditorTabs.classList.add("hidden");
      return;
    }
    fileEditorTabs.classList.remove("hidden");
    tab.openFiles.forEach((file) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = `file-editor-tab${sameFilePath(file.path, tab.activeFilePath) ? " is-active" : ""}${file.dirty ? " is-dirty" : ""}`;
      item.addEventListener("click", () => switchOpenFile(file.path));

      const label = document.createElement("span");
      label.className = "file-editor-tab-label";
      label.textContent = file.dirty ? `${file.name} *` : file.name;
      item.appendChild(label);

      const closeButton = document.createElement("span");
      closeButton.className = "file-editor-tab-close";
      closeButton.textContent = "x";
      closeButton.addEventListener("click", (event) => {
        event.stopPropagation();
        closeOpenFile(file.path);
      });
      item.appendChild(closeButton);
      fileEditorTabs.appendChild(item);
    });
  }

  function updateFileControls(tab) {
    const file = activeOpenFile(tab);
    const hasFile = Boolean(file);
    const markdownFile = hasFile && isMarkdownFile(file);
    const previewMode = markdownFile && file.loaded && file.previewMode === true;
    filePathLabel.textContent = file ? file.path : "No file selected";
    fileStatus.textContent = tab?.error || (file?.dirty ? "Unsaved" : tab?.loadingPath || (file && !file.loaded) ? "Loading..." : "");
    fileSaveButton.disabled = !tab || !hasFile || !file.dirty || Boolean(tab.loadingPath);
    fileEditorInput.disabled = !hasFile || !file.loaded;
    fileEditorInput.classList.toggle("hidden", previewMode);
    fileMarkdownToggleButton.classList.toggle("hidden", !markdownFile);
    fileMarkdownToggleButton.classList.toggle("is-active", previewMode);
    fileMarkdownToggleButton.disabled = !markdownFile || !file.loaded;
    fileMarkdownToggleButton.textContent = previewMode ? "Editor" : "Preview";
    fileMarkdownPreview.classList.toggle("hidden", !previewMode);
    fileMarkdownPreview.innerHTML = previewMode ? markdownToHtml(file.content) : "";
  }

  function renderFileWorkspace() {
    const tab = activeEditorTab();
    if (tab && tab.openFiles.length && !tab.openFiles.some((file) => sameFilePath(file.path, tab.activeFilePath))) {
      tab.activeFilePath = tab.openFiles[0].path;
    }
    const treeOpen = Boolean(tab && tab.treeHidden !== true);
    fileWorkspace.classList.toggle("is-tree-hidden", !treeOpen);
    fileWorkspace.classList.toggle("is-tree-open", treeOpen);
    fileWorkspaceRoot.textContent = tab?.root || "";
    fileWorkspaceTitle.textContent = tab?.name || "Files";
    fileTreeToggleButton.classList.toggle("is-active", treeOpen);
    renderFileTree(tab);
    renderOpenFileTabs(tab);
    updateFileControls(tab);
    const file = activeOpenFile(tab);
    const nextValue = file?.content || "";
    if (document.activeElement !== fileEditorInput && fileEditorInput.value !== nextValue) {
      fileEditorInput.value = nextValue;
    }
    if (!tab) {
      fileEditorInput.value = "";
      fileEditorInput.disabled = true;
    }
  }

  function handleFileServerMessage(payload) {
    const requestId = String(payload.requestId || "");
    const context = pendingFileRequests.get(requestId);
    if (!context || context.profileId !== activeProfileId) {
      return;
    }
    pendingFileRequests.delete(requestId);
    if (context.timeoutId) {
      window.clearTimeout(context.timeoutId);
    }

    if (payload.type === "fs-default-root") {
      lastDefaultFileRoot = payload.path || lastDefaultFileRoot;
      if (payload.home) {
        useHomeRootButton.dataset.path = payload.home;
      }
      if (!fileRootOverlay.classList.contains("hidden") && !fileRootInput.value.trim()) {
        fileRootInput.value = lastDefaultFileRoot;
      }
      return;
    }

    const tab = editorTabById(context.tabId);
    if (!tab) {
      return;
    }

    if (payload.type === "fs-error") {
      tab.loadingPath = "";
      tab.error = payload.message || "File operation failed.";
      if (context.kind === "list" && context.path) {
        const node = ensureTreeNode(tab, context.path);
        node.loaded = false;
        node.error = tab.error;
      }
      showToast(tab.error);
      renderFileWorkspace();
      return;
    }

    if (payload.type === "fs-list") {
      tab.loadingPath = "";
      const resolvedPath = payload.path || context.path || tab.root;
      if (sameFilePath(context.path, tab.root) && !sameFilePath(resolvedPath, tab.root)) {
        const shouldRename = tab.name === pathBaseName(tab.root);
        delete tab.tree[tab.root];
        tab.root = resolvedPath;
        if (shouldRename || !tab.name) {
          tab.name = pathBaseName(resolvedPath);
        }
        persistEditorTabs();
      }
      tab.tree[resolvedPath] = {
        loaded: true,
        expanded: true,
        entries: Array.isArray(payload.entries) ? payload.entries : [],
        error: "",
        truncated: payload.truncated === true,
      };
      tab.error = "";
      syncOpenTabsToSessions();
      return;
    }

    if (payload.type === "fs-read") {
      tab.loadingPath = "";
      const path = payload.path || context.path || "";
      const name = payload.name || pathBaseName(path);
      const content = String(payload.content || "");
      const existingIndex = tab.openFiles.findIndex((file) => sameFilePath(file.path, path));
      const existingFile = existingIndex >= 0 ? tab.openFiles[existingIndex] : null;
      const nextFile = {
        path,
        name,
        content,
        originalContent: content,
        dirty: false,
        loaded: true,
        previewMode: existingFile?.previewMode === true,
      };
      if (existingIndex >= 0) {
        tab.openFiles[existingIndex] = nextFile;
      } else {
        tab.openFiles.push(nextFile);
      }
      tab.activeFilePath = nextFile.path;
      if (fileTreeUsesDrawer()) {
        tab.treeHidden = true;
      }
      tab.error = "";
      persistEditorTabs();
      renderFileWorkspace();
      if (!nextFile.previewMode) {
        fileEditorInput.focus({ preventScroll: true });
      }
      return;
    }

    if (payload.type === "fs-write") {
      tab.loadingPath = "";
      const path = payload.path || context.path || tab.activeFilePath;
      const file = tab.openFiles.find((item) => sameFilePath(item.path, path));
      if (file) {
        file.originalContent = file.content;
        file.dirty = false;
        file.loaded = true;
      }
      tab.error = "";
      persistEditorTabs();
      renderOpenFileTabs(tab);
      updateFileControls(tab);
      showToast(`Saved ${payload.name || pathBaseName(path)}.`);
    }
  }

  function handleFileRequestTimeout(context) {
    if (context.kind === "default-root") {
      fileRootMessage.textContent = "No file response from the server. Restart the server to enable file tabs.";
      return;
    }
    const tab = editorTabById(context.tabId);
    if (!tab) {
      return;
    }
    tab.loadingPath = "";
    tab.error = "No file response from the server. Restart the server to enable file tabs.";
    if (context.kind === "list" && context.path) {
      const node = ensureTreeNode(tab, context.path);
      node.loaded = false;
      node.error = tab.error;
    }
    renderFileWorkspace();
    showToast(tab.error);
  }

  function fileTreeUsesDrawer() {
    return window.matchMedia("(max-aspect-ratio: 4 / 5), (max-width: 720px)").matches;
  }

  function closeBootSocket() {
    if (window.__mtBoot && window.__mtBoot.ws) {
      try {
        window.__mtBoot.ws.close();
      } catch (_error) {
        // ignore
      }
    }
    window.__mtBoot = null;
  }

  async function applyAuthoritativeAuthenticationScope(realm) {
    const previousScope = authenticationScope();
    loginRealm = realm;
    applyAuthenticationScope(realm);
    if (!resumeHandlingReady || authenticationScope(realm) === previousScope) {
      return;
    }
    setPasskeyLocked(true);
    const pendingResumeDecision = resumeDecisionPromise;
    if (pendingResumeDecision) {
      try {
        await pendingResumeDecision;
      } catch (_error) {
        // The authoritative scope still needs its own fail-closed decision.
      }
    }
    passkeyRequiredScope = "";
    passkeyRetryPending = false;
    initialResumeDecisionMade = false;
    handledResumeMarker = "";
    backgroundRecordedScope = "";
    await resumeApplication();
  }

  // Reply to the server's challenge with a realm- and RP-bound device proof.
  async function sendAuthResponse(
    nonce,
    realm = loginRealm,
    profile = "",
    rpId = serverConfig.rpId || location.hostname,
    authSocket = socket,
  ) {
    if (!authSocket || authSocket !== socket || authSocket.readyState !== WebSocket.OPEN) return;
    const keyRealm = serverConfig.profileMode ? realm : loginRealm;
    if (serverConfig.profileMode && realm) {
      await applyAuthoritativeAuthenticationScope(realm);
    }
    const token = localStorage.getItem(tokenStorageKey(keyRealm)) || "";
    const scope = authenticationScope(keyRealm);
    const protocolRealm = deviceProtocolRealm(realm);
    const protocolProfile = String(profile || "");
    const protocolRpId = String(rpId || location.hostname);
    const msg = {
      type: "auth",
      user: currentUser,
      realm: protocolRealm,
      profile: protocolProfile,
      deviceId: getDeviceId(),
      requirePasskey: passkeyRequiredScope === scope,
    };
    if (token) msg.token = token;
    try {
      const rec = await loadDeviceKey(keyRealm);
      if (rec && rec.privateKey && nonce) {
        msg.signature = await signDeviceTranscript(
          rec.privateKey,
          deviceAuthenticationTranscript(
            protocolRpId,
            protocolRealm,
            protocolProfile,
            nonce,
          ),
        );
      }
    } catch (e) {
      /* fall back to WebAuthn bootstrap */
    }
    if (authSocket === socket && authSocket.readyState === WebSocket.OPEN) {
      authSocket.send(JSON.stringify(msg));
    }
  }

  async function enrollDeviceKey(payload, enrollmentSocket = socket) {
    const enrollmentId = payload?.enrollmentId;
    const nonce = payload?.nonce;
    const realm = payload?.realm;
    const profile = payload?.profile;
    const rpId = payload?.rpId;
    if (
      ![enrollmentId, nonce, realm, profile, rpId].every((value) => typeof value === "string") ||
      !enrollmentId ||
      !nonce ||
      !rpId
    ) {
      return;
    }
    const keyRealm = serverConfig.profileMode ? realm : loginRealm;
    try {
      const rec = await ensureDeviceKey(keyRealm);
      const deviceId = getDeviceId();
      const publicKey = await exportPublicSpki(rec.publicKey);
      const signature = await signDeviceTranscript(
        rec.privateKey,
        deviceEnrollmentTranscript(
          rpId,
          realm,
          profile,
          enrollmentId,
          nonce,
          deviceId,
          publicKey,
        ),
      );
      if (
        enrollmentSocket !== socket ||
        enrollmentSocket?.readyState !== WebSocket.OPEN
      ) {
        return;
      }
      enrollmentSocket.send(
        JSON.stringify({
          type: "register-key",
          enrollmentId,
          nonce,
          realm,
          profile,
          deviceId,
          publicKey,
          signature,
        }),
      );
      if (authenticationScope(keyRealm) === authenticationScope()) {
        hasDeviceKey = true;
      }
    } catch (e) {
      /* enrollment is best-effort */
    }
  }

  function setPasskeyLocked(locked) {
    document.body.classList.toggle("passkey-locked", locked);
  }

  function applyTerminalReadyVisibility(readyIsHidden) {
    terminalReadyWhileHidden = readyIsHidden;
    setPasskeyLocked(readyIsHidden);
  }

  function revealTerminalAfterVisibleResume() {
    const revealHiddenReady = terminalReadyWhileHidden;
    terminalReadyWhileHidden = false;
    setPasskeyLocked(false);
    return revealHiddenReady;
  }

  function writeBackgroundedAt(realm = loginRealm, backgroundedAt = Date.now()) {
    if (
      authenticationScope(realm) !== authenticationScope() ||
      !Number.isSafeInteger(backgroundedAt) ||
      backgroundedAt <= 0
    ) {
      return false;
    }
    try {
      localStorage.setItem(
        authenticationStorageKey(STORAGE_PASSKEY_BACKGROUNDED_AT_KEY, realm),
        String(backgroundedAt),
      );
      return true;
    } catch (_error) {
      return false;
    }
  }

  function recordVisibleIdleCheckpoint() {
    const realm = loginRealm;
    if (
      !resumeHandlingReady ||
      !initialResumeDecisionMade ||
      document.visibilityState !== "visible" ||
      waitingForProxyAuth ||
      passkeyRequiredScope ||
      terminalReadyWhileHidden ||
      loadAuthenticationSettings(realm).mode !== "idle"
    ) {
      return false;
    }
    return writeBackgroundedAt(realm, Date.now());
  }

  function recordBackgrounded(event) {
    if (event?.type !== "pagehide" && document.visibilityState !== "hidden") {
      return;
    }
    const realm = loginRealm;
    const scope = authenticationScope(realm);
    if (backgroundRecordedScope === scope) {
      return;
    }
    const backgroundedAt = Date.now();
    if (loadAuthenticationSettings(realm).mode !== "off") {
      // Obscure the terminal before the browser captures an app-switcher frame.
      setPasskeyLocked(true);
    }
    backgroundRecordedScope = scope;
    // A failed write leaves an unknown marker, which fails closed for idle and
    // every-open policies.
    writeBackgroundedAt(realm, backgroundedAt);
  }

  function parseBackgroundedAt(raw) {
    if (raw === null) {
      return 0;
    }
    if (typeof raw !== "string" || !/^\d+$/.test(raw)) {
      return Number.NaN;
    }
    const value = Number(raw);
    return Number.isSafeInteger(value) && value > 0 ? value : Number.NaN;
  }

  function passkeyRequiredAfterBackground(settings, backgroundedAt, initial, now = Date.now()) {
    if (settings.mode === "off") {
      return false;
    }
    if (settings.mode === "every-open") {
      return initial || backgroundedAt !== 0;
    }
    if (backgroundedAt === 0 || !Number.isFinite(backgroundedAt) || !Number.isFinite(now)) {
      return true;
    }
    const elapsed = now - backgroundedAt;
    return elapsed < 0 || elapsed >= settings.idleMinutes * 60 * 1000;
  }

  async function runResumeDecision() {
    const realm = loginRealm;
    const scope = authenticationScope(realm);
    const backgroundKey = authenticationStorageKey(
      STORAGE_PASSKEY_BACKGROUNDED_AT_KEY,
      realm,
    );
    let rawBackgroundMarker = null;
    try {
      rawBackgroundMarker = localStorage.getItem(backgroundKey);
    } catch (_error) {
      // Treat unavailable storage as an unknown marker and fail closed.
    }
    const resumeMarker = `${scope}:${rawBackgroundMarker ?? "initial"}`;
    const initial = !initialResumeDecisionMade;
    initialResumeDecisionMade = true;
    backgroundRecordedScope = "";
    if (handledResumeMarker === resumeMarker) {
      if (!passkeyRequiredScope) {
        refreshConnection();
      }
      return;
    }
    handledResumeMarker = resumeMarker;
    const settings = loadAuthenticationSettings(realm);
    const requiresPasskey = passkeyRequiredAfterBackground(
      settings,
      parseBackgroundedAt(rawBackgroundMarker),
      initial,
    );
    if (requiresPasskey) {
      // Lock before IndexedDB access so key loss and slow storage cannot reveal
      // a frame of terminal contents before the WebAuthn gate is established.
      passkeyRequiredScope = scope;
      passkeyRetryPending = false;
      setPasskeyLocked(true);
    }
    await refreshDeviceKeyFlag(realm);
    if (scope !== authenticationScope()) {
      return;
    }
    if (!requiresPasskey && settings.mode === "idle" && !passkeyRequiredScope) {
      writeBackgroundedAt(realm, Date.now());
    }
    if (requiresPasskey) {
      if (!initial) {
        reconnectSocket();
      }
      return;
    }
    if (!passkeyRequiredScope && (!initial || terminalReadyWhileHidden)) {
      const revealHiddenReady = revealTerminalAfterVisibleResume();
      if (revealHiddenReady && activeEditorTab() === null) {
        focusTerminal();
        performLayoutNow();
      }
      if (!initial) {
        refreshConnection();
      }
    }
  }

  function waitForVisibleResume() {
    return new Promise((resolve) => {
      const handleResumeSignal = () => {
        if (document.visibilityState !== "visible") {
          return;
        }
        document.removeEventListener("visibilitychange", handleResumeSignal);
        window.removeEventListener("focus", handleResumeSignal);
        window.removeEventListener("pageshow", handleResumeSignal);
        resolve();
      };
      document.addEventListener("visibilitychange", handleResumeSignal);
      window.addEventListener("focus", handleResumeSignal);
      window.addEventListener("pageshow", handleResumeSignal);
    });
  }

  function resumeApplication() {
    if (!resumeHandlingReady) {
      return Promise.resolve();
    }
    if (resumeDecisionPromise) {
      return resumeDecisionPromise;
    }
    resumeDecisionPromise = (async () => {
      if (document.visibilityState === "hidden") {
        await waitForVisibleResume();
      }
      return runResumeDecision();
    })().finally(() => {
      resumeDecisionPromise = null;
    });
    return resumeDecisionPromise;
  }

  function reconnectSocket() {
    cancelPasskeyCeremony();
    window.clearTimeout(reconnectTimer);
    window.clearTimeout(resumeProbeTimer);
    reconnectTimer = null;
    resumeProbeTimer = null;
    const previousSocket = socket;
    socket = null;
    updateProfileConnectionState();
    if (
      previousSocket &&
      (previousSocket.readyState === WebSocket.CONNECTING || previousSocket.readyState === WebSocket.OPEN)
    ) {
      try {
        previousSocket.close();
      } catch (_error) {
        // A dead iOS socket may already be detached from its network process.
      }
    }
    connect();
  }

  function refreshConnection() {
    window.clearTimeout(resumeProbeTimer);
    resumeProbeTimer = null;
    if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
      reconnectSocket();
      return;
    }
    if (socket.readyState === WebSocket.CONNECTING) {
      if (performance.now() - socketConnectStartedAt > 10000) {
        reconnectSocket();
      }
      return;
    }

    const observedSocket = socket;
    const observedMessageAt = lastServerMessageAt;
    sendMessage({ type: "request-tabs" });
    sendMessage({ type: "request-sessions" });
    sendMessage({ type: "request-settings" });
    if (serverConfig.profileMode) sendMessage({ type: "request-profiles" });
    resumeProbeTimer = window.setTimeout(() => {
      resumeProbeTimer = null;
      if (
        socket === observedSocket &&
        observedSocket.readyState === WebSocket.OPEN &&
        lastServerMessageAt <= observedMessageAt
      ) {
        reconnectSocket();
      }
    }, 4000);
  }

  function connect() {
    const token = localStorage.getItem(tokenStorageKey());
    currentUser = localStorage.getItem(STORAGE_USER_KEY) || "";
    // Auto-login by Tailscale identity (behind `tailscale serve`): the server
    // already knows who this is, so no token/username prompt is needed.
    if (serverConfig.autoUser) {
      currentUser = serverConfig.autoUser;
    }
    // An enrolled device signs the server's nonce, so it needs neither a token
    // nor a username prompt — it authenticates silently from anywhere.
    const profileRequiresToken = loginRequiresToken();
    const passkeyAvailable = loginSupportsPasskey();
    const needsToken = profileRequiresToken && !token && !hasDeviceKey && !passkeyAvailable;
    const needsUser = serverConfig.multiTenant && !currentUser && !hasDeviceKey;
    if (needsToken || needsUser) {
      closeBootSocket(); // can't use the pre-opened socket if we must prompt first
      syncLoginFields();
      loginOverlay.classList.remove("hidden");
      (needsUser && userInput ? userInput : tokenInput).focus();
      scheduleAuthConfigPolling();
      return;
    }

    window.clearTimeout(reconnectTimer);
    window.clearTimeout(resumeProbeTimer);
    reconnectTimer = null;
    resumeProbeTimer = null;
    stopAuthConfigPolling();
    loginOverlay.classList.add("hidden");

    // Adopt the socket the inline <head> bootstrap opened during page parse (its
    // TLS+WS handshake overlapped asset download); otherwise open a fresh one.
    const boot = window.__mtBoot;
    let bootQueue = null;
    if (boot && boot.ws && (boot.ws.readyState === WebSocket.OPEN || boot.ws.readyState === WebSocket.CONNECTING)) {
      socket = boot.ws;
      socket.removeEventListener("message", boot.onMsg);
      bootQueue = boot.queue;
      window.__mtBoot = null;
    } else {
      closeBootSocket();
      socket = new WebSocket(wsUrl());
    }
    socketConnectStartedAt = performance.now();
    const thisSocket = socket;
    socket.binaryType = "arraybuffer";
    socketMessageChain = Promise.resolve();
    pendingTerminalOutput = null;
    pendingTerminalSeed = null;
    terminalAuthoritative = false;
    for (const pending of pendingSelectionRequests.values()) {
      window.clearTimeout(pending.timer);
      pending.resolve({ error: "Terminal changed; select again." });
    }
    pendingSelectionRequests.clear();
    updateProfileConnectionState();

    // Auth is challenge-driven now: the server sends {type:"auth-challenge",
    // nonce} and we reply via sendAuthResponse() (signing the nonce with the
    // device key when enrolled, else falling back to token/username). The early
    // boot socket buffers that challenge; it's replayed below.

    socket.addEventListener("open", () => {
      if (socket !== thisSocket) {
        return;
      }
      updateProfileConnectionState();
    });

    const processSocketMessage = async (event) => {
      if (socket !== thisSocket) {
        return;
      }
      lastServerMessageAt = performance.now();
      window.clearTimeout(resumeProbeTimer);
      resumeProbeTimer = null;
      if (typeof event.data === "string") {
        const payload = JSON.parse(event.data);
        await handleServerMessage(payload);
        return;
      }
      await handleTerminalBinary(event.data);
    };
    const onSocketMessage = (event) => {
      socketMessageChain = socketMessageChain
        .then(() => processSocketMessage(event))
        .catch((error) => {
          console.error(error);
          terminalAuthoritative = false;
          if (socket === thisSocket) {
            reconnectSocket();
          }
        });
    };
    socket.addEventListener("message", onSocketMessage);
    // Replay anything the bootstrap socket buffered before app.js attached.
    if (bootQueue && bootQueue.length) {
      const pending = bootQueue.splice(0, bootQueue.length);
      for (const bufferedEvent of pending) {
        onSocketMessage(bufferedEvent);
      }
    }

    socket.addEventListener("close", (event) => {
      if (socket !== thisSocket) {
        return;
      }
      updateProfileConnectionState();
      window.clearTimeout(resumeProbeTimer);
      resumeProbeTimer = null;
      if (reconnectForSessionSwitch) {
        reconnectForSessionSwitch = false;
        window.setTimeout(connect, 80);
        return;
      }
      if (passkeyRetryPending && passkeyRequiredScope) {
        return;
      }
      if (event.code === 4001) {
        waitingForProxyAuth = false;
        setPasskeyRetryUi(false);
        loginOverlay.classList.remove("hidden");
        loginMessage.textContent = "Authentication failed. Check the token and try again.";
        localStorage.removeItem(tokenStorageKey());
        scheduleAuthConfigPolling();
        return;
      }
      if (event.code === 4003) {
        showToast("This device is not allowed to connect.");
        return;
      }
      reconnectTimer = window.setTimeout(connect, 1500);
    });
  }

  function setPasskeyRetryUi(retry) {
    tokenFieldLabel.classList.toggle("hidden", retry);
    tokenInput.classList.toggle("hidden", retry);
    if (userField) {
      userField.classList.toggle("hidden", retry || !serverConfig.multiTenant);
    }
    loginSubmitButton.textContent = retry ? "Retry passkey" : "Connect";
  }

  function showProxySignIn(payload, message) {
    setPasskeyRetryUi(false);
    waitingForProxyAuth = true;
    pendingProfileId = payload.profile || pendingProfileId || activeProfileId;
    loginRealm = payload.realm || activeProfile()?.authRealm || loginRealm;
    loginOverlay.classList.remove("hidden");
    loginMessage.textContent = message;
    tokenInput.value = "";
    tokenInput.focus();
  }

  async function handleServerMessage(payload) {
    if (payload.type === "terminal-output") {
      if (pendingTerminalOutput) {
        terminalAuthoritative = false;
        throw new Error("Terminal output metadata overlapped");
      }
      pendingTerminalOutput = payload;
      return;
    }
    if (payload.type === "seed-start") {
      terminalAuthoritative = false;
      pendingTerminalSeed = null;
      pendingSeedScrollTarget = null;
      clearTerminalSelectionUI();
      sendMessage({
        type: "seed-start-ack",
        epoch: payload.epoch,
        cols: term.cols,
        rows: term.rows,
      });
      return;
    }
    if (payload.type === "seed-data") {
      pendingTerminalSeed = payload;
      return;
    }
    if (payload.type === "seed-end") {
      if (!pendingTerminalSeed || Number(pendingTerminalSeed.epoch) !== Number(payload.epoch)) {
        throw new Error("Terminal seed ended without matching data");
      }
      await applyTerminalSeed(pendingTerminalSeed);
      pendingTerminalSeed = null;
      sendMessage({ type: "seed-ack", epoch: payload.epoch });
      return;
    }
    if (payload.type === "post-flush") {
      sendMessage({ type: "post-flush-ack", epoch: payload.epoch, cycle: payload.cycle });
      return;
    }
    if (payload.type === "seed-open") {
      if (
        Number(payload.epoch) !== terminalEpoch ||
        String(payload.paneId || "") !== terminalPaneId ||
        Number(payload.cutoff) !== terminalCutoff ||
        Number(payload.layoutGeneration) !== terminalLayoutGeneration
      ) {
        terminalAuthoritative = false;
        return;
      }
      terminalAuthoritative = true;
      historyReseedPending = false;
      if (pendingSeedScrollTarget !== null && term.buffer.active.type === "normal") {
        term.scrollToLine(Math.max(0, pendingSeedScrollTarget + terminalSeedHistory));
        followOutput = false;
      }
      pendingSeedScrollTarget = null;
      if (
        lastTerminalCols > 0 &&
        lastTerminalRows > 0 &&
        (term.cols !== lastTerminalCols || term.rows !== lastTerminalRows)
      ) {
        sendMessage({ type: "resize", cols: lastTerminalCols, rows: lastTerminalRows });
      }
      return;
    }
    if (payload.type === "selection-check") {
      const pending = pendingSelectionRequests.get(String(payload.requestId || ""));
      sendMessage({
        type: "selection-check-ack",
        requestId: payload.requestId,
        epoch: terminalEpoch,
        unchanged: pendingSelectionRequestIsCurrent(pending),
      });
      return;
    }
    if (payload.type === "selection-result") {
      const requestId = String(payload.requestId || "");
      const pending = pendingSelectionRequests.get(requestId);
      if (pending) {
        window.clearTimeout(pending.timer);
        pendingSelectionRequests.delete(requestId);
        pending.resolve(payload.error ? { error: payload.error } : { text: String(payload.text || "") });
      }
      return;
    }
    if (
      window.MobileTerminalPasskeys &&
      (payload.type === "webauthn-auth-options" || payload.type === "webauthn-register-options")
    ) {
      waitingForProxyAuth = true;
      pendingProfileId = payload.profile || pendingProfileId || activeProfileId;
      const authenticationRealm = payload.realm || activeProfile()?.authRealm || loginRealm;
      if (serverConfig.profileMode && authenticationRealm) {
        await applyAuthoritativeAuthenticationScope(authenticationRealm);
      } else {
        loginRealm = authenticationRealm;
      }
      const authenticationSocket = socket;
      cancelPasskeyCeremony();
      const ceremonyController =
        typeof AbortController === "function" ? new AbortController() : null;
      passkeyCeremonyController = ceremonyController;
      const sendAuthenticationMessage = (message) => {
        if (
          authenticationSocket !== socket ||
          authenticationSocket?.readyState !== WebSocket.OPEN
        ) {
          return;
        }
        authenticationSocket.send(JSON.stringify(message));
      };
      try {
        if (
          await window.MobileTerminalPasskeys.handleMessage(
            payload,
            sendAuthenticationMessage,
            ceremonyController?.signal,
          )
        ) {
          return;
        }
      } catch (_error) {
        if (authenticationSocket !== socket) {
          return;
        }
        passkeyRequiredScope = passkeyRequiredScope || authenticationScope(loginRealm);
        passkeyRetryPending = true;
        waitingForProxyAuth = false;
        setPasskeyLocked(true);
        setPasskeyRetryUi(true);
        loginOverlay.classList.remove("hidden");
        loginMessage.textContent = "A passkey is required to reveal the terminal. Tap Retry passkey to try again.";
        try {
          authenticationSocket?.close(4000, "passkey retry");
        } catch (_closeError) {
          // Ignore a socket already closed by the failed authentication.
        }
        return;
      } finally {
        if (passkeyCeremonyController === ceremonyController) {
          passkeyCeremonyController = null;
        }
      }
    }
    if (String(payload.type || "").startsWith("fs-")) {
      handleFileServerMessage(payload);
      return;
    }
    if (payload.type === "auth-challenge") {
      if (serverConfig.profileMode) {
        waitingForProxyAuth = true;
        pendingProfileId = payload.profile || pendingProfileId || activeProfileId;
      }
      sendAuthResponse(
        payload.nonce,
        payload.realm || loginRealm,
        payload.profile || "",
        payload.rpId || serverConfig.rpId || location.hostname,
        socket,
      );
      return;
    }
    if (payload.type === "profiles") {
      updateProfileInventory(payload.profiles, payload.activeProfile);
      return;
    }
    if (payload.type === "profile-status") {
      profiles = profiles.map((profile) =>
        profile.id === payload.profile
          ? {
              ...profile,
              available: payload.available !== false,
              status: payload.available === false ? "down" : "up",
              statusMessage: payload.message || "",
            }
          : profile,
      );
      applyActiveProfile();
      if (payload.message && payload.available === false) {
        showToast(payload.message);
      }
      if (profileMenuOpen) renderProfileMenu();
      return;
    }
    if (payload.type === "enroll-key") {
      enrollDeviceKey(payload, socket);
      return;
    }
    if (payload.type === "ready") {
      terminalAuthoritative = false;
      clearTerminalSelectionUI();
      const readyIsHidden = document.visibilityState === "hidden";
      applyTerminalReadyVisibility(readyIsHidden);
      if (Array.isArray(payload.profiles) || payload.activeProfile) {
        updateProfileInventory(payload.profiles, payload.activeProfile);
      }
      if (!pendingProfileId || payload.activeProfile === pendingProfileId) {
        pendingProfileId = "";
      }
      waitingForProxyAuth = false;
      passkeyRequiredScope = "";
      passkeyRetryPending = false;
      setPasskeyRetryUi(false);
      if (!readyIsHidden && loadAuthenticationSettings(loginRealm).mode === "idle") {
        writeBackgroundedAt(loginRealm, Date.now());
      }
      if (serverConfig.profileMode && loginSupportsPasskey()) {
        localStorage.removeItem(tokenStorageKey(activeProfile()?.authRealm || loginRealm));
      }
      if (serverConfig.profileMode && payload.principal) {
        currentUserLabel = payload.principal;
      }
      resetComposerRevisionState();
      resetSemanticPromptState();
      resetTerminalBufferSyncState();
      // New pane: assume local scroll until the server reports whether the pane
      // owns scrolling through an alternate screen or mouse tracking.
      activePaneLocalScroll = true;
      if (payload.multiTenant) {
        currentUser = payload.user || currentUser;
        currentUserLabel = payload.userLabel || currentUser;
        if (currentUser) {
          localStorage.setItem(STORAGE_USER_KEY, currentUser);
          // If a different user is now signed in on this browser, drop the
          // previous user's cached prefs + tab state so they don't bleed across
          // users. The per-user host settings message (sent right after this
          // ready) then repopulates this user's own settings.
          if (localStorage.getItem(STORAGE_SETTINGS_OWNER_KEY) !== currentUser) {
            [
              STORAGE_UI_SCALE_KEY,
              STORAGE_TERMINAL_FONT_KEY,
              STORAGE_SHORTCUTS_KEY,
              STORAGE_GESTURES_KEY,
              STORAGE_BTOP_ZOOM_KEY,
              STORAGE_ACTIVE_SESSION_KEY,
              STORAGE_OPEN_TABS_KEY,
              STORAGE_EDITOR_TABS_KEY,
            ].forEach((key) => localStorage.removeItem(key));
            removeAuthenticationStorage();
            resetAuthenticationLifecycle({ locked: readyIsHidden });
            terminalReadyWhileHidden = readyIsHidden;
            // Drop the previous user's in-memory tab set too, so it can't
            // leak into the new user's server-synced open-tab list below.
            openTabNames = [];
            localStorage.setItem(STORAGE_SETTINGS_OWNER_KEY, currentUser);
          }
        }
      }
      updateUserBadge();
      activeSessionName = payload.session || "";
      selectedSessionName = activeSessionName;
      const keepEditorActive = activeEditorTab() !== null;
      if (!keepEditorActive) {
        activeTabKey = activeSessionName ? terminalTabKey(activeSessionName) : "";
      }
      persistActiveSession(activeSessionName);
      // Adopt the server's per-user open-tab set (sent only on the first
      // ready of a connection) so signing in anywhere reopens every tab the
      // user had open. An empty server list (first run after upgrade) is
      // seeded from this device's tabs by the push below.
      if (Array.isArray(payload.openTabs)) {
        if (payload.openTabs.length) {
          setOpenTabs(payload.openTabs);
        }
        openTabsServerSync = true;
      }
      addOpenTab(activeSessionName);
      pushOpenTabsToServer();
      followOutput = true;
      if (!pendingProfileId) {
        loginOverlay.classList.add("hidden");
        loginMessage.textContent = "";
        stopAuthConfigPolling();
      }
      syncOpenTabsToSessions();
      // Re-assert keyboard-closed layout. The keyboard cannot be open at
      // this point (tab pills aren't keyboard inputs), but visualViewport
      // sometimes stays stale on iOS without firing a resize event.
      if (mobileComposerMode) {
        document.documentElement.style.setProperty("--app-top", "0px");
        document.documentElement.style.setProperty("--app-height", `${window.innerHeight}px`);
        document.documentElement.style.setProperty("--keyboard-inset", "0px");
        document.body.dataset.keyboardOpen = "false";
      }
      // focusTerminal re-shows the composer panel, so layout must be measured
      // *after* it to account for the composer's height in --shortcut-reserve.
      if (!keepEditorActive && !readyIsHidden) {
        focusTerminal();
        performLayoutNow();
      }
      // A "To tab" send switched us here. Direct PTY destinations can receive
      // the paste now; mobile composer destinations wait for composer-state so
      // their existing draft and cursor are applied before insertion.
      handlePendingPasteReady();
      return;
    }
    if (payload.type === "tabs") {
      const nextActiveSession =
        payload.tabs?.find((tab) => tab.active)?.name || activeSessionName;
      updateSessionInventory(payload.tabs || [], nextActiveSession);
      scheduleLayoutRefresh();
      return;
    }
    if (payload.type === "pane-scroll") {
      activePaneLocalScroll = payload.local === true;
      return;
    }
    if (payload.type === "notice") {
      showToast(payload.message);
      return;
    }
    if (payload.type === "image-uploaded") {
      const path = payload.path || "";
      if (path) {
        // Server saved the pasted screenshot and handed back its absolute path.
        // Drop the path into the prompt so the claude CLI reads the file; the
        // user adds any words and sends.
        if (mobileComposerMode) {
          insertComposerText(path + " ");
        } else {
          sendMessage({ type: "input", data: path + " " });
        }
        showToast("Screenshot added — type a message and send.");
      } else if (payload.error) {
        showToast(payload.error);
      }
      return;
    }
    if (payload.type === "composer-state") {
      if (mobileComposerMode && !isBtopSession(activeSessionName) && !composerInput.disabled) {
        if (
          semanticTrackingActive() &&
          payload.source !== "semantic-osc133" &&
          payload.source !== "composer-sync" &&
          payload.source !== "terminal-buffer"
        ) {
          return;
        }
        const revision = Number.isFinite(Number(payload.revision)) ? Number(payload.revision) : 0;
        if (revision < composerRevision || revision < latestAppliedComposerRevision) {
          return;
        }
        latestAppliedComposerRevision = revision;
        setComposerValue(payload.value || "", payload.cursor);
        deliverPendingPasteToComposer(revision);
      }
      return;
    }
    if (payload.type === "sessions") {
      updateSessionInventory(payload.sessions || [], payload.activeSession || activeSessionName);
      if (sessionMenuOpen) {
        positionSessionMenu();
      }
      return;
    }
    if (payload.type === "btop-targets") {
      if (Array.isArray(payload.targets) && payload.targets.length) {
        btopTargets = payload.targets;
      }
      if (btopTargetMenuOpen) {
        renderBtopTargetMenu();
        positionBtopTargetMenu();
      }
      return;
    }
    if (payload.type === "stats") {
      renderUsage(payload);
      return;
    }
    if (payload.type === "settings") {
      const nextSettings = payload.settings || {};
      hostAuthenticationDefault = normalizeAuthenticationSettings(nextSettings.authentication);
      authenticationByRealm = Object.fromEntries(
        Object.entries(
          nextSettings.authenticationByRealm && typeof nextSettings.authenticationByRealm === "object"
            ? nextSettings.authenticationByRealm
            : {},
        ).map(([realm, settings]) => [realm, normalizeAuthenticationSettings(settings)]),
      );
      applyAuthenticationScope();
      const hostPersisted = payload.persisted === true;
      if (!hostPersisted && !hostSettingsReady && hasLocalSettingsOverride()) {
        hostSettingsReady = true;
        saveHostSettings();
        return;
      }
      const nextShortcuts = Array.isArray(nextSettings.shortcuts)
        ? nextSettings.shortcuts.map(normalizeShortcut).filter(Boolean)
        : null;
      if (nextShortcuts && nextShortcuts.length) {
        shortcuts = nextShortcuts;
        localStorage.setItem(STORAGE_SHORTCUTS_KEY, JSON.stringify(shortcuts));
        renderShortcutBar();
      }
      if (nextSettings.gestures && typeof nextSettings.gestures === "object") {
        gestureBindings = normalizeGestureBindings(nextSettings.gestures);
        localStorage.setItem(STORAGE_GESTURES_KEY, JSON.stringify(gestureBindings));
      }
      // Display zoom is per-device ("this device wins"). Only adopt the host's
      // values when this device has never chosen its own — the host acts as the
      // default for a fresh device. Once the user sets zoom locally (slider or
      // pinch), that local override is authoritative, so a focus/reconnect
      // settings push must not snap it back. Deliberately do NOT persist the
      // adopted host value into localStorage here: that would freeze this device
      // onto the host default and defeat the "no local override -> track host"
      // behaviour (and was the source of the zoom-out-on-reconnect bug).
      if (localStorage.getItem(STORAGE_UI_SCALE_KEY) === null && Number.isFinite(Number(nextSettings.uiScale))) {
        applyUiScale(Number(nextSettings.uiScale), false);
      }
      if (
        localStorage.getItem(STORAGE_TERMINAL_FONT_KEY) === null &&
        Number.isFinite(Number(nextSettings.terminalFontSize))
      ) {
        applyTerminalFontSize(Number(nextSettings.terminalFontSize), false);
      }
      if (serverConfig.profileMode && activeProfileId) {
        fileBookmarksByProfile = normalizeFileBookmarksByProfile(nextSettings.fileBookmarksByProfile);
        if (
          !Object.prototype.hasOwnProperty.call(fileBookmarksByProfile, activeProfileId) &&
          activeProfileId === serverConfig.activeProfile &&
          Array.isArray(nextSettings.fileBookmarks)
        ) {
          fileBookmarksByProfile[activeProfileId] = normalizeFileBookmarks(nextSettings.fileBookmarks);
        }
        fileBookmarks = normalizeFileBookmarks(fileBookmarksByProfile[activeProfileId]);
      } else {
        fileBookmarks = normalizeFileBookmarks(nextSettings.fileBookmarks);
      }
      if (!fileRootOverlay.classList.contains("hidden")) {
        renderFileBookmarks();
      }
      hostSettingsReady = true;
      updateDisplayDraft(uiScale, terminalFontSize);
      scheduleLayoutRefresh();
      return;
    }
    if (payload.type === "session-created") {
      addOpenTab(payload.session || "");
      switchSession(payload.session || "");
      return;
    }
    if (payload.type === "session-renamed") {
      const previousName = payload.oldSession || "";
      const nextName = payload.session || previousName;
      replaceOpenTabName(previousName, nextName);
      currentSessions = currentSessions.map((session) =>
        session.name === previousName ? { ...session, name: nextName } : session,
      );
      if (activeSessionName === previousName) {
        activeSessionName = nextName;
      }
      if (activeTabKey === terminalTabKey(previousName)) {
        activeTabKey = terminalTabKey(nextName);
      }
      if (selectedSessionName === previousName || activeSessionName === nextName) {
        selectedSessionName = nextName;
        persistActiveSession(nextName);
      }
      closeTabMenu();
      syncOpenTabsToSessions();
      return;
    }
    if (payload.type === "session-closing") {
      removeOpenTab(payload.closedSession || "");
      const nextSession = payload.nextSession || "";
      if (nextSession) {
        selectedSessionName = nextSession;
        if (activeTabKey === terminalTabKey(payload.closedSession || "")) {
          activeTabKey = terminalTabKey(nextSession);
        }
        persistActiveSession(nextSession);
        reconnectForSessionSwitch = !serverConfig.profileMode;
      }
      return;
    }
    if (payload.type === "auth-error") {
      waitingForProxyAuth = false;
      if (passkeyRequiredScope) {
        passkeyRetryPending = true;
        setPasskeyRetryUi(true);
        loginOverlay.classList.remove("hidden");
        loginMessage.textContent = payload.message || "Passkey authentication is required.";
        try {
          socket?.close(4000, "passkey retry");
        } catch (_closeError) {
          // Ignore a socket already closing after the authentication error.
        }
        return;
      }
      setPasskeyRetryUi(false);
      if (serverConfig.profileMode && payload.profile) {
        pendingProfileId = payload.profile;
        loginRealm = payload.realm || activeProfile()?.authRealm || "";
        loginOverlay.classList.remove("hidden");
        tokenInput.value = "";
        tokenInput.focus();
      }
      loginMessage.textContent = payload.message || "Authentication failed.";
      return;
    }
    if (payload.type === "devices") {
      renderDeviceList(payload.devices || []);
      return;
    }
    if (payload.type === "token-rotated") {
      if (payload.token) {
        localStorage.setItem(tokenStorageKey(), payload.token);
      }
      showToast("Token rotated. Other devices have been signed out.");
      sendMessage({ type: "request-devices" });
      return;
    }
  }

  function toggleTabMenu(tabKey) {
    closeSessionMenu();
    closeSettingsMenu();
    closeAuxMenu();
    closeBtopTargetMenu();
    closeProfileMenu();
    if (openTabMenuKey === tabKey) {
      closeTabMenu();
      return;
    }
    openTabMenuKey = tabKey;
    const tab = currentTabs.find((item) => item.key === openTabMenuKey);
    const terminalTab = !tab || tab.type !== "editor";
    document.getElementById("renameTabButton").textContent = terminalTab ? "Rename Tab" : "Rename Tab";
    document.getElementById("detachOthersButton").classList.toggle("hidden", !terminalTab);
    document.getElementById("killSessionButton").classList.toggle("hidden", !terminalTab);
    tabMenu.classList.remove("hidden");
    positionTabMenu();
  }

  function positionTabMenu() {
    if (!openTabMenuKey) {
      return;
    }
    const button = tabsStrip.querySelector(`[data-tab-key="${CSS.escape(openTabMenuKey)}"]`);
    if (!button) {
      closeTabMenu();
      return;
    }
    const rect = button.getBoundingClientRect();
    const menuWidth = Math.max(168, tabMenu.offsetWidth || 168);
    const left = Math.min(
      window.innerWidth - menuWidth - 12,
      Math.max(12, rect.left),
    );
    tabMenu.style.left = `${left}px`;
    tabMenu.style.top = `${rect.bottom + 8}px`;
  }

  function closeTabMenu() {
    openTabMenuKey = null;
    tabMenu.classList.add("hidden");
  }

  function toggleSessionMenu() {
    closeTabMenu();
    closeSettingsMenu();
    closeAuxMenu();
    closeBtopTargetMenu();
    closeProfileMenu();
    sessionMenuOpen = !sessionMenuOpen;
    sessionMenu.classList.toggle("hidden", !sessionMenuOpen);
    if (!sessionMenuOpen) {
      return;
    }
    renderSessionMenu();
    positionSessionMenu();
    if (socket && socket.readyState === WebSocket.OPEN) {
      sendMessage({ type: "request-sessions" });
    }
  }

  function positionSessionMenu() {
    if (!sessionMenuOpen) {
      return;
    }
    const rect = auxButton.getBoundingClientRect();
    const menuWidth = Math.max(196, sessionMenu.offsetWidth || 196);
    const left = Math.min(
      window.innerWidth - menuWidth - 12,
      Math.max(12, rect.left),
    );
    sessionMenu.style.left = `${left}px`;
    sessionMenu.style.top = `${rect.bottom + 8}px`;
  }

  function closeSessionMenu() {
    sessionMenuOpen = false;
    sessionMenu.classList.add("hidden");
  }

  function toggleProfileMenu() {
    closeSessionMenu();
    closeTabMenu();
    closeSettingsMenu();
    closeAuxMenu();
    closeBtopTargetMenu();
    profileMenuOpen = !profileMenuOpen;
    profileMenu.classList.toggle("hidden", !profileMenuOpen);
    profileButton.setAttribute("aria-expanded", profileMenuOpen ? "true" : "false");
    if (profileMenuOpen) {
      renderProfileMenu();
      positionProfileMenu();
      sendMessage({ type: "request-profiles" });
    }
  }

  function positionProfileMenu() {
    if (!profileMenuOpen) {
      return;
    }
    positionMenuUnder(profileMenu, profileButton, 210);
  }

  function closeProfileMenu() {
    profileMenuOpen = false;
    profileMenu.classList.add("hidden");
    profileButton.setAttribute("aria-expanded", "false");
  }

  function toggleSettingsMenu() {
    closeSessionMenu();
    closeTabMenu();
    closeAuxMenu();
    closeBtopTargetMenu();
    closeProfileMenu();
    settingsMenuOpen = !settingsMenuOpen;
    settingsMenu.classList.toggle("hidden", !settingsMenuOpen);
    if (settingsMenuOpen) {
      positionSettingsMenu();
    }
  }

  function positionSettingsMenu() {
    if (!settingsMenuOpen) {
      return;
    }
    const rect = settingsButton.getBoundingClientRect();
    const menuWidth = Math.max(180, settingsMenu.offsetWidth || 180);
    const left = Math.min(
      window.innerWidth - menuWidth - 12,
      Math.max(12, rect.right - menuWidth),
    );
    settingsMenu.style.left = `${left}px`;
    settingsMenu.style.top = `${rect.bottom + 8}px`;
  }

  function closeSettingsMenu() {
    settingsMenuOpen = false;
    settingsMenu.classList.add("hidden");
  }

  function toggleAuxMenu() {
    closeSessionMenu();
    closeSettingsMenu();
    closeTabMenu();
    closeBtopTargetMenu();
    closeProfileMenu();
    auxMenuOpen = !auxMenuOpen;
    auxMenu.classList.toggle("hidden", !auxMenuOpen);
    if (auxMenuOpen) {
      positionAuxMenu();
    }
  }

  function positionAuxMenu() {
    if (!auxMenuOpen) {
      return;
    }
    positionMenuUnder(auxMenu, auxButton, 160);
  }

  function closeAuxMenu() {
    auxMenuOpen = false;
    auxMenu.classList.add("hidden");
  }

  // Shared positioner: place a fixed .tab-menu under a trigger button, kept
  // within the viewport horizontally.
  function positionMenuUnder(menu, button, minWidth) {
    const rect = button.getBoundingClientRect();
    const menuWidth = Math.max(minWidth, menu.offsetWidth || minWidth);
    const left = Math.min(
      window.innerWidth - menuWidth - 12,
      Math.max(12, rect.right - menuWidth),
    );
    menu.style.left = `${left}px`;
    menu.style.top = `${rect.bottom + 8}px`;
  }

  function openBtopTargetMenu() {
    closeAuxMenu();
    closeSessionMenu();
    closeSettingsMenu();
    closeTabMenu();
    closeProfileMenu();
    btopTargetMenuOpen = true;
    renderBtopTargetMenu();
    btopTargetMenu.classList.remove("hidden");
    positionBtopTargetMenu();
    requestBtopTargets();
    // Keep the list live while open: the server re-pings hosts every 30s, so a
    // host that comes online shows up without reopening the menu.
    window.clearInterval(btopTargetRefreshTimer);
    btopTargetRefreshTimer = window.setInterval(() => {
      if (btopTargetMenuOpen) {
        requestBtopTargets();
      }
    }, 12000);
  }

  function requestBtopTargets() {
    if (socket && socket.readyState === WebSocket.OPEN) {
      sendMessage({ type: "request-btop-targets" });
    }
  }

  function renderBtopTargetMenu() {
    btopTargetMenu.innerHTML = "";
    const heading = document.createElement("div");
    heading.className = "menu-empty menu-heading";
    heading.textContent = "Open btop on…";
    btopTargetMenu.appendChild(heading);
    btopTargets.forEach((target) => {
      const button = document.createElement("button");
      button.className = "tab-menu-button";
      button.type = "button";
      button.textContent = target.label || target.id;
      button.addEventListener("click", () => launchBtop(target.id));
      btopTargetMenu.appendChild(button);
    });
    if (btopTargets.length <= 1) {
      const note = document.createElement("div");
      note.className = "menu-empty";
      note.textContent = "No reachable remote hosts";
      btopTargetMenu.appendChild(note);
    }
  }

  function positionBtopTargetMenu() {
    if (!btopTargetMenuOpen) {
      return;
    }
    positionMenuUnder(btopTargetMenu, auxButton, 200);
  }

  function closeBtopTargetMenu() {
    btopTargetMenuOpen = false;
    window.clearInterval(btopTargetRefreshTimer);
    btopTargetRefreshTimer = null;
    btopTargetMenu.classList.add("hidden");
  }

  function launchBtop(target) {
    closeBtopTargetMenu();
    if (!target) {
      return;
    }
    resetComposerTracking(true);
    sendMessage({ type: "new-btop-tab", target });
  }

  function isBtopSession(name) {
    return typeof name === "string" && name.startsWith(BTOP_SESSION_PREFIX);
  }

  function btopTargetFromSession(name) {
    return isBtopSession(name) ? name.slice(BTOP_SESSION_PREFIX.length) : "";
  }

  function btopTabLabel(name) {
    const target = btopTargetFromSession(name);
    return target === "local" ? "btop: local" : `btop: ${target}`;
  }

  function switchSession(sessionName) {
    if (!sessionName) {
      closeSessionMenu();
      return;
    }
    if (sessionName === activeSessionName) {
      activeTabKey = terminalTabKey(sessionName);
      closeSessionMenu();
      closeTabMenu();
      syncOpenTabsToSessions();
      focusTerminal();
      return;
    }
    // Remember the tab we're leaving so the selection "To tab" chip can target
    // the most recent *other* tab.
    previousSessionName = selectedSessionName || activeSessionName;
    clearTerminalSelectionUI();
    addOpenTab(sessionName);
    activeTabKey = terminalTabKey(sessionName);
    selectedSessionName = sessionName;
    persistActiveSession(sessionName);
    closeSessionMenu();
    closeTabMenu();
    syncOpenTabsToSessions();
    followOutput = true;
    resetSpeechInputState();
    // Explicitly drop composer focus so iOS actually closes the keyboard and
    // fires a real visualViewport resize event before the new session loads.
    // iOS sometimes ignores programmatic blur or doesn't dispatch the resize,
    // so also force the layout variables to keyboard-closed state. The next
    // real visualViewport event will overwrite this if the user reopens it.
    if (mobileComposerMode) {
      if (document.activeElement === composerInput) {
        composerInput.blur();
      }
      setComposerActive(false);
      // Hiding the panel removes the textarea from layout, forcing iOS to
      // drop the keyboard even if blur() alone was ignored. The next
      // openComposer() during focusTerminal() will re-show it.
      composerPanel.classList.add("hidden");
      document.documentElement.style.setProperty("--app-top", "0px");
      document.documentElement.style.setProperty("--app-height", `${window.innerHeight}px`);
      document.documentElement.style.setProperty("--keyboard-inset", "0px");
      document.body.dataset.keyboardOpen = "false";
      // Run measure + fit synchronously so the very first paint after the
      // tap renders the new layout. The default scheduleLayoutRefresh path
      // is debounced 40 ms, which is long enough to flash the gap.
      performLayoutNow({ preserveTerminalCols: false });
    }
    resetComposerTracking(true);
    snapshotActiveSession(); // cache the tab we're leaving, before we clear it
    term.reset();
    followOutput = true;
    // Keep the view pinned to the bottom until the re-attach settles, so the
    // snapshot restore + live redraw can't strand it scrolled up.
    bottomPinUntil = performance.now() + 1500;
    // Fast path: switch on the live connection (no WS reconnect/handshake).
    // A cached snapshot is paint-only. Finish that write before asking the server
    // to switch, then the target's authoritative seed replaces it.
    if (socket && socket.readyState === WebSocket.OPEN) {
      selectedSessionName = sessionName;
      terminalAuthoritative = false;
      const requestSwitch = () => sendMessage({ type: "switch-session", session: sessionName });
      const snapshot = sessionSnapshots.get(sessionSnapshotKey(sessionName));
      if (snapshot) {
        term.write(snapshot, () => {
          term.scrollToBottom();
          requestSwitch();
        });
      } else {
        requestSwitch();
      }
      return;
    }
    // No live socket: fall back to a fresh authoritative attach.
    if (socket && socket.readyState === WebSocket.CONNECTING) {
      reconnectForSessionSwitch = true;
      socket.close(1000, "switch-session");
      return;
    }
    connect();
  }

  function moveEditorRow(row, direction) {
    const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling || !row.parentElement) {
      return;
    }
    if (direction < 0) {
      row.parentElement.insertBefore(row, sibling);
      return;
    }
    row.parentElement.insertBefore(sibling, row);
  }

  function buildEditorRow(shortcut = { label: "", sequence: "", visible: true }) {
    const row = document.createElement("div");
    row.className = "shortcut-editor-row";

    const fields = document.createElement("div");
    fields.className = "shortcut-editor-fields";

    const labelField = document.createElement("label");
    labelField.className = "shortcut-field";
    const labelCaption = document.createElement("span");
    labelCaption.className = "shortcut-field-caption";
    labelCaption.textContent = "Label";
    const labelInput = document.createElement("input");
    labelInput.className = "text-input shortcut-label-input";
    labelInput.placeholder = "Esc";
    labelInput.value = shortcut.label || "";
    labelInput.autocapitalize = "off";
    labelInput.autocomplete = "off";
    labelField.appendChild(labelCaption);
    labelField.appendChild(labelInput);

    const sequenceField = document.createElement("label");
    sequenceField.className = "shortcut-field";
    const sequenceCaption = document.createElement("span");
    sequenceCaption.className = "shortcut-field-caption";
    sequenceCaption.textContent = "Sequence";
    const sequenceInput = document.createElement("input");
    sequenceInput.className = "text-input shortcut-sequence-input";
    sequenceInput.placeholder = "{CTRL+C}";
    sequenceInput.value = shortcut.sequence || "";
    sequenceInput.autocapitalize = "off";
    sequenceInput.autocomplete = "off";
    sequenceInput.spellcheck = false;
    sequenceField.appendChild(sequenceCaption);
    sequenceField.appendChild(sequenceInput);

    fields.appendChild(labelField);
    fields.appendChild(sequenceField);

    const controls = document.createElement("div");
    controls.className = "shortcut-editor-controls";

    const visibilityLabel = document.createElement("label");
    visibilityLabel.className = "shortcut-visibility-toggle";

    const visibilityInput = document.createElement("input");
    visibilityInput.type = "checkbox";
    visibilityInput.className = "shortcut-visibility-input";
    visibilityInput.checked = shortcut.visible !== false;

    const visibilityText = document.createElement("span");
    visibilityText.textContent = "Show in bar";

    visibilityLabel.appendChild(visibilityInput);
    visibilityLabel.appendChild(visibilityText);

    const buttonGroup = document.createElement("div");
    buttonGroup.className = "shortcut-editor-buttons";

    const moveUpButton = document.createElement("button");
    moveUpButton.type = "button";
    moveUpButton.className = "ghost-button shortcut-order-button";
    moveUpButton.textContent = "↑";
    moveUpButton.setAttribute("aria-label", "Move up");
    moveUpButton.addEventListener("click", () => moveEditorRow(row, -1));

    const moveDownButton = document.createElement("button");
    moveDownButton.type = "button";
    moveDownButton.className = "ghost-button shortcut-order-button";
    moveDownButton.textContent = "↓";
    moveDownButton.setAttribute("aria-label", "Move down");
    moveDownButton.addEventListener("click", () => moveEditorRow(row, 1));

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "ghost-button shortcut-remove-button";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => row.remove());

    buttonGroup.appendChild(moveUpButton);
    buttonGroup.appendChild(moveDownButton);
    buttonGroup.appendChild(removeButton);

    controls.appendChild(visibilityLabel);
    controls.appendChild(buttonGroup);

    row.appendChild(fields);
    row.appendChild(controls);
    return row;
  }

  function openEditor() {
    closeSettingsMenu();
    shortcutEditorList.innerHTML = "";
    shortcuts.forEach((shortcut) => shortcutEditorList.appendChild(buildEditorRow(shortcut)));
    editorOverlay.classList.remove("hidden");
    syncOverlayToViewport();
  }

  function closeEditor() {
    editorOverlay.classList.add("hidden");
  }

  function collectEditorShortcuts() {
    return Array.from(shortcutEditorList.children)
      .map((row) => {
        const labelInput = row.querySelector(".shortcut-label-input");
        const sequenceInput = row.querySelector(".shortcut-sequence-input");
        const visibilityInput = row.querySelector(".shortcut-visibility-input");
        return {
          label: labelInput?.value.trim() || "",
          sequence: sequenceInput?.value.trim() || "",
          visible: visibilityInput ? visibilityInput.checked : true,
        };
      })
      .filter((shortcut) => shortcut.label && shortcut.sequence);
  }

  function buildGestureRow(def) {
    const binding = gestureBindings[def.id] || { sequence: def.default, enabled: true };
    const row = document.createElement("div");
    row.className = "gesture-editor-row";
    row.dataset.gestureId = def.id;

    const enableLabel = document.createElement("label");
    enableLabel.className = "gesture-enable-toggle";
    const enableInput = document.createElement("input");
    enableInput.type = "checkbox";
    enableInput.className = "gesture-enable-input";
    enableInput.checked = binding.enabled !== false;
    const nameText = document.createElement("span");
    nameText.className = "gesture-name";
    nameText.textContent = def.label;
    enableLabel.appendChild(enableInput);
    enableLabel.appendChild(nameText);

    const sequenceInput = document.createElement("input");
    sequenceInput.className = "text-input gesture-sequence-input";
    sequenceInput.placeholder = "unbound";
    sequenceInput.value = binding.sequence || "";
    sequenceInput.autocapitalize = "off";
    sequenceInput.autocomplete = "off";
    sequenceInput.spellcheck = false;

    row.appendChild(enableLabel);
    row.appendChild(sequenceInput);
    return row;
  }

  function openGestureEditor() {
    closeSettingsMenu();
    gestureEditorList.innerHTML = "";
    let currentGroup = null;
    GESTURE_DEFS.forEach((def) => {
      if (def.group !== currentGroup) {
        currentGroup = def.group;
        const heading = document.createElement("div");
        heading.className = "gesture-group-heading";
        heading.textContent = def.group;
        gestureEditorList.appendChild(heading);
      }
      gestureEditorList.appendChild(buildGestureRow(def));
    });
    gestureOverlay.classList.remove("hidden");
    syncOverlayToViewport();
  }

  function closeGestureEditor() {
    gestureOverlay.classList.add("hidden");
  }

  function collectGestureBindings() {
    const next = {};
    Array.from(gestureEditorList.querySelectorAll(".gesture-editor-row")).forEach((row) => {
      const id = row.dataset.gestureId;
      if (!id) {
        return;
      }
      const sequenceInput = row.querySelector(".gesture-sequence-input");
      const enableInput = row.querySelector(".gesture-enable-input");
      next[id] = {
        sequence: sequenceInput ? sequenceInput.value.trim() : "",
        enabled: enableInput ? enableInput.checked : true,
      };
    });
    return next;
  }

  function syncDisplayControls(nextScale, nextSize) {
    uiScaleInput.value = nextScale.toFixed(2);
    uiScaleValue.textContent = `${Math.round(nextScale * 100)}%`;
    terminalFontInput.value = String(nextSize);
    terminalFontValue.textContent = `${nextSize}px`;
  }

  function renderDisplayPreview(nextScale, nextSize) {
    if (displayUiPreview) {
      displayUiPreview.style.setProperty("--preview-ui-scale", String(nextScale));
    }
    if (displayTerminalPreview) {
      displayTerminalPreview.style.setProperty("--preview-terminal-font-size", `${nextSize}px`);
      displayTerminalPreview.style.fontSize = `${nextSize}px`;
    }
    if (displayPreview) {
      displayPreview.style.setProperty("--preview-terminal-font-size", `${nextSize}px`);
    }
  }

  function updateDisplayDraft(nextScale = draftUiScale, nextSize = draftTerminalFontSize) {
    draftUiScale = Math.min(1.4, Math.max(0.5, nextScale));
    draftTerminalFontSize = Math.min(24, Math.max(5, nextSize));
    syncDisplayControls(draftUiScale, draftTerminalFontSize);
    renderDisplayPreview(draftUiScale, draftTerminalFontSize);
  }

  function applyUiScale(nextScale, persist = true) {
    uiScale = Math.min(1.4, Math.max(0.5, nextScale));
    const viewport = window.visualViewport;
    const viewportWidth = viewport ? viewport.width : window.innerWidth;
    const viewportHeight = viewport ? viewport.height : window.innerHeight;
    const offsetTop = viewport ? viewport.offsetTop : 0;
    const layoutHeight = window.innerHeight;
    const rawKeyboardInset = Math.max(0, layoutHeight - (viewportHeight + offsetTop));
    const keyboardInset = rawKeyboardInset > KEYBOARD_THRESHOLD ? rawKeyboardInset : 0;
    applyEffectiveUiScale(viewportWidth, viewportHeight, keyboardInset, layoutHeight);
    if (!displayOverlay.classList.contains("hidden")) {
      syncDisplayControls(draftUiScale, draftTerminalFontSize);
    }
    if (persist) {
      localStorage.setItem(STORAGE_UI_SCALE_KEY, String(uiScale));
      // Write through to the host so the stored default tracks the latest choice
      // (and so the next focus/reconnect push carries this value, not a stale one).
      saveHostSettings();
    }
    scheduleLayoutRefresh();
  }

  function applyTerminalFontSize(nextSize, persist = true) {
    terminalFontSize = Math.min(24, Math.max(5, nextSize));
    term.options.fontSize = terminalFontSize;
    document.documentElement.style.setProperty("--terminal-font-size", `${terminalFontSize}px`);
    if (!displayOverlay.classList.contains("hidden")) {
      syncDisplayControls(draftUiScale, draftTerminalFontSize);
    }
    if (persist) {
      localStorage.setItem(STORAGE_TERMINAL_FONT_KEY, String(terminalFontSize));
      // Write through to the host (see applyUiScale) so pinch/{FONT+}/{FONT-}
      // changes survive a reconnect instead of being reverted to a stale value.
      saveHostSettings();
    }
    scheduleLayoutRefresh();
  }

  // How many cells the pane would give at a candidate font size right now.
  // Measures the real cell size (proposeDimensions caches stale metrics after a
  // font change, so force a fresh char measure first).
  function btopGridAtFont(size) {
    term.options.fontSize = size;
    try {
      term._core?._charSizeService?.measure?.();
    } catch (_error) {
      // Internal API — ignore if the shape changes.
    }
    if (typeof fitAddon.proposeDimensions !== "function") {
      return { cols: 0, rows: 0 };
    }
    const proposed = fitAddon.proposeDimensions();
    if (!proposed || !Number.isFinite(proposed.rows) || !Number.isFinite(proposed.cols)) {
      return { cols: 0, rows: 0 };
    }
    // Match fitTerminal's guard so the value reflects what btop actually gets.
    return { cols: Math.floor(proposed.cols) - TERMINAL_COL_GUARD, rows: Math.floor(proposed.rows) };
  }

  function applyBtopFont() {
    // Make sure the reserve for the btop control bar is applied before we read
    // the pane height, otherwise the grid overshoots and btop says "too small".
    measureShortcutHeight();
    if (terminalElement.clientWidth <= 0 || terminalElement.clientHeight <= 0) {
      return;
    }
    const targetCols = BTOP_TARGET_COLS * btopZoomFactor;
    const targetRows = BTOP_TARGET_ROWS * btopZoomFactor;
    // Binary-search the largest 0.5px font whose real grid still clears btop's
    // minimum (smaller font -> more cells, so the predicate is monotonic). This
    // guarantees it renders regardless of layout/measurement timing quirks.
    let loHalf = Math.round(BTOP_MIN_FONT * 2);
    let hiHalf = 48; // 24px
    let bestHalf = loHalf;
    while (loHalf <= hiHalf) {
      const midHalf = Math.floor((loHalf + hiHalf) / 2);
      const grid = btopGridAtFont(midHalf / 2);
      if (grid.cols >= targetCols && grid.rows >= targetRows) {
        bestHalf = midHalf;
        loHalf = midHalf + 1;
      } else {
        hiHalf = midHalf - 1;
      }
    }
    const size = bestHalf / 2;
    // Set xterm font directly (bypassing applyTerminalFontSize's 5px floor and
    // its global persist) so the btop tab scales independently of the UI.
    term.options.fontSize = size;
    document.documentElement.style.setProperty("--terminal-font-size", `${size}px`);
    fitTerminal({ preserveCols: false });
  }

  function enterBtopMode() {
    const firstEnter = !btopMode;
    btopMode = true;
    document.body.dataset.btop = "true";
    shortcutBar.classList.add("hidden");
    btopControls.classList.remove("hidden");
    btopZoom.classList.remove("hidden");
    btopZoomInput.value = String(btopZoomFactor);
    // No prompt/composer and no auto-keyboard in a btop tab.
    if (composerPanel) {
      setComposerActive(false);
      composerPanel.classList.add("hidden");
    }
    if (document.activeElement === composerInput) {
      composerInput.blur();
    }
    if (firstEnter) {
      // Give layout a frame to settle before measuring the pane.
      window.requestAnimationFrame(applyBtopFont);
    } else {
      applyBtopFont();
    }
  }

  function exitBtopMode() {
    if (!btopMode) {
      return;
    }
    btopMode = false;
    delete document.body.dataset.btop;
    btopControls.classList.add("hidden");
    btopZoom.classList.add("hidden");
    shortcutBar.classList.remove("hidden");
    // Restore the normal (global) terminal font size for other tabs.
    term.options.fontSize = terminalFontSize;
    document.documentElement.style.setProperty("--terminal-font-size", `${terminalFontSize}px`);
    fitTerminal({ preserveCols: false });
  }

  function setBtopZoom(nextFactor, persist = true) {
    btopZoomFactor = Math.min(3, Math.max(1, Number(nextFactor) || 1));
    if (persist) {
      localStorage.setItem(STORAGE_BTOP_ZOOM_KEY, String(btopZoomFactor));
    }
    if (btopMode) {
      applyBtopFont();
    }
  }

  function openDisplay() {
    closeSettingsMenu();
    updateDisplayDraft(uiScale, terminalFontSize);
    displayOverlay.classList.remove("hidden");
  }

  function closeDisplay(applyChanges = false) {
    if (applyChanges) {
      applyUiScale(draftUiScale);
      applyTerminalFontSize(draftTerminalFontSize);
      saveHostSettings();
    } else {
      updateDisplayDraft(uiScale, terminalFontSize);
    }
    displayOverlay.classList.add("hidden");
  }


  function openAuthentication() {
    closeSettingsMenu();
    applyAuthenticationScope();
    updateAuthenticationDraft(authenticationSettings, loginRealm);
    authenticationOverlay.classList.remove("hidden");
  }

  function saveAuthentication() {
    const realm = draftAuthenticationRealm;
    const scope = draftAuthenticationScope;
    if (scope !== authenticationScope(realm) || scope !== authenticationScope()) {
      authenticationOverlay.classList.add("hidden");
      applyAuthenticationScope();
      return;
    }
    updateAuthenticationDraft(
      {
        mode: authenticationModeInput.value,
        idleMinutes: authenticationIdleInput.value,
      },
      realm,
    );
    localStorage.setItem(
      authenticationStorageKey(STORAGE_PASSKEY_AUTH_MODE_KEY, realm),
      draftAuthenticationSettings.mode,
    );
    localStorage.setItem(
      authenticationStorageKey(STORAGE_PASSKEY_IDLE_MINUTES_KEY, realm),
      String(draftAuthenticationSettings.idleMinutes),
    );
    if (draftAuthenticationSettings.mode === "idle") {
      writeBackgroundedAt(realm, Date.now());
    }
    applyAuthenticationScope(realm);
    saveHostSettings(realm);
    authenticationOverlay.classList.add("hidden");
    if (authenticationSettings.mode === "off" && passkeyRequiredScope) {
      passkeyRequiredScope = "";
      passkeyRetryPending = false;
      setPasskeyLocked(false);
      reconnectSocket();
    }
  }

  function openUsage() {
    closeSettingsMenu();
    usageOverlay.classList.remove("hidden");
    renderUsageLoading();
    requestUsage();
  }

  function closeUsage() {
    usageOverlay.classList.add("hidden");
    if (usageRequestTimer) {
      clearTimeout(usageRequestTimer);
      usageRequestTimer = null;
    }
  }

  function requestUsage() {
    if (usageRequestTimer) clearTimeout(usageRequestTimer);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      showUsageError("Not connected to the server. Reconnect and try again.");
      return;
    }
    sendMessage({ type: "request-stats" });
    usageRequestTimer = window.setTimeout(() => {
      usageRequestTimer = null;
      if (!lastUsagePayload) {
        showUsageError(
          "No response from the server. The mobile-terminal service may be running an older version — restart it with: systemctl --user restart mobile-terminal",
        );
      }
    }, 4000);
  }

  function showUsageError(message) {
    usageMetaLabel.textContent = message;
    usageMetaLabel.classList.add("usage-error");
    usageStats.innerHTML = "";
    usageDailyChart.innerHTML = "";
    usageHourChart.innerHTML = "";
    usageHourNote.textContent = "";
    usageBreakdown.innerHTML = "";
    usageEmpty.classList.add("hidden");
    usageDailyEmpty.classList.add("hidden");
  }

  function renderUsageLoading() {
    usageMetaLabel.textContent = "Loading…";
    usageMetaLabel.classList.remove("usage-error");
    usageStats.innerHTML = "";
    usageDailyChart.innerHTML = "";
    usageHourChart.innerHTML = "";
    usageHourNote.textContent = "";
    usageBreakdown.innerHTML = "";
    usageEmpty.classList.add("hidden");
    usageDailyEmpty.classList.add("hidden");
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    if (value < 60) return `${value}s`;
    const minutes = Math.floor(value / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const remMinutes = minutes % 60;
    if (hours < 24) return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return remHours ? `${days}d ${remHours}h` : `${days}d`;
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString();
  }

  function emptyUsageBucket() {
    return {
      sessions: 0,
      durationSeconds: 0,
      inputEvents: 0,
      commandsRun: 0,
      bytesIn: 0,
      bytesOut: 0,
    };
  }

  function sumBuckets(buckets) {
    const total = emptyUsageBucket();
    for (const bucket of buckets) {
      if (!bucket) continue;
      for (const key of Object.keys(total)) {
        total[key] += Number(bucket[key]) || 0;
      }
    }
    return total;
  }

  function rangeWindowDays(range) {
    if (range === "7d") return 7;
    if (range === "30d") return 30;
    if (range === "90d") return 90;
    return null;
  }

  function rangeLabel(range) {
    if (range === "7d") return "last 7 days";
    if (range === "30d") return "last 30 days";
    if (range === "90d") return "last 90 days";
    return "all time";
  }

  function toIsoDate(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  }

  function startOfIsoWeek(dateStr) {
    const d = new Date(`${dateStr}T00:00:00`);
    const day = d.getDay();
    const diff = (day + 6) % 7;
    d.setDate(d.getDate() - diff);
    d.setHours(0, 0, 0, 0);
    return d;
  }

  function formatWeekLabel(monday) {
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    const fmt = (d) => `${d.toLocaleString(undefined, { month: "short" })} ${d.getDate()}`;
    return `${fmt(monday)} – ${fmt(sunday)}`;
  }

  function rangeBoundariesIso(payload) {
    const todayIso = payload.today || new Date().toISOString().slice(0, 10);
    const window = rangeWindowDays(usageRange);
    if (window == null) return { startIso: "0000-00-00", endIso: todayIso, windowDays: null, todayIso };
    const start = new Date(`${todayIso}T00:00:00`);
    start.setDate(start.getDate() - (window - 1));
    return { startIso: toIsoDate(start), endIso: todayIso, windowDays: window, todayIso };
  }

  function dayKeysInRange(days, payload) {
    const { startIso, endIso } = rangeBoundariesIso(payload);
    const keys = Object.keys(days).filter((k) => k >= startIso && k <= endIso);
    keys.sort();
    return keys;
  }

  function enumerateDaysAsc(payload) {
    const { startIso, endIso, windowDays, todayIso } = rangeBoundariesIso(payload);
    const result = [];
    if (windowDays == null) {
      const days = lastUsagePayload && lastUsagePayload.usage ? lastUsagePayload.usage.days || {} : {};
      const presentKeys = Object.keys(days).sort();
      if (presentKeys.length === 0) return [todayIso];
      const start = new Date(`${presentKeys[0]}T00:00:00`);
      const end = new Date(`${endIso}T00:00:00`);
      const cap = 400;
      const cursor = new Date(start);
      for (let i = 0; i < cap && cursor <= end; i += 1) {
        result.push(toIsoDate(cursor));
        cursor.setDate(cursor.getDate() + 1);
      }
      return result;
    }
    const cursor = new Date(`${startIso}T00:00:00`);
    for (let i = 0; i < windowDays; i += 1) {
      result.push(toIsoDate(cursor));
      cursor.setDate(cursor.getDate() + 1);
    }
    return result;
  }

  function aggregateForRange(payload) {
    const days = payload.usage.days || {};
    const keys = dayKeysInRange(days, payload);
    return sumBuckets(keys.map((k) => days[k]));
  }

  function renderUsage(payload) {
    if (usageRequestTimer) {
      clearTimeout(usageRequestTimer);
      usageRequestTimer = null;
    }
    lastUsagePayload = payload;
    usageMetaLabel.classList.remove("usage-error");
    const usage = payload && payload.usage ? payload.usage : null;
    if (!usage) {
      usageMetaLabel.textContent = "No usage data available.";
      usageStats.innerHTML = "";
      usageDailyChart.innerHTML = "";
      usageHourChart.innerHTML = "";
      usageHourNote.textContent = "";
      usageBreakdown.innerHTML = "";
      usageEmpty.classList.remove("hidden");
      return;
    }

    const activeSessions = Number(payload.activeSessions) || 0;
    const startedAt = payload.serverStartedAt ? new Date(payload.serverStartedAt) : null;
    const retention = Number(payload.retentionDays) || 0;
    const updated = new Date();
    const metaParts = [];
    metaParts.push(`Updated ${updated.toLocaleTimeString()}`);
    metaParts.push(`Active sessions: ${activeSessions}`);
    if (startedAt && !Number.isNaN(startedAt.getTime())) {
      metaParts.push(`Server up since ${startedAt.toLocaleString()}`);
    }
    if (retention) {
      metaParts.push(`${retention}-day retention`);
    }
    usageMetaLabel.textContent = metaParts.join(" · ");

    renderUsageStats(payload);
    renderDailyChart(payload);
    renderHourlyChart(payload);
    renderUsageBreakdown(payload);
  }

  function renderUsageStats(payload) {
    const bucket = aggregateForRange(payload);
    const label = rangeLabel(usageRange);
    const stats = [
      { label: "Sessions", value: formatNumber(bucket.sessions) },
      { label: "Active Time", value: formatDuration(bucket.durationSeconds) },
      { label: "Commands", value: formatNumber(bucket.commandsRun) },
      { label: "Input Events", value: formatNumber(bucket.inputEvents) },
      { label: "Bytes In", value: formatBytes(bucket.bytesIn) },
      { label: "Bytes Out", value: formatBytes(bucket.bytesOut) },
    ];
    usageStats.innerHTML = stats
      .map(
        (s) => `
          <div class="usage-stat-card">
            <div class="label">${s.label}</div>
            <div class="value">${s.value}</div>
            <div class="sub">${label}</div>
          </div>
        `,
      )
      .join("");
  }

  function renderDailyChart(payload) {
    const days = payload.usage.days || {};
    const keys = enumerateDaysAsc(payload);
    usageDailyChartTitle.textContent = `Daily Activity — ${rangeLabel(usageRange)}`;
    usageDailyChart.innerHTML = "";
    if (keys.length === 0) {
      usageDailyEmpty.classList.remove("hidden");
      return;
    }
    const maxDuration = Math.max(
      1,
      ...keys.map((k) => Number((days[k] || {}).durationSeconds) || 0),
    );
    const anyActivity = keys.some((k) => (Number((days[k] || {}).durationSeconds) || 0) > 0);
    if (!anyActivity) {
      usageDailyEmpty.classList.remove("hidden");
      return;
    }
    usageDailyEmpty.classList.add("hidden");

    const step = Math.max(1, Math.ceil(keys.length / 12));
    for (let i = 0; i < keys.length; i += 1) {
      const dayKey = keys[i];
      const bucket = days[dayKey] || emptyUsageBucket();
      const ratio = Math.min(1, (Number(bucket.durationSeconds) || 0) / maxDuration);
      const col = document.createElement("div");
      col.className = "usage-daily-col";
      col.title = `${dayKey} · ${formatDuration(bucket.durationSeconds)} · ${formatNumber(bucket.sessions)} sessions · ${formatNumber(bucket.commandsRun)} commands`;
      const bar = document.createElement("span");
      bar.className = "usage-daily-bar";
      bar.style.height = `${(ratio * 100).toFixed(1)}%`;
      col.appendChild(bar);
      if (i % step === 0 || i === keys.length - 1) {
        const tick = document.createElement("div");
        tick.className = "usage-daily-tick";
        tick.textContent = dayKey.slice(5);
        col.appendChild(tick);
      }
      usageDailyChart.appendChild(col);
    }
  }

  function renderHourlyChart(payload) {
    const hours = payload.usage.hours || {};
    const { startIso, endIso } = rangeBoundariesIso(payload);
    const buckets = Array.from({ length: 24 }, () => emptyUsageBucket());
    for (const [key, bucket] of Object.entries(hours)) {
      if (!key || key.length < 13) continue;
      const dayPart = key.slice(0, 10);
      if (dayPart < startIso || dayPart > endIso) continue;
      const hour = Number(key.slice(11, 13));
      if (!Number.isInteger(hour) || hour < 0 || hour > 23) continue;
      for (const field of Object.keys(buckets[hour])) {
        buckets[hour][field] += Number(bucket[field]) || 0;
      }
    }
    usageHourChart.innerHTML = "";
    const totalDuration = buckets.reduce((sum, b) => sum + b.durationSeconds, 0);
    if (totalDuration === 0 && buckets.every((b) => b.sessions === 0)) {
      usageHourNote.textContent = "Not enough data for an hourly breakdown yet.";
      return;
    }
    const maxDuration = Math.max(1, ...buckets.map((b) => b.durationSeconds));
    for (let hour = 0; hour < 24; hour += 1) {
      const bucket = buckets[hour];
      const ratio = bucket.durationSeconds / maxDuration;
      const col = document.createElement("div");
      col.className = "usage-hour-col";
      const label = String(hour).padStart(2, "0");
      col.title = `${label}:00 · ${formatDuration(bucket.durationSeconds)} · ${formatNumber(bucket.sessions)} sessions · ${formatNumber(bucket.commandsRun)} commands`;
      col.innerHTML = `
        <div class="usage-hour-bar-wrap"><span class="usage-hour-bar" style="height:${(ratio * 100).toFixed(1)}%"></span></div>
        <div class="usage-hour-label">${label}</div>
      `;
      usageHourChart.appendChild(col);
    }
    const peak = buckets.reduce(
      (acc, b, idx) => (b.durationSeconds > acc.durationSeconds ? { hour: idx, durationSeconds: b.durationSeconds } : acc),
      { hour: 0, durationSeconds: 0 },
    );
    const peakBucket = buckets[peak.hour];
    if (peak.durationSeconds > 0) {
      usageHourNote.textContent = `Peak hour: ${String(peak.hour).padStart(2, "0")}:00 — ${formatDuration(peakBucket.durationSeconds)} active, ${formatNumber(peakBucket.sessions)} sessions, ${formatNumber(peakBucket.commandsRun)} commands (${rangeLabel(usageRange)}).`;
    } else {
      usageHourNote.textContent = "Not enough data to identify a peak hour yet.";
    }
  }

  function renderUsageBreakdown(payload) {
    if (!payload || !payload.usage) return;
    if (usageView === "weekly") {
      renderWeeklyBreakdown(payload);
    } else {
      renderDailyBreakdown(payload);
    }
  }

  function renderDailyBreakdown(payload) {
    const days = payload.usage.days || {};
    const keys = dayKeysInRange(days, payload).reverse();
    usageBreakdownTitle.textContent = "Recent Days";
    usageBreakdown.innerHTML = "";
    if (keys.length === 0) {
      usageEmpty.classList.remove("hidden");
      return;
    }
    usageEmpty.classList.add("hidden");
    const maxDuration = Math.max(1, ...keys.map((k) => Number(days[k].durationSeconds) || 0));
    for (const dayKey of keys) {
      const bucket = days[dayKey];
      const ratio = Math.min(1, (Number(bucket.durationSeconds) || 0) / maxDuration);
      const row = document.createElement("div");
      row.className = "usage-day-row";
      row.innerHTML = `
        <div class="usage-day-key">${dayKey}</div>
        <div class="usage-day-bar"><span style="width:${(ratio * 100).toFixed(1)}%"></span></div>
        <div class="usage-day-stats">
          <span>${formatDuration(bucket.durationSeconds)}</span>
          <span>${formatNumber(bucket.sessions)} sess</span>
          <span>${formatNumber(bucket.commandsRun)} cmds</span>
        </div>
      `;
      usageBreakdown.appendChild(row);
    }
  }

  function renderWeeklyBreakdown(payload) {
    const days = payload.usage.days || {};
    const keys = dayKeysInRange(days, payload);
    const weeks = new Map();
    for (const dayKey of keys) {
      const bucket = days[dayKey];
      if (!bucket) continue;
      const monday = startOfIsoWeek(dayKey);
      const weekKey = toIsoDate(monday);
      let entry = weeks.get(weekKey);
      if (!entry) {
        entry = { monday, bucket: emptyUsageBucket() };
        weeks.set(weekKey, entry);
      }
      for (const field of Object.keys(entry.bucket)) {
        entry.bucket[field] += Number(bucket[field]) || 0;
      }
    }
    const sortedWeeks = [...weeks.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
    usageBreakdownTitle.textContent = "Recent Weeks";
    usageBreakdown.innerHTML = "";
    if (sortedWeeks.length === 0) {
      usageEmpty.classList.remove("hidden");
      return;
    }
    usageEmpty.classList.add("hidden");
    const maxDuration = Math.max(1, ...sortedWeeks.map(([, w]) => Number(w.bucket.durationSeconds) || 0));
    for (const [, week] of sortedWeeks) {
      const ratio = Math.min(1, (Number(week.bucket.durationSeconds) || 0) / maxDuration);
      const row = document.createElement("div");
      row.className = "usage-day-row";
      row.innerHTML = `
        <div class="usage-day-key">${formatWeekLabel(week.monday)}</div>
        <div class="usage-day-bar"><span style="width:${(ratio * 100).toFixed(1)}%"></span></div>
        <div class="usage-day-stats">
          <span>${formatDuration(week.bucket.durationSeconds)}</span>
          <span>${formatNumber(week.bucket.sessions)} sess</span>
          <span>${formatNumber(week.bucket.commandsRun)} cmds</span>
        </div>
      `;
      usageBreakdown.appendChild(row);
    }
  }

  function setUsageView(view) {
    if (view !== "daily" && view !== "weekly") return;
    usageView = view;
    for (const btn of usageViewToggle.querySelectorAll("[data-view]")) {
      const isActive = btn.dataset.view === view;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    if (lastUsagePayload) {
      renderUsageBreakdown(lastUsagePayload);
    }
  }

  function setUsageRange(range) {
    if (!["7d", "30d", "90d", "all"].includes(range)) return;
    usageRange = range;
    for (const btn of usageRangeGroup.querySelectorAll("[data-range]")) {
      const isActive = btn.dataset.range === range;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    if (lastUsagePayload) {
      renderUsageStats(lastUsagePayload);
      renderDailyChart(lastUsagePayload);
      renderHourlyChart(lastUsagePayload);
      renderUsageBreakdown(lastUsagePayload);
    }
  }

  async function pasteFromClipboard({ preserveKeyboardState = false, wasKeyboardFocused = composerHasKeyboardFocus() } = {}) {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        if (mobileComposerMode) {
          if (wasKeyboardFocused) {
            insertComposerText(text, true);
          } else {
            resetComposerTracking(true);
            sendDirectPtyPaste(text);
          }
          if (preserveKeyboardState) {
            restoreShortcutKeyboardState(wasKeyboardFocused);
          }
          return;
        }
        resetSpeechInputState();
        sendDirectPtyPaste(text);
        focusTerminal();
      }
    } catch (_error) {
      showToast("Clipboard paste needs browser permission.");
    }
  }

  function isTerminalCopyTarget(target) {
    return Boolean(target && (target === terminalElement || terminalElement.contains(target)));
  }

  function handleNativeTerminalCopy(event) {
    if (!terminalHasSelection() || !isTerminalCopyTarget(event.target)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    copyTerminalSelection().catch(() => {
      showToast("Clipboard copy is blocked by this browser.");
    });
  }

  document.addEventListener("copy", handleNativeTerminalCopy, true);

  function handleImagePaste(file) {
    if (!file) {
      return;
    }
    const MAX_IMAGE_BYTES = 16 * 1024 * 1024;
    if (file.size > MAX_IMAGE_BYTES) {
      showToast("Image too large (max 16MB).");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      const base64 = comma >= 0 ? result.slice(comma + 1) : result;
      if (!base64) {
        showToast("Couldn't read the image.");
        return;
      }
      sendMessage({
        type: "upload-image",
        data: base64,
        mime: file.type || "image/png",
      });
      showToast("Uploading screenshot…");
    };
    reader.onerror = () => showToast("Couldn't read the image.");
    reader.readAsDataURL(file);
  }

  // Pull an image out of a paste. iOS Safari surfaces a pasted screenshot as a
  // file item on clipboardData; there is no text/plain, so this must run before
  // the text-only early return below or the image is silently dropped.
  function imageFileFromClipboard(clipboardData) {
    const items = clipboardData?.items;
    if (!items) {
      return null;
    }
    for (const item of items) {
      if (item.kind === "file" && item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          return file;
        }
      }
    }
    return null;
  }

  document.addEventListener("paste", (event) => {
    const imageFile = imageFileFromClipboard(event.clipboardData);
    if (imageFile) {
      event.preventDefault();
      handleImagePaste(imageFile);
      return;
    }
    const text = event.clipboardData?.getData("text/plain") || "";
    if (!text) {
      return;
    }
    if (event.target === composerInput) {
      window.setTimeout(() => {
        autoSizeComposer();
        syncComposerState();
      }, 0);
      return;
    }
    if (isEditableTarget(event.target)) {
      return;
    }
    event.preventDefault();
    if (mobileComposerMode) {
      insertComposerText(text);
      return;
    }
    resetSpeechInputState();
    sendDirectPtyPaste(text);
    focusTerminal();
  });

  loginForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (passkeyRetryPending && passkeyRequiredScope) {
      passkeyRetryPending = false;
      loginMessage.textContent = "";
      loginOverlay.classList.add("hidden");
      reconnectSocket();
      return;
    }
    const token = tokenInput.value.trim();
    const userName = userInput ? userInput.value.trim() : "";
    if (serverConfig.multiTenant && !userName) {
      loginMessage.textContent = "Enter your username first.";
      return;
    }
    const requireToken = loginRequiresToken();
    if (requireToken && !token) {
      loginMessage.textContent = "Enter the access token first.";
      return;
    }
    if (serverConfig.multiTenant) {
      const ownerChanged = currentUser !== userName;
      currentUser = userName;
      localStorage.setItem(STORAGE_USER_KEY, userName);
      if (ownerChanged) {
        resetAuthenticationLifecycle();
      }
    }
    if (token) {
      localStorage.setItem(tokenStorageKey(), token);
    }
    loginMessage.textContent = "";
    if (waitingForProxyAuth && socket && socket.readyState === WebSocket.OPEN) {
      loginOverlay.classList.add("hidden");
      sendAuthResponse("", loginRealm);
      return;
    }
    if (pendingProfileId && socket && socket.readyState === WebSocket.OPEN) {
      const profileId = pendingProfileId;
      pendingProfileId = "";
      loginOverlay.classList.add("hidden");
      sendMessage({ type: "switch-profile", profile: profileId, session: loadActiveSession(profileId) });
      return;
    }
    connect();
  });

  // Show/prefill the username field only when the host is multi-tenant.
  function syncLoginFields() {
    if (userField) {
      userField.classList.toggle("hidden", !serverConfig.multiTenant);
    }
    if (userInput && serverConfig.multiTenant && !userInput.value) {
      userInput.value = currentUser;
    }
  }

  function signOut() {
    localStorage.removeItem(tokenStorageKey());
    if (serverConfig.multiTenant) {
      localStorage.removeItem(STORAGE_USER_KEY);
    }
    // Revoke this device's silent key so it can't auto-reconnect, and ask the
    // server to drop the stored public key.
    try {
      sendMessage({
        type: "forget-key",
        realm: deviceProtocolRealm(loginRealm),
        profile: serverConfig.profileMode ? activeProfileId : "",
        deviceId: getDeviceId(),
      });
    } catch (_error) {
      // ignore
    }
    hasDeviceKey = false;
    forgetDeviceKey(loginRealm);
    if (serverConfig.multiTenant) {
      currentUser = "";
    }
    resetAuthenticationLifecycle();
    if (socket) {
      reconnectForSessionSwitch = false;
      try {
        socket.close();
      } catch (_error) {
        // ignore
      }
    }
    syncLoginFields();
    setPasskeyRetryUi(false);
    loginOverlay.classList.remove("hidden");
    loginMessage.textContent = "";
    (serverConfig.multiTenant && userInput ? userInput : tokenInput).focus();
  }

  function updateUserBadge() {
    if (accountButton) {
      accountButton.classList.toggle("hidden", !serverConfig.multiTenant && !serverConfig.profileMode);
    }
    if (accountUserLabel) {
      accountUserLabel.textContent = `Signed in as ${currentUserLabel || currentUser || "user"}`;
    }
    if (accountHelperText) {
      accountHelperText.textContent = serverConfig.profileMode
        ? "Passkeys registered for this auth realm."
        : "Devices signed in to this user. Rotating the token signs out every device.";
    }
    if (rotateTokenButton) {
      rotateTokenButton.textContent = serverConfig.profileMode ? "Revoke all passkeys" : "Rotate token";
    }
  }

  function renderDeviceList(devices) {
    if (!deviceList) {
      return;
    }
    deviceList.innerHTML = "";
    if (!Array.isArray(devices) || !devices.length) {
      const empty = document.createElement("div");
      empty.className = "menu-empty";
      empty.textContent = "No devices recorded yet.";
      deviceList.appendChild(empty);
      return;
    }
    const thisId = getDeviceId();
    devices.forEach((device) => {
      const row = document.createElement("div");
      row.className = "file-bookmark-item";
      const seen = (device.lastSeen || "").replace("T", " ");
      if (serverConfig.profileMode && device.credentialId) {
        row.className = "file-bookmark-row";
        const label = document.createElement("span");
        label.className = "file-bookmark-open passkey-credential-label";
        label.textContent = `${device.label || "passkey"} · ${seen}`;
        const revoke = document.createElement("button");
        revoke.className = "file-bookmark-remove";
        revoke.type = "button";
        revoke.textContent = "Revoke";
        revoke.addEventListener("click", () => {
          sendMessage({ type: "revoke-credential", credentialId: device.credentialId });
        });
        row.append(label, revoke);
      } else {
        const isThis = Boolean(device.id) && typeof thisId === "string" && thisId.startsWith(device.id);
        row.textContent = `${device.label || "device"}${isThis ? " (this device)" : ""} · ${seen}`;
      }
      deviceList.appendChild(row);
    });
  }

  function openAccount() {
    closeSettingsMenu();
    updateUserBadge();
    renderDeviceList([]);
    sendMessage({ type: "request-devices" });
    accountOverlay.classList.remove("hidden");
  }

  function closeAccount() {
    accountOverlay.classList.add("hidden");
  }

  if (accountButton) {
    accountButton.addEventListener("click", openAccount);
  }
  if (closeAccountButton) {
    closeAccountButton.addEventListener("click", closeAccount);
  }
  if (signOutButton) {
    signOutButton.addEventListener("click", () => {
      closeAccount();
      signOut();
    });
  }
  if (rotateTokenButton) {
    rotateTokenButton.addEventListener("click", () => {
      if (serverConfig.profileMode) {
        if (window.confirm("Revoke every passkey for this auth realm?")) {
          sendMessage({ type: "revoke-all-credentials" });
        }
        return;
      }
      if (window.confirm("Rotate the token? This signs out every device for this user.")) {
        sendMessage({ type: "rotate-token" });
      }
    });
  }

  document.getElementById("newTabButton").addEventListener("click", () => {
    closeTabMenu();
    closeSessionMenu();
    closeSettingsMenu();
    resetComposerTracking(true);
    sendMessage({ type: "new-tab" });
  });

  clearComposerButton.addEventListener("click", forceClearComposer);

  auxButton.addEventListener("click", toggleAuxMenu);
  profileButton.addEventListener("click", toggleProfileMenu);
  auxSessionsButton.addEventListener("click", toggleSessionMenu);
  auxFilesButton.addEventListener("click", () => {
    closeAuxMenu();
    openFileRootPicker("new");
  });
  auxBtopButton.addEventListener("click", openBtopTargetMenu);
  btopControls.querySelectorAll("[data-btop-key]").forEach((button) => {
    button.addEventListener("click", () => {
      sendMessage({ type: "input", data: button.dataset.btopKey });
    });
  });
  btopZoomInput.addEventListener("input", () => setBtopZoom(btopZoomInput.value));
  fileRootForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (fileRootForm.dataset.mode === "change") {
      changeActiveEditorRoot(fileRootInput.value);
      return;
    }
    createEditorTab(fileRootInput.value);
  });
  document.getElementById("cancelFileRootButton").addEventListener("click", closeFileRootPicker);
  useHomeRootButton.addEventListener("click", () => {
    if (useHomeRootButton.dataset.path) {
      fileRootInput.value = useHomeRootButton.dataset.path;
    }
  });
  fileBookmarkButton.addEventListener("click", () => addFileBookmark(fileRootInput.value));
  fileRootOverlay.addEventListener("click", (event) => {
    if (event.target === fileRootOverlay) {
      closeFileRootPicker();
    }
  });
  fileChangeRootButton.addEventListener("click", () => openFileRootPicker("change"));
  fileRefreshButton.addEventListener("click", () => {
    const tab = activeEditorTab();
    if (tab) {
      tab.tree = {};
      requestFileList(tab, tab.root);
    }
  });
  fileTreeToggleButton.addEventListener("click", () => {
    const tab = activeEditorTab();
    if (!tab) {
      return;
    }
    tab.treeHidden = !tab.treeHidden;
    persistEditorTabs();
    renderFileWorkspace();
  });
  fileTreeScrim.addEventListener("click", () => {
    const tab = activeEditorTab();
    if (!tab) {
      return;
    }
    tab.treeHidden = true;
    persistEditorTabs();
    renderFileWorkspace();
  });
  fileSaveButton.addEventListener("click", saveActiveFile);
  fileMarkdownToggleButton.addEventListener("click", () => {
    const tab = activeEditorTab();
    const file = activeOpenFile(tab);
    if (!file || !isMarkdownFile(file) || !file.loaded) {
      return;
    }
    file.previewMode = !file.previewMode;
    renderFileWorkspace();
    if (!file.previewMode) {
      fileEditorInput.focus({ preventScroll: true });
    }
  });
  fileEditorInput.addEventListener("input", () => {
    const tab = activeEditorTab();
    const file = activeOpenFile(tab);
    if (!tab || !file) {
      return;
    }
    file.content = fileEditorInput.value;
    file.dirty = file.content !== file.originalContent;
    file.loaded = true;
    persistEditorTabs();
    renderOpenFileTabs(tab);
    updateFileControls(tab);
  });
  fileEditorInput.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveActiveFile();
    }
  });

  document.getElementById("renameTabButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.key === openTabMenuKey) || activeTab();
    if (!current) {
      return;
    }
    const nextName = window.prompt("Rename current tab", current.name || "");
    if (nextName) {
      if (current.type === "editor") {
        const tab = editorTabById(current.id);
        if (tab) {
          tab.name = nextName.trim().slice(0, 40) || tab.name;
          persistEditorTabs();
          syncOpenTabsToSessions();
        }
      } else {
        sendMessage({ type: "rename-tab", session: current.name, name: nextName });
      }
    }
    closeTabMenu();
  });

  document.getElementById("detachOthersButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.key === openTabMenuKey) || activeTab();
    if (!current || current.type === "editor") {
      return;
    }
    sendMessage({ type: "detach-other-clients", session: current.name });
    closeTabMenu();
  });

  document.getElementById("closeTabButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.key === openTabMenuKey) || activeTab();
    if (!current) {
      return;
    }
    if (current.type === "editor") {
      closeEditorTab(current.id);
      closeTabMenu();
      return;
    }
    const terminalTabs = currentTabs.filter((tab) => tab.type !== "editor");
    if (terminalTabs.length <= 1) {
      showToast("The last terminal tab stays open.");
      closeTabMenu();
      return;
    }
    if (current.name === activeSessionName) {
      const fallback = terminalTabs.find((tab) => tab.name !== current.name);
      if (!fallback) {
        showToast("The last terminal tab stays open.");
        closeTabMenu();
        return;
      }
      removeOpenTab(current.name);
      closeTabMenu();
      switchSession(fallback.name);
      return;
    }
    removeOpenTab(current.name);
    syncOpenTabsToSessions();
    closeTabMenu();
  });

  document.getElementById("killSessionButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.key === openTabMenuKey) || activeTab();
    if (!current || current.type === "editor") {
      return;
    }
    if (!window.confirm(`Kill tmux session "${current.name}"?`)) {
      return;
    }
    sendMessage({ type: "kill-session", session: current.name });
    closeTabMenu();
  });
  composerInput.addEventListener("focus", () => {
    composerPanel.classList.remove("hidden");
    setComposerActive(true);
    autoSizeComposer();
    scheduleLayoutRefresh({ preserveTerminalCols: true });
  });
  composerInput.addEventListener("blur", () => {
    setComposerActive(false);
    // After blur, iOS sometimes neglects to fire a visualViewport resize even
    // though the keyboard actually closed. Re-measure on a couple of delays
    // so --app-height and --shortcut-reserve recover.
    [60, 220, 520].forEach((delay) =>
      window.setTimeout(() => {
        updateViewportMetrics();
        scheduleLayoutRefresh({ preserveTerminalCols: true });
      }, delay),
    );
  });
  composerInput.addEventListener("input", () => {
    lastComposerInputAt = Date.now();
    const value = composerInput.value;
    const normalizedValue = value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    if (normalizedValue !== value) {
      const cursor = composerInput.selectionEnd ?? value.length;
      const nextCursor = normalizedValue.slice(0, cursor).replace(/\r\n/g, "\n").replace(/\r/g, "\n").length;
      composerInput.value = normalizedValue;
      try {
        composerInput.setSelectionRange(nextCursor, nextCursor);
      } catch (_error) {
        // Ignore transient selection errors while the textarea is unfocused.
      }
    }
    autoSizeComposer();
    syncComposerState();
  });
  composerInput.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && !event.altKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      resetComposerTracking(true);
      sendMessage({ type: "input", data: event.shiftKey ? specialMap["SHIFT+TAB"] : specialMap.TAB });
      requestComposerRefresh();
      return;
    }
    if (event.key === "Enter") {
      if (event.shiftKey) {
        return;
      }
      event.preventDefault();
      commitComposerLine();
      return;
    }
    if (event.key === "ArrowUp" && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      navigateComposerHistory("up");
      return;
    }
    if (event.key === "ArrowDown" && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      navigateComposerHistory("down");
      return;
    }
    if (
      event.key === "Backspace" &&
      !event.shiftKey &&
      !event.altKey &&
      !event.metaKey &&
      !event.ctrlKey &&
      composerInput.value === ""
    ) {
      event.preventDefault();
      sendMessage({ type: "input", data: specialMap.BACKSPACE });
      requestComposerRefresh();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      clearComposer(true);
      closeComposer();
    }
  });
  document.getElementById("editShortcutsButton").addEventListener("click", openEditor);
  document.getElementById("closeEditorButton").addEventListener("click", closeEditor);
  document.getElementById("addShortcutButton").addEventListener("click", () => {
    shortcutEditorList.appendChild(buildEditorRow({ visible: true }));
  });
  document.getElementById("saveShortcutsButton").addEventListener("click", () => {
    const nextShortcuts = collectEditorShortcuts();
    saveShortcuts(nextShortcuts.length ? nextShortcuts : defaultShortcuts.slice());
    closeEditor();
  });
  document.getElementById("editGesturesButton").addEventListener("click", openGestureEditor);
  document.getElementById("closeGestureButton").addEventListener("click", closeGestureEditor);
  document.getElementById("saveGesturesButton").addEventListener("click", () => {
    saveGestures(collectGestureBindings());
    showToast("Gestures saved.");
    closeGestureEditor();
  });
  document.getElementById("resetGesturesButton").addEventListener("click", () => {
    saveGestures(null);
    openGestureEditor();
  });
  document.getElementById("displayButton").addEventListener("click", openDisplay);
  authenticationButton.addEventListener("click", openAuthentication);
  authenticationModeInput.addEventListener("change", () => {
    updateAuthenticationDraft({
      mode: authenticationModeInput.value,
      idleMinutes: authenticationIdleInput.value,
    }, draftAuthenticationRealm);
  });
  authenticationIdleInput.addEventListener("change", () => {
    updateAuthenticationDraft({
      mode: authenticationModeInput.value,
      idleMinutes: authenticationIdleInput.value,
    }, draftAuthenticationRealm);
  });
  document.getElementById("closeAuthenticationButton").addEventListener("click", saveAuthentication);
  document.getElementById("saveAuthenticationButton").addEventListener("click", saveAuthentication);
  authenticationOverlay.addEventListener("click", (event) => {
    if (event.target === authenticationOverlay) {
      saveAuthentication();
    }
  });
  document.getElementById("usageButton").addEventListener("click", openUsage);
  document.getElementById("closeUsageButton").addEventListener("click", closeUsage);
  document.getElementById("refreshUsageButton").addEventListener("click", () => {
    renderUsageLoading();
    requestUsage();
  });
  usageOverlay.addEventListener("click", (event) => {
    if (event.target === usageOverlay) {
      closeUsage();
    }
  });
  usageViewToggle.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-view]");
    if (target && target.dataset.view) {
      setUsageView(target.dataset.view);
    }
  });
  usageRangeGroup.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-range]");
    if (target && target.dataset.range) {
      setUsageRange(target.dataset.range);
    }
  });
  settingsButton.addEventListener("click", toggleSettingsMenu);
  document.getElementById("closeDisplayButton").addEventListener("click", () => closeDisplay(true));
  document.getElementById("saveDisplayButton").addEventListener("click", () => closeDisplay(true));
  editorOverlay.addEventListener("click", (event) => {
    if (event.target === editorOverlay) {
      closeEditor();
    }
  });
  gestureOverlay.addEventListener("click", (event) => {
    if (event.target === gestureOverlay) {
      closeGestureEditor();
    }
  });
  displayOverlay.addEventListener("click", (event) => {
    if (event.target === displayOverlay) {
      closeDisplay(true);
    }
  });
  document.getElementById("resetDisplayButton").addEventListener("click", () => {
    updateDisplayDraft(DEFAULT_UI_SCALE, DEFAULT_TERMINAL_FONT);
  });

  document.addEventListener("pointerdown", reportForcedActivity, { capture: true, passive: true });
  document.addEventListener("touchstart", reportForcedActivity, { capture: true, passive: true });

  let lastTouchEndAt = 0;
  document.addEventListener(
    "touchend",
    (event) => {
      const now = Date.now();
      if (now - lastTouchEndAt < 300) {
        event.preventDefault();
      }
      lastTouchEndAt = now;
    },
    { passive: false },
  );
  document.addEventListener(
    "gesturestart",
    (event) => {
      event.preventDefault();
    },
    { passive: false },
  );
  document.addEventListener(
    "gesturechange",
    (event) => {
      event.preventDefault();
    },
    { passive: false },
  );
  document.addEventListener(
    "gestureend",
    (event) => {
      event.preventDefault();
    },
    { passive: false },
  );

  uiScaleInput.addEventListener("input", (event) => {
    updateDisplayDraft(Number.parseFloat(event.target.value), draftTerminalFontSize);
  });
  terminalFontInput.addEventListener("input", (event) => {
    updateDisplayDraft(draftUiScale, Number.parseInt(event.target.value, 10));
  });

  term.onKey(() => reportActivity());

  term.onData((data) => {
    if (activeTab()?.type === "editor") {
      return;
    }
    if (mobileComposerMode) {
      return;
    }
    if (data.length === 1) {
      resetSpeechInputState();
    }
    sendMessage({ type: "input", data });
  });

  term.onScroll(() => {
    refreshFollowOutput();
    scheduleTerminalSelectionUISync();
  });

  const layoutObserver = new ResizeObserver(() => {
    measureShortcutHeight();
    fitTerminal({ preserveCols: true });
    scheduleTerminalSelectionUISync();
  });
  layoutObserver.observe(shortcutsPanel);
  layoutObserver.observe(composerPanel);
  layoutObserver.observe(terminalElement);

  window.visualViewport?.addEventListener("resize", () => {
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.visualViewport?.addEventListener("scroll", updateViewportMetrics);
  // When a field inside a settings sheet gets focus, refresh viewport metrics
  // (so the overlay re-anchors above the keyboard) and scroll the field into
  // view within the scrollable sheet once the keyboard animation settles.
  document.addEventListener("focusin", (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") {
      return;
    }
    if (!target.closest(".overlay .sheet")) {
      return;
    }
    syncOverlayToViewport();
    // Re-pin across the keyboard's open animation, then bring the field into view.
    [120, 280, 480].forEach((delay) =>
      window.setTimeout(() => {
        syncOverlayToViewport();
        if (delay === 480 && document.activeElement === target && typeof target.scrollIntoView === "function") {
          target.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }, delay),
    );
  });
  window.addEventListener("resize", () => {
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.addEventListener("orientationchange", () => {
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.addEventListener("focus", () => {
    reportForcedActivity();
    updateViewportMetrics();
    resumeApplication();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      recordBackgrounded();
    } else if (document.visibilityState === "visible") {
      resumeApplication();
    }
  });
  window.addEventListener("pagehide", recordBackgrounded);
  window.setInterval(recordVisibleIdleCheckpoint, 15000);
  window.addEventListener("online", () => resumeApplication());
  window.addEventListener("pageshow", () => {
    resumeApplication();
  });
  document.addEventListener("click", (event) => {
    if (
      openTabMenuKey &&
      !tabMenu.contains(event.target) &&
      !tabsStrip.contains(event.target)
    ) {
      closeTabMenu();
    }
    if (
      sessionMenuOpen &&
      !sessionMenu.contains(event.target) &&
      !auxButton.contains(event.target) &&
      !auxMenu.contains(event.target)
    ) {
      closeSessionMenu();
    }
    if (
      profileMenuOpen &&
      !profileMenu.contains(event.target) &&
      !profileButton.contains(event.target)
    ) {
      closeProfileMenu();
    }
    if (
      settingsMenuOpen &&
      !settingsMenu.contains(event.target) &&
      !settingsButton.contains(event.target)
    ) {
      closeSettingsMenu();
    }
    if (
      auxMenuOpen &&
      !auxMenu.contains(event.target) &&
      !auxButton.contains(event.target)
    ) {
      closeAuxMenu();
    }
    if (
      btopTargetMenuOpen &&
      !btopTargetMenu.contains(event.target) &&
      !auxButton.contains(event.target) &&
      !auxMenu.contains(event.target)
    ) {
      closeBtopTargetMenu();
    }
  });
  document.addEventListener("focusin", updateViewportMetrics);
  document.addEventListener("focusout", () => {
    window.setTimeout(updateViewportMetrics, 120);
  });

  renderShortcutBar();
  applyUiScale(uiScale, false);
  applyTerminalFontSize(terminalFontSize, false);
  guardTerminalHelperTextarea();
  installTerminalScrollHandlers();
  installTabStripScrollHandlers();
  installShortcutBarScrollHandlers();
  installMobileTextInputGuards();
  if (mobileComposerMode) {
    setComposerActive(false);
    openComposer(false);
  }
  updateViewportMetrics();
  document.fonts?.ready?.then(() => {
    scheduleLayoutRefresh();
  });
  loadServerConfig()
    // Learn whether this device already holds a silent key before deciding
    // whether to prompt for a token, so enrolled devices never see the overlay.
    .then(prepareAuthenticationClient, async () => {
      resumeHandlingReady = true;
      await resumeApplication();
      return true;
    })
    .then((authenticationReady) => {
      if (!serverConfig.requireToken) {
        loginOverlay.classList.add("hidden");
      }
      if (authenticationReady) {
        connect();
      }
    });

  // Cache the UI shell for instant, round-trip-free loads. Only registers in a
  // secure context (HTTPS/localhost); over plain HTTP `navigator.serviceWorker`
  // is undefined, so this is a harmless no-op until the app is served via HTTPS.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }
})();
