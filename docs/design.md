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

Run one credit-matrix catalog through deterministic checks, then benchmark model findings against planted errors.
