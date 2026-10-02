"""
Provably fair number generation and ticket signing.

Commit-reveal scheme
--------------------
1. When a draw opens we generate a secret 256-bit server seed and publish
   SHA-256(server_seed). The operator is now committed to it.
2. When sales close, the client seed is computed from every sold ticket's
   signature, so players collectively contribute entropy the operator could
   not know when committing.
3. Winning numbers = HMAC-SHA256(server_seed, f"{client_seed}:{nonce}:{round}")
   consumed 4 bytes at a time with rejection sampling (no modulo bias),
   skipping duplicates, until N numbers are chosen.
4. After the draw the server seed is revealed. Anyone can check that its hash
   matches the commitment and re-derive the same numbers (the results page
   does this in the browser).
"""

import hashlib
import hmac
import secrets

from django.conf import settings


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def compute_client_seed(ticket_signatures, nonce) -> str:
    material = "|".join(sorted(ticket_signatures)) or f"no-tickets:{nonce}"
    return sha256_hex(material)


def derive_numbers(server_seed: str, client_seed: str, nonce: str, count: int, maximum: int) -> list[int]:
    if count > maximum:
        raise ValueError("Cannot draw more unique numbers than the range holds.")
    limit = (2**32 // maximum) * maximum  # largest multiple of maximum below 2^32
    numbers: list[int] = []
    round_ = 0
    while len(numbers) < count:
        digest = hmac.new(server_seed.encode(), f"{client_seed}:{nonce}:{round_}".encode(), hashlib.sha256).digest()
        round_ += 1
        for i in range(0, len(digest), 4):
            value = int.from_bytes(digest[i:i + 4], "big")
            if value >= limit:
                continue
            number = value % maximum + 1
            if number not in numbers:
                numbers.append(number)
                if len(numbers) == count:
                    break
    return numbers


def fair_rank(server_seed: str, client_seed: str, serial: str) -> str:
    """Verifiable random order used to choose between equally-matched tickets."""
    return hmac.new(server_seed.encode(), f"{client_seed}:rank:{serial}".encode(), hashlib.sha256).hexdigest()


def quick_pick(count: int, maximum: int) -> list[int]:
    pool = list(range(1, maximum + 1))
    picks = []
    for _ in range(count):
        picks.append(pool.pop(secrets.randbelow(len(pool))))
    return sorted(picks)


def ticket_payload(*, serial, user_public_id, draw_id, purchased_at, numbers) -> str:
    return f"{serial}|{user_public_id}|{draw_id}|{purchased_at.isoformat()}|{','.join(map(str, numbers))}"


def sign_ticket(**fields) -> str:
    return hmac.new(settings.TICKET_SIGNING_KEY.encode(), ticket_payload(**fields).encode(), hashlib.sha256).hexdigest()


def verify_ticket(ticket) -> bool:
    expected = sign_ticket(
        serial=ticket.serial,
        user_public_id=ticket.user.public_id,
        draw_id=ticket.draw_id,
        purchased_at=ticket.purchased_at,
        numbers=ticket.numbers,
    )
    return hmac.compare_digest(expected, ticket.signature)
