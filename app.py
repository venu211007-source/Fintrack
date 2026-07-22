import os
import json
import stripe
import requests as req_http
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import extract, text

from models import db, User, Account, Transaction, Asset, Liability, ExchangeRate, UploadLog, PayeeRule, Budget
from parsers import parse_bank_statement, get_column_preview, process_transactions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fintrack-secret-key-change-before-deploy-2024')

# Railway provides DATABASE_URL for PostgreSQL; fall back to local SQLite
_db_url = os.environ.get('DATABASE_URL', '')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url or ('sqlite:///' + os.path.join(BASE_DIR, 'financial.db'))
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
ALLOWED_EXT = {'csv', 'xlsx', 'xls', 'pdf'}

# Stripe config (set these env vars on Railway)
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
APP_URL = os.environ.get('APP_URL', 'http://localhost:5000').rstrip('/')

FREE_ACCOUNT_LIMIT = 3
FREE_UPLOAD_LIMIT = 5

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def _migrate_db():
    """Add new columns to existing tables without losing data. Safe to run multiple times."""
    migrations = [
        "ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR(100)",
        "ALTER TABLE users ADD COLUMN stripe_subscription_id VARCHAR(100)",
        "ALTER TABLE users ADD COLUMN premium_until TIMESTAMP",
    ]
    with db.engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()


# Ensure tables exist and schema is up to date every startup (works under gunicorn too)
with app.app_context():
    db.create_all()
    _migrate_db()


@app.after_request
def no_cache(response):
    if current_user.is_authenticated:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ---------- helpers ----------

def get_rate(user_id, from_cur, to_cur):
    if from_cur == to_cur:
        return 1.0
    r = ExchangeRate.query.filter_by(user_id=user_id, from_currency=from_cur, to_currency=to_cur).first()
    if r:
        return r.rate
    r2 = ExchangeRate.query.filter_by(user_id=user_id, from_currency=to_cur, to_currency=from_cur).first()
    if r2 and r2.rate:
        return 1.0 / r2.rate
    return 1.0


def to_base(amount, from_cur, user):
    return amount * get_rate(user.id, from_cur, user.base_currency)


def missing_rates_warning(user):
    """Return list of currencies that have no exchange rate configured."""
    foreign_currencies = (
        db.session.query(Account.currency)
        .filter(Account.user_id == user.id, Account.currency != user.base_currency)
        .distinct()
        .all()
    )
    missing = []
    for (cur,) in foreign_currencies:
        if get_rate(user.id, cur, user.base_currency) == 1.0:
            missing.append(cur)
    return missing


def _uploads_this_month(user_id):
    now = datetime.utcnow()
    return UploadLog.query.filter(
        UploadLog.user_id == user_id,
        extract('month', UploadLog.created_at) == now.month,
        extract('year', UploadLog.created_at) == now.year
    ).count()


def fetch_live_rates(user):
    """
    Pull latest exchange rates from Frankfurter (ECB data, free, no API key).
    Updates ExchangeRate rows for all foreign currencies the user has.
    Returns {'updated': [...], 'errors': [...], 'date': 'YYYY-MM-DD'}.
    """
    foreign = list({row[0] for row in
                    db.session.query(Account.currency)
                    .filter(Account.user_id == user.id,
                            Account.currency != user.base_currency)
                    .all()})
    if not foreign:
        return {'updated': [], 'errors': [], 'date': None}

    url = (f'https://api.frankfurter.app/latest'
           f'?from={user.base_currency}&to={",".join(foreign)}')
    try:
        resp = req_http.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {'updated': [], 'errors': [str(exc)], 'date': None}

    api_rates = data.get('rates', {})
    rate_date  = data.get('date', '')
    now        = datetime.utcnow()
    updated, errors = [], []

    for cur in foreign:
        if cur not in api_rates:
            errors.append(cur)
            continue

        # Frankfurter: 1 base = api_rates[cur] foreign
        # We store foreign→base so to_base() can multiply directly
        rate_val = round(1.0 / api_rates[cur], 8)

        existing = ExchangeRate.query.filter_by(
            user_id=user.id, from_currency=cur, to_currency=user.base_currency
        ).first()
        if existing:
            existing.rate       = rate_val
            existing.updated_at = now
        else:
            db.session.add(ExchangeRate(
                user_id=user.id, from_currency=cur,
                to_currency=user.base_currency, rate=rate_val,
            ))
        updated.append({'from': cur, 'to': user.base_currency,
                        'rate': rate_val, 'date': rate_date})

    db.session.commit()
    return {'updated': updated, 'errors': errors, 'date': rate_date}


def _rates_are_stale(user, max_hours=20):
    """True if any foreign-currency rate is missing or older than max_hours."""
    foreign = [row[0] for row in
               db.session.query(Account.currency)
               .filter(Account.user_id == user.id,
                       Account.currency != user.base_currency)
               .distinct().all()]
    if not foreign:
        return False
    cutoff = datetime.utcnow() - timedelta(hours=max_hours)
    for cur in foreign:
        r = ExchangeRate.query.filter_by(
            user_id=user.id, from_currency=cur, to_currency=user.base_currency
        ).first()
        if not r or r.updated_at < cutoff:
            return True
    return False


