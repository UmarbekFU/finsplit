import csv
import io
import os

import pytesseract
from PIL import Image
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    Response,
)
from flask_wtf.csrf import CSRFProtect
from datetime import date, datetime
from calendar import monthrange
from collections import defaultdict

from models import (
    db, Transaction, Budget, Group, GroupMember,
    SplitExpense, SplitShare, Investment, FixedPayment, Trip,
    SavingsGoal, AppSettings,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, INVESTMENT_TYPES,
    CURRENCIES, CURRENCY_SYMBOLS, FIXED_PAYMENT_FREQUENCIES,
    CATEGORY_COLORS, CATEGORY_BG_COLORS,
)
from parsers import parse_receipt_text, parse_sms_bulk, parse_csv

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///finsplit.db'
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-key-change-in-production-8x7k2m')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

csrf = CSRFProtect(app)
db.init_app(app)

with app.app_context():
    db.create_all()
    for stmt in [
        "ALTER TABLE 'transaction' ADD COLUMN currency VARCHAR(3) DEFAULT 'USD'",
        "ALTER TABLE investment ADD COLUMN monthly_contribution FLOAT DEFAULT 0",
    ]:
        try:
            db.session.execute(db.text(stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()
    if not AppSettings.query.filter_by(key='uzs_usd_rate').first():
        db.session.add(AppSettings(key='uzs_usd_rate', value='12800'))
        db.session.commit()


# ── Currency Helpers ──────────────────────────────────────────

def get_exchange_rate():
    setting = AppSettings.query.filter_by(key='uzs_usd_rate').first()
    return float(setting.value) if setting else 12800.0

def to_usd(amount, currency):
    if currency == 'USD':
        return amount
    rate = get_exchange_rate()
    return amount / rate if rate > 0 else 0

def format_money(amount, currency='USD'):
    if currency == 'UZS':
        return f'{amount:,.0f} UZS'
    return f'${amount:,.2f}'


# ── Template Helpers ──────────────────────────────────────────

@app.template_filter('money')
def money_filter(amount, currency='USD'):
    return format_money(amount, currency)

@app.template_filter('to_usd')
def to_usd_filter(amount, currency='USD'):
    return to_usd(amount, currency)

@app.context_processor
def inject_globals():
    return {
        'category_colors': CATEGORY_COLORS,
        'category_bg_colors': CATEGORY_BG_COLORS,
        'currencies': CURRENCIES,
    }

def current_month():
    return date.today().strftime('%Y-%m')

def parse_date(s):
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return date.today()

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Dashboard ─────────────────────────────────────────────────

def calculate_daily_allowance(month=None):
    today = date.today()
    if month is None:
        month = current_month()
    year, mo = int(month[:4]), int(month[5:7])
    days_in_month = monthrange(year, mo)[1]

    is_current = (year == today.year and mo == today.month)
    days_remaining = max(1, days_in_month - today.day + 1) if is_current else days_in_month

    txns = Transaction.query.filter(
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).all()

    income = sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'income')
    already_spent = sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'expense')

    fixed = FixedPayment.query.filter_by(is_active=True).all()
    fixed_total = 0.0
    for f in fixed:
        amt_usd = to_usd(f.amount, f.currency)
        if f.frequency == 'monthly':
            fixed_total += amt_usd
        elif f.frequency == 'weekly':
            fixed_total += amt_usd * 52 / 12
        elif f.frequency == 'yearly':
            fixed_total += amt_usd / 12

    investments = Investment.query.all()
    invest_contributions = sum(i.monthly_contribution or 0 for i in investments)

    remaining = income - fixed_total - invest_contributions - already_spent
    daily_allowance = remaining / days_remaining

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


