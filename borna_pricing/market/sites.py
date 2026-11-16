from __future__ import annotations

import html as htmllib
import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field

from . import textnorm as tn
from .extract import RawItem, clean
from .prices import schema_availability

REPAIR_WORDS = ("برق خودرو", "برق ماشین", "انژکتور", "تنظیم موتور", "دیاگ", "ایسیو", "ecu", "مکانیک", "تعمیرگاه",
                "تعمیرات خودرو", "تعمیر خودرو", "تعمیر ماشین", "تعمیرات موتور", "الکترونیک")
NOT_REPAIR_WORDS = ("یدک کش", "یدککش", "خودروبر", "کارواش", "صفرشویی", "صفر شویی", "دیتیلینگ", "پولیش", "سرامیک",
                    "کاور بدنه", "کاوربدنه", "نقاشی", "صافکاری", "شیشه", "سانروف", "آموزش", "اجاره", "کرایه", "استخدام")
GENERIC_NAMES = ("تعمیرگاه", "تعمیرگاه خودرو", "تعمیرگاه اتومبیل", "تعمیرگاه ماشین", "مکانیکی", "مکانیک",
                 "مکانیکی خودرو", "تعمیر خودرو", "تعمیرات خودرو", "برق خودرو", "برق ماشین", "لوازم یدکی",
                 "لوازم یدکی خودرو", "فروشگاه لوازم یدکی", "قطعات یدکی", "قطعات خودرو", "car repair", "auto repair",
                 "garage", "repair", "car parts", "auto parts", "workshop")
CITY_RADIUS_KM = 30
PLATFORM_DOMAINS = ("emalls.ir", "torob.com", "digikala.com", "basalam.com", "divar.ir", "divarcdn.com", "sheypoor.com",
                    "snappshop.ir", "timcheh.com", "esam.ir", "openstreetmap.org", "arvancloud.ir", "arvancloud.com",
                    "cloudfront.net", "googleusercontent.com", "wp.com", "imgur.com", "r2.dev")
_SITE_PREFIX = re.compile(r"^(?:www|cdn|static|statics|img|image|images|media|files|dl|s)\d*\.")
_DIGITS = r"[0-9۰-۹٠-٩]"
_PERSIAN = re.compile(r"[\u0600-\u06FF]")


@dataclass
class Business:
    name: str
    kind: str
    city: str | None
    url: str | None
    address: str | None = None
    key: str | None = None
    phone: str | None = None


@dataclass
class Parsed:
    items: list[RawItem] = field(default_factory=list)
    product_urls: list[str] = field(default_factory=list)
    businesses: list[Business] = field(default_factory=list)
    cards: int = 0
    skipped: int = 0


def _text(fragment) -> str:
    return clean(fragment)


def _first(rx: str, text: str, flags=re.S | re.I) -> str | None:
    m = re.search(rx, text, flags)
    return m.group(1) if m else None


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\s{re.escape(name)}\s*=\s*"([^"]*)"', tag, re.I) or re.search(rf"\s{re.escape(name)}\s*=\s*'([^']*)'", tag, re.I)
    return clean(m.group(1)) if m else None


def _segments(html: str, marker: str) -> list[tuple[int, int]]:
    starts = [m.start() for m in re.finditer(marker, html)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(html)) for i, s in enumerate(starts)]


def _q(text: str) -> str:
    return urllib.parse.quote(re.sub(r"\s+", " ", str(text)).strip(), safe="")


def on_platform(url: str | None) -> bool:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in PLATFORM_DOMAINS)


def _shop_site(seg: str) -> str | None:
    for m in re.finditer(r"service\.emalls\.ir/r/\d+/(https?%3a%2f%2f[^\s\"',]+)", seg, re.I):
        target = urllib.parse.unquote(m.group(1))
        host = _SITE_PREFIX.sub("", (urllib.parse.urlsplit(target).hostname or "").lower())
        if "." in host and not on_platform(f"https://{host}/"):
            return f"https://{host}/"
    return None


