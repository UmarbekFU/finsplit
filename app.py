from flask import Flask, render_template, request, redirect, url_for, flash
from models import (
    db, Transaction, Budget, Group, GroupMember,
    SplitExpense, SplitShare, Investment, FixedPayment, Trip,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, INVESTMENT_TYPES,
    CURRENCIES, CURRENCY_SYMBOLS, FIXED_PAYMENT_FREQUENCIES
)
from parsers import parse_receipt_text, parse_sms_bulk, parse_csv
from datetime import date, datetime
from calendar import monthrange
from collections import defaultdict
import pytesseract
from PIL import Image
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///finsplit.db'
app.config['SECRET_KEY'] = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
db.init_app(app)

with app.app_context():
    db.create_all()
    # Migrate: add currency column if missing
    try:
        db.session.execute(db.text("ALTER TABLE transaction ADD COLUMN currency VARCHAR(3) DEFAULT 'USD'"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Migrate: add monthly_contribution to investment
    try:
        db.session.execute(db.text("ALTER TABLE investment ADD COLUMN monthly_contribution FLOAT DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── Helpers ───────────────────────────────────────────────────

@app.template_filter('money')
def money_filter(amount, currency='USD'):
    """Format amount with currency. UZS: no decimals, commas. USD: $xx.xx."""
    if currency == 'UZS':
        return f'{amount:,.0f} UZS'
    return f'${amount:,.2f}'

def current_month():
    return date.today().strftime('%Y-%m')

def parse_date(s):
    return datetime.strptime(s, '%Y-%m-%d').date() if s else date.today()


def calculate_daily_allowance(month=None):
    """The core number: how much can you spend today?"""
    today = date.today()
    if month is None:
        month = current_month()
    year, mo = int(month[:4]), int(month[5:7])
    days_in_month = monthrange(year, mo)[1]

    # If viewing current month, days remaining from today
    # If viewing past/future month, show full month average
    is_current = (year == today.year and mo == today.month)
    days_remaining = max(1, days_in_month - today.day + 1) if is_current else days_in_month

    # 1. Monthly income
    txns = Transaction.query.filter(
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).all()
    income = sum(t.amount for t in txns if t.type == 'income')
    already_spent = sum(t.amount for t in txns if t.type == 'expense')

    # 2. Fixed payments (convert to monthly equivalent)
    fixed = FixedPayment.query.filter_by(is_active=True).all()
    fixed_total = 0.0
    for f in fixed:
        if f.frequency == 'monthly':
            fixed_total += f.amount
        elif f.frequency == 'weekly':
            fixed_total += f.amount * 52 / 12
        elif f.frequency == 'yearly':
            fixed_total += f.amount / 12

    # 3. Investment contributions
    investments = Investment.query.all()
    invest_contributions = sum(i.monthly_contribution or 0 for i in investments)

    # 4. Calculate
    remaining = income - fixed_total - invest_contributions - already_spent
    daily_allowance = remaining / days_remaining

    # Status
    daily_baseline = income / 30 if income > 0 else 0
    if daily_allowance <= 0:
        status = 'over'
    elif daily_allowance < daily_baseline * 0.2:
        status = 'tight'
    else:
        status = 'comfortable'

    return {
        'monthly_income': income,
        'fixed_payments_total': fixed_total,
        'investment_contributions': invest_contributions,
        'already_spent': already_spent,
        'remaining': remaining,
        'days_remaining': days_remaining,
        'daily_allowance': daily_allowance,
        'status': status,
    }


# ── Dashboard ─────────────────────────────────────────────────

@app.route('/')
def dashboard():
    month = request.args.get('month', current_month())
    year, mo = int(month[:4]), int(month[5:7])

    txns = Transaction.query.filter(
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).order_by(Transaction.date.desc()).all()

    income = sum(t.amount for t in txns if t.type == 'income')
    expenses = sum(t.amount for t in txns if t.type == 'expense')
    savings = income - expenses
    savings_rate = (savings / income * 100) if income > 0 else 0

    # daily allowance
    allowance = calculate_daily_allowance(month)

    # budget status
    budgets = Budget.query.filter_by(month=month).all()
    budget_status = []
    for b in budgets:
        spent = sum(
            t.amount for t in txns
            if t.type == 'expense' and t.category == b.category
        )
        pct = (spent / b.monthly_limit * 100) if b.monthly_limit > 0 else 0
        budget_status.append({
            'category': b.category,
            'limit': b.monthly_limit,
            'spent': spent,
            'pct': min(pct, 100),
            'over': spent > b.monthly_limit
        })

    # portfolio summary
    holdings = Investment.query.all()
    portfolio_value = sum(i.current_price * i.quantity for i in holdings)
    portfolio_cost = sum(i.buy_price * i.quantity for i in holdings)
    portfolio_gain = portfolio_value - portfolio_cost

    recent = txns[:10]

    return render_template('dashboard.html',
        month=month, income=income, expenses=expenses,
        savings=savings, savings_rate=savings_rate,
        allowance=allowance,
        budget_status=budget_status,
        portfolio_value=portfolio_value, portfolio_gain=portfolio_gain,
        recent=recent
    )


# ── Transactions ──────────────────────────────────────────────

@app.route('/transactions')
def transactions():
    type_filter = request.args.get('type', '')
    cat_filter = request.args.get('category', '')

    q = Transaction.query
    if type_filter:
        q = q.filter_by(type=type_filter)
    if cat_filter:
        q = q.filter_by(category=cat_filter)

    txns = q.order_by(Transaction.date.desc()).all()
    total = sum(t.amount if t.type == 'income' else -t.amount for t in txns)

    return render_template('transactions.html',
        transactions=txns, total=total,
        expense_categories=EXPENSE_CATEGORIES,
        income_categories=INCOME_CATEGORIES,
        type_filter=type_filter, cat_filter=cat_filter
    )

@app.route('/transactions/add', methods=['POST'])
def add_transaction():
    t = Transaction(
        type=request.form['type'],
        amount=float(request.form['amount']),
        currency=request.form.get('currency', 'USD'),
        category=request.form['category'],
        description=request.form.get('description', ''),
        date=parse_date(request.form.get('date'))
    )
    db.session.add(t)
    db.session.commit()
    sym = CURRENCY_SYMBOLS.get(t.currency, '$')
    flash(f'{"Income" if t.type == "income" else "Expense"} added: {sym}{t.amount:,.0f}' if t.currency == 'UZS'
          else f'{"Income" if t.type == "income" else "Expense"} added: ${t.amount:.2f}', 'success')
    return redirect(url_for('transactions'))

@app.route('/transactions/delete/<int:id>')
def delete_transaction(id):
    t = Transaction.query.get_or_404(id)
    db.session.delete(t)
    db.session.commit()
    flash('Transaction deleted', 'info')
    return redirect(url_for('transactions'))


# ── Budgets ───────────────────────────────────────────────────

@app.route('/budgets')
def budgets():
    month = request.args.get('month', current_month())
    year, mo = int(month[:4]), int(month[5:7])

    budget_list = Budget.query.filter_by(month=month).all()

    # actual spending per category
    expense_txns = Transaction.query.filter(
        Transaction.type == 'expense',
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).all()

    spending = defaultdict(float)
    for t in expense_txns:
        spending[t.category] += t.amount

    budget_data = []
    for b in budget_list:
        spent = spending.get(b.category, 0)
        pct = (spent / b.monthly_limit * 100) if b.monthly_limit > 0 else 0
        budget_data.append({
            'id': b.id,
            'category': b.category,
            'limit': b.monthly_limit,
            'spent': spent,
            'remaining': b.monthly_limit - spent,
            'pct': min(pct, 100),
            'over': spent > b.monthly_limit
        })

    # unbudgeted spending
    budgeted_cats = {b.category for b in budget_list}
    unbudgeted = {cat: amt for cat, amt in spending.items() if cat not in budgeted_cats}

    return render_template('budgets.html',
        month=month, budgets=budget_data, unbudgeted=unbudgeted,
        categories=EXPENSE_CATEGORIES
    )

@app.route('/budgets/add', methods=['POST'])
def add_budget():
    month = request.form.get('month', current_month())
    existing = Budget.query.filter_by(
        category=request.form['category'], month=month
    ).first()

    if existing:
        existing.monthly_limit = float(request.form['amount'])
    else:
        b = Budget(
            category=request.form['category'],
            monthly_limit=float(request.form['amount']),
            month=month
        )
        db.session.add(b)

    db.session.commit()
    flash('Budget saved', 'success')
    return redirect(url_for('budgets', month=month))

@app.route('/budgets/delete/<int:id>')
def delete_budget(id):
    b = Budget.query.get_or_404(id)
    month = b.month
    db.session.delete(b)
    db.session.commit()
    flash('Budget removed', 'info')
    return redirect(url_for('budgets', month=month))


# ── Expense Splitting ─────────────────────────────────────────

@app.route('/split')
def split():
    groups = Group.query.order_by(Group.created_at.desc()).all()
    return render_template('split.html', groups=groups, active_group=None)

@app.route('/split/<int:group_id>')
def split_group(group_id):
    groups = Group.query.order_by(Group.created_at.desc()).all()
    group = Group.query.get_or_404(group_id)
    expenses = SplitExpense.query.filter_by(group_id=group_id).order_by(SplitExpense.date.desc()).all()

    # calculate balances: positive = owed money, negative = owes money
    balances = defaultdict(float)
    for exp in expenses:
        if exp.settled:
            continue
        balances[exp.paid_by] += exp.amount
        for share in exp.shares:
            balances[share.member_id] -= share.share_amount

    # build member name map
    member_map = {m.id: m.name for m in group.members}

    # simplify debts
    debts = simplify_debts(balances, member_map)

    return render_template('split.html',
        groups=groups, active_group=group, expenses=expenses,
        balances=balances, member_map=member_map, debts=debts
    )

def simplify_debts(balances, member_map):
    """Minimize number of transactions to settle all debts."""
    creditors = []  # (member_id, amount_owed_to_them)
    debtors = []    # (member_id, amount_they_owe)

    for mid, bal in balances.items():
        if bal > 0.01:
            creditors.append([mid, bal])
        elif bal < -0.01:
            debtors.append([mid, -bal])

    creditors.sort(key=lambda x: -x[1])
    debtors.sort(key=lambda x: -x[1])

    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        if amount > 0.01:
            settlements.append({
                'from': member_map.get(debtors[i][0], '?'),
                'to': member_map.get(creditors[j][0], '?'),
                'amount': round(amount, 2)
            })
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] < 0.01:
            i += 1
        if creditors[j][1] < 0.01:
            j += 1

    return settlements

@app.route('/split/create_group', methods=['POST'])
def create_group():
    name = request.form['name']
    members = [n.strip() for n in request.form['members'].split(',') if n.strip()]

    g = Group(name=name)
    db.session.add(g)
    db.session.flush()

    for mname in members:
        db.session.add(GroupMember(group_id=g.id, name=mname))

    db.session.commit()
    flash(f'Group "{name}" created with {len(members)} members', 'success')
    return redirect(url_for('split_group', group_id=g.id))

@app.route('/split/<int:group_id>/add_expense', methods=['POST'])
def add_split_expense(group_id):
    group = Group.query.get_or_404(group_id)
    amount = float(request.form['amount'])
    paid_by = int(request.form['paid_by'])

    exp = SplitExpense(
        group_id=group_id,
        description=request.form['description'],
        amount=amount,
        paid_by=paid_by,
        date=parse_date(request.form.get('date'))
    )
    db.session.add(exp)
    db.session.flush()

    # split equally among all members
    split_type = request.form.get('split_type', 'equal')
    members = group.members

    if split_type == 'equal':
        share = round(amount / len(members), 2)
        for m in members:
            db.session.add(SplitShare(
                expense_id=exp.id, member_id=m.id, share_amount=share
            ))
    else:
        # custom split — amounts come from form
        for m in members:
            custom_amount = float(request.form.get(f'share_{m.id}', 0))
            db.session.add(SplitShare(
                expense_id=exp.id, member_id=m.id, share_amount=custom_amount
            ))

    db.session.commit()
    flash(f'Expense "${amount:.2f}" added', 'success')
    return redirect(url_for('split_group', group_id=group_id))

@app.route('/split/<int:group_id>/settle/<int:expense_id>')
def settle_expense(group_id, expense_id):
    exp = SplitExpense.query.get_or_404(expense_id)
    exp.settled = not exp.settled
    db.session.commit()
    flash('Expense ' + ('settled' if exp.settled else 'unsettled'), 'info')
    return redirect(url_for('split_group', group_id=group_id))

@app.route('/split/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    g = Group.query.get_or_404(group_id)
    db.session.delete(g)
    db.session.commit()
    flash(f'Group "{g.name}" deleted', 'info')
    return redirect(url_for('split'))

@app.route('/split/<int:group_id>/add_member', methods=['POST'])
def add_member(group_id):
    name = request.form['name'].strip()
    if name:
        db.session.add(GroupMember(group_id=group_id, name=name))
        db.session.commit()
        flash(f'Added {name} to group', 'success')
    return redirect(url_for('split_group', group_id=group_id))


# ── Investments ───────────────────────────────────────────────

@app.route('/investments')
def investments():
    holdings = Investment.query.order_by(Investment.buy_date.desc()).all()

    total_value = sum(h.current_price * h.quantity for h in holdings)
    total_cost = sum(h.buy_price * h.quantity for h in holdings)
    total_gain = total_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost > 0 else 0

    # allocation by type
    allocation = defaultdict(float)
    for h in holdings:
        allocation[h.type] += h.current_price * h.quantity

    return render_template('investments.html',
        holdings=holdings, total_value=total_value,
        total_cost=total_cost, total_gain=total_gain,
        total_gain_pct=total_gain_pct,
        allocation=dict(allocation),
        investment_types=INVESTMENT_TYPES
    )

@app.route('/investments/add', methods=['POST'])
def add_investment():
    inv = Investment(
        symbol=request.form['symbol'].upper(),
        name=request.form['name'],
        type=request.form['type'],
        quantity=float(request.form['quantity']),
        buy_price=float(request.form['buy_price']),
        current_price=float(request.form.get('current_price', request.form['buy_price'])),
        buy_date=parse_date(request.form.get('buy_date')),
        monthly_contribution=float(request.form.get('monthly_contribution') or 0),
    )
    db.session.add(inv)
    db.session.commit()
    flash(f'Added {inv.symbol} to portfolio', 'success')
    return redirect(url_for('investments'))

@app.route('/investments/update/<int:id>', methods=['POST'])
def update_investment(id):
    inv = Investment.query.get_or_404(id)
    inv.current_price = float(request.form['current_price'])
    db.session.commit()
    flash(f'{inv.symbol} price updated', 'success')
    return redirect(url_for('investments'))

@app.route('/investments/delete/<int:id>')
def delete_investment(id):
    inv = Investment.query.get_or_404(id)
    db.session.delete(inv)
    db.session.commit()
    flash(f'{inv.symbol} removed from portfolio', 'info')
    return redirect(url_for('investments'))


# ── Fixed Payments ───────────────────────────────────────────

@app.route('/fixed-payments')
def fixed_payments():
    payments = FixedPayment.query.order_by(FixedPayment.is_active.desc(), FixedPayment.name).all()
    monthly_total = 0.0
    for f in payments:
        if not f.is_active:
            continue
        if f.frequency == 'monthly':
            monthly_total += f.amount
        elif f.frequency == 'weekly':
            monthly_total += f.amount * 52 / 12
        elif f.frequency == 'yearly':
            monthly_total += f.amount / 12

    return render_template('fixed_payments.html',
        payments=payments, monthly_total=monthly_total,
        categories=EXPENSE_CATEGORIES,
        frequencies=FIXED_PAYMENT_FREQUENCIES
    )

@app.route('/fixed-payments/add', methods=['POST'])
def add_fixed_payment():
    amount = request.form.get('amount', '')
    if not amount or float(amount) <= 0:
        flash('Amount must be greater than 0', 'error')
        return redirect(url_for('fixed_payments'))
    fp = FixedPayment(
        name=request.form['name'],
        amount=float(amount),
        currency=request.form.get('currency', 'USD'),
        frequency=request.form.get('frequency', 'monthly'),
        day_of_month=int(request.form['day_of_month']) if request.form.get('day_of_month') else None,
        category=request.form.get('category', 'Other'),
    )
    db.session.add(fp)
    db.session.commit()
    flash(f'Added "{fp.name}"', 'success')
    return redirect(url_for('fixed_payments'))

@app.route('/fixed-payments/toggle/<int:id>')
def toggle_fixed_payment(id):
    fp = FixedPayment.query.get_or_404(id)
    fp.is_active = not fp.is_active
    db.session.commit()
    flash(f'"{fp.name}" {"activated" if fp.is_active else "paused"}', 'info')
    return redirect(url_for('fixed_payments'))

@app.route('/fixed-payments/delete/<int:id>')
def delete_fixed_payment(id):
    fp = FixedPayment.query.get_or_404(id)
    db.session.delete(fp)
    db.session.commit()
    flash(f'"{fp.name}" removed', 'info')
    return redirect(url_for('fixed_payments'))


# ── Trips ────────────────────────────────────────────────────

@app.route('/trips')
def trips():
    all_trips = Trip.query.order_by(Trip.start_date).all()
    today = date.today()

    # Current month savings rate for projections
    month = current_month()
    year, mo = int(month[:4]), int(month[5:7])
    txns = Transaction.query.filter(
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).all()
    income = sum(t.amount for t in txns if t.type == 'income')
    expenses = sum(t.amount for t in txns if t.type == 'expense')
    monthly_savings = income - expenses

    trip_data = []
    for t in all_trips:
        days_until = (t.start_date - today).days
        days_until = max(days_until, 0)

        if days_until > 0:
            daily_saving_needed = t.estimated_cost / days_until
            projected_savings = monthly_savings * (days_until / 30)
            can_afford = projected_savings >= t.estimated_cost
            if can_afford:
                verdict = 'comfortable'
            elif projected_savings >= t.estimated_cost * 0.7:
                verdict = 'tight'
            else:
                verdict = 'over'
        else:
            daily_saving_needed = 0
            projected_savings = 0
            can_afford = False
            verdict = 'past' if t.start_date < today else 'today'

        trip_data.append({
            'trip': t,
            'days_until': days_until,
            'daily_saving_needed': daily_saving_needed,
            'projected_savings': projected_savings,
            'can_afford': can_afford,
            'verdict': verdict,
        })

    return render_template('trips.html',
        trips=trip_data, monthly_savings=monthly_savings
    )

@app.route('/trips/add', methods=['POST'])
def add_trip():
    amount = request.form.get('estimated_cost', '')
    if not amount or float(amount) <= 0:
        flash('Estimated cost must be greater than 0', 'error')
        return redirect(url_for('trips'))
    t = Trip(
        name=request.form['name'],
        estimated_cost=float(amount),
        currency=request.form.get('currency', 'USD'),
        start_date=parse_date(request.form.get('start_date')),
        end_date=parse_date(request.form.get('end_date')) if request.form.get('end_date') else None,
        notes=request.form.get('notes', ''),
    )
    db.session.add(t)
    db.session.commit()
    flash(f'Trip "{t.name}" added', 'success')
    return redirect(url_for('trips'))

@app.route('/trips/delete/<int:id>')
def delete_trip(id):
    t = Trip.query.get_or_404(id)
    db.session.delete(t)
    db.session.commit()
    flash(f'Trip "{t.name}" removed', 'info')
    return redirect(url_for('trips'))


# ── Edit Transaction ─────────────────────────────────────────

@app.route('/transactions/edit/<int:id>', methods=['GET', 'POST'])
def edit_transaction(id):
    t = Transaction.query.get_or_404(id)
    if request.method == 'POST':
        t.type = request.form['type']
        t.amount = float(request.form['amount'])
        t.currency = request.form.get('currency', 'USD')
        t.category = request.form['category']
        t.description = request.form.get('description', '')
        t.date = parse_date(request.form.get('date'))
        db.session.commit()
        flash('Transaction updated', 'success')
        return redirect(url_for('transactions'))

    return render_template('edit_transaction.html',
        t=t,
        expense_categories=EXPENSE_CATEGORIES,
        income_categories=INCOME_CATEGORIES
    )


# ── Receipt Scanning ──────────────────────────────────────────

@app.route('/scan', methods=['GET', 'POST'])
def scan():
    parsed = None
    if request.method == 'POST':
        file = request.files.get('receipt')
        if file:
            try:
                file.stream.seek(0)
                image = Image.open(file.stream).convert('RGB')
                ocr_text = pytesseract.image_to_string(image, lang='rus+eng')
            except Exception as e:
                flash(f'Could not process image: {e}', 'error')
                return render_template('scan.html', parsed=None, categories=EXPENSE_CATEGORIES)

            parsed = parse_receipt_text(ocr_text)
    return render_template('scan.html', parsed=parsed, categories=EXPENSE_CATEGORIES)

@app.route('/scan/confirm', methods=['POST'])
def scan_confirm():
    t = Transaction(
        type=request.form['type'],
        amount=float(request.form['amount']),
        currency=request.form.get('currency', 'UZS'),
        category=request.form['category'],
        description=request.form.get('description', ''),
        date=parse_date(request.form.get('date'))
    )
    db.session.add(t)
    db.session.commit()
    if t.currency == 'UZS':
        flash(f'Receipt saved: {t.amount:,.0f} UZS', 'success')
    else:
        flash(f'Receipt saved: ${t.amount:.2f}', 'success')
    return redirect(url_for('transactions'))


# ── Import (SMS + CSV) ───────────────────────────────────────

@app.route('/import')
def import_page():
    return render_template('import.html', transactions=None, categories=EXPENSE_CATEGORIES)

@app.route('/import/sms', methods=['POST'])
def import_sms():
    text = request.form.get('sms_text', '')
    parsed = parse_sms_bulk(text)
    return render_template('import.html', transactions=parsed, categories=EXPENSE_CATEGORIES)

@app.route('/import/csv', methods=['POST'])
def import_csv_route():
    file = request.files.get('csv_file')
    if not file:
        flash('No file uploaded', 'error')
        return redirect(url_for('import_page'))
    content = file.read()
    parsed = parse_csv(content)
    return render_template('import.html', transactions=parsed, categories=EXPENSE_CATEGORIES)

@app.route('/import/confirm', methods=['POST'])
def import_confirm():
    count = int(request.form.get('count', 0))
    added = 0
    for i in range(count):
        if not request.form.get(f'select_{i}'):
            continue
        amount = request.form.get(f'amount_{i}')
        if not amount:
            continue
        t = Transaction(
            type=request.form.get(f'type_{i}', 'expense'),
            amount=float(amount),
            currency=request.form.get(f'currency_{i}', 'UZS'),
            category=request.form.get(f'category_{i}', 'Other'),
            description=request.form.get(f'desc_{i}', ''),
            date=parse_date(request.form.get(f'date_{i}'))
        )
        db.session.add(t)
        added += 1
    db.session.commit()
    flash(f'Imported {added} transactions', 'success')
    return redirect(url_for('transactions'))


# ── Run ───────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5001)