@app.route('/')
def dashboard():
    month = request.args.get('month', current_month())
    year, mo = int(month[:4]), int(month[5:7])

    txns = Transaction.query.filter(
        db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo
    ).order_by(Transaction.date.desc()).all()

    income = sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'income')
    expenses = sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'expense')
    savings = income - expenses
    savings_rate = (savings / income * 100) if income > 0 else 0

    allowance = calculate_daily_allowance(month)

    budgets = Budget.query.filter_by(month=month).all()
    budget_status = []
    for b in budgets:
        spent = sum(
            to_usd(t.amount, t.currency) for t in txns
            if t.type == 'expense' and t.category == b.category
        )
        pct = (spent / b.monthly_limit * 100) if b.monthly_limit > 0 else 0
        budget_status.append({
            'category': b.category, 'limit': b.monthly_limit, 'spent': spent,
            'pct': min(pct, 100), 'over': spent > b.monthly_limit
        })

    spending_by_cat = defaultdict(float)
    for t in txns:
        if t.type == 'expense':
            spending_by_cat[t.category] += to_usd(t.amount, t.currency)

    holdings = Investment.query.all()
    portfolio_value = sum(i.current_price * i.quantity for i in holdings)
    portfolio_cost = sum(i.buy_price * i.quantity for i in holdings)
    portfolio_gain = portfolio_value - portfolio_cost

    today = date.today()
    upcoming_bills = []
    for fp in FixedPayment.query.filter_by(is_active=True).all():
        if fp.day_of_month:
            due_day = min(fp.day_of_month, monthrange(today.year, today.month)[1])
            if today.day <= due_day <= today.day + 7:
                upcoming_bills.append({
                    'name': fp.name, 'amount': fp.amount,
                    'currency': fp.currency, 'day': due_day, 'category': fp.category,
                })

    # Daily spending trend for chart
    days_in_month = monthrange(year, mo)[1]
    daily_spending = [0.0] * days_in_month
    for t in txns:
        if t.type == 'expense':
            day_idx = t.date.day - 1
            if 0 <= day_idx < days_in_month:
                daily_spending[day_idx] += to_usd(t.amount, t.currency)

    return render_template('dashboard.html',
        month=month, income=income, expenses=expenses,
        savings=savings, savings_rate=savings_rate,
        allowance=allowance, budget_status=budget_status,
        spending_by_cat=dict(spending_by_cat),
        portfolio_value=portfolio_value, portfolio_gain=portfolio_gain,
        upcoming_bills=upcoming_bills, recent=txns[:8],
        daily_spending=daily_spending,
    )


# ── Transactions ──────────────────────────────────────────────

@app.route('/transactions')
def transactions():
    type_filter = request.args.get('type', '')
    cat_filter = request.args.get('category', '')
    search = request.args.get('q', '').strip()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    page = request.args.get('page', 1, type=int)

    q = Transaction.query
    if type_filter:
        q = q.filter_by(type=type_filter)
    if cat_filter:
        q = q.filter_by(category=cat_filter)
    if search:
        q = q.filter(Transaction.description.ilike(f'%{search}%'))
    if date_from:
        q = q.filter(Transaction.date >= parse_date(date_from))
    if date_to:
        q = q.filter(Transaction.date <= parse_date(date_to))

    pagination = q.order_by(Transaction.date.desc()).paginate(page=page, per_page=25, error_out=False)
    txns = pagination.items
    total = sum(to_usd(t.amount, t.currency) if t.type == 'income' else -to_usd(t.amount, t.currency) for t in txns)

    return render_template('transactions.html',
        transactions=txns, total=total, pagination=pagination,
        expense_categories=EXPENSE_CATEGORIES, income_categories=INCOME_CATEGORIES,
        type_filter=type_filter, cat_filter=cat_filter,
        search=search, date_from=date_from, date_to=date_to,
    )

@app.route('/transactions/add', methods=['POST'])
def add_transaction():
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Amount must be greater than 0', 'error')
        return redirect(url_for('transactions'))
    t = Transaction(
        type=request.form['type'], amount=amount,
        currency=request.form.get('currency', 'USD'),
        category=request.form['category'],
        description=request.form.get('description', ''),
        date=parse_date(request.form.get('date'))
    )
    db.session.add(t)
    db.session.commit()
    flash(f'{"Income" if t.type == "income" else "Expense"} added: {format_money(t.amount, t.currency)}', 'success')
    return redirect(url_for('transactions'))