def month_summary(user, month, year):
    ts = (Transaction.query
          .join(Account)
          .filter(Account.user_id == user.id,
                  extract('month', Transaction.date) == month,
                  extract('year', Transaction.date) == year,
                  Transaction.is_internal_transfer == False)
          .all())
    income = sum(to_base(t.amount, t.account.currency, user) for t in ts if t.amount > 0)
    expense = sum(abs(to_base(t.amount, t.account.currency, user)) for t in ts if t.amount < 0)
    return round(income, 2), round(expense, 2)


# ---------- auth ----------

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=bool(request.form.get('remember')))
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').lower().strip()
        pw = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        base_cur = request.form.get('base_currency', 'USD').upper()

        if not name or not email or not pw:
            flash('All fields are required.', 'danger')
            return render_template('register.html')
        if not request.form.get('consent'):
            flash('You must agree to the Privacy Policy and Terms of Service to create an account.', 'danger')
            return render_template('register.html')
        if pw != pw2:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html')
        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return render_template('register.html')

        user = User(name=name, email=email,
                    password_hash=generate_password_hash(pw),
                    base_currency=base_cur)
        db.session.add(user)
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))


# ---------- dashboard ----------

def _latest_data_month(user_id):
    """Return (month, year) of the most recent transaction, or current month if none."""
    row = (Transaction.query.join(Account)
           .filter(Account.user_id == user_id)
           .order_by(Transaction.date.desc())
           .first())
    if row:
        return row.date.month, row.date.year
    now = datetime.now()
    return now.month, now.year


@app.route('/dashboard')
@login_required
def dashboard():
    # Silently refresh stale exchange rates (runs fast, <300ms typically)
    if _rates_are_stale(current_user):
        try:
            fetch_live_rates(current_user)
        except Exception:
            pass  # never crash the dashboard if the API is down

    now = datetime.now()
    accounts = Account.query.filter_by(user_id=current_user.id).all()

    # Use current month, but fall back to latest month with actual data
    disp_month, disp_year = now.month, now.year
    cur_inc, cur_exp = month_summary(current_user, disp_month, disp_year)
    if cur_inc == 0 and cur_exp == 0:
        disp_month, disp_year = _latest_data_month(current_user.id)
        cur_inc, cur_exp = month_summary(current_user, disp_month, disp_year)

    # net worth
    acc_total = sum(to_base(a.balance, a.currency, current_user) for a in accounts)
    assets = Asset.query.filter_by(user_id=current_user.id).all()
    liabilities = Liability.query.filter_by(user_id=current_user.id).all()
    asset_total = acc_total + sum(to_base(a.value, a.currency, current_user) for a in assets)
    liab_total = sum(to_base(l.balance, l.currency, current_user) for l in liabilities)
    net_worth = asset_total - liab_total

    # last 6 months chart anchored at disp_month/disp_year
    from calendar import monthrange
    monthly_chart = []
    for i in range(5, -1, -1):
        # step back i months from disp_month/disp_year
        m = disp_month - i
        y = disp_year
        while m <= 0:
            m += 12
            y -= 1
        inc, exp = month_summary(current_user, m, y)
        from datetime import date as _date
        monthly_chart.append({
            'month': _date(y, m, 1).strftime('%b %Y'),
            'income': inc, 'expense': exp
        })

    # category breakdown for displayed month
    ts_month = (Transaction.query.join(Account)
                .filter(Account.user_id == current_user.id,
                        extract('month', Transaction.date) == disp_month,
                        extract('year', Transaction.date) == disp_year,
                        Transaction.is_internal_transfer == False,
                        Transaction.amount < 0)
                .all())
    cats = {}
    for t in ts_month:
        cat = t.category or 'Uncategorized'
        cats[cat] = round(cats.get(cat, 0) + abs(to_base(t.amount, t.account.currency, current_user)), 2)

    recent = (Transaction.query.join(Account)
              .filter(Account.user_id == current_user.id)
              .order_by(Transaction.date.desc())
              .limit(10).all())

    from datetime import date as _date
    return render_template('dashboard.html',
                           accounts=accounts,
                           total_income=cur_inc,
                           total_expense=cur_exp,
                           net_month=round(cur_inc - cur_exp, 2),
                           asset_total=round(asset_total, 2),
                           liab_total=round(liab_total, 2),
                           net_worth=round(net_worth, 2),
                           monthly_chart=json.dumps(monthly_chart),
                           cats=json.dumps(cats),
                           recent=recent,
                           current_month=_date(disp_year, disp_month, 1).strftime('%B %Y'),
                           missing_rates=missing_rates_warning(current_user))


# ---------- accounts ----------

