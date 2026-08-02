const STATUS_ICON = {
  pending: "○",
  active: "●",
  done: "✓",
  failed: "✕",
};

function formatDuration(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/**
 * Renders the backend-declared pipeline stages with their live status.
 *
 * The stage list comes from the server (the first event of the stream), so the
 * frontend never has to duplicate the pipeline definition.
 */
export default function StageProgress({ stages, progress, streamedText = {} }) {
  if (!stages.length) return null;

  return (
    <ol className="stages">
      {stages.map((stage) => {
        const { status = "pending", detail, content, durationMs } = progress[stage.id] || {};
        const live = status === "active" ? streamedText[stage.id] : null;

        // When the stage reported full text behind its summary (e.g. the schema
        // prompt), make the summary itself the disclosure toggle.
        const detailNode = content ? (
          <details className="stage-detail stage-detail-expandable">
            <summary>{detail}</summary>
            <pre className="stage-content">{content}</pre>
          </details>
        ) : (
          detail && <p className="stage-detail">{detail}</p>
        );

        return (
          <li key={stage.id} className={`stage stage-${status}`}>
            <span className="stage-icon" aria-hidden="true">
              {STATUS_ICON[status]}
            </span>
            <span className="stage-label">{stage.label}</span>
            <span className="stage-duration">
              {durationMs === undefined ? "" : formatDuration(durationMs)}
            </span>
            {live ? <pre className="stage-stream">{live}</pre> : detailNode}
          </li>
        );
      })}
    </ol>
  );
}
