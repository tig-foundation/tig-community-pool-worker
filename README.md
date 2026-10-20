# Community Pool member worker

The reference worker for members of
[Community Pool](https://github.com/tig-foundation/tig-community-pool), a member-run
pool for The Innovation Game (TIG). It takes one whole benchmark at a time
from the pool, computes every nonce with the official pinned challenge
runtime, keeps durable evidence of every output, and returns the complete
result and the sampled proofs through the pool's
[member API](https://github.com/tig-foundation/tig-community-pool/blob/main/docs/MEMBER_API.md).
It can also act as a member master that spreads one benchmark across
compatible machines over SSH.

- [Worker behavior and configuration](docs/WORKER.md)
- [Install, drain and update a pinned worker](docs/INSTALL.md)
- [Configure a member master and SSH helpers](docs/SSH_MEMBER_MASTER.md)
- [Run one bounded worker on a shared machine](docs/LOCAL_CPU_PILOT.md)
- [Development notes](docs/REDESIGN.md)

## How members install it

The pool's Join page serves a checked installer for the exact worker commit
named in the pool's paired release. The installer verifies the pool's release
digest and commit, creates a private installation with a detached checkout and
its own Python environment, asks privately for the member's execution token,
and never touches other installations or containers. Restarting runs the same
recorded code. Updates drain existing work first and preserve configuration,
credentials and benchmark evidence.

Execution tokens authorize work only; they cannot move member funds and are
not TIG API keys or wallet keys.

## Members with several machines

Members may use the SSH member master mode, or their own compatible master, to
coordinate several machines and submit one complete benchmark through the
pool's API. Every machine used for an assignment must match the verification
compute type, architecture, algorithm binary and pinned runtime. The pool's
[member master contract](https://github.com/tig-foundation/tig-community-pool/blob/main/docs/MEMBER_MASTER.md)
specifies the integration.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-v2.txt
.venv/bin/python -m unittest discover -s tests/v2 -v
.venv/bin/python -m worker_v2 --config /absolute/path/worker.json
```

Python 3.12 is the supported version. `scripts/start-fresh.sh <installation>`
starts an installed worker's pinned launcher and performs no Git, container or
service changes. `main` is the integration branch and `release/v2` follows the
worker commit the production pool serves; the pool's CI pins a worker commit
for its paired API tests.