@app.route('/accounts')
@login_required
def accounts():
    accs = Account.query.filter_by(user_id=current_user.id).all()
    return render_template('accounts.html', accounts=accs)


@app.route('/accounts/add', methods=['POST'])
@login_required
def add_account():
    if not current_user.is_premium:
        existing = Account.query.filter_by(user_id=current_user.id).count()
        if existing >= FREE_ACCOUNT_LIMIT:
            flash(f'Free plan allows up to {FREE_ACCOUNT_LIMIT} accounts. '
                  f'<a href="{url_for("pricing")}">Upgrade to Premium</a> for unlimited accounts.', 'warning')
            return redirect(url_for('accounts'))
    name = request.form.get('name', '').strip()
    bank = request.form.get('bank_name', '').strip()
    if not name or not bank:
        flash('Account name and bank name are required.', 'danger')
        return redirect(url_for('accounts'))
    acc = Account(
        user_id=current_user.id,
        name=name,
        bank_name=bank,
        account_number=request.form.get('account_number', '').strip(),
        currency=request.form.get('currency', 'USD').upper().strip(),
        country=request.form.get('country', '').strip(),
        account_type=request.form.get('account_type', 'checking'),
        balance=float(request.form.get('balance', 0) or 0),
    )
    db.session.add(acc)
    db.session.commit()
    flash('Account added.', 'success')
    return redirect(url_for('accounts'))


@app.route('/accounts/<int:aid>/edit', methods=['POST'])
@login_required
def edit_account(aid):
    acc = Account.query.filter_by(id=aid, user_id=current_user.id).first_or_404()
    acc.name = request.form.get('name', acc.name).strip()
    acc.bank_name = request.form.get('bank_name', acc.bank_name).strip()
    acc.balance = float(request.form.get('balance', acc.balance) or acc.balance)
    acc.currency = request.form.get('currency', acc.currency).upper().strip()
    acc.country = request.form.get('country', acc.country).strip()
    acc.account_type = request.form.get('account_type', acc.account_type)
    db.session.commit()
    flash('Account updated.', 'success')
    return redirect(url_for('accounts'))


@app.route('/accounts/<int:aid>/delete', methods=['POST'])
@login_required
def delete_account(aid):
    acc = Account.query.filter_by(id=aid, user_id=current_user.id).first_or_404()
    db.session.delete(acc)
    db.session.commit()
    flash('Account deleted.', 'success')
    return redirect(url_for('accounts'))


# ---------- upload ----------

@app.route('/upload')
@login_required
def upload():
    accs = Account.query.filter_by(user_id=current_user.id).all()
    uploads_used = _uploads_this_month(current_user.id)
    return render_template('upload.html', accounts=accs,
                           uploads_used=uploads_used,
                           upload_limit=FREE_UPLOAD_LIMIT)


@app.route('/upload/preview', methods=['POST'])
@login_required
def upload_preview():
    if not current_user.is_premium:
        used = _uploads_this_month(current_user.id)
        if used >= FREE_UPLOAD_LIMIT:
            return jsonify({'error': (
                f'You have used all {FREE_UPLOAD_LIMIT} uploads for this month on the free plan. '
                f'Upgrade to Premium for unlimited uploads.'
            )}), 403
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    f = request.files['file']
    if not f.filename or not allowed_file(f.filename):
        return jsonify({'error': 'Unsupported file. Use CSV, XLSX, or PDF.'}), 400

    fname = f'{current_user.id}_{secure_filename(f.filename)}'
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
    f.save(fpath)

    try:
        df, mapping = parse_bank_statement(fpath)
        preview = get_column_preview(df)
        preview['mapping'] = mapping
        preview['filepath'] = fpath
        return jsonify(preview)
    except Exception as e:
        if os.path.exists(fpath):
            os.remove(fpath)
        msg = str(e) or 'Could not read this PDF. It may be password-protected or in an unsupported format. Try downloading the statement as CSV from your bank.'
        return jsonify({'error': msg}), 400


