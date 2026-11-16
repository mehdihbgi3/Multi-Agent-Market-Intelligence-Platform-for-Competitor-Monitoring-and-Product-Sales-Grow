from __future__ import annotations

import configparser
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import jalali

APP_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_TOKEN = "PUT_YOUR_TOKEN_HERE"
PROVIDERS = ("pricetoday", "alanchand")

WEEKDAY_KEYS = {
    "saturday": 5, "sunday": 6, "monday": 0, "tuesday": 1,
    "wednesday": 2, "thursday": 3, "friday": 4,
}

TIER_KEYS = ("genuine", "oem", "aftermarket", "unknown")

DEFAULTS: dict[str, dict[str, str]] = {
    "source": {
        "provider": "pricetoday",
        "mode": "live",
        "timeout_seconds": "20",
    },
    "pricetoday": {
        "url": "https://api.priceto.day/v1/latest/irr/usd",
        "unit": "rial",
        "sample_file": "tests/fixtures/pricetoday_usd_sample.json",
    },
    "alanchand": {
        "token": PLACEHOLDER_TOKEN,
        "url": "https://api.alanchand.com/",
        "type": "currencies",
        "slug": "usd",
        "field": "sell",
        "unit": "toman",
        "sample_file": "tests/fixtures/alanchand_currencies_sample.json",
        "source_utc_offset_minutes": "210",
    },
    "pricing": {
        "base_rate_toman": "260000",
        "base_date": "1405/07/20",
        "base_time": "09:00",
        "alpha_default": "1",
        "round_step_rial": "500000",
        "publish_threshold": "0.02",
        "approval_threshold": "0.05",
        "change_basis": "exact",
        "auto_publish": "false",
        "auto_renew": "true",
        "always_publish": "true",
    },
    "guards": {
        "fetch_interval_minutes": "60",
        "retry_minutes": "10",
        "max_age_minutes": "180",
        "jump_threshold": "0.03",
        "min_rate_toman": "100000",
        "max_rate_toman": "2000000",
    },
    "hours": {
        "saturday": "09:00-17:00",
        "sunday": "09:00-17:00",
        "monday": "09:00-17:00",
        "tuesday": "09:00-17:00",
        "wednesday": "09:00-17:00",
        "thursday": "09:00-13:00",
        "friday": "closed",
        "holidays": "",
    },
    "web": {
        "host": "0.0.0.0",
        "port": "8080",
        "company_name": "برنا سنسور",
        "pricelist_title": "لیست قیمت",
        "validity_text": "تا پایان روز کاری",
        "show_rate_on_pricelist": "true",
        "footer_note": "",
        "session_hours": "12",
        "behind_https_proxy": "false",
    },
    "paths": {
        "database": "data/borna_pricing.sqlite3",
        "products_xlsx": "data/Borna_Step2-3_Products_Pricing.xlsx",
        "log_file": "data/borna_pricing.log",
        "secret_key_file": "data/secret.key",
        "evidence_dir": "data/evidence",
    },
    "market": {
        "own_sellers": "برنا سنسور",
        "min_price_usd": "2",
        "max_price_usd": "60",
        "jump_quarantine": "0.5",
        "stale_days": "14",
        "length_tolerance_mm": "30",
        "separate_tiers": "genuine",
        "min_prices": "3",
        "outlier_high": "2.5",
        "outlier_low": "0.4",
        "auto_approve_strong": "true",
    },
    "collector": {
        "user_agent": "BornaMarketBot/1.2",
        "timeout_seconds": "20",
        "min_delay_seconds": "3",
        "max_bytes": "6000000",
        "max_upload_mb": "8",
        "respect_robots": "false",
    },
    "browser": {
        "enabled": "true",
        "channel": "msedge",
        "executable_path": "",
        "user_agent": "",
        "timeout_seconds": "45",
        "max_scrolls": "6",
        "scroll_pause_ms": "1500",
        "max_mb": "12",
    },
    "scout": {
        "enabled": "true",
        "sites": "torob, emalls, digikala, basalam, divar",
        "interval_hours": "24",
        "queries_per_site": "6",
        "pages_per_query": "2",
        "product_pages": "15",
        "delay_seconds": "5",
        "extra_keywords": "",
        "repair_keywords": "برق خودرو, تنظیم موتور, انژکتور, تعمیرگاه برق خودرو, دیاگ خودرو",
        "divar_region": "iran",
        "site_delay_seconds": "torob: 15",
        "site_queries": "torob: 3",
        "osm": "true",
        "osm_days": "7",
        "osm_urls": "https://overpass-api.de/api/interpreter",
    },
    "agents": {
        "enabled": "true",
        "poll_seconds": "60",
        "report_weekday": "saturday",
        "report_time": "10:00",
    },
    "credit": {
        "annual_cost_of_money": "",
        "annual_dollar_change": "",
        "check_days": "30, 45, 60, 90",
    },
    "advisor": {
        "target_band": "0.05",
        "min_margin": "",
        "min_prices": "2",
    },
    "llm": {
        "enabled": "false",
        "base_url": "",
        "model": "",
        "api_key": "",
        "timeout_seconds": "60",
        "daily_call_limit": "300",
        "max_input_chars": "6000",
    },
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SourceCfg:
    provider: str
    mode: str
    timeout_seconds: int


@dataclass(frozen=True)
class PricetodayCfg:
    url: str
    unit: str
    sample_file: Path


@dataclass(frozen=True)
class AlanchandCfg:
    token: str
    url: str
    type: str
    slug: str
    field: str
    unit: str
    sample_file: Path
    source_utc_offset_minutes: int

    @property
    def token_is_set(self) -> bool:
        return bool(self.token) and self.token != PLACEHOLDER_TOKEN


@dataclass(frozen=True)
class PricingCfg:
    base_rate_toman: int
    base_date: date
    base_date_text: str
    base_time: time
    alpha_default: Decimal
    round_step_rial: int
    publish_threshold: Decimal
    approval_threshold: Decimal
    change_basis: str
    auto_publish: bool
    auto_renew: bool
    always_publish: bool = True

    @property
    def effective_publish_threshold(self) -> Decimal:
        return Decimal(0) if self.always_publish else self.publish_threshold

    @property
    def publishes_automatically(self) -> bool:
        return self.always_publish or self.auto_publish

    @property
    def base_rate_rial(self) -> int:
        return self.base_rate_toman * 10


@dataclass(frozen=True)
class GuardsCfg:
    fetch_interval_minutes: int
    retry_minutes: int
    max_age_minutes: int
    jump_threshold: Decimal
    min_rate_toman: int
    max_rate_toman: int


@dataclass(frozen=True)
class HoursCfg:
    days: dict
    holidays: frozenset


@dataclass(frozen=True)
class WebCfg:
    host: str
    port: int
    company_name: str
    pricelist_title: str
    validity_text: str
    show_rate_on_pricelist: bool
    footer_note: str
    session_hours: int
    behind_https_proxy: bool


@dataclass(frozen=True)
class PathsCfg:
    database: Path
    products_xlsx: Path
    log_file: Path
    secret_key_file: Path
    evidence_dir: Path


@dataclass(frozen=True)
class MarketCfg:
    own_sellers: tuple
    min_price_usd: Decimal
    max_price_usd: Decimal
    jump_quarantine: Decimal
    stale_days: int
    length_tolerance_mm: int
    separate_tiers: tuple = ("genuine",)
    min_prices: int = 3
    outlier_high: Decimal = Decimal("2.5")
    outlier_low: Decimal = Decimal("0.4")
    auto_approve_strong: bool = True


@dataclass(frozen=True)
class CollectorCfg:
    user_agent: str
    timeout_seconds: int
    min_delay_seconds: int
    max_bytes: int
    max_upload_mb: int
    respect_robots: bool


@dataclass(frozen=True)
class BrowserCfg:
    enabled: bool
    channel: str
    executable_path: str
    user_agent: str
    timeout_seconds: int
    max_scrolls: int
    scroll_pause_ms: int
    max_mb: int


@dataclass(frozen=True)
class ScoutCfg:
    enabled: bool
    sites: tuple
    interval_hours: int
    queries_per_site: int
    pages_per_query: int
    product_pages: int
    delay_seconds: int
    extra_keywords: tuple
    repair_keywords: tuple
    divar_region: str
    site_delays: tuple = ()
    site_queries: tuple = ()
    osm: bool = True
    osm_days: int = 7
    osm_urls: tuple = ("https://overpass-api.de/api/interpreter",)

    def delay_for(self, site: str) -> int:
        return max([self.delay_seconds] + [v for k, v in self.site_delays if k == site])

    def queries_for(self, site: str) -> int:
        return min([self.queries_per_site] + [v for k, v in self.site_queries if k == site])


@dataclass(frozen=True)
class AgentsCfg:
    enabled: bool
    poll_seconds: int
    report_weekday: int
    report_time: time


@dataclass(frozen=True)
class CreditCfg:
    annual_cost_of_money: Decimal | None
    annual_dollar_change: Decimal | None
    check_days: tuple


@dataclass(frozen=True)
class AdvisorCfg:
    target_band: Decimal
    min_margin: Decimal | None
    min_prices: int


@dataclass(frozen=True)
class LlmCfg:
    enabled: bool
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    daily_call_limit: int
    max_input_chars: int

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.base_url and self.model)


