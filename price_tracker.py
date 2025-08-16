import os
import re
import csv
import time
import json
import random
import argparse
import urllib.parse as up
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

DEFAULT_SELECTORS = {
    "generic": {
        "title": {"css": "meta[property='og:title'], h1", "attr": None},
        "price": {
            "css": "[itemprop='price'], meta[property='product:price:amount'], .price, .product-price, [data-price]",
            "attr": "content"
        },
        "currency": {"css": "meta[property='product:price:currency'], [itemprop='priceCurrency']", "attr": "content"}
    },
    "www.fnac.com": {
        "title": {"css": "h1", "attr": None},
        "price": {"css": "[data-test='price'] .f-priceBox-price, .f-priceBox-price", "attr": None},
        "currency": {"css": "", "attr": None}
    },
    "www.cdiscount.com": {
        "title": {"css": "h1", "attr": None},
        "price": {"css": ".fpPrice.price, .jsMainPrice", "attr": None},
        "currency": {"css": "", "attr": None}
    },
}

CURRENCY_SIGNS = {"€": "EUR", "$": "USD", "£": "GBP", "CHF": "CHF"}

def get_domain(url: str) -> str:
    return up.urlparse(url).netloc.lower()

def load_config(path: str | None):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            custom = json.load(f)
        cfg = DEFAULT_SELECTORS.copy()
        cfg.update(custom)
        return cfg
    return DEFAULT_SELECTORS

def fetch(url: str, timeout=25) -> str:
    headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "fr,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    if r.status_code in (429, 503):
        time.sleep(2 + random.random())
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text

def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")

def sel_one(soup: BeautifulSoup, css: str, attr: str | None):
    if not css:
        return None
    el = soup.select_one(css)
    if not el:
        return None
    return (el.get(attr).strip() if attr else el.get_text(" ", strip=True)) or None

def parse_price(raw: str | None):
    if not raw:
        return None, None
    txt = raw.strip()
    currency = None
    for sign, code in CURRENCY_SIGNS.items():
        if sign in txt:
            currency = code
            break
    cleaned = re.sub(r"[^\d,.\-]", "", txt)
    if "," in cleaned and cleaned.rfind(",") > cleaned.rfind("."):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = "".join(parts[:-1]).replace(",", "") + "." + parts[-1]
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned), currency
    except ValueError:
        return None, currency

def extract(url: str, selectors_map: dict):
    html = fetch(url)
    soup = make_soup(html)
    domain = get_domain(url)
    rules = selectors_map.get(domain) or selectors_map.get("generic")
    title = sel_one(soup, rules["title"]["css"], rules["title"]["attr"])
    price_raw = sel_one(soup, rules["price"]["css"], rules["price"]["attr"])
    curr_from_rule = None
    if "currency" in rules and rules["currency"]["css"]:
        curr_from_rule = sel_one(soup, rules["currency"]["css"], rules["currency"]["attr"])
    price, currency = parse_price(price_raw)
    if not currency and curr_from_rule:
        currency = curr_from_rule.strip().upper()
    return {"url": url, "domain": domain, "title": title or "", "price": price, "currency": currency}

def save_history(row: dict, out_csv: Path):
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    exists = out_csv.exists()
    with open(out_csv, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "domain", "url", "title", "price", "currency"])
        w.writerow([
            datetime.utcnow().isoformat(timespec="seconds"),
            row["domain"],
            row["url"],
            row["title"],
            row["price"] if row["price"] is not None else "",
            row["currency"] or ""
        ])

