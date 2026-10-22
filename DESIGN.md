---
version: alpha
name: aihub
description: Private multi-model chat for one person on phone, Mac and PC. Local, free and paid models behind one composer, with the route and the reasoning intensity always visible.
colors:
  ink: "oklch(0.25 0.025 250)"
  paper: "oklch(0.985 0.004 250)"
  slate: "oklch(0.955 0.008 250)"
  raised: "oklch(0.998 0.002 250)"
  line: "oklch(0.895 0.012 250)"
  line-strong: "oklch(0.80 0.02 250)"
  muted: "oklch(0.50 0.025 250)"
  primary: "oklch(0.46 0.17 350)"
  on-primary: "oklch(0.99 0.008 350)"
  danger: "oklch(0.52 0.19 27)"
  amber: "oklch(0.84 0.14 82)"
  on-amber: "oklch(0.25 0.04 80)"
  free: "oklch(0.45 0.1 160)"
  free-bg: "oklch(0.94 0.04 160)"
  ink-night: "oklch(0.94 0.008 250)"
  paper-night: "oklch(0.19 0.022 250)"
  slate-night: "oklch(0.16 0.02 250)"
  raised-night: "oklch(0.235 0.024 250)"
  line-night: "oklch(0.31 0.025 250)"
  line-strong-night: "oklch(0.42 0.03 250)"
  muted-night: "oklch(0.72 0.02 250)"
  primary-night: "oklch(0.76 0.13 350)"
  on-primary-night: "oklch(0.2 0.03 350)"
  danger-night: "oklch(0.72 0.15 25)"
  amber-night: "oklch(0.78 0.13 82)"
  free-night: "oklch(0.8 0.12 160)"
  free-bg-night: "oklch(0.3 0.05 160)"
typography:
  title:
    fontFamily: Instrument Sans
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.25
  ui:
    fontFamily: Instrument Sans
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.45
  body:
    fontFamily: Instrument Sans
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.55
  reply:
    fontFamily: Literata
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.68
  small:
    fontFamily: Instrument Sans
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.4
    fontFeature: '"tnum" 1'
rounded:
  sm: 6px
  md: 10px
  lg: 16px
  xl: 22px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 32px
components:
  reply:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.reply}"
  reply-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.muted}"
    typography: "{typography.small}"
  sidebar-secondary:
    backgroundColor: "{colors.slate}"
    textColor: "{colors.muted}"
    typography: "{typography.small}"
  message-user:
    backgroundColor: "{colors.slate}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  composer:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 12px
  button-send:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.ui}"
    rounded: "{rounded.md}"
    height: 40px
  chip-free:
    backgroundColor: "{colors.free-bg}"
    textColor: "{colors.free}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: 2px 6px
  chip-paid:
    backgroundColor: "{colors.amber}"
    textColor: "{colors.on-amber}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: 2px 6px
  banner-error:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.danger}"
    typography: "{typography.ui}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  separator:
    backgroundColor: "{colors.line}"
    height: 1px
  separator-strong:
    backgroundColor: "{colors.line-strong}"
    height: 1px
  reply-night:
    backgroundColor: "{colors.paper-night}"
    textColor: "{colors.ink-night}"
    typography: "{typography.reply}"
  reply-secondary-night:
    backgroundColor: "{colors.paper-night}"
    textColor: "{colors.muted-night}"
    typography: "{typography.small}"
  sidebar-secondary-night:
    backgroundColor: "{colors.slate-night}"
    textColor: "{colors.muted-night}"
    typography: "{typography.small}"
  message-user-night:
    backgroundColor: "{colors.slate-night}"
    textColor: "{colors.ink-night}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  composer-night:
    backgroundColor: "{colors.raised-night}"
    textColor: "{colors.ink-night}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 12px
  button-send-night:
    backgroundColor: "{colors.primary-night}"
    textColor: "{colors.on-primary-night}"
    typography: "{typography.ui}"
    rounded: "{rounded.md}"
    height: 40px
  chip-free-night:
    backgroundColor: "{colors.free-bg-night}"
    textColor: "{colors.free-night}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: 2px 6px
  chip-paid-night:
    backgroundColor: "{colors.amber-night}"
    textColor: "{colors.on-amber}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: 2px 6px
  banner-error-night:
    backgroundColor: "{colors.paper-night}"
    textColor: "{colors.danger-night}"
    typography: "{typography.ui}"
    rounded: "{rounded.md}"
    padding: 12px 16px
  separator-night:
    backgroundColor: "{colors.line-night}"
    height: 1px
  separator-strong-night:
    backgroundColor: "{colors.line-strong-night}"
    height: 1px
