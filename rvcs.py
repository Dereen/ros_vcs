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
import os
import shutil
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

__version__ = "1.1.0"

# Module-level debug flag (set by CLI)
_debug = False


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

        # Remote changes (for comparison tool, means changes on remote PC, not git remote)
        remote_changes = "no"

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

        # Get staged changes (ensure trailing newline for git apply)
        staged_diff = repo.git.diff('--cached')
        if staged_diff:
            if not staged_diff.endswith('\n'):
                staged_diff += '\n'
            result['staged_diff'] = staged_diff

        # Get unstaged changes (ensure trailing newline for git apply)
        unstaged_diff = repo.git.diff()
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

    # Collect diffs for dirty repos
    state_data = {
        'workspace_name': ws_name,
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

    shutil.rmtree(bundle_tmp, ignore_errors=True)

    dirty_count = len(state_data['dirty_repos'])
    print(f"\nExported to: {zip_file}")
    print(f"  Repositories: {repos_content.count('type:')}")
    print(f"  With uncommitted changes: {dirty_count}")
    if bundles:
        print(f"  Bundled (not on any remote): {len(bundles)}")

    return zip_file


def export_pipeline_state(pipeline_file, workspace_path=None, output_dir=None):
    """
    Export one pipeline's slice of a workspace to a .pipeline.zip.

    The zip contains everything a plain workspace export has (workspace.repos
    manifest + workspace.state.yaml with uncommitted changes), restricted to the
    repos the pipeline lists, PLUS:
      pipeline.yaml       - the pipeline definition itself
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

    extra_files = {'pipeline.yaml': open(pipeline_file, 'rb').read()}

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


def import_workspace_state(input_file, output_dir, state_file=None, install_tmuxinator=False):
    """
    Import workspace state using vcstool and optionally apply diffs.

    Pipeline zips (.pipeline.zip) additionally restore:
      tmuxinator/* -> <output_dir>/tmuxinator/ (and, with install_tmuxinator,
                      copied into ~/.config/tmuxinator/)
      extra/*      -> <output_dir>/ (workspace-relative non-repo paths)
      pipeline.yaml-> <output_dir>/

    Args:
        input_file: Path to .workspace.zip/.pipeline.zip, .repos file, or directory
        output_dir: Directory where repositories will be cloned
        state_file: Optional path to .state.yaml file with diffs (ignored if zip provided)
        install_tmuxinator: Also copy bundled tmuxinator configs to ~/.config/tmuxinator

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

    # Handle zip file input
    if input_file.endswith('.zip'):
        print(f"Extracting workspace from {input_file}...")
        with zipfile.ZipFile(input_file, 'r') as zf:
            repos_content = zf.read('workspace.repos').decode('utf-8')
            if 'workspace.state.yaml' in zf.namelist():
                state_content = zf.read('workspace.state.yaml').decode('utf-8')
                state_data = yaml.safe_load(state_content)
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
                                or n.startswith('tmuxinator/')
                                or n.startswith('extra/')]
            if pipeline_members:
                # Restore pipeline payload into the new workspace. extra/<rel>
                # entries land at their workspace-relative path; tmuxinator
                # configs land in <output_dir>/tmuxinator/ so nothing outside
                # the target is touched without install_tmuxinator.
                for member in pipeline_members:
                    if member.endswith('/'):
                        continue
                    if member.startswith('extra/'):
                        dest = os.path.join(output_dir, os.path.relpath(member, 'extra'))
                    else:
                        dest = os.path.join(output_dir, member)
                    os.makedirs(os.path.dirname(dest) or output_dir, exist_ok=True)
                    with open(dest, 'wb') as out:
                        out.write(zf.read(member))
                tmux_files = [n for n in pipeline_members if n.startswith('tmuxinator/')]
                if tmux_files:
                    print(f"  Restored tmuxinator configs to {os.path.join(output_dir, 'tmuxinator')}/")
                    if install_tmuxinator:
                        tmux_config_dir = os.path.expanduser('~/.config/tmuxinator')
                        os.makedirs(tmux_config_dir, exist_ok=True)
                        for n in tmux_files:
                            dest = os.path.join(tmux_config_dir, os.path.basename(n))
                            with open(dest, 'wb') as out:
                                out.write(zf.read(n))
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
        'bundle_failed': []
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
        """
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug messages')
    parser.add_argument('-j', '--json', action='store_true', help='Save results to JSON file')
    parser.add_argument('-c', '--compare', help='Compare with JSON file')
    parser.add_argument('--export-state', action='store_true', help='Export workspace state to zip')
    parser.add_argument('--export-pipeline', metavar='PIPELINE_FILE',
                        help='Export a pipeline slice (.pipeline.yaml) incl. tmuxinator configs')
    parser.add_argument('--pipeline', metavar='PIPELINE_FILE',
                        help='Restrict status/compare to the repos of a pipeline definition')
    parser.add_argument('--import-state', help='Import workspace from .workspace.zip/.pipeline.zip or .repos file')
    parser.add_argument('--state-file', help='State file with diffs (use with --import-state .repos)')
    parser.add_argument('--install-tmuxinator', action='store_true',
                        help='With --import-state: copy bundled tmuxinator configs into ~/.config/tmuxinator')
    parser.add_argument('--ignore', help='Ignore file with package names to exclude (status AND --export-state)')
    parser.add_argument('--name', help='Workspace name for --export-state output file (default: workspace dir name)')
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
    if args.export_pipeline:
        args.export_pipeline = os.path.expanduser(args.export_pipeline)
    if args.pipeline:
        args.pipeline = os.path.expanduser(args.pipeline)

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
                               install_tmuxinator=args.install_tmuxinator)
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
                colorized_row.append(f"{colors[i][j]}{value}\033[39m")
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
                    if result[3] == 'yes' and result[4] == 'yes':
                        row_colors = ['\033[38;5;196m'] * len(result)
                    elif result[3] == 'yes':
                        row_colors = ['\033[38;5;208m'] * len(result)
                    elif result[4] == 'yes':
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
                colorized_row.append(f"{colors[i][j]}{value}\033[39m")
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