@app.route('/transactions/delete/<int:id>', methods=['POST'])
def delete_transaction(id):
    Transaction.query.get_or_404(id)
    db.session.delete(Transaction.query.get(id))
    db.session.commit()
    flash('Transaction deleted', 'info')
    return redirect(url_for('transactions'))

@app.route('/transactions/edit/<int:id>', methods=['GET', 'POST'])
def edit_transaction(id):
    t = Transaction.query.get_or_404(id)
    if request.method == 'POST':
        amount = safe_float(request.form.get('amount'))
        if amount <= 0:
            flash('Amount must be greater than 0', 'error')
            return redirect(url_for('edit_transaction', id=id))
        t.type = request.form['type']
        t.amount = amount
        t.currency = request.form.get('currency', 'USD')
        t.category = request.form['category']
        t.description = request.form.get('description', '')
        t.date = parse_date(request.form.get('date'))
        db.session.commit()
        flash('Transaction updated', 'success')
        return redirect(url_for('transactions'))
    return render_template('edit_transaction.html', t=t,
        expense_categories=EXPENSE_CATEGORIES, income_categories=INCOME_CATEGORIES)

@app.route('/transactions/export')
def export_transactions():
    q = Transaction.query
    for key, filt in [('type', 'type'), ('category', 'category')]:
        val = request.args.get(key, '')
        if val:
            q = q.filter_by(**{filt: val})
    if request.args.get('from'):
        q = q.filter(Transaction.date >= parse_date(request.args['from']))
    if request.args.get('to'):
        q = q.filter(Transaction.date <= parse_date(request.args['to']))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Type', 'Category', 'Description', 'Amount', 'Currency', 'Amount (USD)'])
    for t in q.order_by(Transaction.date.desc()).all():
        writer.writerow([t.date.strftime('%Y-%m-%d'), t.type, t.category,
            t.description, t.amount, t.currency, round(to_usd(t.amount, t.currency), 2)])
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=transactions_{date.today()}.csv'})


# ── Budgets ───────────────────────────────────────────────────

@app.route('/budgets')
def budgets():
    month = request.args.get('month', current_month())
    year, mo = int(month[:4]), int(month[5:7])
    budget_list = Budget.query.filter_by(month=month).all()

    spending = defaultdict(float)
    for t in Transaction.query.filter(Transaction.type == 'expense',
            db.extract('year', Transaction.date) == year,
            db.extract('month', Transaction.date) == mo).all():
        spending[t.category] += to_usd(t.amount, t.currency)

    budget_data = []
    for b in budget_list:
        spent = spending.get(b.category, 0)
        pct = (spent / b.monthly_limit * 100) if b.monthly_limit > 0 else 0
        budget_data.append({
            'id': b.id, 'category': b.category, 'limit': b.monthly_limit,
            'spent': spent, 'remaining': b.monthly_limit - spent,
            'pct': min(pct, 100), 'over': spent > b.monthly_limit
        })

    budgeted_cats = {b.category for b in budget_list}
    unbudgeted = {cat: amt for cat, amt in spending.items() if cat not in budgeted_cats}
    return render_template('budgets.html', month=month, budgets=budget_data,
        unbudgeted=unbudgeted, categories=EXPENSE_CATEGORIES)

@app.route('/budgets/add', methods=['POST'])
def add_budget():
    month = request.form.get('month', current_month())
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Budget limit must be greater than 0', 'error')
        return redirect(url_for('budgets', month=month))
    existing = Budget.query.filter_by(category=request.form['category'], month=month).first()
    if existing:
        existing.monthly_limit = amount
    else:
        db.session.add(Budget(category=request.form['category'], monthly_limit=amount, month=month))
    db.session.commit()
    flash('Budget saved', 'success')
    return redirect(url_for('budgets', month=month))

@app.route('/budgets/delete/<int:id>', methods=['POST'])
def delete_budget(id):
    b = Budget.query.get_or_404(id)
    month = b.month
    db.session.delete(b)
    db.session.commit()
    flash('Budget removed', 'info')
    return redirect(url_for('budgets', month=month))

