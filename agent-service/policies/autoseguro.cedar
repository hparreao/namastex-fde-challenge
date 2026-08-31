// Default deny: Cedar denies when no permit policy matches.

permit (
  principal is AutoSeguro::Session,
  action == AutoSeguro::Action::"CallLLM",
  resource is AutoSeguro::Session
)
when {
  resource == principal &&
  context.sessionState != "completed" &&
  context.sessionState != "handoff" &&
  context.sanitized &&
  ["openai", "openai-agents-sdk", "anthropic", "fake"].contains(context.destination)
};

permit (
  principal is AutoSeguro::Session,
  action == AutoSeguro::Action::"CallQuote",
  resource is AutoSeguro::Session
)
when {
  resource == principal &&
  context.sessionState == "confirmation" &&
  context.dataComplete &&
  context.confirmed &&
  context.sanitized &&
  context.destination == "quote-service"
};

permit (
  principal is AutoSeguro::Session,
  action == AutoSeguro::Action::"PersistAudit",
  resource is AutoSeguro::Session
)
when { resource == principal && context.sanitized };

permit (
  principal is AutoSeguro::Session,
  action == AutoSeguro::Action::"CompleteSession",
  resource is AutoSeguro::Session
)
when {
  resource == principal &&
  context.sessionState == "quote_presented" &&
  context.quoteSucceeded
};

permit (
  principal is AutoSeguro::Session,
  action == AutoSeguro::Action::"HandoffSession",
  resource is AutoSeguro::Session
)
when { resource == principal && context.reasonPresent };