@dataclass(frozen=True)
class Config:
    root: Path
    settings_path: Path
    settings_exists: bool
    source: SourceCfg
    pricetoday: PricetodayCfg
    alanchand: AlanchandCfg
    pricing: PricingCfg
    guards: GuardsCfg
    hours: HoursCfg
    web: WebCfg
    paths: PathsCfg
    market: MarketCfg
    collector: CollectorCfg
    browser: BrowserCfg
    scout: ScoutCfg
    agents: AgentsCfg
    credit: CreditCfg
    advisor: AdvisorCfg
    llm: LlmCfg


def _int(sec, key, raw, lo=None, hi=None) -> int:
    try:
        v = int(str(raw).replace(",", "").replace("_", "").strip())
    except ValueError:
        raise ConfigError(f"[{sec}] {key} باید عدد صحیح باشد، نه «{raw}».") from None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError(f"[{sec}] {key} باید بین {lo} و {hi} باشد، نه {v}.")
    return v


def _dec(sec, key, raw, lo=None, hi=None) -> Decimal:
    try:
        v = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ConfigError(f"[{sec}] {key} باید عدد باشد، نه «{raw}».") from None
    if not v.is_finite():
        raise ConfigError(f"[{sec}] {key} باید عدد باشد، نه «{raw}».")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError(f"[{sec}] {key} باید بین {lo} و {hi} باشد، نه {raw}.")
    return v


