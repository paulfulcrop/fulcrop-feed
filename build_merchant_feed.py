#!/usr/bin/env python3
"""
Build a Google Merchant Center product feed (TSV) from the WooCommerce Store API.

Usage (on your Mac, from ~/seo-bot):
    python3 build_merchant_feed.py                 # fetch live from fulcropsciences.com
    python3 build_merchant_feed.py --from-json products.json   # use a saved snapshot

Output: merchant_feed.tsv (upload in Merchant Center > Products > Add products > Add from file)
No extra packages needed (standard library only).
"""
import argparse, csv, html, json, re, sys, urllib.request
from urllib.parse import quote

SITE = "https://fulcropsciences.com"
DEFAULT_BRAND = "FulCrop Sciences"

# ---- Per-product settings (keyed by WooCommerce product ID) -----------------
# title: base title used for every variant; variant values get appended.
# brand / category override the defaults.
FERT = "Home & Garden > Lawn & Garden > Gardening > Fertilizers"
PRODUCTS = {
    13582: dict(title="FulCrop Sciences Humate Fertilizer", category=FERT),
    13758: dict(title="FulCrop Sciences Soil Health Bundle", category=FERT, is_bundle=True),
    15362: dict(title="FulCrop Sciences Soil Amendment", category=FERT),
    15553: dict(title="FulCrop Sciences Beneficial Spores Microbial Inoculant", category=FERT),
    156:   dict(title="FulCrop Sciences Probiotic Microbial+ Soil Inoculant", category=FERT),
    38:    dict(title="FulCrop Sciences IQ+ Cold Pressed Quillaja Natural Surfactant", category=FERT),
    16248: dict(title="ESTTECH LED Grow Light", brand="ESTTECH"),
}

# Left out of the feed, with the reason.
EXCLUDE_IDS = {
    12747: "sticker listing mentions cannabis strain names (Google Shopping policy risk)",
}
EXCLUDE_VARIANTS = {
    24006: "lighting controller has no product photo (only the logo)",
}
# Full title overrides for single variants whose option names read awkwardly.
VARIANT_TITLES = {
    15559: "FulCrop Sciences Beneficial Spores Duo – Bacillus + Trichoderma, 1 Vial Each",
    15560: "FulCrop Sciences Beneficial Spores Duo – Bacillus + Trichoderma, 1 Case Each",
}
# Only products in these WooCommerce categories go into the feed. Anything else
# (lighting, clothing/merch, new categories added later) is left out automatically.
INCLUDE_CATEGORIES = {"Humate Fertilizer", "Microbes", "Spores", "Amendments", "IQ", "Bundles"}
# Images whose file name contains any of these are skipped (logos aren't product photos).
SKIP_IMAGE_WORDS = ["logo"]
# Phrases removed from descriptions (they don't make sense outside the product page).
REMOVE_PHRASES = ["Check out the Product Sheets HERE."]

HEADERS = ["id", "item_group_id", "title", "description", "link", "image_link",
           "additional_image_link", "availability", "price", "sale_price", "condition",
           "brand", "gtin", "identifier_exists", "google_product_category",
           "product_type", "is_bundle", "shipping_weight", "checkout_link_template"]


