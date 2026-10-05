#!/usr/bin/env python3
"""Build grovae.com/links/ — an index of every HTML page published across the grovae GitHub Pages repos.

Runs in GitHub Actions (GITHUB_TOKEN) or locally:  GITHUB_TOKEN=... python3 scripts/build_links.py
Stdlib only. Writes links/index.html and links/links.json. New repos and new pages are picked up automatically;
links/config.json only holds display names, exclusions and the odd manual category.
"""
import html, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "scripts", "links_config.json")))
OWNER, SITE = CFG["owner"], CFG["site"].rstrip("/") + "/"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
API = "https://api.github.com"


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "grovae-links"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return body.decode("utf-8", "replace") if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            if e.code == 404:
                return None
            raise
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt < 3:
                time.sleep(3 * (attempt + 1)); continue
            raise


# ---------- classification ----------
TYPES = [  # first match wins; checked against "path title"
    ("Flows & wireframes", r"wirefram|\bflows?\b|/flows/|journey map|lifecycle map"),
    ("Audits", r"audit|teardown|diagnos"),
    ("Proposals & onboarding", r"proposal|pitch|scope of work|onboarding|first[- ]90[- ]days|engagement plan"),
    ("Reports & performance", r"report|performance|review|snapshot|results|tracker|attribution|analysis|insight|crm/|recap|dashboard"),
    ("Plans & calendars", r"calendar|\bplan\b|-plan|campaigns/|brief|board|roadmap|send plan|strategy"),
]
TYPE_ORDER = ["Reports & performance", "Audits", "Plans & calendars", "Flows & wireframes", "Proposals & onboarding", "Client hub", "Other"]


def classify(repo, path, title):
    key = f"{repo}/{path}"
    if key in CFG.get("type_overrides", {}):
        return CFG["type_overrides"][key]
    hay = f"{path} {title}".lower()
    if path == "index.html" and re.search(r"overview|\bhub\b|all links", hay):
        return "Client hub"
    for name, rx in TYPES:
        if re.search(rx, hay):
            return name
    if path == "index.html":
        return "Client hub"
    return "Other"


def client_name(repo):
    names = CFG.get("client_names", {})
    if repo in names:
        return names[repo]
    base = re.sub(r"-(retention|report|reports|audit|crm|site)$", "", repo)
    base = re.sub(r"-(retention|report|audit)$", "", base)
    return " ".join(w.capitalize() for w in base.split("-"))


def page_title(src, path):
    m = re.search(r"<title[^>]*>(.*?)</title>", src, re.S | re.I)
    t = m.group(1) if m else ""
    if not t.strip():
        m = re.search(r"<h1[^>]*>(.*?)</h1>", src, re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", m.group(1)) if m else path
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def page_desc(src):
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', src, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def url_for(repo, path):
    p = path[:-10] if path.endswith("index.html") else path
    return SITE + p if repo == CFG["user_site_repo"] else f"{SITE}{repo}/{p}"


def main():
    repos = []
    page = 1
    while True:
        batch = get(f"{API}/users/{OWNER}/repos?per_page=100&page={page}&type=owner")
        if not batch:
            break
        repos += batch
        page += 1
    items = []
    for r in repos:
        name = r["name"]
        if not r.get("has_pages") or r.get("archived") or name in CFG.get("exclude_repos", []):
            continue
        branch = r["default_branch"]
        tree = get(f"{API}/repos/{OWNER}/{name}/git/trees/{branch}?recursive=1") or {}
        skip = set(CFG.get("exclude_paths", {}).get(name, []))
        for node in tree.get("tree", []):
            path = node["path"]
            if node["type"] != "blob" or not path.endswith(".html") or path in skip:
                continue
            if re.search(r"(^|/)(node_modules|_site|\.github|vendor|assets|scripts)/", path) or path.startswith("_"):
                continue
            src = get(f"https://raw.githubusercontent.com/{OWNER}/{name}/{branch}/{path}", raw=True) or ""
            if re.search(r'<meta[^>]+name=["\']grovae-links["\'][^>]+content=["\']hide', src, re.I):
                continue  # opt-out tag for a page
            commits = get(f"{API}/repos/{OWNER}/{name}/commits?path={urllib.request.quote(path)}&per_page=100") or []
            updated = commits[0]["commit"]["committer"]["date"] if commits else r["pushed_at"]
            created = commits[-1]["commit"]["committer"]["date"] if commits else updated
            title = page_title(src, path)
            items.append({
                "client": client_name(name), "repo": name, "path": path,
                "title": title, "desc": page_desc(src),
                "type": classify(name, path, title),
                "url": url_for(name, path),
                "updated": updated, "created": created,
            })
    items.sort(key=lambda x: x["updated"], reverse=True)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"built": built, "types": TYPE_ORDER, "items": items,
            "rules": [[n, rx] for n, rx in TYPES], "cfg": CFG}
    out = os.path.join(ROOT, "links")
    pw = os.environ.get("LINKS_PASSCODE")
    if not pw:
        sys.exit("LINKS_PASSCODE not set; refusing to publish the index unencrypted")
    import base64, hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = hashlib.sha256(b"grovae-links-v1").digest()[:16]  # fixed so 'remember this device' survives rebuilds
    iters = 210000
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters, 32)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    enc = json.dumps({"s": b(salt), "i": b(iv), "c": b(ct), "n": iters})
    tpl = open(os.path.join(ROOT, "scripts", "links_template.html"), encoding="utf-8").read()
    pages_hash = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16]
    html_out = tpl.replace("/*__ENC__*/null", enc) + f"\n<!-- pages:{pages_hash} -->\n"
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(html_out)
    stale = os.path.join(out, "links.json")
    if os.path.exists(stale):
        try:
            os.remove(stale)
        except OSError:
            pass
    print(f"{len(items)} pages across {len({i['repo'] for i in items})} repos")


if __name__ == "__main__":
    sys.exit(main())
