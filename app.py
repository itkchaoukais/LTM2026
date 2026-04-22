# ============================
# APP.PY VERSION CORRIGÉE
# ============================

from flask import Flask, render_template, request, jsonify, session, send_file
import pyodbc
import os, hashlib, secrets, base64, io
from datetime import datetime
from functools import wraps
from waitress import serve

# ----------------------------
# FLASK APP
# ----------------------------
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = False   # True si HTTPS
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# ----------------------------
# SQL SERVER CONFIG
# ----------------------------
SQL_SERVER = '192.168.213.128'
SQL_DATABASE = 'conges'
SQL_USER = 'ADMIN'
SQL_PASSWORD = 'ADMIN'
SQL_DRIVER = '{ODBC Driver 17 for SQL Server}'

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ----------------------------
# DATABASE FUNCTIONS
# ----------------------------
def conn_str(db):
    return f"""
    DRIVER={SQL_DRIVER};
    SERVER={SQL_SERVER};
    DATABASE={db};
    UID={SQL_USER};
    PWD={SQL_PASSWORD};
    TrustServerCertificate=yes;
    """

def get_db(db=SQL_DATABASE, autocommit=False):
    return pyodbc.connect(conn_str(db), autocommit=autocommit)

def row_to_dict(cursor, row):
    return {col[0]: val for col, val in zip(cursor.description, row)}

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ----------------------------
# INIT DATABASE
# ----------------------------
def init_db():
    with get_db('master', autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("IF DB_ID('conges') IS NULL CREATE DATABASE conges")

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
        IF OBJECT_ID('utilisateurs','U') IS NULL
        CREATE TABLE utilisateurs(
            id INT IDENTITY(1,1) PRIMARY KEY,
            nom NVARCHAR(100),
            prenom NVARCHAR(100),
            email NVARCHAR(200) UNIQUE,
            mot_de_passe NVARCHAR(255),
            role NVARCHAR(50) DEFAULT 'employe',
            departement NVARCHAR(100),
            superviseur_id INT NULL,
            date_creation DATETIME DEFAULT GETDATE()
        )
        """)

        cur.execute("""
        IF OBJECT_ID('demandes_conge','U') IS NULL
        CREATE TABLE demandes_conge(
            id INT IDENTITY(1,1) PRIMARY KEY,
            employe_id INT,
            type_conge NVARCHAR(100),
            date_debut DATE,
            date_fin DATE,
            nb_jours INT,
            motif NVARCHAR(MAX),
            statut NVARCHAR(50) DEFAULT 'en_attente',
            document_nom NVARCHAR(255),
            document_data VARBINARY(MAX),
            document_type NVARCHAR(100),
            commentaire_superviseur NVARCHAR(MAX),
            date_demande DATETIME DEFAULT GETDATE(),
            date_traitement DATETIME,
            superviseur_id INT NULL
        )
        """)

        # comptes par défaut
        admin_pw = hash_password("Admin123!")
        sup_pw = hash_password("Super123!")
        emp_pw = hash_password("Employe123!")

        cur.execute("""
        IF NOT EXISTS (SELECT 1 FROM utilisateurs WHERE email='admin@entreprise.com')
        INSERT INTO utilisateurs(nom,prenom,email,mot_de_passe,role,departement)
        VALUES ('Admin','Systeme','admin@entreprise.com',?,'admin','Direction')
        """, admin_pw)

        cur.execute("""
        IF NOT EXISTS (SELECT 1 FROM utilisateurs WHERE email='superviseur@entreprise.com')
        INSERT INTO utilisateurs(nom,prenom,email,mot_de_passe,role,departement)
        VALUES ('Dupont','Marie','superviseur@entreprise.com',?,'superviseur','RH')
        """, sup_pw)

        cur.execute("SELECT id FROM utilisateurs WHERE email='superviseur@entreprise.com'")
        row = cur.fetchone()

        if row:
            cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM utilisateurs WHERE email='employe@entreprise.com')
            INSERT INTO utilisateurs(nom,prenom,email,mot_de_passe,role,departement,superviseur_id)
            VALUES ('Martin','Jean','employe@entreprise.com',?,'employe','RH',?)
            """, emp_pw, row[0])

        conn.commit()

# ----------------------------
# AUTH
# ----------------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Non authentifié"}), 401
        return fn(*args, **kwargs)
    return wrapper

def get_current_user():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM utilisateurs WHERE id=?", session["user_id"])
        row = cur.fetchone()
        return row_to_dict(cur, row) if row else None

# ----------------------------
# ROUTES
# ----------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email", "").strip().lower()
    pw = hash_password(data.get("mot_de_passe", ""))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM utilisateurs WHERE email=? AND mot_de_passe=?", email, pw)
        row = cur.fetchone()

        if not row:
            return jsonify({"error": "Email ou mot de passe incorrect"}), 401

        user = row_to_dict(cur, row)

    session["user_id"] = user["id"]
    return jsonify(user)

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me")
@login_required
def me():
    return jsonify(get_current_user())

@app.route("/api/demandes", methods=["GET"])
@login_required
def demandes():
    user = get_current_user()

    with get_db() as conn:
        cur = conn.cursor()

        if user["role"] == "employe":
            cur.execute("SELECT * FROM demandes_conge WHERE employe_id=?", user["id"])
        else:
            cur.execute("SELECT * FROM demandes_conge")

        rows = cur.fetchall()
        result = []

        for row in rows:
            item = row_to_dict(cur, row)
            item.pop("document_data", None)
            result.append(item)

        return jsonify(result)

@app.route("/api/demandes", methods=["POST"])
@login_required
def creer_demande():
    user = get_current_user()

    if user["role"] != "employe":
        return jsonify({"error": "Réservé aux employés"}), 403

    data = request.json

    d1 = datetime.strptime(data["date_debut"], "%Y-%m-%d")
    d2 = datetime.strptime(data["date_fin"], "%Y-%m-%d")
    nb = (d2 - d1).days + 1

    raw = None
    if data.get("document_data"):
        raw = base64.b64decode(data["document_data"].split(",")[-1])

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
        INSERT INTO demandes_conge(
            employe_id,type_conge,date_debut,date_fin,nb_jours,motif,
            document_nom,document_data,document_type
        )
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        user["id"],
        data["type_conge"],
        data["date_debut"],
        data["date_fin"],
        nb,
        data.get("motif"),
        data.get("document_nom"),
        raw,
        data.get("document_type")
        )

        conn.commit()

    return jsonify({"ok": True})

@app.route("/api/stats")
@login_required
def stats():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) total FROM demandes_conge")
        row = cur.fetchone()
        return jsonify({"total": row[0]})

# ----------------------------
# MAIN
# ----------------------------
if __name__ == "__main__":
    init_db()

    print("✅ Base SQL Server initialisée")
    print("📱 Application disponible sur : http://localhost:5000")
    print("👤 Admin : admin@entreprise.com / Admin123!")

    # VERSION PRODUCTION
    serve(app, host="0.0.0.0", port=5000)