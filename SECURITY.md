# Security policy

The member worker runs on members' own machines, handles an execution token
that authorizes paid work, and executes pinned challenge runtimes in Docker.

## Reporting a vulnerability

Please report vulnerabilities privately rather than in a public issue. Use
GitHub's private vulnerability reporting for this repository:
[report a vulnerability](https://github.com/tig-foundation/tig-community-pool-worker/security/advisories/new).
Include what you found, how to reproduce it and the impact you expect. We will
acknowledge the report, keep you informed and credit you when the fix is
published unless you prefer otherwise.

## Scope

- The worker package (`worker_v2`), the installer (`tools/install_worker_v2.py`)
  and the bounded pilot launcher.
- The member master mode and its SSH helper transport.

The pool itself lives in [its own repository](https://github.com/tig-foundation/tig-community-pool) with
[its own policy](https://github.com/tig-foundation/tig-community-pool/blob/main/SECURITY.md).
Findings about a particular member's machine or network belong to that member.

## What we ask

Do not test against a live pool with real member funds without the operator's
written permission.