@app.route('/budgets/copy', methods=['POST'])
def copy_budgets():
    target_month = request.form.get('month', current_month())
    year, mo = int(target_month[:4]), int(target_month[5:7])
    prev_month = f'{year - 1}-12' if mo == 1 else f'{year}-{mo - 1:02d}'
    prev_budgets = Budget.query.filter_by(month=prev_month).all()
    if not prev_budgets:
        flash('No budgets found in previous month to copy', 'error')
        return redirect(url_for('budgets', month=target_month))
    copied = 0
    for pb in prev_budgets:
        if not Budget.query.filter_by(category=pb.category, month=target_month).first():
            db.session.add(Budget(category=pb.category, monthly_limit=pb.monthly_limit, month=target_month))
            copied += 1
    db.session.commit()
    flash(f'Copied {copied} budgets from {prev_month}', 'success')
    return redirect(url_for('budgets', month=target_month))


# ── Expense Splitting ─────────────────────────────────────────

@app.route('/split')
def split():
    return render_template('split.html',
        groups=Group.query.order_by(Group.created_at.desc()).all(), active_group=None)

@app.route('/split/<int:group_id>')
def split_group(group_id):
    group = Group.query.get_or_404(group_id)
    expenses = SplitExpense.query.filter_by(group_id=group_id).order_by(SplitExpense.date.desc()).all()
    balances = defaultdict(float)
    for exp in expenses:
        if not exp.settled:
            balances[exp.paid_by] += exp.amount
            for share in exp.shares:
                balances[share.member_id] -= share.share_amount
    member_map = {m.id: m.name for m in group.members}
    return render_template('split.html',
        groups=Group.query.order_by(Group.created_at.desc()).all(),
        active_group=group, expenses=expenses,
        balances=balances, member_map=member_map,
        debts=simplify_debts(balances, member_map))

def simplify_debts(balances, member_map):
    creditors, debtors = [], []
    for mid, bal in balances.items():
        if bal > 0.01: creditors.append([mid, bal])
        elif bal < -0.01: debtors.append([mid, -bal])
    creditors.sort(key=lambda x: -x[1])
    debtors.sort(key=lambda x: -x[1])
    settlements, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        if amount > 0.01:
            settlements.append({'from': member_map.get(debtors[i][0], '?'),
                'to': member_map.get(creditors[j][0], '?'), 'amount': round(amount, 2)})
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] < 0.01: i += 1
        if creditors[j][1] < 0.01: j += 1
    return settlements

@app.route('/split/create_group', methods=['POST'])
def create_group():
    name = request.form.get('name', '').strip()
    members = [n.strip() for n in request.form.get('members', '').split(',') if n.strip()]
    if not name:
        flash('Group name is required', 'error')
        return redirect(url_for('split'))
    if len(members) < 2:
        flash('At least 2 members required', 'error')
        return redirect(url_for('split'))
    g = Group(name=name)
    db.session.add(g)
    db.session.flush()
    for mname in members:
        db.session.add(GroupMember(group_id=g.id, name=mname))
    db.session.commit()
    flash(f'Group "{name}" created', 'success')
    return redirect(url_for('split_group', group_id=g.id))

@app.route('/split/<int:group_id>/add_expense', methods=['POST'])
def add_split_expense(group_id):
    group = Group.query.get_or_404(group_id)
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Amount must be greater than 0', 'error')
        return redirect(url_for('split_group', group_id=group_id))
    exp = SplitExpense(group_id=group_id, description=request.form['description'],
        amount=amount, paid_by=int(request.form['paid_by']),
        date=parse_date(request.form.get('date')))
    db.session.add(exp)
    db.session.flush()
    members = group.members
    if request.form.get('split_type', 'equal') == 'equal':
        share = round(amount / len(members), 2)
        for m in members:
            db.session.add(SplitShare(expense_id=exp.id, member_id=m.id, share_amount=share))
    else:
        for m in members:
            db.session.add(SplitShare(expense_id=exp.id, member_id=m.id,
                share_amount=safe_float(request.form.get(f'share_{m.id}', 0))))
    db.session.commit()
    flash(f'Expense ${amount:.2f} added', 'success')
    return redirect(url_for('split_group', group_id=group_id))

