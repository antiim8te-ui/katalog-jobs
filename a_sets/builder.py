"""Schritte 2, 3, 5: Einzelprodukt aus dem Großhändler importieren und 2er-Set anlegen."""
from __future__ import annotations

import math
from pathlib import Path

from . import images as img
from . import texts
from .base_api import BaseClient, matches_code, search_plan
from .config import Settings


# ------------------------------------------------------------------ Quelle
def _normalize(raw: dict) -> dict:
    """Katalog- und externe-Speicher-Daten auf ein gemeinsames Format bringen."""
    tf = raw.get("text_fields") or {}
    imgs = raw.get("images") or {}
    if isinstance(imgs, dict):
        img_list = [imgs[k] for k in sorted(imgs, key=lambda x: int(x))]
    else:
        img_list = list(imgs)
    img_list = [u.get("url") if isinstance(u, dict) else u for u in img_list]
    prices = raw.get("prices") or {}
    price = raw.get("price") or (next(iter(prices.values())) if prices else None)
    return {
        "name": tf.get("name") or raw.get("name") or "",
        "description": tf.get("description") or raw.get("description") or "",
        "features": tf.get("features") or raw.get("features") or {},
        "sku": raw.get("sku") or "",
        "ean": raw.get("ean") or "",
        "tax_rate": raw.get("tax_rate", 23),
        "weight": float(raw.get("weight") or 0),
        "height": float(raw.get("height") or 0),
        "width": float(raw.get("width") or 0),
        "length": float(raw.get("length") or 0),
        "price": float(price) if price else None,
        "images": [u for u in img_list if u],
    }


def _is_bundle(raw: dict) -> bool:
    return bool(raw.get("is_bundle")) or bool(raw.get("bundle_products"))


def find_source_product(base: BaseClient, s: Settings, ean, mpn, title) -> tuple[dict | None, str]:
    """Reihenfolge: EAN, Allegro-EAN als SKU, Kod producenta als SKU und im Namen, zuletzt Titel
    (Titel nur bei eindeutigem Treffer). Sets (Bundles) werden ignoriert, gesucht ist das Einzelprodukt."""
    plan = search_plan(ean, mpn) + ([("name", "name", title, False)] if title else [])
    for key, field, val, check in plan:
        if s.source_mode == "external_storage":
            ids = base.find_in_external(s.source_id, **{field: val})
        else:
            prods = base.list_inventory(s.source_id, **{field: val})
            ids = [pid for pid, p in prods.items() if not check or matches_code(p, field, val)]
        if not ids:
            continue
        if s.source_mode == "external_storage":
            data = base.external_products_data(s.source_id, ids[:20])
        else:
            data = base.inventory_products_data(s.source_id, ids[:20])
        singles = {pid: raw for pid, raw in data.items() if not _is_bundle(raw)}
        if not singles:
            continue
        if key == "name" and len(singles) != 1:
            return None, f"Name nicht eindeutig ({len(singles)} Treffer)"
        pid, raw = next(iter(singles.items()))
        src = _normalize(raw)
        src["_id"] = str(pid)
        return src, f"gefunden über {key}"
    return None, "nicht im Großhändlerkatalog"


# ------------------------------------------------------------------ Preise
def set_price(competitor_single: float | None, source_price: float | None, rules: dict) -> float | None:
    count = rules.get("set_count", 2)
    pr = rules.get("pricing", {})
    if competitor_single:
        p = competitor_single * count * pr.get("competitor_factor", 0.95)
    elif source_price:
        p = source_price * count * pr.get("markup_factor", 1.6)
    else:
        return None
    if source_price and pr.get("min_margin_factor"):
        p = max(p, source_price * count * pr["min_margin_factor"])
    return math.floor(p) + 0.99 if pr.get("end_99", True) else round(p, 2)


def set_dimensions(single: dict, count: int) -> dict:
    """Stückmaße: Exemplare entlang der kleinsten Seite stapeln, Gewicht mal Stückzahl."""
    dims = sorted([single["length"], single["width"], single["height"]])
    if not all(dims):
        return {"weight": round(single["weight"] * count, 3)}
    dims[0] *= count
    dims.sort(reverse=True)
    return {"length": dims[0], "width": dims[1], "height": dims[2],
            "weight": round(single["weight"] * count, 3)}


