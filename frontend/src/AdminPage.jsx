import ActivityPanel from "./ActivityPanel";
import CatalogPanel from "./CatalogPanel";
import ConceptsPanel from "./ConceptsPanel";

/**
 * The admin page: three widgets, one screen.
 *
 * They were three separate tabs. The work is one job — a change to the data gets
 * documented, the activity log is where you find out, and a concept is what
 * makes the new information answerable — and splitting it across tabs meant the
 * evidence for a decision was never on the same screen as the decision.
 *
 * The grid's first column is the right-hand one, because the page is RTL.
 */
export default function AdminPage({ onReviewCountChange }) {
  return (
    <div className="admin">
      <div className="admin-grid">
        <div className="admin-column">
          <Widget title="المفاهيم القانونية" icon="📖" accent="#2b6cb0" tint="#e8f0fe">
            <ConceptsPanel />
          </Widget>

          {/* 🔄 rather than a filing-cabinet glyph: the dark card-index emoji
              reads as a smudge at this size against the amber tint. */}
          <Widget title="تعديلات في قاعدة البيانات" icon="🔄" accent="#b7791f" tint="#fdf1d6">
            <CatalogPanel onReviewCountChange={onReviewCountChange} />
          </Widget>
        </div>

        <div className="admin-column">
          {/* Taller than the others by design: it is a feed, and a feed with
              four rows visible tells you nothing about what has been going on. */}
          <Widget title="نشاط النظام" icon="📋" accent="#2f855a" tint="#e6f4ea" tall>
            <ActivityPanel />
          </Widget>
        </div>
      </div>
    </div>
  );
}

/**
 * One panel.
 *
 * The accent colour is passed down as a custom property rather than a modifier
 * class, so adding a fourth widget is a colour in this file and nothing in the
 * stylesheet.
 */
function Widget({ title, icon, accent, tint, tall, children }) {
  return (
    <section
      className={`widget${tall ? " widget-tall" : ""}`}
      style={{ "--widget-accent": accent, "--widget-tint": tint }}
    >
      <div className="widget-head">
        <span className="widget-icon" aria-hidden="true">
          {icon}
        </span>
        <h2 className="widget-title">{title}</h2>
      </div>
      {/* Each widget scrolls inside itself so one long list — usually the
          activity feed — cannot push the others off the bottom of the page. */}
      <div className="widget-body">{children}</div>
    </section>
  );
}