@app.route('/upload/import', methods=['POST'])
@login_required
def import_transactions():
    data = request.get_json()
    fpath = data.get('filepath', '')
    account_id = data.get('account_id')
    col_map = data.get('column_mapping')

    if not fpath or not account_id:
        return jsonify({'error': 'Missing filepath or account.'}), 400
    if not os.path.exists(fpath):
        return jsonify({'error': 'Upload file not found. Please upload again.'}), 400

    acc = Account.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()

    # Enforce upload quota at import time (not just at preview time)
    if not current_user.is_premium and _uploads_this_month(current_user.id) >= FREE_UPLOAD_LIMIT:
        return jsonify({'error': f'Monthly upload limit reached ({FREE_UPLOAD_LIMIT} uploads). Upgrade to Premium for unlimited uploads.'}), 403

    try:
        from parsers import extract_upi_payee, llm_categorize_batch
        df, mapping = parse_bank_statement(fpath, col_map)
        rows = process_transactions(df, mapping)

        # ── Pass 1: PayeeRule overrides (never touches user-corrected categories) ──
        payee_rules = {
            pr.payee_key: pr.category
            for pr in PayeeRule.query.filter_by(user_id=current_user.id).all()
        }
        if payee_rules:
            for r in rows:
                pk, _ = extract_upi_payee(r['description'])
                if pk and pk in payee_rules:
                    r['category'] = payee_rules[pk]
                else:
                    desc_key = r['description'].lower().strip()[:100]
                    if desc_key in payee_rules:
                        r['category'] = payee_rules[desc_key]

        # ── Pass 2: LLM batch for anything still Uncategorized ──────────────────
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if api_key:
            unc_idx = [i for i, r in enumerate(rows) if r['category'] == 'Uncategorized']
            if unc_idx:
                llm_cats = llm_categorize_batch(
                    [rows[i]['description'] for i in unc_idx], api_key
                )
                for i, cat in zip(unc_idx, llm_cats):
                    rows[i]['category'] = cat

        # ── Pass 3: Insert (dedup check) ────────────────────────────────────────
        imported = skipped = 0
        for r in rows:
            exists = Transaction.query.filter_by(
                account_id=acc.id, date=r['date'],
                amount=r['amount'], description=r['description']
            ).first()
            if exists:
                skipped += 1
                continue
            db.session.add(Transaction(
                account_id=acc.id,
                date=r['date'],
                description=r['description'],
                amount=r['amount'],
                amount_base=to_base(r['amount'], acc.currency, current_user),
                category=r['category'],
                transaction_type=r['transaction_type'],
            ))
            imported += 1

        db.session.commit()
        if os.path.exists(fpath):
            os.remove(fpath)

        db.session.add(UploadLog(user_id=current_user.id))
        db.session.commit()

        _detect_transfers(current_user.id)
        return jsonify({'success': True, 'imported': imported, 'skipped': skipped})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


def _detect_transfers(user_id):
    user = db.session.get(User, user_id)
    accs = {a.id: a for a in Account.query.filter_by(user_id=user_id).all()}

    ts = (Transaction.query.join(Account)
          .filter(Account.user_id == user_id,
                  Transaction.is_internal_transfer == False,
                  Transaction.transfer_pair_id == None)
          .order_by(Transaction.date)
          .all())

    matched = set()
    for i, t1 in enumerate(ts):
        if t1.id in matched:
            continue
        # Always convert on-the-fly using current exchange rates
        a1_base = to_base(t1.amount, accs[t1.account_id].currency, user)

        for t2 in ts[i + 1:]:
            if t2.id in matched or t1.account_id == t2.account_id:
                continue
            # Stop scanning forward if date gap > 5 days
            if (t2.date - t1.date).days > 5:
                break

            a2_base = to_base(t2.amount, accs[t2.account_id].currency, user)

            # Must be opposite signs (one debit, one credit)
            if a1_base * a2_base >= 0:
                continue

            # Percentage tolerance (5%) handles FX spread + bank fees across currencies
            larger = max(abs(a1_base), abs(a2_base))
            if larger == 0:
                continue
            if abs(abs(a1_base) - abs(a2_base)) / larger <= 0.05:
                t1.is_internal_transfer = True
                t2.is_internal_transfer = True
                t1.transfer_pair_id = t2.id
                t2.transfer_pair_id = t1.id
                # Update stored base amounts to use current rates
                t1.amount_base = a1_base
                t2.amount_base = a2_base
                matched.update([t1.id, t2.id])
                break
    db.session.commit()


# ---------- transactions ----------

@app.route('/transactions')
@login_required
def transactions():
    page        = request.args.get('page', 1, type=int)
    date_from   = request.args.get('date_from', '').strip()
    date_to     = request.args.get('date_to', '').strip()
    account_id  = request.args.get('account_id', type=int)
    show_transfers = request.args.get('show_transfers', '') == '1'
    cat_filter  = request.args.get('category', '')

    # Back-compat: old month/year params convert to date range
    _month = request.args.get('month', type=int)
    _year  = request.args.get('year', type=int)
    if _month and _year and not date_from and not date_to:
        import calendar
        last_day = calendar.monthrange(_year, _month)[1]
        date_from = f'{_year}-{_month:02d}-01'
        date_to   = f'{_year}-{_month:02d}-{last_day:02d}'

    q = Transaction.query.join(Account).filter(Account.user_id == current_user.id)

    d_from = None
    d_to   = None
    try:
        if date_from:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            q = q.filter(Transaction.date >= d_from)
    except ValueError:
        date_from = ''
    try:
        if date_to:
            d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            q = q.filter(Transaction.date <= d_to)
    except ValueError:
        date_to = ''

    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if not show_transfers:
        q = q.filter(Transaction.is_internal_transfer == False)
    if cat_filter:
        q = q.filter(Transaction.category == cat_filter)

    # Summary over the full filtered set (before pagination)
    all_txns      = q.all()
    sum_income    = sum(t.amount_base or 0 for t in all_txns if (t.amount_base or 0) > 0)
    sum_expense   = sum(abs(t.amount_base or 0) for t in all_txns if (t.amount_base or 0) < 0)
    sum_net       = sum_income - sum_expense
    has_filter    = bool(date_from or date_to or account_id or cat_filter)

    paged = q.order_by(Transaction.date.desc()).paginate(page=page, per_page=25, error_out=False)
    accs  = Account.query.filter_by(user_id=current_user.id).all()

    return render_template('transactions.html',
                           transactions=paged, accounts=accs,
                           date_from=date_from,
                           date_to=date_to,
                           sel_account=account_id,
                           show_transfers=show_transfers,
                           cat_filter=cat_filter,
                           missing_rates=missing_rates_warning(current_user),
                           sum_income=sum_income,
                           sum_expense=sum_expense,
                           sum_net=sum_net,
                           has_filter=has_filter,
                           sel_month=None,
                           sel_year=None)


