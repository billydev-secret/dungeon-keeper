"""The member-facing ``/info`` panel — the self-service half of ``/modinfo``.

``logic`` decides *what* a member is told and which self-service actions are
offered; ``embeds`` renders it; ``views`` wires the buttons to each feature's
own opt-in flow. Nothing here grants a role or writes an opt-in itself — see
``views`` for why that separation is load-bearing.
"""
