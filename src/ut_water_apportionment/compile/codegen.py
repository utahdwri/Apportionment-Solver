"""Generate the human-readable Python program that is also executed by CompiledPlan.

Compiled operations are compile-time IR.  This module lowers them to one ordinary
Python module containing the exact ``execute`` function used at runtime.
``CompiledPlan.code()`` returns this same source.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .formula import (
    PROJECTED_ROW_SOURCE, Constant, NONNEGATIVE, NONPOSITIVE,
    possible_signs, scalar_expr,
)
from .kernel import (
    DirectCalculationKernel,
    LPKernel,
    ProportionalCalculationKernel,
    ScalarFormulaKernel,
    TOL
)
from .lp import BlockLP, Maximize, Proportional, Slot, Scalar
from ..models import NaturalFlowMode, ZoneTypes, PathTrxn


_IDENTIFIER_RE = re.compile(r"[^0-9A-Za-z_]+")
_STATE_INDEX_RE = re.compile(r"state\[(\d+)\]")


def _identifier(text: str) -> str:
    text = _IDENTIFIER_RE.sub("_", text).strip("_")
    if not text:
        text = "slot"
    if text[0].isdigit():
        text = "_" + text
    return text


# Emitted once per plan. Keep the scheduling code as ordinary readable Python;
# each block supplies only its formulas, blocker checks, and state updates.
_PROPORTIONAL_SOURCE = """
def _allocate_proportionally(state, references, maximum, commit, blockers):
    if any(isnan(cfs) or cfs < 0 for cfs in references.values()):
        raise SolverError('Invalid proportional reference cfs')
    phases = [
        {name: 1.0 for name, cfs in references.items() if isinf(cfs) and cfs > 0},
        {name: cfs for name, cfs in references.items() if isfinite(cfs) and cfs > 0},
    ]
    calls = 0

    def classify(names):
        nonlocal calls
        names = list(names)
        if not names:
            return []
        witness, extra = maximum(state, {name: 1.0 for name in names})
        calls += extra
        if witness > TOL:
            return []
        if len(names) == 1:
            return names
        middle = len(names) // 2
        return classify(names[:middle]) + classify(names[middle:])

    for active in phases:
        while active:
            scale = max(active.values())
            total = sum(value / scale for value in active.values())
            factors = {name: (value / scale) / total for name, value in active.items()}
            increment, extra = maximum(state, factors)
            calls += extra
            commit(state, {name: factor * increment for name, factor in factors.items()})
            blocked = blockers(state, active)
            if not blocked:
                blocked = classify(active)
            if not blocked:
                raise SolverError('Proportional allocation made no blocking progress')
            active = {name: cfs for name, cfs in active.items() if name not in blocked}

    return calls
"""


_DIRECT_SOURCE = """
def checked_nonnegative_increment(amount):
    if not isfinite(amount) or amount < -TOL:
        raise SolverError(f'Invalid allocation increment: {amount}')
    return max(0.0, amount)