@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    d = request.get_json()
    acc = Account.query.filter_by(id=d.get('account_id'), user_id=current_user.id).first_or_404()
    amount = float(d.get('amount', 0))
    if d.get('is_expense', True):
        amount = -abs(amount)
    else:
        amount = abs(amount)
    date = datetime.strptime(d['date'], '%Y-%m-%d').date()
    t = Transaction(
        account_id=acc.id,
        date=date,
        description=d.get('description', ''),
        amount=amount,
        amount_base=to_base(amount, acc.currency, current_user),
        category=d.get('category', 'Uncategorized'),
        transaction_type='expense' if amount < 0 else 'income',
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'success': True, 'id': t.id})


@app.route('/transactions/<int:tid>/update', methods=['POST'])
@login_required
def update_transaction(tid):
    t = (Transaction.query.join(Account)
         .filter(Transaction.id == tid, Account.user_id == current_user.id)
         .first_or_404())
    d = request.get_json()
    for field in ('category', 'notes', 'description'):
        if field in d:
            setattr(t, field, d[field])
    if 'is_internal_transfer' in d:
        t.is_internal_transfer = bool(d['is_internal_transfer'])
    db.session.commit()

    # If the client asks to remember this payee → category mapping
    if d.get('remember_payee') and d.get('category'):
        from parsers import extract_upi_payee
        pk, plabel = extract_upi_payee(t.description)
        if not pk:
            # Non-UPI: use first 100 chars of description as key
            pk = t.description.lower().strip()[:100]
            plabel = t.description[:100]
        if pk:
            existing = PayeeRule.query.filter_by(
                user_id=current_user.id, payee_key=pk).first()
            if existing:
                existing.category = d['category']
            else:
                db.session.add(PayeeRule(
                    user_id=current_user.id,
                    payee_key=pk,
                    payee_label=plabel or pk,
                    category=d['category'],
                ))
            db.session.commit()

    return jsonify({'success': True})


@app.route('/transactions/<int:tid>/delete', methods=['POST'])
@login_required
def delete_transaction(tid):
    t = (Transaction.query.join(Account)
         .filter(Transaction.id == tid, Account.user_id == current_user.id)
         .first_or_404())
    db.session.delete(t)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/payee-rules')
@login_required
def list_payee_rules():
    rules = PayeeRule.query.filter_by(user_id=current_user.id).order_by(PayeeRule.created_at.desc()).all()
    return jsonify([{
        'id': r.id, 'payee_key': r.payee_key,
        'payee_label': r.payee_label or r.payee_key,
        'category': r.category,
    } for r in rules])


@app.route('/api/payee-rules/<int:rid>/delete', methods=['POST'])
@login_required
def delete_payee_rule(rid):
    r = PayeeRule.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    db.session.delete(r)
    db.session.commit()
    return jsonify({'success': True})


# ---------- budgets ----------

def _detect_recurring(user_id, min_months=2):
    """Return a list of recurring payment patterns detected from transaction history."""
    import re as _re
    from collections import defaultdict

    txns = (Transaction.query.join(Account)
            .filter(Account.user_id == user_id,
                    Transaction.amount < 0,
                    Transaction.is_internal_transfer == False)
            .order_by(Transaction.date)
            .all())

    def _norm(desc):
        d = _re.sub(r'\d{6,}', '', (desc or '').lower())
        d = _re.sub(r'upi/[a-zA-Z0-9/\-]+', 'upi', d)
        d = _re.sub(r'[^a-z\s]', ' ', d)
        return ' '.join(d.split())[:50]

    # Group by (normalized description, amount rounded to nearest 10)
    groups = defaultdict(list)
    for t in txns:
        key = (_norm(t.description), round(abs(t.amount) / 10) * 10)
        groups[key].append(t)

    recurring = []
    for (desc_key, _), group in groups.items():
        months = {(t.date.year, t.date.month) for t in group}
        if len(months) < min_months:
            continue
        amounts = [abs(t.amount) for t in group]
        avg_amt = sum(amounts) / len(amounts)
        last_t = max(group, key=lambda t: t.date)
        dates = sorted(t.date for t in group)
        if len(dates) >= 2:
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            avg_gap = sum(gaps) / len(gaps)
            next_date = last_t.date + timedelta(days=round(avg_gap))
        else:
            next_date = None
        recurring.append({
            'description': last_t.description[:55],
            'category': last_t.category,
            'avg_amount': round(avg_amt, 2),
            'min_amount': round(min(amounts), 2),
            'max_amount': round(max(amounts), 2),
            'occurrences': len(group),
            'months_seen': len(months),
            'last_date': last_t.date,
            'next_date': next_date,
            'currency': last_t.account.currency,
        })

    recurring.sort(key=lambda x: x['avg_amount'], reverse=True)
    return recurring[:25]


