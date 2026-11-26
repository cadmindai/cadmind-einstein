"""CADmind einstein: the Bittensor side of CADmind's einstein engine.

Miners run their own agents and build client devices through the hosted CADmind Watt tools (MCP or
REST). The validator gives each client job to one miner (:mod:`.desk`); its output passes the
hosted Watt gate, or the round ends "not finished" with its project kept for a round that continues
it. A delivered job scores by its client's latest reaction (0.5 untouched; 1 downloaded or rated
3-5 stars; 0 rated 1-2 stars or refunded for its quality); anything not delivered scores 0
(:mod:`.scoring`). A change of points is dated when it happens: a miner's weight is the sum of its
point changes dated in the last 7 days, set hourly, never burned and never empty; its rating (the
points of its jobs of the last 7 days over their number) reads its lane. With the lanes open
(off by default) client jobs go to the miners rated into the production lane and the others
improve delivered devices a judge scored low (:mod:`.improvement`). The validators' model votes
(:mod:`.review`, :mod:`.votes`) are recorded and published for calibration; no model decides a
delivery, a client job's score, a weight or a route. Miners read
what affects their weight in :mod:`.stats`. A miner works on its own agent key, made on the
platform and naming its hotkey; job workspaces, each with its allowance of compute credits, are
granted, frozen and forked on the platform (:mod:`.platform`); copies are caught
(:mod:`.similarity`); state is durable (:mod:`.store`); the chain side is :mod:`.chain`, the HTTP
face :mod:`.service`, the process :mod:`.daemon`; the command-line conventions shared by the
programs are :mod:`.cli` and the release check :mod:`.version`.
"""


def _spec(version: str) -> int:
    """``MAJOR.MINOR.PATCH`` as one integer, ``10000*MAJOR + 100*MINOR + PATCH`` (each part below
    100): the version key the validator sets its weights with."""
    parts = [int(p) for p in version.split(".")[:3]]
    parts += [0] * (3 - len(parts))
    if any(not 0 <= p < 100 for p in parts):
        raise ValueError(f"version part out of range for the spec encoding: {version}")
    major, minor, patch = parts
    return 10000 * major + 100 * minor + patch


__version__ = "0.6.27"
__spec_version__ = _spec(__version__)