@app.route('/split/<int:group_id>/settle/<int:expense_id>', methods=['POST'])
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
    name = request.form.get('name', '').strip()
    if name:
        db.session.add(GroupMember(group_id=group_id, name=name))
        db.session.commit()
        flash(f'Added {name}', 'success')
    return redirect(url_for('split_group', group_id=group_id))


# ── Investments ───────────────────────────────────────────────

@app.route('/investments')
def investments():
    holdings = Investment.query.order_by(Investment.buy_date.desc()).all()
    total_value = sum(h.current_price * h.quantity for h in holdings)
    total_cost = sum(h.buy_price * h.quantity for h in holdings)
    total_gain = total_value - total_cost
    allocation = defaultdict(float)
    for h in holdings:
        allocation[h.type] += h.current_price * h.quantity
    return render_template('investments.html', holdings=holdings,
        total_value=total_value, total_cost=total_cost, total_gain=total_gain,
        total_gain_pct=(total_gain / total_cost * 100) if total_cost > 0 else 0,
        allocation=dict(allocation), investment_types=INVESTMENT_TYPES)

@app.route('/investments/add', methods=['POST'])
def add_investment():
    qty = safe_float(request.form.get('quantity'))
    buy_price = safe_float(request.form.get('buy_price'))
    if qty <= 0 or buy_price <= 0:
        flash('Quantity and buy price must be > 0', 'error')
        return redirect(url_for('investments'))
    inv = Investment(symbol=request.form['symbol'].upper(), name=request.form['name'],
        type=request.form['type'], quantity=qty, buy_price=buy_price,
        current_price=safe_float(request.form.get('current_price')) or buy_price,
        buy_date=parse_date(request.form.get('buy_date')),
        monthly_contribution=safe_float(request.form.get('monthly_contribution')))
    db.session.add(inv)
    db.session.commit()
    flash(f'Added {inv.symbol}', 'success')
    return redirect(url_for('investments'))

@app.route('/investments/update/<int:id>', methods=['POST'])
def update_investment(id):
    inv = Investment.query.get_or_404(id)
    price = safe_float(request.form.get('current_price'))
    if price <= 0:
        flash('Price must be > 0', 'error')
        return redirect(url_for('investments'))
    inv.current_price = price
    db.session.commit()
    flash(f'{inv.symbol} updated', 'success')
    return redirect(url_for('investments'))

@app.route('/investments/delete/<int:id>', methods=['POST'])
def delete_investment(id):
    inv = Investment.query.get_or_404(id)
    db.session.delete(inv)
    db.session.commit()
    flash(f'{inv.symbol} removed', 'info')
    return redirect(url_for('investments'))


# ── Fixed Payments ───────────────────────────────────────────

@app.route('/fixed-payments')
def fixed_payments():
    payments = FixedPayment.query.order_by(FixedPayment.is_active.desc(), FixedPayment.name).all()
    monthly_total = 0.0
    for f in payments:
        if not f.is_active: continue
        amt = to_usd(f.amount, f.currency)
        if f.frequency == 'monthly': monthly_total += amt
        elif f.frequency == 'weekly': monthly_total += amt * 52 / 12
        elif f.frequency == 'yearly': monthly_total += amt / 12
    return render_template('fixed_payments.html', payments=payments,
        monthly_total=monthly_total, categories=EXPENSE_CATEGORIES,
        frequencies=FIXED_PAYMENT_FREQUENCIES)

@app.route('/fixed-payments/add', methods=['POST'])
def add_fixed_payment():
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Amount must be > 0', 'error')
        return redirect(url_for('fixed_payments'))
    fp = FixedPayment(name=request.form['name'], amount=amount,
        currency=request.form.get('currency', 'USD'),
        frequency=request.form.get('frequency', 'monthly'),
        day_of_month=int(request.form['day_of_month']) if request.form.get('day_of_month') else None,
        category=request.form.get('category', 'Other'))
    db.session.add(fp)
    db.session.commit()
    flash(f'Added "{fp.name}"', 'success')
    return redirect(url_for('fixed_payments'))