@app.route('/budgets')
@login_required
def budgets():
    now = datetime.utcnow()
    # Actual spend this month per category (expenses only, excluding transfers)
    month_txns = (Transaction.query.join(Account)
                  .filter(Account.user_id == current_user.id,
                          Transaction.amount < 0,
                          Transaction.is_internal_transfer == False,
                          extract('month', Transaction.date) == now.month,
                          extract('year',  Transaction.date) == now.year)
                  .all())
    spent = {}
    for t in month_txns:
        cat = t.category or 'Uncategorized'
        spent[cat] = round(spent.get(cat, 0) + abs(to_base(t.amount, t.account.currency, current_user)), 2)

    user_budgets = Budget.query.filter_by(user_id=current_user.id).order_by(Budget.category).all()
    recurring = _detect_recurring(current_user.id)

    from datetime import date as _date
    return render_template('budgets.html',
                           budgets=user_budgets,
                           spent=spent,
                           recurring=recurring,
                           today=_date.today(),
                           current_month=now.strftime('%B %Y'))


@app.route('/api/budgets', methods=['POST'])
@login_required
def upsert_budget():
    d = request.get_json()
    category = (d.get('category') or '').strip()
    try:
        amount = float(d.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid amount.'}), 400
    if not category or amount <= 0:
        return jsonify({'error': 'Category and a positive amount are required.'}), 400

    existing = Budget.query.filter_by(user_id=current_user.id, category=category).first()
    if existing:
        existing.amount = amount
    else:
        db.session.add(Budget(user_id=current_user.id, category=category, amount=amount))
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/budgets/<int:bid>/delete', methods=['POST'])
@login_required
def delete_budget(bid):
    b = Budget.query.filter_by(id=bid, user_id=current_user.id).first_or_404()
    db.session.delete(b)
    db.session.commit()
    return jsonify({'success': True})


# ---------- reports ----------

@app.route('/reports')
@login_required
def reports():
    yr_rows = (db.session.query(extract('year', Transaction.date).label('y'))
               .join(Account).filter(Account.user_id == current_user.id)
               .distinct().all())
    available_years = sorted({int(r.y) for r in yr_rows if r.y}, reverse=True) or [datetime.now().year]

    # Default to latest year with data, not necessarily current year
    default_year = available_years[0]
    year = request.args.get('year', default_year, type=int)

    monthly = []
    for m in range(1, 13):
        inc, exp = month_summary(current_user, m, year)
        monthly.append({
            'month': datetime(year, m, 1).strftime('%B'),
            'month_num': m, 'income': inc, 'expense': exp,
            'net': round(inc - exp, 2)
        })

    return render_template('reports.html', monthly=monthly, year=year,
                           available_years=available_years,
                           monthly_json=json.dumps(monthly))


@app.route('/reports/month/<int:year>/<int:month>')
@login_required
def month_detail(year, month):
    ts = (Transaction.query.join(Account)
          .filter(Account.user_id == current_user.id,
                  extract('month', Transaction.date) == month,
                  extract('year', Transaction.date) == year,
                  Transaction.is_internal_transfer == False)
          .order_by(Transaction.date)
          .all())
    cats = {}
    for t in ts:
        if t.amount < 0:
            c = t.category or 'Uncategorized'
            cats[c] = round(cats.get(c, 0) + abs(to_base(t.amount, t.account.currency, current_user)), 2)

    income = sum(to_base(t.amount, t.account.currency, current_user) for t in ts if t.amount > 0)
    expense = sum(abs(to_base(t.amount, t.account.currency, current_user)) for t in ts if t.amount < 0)
    return jsonify({
        'income': round(income, 2), 'expense': round(expense, 2),
        'net': round(income - expense, 2), 'categories': cats,
        'transactions': [{'date': t.date.isoformat(), 'description': t.description,
                          'amount': t.amount, 'category': t.category,
                          'account': t.account.name} for t in ts]
    })


# ---------- net worth ----------

@app.route('/networth')
@login_required
def networth():
    accs = Account.query.filter_by(user_id=current_user.id).all()
    assets = Asset.query.filter_by(user_id=current_user.id).all()
    liabs = Liability.query.filter_by(user_id=current_user.id).all()

    acc_total = sum(to_base(a.balance, a.currency, current_user) for a in accs)
    asset_total = sum(to_base(a.value, a.currency, current_user) for a in assets)
    liab_total = sum(to_base(l.balance, l.currency, current_user) for l in liabs)
    total_assets = acc_total + asset_total
    nw = total_assets - liab_total

    return render_template('networth.html',
                           accounts=accs, assets=assets, liabilities=liabs,
                           acc_total=round(acc_total, 2),
                           asset_total=round(asset_total, 2),
                           liab_total=round(liab_total, 2),
                           total_assets=round(total_assets, 2),
                           net_worth=round(nw, 2))


@app.route('/networth/asset/add', methods=['POST'])
@login_required
def add_asset():
    d = request.get_json()
    a = Asset(user_id=current_user.id, name=d['name'],
              value=float(d.get('value', 0)),
              currency=d.get('currency', current_user.base_currency).upper(),
              asset_type=d.get('asset_type', 'other'),
              description=d.get('description', ''))
    db.session.add(a)
    db.session.commit()
    return jsonify({'success': True, 'id': a.id})


@app.route('/networth/asset/<int:aid>/delete', methods=['POST'])
@login_required
def delete_asset(aid):
    a = Asset.query.filter_by(id=aid, user_id=current_user.id).first_or_404()
    db.session.delete(a)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/networth/liability/add', methods=['POST'])
@login_required
def add_liability():
    d = request.get_json()
    l = Liability(user_id=current_user.id, name=d['name'],
                  balance=float(d.get('balance', 0)),
                  currency=d.get('currency', current_user.base_currency).upper(),
                  liability_type=d.get('liability_type', 'other'),
                  interest_rate=float(d.get('interest_rate', 0) or 0),
                  description=d.get('description', ''))
    db.session.add(l)
    db.session.commit()
    return jsonify({'success': True, 'id': l.id})


@app.route('/networth/liability/<int:lid>/delete', methods=['POST'])
@login_required
def delete_liability(lid):
    l = Liability.query.filter_by(id=lid, user_id=current_user.id).first_or_404()
    db.session.delete(l)
    db.session.commit()
    return jsonify({'success': True})


# ---------- settings ----------

@app.route('/settings')
@login_required
def settings():
    rates = ExchangeRate.query.filter_by(user_id=current_user.id).order_by(ExchangeRate.updated_at.desc()).all()
    rates_stale = _rates_are_stale(current_user)
    return render_template('settings.html', rates=rates, rates_stale=rates_stale)


@app.route('/settings/update', methods=['POST'])
@login_required
def update_settings():
    current_user.base_currency = request.form.get('base_currency', 'USD').upper()
    db.session.commit()
    flash('Settings saved.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/rate/add', methods=['POST'])
@login_required
def add_rate():
    d = request.get_json()
    fc = d.get('from_currency', '').upper()
    tc = d.get('to_currency', '').upper()
    rate = float(d.get('rate', 1))
    existing = ExchangeRate.query.filter_by(user_id=current_user.id,
                                             from_currency=fc, to_currency=tc).first()
    if existing:
        existing.rate = rate
        existing.updated_at = datetime.utcnow()
    else:
        db.session.add(ExchangeRate(user_id=current_user.id,
                                    from_currency=fc, to_currency=tc, rate=rate))
    db.session.commit()
    return jsonify({'success': True})


@app.route('/settings/rate/<int:rid>/delete', methods=['POST'])
@login_required
def delete_rate(rid):
    r = ExchangeRate.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    db.session.delete(r)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/settings/fetch-rates', methods=['POST'])
@login_required
def fetch_rates_route():
    """Fetch live rates from Frankfurter, then recalculate all transaction base amounts."""
    result = fetch_live_rates(current_user)

    if result['updated']:
        # Recalculate all base amounts with the new rates
        accs = {a.id: a for a in Account.query.filter_by(user_id=current_user.id).all()}
        for t in Transaction.query.filter(Transaction.account_id.in_(accs.keys())).all():
            t.amount_base = to_base(t.amount, accs[t.account_id].currency, current_user)
        db.session.commit()

    return jsonify(result)


@app.route('/settings/recalculate', methods=['POST'])
@login_required
def recalculate_rates():
    accs = {a.id: a for a in Account.query.filter_by(user_id=current_user.id).all()}
    ts = Transaction.query.filter(Transaction.account_id.in_(accs.keys())).all()
    for t in ts:
        t.amount_base = to_base(t.amount, accs[t.account_id].currency, current_user)
        # Reset transfer flags so detection reruns cleanly
        t.is_internal_transfer = False
        t.transfer_pair_id = None
    db.session.commit()
    _detect_transfers(current_user.id)
    flash(f'Recalculated exchange rates for {len(ts)} transactions and re-detected transfers.', 'success')
    return redirect(url_for('settings'))


# ---------- pricing ----------

@app.route('/pricing')
def pricing():
    uploads_used = _uploads_this_month(current_user.id) if current_user.is_authenticated else 0
    accounts_used = Account.query.filter_by(user_id=current_user.id).count() if current_user.is_authenticated else 0
    return render_template('pricing.html',
                           stripe_key=STRIPE_PUBLISHABLE_KEY,
                           stripe_configured=bool(stripe.api_key and STRIPE_PRICE_ID),
                           uploads_used=uploads_used,
                           accounts_used=accounts_used)


# ---------- stripe checkout ----------

@app.route('/checkout/start', methods=['POST'])
@login_required
def checkout_start():
    if not stripe.api_key or not STRIPE_PRICE_ID:
        flash('Payment system is not set up yet. Please contact support.', 'warning')
        return redirect(url_for('pricing'))
    if current_user.is_premium:
        flash('You already have Premium!', 'info')
        return redirect(url_for('dashboard'))

    customer_id = current_user.stripe_customer_id
    if not customer_id:
        try:
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.name,
                metadata={'user_id': str(current_user.id)}
            )
            current_user.stripe_customer_id = customer.id
            db.session.commit()
            customer_id = customer.id
        except stripe.error.StripeError as e:
            flash(f'Could not start checkout: {e.user_message}', 'danger')
            return redirect(url_for('pricing'))

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            mode='subscription',
            success_url=APP_URL + url_for('checkout_success') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=APP_URL + url_for('checkout_cancel'),
        )
    except stripe.error.StripeError as e:
        flash(f'Checkout error: {e.user_message}', 'danger')
        return redirect(url_for('pricing'))

    return redirect(session.url, code=303)


