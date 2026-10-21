"""Synthetic TD AI-assistant usage: rhythm, model choice, tokens, latency, errors, cost.

Drives both the 30-day history backfill and live traffic. All clock logic is
America/Toronto local time; inputs and outputs are UTC-aware datetimes.
"""
from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterator
from zoneinfo import ZoneInfo

from .org import Org, Tool, VirtualKey
from .pricing import Rates

TORONTO = ZoneInfo("America/Toronto")
BUCKET = timedelta(minutes=5)
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelRoute:
    route: str
    provider: str          # provider label exactly as in custom_dimensions (NOTES.md)
    model: str             # backend model string
    catalog_provider: str
    catalog_model: str


# NOTES.md route table, verbatim.
MODELS: dict[str, ModelRoute] = {
    "azure-gpt": ModelRoute("/azure/gpt-5-4", "azure", "gpt-5.4", "azure", "gpt-5.4"),
    "azure-gpt-mini": ModelRoute("/azure/gpt-5-4-mini", "azure", "gpt-5.4-mini", "azure", "gpt-5.4-mini"),
    "bedrock-opus": ModelRoute("/bedrock/claude-opus-5-5", "aws.bedrock", "anthropic.claude-opus-5-5",
                               "aws.bedrock", "anthropic.claude-opus-5-5"),
    "mistral-large": ModelRoute("/mistral/mistral-large", "mistral", "mistral-large-latest",
                                "mistral", "mistral-large-latest"),
}


@dataclass(slots=True)
class Request:
    ts: datetime
    key: VirtualKey
    route: ModelRoute
    user_agent: str
    input_tokens: int        # total input, cached tokens included (gen_ai.usage.input_tokens)
    output_tokens: int
    cache_read_tokens: int
    latency_ms: int
    status: int


# ---------------------------------------------------------------- user agents

USER_AGENTS: dict[Tool, list[str]] = {
    "copilot": [
        "GitHubCopilotChat/0.31.0 vscode/1.105.0",
        "GitHubCopilotChat/0.31.2 vscode/1.105.1",
        "GitHubCopilotChat/0.31.3 vscode/1.105.1",
    ],
    "vscode": [
        "Continue/1.2.4 vscode/1.105.0",
        "Continue/1.2.6 vscode/1.105.1",
        "Cline/3.32.1 vscode/1.105.1",
    ],
    "claude-code": [
        "claude-cli/2.1.3 (external, cli)",
        "claude-cli/2.1.4 (external, cli)",
        "claude-cli/2.1.6 (external, cli)",
    ],
    "claude-desktop": [
        "Claude/1.0.211 (Macintosh; Apple Silicon Mac OS X 15_6) Chrome/138.0.7204.243 Electron/37.2.1 Claude Desktop",
        "Claude/1.0.215 (Macintosh; Apple Silicon Mac OS X 15_7) Chrome/138.0.7204.243 Electron/37.3.0 Claude Desktop",
    ],
}
# Headless Claude Code in CI (team keys).
CI_USER_AGENTS: list[str] = [
    "claude-cli/2.1.4 (external, sdk-cli)",
    "claude-cli/2.1.6 (external, sdk-cli)",
]


def client_for_ua(ua: str) -> str:
    """Mirror of the `client` dimension CEL (NOTES.md §4) PLUS a VS Code branch.

    The NOTES expression maps Continue/Cline to "Other"; Task 5 must add, before the
    final "Other":  ua.startsWith("Continue/") || ua.startsWith("Cline/") ? "VS Code" :
    """
    if ua.startswith("claude-cli"):
        return "Claude Code"
    if ua.startswith("GitHubCopilot"):
        return "GitHub Copilot"
    if "Claude Desktop" in ua:
        return "Claude Desktop"
    if ua.startswith("Continue/") or ua.startswith("Cline/"):
        return "VS Code"
    return "Other"


CLIENT_FOR_UA: dict[str, str] = {
    ua: client_for_ua(ua) for uas in [*USER_AGENTS.values(), CI_USER_AGENTS] for ua in uas
}


def _user_agent(key: VirtualKey) -> str:
    """Stable per key (a person runs one client build at a time)."""
    pool = CI_USER_AGENTS if key.kind == "team" else USER_AGENTS[key.tool]
    return pool[zlib.crc32(key.id.encode()) % len(pool)]


# ---------------------------------------------------------------- activity

