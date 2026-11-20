import csv
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
import html
import io
import mimetypes
import os
import smtplib
import sqlite3
import threading
from dotenv import load_dotenv
from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "idl-integrated-dairies-secret-2026"
)

DATABASE = os.path.join(BASE_DIR, "idl_helpdesk.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max upload size
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# --- IT ADMIN CREDENTIALS ---
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "itadmin")
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get("ADMIN_PASS", "IDLHelpdesk@2026")
)

# --- EMAIL / SMTP CONFIGURATION ---
MAIL_ENABLED = os.environ.get("MAIL_ENABLED", "False").lower() == "true"
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
IT_ALERT_EMAIL = os.environ.get("IT_ALERT_EMAIL", "")


def allowed_file(filename):
  return (
      "." in filename
      and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
  )


def build_email_message(
    to_email,
    subject,
    text_body,
    html_body=None,
    attachment_path=None,
    reply_to=None,
    message_id=None,
    in_reply_to=None,
):
  msg = EmailMessage()
  msg["Subject"] = subject
  msg["From"] = f"IDL IT Support <{SMTP_USER}>"
  msg["To"] = to_email

  if reply_to:
    msg["Reply-To"] = reply_to
  if message_id:
    msg["Message-ID"] = message_id
  if in_reply_to:
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = in_reply_to

  msg.set_content(text_body)

  if html_body:
    msg.add_alternative(html_body, subtype="html")

  if attachment_path and os.path.exists(attachment_path):
    mime_type, _ = mimetypes.guess_type(attachment_path)
    maintype, subtype = (
        mime_type.split("/", 1) if mime_type else ("application", "octet-stream")
    )
    with open(attachment_path, "rb") as f:
      msg.add_attachment(
          f.read(),
          maintype=maintype,
          subtype=subtype,
          filename=os.path.basename(attachment_path),
      )

  return msg


def send_email_sync(
    to_email,
    subject,
    text_body,
    html_body=None,
    attachment_path=None,
    reply_to=None,
    message_id=None,
    in_reply_to=None,
):
  msg = build_email_message(
      to_email,
      subject,
      text_body,
      html_body=html_body,
      attachment_path=attachment_path,
      reply_to=reply_to,
      message_id=message_id,
      in_reply_to=in_reply_to,
  )
  with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASSWORD)
    server.send_message(msg)


def send_email_async(
    to_email,
    subject,
    text_body,
    html_body=None,
    attachment_path=None,
    reply_to=None,
    message_id=None,
    in_reply_to=None,
):
  if not MAIL_ENABLED or not to_email or not SMTP_USER or not SMTP_PASSWORD:
    return

  def _send():
    try:
      send_email_sync(
          to_email,
          subject,
          text_body,
          html_body=html_body,
          attachment_path=attachment_path,
          reply_to=reply_to,
          message_id=message_id,
          in_reply_to=in_reply_to,
      )
    except Exception as e:
      print(f"[Email Error] Failed to send to {to_email}: {e}")

  threading.Thread(target=_send, daemon=True).start()


def get_db():
  db = getattr(g, "_database", None)
  if db is None:
    db = g._database = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
  return db


@app.teardown_appcontext
def close_connection(exception):
  db = getattr(g, "_database", None)
  if db is not None:
    db.close()


def init_db():
  with app.app_context():
    db = get_db()
    db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_code TEXT UNIQUE,
                staff_name TEXT NOT NULL,
                staff_email TEXT NOT NULL,
                location TEXT NOT NULL,
                department TEXT NOT NULL,
                asset_tag TEXT,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                description TEXT NOT NULL,
                attachment TEXT DEFAULT '',
                status TEXT DEFAULT 'Open',
                resolution_notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT DEFAULT ''
            )
        """)
    db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                message TEXT NOT NULL,
                status_change TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets (id)
            )
        """)
    existing_cols = [
        row["name"]
        for row in db.execute("PRAGMA table_info(tickets)").fetchall()
    ]
    if "resolved_at" not in existing_cols:
      db.execute("ALTER TABLE tickets ADD COLUMN resolved_at TEXT DEFAULT ''")
    if "attachment" not in existing_cols:
      db.execute("ALTER TABLE tickets ADD COLUMN attachment TEXT DEFAULT ''")
    db.commit()


