#!/usr/bin/env python3
"""
RVCS - ROS VCS Workspace Manager

A tool for managing ROS/catkin workspaces with git repositories.
Supports exporting/importing workspace state using vcstool format
with uncommitted changes preservation.

Library usage:
    from rvcs import scan_workspace, get_git_info_dict
    from rvcs import export_workspace_state, import_workspace_state

    # Scan a workspace directory
    results = scan_workspace('/path/to/workspace')

    # Get info for a single git repo
    info = get_git_info_dict('/path/to/repo')

    # Export workspace state (vcstool .repos + diffs for dirty repos)
    export_workspace_state('/path/to/workspace', output_dir='/tmp/export')

    # Import workspace state
    import_workspace_state('workspace.workspace.zip', '/path/to/new/workspace')

CLI usage:
    # Show git status table
    rvcs [workspace]

    # Export workspace state to zip (contains .repos and .state.yaml)
    rvcs --export-state [workspace]

    # Export a PIPELINE: the subset of the workspace listed in a pipeline
    # definition file, plus its tmuxinator session configs
    rvcs --export-pipeline overhang.pipeline.yaml [workspace]

    # Status table restricted to the repos of one pipeline
    rvcs --pipeline overhang.pipeline.yaml [workspace]

    # Import workspace from zip file (includes uncommitted changes)
    rvcs --import-state workspace.workspace.zip [output_dir]

    # Import from .repos file only (no uncommitted changes)
    rvcs --import-state workspace.repos [output_dir]

    # Import from .repos with separate state file
    rvcs --import-state workspace.repos --state-file workspace.state.yaml [output_dir]
"""

import argparse
import base64
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import json
import yaml
import zipfile
import tempfile
from io import StringIO
from contextlib import redirect_stdout
from tabulate import tabulate
from datetime import datetime
import git

# vcstool imports
from vcstool.commands.export import main as vcs_export
from vcstool.commands.import_ import main as vcs_import

__version__ = "1.4.0"

# Workspace-level colcon configuration, captured on export and restored on
# import. colcon reads colcon_defaults.yaml from the directory it runs in, so
# restoring it is what lets a workspace pin its own build settings instead of
# every importer passing them by hand.
COLCON_CONFIG_FILES = ('colcon_defaults.yaml', '.colcon/config.yaml')

# Canonical, workspace-independent home for restored pipeline definitions —
# same role as ~/.config/tmuxinator/ for tmuxinator configs. Not under any
# particular output_dir: a pipeline definition describes a slice of a
# workspace, and belongs in one place regardless of which workspace an
# import lands in.
#
# Layout: PIPELINE_CONFIG_DIR/<name>/<name>.pipeline.yaml, each <name>/ its
# OWN git repo (not one shared repo) — so each pipeline's history is its own
# clean log, distinguishable at the filesystem level (ls PIPELINE_CONFIG_DIR
# already lists every known pipeline), and diffable/mergeable independently.
PIPELINE_CONFIG_DIR = os.path.expanduser('~/.config/ros_vcs/pipeline')

# Module-level debug flag (set by CLI)
_debug = False

# Max width for the Package column in the status table. A very long package
# name (e.g. a deep relative path) otherwise stretches the whole table and
# wraps onto the next line. Display only — the underlying data (and JSON
# export) keep the full name.
MAX_PACKAGE_NAME_WIDTH = 40


def truncate_name(text, width=MAX_PACKAGE_NAME_WIDTH):
    """Truncate a display string to ``width`` chars, marking cuts with '…'."""
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + '…'


