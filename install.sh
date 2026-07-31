#!/usr/bin/env bash
#
# Install rvcs so it runs from anywhere as `rvcs`.
#
#   ./install.sh              # create/refresh .venv, editable install, link into ~/.local/bin
#   ./install.sh --uninstall  # remove the symlink (leaves .venv alone)
#
# The install is editable: edits to rvcs.py take effect with no reinstall.
# Ubuntu's system python is externally managed (PEP 668), so a venv is not
# optional here — pip install --user is refused.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
BIN_DIR="${RVCS_BIN_DIR:-$HOME/.local/bin}"
LINK="$BIN_DIR/rvcs"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$LINK"
    echo "Removed $LINK"
    exit 0
fi

# Create the venv if missing. Prefer uv; fall back to stdlib venv, which needs
# the python3-venv package (ensurepip) that Ubuntu ships separately.
if [[ ! -x "$VENV/bin/python" ]]; then
    if command -v uv >/dev/null 2>&1; then
        uv venv --seed "$VENV"
    elif python3 -m venv --help >/dev/null 2>&1; then
        python3 -m venv "$VENV"
    else
        echo "error: need 'uv' or python3-venv to create $VENV" >&2
        echo "       apt install python3-venv   (or install uv)" >&2
        exit 1
    fi
fi

# A venv copied in from another machine keeps the original interpreter path in
# every console-script shebang, so ./.venv/bin/pip fails with "required file
# not found". Going through python -m pip sidesteps that.
if command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$VENV" uv pip install -e "$REPO"
else
    "$VENV/bin/python" -m pip install -e "$REPO"
fi

mkdir -p "$BIN_DIR"
ln -sfn "$VENV/bin/rvcs" "$LINK"

echo
echo "Installed: $LINK -> $VENV/bin/rvcs"
"$LINK" --version

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       echo "warning: $BIN_DIR is not on your PATH — add this to ~/.bashrc:"
       echo "    export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac
