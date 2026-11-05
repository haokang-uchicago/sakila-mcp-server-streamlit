"""
server.py - the MCP server: puts the four bounded tools from tools.py behind MCP,
so Claude Code (or any MCP client) can read their descriptions and call them.

This file only passes calls through. The SQL, the argument checks and the honesty
notes all live in tools.py, which app.py calls too, so both front doors give the
same answer.

Run by the MCP client over stdio (see .mcp.json); nothing to start by hand.
"""
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import tools

INSTRUCTIONS = """Answers questions about the Sakila DVD-rental data warehouse (Snowflake, SAKILA.DW).
- Call describe_schema first when a question is vague, names a year or date, or may ask for data that is not here.
- Use aggregate for any count, total, average or ranking; list_payments only shows example rows.
- "Customers" means distinct people (metric="customers"); rentals are about 27x more, never use them for customers.
- Every result has `grain` (what one row is) and `notes`. Read the notes and repeat what matters:
  zero rows means NO DATA, not zero; a gap in the months is missing data, not a drop in business.
- If no tool can answer the question, say so plainly instead of guessing.
- Use only the filters (category, store, dates, customer) the current question asks for. Do not carry
  filters over from an earlier question unless the user refers back to it ("same month", "that category").
- Map a value to an allowed option only when it clearly means that option ("sci fi" -> "Sci-Fi").
  If it holds anything else (extra words, quotes, SQL, symbols), pass it unchanged so the tool can
  reject it, then tell the user the value was not valid. Never quietly fix it.
- To report cost, pass the query_id values from a result's `queries` to query_cost."""

mcp = MCPServer("sakila", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
for tool in tools.TOOLS:  # each function's docstring becomes the description the model reads
    mcp.tool(annotations=READ_ONLY)(tool)

if __name__ == "__main__":
    mcp.run()  # stdio: the client starts this process and talks to it over stdin/stdout
