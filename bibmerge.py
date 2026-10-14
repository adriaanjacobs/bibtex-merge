#!/usr/bin/env python3
"""
Enrich one BibTeX file with the entries of another, without creating duplicates.

    python3 bibmerge.py --into references.bib --from other.bib

Every entry of other.bib is compared against references.bib (DOI, title,
authors, year, key). Entries that are certainly already there are dropped, and
if their key differs it is recorded as an alias (biblatex `ids` field) so
\\cite{other-key} keeps working. When it is unclear, you are asked. Everything
else is appended to references.bib, copied verbatim. other.bib is only read.

    python3 bibmerge.py --into references.bib --self

does the same for references.bib against itself: every entry is compared with
the ones above it, and duplicates are folded into the first one.

references.bib is edited as text: entries are only touched when they gain an
alias, a new key or are replaced by the input version, everything else
(formatting, comments, order) stays as is.
"""

import argparse
import difflib
import os
import re
import sys
import tempfile
import unicodedata

# Built-in BibTeX macros, never need an @string definition
MONTHS = {'jan', 'feb', 'mar', 'apr', 'may', 'jun',
          'jul', 'aug', 'sep', 'oct', 'nov', 'dec'}

# Titles at least this similar are offered as possible duplicates. Short titles
# ('strace' vs 'ltrace') need to be closer, as a single letter weighs more there.
TITLE_MAYBE = 0.85
TITLE_MAYBE_SHORT = 0.92
SHORT_TITLE = 20
# ...but with the same authors and year, a looser title match is enough
# (e.g. a preprint that was renamed for the conference version)
TITLE_MAYBE_SAME_AUTHORS = 0.75
# Fraction of the (smaller) author list that must be shared to count as the
# same authors
AUTHORS_SAME = 0.5
# How many possible duplicates to ask about before considering an entry new
MAX_CANDIDATES = 3

VENUE_FIELDS = ('booktitle', 'journal', 'howpublished', 'publisher',
                'institution', 'school', 'organization')


class BibSyntaxError(Exception):
    pass


class Quit(Exception):
    """The user asked to stop, keeping what was merged so far."""


# --- Parsing ----------------------------------------------------------------
#
# A small hand-rolled parser instead of bibtexparser, because we need to know
# where everything is in the source text to edit it without reformatting it.

HEAD_RE = re.compile(r'@[ \t]*([A-Za-z]+)\s*([{(])')
NAME_RE = re.compile(r'[^\s=,{}()"#%]+')
KEY_RE = re.compile(r'[^,\s{}()]*')


