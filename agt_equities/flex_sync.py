"""
agt_equities.flex_sync — IBKR Flex Web Service client and master_log mirror writer.

This module is the ONLY writer to master_log_* tables. No other module may
write to Bucket 2 tables. See REFACTOR_SPEC_v3.md section 6.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Sprint A / A3: single atomic transaction for run_sync.
from agt_equities.db import tx_immediate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FLEX_TOKEN = os.environ.get("AGT_FLEX_TOKEN", os.environ.get("FLEX_TOKEN", ""))
FLEX_QUERY_ID = "1461095"
FLEX_ENDPOINT_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_POLL_DELAY_SECONDS = 25
FLEX_MAX_POLL_RETRIES = 6
FLEX_INCEPTION_FROM_DATE = "20250901"

_BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("AGT_DB_PATH", str(_BASE_DIR / "agt_desk.db")))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    # FU-A / A3: 15s busy_timeout to survive scheduler/bot contention under
    # the new single-atomic-transaction window.
    conn.execute("PRAGMA busy_timeout = 15000;")
    return conn


# ---------------------------------------------------------------------------
# Sync modes and result
# ---------------------------------------------------------------------------

class SyncMode(Enum):
    INCEPTION = 'inception'
    INCREMENTAL = 'incremental'
    ONESHOT = 'oneshot'


@dataclass
class SyncResult:
    sync_id: int
    status: str  # 'success' | 'error' | 'suspicious' (ADR-018 Phase 1)
    sections_processed: int = 0
    rows_received: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    anomalies: list = field(default_factory=list)
    error_message: Optional[str] = None
    # ADR-018 Phase 1: zero-row on known trading day escalation.
    needs_retry: bool = False
    retry_date: Optional[str] = None
    next_attempt_n: int = 0


# ---------------------------------------------------------------------------
# Section field mappings: {xml_attrib_name: db_column_name}
# ---------------------------------------------------------------------------

_ACCOUNT_INFO_FIELDS = {
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'model': 'model',
}

_TRADE_FIELDS = {
    'transactionID': 'transaction_id',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'model': 'model',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'conid': 'conid',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'tradeID': 'trade_id',
    'ibOrderID': 'ib_order_id',
    'ibExecID': 'ib_exec_id',
    'relatedTransactionID': 'related_transaction_id',
    'origTradeID': 'orig_trade_id',
    'dateTime': 'date_time',
    'tradeDate': 'trade_date',
    'reportDate': 'report_date',
    'orderTime': 'order_time',
    'openDateTime': 'open_date_time',
    'transactionType': 'transaction_type',
    'exchange': 'exchange',
    'buySell': 'buy_sell',
    'openCloseIndicator': 'open_close',
    'orderType': 'order_type',
    'notes': 'notes',
    'quantity': 'quantity',
    'tradePrice': 'trade_price',
    'proceeds': 'proceeds',
    'ibCommission': 'ib_commission',
    'netCash': 'net_cash',
    'cost': 'cost',
    'fifoPnlRealized': 'fifo_pnl_realized',
    'mtmPnl': 'mtm_pnl',
}

_STMT_FUNDS_FIELDS = {
    'transactionID': 'transaction_id',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'conid': 'conid',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'reportDate': 'report_date',
    'date': 'date',
    'settleDate': 'settle_date',
    'activityCode': 'activity_code',
    'activityDescription': 'activity_description',
    'tradeID': 'trade_id',
    'relatedTradeID': 'related_trade_id',
    'orderID': 'order_id',
    'buySell': 'buy_sell',
    'tradeQuantity': 'trade_quantity',
    'tradePrice': 'trade_price',
    'tradeGross': 'trade_gross',
    'tradeCommission': 'trade_commission',
    'tradeTax': 'trade_tax',
    'debit': 'debit',
    'credit': 'credit',
    'amount': 'amount',
    'tradeCode': 'trade_code',
    'balance': 'balance',
    'levelOfDetail': 'level_of_detail',
    'origTransactionID': 'orig_transaction_id',
    'relatedTransactionID': 'related_transaction_id',
    'actionID': 'action_id',
}

_OPEN_POS_FIELDS = {
    'reportDate': 'report_date',
    'accountId': 'account_id',
    'conid': 'conid',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'subCategory': 'sub_category',
    'symbol': 'symbol',
    'description': 'description',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'position': 'position',
    'markPrice': 'mark_price',
    'positionValue': 'position_value',
    'openPrice': 'open_price',
    'costBasisPrice': 'cost_basis_price',
    'costBasisMoney': 'cost_basis_money',
    'percentOfNAV': 'percent_of_nav',
    'fifoPnlUnrealized': 'fifo_pnl_unrealized',
    'side': 'side',
    'openDateTime': 'open_date_time',
    'originatingOrderID': 'originating_order_id',
    'originatingTransactionID': 'originating_transaction_id',
}

_CORP_ACTION_FIELDS = {
    'transactionID': 'transaction_id',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'conid': 'conid',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'reportDate': 'report_date',
    'dateTime': 'date_time',
    'actionDescription': 'action_description',
    'type': 'type',
    'amount': 'amount',
    'proceeds': 'proceeds',
    'value': 'value',
    'quantity': 'quantity',
    'cost': 'cost',
    'realizedPnl': 'realized_pnl',
    'mtmPnl': 'mtm_pnl',
    'code': 'code',
    'actionID': 'action_id',
}

_OPTION_EAE_FIELDS = {
    'tradeID': 'trade_id',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'conid': 'conid',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'date': 'date',
    'transactionType': 'transaction_type',
    'quantity': 'quantity',
    'tradePrice': 'trade_price',
    'markPrice': 'close_price',   # XML markPrice → DB close_price
    'proceeds': 'proceeds',
    'commisionsAndTax': 'comm_tax',  # IBKR typo: "commisionsAndTax"
    'costBasis': 'basis',
    'realizedPnl': 'realized_pnl',
    'mtmPnl': 'mtm_pnl',
}

_NAV_FIELDS = {
    'reportDate': 'report_date',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'cash': 'cash',
    'cashLong': 'cash_long',
    'cashShort': 'cash_short',
    'stock': 'stock',
    'stockLong': 'stock_long',
    'stockShort': 'stock_short',
    'options': 'options',
    'optionsLong': 'options_long',
    'optionsShort': 'options_short',
    # dividend_accruals intentionally omitted — DEAD column, see schema.py Edit C
    'interestAccruals': 'interest_accruals',
    'interestAccrualsLong': 'interest_accruals_long',
    'interestAccrualsShort': 'interest_accruals_short',
    'bondInterestAccrualsComponent': 'bond_interest_accruals_component',
    'bondInterestAccrualsComponentLong': 'bond_interest_accruals_component_long',
    'bondInterestAccrualsComponentShort': 'bond_interest_accruals_component_short',
    'brokerFeesAccrualsComponent': 'broker_fees_accruals_component',
    'brokerFeesAccrualsComponentLong': 'broker_fees_accruals_component_long',
    'brokerFeesAccrualsComponentShort': 'broker_fees_accruals_component_short',
    'marginFinancingChargeAccruals': 'margin_financing_charge_accruals',
    'marginFinancingChargeAccrualsLong': 'margin_financing_charge_accruals_long',
    'marginFinancingChargeAccrualsShort': 'margin_financing_charge_accruals_short',
    'crypto': 'crypto',
    'cryptoLong': 'crypto_long',
    'cryptoShort': 'crypto_short',
    'total': 'total',
    'totalLong': 'total_long',
    'totalShort': 'total_short',
}

_CHANGE_NAV_FIELDS = {
    'fromDate': 'from_date',
    'toDate': 'to_date',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'startingValue': 'starting_value',
    'mtm': 'mtm',
    'realized': 'realized',
    'changeInUnrealized': 'change_in_unrealized',
    'costAdjustments': 'cost_adjustments',
    'transferredPnlAdjustments': 'transferred_pnl_adjustments',
    'depositsWithdrawals': 'deposits_withdrawals',
    'internalCashTransfers': 'internal_cash_transfers',
    'assetTransfers': 'asset_transfers',
    'dividends': 'dividends',
    'withholdingTax': 'withholding_tax',
    'changeInDividendAccruals': 'change_in_dividend_accruals',
    'interest': 'interest',
    'changeInInterestAccruals': 'change_in_interest_accruals',
    'brokerFees': 'broker_fees',
    'changeInBrokerFeeAccruals': 'change_in_broker_fee_accruals',
    'otherFees': 'other_fees',
    'otherIncome': 'other_income',
    'commissions': 'commissions',
    'other': 'other',
    'endingValue': 'ending_value',
    'twr': 'twr',
    'corporateActionProceeds': 'corporate_action_proceeds',
}

_FIFO_PERF_FIELDS = {
    'reportDate': 'report_date',
    'accountId': 'account_id',
    'conid': 'conid',
    'acctAlias': 'acct_alias',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'costAdj': 'cost_adj',
    'realizedSTProfit': 'realized_st_profit',
    'realizedSTLoss': 'realized_st_loss',
    'realizedLTProfit': 'realized_lt_profit',
    'realizedLTLoss': 'realized_lt_loss',
    'totalRealizedPnl': 'total_realized_pnl',
    'unrealizedProfit': 'unrealized_profit',
    'unrealizedLoss': 'unrealized_loss',
    'unrealizedSTProfit': 'unrealized_st_profit',
    'unrealizedSTLoss': 'unrealized_st_loss',
    'unrealizedLTProfit': 'unrealized_lt_profit',
    'unrealizedLTLoss': 'unrealized_lt_loss',
    'totalUnrealizedPnl': 'total_unrealized_pnl',
    'totalFifoPnl': 'total_fifo_pnl',
    'transferredPnl': 'transferred_pnl',
    'code': 'code',
}

_MTM_PERF_FIELDS = {
    'reportDate': 'report_date',
    'accountId': 'account_id',
    'conid': 'conid',
    'acctAlias': 'acct_alias',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'prevCloseQuantity': 'previous_close_quantity',
    'prevClosePrice': 'previous_close_price',
    'closeQuantity': 'close_quantity',
    'closePrice': 'close_price',
    'transactionMtm': 'transaction_mtm_pnl',
    'priorOpenMtm': 'prior_open_mtm_pnl',
    'commissions': 'commissions',
    'other': 'other',
    'otherWithAccruals': 'other_accruals',
    'total': 'total',
    'totalWithAccruals': 'total_accruals',
    'code': 'code',
}

_DIV_ACCRUAL_FIELDS = {
    'reportDate': 'report_date',
    'accountId': 'account_id',
    'conid': 'conid',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'exDate': 'ex_date',
    'payDate': 'pay_date',
    'quantity': 'quantity',
    'tax': 'tax',
    'fee': 'fee',
    'grossRate': 'gross_rate',
    'grossAmount': 'gross_amount',
    'netAmount': 'net_amount',
    'code': 'code',
}

_TRANSFER_FIELDS = {
    'transactionID': 'transaction_id',
    'accountId': 'account_id',
    'acctAlias': 'acct_alias',
    'currency': 'currency',
    'assetCategory': 'asset_category',
    'symbol': 'symbol',
    'description': 'description',
    'conid': 'conid',
    'underlyingConid': 'underlying_conid',
    'underlyingSymbol': 'underlying_symbol',
    'multiplier': 'multiplier',
    'strike': 'strike',
    'expiry': 'expiry',
    'putCall': 'put_call',
    'reportDate': 'report_date',
    'date': 'date',
    'dateTime': 'date_time',
    'settleDate': 'settle_date',
    'type': 'type',
    'direction': 'direction',
    'company': 'transfer_company',
    'account': 'transfer_account',
    'accountName': 'transfer_account_name',
    'deliveringBroker': 'delivering_broker',
    'quantity': 'quantity',
    'transferPrice': 'transfer_price',
    'positionAmount': 'position_amount',
    'positionAmountInBase': 'position_amount_in_base',
    'pnlAmount': 'pnl_amount',
    'pnlAmountInBase': 'pnl_amount_in_base',
    'cashTransfer': 'cash_transfer',
    'code': 'code',
    'clientReference': 'client_reference',
    'levelOfDetail': 'level_of_detail',
}

# ---------------------------------------------------------------------------
# NAV section note: the XML element 'dividendAccruals' is NOT in the
# EquitySummaryByReportDateInBase rows — that field appears in the
# OpenDividendAccruals section instead. The schema column
# 'dividend_accruals' in master_log_nav will be NULL from this sync path.
# ---------------------------------------------------------------------------

# Master section registry
# (xml_container, xml_row_tag_or_None, db_table, field_map, pk_columns)
SECTIONS = [
    ('AccountInformation', None, 'master_log_account_info',
     _ACCOUNT_INFO_FIELDS, ['account_id']),
    ('ChangeInNAV', None, 'master_log_change_in_nav',
     _CHANGE_NAV_FIELDS, ['from_date', 'to_date', 'account_id']),
    ('Trades', 'Trade', 'master_log_trades',
     _TRADE_FIELDS, ['transaction_id']),
    ('StmtFunds', 'StatementOfFundsLine', 'master_log_statement_of_funds',
     _STMT_FUNDS_FIELDS, ['transaction_id']),
    ('OpenPositions', 'OpenPosition', 'master_log_open_positions',
     _OPEN_POS_FIELDS, ['report_date', 'account_id', 'conid']),
    ('CorporateActions', 'CorporateAction', 'master_log_corp_actions',
     _CORP_ACTION_FIELDS, ['transaction_id']),
    ('OptionEAE', 'OptionEAE', 'master_log_option_eae',
     _OPTION_EAE_FIELDS, ['trade_id']),
    ('EquitySummaryInBase', 'EquitySummaryByReportDateInBase', 'master_log_nav',
     _NAV_FIELDS, ['report_date', 'account_id']),
    ('FIFOPerformanceSummaryInBase', 'FIFOPerformanceSummaryUnderlying',
     'master_log_realized_unrealized_perf', _FIFO_PERF_FIELDS,
     ['report_date', 'account_id', 'conid']),
    ('MTMPerformanceSummaryInBase', 'MTMPerformanceSummaryUnderlying',
     'master_log_mtm_perf', _MTM_PERF_FIELDS,
     ['report_date', 'account_id', 'conid']),
    ('OpenDividendAccruals', 'OpenDividendAccrual', 'master_log_div_accruals',
     _DIV_ACCRUAL_FIELDS, ['report_date', 'account_id', 'conid']),
    ('Transfers', 'Transfer', 'master_log_transfers',
     _TRANSFER_FIELDS, ['transaction_id']),
]


# ---------------------------------------------------------------------------
# XML pull
# ---------------------------------------------------------------------------

def pull_flex_xml() -> bytes:
    """Pull raw XML from IBKR Flex Web Service. Returns XML bytes."""
    url = f"{FLEX_ENDPOINT_BASE}/SendRequest?t={FLEX_TOKEN}&q={FLEX_QUERY_ID}&v=3"
    req = urllib.request.Request(url, headers={"User-Agent": "AGT-Equities/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        send_body = r.read().decode()

    send_root = ET.fromstring(send_body)
    status_el = send_root.find("Status")
    if status_el is not None and status_el.text != "Success":
        ec = send_root.find("ErrorCode")
        em = send_root.find("ErrorMessage")
        raise RuntimeError(
            f"Flex SendRequest failed: code={ec.text if ec is not None else '?'}, "
            f"msg={em.text if em is not None else '?'}"
        )

    ref = send_root.find("ReferenceCode").text
    logger.info("Flex SendRequest OK, reference=%s", ref)

    for attempt in range(1, FLEX_MAX_POLL_RETRIES + 1):
        time.sleep(FLEX_POLL_DELAY_SECONDS)
        get_url = f"{FLEX_ENDPOINT_BASE}/GetStatement?t={FLEX_TOKEN}&q={ref}&v=3"
        req = urllib.request.Request(get_url, headers={"User-Agent": "AGT-Equities/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            xml_bytes = r.read()

        xml_text = xml_bytes.decode()
        if xml_text.strip().startswith("<FlexStatementResponse>"):
            err_root = ET.fromstring(xml_text)
            ec = err_root.find("ErrorCode")
            if ec is not None and ec.text == "1019":
                logger.info("Flex not ready (attempt %d/%d)", attempt, FLEX_MAX_POLL_RETRIES)
                continue
            elif ec is not None:
                em = err_root.find("ErrorMessage")
                raise RuntimeError(
                    f"Flex GetStatement failed: code={ec.text}, "
                    f"msg={em.text if em is not None else '?'}"
                )
        logger.info("Flex report received: %d bytes", len(xml_bytes))
        return xml_bytes

    raise RuntimeError("Flex report not ready after max retries")


def load_flex_xml_from_file(path: str | Path) -> bytes:
    """Load Flex XML from a local file (for testing/bootstrap)."""
    with open(path, 'rb') as f:
        return f.read()


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _extract_row(element: ET.Element, field_map: dict) -> dict:
    """Extract {db_column: value} from an XML element's attributes."""
    row = {}
    for xml_attr, db_col in field_map.items():
        val = element.attrib.get(xml_attr, '')
        row[db_col] = val if val != '' else None
    return row