def _bool(sec, key, raw) -> bool:
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on", "بله"):
        return True
    if s in ("0", "false", "no", "off", "خیر"):
        return False
    raise ConfigError(f"[{sec}] {key} باید true یا false باشد، نه «{raw}».")


def _time(sec, key, raw) -> time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(raw))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"[{sec}] {key} باید ساعت به شکل 09:00 باشد، نه «{raw}».")
    return time(int(m.group(1)), int(m.group(2)))


def _day(key, raw):
    s = str(raw).strip().lower()
    if s in ("closed", "تعطیل", "off", ""):
        return None
    parts = s.split("-")
    if len(parts) != 2:
        raise ConfigError(f"[hours] {key} باید به شکل 09:00-17:00 یا closed باشد، نه «{raw}».")
    o, c = _time("hours", key, parts[0]), _time("hours", key, parts[1])
    if c <= o:
        raise ConfigError(f"[hours] {key}: ساعت پایان باید بعد از ساعت شروع باشد.")
    return (o, c)


def _path(root: Path, raw: str) -> Path:
    p = Path(str(raw).strip())
    return p if p.is_absolute() else (root / p)


def _choice(sec, key, raw, allowed) -> str:
    s = str(raw).strip().lower()
    if s not in allowed:
        raise ConfigError(f"[{sec}] {key} باید یکی از {' یا '.join(allowed)} باشد، نه «{raw}».")
    return s


