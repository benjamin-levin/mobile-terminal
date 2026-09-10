// Real browser + backend + private tmux regressions. No live user data is used.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const net = require("node:net");
const { spawn, execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const checkout = path.resolve(__dirname, "../..");
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(probe, description, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await probe();
    if (result) return result;
    await pause(40);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function startFixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "mt-browser-"));
  await fs.chmod(root, 0o700);
  const app = path.join(root, "app");
  const home = path.join(root, "home");
  const tmuxRoot = path.join(root, "tmux");
  await Promise.all([app, home, tmuxRoot].map((directory) => fs.mkdir(directory)));
  const environment = {
    PATH: process.env.PATH,
    HOME: home,
    TMPDIR: root,
    TMUX_TMPDIR: tmuxRoot,
    SHELL: "/bin/bash",
    TERM: "xterm-256color",
    PYTHONUNBUFFERED: "1",
    PYTHONDONTWRITEBYTECODE: "1",
  };
  const socket = path.join(tmuxRoot, `tmux-${process.getuid()}`, "default");
  const tmux = (...args) => execFileSync("tmux", ["-S", socket, ...args], {
    env: environment, encoding: "utf8", timeout: 5000,
  }).trimEnd();
  let server;
  let browser;
  t.after(async () => {
    await browser?.close();
    if (server && server.exitCode === null) {
      const exited = new Promise((resolve) => server.once("exit", resolve));
      server.kill("SIGTERM");
      await Promise.race([exited, pause(2000)]);
      if (server.exitCode === null) server.kill("SIGKILL");
    }
    try { tmux("kill-server"); } catch { /* No server if startup failed. */ }
    await fs.rm(root, { recursive: true, force: true });
  });
  for (const file of await fs.readdir(checkout)) {
    if (file.endsWith(".py")) await fs.copyFile(path.join(checkout, file), path.join(app, file));
  }
  await fs.cp(path.join(checkout, "static"), path.join(app, "static"), { recursive: true });
  await fs.symlink(path.join(checkout, "node_modules"), path.join(app, "node_modules"), "dir");
  await fs.writeFile(path.join(app, "mobile-terminal-settings.json"), JSON.stringify({
    authentication: { mode: "off", idleMinutes: 15 }, uiScale: 0.85, terminalFontSize: 10,
  }));
  // Expose real functions only in the disposable copy; never ship a test API.
  const clientPath = path.join(app, "static/app.js");
  const source = await fs.readFile(clientPath, "utf8");
  const end = source.lastIndexOf("})();");
  assert.ok(end > 0);
  await fs.writeFile(clientPath, source.slice(0, end) + `
  window.__testTerminal = {
    term, requestAuthoritativeSelection, copyTerminalSelectionAndDismiss,
    sendMessage, selectWordAt, switchSession,
    state: () => ({terminalAuthoritative, terminalSeedInFlight, terminalEpoch,
      activeSessionName, followOutput, desiredTerminalCols, desiredTerminalRows})
  };
` + source.slice(end));
  const reservation = net.createServer();
  await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  const url = `http://localhost:${port}`;
  let log = "";
  server = spawn(path.join(checkout, ".venv/bin/python"), [
    path.join(app, "server.py"), "--host", "127.0.0.1", "--port", String(port),
    "--no-token", "--cwd", home, "--shell", "/bin/bash",
  ], { env: environment, stdio: ["ignore", "pipe", "pipe"] });
  server.stdout.on("data", (data) => { log = (log + data).slice(-8000); });
  server.stderr.on("data", (data) => { log = (log + data).slice(-8000); });
  await until(async () => {
    if (server.exitCode !== null) throw new Error(`Fixture server exited: ${log}`);
    try { return (await fetch(`${url}/health`)).ok; } catch { return false; }
  }, "fixture HTTP server");
  browser = await chromium.launch({
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
    headless: true,
  });

  async function openPage({ mobile = true } = {}) {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 }, isMobile: mobile, hasTouch: mobile,
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
      protocol: "ctap2", transport: "internal", hasResidentKey: true,
      hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
    } });
    const errors = [];
    const messages = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("websocket", (ws) => {
      for (const [event, direction] of [["framesent", "out"], ["framereceived", "in"]]) {
        ws.on(event, ({ payload }) => {
          try {
            const message = JSON.parse(payload);
            if (!/auth|register|enroll|device/i.test(message.type)
                && !["terminal-output", "seed-data"].includes(message.type)) {
              messages.push({ ...message, direction, at: Date.now() });
            }
          } catch { /* Terminal bytes are not JSON. */ }
        });
      }
    });
    await page.goto(url);
    await page.waitForFunction(() => window.__testTerminal?.state().terminalAuthoritative);
    await pause(1700); // finish the finite viewport-settle sequence
    assert.deepEqual(errors, [], "browser startup errors");
    return { page, context, messages, errors };
  }
  return { root, home, tmux, url, openPage, log: () => log };
}