@app.route('/checkout/success')
@login_required
def checkout_success():
    session_id = request.args.get('session_id')
    if session_id and stripe.api_key:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            if sess.subscription:
                sub = stripe.Subscription.retrieve(sess.subscription)
                current_user.is_premium = True
                current_user.stripe_subscription_id = sub.id
                current_user.stripe_customer_id = current_user.stripe_customer_id or sess.customer
                current_user.premium_until = datetime.fromtimestamp(sub['current_period_end'])
                db.session.commit()
        except Exception as e:
            app.logger.error(f'Stripe success handler error: {e}')
    flash('Welcome to FinTrack Premium! All limits are now unlocked.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/checkout/cancel')
@login_required
def checkout_cancel():
    flash('Payment cancelled. You can upgrade any time from the Pricing page.', 'info')
    return redirect(url_for('pricing'))


# ---------- stripe webhook ----------

def _handle_checkout_completed(session):
    customer_id = session.get('customer')
    subscription_id = session.get('subscription')
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if not user:
        user = User.query.filter_by(email=session.get('customer_details', {}).get('email')).first()
    if user and subscription_id:
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
            user.is_premium = True
            user.stripe_subscription_id = subscription_id
            user.stripe_customer_id = customer_id
            user.premium_until = datetime.fromtimestamp(sub['current_period_end'])
            db.session.commit()
        except Exception as e:
            app.logger.error(f'Webhook checkout completed error: {e}')


