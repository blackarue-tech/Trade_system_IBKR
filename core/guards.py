def _is_farming(last_price: float, ctx: dict) -> bool:
    """
    Expect caller to supply context fields like:
    ctx['trend_ma_fast'] > ctx['trend_ma_slow'], ctx['above_vwap'] True/False, etc.
    For now, simple proxy: above VWAP and MA_fast > MA_slow.
    """
    if not ctx.get("live_params", {}).get("guards", {}).get("require_farming", True):
        return True
    return bool(ctx.get("above_vwap", True) and ctx.get("ma_fast_gt_ma_slow", True))

def _is_fading(last_price: float, ctx: dict) -> bool:
    """
    Simple proxy: price below VWAP and momentum negative.
    """
    avoid = ctx.get("live_params", {}).get("guards", {}).get("avoid_fading", True)
    if not avoid:
        return False
    return bool((not ctx.get("above_vwap", True)) and ctx.get("neg_momentum", False))

def _spike_guard(last_price: float, ctx: dict) -> bool:
    """
    Reject if last move spike > spike_guard_pct over short window.
    Caller provides ctx['recent_pct_change'] over ~1-3m.
    """
    lim = float(ctx.get("live_params", {}).get("guards", {}).get("spike_guard_pct", 1.0))
    recent = float(ctx.get("recent_pct_change", 0.0))
    return abs(recent) <= lim

def _duplicate_guard(symbol: str, ctx: dict) -> bool:
    if not ctx.get("live_params", {}).get("guards", {}).get("block_duplicate_symbol", True):
        return True
    open_syms = set(ctx.get("open_symbols", []))
    return symbol not in open_syms

def should_buy_symbol(symbol: str, last_price: float, ctx: dict) -> bool:
    if not _duplicate_guard(symbol, ctx):
        return False
    if not _spike_guard(last_price, ctx):
        return False
    if _is_fading(last_price, ctx):
        return False
    if not _is_farming(last_price, ctx):
        return False
    # Confidence threshold
    min_c = float(ctx.get("live_params", {}).get("min_confidence", 0.55))
    return float(ctx.get("confidence", 0.0)) >= min_c