def parse_flex_xml(xml_bytes: bytes) -> list[dict]:
    """Parse Flex XML into a list of section dicts with rows.

    Each dict: {table, rows, pk_cols, account_id}.
    """
    root = ET.fromstring(xml_bytes)
    results = []

    for fs in root.findall('.//FlexStatement'):
        account_id = fs.attrib.get('accountId', '')
        stmt_to_date = fs.attrib.get('toDate', '')  # fallback for empty reportDate

        for xml_container, xml_row_tag, db_table, field_map, pk_cols in SECTIONS:
            container = fs.find(xml_container)
            if container is None:
                continue

            if xml_row_tag is None:
                # Attribute-only (AccountInformation, ChangeInNAV)
                row = _extract_row(container, field_map)
                rows = [row] if any(v is not None for v in row.values()) else []
            else:
                rows = [_extract_row(el, field_map) for el in container.findall(xml_row_tag)]

            # Fill empty report_date from FlexStatement.toDate for summary sections
            if 'report_date' in field_map.values():
                for row in rows:
                    if row.get('report_date') is None and stmt_to_date:
                        row['report_date'] = stmt_to_date

            if rows:
                results.append({
                    'table': db_table,
                    'rows': rows,
                    'pk_cols': pk_cols,
                    'account_id': account_id,
                })

    return results