def _strip_query(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _nkey(name: str) -> str:
    return re.sub(r"\s+", " ", tn.norm(name or "")).strip()[:120]


class Site:
    key = ""
    label = ""
    domain = ""
    kind = "aggregator"
    unit = "toman"
    price_level = "retail"
    mode = "light"
    ready: str | None = None
    collect: str | None = None
    product_ready: str | None = None
    product_mode = "light"

    def search_urls(self, query: str, pages: int, region: str) -> list[str]:
        raise NotImplementedError

    def parse_search(self, html: str, url: str) -> Parsed:
        raise NotImplementedError

    def parse_product(self, html: str, url: str) -> Parsed:
        return Parsed()

    def group_of(self, url: str) -> str | None:
        return None


class Torob(Site):
    key = "torob"
    label = "ترب"
    domain = "torob.com"
    kind = "aggregator"
    mode = "auto"
    ready = '[data-testid="product-card"]'
    product_mode = "auto"

    def search_urls(self, query, pages, region):
        return [f"https://torob.com/search/?query={_q(query)}"]

    def group_of(self, url):
        return _first(r"/p/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url or "")

    def parse_search(self, html, url):
        out = Parsed()
        segs = _segments(html, r'data-testid="product-card"')
        prev = 0
        for s, e in segs:
            out.cards += 1
            seg = html[s:e]
            links = re.findall(r'href="((?:https://torob\.com)?/p/[^"]+)"', html[prev:s])
            prev = s
            title = _text(_first(r"<h2[^>]*>(.*?)</h2>", seg) or "")
            if not links or not title:
                out.skipped += 1
                continue
            purl = _strip_query(urllib.parse.urljoin("https://torob.com/", links[-1]))
            group = self.group_of(purl)
            head = seg[: seg.find("</a>")] if "</a>" in seg else seg[:3000]
            if "کارکرده" in _text(head):
                out.skipped += 1
                continue
            price = _text(_first(r'class="[^"]*price-text[^"]*"[^>]*>(.*?)</div>', seg) or "")
            shops = _text(_first(r'class="[^"]*_shops[^"]*"[^>]*>(.*?)</div>', seg) or "")
            many = re.search(rf"در\s*{_DIGITS}+\s*فروشگاه", shops)
            shop = shops[2:].strip() if shops.startswith("در ") and not many else ""
            if not group:
                out.skipped += 1
                continue
            if shop:
                out.items.append(RawItem(title=title, price=price or None, url=purl, seller=shop,
                                         ext_id=f"{group}|{_nkey(shop)}", group=group))
            elif purl not in out.product_urls:
                out.product_urls.append(purl)
        return out

    def parse_product(self, html, url):
        out = Parsed()
        group = self.group_of(url)
        if not group:
            return out
        purl = _strip_query(url)
        product = None
        for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
            try:
                data = json.loads(block.strip())
            except ValueError:
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict) and "product" in str(node.get("@type", "")).lower():
                    product = node
                    break
            if product:
                break
        if not product:
            return out
        cities = {}
        for m in re.finditer(r"<a\b[^>]*\bshop-name\b[^>]*>(.*?)</a>", html, re.S):
            name = _text(m.group(1))
            city = _text(_first(r"\bcity-name\b[^>]*>(.*?)</a>", html[m.end(): m.end() + 1500]) or "")
            if name and city and _nkey(name) not in cities:
                cities[_nkey(name)] = city
        title = _text(product.get("name") or "")
        offers = product.get("offers") or {}
        if isinstance(offers, dict):
            offers = offers.get("offers") or ([offers] if offers.get("price") is not None else [])
        best: dict[str, tuple] = {}
        for o in offers if isinstance(offers, list) else []:
            if not isinstance(o, dict):
                continue
            seller = o.get("seller")
            shop = _text(o.get("name") or (seller.get("name") if isinstance(seller, dict) else "") or "")
            if not shop:
                continue
            try:
                price = int(float(str(o.get("price") or 0)))
            except ValueError:
                price = 0
            avail = schema_availability(o.get("availability"))
            key = _nkey(shop)
            cur = best.get(key)
            if cur is None or (price > 0 and (cur[1] <= 0 or price < cur[1])):
                best[key] = (shop, price, avail, str(o.get("priceCurrency") or "IRR").upper())
        for key, (shop, price, avail, cur) in best.items():
            out.cards += 1
            unit_word = "تومان" if cur in ("IRT", "TOMAN") else "ریال"
            out.items.append(RawItem(title=title, price=f"{price} {unit_word}" if price > 0 else None,
                                     availability="out_of_stock" if price <= 0 else avail, url=purl, seller=shop,
                                     ext_id=f"{group}|{key}", group=group, city=cities.get(key)))
        return out


class Emalls(Site):
    key = "emalls"
    label = "ایمالز"
    domain = "emalls.ir"
    kind = "aggregator"
    mode = "auto"
    ready = ".product-block-parent"

    def search_urls(self, query, pages, region):
        words = re.sub(r"\s+", " ", query).strip()
        base = f"/لیست-قیمت~skey~{words}"
        paths = [base] + [f"{base}~page~{n}" for n in range(2, max(1, pages) + 1)]
        return ["https://emalls.ir" + urllib.parse.quote(p, safe="/~") for p in paths]

    def parse_search(self, html, url):
        out = Parsed()
        seen: set[str] = set()
        for s, e in _segments(html, r'<div class="product-block-parent\b'):
            seg = html[s:e]
            title = _text(_first(r'class="prd-name[^"]*"[^>]*>(.*?)</(?:span|a)>', seg) or "")
            if not title:
                continue
            out.cards += 1
            shop = _text(_first(r"خرید\u200c?\s*از\s*([^<]+)<", seg) or "")
            if not shop:
                out.skipped += 1
                continue
            price = _text(_first(r'class="prd-price"[^>]*>\s*<span[^>]*>(.*?)</span>', seg) or "")
            ext = None
            for raw in re.findall(r'data-esrever="([^"]+)"', seg):
                link = clean(raw)[::-1]
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
                if q.get("exid"):
                    ext = f"em{q.get('itemid', ['0'])[0]}-{q['exid'][0]}"
                    break
            ext = ext or f"em:{_nkey(shop)}:{_nkey(title)}"[:200]
            if ext in seen:
                continue
            seen.add(ext)
            page = _first(r'href="((?:https://emalls\.ir)?/[^"]*~id~\d+[^"]*)"', seg)
            link = (urllib.parse.quote(urllib.parse.urljoin("https://emalls.ir/", htmllib.unescape(page)), safe=":/~%?=&")
                    if page else _shop_site(seg) or url)
            out.items.append(RawItem(title=title, price=price or None, seller=shop, ext_id=ext, url=link))
        return out


class Digikala(Site):
    key = "digikala"
    label = "دیجی‌کالا"
    domain = "digikala.com"
    kind = "marketplace"
    mode = "browser"
    ready = '[data-testid="product-card"]'
    product_mode = "browser"
    product_ready = '[data-testid="pdp-title"]'

    def search_urls(self, query, pages, region):
        return [f"https://www.digikala.com/search/?q={_q(query)}"]

    def group_of(self, url):
        m = re.search(r"/product/dkp-(\d+)", url or "")
        return f"dkp-{m.group(1)}" if m else None

    def parse_search(self, html, url):
        out = Parsed()
        prev = 0
        for s, e in _segments(html, r'data-testid="product-card"'):
            out.cards += 1
            links = re.findall(r'href="((?:https://www\.digikala\.com)?/product/dkp-\d+[^"]*)"', html[prev:s])
            prev = s
            if not links:
                out.skipped += 1
                continue
            purl = _strip_query(urllib.parse.urljoin("https://www.digikala.com/", links[-1].replace("&amp;", "&")))
            if purl not in out.product_urls:
                out.product_urls.append(purl)
        return out

    def parse_product(self, html, url):
        out = Parsed()
        group = self.group_of(url)
        title = _text(_first(r'data-testid="pdp-title"[^>]*>(.*?)</h1>', html) or "")
        if not group or not title:
            return out
        purl = _strip_query(url)
        start = html.find("فروشندگان این کالا")
        zone = html[start:] if start >= 0 else html
        seen: set[str] = set()
        anchors = list(re.finditer(r'href="(?:https://www\.digikala\.com)?/seller/([A-Za-z0-9]+)/?"[^>]*>(.*?)</a>', zone, re.S))
        if start < 0:
            anchors = anchors[:1]
        for i, m in enumerate(anchors):
            code = m.group(1).upper()
            name = _text(m.group(2))
            if not name or code in seen:
                continue
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(zone)
            seg = zone[m.end(): end]
            cart = seg.find("data-add-to-cart-variant-id")
            body = seg[:cart] if cart >= 0 else seg[:6000]
            price = _first(r'data-testid="price-final"[^>]*>(.*?)</span>', body)
            if price is None:
                nums = re.findall(rf"<span[^>]*>\s*({_DIGITS}[0-9۰-۹٠-٩,٬]*)\s*</span>", body)
                price = nums[-1] if nums else None
            seen.add(code)
            out.cards += 1
            out.items.append(RawItem(title=title, price=f"{_text(price)} تومان" if price else None,
                                     availability="unknown" if price else "out_of_stock", url=purl, seller=name,
                                     ext_id=f"{group}|{code}", group=group))
        return out


class Basalam(Site):
    key = "basalam"
    label = "باسلام"
    domain = "basalam.com"
    kind = "marketplace"
    mode = "browser"
    ready = 'a[data-component="search-items"]'
    collect = 'a[data-component="search-items"]'

    def search_urls(self, query, pages, region):
        return [f"https://basalam.com/s?q={_q(query)}"]

    def parse_search(self, html, url):
        out = Parsed()
        seen: set[str] = set()
        for s, e in _segments(html, r'<a [^>]*data-component="search-items"'):
            tag = html[s: html.find(">", s) + 1]
            seg = html[s: html.find("</a>", s) + 4 if html.find("</a>", s) > 0 else e]
            href = _attr(tag, "href") or ""
            m = re.search(r"/([^/?#]+)/product/(\d+)", href)
            if not m:
                continue
            out.cards += 1
            if m.group(2) in seen:
                continue
            seen.add(m.group(2))
            title = _text(_first(r"<h2[^>]*>(.*?)</h2>", seg) or "")
            vendor = _first(r"bi-vendor[^>]*>\s*</span>\s*<span[^>]*>(.*?)</span>\s*</div>", seg) or ""
            city = _text(re.split(r"<span", vendor, maxsplit=1)[0])
            booth = _text(_first(r'<div title="([^"]+)"', vendor) or _first(r'\|</span>\s*<div[^>]*>(.*?)</div>', vendor) or "")
            price = _first(r">\s*([0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬]*)\s*<span class=\"bi-toman", seg)
            old = _first(r">\s*([0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬]*)\s*</span>\s*<span[^>]*>\s*٪", seg)
            if not title:
                out.skipped += 1
                continue
            out.items.append(RawItem(title=title, price=f"{price} تومان" if price else None,
                                     list_price=f"{old} تومان" if old else None, seller=booth or None,
                                     city=city or None, url=urllib.parse.urljoin("https://basalam.com/", href),
                                     ext_id=f"bsl-{m.group(2)}"))
        return out


def _divar_cards(html: str):
    for s, e in _segments(html, r'<article class="kt-post-card\b'):
        seg = html[s: html.find("</article>", s) + 10 if html.find("</article>", s) > 0 else e]
        a = re.search(r'<a\b[^>]*kt-post-card__action[^>]*>', seg)
        href = _attr(a.group(0), "href") if a else None
        title = _text(_first(r'class="kt-post-card__title"[^>]*>(.*?)</h2>', seg) or "")
        descs = [_text(x) for x in re.findall(r'class="kt-post-card__description"[^>]*>(.*?)</div>', seg, re.S)]
        bottom = _first(r'class="kt-post-card__bottom-description[^"]*"[^>]*title="([^"]*)"', seg) or \
            _text(_first(r'class="kt-post-card__bottom-description[^"]*"[^>]*>(.*?)</span>', seg) or "")
        if not href or not title:
            continue
        place = re.findall(r"(?<!\S)(?:در|از)\s+([^·|]+?)\s*(?=·|$)", clean(bottom))
        url = _strip_query(urllib.parse.urljoin("https://divar.ir/", href))
        token = url.rstrip("/").rsplit("/", 1)[-1]
        city = re.split(r"[،,]", place[-1])[0].strip() if place else None
        yield {"title": title, "descs": descs, "city": city or None, "url": url, "token": token}


class Divar(Site):
    key = "divar"
    label = "دیوار"
    domain = "divar.ir"
    kind = "marketplace"
    mode = "auto"
    ready = "article.kt-post-card"

    def search_urls(self, query, pages, region):
        return [f"https://divar.ir/s/{region}/car-accessories?q={_q(query)}"]

    def repair_urls(self, query, region):
        return [f"https://divar.ir/s/{region}/vehicles-services?q={_q(query)}"]

    def parse_search(self, html, url):
        out = Parsed()
        for card in _divar_cards(html):
            out.cards += 1
            price = next((d for d in card["descs"] if "تومان" in d or "توافقی" in d), None)
            out.items.append(RawItem(title=card["title"], price=price, url=card["url"], city=card["city"],
                                     ext_id=f"dvr-{card['token']}"))
        return out

    def parse_repairs(self, html, url) -> Parsed:
        out = Parsed()
        for card in _divar_cards(html):
            out.cards += 1
            t = tn.norm(card["title"])
            if not any(w in t for w in REPAIR_WORDS) or any(w in t for w in NOT_REPAIR_WORDS):
                out.skipped += 1
                continue
            out.businesses.append(Business(name=card["title"], kind="repair", city=card["city"], url=card["url"]))
        return out


def _coords(el: dict) -> tuple[float | None, float | None]:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    c = el.get("center") or {}
    if "lat" in c and "lon" in c:
        return float(c["lat"]), float(c["lon"])
    return None, None


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat * 2), math.floor(lon * 2)


