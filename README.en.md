# WP Synchro

> Incremental WordPress-to-WordPress sync with a local web UI.  
> Transfers files and database content from an old server to a new one — safely, with full control.

Built by [Mediatoring.com](https://mediatoring.cz) · [Česky / Czech version](README.md)

---

## What it does

WP Synchro keeps two WordPress installations in sync — useful for migrations, staging-to-production workflows, or ongoing content mirroring.

| Feature | Details |
|---|---|
| **Motor A — Files** | Syncs `wp-content/uploads` and custom dirs via local machine as relay (old → local → new) |
| **Motor B — Content** | Syncs posts, pages, CPTs via WP-CLI; preserves IDs and Polylang language data |
| **Delta preview** | Shows exactly what will change before you confirm |
| **Live progress** | Background jobs with real-time log streaming in the browser |
| **Read-only source** | Old server is never written to — enforced at code level |
| **Multi-profile** | One tool, multiple sites — each site gets its own YAML config |

## Requirements

- Python 3.11+
- `rsync` and `ssh` in PATH
- SSH key-based access to both servers (no password prompts)
- WP-CLI available on both servers in the SSH shell (`wp --info` must work)
- Local machine must be able to reach both servers via SSH

## Installation

```bash
git clone https://github.com/mediatoring/wp-synchro.git
cd wp-synchro
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Setup

1. Copy the example config:

```bash
cp configs/example.yaml configs/mysite.yaml
```

2. Edit `configs/mysite.yaml` — fill in SSH hosts, paths, and WP-CLI location:

```yaml
old_server:
  ssh_host: "user@old-server.example.com"
  ssh_key: "~/.ssh/keyfile"        # optional, omit to use ssh-agent
  wp_root: "/path/to/wordpress"
  php_binary: "/usr/bin/php"
  wpcli_path: "/path/to/wp.phar"   # or "wp" if wp-cli is in PATH
  table_prefix: "wp_"

new_server:
  ssh_host: "user@new-server.example.com"
  ssh_key: "~/.ssh/keyfile"        # optional
  wp_root: "/path/to/wordpress"
  wpcli_binary: "wp"

sync_dirs:
  - source: "wp-content/uploads"
    dest: "wp-content/uploads"
```

3. Verify SSH access and WP-CLI availability:

```bash
ssh user@old-server.example.com "echo ok"
ssh user@new-server.example.com "wp --info --skip-themes"
```

## Run

```bash
WP_SYNCHRO_CONFIG=configs/mysite.yaml python run.py --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

## How it works

### File sync (Motor A)

1. `find` lists all files on both servers with size + mtime (~4 s even for 25 k files)
2. Delta is computed locally
3. On confirm: rsync downloads only the changed files from the old server to a local staging dir, then uploads them to the new server
4. The old server is never written to

### Content sync (Motor B)

1. WP-CLI `post list` on both servers, compare `post_modified`
2. UI shows new / modified / deleted posts per post type
3. On confirm: posts created or updated on new server via WP-CLI
4. Polylang language assignments and translation groups are preserved

### Safety

- Old server SSH wrapper has a read-only allowlist — any write attempt raises `SecurityError`
- Deletions require a DB backup to be taken first
- Mirror deletion (sync deletes) is opt-in with a second confirmation

## State & logs

SQLite database at `~/.wp-synchro/<profile>/state.db`. Browse in the **Logs** tab or directly:

```bash
sqlite3 ~/.wp-synchro/mysite/state.db "SELECT * FROM jobs ORDER BY id DESC LIMIT 20;"
```

## License

MIT

---

*WP Synchro is developed and maintained by [Mediatoring.com s.r.o.](https://mediatoring.cz)*
