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
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FLEX_TOKEN = os.environ.get("AGT_FLEX_TOKEN", os.environ.get("FLEX_TOKEN", ""))
FLEX_QUERY_ID = "1461095"
FLEX_ENDPOINT_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_POLL_DELAY_SECONDS = 25
FLEX_MAX_POLL_RETRIES = 6
FLEX_INCEPTION_FROM_DATE = "20250901"

DB_PATH = Path(__file__).resolve().parent.parent / "agt_desk.db"


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
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
    status: str  # 'success' or 'error'
    sections_processed: int = 0
    rows_received: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    anomalies: list = field(default_factory=list)
    error_message: Optional[str] = None


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

    _tr.DB_PATH = str(conn.execute("PRAGMA database_list").fetchone()[2])

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
    conn.commit()
    logger.info("Walker warnings persisted: %d warnings for sync_id=%s", len(all_warnings), sync_id)


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_sync(mode: SyncMode, xml_bytes: bytes | None = None) -> SyncResult:
    """Execute a Flex sync.

    Args:
        mode: INCEPTION, INCREMENTAL, or ONESHOT.
        xml_bytes: Pre-fetched XML (for testing). If None, pulls from IBKR.
    """
    now = datetime.utcnow().isoformat()
    conn = _get_db()

    cursor = conn.execute(
        "INSERT INTO master_log_sync (started_at, flex_query_id, status) "
        "VALUES (?, ?, 'running')",
        (now, FLEX_QUERY_ID),
    )
    sync_id = cursor.lastrowid
    conn.commit()

    result = SyncResult(sync_id=sync_id, status='running')

    try:
        if xml_bytes is None:
            xml_bytes = pull_flex_xml()

        section_data = parse_flex_xml(xml_bytes)
        result.sections_processed = len(section_data)

        total_rows = 0
        total_inserted = 0
        for sd in section_data:
            rows = sd['rows']
            total_rows += len(rows)
            ins, upd = _upsert_rows(conn, sd['table'], rows, sd['pk_cols'], now)
            total_inserted += ins
            logger.info("  %s (%s): %d rows, %d upserted",
                        sd['table'], sd['account_id'], len(rows), ins)

        conn.commit()

        result.rows_received = total_rows
        result.rows_inserted = total_inserted
        result.status = 'success'

        conn.execute(
            "UPDATE master_log_sync SET finished_at=?, sections_processed=?, "
            "rows_received=?, rows_inserted=?, rows_updated=?, status='success' "
            "WHERE sync_id=?",
            (datetime.utcnow().isoformat(), result.sections_processed,
             result.rows_received, result.rows_inserted, result.rows_updated,
             sync_id),
        )
        conn.commit()

        # W3.6: Post-sync walker warnings pass — persist to walker_warnings_log
        try:
            _persist_walker_warnings(conn, str(sync_id))
        except Exception as warn_exc:
            logger.error("Walker warnings persist failed (non-fatal): %s", warn_exc)

        # Phase 3A: Regenerate desk_state.md after successful sync
        try:
            from agt_deck.desk_state_writer import write_desk_state_atomic, generate_desk_state
            from agt_equities.mode_engine import get_current_mode
            # Minimal desk_state with available data — full version runs on 5-min scheduler
            mode = get_current_mode(conn)
            content = generate_desk_state(
                mode=mode, household_data={}, rule_evaluations=[],
                glide_paths=[], walker_warning_count=0,
                walker_worst_severity=None, recent_transitions=[],
                report_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
            write_desk_state_atomic(content)
        except Exception as ds_exc:
            logger.error("desk_state.md regeneration failed (non-fatal): %s", ds_exc)

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
        conn.execute(
            "UPDATE master_log_sync SET finished_at=?, status='error', error_message=? "
            "WHERE sync_id=?",
            (datetime.utcnow().isoformat(), str(exc), sync_id),
        )
        conn.commit()

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