# ---------------------------------------------------------------------------
# UPSERT
# ---------------------------------------------------------------------------

def _upsert_rows(conn: sqlite3.Connection, table: str, rows: list[dict],
                 pk_cols: list[str], now: str) -> tuple[int, int]:
    """UPSERT rows into a table. Returns (inserted, updated)."""
    if not rows:
        return 0, 0

    inserted = updated = 0
    sample = rows[0]
    all_cols = list(sample.keys()) + ['last_synced_at']
    non_pk_cols = [c for c in all_cols if c not in pk_cols and c != 'last_synced_at']

    col_list = ', '.join(all_cols)
    placeholders = ', '.join(f':{c}' for c in all_cols)
    conflict_target = ', '.join(pk_cols)

    if non_pk_cols:
        update_sets = ', '.join(f'{c} = excluded.{c}' for c in non_pk_cols)
        update_sets += ', last_synced_at = excluded.last_synced_at'
        # Only update when at least one value changed
        where_parts = [f'{table}.{c} IS NOT excluded.{c}' for c in non_pk_cols[:6]]
        where_clause = ' OR '.join(where_parts)

        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO UPDATE SET {update_sets} "
            f"WHERE {where_clause}"
        )
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"

    for row in rows:
        # Skip rows where any PK column is NULL (summary rows without keys)
        if any(row.get(pk) is None for pk in pk_cols):
            continue
        params = {**row, 'last_synced_at': now}
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 1:
            inserted += 1

    return inserted, 0


