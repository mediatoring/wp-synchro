# WP Synchro

> Přírůstkový WordPress-to-WordPress sync s lokálním webovým rozhraním.  
> Přenáší soubory a obsah databáze ze starého serveru na nový — bezpečně a s plnou kontrolou.

Vytvořil [Mediatoring.com](https://mediatoring.cz) · [English version](README.md)

---

## Co to dělá

WP Synchro udržuje dvě WordPress instalace synchronizované — hodí se pro migrace, staging-to-production workflow nebo průběžné zrcadlení obsahu.

| Funkce | Popis |
|---|---|
| **Motor A — Soubory** | Synchronizuje `wp-content/uploads` a další adresáře přes Mac jako mezičlánek (starý → Mac → nový) |
| **Motor B — Obsah** | Synchronizuje posty, stránky a CPT přes WP-CLI; zachovává ID a jazyková přiřazení Polylangu |
| **Delta preview** | Ukáže přesně co se změní, než potvrdíte |
| **Live progress** | Úlohy na pozadí s real-time streamováním logů v prohlížeči |
| **Starý server jen ke čtení** | Na starý server se nikdy nezapisuje — vynuceno na úrovni kódu |
| **Multi-profil** | Jeden nástroj, více webů — každý web má vlastní YAML config |

## Požadavky

- Python 3.11+
- `rsync` a `ssh` v PATH na Macu
- SSH přístup na oba servery pomocí klíčů (bez hesel)
- WP-CLI dostupné na obou serverech
- Mac musí být schopen připojit se na oba servery přes SSH

## Instalace

```bash
git clone https://github.com/mediatoring/wp-synchro.git
cd wp-synchro
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Nastavení

1. Zkopíruj ukázkový config:

```bash
cp configs/example.yaml configs/mojeweb.yaml
```

2. Uprav `configs/mojeweb.yaml` — vyplň SSH hosty, cesty a umístění WP-CLI:

```yaml
old_server:
  ssh_host: "uzivatel@stary-server.example.com"
  ssh_key: "~/.ssh/id_rsa_staryserver"   # vynech pro použití ssh-agent
  wp_root: "/var/www/html/wordpress"
  php_binary: "/usr/bin/php"
  wpcli_path: "~/wp.phar"
  table_prefix: "wp_"

new_server:
  ssh_host: "uzivatel@novy-server.example.com"
  ssh_key: "~/.ssh/id_ed25519_novyserver"
  wp_root: "~/public_html"
  wpcli_binary: "wp"

sync_dirs:
  - source: "wp-content/uploads"
    dest: "wp-content/uploads"
```

3. Ověř, že SSH funguje bez hesla:

```bash
ssh -i ~/.ssh/id_rsa_staryserver uzivatel@stary-server.example.com "echo ok"
ssh -i ~/.ssh/id_ed25519_novyserver uzivatel@novy-server.example.com "wp --info --skip-themes"
```

## Spuštění

```bash
WP_SYNCHRO_CONFIG=configs/mojeweb.yaml python run.py --port 8765
```

Otevři [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Jak to funguje

### Sync souborů (Motor A)

1. `find` vypíše soubory na obou serverech s velikostí a mtime (~4 s i pro 25 tis. souborů)
2. Delta se spočítá lokálně — žádný rsync dry-run přes pomalé VPN
3. Po potvrzení: rsync stáhne jen změněné soubory starý → Mac staging, pak je nahraje na nový server
4. Na starý server se nikdy nezapisuje

### Sync obsahu (Motor B)

1. WP-CLI `post list` na obou serverech, porovnání `post_modified`
2. UI ukáže nové / změněné / smazané posty podle typu
3. Po potvrzení: posty vytvoří nebo aktualizuje na novém serveru přes WP-CLI
4. Jazyková přiřazení Polylangu a skupiny překladů jsou zachovány

### Bezpečnost

- SSH wrapper starého serveru má allowlist čtecích příkazů — jakýkoli pokus o zápis vyhodí `SecurityError`
- Mazání vyžaduje předchozí zálohu DB
- Zrcadlové mazání (sync deletes) je opt-in s druhým potvrzením

## Stav a logy

SQLite databáze na `~/.wp-synchro/<profil>/state.db`. Procházet v záložce **Logy** nebo přímo:

```bash
sqlite3 ~/.wp-synchro/mojeweb/state.db "SELECT * FROM jobs ORDER BY id DESC LIMIT 20;"
```

## Licence

MIT

---

*WP Synchro vytvořil a spravuje [Mediatoring.com s.r.o.](https://mediatoring.cz)*
