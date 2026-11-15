// Smoke test and axe scan for the review page. Run inside the Playwright image:
//   node scripts/browser-check.mjs <url-with-token> [screenshot.png]
// Needs `playwright` and `@axe-core/playwright` resolvable (NODE_PATH).
import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";

const [url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await (await browser.newContext()).newPage();
await page.goto(url);
await page.waitForSelector("#list button");

const axe = async (label) => {
  const { violations } = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
  console.log(`axe ${label}: ${violations.length} violations`);
  for (const v of violations) console.log(`  ${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  return violations.length;
};

let failures = await axe("list");
await page.emulateMedia({ colorScheme: "dark" });
failures += await axe("list, dark");
await page.emulateMedia({ colorScheme: "light" });
const total = await page.locator("#list button").count();
await page.selectOption("#f-kind", "untranslated");
const shown = await page.locator("#list button").count();
console.log(`filter kind=untranslated: ${shown} of ${total} rows`);
await page.fill("#f-text", "cancel");
console.log(`search cancel: ${await page.locator("#list button").count()} rows`);
await page.locator("#list button").first().focus();
await page.keyboard.press("e");
failures += await axe("edit open");
await page.fill("#edited", "Annuleren");
await page.screenshot({ path: shot, fullPage: true });
await page.click("#save");
await page.waitForSelector("#results li");
const message = await page.textContent("#message");
const results = await page.locator("#results li").allTextContents();
console.log(`message: ${message}`);
console.log(`outcome: ${results.join(" | ")}`);
const ok = message.includes("1 applied") && results.some((r) => r.includes("applied"));
failures += await axe("after save");
console.log(ok ? "smoke: PASS" : "smoke: FAIL");
await browser.close();
process.exit(ok && failures === 0 ? 0 : 1);