def _places(elements: list) -> dict:
    grid: dict = {}
    for el in elements:
        tags = el.get("tags") or {}
        name = clean(tags.get("name:fa") or tags.get("name") or "")
        lat, lon = _coords(el)
        if name and lat is not None:
            grid.setdefault(_cell(lat, lon), []).append((name, lat, lon))
    return grid


def _nearest(grid: dict, lat: float | None, lon: float | None) -> str | None:
    if lat is None or not grid:
        return None
    i, j = _cell(lat, lon)
    best, best_d = None, CITY_RADIUS_KM
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for name, plat, plon in grid.get((i + di, j + dj), ()):
                dx = (plon - lon) * 111.32 * math.cos(math.radians((plat + lat) / 2))
                d = math.hypot(dx, (plat - lat) * 110.57)
                if d <= best_d:
                    best, best_d = name, d
    return best


def _address(tags: dict) -> str | None:
    if tags.get("addr:full"):
        return clean(tags["addr:full"])[:200]
    street = " ".join(x for x in (clean(tags.get("addr:street") or ""), clean(tags.get("addr:housenumber") or "")) if x)
    parts = []
    for x in (tags.get("addr:suburb"), tags.get("addr:neighbourhood"), tags.get("addr:place"), street):
        x = clean(x or "")
        if x and x not in parts:
            parts.append(x)
    return "، ".join(parts)[:200] or None


