"""Zentrale Konfiguration: Geheimnisse aus .env, Regeln aus rules.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and not val:
        raise SystemExit(f"Fehlende Einstellung in .env: {name}")
    return val


@dataclass
class Settings:
    base_token: str
    anthropic_key: str | None
    anthropic_model: str
    ai_provider: str            # anthropic, gemini, ollama oder none
    gemini_key: str | None
    gemini_model: str | None
    ollama_url: str
    ollama_model: str
    # Woher die Einzelprodukte kommen (Base Connect Katalog oder externer Speicher)
    source_mode: str            # "inventory" oder "external_storage"
    source_id: str              # inventory_id des Connect-Katalogs ODER storage_id
    target_inventory_id: int    # dein eigener Katalog
    price_group_id: str
    warehouse_id: str | None
    text_field_suffix: str      # z. B. "" oder "|pl|allegro_12345"
    rules: dict = field(default_factory=dict)
    proofread: bool = True
    profile: str = ""           # Großhändler-Ordner, z. B. "p1" oder "p2"
    out_dir: Path = ROOT / "data"  # dort landen Protokolle, fehlt_in_base.csv, Berichte
    data_dir: Path = ROOT / "data"


DEFAULT_RULES = "p1/rules.yaml"


def load_settings(only_token: bool = False, rules_file: str = DEFAULT_RULES) -> Settings:
    """only_token=True für den Befehl setup, der die übrigen IDs erst anzeigt."""
    if not (ROOT / ".env").exists():
        raise SystemExit(f"Keine .env gefunden in {ROOT}. Bitte .env.example kopieren und als .env speichern.")
    rules_path = ROOT / rules_file
    if not rules_path.exists():
        raise SystemExit(f"Regeldatei nicht gefunden: {rules_path}")
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else {}
    need = not only_token
    s = Settings(
        base_token=_env("BASE_API_TOKEN", required=True),
        anthropic_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        ai_provider=(_env("AI_PROVIDER") or ("anthropic" if _env("ANTHROPIC_API_KEY") else "none")).strip().lower(),
        gemini_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL"),
        ollama_url=_env("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=_env("OLLAMA_MODEL", "gemma3:12b"),
        source_mode=_env("SOURCE_MODE", "inventory"),
        source_id=_env("SOURCE_ID", "", required=need),
        target_inventory_id=int(_env("TARGET_INVENTORY_ID", "0", required=need) or 0),
        price_group_id=_env("PRICE_GROUP_ID", "", required=need),
        warehouse_id=_env("WAREHOUSE_ID"),
        text_field_suffix=_env("TEXT_FIELD_SUFFIX", "") or "",
        rules=rules or {},
    )
    # Profil = Name des Großhändler-Ordners (z. B. p1, p2). Dort landen Protokolle und Listen.
    if rules_path.resolve().parent != ROOT.resolve():
        s.profile = rules_path.parent.name
        s.out_dir = rules_path.parent
    else:
        stem = rules_path.stem
        s.profile = "" if stem == "rules" else stem.replace("rules_", "")
        s.out_dir = s.data_dir
    s.proofread = bool((s.rules.get("description") or {}).get("proofread", True))
    # Eigener Gemini-Schlüssel je Großhändler (eigenes Tageskontingent), z. B. in rules.yaml:
    #   ai:
    #     gemini_key_env: GEMINI_API_KEY_P3
    ai = s.rules.get("ai") or {}
    key_env = ai.get("gemini_key_env")
    if key_env:
        val = os.getenv(key_env)
        if val:
            s.gemini_key = val
            print(f"Gemini-Schlüssel: {key_env}")
        else:
            print(f"Hinweis: {key_env} fehlt in der .env, es wird GEMINI_API_KEY verwendet.")
    if ai.get("gemini_model"):
        s.gemini_model = ai["gemini_model"]
    s.data_dir.mkdir(exist_ok=True)
    s.out_dir.mkdir(exist_ok=True)
    (s.data_dir / "images").mkdir(exist_ok=True)
    return s
