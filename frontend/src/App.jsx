import { useEffect, useState } from "react";
import AdminPage from "./AdminPage";
import ChatPanel from "./ChatPanel";
import { fetchCdcStatus } from "./api";

const PAGES = [
  { id: "ask", label: "اسأل", icon: "💬" },
  { id: "admin", label: "الإدارة", icon: "⚙️" },
];

export default function App() {
  const [page, setPage] = useState("ask");
  // Tables the automated path documented but would NOT index — those are not
  // queryable until someone acts. Badged because the whole point of automation
  // is that nobody is watching the page where the work lands. Tables it indexed
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
      {/* The sidebar is first in the DOM and the page is dir="rtl", which is
          what puts it on the right — no ordering override needed. */}
      <nav className="sidebar">
        <div className="brand">مساعد الاستعلام</div>
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
            {id === "admin" && waiting > 0 && <span className="nav-badge">{waiting}</span>}
          </button>
        ))}
      </nav>

      {/* Both pages stay mounted so switching mid-review doesn't discard the
          scan results, the transcript, or an in-flight answer. */}
      <main className="main">
        <div className="pane" hidden={page !== "ask"}>
          <ChatPanel />
        </div>
        <div className="pane pane-scroll" hidden={page !== "admin"}>
          <AdminPage onReviewCountChange={setWaiting} />
        </div>
      </main>
    </div>
  );
}
