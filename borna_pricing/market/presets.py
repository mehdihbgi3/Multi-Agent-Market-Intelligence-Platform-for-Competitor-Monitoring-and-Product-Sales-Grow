from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    label: str
    regex: str
    verified: bool


PRESETS: dict[str, Preset] = {
    "prestashop": Preset(
        "پرستاشاپ",
        r'data-id-product="(?P<id>\d+)"(?:(?!</article>).)*?<h3[^>]*>\s*<a\s+href="(?P<url>[^"]+)"[^>]*>'
        r'(?P<title>[^<]+)</a>(?:(?!</article>).)*?class="price[^"]*"[^>]*>(?P<price>[^<]+)<',
        True),
    "woocommerce": Preset(
        "ووکامرس",
        r'<li\b[^>]*\bclass="[^"]*\bproduct\b[^"]*\bpost-(?P<id>\d+)\b[^"]*"[^>]*>'
        r'(?:(?!</li>).)*?<a\s+href="(?P<url>[^"]+)"[^>]*\bwoocommerce-LoopProduct-link\b[^>]*>'
        r'(?:(?!</li>).)*?<h2[^>]*\bwoocommerce-loop-product__title\b[^>]*>(?P<title>[^<]+)</h2>'
        r'(?:(?!</li>).)*?<span[^>]*\bclass="price[^"]*"[^>]*>'
        r'(?:(?!</li>).)*?\bwoocommerce-Price-amount\b[^>]*>(?P<price>(?:(?!</li>|</bdi>|</del>|<ins\b).)+?)'
        r'(?:</bdi>|</span>(?=</span>|</del>|</ins>))(?!(?:</span>)?\s*</del>)',
        False),
}
