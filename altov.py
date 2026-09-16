import json, re, time, logging, os
import requests
from bs4 import BeautifulSoup

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "@cryptopulsnews")
SRC = "https://t.me/s/profaltov"
CHECK_INTERVAL = 600
SEEN_FILE = "seen_altov.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("altov-bot")
SESS = requests.Session()
SESS.headers.update(HEADERS)

CUT_MARKERS = ("торгуем на", "код для регистрации", "подписывай", "реферал", "://", "t.me/")
KEYWORDS = ("обзор", "доброе утро")

def clean_text(text):
    out = []
    for ln in text.split("\n"):
        s = ln.strip().lower()
        if not s:
            out.append("")
            continue
        if any(m in s for m in CUT_MARKERS):
            break
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()

def fetch_posts():
    r = SESS.get(SRC, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        pid = msg.get("data-post")
        if not pid:
            continue
        tnode = msg.find("div", class_="tgme_widget_message_text")
        if not tnode:
            continue
        for br in tnode.find_all("br"):
            br.replace_with("\n")
        text = tnode.get_text()
        photo = ""
        pw = msg.find("a", class_="tgme_widget_message_photo_wrap")
        if pw and pw.get("style"):
            m = re.search(r"url\('([^']+)'\)", pw["style"])
            if m:
                photo = m.group(1)
        posts.append({"id": pid, "text": text, "photo": photo})
    return posts

def send(photo, text):
    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    if photo and len(text) <= 1024:
        r = SESS.post(api + "/sendPhoto", data={"chat_id": TG_CHAT_ID, "caption": text},
                      files={"photo": (None, photo)}, timeout=60)
        if r.ok:
            return True
    if len(text) > 4096:
        text = text[:4090] + "..."
    r = SESS.post(api + "/sendMessage", data={"chat_id": TG_CHAT_ID, "text": text}, timeout=60)
    if not r.ok:
        log.error("Telegram: %s", r.text)
    return r.ok

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)

def process(seen, first_run):
    posts = fetch_posts()
    if not posts:
        return
    matches = [p for p in posts if any(k in p["text"].lower() for k in KEYWORDS)]
    if first_run:
        fresh = matches[-1:]
        fresh_ids = {p["id"] for p in fresh}
        seen.update(p["id"] for p in posts if p["id"] not in fresh_ids)
        save_seen(seen)
    else:
        fresh = [p for p in matches if p["id"] not in seen]
    for p in fresh:
        text = clean_text(p["text"])
        if len(text) < 50:
            continue
        if send(p["photo"], text):
            seen.add(p["id"])
            save_seen(seen)
            log.info("Обзор опубликован (%d симв.)", len(text))

if __name__ == "__main__":
    seen = load_seen()
    log.info("Старт altov-bot. Проверка каждые %d сек.", CHECK_INTERVAL)
    while True:
        process(seen, first_run=not seen)
        time.sleep(CHECK_INTERVAL)
