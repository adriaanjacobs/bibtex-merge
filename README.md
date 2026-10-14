# BibTeX Merge

Enrich a BibTeX file you're happy with by pulling in the new references from another one, without creating duplicates.

```bash
./bibmerge.py --into references.bib --from other.bib
```

`--into` is the file that gets enriched (and modified, unless you pass `-o`). `--from` is only read, and can be given more than once. No dependencies beyond Python 3.

To clean up duplicates *within* a file (the same work under different keys):

```bash
./bibmerge.py --into references.bib --self
```

`--self` and `--from` can be combined: the file is deduplicated first, then the merge happens.

> [!CAUTION]
> This whole thing was AI-generated without a single look at the code. 
> I use it on git-tracked files so I can review all the resulting changes to the bibtex. 
> It has worked well for me so far, but I can't guarantee the same for you. 

## What it does

Every entry of `other.bib` is compared against `references.bib` (and against the entries added so far, so duplicates *within* `other.bib` are caught as well):

- **Certainly a duplicate**: same DOI (also found in `doi.org` URLs), or the exact same title with matching year and authors. It is dropped. If its key differs, the incoming key is added to the existing entry as an alias, so `\cite{other-key}` keeps working:
  ```bibtex
  @inproceedings{abadi2005CFI,
  ids = {abadi2005control},
  ...
  ```
- **Possibly a duplicate**: similar title, same authors with a looser title match, or the same key. You're asked:
  ```
  [Possible duplicate] title 91% similar, 100% of authors shared, same year
    incoming   @article{ann2019widgets}  (in other.bib)
               Fuzzy widgets for everybody
    existing   @article{lee2019}  (in references.bib)
               Fuzzy Widgets for Everyone
      [y] yes, keep the existing entry, key lee2019, alias ann2019widgets
      [r] yes, keep the existing entry, key ann2019widgets, alias lee2019
      [i] yes, replace it with the incoming entry, key lee2019, alias ann2019widgets
      [ir] yes, replace it with the incoming entry, key ann2019widgets, alias lee2019
      [n] no, add it as a new entry
      [s] skip, don't add the incoming entry
      [v] view both entries in full
      [q] quit, saving what was merged so far
  ```
  So you pick which version of the entry to keep (`i` takes the incoming one), and which key to keep (`r` takes the incoming one); the other key becomes an alias. The incoming entry takes the existing entry's place in the file.
- **New**: appended to `references.bib`, copied verbatim (including `%` comment lines directly above it). If its key is already taken by a different entry, you're asked for a new one.

`@string` macros the new entries use are copied along. If `references.bib` already defines a macro with the same name but a different meaning, that value is written out in full in the entry instead.

`references.bib` is edited as text, so its formatting, comments and order stay untouched: only entries that gain an alias, a new key (`r`) or are replaced (`i`) change. The file is written once, at the end: `Ctrl-C` aborts without writing anything, and `q` writes what was decided so far.

## Deduplicating a file with `--self`

Every entry is compared against the entries above it, in the same way as a merge. When a later entry is a duplicate of an earlier one, the earlier entry stays where it is and the later one is deleted, with its key (and aliases) moved over as aliases. As with a merge, certain duplicates are handled automatically and you are asked about the uncertain ones:

```
[Possible duplicate] title 91% similar, 100% of authors shared, same year
  later      @article{ann2019widgets}  (references.bib:20)
  earlier    @article{lee2019}  (references.bib:2)
    [y] yes, keep the earlier entry, key lee2019, alias ann2019widgets
    [r] yes, keep the earlier entry, key ann2019widgets, alias lee2019
    [i] yes, keep the later entry instead, key lee2019, alias ann2019widgets
    [ir] yes, keep the later entry instead, key ann2019widgets, alias lee2019
    [n] no, keep both
```

With `i`, the later entry's contents move to the earlier entry's place. Two *different* entries that share a key are reported as a key conflict, and you're asked for a new key for the later one.

## Aliases

Aliases go in the `ids` field, which **biblatex** (with biber) understands. Classic BibTeX/natbib ignores the field, so there `\cite{alias}` will be an undefined citation. If you use BibTeX, answer `r`/`ir` to keep the key your document cites. You can also pick a different field with `--alias-field`.

## Options

| Flag | Meaning |
| --- | --- |
| `--into BIB` | The `.bib` file to enrich. Edited in place unless `-o` is given. |
| `--from BIB` | A `.bib` file to take new entries from. Only read. Repeat it to merge several files. |
| `--self` | Remove duplicates within the `--into` file itself (before merging, if `--from` is also given). |
| `-o`, `--output` | Write the result here instead of over the `--into` file. |
| `-n`, `--dry-run` | Only report what would happen. |
| `--confirm` | Also ask about certain duplicates, e.g. to choose which key or entry to keep. |
| `--no-interactive` | Never ask. Also the case when stdin is not a terminal. |
| `--uncertain {skip,add}` | What to do with possible duplicates when we cannot ask. Default `skip`: they are listed at the end, so you can rerun interactively. With `--self`, possible duplicates are always left alone. |
| `--alias-field` | Field to store alias keys in (default `ids`). |
| `-v`, `--verbose` | List every decision, including all added entries. |

The matching thresholds are constants at the top of `bibmerge.py`.
