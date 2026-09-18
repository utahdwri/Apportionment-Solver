from math import isfinite, isinf, isnan
from ut_water_apportionment.compile.kernel import BlockLPError, TOL
from ut_water_apportionment.compile.formula import FormulaEvaluationError, FormulaGuardFailed

# Runtime state layout.  Formula code below uses these names instead of raw indexes.
S_FLOW_COEFFICIENT_TRXN_1_RIVER_USER = 0  # flow_coefficient[('TRXN_1', 'RIVER>USER')]
S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER = 1  # negative_flow_coefficient[('TRXN_1', 'RIVER>USER')]
S_ALLOCATED_TRXN_1 = 2  # allocated['TRXN_1']
S_REMAINING_LIMIT_TRXN_1 = 3  # remaining_limit['TRXN_1']
S_REFERENCE_CFS_TRXN_1 = 4  # reference_cfs['TRXN_1']
S_ALLOCATED_TRXN_2 = 5  # allocated['TRXN_2']
S_REMAINING_LIMIT_TRXN_2 = 6  # remaining_limit['TRXN_2']
S_REFERENCE_CFS_TRXN_2 = 7  # reference_cfs['TRXN_2']
S_REMAINING_MEASURED_RIVER_USER = 8  # remaining_measured['RIVER>USER']
S_REMAINING_MEASURED_AVAILABLE_RIVER_USER = 9  # remaining_measured_available['RIVER>USER']
S_REMAINING_MEASURED_FORWARD_RIVER_USER = 10  # remaining_measured_forward['RIVER>USER']
S_REMAINING_MEASURED_SYS_RIVER = 11  # remaining_measured['SYS>RIVER']
S_REMAINING_MEASURED_AVAILABLE_SYS_RIVER = 12  # remaining_measured_available['SYS>RIVER']
S_REMAINING_MEASURED_FORWARD_SYS_RIVER = 13  # remaining_measured_forward['SYS>RIVER']
S_REMAINING_MEASURED_REVERSE_SYS_RIVER = 14  # remaining_measured_reverse['SYS>RIVER']
S_REMAINING_NF_RIVER = 15  # remaining_nf['RIVER']
S_NF_COEFFICIENT_RIVER_RIVER = 16  # nf_coefficient[('RIVER', 'RIVER')]
S_NEGATIVE_NF_COEFFICIENT_RIVER_RIVER = 17  # negative_nf_coefficient[('RIVER', 'RIVER')]

# ============================================================================
# PASS 1 block 0: ['TRXN_1'] (DirectCalculationKernel)
# ============================================================================
def _pass1_block_0(state):
    # Direct formula for TRXN_1
    _lower = 0.0
    _upper = state[S_REMAINING_LIMIT_TRXN_1]
    if _upper != float('inf') and -TOL <= _upper < 0 and _lower == 0:
        _upper = 0.0
    if not isfinite(_lower) or isnan(_upper):
        raise BlockLPError('Non-finite variable bound')

    # measurement_forward['RIVER>USER']
    _c0 = state[S_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]
    _hi0 = state[S_REMAINING_MEASURED_FORWARD_RIVER_USER]
    if not isfinite(_c0):
        raise BlockLPError("Non-finite coefficient: measurement_forward['RIVER>USER']")
    if not isfinite(_hi0):
        raise BlockLPError("Non-finite constraint bound: measurement_forward['RIVER>USER']")
    if abs(_c0) <= 1e-15:
        if _hi0 < -TOL:
            raise BlockLPError("Block ['TRXN_1'] failed: infeasible measurement_forward['RIVER>USER']")
    elif _c0 > 0:
        _upper = min(_upper, _hi0 / _c0)
    else:
        _lower = max(_lower, _hi0 / _c0)

    # natural_flow['RIVER']
    _c1 = state[S_NF_COEFFICIENT_RIVER_RIVER]
    _hi1 = state[S_REMAINING_NF_RIVER]
    if not isfinite(_c1):
        raise BlockLPError("Non-finite coefficient: natural_flow['RIVER']")
    if not isfinite(_hi1):
        raise BlockLPError("Non-finite constraint bound: natural_flow['RIVER']")
    if abs(_c1) <= 1e-15:
        if _hi1 < -TOL:
            raise BlockLPError("Block ['TRXN_1'] failed: infeasible natural_flow['RIVER']")
    elif _c1 > 0:
        _upper = min(_upper, _hi1 / _c1)
    else:
        _lower = max(_lower, _hi1 / _c1)

    _scale = max(1.0, abs(_lower) if isfinite(_lower) else 1.0, abs(_upper) if isfinite(_upper) else 1.0)
    if _upper < _lower - TOL * _scale:
        raise BlockLPError("Block ['TRXN_1'] failed: direct interval is infeasible")
    if _upper < _lower:
        _lower = _upper = 0.5 * (_lower + _upper)
    _objective = 1.0
    if not isfinite(_objective) or abs(_objective) <= 1e-15:
        raise BlockLPError('Invalid direct objective coefficient')
    TRXN_1 = _upper if _objective > 0 else _lower
    if not isfinite(TRXN_1):
        raise BlockLPError("Block ['TRXN_1'] failed: unbounded direct objective")
    _change_2 = (1.0) * (TRXN_1)
    _change_3 = (-1.0) * (TRXN_1)
    _change_8 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_9 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_10 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_15 = (state[S_NEGATIVE_NF_COEFFICIENT_RIVER_RIVER]) * (TRXN_1)
    state[S_ALLOCATED_TRXN_1] += _change_2  # allocated['TRXN_1']
    state[S_REMAINING_LIMIT_TRXN_1] += _change_3  # remaining_limit['TRXN_1']
    state[S_REMAINING_MEASURED_RIVER_USER] += _change_8  # remaining_measured['RIVER>USER']
    state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER] += _change_9  # remaining_measured_available['RIVER>USER']
    state[S_REMAINING_MEASURED_FORWARD_RIVER_USER] += _change_10  # remaining_measured_forward['RIVER>USER']
    state[S_REMAINING_NF_RIVER] += _change_15  # remaining_nf['RIVER']
    return 0