def _handle_subscription_event(sub):
    user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
    if not user:
        user = User.query.filter_by(stripe_customer_id=sub.get('customer')).first()
    if not user:
        return
    if sub['status'] in ('canceled', 'unpaid', 'incomplete_expired'):
        user.is_premium = False
        user.stripe_subscription_id = None
    elif sub['status'] == 'active':
        user.is_premium = True
        user.premium_until = datetime.fromtimestamp(sub['current_period_end'])
    db.session.commit()


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook secret not configured'}), 400
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({'error': 'Invalid payload or signature'}), 400

    etype = event['type']
    obj = event['data']['object']
    if etype == 'checkout.session.completed':
        _handle_checkout_completed(obj)
    elif etype in ('customer.subscription.updated', 'customer.subscription.deleted'):
        _handle_subscription_event(obj)
    elif etype == 'invoice.payment_failed':
        cid = obj.get('customer')
        user = User.query.filter_by(stripe_customer_id=cid).first()
        if user:
            app.logger.warning(f'Payment failed for user {user.id} ({user.email})')

    return jsonify({'received': True})


# ---------- billing portal ----------

@app.route('/billing/portal', methods=['POST'])
@login_required
def billing_portal():
    if not current_user.stripe_customer_id or not stripe.api_key:
        flash('Billing portal is not available. Contact support.', 'warning')
        return redirect(url_for('settings'))
    try:
        portal = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=APP_URL + url_for('settings')
        )
        return redirect(portal.url, code=303)
    except stripe.error.StripeError as e:
        flash(f'Could not open billing portal: {e.user_message}', 'danger')
        return redirect(url_for('settings'))


if __name__ == '__main__':
    app.run(debug=False, port=5000)
