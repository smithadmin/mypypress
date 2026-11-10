"""MyPyPress - a small WordPress-style blogging platform (Flask + SQLite).

Features: multiple users, roles (admin / user), per-user "can post" permission,
posts with Markdown, tags, comments, search, drafts, and admin customization of
site name, tagline, banner (text + image) and theme colors.

All persistent state (database, uploads, secret key) lives in DATA_DIR.
"""
import html
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import markdown
from markdown.treeprocessors import Treeprocessor
from markupsafe import Markup
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)

# --------------------------------------------------------------------------
# Configuration / settings
# --------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "site_name": "My Blog",
    "tagline": "Just another Python-powered site",
    "banner_text": "",
    "banner_image": "",
    "allow_registration": "1",
    "posts_per_page": "10",
    "color_primary": "#2563eb",
    "color_bg": "#f3f4f6",
    "color_surface": "#ffffff",
    "color_text": "#1f2937",
    "color_header_text": "#ffffff",
}
COLOR_KEYS = ["primary", "bg", "surface", "text", "header_text"]
COLOR_LABELS = {
    "primary": "Primary (header, buttons, links)",
    "bg": "Page background",
    "surface": "Cards / content background",
    "text": "Body text",
    "header_text": "Header text",
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
  can_post INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('published','draft')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS post_tags (
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (post_id, tag_id)
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_secret(data_dir):
    """SECRET_KEY env var wins; otherwise a key is generated once and persisted."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = data_dir / "secret_key"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    return path.read_text().strip()


def configure(flask_app, data_dir):
    data = Path(data_dir).resolve()
    (data / "uploads").mkdir(parents=True, exist_ok=True)
    flask_app.config.update(
        DATA_DIR=data,
        DB_PATH=data / "blog.db",
        UPLOAD_DIR=data / "uploads",
        SECRET_KEY=load_secret(data),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )
    if os.environ.get("TRUST_PROXY") == "1":
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def connect():
    db = sqlite3.connect(app.config["DB_PATH"], timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = connect()
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            name = os.environ.get("ADMIN_USERNAME", "admin")
            pw = os.environ.get("ADMIN_PASSWORD")
            generated = not pw
            if generated:
                pw = secrets.token_urlsafe(12)
            try:
                db.execute(
                    "INSERT INTO users(username,password_hash,role,can_post,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (name, generate_password_hash(pw), "admin", 1, now()))
                db.commit()
                if generated:
                    print(f'*** Created admin user "{name}" with generated password: {pw}\n'
                          f'*** Log in and change it under Account (or set ADMIN_PASSWORD).',
                          file=sys.stderr, flush=True)
            except sqlite3.IntegrityError:
                pass  # another process created it first
    finally:
        db.close()


def get_settings():
    if "settings" not in g:
        s = dict(DEFAULT_SETTINGS)
        s.update({r["key"]: r["value"] for r in get_db().execute("SELECT key,value FROM settings")})
        g.settings = s
    return g.settings


# --------------------------------------------------------------------------
# Safe Markdown rendering (raw HTML is escaped, dangerous URLs neutralised)
# --------------------------------------------------------------------------
SAFE_URL = re.compile(r"^(?:https?:|mailto:|[/#?]|[^:/?#]*(?:[/?#]|$))", re.I)


class SafeUrls(Treeprocessor):
    def run(self, root):
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if v is not None and not SAFE_URL.match(re.sub(r"[\x00-\x20\x7f]+", "", v)):
                    el.set(attr, "#")
            if el.tag == "a":
                el.set("rel", "nofollow noopener ugc")


def render_markdown(text):
    md = markdown.Markdown(extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
                           output_format="html")
    md.preprocessors.deregister("html_block")   # raw HTML blocks -> plain text
    md.inlinePatterns.deregister("html")        # inline raw HTML -> plain text
    md.treeprocessors.register(SafeUrls(md), "safe_urls", 1)
    return md.convert(text)


@app.template_filter("md")
def md_filter(text):
    return Markup(render_markdown(text))


@app.template_filter("dt")
def dt_filter(value):
    try:
        d = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return f"{d:%b} {d.day}, {d.year}"
    except (TypeError, ValueError):
        return value or ""


def excerpt(body, n=280):
    text = html.unescape(re.sub(r"<[^>]+>", " ", render_markdown(body)))
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def slugify(text, fallback="post"):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or fallback


def unique_slug(db, title):
    base = slugify(title)
    slug, i = base, 2
    while db.execute("SELECT 1 FROM posts WHERE slug=?", (slug,)).fetchone():
        slug, i = f"{base}-{i}", i + 1
    return slug


# --------------------------------------------------------------------------
# Auth, CSRF, permissions
# --------------------------------------------------------------------------
FAILS = {}
FAIL_LIMIT, LOCK_SECONDS = 5, 300
DUMMY_HASH = generate_password_hash("not-a-real-password")


def throttled(key):
    e = FAILS.get(key)
    return bool(e and e[0] >= FAIL_LIMIT and time.time() < e[1])


def record_fail(key):
    if len(FAILS) > 5000:
        FAILS.clear()
    t = time.time()
    e = FAILS.get(key)
    if not e or t > e[1]:
        e = [0, 0]
    e[0] += 1
    e[1] = t + LOCK_SECONDS
    FAILS[key] = e


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        u = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if u:
            g.user = u
        else:
            session.clear()


@app.before_request
def csrf_protect():
    if request.method == "POST":
        tok = session.get("csrf", "")
        if not tok or not secrets.compare_digest(tok, request.form.get("_csrf", "")):
            abort(400, "Your session expired or the form was invalid. Please go back, reload and try again.")


@app.template_global()
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def is_admin():
    return bool(g.user and g.user["role"] == "admin")


def can_post():
    return bool(g.user and (g.user["role"] == "admin" or g.user["can_post"]))


def can_edit(post):
    return bool(g.user and (g.user["role"] == "admin" or
                            (g.user["can_post"] and post["author_id"] == g.user["id"])))


def can_delete_post(post):
    # Spec: admins write to everything; authors only edit their own posts.
    # To let authors delete their own posts, return can_edit(post) instead.
    #return is_admin()       # Uncomment this line for only Admin to delete post
    return can_edit(post)    # Logged-in users can delete their own posts too


def can_delete_comment(c):
    return bool(g.user and (is_admin() or c["user_id"] == g.user["id"]))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path if request.method == "GET" else None))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not is_admin():
            abort(403)
        return f(*a, **kw)
    return wrapper


def poster_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not can_post():
            abort(403, "An administrator has not enabled posting for your account.")
        return f(*a, **kw)
    return wrapper


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


@app.context_processor
def inject():
    s = get_settings()
    css = []
    for k in COLOR_KEYS:
        v = s.get("color_" + k, "")
        if not HEX_RE.match(v):
            v = DEFAULT_SETTINGS["color_" + k]
        css.append(f"--{k}:{v};")
    return dict(site=s, theme_css=":root{" + "".join(css) + "}", is_admin=is_admin,
                can_post=can_post, can_edit=can_edit, can_delete_post=can_delete_post,
                can_delete_comment=can_delete_comment)


@app.template_global()
def popular_tags():
    return get_db().execute(
        "SELECT t.name, t.slug, COUNT(*) AS n FROM tags t "
        "JOIN post_tags pt ON pt.tag_id=t.id JOIN posts p ON p.id=pt.post_id "
        "WHERE p.status='published' GROUP BY t.id ORDER BY n DESC, t.name LIMIT 30").fetchall()


@app.template_global()
def page_url(p):
    args = dict(request.view_args or {})
    args.update(request.args.to_dict())
    args["page"] = p
    return url_for(request.endpoint, **args)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
    return resp


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(e):
    msgs = {403: "You don't have permission to do that.", 404: "That page doesn't exist.",
            413: "That upload is too large (6 MB max)."}
    detail = e.description if e.code in (400, 403) and e.description and \
        not e.description.startswith(("The browser", "You don't have the permission")) else msgs.get(e.code, e.description)
    return render_template("error.html", code=e.code, message=detail), e.code


# --------------------------------------------------------------------------
# Post queries
# --------------------------------------------------------------------------
POST_SELECT = ("SELECT p.*, u.username AS author, "
               "(SELECT COUNT(*) FROM comments c WHERE c.post_id=p.id) AS comment_count "
               "FROM posts p JOIN users u ON u.id=p.author_id")


def attach_tags(posts):
    if not posts:
        return posts
    ids = [p["id"] for p in posts]
    rows = get_db().execute(
        "SELECT pt.post_id, t.name, t.slug FROM post_tags pt JOIN tags t ON t.id=pt.tag_id "
        f"WHERE pt.post_id IN ({','.join('?' * len(ids))}) ORDER BY t.name", ids).fetchall()
    by_post = {}
    for r in rows:
        by_post.setdefault(r["post_id"], []).append({"name": r["name"], "slug": r["slug"]})
    for p in posts:
        p["tags"] = by_post.get(p["id"], [])
    return posts


def paged_posts(where="1=1", params=(), order="p.created_at DESC, p.id DESC"):
    db = get_db()
    per = max(1, min(50, int(get_settings().get("posts_per_page") or 10)))
    total = db.execute(f"SELECT COUNT(*) FROM posts p WHERE {where}", params).fetchone()[0]
    pages = max(1, -(-total // per))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    rows = db.execute(f"{POST_SELECT} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                      (*params, per, (page - 1) * per)).fetchall()
    posts = attach_tags([dict(r) for r in rows])
    for p in posts:
        p["excerpt"] = excerpt(p["body"])
    return posts, page, pages, total


def parse_tags(raw):
    seen = {}
    for part in raw.split(","):
        name = " ".join(part.strip().lower().split())[:30]
        slug = slugify(name, fallback="")
        if slug and slug not in seen:
            seen[slug] = name
    return list(seen.items())[:10]


def set_tags(db, post_id, raw):
    db.execute("DELETE FROM post_tags WHERE post_id=?", (post_id,))
    for slug, name in parse_tags(raw):
        db.execute("INSERT OR IGNORE INTO tags(name,slug) VALUES (?,?)", (name, slug))
        tid = db.execute("SELECT id FROM tags WHERE slug=?", (slug,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO post_tags(post_id,tag_id) VALUES (?,?)", (post_id, tid))
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM post_tags)")


def get_post_or_404(slug):
    row = get_db().execute(f"{POST_SELECT} WHERE p.slug=?", (slug,)).fetchone()
    if not row:
        abort(404)
    post = attach_tags([dict(row)])[0]
    if post["status"] == "draft" and not can_edit(post):
        abort(404)
    return post


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return "ok"


@app.route("/")
def index():
    posts, page, pages, _ = paged_posts("p.status='published'")
    return render_template("index.html", posts=posts, page=page, pages=pages, heading=None)


@app.route("/tag/<slug>")
def tag(slug):
    t = get_db().execute("SELECT * FROM tags WHERE slug=?", (slug,)).fetchone()
    if not t:
        abort(404)
    posts, page, pages, _ = paged_posts(
        "p.status='published' AND p.id IN (SELECT post_id FROM post_tags WHERE tag_id=?)", (t["id"],))
    return render_template("index.html", posts=posts, page=page, pages=pages,
                           heading=f"Tag: {t['name']}")


def like_escape(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()[:200]
    terms = q.split()[:8]
    if not terms:
        return render_template("index.html", posts=[], page=1, pages=1, heading="Search",
                               search_q=q)
    clause = ("(p.title LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\' OR EXISTS ("
              "SELECT 1 FROM post_tags pt JOIN tags t ON t.id=pt.tag_id "
              "WHERE pt.post_id=p.id AND t.name LIKE ? ESCAPE '\\'))")
    where = "p.status='published' AND " + " AND ".join([clause] * len(terms))
    params = []
    for term in terms:
        params += [like_escape(term)] * 3
    posts, page, pages, total = paged_posts(where, tuple(params))
    return render_template("index.html", posts=posts, page=page, pages=pages, search_q=q,
                           heading=f"{total} result{'s' if total != 1 else ''} for “{q}”")


@app.route("/post/<slug>")
def view_post(slug):
    post = get_post_or_404(slug)
    comments = get_db().execute(
        "SELECT c.*, u.username FROM comments c LEFT JOIN users u ON u.id=c.user_id "
        "WHERE c.post_id=? ORDER BY c.created_at, c.id", (post["id"],)).fetchall()
    return render_template("post.html", post=post, comments=comments)


@app.route("/post/<slug>/comment", methods=["POST"])
@login_required
def add_comment(slug):
    post = get_post_or_404(slug)
    if post["status"] != "published":
        abort(404)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment can't be empty.", "error")
    elif len(body) > 5000:
        flash("Comment is too long (5000 characters max).", "error")
    else:
        db = get_db()
        db.execute("INSERT INTO comments(post_id,user_id,body,created_at) VALUES (?,?,?,?)",
                   (post["id"], g.user["id"], body, now()))
        db.commit()
        flash("Comment added.", "ok")
    return redirect(url_for("view_post", slug=slug) + "#comments")


@app.route("/comment/<int:cid>/delete", methods=["POST"])
@login_required
def delete_comment(cid):
    db = get_db()
    c = db.execute("SELECT c.*, p.slug FROM comments c JOIN posts p ON p.id=c.post_id WHERE c.id=?",
                   (cid,)).fetchone()
    if not c:
        abort(404)
    if not can_delete_comment(c):
        abort(403)
    db.execute("DELETE FROM comments WHERE id=?", (cid,))
    db.commit()
    flash("Comment deleted.", "ok")
    return redirect(url_for("view_post", slug=c["slug"]) + "#comments")


@app.route("/uploads/<path:filename>")
def uploads(filename):
    if not UPLOAD_NAME_RE.match(filename):
        abort(404)
    return send_from_directory(app.config["UPLOAD_DIR"], filename, max_age=3600)


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
def validate_new_password(pw, confirm):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 200:
        return "Password is too long."
    if pw != confirm:
        return "Passwords do not match."
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        key = (request.remote_addr, username.lower())
        if throttled(key):
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html", next=request.form.get("next", "")), 429
        u = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, pw) and u and u["active"]
        if not ok:
            record_fail(key)
            flash("Invalid username or password.", "error")
            return render_template("login.html", next=request.form.get("next", "")), 401
        FAILS.pop(key, None)
        session.clear()
        session["uid"] = u["id"]
        session.permanent = True
        return redirect(safe_next(request.form.get("next")) or url_for("index"))
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "ok")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_settings()["allow_registration"] != "1":
        abort(404)
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        err = None
        if not USERNAME_RE.match(username):
            err = "Username must be 3-32 characters: letters, digits, dot, dash, underscore."
        else:
            err = validate_new_password(request.form.get("password", ""),
                                        request.form.get("confirm", ""))
        db = get_db()
        if not err and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            err = "That username is taken."
        if err:
            flash(err, "error")
            return render_template("register.html", username=username), 400
        cur = db.execute("INSERT INTO users(username,password_hash,created_at) VALUES (?,?,?)",
                         (username, generate_password_hash(request.form["password"]), now()))
        db.commit()
        session.clear()
        session["uid"] = cur.lastrowid
        session.permanent = True
        flash("Welcome! Your account has been created.", "ok")
        return redirect(url_for("index"))
    return render_template("register.html", username="")


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
            flash("Current password is incorrect.", "error")
        else:
            err = validate_new_password(request.form.get("password", ""), request.form.get("confirm", ""))
            if err:
                flash(err, "error")
            else:
                db = get_db()
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (generate_password_hash(request.form["password"]), g.user["id"]))
                db.commit()
                flash("Password changed.", "ok")
                return redirect(url_for("account"))
    return render_template("account.html")


# --------------------------------------------------------------------------
# Writing posts
# --------------------------------------------------------------------------
@app.route("/dashboard")
@poster_required
def dashboard():
    if is_admin():
        posts, page, pages, _ = paged_posts()
    else:
        posts, page, pages, _ = paged_posts("p.author_id=?", (g.user["id"],))
    return render_template("dashboard.html", posts=posts, page=page, pages=pages)


def validate_post_form(form):
    title, body = form.get("title", "").strip(), form.get("body", "").strip()
    status = "draft" if form.get("status") == "draft" else "published"
    if not title or len(title) > 200:
        return None, "Title is required (200 characters max)."
    if not body or len(body) > 100_000:
        return None, "Body is required (100,000 characters max)."
    return dict(title=title, body=body, status=status, tags=form.get("tags", "")), None


@app.route("/new", methods=["GET", "POST"])
@poster_required
def new_post():
    values = dict(title="", body="", tags="", status="published")
    if request.method == "POST":
        data, err = validate_post_form(request.form)
        values = {**values, **request.form.to_dict()}
        if err:
            flash(err, "error")
        else:
            db = get_db()
            ts = now()
            cur = db.execute(
                "INSERT INTO posts(author_id,title,slug,body,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (g.user["id"], data["title"], unique_slug(db, data["title"]), data["body"],
                 data["status"], ts, ts))
            set_tags(db, cur.lastrowid, data["tags"])
            db.commit()
            slug = db.execute("SELECT slug FROM posts WHERE id=?", (cur.lastrowid,)).fetchone()["slug"]
            flash("Post saved.", "ok")
            return redirect(url_for("view_post", slug=slug))
    return render_template("editor.html", values=values, post=None)


@app.route("/post/<slug>/edit", methods=["GET", "POST"])
@login_required
def edit_post(slug):
    post = get_post_or_404(slug)
    if not can_edit(post):
        abort(403)
    values = dict(title=post["title"], body=post["body"], status=post["status"],
                  tags=", ".join(t["name"] for t in post["tags"]))
    if request.method == "POST":
        data, err = validate_post_form(request.form)
        values = {**values, **request.form.to_dict()}
        if err:
            flash(err, "error")
        else:
            db = get_db()
            db.execute("UPDATE posts SET title=?, body=?, status=?, updated_at=? WHERE id=?",
                       (data["title"], data["body"], data["status"], now(), post["id"]))
            set_tags(db, post["id"], data["tags"])
            db.commit()
            flash("Post updated.", "ok")
            return redirect(url_for("view_post", slug=slug))
    return render_template("editor.html", values=values, post=post)


@app.route("/post/<slug>/delete", methods=["POST"])
@login_required
def delete_post(slug):
    post = get_post_or_404(slug)
    if not can_delete_post(post):
        abort(403)
    db = get_db()
    db.execute("DELETE FROM posts WHERE id=?", (post["id"],))
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM post_tags)")
    db.commit()
    flash("Post deleted.", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Administration
# --------------------------------------------------------------------------
def detect_image(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def remove_upload(name):
    if name and UPLOAD_NAME_RE.match(name):
        (app.config["UPLOAD_DIR"] / name).unlink(missing_ok=True)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    values = dict(get_settings())
    if request.method == "POST":
        f = request.form
        new = {
            "site_name": f.get("site_name", "").strip(),
            "tagline": f.get("tagline", "").strip(),
            "banner_text": f.get("banner_text", "").strip(),
            "allow_registration": "1" if f.get("allow_registration") else "0",
            "posts_per_page": f.get("posts_per_page", "10").strip(),
        }
        for k in COLOR_KEYS:
            new["color_" + k] = f.get("color_" + k, "").strip()
        values.update(new)
        errors = []
        if not new["site_name"] or len(new["site_name"]) > 80:
            errors.append("Site name is required (80 characters max).")
        if len(new["tagline"]) > 200 or len(new["banner_text"]) > 200:
            errors.append("Tagline and banner text are limited to 200 characters.")
        if not (new["posts_per_page"].isdigit() and 1 <= int(new["posts_per_page"]) <= 50):
            errors.append("Posts per page must be between 1 and 50.")
        for k in COLOR_KEYS:
            if not HEX_RE.match(new["color_" + k]):
                errors.append(f"Invalid color for “{COLOR_LABELS[k]}”.")
        upload = request.files.get("banner_image")
        image_data = ext = None
        if upload and upload.filename:
            image_data = upload.read()
            ext = detect_image(image_data[:16])
            if not ext:
                errors.append("Banner must be a PNG, JPEG, GIF or WebP image.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_settings.html", values=values, labels=COLOR_LABELS,
                                   colors=COLOR_KEYS), 400
        old = get_settings().get("banner_image", "")
        if image_data:
            name = f"{secrets.token_hex(8)}.{ext}"
            (app.config["UPLOAD_DIR"] / name).write_bytes(image_data)
            new["banner_image"] = name
            remove_upload(old)
        elif f.get("remove_banner"):
            new["banner_image"] = ""
            remove_upload(old)
        db = get_db()
        db.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", list(new.items()))
        db.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", values=values, labels=COLOR_LABELS, colors=COLOR_KEYS)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()
    if request.method == "POST":  # create user
        username = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if not USERNAME_RE.match(username):
            flash("Username must be 3-32 characters: letters, digits, dot, dash, underscore.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            flash("That username is taken.", "error")
        else:
            role = "admin" if request.form.get("role") == "admin" else "user"
            db.execute("INSERT INTO users(username,password_hash,role,can_post,created_at) VALUES (?,?,?,?,?)",
                       (username, generate_password_hash(pw), role,
                        1 if request.form.get("can_post") or role == "admin" else 0, now()))
            db.commit()
            flash(f"User {username} created.", "ok")
        return redirect(url_for("admin_users"))
    users = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM posts WHERE author_id=u.id) AS post_count, "
        "(SELECT COUNT(*) FROM comments WHERE user_id=u.id) AS comment_count "
        "FROM users u ORDER BY u.username COLLATE NOCASE").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:uid>/<action>", methods=["POST"])
@admin_required
def admin_user_action(uid, action):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    if u["id"] == g.user["id"]:
        flash("You can't change your own role or status here (this prevents lock-out).", "error")
        return redirect(url_for("admin_users"))
    if action == "toggle_post":
        db.execute("UPDATE users SET can_post=1-can_post WHERE id=?", (uid,))
        flash(f"Posting {'disabled' if u['can_post'] else 'enabled'} for {u['username']}.", "ok")
    elif action == "toggle_admin":
        db.execute("UPDATE users SET role=? WHERE id=?", ("user" if u["role"] == "admin" else "admin", uid))
        flash(f"Role changed for {u['username']}.", "ok")
    elif action == "toggle_active":
        db.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
        flash(f"{u['username']} {'deactivated' if u['active'] else 'reactivated'}.", "ok")
    elif action == "reset_password":
        pw = request.form.get("password", "")
        if len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
            flash(f"Password reset for {u['username']}.", "ok")
    elif action == "delete":
        # Their posts are kept and reassigned to the deleting admin; comments become anonymous.
        db.execute("UPDATE posts SET author_id=? WHERE author_id=?", (g.user["id"], uid))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        flash(f"User {u['username']} deleted; their posts were reassigned to you.", "ok")
    else:
        abort(404)
    db.commit()
    return redirect(url_for("admin_users"))


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
if os.environ.get("MYPYPRESS_NO_AUTOINIT") != "1":
    configure(app, os.environ.get("DATA_DIR", BASE_DIR / "data"))
    init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=False)
