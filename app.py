from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    Response,
    send_file,
)
from dotenv import load_dotenv
load_dotenv()
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
from sqlalchemy import func
from functools import wraps
from twilio.rest import Client
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import os
import secrets
import io
import csv
import random  # for OTP

from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "change_this_to_a_random_secret_key"  # TODO: change in production

# Twilio SMS config - DO NOT hardcode real secrets
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
  # Your Twilio phone number

# --- SQLite DB in absolute path inside 'instance' folder --- #
# This builds:  <folder_where_app_py_is>/instance/smartdairy.db
INSTANCE_DIR = os.path.join(app.root_path, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "smartdairy.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# -------------------- DB PATH HELPER (for backup) -------------------- #

def get_db_path():
    """Return absolute filesystem path of SQLite DB."""
    return DB_PATH


# -------------------- MODELS -------------------- #

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    mobile = db.Column(db.String(20), unique=True, nullable=False)  # mobile number
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="user")  # 'admin' or 'user'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # for OTP-based reset
    reset_token = db.Column(db.String(100), nullable=True)        # will store OTP as string
    reset_expires_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<User {self.id} - {self.username}>"


class Buffalo(db.Model):
    __tablename__ = "buffaloes"

    id = db.Column(db.Integer, primary_key=True)
    tag_name = db.Column(db.String(100), nullable=False)
    animal_type = db.Column(db.String(20), nullable=False, default="buffalo")  # buffalo/cow
    age = db.Column(db.String(50))
    purchase_date = db.Column(db.Date)
    purchase_price = db.Column(db.Integer)
    seller_name = db.Column(db.String(100))
    seller_mobile = db.Column(db.String(20))
    status = db.Column(db.String(20), default="active")    # active/sold/dead
    notes = db.Column(db.Text)

    def __repr__(self):
        return f"<Buffalo {self.id} - {self.tag_name}>"


class Worker(db.Model):
    __tablename__ = "workers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mobile = db.Column(db.String(20), nullable=False)
    alt_mobile = db.Column(db.String(20))
    role = db.Column(db.String(100))
    bank_name = db.Column(db.String(100))
    account_number = db.Column(db.String(50))
    ifsc = db.Column(db.String(20))
    joining_date = db.Column(db.Date)
    salary_per_month = db.Column(db.Integer)
    status = db.Column(db.String(20), default="active")    # active/left
    notes = db.Column(db.Text)

    def __repr__(self):
        return f"<Worker {self.id} - {self.name}>"


class SalaryPayment(db.Model):
    __tablename__ = "salary_payments"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("workers.id"), nullable=False)
    month = db.Column(db.String(7), nullable=False)        # format: YYYY-MM (e.g. "2025-12")
    amount = db.Column(db.Integer, nullable=False)
    payment_mode = db.Column(db.String(20))                # cash / upi / bank
    transaction_ref = db.Column(db.String(100))
    date_paid = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)

    worker = db.relationship("Worker", backref="salary_payments")

    def __repr__(self):
        return f"<SalaryPayment {self.id} - worker {self.worker_id} - {self.month}>"


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text)

    def __repr__(self):
        return f"<Expense {self.id} - {self.category} - {self.amount}>"

