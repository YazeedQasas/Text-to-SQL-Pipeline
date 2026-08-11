import { useCallback, useEffect, useRef, useState } from "react";
import {
  createChat as apiCreateChat,
  deleteChat as apiDeleteChat,
  fetchChat,
  fetchChats,
  renameChat as apiRenameChat,
  submitQueryStream,
} from "./api";

/**
 * Every conversation, and the one being looked at.
 *
 * WHY THIS IS A MODULE AND NOT STATE INSIDE ChatPanel
 * ---------------------------------------------------
 * ChatPanel used to hold the transcript itself, which made "the current chat"
 * and "the chat being streamed into" the same thing by construction. With
 * several chats they come apart, and every patch that assumed otherwise becomes
 * a bug: the old code updated `turns[turns.length - 1]` — the last turn of
 * whatever was on screen — so switching tabs mid-answer wrote another chat's
 * tokens into the visible one.
 *
 * So nothing here is addressed by position. Every update names the chat and the
 * turn it belongs to, and a patch for a chat that has since been deleted lands
 * nowhere instead of on its neighbour.
 *
 * CONCURRENCY
 * -----------
 * Several chats may stream at once. Each gets its own AbortController, held in a
 * ref keyed by chat id, so stopping one leaves the others running.
 *
 * The real ceiling is not this code: LM Studio serves a single loaded model, so
 * two concurrent runs queue against each other and both take longer than one
 * would alone. That is worth showing rather than hiding — the sidebar marks
 * every chat that is currently working.
 *
 * TWO KINDS OF TURN
 * -----------------
 * A turn read back from the server is finished: it has a question, an answer and
 * nothing else. A turn created here starts empty and fills in as the stream
 * arrives. Both use the same shape so the transcript renders one way, with the
 * live fields simply empty on a restored turn — which is why reopening a chat
 * shows answers but no stage timings. Those were never stored; they described a
 * run, not a result.
 */

/** Local ids for turns created in this session, distinct from server row ids. */
let nextLocalTurnId = 1;

function newTurn(question) {
  return {
    key: `local-${nextLocalTurnId++}`,
    question,
    stages: [],
    progress: {}, // stage id -> { status, detail, content, startedAt, durationMs }
    streamed: {}, // stage id -> partial LLM output
    result: null,
    error: null,
    status: "running",
    elapsedMs: undefined,
    saved: false,
  };
}

/** A stored turn, in the shape the transcript renders. */
function restoredTurn(turn) {
  return {
    key: `server-${turn.id}`,
    question: turn.question,
    // Empty on purpose: stage progress described a run that is over, and was
    // never stored. The collapsed "steps" summary is simply absent.
    stages: [],
    progress: {},
    streamed: {},
    result: {
      answer: turn.answer,
      sql: turn.sql,
      columns: [],
      rows: [],
      retrieved_tables: turn.table_names.map((name) => ({
        table_name: name,
        description: "",
        score: 0,
      })),
    },
    error: null,
    status: "done",
    elapsedMs: undefined,
    saved: true,
  };
}

/** The per-chat state that is not on the server. */
function newSession() {
  return {
    turns: [],
    usage: null, // { used_tokens, limit_tokens, history_turns }
    full: false, // this chat's window is exhausted; others are unaffected
    loaded: false, // transcript fetched at least once
    loadError: null,
    draft: "",
  };
}

