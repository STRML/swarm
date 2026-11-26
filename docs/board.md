# Live TUI board & room picker

Run `swarm board` to open the room picker, or `swarm board --room <R>` to jump straight into a room.

![Swarm board](assets/board.gif)

## Roster & animated avatars

- Cards display a 4x5 animated Unicode avatar matched from each member's `model` (preferred) or `harness`:
  - **Gemini / `agy`:** buzzing bee
  - **Claude:** Clawd mascot
  - **Codex / GPT:** rotating hex-knot
  - **DeepSeek:** spouting whale
  - **OMP / GLM / Kimi:** pulsing orbital node
  - **Other:** diamond
- Parallel members of the same vendor pick distinct tints from that vendor's palette.
- `working` members animate briskly; `waiting` members slow to half-speed `zZ` sleeping frames; `gone` and `retired`
  members render dim.
- The focused pane gets a `#58a6ff` left accent bar. Click any pane or press `tab` to switch focus.

## Room picker

![Room picker](assets/picker.png)

When opened with no `--room`, the picker sweeps rooms idle longer than `SWARM_ARCHIVE_IDLE_HOURS` (default `6`,
set `0` to disable) that have zero present members, then lists live rooms newest-first. Archive (`a`) and clear
(`x` then `y`) refuse when members are present and print the exact `swarm ... --force` command instead.

## Keybindings & mouse

Click any roster card or post row to select it directly, and use the mouse wheel to scroll roster, posts, or
messages.

| Key | In board | In room picker |
|---|---|---|
| `← → ↑ ↓` / `h j k` | 2D grid navigation in roster, move in posts, or scroll message | Move selection |
| `tab` | Cycle pane (`roster` / `posts` / `message`) | - |
| `m` | Open inline compose bar (`tab` toggles `@selected`/`all`; `@who` or `/to` overrides recipient; `enter` posts as `operator`) | - |
| `l` | Toggle live log peek for selected member (`esc` or `l` closes) | - |
| `d` then `y` | Finish swarm (`--kind done` to `all`; any other key cancels) | - |
| `enter` | Hide/show full message pane | Open board for selected room |
| `space` | Expand/collapse selected post inline | - |
| `p` | Cycle sender filter (`esc` clears) | - |
| `f` | Toggle live follow | - |
| `z` | Zoom message pane full-screen (`esc` restores) | - |
| `o` | Open selected member's `workspace` directory | - |
| `b` | Return to room picker | - |
| `a` | - | Archive selected room |
| `x` then `y` | - | Permanently clear selected room (`esc` or any other key cancels) |
| `r` | - | Refresh room list |
| `q` | Quit | Quit |
