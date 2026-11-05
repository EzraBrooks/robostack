"""build_gap_report.py counts noarch artifacts for every platform."""

import build_gap_report


def test_noarch_artifacts_count_for_every_platform(tmp_path, monkeypatch, capsys):
    recipes = tmp_path / "recipes"
    for recipe in ("ros2-a", "ros2-b", "ros2-c"):
        (recipes / recipe).mkdir(parents=True)
    output = tmp_path / "output"
    for subdir, package in (("linux-64", "ros2-a-1.0-h0_1"), ("osx-arm64", "ros2-c-1.0-h0_1"),
                            ("noarch", "ros2-b-1.0-h0_1")):
        (output / subdir).mkdir(parents=True)
        (output / subdir / f"{package}.conda").write_text("")
    monkeypatch.setattr("sys.argv", ["build_gap_report.py", "--output-dir", str(output), "--recipes-dir",
                                     str(recipes), "--any-build-number"])

    assert build_gap_report.main() == 0

    report = capsys.readouterr().out
    assert "Platform: noarch" not in report
    linux, osx = report.split("Platform: linux-64")[1].split("Platform: osx-arm64")
    assert "1 out of 3\n  - ros2-c\n" in linux
    assert "1 out of 3\n  - ros2-a\n" in osx