# ---------------------------------------------------------------------------
# Walker warnings persistence (W3.6)
# ---------------------------------------------------------------------------

def _persist_walker_warnings(conn: sqlite3.Connection, sync_id: str) -> None:
    """Run walker across all (household, ticker) groups and persist warnings.

    Writes to walker_warnings_log (Bucket 3). Non-fatal — caller must catch.
    """
    import json
    from itertools import groupby
    from agt_equities import trade_repo as _tr
    from agt_equities.walker import walk_cycles, get_walker_warnings, UnknownEventError

    # NOTE (A3): trade_repo.DB_PATH was deleted in FU-A; this assignment is dead.
    # Connection is passed explicitly via the loaders below.
    all_ev = _tr._load_trade_events(conn)
    ci_ev = _tr._load_carryin_events(conn)
    combined = ci_ev + all_ev
    combined.sort(key=lambda e: (e.household_id, e.ticker))

    all_warnings = []
    for (hh, tk), grp in groupby(combined, key=lambda e: (e.household_id, e.ticker)):
        try:
            walk_cycles(list(grp))
            all_warnings.extend(get_walker_warnings())
        except UnknownEventError:
            # Frozen ticker — not a warning, handled separately
            pass

    # Clear previous warnings for this sync_id, then insert new ones
    conn.execute("DELETE FROM walker_warnings_log WHERE sync_id = ?", (sync_id,))
    for w in all_warnings:
        conn.execute(
            "INSERT INTO walker_warnings_log "
            "(sync_id, code, severity, ticker, household, account, message, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sync_id, w.code, w.severity, w.ticker, w.household, w.account,
             w.message, json.dumps(w.context) if w.context else None),
        )
    # A3: caller (run_sync) owns the surrounding tx_immediate; do not commit here.
    logger.info("Walker warnings persisted: %d warnings for sync_id=%s", len(all_warnings), sync_id)


