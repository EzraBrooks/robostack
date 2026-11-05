"""ci_config.py applies ci.yaml's cache controls to the platform's and noarch packages."""

import ci_config


def _run(tmp_path, monkeypatch, output, ci_yaml=None):
    config = tmp_path / "ci.yaml"
    if ci_yaml is not None:
        config.write_text(ci_yaml)
    vinca = tmp_path / "vinca.yaml"
    vinca.write_text("ros_distro: lyrical\n")
    monkeypatch.setattr("sys.argv", ["ci_config.py", "--output-dir", str(output), "--platform", "linux-64",
                                     "--config", str(config), "--vinca", str(vinca)])
    assert ci_config.main() == 0


def test_evicts_from_the_platform_and_noarch_caches(tmp_path, monkeypatch):
    output = tmp_path / "output"
    platform, noarch = output / "linux-64", output / "noarch"
    for cache, package in ((platform, "ros2-foo-1.0-h0_1.conda"), (noarch, "ros-lyrical-foo-1.0-h0_1.conda")):
        cache.mkdir(parents=True)
        (cache / package).write_text("")
        (cache / "ros2-bar-1.0-h0_1.conda").write_text("")

    _run(tmp_path, monkeypatch, output, "evict_cache: [foo]\n")

    for cache in (platform, noarch):
        assert sorted(p.name for p in cache.iterdir()) == [".ci-controls", "ros2-bar-1.0-h0_1.conda"]


def test_creates_a_missing_noarch_cache(tmp_path, monkeypatch):
    output = tmp_path / "output"
    (output / "linux-64").mkdir(parents=True)

    _run(tmp_path, monkeypatch, output)

    assert (output / "noarch" / ".ci-controls").is_file()


def test_full_rebuild_keeps_the_rest_of_the_output_directory(tmp_path, monkeypatch):
    output = tmp_path / "output"
    for d in ("linux-64", "noarch", "bld"):
        (output / d).mkdir(parents=True)
        (output / d / "ros2-foo-1.0-h0_1.conda").write_text("")

    _run(tmp_path, monkeypatch, output, "full_rebuild: true\n")

    assert [p.name for p in (output / "linux-64").iterdir()] == [".ci-controls"]
    assert [p.name for p in (output / "noarch").iterdir()] == [".ci-controls"]
    assert (output / "bld" / "ros2-foo-1.0-h0_1.conda").is_file()
