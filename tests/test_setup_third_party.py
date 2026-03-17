from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

HELPER_BASE = """def get_parser():  # pylint: disable=too-many-statements,too-many-locals
  build_fuzzers_parser = subparsers.add_parser(
      'build_fuzzers', help='Build fuzzers for a project.')
  _add_architecture_args(build_fuzzers_parser)
  _add_engine_args(build_fuzzers_parser)
  _add_sanitizer_args(build_fuzzers_parser)
  _add_environment_args(build_fuzzers_parser)
  _add_external_project_args(build_fuzzers_parser)
  build_fuzzers_parser.add_argument('project')
  build_fuzzers_parser.add_argument('source_path',
                                    help='path of local source',
                                    nargs='?')
  build_fuzzers_parser.add_argument('--mount_path',
                                    dest='mount_path',
                                    help='path to mount local source in '
                                    '(defaults to WORKDIR)')
  build_fuzzers_parser.add_argument('--clean',
                                    dest='clean',
                                    action='store_true',
                                    help='clean existing artifacts.')
  build_fuzzers_parser.add_argument('--no-clean',
                                    dest='clean',
                                    action='store_false',
                                    help='do not clean existing artifacts '
                                    '(default).')
  build_fuzzers_parser.set_defaults(clean=False)

  fuzzbench_build_fuzzers_parser = subparsers.add_parser(
      'fuzzbench_build_fuzzers')


def _check_fuzzer_exists(project, fuzzer_name, args, architecture='x86_64'):
  \"\"\"Checks if a fuzzer exists.\"\"\"
  platform = 'linux/arm64' if architecture == 'aarch64' else 'linux/amd64'
  command = ['docker', 'run', '--rm', '--platform', platform]
  command.extend(['-v', '%s:/out:z' % project.out])
  command.append(_get_base_runner_image(args))

  command.extend(['/bin/bash', '-c', 'test -f /out/%s' % fuzzer_name])

  try:
    subprocess.check_call(command)
  except subprocess.CalledProcessError:
    logger.error('%s does not seem to exist. Please run build_fuzzers first.',
                 fuzzer_name)
    return False

  return True


def _env_to_docker_args(env_list):
  \"\"\"Turns envirnoment variable list into docker arguments.\"\"\"
  return sum([['-e', v] for v in env_list], [])


def workdir_from_lines(lines, default='/src'):
  \"\"\"Gets the WORKDIR from the given lines.\"\"\"
  for line in reversed(lines):  # reversed to get last WORKDIR.
    match = re.match(WORKDIR_REGEX, line)
    if match:
      workdir = match.group(1)
      workdir = workdir.replace('$SRC', '/src')

      if not os.path.isabs(workdir):
        workdir = os.path.join('/src', workdir)

      return os.path.normpath(workdir)

  return default


def docker_run(run_args, *, print_output=True, architecture='x86_64'):
  \"\"\"Calls `docker run`.\"\"\"
  platform = 'linux/arm64' if architecture == 'aarch64' else 'linux/amd64'
  command = [
      'docker', 'run', '--privileged', '--shm-size=2g', '--platform', platform
  ]
  if os.getenv('OSS_FUZZ_SAVE_CONTAINERS_NAME'):
    command.append('--name')
    command.append(os.getenv('OSS_FUZZ_SAVE_CONTAINERS_NAME'))
  else:
    command.append('--rm')

  # Support environments with a TTY.
  if sys.stdin.isatty():
    command.append('-i')

  command.extend(run_args)

  logger.info('Running: %s.', common_utils.get_command_string(command))
  stdout = None
  if not print_output:
    stdout = open(os.devnull, 'w')

  try:
    subprocess.check_call(command, stdout=stdout, stderr=subprocess.STDOUT)
  except subprocess.CalledProcessError:
    return False

  return True


def build_fuzzers(args):
  \"\"\"Build fuzzers.\"\"\"
  sanitized_binary_directories = ((args.sanitizer, ''),)
  return all(
      build_fuzzers_impl(args.project,
                         args.clean,
                         args.engine,
                         sanitizer,
                         args.architecture,
                         args.e,
                         args.source_path,
                         mount_path=args.mount_path,
                         child_dir=child_dir)
      for sanitizer, child_dir in sanitized_binary_directories)


def fuzzbench_build_fuzzers(args):
  \"\"\"Build fuzz targets with an arbitrary fuzzer from FuzzBench.\"\"\"
  with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_dir = os.path.abspath(tmp_dir)
    fuzzbench_path = os.path.join(tmp_dir, 'fuzzbench')
    subprocess.run([
        'git', 'clone', 'https://github.com/google/fuzzbench', '--depth', '1',
        fuzzbench_path
    ], check=True)
"""


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_fake_oss_fuzz_upstream(tmp_path: Path) -> tuple[Path, str, str]:
    upstream = tmp_path / "oss-fuzz-upstream"
    upstream.mkdir(parents=True)
    _run(["git", "init"], cwd=upstream)
    _run(["git", "config", "user.name", "Test User"], cwd=upstream)
    _run(["git", "config", "user.email", "test@example.com"], cwd=upstream)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=upstream)

    _write_file(upstream / "infra" / "helper.py", HELPER_BASE)
    for name in [
        "AGENTS.md",
        "CITATION.cff",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
    ]:
        _write_file(upstream / name, f"{name}\n")

    _run(["git", "add", "."], cwd=upstream)
    _run(["git", "commit", "-m", "base"], cwd=upstream)
    base_commit = _run(["git", "rev-parse", "HEAD"], cwd=upstream).stdout.strip()

    helper_path = upstream / "infra" / "helper.py"
    helper_path.write_text(helper_path.read_text() + "\n# drift commit\n")
    _run(["git", "commit", "-am", "drift"], cwd=upstream)
    drift_commit = _run(["git", "rev-parse", "HEAD"], cwd=upstream).stdout.strip()
    return upstream, base_commit, drift_commit