# ---------------------------------------------------------------------------
# ADR-018 Phase 1 helpers: known-trading-day classification + retry enqueue
# ---------------------------------------------------------------------------

def _is_known_trading_day(d: str, *, conn: sqlite3.Connection) -> bool:
    """Return True if date D (YYYYMMDD) was a confirmed trading day.

    Evidence (either is sufficient):
      A) pending_orders has status='filled' rows with fill_time on D for
         any tracked account, OR
      B) daemon_heartbeat has last_beat_utc rows landing inside 09:30-16:00
         ET on D with gap < 120s continuous (heartbeat writer emits every
         30s; absent restart, 120s SLA easily met).

    Conservative: any single failing query returns False rather than
    raising, so this helper never blows up the caller. False positives
    (classifying a trading day as non-trading) silently lose a zero-row
    refusal event; false negatives (classifying a non-trading day as
    trading) raise a spurious incident. We favor the former — incidents
    on non-trading days are noisier than rare missed known-trading
    days.
    """
    if not d or len(d) != 8 or not d.isdigit():
        return False
    d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    # Evidence A: filled pending_orders on D.
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_orders "
            "WHERE status='filled' AND DATE(fill_time) = ?",
            (d_iso,),
        ).fetchone()
        if row and row[0] and row[0] > 0:
            return True
    except sqlite3.OperationalError:
        pass  # table/column missing on bootstrap DB — treat as inconclusive

    # Evidence B: heartbeat continuity during RTH (09:30-16:00 ET = 13:30-20:00 UTC
    # winter / 14:30-21:00 UTC summer DST — use 13:30-21:00 UTC as a conservative
    # superset since we don't want to misclassify a DST-boundary day).
    try:
        rth_start = f"{d_iso}T13:30:00+00:00"
        rth_end = f"{d_iso}T21:00:00+00:00"
        row = conn.execute(
            "SELECT COUNT(*) FROM daemon_heartbeat "
            "WHERE last_beat_utc BETWEEN ? AND ?",
            (rth_start, rth_end),
        ).fetchone()
        # Any heartbeat inside RTH is evidence the bot was running; we don't
        # require continuous coverage (that would be stricter than needed).
        if row and row[0] and row[0] > 0:
            return True
    except sqlite3.OperationalError:
        pass

    return False


def _enqueue_flex_retry_attempt(
    conn: sqlite3.Connection,
    *,
    original_sync_id: int,
    coverage_date: str,
    attempt_n: int,
    scheduled_at_utc: str,
) -> int:
    """Insert a pending-retry row in flex_sync_retry_attempts. Returns row id.

    The `flex_sync_retry_poller` scheduler job (agt_scheduler.py) scans this
    table every 15 minutes for due rows and invokes run_sync with the
    retry_attempt_n kwarg. This table survives process restarts; APScheduler
    one-shot jobs would not.

    Returns 0 if the retry_attempts table is missing (bootstrap/test DB).
    """
    try:
        cur = conn.execute(
            "INSERT INTO flex_sync_retry_attempts "
            "(original_sync_id, coverage_date, attempt_n, scheduled_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (original_sync_id, coverage_date, attempt_n, scheduled_at_utc),
        )
        return cur.lastrowid or 0
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.warning(
                "flex_sync_retry_attempts table missing; retry not persisted. "
                "Run scripts/migrate_flex_sync_retry_attempts.py"
            )
            return 0
        raise


def _filter_section_rows_by_date(
    section_data: list[dict], from_date: str, to_date: str,
) -> tuple[list[dict], int]:
    """Filter master_log_trades rows to the [from_date, to_date] window.

    Other sections pass through unchanged — they don't carry a per-row
    trade_date column. Returns (filtered_section_data, kept_row_count).
    """
    filtered: list[dict] = []
    kept = 0
    for sd in section_data:
        if sd.get("table") != "master_log_trades":
            filtered.append(sd)
            kept += len(sd.get("rows", []))
            continue
        kept_rows = [
            r for r in sd.get("rows", [])
            if from_date <= (r.get("trade_date") or "") <= to_date
        ]
        kept += len(kept_rows)
        new_sd = dict(sd)
        new_sd["rows"] = kept_rows
        filtered.append(new_sd)
    return filtered, kept


