"""Titel und Beschreibung für das Set auf Allegro erzeugen.

Die Beschreibung folgt der festen Vorlage: Allegro-Abschnitte (section/item-12/text-item)
in dieser Reihenfolge:
  1. Einleitung (h2 + p)
  2. Cechy Produktu (Liste mit fettem Stichwort – Erklärung)
  3. Specyfikacja (Liste mit fettem Merkmal: Wert.)
  4.–6. drei Themenabschnitte (h2 + p), der letzte endet mit der Markenzeile
Die KI liefert nur die Inhalte als JSON, das HTML baut dieses Modul selbst.
"""
from __future__ import annotations

import html
import json
import re

TITLE_MAX = 75  # Allegro-Limit für Angebotstitel
TITLE_MIN = 55  # kürzere Titel verschenken Suchwörter, dann wird nachgebessert

PROMPT = """Du schreibst einen Allegro-Angebotstext auf Polnisch.

Produktdaten (Einzelprodukt aus dem Großhändlerkatalog):
{product}

Verkauft wird ein Set aus {count} identischen Stück dieses Produkts.

Liefere die Inhalte als JSON mit genau diesen Feldern:
{{
  "title": "Angebotstitel",
  "brand": "Marke, falls aus den Daten erkennbar, sonst null",
  "intro_h2": "Überschrift der Einleitung",
  "intro_p": "Einleitungsabsatz",
  "features": [{{"label": "Stichwort", "text": "Erklärung"}}],
  "specs": [{{"label": "Merkmal", "value": "Wert"}}],
  "sections": [{{"h2": "Überschrift", "p": "Absatz"}}]
}}

Regeln:
- title: Allegro-Suchtitel, {title_min} bis {title_max} Zeichen, den Platz möglichst ausnutzen.
  Aufbau: Hauptsuchbegriff (Produktart) zuerst, dann die wichtigsten Merkmale und Suchwörter, nach denen
  Käufer auf Allegro suchen: Größe/Maße, Material, Farbe, Einsatzzweck, Synonyme und passende Zielobjekte
  (z. B. "do garnka patelni"). Orientiere dich an den Suchwörtern im Konkurrenztitel (market_title),
  kopiere ihn aber nicht. Ein bis zwei Schlüsselwörter dürfen in GROSSBUCHSTABEN stehen wie auf Allegro üblich.
  {brand_rule}
  KEIN Set-Hinweis im Titel: kein "Zestaw", kein "{count}x", kein "{count} szt.", kein "komplet".
  Keine Sonderzeichen wie !, keine Wörter wie "hit", "promocja", "najlepszy". Keine Wortwiederholungen.
  Maßangaben nur vollständig ("30x13x8 cm"), passt eine Angabe nicht mehr hinein, lass sie ganz weg.
- intro_h2: Produktname mit Stückzahl und Marke, z. B. "Szklane Zaparzacze Do Herbaty I Ziół {count} Sztuki Lumio".
- intro_p: 3 bis 4 Sätze. Beginnt wie "Praktyczny zestaw <strong>{count} …</strong> …". Genau eine
  <strong>-Hervorhebung mit Stückzahl und Produktart. Was ist es, woraus, wofür, was ist enthalten.
- features: 6 bis 8 Einträge. label kurz (2 bis 4 Wörter), der erste ist "Zestaw {count} …".
  text ein Halbsatz ohne Punkt am Ende.
- specs: 6 bis 10 Einträge, nur Fakten aus den Produktdaten. Der erste ist
  {{"label": "Rodzaj produktu", ...}}, der zweite die Stückzahl im Set, z. B.
  {{"label": "Liczba sztuk w zestawie", "value": "{count} sztuki"}}. Maße beziehen sich auf ein Stück
  ("Wymiary jednego …"). Zuletzt "Marka", falls bekannt.
- sections: genau 3 Abschnitte. 1. Anwendung/Nutzen, 2. Pflege oder praktische Handhabung,
  3. Design oder Geschenkidee. Je 2 bis 4 Sätze.
- specs-Format: Einheiten gehören in den Wert, nicht ins Merkmal ("Średnica jednego elementu": "26 cm",
  nicht "Średnica (cm)": "26"). Dezimalzahlen mit Komma ("9,5 cm"). Werte klein beginnen,
  außer Eigennamen und Marken.
- Erwähne in den sections KEINE Herkunft, Qualitätsstandards oder Normen ("europejskie standardy",
  "oryginalny produkt"), das steht bereits in einer festen Schlusszeile.
- Erfinde keine technischen Daten, Materialien oder Zertifikate, die nicht in den Produktdaten stehen.
- Natürlicher, sachlicher Stil, keine Übertreibungen, möglichst wenige Gedankenstriche im Fließtext.

Antworte ausschließlich mit dem JSON."""

