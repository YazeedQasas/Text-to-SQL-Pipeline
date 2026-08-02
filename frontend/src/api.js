const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

/**
 * Run a query against the streaming endpoint, reporting progress as it happens.
 *
 * EventSource can't issue a POST, so this reads the server-sent event stream
 * off the fetch response body directly.
 *
 * @param {string} question
 * @param {{ onStages?: (stages) => void, onStage?: (event) => void, onToken?: (event) => void }} handlers
 * @returns {Promise<object>} the final QueryResponse
 */
export async function submitQueryStream(question, handlers = {}) {
  const { onStages, onStage, onToken } = handlers;

  const response = await fetch(`${API_BASE_URL}/api/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ question }),
  });

  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || `Request failed with status ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;

  const handleEvent = (event) => {
    switch (event.type) {
      case "stages":
        onStages?.(event.stages);
        break;
      case "stage":
        onStage?.(event);
        break;
      case "token":
        onToken?.(event);
        break;
      case "result":
        result = event.result;
        break;
      case "error":
        throw new Error(event.detail);
      default:
        break;
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; the trailing piece may be partial.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("");
        if (data) handleEvent(JSON.parse(data));
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }

  if (!result) {
    throw new Error("The connection closed before a result arrived.");
  }
  return result;
}