def last_price_for(url: str, out_csv: Path):
    if not out_csv.exists():
        return None
    last = None
    with open(out_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["url"] == url and r["price"]:
                try:
                    last = float(r["price"])
                except:
                    pass
    return last

def send_discord(webhook: str, title: str, desc: str, color=3066993):
    if not webhook:
        return
    try:
        requests.post(webhook, json={
            "username": "Price Tracker",
            "embeds": [{
                "title": title,
                "description": desc,
                "color": color,
                "timestamp": datetime.utcnow().isoformat()
            }]
        }, timeout=15)
    except Exception:
        pass

# ---------- Auto-setup helpers ----------
def ensure_urls_file(urls_path: Path):
    if urls_path.exists():
        return
    urls_path.write_text(
        "# Mets 1 URL produit par ligne. Lignes commençant par # ignorées.\n"
        "# Exemple:\n"
        "# https://www.fnac.com/a1234567/mon-produit\n"
        "# https://www.cdiscount.com/some/product/page.html\n",
        encoding="utf-8"
    )
    print(f"🆕 Fichier créé: {urls_path}")
    first = input("➕ Ajoute une première URL maintenant (ou laisse vide): ").strip()
    if first:
        with urls_path.open("a", encoding="utf-8") as f:
            f.write(first + "\n")
        print("✅ URL ajoutée.")

def ensure_webhook_file(webhook_file: Path, cli_webhook: str) -> str:
    # priorité à l’argument CLI
    if cli_webhook:
        webhook_file.write_text(cli_webhook.strip(), encoding="utf-8")
        return cli_webhook.strip()
    # fichier existant ?
    if webhook_file.exists():
        content = webhook_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    # demander et créer
    print("🔔 (Optionnel) Webhook Discord pour alertes baisse de prix.")
    wh = input("→ Colle ton webhook (ou laisse vide pour désactiver): ").strip()
    webhook_file.write_text(wh, encoding="utf-8")
    if wh:
        print(f"✅ Webhook sauvegardé dans {webhook_file}")
    else:
        print("ℹ️ Pas de webhook: alertes désactivées.")
    return wh

def main():
    ap = argparse.ArgumentParser(description="Price tracker (auto-setup fichiers + webhook)")
    ap.add_argument("--urls", default="urls.txt", help="Fichier avec 1 URL par ligne (créé auto si absent)")
    ap.add_argument("--config", default=None, help="JSON sélecteurs par domaine (optionnel)")
    ap.add_argument("--outdir", default="data", help="Dossier de sortie (history.csv dedans)")
    ap.add_argument("--webhook", default="", help="Webhook Discord (sinon on te le demande et on crée webhook.txt)")
    ap.add_argument("--interval", type=int, default=0, help="Répéter toutes N minutes (0 = une passe)")
    ap.add_argument("--no_prompt", action="store_true", help="Ne pas poser de questions (CI)")
    args = ap.parse_args()

    # chemins
    urls_path = Path(args.urls)
    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "history.csv"
    webhook_file = Path("webhook.txt")  # à la racine du projet

    # auto-création urls.txt
    if not args.no_prompt:
        ensure_urls_file(urls_path)
    else:
        urls_path.touch(exist_ok=True)

    # webhook (demande 1x puis stocke)
    webhook = ensure_webhook_file(webhook_file, args.webhook) if not args.no_prompt else (args.webhook or (webhook_file.read_text(encoding="utf-8").strip() if webhook_file.exists() else ""))

    selectors_map = load_config(args.config)

    def run_once():
        ok = 0
        lines = urls_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            print(f"⚠️ {urls_path} est vide. Ajoute des URLs.")
            return ok
        for line in lines:
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            try:
                data = extract(url, selectors_map)
                previous_price = last_price_for(url, out_csv)  # avant d'écrire
                save_history(data, out_csv)
                msg = f"{data['title'][:80]} — {data['price']} {data['currency'] or ''}".strip()
                print(f"✅ {data['domain']}: {msg}")
                ok += 1
                # alerte baisse
                if webhook and previous_price is not None and data["price"] is not None and data["price"] < previous_price:
                    delta = round(previous_price - data["price"], 2)
                    send_discord(
                        webhook,
                        "📉 Baisse de prix détectée",
                        f"[{data['title']}]({data['url']})\nAvant: **{previous_price}** → Maintenant: **{data['price']}** ({data['currency'] or ''})\nDiff: **-{delta}**"
                    )
            except Exception as e:
                print(f"⚠️ {url} -> {e}")
        return ok

    if args.interval <= 0:
        run_once()
    else:
        print(f"⏱️ Monitoring chaque {args.interval} min. CTRL+C pour quitter.")
        try:
            while True:
                run_once()
                time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\n👋 Stop.")

if __name__ == "__main__":
    main()
