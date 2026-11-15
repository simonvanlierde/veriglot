# veriglot

Review app translations with local models and deterministic checks.

The `veriglot` command checks translation catalogs against their source catalog and
applies the corrections a reviewer accepted. Model-assisted review is
planned; see the [design](docs/design.md).

## Scope

UI catalogs: message keys, source and target text, locale, glossary and usage context.

Placeholder and plural checks, terminology checks and model-assisted review. Catalog adapters preserve significant whitespace and never change vendored translations.

A simple local web page reviews findings, edits proposed wording and accepts or rejects
changes. CSV is an alternative for the same review.

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
| `missing` | error | a source key with no entry, or an empty or whitespace-only translation |
| `extra` | warning | a key that is not in the source catalog |
| `untranslated` | info | a translation identical to source text that has letters |
| `placeholder` | error or warning | arguments such as `{name}` missing or added (error); a plural or select branch that lacks an argument its source branch has, or a plural branch that shows neither `#` nor the number argument (warning) |
| `plural` | error or warning | plural/select arguments of another type, or with branches that do not match the source (select) or the locale's CLDR plural categories, taking `offset:` into account |
| `syntax` | error | malformed ICU MessageFormat in the source or target, including a plural or select without branches or without `other`, a non-integer `=N` key and an unknown argument type |
| `whitespace` | warning | leading or trailing whitespace, or a newline count, that differs from the source |
| `tag` | error | rich-text tags such as `<contrib>` or `<0>` missing or added, outside branches and per branch |
| `fuzzy` | info | an entry marked `#, fuzzy` |

Branches are compared one by one with the source branch of the same key, else with the
source `other` branch, so a locale may have more or fewer plural categories than the source.

Exit code 0 means no error findings, 1 means some, 2 is a usage error and 3 means a file
could not be read or is malformed (the message names the line).

To apply a review, fill in the `decision` column of the CSV (`accept` writes `proposed`,
`edit` writes `edited`, `reject` or empty leaves the entry) and run:

```sh
veriglot apply src/locales/nl/messages.po review.csv --source src/locales/en/messages.po --dry-run
```

Every row is reported as applied, rejected (with the reason: `stale`, `unknown`,
`invalid`, `read-only`, `catalog changed`, ...) or skipped (`other locale`, `resolved` by an
earlier row, `already applied`, ...). Rows for other locales are skipped, so one sheet with
every locale can be applied to each catalog in turn. Only the changed `msgstr` lines are
rewritten, and a `fuzzy` flag on a written entry is dropped; editing a fuzzy entry to its
current text confirms it. The file is replaced atomically, and not at all if it changed
since it was read.

Text cells that a spreadsheet would run as a formula (starting with `=`, `+`, `-`, `@`, a
tab or a carriage return) are written with one leading `'`, which `apply` strips again.

### Web review

```sh
veriglot review src/locales/en/messages.po src/locales/*/messages.po [--port N] [--no-browser] [--read-only GLOB]
```

Prints a URL such as `http://127.0.0.1:41873/#token=...` and opens it in a browser unless
`--no-browser`. The server listens on 127.0.0.1 only and stops on Ctrl-C. Filter by locale,
kind, severity or text; move with `j`/`k` or the arrow keys; `a` accepts the proposed text, `r`
rejects, `e` edits, `c` clears. Save applies the decisions with the same code as
`veriglot apply` and shows each row as applied, rejected or skipped with the reason. Export
and import use the review CSV columns. See [accessibility](docs/accessibility.md).

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
