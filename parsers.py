"""
Parsing logic for receipts, SMS (UzCard/Humo), and CSV bank exports.
Pure functions — no Flask dependencies. Takes strings/bytes, returns dicts.
"""
import re
import csv
import io
from datetime import datetime, date


# ── Store-to-Category Map ─────────────────────────────────────
# Flat keyword lookup. Covers ~90% of Uzbek retail.

STORE_CATEGORY_MAP = {
    # Food / Grocery
    'korzinka': 'Food', 'makro': 'Food', 'macro': 'Food',
    'havas': 'Food', 'carrefour': 'Food', 'magnum': 'Food',
    'magnit': 'Food', 'oqtepa': 'Food', 'evos': 'Food',
    'burger': 'Food', 'restaurant': 'Food', 'restoran': 'Food',
    'cafe': 'Food', 'coffee': 'Food', 'kofe': 'Food',
    'stolovaya': 'Food', 'oshxona': 'Food', 'lavash': 'Food',
    'choyxona': 'Food', 'bazar': 'Food', 'supermarket': 'Food',
    'minimarket': 'Food', 'produkti': 'Food', 'bakkaleja': 'Food',
    'non': 'Food', 'go\'sht': 'Food', 'meva': 'Food',

    # Transport
    'yandex go': 'Transport', 'yandex taxi': 'Transport',
    'uber': 'Transport', 'mycar': 'Transport',
    'uzairways': 'Transport', 'avto': 'Transport',
    'benzin': 'Transport', 'toplivo': 'Transport',
    'zapravka': 'Transport', 'gaz station': 'Transport',
    'metro': 'Transport', 'taksi': 'Transport',

    # Utilities / Telecom
    'beeline': 'Utilities', 'ucell': 'Utilities',
    'mobiuz': 'Utilities', 'uzmobile': 'Utilities',
    'turon telecom': 'Utilities', 'uztelecom': 'Utilities',
    'elektr': 'Utilities', 'kommunal': 'Utilities',
    'issiqlik': 'Utilities', 'suv': 'Utilities',
    'internet': 'Utilities', 'suvokava': 'Utilities',

    # Shopping
    'mediapark': 'Shopping', 'texnomart': 'Shopping',
    'zara': 'Shopping', 'lcwaikiki': 'Shopping',
    'samsung': 'Shopping', 'apple': 'Shopping',
    'kiyim': 'Shopping', 'poyabzal': 'Shopping',
    'mebel': 'Shopping', 'bozor': 'Shopping',

    # Health
    'apteka': 'Health', 'dorixona': 'Health',
    'pharmacy': 'Health', 'klinika': 'Health',
    'hospital': 'Health', 'poliklinika': 'Health',
    'stomatolog': 'Health', 'labaratoriya': 'Health',

    # Entertainment
    'kinoteatr': 'Entertainment', 'cinema': 'Entertainment',
    'magic city': 'Entertainment', 'aquapark': 'Entertainment',
    'park': 'Entertainment', 'konsert': 'Entertainment',

    # Education
    'kitob': 'Education', 'book': 'Education',
    'kurs': 'Education', 'talim': 'Education',
    'universitet': 'Education', 'maktab': 'Education',
    'repetitor': 'Education',

    # Housing
    'ijara': 'Housing', 'arenda': 'Housing', 'kvartira': 'Housing',
}


def guess_category(merchant_name):
    """Match merchant name against known stores. Returns category or 'Other'."""
    if not merchant_name:
        return 'Other'
    name_lower = merchant_name.lower().strip()
    for keyword, category in STORE_CATEGORY_MAP.items():
        if keyword in name_lower:
            return category
    return 'Other'


# ── Receipt OCR Parser ────────────────────────────────────────

