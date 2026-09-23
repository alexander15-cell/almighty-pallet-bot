"""runtime_settings.py - role-ID bindings persist and resolve_role() prefers
a bound ID over name-matching, falling back correctly when nothing's bound
or the bound role no longer exists."""
import importlib

import runtime_settings


class _FakeRole:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class _FakeGuild:
    def __init__(self, roles):
        self.roles = roles

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


def _isolated_settings(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    module = importlib.reload(runtime_settings)
    return module


def test_bindings_persist_across_reload(tmp_path, monkeypatch):
    """Reloading the module simulates a bot restart - bindings must survive
    it, since they're read from SETTINGS_PATH on import, not held only in
    memory."""
    rs = _isolated_settings(tmp_path, monkeypatch)
    assert rs.get_bindings() == {}

    rs.set_role_id("Pallet Admin", 12345)
    assert rs.get_bindings() == {"Pallet Admin": 12345}

    reloaded = importlib.reload(rs)
    assert reloaded.get_bindings() == {"Pallet Admin": 12345}


def test_resolve_role_prefers_bound_id_and_survives_rename(tmp_path, monkeypatch):
    rs = _isolated_settings(tmp_path, monkeypatch)
    admin_role = _FakeRole(999, "Pallet Admin")
    guild = _FakeGuild([admin_role])

    # unbound -> falls back to name match
    assert rs.resolve_role(guild, "Pallet Admin") is admin_role

    # bound -> resolves by id
    rs.set_role_id("Pallet Admin", 999)
    assert rs.resolve_role(guild, "Pallet Admin") is admin_role

    # role renamed in Discord, but the bound ID still finds it
    admin_role.name = "Super Admin"
    assert rs.resolve_role(guild, "Pallet Admin") is admin_role


def test_resolve_role_falls_back_when_bound_role_deleted(tmp_path, monkeypatch):
    rs = _isolated_settings(tmp_path, monkeypatch)
    admin_role = _FakeRole(999, "Pallet Admin")
    guild = _FakeGuild([admin_role])

    rs.set_role_id("Pallet Admin", 55555)  # a stale/deleted role id
    assert rs.resolve_role(guild, "Pallet Admin") is admin_role


def test_clear_role_id(tmp_path, monkeypatch):
    rs = _isolated_settings(tmp_path, monkeypatch)
    rs.set_role_id("Pallet Admin", 999)
    rs.clear_role_id("Pallet Admin")
    assert rs.get_bindings() == {}
