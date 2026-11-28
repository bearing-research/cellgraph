"""The Python language analyzer: defines and references from the AST and symtable.

Module-level code runs in source order, so the AST visitor walks it statement by
statement. A function body runs later, and ``symtable`` gives the module names it
reads; a lambda or a generator is read where it is written, since most run at
once (``sorted(key=lambda ...)``, ``sum(... for ...)``). Names starting with ``_``
are the cell's own and never cross cells. What a star import binds cannot be
known, so it binds nothing here.
"""

from __future__ import annotations

import ast
import builtins
import functools
import keyword
import symtable
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cellgraph.diagnostics import Diagnostic, Severity
from cellgraph.languages import Analyzed, strip_lines

if TYPE_CHECKING:
    from cellgraph.analysis import Cell


@dataclass
class CellNames:
    """One cell's names; ``error`` is None on success."""

    defines: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    # Defines from in-place mutation (``df["col"] = ...``): the cell produces the
    # mutated value even though it never rebinds the name.
    mutation_defines: list[str] = field(default_factory=list)
    # Free names that are builtins, kept out of ``references`` so every
    # ``print``/``len`` caller doesn't list them. A cell can still define
    # ``input = ...``, so the DAG resolves these against producers too: an
    # unshadowed builtin wires nothing, a shadowed one gets its edge.
    builtin_references: list[str] = field(default_factory=list)
    error: str | None = None
    # The cell nests deeper than the analyzer can walk.
    too_deep: bool = False
    # symtable refused the cell, so what its function bodies read is unknown.
    bodies_unread: bool = False
    # What went wrong in a call reader, one line each; the names it gave are dropped.
    reader_failures: list[str] = field(default_factory=list)
    # Defines bound only by an import at the head of the cell, so the same module
    # whatever the cell's inputs: see ``_leading_imports``.
    imports: list[str] = field(default_factory=list)


# The names one call reads that its code does not show (``df.query("a > @mu")`` reads
# ``mu``), or None when the call is not the reader's. A host supplies these.
type CallReader = Callable[[ast.Call], Iterable[str] | None]


