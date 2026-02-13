from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime

db = SQLAlchemy()

# ── Transactions ──────────────────────────────────────────────

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(10), nullable=False)          # 'income' or 'expense'
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='USD')  # 'USD' or 'UZS'
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200), default='')
    date = db.Column(db.Date, nullable=False, default=date.today)

CURRENCIES = ['USD', 'UZS']
CURRENCY_SYMBOLS = {'USD': '$', 'UZS': 'UZS'}

EXPENSE_CATEGORIES = [
    'Food', 'Transport', 'Housing', 'Utilities', 'Entertainment',
    'Health', 'Shopping', 'Education', 'Other'
]
INCOME_CATEGORIES = ['Salary', 'Freelance', 'Investment', 'Gift', 'Other']

# ── Budgets ───────────────────────────────────────────────────

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    monthly_limit = db.Column(db.Float, nullable=False)
    month = db.Column(db.String(7), nullable=False)          # 'YYYY-MM'

    __table_args__ = (
        db.UniqueConstraint('category', 'month', name='uq_budget_cat_month'),
    )

# ── Expense Splitting ─────────────────────────────────────────

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    members = db.relationship('GroupMember', backref='group', cascade='all, delete-orphan')
    expenses = db.relationship('SplitExpense', backref='group', cascade='all, delete-orphan')

class GroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)

class SplitExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid_by = db.Column(db.Integer, db.ForeignKey('group_member.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    settled = db.Column(db.Boolean, default=False)
    payer = db.relationship('GroupMember', foreign_keys=[paid_by])
    shares = db.relationship('SplitShare', backref='expense', cascade='all, delete-orphan')

class SplitShare(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_id = db.Column(db.Integer, db.ForeignKey('split_expense.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('group_member.id'), nullable=False)
    share_amount = db.Column(db.Float, nullable=False)
    member = db.relationship('GroupMember', foreign_keys=[member_id])

# ── Investments ───────────────────────────────────────────────

class Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)          # 'stock', 'crypto', 'mutual_fund'
    quantity = db.Column(db.Float, nullable=False)
    buy_price = db.Column(db.Float, nullable=False)
    current_price = db.Column(db.Float, nullable=False)
    buy_date = db.Column(db.Date, nullable=False, default=date.today)
    monthly_contribution = db.Column(db.Float, default=0)

INVESTMENT_TYPES = ['stock', 'crypto', 'mutual_fund', 'etf', 'bond']

# ── Fixed Payments ───────────────────────────────────────────

FIXED_PAYMENT_FREQUENCIES = ['monthly', 'weekly', 'yearly']

class FixedPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='USD')
    frequency = db.Column(db.String(10), nullable=False, default='monthly')
    day_of_month = db.Column(db.Integer, nullable=True)
    category = db.Column(db.String(50), nullable=False, default='Other')
    is_active = db.Column(db.Boolean, default=True)

# ── Trips ────────────────────────────────────────────────────

class Trip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    estimated_cost = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='USD')
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
