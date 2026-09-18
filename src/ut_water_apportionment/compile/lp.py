from dataclasses import dataclass, field
from typing import TypeAlias


@dataclass(frozen=True)
class Slot:
    """A position in the runtime state array.

    ``sign`` is structural metadata used only when a slot is an LP coefficient:
    ``+1`` means the runtime value is nonnegative, ``-1`` means nonpositive,
    and ``0`` means no sign guarantee is available.  Bounds/state slots do not
    need sign metadata.
    """
    index: int
    name: str  # For readable generated code and diagnostics.
    sign: int = 0
    source_index: int | None = None
    source_factor: float = 1.0
    constant_value: float | None = None


Scalar: TypeAlias = float | Slot


@dataclass(frozen=True)
class Variable:
    """An additional allocation, not the total already allocated."""
    lower: Scalar = 0.0
    upper: Scalar | None = None  # None means unbounded.


@dataclass(frozen=True)
class Constraint:
    """
    lower <= sum(coefficients[name] * variable[name]) <= upper

    None means that side is unbounded.
    """
    name: str
    coefficients: dict[str, Scalar]
    lower: Scalar | None = None
    upper: Scalar | None = None


@dataclass(frozen=True)
class Maximize:
    """An ordinary linear maximization objective."""
    coefficients: dict[str, Scalar]


@dataclass(frozen=True)
class Proportional:
    """
    Repeated proportional allocation with blocked-member removal.

    Values are reference cfs used to calculate the active proportions.
    They are not weights for a maximum-sum objective.
    """
    reference_cfs: dict[str, Scalar]


AllocationRule: TypeAlias = Maximize | Proportional


@dataclass
class BlockLP:
    variables: dict[str, Variable]
    constraints: list[Constraint]
    rule: AllocationRule

    # Updates when an allocation increment is committed:
    #
    # state[slot] += coefficient * increment[variable_name]
    #
    # Positive coefficients add; negative coefficients subtract.
    updates: dict[str, dict[Slot, Scalar]] = field(default_factory=dict)