# ============================================================================
# PASS 1 block 1: ['TRXN_2'] (DirectCalculationKernel)
# ============================================================================
def _pass1_block_1(state):
    # Direct formula for TRXN_2
    _lower = 0.0
    _upper = state[S_REMAINING_LIMIT_TRXN_2]
    if _upper != float('inf') and -TOL <= _upper < 0 and _lower == 0:
        _upper = 0.0
    if not isfinite(_lower) or isnan(_upper):
        raise BlockLPError('Non-finite variable bound')

    # measurement_forward['RIVER>USER']
    _c0 = state[S_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]
    _hi0 = state[S_REMAINING_MEASURED_FORWARD_RIVER_USER]
    if not isfinite(_c0):
        raise BlockLPError("Non-finite coefficient: measurement_forward['RIVER>USER']")
    if not isfinite(_hi0):
        raise BlockLPError("Non-finite constraint bound: measurement_forward['RIVER>USER']")
    if abs(_c0) <= 1e-15:
        if _hi0 < -TOL:
            raise BlockLPError("Block ['TRXN_2'] failed: infeasible measurement_forward['RIVER>USER']")
    elif _c0 > 0:
        _upper = min(_upper, _hi0 / _c0)
    else:
        _lower = max(_lower, _hi0 / _c0)

    # natural_flow['RIVER']
    _c1 = state[S_NF_COEFFICIENT_RIVER_RIVER]
    _hi1 = state[S_REMAINING_NF_RIVER]
    if not isfinite(_c1):
        raise BlockLPError("Non-finite coefficient: natural_flow['RIVER']")
    if not isfinite(_hi1):
        raise BlockLPError("Non-finite constraint bound: natural_flow['RIVER']")
    if abs(_c1) <= 1e-15:
        if _hi1 < -TOL:
            raise BlockLPError("Block ['TRXN_2'] failed: infeasible natural_flow['RIVER']")
    elif _c1 > 0:
        _upper = min(_upper, _hi1 / _c1)
    else:
        _lower = max(_lower, _hi1 / _c1)

    _scale = max(1.0, abs(_lower) if isfinite(_lower) else 1.0, abs(_upper) if isfinite(_upper) else 1.0)
    if _upper < _lower - TOL * _scale:
        raise BlockLPError("Block ['TRXN_2'] failed: direct interval is infeasible")
    if _upper < _lower:
        _lower = _upper = 0.5 * (_lower + _upper)
    _objective = 1.0
    if not isfinite(_objective) or abs(_objective) <= 1e-15:
        raise BlockLPError('Invalid direct objective coefficient')
    TRXN_2 = _upper if _objective > 0 else _lower
    if not isfinite(TRXN_2):
        raise BlockLPError("Block ['TRXN_2'] failed: unbounded direct objective")
    _change_5 = (1.0) * (TRXN_2)
    _change_6 = (-1.0) * (TRXN_2)
    _change_8 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_9 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_10 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_15 = (state[S_NEGATIVE_NF_COEFFICIENT_RIVER_RIVER]) * (TRXN_2)
    state[S_ALLOCATED_TRXN_2] += _change_5  # allocated['TRXN_2']
    state[S_REMAINING_LIMIT_TRXN_2] += _change_6  # remaining_limit['TRXN_2']
    state[S_REMAINING_MEASURED_RIVER_USER] += _change_8  # remaining_measured['RIVER>USER']
    state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER] += _change_9  # remaining_measured_available['RIVER>USER']
    state[S_REMAINING_MEASURED_FORWARD_RIVER_USER] += _change_10  # remaining_measured_forward['RIVER>USER']
    state[S_REMAINING_NF_RIVER] += _change_15  # remaining_nf['RIVER']
    return 0

