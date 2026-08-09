import formatAnswer from "./formatAnswer";
import StageProgress from "./StageProgress";

function formatDuration(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} ث` : `${Math.round(ms)} مث`;
}

/**
 * One exchange: the question asked, and the pipeline's response to it.
 *
 * The pipeline stages are the equivalent of a chat model's "thinking" — shown
 * expanded while the run is in flight, then collapsed to a one-line summary,
 * because once an answer exists the stages are diagnostics rather than content.
 *
 * The answer is the whole of what this page shows. The SQL, the rows it
 * returned and the tables it read are not rendered anywhere: they are how the
 * answer was produced, and the reader here is being answered, not debugged. All
 * three are still on the admin side, and the SQL is still what the next turn's
 * history replays.
 */
const STEPS_LABEL = {
  cancelled: "توقّف عند",
  error: "تعذّر الإكمال بعد",
  done: "خطوات المعالجة",
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
          <StageProgress stages={stages} progress={progress} />
        ) : (
          stages.length > 0 && (
            <details className="steps">
              <summary>
                {STEPS_LABEL[status] ?? STEPS_LABEL.done}
                {elapsedMs === undefined ? "" : ` — ${formatDuration(elapsedMs)}`}
              </summary>
              <StageProgress stages={stages} progress={progress} />
            </details>
          )
        )}

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
            {answer
              ? "تم الإيقاف — الإجابة أعلاه غير مكتملة."
              : "تم الإيقاف قبل كتابة أي إجابة."}{" "}
            لن يتذكّر النظام هذا السؤال.
          </p>
        )}
      </div>
    </article>
  );
}
