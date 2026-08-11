import { useEffect, useLayoutEffect, useRef } from "react";
import ChatTurn from "./ChatTurn";

const SUGGESTIONS = [
  "كم عدد القضايا المفتوحة في كل محكمة؟",
  "من هو المدعي ومن هو المدعى عليه في القضية ID-2025-0064؟",
  "ما هي القضايا التي استغرقت أطول وقت قبل صدور الحكم؟",
];

// Distance from the bottom, in pixels, within which the transcript is
// considered "being followed" and keeps auto-scrolling. Scrolling further up
// than this to read an earlier answer stops the view being yanked back down on
// every streamed token.
const FOLLOW_THRESHOLD = 96;

const MAX_COMPOSER_HEIGHT = 200;

/**
 * "N remembered questions", with Arabic number agreement.
 *
 * Arabic does not split on one-vs-many the way English does: zero takes a
 * negation, two has its own dual form, 3–10 take a plural, and 11 and up go
 * back to a singular accusative. A ternary on `=== 1` produces "0 أسئلة", which
 * is wrong in a way a reader notices immediately.
 */
function rememberedLabel(count) {
  if (count === 0) return "لا أسئلة محفوظة";
  if (count === 1) return "سؤال واحد محفوظ";
  if (count === 2) return "سؤالان محفوظان";
  if (count <= 10) return `${count} أسئلة محفوظة`;
  return `${count} سؤالًا محفوظًا`;
}

/**
 * The transcript of one conversation.
 *
 * This component no longer owns the conversation — useChats does, because with
 * several chats the one on screen and the one being streamed into are not the
 * same thing. Everything here is a function of the `session` prop, which means
 * switching chats mid-answer swaps what is drawn without touching what is
 * running.
 *
 * The memory meter reads THIS chat's window. Each conversation is measured
 * against the model's context separately, so a chat that has filled up says so
 * without implying anything about the others.
 *
 * Result rows are left out of what the model is replayed — the answer text
 * already says what they showed, and one large result would crowd out the entire
 * rest of the window. When a chat's window fills, the conversation stops rather
 * than silently dropping its oldest turns, so what the model can see is never
 * less than what the user can see.
 */
