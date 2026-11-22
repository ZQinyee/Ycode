import type { Chat, Reply } from "./agent";

export function ollama(baseURL: string, model: string): Chat {
  const url = new URL("/api/chat", baseURL);
  return async (messages, tools) => {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages, tools, stream: false }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!response.ok) throw new Error(`Ollama HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
    const { message } = await response.json();
    if (message?.role !== "assistant" || typeof message.content !== "string") {
      throw new Error("Invalid chat response from Ollama.");
    }
    if (message.tool_calls != null) {
      if (!Array.isArray(message.tool_calls)) throw new Error("Expected a list of tool calls.");
      for (const call of message.tool_calls) {
        if (typeof call?.function?.name !== "string" || !("arguments" in call.function)) {
          throw new Error("Invalid tool call from Ollama.");
        }
      }
    }
    return message as Reply;
  };
}
