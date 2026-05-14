import random


def random_derangement(participants: list[int]) -> dict[int, int]:
    """
    Generate a random derangement: each person gives to exactly one other,
    receives from exactly one other, and no one is paired with themselves.
    Returns {giver_id: receiver_id}.
    """
    n = len(participants)
    if n < 2:
        return {}

    shuffled = participants[:]
    random.shuffle(shuffled)

    # Sattolo cycle — guaranteed derangement in O(n)
    perm = list(range(n))
    for i in range(n - 1, 0, -1):
        j = random.randint(0, i - 1)
        perm[i], perm[j] = perm[j], perm[i]

    return {shuffled[i]: shuffled[perm[i]] for i in range(n)}
