# The cellgraph API

Everything below is importable from `cellgraph`. The package runs nothing and has
no dependencies: the host executes cells, stores their outputs and resolves their
external inputs, and cellgraph answers what the graph is, what can be planned, and
what each cell's key is.

## Cells and the grammar

```python
Cell(id, source, language="python", meta={})
```

`meta` is the host's, passed untouched to the vocabulary's `cell_effects` hook.

A cell's directives are its leading block of `#` comment lines that read
`# @key value`. Blank lines and other comments may sit in the block, and the block
ends at the first line of body. Keys are lower-cased and values stripped.
`GRAMMAR` (a `Grammar`) parses and edits that block:

| Method | Does |
| ------ | ---- |
| `directives(source, keys=None)` | `[Directive(key, value, line)]`, in source order; only `keys` when given |
| `parse_directive(line)` | `(key, value)` or None |
| `strip(source)` | the source without its leading block |
| `strip_directives(source, keys=None)` | the source without its directive lines (only `keys`' when given); other comments stay |
| `set_directive(source, key, value)` | replace the first `# @key`, drop repeats, or insert one after the last directive; line endings are kept |
| `remove_directive(source, key)` | every `# @key` in the block removed |

## Effects

An annotation acts on the graph only through these ten effects. Names may be given
as a string or an iterable.

| Effect | Meaning |
| ------ | ------- |
| `Binds(names)` | put in the namespace before the body runs; never read from upstream |
| `Defines(names)` | outputs the body does not assign |
| `Reads(names, even_if_defined=False)` | references the body's analysis cannot see; a name the cell defines is added only with `even_if_defined=True` |
| `Keeps(names)` | outputs kept with no downstream reader (a defined name only) |
| `After(cell_ids)` | ordering edges with no variable |
| `Seeds(cell_id, name)` | read `name` from `cell_id` instead of its latest earlier producer |
| `Member(group, name)` | member `name` of variant group `group` |
| `FanOut(group=None, over=None)` | run once per member of `group`, or of the one sweep group the cell reads; with `over`, once per item the host lists from that name |
| `Input(kind, name, spec=None)` | an external input; one per `(kind, name)` in a cell; `spec` is the resolver's and does not affect equality |
| `NoCache()` | never served from cache; a downstream key follows this cell's bytes |

## Annotations and vocabularies

```python
Annotation(
    key, parse=scalar(str), effects=None, in_key=True, encode=None, validate=None, also_reads=()
)
```

- `parse(directives)` gets every directive of `key` and of `also_reads`, in source
  order. It raises `AnnotationError(message, code=None, line=None)` for a value it
  cannot read; the code defaults to `<key>_unreadable` and the line to the
  annotation's first. To report several problems, it raises an `ExceptionGroup` of
  `AnnotationError`s, and each becomes a diagnostic; any other exception in the
  group propagates. A parser that can read some lines and not others returns
  `Partial(value, errors)`: the annotation is present with `value`, and each error
  is a diagnostic.
- `effects(value)` returns effects. It runs only when the annotation is present.
- `in_key`: a present annotation adds the field `key` to the definition key, with
  the value `encode(value)`, or `value` itself when that is canonical (None, bool,
  int, finite float, str, and lists and str-keyed dicts of these). A value that is
  not canonical is the error `<key>_unencodable`.
- `validate(value, ValidationContext(cell, defines, references, host))` returns
  diagnostics. It runs after names are known; an `AnnotationError` it raises (or
  a group of them) becomes `<key>_invalid`, on the annotation's first line unless
  the error names one.

Parsers: `scalar(convert=str)` (the last line wins; empty or unconvertible is an
error), `flag()` (bare, or `on|off|true|false|yes|no|1|0`), and `stacked(item)`
(one value per line). `mark_stacked(parse)` marks a hand-written parser as reading
repeated lines, and `is_stacked(parse)` tells.

```python
Vocabulary(annotations, cell_effects=None, grammar=GRAMMAR)
```

- A directive key claimed twice is refused, and so is a key in both sides of
  `a | b`, so composition order never decides a result. `|` chains the two
  `cell_effects` hooks.
- `cell_effects(meta, parsed)` is called for every cell with the host's `meta` and
  the parsed value of each present annotation. It is for effects that come from
  host configuration, such as mounts declared outside the source.
- `Vocabulary.core()` holds `name` (metadata), `after`, `nocache`,
  `variant <group> <name>`, `per_variant [group]` and
  `each <name> in <source> [key=<column>] [glob=<pattern>]` (in the key).
- `vocab[key]`, `key in vocab`, `vocab.keys` (also-read keys included),
  `vocab.owner(directive_key)`, `vocab.without(*keys)`, `vocab.replace(key, **changes)`.
- `vocab.set_directive(source, key, value)` refuses a stacked annotation's key,
  which a single-line edit would truncate.

## Languages

```python
class LanguageAnalyzer(Protocol):
    owns_key: bool

    def analyze(self, cell) -> Analyzed: ...
    def normalize(self, body: str) -> str: ...
```

An analyzer may also set `directives`:

- `True`, the default: every directive is read.
- `False`: none, for a language whose `# @` lines are prose, such as markdown
  headings.
- A frozenset of keys: only those, e.g. `frozenset({"app"})`.

A `# @` line whose key the language does not read is plain text: no annotation and
no diagnostic, not even `annotation_unknown`, and the `cellgraph-1` normalization
keeps it in the body. `directive_keys(analyzer)` gives the set, or None for every
key; any other value of `directives` is a `TypeError`.

`Analyzed(defines, references, builtin_references, mutation_defines, error,
diagnostics)`.

- `error` is a body that cannot be read, and becomes the error `syntax_error`.
- `diagnostics` is anything else the analyzer finds, such as a host's
  per-language checks that run with or without a directive. They are the cell's
  (their `cell` is set to its id). An error blocks the cell and its dependents,
  exactly as a syntax error does.

`owns_key=False` means the host computes the cell's execution key; such a cell
still gets names, edges, plannability and plan steps, and `execution_key` returns
`HostKeyed`.

- `register_language_analyzer(language, analyzer)` and `get_language_analyzer(language)`.
- `"python"` is registered on import as `PythonAnalyzer`.
- `strip_lines` is the default normalizer.
- A cell whose language has no analyzer raises `UnknownLanguageError` instead of
  silently dropping its references.

## Analysis

```python
analyze(cells, vocabulary, groups=None, normalization="legacy-1",
        digest="legacy-1", strict=False, *, analyzers=None, context=None)
```

`groups` is `GroupConfig(modes={group: "switch" | "sweep"}, active={group: name})`.
`analyzers` replaces the registry for this call; `context` reaches every validator
as `ValidationContext.host`. With `strict=True`, an unknown directive key is an
error rather than a warning.

The phases, each over every cell before the next:

1. **Parse**:
   - every present annotation's parser, encoder and effects, then the hook's
     effects;
   - the language analyzer;
   - unknown keys are `annotation_unknown`, with a close match as a hint.
2. **Names**:
   - `Defines`, then `Reads`, then `Binds`;
   - a bound name leaves the references;
   - the conflicts: `bind_conflict`, `bind_read_conflict`, `seed_conflict`,
     `member_conflict`, `fanout_conflict`, `input_conflict`.
3. **Validate**: each annotation's validator.
4. **Wire**:
   - variant groups are resolved (a name used twice in one group is
     `variant_collision` on both cells), and an inactive switch member is left out;
   - every reference goes to its latest earlier producer, or to every member of a
     sweep;
   - seeds, `after` (`after_self`, `after_unknown_cell`, `after_inactive_variant`)
     and fan-outs (`per_variant_*`, `each_*`) are applied;
   - cycles are `cycle` on every cell on one, with the message
     `Cycle detected in DAG: a → b → a`.

A cell with an error diagnostic is not plannable, and neither is any cell
downstream of it. The analysis is partial, not refused: every other cell is still
wired, planned and keyed.

`Analysis` has:

- `cells`: `{id: CellAnalysis}`.
- `graph`: the `Dag` over every active cell.
- `dag`: the same graph restricted to plannable cells.
- `diagnostics`, `structural_errors` (`cycle` and `variant_collision`), and
  `blocking(cell_id)`: the error diagnostics of the cells that keep `cell_id` from
  planning.
- `execution_key(...)` and `plan(...)`, described below.
- `variant_read(cell_id, upstream_id, var, instance=None)`: what the cell (or one
  of its instances) reads of `var` from `upstream_id` when a sweep makes it, as
  `VariantRead(variants, scalar, fanout)`, or None. `scalar` is an instance's own
  group, read as its variant's value; otherwise the `{variant: value}` dict.
  `fanout` says the variants are one fan-out cell's instances. No variants is
  another variant's member, which the cell does not read. The execution key reads
  by this rule, so a host that loads inputs by it loads what the key covers.
- `shadowed_reads(cell_id)`: the names the cell reads that an upstream defines but
  is not wired to give it; each is read from its wired producer only.

`CellAnalysis` carries, among others:

- `annotations` (the parsed values), `effects`, `defines`, `references`,
  `builtin_references`, `bound`, `inputs`, `nocache`;
- `key_fields`, `diagnostics`, `plannable`, `blocked_by`;
- `normalized` and `definition_key`, for every cell.

`Dag` carries:

- `edges` (`DagEdge(from_cell_id, to_cell_id, variable)`; the variable is empty for
  an ordering edge), `cell_upstream`, `cell_downstream`, `roots`, `leaves`, and
  `topological_order` (Kahn);
- `consumed_variables`, `variable_producer`, `read_producers`, `wired_inputs`,
  `variant_groups`, `inactive_cells`, `fanout_cells`, `shadow_warnings`.

## Keys

### Definition key

```
normalized = legacy-1:     the Python AST round trip of the whole source
                           (line strip when it does not parse), any language
             cellgraph-1:  the language's normalize() of the source without
                           its directive lines
key        = legacy-1:     sha256(normalized [+ "\0" + json(fields, sorted, compact)])
             cellgraph-1:  sha256(canonical_json(["cellgraph-1", "definition",
                                                  normalized, fields]))
```

`fields` is the cell's `key_fields`, and is left out of the legacy text when empty.
`normalization` and `digest` are chosen independently.

### Execution key

```python
analysis.execution_key(cell_id, *, instance=None, published, resolve=None, env_hash)
```

- `published(cell_id) -> Published(outputs, instances, cell_ref)`.
  - `outputs` maps a variable to `OutputRef(identity, content_digest=None)`, or to
    `Unavailable` when it is published but unreadable.
  - A fan-out cell lists each variable in `outputs` and each `(variable, variant)`
    in `instances`.
  - `cell_ref` is the older single cell-level output.
- `identity` is `Provenance(hash)` for an output filed under a reusable key, or
  `Run(id)` for one that had none. Run outputs become `content:<digest>`, or
  `run:<id>` without a digest.
- A `NoCache` upstream is keyed by its bytes, as
  `derive_subkey(hash, "content=<digest>")`, and only for the variables the reader
  reads.
- `resolve[kind](input) -> Resolved(identity, fingerprint, legacy=None)` or
  `Unavailable`; `Resolver` is that callable's type. Under the `legacy-1` digest, `legacy` is the component itself and
  is required.
- `instance` is an `Item(key, digest)` on an `@each` cell: the key is built with this
  item in place of its source, under `each:<source>` in `upstream`, so changing one
  item leaves the others' keys alone. Which items there are, and what a digest covers,
  are the host's. `instance=None` is the cell-level key, over the whole source; a cell
  reading the output sees one ordinary output.
- `instance` names one variant of a fan-out cell. It reads its own group's
  variable as a scalar of that variant, and its key is
  `derive_subkey(cell key, "variant=<name>")` under `legacy-1`.

Results:

- `Keyed(digest, parts)`.
- `Unavailable(reason, parts, unresolved)`: under `cellgraph-1`, an unreadable
  output, a content-keyed output without a digest, or an unresolved input. Under
  `legacy-1`, an unreadable output is skipped, and a missing digest falls back to
  the provenance hash, as Strata does; an unresolved input is unavailable under
  both.
- `HostKeyed(parts)`: for a language with `owns_key=False`. `parts` holds the
  upstream components it reads, for the host's own key, and no inputs.

A blocked cell is keyed too, as are the cells downstream of it: a result cached
under its key is still its result, and whether it may run is `plannable`. A cell a
cycle or variant collision reaches (on it or upstream) raises
`NotPlannableError(cell_id, diagnostics)` with those structural diagnostics.

`outputs` is read lazily: a variable's ref is looked up only when the key counts
it, so a host can pass a mapping that reads its store on demand.

`parts` is versioned by `parts_schema` (`PARTS_SCHEMA`, now 1) and holds:

- `digest`, `scope` (`cell` or `instance`), `instance`, `definition_key`, `fields`,
  `env_hash`;
- `upstream`: `[address, component]` pairs, with addresses `output:`, `cell:`,
  `wired:`, `variant:`, `sweep:`, `instances:` and `each:`;
- `inputs`: `[kind, name, component]`.

```
legacy-1:     compute_provenance_hash(components, definition_key, env_hash)
              = sha256 over sorted components, each followed by "\0", then the
                definition key, "\0", the env hash
cellgraph-1:  sha256(canonical_json(["cellgraph-1", "execution", definition_key,
                                     env_hash, sorted(components), scope, instance]))
```

`derive_subkey(parent, *labels)` is `sha256("parent:label1:...")`, byte-identical
to vestigo's; a label containing `:` is refused. `canonical_json` sorts keys, has
no whitespace, keeps UTF-8 as is, and refuses NaN.

## Planning

```python
analysis.plan(target, ready) -> list[str]
```

The plan is the cells upstream-reachable from `target` in the plannable DAG that
`ready(cell_id)` says are not ready, in topological order, followed by `target`.
Ordering edges count as dependencies, and a fan-out cell is one step. A target that
is not plannable raises `NotPlannableError(cell_id, diagnostics)`.

The plan asks for readiness, not keys: a downstream key can depend on bytes and
inputs that exist only after its upstream runs.

## Diagnostics

`Diagnostic(code, severity, message, cell, annotation=None, line=None, args={})`,
with `Severity.ERROR`, `WARN` or `INFO`. Only an error blocks planning.

`args` holds what cellgraph built the message from, so a host can word its own:
the `key` and `suggestion` of `annotation_unknown`, the `cell_id` an `after_*`
names, the `name`, `cell_id` and `reason` (`unknown`, `self`, `inactive` or
`undefined`) of `seed_unknown_cell`, the `group` and the names of the variant and
fan-out checks, and the `cycle` of a cycle. It does not count in equality.

An `@after` problem is on the first `@after` line that names the id. A conflict or
seed error is on the first line of the annotation whose effect caused it.
