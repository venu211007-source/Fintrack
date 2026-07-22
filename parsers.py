import os
import re
import pandas as pd
from datetime import datetime
from dateutil import parser as date_parser

DATE_KEYS   = ['date', 'transaction date', 'value date', 'posting date', 'trans date',
               'txn date', 'booking date', 'trans. date', 'tran date', 'valuedate',
               # Deutsche Bank
               'buchungsdatum', 'wertstellung',
               # US banks
               'post date']
DESC_KEYS   = ['description', 'narration', 'details', 'memo', 'particulars',
               'transaction details', 'remarks', 'narrative', 'transaction remarks',
               'reference', 'beneficiary', 'chq / trn no.', 'tran particulars',
               # Deutsche Bank
               'verwendungszweck', 'beguenstigter/auftraggeber', 'glaeubiger id',
               # US banks
               'name on card', 'merchant name']
DEBIT_KEYS  = ['debit', 'withdrawal', 'withdrawals', 'dr', 'money out',
               'debit amount', 'amount debited', 'withdrawal amt.', 'withdrawal amt',
               'debit amt.', 'debit amt', 'dr amount',
               # Deutsche Bank
               'soll', 'belastung']
CREDIT_KEYS = ['credit', 'deposit', 'deposits', 'cr', 'money in',
               'credit amount', 'amount credited', 'deposit amt.', 'deposit amt',
               'credit amt.', 'credit amt', 'cr amount',
               # Deutsche Bank
               'haben', 'gutschrift']
AMOUNT_KEYS = ['amount', 'net amount', 'transaction amount', 'value',
               # Deutsche Bank
               'betrag', 'umsatz',
               # US banks (Chase uses single signed Amount column)
               'debit amount', 'credit amount']

# Patterns that begin a new HDFC/compressed-format transaction narration
_HDFC_TXN_START = re.compile(
    r'^(UPI[-\s]|NEFT|IB\s*FUNDS|ACH\s*[DC][-\s]|IMPS[-\s]|INTEREST\s*PAID|'
    r'TAX\s*DEDUCTED|FT[-\s]|UPILITE|UPIRET|ATM\s*W/D|POS\s*|RTGS)',
    re.IGNORECASE,
)
_DATE_RE   = re.compile(r'^\d{2}/\d{2}/\d{2}$')
_AMOUNT_RE = re.compile(r'^[\d,]+\.\d{2}$')

# Bank name detection patterns (searched in PDF raw text / CSV headers)
_BANK_SIGNATURES = {
    # Indian banks
    'hdfc':      re.compile(r'HDFC\s*BANK', re.IGNORECASE),
    'sbi':       re.compile(r'STATE\s*BANK\s*OF\s*INDIA|SBIINB', re.IGNORECASE),
    'icici':     re.compile(r'ICICI\s*BANK', re.IGNORECASE),
    'axis':      re.compile(r'AXIS\s*BANK', re.IGNORECASE),
    'kotak':     re.compile(r'KOTAK\s*(MAHINDRA)?\s*BANK', re.IGNORECASE),
    'yes':       re.compile(r'YES\s*BANK', re.IGNORECASE),
    'indusind':  re.compile(r'INDUSIND\s*BANK', re.IGNORECASE),
    'federal':   re.compile(r'FEDERAL\s*BANK', re.IGNORECASE),
    'idfc':      re.compile(r'IDFC\s*(FIRST)?\s*BANK', re.IGNORECASE),
    'idbi':      re.compile(r'IDBI\s*BANK', re.IGNORECASE),
    'pnb':       re.compile(r'PUNJAB\s*NATIONAL\s*BANK', re.IGNORECASE),
    'bob':       re.compile(r'BANK\s*OF\s*BARODA', re.IGNORECASE),
    'canara':    re.compile(r'CANARA\s*BANK', re.IGNORECASE),
    'dbs':       re.compile(r'\bDBS\s*BANK|\bDBS\s*TREASURES|\bDevelopment\s*Bank\s*of\s*Singapore', re.IGNORECASE),
    # European banks
    'deutsche':  re.compile(r'DEUTSCHE\s*BANK|DB\s*PRIVAT', re.IGNORECASE),
    'barclays':  re.compile(r'BARCLAYS', re.IGNORECASE),
    'hsbc':      re.compile(r'HSBC', re.IGNORECASE),
    'santander': re.compile(r'SANTANDER', re.IGNORECASE),
    'bnp':       re.compile(r'BNP\s*PARIBAS', re.IGNORECASE),
    'ing':       re.compile(r'\bING\s*BANK\b|\bING\s*DIBA\b', re.IGNORECASE),
    # US banks
    'chase':     re.compile(r'JPMORGAN\s*CHASE|CHASE\s*BANK', re.IGNORECASE),
    'bofa':      re.compile(r'BANK\s*OF\s*AMERICA', re.IGNORECASE),
    'wellsfargo':re.compile(r'WELLS\s*FARGO', re.IGNORECASE),
    'citi':      re.compile(r'CITIBANK|CITI\s*BANK', re.IGNORECASE),
    'usbank':    re.compile(r'U\.?S\.?\s*BANK', re.IGNORECASE),
    'capitalone':re.compile(r'CAPITAL\s*ONE', re.IGNORECASE),
    'amex':      re.compile(r'AMERICAN\s*EXPRESS|AMEX', re.IGNORECASE),
}