def _url(sec, key, raw) -> str:
    s = str(raw).strip()
    if not s.lower().startswith(("http://", "https://")):
        raise ConfigError(f"[{sec}] {key} باید با http:// یا https:// شروع شود، نه «{raw}».")
    return s


def default_settings_path() -> Path:
    env = os.environ.get("BORNA_SETTINGS")
    return Path(env).resolve() if env else APP_ROOT / "settings.ini"


def load_config(path: str | Path | None = None) -> Config:
    settings_path = Path(path).resolve() if path else default_settings_path()
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
    cp.read_dict(DEFAULTS)
    exists = settings_path.exists()
    if exists:
        try:
            with open(settings_path, encoding="utf-8-sig") as fh:
                cp.read_file(fh)
        except (configparser.Error, UnicodeDecodeError) as exc:
            raise ConfigError(f"فایل تنظیمات {settings_path} خوانا نیست (آن را با کدگذاری UTF-8 ذخیره کنید): {exc}") from None
    root = settings_path.parent

    s = cp["source"]
    source = SourceCfg(
        provider=_choice("source", "provider", s["provider"], PROVIDERS),
        mode=_choice("source", "mode", s["mode"], ("live", "file", "off")),
        timeout_seconds=_int("source", "timeout_seconds", s["timeout_seconds"], 1, 120),
    )

    t = cp["pricetoday"]
    pricetoday = PricetodayCfg(
        url=_url("pricetoday", "url", t["url"]),
        unit=_choice("pricetoday", "unit", t["unit"], ("rial", "toman")),
        sample_file=_path(root, t["sample_file"]),
    )

    a = cp["alanchand"]
    alan = AlanchandCfg(
        token=a["token"].strip(),
        url=_url("alanchand", "url", a["url"]),
        type=a["type"].strip(),
        slug=a["slug"].strip(),
        field=a["field"].strip(),
        unit=_choice("alanchand", "unit", a["unit"], ("toman", "rial")),
        sample_file=_path(root, a["sample_file"]),
        source_utc_offset_minutes=_int("alanchand", "source_utc_offset_minutes",
                                       a["source_utc_offset_minutes"], -720, 840),
    )

    p = cp["pricing"]
    basis = p["change_basis"].strip().lower()
    if basis not in ("exact", "rounded"):
        raise ConfigError("[pricing] change_basis باید exact یا rounded باشد.")
    try:
        base_date = jalali.parse_jalali(p["base_date"])
    except ValueError as exc:
        raise ConfigError(f"[pricing] base_date: {exc}") from None
    pricing = PricingCfg(
        base_rate_toman=_int("pricing", "base_rate_toman", p["base_rate_toman"], 1000, 100_000_000),
        base_date=base_date,
        base_date_text=jalali.jalali_str(base_date),
        base_time=_time("pricing", "base_time", p["base_time"]),
        alpha_default=_dec("pricing", "alpha_default", p["alpha_default"], Decimal(0), Decimal(1)),
        round_step_rial=_int("pricing", "round_step_rial", p["round_step_rial"], 1, 1_000_000_000),
        publish_threshold=_dec("pricing", "publish_threshold", p["publish_threshold"], Decimal(0), Decimal(1)),
        approval_threshold=_dec("pricing", "approval_threshold", p["approval_threshold"], Decimal(0), Decimal(1)),
        change_basis=basis,
        auto_publish=_bool("pricing", "auto_publish", p["auto_publish"]),
        auto_renew=_bool("pricing", "auto_renew", p["auto_renew"]),
        always_publish=_bool("pricing", "always_publish", p["always_publish"]),
    )

    g = cp["guards"]
    guards = GuardsCfg(
        fetch_interval_minutes=_int("guards", "fetch_interval_minutes", g["fetch_interval_minutes"], 1, 1440),
        retry_minutes=_int("guards", "retry_minutes", g["retry_minutes"], 1, 1440),
        max_age_minutes=_int("guards", "max_age_minutes", g["max_age_minutes"], 1, 100_000),
        jump_threshold=_dec("guards", "jump_threshold", g["jump_threshold"], Decimal(0), Decimal(1)),
        min_rate_toman=_int("guards", "min_rate_toman", g["min_rate_toman"], 1),
        max_rate_toman=_int("guards", "max_rate_toman", g["max_rate_toman"], 1),
    )
    if guards.min_rate_toman >= guards.max_rate_toman:
        raise ConfigError("[guards] min_rate_toman باید کمتر از max_rate_toman باشد.")

    h = cp["hours"]
    days = {wd: _day(k, h[k]) for k, wd in WEEKDAY_KEYS.items()}
    hol = set()
    for item in re.split(r"[,\s،]+", h["holidays"].strip()):
        if item:
            try:
                hol.add(jalali.parse_jalali(item))
            except ValueError as exc:
                raise ConfigError(f"[hours] holidays: {exc}") from None
    hours = HoursCfg(days=days, holidays=frozenset(hol))

    w = cp["web"]
    web = WebCfg(
        host=w["host"].strip(),
        port=_int("web", "port", w["port"], 1, 65535),
        company_name=w["company_name"].strip(),
        pricelist_title=w["pricelist_title"].strip(),
        validity_text=w["validity_text"].strip(),
        show_rate_on_pricelist=_bool("web", "show_rate_on_pricelist", w["show_rate_on_pricelist"]),
        footer_note=w["footer_note"].strip(),
        session_hours=_int("web", "session_hours", w["session_hours"], 1, 24 * 30),
        behind_https_proxy=_bool("web", "behind_https_proxy", w["behind_https_proxy"]),
    )

    pa = cp["paths"]
    paths = PathsCfg(
        database=_path(root, pa["database"]),
        products_xlsx=_path(root, pa["products_xlsx"]),
        log_file=_path(root, pa["log_file"]),
        secret_key_file=_path(root, pa["secret_key_file"]),
        evidence_dir=_path(root, pa["evidence_dir"]),
    )

    mk = cp["market"]
    tiers = []
    for item in re.split(r"[,،\s]+", mk["separate_tiers"].strip().lower()):
        if not item:
            continue
        if item not in TIER_KEYS:
            raise ConfigError(f"[market] separate_tiers فقط می‌تواند این‌ها باشد: {', '.join(TIER_KEYS)}؛ «{item}» شناخته نشد.")
        if item not in tiers:
            tiers.append(item)
    market = MarketCfg(
        own_sellers=tuple(x.strip() for x in re.split(r"[,،\n]+", mk["own_sellers"]) if x.strip()),
        min_price_usd=_dec("market", "min_price_usd", mk["min_price_usd"], Decimal("0.01"), Decimal(100000)),
        max_price_usd=_dec("market", "max_price_usd", mk["max_price_usd"], Decimal("0.01"), Decimal(100000)),
        jump_quarantine=_dec("market", "jump_quarantine", mk["jump_quarantine"], Decimal("0.05"), Decimal(10)),
        stale_days=_int("market", "stale_days", mk["stale_days"], 1, 365),
        length_tolerance_mm=_int("market", "length_tolerance_mm", mk["length_tolerance_mm"], 0, 500),
        separate_tiers=tuple(tiers),
        min_prices=_int("market", "min_prices", mk["min_prices"], 1, 50),
        outlier_high=_dec("market", "outlier_high", mk["outlier_high"], Decimal("1.1"), Decimal(100)),
        outlier_low=_dec("market", "outlier_low", mk["outlier_low"], Decimal("0.01"), Decimal("0.95")),
        auto_approve_strong=_bool("market", "auto_approve_strong", mk["auto_approve_strong"]),
    )
    if market.min_price_usd >= market.max_price_usd:
        raise ConfigError("[market] min_price_usd باید کمتر از max_price_usd باشد.")

    co = cp["collector"]
    ua = co["user_agent"].strip()
    if not ua or any(ch in ua for ch in "\r\n"):
        raise ConfigError("[collector] user_agent نباید خالی باشد.")
    collector = CollectorCfg(
        user_agent=ua,
        timeout_seconds=_int("collector", "timeout_seconds", co["timeout_seconds"], 1, 120),
        min_delay_seconds=_int("collector", "min_delay_seconds", co["min_delay_seconds"], 1, 600),
        max_bytes=_int("collector", "max_bytes", co["max_bytes"], 10_000, 50_000_000),
        max_upload_mb=_int("collector", "max_upload_mb", co["max_upload_mb"], 1, 9),
        respect_robots=_bool("collector", "respect_robots", co["respect_robots"]),
    )

    br = cp["browser"]
    channel = br["channel"].strip().lower()
    if channel not in ("msedge", "chrome", "chromium", ""):
        raise ConfigError(f"[browser] channel باید msedge، chrome یا chromium باشد، نه «{channel}».")
    exe = br["executable_path"].strip().strip('"')
    bua = br["user_agent"].strip()
    if any(ch in bua for ch in "\r\n"):
        raise ConfigError("[browser] user_agent نباید چند خط باشد.")
    browser = BrowserCfg(
        enabled=_bool("browser", "enabled", br["enabled"]),
        channel=channel or "msedge",
        executable_path=str((root / exe).resolve()) if exe and not Path(exe).is_absolute() else exe,
        user_agent=bua,
        timeout_seconds=_int("browser", "timeout_seconds", br["timeout_seconds"], 5, 300),
        max_scrolls=_int("browser", "max_scrolls", br["max_scrolls"], 0, 50),
        scroll_pause_ms=_int("browser", "scroll_pause_ms", br["scroll_pause_ms"], 100, 20_000),
        max_mb=_int("browser", "max_mb", br["max_mb"], 1, 64),
    )

    sc = cp["scout"]
    known_sites = ("torob", "emalls", "digikala", "basalam", "divar")
    sites = []
    for item in re.split(r"[,،\s]+", sc["sites"].strip().lower()):
        if not item:
            continue
        if item not in known_sites:
            raise ConfigError(f"[scout] sites فقط می‌تواند این‌ها باشد: {', '.join(known_sites)}؛ «{item}» شناخته نشد.")
        if item not in sites:
            sites.append(item)
    region = sc["divar_region"].strip().lower() or "iran"
    if not re.fullmatch(r"[a-z0-9\-]{2,40}", region):
        raise ConfigError(f"[scout] divar_region باید نام شهر در نشانی دیوار باشد (مثل iran یا tehran)، نه «{region}».")

    def _site_map(key, raw, lo, hi):
        out = []
        for part in re.split(r"[,،\n]+", raw or ""):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"([a-z]+)\s*[:=]\s*(\S+)", part.lower())
            if not m or m.group(1) not in known_sites:
                raise ConfigError(f"[scout] {key} باید به شکل «torob: 15» و با نام یکی از سایت‌های "
                                  f"{', '.join(known_sites)} باشد؛ «{part}» درست نیست.")
            out.append((m.group(1), _int("scout", key, m.group(2), lo, hi)))
        return tuple(out)

    def _osm_urls(raw):
        out = []
        for u in re.split(r"[,،\s]+", raw or ""):
            if not u:
                continue
            parts = urllib.parse.urlsplit(u)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise ConfigError(f"[scout] osm_urls باید نشانی https سرویس Overpass باشد، نه «{u}».")
            if u not in out:
                out.append(u)
        return tuple(out)

    def _words(raw):
        out = []
        for w in re.split(r"[,،\n]+", raw):
            w = re.sub(r"\s+", " ", w).strip()
            if w and len(w) <= 80 and w not in out:
                out.append(w)
        return tuple(out)

    scout = ScoutCfg(
        enabled=_bool("scout", "enabled", sc["enabled"]),
        sites=tuple(sites),
        interval_hours=_int("scout", "interval_hours", sc["interval_hours"], 1, 24 * 30),
        queries_per_site=_int("scout", "queries_per_site", sc["queries_per_site"], 1, 100),
        pages_per_query=_int("scout", "pages_per_query", sc["pages_per_query"], 1, 10),
        product_pages=_int("scout", "product_pages", sc["product_pages"], 0, 200),
        delay_seconds=_int("scout", "delay_seconds", sc["delay_seconds"], 1, 600),
        extra_keywords=_words(sc["extra_keywords"]),
        repair_keywords=_words(sc["repair_keywords"]),
        divar_region=region,
        site_delays=_site_map("site_delay_seconds", sc["site_delay_seconds"], 1, 600),
        site_queries=_site_map("site_queries", sc["site_queries"], 1, 100),
        osm=_bool("scout", "osm", sc["osm"]),
        osm_days=_int("scout", "osm_days", sc["osm_days"], 1, 365),
        osm_urls=_osm_urls(sc["osm_urls"]),
    )

    ag = cp["agents"]
    wd = ag["report_weekday"].strip().lower()
    if wd not in WEEKDAY_KEYS:
        raise ConfigError(f"[agents] report_weekday باید نام انگلیسی یک روز هفته باشد (مثل saturday)، نه «{wd}».")
    agents = AgentsCfg(
        enabled=_bool("agents", "enabled", ag["enabled"]),
        poll_seconds=_int("agents", "poll_seconds", ag["poll_seconds"], 5, 3600),
        report_weekday=WEEKDAY_KEYS[wd],
        report_time=_time("agents", "report_time", ag["report_time"]),
    )

    def _opt_dec(sec, key, raw, lo, hi):
        return _dec(sec, key, raw, lo, hi) if str(raw).strip() else None

    cr = cp["credit"]
    days = []
    for item in re.split(r"[,\s،]+", cr["check_days"].strip()):
        if item:
            days.append(_int("credit", "check_days", item, 1, 365))
    credit = CreditCfg(
        annual_cost_of_money=_opt_dec("credit", "annual_cost_of_money", cr["annual_cost_of_money"], Decimal(0), Decimal(10)),
        annual_dollar_change=_opt_dec("credit", "annual_dollar_change", cr["annual_dollar_change"], Decimal(-1), Decimal(10)),
        check_days=tuple(sorted(set(days))) or (30, 45, 60, 90),
    )
    ad = cp["advisor"]
    advisor = AdvisorCfg(
        target_band=_dec("advisor", "target_band", ad["target_band"], Decimal(0), Decimal("0.5")),
        min_margin=_opt_dec("advisor", "min_margin", ad["min_margin"], Decimal(0), Decimal("0.95")),
        min_prices=_int("advisor", "min_prices", ad["min_prices"], 1, 50),
    )
    lm = cp["llm"]
    base_url = lm["base_url"].strip().rstrip("/")
    if base_url:
        base_url = _url("llm", "base_url", base_url)
    llm = LlmCfg(
        enabled=_bool("llm", "enabled", lm["enabled"]),
        base_url=base_url,
        model=lm["model"].strip(),
        api_key=lm["api_key"].strip(),
        timeout_seconds=_int("llm", "timeout_seconds", lm["timeout_seconds"], 1, 600),
        daily_call_limit=_int("llm", "daily_call_limit", lm["daily_call_limit"], 0, 100_000),
        max_input_chars=_int("llm", "max_input_chars", lm["max_input_chars"], 200, 100_000),
    )
    return Config(root=root, settings_path=settings_path, settings_exists=exists, source=source,
                  pricetoday=pricetoday, alanchand=alan, pricing=pricing, guards=guards, hours=hours, web=web,
                  paths=paths, market=market, collector=collector, browser=browser, scout=scout, agents=agents,
                  credit=credit, advisor=advisor, llm=llm)
