"""Frozen parameterized v2 LP backend.

Preparation compiles each encountered structural objective once.  Numeric LP
bounds and selected matrix coefficients are parameter slots inside the frozen
IR, so later days reuse the same transformed equations/kernels and refresh only
coefficient/bound/RHS values.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from math import inf, isfinite
from typing import Callable

from .lp_engine import LPSolver as ScipyLPSolver
from .compiler import (
    V2CannotCompile,
    V2CompilationOptions,
    compile_equal_priority_kernel,
    SequentialDirectContext,
    compile_objective,
    objective_signature,
)
from .program import DirectScalarProgram, EqualPriorityProgram, V2Program


@dataclass
class V2CompilationSession:
    options: V2CompilationOptions = field(default_factory=V2CompilationOptions)
    programs: list[V2Program] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    _cache: dict[tuple, list[V2Program]] = field(default_factory=dict, repr=False)
    _equal_priority_cache: dict[tuple, list[EqualPriorityProgram]] = field(
        default_factory=dict, repr=False
    )
    # Direct execution index populated during structural preparation. Runtime
    # equal-priority loops already know the compiled coefficient regime and a
    # cohort member, so they should not fingerprint and scan the production LP
    # merely to rediscover the frozen program.
    _equal_priority_by_regime_member: dict[
        tuple[tuple, str], list[EqualPriorityProgram]
    ] = field(default_factory=dict, repr=False)
    # Sequential execution already knows the structural regime and target
    # transaction.  Index scalar programs the same way as equal-priority
    # kernels so daily execution never rebuilds an objective signature by
    # scanning the production LP.
    _scalar_by_regime_target: dict[
        tuple[tuple, str], list[V2Program]
    ] = field(default_factory=dict, repr=False)
    frozen: bool = False
    preparing: bool = False
    # Runtime can omit rebuilding natural-flow coefficients on the production
    # LP when no frozen kernel reads those coefficients. If a genuinely
    # auxiliary production-LP solve is needed, Apportioner materializes them
    # lazily immediately before that solve.
    defer_production_nf_coefficients: bool = False

    def next_name(self) -> str:
        return f"V2P{len(self.programs) + 1}"

    def begin_preparation(self) -> None:
        self.preparing = True
        self.frozen = False
        self.stats.clear()

    def finish_preparation(self) -> None:
        self.preparing = False
        self._validate_guard_coverage()
        if self.options.freeze_after_prepare:
            self.frozen = True
        self.stats["prepared_program_count"] = len(self.programs)

    def _validate_guard_coverage(self) -> None:
        """Require a frozen guard-free alternate for every regional program."""

        guarded = 0
        covered = 0
        all_variant_sets = list(self._cache.items()) + list(self._equal_priority_cache.items())
        for signature, variants in all_variant_sets:
            guarded_variants = [
                program
                for program in variants
                if getattr(getattr(program, "model", None), "guards", None)
            ]
            if not guarded_variants:
                continue
            guarded += len(guarded_variants)
            alternates = [
                program
                for program in variants
                if getattr(program, "model", None) is not None
                and not program.model.guards
            ]
            if not alternates:
                raise V2CannotCompile(
                    "Guarded v2 simplification has no frozen guard-free alternate "
                    f"for objective signature {signature!r}"
                )
            covered += len(guarded_variants)
        self.stats["guarded_programs"] = guarded
        self.stats["guarded_programs_with_fallback"] = covered

    def prepare_objective(
        self,
        engine,
        *,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
        residual_transaction_names: set[str] | None = None,
        committed_transaction_names: set[str] | None = None,
        regime_signature: tuple | None = None,
        base_logical_model=None,
        base_collapse_stats: dict[str, int] | None = None,
        sequential_direct_context: SequentialDirectContext | None = None,
    ) -> None:
        """Compile every frozen variant for one structural objective.

        This method never solves the LP.  It is called by the structural
        preparation pass after the production LP coefficient matrix has been
        built for a structural date regime.
        """

        signature = objective_signature(
            engine,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
        )
        if signature in self._cache:
            if regime_signature is not None and len(variable_names) == 1:
                for program in self._cache[signature]:
                    self._index_scalar_program(
                        regime_signature, variable_names[0], program
                    )
            self.stats["structural_duplicate_objectives"] += 1
            return

        guarded = compile_objective(
            engine,
            name=self.next_name(),
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
            options=self.options,
            residual_transaction_names=residual_transaction_names,
            committed_transaction_names=committed_transaction_names,
            base_logical_model=base_logical_model,
            base_collapse_stats=base_collapse_stats,
            sequential_direct_context=sequential_direct_context,
        )
        self._record_program(signature, guarded)
        if regime_signature is not None and len(variable_names) == 1:
            self._index_scalar_program(
                regime_signature, variable_names[0], guarded
            )

        # A guarded direct formula is intentionally regional.  Freeze one
        # conservative variant as well so runtime parameter changes never
        # trigger compilation.
        if getattr(guarded, "model", None) is not None and guarded.model.guards:
            conservative_options = replace(
                self.options,
                enable_guarded_redundancy=False,
            )
            conservative = compile_objective(
                engine,
                name=self.next_name(),
                variable_names=variable_names,
                maximization=maximization,
                weights=weights,
                options=conservative_options,
                residual_transaction_names=residual_transaction_names,
                committed_transaction_names=committed_transaction_names,
                base_logical_model=base_logical_model,
                base_collapse_stats=base_collapse_stats,
                sequential_direct_context=sequential_direct_context,
            )
            if getattr(conservative, "model", None) is None or conservative.model.guards:
                raise V2CannotCompile(
                    "Could not freeze a guard-free alternate for a parameter-dependent "
                    "v2 simplification"
                )
            self._record_program(signature, conservative)
            if regime_signature is not None and len(variable_names) == 1:
                self._index_scalar_program(
                    regime_signature, variable_names[0], conservative
                )
            self.stats["prepared_unguarded_variants"] += 1

    def _index_scalar_program(
        self,
        regime_signature: tuple,
        target: str,
        program: V2Program,
    ) -> None:
        bucket = self._scalar_by_regime_target.setdefault(
            (regime_signature, target), []
        )
        if program not in bucket:
            bucket.append(program)

    @staticmethod
    def _scalar_variant_sort_key(program: V2Program) -> tuple:
        model = getattr(program, "model", None)
        guards = getattr(model, "guards", ()) if model is not None else ()
        variables = getattr(model, "variables", {}) if model is not None else {}

        # Prefer parameter-region-independent scalar execution over a smaller
        # guarded formula when the conservative kernel has already compiled a
        # proven lower-bound projection.  The projected kernel binds only its
        # compact one-variable IR and avoids evaluating hundreds of guard slots
        # merely to rediscover that a later sequential priority has entered a
        # different residual region.
        is_direct = program.__class__.__name__ == "DirectScalarProgram"
        has_projection = getattr(program, "_projected_scalar", None) is not None
        if is_direct and not guards:
            tier = 0
        elif has_projection and not guards:
            tier = 1
        elif guards:
            tier = 2
        else:
            tier = 3
        return (tier, len(variables), program.name)

    def resolve_scalar_with_parameters(
        self,
        engine,
        target: str,
    ) -> tuple[V2Program, object] | None:
        """Resolve a sequential scalar directly from frozen regime IR.

        Returning ``None`` preserves compatibility for callers that execute a
        backend outside ``DayExecutionProgram``; the legacy structural resolver
        can still handle those cases.
        """

        regime_signature = getattr(engine, "v2_regime_signature", None)
        variants = (
            self._scalar_by_regime_target.get((regime_signature, target), [])
            if regime_signature is not None
            else []
        )
        if not variants and len({key[0] for key in self._scalar_by_regime_target}) == 1:
            only_regime = next(iter({key[0] for key in self._scalar_by_regime_target}))
            variants = self._scalar_by_regime_target.get((only_regime, target), [])
        for program in sorted(variants, key=self._scalar_variant_sort_key):
            model = getattr(program, "model", None)
            if model is None:
                return program, {}
            # Dynamic matrix coefficients can change the relationship between a
            # collapsed path transaction and other compiler rows (notably NF
            # rows).  Until all such rows are parameterized through the same
            # transformed coefficient algebra, retain the legacy structural
            # resolver/auxiliary fallback for those uncommon objectives. Static
            # scalar programs take the O(1) direct path used by large systems.
            if any(reader[0] == "coefficient" for reader in model._runtime_readers()):
                self.stats["execution_scalar_dynamic_coefficient_fallbacks"] += 1
                return None

            # A guard-free conservative kernel with a proven scalar projection
            # performs its own compact parameter binding in execute().  Reading
            # the full conservative model here would defeat the projection by
            # touching every junior transaction bound before execution.
            if (
                not model.guards
                and getattr(program, "_projected_scalar", None) is not None
            ):
                self.stats["execution_scalar_direct_index_hits"] += 1
                self.stats["execution_cache_hits"] += 1
                self.stats["execution_scalar_projected_index_hits"] += 1
                return program, {}

            try:
                if isinstance(program, DirectScalarProgram):
                    parameters = program.runtime_parameter_array(engine)
                    program.check_indexed_guards(parameters)
                else:
                    layout = getattr(program, "_parameter_layout", None)
                    guards = getattr(program, "_indexed_guards", None)
                    if layout is not None and guards is not None:
                        parameters = layout.read(engine)
                        model.check_indexed_guards(guards, parameters)
                    else:
                        parameters = model.runtime_parameters(engine)
                        model.check_guards(parameters)
            except (ValueError, KeyError):
                continue
            self.stats["execution_scalar_direct_index_hits"] += 1
            self.stats["execution_cache_hits"] += 1
            return program, parameters
        if variants:
            self.stats["execution_scalar_direct_index_misses"] += 1
        return None


    @staticmethod
    def _equal_priority_structure_signature(engine) -> tuple:
        # Reuse the exact production-LP structural fingerprint used by scalar
        # objectives, but omit any objective target. Bounds/RHS are runtime
        # parameters and therefore intentionally absent.
        return objective_signature(
            engine,
            variable_names=[],
            maximization=True,
            weights=None,
        )

    def prepare_equal_priority(
        self,
        engine,
        *,
        member_transaction_names: list[str],
        priority: float,
        residual_transaction_names: set[str],
        committed_transaction_names: set[str] | None = None,
        regime_signature: tuple | None = None,
        base_logical_model=None,
        base_collapse_stats: dict[str, int] | None = None,
    ) -> None:
        """Freeze one logical equal-priority cohort for this LP regime."""

        members = tuple(dict.fromkeys(member_transaction_names))
        structure = self._equal_priority_structure_signature(engine)
        cache_key = (structure, tuple(sorted(members)))
        existing = self._equal_priority_cache.get(cache_key, [])
        if existing:
            if regime_signature is not None:
                for program in existing:
                    self._index_equal_priority_program(regime_signature, program)
            self.stats["structural_duplicate_equal_priority_kernels"] += 1
            return

        guarded = compile_equal_priority_kernel(
            engine,
            name=self.next_name(),
            member_transaction_names=list(members),
            priority=priority,
            options=self.options,
            residual_transaction_names=residual_transaction_names,
            committed_transaction_names=committed_transaction_names,
            base_logical_model=base_logical_model,
            base_collapse_stats=base_collapse_stats,
        )
        self._record_equal_priority_program(
            cache_key, guarded, regime_signature=regime_signature
        )

        if guarded.model.guards:
            conservative_options = replace(
                self.options,
                enable_guarded_redundancy=False,
            )
            conservative = compile_equal_priority_kernel(
                engine,
                name=self.next_name(),
                member_transaction_names=list(members),
                priority=priority,
                options=conservative_options,
                residual_transaction_names=residual_transaction_names,
                committed_transaction_names=committed_transaction_names,
                base_logical_model=base_logical_model,
                base_collapse_stats=base_collapse_stats,
            )
            if conservative.model.guards:
                raise V2CannotCompile(
                    "Could not freeze a guard-free equal-priority alternate"
                )
            self._record_equal_priority_program(
                cache_key, conservative, regime_signature=regime_signature
            )
            self.stats["prepared_unguarded_equal_priority_variants"] += 1

    def _index_equal_priority_program(
        self,
        regime_signature: tuple,
        program: EqualPriorityProgram,
    ) -> None:
        for member_id in program.member_ids:
            bucket = self._equal_priority_by_regime_member.setdefault(
                (regime_signature, member_id), []
            )
            if program not in bucket:
                bucket.append(program)

    def _record_equal_priority_program(
        self,
        cache_key: tuple,
        program: EqualPriorityProgram,
        *,
        regime_signature: tuple | None = None,
    ) -> EqualPriorityProgram:
        self._equal_priority_cache.setdefault(cache_key, []).append(program)
        if regime_signature is not None:
            self._index_equal_priority_program(regime_signature, program)
        self.programs.append(program)
        self.stats["compiled_programs"] += 1
        self.stats["equal_priority_compiled_kernels"] += 1
        for key, value in program.stats.items():
            self.stats[key] += value
        return program

    @staticmethod
    def _equal_priority_variant_sort_key(program: EqualPriorityProgram) -> tuple:
        """Prefer the smallest guarded reduction before its conservative fallback.

        Program names are not a valid variant ordering (for example ``V2P10``
        sorts before ``V2P9``).  Guarded variants are intentionally tried first;
        runtime guard failure then falls through to the frozen guard-free kernel.
        """
        return (
            len(program.member_ids),
            0 if program.model.guards else 1,
            len(program.model.variables),
            program.name,
        )

    def _equal_priority_candidates(
        self,
        engine,
        transaction_ids: list[str],
    ) -> list[EqualPriorityProgram]:
        """Return cohort variants without re-fingerprinting the production LP.

        ``DayExecutionProgram`` installs the already-resolved compiler regime on
        the engine. One member therefore gives us the exact frozen cohort family
        in O(1); the requested subset is only a runtime water-filling detail.
        The old structural scan remains as a compatibility fallback for tests or
        developer use that invoke the backend outside a day execution program.
        """

        requested = set(transaction_ids)
        if not requested:
            return []

        regime_signature = getattr(engine, "v2_regime_signature", None)
        if regime_signature is not None:
            first = next(iter(requested))
            direct = self._equal_priority_by_regime_member.get(
                (regime_signature, first), []
            )
            candidates = [
                program
                for program in direct
                if requested.issubset(program.member_ids)
            ]
            if candidates:
                return sorted(
                    candidates,
                    key=self._equal_priority_variant_sort_key,
                )

        # Single-regime plans can still use the direct member index even when a
        # caller has not explicitly attached the regime to the engine.
        indexed_regimes = {key[0] for key in self._equal_priority_by_regime_member}
        if len(indexed_regimes) == 1:
            only_regime = next(iter(indexed_regimes))
            first = next(iter(requested))
            direct = self._equal_priority_by_regime_member.get(
                (only_regime, first), []
            )
            candidates = [
                program
                for program in direct
                if requested.issubset(program.member_ids)
            ]
            if candidates:
                return sorted(
                    candidates,
                    key=self._equal_priority_variant_sort_key,
                )

        # Compatibility fallback for sessions prepared without a regime index.
        structure = self._equal_priority_structure_signature(engine)
        candidates: list[EqualPriorityProgram] = []
        for (candidate_structure, _members), variants in self._equal_priority_cache.items():
            if candidate_structure != structure:
                continue
            for program in variants:
                if requested.issubset(program.member_ids):
                    candidates.append(program)

        if not candidates:
            structural_keys = {key[0] for key in self._equal_priority_cache}
            if len(structural_keys) == 1:
                for variants in self._equal_priority_cache.values():
                    for program in variants:
                        if requested.issubset(program.member_ids):
                            candidates.append(program)

        return sorted(candidates, key=self._equal_priority_variant_sort_key)

    def resolve_equal_priority_with_parameters(
        self,
        engine,
        transaction_ids: list[str],
    ) -> tuple[EqualPriorityProgram, object]:
        """Resolve a cohort and its runtime parameters in one pass.

        Previously guard selection evaluated ``runtime_parameters()`` and then
        the selected program evaluated the same parameter set again. Returning
        the already-evaluated mapping removes that duplicate daily work.
        """

        requested = set(transaction_ids)
        for program in self._equal_priority_candidates(engine, transaction_ids):
            model = program.model
            try:
                parameters = program._parameter_layout.read(engine)
                model.check_indexed_guards(program._indexed_guards, parameters)
            except (ValueError, KeyError):
                continue
            self.stats["execution_equal_priority_cache_hits"] += 1
            return program, parameters

        self.stats["execution_equal_priority_cache_misses"] += 1
        raise V2CannotCompile(
            "Frozen v2 plan has no applicable logical equal-priority kernel "
            f"for members {sorted(requested)!r}"
        )

    def resolve_equal_priority(
        self,
        engine,
        transaction_ids: list[str],
    ) -> EqualPriorityProgram:
        """Compatibility wrapper returning only the selected frozen cohort."""

        program, _parameters = self.resolve_equal_priority_with_parameters(
            engine, transaction_ids
        )
        return program

    def equal_priority_column_signature(
        self,
        engine,
        transaction_id: str,
    ) -> tuple:
        """Return a conservative structural member signature without parameters."""

        candidates = self._equal_priority_candidates(engine, [transaction_id])
        if not candidates:
            raise V2CannotCompile(
                "Frozen v2 plan has no logical equal-priority kernel for "
                f"member {transaction_id!r}"
            )
        # Prefer the guard-free alternate. If columns are identical there,
        # grouping them is valid in every narrower guarded region as well.
        program = next(
            (candidate for candidate in candidates if not candidate.model.guards),
            candidates[0],
        )
        return program.column_signature(transaction_id)

    def reset_execution_stats(self) -> None:
        for key in list(self.stats):
            if key.startswith("execution_"):
                del self.stats[key]

    @staticmethod
    def _program_applicable(program: V2Program, engine) -> bool:
        model = getattr(program, "model", None)
        if model is None:
            return True
        try:
            if isinstance(program, DirectScalarProgram):
                parameters = program.runtime_parameter_array(engine)
                program.check_indexed_guards(parameters)
            elif isinstance(program, EqualPriorityProgram):
                parameters = program._parameter_layout.read(engine)
                model.check_indexed_guards(program._indexed_guards, parameters)
            else:
                layout = getattr(program, "_parameter_layout", None)
                guards = getattr(program, "_indexed_guards", None)
                if layout is not None and guards is not None:
                    parameters = layout.read(engine)
                    model.check_indexed_guards(guards, parameters)
                else:
                    parameters = model.runtime_parameters(engine)
                    model.check_guards(parameters)
        except (ValueError, KeyError):
            return False
        return True

    def _record_program(self, signature: tuple, program: V2Program) -> V2Program:
        self._cache.setdefault(signature, []).append(program)
        self.programs.append(program)
        self.stats["compiled_programs"] += 1
        if program.__class__.__name__ == "DirectScalarProgram":
            self.stats["direct_programs"] += 1
        elif isinstance(program, EqualPriorityProgram):
            self.stats["equal_priority_compiled_kernels"] += 1
        else:
            self.stats["reduced_lp_kernels"] += 1
        for key, value in program.stats.items():
            self.stats[key] += value
        return program

    def resolve(
        self,
        engine,
        *,
        variable_names: list[str],
        maximization: bool,
        weights: dict[str, float] | None,
    ) -> V2Program:
        signature = objective_signature(
            engine,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
        )
        variants = self._cache.get(signature, [])
        for program in variants:
            if self._program_applicable(program, engine):
                if self.preparing:
                    self.stats["preparation_cache_hits"] += 1
                else:
                    self.stats["execution_cache_hits"] += 1
                    model = getattr(program, "model", None)
                    if model is not None and model.guards:
                        self.stats["execution_guarded_variant_hits"] += 1
                    elif any(
                        getattr(getattr(candidate, "model", None), "guards", None)
                        for candidate in variants
                    ):
                        self.stats["execution_guard_fallback_hits"] += 1
                return program

        if self.frozen:
            self.stats["execution_cache_misses"] += 1
            raise V2CannotCompile(
                "Frozen v2 plan encountered a parameter region/objective "
                "structure that was not seen during preparation. Recompile "
                "the plan for this schedule/parameter region."
            )

        # Kept for developer use when freeze_after_prepare=False.  Normal v2
        # plans compile structurally through prepare_objective() and never take
        # this path at runtime.
        options = self.options
        if variants:
            options = replace(options, enable_guarded_redundancy=False)
            self.stats["prepared_unguarded_variants"] += 1

        program = compile_objective(
            engine,
            name=self.next_name(),
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
            options=options,
        )
        return self._record_program(signature, program)

    def routine_assignment_lines(self, transaction_id: str, *, indent: int = 0) -> list[str]:
        """Render the preferred frozen assignment plus any guarded fallback."""

        from .program import DirectScalarProgram

        candidates = [
            program for program in self.programs
            if getattr(program, "active_name", None) == transaction_id
            or set(program.objective.variables) == {transaction_id}
        ]
        direct = next(
            (program for program in candidates if isinstance(program, DirectScalarProgram)),
            None,
        )
        pad = " " * indent
        if direct is not None:
            lines = direct.assignment_lines(indent=indent)
            fallbacks = [program for program in candidates if program is not direct]
            if direct.model.guards and fallbacks:
                lines.append(f"{pad}GUARD CONDITIONS:")
                for guard in direct.model.guards:
                    lines.append(f"{pad}    REQUIRE {guard.text()}")
                lines.append(
                    f"{pad}IF ANY GUARD FAILS: use frozen alternate {fallbacks[0].name}"
                )
            return lines
        if candidates:
            return [f"{pad}{transaction_id} = {candidates[0].name} REDUCED_LP_KERNEL(...)"]
        return [f"{pad}{transaction_id} = AUXILIARY_REDUCED_LP_KERNEL(...)"]

    def formulas(self) -> str:
        lines = [
            "COMPILED V2 — STRUCTURAL FROZEN IR",
            "===================================",
            "",
            "SolverInput -> production LP -> parameterized compiler LP ->",
            "frozen algebraic substitutions/presolve -> direct equation or reduced LP kernel.",
            "",
            "The objective graph is derived from transaction/LP structure, not from a",
            "numerical solve trace. Numeric variable/constraint bounds and selected",
            "LP coefficients are runtime parameters. No objective compilation occurs",
            "during plan.solve().",
            "",
            "Unique sequential priorities first attempt direct sparse-column MIN compilation;",
            "only non-monotone/coupled cases enter the generic per-objective presolve path.",
            "",
            "Equal-priority water-filling is compiled over logical transaction residuals",
            "with one runtime common-increment scalar. Directional ambiguity tie-breaks may",
            "still use structural auxiliary LP kernels. Reporting slacks, physical spill residuals,",
            "and final path-leg outputs are derived directly; they do not add compiler",
            "programs or alter the frozen objective graph.",
            "",
        ]
        if not self.programs:
            lines.append("(no objective structures were encountered during preparation)")
        for program in self.programs:
            lines.append(program.text())
            lines.append("")
        return "\n".join(lines).rstrip()

    def report(self) -> dict:
        return {
            "program_count": len(self.programs),
            "prepared_program_count": len(self.programs),
            "direct_programs": self.stats["direct_programs"],
            "reduced_lp_kernels": self.stats["reduced_lp_kernels"],
            "indexed_direct_programs": self.stats["indexed_direct_programs"],
            "indexed_equal_priority_programs": self.stats[
                "indexed_equal_priority_programs"
            ],
            "indexed_reduced_lp_programs": self.stats[
                "indexed_reduced_lp_programs"
            ],
            "indexed_parameter_slots_total": self.stats[
                "indexed_parameter_slots"
            ],
            "early_direct_sequential_programs": self.stats[
                "early_direct_sequential_programs"
            ],
            "sequential_direct_contexts": self.stats["sequential_direct_contexts"],
            "sequential_zero_safe_transactions": self.stats[
                "sequential_zero_safe_transactions"
            ],
            "sequential_residual_lower_sides_structural": self.stats[
                "sequential_residual_lower_sides_structural"
            ],
            "sequential_residual_upper_sides_structural": self.stats[
                "sequential_residual_upper_sides_structural"
            ],
            "early_direct_sequential_rows": self.stats[
                "early_direct_sequential_rows"
            ],
            "early_direct_sequential_structural_sides_removed": self.stats[
                "early_direct_sequential_structural_sides_removed"
            ],
            "logical_transactions_collapsed": self.stats["logical_transactions_collapsed"],
            "path_leg_variables_collapsed": self.stats["path_leg_variables_collapsed"],
            "continuity_rows_removed": self.stats["continuity_rows_removed"],
            "residual_variables_rebased": self.stats["residual_variables_rebased"],
            "committed_variables_removed": self.stats["committed_variables_removed"],
            "residual_constraint_sides_parameterized": self.stats[
                "residual_constraint_sides_parameterized"
            ],
            "equality_eliminated": self.stats["equality_eliminated"],
            "fixed_eliminated": self.stats["fixed_eliminated"],
            "slack_eliminated": self.stats["slack_eliminated"],
            "monotone_eliminated": self.stats["monotone_eliminated"],
            "whole_day_lp_fallbacks": 0,
            "runtime_compilation": not self.frozen,
            "frozen": self.frozen,
            "preparation_cache_hits": self.stats["preparation_cache_hits"],
            "prepared_unguarded_variants": self.stats["prepared_unguarded_variants"],
            "guarded_programs": self.stats["guarded_programs"],
            "guarded_programs_with_fallback": self.stats[
                "guarded_programs_with_fallback"
            ],
            "execution_cache_hits": self.stats["execution_cache_hits"],
            "execution_cache_misses": self.stats["execution_cache_misses"],
            "execution_guarded_variant_hits": self.stats[
                "execution_guarded_variant_hits"
            ],
            "execution_guard_fallback_hits": self.stats[
                "execution_guard_fallback_hits"
            ],
            "structural_preparation": True,
            "date_traced_preparation": False,
            "structural_regimes": self.stats["structural_regimes"],
            "structural_duplicate_regimes": self.stats["structural_duplicate_regimes"],
            "structural_duplicate_objectives": self.stats["structural_duplicate_objectives"],
            "auxiliary_kernel_solves": self.stats["auxiliary_kernel_solves"],
            "equal_priority_kernel_members": self.stats["equal_priority_kernel_members"],
            "equal_priority_compiled_kernels": self.stats["equal_priority_compiled_kernels"],
            "equal_priority_logical_members": self.stats["equal_priority_logical_members"],
            "equal_priority_scalar_auxiliary": self.stats["equal_priority_scalar_auxiliary"],
            "equal_priority_direct_common_increment_formulas": self.stats[
                "equal_priority_direct_common_increment_formulas"
            ],
            "execution_equal_priority_common_increment_solves": self.stats[
                "execution_equal_priority_common_increment_solves"
            ],
            "execution_equal_priority_direct_common_increment_formulas": self.stats[
                "execution_equal_priority_direct_common_increment_formulas"
            ],
            "execution_equal_priority_lp_common_increment_solves": self.stats[
                "execution_equal_priority_lp_common_increment_solves"
            ],
            "execution_equal_priority_classification_solves": self.stats[
                "execution_equal_priority_classification_solves"
            ],
            "execution_equal_priority_scalar_solves": self.stats[
                "execution_equal_priority_scalar_solves"
            ],
            "execution_equal_priority_cache_hits": self.stats[
                "execution_equal_priority_cache_hits"
            ],
            "execution_equal_priority_cache_misses": self.stats[
                "execution_equal_priority_cache_misses"
            ],
            "explicit_execution_ir": True,
            "execution_residual_commits": self.stats["execution_residual_commits"],
            "execution_residual_row_updates": self.stats["execution_residual_row_updates"],
            "derived_slack_reconciliations": self.stats["derived_slack_reconciliations"],
            "derived_slack_values": self.stats["derived_slack_values"],
            "derived_path_reconstructions": self.stats["derived_path_reconstructions"],
        }


class V2LPSolver(ScipyLPSolver):
    """LP protocol implementation backed by frozen v2 objective programs."""

    def __init__(self, *, session: V2CompilationSession, tolerance: float | None = None):
        self._v2_revision = 0
        self._v2_cached_problem_revision = -1
        self._v2_cached_problem = None
        super().__init__(tolerance=tolerance, method="highs-ds", presolve=True)
        self.v2_session = session
        # Marks constructor-time bounds that later become runtime state. The
        # parameterized model uses this to distinguish a structural zero from a
        # measurement/current-allocation value that happens to be zero today.
        self._v2_dynamic_bound_sides: set[tuple[str, str, str]] = set()
        # Keep the compiled execution sequence structural rather than dependent
        # on today's NF exhaustion. A zero-capacity formula simply returns the
        # already committed value.
        self.force_explicit_priority_solves = True
        # Reporting slack outputs are reconstructed from leftover measurement
        # residuals by the day IR. One-direction and pure bidirectional reporting
        # rows are projected out; storage-direction proxy columns can remain only
        # for the allocation ambiguity convention and are never reported directly.
        self.derive_reporting_slacks = True
        self.defer_nf_coefficient_updates = (
            not session.preparing and session.defer_production_nf_coefficients
        )

    def _touch_structure_or_state(self) -> None:
        self._v2_revision += 1

    def _build_problem(self):
        """Reuse one sparse numeric LP assembly until the engine mutates."""

        if (
            self._v2_cached_problem is not None
            and self._v2_cached_problem_revision == self._v2_revision
        ):
            return self._v2_cached_problem
        problem = super()._build_problem()
        self._v2_cached_problem = problem
        self._v2_cached_problem_revision = self._v2_revision
        return problem

    def add_variable(self, *args, **kwargs):
        value = super().add_variable(*args, **kwargs)
        self._touch_structure_or_state()
        return value

    def add_constraint(self, *args, **kwargs):
        value = super().add_constraint(*args, **kwargs)
        self._touch_structure_or_state()
        return value

    def set_coefficient(self, *args, **kwargs):
        value = super().set_coefficient(*args, **kwargs)
        self._touch_structure_or_state()
        return value

    def update_variable_bounds(self, name: str, lb: float | None = None, ub: float | None = None) -> None:
        current_lb, current_ub = self.get_variable_bounds(name)
        if lb is not None and (
            abs(lb - current_lb) > 1e-15 or name in self._last_solution_values
        ):
            self._v2_dynamic_bound_sides.add(("variable", name, "lower"))
        if ub is not None and (
            abs(ub - current_ub) > 1e-15 or name in self._last_solution_values
        ):
            self._v2_dynamic_bound_sides.add(("variable", name, "upper"))
        super().update_variable_bounds(name, lb=lb, ub=ub)
        self._touch_structure_or_state()

    def update_constraint_ub(self, name: str, ub: float | None = None) -> None:
        from math import inf, isfinite
        current = self.get_constraint_bounds(name)[1]
        incoming = inf if ub is None else ub
        if incoming != current:
            self._v2_dynamic_bound_sides.add(("constraint", name, "upper"))
        super().update_constraint_ub(name, ub=ub)
        self._touch_structure_or_state()

    def update_constraint_lb(self, name: str, lb: float | None = None) -> None:
        from math import inf, isfinite
        current = self.get_constraint_bounds(name)[0]
        incoming = -inf if lb is None else lb
        if incoming != current:
            self._v2_dynamic_bound_sides.add(("constraint", name, "lower"))
        super().update_constraint_lb(name, lb=lb)
        self._touch_structure_or_state()

    def commit_equal_priority_residual(self, apportioner, var, delta: float) -> None:
        """Commit one equal-priority member's delta to the explicit residual state."""

        if abs(delta) <= 1e-15:
            return
        state = getattr(self, "v2_residual_state", None)
        effects = getattr(self, "v2_residual_effects", {})
        if state is None:
            return
        transaction_effects = effects.get(var.id, ())
        state.apply_effects(
            self,
            transaction_effects,
            delta,
            getattr(self, "v2_residual_parameters", None),
        )
        self.v2_session.stats["execution_residual_commits"] += 1
        self.v2_session.stats["execution_residual_row_updates"] += len(
            effects.get(var.id, ())
        )

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        program = self.v2_session.resolve(
            self,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
        )
        result = program.execute(self)
        self.solve_count += 1
        if self.v2_session.preparing:
            self.v2_session.stats["preparation_objective_calls"] += 1
        else:
            self.v2_session.stats["execution_objective_calls"] += 1
        self._last_solution_values.update(result.requested_values)
        return result.objective_value, result.requested_values

    def maximize_and_update_variable(self, variable_name: str) -> float:
        """Use the directly indexed frozen scalar program when available.

        ``DayExecutionProgram`` already resolved the structural regime, so the
        normal sequential path must not rescan the entire production LP merely
        to reconstruct an objective cache signature.  A compatibility fallback
        remains for developer/tests that invoke the backend outside that IR.
        """

        direct = self.v2_session.resolve_scalar_with_parameters(
            self, variable_name
        )
        if direct is not None:
            program, parameters = direct
            execute_with_parameters = getattr(program, "execute_with_parameters", None)
            result = (
                execute_with_parameters(self, parameters)
                if execute_with_parameters is not None
                else program.execute(self)
            )
            self.solve_count += 1
            self.v2_session.stats["execution_objective_calls"] += 1
            self._last_solution_values.update(result.requested_values)
            solved_value = result.requested_values[variable_name]
            self.update_variable_bounds(variable_name, lb=solved_value)
            return solved_value

        signature = objective_signature(
            self,
            variable_names=[variable_name],
            maximization=True,
            weights=None,
        )
        if signature in self.v2_session._cache:
            _, values = self.solve_objective(
                [variable_name],
                maximization=True,
            )
        else:
            _, values = self.solve_auxiliary_objective(
                [variable_name],
                maximization=True,
            )
            self.v2_session.stats["equal_priority_scalar_auxiliary"] += 1
        solved_value = values[variable_name]
        self.update_variable_bounds(variable_name, lb=solved_value)
        return solved_value

    def solve_auxiliary_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """Solve a structurally declared auxiliary kernel numerically.

        Auxiliary objectives are intentionally outside the frozen formula
        cache because their target sets can vary inside equal-priority loops or
        deterministic tie-breaks.  This is a local LP kernel, not a whole-day
        fallback, and it never compiles or discovers a new v2 program.
        """

        self.v2_session.stats["auxiliary_kernel_solves"] += 1
        return ScipyLPSolver.solve_objective(
            self,
            variable_names,
            maximization=maximization,
            weights=weights,
        )

    def solve_auxiliary_objective_value(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> float:
        value, _ = self.solve_auxiliary_objective(
            variable_names,
            maximization=maximization,
            weights=weights,
        )
        return value

    def solve_objective_value(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> float:
        # Objective-value-only calls are used for equal-priority membership
        # classification and similar runtime loop decisions.  Keep them inside
        # the declared structural auxiliary kernel instead of generating an
        # exponential family of frozen target subsets.
        return self.solve_auxiliary_objective_value(
            variable_names,
            maximization=maximization,
            weights=weights,
        )


    def maximize_equal_priority_transactions(
        self,
        proportion_factors: dict[str, float],
    ) -> dict[str, float]:
        """Execute water filling on frozen logical transaction IR."""

        program, parameters = self.v2_session.resolve_equal_priority_with_parameters(
            self, list(proportion_factors)
        )
        self.v2_session.stats["execution_equal_priority_common_increment_solves"] += 1
        self.solve_count += 1
        return program.maximize_common_increment(
            self, proportion_factors, parameters=parameters
        )

    def solve_equal_priority_objective_value(
        self,
        transaction_ids: list[str],
    ) -> float:
        """Maximum absolute sum used to classify blocked cohort members."""

        program, parameters = self.v2_session.resolve_equal_priority_with_parameters(
            self, transaction_ids
        )
        self.v2_session.stats["execution_equal_priority_classification_solves"] += 1
        self.solve_count += 1
        return program.maximize_member_sum(
            self, transaction_ids, parameters=parameters
        )

    def equal_priority_column_signature(self, transaction_id: str) -> tuple:
        return self.v2_session.equal_priority_column_signature(self, transaction_id)

    def maximize_equal_priority_transaction(self, transaction_id: str) -> float:
        """Maximize a deferred tiny-factor member on its logical residual column."""

        program, parameters = self.v2_session.resolve_equal_priority_with_parameters(
            self, [transaction_id]
        )
        self.v2_session.stats["execution_equal_priority_scalar_solves"] += 1
        self.solve_count += 1
        return program.maximize_single_member(
            self, transaction_id, parameters=parameters
        )

    def maximize_group_by_proportions(
        self,
        variable_names: list[str],
        proportion_factors: dict[str, float],
    ) -> dict[str, float]:
        """Runtime water-filling kernel for one structural equal-priority loop."""

        merge_variable_name = "combined"
        if merge_variable_name not in self.vars:
            self.add_variable(merge_variable_name, lb=0)
        self.vars[merge_variable_name].SetBounds(0, inf)
        self._touch_structure_or_state()

        merge_constraint_names: list[str] = []
        for variable_name in variable_names:
            factor = proportion_factors[variable_name]
            constraint_name = "combined_" + variable_name
            if constraint_name not in self.cons:
                self.add_constraint(constraint_name)
            constraint = self.cons[constraint_name]
            constraint.Clear()
            self._constraint_vars[constraint_name] = []
            constraint.SetBounds(self.vars[variable_name].lb(), inf)
            self.set_coefficient(constraint_name, variable_name, 1.0)
            self.set_coefficient(constraint_name, merge_variable_name, -factor)
            merge_constraint_names.append(constraint_name)
        self._touch_structure_or_state()

        _, solved_values = self.solve_auxiliary_objective(
            [merge_variable_name],
            maximization=True,
        )
        increment = solved_values[merge_variable_name]
        initial_values = {
            name: self.vars[name].lb() for name in variable_names
        }

        # Remove the temporary structure completely so subsequent frozen scalar
        # objective signatures remain identical to the statically prepared LP.
        for constraint_name in merge_constraint_names:
            self.cons.pop(constraint_name, None)
            self._constraint_vars.pop(constraint_name, None)
        self.vars.pop(merge_variable_name, None)
        self._last_solution_values.pop(merge_variable_name, None)
        self._touch_structure_or_state()

        return {
            name: initial_values[name] + proportion_factors[name] * increment
            for name in variable_names
        }

    def get_last_variable_reduced_cost(self, variable_name: str) -> float | None:
        return None

    def get_last_solve_constraint_evidence(self, variable_name: str, tolerance: float = 1e-6) -> list[dict]:
        return []


def v2_factory(session: V2CompilationSession) -> Callable[..., V2LPSolver]:
    def factory(*, tolerance=None, **_kwargs):
        return V2LPSolver(session=session, tolerance=tolerance)

    return factory