# Toronto-local weekday hour curve, 1.0 = peak (10-12, 14-16), ~3% night floor.
WEEKDAY_CURVE = [
    0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.05, 0.15,   # 00-07
    0.45, 0.80, 1.00, 1.00, 0.70, 0.85, 1.00, 1.00,   # 08-15
    0.80, 0.45, 0.20, 0.12, 0.08, 0.08, 0.05, 0.03,   # 16-23
]
WEEKEND_FACTOR = 0.10
NIGHT_FLOOR = 0.03
# CI: steady through the working day, nightly pipelines 01-04, quiet weekends.
CI_WEEKDAY_CURVE = [
    0.10, 0.45, 0.60, 0.45, 0.15, 0.10, 0.10, 0.30,   # 00-07
    0.70, 0.90, 1.00, 1.00, 0.90, 1.00, 1.00, 1.00,   # 08-15
    0.90, 0.70, 0.40, 0.25, 0.15, 0.10, 0.10, 0.10,   # 16-23
]
CI_WEEKEND_FACTOR = 0.12

# Expected requests per minute at peak, by tool and persona.
PEAK_RPM: dict[str, dict[str, float]] = {
    # Copilot power users run agent mode: many back-to-back tool-call turns.
    "copilot": {"power": 5.0, "regular": 1.20, "light": 0.20},
    "vscode": {"power": 1.6, "regular": 0.50, "light": 0.15},
    "claude-code": {"power": 0.90, "regular": 0.35, "light": 0.10},
    "claude-desktop": {"power": 0.30, "regular": 0.15, "light": 0.06},
}
CI_PEAK_RPM = 0.50


def _curve(kind: str, at_utc: datetime) -> float:
    local = at_utc.astimezone(TORONTO)
    weekend = local.weekday() >= 5
    if kind == "team":
        v = CI_WEEKDAY_CURVE[local.hour]
        return max(0.03, v * CI_WEEKEND_FACTOR) if weekend else v
    v = WEEKDAY_CURVE[local.hour]
    return max(NIGHT_FLOOR, v * WEEKEND_FACTOR) if weekend else v


def _intensity(key: VirtualKey) -> float:
    """Stable per-person (or per-pipeline) multiplier in [0.7, 1.3] so no two users look identical."""
    who = key.user.email if key.user is not None else key.id
    return 0.7 + 0.6 * (zlib.crc32(who.encode()) % 1000) / 999


def _peak(key: VirtualKey) -> float:
    if key.kind == "team":
        return CI_PEAK_RPM * _intensity(key)
    assert key.user is not None
    return PEAK_RPM[key.tool][key.user.persona] * _intensity(key)


def activity(key: VirtualKey, at_utc: datetime, share: float = 1.0) -> float:
    """Expected requests per minute on this key at this instant.

    `share` splits a person's activity across their project keys (generate() sets it).
    """
    return _peak(key) * _curve(key.kind, at_utc) * share


# ---------------------------------------------------------------- per-request sampling

@dataclass(frozen=True)
class _Profile:
    in_median: float
    in_sigma: float
    cache_lo: float        # fraction of input served as cache_read, uniform [lo, hi]
    cache_hi: float
    out_median: float
    out_sigma: float


# Share of Copilot requests that are agent-mode turns, by persona.
COPILOT_AGENT_SHARE = {"power": 0.90, "regular": 0.70, "light": 0.25}


def _profile(key: VirtualKey, route_id: str, agent: bool) -> _Profile:
    if key.kind == "team":                                  # CI review / fix-up runs
        return _Profile(20_000, 0.6, 0.45, 0.60, 900, 0.7)
    tool = key.tool
    if tool == "copilot":
        if agent:                                           # agent mode, big contexts
            if route_id == "azure-gpt":
                return _Profile(30_000, 1.3, 0.0, 0.10, 2_600, 0.7)
            return _Profile(14_000, 0.9, 0.0, 0.0, 800, 0.7)
        return _Profile(2_500, 0.9, 0.0, 0.15, 400, 0.7)    # chat / inline edits
    if tool == "vscode":                                    # Continue / Cline agent loops
        if route_id == "azure-gpt":
            return _Profile(25_000, 1.1, 0.05, 0.20, 1_600, 0.7)
        return _Profile(18_000, 0.9, 0.15, 0.35, 900, 0.7)
    if tool == "claude-code":
        return _Profile(28_000, 0.8, 0.55, 0.65, 600, 0.8)
    return _Profile(6_000, 0.8, 0.10, 0.30, 900, 0.7)      # claude-desktop


def _route_id(key: VirtualKey, rng: random.Random) -> str:
    if key.tool == "copilot":
        return "azure-gpt" if rng.random() < 0.70 else "azure-gpt-mini"
    if key.tool == "vscode":
        return "mistral-large" if rng.random() < 0.60 else "azure-gpt"
    return "bedrock-opus"


