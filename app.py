from functools import wraps
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    Response,
    flash,
    session,
)
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import json
import csv
import io
import os

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "expense-tracker-development-key",
)

DATABASE = "finance.db"

# Transactions are stored in KES.
# Override this in production with an environment variable when needed.
USD_KES_RATE = float(os.environ.get("USD_KES_RATE", "130.0"))

SUPPORTED_CURRENCIES = {
    "KES": {
        "symbol": "KSh",
        "rate": 1.0,
    },
    "USD": {
        "symbol": "$",
        "rate": 1 / USD_KES_RATE,
    },
}


def get_currency_details(currency_code):
    return SUPPORTED_CURRENCIES.get(
        currency_code,
        SUPPORTED_CURRENCIES["KES"],
    )


def convert_amount(amount, currency_code):
    details = get_currency_details(currency_code)
    return float(amount or 0) * details["rate"]


@app.template_filter("format_date")
def format_date(value):
    if not value:
        return "Unknown date"

    try:
        parsed_date = datetime.strptime(
            str(value),
            "%Y-%m-%d %H:%M:%S",
        )
        return parsed_date.strftime("%d %b %Y, %I:%M %p")
    except ValueError:
        return value


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            date_created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            date_created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )

    user_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    }

    if "currency" not in user_columns:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN currency TEXT NOT NULL DEFAULT 'KES'
            """
        )

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(transactions)"
        ).fetchall()
    }

    if "user_id" not in columns:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN user_id INTEGER"
        )

    conn.commit()
    conn.close()


init_db()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped_view


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if len(username) < 2:
            flash(
                "Please enter a name with at least 2 characters.",
                "danger",
            )
            return redirect(url_for("register"))

        if len(password) < 6:
            flash(
                "Your password must contain at least 6 characters.",
                "danger",
            )
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("The passwords do not match.", "danger")
            return redirect(url_for("register"))

        conn = get_db()

        existing_user = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if existing_user is not None:
            conn.close()
            flash(
                "That username is already registered.",
                "danger",
            )
            return redirect(url_for("register"))

        cursor = conn.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (?, ?)
            """,
            (
                username,
                generate_password_hash(password),
            ),
        )

        user_id = cursor.lastrowid

        # Keep transactions made before login was added.
        conn.execute(
            """
            UPDATE transactions
            SET user_id = ?
            WHERE user_id IS NULL
            """,
            (user_id,),
        )

        conn.commit()
        conn.close()

        session.clear()
        session["user_id"] = user_id
        session["username"] = username
        session["currency"] = "KES"

        flash(
            f"Welcome, {username}! Your account is ready.",
            "success",
        )
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()

        user = conn.execute(
            """
            SELECT id, username, password_hash, currency
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        conn.close()

        if user is None or not check_password_hash(
            user["password_hash"],
            password,
        ):
            flash(
                "Incorrect username or password.",
                "danger",
            )
            return redirect(url_for("login"))

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["currency"] = user["currency"] or "KES"

        flash(
            f"Welcome back, {user['username']}!",
            "success",
        )
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    username = session.get("username", "User")
    session.clear()
    flash(f"Goodbye, {username}. You have logged out.", "info")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    search = request.args.get("search", "").strip()
    transaction_type = request.args.get("type", "").strip()
    user_id = session["user_id"]
    currency_code = session.get("currency", "KES")
    currency_details = get_currency_details(currency_code)
    currency_symbol = currency_details["symbol"]
    currency_rate = currency_details["rate"]

    conn = get_db()

    query = """
        SELECT *
        FROM transactions
        WHERE user_id = ?
    """

    params = [user_id]

    if search:
        query += """
            AND (
                title LIKE ?
                OR category LIKE ?
            )
        """

        search_value = f"%{search}%"
        params.extend([search_value, search_value])

    if transaction_type in ["Income", "Expense"]:
        query += " AND type = ?"
        params.append(transaction_type)

    query += " ORDER BY id DESC"

    transactions = conn.execute(
        query,
        params,
    ).fetchall()

    income = conn.execute(
        """
        SELECT IFNULL(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ? AND type = 'Income'
        """,
        (user_id,),
    ).fetchone()[0]

    expenses = conn.execute(
        """
        SELECT IFNULL(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ? AND type = 'Expense'
        """,
        (user_id,),
    ).fetchone()[0]

    total_transactions = conn.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()[0]

    category_rows = conn.execute(
        """
        SELECT category, SUM(amount) AS total
        FROM transactions
        WHERE user_id = ? AND type = 'Expense'
        GROUP BY category
        ORDER BY total DESC
        """,
        (user_id,),
    ).fetchall()

    category_labels = [
        row["category"]
        for row in category_rows
    ]
    category_values = [
        row["total"]
        for row in category_rows
    ]

    monthly_rows = conn.execute(
        """
        SELECT
            strftime('%Y-%m', date_created) AS month,
            SUM(
                CASE
                    WHEN type = 'Income' THEN amount
                    ELSE 0
                END
            ) AS income_total,
            SUM(
                CASE
                    WHEN type = 'Expense' THEN amount
                    ELSE 0
                END
            ) AS expense_total
        FROM transactions
        WHERE user_id = ?
        GROUP BY strftime('%Y-%m', date_created)
        ORDER BY month ASC
        """,
        (user_id,),
    ).fetchall()

    monthly_labels = [
        row["month"]
        for row in monthly_rows
    ]
    monthly_income = [
        row["income_total"] or 0
        for row in monthly_rows
    ]
    monthly_expenses = [
        row["expense_total"] or 0
        for row in monthly_rows
    ]

    stats = {
        "balance": convert_amount(
            income - expenses,
            currency_code,
        ),
        "income": convert_amount(
            income,
            currency_code,
        ),
        "expenses": convert_amount(
            expenses,
            currency_code,
        ),
        "transactions": total_transactions,
    }

    category_values = [
        convert_amount(value, currency_code)
        for value in category_values
    ]

    monthly_income = [
        convert_amount(value, currency_code)
        for value in monthly_income
    ]

    monthly_expenses = [
        convert_amount(value, currency_code)
        for value in monthly_expenses
    ]

    conn.close()

    return render_template(
        "index.html",
        transactions=transactions,
        stats=stats,
        search=search,
        selected_type=transaction_type,
        category_labels=json.dumps(category_labels),
        category_values=json.dumps(category_values),
        monthly_labels=json.dumps(monthly_labels),
        monthly_income=json.dumps(monthly_income),
        monthly_expenses=json.dumps(monthly_expenses),
        username=session["username"],
        currency_code=currency_code,
        currency_symbol=currency_symbol,
        currency_rate=currency_rate,
    )


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_transaction():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        transaction_type = request.form.get("type", "").strip()
        amount_text = request.form.get("amount", "").strip()

        try:
            amount = float(amount_text)
        except ValueError:
            flash("Please enter a valid amount.", "danger")
            return redirect(url_for("add_transaction"))

        if not title or not category:
            flash(
                "Title and category are required.",
                "danger",
            )
            return redirect(url_for("add_transaction"))

        if amount <= 0:
            flash(
                "Amount must be greater than zero.",
                "danger",
            )
            return redirect(url_for("add_transaction"))

        if transaction_type not in ["Income", "Expense"]:
            flash(
                "Please select a valid transaction type.",
                "danger",
            )
            return redirect(url_for("add_transaction"))

        conn = get_db()

        conn.execute(
            """
            INSERT INTO transactions (
                user_id,
                title,
                category,
                amount,
                type
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session["user_id"],
                title,
                category,
                amount,
                transaction_type,
            ),
        )

        conn.commit()
        conn.close()

        flash(
            "Transaction added successfully!",
            "success",
        )
        return redirect(url_for("dashboard"))

    return render_template("add.html")


@app.route(
    "/edit/<int:transaction_id>",
    methods=["GET", "POST"],
)
@login_required
def edit_transaction(transaction_id):
    conn = get_db()

    transaction = conn.execute(
        """
        SELECT *
        FROM transactions
        WHERE id = ? AND user_id = ?
        """,
        (
            transaction_id,
            session["user_id"],
        ),
    ).fetchone()

    if transaction is None:
        conn.close()
        flash("Transaction not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        transaction_type = request.form.get("type", "").strip()
        amount_text = request.form.get("amount", "").strip()

        try:
            amount = float(amount_text)
        except ValueError:
            conn.close()
            flash("Please enter a valid amount.", "danger")
            return redirect(
                url_for(
                    "edit_transaction",
                    transaction_id=transaction_id,
                )
            )

        if not title or not category:
            conn.close()
            flash(
                "Title and category are required.",
                "danger",
            )
            return redirect(
                url_for(
                    "edit_transaction",
                    transaction_id=transaction_id,
                )
            )

        if amount <= 0:
            conn.close()
            flash(
                "Amount must be greater than zero.",
                "danger",
            )
            return redirect(
                url_for(
                    "edit_transaction",
                    transaction_id=transaction_id,
                )
            )

        if transaction_type not in ["Income", "Expense"]:
            conn.close()
            flash(
                "Please select a valid transaction type.",
                "danger",
            )
            return redirect(
                url_for(
                    "edit_transaction",
                    transaction_id=transaction_id,
                )
            )

        conn.execute(
            """
            UPDATE transactions
            SET
                title = ?,
                category = ?,
                amount = ?,
                type = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                title,
                category,
                amount,
                transaction_type,
                transaction_id,
                session["user_id"],
            ),
        )

        conn.commit()
        conn.close()

        flash(
            "Transaction updated successfully!",
            "info",
        )
        return redirect(url_for("dashboard"))

    conn.close()

    return render_template(
        "edit.html",
        transaction=transaction,
    )


@app.route(
    "/delete/<int:transaction_id>",
    methods=["POST"],
)
@login_required
def delete_transaction(transaction_id):
    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM transactions
        WHERE id = ? AND user_id = ?
        """,
        (
            transaction_id,
            session["user_id"],
        ),
    )

    conn.commit()
    deleted = cursor.rowcount
    conn.close()

    if deleted == 0:
        flash("Transaction not found.", "danger")
    else:
        flash(
            "Transaction deleted successfully!",
            "danger",
        )

    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = session["user_id"]

    conn = get_db()

    user = conn.execute(
        """
        SELECT id, username, password_hash, currency
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    if user is None:
        conn.close()
        session.clear()
        flash("Your account could not be found.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "update_username":
            new_username = request.form.get(
                "username",
                "",
            ).strip()

            if len(new_username) < 2:
                conn.close()
                flash(
                    "Please enter a name with at least 2 characters.",
                    "danger",
                )
                return redirect(url_for("settings"))

            existing_user = conn.execute(
                """
                SELECT id
                FROM users
                WHERE username = ? AND id != ?
                """,
                (
                    new_username,
                    user_id,
                ),
            ).fetchone()

            if existing_user is not None:
                conn.close()
                flash(
                    "That username is already being used.",
                    "danger",
                )
                return redirect(url_for("settings"))

            conn.execute(
                """
                UPDATE users
                SET username = ?
                WHERE id = ?
                """,
                (
                    new_username,
                    user_id,
                ),
            )

            conn.commit()
            conn.close()

            session["username"] = new_username

            flash(
                "Your display name was updated successfully.",
                "success",
            )
            return redirect(url_for("settings"))

        if action == "update_password":
            current_password = request.form.get(
                "current_password",
                "",
            )
            new_password = request.form.get(
                "new_password",
                "",
            )
            confirm_password = request.form.get(
                "confirm_password",
                "",
            )

            if not check_password_hash(
                user["password_hash"],
                current_password,
            ):
                conn.close()
                flash(
                    "Your current password is incorrect.",
                    "danger",
                )
                return redirect(url_for("settings"))

            if len(new_password) < 6:
                conn.close()
                flash(
                    "Your new password must contain at least 6 characters.",
                    "danger",
                )
                return redirect(url_for("settings"))

            if new_password != confirm_password:
                conn.close()
                flash(
                    "The new passwords do not match.",
                    "danger",
                )
                return redirect(url_for("settings"))

            if check_password_hash(
                user["password_hash"],
                new_password,
            ):
                conn.close()
                flash(
                    "Your new password must be different from your current password.",
                    "danger",
                )
                return redirect(url_for("settings"))

            conn.execute(
                """
                UPDATE users
                SET password_hash = ?
                WHERE id = ?
                """,
                (
                    generate_password_hash(new_password),
                    user_id,
                ),
            )

            conn.commit()
            conn.close()

            flash(
                "Your password was changed successfully.",
                "success",
            )
            return redirect(url_for("settings"))


        if action == "update_currency":
            currency_code = request.form.get(
                "currency",
                "KES",
            ).strip().upper()

            if currency_code not in SUPPORTED_CURRENCIES:
                conn.close()
                flash(
                    "Please choose a supported currency.",
                    "danger",
                )
                return redirect(url_for("settings"))

            conn.execute(
                """
                UPDATE users
                SET currency = ?
                WHERE id = ?
                """,
                (
                    currency_code,
                    user_id,
                ),
            )

            conn.commit()
            conn.close()

            session["currency"] = currency_code

            flash(
                f"Currency changed to {currency_code}.",
                "success",
            )
            return redirect(url_for("settings"))

        if action == "delete_account":
            delete_password = request.form.get(
                "delete_password",
                "",
            )

            if not check_password_hash(
                user["password_hash"],
                delete_password,
            ):
                conn.close()
                flash(
                    "Incorrect password. Your account was not deleted.",
                    "danger",
                )
                return redirect(url_for("settings"))

            conn.execute(
                """
                DELETE FROM transactions
                WHERE user_id = ?
                """,
                (user_id,),
            )

            conn.execute(
                """
                DELETE FROM users
                WHERE id = ?
                """,
                (user_id,),
            )

            conn.commit()
            conn.close()

            session.clear()

            flash(
                "Your account and transactions were permanently deleted.",
                "info",
            )
            return redirect(url_for("register"))

        conn.close()
        flash("Invalid settings action.", "danger")
        return redirect(url_for("settings"))

    username = user["username"]
    conn.close()

    return render_template(
        "settings.html",
        username=username,
        currency_code=user["currency"] or "KES",
        usd_kes_rate=USD_KES_RATE,
    )


@app.route("/export")
@login_required
def export_transactions():
    currency_code = session.get("currency", "KES")
    currency_details = get_currency_details(currency_code)

    conn = get_db()

    transactions = conn.execute(
        """
        SELECT
            id,
            title,
            category,
            amount,
            type,
            date_created
        FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],),
    ).fetchall()

    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "ID",
            "Title",
            "Category",
            f"Amount ({currency_code})",
            "Type",
            "Date Created",
        ]
    )

    for transaction in transactions:
        writer.writerow(
            [
                transaction["id"],
                transaction["title"],
                transaction["category"],
                round(
                    float(transaction["amount"]) *
                    currency_details["rate"],
                    2,
                ),
                transaction["type"],
                transaction["date_created"],
            ]
        )

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; "
                f"filename=expense-tracker-transactions-{currency_code.lower()}.csv"
        },
    )


if __name__ == "__main__":
    app.run(debug=True)
