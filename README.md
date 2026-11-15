# veriglot

Review app translations with local models and deterministic checks.

The `veriglot` command checks translation catalogs against their source catalog and
applies the corrections a reviewer accepted. Model-assisted review and the web UI are
planned; see the [design](docs/design.md).

## Scope

UI catalogs: message keys, source and target text, locale, glossary and usage context.

Placeholder and plural checks, terminology checks and model-assisted review. Catalog adapters preserve significant whitespace and never change vendored translations.

A simple local web UI is planned for reviewing findings, editing proposed wording
and accepting or rejecting changes. CSV remains an alternative for the same review.

## Usage

Check every locale of a Lingui `.po` catalog against the English source:

```sh
veriglot check src/locales/en/messages.po src/locales/*/messages.po
```

The source catalog is skipped when it appears among the targets. `--format json` prints
one object with `schema_version`, `findings` and `errors`; `--format csv` writes the
review sheet. Flat JSON catalogs (`{"key": "text"}`, such as `nl.json`) can be checked too.

Each finding has a stable `id` (`<locale>:<key>:<kind>`), the source and target text, a
`fingerprint` of both, and one of these kinds:

| Kind | Severity | Finds |
| --- | --- | --- |
| `missing` | error | a source key with no entry or an empty translation |
| `extra` | warning | a key that is not in the source catalog |
| `untranslated` | info | a translation identical to source text that has letters |
| `placeholder` | error | arguments such as `{name}` missing or added |
| `plural` | error or warning | plural/select arguments of another type, without `other`, or with branches that do not match the source (select) or the locale's CLDR plural categories |
| `syntax` | error | malformed ICU MessageFormat in the source or target |
| `whitespace` | warning | leading or trailing whitespace, or a newline count, that differs from the source |
| `tag` | error | rich-text tags such as `<contrib>` or `<0>` missing or added |

Exit code 0 means no error findings, 1 means some, 2 is a usage error and 3 means a file
could not be read.

To apply a review, fill in the `decision` column of the CSV (`accept` writes `proposed`,
`edit` writes `edited`, `reject` or empty leaves the entry) and run:

```sh
veriglot apply src/locales/nl/messages.po review.csv --source src/locales/en/messages.po --dry-run
```

Every row is reported as applied, rejected (with the reason: `stale`, `unknown`,
`invalid`, `read-only`, ...) or skipped. Only the changed `msgstr` lines are rewritten.
CRediT role names (`https://credit.niso.org/contributor-roles/...`) are vendored and
never written; `--read-only GLOB` protects more keys.

## Development

Install uv and just, then run:

```sh
just py-sync
uv tool install prek
prek install
just check
just hooks
```

## License

[MIT](LICENSE). Third-party data and model weights keep their own licenses.
