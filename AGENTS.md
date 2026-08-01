# HAH — Agent Guide

## Project

HAH (Hire a Human) is a hackathon project where agents hire humans to amplify their product on social channels. Humans will post about products through their warm accounts on behalf of the agent. 

Brands will create tasks an agent using MCP. The agent pays the human through Prava.

## User journeys

### Brand

1. Sign up.
2. Create a task using MCP (or manually).
3. Hire a human.
4. Pay the human through Prava.

### Human

1. Sign up.
2. Submit only a public Reddit or LinkedIn account URL for enrichment; no username, social login, or OAuth.
3. Find and accept a task.
4. Complete the task and mark it done.
5. Get paid.

## Working rules

- Use FastAPI for the backend.
- Simplify aggressively.
- Build only what the request requires.
- Do not invent requirements, flows, integrations, commands, or code.
- Ask when missing information would change the implementation.
- Keep copy short and direct (remove stuff obvious to humans).

# Repo conventions

- NEVER PUSH TO `main`, use named branch: `aaryan`, `abhik`, `akhil`, `shivesh`
- ALWAYS pull before push
