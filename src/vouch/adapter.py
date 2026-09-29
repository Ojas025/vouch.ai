"""
Adapter for converting raw Telegram or Whatsapp payload into canonical MessageSubmission schema
"""

from vouch.schema import MessageSubmission

class Adapter:
    def __init__(self):
        pass

    def _adapt_whatsapp(self) -> MessageSubmission:
        return MessageSubmission()

    def _adapt_telegram(self) -> MessageSubmission:
        return MessageSubmission()

    def adapt(self, channel: str, raw_payload: dict) -> MessageSubmission:
        self.raw_payload = raw_payload

        if channel == "whatsapp":
            return self._adapt_whatsapp()
        else:
            return self._adapt_telegram()