---

## Overview
A tool for one person who opens it many times a day. It is Operate mode: scanability and speed beat expression, so the chrome is quiet and nearly everything is slate and ink. What makes it different from any single-vendor chat app is the route: every message goes to a model on this PC, a free cloud model, or a paid Claude model, at a chosen reasoning intensity. The interface keeps that choice visible and cheap to change, and says plainly where the prompt goes.

## Colors
- Cool slate neutrals at hue 250 carry about 95% of the surface. Ink and paper are tinted toward that hue; secondary text is `muted`, never a plain gray.
- Primary, a mulberry, is the only accent. It marks the primary button (Send, Save), the focus ring, text selection, the filled signal bars, links inside a reply and keywords in code. Nothing else is mulberry.
- Amber marks the paid tier so spend is never a surprise. Danger is for failed requests and the delete confirmation only. State is always paired with text or an icon, never color alone.
- The dark theme is a separate palette on the same hue: a deep slate canvas, raised surfaces lighter instead of shadowed, and a lighter, less saturated mulberry.

## Typography
- Instrument Sans for the interface and for your own messages. Literata for model replies, because replies are the long text people read, and the serif separates the model's voice from the interface.
- Scale 12, 14, 16, 20, 24, 30 px. Reply text is 16 px on 1.68 leading, 44 rem measure at most. Numbers that are compared (tokens, cost, speed) use tabular figures.
- Code uses the system monospace. Uppercase is not used for labels.

## Layout
- 4 px base. Spacing tokens only: 4, 8, 12, 16, 24, 32. Comfortable density in the conversation, compact density in the sidebar.
- 280 px sidebar plus a conversation column of 44 rem, centered in the pane, with left-aligned text. Under 1024 px the sidebar is a drawer; on a narrow phone the model chip takes the first row of the composer and the intensity chip (or, for an image model, the Shape chip) and Send share the second, with 44 px touch targets.
- The composer is docked, and the page uses `dvh` and safe-area insets so the on-screen keyboard never covers it.

## Elevation & Depth
- Panes are separated by background tone and spacing. The composer has one soft shadow; layers that float above the page (popovers, dialogs, the drawer, toasts, the jump button) share another; nothing else casts a shadow. A surface uses a border or a shadow, not both.

## Shapes
- Radius scale 6 (chips), 10 (buttons, message blocks), 16 (popovers, dialogs), 22 (composer). Nested radius is the outer radius minus the padding. Nothing is pill-shaped.

