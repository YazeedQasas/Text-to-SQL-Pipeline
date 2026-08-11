import ActivityPanel from "./ActivityPanel";
import CachePanel from "./CachePanel";
import CatalogPanel from "./CatalogPanel";
import ConceptsPanel from "./ConceptsPanel";
import { BoltIcon, BookIcon, ListIcon, RefreshIcon } from "./Icons";

/**
 * The admin page: four widgets, one screen.
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
          <Widget title="المفاهيم القانونية" Icon={BookIcon} accent="#2b6cb0" tint="#e8f0fe">
            <ConceptsPanel />
          </Widget>

          <Widget
            title="تعديلات في قاعدة البيانات"
            Icon={RefreshIcon}
            accent="#b7791f"
            tint="#fdf1d6"
          >
            <CatalogPanel onReviewCountChange={onReviewCountChange} />
          </Widget>
        </div>

        <div className="admin-column">
          {/* Taller than the others by design: it is a feed, and a feed with
              four rows visible tells you nothing about what has been going on. */}
          <Widget title="نشاط النظام" Icon={ListIcon} accent="#2f855a" tint="#e6f4ea" tall>
            <ActivityPanel />
          </Widget>

          {/* Below the feed rather than beside the glossary: it reports on what
              the system did, which is the same question the activity log
              answers, and it is the shortest widget on the page. */}
          <Widget title="ذاكرة الأسئلة المتكررة" Icon={BoltIcon} accent="#6b46c1" tint="#efe9fb">
            <CachePanel />
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
function Widget({ title, Icon, accent, tint, tall, children }) {
  return (
    <section
      className={`widget${tall ? " widget-tall" : ""}`}
      style={{ "--widget-accent": accent, "--widget-tint": tint }}
    >
      <div className="widget-head">
        {/* The icon takes the widget's accent colour, which an emoji could not —
            it carries its own palette and ignored the one around it. */}
        <span className="widget-icon">
          <Icon size={18} />
        </span>
        <h2 className="widget-title">{title}</h2>
      </div>
      {/* Each widget scrolls inside itself so one long list — usually the
          activity feed — cannot push the others off the bottom of the page. */}
      <div className="widget-body">{children}</div>
    </section>
  );
}