export default function useChats() {
  // Chat metadata, in sidebar order (newest activity first).
  const [list, setList] = useState([]);
  // chatId -> session. Separate from `list` because the two change for different
  // reasons: the list is the server's, sessions are this tab's view of them.
  const [sessions, setSessions] = useState({});
  const [activeId, setActiveId] = useState(null);
  const [ready, setReady] = useState(false);
  const [listError, setListError] = useState(null);

  // chatId -> AbortController for the run in flight, if any.
  const controllers = useRef(new Map());

  /** Patch one session by id. A chat that is gone is left alone. */
  const patchSession = useCallback((chatId, patch) => {
    setSessions((prev) => {
      const session = prev[chatId];
      if (!session) return prev;
      return { ...prev, [chatId]: { ...session, ...patch(session) } };
    });
  }, []);

  /**
   * Patch one turn of one chat, addressed by key rather than by position.
   *
   * This is the function the single-chat version got wrong. It patched the last
   * turn of the visible chat, which is the right turn only while there is one
   * chat and it is the one on screen.
   */
  const patchTurn = useCallback(
    (chatId, turnKey, patch) => {
      patchSession(chatId, (session) => ({
        turns: session.turns.map((turn) =>
          turn.key === turnKey ? { ...turn, ...patch(turn) } : turn,
        ),
      }));
    },
    [patchSession],
  );

  const ensureSession = useCallback((chatId) => {
    setSessions((prev) => (prev[chatId] ? prev : { ...prev, [chatId]: newSession() }));
  }, []);

  /** Fetch a chat's transcript, once. */
  const loadChat = useCallback(
    async (chatId) => {
      let alreadyLoaded = false;
      setSessions((prev) => {
        const session = prev[chatId] ?? newSession();
        alreadyLoaded = session.loaded;
        return { ...prev, [chatId]: session };
      });
      if (alreadyLoaded) return;

      try {
        const detail = await fetchChat(chatId);
        setSessions((prev) => {
          const session = prev[chatId];
          if (!session) return prev;
          // Turns streamed into this chat while the fetch was in flight are kept:
          // the server does not have them yet, and dropping them would erase an
          // answer the user is watching.
          const live = session.turns.filter((turn) => turn.key.startsWith("local-"));
          return {
            ...prev,
            [chatId]: {
              ...session,
              turns: [...detail.turns.map(restoredTurn), ...live],
              loaded: true,
              loadError: null,
            },
          };
        });
      } catch (err) {
        // 404 means it was deleted elsewhere — drop it rather than showing an
        // error for a chat that no longer exists.
        if (err.status === 404) {
          setList((prev) => prev.filter((chat) => chat.id !== chatId));
          setSessions((prev) => {
            const next = { ...prev };
            delete next[chatId];
            return next;
          });
          setActiveId((current) => (current === chatId ? null : current));
          return;
        }
        patchSession(chatId, () => ({ loadError: err.message, loaded: false }));
      }
    },
    [patchSession],
  );

  /** Create a chat and select it. */
  const startChat = useCallback(async () => {
    const id = crypto.randomUUID();
    // Added locally first so the new chat is on screen and typeable immediately;
    // the POST only has to succeed before the first question is asked, and that
    // question cannot be sent faster than a person can type it.
    const optimistic = {
      id,
      title: null,
      created_at: Date.now() / 1000,
      updated_at: Date.now() / 1000,
      turn_count: 0,
    };
    setList((prev) => [optimistic, ...prev]);
    setSessions((prev) => ({ ...prev, [id]: { ...newSession(), loaded: true } }));
    setActiveId(id);

    try {
      await apiCreateChat(id);
    } catch (err) {
      setList((prev) => prev.filter((chat) => chat.id !== id));
      setSessions((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setListError(err.message);
      return null;
    }
    return id;
  }, []);

  const selectChat = useCallback(
    (chatId) => {
      setActiveId(chatId);
      ensureSession(chatId);
      loadChat(chatId);
    },
    [ensureSession, loadChat],
  );

  const removeChat = useCallback(
    async (chatId) => {
      // Stop a run in flight first: its answer is about to have nowhere to go.
      controllers.current.get(chatId)?.abort();
      controllers.current.delete(chatId);

      let nextActive = null;
      setList((prev) => {
        const remaining = prev.filter((chat) => chat.id !== chatId);
        nextActive = remaining[0]?.id ?? null;
        return remaining;
      });
      setSessions((prev) => {
        const next = { ...prev };
        delete next[chatId];
        return next;
      });
      setActiveId((current) => (current === chatId ? nextActive : current));

      try {
        await apiDeleteChat(chatId);
      } catch (err) {
        // Already gone is the outcome we wanted; anything else is worth saying.
        if (err.status !== 404) setListError(err.message);
      }
    },
    [],
  );

  const retitleChat = useCallback(async (chatId, title) => {
    const trimmed = title.trim();
    if (!trimmed) return;

    let previous = null;
    setList((prev) =>
      prev.map((chat) => {
        if (chat.id !== chatId) return chat;
        previous = chat.title;
        return { ...chat, title: trimmed };
      }),
    );

    try {
      await apiRenameChat(chatId, trimmed);
    } catch (err) {
      // Put the old title back rather than leaving a name the server rejected.
      setList((prev) =>
        prev.map((chat) => (chat.id === chatId ? { ...chat, title: previous } : chat)),
      );
      if (err.status !== 404) setListError(err.message);
    }
  }, []);

  const setDraft = useCallback(
    (chatId, draft) => patchSession(chatId, () => ({ draft })),
    [patchSession],
  );

  const stopChat = useCallback((chatId) => {
    controllers.current.get(chatId)?.abort();
  }, []);

  /** Ask a question in a specific chat. */
  const ask = useCallback(
    async (chatId, question) => {
      const turn = newTurn(question);
      patchSession(chatId, (session) => ({
        turns: [...session.turns, turn],
        draft: "",
      }));

      const controller = new AbortController();
      controllers.current.set(chatId, controller);
      const startedAt = performance.now();

      try {
        const result = await submitQueryStream(question, {
          chatId,
          signal: controller.signal,
          onStages: (stages) => patchTurn(chatId, turn.key, () => ({ stages })),
          onStage: ({ stage, status, detail, content }) =>
            patchTurn(chatId, turn.key, (current) => {
              const previous = current.progress[stage];
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
              return { progress: { ...current.progress, [stage]: next } };
            }),
          // Arrives mid-run, so the meter is right even if the turn then fails.
          onUsage: (usage) => patchSession(chatId, () => ({ usage })),
          onToken: ({ stage, text }) =>
            patchTurn(chatId, turn.key, (current) => ({
              streamed: { ...current.streamed, [stage]: (current.streamed[stage] ?? "") + text },
            })),
        });

        patchSession(chatId, () => ({ usage: result.usage }));
        patchTurn(chatId, turn.key, () => ({
          result,
          status: "done",
          saved: result.saved !== false,
          elapsedMs: performance.now() - startedAt,
        }));

        // Reflect the new activity in the sidebar: title (set from the first
        // question) and position, since the list is ordered by last activity.
        setList((prev) => {
          const found = prev.find((chat) => chat.id === chatId);
          if (!found) return prev; // deleted mid-answer
          const updated = {
            ...found,
            title: found.title ?? question.slice(0, 120),
            turn_count: found.turn_count + 1,
            updated_at: Date.now() / 1000,
          };
          return [updated, ...prev.filter((chat) => chat.id !== chatId)];
        });
      } catch (err) {
        // A cancellation is not a failure: whatever the model wrote before the
        // stop is kept and shown, it just never becomes a finished turn.
        const cancelled = err.name === "AbortError";

        // 413 is the context cutoff for THIS chat only. Every other conversation
        // is unaffected, which is the point of giving each one its own window.
        if (err.status === 413) patchSession(chatId, () => ({ full: true }));

        patchTurn(chatId, turn.key, (current) => ({
          error: cancelled ? null : err.message,
          status: cancelled ? "cancelled" : "error",
          elapsedMs: performance.now() - startedAt,
          // Whichever stage was in flight is the one that stopped.
          progress: Object.fromEntries(
            Object.entries(current.progress).map(([id, stage]) =>
              stage.status === "active" ? [id, { ...stage, status: "failed" }] : [id, stage],
            ),
          ),
        }));
      } finally {
        // Only clear the controller if it is still ours — a chat asked again
        // after this run finished has already replaced it.
        if (controllers.current.get(chatId) === controller) {
          controllers.current.delete(chatId);
        }
      }
    },
    [patchSession, patchTurn],
  );

  // Load the chat list once, and open something: the most recent conversation if
  // there is one, otherwise a fresh chat, so the composer is never disabled for
  // want of somewhere to type.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const data = await fetchChats();
        if (cancelled) return;
        setList(data.chats);
        if (data.chats.length > 0) {
          setActiveId(data.chats[0].id);
          setSessions(
            Object.fromEntries(data.chats.map((chat) => [chat.id, newSession()])),
          );
          loadChat(data.chats[0].id);
        } else {
          await startChat();
        }
      } catch (err) {
        if (!cancelled) setListError(err.message);
      } finally {
        if (!cancelled) setReady(true);
      }
    })();

    return () => {
      cancelled = true;
    };
    // Deliberately once: this is initial load, not a subscription.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Leaving the page should free every model call this tab is holding, not just
  // the visible one.
  useEffect(() => {
    const inFlight = controllers.current;
    return () => inFlight.forEach((controller) => controller.abort());
  }, []);

  const active = activeId ? sessions[activeId] ?? null : null;
  // Which chats are working right now, for the sidebar. Derived from the turns
  // rather than tracked separately, so it cannot disagree with what is on screen.
  const running = new Set(
    Object.entries(sessions)
      .filter(([, session]) => session.turns.some((turn) => turn.status === "running"))
      .map(([chatId]) => chatId),
  );

  return {
    list,
    sessions,
    activeId,
    active,
    running,
    ready,
    listError,
    dismissListError: () => setListError(null),
    startChat,
    selectChat,
    removeChat,
    retitleChat,
    setDraft,
    ask,
    stopChat,
  };
}
