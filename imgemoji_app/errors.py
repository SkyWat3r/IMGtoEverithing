from __future__ import annotations


class EmojiMakerError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise EmojiMakerError(message)
