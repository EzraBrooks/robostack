# RoboStack: ROS 2 distributions

Conda packages of [ROS](https://www.ros.org) for Linux, macOS and Windows, built from
this repository for every supported ROS 2 distribution. To install and use them, see
the [RoboStack documentation](https://robostack.github.io/GettingStarted.html).

| Distribution | Channel |
|---|---|
| rolling | robostack-rolling on [prefix.dev](https://prefix.dev/channels/robostack-rolling) and [anaconda.org](https://anaconda.org/robostack-rolling) (the same packages) |
| lyrical | robostack-lyrical on [prefix.dev](https://prefix.dev/channels/robostack-lyrical) and [anaconda.org](https://anaconda.org/robostack-lyrical) (the same packages) |
| jazzy | [robostack-jazzy](https://anaconda.org/robostack-jazzy) (prefix.dev mirrors it) |
| humble | [robostack-humble](https://anaconda.org/robostack-humble) (prefix.dev mirrors it) |

Packages are named `ros2-<package>`, e.g. `ros2-desktop` or `ros2-rclcpp`; the
distribution comes from the channel. The older names keep working: for every package
there is also a small `ros-<distro>-<package>` package that just depends on
`ros2-<package>` of the same version (vinca's `package_name_mode: both` in
`shared/vinca.yaml`), so existing environments and instructions such as
`pixi add ros-jazzy-desktop` don't break.

**A package is missing?** Open a *Package request* issue. robostack-bot opens a pull
request that adds it to the build, and a maintainer reviews it.

If you use RoboStack in your academic work, please cite:

```bibtex
@article{FischerRAM2021,
    title={A RoboStack Tutorial: Using the Robot Operating System Alongside the Conda and Jupyter Data Science Ecosystems},
    author={Tobias Fischer and Wolf Vollprecht and Silvio Traversaro and Sean Yen and Carlos Herrero and Michael Milford},
    journal={IEEE Robotics and Automation Magazine},
    year={2021},
    doi={10.1109/MRA.2021.3128367},
}
```

---

## For maintainers

### Layout

```
distros/<distro>/          only what is specific to one distribution
  distro.yaml              channel, where it's uploaded and read from, conda-forge pinning version/migrations, own pins, rosdistro sync
  vinca.yaml               extends shared/vinca.yaml: ros_distro, build number, mutex version, its exceptions
  pkg_additional_info.yaml its own per-package settings (build numbers, version-specific cmake args)
  patch/                   its patches; patch/dependencies.yaml its own dependency fixes
  rosdistro_snapshot.yaml, rosdistro_additional_recipes.yaml
  ci.yaml                  temporary PR-build controls (full rebuild, cache evictions)
  conda_build_config.yaml  generated: `pixi run rs <distro> render-pinning`
  work/                    generated, git-ignored: where vinca and rattler-build run
shared/                    the same for every distribution
  vinca.yaml               settings and the package selection of every distribution
  pkg_additional_info.yaml per-package settings that are the same everywhere
  patch/dependencies.yaml  dependency fixes that are the same everywhere
  robostack.yaml           rosdep key -> conda package mapping (`robostack: []` drops a key)
  pinning/                 conda-forge pinning version, migrations and overrides
  tests/                   package tests (ros2-<pkg>.yaml; `if: ros_distro ...` for distro-specific bits)
tools/robostack.py         `pixi run rs ...`: every task, for one distribution or the repository
tools/maintenance.py       checks and robostack-bot commands
tools/import_distro.py     (re)import a distribution from its own repository
tools/*.py                 check_patches_clean_apply, check_dependency_compat, build_gap_report, ...
.scripts/                  staged build scripts (build_unix.sh, build_win.bat)
.github/workflows/         testpr.yaml, main.yaml (staged build branches), bot.yaml
.claude/skills/             procedures for agents (debug a build, patches, versions/rebuilds, package selection)
pixi.toml                  one environment for all distributions (one vinca, one rattler-build)
```

### How shared and distribution files combine

`distros/<distro>/vinca.yaml` starts with `extends: ../../shared/vinca.yaml`; vinca
combines the two, and the files next to each:

- **`vinca.yaml`:**
  - The package selection (`packages_select_by_deps`) lives in `shared/` for every distribution; vinca skips packages a distribution's rosdistro doesn't have.
  - Exceptions, in `shared/` or a distribution: `packages_exclude` (not built, dropped from other packages' dependencies) and `packages_skip` (not built, dependents keep the dependency), each as plain names or under `- if: <condition>`. ROS packages mapped to conda-forge in `robostack.yaml` are skipped automatically.
  - `mutex_package`: name and `upper_bound` are shared; each distribution sets its own `version`. The mutex only keeps one distribution per environment and has no `run_constraints` (`rs check` enforces it): library versions come from the packages' own dependencies, and wrong ones are fixed in the package's metadata.
  - Other keys come from the distribution if it sets them, else from `shared/`. A distribution that differs from a shared setting just sets it.
  - Paths are relative to the file that sets them. `distros/<distro>/work/vinca.yaml` (generated) extends the distribution's file and adds `skip_existing` (its channel).
- **`pkg_additional_info.yaml` and `patch/dependencies.yaml`:** merged per package. The distribution's keys win, for example its own `build_number` on top of a shared `additional_cmake_args`.
- **`patch/*.patch` and `tests/`:** per package, the distribution's file replaces the shared one. In tests, `- if: ros_distro in ["humble", "jazzy"]` blocks select distribution-specific parts.
- **`vinca_pinning.yaml`:** the shared pinning, adjusted by `distro.yaml`:
  - `conda_forge_pinning_version` and `conda_forge_migrations` keep a distribution on its own conda-forge pinning, for example until its next full rebuild;
  - `pinning_overrides` replace shared pins by name, for example jazzy's Python 3.12. Items written as a block list keep their conda-build selectors (`- 3.12.*  # [not riscv64]`).
  - `pixi run rs <distro> render-pinning` turns this into the committed `conda_build_config.yaml`.

A fix that applies to every distribution goes into `shared/`, a version-specific one into `distros/<distro>/`.

### Everyday tasks

```bash
pixi run rs jazzy generate-recipes            # vinca -m for jazzy
pixi run rs jazzy build                       # build everything missing on its channel
pixi run rs jazzy build-one ros2-ros-workspace
pixi run rs jazzy check-patches
pixi run rs jazzy check-deps                  # pin conflicts, before building anything
pixi run rs jazzy create-snapshot             # refresh rosdistro_snapshot.yaml
pixi run rs jazzy render-pinning              # after editing shared/pinning or distro.yaml
pixi run rs add-package foxglove_bridge humble jazzy
pixi run sort                                 # all YAML files
pixi run rs check                             # sanity checks (also run in CI)
```

**Rebuilding a package without bumping its build number:** add it to `evict_cache` in
`distros/<distro>/ci.yaml` for the PR, and reset the file after merging. A full rebuild
of a distribution uses `full_rebuild: true` there. A new build number goes into its
`pkg_additional_info.yaml` (one package) or `vinca.yaml` (everything).

**Building only some distributions in a PR:** a PR builds the distributions it changes,
and all of them as soon as it touches anything outside `distros/<distro>/`. To limit
that (e.g. a humble rebuild that also changes `shared/` or `tools/`), add the label
`build:humble` (one per distribution to build), then push or re-run the build: adding a
label doesn't start one by itself. Nothing has to be reset before merging. A PR that
changes no packages (tooling, CI, docs) gets `build:none`: only the checks run.
Platform labels limit the platforms the same way: `build:linux`, `build:osx`, `build:win`
(or one platform, e.g. `build:linux-64`), e.g. to iterate on Windows fixes without
waiting for the macOS runners; remove them for a full build before merging.

### CI

- **Pull requests** (`testpr.yaml`):
  - A PR that only touches `distros/<distro>/` builds that distribution. Changes anywhere else (except documentation) build every distribution.
  - Each selected distribution is built on all five platforms, with its own build cache. The `check` job runs `pixi run rs check`.
- **After a merge** (`main.yaml`): for every changed distribution and platform, the job regenerates the recipes and the staged build workflow, and pushes them to `buildbranch_<distro>_<platform>`.
  - Each build branch carries its workflow as `.github/workflows/build.yaml`, which runs `.scripts/build_*.sh` in `distros/<distro>/work`.
  - The workflow builds in stages (by dependency depth), each stage in parallel jobs. Each job records how long its recipes took to build and keeps that as an artifact; `robostack-bot update-build-timings` merges them weekly into `shared/build_timings/<platform>.json` (each recipe's time per distribution). With those times, each stage's recipes are packed into jobs by time (vinca-gha `--timings`, each recipe's average over the distributions), so that no single job holds up its stage; until a platform has times, its jobs are cut by count as before.
  - Those workflows build the packages and upload them to the distribution's channel on the servers `upload_to` in `distro.yaml` lists (prefix.dev and/or anaconda.org, the same channel name on each; packages already there are skipped). `read_from` says where the builds look up what's already published.
  - A new push to a build branch cancels its run in progress: the new run skips what the old one uploaded. Re-run failed jobs only in a build branch's newest run, since re-running an older one cancels the newer run.
- **Repository variables:**
  - `ROBOSTACK_UPLOAD_CHANNEL`: upload only to this prefix.dev channel instead (for example a test channel), or `none` to build without uploading. Use the canonical reference: `<namespace>/<channel>` for a channel that isn't the namespace's primary one (e.g. `tobias-fischer/robostack-distro-test`).
  - Upload credentials:
    - prefix.dev: Repository Access (OIDC, no stored key). In each channel's *Settings > Repository Access*, authorize GitHub, this repository, workflow filename `build.yaml`, mode *Read/write*.
    - anaconda.org: one repository secret per distribution, `ANACONDA_API_TOKEN_<DISTRO>` (e.g. `ANACONDA_API_TOKEN_HUMBLE`): an API token of its `robostack-<distro>` organization with scope *Allow write access to the API site* (`api:write`). The generated build workflow passes it to the upload as `ANACONDA_API_TOKEN`.
  - `ROBOSTACK_BOT_CLIENT_ID` (the App's Client ID, `Iv23…`) with the secret `ROBOSTACK_BOT_PRIVATE_KEY`: the robostack-bot GitHub App. It pushes the build branches (which contain workflow files) and opens bot PRs so that CI runs on them. `GHA_PAT` works as a fallback.

  The app needs repository permissions Contents, Pull requests, Issues and Workflows (read and write), with its webhook inactive.

### robostack-bot

`.github/workflows/bot.yaml` runs `tools/maintenance.py`:

| command | who | what it does |
|---|---|---|
| `add-package <pkg>... [<distro>...]` | anyone | Adds ROS packages to the shared selection in `shared/vinca.yaml`, in sorted position; every distribution that has them builds them. Opens a PR listing, for the given (default: every) distribution, what will be built on linux-64 (and what is already published as a dependency). Valid ROS package names only, at most 10 per request. |
| `update-rosdistro-snapshot [<distro>... \| all]` | maintainers | Snapshots the distribution's latest rosdistro sync (the release team's tag, e.g. `jazzy/2026-10-05`, recorded as `rosdistro_sync` in `distro.yaml`; rosdistro master for `rosdistro_sync: manual`, like rolling), then sets `build_number` to the highest build of the distribution's released packages + 1, bumps the mutex minor version and removes all per-package build numbers: a full rebuild. The PR links the sync's announcement and lists the version bumps. Runs daily for distributions with a new sync. |
| `find-stale-packages [<distro>... \| all]` | maintainers | Published packages built against pins that no longer match, with the build-number snippet to rebuild them. Replies only. |
| `update-conda-forge-pinning [<distro>... \| all]` | maintainers | Moves the distribution's conda-forge pinning (`conda_forge_pinning_version` / `conda_forge_migrations` in `distro.yaml`, or `shared/pinning/conda_forge.yaml` for distributions that follow it) to the latest version, with the migrations that are done for the distribution's dependencies, and re-renders `conda_build_config.yaml`. Only packages that use a changed pin, and everything depending on them, are rebuilt (per-package build numbers); more than half affected means a full rebuild. The PR lists the rebuilt packages, the migrations used and those waiting for feedstocks, and the dependency check; it is labelled `dependency-conflict` when the pins can't be installed together. Runs daily, one PR per distribution. Its commits carry `[ci skip]`, so the daily update of the PR (a force push) doesn't start a build: to build it, push a commit without the marker (e.g. `git commit --allow-empty -m "Build"`). |
| `rebuild <pkg>... [<distro>... \| all] [--with-dependents]` | maintainers | New builds of ROS packages (dashes or underscores, `ros-<distro>-` / `ros2-` prefixes accepted) in the given distributions (default: every one that builds them): per-package build numbers in `distros/<distro>/pkg_additional_info.yaml`, above every released build. `--with-dependents` also rebuilds every package that depends on them (host or run, transitively; vinca's rebuild planner). Opens a PR listing the rebuilt packages. |
| `rebuild-dependents` | maintainers | Only as a comment on a pull request: commits the build-number entries the ABI check's comment suggests (its `yaml` blocks per distribution) to the pull request's branch, keeping existing entries' other keys. Pull requests from forks can't be pushed to: the bot replies with the entries to add instead. |
| `update-vinca [<ref>]` | maintainers | Moves the vinca pin in `pixi.toml` to the newest commit of the pinned repository's branch (`robostack-integration` for Tobias-Fischer/vinca, else its default branch) or of `<ref>`, runs `pixi lock`, re-renders the pinning and runs `rs check`. The PR links the comparison and lists the vinca commits. Labelled `build:none` (see below). |
| `dependency-report [<distro>... \| all]` | maintainers | Runs the dependency check (`check-deps`, linux-64) and keeps one issue per distribution, *`<distro>`: dependency report* (label `dependency-report`), up to date with the conflicts, notes and conda-forge migration status; the issue is closed when nothing conflicts. Runs weekly (Mondays). |
| `update-build-timings` | maintainers | Merges the build times the last week's build-branch runs recorded (`rs <distro> build-ci`, kept as each job's artifact) into `shared/build_timings/<platform>.json` and opens a PR. The build workflows pack each stage's recipes into jobs by these times. Runs weekly (Mondays). Labelled `build:none` (see below). |
| `update-pixi` | maintainers | Runs `pixi upgrade`: the newest versions of the tooling's dependencies in `pixi.toml` and `pixi.lock` (rattler-build, rattler-index, gh, pytest, ...), except Python (moved by hand) and requirements that are only an upper bound (`cmake = "<4.0"`: capped on purpose); the vinca pin is `update-vinca`'s. Runs `rs check`; the PR lists the changes as a table (pixi-diff-to-markdown). Runs monthly (on the 1st). Labelled `build:none` (see below). |

The PRs of `update-vinca`, `update-build-timings` and `update-pixi` don't change packages directly, but touch files outside `distros/`, which would build every distribution: they are labelled `build:none`, so only the checks run (and nothing is built while a build is under way). To build one, remove the label, or replace it with `build:<distro>` labels.

There are three ways to trigger a command:
- a *Package request* or *robostack-bot command* issue;
- a comment `@robostack-bot <command> ...` (no distribution or `all`: every distribution);
- *Actions › robostack-bot › Run workflow*.

"Maintainers" means the repository's owners, members and collaborators.

### Nightly package tests

`.github/workflows/package-tests.yaml` runs `tools/package_tests.py` every night (or via *Actions › package-tests › Run workflow*, for some distributions):
- **What:** every package with a test file (`shared/tests/<package>.yaml`, `distros/<distro>/tests/<package>.yaml`), in its newest build on the distribution's channel, with every variant of that build (e.g. each Python), on all five platforms.
- **How:** `rattler-build test` runs the tests bundled in the published package, with the newest dependencies from the distribution's channel and conda-forge. This catches breakages that come from conda-forge rather than from RoboStack, e.g. an ABI change in a dependency a package was built against.
- **Report:** one issue for all distributions, *Package tests* (label `package-tests`), with a section per distribution listing the failed tests and their output; it is closed when every test passes. Only the scheduled run (and a manual run of all distributions) on `main` updates it; other runs print the report in the log. Builds from before their test file was added carry no tests and are listed as such.

Locally:

```bash
pixi run python tools/package_tests.py run --distro jazzy --platform osx-arm64 --results results/jazzy.json [--package ros2-rclpy]
pixi run python tools/package_tests.py report results/
```

### New distribution

```bash
pixi run rs new-distro <name> --from rolling
```

This creates `distros/<name>/` with rolling's package selection and settings, but without its build numbers, patches or own pins. It also generates the snapshot and `conda_build_config.yaml`, and prints a checklist: mutex, channel, upload credentials, and patches to port.

### Moving a distribution here from its own repository

```bash
pixi run python tools/import_distro.py ../ros-<distro> --ref origin/main
pixi run rs <distro> render-pinning && pixi run sort && pixi run rs check
```

The import leaves out what `shared/` already provides (shared package selections and entries, derived keys) and keeps comments. It writes `distro.yaml` from the old repository's `pixi.toml` and `vinca_pinning.yaml`. Run it again to pick up changes made in the old repository until that is archived.

**Check:** re-importing lyrical from its repository reproduces `distros/lyrical/` exactly. The full linux-64 recipe sets of rolling, humble and lyrical generated here are byte-identical to those of their own repositories.

