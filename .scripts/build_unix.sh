#!/usr/bin/env bash


# Default values
target=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --target) target="$2"; shift ;;  # Capture value for --target
        *) echo "Unknown parameter passed: $1"; exit 1 ;;  # Handle invalid options
    esac
    shift
done

set -xeuo pipefail
export PYTHONUNBUFFERED=1

# ROBOSTACK_DISTRO is set by the generated build workflow (tools/robostack.py gha).
: "${ROBOSTACK_DISTRO:?ROBOSTACK_DISTRO is not set}"
cd "distros/${ROBOSTACK_DISTRO}/work"
export FEEDSTOCK_ROOT=`pwd`
export "CONDA_BLD_PATH=$HOME/conda-bld/"

if [[ "$target" == *"osx"* ]]; then
    echo "osx"
    # Spotlight indexes every file the environments and builds write, using up to a
    # core or two of the runner's three or four; the runner is thrown away after the job
    sudo mdutil -a -i off || true
    export PATH=$(echo $PATH | tr ":" "\n" | grep -v 'homebrew' | xargs | tr ' ' ':')
fi

# All of the job's recipes in one build-ci run (tools/robostack.py), which builds
# independent ones in parallel and adds the variant config and the distro's channels.
recipe_args=()
for recipe in ${CURRENT_RECIPES[@]}; do
	recipe_args+=(--recipe "${FEEDSTOCK_ROOT}/recipes/${recipe}")
done
pixi run -v rs "${ROBOSTACK_DISTRO}" build-ci "${recipe_args[@]}" --output-dir $CONDA_BLD_PATH

# Check if it build something, this is a hotfix for the skips inside additional_recipes
shopt -s nullglob
if [[ -n "$target" ]]; then
    # noarch packages (e.g. the compatibility packages) are uploaded from every platform;
    # the first upload wins and the others skip
    conda_packages=( "${CONDA_BLD_PATH}/${target}"*/*.conda "${CONDA_BLD_PATH}/noarch/"*.conda )
else
    conda_packages=( "${CONDA_BLD_PATH}/"*/*.conda )
fi
if (( ${#conda_packages[@]} > 0 )); then
    # Upload packages one-by-one; the upload task (tools/robostack.py) uploads them to
    # prefix.dev and anaconda.org and skips packages that are already there.
    for conda_package in "${conda_packages[@]}"; do
        pixi run rs "${ROBOSTACK_DISTRO}" upload "${conda_package}"
    done
else
    echo "Warning: No .conda files found in ${CONDA_BLD_PATH}/${target}"
    echo "This might be due to all the packages being skipped"
fi
