"""The run engine: takes a ticket to a decided draft merge request, starting
each agent as its own headless session. Agents answer; the engine applies
their answers through the rules in cq, and `cq.policy` decides."""