def _raise_zero_row_incident(
    *,
    sync_id: int,
    coverage_date: str,
    attempt_n: int,
    bot_uptime_window_seconds: int,
    filled_pending_orders_count: int,
    flex_response_size_bytes: int,
    conn: sqlite3.Connection,
) -> None:
    """Raise FLEX_SYNC_EMPTY_KNOWN_TRADING_DAY (or PERSISTENT_EMPTY after
    attempt 4) as a tier-0 incident. Best-effort: failures here must not
    abort the caller.
    """
    try:
        from agt_equities.incidents_repo import register as incident_register
    except Exception as imp_exc:
        logger.error("incidents_repo import failed: %s", imp_exc)
        return

    persistent = attempt_n >= 4
    invariant_id = (
        "FLEX_SYNC_PERSISTENT_EMPTY" if persistent
        else "FLEX_SYNC_EMPTY_KNOWN_TRADING_DAY"
    )
    incident_key = f"{invariant_id}:{coverage_date}"
    evidence = {
        "sync_id": sync_id,
        "coverage_date": coverage_date,
        "attempt_n": attempt_n,
        "bot_uptime_window_seconds": bot_uptime_window_seconds,
        "filled_pending_orders_count": filled_pending_orders_count,
        "flex_response_size_bytes": flex_response_size_bytes,
    }
    try:
        incident_register(
            incident_key,
            severity="critical",
            scrutiny_tier="high",
            detector="flex_sync.run_sync",
            invariant_id=invariant_id,
            observed_state=evidence,
            desired_state={"rows_received_gt_zero": True},
        )
    except Exception as reg_exc:
        logger.error("incident register failed (%s): %s", invariant_id, reg_exc)

    if persistent:
        # Cross-daemon alert for operator Telegram path.
        try:
            from agt_equities.alerts import enqueue_alert
            enqueue_alert(
                "FLEX_SYNC_PERSISTENT_EMPTY",
                {
                    "coverage_date": coverage_date,
                    "attempt_n": attempt_n,
                    "operator_action": (
                        f"Manually verify IBKR portal and run: "
                        f"/flex_manual_reconcile {coverage_date}"
                    ),
                },
                severity="crit",
            )
        except Exception as alert_exc:
            logger.error("persistent-empty alert enqueue failed: %s", alert_exc)


