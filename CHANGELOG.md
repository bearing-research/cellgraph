# Changelog

## Unreleased

- One key scheme. `analyze` loses `normalization=` and `digest=`, and the
  `legacy-1` format (Strata's `compute_source_hash` and `compute_provenance_hash`)
  is gone with `compute_provenance_hash`, `Resolved.legacy` and the four
  `NORMALIZE_*` and `DIGEST_*` names. Every key changes once (#14):
  - The definition key is the language, the body without its directive lines as
    the language normalizes it, and the key fields.
  - The execution key is `parts_digest(parts)`, the hash of `parts` itself, so a
    component counts with its address: swapping two variables' contents changes
    the key. A host keying a `HostKeyed` cell uses `parts_digest` too.
  - An unreadable output, a no-cache output without a content digest, and an
    in-key annotation that could not be read or encoded (`CellAnalysis.unkeyed`)
    are `Unavailable`, where a key was built without them. `HostKeyed.unresolved` names the upstreams a host-keyed cell could not
    read.
  - An instance is `scope` and `instance` in `parts`, so an `@each` item key may
    contain `:` (a path or a URI), which `derive_subkey` refused.
  - `canonical_json` takes only what the new `is_canonical` accepts, the same rule
    as a key field, so `{1: "a"}` and `{"1": "a"}`, or a tuple and a list, no longer
    share a key. Fan-out and sweep components are JSON lists, not `;`-joined
    strings a run id could forge.
  - `PARTS_SCHEMA` is 2, and the definition key has its own format tag.
  - `strict` is keyword-only, so an old positional call fails loudly.
- `each_reads_source` also covers a source read through a `Reads` effect or named
  like a builtin, which an item key would have left out (#14).

- A language's directives use its own comment marker: an analyzer sets `comment`
  (`"#"` by default), so a SQL cell reads `-- @key value`. The marker replaces `#`
  for that language, where a `#` line is body. `comment_marker(analyzer)` reads it,
  `Grammar(marker=)` and `Grammar.with_marker` build the grammar for one, and
  `Vocabulary.grammar_for(analyzer)` is the grammar `analyze` and the definition key
  use. `Vocabulary.set_directive` and `remove_directive` take `language=`, a
  registered name or the analyzer itself. A `#` language is unchanged.
  `Vocabulary.__or__` keeps a custom `grammar`, which it dropped, and refuses two
  different ones (#13).

## 0.3.0 - 2026-11-02

- `@each <name> in <source> [key=<column>] [glob=<pattern>]`, a core annotation: a
  cell that runs once per item the host lists, from an upstream variable or a name
  the cell binds. `FanOut(over=)` and `graph.each_cells` carry it, and
  `execution_key(instance=Item(key, digest))` keys one item in place of its source, so
  changing one item leaves the others' keys alone. A reader of the cell sees one
  ordinary output. A body reading the source itself is `each_reads_source`, and
  `each_over_sweep`, `each_unknown_source` and `each_on_variant_member` are errors too
  (#11).

## 0.2.0 - 2026-11-01

- A blocked cell and the cells downstream of it are keyed: every cell has a
  definition key, and `execution_key` refuses only a cell a cycle or variant
  collision reaches. `Analysis.variant_read` and `Analysis.shadowed_reads` give the
  rule the key reads its upstreams by, so a host loads what the key covers. An
  upstream's `outputs` is looked up only for the variables the key counts. A
  host-keyed cell's `HostKeyed.parts` holds the upstream components it reads.
  `Resolver`, the type of a `resolve` entry, is exported (#10).
- The Python analyzer and `normalize` remember the last 4096 sources they read, so
  a notebook analyzed again after an edit pays again only for the cells that changed.
  The analyzer hands each caller its own lists (#9).
- `Diagnostic.args`: what the message was built from, such as the cell id an
  `@after` names, so a host can word a finding its own way. It does not count in
  equality or hashing (#7).
- An `@after` problem is on the line that names the id, and a seed error is on the
  line of the annotation that seeded; it had no line (#7).

## 0.1.0 - 2026-11-01

- The package ships `py.typed`, so type checkers read its annotations (#6).
- `directives` on a language analyzer is a bool or a frozenset of keys; a language
  with a set reads only those keys, and its other `# @` lines are plain text.
  `directive_keys(analyzer)` replaces `reads_directives` (#3).
- `Analyzed.diagnostics`: an analyzer reports findings of its own, and an error
  among them blocks the cell and its dependents like a syntax error.
  `Analyzed.error` stays, as the `syntax_error` diagnostic (#3).
- A language analyzer can set `directives = False`, so its `# @` lines (markdown
  headings) are not read as directives (#2).
- A parser or validator reports several problems by raising an `ExceptionGroup` of
  `AnnotationError`s, or returns `Partial(value, errors)` to keep what it could
  read; a validator's error defaults to its annotation's first line (#2).
- The package: the `# @key value` grammar, annotations and vocabularies over the
  ten effects, the core vocabulary, the Python analyzer and the analyzer protocol,
  analysis with partial results and plannability, definition and execution keys
  under the `legacy-1` and `cellgraph-1` formats with versioned key parts, and
  planning by readiness (#1).
