"""Befehle:

  python -m allegro_sets daily           täglicher Komplettlauf mit Rückfragen (empfohlen)
  python -m allegro_sets setup           IDs von Katalogen, Speichern, Preisgruppen anzeigen
  python -m allegro_sets scan [--headed] Allegro-Shops lesen, Kandidaten nach data/review.csv
  python -m allegro_sets import-csv F    Kandidaten aus einer CSV (z. B. Apify-Export) laden
  python -m allegro_sets review          review.csv neu erzeugen (nach Regeländerungen)
  python -m allegro_sets check-base      Kandidaten aussortieren, für die es schon ein Set gibt
  python -m allegro_sets skip-missing    nicht importierbare Produkte dauerhaft überspringen
  python -m allegro_sets models          verfügbare KI-Modelle des eingestellten Anbieters anzeigen
  python -m allegro_sets refresh-texts [--dry-run] [--limit N]   Texte angelegter Sets neu erzeugen
  python -m allegro_sets approve         Freigaben aus data/review.csv übernehmen
  python -m allegro_sets build [--dry-run] [--limit N]   freigegebene Kandidaten zu Sets machen
  python -m allegro_sets run             scan + build in einem (für den täglichen Lauf)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from . import builder
from .base_api import BaseClient
from .config import load_settings
from .state import State

REVIEW_COLS = ["freigabe", "buyers", "price", "title", "ean", "mpn", "seller", "url", "offer_id", "status"]


def cmd_setup(s, base, st, a):
    print("\nKataloge (inventory_id):")
    for inv in base.inventories():
        print(f"  {inv.get('inventory_id'):>8}  {inv.get('name')}")
    print("\nExterne Speicher (storage_id):")
    for es in base.external_storages():
        print(f"  {es.get('storage_id'):>20}  {es.get('name')}")
    print("\nPreisgruppen (price_group_id):")
    for pg in base.price_groups():
        print(f"  {pg.get('price_group_id'):>8}  {pg.get('name')} {pg.get('currency')}")
    print("\nLager (warehouse_id):")
    for wh in base.warehouses():
        print(f"  {wh.get('warehouse_type')}_{wh.get('warehouse_id')}  {wh.get('name')}")
    print("\nDen Großhändlerkatalog mit Zusatz '(Base Connect)' als SOURCE_ID in .env eintragen.")


MULTIPACK_RE = re.compile(r"(?:^|[\s(])\d+\s?[xX×](?=\s|[^\W\d])|\b\d+\s?(?:szt|sztuk|pak|pack)\b|\bx\s?\d+\b", re.I)


def _open_csv(path: Path):
    """CSV schreiben, auch wenn die Datei gerade in Excel offen ist (dann unter neuem Namen)."""
    try:
        return path.open("w", newline="", encoding="utf-8-sig"), path
    except PermissionError:
        import datetime as dt
        alt = path.with_name(f"{path.stem}_{dt.datetime.now():%H%M%S}{path.suffix}")
        print(f"  Hinweis: {path.name} ist gerade geöffnet (z. B. in Excel), schreibe stattdessen {alt.name}")
        return alt.open("w", newline="", encoding="utf-8-sig"), alt


def is_multipack(title: str) -> bool:
    """Titel wie '2x Wieszak', '20x Worki', 'x2', '4 szt.' gelten als Mehrfachpackung."""
    return bool(MULTIPACK_RE.search(title or ""))


def _passes(o, rules) -> bool:
    f = rules.get("filters", {})
    if f.get("exclude_multipacks", True) and is_multipack(o.title):
        return False
    if o.buyers < f.get("min_buyers", 5):
        return False
    if o.price is not None:
        if o.price < f.get("min_price", 0) or o.price > f.get("max_price", 10**9):
            return False
    title = (o.title or "").lower()
    return not any(w.lower() in title for w in f.get("exclude_words", []))


def _store(st, s, offers):
    added = 0
    for o in offers:
        st.add_snapshot(o)
        if not _passes(o, s.rules) or (o.ean and st.ean_done(o.ean)):
            continue
        st.upsert_candidate(o)
        added += 1
    st.commit()
    return added


def _write_review(s, st):
    path = Path(s.out_dir, "review.csv")
    # bereits gespeicherte Kandidaten nachträglich gegen die aktuellen Regeln prüfen
    if s.rules.get("filters", {}).get("exclude_multipacks", True):
        n = 0
        for r in st.by_status("neu"):
            if is_multipack(r["title"]):
                st.set_status(r["offer_id"], "abgelehnt", note="Mehrfachpackung")
                n += 1
        if n:
            st.commit()
            print(f"{n} Mehrfachpackungen aussortiert.")
    rows = st.by_status("neu")
    fh, path = _open_csv(path)
    with fh:
        w = csv.DictWriter(fh, REVIEW_COLS, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if c == "freigabe" else r[c]) for c in REVIEW_COLS})
    if s.rules.get("auto_approve", False):
        return
    print(f"{len(rows)} offene Kandidaten in {path}. Spalte 'freigabe' mit x (ja) oder n (nein) füllen,"
          f" dann: python -m allegro_sets approve")


def _check_base(s, base, st) -> int:
    """Kandidaten aussortieren, für die es in Base schon ein Set gibt.

    Ein Produkt gilt als verwendet, wenn ein Set (Bundle) in deinem Katalog es enthält.
    Liegt das Einzelprodukt nur im Katalog, bleibt es Kandidat und wird zum Set gebaut.
    """
    # in_base wird mitgeprüft, falls es nach einer älteren Regel aussortiert wurde
    rows = [r for r in st.by_status("neu", "freigegeben", "nicht_gefunden", "in_base", "fehler") if r["ean"] or r["mpn"]]
    if not rows:
        return 0
    print("Lese Sets aus deinem Base-Katalog ...")
    index = base.set_index(s.target_inventory_id)
    fallback = not index
    if fallback:
        print("  Keine Sets (Bundles) im Katalog gefunden. Vorsichtshalber gilt: EAN im Katalog = schon verwendet.")
    else:
        print(f"  {len(set(x for v in index.values() for x in v))} Sets gefunden.")
    used = present = missing = 0
    missing_rows = []
    for r in rows:
        try:
            ids, _how = base.find_product(s.target_inventory_id, ean=r["ean"], mpn=r["mpn"])
        except Exception as exc:
            print(f"  {r['ean']}: Abfrage fehlgeschlagen ({exc})")
            continue
        bundle_ids = {x for v in index.values() for x in v}
        sets = list(dict.fromkeys([sid for pid in ids for sid in index.get(str(pid), [])]
                                  + [pid for pid in ids if pid in bundle_ids]))
        if ids and (sets or fallback):
            st.set_status(r["offer_id"], "in_base", single_id=ids[0],
                          note=f"Set existiert bereits (Set-ID {', '.join(sets[:3])})" if sets
                          else f"bereits in Base (Produkt-ID {ids[0]})")
            used += 1
            print(f"  schon als Set  {r['ean']}  {r['title'][:55]}")
        elif r["status"] == "in_base":
            st.set_status(r["offer_id"], "neu")
        if ids and not (sets or fallback):
            st.db.execute("UPDATE candidates SET single_id=?, note=? WHERE offer_id=?",
                          (ids[0], "Einzelprodukt in Base, noch kein Set", r["offer_id"]))
            present += 1
        elif not ids:
            st.db.execute("UPDATE candidates SET note=? WHERE offer_id=?",
                          ("noch nicht aus Base Connect importiert", r["offer_id"]))
            missing += 1
            missing_rows.append(r)
    st.commit()
    print(f"{used} schon als Set vorhanden, {present} Einzelprodukte bereit zum Set-Bau, "
          f"{missing} noch nicht in deinem Katalog.")
    if missing_rows:
        fh, path = _open_csv(Path(s.out_dir, "fehlt_in_base.csv"))
        s.missing_csv = path
        with fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["ean", "kod_producenta", "title", "buyers", "url"])
            for r in missing_rows:
                w.writerow([r["ean"], r["mpn"] or "", r["title"], r["buyers"], r["url"]])
        print(f"Diese Produkte bitte aus Base Connect importieren (EAN oder Kod producenta): {path}")
    return used


def _auto_approve(s, st) -> None:
    if not s.rules.get("auto_approve", False):
        return
    rows = st.by_status("neu")
    for r in rows:
        st.set_status(r["offer_id"], "freigegeben")
    st.commit()
    if rows:
        print(f"{len(rows)} Kandidaten automatisch freigegeben.")


def cmd_skip_missing(s, base, st, a):
    """Alles, was nach dem Import immer noch nicht in deinem Katalog ist, endgültig überspringen."""
    rows = [r for r in st.by_status("neu", "freigegeben", "nicht_gefunden")
            if (r["note"] or "").startswith("noch nicht aus Base Connect") or r["status"] == "nicht_gefunden"]
    for r in rows:
        st.set_status(r["offer_id"], "abgelehnt", note="nicht in Base Connect verfügbar")
        print(f"  übersprungen  {r['ean']}  {r['title'][:60]}")
    st.commit()
    print(f"{len(rows)} Kandidaten dauerhaft übersprungen.")


def cmd_check_base(s, base, st, a):
    _check_base(s, base, st)
    _auto_approve(s, st)
    _write_review(s, st)


def _seller_urls(rules) -> list[str]:
    """Shop-Adressen mit Bestseller-Sortierung und optionalem Bewertungsfilter von Allegro."""
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    rating = (rules.get("scraper") or {}).get("rating_filter")
    out = []
    for url in rules["seller_urls"]:
        u = urlparse(url)
        q = dict(parse_qsl(u.query))
        q.pop("dd_referrer", None)
        q.setdefault("order", "qd")
        if rating:
            q["ocena-produktu"] = rating
        else:
            q.pop("ocena-produktu", None)
        out.append(urlunparse(u._replace(query=urlencode(q))))
    return out


def _pre_filter(s, st):
    """Vor dem Öffnen der Angebotsseiten aussortieren, was nicht gebraucht wird."""
    known = {r[0] for r in st.db.execute(
        "SELECT offer_id FROM candidates WHERE status IN ('in_base','erstellt','abgelehnt')")}
    f = s.rules.get("filters", {})

    def skip(o):
        if o.offer_id in known:
            return "bereits bekannt"
        if f.get("exclude_multipacks", True) and is_multipack(o.title):
            return "Mehrfachpackungen"
        if o.buyers < f.get("min_buyers", 5):
            return "zu wenig Käufer"
        if o.price is not None and not (f.get("min_price", 0) <= o.price <= f.get("max_price", 10**9)):
            return "außerhalb Preisspanne"
        title = (o.title or "").lower()
        if any(w.lower() in title for w in f.get("exclude_words", [])):
            return "Ausschlusswort"
        return None
    return skip


def cmd_scan(s, base, st, a):
    from .scraper import scrape_sellers
    sc = s.rules.get("scraper", {})
    offers = scrape_sellers(_seller_urls(s.rules), max_pages=sc.get("max_pages", 5),
                            detail_top=sc.get("detail_top", 40),
                            min_buyers=s.rules.get("filters", {}).get("min_buyers", 1),
                            headed=a.headed, profile_dir=str(Path(s.data_dir, "chrome_profile")),
                            skip=_pre_filter(s, st))
    print(f"{len(offers)} Angebote gelesen, {_store(st, s, offers)} Kandidaten gespeichert.")
    if s.rules.get("filters", {}).get("skip_existing_in_base", True):
        _check_base(s, base, st)
    _auto_approve(s, st)
    _write_review(s, st)


def cmd_import_csv(s, base, st, a):
    """Erwartete Spalten (Groß-/Kleinschreibung egal): offer_id, url, title, price, buyers, ean, mpn, seller."""
    offers = []
    with open(a.file, encoding="utf-8-sig") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        reader = csv.DictReader(fh, dialect=csv.Sniffer().sniff(sample, delimiters=",;\t"))
        for r in reader:
            r = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            offers.append(SimpleNamespace(
                offer_id=r.get("offer_id") or r.get("offerid") or r.get("id"),
                url=r.get("url", ""), title=r.get("title") or r.get("name", ""),
                price=float(r["price"].replace(",", ".")) if r.get("price") else None,
                buyers=int(float(r.get("buyers") or r.get("popularity") or 0)),
                ean=r.get("ean") or r.get("gtin") or None, mpn=r.get("mpn") or r.get("sku") or None,
                seller=r.get("seller", ""), main_image=r.get("image") or None))
    print(f"{len(offers)} Zeilen gelesen, {_store(st, s, offers)} Kandidaten gespeichert.")
    if s.rules.get("filters", {}).get("skip_existing_in_base", True):
        _check_base(s, base, st)
    _auto_approve(s, st)
    _write_review(s, st)


def cmd_refresh_texts(s, base, st, a):
    """Titel und Beschreibung aller vom Skript angelegten Sets neu erzeugen."""
    rows = [r for r in st.by_status("erstellt") if r["set_id"] and r["single_id"]]
    if not a.only_incomplete:
        rows = rows[: a.limit or None]
        print(f"{len(rows)} Sets werden {'(Testlauf) ' if a.dry_run else ''}überarbeitet.")
    ok = 0
    if a.only_incomplete:
        print("Suche Sets mit unvollständiger Beschreibung (Ersatzvorlage) ...")
        rows = [r for r in rows if builder.is_incomplete(base, s, r["set_id"])]
        rows = rows[: a.limit or None]
        print(f"{len(rows)} gefunden und werden {'(Testlauf) ' if a.dry_run else ''}überarbeitet.")
    for r in rows:
        try:
            res = builder.refresh_texts(base, s, r, dry_run=a.dry_run, layout_only=a.layout_only)
            ok += 1
            print(f"  Set {res['set_id']} ({len(res['title'])} Z.): {res['title']}")
        except Exception as exc:
            print(f"  Set {r['set_id']}: fehlgeschlagen ({exc})")
    print(f"{ok} von {len(rows)} erledigt. Vorschau: data\\images\\<Set-ID>_neu.html")


def cmd_models(s, base, st, a):
    from . import llm
    print(f"KI-Anbieter: {s.ai_provider}")
    try:
        for m in llm.list_models(s):
            print("  " + m)
    except Exception as exc:
        print(f"  Abfrage fehlgeschlagen: {exc}")


def cmd_review(s, base, st, a):
    _write_review(s, st)


def cmd_approve(s, base, st, a):
    path = Path(s.out_dir, "review.csv")
    yes = no = 0
    with path.open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            v = (r.get("freigabe") or "").strip().lower()
            if v in ("x", "j", "ja", "y", "1", "t", "tak"):
                st.set_status(r["offer_id"], "freigegeben"); yes += 1
            elif v in ("n", "nein", "nie", "0"):
                st.set_status(r["offer_id"], "abgelehnt"); no += 1
    st.commit()
    print(f"{yes} freigegeben, {no} abgelehnt.")


def _check_tag(s, base) -> bool:
    tag = s.rules.get("listing_tag")
    if not tag:
        return True
    try:
        names = base.tag_names()
    except Exception as exc:
        print(f"Tags konnten nicht gelesen werden ({exc}), versuche es trotzdem.")
        return True
    if tag in names:
        return True
    print(f"\nDer Tag '{tag}' existiert in Base noch nicht. Base nimmt nur vorhandene Tags an.")
    print("Bitte in Base unter Produkte einmal einen Tag mit genau diesem Namen anlegen,")
    print("oder in rules.yaml bei listing_tag einen vorhandenen Tag eintragen.")
    print("Vorhandene Tags: " + (", ".join(n for n in names if n) or "(keine)"))
    return False


def cmd_build(s, base, st, a):
    if not a.dry_run and not _check_tag(s, base):
        return
    if s.rules.get("filters", {}).get("skip_existing_in_base", True):
        _check_base(s, base, st)
    # nicht_gefunden wird erneut versucht, falls du das Produkt inzwischen importiert hast
    rows = st.by_status("freigegeben", "nicht_gefunden", "fehler")[: a.limit or None]
    if not rows:
        print("Keine freigegebenen Kandidaten.")
        return
    report = []
    for c in rows:
        print(f"\n> {c['title']} ({c['buyers']} Käufer)")
        try:
            src, how = builder.find_source_product(base, s, c["ean"], c["mpn"], c["title"])
            if not src:
                print(f"  übersprungen: {how}")
                if not a.dry_run:
                    st.set_status(c["offer_id"], "nicht_gefunden", note=how)
                continue
            print(f"  Großhändler: {src['name']} ({how})")
            single_id = "(Testlauf)" if a.dry_run else builder.ensure_single(base, s, src)
            res = builder.create_set(base, s, c, src, single_id, dry_run=a.dry_run)
            price = f"{res['price']} zł" if res.get("price") else "Preis über deine Automatisierung"
            if res.get("note") == "Set existierte bereits":
                print(f"  Set {res['set_id']} mit SKU {res['sku']} gibt es schon in Base, als erledigt markiert.")
            else:
                print(f"  Set {res['set_id']}: {res['sku']} | {res['title']} | {price} | {res['images']} Bilder")
                print(f"  Hauptbild: {res['preview']}")
            report.append({**res, "offer": c["url"]})
            if not a.dry_run:
                st.set_status(c["offer_id"], "erstellt", single_id=single_id, set_id=res["set_id"],
                              note=res.get("note", ""))
        except Exception as exc:
            print(f"  FEHLER: {str(exc)[:200]}")
            traceback.print_exc()
            if not a.dry_run:
                st.set_status(c["offer_id"], "fehler", note=str(exc)[:500])
        st.commit()
    if report:
        fh, out = _open_csv(Path(s.out_dir, "build_report.csv"))
        with fh:
            w = csv.DictWriter(fh, list(report[0].keys()), delimiter=";", extrasaction="ignore")
            w.writeheader(); w.writerows(report)
        print(f"\nBericht: {out}")


def cmd_sets3(s, base, st, a):
    """3er-Sets für alle Großhändler-Produkte anlegen, die schon ein 2er-Set haben."""
    import copy
    cfg = s.rules.get("triple_sets") or {}
    count, suffix = int(cfg.get("count", 3)), cfg.get("sku_suffix", "x3")
    if not a.dry_run and not _check_tag(s, base):
        return
    print(f"Suche Produkte mit 2er-Set ohne {count}er-Set (Lager: {cfg.get('warehouse_name') or 'alle'}) ...")
    todo, stats = builder.multi_set_candidates(base, s, int(s.rules.get("set_count", 2)), count,
                                               cfg.get("warehouse_name", ""), suffix)
    print(f"  {stats['sets']} 2er-Sets im Katalog, {stats['wholesaler']} davon mit Produkten dieses Großhändlers "
          f"(auf Lager), {stats['done']} haben schon ein {count}er-Set, {len(todo)} offen.")
    limit = a.limit or int(cfg.get("limit_per_run", 0) or 0)
    if limit and len(todo) > limit:
        print(f"  In diesem Lauf werden {limit} angelegt, der Rest beim nächsten Mal.")
        todo = todo[:limit]
    rules = copy.deepcopy(s.rules)
    rules["set_count"], rules["set_sku_suffix"] = count, suffix
    s3 = copy.copy(s)
    s3.rules = rules
    report, ok = [], 0
    for t in todo:
        src = t["src"]
        print(f"\n> {src['name'][:80]}")
        try:
            res = builder.create_set(base, s3, {"title": t["set_name"], "price": None}, src,
                                     t["single_id"], dry_run=a.dry_run)
            if res.get("note") == "Set existierte bereits":
                print(f"  {res['sku']} gibt es schon in Base.")
                continue
            print(f"  Set {res['set_id']}: {res['sku']} | {res['title']} | {res['images']} Bilder")
            print(f"  Hauptbild: {res['preview']}")
            report.append(res)
            ok += 1
        except Exception as exc:
            print(f"  FEHLER: {str(exc)[:200]}")
    if report:
        fh, out = _open_csv(Path(s.out_dir, f"build_report_{count}er.csv"))
        with fh:
            w = csv.DictWriter(fh, list(report[0].keys()), delimiter=";", extrasaction="ignore")
            w.writeheader(); w.writerows(report)
        print(f"\nBericht: {out}")
    print(f"\n{ok} {count}er-Sets {'als Vorschau erstellt' if a.dry_run else 'angelegt'}.")


def cmd_catalog_sets(s, base, st, a):
    """Sets für ALLE Produkte eines Großhändlers anlegen, ohne Allegro-Recherche."""
    import copy
    import datetime as dt
    cfg = s.rules.get("catalog_sets") or {}
    if not cfg.get("warehouse_name"):
        print("In der rules.yaml fehlt catalog_sets: warehouse_name.")
        return
    count, suffix = int(cfg.get("count", 2)), cfg.get("sku_suffix", "x2")
    log_dir = Path(s.out_dir, "Protokolle")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{dt.datetime.now():%Y-%m-%d_%H%M}{'_Test' if a.dry_run else ''}.txt"
    fh = log_path.open("w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    try:
        print(f"=== {count}er-Sets für alle Produkte aus Lager „{cfg['warehouse_name']}“, "
              f"{dt.datetime.now():%d.%m.%Y %H:%M} ===\n")
        if not a.dry_run and not _check_tag(s, base):
            return
        todo, stats = builder.catalog_set_candidates(base, s, count, cfg["warehouse_name"], suffix)
        print(f"  {stats['singles']} Einzelprodukte im Katalog, {stats['wholesaler']} davon von "
              f"{cfg['warehouse_name']} auf Lager, {stats['done']} haben schon ein {count}er-Set, {len(todo)} offen.")
        limit = a.limit or int(cfg.get("limit_per_run", 0) or 0)
        if limit and len(todo) > limit:
            print(f"  In diesem Lauf werden {limit} angelegt, der Rest beim nächsten Mal.")
            todo = todo[:limit]
        rules = copy.deepcopy(s.rules)
        rules["set_count"], rules["set_sku_suffix"] = count, suffix
        s2 = copy.copy(s)
        s2.rules = rules
        report, ok, fails = [], 0, 0
        for t in todo:
            src = t["src"]
            print(f"\n> {src['name'][:80]} (Lager {t['stock']:g})")
            try:
                res = builder.create_set(base, s2, {"price": None}, src, t["single_id"], dry_run=a.dry_run)
                if res.get("note") == "Set existierte bereits":
                    print(f"  {res['sku']} gibt es schon in Base.")
                    continue
                print(f"  Set {res['set_id']}: {res['sku']} | {res['title']} | {res['images']} Bilder")
                report.append(res)
                ok += 1
                fails = 0
            except Exception as exc:
                print(f"  FEHLER: {str(exc)[:200]}")
                fails += 1
                if "KI nicht verfügbar" in str(exc) and fails >= 3:
                    print("\nGemini antwortet nicht mehr (vermutlich Tageslimit). Lauf beendet, "
                          "der Rest folgt beim nächsten Mal.")
                    break
        if report:
            fh2, out = _open_csv(Path(s.out_dir, "build_report.csv"))
            with fh2:
                w = csv.DictWriter(fh2, list(report[0].keys()), delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(report)
            print(f"\nBericht: {out}")
        print(f"\n=== Fertig: {ok} {count}er-Sets {'als Vorschau erstellt' if a.dry_run else 'angelegt'}. ===")
        print(f"Protokoll: {log_path}")
    except BaseException as exc:
        import traceback
        if not isinstance(exc, KeyboardInterrupt):
            print("\n=== Abbruch wegen Fehler ===")
            traceback.print_exc(file=sys.stdout)
        raise
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        fh.close()


def cmd_catalog_all(s, base, st, a):
    """Alle Komplett-Läufe in einem: Katalog EINMAL lesen, dann die Großhändler abwechselnd bedienen
    (je ein Textpaket pro Großhändler), mehrere Produkte pro KI-Aufruf, Zeitlimit für die Cloud."""
    import copy
    import datetime as dt
    import time as _time
    from . import llm, texts
    from .config import ROOT
    deadline = _time.time() + a.max_minutes * 60
    log_dir = Path(ROOT, "Protokolle")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{dt.datetime.now():%Y-%m-%d_%H%M}{'_Test' if a.dry_run else ''}.txt"
    fh = log_path.open("w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    try:
        profiles = [p.strip().strip("/") for p in a.profiles.split(",") if p.strip()]
        print(f"=== Sets für {', '.join(profiles)} – {dt.datetime.now():%d.%m.%Y %H:%M} ===\n")
        print("Lese den Base-Katalog (einmal für alle) ...")
        data = builder.read_catalog(base, s.target_inventory_id)
        jobs = []
        for p in profiles:
            sp = load_settings(rules_file=f"{p}/rules.yaml")
            cfg = sp.rules.get("catalog_sets") or {}
            if not cfg.get("warehouse_name"):
                print(f"{p}: catalog_sets/warehouse_name fehlt, übersprungen.")
                continue
            if not a.dry_run and not _check_tag(sp, base):
                continue
            count, suffix = int(cfg.get("count", 2)), cfg.get("sku_suffix", "x2")
            try:
                todo, stats = builder.catalog_set_candidates(base, sp, count, cfg["warehouse_name"], suffix, data=data)
            except RuntimeError as exc:
                print(f"{p}: {exc}")
                continue
            limit = a.limit or int(cfg.get("limit_per_run", 0) or 0)
            # optional: höchstens count_limit_per_run Sets mit „count“ Stück, der Rest des Kontingents
            # geht an Sets mit fill_count Stück (z. B. 3er-Sets, wenn die 2er-Sets fast alle fertig sind)
            main_cap = int(cfg.get("count_limit_per_run", 0) or 0)
            if a.limit and main_cap:
                main_cap = min(main_cap, a.limit)
            caps = [x for x in (main_cap, limit) if x]
            take = min([len(todo)] + caps)
            print(f"{p}: {stats['wholesaler']} Produkte auf Lager, {stats['done']} mit {count}er-Set, "
                  f"{len(todo)} offen, in diesem Lauf {take}.")
            todo = todo[:take]
            parts = [(count, suffix, todo)]
            fill = int(cfg.get("fill_count", 0) or 0)
            rest = (limit - take) if limit else 0
            if fill and rest > 0:
                fsuffix = cfg.get("fill_sku_suffix", f"x{fill}")
                try:
                    ftodo, fstats = builder.catalog_set_candidates(base, sp, fill, cfg["warehouse_name"], fsuffix,
                                                                   data=data, min_stock=fill)
                except RuntimeError as exc:
                    print(f"{p}: {exc}")
                    ftodo, fstats = [], {"done": 0}
                print(f"{p}: {fstats['done']} mit {fill}er-Set, {len(ftodo)} offen (mind. {fill} Stück auf Lager), "
                      f"in diesem Lauf {min(rest, len(ftodo))}.")
                parts.append((fill, fsuffix, ftodo[:rest]))
            for cnt, sfx, items in parts:
                if not items:
                    continue
                rules = copy.deepcopy(sp.rules)
                rules["set_count"], rules["set_sku_suffix"] = cnt, sfx
                sp2 = copy.copy(sp)
                sp2.rules = rules
                desc = rules.get("description") or {}
                jobs.append({"name": p if cnt == count else f"{p} {cnt}er", "main": cnt == count, "s": sp2, "todo": items, "count": cnt,
                             "batch": max(1, int(desc.get("batch_size", 3))), "desc": desc,
                             "ok": 0, "fail": 0, "report": [], "stop": False})
        print()
        stop_all = False
        while not stop_all and any(j["todo"] and not j["stop"] for j in jobs):
            for j in jobs:
                if j["stop"] or not j["todo"]:
                    continue
                if _time.time() > deadline:
                    print("\nZeitlimit für diesen Lauf erreicht, der Rest folgt beim nächsten Mal.")
                    stop_all = True
                    break
                chunk, j["todo"] = j["todo"][:j["batch"]], j["todo"][j["batch"]:]
                d = j["desc"]
                results = texts.generate_batch([t["src"] for t in chunk], j["count"], j["s"],
                                               closing_tpl=d.get("closing_line"),
                                               banned_words=d.get("title_banned_words", []),
                                               brand_in_title=d.get("brand_in_title", False))
                for t, txt in zip(chunk, results):
                    print(f"[{j['name']}] {t['src']['name'][:70]}")
                    try:
                        res = builder.create_set(base, j["s"], {"price": None}, t["src"], t["single_id"],
                                                 dry_run=a.dry_run, text=txt)
                        if res.get("note") == "Set existierte bereits":
                            print(f"  {res['sku']} gibt es schon.")
                            continue
                        print(f"  {res['sku']} | {res['title']} | {res['images']} Bilder")
                        j["report"].append(res)
                        j["ok"] += 1
                    except Exception as exc:
                        j["fail"] += 1
                        print(f"  FEHLER: {str(exc)[:200]}")
                        if "KI nicht verfügbar" in str(exc) and llm.all_exhausted(j["s"]):
                            print("\nAlle Gemini-Schlüssel haben ihr Tageslimit erreicht. Lauf beendet.")
                            stop_all = True
                            break
                if stop_all:
                    break
        print("\n=== Ergebnis ===")
        for j in jobs:
            print(f"  {j['name']}: {j['ok']} Sets {'als Vorschau' if a.dry_run else 'angelegt'}"
                  f"{(', ' + str(j['fail']) + ' Fehler') if j['fail'] else ''}, {len(j['todo'])} offen geblieben")
            if j["report"]:
                name = "build_report.csv" if j["main"] else f"build_report_{j['count']}er.csv"
                fh2, out = _open_csv(Path(j["s"].out_dir, name))
                with fh2:
                    w = csv.DictWriter(fh2, list(j["report"][0].keys()), delimiter=";", extrasaction="ignore")
                    w.writeheader(); w.writerows(j["report"])
        print(f"Protokoll: {log_path}")
    except BaseException as exc:
        if not isinstance(exc, KeyboardInterrupt):
            print("\n=== Abbruch wegen Fehler ===")
            traceback.print_exc(file=sys.stdout)
        raise
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        fh.close()


def cmd_run(s, base, st, a):
    a.headed = False
    cmd_scan(s, base, st, a)
    cmd_build(s, base, st, a)


class _Tee:
    """Konsolenausgabe zusätzlich in eine Logdatei schreiben."""
    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, text):
        self.stream.write(text)
        self.fh.write(text)
        self.fh.flush()

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def _ask(question: str) -> str:
    try:
        return input(question).strip().lower()
    except EOFError:  # ohne Konsole (z. B. Hintergrundlauf) nicht warten
        return ""


def cmd_daily(s, base, st, a):
    """Täglicher Ablauf: Scan, Abgleich, ggf. Import abwarten, Sets bauen, Bericht."""
    import datetime as dt
    import os
    from types import SimpleNamespace

    log_dir = Path(s.out_dir, "Protokolle")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{dt.datetime.now():%Y-%m-%d_%H%M}.txt"
    fh = log_path.open("w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    try:
        before = st.db.execute("SELECT COUNT(*) FROM candidates WHERE status='erstellt'").fetchone()[0]
        print(f"=== Täglicher Lauf {dt.datetime.now():%d.%m.%Y %H:%M} ===\n")

        print("[1/3] Allegro-Shops lesen ...")
        cmd_scan(s, base, st, SimpleNamespace(headed=not a.background))

        missing = st.db.execute(
            "SELECT COUNT(*) FROM candidates WHERE status IN ('freigegeben','neu') "
            "AND note LIKE 'noch nicht aus Base Connect%'").fetchone()[0]
        if missing and not a.background:
            print(f"\n[2/3] {missing} Produkte fehlen noch in deinem Base-Katalog.")
            csv_path = getattr(s, "missing_csv", None) or Path(s.out_dir, "fehlt_in_base.csv")
            try:
                os.startfile(csv_path)  # öffnet die Liste in Excel (nur Windows)
            except Exception:
                print(f"      Liste: {csv_path}")
            _ask("      Bitte diese EANs in Base aus Base Connect importieren und danach Enter drücken ... ")
        else:
            print("\n[2/3] Kein Import nötig." if not missing else
                  f"\n[2/3] {missing} Produkte fehlen in Base (Hintergrundlauf, wird beim nächsten Mal erneut versucht).")

        print("\n[3/3] Sets bauen ...")
        cmd_build(s, base, st, SimpleNamespace(dry_run=False, limit=0))

        if (s.rules.get("triple_sets") or {}).get("in_daily"):
            print("\n[3b] 3er-Sets bauen ...")
            try:
                cmd_sets3(s, base, st, SimpleNamespace(dry_run=False, limit=0))
            except Exception as exc:
                print(f"  3er-Sets übersprungen: {exc}")

        still = st.db.execute("SELECT COUNT(*) FROM candidates WHERE status='nicht_gefunden'").fetchone()[0]
        if still and not a.background:
            if _ask(f"\n{still} Produkte gibt es in Base Connect nicht. Dauerhaft überspringen? (j/n) ") in ("j", "ja", "t", "tak", "y"):
                cmd_skip_missing(s, base, st, SimpleNamespace())

        after = st.db.execute("SELECT COUNT(*) FROM candidates WHERE status='erstellt'").fetchone()[0]
        errors = st.db.execute("SELECT COUNT(*) FROM candidates WHERE status='fehler'").fetchone()[0]
        print(f"\n=== Fertig: {after - before} neue Sets angelegt"
              f"{f', {errors} mit Fehler (werden beim nächsten Lauf erneut versucht)' if errors else ''}. ===")
        print(f"Protokoll: {log_path}")
    except BaseException as exc:
        import traceback
        if not isinstance(exc, KeyboardInterrupt):
            print("\n=== Abbruch wegen Fehler ===")
            traceback.print_exc(file=sys.stdout)
            print(f"Protokoll: {log_path}")
        raise
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        fh.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="allegro_sets")
    p.add_argument("--rules", default="p1/rules.yaml",
                   help="Regeldatei des Großhändlers, z. B. p1/rules.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sp = sub.add_parser("scan"); sp.add_argument("--headed", action="store_true")
    sp = sub.add_parser("import-csv"); sp.add_argument("file")
    sub.add_parser("approve")
    sub.add_parser("review")
    sub.add_parser("check-base")
    sub.add_parser("skip-missing")
    sub.add_parser("models")
    sp = sub.add_parser("daily")
    sp.add_argument("--background", action="store_true",
                    help="ohne Rückfragen und ohne sichtbaren Browser (für unbeaufsichtigte Läufe)")
    sp = sub.add_parser("refresh-texts")
    sp.add_argument("--dry-run", action="store_true", help="nur Vorschau, nichts in Base ändern")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--only-incomplete", action="store_true",
                    help="nur Sets, deren Beschreibung aus der Ersatzvorlage stammt (ohne Cechy Produktu)")
    sp.add_argument("--layout-only", action="store_true",
                    help="vorhandene Texte behalten, nur Layout mit Bildern erneuern (ohne KI)")
    sp = sub.add_parser("catalog-all", help="mehrere Komplett-Läufe in einem (Katalog nur einmal lesen)")
    sp.add_argument("--profiles", default="")
    sp.add_argument("--dry-run", action="store_true", help="nichts in Base anlegen, nur Vorschau")
    sp.add_argument("--limit", type=int, default=0, help="Sets pro Großhändler (0 = limit_per_run aus rules.yaml)")
    sp.add_argument("--max-minutes", type=int, default=320, help="danach keine neuen Sets mehr beginnen")
    sp = sub.add_parser("catalog-sets", help="Sets für alle Produkte eines Großhändlers (ohne Allegro-Recherche)")
    sp.add_argument("--dry-run", action="store_true", help="nichts in Base anlegen, nur Vorschau")
    sp.add_argument("--limit", type=int, default=0)
    sp = sub.add_parser("sets3", help="3er-Sets für Produkte mit 2er-Set anlegen")
    sp.add_argument("--dry-run", action="store_true", help="nichts in Base anlegen, nur Vorschau")
    sp.add_argument("--limit", type=int, default=0)
    for name in ("build", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--dry-run", action="store_true", help="nichts in Base anlegen, nur Vorschau")
        sp.add_argument("--limit", type=int, default=0)
    a = p.parse_args(argv)

    s = load_settings(only_token=(a.cmd in ("setup", "models")), rules_file=a.rules)
    if s.profile:
        print(f"Profil: {s.profile} ({a.rules})")
    base = BaseClient(s.base_token)
    st = State(Path(s.data_dir, "state.sqlite"))
    {"setup": cmd_setup, "scan": cmd_scan, "import-csv": cmd_import_csv, "approve": cmd_approve, "review": cmd_review, "check-base": cmd_check_base, "skip-missing": cmd_skip_missing, "models": cmd_models, "daily": cmd_daily, "refresh-texts": cmd_refresh_texts,
     "build": cmd_build, "run": cmd_run, "sets3": cmd_sets3, "catalog-sets": cmd_catalog_sets, "catalog-all": cmd_catalog_all}[a.cmd](s, base, st, a)


if __name__ == "__main__":
    sys.exit(main())