# ---- Helpers ----------------------------------------------------------------
def clean_text(h):
    h = re.sub(r"</(p|li|h\d|div)>|<br\s*/?>", ". ", h or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(\s*\.\s*){2,}", ". ", t)
    t = re.sub(r"\?\.", "?", t).replace("!.", "!").replace(":.", ":")
    return t.strip(" .") + ("." if t.strip(" .") else "")

def trim(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > limit * 0.5 else cut.rsplit(" ", 1)[0] + "…"

def money(minor, unit=2):
    return f"{int(minor) / 10**unit:.2f} USD"

def valid_gtin(code):
    code = (code or "").strip()
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]

def attr_param(name):
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return "attribute_" + slug

def tidy_value(v):
    # "HIGH BAY" -> "High Bay", keeps model codes like "GH001" as-is
    return " ".join(w.capitalize() if w.isalpha() and w.isupper() and len(w) > 2 else w
                    for w in str(v).split())

def good_images(urls):
    return [u for u in urls if not any(w in u.rsplit("/", 1)[-1].lower() for w in SKIP_IMAGE_WORDS)]


# ---- Data loading -----------------------------------------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FulCrop-feed-builder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def fetch_weights(url):
    """Weights (lb) from the product page: per variation, plus the product's own weight."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FulCrop-feed-builder/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  warning: couldn't read weights from {url}: {e}")
        return {}
    weights = {}
    m = re.search(r'data-product_variations="([^"]*)"', page)
    if m:
        try:
            for v in json.loads(html.unescape(m.group(1))) or []:
                if v.get("weight"):
                    weights[v["variation_id"]] = v["weight"]
        except ValueError:
            pass
    m = re.search(r'woocommerce-product-attributes-item--weight.*?<td[^>]*>(.*?)</td>', page, re.S)
    if m:
        w = re.sub(r"<[^>]+>|[^\d.]", "", m.group(1))
        if w:
            weights["parent"] = w
    return weights

def load_live():
    products, page = [], 1
    while True:
        batch = fetch_json(f"{SITE}/wp-json/wc/store/v1/products?per_page=100&page={page}")
        products += batch
        if len(batch) < 100:
            break
        page += 1
    out = []
    for p in products:
        item = dict(
            id=p["id"], name=html.unescape(p["name"]), type=p["type"], link=p["permalink"],
            sku=p.get("sku", ""), price=p["prices"]["regular_price"],
            sale=p["prices"]["sale_price"] if p.get("on_sale") else "",
            unit=p["prices"].get("currency_minor_unit", 2),
            images=[i["src"] for i in p.get("images", [])],
            categories=[html.unescape(c["name"]) for c in p.get("categories", [])],
            in_stock=p.get("is_in_stock", True),
            short=p.get("short_description", ""), description=p.get("description", ""),
            variations=[])
        weights = fetch_weights(p["permalink"])
        item["weight"] = weights.get("parent", "")
        for v in p.get("variations", []):
            d = fetch_json(f"{SITE}/wp-json/wc/store/v1/products/{v['id']}")
            item["variations"].append(dict(
                id=d["id"], sku=d.get("sku", ""), price=d["prices"]["regular_price"],
                sale=d["prices"]["sale_price"] if d.get("on_sale") else "",
                image=(d.get("images") or [{}])[0].get("src", ""),
                in_stock=d.get("is_in_stock", True),
                weight=weights.get(d["id"], weights.get("parent", "")),
                attributes={a["name"]: a["value"] for a in v.get("attributes", [])}))
        out.append(item)
    return out


# ---- Feed building ----------------------------------------------------------
def build_rows(products):
    rows, skipped = [], []
    for p in products:
        if p["id"] in EXCLUDE_IDS:
            skipped.append((p["name"], EXCLUDE_IDS[p["id"]])); continue
        if not any(c in INCLUDE_CATEGORIES for c in p["categories"]):
            skipped.append((p["name"], "category not in INCLUDE_CATEGORIES: " + ", ".join(p["categories"]))); continue

        cfg = PRODUCTS.get(p["id"], {})
        base_title = cfg.get("title", p["name"])
        brand = cfg.get("brand", DEFAULT_BRAND)
        unit = p.get("unit", 2)
        desc = clean_text(p["description"]) or clean_text(p["short"])
        short = clean_text(p["short"])
        if short and short not in desc:
            desc = short + " " + desc
        for phrase in REMOVE_PHRASES:
            desc = desc.replace(phrase, "")
        desc = trim(re.sub(r"\s+", " ", desc).strip(), 5000)
        parent_imgs = good_images(p["images"])

        variants = p["variations"] or [dict(id=p["id"], sku=p["sku"], price=p["price"],
                                            sale=p["sale"], image="", in_stock=p["in_stock"],
                                            weight=p.get("weight", ""), attributes={})]
        for v in variants:
            if not v["price"] or int(v["price"]) == 0:
                skipped.append((f'{p["name"]} (variant {v["id"]})', "price is $0")); continue
            if v["id"] in EXCLUDE_VARIANTS:
                skipped.append((f'{p["name"]} (variant {v["id"]})', EXCLUDE_VARIANTS[v["id"]])); continue
            values = [tidy_value(val) for val in v["attributes"].values() if val]
            values = [val.replace(brand + " ", "") if base_title.startswith(brand) else val for val in values]
            title = VARIANT_TITLES.get(v["id"]) or base_title + (" – " + ", ".join(values) if values else "")
            link = p["link"]
            params = [f"{attr_param(k)}={quote(val)}" for k, val in v["attributes"].items() if val]
            if params:
                link += ("&" if "?" in link else "?") + "&".join(params)
            imgs = good_images([v["image"]]) + [i for i in parent_imgs if i != v["image"]]
            if not imgs:
                skipped.append((title, "no usable image")); continue
            gtin = v["sku"] if valid_gtin(v["sku"]) else ""
            rows.append({
                "id": v["sku"] if v["sku"] and not gtin else f"fcs_{v['id']}",
                "item_group_id": f"fcs_{p['id']}" if p["variations"] else "",
                "title": trim(title, 150),
                "description": desc,
                "link": link,
                "image_link": imgs[0],
                "additional_image_link": ",".join(imgs[1:11]),
                "availability": "in_stock" if v["in_stock"] else "out_of_stock",
                "price": money(v["price"], unit),
                "sale_price": money(v["sale"], unit) if v["sale"] and v["sale"] != v["price"] else "",
                "condition": "new",
                "brand": brand,
                "gtin": gtin,
                "identifier_exists": "yes" if gtin else "no",
                "google_product_category": cfg.get("category", ""),
                "product_type": " > ".join(p["categories"]),
                "is_bundle": "yes" if cfg.get("is_bundle") else "",
                "shipping_weight": f"{float(v['weight']):g} lb" if v.get("weight") else "",
                # "Buy" button in Google listings: adds this exact size to the cart and opens checkout.
                # {id} is required by Google; WooCommerce ignores the extra gmc_id parameter.
                "checkout_link_template": f"{SITE}/checkout/?add-to-cart={v['id']}&quantity=1&gmc_id={{id}}",
            })
    return rows, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="use a saved product snapshot instead of fetching live")
    ap.add_argument("--save-json", help="also save the fetched products to this file")
    ap.add_argument("-o", "--output", default="merchant_feed.tsv")
    a = ap.parse_args()

    products = json.load(open(a.from_json)) if a.from_json else load_live()
    if a.save_json:
        json.dump(products, open(a.save_json, "w"), indent=1)
    rows, skipped = build_rows(products)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS, delimiter="\t", lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} items to {a.output}")
    print(f"  with GTIN: {sum(1 for r in rows if r['gtin'])}")
    for r in rows:
        if not r["shipping_weight"]:
            print(f"  warning: no weight for {r['title']} (set it in WooCommerce)")
    for name, why in skipped:
        print(f"  skipped: {name} — {why}")

if __name__ == "__main__":
    main()
