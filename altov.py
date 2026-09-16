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
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        msg = wrap.find("div", class_="tgme_widget_message")
        if not msg or not msg.get("data-post"):
            continue
        pid = msg["data-post"]
        tnode = msg.find("div", class_="tgme_widget_message_text")
        if not tnode:
            continue
        for br in tnode.find_all("br"):
            br.replace_with("\n")
        text = tnode.get_text()
        photo = ""
        for el in wrap.find_all(attrs={"style": re.compile(r"url\(")}):
            cls = " ".join(el.get("class") or [])
            if "avatar" in cls or "emoji" in cls:
                continue
            m = re.search(r"url\(['\"]?([^'\")]+)", el["style"])
            if m:
                photo = m.group(1)
                break
        if not photo:
            for img in wrap.find_all("img"):
                src = img.get("src") or ""
                cls = " ".join(img.get("class") or [])
                if src and "/img/emoji/" not in src and "emoji" not in cls:
                    photo = src
                    break
        if photo.startswith("//"):
            photo = "https:" + photo
        posts.append({"id": pid, "text": text, "photo": photo})
    for i, p in enumerate(posts):
        if not p["photo"] and i > 0 and posts[i - 1]["photo"] and not posts[i - 1]["text"]:
            p["photo"] = posts[i - 1]["photo"]
    return posts

def send(photo, text):
    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    if photo:
        if len(text) <= 1024:
            r = SESS.post(api + "/sendPhoto", data={"chat_id": TG_CHAT_ID, "caption": text},
                          files={"photo": (None, photo)}, timeout=60)
            if r.ok:
                return True
            log.warning("Фото ссылкой не ушло: %s", r.text[:200])
        try:
            img = SESS.get(photo, timeout=30)
            img.raise_for_status()
            cap = text if len(text) <= 1024 else text[:1020] + "..."
            r = SESS.post(api + "/sendPhoto", data={"chat_id": TG_CHAT_ID, "caption": cap},
                          files={"photo": ("chart.jpg", img.content)}, timeout=60)
            if r.ok:
                return True
            log.warning("Фото файлом не ушло: %s", r.text[:200])
        except Exception as ex:
            log.warning("Фото не скачалось: %s", ex)
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
            log.info("Обзор опубликован (%d симв., фото=%s)", len(text), bool(p["photo"]))

if __name__ == "__main__":
    seen = load_seen()
    log.info("Старт altov-bot. Проверка каждые %d сек.", CHECK_INTERVAL)
    while True:
        process(seen, first_run=not seen)
        time.sleep(CHECK_INTERVAL)