SECTION = """<section class="section">
    <div class="item item-12">
        <section class="text-item">
{inner}
        </section>
    </div>
</section>"""

SET_HINT_RE = re.compile(
    r"^\s*(?:(?:zestaw|komplet)\b|\d+\s?[x×](?=\s|[^\W\d])|x\s?\d+\b|\d+\s?(?:szt|sztuk)\b\.?)\s*[:,-]?\s*", re.I)

ALLOWED_INLINE = re.compile(r"</?(?:strong|b)>")


# ------------------------------------------------------------------ Hilfen
def _strip_words(title: str, banned) -> str:
    for w in banned or []:
        if w:
            title = re.sub(rf"(?i)(?<!\w){re.escape(w)}(?!\w)", " ", title)
    return re.sub(r"\s+", " ", title).strip(" ,;-")


def _clean_title(title: str, banned=None) -> str:
    title = re.sub(r"\s+", " ", title).strip().strip('"')
    title = _strip_words(title, banned)
    for _ in range(3):  # Set-Hinweise vorn entfernen, z. B. "Zestaw: 2 szt."
        title = SET_HINT_RE.sub("", title, count=1).strip()
    title = title[:1].upper() + title[1:]
    if len(title) <= TITLE_MAX:
        return title
    cut = title[:TITLE_MAX].rsplit(" ", 1)[0]
    return cut.rstrip(",;-")


UNITS = {"mm", "cm", "m", "ml", "l", "g", "kg", "w", "v", "mah", "szt", "szt.", "x", "×"}


def _title_case(text: str) -> str:
    """Überschriften wie in der Vorlage: jedes Wort groß ('Do', 'I', 'W', 'Na').
    Einheiten bleiben nur direkt hinter einer Zahl klein ('26 cm', '500 ml')."""
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    out = []
    for i, w in enumerate(words):
        prev_num = i > 0 and re.search(r"\d$", words[i - 1])
        core = w.lower().rstrip(",.;:)")
        out.append(w if (prev_num and core in UNITS) else w[:1].upper() + w[1:])
    return " ".join(out)


def _esc(text: str, allow_strong: bool = False) -> str:
    """HTML escapen, optional <strong> erlauben."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not allow_strong:
        return html.escape(re.sub(r"<[^>]+>", "", text), quote=False)
    parts = re.split(r"(</?(?:strong|b)>)", text)
    return "".join(p if ALLOWED_INLINE.fullmatch(p) else html.escape(re.sub(r"<[^>]+>", "", p), quote=False)
                   for p in parts)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _h2(t): return f"            <h2>{_esc(_title_case(t))}</h2>"
def _p(t, strong=True): return f"            <p>{_bold(_esc(t, allow_strong=strong))}</p>"


def _ul(items: list[str]) -> str:
    li = "\n".join(f"                <li>{i}</li>" for i in items)
    return f"            <ul>\n{li}\n            </ul>"


# ------------------------------------------------------------------ Aufbau
FULL = """<section class="section">
    <div class="item item-12">
        <section class="text-item">
{text}
        </section>
    </div>
</section>"""

IMG_LEFT = """<section class="section">
    <div class="item item-6">
        <section class="image-item">
            <img src="{img}" />
        </section>
    </div>

    <div class="item item-6">
        <section class="text-item">
{text}
        </section>
    </div>
</section>"""

IMG_RIGHT = """<section class="section">
    <div class="item item-6">
        <section class="text-item">
{text}
        </section>
    </div>

    <div class="item item-6">
        <section class="image-item">
            <img src="{img}" />
        </section>
    </div>
