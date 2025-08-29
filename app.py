from flask import Flask, render_template, request, redirect, url_for, flash, Response
from werkzeug.utils import secure_filename
import os
import io
import csv
from datetime import date, datetime
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

def ensure_schema():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS classes (
          class_id INT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(100) NOT NULL UNIQUE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
          student_id INT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(100) NOT NULL,
          class VARCHAR(100) NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework_records (
          hw_id INT AUTO_INCREMENT PRIMARY KEY,
          hw_date DATE NOT NULL,
          student_id INT NOT NULL,
          status ENUM('done','not_done') NOT NULL,
          grade VARCHAR(10) NULL,
          notes VARCHAR(255) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uniq_date_student (hw_date, student_id),
          INDEX idx_student (student_id),
          INDEX idx_date (hw_date),
          CONSTRAINT fk_hw_student
            FOREIGN KEY (student_id) REFERENCES students(student_id)
            ON DELETE CASCADE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASS", ""),
        database=os.getenv("DB_NAME", "coaching_app"),
    )



# --- helper: how many records already exist for this class+date? ---
def count_existing_hw_for_class_date(conn, class_name, hw_date):
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM homework_records hr
        JOIN students s ON s.student_id = hr.student_id
        WHERE s.class=%s AND hr.hw_date=%s
    """, (class_name, hw_date))
    (cnt,) = cur.fetchone()
    cur.close()
    return cnt

app = Flask(__name__)
ensure_schema()
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

ALLOWED_IMPORT_HEADERS = {"name", "class"}

@app.route("/hw/import-students", methods=["GET", "POST"])
def hw_import_students():
    # Class comes from query (GET) or form (POST)
    class_name = (request.args.get("class_name") or request.form.get("class_name") or "").strip()
    if not class_name:
        flash("Please select a class first, then open Bulk import.", "warning")
        return redirect(url_for("hw_tracker"))

    if request.method == "GET":
        # Render upload page for this class
        return render_template("hw_import_students.html", class_name=class_name)

    # POST: handle CSV upload (names only)
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Please choose a CSV file.", "danger")
        return redirect(url_for("hw_import_students", class_name=class_name))

    # Read whole CSV to text
    raw = file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    # Accept either header "name" or no header.
    # We’ll try DictReader first; if no "name" header, fall back to simple reader and treat first column as name.
    names = []
    f = io.StringIO(text)
    try:
        sniffer = csv.Sniffer()
        has_header = sniffer.has_header(text[:2048])
    except Exception:
        has_header = True  # safe default

    if has_header:
        reader = csv.DictReader(f)
        if reader.fieldnames and any(h.strip().lower() == "name" for h in reader.fieldnames):
            for i, row in enumerate(reader, start=2):
                nm = (row.get("name") or "").strip()
                if nm:
                    names.append((i, nm))
        else:
            # header exists but not named "name" → fallback: first column as name
            f.seek(0)
            reader2 = csv.reader(f)
            header = next(reader2, None)  # skip header row
            for i, row in enumerate(reader2, start=2):
                nm = (row[0] if row else "").strip()
                if nm:
                    names.append((i, nm))
    else:
        # No header → every row’s first column is a name
        f.seek(0)
        reader = csv.reader(f)
        for i, row in enumerate(reader, start=1):
            nm = (row[0] if row else "").strip()
            if nm:
                names.append((i, nm))

    if not names:
        flash("No student names found in CSV.", "warning")
        return redirect(url_for("hw_import_students", class_name=class_name))

    # Insert names into the selected class (skip duplicates name+class, case-insensitive)
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # Ensure the class exists in classes table (optional)
    try:
        cur2 = conn.cursor()
        cur2.execute("INSERT IGNORE INTO classes (name) VALUES (%s)", (class_name,))
        cur2.close()
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute("SELECT LOWER(name) AS lname FROM students WHERE class=%s", (class_name,))
    existing_names = {r["lname"] for r in cur.fetchall()}

    added, skipped = 0, 0
    errors = []
    try:
        for i, nm in names:
            key = nm.lower()
            if key in existing_names:
                skipped += 1
                continue
            c = conn.cursor()
            c.execute("INSERT INTO students (name, class) VALUES (%s, %s)", (nm, class_name))
            c.close()
            existing_names.add(key)
            added += 1
        conn.commit()
        msg = f"Import complete for class '{class_name}': added {added}, skipped {skipped}."
        flash(msg, "success" if added else "warning")
    except Exception as e:
        conn.rollback()
        flash(f"Import failed: {e}", "danger")
    finally:
        cur.close(); conn.close()

    return redirect(url_for("hw_import_students", class_name=class_name))

# Optional: downloadable sample CSV
@app.route("/hw/students/sample.csv")
def hw_students_sample_csv():
    sample = io.StringIO()
    w = csv.writer(sample)
    w.writerow(["name", "class"])
    w.writerow(["Aarav Patil", "8A"])
    w.writerow(["Sara Khan", "8A"])
    w.writerow(["Rohan Deshmukh", "9B"])
    data = sample.getvalue()
    sample.close()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students_sample.csv"}
    )

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/tasks")
def tasks():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, title, notes, category, due_date, is_done, created_at
        FROM tasks
        ORDER BY is_done ASC, COALESCE(due_date, '9999-12-31') ASC, id DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("tasks.html", tasks=rows)

@app.route("/tasks/add", methods=["POST"])
def add_task():
    title = (request.form.get("title") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    category = (request.form.get("category") or "").strip() or None
    due_date = request.form.get("due_date") or None

    if not title:
        flash("Title is required.")
        return redirect(url_for("tasks"))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (title, notes, category, due_date) VALUES (%s, %s, %s, %s)",
        (title, notes, category, due_date)
    )
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("tasks"))

@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def toggle_task(task_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET is_done = NOT is_done WHERE id = %s", (task_id,))
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("tasks"))

@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("tasks"))

# ---------- Helpers ----------
def fetch_classes(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT class_id, name FROM classes ORDER BY name;")
    rows = cur.fetchall()
    cur.close()
    return rows

def fetch_students_by_class(conn, class_name):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT student_id, name, class FROM students WHERE class=%s ORDER BY name;", (class_name,))
    rows = cur.fetchall()
    cur.close()
    return rows

def upsert_homework(conn, hw_date, student_id, status, grade):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO homework_records (hw_date, student_id, status, grade)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE status=VALUES(status), grade=VALUES(grade), updated_at=NOW();
    """, (hw_date, student_id, status, grade))
    conn.commit()
    cur.close()


