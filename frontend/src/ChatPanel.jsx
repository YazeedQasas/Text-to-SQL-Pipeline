import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { submitQueryStream } from "./api";
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

let nextTurnId = 1;

function newTurn(question) {
  return {
    id: nextTurnId++,
    question,
    stages: [],
    progress: {}, // stage id -> { status, detail, content, startedAt, durationMs }
    streamed: {}, // stage id -> partial LLM output
    result: null,
    error: null,
    status: "running",
    elapsedMs: undefined,
  };
}

/** The turns the model gets to see, in the shape the API expects. */
function toHistory(turns) {
  return turns
    .filter((turn) => turn.status === "done" && turn.result)
    .map((turn) => ({
      question: turn.question,
      sql: turn.result.sql,
      answer: turn.result.answer,
      table_names: turn.result.retrieved_tables.map((table) => table.table_name),
    }));
}

/**
 * The chat transcript.
 *
 * The backend keeps no session state, so this component owns the conversation:
 * every question is sent with the exchanges before it, and the model's memory
 * is exactly what is on screen. Result rows are left out of that replay — the
 * answer text already says what they showed, and one large result would crowd
 * out the entire rest of the window.
 *
 * When the window fills the conversation stops rather than silently dropping
 * its oldest turns, so what the model can see is never less than what the user
 * can see.
 *
 * The SQL and the tables each turn used are still carried in `result` and still
 * replayed to the model as history — they are simply never drawn. See ChatTurn.
 */
export default function ChatPanel() {
  const [turns, setTurns] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState(null); // { used_tokens, limit_tokens, history_turns }
  const [full, setFull] = useState(false);

  const scrollerRef = useRef(null);
  const composerRef = useRef(null);
  const following = useRef(true);
  const abortRef = useRef(null);

  /** Patch the turn currently being streamed (always the last one). */
  function updateActiveTurn(patch) {
    setTurns((prev) => {
      if (prev.length === 0) return prev;
      const active = prev[prev.length - 1];
      return [...prev.slice(0, -1), { ...active, ...patch(active) }];
    });
  }

  function handleStage({ stage, status, detail, content }) {
    updateActiveTurn((turn) => {
      const previous = turn.progress[stage];
      const next =
        status === "started"
          ? { status: "active", startedAt: performance.now() }
          : {
              status: "done",
              detail,
              content,
              durationMs:
                previous?.startedAt === undefined
                  ? undefined
                  : performance.now() - previous.startedAt,
            };
      return { progress: { ...turn.progress, [stage]: next } };
    });
  }

  function startNewChat() {
    setTurns([]);
    setUsage(null);
    setFull(false);
    setDraft("");
    composerRef.current?.focus();
  }

  /** Drop the connection, which also cancels the model call behind it. */
  function stopGenerating() {
    abortRef.current?.abort();
  }

  async function ask(question) {
    setBusy(true);
    setDraft("");
    const history = toHistory(turns);
    setTurns((prev) => [...prev, newTurn(question)]);
    following.current = true;
    const startedAt = performance.now();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const result = await submitQueryStream(question, history, {
        signal: controller.signal,
        onStages: (stages) => updateActiveTurn(() => ({ stages })),
        onStage: handleStage,
        // Arrives mid-run, so the meter is right even if the turn then fails.
        onUsage: setUsage,
        onToken: ({ stage, text }) =>
          updateActiveTurn((turn) => ({
            streamed: { ...turn.streamed, [stage]: (turn.streamed[stage] ?? "") + text },
          })),
      });
      setUsage(result.usage);
      updateActiveTurn(() => ({
        result,
        status: "done",
        elapsedMs: performance.now() - startedAt,
      }));
    } catch (err) {
      // A cancellation is not a failure: whatever the model wrote before the
      // stop is kept and shown, it just never becomes a finished turn.
      const cancelled = err.name === "AbortError";

      // 413 is the context cutoff: the question was never run, and no further
      // question can be until the conversation is reset.
      if (err.status === 413) setFull(true);

      updateActiveTurn((turn) => ({
        error: cancelled ? null : err.message,
        status: cancelled ? "cancelled" : "error",
        elapsedMs: performance.now() - startedAt,
        // Whichever stage was in flight is the one that stopped.
        progress: Object.fromEntries(
          Object.entries(turn.progress).map(([id, stage]) =>
            stage.status === "active" ? [id, { ...stage, status: "failed" }] : [id, stage],
          ),
        ),
      }));
    } finally {
      abortRef.current = null;
      setBusy(false);
      composerRef.current?.focus();
    }
  }

  function handleSubmit(event) {
    event.preventDefault();
    const question = draft.trim();
    if (question && !busy && !full) ask(question);
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
  // the answer is what you are watching when you decide to stop it.
  useEffect(() => {
    if (!busy) return undefined;

    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        stopGenerating();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy]);

  // Abandoning the page mid-run should free the model too.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Keep the newest output in view, unless the reader has scrolled away from it.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller && following.current) {
      scroller.scrollTop = scroller.scrollHeight;
    }
  }, [turns]);

  function handleScroll(event) {
    const { scrollTop, scrollHeight, clientHeight } = event.currentTarget;
    following.current = scrollHeight - scrollTop - clientHeight < FOLLOW_THRESHOLD;
  }

  const fraction = usage ? Math.min(1, usage.used_tokens / usage.limit_tokens) : 0;

  return (
    <div className="chat">
      {/* Always rendered, so the tracker has a fixed home the eye can find
          rather than appearing only once there is something to report. */}
      <header className="chat-bar">
        <div
          className="meter"
          title={
            usage
              ? `استُهلك ${Math.round(fraction * 100)}٪ من ذاكرة المحادثة`
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
              ? `${Math.round(fraction * 100)}٪ من الذاكرة · ${rememberedLabel(
                  usage.history_turns,
                )}`
              : "الذاكرة — لم تُقَس بعد"}
          </span>
        </div>
        <button
          type="button"
          className="new-chat"
          onClick={startNewChat}
          disabled={turns.length === 0}
        >
          محادثة جديدة
        </button>
      </header>

      <div className="chat-scroller" ref={scrollerRef} onScroll={handleScroll}>
        <div className="chat-column">
          {turns.length === 0 ? (
            <div className="chat-empty">
              <h2>اسأل عن قاعدة بيانات القضايا</h2>
              <p>
                اطرح سؤالك بالعربية وستحصل على إجابة مبنية على بيانات المحاكم. يمكنك
                المتابعة بأسئلة إضافية — النظام يتذكّر هذه المحادثة — إلى أن تمتلئ
                الذاكرة، وعندها تبدأ محادثة جديدة.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="suggestion-chip"
                    dir="auto"
                    onClick={() => ask(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((turn) => <ChatTurn key={turn.id} turn={turn} />)
          )}
        </div>
      </div>

      <div className="composer-area">
        {full && (
          <div className="context-full">
            <span>
              امتلأت ذاكرة هذه المحادثة. لم يُحذف أي سؤال سابق — ابدأ محادثة جديدة
              للمتابعة.
            </span>
            <button type="button" onClick={startNewChat}>
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
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleKeyDown}
          />
          {busy ? (
            <button
              type="button"
              className="stop"
              onClick={stopGenerating}
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
            : "Enter للإرسال · Shift+Enter لسطر جديد · النظام يتذكّر هذه المحادثة"}
        </p>
      </div>
    </div>
  );
}
