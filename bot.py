#!/usr/bin/env python3
"""
SmartHomeShopperEG — on-demand Cairo grocery price search bot.
Requires Python 3.11+ and Telegram Bot API token + Serper API key.
Only searches when a user sends a message; no scheduled/daily reports.
"""
import os
import re
import time
import logging
import requests
from urllib.parse import urlparse

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "457342847").strip()
SERPER_ENDPOINT = os.getenv("SERPER_ENDPOINT", "https://google.serper.dev/search").strip()
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "30"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "6"))
REQUEST_TIMEOUT = 40

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("SmartHomeShopperEG")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def telegram(method, payload=None, timeout=45):
    response = requests.post(f"{TG_API}/{method}", json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def host_of(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""

def build_queries(user_query):
    # Cairo-wide search. No district is required. Search is triggered only by a message.
    q = clean_text(user_query)
    return [
        f'{q} سعر مصر القاهرة',
        f'{q} سعر سوبر ماركت مصر',
    ]

def serper_search(query):
    response = requests.post(
        SERPER_ENDPOINT,
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "gl": "eg", "hl": "ar", "num": 10},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 401:
        raise RuntimeError("مفتاح Serper غير صحيح أو غير مفعّل (HTTP 401).")
    if response.status_code == 429:
        raise RuntimeError("تم استهلاك رصيد البحث أو تجاوز حد الطلبات في Serper (HTTP 429).")
    response.raise_for_status()
    return response.json()

def looks_like_price(text):
    # Detect likely prices without pretending every number is a product price.
    return bool(re.search(r'(?:(?:جنيه|ج\.م|EGP|LE)\s*[\d,.]+|[\d,.]+\s*(?:جنيه|ج\.م|EGP|LE))', text, re.I))

def extract_results(payload):
    rows = []
    for item in payload.get("organic", []) or []:
        title = clean_text(item.get("title"))
        snippet = clean_text(item.get("snippet"))
        link = clean_text(item.get("link"))
        if not (title or snippet or link):
            continue
        rows.append({
            "title": title or "نتيجة بحث",
            "snippet": snippet,
            "link": link,
            "source": host_of(link),
            "has_price": looks_like_price(f"{title} {snippet}"),
        })
    # Deduplicate by URL.
    seen, unique = set(), []
    for row in rows:
        key = row["link"] or (row["title"], row["snippet"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique

def split_telegram_message(text, limit=3900):
    text = text or ""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text

def format_reply(user_query, all_rows, error_note=None):
    if error_note:
        return (
            "تعذّر إكمال البحث الآن.\n"
            f"السبب: {error_note}\n\n"
            "تحقّق من إعدادات مفتاح Serper ورصيده ثم أعد إرسال اسم المنتج."
        )

    # Prioritize results whose snippets appear to contain a price, but retain sources.
    priced = [r for r in all_rows if r["has_price"]]
    others = [r for r in all_rows if not r["has_price"]]
    ordered = (priced + others)[:MAX_RESULTS]

    if not ordered:
        return (
            f"بحثت عن: {user_query}\n\n"
            "لم أجد نتيجة موثوقة كافية لعرض سعر حالي. جرّب كتابة اسم المنتج كاملًا "
            "مع الحجم أو الوزن، مثل: لبن المراعي كامل الدسم 1 لتر."
        )

    lines = [
        f"🛒 نتائج البحث في مصر/القاهرة عن: {user_query}",
        "",
        "تنبيه: هذه نتائج ويب وليست تأكيدًا مباشرًا من المتجر. قد يتغير السعر أو يختلف حسب الفرع والعروض.",
    ]
    if not priced:
        lines += ["", "⚠️ لم يظهر سعر واضح بالجنيه في مقتطفات النتائج؛ لذلك لن أخمّن السعر."]
    lines.append("")
    for i, row in enumerate(ordered, 1):
        lines.append(f"{i}) {row['title']}")
        if row["snippet"]:
            lines.append(f"   {row['snippet']}")
        if row["source"]:
            lines.append(f"   المصدر: {row['source']}")
        if row["link"]:
            lines.append(f"   {row['link']}")
        lines.append("")
    lines += [
        "للمقارنة الأدق، أرسل اسم المنتج والحجم/الوزن والعلامة التجارية إن وُجدت.",
        "أبحث عند الطلب فقط؛ لا توجد تقارير يومية تلقائية.",
    ]
    return "\n".join(lines)

def answer_query(user_query):
    results = []
    errors = []
    for query in build_queries(user_query):
        try:
            data = serper_search(query)
            results.extend(extract_results(data))
        except Exception as exc:
            log.exception("Search failed")
            errors.append(str(exc))
            # Don't spend more credits if the first request failed.
            if not results:
                break
    if not results and errors:
        return format_reply(user_query, [], error_note=errors[0])
    return format_reply(user_query, results)

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
    if not SERPER_API_KEY:
        raise SystemExit("Missing SERPER_API_KEY environment variable.")

    log.info("Starting SmartHomeShopperEG polling bot.")
    # Remove any old webhook before long polling; this bot must be the only active poller.
    try:
        telegram("deleteWebhook", {"drop_pending_updates": False})
    except Exception:
        log.exception("Could not delete webhook; continuing to poll.")

    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = telegram("getUpdates", params, timeout=POLL_TIMEOUT + 15)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = clean_text(message.get("text"))
                if not text or text.startswith("/start") or text.startswith("/help"):
                    if text.startswith("/start") or text.startswith("/help"):
                        telegram("sendMessage", {
                            "chat_id": chat_id,
                            "text": (
                                "أهلًا بك في SmartHomeShopperEG 🛒\n"
                                "أرسل اسم أي منتج للبحث عن أسعاره ومصادره في مصر والقاهرة.\n"
                                "مثال: أرخص سعر لبن المراعي 1 لتر.\n"
                                "أقارن نتائج البحث المتاحة، ولا أخترع سعرًا عند عدم ظهوره.\n"
                                "البحث عند الطلب فقط — لا توجد تقارير يومية."
                            ),
                        })
                    continue
                if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
                    log.warning("Ignoring message from unapproved chat id %s", chat_id)
                    continue
                telegram("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                reply = answer_query(text)
                for part in split_telegram_message(reply):
                    telegram("sendMessage", {
                        "chat_id": chat_id,
                        "text": part,
                        "disable_web_page_preview": True,
                    })
        except KeyboardInterrupt:
            log.info("Stopping bot.")
            break
        except Exception:
            log.exception("Polling loop error; retrying in 5 seconds.")
            time.sleep(5)

if __name__ == "__main__":
    main()
