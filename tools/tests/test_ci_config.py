"""ci_config.py applies ci.yaml's cache controls to each cache directory."""

import ci_config


def test_evicts_from_the_platform_and_noarch_caches(tmp_path, monkeypatch):
    platform, noarch = tmp_path / "linux-64", tmp_path / "noarch"
    for cache, package in ((platform, "ros2-foo-1.0-h0_1.conda"), (noarch, "ros-lyrical-foo-1.0-h0_1.conda")):
        cache.mkdir()
        (cache / package).write_text("")
        (cache / "ros2-bar-1.0-h0_1.conda").write_text("")
    config = tmp_path / "ci.yaml"
    config.write_text("evict_cache: [foo]\n")
    vinca = tmp_path / "vinca.yaml"
    vinca.write_text("ros_distro: lyrical\n")
    monkeypatch.setattr("sys.argv", ["ci_config.py", "--cache-dir", str(platform), "--cache-dir", str(noarch),
                                     "--config", str(config), "--vinca", str(vinca)])

    assert ci_config.main() == 0

    for cache in (platform, noarch):
        assert sorted(p.name for p in cache.iterdir()) == [".ci-controls", "ros2-bar-1.0-h0_1.conda"]


def test_creates_a_missing_noarch_cache(tmp_path, monkeypatch):
    platform, noarch = tmp_path / "linux-64", tmp_path / "noarch"
    platform.mkdir()
    vinca = tmp_path / "vinca.yaml"
    vinca.write_text("ros_distro: lyrical\n")
    monkeypatch.setattr("sys.argv", ["ci_config.py", "--cache-dir", str(platform), "--cache-dir", str(noarch),
                                     "--config", str(tmp_path / "ci.yaml"), "--vinca", str(vinca)])

    assert ci_config.main() == 0
    assert (noarch / ".ci-controls").is_file()