</section>"""

IMAGE_SLOTS = 5   # Bilder 1 bis 5 werden der Reihe nach verteilt
LEFT_SLOTS = 3    # die ersten drei Bilder stehen links, Bild 4 und 5 rechts


def layout(title_inner: str, slots: list[str], images: list[str] | None = None) -> str:
    """Allegro-Layout: Titel allein, dann Bild/Text im Wechsel, letzter Abschnitt volle Breite.
    slots: Text-Blöcke in Reihenfolge (Einleitung, Cechy, Specyfikacja, Abschnitte ...)."""
    images = [u for u in (images or []) if u]
    blocks = [FULL.format(text=title_inner)]
    for i, inner in enumerate(slots):
        last = i == len(slots) - 1 and len(slots) > 1
        if i < IMAGE_SLOTS and i < len(images) and not last:
            tpl = IMG_LEFT if i < LEFT_SLOTS else IMG_RIGHT
            blocks.append(tpl.format(img=html.escape(images[i], quote=True), text=inner))
        else:
            blocks.append(FULL.format(text=inner))
    return "".join(blocks)


def render(d: dict, closing_line: str | None, images: list[str] | None = None) -> str:
    title_inner = _h2(d["intro_h2"])
    slots = [_p(_sentence(d["intro_p"]))]

    if d.get("features"):
        items = [f"✅ <b>{_esc(f['label'])}</b> – {_esc(f['text']).rstrip('.')}." for f in d["features"]]
        slots.append(_h2("Cechy Produktu") + "\n" + _ul(items))

    if d.get("specs"):
        items = [f"<b>{_esc(sp['label']).rstrip(':')}:</b> "
                 f"{_sentence(re.sub(r'(?<![\d.])(\d+)\.(\d+)(?![.\d])', r'\1,\2', _esc(sp['value'])))}" for sp in d["specs"]]
        slots.append(_h2("Specyfikacja") + "\n" + _ul(items))

    sections = d.get("sections") or []
    for i, sec in enumerate(sections):
        inner = _h2(sec["h2"]) + "\n" + _p(_sentence(sec["p"]))
        if i == len(sections) - 1 and closing_line:
            inner += "\n" + f"            <p>{_bold(closing_line)}</p>"
        slots.append(inner)
    if not sections and closing_line:
        slots.append(f"            <p>{_bold(closing_line)}</p>")
    return layout(title_inner, slots, images)


def relayout(old_html: str, images: list[str] | None = None) -> str | None:
    """Vorhandene Beschreibung (vom Skript erzeugt) ins neue Layout mit Bildern umbauen, ohne KI."""
    inners = re.findall(r'<section class="text-item">\s*(.*?)\s*</section>', old_html or "", re.S)
    if not inners:
        return None
    first = inners[0]
    m = re.match(r"\s*(<h2>.*?</h2>)\s*(.*)", first, re.S)
    if not m:
        return None
    title_inner = "            " + m.group(1)
    slots = ([("            " + m.group(2).strip())] if m.group(2).strip() else []) + \
            ["            " + x.strip() for x in inners[1:]]
    return layout(title_inner, [_bold(x) for x in slots], images)


def _bold(text: str) -> str:
    """Allegro erlaubt <b>, nicht <strong>."""
    return re.sub(r"<(/?)strong>", r"<\1b>", text or "")


def _closing(brand: str | None, template: str | None) -> str | None:
    if not brand or not template:
        return None
    return template.replace("{brand}", _esc(brand))


def _fallback(product: dict, name: str, description: str, count: int, closing_tpl: str | None,
              banned=None) -> dict:
    plain = re.sub(r"<[^>]+>", " ", description or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    specs = [{"label": "Rodzaj produktu", "value": name},
             {"label": "Liczba sztuk w zestawie", "value": f"{count} sztuki"}]
    dims = [product.get("length"), product.get("width"), product.get("height")]
    if all(dims):
        specs.append({"label": "Wymiary jednego produktu", "value": " × ".join(f"{float(x):g}" for x in dims) + " cm"})
    d = {
        "intro_h2": f"{name} {count} Sztuki",
        "intro_p": f"Praktyczny zestaw <strong>{count} sztuk: {name}</strong>. {plain[:600]}",
        "features": [],
        "specs": specs,
        "sections": [],
    }
    return {"title": _clean_title(name, banned), "description": render(d, None), "ai": False,
            "data": d, "closing": None}


def _valid(d: dict) -> bool:
    try:
        return (bool(d.get("title")) and bool(d.get("intro_h2")) and bool(d.get("intro_p"))
                and all(f.get("label") and f.get("text") for f in d.get("features", []))
                and all(sp.get("label") and sp.get("value") for sp in d.get("specs", []))
                and all(sec.get("h2") and sec.get("p") for sec in d.get("sections", [])))
    except AttributeError:
        return False


def generate(product: dict, count: int, s, closing_tpl: str | None = None,
             market_title: str | None = None, banned_words=None, brand_in_title: bool = False) -> dict:
    """s = Settings (für KI-Anbieter und Schlüssel).
    market_title: Titel des Bestseller-Angebots auf Allegro, liefert Suchwörter.
    banned_words: Wörter, die nie im Titel stehen dürfen (Marken)."""
    from . import llm

    tf = product.get("text_fields") or {}
    name = tf.get("name") or product.get("name") or ""
    description = tf.get("description") or product.get("description") or ""
    banned = list(banned_words or [])
    if not llm.enabled(s):
        return _fallback(product, name, description, count, closing_tpl, banned)

    info = {
        "name": name,
        "market_title": market_title,
        "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", description))[:5000],
        "features": tf.get("features") or product.get("features"),
        "ean": product.get("ean"),
        "weight_kg": product.get("weight"),
        "dimensions_cm": [product.get("length"), product.get("width"), product.get("height")],
    }
    brand_rule = BRAND_IN_TITLE if brand_in_title else BRAND_NOT_IN_TITLE
    prompt = PROMPT.format(product=json.dumps(info, ensure_ascii=False, indent=1), count=count,
                           title_max=TITLE_MAX, title_min=TITLE_MIN, brand_rule=brand_rule)
    data: dict = {}
    for _ in range(2):  # bei ungültigem JSON einmal neu versuchen
        try:
            raw = llm.ask(prompt, s)
        except llm.LlmError as exc:
            print(f"  KI nicht verfügbar ({exc}), nutze Vorlage.")
            break
        data = _parse(raw)
        if _valid(data):
            break
    if not _valid(data):
        return _fallback(product, name, description, count, closing_tpl, banned)

    if getattr(s, "proofread", True):
        data = _proofread(data, s)

    if data.get("brand") and not brand_in_title:
        banned.append(str(data["brand"]))
    title = _clean_title(data["title"], banned)
    if len(title) < TITLE_MIN:  # zu kurz: einmal gezielt nachbessern lassen
        try:
            raw = llm.ask(TITLE_RETRY.format(title=title, n=len(title), title_min=TITLE_MIN, title_max=TITLE_MAX,
                                             brand_rule=brand_rule,
                                             product=json.dumps(info, ensure_ascii=False)), s, max_tokens=500)
            better = _clean_title(_parse(raw).get("title", ""), banned)
            if len(better) > len(title):
                title = better
        except llm.LlmError:
            pass
    closing = _closing(data.get("brand"), closing_tpl)
    return {"title": title, "description": render(data, closing), "ai": True, "data": data, "closing": closing}


def with_images(text: dict, images: list[str]) -> str:
    """Beschreibung eines generate()-Ergebnisses mit Bildadressen neu aufbauen."""
    return render(text["data"], text.get("closing"), images)


BRAND_NOT_IN_TITLE = "NIEMALS einen Markennamen im Titel (keine Marke, auch nicht die des Herstellers)."
BRAND_IN_TITLE = ("Die Marke des Produkts gehört in den Titel, möglichst direkt nach dem Produkttyp "
                  "(Käufer suchen gezielt nach bekannten Marken). Nur die echte Herstellermarke, keine Händlernamen.")


TITLE_RETRY = """Dieser Allegro-Titel ist mit {n} Zeichen zu kurz: "{title}"
Schreibe ihn neu mit {title_min} bis {title_max} Zeichen. Hauptsuchbegriff zuerst, dann Maße, Material,
Farbe, Einsatzzweck und passende Suchwörter. {brand_rule} Kein "Zestaw", kein "2x" oder "3x", keine Sonderzeichen.
Produktdaten: {product}
Antworte nur mit JSON: {{"title": "..."}}"""


PROOFREAD = """Jesteś korektorem języka polskiego. Popraw w poniższym JSON wyłącznie błędy ortograficzne,
gramatyczne (odmiana, związki zgody), interpunkcyjne i literówki oraz nieistniejące słowa.
Nie zmieniaj struktury, kluczy, kolejności, faktów, liczb ani znaczników <strong>. Nie dodawaj nowych treści.
Pole "title" może mieć najwyżej {title_max} znaków. Zwróć wyłącznie poprawiony JSON.

