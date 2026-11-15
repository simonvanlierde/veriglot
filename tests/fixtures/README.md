# Test fixtures

`credit-matrix/<locale>/messages.po` are excerpts of the Lingui catalogs in
[credit-matrix](https://github.com/simonvanlierde/credit-matrix) at commit `e91894f`
(MIT): app strings from `src/locales/` and `src/core/credit-i18n/locales/` in one file.
The `https://credit.niso.org/contributor-roles/...` entry is a role name from the
[credit-translation](https://github.com/contributorshipcollaboration/credit-translation)
project (CC BY 4.0; translators listed in credit-matrix's
`src/core/credit-i18n/translations/CREDITS.md`).

`broken/nl/messages.po` is the Dutch excerpt with one seeded defect per check:
`stepContributors` removed, `removedKey` added, `cancel` left in English, a renamed
placeholder, a plural without `other`, a select turned into a plural, the `<contrib>` tag
dropped, a newline and a separator space dropped, and an unclosed brace.
