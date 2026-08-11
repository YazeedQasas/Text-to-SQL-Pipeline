import { useEffect, useState } from "react";
import AdminPage from "./AdminPage";
import ChatList from "./ChatList";
import ChatPanel from "./ChatPanel";
import { fetchCdcStatus } from "./api";
import { ChatIcon, CloseIcon, SettingsIcon } from "./Icons";
import useChats from "./useChats";

const PAGES = [
  { id: "ask", label: "اسأل", Icon: ChatIcon },
  { id: "admin", label: "الإدارة", Icon: SettingsIcon },
];

export default function App() {
  const [page, setPage] = useState("ask");
  // Tables the automated path documented but would NOT index — those are not
  // queryable until someone acts. Badged because the whole point of automation
  // is that nobody is watching the page where the work lands. Tables it indexed
  // on its own are deliberately not counted here: they already work, and
  // badging them would train people to ignore the badge.
  const [waiting, setWaiting] = useState(0);

  // The conversations live here rather than in ChatPanel: several can be running
  // at once, and only one of them is the one on screen. See useChats.
  const chats = useChats();

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
        {PAGES.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            className={`nav-item${page === id ? " nav-item-active" : ""}`}
            onClick={() => setPage(id)}
          >
            <span className="nav-icon">
              <Icon />
            </span>
            {label}
            {id === "admin" && waiting > 0 && <span className="nav-badge">{waiting}</span>}
          </button>
        ))}

        {/* Only on the Ask page: the chat list is navigation within that page,
            and showing it next to the admin screens would imply otherwise. */}
        {page === "ask" && (
          <ChatList
            chats={chats.list}
            activeId={chats.activeId}
            running={chats.running}
            onSelect={chats.selectChat}
            onNew={chats.startChat}
            onRename={chats.retitleChat}
            onDelete={chats.removeChat}
          />
        )}
      </nav>

      {/* Both pages stay mounted so switching mid-review doesn't discard the
          scan results, the transcript, or an in-flight answer. */}
      <main className="main">
        <div className="pane" hidden={page !== "ask"}>
          {chats.listError && (
            <div className="error chat-list-error" dir="auto">
              {chats.listError}
              <button type="button" onClick={chats.dismissListError} aria-label="إغلاق">
                <CloseIcon size={14} />
              </button>
            </div>
          )}
          <ChatPanel
            // Keyed by chat so per-chat DOM state — scroll position, the
            // composer's measured height — is not carried across a switch.
            key={chats.activeId ?? "none"}
            chatId={chats.activeId}
            session={chats.active}
            busy={chats.activeId ? chats.running.has(chats.activeId) : false}
            onAsk={(question) => chats.ask(chats.activeId, question)}
            onStop={() => chats.stopChat(chats.activeId)}
            onDraftChange={(draft) => chats.setDraft(chats.activeId, draft)}
            onNewChat={chats.startChat}
          />
        </div>
        <div className="pane pane-scroll" hidden={page !== "admin"}>
          <AdminPage onReviewCountChange={setWaiting} />
        </div>
      </main>
    </div>
  );
}