## Components
- **Signal bars:** four rising bars. In the composer they show the intensity level (Off is all outline). While a model is thinking they animate as an activity indicator. In the sidebar they are the wordmark. This is the one distinctive element.
- **Composer:** one rounded surface holding the text area, the model chip and the intensity chip, and the Send button, which becomes Stop while a reply streams.
- **Projects section:** the first thing in the chat list, above the day groups. A header row ("Projects" and a plus button) is followed by one disclosure row per project: a chevron that turns when open, the name cut with an ellipsis, the number of its chats in `muted`, and two icon buttons (new chat in the project, options) that show on hover and focus, and always on touch. Its chats sit under it, indented, with no rule or coloured edge. With no project, one muted line says what they are for. Open or closed is kept per browser, and opening a chat that is inside a closed project opens it.
- **Project dialog:** a modal like Settings: Name and Instructions, a hint that says where the instructions go and, once there is text, about how many tokens of the model's memory they take, and Cancel and Save. A missing name is said next to the field, which takes focus; Enter in the name saves. Deleting asks in a second dialog that says what stays, and offers one unchecked box to delete the chats too.
- **Scheduled tasks page:** a view in place of the conversation, opened from "Scheduled tasks" above Settings in the sidebar (a link, `aria-current` on that page). The title is "Scheduled tasks", the composer is gone, and the skip link reads "Skip to the task list" and goes to "New task". One muted sentence says what a task is, where it runs and how long it may take, with a "New task" button; below it a card for each task, or one line when there are none.
- **Task card:** a raised block with a 1 px `line` outline. The name and the on/off switch share the first row; then the schedule in words, the model and the project, separated by space and never by a dot; the first line of the prompt, cut with an ellipsis; the last run's status (Done, Failed, Stopped or Skipped with the time, and the reason under it in the error colour, or "Running now" with the signal bars, or "Not run yet"); "Next run tomorrow 8:00 AM" while the task is on; and the buttons Run now, Edit and Delete, named with the task ("Run now: Morning digest") so that each says what it acts on. A task that the scheduler switched off shows its explanation in place of the status. The "Open chat" link appears once a run has finished. The page asks again every 2 seconds while a task runs and every 30 otherwise, and redraws only cards whose words would change; when a run ends, the live region says so ("Morning digest: Done today 08:02").
- **Switch:** the input itself, 44 px on touch, with a square track and thumb (nothing is pill-shaped). Its name is the task's; "On" or "Off" beside it is for the eyes only.
- **Task dialog:** a modal like the project dialog: Name, Prompt, Model (only models on this PC), Intensity (only where the model has levels), Repeat with a Time and, for a weekly task, a Day, Project, and "Run on this schedule". A hint under Repeat says that times are on this PC's clock and how a missed run is handled. A message about the form (what is missing, why the server refused) sits in the footer beside Save, which stays in view when the form scrolls. An edit sends only what was changed.
- **Task mark:** a small clock before the title of a chat that a scheduled task made, in the list and with "Scheduled task: " for a screen reader; the open chat's title line says "Scheduled task: <name>".
- **Title line:** under a chat's title, "Project: <name>" and, in its last week, the deletion warning beside it, separated by space and never by a dot. A long project name is cut with an ellipsis on the line and never widens the page.
- **Model picker:** a searchable list grouped as Free (cloud), On this PC, Makes images, and Paid. Each row states tier, context size and price in words; an image model states what it is good at instead. Unavailable rows say what is missing.
- **Move menu:** the same small choice popover as the Shape chip's, opened from a chat's menu: "No project" and every project by name, the current one checked, a long name cut with an ellipsis. It opens under the menu button that led to it, or over it when the room under it is smaller, and never taller than that room: past that the rows scroll. A phone shows it as the bottom sheet that the other choice menus use.
- **Shape chip:** in image mode it takes the intensity chip's place and opens the same small menu: Square, Wide, Tall, with the usual seconds for each in the note under the choices. It is text only and has no icon.
- **Image reply:** a figure at column width, never taller than 70% of the window, with a 1 px `line` outline so a dark image does not disappear into the dark theme. While it is made, a slate in the same box (the same size rule, so the image replaces it without a jump) holds signal bars (one per step up to four, else one per two steps) and a line of state in `muted`: "Waiting for the GPU…", "Step 3 of 8", "Finishing…". A model estimated under ten seconds shows looping bars and "About 8 s" instead of steps. The slate is a progress bar that a keyboard can reach and that is never announced as it changes; the page says "Creating image", "Still working" and the result once each. The footer lists model, seconds, steps, seed and size, then the actions as text buttons: Download, New seed, Reuse prompt, Redo with the other model, Full size. They stay visible, unlike Copy and Retry, which show on hover.
- **Reply:** no container, serif text. Reasoning sits above it in a collapsed "Thought for 8 s" disclosure.
- **Empty state:** a short title naming the model, then one sentence about where the next prompt goes. No greeting, no suggestion chips.

## Do's and Don'ts
- Do say where a prompt goes: stays on this PC, may be logged by a free host, or billed to the API credits.
- Do show the cost of paid replies and the speed of local ones in the message footer, as separate items.
- Do keep every control reachable by keyboard and 44 px on touch.
- Don't use a cream background with terracotta, a serif display headline, or any Anthropic mark, name or typeface.
- Don't add a sparkle or star logo, suggestion chips, gradient washes, glass panels, or colored left borders.
- Don't join meta items with middle dots or put an arrow after link text.
- Don't animate list navigation, keyboard actions or streaming text. Motion only confirms an action or signals that a model is working.

## Motion
- Press 120 ms, popover 160 ms, drawer 240 ms, `cubic-bezier(0.23, 1, 0.32, 1)`. Exits run faster. Only `transform` and `opacity` animate.
- The signal bars loop at 900 ms while a model is working. Reduced motion shows static bars and no drawer slide; the bars of an image being made then light one by one as steps finish, or, for a short job, all light.