# ============================================================================
# REPLAY block 0: ['TRXN_1'] (DirectCalculationKernel)
# ============================================================================
def _replay_block_0(state):
    # Direct formula for TRXN_1
    _lower = 0.0
    _upper = state[S_REMAINING_LIMIT_TRXN_1]
    if _upper != float('inf') and -TOL <= _upper < 0 and _lower == 0:
        _upper = 0.0
    if not isfinite(_lower) or isnan(_upper):
        raise BlockLPError('Non-finite variable bound')

    # measurement_net['RIVER>USER']
    _c0 = state[S_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]
    _hi0 = state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER]
    if not isfinite(_c0):
        raise BlockLPError("Non-finite coefficient: measurement_net['RIVER>USER']")
    if not isfinite(_hi0):
        raise BlockLPError("Non-finite constraint bound: measurement_net['RIVER>USER']")
    if abs(_c0) <= 1e-15:
        if _hi0 < -TOL:
            raise BlockLPError("Block ['TRXN_1'] failed: infeasible measurement_net['RIVER>USER']")
    elif _c0 > 0:
        _upper = min(_upper, _hi0 / _c0)
    else:
        _lower = max(_lower, _hi0 / _c0)

    # natural_flow['RIVER']
    _c1 = state[S_NF_COEFFICIENT_RIVER_RIVER]
    _hi1 = state[S_REMAINING_NF_RIVER]
    if not isfinite(_c1):
        raise BlockLPError("Non-finite coefficient: natural_flow['RIVER']")
    if not isfinite(_hi1):
        raise BlockLPError("Non-finite constraint bound: natural_flow['RIVER']")
    if abs(_c1) <= 1e-15:
        if _hi1 < -TOL:
            raise BlockLPError("Block ['TRXN_1'] failed: infeasible natural_flow['RIVER']")
    elif _c1 > 0:
        _upper = min(_upper, _hi1 / _c1)
    else:
        _lower = max(_lower, _hi1 / _c1)

    _scale = max(1.0, abs(_lower) if isfinite(_lower) else 1.0, abs(_upper) if isfinite(_upper) else 1.0)
    if _upper < _lower - TOL * _scale:
        raise BlockLPError("Block ['TRXN_1'] failed: direct interval is infeasible")
    if _upper < _lower:
        _lower = _upper = 0.5 * (_lower + _upper)
    _objective = 1.0
    if not isfinite(_objective) or abs(_objective) <= 1e-15:
        raise BlockLPError('Invalid direct objective coefficient')
    TRXN_1 = _upper if _objective > 0 else _lower
    if not isfinite(TRXN_1):
        raise BlockLPError("Block ['TRXN_1'] failed: unbounded direct objective")
    _change_2 = (1.0) * (TRXN_1)
    _change_3 = (-1.0) * (TRXN_1)
    _change_8 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_9 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_10 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_1)
    _change_15 = (state[S_NEGATIVE_NF_COEFFICIENT_RIVER_RIVER]) * (TRXN_1)
    state[S_ALLOCATED_TRXN_1] += _change_2  # allocated['TRXN_1']
    state[S_REMAINING_LIMIT_TRXN_1] += _change_3  # remaining_limit['TRXN_1']
    state[S_REMAINING_MEASURED_RIVER_USER] += _change_8  # remaining_measured['RIVER>USER']
    state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER] += _change_9  # remaining_measured_available['RIVER>USER']
    state[S_REMAINING_MEASURED_FORWARD_RIVER_USER] += _change_10  # remaining_measured_forward['RIVER>USER']
    state[S_REMAINING_NF_RIVER] += _change_15  # remaining_nf['RIVER']
    return 0

