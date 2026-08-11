import { useEffect, useRef, useState } from "react";
import { PencilIcon, PlusIcon, TrashIcon } from "./Icons";

/**
 * The conversation list.
 *
 * Ordered by last activity rather than creation, so the chat just used is at the
 * top. A rename does not reorder it — retitling an old conversation is not
 * activity in it, and having it jump to the top for a typo fix would be
 * disorienting.
 *
 * A chat still working is marked, because chats run concurrently and the one
 * producing an answer is often not the one being looked at. It is also honest
 * about a real constraint: LM Studio serves one model at a time, so a second
 * question started elsewhere is why this one slowed down.
 */

/** An untitled chat is one with no turns yet — it has nothing to be named after. */
const UNTITLED = "محادثة جديدة";

function ChatRow({ chat, active, running, onSelect, onRename, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(chat.title ?? "");
  const [confirming, setConfirming] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  // A title changed elsewhere — the first question naming an untitled chat —
  // should be picked up, but never while it is being typed over.
  useEffect(() => {
    if (!editing) setValue(chat.title ?? "");
  }, [chat.title, editing]);

  function commit() {
    setEditing(false);
    const trimmed = value.trim();
    if (trimmed && trimmed !== chat.title) onRename(trimmed);
    else setValue(chat.title ?? "");
  }

  if (editing) {
    return (
      <li className="chat-row chat-row-editing">
        <input
          ref={inputRef}
          className="chat-row-input"
          dir="auto"
          value={value}
          maxLength={120}
          onChange={(event) => setValue(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "Escape") {
              event.preventDefault();
              setValue(chat.title ?? "");
              setEditing(false);
            }
          }}
        />
      </li>
    );
  }

  return (
    <li className={`chat-row${active ? " chat-row-active" : ""}`}>
      <button type="button" className="chat-row-main" onClick={onSelect} dir="auto">
        <span className="chat-row-title">{chat.title || UNTITLED}</span>
        {running && (
          <span className="chat-row-running" aria-label="جارٍ الإجابة" title="جارٍ الإجابة" />
        )}
      </button>

      {confirming ? (
        <span className="chat-row-confirm">
          <button
            type="button"
            className="chat-row-danger"
            onClick={() => {
              setConfirming(false);
              onDelete();
            }}
          >
            حذف
          </button>
          <button type="button" onClick={() => setConfirming(false)}>
            إلغاء
          </button>
        </span>
      ) : (
        <span className="chat-row-actions">
          <button
            type="button"
            onClick={() => setEditing(true)}
            aria-label="إعادة التسمية"
            title="إعادة التسمية"
          >
            <PencilIcon size={14} />
          </button>
          {/* Confirmed rather than immediate: the transcript is deleted with the
              chat and there is no undo. */}
          <button
            type="button"
            onClick={() => setConfirming(true)}
            aria-label="حذف المحادثة"
            title="حذف المحادثة"
          >
            <TrashIcon size={14} />
          </button>
        </span>
      )}
    </li>
  );
}

export default function ChatList({
  chats,
  activeId,
  running,
  onSelect,
  onNew,
  onRename,
  onDelete,
}) {
  return (
    <div className="chat-list">
      <button type="button" className="chat-list-new" onClick={onNew}>
        <PlusIcon size={14} />
        محادثة جديدة
      </button>

      {chats.length === 0 ? (
        <p className="chat-list-empty">لا توجد محادثات</p>
      ) : (
        <ul className="chat-rows">
          {chats.map((chat) => (
            <ChatRow
              key={chat.id}
              chat={chat}
              active={chat.id === activeId}
              running={running.has(chat.id)}
              onSelect={() => onSelect(chat.id)}
              onRename={(title) => onRename(chat.id, title)}
              onDelete={() => onDelete(chat.id)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