def skip_ws(s, i):
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def match_brace(s, i):
    """s[i] is '{', return the index just past its matching '}'."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
    raise BibSyntaxError('unbalanced braces')


def match_quote(s, i):
    """s[i] is '"', return the index just past the closing '"'."""
    depth = 0
    for j in range(i + 1, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
        elif s[j] == '"' and depth == 0:
            return j + 1
    raise BibSyntaxError('unterminated "quoted" value')


def match_paren(s, i):
    """s[i] is '(' opening an entry, return the index just past its ')'."""
    depth = 0
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == '{':
            j = match_brace(s, j)
            continue
        if c == '"':
            j = match_quote(s, j)
            continue
        if c == ')':
            return j + 1
        j += 1
    raise BibSyntaxError('unbalanced parentheses')


class Field:
    def __init__(self, name, name_start, name_end, eq, value_start, value_end, parts):
        self.name = name                  # lowercased
        self.name_start = name_start      # offsets into the entry's raw text
        self.name_end = name_end
        self.eq = eq
        self.value_start = value_start
        self.value_end = value_end
        self.parts = parts                # [(kind, text, start, end)], joined by '#'

    def text(self, strings):
        """The value as LaTeX text, with @string macros expanded."""
        out = []
        for kind, text, _, _ in self.parts:
            if kind == 'macro':
                s = strings.get(text.lower())
                out.append(s.value if s else text)
            else:
                out.append(text)
        return ''.join(out)


def parse_fields(raw, i, end):
    """Parse 'name = value, ...' in raw[i:end]."""
    fields = []
    while True:
        i = skip_ws(raw, i)
        if i < end and raw[i] == ',':
            i += 1
            continue
        if i >= end:
            return fields
        m = NAME_RE.match(raw, i)
        if not m:
            raise BibSyntaxError(f'expected a field name, got {raw[i:i + 20]!r}')
        i = skip_ws(raw, m.end())
        if i >= end or raw[i] != '=':
            raise BibSyntaxError(f"expected '=' after field {m.group()!r}")
        eq = i
        i = skip_ws(raw, i + 1)
        value_start = i
        parts = []
        while True:
            if i >= end:
                raise BibSyntaxError(f'missing value for field {m.group()!r}')
            if raw[i] == '{':
                j = match_brace(raw, i)
                parts.append(('brace', raw[i + 1:j - 1], i, j))
            elif raw[i] == '"':
                j = match_quote(raw, i)
                parts.append(('quote', raw[i + 1:j - 1], i, j))
            else:
                tok = NAME_RE.match(raw, i)
                if not tok:
                    raise BibSyntaxError(f'bad value for field {m.group()!r}: {raw[i:i + 20]!r}')
                j = tok.end()
                parts.append(('number' if tok.group().isdigit() else 'macro', tok.group(), i, j))
            value_end = j
            i = skip_ws(raw, j)
            if i < end and raw[i] == '#':
                i = skip_ws(raw, i + 1)
                continue
            break
        fields.append(Field(m.group().lower(), m.start(), m.end(), eq,
                            value_start, value_end, parts))


class StringDef:
    def __init__(self, raw, span):
        self.raw = raw
        self.span = span
        m = HEAD_RE.match(raw)
        fields = parse_fields(raw, m.end(), len(raw) - 1)
        if len(fields) != 1:
            raise BibSyntaxError('@string should define exactly one macro')
        self.field = fields[0]
        self.name = raw[self.field.name_start:self.field.name_end].lower()
        # Macros used inside other @string definitions are not expanded here
        self.value = ''.join(p[1] for p in self.field.parts)


class Entry:
    def __init__(self, raw, origin, path, line, span=None, prefix=''):
        self.raw = raw
        self.origin = origin          # 'target' (the --into file) or 'input' (a --from file)
        self.path = path
        self.line = line
        self.span = span              # where it sits in its source file
        self.prefix = prefix          # comment lines just above it, kept with it
        self.modified = False
        self.info = None
        self._parse()

    def _parse(self):
        m = HEAD_RE.match(self.raw)
        self.type = m.group(1).lower()
        k = KEY_RE.match(self.raw, skip_ws(self.raw, m.end()))
        self.key = k.group()
        self.key_span = k.span()
        self.field_list = parse_fields(self.raw, k.end(), len(self.raw) - 1)
        self.fields = {}
        for f in self.field_list:
            self.fields.setdefault(f.name, f)

    def get(self, name, strings):
        f = self.fields.get(name)
        return f.text(strings) if f else None

    @property
    def aliases(self):
        f = self.fields.get('ids')
        if not f:
            return []
        return [a.strip() for a in f.text({}).split(',') if a.strip()]

    @property
    def keys(self):
        return [self.key] + self.aliases

    def _replace(self, start, end, new):
        self.raw = self.raw[:start] + new + self.raw[end:]
        self.modified = True
        self._parse()

    def rename(self, key):
        self._replace(*self.key_span, key)

    def replace_with(self, other):
        """Take over the contents of another entry, staying where this one is."""
        self._replace(0, len(self.raw), other.raw)
        self.info = other.info

    def add_aliases(self, aliases, field='ids'):
        if not aliases:
            return
        existing = self.fields.get(field)
        if existing:
            value = ', '.join(self.aliases + aliases)
            self._replace(existing.value_start, existing.value_end, '{' + value + '}')
            return
        value = '{' + ', '.join(aliases) + '}'
        # Insert as the first field, formatted like the field that is first now
        after_key = self.key_span[1]
        comma = skip_ws(self.raw, after_key)
        nl = '\r\n' if '\r\n' in self.raw else '\n'
        if comma >= len(self.raw) - 1 or self.raw[comma] != ',':
            # '@misc{key}' without any fields
            self._replace(after_key, after_key, f',{nl}  {field} = {value}{nl}')
            return
        first = self.field_list[0] if self.field_list else None
        if first is None or '\n' not in self.raw[comma:first.name_start]:
            # one-line entry
            self._replace(comma + 1, comma + 1, f' {field} = {value},')
            return
        line_start = self.raw.rfind('\n', 0, first.name_start) + 1
        indent = self.raw[line_start:first.name_start]
        if indent.strip():
            indent = '  '
        before_eq = self.raw[first.name_end:first.eq]
        after_eq = self.raw[first.eq + 1:first.value_start]
        if len(before_eq) > 1 and '\t' not in before_eq:
            # aligned '=' signs: keep them aligned
            before_eq = ' ' * max(1, first.eq - first.name_start - len(field))
        line = f'{nl}{indent}{field}{before_eq}={after_eq}{value},'
        self._replace(comma + 1, comma + 1, line)

    def inline_macro(self, field, part_index, value):
        _, _, start, end = field.parts[part_index]
        self._replace(start, end, '{' + value + '}')


def preceding_comments(text, at):
    """Comment lines directly above position `at` (no blank line in between)."""
    start = text.rfind('\n', 0, at) + 1
    if text[start:at].strip():
        return ''
    begin = start
    while begin > 0:
        prev = text.rfind('\n', 0, begin - 1) + 1
        if not text[prev:begin].strip().startswith('%'):
            break
        begin = prev
    return text[begin:start]


def parse_bib(text, origin, path):
    """Return (entries, @string definitions) found in text."""
    entries, strings = [], []
    i = 0
    line, counted = 1, 0
    while True:
        at = text.find('@', i)
        if at == -1:
            return entries, strings
        m = HEAD_RE.match(text, at)
        if not m:
            i = at + 1
            continue
        kind = m.group(1).lower()
        line += text.count('\n', counted, at)
        counted = at
        try:
            open_idx = m.end() - 1
            end = match_brace(text, open_idx) if text[open_idx] == '{' else match_paren(text, open_idx)
            raw = text[at:end]
            if kind == 'string':
                strings.append(StringDef(raw, (at, end)))
            elif kind not in ('comment', 'preamble'):
                entries.append(Entry(raw, origin, path, line, (at, end), preceding_comments(text, at)))
        except BibSyntaxError as e:
            sys.exit(f'{path}:{line}: {e}')
        i = end


# --- Comparing entries ------------------------------------------------------

LATEX_SPECIAL = {'ss': 'ss', 'ae': 'ae', 'AE': 'ae', 'oe': 'oe', 'OE': 'oe',
                 'aa': 'a', 'AA': 'a', 'o': 'o', 'O': 'o', 'l': 'l', 'L': 'l',
                 'i': 'i', 'j': 'j'}
UNICODE_SPECIAL = str.maketrans({'ø': 'o', 'Ø': 'o', 'ß': 'ss', 'æ': 'ae', 'Æ': 'ae',
                                 'œ': 'oe', 'Œ': 'oe', 'ł': 'l', 'Ł': 'l', 'đ': 'd',
                                 'Đ': 'd', 'ı': 'i'})


def normalize(text):
    """Reduce LaTeX/Unicode text to lowercase ASCII words for comparison."""
    if not text:
        return ''
    t = re.sub(r'\\[`\'^"~=.]', '', text)                    # \' \" ...
    t = re.sub(r'\\[uvHcdbrtk](?![A-Za-z])\s*', '', t)       # \v{s} \c c ...
    t = re.sub(r'\\([A-Za-z]+)(?![A-Za-z])',
               lambda m: LATEX_SPECIAL.get(m.group(1), ' '), t)  # \ae, \textendash
    t = t.translate(UNICODE_SPECIAL)
    t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode()
    t = re.sub(r'[^a-z0-9]+', ' ', t.lower())
    return t.strip()


def split_top_level(text, sep_re):
    """Split on sep_re, ignoring separators inside braces."""
    out, depth, start = [], 0, 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        elif depth == 0:
            m = sep_re.match(text, i)
            if m:
                out.append(text[start:i])
                start = i = m.end()
                continue
        i += 1
    out.append(text[start:])
    return out


AND_RE = re.compile(r'\s+and\s+')


def last_names(author_field):
    """Normalized last names: 'van der Kouwe, Erik' and 'Erik van der Kouwe' both give 'kouwe'."""
    names = []
    for person in split_top_level(author_field.replace('\n', ' '), AND_RE):
        person = person.strip()
        if not person or person == 'others':
            continue
        if person.startswith('{') and person.endswith('}'):
            # {Intel Inc.}: an institution, keep it whole
            names.append(normalize(person))
            continue
        parts = split_top_level(person, re.compile(r','))
        last = parts[0] if len(parts) > 1 else person
        words = normalize(last).split()
        if words:
            names.append(words[-1])
    return names


DOI_RE = re.compile(r'10\.\d{4,9}/\S+')


def find_doi(entry, strings):
    for name in ('doi', 'url'):
        value = entry.get(name, strings)
        if value:
            m = DOI_RE.search(value)
            if m and (name == 'doi' or 'doi.org' in value):
                return m.group().lower().rstrip('.')
    return None


URL_RE = re.compile(r'https?://\S+?(?=[}\s]|$)')


def find_url(entry, strings):
    """The first URL in url/howpublished/note, normalized, or None."""
    for name in ('url', 'howpublished', 'note'):
        m = URL_RE.search(entry.get(name, strings) or '')
        if m:
            url = re.sub(r'^https?://(www\.)?', '', m.group().lower())
            return url.rstrip('/')
    return None


class Info:
    """What we compare entries on, extracted once per entry."""

    def __init__(self, entry, strings):
        self.strings = strings
        self.title_raw = ' '.join((entry.get('title', strings) or '').split())
        self.title = normalize(self.title_raw)
        self.authors_raw = entry.get('author', strings) or entry.get('editor', strings) or ''
        self.authors = last_names(self.authors_raw)
        year = entry.get('year', strings) or entry.get('date', strings) or ''
        m = re.search(r'\d{4}', year)
        self.year = m.group() if m else None
        self.year_raw = year.strip()
        self.doi = find_doi(entry, strings)
        self.url = find_url(entry, strings)
        # 'CWE-125' vs 'CWE-787', 'SPEC CPU2006' vs 'SPEC CPU2017': numbers in a
        # title tend to tell apart works that are otherwise named alike
        self.numbers = sorted(w for w in self.title.split() if any(c.isdigit() for c in w))
        self.venue = next((' '.join(v.split()) for v in
                           (entry.get(f, strings) for f in VENUE_FIELDS) if v), None)


class Match:
    def __init__(self, target, sure, score, reasons):
        self.target = target
        self.sure = sure
        self.score = score
        self.reasons = reasons


def author_overlap(a, b):
    """Fraction of the smaller author list found in the other, None if unknown."""
    if not a or not b:
        return None
    shared = len(set(a) & set(b))
    return shared / min(len(set(a)), len(set(b)))


def compare(entry, other):
    """Is other (already in the target) the same work as entry? None if clearly not."""
    a, b = entry.info, other.info
    same_key = {k.lower() for k in entry.keys} & {k.lower() for k in other.keys}

    if a.doi and a.doi == b.doi:
        return Match(other, True, 3.0, ['same DOI'])

    sim = difflib.SequenceMatcher(None, a.title, b.title).ratio() if a.title and b.title else 0.0
    authors = author_overlap(a.authors, b.authors)
    same_authors = authors is not None and authors >= AUTHORS_SAME
    year_ok = not a.year or not b.year or a.year == b.year
    doi_conflict = bool(a.doi and b.doi and a.doi != b.doi)
    url_conflict = bool(a.url and b.url and a.url != b.url)
    if a.numbers and b.numbers and a.numbers != b.numbers:
        return None

    reasons = [f'title {sim:.0%} similar']
    if authors is not None:
        reasons.append(f'{authors:.0%} of authors shared')
    if a.year and b.year:
        reasons.append('same year' if a.year == b.year else 'different year')
    if doi_conflict:
        reasons.append('different DOI')
    if url_conflict:
        reasons.append('different URL')
    if same_key:
        reasons.append('same key')
    score = sim + 0.3 * (authors or 0) + 0.1 * year_ok + 0.2 * bool(same_key)

    if sim == 1.0 and year_ok and not doi_conflict and (authors is None or same_authors):
        return Match(other, True, 2.0 + score, reasons)

    short = min(len(a.title), len(b.title)) < SHORT_TITLE
    # different URLs: only trust a really similar title, not just shared authors
    loose = same_authors and not url_conflict
    if (sim >= (TITLE_MAYBE_SHORT if short else TITLE_MAYBE)
            or (loose and year_ok and sim >= TITLE_MAYBE_SAME_AUTHORS)
            or (loose and a.title and b.title
                and (a.title.startswith(b.title) or b.title.startswith(a.title)))
            # same key but nothing else in common is a key conflict, handled when adding
            or (same_key and (sim >= 0.5 or same_authors))):
        return Match(other, False, score, reasons)
    return None


# --- Talking to the user ----------------------------------------------------

def style(code):
    use = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
    return (lambda s: f'\033[{code}m{s}\033[0m') if use else (lambda s: s)


bold, dim, green, yellow = style('1'), style('2'), style('32'), style('33')


def short_authors(info, n=4):
    if not info.authors_raw:
        return '(no authors)'
    people = [' '.join(p.split()) for p in split_top_level(info.authors_raw, AND_RE)]
    if len(people) > n:
        return '; '.join(people[:n]) + f'; ... ({len(people)} total)'
    return '; '.join(people)


def describe(entry, label, into):
    i = entry.info
    alias = f'  {dim("aliases: " + ", ".join(entry.aliases))}' if entry.aliases else ''
    if label == 'existing' and entry.origin == 'input':
        where = f'in {into}, added from {entry.path}:{entry.line} during this run'
    else:
        where = f'{entry.path}:{entry.line}'
    lines = [f'  {bold(f"{label:<10}")} @{entry.type}{{{bold(entry.key)}}}{alias}  {dim("(" + where + ")")}',
             f'             {i.title_raw or "(no title)"}',
             f'             {short_authors(i)}',
             f'             {i.year_raw or "(no year)"}' + (f' · {i.venue}' if i.venue else '')]
    if i.doi:
        lines.append(f'             doi:{i.doi}')
    return '\n'.join(lines)


class Duplicate:
    def __init__(self, input_key, target, how, aliases, renamed_from, replaced, own):
        self.own = own                    # --self: a duplicate within the --into file
        self.input_key = input_key
        self.target = target
        self.key = target.key             # as it was right after this decision
        self.how = how
        self.aliases = aliases            # keys added as alias to target
        self.renamed_from = renamed_from  # target's key before, if it changed
        self.replaced = replaced          # target now holds the input entry's contents


class Merger:
    def __init__(self, target_entries, target_strings, input_strings, args):
        self.args = args
        self.entries = list(target_entries)
        self.target_strings = {s.name: s for s in target_strings}
        self.input_strings = {s.name: s for s in input_strings}
        self.new_strings = {}
        self.index = {}          # lowercased key or alias -> entry, of the entries in self.entries
        # Every key in the --into file, including entries --self hasn't looked at
        # yet, so we never hand out a key that is used further down.
        self.reserved = {}
        for e in self.entries:
            for k in e.keys:
                self.reserved.setdefault(k.lower(), e)
        self.entries = []
        self.interactive = args.interactive
        self.added = []
        self.removed = []        # --self: entries of the --into file folded into an earlier one
        self.duplicates = []     # [Duplicate]
        self.skipped = []        # (entry, reason)
        self.renamed = []        # (old key, new key, own)
        self.warnings = []

    def keep(self, entry):
        self.entries.append(entry)
        for k in entry.keys:
            self.index.setdefault(k.lower(), entry)

    def owner(self, key):
        return self.index.get(key.lower()) or self.reserved.get(key.lower())

    # prompting

    def ask(self, question, valid=None, default=None):
        """Read an answer, or return None if we can't ask anyone."""
        while self.interactive:
            try:
                ans = input(question).strip()
            except EOFError:
                print()
                self.interactive = False
                return None
            if valid is None or ans.lower() in valid:
                return ans
            if not ans and default is not None:
                return default
            print(f'  Please answer one of: {", ".join(sorted(valid))}')
        return None

    def ask_duplicate(self, entry, match, n, total):
        own = entry.origin == 'target'
        this, that = ('later', 'earlier') if own else ('incoming', 'existing')
        same_key = entry.key.lower() == match.target.key.lower()
        what = 'Duplicate' if match.sure else 'Possible duplicate'
        print()
        print(yellow(f'[{what}{f" {n}/{total}" if total > 1 else ""}] ') + dim(', '.join(match.reasons)))
        print(describe(entry, this, self.args.into))
        print(describe(match.target, that, self.args.into))
        old, new = bold(match.target.key), bold(entry.key)
        if same_key:
            options = {'y': f'yes, keep the {that} entry (the key is the same already)',
                       'i': f'yes, keep the {this} entry instead'}
        else:
            options = {'y': f'yes, keep the {that} entry, key {old}, alias {new}',
                       'r': f'yes, keep the {that} entry, key {new}, alias {old}',
                       'i': f'yes, keep the {this} entry instead, key {old}, alias {new}',
                       'ir': f'yes, keep the {this} entry instead, key {new}, alias {old}'}
        if n < total:
            options['n'] = 'no, look at the next candidate'
        else:
            options['n'] = 'no, keep both' if own else 'no, add it as a new entry'
        if not own:
            options['s'] = "skip, don't add the incoming entry"
        options['v'] = 'view both entries in full'
        options['q'] = 'quit, saving what was merged so far'
        for k, v in options.items():
            print(f'    [{k}] {v}')
        while True:
            ans = self.ask('  > ', set(options) | ({'ri'} if 'ir' in options else set()))
            if ans is None:
                return None
            ans = ''.join(sorted(ans.lower()))  # 'ri' -> 'ir'
            if ans == 'v':
                print('\n' + entry.prefix + entry.raw + '\n\n' + match.target.raw + '\n')
                continue
            if ans == 'q':
                raise Quit()
            return ans

    # merging

    def merge(self, entry):
        """Find out whether entry is already among self.entries, and act on it.

        entry comes from a --from file, or, with --self, from the --into file
        itself, in which case self.entries are the entries above it.
        """
        own = entry.origin == 'target'
        if entry.info is None:
            entry.info = Info(entry, self.input_strings)
        matches = [m for m in (compare(entry, t) for t in self.entries) if m]
        matches.sort(key=lambda m: m.score, reverse=True)

        sure = [m for m in matches if m.sure]
        if sure and not self.args.confirm:
            self.duplicate(entry, sure[0].target, how='auto: ' + ', '.join(sure[0].reasons))
            return

        candidates = matches[:MAX_CANDIDATES]
        for n, m in enumerate(candidates, 1):
            ans = self.ask_duplicate(entry, m, n, len(candidates))
            if ans is None:
                # can't ask: only certain duplicates are decided without the user
                if m.sure:
                    self.duplicate(entry, m.target, how='auto: ' + ', '.join(m.reasons))
                elif self.args.uncertain == 'add' and not own:
                    continue
                else:
                    self.skipped.append((entry, f'possibly a duplicate of {m.target.key}'))
                    if own:
                        self.keep(entry)  # it's in the file, and stays there
                return
            if ans in ('y', 'r', 'i', 'ir'):
                self.duplicate(entry, m.target, how='confirmed',
                               use_input_key='r' in ans, use_input_entry='i' in ans)
                return
            if ans == 's':
                self.skipped.append((entry, 'skipped by you'))
                return
        self.add(entry)

    def duplicate(self, entry, target, how, use_input_key=False, use_input_entry=False):
        """entry is the same work as target: keep one of them, with the other's keys as aliases."""
        old_key = target.key
        all_keys = target.keys + entry.keys
        own = entry.origin == 'target'
        if own:
            self.removed.append(entry)
        if use_input_entry:
            target.replace_with(entry)
            if not own:
                self.bring_strings(target)
        key = entry.key if use_input_key else old_key
        owner = self.owner(key)
        if owner is not None and owner not in (target, entry):
            self.warnings.append(f"can't rename {old_key} to {key}: {owner.key} already uses that key")
            key = old_key
        if target.key != key:
            target.rename(key)
        self.index[key.lower()] = target
        have = {k.lower() for k in target.keys}
        aliases = []
        for k in all_keys:
            if k.lower() in have:
                continue
            owner = self.owner(k)
            if owner is not None and owner not in (target, entry):
                self.warnings.append(f"can't add alias {k} to {target.key}: "
                                     f'{owner.key} already uses that key')
                continue
            aliases.append(k)
            have.add(k.lower())
        target.add_aliases(aliases, self.args.alias_field)
        for k in aliases:
            self.index[k.lower()] = target
        self.duplicates.append(Duplicate(entry.key, target, how, aliases,
                                         old_key if key.lower() != old_key.lower() else None,
                                         use_input_entry, own))

    def add(self, entry):
        """entry is not a duplicate: add it (or with --self, leave it where it is)."""
        own = entry.origin == 'target'
        this, that = ('later', 'earlier') if own else ('incoming', 'existing')
        if entry.key.lower() in self.index:
            other = self.index[entry.key.lower()]
            new = self.free_key(entry.key)
            print()
            print(yellow('[Key conflict] ') + f'{bold(entry.key)} is already used by a different entry:')
            print(describe(entry, this, self.args.into))
            print(describe(other, that, self.args.into))
            ans = self.ask(f'  New key for the {this} entry [{new}] > ')
            while ans and self.owner(ans) not in (None, entry):
                ans = self.ask(f'  {ans} is taken too. New key [{new}] > ')
            new = ans or new
            self.renamed.append((entry.key, new, own))
            entry.rename(new)
        clashes = [k for k in entry.aliases if self.owner(k) not in (None, entry)]
        if clashes:
            self.warnings.append(f'{entry.key}: its aliases {", ".join(clashes)} '
                                 'are already used by other entries')
        if not own:
            self.bring_strings(entry)
            self.added.append(entry)
        self.keep(entry)

    def free_key(self, key):
        def taken(k):
            return self.owner(k) is not None
        for suffix in 'bcdefghijklmnopqrstuvwxyz':
            if not taken(key + suffix):
                return key + suffix
        n = 2
        while taken(f'{key}-{n}'):
            n += 1
        return f'{key}-{n}'

    def bring_strings(self, entry):
        """Make sure every @string macro the entry uses means the same in the --into file."""
        for field in list(entry.field_list):
            for idx, (kind, name, _, _) in enumerate(field.parts):
                if kind == 'macro' and name.lower() not in MONTHS:
                    self.resolve_macro(entry, field.name, idx, name.lower())

    def resolve_macro(self, entry, field_name, idx, name):
        source = self.input_strings.get(name)
        ours = self.target_strings.get(name) or self.new_strings.get(name)
        if source is None:
            if ours is None:
                self.warnings.append(f'{entry.key}: @string {name} is not defined anywhere')
            return
        if ours is not None:
            if ' '.join(ours.value.split()) != ' '.join(source.value.split()):
                # same macro name, different meaning: spell it out
                field = next(f for f in entry.field_list if f.name == field_name)
                entry.inline_macro(field, idx, source.value)
                self.warnings.append(f'{entry.key}: @string {name} means something else in '
                                     f'{self.args.into}, so its value was written out in full')
            return
        self.take_string(name)

    def take_string(self, name):
        source = self.input_strings[name]
        self.new_strings[name] = None   # placeholder, keeps the dependencies in front of it
        # an @string can use other @strings, those have to come first
        for kind, dep, _, _ in source.field.parts:
            dep = dep.lower()
            if (kind == 'macro' and dep in self.input_strings
                    and dep not in self.target_strings and dep not in self.new_strings):
                self.take_string(dep)
        del self.new_strings[name]
        self.new_strings[name] = source

    # output

    def render(self, text, target_strings):
        nl = '\r\n' if '\r\n' in text else '\n'
        edits = [(e.span[0], e.span[1], e.raw) for e in self.entries
                 if e.origin == 'target' and e.modified]
        for e in self.removed:
            # the whole lines it is on, and the blank lines after it
            start, end = e.span
            line_start = text.rfind('\n', 0, start) + 1
            if not text[line_start:start].strip():
                start = line_start
            end = re.compile(r'[ \t]*(?:\r?\n[ \t]*)*').match(text, end).end()
            if end < len(text):
                end = text.rfind('\n', 0, end) + 1
                edits.append((start, end, ''))
            else:
                # last entry of the file: also drop the blank lines before it
                start = len(text[:start].rstrip())
                edits.append((start, end, nl if start else ''))
        strings_block = nl.join(s.raw for s in self.new_strings.values())
        if strings_block and target_strings:
            # next to the @strings already there
            end = max(s.span[1] for s in target_strings)
            edits.append((end, end, nl + strings_block))
            strings_block = ''
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        if strings_block or self.added:
            text = text.rstrip() + nl
            if strings_block:
                text += nl + strings_block + nl
            for e in self.added:
                text += nl + e.prefix + e.raw + nl
        return text

    def report(self, verbose):
        own_dups = [d for d in self.duplicates if d.own]
        if own_dups:
            print(f'\n{bold("Duplicates removed")} from {self.args.into}: {len(own_dups)} entries')
            self.report_duplicates(own_dups, True)
        own_renamed = [r for r in self.renamed if r[2]]
        if own_renamed:
            print(f'\n{bold("Renamed")} (the key was used by two different entries):')
            for old, new, _ in own_renamed:
                print(f'  {old} -> {new}')
        own_skipped = [s for s in self.skipped if s[0].origin == 'target']
        if own_skipped:
            print(f'\n{bold("Left alone")}:')
            for e, why in own_skipped:
                print(f'  {e.key:<32} {why}')

        dups = [d for d in self.duplicates if not d.own]
        if dups:
            changed = [d for d in dups if d.aliases or d.renamed_from or d.replaced]
            print(f'\n{bold("Already present")}: {len(dups)} entries, '
                  f'{len(changed)} of them changed the existing entry')
            self.report_duplicates(dups, verbose)
        if self.added:
            print(f'\n{bold("Added")}: {len(self.added)} entries' +
                  (f', {len(self.new_strings)} @string definitions' if self.new_strings else ''))
            if verbose:
                for e in self.added:
                    print(f'  {e.key:<32} {e.info.title_raw}')
        renamed = [r for r in self.renamed if not r[2]]
        if renamed:
            print(f'\n{bold("Added under a new key")} (because the key was taken):')
            for old, new, _ in renamed:
                print(f'  {old} -> {new}')
        skipped = [s for s in self.skipped if s[0].origin != 'target']
        if skipped:
            print(f'\n{bold("Not added")}:')
            for e, why in skipped:
                print(f'  {e.key:<32} {why}')
        if self.warnings:
            print(f'\n{bold("Warnings")}:')
            for w in self.warnings:
                print(f'  {w}')

    def report_duplicates(self, dups, verbose):
        for d in dups:
            changes = []
            if d.replaced:
                changes.append('replaced by the ' + ('later' if d.own else 'incoming') + ' entry')
            if d.renamed_from:
                changes.append(f'renamed {d.renamed_from} -> {d.key}')
            if d.aliases:
                changes.append(f'alias {", ".join(d.aliases)}')
            if not changes and not verbose:
                continue
            change = ', '.join(changes) if changes else 'nothing to do'
            print(f'  {d.input_key:<32} {d.key}: {change}  {dim("(" + d.how + ")")}')