@app.route('/fixed-payments/toggle/<int:id>', methods=['POST'])
def toggle_fixed_payment(id):
    fp = FixedPayment.query.get_or_404(id)
    fp.is_active = not fp.is_active
    db.session.commit()
    flash(f'"{fp.name}" {"activated" if fp.is_active else "paused"}', 'info')
    return redirect(url_for('fixed_payments'))

@app.route('/fixed-payments/delete/<int:id>', methods=['POST'])
def delete_fixed_payment(id):
    fp = FixedPayment.query.get_or_404(id)
    db.session.delete(fp)
    db.session.commit()
    flash(f'"{fp.name}" removed', 'info')
    return redirect(url_for('fixed_payments'))


# ── Trips ────────────────────────────────────────────────────

@app.route('/trips')
def trips():
    today = date.today()
    month = current_month()
    year, mo = int(month[:4]), int(month[5:7])
    txns = Transaction.query.filter(db.extract('year', Transaction.date) == year,
        db.extract('month', Transaction.date) == mo).all()
    monthly_savings = (sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'income')
        - sum(to_usd(t.amount, t.currency) for t in txns if t.type == 'expense'))
    trip_data = []
    for t in Trip.query.order_by(Trip.start_date).all():
        cost_usd = to_usd(t.estimated_cost, t.currency)
        days_until = max((t.start_date - today).days, 0)
        if days_until > 0:
            daily_saving_needed = cost_usd / days_until
            projected = monthly_savings * (days_until / 30)
            verdict = 'comfortable' if projected >= cost_usd else ('tight' if projected >= cost_usd * 0.7 else 'over')
        else:
            daily_saving_needed, projected = 0, 0
            verdict = 'past' if t.start_date < today else 'today'
        trip_data.append({'trip': t, 'cost_usd': cost_usd, 'days_until': days_until,
            'daily_saving_needed': daily_saving_needed, 'projected_savings': projected,
            'can_afford': projected >= cost_usd if days_until > 0 else False, 'verdict': verdict})
    return render_template('trips.html', trips=trip_data, monthly_savings=monthly_savings)

@app.route('/trips/add', methods=['POST'])
def add_trip():
    amount = safe_float(request.form.get('estimated_cost'))
    if amount <= 0:
        flash('Cost must be > 0', 'error')
        return redirect(url_for('trips'))
    t = Trip(name=request.form['name'], estimated_cost=amount,
        currency=request.form.get('currency', 'USD'),
        start_date=parse_date(request.form.get('start_date')),
        end_date=parse_date(request.form.get('end_date')) if request.form.get('end_date') else None,
        notes=request.form.get('notes', ''))
    db.session.add(t)
    db.session.commit()
    flash(f'Trip "{t.name}" added', 'success')
    return redirect(url_for('trips'))

@app.route('/trips/delete/<int:id>', methods=['POST'])
def delete_trip(id):
    t = Trip.query.get_or_404(id)
    db.session.delete(t)
    db.session.commit()
    flash(f'Trip "{t.name}" removed', 'info')
    return redirect(url_for('trips'))


# ── Savings Goals ─────────────────────────────────────────────

@app.route('/savings')
def savings():
    goal_data = []
    for g in SavingsGoal.query.order_by(SavingsGoal.created_at.desc()).all():
        pct = (g.current_amount / g.target_amount * 100) if g.target_amount > 0 else 0
        remaining = g.target_amount - g.current_amount
        days_left = max((g.deadline - date.today()).days, 0) if g.deadline else None
        daily_needed = (remaining / days_left if days_left else None) if days_left else None
        goal_data.append({'goal': g, 'pct': min(pct, 100), 'remaining': remaining,
            'days_left': days_left, 'daily_needed': daily_needed})
    return render_template('savings.html', goals=goal_data)

