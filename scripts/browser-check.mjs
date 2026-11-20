// Smoke test and axe scan for the review page. Run inside the Playwright image:
//   node scripts/browser-check.mjs <url-with-token> <screenshot.png>
// The catalogs sit next to the screenshot (nl/messages.po), so the script can change them
// the way an editor would while the page is open. Needs `playwright` and `@axe-core/playwright`.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";

const [url, shot] = process.argv.slice(2);
const nl = `${dirname(shot)}/nl/messages.po`;
const browser = await chromium.launch();
const page = await (await browser.newContext({ acceptDownloads: true })).newPage();
await page.goto(url);
await page.waitForSelector("#list button");

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : ` ${detail}`}`);
  if (!ok) failures += 1;
};
const axe = async (label) => {
  const { violations } = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
  console.log(`axe ${label}: ${violations.length} violations`);
  for (const v of violations) console.log(`  ${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  failures += violations.length;
};
const edit = (from, to) => writeFileSync(nl, readFileSync(nl, "utf8").replace(from, to));
const rows = () => page.locator("#list button");
const pick = async (text) => { await page.fill("#f-text", text); await rows().first().focus(); };
const finding = (id) => page.evaluate(async (want) => {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  const data = await (await fetch("/api/findings", { headers: { "X-Veriglot-Token": token } })).json();
  return data.findings.find((f) => f.id === want);
}, id);
const counter = () => page.textContent("#counter");

await axe("list");
await page.emulateMedia({ colorScheme: "dark" });
await axe("list, dark");
await page.emulateMedia({ colorScheme: "light" });

// A page that cannot load shows the failure as an error.
const broken = await page.context().newPage();
await broken.goto(url.replace(/token=[^&]+/, "token=wrong"));
await broken.waitForFunction(() => document.getElementById("message")?.textContent);
check("load failure is marked as an error", await broken.evaluate(() => document.getElementById("message").classList.contains("error")));
await broken.close();

// Moving through the list does not rewrite the live regions, so screen readers stay quiet.
await rows().first().focus();
const writes = await page.evaluate(async () => {
  let n = 0;
  const seen = new MutationObserver((m) => { n += m.length; });
  for (const id of ["shown", "counter"]) seen.observe(document.getElementById(id), { childList: true, characterData: true, subtree: true });
  document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  seen.disconnect();
  return n;
});
check("moving does not rewrite live regions", writes === 0, `${writes} writes`);

// The focus ring of a list row is drawn inside the scrolling list, where it cannot be clipped.
const ring = await page.evaluate(() => getComputedStyle(document.activeElement).outlineOffset);
check("list row focus ring sits inside the list", Number.parseFloat(ring) < 0, ring);
await page.keyboard.press("k");

// Filters.
const total = await rows().count();
await page.selectOption("#f-kind", "untranslated");
check("filter by kind", (await rows().count()) < total);
await page.selectOption("#f-kind", "");
await pick("cancel");
check("search", (await rows().count()) === 1);

// Enter in a field submits the filter form unless a handler cancels it (inline handlers are
// blocked by the CSP); a real submit would reload the page and lose the token.
const submitted = await page.evaluate(() => document.getElementById("filters").dispatchEvent(new Event("submit", { cancelable: true })));
check("filter form submit is cancelled", !submitted);

// Invisible format characters are marked.
await pick("bidiTest");
const shown = await page.locator("#detail-body pre").nth(1).textContent();
check("bidi control shown as U+202E", shown.includes("‹U+202E›"), JSON.stringify(shown));

// A decision changed while a save is in flight survives it.
await pick("cancel");
await page.keyboard.press("r");
await page.route("**/api/decisions", async (route) => { await new Promise((r) => setTimeout(r, 600)); await route.continue(); });
await page.click("#save");
await page.waitForTimeout(150);
await page.keyboard.press("e");
await page.waitForSelector("#results li");
await page.unroute("**/api/decisions");
check("decision changed during save is kept", (await counter()).startsWith("1 decided"), await counter());
await page.keyboard.press("c");

