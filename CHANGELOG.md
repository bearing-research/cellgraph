# Changelog

## Unreleased

- `analyze` stays up on odd text, and keys it apart (#22):
  - Lines break only at `\n`, `\r\n` and `\r`, as in Python. A form feed,
    `\u2028` and the others `str.splitlines` also breaks at stay inside the line,
    so `# note\x0cx = 1` (one comment) and `# note\nx = 1` no longer share a
    definition key, nor two strings that differ by one of them.
  - A lone surrogate, as a truncated emoji in a notebook file leaves, is a
    `syntax_error` in Python and is hashed in any language, where `analyze`
    raised `UnicodeEncodeError`.
  - A variant member too deep to parse is `python_too_deep`, where `analyze`
    raised `MemoryError`.
  - `strip_lines`, and Python's fallback for source it cannot parse, keep the
    first line's indentation, as documented, so `"  x = 1"` (an error) and
    `"x = 1"` no longer share a key.

- The Python analyzer finds more of what a cell reads and binds (#21):
  - A class-body annotation or type alias finds the class's own names first. On
    Python 3.14 `class M:` with `class Inner` and `inner: Inner` read `Inner`
    upstream, and on any Python so did `type A = T` for a class-level `T`. So
    does one in a type parameter scope (`type A[U] = T`), and a name the class
    deleted is read from the module.
  - A comprehension's `if` runs before its element, so a walrus there binds
    first: `[y for x in a if (y := x)]` no longer reads `y` upstream, and
    `[y := f(x) for x in a if y]` does.
  - `global np` and then `import numpy as np` in a function defines `np`, and a
    `global x` the function only deletes defines nothing, whatever a scope
    without `global x` binds.
  - An unconditional `del` in a class body unbinds the name there, as at module
    level, so a later read in the body is the module's.

- Execution keys count what each upstream makes, and every output component is
  tagged (#20):
  - An output a cell does not read is keyed as its upstream makes it, a sweep
    member's as its variant's, not by whichever cell defines that name last. A
    cell after a reader changed the reader's key, and a fan-out instance read
    another variant's outputs (any it published beyond its defines, or as one cell
    ref, too). One it seeds from or runs `@after` still counts.
  - A sweep component is `sweep:<group>.<var>`, `["sweep", group, var, members]`,
    so two sweeps that both make `y` no longer share one.
  - A fan-out cell's output with no instances is an `output:`, where it was an
    empty fan-out list that never changed.
  - A no-cache upstream is read only for the variables the cell takes from it, so
    one the cell reads from another producer no longer leaves the key
    `Unavailable`.
  - An output's component is a tagged list: `["provenance", hash]`,
    `["nocache", hash, digest]`, `["content", digest]` or `["run", id]`.
    `Provenance("run:r1")` and `Run("r1")` shared a component, and a content digest
    with a `:` in it (`sha256:...`) made `execution_key` raise.
  - `is_canonical` refuses an int of more than 640 digits, which `json` may not
    write, so such a key field is `<key>_unencodable` where `analyze` raised. The
    bound is fixed, so `sys.set_int_max_str_digits` does not change a key.
  - Every output an upstream publishes still counts, read or not: a read the
    analyzer cannot see stays covered. docs/api.md says so, and what it costs.

- CI runs on Linux, macOS and Windows, and a release is also a GitHub release
  (#19):
  - The suite runs on each of Python 3.12, 3.13 and 3.14 on all three, in CI and
    on the tagged commit before a release.
  - A release installs the built wheel and sdist on all three and runs them
    before it publishes anything.
  - After PyPI, the release workflow makes the GitHub release: the version's
    CHANGELOG section as its notes, the wheel and sdist as its assets, and a build
    provenance attestation for each (`gh attestation verify`). The release is a
    draft until its assets are attached, so it works with immutable releases.
  - No checkout keeps its token in `.git/config`.
  - docs/api.md says how deep a cell can nest before it is `python_too_deep`
    depends on the Python: before 3.14, Windows parses about 3000 levels, Linux
    and macOS past 8000. The tests stay under it.

- Hosts build input components the way cellgraph does (#18):
  - `input_component(kind, name, resolved)` is the component `execution_key` puts
    in `parts["inputs"]`. A host keying a `HostKeyed` cell, or comparing saved
    parts, builds its inputs with it.
  - The `wired:`, `variant:` and `each:` upstream components are canonical JSON
    lists, as `sweep:` and `instances:` are. An item's key and digest were joined
    by `=`, so `Item("a=b", "c")` and `Item("a", "b=c")` shared a component.
  - docs/api.md gives every upstream component's format, and says that an
    inactive variant keeps `plannable` but is refused by `plan` and
    `execution_key`.

- The API is what `cellgraph` exports, and the docs say so (#17):
  - `Validator`, `CellEffects` and `registered_analyzers` are exported.
  - `Analysis.execution_key` and `Analysis.variant_read` have their real
    signatures and return types, not `**kwargs` and `Any`.
  - `HostKeyed.parts` is always a dict, never None.
  - docs/api.md lists every code cellgraph emits with its severity and its
    `args`, and documents `Dag.each_cells`, `VariantGroupResolution`,
    `SweepProducer` and `Analysis.vocabulary`, `analyzers` and `groups`.
  - A release runs CI's own jobs on the tagged commit (ci.yml is reusable), so
    the suite runs on Python 3.12, 3.13 and 3.14 and the pre-commit checks rerun.
    A manual re-publish must be run from the tag, so both workflows are the tag's.
    ty is pinned, and it checks the tests too, at commit time as in CI.
  - Grammars are equal by class, marker and directive pattern, so two packages'
    equal `Grammar("--")` compose with `|`, where distinct objects were refused.

- `analyze` survives large and odd notebooks (#16):
  - Cycle detection no longer recurses, so a chain of thousands of cells analyzes;
    1000 raised `RecursionError`. Blocked cells are found in one pass over the
    strongly connected components, where a cycle's length made it cubic.
  - Each cycle is reported on its own cells; every cycle cell had the first one.
  - Repeated cell ids are a `ValueError`; one cell's analysis replaced the other's.
  - `plan` and `execution_key` refuse an inactive switch member with the new
    `InactiveVariantError`, a `NotPlannableError`: what it reads depends on the
    selection, so a host selects it and analyzes again. Its plan left its upstream
    out, and its key covered none of it.
  - A cell with a member or fan-out conflict joins no group, where the last effect
    won and so composition order decided. A variant member never fans out.
  - Cells sharing a variant name get one `variant_collision` each, on the line of
    the annotation that made them members. A repeat is inactive, where it shadowed
    the active member.
  - An unreadable annotation's default code is `<key>_unreadable` for its own key,
    not an `also_reads` key. An encoder's `AnnotationError` is `<key>_unencodable`,
    where it escaped `analyze`. A `scalar` converter or an encoder that raises
    `ValueError`, `TypeError` or `LookupError` (an encoder, also in a group) is
    unreadable or unencodable; a converter's own `AnnotationError` keeps its code
    and line; any other exception is a bug in it, and propagates. A validator's
    diagnostics carry the cell's id.
  - `bind_conflict`, `bind_read_conflict`, `each_reads_source`, the variant
    diagnostics and the `after_*`, `each_*` and `per_variant_*` ones are on the
    line of the annotation whose effect caused them, a host's included.

- The Python analyzer finds edges it missed, and drops ones it invented (#15):
  - On Python 3.14, a cell with a top-level `await`, `async for` or `async with`
    lost every name its functions and classes read, and their `global` writes.
  - `async for` binds its target; `_ = expr` reads `expr`; `global n; n += 1`
    reads `n`; `df[col] = 1` reads `col`; `del df["c"]` and `del obj.a` mutate.
  - A mutation or a `del` reads the name even when the cell rebinds it later.
  - A class body runs when the class is defined: a name it reads before binding it
    is a module read, and so is one its comprehensions, lambdas or nested classes
    read. A name it binds by `def`, `class` or `import` is its own when mutated,
    as one at module level is the cell's.
  - A lambda's walrus binds nothing at module level.
  - `x: int` alone binds nothing, `except E as e` leaves `e` unbound, also to a
    lambda or def in the handler, and rebinding or mutating `e` there defines
    nothing. A top-level `x = 1; del x` defines no `x`.
  - A cell whose symbol table Python refuses is the warning `python_bodies_unread`,
    where its function bodies' reads were dropped silently.
  - A long expression is analyzed and normalized; one nested deeper than the
    analyzer can walk is the error `python_too_deep`, not a crash of `analyze`.
    The retry under a higher recursion limit starts afresh, and holds a lock, so
    threads analyzing at once leave the limit as they found it.
  - The analyzer's tests from Strata are ported. `python.imported_names`, unused,
    is gone, and `python.CellAnalysis` is `python.CellNames`.

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
    are `Unavailable`, where a key was built without them.
    `HostKeyed.unresolved` names the upstreams a host-keyed cell could not read.
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
