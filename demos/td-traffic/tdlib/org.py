"""Deterministic synthetic TD org model (fictional people, plausible bank structure)."""
from __future__ import annotations

import base64
import hashlib
import random
import re
from dataclasses import dataclass
from typing import Literal

Tool = Literal["copilot", "vscode", "claude-code", "claude-desktop"]
Persona = Literal["power", "regular", "light"]


@dataclass(frozen=True)
class User:
    email: str
    name: str
    group: str
    team: str
    tool: Tool
    persona: Persona


@dataclass(frozen=True)
class VirtualKey:
    id: str
    secret: str
    kind: Literal["personal", "team"]
    team: str
    group: str
    project: str
    user: User | None
    tool: Tool


@dataclass(frozen=True)
class Org:
    name: str
    domain: str
    groups: list[str]
    teams: dict[str, str]  # team -> group
    projects: list[str]
    users: list[User]
    keys: list[VirtualKey]


GROUP_TEAMS: dict[str, list[str]] = {
    "Personal Banking": ["Retail Deposits", "Mortgage Lending", "Credit Cards & Lending"],
    "Wealth Management": ["Advisory Platforms", "Private Client Services", "Investment Operations"],
    "TD Securities": ["Equities Trading Tech", "Fixed Income Analytics", "Capital Markets Data"],
    "Enterprise Technology": ["Cloud Platform Engineering", "Digital Channels", "Data & AI Enablement"],
    "Risk & Compliance": ["Fraud Strategy", "AML & Sanctions", "Model Risk Governance"],
}

PROJECTS = [
    "Mortgage Digital Onboarding",
    "Fraud Signals Platform",
    "Advisor Copilot Workbench",
    "Real-Time Payments Hub",
    "KYC Refresh Automation",
    "Credit Adjudication Engine",
    "Trade Surveillance Analytics",
    "Client Statement Modernization",
    "Mobile App Rebuild",
    "Regulatory Reporting Pipeline",
    "Customer 360 Data Mesh",
    "Branch Appointment Scheduler",
]

# 35 distinct fictional names (diverse, Canadian-bank plausible).
NAMES = [
    "Priya Nair", "Liam Tremblay", "Wei Zhang", "Amelia Campbell", "Mohammed Rahman",
    "Sophie Gagnon", "Jasdeep Gill", "Emma MacDonald", "Daniel Okafor", "Chloe Bouchard",
    "Arjun Mehta", "Olivia Fraser", "Mateo Fernandez", "Hannah Levesque", "Kenji Tanaka",
    "Isabelle Roy", "Tariq Hussain", "Grace Whitfield", "Noah Lefebvre", "Aisha Siddiqui",
    "Ethan Kowalski", "Mei Ling Chen", "Samuel Addo", "Julia Petrov", "Raj Patel",
    "Claire Dubois", "Omar Haddad", "Natalie Brooks", "Lucas Pereira", "Anika Sharma",
    "Connor Murphy", "Fatima Qureshi", "Gabriel Morin", "Yuna Park", "Victor Nguyen",
]

N_COPILOT, N_VSCODE, N_CODE, N_DESKTOP, N_TEAM_KEYS = 16, 6, 9, 4, 4

# Exact persona mix per tool (shuffled deterministically); copilot needs power users.
PERSONAS: dict[str, list[Persona]] = {
    "copilot": ["power"] * 4 + ["regular"] * 6 + ["light"] * 6,
    "vscode": ["power"] * 3 + ["regular"] * 3,
    "claude-code": ["power"] * 7 + ["regular"] * 2,
    "claude-desktop": ["regular"] * 4,
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _secret(seed: int, kid: str) -> str:
    """Solo UI virtual-key format: sk-agw- + 43 base64url chars (deterministic)."""
    d = hashlib.sha256(f"{seed}:{kid}".encode()).digest()
    return "sk-agw-" + base64.urlsafe_b64encode(d).decode().rstrip("=")


def service_identity(key: VirtualKey) -> str:
    if key.kind == "team":
        return f"svc-{_slug(key.team)}@td.com"
    assert key.user is not None
    return key.user.email


def build_org(seed: int = 42) -> Org:
    rng = random.Random(seed)
    groups = list(GROUP_TEAMS)
    teams = {t: g for g, ts in GROUP_TEAMS.items() for t in ts}
    team_names = list(teams)

    tools: list[Tool] = (
        ["copilot"] * N_COPILOT + ["vscode"] * N_VSCODE
        + ["claude-code"] * N_CODE + ["claude-desktop"] * N_DESKTOP
    )
    rng.shuffle(tools)
    pools = {t: list(p) for t, p in PERSONAS.items()}
    for p in pools.values():
        rng.shuffle(p)

    users: list[User] = []
    for i, name in enumerate(NAMES):
        team = team_names[i % len(team_names)]  # round-robin: every team used
        first, last = name.lower().split(" ", 1)
        email = f"{first}.{_slug(last).replace('-', '.')}@td.com"
        users.append(User(email, name, teams[team], team, tools[i], pools[tools[i]].pop()))

    keys: list[VirtualKey] = []
    for i, u in enumerate(users):
        assigned = [PROJECTS[i % len(PROJECTS)]]  # round-robin: every project used
        if i % 3 == 0:  # about a third span a second project
            other = PROJECTS[(i + 1 + rng.randrange(len(PROJECTS) - 1)) % len(PROJECTS)]
            assigned.append(other)
        for proj in assigned:
            kid = f"vk-{u.email.split('@')[0]}-{_slug(proj)}"
            keys.append(VirtualKey(kid, _secret(seed, kid), "personal", u.team, u.group, proj, u, u.tool))

    # Shared team keys: headless Claude Code in CI, no user.
    for j, team in enumerate(rng.sample(team_names, N_TEAM_KEYS)):
        kid = f"vk-team-{_slug(team)}"
        keys.append(VirtualKey(kid, _secret(seed, kid), "team", team, teams[team],
                               PROJECTS[(j * 5 + 2) % len(PROJECTS)], None, "claude-code"))

    return Org("TD", "td.com", groups, teams, list(PROJECTS), users, keys)
