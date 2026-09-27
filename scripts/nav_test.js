// Back-navigation smoke test (Playwright, global install via NODE_PATH, so CommonJS).
// Needs the test server on 127.0.0.1:8781 with its own C4AI_DATA_DIR and a
// pairing code from `pair --no-qr --root ~/work --no-passkey` (same data dir):
//   NODE_PATH=$HOME/.npm-global/lib/node_modules node scripts/nav_test.js <code>
// ‹ undoes the last step: checks ‹, the edge swipe (history.back), Tabs, lists,
// a deleted previous session, reload and a fresh start. Sessions are fakes
// injected in the page; nothing reaches claude.
const { chromium, devices } = require("playwright");
const path = require("path");
const CODE = process.argv[2];
let fails = 0;
const ok = (c, m) => { console.log((c ? "PASS " : "FAIL ") + m); if (!c) fails++; };

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ ...devices["iPhone 13"] });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => ok(false, "pageerror " + e.message));
  await p.goto("about:blank");
  await p.goto("http://127.0.0.1:8781/#login=" + CODE);
  await p.waitForSelector("#topbar .menu", { timeout: 15000 });
  const P = path.resolve(__dirname, "..");   // this repo: a project inside ~/work
  await p.evaluate((P) => {
    for (const sid of ["fakeA", "fakeB", "fakeC"])
      S.sessions.push({ sid, title: sid, cwd: P, project: "clicker4ai", state: "detached" });
  }, P);
  const st = async () => { await p.waitForTimeout(350); return p.evaluate(() => location.hash + " d=" + S.depth); };
  const run = (js, a) => p.evaluate(js, a);
  const expect = async (want, m) => { const got = await st(); ok(got === want, m + " → " + got); };

  const PH = `#/project?${encodeURIComponent(P)}`;
  const inject = () => run(() => { for (const sid of ["fakeA", "fakeB", "fakeC"])
    if (!S.sessions.some((s) => s.sid === sid)) S.sessions.push({ sid, title: sid, cwd: "", project: "", state: "detached" }); });

  // 1. project → session → ‹ → project → ‹ → session; swipe the same
  await run(() => nav({ name: "projects" }));
  await expect("#/projects d=1", "Home → Projects");
  await run((P) => nav({ name: "project", path: P }), P);
  await expect(PH + " d=1", "Projects → project (only one kept under)");
  await run(() => openChat("fakeA"));
  await expect("#/chat/fakeA d=1", "project → A");
  ok(await run(() => { const k = [...document.querySelector("#topbar").children];
    return k[0].classList.contains("menu") && k[1].textContent === "‹"; }), "❯ first, ‹ second");
  await run(() => goBack());
  await expect(PH + " d=1", "‹ from A → project");
  await run(() => goBack());
  await expect("#/chat/fakeA d=1", "‹ again → A");
  await run(() => history.back());
  await expect(PH + " d=1", "swipe from A → project");
  await run(() => history.back());
  await expect("#/chat/fakeA d=1", "swipe again → A");

  // 2. chat ↔ True View is one screen
  await run(() => nav({ name: "term", sid: "fakeA" }));
  await expect("#/term/fakeA d=1", "A → True View replaces");
  await run(() => openChat("fakeA"));
  await run(() => goBack());
  await expect(PH + " d=1", "‹ after TV round trip → project");
  await run(() => goBack());

  // 3. session → Tabs → other session → ‹ → Tabs → ‹ → other session
  await run(() => nav({ name: "tabs" }));
  await expect("#/tabs d=1", "A → Tabs");
  await run(() => openChat("fakeB"));
  await expect("#/chat/fakeB d=1", "Tabs → B");
  await run(() => goBack());
  await expect("#/tabs d=1", "‹ from B → Tabs");
  await run(() => goBack());
  await expect("#/chat/fakeB d=1", "‹ from Tabs → B");

  // 4. list: no ‹ icon, swipe returns to the session
  await run(() => document.querySelector("#topbar .menu").click());
  await p.click(".drawer .nav-item:has-text('Skills')");
  await expect("#/skills d=1", "B → menu → Skills");
  ok(await run(() => ![...document.querySelector("#topbar").children].some((b) => b.textContent === "‹")), "no ‹ on a list");
  await run(() => history.back());
  await expect("#/chat/fakeB d=1", "swipe on Skills → B");

  // 5. previous screen deleted → Sessions
  await run(() => openChat("fakeC"));
  await run(() => nav({ name: "tabs" }));
  await run(() => { S.sessions = S.sessions.filter((s) => s.sid !== "fakeC"); });
  await run(() => goBack());
  await expect("#/sessions d=1", "‹ into deleted C → Sessions");
  await inject();

  // 6. reload keeps the pair
  await run((P) => nav({ name: "project", path: P }), P);
  await run(() => openChat("fakeA"));
  await st();   // let the rewind finish before reloading
  await p.reload();
  await p.waitForSelector("#topbar .menu", { timeout: 15000 });
  await inject();
  await expect("#/chat/fakeA d=1", "after reload");
  await run(() => goBack());
  await expect(PH + " d=1", "‹ after reload → project");

  // 7. first screen of a fresh start: ‹ → Home, ‹ again → back
  const p2 = await ctx.newPage();
  p2.on("pageerror", (e) => ok(false, "pageerror(2) " + e.message));
  await p2.goto("http://127.0.0.1:8781/#/chat/fakeA");
  await p2.waitForSelector("#topbar .menu", { timeout: 15000 });
  await p2.evaluate(() => S.sessions.push({ sid: "fakeA", title: "fakeA", cwd: "", project: "", state: "detached" }));
  const st2 = async () => { await p2.waitForTimeout(350); return p2.evaluate(() => location.hash + " d=" + S.depth); };
  ok((await st2()) === "#/chat/fakeA d=0", "fresh start at A, d=0");
  await p2.evaluate(() => goBack());
  let g = await st2(); ok(g === "#/home d=1", "‹ with nothing behind → Home: " + g);
  await p2.evaluate(() => goBack());
  g = await st2(); ok(g === "#/chat/fakeA d=1", "‹ from Home → A: " + g);

    await b.close();
  console.log(fails ? `${fails} FAILED` : "ALL PASS");
  process.exit(fails ? 1 : 0);
})();