def _init_fake_crsbench_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    repo_root = tmp_path / "repo"
    (repo_root / "scripts").mkdir(parents=True, exist_ok=True)
    (repo_root / "third_party" / "patches").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        source_root / "scripts" / "setup-third-party.sh", repo_root / "scripts"
    )
    shutil.copy2(
        source_root / "third_party" / "patches" / "oss-fuzz-helper-cgroup.patch",
        repo_root / "third_party" / "patches",
    )
    shutil.copy2(
        source_root / "third_party" / "patches" / "oss-fuzz-helper-build-image.patch",
        repo_root / "third_party" / "patches",
    )
    return repo_root


def test_setup_third_party_repairs_existing_managed_oss_fuzz_checkout(
    tmp_path: Path,
) -> None:
    repo_root = _init_fake_crsbench_root(tmp_path)
    upstream, base_commit, drift_commit = _init_fake_oss_fuzz_upstream(tmp_path)
    script = repo_root / "scripts" / "setup-third-party.sh"
    env = os.environ.copy()
    env.update(
        {
            "CRSBENCH_OSS_FUZZ_REPO": upstream.as_uri(),
            "CRSBENCH_OSS_FUZZ_COMMIT": base_commit,
        }
    )

    first = _run(["bash", str(script), "--oss-fuzz-only"], cwd=repo_root, env=env)
    assert first.returncode == 0, first.stderr or first.stdout

    managed = repo_root / "third_party" / "oss-fuzz"
    helper_path = managed / "infra" / "helper.py"
    helper_text = helper_path.read_text()
    assert "_runtime_resource_docker_args" in helper_text
    assert "build_project_image=args.build_image" in helper_text

    _run(["git", "fetch", "--depth", "1", "origin", drift_commit], cwd=managed)
    _run(["git", "checkout", "-f", drift_commit], cwd=managed)
    helper_path.write_text(helper_path.read_text() + "\n# local drift\n")
    stray = managed / "stray.txt"
    stray.write_text("leftover\n")

    second = _run(["bash", str(script), "--oss-fuzz-only"], cwd=repo_root, env=env)
    assert second.returncode == 0, second.stderr or second.stdout

    head = _run(["git", "rev-parse", "HEAD"], cwd=managed).stdout.strip()
    assert head == base_commit
    assert not stray.exists()
    helper_text = helper_path.read_text()
    assert "_runtime_resource_docker_args" in helper_text
    assert "build_project_image=args.build_image" in helper_text


def test_setup_third_party_repoints_existing_managed_oss_fuzz_checkout_origin(
    tmp_path: Path,
) -> None:
    repo_root = _init_fake_crsbench_root(tmp_path)
    upstream_one, base_commit_one, _ = _init_fake_oss_fuzz_upstream(tmp_path / "one")
    upstream_two, base_commit_two, _ = _init_fake_oss_fuzz_upstream(tmp_path / "two")
    script = repo_root / "scripts" / "setup-third-party.sh"

    env_one = os.environ.copy()
    env_one.update(
        {
            "CRSBENCH_OSS_FUZZ_REPO": upstream_one.as_uri(),
            "CRSBENCH_OSS_FUZZ_COMMIT": base_commit_one,
        }
    )
    first = _run(["bash", str(script), "--oss-fuzz-only"], cwd=repo_root, env=env_one)
    assert first.returncode == 0, first.stderr or first.stdout

    managed = repo_root / "third_party" / "oss-fuzz"

    env_two = os.environ.copy()
    env_two.update(
        {
            "CRSBENCH_OSS_FUZZ_REPO": upstream_two.as_uri(),
            "CRSBENCH_OSS_FUZZ_COMMIT": base_commit_two,
        }
    )
    second = _run(["bash", str(script), "--oss-fuzz-only"], cwd=repo_root, env=env_two)
    assert second.returncode == 0, second.stderr or second.stdout

    head = _run(["git", "rev-parse", "HEAD"], cwd=managed).stdout.strip()
    origin_url = _run(
        ["git", "remote", "get-url", "origin"], cwd=managed
    ).stdout.strip()
    assert head == base_commit_two
    assert origin_url == upstream_two.as_uri()
