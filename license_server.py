import json
import secrets
import os
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import os

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8787"))

BASE = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

DB = BASE / "license.db"

TRIAL_DAYS = 3
SUBSCRIPTION_DAYS = 30


def now():
    return datetime.now()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL wajib diisi untuk server online")

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        sslmode="require",
        prepare_threshold=None,
    )


def init_db():
    con = get_db()
    try:
        con.execute("SELECT 1")
        con.commit()
    finally:
        con.close()

def make_customer_code():
    while True:
        code = "CUS-" + secrets.token_hex(4).upper()

        con = get_db()
        row = con.execute(
            "SELECT id FROM customers WHERE customer_code=?",
            (code,)
        ).fetchone()
        con.close()

        if not row:
            return code


def make_license_key():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def group():
        return "".join(secrets.choice(chars) for _ in range(5))

    return "ACFB-" + group() + "-" + group() + "-" + group()


def log_action(customer_id, action, device_id="", result=""):
    con = get_db()

    con.execute(
        """
        INSERT INTO license_logs
        (customer_id, action, device_id, result, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            customer_id,
            action,
            device_id,
            result,
            iso(now())
        )
    )

    con.commit()
    con.close()


def current_status(row):
    if row["status"] == "DEACTIVATED":
        return "DEACTIVATED"

    current = now()

    # Customer yang sudah diaktifkan berbayar
    # harus berstatus ACTIVE selama paid_until masih berlaku.
    if row["status"] == "ACTIVE" and row["paid_until"]:
        try:
            if datetime.fromisoformat(row["paid_until"]) >= current:
                return "ACTIVE"
        except Exception:
            pass

    # Trial hanya berlaku jika status customer memang TRIAL.
    if row["status"] == "TRIAL" and row["trial_end"]:
        try:
            if datetime.fromisoformat(row["trial_end"]) >= current:
                return "TRIAL"
        except Exception:
            pass

    if row["trial_start"] or row["trial_end"] or row["paid_until"]:
        return "EXPIRED"

    return "NEW"




class LicenseHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(
            "[%s] %s"
            % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                format % args
            )
        )

    def send_json(self, data, status=200):
        body = json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.end_headers()

        self.wfile.write(body)

    def read_json(self):
        length = int(
            self.headers.get("Content-Length", "0")
        )

        if length <= 0:
            return {}

        raw = self.rfile.read(length)

        try:
            return json.loads(
                raw.decode("utf-8")
            )
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )
        self.end_headers()

    def do_GET(self):
        path = urlparse(
            self.path
        ).path.rstrip("/")

        if path == "/api/health":
            self.send_json({
                "ok": True,
                "service": "AUTO CUAN LICENSE SERVER",
                "version": "1.0",
                "time": iso(now())
            })
            return

        if path == "/api/customers":
            con = get_db()

            rows = con.execute(
                """
                SELECT *
                FROM customers
                ORDER BY id DESC
                """
            ).fetchall()

            con.close()

            customers = []

            for row in rows:
                item = dict(row)
                item["current_status"] = current_status(row)
                customers.append(item)

            self.send_json({
                "ok": True,
                "customers": customers
            })
            return

        self.send_json({
            "ok": False,
            "error": "Endpoint tidak ditemukan"
        }, 404)

    def do_POST(self):
        path = urlparse(
            self.path
        ).path.rstrip("/")

        data = self.read_json()

        if path == "/api/customer/create":
            self.create_customer(data)
            return

        if path == "/api/customer/update":
            self.update_customer(data)
            return

        if path == "/api/license/create":
            self.create_license(data)
            return

        if path == "/api/license/trial":
            self.start_trial(data)
            return

        if path == "/api/license/activate":
            self.activate_license(data)
            return

        if path == "/api/license/check":
            self.check_license(data)
            return

        if path == "/api/license/deactivate":
            self.deactivate_license(data)
            return

        if path == "/api/license/extend":
            self.extend_license(data)
            return

        self.send_json({
            "ok": False,
            "error": "Endpoint tidak ditemukan"
        }, 404)

    def create_customer(self, data):
        name = str(
            data.get("name", "")
        ).strip()

        whatsapp = str(
            data.get("whatsapp", "")
        ).strip()

        device_id = str(
            data.get("device_id", "")
        ).strip()

        if not name:
            self.send_json({
                "ok": False,
                "error": "Nama customer wajib diisi"
            }, 400)
            return

        code = make_customer_code()
        timestamp = iso(now())

        con = get_db()

        if DATABASE_URL:
            cur = con.execute(
                """
                INSERT INTO customers
                (
                    customer_code,
                    name,
                    whatsapp,
                    device_id,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, 'NEW', %s, %s)
                RETURNING id
                """,
                (
                    code,
                    name,
                    whatsapp,
                    device_id,
                    timestamp,
                    timestamp
                )
            )
            customer_id = cur.fetchone()["id"]
        else:
            cur = con.execute(
                """
                INSERT INTO customers
                (
                    customer_code,
                    name,
                    whatsapp,
                    device_id,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'NEW', ?, ?)
                """,
                (
                    code,
                    name,
                    whatsapp,
                    device_id,
                    timestamp,
                    timestamp
                )
            )
            customer_id = cur.fetchone()["id"]

        con.commit()
        con.close()

        log_action(
            customer_id,
            "CREATE_CUSTOMER",
            device_id,
            "OK"
        )

        self.send_json({
            "ok": True,
            "customer_id": customer_id,
            "customer_code": code
        })

    def update_customer(self, data):
        customer_id = data.get("customer_id")
        name = str(data.get("name", "")).strip()
        whatsapp = str(data.get("whatsapp", "")).strip()
        device_id = str(data.get("device_id", "")).strip()

        if not customer_id:
            self.send_json({
                "ok": False,
                "error": "customer_id wajib diisi"
            })
            return

        if not name:
            self.send_json({
                "ok": False,
                "error": "Nama customer wajib diisi"
            })
            return

        con = get_db()

        if DATABASE_URL:
            row = con.execute(
                "SELECT id FROM customers WHERE id=%s",
                (customer_id,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT id FROM customers WHERE id=?",
                (customer_id,)
            ).fetchone()

        if not row:
            con.close()
            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            })
            return

        timestamp = now().isoformat(timespec="seconds")

        if DATABASE_URL:
            con.execute(
                """
                UPDATE customers
                SET name=%s,
                    whatsapp=%s,
                    device_id=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (
                    name,
                    whatsapp,
                    device_id,
                    timestamp,
                    customer_id
                )
            )
        else:
            con.execute(
                """
                UPDATE customers
                SET name=?,
                    whatsapp=?,
                    device_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    name,
                    whatsapp,
                    device_id,
                    timestamp,
                    customer_id
                )
            )

        con.commit()
        con.close()

        self.send_json({
            "ok": True,
            "customer_id": customer_id,
            "name": name,
            "whatsapp": whatsapp,
            "device_id": device_id,
            "updated_at": timestamp
        })

    def create_license(self, data):
        customer_id = data.get("customer_id")

        if not customer_id:
            self.send_json({
                "ok": False,
                "error": "customer_id wajib diisi"
            }, 400)
            return

        key = make_license_key()

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE id=?
            """,
            (customer_id,)
        ).fetchone()

        if not row:
            con.close()

            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            }, 404)
            return

        con.execute(
            """
            UPDATE customers
            SET license_key=?,
                updated_at=?
            WHERE id=?
            """,
            (
                key,
                iso(now()),
                customer_id
            )
        )

        con.commit()
        con.close()

        log_action(
            customer_id,
            "CREATE_LICENSE",
            row["device_id"],
            "OK"
        )

        self.send_json({
            "ok": True,
            "license_key": key
        })

    def activate_license(self, data):
        customer_id = data.get("customer_id")

        if not customer_id:
            self.send_json({
                "ok": False,
                "error": "customer_id wajib diisi"
            }, 400)
            return

        start = now()
        end = start + timedelta(
            days=SUBSCRIPTION_DAYS
        )

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE id=?
            """,
            (customer_id,)
        ).fetchone()

        if not row:
            con.close()

            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            }, 404)
            return

        con.execute(
            """
            UPDATE customers
            SET status='ACTIVE',
                paid_until=?,
                payment_status='PAID',
                updated_at=?
            WHERE id=?
            """,
            (
                iso(end),
                iso(now()),
                customer_id
            )
        )

        con.commit()
        con.close()

        log_action(
            customer_id,
            "ACTIVATE_30_DAYS",
            row["device_id"],
            "OK"
        )

        self.send_json({
            "ok": True,
            "status": "ACTIVE",
            "paid_until": iso(end),
            "days": SUBSCRIPTION_DAYS
        })

    def start_trial(self, data):
        customer_id = data.get("customer_id")
        device_id = str(data.get("device_id", "")).strip()

        if not customer_id:
            self.send_json({
                "ok": False,
                "error": "customer_id wajib diisi"
            }, 400)
            return

        if not device_id:
            self.send_json({
                "ok": False,
                "error": "device_id wajib diisi"
            }, 400)
            return

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE id=?
            """,
            (customer_id,)
        ).fetchone()

        if not row:
            con.close()

            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            }, 404)
            return

        stored_device = str(row["device_id"] or "").strip()

        if stored_device and stored_device != device_id:
            con.close()

            self.send_json({
                "ok": False,
                "error": "DEVICE_MISMATCH"
            }, 403)
            return

        if stored_device == device_id and row["trial_start"] and row["trial_end"]:
            con.close()

            self.send_json({
                "ok": True,
                "status": "TRIAL",
                "trial_start": row["trial_start"],
                "trial_end": row["trial_end"],
                "days": TRIAL_DAYS,
                "device_id": stored_device
            })
            return

        start = now()
        end = start + timedelta(days=TRIAL_DAYS)

        con.execute(
            """
            UPDATE customers
            SET status='TRIAL',
                trial_start=?,
                trial_end=?,
                paid_until='',
                payment_status='UNPAID',
                device_id=?,
                updated_at=?
            WHERE id=?
            """,
            (
                iso(start),
                iso(end),
                device_id,
                iso(now()),
                customer_id
            )
        )

        con.commit()
        con.close()

        log_action(
            customer_id,
            "START_TRIAL",
            device_id,
            "OK"
        )

        self.send_json({
            "ok": True,
            "status": "TRIAL",
            "trial_start": iso(start),
            "trial_end": iso(end),
            "days": TRIAL_DAYS,
            "device_id": device_id
        })

    def check_license(self, data):
        key = str(
            data.get("license_key", "")
        ).strip()

        device_id = str(
            data.get("device_id", "")
        ).strip()

        if not key:
            self.send_json({
                "ok": False,
                "valid": False,
                "error": "license_key wajib diisi"
            }, 400)
            return

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE license_key=?
            """,
            (key,)
        ).fetchone()

        con.close()

        if not row:
            self.send_json({
                "ok": True,
                "valid": False,
                "status": "NOT_FOUND",
                "message": "License tidak ditemukan"
            })
            return

        status = current_status(row)

        if row["device_id"]:
            if device_id and row["device_id"] != device_id:
                log_action(
                    row["id"],
                    "CHECK_LICENSE",
                    device_id,
                    "DEVICE_MISMATCH"
                )

                self.send_json({
                    "ok": True,
                    "valid": False,
                    "status": "DEVICE_MISMATCH",
                    "message": "License terikat ke device lain"
                })
                return

        valid = status in (
            "TRIAL",
            "ACTIVE"
        )

        log_action(
            row["id"],
            "CHECK_LICENSE",
            device_id,
            "VALID" if valid else "EXPIRED"
        )

        self.send_json({
            "ok": True,
            "valid": valid,
            "status": status,
            "customer_code": row["customer_code"],
            "customer_name": row["name"],
            "trial_end": row["trial_end"],
            "paid_until": row["paid_until"]
        })

    def deactivate_license(self, data):
        customer_id = data.get("customer_id")

        if not customer_id:
            self.send_json({
                "ok": False,
                "error": "customer_id wajib diisi"
            }, 400)
            return

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE id=?
            """,
            (customer_id,)
        ).fetchone()

        if not row:
            con.close()

            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            }, 404)
            return

        con.execute(
            """
            UPDATE customers
            SET status='DEACTIVATED',
                updated_at=?
            WHERE id=?
            """,
            (
                iso(now()),
                customer_id
            )
        )

        con.commit()
        con.close()

        log_action(
            customer_id,
            "DEACTIVATE",
            row["device_id"],
            "OK"
        )

        self.send_json({
            "ok": True,
            "status": "DEACTIVATED"
        })

    def extend_license(self, data):
        customer_id = data.get("customer_id")

        try:
            days = int(
                data.get(
                    "days",
                    SUBSCRIPTION_DAYS
                )
            )
        except Exception:
            days = SUBSCRIPTION_DAYS

        if days <= 0:
            self.send_json({
                "ok": False,
                "error": "Jumlah hari tidak valid"
            }, 400)
            return

        con = get_db()

        row = con.execute(
            """
            SELECT *
            FROM customers
            WHERE id=?
            """,
            (customer_id,)
        ).fetchone()

        if not row:
            con.close()

            self.send_json({
                "ok": False,
                "error": "Customer tidak ditemukan"
            }, 404)
            return

        base = now()

        if row["paid_until"]:
            try:
                old = datetime.fromisoformat(
                    row["paid_until"]
                )

                if old > base:
                    base = old

            except Exception:
                pass

        end = base + timedelta(days=days)

        con.execute(
            """
            UPDATE customers
            SET status='ACTIVE',
                paid_until=?,
                payment_status='PAID',
                updated_at=?
            WHERE id=?
            """,
            (
                iso(end),
                iso(now()),
                customer_id
            )
        )

        con.commit()
        con.close()

        log_action(
            customer_id,
            "EXTEND_LICENSE",
            row["device_id"],
            "OK"
        )

        self.send_json({
            "ok": True,
            "status": "ACTIVE",
            "paid_until": iso(end),
            "extended_days": days
        })


def main():
    init_db()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        LicenseHandler
    )

    print("=" * 60)
    print("AUTO CUAN DARI FBMP - LICENSE SERVER")
    print("=" * 60)
    print()
    print(f"Server : http://{HOST}:{PORT}")
    print(f"Database: {DB}")
    print()
    print("Health : http://127.0.0.1:8787/api/health")
    print()
    print("Server aktif. Tekan CTRL+C untuk berhenti.")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Server dihentikan.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()