# Latency: lognormal time-to-first-token median (ms) + per-output-token ms.
LATENCY: dict[str, tuple[float, float, float]] = {
    "azure-gpt": (900, 0.45, 12.0),
    "azure-gpt-mini": (450, 0.40, 6.0),
    "bedrock-opus": (1_600, 0.45, 18.0),
    "mistral-large": (650, 0.40, 9.0),
}

ERROR_RATE = 0.012
ERRORS = [(429, 0.50), (500, 0.20), (503, 0.20), (400, 0.10)]
MAX_INPUT = 260_000       # below gpt-5.4's 272k long-context price tier
MAX_OUTPUT = 32_000


def _lognorm(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def sample_request(key: VirtualKey, at_utc: datetime, rng: random.Random) -> Request:
    route_id = _route_id(key, rng)
    route = MODELS[route_id]
    ua = _user_agent(key)

    if rng.random() < ERROR_RATE:
        x = rng.random()
        for status, p in ERRORS:
            x -= p
            if x < 0:
                break
        # Rejected / failed upstream: no tokens billed, fast response.
        latency = int(_lognorm(rng, 60 if status == 429 else 400, 0.6)) + 1
        return Request(at_utc, key, route, ua, 0, 0, 0, latency, status)

    agent = (key.tool == "copilot" and key.user is not None
             and rng.random() < COPILOT_AGENT_SHARE[key.user.persona])
    p = _profile(key, route_id, agent)
    inp = max(64, min(MAX_INPUT, int(_lognorm(rng, p.in_median, p.in_sigma))))
    out = max(8, min(MAX_OUTPUT, int(_lognorm(rng, p.out_median, p.out_sigma))))
    # gpt-5.4-mini has no measured cacheRead rate in the built-in catalog: send none.
    cached = 0 if route_id == "azure-gpt-mini" else int(inp * rng.uniform(p.cache_lo, p.cache_hi))
    ttft, sig, per_tok = LATENCY[route_id]
    latency = int(_lognorm(rng, ttft, sig) + out * per_tok)
    return Request(at_utc, key, route, ua, inp, out, cached, latency, 200)


# ---------------------------------------------------------------- generation

def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, prod = math.exp(-lam), 0, rng.random()
    while prod > limit:
        k += 1
        prod *= rng.random()
    return k


def _shares(org: Org) -> dict[str, float]:
    """Split each person's activity across their project keys: primary 65%, rest share 35%."""
    by_user: dict[str, list[VirtualKey]] = {}
    for k in org.keys:
        if k.kind == "personal" and k.user is not None:
            by_user.setdefault(k.user.email, []).append(k)
    shares = {k.id: 1.0 for k in org.keys}
    for ks in by_user.values():
        if len(ks) > 1:
            shares[ks[0].id] = 0.65
            for k in ks[1:]:
                shares[k.id] = 0.35 / (len(ks) - 1)
    return shares


def generate(org: Org, start_utc: datetime, end_utc: datetime, seed: int) -> Iterator[Request]:
    """Requests in [start_utc, end_utc), ordered by ts: Poisson per key per 5-minute bucket."""
    rng = random.Random(seed)
    shares = _shares(org)
    rates = [(k, _peak(k) * shares[k.id]) for k in org.keys]
    t = start_utc
    while t < end_utc:
        span = min(BUCKET, end_utc - t)
        minutes = span.total_seconds() / 60
        mid = t + span / 2
        curve = {"personal": _curve("personal", mid), "team": _curve("team", mid)}
        batch: list[Request] = []
        for k, base in rates:
            n = _poisson(rng, base * curve[k.kind] * minutes)
            for _ in range(n):
                ts = t + span * rng.random()
                batch.append(sample_request(k, ts, rng))
        batch.sort(key=lambda r: r.ts)
        yield from batch
        t += span


def cost(req: Request, rates: dict[tuple[str, str], Rates]) -> dict[str, Decimal]:
    """Gateway-equivalent cost: uncached input, cache_read and output, USD."""
    r = rates[(req.route.catalog_provider, req.route.catalog_model)]
    c_in = Decimal(req.input_tokens - req.cache_read_tokens) * r.input / MILLION
    c_cache = Decimal(req.cache_read_tokens) * r.cache_read / MILLION
    c_out = Decimal(req.output_tokens) * r.output / MILLION
    return {"input": c_in, "output": c_out, "cache_read": c_cache, "total": c_in + c_cache + c_out}
