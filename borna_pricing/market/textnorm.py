from __future__ import annotations

import re

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
LETTERS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "ٱ": "ا",
                         "ـ": "", "‌": " ", "‍": "", "‏": "", "‎": "", "\xa0": " "})
_MARKS = re.compile(r"[ً-ٰٟ]")

ECU_LABELS = {"siemens": "زیمنس", "sagem": "ساژم", "me17": "ME17", "me7": "ME7", "re94": "RE94"}
COLOR_LABELS = {"gray": "طوسی", "black": "مشکی", "green": "سبز", "blue": "آبی", "yellow": "زرد",
                "white": "سفید", "red": "قرمز"}
DESIGN_LABELS = {"ntk": "NTK", "bosch": "Bosch", "delphi": "Delphi", "denso": "Denso"}
TIER_LABELS = {"genuine": "اصلی", "oem": "شرکتی / فابریک", "aftermarket": "طرح / غیراصلی", "unknown": "نامشخص"}
POSITION_LABELS = {"upstream": "بالایی", "downstream": "پایینی"}
LENGTH_CLASS_LABELS = {"short": "کوتاه", "medium": "متوسط", "long": "بلند"}

_COLOR_WORDS = {
    "طوسی": "gray", "خاکستری": "gray", "gray": "gray", "grey": "gray",
    "مشکی": "black", "سیاه": "black", "black": "black",
    "سبز": "green", "green": "green",
    "آبی": "blue", "ابی": "blue", "blue": "blue",
    "زرد": "yellow", "yellow": "yellow",
    "سفید": "white", "white": "white",
    "قرمز": "red", "red": "red",
}
_COLOR_RE = "|".join(sorted(_COLOR_WORDS, key=len, reverse=True))
_CLASS_WORDS = {"کوتاه": "short", "متوسط": "medium", "بلند": "long"}

_DESIGN_PATTERNS = [
    ("ntk", r"ntk|ngk|ان\s?تی\s?کی"),
    ("bosch", r"bosch|بوش"),
    ("delphi", r"delphi|دلفی"),
    ("denso", r"denso|دنسو"),
]
_OTHER_BRANDS = [
    ("Crouse", r"crouse|کروز"),
    ("Isaco", r"isaco|ایساکو"),
    ("Saipa Yadak", r"سایپا\s?یدک"),
    ("Ezam", r"ezam|عظام"),
]
_ECU_PATTERNS = [
    ("siemens", r"siemens|زیمنسی|زیمنس"),
    ("sagem", r"sagem|ساژمی|ساژم"),
    ("me17", r"me\s?-?\s?17|ام\s?ای\s?(?:17|هفده)"),
    ("me7", r"me\s?-?\s?7(?:\.\d+)*|ام\s?ای\s?(?:7|هفت)"),
    ("re94", r"re\s?94"),
]
VEHICLES = [
    ("pride", "پراید", r"پراید|pride"),
    ("tiba", "تیبا", r"تیبا|tiba"),
    ("saina", "ساینا", r"ساینا|saina"),
    ("quick", "کوییک", r"کوییک|کوئیک|quick"),
    ("shahin", "شاهین", r"شاهین"),
    ("rana", "رانا", r"رانا|rana"),
    ("p206", "پژو ۲۰۶", r"206"),
    ("p207", "پژو ۲۰۷", r"207"),
    ("p405", "پژو ۴۰۵", r"405"),
    ("pars", "پارس", r"پارس|pars"),
    ("samand", "سمند", r"سمند|samand"),
    ("dena", "دنا", r"دنا|dena"),
    ("soren", "سورن", r"سورن"),
    ("peykan", "پیکان", r"پیکان|peykan"),
    ("l90", "L90 / تندر", r"l\s?90|ال\s?90|تندر"),
    ("ef7", "موتور EF7", r"ef\s?7|ای\s?اف\s?7"),
    ("tu5", "موتور TU5", r"tu\s?5"),
    ("lifan", "لیفان", r"لیفان|lifan"),
    ("changan", "چانگان", r"چانگان|changan|cs\s?35"),
    ("jac", "جک", r"جک|jac|j\s?5"),
    ("mvm", "ام‌وی‌ام", r"mvm|ام\s?وی\s?ام"),
    ("arrizo", "آریزو", r"آریزو|اریزو|arrizo"),
    ("x22", "X22", r"x\s?22"),
    ("haima", "هایما", r"هایما|haima"),
]
VEHICLE_LABELS = {code: label for code, label, _ in VEHICLES}

