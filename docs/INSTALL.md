# Installing and updating a pinned worker

The pool serves a checked installer for the exact worker commit named in its
paired release. Installation starts from the pool's Join page.

## Install from the pool's recorded release

Use Python 3.12, Git and a working Docker installation on the member's machine.
GPU work also needs a compatible NVIDIA GPU and container runtime. The pool's
Join page supplies the installer from the exact worker commit in its paired
release. Save that file locally before running it.

```sh
python3 install_worker_v2.py install \
  --pool https://pool.example \
  --directory "$HOME/innopool-v2-member" \
  --resource CPU --compute-type aws_c7a --workers 4
"$HOME/innopool-v2-member/run"
```

Choose an ARM verification type (`aws_t4g`, `aws_c7g` or `aws_m7g`) on an ARM
machine. Other CPU types use AMD64. GPU requests use `--resource GPU
--compute-type aws_g4dn` on AMD64. Each installation offers one resource class.
Member installations still share the pool's two-slot and collateral limits.

On a shared host, configure hard resource limits before starting the worker.
`--workers 1` does not itself cap CPU use or memory. Follow the
[local CPU pilot procedure](LOCAL_CPU_PILOT.md) for one worker at a time within
a shared one-CPU/2-GiB budget. Its limits in `worker.json` survive drained updates.

The installer prompts privately for a member execution token. It can also read
`POOL_V2_EXECUTION_TOKEN` from the environment. The token is saved in a private
file outside Git checkouts and is passed only to the worker process. Never
provide a member wallet private key or the pool's TIG API key.

The installer verifies the pool's API version, whole-benchmark assignment unit,
release digest and pool commit. The manifest must name the expected worker repository,
a full worker commit, a tag resolving to that commit and a compatible evidence
format. A moving branch is not an installation target. The installer records
the worker commit in `worker.json`; the running worker refuses new work once
the pool pairs a different worker release. An existing destination is refused unless an explicit
update verifies its ownership marker and checkout identity.

Each installation has its own private directory:

| Path | Purpose |
|---|---|
| `installation-v2.json` | Atomically activated paired release and ownership record. |
| `worker.json` | Member resource, capacity and pool configuration. |
| `execution-token` | Private execution credential, mode 0600. |
| `data/` | SQLite state, nonce evidence and owned runtime files. |
| `releases/<full-commit>/code` | Detached, verified worker checkout. |
| `releases/<full-commit>/venv` | Separate Python environment. |
| `run` | Launcher for the installed revision; no Git network operations. |

The runtime manages only containers with this installation's ownership labels.
The installer does not start or stop Docker containers, install system services,
reset checkouts or modify an original pool/worker installation. A routine
restart runs the same recorded code. Local checkout changes cause startup and
updates to stop for review.

## Drain and explicitly update

```sh
python3 install_worker_v2.py drain --directory "$HOME/innopool-v2-member"
# Wait for the worker to finish saved work and exit.
python3 install_worker_v2.py update --directory "$HOME/innopool-v2-member"
"$HOME/innopool-v2-member/run"
```

Download the current installer from the Join page again before an update. The
installer checks its own bytes against the recorded release and refuses an
outdated or altered copy. Draining can still use the previously downloaded file.

The drain request is durable. A running worker sees it at the next progress
cycle, finishes existing work and exits without refreshing a queued offer.
Existing ambiguity can still require protocol reconciliation before draining
can finish. The updater will not discard or mark that work complete itself.

Updates require the running-worker lock to be free and SQLite to contain no
unfinished request. The old release remains available and the token, member
configuration and entire evidence directory are preserved. Runtime image
references change only to the new recorded manifest. An interrupted build can
be retried with `update`; a partially activated configuration fails closed and
the explicit retry completes activation. It never falls back to an upstream
reset or deletes evidence to make an upgrade proceed.

`scripts/start-fresh.sh /absolute/installation/directory` invokes this pinned
launcher. It has no image-pull, Git-update, container-recreation or
system-service installation behavior. Give any locally configured supervisor
its own service name and use restart-on-failure, so a successful
drain stays stopped. Service installation remains an explicit member action.

## Release metadata contract

The pool serves `/api/v2/release`. Its capabilities response identifies the
same canonical JSON SHA-256 as `release_digest`, plus `pool_commit`.

```json
{
  "manifest_version": 1,
  "api_version": "2.0",
  "pool": {"repository": "https://github.com/Daniel-T-S-Adams/community-pool.git", "commit": "<40 hex characters>", "tag": "<tested pool tag>"},
  "worker": {"repository": "https://github.com/Daniel-T-S-Adams/community-pool-worker.git", "commit": "<40 hex characters>", "tag": "<tested worker tag>", "state_version": 1, "installer_sha256": "<64 hex characters>"},
  "runtime_version": "0.0.8",
  "runtime_images": {"c001": "ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:<64 hex characters>"}
}
```

`runtime_images` is informational here: the pool pins the official image of
each active challenge at that runtime version and names it inside every
assignment, so a newly pinned challenge needs no reinstall.

This is a schema example, not a deployable release. The pool's deployment
record also needs the tested database migrations, application images and TIG
integration evidence. Release metadata is created after the tested application
commits, so it does not change the commits it identifies.

Installer tests use real temporary Git repositories, tags, detached checkouts,
Python environments and subprocesses. They simulate the pool manifest and
worker execution; they do not contact a live pool, start a runtime container or
demonstrate actual CPU/GPU challenge performance.
