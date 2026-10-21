"""Deterministic synthetic TD org model (fictional people, plausible bank structure)."""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Literal

Tool = Literal["claude-code", "copilot", "claude-desktop"]
Persona = Literal["power", "regular", "light"]


@dataclass(frozen=True)
class User:
    email: str
    name: str
    group: str
    team: str
    tool: Tool
    persona: Persona
    mistral_byok: bool


@dataclass(frozen=True)
class VirtualKey:
    id: str
    secret: str
    user: User
    project: str


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

N_COPILOT, N_CODE, N_DESKTOP, N_BYOK = 20, 10, 5, 4


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def build_org(seed: int = 42) -> Org:
    rng = random.Random(seed)
    groups = list(GROUP_TEAMS)
    teams = {t: g for g, ts in GROUP_TEAMS.items() for t in ts}
    team_names = list(teams)

    tools: list[Tool] = (
        ["copilot"] * N_COPILOT + ["claude-code"] * N_CODE + ["claude-desktop"] * N_DESKTOP
    )
    rng.shuffle(tools)
    copilot_idx = [i for i, t in enumerate(tools) if t == "copilot"]
    byok = set(rng.sample(copilot_idx, N_BYOK))

    def persona(tool: Tool) -> Persona:
        if tool == "claude-code":
            return rng.choices(["power", "regular"], weights=[80, 20])[0]
        if tool == "copilot":
            return rng.choices(["light", "regular", "power"], weights=[50, 40, 10])[0]
        return "regular"

    users: list[User] = []
    for i, name in enumerate(NAMES):
        team = team_names[i % len(team_names)]  # round-robin: every team used
        first, last = name.lower().split(" ", 1)
        email = f"{first}.{_slug(last).replace('-', '.')}@td.com"
        users.append(User(email, name, teams[team], team, tools[i], persona(tools[i]), i in byok))

    keys: list[VirtualKey] = []
    for i, u in enumerate(users):
        assigned = [PROJECTS[i % len(PROJECTS)]]  # round-robin: every project used
        if i % 3 == 0:  # about a third span a second project
            other = PROJECTS[(i + 1 + rng.randrange(len(PROJECTS) - 1)) % len(PROJECTS)]
            assigned.append(other)
        for proj in assigned:
            kid = f"vk-{u.email.split('@')[0]}-{_slug(proj)}"
            secret = "td-" + hashlib.sha256(f"{seed}:{kid}".encode()).hexdigest()[:40]
            keys.append(VirtualKey(kid, secret, u, proj))

    return Org("TD", "td.com", groups, teams, list(PROJECTS), users, keys)