_RETRY_BACKOFF_HOURS = {1: 2, 2: 4, 3: 6}  # next_attempt_n → hours delay


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_sync(
    mode: SyncMode,
    xml_bytes: bytes | None = None,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    retry_attempt_n: int = 0,
) -> SyncResult:
    """Execute a Flex sync.

    Sprint A / A3: single atomic transaction.
        Per DT Q2 ruling (2026-04-14): the data side of a sync is one
        ``BEGIN IMMEDIATE`` ... ``COMMIT`` block covering all section
        upserts, the master_log_sync success update, and the walker
        warnings persist. Per-section commits are rejected — a partial
        section pile would corrupt the master_log invariant that any
        sync_id present in master_log_sync with status='success' has the
        full row set committed.

        The sync_id allocation row (status='running') is its own small
        txn at entry — this guarantees that even a hard failure of the
        data block leaves an auditable master_log_sync row, which the
        error path then updates to status='error' in a second small txn.

        Side effects (desk_state regen, friday handoff archive, git
        auto-push) stay outside the txn — they are wall-clock effects,
        not DB state.

    Args:
        mode: INCEPTION, INCREMENTAL, or ONESHOT.
        xml_bytes: Pre-fetched XML (for testing). If None, pulls from IBKR.
        from_date, to_date: Optional YYYYMMDD window. Rows outside this
            range are dropped before upsert (ADR-018 Phase 1 targeted
            backfill support). Environment fallback: AGT_FLEX_FROM_DATE /
            AGT_FLEX_TO_DATE.
        retry_attempt_n: 0 = original fire, 1..4 = subsequent retry
            attempts on zero-row-known-trading-day escalation (ADR-018
            Phase 1). Attempt 4 that still returns zero raises
            FLEX_SYNC_PERSISTENT_EMPTY.
    """
    # ADR-018 Phase 1: env-var fallback for date kwargs.
    if from_date is None:
        from_date = os.environ.get("AGT_FLEX_FROM_DATE") or None
    if to_date is None:
        to_date = os.environ.get("AGT_FLEX_TO_DATE") or None

    now = datetime.utcnow().isoformat()
    conn = _get_db()

    # --- Audit row allocation (own small txn) ---
    with tx_immediate(conn):
        cursor = conn.execute(
            "INSERT INTO master_log_sync (started_at, flex_query_id, from_date, to_date, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (now, FLEX_QUERY_ID, from_date, to_date),
        )
        sync_id = cursor.lastrowid

    result = SyncResult(sync_id=sync_id, status='running')
    flex_response_size_bytes = 0

    try:
        if xml_bytes is None:
            xml_bytes = pull_flex_xml()
        flex_response_size_bytes = len(xml_bytes) if xml_bytes else 0

        section_data = parse_flex_xml(xml_bytes)
        rows_total_in_xml = sum(len(sd.get("rows", [])) for sd in section_data)

        # ADR-018 Phase 1: filter by date window if provided.
        if from_date and to_date:
            section_data, kept_count = _filter_section_rows_by_date(
                section_data, from_date, to_date,
            )
            logger.info(
                "Date filter applied: from=%s to=%s kept=%d (of %d)",
                from_date, to_date, kept_count, rows_total_in_xml,
            )

        result.sections_processed = len(section_data)

        # --- Single atomic data txn (A3) ---
        with tx_immediate(conn):
            total_rows = 0
            total_inserted = 0
            for sd in section_data:
                rows = sd['rows']
                total_rows += len(rows)
                ins, upd = _upsert_rows(conn, sd['table'], rows, sd['pk_cols'], now)
                total_inserted += ins
                logger.info("  %s (%s): %d rows, %d upserted",
                            sd['table'], sd['account_id'], len(rows), ins)

            result.rows_received = total_rows
            result.rows_inserted = total_inserted

            # ADR-018 Phase 1: zero-row classification. If the sync received
            # zero rows AND the coverage date is a confirmed trading day, we
            # refuse the silent 'success' path — this is the W1 bug that the
            # Wednesday 2026-04-23 Flex loss exhibited. Persist 'suspicious'
            # status, enqueue a retry attempt row, and raise a tier-0
            # incident outside the txn.
            coverage_date_for_classify: str | None = None
            is_known_td = False
            if total_rows == 0:
                # Coverage date preference: explicit to_date > env var > None.
                coverage_date_for_classify = to_date or os.environ.get("AGT_FLEX_TO_DATE") or None
                if coverage_date_for_classify:
                    is_known_td = _is_known_trading_day(
                        coverage_date_for_classify, conn=conn,
                    )

            if total_rows == 0 and is_known_td:
                next_attempt = retry_attempt_n + 1
                next_status = 'suspicious'
                result.status = 'suspicious'
                result.needs_retry = next_attempt <= 3  # attempts 2,3,4 remain
                result.retry_date = coverage_date_for_classify
                result.next_attempt_n = next_attempt
                conn.execute(
                    "UPDATE master_log_sync SET finished_at=?, sections_processed=?, "
                    "rows_received=?, rows_inserted=?, rows_updated=?, status=? "
                    "WHERE sync_id=?",
                    (datetime.utcnow().isoformat(), result.sections_processed,
                     result.rows_received, result.rows_inserted, result.rows_updated,
                     next_status, sync_id),
                )
                # Enqueue retry if we have attempts left (next_attempt 1-3 schedule).
                if result.needs_retry:
                    delay_h = _RETRY_BACKOFF_HOURS.get(next_attempt, 6)
                    scheduled_at = (
                        datetime.now(timezone.utc) + timedelta(hours=delay_h)
                    ).isoformat()
                    _enqueue_flex_retry_attempt(
                        conn,
                        original_sync_id=int(sync_id),
                        coverage_date=coverage_date_for_classify,
                        attempt_n=next_attempt,
                        scheduled_at_utc=scheduled_at,
                    )
            else:
                conn.execute(
                    "UPDATE master_log_sync SET finished_at=?, sections_processed=?, "
                    "rows_received=?, rows_inserted=?, rows_updated=?, status='success' "
                    "WHERE sync_id=?",
                    (datetime.utcnow().isoformat(), result.sections_processed,
                     result.rows_received, result.rows_inserted, result.rows_updated,
                     sync_id),
                )

            # W3.6: walker warnings inside the same txn so success+warnings are
            # all-or-nothing. _persist_walker_warnings no longer commits.
            try:
                _persist_walker_warnings(conn, str(sync_id))
            except Exception as warn_exc:
                # Walker warning failure must NOT roll the entire sync back.
                # Log + swallow so the success commit stands.
                logger.error("Walker warnings persist failed (non-fatal): %s", warn_exc)

        # If we got here, the atomic txn committed.
        if result.status != 'suspicious':
            result.status = 'success'

        # ADR-018 Phase 1: raise tier-0 incident outside the txn on suspicious.
        # FLEX_SYNC_EMPTY_KNOWN_TRADING_DAY (attempts 1..3) or
        # FLEX_SYNC_PERSISTENT_EMPTY (attempt 4+). Evidence includes the
        # sync_id, coverage_date, attempt number, and a best-effort uptime
        # measure so operator can correlate with known outages.
        if result.status == 'suspicious' and coverage_date_for_classify:
            try:
                filled_count_row = conn.execute(
                    "SELECT COUNT(*) FROM pending_orders "
                    "WHERE status='filled' AND DATE(fill_time) = ?",
                    (f"{coverage_date_for_classify[:4]}-"
                     f"{coverage_date_for_classify[4:6]}-"
                     f"{coverage_date_for_classify[6:8]}",),
                ).fetchone()
                filled_count = filled_count_row[0] if filled_count_row else 0
            except Exception:
                filled_count = 0
            _raise_zero_row_incident(
                sync_id=int(sync_id),
                coverage_date=coverage_date_for_classify,
                attempt_n=retry_attempt_n,  # "attempt that just failed"
                bot_uptime_window_seconds=0,
                filled_pending_orders_count=int(filled_count),
                flex_response_size_bytes=int(flex_response_size_bytes),
                conn=conn,
            )

        # --- A5d.b: digest alert via cross_daemon_alerts bus (best-effort) ---
        # Enqueued AFTER the data txn commits but BEFORE side-effects so a
        # later side-effect failure (desk_state regen, git push) does not
        # gate the operator's success notification. Lazy import keeps the
        # alerts module out of flex_sync's import graph for callers who
        # don't need the bus. Failure here is non-fatal: log + swallow.
        try:
            from agt_equities.alerts import enqueue_alert
            enqueue_alert(
                "FLEX_SYNC_DIGEST",
                {
                    "sync_id": int(sync_id),
                    "mode": mode.value if hasattr(mode, "value") else str(mode),
                    "sections_processed": int(result.sections_processed),
                    "rows_received": int(result.rows_received),
                    "rows_inserted": int(result.rows_inserted),
                },
                severity="info",
            )
        except Exception as alert_exc:
            logger.warning("flex_sync digest alert enqueue failed: %s", alert_exc)

        # --- Side effects (outside DB txn) ---
        # Phase 3A: Regenerate desk_state.md after successful sync
        try:
            from agt_deck.desk_state_writer import write_desk_state_atomic, generate_desk_state
            mode_now = ""  # ADR-014: mode engine retired
            content = generate_desk_state(
                mode=mode_now, household_data={}, rule_evaluations=[],
                glide_paths=[], walker_warning_count=0,
                walker_worst_severity=None, recent_transitions=[],
                report_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
            write_desk_state_atomic(content)
        except Exception as ds_exc:
            logger.error("desk_state.md regeneration failed (non-fatal): %s", ds_exc)

        # Friday EOD: archive handoff docs before git push
        try:
            if datetime.utcnow().weekday() == 4:  # Friday
                from scripts.archive_handoffs import archive_handoffs
                archive_handoffs()
        except Exception as arch_exc:
            logger.warning("handoff archive failed: %s", arch_exc)

        # Git auto-commit + push after successful sync
        try:
            import subprocess
            _git_cwd = r"C:\AGT_Telegram_Bridge"
            subprocess.run(
                ["git", "add", "reports/", "*.md", "schema.py",
                 "agt_equities/", "agt_deck/", "telegram_bot.py"],
                cwd=_git_cwd, check=False, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m",
                 f"auto: EOD {datetime.utcnow().strftime('%Y-%m-%d')}"],
                cwd=_git_cwd, check=False, timeout=30,
            )
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=_git_cwd, check=False, timeout=60,
            )
        except Exception as git_exc:
            logger.warning("git auto-push failed: %s", git_exc)

    except Exception as exc:
        result.status = 'error'
        result.error_message = str(exc)
        logger.exception("Flex sync failed: %s", exc)
        # --- Error audit (own small txn so it survives data rollback) ---
        try:
            with tx_immediate(conn):
                conn.execute(
                    "UPDATE master_log_sync SET finished_at=?, status='error', error_message=? "
                    "WHERE sync_id=?",
                    (datetime.utcnow().isoformat(), str(exc), sync_id),
                )
        except Exception as audit_exc:
            logger.exception("master_log_sync error-audit write failed: %s", audit_exc)

    finally:
        conn.close()

    return result


