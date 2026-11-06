from typing import Any

from vouch.lease import Lease

async def complete_investigation(result: Any, lease: Lease):
    ...

async def handle_failure(error: str, lease: Lease):
    ...
