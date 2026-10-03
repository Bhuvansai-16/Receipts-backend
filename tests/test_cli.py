import sys

import pytest

from receipts import __main__ as cli, config, db
from receipts.__main__ import load_patch


def test_patch_file_is_read_as_utf8(tmp_path):
    f = tmp_path / "fix.diff"
    f.write_bytes("+name = 'café'\r\n".encode("utf-8"))
    assert load_patch(str(f), gold="G") == ("+name = 'café'\n", "fix")


def test_patch_keywords():
    assert load_patch("gold", gold="G") == ("G", "gold")
    assert load_patch("none", gold="G") == (None, "none")


def test_db_commands_need_a_database_url(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(sys, "argv", ["receipts", "migrate"])
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        cli.main()


@pytest.mark.parametrize("argv", [["run", "psf__requests-1142"], ["smoke"]])
def test_checks_need_the_two_nebius_settings(monkeypatch, argv):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("CONTREE_PROJECT", "p")
    monkeypatch.setattr(sys, "argv", ["receipts", *argv])
    with pytest.raises(SystemExit, match="NEBIUS_API_KEY"):
        cli.main()


def test_migrate_uses_the_direct_connection(monkeypatch, capsys):
    seen = []

    async def fake_migrate(url):
        seen.append(url)
        return ["001_init"]

    monkeypatch.setattr(config, "DATABASE_URL", "pooled")
    monkeypatch.setattr(config, "DATABASE_URL_UNPOOLED", "direct")
    monkeypatch.setattr(db, "migrate", fake_migrate)
    monkeypatch.setattr(sys, "argv", ["receipts", "migrate"])
    cli.main()
    assert seen == ["direct"] and "001_init" in capsys.readouterr().out


def test_serve_uses_a_selector_loop_on_windows(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["receipts", "serve", "--port", "8123"])
    cli.main()
    assert seen == {"app": "receipts.server:app", "host": "localhost", "port": 8123, "loop": "asyncio:SelectorEventLoop"}


def test_serve_listens_on_cloud_runs_port_on_every_interface(monkeypatch):
    # Cloud Run sets PORT and routes to the container from outside, so localhost would never answer.
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setattr(sys, "argv", ["receipts", "serve"])
    cli.main()
    assert seen == {"host": "0.0.0.0", "port": 8080, "loop": "auto"}