def sync_from_file(path: str | Path) -> SyncResult:
    """Convenience: run a sync from a local XML file."""
    xml_bytes = load_flex_xml_from_file(path)
    return run_sync(SyncMode.ONESHOT, xml_bytes=xml_bytes)


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

@dataclass
class DryRunPlan:
    """Transaction plan from a dry-run sync."""
    xml_bytes_size: int = 0
    accounts: int = 0
    sections: int = 0
    per_table: dict = field(default_factory=dict)  # {table: {rows, would_insert, would_skip_null_pk}}
    total_rows: int = 0
    total_would_insert: int = 0


def dry_run_sync(
    xml_bytes: bytes | None = None,
    db_path_override: str | Path | None = None,
) -> DryRunPlan:
    """Pull and parse Flex XML, compute would-be writes, write NOTHING.

    If xml_bytes is None, pulls live from IBKR.
    Compares parsed rows against current DB state to compute insert/skip counts.
    """
    if xml_bytes is None:
        xml_bytes = pull_flex_xml()

    plan = DryRunPlan(xml_bytes_size=len(xml_bytes))

    section_data = parse_flex_xml(xml_bytes)
    plan.sections = len(section_data)

    # Count accounts
    plan.accounts = len(set(sd['account_id'] for sd in section_data))

    # Open DB read-only to check existing rows
    target = db_path_override or DB_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row

    for sd in section_data:
        table = sd['table']
        rows = sd['rows']
        pk_cols = sd['pk_cols']

        if table not in plan.per_table:
            plan.per_table[table] = {
                'rows_parsed': 0,
                'would_insert': 0,
                'would_update': 0,
                'would_skip_null_pk': 0,
            }

        entry = plan.per_table[table]

        for row in rows:
            entry['rows_parsed'] += 1
            plan.total_rows += 1

            # Skip null PK
            if any(row.get(pk) is None for pk in pk_cols):
                entry['would_skip_null_pk'] += 1
                continue

            # Check if PK already exists
            try:
                where = ' AND '.join(f'{pk} = ?' for pk in pk_cols)
                pk_vals = [row[pk] for pk in pk_cols]
                existing = conn.execute(
                    f"SELECT 1 FROM {table} WHERE {where}", pk_vals
                ).fetchone()
                if existing:
                    entry['would_update'] += 1
                else:
                    entry['would_insert'] += 1
                    plan.total_would_insert += 1
            except Exception:
                # Table doesn't exist yet — all rows are inserts
                entry['would_insert'] += 1
                plan.total_would_insert += 1

    conn.close()
    return plan
