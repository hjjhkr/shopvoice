"""Amazon Reviews 2023 (McAuley Lab), Health & Household -> Household Supplies
-> data/processed/products.parquet in the project's target schema.

    python -m voice_agent.data.download_hh2023    # stream the category metadata, keep Household Supplies (~3 min)
    python -m voice_agent.data.ingest             # build products.parquet from it

What the source really contains (metadata crawled in 2023; reviews May 1996 - Sep 2023):
  * title, price (USD at crawl time, about 40% of Health & Household items have one), store, categories,
    features (bullet list), description, details (a dict: Brand, Manufacturer, Scent, Item Form,
    Item Volume, Unit Count, Special Ingredients, Material, ...), average_rating, rating_number, parent_asin
  * the download step keeps only Household Supplies items with a price, a title and >= 10 ratings

Mapping, and what is derived rather than given:
  doc_id / sku   "hh23-" + parent_asin / parent_asin; url is the amazon.com/dp/<ASIN> page
  brand          details.Brand > details.Manufacturer > store (the Amazon storefront name); brand_source says which
  rating         average_rating (1-5), rating_count = rating_number: both from the dataset, never estimated
  ingredients    details keys that name ingredients (rare: most cleaning listings name none), otherwise real
                 materials ("bamboo, nylon") from the Material fields, title and features
  excluded       "Tobacco-Related Products" (age-restricted), although Amazon files them under Household Supplies
  claims         the listing's own claims found in title/features ("plant-based", "fragrance-free", ...); they are
                 the seller's wording, not verified facts, and are labelled that way downstream
  size / price_per_oz / price_per_count   parsed from Item Volume, Item Weight, Unit Count, Size
  recommended_age  details "Age Range (Description)" when present (rare in this category)
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from ..config import get_settings

SOURCE = "amazon-reviews-2023/health-and-household"

MATERIALS = [
    "stainless steel", "plastic", "bpa-free", "bpa free", "wood", "wooden", "bamboo", "cotton", "microfiber",
    "polyester", "silicone", "rubber", "foam", "metal", "aluminum", "steel", "glass", "ceramic", "nylon",
    "paper", "cellulose", "latex", "wool", "felt", "resin", "vinyl",
]
CLAIMS = [
    "plant-based", "plant based", "plant-derived", "biodegradable", "non-toxic", "nontoxic", "fragrance-free",
    "fragrance free", "unscented", "dye-free", "dye free", "phosphate-free", "phosphate free", "chlorine-free",
    "bleach-free", "ammonia-free", "ammonia free", "hypoallergenic", "eco-friendly", "eco friendly",
    "environmentally friendly", "safer choice", "usda certified biobased", "biobased", "septic safe", "septic-safe",
    "cruelty-free", "cruelty free", "vegan", "recyclable", "recycled", "compostable", "refillable", "concentrated",
    "sulfate-free", "paraben-free", "phthalate-free", "voc-free", "low voc", "streak-free", "streak free",
    "organic", "natural", "food-safe", "food safe", "pet-safe", "pet safe", "kid-safe", "septic",
]
_MATERIAL_RE = re.compile(r"\b(" + "|".join(re.escape(m) for m in MATERIALS) + r")\b", re.I)
_CLAIM_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(c) for c in sorted(CLAIMS, key=len, reverse=True)) + r")(?![\w-])", re.I)
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_QTY_RE = re.compile(r"([\d.]+)\s*(fluid ounces?|fl\.? ?oz|ounces?|oz|pounds?|lbs?|kilograms?|kg|grams?|g|liters?|l|milliliters?|ml|gallons?|gal|count|ct)\b", re.I)
_TO_OZ = {"fluid ounce": 1.0, "fluid ounces": 1.0, "fl oz": 1.0, "fl. oz": 1.0, "fl.oz": 1.0, "floz": 1.0,
          "ounce": 1.0, "ounces": 1.0, "oz": 1.0, "pound": 16.0, "pounds": 16.0, "lb": 16.0, "lbs": 16.0,
          "kilogram": 35.274, "kilograms": 35.274, "kg": 35.274, "gram": 0.035274, "grams": 0.035274, "g": 0.035274,
          "liter": 33.814, "liters": 33.814, "l": 33.814, "milliliter": 0.033814, "milliliters": 0.033814,
          "ml": 0.033814, "gallon": 128.0, "gallons": 128.0, "gal": 128.0}
_INGREDIENT_KEY = re.compile(r"ingredient", re.I)
_MATERIAL_KEY = re.compile(r"material", re.I)
_CLAIM_KEYS = ("Material Feature", "Material Type Free")       # listing attributes such as "Phosphate Free", "Septic safe"
EXCLUDED_CATEGORIES = re.compile(r"Tobacco-Related Products", re.I)   # age-restricted; not offered by this assistant
_DETAIL_FIELDS = ["Scent", "Item Form", "Specific Uses For Product", "Surface Recommendation", "Recommended Uses For Product",
                  "Item Volume", "Unit Count", "Number of Items", "Size", "Item Weight", "Color"]


def parse_price(value) -> tuple[float | None, float | None]:
    """A number is taken as is; '$44.00 - $54.95' -> (44.0, 54.95); junk -> (None, None)."""
    if isinstance(value, (int, float)) and value == value:
        return float(value), None
    if not isinstance(value, str):
        return None, None
    nums = [float(n.replace(",", "")) for n in _PRICE_RE.findall(value)]
    if not nums:
        return None, None
    return nums[0], (nums[-1] if len(nums) > 1 and nums[-1] != nums[0] else None)


def parse_weight_oz(*texts) -> float | None:
    """First weight or volume found, in ounces (fluid ounces and ounces are treated alike, as on shelf labels)."""
    for text in texts:
        if not isinstance(text, str):
            continue
        for m in _QTY_RE.finditer(text):
            unit = re.sub(r"\s+", " ", m.group(2).lower())
            if unit in ("count", "ct"):
                continue
            try:
                oz = float(m.group(1)) * _TO_OZ[unit]
            except (ValueError, KeyError):
                continue
            if oz > 0:
                return round(oz, 2)
    return None


def parse_count(*texts) -> float | None:
    """'100.0 Count' -> 100; used for pods, wipes, sheets and bags (price per count)."""
    for text in texts:
        if isinstance(text, str) and (m := re.search(r"([\d.]+)\s*(count|ct)\b", text, re.I)):
            try:
                n = float(m.group(1))
            except ValueError:
                continue
            if n > 1:
                return n
    return None


def infer_brand(title: str) -> str | None:
    """Leading token(s) of an Amazon title are the brand in most listings (last resort only)."""
    if not isinstance(title, str) or not title.strip():
        return None
    tokens = title.replace(",", " ").split()
    first = tokens[0]
    if first.lower() in {"the", "a", "an", "new", "set", "pack", "lot"} or first[0].isdigit():
        return None
    if len(tokens) > 2 and tokens[1] == "&":
        first = " ".join(tokens[:3])
    elif len(first) <= 2 and len(tokens) > 1:
        first = f"{first} {tokens[1]}"
    return first.strip("-:()[]") or None


def clean_text(value) -> str:
    if isinstance(value, list):
        value = " | ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace("�", " ")).strip(" |")


def _dedupe_join(found: list[str], limit: int) -> str | None:
    out: list[str] = []
    for f in found:
        f = f.lower().replace("bpa free", "bpa-free").replace("nontoxic", "non-toxic").replace(" free", "-free") \
            .replace("plant based", "plant-based").replace("eco friendly", "eco-friendly").replace("septic-safe", "septic safe")
        f = "septic safe" if f == "septic" else f          # "safe for septic systems"
        if f not in out:
            out.append(f)
    return ", ".join(out[:limit]) if out else None


def extract_materials(*texts) -> str | None:
    return _dedupe_join([m for t in texts if isinstance(t, str) for m in _MATERIAL_RE.findall(t)], 6)


def extract_claims(*texts) -> str | None:
    return _dedupe_join([m for t in texts if isinstance(t, str) for m in _CLAIM_RE.findall(t)], 8)


def brand_of(item: dict) -> tuple[str | None, str]:
    d = item.get("details") or {}
    for key, source in (("Brand", "brand"), ("Brand Name", "brand"), ("Manufacturer", "manufacturer")):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip(), source
    if isinstance(item.get("store"), str) and item["store"].strip():
        return item["store"].strip(), "store"
    return infer_brand(item.get("title") or ""), "title"


def ingredients_of(item: dict) -> str | None:
    """Ingredients the listing names, else real materials. The listing's "Material" field is only trusted when it
    names a material ("Bamboo, Nylon"); for liquids it often holds the product type ("Toilet cleaner")."""
    d = item.get("details") or {}
    parts = [v for k, v in d.items() if _INGREDIENT_KEY.search(k) and isinstance(v, str) and v.strip()]
    if parts:
        return "; ".join(parts)[:300]
    # Materials only make sense for physical goods (brushes, sponges, bags, plates). For a cleaner, the title and
    # features name the SURFACES it is for ("Stainless Steel Cleaning Spray", "safe on wood and glass"), and its
    # "Material" field often holds the product type ("Toilet cleaner"), so nothing is recorded for it.
    cats = " ".join(item.get("categories") or [])
    if not re.search(r"Cleaning Tools|Paper & Plastic|Toothpicks|Lighters|Batteries", cats):
        return None
    materials = [v for k, v in d.items() if _MATERIAL_KEY.search(k) and k not in _CLAIM_KEYS and isinstance(v, str)]
    return extract_materials(*materials, item.get("title"), clean_text(item.get("features")))


def claims_of(item: dict, title: str, features: str) -> str | None:
    d = item.get("details") or {}
    listed = " , ".join(v for k in _CLAIM_KEYS if isinstance(v := d.get(k), str))
    return extract_claims(listed, title, features)


def detail_text(item: dict) -> str:
    d = item.get("details") or {}
    return "; ".join(f"{k}: {d[k]}" for k in _DETAIL_FIELDS if isinstance(d.get(k), str) and d[k].strip())


def product_row(item: dict) -> dict:
    d = item.get("details") or {}
    title = clean_text(item.get("title"))
    features = clean_text(item.get("features"))
    price, price_max = parse_price(item.get("price"))
    brand, brand_source = brand_of(item)
    cats = [c for c in item.get("categories") or [] if c]
    size_texts = [d.get("Item Volume"), d.get("Unit Count"), d.get("Item Weight"), d.get("Size"), title]
    oz = parse_weight_oz(*size_texts)
    count = parse_count(d.get("Unit Count"), d.get("Number of Items"), title)
    asin = item.get("parent_asin")
    return {
        "id": asin, "doc_id": f"hh23-{asin}", "sku": asin, "url": f"https://www.amazon.com/dp/{asin}",
        "title": title, "price": price, "price_max": price_max,
        "category": " > ".join(cats), "top_category": cats[2] if len(cats) > 2 else (cats[-1] if cats else ""),
        "brand": brand, "brand_source": brand_source, "brand_inferred": brand_source == "title",
        "rating": float(item["average_rating"]) if item.get("average_rating") is not None else None,
        "rating_count": int(item.get("rating_number") or 0),
        "features": features, "description": (detail_text(item) + " " + clean_text(item.get("description"))).strip()[:700],
        "ingredients": ingredients_of(item), "claims": claims_of(item, title, features),
        "scent": d.get("Scent") if isinstance(d.get("Scent"), str) else None,
        "form": d.get("Item Form") if isinstance(d.get("Item Form"), str) else None,
        "size": next((s for s in size_texts[:4] if isinstance(s, str) and s.strip()), None),
        "weight_oz": oz, "count": count,
        "price_per_oz": round(price / oz, 3) if price and oz else None,
        "price_per_count": round(price / count, 3) if price and count else None,
        "recommended_age": d.get("Age Range (Description)") if isinstance(d.get("Age Range (Description)"), str) else None,
        "age_min_years": None, "model_number": d.get("Item model number"), "source": SOURCE,
    }


def build_products(meta_path: Path, category_regex: str = "", max_products: int = 25000) -> pd.DataFrame:
    rows = []
    with meta_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(product_row(json.loads(line)))
    df = pd.DataFrame(rows)
    keep = (df["title"].str.len() > 0) & df["price"].notna() & (df["features"].str.len() + df["description"].str.len() > 20)
    keep &= ~df["category"].str.contains(EXCLUDED_CATEGORIES)
    if category_regex:
        keep &= df["category"].str.contains(category_regex, case=False, regex=True)
    df = df[keep]
    # The same product is often listed several times (sizes, packs): keep the most-rated listing per title.
    df = df.sort_values("rating_count", ascending=False).drop_duplicates("doc_id").drop_duplicates("title")
    df = df.head(max_products) if max_products else df
    return df.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--meta", help="Path to the filtered metadata JSONL (default: RAW_META)")
    ap.add_argument("--category-regex", default=None, help="Keep only categories matching this regex")
    ap.add_argument("--max-products", type=int, default=None, help="Keep the N most-rated products (default MAX_PRODUCTS)")
    args = ap.parse_args()

    settings = get_settings()
    meta = Path(args.meta) if args.meta else settings.raw_meta
    if not meta.exists():
        raise SystemExit(f"{meta} not found: run `python -m voice_agent.data.download_hh2023` first")
    regex = settings.category_regex if args.category_regex is None else args.category_regex
    df = build_products(meta, regex, settings.max_products if args.max_products is None else args.max_products)
    settings.products_parquet.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(settings.products_parquet, index=False)
    print(f"Wrote {len(df):,} products -> {settings.products_parquet}")
    print(df["top_category"].value_counts().head(12).to_string())
    n = len(df)
    for col in ("rating", "brand", "ingredients", "claims", "scent", "price_per_oz", "price_per_count"):
        print(f"  {col:<16} {df[col].notna().mean():6.1%}")
    print(f"  brand source: {df['brand_source'].value_counts(normalize=True).round(3).to_dict()}")
    print(f"  price median ${df['price'].median():.2f}; rating mean {df['rating'].mean():.2f}; "
          f"rating_count median {int(df['rating_count'].median())}; n={n:,}")


if __name__ == "__main__":
    main()