async function send(page, data) {
  assert.equal(await page.evaluate((data) => __testTerminal.sendMessage({ type: "input", data }), data), true);
}

async function selectLine(page, text) {
  await page.waitForFunction((text) => {
    const { term } = __testTerminal;
    for (let y = term.buffer.active.length - 1; y >= 0; y -= 1) {
      if (term.buffer.active.getLine(y).translateToString(true).startsWith(text)) {
        term.select(0, y, text.length);
        return term.getSelection() === text;
      }
    }
    return false;
  }, text);
}

test("browser regressions through a private backend and tmux", { timeout: 120000 }, async (t) => {
  const fixture = await startFixture(t);
  const { page, context, messages, errors } = await fixture.openPage();

  await t.test("localhost passkey registration works without an origin override", async () => {
    assert.equal(await page.locator("#loginOverlay").isVisible(), false);
    assert.ok(await page.evaluate(() => __testTerminal.state().activeSessionName));
  });

  await t.test("fresh output and new scrollback copy successfully on the first attempt", async () => {
    await send(page, "printf 'FRESH OUTPUT\\n'\r");
    await selectLine(page, "FRESH OUTPUT");
    const fresh = await page.evaluate(() => __testTerminal.requestAuthoritativeSelection());
    assert.equal(fresh.text, "FRESH OUTPUT", JSON.stringify(fresh));
    await page.evaluate(() => __testTerminal.term.clearSelection());
    await send(page, "for i in $(seq 1 100); do printf 'NEW-ROW-%s\\n' \"$i\"; done\r");
    await selectLine(page, "NEW-ROW-100");
    const copied = await page.evaluate(() => __testTerminal.requestAuthoritativeSelection());
    assert.equal(copied.text?.trimEnd(), "NEW-ROW-100", JSON.stringify(copied));
  });

  await t.test("failed copy retains the selection", async () => {
    await selectLine(page, "NEW-ROW-100");
    const selected = await page.evaluate(() => __testTerminal.term.getSelection());
    await context.setOffline(true);
    await pause(100);
    await page.evaluate(() => __testTerminal.copyTerminalSelectionAndDismiss());
    assert.equal(await page.evaluate(() => __testTerminal.term.getSelection()), selected);
    await context.setOffline(false);
    await page.reload();
    await page.waitForFunction(() => window.__testTerminal?.state().terminalAuthoritative);
    await pause(1700);
  });

  await t.test("soft wraps copy exactly before and after a history reseed", async () => {
    const text = "WRAP " + "a".repeat(140) + " END";
    await send(page, `printf '${text}\\n'\r`);
    const select = async () => page.waitForFunction((text) => {
      const t = __testTerminal.term;
      for (let y = t.buffer.active.length - 1; y >= 0; y -= 1) {
        if (t.buffer.active.getLine(y).translateToString(true).startsWith("WRAP ")) {
          t.select(0, y, text.length);
          return true;
        }
      }
      return false;
    }, text);
    await select();
    assert.equal((await page.evaluate(() => __testTerminal.requestAuthoritativeSelection())).text, text);
    const epoch = await page.evaluate(() => __testTerminal.state().terminalEpoch);
    await page.evaluate(() => __testTerminal.sendMessage({ type: "history-reseed", historyLines: 2000 }));
    await page.waitForFunction((epoch) => __testTerminal.state().terminalEpoch > epoch && __testTerminal.state().terminalAuthoritative, epoch);
    await select();
    assert.equal((await page.evaluate(() => __testTerminal.requestAuthoritativeSelection())).text, text);
    await page.evaluate(() => __testTerminal.term.clearSelection());
  });

  await t.test("word hit testing after a wide character selects the complete word", async () => {
    await send(page, "printf '界 hello world\\n'\r");
    await page.waitForFunction(() => {
      const b = __testTerminal.term.buffer.active;
      return Array.from({ length: b.length }, (_, y) => b.getLine(y).translateToString(true)).some((line) => line.startsWith("界 hello world"));
    });
    const selection = await page.evaluate(() => {
      const t = __testTerminal.term;
      t.scrollToBottom();
      for (let y = t.buffer.active.length - 1; y >= t.buffer.active.viewportY; y -= 1) {
        if (t.buffer.active.getLine(y).translateToString(true).startsWith("界 hello world")) {
          const rect = document.querySelector(".xterm-screen").getBoundingClientRect();
          const cell = t._core._renderService.dimensions.css.cell;
          __testTerminal.selectWordAt(rect.left + 3.5 * cell.width, rect.top + (y - t.buffer.active.viewportY + 0.5) * cell.height);
          return t.getSelection();
        }
      }
      return null;
    });
    assert.equal(selection, "hello");
    await page.evaluate(() => __testTerminal.term.clearSelection());
  });

  await t.test("paste button preserves whitespace with keyboard closed and open", async () => {
    const text = "a  b\n  c";
    for (const focused of [false, true]) {
      await page.locator("#clearComposerButton").click();
      await pause(250);
      if (focused) await page.locator("#composerInput").focus();
      else await page.locator("#composerInput").blur();
      await page.evaluate((text) => navigator.clipboard.writeText(text), text);
      await page.getByRole("button", { name: "📋", exact: true }).tap();
      await until(async () => await page.locator("#composerInput").inputValue() === text, "exact pasted composer text");
    }
    await page.locator("#clearComposerButton").click();
    await page.locator("#composerInput").blur();
  });

  await t.test("rapid Settings then Display taps both activate", async () => {
    await page.locator("#settingsButton").tap();
    const start = Date.now();
    await page.locator("#displayButton").tap();
    assert.ok(Date.now() - start < 300, "probe must tap within the former global cancellation window");
    assert.equal(await page.locator("#displayOverlay").isVisible(), true);
    await page.locator("#saveDisplayButton").click();
  });

  await t.test("narrow height keeps browser and tmux geometry equal", async () => {
    await page.setViewportSize({ width: 390, height: 160 });
    await pause(1800);
    const state = await page.evaluate(() => ({ ...__testTerminal.state(), cols: __testTerminal.term.cols, rows: __testTerminal.term.rows }));
    assert.equal(fixture.tmux("display-message", "-p", "-t", state.activeSessionName, "#{pane_width}x#{pane_height}"), `${state.cols}x${state.rows}`);
    assert.ok(state.rows >= 6);
    assert.equal(state.terminalAuthoritative, true);
    assert.equal(await page.locator("#terminalPanel").evaluate((element) => element.classList.contains("terminal-constrained")), true);
    await page.setViewportSize({ width: 390, height: 844 });
    await pause(1800);
    assert.equal(await page.locator("#terminalPanel").evaluate((element) => element.classList.contains("terminal-constrained")), false);
  });

  await t.test("ordinary reseeds preserve reading position and follow-output intent", async () => {
    const anchor = await page.evaluate(() => {
      const t = __testTerminal.term;
      t.scrollToLine(Math.max(1, t.buffer.active.baseY - 30));
      return t.buffer.active.getLine(t.buffer.active.viewportY).translateToString(true);
    });
    await page.waitForFunction(() => !__testTerminal.state().followOutput);
    const reseed = async () => {
      const epoch = await page.evaluate(() => __testTerminal.state().terminalEpoch);
      await page.evaluate(() => __testTerminal.sendMessage({ type: "history-reseed", historyLines: 2000, scrollTarget: null }));
      await page.waitForFunction((epoch) => __testTerminal.state().terminalEpoch > epoch && __testTerminal.state().terminalAuthoritative, epoch);
    };
    await reseed();
    assert.equal(await page.evaluate(() => __testTerminal.state().followOutput), false);
    assert.equal(await page.evaluate(() => {
      const t = __testTerminal.term;
      return t.buffer.active.getLine(t.buffer.active.viewportY).translateToString(true);
    }), anchor);
    await page.evaluate(() => __testTerminal.term.scrollToBottom());
    await page.waitForFunction(() => __testTerminal.state().followOutput);
    await reseed();
    assert.equal(await page.evaluate(() => __testTerminal.state().followOutput), true);
    assert.equal(await page.evaluate(() => {
      const b = __testTerminal.term.buffer.active;
      return b.viewportY === b.baseY;
    }), true);
  });

  await t.test("resize remains responsive during continuous repaint", async () => {
    const file = path.join(fixture.home, "repaint.py");
    await fs.writeFile(file, "import sys,time\nfor i in range(120):\n sys.stdout.write('\\rSPINNER %04d'%i);sys.stdout.flush();time.sleep(.04)\nprint()\n");
    await send(page, `python3 ${file}\r`);
    await page.waitForFunction(() => {
      const b = __testTerminal.term.buffer.active;
      return Array.from({ length: b.length }, (_, y) => b.getLine(y).translateToString(true)).some((line) => line.startsWith("SPINNER"));
    });
    const from = messages.length;
    await page.setViewportSize({ width: 430, height: 844 });
    const start = await until(() => messages.slice(from).find((m) => m.type === "seed-start" && m.reason === "resize"), "resize start");
    const opened = await until(() => messages.slice(from).find((m) => m.type === "seed-open" && m.epoch === start.epoch), "responsive resize", 2500);
    assert.ok(opened.at - start.at < 2000, `resize took ${opened.at - start.at}ms`);
    await pause(5000);
  });

  await t.test("file-editor image paste does not upload into the terminal", async () => {
    await fs.writeFile(path.join(fixture.home, "review.txt"), "Example\n");
    await page.locator("#auxButton").click();
    await page.locator("#auxFilesButton").click();
    await page.locator("#fileRootInput").fill(fixture.home);
    await page.locator("#fileRootForm button[type=submit]").click();
    await page.getByText("review.txt", { exact: true }).click();
    await page.locator("#fileEditorInput").fill("Saved through UI\n");
    await page.locator("#fileSaveButton").click();
    await until(async () => await fs.readFile(path.join(fixture.home, "review.txt"), "utf8") === "Saved through UI\n", "file save");
    const from = messages.length;
    await page.locator("#fileEditorInput").evaluate((element) => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array([1, 2, 3])], "test.png", { type: "image/png" }));
      element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    });
    await pause(150);
    assert.equal(messages.slice(from).some((m) => m.type === "upload-image"), false);
    assert.deepEqual(errors, []);
  });
});

