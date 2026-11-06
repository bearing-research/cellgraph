"""The Python language analyzer: defines and references from the AST and symtable.

Module-level code runs in source order, so the AST visitor walks it statement by
statement; function, lambda and generator bodies run later, and ``symtable`` gives
the module names they read. Names starting with ``_`` are the cell's own and never
cross cells. What a star import binds cannot be known, so it binds nothing here.
"""

from __future__ import annotations

import ast
import builtins
import functools
import symtable
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cellgraph.diagnostics import Diagnostic, Severity
from cellgraph.languages import Analyzed

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


# Walking the AST recurses once per level; a long ``a + a + ...`` chain is that deep.
_DEEP_RECURSION_LIMIT = 100_000


def _deeply[T](fn: Callable[..., T], *args: object) -> T:
    """``fn(*args)``, retried once under a higher recursion limit on RecursionError."""
    try:
        return fn(*args)
    except RecursionError:
        pass
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, _DEEP_RECURSION_LIMIT))
    try:
        return fn(*args)
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
        # Pure ``x = ...`` targets. ``df = ...`` then ``df["col"] = ...`` in
        # one cell is a local define, not a mutation of an upstream.
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
        # Inside a generator's elements, which run when it is iterated: a read
        # there is not a read before the cell's own binding.
        self._deferred = False
        # A class body's analyzer: names it reads that resolve to module globals
        # whatever the class binds (a comprehension's elements, a nested class).
        self._in_class = False
        self._global_scope = False
        self.global_reads: set[str] = set()
        # PEP 563 stringifies annotations, so under it they are not
        # runtime references.
        self._future_annotations: bool = False

    def visit_Module(self, node: ast.Module) -> None:
        """Visit module: process top-level statements."""
        self._future_annotations = any(
            isinstance(s, ast.ImportFrom)
            and s.module == "__future__"
            and any(alias.name == "annotations" for alias in s.names)
            for s in node.body
        )
        for child in node.body:
            self.visit(child)

    def _read(self, name: str) -> None:
        """Record a read of *name*, at this point of the cell."""
        if name in self._local_vars:
            return
        if self._global_scope:
            self.global_reads.add(name)
            return
        self.references.add(name)
        if name not in self._defined_so_far and not self._deferred:
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
        self.defines.add(node.name)
        self._visit_function_signature(node)
        self._defined_so_far.add(node.name)
        # Don't recurse into function body (it's a nested scope)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Handle: async def f(): ..."""
        self.defines.add(node.name)
        self._visit_function_signature(node)
        self._defined_so_far.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Handle: class C: the name is defined, the body is a nested scope.

        Decorators, bases and class keywords evaluate at module load and are
        walked here; body free names come from the symtable pass.
        """
        self.defines.add(node.name)
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
        for stmt in node.body:
            body.visit(stmt)
        self._local_vars = old_local_vars
        for name in sorted(body.read_before_define | body.global_reads):
            self._read(name)
        for name in body.mutation_defines - body.pure_defines:
            self.defines.add(name)
            self.mutation_defines.add(name)
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
            self.defines.add(name)
            self._defined_so_far.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Handle: from foo import bar or from foo import *."""
        for alias in node.names:
            if alias.name == "*":
                # Star imports: defines are unknowable, skip.
                pass
            else:
                name = alias.asname if alias.asname else alias.name
                self.defines.add(name)
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

    def visit_With(self, node: ast.With) -> None:
        """Handle ``with ... as x``: x is defined at top level."""
        for item in node.items:
            if item.optional_vars:
                self._add_assign_target(item.optional_vars)
            self.visit(item.context_expr)
            if item.optional_vars:
                _collect_name_targets(item.optional_vars, self._defined_so_far)
        for stmt in node.body:
            self.visit(stmt)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """Handle: async with ... as x:"""
        for item in node.items:
            if item.optional_vars:
                self._add_assign_target(item.optional_vars)
            self.visit(item.context_expr)
            if item.optional_vars:
                _collect_name_targets(item.optional_vars, self._defined_so_far)
        for stmt in node.body:
            self.visit(stmt)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Handle ``except E as e``: Python unbinds e when the handler ends, so it is
        the handler's, like a lambda parameter."""
        if node.type:
            self.visit(node.type)
        old_local_vars = self._local_vars
        if node.name:
            self._local_vars = self._local_vars | {node.name}
        for stmt in node.body:
            self.visit(stmt)
        self._local_vars = old_local_vars

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """Handle: walrus operator := at top level."""
        if isinstance(node.target, ast.Name):
            self.defines.add(node.target.id)
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
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
        self.defines.add(name)
        self.pure_defines.add(name)
        self._defined_so_far.add(name)

    def visit_Delete(self, node: ast.Delete) -> None:
        """Handle ``del x``: x is referenced but not defined."""
        for target in node.targets:
            self._add_delete_target(target)

    def _add_delete_target(self, target: ast.expr) -> None:
        """Read the root of a ``del`` target; ``del x[k]`` and ``del x.a`` mutate x."""
        if isinstance(target, ast.Name):
            self._read(target.id)
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
        old_deferred, old_global = self._deferred, self._global_scope
        # A generator's elements run when it is iterated, not now.
        self._deferred = old_deferred or isinstance(node, ast.GeneratorExp)
        # In a class body, the rest cannot see the class's names: they are globals.
        self._global_scope = old_global or self._in_class
        try:
            local_vars = set(self._local_vars)
            for gen in node.generators:
                local_vars |= self._extract_target_names(gen.target)
            self._local_vars = local_vars

            # Element(s) and if-clauses see all loop targets.
            for elt in elements:
                self.visit(elt)
            # Subsequent generators' iter expressions can see prior
            # loop targets, so they get visited under the local scope.
            for gen in node.generators[1:]:
                self.visit(gen.iter)
            for gen in node.generators:
                for cond in gen.ifs:
                    self.visit(cond)
        finally:
            self._local_vars = old_local_vars
            self._deferred, self._global_scope = old_deferred, old_global

    def _extract_target_names(self, target: ast.expr) -> set[str]:
        """Names bound by a comprehension or for-loop target."""
        names: set[str] = set()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                names |= self._extract_target_names(elt)
        elif isinstance(target, ast.Starred):
            names |= self._extract_target_names(target.value)
        return names

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Lambda: its defaults run now; its body runs later, like a def body (symtable's)."""
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

    def _add_assign_target(self, target: ast.expr) -> None:
        """Add the names an assignment target defines.

        Subscript (``df["col"]``) and attribute (``obj.attr``) targets define
        their root name.
        """
        if isinstance(target, ast.Name):
            self.defines.add(target.id)
            self.pure_defines.add(target.id)
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
            self.defines.add(node.id)
            self.mutation_defines.add(node.id)
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            self._add_mutation_define(node.value)


class _Synchronous(ast.NodeTransformer):
    """Module-level ``await``, ``async for``, ``async with`` and async comprehensions
    made synchronous, which changes no scope. Python 3.14's symtable refuses them."""

    def visit_FunctionDef(self, node: ast.AST) -> ast.AST:
        return node  # an async def's body may await

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

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


def _symbol_tables(source: str, tree: ast.Module) -> list[symtable.SymbolTable]:
    """The module's symbol table, or one per top-level statement when it is refused.

    A function's scopes depend only on the functions around it, all in the same
    top-level statement, so the per-statement tables give the same answers.
    """
    try:
        return [symtable.symtable(source, "<cell>", "exec")]
    except SyntaxError:
        pass
    lines = source.splitlines()
    tables = []
    for stmt in tree.body:
        decorators = getattr(stmt, "decorator_list", [])
        first = min([stmt.lineno, *(d.lineno for d in decorators)])
        assert stmt.end_lineno is not None
        segment = "\n".join(lines[first - 1 : stmt.end_lineno])
        try:
            tables.append(symtable.symtable(segment, "<cell>", "exec"))
            continue
        except SyntaxError:
            pass
        try:
            sync = _deeply(lambda: ast.unparse(_Synchronous().visit(ast.parse(segment))))
            tables.append(symtable.symtable(sync, "<cell>", "exec"))
        except (SyntaxError, RecursionError, ValueError):
            continue  # nothing this statement's bodies read is known
    return tables


def _body_refs(tables: list[symtable.SymbolTable]) -> set[str]:
    """Names read in function, lambda and class bodies that resolve to module globals.

    A symbol does iff it is referenced, ``is_global()``, and not local, a parameter
    or a closure variable (``is_free()`` resolves via enclosing scopes). The caller
    filters builtins, privates and defines.
    """
    refs: set[str] = set()
    stack = [child for table in tables for child in table.get_children()]
    while stack:
        scope = stack.pop()
        for sym in scope.get_symbols():
            if (
                sym.is_referenced()
                and sym.is_global()
                and not sym.is_local()
                and not sym.is_parameter()
                and not sym.is_free()
            ):
                refs.add(sym.get_name())
        stack.extend(scope.get_children())
    return refs


def _global_writes(tables: list[symtable.SymbolTable]) -> tuple[set[str], set[str]]:
    """Names a function binds at module scope through ``global X; X = ...``.

    Returns ``(writes, writes_with_reads)``; the second are also read in the function
    and stay in references. A bare ``global X`` and ``nonlocal`` bind nothing here.
    """
    writes: set[str] = set()
    read_writes: set[str] = set()
    stack = [child for table in tables for child in table.get_children()]
    while stack:
        scope = stack.pop()
        for sym in scope.get_symbols():
            if sym.is_assigned() and sym.is_declared_global():
                writes.add(sym.get_name())
                if sym.is_referenced():
                    read_writes.add(sym.get_name())
        stack.extend(scope.get_children())
    return writes, read_writes


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


_BUILTIN_NAMES = frozenset(dir(builtins)) | {"__name__", "__file__", "__doc__", "__package__"}


def analyze_cell(source: str) -> CellNames:
    """Analyze a cell's source and extract its defines and references."""
    if not source or not source.strip():
        return CellNames(defines=[], references=[])

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return CellNames(defines=[], references=[], error=f"Syntax error: {e.msg}")
    except (RecursionError, MemoryError):
        return CellNames(too_deep=True)

    # The visitor stops at function, lambda and generator bodies; symtable covers those.
    analyzer = VariableAnalyzer()
    try:
        _deeply(analyzer.visit, tree)
    except RecursionError:
        return CellNames(too_deep=True)

    tables = _symbol_tables(source, tree)
    nested_refs = _body_refs(tables)

    # ``global`` writes inside functions are module defines too.
    # ``global_read_writes`` are also read there, so they stay in references.
    global_writes, global_read_writes = _global_writes(tables)
    augmented = _global_augmented(tree)
    global_read_writes |= augmented
    nested_refs |= augmented

    defines = [v for v in (analyzer.defines | global_writes) if not v.startswith("_")]
    # A pure assignment in the same cell supersedes a mutation-define.
    effective_mutation_defines = {
        v
        for v in (analyzer.mutation_defines | global_read_writes)
        if not v.startswith("_") and v in set(defines) and v not in analyzer.pure_defines
    }
    # Pure defines are filtered from references (intra-cell rebinds);
    # mutation-defines and reads before the cell's own binding are kept.
    pure_defined_names = set(defines) - effective_mutation_defines - analyzer.read_before_define
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

    return CellNames(
        defines=sorted(set(defines)),
        references=sorted(set(references)),
        mutation_defines=sorted(effective_mutation_defines),
        builtin_references=sorted(set(builtin_references)),
        error=None,
    )


# A notebook is analyzed again on every edit with most cells unchanged, so the
# per-source work is kept for the sources seen last.
_MEMO_SIZE = 4096


@functools.lru_cache(maxsize=_MEMO_SIZE)
def _analyzed(source: str) -> CellNames:
    return analyze_cell(source)


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
    except (SyntaxError, RecursionError, ValueError, MemoryError):
        lines = [line.rstrip() for line in source.splitlines()]
        return "\n".join(lines).strip()


class PythonAnalyzer:
    """The Python language analyzer: ``analyze_cell`` over the cell body."""

    owns_key = True

    def analyze(self, cell: Cell) -> Analyzed:
        # Memoized: the lists are copied, so the caller may change its own.
        result = _analyzed(cell.source)
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
        return Analyzed(
            defines=list(result.defines),
            references=list(result.references),
            builtin_references=list(result.builtin_references),
            mutation_defines=list(result.mutation_defines),
            error=result.error,
            diagnostics=diagnostics,
        )

    def normalize(self, body: str) -> str:
        return normalize(body)
