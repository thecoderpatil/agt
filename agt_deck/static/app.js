/* AGT Command Deck — HTMX init + SSE */
document.addEventListener("DOMContentLoaded", function() {
    // SSE connection for live updates
    const token = new URLSearchParams(window.location.search).get("t") || "";
    const evtSource = new EventSource("/sse?t=" + encodeURIComponent(token));

    const banner = document.getElementById("sse-banner");

    evtSource.addEventListener("topstrip", function(e) {
        try {
            const d = JSON.parse(e.data);
            const el = (id, val) => {
                const node = document.getElementById(id);
                if (node && val !== undefined) node.textContent = val;
            };
            el("nav-value", d.total_nav);
            el("pnl-value", d.day_pnl);
            el("pnl-pct", d.day_pnl_pct);
            el("vix-value", d.vix);
            el("sync-ago", d.last_sync);
        } catch (err) {
            console.warn("SSE parse error:", err);
        }
    });

    evtSource.onerror = function() {
        if (banner) {
            banner.classList.remove("hidden");
            banner.textContent = "Live updates disconnected — reconnecting…";
        }
    };

    evtSource.onopen = function() {
        if (banner) banner.classList.add("hidden");
    };
});