def string_literal(node: ast.expr | None) -> str | None:
    """The text of *node* when it is a plain string literal, else None.

    Implicitly concatenated literals (``"a" "b"``) are one literal; an f-string is not.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def at_names(text: str) -> list[str]:
    """The identifiers after ``@`` in *text*, in order and once each.

    Text in ``'...'``, ``"..."`` or backticks is skipped: there ``@`` is a character,
    not a name. Space between ``@`` and the name is allowed. A reader for a library
    that writes names this way (pandas' ``query`` and ``eval``) can use it.
    """
    found: dict[str, None] = {}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            i = _past_quoted(text, i)
            continue
        i += 1
        if ch != "@":
            continue
        while i < n and text[i] in " \t":
            i += 1
        start = i
        if i < n and text[i].isidentifier():
            i += 1
            while i < n and ("a" + text[i]).isidentifier():
                i += 1
            # As Python reads an identifier: ``ﬁ`` is ``fi``.
            name = unicodedata.normalize("NFKC", text[start:i])
            if not keyword.iskeyword(name):
                found[name] = None
    return list(found)


def _past_quoted(text: str, start: int) -> int:
    """The index just past the quoted run opening at *start*; the end when it is unclosed."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\" and quote != "`":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return len(text)


def _identifier(name: object) -> str | None:
    """*name* as Python reads it as an identifier (NFKC), or None when it is not one."""
    if not isinstance(name, str):
        return None
    name = unicodedata.normalize("NFKC", name)
    return name if name.isidentifier() and not keyword.iskeyword(name) else None


def _exception_line(exc: Exception) -> str:
    """The exception's type and the first line of its message, cut short."""
    text = str(exc).partition("\n")[0]
    text = text if len(text) <= 120 else text[:119] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _in_function_bodies(tree: ast.Module) -> set[int]:
    """The ids of the calls in *tree* that sit in a function or method body."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ids.update(
                id(n) for stmt in node.body for n in ast.walk(stmt) if isinstance(n, ast.Call)
            )
    return ids


def _reader_label(reader: CallReader) -> str:
    return getattr(reader, "__qualname__", None) or repr(reader)


def _insert_call_reads(
    tree: ast.Module, readers: Sequence[CallReader]
) -> tuple[list[ast.Call], list[str]]:
    """Write each name a reader gives as a bare name read at its call, in *tree*.

    ``df.query("a > @mu")`` becomes ``df.query("a > @mu", **(mu,))``: the existing
    passes then read ``mu`` with every scope rule that applies there. Returns the calls
    it changed, and what went wrong in the readers.
    """
    failures: list[str] = []
    inserts: list[tuple[ast.Call, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found: dict[str, None] = {}
        for reader in readers:
            try:
                got = reader(node)
                if got is None:
                    continue
                if isinstance(got, (str, bytes)):
                    raise TypeError(f"returned {type(got).__name__}, not an iterable of names")
                given = list(got)
            except Exception as exc:  # a host's reader must not take the analysis down
                failures.append(f"{_reader_label(reader)} raised {_exception_line(exc)}")
                continue
            for given_name in given:
                name = _identifier(given_name)
                if name is not None:
                    found[name] = None
                else:
                    failures.append(
                        f"{_reader_label(reader)} returned {given_name!r:.80}, not a name"
                    )
        if found:
            inserts.append((node, list(found)))
    for call, names in inserts:
        loads: list[ast.expr] = [ast.Name(id=name, ctx=ast.Load()) for name in names]
        call.keywords.append(ast.keyword(arg=None, value=ast.Tuple(elts=loads, ctx=ast.Load())))
    return [call for call, _ in inserts], list(dict.fromkeys(failures))


# Walking the AST recurses once per level; a long ``a + a + ...`` chain is that deep.
_DEEP_RECURSION_LIMIT = 100_000


# The limit is the process's: one retry at a time, so two cannot restore each other's.
# Other threads run under the raised limit meanwhile; a nested retry re-enters.
_DEEP_LOCK = threading.RLock()


def _deeply[T](fn: Callable[[], T]) -> T:
    """``fn()``, retried once under a higher recursion limit on RecursionError.

    *fn* must start afresh each call: the failed attempt may have left state behind.
    """
    try:
        return fn()
    except RecursionError:
        pass
    with _DEEP_LOCK:
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old, _DEEP_RECURSION_LIMIT))
        try:
            return fn()
        finally:
            sys.setrecursionlimit(old)


def _collect_name_targets(target: ast.expr, out: set[str]) -> None:
    """Recursively collect Name ids from an assignment target.

    Handles ``x``, tuple/list unpacking and ``*rest``. Subscript and attribute
    targets are mutations and are ignored here.
    """
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_name_targets(elt, out)
    elif isinstance(target, ast.Starred):
        _collect_name_targets(target.value, out)


def _leading_imports(module: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """The imports at the head of *module*, before any other statement but a docstring.

    A statement before an import may change what it finds (``sys.path.insert``,
    ``os.chdir``), so only these are taken to give the same module every run.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    for index, stmt in enumerate(module.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            found.append(stmt)
        elif not (
            index == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            break
    return found


def _declared_global(tree: ast.Module) -> set[str]:
    """Every name a ``global`` statement anywhere in *tree* declares."""
    return {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}


def _type_param_names(params: list[ast.type_param]) -> set[str]:
    """Names bound by PEP 695 type parameters (``[T, *Ts, **P]``)."""
    return {p.name for p in params if isinstance(p, (ast.TypeVar, ast.TypeVarTuple, ast.ParamSpec))}


def _has_inplace_true(keywords: list[ast.keyword]) -> bool:
    """Return whether a call's keywords contain a literal ``inplace=True``."""
    return any(
        kw.arg == "inplace" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in keywords
    )


class VariableAnalyzer(ast.NodeVisitor):
    """AST visitor that collects defined and referenced variables."""

    def __init__(self):
        """Initialize the analyzer."""
        self.defines: set[str] = set()
        self.references: set[str] = set()
        # Names bound other than by mutation (``x = ...``, def, class, import):
        # ``df = ...`` then ``df["col"] = ...`` in one cell is a local define, not
        # a mutation of an upstream.
        self.pure_defines: set[str] = set()
        # Subscript/attribute mutations. Unlike pure rebinds these stay in
        # references: the cell depends on an upstream producer.
        self.mutation_defines: set[str] = set()
        # Names read before the cell binds them (``df = df.dropna()``,
        # ``print(x); x = 5``): genuine upstream references that survive the
        # pure-define filter.
        self.read_before_define: set[str] = set()
        # Names bound so far in source order: tells ``x = 0\nx += 1``
        # (local) from a bare ``x += 1`` (upstream read).
        self._defined_so_far: set[str] = set()
        self._local_vars: set[str] = set()  # Track local scope variables
        # Inside a lambda body, where a walrus binds the lambda's own name; those
        # names, which a comprehension in the lambda must not drop on its way out.
        self._in_lambda = False
        self._lambda_walrus: set[str] = set()
        # Names a top-level ``del`` unbound; those not bound again are not defines.
        # A ``del`` under ``if`` or ``try`` may not run, so it unbinds nothing.
        self.deleted: set[str] = set()
        self._unconditional = False
        # Names a class body deletes and does not bind again, in any class.
        self.class_unbound: set[str] = set()
        # A class body's analyzer: names it reads that resolve to module globals
        # whatever the class binds (a comprehension's elements, a nested class).
        self._in_class = False
        self._global_scope = False
        self.global_reads: set[str] = set()
        # PEP 563 stringifies annotations, so under it they are not
        # runtime references.
        self._future_annotations: bool = False
        # Names an import at the head of the cell binds, and names bound or unbound any
        # other way (another import, an assignment, a mutation, a ``del``, an ``except
        # E as name``): ``imports`` is the first less the second.
        self.top_imported: set[str] = set()
        self.bound_otherwise: set[str] = set()
        self._top_imports: set[int] = set()

    def visit_Module(self, node: ast.Module) -> None:
        """Visit module: process top-level statements."""
        self._future_annotations = any(
            isinstance(s, ast.ImportFrom)
            and s.module == "__future__"
            and any(alias.name == "annotations" for alias in s.names)
            for s in node.body
        )
        self._top_imports = {id(s) for s in _leading_imports(node)}
        self._visit_body(node.body)

    def _visit_body(self, body: list[ast.stmt]) -> None:
        """Visit a module or class body; only its own top-level ``del`` unbinds."""
        for stmt in body:
            self._unconditional = isinstance(stmt, ast.Delete)
            self.visit(stmt)
            self._unconditional = False

    def _read(self, name: str) -> None:
        """Record a read of *name*, at this point of the cell."""
        if name in self._local_vars:
            return
        if self._global_scope:
            self.global_reads.add(name)
            return
        self.references.add(name)
        if name not in self._defined_so_far:
            self.read_before_define.add(name)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Handle: x = ... or x, y = ..."""
        # The RHS runs before the targets bind, so ``x = x + 1`` and swaps like
        # ``a, b = b, a`` read the upstream value; ``_ = expr`` still runs expr.
        self.visit(node.value)
        for target in node.targets:
            self._add_assign_target(target)
            _collect_name_targets(target, self._defined_so_far)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Handle: x += ... or df["col"] += ...

        The LHS is implicitly read, so a Name target is added to references
        (unless the cell already pure-defined it earlier); without that the DAG
        misses the upstream edge and the cell hits NameError. Subscript and
        attribute targets go through ``_add_assign_target``.
        """
        if isinstance(node.target, ast.Name):
            self._read(node.target.id)
        self._add_assign_target(node.target)
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._defined_so_far.add(node.target.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Handle: x: int = ... or x: int (without value, which binds nothing)."""
        if node.value is not None:
            self._add_assign_target(node.target)
        elif not isinstance(node.target, ast.Name):
            self._add_reference_target(node.target)  # ``obj.a: int`` evaluates obj
        # Module-scope annotations are evaluated at runtime unless PEP 563. Python
        # 3.14 defers them (PEP 649), but the kernel may run an older Python, so they
        # count as reads: at worst an extra edge.
        if not self._future_annotations:
            self.visit(node.annotation)
        if node.value:
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                self._defined_so_far.add(node.target.id)

    def visit_Call(self, node: ast.Call) -> None:
        """Treat ``X.method(..., inplace=True)`` as a mutation of ``X``.

        Otherwise ``df`` looks read-only, and in the shared-namespace batch path
        a downstream cell sees the mutated object while the stored artifact holds
        the old value. Only a literal ``inplace=True`` on an attribute call with a
        name/attribute receiver counts.
        """
        if isinstance(node.func, ast.Attribute) and _has_inplace_true(node.keywords):
            self._add_reference_target(node.func.value)
            self._add_mutation_define(node.func.value)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Handle: def f(): the name is defined, the body is a nested scope.

        Decorators, defaults and annotations (unless PEP 563 is on) evaluate at
        module load, so their free names are module references. Body free names
        come from the symtable pass.
        """
        self._bind(node.name)
        self._visit_function_signature(node)
        self._defined_so_far.add(node.name)
        # Don't recurse into function body (it's a nested scope)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Handle: async def f(): ..."""
        self._bind(node.name)
        self._visit_function_signature(node)
        self._defined_so_far.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Handle: class C: the name is defined, the body is a nested scope.

        Decorators, bases and class keywords evaluate at module load and are
        walked here; body free names come from the symtable pass.
        """
        self._bind(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        old_local_vars = self._local_vars
        self._local_vars = self._local_vars | _type_param_names(node.type_params)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        # The body runs now, in order, in a namespace of its own: a name it reads
        # before binding it there is a module read. Method bodies are symtable's.
        body = VariableAnalyzer()
        body._future_annotations = self._future_annotations
        body._local_vars = set(self._local_vars)
        body._in_class = True
        body._visit_body(node.body)
        # A name the class deletes is looked up past it, in the module.
        self.class_unbound |= body.class_unbound | (body.deleted - body._defined_so_far)
        self._local_vars = old_local_vars
        # A class body cannot see an enclosing class's names: a nested class reads globals.
        old_global = self._global_scope
        self._global_scope = old_global or self._in_class
        for name in sorted(body.read_before_define | body.global_reads):
            self._read(name)
        self._global_scope = old_global
        for name in body.mutation_defines - body.pure_defines:
            self.defines.add(name)
            self.mutation_defines.add(name)
            self.bound_otherwise.add(name)
        self._defined_so_far.add(node.name)

    def _visit_function_signature(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        """Visit decorators, defaults and (without PEP 563) annotations of a function."""
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        if not self._future_annotations:
            # PEP 695 type parameters are local to the signature's annotations.
            old_local_vars = self._local_vars
            self._local_vars = self._local_vars | _type_param_names(node.type_params)
            if node.returns is not None:
                self.visit(node.returns)
            for arg_list in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs):
                for arg in arg_list:
                    if arg.annotation is not None:
                        self.visit(arg.annotation)
            if node.args.vararg is not None and node.args.vararg.annotation is not None:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
                self.visit(node.args.kwarg.annotation)
            self._local_vars = old_local_vars

    def visit_Import(self, node: ast.Import) -> None:
        """Handle: import foo, import foo as bar, or import foo.bar (binds ``foo``)."""
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name.split(".")[0]
            self._bind(name, top_import=id(node) in self._top_imports)
            self._defined_so_far.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Handle: from foo import bar or from foo import *."""
        for alias in node.names:
            if alias.name == "*":
                # Star imports: defines are unknowable, skip.
                pass
            else:
                name = alias.asname if alias.asname else alias.name
                self._bind(name, top_import=id(node) in self._top_imports)
                self._defined_so_far.add(name)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        """Handle ``for x in ...`` and ``async for``: x is defined at top level."""
        self._add_assign_target(node.target)
        self.visit(node.iter)
        _collect_name_targets(node.target, self._defined_so_far)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        """Handle ``with ... as x``: x is defined at top level."""
        for item in node.items:
            if item.optional_vars:
                self._add_assign_target(item.optional_vars)
            self.visit(item.context_expr)
            if item.optional_vars:
                _collect_name_targets(item.optional_vars, self._defined_so_far)
        for stmt in node.body:
            self.visit(stmt)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Handle ``except E as e``: Python unbinds e when the handler ends, so it is
        the handler's, like a lambda parameter."""
        if node.type:
            self.visit(node.type)
        old_local_vars = self._local_vars
        if node.name:
            # Unbound when the handler ends, whatever it held before.
            if node.name not in self._local_vars:
                self.bound_otherwise.add(node.name)
            self._local_vars = self._local_vars | {node.name}
        for stmt in node.body:
            self.visit(stmt)
        self._local_vars = old_local_vars

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """Handle: walrus operator := at top level, or in a lambda (the lambda's name)."""
        self.visit(node.value)
        if not isinstance(node.target, ast.Name):
            return
        if self._in_lambda:
            self._lambda_walrus.add(node.target.id)
            self._local_vars = self._local_vars | {node.target.id}
            return
        self._bind(node.target.id)
        self._defined_so_far.add(node.target.id)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        """Handle: type A = ... (PEP 695); the lazy value's names come from symtable."""
        self._add_assign_target(node.name)
        _collect_name_targets(node.name, self._defined_so_far)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        """Handle: case [x] / case P() as x: the capture binds x."""
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name is not None:
            self._bind_capture(node.name)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        """Handle: case [first, *rest]."""
        if node.name is not None:
            self._bind_capture(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        """Handle: case {"k": v, **rest}."""
        for key in node.keys:
            self.visit(key)
        for pattern in node.patterns:
            self.visit(pattern)
        if node.rest is not None:
            self._bind_capture(node.rest)

    def _bind_capture(self, name: str) -> None:
        self._bind(name)
        self._defined_so_far.add(name)

    def _bind(self, name: str, *, top_import: bool = False) -> None:
        """A binding that is not a mutation: def, class, import, walrus, a capture.

        A local name (``e`` in its ``except E as e`` handler) is unbound when the
        handler ends, so rebinding it there defines nothing.
        """
        if name in self._local_vars:
            return
        self.defines.add(name)
        self.pure_defines.add(name)
        (self.top_imported if top_import else self.bound_otherwise).add(name)

    def visit_Delete(self, node: ast.Delete) -> None:
        """Handle ``del x``: x is referenced but not defined."""
        for target in node.targets:
            self._add_delete_target(target)

    def _add_delete_target(self, target: ast.expr) -> None:
        """Read the root of a ``del`` target; ``del x[k]`` and ``del x.a`` mutate x."""
        if isinstance(target, ast.Name):
            self._read(target.id)
            if target.id not in self._local_vars:
                self.bound_otherwise.add(target.id)  # under ``if`` too: it may be unbound
            if self._unconditional:
                self._defined_so_far.discard(target.id)
                self.deleted.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._add_delete_target(elt)
        elif isinstance(target, ast.Subscript):
            self._add_reference_target(target)
            self._add_mutation_define(target.value)
        elif isinstance(target, ast.Attribute):
            self._add_reference_target(target.value)
            self._add_mutation_define(target.value)

    def visit_Name(self, node: ast.Name) -> None:
        """Collect referenced names (Load context)."""
        if isinstance(node.ctx, ast.Load):
            self._read(node.id)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """``[elt for x in iter if cond]``: loop targets are local; the first iter is outer."""
        self._visit_comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """``{elt for x in iter}``."""
        self._visit_comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """``{k: v for x in iter}``: both key and value are scoped."""
        self._visit_comprehension(node, [node.key, node.value])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """``(elt for x in iter)``."""
        self._visit_comprehension(node, [node.elt])

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        elements: list[ast.expr],
    ) -> None:
        """Walk comprehension parts with loop variables locally scoped.

        The first iterable runs in the outer scope; later iterables, elements
        and ``if`` clauses see the loop variables. Python 3.13 inlines
        comprehensions (PEP 709), so symtable has no child scope for them and
        this AST tracking is what finds their free names.
        """
        if not node.generators:
            return

        # First generator's iter runs in the OUTER scope.
        self.visit(node.generators[0].iter)

        old_local_vars = self._local_vars
        old_global = self._global_scope
        # In a class body, the rest cannot see the class's names: they are globals.
        self._global_scope = old_global or self._in_class
        try:
            local_vars = set(self._local_vars)
            for gen in node.generators:
                _collect_name_targets(gen.target, local_vars)
            self._local_vars = local_vars

            # In the order they run: each generator's iter (the first's is above) and
            # ifs, then the element, so a walrus in an if binds before the element reads.
            for index, gen in enumerate(node.generators):
                if index:
                    self.visit(gen.iter)
                for cond in gen.ifs:
                    self.visit(cond)
            for elt in elements:
                self.visit(elt)
        finally:
            # A walrus in a lambda's comprehension binds in the lambda, which outlives it.
            self._local_vars = old_local_vars | self._lambda_walrus
            self._global_scope = old_global

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Lambda: its defaults run now, and its body is read here with its parameters local.

        Most lambdas are called at once, so a name the cell binds after one is still
        read from upstream: an extra edge at worst, where a missed one runs too early.
        """
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        args = node.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        params |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        saved = self._local_vars, self._global_scope, self._in_lambda, self._lambda_walrus
        self._local_vars = self._local_vars | params
        # In a class body, a lambda cannot see the class's names either.
        self._global_scope = saved[1] or self._in_class
        self._in_lambda, self._lambda_walrus = True, set()
        try:
            self.visit(node.body)
        finally:
            self._local_vars, self._global_scope, self._in_lambda, self._lambda_walrus = saved

    def _add_assign_target(self, target: ast.expr) -> None:
        """Add the names an assignment target defines.

        Subscript (``df["col"]``) and attribute (``obj.attr``) targets define
        their root name.
        """
        if isinstance(target, ast.Name):
            self._bind(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._add_assign_target(elt)
        elif isinstance(target, ast.Subscript):
            # df["col"] = ...: reads df and produces the mutated df, so
            # downstream reads route through this cell (else they can run
            # before the mutation and KeyError). The index is read too.
            self._add_reference_target(target)
            self._add_mutation_define(target.value)
        elif isinstance(target, ast.Attribute):
            # obj.attr = ...: same as subscript mutation.
            self._add_reference_target(target.value)
            self._add_mutation_define(target.value)
        elif isinstance(target, ast.Starred):
            self._add_assign_target(target.value)

    def _add_reference_target(self, node: ast.expr) -> None:
        """Read the root name of an attribute/subscript target, and every index on the way."""
        if isinstance(node, ast.Name):
            self._read(node.id)
        elif isinstance(node, ast.Attribute):
            self._add_reference_target(node.value)
        elif isinstance(node, ast.Subscript):
            self._add_reference_target(node.value)
            self.visit(node.slice)
        else:
            self.visit(node)  # ``f()[k] = 1`` reads what f() reads

    def _add_mutation_define(self, node: ast.expr) -> None:
        """Record the root name of a mutated target as a define.

        The mutating cell produces the post-mutation value downstream cells see.
        The name also stays in references (the mutation reads the prior value);
        ``mutation_defines`` tells the caller not to strip it from them.
        """
        if isinstance(node, ast.Name):
            if node.id in self._local_vars:
                return  # a handler's own ``e``, gone when the handler ends
            self.defines.add(node.id)
            self.mutation_defines.add(node.id)
            self.bound_otherwise.add(node.id)
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            self._add_mutation_define(node.value)


_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
)


class _ForSymtable(ast.NodeTransformer):
    """The module rewritten for ``symtable``, outside function bodies, changing no scope
    a reference resolves through.

    - ``await``, ``async for``, ``async with`` and async comprehensions become
      synchronous: Python 3.14's symtable refuses them at module level.
    - ``except E as e`` binds ``e`` for its handler only. symtable sees a module
      ``e``, so in a handler holding a nested scope ``e`` is renamed to a private
      name, which a reference never crosses cells by.
    """

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        # Decorators, defaults and annotations run here; the body is the function's.
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args = self.visit(node.args)
        if node.returns is not None:
            node.returns = self.visit(node.returns)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        node.args = self.visit(node.args)
        return node

    def visit_Await(self, node: ast.Await) -> ast.AST:
        return self.visit(node.value)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        self.generic_visit(node)
        loop = ast.For(target=node.target, iter=node.iter, body=node.body, orelse=node.orelse)
        return ast.copy_location(loop, node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:
        self.generic_visit(node)
        return ast.copy_location(ast.With(items=node.items, body=node.body), node)

    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        self.generic_visit(node)
        node.is_async = 0
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
        self.generic_visit(node)
        if node.name and _holds_a_scope(node):
            renamer = _Rename(node.name, f"_cellgraph_{node.name}")
            node.body = [renamer.visit(stmt) for stmt in node.body]
            node.name = renamer.new
        return node


def _holds_a_scope(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, _SCOPES) for stmt in handler.body for n in ast.walk(stmt))


class _Rename(ast.NodeTransformer):
    """Every binding and use of one name renamed, in every scope: scoping is unchanged."""

    def __init__(self, old: str, new: str) -> None:
        self.old, self.new = old, new

    def _swap(self, name: str | None) -> str | None:
        return self.new if name == self.old else name

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self._swap(node.id) or node.id
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = self._swap(node.arg) or node.arg
        self.generic_visit(node)
        return node

    def visit_alias(self, node: ast.alias) -> ast.AST:
        if node.asname == self.old or (node.asname is None and node.name == self.old):
            node.asname = self.new
        return node

    def _named(self, node: ast.AST, attr: str) -> ast.AST:
        setattr(node, attr, self._swap(getattr(node, attr)))
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.AST) -> ast.AST:
        return self._named(node, "name")

    visit_AsyncFunctionDef = visit_ClassDef = visit_ExceptHandler = visit_FunctionDef
    visit_MatchAs = visit_MatchStar = visit_TypeVar = visit_FunctionDef

    def visit_MatchMapping(self, node: ast.AST) -> ast.AST:
        return self._named(node, "rest")

    def visit_Global(self, node: ast.Global | ast.Nonlocal) -> ast.AST:
        node.names = [self._swap(n) or n for n in node.names]
        return node

    visit_Nonlocal = visit_Global


def _needs_rewrite(tree: ast.Module) -> bool:
    """Whether symtable must read the rewrite: module-level async, or a handler to rename."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend([*node.decorator_list, node.args])
            if node.returns is not None:
                stack.append(node.returns)
            continue
        if isinstance(node, ast.Lambda):
            stack.append(node.args)
            continue
        if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            return True
        if isinstance(node, ast.comprehension) and node.is_async:
            return True
        if isinstance(node, ast.ExceptHandler) and node.name and _holds_a_scope(node):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _symbol_tables(source: str, tree: ast.Module) -> list[symtable.SymbolTable] | None:
    """The module's symbol table: of *source* itself, or of its rewrite when it needs one.

    None when it cannot be built, and then nothing a function body reads is known.
    """

    def rewritten() -> str:
        module = _ForSymtable().visit(ast.parse(source))
        return ast.unparse(ast.fix_missing_locations(module))

    try:
        text = _deeply(rewritten) if _needs_rewrite(tree) else source
        return [symtable.symtable(text, "<cell>", "exec")]
    except UNPARSEABLE:
        return None


def _body_refs(tables: list[symtable.SymbolTable], class_unbound: set[str]) -> set[str]:
    """Names read in function, lambda and class bodies that resolve to module globals.

    A symbol does iff it is referenced, ``is_global()``, and not local, a parameter
    or a closure variable (``is_free()`` resolves via enclosing scopes). The caller
    filters builtins, privates and defines.
    """
    refs: set[str] = set()
    none: frozenset[str] = frozenset()
    by_class: dict[int, frozenset[str]] = {}
    stack = [(child, none) for table in tables for child in table.get_children()]
    while stack:
        scope, inherited = stack.pop()
        # An annotation or type-alias scope in a class body (and one nested in its type
        # parameters) looks names up in the class namespace first; the class body's
        # own walk reads those names already. A name the class deletes is not there.
        class_names = none
        if scope.get_type() == "class":
            key = id(scope)
            if key not in by_class:
                local = {sym.get_name() for sym in scope.get_symbols() if sym.is_local()}
                by_class[key] = frozenset(local - class_unbound)
            for child in scope.get_children():
                stack.append((child, by_class[key]))
            inherited = none
        elif "__classdict__" in scope.get_identifiers():
            class_names = inherited
        for sym in scope.get_symbols():
            if (
                sym.is_referenced()
                and sym.is_global()
                and not sym.is_local()
                and not sym.is_parameter()
                and not sym.is_free()
                and sym.get_name() not in class_names
            ):
                refs.add(sym.get_name())
        if scope.get_type() != "class":
            stack.extend((child, class_names) for child in scope.get_children())
    return refs


def _free_only_by_insertion(
    inserted: list[symtable.SymbolTable], original: list[symtable.SymbolTable]
) -> set[str]:
    """Names free in a scope only because a call reader's name was inserted there.

    A reader's library looks the name up in the calling frame, then its globals. An
    enclosing function's variable is in that frame only when the code captures it, so
    one the code does not capture is read from the module.
    """
    found: set[str] = set()
    stack = [
        (a, b)
        for ta, tb in zip(inserted, original)
        for a, b in zip(ta.get_children(), tb.get_children())
    ]
    while stack:
        a, b = stack.pop()
        if a.get_name() != b.get_name() or a.get_type() != b.get_type():
            continue  # the scopes no longer line up; read nothing more from here
        known = set(b.get_identifiers())
        for sym in a.get_symbols():
            name = sym.get_name()
            if sym.is_free() and not (name in known and b.lookup(name).is_free()):
                found.add(name)
        stack.extend(zip(a.get_children(), b.get_children()))
    return found


def _global_writes(
    tables: list[symtable.SymbolTable], tree: ast.Module
) -> tuple[set[str], set[str]]:
    """Names a function binds at module scope through ``global X; X = ...``.

    Returns ``(writes, writes_with_reads)``; the second are also read in the function
    and stay in references. A bare ``global X`` and ``nonlocal`` bind nothing here.
    """
    writes: set[str] = set()
    read_writes: set[str] = set()
    deleted_only: set[str] | None = None
    stack = [child for table in tables for child in table.get_children()]
    while stack:
        scope = stack.pop()
        for sym in scope.get_symbols():
            # symtable counts a del as an assignment, and an import as neither.
            bound = sym.is_assigned() or sym.is_imported()
            if not (bound and sym.is_declared_global()):
                continue
            if deleted_only is None:
                deleted_only = _deleted_only(tree)
            if sym.get_name() not in deleted_only:
                writes.add(sym.get_name())
                if sym.is_referenced():
                    read_writes.add(sym.get_name())
        stack.extend(scope.get_children())
    return writes, read_writes


def _deleted_only(tree: ast.Module) -> set[str]:
    """Names a scope declares ``global`` and deletes, that no such scope binds.

    A binding in a scope without the ``global`` is that scope's own.
    """
    deleted: set[str] = set()
    bound: set[str] = set()
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for scope in ast.walk(tree):
        if not isinstance(scope, scopes):
            continue
        declared: set[str] = set()
        own_deleted: set[str] = set()
        own_bound: set[str] = set()
        stack: list[ast.AST] = list(scope.body)
        while stack:
            node = stack.pop()
            if isinstance(node, scopes):
                own_bound.add(node.name)  # its body is its own scope
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Name):
                (own_deleted if isinstance(node.ctx, ast.Del) else own_bound).add(node.id)
            elif isinstance(node, ast.alias):
                own_bound.add((node.asname or node.name).partition(".")[0])
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                own_bound.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                own_bound.add(node.rest)
            stack.extend(ast.iter_child_nodes(node))
        deleted |= declared & own_deleted
        bound |= declared & own_bound
    return deleted - bound


def _global_augmented(tree: ast.Module) -> set[str]:
    """Names a function declares ``global`` and updates with ``x += ...``.

    symtable does not count an augmented target as referenced, but it is read.
    """
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared: set[str] = set()
        augmented: set[str] = set()
        stack: list[ast.AST] = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue  # its own scope, walked on its own
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                augmented.add(node.target.id)
            stack.extend(ast.iter_child_nodes(node))
        found |= declared & augmented
    return found


# What parsing raises for text Python cannot read: bad syntax, a lone surrogate (a
# ValueError), or nesting past the parser.
UNPARSEABLE = (SyntaxError, ValueError, RecursionError, MemoryError)

_BUILTIN_NAMES = frozenset(dir(builtins)) | {"__name__", "__file__", "__doc__", "__package__"}


def analyze_cell(source: str, *, call_readers: Sequence[CallReader] = ()) -> CellNames:
    """Analyze a cell's source and extract its defines and references.

    A name a call reader gives is read at its call, after the call's arguments, as a
    bare name written there would be; in a function body, unless that function binds
    it, it is read from the module.
    """
    if not source or not source.strip():
        return CellNames(defines=[], references=[])

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return CellNames(defines=[], references=[], error=f"Syntax error: {e.msg}")
    except ValueError:
        # A lone surrogate, as a truncated emoji in a notebook file leaves.
        return CellNames(defines=[], references=[], error="Syntax error: not UTF-8 text")
    except (RecursionError, MemoryError):
        return CellNames(too_deep=True)

    # The readers' names go into a tree of their own: the global-write passes read
    # the cell as written.
    read_tree, read_source, reader_failures = tree, source, []
    rewritten = False
    if call_readers:
        try:
            read_tree = ast.parse(source)
            inserted, reader_failures = _insert_call_reads(read_tree, call_readers)
        except (RecursionError, MemoryError):
            return CellNames(too_deep=True)
        if not inserted:
            read_tree = tree
        else:
            try:
                read_source, rewritten = _deeply(lambda: ast.unparse(read_tree)), True
            except (ValueError, RecursionError, MemoryError):
                # An integer too long for ``str`` cannot be written back: the module
                # level still reads the names, symtable reads the cell as written.
                in_bodies = _in_function_bodies(read_tree)
                if any(id(call) in in_bodies for call in inserted):
                    reader_failures.append(
                        "the cell could not be rewritten, so names its function bodies' "
                        "calls read are missing"
                    )

    # The visitor stops at function bodies; symtable covers those.
    def visited() -> VariableAnalyzer:
        analyzer = VariableAnalyzer()
        analyzer.visit(read_tree)
        return analyzer

    try:
        analyzer = _deeply(visited)
    except RecursionError:
        return CellNames(too_deep=True)

    found = _symbol_tables(read_source, read_tree)
    tables = found or []
    nested_refs = _body_refs(tables, analyzer.class_unbound)
    if found is not None and rewritten:
        original = _symbol_tables(source, tree)
        if original is not None:
            nested_refs |= _free_only_by_insertion(found, original)

    # ``global`` writes inside functions are module defines too.
    # ``global_read_writes`` are also read there, so they stay in references.
    global_writes, global_read_writes = _global_writes(tables, tree)
    augmented = _global_augmented(tree)
    global_read_writes |= augmented
    nested_refs |= augmented

    bound = [v for v in (analyzer.defines | global_writes) if not v.startswith("_")]
    # ``x = 1; del x`` leaves no x behind, though its own reads of x stay local; a
    # function's ``global x`` write may still bind it later.
    unbound = analyzer.deleted - analyzer._defined_so_far - global_writes
    defines = [v for v in bound if v not in unbound]
    # A pure assignment in the same cell supersedes a mutation-define.
    effective_mutation_defines = {
        v
        for v in (analyzer.mutation_defines | global_read_writes)
        if not v.startswith("_") and v in set(defines) and v not in analyzer.pure_defines
    }
    # Pure defines are filtered from references (intra-cell rebinds);
    # mutation-defines and reads before the cell's own binding are kept.
    pure_defined_names = set(bound) - effective_mutation_defines - analyzer.read_before_define
    combined_refs = set(analyzer.references) | nested_refs
    references = [
        v
        for v in combined_refs
        if not v.startswith("_") and v not in _BUILTIN_NAMES and v not in pure_defined_names
    ]
    builtin_references = [
        v
        for v in combined_refs
        if not v.startswith("_") and v in _BUILTIN_NAMES and v not in pure_defined_names
    ]

    # A function's ``global`` may bind or delete the name whenever it is called.
    imports = (set(defines) & analyzer.top_imported) - (
        analyzer.bound_otherwise | effective_mutation_defines | _declared_global(tree)
    )

    return CellNames(
        defines=sorted(set(defines)),
        references=sorted(set(references)),
        mutation_defines=sorted(effective_mutation_defines),
        builtin_references=sorted(set(builtin_references)),
        error=None,
        bodies_unread=found is None,
        reader_failures=reader_failures,
        imports=sorted(imports),
    )


# A notebook is analyzed again on every edit with most cells unchanged, so the
# per-source work is kept for the sources seen last.
_MEMO_SIZE = 4096


@functools.lru_cache(maxsize=_MEMO_SIZE)
def _analyzed(source: str) -> CellNames:
    return analyze_cell(source)


# Keyed by the readers too, so analyzers built afresh for each ``analyze`` still share it.
@functools.lru_cache(maxsize=_MEMO_SIZE)
def _analyzed_with(source: str, readers: tuple[CallReader, ...]) -> CellNames:
    return analyze_cell(source, call_readers=readers)


def normalize(source: str) -> str:
    """Canonical text for *source*: its AST round trip, so comments and layout drop out.

    Source that does not parse, or that the round trip cannot write back (nested too
    deep, an integer too long for ``str``), falls back to the line strip, which
    keeps comments.
    """
    return _normalized(source)


@functools.lru_cache(maxsize=_MEMO_SIZE)
def _normalized(source: str) -> str:
    try:
        return _deeply(lambda: ast.unparse(ast.parse(source)))
    except UNPARSEABLE:
        return strip_lines(source)


class PythonAnalyzer:
    """The Python language analyzer: ``analyze_cell`` over the cell body.

    *call_readers* are the host's: each says which names a call reads that its code
    does not show. A reader is called once per call in a cell, and its answer for a
    source is memoized, so it must depend on the call alone.
    """

    owns_key = True

    def __init__(self, call_readers: Iterable[CallReader] = ()) -> None:
        readers = tuple(call_readers)
        for reader in readers:
            if not callable(reader):
                raise TypeError(f"a call reader must be callable, not {reader!r}")
            try:
                hash(reader)
            except TypeError:
                raise TypeError(f"a call reader must be hashable, not {reader!r}") from None
        self._call_readers = readers

    @property
    def call_readers(self) -> tuple[CallReader, ...]:
        return self._call_readers

    def analyze(self, cell: Cell) -> Analyzed:
        # Memoized: the lists are copied, so the caller may change its own.
        readers = self._call_readers
        result = _analyzed_with(cell.source, readers) if readers else _analyzed(cell.source)
        diagnostics = []
        if result.too_deep:
            diagnostics.append(
                Diagnostic(
                    "python_too_deep",
                    Severity.ERROR,
                    "the cell nests deeper than the analyzer can walk, so its names are "
                    "unknown; split the expression",
                    cell.id,
                )
            )
        if result.bodies_unread:
            diagnostics.append(
                Diagnostic(
                    "python_bodies_unread",
                    Severity.WARN,
                    "the names this cell's functions and classes read could not be found, "
                    "so the cell may run before the cells that define them",
                    cell.id,
                )
            )
        if result.reader_failures:
            diagnostics.append(
                Diagnostic(
                    "python_call_reader_failed",
                    Severity.WARN,
                    "a call reader failed, so names this cell's calls read may be missing: "
                    + "; ".join(result.reader_failures),
                    cell.id,
                )
            )
        return Analyzed(
            defines=list(result.defines),
            references=list(result.references),
            builtin_references=list(result.builtin_references),
            mutation_defines=list(result.mutation_defines),
            imports=list(result.imports),
            error=result.error,
            diagnostics=diagnostics,
        )

    def normalize(self, body: str) -> str:
        return normalize(body)