# ------------------------------------------------------------------ Anlegen
def ensure_single(base: BaseClient, s: Settings, src: dict) -> str:
    """Einzelprodukt im eigenen Katalog anlegen, falls es dort noch nicht existiert."""
    # Quelle ist dein eigener Katalog: das gefundene Einzelprodukt direkt verwenden
    if s.source_mode == "inventory" and str(s.source_id) == str(s.target_inventory_id) and src.get("_id"):
        return src["_id"]
    for key, val in (("ean", src["ean"]), ("sku", src["sku"])):
        if val:
            ids = base.find_in_inventory(s.target_inventory_id, **{key: val})
            if ids:
                return ids[0]
    fields = {
        "inventory_id": s.target_inventory_id,
        "sku": src["sku"], "ean": src["ean"], "tax_rate": src["tax_rate"],
        "weight": src["weight"], "height": src["height"],
        "width": src["width"], "length": src["length"],
        "text_fields": {"name": src["name"], "description": src["description"],
                        **({"features": src["features"]} if src["features"] else {})},
        "images": {str(i): img.url_to_base_image(u) for i, u in enumerate(src["images"][:16])},
    }
    if src["price"]:
        fields["prices"] = {s.price_group_id: src["price"]}
    return base.add_product(**fields)


def create_set(base: BaseClient, s: Settings, cand, src: dict, single_id: str,
               dry_run: bool = False, text: dict | None = None) -> dict:
    r = s.rules
    count = r.get("set_count", 2)
    base_sku = src["sku"] or src["ean"] or str(single_id)
    sku = f"{r.get('set_sku_prefix', '')}{base_sku}{r.get('set_sku_suffix', 'x2')}"

    existing = base.find_in_inventory(s.target_inventory_id, sku=sku)
    if existing:
        return {"set_id": existing[0], "sku": sku, "title": "(bereits in Base vorhanden)", "price": None,
                "images": 0, "preview": "-", "note": "Set existierte bereits"}

    if text is None:
        text = texts.generate(src, count, s, closing_tpl=r.get("description", {}).get("closing_line"),
                              market_title=cand["title"] if "title" in cand.keys() else None,
                              banned_words=r.get("description", {}).get("title_banned_words", []),
                              brand_in_title=r.get("description", {}).get("brand_in_title", False))
    from . import llm
    if not text.get("ai") and llm.enabled(s) and r.get("description", {}).get("require_ai", True):
        # KI eingestellt, aber gerade nicht erreichbar: lieber beim nächsten Lauf erneut versuchen
        raise RuntimeError("KI nicht verfügbar, Set wird beim nächsten Lauf erneut versucht")

    # Bild 1: Set-Hauptbild aus dem ersten Produktbild
    if not src["images"]:
        raise RuntimeError("Einzelprodukt hat keine Bilder")
    # Hauptbild aus dem ersten Bild, das sich laden lässt (defekte Links werden übersprungen)
    first, first_idx = None, None
    for i, u in enumerate(src["images"]):
        try:
            first, first_idx = img.download(u), i
            break
        except Exception as exc:
            print(f"  Bild {i + 1} nicht ladbar, übersprungen ({str(exc)[:80]})")
    if first is None:
        raise RuntimeError("keines der Produktbilder ließ sich laden")
    main = img.make_set_image(first, count,
                              use_rembg=r.get("images", {}).get("remove_background", False),
                              overlap=r.get("images", {}).get("overlap", 0.12))
    preview = Path(s.data_dir, "images", f"{_safe_name(sku)}.jpg")
    main.save(preview, quality=90)
    start = first_idx if r.get("images", {}).get("include_single_main_image", False) else first_idx + 1
    rest = src["images"][start:]
    set_images = {"0": img.to_base_image(main)}
    for u in rest:
        if len(set_images) >= 16:
            break
        try:
            set_images[str(len(set_images))] = img.url_to_base_image(u)
        except Exception as exc:
            print(f"  Galeriebild nicht ladbar, übersprungen ({str(exc)[:80]})")

    suffix = s.text_field_suffix
    text_fields = {"name": text["title"], "description": text["description"]}
    if suffix:
        text_fields[f"name{suffix}"] = text["title"]
        text_fields[f"description{suffix}"] = text["description"]

    fields = {
        "inventory_id": s.target_inventory_id,
        "is_bundle": True,
        "bundle_products": {str(single_id): count},
        "sku": sku,
        "ean": src["ean"],          # Set mit derselben EAN wie das Einzelprodukt
        "tax_rate": src["tax_rate"],
        "text_fields": text_fields,
        "images": set_images,
        **set_dimensions(src, count),
    }
    price = None
    if r.get("pricing", {}).get("set_price", False):  # aus: Preise setzt deine eigene Automatisierung
        price = set_price(cand["price"], src["price"], r)
        if price:
            fields["prices"] = {s.price_group_id: price}
    if r.get("listing_tag"):
        fields["tags"] = [r["listing_tag"]]

    summary = {"sku": sku, "title": text["title"], "price": price, "preview": str(preview),
               "images": len(set_images)}
    preview_html = Path(s.data_dir, "images", f"{_safe_name(sku)}.html")
    if dry_run:
        # Vorschau mit lokalem Hauptbild und den Bildern des Einzelprodukts als Platzhalter
        stand_in = [preview.resolve().as_uri()] + rest[:4]
        _write_preview(preview_html, text["title"], texts.with_images(text, stand_in))
        summary["set_id"] = "(Testlauf)"
        return summary
    set_id = base.add_product(**fields)
    summary["set_id"] = set_id
    # Beschreibung mit den echten Bildadressen des Sets (Base-CDN) neu setzen
    description = text["description"]
    try:
        urls = set_image_urls(base, s, set_id)
        if urls:
            description = texts.with_images(text, urls)
            base.add_product(inventory_id=s.target_inventory_id, product_id=int(set_id),
                             text_fields=_text_fields(s, None, description))
        else:
            print("  Hinweis: Base hat noch keine Bildadressen geliefert, Beschreibung ohne Bilder.")
    except Exception as exc:
        print(f"  Hinweis: Bilder konnten nicht in die Beschreibung übernommen werden ({exc}).")
    _write_preview(preview_html, text["title"], description)
    return summary


