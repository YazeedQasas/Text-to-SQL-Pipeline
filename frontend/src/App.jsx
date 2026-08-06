import { useEffect, useState } from "react";
import ActivityPanel from "./ActivityPanel";
import CatalogPanel from "./CatalogPanel";
import ChatPanel from "./ChatPanel";
import ConceptsPanel from "./ConceptsPanel";
import { fetchCdcStatus } from "./api";

const PAGES = [
  { id: "ask", label: "Ask", icon: "💬" },
  { id: "schema", label: "Schema updates", icon: "🗂" },
  { id: "concepts", label: "Concepts", icon: "📖" },
  { id: "activity", label: "Activity", icon: "📋" },
];

export default function App() {
  const [page, setPage] = useState("ask");
  // Tables the automated path documented but would NOT index — those are not
  // queryable until someone acts. Badged because the whole point of automation
  // is that nobody is watching the tab where the work lands. Tables it indexed
  // on its own are deliberately not counted here: they already work, and
  // badging them would train people to ignore the badge.
  const [waiting, setWaiting] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchCdcStatus()
        .then((data) => !cancelled && setWaiting(data.awaiting_review ?? 0))
        .catch(() => {});
    load();
    const interval = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">Text-to-SQL</div>
        {PAGES.map(({ id, label, icon }) => (
          <button
            key={id}
            type="button"
            className={`nav-item${page === id ? " nav-item-active" : ""}`}
            onClick={() => setPage(id)}
          >
            <span className="nav-icon" aria-hidden="true">
              {icon}
            </span>
            {label}
            {id === "schema" && waiting > 0 && <span className="nav-badge">{waiting}</span>}
          </button>
        ))}
      </nav>

      {/* Both panels stay mounted so switching pages mid-review doesn't discard
          the scan results, the transcript, or an in-flight answer. */}
      <main className="main">
        <div className="pane" hidden={page !== "ask"}>
          <ChatPanel />
        </div>
        <div className="pane pane-scroll" hidden={page !== "schema"}>
          <div className="page">
            <h1>Schema updates</h1>
            <CatalogPanel onReviewCountChange={setWaiting} />
          </div>
        </div>
        <div className="pane pane-scroll" hidden={page !== "concepts"}>
          <div className="page">
            <h1>Concepts</h1>
            <ConceptsPanel />
          </div>
        </div>
        <div className="pane pane-scroll" hidden={page !== "activity"}>
          <div className="page">
            <h1>Activity</h1>
            <ActivityPanel />
          </div>
        </div>
      </main>
    </div>
  );
}
