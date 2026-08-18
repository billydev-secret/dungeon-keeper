"""Read-only MCP server exposing Dungeon Keeper's specs and source.

Runs as a separate service (``/opt/dk-mcp``, unit ``dk-mcp.service``) against
the production checkout, so that specs drafted in chat are grounded in what the
bot actually does. Everything here is read-only by design: the chat session
produces spec text that lands through the normal ``/dk-feature`` gate and
commit path.
"""