# ------------------------------------------------------------------ Bilder in der Beschreibung
def set_image_urls(base: BaseClient, s: Settings, set_id) -> list[str]:
    """Bildadressen (Base-CDN) eines Produkts in der Reihenfolge Bild 1, 2, 3 ..."""
    data = base.inventory_products_data(s.target_inventory_id, [set_id])
    raw = next(iter(data.values()), {}) or {}
    imgs = raw.get("images") or {}
    if isinstance(imgs, dict):
        return [imgs[k] for k in sorted(imgs, key=lambda x: int(x)) if imgs[k]]
    return [u for u in imgs if u]


def _text_fields(s: Settings, title: str | None, description: str) -> dict:
    fields = {"description": description}
    if title:
        fields["name"] = title
    if s.text_field_suffix:
        fields[f"description{s.text_field_suffix}"] = description
        if title:
            fields[f"name{s.text_field_suffix}"] = title
    return fields


def _safe_name(text) -> str:
    """Dateiname ohne Zeichen, die Windows/Linux nicht erlauben (z. B. SKU „PNME3/32 100-200“)."""
    import re as _re
    return _re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("._") or "set"


def _write_preview(path: Path, title: str, description: str) -> None:
    path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
        "<style>body{font-family:sans-serif;max-width:1000px;margin:2rem auto}"
        ".section{display:flex;gap:24px;margin:24px 0}.item-12{flex:1 1 100%}.item-6{flex:1 1 50%}"
        "img{max-width:100%}</style><body>"
        f"<p><b>Titel ({len(title)}/75):</b> {title}</p><hr>{description}</body>",
        encoding="utf-8")


