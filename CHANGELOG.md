# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `veriglot check` compares target catalogs (Lingui `.po`, flat JSON) with their source catalog and reports missing, extra and untranslated keys, placeholder, plural and select, tag, whitespace and ICU syntax problems, as text, JSON or a review CSV.
- `veriglot apply` writes accepted and edited CSV decisions into a `.po` catalog and accounts for every row; stale, unknown, invalid and vendored rows are rejected, never applied.
- `veriglot check` reports entries marked `#, fuzzy` (info), a plural branch that drops `#` or a placeholder its source branch has (warning), and per-branch tag differences; `veriglot apply` drops the `fuzzy` flag from entries it writes.
- `veriglot review SOURCE TARGET...` serves a local web page on 127.0.0.1 to filter findings, edit, accept or reject them and save through the same apply code as `veriglot apply`, with CSV export and import.
- Python development scaffold and [review design](docs/design.md), including the planned CSV and local web review interfaces.

### Fixed

- `veriglot apply` no longer writes into the previous entry when an earlier `msgstr` holds U+2028, U+2029, form feed or another character Python treats as a line break; it now checks the msgid of every block it replaces.
- Catalogs with a UTF-8 byte order mark are read correctly (the header no longer swallows the first entry), and malformed `.po` lines such as unterminated strings exit with code 3 and the line number instead of being read silently.
- Non-integer `=N` keys (`=abc`, `=inf`, `=1.5`), plural or select arguments without branches or without `other`, and unknown argument types are syntax findings instead of crashes or passes.
- Targets with more or fewer plural categories than the source no longer get false tag errors; `offset:` is taken into account when checking category coverage; a whitespace-only translation is reported as missing.
- `veriglot apply` skips rows for other locales and rows an earlier row resolved, names unknown kinds, proposes whitespace fixes only when they pass the checks, writes the catalog atomically and reports every row as `catalog changed` if the file changed meanwhile. Decisions read from stdin may carry a byte order mark.
- CSV text cells that a spreadsheet would run as a formula are written with a leading `'`, which `apply` strips.

[Unreleased]: https://github.com/simonvanlierde/veriglot/commits/main
