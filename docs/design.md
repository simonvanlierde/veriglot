# Review design

## Scope

UI catalogs: message keys, source and target text, locale, glossary and usage context.

Placeholder and plural checks, terminology checks and model-assisted review. Catalog adapters preserve significant whitespace and never change vendored translations.

## One review implementation

The review module produces findings with stable entry identifiers, source and target
text, locale, context, an error kind and a proposed correction when available.
Model calls and domain checks belong inside that module. The CLI, CSV export and
web UI consume its results rather than implementing checks themselves.

A reviewer can accept, reject or edit a proposal. Applying those decisions checks
the current input against the reviewed snapshot, validates the resulting changes
and returns an explicit account of applied and rejected rows. Unknown identifiers,
stale text and invalid proposals must never disappear silently or overwrite newer edits.
CSV and the web UI use this same apply implementation.

Keep these contracts as design requirements until the first working review establishes
the concrete types. Add adapters where two actual implementations need the same
interface, rather than introducing a general plugin framework.

## Simple web review

`veriglot review SOURCE TARGET...` serves a local review page. It lists findings, filters
them by locale, kind, severity and text, shows source, translation, context and proposed
text with edge spaces, line breaks and tabs made visible, and records accept, reject, edit
or clear per finding. Save sends the decisions to `apply_decisions`, the function the CLI
uses, and shows each row's outcome. There are no accounts, hosted database or collaborative
workflow. The review and apply modules work without the browser, and CSV is not the page's
data model: the page keeps decisions in memory and can export or import the CSV columns
through `csvio`.

Stack: the Python standard library `http.server` (`ThreadingHTTPServer`), one static HTML
page, one vanilla JavaScript file and one CSS file in `veriglot.web`. This is the smallest
thing that works: no runtime dependency, no npm build and no framework to keep patched, at
the price of hand-written DOM code. Catalog text goes in with `textContent`, never
`innerHTML`.

API, all JSON except the CSV export:

- `GET /api/findings`: `schema_version`, `locales`, `read_only`, `findings` and `errors` (a
  locale whose catalog cannot be read, without paths). The catalogs are read again on every request.
- `POST /api/decisions`: `{"rows": [...], "dry_run": false}` with the `DecisionRow` fields;
  returns `written` and one outcome per row, as `ApplyReport` does. Rows for a locale
  without a catalog are rejected as `unknown`, rows for an unreadable one as `unreadable`.
- `POST /api/export`: decisions (with the fingerprint from when each was made) in, the
  review CSV out; the exported fingerprint is that one, so re-importing cannot apply over newer text. `POST /api/import`: CSV text in,
  decision rows out.

Local-server safety: it binds 127.0.0.1 (a random free port unless `--port`), answers only
requests whose `Host` is `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), needs a
per-run random token in the `X-Veriglot-Token` header on every `/api` call, sends no CORS
headers, limits request bodies to 1 MiB and serves only its three static files with a
strict Content-Security-Policy. The URL carries the token in the fragment, which browsers
do not send. A stale fingerprint keeps a decision from overwriting newer text: the page
sends the fingerprint from when the decision was made, and apply rejects it as `stale`.

Accessibility: keyboard use (j/k or arrows, a/r/e/c, plus Tab and a roving tab stop in the
list), visible focus, labelled controls, an unsaved-changes warning and a polite live region
for results. See [accessibility](accessibility.md).

## Relationship to the sibling project

Veriglot reviews app UI catalogs. Sensebank reviews concept-based word lists and
group rules. They remain separate packages and repositories. Both may use the same
local model endpoint and reviewer decision columns. Establish the CSV contract with
real examples from both before treating it as stable.

Build Veriglot's first working review before implementing Sensebank. Extract a shared
package only after both implementations demonstrate common behavior. Model choice,
license, language coverage and memory requirements must be checked before use.

## First working example

`veriglot check` runs the deterministic checks over credit-matrix's Lingui catalogs
(`src/locales/<locale>/messages.po` and `src/core/credit-i18n/locales/<locale>/messages.po`),
whose msgids are keys and whose `en` msgstrs are the source text. The modules:

- `icu` parses the brace structure of ICU MessageFormat (arguments and their types,
  plural/select branches with `offset:`, `#`, apostrophe quoting) and leaves rich-text tags
  as literal text, since `<contrib>` in credit-matrix is a literal XML name a strict tag
  parser rejects. Each branch records its argument names, whether `#` occurs directly in
  it and its tags. Branching arguments need at least one branch and an `other` branch, and
  `=N` keys are integers, as in Lingui's parser. `#` inside a select nested in a plural is
  literal, as in ICU4J.
- `catalog` reads `.po` files with Babel (`abort_invalid`, plus a check for unterminated
  strings, which Babel accepts) and flat JSON files; a UTF-8 byte order mark is allowed
  and kept. It writes a changed `msgstr` in place, leaving every other byte as it was
  except a `fuzzy` flag on that entry. Lines are counted on `\n` only, as Babel counts
  them, and the writer checks that each located block holds the entry's msgid. It
  refuses to write when the file changed since it was read and replaces the file atomically.
- `review` holds the checks and the `Finding` type. Plural categories come from Babel's
  CLDR data; a category is required only if it selects an integer below 1000 that no
  `=N` branch covers (after subtracting `offset:`), so French `many` (millions) falls back
  to `other`. Tags outside branches are compared as a multiset; each target branch is
  compared with the source branch of the same key, else the source `other`, for tags
  (error), argument names and `#` (warning). A whitespace fix is proposed only when the
  fixed text passes the checks.
- `apply` takes `DecisionRow`s and returns an `ApplyReport` with one outcome per row.
  It skips rows for another locale and rows an earlier row resolved; it rejects unknown
  ids and kinds, stale fingerprints, vendored keys and text that fails the checks with an
  error, and treats a row whose text is already in place as skipped. A source key the
  target lacks is checked as an empty translation and appended as a new entry. If the file changed
  before the write, every row is rejected as `catalog changed`.
- `csvio` maps findings and decisions to the CSV columns
  `id,locale,key,kind,severity,source,target,context,message,proposed,fingerprint,decision,edited,note`.
  Text cells a spreadsheet would run as a formula (leading `=`, `+`, `-`, `@`, tab or
  carriage return, possibly after apostrophes) get one leading `'`, stripped again on
  read. Sensebank's review CSV uses the same rule; it is part of the shared contract.

Flat JSON catalogs are check-only. The per-locale review-status sidecar is not built yet.

- `model` asks a chat-completions server about one entry and returns `Finding`s of the model
  kinds (`meaning`, `omission`, `addition`, `terminology`, `register`, `grammar`, `untranslated`).
  Findings of one kind are merged so ids stay `<locale>:<key>:<kind>`, and a proposal is kept only
  if it differs from the target and passes the deterministic checks, so `apply` accepts it.
  Placeholder, plural, tag and syntax defects belong to the deterministic checks.
- `bench` plants errors in clean translations (seeded perturbations, plus hand-written ones for
  language-dependent kinds) and scores recall per kind for the review prompt and a direct-judge
  baseline, and tabulates a MetricX quality-estimation scorer at matched flag rates. Model scores
  are not a substitute for native review, and the benchmark does not record who wrote the translations
  it uses, so it cannot exclude a model judging its own output; see [the benchmark](benchmark.md).