# ------------------------------------------------------------------ Texte nachträglich erneuern
def is_incomplete(base: BaseClient, s: Settings, set_id) -> bool:
    """True, wenn die Beschreibung aus der Ersatzvorlage stammt (keine Cechy Produktu)."""
    data = base.inventory_products_data(s.target_inventory_id, [set_id])
    raw = next(iter(data.values()), None) or {}
    tf = raw.get("text_fields") or {}
    desc = tf.get(f"description{s.text_field_suffix}") if s.text_field_suffix else None
    return "Cechy Produktu" not in (desc or tf.get("description") or "")


def refresh_texts(base: BaseClient, s: Settings, cand, dry_run: bool = False,
                  layout_only: bool = False) -> dict:
    """Titel und Beschreibung eines schon angelegten Sets erneuern und in Base überschreiben.
    layout_only=True: vorhandenen Text behalten, nur ins neue Layout mit Bildern bringen (ohne KI).
    Nur die Textfelder werden gesendet: Bilder, SKU, EAN, Bundle und Tags bleiben unverändert."""
    r = s.rules
    count = r.get("set_count", 2)
    set_id = cand["set_id"]
    urls = set_image_urls(base, s, set_id)
    preview = Path(s.data_dir, "images", f"{set_id}_neu.html")

    if layout_only:
        data = base.inventory_products_data(s.target_inventory_id, [set_id])
        raw = next(iter(data.values()), None) or {}
        tf = raw.get("text_fields") or {}
        key = f"description{s.text_field_suffix}" if s.text_field_suffix and tf.get(f"description{s.text_field_suffix}") else "description"
        title = tf.get(f"name{s.text_field_suffix}") or tf.get("name") or ""
        description = texts.relayout(tf.get(key, ""), urls)
        if not description:
            raise RuntimeError("Beschreibung hat nicht das erwartete Format, bitte ohne --layout-only erneuern")
        _write_preview(preview, title, description)
        if not dry_run:
            base.add_product(inventory_id=s.target_inventory_id, product_id=int(set_id),
                             text_fields=_text_fields(s, None, description))
        return {"set_id": set_id, "title": title}

    data = base.inventory_products_data(s.target_inventory_id, [cand["single_id"]])
    raw = next(iter(data.values()), None)
    if not raw:
        raise RuntimeError(f"Einzelprodukt {cand['single_id']} nicht mehr in Base")
    src = _normalize(raw)
    text = texts.generate(src, count, s, closing_tpl=r.get("description", {}).get("closing_line"),
                          market_title=cand["title"],
                          banned_words=r.get("description", {}).get("title_banned_words", []),
                          brand_in_title=r.get("description", {}).get("brand_in_title", False))
    if not text.get("ai"):
        raise RuntimeError("keine KI-Antwort (Limit erreicht?), Set bleibt unverändert")
    description = texts.with_images(text, urls)
    _write_preview(preview, text["title"], description)
    if not dry_run:
        base.add_product(inventory_id=s.target_inventory_id, product_id=int(set_id),
                         text_fields=_text_fields(s, text["title"], description))
    return {"set_id": set_id, "title": text["title"]}


# ------------------------------------------------------------------ 3er-Sets aus vorhandenen 2er-Sets
def _stock_keys_for(warehouses: list[dict], name_part: str) -> set[str]:
    """Lager-Schlüssel (z. B. bl_123) aller Lager, deren Name name_part enthält."""
    keys = set()
    for w in warehouses:
        if name_part.lower() in str(w.get("name", "")).lower():
            wid = str(w.get("warehouse_id"))
            keys |= {wid, f"{w.get('warehouse_type', 'bl')}_{wid}", f"bl_{wid}"}
    return keys


