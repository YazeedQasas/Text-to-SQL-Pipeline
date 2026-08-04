import formatAnswer from "./formatAnswer";
import StageProgress from "./StageProgress";

function formatDuration(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/**
 * One exchange: the question asked, and the pipeline's response to it.
 *
 * The pipeline stages are the equivalent of a chat model's "thinking" — shown
 * expanded while the run is in flight, then collapsed to a one-line summary,
 * because once an answer exists the stages are diagnostics rather than content.
 */
const STEPS_LABEL = {
  cancelled: "Stopped",
  error: "Failed",
  done: "Worked through",
};

export default function ChatTurn({ turn }) {
  const { question, stages, progress, streamed, result, error, status, elapsedMs } = turn;
  const running = status === "running";
  const cancelled = status === "cancelled";
  // A cancelled turn keeps whatever the model had already written.
  const answer = result ? result.answer : streamed.answer_generation;

  return (
    <article className="turn">
      <div className="turn-user">
        <div className="bubble" dir="auto">
          {question}
        </div>
      </div>

      <div className="turn-assistant">
        {running ? (
          <StageProgress
            stages={stages}
            progress={progress}
            streamedText={{ sql_generation: streamed.sql_generation }}
          />
        ) : (
          stages.length > 0 && (
            <details className="steps">
              <summary>
                {STEPS_LABEL[status] ?? "Worked through"}
                {elapsedMs === undefined ? "" : ` — ${formatDuration(elapsedMs)}`}
              </summary>
              <StageProgress stages={stages} progress={progress} />
            </details>
          )
        )}

        {/* dir="auto" throughout: answers and refusals are Arabic, while
            transport errors and Latin-script identifiers are not. */}
        {error && (
          <div className="error" dir="auto">
            {error}
          </div>
        )}

        {answer && (
          <p className={`answer${running ? " answer-streaming" : ""}`} dir="auto">
            {formatAnswer(answer)}
          </p>
        )}

        {cancelled && (
          <p className="cancelled-note">
            Stopped{answer ? " — the answer above is incomplete" : " before an answer was written"}.
            This turn is not part of what the model remembers.
          </p>
        )}

        {result && (
          <div className="turn-details">
            <details>
              <summary>SQL</summary>
              <pre>{result.sql}</pre>
            </details>

            <details>
              <summary>Rows ({result.rows.length})</summary>
              <pre>{JSON.stringify(result.rows, null, 2)}</pre>
            </details>

            <details>
              <summary>Tables used ({result.retrieved_tables.length})</summary>
              <ul>
                {result.retrieved_tables.map((table) => (
                  <li key={table.table_name}>
                    <strong>{table.table_name}</strong> ({table.score.toFixed(3)}) —{" "}
                    <span dir="auto">{table.description}</span>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
      </div>
    </article>
  );
}