_O2_WORDS = re.compile(r"اکسیژن|لامبدا|oxygen|lambda|o2\s?sensor|\bo2\b")
_NOT_SENSOR = re.compile(r"بکس|آچار|اچار|ابزار|wrench|\btool\b|شبیه\s?ساز|emulator|simulator|رابط|آداپتور|"
                         r"adapter|دسته\s?سیم|دریچه\s?گاز|میل\s?سوپاپ|دمای\s?آب|ضربه|کیلومتر")
_TIER_AFTER = re.compile(r"(?<!\w)طرح(?!\w)|های\s?کپی|high\s?copy|درجه\s?(?:یک|1)(?!\d)|(?<!\w)مشابه(?!\w)")
_TIER_OEM = re.compile(r"(?<!\w)(?:شرکتی|فابریک)(?!\w)")
_TIER_GENUINE = re.compile(r"(?<!\w)(?:اصل|اصلی|اورجینال|اوریجینال|اورجینالی|original|orginal|genuine)(?!\w)")
_POS_UP = re.compile(r"(?<!\w)(?:بالایی|بالائی|upstream)(?!\w)")
_POS_DOWN = re.compile(r"(?<!\w)(?:پایینی|پائینی|downstream)(?!\w)")
_NEW_GEN = re.compile(r"نسل\s?جدید")
_LEN_CM = re.compile(r"(\d{1,3}(?:[.٫/]\d)?)\s*(?:سانتی\s?متر|سانتیمتر|سانتی|سانت|cm)(?!\w)")
_LEN_MM = re.compile(r"(\d{2,4})\s*(?:میلی\s?متر|میلیمتر|mm)(?!\w)")
_LEN_CLASS = re.compile(r"سیم\s+(کوتاه|متوسط|بلند)")
_WIRE_COLOR = re.compile(rf"سیم\s+(?:(?:کوتاه|متوسط|بلند)\s+)?({_COLOR_RE})(?!\w)")
_SOCKET_COLOR = re.compile(rf"(?:سوکت|کانکتور|فیش)\s+({_COLOR_RE})(?!\w)")
_WIRE_COUNT_DIGIT = re.compile(r"(?<![\w.])([1-5])\s*(?:سیم|wires?)(?!\w)")
_WIRE_COUNT_WORDS = {"تک": 1, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5}
_WIRE_COUNT_WORD = re.compile(r"(?<!\w)(تک|یک|دو|سه|چهار|پنج)\s?سیم(?!\w)")

_PN_OZA = re.compile(r"(?<![A-Z0-9])OZA\s?-?\s?(\d{3})\s?-?\s?([A-Z]{1,3}\d{1,2})(?![A-Z0-9])")
_PN_BOSCH = re.compile(r"(?<![\d])0?\s?258[\s.\-]?(\d{3})[\s.\-]?(\d{3})(?![\d])")
_PN_SMW = re.compile(r"(?<![A-Z0-9])SMW\s?-?\s?(\d{6})(?![A-Z0-9])")
_PN_GENERIC = re.compile(r"(?<![A-Z0-9])([A-Z]{1,3}\d{8,10})(?![A-Z0-9])")


def latin_digits(text) -> str:
    return str(text or "").translate(DIGITS)


