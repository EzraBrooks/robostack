# AGENTS.md

Working notes for coding agents in the RoboStack all-distributions repository. The
step-by-step procedures are skills in `.claude/skills/` (plain markdown, also useful
to read without Claude):

| skill | for |
|---|---|
| `robostack-debug-build` | a package fails to build: find the work dir, read the log, common fixes |
| `robostack-patch` | create, refresh, place and port patches; check that they apply |
| `robostack-versions` | snapshot and conda-forge pinning updates, build number / mutex bumps, full rebuilds, stale packages |
| `robostack-package-selection` | add or exclude packages in `vinca.yaml`, platform conditions, gap report |

## Layout and rules

- Every distribution lives in `distros/<distro>/` (only distro-specific data); shared
  data is in `shared/` (vinca.yaml, pkg_additional_info.yaml, dependencies.yaml,
  robostack.yaml, pinning, tests), tools in `tools/`. See README.md for the layout and
  how they combine. Never edit `distros/<d>/work/`: every task regenerates it.
- A fix that applies to every distribution goes into `shared/`; a version-specific one
  into `distros/<d>/`.
- Run everything per distribution with `pixi run rs <distro> <task>`, from the
  repository root. Package names are `ros2-<pkg>` (dashes), e.g. `ros2-rsl`.
- lyrical builds packages with Python content for Python 3.12-3.14 and the others once
  (`conditional_python_dependency`, see README "Python versions"). A built-once package
  that fails its `package_contents` test ships Python after all: add `python` to its
  `add_host` in `patch/dependencies.yaml`.
- Never edit `conda_build_config.yaml` by hand: change `shared/pinning/` or the
  distribution's `distro.yaml`, then `pixi run rs <distro> render-pinning` (CI checks it).
- A change outside `distros/` rebuilds every distribution in CI; keep such PRs focused.
- `pixi run rs check` must pass (CI runs it): every distribution assembles, generated
  files are up to date, YAML is sorted (`pixi run sort` fixes that).
- Moving a distribution from its old repository: `tools/import_distro.py` (see README).

## Session defaults

- Prefer easy, low-risk build fixes first (one-line CMake / include / C++ standard
  fixes); move on when a failure becomes complex and come back to it later.
- Run build/debug loops yourself instead of asking the maintainer to run commands.
- Patch against the checked-out sources in `distros/<d>/work/output/bld/` and
  `distros/<d>/work/output/src_cache/`.
- A package that only works on some platforms goes behind a platform condition in
  `vinca.yaml` (see `robostack-package-selection`), not an ad-hoc comment.
- One patch per package and platform:
  `distros/<d>/patch/ros2-<pkg>[.osx|.linux|.win|.unix].patch`. Packages are only built
  under the new `ros2-<pkg>` names (`package_name_mode: new`); write dependencies in
  `dependencies.yaml` as `ros2-<pkg>` too.

## Everyday commands

```bash
pixi run rs <d> generate-recipes                # vinca -m (recipes in distros/<d>/work/recipes)
pixi run rs <d> build-one ros2-<pkg>            # one package (preferred for debugging)
pixi run rs <d> build --continue-on-failure     # everything missing on the channel
pixi run rs <d> check-patches [--recipe ros2-<pkg>]
pixi run rs <d> check-deps                      # pin conflicts, before building anything
pixi run rs <d> gap-report                      # recipes without artifacts and vice versa
pixi run rs <d> python-audit [--elf]            # per-Python vs built-once recipes against the published packages
pixi run rs add-package <pkg>... [<d>...]       # select packages (as the bot does)
pixi run rs check                               # sanity checks (CI)
```

## Dependency-aware parallel work

Splitting work across agents is fine for independent packages only. Infer
dependencies from `requirements.host` / `requirements.run` of the generated recipes;
if A depends on B, build and fix B first; if unsure, serialize. Different
distributions never depend on each other, but they share `shared/`: only one agent
should edit a shared file at a time.

## CI

- PR builds cache the built packages per PR. A package that changes without a new
  build number (e.g. a patch fix) must be evicted: add it to `evict_cache` in
  `distros/<d>/ci.yaml` (`full_rebuild: true` for everything), and reset the file
  after merging.
- A PR builds the distributions it changes (all of them if it touches anything
  outside `distros/<d>/`). To build only some, add `build:<distro>` labels (e.g.
  `build:humble`), then push or re-run the build (adding a label doesn't start one).
  `build:linux` / `build:osx` / `build:win` (or `build:<platform>`) limit the platforms;
  remove them for one full build before merging.
- After a merge, `main.yaml` regenerates the staged build branches
  `buildbranch_<distro>_<platform>`; uploads go to the channel in `distro.yaml`, or to
  the repository variable `ROBOSTACK_UPLOAD_CHANNEL`.
- When cancelling and restarting a run, wait for the cancelled run's cache save to
  finish (a few minutes) or its packages are lost.