# Banks that use MM/DD/YYYY (month-first) date format
_MONTH_FIRST_BANKS = {'chase', 'bofa', 'wellsfargo', 'citi', 'usbank', 'capitalone', 'amex'}


# ── amount / date cleaning ──────────────────────────────────────────────────

def clean_amount(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s in ['-', 'nan', 'NaN', '']:
        return None
    s = ' '.join(s.split())          # collapse internal whitespace / newlines
    s = re.sub(r'[£$€₹¥₩₦₿\s]', '', s)
    s = re.sub(r'^[A-Za-z]+', '', s) # strip currency codes like S, SGD, INR, USD
    negative = False
    if s.startswith('(') and s.endswith(')'):
        negative = True
        s = s[1:-1]
    if s.startswith('-'):
        negative = True
        s = s[1:]
    # European format: 1.234,56  →  1234.56
    if re.match(r'^[\d\.]+,\d{2}$', s):
        s = s.replace('.', '').replace(',', '.')
    else:
        s = s.replace(',', '')
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None


def parse_date_val(val, dayfirst=True):
    if val is None:
        return None
    try:
        if isinstance(val, datetime):
            return val.date()
        if hasattr(val, 'date'):
            return val.date()
        s = str(val).strip()
        if not s or s in ['nan', 'NaN', '']:
            return None
        s = ' '.join(s.split())  # collapse newlines / multiple spaces from PDF cells
        # Handle German date format: DD.MM.YYYY
        if re.match(r'^\d{1,2}\.\d{1,2}\.\d{2,4}$', s):
            parts = s.split('.')
            s = f"{parts[0].zfill(2)}/{parts[1].zfill(2)}/{parts[2]}"
        return date_parser.parse(s, dayfirst=dayfirst).date()
    except Exception:
        return None


def _sniff_dayfirst(series):
    """
    Returns False (month-first, US style) when any date value has a
    month-position value > 12, meaning it must be the day — i.e. US format.
    Falls back to True (day-first, international) when ambiguous.
    """
    for val in series.dropna().head(20):
        s = str(val).strip()
        parts = re.split(r'[/\-\.]', s)
        if len(parts) >= 2:
            try:
                first, second = int(parts[0]), int(parts[1])
                if first > 12:
                    return True   # day is first → DD/MM
                if second > 12:
                    return False  # month is first → MM/DD
            except ValueError:
                pass
    return True  # default: international day-first


# ── column auto-detection ───────────────────────────────────────────────────

def _find_col(columns_lower_map, keywords):
    for kw in keywords:
        if kw in columns_lower_map:
            return columns_lower_map[kw]
    for kw in keywords:
        for col_lower, col in columns_lower_map.items():
            if kw in col_lower:
                return col
    return None


def detect_columns(df):
    clm = {c.lower().strip(): c for c in df.columns}
    has_debit = _find_col(clm, DEBIT_KEYS)
    return {
        'date':        _find_col(clm, DATE_KEYS),
        'description': _find_col(clm, DESC_KEYS),
        'debit':       has_debit,
        'credit':      _find_col(clm, CREDIT_KEYS),
        'amount':      _find_col(clm, AMOUNT_KEYS) if not has_debit else None,
        'type':        clm.get('type'),  # Chase "Type" column
    }


# ── file readers ────────────────────────────────────────────────────────────

def _read_csv(filepath):
    for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
        try:
            df = pd.read_csv(filepath, encoding=enc, skip_blank_lines=True, on_bad_lines='skip')
            df = df.dropna(how='all').reset_index(drop=True)
            if len(df.columns) >= 2:
                # Wells Fargo CSV has no headers — detect by checking if
                # first column header looks like a date (MM/DD/YYYY)
                first_col = str(df.columns[0]).strip()
                if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', first_col):
                    # Re-read without header and assign known Wells Fargo schema
                    df = pd.read_csv(filepath, encoding=enc, header=None,
                                     skip_blank_lines=True, on_bad_lines='skip')
                    df = df.dropna(how='all').reset_index(drop=True)
                    if len(df.columns) >= 5:
                        df.columns = ['Date', 'Amount', '_c2', '_c3', 'Description'] + \
                                     [f'_c{i}' for i in range(4, len(df.columns) - 1)]
                    else:
                        df.columns = ['Date', 'Amount', 'Description'] + \
                                     [f'_c{i}' for i in range(3, len(df.columns))]
                return df
        except Exception:
            continue
    raise ValueError("Could not read CSV file.")


def _read_excel(filepath):
    df = pd.read_excel(filepath, skip_blank_lines=True)
    return df.dropna(how='all').reset_index(drop=True)


# ── bank detection ──────────────────────────────────────────────────────────

def _detect_bank(pdf_text):
    for bank, pattern in _BANK_SIGNATURES.items():
        if pattern.search(pdf_text):
            return bank
    return 'unknown'


def _is_compressed_format(table):
    """
    Returns True if the table looks like HDFC/compressed format:
    all transactions on one mega-row with \\n-separated values per cell.
    """
    if not table or len(table) < 1:
        return False
    # Check first row for HDFC-style column names
    first = [str(c or '').lower() for c in table[0]]
    has_narr = any('narr' in c for c in first)
    has_withdrawal = any('withdrawal' in c or 'deposit' in c for c in first)
    if has_narr and has_withdrawal:
        return True
    # Also detect if the "header" is actually a data row with many \n (data-only pages)
    if len(table) == 1:
        first_cell = str(table[0][0] or '')
        dates_in_cell = [d for d in first_cell.split('\n') if _DATE_RE.match(d.strip())]
        if len(dates_in_cell) >= 3:
            return True
    return False


# ── HDFC / compressed-format PDF parser ────────────────────────────────────

def _split_narrations(narr_lines, n):
    groups, current = [], []
    for line in narr_lines:
        compressed = line.replace(' ', '')
        if _HDFC_TXN_START.match(compressed) and current:
            groups.append(' '.join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(' '.join(current))

    if len(groups) == n:
        return groups

    per = max(1, len(narr_lines) // n) if n else 1
    return [' '.join(narr_lines[i * per: (i + 1) * per]) for i in range(n)]


def _parse_compressed_pdf(filepath):
    """
    Handles PDFs where pdfplumber collapses every page into one mega-row
    with \\n-separated values per column (HDFC and similar formats).
    Uses closing-balance diffs as the authoritative transaction amount.
    """
    import pdfplumber

    all_dates, all_balances, all_narrations = [], [], []
    opening_balance = None

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ''
            m = re.search(r'Opening\s*Balance[^\d]*([\d,]+\.\d{2})', txt, re.IGNORECASE)
            if m:
                opening_balance = float(m.group(1).replace(',', ''))
                break

        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                if not table:
                    continue

                first_row = [str(c or '').strip() for c in table[0]]
                first_cell_lower = first_row[0].lower() if first_row else ''
                has_header = first_cell_lower == 'date'

                if has_header:
                    hdr = [c.lower().replace(' ', '').replace('.', '') for c in first_row]
                    date_idx = next((i for i, h in enumerate(hdr) if h.startswith('date')), 0)
                    narr_idx = next((i for i, h in enumerate(hdr) if 'narr' in h or 'desc' in h), 1)
                    bal_idx  = next((i for i, h in enumerate(hdr)
                                     if 'closing' in h or ('balance' in h and 'opening' not in h)),
                                    len(first_row) - 1)
                    data = table[1:]
                else:
                    date_idx, narr_idx, bal_idx = 0, 1, len(first_row) - 1
                    data = table

                for row in data:
                    if not row or len(row) <= max(date_idx, bal_idx):
                        continue

                    dates_raw = str(row[date_idx] or '')
                    narr_raw  = str(row[narr_idx] or '')
                    bal_raw   = str(row[bal_idx]  or '')

                    dates = [d.strip() for d in dates_raw.split('\n')
                             if _DATE_RE.match(d.strip())]
                    bals  = [b.strip().replace(',', '') for b in bal_raw.split('\n')
                             if _AMOUNT_RE.match(b.strip().replace(',', ''))]

                    if not dates or not bals:
                        continue

                    n = min(len(dates), len(bals))
                    narr_lines = [l.strip() for l in narr_raw.split('\n') if l.strip()]
                    narrations = _split_narrations(narr_lines, n)

                    for i in range(n):
                        all_dates.append(dates[i])
                        all_balances.append(float(bals[i]))
                        all_narrations.append(narrations[i] if i < len(narrations) else '')

    if not all_dates:
        return None

    rows = []
    prev = opening_balance
    for date, bal, narr in zip(all_dates, all_balances, all_narrations):
        if prev is not None:
            diff = round(bal - prev, 2)
            dr = '' if diff >= 0 else str(abs(diff))
            cr = str(diff) if diff > 0 else ''
        else:
            dr, cr = '', ''
        rows.append({'Date': date, 'Narration': narr,
                     'Withdrawal Amt.': dr, 'Deposit Amt.': cr})
        prev = bal

    return pd.DataFrame(rows)


# ── generic PDF parser (SBI / ICICI / Axis / Kotak / most banks) ───────────

def _looks_compressed(rows):
    """Return True if cells contain multiple newlines — pdfplumber merged rows into one."""
    for row in rows[:5]:
        for cell in (row or []):
            if cell and str(cell).count('\n') > 2:
                return True
    return False


def _extract_tables_from_pdf(filepath, table_settings=None):
    """Extract (all_rows, headers, n_cols) from all pages using given pdfplumber settings."""
    import pdfplumber
    all_rows = []
    headers  = None
    n_cols   = None
    kw = {'table_settings': table_settings} if table_settings else {}

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables(**kw) or []):
                if not table:
                    continue
                found_header = False
                for row_idx, row in enumerate(table[:6]):
                    if not row or len(row) <= 1:
                        continue
                    row_lower = [str(h or '').lower().strip() for h in row]
                    has_date = any('date' in h for h in row_lower)
                    has_val  = any(k in h for h in row_lower
                                   for k in ['amount', 'debit', 'credit',
                                             'withdrawal', 'deposit', 'dr', 'cr'])
                    if has_date and has_val:
                        if headers is None:
                            headers = row
                            n_cols  = len(row)
                        all_rows.extend(r for r in table[row_idx + 1:] if r and len(r) == n_cols)
                        found_header = True
                        break
                if not found_header and headers is not None:
                    all_rows.extend(r for r in table if r and len(r) == n_cols)

    return all_rows, headers, n_cols


def _read_pdf_generic(filepath):
    """
    Standard row-per-transaction PDF parser.
    Works for SBI, ICICI, Axis, Kotak, DBS, Yes Bank, IndusInd, and most
    international banks whose statements have one row per transaction.

    When the default extraction produces compressed mega-rows (whole page in one
    cell), retries with horizontal_strategy="text" so pdfplumber uses text
    positions for row boundaries instead of invisible grid lines.
    """
    all_rows, headers, n_cols = _extract_tables_from_pdf(filepath)

    # If cells contain many \n the PDF has no visible horizontal rules —
    # pdfplumber collapsed every page into one mega-row. Retry with text strategy.
    if all_rows and _looks_compressed(all_rows):
        alt_rows, alt_headers, alt_n_cols = _extract_tables_from_pdf(
            filepath,
            table_settings={"horizontal_strategy": "text", "vertical_strategy": "lines"}
        )
        if alt_rows and len(alt_rows) > len(all_rows):
            all_rows, headers, n_cols = alt_rows, alt_headers, alt_n_cols

    if all_rows and headers:
        df = pd.DataFrame(all_rows, columns=headers)
        return df.dropna(how='all').reset_index(drop=True)
    return None


# ── text-based fallback (last resort for any PDF) ──────────────────────────

def _parse_pdf_text(filepath):
    """
    Last-resort parser: extracts raw text from the PDF and uses regex to
    find date + amount patterns. Covers unusual PDF layouts.
    Works reasonably well for simple single-column statement layouts.
    """
    import pdfplumber

    # Matches: DD/MM/YY(YY)  or  DD-MM-YY(YY)  or  DD Mon YY(YY)
    DATE_PAT   = re.compile(
        r'\b(\d{2}[/\-]\d{2}[/\-]\d{2,4}|\d{2}\s+\w{3}\s+\d{2,4})\b'
    )
    AMOUNT_PAT = re.compile(r'([\d,]+\.\d{2})')

    all_text = ''
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            all_text += (page.extract_text() or '') + '\n'

    rows = []
    lines = all_text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        dm = DATE_PAT.match(line)
        if dm:
            # Collect up to 3 lines as description + amounts
            block = line
            for j in range(1, 4):
                if i + j < len(lines):
                    block += ' ' + lines[i + j].strip()
            amounts = AMOUNT_PAT.findall(block)
            if len(amounts) >= 2:
                # Last amount is usually the balance; second-to-last is the transaction
                txn_amt_str = amounts[-2].replace(',', '')
                bal_str     = amounts[-1].replace(',', '')
                try:
                    txn_amt = float(txn_amt_str)
                    desc = re.sub(r'\d[\d,]*\.\d{2}', '', block).strip()
                    desc = re.sub(r'\s+', ' ', desc)
                    rows.append({
                        'Date':       dm.group(1),
                        'Narration':  desc,
                        'Withdrawal Amt.': '',
                        'Deposit Amt.':    '',
                        '_raw_amount':     txn_amt,
                    })
                except ValueError:
                    pass
        i += 1

    if not rows:
        return None
    return pd.DataFrame(rows)


# ── main PDF dispatcher ─────────────────────────────────────────────────────

def _read_pdf(filepath):
    import pdfplumber

    # Detect bank from first-page text
    try:
        with pdfplumber.open(filepath) as pdf:
            first_text = pdf.pages[0].extract_text() or '' if pdf.pages else ''
            first_tables = pdf.pages[0].extract_tables() if pdf.pages else []
    except Exception as open_err:
        err_name = type(open_err).__name__
        if 'password' in err_name.lower() or 'password' in str(open_err).lower() or not str(open_err):
            raise ValueError(
                "This PDF is password-protected. "
                "DBS/POSB password is usually your date of birth (DDMMYYYY). "
                "ICICI password is usually your date of birth (DDMMYYYY). "
                "Open the PDF in Chrome or Adobe, enter the password, then print/save as a new PDF — "
                "or download the statement as CSV from your bank's internet banking."
            )
        raise ValueError(f"Could not open PDF: {open_err or err_name}")

    bank = _detect_bank(first_text)

    # Strategy 1 — compressed/HDFC-style format
    use_compressed = False
    if first_tables:
        use_compressed = _is_compressed_format(first_tables[0])

    if use_compressed or bank == 'hdfc':
        df = _parse_compressed_pdf(filepath)
        if df is not None and len(df) > 1:
            return df

    # Strategy 2 — standard row-per-transaction table (SBI, ICICI, Axis, Kotak…)
    df = _read_pdf_generic(filepath)
    if df is not None and len(df) > 1:
        return df

    # Strategy 3 — text-based extraction (last resort)
    df = _parse_pdf_text(filepath)
    if df is not None and len(df) > 1:
        return df

    raise ValueError(
        f"Could not extract transactions from this PDF "
        f"(detected bank: {bank}). "
        "Try downloading the statement as CSV or Excel from your bank's internet banking — "
        "that format imports perfectly for all banks."
    )


# ── public API ──────────────────────────────────────────────────────────────

def parse_bank_statement(filepath, column_mapping=None):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.csv':
        df = _read_csv(filepath)
    elif ext in ('.xlsx', '.xls'):
        df = _read_excel(filepath)
    elif ext == '.pdf':
        df = _read_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Use CSV, XLSX, or PDF.")

    mapping = column_mapping if column_mapping else detect_columns(df)
    return df, mapping


def get_column_preview(df, n=5):
    safe_df = df.head(n).fillna('').astype(str)
    return {'columns': list(df.columns), 'preview': safe_df.to_dict('records')}


# ── UPI payee extraction ──────────────────────────────────────────────────────

_VPA_RE = re.compile(
    r'\b([a-z0-9._\-]+@(?:oksbi|okaxis|okicici|okhdfcbank|ybl|ibl|axl|paytm|apl|'
    r'naviaxis|superyes|freecharge|indus|aubank|icici|hdfc|sbi|axis|kotak|yes|rbl|'
    r'idbi|dbs|jupiteraxis|niyoicici|[a-z]{2,12}))\b',
    re.IGNORECASE,
)

_BUSINESS_KW = {
    'shop', 'store', 'mart', 'market', 'works', 'enterprise', 'traders', 'pvt', 'ltd',
    'llp', 'inc', 'corp', 'agencies', 'services', 'solutions', 'tech', 'foods',
    'restaurant', 'cafe', 'hotel', 'hospital', 'clinic', 'pharmacy', 'medical',
    'school', 'college', 'industries', 'exports', 'imports', 'motors', 'garage',
    'petrol', 'station', 'super', 'discount', 'wholesale', 'retail', 'steel',
    'hardware', 'cement', 'paint', 'glass', 'telecom', 'broadband', 'fiber',
    'electronics', 'digital', 'computer', 'printing', 'studio', 'fashion',
    'boutique', 'salon', 'beauty', 'jewellers', 'opticals', 'gym', 'fitness',
    'cycle', 'bicycle', 'sports', 'auto', 'tyres', 'spares', 'bakery', 'sweets',
    'laundry', 'tailors', 'caterers', 'travels', 'tours', 'packaging', 'timber',
}


def extract_upi_payee(description):
    """Return (payee_key, payee_label) from a UPI transaction description.

    payee_key  — stable identifier for PayeeRule lookup (VPA or lowercased name)
    payee_label — human-readable display name
    Returns (None, None) when the description doesn't look like a UPI transaction.
    """
    desc = description or ''
    if not re.search(r'\bUPI\b|\bPAYNOW\b|\bFAST PAYMENT\b', desc, re.IGNORECASE):
        return None, None

    vpa_match = _VPA_RE.search(desc)
    vpa = vpa_match.group(1).lower() if vpa_match else None

    # Extract display name: "UPI/P2P/txnid/DisplayName/remark"
    name = None
    m = re.search(r'UPI/[A-Z0-9]+/\d+/([^/\d][^/]+?)(?:/|$)', desc, re.IGNORECASE)
    if m:
        name = m.group(1).strip()

    # Fallback: derive readable name from VPA local part
    if not name and vpa:
        local = vpa.split('@')[0]
        if not local.isdigit():
            name = local.replace('.', ' ').replace('_', ' ').replace('-', ' ').title()

    # Fallback: scan description segments
    if not name:
        for part in reversed(re.split(r'[/\-]', desc)):
            p = re.sub(r'\s*\d{8,}.*$', '', part).strip()
            if p and len(p) > 2 and p.upper() not in ('UPI', 'P2P', 'P2M', 'NEFT', 'IMPS', 'CR', 'DR'):
                name = p
                break

    key = vpa or (name.lower().strip() if name else None)
    label = name or vpa
    return key, label


def _is_personal_upi(vpa, name):
    """Return True when the payee looks like a private individual, not a merchant."""
    combined = ((name or '') + ' ' + (vpa or '')).lower()
    # Business keyword anywhere → not personal
    if any(kw in combined for kw in _BUSINESS_KW):
        return False
    # Phone-number VPA (e.g. 9876543210@okaxis) → personal
    if vpa:
        local = vpa.split('@')[0]
        if local.isdigit() and len(local) >= 8:
            return True
    # Two-word human name with no digits → personal
    if name:
        words = name.strip().split()
        if 1 <= len(words) <= 3 and all(re.match(r'^[A-Za-z\.]+$', w) for w in words):
            return True
    return False


# ── Keyword rules ─────────────────────────────────────────────────────────────

_KEYWORD_RULES = [
    ('Salary',        ['salary', 'payroll', 'wages', 'pay credit', 'employer',
                       'ctc', 'stipend', 'remuneration']),
    ('Investment',    ['dividend', 'mutual fund', 'mf', 'sip', 'stock', 'equity',
                       'fd interest', 'interest credit', 'birla', 'motilal', 'dsp',
                       'sbi mf', 'hdfc mf', 'icici pru', 'nippon', 'mirae', 'tata mf',
                       'redemption', 'cams', 'kfin', 'nsdl', 'cdsl', 'demat',
                       'zerodha', 'groww', 'upstox', 'smallcase', 'icicidirect',
                       'hdfcsec', 'kotak sec', 'sbicap']),
    ('Food & Dining', ['restaurant', 'cafe', 'coffee', 'swiggy', 'zomato', 'uber eats',
                       'dominos', 'pizza', 'burger', 'food', 'bakers', 'a2b',
                       'dhaba', 'biryani', 'bakery', 'dairy', 'milk', 'grocery',
                       'bigbasket', 'dunzo', 'zepto', 'instamart', 'fresh',
                       'kfc', 'mcdonalds', 'subway', 'starbucks', 'chai', 'tata tea']),
    ('Shopping',      ['amazon', 'flipkart', 'walmart', 'target', 'myntra', 'meesho',
                       'nykaa', 'shop', 'mart', 'store', 'blinkit', 'steam',
                       'ajio', 'tatacliq', 'snapdeal', 'reliance', 'dmart', 'more',
                       'lifestyle', 'shoppers stop', 'westside', 'h&m', 'zara',
                       'decathlon', 'croma', 'vijay sales', 'poorvika',
                       'cycle', 'bicycle', 'sports', 'hardware', 'tyres', 'spares']),
    ('Transport',     ['uber', 'ola', 'rapido', 'lyft', 'taxi', 'metro', 'train',
                       'petrol', 'fuel', 'diesel', 'parking', 'toll', 'irctc',
                       'railway', 'bus', 'redbus', 'fasttag', 'bmtc', 'best bus',
                       'ksrtc', 'hrtc', 'indigo', 'air india', 'spicejet', 'vistara',
                       'akasa', 'aviation', 'cab', 'auto', 'motors', 'garage']),
    ('Utilities',     ['electricity', 'water bill', 'gas bill', 'internet', 'broadband',
                       'mobile recharge', 'dth', 'bill payment', 'bescom', 'bsnl',
                       'airtel', 'jiofiber', 'myjio', 'tata power', 'adani electric',
                       'torrent power', 'mseb', 'tneb', 'kseb', 'cesc', 'bijli',
                       'bbmp', 'nmc', 'ghmc', 'paytm bills', 'phonepe bills']),
    ('Healthcare',    ['hospital', 'clinic', 'pharmacy', 'medical', 'doctor', 'health',
                       'apollo', 'medplus', 'dr ', 'fortis', '1mg', 'pharmeasy',
                       'netmeds', 'manipal', 'aiims', 'diagnostic', 'lab', 'scan',
                       'dental', 'optician', 'ayurveda', 'practo', 'healthians']),
    ('Entertainment', ['netflix', 'spotify', 'prime video', 'hotstar', 'cinema', 'movie',
                       'game', 'bookmyshow', 'pvr', 'inox', 'disney', 'zee5',
                       'sonyliv', 'jiocinema', 'mxplayer', 'hungama', 'gaana', 'wynk',
                       'youtube premium', 'playstation', 'xbox', 'ea games', 'epic games']),
    ('Rent',          ['rent', 'lease', 'pg ', 'hostel', 'maintenance',
                       'society', 'housing', 'flat', 'apartment']),
    ('Education',     ['school', 'college', 'university', 'course', 'tuition',
                       'udemy', 'coursera', 'fees', 'edtech', 'byjus', 'unacademy',
                       'vedantu', 'whitehat', 'simplilearn', 'exam', 'coaching']),
    ('Insurance',     ['insurance', 'lic', 'policy', 'ipruin', 'star health', 'niva bupa',
                       'hdfc life', 'icici lombard', 'bajaj allianz', 'sbi life',
                       'max life', 'term plan', 'mediclaim']),
    ('EMI / Loan',    ['emi', 'loan', 'mortgage', 'equated', 'gold loan', 'principal',
                       'home loan', 'car loan', 'personal loan', 'credit card bill',
                       'repayment', 'instalment', 'tata capital', 'bajaj finance',
                       'hdfc credila', 'muthoot', 'manappuram']),
    ('Travel',        ['resort', 'oyo', 'makemytrip', 'goibibo', 'yatra', 'cleartrip',
                       'ixigo', 'airbnb', 'booking.com', 'agoda', 'forex', 'thomas cook',
                       'expedia', 'kayak', 'trivago', 'marriott', 'hilton', 'hyatt',
                       'emirates', 'lufthansa', 'delta', 'united airlines',
                       'british airways', 'singapore airlines']),
    ('Personal Transfer', ['self transfer']),
    ('Bank Transfer', ['neft cr', 'neft dr', 'imps', 'rtgs', 'ib funds transfer',
                       'funds transfer', 'sepa', 'wire transfer', 'zelle', 'venmo', 'paypal']),
    ('Groceries',     ['whole foods', 'trader joe', 'kroger', 'safeway', 'costco',
                       'walmart grocery', 'target grocery', 'aldi', 'lidl', 'rewe',
                       'edeka', 'penny', 'netto', 'kaufland', 'dm markt']),
]


def _keyword_match(text):
    t = text.lower()
    for category, keywords in _KEYWORD_RULES:
        if any(k in t for k in keywords):
            return category
    return 'Uncategorized'


def auto_categorize(description):
    """Categorize a transaction description.

    UPI transactions are handled specially:
    - If the payee looks like a private individual → 'Personal Transfer'
    - Otherwise keyword-match on the extracted merchant name first, then fall
      through to full-description keyword matching.
    """
    desc = description or ''

    payee_key, payee_label = extract_upi_payee(desc)
    if payee_key is not None:
        # It is a UPI transaction — decide based on the payee identity
        if _is_personal_upi(payee_key, payee_label):
            return 'Personal Transfer'
        # Known merchant keywords in the payee name take priority
        if payee_label:
            cat = _keyword_match(payee_label)
            if cat != 'Uncategorized':
                return cat

    # Full-description keyword pass (covers non-UPI and UPI with no payee extracted)
    return _keyword_match(desc)


_VALID_CATEGORIES = {
    'Salary', 'Investment', 'Food & Dining', 'Shopping', 'Transport',
    'Utilities', 'Healthcare', 'Entertainment', 'Rent', 'Education',
    'Insurance', 'EMI / Loan', 'Travel', 'Groceries',
    'Personal Transfer', 'Bank Transfer', 'Uncategorized',
}


def llm_categorize_batch(descriptions, api_key):
    """Send a batch of uncategorized transaction descriptions to Claude for classification.

    Returns a list of category strings in the same order as `descriptions`.
    Falls back to 'Uncategorized' silently on any error so imports never fail.
    """
    if not descriptions or not api_key:
        return ['Uncategorized'] * len(descriptions)

    try:
        import anthropic, json as _json

        numbered = '\n'.join(f'{i + 1}. {d}' for i, d in enumerate(descriptions))
        cats_str = ', '.join(sorted(_VALID_CATEGORIES - {'Uncategorized'}))

        prompt = (
            f"You are a bank transaction categorizer for Indian and international banks.\n"
            f"Classify each transaction into exactly one of these categories:\n"
            f"{cats_str}, Uncategorized\n\n"
            f"Rules:\n"
            f"- UPI/FAST/PayNow to a person name with no merchant context → Personal Transfer\n"
            f"- NEFT/IMPS/RTGS/wire between bank accounts → Bank Transfer\n"
            f"- SIP/mutual fund/stock purchase → Investment\n"
            f"- If genuinely unclear → Uncategorized\n"
            f"- Reply with ONLY a JSON array of strings, one per line item, same order, no extra text.\n\n"
            f"Transactions:\n{numbered}"
        )

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = msg.content[0].text.strip()
        start, end = text.index('['), text.rindex(']') + 1
        result = _json.loads(text[start:end])
        if len(result) != len(descriptions):
            return ['Uncategorized'] * len(descriptions)
        return [r if r in _VALID_CATEGORIES else 'Uncategorized' for r in result]
    except Exception:
        return ['Uncategorized'] * len(descriptions)


def process_transactions(df, column_mapping):
    date_col   = column_mapping.get('date')
    desc_col   = column_mapping.get('description')
    debit_col  = column_mapping.get('debit')
    credit_col = column_mapping.get('credit')
    amount_col = column_mapping.get('amount')
    type_col   = column_mapping.get('type')  # Chase "Type" column: DEBIT / CREDIT

    if not date_col:
        raise ValueError("Date column not identified. Please map it manually.")

    # Detect date format once for the whole file
    dayfirst = _sniff_dayfirst(df[date_col])

    results = []
    for _, row in df.iterrows():
        date = parse_date_val(row.get(date_col), dayfirst=dayfirst)
        if not date:
            continue

        description = str(row.get(desc_col, '') if desc_col else '').strip()

        amount = None
        if debit_col and credit_col:
            debit  = clean_amount(row.get(debit_col))
            credit = clean_amount(row.get(credit_col))
            if credit and credit != 0:
                amount = abs(credit)
            elif debit and debit != 0:
                amount = -abs(debit)
        elif '_raw_amount' in df.columns:
            amount = clean_amount(row.get('_raw_amount'))
        elif amount_col:
            amount = clean_amount(row.get(amount_col))
            # Chase CSV: amount is already signed (negative = debit)
            # Type column says "Sale" / "Return" — we keep the sign as-is
            if amount and type_col:
                txn_type = str(row.get(type_col, '')).lower()
                if txn_type in ('sale', 'debit') and amount > 0:
                    amount = -amount
                elif txn_type in ('return', 'credit', 'payment') and amount < 0:
                    amount = abs(amount)

        if amount is None or amount == 0:
            continue

        results.append({
            'date':             date,
            'description':      description,
            'amount':           amount,
            'category':         auto_categorize(description),
            'transaction_type': 'income' if amount > 0 else 'expense',
        })

    return results