PACK_MAX = 500
_PACK_COUNT = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:عددی|عدد|تایی|pcs|pc|pieces?)(?!\w)", re.I)
_PACK_BOX = re.compile(r"(?<!\w)(?:پک|بسته|کارتن|جعبه|pack(?:\s+of)?|set\s+of)\s*(\d{1,3})(?!\d)", re.I)
_PACK_WORD = re.compile(r"(?<!\w)(دو|سه|چهار|پنج|شش|ده)\s*(?:عددی|تایی)(?!\w)")
_PACK_WORDS = {"دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "ده": 10}
_PACK_PAIR = re.compile(r"(?<!\w)جفت(?!\w)(?!\s*(?:سیم|فیش|سوکت|کانکتور|پین))")


_NOT_PACK_BEFORE = re.compile(r"(?:حداقل|کمترین|حداکثر|موجودی|فقط|تنها|min(?:imum)?|moq|max)\W*(?:\w+\W+){0,2}$", re.I)
_NOT_PACK_AFTER = re.compile(r"^\W*(?:\w+\W+)?(?:باقی|موجود|مانده|در انبار|in stock|left)", re.I)


def _is_pack(s: str, m) -> bool:
    return not (_NOT_PACK_BEFORE.search(s[max(0, m.start() - 30):m.start()])
                or _NOT_PACK_AFTER.search(s[m.end():m.end() + 25]))


_WHOLESALE = re.compile(r"(?<!\w)(?:عمده|عمده‌فروشی|عمده فروشی|بنکدار|بنکداری|پخش عمده|wholesale)(?!\w)", re.I)
_SINGLE = re.compile(r"(?<!\w)(?:تکی|جزئی|خرده|خرده‌فروشی|تک فروشی|retail)(?!\w)", re.I)


def level_hint(text) -> str | None:
    s = norm(text)
    if _WHOLESALE.search(s) and not _SINGLE.search(s):
        return "wholesale"
    return None


def pack_qty(text) -> int:
    s = norm(text)
    found = [int(m.group(1)) for rx in (_PACK_BOX, _PACK_COUNT) for m in rx.finditer(s) if _is_pack(s, m)]
    found += [_PACK_WORDS[m.group(1)] for m in _PACK_WORD.finditer(s) if _is_pack(s, m)]
    if _PACK_PAIR.search(s):
        found.append(2)
    found = [n for n in found if 2 <= n <= PACK_MAX]
    return max(found) if found else 1


def norm(text) -> str:
    s = _MARKS.sub("", latin_digits(text).translate(LETTERS)).lower()
    return re.sub(r"\s+", " ", s).strip()


_PHONE_PARTS = re.compile(r"[;,،؛/|\n]+|\s+[-–]\s+|\s+(?:و|یا|or|and)\s+", re.I)
_PHONE_EXT = re.compile(r"داخلی|ext", re.I)


def phones(text, limit: int = 3) -> str | None:
    out: list[str] = []
    for part in _PHONE_PARTS.split(latin_digits(text)):
        part = _PHONE_EXT.split(part)[0].strip()
        plus = part.lstrip("( ").startswith("+")
        digits = re.sub(r"\D", "", part)
        if digits.startswith("0098"):
            digits = "0" + digits[4:]
        elif digits.startswith("98") and (plus or len(digits) == 12):
            digits = "0" + digits[2:]
        if not 7 <= len(digits) <= 15:
            continue
        digits = "+" + digits if plus and not digits.startswith("0") else digits
        if digits not in out:
            out.append(digits)
    return "، ".join(out[:limit]) or None


def tokens_text(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[()\[\]{}«»\"'،,؛;:!?|*+]", " ", norm(text))).strip()


def normalize_pn(raw) -> str | None:
    if raw is None:
        return None
    s = latin_digits(raw).upper().strip()
    if not s:
        return None
    m = _PN_OZA.fullmatch(s.replace(" ", ""))
    if m:
        return f"OZA{m.group(1)}-{m.group(2)}"
    compact = re.sub(r"[\s.\-]", "", s)
    if re.fullmatch(r"0?258\d{6}", compact):
        return compact.rjust(10, "0")
    m = _PN_SMW.fullmatch(compact)
    if m:
        return f"SMW{m.group(1)}"
    return compact or None


def find_part_numbers(text) -> list[str]:
    s = latin_digits(text).upper()
    found: list[str] = []

    def add(pn):
        if pn and pn not in found:
            found.append(pn)

    for m in _PN_OZA.finditer(s):
        add(f"OZA{m.group(1)}-{m.group(2)}")
    for m in _PN_BOSCH.finditer(s):
        add(f"0258{m.group(1)}{m.group(2)}")
    for m in _PN_SMW.finditer(s):
        add(f"SMW{m.group(1)}")
    for m in _PN_GENERIC.finditer(s):
        add(m.group(1))
    return found


def _first(patterns, text) -> str | None:
    for code, pat in patterns:
        if re.search(rf"(?<!\w)(?:{pat})(?!\w)", text):
            return code
    return None


def find_design(text) -> str | None:
    return _first(_DESIGN_PATTERNS, norm(text))


def find_brand(text) -> str | None:
    t = norm(text)
    d = _first(_DESIGN_PATTERNS, t)
    if d:
        return DESIGN_LABELS[d]
    return _first(_OTHER_BRANDS, t)


def find_ecus(text) -> list[str]:
    t = norm(text)
    return [code for code, pat in _ECU_PATTERNS if re.search(rf"(?<!\w)(?:{pat})(?!\w)", t)]


def color_code(text) -> str | None:
    t = norm(text)
    if not t:
        return None
    if t in _COLOR_WORDS:
        return _COLOR_WORDS[t]
    for word, code in _COLOR_WORDS.items():
        if re.search(rf"(?<!\w){word}(?!\w)", t):
            return code
    return None


def find_vehicles(text) -> list[str]:
    t = norm(text)
    return [code for code, _, pat in VEHICLES if re.search(rf"(?<![\w]){pat}(?![\w])", t)]


def find_tier(text) -> str:
    t = norm(text)
    if _TIER_AFTER.search(t):
        return "aftermarket"
    if _TIER_OEM.search(t):
        return "oem"
    if _TIER_GENUINE.search(t):
        return "genuine"
    return "unknown"


def find_length(text) -> tuple[int | None, str | None]:
    t = norm(text)
    mm = None
    m = _LEN_MM.search(t)
    if m:
        mm = int(m.group(1))
    else:
        m = _LEN_CM.search(t)
        if m:
            v = float(m.group(1).replace("٫", ".").replace("/", "."))
            if 5 <= v <= 300:
                mm = int(round(v * 10))
    if mm is not None and not 50 <= mm <= 3000:
        mm = None
    c = _LEN_CLASS.search(t)
    if c:
        return mm, _CLASS_WORDS[c.group(1)]
    t2 = t.strip()
    return mm, _CLASS_WORDS.get(t2)


def find_wire_count(text) -> int | None:
    t = norm(text)
    m = _WIRE_COUNT_DIGIT.search(t)
    if m:
        return int(m.group(1))
    m = _WIRE_COUNT_WORD.search(t)
    return _WIRE_COUNT_WORDS[m.group(1)] if m else None


def find_position(text) -> str | None:
    t = norm(text)
    up, down = bool(_POS_UP.search(t)), bool(_POS_DOWN.search(t))
    if up == down:
        return None
    return "upstream" if up else "downstream"


def is_o2_sensor(text) -> bool:
    t = norm(text)
    return bool(_O2_WORDS.search(t)) and not _NOT_SENSOR.search(t)


def parse_title(text) -> dict:
    t = tokens_text(text)
    ecus = find_ecus(t)
    mm, cls = find_length(t)
    sock = _SOCKET_COLOR.search(t)
    wire = _WIRE_COLOR.search(t)
    out = {
        "family": "o2" if is_o2_sensor(text) else "other",
        "part_nos": find_part_numbers(text),
        "design": find_design(t),
        "brand": find_brand(t),
        "tier": find_tier(t),
        "ecus": ecus,
        "ecu": ecus[0] if len(ecus) == 1 else None,
        "socket_color": _COLOR_WORDS[sock.group(1)] if sock else None,
        "wire_color": _COLOR_WORDS[wire.group(1)] if wire else None,
        "length_mm": mm,
        "length_class": cls,
        "wire_count": find_wire_count(t),
        "position": find_position(t),
        "generation": "new" if _NEW_GEN.search(t) else None,
        "vehicles": find_vehicles(t),
    }
    return out
