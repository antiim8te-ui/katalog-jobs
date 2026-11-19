"""Einheitlicher Zugang zu verschiedenen KI-Anbietern für die Texterstellung.

AI_PROVIDER in der .env:
  anthropic  Claude über die Anthropic API (kostenpflichtig, beste Qualität)
  gemini     Google Gemini über AI Studio (kostenlose Stufe für Flash-Modelle)
  ollama     lokales Modell auf deinem PC (kostenlos, braucht Ollama)
  none       keine KI, einfache Vorlage aus den Großhändlerdaten
"""
from __future__ import annotations

import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"


class LlmError(RuntimeError):
    pass


def ask(prompt: str, s, max_tokens: int = 4000) -> str:
    provider = (s.ai_provider or "none").lower()
    if provider == "anthropic":
        return _anthropic(prompt, s, max_tokens)
    if provider == "gemini":
        return _gemini(prompt, s, max_tokens)
    if provider == "ollama":
        return _ollama(prompt, s)
    raise LlmError("keine KI eingestellt")


def enabled(s) -> bool:
    p = (s.ai_provider or "none").lower()
    import os
    return ((p == "anthropic" and bool(s.anthropic_key))
            or (p == "gemini" and bool(s.gemini_key or os.getenv("GEMINI_API_KEYS"))) or p == "ollama")


# ---------------------------------------------------------------- Anbieter
def _anthropic(prompt, s, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=s.anthropic_key)
    msg = client.messages.create(model=s.anthropic_model, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


_EXHAUSTED: set = set()      # (Schlüssel, Modell), deren Tageskontingent in diesem Lauf aufgebraucht ist
_BAD_KEYS: set = set()


def _gemini_keys(s) -> list[str]:
    """Eigener Schlüssel des Großhändlers zuerst, dann alle aus GEMINI_API_KEYS (.env, durch Komma getrennt)."""
    import os
    keys = [s.gemini_key] + [k.strip() for k in (os.getenv("GEMINI_API_KEYS") or "").split(",")]
    keys += [os.getenv("GEMINI_API_KEY") or ""]
    out = []
    for k in keys:
        if k and k not in out and k not in _BAD_KEYS:
            out.append(k)
    return out


def all_exhausted(s) -> bool:
    """True, wenn in diesem Lauf alle Gemini-Schlüssel/Modelle ihr Tageslimit erreicht haben."""
    import os
    if (s.ai_provider or "").lower() != "gemini":
        return False
    models = [s.gemini_model] + [m.strip() for m in (os.getenv("GEMINI_FALLBACK_MODEL") or "").split(",") if m.strip()]
    return not [(k, m) for m in models for k in _gemini_keys(s) if (k, m) not in _EXHAUSTED]


def _gemini(prompt, s, max_tokens):
    """Gemini mit Schlüssel- und Modellwechsel: Ist das Tageskontingent eines Schlüssels erschöpft,
    geht es sofort mit dem nächsten weiter; ist ein Modell überlastet, mit dem Ausweichmodell
    (GEMINI_FALLBACK_MODEL). Erst wenn alle Kombinationen erschöpft sind, kommt ein Fehler."""
    import os
    if not s.gemini_model:
        raise LlmError("GEMINI_MODEL fehlt in .env (verfügbare Modelle: python -m allegro_sets models)")
    models = [s.gemini_model] + [m.strip() for m in (os.getenv("GEMINI_FALLBACK_MODEL") or "").split(",") if m.strip()]
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens,
                                 "temperature": 0.6}}
    for round_ in range(4):
        combos = [(k, m) for m in models for k in _gemini_keys(s) if (k, m) not in _EXHAUSTED]
        if not combos:
            break
        busy = False
        for key, model in combos:
            try:
                r = requests.post(f"{GEMINI_URL}/models/{model}:generateContent", json=body,
                                  headers={"x-goog-api-key": key}, timeout=180)
            except requests.RequestException as exc:
                print(f"  Gemini nicht erreichbar ({type(exc).__name__}) ...")
                busy = True
                time.sleep(5)
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                except (KeyError, IndexError):
                    raise LlmError(f"Gemini ohne Text: {str(data)[:300]}")
            txt = r.text or ""
            tag = f"{model}/Schlüssel …{key[-4:]}"
            if r.status_code == 429:
                if "PerDay" in txt or "per day" in txt.lower():
                    _EXHAUSTED.add((key, model))
                    print(f"  Tageslimit erreicht ({tag}), nächster Schlüssel.")
                else:
                    busy = True              # Minutenlimit: kurz warten, nächster Schlüssel
                    time.sleep(3)
                continue
            if r.status_code in (500, 502, 503, 504):
                busy = True
                print(f"  Gemini überlastet ({tag}), versuche den nächsten ...")
                time.sleep(3)
                continue
            if r.status_code == 404 and model != s.gemini_model:
                print(f"  Ausweichmodell {model} gibt es nicht, übersprungen.")
                for k in _gemini_keys(s):
                    _EXHAUSTED.add((k, model))
                continue
            if r.status_code in (400, 401, 403) and ("API_KEY" in txt or "API key" in txt or "PERMISSION" in txt):
                print(f"  Schlüssel …{key[-4:]} ungültig oder gesperrt, wird übersprungen.")
                _BAD_KEYS.add(key)
                continue
            raise LlmError(f"Gemini {r.status_code}: {txt[:300]}")
        if busy:
            time.sleep(20 * (round_ + 1))
    if not [(k, m) for m in models for k in _gemini_keys(s) if (k, m) not in _EXHAUSTED]:
        raise LlmError("Tageslimit aller Gemini-Schlüssel erreicht")
    raise LlmError("Gemini nach mehreren Versuchen nicht verfügbar (Überlastung)")


def _ollama(prompt, s):
    r = requests.post(f"{s.ollama_url.rstrip('/')}/api/chat", timeout=600, json={
        "model": s.ollama_model, "stream": False, "format": "json",
        "messages": [{"role": "user", "content": prompt}],
    })
    if r.status_code >= 400:
        raise LlmError(f"Ollama {r.status_code}: {r.text[:300]}")
    return r.json().get("message", {}).get("content", "")


def list_models(s) -> list[str]:
    """Für die Einrichtung: welche Modelle der eingestellte Anbieter anbietet."""
    p = (s.ai_provider or "").lower()
    if p == "gemini":
        r = requests.get(f"{GEMINI_URL}/models", headers={"x-goog-api-key": s.gemini_key},
                         params={"pageSize": 200}, timeout=60)
        r.raise_for_status()
        return [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]
    if p == "ollama":
        r = requests.get(f"{s.ollama_url.rstrip('/')}/api/tags", timeout=30)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    return []
