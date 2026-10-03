"""Static reference data used by the synthetic generator.

Everything here is synthetic. FX rates are fixed engineering values used only to
normalise amounts for peer comparison; they are not market data.
"""
from __future__ import annotations

# (city, country, lat, lon, weight)
CITIES: list[tuple[str, str, float, float, float]] = [
    ("Lagos", "NG", 6.5244, 3.3792, 30.0),
    ("Abuja", "NG", 9.0765, 7.3986, 9.0),
    ("Ibadan", "NG", 7.3775, 3.9470, 6.0),
    ("Port Harcourt", "NG", 4.8156, 7.0498, 5.0),
    ("Kano", "NG", 12.0022, 8.5920, 4.0),
    ("Enugu", "NG", 6.5244, 7.5086, 3.0),
    ("Accra", "GH", 5.6037, -0.1870, 6.0),
    ("Kumasi", "GH", 6.6885, -1.6244, 2.0),
    ("Nairobi", "KE", -1.2921, 36.8219, 4.0),
    ("Johannesburg", "ZA", -26.2041, 28.0473, 3.0),
    ("London", "GB", 51.5072, -0.1276, 6.0),
    ("Manchester", "GB", 53.4808, -2.2426, 2.0),
    ("New York", "US", 40.7128, -74.0060, 4.0),
    ("Houston", "US", 29.7604, -95.3698, 2.0),
    ("Toronto", "CA", 43.6532, -79.3832, 2.0),
    ("Dubai", "AE", 25.2048, 55.2708, 3.0),
    ("Shanghai", "CN", 31.2304, 121.4737, 1.5),
    ("Istanbul", "TR", 41.0082, 28.9784, 1.5),
]

CURRENCY_BY_COUNTRY = {
    "NG": "NGN", "GH": "GHS", "KE": "KES", "ZA": "ZAR", "GB": "GBP",
    "US": "USD", "CA": "CAD", "AE": "AED", "CN": "CNY", "TR": "TRY",
}

# Units of currency per 1 USD (static synthetic values).
FX_PER_USD = {
    "NGN": 1500.0, "GHS": 15.0, "KES": 130.0, "ZAR": 18.0, "GBP": 0.78,
    "USD": 1.0, "CAD": 1.36, "AED": 3.67, "CNY": 7.2, "TRY": 34.0,
}

# category -> (base risk, typical USD amount (lognormal median), sigma)
MERCHANT_CATEGORIES: dict[str, tuple[str, float, float]] = {
    "grocery": ("low", 25.0, 0.7),
    "fuel": ("low", 30.0, 0.5),
    "restaurant": ("low", 20.0, 0.6),
    "pharmacy": ("low", 15.0, 0.6),
    "telecom": ("low", 10.0, 0.5),
    "utilities": ("low", 40.0, 0.5),
    "clothing": ("low", 45.0, 0.8),
    "electronics": ("medium", 180.0, 0.9),
    "travel": ("medium", 350.0, 0.8),
    "education": ("low", 600.0, 0.7),
    "real_estate": ("medium", 5000.0, 0.8),
    "jewelry": ("high", 400.0, 1.0),
    "money_transfer": ("high", 250.0, 1.0),
    "gambling": ("high", 60.0, 1.0),
    "crypto_exchange": ("high", 300.0, 1.1),
}
MERCHANT_CATEGORY_WEIGHTS = {
    "grocery": 18, "fuel": 10, "restaurant": 16, "pharmacy": 6, "telecom": 6,
    "utilities": 5, "clothing": 9, "electronics": 6, "travel": 4, "education": 3,
    "real_estate": 1, "jewelry": 3, "money_transfer": 4, "gambling": 5, "crypto_exchange": 4,
}

MERCHANT_NAME_PARTS = (
    ["Alpha", "Bright", "Crest", "Delta", "Eko", "Falcon", "Golden", "Harbor", "Ivory", "Jade",
     "Kola", "Lagoon", "Meridian", "Niger", "Oasis", "Prime", "Quartz", "River", "Summit", "Tide",
     "Unity", "Vista", "Willow", "Zenith"],
    ["Mart", "Stores", "Express", "Hub", "Plaza", "Traders", "Services", "Point", "Market", "Depot"],
)

# segment -> (daily txn rate, USD amount median for transfers, sigma, monthly income USD)
SEGMENTS: dict[str, tuple[float, float, float, float]] = {
    "student": (0.04, 25.0, 0.8, 150.0),
    "mass": (0.05, 60.0, 0.9, 400.0),
    "affluent": (0.10, 250.0, 1.0, 3000.0),
    "sme": (0.22, 900.0, 1.1, 0.0),
    "corporate": (0.50, 6000.0, 1.2, 0.0),
}
INDIVIDUAL_SEGMENT_WEIGHTS = {"student": 0.15, "mass": 0.65, "affluent": 0.20}
BUSINESS_SEGMENT_WEIGHTS = {"sme": 0.8, "corporate": 0.2}

# transaction type mix for ordinary outbound activity, by customer type
OUTBOUND_TYPE_MIX = {
    "individual": {"card_payment": 0.55, "transfer": 0.25, "bill_payment": 0.10, "cash_withdrawal": 0.10},
    "business": {"transfer": 0.60, "bill_payment": 0.20, "card_payment": 0.15, "cash_withdrawal": 0.05},
}