def multi_set_candidates(base: BaseClient, s: Settings, from_count: int, to_count: int,
                         warehouse_name: str, to_suffix: str) -> tuple[list[dict], dict]:
    """Einzelprodukte des Großhändlers, die schon ein Set mit from_count Stück haben,
    aber noch keins mit to_count Stück (SKU des Einzelprodukts + to_suffix)."""
    stats = {"sets": 0, "singles": 0, "wholesaler": 0, "done": 0}
    keys = _stock_keys_for(base.warehouses(), warehouse_name) if warehouse_name else set()
    if warehouse_name and not keys:
        raise RuntimeError(f"Kein Lager mit „{warehouse_name}“ im Namen gefunden "
                           "(Lagernamen zeigt: python -m allegro_sets setup)")
    ids = base.all_product_ids(s.target_inventory_id)
    data: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        data.update({str(k): v for k, v in base.inventory_products_data(s.target_inventory_id, ids[i:i + 100]).items()})
    skus = {str(p.get("sku") or "").strip().lower() for p in data.values()}
    singles: dict[str, dict] = {}
    for sid, p in data.items():
        bp = p.get("bundle_products") or {}
        if len(bp) == 1 and int(next(iter(bp.values()))) == from_count:
            child = str(next(iter(bp)))
            stats["sets"] += 1
            singles.setdefault(child, {"set_name": (p.get("text_fields") or {}).get("name") or p.get("name") or ""})
    stats["singles"] = len(singles)
    out = []
    for child, info in singles.items():
        raw = data.get(child)
        if not raw or _is_bundle(raw):
            continue
        stock = raw.get("stock") or {}
        if keys and not any(float(stock.get(k) or 0) > 0 for k in keys):
            continue
        stats["wholesaler"] += 1
        src = _normalize(raw)
        src["_id"] = child
        target_sku = f"{s.rules.get('set_sku_prefix', '')}{src['sku'] or src['ean'] or child}{to_suffix}"
        if target_sku.lower() in skus:
            stats["done"] += 1
            continue
        out.append({"src": src, "single_id": child, "set_name": info["set_name"], "sku": target_sku})
    return out, stats


# ------------------------------------------------------------------ Sets für den ganzen Großhändler-Katalog
def read_catalog(base: BaseClient, inventory_id) -> dict[str, dict]:
    """Alle Produkte des Katalogs (einmal lesen, für mehrere Großhändler nutzen)."""
    ids = base.all_product_ids(inventory_id)
    data: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        data.update({str(k): v for k, v in base.inventory_products_data(inventory_id, ids[i:i + 100]).items()})
        if len(data) % 5000 < 100 or len(data) >= len(ids):
            print(f"  {len(data)}/{len(ids)} Produkte gelesen")
    return data


def catalog_set_candidates(base: BaseClient, s: Settings, count: int, warehouse_name: str,
                           suffix: str, data: dict | None = None, min_stock: float = 0) -> tuple[list[dict], dict]:
    """Alle Einzelprodukte des Großhändlers (Lager warehouse_name, auf Lager), die noch kein
    Set mit count Stück haben. Ohne Allegro-Recherche."""
    stats = {"singles": 0, "wholesaler": 0, "done": 0}
    warehouses = base.warehouses()
    keys = _stock_keys_for(warehouses, warehouse_name)
    if not keys:
        names = ", ".join(str(w.get("name")) for w in warehouses)
        raise RuntimeError(f"Kein Lager mit „{warehouse_name}“ im Namen gefunden. Vorhandene Lager: {names}. "
                           "Bitte warehouse_name in der rules.yaml anpassen.")
    if data is None:
        data = read_catalog(base, s.target_inventory_id)
    skus = {str(p.get("sku") or "").strip().lower() for p in data.values()}
    has_set = set()
    for p in data.values():
        bp = p.get("bundle_products") or {}
        if len(bp) == 1 and int(next(iter(bp.values()))) == count:
            has_set.add(str(next(iter(bp))))
    out = []
    for pid, raw in data.items():
        if _is_bundle(raw):
            continue
        stats["singles"] += 1
        stock = raw.get("stock") or {}
        qty = max((float(stock.get(k) or 0) for k in keys), default=0)
        if qty <= 0:
            continue
        stats["wholesaler"] += 1
        if qty < min_stock:                 # z. B. 3er-Set nur, wenn mindestens 3 Stück da sind
            continue
        src = _normalize(raw)
        src["_id"] = pid
        target_sku = f"{s.rules.get('set_sku_prefix', '')}{src['sku'] or src['ean'] or pid}{suffix}"
        if pid in has_set or target_sku.lower() in skus:
            stats["done"] += 1
            continue
        if not src["images"]:
            continue
        out.append({"src": src, "single_id": pid, "sku": target_sku, "stock": qty})
    out.sort(key=lambda t: t["stock"], reverse=True)   # gut verfügbare Produkte zuerst
    return out, stats
