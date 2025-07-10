from dataclasses import dataclass
from math import isclose

class Variable:
    def __init__(self, id, type='', lb=None, ub=None, 
                 from_zone=None, to_zone=None, 
                 from_change=None, to_change=None,
                 forward_subarcs=[], backward_subarcs=[]):
        
        self.id = id
        self.type = type
        self.lb = lb
        self.ub = ub

        # These will eventually need to be moved to self.segments 
        self.from_zone = from_zone
        self.to_zone = to_zone

        #
        self.from_change = from_change
        self.to_change = to_change

        self.forward_subarcs = forward_subarcs
        self.backward_subarcs = backward_subarcs

        # 
        self.segments = []

        # For solver output.
        self.value = 0.0
        self.details = []

        #
        self.from_account = None
        self.to_account = None


    def set_value(self, new_value, step, pre_limits, post_limits):
        """
        """

        # 
        d = {
            "step": step,
            "value": (self.value, new_value),
            "limited_by": []
        }

        for key in post_limits:
            d[key] = (pre_limits[key], post_limits[key])

            if isclose(post_limits[key], 0):
                d['limited_by'].append(key)

        self.details.append(d)
        self.value = new_value

        return d


    def as_transaction(self, date):

        from_account_name = None
        if self.from_zone is not None:
            from_account_name = self.from_zone.name

        to_account_name = None 
        if self.to_zone is not None:
            to_account_name = self.to_zone.name

        return Transaction(variable=self.id, 
                           from_account=from_account_name, 
                           to_account=to_account_name, 
                           date=date, value=self.value, 
                           memo='')


@dataclass
class Transaction:
    """Class for representing a transaction - a named flow from one account to another."""
    variable: str
    from_account: str
    to_account: str
    date: str
    value: float
    memo: str
