"""Generate the human-readable Python program that is also executed by CompiledPlan.

The block kernels are compile-time IR.  This module lowers them to one ordinary
Python module containing the exact ``execute_day`` / ``execute_replay``
functions used at runtime.  ``CompiledPlan.code()`` returns this same source.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .kernel import (
    DirectCalculationKernel,
    LPKernel,
    ProportionalCalculationKernel,
    ScalarFormulaKernel,
)
from .lp import BlockLP, Maximize, Proportional, Slot


_IDENTIFIER_RE = re.compile(r"[^0-9A-Za-z_]+")
_STATE_INDEX_RE = re.compile(r"state\[(\d+)\]")


def _identifier(text: str) -> str:
    text = _IDENTIFIER_RE.sub("_", text).strip("_")
    if not text:
        text = "slot"
    if text[0].isdigit():
        text = "_" + text
    return text


@dataclass
class GeneratedPlanSource:
    source: str
    namespace: dict[str, object]


class PythonPlanEmitter:
    def __init__(self, state_layout):
        self.state_layout = state_layout
        self.lines: list[str] = []
        self.namespace: dict[str, object] = {}
        self.slot_names: dict[int, str] = {}
        used: set[str] = set()
        for slot in sorted(state_layout.slots.values(), key=lambda value: value.index):
            base = "S_" + _identifier(slot.name).upper()
            name = base
            suffix = 2
            while name in used:
                name = f"{base}_{suffix}"
                suffix += 1
            used.add(name)
            self.slot_names[slot.index] = name

    def emit(self, line: str = ""):
        self.lines.append(line)

    def scalar(self, value) -> str:
        if isinstance(value, Slot):
            return f"state[{self.slot_names[value.index]}]"
        number = float(value)
        if math.isinf(number):
            return "float('inf')" if number > 0 else "float('-inf')"
        if math.isnan(number):
            return "float('nan')"
        return repr(number)

    def slot_constant(self, slot: Slot) -> str:
        return self.slot_names[slot.index]

    # ------------------------------------------------------------------
    # Human-readable LP rendering used for numerical fallbacks.
    #
    # This is intentionally separate from ``scalar()``: the text is emitted
    # only as comments, so slot *names* are much easier to understand than
    # executable ``state[S_...]`` expressions.
    # ------------------------------------------------------------------
    def readable_scalar(self, value) -> str:
        if isinstance(value, Slot):
            return value.name
        number = float(value)
        if math.isinf(number):
            return "INF" if number > 0 else "-INF"
        if math.isnan(number):
            return "NAN"
        return repr(number)

    def readable_linear_expression(self, coefficients: dict[str, object]) -> list[str]:
        """Render a linear expression as one readable term per line."""
        lines: list[str] = []
        for name, coefficient in coefficients.items():
            if isinstance(coefficient, Slot):
                coefficient_text = coefficient.name
                term = f"({coefficient_text}) * {name}"
                sign = "+"
            else:
                number = float(coefficient)
                if abs(number) <= 1e-15:
                    continue
                magnitude = abs(number)
                if magnitude == 1.0:
                    term = name
                else:
                    term = f"{magnitude!r} * {name}"
                sign = "+" if number >= 0 else "-"

            if not lines:
                if sign == "-":
                    lines.append(f"- {term}")
                else:
                    lines.append(term)
            else:
                lines.append(f"{sign} {term}")

        return lines or ["0"]

    def readable_lp_lines(self, model: BlockLP) -> list[str]:
        """Return a concise, human-readable rendering of a fallback LP."""
        lines: list[str] = ["Numerical LP fallback", ""]

        lines.append("VARIABLES")
        for name, variable in model.variables.items():
            lower = self.readable_scalar(variable.lower)
            if variable.upper is None:
                lines.append(f"    {name} >= {lower}")
            else:
                upper = self.readable_scalar(variable.upper)
                lines.append(f"    {lower} <= {name} <= {upper}")

        lines.extend(["", "ALLOCATION RULE"])
        if isinstance(model.rule, Maximize):
            lines.append("    MAXIMIZE")
            for term in self.readable_linear_expression(model.rule.coefficients):
                lines.append(f"        {term}")
        elif isinstance(model.rule, Proportional):
            lines.append("    PROPORTIONAL")
            for name, reference in model.rule.reference_cfs.items():
                lines.append(
                    f"        {name}: reference_cfs = {self.readable_scalar(reference)}"
                )
        else:
            lines.append(f"    {model.rule!r}")

        lines.extend(["", "CONSTRAINTS"])
        if not model.constraints:
            lines.append("    (none)")
        for constraint in model.constraints:
            lines.append(f"    {constraint.name}:")
            expression = self.readable_linear_expression(constraint.coefficients)
            lower = (
                None if constraint.lower is None
                else self.readable_scalar(constraint.lower)
            )
            upper = (
                None if constraint.upper is None
                else self.readable_scalar(constraint.upper)
            )

            if lower is not None:
                lines.append(f"        {lower} <=")
            for term in expression:
                lines.append(f"        {term}")
            if upper is not None:
                lines.append(f"        <= {upper}")

        lines.extend(["", "COMMITTED STATE UPDATES"])
        if not model.updates:
            lines.append("    (none)")
        for variable_name, updates in model.updates.items():
            lines.append(f"    {variable_name}:")
            if not updates:
                lines.append("        (none)")
                continue
            for slot, coefficient in updates.items():
                lines.append(
                    f"        {slot.name} += "
                    f"({self.readable_scalar(coefficient)}) * {variable_name}"
                )

        return lines

    def emit_commented_lp(self, model: BlockLP):
        for line in self.readable_lp_lines(model):
            self.emit("#" if not line else f"# {line}")

    def rewrite_formula_source(self, source: str, function_name: str) -> str:
        source = source.replace("def maximum(state, factors):", f"def {function_name}(state, factors):", 1)

        def replace(match):
            index = int(match.group(1))
            return f"state[{self.slot_names.get(index, str(index))}]"

        return _STATE_INDEX_RE.sub(replace, source)

    def add_external(self, name: str, value):
        self.namespace[name] = value

    # ------------------------------------------------------------------
    # State updates.  Coefficients are evaluated from one pre-write state
    # snapshot, matching kernel._commit_updates exactly.
    # ------------------------------------------------------------------
    def emit_commit(self, model: BlockLP, increments: dict[str, str], indent="    "):
        slots: dict[int, Slot] = {}
        terms: dict[int, list[str]] = {}
        for variable_name, increment_code in increments.items():
            for slot, coefficient in model.updates.get(variable_name, {}).items():
                slots[slot.index] = slot
                terms.setdefault(slot.index, []).append(
                    f"({self.scalar(coefficient)}) * ({increment_code})"
                )
        if not terms:
            return
        for index in sorted(terms):
            local = f"_change_{index}"
            self.emit(indent + local + " = " + " + ".join(terms[index]))
        for index in sorted(terms):
            self.emit(
                indent
                + f"state[{self.slot_names[index]}] += _change_{index}"
                + f"  # {slots[index].name}"
            )

    # ------------------------------------------------------------------
    # Direct kernel.
    # ------------------------------------------------------------------
    def emit_direct(self, operation: DirectCalculationKernel, function_name: str):
        model = operation.model
        name = operation.name
        variable = model.variables[name]
        safe = _identifier(name)
        self.emit(f"def {function_name}(state):")
        self.emit(f"    # Direct formula for {name}")
        self.emit(f"    _lower = {self.scalar(variable.lower)}")
        self.emit(
            "    _upper = float('inf')"
            if variable.upper is None
            else f"    _upper = {self.scalar(variable.upper)}"
        )
        self.emit("    if _upper != float('inf') and -TOL <= _upper < 0 and _lower == 0:")
        self.emit("        _upper = 0.0")
        self.emit("    if not isfinite(_lower) or isnan(_upper):")
        self.emit("        raise BlockLPError('Non-finite variable bound')")

        for row_index, constraint in enumerate(model.constraints):
            coefficient = constraint.coefficients.get(name, 0.0)
            ccode = self.scalar(coefficient)
            lo = constraint.lower
            hi = constraint.upper
            self.emit("")
            self.emit(f"    # {constraint.name}")
            self.emit(f"    _c{row_index} = {ccode}")
            if lo is not None:
                self.emit(f"    _lo{row_index} = {self.scalar(lo)}")
            if hi is not None:
                self.emit(f"    _hi{row_index} = {self.scalar(hi)}")
            self.emit(f"    if not isfinite(_c{row_index}):")
            self.emit(f"        raise BlockLPError({('Non-finite coefficient: ' + constraint.name)!r})")
            bound_vars = []
            if lo is not None:
                bound_vars.append(f"_lo{row_index}")
            if hi is not None:
                bound_vars.append(f"_hi{row_index}")
            if bound_vars:
                cond = " or ".join(f"not isfinite({v})" for v in bound_vars)
                self.emit(f"    if {cond}:")
                self.emit(f"        raise BlockLPError({('Non-finite constraint bound: ' + constraint.name)!r})")

            # A Slot sign is only a nonnegative/nonpositive guarantee; it does
            # *not* mean the runtime coefficient is strictly away from zero.
            # Direct execution must therefore retain the exact three-way
            # runtime branch used by DirectCalculationKernel._interval().
            # Otherwise a structurally nonnegative coefficient that is 0.0 on
            # a particular day would incorrectly divide a row bound by zero.
            if isinstance(coefficient, Slot):
                self.emit(f"    if abs(_c{row_index}) <= 1e-15:")
                if lo is not None:
                    self.emit(f"        if _lo{row_index} > TOL:")
                    self.emit(f"            raise BlockLPError({('Block [' + repr(name) + '] failed: infeasible ' + constraint.name)!r})")
                if hi is not None:
                    self.emit(f"        if _hi{row_index} < -TOL:")
                    self.emit(f"            raise BlockLPError({('Block [' + repr(name) + '] failed: infeasible ' + constraint.name)!r})")
                self.emit(f"    elif _c{row_index} > 0:")
                if lo is not None:
                    self.emit(f"        _lower = max(_lower, _lo{row_index} / _c{row_index})")
                if hi is not None:
                    self.emit(f"        _upper = min(_upper, _hi{row_index} / _c{row_index})")
                self.emit("    else:")
                if lo is not None:
                    self.emit(f"        _upper = min(_upper, _lo{row_index} / _c{row_index})")
                if hi is not None:
                    self.emit(f"        _lower = max(_lower, _hi{row_index} / _c{row_index})")
            else:
                number = float(coefficient)
                if abs(number) <= 1e-15:
                    if lo is not None:
                        self.emit(f"    if _lo{row_index} > TOL:")
                        self.emit(f"        raise BlockLPError({('Block [' + repr(name) + '] failed: infeasible ' + constraint.name)!r})")
                    if hi is not None:
                        self.emit(f"    if _hi{row_index} < -TOL:")
                        self.emit(f"        raise BlockLPError({('Block [' + repr(name) + '] failed: infeasible ' + constraint.name)!r})")
                elif number > 0:
                    if lo is not None:
                        self.emit(f"    _lower = max(_lower, _lo{row_index} / _c{row_index})")
                    if hi is not None:
                        self.emit(f"    _upper = min(_upper, _hi{row_index} / _c{row_index})")
                else:
                    if lo is not None:
                        self.emit(f"    _upper = min(_upper, _lo{row_index} / _c{row_index})")
                    if hi is not None:
                        self.emit(f"    _lower = max(_lower, _hi{row_index} / _c{row_index})")

        self.emit("")
        self.emit("    _scale = max(1.0, abs(_lower) if isfinite(_lower) else 1.0, abs(_upper) if isfinite(_upper) else 1.0)")
        self.emit("    if _upper < _lower - TOL * _scale:")
        self.emit(f"        raise BlockLPError({('Block [' + repr(name) + '] failed: direct interval is infeasible')!r})")
        self.emit("    if _upper < _lower:")
        self.emit("        _lower = _upper = 0.5 * (_lower + _upper)")
        objective = self.scalar(model.rule.coefficients[name])
        self.emit(f"    _objective = {objective}")
        self.emit("    if not isfinite(_objective) or abs(_objective) <= 1e-15:")
        self.emit("        raise BlockLPError('Invalid direct objective coefficient')")
        self.emit(f"    {safe} = _upper if _objective > 0 else _lower")
        self.emit(f"    if not isfinite({safe}):")
        self.emit(f"        raise BlockLPError({('Block [' + repr(name) + '] failed: unbounded direct objective')!r})")
        self.emit_commit(model, {name: safe})
        self.emit("    return 0")
        self.emit("")

    # ------------------------------------------------------------------
    # Monotone proportional kernel.
    # ------------------------------------------------------------------
    def emit_proportional(self, operation: ProportionalCalculationKernel, function_name: str, external_name: str):
        model = operation.model
        targets = tuple(operation.targets)
        self.add_external(external_name, operation.fallback)

        # Helper: common proportional MIN formula.
        common_name = function_name + "_common_increment"
        self.emit(f"def {common_name}(state, factors):")
        self.emit("    _upper = float('inf')")
        for name in targets:
            variable = model.variables[name]
            if variable.upper is not None:
                self.emit(f"    if {name!r} in factors:")
                self.emit(f"        _vu = {self.scalar(variable.upper)}")
                self.emit("        if isnan(_vu) or _vu < -TOL: raise BlockLPError('Invalid proportional variable bound')")
                self.emit(f"        _upper = min(_upper, max(0.0, _vu) / factors[{name!r}])")
        for index, constraint in enumerate(model.constraints):
            if constraint.upper is None:
                continue
            pieces = [
                f"({self.scalar(constraint.coefficients.get(name, 0.0))}) * factors.get({name!r}, 0.0)"
                for name in targets
                if name in constraint.coefficients
            ]
            consumption = " + ".join(pieces) if pieces else "0.0"
            self.emit(f"    _capacity_{index} = {self.scalar(constraint.upper)}  # {constraint.name}")
            self.emit(f"    _use_{index} = {consumption}")
            self.emit(f"    if _capacity_{index} < -TOL: raise BlockLPError({('Negative remaining capacity: ' + constraint.name)!r})")
            self.emit(f"    if _use_{index} > 1e-15:")
            self.emit(f"        _upper = min(_upper, max(0.0, _capacity_{index}) / _use_{index})")
        self.emit("    if not isfinite(_upper): raise BlockLPError('Unbounded proportional increment')")
        self.emit("    if _upper < -TOL: raise BlockLPError('Infeasible proportional increment')")
        self.emit("    return max(0.0, _upper)")
        self.emit("")

        member_name = function_name + "_member_capacity"
        self.emit(f"def {member_name}(state, name):")
        self.emit("    _upper = float('inf')")
        for i, name in enumerate(targets):
            prefix = "if" if i == 0 else "elif"
            variable = model.variables[name]
            self.emit(f"    {prefix} name == {name!r}:")
            if variable.upper is None:
                self.emit("        _upper = float('inf')")
            else:
                self.emit(f"        _upper = max(0.0, {self.scalar(variable.upper)})")
            for constraint in model.constraints:
                if constraint.upper is None or name not in constraint.coefficients:
                    continue
                coeff = self.scalar(constraint.coefficients[name])
                cap = self.scalar(constraint.upper)
                self.emit(f"        _c = {coeff}  # {constraint.name}")
                self.emit("        if _c > 1e-15:")
                self.emit(f"            _upper = min(_upper, max(0.0, {cap}) / _c)")
        self.emit("    else:")
        self.emit("        raise KeyError(name)")
        self.emit("    return max(0.0, _upper)")
        self.emit("")

        self.emit(f"def {function_name}(state):")
        self.emit(f"    # Analytical proportional water filling for {list(targets)!r}")
        # Guard all runtime coefficient slots whose sign was not structurally positive.
        seen_guard = set()
        guards = []
        for constraint in model.constraints:
            for coefficient in constraint.coefficients.values():
                if isinstance(coefficient, Slot) and coefficient.index not in seen_guard:
                    seen_guard.add(coefficient.index)
                    if coefficient.sign == 0:
                        guards.append(coefficient)
        for slot in guards:
            self.emit(f"    if {self.scalar(slot)} < -TOL:")
            self.emit(f"        return {external_name}.execute(state)")

        refs = ", ".join(f"{name!r}: {self.scalar(model.rule.reference_cfs[name])}" for name in targets)
        self.emit(f"    _references = {{{refs}}}")
        self.emit("    if any(isnan(c) or c < 0 for c in _references.values()):")
        self.emit("        raise BlockLPError('Invalid proportional reference cfs')")
        self.emit("    _phases = [")
        self.emit("        {name: 1.0 for name, cfs in _references.items() if isinf(cfs) and cfs > 0},")
        self.emit("        {name: cfs for name, cfs in _references.items() if isfinite(cfs) and cfs > 0},")
        self.emit("    ]")
        self.emit("    _deferred = []")
        self.emit("    for _active in _phases:")
        self.emit("        while _active:")
        self.emit("            _scale = max(_active.values())")
        self.emit("            _total = sum(v / _scale for v in _active.values())")
        self.emit("            _factors = {name: (v / _scale) / _total for name, v in _active.items()}")
        self.emit("            _tiny = [name for name, factor in _factors.items() if factor < 1e-6]")
        self.emit("            if _tiny:")
        self.emit("                _deferred.extend(_tiny)")
        self.emit("                _active = {name: cfs for name, cfs in _active.items() if name not in _tiny}")
        self.emit("                continue")
        self.emit(f"            _increment = {common_name}(state, _factors)")
        # Commit factors * increment using dynamic factor mapping; emit updates per target with .get
        self.emit_commit(model, {name: f"_factors.get({name!r}, 0.0) * _increment" for name in targets}, indent="            ")
        self.emit(f"            _blocked = [name for name in _active if {member_name}(state, name) <= TOL]")
        self.emit("            if not _blocked:")
        self.emit("                raise BlockLPError('Proportional allocation made no blocking progress')")
        self.emit("            _active = {name: cfs for name, cfs in _active.items() if name not in _blocked}")
        self.emit("    for _name in _deferred:")
        self.emit(f"        _increment = {member_name}(state, _name)")
        # Conditional commit by name.
        for i, name in enumerate(targets):
            prefix = "if" if i == 0 else "elif"
            self.emit(f"        {prefix} _name == {name!r}:")
            self.emit_commit(model, {name: "_increment"}, indent="            ")
        self.emit("    return 0")
        self.emit("")

    # ------------------------------------------------------------------
    # General scalar-formula kernel.
    # ------------------------------------------------------------------
    def emit_scalar_formula(self, operation: ScalarFormulaKernel, function_name: str, external_name: str):
        model = operation.model
        self.add_external(external_name, operation.fallback)
        maximum_name = function_name + "_maximum_formula"
        source = self.rewrite_formula_source(operation.formula_source, maximum_name)
        self.emit(f"# Symbolically projected scalar formula for {list(model.updates)!r}")
        for line in source.rstrip().splitlines():
            self.emit(line)
        self.emit("")

        scalar_name = function_name + "_scalar_maximum"
        self.emit(f"def {scalar_name}(state, factors):")
        self.emit("    try:")
        self.emit(f"        return {maximum_name}(state, factors), 0")
        self.emit("    except (FormulaGuardFailed, FormulaEvaluationError, ArithmeticError, OverflowError):")
        self.emit("        pass")
        self.emit("    if len(factors) == 1 and next(iter(factors.values())) == 1.0:")
        self.emit("        _name = next(iter(factors))")
        self.emit(f"        _result = {external_name}._solve(state, weights={{_name: 1.0}})")
        names = tuple(operation.names)
        self.emit(f"        _index = {dict(operation.index)!r}[_name]")
        self.emit("        return max(0.0, float(_result[_index])), 1")
        self.emit(f"    _result = {external_name}._solve(state, proportions=factors)")
        self.emit("    return max(0.0, float(_result[-1])), 1")
        self.emit("")

        blockers_name = function_name + "_structural_blockers"
        self.emit(f"def {blockers_name}(state, active):")
        self.emit("    _blocked = []")
        self.emit("    _blocked_set = set()")
        for name in operation.targets:
            variable = model.variables[name]
            if variable.upper is not None:
                self.emit(f"    if {name!r} in active and {self.scalar(variable.upper)} <= TOL:")
                self.emit(f"        _blocked.append({name!r}); _blocked_set.add({name!r})")
        for index, constraint in enumerate(model.constraints):
            coeff_entries = ", ".join(
                f"{name!r}: {self.scalar(coefficient)}"
                for name, coefficient in constraint.coefficients.items()
            )
            self.emit(f"    _coeffs_{index} = {{{coeff_entries}}}  # {constraint.name}")
            self.emit(f"    _live_{index} = {{}}")
            for name in constraint.coefficients:
                variable = model.variables[name]
                if variable.upper is None:
                    self.emit(f"    _live_{index}[{name!r}] = _coeffs_{index}[{name!r}]")
                else:
                    self.emit(f"    if {self.scalar(variable.upper)} > TOL:")
                    self.emit(f"        _live_{index}[{name!r}] = _coeffs_{index}[{name!r}]")
            if constraint.upper is not None:
                self.emit(f"    if {self.scalar(constraint.upper)} <= TOL and not any(c < -TOL for c in _live_{index}.values()):")
                emitted = False
                for name in operation.targets:
                    if name in constraint.coefficients:
                        emitted = True
                        self.emit(f"        if {name!r} in active and {name!r} not in _blocked_set and _coeffs_{index}.get({name!r}, 0.0) > TOL:")
                        self.emit(f"            _blocked.append({name!r}); _blocked_set.add({name!r})")
                if not emitted:
                    self.emit("        pass")
            if constraint.lower is not None:
                self.emit(f"    if -({self.scalar(constraint.lower)}) <= TOL and not any(c > TOL for c in _live_{index}.values()):")
                emitted = False
                for name in operation.targets:
                    if name in constraint.coefficients:
                        emitted = True
                        self.emit(f"        if {name!r} in active and {name!r} not in _blocked_set and _coeffs_{index}.get({name!r}, 0.0) < -TOL:")
                        self.emit(f"            _blocked.append({name!r}); _blocked_set.add({name!r})")
                if not emitted:
                    self.emit("        pass")
        self.emit("    return _blocked")
        self.emit("")

        self.emit(f"def {function_name}(state):")
        if isinstance(model.rule, Maximize):
            name, coefficient = next(iter(model.rule.coefficients.items()))
            safe = _identifier(name)
            self.emit(f"    _objective = {self.scalar(coefficient)}")
            self.emit("    if not isfinite(_objective) or _objective <= 0:")
            self.emit(f"        return {external_name}.execute(state)")
            self.emit(f"    {safe}, _calls = {scalar_name}(state, {{{name!r}: 1.0}})")
            self.emit_commit(model, {name: safe})
            self.emit("    return _calls")
            self.emit("")
            return

        targets = tuple(operation.targets)
        refs = ", ".join(f"{name!r}: {self.scalar(model.rule.reference_cfs[name])}" for name in targets)
        self.emit(f"    _references = {{{refs}}}")
        self.emit("    if any(isnan(c) or c < 0 for c in _references.values()):")
        self.emit("        raise BlockLPError('Invalid proportional reference cfs')")
        self.emit("    _phases = [")
        self.emit("        {name: 1.0 for name, cfs in _references.items() if isinf(cfs) and cfs > 0},")
        self.emit("        {name: cfs for name, cfs in _references.items() if isfinite(cfs) and cfs > 0},")
        self.emit("    ]")
        self.emit("    _deferred = []")
        self.emit("    _calls = 0")
        self.emit("    for _active in _phases:")
        self.emit("        while _active:")
        self.emit("            _scale = max(_active.values())")
        self.emit("            _total = sum(v / _scale for v in _active.values())")
        self.emit("            _factors = {name: (v / _scale) / _total for name, v in _active.items()}")
        self.emit("            _tiny = [name for name, factor in _factors.items() if factor < 1e-6]")
        self.emit("            if _tiny:")
        self.emit("                _deferred.extend(_tiny)")
        self.emit("                _active = {name: cfs for name, cfs in _active.items() if name not in _tiny}")
        self.emit("                continue")
        self.emit(f"            _increment, _extra = {scalar_name}(state, _factors)")
        self.emit("            _calls += _extra")
        self.emit_commit(model, {name: f"_factors.get({name!r}, 0.0) * _increment" for name in targets}, indent="            ")
        self.emit(f"            _blocked = {blockers_name}(state, _active)")
        self.emit("            if not _blocked:")
        self.emit("                def _classify(_names):")
        self.emit("                    nonlocal _calls")
        self.emit("                    _names = list(_names)")
        self.emit("                    if not _names: return []")
        self.emit(f"                    _witness, _extra = {scalar_name}(state, {{name: 1.0 for name in _names}})")
        self.emit("                    _calls += _extra")
        self.emit("                    if _witness > TOL: return []")
        self.emit("                    if len(_names) == 1: return _names")
        self.emit("                    _middle = len(_names) // 2")
        self.emit("                    return _classify(_names[:_middle]) + _classify(_names[_middle:])")
        self.emit("                _blocked = _classify(_active)")
        self.emit("            if not _blocked:")
        self.emit("                raise BlockLPError('Proportional allocation made no blocking progress')")
        self.emit("            _active = {name: cfs for name, cfs in _active.items() if name not in _blocked}")
        self.emit("    for _name in _deferred:")
        self.emit(f"        _increment, _extra = {scalar_name}(state, {{_name: 1.0}})")
        self.emit("        _calls += _extra")
        for i, name in enumerate(targets):
            prefix = "if" if i == 0 else "elif"
            self.emit(f"        {prefix} _name == {name!r}:")
            self.emit_commit(model, {name: "_increment"}, indent="            ")
        self.emit("    return _calls")
        self.emit("")

    def emit_lp(self, operation: LPKernel, function_name: str, external_name: str):
        self.add_external(external_name, operation)
        self.emit_commented_lp(operation.model)
        self.emit("")
        self.emit(f"def {function_name}(state):")
        self.emit(f"    # Execute the LP rendered immediately above.")
        self.emit(f"    return {external_name}.execute(state)")
        self.emit("")

    def emit_operation(self, operation, function_name: str, external_name: str):
        if isinstance(operation, DirectCalculationKernel):
            self.emit_direct(operation, function_name)
        elif isinstance(operation, ProportionalCalculationKernel):
            self.emit_proportional(operation, function_name, external_name)
        elif isinstance(operation, ScalarFormulaKernel):
            self.emit_scalar_formula(operation, function_name, external_name)
        elif isinstance(operation, LPKernel):
            self.emit_lp(operation, function_name, external_name)
        else:
            raise TypeError(f"Unknown compiled operation: {operation!r}")

    def build(self, operations, replay_operations) -> GeneratedPlanSource:
        self.emit('"""Generated apportionment program.  This exact source is executed by CompiledPlan."""')
        self.emit("from math import isfinite, isinf, isnan")
        self.emit("from ut_water_apportionment.compile.kernel import BlockLPError, TOL")
        self.emit("from ut_water_apportionment.compile.formula import FormulaEvaluationError, FormulaGuardFailed")
        self.emit("")
        self.emit("# Runtime state layout.  Formula code below uses these names instead of raw indexes.")
        for slot in sorted(self.state_layout.slots.values(), key=lambda value: value.index):
            self.emit(f"{self.slot_names[slot.index]} = {slot.index}  # {slot.name}")
        self.emit("")

        pass_names = []
        for index, operation in enumerate(operations):
            fn = f"_pass1_block_{index}"
            ext = f"_PASS1_FALLBACK_{index}"
            pass_names.append(fn)
            self.emit("# " + "=" * 76)
            self.emit(f"# PASS 1 block {index}: {list(operation.model.updates)!r} ({type(operation).__name__})")
            self.emit("# " + "=" * 76)
            self.emit_operation(operation, fn, ext)

        replay_names = []
        for index, operation in enumerate(replay_operations):
            fn = f"_replay_block_{index}"
            ext = f"_REPLAY_FALLBACK_{index}"
            replay_names.append(fn)
            self.emit("# " + "=" * 76)
            self.emit(f"# REPLAY block {index}: {list(operation.model.updates)!r} ({type(operation).__name__})")
            self.emit("# " + "=" * 76)
            self.emit_operation(operation, fn, ext)

        self.emit("def execute_day(state):")
        self.emit("    lp_solves = 0")
        for fn in pass_names:
            self.emit(f"    lp_solves += {fn}(state)")
        self.emit("    return lp_solves")
        self.emit("")
        self.emit("def execute_replay(state):")
        self.emit("    lp_solves = 0")
        for fn in replay_names:
            self.emit(f"    lp_solves += {fn}(state)")
        self.emit("    return lp_solves")
        self.emit("")
        return GeneratedPlanSource("\n".join(self.lines), dict(self.namespace))


def generate_plan_source(operations, replay_operations, state_layout) -> GeneratedPlanSource:
    return PythonPlanEmitter(state_layout).build(operations, replay_operations)
