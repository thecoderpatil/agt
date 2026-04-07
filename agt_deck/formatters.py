"""Display formatters for the Command Deck."""
from __future__ import annotations


def money(val, decimals: int = 0, plus: bool = False) -> str:
    """Format a dollar amount."""
    if val is None:
        return "—"
    prefix = "+" if plus and val > 0 else ""
    if abs(val) >= 1_000_000:
        return f"{prefix}${val:,.0f}"
    elif abs(val) >= 1000:
        return f"{prefix}${val:,.{decimals}f}"
    else:
        return f"{prefix}${val:,.2f}"


def pct(val, decimals: int = 1, plus: bool = False) -> str:
    """Format a percentage."""
    if val is None:
        return "—"
    prefix = "+" if plus and val > 0 else ""
    return f"{prefix}{val:.{decimals}f}%"


def color_class(val, threshold_green=0, threshold_red=0) -> str:
    """Return Tailwind text color class based on value."""
    if val is None:
        return "text-slate-500"
    if val > threshold_green:
        return "text-emerald-400"
    elif val < threshold_red:
        return "text-rose-400"
    return "text-slate-300"


def pnl_color(val) -> str:
    """Green for positive, red for negative P&L."""
    return color_class(val)


def el_color(pct_val, required_pct) -> str:
    """EL gauge color: red if below required, green if above."""
    if pct_val is None or required_pct is None:
        return "text-slate-500"
    return "text-emerald-400" if pct_val >= required_pct else "text-rose-400"


def concentration_color(pct_val) -> str:
    """Rule 1: red >20%, amber 18-20%, green otherwise."""
    if pct_val is None:
        return "text-slate-500"
    if pct_val > 20:
        return "text-rose-400"
    if pct_val >= 18:
        return "text-amber-400"
    return "text-emerald-400"


def time_ago(iso_str: str | None) -> str:
    """Human-readable time since ISO timestamp."""
    if not iso_str:
        return "never"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.total_seconds() < 60:
            return "just now"
        elif delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() / 60)}m ago"
        elif delta.total_seconds() < 86400:
            return f"{int(delta.total_seconds() / 3600)}h ago"
        else:
            return f"{delta.days}d ago"
    except Exception:
        return iso_str[:16] if iso_str else "—"