class MilkRecord(db.Model):
    __tablename__ = "milk_records"

    id = db.Column(db.Integer, primary_key=True)
    buffalo_id = db.Column(db.Integer, db.ForeignKey("buffaloes.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    morning_litres = db.Column(db.Float)
    evening_litres = db.Column(db.Float)
    notes = db.Column(db.Text)

    buffalo = db.relationship("Buffalo", backref="milk_records")

    @property
    def total_litres(self):
        """Convenience property: morning + evening."""
        m = self.morning_litres or 0
        e = self.evening_litres or 0
        return m + e

    def __repr__(self):
        return f"<MilkRecord {self.id} - buffalo {self.buffalo_id} - {self.date}>"


with app.app_context():
    db.create_all()


# -------------------- AUTH HELPERS -------------------- #

def validate_password(password: str):
    """Return (ok, message)."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not has_letter or not has_digit:
        return False, "Password must contain at least one letter and one number."
    return True, ""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            next_url = request.path
            return redirect(url_for("login", next=next_url))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            # For admin-only pages, go to ADMIN login
            return redirect(url_for("admin_login", next=request.path))
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def generate_otp_code(length=6):
    """Generate a cryptographically secure 6-digit OTP."""
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))


def send_otp_via_mobile(mobile: str, otp: str):
    """
    Send OTP via SMS using Twilio.
    If Twilio is not configured, fallback to printing OTP in console.
    """
    # If config looks like placeholders, just print OTP (dev mode)
    if TWILIO_ACCOUNT_SID.startswith("your_") or TWILIO_AUTH_TOKEN.startswith("your_"):
        print(f"[DEBUG] OTP for {mobile} is: {otp}")
        return

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        # Ensure mobile has country code; assuming India here, auto-add +91 if missing
        if mobile.startswith("+"):
            to_number = mobile
        else:
            to_number = "+91" + mobile  # change if your country is different

        message = client.messages.create(
            body=f"Your SmartDairy OTP is {otp}. It is valid for 10 minutes.",
            from_=TWILIO_FROM_NUMBER,
            to=to_number,
        )

        # Optional debug
        print(f"[SMS] Sent OTP to {to_number}, Twilio SID: {message.sid}")

    except Exception as e:
        # In case SMS fails, log and still print OTP so you can debug
        print(f"[SMS ERROR] Failed to send OTP to {mobile}: {e}")
        print(f"[DEBUG FALLBACK] OTP for {mobile} is: {otp}")



# -------------------- DASHBOARD -------------------- #

@app.route("/")
@login_required
def dashboard():
    total_buffaloes = Buffalo.query.count()
    active_buffaloes = Buffalo.query.filter_by(status="active").count()

    active_workers_query = Worker.query.filter_by(status="active")
    total_workers = Worker.query.count()
    active_workers_count = active_workers_query.count()
    active_workers_list = active_workers_query.all()

    today = date.today()
    month_str = today.strftime("%Y-%m")        # e.g. 2025-12
    month_label = today.strftime("%B %Y")      # e.g. December 2025

    # This month's salary total
    month_salary_total = (
        db.session.query(func.coalesce(func.sum(SalaryPayment.amount), 0))
        .filter(SalaryPayment.month == month_str)
        .scalar()
    )

    # This month's expense total
    first_day = today.replace(day=1)
    if first_day.month == 12:
        next_month_first = date(first_day.year + 1, 1, 1)
    else:
        next_month_first = date(first_day.year, first_day.month + 1, 1)

    month_expense_total = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.date >= first_day, Expense.date < next_month_first)
        .scalar()
    )

    month_total_cost = (month_salary_total or 0) + (month_expense_total or 0)

    # --------- Salary Due (Partial + Full) for current month ---------- #
    payments = (
        db.session.query(
            SalaryPayment.worker_id,
            func.coalesce(func.sum(SalaryPayment.amount), 0)
        )
        .filter(SalaryPayment.month == month_str)
        .group_by(SalaryPayment.worker_id)
        .all()
    )
    payments_by_worker = {wid: total for wid, total in payments}

    due_workers = []
    for w in active_workers_list:
        if not w.salary_per_month:
            continue

        expected = w.salary_per_month or 0
        paid = payments_by_worker.get(w.id, 0) or 0
        due = expected - paid

        if due > 0:
            due_workers.append({
                "worker": w,
                "expected": expected,
                "paid": paid,
                "due": due,
            })

    due_count = len(due_workers)
    
    # ------------------------------------------------------------------ #

    # --------- LOW MILK ALERTS (LAST 7 DAYS AVERAGE vs TODAY) ---------- #
    try:
        lookback_days = 7
        today = date.today()
        start_period = today - timedelta(days=lookback_days)

        # Average daily milk per buffalo over last N days (excluding today)
        avg_rows = (
            db.session.query(
                MilkRecord.buffalo_id,
                func.avg(
                    (func.coalesce(MilkRecord.morning_litres, 0) +
                     func.coalesce(MilkRecord.evening_litres, 0))
                ).label("avg_total")
            )
            .filter(MilkRecord.date >= start_period, MilkRecord.date < today)
            .group_by(MilkRecord.buffalo_id)
            .all()
        )
        avg_by_buffalo = {row.buffalo_id: row.avg_total for row in avg_rows}

        # Today's records
        today_records = MilkRecord.query.filter_by(date=today).all()

        low_milk_alerts = []
        for r in today_records:
            avg = avg_by_buffalo.get(r.buffalo_id)
            if avg is None:
                continue  # no history → skip

            today_total = (r.morning_litres or 0) + (r.evening_litres or 0)
            # if today < 70% of average
            if today_total < 0.7 * avg:
                low_milk_alerts.append({
                    "buffalo": r.buffalo,
                    "today_total": today_total,
                    "avg_total": avg,
                })

        low_milk_count = len(low_milk_alerts)
    except Exception as e:
        print(f"[DEBUG] Low milk alert calc error: {e}")
        low_milk_alerts = []
        low_milk_count = 0
    # ------------------------------------------------------------------- #

    # --------- TOTAL MILK: TODAY + THIS MONTH ---------- #
    # Today total
    today = date.today()
    today_milk_total = (
        db.session.query(
            func.coalesce(
                func.sum(
                    func.coalesce(MilkRecord.morning_litres, 0) +
                    func.coalesce(MilkRecord.evening_litres, 0)
                ),
                0
            )
        )
        .filter(MilkRecord.date == today)
        .scalar()
    )

    # Month total
    first_day_milk = today.replace(day=1)
    if first_day_milk.month == 12:
        next_month_first_milk = date(first_day_milk.year + 1, 1, 1)
    else:
        next_month_first_milk = date(first_day_milk.year, first_day_milk.month + 1, 1)

    month_milk_total = (
        db.session.query(
            func.coalesce(
                func.sum(
                    func.coalesce(MilkRecord.morning_litres, 0) +
                    func.coalesce(MilkRecord.evening_litres, 0)
                ),
                0
            )
        )
        .filter(MilkRecord.date >= first_day_milk, MilkRecord.date < next_month_first_milk)
        .scalar()
    )
    # ---------------------------------------------------- #

    return render_template(
        "dashboard.html",
        total_buffaloes=total_buffaloes,
        active_buffaloes=active_buffaloes,
        total_workers=total_workers,
        active_workers=active_workers_count,
        month_label=month_label,
        month_salary_total=month_salary_total,
        month_expense_total=month_expense_total,
        month_total_cost=month_total_cost,
        month_str=month_str,
        due_workers=due_workers,
        due_count=due_count,
        low_milk_alerts=low_milk_alerts,
        low_milk_count=low_milk_count,
        today_milk_total=today_milk_total,
        month_milk_total=month_milk_total,
    )


# -------------------- BUFFALOES -------------------- #

@app.route("/buffaloes")
@login_required
def buffalo_list():
    search = request.args.get("search", "", type=str).lower().strip()
    filter_type = request.args.get("type", "", type=str)
    filter_status = request.args.get("status", "", type=str)
    download = request.args.get("download", "", type=str)

    buffaloes_query = Buffalo.query

    if filter_type:
        buffaloes_query = buffaloes_query.filter_by(animal_type=filter_type)
    if filter_status:
        buffaloes_query = buffaloes_query.filter_by(status=filter_status)

    buffaloes = buffaloes_query.all()

    if search:
        buffaloes = [
            b for b in buffaloes
            if search in (b.tag_name or "").lower()
            or search in (b.seller_name or "").lower()
            or search in f"b-{b.id}".lower()
        ]

    # ------------- CSV DOWNLOAD ------------- #
    if download == "csv":
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "ID",
            "Tag/Name",
            "Animal Type",
            "Age",
            "Purchase Date",
            "Purchase Price",
            "Seller Name",
            "Seller Mobile",
            "Status",
            "Notes",
        ])

        for b in buffaloes:
            writer.writerow([
                f"B-{b.id}",
                b.tag_name or "",
                b.animal_type or "",
                b.age or "",
                b.purchase_date.strftime("%Y-%m-%d") if b.purchase_date else "",
                b.purchase_price or "",
                b.seller_name or "",
                b.seller_mobile or "",
                b.status or "",
                (b.notes or "").replace("\n", " "),
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=buffaloes_filtered.csv"},
        )
    # ---------------------------------------- #

    return render_template(
        "buffalo_list.html",
        buffaloes=buffaloes,
        search=search,
        filter_type=filter_type,
        filter_status=filter_status,
    )


@app.route("/buffaloes/add", methods=["GET", "POST"])
@login_required
def buffalo_add():
    if request.method == "POST":
        tag_name = request.form.get("tag_name")
        animal_type = request.form.get("animal_type") or "buffalo"
        age = request.form.get("age")
        purchase_date_str = request.form.get("purchase_date")
        purchase_price = request.form.get("purchase_price") or None
        seller_name = request.form.get("seller_name")
        seller_mobile = request.form.get("seller_mobile")
        status = request.form.get("status") or "active"
        notes = request.form.get("notes")

        if not tag_name:
            flash("Tag/Name is required", "error")
            return redirect(url_for("buffalo_add"))

        purchase_date = None
        if purchase_date_str:
            try:
                purchase_date = datetime.strptime(purchase_date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid purchase date", "error")

        buffalo = Buffalo(
            tag_name=tag_name,
            animal_type=animal_type,
            age=age,
            purchase_date=purchase_date,
            purchase_price=int(purchase_price) if purchase_price else None,
            seller_name=seller_name,
            seller_mobile=seller_mobile,
            status=status,
            notes=notes,
        )
        db.session.add(buffalo)
        db.session.commit()
        flash("Buffalo saved successfully!", "success")
        return redirect(url_for("buffalo_list"))

    return render_template("buffalo_form.html", buffalo=None)


@app.route("/buffaloes/<int:buffalo_id>/edit", methods=["GET", "POST"])
@login_required
def buffalo_edit(buffalo_id):
    buffalo = Buffalo.query.get_or_404(buffalo_id)

    if request.method == "POST":
        buffalo.tag_name = request.form.get("tag_name")
        buffalo.animal_type = request.form.get("animal_type") or "buffalo"
        buffalo.age = request.form.get("age")
        purchase_date_str = request.form.get("purchase_date")
        buffalo.purchase_price = (
            int(request.form.get("purchase_price"))
            if request.form.get("purchase_price") else None
        )
        buffalo.seller_name = request.form.get("seller_name")
        buffalo.seller_mobile = request.form.get("seller_mobile")
        buffalo.status = request.form.get("status") or "active"
        buffalo.notes = request.form.get("notes")

        if purchase_date_str:
            try:
                buffalo.purchase_date = datetime.strptime(purchase_date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid purchase date", "error")

        if not buffalo.tag_name:
            flash("Tag/Name is required", "error")
            return redirect(url_for("buffalo_edit", buffalo_id=buffalo.id))

        db.session.commit()
        flash("Buffalo updated successfully!", "success")
        return redirect(url_for("buffalo_list"))

    return render_template("buffalo_form.html", buffalo=buffalo)


@app.route("/buffaloes/<int:buffalo_id>/delete", methods=["POST"])
@admin_required
def buffalo_delete(buffalo_id):
    buffalo = Buffalo.query.get_or_404(buffalo_id)
    db.session.delete(buffalo)
    db.session.commit()
    flash("Buffalo record deleted.", "success")
    return redirect(url_for("buffalo_list"))


@app.route("/buffaloes/<int:buffalo_id>")
@login_required
def buffalo_detail(buffalo_id):
    buffalo = Buffalo.query.get_or_404(buffalo_id)
    return render_template("buffalo_detail.html", buffalo=buffalo)

@app.route("/buffaloes/<int:buffalo_id>/milk-summary")
@login_required
def buffalo_milk_summary(buffalo_id):
    buffalo = Buffalo.query.get_or_404(buffalo_id)

    # Aggregate stats for this buffalo
    q = (
        db.session.query(
            func.min(MilkRecord.date),
            func.max(MilkRecord.date),
            func.coalesce(
                func.sum(
                    func.coalesce(MilkRecord.morning_litres, 0) +
                    func.coalesce(MilkRecord.evening_litres, 0)
                ),
                0
            ),
            func.count(func.distinct(MilkRecord.date))
        )
        .filter(MilkRecord.buffalo_id == buffalo_id)
    )

    first_date, last_date, total_litres, days_count = q.one()

    if days_count and days_count > 0:
        avg_per_day = total_litres / days_count
    else:
        avg_per_day = 0

    # All records for table
    records = (
        MilkRecord.query
        .filter_by(buffalo_id=buffalo_id)
        .order_by(MilkRecord.date.asc())
        .all()
    )

    return render_template(
        "buffalo_milk_summary.html",
        buffalo=buffalo,
        first_date=first_date,
        last_date=last_date,
        total_litres=total_litres,
        days_count=days_count,
        avg_per_day=avg_per_day,
        records=records,
    )


# -------------------- WORKERS -------------------- #

@app.route("/workers")
@login_required
def worker_list():
    search = request.args.get("search", "", type=str).lower().strip()
    filter_status = request.args.get("status", "", type=str)
    download = request.args.get("download", "", type=str)

    workers_query = Worker.query
    if filter_status:
        workers_query = workers_query.filter_by(status=filter_status)

    workers = workers_query.all()

    if search:
        workers = [
            w for w in workers
            if search in (w.name or "").lower()
            or search in (w.mobile or "").lower()
            or search in f"w-{w.id}".lower()
        ]

    # ------------- CSV DOWNLOAD ------------- #
    if download == "csv":
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "ID",
            "Name",
            "Mobile",
            "Alt Mobile",
            "Role",
            "Bank Name",
            "Account Number",
            "IFSC",
            "Joining Date",
            "Salary Per Month",
            "Status",
            "Notes",
        ])

        for w in workers:
            writer.writerow([
                f"W-{w.id}",
                w.name or "",
                w.mobile or "",
                w.alt_mobile or "",
                w.role or "",
                w.bank_name or "",
                w.account_number or "",
                w.ifsc or "",
                w.joining_date.strftime("%Y-%m-%d") if w.joining_date else "",
                w.salary_per_month or "",
                w.status or "",
                (w.notes or "").replace("\n", " "),
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=workers_filtered.csv"},
        )
    # ---------------------------------------- #

    return render_template(
        "worker_list.html",
        workers=workers,
        search=search,
        filter_status=filter_status,
    )


@app.route("/workers/add", methods=["GET", "POST"])
@login_required
def worker_add():
    if request.method == "POST":
        name = request.form.get("name")
        mobile = request.form.get("mobile")
        alt_mobile = request.form.get("alt_mobile")
        role = request.form.get("role")
        bank_name = request.form.get("bank_name")
        account_number = request.form.get("account_number")
        ifsc = request.form.get("ifsc")
        joining_date_str = request.form.get("joining_date")
        salary_per_month = request.form.get("salary_per_month") or None
        status = request.form.get("status") or "active"
        notes = request.form.get("notes")

        if not name or not mobile:
            flash("Name and mobile are required", "error")
            return redirect(url_for("worker_add"))

        joining_date = None
        if joining_date_str:
            try:
                joining_date = datetime.strptime(joining_date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid joining date", "error")

        worker = Worker(
            name=name,
            mobile=mobile,
            alt_mobile=alt_mobile,
            role=role,
            bank_name=bank_name,
            account_number=account_number,
            ifsc=ifsc,
            joining_date=joining_date,
            salary_per_month=int(salary_per_month) if salary_per_month else None,
            status=status,
            notes=notes,
        )
        db.session.add(worker)
        db.session.commit()
        flash("Worker saved successfully!", "success")
        return redirect(url_for("worker_list"))

    return render_template("worker_form.html", worker=None, default_joining="")


@app.route("/workers/<int:worker_id>/edit", methods=["GET", "POST"])
@login_required
def worker_edit(worker_id):
    worker = Worker.query.get_or_404(worker_id)

    if request.method == "POST":
        worker.name = request.form.get("name")
        worker.mobile = request.form.get("mobile")
        worker.alt_mobile = request.form.get("alt_mobile")
        worker.role = request.form.get("role")
        worker.bank_name = request.form.get("bank_name")
        worker.account_number = request.form.get("account_number")
        worker.ifsc = request.form.get("ifsc")
        joining_date_str = request.form.get("joining_date")
        salary_per_month = request.form.get("salary_per_month")
        worker.status = request.form.get("status") or "active"
        worker.notes = request.form.get("notes")

        if not worker.name or not worker.mobile:
            flash("Name and mobile are required", "error")
            return redirect(url_for("worker_edit", worker_id=worker.id))

        if joining_date_str:
            try:
                worker.joining_date = datetime.strptime(joining_date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid joining date", "error")

        worker.salary_per_month = int(salary_per_month) if salary_per_month else None

        db.session.commit()
        flash("Worker updated successfully!", "success")
        return redirect(url_for("worker_list"))

    default_joining = worker.joining_date.strftime("%Y-%m-%d") if worker.joining_date else ""

    return render_template(
        "worker_form.html",
        worker=worker,
        default_joining=default_joining,
    )


@app.route("/workers/<int:worker_id>/delete", methods=["POST"])
@admin_required
def worker_delete(worker_id):
    worker = Worker.query.get_or_404(worker_id)
    db.session.delete(worker)
    db.session.commit()
    flash("Worker record deleted.", "success")
    return redirect(url_for("worker_list"))


@app.route("/workers/<int:worker_id>")
@login_required
def worker_detail(worker_id):
    worker = Worker.query.get_or_404(worker_id)
    return render_template("worker_detail.html", worker=worker)


# -------------------- SALARIES -------------------- #

@app.route("/salaries")
@login_required
def salary_list():
    """Show salary payments, with optional filter by month & CSV download."""
    month = request.args.get("month", "", type=str)
    download = request.args.get("download", "", type=str)

    query = SalaryPayment.query.join(Worker)
    if month:
        query = query.filter(SalaryPayment.month == month)

    payments = query.order_by(SalaryPayment.date_paid.desc()).all()

    months = (
        db.session.query(SalaryPayment.month)
        .distinct()
        .order_by(SalaryPayment.month.desc())
        .all()
    )
    month_values = [m[0] for m in months]

    # ------------- CSV DOWNLOAD ------------- #
    if download == "csv":
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Payment ID",
            "Worker ID",
            "Worker Name",
            "Month",
            "Amount",
            "Payment Mode",
            "Transaction Ref",
            "Date Paid",
            "Notes",
        ])

        for p in payments:
            writer.writerow([
                p.id,
                f"W-{p.worker.id}" if p.worker else "",
                p.worker.name if p.worker else "",
                p.month,
                p.amount,
                p.payment_mode or "",
                p.transaction_ref or "",
                p.date_paid.strftime("%Y-%m-%d") if p.date_paid else "",
                (p.notes or "").replace("\n", " "),
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=salaries_filtered.csv"},
        )
    # ---------------------------------------- #

    return render_template(
        "salary_list.html",
        payments=payments,
        month=month,
        month_values=month_values,
    )


@app.route("/salaries/add", methods=["GET", "POST"])
@login_required
def salary_add():
    workers = Worker.query.filter_by(status="active").order_by(Worker.name).all()

    if request.method == "POST":
        worker_id = request.form.get("worker_id")
        month = request.form.get("month")
        amount = request.form.get("amount")
        payment_mode = request.form.get("payment_mode")
        transaction_ref = request.form.get("transaction_ref")
        date_paid_str = request.form.get("date_paid")
        notes = request.form.get("notes")

        if not worker_id or not month or not amount or not date_paid_str:
            flash("Worker, month, amount and date paid are required.", "error")
            return redirect(url_for("salary_add"))

        try:
            date_paid = datetime.strptime(date_paid_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid paid date.", "error")
            return redirect(url_for("salary_add"))

        payment = SalaryPayment(
            worker_id=int(worker_id),
            month=month,
            amount=int(amount),
            payment_mode=payment_mode,
            transaction_ref=transaction_ref,
            date_paid=date_paid,
            notes=notes,
        )
        db.session.add(payment)
        db.session.commit()
        flash("Salary payment recorded successfully!", "success")
        return redirect(url_for("salary_list"))

    default_month = datetime.now().strftime("%Y-%m")
    default_date = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "salary_form.html",
        workers=workers,
        default_month=default_month,
        default_date=default_date,
        payment=None,
    )


@app.route("/salaries/<int:payment_id>/edit", methods=["GET", "POST"])
@login_required
def salary_edit(payment_id):
    payment = SalaryPayment.query.get_or_404(payment_id)
    workers = Worker.query.order_by(Worker.name).all()

    if request.method == "POST":
        worker_id = request.form.get("worker_id")
        month = request.form.get("month")
        amount = request.form.get("amount")
        payment_mode = request.form.get("payment_mode")
        transaction_ref = request.form.get("transaction_ref")
        date_paid_str = request.form.get("date_paid")
        notes = request.form.get("notes")

        if not worker_id or not month or not amount or not date_paid_str:
            flash("Worker, month, amount and date paid are required.", "error")
            return redirect(url_for("salary_edit", payment_id=payment.id))

        try:
            payment.date_paid = datetime.strptime(date_paid_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid paid date.", "error")
            return redirect(url_for("salary_edit", payment_id=payment.id))

        payment.worker_id = int(worker_id)
        payment.month = month
        payment.amount = int(amount)
        payment.payment_mode = payment_mode
        payment.transaction_ref = transaction_ref
        payment.notes = notes

        db.session.commit()
        flash("Salary payment updated successfully!", "success")
        return redirect(url_for("salary_list"))

    return render_template(
        "salary_form.html",
        workers=workers,
        payment=payment,
        default_month=payment.month,
        default_date=payment.date_paid.strftime("%Y-%m-%d"),
    )


@app.route("/salaries/<int:payment_id>/delete", methods=["POST"])
@admin_required
def salary_delete(payment_id):
    payment = SalaryPayment.query.get_or_404(payment_id)
    db.session.delete(payment)
    db.session.commit()
    flash("Salary payment deleted.", "success")
    return redirect(url_for("salary_list"))


# -------------------- SALARY REPORT -------------------- #

@app.route("/reports/salary")
@login_required
def salary_report():
    """Monthly salary status: expected, paid, due per active worker."""
    month = request.args.get("month", "", type=str)

    if not month:
        month = date.today().strftime("%Y-%m")

    months = (
        db.session.query(SalaryPayment.month)
        .distinct()
        .order_by(SalaryPayment.month.desc())
        .all()
    )
    month_values = [m[0] for m in months]

    try:
        month_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        month_label = month

    active_workers = Worker.query.filter_by(status="active").order_by(Worker.name).all()

    payments = (
        db.session.query(
            SalaryPayment.worker_id,
            func.coalesce(func.sum(SalaryPayment.amount), 0)
        )
        .filter(SalaryPayment.month == month)
        .group_by(SalaryPayment.worker_id)
        .all()
    )
    payments_by_worker = {wid: total for wid, total in payments}

    rows = []
    total_expected = 0
    total_paid = 0
    total_due = 0

    for w in active_workers:
        expected = w.salary_per_month or 0
        paid = payments_by_worker.get(w.id, 0) or 0
        due = max(expected - paid, 0)

        if w.salary_per_month is None:
            status = "Salary not set"
        elif expected == 0:
            status = "Salary set as 0"
        elif paid == 0:
            status = "Not Paid"
        elif paid < expected:
            status = "Partially Paid"
        else:
            status = "Paid"

        rows.append({
            "worker": w,
            "expected": expected,
            "paid": paid,
            "due": due,
            "status": status,
        })

        total_expected += expected
        total_paid += paid
        total_due += due

    return render_template(
        "salary_report.html",
        month=month,
        month_values=month_values,
        month_label=month_label,
        rows=rows,
        total_expected=total_expected,
        total_paid=total_paid,
        total_due=total_due,
    )


# -------------------- EXPENSES -------------------- #

@app.route("/expenses")
@login_required
def expense_list():
    """List expenses with optional category filter & CSV download."""
    category = request.args.get("category", "", type=str)
    download = request.args.get("download", "", type=str)

    query = Expense.query.order_by(Expense.date.desc())
    if category:
        query = query.filter(Expense.category == category)

    expenses = query.all()

    categories = (
        db.session.query(Expense.category)
        .distinct()
        .order_by(Expense.category.asc())
        .all()
    )
    category_values = [c[0] for c in categories]

    # ------------- CSV DOWNLOAD ------------- #
    if download == "csv":
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "ID",
            "Date",
            "Category",
            "Amount",
            "Description",
        ])

        for e in expenses:
            writer.writerow([
                e.id,
                e.date.strftime("%Y-%m-%d") if e.date else "",
                e.category or "",
                e.amount or "",
                (e.description or "").replace("\n", " "),
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=expenses_filtered.csv"},
        )
    # ---------------------------------------- #

    return render_template(
        "expense_list.html",
        expenses=expenses,
        category=category,
        category_values=category_values,
    )

# -------------------- MILK RECORDS -------------------- #

@app.route("/milk-records")
@login_required
def milk_record_list():
    """
    List milk records, with optional filters by date, buffalo,
    and support CSV/PDF download.
    """
    date_str = request.args.get("date", "", type=str)
    buffalo_id = request.args.get("buffalo_id", "", type=str)
    download = request.args.get("download", "", type=str)

    query = MilkRecord.query.join(Buffalo).order_by(MilkRecord.date.desc(), Buffalo.tag_name.asc())

    # Filter by date (YYYY-MM-DD)
    filter_date = None
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            query = query.filter(MilkRecord.date == filter_date)
        except ValueError:
            flash("Invalid date filter.", "error")

    # Filter by buffalo
    selected_buffalo_id = None
    if buffalo_id:
        try:
            selected_buffalo_id = int(buffalo_id)
            query = query.filter(MilkRecord.buffalo_id == selected_buffalo_id)
        except ValueError:
            flash("Invalid buffalo filter.", "error")

    records = query.all()

    # For filter dropdown
    buffaloes = Buffalo.query.order_by(Buffalo.tag_name.asc()).all()

    # ---------------- CSV DOWNLOAD ---------------- #
    if download == "csv":
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Date",
            "Buffalo ID",
            "Buffalo Tag",
            "Animal Type",
            "Morning Litres",
            "Evening Litres",
            "Total Litres",
            "Notes",
        ])

        for r in records:
            total = (r.morning_litres or 0) + (r.evening_litres or 0)
            writer.writerow([
                r.date.strftime("%Y-%m-%d"),
                f"B-{r.buffalo.id}" if r.buffalo else "",
                r.buffalo.tag_name if r.buffalo else "",
                r.buffalo.animal_type if r.buffalo else "",
                r.morning_litres or 0,
                r.evening_litres or 0,
                total,
                (r.notes or "").replace("\n", " "),
            ])

        csv_data = output.getvalue()
        output.close()

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=milk_records_filtered.csv"},
        )
    # ---------------- PDF DOWNLOAD ---------------- #
    if download == "pdf":
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        y = height - 40

        # Title
        p.setFont("Helvetica-Bold", 14)
        p.drawString(40, y, "Milk Records (Filtered)")
        y -= 20

        # Filters info
        p.setFont("Helvetica", 9)
        if filter_date:
            p.drawString(40, y, f"Date filter: {filter_date.strftime('%Y-%m-%d')}")
            y -= 12
        if selected_buffalo_id:
            # find buffalo name
            b = Buffalo.query.get(selected_buffalo_id)
            if b:
                p.drawString(40, y, f"Buffalo: B-{b.id} • {b.tag_name} ({b.animal_type})")
                y -= 12

        y -= 10

        # Table header
        p.setFont("Helvetica-Bold", 9)
        p.drawString(40,  y, "Date")
        p.drawString(110, y, "Buffalo")
        p.drawString(230, y, "M (L)")
        p.drawString(280, y, "E (L)")
        p.drawString(330, y, "Total")
        p.drawString(380, y, "Notes")
        y -= 14

        p.setFont("Helvetica", 8)

        for r in records:
            if y < 40:
                p.showPage()
                y = height - 40
                p.setFont("Helvetica-Bold", 9)
                p.drawString(40,  y, "Date")
                p.drawString(110, y, "Buffalo")
                p.drawString(230, y, "M (L)")
                p.drawString(280, y, "E (L)")
                p.drawString(330, y, "Total")
                p.drawString(380, y, "Notes")
                y -= 14
                p.setFont("Helvetica", 8)

            total = (r.morning_litres or 0) + (r.evening_litres or 0)
            buffalo_label = ""
            if r.buffalo:
                buffalo_label = f"B-{r.buffalo.id} {r.buffalo.tag_name}"

            p.drawString(40,  y, r.date.strftime("%Y-%m-%d"))
            p.drawString(110, y, buffalo_label[:18])
            p.drawRightString(260, y, f"{(r.morning_litres or 0):.2f}")
            p.drawRightString(310, y, f"{(r.evening_litres or 0):.2f}")
            p.drawRightString(360, y, f"{total:.2f}")
            p.drawString(380, y, (r.notes or "")[:40])
            y -= 12

        p.showPage()
        p.save()
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="milk_records_filtered.pdf",
            mimetype="application/pdf",
        )
    # ------------------------------------------------ #

    return render_template(
        "milk_list.html",
        records=records,
        buffaloes=buffaloes,
        date_str=date_str,
        selected_buffalo_id=buffalo_id,
    )

@app.route("/milk-records/add", methods=["GET", "POST"])
@login_required
def milk_record_add():
    buffaloes = Buffalo.query.filter_by(status="active").order_by(Buffalo.tag_name.asc()).all()

    if request.method == "POST":
        buffalo_id = request.form.get("buffalo_id")
        date_str = request.form.get("date")
        morning_litres = request.form.get("morning_litres") or None
        evening_litres = request.form.get("evening_litres") or None
        notes = request.form.get("notes")

        if not buffalo_id or not date_str:
            flash("Buffalo and date are required.", "error")
            return redirect(url_for("milk_record_add"))

        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("milk_record_add"))

        record = MilkRecord(
            buffalo_id=int(buffalo_id),
            date=d,
            morning_litres=float(morning_litres) if morning_litres else None,
            evening_litres=float(evening_litres) if evening_litres else None,
            notes=notes,
        )
        db.session.add(record)
        db.session.commit()
        flash("Milk record added.", "success")
        return redirect(url_for("milk_record_list"))

    default_date = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "milk_form.html",
        buffaloes=buffaloes,
        record=None,
        default_date=default_date,
    )


@app.route("/milk-records/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def milk_record_edit(record_id):
    record = MilkRecord.query.get_or_404(record_id)
    buffaloes = Buffalo.query.order_by(Buffalo.tag_name.asc()).all()

    if request.method == "POST":
        buffalo_id = request.form.get("buffalo_id")
        date_str = request.form.get("date")
        morning_litres = request.form.get("morning_litres") or None
        evening_litres = request.form.get("evening_litres") or None
        notes = request.form.get("notes")

        if not buffalo_id or not date_str:
            flash("Buffalo and date are required.", "error")
            return redirect(url_for("milk_record_edit", record_id=record.id))

        try:
            record.date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("milk_record_edit", record_id=record.id))

        record.buffalo_id = int(buffalo_id)
        record.morning_litres = float(morning_litres) if morning_litres else None
        record.evening_litres = float(evening_litres) if evening_litres else None
        record.notes = notes

        db.session.commit()
        flash("Milk record updated.", "success")
        return redirect(url_for("milk_record_list"))

    default_date = record.date.strftime("%Y-%m-%d")

    return render_template(
        "milk_form.html",
        buffaloes=buffaloes,
        record=record,
        default_date=default_date,
    )


@app.route("/milk-records/<int:record_id>/delete", methods=["POST"])
@admin_required
def milk_record_delete(record_id):
    record = MilkRecord.query.get_or_404(record_id)
    db.session.delete(record)
    db.session.commit()
    flash("Milk record deleted.", "success")
    return redirect(url_for("milk_record_list"))


@app.route("/expenses/add", methods=["GET", "POST"])
@login_required
def expense_add():
    if request.method == "POST":
        date_str = request.form.get("date")
        category = request.form.get("category")
        amount = request.form.get("amount")
        description = request.form.get("description")

        if not date_str or not category or not amount:
            flash("Date, category and amount are required.", "error")
            return redirect(url_for("expense_add"))

        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("expense_add"))

        expense = Expense(
            date=date_val,
            category=category,
            amount=int(amount),
            description=description,
        )
        db.session.add(expense)
        db.session.commit()
        flash("Expense recorded successfully!", "success")
        return redirect(url_for("expense_list"))

    default_date = datetime.now().strftime("%Y-%m-%d")
    base_categories = [
        "Feed",
        "Medicine / Vet",
        "Electricity",
        "Transport",
        "Maintenance",
        "Other",
    ]

    return render_template(
        "expense_form.html",
        default_date=default_date,
        base_categories=base_categories,
        expense=None,
    )


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def expense_edit(expense_id):
    expense = Expense.query.get_or_404(expense_id)

    if request.method == "POST":
        date_str = request.form.get("date")
        category = request.form.get("category")
        amount = request.form.get("amount")
        description = request.form.get("description")

        if not date_str or not category or not amount:
            flash("Date, category and amount are required.", "error")
            return redirect(url_for("expense_edit", expense_id=expense.id))

        try:
            expense.date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("expense_edit", expense_id=expense.id))

        expense.category = category
        expense.amount = int(amount)
        expense.description = description

        db.session.commit()
        flash("Expense updated successfully!", "success")
        return redirect(url_for("expense_list"))

    default_date = expense.date.strftime("%Y-%m-%d")
    base_categories = [
        "Feed",
        "Medicine / Vet",
        "Electricity",
        "Transport",
        "Maintenance",
        "Other",
    ]

    return render_template(
        "expense_form.html",
        default_date=default_date,
        base_categories=base_categories,
        expense=expense,
    )


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@admin_required
def expense_delete(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    db.session.delete(expense)
    db.session.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("expense_list"))


# -------------------- BACKUP & RESTORE (ADMIN) -------------------- #

@app.route("/backup/download")
@admin_required
def backup_download():
    """Download the current SQLite database file."""
    db_path = get_db_path()

    if not os.path.exists(db_path):
        flash("Database file not found.", "error")
        return redirect(url_for("dashboard"))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"smartdairy-backup-{stamp}.db"

    return send_file(
        db_path,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/backup", methods=["GET", "POST"])
@admin_required
def backup_page():
    """
    Admin page to download backup and restore from uploaded .db file.
    WARNING: restore will overwrite current data.
    """
    if request.method == "POST":
        file = request.files.get("db_file")

        if not file or file.filename == "":
            flash("Please choose a .db or .sqlite file to upload.", "error")
            return redirect(url_for("backup_page"))

        if not (file.filename.endswith(".db") or file.filename.endswith(".sqlite")):
            flash("Invalid file type. Upload a .db or .sqlite file.", "error")
            return redirect(url_for("backup_page"))

        db_path = get_db_path()
        tmp_path = db_path + ".upload_tmp"

        # Save uploaded file temporarily
        file.save(tmp_path)

        # Close DB session before replacing
        db.session.remove()

        try:
            os.replace(tmp_path, db_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            flash(f"Restore failed: {e}", "error")
            return redirect(url_for("backup_page"))

        flash("Database restored successfully. Please reload pages.", "success")
        return redirect(url_for("dashboard"))

    return render_template("backup.html")


# -------------------- AUTH ROUTES -------------------- #

@app.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in, go to dashboard
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    next_url = request.args.get("next") or url_for("dashboard")

    if request.method == "POST":
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        ).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username/email or password.", "error")
            return redirect(url_for("login", next=next_url))

        # Only allow 'user' role here
        if user.role != "user":
            flash("This is an admin account. Please use Admin Login.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user.id
        session["username"] = user.username
        session["role"] = user.role

        flash("Logged in successfully as user.", "success")
        return redirect(next_url)

    return render_template("login.html", next_url=next_url)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    # If already logged in as admin, go dashboard
    if session.get("user_id") and session.get("role") == "admin":
        return redirect(url_for("dashboard"))

    next_url = request.args.get("next") or url_for("dashboard")

    if request.method == "POST":
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        ).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username/email or password.", "error")
            return redirect(url_for("admin_login", next=next_url))

        # Only allow 'admin' role here
        if user.role != "admin":
            flash("This is not an admin account.", "error")
            return redirect(url_for("admin_login"))

        session["user_id"] = user.id
        session["username"] = user.username
        session["role"] = user.role

        flash("Logged in successfully as admin.", "success")
        return redirect(next_url)

    return render_template("admin_login.html", next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not email or not mobile or not password:
            flash("Username, email, mobile and password are required.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        ok, msg = validate_password(password)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("register"))

        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "error")
            return redirect(url_for("register"))

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for("register"))

        if User.query.filter_by(mobile=mobile).first():
            flash("Mobile number already registered.", "error")
            return redirect(url_for("register"))

        # First user becomes admin
        is_first_user = User.query.count() == 0
        role = "admin" if is_first_user else "user"

        user = User(
            username=username,
            email=email,
            mobile=mobile,
            password_hash=generate_password_hash(password),
            role=role,
        )
        db.session.add(user)
        db.session.commit()
        flash("Account created successfully. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


# --------- OTP-BASED PASSWORD RESET (MOBILE) --------- #

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """
    Step 1: User enters mobile number. We generate OTP and send via SMS/WhatsApp (placeholder).
    """
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()

        if not mobile:
            flash("Please enter your registered mobile number.", "error")
            return redirect(url_for("forgot_password"))

        user = User.query.filter_by(mobile=mobile).first()
        # Do not reveal whether user exists or not for security
        if not user:
            flash("If this mobile is registered, an OTP has been sent.", "info")
            return redirect(url_for("forgot_password"))

        otp = generate_otp_code(6)
        user.reset_token = otp
        user.reset_expires_at = datetime.utcnow() + timedelta(minutes=10)
        db.session.commit()

        # send OTP via SMS/WhatsApp gateway (placeholder prints to console)
        send_otp_via_mobile(user.mobile, otp)

        flash("If this mobile is registered, an OTP has been sent.", "info")
        return redirect(url_for("reset_password_otp", mobile=mobile))

    return render_template("forgot_password_mobile.html")


@app.route("/reset-password-otp", methods=["GET", "POST"])
def reset_password_otp():
    """
    Step 2: User enters mobile, OTP and new password.
    """
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        otp = request.form.get("otp", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not mobile or not otp or not password:
            flash("Mobile, OTP and new password are required.", "error")
            return redirect(url_for("reset_password_otp", mobile=mobile))

        user = User.query.filter_by(mobile=mobile).first()
        if not user:
            flash("Invalid mobile or OTP.", "error")
            return redirect(url_for("reset_password_otp"))

        # Check OTP
        if not user.reset_token or user.reset_token != otp:
            flash("Invalid OTP.", "error")
            return redirect(url_for("reset_password_otp", mobile=mobile))

        if not user.reset_expires_at or user.reset_expires_at < datetime.utcnow():
            flash("OTP expired. Please request a new one.", "error")
            return redirect(url_for("forgot_password"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password_otp", mobile=mobile))

        ok, msg = validate_password(password)
        if not ok:
            flash(msg, "error")
            return redirect(url_for("reset_password_otp", mobile=mobile))

        # All good → update password and clear OTP
        user.password_hash = generate_password_hash(password)
        user.reset_token = None
        user.reset_expires_at = None
        db.session.commit()

        flash("Password updated successfully. Please login.", "success")
        return redirect(url_for("login"))

    mobile_prefill = request.args.get("mobile", "")
    return render_template("reset_password_otp.html", mobile=mobile_prefill)


# (Old /reset-password/<token> route is no longer needed)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/account/delete", methods=["GET", "POST"])
@login_required
def account_delete():
    user_id = session.get("user_id")
    user = User.query.get_or_404(user_id)

    # Admin should NOT be able to delete own account
    if user.role == "admin":
        flash("Admin account cannot be deleted. Use admin panel to manage users.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")

        # Confirm password
        if not check_password_hash(user.password_hash, password):
            flash("Incorrect password. Account not deleted.", "error")
            return redirect(url_for("account_delete"))

        # Delete this user
        db.session.delete(user)
        db.session.commit()

        # Clear session
        session.clear()
        flash("Your account has been deleted.", "success")
        return redirect(url_for("login"))

    return render_template("account_delete.html", user=user)


@app.route("/admin/users")
@admin_required
def admin_users():
    """Admin: see all users."""
    users = User.query.order_by(User.created_at.asc()).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    """Admin: delete a normal user account."""
    user = User.query.get_or_404(user_id)

    # Do not allow deleting any admin account here
    if user.role == "admin":
        flash("Admin accounts cannot be deleted from this page.", "error")
        return redirect(url_for("admin_users"))

    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' deleted successfully.", "success")
    return redirect(url_for("admin_users"))


if __name__ == "__main__":
    app.run(debug=True)
