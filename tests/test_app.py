"""End-to-end tests using Flask's test client. Run:  python -m unittest discover -s tests -v"""
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["MYPYPRESS_NO_AUTOINIT"] = "1"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "adminpass1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as blog  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        blog.configure(blog.app, self.tmp.name)
        blog.init_db()
        blog.FAILS.clear()
        blog.app.testing = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        c = blog.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        return c

    def post(self, c, url, data=None, **kw):
        d = dict(data or {})
        with c.session_transaction() as sess:   # login/register rotate the session, so use the live token
            d["_csrf"] = sess.setdefault("csrf", "tok")
        return c.post(url, data=d, **kw)

    def login(self, c, user, pw):
        return self.post(c, "/login", {"username": user, "password": pw})

    def admin(self):
        c = self.client()
        self.assertEqual(self.login(c, "admin", "adminpass1").status_code, 302)
        return c

    def register(self, name="alice", pw="password123"):
        c = self.client()
        r = self.post(c, "/register", {"username": name, "password": pw, "confirm": pw})
        self.assertEqual(r.status_code, 302)
        return c

    def make_user(self, admin_c, name, can_post=False):
        self.post(admin_c, "/admin/users", {"username": name, "password": "password123",
                                            "can_post": "1" if can_post else ""})
        c = self.client()
        self.login(c, name, "password123")
        return c

    def new_post(self, c, title="Hello World", body="First *post*", tags="python, web", status="published"):
        return self.post(c, "/new", {"title": title, "body": body, "tags": tags, "status": status})


