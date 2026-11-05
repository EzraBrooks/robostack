"""upload(): every distribution goes to its channel on both prefix.dev and anaconda.org."""

from pathlib import Path

import pytest

import robostack as rs


@pytest.fixture
def calls(monkeypatch):
    calls = []
    monkeypatch.setattr(rs, "run", lambda cmd, cwd: calls.append(cmd) or 0)
    monkeypatch.setattr(rs, "settings", lambda distro: {"channel_name": f"robostack-{distro}"})
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)
    monkeypatch.setenv("ANACONDA_API_TOKEN", "secret")
    return calls


def test_uploads_to_both(calls, monkeypatch):
    monkeypatch.setattr(rs, "on_anaconda", lambda channel, package: False)
    assert rs.upload("jazzy", ["linux-64/a-1-0.conda"], Path(".")) == 0
    assert calls[0][:5] == ["rattler-build", "upload", "prefix", "-c", "robostack-jazzy"]
    assert calls[1][:5] == ["rattler-build", "upload", "anaconda", "-o", "robostack-jazzy"]
    assert calls[1][-1] == "linux-64/a-1-0.conda"


def test_skips_what_anaconda_has(calls, monkeypatch):
    monkeypatch.setattr(rs, "on_anaconda", lambda channel, package: True)
    assert rs.upload("jazzy", ["linux-64/a-1-0.conda"], Path(".")) == 0
    assert [c[2] for c in calls] == ["prefix"]


def test_test_channel_only_prefix(calls, monkeypatch):
    monkeypatch.setenv("ROBOSTACK_UPLOAD_CHANNEL", "someone/test")
    assert rs.upload("jazzy", ["linux-64/a-1-0.conda"], Path(".")) == 0
    assert len(calls) == 1 and calls[0][4] == "someone/test"


def test_missing_token_fails(calls, monkeypatch):
    monkeypatch.delenv("ANACONDA_API_TOKEN")
    assert rs.upload("jazzy", ["linux-64/a-1-0.conda"], Path(".")) == 1
    assert [c[2] for c in calls] == ["prefix"]


def test_reads_from(monkeypatch):
    monkeypatch.setattr(rs, "settings", lambda distro: {"channel_name": "robostack-humble", "read_from": "anaconda"})
    assert rs.channel_url("humble") == "https://conda.anaconda.org/robostack-humble"
    monkeypatch.setattr(rs, "settings", lambda distro: {"channel_name": "robostack-rolling"})
    assert rs.repodata_url("rolling") == "https://repo.prefix.dev/robostack-rolling"


def test_anaconda_only(calls, monkeypatch):
    monkeypatch.setattr(rs, "settings", lambda distro: {"channel_name": "robostack-humble", "upload_to": ["anaconda"]})
    monkeypatch.setattr(rs, "on_anaconda", lambda channel, package: False)
    assert rs.upload("humble", ["linux-64/a-1-0.conda"], Path(".")) == 0
    assert [c[2] for c in calls] == ["anaconda"] and calls[0][4] == "robostack-humble"


def test_on_anaconda_looks_up_noarch_packages_in_noarch(monkeypatch):
    import urllib.error
    import urllib.request

    urls = []

    def urlopen(request, timeout):
        urls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert rs.on_anaconda("robostack-lyrical", Path("out/noarch/ros-lyrical-a-1.0.0-h0_31.conda")) is False
    assert urls == ["https://api.anaconda.org/dist/robostack-lyrical/ros-lyrical-a/1.0.0/noarch/"
                    "ros-lyrical-a-1.0.0-h0_31.conda"]
