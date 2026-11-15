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

Start with a local review list: filter by locale and error kind, inspect source,
translation and context, edit wording, accept or reject, then save the decisions.
Provide keyboard access, visible focus, labelled controls and an unsaved-changes warning.

The review UI needs no accounts, hosted database or collaborative workflow initially.
Choose its frontend and transport when implementing it; the Python review and apply
modules must be usable without a browser. CSV is an alternative interface, not the
web UI's internal data model.

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
  error, and treats a row whose text is already in place as skipped. If the file changed
  before the write, every row is rejected as `catalog changed`.
- `csvio` maps findings and decisions to the CSV columns
  `id,locale,key,kind,severity,source,target,context,message,proposed,fingerprint,decision,edited,note`.
  Text cells a spreadsheet would run as a formula (leading `=`, `+`, `-`, `@`, tab or
  carriage return, possibly after apostrophes) get one leading `'`, stripped again on
  read. Sensebank's review CSV uses the same rule; it is part of the shared contract.

Flat JSON catalogs are check-only. The per-locale review-status sidecar and model
findings are not built yet: the next step is to benchmark model findings against
planted errors.