export default function ChatPanel({
  chatId,
  session,
  busy,
  onAsk,
  onStop,
  onDraftChange,
  onNewChat,
}) {
  const scrollerRef = useRef(null);
  const composerRef = useRef(null);
  const following = useRef(true);

  const turns = session?.turns ?? [];
  const draft = session?.draft ?? "";
  const usage = session?.usage ?? null;
  const full = session?.full ?? false;

  function submit(question) {
    following.current = true;
    onAsk(question);
  }

  function handleSubmit(event) {
    event.preventDefault();
    const question = draft.trim();
    if (question && !busy && !full) submit(question);
  }

  function handleKeyDown(event) {
    // Enter sends, Shift+Enter opens a new line. `isComposing` guards IME input
    // (Arabic keyboards included) mid-composition.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      handleSubmit(event);
    }
  }

  // Grow the composer with its content, up to a cap, then scroll internally.
  useLayoutEffect(() => {
    const textarea = composerRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_COMPOSER_HEIGHT)}px`;
  }, [draft]);

  // Esc stops generating from anywhere on the page, not just the composer —
  // the answer is what you are watching when you decide to stop it. It stops the
  // VISIBLE chat only; another chat still working is not what Esc was aimed at.
  useEffect(() => {
    if (!busy) return undefined;

    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        onStop();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onStop]);

  // Keep the newest output in view, unless the reader has scrolled away from it.
  // Re-runs on chatId too, so switching to a chat lands at its newest turn.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller && following.current) {
      scroller.scrollTop = scroller.scrollHeight;
    }
  }, [turns, chatId]);

  // A different conversation is a different reading position: assume the newest
  // turn is wanted until this chat is scrolled away from in its own right.
  useEffect(() => {
    following.current = true;
    composerRef.current?.focus();
  }, [chatId]);

  function handleScroll(event) {
    const { scrollTop, scrollHeight, clientHeight } = event.currentTarget;
    following.current = scrollHeight - scrollTop - clientHeight < FOLLOW_THRESHOLD;
  }

  const fraction = usage ? Math.min(1, usage.used_tokens / usage.limit_tokens) : 0;
  // A turn that was answered but not stored. The answer is on screen and correct;
  // it will not be there after a reload, and only saying so at the time gives the
  // user the chance to copy it.
  const unsaved = turns.some((turn) => turn.status === "done" && turn.saved === false);

  if (!session) {
    return (
      <div className="chat">
        <div className="chat-scroller">
          <div className="chat-column">
            <div className="chat-empty">
              <h2>لا توجد محادثة مفتوحة</h2>
              <p>ابدأ محادثة جديدة من القائمة.</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chat">
      {/* Always rendered, so the tracker has a fixed home the eye can find
          rather than appearing only once there is something to report. */}
      <header className="chat-bar">
        <div
          className="meter"
          title={
            usage
              ? `استُهلك ${Math.round(fraction * 100)}٪ من ذاكرة هذه المحادثة`
              : "يُقاس بعد إرسال أول سؤال"
          }
        >
          <div className="meter-track">
            <div
              className={`meter-fill${fraction > 0.85 ? " meter-fill-high" : ""}`}
              style={{ width: `${Math.round(fraction * 100)}%` }}
            />
          </div>
          <span className="meter-label">
            {usage
              ? `${Math.round(fraction * 100)}٪ من ذاكرة المحادثة · ${rememberedLabel(
                  usage.history_turns,
                )}`
              : "الذاكرة — لم تُقَس بعد"}
          </span>
        </div>
      </header>

      <div className="chat-scroller" ref={scrollerRef} onScroll={handleScroll}>
        <div className="chat-column">
          {session.loadError && (
            <div className="error" dir="auto">
              تعذّر تحميل هذه المحادثة: {session.loadError}
            </div>
          )}

          {turns.length === 0 ? (
            <div className="chat-empty">
              <h2>اسأل عن قاعدة بيانات القضايا</h2>
              <p>
                اطرح سؤالك بالعربية وستحصل على إجابة مبنية على بيانات المحاكم. يمكنك
                المتابعة بأسئلة إضافية — لكل محادثة ذاكرتها الخاصة — إلى أن تمتلئ
                ذاكرتها، وعندها تبدأ محادثة جديدة.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="suggestion-chip"
                    dir="auto"
                    onClick={() => submit(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((turn) => <ChatTurn key={turn.key} turn={turn} />)
          )}
        </div>
      </div>

      <div className="composer-area">
        {unsaved && (
          <div className="context-full context-warn">
            <span>
              تعذّر حفظ إحدى الإجابات في هذه المحادثة. الإجابة صحيحة لكنها لن تظهر
              بعد إعادة تحميل الصفحة — انسخها إن كنت بحاجة إليها.
            </span>
          </div>
        )}

        {full && (
          <div className="context-full">
            <span>
              امتلأت ذاكرة هذه المحادثة. لم يُحذف أي سؤال سابق — ابدأ محادثة جديدة
              للمتابعة. المحادثات الأخرى غير متأثرة.
            </span>
            <button type="button" onClick={onNewChat}>
              محادثة جديدة
            </button>
          </div>
        )}

        <form className="composer" onSubmit={handleSubmit}>
          <textarea
            ref={composerRef}
            rows={1}
            value={draft}
            // `auto` reads the direction off the value, and an empty box has no
            // strong character to read — so browsers fall back to LTR and the
            // Arabic placeholder starts at the left with its ellipsis trailing
            // off to the right. Pin the empty field to RTL; once there is
            // something typed, `auto` can do its job again.
            dir={draft ? "auto" : "rtl"}
            disabled={full}
            placeholder={
              full
                ? "ابدأ محادثة جديدة للمتابعة"
                : "اسأل عن القضايا أو القضاة أو الجلسات أو الأحكام…"
            }
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={handleKeyDown}
          />
          {busy ? (
            <button
              type="button"
              className="stop"
              onClick={onStop}
              aria-label="إيقاف"
              title="إيقاف (Esc)"
            >
              <span className="stop-icon" />
            </button>
          ) : (
            <button type="submit" disabled={full || !draft.trim()} aria-label="إرسال">
              ↑
            </button>
          )}
        </form>
        <p className="composer-hint">
          {busy
            ? "اضغط Esc أو زر الإيقاف للإلغاء"
            : "Enter للإرسال · Shift+Enter لسطر جديد · لكل محادثة ذاكرتها الخاصة"}
        </p>
      </div>
    </div>
  );
}
