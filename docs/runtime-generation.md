# Runtime generation

The profile sync and a few long-running helpers run from an installed runtime generation under
`~/.codex/claude-sync/runtime-artifacts/<root>`, not from source checkouts. The tools that build,
install and launch it live in `tools/`:

| File | Role |
|---|---|
| `tools/build-runtime-generation.py` | Builds a release (wheels, source resources, lock, bootstrap) from committed shas |
| `tools/runtime_artifacts.py` | Lock validation and offline install; shipped inside the bootstrap |
| `tools/install-runtime-artifacts.py` | The bootstrap's `__main__` (`runtime-bootstrap.pyz`) |
| `tools/runtime_launch.py` | The stable entry, published as `<root>/launch.py` |
| `tools/runtime_restore.py` | Installs the toolchain and a generation on a machine that has none |
| `tools/remote_bridge.py` | The MCP remote bridge, shipped as a source resource |

The build spec and its version pins are operator configuration and stay in a private config
repository: the builder takes `--config-repo <checkout> --spec <path inside it>` and reads
`requirements-runtime.txt` beside the spec, both at one commit of that repository. The spec names
the repositories to build (this one must be among them, because the bootstrap and the lock code
come from it), the source resources, the pinned PyPI wheels, the pinned toolchain archive and the
lock header.

## Building a release

Run the builder with the toolchain interpreter the spec names, because the lock records that
interpreter's bootstrap and the installer refuses a mismatch:

```text
<toolchain python> -I -B tools/build-runtime-generation.py --config-repo <dir> --spec <path> --out <empty dir>
```

It resolves each repository's ref (default `main`, override with `--ref REPO=REF`, `config=REF`
for the config repository) once to a sha, builds every first-party wheel from a fresh clone
detached at that sha and reads every source resource from the object store at that sha. A version
that differs from its pin, or a wheel whose Python bytes are not committed bytes, fails the build.
Nothing comes from an earlier release: third-party wheels are downloaded from the PyPI URLs the
spec lists and must hash to the listed sha256, and the lock is computed fresh. The finished bundle
is verified by the `runtime-bootstrap.pyz` it ships, and `build-report.json` records every sha.

Install is a separate step: publish the output as `runtime-artifacts/releases/<lock sha256>` and run
`<toolchain python> -I -B <release>/runtime-bootstrap.pyz --lock <release>/runtime-lock.json --bundle <release> --root <root> --expected-lock-sha256 <digest>`.
The builder installs only with `--install-root`, and refuses the live roots unless
`--allow-live-root` is also given.

## The stable entry: `<root>/launch.py`

A generation directory is named by a fresh random token on every install, so no consumer may name
one. Every install publishes `tools/runtime_launch.py` as `<root>/launch.py`; a root installed
before that gets it with `runtime-bootstrap.pyz --publish-launcher --root <root>`. Consumers name
the launcher and the toolchain interpreter, both stable paths:

```text
<toolchain python> -I -B <root>/launch.py --script sources/<resource>/<path> [ARGS...]
<toolchain python> -I -B <root>/launch.py -m <module> [ARGS...]
```

Each launch checks `current.json`, the receipt digest and the interpreter (and `--script` file)
digests, then runs the child with `-I -B` inside a Windows job object that ends with the launcher.
It does not re-hash the whole generation; `runtime-bootstrap.pyz --resolve` is the full check.
Every refusal exits 78 with one JSON line naming a fixed reason, and starts nothing.

## A new machine

`runtime-artifacts/` and `toolchains/` are rebuildable and are not backed up.
`tools/runtime_restore.py install --config-repo <dir> --spec <file>` installs the pinned toolchain
when it is missing, builds a release from the repositories' `main`, publishes it under `releases/`,
installs it with the release's own bootstrap and publishes `launch.py`. An active generation that
verifies is kept; one that does not fails untouched. `--root-name R --from-root installed` installs
the release another root already runs into a second root.
