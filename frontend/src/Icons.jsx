/**
 * Inline SVG icons.
 *
 * Inline rather than an icon font or a sprite sheet: there are six of them, they
 * are needed at one size, and both alternatives add a network request to draw a
 * pencil.
 *
 * Emoji were here first and are gone on purpose. They render from whatever the
 * OS ships, so the same UI is flat on one machine and glossy on another, they
 * carry colour that cannot be matched to the palette, and they sit on the text
 * baseline rather than on the optical centre of a button. These take
 * `currentColor`, so an icon inside an active nav item is the same blue as its
 * label without a second rule.
 *
 * All are 16×16 on a 24-unit grid with `stroke-width: 1.8`, which keeps their
 * weight close to the surrounding 0.85rem text.
 */

function Icon({ children, size = 16, className = "" }) {
  return (
    <svg
      className={`icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      // Decorative in every current use: each one sits next to a text label or
      // inside a button that carries its own aria-label.
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** The Ask page — a conversation. */
export function ChatIcon(props) {
  return (
    <Icon {...props}>
      <path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z" />
    </Icon>
  );
}

/** The Admin page. */
export function SettingsIcon(props) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
    </Icon>
  );
}

/** Start a new chat. */
export function PlusIcon(props) {
  return (
    <Icon {...props}>
      <path d="M12 5v14M5 12h14" />
    </Icon>
  );
}

/** Rename a chat. */
export function PencilIcon(props) {
  return (
    <Icon {...props}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </Icon>
  );
}

/** Delete a chat. */
export function TrashIcon(props) {
  return (
    <Icon {...props}>
      <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0-1 14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1L5 6" />
      <path d="M10 11v6M14 11v6" />
    </Icon>
  );
}

/** Dismiss an error. */
export function CloseIcon(props) {
  return (
    <Icon {...props}>
      <path d="M18 6 6 18M6 6l12 12" />
    </Icon>
  );
}

/* --- Admin widget headers ----------------------------------------------------
   Drawn at 18px against a tinted circle, so each one carries its widget's accent
   colour rather than the emoji's own. */

/** The legal glossary. */
export function BookIcon(props) {
  return (
    <Icon {...props}>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z" />
      <path d="M9 7h7M9 11h7" />
    </Icon>
  );
}

/** Schema changes waiting to be reviewed. */
export function RefreshIcon(props) {
  return (
    <Icon {...props}>
      <path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-7.9-4.7" />
      <path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 7.9 4.7" />
      <path d="M20 3v5h-5M4 21v-5h5" />
    </Icon>
  );
}

/** The activity feed. */
export function ListIcon(props) {
  return (
    <Icon {...props}>
      <path d="M8 6h13M8 12h13M8 18h13" />
      <path d="M3 6h.01M3 12h.01M3 18h.01" />
    </Icon>
  );
}

/** The repeated-question cache. */
export function BoltIcon(props) {
  return (
    <Icon {...props}>
      <path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z" />
    </Icon>
  );
}
