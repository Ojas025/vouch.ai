from vouch.schema import MessageSubmission, Result
from vouch import security
from vouch import adapter

class PipelineOrchestrator:
    def __init__(self, adapter: adapter.Adapter, dispatcher, cache, jev):
        self.adapter = adapter
        self.cache = None
        self.jev = None

    async def run(self, channel: str, raw_payload: dict, headers: dict) -> Result | None:
        # Normalize input to canonical MessageSubmission
        # Meta or Telegram sends a token/secret in the headers
        # We verify these headers before touching the body to block denial-of-service attacks
        if not security.validate_signature(raw_payload, headers):
            return None

        submission = self.adapter.adapt(channel, raw_payload)

        # Normalize the submission


        return Result()
