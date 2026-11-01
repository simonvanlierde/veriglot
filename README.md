# veriglot

Review app translations with local models and deterministic checks.

This repository contains the initial Python scaffold and [design](docs/design.md).
Review commands and the web UI are planned; they are not implemented yet.

## Scope

UI catalogs: message keys, source and target text, locale, glossary and usage context.

Placeholder and plural checks, terminology checks and model-assisted review. Catalog adapters preserve significant whitespace and never change vendored translations.

A simple local web UI is planned for reviewing findings, editing proposed wording
and accepting or rejecting changes. CSV remains an alternative for the same review.

## Development

Install uv, just and prek, then run:

```sh
just py-sync
prek install
just check
just hooks
```

Run one credit-matrix catalog through deterministic checks, then benchmark model findings against planted errors.

## License

[MIT](LICENSE). Third-party data and model weights keep their own licenses.
