const STATUS_ICON = {
  pending: "○",
  active: "●",
  done: "✓",
  failed: "✕",
};

function formatDuration(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} ث` : `${Math.round(ms)} مث`;
}

/**
 * Renders the backend-declared pipeline stages with their live status.
 *
 * The stage list and its per-stage text come from the server (the first event of
 * the stream), so the frontend never has to duplicate the pipeline definition —
 * including its Arabic wording.
 *
 * Stages carry a `content` field too (the schema prompt, the matched glossary)
 * and the SQL arrives token by token, but neither is rendered here: this is what
 * an end user reads while waiting, and it is meant to say what the system is
 * doing, not to show it the raw material it is doing it with.
 */
export default function StageProgress({ stages, progress }) {
  if (!stages.length) return null;

  return (
    <ol className="stages">
      {stages.map((stage) => {
        const { status = "pending", detail, durationMs } = progress[stage.id] || {};

        return (
          <li key={stage.id} className={`stage stage-${status}`}>
            <span className="stage-icon" aria-hidden="true">
              {STATUS_ICON[status]}
            </span>
            <span className="stage-label">{stage.label}</span>
            <span className="stage-duration">
              {durationMs === undefined ? "" : formatDuration(durationMs)}
            </span>
            {detail && <p className="stage-detail">{detail}</p>}
          </li>
        );
      })}
    </ol>
  );
}
