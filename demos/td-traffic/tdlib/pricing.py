"""Rates (USD per 1M tokens) as the gateway prices them: repo model-costs catalog + proxy built-ins."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

BUILTIN_PATH = Path(__file__).with_name("rates_builtin.json")
BUILTIN_PROVIDERS = ("azure", "mistral")  # file wins for these; repo catalog wins for the rest


@dataclass(frozen=True)
class Rates:
    input: Decimal
    output: Decimal
    cache_read: Decimal


def _catalog_from_yaml(text: str) -> dict:
    """Extract the JSON under `catalog.json: |` (as scripts/gen-model-costs.py writes it)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "catalog.json: |":
            indent = len(line) - len(line.lstrip())
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt)
            return json.loads("\n".join(body))
    raise ValueError("no `catalog.json: |` block found")


def _flatten(catalog: dict, only: tuple[str, ...] | None = None) -> dict[tuple[str, str], Rates]:
    out: dict[tuple[str, str], Rates] = {}
    for prov, pdata in catalog.get("providers", {}).items():
        if only is not None and prov not in only:
            continue
        for model, mdata in pdata.get("models", {}).items():
            r = mdata.get("rates", {})
            out[(prov, model)] = Rates(Decimal(str(r.get("input", "0"))),
                                       Decimal(str(r.get("output", "0"))),
                                       Decimal(str(r.get("cacheRead", "0"))))
    return out


def load_rates(path: Path) -> dict[tuple[str, str], Rates]:
    rates = {k: v for k, v in _flatten(_catalog_from_yaml(Path(path).read_text())).items()
             if k[0] not in BUILTIN_PROVIDERS}
    rates.update(_flatten(json.loads(BUILTIN_PATH.read_text()), BUILTIN_PROVIDERS))
    return rates
