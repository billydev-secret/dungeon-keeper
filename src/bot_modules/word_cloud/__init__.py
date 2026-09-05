"""Channel word clouds — corpus acquisition, tokenising and rendering.

``logic`` is pure and holds the behaviour worth testing; ``corpus`` reads the
message archive or falls back to a live Discord fetch; ``render`` turns counts
into a PNG. The Discord surface is ``cogs.word_cloud_cog``.
"""
