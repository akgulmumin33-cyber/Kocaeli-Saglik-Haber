#!/usr/bin/env python3
"""
Kocaeli hastane haberleri toplayıcısı.

- sources.json'daki kaynaklardan (Google Haberler aramaları, RSS, resmi haber sayfaları) haberleri çeker
- Kocaeli + sağlık/hastane ile ilgili olanları süzer, hastaneye göre etiketler
- docs/news.json dosyasını günceller (uygulama bu dosyayı okur)
- Yeni haber varsa Firebase Cloud Messaging ile "kocaeli-saglik" konusuna bildirim gönderir

GitHub Actions bu betiği her 10 dakikada bir çalıştırır (.github/workflows/haberleri-guncelle.yml).
Elle çalıştırmak için:  python fetch_news.py
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "docs" / "news.json"
SOURCES_FILE = ROOT / "sources.json"
HOSPITALS_FILE = ROOT / "hospitals.json"

PUSH_TOPIC = "kocaeli-saglik"
MAX_ITEMS = 400          # news.json'da tutulacak en fazla haber
MAX_AGE_DAYS = 45        # bundan eski haberler silinir
MAX_PUSH_PER_RUN = 3     # bir çalıştırmada en fazla bu kadar ayrı bildirim; fazlası tek özet bildirimde
SUMMARY_MAX_CHARS = 600
HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; KocaeliSaglikHaber/1.0)"

DISTRICTS = [
    "kocaeli", "izmit", "gebze", "darica", "golcuk", "korfez", "derince", "karamursel",
    "kandira", "kartepe", "basiskele", "cayirova", "dilovasi",
]
HEALTH_WORDS = [
    "hastane", "saglik", "hekim", "doktor", "hemsire", "acil servis", "ambulans", "112",
    "poliklinik", "ameliyat", "yogun bakim", "hasta", "tedavi", "ilac", "asilama",
]

_TR_MAP = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"})


def fold(text: str) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (eşleştirme için)."""
    text = text.replace("I", "ı").replace("İ", "i").lower()
    return text.translate(_TR_MAP)


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def make_id(link: str) -> str:
    return hashlib.sha1(link.strip().encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(title))[:80]


def http_get(url: str) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- kaynaklar

def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)  # feedparser saatleri UTC verir
    return None


def entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url")
            if url:
                return url
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    match = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    return match.group(1) if match else None


def from_google_news(query: str) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}&hl=tr&gl=TR&ceid=TR:tr"
    feed = feedparser.parse(http_get(url).content)
    items = []
    for e in feed.entries:
        source = (e.get("source") or {}).get("title") or ""
        title = clean_html(e.get("title", ""))
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        items.append({
            "title": title,
            "summary": None,  # Google Haberler özet vermiyor; metin kaynakta okunur
            "link": e.get("link", ""),
            "source": source or "Google Haberler",
            "published": entry_datetime(e),
            "image": None,
            "trusted": False,
        })
    return items


def from_rss(src: dict) -> list[dict]:
    feed = feedparser.parse(http_get(src["url"]).content)
    items = []
    for e in feed.entries:
        summary = truncate(clean_html(e.get("summary", "")), SUMMARY_MAX_CHARS)
        items.append({
            "title": clean_html(e.get("title", "")),
            "summary": summary or None,
            "link": e.get("link", ""),
            "source": src.get("ad") or clean_html(feed.feed.get("title", "")) or "RSS",
            "published": entry_datetime(e),
            "image": entry_image(e),
            "trusted": bool(src.get("guvenilir")),
        })
    return items


def from_html_list(src: dict) -> list[dict]:
    """RSS'i olmayan resmi sitelerin haber listesi sayfasından başlık + bağlantı çıkarır."""
    page = http_get(src["url"]).text
    pattern = re.compile(r'<a\b([^>]*?)href=["\']([^"\']*' + src["link_deseni"] + r')["\'][^>]*>(.*?)</a>', re.S | re.I)
    list_path = re.sub(r"^https?://[^/]+", "", src["url"])
    matches = pattern.findall(page)
    # saglik.gov.tr sitelerinde haber listesindeki bağlantıların id'si sayfa numarasıyla aynıdır
    # (<a id="395040" href="/TR-395040/...">); menü bağlantılarında bu yoktur. Varsa yalnızca onları al.
    def is_list_link(attrs: str, href: str) -> bool:
        num = re.search(r"/TR-(\d+)/", href)
        aid = re.search(r'\bid=["\'](\d+)["\']', attrs)
        return bool(num and aid and num.group(1) == aid.group(1))
    listed = [(h, i) for a, h, i in matches if is_list_link(a, h)]
    candidates = listed or [(h, i) for _, h, i in matches]
    items, seen = [], set()
    for href, inner in candidates:
        link = urljoin(src["url"], href)
        title = clean_html(inner).replace("\n", " ")
        if not title:
            title_attr = re.search(r'title=["\']([^"\']+)', inner)
            title = html.unescape(title_attr.group(1)) if title_attr else ""
        if len(title) < 20 or link in seen or link.endswith(list_path):
            continue  # menü / sayfalama bağlantılarını atla
        seen.add(link)
        items.append({
            "title": title,
            "summary": None,
            "link": link,
            "source": src["ad"],
            "published": None,  # liste sayfasında tarih yok; ilk görüldüğü an kullanılır
            "image": None,
            "trusted": bool(src.get("guvenilir")),
        })
    return items