def parse_receipt_text(ocr_text):
    """
    Parse OCR-extracted receipt text.
    Returns dict: amount, date, merchant, category, currency, raw_text
    """
    lines = ocr_text.strip().split('\n')
    result = {
        'amount': None,
        'date': None,
        'merchant': None,
        'category': 'Other',
        'currency': 'UZS',
        'raw_text': ocr_text
    }

    # --- Amount: look for total keywords from bottom ---
    total_pattern = re.compile(
        r'(?:JAMI|ИТОГО|ИТОГ|TOTAL|ЖАМИ|HAMMASI|ВСЕГО)\s*[:=]?\s*([\d\s.,]+)',
        re.IGNORECASE
    )
    for line in reversed(lines):
        m = total_pattern.search(line)
        if m:
            amount_str = m.group(1).replace(' ', '').replace(',', '.')
            # Uzbek format: 150.000 means 150,000 (dot as thousands sep)
            if re.match(r'^\d+\.\d{3}$', amount_str):
                amount_str = amount_str.replace('.', '')
            # Handle multiple dots like 1.500.000
            parts = amount_str.split('.')
            if len(parts) > 2:
                amount_str = ''.join(parts)
            try:
                result['amount'] = float(amount_str)
                break
            except ValueError:
                continue

    # Fallback: largest number in receipt
    if result['amount'] is None:
        numbers = []
        for line in lines:
            for match in re.finditer(r'([\d\s]{1,15}[.,]\d{2})', line):
                num_str = match.group(1).replace(' ', '').replace(',', '.')
                try:
                    numbers.append(float(num_str))
                except ValueError:
                    pass
        if numbers:
            result['amount'] = max(numbers)

    # --- Date ---
    date_patterns = [
        (r'(\d{2})[./](\d{2})[./](\d{4})', '%d.%m.%Y'),
        (r'(\d{2})[./](\d{2})[./](\d{2})\b', '%d.%m.%y'),
        (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),
    ]
    for line in lines:
        for pattern, fmt in date_patterns:
            m = re.search(pattern, line)
            if m:
                try:
                    raw = m.group(0).replace('/', '.')
                    result['date'] = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
        if result['date']:
            break

    # --- Merchant: first non-numeric line in first 5 lines ---
    skip_words = {'chek', 'check', 'kvitantsiya', 'receipt', 'inn', 'qqs', 'stir'}
    for line in lines[:5]:
        cleaned = line.strip()
        if cleaned and len(cleaned) > 2:
            if not re.match(r'^[\d\s.,:\-/]+$', cleaned):
                if not any(w in cleaned.lower() for w in skip_words):
                    result['merchant'] = cleaned
                    break

    # --- Category from merchant ---
    if result['merchant']:
        result['category'] = guess_category(result['merchant'])

    # --- Currency detection ---
    text_upper = ocr_text.upper()
    if 'USD' in text_upper or '$' in ocr_text:
        result['currency'] = 'USD'

    return result


# ── SMS Parsers ───────────────────────────────────────────────

def parse_sms_uzcard(text):
    """
    Parse UzCard SMS.
    Example: "Karta *1234: -150,000.00 UZS. Korzinka. 12.02.2026 14:30. Balans: 3,500,000.00 UZS"
    """
    result = {
        'amount': None, 'merchant': None, 'date': None,
        'card': None, 'category': 'Other', 'currency': 'UZS',
        'type': 'expense', 'description': '', 'raw': text
    }

    # Card number
    card_match = re.search(r'[Kk]arta\s*\*(\d{4})', text)
    if card_match:
        result['card'] = '*' + card_match.group(1)

    # Amount
    amt_match = re.search(r'[-+]?([\d\s,]+\.\d{2})\s*(?:UZS|сум)', text, re.IGNORECASE)
    if amt_match:
        amt_str = amt_match.group(1).replace(',', '').replace(' ', '')
        result['amount'] = float(amt_str)
        # Check if income (+ or popolnenie)
        prefix = text[:amt_match.start()]
        if '+' in prefix or 'popolnenie' in text.lower() or 'zachislenie' in text.lower():
            result['type'] = 'income'

    # Merchant: text segment that isn't amount, card, or balance
    parts = re.split(r'[.;]\s*', text)
    for part in parts:
        part = part.strip()
        if not part or len(part) < 2:
            continue
        if re.search(r'UZS|сум|Karta|karta|Balans|balans|\d{2}[./]\d{2}[./]\d{2,4}', part, re.IGNORECASE):
            continue
        if re.match(r'^[\d\s,.:+\-]+$', part):
            continue
        result['merchant'] = part
        result['description'] = part
        break

    # Date
    date_match = re.search(r'(\d{2})[./](\d{2})[./](\d{4})', text)
    if date_match:
        try:
            result['date'] = datetime.strptime(date_match.group(0).replace('/', '.'), '%d.%m.%Y').date()
        except ValueError:
            pass

    if result['merchant']:
        result['category'] = guess_category(result['merchant'])

    return result


