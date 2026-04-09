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


def format_age(seconds) -> str:
    """Human-readable relative age from seconds."""
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m ago"
    else:
        return f"{seconds // 86400}d ago"


def el_pct_color(pct_val) -> str:
    """EL percentage → Tailwind color class for Health Strip."""
    if pct_val is None:
        return "text-slate-500"
    if pct_val >= 40:
        return "text-emerald-400"
    if pct_val >= 25:
        return "text-amber-400"
    if pct_val >= 15:
        return "text-orange-400"
    return "text-rose-500 font-bold animate-pulse"


def lifecycle_state_classes(status: str, is_orphan: bool = False) -> str:
    """Lifecycle row → Tailwind classes for Action Queue."""
    if is_orphan:
        return "border-l-4 border-rose-600 bg-rose-900/40 font-bold"
    return {
        "STAGED": "border-l-4 border-amber-500 bg-amber-950/20",
        "ATTESTED": "border-l-4 border-blue-500 bg-blue-950/20",
        "TRANSMITTING": "border-l-4 border-purple-500 bg-purple-950/30 animate-pulse",
        "TRANSMITTED": "border-l-4 border-emerald-600 bg-emerald-950/20 opacity-60",
    }.get(status, "")