def collect(sources: dict) -> list[dict]:
    jobs = [(f"Google Haberler: {q}", from_google_news, q) for q in sources.get("google_news", [])]
    jobs += [(f"RSS: {s.get('ad', s['url'])}", from_rss, s) for s in sources.get("rss", [])]
    jobs += [(f"Sayfa: {s.get('ad', s['url'])}", from_html_list, s) for s in sources.get("html_list", [])]
    collected = []
    for label, func, arg in jobs:
        try:
            got = func(arg)
            print(f"  ✓ {label}: {len(got)} haber")
            collected.extend(got)
        except Exception as exc:  # bir kaynak bozuksa diğerleri çalışmaya devam etsin
            print(f"  ✗ {label}: {exc}", file=sys.stderr)
    return collected


# --------------------------------------------------------------------------- süzme / etiketleme

def load_hospitals() -> list[tuple[str, list[str]]]:
    data = json.loads(HOSPITALS_FILE.read_text(encoding="utf-8"))
    return [(h["ad"], [fold(k) for k in h["anahtarlar"]]) for h in data["hastaneler"]]


def tag_hospitals(text_folded: str, hospitals) -> list[str]:
    return [name for name, keys in hospitals if any(k in text_folded for k in keys)]


def is_relevant(item: dict, text_folded: str, tags: list[str]) -> bool:
    if item["trusted"]:
        return True
    padded = f" {text_folded} "
    in_kocaeli = bool(tags) or any(re.search(rf"\b{d}", text_folded) for d in DISTRICTS)
    about_health = any(w in padded for w in HEALTH_WORDS)
    return in_kocaeli and about_health


# --------------------------------------------------------------------------- bildirim
#
# Bildirimler Firebase Cloud Messaging ile "kocaeli-saglik" konusuna gönderilir.
# - iPhone/iPad web uygulaması (ana ekrana eklenmiş) bildirime izin verince cihaz anahtarını
#   Firestore'daki "web_tokens" koleksiyonuna yazar; bu betik onları konuya abone eder.
# - (İsteğe bağlı) yerel iOS uygulaması kendisi doğrudan konuya abone olur.

WEB_TOKENS_COLLECTION = "web_tokens"
PUSH_QUEUE_FILE = ROOT / ".push-queue.json"   # depoya kaydedilmez; aynı iş akışı içinde kullanılır


def site_url() -> str:
    """Web uygulamasının adresi (bildirime dokununca açılacak)."""
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")  # GitHub Actions bunu "kullanici/depo" olarak verir
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io/"
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def firebase_app():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        return None
    import firebase_admin
    from firebase_admin import credentials
    if firebase_admin._apps:
        return firebase_admin.get_app()
    return firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))


def sync_web_tokens(app) -> None:
    """Web uygulamasından gelen yeni cihaz anahtarlarını bildirim konusuna abone eder."""
    from firebase_admin import firestore, messaging
    try:
        db = firestore.client(app)
        docs = list(db.collection(WEB_TOKENS_COLLECTION).where("subscribed", "==", False).limit(1000).stream())
    except Exception as exc:
        print(f"  Firestore okunamadı (web bildirimleri için Firestore kurulu mu?): {exc}", file=sys.stderr)
        return
    if not docs:
        return
    tokens = [(d.to_dict() or {}).get("token") or d.id for d in docs]
    resp = messaging.subscribe_to_topic(tokens, PUSH_TOPIC, app=app)
    failed = {err.index for err in resp.errors}
    for i, d in enumerate(docs):
        if i in failed:
            d.reference.delete()  # geçersiz/eskimiş anahtar
        else:
            d.reference.update({"subscribed": True})
    print(f"  {resp.success_count} yeni web cihazı bildirimlere abone edildi, {resp.failure_count} geçersiz silindi.")


def build_messages(items: list[dict]) -> list[dict]:
    base = site_url()
    if len(items) <= MAX_PUSH_PER_RUN:
        return [{
            "title": it["hospitals"][0] if it["hospitals"] else "Kocaeli Hastane Haberi",
            "body": it["title"],
            "link": f"{base}#/haber/{it['id']}" if base else "",
            # Haber bilgileri bildirime de eklenir (yerel iOS uygulaması için)
            "data": {
                "newsId": it["id"], "title": it["title"], "link": it["link"],
                "source": it.get("source") or "", "published": it.get("published") or "",
                "hospitals": ",".join(it.get("hospitals") or []),
            },
        } for it in items]
    return [{
        "title": f"{len(items)} yeni hastane haberi",
        "body": truncate(items[0]["title"], 120) + " ve diğerleri",
        "link": base,
        "data": {"newsId": ""},
    }]


