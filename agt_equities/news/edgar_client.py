"""SEC EDGAR 8-K client.

Per ADR-CSP_NEWS_OVERLAY_v1 section "Adapters shipped in this MR" item 3
+ followup item.

SEC API uses padded CIK (10 chars, leading zeros). We resolve ticker→CIK
on demand and cache in process memory; persistent on-disk cache (pickle)
is a follow-up.

Filter to 8-K filings with ANY of items {1.02, 2.04, 4.01, 4.02, 5.02}.
These are the high-signal items per the ADR; other 8-Ks are noise.

User-Agent header REQUIRED by SEC policy. Failure to set it = blocked.
SEC tolerates ~10 req/sec; we run 1-2/sec at most.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SEC_USER_AGENT = "AGT Equities RIA contact@agtequities.com"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik_padded}.json"

# 8-K items that move a name. Per ADR.
HIGH_SIGNAL_8K_ITEMS = frozenset({"1.02", "2.04", "4.01", "4.02", "5.02"})


class EdgarClient:
    """SEC EDGAR async client. CIK lookup cached in-process.

    Construction is cheap. The first call triggers a one-time fetch of
    the ticker→CIK mapping from SEC; subsequent calls hit the in-memory
    cache.
    """

    def __init__(
        self,
        *,
        user_agent: str = SEC_USER_AGENT,
        timeout_s: float = 8.0,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = httpx.Timeout(connect=3.0, read=timeout_s, write=3.0, pool=3.0)
        self._cik_map: dict[str, str] | None = None
        self._cik_lock = asyncio.Lock()

    async def aclose(self) -> None:
        # Stateless beyond cik_map; nothing to close. Method present for
        # symmetry with other async clients.
        return None

    async def _ensure_cik_map(self) -> dict[str, str]:
        if self._cik_map is not None:
            return self._cik_map
        async with self._cik_lock:
            if self._cik_map is not None:  # double-check after acquiring
                return self._cik_map
            mapping = await self._fetch_cik_map()
            self._cik_map = mapping
            return mapping

    async def _fetch_cik_map(self) -> dict[str, str]:
        """Fetch SEC company_tickers.json and build {ticker: cik_padded}."""
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            ) as client:
                resp = await client.get(SEC_TICKERS_URL)
                if resp.status_code != 200:
                    logger.warning(
                        "edgar.cik_map_fetch_failed status=%d", resp.status_code
                    )
                    return {}
                payload = resp.json()
        except Exception as exc:
            logger.warning("edgar.cik_map_fetch_err err=%s", exc)
            return {}
        # SEC payload is dict-of-dict keyed by row index, with each row
        # having keys: cik_str, ticker, title.
        out: dict[str, str] = {}
        if not isinstance(payload, dict):
            return out
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            cik = row.get("cik_str")
            if not ticker or cik is None:
                continue
            try:
                cik_padded = str(int(cik)).zfill(10)
            except (TypeError, ValueError):
                continue
            out[str(ticker).upper()] = cik_padded
        logger.info("edgar.cik_map_loaded n=%d", len(out))
        return out

    async def fetch_8k_filings(
        self,
        ticker: str,
        *,
        lookback_hours: int = 72,
    ) -> list[dict[str, Any]]:
        """Return high-signal 8-K filings for ticker within lookback window.

        Each returned dict has keys:
          accession_number, form, filed_date (date), items (list[str]),
          primary_document_url

        Empty list = no high-signal 8-Ks in window (valid; not a failure).
        Returns [] on any error (fail-soft).
        """
        cik_map = await self._ensure_cik_map()
        cik_padded = cik_map.get(ticker.upper())
        if cik_padded is None:
            logger.info("edgar.cik_unknown ticker=%s", ticker)
            return []

        url = SEC_SUBMISSIONS_URL.format(cik_padded=cik_padded)
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning(
                        "edgar.submissions_fetch_failed ticker=%s status=%d",
                        ticker, resp.status_code,
                    )
                    return []
                payload = resp.json()
        except Exception as exc:
            logger.warning("edgar.submissions_fetch_err ticker=%s err=%s", ticker, exc)
            return []

        recent = self._extract_recent_filings(payload)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).date()
        out: list[dict[str, Any]] = []
        for row in recent:
            if row["form"] != "8-K":
                continue
            if row["filed_date"] < cutoff:
                continue
            high_signal_items = [it for it in row["items"] if it in HIGH_SIGNAL_8K_ITEMS]
            if not high_signal_items:
                continue
            out.append({
                "accession_number": row["accession_number"],
                "form": row["form"],
                "filed_date": row["filed_date"],
                "items": high_signal_items,
                "primary_document_url": row["primary_document_url"],
                "ticker": ticker.upper(),
                "cik": cik_padded,
            })
        return out

    @staticmethod
    def _extract_recent_filings(payload: dict) -> list[dict[str, Any]]:
        """Pull the parallel-arrays 'recent' filings block out of submissions JSON."""
        try:
            recent = payload["filings"]["recent"]
        except (KeyError, TypeError):
            return []
        accession = recent.get("accessionNumber") or []
        form = recent.get("form") or []
        filing_date = recent.get("filingDate") or []
        items = recent.get("items") or []
        primary_doc = recent.get("primaryDocument") or []
        out: list[dict[str, Any]] = []
        for i in range(min(len(accession), len(form), len(filing_date))):
            try:
                d = date.fromisoformat(filing_date[i])
            except (ValueError, TypeError):
                continue
            raw_items = items[i] if i < len(items) else ""
            item_list = [s.strip() for s in str(raw_items).split(",") if s.strip()]
            doc = primary_doc[i] if i < len(primary_doc) else ""
            acc = accession[i]
            cik_part = acc.replace("-", "") if isinstance(acc, str) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/0/{cik_part}/{doc}"
                if cik_part and doc else ""
            )
            out.append({
                "accession_number": acc,
                "form": form[i],
                "filed_date": d,
                "items": item_list,
                "primary_document_url": doc_url,
            })
        return out
