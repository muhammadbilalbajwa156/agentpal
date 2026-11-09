"""Load and validate the reviewed vendor catalogue (structure is trusted, text is not)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..common import EMAIL, AgentError, identifier, money, text, untrusted_text

CATALOGUE_PATH = Path(__file__).with_name("catalogue.json")
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
# Demo prices are deliberately small sandbox amounts.
MIN_PRICE, MAX_PRICE = Decimal("1.00"), Decimal("25.00")


@dataclass(frozen=True)
class Vendor:
    vendor_id: str
    name: str
    domain: str
    paypal_email: str


@dataclass(frozen=True)
class Item:
    item_id: str
    vendor: Vendor
    title: str
    price: str
    product_query: str | None = None

    def for_model(self) -> dict:
        """What the model may see: no payee mailbox, instruction-shaped text withheld."""
        return {"offer_id": self.item_id, "vendor": self.vendor.name,
                "title": untrusted_text(self.title, 160, "Catalogue item (text withheld as untrusted)"),
                "amount": self.price, "currency": "USD"}


@dataclass(frozen=True)
class Catalogue:
    version: str
    vendors: dict[str, Vendor]
    items: dict[str, Item]

    def for_vendor(self, vendor_id: str) -> list[Item]:
        return [i for i in self.items.values() if i.vendor.vendor_id == vendor_id]


def load_catalogue(path: str | Path = CATALOGUE_PATH) -> Catalogue:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("currency") != "USD":
            raise ValueError
        vendors = {}
        for vendor_id, v in raw["vendors"].items():
            email = v["paypal_email"]
            if (not isinstance(email, str) or not EMAIL.fullmatch(email)
                    or not email.endswith("@business.example.com")):
                raise ValueError  # sandbox business mailboxes only
            if not DOMAIN.fullmatch(v["domain"]):
                raise ValueError
            vendors[identifier(vendor_id)] = Vendor(vendor_id, text(v["name"], 100),
                                                    v["domain"], email)
        items = {}
        for i in raw["items"]:
            item_id = identifier(i["item_id"])
            price = money(i["price"], MAX_PRICE)
            if Decimal(price) < MIN_PRICE or item_id in items or i["price"] != price:
                raise ValueError
            query = i.get("product_query")
            items[item_id] = Item(item_id, vendors[i["vendor"]], text(i["title"], 160), price,
                                  text(query, 200) if query else None)
        return Catalogue(text(raw["catalogue_version"], 40), vendors, items)
    except (OSError, KeyError, TypeError, ValueError, AttributeError, AgentError):
        raise AgentError("invalid_vendor_catalogue") from None