test("desktop query ownership and reconnect paste negotiation preserve input", { timeout: 45000 }, async (t) => {
  const fixture = await startFixture(t);
  const { page, messages, errors } = await fixture.openPage({ mobile: false });
  const captured = path.join(fixture.home, "input.json");
  const program = path.join(fixture.home, "protocol.py");
  await fs.writeFile(program, `import os,sys,tty,termios,select,time,json
fd=sys.stdin.fileno()
before=termios.tcgetattr(fd)
tty.setraw(fd)
received=[]
try:
 sys.stdout.write('\\x1b[?2004h\\x1b[2J\\x1b[HProtocol ready\\x1b[6n')
 sys.stdout.flush()
 deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  if select.select([fd],[],[],.1)[0]:
   data=os.read(fd,4096).decode('utf8')
   received.append(data)
   if data=='!':
    sys.stdout.write('\\x1b[?2004h');sys.stdout.flush()
   with open(${JSON.stringify(captured)},'w') as f: json.dump(received,f)
finally:
 termios.tcsetattr(fd,termios.TCSANOW,before)
`);
  await send(page, `python3 ${program}\r`);
  await until(async () => { try { return (await fs.readFile(captured, "utf8")).length > 0; } catch { return false; } }, "raw query response");
  await pause(150);
  const queryReplies = JSON.parse(await fs.readFile(captured, "utf8")).join("");
  assert.equal((queryReplies.match(/\x1b\[\d+;\d+R/g) || []).length, 1);
  assert.equal(await page.evaluate(() => __testTerminal.term.modes.bracketedPasteMode), true);
  await page.goto("about:blank");
  await pause(300); // no observer may infer that modes remained unchanged offline
  await page.goto(fixture.url);
  await page.waitForFunction(() => window.__testTerminal?.state().terminalAuthoritative);
  await pause(1700);
  const nativePaste = () => page.locator(".xterm-helper-textarea").evaluate((element) => {
    const data = new DataTransfer();
    data.setData("text/plain", "one\n  two");
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  if (!await page.evaluate(() => __testTerminal.term.modes.bracketedPasteMode)) {
    const from = messages.length;
    let reviewed = false;
    page.once("dialog", async (dialog) => { reviewed = true; await dialog.dismiss(); });
    await nativePaste();
    assert.equal(reviewed, true, "unknown multiline mode requires explicit review");
    assert.deepEqual(messages.slice(from).filter((m) => m.direction === "out" && m.type === "input"), []);
  }
  await send(page, "!"); // application reannounces its input mode
  await page.waitForFunction(() => __testTerminal.term.modes.bracketedPasteMode);
  const from = messages.length;
  await nativePaste();
  await until(async () => JSON.parse(await fs.readFile(captured, "utf8")).join("").includes("two"), "raw pasted bytes");
  const expected = "\x1b[200~one\n  two\x1b[201~";
  assert.ok(JSON.parse(await fs.readFile(captured, "utf8")).join("").endsWith(expected));
  assert.deepEqual(messages.slice(from).filter((m) => m.direction === "out" && m.type === "input").map((m) => m.data), [expected]);
  assert.deepEqual(errors, []);
});

test("mobile multiline draft survives tab switches and clears only after submission", { timeout: 45000 }, async (t) => {
  const fixture = await startFixture(t);
  const { page, messages, errors } = await fixture.openPage();
  const original = await page.evaluate(() => __testTerminal.state().activeSessionName);
  const captured = path.join(fixture.home, "mobile-input.json");
  const program = path.join(fixture.home, "mobile-input.py");
  await fs.writeFile(captured, "[]");
  await fs.writeFile(program, `import os,sys,tty,termios,select,time,json
fd=sys.stdin.fileno()
before=termios.tcgetattr(fd)
tty.setraw(fd)
received=[]
try:
 sys.stdout.write('\\x1b[?2004l\\x1b[2J\\x1b[HMobile input ready');sys.stdout.flush()
 deadline=time.monotonic()+40
 while time.monotonic()<deadline:
  if select.select([fd],[],[],.1)[0]:
   received.append(os.read(fd,4096).decode('utf8'))
   with open(${JSON.stringify(captured)},'w') as f: json.dump(received,f)
finally:
 termios.tcsetattr(fd,termios.TCSANOW,before)
`);
  await send(page, `python3 ${program}\r`);
  await page.waitForFunction(() => __testTerminal.term.buffer.active.getLine(0).translateToString(true).startsWith("Mobile input ready"));
  assert.equal(await page.evaluate(() => __testTerminal.term.modes.bracketedPasteMode), false);
  const text = "one  😀\n  two";
  await page.locator("#composerInput").evaluate((element, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, text);
  assert.equal(await page.locator("#composerInput").inputValue(), text);
  assert.equal(await page.locator("#composerInput").evaluate((element) => element.selectionEnd), text.length);
  assert.equal(await page.locator("#composerDraftStatus").isVisible(), true);
  assert.deepEqual(JSON.parse(await fs.readFile(captured, "utf8")), []);
  fixture.tmux("new-session", "-d", "-s", "other", "/bin/bash");
  const switchTo = async (session) => {
    await page.evaluate((session) => __testTerminal.switchSession(session), session);
    await page.waitForFunction((session) => __testTerminal.state().activeSessionName === session && __testTerminal.state().terminalAuthoritative, session);
    await pause(1700);
  };
  await switchTo("other");
  assert.equal(await page.locator("#composerDraftStatus").isVisible(), false);
  await switchTo(original);
  assert.equal(await page.locator("#composerInput").inputValue(), text);
  assert.equal(await page.locator("#composerInput").evaluate((element) => element.selectionEnd), text.length, "tab restore preserves the draft cursor");
  assert.equal(await page.locator("#composerDraftStatus").isVisible(), true);
  let reviews = 0;
  page.once("dialog", async (dialog) => { reviews += 1; await dialog.dismiss(); });
  await page.getByRole("button", { name: "↩️", exact: true }).tap();
  assert.equal(reviews, 1);
  assert.equal(await page.locator("#composerInput").inputValue(), text);
  assert.deepEqual(JSON.parse(await fs.readFile(captured, "utf8")), []);
  const from = messages.length;
  page.once("dialog", async (dialog) => { reviews += 1; await dialog.accept(); });
  await page.getByRole("button", { name: "↩️", exact: true }).tap();
  await until(() => messages.slice(from).some((m) => m.type === "composer-submitted"), "staged submission acknowledgment");
  assert.equal(reviews, 2);
  assert.equal(JSON.parse(await fs.readFile(captured, "utf8")).join(""), text + "\r");
  assert.equal(await page.locator("#composerInput").inputValue(), "");
  assert.equal(await page.locator("#composerDraftStatus").isVisible(), false);
  const submits = messages.slice(from).filter((m) => m.direction === "out" && m.type === "composer-enter");
  assert.equal(submits.length, 1);
  assert.equal(submits[0].session, original);
  assert.equal(submits[0].value, text);
  assert.deepEqual(errors, []);
});
