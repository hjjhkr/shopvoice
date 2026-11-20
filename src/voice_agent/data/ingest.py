"""Amazon Product Dataset 2020 (Kaggle: promptcloud/amazon-product-dataset-2020)
-> data/processed/products.parquet in the project's target schema.

    python -m voice_agent.data.ingest            # download via kagglehub if needed
    python -m voice_agent.data.ingest --csv path/to/file.csv

What the raw file really contains (10,002 rows, Jan 2020 crawl):
  * populated: Uniq Id, Product Name, Category, Selling Price, About Product,
    Product Specification, Technical Details, Shipping Weight, Product Url
  * 100% empty: Brand Name, Asin, Sku, Ingredients, Product Description, Stock
  * no ratings or reviews at all

So this script derives what it honestly can and leaves the rest null:
  sku         ASIN parsed from the product URL (/dp/<ASIN>)
  brand       Manufacturer / "Brand Name" found in the specification text (~9% of rows), otherwise
              inferred from the leading token(s) of the title (brand_inferred=True)
  recommended_age  "Manufacturer recommended age" parsed from the specification text (~60% of rows)
  ingredients material keywords found in the text (e.g. "stainless steel, bpa-free")
  rating      null (not in the dataset) - never invented
  price_per_oz price / item weight when a weight is listed (unit normalisation)
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
from pathlib import Path

import pandas as pd

from ..config import get_settings

KAGGLE_DATASET = "promptcloud/amazon-product-dataset-2020"

MATERIALS = [
    "stainless steel", "plastic", "bpa-free", "bpa free", "wood", "wooden", "bamboo", "cotton",
    "polyester", "silicone", "rubber", "foam", "metal", "aluminum", "steel", "glass", "ceramic",
    "leather", "nylon", "paper", "cardboard", "plush", "vinyl", "latex", "wool", "felt", "resin",
    "non-toxic", "nontoxic", "organic", "recycled", "biodegradable", "plant-based", "lead-free",
    "phthalate-free", "fragrance-free", "hypoallergenic",
]
_MATERIAL_RE = re.compile(r"\b(" + "|".join(re.escape(m) for m in MATERIALS) + r")\b", re.I)
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
_WEIGHT_RE = re.compile(r"([\d.]+)\s*(ounces?|oz|pounds?|lbs?|kg|g)\b", re.I)
_TO_OZ = {"ounce": 1.0, "ounces": 1.0, "oz": 1.0, "pound": 16.0, "pounds": 16.0, "lb": 16.0,
          "lbs": 16.0, "kg": 35.274, "g": 0.035274}
_BOILERPLATE = re.compile(
    r"Go to your orders and start the return.*?Ship it!|show up to \d+ reviews by default|"
    r"\(View shipping rates and policies\)|Make sure this fits by entering your model number\.?",
    re.I,
)


def find_or_download_csv(csv: str | None = None) -> Path:
    """Return the raw CSV path, downloading from Kaggle (public dataset) if absent."""
    settings = get_settings()
    if csv:
        return Path(csv)
    if settings.raw_csv.exists():
        return settings.raw_csv
    import kagglehub

    root = kagglehub.dataset_download(KAGGLE_DATASET)
    found = glob.glob(os.path.join(root, "**", "*.csv"), recursive=True)
    if not found:
        raise FileNotFoundError(f"No CSV found in Kaggle download at {root}")
    settings.raw_csv.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(found[0], settings.raw_csv)
    return settings.raw_csv


def parse_price(value) -> tuple[float | None, float | None]:
    """'$44.00 - $54.95' -> (44.0, 54.95); '$4,199.00' -> (4199.0, None); junk -> (None, None)."""
    if not isinstance(value, str):
        return None, None
    nums = [float(n.replace(",", "")) for n in _PRICE_RE.findall(value)]
    if not nums:
        return None, None
    return nums[0], (nums[-1] if len(nums) > 1 and nums[-1] != nums[0] else None)


def parse_weight_oz(*texts) -> float | None:
    for text in texts:
        if not isinstance(text, str):
            continue
        m = _WEIGHT_RE.search(text)
        if m:
            try:
                oz = float(m.group(1)) * _TO_OZ[m.group(2).lower()]
            except (ValueError, KeyError):
                continue
            if oz > 0:
                return round(oz, 2)
    return None


_MANUFACTURER_RE = re.compile(r"(?:^|\|)\s*Manufacturer:([^|]+)", re.I)
_TECH_BRAND_RE = re.compile(
    r"Brand Name\s+(.{2,40}?)\s+(?:Item Weight|Product Dimensions|Item model number|Color|Material|Manufacturer|"
    r"Number of|Size|Style|Model|Batteries|Assembled|Included|Part Number)", re.I)
_AGE_RE = re.compile(r"Manufacturer\s*recommended\s*age:([^|]+)", re.I)
_AGE_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(months?|years?)", re.I)


def spec_manufacturer(spec) -> str | None:
    """'...|Manufacturer:TheNorthwestCompany|...' -> 'The Northwest Company'. The specification column
    has its spaces stripped, so CamelCase boundaries are used to put them back."""
    if not isinstance(spec, str) or not (m := _MANUFACTURER_RE.search(spec)):
        return None
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1).strip())
    name = re.sub(r"\s*-\s*Dropship.*$", "", name, flags=re.I).strip(" ,.-")
    return name or None


def tech_brand_name(tech) -> str | None:
    if not isinstance(tech, str) or not (m := _TECH_BRAND_RE.search(tech)):
        return None
    return m.group(1).strip(" ,.-") or None


def parse_recommended_age(spec) -> tuple[str | None, float | None]:
    """'36months-15years' -> ('36 months - 15 years', 3.0); '3yearsandup' -> ('3 years and up', 3.0)."""
    if not isinstance(spec, str) or not (m := _AGE_RE.search(spec)):
        return None, None
    raw = m.group(1).strip()
    parts = _AGE_PART.findall(raw)
    if not parts:
        return None, None
    text = " - ".join(f"{n} {unit.lower()}" for n, unit in parts) + (" and up" if "andup" in raw.lower().replace(" ", "") else "")
    n, unit = parts[0]
    return text, round(float(n) / 12 if unit.lower().startswith("month") else float(n), 2)


def infer_brand(title: str) -> str | None:
    """Leading token(s) of an Amazon title are the brand in most listings."""
    if not isinstance(title, str) or not title.strip():
        return None
    tokens = title.replace(",", " ").split()
    if not tokens:
        return None
    first = tokens[0]
    if first.lower() in {"the", "a", "an", "new", "set", "pack", "lot"} or first[0].isdigit():
        return None                               # not a brand - leave unknown rather than guess
    if len(tokens) > 2 and tokens[1] == "&":      # "Melissa & Doug ..."
        first = " ".join(tokens[:3])
    elif len(first) <= 2 and len(tokens) > 1:     # "LA Kings ..."
        first = f"{first} {tokens[1]}"
    return first.strip("-:()[]") or None


def clean_text(value) -> str:
    if not isinstance(value, str):
        return ""
    value = _BOILERPLATE.sub(" ", value).replace("�", " ")
    return re.sub(r"\s+", " ", value).strip(" |")


def extract_materials(*texts) -> str | None:
    found: list[str] = []
    for text in texts:
        if isinstance(text, str):
            for m in _MATERIAL_RE.findall(text):
                m = m.lower().replace("bpa free", "bpa-free").replace("nontoxic", "non-toxic")
                if m not in found:
                    found.append(m)
    return ", ".join(found[:6]) if found else None


def build_products(csv_path: Path, category_regex: str = "") -> pd.DataFrame:
    raw = pd.read_csv(csv_path)
    df = pd.DataFrame()
    df["id"] = raw["Uniq Id"].astype(str)
    df["title"] = raw["Product Name"].map(clean_text)
    prices = raw["Selling Price"].map(parse_price)
    df["price"] = prices.map(lambda p: p[0])
    # a second figure is a range maximum only when it is higher ("$44.00 - $54.95"); otherwise it is noise
    df["price_max"] = prices.map(lambda p: p[1] if p[1] is not None and p[0] is not None and p[1] > p[0] else None)
    df["category"] = raw["Category"].fillna("").map(lambda c: " > ".join(x.strip() for x in c.split("|") if x.strip()))
    df["top_category"] = df["category"].map(lambda c: c.split(" > ")[0] if c else "")
    # Brand: the Brand Name column is empty, but a real brand is recoverable for ~9% of rows from the
    # packed specification text. Precedence: Brand Name column > Manufacturer (spec) > "Brand Name X"
    # (technical details) > leading title token (flagged as inferred).
    brand_col = raw["Brand Name"] if "Brand Name" in raw else pd.Series([None] * len(raw))
    brands, sources = [], []
    for col_value, spec, tech, title in zip(brand_col, raw["Product Specification"], raw["Technical Details"], raw["Product Name"]):
        if isinstance(col_value, str) and col_value.strip():
            brands.append(col_value.strip()); sources.append("brand column")
        elif (b := spec_manufacturer(spec)):
            brands.append(b); sources.append("manufacturer")
        elif (b := tech_brand_name(tech)):
            brands.append(b); sources.append("brand name")
        else:
            brands.append(infer_brand(title)); sources.append("title")
    df["brand"] = brands
    df["brand_source"] = sources
    df["brand_inferred"] = [s == "title" for s in sources]
    ages = raw["Product Specification"].map(parse_recommended_age)
    df["recommended_age"] = ages.map(lambda a: a[0])      # e.g. "3 years and up" (60% of rows)
    df["age_min_years"] = ages.map(lambda a: a[1])
    df["features"] = raw["About Product"].map(clean_text)
    df["description"] = raw["Technical Details"].map(clean_text).str.slice(0, 600)
    ingredients = raw["Ingredients"] if "Ingredients" in raw else pd.Series([None] * len(raw))
    materials = [extract_materials(t, f) for t, f in zip(raw["Product Name"], raw["About Product"])]
    df["ingredients"] = ingredients.where(ingredients.notna(), pd.Series(materials))
    df["rating"] = pd.Series([None] * len(raw), dtype="float64")   # not present in the 2020 dataset
    df["sku"] = raw["Product Url"].map(lambda u: (m.group(1) if isinstance(u, str) and (m := _ASIN_RE.search(u)) else None))
    df["url"] = raw["Product Url"]
    df["weight_oz"] = [parse_weight_oz(s, w) for s, w in zip(raw["Shipping Weight"], raw["Product Specification"])]
    df["price_per_oz"] = (df["price"] / df["weight_oz"]).round(3)
    df["model_number"] = raw["Model Number"].astype("string")

    # Curation: a product must have a title, a parseable price and some descriptive text.
    keep = (df["title"].str.len() > 0) & df["price"].notna() & (df["features"].str.len() > 20)
    if category_regex:
        keep &= df["category"].str.contains(category_regex, case=False, regex=True)
    df = df[keep].drop_duplicates("id").reset_index(drop=True)
    df["doc_id"] = "amz20-" + df["id"].str.slice(0, 12)
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="Path to the raw Kaggle CSV (downloaded with kagglehub if omitted)")
    ap.add_argument("--category-regex", default=None, help="Keep only categories matching this regex")
    args = ap.parse_args()

    settings = get_settings()
    csv_path = find_or_download_csv(args.csv)
    regex = settings.category_regex if args.category_regex is None else args.category_regex
    df = build_products(csv_path, regex)
    settings.products_parquet.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(settings.products_parquet, index=False)
    print(f"Wrote {len(df):,} products -> {settings.products_parquet}")
    print(df["top_category"].value_counts().head(10).to_string())
    print(f"with weight/unit price: {df['price_per_oz'].notna().sum():,} | with materials: {df['ingredients'].notna().sum():,}")


if __name__ == "__main__":
    main()
