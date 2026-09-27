// Keyboard shortcuts smoke test (Playwright, global install via NODE_PATH, so CommonJS).
// Needs the test server on 127.0.0.1:8781 with its own C4AI_DATA_DIR and a
// pairing code from `pair --no-qr --root ~/work --no-passkey` (same data dir):
//   NODE_PATH=$HOME/.npm-global/lib/node_modules node scripts/kbd_test.js <code>
// Approval cards are injected into a fake chat in the page; nothing reaches claude.
const { chromium } = require("playwright");
const CODE = process.argv[2];
let fails = 0;
const ok = (c, m) => { console.log((c ? "PASS " : "FAIL ") + m); if (!c) fails++; };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1100, height: 800 } });
  p.on("pageerror", (e) => ok(false, "pageerror " + e.message));
  await p.goto("about:blank");
  await p.goto("http://127.0.0.1:8781/#login=" + CODE);
  await p.waitForSelector(".vs-row", { timeout: 15000 });
  const active = () => p.evaluate(() => { const a = document.activeElement; return a ? a.className + "|" + (a.dataset.kbd || "") : ""; });
  const hash = () => p.evaluate(() => location.hash);

  await p.keyboard.press("j");
  const a1 = await active();
  ok(/vs-row|s-card|btn/.test(a1), "j focuses first item: " + a1);
  await p.keyboard.press("j");
  const a2 = await active();
  ok(a2 !== a1, "second j moves on: " + a2);
  await p.keyboard.press("k");
  ok((await active()) === a1, "k moves back");

  await p.keyboard.press("?");
  ok(await p.evaluate(() => document.querySelector("#sheet-root").textContent.includes("Keyboard shortcuts")), "? opens help");
  await p.keyboard.press("Escape");
  ok(await p.evaluate(() => !document.querySelector("#sheet-root").children.length), "Esc closes help");

  await p.keyboard.press("/");
  await p.waitForTimeout(150);
  ok(await p.evaluate(() => !!document.querySelector(".qo-panel") && document.activeElement.type === "search"), "/ on home opens quick-open with focus");
  await p.keyboard.press("ArrowDown");
  ok((await active()).includes("vs-row"), "↓ from search goes to list");
  await p.keyboard.press("Escape");

  await p.keyboard.press("g"); await p.keyboard.press("p");
  await p.waitForSelector(".search-input");
  ok((await hash()) === "#/projects", "g p → projects");
  await p.keyboard.press("/");
  ok(await p.evaluate(() => document.activeElement.classList.contains("search-input")), "/ focuses search");
  await p.keyboard.type("jk");   // typing must not navigate
  ok((await p.evaluate(() => document.activeElement.value)) === "jk", "keys type into the search box");
  await p.keyboard.press("Escape");
  ok(await p.evaluate(() => document.activeElement === document.body), "Esc leaves the field");
  await p.keyboard.press("Backspace");
  await p.waitForTimeout(300);
  ok(["#/home", ""].includes(await hash()) && !!(await p.$(".vs-row")), "Backspace goes back to home: " + (await hash()));
  await p.keyboard.press("g"); await p.keyboard.press("s");
  ok((await hash()) === "#/sessions", "g s → sessions");
  await p.keyboard.press("g"); await p.keyboard.press("t");
  ok((await hash()) === "#/tabs", "g t → tabs");
  await p.keyboard.press("g"); await p.keyboard.press("h");
  ok((await hash()) === "#/home", "g h → home");

  // drawer: arrows walk its items
  await p.click(".tb-btn.menu");
  await p.keyboard.press("ArrowDown");
  ok((await active()).includes("nav-item"), "↓ in drawer focuses nav item");
  await p.keyboard.press("Escape");

  // approval cards on a fake chat (events injected, nothing sent to claude)
  await p.evaluate(() => nav({ name: "chat", sid: "kbdtest" }));
  await p.waitForSelector("#chat-input");
  const sent = [];
  await p.exposeFunction("logSend", (m) => sent.push(m));
  await p.evaluate(() => { const orig = wsSend; window.wsSend = undefined;
    wsSend = (o) => { if (o.type === "permission") { logSend(JSON.stringify(o)); return true; } return orig(o); }; });
  const inject = (ev) => p.evaluate((ev) => { S.events.kbdtest = S.events.kbdtest || []; handleWS({ type: "event", session_id: "kbdtest", ev }); }, ev);

  // 1) textarea focused and just typed → no focus steal
  await p.focus("#chat-input");
  await p.keyboard.type("x"); await p.keyboard.press("Backspace");
  await inject({ kind: "permission", request_id: "r1", tool: "Bash", input: { command: "ls" } });
  await p.waitForTimeout(900);
  ok((await active()).includes("c-input"), "recent typing: focus stays in message box");
  await p.waitForTimeout(1500);
  ok((await active()).includes("primary"), "after 2 s idle: Allow takes focus");
  ok(await p.evaluate(() => /1/.test(document.querySelector(".action-card .k-hint").textContent)
    && getComputedStyle(document.querySelector(".k-hint")).display !== "none"), "hint visible with fine pointer");
  // a second card waiting behind the first
  await inject({ kind: "permission", request_id: "r2", tool: "Bash", input: { command: "pwd" } });
  await p.keyboard.press("Enter");
  ok(sent.length === 1 && JSON.parse(sent[0]).behavior === "allow" && JSON.parse(sent[0]).request_id === "r1", "⏎ allows r1");
  await p.keyboard.press("Enter");   // double ⏎ inside the grace period
  await p.keyboard.press("n");
  ok(sent.length === 1, "keys ignored during grace for r2");
  await p.waitForTimeout(800);
  ok((await active()).includes("primary"), "r2 Allow focused after grace");
  await p.keyboard.press("n");
  ok(sent.length === 2 && JSON.parse(sent[1]).behavior === "deny" && JSON.parse(sent[1]).request_id === "r2", "n denies r2");

  // question: digits pick the option
  await inject({ kind: "question", request_id: "q1", questions: [{ question: "Pick?", options: [{ label: "Alpha" }, { label: "Beta" }] }] });
  await p.waitForTimeout(800);
  await p.keyboard.press("2");
  ok(sent.length === 3 && JSON.parse(sent[2]).answers["Pick?"] === "Beta", "2 answers Beta");

  // plan: 2 = approve, ask each time
  await inject({ kind: "plan_approval", request_id: "p1", plan: "do it" });
  await p.waitForTimeout(800);
  await p.keyboard.press("2");
  ok(sent.length === 4 && JSON.parse(sent[3]).behavior === "allow", "plan 2 approves");

  // text in the box → no steal, digits type
  await p.focus("#chat-input");
  await p.keyboard.type("hello");
  await inject({ kind: "permission", request_id: "r3", tool: "Bash", input: { command: "ls" } });
  await p.waitForTimeout(3000);
  ok((await active()).includes("c-input"), "non-empty box keeps focus");
  await p.keyboard.press("1");
  ok(sent.length === 4 && (await p.evaluate(() => document.querySelector("#chat-input").value)) === "hello1", "1 types in the box");
  await p.keyboard.press("Escape");
  await p.keyboard.press("y");
  ok(sent.length === 5 && JSON.parse(sent[4]).request_id === "r3", "Esc then y allows r3");
  ok((await active()).includes("c-input"), "last card answered: focus back in the message box");
  await p.keyboard.press("Escape");
  await p.keyboard.press("/");
  ok((await active()).includes("c-input"), "/ in chat focuses message box");

  // iPad + trackpad: primary pointer coarse, any-pointer fine
  const fakePointer = (fine, anyFine) => p.evaluate(([fine, anyFine]) => {
    const orig = window._mm || (window._mm = window.matchMedia.bind(window));
    window.matchMedia = (q) => /any-pointer:\s*fine/.test(q) ? { matches: anyFine }
      : /pointer:\s*fine/.test(q) ? { matches: fine } : orig(q);
  }, [fine, anyFine]);

  // phone: an answered card leaves the focus alone (no on-screen keyboard pop)
  await p.keyboard.press("Escape");
  await fakePointer(false, false);
  await inject({ kind: "permission", request_id: "r4", tool: "Bash", input: { command: "ls" } });
  await p.waitForTimeout(800);
  await p.keyboard.press("y");
  ok(sent.length === 6 && !(await active()).includes("c-input"), "phone: answered card does not focus the message box");
  await p.evaluate(() => { window.matchMedia = window._mm; });

  // 4 = deny with note: the app's own sheet, never the browser's prompt()
  p.on("dialog", (d) => { ok(false, "native dialog: " + d.message()); d.dismiss(); });
  await inject({ kind: "permission", request_id: "r5", tool: "Bash", input: { command: "ls" } });
  await p.waitForTimeout(800);
  await p.keyboard.press("4");
  ok(await p.evaluate(() => document.activeElement === document.querySelector("#sheet-root input")
    && document.activeElement.value === ""), "4 opens the note sheet, field focused and empty");
  await p.keyboard.type("not now");
  await p.keyboard.press("Enter");
  ok(sent.length === 7 && JSON.parse(sent[6]).behavior === "deny" && JSON.parse(sent[6]).message === "not now",
    "note sheet ⏎ denies r5 with the note");
  ok(await p.evaluate(() => !document.querySelector("#sheet-root").children.length), "note sheet closed");

  // composer ⏎: sends with any fine pointer, new line on a phone; ⇧⏎ always a new line
  await p.evaluate(() => { const orig = wsSend;
    wsSend = (o) => { if (o.type === "send") { logSend(JSON.stringify(o)); return true; } return orig(o); }; });
  const box = () => p.evaluate(() => document.querySelector("#chat-input").value);
  const sendsBefore = sent.length;
  await p.evaluate(() => { document.querySelector("#chat-input").value = ""; });
  await p.focus("#chat-input");
  await fakePointer(false, true);   // iPad with a trackpad keyboard
  await p.keyboard.type("ab");
  await p.keyboard.press("Shift+Enter");
  ok((await box()) === "ab\n" && sent.length === sendsBefore, "iPad: ⇧⏎ is a new line");
  await p.keyboard.press("Enter");
  ok(sent.length === sendsBefore + 1 && JSON.parse(sent[sendsBefore]).text === "ab", "iPad: ⏎ sends");
  await p.evaluate(() => { document.querySelector("#chat-input").value = ""; });
  await fakePointer(false, false);  // phone
  await p.keyboard.type("cd");
  await p.keyboard.press("Enter");
  ok((await box()) === "cd\n" && sent.length === sendsBefore + 1, "phone: ⏎ is a new line");
  await p.evaluate(() => { document.querySelector("#chat-input").value = ""; window.matchMedia = window._mm; });

  // confirmation sheet (/clear): Confirm focused after the grace period,
  // one arrow press per button, ⏎ confirms
  await p.evaluate(() => { const orig = wsSend;
    wsSend = (o) => { if (o.type === "clear") { logSend(JSON.stringify(o)); return true; } return orig(o); }; });
  const clears = () => sent.filter((s) => JSON.parse(s).type === "clear").length;
  await p.focus("#chat-input");
  await p.evaluate(() => confirmClear("kbdtest"));
  await p.keyboard.press("Enter");   // the ⏎ that sent /clear, repeated
  ok(clears() === 0, "⏎ during grace does not confirm");
  await p.waitForTimeout(800);
  ok((await active()).includes("primary"), "Confirm focused after grace: " + await active());
  await p.keyboard.press("ArrowRight");
  ok(await p.evaluate(() => document.activeElement.textContent === "Cancel"), "one → reaches Cancel: " + await active());
  await p.keyboard.press("ArrowLeft");
  ok((await active()).includes("primary"), "one ← back to Confirm: " + await active());
  await p.keyboard.press("ArrowDown");
  ok(await p.evaluate(() => document.activeElement.textContent === "Cancel"), "one ↓ reaches Cancel");
  // Safari loses track of the focused button: keys go on from the last move
  const lose = () => p.evaluate(() => document.activeElement.blur());
  await lose();
  await p.keyboard.press("ArrowUp");
  ok((await active()).includes("primary"), "focus lost: one ↑ still back to Confirm");
  await lose();
  await p.keyboard.press("ArrowDown");
  ok(await p.evaluate(() => document.activeElement.textContent === "Cancel"), "focus lost: one ↓ still to Cancel");
  await p.keyboard.press("ArrowUp");
  await lose();
  await p.keyboard.press("Enter");
  ok(clears() === 1 && await p.evaluate(() => !document.querySelector("#sheet-root").children.length),
    "⏎ on Confirm clears and closes the sheet");

  // /compact from the palette asks first, like /clear
  await p.evaluate(() => { const orig = wsSend;
    wsSend = (o) => { if (o.type === "compact") { logSend(JSON.stringify(o)); return true; } return orig(o); }; });
  const compacts = () => sent.filter((s) => JSON.parse(s).type === "compact").length;
  await p.evaluate(() => palette("kbdtest"));
  await p.locator("#sheet-root .sheet-item", { hasText: "/compact" }).click();
  await p.waitForTimeout(300);
  ok(compacts() === 0 && await p.evaluate(() => document.querySelector("#sheet-root").textContent.includes("Compact conversation?")),
    "palette /compact opens a confirm sheet, sends nothing");
  await p.locator("#sheet-root button", { hasText: "Cancel" }).click();
  await p.waitForTimeout(300);
  ok(compacts() === 0, "Cancel does not compact");

  // an approval card with a long unbroken "Always" rule does not widen the chat
  await p.setViewportSize({ width: 390, height: 800 });
  await inject({ kind: "permission", request_id: "r9", tool: "Bash", input: { command: "ls" },
    suggestions: [{ type: "addRules", destination: "localSettings", behavior: "allow",
      rules: [{ toolName: "Bash", ruleContent: "python3 -c \"import json;m=json.load(open('data/sessions/e0a335f6ff/meta.json'));print(m.get('cwd'))\"" }] }] });
  await p.waitForTimeout(300);
  ok(await p.evaluate(() => { const m = chatUI.msgs; return m.scrollWidth <= m.clientWidth; }),
    "long rule wraps (scroll/client width: " + await p.evaluate(() => chatUI.msgs.scrollWidth + "/" + chatUI.msgs.clientWidth) + ")");
  await inject({ kind: "permission_resolved", request_id: "r9", behavior: "deny" });

  // /btw: typed in the box it asks and opens the side-question sheet;
  // answers arrive as "btw" events (injected here, nothing reaches claude)
  await p.evaluate(() => { const orig = wsSend;
    wsSend = (o) => { if (o.type.startsWith("btw")) { logSend(JSON.stringify(o)); return true; } return orig(o); }; });
  const btws = () => sent.map((s) => JSON.parse(s)).filter((o) => o.type.startsWith("btw"));
  await p.fill("#chat-input", "/btw what codeword?");
  await p.press("#chat-input", "Enter");
  await p.waitForTimeout(300);
  ok(btws().length === 1 && btws()[0].question === "what codeword?" && btws()[0].session_id === "kbdtest",
    "/btw sends the question: " + JSON.stringify(btws()));
  ok(await p.evaluate(() => $("#sheet-root").textContent.includes("Side question") && $("#chat-input").value === ""),
    "/btw opens the sheet and empties the box");
  await inject({ kind: "btw", items: [{ id: "a", q: "what codeword?", a: "", state: "asking", cost: null }] });
  ok(await p.evaluate(() => $(".btw-a.dim")?.textContent === "thinking…"
    && [...document.querySelectorAll("#sheet-root .btn")].find((b) => b.textContent === "Ask").disabled), "asking: thinking…, Ask disabled");
  await inject({ kind: "btw", items: [
    { id: "a", q: "what codeword?", a: "It is **BLUEFISH**.", state: "done", cost: 0.0045 },
    { id: "b", q: "and the second one, with a long unbroken word " + "x".repeat(90) + "?", a: "Not given yet.", state: "done", cost: 0.005 }] });
  ok(await p.evaluate(() => $(".btw-list").lastElementChild.querySelector(".btw-a")?.textContent === "Not given yet."),
    "newest answer at the bottom, in full");
  ok(await p.evaluate(() => $(".btw-list").firstElementChild.matches(".btw-old")
    && document.querySelectorAll(".btw-old").length === 1), "one earlier exchange above, folded");
  ok(await p.evaluate(() => { const b = $(".sheet-body"); return b.scrollTop + b.clientHeight >= b.scrollHeight - 1; }),
    "scrolled to the newest");
  await p.click(".btw-old");
  ok(await p.evaluate(() => $(".btw-list").firstElementChild.querySelector(".btw-a strong")?.textContent === "BLUEFISH"
    && $(".btw-list").lastElementChild.matches(".btw-old")), "tapping an earlier one opens it in place (markdown)");
  ok(await p.evaluate(() => { const s = $(".sheet"); return s.scrollWidth <= s.clientWidth; }), "long word wraps in the sheet");
  await p.screenshot({ path: ".scratch/btw-sheet.png" });
  await p.fill(".btw-input", "next one");
  await p.press(".btw-input", "Enter");
  ok(btws().length === 2 && btws()[1].question === "next one", "Enter in the sheet asks again");
  await p.locator("#sheet-root .btn", { hasText: "Clear" }).click();
  ok(btws().length === 3 && btws()[2].type === "btw_clear", "Clear sends btw_clear");
  await inject({ kind: "btw", items: [] });
  ok(await p.evaluate(() => $("#sheet-root").textContent.includes("Neither the question nor the answer")), "cleared: the intro shows");
  await p.evaluate(() => closeSheet());
  await p.setViewportSize({ width: 1100, height: 800 });

  // Back navigation (‹, ⌫, the edge swipe, Tabs) is scripts/nav_test.js.

  // mobile: no hints
  const m = await b.newPage({ ...require("playwright").devices["iPhone 13"] });
  await m.goto("http://127.0.0.1:8781/manifest.json");
  await m.setContent(`<link rel=stylesheet href="http://127.0.0.1:8781/style.css"><button class=btn>Allow<kbd class=k-hint>1</kbd></button>`, { waitUntil: "load" });
  ok(await m.evaluate(() => getComputedStyle(document.querySelector(".k-hint")).display === "none"), "hints hidden on phone");

  await b.close();
  console.log(fails ? `${fails} FAILED` : "ALL PASS");
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