def admin_required(f):

  @wraps(f)
  def decorated_function(*args, **kwargs):
    if not session.get("is_admin"):
      flash("Please log in to access the IDL IT Command Center.", "warning")
      return redirect(url_for("login"))
    return f(*args, **kwargs)

  return decorated_function


@app.errorhandler(413)
def file_too_large(e):
  flash("Screenshot/photo is too large. Maximum allowed size is 5 MB.", "danger")
  return redirect(url_for("index"))


# --- STAFF ROUTES ---


@app.route("/", methods=["GET", "POST"])
def index():
  if request.method == "POST":
    staff_name = request.form["staff_name"].strip()
    staff_email = request.form["staff_email"].strip().lower()
    location = request.form["location"].strip()
    department = request.form["department"].strip()
    asset_tag = request.form.get("asset_tag", "").strip().upper()
    category = request.form["category"]
    priority = request.form["priority"]
    description = request.form["description"].strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    attachment_filename = ""
    saved_filepath = None
    file = request.files.get("screenshot")
    if file and file.filename != "":
      if allowed_file(file.filename):
        clean_name = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        attachment_filename = f"{timestamp}_{clean_name}"
        saved_filepath = os.path.join(
            app.config["UPLOAD_FOLDER"], attachment_filename
        )
        file.save(saved_filepath)
      else:
        flash(
            "Unsupported image format. Please upload PNG, JPG, JPEG, GIF, or"
            " WEBP.",
            "danger",
        )
        return redirect(url_for("index"))

    db = get_db()
    cursor = db.execute(
        """
            INSERT INTO tickets (
                staff_name, staff_email, location, department, asset_tag,
                category, priority, description, attachment, status, created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?, '')
        """,
        (
            staff_name,
            staff_email,
            location,
            department,
            asset_tag,
            category,
            priority,
            description,
            attachment_filename,
            now,
            now,
        ),
    )

    row_id = cursor.lastrowid
    ticket_code = f"IDL-{1000 + row_id}"
    db.execute(
        "UPDATE tickets SET ticket_code = ? WHERE id = ?",
        (ticket_code, row_id),
    )
    db.commit()

    # Unified subject line so human replies and automated updates stay in one thread
    base_subject = f"[{ticket_code}] IDL IT Support — {category}"

    # 1. Notify IT Support Inbox
    admin_url = url_for("admin", _external=True)
    it_subject = f"{base_subject} ({priority} • {location})"
    it_thread_id = f"<{ticket_code.lower()}-it@idlgroup.com.ng>"
    it_text_body = (
        "New IT Support Ticket Logged at Integrated Dairies Limited (IDL)\n"
        "================================================================\n"
        f"Ticket ID:   {ticket_code}\n"
        f"Priority:    {priority}\n"
        f"Category:    {category}\n"
        f"Location:    {location}\n"
        f"Staff:       {staff_name} ({department})\n"
        f"Email:       {staff_email}\n"
        f"Asset Tag:   {asset_tag or 'Not provided'}\n"
        f"Screenshot:  {'Attached to this email' if attachment_filename else 'None'}\n\n"
        "ISSUE DESCRIPTION:\n"
        f"{description}\n\n"
        f"Manage Ticket in Command Center: {admin_url}\n"
    )

    priority_color = (
        "#E02674"
        if priority == "High"
        else ("#EB7B00" if priority == "Medium" else "#68266C")
    )
    safe_desc = html.escape(description).replace("\n", "<br>")
    it_html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 620px; margin: 0 auto; border: 1px solid #d9eef2; border-radius: 10px; overflow: hidden;">
            <div style="background-color: #398897; color: #ffffff; padding: 16px 20px; border-bottom: 4px solid #56AF00;">
                <h2 style="margin: 0; font-size: 18px;">New IT Ticket: {html.escape(ticket_code)}</h2>
                <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">Integrated Dairies Limited (IDL) — IT Support Portal</p>
            </div>
            <div style="padding: 20px; background-color: #ffffff; color: #1f2d3d;">
                <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 16px;">
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d; width: 130px;"><strong>Ticket ID:</strong></td>
                        <td style="padding: 6px 0;"><strong>{html.escape(ticket_code)}</strong></td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Priority:</strong></td>
                        <td style="padding: 6px 0;"><span style="background-color: {priority_color}; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;">{html.escape(priority)}</span></td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Category:</strong></td>
                        <td style="padding: 6px 0;">{html.escape(category)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Location:</strong></td>
                        <td style="padding: 6px 0;">{html.escape(location)}</td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Staff & Dept:</strong></td>
                        <td style="padding: 6px 0;">{html.escape(staff_name)} ({html.escape(department)})</td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Staff Email:</strong></td>
                        <td style="padding: 6px 0;"><a href="mailto:{html.escape(staff_email)}">{html.escape(staff_email)}</a></td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 0; color: #6c757d;"><strong>Asset Tag:</strong></td>
                        <td style="padding: 6px 0;">{html.escape(asset_tag) if asset_tag else 'Not provided'}</td>
                    </tr>
                </table>

                <div style="background-color: #E9FCFF; padding: 14px; border-radius: 8px; border-left: 4px solid #398897; margin-bottom: 18px;">
                    <div style="font-size: 12px; font-weight: bold; color: #398897; margin-bottom: 6px;">ISSUE DESCRIPTION</div>
                    <div style="font-size: 14px; line-height: 1.5;">{safe_desc}</div>
                </div>

                <a href="{admin_url}" style="display: inline-block; background-color: #398897; color: #ffffff; text-decoration: none; padding: 10px 18px; border-radius: 6px; font-weight: bold; font-size: 14px;">
                    Open in IT Command Center
                </a>
            </div>
        </div>
        """
    send_email_async(
        IT_ALERT_EMAIL,
        it_subject,
        it_text_body,
        html_body=it_html_body,
        attachment_path=saved_filepath,
        reply_to=staff_email,
        message_id=it_thread_id,
    )

    # 2. Send Confirmation Receipt to Staff Member
    track_url = url_for("track", _external=True)
    staff_thread_id = f"<{ticket_code.lower()}-staff@idlgroup.com.ng>"
    staff_text_body = (
        f"Hi {staff_name},\n\n"
        "Thanks for reaching out! Your IT support request has been logged and"
        " our team is on it.\n\n"
        f"Ticket Reference ID: {ticket_code}\n"
        f"Category: {category}\n"
        f"Priority: {priority}\n"
        f"Description: {description}\n\n"
        f"View live progress or post follow-up updates on the portal:\n{track_url}\n"
        f"(Use Ticket ID {ticket_code} and {staff_email})\n\n"
        "You can also reply directly to this email thread if you need to share"
        " extra details with IT.\n\n"
        "Warm regards,\n"
        "IT Support Team\n"
        "Integrated Dairies Limited (IDL)"
    )
    send_email_async(
        staff_email,
        base_subject,
        staff_text_body,
        reply_to=IT_ALERT_EMAIL,
        message_id=staff_thread_id,
    )

    # Post/Redirect/Get: store one-time confirmation in session and redirect to GET /
    session["submitted_ticket"] = {
        "code": ticket_code,
        "email": staff_email,
    }
    return redirect(url_for("index"))

  submitted = session.pop("submitted_ticket", None)
  return render_template(
      "index.html",
      submitted_code=submitted["code"] if submitted else None,
      staff_email=submitted["email"] if submitted else None,
  )


@app.route("/track", methods=["GET", "POST"])
def track():
  if request.method == "POST":
    ticket_code = request.form["ticket_code"].strip().upper()
    staff_email = request.form["staff_email"].strip().lower()
    session["track_lookup"] = {"code": ticket_code, "email": staff_email}
    return redirect(url_for("track"))

  ticket = None
  messages = []
  searched = False
  lookup = session.pop("track_lookup", None)

  if lookup:
    searched = True
    db = get_db()
    ticket = db.execute(
        "SELECT * FROM tickets WHERE ticket_code = ? AND staff_email = ?",
        (lookup["code"], lookup["email"]),
    ).fetchone()
    if ticket:
      messages = db.execute(
          "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
          (ticket["id"],),
      ).fetchall()

  return render_template(
      "track.html", ticket=ticket, messages=messages, searched=searched
  )


@app.route("/track/reply", methods=["POST"])
def track_reply():
  ticket_code = request.form["ticket_code"].strip().upper()
  staff_email = request.form["staff_email"].strip().lower()
  followup_note = request.form["followup_note"].strip()

  db = get_db()
  ticket = db.execute(
      "SELECT * FROM tickets WHERE ticket_code = ? AND staff_email = ?",
      (ticket_code, staff_email),
  ).fetchone()

  if ticket and followup_note and ticket["status"] != "Resolved":
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        """
            INSERT INTO ticket_messages (ticket_id, sender_role, sender_name, message, status_change, created_at)
            VALUES (?, 'Staff', ?, ?, '', ?)
        """,
        (ticket["id"], ticket["staff_name"], followup_note, now),
    )
    db.execute(
        "UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket["id"])
    )
    db.commit()

    # Thread the staff follow-up directly into the IT alert email conversation
    admin_url = url_for("admin", _external=True)
    it_subject = (
        f"Re: [{ticket_code}] IDL IT Support — {ticket['category']}"
        f" ({ticket['priority']} • {ticket['location']})"
    )
    it_thread_id = f"<{ticket_code.lower()}-it@idlgroup.com.ng>"
    it_body = (
        f"Staff Follow-Up Added on Ticket {ticket_code}\n"
        "============================================\n"
        f"From: {ticket['staff_name']} ({staff_email})\n"
        f"Current Status: {ticket['status']}\n\n"
        f"Follow-Up Message:\n{followup_note}\n\n"
        f"Open in Command Center: {admin_url}\n"
    )
    send_email_async(
        IT_ALERT_EMAIL,
        it_subject,
        it_body,
        reply_to=staff_email,
        in_reply_to=it_thread_id,
    )

    flash("Your follow-up note has been added to the ticket thread.", "success")
    session["track_lookup"] = {"code": ticket_code, "email": staff_email}
    return redirect(url_for("track"))

  flash("Could not submit follow-up note. Please verify your details.", "danger")
  return redirect(url_for("track"))


# --- IT ADMIN ROUTES ---


@app.route("/login", methods=["GET", "POST"])
def login():
  if request.method == "POST":
    username = request.form["username"].strip()
    password = request.form["password"]
    if username == ADMIN_USERNAME and check_password_hash(
        ADMIN_PASSWORD_HASH, password
    ):
      session["is_admin"] = True
      return redirect(url_for("admin"))
    flash("Invalid IT Admin credentials.", "danger")
  return render_template("login.html")


@app.route("/logout")
def logout():
  session.pop("is_admin", None)
  flash("Logged out successfully.", "info")
  return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin():
  # Default to 'Active' (Open + In Progress) so Resolved tickets move to the Archive tab
  status_filter = request.args.get("status", "Active")
  location_filter = request.args.get("location", "All")
  category_filter = request.args.get("category", "All")

  db = get_db()
  query = "SELECT * FROM tickets WHERE 1=1"
  params = []

  if status_filter == "Active":
    query += " AND status != 'Resolved'"
  elif status_filter != "All":
    query += " AND status = ?"
    params.append(status_filter)

  if location_filter != "All":
    query += " AND location = ?"
    params.append(location_filter)

  if category_filter != "All":
    query += " AND category = ?"
    params.append(category_filter)

  query += " ORDER BY id DESC"
  raw_tickets = db.execute(query, params).fetchall()

  now_dt = datetime.now()
  tickets = []
  for row in raw_tickets:
    t = dict(row)
    try:
      created_dt = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M")
      hours_open = round((now_dt - created_dt).total_seconds() / 3600, 1)
    except ValueError:
      hours_open = 0
    t["hours_open"] = hours_open
    t["is_overdue"] = t["status"] != "Resolved" and hours_open >= 24
    t["messages"] = []
    tickets.append(t)

  if tickets:
    ticket_ids = [t["id"] for t in tickets]
    placeholders = ",".join("?" for _ in ticket_ids)
    msg_rows = db.execute(
        f"SELECT * FROM ticket_messages WHERE ticket_id IN ({placeholders})"
        " ORDER BY id ASC",
        ticket_ids,
    ).fetchall()
    msgs_by_ticket = {}
    for m in msg_rows:
      msgs_by_ticket.setdefault(m["ticket_id"], []).append(dict(m))
    for t in tickets:
      t["messages"] = msgs_by_ticket.get(t["id"], [])

  locations = [
      row["location"]
      for row in db.execute(
          "SELECT DISTINCT location FROM tickets ORDER BY location"
      ).fetchall()
  ]
  categories = [
      row["category"]
      for row in db.execute(
          "SELECT DISTINCT category FROM tickets ORDER BY category"
      ).fetchall()
  ]

  total_count = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
  open_count = db.execute(
      "SELECT COUNT(*) FROM tickets WHERE status = 'Open'"
  ).fetchone()[0]
  in_progress_count = db.execute(
      "SELECT COUNT(*) FROM tickets WHERE status = 'In Progress'"
  ).fetchone()[0]
  resolved_count = db.execute(
      "SELECT COUNT(*) FROM tickets WHERE status = 'Resolved'"
  ).fetchone()[0]
  urgent_active = db.execute(
      "SELECT COUNT(*) FROM tickets WHERE priority = 'High' AND status !="
      " 'Resolved'"
  ).fetchone()[0]

  resolved_rows = db.execute(
      "SELECT created_at, resolved_at FROM tickets WHERE status = 'Resolved'"
      " AND resolved_at != ''"
  ).fetchall()
  resolution_minutes = []
  for r in resolved_rows:
    try:
      c_dt = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M")
      r_dt = datetime.strptime(r["resolved_at"], "%Y-%m-%d %H:%M")
      diff_mins = max((r_dt - c_dt).total_seconds() / 60, 1)
      resolution_minutes.append(diff_mins)
    except ValueError:
      continue

  if resolution_minutes:
    avg_mins = sum(resolution_minutes) / len(resolution_minutes)
    if avg_mins < 60:
      avg_resolution_str = f"{int(round(avg_mins))} mins"
    else:
      avg_resolution_str = f"{round(avg_mins / 60, 1)} hrs"
  else:
    avg_resolution_str = "N/A"

  stats = {
      "total": total_count,
      "active": open_count + in_progress_count,
      "open": open_count,
      "in_progress": in_progress_count,
      "resolved": resolved_count,
      "urgent_active": urgent_active,
      "avg_resolution": avg_resolution_str,
  }

  location_stats = db.execute("""
        SELECT location,
               COUNT(*) AS total,
               SUM(CASE WHEN status != 'Resolved' THEN 1 ELSE 0 END) AS active
        FROM tickets
        GROUP BY location
        ORDER BY total DESC
    """).fetchall()

  category_stats = db.execute("""
        SELECT category, COUNT(*) AS total
        FROM tickets
        GROUP BY category
        ORDER BY total DESC
    """).fetchall()

  repeat_assets = {
      row["asset_tag"]: row["cnt"]
      for row in db.execute("""
            SELECT asset_tag, COUNT(*) AS cnt
            FROM tickets
            WHERE asset_tag != ''
            GROUP BY asset_tag
            HAVING cnt >= 2
        """).fetchall()
  }

  return render_template(
      "admin.html",
      tickets=tickets,
      locations=locations,
      categories=categories,
      status_filter=status_filter,
      location_filter=location_filter,
      category_filter=category_filter,
      stats=stats,
      location_stats=location_stats,
      category_stats=category_stats,
      repeat_assets=repeat_assets,
      mail_enabled=MAIL_ENABLED,
  )


@app.route("/admin/update/<int:ticket_id>", methods=["POST"])
@admin_required
def update_ticket(ticket_id):
  new_status = request.form["status"]
  resolution_notes = request.form.get("resolution_notes", "").strip()
  now = datetime.now().strftime("%Y-%m-%d %H:%M")

  db = get_db()
  ticket = db.execute(
      "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
  ).fetchone()

  if ticket:
    if new_status == "Resolved":
      resolved_at = (
          ticket["resolved_at"]
          if (ticket["status"] == "Resolved" and ticket["resolved_at"])
          else now
      )
    else:
      resolved_at = ""

    latest_note = (
        resolution_notes if resolution_notes else ticket["resolution_notes"]
    )
    db.execute(
        """
            UPDATE tickets 
            SET status = ?, resolution_notes = ?, updated_at = ?, resolved_at = ?
            WHERE id = ?
        """,
        (new_status, latest_note, now, resolved_at, ticket_id),
    )

    thread_msg = (
        resolution_notes
        if resolution_notes
        else f"Ticket status changed to {new_status}."
    )
    db.execute(
        """
            INSERT INTO ticket_messages (ticket_id, sender_role, sender_name, message, status_change, created_at)
            VALUES (?, 'IT Support', 'IDL IT Team', ?, ?, ?)
        """,
        (ticket_id, thread_msg, new_status, now),
    )
    db.commit()

    track_url = url_for("track", _external=True)
    staff_subject = (
        f"Re: [{ticket['ticket_code']}] IDL IT Support — {ticket['category']}"
    )
    staff_thread_id = f"<{ticket['ticket_code'].lower()}-staff@idlgroup.com.ng>"

    reply_guidance = (
        "Our IT team is actively working on this. You can post a follow-up"
        " note on the portal or reply directly to this email thread if we"
        " need more details.\n\n"
        if new_status == "In Progress"
        else ""
    )

    body = (
        f"Hi {ticket['staff_name']},\n\n"
        f"Your IDL IT Support ticket ({ticket['ticket_code']}) has been"
        " updated.\n\n"
        f"Current Status: {new_status}\n"
        f"IT Support Update: {thread_msg}\n\n"
        f"{reply_guidance}"
        f"View full ticket conversation on the portal: {track_url}\n\n"
        "Warm regards,\n"
        "IT Support Team\n"
        "Integrated Dairies Limited (IDL)"
    )
    send_email_async(
        ticket["staff_email"],
        staff_subject,
        body,
        reply_to=IT_ALERT_EMAIL,
        in_reply_to=staff_thread_id,
    )

    if new_status == "Resolved":
      flash(
          f"Ticket {ticket['ticket_code']} marked Resolved and moved to the"
          " Resolved Archive.",
          "success",
      )
    else:
      flash(
          f"Ticket {ticket['ticket_code']} updated ({new_status}) and staff"
          " notified.",
          "success",
      )

  return redirect(url_for("admin"))


@app.route("/admin/test-email", methods=["POST"])
@admin_required
def test_email():
  if not MAIL_ENABLED:
    flash(
        "Email notifications are currently disabled. Set MAIL_ENABLED=True in"
        " your .env file and restart the app.",
        "warning",
    )
    return redirect(url_for("admin"))

  if not SMTP_USER or not SMTP_PASSWORD or not IT_ALERT_EMAIL:
    flash(
        "Missing SMTP_USER, SMTP_PASSWORD, or IT_ALERT_EMAIL in your .env"
        " file.",
        "danger",
    )
    return redirect(url_for("admin"))

  try:
    send_email_sync(
        IT_ALERT_EMAIL,
        "[IDL-TEST] IDL IT Helpdesk — Test Email Alert",
        "Success! Your Integrated Dairies Limited (IDL) IT Helpdesk email"
        " notifications are active and working properly.",
    )
    flash(
        f"Test email sent successfully to {IT_ALERT_EMAIL}! Check your inbox.",
        "success",
    )
  except Exception as e:
    flash(f"SMTP Error: {e}", "danger")

  return redirect(url_for("admin"))


@app.route("/admin/export")
@admin_required
def export_csv():
  status_filter = request.args.get("status", "All")
  location_filter = request.args.get("location", "All")
  category_filter = request.args.get("category", "All")

  db = get_db()
  query = "SELECT * FROM tickets WHERE 1=1"
  params = []

  if status_filter == "Active":
    query += " AND status != 'Resolved'"
  elif status_filter != "All":
    query += " AND status = ?"
    params.append(status_filter)

  if location_filter != "All":
    query += " AND location = ?"
    params.append(location_filter)

  if category_filter != "All":
    query += " AND category = ?"
    params.append(category_filter)

  query += " ORDER BY id DESC"
  rows = db.execute(query, params).fetchall()

  output = io.StringIO()
  writer = csv.writer(output)
  writer.writerow([
      "Ticket ID",
      "Staff Name",
      "Staff Email",
      "Location",
      "Department",
      "Asset Tag",
      "Category",
      "Priority",
      "Status",
      "Description",
      "Latest Resolution Notes",
      "Created At",
      "Updated At",
      "Resolved At",
  ])

  for r in rows:
    writer.writerow([
        r["ticket_code"],
        r["staff_name"],
        r["staff_email"],
        r["location"],
        r["department"],
        r["asset_tag"],
        r["category"],
        r["priority"],
        r["status"],
        r["description"],
        r["resolution_notes"],
        r["created_at"],
        r["updated_at"],
        r["resolved_at"],
    ])

  filename = f"idl_it_tickets_{datetime.now().strftime('%Y-%m-%d')}.csv"
  return Response(
      output.getvalue(),
      mimetype="text/csv",
      headers={"Content-Disposition": f"attachment; filename={filename}"},
  )


init_db()

if __name__ == "__main__":
  app.run(debug=True)