def parse_sms_humo(text):
    """
    Parse Humo SMS.
    Example: "HUMO *5678: Spisanie 250,000 UZS. Macro. 12/02/2026. Ost: 1,200,000 UZS"
    """
    result = {
        'amount': None, 'merchant': None, 'date': None,
        'card': None, 'category': 'Other', 'currency': 'UZS',
        'type': 'expense', 'description': '', 'raw': text
    }

    # Card
    card_match = re.search(r'HUMO\s*\*(\d{4})', text, re.IGNORECASE)
    if card_match:
        result['card'] = 'HUMO *' + card_match.group(1)

    # Type
    if re.search(r'popolnenie|zachislenie|kirim', text, re.IGNORECASE):
        result['type'] = 'income'

    # Amount
    amt_match = re.search(
        r'(?:Spisanie|Popolnenie|Zachislenie|Oplata|Chiqim|Kirim)\s+([\d\s,]+(?:\.\d{2})?)\s*(?:UZS|сум)',
        text, re.IGNORECASE
    )
    if not amt_match:
        # Fallback: first number followed by UZS
        amt_match = re.search(r'([\d\s,]+(?:\.\d{2})?)\s*(?:UZS|сум)', text, re.IGNORECASE)
    if amt_match:
        amt_str = amt_match.group(1).replace(',', '').replace(' ', '')
        try:
            result['amount'] = float(amt_str)
        except ValueError:
            pass

    # Merchant
    parts = re.split(r'[.;]\s*', text)
    for part in parts:
        part = part.strip()
        if not part or len(part) < 2:
            continue
        if re.search(
            r'UZS|сум|HUMO|humo|Ost|ost|Spisanie|Popolnenie|Zachislenie|Oplata|Chiqim|Kirim|\d{2}[./]\d{2}[./]\d{2,4}',
            part, re.IGNORECASE
        ):
            continue
        if re.match(r'^[\d\s,.:+\-]+$', part):
            continue
        result['merchant'] = part
        result['description'] = part
        break

    # Date
    date_match = re.search(r'(\d{2})[/.](\d{2})[/.](\d{4})', text)
    if date_match:
        try:
            result['date'] = datetime.strptime(
                date_match.group(0).replace('/', '.'), '%d.%m.%Y'
            ).date()
        except ValueError:
            pass

    if result['merchant']:
        result['category'] = guess_category(result['merchant'])

    return result


def parse_sms_bulk(text_block):
    """
    Parse multiple SMS messages pasted together.
    Split by blank lines or by message start markers.
    Returns list of parsed dicts.
    """
    if not text_block or not text_block.strip():
        return []

    results = []

    # Split by blank lines first
    chunks = re.split(r'\n\s*\n', text_block.strip())

    # If only one chunk, try splitting by message start markers
    if len(chunks) == 1:
        chunks = re.split(r'(?=(?:Karta|HUMO)\s*\*\d{4})', text_block.strip(), flags=re.IGNORECASE)
        chunks = [c.strip() for c in chunks if c.strip()]

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if 'humo' in chunk.lower():
            parsed = parse_sms_humo(chunk)
        elif 'karta' in chunk.lower():
            parsed = parse_sms_uzcard(chunk)
        else:
            # Try generic: look for UZS amount
            parsed = parse_sms_uzcard(chunk)
        if parsed.get('amount'):
            results.append(parsed)

    return results


