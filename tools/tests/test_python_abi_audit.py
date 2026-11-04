import python_abi_audit as audit


def test_classify_by_package_content():
    assert audit.classify(["share/a/package.xml", "share/a/launch/__pycache__/x.cpython-314.pyc"], [])["content"] == "none"
    assert audit.classify(["lib/python3.14/site-packages/a/__init__.py"], [])["content"] == "pure"
    assert audit.classify(["lib/python/site-packages/a/__init__.py"], [])["content"] == "pure"
    assert audit.classify(["Lib/site-packages/a/__init__.py"], [])["content"] == "pure"
    assert audit.classify(["lib/a/_a.cpython-314-x86_64-linux-gnu.so"], [])["content"] == "native"
    assert audit.classify(["Lib/site-packages/a/_a.cp314-win_amd64.pyd"], [])["content"] == "native"
    assert audit.classify(["lib/liba_msgs__rosidl_generator_py.so"], [])["content"] == "native"
    assert audit.classify(["lib/a/node"], ["lib/a/node"])["content"] == "native"


def test_recipes_needing_python_reads_host(tmp_path):
    for name, host in (("ros2-a", "[{if: unix, then: [python]}, ros2-b]"),
                       ("ros2-b", "[\"python ${{ python_min }}.*\"]"),
                       ("ros-lyrical-a", "[python]")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "recipe.yaml").write_text(f"package: {{name: {name}}}\nrequirements:\n  host: {host}\n")
    assert audit.recipes_needing_python(tmp_path) == {"ros2-a": True, "ros2-b": False}


def test_newest_builds_takes_the_highest_build_number():
    def rec(name, number, timestamp=0):
        return {"name": name, "build_number": number, "timestamp": timestamp}

    repodata = {"packages.conda": {"a-1-h_30.conda": rec("ros2-a", 30), "a-1-h_29.conda": rec("ros2-a", 29, 9),
                                   "a-1-g_30.conda": rec("ros2-a", 30, 5), "x.conda": rec("ros-lyrical-a", 31)}}
    assert audit.newest_builds(repodata) == {"ros2-a": "a-1-g_30.conda"}
