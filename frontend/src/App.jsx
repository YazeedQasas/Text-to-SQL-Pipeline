import { useState } from "react";
import CatalogPanel from "./CatalogPanel";
import ChatPanel from "./ChatPanel";

const PAGES = [
  { id: "ask", label: "Ask", icon: "💬" },
  { id: "schema", label: "Schema updates", icon: "🗂" },
];

export default function App() {
  const [page, setPage] = useState("ask");

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
            <CatalogPanel />
          </div>
        </div>
      </main>
    </div>
  );
}
