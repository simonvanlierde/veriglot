# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `veriglot review-model --format jsonl` prints each finding as its entry is answered and a summary line last; a terminal shows `reviewed N/M` progress on stderr; a rerun with the same `--log` reuses the answers there (matched by a new `request_sha256` log field) and asks only for the rest.
- `veriglot kinds` lists the finding kinds, their severity and the command that reports them; `bench score --format json` prints the score cells.
- `src/veriglot/findings.schema.json` describes the `check` and `review-model` JSON and JSON Lines output.
- `veriglot review-model` reviews translations with a model on any chat-completions server and reports `meaning`, `omission`, `addition`, `terminology`, `register`, `grammar` and `untranslated` findings in the same type, CSV and `apply` step as `check`; calls are constrained, validated, retried once and logged with the prompt hash.
- `bench score --qe` tabulates a MetricX-24 quality-estimation baseline (`scripts/metricx_qe.py`) at matched flag rates.
- `veriglot bench make`, `run` and `score` build a planted-error benchmark, run a model in review and direct-judge modes, and report recall per error kind with Wilson intervals; results are in `docs/benchmark.md`.
- `veriglot check` compares target catalogs (Lingui `.po`, flat JSON) with their source catalog and reports missing, extra and untranslated keys, placeholder, plural and select, tag, whitespace and ICU syntax problems, as text, JSON or a review CSV.
- `veriglot apply` writes accepted and edited CSV decisions into a `.po` catalog and accounts for every row; stale, unknown, invalid and vendored rows are rejected, never applied.
- `veriglot check` reports entries marked `#, fuzzy` (info), a plural branch that drops `#` or a placeholder its source branch has (warning), and per-branch tag differences; `veriglot apply` drops the `fuzzy` flag from entries it writes.
- `veriglot review SOURCE TARGET...` serves a local web page on 127.0.0.1 to filter findings, edit, accept or reject them and save through the same apply code as `veriglot apply`, with CSV export and import.
- Python development scaffold and [review design](docs/design.md), including the planned CSV and local web review interfaces.

### Changed

- `bench make` and `bench run` print a JSON object with `schema_version` and `errors`: the counts per language and kind moved under `items`, next to the items `path`; the run manifest gains `run_dir`.

### Fixed

- The JSON printed when a command fails keeps the success shape (`findings: []` for `check` and `review-model`), and Ctrl-C with `--format json` or `jsonl` prints the object too.
- An unknown `bench run --modes` value, and `bench score` without runs or `--qe`, exit 2 as usage errors instead of 3.
- `veriglot apply` rejects a later row of a model kind on an entry an earlier row changed as stale, instead of skipping it as resolved.
- Model calls retry with a doubled `max_tokens` after a length stop, treat `http.client` errors as recorded failures, ignore proxy variables and redirects for loopback servers, and JSON-encode catalog text in the prompts; `--extra-body` must be a JSON object.
- `bench run` keeps answers as they arrive, retries failed items on rerun, refuses to mix runs and exits 3 on failures; `bench score` checks the items hash.
- `veriglot apply` no longer writes into the previous entry when an earlier `msgstr` holds U+2028, U+2029, form feed or another character Python treats as a line break; it now checks the msgid of every block it replaces.
- Catalogs with a UTF-8 byte order mark are read correctly (the header no longer swallows the first entry), and malformed `.po` lines such as unterminated strings exit with code 3 and the line number instead of being read silently.
- Non-integer `=N` keys (`=abc`, `=inf`, `=1.5`), plural or select arguments without branches or without `other`, and unknown argument types are syntax findings instead of crashes or passes.
- Targets with more or fewer plural categories than the source no longer get false tag errors; `offset:` is taken into account when checking category coverage; a whitespace-only translation is reported as missing.
- `veriglot apply` skips rows for other locales and rows an earlier row resolved, names unknown kinds, proposes whitespace fixes only when they pass the checks, writes the catalog atomically and reports every row as `catalog changed` if the file changed meanwhile. Decisions read from stdin may carry a byte order mark.
- CSV text cells that a spreadsheet would run as a formula are written with a leading `'`, which `apply` strips.

[Unreleased]: https://github.com/simonvanlierde/veriglot/commits/main