{data}"""


def _proofread(data: dict, s) -> dict:
    """Zweiter Durchgang: nur Sprachfehler korrigieren. Bei Problemen bleibt der Originaltext."""
    from . import llm
    try:
        raw = llm.ask(PROOFREAD.format(title_max=TITLE_MAX, data=json.dumps(data, ensure_ascii=False, indent=1)), s)
    except llm.LlmError:
        return data
    fixed = _parse(raw)
    same_shape = (_valid(fixed)
                  and len(fixed.get("features", [])) == len(data.get("features", []))
                  and len(fixed.get("specs", [])) == len(data.get("specs", []))
                  and len(fixed.get("sections", [])) == len(data.get("sections", [])))
    return fixed if same_shape else data


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ mehrere Produkte pro KI-Aufruf
BATCH_HEAD = """Du schreibst Allegro-Angebotstexte auf Polnisch für {n} verschiedene Produkte.
Behandle jedes Produkt vollständig unabhängig: übernimm keine Wörter, Merkmale, Maße oder Marken von
einem Produkt zum anderen. Jedes Produkt wird als Set aus {count} identischen Stück verkauft.

Produkte (jeweils mit "id"):
{products}

Liefere ein JSON-Objekt {{"items": [...]}} mit genau einem Eintrag pro Produkt, in derselben Reihenfolge.
Jeder Eintrag hat genau diese Felder:
{{
  "id": "die id des Produkts",
  "title": "Angebotstitel",
  "brand": "Marke, falls aus den Daten erkennbar, sonst null",
  "intro_h2": "Überschrift der Einleitung",
  "intro_p": "Einleitungsabsatz",
  "features": [{{"label": "Stichwort", "text": "Erklärung"}}],
  "specs": [{{"label": "Merkmal", "value": "Wert"}}],
  "sections": [{{"h2": "Überschrift", "p": "Absatz"}}]
}}