def write_atomic(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read(path):
    with open(path, encoding='utf-8', newline='') as f:
        return f.read()


def main():
    parser = argparse.ArgumentParser(
        description='Enrich a BibTeX file with the entries of other ones, without creating duplicates.',
        epilog='example: %(prog)s --into references.bib --from chapters/paper/references.bib')
    parser.add_argument('--into', required=True, metavar='BIB',
                        help='the .bib file to enrich: new entries and aliases are written into it '
                             '(in place, unless -o is given)')
    parser.add_argument('--from', dest='sources', action='append', default=[], metavar='BIB',
                        help='a .bib file to take entries from; it is only read, never modified. '
                             'Can be given more than once')
    parser.add_argument('--self', dest='dedupe', action='store_true',
                        help='first remove duplicates within the --into file itself '
                             '(with --from, the merge happens after that)')
    parser.add_argument('-o', '--output', metavar='BIB',
                        help='write the result here instead of over the --into file')
    parser.add_argument('-n', '--dry-run', action='store_true', help="only report, don't write anything")
    parser.add_argument('--confirm', action='store_true',
                        help='also ask about certain duplicates (to choose which key or entry to keep)')
    parser.add_argument('--no-interactive', action='store_true', help='never ask anything')
    parser.add_argument('--uncertain', choices=('skip', 'add'), default='skip',
                        help='what to do with possible duplicates when we cannot ask (default: skip)')
    parser.add_argument('--alias-field', default='ids',
                        help="field to store alias keys in (default: 'ids', understood by biblatex)")
    parser.add_argument('-v', '--verbose', action='store_true', help='list every decision')
    args = parser.parse_args()
    args.interactive = not args.no_interactive and sys.stdin.isatty()
    if not args.sources and not args.dedupe:
        parser.error('nothing to do: give --from BIB to merge, and/or --self to deduplicate --into')

    for path in [args.into] + args.sources:
        if not os.path.isfile(path):
            parser.error(f'{path}: no such file')
    for path in args.sources:
        if os.path.samefile(path, args.into):
            parser.error(f'--from {path} is the same file as --into {args.into}')

    out = args.output or args.into
    sources = ', '.join(args.sources)
    if args.dedupe and args.sources:
        what = f'Removing duplicates within {bold(out)}, then merging entries from {sources} into it'
    elif args.dedupe:
        what = f'Removing duplicates within {bold(out)}'
    else:
        what = f'Merging entries from {sources} into {bold(out)}'
    print(what + (f' (based on {args.into})' if args.output else '')
          + (' (dry run)' if args.dry_run else '') + '.')

    target_text = read(args.into)
    target_entries, target_strings = parse_bib(target_text, 'target', args.into)
    input_strings = []
    input_entries = []
    for path in args.sources:
        entries, strings = parse_bib(read(path), 'input', path)
        input_entries += entries
        input_strings += strings

    merger = Merger(target_entries, target_strings, input_strings, args)
    for e in target_entries:
        e.info = Info(e, merger.target_strings)

    quit_early = False
    todo = (target_entries if args.dedupe else []) + input_entries
    if not args.dedupe:
        for e in target_entries:
            merger.keep(e)
    try:
        for n, entry in enumerate(todo):
            merger.merge(entry)
    except Quit:
        quit_early = True
        for entry in todo[n:]:
            merger.skipped.append((entry, 'not looked at (quit)'))
    except KeyboardInterrupt:
        sys.exit('\nAborted, nothing was written.')

    merger.report(args.verbose)
    result = merger.render(target_text, target_strings)
    if args.dry_run:
        print(f'\nDry run: {out} was not written.')
    elif result != target_text or args.output:
        write_atomic(out, result)
        print(f'\nWrote {out}.' + (' (stopped early)' if quit_early else ''))
    else:
        print(f'\nNothing to do, {out} is unchanged.')


if __name__ == '__main__':
    main()
