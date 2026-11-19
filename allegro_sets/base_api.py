"""Schlanker Client für die Base.com (BaseLinker) API.

Doku: https://api.base.com/  Limit: 100 Anfragen pro Minute.
"""
from __future__ import annotations

import json
import time
from collections import deque

import requests

API_URL = "https://api.baselinker.com/connector.php"


class BaseApiError(RuntimeError):
    pass


class BaseClient:
    def __init__(self, token: str, max_per_minute: int = 90):
        self.session = requests.Session()
        self.session.headers["X-BLToken"] = token
        self.max_per_minute = max_per_minute
        self._calls: deque[float] = deque()

    # ------------------------------------------------------------------ intern
    def _throttle(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_minute:
            time.sleep(60 - (now - self._calls[0]) + 0.5)
        self._calls.append(time.monotonic())

    def call(self, method: str, **params) -> dict:
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.post(
                    API_URL,
                    data={"method": method, "parameters": json.dumps(params)},
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    raise BaseApiError(f"{method}: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if data.get("status") == "SUCCESS":
                return data
            raise BaseApiError(f"{method}: {data.get('error_code')} {data.get('error_message')}")
        raise BaseApiError(f"{method}: keine Antwort")

    # -------------------------------------------------------------- Übersicht
    def inventories(self) -> list[dict]:
        return self.call("getInventories").get("inventories", [])

    def external_storages(self) -> list[dict]:
        return self.call("getExternalStoragesList").get("storages", [])

    def price_groups(self) -> list[dict]:
        return self.call("getInventoryPriceGroups").get("price_groups", [])

    def tag_names(self) -> list[str]:
        data = self.call("getInventoryTags")
        tags = data.get("tags", [])
        if isinstance(tags, dict):
            tags = list(tags.values())
        return [t.get("name") if isinstance(t, dict) else str(t) for t in tags]

    def warehouses(self) -> list[dict]:
        return self.call("getInventoryWarehouses").get("warehouses", [])

    # ---------------------------------------------------------- Katalogsuche
    def list_inventory(self, inventory_id, ean=None, sku=None, name=None) -> dict:
        """{Produkt-ID: {name, sku, ean, ...}} für einen Filter (EAN, SKU oder Name)."""
        params = {"inventory_id": int(inventory_id)}
        if ean:
            params["filter_ean"] = ean
        elif sku:
            params["filter_sku"] = sku
        elif name:
            params["filter_name"] = name
        else:
            return {}
        return self.call("getInventoryProductsList", **params).get("products", {}) or {}

    def find_in_inventory(self, inventory_id, ean=None, sku=None, name=None) -> list[str]:
        return list(self.list_inventory(inventory_id, ean=ean, sku=sku, name=name).keys())

    def find_product(self, inventory_id, ean=None, mpn=None) -> tuple[list[str], str]:
        """Produkt im Katalog suchen, in dieser Reihenfolge:
        1. Allegro-EAN im EAN-Feld
        2. Allegro-EAN im SKU-Feld
        3. Kod producenta von Allegro im SKU-Feld
        4. Kod producenta im Produktnamen (manche Großhändler schreiben die SKU in den Namen)
        Bei 3 und 4 auch ohne Variantenanhang, z. B. TG77667-57416 -> TG77667."""
        for label, field, val, check in search_plan(ean, mpn):
            prods = self.list_inventory(inventory_id, **{field: val})
            ids = [pid for pid, p in prods.items() if not check or matches_code(p, field, val)]
            if ids:
                return [str(i) for i in ids], label
        return [], ""

    def find_by_code(self, inventory_id, code) -> tuple[list[str], str]:
        return self.find_product(inventory_id, ean=code)

    def inventory_products_data(self, inventory_id, ids: list) -> dict:
        return self.call(
            "getInventoryProductsData", inventory_id=int(inventory_id), products=[int(i) for i in ids]
        ).get("products", {})

    def find_in_external(self, storage_id, ean=None, sku=None, name=None) -> list[str]:
        params = {"storage_id": storage_id}
        if ean:
            params["filter_ean"] = ean
        elif sku:
            params["filter_sku"] = sku
        elif name:
            params["filter_name"] = name
        else:
            return []
        prods = self.call("getExternalStorageProductsList", **params).get("products", [])
        if isinstance(prods, dict):
            return list(prods.keys())
        return [str(p.get("product_id")) for p in prods]

    def external_products_data(self, storage_id, ids: list) -> dict:
        return self.call(
            "getExternalStorageProductsData", storage_id=storage_id, products=list(ids)
        ).get("products", {})

    def all_product_ids(self, inventory_id) -> list[str]:
        ids, page = [], 1
        while True:
            prods = self.call("getInventoryProductsList", inventory_id=int(inventory_id), page=page).get("products", {})
            if not prods:
                break
            ids.extend(prods.keys())
            if len(prods) < 1000:
                break
            page += 1
        return ids

    def set_index(self, inventory_id, batch: int = 100) -> dict[str, list[str]]:
        """{Einzelprodukt-ID: [Set-IDs, die es enthalten]} für den ganzen Katalog."""
        ids = self.all_product_ids(inventory_id)
        index: dict[str, list[str]] = {}
        for i in range(0, len(ids), batch):
            data = self.inventory_products_data(inventory_id, ids[i:i + batch])
            for pid, prod in data.items():
                for child in (prod.get("bundle_products") or {}):
                    index.setdefault(str(child), []).append(str(pid))
        return index

    # --------------------------------------------------------------- Anlegen
    def add_product(self, **fields) -> str:
        return str(self.call("addInventoryProduct", **fields)["product_id"])


# ------------------------------------------------------------- Suchreihenfolge
import re as _re

_CODE_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_./]{2,39}$")


def code_variants(mpn: str | None) -> list[str]:
    """Kod producenta und seine Grundform ohne Variantenanhang (TG60297-58017-V2 -> TG60297)."""
    mpn = (mpn or "").strip()
    if not mpn or not _CODE_RE.match(mpn):
        return []
    out = [mpn]
    head = _re.split(r"[-_/ ]", mpn)[0]
    if head != mpn and len(head) >= 4 and _re.search(r"[A-Za-z]", head) and _re.search(r"\d", head):
        out.append(head)
    return out


def search_plan(ean: str | None, mpn: str | None) -> list[tuple[str, str, str, bool]]:
    """(Bezeichnung, Feld, Wert, Treffer prüfen) in der Reihenfolge, in der gesucht wird."""
    plan = []
    if ean:
        plan += [("ean", "ean", ean, False), ("SKU = Allegro-EAN", "sku", ean, False)]
    for code in code_variants(mpn):
        if code == ean:
            continue
        plan += [(f"Kod producenta {code} als SKU", "sku", code, True),
                 (f"Kod producenta {code} im Namen", "name", code, True)]
    return plan


def matches_code(prod: dict, field: str, code: str) -> bool:
    """Nur echte Treffer zählen: SKU genau gleich bzw. Code als eigenes Wort im Namen
    (BIO1 darf nicht BIO10 finden)."""
    if field == "sku":
        return str(prod.get("sku") or "").strip().lower() == code.lower()
    name = str(prod.get("name") or prod.get("text_fields", {}).get("name") or "")
    return bool(_re.search(rf"(?<![A-Za-z0-9]){_re.escape(code)}(?![A-Za-z0-9])", name, _re.I))
