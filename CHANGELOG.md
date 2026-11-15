# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `veriglot check` compares target catalogs (Lingui `.po`, flat JSON) with their source catalog and reports missing, extra and untranslated keys, placeholder, plural and select, tag, whitespace and ICU syntax problems, as text, JSON or a review CSV.
- `veriglot apply` writes accepted and edited CSV decisions into a `.po` catalog and accounts for every row; stale, unknown, invalid and vendored rows are rejected, never applied.
- Python development scaffold and [review design](docs/design.md), including the planned CSV and local web review interfaces.

[Unreleased]: https://github.com/simonvanlierde/veriglot/commits/main
