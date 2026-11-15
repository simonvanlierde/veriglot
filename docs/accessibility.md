# Accessibility statement

`veriglot review` serves a local web page for reviewing translation findings. This
statement covers that page. The command line has no graphical interface.

## What is in place

- Every control has a visible label or text; the shortcuts are also announced
  (`aria-keyshortcuts`) and listed on the page.
- Everything works with the keyboard alone: Tab moves through the filters, the list (one
  tab stop, arrow keys or j/k inside), the decision buttons and the edit field.
- Focus is outlined in all controls, in light and dark colour schemes.
- Results of a save and the counters sit in polite live regions.
- Leaving the page with unsaved decisions asks for confirmation.
- The layout reflows to one column on narrow screens.

## What was tested

- axe-core 4 through `@axe-core/playwright` (WCAG 2.0, 2.1 and 2.2 A and AA rules plus
  best practices) on the list, with the edit field open and after a save, in light and dark
  colour schemes: 0 violations on the 10-finding test catalog.
- A Playwright run that filters, accepts an edit by keyboard and saves.
- Run both with `scripts/browser-check.sh`; the date and result of the last run are in the
  release notes.

## Known limits

- Not tested with a screen reader. Whitespace markers (middle dot, return arrow, tab
  arrow) may be read as symbols.
- axe finds only part of the possible problems; no manual WCAG audit has been done.
- Rows in the list are buttons, not a grid: no column sorting.
- Text zoom to 200% was not checked beyond the responsive layout.

## Report a problem

Open an issue at <https://github.com/simonvanlierde/veriglot/issues> with the browser, what you
did and what happened.
