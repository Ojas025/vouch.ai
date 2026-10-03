"""
Adapter for converting raw Telegram or Whatsapp payload into canonical MessageSubmission schema
"""

from vouch.schema import IncomingMessage

def adapt_whatsapp(raw_payload) -> IncomingMessage:
    ...

def adapt_telegram(raw_payload) -> IncomingMessage:
    ...
