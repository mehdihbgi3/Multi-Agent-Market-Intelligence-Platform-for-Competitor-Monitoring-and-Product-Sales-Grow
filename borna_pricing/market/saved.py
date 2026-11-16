from __future__ import annotations

import email
import email.policy
import re
import urllib.parse

from .. import db, fmt
from ..clock import iso, now_utc
from ..service import ServiceError
from . import scout
from . import sites as site_mod
from .fetcher import Page, _sniff_charset

CHOICES = {"auto": "تشخیص خودکار", "torob": "ترب", "emalls": "ایمالز", "digikala": "دیجی‌کالا", "basalam": "باسلام",
           "divar": "دیوار (قطعات)", "divar_repair": "دیوار (تعمیرگاه)"}
KIND_LABELS = {"search": "صفحه جست‌وجو", "product": "صفحه کالا", "repair": "آگهی‌های خدمات خودرو"}
_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)\s*(\S+?)\s*-->", re.I)


def read_saved(data: bytes) -> tuple[str, str | None]:
    head = data[:4000].lower()
    if b"multipart/related" in head and (b"mime-version:" in head or head.lstrip().startswith(b"from:")):
        msg = email.message_from_bytes(data, policy=email.policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or _sniff_charset(raw) or "utf-8"
                try:
                    html = raw.decode(charset, errors="replace")
                except LookupError:
                    html = raw.decode("utf-8", errors="replace")
                return html, msg.get("Snapshot-Content-Location") or part.get("Content-Location")
        raise ServiceError("در این فایل MHTML صفحه وب پیدا نشد.")
    return data.decode(_sniff_charset(data) or "utf-8", errors="replace"), None


def _tag_attr(html: str, tag: str, key: str, value: str, want: str) -> str | None:
    for t in re.findall(rf"<{tag}\b[^>]*>", html[:300_000], re.I):
        if re.search(rf"""\b{key}\s*=\s*["']{re.escape(value)}["']""", t, re.I):
            m = re.search(rf"""\b{want}\s*=\s*["']([^"']+)["']""", t, re.I)
            if m:
                return m.group(1).strip()
    return None


def find_url(html: str) -> str | None:
    m = _SAVED_FROM.search(html[:5000])
    for cand in (m.group(1) if m else None, _tag_attr(html, "meta", "property", "og:url", "content"),
                 _tag_attr(html, "link", "rel", "canonical", "href")):
        if cand and cand.lower().startswith(("http://", "https://")):
            return cand
    return None


def _site_of_url(url: str | None) -> str | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower() if url else ""
    for key, site in site_mod.SITES.items():
        if host == site.domain or host.endswith("." + site.domain):
            return key
    return None


def detect_site(url: str | None, html: str) -> str | None:
    key = _site_of_url(url)
    if key:
        return key
    counts = {key: html.count(site.domain) for key, site in site_mod.SITES.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] >= 3 else None


def detect_kind(key: str, url: str | None, choice: str) -> str:
    site = site_mod.SITES[key]
    if key == "divar":
        return "repair" if choice == "divar_repair" or (url and "vehicles-services" in url) else "search"
    if key in ("torob", "digikala") and url and site.group_of(url):
        return "product"
    return "search"


def import_page(ms, data: bytes, filename: str, choice: str, given_url: str, user: str, role: str) -> dict:
    if choice not in CHOICES:
        raise ServiceError("سایت را از فهرست انتخاب کنید.")
    if not data:
        raise ServiceError("فایلی انتخاب نشده است.")
    if len(data) > ms.cfg.collector.max_upload_mb * 1024 * 1024:
        raise ServiceError(f"حجم فایل بیش از {fmt.fa(ms.cfg.collector.max_upload_mb)} مگابایت است.")
    given_url = (given_url or "").strip()
    if given_url and not given_url.lower().startswith(("http://", "https://")):
        raise ServiceError("نشانی صفحه باید با http یا https شروع شود.")
    html, mhtml_url = read_saved(data)
    if "<" not in html[:20_000] or not re.search(r"<(?:html|body|div|article|a)\b", html, re.I):
        raise ServiceError("این فایل صفحه وب نیست؛ صفحه را در مرورگر با Ctrl+S ذخیره کنید.")
    url = given_url or mhtml_url or find_url(html)
    key = "divar" if choice in ("divar", "divar_repair") else (choice if choice != "auto" else detect_site(url, html))
    if key is None:
        raise ServiceError("سایت این صفحه شناخته نشد؛ سایت را از فهرست انتخاب کنید.")
    site = site_mod.SITES[key]
    if url and _site_of_url(url) not in (None, key):
        raise ServiceError(f"نشانی این صفحه مال {site.label} نیست.")
    if url and _site_of_url(url) is None:
        url = None
    if key == "divar" and url and urllib.parse.urlsplit(url).path.startswith("/v/"):
        raise ServiceError("این صفحه یک آگهی تکی دیوار است؛ صفحه فهرست جست‌وجو (قطعات یدکی یا خدمات موتور و ماشین) "
                           "را ذخیره و بارگذاری کنید.")
    kind = detect_kind(key, url, choice)
    page_url = url or f"https://{site.domain}/"
    parse = site.parse_repairs if kind == "repair" else site.parse_product if kind == "product" else site.parse_search
    parsed = parse(html, page_url)
    if not parsed.items and not parsed.businesses and not parsed.product_urls:
        hint = (" اگر این صفحه یک کالاست، نشانی صفحه را هم در فرم وارد کنید."
                if kind == "search" and key in ("torob", "digikala") and not url else "")
        raise ServiceError("در این صفحه کالا یا تعمیرگاهی پیدا نشد. صفحه را بعد از کامل باز شدن ذخیره کنید و سایت را "
                           "درست انتخاب کنید." + hint)
    src = scout.ensure_source(ms, site)
    status = "ok" if role in ("approver", "admin") else "pending"
    st = ms._new_stats(src)
    name = re.sub(r"[\r\n\t]+", " ", filename or "page.html").strip()[:200] or "page.html"
    n_biz = len(parsed.businesses)
    note = (f"{fmt.fa(n_biz)} تعمیرگاه پیدا شد." if kind == "repair" else
            f"{fmt.fa(len(parsed.product_urls))} کالا چند فروشنده دارد؛ صفحه هر کالا را جدا بارگذاری کنید.")
    ms._store_page(src, Page(page_url, 200, "text/html", html.encode("utf-8"), "utf-8"), parsed.items, [], user,
                   None, st, [], f"بارگذاری «{name}»", empty_note=note, check_drop=False, status=status, filename=name)
    new_biz = 0
    if n_biz:
        with ms.db() as c, db.tx(c):
            new_biz = scout.upsert_businesses(c, parsed.businesses, site.label, iso(now_utc()))
    out = {"site": key, "label": site.label, "kind": kind, "url": url, "items": st["items"],
           "observations": st["observations"], "new_listings": st["new_listings"], "quarantined": st["quarantined"],
           "pending": st.get("pending", 0), "unchanged": st["unchanged"], "businesses": n_biz,
           "new_businesses": new_biz, "product_urls": len(parsed.product_urls) if kind == "search" else 0}
    with ms.db() as c, db.tx(c):
        db.audit(c, iso(now_utc()), user, "page_upload", {"source": src["key"], "file": name, "kind": kind,
                                                          "url": url, "items": out["items"], "businesses": n_biz})
    ms.match_pending(user)
    if getattr(ms, "sales", None) is not None:
        ms.sales.refresh_leads()
    return out


def summary(r: dict) -> str:
    parts = [f"{KIND_LABELS[r['kind']]} {r['label']} خوانده شد"]
    if r["items"]:
        parts.append(f"{fmt.fa(r['items'])} آگهی، {fmt.fa(r['observations'])} قیمت تازه، "
                     f"{fmt.fa(r['new_listings'])} آگهی تازه")
    if r["quarantined"]:
        parts.append(f"{fmt.fa(r['quarantined'])} قیمت قرنطینه شد")
    if r["pending"]:
        parts.append(f"{fmt.fa(r['pending'])} قیمت منتظر تأیید مدیر مالی است")
    if r["businesses"]:
        parts.append(f"{fmt.fa(r['businesses'])} تعمیرگاه ({fmt.fa(r['new_businesses'])} تازه)")
    text = "؛ ".join(parts) + "."
    if r["product_urls"]:
        text += (f" {fmt.fa(r['product_urls'])} کالا چند فروشنده دارد؛ برای قیمت همه فروشنده‌ها، صفحه همان کالا را هم "
                 "بارگذاری کنید.")
    return text
