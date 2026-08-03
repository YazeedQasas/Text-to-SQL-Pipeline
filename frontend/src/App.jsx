import { useState } from "react";
import CatalogPanel from "./CatalogPanel";
import QueryPanel from "./QueryPanel";

const TABS = [
  { id: "ask", label: "Ask" },
  { id: "schema", label: "Schema updates" },
];

export default function App() {
  const [tab, setTab] = useState("ask");

  return (
    <div className="page">
      <h1>Text-to-SQL — test harness</h1>

      <nav className="tabs">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            className={`tab${tab === id ? " tab-active" : ""}`}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {/* Both panels stay mounted so switching tabs mid-review doesn't discard
          the scan results or an in-flight answer. */}
      <div hidden={tab !== "ask"}>
        <QueryPanel />
      </div>
      <div hidden={tab !== "schema"}>
        <CatalogPanel />
      </div>
    </div>
  );
}
