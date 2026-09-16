import json, re, time, logging, os
import requests
import feedparser
from bs4 import BeautifulSoup

# ============ НАСТРОЙКИ ============
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = "gpt-4o-mini"
# ===================================

RSS_URL = "https://forklog.com/feed"
CHECK_INTERVAL = 600
DELAY_BETWEEN_POSTS = 7
POST_MAX_LEN = 340
ADD_SOURCE_LINK = False
SEEN_FILE = "seen.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("forklog-bot")
SESS = requests.Session()
SESS.headers.update(HEADERS)

STOP_MARKERS = ("Подписывайтесь на ForkLog", "Нашли ошибку", "Материалы по теме", "Подписывайтесь на")


def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    img = ""
    tag = soup.find("img", src=re.compile(r"uploads")) or soup.find("img")
    if tag and tag.get("src"):
        img = tag["src"]
    text = soup.get_text(" ", strip=True)
    for m in STOP_MARKERS:
        pos = text.find(m)
        if pos > 200:
            text = text[:pos]
    return text, img


def fetch_feed():
    items = []
    feed = feedparser.parse(RSS_URL)
    for e in feed.entries:
        url = e.get("link", "")
        if "/news/" not in url:
            continue
        raw = ""
        if e.get("content"):
            raw = e["content"][0].get("value", "")
        if not raw:
            raw = e.get("description", "")
        text, img = clean_html(raw) if raw else ("", "")
        if not text:
            text = e.get("summary", "") or e.get("title", "")
        items.append({"url": url, "title": e.get("title", ""), "ts": e.get("published_parsed"),
                      "text": text, "image": img})
    if items and items[0].get("ts"):
        items.sort(key=lambda i: i["ts"])
    return items


def jina_article(url):
    try:
        r = SESS.get("https://r.jina.ai/" + url, timeout=60)
        if not r.ok:
            return None
        md = r.text
        image = ""
        imgs = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", md)
        image = next((i for i in imgs if "uploads" in i), imgs[0] if imgs else "")
        md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
        lines = [ln.strip() for ln in md.splitlines()]
        paras = [ln for ln in lines if len(ln) > 50 and not ln.startswith(("#", "!", "|"))]
        text = " ".join(paras)
        for m in STOP_MARKERS:
            pos = text.find(m)
            if pos > 200:
                text = text[:pos]
        return {"text": text, "image": image}
    except Exception as ex:
        log.warning("Читалка не удалась: %s", ex)
        return None


SYSTEM_PROMPT = ("Ты - редактор новостного Telegram-канала о криптовалютах. По тексту статьи напиши ОДИН пост на русском. "
                 "Требования: 280-320 символов; суть и ключевые факты/цифры; нейтральный новостной стиль; без эмодзи, "
                 "хэштегов и заголовков; не выдумывай факты. Верни только текст поста.")

def _clamp(text):
    text = re.sub(r"\s+", " ", text).strip().strip('"«»')
    if len(text) <= POST_MAX_LEN:
        return text
    cut = text[:POST_MAX_LEN]
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos > 180:
            return cut[:pos + 1].strip()
    return cut[:cut.rfind(" ")].rstrip(" ,;") + "..."

def make_post(art):
    if OPENAI_API_KEY and art["text"]:
        try:
            resp = SESS.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                             json={"model": OPENAI_MODEL, "temperature": 0.4, "max_tokens": 250,
                                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                {"role": "user", "content": f"Заголовок: {art['title']}\nТекст: {art['text'][:12000]}"}]},
                             timeout=60)
            resp.raise_for_status()
            out = resp.json()["choices"][0]["message"]["content"].strip()
            if 120 <= len(out) <= 500:
                return _clamp(out)
        except Exception as ex:
            log.warning("LLM не ответила, резервный режим: %s", ex)
    src = art["text"] or art["title"]
    sentences = re.split(r"(?<=[.!?])\s+", src)
    post = ""
    for s in sentences:
        if len(post) + len(s) + 1 > 300:
            break
        post = (post + " " + s).strip()
    return _clamp(post or src)


def send_telegram(image_url, caption):
    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    if image_url:
        r = SESS.post(api + "/sendPhoto", data={"chat_id": TG_CHAT_ID, "caption": caption},
                      files={"photo": (None, image_url)}, timeout=60)
        if r.ok:
            return True
        try:
            img = SESS.get(image_url, timeout=30)
            img.raise_for_status()
            r = SESS.post(api + "/sendPhoto", data={"chat_id": TG_CHAT_ID, "caption": caption},
                          files={"photo": ("image.jpg", img.content)}, timeout=60)
            if r.ok:
                return True
        except Exception as ex:
            log.warning("Картинка не отправилась: %s", ex)
    r = SESS.post(api + "/sendMessage", data={"chat_id": TG_CHAT_ID, "text": caption}, timeout=60)
    if not r.ok:
        log.error("Telegram: %s", r.text)
    return r.ok


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)


def process(seen, first_run):
    items = fetch_feed()
    if not items:
        log.error("RSS недоступен, попробую позже")
        return
    if first_run:
        fresh = [items[-1]]
        seen.update(i["url"] for i in items if i["url"] != fresh[0]["url"])
        save_seen(seen)
    else:
        fresh = [i for i in items if i["url"] not in seen]
    for item in fresh:
        try:
            log.info("Обрабатываю: %s", item["url"])
            text, image = item["text"], item["image"]
            if len(text) < 600:
                extra = jina_article(item["url"])
                if extra and len(extra["text"]) > len(text):
                    text = extra["text"]
                    image = image or extra["image"]
            art = {"title": item["title"], "image": image, "text": text}
            post = make_post(art)
            caption = post + (f"\n\n{item['url']}" if ADD_SOURCE_LINK else "")
            if send_telegram(art["image"], caption):
                seen.add(item["url"])
                save_seen(seen)
                log.info("Опубликовано (%d симв.): %s", len(post), art["title"][:60])
            time.sleep(DELAY_BETWEEN_POSTS)
        except Exception as ex:
            log.error("Ошибка на %s: %s", item["url"], ex)


if __name__ == "__main__":
    seen = load_seen()
    log.info("Старт. Проверка каждые %d сек.", CHECK_INTERVAL)
    while True:
        process(seen, first_run=not seen)
        time.sleep(CHECK_INTERVAL)
