from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    base_currency = db.Column(db.String(3), default='USD')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    accounts = db.relationship('Account', backref='user', lazy=True, cascade='all, delete-orphan')
    assets = db.relationship('Asset', backref='user', lazy=True, cascade='all, delete-orphan')
    liabilities = db.relationship('Liability', backref='user', lazy=True, cascade='all, delete-orphan')
    exchange_rates = db.relationship('ExchangeRate', backref='user', lazy=True, cascade='all, delete-orphan')


class Account(db.Model):
    __tablename__ = 'accounts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    bank_name = db.Column(db.String(100), nullable=False)
    account_number = db.Column(db.String(50), default='')
    currency = db.Column(db.String(3), nullable=False, default='USD')
    country = db.Column(db.String(100), default='')
    account_type = db.Column(db.String(50), default='checking')
    balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='account', lazy=True, cascade='all, delete-orphan')


class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500), default='')
    amount = db.Column(db.Float, nullable=False)
    amount_base = db.Column(db.Float)
    category = db.Column(db.String(100), default='Uncategorized')
    transaction_type = db.Column(db.String(20), default='expense')
    is_internal_transfer = db.Column(db.Boolean, default=False)
    transfer_pair_id = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Asset(db.Model):
    __tablename__ = 'assets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    value = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    asset_type = db.Column(db.String(50), default='other')
    description = db.Column(db.String(500), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Liability(db.Model):
    __tablename__ = 'liabilities'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    balance = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    liability_type = db.Column(db.String(50), default='other')
    interest_rate = db.Column(db.Float, default=0.0)
    due_date = db.Column(db.Date)
    description = db.Column(db.String(500), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class ExchangeRate(db.Model):
    __tablename__ = 'exchange_rates'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    from_currency = db.Column(db.String(3), nullable=False)
    to_currency = db.Column(db.String(3), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'from_currency', 'to_currency', name='unique_rate'),
    )