@app.route('/savings/add', methods=['POST'])
def add_savings_goal():
    target = safe_float(request.form.get('target_amount'))
    if target <= 0:
        flash('Target must be > 0', 'error')
        return redirect(url_for('savings'))
    db.session.add(SavingsGoal(name=request.form['name'], target_amount=target,
        current_amount=safe_float(request.form.get('current_amount')),
        currency=request.form.get('currency', 'USD'),
        deadline=parse_date(request.form.get('deadline')) if request.form.get('deadline') else None,
        icon=request.form.get('icon', 'piggy-bank')))
    db.session.commit()
    flash('Goal created', 'success')
    return redirect(url_for('savings'))

@app.route('/savings/fund/<int:id>', methods=['POST'])
def fund_savings_goal(id):
    g = SavingsGoal.query.get_or_404(id)
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Amount must be > 0', 'error')
        return redirect(url_for('savings'))
    g.current_amount = min(g.current_amount + amount, g.target_amount)
    db.session.commit()
    flash(f'Added ${amount:.2f} to "{g.name}"', 'success')
    return redirect(url_for('savings'))

@app.route('/savings/delete/<int:id>', methods=['POST'])
def delete_savings_goal(id):
    g = SavingsGoal.query.get_or_404(id)
    db.session.delete(g)
    db.session.commit()
    flash(f'"{g.name}" removed', 'info')
    return redirect(url_for('savings'))


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
    amount = safe_float(request.form.get('amount'))
    if amount <= 0:
        flash('Amount must be > 0', 'error')
        return redirect(url_for('scan'))
    t = Transaction(type=request.form['type'], amount=amount,
        currency=request.form.get('currency', 'UZS'), category=request.form['category'],
        description=request.form.get('description', ''), date=parse_date(request.form.get('date')))
    db.session.add(t)
    db.session.commit()
    flash(f'Receipt saved: {format_money(t.amount, t.currency)}', 'success')
    return redirect(url_for('transactions'))


# ── Import ───────────────────────────────────────────────────

@app.route('/import')
def import_page():
    return render_template('import.html', transactions=None, categories=EXPENSE_CATEGORIES)

@app.route('/import/sms', methods=['POST'])
def import_sms():
    return render_template('import.html',
        transactions=parse_sms_bulk(request.form.get('sms_text', '')), categories=EXPENSE_CATEGORIES)

@app.route('/import/csv', methods=['POST'])
def import_csv_route():
    file = request.files.get('csv_file')
    if not file:
        flash('No file uploaded', 'error')
        return redirect(url_for('import_page'))
    return render_template('import.html', transactions=parse_csv(file.read()), categories=EXPENSE_CATEGORIES)

@app.route('/import/confirm', methods=['POST'])
def import_confirm():
    added = 0
    for i in range(int(request.form.get('count', 0))):
        if not request.form.get(f'select_{i}'): continue
        amount = safe_float(request.form.get(f'amount_{i}'))
        if amount <= 0: continue
        db.session.add(Transaction(
            type=request.form.get(f'type_{i}', 'expense'), amount=amount,
            currency=request.form.get(f'currency_{i}', 'UZS'),
            category=request.form.get(f'category_{i}', 'Other'),
            description=request.form.get(f'desc_{i}', ''),
            date=parse_date(request.form.get(f'date_{i}'))))
        added += 1
    db.session.commit()
    flash(f'Imported {added} transactions', 'success')
    return redirect(url_for('transactions'))


# ── Settings ─────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        rate = safe_float(request.form.get('uzs_usd_rate'))
        if rate <= 0:
            flash('Rate must be > 0', 'error')
            return redirect(url_for('settings'))
        setting = AppSettings.query.filter_by(key='uzs_usd_rate').first()
        if setting: setting.value = str(rate)
        else: db.session.add(AppSettings(key='uzs_usd_rate', value=str(rate)))
        db.session.commit()
        flash('Settings saved', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', uzs_usd_rate=get_exchange_rate())


if __name__ == '__main__':
    app.run(debug=True, port=5001)