# ── CSV Parser ────────────────────────────────────────────────

def parse_csv(file_content, encoding='utf-8'):
    """
    Parse bank CSV export. Auto-detect columns.
    Returns list of dicts: amount, date, description, type, category, currency.
    """
    if isinstance(file_content, bytes):
        # Try utf-8 first, fall back to cp1251 (common for Russian exports)
        try:
            text = file_content.decode('utf-8')
        except UnicodeDecodeError:
            text = file_content.decode('cp1251')
    else:
        text = file_content

    # Detect delimiter
    first_line = text.split('\n')[0]
    delimiter = ';' if ';' in first_line else ','

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)

    if len(rows) < 2:
        return []

    header = [h.strip().lower() for h in rows[0]]

    # Auto-detect column indices
    col_map = {}
    for i, h in enumerate(header):
        if h in ('date', 'дата', 'sana', 'transaction date', 'дата операции'):
            col_map['date'] = i
        elif h in ('amount', 'сумма', 'summa', 'miqdor', 'sum', 'сумма операции'):
            col_map['amount'] = i
        elif h in ('description', 'описание', 'tavsif', 'details', 'merchant', 'назначение', 'наименование'):
            col_map['description'] = i
        elif h in ('credit', 'кредит', 'kirim', 'приход'):
            col_map['credit'] = i
        elif h in ('debit', 'дебет', 'chiqim', 'расход'):
            col_map['debit'] = i
        elif h in ('currency', 'валюта', 'valyuta'):
            col_map['currency'] = i

    results = []
    for row in rows[1:]:
        if not row or all(cell.strip() == '' for cell in row):
            continue

        entry = {
            'amount': None, 'date': None, 'description': '',
            'merchant': None, 'type': 'expense', 'category': 'Other', 'currency': 'UZS'
        }

        # Amount
        if 'amount' in col_map and col_map['amount'] < len(row):
            amt_str = row[col_map['amount']].replace(',', '').replace(' ', '').strip()
            try:
                amt = float(amt_str)
                entry['type'] = 'income' if amt > 0 else 'expense'
                entry['amount'] = abs(amt)
            except ValueError:
                continue
        elif 'credit' in col_map and 'debit' in col_map:
            credit_str = row[col_map['credit']].replace(',', '').replace(' ', '').strip() if col_map['credit'] < len(row) else ''
            debit_str = row[col_map['debit']].replace(',', '').replace(' ', '').strip() if col_map['debit'] < len(row) else ''
            try:
                credit = float(credit_str) if credit_str else 0
                debit = float(debit_str) if debit_str else 0
            except ValueError:
                continue
            if credit > 0:
                entry['type'] = 'income'
                entry['amount'] = credit
            elif debit > 0:
                entry['type'] = 'expense'
                entry['amount'] = debit
            else:
                continue

        # Date
        if 'date' in col_map and col_map['date'] < len(row):
            date_str = row[col_map['date']].strip()
            for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
                try:
                    entry['date'] = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue

        # Description
        if 'description' in col_map and col_map['description'] < len(row):
            entry['description'] = row[col_map['description']].strip()
            entry['merchant'] = entry['description']
            entry['category'] = guess_category(entry['description'])

        # Currency
        if 'currency' in col_map and col_map['currency'] < len(row):
            curr = row[col_map['currency']].strip().upper()
            if curr in ('USD', 'UZS'):
                entry['currency'] = curr

        if entry['amount']:
            results.append(entry)

    return results
