from tdlib.org import _slug, build_org, service_identity


def test_counts():
    o = build_org()
    assert (o.name, o.domain) == ("TD", "td.com")
    assert len(o.groups) == 5 and len(o.teams) == 15 and len(o.projects) == 12 and len(o.users) == 35


def test_tool_split():
    o = build_org(); tools = [u.tool for u in o.users]
    assert tools.count("copilot") == 16 and tools.count("vscode") == 6
    assert tools.count("claude-code") == 9 and tools.count("claude-desktop") == 4
    assert not hasattr(o.users[0], "mistral_byok")


def test_team_keys():
    o = build_org(); tk = [k for k in o.keys if k.kind == "team"]
    assert len(tk) == 4 and len({k.team for k in tk}) == 4
    for k in tk:
        assert k.user is None and k.tool == "claude-code"
        assert k.id == "vk-team-" + _slug(k.team)
        sid = service_identity(k)
        assert sid.startswith("svc-") and sid.endswith("@td.com")
        assert o.teams[k.team] == k.group


def test_personal_keys_match_user():
    o = build_org()
    pk = [k for k in o.keys if k.kind == "personal"]
    assert pk
    for k in pk:
        assert (k.team, k.group, k.tool) == (k.user.team, k.user.group, k.user.tool)
        assert service_identity(k) == k.user.email


def test_copilot_has_power_users():
    o = build_org()
    assert sum(u.persona == "power" for u in o.users if u.tool == "copilot") >= 3


def test_secret_format():
    import re
    o = build_org()
    assert all(re.fullmatch(r"sk-agw-[A-Za-z0-9_-]{43}", k.secret) for k in o.keys)


def test_emails_unique_and_domain():
    o = build_org(); emails = [u.email for u in o.users]
    assert len(set(emails)) == 35 and all(e.endswith("@td.com") for e in emails)


def test_every_team_and_project_used():
    o = build_org()
    assert {u.team for u in o.users} == set(o.teams)
    assert {k.project for k in o.keys} == set(o.projects)


def test_keys_unique_and_deterministic():
    a, b = build_org(), build_org()
    assert [k.secret for k in a.keys] == [k.secret for k in b.keys]
    assert len({k.id for k in a.keys}) == len(a.keys) and len({k.secret for k in a.keys}) == len(a.keys)
    assert all(any(k.user == u for k in a.keys) for u in a.users)
