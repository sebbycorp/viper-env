from tdlib.org import build_org


def test_counts():
    o = build_org()
    assert (o.name, o.domain) == ("TD", "td.com")
    assert len(o.groups) == 5 and len(o.teams) == 15 and len(o.projects) == 12 and len(o.users) == 35


def test_tool_split_and_byok():
    o = build_org(); tools = [u.tool for u in o.users]
    assert tools.count("copilot") == 20 and tools.count("claude-code") == 10 and tools.count("claude-desktop") == 5
    assert 3 <= sum(u.mistral_byok for u in o.users) <= 5
    assert all(u.tool == "copilot" for u in o.users if u.mistral_byok)


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