def overpass_elements(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("پاسخ سرویس نقشه JSON نیست.") from None
    remark = str(data.get("remark") or "") if isinstance(data, dict) else ""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list) or "error" in remark.lower():
        raise ValueError(f"سرویس نقشه جواب درستی نداد: {remark[:160] or 'ساختار پاسخ نامعتبر است'}")
    return data["elements"]


class OpenStreetMap(Site):
    key = "osm"
    label = "اوپن‌استریت‌مپ"
    domain = "overpass-api.de"
    kind = "aggregator"
    price_level = "unknown"
    SHOP_TAGS = ("car_repair", "car_parts")
    PHONE_TAGS = ("phone", "contact:phone", "mobile", "contact:mobile")
    _AREA = 'area["ISO3166-1"="IR"][admin_level=2]->.ir;'

    def shops_query(self) -> str:
        return '[out:json][timeout:180];' + self._AREA + 'nwr["shop"~"^(car_repair|car_parts)$"](area.ir);out center qt;'

    def places_query(self) -> str:
        return '[out:json][timeout:120];' + self._AREA + 'node["place"~"^(city|town)$"](area.ir);out qt;'

    @staticmethod
    def request_url(base: str, query: str) -> str:
        return base + ("&" if "?" in base else "?") + "data=" + urllib.parse.quote(query, safe="")

    def parse_map(self, shops_text: str, places_text: str | None) -> Parsed:
        out = Parsed()
        grid = _places(overpass_elements(places_text)) if places_text else {}
        generic = {_nkey(g) for g in GENERIC_NAMES}
        blocked = [_nkey(w) for w in NOT_REPAIR_WORDS]
        seen: set[str] = set()
        for el in overpass_elements(shops_text):
            tags = el.get("tags") or {}
            shop = tags.get("shop")
            if shop not in self.SHOP_TAGS:
                continue
            out.cards += 1
            name = clean(tags.get("name:fa") or tags.get("name") or "")
            norm = _nkey(name)
            if (not any(ch.isalpha() for ch in name) or norm in generic
                    or (shop == "car_repair" and any(w in norm for w in blocked))):
                out.skipped += 1
                continue
            lat, lon = _coords(el)
            tagged = clean(tags.get("addr:city:fa") or tags.get("addr:city") or "")
            city = (tagged if _PERSIAN.search(tagged) else None) or _nearest(grid, lat, lon) or tagged or None
            ref = f"{el.get('type')}/{el.get('id')}"
            key = (f"{norm} @ {_nkey(city)}" if city else f"{norm} @ osm:{ref}")[:200]
            if key in seen:
                out.skipped += 1
                continue
            seen.add(key)
            out.businesses.append(Business(name=name[:120], kind="repair" if shop == "car_repair" else "seller",
                                           city=city[:60] if city else None, url=f"https://www.openstreetmap.org/{ref}",
                                           address=_address(tags), key=key,
                                           phone=tn.phones(";".join(tags[t] for t in self.PHONE_TAGS if tags.get(t)))))
        return out


SITES: dict[str, Site] = {s.key: s for s in (Torob(), Emalls(), Digikala(), Basalam(), Divar())}
OSM = OpenStreetMap()
ALL_SITES: dict[str, Site] = {**SITES, OSM.key: OSM}
