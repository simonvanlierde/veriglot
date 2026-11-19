# veriglot

Review app translations with local models and deterministic checks.

The `veriglot` command checks translation catalogs against their source catalog and
applies the corrections a reviewer accepted, from the command line or a local web page.
`veriglot review-model` adds model-assisted review on a local server. See the
[design](docs/design.md) and the [benchmark](docs/benchmark.md).

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
one object with `schema_version`, `findings` and `errors`, also when the command fails;
[`findings.schema.json`](src/veriglot/findings.schema.json) describes it. `--format csv` writes the
review sheet. Flat JSON catalogs (`{"key": "text"}`, such as `nl.json`) can be checked too.

Each finding has a stable `id` (`<locale>:<key>:<kind>`), the source and target text, a
`fingerprint` of both, and one of these kinds (`veriglot kinds [--format json]` lists them too):

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
| `meaning`, `omission`, `addition`, `terminology`, `register`, `grammar` | per finding | only `review-model` reports these (it also uses `untranslated`) |

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

## Model-assisted review

`review-model` asks a language model about each translation. It speaks the chat-completions
protocol, so any server that takes a base URL and a model name works (vLLM, llama.cpp
`llama-server`); the package imports no model library. Model findings are suggestions, not native
review. They use the same `Finding` type, CSV and `apply` step as `check`, with `proposed`
filled when the model proposes a fix that passes the deterministic checks.

```sh
veriglot review-model src/locales/en/messages.po src/locales/nl/messages.po \
  --base-url http://127.0.0.1:8000/v1 --model my-model --glossary glossary.toml --format csv
```

- The server must serve `--model`; the command exits 3 if it does not or cannot be reached. A base
  URL that is not on this machine needs `--allow-remote`: nothing is sent to a hosted API otherwise.
- Answers are constrained to a JSON schema (`structured_outputs`; `--constraint response_format` for
  servers that use that field, `none` to skip), validated again, checked for `finish_reason`, and
  retried once. An entry that still fails is reported on stderr and in the JSON `errors`, and the
  exit code is 3.
- Every call is appended to `--log` (default `veriglot-model.log.jsonl`): model, server version,
  prompt name and SHA-256, a `request_sha256` of the model, settings, schema and rendered prompt,
  attempt, finish reason, token counts and the raw answer.
- A rerun with the same `--log` reuses every answer there whose `request_sha256` matches and asks only
  for the rest, so a stopped or partly failed run resumes. A changed entry, glossary, prompt, model or
  setting is asked again. Use a new `--log` for a fresh run.
- `--format jsonl` prints each finding as one line as soon as its entry is answered, then a summary
  line with `schema_version`, `total`, `counts` per severity and `errors`. When stderr is a terminal
  and the format is not `json` or `jsonl`, `reviewed N/M` shows progress there.
- `--with-checks` merges the deterministic findings. Placeholder, plural, tag and syntax problems
  stay with those checks; the prompt tells the model not to report them.
- `--extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'` adds request fields, for
  example to turn reasoning off.
- The prompts are `src/veriglot/prompts/review.md` (source, target, locale, key, context, glossary)
  and `direct-judge.md`, the baseline that asks only whether a translation is correct.

A glossary is TOML. `[style]` holds a rule per locale; each `[[term]]` has an `en` source term, one
key per locale and an optional `note`. Only terms whose English side occurs in the entry's source
reach the prompt.

```toml
[style]
nl = "Address the user formally: u, uw."

[[term]]
en = "draft"
nl = "concept"
de = "Entwurf"
```

### Benchmark

`veriglot bench make` plants one error per clean translation, `bench run` asks a model in both
modes (the review prompt and the direct judge) and writes a run manifest, and `bench score` prints
recall per planted kind with Wilson 95% intervals. The data stays under the ignored `data/bench/`,
every file starting with a canary line; the method and results are in [docs/benchmark.md](docs/benchmark.md).

```sh
veriglot bench make en.po nl.po de.po fr.po --out data/bench --handwritten data/bench/handwritten.jsonl
veriglot bench run data/bench/items.jsonl --out data/bench/runs/m1 --base-url http://127.0.0.1:8000/v1 \
  --model m1 --glossary data/bench/glossary.toml --model-repo org/m1 --revision <hash>
veriglot bench score data/bench/items.jsonl data/bench/runs/m1
```

`bench make` prints a JSON object with `schema_version`, the items `path`, the counts per language and
kind, and `errors`. `bench run` appends each answer as it arrives, asks again for failed ones when rerun
into the same `--out` (and refuses a different model, prompt or items there), prints the manifest with
`schema_version`, `run_dir`, `failed` and `errors`, and exits 3 if any answer failed. An unknown `--modes`
value is a usage error (exit 2). `bench score --format json` prints the cells of each run and mode
(`k`, `n`, `failed`, `kind_match`, `rate`, `interval`) instead of the Markdown table. A MetricX-24
quality-estimation baseline comes from `uv run scripts/metricx_qe.py` (it needs a GPU and a checkout of
google-research/metricx; see its docstring) and is tabulated with `bench score ITEMS --qe DIR`.
Call logs end in `.log.jsonl` and are git-ignored; each starts with the canary line.

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
