# MyPyPress

A small WordPress-style blog/CMS in Python (Flask + SQLite, no external services).

## Features
- **Users & roles** – administrators and regular users; open registration can be switched off.
- **Permissions**
  - Visitors (not logged in): read posts and comments only.
  - Logged-in users: comment on any published post.
  - Users an admin has enabled for posting ("Can post"): create posts and edit **their own** posts.
  - Administrators: write/edit **any** post, delete posts and comments, manage users, customize the site.
- **Posts** – Markdown (raw HTML is escaped), drafts, tags, comments, pagination.
- **Search** – every word must match the title, body or a tag (published posts only).
- **Admin customisation** – site name, tagline, banner text + banner image, theme colors, registration on/off, posts per page.

## Run locally (development)
    pip install -r requirements.txt
    python app.py                 # http://127.0.0.1:8000, data in ./data
On first start an admin account is created. Set `ADMIN_USERNAME` / `ADMIN_PASSWORD`, or a random
password is printed to the console. Change it under **Account**.

## Run tests
    python -m unittest discover -s tests -v

## Run in Docker (persistent)
    mkdir -p data                        # create it yourself so it is owned by you, not root
    echo "PUID=$(id -u)" >  .env
    echo "PGID=$(id -g)" >> .env
    echo "ADMIN_PASSWORD=choose-a-strong-one" >> .env     # optional
    docker compose up -d --build
    docker compose logs mypypress           # shows the generated admin password if you didn't set one

Open http://localhost:8000. Everything persistent is in `./data` on the host:

| File | Contents |
|---|---|
| `data/blog.db` (+ `-wal`, `-shm`) | users, posts, comments, tags, settings |
| `data/uploads/` | banner image |
| `data/secret_key` | session signing key (keep it so logins survive restarts) |

`restart: unless-stopped` brings the container back after a reboot. **Back up** by stopping the
container (`docker compose stop`) and copying `./data`. **Upgrade** with `git pull`/new files then
`docker compose up -d --build` – the schema is created with `IF NOT EXISTS`, data is untouched.

## HTTPS / reverse proxy
Run behind nginx/Caddy/Traefik for TLS and set `COOKIE_SECURE=1` and `TRUST_PROXY=1`.

## Configuration (environment variables)
`DATA_DIR` (default `./data`; `/data` in Docker), `SECRET_KEY` (optional override),
`ADMIN_USERNAME`, `ADMIN_PASSWORD` (first run only), `COOKIE_SECURE`, `TRUST_PROXY`, `PORT` (dev server).

## Design notes / limits
- Authors cannot delete posts, only admins can (see `can_delete_post()` in `app.py` to change this).
- Login throttling is in-memory per worker; use your proxy for stricter rate limiting.
- Search uses `LIKE`; fine for thousands of posts. Swap in SQLite FTS5 if you outgrow it.
- No email, password-reset-by-email, or media library: admins reset passwords from **Users**.
