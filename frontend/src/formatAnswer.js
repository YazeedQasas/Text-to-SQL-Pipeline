/**
 * Strip Markdown syntax the answer model was told not to emit.
 *
 * The answer prompt asks for plain text, because there is no Markdown renderer
 * here and the answer is right-to-left Arabic — `**bold**` and `*   ` bullets
 * show up as literal punctuation the reader has to look past. A small local
 * model still slips into Markdown often enough that the instruction alone is
 * not enough, so this cleans up what gets through rather than pulling in a
 * renderer for output that is meant to be prose.
 *
 * Deliberately narrow: it handles the two things Gemma actually emits, and
 * leaves a lone `*` alone rather than guessing whether it is emphasis or a
 * literal asterisk. Each pattern is line-bounded so that syntax cut mid-stream
 * cannot swallow the rest of the answer while the next chunk is in flight.
 */
export default function formatAnswer(text) {
  if (!text) return text;

  return (
    text
      // **bold** / __bold__ -> the words themselves
      .replace(/\*\*([^\n*]+)\*\*/g, "$1")
      .replace(/__([^\n_]+)__/g, "$1")
      // "*   item" / "+ item" / "-   item" -> one dash, one space. Leading
      // whitespace is kept: `white-space: pre-wrap` turns it into a visual
      // indent, which is the only thing left marking a nested item.
      .replace(/^([ \t]*)[*+-][ \t]+/gm, "$1- ")
      // "### heading" -> heading
      .replace(/^[ \t]*#{1,6}[ \t]+/gm, "")
      // `code` -> code
      .replace(/`([^`\n]+)`/g, "$1")
  );
}
