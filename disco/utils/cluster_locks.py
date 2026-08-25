from __future__ import annotations

import asyncio

# Held by the reconciler while it makes changes; recovery takes it as a barrier.
reconciler_lock = asyncio.Lock()

# Held by recovery for the whole operation; the reconciler skips its pass while
# it is locked.
recovery_lock = asyncio.Lock()

# Addresses whose cluster .remove failed, retried by each reconcile pass. Once
# the Swarm service is gone the reconciler cannot detect the phantom voter as an
# orphan, so it must be remembered here.
pending_cluster_removes: set[str] = set()