def load_ignore_packages(ignore_file):
    """Load package names to ignore from a file."""
    if not ignore_file or not os.path.exists(ignore_file):
        return set()

    ignore_packages = set()
    try:
        with open(ignore_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    ignore_packages.add(line)
    except Exception as e:
        print(f"Warning: Could not read ignore file {ignore_file}: {e}")

    return ignore_packages


def load_pipeline(pipeline_file):
    """
    Load a pipeline definition file (YAML).

    A pipeline names the subset of a workspace that one line of work uses,
    plus the tmuxinator session configs that bring it up:

        name: overhang_rejection          # required
        workspace: ~/marv_ws              # optional default workspace
        repos:                            # paths relative to src/ — each entry
          - overhang_research             #   includes its whole subtree (nested
          - robot_rodeo_gym_ros2          #   repos inside it are included too)
        tmuxinator:                       # tmuxinator configs bundled verbatim
          - ~/.config/tmuxinator/marv_overhang.yml
        extra_paths:                      # optional NON-repo paths (relative to
          - src/marv_elevation_mapping    #   the workspace root) copied raw

    Returns the pipeline dict with defaults filled in.
    """
    with open(pipeline_file, 'r') as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not data.get('name'):
        raise ValueError(f"{pipeline_file}: pipeline file must be a YAML mapping "
                         f"with at least a 'name' key")
    data.setdefault('repos', [])
    data.setdefault('tmuxinator', [])
    data.setdefault('extra_paths', [])
    if data.get('workspace'):
        data['workspace'] = os.path.expanduser(data['workspace'])
    data['tmuxinator'] = [os.path.expanduser(p) for p in data['tmuxinator']]
    return data


def _pipeline_repo_dir(name):
    return os.path.join(PIPELINE_CONFIG_DIR, name)


def _pipeline_yaml_basename(name):
    """The definition file inside a pipeline's store repo. Canonically
    <name>.pipeline.yaml, but the DIRECTORY name is the identity the CLI
    uses — a user renaming the directory must not orphan the store — so
    fall back to whatever single *.pipeline.yaml the repo carries."""
    d = _pipeline_repo_dir(name)
    exact = f'{name}.pipeline.yaml'
    if os.path.isfile(os.path.join(d, exact)):
        return exact
    cands = sorted(f for f in os.listdir(d) if f.endswith(('.pipeline.yaml',
                                                           '.pipeline.yml'))) \
        if os.path.isdir(d) else []
    return cands[0] if cands else exact


def _pipeline_file_in_repo(name):
    return os.path.join(_pipeline_repo_dir(name), _pipeline_yaml_basename(name))


def list_pipeline_names():
    """Names of every pipeline in the canonical store — one git repo per
    DIRECTORY under PIPELINE_CONFIG_DIR (the directory name is the CLI
    name, surviving renames). Directories without a .git or without any
    *.pipeline.yaml are not pipelines; skipped rather than erroring, so
    stray files don't break listing."""
    if not os.path.isdir(PIPELINE_CONFIG_DIR):
        return []
    names = []
    for n in sorted(os.listdir(PIPELINE_CONFIG_DIR)):
        d = _pipeline_repo_dir(n)
        if os.path.isdir(os.path.join(d, '.git')) and os.path.isfile(_pipeline_file_in_repo(n)):
            names.append(n)
    return names


def _git_identity_args(repo_dir):
    """[] if the repo (or global config) already has a committer identity,
    else -c overrides for a neutral one — same fallback _merge_zip_commits
    uses, so unattended commits (import, this store) never fail on a bare
    'cnuc'-style environment with no configured git identity."""
    import subprocess
    if subprocess.run(['git', '-C', repo_dir, 'config', 'user.email'],
                      capture_output=True).stdout.strip():
        return []
    return ['-c', 'user.name=rvcs', '-c', 'user.email=rvcs@localhost']


def pipeline_source_path(name):
    """Resolve a pipeline NAME (not a path) to a real file path holding its
    LATEST COMMITTED definition — read via `git show HEAD:...`, so an
    uncommitted edit sitting in the store's working tree is never used as
    the canonical content; only what's actually been committed counts.
    Written to a temp file (real path, for load_pipeline/open() callers) and
    returned. Raises FileNotFoundError with the list of known names if there
    is no such pipeline."""
    import subprocess
    repo_dir = _pipeline_repo_dir(name)
    if not os.path.isdir(os.path.join(repo_dir, '.git')):
        known = list_pipeline_names()
        hint = ('Known pipelines: ' + ', '.join(known)) if known else \
               f'No pipelines stored yet under {PIPELINE_CONFIG_DIR}.'
        raise FileNotFoundError(f"No pipeline named '{name}' and no such file. {hint}")
    r = subprocess.run(['git', '-C', repo_dir, 'show',
                        f'HEAD:{_pipeline_yaml_basename(name)}'],
                       capture_output=True)
    if r.returncode != 0:
        raise FileNotFoundError(
            f"pipeline '{name}': could not read the latest commit "
            f"({r.stderr.decode('utf-8', 'replace').strip()})")
    tmp_dir = tempfile.mkdtemp(prefix='rvcs_pipeline_')
    tmp_path = os.path.join(tmp_dir, f'{name}.pipeline.yaml')
    with open(tmp_path, 'wb') as f:
        f.write(r.stdout)
    return tmp_path


def resolve_pipeline_arg(value):
    """A --pipeline/--export-pipeline argument (or the bare-name CLI
    shortcut) is either a real path or a pipeline NAME to resolve against
    the canonical store. Paths win: if it exists on disk, or looks like one
    (has a path separator or a .yaml/.yml suffix), it's used as-is —
    resolution only kicks in for bare tokens that are neither."""
    if os.path.exists(value) or os.sep in value or value.endswith(('.yaml', '.yml')):
        return value
    return pipeline_source_path(value)


def commit_pipeline_snapshot(name, content_bytes, message, author_date=None):
    """Write content_bytes as <name>.pipeline.yaml into its own canonical
    repo (created on first use), and commit if it actually changed anything
    — this is the versioning step: every import of a pipeline zip becomes
    one commit in that pipeline's own history. author_date (a
    'YYYY-MM-DDTHH:MM:SS' string) backdates the commit to when the snapshot
    was actually exported, not when it happened to be imported; None uses
    now. Returns the short commit hash, or None if nothing changed."""
    import subprocess
    target = name
    if not os.path.isdir(_pipeline_repo_dir(name)):
        # a renamed store dir keeps its identity: if some existing pipeline's
        # definition declares this name, version there instead of forking a
        # fresh directory next to it
        for n in list_pipeline_names():
            try:
                if load_pipeline(_pipeline_file_in_repo(n)).get('name') == name:
                    target = n
                    break
            except Exception:
                continue
    repo_dir = _pipeline_repo_dir(target)
    dest = _pipeline_file_in_repo(target)
    os.makedirs(repo_dir, exist_ok=True)
    if not os.path.isdir(os.path.join(repo_dir, '.git')):
        subprocess.run(['git', 'init', '-q', '-b', 'main', repo_dir], check=True)
    with open(dest, 'wb') as f:
        f.write(content_bytes)
    ident = _git_identity_args(repo_dir)
    subprocess.run(['git', '-C', repo_dir, 'add', os.path.basename(dest)], check=True)
    if subprocess.run(['git', '-C', repo_dir, 'diff', '--cached', '--quiet']).returncode == 0:
        return None   # identical to HEAD (or an empty repo with nothing staged) -- no-op
    env = dict(os.environ)
    if author_date:
        env['GIT_AUTHOR_DATE'] = author_date
        env['GIT_COMMITTER_DATE'] = author_date
    r = subprocess.run(['git'] + ident + ['-C', repo_dir, 'commit', '-q', '-m', message],
                       env=env, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"commit failed for pipeline '{name}': "
                           f"{r.stderr.decode('utf-8', 'replace').strip()}")
    return subprocess.run(['git', '-C', repo_dir, 'rev-parse', '--short', 'HEAD'],
                          capture_output=True, text=True).stdout.strip()


def repo_in_include_paths(rel_path, include_paths):
    """
    True if a repo (path relative to src/) is selected by an include list.
    None means no filtering (everything included). Matching is by exact path
    or subtree: 'a/b' matches include entry 'a' (nested repos come along).
    """
    if include_paths is None:
        return True
    rel = rel_path.replace(os.sep, '/')
    for entry in include_paths:
        e = str(entry).strip().strip('/').replace(os.sep, '/')
        if rel == e or rel.startswith(e + '/'):
            return True
    return False


def _debug_print(message):
    """Print debug message if debug mode is enabled."""
    if _debug:
        print(f"\nDebug: {message}")


def get_git_info_dict(folder, debug=False):
    """
    Get git repository information as a dictionary.

    Args:
        folder: Path to the git repository folder
        debug: Enable debug output

    Returns:
        Dictionary with keys: package, branch, hash, local_changes, remote_changes, url
        Returns None if folder is not a git repository
    """
    global _debug
    old_debug = _debug
    _debug = debug

    # .git is a directory for normal repos and a "gitdir: ..." pointer FILE for
    # submodule-style checkouts — both are valid repositories (GitPython follows
    # the pointer transparently)
    if not os.path.exists(os.path.join(folder, '.git')):
        _debug = old_debug
        return None

    try:
        repo = git.Repo(folder)

        # Get basic info
        try:
            branch_name = repo.active_branch.name
        except TypeError:
            # Detached HEAD state
            branch_name = f"({repo.head.commit.hexsha[:7]})"

        commit_hash = repo.head.commit.hexsha[:7]

        # Get remote URL
        if 'origin' in repo.remotes:
            repo_url = list(repo.remotes.origin.urls)[0]
        else:
            repo_url = "-"

        _debug_print(f"Folder: {os.path.basename(folder)}, {branch_name}\n--------------------------")

        # Check for local changes (dirty working directory including untracked files)
        is_dirty = repo.is_dirty(untracked_files=True)
        local_changes = "yes" if is_dirty else "no"

        # Remote changes: the LOCAL BRANCH vs its upstream tracking ref --
        # purely local (uses the last fetch, no network). 'N to merge' =
        # fetched-but-not-merged commits sitting on origin/<branch>; 'N to
        # push' = local commits the remote lacks. (The compare tool overwrites
        # this column with its own remote-PC meaning, as before.)
        remote_changes = "no"
        try:
            tb = repo.active_branch.tracking_branch()
            if tb is not None:
                behind = int(repo.git.rev_list('--count',
                             f'{repo.active_branch.name}..{tb.name}') or 0)
                ahead = int(repo.git.rev_list('--count',
                            f'{tb.name}..{repo.active_branch.name}') or 0)
                bits = []
                if behind:
                    bits.append(f'{behind} to merge')
                if ahead:
                    bits.append(f'{ahead} to push')
                if bits:
                    remote_changes = ', '.join(bits)
        except Exception:
            pass

        _debug_print(f"Local changes: {local_changes}")

        _debug = old_debug
        return {
            'package': os.path.basename(folder),
            'branch': branch_name,
            'hash': commit_hash,
            'local_changes': local_changes,
            'remote_changes': remote_changes,
            'url': repo_url,
            'dirty': is_dirty
        }

    except Exception as e:
        _debug_print(f"Git error in {folder}: {e}")
        _debug = old_debug
        return None


def scan_workspace(workspace_path, ignore_packages=None, debug=False):
    """
    Scan a workspace directory for git repositories.

    Args:
        workspace_path: Path to the workspace (will check for 'src' subfolder)
        ignore_packages: Set of package names to ignore
        debug: Enable debug output

    Returns:
        List of dictionaries with git info for each repository found
    """
    if ignore_packages is None:
        ignore_packages = set()

    # Check if workspace has a src folder
    source_folder = os.path.join(workspace_path, "src")
    if not os.path.exists(source_folder):
        source_folder = workspace_path

    results = []
    for folder_path in find_git_repos(source_folder, ignore_packages):
        info = get_git_info_dict(folder_path, debug=debug)
        if info:
            # Show nested repos by their path relative to the source folder
            rel = os.path.relpath(folder_path, source_folder)
            if os.sep in rel:
                info['package'] = rel
            results.append(info)

    return results


def find_git_repos(source_folder, ignore_packages=None):
    """
    Recursively find git repositories under source_folder, including
    repositories nested inside other repositories (e.g. submodule-style
    checkouts). Returns a sorted list of repository paths.
    """
    if ignore_packages is None:
        ignore_packages = set()

    repos = []
    for root, dirs, files in os.walk(source_folder):
        if '.git' in dirs:
            dirs.remove('.git')
        if os.path.basename(root) in ignore_packages:
            dirs[:] = []
            continue
        if os.path.exists(os.path.join(root, '.git')) and root != source_folder:
            repos.append(root)
        # keep walking into the repo so nested repositories are found too
    return sorted(repos)


def export_to_json(results, output_path=None, workspace_name=None):
    """
    Export scan results to JSON file.

    Args:
        results: List of dictionaries from scan_workspace()
        output_path: Optional path for output file. If None, generates timestamped filename.
        workspace_name: Name to use in filename (default: 'workspace')

    Returns:
        Path to the created JSON file
    """
    if output_path is None:
        ws_name = workspace_name or 'workspace'
        date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_path = f"{ws_name}_{date_str}.json"

    # Convert to legacy format for backwards compatibility
    json_data = []
    for info in results:
        json_data.append({
            'Package': info['package'],
            'Branch': info['branch'],
            'Local Hash': info['hash'],
            'Local': info['local_changes'],
            'Remote': info['remote_changes'],
            'Url': info['url']
        })

    with open(output_path, 'w') as f:
        json.dump(json_data, f, indent=4)

    return output_path


def get_git_info(folder):
    """
    Legacy function for backwards compatibility.
    Returns list format: [package, branch, hash, local_changes, remote_changes, url]
    """
    info = get_git_info_dict(folder, debug=_debug)
    if info is None:
        return None
    return [
        info['package'],
        info['branch'],
        info['hash'],
        info['local_changes'],
        info['remote_changes'],
        info['url']
    ]


def find_catkin_workspace(folder):
    """Find the root of a catkin workspace by looking for .catkin_tools folder."""
    if os.path.isdir(os.path.join(folder, '.catkin_tools')):
        return folder
    else:
        parent_folder = os.path.dirname(folder)
        if parent_folder == folder:  # reached root directory
            return None
        elif parent_folder == os.path.expanduser('~'):  # reached user directory
            return None
        else:
            return find_catkin_workspace(parent_folder)


def export_vcs_repos(workspace_path, output_file=None, exact=True, ignore_packages=None,
                     include_paths=None, suppress_unpushed_warnings=None):
    """
    Export workspace repositories using vcstool format (YAML).

    Args:
        workspace_path: Path to the workspace directory
        output_file: Optional output file path. If None, returns YAML string.
        exact: If True, export exact commit hashes instead of branch names
        ignore_packages: Set of directory basenames to exclude (each prunes its
            whole subtree, so nested repos inside an ignored repo are excluded too).
            Enables PER-RESEARCH workspace images from a shared workspace.
        include_paths: Optional list of src-relative paths to INCLUDE (subtree
            semantics, see repo_in_include_paths). None = include everything.
            This is how pipelines carve their slice out of the workspace.

    Returns:
        Path to output file if output_file provided, otherwise YAML string
    """
    # Determine source folder
    source_folder = os.path.join(workspace_path, "src")
    if not os.path.exists(source_folder):
        source_folder = workspace_path

    # Build the manifest ourselves (vcstool-compatible YAML) instead of calling
    # `vcs export`: vcstool cannot see submodule-style repos (.git pointer FILE)
    # and silently DROPS any repo whose HEAD commit is not on a remote — for a
    # backup tool both must be recorded. Nested repositories (repos inside
    # repos) are always included; the diff capture in export_workspace_state()
    # walks them too, so manifest and state agree.
    repositories = {}
    for repo_path in find_git_repos(source_folder, ignore_packages):
        rel = os.path.relpath(repo_path, source_folder)
        if not repo_in_include_paths(rel, include_paths):
            continue
        try:
            repo = git.Repo(repo_path)
            url = list(repo.remotes.origin.urls)[0] if 'origin' in repo.remotes else None
            version = repo.head.commit.hexsha if exact else None
            if not exact or version is None:
                try:
                    version = repo.active_branch.name
                except TypeError:
                    version = repo.head.commit.hexsha
            suppressed = rel in (suppress_unpushed_warnings or set())
            if url is None:
                if not suppressed:
                    print(f"Warning: {rel}: no 'origin' remote — recorded without url, "
                          f"import will not be able to clone it")
            elif exact and not suppressed:
                # warn (but still record) when the exact hash exists on no remote
                on_remote = repo.git.branch('-r', '--contains', version) if repo.remotes else ''
                if not on_remote.strip():
                    print(f"Warning: {rel}: HEAD {version[:7]} not found on any remote "
                          f"— push it before this manifest can be restored")
            repositories[rel] = {'type': 'git', 'url': url, 'version': version}
        except Exception as e:
            print(f"Warning: could not export {rel}: {e}")

    yaml_content = yaml.dump({'repositories': repositories}, default_flow_style=False)

    if output_file:
        with open(output_file, 'w') as f:
            f.write(yaml_content)
        return output_file
    return yaml_content


def get_repo_diff(repo_path):
    """
    Get complete diff for a repository including staged, unstaged, and untracked files.

    Args:
        repo_path: Path to the git repository

    Returns:
        Dictionary with 'staged_diff', 'unstaged_diff', and 'untracked_files'
        Returns None if repo is clean
    """
    try:
        repo = git.Repo(repo_path)

        if not repo.is_dirty(untracked_files=True):
            return None

        result = {
            'staged_diff': '',
            'unstaged_diff': '',
            'untracked_files': {}
        }

        # Get staged changes (ensure trailing newline for git apply).
        # --binary: without it, binary-file changes come out as non-applyable
        # "Binary files differ" stubs and git apply rejects the whole patch on
        # import (hit in the wild: upstream elevation_mapping has committed
        # build/ artifacts, deleted locally). Base64 transport keeps it safe.
        staged_diff = repo.git.diff('--binary', '--cached')
        if staged_diff:
            if not staged_diff.endswith('\n'):
                staged_diff += '\n'
            result['staged_diff'] = staged_diff

        # Get unstaged changes (ensure trailing newline for git apply)
        unstaged_diff = repo.git.diff('--binary')
        if unstaged_diff:
            if not unstaged_diff.endswith('\n'):
                unstaged_diff += '\n'
            result['unstaged_diff'] = unstaged_diff

        # Get untracked files with their contents
        untracked = repo.untracked_files
        for filepath in untracked:
            full_path = os.path.join(repo_path, filepath)
            try:
                with open(full_path, 'rb') as f:
                    content = f.read()
                    # Try to decode as text, otherwise store as base64
                    try:
                        result['untracked_files'][filepath] = {
                            'content': content.decode('utf-8'),
                            'binary': False
                        }
                    except UnicodeDecodeError:
                        result['untracked_files'][filepath] = {
                            'content': base64.b64encode(content).decode('ascii'),
                            'binary': True
                        }
            except Exception as e:
                _debug_print(f"Could not read untracked file {filepath}: {e}")

        # Check if there's anything to return
        if not result['staged_diff'] and not result['unstaged_diff'] and not result['untracked_files']:
            return None

        return result

    except Exception as e:
        _debug_print(f"Error getting diff for {repo_path}: {e}")
        return None


def create_repo_bundle(repo_path, bundle_file, url=None):
    """
    Create a git bundle for a repo whose HEAD no remote can serve.

    If the repo has a usable url AND remote-tracking refs, the bundle contains
    only the commits missing from the remotes (small, incremental) — restoring
    clones from the url first, then fetches the bundle on top. Otherwise the
    bundle is fully self-contained (--all) and restorable with no remote at all
    (covers repos whose configured remote does not actually exist).

    Returns True if the bundle is self-contained, False if incremental.
    """
    repo = git.Repo(repo_path)
    has_remote_refs = bool(repo.git.for_each_ref('refs/remotes'))
    self_contained = url is None or not has_remote_refs
    if self_contained:
        # HEAD + --all only: also naming the branch would duplicate its ref
        # in the bundle, which 'git clone <bundle>' rejects
        args = ['HEAD', '--all']
    else:
        args = ['HEAD']
        try:
            args.append(repo.active_branch.name)
        except TypeError:
            pass  # detached HEAD
        args += ['--not', '--remotes']
    repo.git.bundle('create', bundle_file, *args)
    return self_contained


def export_workspace_state(workspace_path, output_dir=None, workspace_name=None, ignore_packages=None,
                           include_paths=None, extra_files=None, zip_suffix='.workspace.zip'):
    """
    Export complete workspace state to a zip file containing vcstool repos and diffs.

    Args:
        workspace_path: Path to the workspace directory
        output_dir: Directory for output zip file. If None, uses current directory.
        workspace_name: Name for output file (default: basename of workspace)
        include_paths: Optional src-relative include list (pipeline slice);
            applied to BOTH the manifest and the dirty-state capture.
        extra_files: Optional dict {arcname: source} of additional zip entries;
            source is a filesystem path (str) or literal content (bytes).
        zip_suffix: Output filename suffix (pipelines use '.pipeline.zip')

    Returns:
        Path to created zip file
    """
    if output_dir is None:
        output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    ws_name = workspace_name or os.path.basename(os.path.abspath(workspace_path)) or 'workspace'
    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Determine source folder
    source_folder = os.path.join(workspace_path, "src")
    if not os.path.exists(source_folder):
        source_folder = workspace_path

    # Bundle repos that no remote can restore (unpushed HEAD, no remote, or a
    # configured remote that does not actually exist) so the zip stays
    # self-sufficient. Incremental bundles carry only the unpushed commits.
    bundle_tmp = tempfile.mkdtemp(prefix='rvcs_bundles_')
    bundles = {}       # rel -> {'file': arcname, 'self_contained': bool}
    bundle_paths = {}  # rel -> tmp file on disk
    for root in find_git_repos(source_folder, ignore_packages):
        rel = os.path.relpath(root, source_folder)
        if not repo_in_include_paths(rel, include_paths):
            continue
        try:
            repo = git.Repo(root)
            url = list(repo.remotes.origin.urls)[0] if 'origin' in repo.remotes else None
            head = repo.head.commit.hexsha
            on_remote = repo.git.branch('-r', '--contains', head) if repo.remotes else ''
            if url and on_remote.strip():
                continue  # restorable from its remote, no bundle needed
            bundle_file = os.path.join(bundle_tmp, rel.replace(os.sep, '__') + '.bundle')
            self_contained = create_repo_bundle(root, bundle_file, url)
            bundles[rel] = {'file': f'bundles/{rel}.bundle', 'self_contained': self_contained}
            bundle_paths[rel] = bundle_file
            kind = 'full history' if self_contained else 'unpushed commits only'
            print(f"  Bundled {rel} ({kind}): HEAD {head[:7]} not restorable from a remote")
        except Exception as e:
            print(f"Warning: could not bundle {rel}: {e}")

    # Get vcstool repos content
    repos_content = export_vcs_repos(workspace_path, exact=True, ignore_packages=ignore_packages,
                                     include_paths=include_paths,
                                     suppress_unpushed_warnings=set(bundles))

    # Collect diffs for dirty repos.
    # workspace_root records where this workspace lived at export time; import
    # rewrites that prefix out of the pipeline payload (tmuxinator configs and
    # pipeline.yaml routinely hardcode it) when restoring somewhere else.
    state_data = {
        'workspace_name': ws_name,
        'workspace_root': os.path.abspath(workspace_path),
        'export_date': date_str,
        'dirty_repos': {}
    }
    if bundles:
        state_data['bundles'] = bundles

    # Find all git repos and check for dirty state (same discovery + ignore
    # semantics as the manifest, so both sides of the export always agree)
    for root in find_git_repos(source_folder, ignore_packages):
        rel_path = os.path.relpath(root, source_folder)
        if not repo_in_include_paths(rel_path, include_paths):
            continue
        diff_data = get_repo_diff(root)
        if diff_data:
            state_data['dirty_repos'][rel_path] = diff_data
            print(f"  Captured changes for: {rel_path}")

    # Workspace-level colcon configuration. Both files live outside every repo,
    # so nothing else in the export would carry them: colcon_defaults.yaml is
    # read automatically from the directory colcon runs in, and .colcon/config.yaml
    # when it is pointed at via COLCON_HOME/COLCON_DEFAULTS_FILE.
    colcon_files = {}   # arcname -> absolute source path
    for rel in COLCON_CONFIG_FILES:
        full = os.path.join(workspace_path, rel)
        if os.path.isfile(full):
            colcon_files[rel] = full

    # Create zip file
    zip_file = os.path.join(output_dir, f"{ws_name}_{date_str}{zip_suffix}")
    with zipfile.ZipFile(zip_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add repos file
        zf.writestr('workspace.repos', repos_content)
        # Base64 encode diffs to ensure they survive YAML serialization
        for repo_data in state_data.get('dirty_repos', {}).values():
            if repo_data.get('staged_diff'):
                repo_data['staged_diff'] = base64.b64encode(repo_data['staged_diff'].encode('utf-8')).decode('ascii')
                repo_data['staged_diff_encoded'] = True
            if repo_data.get('unstaged_diff'):
                repo_data['unstaged_diff'] = base64.b64encode(repo_data['unstaged_diff'].encode('utf-8')).decode('ascii')
                repo_data['unstaged_diff_encoded'] = True

        state_content = yaml.dump(state_data, default_flow_style=False, allow_unicode=True)
        zf.writestr('workspace.state.yaml', state_content)

        # Git bundles for repos not restorable from a remote
        for rel, bundle_file in bundle_paths.items():
            zf.write(bundle_file, bundles[rel]['file'])

        # Additional entries (pipeline definition, tmuxinator configs, raw extras)
        for arcname, source in (extra_files or {}).items():
            if isinstance(source, bytes):
                zf.writestr(arcname, source)
            else:
                zf.write(source, arcname)

        # Workspace colcon configuration
        for arcname, full in colcon_files.items():
            zf.write(full, arcname)
            print(f"  Included {arcname}")

    shutil.rmtree(bundle_tmp, ignore_errors=True)

    dirty_count = len(state_data['dirty_repos'])
    print(f"\nExported to: {zip_file}")
    print(f"  Repositories: {repos_content.count('type:')}")
    print(f"  With uncommitted changes: {dirty_count}")
    if bundles:
        print(f"  Bundled (not on any remote): {len(bundles)}")
    print(f"  Colcon config: {', '.join(colcon_files) if colcon_files else 'not found'}")

    return zip_file


def export_pipeline_state(pipeline_file, workspace_path=None, output_dir=None):
    """
    Export one pipeline's slice of a workspace to a .pipeline.zip.

    The zip contains everything a plain workspace export has (workspace.repos
    manifest + workspace.state.yaml with uncommitted changes), restricted to the
    repos the pipeline lists, PLUS:
      pipeline/<basename> - the pipeline definition itself, under its own
                            filename (import installs it to the canonical
                            <output_dir>/pipeline/ directory)
      tmuxinator/<name>   - the tmuxinator session configs, verbatim
      extra/<rel_path>    - raw copies of non-repo paths (workspace-relative)

    Args:
        pipeline_file: Path to the *.pipeline.yaml definition
        workspace_path: Workspace override; falls back to the pipeline's
            'workspace' key, then to the current directory.
        output_dir: Directory for the zip (default: current directory)

    Returns:
        Path to created zip file
    """
    pipeline = load_pipeline(pipeline_file)
    workspace = workspace_path or pipeline.get('workspace') or os.getcwd()
    if not os.path.isdir(workspace):
        raise FileNotFoundError(f"Workspace not found: {workspace}")

    print(f"Pipeline: {pipeline['name']}  (workspace: {workspace})")
    if pipeline['repos']:
        print(f"  Repos: {', '.join(pipeline['repos'])}")

    # arcname keeps the real filename (flipper_eval.pipeline.yaml, not a
    # generic 'pipeline.yaml') so multiple pipelines coexist under the
    # canonical pipeline/ directory on import instead of overwriting each other
    extra_files = {f'pipeline/{os.path.basename(pipeline_file)}':
                   open(pipeline_file, 'rb').read()}

    for tmux_path in pipeline['tmuxinator']:
        if os.path.isfile(tmux_path):
            extra_files[f"tmuxinator/{os.path.basename(tmux_path)}"] = tmux_path
        else:
            print(f"Warning: tmuxinator config not found, skipping: {tmux_path}")

    for rel in pipeline['extra_paths']:
        full = os.path.join(workspace, rel)
        if os.path.isfile(full):
            extra_files[f"extra/{rel}"] = full
        elif os.path.isdir(full):
            for root, dirs, files in os.walk(full):
                dirs[:] = [d for d in dirs if d != '.git']
                for fn in files:
                    fp = os.path.join(root, fn)
                    extra_files[f"extra/{os.path.relpath(fp, workspace)}"] = fp
        else:
            print(f"Warning: extra path not found, skipping: {full}")

    # Warn about include entries that select no repo (typo guard)
    source_folder = os.path.join(workspace, "src")
    if not os.path.exists(source_folder):
        source_folder = workspace
    found = [os.path.relpath(p, source_folder) for p in find_git_repos(source_folder)]
    for entry in pipeline['repos']:
        if not any(repo_in_include_paths(rel, [entry]) for rel in found):
            print(f"Warning: pipeline repo entry matches nothing in {source_folder}: {entry}")

    return export_workspace_state(
        workspace,
        output_dir=output_dir,
        workspace_name=pipeline['name'],
        include_paths=pipeline['repos'] or None,
        extra_files=extra_files,
        zip_suffix='.pipeline.zip',
    )


# Absolute paths worth reasoning about. Deliberately not every possible root:
# these are the ones that carry a machine/user identity and therefore break when
# a workspace moves. Trailing punctuation is stripped by _clean_path_match.
_ABS_PATH_RE = re.compile(
    r'(?<![\w.-])(/(?:home|Users|root|mnt|media|srv|data|opt)(?:/[^\s\'"`,;:()\[\]{}<>|*?]+)*)'
)

# YAML keys that name the workspace root structurally rather than as part of a
# longer command string (tmuxinator's `root:`, rvcs's own `workspace:`).
_ROOT_KEY_RE = re.compile(r'^(\s*(?:root|workspace):\s*)(["\']?)([^"\'#\n]+?)(\2\s*(?:#.*)?)$',
                          re.MULTILINE)


def _clean_path_match(p):
    """Strip trailing punctuation a regex inevitably swallows off a path."""
    return p.rstrip('.,;:\'"`)]}')


def _tilde_forms(path):
    """
    Both spellings of an absolute home path: '/home/bob/ws' and '~/ws'.

    A config written on another machine may use either, and only the absolute
    form carries the foreign username, so both have to be rewritten.
    """
    forms = {path}
    m = re.match(r'^(/(?:home|Users)/[^/]+|/root)(/.*)?$', path)
    if m:
        forms.add('~' + (m.group(2) or ''))
    return forms


def infer_export_root(texts, declared_root=None):
    """
    Best-effort recovery of the export-time workspace root for zips written
    before workspace_root was recorded (rvcs <= 1.1.0).

    Looks for absolute paths under a home directory that is not this user's,
    and keeps the prefix ending at the workspace directory component — taken
    from declared_root's basename when available, e.g. a config full of
    /home/cnuc/marv_ws/... with `root: ~/marv_ws` yields /home/cnuc/marv_ws.

    Returns the inferred root, or None when there is nothing to go on.
    """
    home = os.path.expanduser('~')
    basename = os.path.basename((declared_root or '').rstrip('/')) or None
    counts = {}
    for text in texts:
        for raw in _ABS_PATH_RE.findall(text):
            path = _clean_path_match(raw)
            if path == home or path.startswith(home + os.sep):
                continue
            if not re.match(r'^/(?:home|Users)/[^/]+/', path):
                continue
            parts = path.split('/')
            if basename:
                if basename not in parts:
                    continue
                cut = parts.index(basename) + 1
            else:
                cut = 4  # /home/<user>/<dir>
                if len(parts) < cut:
                    continue
            candidate = '/'.join(parts[:cut])
            counts[candidate] = counts.get(candidate, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def rewrite_workspace_paths(text, old_root, new_root):
    """
    Point a restored pipeline payload at the workspace it was actually imported
    into: substitute old_root (in both its absolute and ~ spellings) with
    new_root, then fix up any `root:`/`workspace:` key the substitution missed
    (the config may declare a root that never appears in the commands).

    Only ever applied to files rvcs itself authored into the zip — tmuxinator
    configs and pipeline.yaml. Never to repository working trees: those are
    tracked git content, and rewriting them would surface as phantom diffs.

    Returns (new_text, substitution_count).
    """
    count = 0
    for form in sorted(_tilde_forms(old_root), key=len, reverse=True):
        if form and form != new_root and form in text:
            count += text.count(form)
            text = text.replace(form, new_root)

    def _fix_key(m):
        nonlocal count
        value = m.group(3).strip()
        if os.path.expanduser(value).rstrip('/') == new_root.rstrip('/'):
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{new_root}{m.group(4)}"

    return _ROOT_KEY_RE.sub(_fix_key, text), count


def check_restored_paths(workspace_path, max_examples=3):
    """
    Post-import doctor: find absolute paths in the restored workspace that do
    not exist on this machine.

    Catches what rewriting deliberately cannot — paths hardcoded inside the
    repositories themselves (launch files, shell scripts). Those belong to the
    repos and must be fixed upstream, so this only reports them.

    Returns {missing_prefix: {'count': int, 'files': [rel_path, ...]}}.
    """
    # .github holds CI templates full of paths for other machines/distros — pure
    # noise here, and never what a broken session is tripping over.
    skip_dirs = {'.git', '.github', 'build', 'install', 'log', '__pycache__',
                 '.venv', 'node_modules'}
    text_ext = {'.py', '.sh', '.bash', '.yaml', '.yml', '.launch', '.xml', '.json',
                '.cfg', '.ini', '.txt', '.md', '.rviz', '.repos', ''}
    home = os.path.expanduser('~')
    findings = {}

    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in text_ext:
                continue
            full = os.path.join(root, fn)
            try:
                if os.path.getsize(full) > 2 * 1024 * 1024:
                    continue
                with open(full, 'r', encoding='utf-8', errors='strict') as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            rel = os.path.relpath(full, workspace_path)
            for raw in set(_ABS_PATH_RE.findall(content)):
                path = _clean_path_match(raw)
                if os.path.exists(path):
                    continue
                # group by the identity-carrying prefix: /home/<user>, /opt/<x>
                parts = path.split('/')
                prefix = '/'.join(parts[:3]) if len(parts) > 3 else path
                if prefix == home or path.startswith(home + os.sep):
                    continue
                entry = findings.setdefault(prefix, {'count': 0, 'files': []})
                entry['count'] += 1
                if rel not in entry['files']:
                    entry['files'].append(rel)
    return findings


def report_restored_paths(findings, max_examples=3):
    """Print the doctor's findings as an actionable block."""
    if not findings:
        return
    total = sum(v['count'] for v in findings.values())
    print(f"\nWarning: {total} reference(s) to paths that do not exist on this machine:")
    for prefix, info in sorted(findings.items(), key=lambda kv: -kv[1]['count']):
        print(f"  {prefix}* — {info['count']} reference(s) in {len(info['files'])} file(s)")
        for rel in info['files'][:max_examples]:
            print(f"      {rel}")
        if len(info['files']) > max_examples:
            print(f"      ... and {len(info['files']) - max_examples} more")
    print("  These live inside the restored repositories; rvcs does not rewrite")
    print("  tracked files. Fix them upstream or adjust them by hand.")


_ROSDEP_UNRESOLVED_RE = re.compile(r'^ERROR\[(.+?)\]:\s*Cannot locate rosdep definition for \[(.+?)\]',
                                   re.MULTILINE)
# "apt\tros-jazzy-grid-map-core" under "System dependencies have not been satisfied:"
_ROSDEP_MISSING_RE = re.compile(r'^(apt|pip|pip3|gem|source|npm)\s+(\S+)\s*$', re.MULTILINE)


def detect_rosdistro():
    """ROS distro for rosdep: the sourced one, else the newest under /opt/ros."""
    distro = os.environ.get('ROS_DISTRO')
    if distro:
        return distro
    try:
        candidates = sorted(d for d in os.listdir('/opt/ros')
                            if os.path.isdir(os.path.join('/opt/ros', d)))
    except OSError:
        return None
    return candidates[-1] if candidates else None


def _rosdep_source_folder(workspace_path):
    src = os.path.join(workspace_path, 'src')
    return src if os.path.isdir(src) else workspace_path


def check_system_deps(workspace_path, rosdistro=None):
    """
    Ask rosdep what the restored workspace still needs from the system.

    Read-only and sudo-free -- the counterpart to check_restored_paths: report
    what stands between the import and a successful build, without changing the
    machine. rosdep is a standalone tool, so no ROS overlay need be sourced;
    --rosdistro carries what a sourced environment otherwise would.

    Returns {'ok': bool, 'reason': str|None, 'missing': [(installer, pkg)],
             'unresolved': {package: [rosdep_key]}, 'rosdistro': str|None}
    """
    result = {'ok': False, 'reason': None, 'missing': [], 'unresolved': {},
              'rosdistro': rosdistro}
    if shutil.which('rosdep') is None:
        result['reason'] = 'rosdep is not installed'
        return result

    rosdistro = rosdistro or detect_rosdistro()
    result['rosdistro'] = rosdistro
    cmd = ['rosdep', 'check', '--from-paths', _rosdep_source_folder(workspace_path),
           '--ignore-src']
    if rosdistro:
        cmd += ['--rosdistro', rosdistro]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        result['reason'] = f'could not run rosdep: {e}'
        return result

    output = (proc.stdout or '') + (proc.stderr or '')
    # rosdep is unusable until `rosdep update` has populated the local cache;
    # its own error text is cryptic, so say what to do instead of parsing on.
    if 'rosdep update' in output or 'no sources list' in output.lower():
        result['reason'] = ('rosdep has no local cache -- run `rosdep update` '
                            '(and `sudo rosdep init` if never initialised)')
        return result

    result['ok'] = True
    for pkg, key in _ROSDEP_UNRESOLVED_RE.findall(output):
        result['unresolved'].setdefault(pkg, []).append(key)
    # Only the block after the "not been satisfied" banner lists real installs;
    # anything before it is diagnostic noise.
    _, _, tail = output.partition('System dependencies have not been satisfied:')
    for installer, pkg in _ROSDEP_MISSING_RE.findall(tail):
        if (installer, pkg) not in result['missing']:
            result['missing'].append((installer, pkg))
    return result


def report_system_deps(deps):
    """Print what rosdep found, and the command that would fix the fixable half."""
    if not deps:
        return
    if not deps.get('ok'):
        if deps.get('reason'):
            print(f"\nNote: system dependencies not checked -- {deps['reason']}")
        return

    missing, unresolved = deps.get('missing') or [], deps.get('unresolved') or {}
    if not missing and not unresolved:
        print("\nSystem dependencies: all satisfied")
        return

    if missing:
        by_installer = {}
        for installer, pkg in missing:
            by_installer.setdefault(installer, []).append(pkg)
        print(f"\nWarning: {len(missing)} system dependency(ies) not installed:")
        for installer, pkgs in sorted(by_installer.items()):
            print(f"  [{installer}] {' '.join(sorted(pkgs))}")
        print("  Install with: rvcs --import-state ... --install-deps")
        print("  or: rosdep install --from-paths src --ignore-src -r -y")

    if unresolved:
        # These survive `rosdep install -r`, which prints them and continues --
        # easy to miss, and usually a bad key in package.xml or a repo the
        # pipeline definition forgot.
        total = sum(len(v) for v in unresolved.values())
        print(f"\nWarning: {total} rosdep key(s) could not be resolved:")
        for pkg, keys in sorted(unresolved.items()):
            print(f"  {pkg}: {', '.join(sorted(keys))}")
        print("  These are unknown to rosdep -- typically a wrong key in the")
        print("  package's package.xml, or a repo missing from the pipeline.")


def install_system_deps(workspace_path, rosdistro=None):
    """
    Run `rosdep install` for the workspace. Requires sudo: rosdep shells out to
    `sudo -H apt-get`, so this is opt-in only and never part of a plain import.

    Output is deliberately not captured, so the sudo password prompt reaches the
    terminal. Returns True if rosdep exited cleanly.
    """
    if shutil.which('rosdep') is None:
        print("Cannot install dependencies: rosdep is not installed")
        return False
    rosdistro = rosdistro or detect_rosdistro()
    cmd = ['rosdep', 'install', '--from-paths', _rosdep_source_folder(workspace_path),
           '--ignore-src', '-r', '-y']
    if rosdistro:
        cmd += ['--rosdistro', rosdistro]
    print(f"\nInstalling system dependencies (sudo may prompt):\n  {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  rosdep install failed: {e}")
        return False
    if proc.returncode != 0:
        print(f"  rosdep install exited with code {proc.returncode}")
        return False
    return True


def _cuda_bin_dir():
    """
    Directory holding nvcc, when it exists but is not on PATH.

    CUDA toolkits install under /usr/local/cuda*/bin, which the distro does not
    add to PATH. A package with a CUDA target then dies at configure time with
    "No CMAKE_CUDA_COMPILER could be found" even though the compiler is present.
    Prefers the /usr/local/cuda symlink (points at the chosen toolkit), then the
    highest-numbered versioned directory.
    """
    if shutil.which('nvcc'):
        return None
    canonical = '/usr/local/cuda/bin/nvcc'
    if os.path.isfile(canonical):
        return os.path.dirname(canonical)
    for path in sorted(glob.glob('/usr/local/cuda-*/bin/nvcc'), reverse=True):
        return os.path.dirname(path)
    return None


def build_environment(env_overrides=None):
    """
    Environment for the build subprocess.

    Returns (env, notes); notes describe every adjustment so nothing happens
    invisibly. env_overrides is a list of 'KEY=VALUE' strings, applied last so
    the caller always wins over the automatic fixes.
    """
    env = os.environ.copy()
    notes = []

    cuda_bin = _cuda_bin_dir()
    if cuda_bin:
        env['PATH'] = cuda_bin + os.pathsep + env.get('PATH', '')
        notes.append(f'PATH += {cuda_bin} (nvcc found but not on PATH)')

    for item in env_overrides or []:
        key, sep, value = item.partition('=')
        if not sep or not key:
            raise ValueError(f"--build-env expects KEY=VALUE, got {item!r}")
        env[key] = value
        notes.append(f'{key}={value}')
    return env, notes


def build_workspace(workspace_path, rosdistro=None, build_args=None, env_overrides=None):
    """
    Build the restored workspace with colcon.

    colcon resolves ament packages out of the environment, and rvcs runs from
    its own venv with no ROS overlay sourced -- so the build goes through a
    shell that sources /opt/ros/<distro>/setup.bash first. Output is left
    uncaptured: a build is long, and its progress is the point.

    Returns colcon's exit code, or None if it could not be started.
    """
    if shutil.which('colcon') is None:
        print("Cannot build: colcon is not installed")
        return None
    rosdistro = rosdistro or detect_rosdistro()
    setup = f'/opt/ros/{rosdistro}/setup.bash' if rosdistro else None
    if not setup or not os.path.isfile(setup):
        print(f"Cannot build: no ROS setup.bash for distro {rosdistro!r}")
        return None

    argv = ['colcon', 'build', '--symlink-install']
    if build_args:
        argv += build_args if isinstance(build_args, list) else shlex.split(build_args)
    try:
        env, notes = build_environment(env_overrides)
    except ValueError as e:
        print(f"Cannot build: {e}")
        return None
    command = f'. {shlex.quote(setup)} && exec {shlex.join(argv)}'
    print(f"\nBuilding workspace in {workspace_path}:\n  {shlex.join(argv)}")
    for note in notes:
        print(f"  env: {note}")
    try:
        proc = subprocess.run(['bash', '-c', command], cwd=workspace_path, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  colcon build failed to start: {e}")
        return None
    if proc.returncode != 0:
        print(f"  colcon build exited with code {proc.returncode} "
              f"(see {os.path.join(workspace_path, 'log', 'latest_build')})")
    return proc.returncode


def import_workspace_state(input_file, output_dir, state_file=None, install_tmuxinator=False,
                           rewrite_paths=True, install_deps=False, build=False,
                           build_args=None, build_env=None):
    """
    Import workspace state using vcstool and optionally apply diffs.

    Pipeline zips (.pipeline.zip) additionally restore:
      tmuxinator/* -> <output_dir>/tmuxinator/ (and, with install_tmuxinator,
                      copied into ~/.config/tmuxinator/)
      extra/*      -> <output_dir>/ (workspace-relative non-repo paths)
      pipeline/*   -> VERSIONED into PIPELINE_CONFIG_DIR/<name>/ (its own
                      git repo, one commit per import, backdated to the
                      export's own timestamp); older zips carrying a bare
                      'pipeline.yaml' member version there too, under a
                      name recovered from the definition's own 'name:' key

    Args:
        input_file: Path to .workspace.zip/.pipeline.zip, .repos file, or directory
        output_dir: Directory where repositories will be cloned
        state_file: Optional path to .state.yaml file with diffs (ignored if zip provided)
        install_tmuxinator: Also copy bundled tmuxinator configs to ~/.config/tmuxinator
        rewrite_paths: Rewrite the export-time workspace root out of the pipeline
            payload (tmuxinator configs, pipeline.yaml) so it points at output_dir
        install_deps: Run `rosdep install` for the restored workspace afterwards.
            Requires sudo, so it is opt-in; the check itself always runs.
        build: Run `colcon build` once everything is restored and dependencies
            are in place (opt-in -- a build is slow and writes build/install/log)
        build_args: Extra arguments appended to the colcon build command
            (string or list), e.g. '--cmake-args -DBUILD_TESTING=OFF'
        build_env: List of 'KEY=VALUE' overrides for the build environment

    Returns:
        Dictionary with 'import_return_code', 'patched', 'patch_failed'
    """
    # Create output directory with src subfolder for ROS workspace structure
    src_dir = os.path.join(output_dir, 'src')
    os.makedirs(src_dir, exist_ok=True)

    repos_content = None
    state_data = None
    pipeline_members = []
    bundle_files = {}   # rel -> extracted .bundle path on disk
    bundle_tmp = None

    colcon_config_content = {}   # arcname -> bytes
    results_paths_rewritten = []   # (member, substitutions), merged into results below

    # Handle zip file input
    if input_file.endswith('.zip'):
        print(f"Extracting workspace from {input_file}...")
        with zipfile.ZipFile(input_file, 'r') as zf:
            repos_content = zf.read('workspace.repos').decode('utf-8')
            if 'workspace.state.yaml' in zf.namelist():
                state_content = zf.read('workspace.state.yaml').decode('utf-8')
                state_data = yaml.safe_load(state_content)
            for rel in COLCON_CONFIG_FILES:
                if rel in zf.namelist():
                    colcon_config_content[rel] = zf.read(rel)
            # Extract git bundles for repos that no remote can restore
            for rel, meta in ((state_data or {}).get('bundles') or {}).items():
                if meta.get('file') in zf.namelist():
                    if bundle_tmp is None:
                        bundle_tmp = tempfile.mkdtemp(prefix='rvcs_bundles_')
                    dest = os.path.join(bundle_tmp, rel.replace('/', '__') + '.bundle')
                    with open(dest, 'wb') as out:
                        out.write(zf.read(meta['file']))
                    bundle_files[rel] = dest
            pipeline_members = [n for n in zf.namelist()
                                if n == 'pipeline.yaml'
                                or n.startswith('pipeline/')
                                or n.startswith('tmuxinator/')
                                or n.startswith('extra/')]
            if pipeline_members:
                # Files rvcs authored itself — the only ones it may rewrite.
                # extra/* is verbatim user content and repos are tracked git
                # trees, so both are restored byte-for-byte regardless.
                rewritable = [n for n in pipeline_members
                              if n == 'pipeline.yaml' or n.startswith('pipeline/')
                              or n.startswith('tmuxinator/')]

                export_root = (state_data or {}).get('workspace_root')
                if rewrite_paths and not export_root and rewritable:
                    # Pre-workspace_root zip: recover the old root from the
                    # payload itself, anchored on whatever root it declares.
                    texts = []
                    declared = None
                    for n in rewritable:
                        try:
                            t = zf.read(n).decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                        texts.append(t)
                        m = _ROOT_KEY_RE.search(t)
                        if m and not declared:
                            declared = m.group(3).strip()
                    export_root = infer_export_root(texts, declared)
                    if export_root:
                        print(f"  Inferred export-time workspace root: {export_root}")

                target_root = os.path.abspath(output_dir)
                rewritten = {}   # member -> rewritten bytes
                if rewrite_paths and export_root:
                    for n in rewritable:
                        try:
                            text = zf.read(n).decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                        new_text, subs = rewrite_workspace_paths(text, export_root, target_root)
                        if subs:
                            rewritten[n] = new_text.encode('utf-8')
                            results_paths_rewritten.append((n, subs))
                            print(f"  Rewrote {subs} path(s) in {n}: "
                                  f"{export_root} -> {target_root}")

                # Restore pipeline payload. extra/<rel> entries land at their
                # workspace-relative path; tmuxinator configs land in
                # <output_dir>/tmuxinator/. Pipeline definitions VERSION into
                # their own canonical repo under PIPELINE_CONFIG_DIR — every
                # import of a pipeline zip is one commit in that pipeline's
                # history, backdated to when it was actually exported.
                export_date = (state_data or {}).get('export_date')
                pipe_files = []
                for member in pipeline_members:
                    if member.endswith('/'):
                        continue
                    content = rewritten.get(member) or zf.read(member)
                    if member.startswith('extra/'):
                        dest = os.path.join(output_dir, os.path.relpath(member, 'extra'))
                    elif member == 'pipeline.yaml':
                        # Pre-canonical-directory zip: a bare 'pipeline.yaml'
                        # member carries no real filename. Recover one from
                        # the pipeline's own required 'name:' key so it still
                        # versions under a meaningful name, not a generic one.
                        # The store gets the RAW exported bytes, not the
                        # path-rewritten copy: the canonical version is what
                        # the exporter authored, so re-importing the same zip
                        # (into any directory) is a no-op, not a phantom
                        # 'workspace: changed' version.
                        raw = zf.read(member)
                        pname = None
                        try:
                            doc = yaml.safe_load(raw.decode('utf-8'))
                            if isinstance(doc, dict):
                                pname = doc.get('name')
                        except Exception:
                            pass
                        if pname:
                            pipe_files.append((pname, raw))
                        continue
                    elif member.startswith('pipeline/'):
                        pname = os.path.basename(member)
                        for suf in ('.pipeline.yaml', '.pipeline.yml'):
                            if pname.endswith(suf):
                                pname = pname[:-len(suf)]
                                break
                        pipe_files.append((pname, zf.read(member)))
                        continue
                    else:
                        dest = os.path.join(output_dir, member)
                    os.makedirs(os.path.dirname(dest) or output_dir, exist_ok=True)
                    with open(dest, 'wb') as out:
                        out.write(content)
                for pname, content in pipe_files:
                    author_date = None
                    if export_date:
                        try:
                            author_date = datetime.strptime(
                                export_date, '%Y-%m-%d_%H-%M-%S').strftime('%Y-%m-%dT%H:%M:%S')
                        except ValueError:
                            pass
                    sha = commit_pipeline_snapshot(
                        pname, content,
                        f"import: {pname} pipeline snapshot"
                        + (f" ({export_date})" if export_date else "")
                        + f"\n\nFrom {os.path.basename(input_file)}.",
                        author_date=author_date)
                    print(f"  Pipeline '{pname}': "
                          f"{'committed ' + sha if sha else 'unchanged (identical to HEAD)'} "
                          f"in {_pipeline_repo_dir(pname)}/")
                    if sha:
                        # show what this snapshot changed vs the previous one
                        import subprocess
                        d = subprocess.run(
                            ['git', '-C', _pipeline_repo_dir(pname), 'show',
                             '--format=', '--unified=2', 'HEAD'],
                            capture_output=True, text=True).stdout.splitlines()
                        if d:
                            shown = d[:80]
                            print('    ' + '\n    '.join(shown))
                            if len(d) > len(shown):
                                print(f'    … {len(d) - len(shown)} more diff line(s) — '
                                      f'git -C {_pipeline_repo_dir(pname)} show')
                tmux_files = [n for n in pipeline_members if n.startswith('tmuxinator/')]
                if tmux_files:
                    print(f"  Restored tmuxinator configs to {os.path.join(output_dir, 'tmuxinator')}/")
                    if install_tmuxinator:
                        tmux_config_dir = os.path.expanduser('~/.config/tmuxinator')
                        os.makedirs(tmux_config_dir, exist_ok=True)
                        for n in tmux_files:
                            dest = os.path.join(tmux_config_dir, os.path.basename(n))
                            # install the rewritten copy, not the raw one, or
                            # ~/.config would get the broken paths back
                            with open(dest, 'wb') as out:
                                out.write(rewritten.get(n) or zf.read(n))
                            print(f"  Installed {dest}")
                    else:
                        print(f"  (use --install-tmuxinator to copy them into ~/.config/tmuxinator)")
    else:
        # Handle .repos file input
        print(f"Importing repositories from {input_file}...")
        with open(input_file, 'r') as f:
            repos_content = f.read()

        # Load state file if provided
        if state_file and os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state_data = yaml.safe_load(f)

    results = {
        'import_return_code': None,
        'patched': [],
        'patch_failed': [],
        'bundled': [],
        'bundle_failed': [],
        'colcon_config_restored': False,
        'paths_rewritten': results_paths_rewritten,
        'path_warnings': {}
    }

    # Bundled repos are restored manually (vcstool cannot checkout a hash that
    # exists on no remote), so strip them from the manifest handed to vcstool.
    bundle_meta = (state_data or {}).get('bundles') or {}
    bundled_entries = {}
    manifest_repos = {}
    if bundle_files:
        manifest = yaml.safe_load(repos_content) or {}
        manifest_repos = manifest.get('repositories') or {}
        for rel in bundle_files:
            if rel in manifest_repos:
                bundled_entries[rel] = manifest_repos.pop(rel)
        repos_content = yaml.dump({'repositories': manifest_repos}, default_flow_style=False)

    def _restore_bundled_repo(rel):
        meta = bundle_meta.get(rel, {})
        entry = bundled_entries.get(rel, {})
        url = entry.get('url')
        version = entry.get('version')
        dest = os.path.join(src_dir, rel)
        try:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if meta.get('self_contained'):
                repo = git.Repo.clone_from(bundle_files[rel], dest)
                # origin now points at the temporary bundle file — fix it up
                if url:
                    repo.git.remote('set-url', 'origin', url)
                else:
                    repo.git.remote('remove', 'origin')
            else:
                # incremental bundle: base history comes from the remote
                repo = git.Repo.clone_from(url, dest)
                repo.git.fetch(bundle_files[rel], 'HEAD')
            if version:
                repo.git.checkout(version)
            kind = 'bundle' if meta.get('self_contained') else 'remote + bundle'
            print(f"  Restored {rel} from {kind}")
            results['bundled'].append(rel)
        except Exception as e:
            print(f"  Failed to restore bundled {rel}: {e}")
            results['bundle_failed'].append(rel)

    # Bundled repos that CONTAIN a manifest repo must exist before vcstool
    # clones into them (parent-first); all other bundled repos come after, so
    # a bundled repo nested inside a manifest repo finds its parent in place.
    early = sorted(rel for rel in bundle_files
                   if any(r.startswith(rel + '/') for r in manifest_repos))
    late = sorted(set(bundle_files) - set(early))
    if early:
        print("\nRestoring bundled repos (parents of manifest repos)...")
        for rel in early:
            _restore_bundled_repo(rel)

    # Use vcstool import with temporary file (redirect stdout as vcstool ignores the stdout parameter)
    stdout_capture = StringIO()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.repos', delete=False) as tmp_file:
        tmp_file.write(repos_content)
        tmp_file_path = tmp_file.name

    try:
        with redirect_stdout(stdout_capture):
            # single worker: nested repos must be cloned parent-first (the
            # manifest is sorted so a parent path precedes its nested repos);
            # parallel workers could clone a child into a not-yet-cloned parent
            # path and make the parent clone fail on a non-empty directory
            rc = vcs_import(args=['--input', tmp_file_path, '--workers', '1', src_dir])
    finally:
        os.unlink(tmp_file_path)

    import_output = stdout_capture.getvalue()
    print(import_output)
    results['import_return_code'] = rc

    if late:
        print("Restoring bundled repos...")
        for rel in late:
            _restore_bundled_repo(rel)

    # Restore the workspace colcon configuration. Written before the build so
    # colcon_defaults.yaml is in place when colcon runs -- that is what makes
    # --build need no --build-args for a workspace that pins its own settings.
    for rel, content in colcon_config_content.items():
        dest = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(dest) or output_dir, exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(content)
        print(f"Restored {rel}")
        results['colcon_config_restored'] = True

    # Apply diffs if state data available
    if state_data and state_data.get('dirty_repos'):
        print(f"\nApplying uncommitted changes...")

        for rel_path, diff_data in state_data.get('dirty_repos', {}).items():
            repo_path = os.path.join(src_dir, rel_path)

            if not os.path.isdir(repo_path):
                print(f"  Skipping {rel_path}: directory not found")
                results['patch_failed'].append(rel_path)
                continue

            try:
                repo = git.Repo(repo_path)
                applied = False

                # Apply staged diff (use --index to stage the changes)
                if diff_data.get('staged_diff'):
                    staged_diff = diff_data['staged_diff']
                    if diff_data.get('staged_diff_encoded'):
                        staged_diff = base64.b64decode(staged_diff).decode('utf-8')
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as pf:
                        pf.write(staged_diff)
                        patch_file = pf.name
                    try:
                        repo.git.apply('--index', patch_file)
                        applied = True
                    finally:
                        os.unlink(patch_file)

                # Apply unstaged diff
                if diff_data.get('unstaged_diff'):
                    unstaged_diff = diff_data['unstaged_diff']
                    if diff_data.get('unstaged_diff_encoded'):
                        unstaged_diff = base64.b64decode(unstaged_diff).decode('utf-8')
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as pf:
                        pf.write(unstaged_diff)
                        patch_file = pf.name
                    try:
                        repo.git.apply(patch_file)
                        applied = True
                    finally:
                        os.unlink(patch_file)

                # Restore untracked files
                for filepath, file_data in diff_data.get('untracked_files', {}).items():
                    full_path = os.path.join(repo_path, filepath)
                    # Create parent directory if needed (handle root-level files)
                    parent_dir = os.path.dirname(full_path)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)

                    content = file_data['content']
                    if file_data.get('binary'):
                        content = base64.b64decode(content)
                        mode = 'wb'
                    else:
                        mode = 'w'

                    with open(full_path, mode) as f:
                        f.write(content)
                    applied = True

                if applied:
                    print(f"  Applied changes to: {rel_path}")
                    results['patched'].append(rel_path)

            except Exception as e:
                print(f"  Failed to apply changes to {rel_path}: {e}")
                results['patch_failed'].append(rel_path)

    if bundle_tmp:
        shutil.rmtree(bundle_tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"Import summary:")
    print(f"  vcstool import return code: {rc}")
    if bundle_files:
        print(f"  Bundled repos restored: {len(results['bundled'])}")
        if results['bundle_failed']:
            print(f"  Bundled repos FAILED: {len(results['bundle_failed'])} ({', '.join(results['bundle_failed'])})")
    if state_data:
        print(f"  Patches applied: {len(results['patched'])}")
        print(f"  Patches failed: {len(results['patch_failed'])}")
    if results['colcon_config_restored']:
        print(f"  Colcon config: restored")
    if results['paths_rewritten']:
        total = sum(n for _, n in results['paths_rewritten'])
        print(f"  Paths rewritten: {total} in {len(results['paths_rewritten'])} file(s)")

    # Doctor: what rewriting could not reach (hardcoded paths inside the repos)
    results['path_warnings'] = check_restored_paths(output_dir)
    report_restored_paths(results['path_warnings'])

    # Doctor: what the workspace still needs from the system before it builds
    deps = check_system_deps(output_dir)
    if install_deps and deps.get('missing'):
        if install_system_deps(output_dir, deps.get('rosdistro')):
            deps = check_system_deps(output_dir)   # re-check so the report is post-install
    report_system_deps(deps)
    results['system_deps'] = deps

    # Build last: everything the build needs is now in place, and unmet system
    # deps are already on screen so a failure here is attributable.
    if build:
        if deps.get('ok') and deps.get('missing'):
            print("\nNote: building with unmet system dependencies "
                  "(see the warning above) -- expect failures")
        results['build_return_code'] = build_workspace(
            output_dir, deps.get('rosdistro'), build_args, build_env)

    return results


# ---------------------------------------------------------------------------
# State diff: compare an exported .workspace.zip/.pipeline.zip against a live
# workspace, browsable as a tree in a curses TUI (rvcs --diff-state ZIP [ws]).
# ---------------------------------------------------------------------------

def make_upstream_state_zip(workspace, include_paths=None):
    """Synthesize a minimal snapshot zip whose recorded state is each repo's
    UPSTREAM (origin/<branch>; HEAD when no upstream is configured) with no
    dirty payload. Diffing the live workspace against it shows exactly the
    LOCAL delta: unpushed commits as '^ local ahead', uncommitted files as
    local-only changes, and (after --fetch) new remote commits as takeable.
    This is what --diff-state does when invoked WITHOUT a zip. Returns the
    zip path inside its own temp dir (caller removes the dir when done)."""
    source = os.path.join(workspace, 'src')
    if not os.path.isdir(source):
        source = workspace
    repos = {}
    for path in find_git_repos(source):
        rel = os.path.relpath(path, source)
        if not repo_in_include_paths(rel, include_paths):
            continue
        try:
            r = git.Repo(path)
            url = list(r.remotes.origin.urls)[0] if 'origin' in r.remotes else ''
            up = _repo_upstream(path)
            ver = r.commit(up[1]).hexsha if up else r.head.commit.hexsha
        except Exception:
            continue
        repos[rel] = {'type': 'git', 'url': url, 'version': ver}
    tmp = tempfile.mkdtemp(prefix='rvcs_upstreamdiff_')
    zpath = os.path.join(tmp, 'upstream (origin refs)')
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('workspace.repos', yaml.safe_dump({'repositories': repos}))
        zf.writestr('workspace.state.yaml', yaml.safe_dump({
            'workspace_name': 'upstream state (synthesized -- no zip given)',
            'export_date': datetime.now().strftime('%Y-%m-%d_%H-%M-%S'),
            'workspace_root': os.path.abspath(workspace),
            'dirty_repos': {}}))
    return zpath


def load_state_zip(zip_path):
    """Parse an export zip into {name, date, repos, dirty, bundles, colcon,
    tmuxinator, extra}. Diff payloads are base64-decoded to text."""
    model = {'repos': {}, 'dirty': {}, 'bundles': {}, 'colcon': None,
             'tmuxinator': {}, 'extra': {}, 'name': None, 'date': None}
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        manifest = yaml.safe_load(zf.read('workspace.repos').decode()) or {}
        model['repos'] = manifest.get('repositories') or {}
        if 'workspace.state.yaml' in names:
            state = yaml.safe_load(zf.read('workspace.state.yaml').decode()) or {}
            model['name'] = state.get('workspace_name')
            model['date'] = state.get('export_date')
            model['bundles'] = state.get('bundles') or {}
            for rel, d in (state.get('dirty_repos') or {}).items():
                entry = {'staged': d.get('staged_diff') or '',
                         'unstaged': d.get('unstaged_diff') or '',
                         'untracked': d.get('untracked_files') or {}}
                if d.get('staged_diff_encoded'):
                    entry['staged'] = base64.b64decode(entry['staged']).decode('utf-8', 'replace')
                if d.get('unstaged_diff_encoded'):
                    entry['unstaged'] = base64.b64decode(entry['unstaged']).decode('utf-8', 'replace')
                model['dirty'][rel] = entry
        for member in ('.colcon/config.yaml', 'colcon_defaults.yaml'):
            if member in names:
                model['colcon'] = (member, zf.read(member).decode('utf-8', 'replace'))
                break
        for n in names:
            if n.startswith('tmuxinator/') and not n.endswith('/'):
                model['tmuxinator'][os.path.basename(n)] = zf.read(n).decode('utf-8', 'replace')
            elif n.startswith('extra/') and not n.endswith('/'):
                model['extra'][os.path.relpath(n, 'extra')] = zf.read(n)
    return model


def _split_patch_by_file(patch_text):
    """Split a unified diff into {path: per-file patch text} by diff --git headers."""
    files = {}
    current, buf = None, []
    for line in (patch_text or '').splitlines():
        m = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
        if m:
            if current:
                files[current] = '\n'.join(buf)
            current = m.group(2) if m.group(2) != '/dev/null' else m.group(1)
            buf = [line]
        elif current:
            buf.append(line)
    if current:
        files[current] = '\n'.join(buf)
    return files


def _node(label, status='info', detail=None, children=None):
    return {'label': label, 'status': status, 'detail': detail or [],
            'children': children or [], 'expanded': False}


def _untracked_text(entry):
    """Decode an untracked_files entry to (is_binary, text_or_none, size)."""
    content = entry.get('content', '')
    if entry.get('binary'):
        try:
            raw = base64.b64decode(content)
        except Exception:
            raw = b''
        return True, None, len(raw)
    return False, content, len(content)


_ZIP_STATE_CACHE = {}  # (repo, file, zip_sha, patch-hash, disk-signature) -> state


def _diff_repo_files(zdirty, ldirty, repo_path=None, zip_sha=None):
    """Compare per-file uncommitted changes of one repo between zip and local.
    Returns (child nodes, counts dict). Nodes carry an 'act' payload so the
    TUI can merge/accept them (repo_path, file, zip patch/content, side).
    Content-aware: a zip-side change that is ALREADY in the local file
    (e.g. committed here since the export) counts as in sync, not as a diff."""
    zfiles = _split_patch_by_file(zdirty.get('staged', ''))
    zfiles.update(_split_patch_by_file(zdirty.get('unstaged', '')))
    lfiles = _split_patch_by_file(ldirty.get('staged', ''))
    lfiles.update(_split_patch_by_file(ldirty.get('unstaged', '')))
    zun, lun = zdirty.get('untracked', {}), ldirty.get('untracked', {})

    def zip_change_state(path, zp):
        """How the zip's patch for path relates to the local file:
        'contained' (already in it), 'clean' (3-way merge applies without
        conflicts and brings changes), 'conflict' (3-way merge conflicts),
        or None when undecidable (zip base commit unknown / patch broken).
        Memoized on (zip base, patch, local file signature): the result only
        depends on those, and the check costs 3 subprocesses — without the
        cache every TUI reload re-pays it for every zip-modified file."""
        if not (repo_path and zip_sha):
            return None
        lp = os.path.join(repo_path, path)
        try:
            st = os.stat(lp)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        key = (repo_path, path, zip_sha, hash(zp), sig)
        if key in _ZIP_STATE_CACHE:
            return _ZIP_STATE_CACHE[key]
        base = _git_show_head(repo_path, path, rev=zip_sha)
        theirs = _apply_patch_to_text(base, zp, path)
        if theirs is None:
            _ZIP_STATE_CACHE[key] = None
            return None
        try:
            ours = open(lp, encoding='utf-8', errors='replace').read()
        except OSError:
            return None
        if ours == theirs:
            state = 'contained'
        else:
            merged, n = _merge_texts(ours, base, theirs)
            state = 'conflict' if n else (
                'contained' if merged == ours else 'clean')
        _ZIP_STATE_CACHE[key] = state
        return state

    nodes, counts = [], {'+': 0, '-': 0, '~': 0, '=': 0}
    for path in sorted(set(zfiles) | set(lfiles)):
        zp, lp = zfiles.get(path), lfiles.get(path)
        act = {'kind': 'patch', 'repo': repo_path, 'file': path,
               'zip_patch': zp, 'local_patch': lp, 'zip_sha': zip_sha}
        # A local file still carrying conflict markers (from an earlier merge
        # of THIS or any tool) is flagged in every session, not just the one
        # that wrote them.
        if lp and re.search(r'^\+<{7}[ \n]', lp, re.M):
            counts['~'] += 1
            nodes.append(_node(f"{path}  (UNRESOLVED conflict markers in file)", 'conflict',
                               ['This file contains <<<<<<< / ======= / >>>>>>> markers.',
                                'Edit it, keep the wanted lines, delete the markers.',
                                ''] + (lp or '').splitlines()))
            nodes[-1]['act'] = act
            continue
        if zp is not None and lp is None:
            state = zip_change_state(path, zp)
            if state == 'contained':
                counts['='] += 1
                nodes.append(_node(f"{path}  ({_SIDE} change already in local)", 'same',
                                   [f"The {_SIDE}'s uncommitted patch is already contained in the",
                                    'local file (committed or applied here since the export).',
                                    ''] + zp.splitlines()))
            elif state == 'conflict':
                counts['~'] += 1
                nodes.append(_node(f"{path}  ({_SIDE} change CONFLICTS with local version)",
                                   'clash',
                                   [f"The {_SIDE}'s patch collides with changes made here since the",
                                    'export — u skips this file. d previews the merge with',
                                    'markers; then m (merge), o (keep local) or t (zip version).',
                                    '', '── patch in zip ──'] + zp.splitlines()))
            else:
                counts['+'] += 1
                nodes.append(_node(f"{path}  (modified only in {_SIDE})", 'added',
                                   [f'── patch in {_SIDE} ──'] + zp.splitlines()))
        elif lp is not None and zp is None:
            counts['-'] += 1
            nodes.append(_node(f"{path}  (modified only locally)", 'removed',
                               ['── patch in local workspace ──'] + lp.splitlines()))
        elif zp != lp:
            state = zip_change_state(path, zp)
            if state == 'contained':
                counts['='] += 1
                nodes.append(_node(f"{path}  ({_SIDE} change contained; local has own edits)",
                                   'same',
                                   [f"The {_SIDE}'s patch is already contained in the local file;",
                                    'the remaining difference is local-only work.',
                                    '', '── patch in zip ──'] + zp.splitlines() +
                                   ['', '── patch in local workspace ──'] + lp.splitlines()))
            else:
                clash = state == 'conflict'
                nodes.append(_node(
                    f"{path}  (patches differ{' — merge would conflict' if clash else ''})",
                    'clash' if clash else 'changed',
                    ['── patch in zip ──'] + zp.splitlines() +
                    ['', '── patch in local workspace ──'] + lp.splitlines()))
                counts['~'] += 1
        else:
            counts['='] += 1
            nodes.append(_node(f"{path}  (same patch)", 'same',
                               ['Identical uncommitted patch on both sides.', ''] + zp.splitlines()))
        nodes[-1]['act'] = act
    for path in sorted(set(zun) | set(lun)):
        ze, le = zun.get(path), lun.get(path)
        label_path = f"{path} (untracked)"
        uact = {'kind': 'untracked', 'repo': repo_path, 'file': path,
                'zip_entry': ze, 'local_entry': le}
        if ze is not None and le is None:
            zb, ztext, zsize = _untracked_text(ze)
            disk = os.path.join(repo_path, path) if repo_path else None
            if disk and os.path.exists(disk):
                # untracked in the zip but existing here (typically committed
                # since the export) — compare actual content
                zbytes = base64.b64decode(ze.get('content', '')) if zb \
                    else (ztext or '').encode()
                try:
                    dbytes = open(disk, 'rb').read()
                except OSError:
                    dbytes = None
                if dbytes == zbytes:
                    counts['='] += 1
                    nodes.append(_node(f"{label_path}  (already in local, tracked)", 'same',
                                       ['Untracked in the zip but present here with identical',
                                        'content (committed since the export).']))
                else:
                    import difflib
                    counts['~'] += 1
                    if zb or dbytes is None:
                        detail = ['<binary or unreadable: zip %d bytes, local %s bytes>'
                                  % (zsize, len(dbytes) if dbytes is not None else '?')]
                    else:
                        detail = list(difflib.unified_diff(
                            dbytes.decode('utf-8', 'replace').splitlines(),
                            (ztext or '').splitlines(),
                            'local/' + path, 'zip/' + path, lineterm=''))
                    nodes.append(_node(f"{label_path}  (tracked locally, content differs)",
                                       'clash', detail))
            else:
                counts['+'] += 1
                detail = ['── new file, only in zip ──'] + \
                         (['<binary, %d bytes>' % zsize] if zb else (ztext or '').splitlines())
                nodes.append(_node(f"{label_path}  (only in {_SIDE})", 'added', detail))
        elif le is not None and ze is None:
            lb, ltext, lsize = _untracked_text(le)
            counts['-'] += 1
            detail = ['── new file, only in local workspace ──'] + \
                     (['<binary, %d bytes>' % lsize] if lb else (ltext or '').splitlines())
            nodes.append(_node(f"{label_path}  (only locally)", 'removed', detail))
        else:
            zb, ztext, zsize = _untracked_text(ze)
            lb, ltext, lsize = _untracked_text(le)
            if zb or lb:
                same = ze.get('content') == le.get('content')
                st = 'same' if same else 'changed'
                counts['=' if same else '~'] += 1
                nodes.append(_node(f"{label_path}  ({'same' if same else 'binary differs'})", st,
                                   ['<binary: zip %d bytes, local %d bytes>' % (zsize, lsize)]))
            elif ztext == ltext:
                counts['='] += 1
                nodes.append(_node(f"{label_path}  (same content)", 'same',
                                   (ztext or '').splitlines()))
            else:
                import difflib
                counts['~'] += 1
                d = list(difflib.unified_diff((ltext or '').splitlines(),
                                              (ztext or '').splitlines(),
                                              'local/' + path, 'zip/' + path, lineterm=''))
                nodes.append(_node(f"{label_path}  (content differs)", 'changed', d))
        nodes[-1]['act'] = uact
    return nodes, counts


_MERGE_DRYRUN_CACHE = {}  # (repo, head, zip_sha) -> result dict


def _merge_dry_run(repo_path, zip_sha):
    """Merge HEAD with zip_sha in memory (git merge-tree --write-tree writes
    only objects, never the branch/worktree) and report which files would
    conflict and which auto-merge. Returns None when unsupported (git < 2.38).
    Memoized per (repo, HEAD, zip commit) — reloads are free."""
    import subprocess
    try:
        head = git.Repo(repo_path).head.commit.hexsha
    except Exception:
        return None
    key = (repo_path, head, zip_sha)
    if key in _MERGE_DRYRUN_CACHE:
        return _MERGE_DRYRUN_CACHE[key]

    def out(*a):
        return subprocess.run(['git', '-C', repo_path] + list(a), capture_output=True)

    r = out('merge-tree', '--write-tree', '--name-only', 'HEAD', zip_sha)
    text = r.stdout.decode('utf-8', 'replace')
    if not text.strip() or 'usage:' in r.stderr.decode('utf-8', 'replace'):
        _MERGE_DRYRUN_CACHE[key] = None
        return None
    lines = text.splitlines()
    tree, conflicts = lines[0].strip(), []
    for line in lines[1:]:
        if not line.strip():
            break
        conflicts.append(line.strip())
    base = out('merge-base', 'HEAD', zip_sha).stdout.decode().strip()
    incoming = [f for f in out('diff', '--name-only', base or 'HEAD', zip_sha)
                .stdout.decode().splitlines() if f] if base else []
    hunks = {}
    for f in conflicts[:20]:
        blob = out('show', f'{tree}:{f}').stdout.decode('utf-8', 'replace')
        hunks[f] = blob.count('\n<<<<<<<') + blob.startswith('<<<<<<<')
    res = {'conflicts': conflicts, 'hunks': hunks,
           'clean': [f for f in incoming if f not in set(conflicts)],
           'base': base}
    _MERGE_DRYRUN_CACHE[key] = res
    return res


def _commit_nodes(repo_path, zip_sha, cinfo):
    """Browsable children for a repo whose history differs from the zip:
    the two commit lists and — when diverged — what merging would do."""
    import subprocess

    def log(rng):
        r = subprocess.run(['git', '-C', repo_path, 'log', '--oneline', rng],
                           capture_output=True)
        return [l for l in r.stdout.decode('utf-8', 'replace').splitlines() if l]

    nodes = []
    # where the two histories part: the merge base, plus a graph of both
    # sides above it ('<' = local only, '>' = zip only, 'o' = the fork point)
    base = subprocess.run(['git', '-C', repo_path, 'merge-base', 'HEAD', zip_sha],
                          capture_output=True).stdout.decode().strip()
    if base and cinfo['behind'] and cinfo['ahead']:
        binfo = subprocess.run(
            ['git', '-C', repo_path, 'log', '-1', '--date=short',
             '--format=%h  %ad  %an  %s', base],
            capture_output=True).stdout.decode('utf-8', 'replace').strip()
        graph = subprocess.run(
            ['git', '-C', repo_path, 'log', '--graph', '--oneline',
             '--boundary', '--left-right', f'HEAD...{zip_sha}'],
            capture_output=True).stdout.decode('utf-8', 'replace').splitlines()
        nodes.append(_node(
            f"diverged at {binfo.split('  ')[0]} — {binfo.split('  ')[-1][:60]}",
            'info',
            ['The last commit both sides share (fork point):', '',
             '    ' + binfo, '',
             '── history since the fork ──',
             "   '<' only local      '>' only in zip      'o' the fork point", ''] +
            graph[:60]))
    if cinfo['behind']:
        incoming = log(f'HEAD..{zip_sha}')
        stat = subprocess.run(['git', '-C', repo_path, 'show', '--stat',
                               '--oneline', zip_sha], capture_output=True)
        nodes.append(_node(
            f"commits only in {_SIDE} ({len(incoming)})", 'added',
            incoming + ['', '── newest incoming commit ──'] +
            stat.stdout.decode('utf-8', 'replace').splitlines()[:40]))
    if cinfo['ahead']:
        mine = log(f'{zip_sha}..HEAD')
        nodes.append(_node(f"commits only local ({len(mine)})", 'ahead',
                           [f'Your work — the {_SIDE} has none of it; never touched.',
                            ''] + mine))
    if cinfo['behind'] and cinfo['ahead']:
        dry = _merge_dry_run(repo_path, zip_sha)
        if dry is not None:
            c, cl = dry['conflicts'], dry['clean']
            det = [f'Merging the {_SIDE} commits into your branch would:', '']
            if c:
                det.append('CONFLICT — needs hand-resolution (%d file(s)):' % len(c))
                det += ['    ! %s  (%d hunk(s))' % (f, dry['hunks'].get(f, 0))
                        for f in c[:20]]
                det.append('')
            if cl:
                det.append('auto-merge cleanly (%d file(s)):' % len(cl))
                det += ['    ✓ %s' % f for f in cl[:40]]
            det += ['', 't on the repo node runs this merge; on conflicts it aborts',
                    'and leaves the repo untouched for a hand merge:',
                    '', f'    git -C {repo_path} merge {zip_sha[:9]}']
            label = ('merge dry-run: %d conflicting, %d clean file(s)'
                     % (len(c), len(cl)))
            nodes.append(_node(label, 'clash' if c else 'added', det))
    return nodes


def _repo_commit_info(repo_path, zip_sha):
    """Describe how local HEAD relates to the zip's recorded commit.
    Returns (status, detail lines, {'known': bool, 'ahead': int, 'behind': int})."""
    lines, status = [], 'changed'
    info = {'known': False, 'ahead': 0, 'behind': 0}
    try:
        repo = git.Repo(repo_path)
        local_sha = repo.head.commit.hexsha
        if local_sha == zip_sha:
            info['known'] = True
            return 'same', [], info
        try:
            repo.commit(zip_sha)  # is the zip commit known locally?
        except Exception:
            lines.append(f"{_SIDE} HEAD %s is NOT in local history — commits made on the "
                         f"other machine (check bundles/ in the {_SIDE})." % zip_sha[:9])
            return status, lines, info
        info['known'] = True
        ahead = repo.git.rev_list('--count', f'{zip_sha}..HEAD')
        behind = repo.git.rev_list('--count', f'HEAD..{zip_sha}')
        info['ahead'], info['behind'] = int(ahead), int(behind)
        lines.append(f"local ahead by {ahead}, behind by {behind} (vs {_SIDE} {zip_sha[:9]})")
        if int(ahead):
            lines.append('')
            lines.append('── commits only in local workspace ──')
            lines += repo.git.log('--oneline', f'{zip_sha}..HEAD').splitlines()[:20]
        if int(behind):
            lines.append('')
            lines.append(f'── commits only in {_SIDE} ──')
            lines += repo.git.log('--oneline', f'HEAD..{zip_sha}').splitlines()[:20]
    except Exception as e:
        lines.append(f'(could not inspect local repo: {e})')
    return status, lines, info


def compute_state_diff(zip_path, workspace, include_paths=None):
    """Build the diff tree (zip state vs live workspace) as nested nodes."""
    zm = load_state_zip(zip_path)
    source_folder = os.path.join(workspace, 'src')
    if not os.path.exists(source_folder):
        source_folder = workspace

    local_repos = {}
    for p in find_git_repos(source_folder):
        rel = os.path.relpath(p, source_folder)
        if repo_in_include_paths(rel, include_paths):
            local_repos[rel] = p

    zrepos = {rel: e for rel, e in zm['repos'].items()
              if repo_in_include_paths(rel, include_paths)}

    root = _node(f"{os.path.basename(zip_path)}  vs  {workspace}", 'info')
    root['expanded'] = True
    summary = {'+': 0, '-': 0, '~': 0, '=': 0}

    repos_sec = _node('repos', 'info'); repos_sec['expanded'] = True
    for rel in sorted(zrepos):
        zentry = zrepos.get(rel)
        lpath = local_repos.get(rel)
        if lpath is None:
            summary['+'] += 1
            det = [f"In the zip but not in the local workspace.",
                   f"url: {zentry.get('url')}", f"version: {zentry.get('version')}"]
            repos_sec['children'].append(_node(f"{rel}  (only in zip)", 'added', det))
            continue
        zsha = str(zentry.get('version') or '')
        cstatus, clines, cinfo = _repo_commit_info(lpath, zsha)
        try:
            rurl = git.Repo(lpath).git.config('--get', 'remote.origin.url')
        except Exception:
            rurl = None
        url_lines = [f'remote:  {rurl}'] if rurl else []
        zurl = zentry.get('url')
        if zurl and zurl != rurl:
            url_lines.append(f'zip url: {zurl}')
        if url_lines:
            url_lines.append('')
        clines = url_lines + clines
        zdirty = zm['dirty'].get(rel, {})
        ldirty = get_repo_diff(lpath) or {}
        ldirty = {'staged': ldirty.get('staged_diff', ''),
                  'unstaged': ldirty.get('unstaged_diff', ''),
                  'untracked': ldirty.get('untracked_files', {})}
        file_nodes, fcounts = _diff_repo_files(zdirty, ldirty, repo_path=lpath,
                                               zip_sha=zsha if cinfo['known'] else None)
        dirty_differs = fcounts['+'] or fcounts['-'] or fcounts['~']
        if cstatus == 'same' and not dirty_differs:
            summary['='] += 1
            n = _node(f"{rel}  (in sync)", 'same',
                      url_lines + ['HEAD and uncommitted state match the zip.'])
            n['children'] = [c for c in file_nodes]  # identical patches, browsable
            repos_sec['children'].append(n)
            continue
        # "takeable" = the zip still offers something: file changes to apply
        # or commits we don't have. Local-ahead commits and local-only file
        # changes are OUR work — the zip has nothing for us there.
        takeable = bool(fcounts['+'] or fcounts['~']) \
            or not cinfo['known'] or bool(cinfo['behind'])
        if not takeable:
            # nothing to TAKE, but say plainly when the local side is ahead:
            # '^' in ahead-yellow, which bubbles up to the section and root
            summary['='] += 1
            bits = []
            if cinfo['ahead']:
                bits.append(f"local ahead by {cinfo['ahead']}")
            if fcounts['-']:
                bits.append(f"{fcounts['-']} local-only change(s)")
            n = _node(f"{rel}  (nothing to take — {', '.join(bits) or 'local work only'})",
                      'ahead' if cinfo['ahead'] else 'same', clines)
            n['children'] = (_commit_nodes(lpath, zsha, cinfo)
                             if cinfo['known'] and cinfo['ahead'] else []) + file_nodes
            repos_sec['children'].append(n)
            continue
        summary['~'] += 1
        bits = []
        if cstatus != 'same':
            bits.append('commits differ')
        if dirty_differs:
            bits.append('changes: +%d -%d ~%d' % (fcounts['+'], fcounts['-'], fcounts['~']))
        n = _node(f"{rel}  ({', '.join(bits)})", 'changed', clines)
        n['children'] = (_commit_nodes(lpath, zsha, cinfo)
                         if cinfo['known'] and (cinfo['behind'] or cinfo['ahead'])
                         else []) + file_nodes
        bmeta = zm['bundles'].get(rel)
        if cstatus != 'same' and not cinfo['known'] and bmeta:
            n['act'] = {'kind': 'bundle', 'repo': lpath,
                        'zip': os.path.abspath(zip_path),
                        'member': bmeta['file'], 'zip_sha': zsha}
            n['detail'] += ['', f"t/m on this repo node FETCHES the {_SIDE}'s bundle into the",
                            'repo (refs only, no working-tree change); the reloaded tree',
                            'then shows the commits and offers the merge.']
        elif cinfo['known'] and cinfo['behind']:
            n['act'] = {'kind': 'commits', 'repo': lpath, 'zip_sha': zsha,
                        'ahead': cinfo['ahead'], 'behind': cinfo['behind']}
            how = ('fast-forward' if not cinfo['ahead'] else 'merge commit')
            n['detail'] += ['', f't/m on this repo node MERGES the {_SIDE} commits into the local',
                            f'branch ({how}); undo with git reset --hard ORIG_HEAD.']
            if not cinfo['ahead']:
                ok, why, plan = _ff_plan(lpath, zsha)
                if not ok:
                    n['detail'] += [
                        '', 'u (batch update) SKIPS this repo:', f'    {why}',
                        '', 'Those files are being deleted by the zip commits while you',
                        'have local edits in them — dropping local edits is an explicit',
                        'decision, so t (with its confirm) does it and backs the',
                        'originals up first.']
                elif plan['kept']:
                    n['detail'] += ['', 'ff keeps %d locally-modified file(s) uncommitted '
                                    'on top:' % len(plan['kept'])] + \
                                   ['    ' + p for p in plan['kept'][:10]]
        elif cstatus != 'same' and not cinfo['known'] and not bmeta:
            n['detail'] += ['', f'No bundle for this repo in the {_SIDE}: the exporter saw these',
                            "commits on the repo's remote. Pull them with:",
                            '',
                            f'    git -C {lpath} fetch origin',
                            '',
                            '(the repo ssh key may ask for its passphrase), then reload',
                            'with r — the repo then shows ahead/behind and t/u can merge.']
        repos_sec['children'].append(n)

    local_only = sorted(set(local_repos) - set(zrepos))
    if local_only:
        lo = _node(f"local-only repos not in zip ({len(local_only)})", 'info',
                   ['Present in the local workspace but not exported in this zip',
                    '(the zip may cover a smaller pipeline).', ''] + local_only)
        repos_sec['children'].append(lo)
    root['children'].append(repos_sec)

    if zm['bundles']:
        blines = []
        for rel, meta in sorted(zm['bundles'].items()):
            kind = 'full history' if meta.get('self_contained') else 'unpushed commits only'
            blines.append(f"{rel}  ({kind})")
        root['children'].append(_node(f"git bundles in zip ({len(zm['bundles'])})", 'info',
                                      ['Commits that exist only on the exporting machine:',
                                       ''] + blines +
                                      ['', 'To pull them in: select the repo node above and',
                                       'press t (fetches the bundle; after the reload, t',
                                       'again merges the commits).']))

    if zm['colcon']:
        member, ztext = zm['colcon']
        local_colcon = os.path.join(workspace, '.colcon', 'config.yaml')
        cact = {'kind': 'plainfile', 'local_path': local_colcon, 'zip_text': ztext}
        if os.path.isfile(local_colcon):
            ltext = open(local_colcon, encoding='utf-8', errors='replace').read()
            if ltext == ztext:
                root['children'].append(_node('colcon config (same)', 'same', ztext.splitlines()))
            else:
                import difflib
                d = list(difflib.unified_diff(ltext.splitlines(), ztext.splitlines(),
                                              'local/.colcon/config.yaml', 'zip/' + member,
                                              lineterm=''))
                summary['~'] += 1
                root['children'].append(_node('colcon config (differs)', 'changed', d))
        else:
            summary['+'] += 1
            root['children'].append(_node(f'colcon config (only in zip: {member})', 'added',
                                          ztext.splitlines()))
        root['children'][-1]['act'] = cact

    for name, ztext in sorted(zm['tmuxinator'].items()):
        local_t = os.path.expanduser(os.path.join('~/.config/tmuxinator', name))
        tact = {'kind': 'plainfile', 'local_path': local_t, 'zip_text': ztext}
        if os.path.isfile(local_t):
            ltext = open(local_t, encoding='utf-8', errors='replace').read()
            if ltext == ztext:
                root['children'].append(_node(f'tmuxinator/{name} (same)', 'same', ztext.splitlines()))
            else:
                import difflib
                d = list(difflib.unified_diff(ltext.splitlines(), ztext.splitlines(),
                                              'local/' + name, 'zip/' + name, lineterm=''))
                summary['~'] += 1
                root['children'].append(_node(f'tmuxinator/{name} (differs)', 'changed', d))
        else:
            summary['+'] += 1
            root['children'].append(_node(f'tmuxinator/{name} (only in zip)', 'added',
                                          ztext.splitlines()))
        root['children'][-1]['act'] = tact

    src = zm['name'] or '?'
    in_sync = not summary['+'] and not summary['~'] and not summary['-']
    if in_sync:
        root['status'] = 'done'
        root['label'] = (f"{os.path.basename(zip_path)}  ==  {workspace}"
                         "  (nothing left to take)")
    root['detail'] = ([
        f'✓ NOTHING LEFT TO TAKE FROM THE {_SIDE.upper()} — remaining differences (if any)',
        '  are local-only work, which is never touched.',
        '',
    ] if in_sync else []) + [
        f"zip:       {zip_path}",
        f"exported:  {src}  @  {zm['date'] or '?'}",
        f"workspace: {workspace}",
        '',
        f'repos: %d in {_SIDE}, %d compared, %d local-only skipped' % (
            len(zrepos), len(set(zrepos) & set(local_repos)), len(local_only)),
        f'summary: +%(+)d only-in-{_SIDE}, ~%(~)d differ, =%(=)d in sync' % summary,
        '',
        f'legend: + only in {_SIDE}   - only local   ^ local ahead   ~ differs',
        '        = in sync   ✓ resolved   ! conflict/clash',
        'colors: a parent takes the WORST status beneath it, by this priority',
        '        (low to high):  = in sync  <  ✓ resolved  <  - local-only change',
        '        <  ^ local ahead  <  + to take  <  ~ differs  <  ! needs you',
        'keys:   j/k move   l/Enter expand   h collapse   J/K scroll detail   q quit',
        'merge:  d diff/preview   o checkout --ours (keep local)   t checkout',
    f'        --theirs (take {_SIDE})   m merge 3-way   M merge all',
    '        (the bottom bar always shows the keys valid for the selected node)',
    '        u = safe batch update: apply every NON-conflicting zip change',
    '        (3-way merge; conflicting files stay untouched, then walk them with o/t/m)',
        '        after every action the diff RELOADS from disk (r = reload manually);',
        '        ✓/! marks persist across reloads for this session',
        '        (o/t/m act on the selected node AND everything beneath it;',
        '         originals are backed up under /tmp/rvcs_merge_backup_*)',
        'repos:  on a repo whose commits exist only in the zip, t fetches the',
        '        bundled commits, then (after reload) t merges them — ff when possible',
    ]
    return root


# What the "theirs" side of the diff tree is called in every label and hint.
# 'zip' for real exports; the no-zip upstream mode sets 'remote' so the tree
# does not talk about a zip that never existed.
_SIDE = 'zip'


def set_side_label(label):
    global _SIDE
    _SIDE = label


_STATUS_GLYPH = {'added': '+', 'removed': '-', 'changed': '~', 'same': '=', 'info': ' ',
                 'done': '✓', 'conflict': '!', 'clash': '!', 'ahead': '^'}

# PROPAGATION PRIORITY — a parent is colored by the worst status in its
# subtree, so a collapsed repo can never hide something that needs you.
# The ladder, lowest first (higher number wins when they meet):
#
#   0 info      structural node, nothing to say
#   1 same      = identical on both sides
#   2 done      ✓ resolved by an action this session
#   3 removed   - local-only FILE change; the zip has nothing here
#   4 ahead     ^ local is AHEAD of the zip (your commits / your work)
#   5 added     + the zip has something to take
#   6 changed   ~ both sides differ, mergeable
#   7 clash     ! the zip's change collides with local work
#   8 conflict  ! conflict markers sitting in the file
#
# Rationale: informational states (your own work: removed/ahead) rank below
# actionable ones (something to take: added/changed), which rank below
# states that need a human decision (clash/conflict).
_SEVERITY = {'info': 0, 'same': 1, 'done': 2, 'removed': 3, 'ahead': 4,
             'added': 5, 'changed': 6, 'clash': 7, 'conflict': 8}


def _annotate_worst(node):
    """Store on every node the worst status of its subtree (itself included).
    Returns that status; the TUI uses it for tree colors."""
    worst = node['status']
    for c in node['children']:
        cs = _annotate_worst(c)
        if _SEVERITY.get(cs, 0) > _SEVERITY.get(worst, 0):
            worst = cs
    node['worst'] = worst
    return worst


# --- merge actions (accept ours / accept theirs / 3-way merge) --------------

class _MergeCtx:
    """Backup dir + result log shared by all actions of one TUI session."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.backup_dir = None
        self.results = []
        self.resolutions = {}   # _node_key -> (status, note): survives reloads

    def backup(self, path):
        """Copy a file into the session backup dir before first modification."""
        if not os.path.exists(path):
            return
        if self.backup_dir is None:
            stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            self.backup_dir = os.path.join(tempfile.gettempdir(), f'rvcs_merge_backup_{stamp}')
        rel = os.path.relpath(path, self.workspace)
        if rel.startswith('..'):
            rel = path.lstrip(os.sep)
        dest = os.path.join(self.backup_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest):  # keep the ORIGINAL, not intermediate states
            shutil.copy2(path, dest)

    def log(self, line):
        self.results.append(line)


def _git_show_head(repo_path, rel_file, rev='HEAD'):
    """Content of rel_file at rev ('' if the file is new/unknown there).
    Raw subprocess on purpose: GitPython's .git.show() strips the trailing
    newline, which breaks patch application and 3-way merges."""
    import subprocess
    try:
        r = subprocess.run(['git', '-C', repo_path, 'show', f'{rev}:{rel_file}'],
                           capture_output=True)
        return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else ''
    except Exception:
        return ''


def _apply_patch_to_text(base_text, patch_text, rel_file):
    """Apply a per-file unified diff to base_text (returns new text or None).
    Runs `git apply` inside a scratch repo so we never touch the real one."""
    with tempfile.TemporaryDirectory(prefix='rvcs_apply_') as td:
        target = os.path.join(td, rel_file)
        os.makedirs(os.path.dirname(target) or td, exist_ok=True)
        with open(target, 'w', encoding='utf-8', errors='replace') as f:
            f.write(base_text)
        pfile = os.path.join(td, '.rvcs.patch')
        with open(pfile, 'w', encoding='utf-8', errors='replace') as f:
            f.write(patch_text if patch_text.endswith('\n') else patch_text + '\n')
        try:
            import subprocess
            subprocess.run(['git', 'init', '-q', td], check=True, capture_output=True)
            subprocess.run(['git', '-C', td, 'apply', '.rvcs.patch'],
                           check=True, capture_output=True)
            with open(target, encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception:
            return None


def _merge_texts(ours, base, theirs, label_ours='local', label_theirs='zip'):
    """3-way merge via git merge-file. Returns (merged_text, n_conflicts)."""
    import subprocess
    with tempfile.TemporaryDirectory(prefix='rvcs_merge_') as td:
        po, pb, pt = (os.path.join(td, n) for n in ('ours', 'base', 'theirs'))
        for p, t in ((po, ours), (pb, base), (pt, theirs)):
            with open(p, 'w', encoding='utf-8', errors='replace') as f:
                f.write(t)
        r = subprocess.run(['git', 'merge-file', '-L', label_ours, '-L', 'base',
                            '-L', label_theirs, po, pb, pt], capture_output=True)
        with open(po, encoding='utf-8', errors='replace') as f:
            merged = f.read()
        return merged, max(0, r.returncode)


def _merge_preview(node):
    """Detail lines showing what a 3-way merge of this node would produce."""
    act = node.get('act') or {}
    if act.get('kind') == 'bundle':
        return ["t fetches the zip's bundle into this repo: refs only, stored",
                'under refs/rvcs-bundle/ — the working tree is untouched.',
                'After the automatic reload the incoming commits are listed here',
                'and t merges them (fast-forward when possible).']
    if act.get('kind') == 'commits':
        lines = ['── incoming commits from the zip ──']
        try:
            lines += git.Repo(act['repo']).git.log(
                '--oneline', 'HEAD..' + act['zip_sha']).splitlines()[:30]
        except Exception as e:
            lines += [f'(could not list: {e})']
        if not act.get('ahead'):
            lines += ['', 'fast-forward (local has no commits of its own)']
            return lines
        lines += ['', 'diverged: t creates a merge commit '
                  '(%d local vs %d zip commit(s))' % (act['ahead'], act['behind'])]
        dry = _merge_dry_run(act['repo'], act['zip_sha'])
        if dry is not None:
            lines += ['', '── merge dry-run (nothing written) ──']
            if dry['conflicts']:
                lines += ['CONFLICT (%d file(s)):' % len(dry['conflicts'])]
                lines += ['    ! %s  (%d hunk(s))' % (f, dry['hunks'].get(f, 0))
                          for f in dry['conflicts'][:20]]
            if dry['clean']:
                lines += ['auto-merge cleanly (%d file(s)):' % len(dry['clean'])]
                lines += ['    ✓ %s' % f for f in dry['clean'][:40]]
        return lines
    if act.get('kind') == 'patch' and act.get('zip_patch'):
        base_rev = act.get('zip_sha') or 'HEAD'
        base = _git_show_head(act['repo'], act['file'], rev=base_rev)
        theirs = _apply_patch_to_text(base, act['zip_patch'], act['file'])
        local_path = os.path.join(act['repo'], act['file'])
        try:
            ours = open(local_path, encoding='utf-8', errors='replace').read()
        except OSError:
            ours = ''
        if theirs is None:
            return ['(cannot preview: zip patch does not apply onto the %s '
                    'version of the file)' % ('zip base' if act.get('zip_sha') else 'HEAD')]
        merged, n = _merge_texts(ours, base, theirs)
        head = ['── 3-way merge preview (base: %s): %d conflict(s) ──'
                % (base_rev[:9], n), '']
        return head + merged.splitlines()
    if act.get('kind') == 'untracked' and act.get('zip_entry') and act.get('local_entry'):
        zb, ztext, _ = _untracked_text(act['zip_entry'])
        lb, ltext, _ = _untracked_text(act['local_entry'])
        if zb or lb:
            return ['(binary file: no merge preview — use o/t to pick a side)']
        merged, n = _merge_texts(ltext or '', '', ztext or '')
        return ['── 3-way merge preview (no common base): %d conflict(s) ──' % n,
                ''] + merged.splitlines()
    if act.get('kind') == 'untracked' and act.get('zip_entry') and not act.get('local_entry'):
        zb, ztext, _ = _untracked_text(act['zip_entry'])
        lp = os.path.join(act['repo'], act['file']) if act.get('repo') else None
        if lp and os.path.exists(lp) and not zb:
            try:
                disk = open(lp, encoding='utf-8', errors='replace').read()
            except OSError:
                disk = None
            if disk is not None:
                merged, n = _merge_texts(disk, '', ztext or '')
                return ['── merge preview vs tracked local file (no common base): '
                        '%d conflict(s) ──' % n, ''] + merged.splitlines()
    return ['(no merge preview for this node — d works on files changed on '
            'BOTH sides)']


def _write_local(ctx, path, text):
    ctx.backup(path)
    os.makedirs(os.path.dirname(path) or '/', exist_ok=True)
    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(text)


def _finish(node, status, note):
    node['status'] = status
    base = node['label'].split('  (')[0]
    node['label'] = f"{base}  ({note})"
    return note


def _fetch_bundle(node, act):
    """Fetch the zip's git bundle into the local repo: refs only (under
    refs/rvcs-bundle/), no working-tree change. After the reload the zip's
    commits are visible and the repo node offers the merge."""
    import subprocess
    repo = act['repo']
    try:
        with zipfile.ZipFile(act['zip']) as zf, \
             tempfile.NamedTemporaryFile(suffix='.bundle') as tf:
            tf.write(zf.read(act['member']))
            tf.flush()
            r = subprocess.run(['git', '-C', repo, 'bundle', 'verify', tf.name],
                               capture_output=True)
            if r.returncode != 0:
                err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
                return _finish(node, 'conflict',
                               'bundle verify failed: %s' % (err[-1] if err else '?'))
            heads = subprocess.run(['git', '-C', repo, 'bundle', 'list-heads', tf.name],
                                   capture_output=True, check=True).stdout.decode()
            specs = []
            for line in heads.splitlines():
                ref = line.partition(' ')[2].strip()
                if not ref:
                    continue
                if ref.startswith('refs/heads/'):
                    dest = ref[len('refs/heads/'):]
                elif ref.startswith('refs/'):
                    dest = ref[len('refs/'):]
                else:
                    dest = ref  # 'HEAD'
                specs.append('+%s:refs/rvcs-bundle/%s' % (ref, dest))
            subprocess.run(['git', '-C', repo, 'fetch', '--no-write-fetch-head',
                            tf.name] + specs, capture_output=True, check=True)
    except Exception as e:
        return _finish(node, 'conflict', f'bundle fetch failed: {e}')
    try:
        behind = git.Repo(repo).git.rev_list('--count', 'HEAD..' + act['zip_sha'])
    except Exception:
        behind = '?'
    return _finish(node, 'done',
                   f'bundle fetched: {behind} zip commit(s) now in refs/rvcs-bundle/')


def _ff_plan(repo, sha, allow_delete=False, label='zip', delete_hint=None):
    """Work out what fast-forwarding to sha needs, WITHOUT touching anything.

    git's ff-checkout refuses whenever a file it must update is dirty. That is
    safe to work around only when the local version already CONTAINS the
    incoming change (worktree ⊇ zip side, verified by 3-way merge) — then the
    branch can move while the worktree keeps the local extras.

    `label` names the incoming side in messages (default 'zip', matching the
    diff-TUI callers this was written for); `delete_hint` is the sentence
    telling the human how to confirm dropping a locally-modified file the
    incoming side deletes, since that's a UI-specific instruction that
    differs by caller (defaults to the diff-TUI's own wording).

    Returns (ok, note, plan). plan = {'refresh', 'gone', 'kept', 'drop'}:
    refresh = clean files to update, gone = clean files the incoming side
    deletes, kept = dirty supersets left alone, drop = DIRTY files the
    incoming side deletes (only ever populated with allow_delete=True —
    deleting local edits needs an explicit decision, so the safe batch
    update blocks on them instead)."""
    import subprocess
    if delete_hint is None:
        delete_hint = 't on the repo node confirms dropping them'

    def out(*a):
        return subprocess.run(['git', '-C', repo] + list(a), capture_output=True)

    changed = [l for l in out('diff', '--name-only', 'HEAD', sha)
               .stdout.decode().splitlines() if l]
    porcelain = out('status', '--porcelain').stdout.decode().splitlines()
    dirty = {l[3:].split(' -> ')[-1].strip('"') for l in porcelain}
    plan = {'refresh': [], 'gone': [], 'kept': [], 'drop': []}
    for path in changed:
        in_zip = out('cat-file', '-e', f'{sha}:{path}').returncode == 0
        if path not in dirty:
            plan['gone' if not in_zip else 'refresh'].append(path)
            continue
        if not in_zip:      # incoming DELETES a file that is dirty here
            if not os.path.exists(os.path.join(repo, path)):
                continue    # deleted on BOTH sides — agreement, nothing to do
            if not allow_delete:
                n_del = sum(1 for p in changed
                            if p in dirty and os.path.exists(os.path.join(repo, p))
                            and out('cat-file', '-e', f'{sha}:{p}').returncode != 0)
                return False, ('%d locally-modified file(s) are deleted by the '
                               '%s commits (e.g. %s) — %s'
                               % (n_del, label, path, delete_hint)), plan
            plan['drop'].append(path)
            continue
        theirs_b = out('cat-file', 'blob', f'{sha}:{path}').stdout
        try:
            with open(os.path.join(repo, path), 'rb') as f:
                ours_b = f.read()
        except OSError:
            return False, f'{path}: locally deleted but changed in {label}', plan
        if ours_b == theirs_b:
            continue        # identical content, nothing to preserve
        if b'\0' in ours_b[:8192] or b'\0' in theirs_b[:8192]:
            return False, f'{path}: binary differs from the {label} version', plan
        base = _git_show_head(repo, path, rev='HEAD')
        merged, n = _merge_texts(ours_b.decode('utf-8', 'replace'), base,
                                 theirs_b.decode('utf-8', 'replace'))
        if n or merged != ours_b.decode('utf-8', 'replace'):
            return False, (f'{path}: local changes are not a superset of the '
                           f'{label} change'), plan
        plan['kept'].append(path)
    note = 'fast-forward possible'
    if plan['kept']:
        note += ' (%d local extra(s) kept)' % len(plan['kept'])
    if plan['drop']:
        note += ' (%d locally-modified file(s) dropped)' % len(plan['drop'])
    return True, note, plan


def _dirty_preserving_ff(repo, sha, ctx=None, allow_delete=False, label='zip',
                         delete_hint=None):
    """Execute the _ff_plan: move branch+index, refresh clean files, drop the
    files the incoming side deletes, leave dirty supersets untouched.
    `label`/`delete_hint`: see _ff_plan. Returns (ok, note)."""
    import subprocess

    def out(*a):
        return subprocess.run(['git', '-C', repo] + list(a), capture_output=True)

    ok, note, plan = _ff_plan(repo, sha, allow_delete=allow_delete, label=label,
                              delete_hint=delete_hint)
    if not ok:
        return False, note
    for path in plan['drop']:       # back up local edits before removing them
        if ctx is not None:
            ctx.backup(os.path.join(repo, path))
    if out('reset', '-q', '--mixed', sha).returncode != 0:
        return False, 'git reset failed'
    if plan['refresh']:             # bring previously-clean files up to date
        out('checkout', '--', *plan['refresh'])
    for path in plan['gone'] + plan['drop']:
        try:
            os.unlink(os.path.join(repo, path))
        except OSError:
            pass
    note = f'fast-forwarded to {label} HEAD'
    if plan['kept']:
        note += ' (%d local extra(s) kept uncommitted)' % len(plan['kept'])
    if plan['drop']:
        note += ' (%d locally-modified file(s) deleted per the %s)' % (len(plan['drop']), label)
    return True, note


def _merge_zip_commits(node, act, ctx=None, allow_delete=False):
    """Merge the zip's commits into the local branch. Fast-forward when the
    local repo is strictly behind (preserving dirty files that already
    contain the incoming change); a normal merge commit when diverged. On
    conflicts the merge is aborted and left to the user.
    allow_delete lets the ff drop locally-modified files the zip deletes —
    only for explicit per-node actions (t/m), never the batch update."""
    import subprocess
    repo, sha = act['repo'], act['zip_sha']
    ff = not act.get('ahead')
    ident = []
    if not subprocess.run(['git', '-C', repo, 'config', 'user.email'],
                          capture_output=True).stdout.strip():
        ident = ['-c', 'user.name=rvcs', '-c', 'user.email=rvcs@localhost']
    args = ['git'] + ident + ['-C', repo, 'merge', '--no-edit'] + \
           (['--ff-only'] if ff else []) + [sha]
    r = subprocess.run(args, capture_output=True)
    if r.returncode == 0:
        note = ('fast-forwarded to zip HEAD' if ff else
                'merged %d zip commit(s); undo: git reset --hard ORIG_HEAD'
                % act.get('behind', 0))
        return _finish(node, 'done', note)
    if ff:
        # the usual refusal: dirty/untracked files in the way. If they all
        # already contain the incoming change, ff without touching them.
        ok, note = _dirty_preserving_ff(repo, sha, ctx=ctx,
                                        allow_delete=allow_delete)
        if ok:
            return _finish(node, 'done', note)
        return _finish(node, 'conflict', 'ff blocked — ' + note)
    if os.path.exists(os.path.join(repo, '.git', 'MERGE_HEAD')) or \
       subprocess.run(['git', '-C', repo, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'],
                      capture_output=True).returncode == 0:
        subprocess.run(['git', '-C', repo, 'merge', '--abort'], capture_output=True)
        return _finish(node, 'conflict',
                       f'merge conflicts — aborted; run git merge {sha[:9]} in the repo')
    err = (r.stderr or r.stdout).decode('utf-8', 'replace').strip().splitlines()
    return _finish(node, 'conflict',
                   'merge failed: %s' % (err[-1] if err else '?'))


def apply_action(node, mode, ctx):
    """Apply 'ours'/'theirs'/'merge' to ONE node. Returns a result line or None
    (None = nothing actionable on this node)."""
    act = node.get('act')
    if not act or node['status'] in ('done', 'conflict', 'same'):
        return None
    kind = act['kind']

    if kind == 'plainfile':
        if mode == 'ours':
            return _finish(node, 'done', 'kept local')
        _write_local(ctx, act['local_path'], act['zip_text'])
        return _finish(node, 'done', f'took {_SIDE} version')

    if kind == 'bundle':
        if mode == 'ours':
            return _finish(node, 'done', 'kept local (bundle not fetched)')
        return _fetch_bundle(node, act)

    if kind == 'commits':
        if mode == 'ours':
            return _finish(node, 'done', 'kept local commits')
        # explicit per-node action (behind a y/N confirm in the TUI): may drop
        # locally-modified files the zip deletes, originals backed up first
        return _merge_zip_commits(node, act, ctx=ctx, allow_delete=True)

    repo_path, rel_file = act.get('repo'), act.get('file')
    local_path = os.path.join(repo_path, rel_file) if repo_path else None

    if kind == 'untracked':
        ze, le = act.get('zip_entry'), act.get('local_entry')
        if mode == 'ours':
            return _finish(node, 'done', 'kept local')
        if ze is None:                       # local-only file, theirs = remove it
            if mode == 'merge':
                return _finish(node, 'done', 'kept local (zip has no version)')
            ctx.backup(local_path)
            os.unlink(local_path)
            return _finish(node, 'done', 'removed (not in zip)')
        zb, ztext, _ = _untracked_text(ze)
        if le is None and mode == 'merge' and not zb and local_path \
                and os.path.exists(local_path):
            # zip-untracked but existing here (tracked): merge with disk content
            try:
                disk_text = open(local_path, encoding='utf-8', errors='replace').read()
            except OSError:
                disk_text = None
            if disk_text is not None:
                if disk_text == (ztext or ''):
                    return _finish(node, 'done', 'already identical')
                merged, n = _merge_texts(disk_text, '', ztext or '')
                _write_local(ctx, local_path, merged)
                if n:
                    return _finish(node, 'conflict', f'{n} conflict(s) — markers written')
                return _finish(node, 'done', 'merged with tracked local file')
        if le is None or mode == 'theirs':   # take zip content
            if zb:
                raw = base64.b64decode(ze.get('content', ''))
                ctx.backup(local_path)
                os.makedirs(os.path.dirname(local_path) or '/', exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(raw)
            else:
                _write_local(ctx, local_path, ztext or '')
            return _finish(node, 'done', f'took {_SIDE} version')
        lb, ltext, _ = _untracked_text(le)
        if zb or lb:
            return _finish(node, 'conflict', 'binary differs — pick o/t')
        merged, n = _merge_texts(ltext or '', '', ztext or '')
        _write_local(ctx, local_path, merged)
        if n:
            return _finish(node, 'conflict', f'{n} conflict(s) — markers written')
        return _finish(node, 'done', 'merged')

    if kind == 'patch':
        zp, lp = act.get('zip_patch'), act.get('local_patch')
        if mode == 'ours':
            return _finish(node, 'done', 'kept local')
        if zp is None:                       # modified only locally, theirs = revert
            if mode == 'merge':
                return _finish(node, 'done', 'kept local (zip has no change)')
            ctx.backup(local_path)
            try:
                git.Repo(repo_path).git.checkout('HEAD', '--', rel_file)
            except Exception as e:
                return _finish(node, 'conflict', f'revert failed: {e}')
            return _finish(node, 'done', 'reverted to HEAD (as in zip)')
        # base = the commit the zip's patch was made against, when it is known
        # locally; HEAD only as a fallback
        base_rev = act.get('zip_sha') or 'HEAD'
        base_text = _git_show_head(repo_path, rel_file, rev=base_rev)
        theirs = _apply_patch_to_text(base_text, zp, rel_file)
        if theirs is None:
            return _finish(node, 'conflict', 'zip patch does not apply onto %s'
                           % ('its base commit' if act.get('zip_sha') else 'local HEAD'))
        if mode == 'theirs':                 # take zip side wholesale
            _write_local(ctx, local_path, theirs)
            return _finish(node, 'done', f'took {_SIDE} version')
        try:
            ours = open(local_path, encoding='utf-8', errors='replace').read()
        except OSError:
            ours = base_text
        if ours == theirs:
            return _finish(node, 'done', 'already identical')
        merged, n = _merge_texts(ours, base_text, theirs)
        _write_local(ctx, local_path, merged)
        if n:
            return _finish(node, 'conflict', f'{n} conflict(s) — markers written')
        return _finish(node, 'done', 'merged')
    return None


def apply_subtree(node, mode, ctx):
    """Apply an action to a node and everything below it. Returns #acted."""
    count = 0
    try:
        line = apply_action(node, mode, ctx)
    except Exception as e:
        # One unwritable/broken file must not abort a subtree merge: mark the
        # node and carry on (seen in the wild: files left root-owned by other
        # tooling -> PermissionError mid-merge).
        line = _finish(node, 'conflict', f'FAILED: {e}')
    if line:
        ctx.log(f"{node['label']}")
        key = _node_key(node)
        if key:
            ctx.resolutions[key] = (node['status'], line)
        count += 1
    for c in node['children']:
        count += apply_subtree(c, mode, ctx)
    return count


def update_workspace_state(zip_path, workspace, include_paths=None, dry_run=False,
                           ctx=None):
    """Apply every NON-conflicting change from an export zip to the workspace.

    - zip-side changes are taken via a true 3-way merge: when the zip HEAD
      contains commits unknown locally, its bundles/ are fetched (objects
      only) so the patch applies onto the base it was made against
    - files changed on BOTH sides are written only when the merge is
      conflict-free; anything conflicting is left untouched and reported
    - repos strictly behind the zip are fast-forwarded (after fetching the
      zip's bundle when needed) — a strict ff cannot conflict; diverged
      repos are reported and left for an explicit merge in the TUI
    - local-only changes are never reverted, nothing is ever deleted
    Returns (applied, skipped, kept, ctx); dry_run writes nothing.
    A caller with its own _MergeCtx (the TUI's u key) can pass it so all
    backups of one session share a directory.
    """
    import subprocess
    zm = load_state_zip(zip_path)
    root = compute_state_diff(zip_path, workspace, include_paths)
    ctx = ctx or _MergeCtx(workspace)
    src = os.path.join(workspace, 'src')
    if not os.path.isdir(src):
        src = workspace
    applied, skipped, kept = [], [], []
    fetched = {}

    def mark(node):
        """Remember an applied node in the ctx so the TUI paints it ✓ across
        reloads (keys are stable between tree rebuilds)."""
        if not dry_run:
            key = _node_key(node)
            if key:
                ctx.resolutions[key] = ('done', 'applied by update')

    def ensure_sha(repo_rel, repo_path, sha):
        """Make the zip's commit available locally, fetching its bundle if needed."""
        ok = subprocess.run(['git', '-C', repo_path, 'cat-file', '-e', sha],
                            capture_output=True).returncode == 0
        if ok:
            return True
        if repo_rel in fetched:
            return fetched[repo_rel]
        got = False
        meta = zm['bundles'].get(repo_rel)
        if meta:
            with zipfile.ZipFile(zip_path) as zf:
                data = zf.read(meta['file'])
            bf = tempfile.NamedTemporaryFile(suffix='.bundle', delete=False)
            try:
                bf.write(data)
                bf.close()
                subprocess.run(['git', '-C', repo_path, 'fetch', bf.name, 'HEAD'],
                               capture_output=True)
                got = subprocess.run(['git', '-C', repo_path, 'cat-file', '-e', sha],
                                     capture_output=True).returncode == 0
            finally:
                os.unlink(bf.name)
        fetched[repo_rel] = got
        return got

    def show(repo_path, sha, f):
        r = subprocess.run(['git', '-C', repo_path, 'show', f'{sha}:{f}'],
                           capture_output=True)
        return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else None

    def handle(node):
        act = node.get('act') or {}
        kind = act.get('kind')
        st = node['status']
        label = _norm_label(node)
        if kind == 'patch':
            if st == 'conflict':
                skipped.append((label, 'unresolved conflict markers in the file'))
                return
            if st == 'removed':
                kept.append(label)
                return
            if st in ('same', 'done'):
                return
            repo_path, f = act['repo'], act['file']
            zp = act.get('zip_patch')
            if zp is None:
                return
            repo_rel = os.path.relpath(repo_path, src)
            his_sha = str((zm['repos'].get(repo_rel) or {}).get('version') or '')
            his_txt = base_txt = None
            if his_sha and ensure_sha(repo_rel, repo_path, his_sha):
                his_txt = show(repo_path, his_sha, f)
                mb = subprocess.run(['git', '-C', repo_path, 'merge-base', 'HEAD', his_sha],
                                    capture_output=True, text=True).stdout.strip()
                base_txt = show(repo_path, mb, f) if mb else None
            if his_txt is None:
                his_txt = show(repo_path, 'HEAD', f) or ''
            if base_txt is None:
                base_txt = show(repo_path, 'HEAD', f) or ''
            theirs = _apply_patch_to_text(his_txt, zp, f)
            if theirs is None:
                skipped.append((label, 'zip patch does not apply'))
                return
            lp = os.path.join(repo_path, f)
            ours = open(lp, encoding='utf-8', errors='replace').read() \
                if os.path.exists(lp) else base_txt
            if ours == theirs:
                return
            merged, n = _merge_texts(ours, base_txt, theirs)
            if n:
                skipped.append((label, f'{n} conflict(s) — left untouched'))
                return
            if merged == ours:
                return  # their change is already contained in the local file
            if not dry_run:
                _write_local(ctx, lp, merged)
            mark(node)
            applied.append(label)
        elif kind == 'untracked':
            ze, le = act.get('zip_entry'), act.get('local_entry')
            if ze is None:
                kept.append(label)
                return
            if st in ('same', 'done'):
                return
            lp = os.path.join(act['repo'], act['file'])
            zb, ztext, _ = _untracked_text(ze)
            if le is None:
                if os.path.exists(lp):
                    # untracked in the zip but TRACKED here (e.g. we committed
                    # it since their export) — never overwrite silently
                    try:
                        same = (open(lp, 'rb').read() ==
                                (base64.b64decode(ze.get('content', '')) if zb
                                 else (ztext or '').encode()))
                    except OSError:
                        same = False
                    if not same:
                        skipped.append((label, 'exists locally (tracked) with '
                                               'different content'))
                    return
                if not dry_run:
                    if zb:
                        ctx.backup(lp)
                        os.makedirs(os.path.dirname(lp) or '/', exist_ok=True)
                        with open(lp, 'wb') as fh:
                            fh.write(base64.b64decode(ze.get('content', '')))
                    else:
                        _write_local(ctx, lp, ztext or '')
                mark(node)
                applied.append(label)
                return
            lb, ltext, _ = _untracked_text(le)
            if zb or lb:
                skipped.append((label, 'binary content differs'))
                return
            merged, n = _merge_texts(ltext or '', '', ztext or '')
            if n:
                skipped.append((label, f'{n} conflict(s) — left untouched'))
                return
            if not dry_run:
                _write_local(ctx, lp, merged)
            mark(node)
            applied.append(label)
        elif kind == 'plainfile':
            if st == 'added':
                if not dry_run:
                    _write_local(ctx, act['local_path'], act['zip_text'])
                mark(node)
                applied.append(label)
            elif st == 'changed':
                skipped.append((label, 'differs — pick a side in the TUI (o/t/m)'))
        elif kind == 'commits':
            # zip commits already visible locally: a strict fast-forward is
            # non-conflicting by definition — apply it; diverged repos need
            # a real merge commit, which stays an explicit decision
            if st in ('same', 'done'):
                return
            if act.get('ahead'):
                skipped.append((label, 'diverged — merge from the repo node (t)'))
                return
            if dry_run:
                # probe for real: a dirty worktree can block the ff, and
                # claiming 'would apply' for something u then refuses is worse
                # than saying nothing
                ok, why, _plan = _ff_plan(act['repo'], act['zip_sha'])
                if ok:
                    applied.append(f"{label}: fast-forward {act.get('behind')} commit(s)")
                else:
                    skipped.append((label, f'ff blocked — {why}'))
                return
            note = _merge_zip_commits(node, act, ctx=ctx)
            if node['status'] == 'done':
                applied.append(f"{label}: {note}")
            else:
                skipped.append((label, note))
        elif kind == 'bundle':
            # fetching a bundle only adds refs — harmless; follow up with a
            # fast-forward when the local branch is then strictly behind
            if st in ('same', 'done'):
                return
            if dry_run:
                applied.append(f"{label}: fetch zip bundle (+ fast-forward "
                               "if strictly behind)")
                return
            note = _fetch_bundle(node, act)
            if node['status'] != 'done':
                skipped.append((label, note))
                return
            applied.append(f"{label}: {note}")
            try:
                r = git.Repo(act['repo'])
                ahead = int(r.git.rev_list('--count', act['zip_sha'] + '..HEAD'))
                behind = int(r.git.rev_list('--count', 'HEAD..' + act['zip_sha']))
            except Exception as e:
                skipped.append((label, f'post-fetch state unknown: {e}'))
                return
            if not behind:
                return
            if ahead:
                skipped.append((label, 'diverged — merge from the repo node (t)'))
                return
            node['status'] = 'changed'   # let the second action run
            note = _merge_zip_commits(node, {'repo': act['repo'],
                                             'zip_sha': act['zip_sha'],
                                             'ahead': 0, 'behind': behind},
                                      ctx=ctx)
            if node['status'] == 'done':
                applied.append(f"{label}: {note}")
            else:
                skipped.append((label, note))

    def walk(n):
        handle(n)
        for c in n['children']:
            walk(c)

    walk(root)
    return applied, skipped, kept, ctx


def diff_tree_to_json(root, zip_path, workspace):
    """Machine-readable dump of the diff tree (for Claude Code and scripts)."""
    def strip(n):
        out = {'label': n['label'], 'status': n['status'], 'detail': n['detail']}
        if n.get('act'):
            a = dict(n['act'])
            for k in ('zip_entry', 'local_entry'):
                if a.get(k) is not None:
                    a[k] = {'binary': bool(a[k].get('binary')),
                            'size': len(a[k].get('content', ''))}
            out['act'] = a
        if n['children']:
            out['children'] = [strip(c) for c in n['children']]
        return out
    return {'zip': os.path.abspath(zip_path), 'workspace': os.path.abspath(workspace),
            'tree': strip(root)}


def print_state_diff(root, detail=False, _depth=0):
    """Plain-text fallback when stdout is not a terminal (or --no-tui)."""
    glyph = _STATUS_GLYPH.get(root['status'], ' ')
    print('%s%s %s' % ('  ' * _depth, glyph, root['label']))
    if detail:
        for line in root['detail']:
            print('%s    %s' % ('  ' * _depth, line))
    for c in root['children']:
        print_state_diff(c, detail=detail, _depth=_depth + 1)


def _node_key(node):
    """Stable identity of an actionable node across tree rebuilds."""
    act = node.get('act')
    if not act:
        return None
    return (act.get('kind'), act.get('repo'), act.get('file'), act.get('local_path'))


def _norm_label(node):
    return node['label'].split('  (')[0]


def _walk_paths(node, prefix=()):
    """Yield (path_tuple, node) for the whole tree."""
    path = prefix + (_norm_label(node),)
    yield path, node
    for c in node['children']:
        yield from _walk_paths(c, path)


def run_diff_tui(root, ctx=None, rebuild=None, updater=None):
    """Curses tree browser: left pane = diff tree, right pane = node detail.
    With a _MergeCtx, o/t/m/M apply accept-ours/accept-theirs/merge actions;
    with an updater callable, u batch-applies every NON-conflicting zip change
    (--update-state semantics: conflicts left untouched for o/t/m).
    After every action (and on 'r') the diff is recomputed from disk via
    rebuild(); resolved/conflict marks are overlaid, expansion + cursor kept."""
    import curses

    def flatten(node, depth=0, out=None):
        out = [] if out is None else out
        out.append((node, depth))
        if node['expanded']:
            for c in node['children']:
                flatten(c, depth + 1, out)
        return out

    LEGEND = [(f'+ only in {_SIDE}', 'added'), ('- only local', 'removed'),
              ('^ local ahead', 'ahead'), ('~ differs', 'changed'),
              ('= in sync', 'same'), ('✓ resolved', 'done'),
              ('! conflict', 'conflict')]

    def draw(stdscr, rows, sel, tree_top, detail_off, colors):
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        split = max(34, int(maxx * 0.45))
        tree_h = maxy - 2
        for i in range(tree_h):
            idx = tree_top + i
            if idx >= len(rows):
                break
            node, depth = rows[idx]
            mark = ' '
            if node['children']:
                mark = '▾' if node['expanded'] else '▸'
            glyph = _STATUS_GLYPH.get(node['status'], ' ')
            text = '%s%s %s %s' % ('  ' * depth, mark, glyph, node['label'])
            attr = colors.get(node.get('worst', node['status']),
                              colors.get(node['status'], 0))
            if idx == sel:
                attr |= curses.A_REVERSE
            try:
                stdscr.addnstr(i, 0, text.ljust(split - 1), split - 1, attr)
            except curses.error:
                pass
        for i in range(tree_h):
            try:
                stdscr.addstr(i, split - 1, '│')
            except curses.error:
                pass
        dw = maxx - split - 1
        sel_node = rows[sel][0] if sel < len(rows) else None
        detail = sel_node['detail'] if sel_node else []
        # full label of the selected node as a wrapped, colored header — the
        # tree pane truncates long labels, the detail pane never should
        hdr = []
        if sel_node:
            import textwrap
            full = '%s %s' % (_STATUS_GLYPH.get(sel_node['status'], ' '),
                              sel_node['label'])
            hdr = textwrap.wrap(full, max(10, dw)) or [full]
        hdr_attr = (colors.get(sel_node['status'], 0) | curses.A_BOLD) if sel_node else 0
        for i, line in enumerate(hdr):
            if i >= tree_h:
                break
            try:
                stdscr.addnstr(i, split, line, dw, hdr_attr)
            except curses.error:
                pass
        off = min(len(hdr) + 1, tree_h) if hdr else 0  # +1 blank separator
        # long detail lines WRAP (hard-sliced, code stays aligned) instead of
        # being cut at the pane edge; continuations keep the line's color
        row, li = off, detail_off
        while row < tree_h and li < len(detail) and dw > 0:
            line = detail[li]
            attr = 0
            if line.startswith(('<<<<<<<', '=======', '>>>>>>>')):
                attr = colors.get('confmark', colors.get('conflict', 0)) | curses.A_BOLD
            elif line.startswith('+') and not line.startswith('+++'):
                attr = colors.get('added', 0)
            elif line.startswith('-') and not line.startswith('---'):
                attr = colors.get('removed', 0)
            elif line.startswith('@@') or line.startswith('──'):
                attr = colors.get('info', 0)
            elif re.match(r'^[|/\\ ]*<', line):      # graph: local-only commit
                attr = colors.get('removed', 0)
            elif re.match(r'^[|/\\ ]*>', line):      # graph: zip-only commit
                attr = colors.get('added', 0)
            elif re.match(r'^[|/\\ ]*o ', line):     # graph: the fork point
                attr = colors.get('info', 0) | curses.A_BOLD
            for chunk in [line[i:i + dw] for i in range(0, len(line), dw)] or ['']:
                if row >= tree_h:
                    break
                try:
                    stdscr.addnstr(row, split, chunk, dw, attr)
                except curses.error:
                    pass
                row += 1
            li += 1
        x = 1
        for text, st in LEGEND:
            try:
                stdscr.addnstr(maxy - 2, x, text, max(0, maxx - 1 - x),
                               colors.get(st, 0) | curses.A_BOLD)
            except curses.error:
                pass
            x += len(text) + 3
        # context-sensitive action bar: only the keys valid for the SELECTED
        # node, named after the git operation each one performs
        node_sel = rows[sel][0] if sel < len(rows) else None
        act = (node_sel.get('act') or {}) if node_sel else {}
        kind = act.get('kind')
        st = node_sel['status'] if node_sel else 'info'
        if not act:
            here = '(no action here)'
        elif st == 'done':
            here = '(resolved)'
        elif st == 'conflict':
            here = '(conflict: edit the file, then r)'
        elif st == 'same':
            here = '(in sync)'
        elif kind == 'bundle':
            here = 't/m fetch (bundle)   o skip'
        elif kind == 'commits':
            here = ('d log   t/m merge --ff-only   o keep' if not act.get('ahead')
                    else 'd merge preview   t/m merge   o keep')
        else:   # patch / untracked / plainfile
            here = 'd diff   o checkout --ours   t checkout --theirs   m merge (3-way)'
        status = ' %d/%d │ %s │ u apply all   M merge all   r refresh   q quit' % (
            sel + 1, len(rows), here)
        try:
            stdscr.addnstr(maxy - 1, 0, status.ljust(maxx - 1), maxx - 1, curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()

    def confirm(stdscr, question):
        maxy, maxx = stdscr.getmaxyx()
        try:
            stdscr.addnstr(maxy - 1, 0, (' ' + question + '  [y/N]').ljust(maxx - 1),
                           maxx - 1, curses.A_REVERSE | curses.A_BOLD)
        except curses.error:
            pass
        stdscr.refresh()
        return stdscr.getch() in (ord('y'), ord('Y'))

    def busy(stdscr, msg):
        """Immediate status-line feedback before a slow apply/recompute —
        without this a confirmed action looks like a dead UI and users
        press y again (queued keys then leak into the tree)."""
        maxy, maxx = stdscr.getmaxyx()
        try:
            stdscr.addnstr(maxy - 1, 0, (' ⏳ ' + msg).ljust(maxx - 1),
                           maxx - 1, curses.A_REVERSE | curses.A_BOLD)
        except curses.error:
            pass
        stdscr.refresh()

    def reload_tree(old_root, sel, note):
        """Recompute the diff from disk; keep expansion, cursor and ✓/! marks."""
        if rebuild is None:
            return old_root, sel
        # remember expansion + the selected node's path
        expanded = {p for p, n in _walk_paths(old_root) if n['expanded']}
        sel_path, stack = None, []
        for i, (n, d) in enumerate(flatten(old_root)):
            stack = stack[:d] + [_norm_label(n)]
            if i == sel:
                sel_path = tuple(stack)
                break
        new_root = rebuild()
        new_root['expanded'] = True
        for p, n in _walk_paths(new_root):
            if p in expanded:
                n['expanded'] = True
            if ctx is not None:
                key = _node_key(n)
                if key and key in ctx.resolutions:
                    st, txt = ctx.resolutions[key]
                    n['status'] = st
                    n['label'] = f"{_norm_label(n)}  ({txt})"
        _annotate_worst(new_root)
        new_sel, stack = 0, []
        rows_new = flatten(new_root)
        for i, (n, d) in enumerate(rows_new):
            stack = stack[:d] + [_norm_label(n)]
            if tuple(stack) == sel_path:
                new_sel = i
                break
        else:
            new_sel = min(sel, len(rows_new) - 1)
        extra = [note]
        if ctx is not None and ctx.backup_dir:
            extra.append(f'backup: {ctx.backup_dir}')
        new_root['detail'] = extra + [''] + new_root['detail']
        return new_root, new_sel

    def tui(stdscr):
        nonlocal root
        curses.curs_set(0)
        colors = {}
        if curses.has_colors():
            curses.use_default_colors()
            for i, (st, col) in enumerate(
                    [('added', curses.COLOR_GREEN), ('removed', curses.COLOR_RED),
                     ('changed', curses.COLOR_YELLOW), ('info', curses.COLOR_CYAN),
                     ('conflict', curses.COLOR_MAGENTA)], 1):
                curses.init_pair(i, col, -1)
                colors[st] = curses.color_pair(i)
            colors['same'] = curses.A_DIM
            colors['done'] = colors['added'] | curses.A_DIM
            # Conflict colors. Terminal themes remap the base-16 palette (some
            # render red/yellow/magenta all orange-ish), but the 256-color cube
            # is not themable. Tree nodes: purple; in-file markers: true red.
            if curses.COLORS >= 256:
                curses.init_pair(9, 231, 196)   # white on pure red (cube) — markers
                curses.init_pair(10, 135, -1)   # purple foreground (cube) — tree
                colors['conflict'] = curses.color_pair(10)
            else:
                curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_RED)
            colors['confmark'] = curses.color_pair(9)
            # pre-merge collisions ('would conflict', still actionable via
            # o/t/m) share the conflict purple so they stand out from ~
            colors['clash'] = colors.get('conflict', 0)
            # local-ahead: yellow like ~, distinguished by its '^' glyph
            colors['ahead'] = colors.get('changed', 0)
        sel, tree_top, detail_off = 0, 0, 0
        while True:
            rows = flatten(root)
            sel = max(0, min(sel, len(rows) - 1))
            maxy, _ = stdscr.getmaxyx()
            tree_h = maxy - 2
            if sel < tree_top:
                tree_top = sel
            elif sel >= tree_top + tree_h:
                tree_top = sel - tree_h + 1
            draw(stdscr, rows, sel, tree_top, detail_off, colors)
            ch = stdscr.getch()
            node = rows[sel][0]
            if ch in (ord('q'), 27):
                return
            elif ch in (ord('j'), curses.KEY_DOWN):
                sel += 1; detail_off = 0
            elif ch in (ord('k'), curses.KEY_UP):
                sel -= 1; detail_off = 0
            elif ch in (ord('l'), curses.KEY_RIGHT):
                if node['children']:
                    node['expanded'] = True
            elif ch in (ord('\n'), curses.KEY_ENTER, ord(' ')):
                if node['children']:
                    node['expanded'] = not node['expanded']
            elif ch in (ord('h'), curses.KEY_LEFT):
                if node['expanded']:
                    node['expanded'] = False
                else:  # jump to parent
                    d = rows[sel][1]
                    for i in range(sel - 1, -1, -1):
                        if rows[i][1] < d:
                            sel = i; break
                detail_off = 0
            elif ch == ord('J') or ch == curses.KEY_NPAGE:
                detail_off += (10 if ch == ord('J') else tree_h)
                detail_off = max(0, min(detail_off, max(0, len(node['detail']) - 5)))
            elif ch == ord('K') or ch == curses.KEY_PPAGE:
                detail_off = max(0, detail_off - (10 if ch == ord('K') else tree_h))
            elif ch == ord('g'):
                sel = 0; detail_off = 0
            elif ch == ord('G'):
                sel = len(rows) - 1; detail_off = 0
            elif ch == ord('d'):
                node['detail'] = _merge_preview(node)
                detail_off = 0
            elif ch in (ord('o'), ord('t'), ord('m'), ord('M')) and ctx is not None:
                mode = {ord('o'): 'ours', ord('t'): 'theirs',
                        ord('m'): 'merge', ord('M'): 'merge'}[ch]
                target = root if ch == ord('M') else node
                what = 'EVERYTHING' if ch == ord('M') else target['label'].split('  (')[0]
                verb = {'ours': 'keep LOCAL side for', 'theirs': f'take {_SIDE.upper()} side for',
                        'merge': '3-way merge'}[mode]
                if mode == 'ours' or confirm(stdscr, f'{verb} {what}? files will be '
                                                     f'modified (backup kept)'):
                    busy(stdscr, f'applying {mode} + recomputing diff…')
                    n = apply_subtree(target, mode, ctx)
                    note = f'{mode}: {n} node(s) resolved'
                    if ctx.backup_dir:
                        note += f'   backup: {ctx.backup_dir}'
                    ctx.log(note)
                    root, sel = reload_tree(root, sel, note)
                    curses.flushinp()  # drop keys typed while the UI was busy
                detail_off = 0
            elif ch == ord('u') and ctx is not None and updater is not None:
                if confirm(stdscr, f'apply ALL non-conflicting {_SIDE} changes? '
                                   'conflicts stay untouched (backup kept)'):
                    busy(stdscr, 'applying all non-conflicting changes…')
                    try:
                        applied, skipped, kept = updater(ctx)[:3]
                    except Exception as e:
                        root, sel = reload_tree(root, sel, f'update failed: {e}')
                        curses.flushinp()
                        detail_off = 0
                        continue
                    note = ('update: %d applied, %d skipped (need o/t/m), '
                            '%d local-only kept' % (len(applied), len(skipped), len(kept)))
                    if ctx.backup_dir:
                        note += f'   backup: {ctx.backup_dir}'
                    ctx.log(note)
                    root, sel = reload_tree(root, sel, note)
                    if skipped:
                        root['detail'][1:1] = ['  still needs a human:'] + \
                            ['    ! %s: %s' % s for s in skipped[:15]]
                    curses.flushinp()
                detail_off = 0
            elif ch == ord('r'):
                busy(stdscr, 'recomputing diff from disk…')
                root, sel = reload_tree(root, sel, 'reloaded from disk')
                curses.flushinp()
                detail_off = 0
            elif ch == curses.KEY_RESIZE:
                pass

    _annotate_worst(root)
    try:
        curses.wrapper(tui)
    except KeyboardInterrupt:
        pass  # Ctrl-C quits like q, without a traceback


# ---------------------------------------------------------------------------
# --pull: fetch + fast-forward every repo (or a selected subset) against its
# OWN configured remote (a live git pull, distinct from the zip-vs-workspace
# diff above). A repo blocked on an interactive SSH/credential prompt gets
# the real command staged, unexecuted, in a tmux window -- never entered or
# guessed at here (this process has no passphrase to give it).
# push is intentionally NOT implemented yet; --push exists only as a
# discoverable stub so the CLI shape is stable when it lands.
# ---------------------------------------------------------------------------

_AUTH_FAIL_PATTERNS = (
    'permission denied',              # ssh publickey / password, either way
    'could not read username',        # https credential prompt, blocked
    'could not read password',
    'terminal prompts disabled',      # GIT_TERMINAL_PROMPT=0 tripped
    'host key verification failed',   # needs an interactive accept
    'authentication failed',
    'no supported authentication methods',
)

_PULL_STATUS_STYLE = {   # status -> (glyph, ansi color name)
    'up-to-date':  ('=', 'gray'),
    'pulled':      ('✓', 'bgreen'),   # auto-pulled -- the thing to see
    'fetched':     ('v', 'bgreen'),   # --fetch: new commits available, not merged
    'ahead':       ('^', 'yellow'),
    'diverged':    ('!', 'magenta'),
    'blocked':     ('!', 'magenta'),
    'auth':        ('A', 'bred'),          # needs a human at a terminal
    'error':       ('E', 'red'),
    'no-upstream': ('·', 'gray'),
    'skipped':     ('·', 'gray'),
}

_ANSI = {'red': '\x1b[31m', 'bred': '\x1b[1;31m', 'green': '\x1b[32m',
         'bgreen': '\x1b[1;32m', 'yellow': '\x1b[33m', 'magenta': '\x1b[35m',
         'cyan': '\x1b[36m', 'gray': '\x1b[90m', 'bold': '\x1b[1m',
         'reset': '\x1b[0m'}


def _color(text, name, enabled):
    if not enabled or not name:
        return text
    return f"{_ANSI.get(name, '')}{text}{_ANSI['reset']}"


def _classify_fetch_failure(stderr_text):
    """'auth' when the failure looks like a blocked interactive prompt
    (passphrase, credential, host-key-accept) -- the case a human at a
    terminal can resolve; 'error' for anything else (network, misconfigured
    remote, ...), which staging a tmux window would not fix."""
    low = (stderr_text or '').lower()
    return 'auth' if any(p in low for p in _AUTH_FAIL_PATTERNS) else 'error'


def _repo_upstream(repo_path):
    """(remote_name, upstream_ref) e.g. ('origin', 'origin/main'), or None
    if the current branch has no configured upstream (nothing to pull)."""
    r = subprocess.run(['git', '-C', repo_path, 'rev-parse',
                        '--abbrev-ref', '--symbolic-full-name', '@{u}'],
                       capture_output=True)
    if r.returncode != 0:
        return None
    upstream = r.stdout.decode('utf-8', 'replace').strip()
    remote = upstream.split('/', 1)[0] if '/' in upstream else upstream
    return remote, upstream


def _fetch_repo(repo_path, remote):
    """One `git fetch <remote>` with every interactive prompt disabled --
    BatchMode blocks ssh from falling back to a passphrase/host-key prompt,
    GIT_TERMINAL_PROMPT=0 blocks git's own credential prompt. This makes the
    probe and the real fetch the SAME operation: if it would have needed a
    human, it fails immediately here instead of hanging one open.
    Returns ('ok'|'auth'|'error', note)."""
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    pinned = subprocess.run(['git', '-C', repo_path, 'config', '--get',
                             'core.sshCommand'], capture_output=True)
    base_ssh = pinned.stdout.decode('utf-8', 'replace').strip() or 'ssh'
    env['GIT_SSH_COMMAND'] = f'{base_ssh} -o BatchMode=yes -o ConnectTimeout=10'
    try:
        r = subprocess.run(['git', '-C', repo_path, 'fetch', remote],
                           capture_output=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        return 'error', 'fetch timed out after 30s'
    if r.returncode == 0:
        return 'ok', ''
    stderr = r.stderr.decode('utf-8', 'replace').strip()
    kind = _classify_fetch_failure(stderr)
    return kind, (stderr.splitlines()[-1] if stderr else f'exit {r.returncode}')


def _tmux_available():
    return shutil.which('tmux') is not None


def _current_tmux_session():
    """Name of the tmux session THIS process runs inside, or None if not
    running under tmux at all."""
    if not os.environ.get('TMUX') or not _tmux_available():
        return None
    r = subprocess.run(['tmux', 'display-message', '-p', '#S'], capture_output=True)
    name = r.stdout.decode('utf-8', 'replace').strip()
    return name or None


def _sanitize_tmux_name(name, prefix='pull-', max_len=40):
    """'.' and ':' are NOT safe here even though tmux allows them in a raw
    window name: this name is later embedded in a 'session:window' TARGET
    string (see stage_tmux_command/pull_repo), where '.' is tmux's own
    pane-index separator -- 'sess:pull-foo.bar' parses as window 'pull-foo'
    pane 'bar' and fails with "can't find pane". Excluding both up front
    avoids that whole class of target-parsing bug."""
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_') or 'repo'
    return (prefix + safe)[:max_len]


def stage_tmux_command(command, window_name, session=None):
    """Stage `command` in a tmux window WITHOUT running it -- send-keys with
    no trailing Enter, exactly the pattern used throughout this project for
    anything needing a passphrase Claude cannot type. Reuses an existing
    window of the same name instead of clobbering it (it may already have a
    partially-typed passphrase in it).
    Returns (session, window, freshly_staged: bool, note)."""
    if not _tmux_available():
        return None, None, False, 'tmux is not installed -- cannot stage'
    session = session or _current_tmux_session() or 'rvcs-pull'
    window = _sanitize_tmux_name(window_name)
    has = subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True)
    if has.returncode != 0:
        # name the INITIAL window for this repo -- new-session always creates
        # one, and leaving it as a bare 'bash' just parks an empty first tab
        subprocess.run(['tmux', 'new-session', '-d', '-s', session, '-n', window],
                       capture_output=True)
    else:
        existing = subprocess.run(['tmux', 'list-windows', '-t', session, '-F', '#W'],
                                  capture_output=True).stdout.decode('utf-8', 'replace').splitlines()
        if window in existing:
            return session, window, False, f'already staged in {session}:{window}'
        subprocess.run(['tmux', 'new-window', '-d', '-t', session, '-n', window],
                       capture_output=True)
    subprocess.run(['tmux', 'send-keys', '-t', f'{session}:{window}', command],
                   capture_output=True)
    return session, window, True, f'staged in {session}:{window} — review and press Enter there'


def pull_repo(repo_path, rel, ctx=None, tmux_session=None, dry_run=False,
              fetch_only=False):
    """Pull ONE repo: fetch its upstream (non-interactively; auth needs stage
    a tmux window instead of failing silently), then fast-forward when it's
    now strictly behind (reusing the same dirty-preserving ff as --update-
    state -- local edits that already contain the incoming change survive
    uncommitted; anything else blocks and is reported, never guessed at).
    Diverged repos are reported, not auto-merged -- a real merge commit stays
    an explicit decision, same as the zip-diff side of this tool.
    fetch_only stops after the fetch: remote refs update, HEAD/branch/worktree
    are never touched, and the result just reports ahead/behind ('fetched'
    when new commits arrived). Returns {rel, status, note, tmux}."""
    up = _repo_upstream(repo_path)
    if up is None:
        return {'rel': rel, 'status': 'no-upstream',
                'note': 'current branch has no configured upstream', 'tmux': None}
    remote, upstream_ref = up

    if dry_run:
        # dry-run still fetches -- fetch never writes to the working tree or
        # branch, so it's safe and the ahead/behind numbers below need it
        pass
    kind, note = _fetch_repo(repo_path, remote)
    if kind == 'auth':
        if dry_run:
            return {'rel': rel, 'status': 'auth',
                    'note': f'needs authentication ({note}) -- would stage a tmux window',
                    'tmux': None}
        cmd = (f'git -C {shlex.quote(repo_path)} fetch {shlex.quote(remote)}'
               if fetch_only else f'git -C {shlex.quote(repo_path)} pull')
        session, window, _fresh, stage_note = stage_tmux_command(
            cmd, rel, session=tmux_session)
        return {'rel': rel, 'status': 'auth',
                'note': f'needs authentication ({note}) -- {stage_note}',
                'tmux': f'{session}:{window}' if session else None}
    if kind == 'error':
        return {'rel': rel, 'status': 'error', 'note': note, 'tmux': None}

    # fetch succeeded -- now purely local git, no network involved
    ahead = int(subprocess.run(['git', '-C', repo_path, 'rev-list', '--count',
                                f'{upstream_ref}..HEAD'],
                               capture_output=True).stdout.decode().strip() or 0)
    behind = int(subprocess.run(['git', '-C', repo_path, 'rev-list', '--count',
                                 f'HEAD..{upstream_ref}'],
                                capture_output=True).stdout.decode().strip() or 0)
    if not ahead and not behind:
        return {'rel': rel, 'status': 'up-to-date', 'note': '', 'tmux': None}
    if fetch_only:
        # report-only: the fetch already happened, nothing else may move
        if ahead and behind:
            st, n = 'diverged', (f'local ahead {ahead}, behind {behind} '
                                 f'(fetched, not merged)')
        elif behind:
            st, n = 'fetched', (f'{behind} new commit(s) on {upstream_ref} '
                                '(not merged -- fetch only)')
        else:
            st, n = 'ahead', f'{ahead} local commit(s) not on {upstream_ref}'
        return {'rel': rel, 'status': st, 'note': n, 'tmux': None}
    if ahead and behind:
        return {'rel': rel, 'status': 'diverged',
                'note': f'local ahead {ahead}, behind {behind} -- merge is a '
                       f'human decision: git -C {repo_path} merge {upstream_ref}',
                'tmux': None}
    if ahead and not behind:
        return {'rel': rel, 'status': 'ahead',
                'note': f'{ahead} local commit(s) not on {upstream_ref} (nothing to pull)',
                'tmux': None}
    # strictly behind -- fast-forward, dry_run just reports feasibility.
    # label/delete_hint: _ff_plan/_dirty_preserving_ff default to diff-TUI
    # wording ("zip", "t on the repo node") -- neither applies to a plain
    # pull, so name the remote and point at the actual next step here.
    ff_kwargs = dict(label='remote',
                     delete_hint='re-run with an explicit choice to confirm '
                                 'dropping them (the safe batch pull never does)')
    ok, plan_note, plan = _ff_plan(repo_path, upstream_ref, **ff_kwargs)
    if dry_run:
        status = 'pulled' if ok else 'blocked'
        return {'rel': rel, 'status': status,
                'note': (f'would fast-forward {behind} commit(s)' if ok
                        else f'ff blocked -- {plan_note}'), 'tmux': None}
    if not ok:
        return {'rel': rel, 'status': 'blocked', 'note': f'ff blocked -- {plan_note}',
               'tmux': None}
    done_ok, done_note = _dirty_preserving_ff(repo_path, upstream_ref, ctx=ctx, **ff_kwargs)
    if not done_ok:
        return {'rel': rel, 'status': 'blocked', 'note': f'ff failed -- {done_note}',
               'tmux': None}
    return {'rel': rel, 'status': 'pulled', 'note': done_note, 'tmux': None}


def pull_workspace(workspace, include_paths=None, repos_filter=None,
                   tmux_session=None, dry_run=False, fetch_only=False):
    """Pull every repo under workspace/src (or a `repos_filter` subset of
    them -- 'selected'). Prints a colored status line per repo as it's
    processed (auto-pulls in green, auth-needed in red -- the request this
    was built for) and a final summary. fetch_only = update remote refs and
    report ahead/behind, never touch branches/worktrees.
    Returns the list of result dicts."""
    source_folder = os.path.join(workspace, 'src')
    if not os.path.isdir(source_folder):
        source_folder = workspace
    color = sys.stdout.isatty() and not os.environ.get('NO_COLOR')

    repos = {}
    for p in find_git_repos(source_folder):
        rel = os.path.relpath(p, source_folder)
        if repo_in_include_paths(rel, include_paths):
            repos[rel] = p
    if repos_filter:
        missing = [r for r in repos_filter if r not in repos]
        for m in missing:
            print(_color(f'  ! {m}: not found under {source_folder}', 'red', color))
        repos = {r: p for r, p in repos.items() if r in repos_filter}

    if not repos:
        print('No repos to pull.')
        return []

    verb = 'Fetching' if fetch_only else 'Pulling'
    print(f"{verb} {len(repos)} repo(s){' (dry run)' if dry_run else ''}:")
    ctx = _MergeCtx(workspace)
    results = []
    for rel in sorted(repos):
        res = pull_repo(repos[rel], rel, ctx=ctx, tmux_session=tmux_session,
                        dry_run=dry_run, fetch_only=fetch_only)
        results.append(res)
        glyph, colname = _PULL_STATUS_STYLE.get(res['status'], ('?', None))
        line = f"  {glyph} {rel}"
        if res['note']:
            line += f"  -- {res['note']}"
        print(_color(line, colname, color))

    counts = {}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    summary = '  '.join(f"{_PULL_STATUS_STYLE.get(k, ('?', None))[0]} {k}: {v}"
                        for k, v in sorted(counts.items()))
    print(f"\n{summary}")
    if ctx.backup_dir:
        print(f"Backups of anything touched: {ctx.backup_dir}")
    auth_repos = [r for r in results if r['status'] == 'auth']
    if auth_repos:
        print(_color(f"\n{len(auth_repos)} repo(s) need authentication -- "
                     "review the staged command(s) in tmux and press Enter:",
                     'bred', color))
        for r in auth_repos:
            print(_color(f"  A {r['rel']}: {r['tmux']}", 'bred', color))
    return results


def main():
    """Main entry point for CLI."""
    global _debug

    parser = argparse.ArgumentParser(
        description='RVCS - ROS VCS Workspace Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rvcs                              Show status of current workspace
  rvcs ~/catkin_ws                  Show status of specified workspace
  rvcs --export-state               Export workspace to zip file
  rvcs --import-state ws.zip ~/new  Import workspace from zip
  rvcs --export-pipeline p.pipeline.yaml
                                    Export a pipeline's repos + tmuxinator configs
  rvcs --pipeline p.pipeline.yaml   Status of only the pipeline's repos
  rvcs --list-pipelines             Stored pipelines (~/.config/ros_vcs/pipeline)
  rvcs flipper_eval                 Same as --pipeline flipper_eval (stored NAME;
                                    workspace comes from the definition itself)
        """
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug messages')
    parser.add_argument('-j', '--json', action='store_true', help='Save results to JSON file')
    parser.add_argument('-c', '--compare', help='Compare with JSON file')
    parser.add_argument('--export-state', action='store_true', help='Export workspace state to zip')
    parser.add_argument('--export-pipeline', metavar='PIPELINE',
                        help='Export a pipeline slice incl. tmuxinator configs — a '
                             '.pipeline.yaml path, or a stored pipeline NAME '
                             '(see --list-pipelines)')
    parser.add_argument('--pipeline', metavar='PIPELINE',
                        help='Restrict status/compare to the repos of a pipeline — a '
                             '.pipeline.yaml path, or a stored pipeline NAME')
    parser.add_argument('--list-pipelines', action='store_true',
                        help='List pipelines stored under ~/.config/ros_vcs/pipeline '
                             '(each in its own git repo, one commit per imported snapshot)')
    parser.add_argument('--diff-state', metavar='ZIP', nargs='?', const='',
                        help='Diff an exported .workspace.zip/.pipeline.zip against a live '
                             'workspace, browsable as a tree (curses TUI on a terminal). '
                             'WITHOUT a zip: diff against each repo\'s upstream instead -- '
                             'unpushed commits, uncommitted files, and (after --fetch) '
                             'incoming remote commits. Put the flag last, or its value '
                             'swallows the next argument.')
    parser.add_argument('--no-tui', action='store_true',
                        help='With --diff-state: print the tree instead of the interactive TUI')
    parser.add_argument('--diff-json', metavar='FILE',
                        help='With --diff-state: write the diff tree as JSON to FILE '
                             "('-' for stdout) and exit — machine-readable, e.g. for Claude Code")
    parser.add_argument('--update-state', metavar='ZIP',
                        help='Apply all NON-conflicting changes from an export zip to the '
                             'workspace (3-way merges; conflicting files are left untouched '
                             'and reported — resolve those in the --diff-state TUI)')
    parser.add_argument('--dry-run', action='store_true',
                        help='With --update-state or --pull: only report what would be applied')
    parser.add_argument('--import-state', help='Import workspace from .workspace.zip/.pipeline.zip or .repos file')
    parser.add_argument('--state-file', help='State file with diffs (use with --import-state .repos)')
    parser.add_argument('--install-tmuxinator', action='store_true',
                        help='With --import-state: copy bundled tmuxinator configs into ~/.config/tmuxinator')
    parser.add_argument('--build', action='store_true',
                        help='With --import-state: run colcon build on the restored workspace')
    parser.add_argument('--build-args',
                        help='Extra arguments for --build, e.g. '
                             '"--cmake-args -DBUILD_TESTING=OFF"')
    parser.add_argument('--build-env', action='append', metavar='KEY=VALUE',
                        help='Environment override for --build (repeatable). nvcc is '
                             'added to PATH automatically when installed but not listed')
    parser.add_argument('--install-deps', action='store_true',
                        help='With --import-state: run rosdep install for the restored '
                             'workspace (needs sudo). Missing deps are reported either way')
    parser.add_argument('--no-path-rewrite', action='store_true',
                        help='With --import-state: restore the pipeline payload verbatim instead of '
                             'repointing the export-time workspace root at the import directory')
    parser.add_argument('--ignore', help='Ignore file with package names to exclude (status AND --export-state)')
    parser.add_argument('--name', help='Workspace name for --export-state output file (default: workspace dir name)')
    parser.add_argument('--pull', action='store_true',
                        help='Fetch + fast-forward every repo (or --repos, a selected subset) '
                             "against its OWN remote. A repo blocked on an interactive SSH/"
                             'credential prompt gets the real pull command staged (unexecuted) '
                             'in a tmux window instead of hanging or failing silently.')
    parser.add_argument('--fetch', action='store_true',
                        help='Like --pull but FETCH ONLY: update every repo\'s remote refs and '
                             'report ahead/behind -- branches and working trees are never '
                             'touched. Same --repos/pipeline slicing and tmux-staged auth.')
    parser.add_argument('--repos', metavar='REPO[,REPO...]',
                        help='With --pull/--fetch: only these repos (src-relative paths), instead of all')
    parser.add_argument('--tmux-session', metavar='NAME',
                        help='With --pull: tmux session to stage auth-blocked pulls into '
                             '(default: the session this process runs in, else "rvcs-pull")')
    parser.add_argument('--push', action='store_true',
                        help='Not implemented yet -- reserved for a future push counterpart to --pull')
    parser.add_argument('workspace', nargs='?', default=None, help='Workspace folder or output dir')
    args = parser.parse_args()

    # Set module debug flag
    _debug = args.debug

    # Expand tilde in paths
    if args.workspace:
        args.workspace = os.path.expanduser(args.workspace)
    if args.compare:
        args.compare = os.path.expanduser(args.compare)
    if args.import_state:
        args.import_state = os.path.expanduser(args.import_state)
    if args.state_file:
        args.state_file = os.path.expanduser(args.state_file)
    if args.ignore:
        args.ignore = os.path.expanduser(args.ignore)
    # --list-pipelines: enumerate the canonical store, newest activity first
    if args.list_pipelines:
        import subprocess
        names = list_pipeline_names()
        if not names:
            print(f"No pipelines stored under {PIPELINE_CONFIG_DIR}")
            exit(0)
        print(f"Pipelines in {PIPELINE_CONFIG_DIR} (each its own git repo):\n")
        for n in names:
            d = _pipeline_repo_dir(n)
            last = subprocess.run(
                ['git', '-C', d, 'log', '-1',
                 '--format=%h  %ad  %s', '--date=format:%Y-%m-%d %H:%M'],
                capture_output=True, text=True).stdout.strip()
            count = subprocess.run(['git', '-C', d, 'rev-list', '--count', 'HEAD'],
                                   capture_output=True, text=True).stdout.strip()
            ws = ''
            try:
                ws = load_pipeline(_pipeline_file_in_repo(n)).get('workspace') or ''
            except Exception:
                pass
            print(f"  {n}   ({count} version(s)" + (f", workspace {ws}" if ws else '') + ')')
            print(f"      {last}")
        print(f"\nUse a NAME directly:  rvcs <name>   rvcs --pipeline <name> ...   "
              f"rvcs --export-pipeline <name>")
        exit(0)

    # --pipeline/--export-pipeline accept a stored NAME as well as a path
    try:
        if args.export_pipeline:
            args.export_pipeline = resolve_pipeline_arg(
                os.path.expanduser(args.export_pipeline))
        if args.pipeline:
            args.pipeline = resolve_pipeline_arg(os.path.expanduser(args.pipeline))
    except FileNotFoundError as e:
        print(e)
        exit(1)

    # Bare-name shortcut: `rvcs flipper_eval [...]` == `--pipeline flipper_eval`
    # (workspace then comes from the definition's own 'workspace:' key). Only
    # for a token with no path separator that is not an existing directory and
    # matches a stored pipeline — and never for --import-state, whose
    # positional is an output directory that may not exist yet.
    if args.workspace and not args.import_state and not args.pipeline \
            and os.sep not in args.workspace and not os.path.isdir(args.workspace):
        if args.workspace in list_pipeline_names():
            args.pipeline = pipeline_source_path(args.workspace)
            args.workspace = None
        else:
            # a bare token that is neither a directory nor a stored pipeline
            # can only produce an empty status table -- fail loudly instead
            known = list_pipeline_names()
            print(f"'{args.workspace}' is neither a directory nor a stored pipeline.")
            print('Known pipelines: ' + (', '.join(known) if known else '(none)')
                  + f'   (store: {PIPELINE_CONFIG_DIR})')
            exit(1)

    if args.push:
        print("--push is not implemented yet. --pull (fetch + fast-forward, with "
              "tmux-staged auth) is available; a push counterpart is planned but "
              "not built -- pushing stays a manual `git push` for now.")
        exit(1)

    # Handle --pull / --fetch mode
    if args.pull or args.fetch:
        include = None
        workspace = args.workspace
        if args.pipeline:
            p = load_pipeline(os.path.expanduser(args.pipeline))
            include = p['repos'] or None
            if workspace is None:
                workspace = p.get('workspace')
        workspace = workspace or os.getcwd()
        repos_filter = None
        if args.repos:
            repos_filter = {r.strip() for r in args.repos.split(',') if r.strip()}
        results = pull_workspace(workspace, include_paths=include,
                                 repos_filter=repos_filter,
                                 tmux_session=args.tmux_session, dry_run=args.dry_run,
                                 fetch_only=args.fetch)
        exit(1 if any(r['status'] == 'error' for r in results) else 0)

    # Handle --update-state mode
    if args.update_state:
        include = None
        workspace = args.workspace
        if args.pipeline:
            p = load_pipeline(os.path.expanduser(args.pipeline))
            include = p['repos'] or None
            if workspace is None:
                workspace = p.get('workspace')
        workspace = workspace or os.getcwd()
        applied, skipped, kept, uctx = update_workspace_state(
            os.path.expanduser(args.update_state), workspace,
            include_paths=include, dry_run=args.dry_run)
        verb = 'would apply' if args.dry_run else 'applied'
        print(f"\n{verb.capitalize()} ({len(applied)}):")
        for a in applied:
            print(f"  + {a}")
        if skipped:
            print(f"\nSkipped — needs a human ({len(skipped)}):")
            for s, why in skipped:
                print(f"  ! {s}: {why}")
        if kept:
            print(f"\nLocal-only changes kept as-is: {len(kept)}")
        if uctx.backup_dir:
            print(f"\nBackups of every modified file: {uctx.backup_dir}")
        if skipped:
            print("Resolve the skipped items interactively: "
                  f"rvcs --diff-state {args.update_state} {workspace}")
        exit(0)

    # Handle --diff-state mode
    if args.diff_state is not None:
        if args.compare or args.json or args.import_state or args.export_state or args.export_pipeline:
            print("Cannot use other options with --diff-state")
            exit(1)
        # `rvcs --diff-state flipper_eval` -- nargs='?' swallowed the pipeline
        # name as the zip value; recognize a stored name and shift it over
        if args.diff_state and not os.path.exists(os.path.expanduser(args.diff_state)) \
                and os.sep not in args.diff_state and not args.pipeline \
                and args.diff_state in list_pipeline_names():
            args.pipeline = pipeline_source_path(args.diff_state)
            args.diff_state = ''
        include = None
        workspace = args.workspace
        if args.pipeline:
            p = load_pipeline(os.path.expanduser(args.pipeline))
            include = p['repos'] or None
            if workspace is None:
                workspace = p.get('workspace')
        workspace = workspace or os.getcwd()
        tmp_state = None
        if args.diff_state:
            zip_arg = os.path.expanduser(args.diff_state)
        else:
            zip_arg = make_upstream_state_zip(workspace, include_paths=include)
            tmp_state = os.path.dirname(zip_arg)
            set_side_label('upstream')   # git's own name for the tracking ref:
            # the locally-fetched origin/<branch>, not the live network remote
            print('No zip given -- diffing against each repo\'s upstream '
                  '(run --fetch first for fresh remote refs).')
        root = compute_state_diff(zip_arg, workspace, include_paths=include)
        if args.diff_json:
            payload = json.dumps(diff_tree_to_json(root, zip_arg, workspace), indent=2)
            if args.diff_json == '-':
                print(payload)
            else:
                with open(os.path.expanduser(args.diff_json), 'w') as f:
                    f.write(payload + '\n')
                print(f"Diff tree written to {args.diff_json}")
        elif args.no_tui or not sys.stdout.isatty():
            print_state_diff(root, detail=args.debug)
        else:
            run_diff_tui(root, ctx=_MergeCtx(workspace),
                         rebuild=lambda: compute_state_diff(zip_arg, workspace,
                                                            include_paths=include),
                         updater=lambda c: update_workspace_state(
                             zip_arg, workspace, include_paths=include, ctx=c))
        if tmp_state:
            shutil.rmtree(tmp_state, ignore_errors=True)
        exit(0)

    # Handle --export-pipeline mode
    if args.export_pipeline:
        if args.compare or args.json or args.import_state or args.export_state:
            print("Cannot use other options with --export-pipeline")
            exit(1)
        export_pipeline_state(args.export_pipeline, workspace_path=args.workspace)
        exit(0)

    # Handle --export-state mode
    if args.export_state:
        if args.compare or args.json or args.import_state:
            print("Cannot use other options with --export-state")
            exit(1)
        if args.pipeline:
            # `rvcs <name> --export-state` / `--pipeline X --export-state`:
            # exporting "the state" of a pipeline IS a pipeline export — slice
            # to its repos and carry the definition + tmuxinator configs.
            # Without this, the bare-name shortcut had nulled args.workspace
            # and this would silently export the CWD as a full workspace.
            export_pipeline_state(args.pipeline, workspace_path=args.workspace)
            exit(0)
        workspace = args.workspace if args.workspace else os.getcwd()
        ignore = load_ignore_packages(args.ignore) if args.ignore else None
        if ignore:
            print(f"Excluding (with their subtrees): {', '.join(sorted(ignore))}")
        export_workspace_state(workspace, workspace_name=args.name, ignore_packages=ignore)
        exit(0)

    # Handle --import-state mode
    if args.import_state:
        if args.compare or args.json or args.export_state:
            print("Cannot use other options with --import-state")
            exit(1)
        output_dir = args.workspace if args.workspace else os.getcwd()
        # Check if output directory exists and is not empty
        if os.path.exists(output_dir) and os.listdir(output_dir):
            print(f"Error: Output directory '{output_dir}' exists and is not empty")
            exit(1)
        import_workspace_state(args.import_state, output_dir, args.state_file,
                               install_tmuxinator=args.install_tmuxinator,
                               rewrite_paths=not args.no_path_rewrite,
                               install_deps=args.install_deps,
                               build=args.build, build_args=args.build_args,
                               build_env=args.build_env)
        exit(0)

    if args.compare and args.json:
        print("Cannot use -j with -c")
        exit(1)

    # Pipeline filter for status/compare: restrict to the pipeline's repos and
    # default the workspace to the one the pipeline names
    pipeline = None
    include_paths = None
    if args.pipeline:
        pipeline = load_pipeline(args.pipeline)
        include_paths = pipeline['repos'] or None
        if args.workspace is None and pipeline.get('workspace'):
            args.workspace = pipeline['workspace']
        print(f"Pipeline: {pipeline['name']}")

    if args.workspace is None:
        args.workspace = os.getcwd()
        catkin_workspace = find_catkin_workspace(args.workspace)
        if catkin_workspace is not None:
            print(f"Found catkin workspace at path: {catkin_workspace}")
            args.workspace = catkin_workspace
        else:
            print("No .catkin_tools folder found in current directory or its parents.")
            print("Using current directory as workspace.")

    # Check if workspace has a src folder, otherwise use workspace directly
    source_folder = os.path.join(args.workspace, "src")
    if not os.path.exists(source_folder):
        source_folder = args.workspace
        print(f"No src folder found, using workspace directory: {source_folder}")

    # Load ignore packages
    ignore_packages = load_ignore_packages(args.ignore)
    if ignore_packages:
        print(f"Ignoring packages: {', '.join(sorted(ignore_packages))}")

    print(f"Searching in folder {source_folder}")
    print("=" * 64)

    repo_paths = [p for p in find_git_repos(source_folder, ignore_packages)
                  if repo_in_include_paths(os.path.relpath(p, source_folder), include_paths)]

    results = []
    colors = []
    if args.compare:
        with open(args.compare, 'r') as f:
            json_data = json.load(f)

        # Create a dict for quick lookup of remote data - key by package name AND url
        remote_dict = {}
        for pkg in json_data:
            key = (pkg['Package'], pkg['Url'])
            remote_dict[key] = pkg

        # Process local packages
        processed_remote_keys = set()
        for folder_path in repo_paths:
                result = get_git_info(folder_path)
                if result:
                    rel = os.path.relpath(folder_path, source_folder)
                    if os.sep in rel:
                        result[0] = rel
                    package_name = result[0]
                    local_branch = result[1]
                    local_hash = result[2]
                    local_dirty = result[3]
                    local_url = result[5]

                    # Look for matching package by name and URL
                    remote_match = None
                    for key, remote_pkg in remote_dict.items():
                        if key[0] == package_name and key[1] == local_url:
                            remote_match = remote_pkg
                            processed_remote_keys.add(key)
                            break

                    # If no exact match, look for same package name with different URL
                    if not remote_match:
                        for key, remote_pkg in remote_dict.items():
                            if key[0] == package_name:
                                # Found same package with different URL - show both as separate entries
                                row_data = [package_name]
                                row_colors = ['\033[35m']  # magenta for URL mismatch
                                row_data.extend([local_branch, '-', local_hash, '-', local_dirty, '-', local_url])
                                row_colors.extend(['\033[35m'] * 7)
                                results.append(row_data)
                                colors.append(row_colors)

                                remote_dirty = remote_pkg.get('Local', 'no')
                                row_data = [package_name]
                                row_colors = ['\033[35m']
                                row_data.extend(['-', remote_pkg['Branch'], '-', remote_pkg['Local Hash'], '-', remote_dirty, remote_pkg['Url']])
                                row_colors.extend(['\033[35m'] * 7)
                                results.append(row_data)
                                colors.append(row_colors)

                                processed_remote_keys.add(key)
                                remote_match = "handled"
                                break

                    if remote_match and remote_match != "handled":
                        # Package exists in both with same URL
                        remote_branch = remote_match['Branch']
                        remote_hash = remote_match['Local Hash']
                        remote_dirty = remote_match.get('Local', 'no')
                        remote_url = remote_match['Url']

                        row_data = [package_name]
                        row_colors = []

                        # Determine overall color based on changes and differences
                        has_diff = (local_branch != remote_branch or local_hash != remote_hash)
                        both_dirty = (local_dirty == 'yes' and remote_dirty == 'yes')
                        one_dirty = (local_dirty == 'yes' or remote_dirty == 'yes') and not both_dirty

                        if has_diff:
                            row_colors.append('\033[33m')  # yellow
                        elif both_dirty:
                            row_colors.append('\033[38;5;196m')  # bright red
                        elif one_dirty:
                            row_colors.append('\033[38;5;208m')  # orange
                        else:
                            row_colors.append('\033[32m')  # green

                        # Local branch
                        row_data.append(local_branch)
                        row_colors.append('\033[32m' if local_branch == remote_branch else '\033[39m')

                        # Remote branch
                        row_data.append(remote_branch)
                        row_colors.append('\033[32m' if local_branch == remote_branch else '\033[31m')

                        # Local hash
                        row_data.append(local_hash)
                        row_colors.append('\033[32m' if local_hash == remote_hash else '\033[39m')

                        # Remote hash
                        row_data.append(remote_hash)
                        row_colors.append('\033[32m' if local_hash == remote_hash else '\033[31m')

                        # Local changes
                        row_data.append(local_dirty)
                        row_colors.append('\033[38;5;208m' if local_dirty == 'yes' else '\033[32m')

                        # Remote changes
                        row_data.append(remote_dirty)
                        row_colors.append('\033[38;5;208m' if remote_dirty == 'yes' else '\033[32m')

                        # URL
                        row_data.append(local_url)
                        row_colors.append('\033[32m')

                        results.append(row_data)
                        colors.append(row_colors)

                    elif remote_match != "handled":
                        # Local only - all blue
                        row_data = [package_name]
                        row_colors = ['\033[34m']
                        row_data.extend([local_branch, '-', local_hash, '-', local_dirty, '-', local_url])
                        row_colors.extend(['\033[34m'] * 7)
                        results.append(row_data)
                        colors.append(row_colors)

        # Process remaining remote-only packages
        for key, remote_pkg in remote_dict.items():
            if key not in processed_remote_keys and remote_pkg['Package'] not in ignore_packages:
                remote_dirty = remote_pkg.get('Local', 'no')
                row_data = [remote_pkg['Package']]
                row_colors = ['\033[36m']  # cyan
                row_data.extend(['-', remote_pkg['Branch'], '-', remote_pkg['Local Hash'], '-', remote_dirty, remote_pkg['Url']])
                row_colors.extend(['\033[36m'] * 7)
                results.append(row_data)
                colors.append(row_colors)

        headers = ["Package", "Local Branch", "Remote Branch", "Local Hash", "Remote Hash", "Local Changes", "Remote Changes", "URL"]
        print("\033[32mGreen: Clean matching\033[39m | \033[33mYellow: Different branch/hash\033[39m | \033[38;5;208mOrange: One side dirty\033[39m")
        print("\033[38;5;196mRed: Both dirty\033[39m | \033[34mBlue: Local only\033[39m | \033[36mCyan: Remote only\033[39m | \033[35mMagenta: URL mismatch\033[39m")
        colorized_results = []
        for i, row in enumerate(results):
            colorized_row = []
            for j, value in enumerate(row):
                display = truncate_name(value) if j == 0 else value
                colorized_row.append(f"{colors[i][j]}{display}\033[39m")
            colorized_results.append(colorized_row)
        print(tabulate(colorized_results, headers=headers, tablefmt="simple"))

    else:
        for folder_path in repo_paths:
                result = get_git_info(folder_path)
                if result:
                    rel = os.path.relpath(folder_path, source_folder)
                    if os.sep in rel:
                        result[0] = rel
                    row_colors = []
                    if result[3] == 'yes' and result[4] != 'no':
                        row_colors = ['\033[38;5;196m'] * len(result)
                    elif result[3] == 'yes':
                        row_colors = ['\033[38;5;208m'] * len(result)
                    elif result[4] != 'no':
                        row_colors = ['\033[38;5;70m'] * len(result)
                    else:
                        row_colors = [''] * len(result)
                    results.append(result)
                    colors.append(row_colors)

        headers = ["Package", "Branch", "Hash", "Local", "Remote", "Url"]
        colorized_results = []
        for i, row in enumerate(results):
            colorized_row = []
            for j, value in enumerate(row):
                display = truncate_name(value) if j == 0 else value
                colorized_row.append(f"{colors[i][j]}{display}\033[39m")
            colorized_results.append(colorized_row)
        print(tabulate(colorized_results, headers=headers, tablefmt="simple"))

    if args.json:
        dict_results = []
        for row in results:
            dict_results.append({
                'package': row[0],
                'branch': row[1],
                'hash': row[2],
                'local_changes': row[3],
                'remote_changes': row[4],
                'url': row[5]
            })
        ws_name = os.path.basename(args.workspace)
        json_file_path = export_to_json(dict_results, workspace_name=ws_name)
        print(f"Results saved to JSON file: {json_file_path}")


if __name__ == "__main__":
    main()
