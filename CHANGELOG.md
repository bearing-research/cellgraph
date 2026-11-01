# Changelog

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
