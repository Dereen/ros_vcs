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
import json
import yaml
import zipfile
from io import StringIO
from tabulate import tabulate
from datetime import datetime
import git

# vcstool imports
from vcstool.commands.export import main as vcs_export
from vcstool.commands.import_ import main as vcs_import

__version__ = "1.0.0"

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

    if not os.path.isdir(os.path.join(folder, '.git')):
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
    for folder in os.listdir(source_folder):
        folder_path = os.path.join(source_folder, folder)
        if os.path.isdir(folder_path) and folder not in ignore_packages:
            info = get_git_info_dict(folder_path, debug=debug)
            if info:
                results.append(info)

    return results


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


def export_vcs_repos(workspace_path, output_file=None, exact=True):
    """
    Export workspace repositories using vcstool format (YAML).

    Args:
        workspace_path: Path to the workspace directory
        output_file: Optional output file path. If None, returns YAML string.
        exact: If True, export exact commit hashes instead of branch names

    Returns:
        Path to output file if output_file provided, otherwise YAML string
    """
    # Determine source folder
    source_folder = os.path.join(workspace_path, "src")
    if not os.path.exists(source_folder):
        source_folder = workspace_path

    # Use vcstool export
    stdout_capture = StringIO()
    args = ['--exact'] if exact else []
    args.append(source_folder)

    vcs_export(args=args, stdout=stdout_capture)
    yaml_content = stdout_capture.getvalue()

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

        # Get staged changes
        staged_diff = repo.git.diff('--cached')
        if staged_diff:
            result['staged_diff'] = staged_diff

        # Get unstaged changes
        unstaged_diff = repo.git.diff()
        if unstaged_diff:
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


def export_workspace_state(workspace_path, output_dir=None, workspace_name=None):
    """
    Export complete workspace state to a zip file containing vcstool repos and diffs.

    Args:
        workspace_path: Path to the workspace directory
        output_dir: Directory for output zip file. If None, uses current directory.
        workspace_name: Name for output file (default: basename of workspace)

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

    # Get vcstool repos content
    repos_content = export_vcs_repos(workspace_path, exact=True)

    # Collect diffs for dirty repos
    state_data = {
        'workspace_name': ws_name,
        'export_date': date_str,
        'dirty_repos': {}
    }

    # Find all git repos and check for dirty state
    for root, dirs, files in os.walk(source_folder):
        # Skip .git directories
        if '.git' in dirs:
            dirs.remove('.git')

        git_dir = os.path.join(root, '.git')
        if os.path.isdir(git_dir):
            rel_path = os.path.relpath(root, source_folder)
            diff_data = get_repo_diff(root)
            if diff_data:
                state_data['dirty_repos'][rel_path] = diff_data
                print(f"  Captured changes for: {rel_path}")

    # Create zip file
    zip_file = os.path.join(output_dir, f"{ws_name}_{date_str}.workspace.zip")
    with zipfile.ZipFile(zip_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add repos file
        zf.writestr('workspace.repos', repos_content)
        # Add state file
        state_content = yaml.dump(state_data, default_flow_style=False, allow_unicode=True)
        zf.writestr('workspace.state.yaml', state_content)

    dirty_count = len(state_data['dirty_repos'])
    print(f"\nExported to: {zip_file}")
    print(f"  Repositories: {repos_content.count('type:')}")
    print(f"  With uncommitted changes: {dirty_count}")

    return zip_file


def import_workspace_state(input_file, output_dir, state_file=None):
    """
    Import workspace state using vcstool and optionally apply diffs.

    Args:
        input_file: Path to .workspace.zip, .repos file, or directory containing them
        output_dir: Directory where repositories will be cloned
        state_file: Optional path to .state.yaml file with diffs (ignored if zip provided)

    Returns:
        Dictionary with 'import_return_code', 'patched', 'patch_failed'
    """
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    repos_content = None
    state_data = None

    # Handle zip file input
    if input_file.endswith('.zip'):
        print(f"Extracting workspace from {input_file}...")
        with zipfile.ZipFile(input_file, 'r') as zf:
            repos_content = zf.read('workspace.repos').decode('utf-8')
            if 'workspace.state.yaml' in zf.namelist():
                state_content = zf.read('workspace.state.yaml').decode('utf-8')
                state_data = yaml.safe_load(state_content)
    else:
        # Handle .repos file input
        print(f"Importing repositories from {input_file}...")
        with open(input_file, 'r') as f:
            repos_content = f.read()

        # Load state file if provided
        if state_file and os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state_data = yaml.safe_load(f)

    # Use vcstool import
    stdin_capture = StringIO(repos_content)
    stdout_capture = StringIO()

    rc = vcs_import(args=[output_dir], stdin=stdin_capture, stdout=stdout_capture)

    import_output = stdout_capture.getvalue()
    print(import_output)

    results = {
        'import_return_code': rc,
        'patched': [],
        'patch_failed': []
    }

    # Apply diffs if state data available
    if state_data and state_data.get('dirty_repos'):
        print(f"\nApplying uncommitted changes...")

        for rel_path, diff_data in state_data.get('dirty_repos', {}).items():
            repo_path = os.path.join(output_dir, rel_path)

            if not os.path.isdir(repo_path):
                print(f"  Skipping {rel_path}: directory not found")
                results['patch_failed'].append(rel_path)
                continue

            try:
                repo = git.Repo(repo_path)
                applied = False

                # Apply staged diff (use --index to stage the changes)
                if diff_data.get('staged_diff'):
                    repo.git.apply('--index', input=diff_data['staged_diff'])
                    applied = True

                # Apply unstaged diff
                if diff_data.get('unstaged_diff'):
                    repo.git.apply(input=diff_data['unstaged_diff'])
                    applied = True

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

    print("\n" + "=" * 60)
    print(f"Import summary:")
    print(f"  vcstool import return code: {rc}")
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
        """
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug messages')
    parser.add_argument('-j', '--json', action='store_true', help='Save results to JSON file')
    parser.add_argument('-c', '--compare', help='Compare with JSON file')
    parser.add_argument('--export-state', action='store_true', help='Export workspace state to zip')
    parser.add_argument('--import-state', help='Import workspace from .workspace.zip or .repos file')
    parser.add_argument('--state-file', help='State file with diffs (use with --import-state .repos)')
    parser.add_argument('--ignore', help='Ignore file with package names to exclude')
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

    # Handle --export-state mode
    if args.export_state:
        if args.compare or args.json or args.import_state:
            print("Cannot use other options with --export-state")
            exit(1)
        workspace = args.workspace if args.workspace else os.getcwd()
        export_workspace_state(workspace)
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
        import_workspace_state(args.import_state, output_dir, args.state_file)
        exit(0)

    if args.compare and args.json:
        print("Cannot use -j with -c")
        exit(1)

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
        for folder in os.listdir(source_folder):
            folder_path = os.path.join(source_folder, folder)
            if os.path.isdir(folder_path) and folder not in ignore_packages:
                result = get_git_info(folder_path)
                if result:
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
        for folder in os.listdir(source_folder):
            folder_path = os.path.join(source_folder, folder)
            if os.path.isdir(folder_path) and folder not in ignore_packages:
                result = get_git_info(folder_path)
                if result:
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
