"""What the private catalog is, in one place: every user-facing mention of the dataset reads from here."""

CATALOG_NAME = "Amazon Reviews 2023, Household Supplies"
CATALOG_SOURCE_ID = "amazon-reviews-2023/health-and-household"
PRICE_SNAPSHOT = "2023"                         # prices were captured when the dataset was crawled in 2023
CATALOG_PRICE_LABEL = f"catalog, {PRICE_SNAPSHOT}"
CITATION = ('Hou et al., "Bridging Language and Items for Retrieval and Recommendation", arXiv:2403.03952 (2024); '
            "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023")
