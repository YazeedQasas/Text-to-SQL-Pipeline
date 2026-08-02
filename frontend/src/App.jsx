import { useState } from "react";
import { submitQueryStream } from "./api";
import StageProgress from "./StageProgress";

export default function App() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [stages, setStages] = useState([]);
  const [progress, setProgress] = useState({}); // stage id -> { status, detail, startedAt, durationMs }
  const [streamed, setStreamed] = useState({}); // stage id -> partial LLM output

  function handleStage({ stage, status, detail, content }) {
    setProgress((prev) => {
      if (status === "started") {
        return { ...prev, [stage]: { status: "active", startedAt: performance.now() } };
      }
      const startedAt = prev[stage]?.startedAt;
      return {
        ...prev,
        [stage]: {
          status: "done",
          detail,
          content,
          durationMs: startedAt === undefined ? undefined : performance.now() - startedAt,
        },
      };
    });
  }

  function markActiveStageFailed() {
    setProgress((prev) =>
      Object.fromEntries(
        Object.entries(prev).map(([id, stage]) =>
          stage.status === "active" ? [id, { ...stage, status: "failed" }] : [id, stage],
        ),
      ),
    );
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (!question.trim() || loading) return;

    setLoading(true);
    setError(null);
    setResult(null);
    setStages([]);
    setProgress({});
    setStreamed({});

    try {
      const data = await submitQueryStream(question.trim(), {
        onStages: setStages,
        onStage: handleStage,
        onToken: ({ stage, text }) =>
          setStreamed((prev) => ({ ...prev, [stage]: (prev[stage] ?? "") + text })),
      });
      setResult(data);
    } catch (err) {
      setError(err.message);
      markActiveStageFailed();
    } finally {
      setLoading(false);
    }
  }

  const streamingAnswer = !result && streamed.answer_generation;

  return (
    <div className="page">
      <h1>Text-to-SQL — test harness</h1>
      <p className="subtitle">Ask a question about the legal case database.</p>

      <form onSubmit={handleSubmit} className="query-form">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. Which cases did Judge Amara Okafor preside over?"
          disabled={loading}
        />
        <button type="submit" disabled={loading || !question.trim()}>
          {loading ? "Thinking..." : "Submit"}
        </button>
      </form>

      <StageProgress
        stages={stages}
        progress={progress}
        streamedText={{ sql_generation: streamed.sql_generation }}
      />

      {error && <div className="error">{error}</div>}

      {(result || streamingAnswer) && (
        <div className="result">
          <h2>Answer</h2>
          <p className={`answer${streamingAnswer ? " answer-streaming" : ""}`}>
            {result ? result.answer : streamed.answer_generation}
          </p>

          {result && (
            <>
              <details>
                <summary>SQL executed</summary>
                <pre>{result.sql}</pre>
              </details>

              <details>
                <summary>Raw rows ({result.rows.length})</summary>
                <pre>{JSON.stringify(result.rows, null, 2)}</pre>
              </details>

              <details>
                <summary>Retrieved tables</summary>
                <ul>
                  {result.retrieved_tables.map((t) => (
                    <li key={t.table_name}>
                      <strong>{t.table_name}</strong> (score: {t.score.toFixed(3)}) — {t.description}
                    </li>
                  ))}
                </ul>
              </details>
            </>
          )}
        </div>
      )}
    </div>
  );
}