def send_push(items: list[dict]) -> None:
    app = firebase_app()
    if app is None:
        print("FIREBASE_SERVICE_ACCOUNT tanımlı değil; bildirim gönderilmedi.")
        return
    from firebase_admin import messaging

    sync_web_tokens(app)
    icon = (site_url() + "icons/icon-192.png") if site_url() else None
    for m in build_messages(items):
        message = messaging.Message(
            topic=PUSH_TOPIC,
            notification=messaging.Notification(title=m["title"], body=truncate(m["body"], 180)),
            data=m["data"],
            apns=messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound="default"))),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(icon=icon) if icon else None,
                fcm_options=messaging.WebpushFCMOptions(link=m["link"]) if m["link"] else None,
            ),
        )
        try:
            messaging.send(message, app=app)
            print(f"  bildirim gönderildi: {m['title']} — {m['body'][:60]}")
        except Exception as exc:
            print(f"  bildirim HATASI: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- ana akış

def collect_and_save() -> list[dict]:
    """Haberleri toplar, news.json'u günceller; bildirim gönderilecek yeni haberleri döndürür."""
    now = datetime.now(timezone.utc)
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    hospitals = load_hospitals()

    existing: list[dict] = []
    if OUT_FILE.exists():
        try:
            existing = json.loads(OUT_FILE.read_text(encoding="utf-8")).get("items", [])
        except (json.JSONDecodeError, AttributeError):
            existing = []
    first_run = not existing
    known_ids = {it["id"] for it in existing}
    known_titles = {title_key(it["title"]) for it in existing}

    print("Kaynaklar taranıyor…")
    raw_items = collect(sources)

    new_items: list[dict] = []
    for item in raw_items:
        if not item["title"] or not item["link"].startswith("http"):
            continue
        news_id = make_id(item["link"])
        tkey = title_key(item["title"])
        if news_id in known_ids or tkey in known_titles:
            continue
        text = fold(f"{item['title']} {item['summary'] or ''}")
        tags = tag_hospitals(text, hospitals)
        if not is_relevant(item, text, tags):
            continue
        published = item["published"] or now
        if published > now:
            published = now
        if now - published > timedelta(days=MAX_AGE_DAYS):
            continue
        new_items.append({
            "id": news_id,
            "title": item["title"],
            "summary": item["summary"],
            "link": item["link"],
            "source": item["source"],
            "published": iso(published),
            "image": item["image"],
            "hospitals": tags,
        })
        known_ids.add(news_id)
        known_titles.add(tkey)

    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    merged = [it for it in existing + new_items if parse_iso(it["published"]) >= cutoff]
    merged.sort(key=lambda it: it["published"], reverse=True)
    merged = merged[:MAX_ITEMS]

    if merged == existing:
        print("Yeni haber yok; dosya değiştirilmedi.")
        return []

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({"updated": iso(now), "items": merged}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"{len(new_items)} yeni haber eklendi, toplam {len(merged)}.")

    if first_run:
        print("İlk çalıştırma: eski haberler için bildirim gönderilmeyecek.")
        return []
    fresh = [it for it in new_items if now - parse_iso(it["published"]) <= timedelta(hours=6)]
    fresh.sort(key=lambda it: it["published"], reverse=True)
    return fresh


def main(argv: list[str]) -> int:
    """
    Kullanım:
      python fetch_news.py               topla + hemen bildirim gönder (elle deneme için)
      python fetch_news.py --collect     topla, bildirimleri sıraya koy (GitHub Actions 1. adım)
      python fetch_news.py --push-queue  sıradaki bildirimleri gönder  (GitHub Actions 2. adım,
                                         news.json yayına girdikten sonra)
    """
    if "--push-queue" in argv:
        queue = json.loads(PUSH_QUEUE_FILE.read_text(encoding="utf-8")) if PUSH_QUEUE_FILE.exists() else []
        if not queue:
            app = firebase_app()
            if app is not None:
                sync_web_tokens(app)  # haber olmasa da yeni cihazları abone et
            print("Gönderilecek bildirim yok.")
            return 0
        send_push(queue)
        PUSH_QUEUE_FILE.unlink(missing_ok=True)
        return 0

    fresh = collect_and_save()
    if "--collect" in argv:
        PUSH_QUEUE_FILE.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        print(f"{len(fresh)} bildirim sıraya alındı.")
        return 0
    if fresh:
        send_push(fresh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
