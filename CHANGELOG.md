# Changelog

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
