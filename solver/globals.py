class Globals:
    
    # If a path has no cfs_limit, this large value will be used as its limit. Then before the apportionments are 
    # sent back as results, any instances of this large value will be replaced with text to convey the idea 
    # that the upper-bound is undetermined.
    DEFAULT_PATH_UB = 1e10 # 

    LOG_MODEL_DEFS = True