# ============================================================================
# REPLAY block 1: ['TRXN_2'] (DirectCalculationKernel)
# ============================================================================
def _replay_block_1(state):
    # Direct formula for TRXN_2
    _lower = 0.0
    _upper = state[S_REMAINING_LIMIT_TRXN_2]
    if _upper != float('inf') and -TOL <= _upper < 0 and _lower == 0:
        _upper = 0.0
    if not isfinite(_lower) or isnan(_upper):
        raise BlockLPError('Non-finite variable bound')

    # measurement_net['RIVER>USER']
    _c0 = state[S_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]
    _hi0 = state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER]
    if not isfinite(_c0):
        raise BlockLPError("Non-finite coefficient: measurement_net['RIVER>USER']")
    if not isfinite(_hi0):
        raise BlockLPError("Non-finite constraint bound: measurement_net['RIVER>USER']")
    if abs(_c0) <= 1e-15:
        if _hi0 < -TOL:
            raise BlockLPError("Block ['TRXN_2'] failed: infeasible measurement_net['RIVER>USER']")
    elif _c0 > 0:
        _upper = min(_upper, _hi0 / _c0)
    else:
        _lower = max(_lower, _hi0 / _c0)

    # natural_flow['RIVER']
    _c1 = state[S_NF_COEFFICIENT_RIVER_RIVER]
    _hi1 = state[S_REMAINING_NF_RIVER]
    if not isfinite(_c1):
        raise BlockLPError("Non-finite coefficient: natural_flow['RIVER']")
    if not isfinite(_hi1):
        raise BlockLPError("Non-finite constraint bound: natural_flow['RIVER']")
    if abs(_c1) <= 1e-15:
        if _hi1 < -TOL:
            raise BlockLPError("Block ['TRXN_2'] failed: infeasible natural_flow['RIVER']")
    elif _c1 > 0:
        _upper = min(_upper, _hi1 / _c1)
    else:
        _lower = max(_lower, _hi1 / _c1)

    _scale = max(1.0, abs(_lower) if isfinite(_lower) else 1.0, abs(_upper) if isfinite(_upper) else 1.0)
    if _upper < _lower - TOL * _scale:
        raise BlockLPError("Block ['TRXN_2'] failed: direct interval is infeasible")
    if _upper < _lower:
        _lower = _upper = 0.5 * (_lower + _upper)
    _objective = 1.0
    if not isfinite(_objective) or abs(_objective) <= 1e-15:
        raise BlockLPError('Invalid direct objective coefficient')
    TRXN_2 = _upper if _objective > 0 else _lower
    if not isfinite(TRXN_2):
        raise BlockLPError("Block ['TRXN_2'] failed: unbounded direct objective")
    _change_5 = (1.0) * (TRXN_2)
    _change_6 = (-1.0) * (TRXN_2)
    _change_8 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_9 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_10 = (state[S_NEGATIVE_FLOW_COEFFICIENT_TRXN_1_RIVER_USER]) * (TRXN_2)
    _change_15 = (state[S_NEGATIVE_NF_COEFFICIENT_RIVER_RIVER]) * (TRXN_2)
    state[S_ALLOCATED_TRXN_2] += _change_5  # allocated['TRXN_2']
    state[S_REMAINING_LIMIT_TRXN_2] += _change_6  # remaining_limit['TRXN_2']
    state[S_REMAINING_MEASURED_RIVER_USER] += _change_8  # remaining_measured['RIVER>USER']
    state[S_REMAINING_MEASURED_AVAILABLE_RIVER_USER] += _change_9  # remaining_measured_available['RIVER>USER']
    state[S_REMAINING_MEASURED_FORWARD_RIVER_USER] += _change_10  # remaining_measured_forward['RIVER>USER']
    state[S_REMAINING_NF_RIVER] += _change_15  # remaining_nf['RIVER']
    return 0

def execute_day(state):
    lp_solves = 0
    lp_solves += _pass1_block_0(state)
    lp_solves += _pass1_block_1(state)
    return lp_solves

def execute_replay(state):
    lp_solves = 0
    lp_solves += _replay_block_0(state)
    lp_solves += _replay_block_1(state)
    return lp_solves