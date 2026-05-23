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