// Stale save: the file changes under an open decision.
await pick("labelSeparator");
const before = await finding("nl:output.labelSeparator:whitespace");
await page.keyboard.press("e");
edit('msgid "output.labelSeparator"\nmsgstr ":"', 'msgid "output.labelSeparator"\nmsgstr ";"');
await page.click("#save");
await page.waitForFunction(() => document.querySelector("#results")?.textContent.includes("stale"));
const after = await finding("nl:output.labelSeparator:whitespace");
check("changed text is rejected as stale", after.fingerprint !== before.fingerprint);
const [download] = await Promise.all([page.waitForEvent("download"), page.click("#export")]);
const csv = readFileSync(await download.path(), "utf8");
const exported = csv.slice(csv.indexOf('"nl:output.labelSeparator:whitespace"')).match(/"([0-9a-f]{16})"/)?.[1];
check("export keeps the decision-time fingerprint", exported === before.fingerprint, `${exported} vs ${before.fingerprint}`);
await rows().first().focus();
await page.keyboard.press("e");
await page.click("#save");
await page.waitForFunction(() => document.querySelector("#message")?.textContent.startsWith("Saved"), null, { timeout: 5000 }).catch(() => {});
const retried = await page.textContent("#results");
check("deciding again after a reload uses the current fingerprint", retried.includes("applied") && !retried.includes("stale"), retried);

// A decision whose finding disappears is dropped, with a note.
await pick("bidiTest");
await page.keyboard.press("e");
edit(/msgstr "Hallo\u202e x "/u, 'msgstr "Hallo"');
await page.click("#save");
await page.waitForFunction(() => document.querySelector("#message")?.textContent.includes("Saved") || document.querySelector("#message")?.textContent.includes("Nothing"));
await page.waitForTimeout(300);
check("decision on a vanished finding is dropped", (await counter()).startsWith("0 decided"), await counter());
check("drop is reported", (await page.textContent("#message")).includes("dropped"), await page.textContent("#message"));

// Wide screens: source, translation and proposed sit side by side.
await pick("normsPosition");
const tops = await page.locator("#detail-body pre").evaluateAll((pres) => pres.map((p) => p.getBoundingClientRect().top));
check("texts side by side on a wide screen", tops.length === 3 && new Set(tops).size === 1, JSON.stringify(tops));

// A key the target lacks: Edit starts from the source, selected, and saving adds the entry.
await pick("stepContributors");
await page.keyboard.press("e");
const start = await page.evaluate(() => { const a = document.getElementById("edited"); return a.value.slice(a.selectionStart, a.selectionEnd); });
check("edit on a missing entry starts from the selected source", start === "Contributors", JSON.stringify(start));
check("unsaved changes are marked", await page.evaluate(() => document.getElementById("counter").classList.contains("dirty")));
await page.keyboard.type("Bijdragers");
await page.click("#save");
// The message of the previous save is still shown, so wait for this row's outcome.
await page.waitForFunction(() => document.querySelector("#results")?.textContent.includes("stepContributors"));
const inserted = (await page.textContent("#message")).includes("1 applied") && readFileSync(nl, "utf8").includes('msgid "stepContributors"\nmsgstr "Bijdragers"\n');
check("saving an edit on a missing entry adds it to the catalog", inserted, await page.textContent("#results"));

// Phones: picking a finding scrolls its detail, which sits below the list, into view.
await page.setViewportSize({ width: 390, height: 844 });
await page.fill("#f-text", "");
await rows().last().click();
const top = await page.evaluate(() => document.getElementById("detail").getBoundingClientRect().top);
check("picking on a phone shows the detail", top >= 0 && top < 844 / 3, `detail top ${top}`);
await axe("phone");
await page.emulateMedia({ colorScheme: "dark" });
await axe("phone, dark");
await page.emulateMedia({ colorScheme: "light" });
await page.setViewportSize({ width: 1280, height: 720 });

// Main flow: edit one finding by keyboard and save.
await pick("cancel");
await page.keyboard.press("e");
await axe("edit open");
await page.fill("#edited", "Annuleren");
await page.screenshot({ path: shot, fullPage: true });
await page.click("#save");
await page.waitForFunction(() => document.querySelector("#message")?.textContent.startsWith("Saved"));
const message = await page.textContent("#message");
const results = await page.locator("#results li").allTextContents();
console.log(`message: ${message}`);
console.log(`outcome: ${results.join(" | ")}`);
check("save applies the edit", message.includes("1 applied") && results.some((r) => r.includes("applied")));
await axe("after save");
console.log(failures === 0 ? "smoke: PASS" : `smoke: FAIL (${failures})`);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