Regeln für JEDEN Eintrag:
"""

PROOFREAD_BATCH = """Jesteś korektorem języka polskiego. W poniższym JSON (lista "items") popraw w każdym
elemencie wyłącznie błędy ortograficzne, gramatyczne (odmiana, związki zgody), interpunkcyjne i literówki
oraz nieistniejące słowa. Nie zmieniaj struktury, kluczy, kolejności, pola "id", faktów, liczb ani znaczników
<strong>. Nie dodawaj nowych treści. Pole "title" może mieć najwyżej {title_max} znaków.
Zwróć wyłącznie poprawiony JSON {{"items": [...]}}.

{data}"""


def _info(product: dict, market_title=None) -> dict:
    tf = product.get("text_fields") or {}
    description = tf.get("description") or product.get("description") or ""
    return {
        "name": tf.get("name") or product.get("name") or "",
        "market_title": market_title,
        "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", description))[:3000],
        "features": tf.get("features") or product.get("features"),
        "ean": product.get("ean"),
        "weight_kg": product.get("weight"),
        "dimensions_cm": [product.get("length"), product.get("width"), product.get("height")],
    }


def _same_shape(a: dict, b: dict) -> bool:
    return (_valid(b) and len(b.get("features", [])) == len(a.get("features", []))
            and len(b.get("specs", [])) == len(a.get("specs", []))
            and len(b.get("sections", [])) == len(a.get("sections", [])))


def _quality_ok(d: dict, info: dict) -> bool:
    """Mindestanforderungen wie im Einzelprompt; sonst wird das Produkt einzeln neu erzeugt."""
    if not _valid(d):
        return False
    if not (5 <= len(d.get("features", [])) <= 9 and 5 <= len(d.get("specs", [])) <= 11
            and len(d.get("sections", [])) == 3):
        return False
    # Schutz gegen Vermischung: im Titel sollte mindestens ein Wort aus dem Produktnamen vorkommen
    name_words = {w for w in re.findall(r"\w{4,}", str(info.get("name", "")).lower())}
    title_words = set(re.findall(r"\w{4,}", str(d.get("title", "")).lower()))
    return not name_words or bool(name_words & title_words) or bool(
        {w[:5] for w in name_words} & {w[:5] for w in title_words})


def generate_batch(products: list[dict], count: int, s, closing_tpl: str | None = None,
                   banned_words=None, brand_in_title: bool = False) -> list[dict]:
    """Wie generate(), aber mehrere Produkte in einem KI-Aufruf (spart Kontingent).
    Jedes Ergebnis wird geprüft; was die Prüfung nicht besteht, wird einzeln mit generate() erzeugt,
    damit die Qualität gleich bleibt."""
    from . import llm
    if len(products) <= 1 or not llm.enabled(s):
        return [generate(p, count, s, closing_tpl, banned_words=banned_words, brand_in_title=brand_in_title)
                for p in products]
    infos = [dict(_info(p), id=str(i + 1)) for i, p in enumerate(products)]
    brand_rule = BRAND_IN_TITLE if brand_in_title else BRAND_NOT_IN_TITLE
    rules = PROMPT[PROMPT.index("Regeln:") + len("Regeln:"):PROMPT.index("Antworte ausschließlich")].strip("\n")
    prompt = (BATCH_HEAD.format(n=len(products), count=count,
                                products=json.dumps(infos, ensure_ascii=False, indent=1))
              + rules.format(count=count, title_max=TITLE_MAX, title_min=TITLE_MIN, brand_rule=brand_rule)
              + "\n\nAntworte ausschließlich mit dem JSON.")
    by_id: dict[str, dict] = {}
    try:
        raw = llm.ask(prompt, s, max_tokens=min(4000 * len(products), 24000))
        items = _parse(raw).get("items") or []
        by_id = {str(it.get("id")): it for it in items if isinstance(it, dict)}
    except llm.LlmError as exc:
        print(f"  KI nicht verfügbar ({exc}).")
        if "aller" in str(exc):          # alle Schlüssel erschöpft: Einzelversuch lohnt nicht
            return [_fallback(p, (p.get("text_fields") or {}).get("name") or "", "", count, closing_tpl,
                              list(banned_words or [])) for p in products]

    good = {i: by_id[i] for i in by_id if i in {inf["id"] for inf in infos}
            and _quality_ok(by_id[i], infos[int(i) - 1])}
    if good and getattr(s, "proofread", True):
        try:
            payload = {"items": [dict(v, id=k) for k, v in good.items()]}
            raw = llm.ask(PROOFREAD_BATCH.format(title_max=TITLE_MAX, data=json.dumps(payload, ensure_ascii=False, indent=1)),
                          s, max_tokens=min(4000 * len(good), 24000))
            for it in _parse(raw).get("items") or []:
                k = str(it.get("id"))
                if k in good and _same_shape(good[k], it):
                    good[k] = it
        except llm.LlmError:
            pass

    out = []
    for i, p in enumerate(products):
        d = good.get(str(i + 1))
        if not d:
            print(f"  Text für „{infos[i]['name'][:50]}“ wird einzeln erzeugt (Sammelantwort unvollständig).")
            out.append(generate(p, count, s, closing_tpl, banned_words=banned_words, brand_in_title=brand_in_title))
            continue
        d.pop("id", None)
        banned = list(banned_words or [])
        if d.get("brand") and not brand_in_title:
            banned.append(str(d["brand"]))
        title = _clean_title(d["title"], banned)
        if len(title) < TITLE_MIN:
            try:
                raw = llm.ask(TITLE_RETRY.format(title=title, n=len(title), title_min=TITLE_MIN, title_max=TITLE_MAX,
                                                 brand_rule=brand_rule, product=json.dumps(infos[i], ensure_ascii=False)),
                              s, max_tokens=500)
                better = _clean_title(_parse(raw).get("title", ""), banned)
                if len(better) > len(title):
                    title = better
            except llm.LlmError:
                pass
        closing = _closing(d.get("brand"), closing_tpl)
        out.append({"title": title, "description": render(d, closing), "ai": True, "data": d, "closing": closing})
    return out