"""


@dataclass
class GeneratedPlanSource:
    source: str
    namespace: dict[str, object]


def _nonnegative(value):
    expression = scalar_expr(value)
    return (not isinstance(expression, Constant) or math.isfinite(expression.value)) and (
        possible_signs(expression) <= NONNEGATIVE
    )


def _capacity_proof(model, slot):
    """Find one bound covering the total committed consumption of a capacity.

    Witness consumption must be nonnegative: a negative witness could finance
    a target decrement without actually being committed with it. Separate bounds
    on individual targets do not suffice when they consume the same capacity.
    """
    changes = {name: updates[slot] for name, updates in model.updates.items() if slot in updates}
    lowers = [model.variables[name].lower for name in changes]
    if not all(_nonnegative(lower) for lower in lowers):
        return None
    consumers = {name: change for name, change in changes.items() if not _nonnegative(change)}
    inputs = [*changes.values(), *lowers]
    if not consumers:
        return inputs
    if len(consumers) == 1:
        name, change = next(iter(consumers.items()))
        if model.variables[name].upper == slot and scalar_expr(change) == Constant(-1.0):
            return inputs
    for row in model.constraints:
        if (row.upper == slot
                and all(scalar_expr(row.coefficients.get(name, 0.0)) == scalar_expr(change, -1.0)
                        for name, change in consumers.items())
                and all(_nonnegative(c) and _nonnegative(model.variables[n].lower)
                        for n, c in row.coefficients.items())):
            return [*inputs, *row.coefficients.values(),
                    *(model.variables[n].lower for n in row.coefficients)]
    return None


class PythonPlanEmitter:
    """Generate the executable Python program for a compiled apportionment plan.

    PythonPlanEmitter lowers the compiler's kernel-level intermediate representation
    into readable Python source. The emitted source is both returned by
    ``CompiledPlan.code()`` and executed to perform the runtime calculations, so
    there is a single authoritative representation of the compiled plan.

    The generated program includes runtime state-slot aliases, natural-flow
    initialization and routing, one phase-independent function per priority
    block, spill-credit handling, and a single ``execute(state)`` entry point.
    Counterflow completion is part of the priority block itself; a post-spill
    sweep simply calls the same block functions again. Individual compiler
    kernels are emitted as direct calculations, proportional water-filling
    calculations, scalar formulas, or explicit numerical LP fallbacks.

    Runtime-varying values are referenced through state slots rather than embedded
    as fixed constants, allowing the same generated program to be reused across
    days without recompilation.
    """

    def __init__(self, state_layout):
        self.state_layout = state_layout
        self.lines: list[str] = []
        self.namespace: dict[str, object] = {}
        self.commit_functions: list[tuple[dict, str]] = []
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

        # Display labels are not symbol identities: IDs such as D-1 and D_1
        # normalize to the same text. Prefix each graph tag with its unique
        # structural index, retaining the label only for readability.
        self.flow_tags: dict[str, str] = {}
        self.zone_tags: dict[str, str] = {}
        if hasattr(state_layout, "graph"):
            graph = state_layout.graph.graph
            self.flow_tags = {
                flow.id: f"{index}_{_identifier(flow.id)}"
                for index, flow in enumerate(graph.interzone_flows)
            }
            self.zone_tags = {
                zone.id: f"{index}_{_identifier(zone.id)}"
                for index, zone in enumerate(graph.zones)
            }

    def emit(self, line: str = ""):
        self.lines.append(line)

    def scalar(self, value:Scalar) -> str:
        if isinstance(value, Slot):
            if value.constant_value is None:
                return f"state[{self.slot_names[value.index]}]"
            value = value.constant_value
        number = float(value)
        if number == 0.0:
            return "0.0"
        if math.isinf(number):
            return "float('inf')" if number > 0 else "float('-inf')"
        if math.isnan(number):
            return "float('nan')"
        return repr(number)

    def product(self, coefficient: Scalar, expression: str) -> str:
        code = self.scalar(coefficient)
        if code == "0.0":
            return "0.0"
        if code == "1.0":
            return expression
        if code == "-1.0":
            return f"-({expression})"
        return f"({code}) * ({expression})"

    def scaled_scalar(self, value: Scalar, factor: float) -> str:
        """Fold signs and coefficient-pair aliases in the direct formulas."""
        if isinstance(value, Slot):
            if value.constant_value is not None:
                return self.scalar(factor * value.constant_value)
            if value.source_index is not None:
                return self.product(factor * value.source_factor,
                                    f"state[{self.slot_names[value.source_index]}]")
        elif factor != 1.0:
            return self.scalar(factor * value)
        return self.product(factor, self.scalar(value))

    def prepare_direct_formulas(self, operations):
        """Prove nonnegative capacities are preserved by *all* block commits.

        Only layout capacities known to start nonnegative are candidates. Signed
        measurement residuals and outstanding reservations are not capacities.
        Spill credit only adds natural flow or clears a directional capacity.
        """
        self.written_slots = {slot.index for op in operations
                              for updates in op.model.updates.values() for slot in updates}
        capacities = set()
        for family in ("limits", "natural_flow", "measurement_forward_remaining",
                       "measurement_reverse_remaining", "account_out_remaining", "account_in_remaining"):
            capacities.update(getattr(self.state_layout, family, {}).values())
        self.capacity_proofs = {slot: set() for slot in capacities}
        for op in operations:
            written = {slot for updates in op.model.updates.values() for slot in updates}
            for slot in written & self.capacity_proofs.keys():
                proof = _capacity_proof(op.model, slot)
                if proof is None or not all(self.immutable(c) for c in proof):
                    del self.capacity_proofs[slot]
                else:
                    self.capacity_proofs[slot].update(proof)
        self.direct_inputs = {}  # (Slot, required_sign) -> (allow_infinity, tolerance)

    def immutable(self, value):
        return not isinstance(value, Slot) or not (
            {value.index, value.source_index} & self.written_slots
        )

    def require_direct_input(self, value, allow_infinity=False, sign=0, tolerance=0.0):
        if not isinstance(value, Slot) or value.constant_value is not None:
            return
        key = (value, sign)
        previous = self.direct_inputs.get(key, (True, tolerance))
        self.direct_inputs[key] = (previous[0] and allow_infinity, min(previous[1], tolerance))
        if value.source_index is not None:
            source = next(slot for slot in self.state_layout.slots.values()
                          if slot.index == value.source_index)
            source_sign = sign * (1 if value.source_factor >= 0 else -1)
            self.require_direct_input(source, sign=source_sign)

    def direct_bounds(self, operation):
        """Return upper-capacity formulas, or None when a proof is unavailable."""
        model, name = operation.model, operation.name
        variable = model.variables[name]
        objective = scalar_expr(model.rule.coefficients[name])
        if (scalar_expr(variable.lower) != Constant(0.0)
                or not self.immutable(variable.lower)
                or not self.immutable(model.rule.coefficients[name])
                or not isinstance(objective, Constant)
                or not math.isfinite(objective.value) or objective.value <= 0):
            return None
        inputs, bounds = [], []
        if variable.upper is not None:
            upper = scalar_expr(variable.upper)
            if isinstance(upper, Constant) and (math.isnan(upper.value) or upper.value < 0):
                return None
            sign = 1 if self.immutable(variable.upper) or variable.upper in self.capacity_proofs else 0
            inputs.append((variable.upper, True, sign, TOL))
            bounds.append(self.scalar(variable.upper))
        for row in model.constraints:
            coefficient = row.coefficients.get(name, 0.0)
            if not self.immutable(coefficient):
                return None
            for bound, direction in ((row.upper, 1.0), (row.lower, -1.0)):
                if bound is None:
                    continue
                c = scalar_expr(coefficient, direction)
                rhs = scalar_expr(bound, direction)
                if (not possible_signs(c) <= NONNEGATIVE
                        or isinstance(c, Constant) and not math.isfinite(c.value)
                        or isinstance(rhs, Constant) and not _nonnegative(rhs.value)):
                    return None
                # Zero rows can be omitted only if every writer preserves their
                # RHS. Signed net residuals and reservations need the general path.
                can_be_zero = not isinstance(c, Constant) or c.value == 0.0
                if can_be_zero and isinstance(bound, Slot) and bound.constant_value is None:
                    if direction != 1.0 or bound not in self.capacity_proofs:
                        return None
                    inputs.extend((v, False, v.sign, 0.0) for v in self.capacity_proofs[bound]
                                  if isinstance(v, Slot))
                # A signed residual can become feasible only after preceding
                # blocks. Its MIN result is checked when this block executes.
                sign = int(direction) if self.immutable(bound) or bound in self.capacity_proofs else 0
                inputs.append((bound, False, sign, TOL))
                if c == Constant(0.0):
                    continue
                inputs.append((coefficient, False, getattr(coefficient, "sign", 0), 0.0))
                capacity = self.scaled_scalar(bound, direction)
                divisor = self.scaled_scalar(coefficient, direction)
                term = capacity if c == Constant(1.0) else f"({capacity}) / ({divisor})"
                if isinstance(rhs, Constant) and isinstance(c, Constant):
                    term = self.scalar(rhs.value / c.value)
                if can_be_zero:
                    term = f"({term} if {divisor} > 0.0 else float('inf'))"
                bounds.append(term)
        bounded_slots = {variable.upper, *(row.upper for row in model.constraints)}
        for slot, coefficient in model.updates.get(name, {}).items():
            if not self.immutable(coefficient):
                return None
            if slot in bounded_slots and _capacity_proof(model, slot) is None:
                return None
            expression = scalar_expr(coefficient)
            if (not (possible_signs(expression) <= NONNEGATIVE or possible_signs(expression) <= NONPOSITIVE)
                    or isinstance(expression, Constant) and not math.isfinite(expression.value)):
                return None
            inputs.append((coefficient, False, getattr(coefficient, "sign", 0), 0.0))
        for value, allow_infinity, sign, tolerance in inputs:
            self.require_direct_input(value, allow_infinity, sign, tolerance)
        return list(dict.fromkeys(bounds)) or ["float('inf')"]

    def emit_direct_validation(self):
        self.emit("def _validate_direct_inputs(state):")
        self.emit("    # Once per day, after NF initialization and before allocation.")
        if self.direct_inputs:
            self.emit("    for index, allow_infinity, sign, tolerance, label in (")
            for (slot, sign), (allow_infinity, tolerance) in sorted(
                    self.direct_inputs.items(), key=lambda item: (item[0][0].index, item[0][1])):
                tolerance_code = "TOL" if tolerance == TOL else repr(tolerance)
                self.emit(f"        ({self.slot_names[slot.index]}, {allow_infinity!r}, {sign}, {tolerance_code}, {slot.name!r}),")
            self.emit("    ):")
            self.emit("        value = state[index]")
            self.emit("        if isnan(value) or value == float('-inf') or (not allow_infinity and not isfinite(value)):")
            self.emit("            raise SolverError(f'Invalid daily input {label}: {value}')")
            self.emit("        if sign and sign * value < -tolerance:")
            self.emit("            raise SolverError(f'Invalid daily input sign {label}: {value}')")
        self.emit("    return None")
        self.emit("")

    def slot_constant(self, slot: Slot) -> str:
        return self.slot_names[slot.index]

    # ------------------------------------------------------------------
    # Human-readable LP rendering used for numerical fallbacks.
    #
    # This is intentionally separate from ``scalar()``: the text is emitted
    # only as comments, so slot *names* are much easier to understand than
    # executable ``state[S_...]`` expressions.
    # ------------------------------------------------------------------
    def readable_scalar(self, value:Scalar) -> str:
        if isinstance(value, Slot):
            return value.name
        number = float(value)
        if math.isinf(number):
            return "INF" if number > 0 else "-INF"
        if math.isnan(number):
            return "NAN"
        return repr(number)

    def readable_linear_expression(self, coefficients: dict[str, Scalar]) -> list[str]:
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
            # User IDs may contain newlines or other control characters.
            # Keep every rendered LP line inside its generated comment.
            line = line.encode("unicode_escape").decode("ascii")
            self.emit("#" if not line else f"# {line}")

    def rewrite_formula_source(self, source: str, function_name: str) -> str:
        source = source.replace("def maximum(state, factors):", f"def {function_name}(state, factors):", 1)

        def replace(match):
            index = int(match.group(1))
            return f"state[{self.slot_names.get(index, str(index))}]"

        return _STATE_INDEX_RE.sub(replace, source)

    def add_external(self, name: str, value):
        self.namespace[name] = value

    def emit_commit_function(self, model: BlockLP, function_name: str) -> str:
        """Share identical updates between allocation passes and water-filling rounds."""
        for updates, existing_name in self.commit_functions:
            if updates == model.updates:
                return existing_name
        self.commit_functions.append((model.updates, function_name))
        self.emit(f"def {function_name}(state, increments):")
        self.emit_commit(model, {
            name: f"increments.get({name!r}, 0.0)" for name in model.updates
        })
        self.emit("    return None")
        self.emit("")
        return function_name

    def emit_commit(self, model: BlockLP, increments: dict[str, str], indent="    "):
        terms: dict[int, list[str]] = {}
        reads = set()
        for name, increment in increments.items():
            for slot, coefficient in model.updates.get(name, {}).items():
                term = self.product(coefficient, increment)
                if term == "0.0":
                    continue
                terms.setdefault(slot.index, []).append(term)
                if isinstance(coefficient, Slot) and coefficient.constant_value is None:
                    reads.add(coefficient.index)

        # Normal plan coefficients are immutable during allocation. Only models
        # that also write a coefficient need the pre-write snapshot temporaries.
        snapshot = bool(reads.intersection(terms))
        for index in sorted(terms):
            expression = " + ".join(terms[index])
            target = f"_change_{index} =" if snapshot else f"state[{self.slot_names[index]}] +="
            self.emit(f"{indent}{target} {expression}")
        if snapshot:
            for index in sorted(terms):
                self.emit(f"{indent}state[{self.slot_names[index]}] += _change_{index}")

    # ------------------------------------------------------------------
    # Direct kernel.
    # ------------------------------------------------------------------
    def emit_direct(self, operation: DirectCalculationKernel, function_name: str, external_name: str):
        bounds = self.direct_bounds(operation)
        if bounds is None:
            # Hand-built or coupled-sign scalar models retain the existing
            # interval executor; do not duplicate that interpreter in codegen.
            self.add_external(external_name, operation)
            self.emit(f"# General scalar interval for {operation.name!r}; see DirectCalculationKernel._interval.")
            self.emit(f"def {function_name}(state):")
            self.emit(f"    return {external_name}.execute(state)")
            self.emit("")
            return
        self.emit(f"def {function_name}(state):")
        self.emit(f"    # Direct formula for {operation.name!r}")
        if len(bounds) == 1:
            self.emit(f"    amount = {bounds[0]}")
        else:
            self.emit("    amount = min(")
            for bound in bounds:
                self.emit(f"        {bound},")
            self.emit("    )")
        self.emit("    amount = checked_nonnegative_increment(amount)")
        for slot, coefficient in operation.model.updates.get(operation.name, {}).items():
            expression = scalar_expr(coefficient)
            if expression == Constant(0.0):
                continue
            negative = possible_signs(expression) <= NONPOSITIVE
            magnitude = self.scaled_scalar(coefficient, -1.0 if negative else 1.0)
            change = "amount" if magnitude == "1.0" else f"({magnitude}) * amount"
            self.emit(f"    state[{self.slot_names[slot.index]}] {'-=' if negative else '+='} {change}")
        self.emit("    return 0")
        self.emit("")

    # ------------------------------------------------------------------
    # Monotone proportional kernel.
    # ------------------------------------------------------------------
    def emit_proportional(
        self,
        operation: ProportionalCalculationKernel,
        function_name: str,
        external_name: str
    ):
        """Emit Python for an equal-priority proportional allocation.

        The proportional kernel represents a block in which all target transactions
        share the available capacity according to their runtime reference CFS values.
        The generated code repeatedly computes the largest common proportional
        increment allowed by the remaining transaction limits and shared constraints,
        commits that increment, removes any newly blocked transactions, and continues
        until no further allocation is possible.

        Runtime Slot values are emitted as state lookups so proportions, capacities,
        and constraint coefficients may vary by day without recompilation.
        """

        model = operation.model

        if not isinstance(model.rule, Proportional):
            raise TypeError(
                "ProportionalCalculationKernel requires a Proportional allocation rule"
            )

        targets = tuple(operation.targets)
        self.add_external(external_name, operation.fallback)

        # Helper: common proportional MIN formula.
        common_name = function_name + "_common_increment"
        self.emit(f"def {common_name}(state, factors, allow_unbounded=False):")
        self.emit("    _upper = float('inf')")
        for name in targets:
            variable = model.variables[name]
            if variable.upper is not None:
                self.emit(f"    if {name!r} in factors:")
                self.emit(f"        _vu = {self.scalar(variable.upper)}")
                self.emit("        if isnan(_vu) or _vu < -TOL: raise SolverError('Invalid proportional variable bound')")
                self.emit(f"        _upper = min(_upper, max(0.0, _vu) / factors[{name!r}])")
        for index, constraint in enumerate(model.constraints):
            if constraint.upper is None:
                continue
            pieces = [
                self.product(constraint.coefficients[name], f"factors.get({name!r}, 0.0)")
                for name in targets
                if name in constraint.coefficients
            ]
            consumption = " + ".join(piece for piece in pieces if piece != "0.0") or "0.0"
            self.emit(f"    _capacity_{index} = {self.scalar(constraint.upper)}  # {constraint.name!r}")
            self.emit(f"    if _capacity_{index} < -TOL: raise SolverError({('Negative remaining capacity: ' + constraint.name)!r})")
            if consumption != "0.0":
                self.emit(f"    _use_{index} = {consumption}")
                self.emit(f"    if _use_{index} > 1e-15:")
                self.emit(f"        _upper = min(_upper, max(0.0, _capacity_{index}) / _use_{index})")
        self.emit("    if not allow_unbounded and not isfinite(_upper): raise SolverError('Unbounded proportional increment')")
        self.emit("    if _upper < -TOL: raise SolverError('Infeasible proportional increment')")
        self.emit("    return max(0.0, _upper), 0")
        self.emit("")

        blockers_name = function_name + "_blocked_members"
        self.emit(f"def {blockers_name}(state, active):")
        self.emit(f"    return [name for name in active if {common_name}(state, {{name: 1.0}}, allow_unbounded=True)[0] <= TOL]")
        self.emit("")
        commit_name = self.emit_commit_function(model, function_name + "_commit")

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
        self.emit(f"    return _allocate_proportionally(state, _references, {common_name}, {commit_name}, {blockers_name})")
        self.emit("")

    # ------------------------------------------------------------------
    # General scalar-formula kernel.
    # ------------------------------------------------------------------
    def emit_scalar_formula(
        self,
        operation: ScalarFormulaKernel,
        function_name: str,
        external_name: str
    ):
        """Emit Python for a statically projected scalar formula kernel.

        The scalar-formula kernel represents a block whose LP has been projected at
        compile time into explicit formulas for the target allocations. The generated
        code evaluates those formulas from the current runtime state, checks any
        compiled feasibility guards, and applies the resulting state updates.

        Slot-valued quantities remain runtime state lookups, so coefficients, bounds,
        and other daily inputs may vary without recompiling the symbolic projection.
        If a runtime guard fails, the generated code may invoke the kernel's numerical
        LP fallback when one is available.
        """
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
        self.emit(f"        _index = {dict(operation.index)!r}[_name]")
        self.emit("        return max(0.0, float(_result[_index])), 1")
        self.emit(f"    _result = {external_name}._solve(state, proportions=factors)")
        self.emit("    return max(0.0, float(_result[-1])), 1")
        self.emit("")

        commit_name = self.emit_commit_function(model, function_name + "_commit")
        if isinstance(model.rule, Proportional):
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
                self.emit(f"    _coeffs_{index} = {{{coeff_entries}}}  # {constraint.name!r}")
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
            safe = "_allocation"
            self.emit(f"    _objective = {self.scalar(coefficient)}")
            self.emit("    if not isfinite(_objective) or _objective <= 0:")
            self.emit(f"        return {external_name}.execute(state)")
            self.emit(f"    {safe}, _calls = {scalar_name}(state, {{{name!r}: 1.0}})")
            self.emit(f"    {commit_name}(state, {{{name!r}: {safe}}})")
            self.emit("    return _calls")
            self.emit("")
            return

        targets = tuple(operation.targets)
        refs = ", ".join(f"{name!r}: {self.scalar(model.rule.reference_cfs[name])}" for name in targets)
        self.emit(f"    _references = {{{refs}}}")
        self.emit(f"    return _allocate_proportionally(state, _references, {scalar_name}, {commit_name}, {blockers_name})")
        self.emit("")

    # ------------------------------------------------------------------
    # Compiled natural-flow program.
    #
    # DailyDataManager/new_day() binds only raw measured flows, specified NF,
    # external-boundary NF, and endpoint delivery factors.  Everything below is
    # emitted once at compile time and is part of the exact program returned by
    # plan.code().
    #
    # The small deliver()/required_inflow() functions are intentional extension
    # points for future piecewise losses.  Today they are linear fractional
    # transforms.  A later piecewise compiler can replace their bodies while the
    # propagation, system-gain, boundary, spill, and transaction code remains in
    # the same generated program.
    # ------------------------------------------------------------------
    def _flow_tag(self, flow_id: str) -> str:
        return self.flow_tags[flow_id]

    def _zone_tag(self, zone_id: str) -> str:
        return self.zone_tags[zone_id]

    def _loss_factor_slot(self, flow_id: str, endpoint: str) -> str:
        layout = self.state_layout
        slots = layout.loss_from_delivery if endpoint == "from" else layout.loss_to_delivery
        return self.slot_constant(slots[flow_id])

    def _propagate_name(self, zone_id: str) -> str:
        return f"_nf_propagate_{self._zone_tag(zone_id)}"

    def _route_name(self, zone_id: str) -> str:
        return f"_nf_selected_outflow_{self._zone_tag(zone_id)}"

    def _routing_coeff_name(self, zone_id: str) -> str:
        return f"_nf_routing_coefficients_from_{self._zone_tag(zone_id)}"

    def _adjust_remaining_name(self, zone_id: str) -> str:
        return f"_nf_adjust_remaining_from_{self._zone_tag(zone_id)}"

    def _flow_effect_name(self, flow_id: str) -> str:
        return f"_nf_apply_flow_{self._flow_tag(flow_id)}"

    def _calculated_losses_name(self, zone_id: str) -> str:
        return f"_nf_calculated_endpoint_losses_{self._zone_tag(zone_id)}"

    def _calculated_stream_outflows(self):
        graph = self.state_layout.graph
        result = {}
        for flow in graph.graph.interzone_flows:
            if flow.natural_flow_mode != NaturalFlowMode.CALCULATED:
                continue
            if not (
                graph.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
                and graph.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM
            ):
                continue
            result.setdefault(flow.from_zone, []).append(flow)
        return result

    def emit_loss_transform_functions(self):
        """Emit shared fractional-loss transforms and endpoint error labels."""
        self.emit("# " + "=" * 76)
        self.emit("# ENDPOINT LOSS TRANSFORMS")
        self.emit("# " + "=" * 76)
        self.emit("# Fractional today; future loss types can use their own shared transforms.")
        self.emit("_LOSS_ENDPOINTS = {")
        for flow in self.state_layout.graph.graph.interzone_flows:
            for endpoint in ("from", "to"):
                slot = self._loss_factor_slot(flow.id, endpoint)
                self.emit(f"    {slot}: {(flow.id + ' ' + endpoint)!r},")
        self.emit("}")
        self.emit("")
        self.emit("""
