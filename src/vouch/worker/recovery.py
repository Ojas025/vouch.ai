import os
import json

import redis.asyncio as redis

STREAM = "jobs"
GROUP = "workers"
CONSUMER = os.getenv("HOSTNAME", "worker-1")

async def recover():
   pass