class VisitorTests(Base):
    def test_admin_bootstrapped_and_home_loads(self):
        r = self.client().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"My Blog", r.data)

    def test_visitor_reads_but_cannot_write_or_comment(self):
        a = self.admin()
        self.new_post(a)
        v = self.client()
        self.assertIn(b"Hello World", v.get("/").data)
        self.assertEqual(v.get("/post/hello-world").status_code, 200)
        self.assertIn(b"to comment", v.get("/post/hello-world").data)
        self.assertEqual(v.get("/new").status_code, 302)
        self.assertEqual(v.get("/admin/settings").status_code, 302)
        r = self.post(v, "/post/hello-world/comment", {"body": "spam"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertNotIn(b"spam", v.get("/post/hello-world").data)

    def test_draft_hidden_from_visitors(self):
        a = self.admin()
        self.new_post(a, title="Secret", status="draft")
        v = self.client()
        self.assertEqual(v.get("/post/secret").status_code, 404)
        self.assertNotIn(b"Secret", v.get("/").data)
        self.assertEqual(a.get("/post/secret").status_code, 200)

    def test_csrf_required(self):
        c = blog.app.test_client()
        r = c.post("/login", data={"username": "admin", "password": "adminpass1"})
        self.assertEqual(r.status_code, 400)


class UserTests(Base):
    def test_register_login_logout(self):
        c = self.register()
        self.assertIn(b"alice", c.get("/").data)
        self.post(c, "/logout")
        self.assertIn(b"Log in", c.get("/").data)
        self.assertEqual(self.login(c, "alice", "password123").status_code, 302)
        self.assertEqual(self.login(self.client(), "alice", "wrong").status_code, 401)

    def test_registration_validation_and_duplicate(self):
        c = self.client()
        self.assertEqual(self.post(c, "/register", {"username": "a", "password": "password123", "confirm": "password123"}).status_code, 400)
        self.assertEqual(self.post(c, "/register", {"username": "bob", "password": "short", "confirm": "short"}).status_code, 400)
        self.register("bob")
        self.assertEqual(self.post(self.client(), "/register", {"username": "BOB", "password": "password123", "confirm": "password123"}).status_code, 400)

    def test_registration_can_be_disabled(self):
        a = self.admin()
        self.post(a, "/admin/settings", self.settings_form(allow_registration=""))
        self.assertEqual(self.client().get("/register").status_code, 404)

    def settings_form(self, **over):
        d = {"site_name": "My Blog", "tagline": "t", "banner_text": "", "posts_per_page": "10",
             "allow_registration": "1", "color_primary": "#2563eb", "color_bg": "#f3f4f6",
             "color_surface": "#ffffff", "color_text": "#1f2937", "color_header_text": "#ffffff"}
        d.update(over)
        return d

    def test_user_comments_but_cannot_post_until_enabled(self):
        a = self.admin()
        self.new_post(a)
        u = self.register("carol")
        r = self.post(u, "/post/hello-world/comment", {"body": "Nice post!"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"Nice post!", self.client().get("/post/hello-world").data)  # visitors see comments
        self.assertEqual(u.get("/new").status_code, 403)
        self.assertEqual(self.new_post(u, title="Nope").status_code, 403)
        # admin enables posting
        uid = blog.connect().execute("SELECT id FROM users WHERE username='carol'").fetchone()["id"]
        self.post(a, f"/admin/users/{uid}/toggle_post")
        self.assertEqual(u.get("/new").status_code, 200)
        self.assertEqual(self.new_post(u, title="Carol Post").status_code, 302)

    def test_empty_and_long_comments_rejected(self):
        a = self.admin()
        self.new_post(a)
        u = self.register()
        self.post(u, "/post/hello-world/comment", {"body": "   "})
        self.post(u, "/post/hello-world/comment", {"body": "x" * 5001})
        n = blog.connect().execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        self.assertEqual(n, 0)

    def test_comment_deletion_rules(self):
        a = self.admin()
        self.new_post(a)
        u1, u2 = self.register("user1"), self.register("user2")
        self.post(u1, "/post/hello-world/comment", {"body": "mine"})
        cid = blog.connect().execute("SELECT id FROM comments").fetchone()["id"]
        self.assertEqual(self.post(u2, f"/comment/{cid}/delete").status_code, 403)
        self.assertEqual(self.post(u1, f"/comment/{cid}/delete").status_code, 302)
        self.post(u1, "/post/hello-world/comment", {"body": "again"})
        cid = blog.connect().execute("SELECT id FROM comments").fetchone()["id"]
        self.assertEqual(self.post(a, f"/comment/{cid}/delete").status_code, 302)
        self.assertEqual(blog.connect().execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)

    def test_change_password(self):
        c = self.register("dave")
        self.post(c, "/account", {"current": "password123", "password": "newpassword9", "confirm": "newpassword9"})
        self.assertEqual(self.login(self.client(), "dave", "newpassword9").status_code, 302)
        self.assertEqual(self.login(self.client(), "dave", "password123").status_code, 401)

    def test_login_throttle(self):
        c = self.client()
        for _ in range(5):
            self.assertEqual(self.login(c, "admin", "bad").status_code, 401)
        self.assertEqual(self.login(c, "admin", "adminpass1").status_code, 429)

    def test_open_redirect_blocked(self):
        c = self.client()
        r = self.post(c, "/login", {"username": "admin", "password": "adminpass1", "next": "//evil.com"})
        self.assertEqual(r.headers["Location"], "/")


class PostPermissionTests(Base):
    def test_author_edits_own_not_others_admin_edits_all(self):
        a = self.admin()
        alice = self.make_user(a, "alice", can_post=True)
        bob = self.make_user(a, "bob", can_post=True)
        self.new_post(alice, title="Alice Post", body="alice body", tags="one")
        self.new_post(bob, title="Bob Post", body="bob body", tags="two")
        # alice edits own
        r = self.post(alice, "/post/alice-post/edit", {"title": "Alice Post", "body": "alice v2", "tags": "one", "status": "published"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"alice v2", alice.get("/post/alice-post").data)
        # alice cannot edit bob's
        self.assertEqual(alice.get("/post/bob-post/edit").status_code, 403)
        self.assertEqual(self.post(alice, "/post/bob-post/edit", {"title": "x", "body": "hacked", "tags": "", "status": "published"}).status_code, 403)
        # alice cannot delete her own (admin-only by spec)
        self.assertEqual(self.post(alice, "/post/alice-post/delete").status_code, 403)
        # admin edits bob's
        r = self.post(a, "/post/bob-post/edit", {"title": "Bob Post", "body": "edited by admin", "tags": "two", "status": "published"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"edited by admin", self.client().get("/post/bob-post").data)
        # admin deletes
        self.assertEqual(self.post(a, "/post/bob-post/delete").status_code, 302)
        self.assertEqual(self.client().get("/post/bob-post").status_code, 404)

    def test_revoked_poster_loses_edit_access(self):
        a = self.admin()
        alice = self.make_user(a, "alice", can_post=True)
        self.new_post(alice, title="Mine")
        uid = blog.connect().execute("SELECT id FROM users WHERE username='alice'").fetchone()["id"]
        self.post(a, f"/admin/users/{uid}/toggle_post")
        self.assertEqual(alice.get("/post/mine/edit").status_code, 403)

    def test_unique_slugs(self):
        a = self.admin()
        self.new_post(a, title="Same Title")
        self.new_post(a, title="Same Title")
        self.assertEqual(self.client().get("/post/same-title-2").status_code, 200)

    def test_post_validation(self):
        a = self.admin()
        r = self.new_post(a, title="", body="x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(blog.connect().execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)


class TagSearchTests(Base):
    def test_tags_and_tag_pages(self):
        a = self.admin()
        self.new_post(a, title="P1", tags="Python, Web Dev")
        self.new_post(a, title="P2", tags="python")
        v = self.client()
        home = v.get("/").data
        self.assertIn(b"web dev", home)
        r = v.get("/tag/python")
        self.assertIn(b"P1", r.data); self.assertIn(b"P2", r.data)
        r = v.get("/tag/web-dev")
        self.assertIn(b"P1", r.data); self.assertNotIn(b"P2", r.data)
        self.assertEqual(v.get("/tag/nonexistent").status_code, 404)

    def test_orphan_tags_removed(self):
        a = self.admin()
        self.new_post(a, title="P1", tags="temp")
        self.post(a, "/post/p1/edit", {"title": "P1", "body": "b", "tags": "other", "status": "published"})
        self.assertEqual(self.client().get("/tag/temp").status_code, 404)

    def test_search(self):
        a = self.admin()
        self.new_post(a, title="Learning Flask", body="Building web apps with Python", tags="tutorial")
        self.new_post(a, title="Cooking", body="Pasta recipes", tags="food")
        self.new_post(a, title="Hidden Flask Draft", body="flask", status="draft")
        v = self.client()
        r = v.get("/search?q=flask").data
        self.assertIn(b"Learning Flask", r); self.assertNotIn(b"Cooking", r); self.assertNotIn(b"Hidden Flask", r)
        self.assertIn(b"Learning Flask", v.get("/search?q=python+web").data)   # AND of terms, in body
        self.assertIn(b"Cooking", v.get("/search?q=food").data)                 # matches tag
        self.assertIn(b"0 results", v.get("/search?q=zzzz").data)
        self.assertIn(b"0 results", v.get("/search?q=%25").data)                # wildcard chars are literal
        self.assertEqual(v.get("/search").status_code, 200)

    def test_pagination(self):
        a = self.admin()
        for i in range(12):
            self.new_post(a, title=f"Post {i:02d}", tags="")
        v = self.client()
        self.assertIn(b"Page 1 of 2", v.get("/").data)
        self.assertIn(b"Post 00", v.get("/?page=2").data)
        self.assertEqual(v.get("/?page=999").status_code, 200)


class SecurityTests(Base):
    def test_markdown_is_sanitised(self):
        a = self.admin()
        evil = ('<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n'
                '[click](javascript:alert(3))\n\n[tab](jav\tascript:alert(4))\n\n![i](javascript:alert(5))\n\n'
                '<div onclick="x()">hi</div>')
        self.new_post(a, title="Evil", body=evil)
        html = self.client().get("/post/evil").data.decode()
        body = html.split('class="prose"')[1].split("</article>")[0]
        self.assertNotIn("<script", body)
        self.assertNotIn("<img src=x", body)
        self.assertNotIn("<div onclick", body)
        self.assertNotIn("javascript:", body.replace("\t", ""))
        self.assertIn("&lt;script&gt;", body)

    def test_markdown_still_works(self):
        a = self.admin()
        self.new_post(a, title="MD", body="# Heading\n\n**bold** and [link](https://example.com)\n\n```\ncode\n```")
        body = self.client().get("/post/md").data.decode()
        self.assertIn("<h1>Heading</h1>", body); self.assertIn("<strong>bold</strong>", body)
        self.assertIn('href="https://example.com"', body); self.assertIn("<code>", body)

    def test_comment_and_title_escaped(self):
        a = self.admin()
        self.new_post(a, title="<b>T</b>")
        u = self.register()
        slug = blog.connect().execute("SELECT slug FROM posts").fetchone()["slug"]
        self.post(u, f"/post/{slug}/comment", {"body": "<script>alert(1)</script>"})
        html = self.client().get(f"/post/{slug}").data.decode()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<b>T</b>", html)

    def test_sql_injection_in_search_and_login(self):
        a = self.admin()
        self.new_post(a)
        v = self.client()
        self.assertEqual(v.get("/search?q=' OR 1=1 --").status_code, 200)
        self.assertEqual(self.login(v, "admin' --", "x").status_code, 401)

    def test_security_headers(self):
        r = self.client().get("/")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_admin_pages_forbidden_for_normal_user(self):
        u = self.register()
        for url in ("/admin/settings", "/admin/users"):
            self.assertEqual(u.get(url).status_code, 403)
        self.assertEqual(self.post(u, "/admin/users/1/delete").status_code, 403)

    def test_deactivated_user_locked_out(self):
        a = self.admin()
        u = self.make_user(a, "eve")
        uid = blog.connect().execute("SELECT id FROM users WHERE username='eve'").fetchone()["id"]
        self.post(a, f"/admin/users/{uid}/toggle_active")
        self.assertIn(b"Log in", u.get("/").data)        # existing session no longer valid
        self.assertEqual(self.login(self.client(), "eve", "password123").status_code, 401)

    def test_admin_cannot_lock_self_out(self):
        a = self.admin()
        self.post(a, "/admin/users/1/toggle_admin")
        self.post(a, "/admin/users/1/delete")
        self.assertEqual(blog.connect().execute("SELECT role FROM users WHERE id=1").fetchone()["role"], "admin")


class CustomisationTests(Base):
    def form(self, **over):
        d = {"site_name": "Cool Site", "tagline": "A tagline", "banner_text": "Welcome friends",
             "posts_per_page": "5", "allow_registration": "1", "color_primary": "#ff0000",
             "color_bg": "#101010", "color_surface": "#202020", "color_text": "#eeeeee",
             "color_header_text": "#000000"}
        d.update(over)
        return d

    def test_site_name_banner_colors(self):
        a = self.admin()
        r = self.post(a, "/admin/settings", self.form())
        self.assertEqual(r.status_code, 302)
        html = self.client().get("/").data.decode()
        self.assertIn("Cool Site", html); self.assertIn("A tagline", html)
        self.assertIn("Welcome friends", html); self.assertIn("--primary:#ff0000;", html)
        self.assertIn("--bg:#101010;", html)

    def test_invalid_settings_rejected(self):
        a = self.admin()
        self.assertEqual(self.post(a, "/admin/settings", self.form(color_primary="red;}</style><script>")).status_code, 400)
        self.assertEqual(self.post(a, "/admin/settings", self.form(site_name="")).status_code, 400)
        self.assertEqual(self.post(a, "/admin/settings", self.form(posts_per_page="0")).status_code, 400)
        self.assertNotIn(b"Cool Site", self.client().get("/").data)

    def test_banner_upload_replace_remove(self):
        a = self.admin()
        r = self.post(a, "/admin/settings", {**self.form(), "banner_image": (io.BytesIO(PNG), "b.png")},
                      content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        m = re.search(r'/uploads/([0-9a-f]{16}\.png)', self.client().get("/").data.decode())
        self.assertTrue(m)
        name = m.group(1)
        self.assertEqual(self.client().get(f"/uploads/{name}").status_code, 200)
        self.assertTrue((blog.app.config["UPLOAD_DIR"] / name).exists())
        self.post(a, "/admin/settings", {**self.form(), "remove_banner": "1"})
        self.assertFalse((blog.app.config["UPLOAD_DIR"] / name).exists())
        self.assertNotIn(b"/uploads/", self.client().get("/").data)

    def test_non_image_upload_rejected(self):
        a = self.admin()
        r = self.post(a, "/admin/settings", {**self.form(), "banner_image": (io.BytesIO(b"<svg onload=alert(1)>"), "x.png")},
                      content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client().get("/uploads/../blog.db").status_code, 404)

    def test_user_management_flow(self):
        a = self.admin()
        self.post(a, "/admin/users", {"username": "frank", "password": "password123", "role": "user"})
        f = self.client()
        self.assertEqual(self.login(f, "frank", "password123").status_code, 302)
        uid = blog.connect().execute("SELECT id FROM users WHERE username='frank'").fetchone()["id"]
        self.post(a, f"/admin/users/{uid}/reset_password", {"password": "brandnewpw1"})
        self.assertEqual(self.login(self.client(), "frank", "brandnewpw1").status_code, 302)
        self.post(a, f"/admin/users/{uid}/toggle_admin")
        self.assertEqual(f.get("/admin/users").status_code, 200)
        self.post(f, "/post/x/comment", {"body": "x"})
        self.post(a, f"/admin/users/{uid}/delete")
        self.assertIsNone(blog.connect().execute("SELECT 1 FROM users WHERE username='frank'").fetchone())

    def test_deleting_user_keeps_posts_and_comments(self):
        a = self.admin()
        bob = self.make_user(a, "bob", can_post=True)
        self.new_post(bob, title="Bobs Post")
        self.post(bob, "/post/bobs-post/comment", {"body": "my comment"})
        uid = blog.connect().execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
        self.post(a, f"/admin/users/{uid}/delete")
        html = self.client().get("/post/bobs-post").data
        self.assertIn(b"my comment", html); self.assertIn(b"[deleted user]", html)


class PersistenceTests(Base):
    def test_data_survives_restart(self):
        a = self.admin()
        self.new_post(a, title="Persistent", tags="keep")
        self.register("zed")
        self.post(a, "/admin/settings", CustomisationTests.form(self, site_name="Persisted Site"))
        key1 = blog.app.config["SECRET_KEY"]
        # simulate container restart: fresh configure + init_db on the same data dir
        blog.configure(blog.app, self.tmp.name)
        blog.init_db()
        self.assertEqual(blog.app.config["SECRET_KEY"], key1)           # sessions stay valid
        v = self.client()
        self.assertIn(b"Persistent", v.get("/").data)
        self.assertIn(b"Persisted Site", v.get("/").data)
        self.assertEqual(self.login(v, "zed", "password123").status_code, 302)
        self.assertEqual(blog.connect().execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)  # no 2nd admin
        self.assertEqual(self.client().get("/healthz").data, b"ok")


if __name__ == "__main__":
    unittest.main()