def _deliver(state, factor_slot, value):
    factor = state[factor_slot]
    if not isfinite(factor) or factor < -TOL:
        raise SolverError(f'Invalid delivery factor for {_LOSS_ENDPOINTS[factor_slot]}')
    if abs(value) <= NF_TOL:
        return 0.0
    return value * max(0.0, factor)


def _required_inflow(state, factor_slot, remaining):
    factor = state[factor_slot]
    if not isfinite(factor) or factor < -TOL:
        raise SolverError(f'Invalid delivery factor for {_LOSS_ENDPOINTS[factor_slot]}')
    if abs(remaining) <= NF_TOL:
        return 0.0
    if factor <= TOL:
        raise SolverError(f'Cannot invert zero-delivery loss for {_LOSS_ENDPOINTS[factor_slot]}')
    return remaining / factor
""".strip())
        self.emit("")

    def emit_nf_route_selectors(self):
        layout = self.state_layout
        candidates_by_zone = self._calculated_stream_outflows()
        for zone_id in layout.natural_flow:
            candidates = candidates_by_zone.get(zone_id, [])
            fn = self._route_name(zone_id)
            self.emit(f"def {fn}(state):")
            if not candidates:
                self.emit("    return None")
                self.emit("")
                continue
            self.emit("    _selected = None")
            for flow in candidates:
                active_slot = layout.boundary_natural_active.get(flow.id)
                if active_slot is None:
                    condition = "True"
                else:
                    condition = f"state[{self.slot_names[active_slot.index]}] < 0.5"
                self.emit(f"    if {condition}:  # {flow.id!r}")
                self.emit("        if _selected is not None:")
                self.emit(
                    f"            raise SolverError({('Natural flow at zone ' + zone_id + ' has multiple calculated outflows')!r})"
                )
                self.emit(f"        _selected = {flow.id!r}")
            self.emit("    return _selected")
            self.emit("")

    def emit_nf_propagation_functions(self):
        layout = self.state_layout
        graph = layout.graph
        candidates_by_zone = self._calculated_stream_outflows()
        for zone_id, natural_slot in layout.natural_at_zone.items():
            fn = self._propagate_name(zone_id)
            route_fn = self._route_name(zone_id)
            self.emit(f"def {fn}(state, delta, _visited=()):")
            self.emit("    if abs(delta) <= NF_TOL:")
            self.emit("        return")
            self.emit(f"    if {zone_id!r} in _visited:")
            self.emit("        raise SolverError('Calculated natural-flow routes contain a cycle')")
            self.emit(f"    state[{self.slot_names[natural_slot.index]}] += delta")
            candidates = candidates_by_zone.get(zone_id, [])
            if not candidates:
                self.emit("    return")
                self.emit("")
                continue
            self.emit(f"    _flow = {route_fn}(state)")
            self.emit("    if _flow is None:")
            self.emit("        return")
            self.emit(f"    _visited = _visited + ({zone_id!r},)")
            for i, flow in enumerate(candidates):
                prefix = "if" if i == 0 else "elif"
                flow_natural_slot = layout.flow_natural[flow.id]
                from_factor_slot = self._loss_factor_slot(flow.id, "from")
                to_factor_slot = self._loss_factor_slot(flow.id, "to")
                downstream = self._propagate_name(flow.to_zone)
                self.emit(f"    {prefix} _flow == {flow.id!r}:")
                self.emit(f"        _old = state[{self.slot_names[flow_natural_slot.index]}]")
                self.emit(f"        _source = state[{self.slot_names[natural_slot.index]}]")
                if not flow.bidirectional:
                    self.emit("        _source = max(0.0, _source)")
                self.emit(f"        _new = _deliver(state, {from_factor_slot}, _source)")
                self.emit(f"        _old_at_destination = _deliver(state, {to_factor_slot}, _old)")
                self.emit(f"        _new_at_destination = _deliver(state, {to_factor_slot}, _new)")
                self.emit(f"        state[{self.slot_names[flow_natural_slot.index]}] = _new")
                self.emit(
                    f"        {downstream}(state, _new_at_destination - _old_at_destination, _visited)"
                )
            self.emit("")

    def emit_nf_flow_effect_functions(self):
        layout = self.state_layout
        graph = layout.graph
        for flow in graph.graph.interzone_flows:
            fn = self._flow_effect_name(flow.id)
            from_factor_slot = self._loss_factor_slot(flow.id, "from")
            to_factor_slot = self._loss_factor_slot(flow.id, "to")
            from_stream = graph.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
            to_stream = graph.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM
            self.emit(f"def {fn}(state, natural, boundary=False):")
            self.emit("    if abs(natural) <= NF_TOL:")
            self.emit("        return")
            self.emit("    if natural > 0:")
            if to_stream:
                self.emit(f"        _destination = _deliver(state, {to_factor_slot}, natural)")
                self.emit(f"        {self._propagate_name(flow.to_zone)}(state, _destination)")
            if from_stream:
                self.emit("        if not boundary:")
                self.emit(f"            _source = _required_inflow(state, {from_factor_slot}, natural)")
                self.emit(f"            {self._propagate_name(flow.from_zone)}(state, -_source)")
            self.emit("        return")
            self.emit("    _magnitude = -natural")
            if from_stream:
                self.emit(f"    _destination = _deliver(state, {from_factor_slot}, _magnitude)")
                self.emit(f"    {self._propagate_name(flow.from_zone)}(state, _destination)")
            if to_stream:
                self.emit("    if not boundary:")
                self.emit(f"        _source = _required_inflow(state, {to_factor_slot}, _magnitude)")
                self.emit(f"        {self._propagate_name(flow.to_zone)}(state, -_source)")
            self.emit("")

    def emit_nf_calculated_loss_functions(self):
        layout = self.state_layout
        graph = layout.graph
        for zone_id in layout.natural_flow:
            fn = self._calculated_losses_name(zone_id)
            self.emit(f"def {fn}(state):")
            self.emit("    _total_loss = 0.0")
            for flow in graph.get_zone_inflows(zone_id):
                measured = self.slot_names[layout.measurements[flow.id].index]
                factor_slot = self._loss_factor_slot(flow.id, "to")
                tag = self._flow_tag(flow.id)
                self.emit(f"    _measured_{tag} = state[{measured}]")
                self.emit(f"    if _measured_{tag} >= 0:")
                self.emit(f"        _remaining = _deliver(state, {factor_slot}, _measured_{tag})")
                self.emit(f"        _total_loss += _measured_{tag} - _remaining")
                self.emit("    else:")
                self.emit(f"        _before = _required_inflow(state, {factor_slot}, -_measured_{tag})")
                self.emit(f"        _total_loss += _before + _measured_{tag}")
            for flow in graph.get_zone_outflows(zone_id):
                measured = self.slot_names[layout.measurements[flow.id].index]
                factor_slot = self._loss_factor_slot(flow.id, "from")
                tag = self._flow_tag(flow.id)
                self.emit(f"    _measured_{tag} = state[{measured}]")
                self.emit(f"    if _measured_{tag} >= 0:")
                self.emit(f"        _before = _required_inflow(state, {factor_slot}, _measured_{tag})")
                self.emit(f"        _total_loss += _before - _measured_{tag}")
                self.emit("    else:")
                self.emit(f"        _remaining = _deliver(state, {factor_slot}, -_measured_{tag})")
                self.emit(f"        _total_loss += -_measured_{tag} - _remaining")
            self.emit("    return _total_loss")
            self.emit("")

    def emit_nf_routing_coefficient_functions(self):
        layout = self.state_layout
        candidates_by_zone = self._calculated_stream_outflows()
        stream_zones = tuple(layout.natural_flow)
        for source in stream_zones:
            fn = self._routing_coeff_name(source)
            self.emit(f"def {fn}(state):")
            self.emit(f"    _coefficients = {{{source!r}: 1.0}}")
            self.emit("    _factor = 1.0")
            self.emit(f"    _zone = {source!r}")
            self.emit("    _visited = set()")
            self.emit("    while True:")
            self.emit("        if _zone in _visited:")
            self.emit("            raise SolverError('Calculated natural-flow routes contain a cycle')")
            self.emit("        _visited.add(_zone)")
            for i, zone_id in enumerate(stream_zones):
                prefix = "if" if i == 0 else "elif"
                candidates = candidates_by_zone.get(zone_id, [])
                self.emit(f"        {prefix} _zone == {zone_id!r}:")
                if not candidates:
                    self.emit("            break")
                    continue
                self.emit(f"            _flow = {self._route_name(zone_id)}(state)")
                self.emit("            if _flow is None:")
                self.emit("                break")
                for j, flow in enumerate(candidates):
                    branch = "if" if j == 0 else "elif"
                    from_factor = layout.loss_from_delivery[flow.id]
                    to_factor = layout.loss_to_delivery[flow.id]
                    self.emit(f"            {branch} _flow == {flow.id!r}:")
                    self.emit(
                        f"                _factor *= state[{self.slot_names[from_factor.index]}] * state[{self.slot_names[to_factor.index]}]"
                    )
                    self.emit(f"                _zone = {flow.to_zone!r}")
                self.emit("            if _factor <= NF_TOL:")
                self.emit("                break")
                self.emit("            _coefficients[_zone] = _factor")
            self.emit("        else:")
            self.emit("            break")
            self.emit("    return _coefficients")
            self.emit("")

            adjust = self._adjust_remaining_name(source)
            self.emit(f"def {adjust}(state, amount):")
            self.emit("    if abs(amount) <= NF_TOL:")
            self.emit("        return")
            self.emit(f"    _coefficients = {fn}(state)")
            for zone_id, remaining_slot in layout.natural_flow.items():
                self.emit(f"    _coefficient = _coefficients.get({zone_id!r}, 0.0)")
                self.emit("    if _coefficient:")
                self.emit(f"        state[{self.slot_names[remaining_slot.index]}] += amount * _coefficient")
                self.emit(f"        if abs(state[{self.slot_names[remaining_slot.index]}]) <= NF_TOL:")
                self.emit(f"            state[{self.slot_names[remaining_slot.index]}] = 0.0")
            self.emit("")

    def emit_nf_initialization(self):
        layout = self.state_layout
        graph = layout.graph
        self.emit("# " + "=" * 76)
        self.emit("# NATURAL-FLOW INITIALIZATION")
        self.emit("# " + "=" * 76)
        self.emit("def _initialize_natural_flow(state):")
        self.emit("    # Reset generated NF state for this day.")
        for slot in layout.natural_at_zone.values():
            self.emit(f"    state[{self.slot_names[slot.index]}] = 0.0")
        for slot in layout.natural_flow.values():
            self.emit(f"    state[{self.slot_names[slot.index]}] = 0.0")
        for slot in layout.flow_natural.values():
            self.emit(f"    state[{self.slot_names[slot.index]}] = 0.0")
        for positive, negative in layout.nf_coefficients.values():
            self.emit(f"    state[{self.slot_names[positive.index]}] = 0.0")
            self.emit(f"    state[{self.slot_names[negative.index]}] = 0.0")
        for positive, negative in layout.transaction_nf_coefficients.values():
            self.emit(f"    state[{self.slot_names[positive.index]}] = 0.0")
            self.emit(f"    state[{self.slot_names[negative.index]}] = 0.0")

        self.emit("")
        self.emit("    # Natural-flow routing coefficients used by transaction constraints.")
        sources = sorted({source for source, _ in layout.nf_coefficients})
        for source in sources:
            self.emit(f"    _coefficients = {self._routing_coeff_name(source)}(state)")
            for zone_id in layout.natural_flow:
                pair = layout.nf_coefficients.get((source, zone_id))
                if pair is None:
                    continue
                positive, negative = pair
                self.emit(f"    _value = _coefficients.get({zone_id!r}, 0.0)")
                self.emit(f"    state[{self.slot_names[positive.index]}] = _value")
                self.emit(f"    state[{self.slot_names[negative.index]}] = -_value")

        self.emit("")
        self.emit("    # Convert transaction anchor units to source-zone NF withdrawal units.")
        for name, transaction in layout.transactions.items():
            if not isinstance(transaction, PathTrxn):
                continue
            source = layout.schedule.get_nf_zone_id(transaction)
            if source is None:
                continue
            path = layout.schedule.ordered_paths[name]
            if not path:
                continue
            item = path[0]
            if item.loss_before >= 1.0:
                self.emit(
                    f"    raise SolverError({('First path loss leaves no deliverable flow for ' + name)!r})"
                )
                continue
            endpoint = "from" if item.factor > 0 else "to"
            factor_slot = self._loss_factor_slot(item.flow_id, endpoint)
            before_factor = 1.0 - float(item.loss_before)
            self.emit(
                f"    _source_per_anchor = _required_inflow(state, {factor_slot}, {abs(float(item.factor)) / before_factor!r})  # {name!r}"
            )
            for zone_id in layout.natural_flow:
                pair = layout.transaction_nf_coefficients.get((name, zone_id))
                if pair is None:
                    continue
                positive, negative = pair
                route = layout.nf_coefficients[source, zone_id][0]
                self.emit(
                    f"    _value = _source_per_anchor * state[{self.slot_names[route.index]}]"
                )
                self.emit(f"    state[{self.slot_names[positive.index]}] = _value")
                self.emit(f"    state[{self.slot_names[negative.index]}] = -_value")

        self.emit("")
        self.emit("    # Boundary flows and calculated system gains/losses.")
        for flow in graph.graph.interzone_flows:
            active = layout.boundary_natural_active.get(flow.id)
            boundary = layout.boundary_natural_flow.get(flow.id)
            from_type = graph.get_zone_by_id(flow.from_zone).type
            to_type = graph.get_zone_by_id(flow.to_zone).type
            is_system_calculated = (
                flow.natural_flow_mode == NaturalFlowMode.CALCULATED
                and {from_type, to_type} == {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
            )
            if active is not None:
                self.emit(f"    if state[{self.slot_names[active.index]}] >= 0.5:  # boundary {flow.id!r}")
                self.emit(f"        _value = state[{self.slot_names[boundary.index]}]")
                self.emit(f"        state[{self.slot_names[layout.flow_natural[flow.id].index]}] = _value")
                self.emit(f"        {self._flow_effect_name(flow.id)}(state, _value, boundary=True)")
                if is_system_calculated:
                    self.emit("    else:")
                    indent = "        "
                else:
                    continue
            elif is_system_calculated:
                indent = "    "
            else:
                continue

            measured_slot = layout.measurements[flow.id]
            if from_type == ZoneTypes.SYSTEM_GAIN_LOSS and to_type == ZoneTypes.STREAM:
                expression = (
                    f"state[{self.slot_names[measured_slot.index]}] + "
                    f"{self._calculated_losses_name(flow.to_zone)}(state)"
                )
            elif from_type == ZoneTypes.STREAM and to_type == ZoneTypes.SYSTEM_GAIN_LOSS:
                expression = (
                    f"state[{self.slot_names[measured_slot.index]}] - "
                    f"{self._calculated_losses_name(flow.from_zone)}(state)"
                )
            else:
                continue
            self.emit(indent + f"_value = {expression}")
            if not flow.bidirectional:
                self.emit(indent + "_value = max(0.0, _value)")
            self.emit(indent + f"state[{self.slot_names[layout.flow_natural[flow.id].index]}] = _value")
            self.emit(indent + f"{self._flow_effect_name(flow.id)}(state, _value)")

        self.emit("")
        self.emit("    # Specified natural-flow imports/diversions/storage flows.")
        for flow in graph.graph.interzone_flows:
            specified = layout.specified_natural_flow.get(flow.id)
            if specified is None:
                continue
            active = layout.boundary_natural_active.get(flow.id)
            if active is not None:
                self.emit(f"    if state[{self.slot_names[active.index]}] < 0.5:")
                indent = "        "
            else:
                indent = "    "
            self.emit(indent + f"_value = state[{self.slot_names[specified.index]}]")
            if not flow.bidirectional:
                self.emit(indent + "if _value < -TOL:")
                self.emit(indent + f"    raise SolverError({('Specified natural flow cannot be negative for ' + flow.id)!r})")
            self.emit(indent + f"state[{self.slot_names[layout.flow_natural[flow.id].index]}] = _value")
            self.emit(indent + f"{self._flow_effect_name(flow.id)}(state, _value)")

        self.emit("")
        self.emit("    # Preserve original NF as the initial remaining-NF state.")
        for zone_id, natural_slot in layout.natural_at_zone.items():
            remaining_slot = layout.natural_flow[zone_id]
            self.emit(
                f"    state[{self.slot_names[remaining_slot.index]}] = max(0.0, state[{self.slot_names[natural_slot.index]}])"
            )

        self.emit("")
        self.emit("    # Remove natural flow already apportioned outside external boundaries.")
        for flow_id, boundary in layout.boundary_natural_flow.items():
            flow = graph.get_flow_by_id(flow_id)
            active = layout.boundary_natural_active[flow_id]
            measured = layout.measurements[flow_id]
            self.emit(f"    if state[{self.slot_names[active.index]}] >= 0.5:  # {flow_id!r}")
            self.emit(
                f"        _already_apportioned = state[{self.slot_names[boundary.index]}] - state[{self.slot_names[measured.index]}]"
            )
            self.emit(
                f"        _amount_at_entry = _deliver(state, {self._loss_factor_slot(flow_id, 'to')}, _already_apportioned)"
            )
            if graph.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM:
                self.emit(
                    f"        {self._adjust_remaining_name(flow.to_zone)}(state, -_amount_at_entry)"
                )

        # Prevent rem. natural flow from being negative after externals
        for remaining_slot in layout.natural_flow.values():
            self.emit(
                f"    state[{self.slot_names[remaining_slot.index]}] = max(0.0, state[{self.slot_names[remaining_slot.index]}])"
            )
        self.emit("")

    def emit_spill_credit_function(self):
        layout = self.state_layout
        graph = layout.graph
        self.emit("def _apply_spill_credits(state):")
        self.emit("    _total_credit = 0.0")
        if not layout.spill_credits:
            self.emit("    return 0.0")
            self.emit("")
            return
        for spill in layout.spill_credits:
            flow = graph.get_flow_by_id(spill.flow_id)
            available = self.slot_names[spill.available.index]
            capacity = self.slot_names[spill.directional_capacity.index]
            self.emit(f"    # Spill/import credit on {spill.flow_id!r} into {spill.receiving_zone!r}")
            self.emit(f"    _signed = float(state[{available}])")
            self.emit(f"    _residual = max(0.0, _signed * {spill.factor!r})")
            self.emit("    if _residual > SPILL_TOL:")
            self.emit(f"        state[{available}] = 0.0")
            self.emit(f"        state[{capacity}] = 0.0")
            endpoint = "to" if spill.factor > 0 else "from"
            self.emit(
                f"        _credit = _deliver(state, {self._loss_factor_slot(spill.flow_id, endpoint)}, _residual)"
            )
            self.emit("        if _credit > SPILL_TOL:")
            self.emit("            _total_credit += _credit")
            self.emit(
                f"            {self._adjust_remaining_name(spill.receiving_zone)}(state, _credit)"
            )
        self.emit("    return _total_credit")
        self.emit("")

    def emit_natural_flow_program(self):
        # Tiny codegen unit tests sometimes use a slots-only stand-in layout.
        # Keep those tests useful without requiring a full accounting graph.
        if not hasattr(self.state_layout, "graph") or not hasattr(self.state_layout, "natural_flow"):
            self.emit("def _initialize_natural_flow(state):")
            self.emit("    return None")
            self.emit("")
            self.emit("def _apply_spill_credits(state):")
            self.emit("    return 0.0")
            self.emit("")
            return
        self.emit_loss_transform_functions()
        self.emit_nf_route_selectors()
        self.emit_nf_propagation_functions()
        self.emit_nf_flow_effect_functions()
        self.emit_nf_calculated_loss_functions()
        self.emit_nf_routing_coefficient_functions()
        self.emit_nf_initialization()
        self.emit_spill_credit_function()

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
            self.emit_direct(operation, function_name, external_name)
        elif isinstance(operation, ProportionalCalculationKernel):
            self.emit_proportional(operation, function_name, external_name)
        elif isinstance(operation, ScalarFormulaKernel):
            self.emit_scalar_formula(operation, function_name, external_name)
        elif isinstance(operation, LPKernel):
            self.emit_lp(operation, function_name, external_name)
        else:
            raise TypeError(f"Unknown compiled operation: {operation!r}")

    def build(self, operations) -> GeneratedPlanSource:
        """Generate the python code that will execute the calculations."""

        all_operations = tuple(
            kernel
            for operation in operations
            for kernel in operation.kernels()
        )

        self.emit("from math import isfinite, isinf, isnan")
        self.emit("")
        self.emit("# Numerical tolerances.")
        self.emit(f"TOL = {TOL!r}")
        self.emit("NF_TOL = 1e-6")
        self.emit("SPILL_TOL = 1e-7")
        self.emit("")
        self.emit("# Errors raised by the compiled calculation.")
        self.emit("from ut_water_apportionment.compile.kernel import BlockLPError as SolverError")
        self.emit("class FormulaGuardFailed(RuntimeError):")
        self.emit("    pass")
        self.emit("class FormulaEvaluationError(RuntimeError):")
        self.emit("    pass")
        self.emit("")
        self.emit("# Use names in place of slot indexes for more readable code.")
        for slot in sorted(self.state_layout.slots.values(), key=lambda value: value.index):
            self.emit(f"{self.slot_names[slot.index]} = {slot.index}")
        self.emit("")

        if any(isinstance(operation, DirectCalculationKernel) for operation in all_operations):
            self.emit(_DIRECT_SOURCE.strip())
            self.emit("")
        if any(isinstance(operation, ScalarFormulaKernel) for operation in all_operations):
            self.emit(PROJECTED_ROW_SOURCE.strip())
            self.emit("")
        if any(
            isinstance(operation, (ProportionalCalculationKernel, ScalarFormulaKernel))
            and isinstance(operation.model.rule, Proportional)
            for operation in all_operations
        ):
            self.emit(_PROPORTIONAL_SOURCE.strip())
            self.emit("")

        self.prepare_direct_formulas(all_operations)

        # Natural-flow setup is part of the same generated/executed program.
        self.emit_natural_flow_program()

        block_names = []
        for index, compiled_operation in enumerate(operations):
            operation = compiled_operation.primary
            completion = compiled_operation.counterflow
            primary_fn = f"_block_{index}_direct"
            primary_ext = f"_BLOCK_{index}_DIRECT_FALLBACK"
            self.emit("# " + "=" * 76)
            self.emit(
                f"# BLOCK {index} direct stage: {list(operation.model.updates)!r} "
                f"({type(operation).__name__})"
            )
            self.emit("# " + "=" * 76)
            self.emit_operation(operation, primary_fn, primary_ext)

            counterflow_fn = None
            if completion is not None:
                counterflow_operation = completion.operation
                counterflow_fn = f"_block_{index}_counterflow"
                counterflow_ext = f"_BLOCK_{index}_COUNTERFLOW_FALLBACK"
                self.emit("# " + "=" * 76)
                self.emit(
                    f"# BLOCK {index} counterflow completion: "
                    f"{list(counterflow_operation.model.updates)!r} "
                    f"({type(counterflow_operation).__name__})"
                )
                self.emit("# " + "=" * 76)
                self.emit_operation(
                    counterflow_operation, counterflow_fn, counterflow_ext
                )

            block_fn = f"_block_{index}"
            block_names.append(block_fn)
            self.emit(f"def {block_fn}(state):")
            self.emit(f"    lp_solves = {primary_fn}(state)")
            if counterflow_fn is not None:
                gate_expr = " or ".join(
                    "("
                    f"state[{self.slot_names[forward.index]}] <= TOL and "
                    f"state[{self.slot_names[reverse.index]}] <= TOL"
                    ")"
                    for forward, reverse in completion.gate_slots
                ) or "True"
                target_limit_expr = " or ".join(
                    f"state[{self.slot_names[self.state_layout.limits[name].index]}] > TOL"
                    for name in operation.model.updates
                    if name in self.state_layout.limits
                ) or "False"
                self.emit(f"    if ({gate_expr}) and ({target_limit_expr}):")
                self.emit(f"        lp_solves += {counterflow_fn}(state)")
                for flow_id in completion.normalize_flows:
                    available = self.slot_names[
                        self.state_layout.measurement_available[flow_id].index
                    ]
                    forward = self.slot_names[
                        self.state_layout.measurement_forward_remaining[flow_id].index
                    ]
                    reverse_slot = self.state_layout.measurement_reverse_remaining.get(flow_id)
                    self.emit(
                        f"        state[{forward}] = max(0.0, state[{available}])"
                    )
                    if reverse_slot is not None:
                        reverse = self.slot_names[reverse_slot.index]
                        self.emit(
                            f"        state[{reverse}] = max(0.0, -state[{available}])"
                        )
            self.emit("    return lp_solves")
            self.emit("")

        self.emit_direct_validation()
        self.emit("def execute(state):")
        self.emit("    _initialize_natural_flow(state)")
        self.emit("    _validate_direct_inputs(state)")
        self.emit("    lp_solves = 0")
        self.emit("")
        self.emit("    # Initial priority sweep. Counterflow is handled inside each block.")
        for fn in block_names:
            self.emit(f"    lp_solves += {fn}(state)")
        self.emit("")
        self.emit("    # Apply spill/import natural-flow credits after the initial sweep.")
        self.emit("    _apply_spill_credits(state)")
        if getattr(self.state_layout, "spill_credits", None):
            self.emit("")
            self.emit("    # Offer newly credited NF using the exact same priority blocks.")
            for fn in block_names:
                self.emit(f"    lp_solves += {fn}(state)")
        self.emit("")
        self.emit("    return lp_solves")
        self.emit("")

        return GeneratedPlanSource("\n".join(self.lines), dict(self.namespace))


def generate_plan_source(operations, state_layout) -> GeneratedPlanSource:
    return PythonPlanEmitter(state_layout).build(operations)
