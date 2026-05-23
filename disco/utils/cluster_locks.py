"""Shared in-process locks coordinating recovery and the reconciler.

Recovery is destructive and must not race with the reconciler's
"oh I see a service is missing, let me recreate it" behavior. Both
flows acquire these locks; recovery additionally holds them for the
full duration of the operation.

Locks are in-process. A daemon restart drops them — by design, since
that's also the moment when "what was the previous state" stops being
load-bearing for safety.
"""

from __future__ import annotations

import asyncio

# Held by the reconciler while it's making changes. Acquired by
# recovery as a barrier so the reconciler can't interleave.
reconciler_lock = asyncio.Lock()

# Held by recovery for the entire op. The reconciler skips its run
# if this is locked.
recovery_lock = asyncio.Lock()

# Addresses we tried to evict from the dqlite cluster but couldn't
# (e.g. local dqlite container was momentarily down). Each reconcile
# pass retries them until success. Without this, a single transient
# failure during node deletion leaves the dqlite cluster forever
# carrying a phantom voter that the reconciler can no longer detect
# as an orphan (the Swarm service is already gone).
pending_cluster_removes: set[str] = set()
