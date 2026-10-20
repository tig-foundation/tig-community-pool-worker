# Community Pool worker development

The worker implements the whole-benchmark member protocol in the `worker_v2`
package. See [the worker guide](WORKER.md) for behavior, configuration and
validation limits, and [installation](INSTALL.md) for how members install a
pinned release.

## Package layout

| Module | Role |
|---|---|
| `worker_v2/client.py` | Versioned member protocol over HTTPS; execution token only |
| `worker_v2/runner.py` | Single-machine reference runner owning one benchmark at a time |
| `worker_v2/state.py` | Durable SQLite assignments and nonce evidence, retained across restarts |
| `worker_v2/runtime.py` | Pinned Docker runtime scoped to this worker's own benchmark containers |
| `worker_v2/artifacts.py` | Public algorithm binary downloads that never forward credentials |
| `worker_v2/proofs.py` | TIG output metadata and whole-benchmark Merkle proofs |
| `worker_v2/resources.py` | Explicit container limits and optional containment of the worker itself |
| `worker_v2/fleet.py` | Member master combining local compute and configured SSH helpers |
| `worker_v2/helper.py`, `ssh_transport.py` | Member-owned compute helper and bounded JSON RPC over SSH |
| `tools/install_worker_v2.py` | Checked installer: pinned checkout, private environment, drain and update |
| `tools/run_cpu_pilot.py`, `deploy/local-cpu-pilot/` | Bounded single-worker launcher for a shared host |

## Branches and releases

- `main` is the integration branch; features use separate branches and pull
  requests, and the `worker-baseline` check must pass.
- `release/v2` is fast-forwarded to each worker commit the production pool
  serves and never receives direct development.
- The pool's CI pins a worker commit for its paired API tests. A production
  release pins and validates both commits with real challenge runtimes, and the
  installer's SHA-256 is recorded in the pool's release manifest, so any change
  to `tools/install_worker_v2.py` needs a new paired release.

## Tests

```sh
python3 -m unittest discover -s tests/v2 -v
```

The suite covers CPU and GPU whole assignments, lost responses, interrupted
compute, restart recovery, preserved evidence, runtime isolation, a recorded
public TIG proof, the installer against real temporary Git repositories, the
SSH fleet through real subprocess pipes, and resource limits. Runtime and
network operations are mocked: the tests do not contact a live pool or start
challenge containers.

## Validation

What has been exercised on real hosts and on mainnet, and what remains open,
is recorded in the pool repository's
[ROADMAP.md](https://github.com/tig-foundation/tig-community-pool/blob/main/docs/ROADMAP.md).
Challenge viability depends on the algorithm, workload and hardware and must
be measured again when any of them changes.
