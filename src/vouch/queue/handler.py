from src.vouch.schema import IncomingMessage

class EventHandler:
    def __init__(self):
        ...

    async def handle(self, payload: IncomingMessage) -> None:
        raise NotImplementedError
