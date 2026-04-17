(function () {
  const STORAGE_TOKEN_KEY = "mobile-terminal.token";
  const STORAGE_SHORTCUTS_KEY = "mobile-terminal.shortcuts";
  const STORAGE_UI_SCALE_KEY = "mobile-terminal.ui-scale";
  const STORAGE_TERMINAL_FONT_KEY = "mobile-terminal.terminal-font";
  const STORAGE_ACTIVE_SESSION_KEY = "mobile-terminal.active-session";
  const STORAGE_OPEN_TABS_KEY = "mobile-terminal.open-tabs";
  const DEFAULT_UI_SCALE = 1;
  const DEFAULT_TERMINAL_FONT = 15;
  const KEYBOARD_THRESHOLD = 80;
  const UI_SCALE_FIT_WIDTH = 430;
  const UI_SCALE_FIT_HEIGHT = 700;
  const decoder = new TextDecoder();
  const defaultShortcuts = [
    { label: "Paste", sequence: "{PASTE}" },
    { label: "Tab", sequence: "{TAB}" },
    { label: "Esc", sequence: "{ESC}" },
    { label: "Up", sequence: "{UP}" },
    { label: "Down", sequence: "{DOWN}" },
    { label: "Left", sequence: "{LEFT}" },
    { label: "Right", sequence: "{RIGHT}" },
    { label: "Ctrl+C", sequence: "{CTRL+C}" },
    { label: "Ctrl+L", sequence: "{CTRL+L}" },
    { label: "Ctrl+R", sequence: "{CTRL+R}" },
    { label: "Ctrl+X Tab", sequence: "{CTRL+X}{TAB}" },
  ];
  const specialMap = {
    TAB: "\t",
    ENTER: "\r",
    ESC: "\u001b",
    SPACE: " ",
    BACKSPACE: "\u007f",
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
  const loginOverlay = document.getElementById("loginOverlay");
  const loginForm = document.getElementById("loginForm");
  const tokenInput = document.getElementById("tokenInput");
  const loginMessage = document.getElementById("loginMessage");
  const toast = document.getElementById("toast");
  const tabMenu = document.getElementById("tabMenu");
  const openSessionButton = document.getElementById("openSessionButton");
  const sessionMenu = document.getElementById("sessionMenu");
  const settingsButton = document.getElementById("settingsButton");
  const settingsMenu = document.getElementById("settingsMenu");
  const editorOverlay = document.getElementById("editorOverlay");
  const shortcutEditorList = document.getElementById("shortcutEditorList");
  const displayOverlay = document.getElementById("displayOverlay");
  const uiScaleInput = document.getElementById("uiScaleInput");
  const terminalFontInput = document.getElementById("terminalFontInput");
  const uiScaleValue = document.getElementById("uiScaleValue");
  const terminalFontValue = document.getElementById("terminalFontValue");
  const displayPreview = document.getElementById("displayPreview");
  const displayUiPreview = document.getElementById("displayUiPreview");
  const displayTerminalPreview = document.getElementById("displayTerminalPreview");

  let serverConfig = {
    requireToken: true,
    tailscaleMode: false,
    allowedClients: [],
  };

  let uiScale = loadNumericSetting(STORAGE_UI_SCALE_KEY, DEFAULT_UI_SCALE, 0.5, 1.4);
  let effectiveUiScale = uiScale;
  let terminalFontSize = loadNumericSetting(STORAGE_TERMINAL_FONT_KEY, DEFAULT_TERMINAL_FONT, 5, 24);
  let draftUiScale = uiScale;
  let draftTerminalFontSize = terminalFontSize;
  document.documentElement.style.setProperty("--ui-scale", String(effectiveUiScale));
  document.documentElement.style.setProperty("--terminal-font-size", `${terminalFontSize}px`);

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: terminalFontSize,
    lineHeight: 1.2,
    scrollback: 5000,
    theme: {
      background: "#08131a",
      foreground: "#e6edf3",
      cursor: "#ffd166",
      selectionBackground: "rgba(255, 209, 102, 0.22)",
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
  term.open(document.getElementById("terminal"));

  let socket = null;
  let reconnectTimer = null;
  let fitTimer = null;
  let viewportSettleTimers = [];
  let lastStableViewportWidth = 0;
  let lastStableViewportHeight = 0;
  let currentTabs = [];
  let shortcuts = loadShortcuts();
  let openTabMenuName = null;
  let currentSessions = [];
  let openTabNames = loadOpenTabs();
  let selectedSessionName = localStorage.getItem(STORAGE_ACTIVE_SESSION_KEY) || "";
  let activeSessionName = "";
  let followOutput = true;
  let reconnectForSessionSwitch = false;
  let sessionMenuOpen = false;
  let settingsMenuOpen = false;
  let touchScrollState = null;
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
    return defaultShortcuts.map((shortcut) => ({ ...shortcut, visible: true }));
  }

  function loadOpenTabs() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_OPEN_TABS_KEY) || "null");
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

  function saveShortcuts(nextShortcuts) {
    shortcuts = nextShortcuts
      .map(normalizeShortcut)
      .filter((shortcut) => shortcut && shortcut.label && shortcut.sequence);
    localStorage.setItem(STORAGE_SHORTCUTS_KEY, JSON.stringify(shortcuts));
    renderShortcutBar();
    scheduleLayoutRefresh();
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.remove("hidden");
    window.clearTimeout(showToast.timeoutId);
    showToast.timeoutId = window.setTimeout(() => {
      toast.classList.add("hidden");
    }, 2800);
  }

  function persistActiveSession(sessionName) {
    if (!sessionName) {
      localStorage.removeItem(STORAGE_ACTIVE_SESSION_KEY);
      return;
    }
    localStorage.setItem(STORAGE_ACTIVE_SESSION_KEY, sessionName);
  }

  function persistOpenTabs() {
    if (!openTabNames.length) {
      localStorage.removeItem(STORAGE_OPEN_TABS_KEY);
      return;
    }
    localStorage.setItem(STORAGE_OPEN_TABS_KEY, JSON.stringify(openTabNames));
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
    if (!currentSessions.length) {
      const fallbackTabs = openTabNames.length
        ? openTabNames
        : activeSessionName
          ? [activeSessionName]
          : [];
      currentTabs = fallbackTabs.map((name) => ({
        name,
        active: name === activeSessionName,
        attached: 0,
        windows: 0,
      }));
      renderTabs();
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
    currentTabs = openTabNames
      .map((name) => {
        const session = sessionByName.get(name);
        if (!session) {
          return null;
        }
        return {
          name,
          active: name === activeSessionName,
          attached: session.attached,
          windows: session.windows,
        };
      })
      .filter(Boolean);

    renderTabs();
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
    composerInput.style.height = "auto";
    composerInput.style.height = `${Math.min(composerInput.scrollHeight, window.innerHeight * 0.34)}px`;
    scheduleLayoutRefresh();
  }

  function setComposerActive(active) {
    if (!mobileComposerMode) {
      return;
    }
    document.body.dataset.composerActive = active ? "true" : "false";
    scheduleLayoutRefresh();
    window.requestAnimationFrame(() => {
      scheduleLayoutRefresh();
    });
  }

  function wsUrl() {
    const url = new URL("/_ws", window.location.href);
    if (selectedSessionName) {
      url.searchParams.set("session", selectedSessionName);
    }
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  function openComposer(focus = true) {
    if (!mobileComposerMode) {
      return;
    }
    composerPanel.classList.remove("hidden");
    autoSizeComposer();
    if (!focus) {
      return;
    }
    composerInput.focus({ preventScroll: true });
    const length = composerInput.value.length;
    composerInput.setSelectionRange(length, length);
  }

  function closeComposer() {
    if (!mobileComposerMode) {
      return;
    }
    composerInput.blur();
    setComposerActive(false);
    composerPanel.classList.add("hidden");
  }

  function clearComposer() {
    if (!mobileComposerMode) {
      return;
    }
    composerInput.value = "";
    composerInput.style.height = "";
    resetSpeechInputState();
  }

  function flushComposerText() {
    if (!mobileComposerMode) {
      return "";
    }
    const value = composerInput.value;
    if (!value) {
      return "";
    }
    sendMessage({ type: "input", data: value });
    clearComposer();
    return value;
  }

  function commitComposerLine() {
    if (!mobileComposerMode) {
      sendMessage({ type: "input", data: "\r" });
      return;
    }
    flushComposerText();
    sendMessage({ type: "input", data: "\r" });
    openComposer(true);
  }

  function shortcutShouldFlushComposer(sequence) {
    const upper = sequence.toUpperCase();
    return (
      upper.includes("{TAB}") ||
      upper.includes("{ENTER}") ||
      upper.includes("{UP}") ||
      upper.includes("{DOWN}") ||
      upper.includes("{LEFT}") ||
      upper.includes("{RIGHT}") ||
      upper.includes("{HOME}") ||
      upper.includes("{END}") ||
      upper.includes("{PGUP}") ||
      upper.includes("{PGDN}") ||
      upper.includes("{TEXT:")
    );
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
      };
    }
  }

  function activeTab() {
    return currentTabs.find((tab) => tab.active) || currentTabs[0] || null;
  }

  function updateSessionInventory(sessions, nextActiveSession = activeSessionName) {
    currentSessions = Array.isArray(sessions) ? sessions : [];
    if (nextActiveSession) {
      activeSessionName = nextActiveSession;
    }
    syncOpenTabsToSessions();
    renderSessionMenu();
  }

  function sendMessage(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    if (payload && payload.type === "input") {
      followOutput = true;
    }
    socket.send(JSON.stringify(payload));
  }

  function scheduleLayoutRefresh() {
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(() => {
      measureShortcutHeight();
      fitTerminal();
      positionTabMenu();
      positionSessionMenu();
      positionSettingsMenu();
    }, 40);
  }

  function refreshFollowOutput() {
    const buffer = term.buffer.active;
    followOutput = buffer.viewportY >= buffer.baseY;
  }

  function fitTerminal() {
    window.requestAnimationFrame(() => {
      fitAddon.fit();
      sendMessage({ type: "resize", cols: term.cols, rows: term.rows });
      if (followOutput) {
        term.scrollToBottom();
      }
    });
  }

  function scrollTerminalByPixels(pixelDelta) {
    if (!term.rows || !Number.isFinite(pixelDelta) || pixelDelta === 0) {
      return;
    }
    const lineHeight = term.options.fontSize * (term.options.lineHeight || 1);
    if (!lineHeight) {
      return;
    }
    const lineDelta = pixelDelta / lineHeight;
    if (Math.abs(lineDelta) < 0.35) {
      return;
    }
    const lines = Math.round(lineDelta);
    sendMessage({ type: "scroll-history", lines });
    if (lines > 0) {
      followOutput = false;
    }
  }

  function installTerminalScrollHandlers() {
    const terminalRoot = document.getElementById("terminal");
    if (!terminalRoot) {
      return;
    }
    const wheelTarget = terminalRoot.querySelector(".xterm-viewport") || terminalRoot;

    if (mobileComposerMode) {
      terminalRoot.addEventListener("click", () => {
        openComposer(true);
      });
    }

    wheelTarget.addEventListener(
      "wheel",
      (event) => {
        if (!event.deltaY) {
          return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        scrollTerminalByPixels(-event.deltaY);
      },
      { passive: false, capture: true },
    );

    terminalRoot.addEventListener(
      "touchstart",
      (event) => {
        if (event.touches.length !== 1) {
          touchScrollState = null;
          return;
        }
        touchScrollState = { lastY: event.touches[0].clientY };
      },
      { passive: true },
    );

    terminalRoot.addEventListener(
      "touchmove",
      (event) => {
        if (!touchScrollState || event.touches.length !== 1) {
          return;
        }
        const nextY = event.touches[0].clientY;
        const deltaY = touchScrollState.lastY - nextY;
        if (Math.abs(deltaY) < 2) {
          return;
        }
        touchScrollState.lastY = nextY;
        event.preventDefault();
        scrollTerminalByPixels(deltaY);
      },
      { passive: false },
    );

    const resetTouchScroll = () => {
      touchScrollState = null;
    };
    terminalRoot.addEventListener("touchend", resetTouchScroll, { passive: true });
    terminalRoot.addEventListener("touchcancel", resetTouchScroll, { passive: true });
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

  function forceMinimumUiScale(persist = true) {
    uiScale = 0.5;
    draftUiScale = Math.min(draftUiScale, uiScale);
    if (persist) {
      localStorage.setItem(STORAGE_UI_SCALE_KEY, String(uiScale));
    }
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
    const nextSafeScale = computeViewportSafeUiScale(viewportWidth, viewportHeight, keyboardInset, layoutHeight);
    effectiveUiScale = Math.min(uiScale, nextSafeScale);
    document.documentElement.style.setProperty("--ui-scale", String(effectiveUiScale));
    return effectiveUiScale;
  }

  function measureShortcutHeight() {
    const panelRect = shortcutsPanel.getBoundingClientRect();
    const shortcutRect = shortcutBar.getBoundingClientRect();
    const composerRect =
      mobileComposerMode &&
      !composerPanel.classList.contains("hidden") &&
      document.body.dataset.composerActive === "true"
        ? composerPanel.getBoundingClientRect()
        : null;
    const viewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    const top = composerRect ? Math.min(panelRect.top, composerRect.top) : panelRect.top;
    const shortcutHeight = Math.ceil(shortcutRect.height);
    if (shortcutHeight > 0) {
      document.documentElement.style.setProperty("--shortcut-height", `${shortcutHeight}px`);
    }
    const reserve = Math.max(0, Math.ceil(viewportHeight - top));
    if (reserve > 0) {
      document.documentElement.style.setProperty("--shortcut-reserve", `${reserve}px`);
    }
  }

  function updateViewportMetrics() {
    const viewport = window.visualViewport;
    const viewportWidth = viewport ? viewport.width : window.innerWidth;
    const viewportHeight = viewport ? viewport.height : window.innerHeight;
    const offsetTop = viewport ? viewport.offsetTop : 0;
    const layoutHeight = window.innerHeight;
    const rawKeyboardInset = Math.max(0, layoutHeight - (viewportHeight + offsetTop));
    const keyboardInset = rawKeyboardInset > KEYBOARD_THRESHOLD ? rawKeyboardInset : 0;
    if (detectViewportShock(viewportWidth, viewportHeight, keyboardInset)) {
      forceMinimumUiScale();
    }
    document.documentElement.style.setProperty("--app-height", `${Math.round(viewportHeight)}px`);
    document.documentElement.style.setProperty("--keyboard-inset", `${Math.round(keyboardInset)}px`);
    document.body.dataset.keyboardOpen = keyboardInset > 0 ? "true" : "false";
    applyEffectiveUiScale(viewportWidth, viewportHeight, keyboardInset, layoutHeight);
    scheduleLayoutRefresh();
  }

  function renderTabs() {
    tabsStrip.innerHTML = "";
    currentTabs.forEach((tab) => {
      const button = document.createElement("button");
      button.className = `tab-pill${tab.active ? " is-active" : ""}`;
      button.type = "button";
      button.textContent = tab.name || "session";
      button.addEventListener("click", () => {
        if (Date.now() < suppressTabClickUntil) {
          return;
        }
        if (tab.active) {
          toggleTabMenu(tab.name);
          return;
        }
        switchSession(tab.name);
      });
      button.dataset.tabName = tab.name;
      tabsStrip.appendChild(button);
    });
    if (!currentTabs.some((tab) => tab.name === openTabMenuName && tab.active)) {
      closeTabMenu();
    } else {
      positionTabMenu();
    }
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
      const button = document.createElement("button");
      button.className = `tab-menu-button${session.name === activeSessionName ? " is-active" : ""}`;
      button.type = "button";
      button.textContent = session.name;
      button.addEventListener("click", () => {
        switchSession(session.name);
      });
      sessionMenu.appendChild(button);
    });
  }

  function renderShortcutBar() {
    shortcutBar.innerHTML = "";
    shortcuts.filter((shortcut) => shortcut.visible !== false).forEach((shortcut) => {
      const button = document.createElement("button");
      let preserveComposerFocus = false;
      button.className = "shortcut-button";
      button.type = "button";
      button.textContent = shortcut.label;
      button.addEventListener(
        "pointerdown",
        (event) => {
          preserveComposerFocus = mobileComposerMode && document.activeElement === composerInput;
          if (preserveComposerFocus) {
            event.preventDefault();
          }
        },
        { passive: false },
      );
      button.addEventListener("click", () => {
        if (Date.now() < suppressShortcutClickUntil) {
          return;
        }
        if (shortcut.sequence.trim().toUpperCase() === "{PASTE}") {
          pasteFromClipboard();
          if (preserveComposerFocus) {
            window.requestAnimationFrame(() => openComposer(true));
          }
          return;
        }
        if (mobileComposerMode && shortcutShouldFlushComposer(shortcut.sequence)) {
          flushComposerText();
        }
        const sequence = expandShortcutSequence(shortcut.sequence);
        if (!sequence) {
          if (preserveComposerFocus) {
            window.requestAnimationFrame(() => openComposer(true));
          }
          return;
        }
        sendMessage({ type: "input", data: sequence });
        if (preserveComposerFocus) {
          window.requestAnimationFrame(() => openComposer(true));
        }
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
      openComposer(true);
      scheduleLayoutRefresh();
      return;
    }
    term.focus();
    const helper = document.querySelector(".xterm-helper-textarea");
    if (helper) {
      helper.focus({ preventScroll: true });
    }
    scheduleLayoutRefresh();
  }

  function connect() {
    const token = localStorage.getItem(STORAGE_TOKEN_KEY);
    if (serverConfig.requireToken && !token) {
      loginOverlay.classList.remove("hidden");
      tokenInput.focus();
      return;
    }

    window.clearTimeout(reconnectTimer);
    loginOverlay.classList.add("hidden");
    socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";

    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "auth", token: token || "" }));
    });

    socket.addEventListener("message", async (event) => {
      if (typeof event.data === "string") {
        const payload = JSON.parse(event.data);
        handleServerMessage(payload);
        return;
      }
      let chunk = event.data;
      if (chunk instanceof Blob) {
        chunk = await chunk.arrayBuffer();
      }
      term.write(decoder.decode(chunk, { stream: true }));
      if (followOutput) {
        term.scrollToBottom();
      }
    });

    socket.addEventListener("close", (event) => {
      if (reconnectForSessionSwitch) {
        reconnectForSessionSwitch = false;
        window.setTimeout(connect, 80);
        return;
      }
      if (event.code === 4001) {
        loginOverlay.classList.remove("hidden");
        loginMessage.textContent = "Authentication failed. Check the token and try again.";
        localStorage.removeItem(STORAGE_TOKEN_KEY);
        return;
      }
      if (event.code === 4003) {
        showToast("This device is not allowed to connect.");
        return;
      }
      reconnectTimer = window.setTimeout(connect, 1500);
    });
  }

  function handleServerMessage(payload) {
    if (payload.type === "ready") {
      activeSessionName = payload.session || "";
      selectedSessionName = activeSessionName;
      persistActiveSession(activeSessionName);
      addOpenTab(activeSessionName);
      followOutput = true;
      loginOverlay.classList.add("hidden");
      loginMessage.textContent = "";
      syncOpenTabsToSessions();
      scheduleLayoutRefresh();
      focusTerminal();
      return;
    }
    if (payload.type === "tabs") {
      const nextActiveSession =
        payload.tabs?.find((tab) => tab.active)?.name || activeSessionName;
      updateSessionInventory(payload.tabs || [], nextActiveSession);
      scheduleLayoutRefresh();
      return;
    }
    if (payload.type === "notice") {
      showToast(payload.message);
      return;
    }
    if (payload.type === "sessions") {
      updateSessionInventory(payload.sessions || [], payload.activeSession || activeSessionName);
      if (sessionMenuOpen) {
        positionSessionMenu();
      }
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
        persistActiveSession(nextSession);
        reconnectForSessionSwitch = true;
      }
      return;
    }
    if (payload.type === "auth-error") {
      loginMessage.textContent = payload.message || "Authentication failed.";
    }
  }

  function toggleTabMenu(sessionName) {
    closeSessionMenu();
    closeSettingsMenu();
    if (openTabMenuName === sessionName) {
      closeTabMenu();
      return;
    }
    openTabMenuName = sessionName;
    tabMenu.classList.remove("hidden");
    positionTabMenu();
  }

  function positionTabMenu() {
    if (!openTabMenuName) {
      return;
    }
    const button = tabsStrip.querySelector(`[data-tab-name="${CSS.escape(openTabMenuName)}"]`);
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
    openTabMenuName = null;
    tabMenu.classList.add("hidden");
  }

  function toggleSessionMenu() {
    closeTabMenu();
    closeSettingsMenu();
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
    const rect = openSessionButton.getBoundingClientRect();
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

  function toggleSettingsMenu() {
    closeSessionMenu();
    closeTabMenu();
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

  function switchSession(sessionName) {
    if (!sessionName || sessionName === activeSessionName) {
      closeSessionMenu();
      return;
    }
    addOpenTab(sessionName);
    selectedSessionName = sessionName;
    persistActiveSession(sessionName);
    closeSessionMenu();
    closeTabMenu();
    syncOpenTabsToSessions();
    followOutput = true;
    resetSpeechInputState();
    clearComposer();
    term.reset();
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
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

    const labelInput = document.createElement("input");
    labelInput.className = "text-input shortcut-label-input";
    labelInput.placeholder = "Label";
    labelInput.value = shortcut.label || "";

    const sequenceInput = document.createElement("input");
    sequenceInput.className = "text-input shortcut-sequence-input";
    sequenceInput.placeholder = "{CTRL+C}";
    sequenceInput.value = shortcut.sequence || "";

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

    const moveUpButton = document.createElement("button");
    moveUpButton.type = "button";
    moveUpButton.className = "ghost-button shortcut-order-button";
    moveUpButton.textContent = "Up";
    moveUpButton.addEventListener("click", () => moveEditorRow(row, -1));

    const moveDownButton = document.createElement("button");
    moveDownButton.type = "button";
    moveDownButton.className = "ghost-button shortcut-order-button";
    moveDownButton.textContent = "Down";
    moveDownButton.addEventListener("click", () => moveEditorRow(row, 1));

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "ghost-button shortcut-remove-button";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => row.remove());

    controls.appendChild(visibilityLabel);
    controls.appendChild(moveUpButton);
    controls.appendChild(moveDownButton);
    controls.appendChild(removeButton);

    row.appendChild(labelInput);
    row.appendChild(sequenceInput);
    row.appendChild(controls);
    return row;
  }

  function openEditor() {
    closeSettingsMenu();
    shortcutEditorList.innerHTML = "";
    shortcuts.forEach((shortcut) => shortcutEditorList.appendChild(buildEditorRow(shortcut)));
    editorOverlay.classList.remove("hidden");
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
    }
    scheduleLayoutRefresh();
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
    } else {
      updateDisplayDraft(uiScale, terminalFontSize);
    }
    displayOverlay.classList.add("hidden");
  }

  async function pasteFromClipboard() {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        if (mobileComposerMode) {
          openComposer(true);
          composerInput.setRangeText(text, composerInput.selectionStart, composerInput.selectionEnd, "end");
          return;
        }
        resetSpeechInputState();
        sendMessage({ type: "input", data: text });
        focusTerminal();
      }
    } catch (_error) {
      showToast("Clipboard paste needs browser permission.");
    }
  }

  loginForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!token) {
      loginMessage.textContent = "Enter the access token first.";
      return;
    }
    localStorage.setItem(STORAGE_TOKEN_KEY, token);
    loginMessage.textContent = "";
    connect();
  });

  document.getElementById("newTabButton").addEventListener("click", () => {
    closeTabMenu();
    closeSessionMenu();
    closeSettingsMenu();
    clearComposer();
    sendMessage({ type: "new-tab" });
  });

  openSessionButton.addEventListener("click", toggleSessionMenu);

  document.getElementById("renameTabButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.name === openTabMenuName) || activeTab();
    if (!current) {
      return;
    }
    const nextName = window.prompt("Rename current tab", current.name || "");
    if (nextName) {
      sendMessage({ type: "rename-tab", session: current.name, name: nextName });
    }
    closeTabMenu();
  });

  document.getElementById("detachOthersButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.name === openTabMenuName) || activeTab();
    if (!current) {
      return;
    }
    sendMessage({ type: "detach-other-clients", session: current.name });
    closeTabMenu();
  });

  document.getElementById("closeTabButton").addEventListener("click", () => {
    const current = currentTabs.find((tab) => tab.name === openTabMenuName) || activeTab();
    if (!current) {
      return;
    }
    if (currentTabs.length <= 1) {
      showToast("The last visible tab stays open.");
      closeTabMenu();
      return;
    }
    if (current.name === activeSessionName) {
      const fallback = currentTabs.find((tab) => tab.name !== current.name);
      if (!fallback) {
        showToast("The last visible tab stays open.");
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
    const current = currentTabs.find((tab) => tab.name === openTabMenuName) || activeTab();
    if (!current) {
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
    scheduleLayoutRefresh();
  });
  composerInput.addEventListener("blur", () => {
    setComposerActive(false);
    window.setTimeout(scheduleLayoutRefresh, 60);
  });
  composerInput.addEventListener("input", () => {
    autoSizeComposer();
  });
  composerInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      if (event.shiftKey) {
        window.setTimeout(autoSizeComposer, 0);
        return;
      }
      event.preventDefault();
      commitComposerLine();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      clearComposer();
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
  document.getElementById("displayButton").addEventListener("click", openDisplay);
  settingsButton.addEventListener("click", toggleSettingsMenu);
  document.getElementById("closeDisplayButton").addEventListener("click", () => closeDisplay(true));
  document.getElementById("saveDisplayButton").addEventListener("click", () => closeDisplay(true));
  editorOverlay.addEventListener("click", (event) => {
    if (event.target === editorOverlay) {
      closeEditor();
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

  term.onData((data) => {
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
  });

  const layoutObserver = new ResizeObserver(() => {
    measureShortcutHeight();
    fitTerminal();
  });
  layoutObserver.observe(shortcutsPanel);
  layoutObserver.observe(composerPanel);

  window.visualViewport?.addEventListener("resize", () => {
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.visualViewport?.addEventListener("scroll", updateViewportMetrics);
  window.addEventListener("resize", () => {
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.addEventListener("orientationchange", () => {
    forceMinimumUiScale();
    updateViewportMetrics();
    scheduleViewportSettlePasses();
  });
  window.addEventListener("focus", () => {
    updateViewportMetrics();
    sendMessage({ type: "request-tabs" });
    sendMessage({ type: "request-sessions" });
  });
  document.addEventListener("click", (event) => {
    if (
      openTabMenuName &&
      !tabMenu.contains(event.target) &&
      !tabsStrip.contains(event.target)
    ) {
      closeTabMenu();
    }
    if (
      sessionMenuOpen &&
      !sessionMenu.contains(event.target) &&
      !openSessionButton.contains(event.target)
    ) {
      closeSessionMenu();
    }
    if (
      settingsMenuOpen &&
      !settingsMenu.contains(event.target) &&
      !settingsButton.contains(event.target)
    ) {
      closeSettingsMenu();
    }
  });
  document.addEventListener("focusin", updateViewportMetrics);
  document.addEventListener("focusout", () => {
    window.setTimeout(updateViewportMetrics, 120);
  });

  renderShortcutBar();
  applyUiScale(uiScale, false);
  applyTerminalFontSize(terminalFontSize, false);
  installTerminalScrollHandlers();
  installTabStripScrollHandlers();
  installShortcutBarScrollHandlers();
  installMobileTextInputGuards();
  if (mobileComposerMode) {
    setComposerActive(false);
    openComposer(false);
  }
  updateViewportMetrics();
  loadServerConfig().finally(() => {
    if (!serverConfig.requireToken) {
      loginOverlay.classList.add("hidden");
    }
    connect();
  });
})();