# ---------- HW Tracker main ----------
@app.route("/hw", methods=["GET", "POST"])
def hw_tracker():
    conn = get_db_connection()

    if request.method == "POST":
        hw_date = request.form.get("hw_date") or date.today().isoformat()
        class_name = request.form.get("class_name")
        confirm = (request.form.get("confirm") or "no").lower()  # "yes" only after user confirms

        # Check if there are any existing records for this class on this date
        existing_cnt = count_existing_hw_for_class_date(conn, class_name, hw_date)

        # If there are existing records and user hasn't confirmed, block and ask
        if existing_cnt > 0 and confirm != "yes":
            # Don't write anything—send user back to the page where a confirm dialog will appear
            conn.close()
            flash(f"Homework for {class_name} on {hw_date} already exists. Confirm to overwrite.", "warning")
            return redirect(url_for("hw_tracker", hw_date=hw_date, class_name=class_name, confirm_needed=1))

        # Proceed to write:
        # - if confirm == "yes" (user accepted), allow overwriting (UPSERT)
        # - else (first time, no existing), regular UPSERT is fine
        for key in request.form.keys():
            if key.startswith("status_"):
                sid = key.split("_", 1)[1]
                status = request.form.get(f"status_{sid}")
                grade = (request.form.get(f"grade_{sid}") or "").strip() or None
                if status in ("done", "not_done"):
                    # allow overwrite only if confirm=yes or no previous records existed
                    cur = conn.cursor()
                    if existing_cnt > 0 and confirm != "yes":
                        # safety (shouldn't reach here due to redirect), but keep as guard
                        cur.close()
                        continue
                    cur.execute("""
                        INSERT INTO homework_records (hw_date, student_id, status, grade)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE status=VALUES(status), grade=VALUES(grade), updated_at=NOW();
                    """, (hw_date, sid, status, grade))
                    cur.close()

        conn.commit()
        conn.close()
        flash("Homework saved!", "success")
        return redirect(url_for("hw_tracker", hw_date=hw_date, class_name=class_name))

    # ------- GET -------
    hw_date = request.args.get("hw_date") or date.today().isoformat()
    selected_class = request.args.get("class_name")

    classes = fetch_classes(conn)
    if not selected_class and classes:
        selected_class = classes[0]["name"]

    students = fetch_students_by_class(conn, selected_class) if selected_class else []

    # existing marks (to prefill) + count for confirm
    marks = {}
    existing_cnt_for_page = 0
    if students:
        sids = tuple([s["student_id"] for s in students])
        placeholders = ",".join(["%s"] * len(sids)) if sids else "NULL"
        cur = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT student_id, status, grade
            FROM homework_records
            WHERE hw_date=%s AND student_id IN ({placeholders})
        """, (hw_date, *sids) if sids else (hw_date,))
        for row in cur.fetchall():
            marks[row["student_id"]] = {"status": row["status"], "grade": row["grade"]}
        cur.close()

        # also compute class-level existing count for this date
        existing_cnt_for_page = count_existing_hw_for_class_date(conn, selected_class, hw_date)

    conn.close()
    confirm_needed = request.args.get("confirm_needed", "0")
    return render_template(
        "hw_tracker.html",
        classes=classes,
        selected_class=selected_class,
        students=students,
        hw_date=hw_date,
        marks=marks,
        existing_cnt=existing_cnt_for_page,
        confirm_needed=confirm_needed
    )

# --- helpers ---
def count_students_in_class(conn, class_name):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM students WHERE class=%s", (class_name,))
    (cnt,) = cur.fetchone()
    cur.close()
    return cnt

# --- delete a single student (cascades HW via FK) ---
@app.route("/hw/delete-student", methods=["POST"])
def hw_delete_student():
    student_id = request.form.get("student_id")
    class_name = request.form.get("class_name")  # to come back to the same class tab
    if not student_id:
        flash("Student id missing.", "danger")
        return redirect(url_for("hw_tracker", class_name=class_name))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM students WHERE student_id=%s", (student_id,))
        conn.commit()
        flash("Student removed (and their homework records).", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not delete student: {e}", "danger")
    finally:
        cur.close(); conn.close()

    return redirect(url_for("hw_tracker", class_name=class_name))

# --- delete a class (optionally cascades all students in that class, and their HW) ---
@app.route("/hw/delete-class", methods=["POST"])
def hw_delete_class():
    class_name = (request.form.get("class_name") or "").strip()
    confirm = (request.form.get("confirm") or "no").lower()

    if not class_name:
        flash("Class name missing.", "danger")
        return redirect(url_for("hw_tracker"))

    conn = get_db_connection()
    try:
        students_cnt = count_students_in_class(conn, class_name)
        # If there are students and user hasn't confirmed, ask first
        if students_cnt > 0 and confirm != "yes":
            conn.close()
            flash(f"Class '{class_name}' has {students_cnt} student(s). Confirm to delete the class, all its students, and their homework.", "warning")
            return redirect(url_for("hw_tracker", class_name=class_name, del_confirm_class=class_name, del_students_cnt=students_cnt))

        cur = conn.cursor()
        # 1) remove students of this class (cascades homework_records)
        cur.execute("DELETE FROM students WHERE class=%s", (class_name,))
        # 2) remove class entry from classes table (if you maintain it)
        cur.execute("DELETE FROM classes WHERE name=%s", (class_name,))
        conn.commit()
        cur.close()
        flash(f"Class '{class_name}' deleted.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not delete class: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("hw_tracker"))


# ---------- Add Class ----------
@app.route("/hw/add-class", methods=["POST"])
def hw_add_class():
    name = (request.form.get("class_name") or "").strip()
    if not name:
        flash("Class name cannot be empty.", "danger")
        return redirect(url_for("hw_tracker"))
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO classes (name) VALUES (%s)", (name,))
        conn.commit()
        flash("Class added!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not add class: {e}", "danger")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for("hw_tracker", class_name=name))


# ---------- Add Student (under a class) ----------
@app.route("/hw/add-student", methods=["POST"])
def hw_add_student():
    name = (request.form.get("student_name") or "").strip()
    class_name = (request.form.get("student_class") or "").strip()
    if not name or not class_name:
        flash("Student name and class are required.", "danger")
        return redirect(url_for("hw_tracker"))
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO students (name, class) VALUES (%s, %s)", (name, class_name))
        conn.commit()
        flash("Student added!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not add student: {e}", "danger")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for("hw_tracker", class_name=class_name))


# ---------- View by Class ----------
@app.route("/hw/view/class")
def hw_view_class():
    class_name = request.args.get("class_name")
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    params = []
    where = ["s.class=%s"]
    params.append(class_name)

    if start:
        where.append("hr.hw_date >= %s")
        params.append(start)
    if end:
        where.append("hr.hw_date <= %s")
        params.append(end)

    cur.execute(f"""
        SELECT hr.hw_date, s.name AS student_name, s.class, hr.status, hr.grade
        FROM homework_records hr
        JOIN students s ON hr.student_id = s.student_id
        WHERE {" AND ".join(where)}
        ORDER BY hr.hw_date DESC, s.name;
    """, tuple(params))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("hw_view_class.html", class_name=class_name, rows=rows, start=start, end=end)


# ---------- View by Student ----------
@app.route("/hw/view/student")
def hw_view_student():
    student_id = request.args.get("student_id")
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    params = [student_id]
    where = ["hr.student_id=%s"]

    if start:
        where.append("hr.hw_date >= %s")
        params.append(start)
    if end:
        where.append("hr.hw_date <= %s")
        params.append(end)

    cur.execute(f"""
        SELECT hr.hw_date, s.name AS student_name, s.class, hr.status, hr.grade
        FROM homework_records hr
        JOIN students s ON hr.student_id = s.student_id
        WHERE {" AND ".join(where)}
        ORDER BY hr.hw_date DESC;
    """, tuple(params))
    rows = cur.fetchall()

    # student info for heading
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT student_id, name, class FROM students WHERE student_id=%s", (student_id,))
    student = cur.fetchone()
    cur.close()
    conn.close()

    return render_template("hw_view_student.html", student=student, rows=rows, start=start, end=end)


# ---------- CSV Exports ----------
@app.route("/hw/export/class.csv")
def hw_export_class_csv():
    class_name = request.args.get("class_name")
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    params = [class_name]
    where = ["s.class=%s"]

    if start:
        where.append("hr.hw_date >= %s")
        params.append(start)
    if end:
        where.append("hr.hw_date <= %s")
        params.append(end)

    cur.execute(f"""
        SELECT hr.hw_date, s.name AS student_name, s.class, hr.status, hr.grade
        FROM homework_records hr
        JOIN students s ON hr.student_id = s.student_id
        WHERE {" AND ".join(where)}
        ORDER BY hr.hw_date, s.name;
    """, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Student", "Class", "Status", "Grade"])
    for r in rows:
        date_str = r["hw_date"].strftime("%Y-%m-%d") if r["hw_date"] else ""
        writer.writerow([date_str, r["student_name"], r["class"], r["status"], r["grade"] or ""])
    
    csv_data = output.getvalue()
    output.close()

    filename = f"hw_{class_name}_{(start or 'start')}_{(end or 'end')}.csv"
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/hw/export/student.csv")
def hw_export_student_csv():
    student_id = request.args.get("student_id")
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    params = [student_id]
    where = ["hr.student_id=%s"]

    if start:
        where.append("hr.hw_date >= %s")
        params.append(start)
    if end:
        where.append("hr.hw_date <= %s")
        params.append(end)

    cur.execute(f"""
        SELECT hr.hw_date, s.name AS student_name, s.class, hr.status, hr.grade
        FROM homework_records hr
        JOIN students s ON hr.student_id = s.student_id
        WHERE {" AND ".join(where)}
        ORDER BY hr.hw_date;
    """, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

# find student name for filename
    student_name = (rows[0]["student_name"] if rows else f"id{student_id}") if student_id else "student"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Student", "Class", "Status", "Grade"])
    for r in rows:
        date_str = r["hw_date"].strftime("%Y-%m-%d") if r["hw_date"] else ""
        writer.writerow([date_str, r["student_name"], r["class"], r["status"], r["grade"] or ""])

    csv_data = output.getvalue()
    output.close()

    filename = f"hw_{student_name}_{(start or 'start')}_{(end or 'end')}.csv"
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/reports")
def reports():
    return redirect(url_for("hw_tracker"))

# --- Mobile roll call: quick tap UI (GET shows UI, POST saves) ---
@app.route("/hw/roll", methods=["GET", "POST"])
def hw_roll():
    from datetime import date
    conn = get_db_connection()

    if request.method == "POST":
        # expects JSON: { "class_name": "...", "hw_date": "YYYY-MM-DD", "marks": [{ "student_id": 1, "status": "done", "grade": "" }, ...] }
        data = request.get_json(silent=True) or {}
        class_name = (data.get("class_name") or "").strip()
        hw_date = data.get("hw_date") or date.today().isoformat()
        marks = data.get("marks") or []

        # Only-once-per-day rule with overwrite confirm flag
        confirm = (data.get("confirm") or "no").lower()
        existing_cnt = count_existing_hw_for_class_date(conn, class_name, hw_date)
        if existing_cnt > 0 and confirm != "yes":
            conn.close()
            return {"ok": False, "need_confirm": True, "message": f"Already marked for {class_name} on {hw_date}. Overwrite?"}, 409

        for item in marks:
            sid = str(item.get("student_id"))
            status = item.get("status")
            grade = (item.get("grade") or None)
            if status in ("done","not_done"):
                upsert_homework(conn, hw_date, sid, status, grade)
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Saved"}, 200

    # GET
    hw_date = request.args.get("hw_date") or date.today().isoformat()
    class_name = request.args.get("class_name")
    classes = fetch_classes(conn)
    if not class_name and classes:
        class_name = classes[0]["name"]
    students = fetch_students_by_class(conn, class_name) if class_name else []
    # prefill for today (optional)
    marks = {}
    if students:
        sids = tuple([s["student_id"] for s in students])
        placeholders = ",".join(["%s"] * len(sids)) if sids else "NULL"
        cur = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT student_id, status, grade
            FROM homework_records
            WHERE hw_date=%s AND student_id IN ({placeholders})
        """, (hw_date, *sids) if sids else (hw_date,))
        for r in cur.fetchall():
            marks[r["student_id"]] = {"status": r["status"], "grade": r["grade"]}
        cur.close()
    conn.close()
    return render_template("hw_roll.html",
                           classes=classes,
                           class_name=class_name,
                           hw_date=hw_date,
                           students=students,
                           marks=marks)


# if __name__ == "__main__":
#     app.run(debug=True)
