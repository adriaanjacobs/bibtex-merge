# BibTeX Merge

Enrich a BibTeX file you're happy with by pulling in the new references from another one, without creating duplicates.

```bash
python3 bibmerge.py references.bib input.bib
```

No dependencies beyond Python 3.

## What it does

Every entry of `input.bib` is compared against `references.bib` (and against the entries added so far, so duplicates *within* `input.bib` are caught as well):

- **Certainly a duplicate**: same DOI (also found in `doi.org` URLs), or the exact same title with matching year and authors. It is dropped. If its key differs, the input key is added to the existing entry as an alias, so `\cite{input-key}` keeps working:
  ```bibtex
  @inproceedings{abadi2005CFI,
  ids = {abadi2005control},
  ...
  ```
- **Possibly a duplicate**: similar title, same authors with a looser title match, or the same key. You're asked:
  ```
  [Possible duplicate] title 91% similar, 100% of authors shared, same year
    input      @article{ann2019widgets}
               Fuzzy widgets for everybody
    existing   @article{lee2019}
               Fuzzy Widgets for Everyone
      [y] yes, keep lee2019 and add ann2019widgets as alias
      [r] yes, use ann2019widgets as key and add lee2019 as alias
      [n] no, add it as a new entry
      [s] skip, don't add the input entry
      [v] view both entries in full
      [q] quit, saving what was merged so far
  ```
- **New**: appended to `references.bib`, copied verbatim (including `%` comment lines directly above it). If its key is already taken by a different entry, you're asked for a new one.

`@string` macros the new entries use are copied along. If `references.bib` already defines a macro with the same name but a different meaning, that value is written out in full in the entry instead.

`references.bib` is edited as text, so its formatting, comments and order stay untouched: only entries that gain an alias (or a new key with `r`) change. The file is written once, at the end: `Ctrl-C` aborts without writing anything, and `q` writes what was decided so far.

## Aliases

Aliases go in the `ids` field, which **biblatex** (with biber) understands. Classic BibTeX/natbib ignores the field, so there `\cite{alias}` will be an undefined citation. If you use BibTeX, answer `r` to keep the key your document cites. You can also pick a different field with `--alias-field`.

## Options

| Flag | Meaning |
| --- | --- |
| `target` | The `.bib` file to enrich. Edited in place unless `-o` is given. |
| `input...` | One or more `.bib` files to take new entries from. |
| `-o`, `--output` | Write the result here instead of over `target`. |
| `-n`, `--dry-run` | Only report what would happen. |
| `--confirm` | Also ask about certain duplicates, e.g. to choose which key to keep. |
| `--no-interactive` | Never ask. Also the case when stdin is not a terminal. |
| `--uncertain {skip,add}` | What to do with possible duplicates when we cannot ask. Default `skip`: they are listed at the end, so you can rerun interactively. |
| `--alias-field` | Field to store alias keys in (default `ids`). |
| `-v`, `--verbose` | List every decision, including all added entries. |

The matching thresholds are constants at the top of `bibmerge.py`.
