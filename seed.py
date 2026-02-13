"""Seed sample data for FinSplit demo."""
from app import app, db
from models import Transaction, Budget, Group, GroupMember, SplitExpense, SplitShare, Investment, FixedPayment, Trip, SavingsGoal, AppSettings
from datetime import date, timedelta
import random

def seed():
    with app.app_context():
        # Clear existing data
        db.drop_all()
        db.create_all()

        today = date.today()
        month_start = today.replace(day=1)

        # ── Transactions ──────────────────────────────────
        # (type, amount, currency, category, description, date)
        transactions = [
            ('income', 5000, 'USD', 'Salary', 'Monthly salary', month_start + timedelta(days=0)),
            ('income', 12000000, 'UZS', 'Freelance', 'Logo design project', month_start + timedelta(days=5)),
            ('expense', 1200, 'USD', 'Housing', 'Rent', month_start + timedelta(days=1)),
            ('expense', 850000, 'UZS', 'Utilities', 'Beeline mobile', month_start + timedelta(days=3)),
            ('expense', 350000, 'UZS', 'Utilities', 'Internet Turon Telecom', month_start + timedelta(days=3)),
            ('expense', 2500000, 'UZS', 'Food', 'Korzinka Go', month_start + timedelta(days=2)),
            ('expense', 180000, 'UZS', 'Food', 'Oqtepa Lavash', month_start + timedelta(days=6)),
            ('expense', 85000, 'UZS', 'Food', 'Coffee House', month_start + timedelta(days=8)),
            ('expense', 65, 'USD', 'Transport', 'Gas', month_start + timedelta(days=4)),
            ('expense', 150000, 'UZS', 'Transport', 'Yandex Go', month_start + timedelta(days=7)),
            ('expense', 55, 'USD', 'Health', 'Gym membership', month_start + timedelta(days=1)),
            ('expense', 3500000, 'UZS', 'Shopping', 'Texnomart headphones', month_start + timedelta(days=9)),
            ('expense', 35, 'USD', 'Education', 'Online course', month_start + timedelta(days=5)),
        ]
        for t_type, amount, currency, cat, desc, d in transactions:
            db.session.add(Transaction(type=t_type, amount=amount, currency=currency, category=cat, description=desc, date=d))

        # ── Budgets ───────────────────────────────────────
        month_str = today.strftime('%Y-%m')
        budgets = [
            ('Food', 500), ('Transport', 150), ('Housing', 1300),
            ('Utilities', 200), ('Entertainment', 200), ('Health', 100),
            ('Shopping', 150), ('Education', 100),
        ]
        for cat, limit in budgets:
            db.session.add(Budget(category=cat, monthly_limit=limit, month=month_str))

        # ── Split Group ───────────────────────────────────
        g = Group(name='Weekend Trip')
        db.session.add(g)
        db.session.flush()

        members = ['You', 'Alice', 'Bob']
        member_objs = []
        for name in members:
            m = GroupMember(group_id=g.id, name=name)
            db.session.add(m)
            member_objs.append(m)
        db.session.flush()

        # Expenses for the trip
        split_data = [
            ('Hotel', 450, member_objs[0], today - timedelta(days=3)),
            ('Dinner', 120, member_objs[1], today - timedelta(days=2)),
            ('Gas', 80, member_objs[2], today - timedelta(days=2)),
            ('Breakfast', 45, member_objs[0], today - timedelta(days=1)),
        ]
        for desc, amount, payer, d in split_data:
            exp = SplitExpense(group_id=g.id, description=desc, amount=amount, paid_by=payer.id, date=d)
            db.session.add(exp)
            db.session.flush()
            share = round(amount / len(member_objs), 2)
            for m in member_objs:
                db.session.add(SplitShare(expense_id=exp.id, member_id=m.id, share_amount=share))

        # Second group
        g2 = Group(name='Roommates')
        db.session.add(g2)
        db.session.flush()
        rm_members = []
        for name in ['You', 'Dave']:
            m = GroupMember(group_id=g2.id, name=name)
            db.session.add(m)
            rm_members.append(m)
        db.session.flush()

        exp2 = SplitExpense(group_id=g2.id, description='Groceries', amount=90, paid_by=rm_members[0].id, date=today)
        db.session.add(exp2)
        db.session.flush()
        for m in rm_members:
            db.session.add(SplitShare(expense_id=exp2.id, member_id=m.id, share_amount=45))

        # ── Investments ───────────────────────────────────
        investments = [
            ('AAPL', 'Apple Inc.', 'stock', 10, 150.00, 178.50, today - timedelta(days=90)),
            ('GOOGL', 'Alphabet Inc.', 'stock', 5, 140.00, 165.20, today - timedelta(days=60)),
            ('BTC', 'Bitcoin', 'crypto', 0.5, 42000, 48500, today - timedelta(days=120)),
            ('VTI', 'Vanguard Total Market', 'etf', 20, 220.00, 235.80, today - timedelta(days=180)),
            ('VXUS', 'Vanguard Intl Stock', 'etf', 15, 55.00, 58.30, today - timedelta(days=150)),
        ]
        for sym, name, itype, qty, buy, curr, d in investments:
            db.session.add(Investment(
                symbol=sym, name=name, type=itype,
                quantity=qty, buy_price=buy, current_price=curr, buy_date=d,
                monthly_contribution=100 if itype == 'etf' else 0
            ))

        # ── Fixed Payments ──────────────────────────────────
        fixed_payments = [
            ('Rent', 1200, 'USD', 'monthly', 1, 'Housing'),
            ('Internet', 30, 'USD', 'monthly', 5, 'Utilities'),
            ('Phone', 25, 'USD', 'monthly', 15, 'Utilities'),
            ('Gym', 55, 'USD', 'monthly', 1, 'Health'),
            ('Netflix', 15, 'USD', 'monthly', 10, 'Entertainment'),
            ('Spotify', 10, 'USD', 'monthly', 10, 'Entertainment'),
            ('Car Insurance', 1200, 'USD', 'yearly', None, 'Transport'),
        ]
        for name, amount, currency, freq, day, cat in fixed_payments:
            db.session.add(FixedPayment(
                name=name, amount=amount, currency=currency,
                frequency=freq, day_of_month=day, category=cat
            ))

        # ── Trips ───────────────────────────────────────────
        trips = [
            ('Bali', 2500, 'USD', today + timedelta(days=45), today + timedelta(days=52), 'Flights + hotel + activities'),
            ('Istanbul Weekend', 800, 'USD', today + timedelta(days=20), today + timedelta(days=23), 'Budget trip'),
        ]
        for name, cost, currency, start, end, notes in trips:
            db.session.add(Trip(
                name=name, estimated_cost=cost, currency=currency,
                start_date=start, end_date=end, notes=notes
            ))

        # ── Savings Goals ───────────────────────────────────
        savings_goals = [
            ('Emergency Fund', 10000, 3500, 'USD', today + timedelta(days=365), 'shield'),
            ('New Laptop', 2000, 800, 'USD', today + timedelta(days=90), 'laptop'),
            ('Vacation Fund', 3000, 1200, 'USD', today + timedelta(days=180), 'plane'),
        ]
        for name, target, current, currency, deadline, icon in savings_goals:
            db.session.add(SavingsGoal(
                name=name, target_amount=target, current_amount=current,
                currency=currency, deadline=deadline, icon=icon
            ))

        # ── App Settings ───────────────────────────────────
        db.session.add(AppSettings(key='uzs_usd_rate', value='12800'))

        db.session.commit()
        print('Seeded successfully!')
        print(f'  {len(transactions)} transactions')
        print(f'  {len(budgets)} budgets')
        print(f'  2 split groups with expenses')
        print(f'  {len(investments)} investments')
        print(f'  {len(fixed_payments)} fixed payments')
        print(f'  {len(trips)} trips')
        print(f'  {len(savings_goals)} savings goals')

if __name__ == '__main__':
    seed()
